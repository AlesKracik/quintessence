#!/usr/bin/env python3
"""
lean_gen.py — the Lean model of a Quint area, generated from the Quint one.

A `proof: "lean"` invariant is proven about a Lean model, and the replay
(tools/lean_backend.py) checks that model against the Quint one trace by
trace. A hand-written model can still drift where no trace goes; a generated
one cannot drift at all — it is a translation, rerun whenever the `.qnt`
changes. This file is that translation.

    namespace <Module>                       -- e.g. Spec.Subscription
      <types>     a sum becomes an inductive, a record a structure, an alias
                  an abbrev
      <defs>      every pure val/def as a function; one that reads state
                  takes `(s : State)` first
      structure State   one field per var, own and imported, unqualified
      inductive Action  one constructor per action of the module, its
                  parameters in order
      def init  : State
      def apply : Action → State → Option State   -- none = guard false

It reads the Quint compiler's TYPED IR (`quint typecheck --out`), so it
never guesses a type or what a name refers to: the IR's name table resolves
every reference — a parameter, a `val` of an action, a def of an imported
module (`auth::isLocked` becomes `auth.isLocked`) — and its type map gives
each expression's type where Lean needs one (`Map()`, `Set()`).

Where Quint and Lean differ, the Lean model admits AT LEAST every transition
the Quint one does, so a proof about every reachable Lean state covers every
reachable Quint state:

  * `m.get(k)` on a missing key is a Quint error, a step that never
    happens; Lean reads `default`. `set` on a missing key inserts.
  * The Lean model's steps are `apply a s` for EVERY action `a` and every
    argument, not just the arguments `step` picks; `step` is checked to be
    a choice among the module's actions and nothing more.

The other direction — that each Quint step IS a Lean step — is what the
replay checks, on every trace the area has.

Supported: bool/int/str, maps, sets, tuples, records and sum types (with
payloads); the boolean, integer, map and set operators; `if`, `match`,
`val` and lambdas; actions made of guards, assignments, `val`s and calls
to other actions (inlined). Anything else — `nondet` inside an action, an
action-level `any`/`if`, lists, `const`s, temporal operators — stops the
generation with the construct named, and the verdict is not-checkable
rather than a guess.

    python3 tools/lean_gen.py specs/x.qnt --namespace Spec.X [--out F] [--check]
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from proc import run_group  # noqa: E402

# Bump when the generated text changes for the same input: every generated
# model then reads as stale until it is regenerated.
GEN_VERSION = "1"
HEADER_SHA_RE = re.compile(r"^source_sha: (\S+)$", re.MULTILINE)
QUINT_TIMEOUT_S = 120

LEAN_KEYWORDS = {
    "abbrev", "at", "axiom", "by", "calc", "class", "deriving", "do", "else",
    "end", "example", "export", "for", "from", "fun", "have", "if", "import",
    "in", "inductive", "instance", "let", "local", "match", "mutual",
    "namespace", "noncomputable", "open", "partial", "private", "protected",
    "return", "section", "show", "structure", "then", "theorem", "universe",
    "unsafe", "variable", "where", "with", "Type", "Prop", "Sort", "λ",
    "macro", "syntax", "notation", "def", "opaque", "omit", "include",
}
# Names the generated module defines itself.
RESERVED = {"State", "Action", "init", "apply", "s"}


class LeanGenError(Exception):
    pass


def esc(name):
    return f"«{name}»" if name in LEAN_KEYWORDS else name


# ── Input ────────────────────────────────────────────────────────────────────

MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_]\w*)\s*\{", re.MULTILINE)


def source_sha(qnt_path):
    """What the generated model is a translation OF: the canonical Quint
    model — the file and every module it imports, comments, layout,
    declaration order and runs aside (itf_tools.canonical_quint) — plus the
    generator's version. The generated header records it, and spec-lint
    compares without running Quint. The generated text is a function of the
    same thing: every list in it is emitted in a fixed order, never in the
    order the file declares."""
    from itf_tools import _model_texts, canonical_quint
    h = hashlib.sha256(f"lean_gen {GEN_VERSION}\n".encode())
    for text in _model_texts(Path(qnt_path)):
        h.update(canonical_quint(text).encode("utf-8"))
        h.update(b"\0")
    return "src:" + h.hexdigest()


def header_sha(text):
    m = HEADER_SHA_RE.search(text or "")
    return m.group(1) if m else None


def typecheck(qnt_path, quint="quint"):
    """The typed IR, or LeanGenError saying why there is none."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ir.json"
        try:
            proc = run_group([quint, "typecheck", f"--out={out}", str(qnt_path)],
                             timeout=QUINT_TIMEOUT_S)
        except FileNotFoundError:
            raise LeanGenError("the quint CLI is not on PATH: the Lean model is "
                               "generated from its typed IR")
        except subprocess.TimeoutExpired:
            raise LeanGenError(f"quint typecheck timed out after {QUINT_TIMEOUT_S}s")
        if not out.exists() or not out.stat().st_size:
            raise LeanGenError("quint typecheck produced no IR: "
                               + ((proc.stderr or proc.stdout or "").strip()[-400:]))
        ir = json.loads(out.read_text(encoding="utf-8"))
    if ir.get("errors"):
        raise LeanGenError(f"{Path(qnt_path).name} does not typecheck "
                           f"({len(ir['errors'])} errors)")
    return ir


# ── The translation ──────────────────────────────────────────────────────────

def _row(t):
    """The fields of a row type, in order: [(name, type)]."""
    out = []
    while t and t.get("kind") == "row":
        out.extend((f["fieldName"], f["fieldType"]) for f in t.get("fields") or [])
        t = t.get("other")
    return out


def _is_unit(t):
    return t.get("kind") == "tup" and not _row(t.get("fields") or {})


class Gen:
    def __init__(self, ir, main_name, init="init", step="step"):
        self.ir = ir
        self.table = ir.get("table") or {}
        self.types = ir.get("types") or {}
        self.init_name, self.step_name = init, step
        mods = {m["name"]: m for m in ir.get("modules") or []}
        if main_name not in mods:
            raise LeanGenError(f"no module {main_name} in the IR")
        self.main = main_name
        self.decls = {}      # id -> decl
        self.module_of = {}  # id -> module name
        for m in ir["modules"]:
            for d in m.get("declarations") or []:
                if "id" in d:
                    self.decls[d["id"]] = d
                    self.module_of[d["id"]] = m["name"]
        self.vars = sorted((d for d in self._decls() if d["kind"] == "var"),
                           key=lambda d: d["name"])
        self._sums = {}      # frozenset(labels) -> lean type name
        self._recs = {}      # tuple(sorted fields) -> lean type name
        self._ctor = {}      # decl id -> (lean type name, label, has payload)
        self._typedefs = {}  # (module, name) -> lean name
        self._state_dep = {}

    # -- declarations

    def _decls(self, module=None):
        for m in self.ir["modules"]:
            if module and m["name"] != module:
                continue
            for d in m.get("declarations") or []:
                yield d

    def lname(self, d):
        """The Lean name of a top-level declaration."""
        mod = self.module_of.get(d["id"], self.main)
        base = esc(d["name"])
        return base if mod == self.main else f"{mod}.{base}"

    def ref(self, node):
        r = self.table.get(str(node["id"]))
        if r is None:
            return None
        return self.decls.get(r.get("id"), r)

    def is_action(self, d):
        return d.get("kind") == "def" and d.get("qualifier") == "action"

    # -- types

    def collect_types(self):
        for d in self._decls():
            if d["kind"] != "typedef":
                continue
            if "type" not in d:
                raise LeanGenError(f"type {d['name']} is uninterpreted; the Lean model "
                                   f"needs every type defined")
            name = self.lname(d)
            self._typedefs[(self.module_of[d["id"]], d["name"])] = name
            t = d["type"]
            if t["kind"] == "sum":
                self._sums.setdefault(frozenset(l for l, _ in _row(t["fields"])), name)
            elif t["kind"] == "rec":
                self._recs.setdefault(tuple(sorted(l for l, _ in _row(t["fields"]))), name)
        for d in self._decls():
            if d.get("kind") == "def" and d.get("qualifier") in ("val", "def"):
                e = d.get("expr") or {}
                body = e.get("expr") if e.get("kind") == "lambda" else e
                if body and body.get("kind") == "app" and body.get("opcode") == "variant":
                    label = body["args"][0]["value"]
                    tname = self._sum_of_label(label, d)
                    self._ctor[d["id"]] = (tname, label, e.get("kind") == "lambda")

    def _sum_of_label(self, label, d):
        mod = self.module_of[d["id"]]
        for td in self._decls(mod):
            if td["kind"] == "typedef" and td.get("type", {}).get("kind") == "sum":
                if label in [l for l, _ in _row(td["type"]["fields"])]:
                    return self.lname(td)
        raise LeanGenError(f"variant {label} belongs to no named sum type")

    def ty(self, t, where=""):
        k = t.get("kind")
        if k == "bool":
            return "Bool"
        if k == "int":
            return "Int"
        if k == "str":
            return "String"
        if k == "const":
            name = t["name"]
            mod = self.main
            if "::" in name:
                mod, name = name.rsplit("::", 1)
            for m in (mod, *[m["name"] for m in self.ir["modules"]]):
                if (m, name) in self._typedefs:
                    return self._typedefs[(m, name)]
            raise LeanGenError(f"type {t['name']} is not defined{where}")
        if k == "fun":
            return f"(Quint.QMap {self.ty(t['arg'], where)} {self.ty(t['res'], where)})"
        if k == "set":
            return f"(Quint.QSet {self.ty(t['elem'], where)})"
        if k == "tup":
            items = [ft for _, ft in _row(t.get("fields") or {})]
            if not items:
                return "Unit"
            return "(" + " × ".join(self.ty(x, where) for x in items) + ")"
        if k == "sum":
            key = frozenset(l for l, _ in _row(t["fields"]))
            if key in self._sums:
                return self._sums[key]
            raise LeanGenError(f"an anonymous sum type{where}: name it with `type`")
        if k == "rec":
            key = tuple(sorted(l for l, _ in _row(t["fields"])))
            if key in self._recs:
                return self._recs[key]
            raise LeanGenError(f"an anonymous record type{where}: name it with `type`")
        if k == "list":
            raise LeanGenError(f"lists are not supported yet{where}")
        if k == "var":
            raise LeanGenError(f"a polymorphic type{where}: annotate it")
        raise LeanGenError(f"no Lean type for {k}{where}")

    def type_of(self, node, where=""):
        t = self.types.get(str(node["id"]))
        if not t:
            raise LeanGenError(f"no type for expression {node['id']}{where}")
        return self.ty(t["type"], where)

    def _typedefs_ordered(self):
        """Every typedef, each after the ones it names, otherwise by name."""
        tds = sorted((d for d in self._decls() if d["kind"] == "typedef"),
                     key=lambda d: self.lname(d))
        by_name = {}
        for d in tds:
            by_name.setdefault(d["name"], []).append(d)
        order, seen = [], set()

        def names_in(t, acc):
            if isinstance(t, dict):
                if t.get("kind") == "const":
                    acc.add(t["name"].rsplit("::", 1)[-1])
                for v in t.values():
                    names_in(v, acc)
            elif isinstance(t, list):
                for v in t:
                    names_in(v, acc)
            return acc

        def visit(d):
            if d["id"] in seen:
                return
            seen.add(d["id"])
            for n in sorted(names_in(d.get("type"), set())):
                for dep in by_name.get(n, []):
                    if self.module_of[dep["id"]] == self.module_of[d["id"]]:
                        visit(dep)
            order.append(d)
        for d in tds:
            visit(d)
        return order

    def emit_types(self):
        out = []
        for d in self._typedefs_ordered():
            name, t = self.lname(d), d["type"]
            where = f" (type {d['name']})"
            if t["kind"] == "sum":
                ctors = _row(t["fields"])
                lines = [f"inductive {name} where"]
                for label, ft in ctors:
                    if _is_unit(ft):
                        lines.append(f"  | {esc(label)}")
                    else:
                        lines.append(f"  | {esc(label)} (x : {self.ty(ft, where)})")
                lines.append("  deriving DecidableEq, Repr, Inhabited")
                if all(_is_unit(ft) for _, ft in ctors):
                    # Ordered by constructor, so it can key a map or fill a set.
                    lines += [
                        f"instance : LT {name} := ⟨fun a b => a.toCtorIdx < b.toCtorIdx⟩",
                        f"instance : DecidableRel (α := {name}) (· < ·) :=",
                        f"  fun a b => inferInstanceAs (Decidable (a.toCtorIdx < b.toCtorIdx))",
                    ]
                out.append("\n".join(lines))
            elif t["kind"] == "rec":
                lines = [f"structure {name} where"]
                for label, ft in _row(t["fields"]):
                    lines.append(f"  {esc(label)} : {self.ty(ft, where)}")
                lines.append("  deriving DecidableEq, Repr, Inhabited")
                out.append("\n".join(lines))
            else:
                out.append(f"abbrev {name} := {self.ty(t, where)}")
        return out

    # -- expressions

    def reads_state(self, d, stack=()):
        """True when a top-level def reads a var, directly or through
        another def."""
        did = d["id"]
        if did in self._state_dep:
            return self._state_dep[did]
        if did in stack:
            return False
        found = [False]

        def walk(n):
            if isinstance(n, dict):
                if n.get("kind") in ("name", "app") and "id" in n:
                    r = self.ref(n)
                    if r is not None and r is not d:
                        if r.get("kind") == "var":
                            found[0] = True
                        elif r.get("id") in self.decls and r.get("kind") == "def" \
                                and not self.is_action(r):
                            if self.reads_state(r, stack + (did,)):
                                found[0] = True
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(d.get("expr"))
        self._state_dep[did] = found[0]
        return found[0]

    def ex(self, n, env, st, where):
        """A Quint expression as a Lean term. `env` maps a local's decl id to
        the Lean term it stands for; `st` is the state binder, or None where
        no state is in scope."""
        k = n.get("kind")
        if k == "bool":
            return "true" if n["value"] else "false"
        if k == "int":
            v = int(n["value"])
            return f"({v} : Int)"
        if k == "str":
            from lean_backend import lean_string
            return lean_string(n["value"])
        if k == "name":
            return self._name(n, env, st, where)
        if k == "app":
            return self._app(n, env, st, where)
        if k == "lambda":
            env2 = dict(env)
            ps = []
            for p in n["params"]:
                pn = self._local(p["name"], env2, st)
                env2[p["id"]] = pn
                ps.append(pn)
            return f"(fun {' '.join(ps)} => {self.ex(n['expr'], env2, st, where)})"
        if k == "let":
            od = n["opdef"]
            if od.get("qualifier") == "nondet":
                raise LeanGenError(f"`nondet {od['name']}` inside an expression{where}")
            env2 = dict(env)
            ln = self._local(od["name"], env2, st)
            val = self.ex(od["expr"], env, st, where)
            env2[od["id"]] = ln
            return f"(let {ln} := {val}; {self.ex(n['expr'], env2, st, where)})"
        raise LeanGenError(f"no Lean for expression kind {k}{where}")

    def _local(self, name, env, st):
        n = esc(name)
        taken = {v for v in env.values()} | ({st} if st else set()) | {"s"}
        while n in taken:
            n = n + "_"
        return n

    def _name(self, n, env, st, where):
        r = self.ref(n)
        if r is None:
            raise LeanGenError(f"`{n['name']}` is a Quint builtin with no Lean meaning{where}")
        if r.get("id") in env:
            return env[r["id"]]
        if r.get("kind") == "var":
            if not st:
                raise LeanGenError(f"`{n['name']}` reads state where none is in scope{where}")
            return f"{st}.{esc(r['name'])}"
        if r.get("id") in self._ctor:
            tname, label, payload = self._ctor[r["id"]]
            return f"{tname}.{esc(label)}"
        if r.get("kind") == "def" and r.get("id") in self.decls:
            if self.is_action(r):
                raise LeanGenError(f"action {n['name']} used as a value{where}")
            if r.get("qualifier") in ("temporal", "run"):
                raise LeanGenError(f"{r['qualifier']} {n['name']} used in a state model{where}")
            if self.reads_state(r):
                if not st:
                    raise LeanGenError(f"`{n['name']}` reads state where none is in scope{where}")
                return f"({self.lname(r)} {st})"
            return self.lname(r)
        raise LeanGenError(f"`{n['name']}` refers to nothing the model defines{where}")

    def _app(self, n, env, st, where):
        op, args = n["opcode"], n.get("args") or []
        a = lambda i: self.ex(args[i], env, st, where)  # noqa: E731
        allargs = lambda: [self.ex(x, env, st, where) for x in args]  # noqa: E731
        binop = {
            "iadd": "+", "isub": "-", "imul": "*", "idiv": "/", "imod": "%",
        }
        cmp_ = {"ilt": "<", "ilte": "≤", "igt": ">", "igte": "≥"}
        if op in binop:
            return f"({a(0)} {binop[op]} {a(1)})"
        if op in cmp_:
            return f"(decide ({a(0)} {cmp_[op]} {a(1)}))"
        if op == "iuminus":
            return f"(-{a(0)})"
        if op == "eq":
            return f"({a(0)} == {a(1)})"
        if op == "neq":
            return f"({a(0)} != {a(1)})"
        if op == "and":
            return "(" + " && ".join(allargs()) + ")" if args else "true"
        if op == "or":
            return "(" + " || ".join(allargs()) + ")" if args else "false"
        if op == "not":
            return f"(!{a(0)})"
        if op == "implies":
            return f"(!{a(0)} || {a(1)})"
        if op == "iff":
            return f"({a(0)} == {a(1)})"
        if op == "ite":
            return f"(if {a(0)} then {a(1)} else {a(2)})"
        # maps
        if op == "get":
            return f"({a(0)}.get {a(1)})"
        if op in ("set", "put"):
            return f"({a(0)}.set {a(1)} {a(2)})"
        if op == "setBy":
            return f"({a(0)}.setBy {a(1)} {a(2)})"
        if op == "keys":
            return f"({a(0)}.keySet)"
        if op == "mapBy":
            return f"(Quint.QSet.mapBy {a(0)} {a(1)})"
        if op == "Map":
            if not args:
                return f"(Quint.QMap.empty : {self.type_of(n, where)})"
            return f"(Quint.QMap.ofList [{', '.join(allargs())}] : {self.type_of(n, where)})"
        # sets
        if op == "Set":
            if not args:
                return f"(Quint.QSet.empty : {self.type_of(n, where)})"
            return f"(Quint.QSet.ofList [{', '.join(allargs())}] : {self.type_of(n, where)})"
        if op == "contains":
            return f"({a(0)}.contains {a(1)})"
        if op == "in":
            return f"({a(1)}.contains {a(0)})"
        if op == "forall":
            return f"({a(0)}.all {a(1)})"
        if op == "exists":
            return f"({a(0)}.any {a(1)})"
        if op == "union":
            return f"({a(0)}.union {a(1)})"
        if op == "intersect":
            return f"({a(0)}.inter {a(1)})"
        if op == "exclude":
            return f"({a(0)}.diff {a(1)})"
        if op == "subseteq":
            return f"({a(0)}.subseteq {a(1)})"
        if op == "size":
            return f"({a(0)}.size)"
        if op == "filter":
            return f"({a(0)}.filter {a(1)})"
        if op == "map":
            return f"({a(0)}.map {a(1)})"
        if op == "fold":
            return f"({a(0)}.fold {a(1)} {a(2)})"
        if op == "to":
            return f"(Quint.QSet.range {a(0)} {a(1)})"
        # tuples and records
        if op == "Tup":
            return "()" if not args else "(" + ", ".join(allargs()) + ")"
        if op == "item":
            tt = self.types.get(str(args[0]["id"]), {}).get("type", {})
            size = len(_row(tt.get("fields") or {}))
            i = int(args[1]["value"])
            path = ".2" * (i - 1) + (".1" if i < size else "")
            return f"({a(0)}{path})"
        if op == "Rec":
            pairs = [(args[i]["value"], self.ex(args[i + 1], env, st, where))
                     for i in range(0, len(args), 2)]
            body = ", ".join(f"{esc(f)} := {v}" for f, v in pairs)
            return f"({{ {body} }} : {self.type_of(n, where)})"
        if op == "field":
            return f"({a(0)}.{esc(args[1]['value'])})"
        if op == "with":
            return f"{{ {a(0)} with {esc(args[1]['value'])} := {a(2)} }}"
        if op == "matchVariant":
            scrut = a(0)
            tname = self.type_of(args[0], where)
            arms = []
            for i in range(1, len(args), 2):
                label = args[i]["value"]
                lam = args[i + 1]
                env2 = dict(env)
                p = lam["params"][0]
                pn = self._local(p["name"], env2, st)
                env2[p["id"]] = pn
                body = self.ex(lam["expr"], env2, st, where)
                if label == "_":
                    arms.append(f"| _ => {body}")
                else:
                    ctor_t = self._ctor_payload(tname, label)
                    pat = f"{tname}.{esc(label)}" + ("" if ctor_t is None else f" {pn}")
                    arms.append(f"| {pat} => {body}")
            return f"(match {scrut} with {' '.join(arms)})"
        # user operators
        r = self.ref(n)
        if r is not None and r.get("id") in self._ctor:
            tname, label, _ = self._ctor[r["id"]]
            return f"({tname}.{esc(label)} {' '.join(allargs())})"
        if r is not None and r.get("kind") == "def" and r.get("id") in self.decls:
            if self.is_action(r):
                raise LeanGenError(f"action {op} called inside an expression{where}")
            pre = [st] if self.reads_state(r) else []
            if pre and not st:
                raise LeanGenError(f"`{op}` reads state where none is in scope{where}")
            return f"({' '.join([self.lname(r)] + pre + allargs())})"
        raise LeanGenError(f"operator `{op}` has no Lean translation yet{where}")

    def _ctor_payload(self, tname, label):
        for d in self._decls():
            if d["kind"] == "typedef" and self.lname(d) == tname:
                for l, ft in _row(d["type"]["fields"]):
                    if l == label:
                        return None if _is_unit(ft) else ft
        return None

    # -- actions

    def act(self, n, env, where, out):
        """Flatten an action into `out` = {"guards": [...], "assigns": {var: term}}."""
        k = n.get("kind")
        if k == "let":
            od = n["opdef"]
            if od.get("qualifier") == "nondet":
                raise LeanGenError(f"`nondet {od['name']}` inside an action{where}: "
                                   f"make it a parameter, chosen in step")
            if (od.get("expr") or {}).get("kind") == "lambda":
                raise LeanGenError(f"a local operator `{od['name']}` in an action{where}")
            env2 = dict(env)
            env2[od["id"]] = self.ex(od["expr"], env, "s", where)
            return self.act(n["expr"], env2, where, out)
        if k == "app" and n["opcode"] == "actionAll":
            for x in n["args"]:
                self.act(x, env, where, out)
            return
        if k == "app" and n["opcode"] == "assign":
            target = self.ref(n["args"][0])
            if not target or target.get("kind") != "var":
                raise LeanGenError(f"assignment to a non-variable{where}")
            var = target["name"]
            if var in out["assigns"] or var in out["kept"]:
                raise LeanGenError(f"{var} assigned twice{where}")
            rhs = n["args"][1]
            r = self.ref(rhs) if rhs.get("kind") == "name" else None
            if r is target:
                out["kept"].add(var)
            else:
                out["assigns"][var] = self.ex(rhs, env, "s", where)
            return
        if k == "app" and n["opcode"] in ("actionAny", "ite") and self._is_act(n):
            raise LeanGenError(f"an action-level `{'any' if n['opcode'] == 'actionAny' else 'if'}`"
                               f"{where}: split it into separate actions")
        if k in ("app", "name"):
            r = self.ref(n)
            if r is not None and self.is_action(r):
                body = r.get("expr") or {}
                env2 = dict(env)
                if body.get("kind") == "lambda":
                    args = n.get("args") or []
                    for p, x in zip(body["params"], args):
                        env2[p["id"]] = self.ex(x, env, "s", where)
                    body = body["expr"]
                return self.act(body, env2, f" (in {r['name']}, called{where})", out)
        out["guards"].append(self.ex(n, env, "s", where))

    def _is_act(self, n):
        found = [False]

        def walk(x):
            if isinstance(x, dict):
                if x.get("kind") == "app" and x.get("opcode") == "assign":
                    found[0] = True
                if x.get("kind") in ("app", "name") and "id" in x:
                    r = self.ref(x)
                    if r is not None and self.is_action(r):
                        found[0] = True
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(n)
        return found[0]

    def actions(self):
        return sorted((d for d in self._decls(self.main)
                       if self.is_action(d) and d["name"] not in (self.init_name,
                                                                   self.step_name)),
                      key=lambda d: d["name"])

    def check_step(self, ctor_names):
        step = next((d for d in self._decls(self.main)
                     if self.is_action(d) and d["name"] == self.step_name), None)
        if step is None:
            raise LeanGenError(f"no `{self.step_name}` action")

        def walk(n):
            k = n.get("kind")
            if k == "let":
                return walk(n["expr"])
            if k == "app" and n["opcode"] == "actionAny":
                for x in n["args"]:
                    walk(x)
                return
            if k in ("app", "name"):
                r = self.ref(n)
                if r is not None and self.is_action(r) and r["name"] in ctor_names \
                        and self.module_of.get(r["id"]) == self.main:
                    return
            raise LeanGenError(
                f"`{self.step_name}` does more than choose among the module's actions "
                f"(id {n.get('id')}): the Lean model's steps are `apply a` for every "
                f"action a, so a step with its own effect has no Lean meaning")
        walk(step["expr"])

    # -- the module

    def generate(self, namespace, header):
        self.collect_types()
        for v in self.vars:
            if v["name"] in LEAN_KEYWORDS or v["name"] in RESERVED \
                    or not re.fullmatch(r"[A-Za-z_]\w*", v["name"]):
                raise LeanGenError(f"var `{v['name']}` cannot be a Lean field name")
        names = [v["name"] for v in self.vars]
        dup = {x for x in names if names.count(x) > 1}
        if dup:
            raise LeanGenError(f"var {', '.join(sorted(dup))} is declared by two modules; "
                               f"traces name it unqualified")
        for d in self._decls(self.main):
            if d.get("kind") == "def" and d["name"] in ("State", "Action", "apply"):
                raise LeanGenError(f"`{d['name']}` is a name the Lean model defines itself")

        types = self.emit_types()

        # defs: translate each, then keep what is needed (and what translates)
        defs = {}
        failed = {}
        for d in self._decls():
            if d.get("kind") != "def" or d.get("qualifier") in ("action", "run", "temporal",
                                                                 "nondet"):
                continue
            if d["id"] in self._ctor:
                continue
            try:
                defs[d["id"]] = self._emit_def(d)
            except LeanGenError as e:
                failed[d["id"]] = str(e)

        ctors, branches = [], []
        for d in self.actions():
            where = f" (action {d['name']})"
            body = d.get("expr") or {}
            params = []
            env = {}
            if body.get("kind") == "lambda":
                for p in body["params"]:
                    pn = self._local(p["name"], env, "s")
                    env[p["id"]] = pn
                    pt = self.types.get(str(p["id"]))
                    if not pt:
                        raise LeanGenError(f"no type for parameter {p['name']}{where}")
                    params.append((pn, self.ty(pt["type"], where)))
                body = body["expr"]
            out = {"guards": [], "assigns": {}, "kept": set()}
            self.act(body, env, where, out)
            ctors.append(f"  | {esc(d['name'])}" + "".join(f" ({p} : {t})" for p, t in params))
            pat = f".{esc(d['name'])}" + "".join(f" {p}" for p, _ in params)
            if out["assigns"]:
                upd = "\n".join(f"        {esc(v)} := {e}"
                                for v, e in sorted(out["assigns"].items()))
                res = "some { s with\n" + upd + " }"
            else:
                res = "some s"
            if out["guards"]:
                cond = " &&\n      ".join(out["guards"])
                branches.append(f"  | {pat}, s =>\n    if {cond} then\n      {res}\n    else none")
            else:
                branches.append(f"  | {pat}, s =>\n    {res}")
        if not ctors:
            raise LeanGenError("the module has no actions besides init and step")
        self.check_step({d["name"] for d in self.actions()})

        init = next((d for d in self._decls(self.main)
                     if self.is_action(d) and d["name"] == self.init_name), None)
        if init is None:
            raise LeanGenError(f"no `{self.init_name}` action")
        iout = {"guards": [], "assigns": {}, "kept": set()}
        self._init_mode = True
        try:
            self.act(init["expr"], {}, " (init)", iout)
        finally:
            self._init_mode = False
        if iout["guards"] or iout["kept"]:
            raise LeanGenError("init has a guard or reads the state it sets: the Lean "
                               "model needs one initial state")
        missing = [v for v in names if v not in iout["assigns"]]
        if missing:
            raise LeanGenError(f"init does not set {', '.join(missing)}")
        for v, e in iout["assigns"].items():
            if re.search(r"\bs\.", e):
                raise LeanGenError(f"init's value for {v} reads the state")

        # which defs are needed: referenced by actions, init or the step's
        # callees; any invariant that translates is kept as well
        needed = self._needed([d for d in self.actions()] + [init])
        bad = [i for i in needed if i in failed]
        if bad:
            raise LeanGenError(failed[bad[0]])
        order = self._topo(defs)
        pure = [defs[i] for i in order if not self.reads_state(self.decls[i])]
        stateful = [defs[i] for i in order if self.reads_state(self.decls[i])]
        skipped = sorted(f"-- not translated: {self.lname(self.decls[i])}: {why}"
                         for i, why in failed.items())

        fields = "\n".join(f"  {esc(v['name'])} : {self.ty(v['typeAnnotation'], ' (var ' + v['name'] + ')')}"
                           for v in self.vars)
        parts = ["import Quint", "", header, "", f"namespace {namespace}", "open Quint",
                 "-- an action's parameter its guard and effect never read is still its own",
                 "set_option linter.unusedVariables false", ""]
        parts += [t + "\n" for t in types]
        parts += [p + "\n" for p in pure]
        parts += ["structure State where", fields, "  deriving DecidableEq, Repr", ""]
        parts += [p + "\n" for p in stateful]
        parts += ["inductive Action where", *ctors, ""]
        parts += ["def init : State :=",
                  "  { " + ",\n    ".join(f"{esc(v)} := {e}"
                                          for v, e in sorted(iout["assigns"].items()))
                  + " }", ""]
        parts += ["/-- One step. `none` = the action's guard is false. -/",
                  "def apply : Action → State → Option State", *branches, ""]
        if skipped:
            parts += skipped + [""]
        parts += [f"end {namespace}", ""]
        return "\n".join(parts)

    def _emit_def(self, d):
        where = f" ({d['name']})"
        if d["name"] in RESERVED:
            raise LeanGenError(f"`{d['name']}` is a name the Lean model defines itself")
        e = d.get("expr") or {}
        st = "s" if self.reads_state(d) else None
        env, params = {}, []
        if st:
            params.append("(s : State)")
        if e.get("kind") == "lambda":
            for p in e["params"]:
                pn = self._local(p["name"], env, st)
                env[p["id"]] = pn
                pt = self.types.get(str(p["id"]))
                if not pt:
                    raise LeanGenError(f"no type for parameter {p['name']}{where}")
                params.append(f"({pn} : {self.ty(pt['type'], where)})")
            e = e["expr"]
        res = self.type_of(e, where)
        body = self.ex(e, env, st, where)
        return f"def {self.lname(d)}{''.join(' ' + p for p in params)} : {res} :=\n  {body}"

    def _refs(self, n, acc):
        if isinstance(n, dict):
            if n.get("kind") in ("name", "app") and "id" in n:
                r = self.ref(n)
                if r is not None and r.get("id") in self.decls:
                    acc.add(r["id"])
            for v in n.values():
                self._refs(v, acc)
        elif isinstance(n, list):
            for v in n:
                self._refs(v, acc)
        return acc

    def _needed(self, roots):
        seen, todo = set(), []
        for r in roots:
            todo.extend(self._refs(r.get("expr"), set()))
        while todo:
            i = todo.pop()
            if i in seen:
                continue
            seen.add(i)
            todo.extend(self._refs(self.decls[i].get("expr"), set()))
        return [i for i in seen if self.decls[i].get("kind") == "def"
                and not self.is_action(self.decls[i]) and i not in self._ctor]

    def _topo(self, defs):
        order, seen = [], set()

        def visit(i):
            if i in seen:
                return
            seen.add(i)
            for j in sorted(self._refs(self.decls[i].get("expr"), set()),
                            key=lambda j: (self.module_of.get(j, ""),
                                           self.decls[j].get("name") or "")):
                if j in defs:
                    visit(j)
            order.append(i)
        for i in sorted(defs, key=lambda i: self.lname(self.decls[i])):
            visit(i)
        # a def whose dependency did not translate is dropped with it
        ok = []
        for i in order:
            deps = [j for j in self._refs(self.decls[i].get("expr"), set())
                    if j in self.decls and self.decls[j].get("kind") == "def"
                    and not self.is_action(self.decls[j]) and j not in self._ctor
                    and self.decls[j].get("qualifier") not in ("run", "temporal")]
            if all(j in ok for j in deps):
                ok.append(i)
        return ok


def main_module(qnt_path):
    m = MODULE_RE.search(Path(qnt_path).read_text(encoding="utf-8"))
    if not m:
        raise LeanGenError(f"{Path(qnt_path).name} declares no module")
    return m.group(1)


def generate(qnt_path, namespace, init="init", step="step", ir=None, quint="quint"):
    """The generated Lean model's text."""
    qnt_path = Path(qnt_path)
    ir = ir if ir is not None else typecheck(qnt_path, quint)
    sha = source_sha(qnt_path)
    header = ("/-!\n"
              f"GENERATED by tools/lean_gen.py from {qnt_path.name} — do not edit.\n"
              "spec-record regenerates it whenever the Quint model changes; a proof\n"
              "lives in the module that imports it.\n\n"
              f"source_sha: {sha}\n"
              "-/")
    return Gen(ir, main_module(qnt_path), init, step).generate(namespace, header)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("qnt")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--out", help="write here (default: stdout)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when --out is not what generation produces")
    ap.add_argument("--init", default="init")
    ap.add_argument("--step", default="step")
    args = ap.parse_args(argv)
    try:
        text = generate(args.qnt, args.namespace, args.init, args.step)
    except LeanGenError as e:
        print(f"lean_gen: {e}", file=sys.stderr)
        return 2
    if args.check:
        cur = Path(args.out).read_text(encoding="utf-8") if args.out and Path(args.out).exists() else None
        if cur != text:
            print(f"lean_gen: {args.out} is stale; regenerate it", file=sys.stderr)
            return 1
        return 0
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
