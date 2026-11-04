#!/usr/bin/env python3
"""
lean_backend.py — the optional Lean backend: `proof: "lean"` invariants.

An invariant Apalache cannot settle — not inductive as written (a
counterexample to induction), or true only over a domain it cannot bound —
can be proven in Lean instead. Quint stays the model; Apalache still finds
the witnesses, counterexamples and liveness answers. Lean proves; Apalache
witnesses; never both as the verdict on one claim.

The Lean model lives in a Lake project (`lean.project_dir`, default
`specs/lean`) and follows one convention, so a tool can write what each proof
must prove instead of trusting the author's statement:

    namespace <Module>             -- = the Lean module, e.g. Spec.Subscription
      structure State              -- one field per Quint `var`, same names
      inductive Action             -- one constructor per action, its params in order
      def init  : State
      def apply : Action → State → Option State     -- none = guard false
      def <quint_name> : State → Bool               -- the invariant, same name

The invariant record names its proof, `@lean <Module>.<theorem>`, and this
backend checks it in three steps — after regenerating the model, when the
proof module imports `<Module>.Model`: tools/lean_gen.py writes that file
from the Quint model's typed IR, so the only Lean a person writes is the
proof:

  1. BUILD  `lake build <Module>`. A proof that does not compile is no proof.
  2. AUDIT  a generated file restates the theorem as

               ∀ s, Quint.Reachable <M>.init <M>.apply s → <M>.<quint_name> s = true

            and accepts the author's proof only if it proves exactly that (the
            kernel's defeq check, so a weaker statement fails to compile). It
            then reports the axioms the proof rests on — anything outside
            {propext, Classical.choice, Quot.sound} is refused: `sorryAx` is a
            `sorry`, `Lean.ofReduceBool` a `native_decide` — and the statement's
            MEANING closure: every definition of the project the statement
            reaches, elaborated. `statement_sha` hashes that closure, so a
            reformatted file keeps its verdict and a changed definition of
            init, apply or the invariant moves it.
  3. REPLAY the area's ITF traces run through the Lean model, so the proof is
            about the Quint model and not about a Lean file that drifted from
            it. `State` holds every variable the model's states hold — its
            own and those of the modules it imports, which traces name
            unqualified. A trace that records its actions (`_lastAction` and the
            parameter ghosts the probe module writes) must be a run of
            `apply` from `init`, state for state. A counterexample or
            counterexample-to-induction trace of the invariant itself must
            agree on the predicate: true on its first state, false on its
            last. With no trace replayed at all, nothing links the two
            models, and the verdict is not-checkable.

The audit and replay files are generated here, never read from the repo: the
code that reports axioms and hashes is the tool's, not the proof author's.
`lean4checker` (CI) re-checks the built environment against the kernel, which
is what catches a proof that tampered with the elaborator.

    python3 tools/lean_backend.py audit [--root .] [--area A]

recomputes every Lean verdict without writing anything and fails when one no
longer matches its record — the CI gate, since spec-lint is pure Python and
cannot rebuild a Lean proof.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from itf_tools import ghost_for_param  # noqa: E402
from proc import run_group  # noqa: E402

ALLOWED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")
DEFAULT_PROJECT_DIR = "specs/lean"
DEFAULT_TIMEOUT = 600
# spec-runs.py marks a call the model must refuse with this ghost: the state
# is a copy of the one before, and the action's guard must be false there.
REFUSED_VAR = "_refused"

# Lean that a proof file must not contain. Each one either is not a proof
# (sorry, admit), trusts something other than the kernel (native_decide,
# implemented_by, extern, unsafe, skipKernelTC), or adds an assumption
# (axiom). The axiom audit catches the first three by their axioms; this is
# the cheap Python-only gate spec-lint runs without a Lean toolchain.
FORBIDDEN = {
    "sorry": r"\bsorry\b",
    "admit": r"\badmit\b",
    "native_decide": r"\bnative_decide\b",
    "axiom": r"^\s*(?:private\s+|protected\s+)?axiom\b",
    "implemented_by": r"\bimplemented_by\b",
    "extern": r"@\[\s*extern\b",
    "unsafe": r"\bunsafe\b",
    "debug.skipKernelTC": r"\bdebug\.skipKernelTC\b",
}


class LeanSetupError(Exception):
    pass


# ── Configuration and names ──────────────────────────────────────────────────

def config(project, root):
    cfg = project.get("lean") or {}
    pdir = Path(root) / cfg.get("project_dir", DEFAULT_PROJECT_DIR)
    return {"project_dir": pdir,
            "timeout": int(cfg.get("timeout_seconds", DEFAULT_TIMEOUT)),
            "lake": cfg.get("lake")}


def find_lake(cfg):
    """LAKE_BIN, then lean.lake, then PATH, then elan's own resolution."""
    exe = os.environ.get("LAKE_BIN") or cfg.get("lake") or shutil.which("lake")
    if not exe and shutil.which("elan"):
        try:
            out = subprocess.run(["elan", "which", "lake"], capture_output=True,
                                 text=True, cwd=cfg["project_dir"], timeout=60)
            exe = out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            exe = None
    if not exe:
        raise LeanSetupError(
            "`lake` not found. Install elan (https://github.com/leanprover/elan), "
            "or set LAKE_BIN / lean.lake in .spec/project.json. The Lean backend "
            "is optional — only invariants marked proof: 'lean' need it.")
    if not shutil.which(exe) and not Path(exe).exists():
        raise LeanSetupError(f"lake not found at {exe}.")
    return exe


def module_of(theorem):
    """Spec.Subscription.inv_holds -> Spec.Subscription. The model's
    namespace IS its module: one name says where the proof is and where
    init, apply and the invariant are."""
    parts = (theorem or "").split(".")
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z_][\w']*", p) for p in parts):
        return None
    return ".".join(parts[:-1])


def module_file(project_dir, module):
    return Path(project_dir) / (module.replace(".", "/") + ".lean")


def generated_model_of(proof_file, module):
    """The proof module imports `<module>.Model`: its model is generated
    from the Quint one by tools/lean_gen.py, not written by hand."""
    try:
        text = Path(proof_file).read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(rf"^\s*import\s+{re.escape(module)}\.Model\s*$", text, re.M) is not None


def toolchain(project_dir):
    p = Path(project_dir) / "lean-toolchain"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def statement(module, quint_name):
    return (f"∀ s, Quint.Reachable {module}.init {module}.apply s → "
            f"{module}.{quint_name} s = true")


def forbidden_in(text):
    """Forbidden constructs in a Lean file, comments and strings removed."""
    code = _strip_lean_comments(text)
    found = []
    for name, pat in FORBIDDEN.items():
        if re.search(pat, code, re.M):
            found.append(name)
    return found


def _strip_lean_comments(text):
    out, i, depth, n = [], 0, 0, len(text)
    while i < n:
        if text.startswith("/-", i):
            depth += 1
            i += 2
            continue
        if depth and text.startswith("-/", i):
            depth -= 1
            i += 2
            continue
        if depth:
            i += 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append('""')
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


# ── Running Lean ─────────────────────────────────────────────────────────────

def _run(cmd, cwd, timeout):
    started = datetime.now(timezone.utc)
    try:
        proc = run_group(cmd, timeout=timeout, cwd=str(cwd))
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s", timeout
    dur = (datetime.now(timezone.utc) - started).total_seconds()
    return proc, "", dur


def _tail(proc, n=8):
    out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    errs = [ln for ln in out if "error" in ln]
    return "\n".join((errs or out)[-n:])


def build(lake, project_dir, module, timeout):
    proc, why, dur = _run([lake, "build", module], project_dir, timeout)
    if proc is None:
        return "timeout", why, dur
    if proc.returncode != 0:
        return "error", _tail(proc), dur
    return "ok", "", dur


def run_file(lake, project_dir, path, timeout):
    proc, why, dur = _run([lake, "env", "lean", str(path)], project_dir, timeout)
    if proc is None:
        return None, why, dur
    return proc, "", dur


# ── Audit ────────────────────────────────────────────────────────────────────

# The closure walk and the report. Inlined into every audit file rather than
# kept in the Lake project, so the code that reports a proof's axioms is never
# code the proof's author can edit.
_AUDIT_META = r'''
open Lean Elab Command in
partial def QuintAudit.closure (env : Environment) (roots : List Name)
    (ours : Name → Bool) : Array (String × String) := Id.run do
  let mut seen : NameSet := {}
  let mut todo := roots
  let mut out := #[]
  while true do
    match todo with
    | [] => break
    | n :: rest =>
      todo := rest
      if seen.contains n then continue
      seen := seen.insert n
      let some ci := env.find? n | continue
      let mod := (env.getModuleIdxFor? n).bind (fun i => env.header.moduleNames[i.toNat]?)
      if !(mod.map ours |>.getD true) then continue
      let body := match ci with
        | .defnInfo d => s!"def {d.type} := {d.value}"
        | .thmInfo t => s!"thm {t.type}"
        | .inductInfo i => s!"ind {i.type} ctors {i.ctors}"
        | .ctorInfo c => s!"ctor {c.type}"
        | .opaqueInfo o => s!"opaque {o.type} := {o.value}"
        | .axiomInfo a => s!"axiom {a.type}"
        | .recInfo r => s!"rec {r.type}"
        | .quotInfo q => s!"quot {q.type}"
      out := out.push (toString n, body)
      let used := match ci with
        | .thmInfo t => t.type.getUsedConstants
        | _ => ci.type.getUsedConstants ++ (ci.value?.map (·.getUsedConstants) |>.getD #[])
      let extra := match ci with
        | .inductInfo i => i.ctors.toArray
        | _ => #[]
      todo := (used ++ extra).toList ++ todo
  return out

open Lean Elab Command in
run_cmd do
  let env ← getEnv
  let axs ← liftCoreM <| collectAxioms ``QuintAudit.target
  let roots : List Name := [__ROOTS__]
  let ours : Name → Bool := fun m => roots.any (fun r => m.getRoot == r.getRoot) || m.getRoot == `Quint
  let some ci := env.find? ``QuintAudit.target | throwError "QuintAudit.target missing"
  let cl := QuintAudit.closure env ci.type.getUsedConstants.toList ours
  let j := Json.mkObj [
    ("axioms", toJson (axs.map toString)),
    ("statement", toJson (toString ci.type)),
    ("closure", toJson (cl.map fun (n, b) => Json.mkObj [("name", toJson n), ("body", toJson b)]))]
  logInfo m!"QUINT-AUDIT {j.compress}"
'''


def audit_source(module, quint_name, theorem):
    roots = f"`{module.split('.')[0]}"
    return (f"import {module}\nimport Lean\n\n"
            f"theorem QuintAudit.target :\n    {statement(module, quint_name)} :=\n"
            f"  {theorem}\n" + _AUDIT_META.replace("__ROOTS__", roots))


def parse_audit(out):
    for line in (out or "").splitlines():
        i = line.find("QUINT-AUDIT ")
        if i >= 0:
            try:
                return json.loads(line[i + len("QUINT-AUDIT "):])
            except ValueError:
                return None
    return None


def statement_sha(report, chain):
    """What the verdict means: the elaborated statement and the closure of
    every project definition it reaches, under the toolchain that
    elaborated them. Layout never reaches it; a changed definition does."""
    closure = sorted((c["name"], c["body"]) for c in report.get("closure") or [])
    blob = json.dumps({"toolchain": chain, "statement": report.get("statement"),
                       "closure": closure}, sort_keys=True, ensure_ascii=False)
    return "lean:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── ITF -> Lean ──────────────────────────────────────────────────────────────

class Unencodable(Exception):
    pass


def lean_string(s):
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def lean_literal(v):
    """An ITF value as a Lean term, elaborated against the expected type of
    the field it fills — which is what lets `.Active` and `QMap.ofList`
    resolve without this knowing the model's types. A map or set is built
    by `ofList`, which puts it in canonical form, so the order the trace
    lists it in does not matter."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v) if v >= 0 else f"({v})"
    if isinstance(v, str):
        return lean_string(v)
    if isinstance(v, list):
        return "[" + ", ".join(lean_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        if "#bigint" in v:
            return lean_literal(int(v["#bigint"]))
        if "#set" in v:
            return "(Quint.QSet.ofList [" + ", ".join(lean_literal(x) for x in v["#set"]) + "])"
        if "#map" in v:
            return "(Quint.QMap.ofList [" + ", ".join(
                f"({lean_literal(k)}, {lean_literal(x)})" for k, x in v["#map"]) + "])"
        if "#tup" in v:
            items = v["#tup"]
            if not items:
                return "()"
            return "(" + ", ".join(lean_literal(x) for x in items) + ")"
        if set(v) == {"tag", "value"}:
            inner = v["value"]
            if inner in ({"#tup": []}, {}, {"tag": "UNIT"}):
                return f".{v['tag']}"
            if isinstance(inner, dict) and "#tup" in inner:
                args = " ".join(lean_literal(x) for x in inner["#tup"])
            else:
                args = lean_literal(inner)
            return f"(.{v['tag']} {args})"
        if any(k.startswith("#") for k in v):
            raise Unencodable(f"no Lean literal for {sorted(v)}")
        return "{ " + ", ".join(f"{k} := {lean_literal(x)}" for k, x in v.items()) + " }"
    raise Unencodable(repr(v))


def lean_state(st, model_vars):
    missing = [x for x in model_vars if x not in st]
    if missing:
        raise Unencodable(f"state has no {', '.join(missing)}")
    bad = [x for x in model_vars if not re.fullmatch(r"[A-Za-z_]\w*", x)]
    if bad:
        raise Unencodable(f"no Lean field name for {', '.join(bad)}")
    return "{ " + ", ".join(f"{x} := {lean_literal(st[x])}" for x in model_vars) + " }"


def lean_action(st, action_params):
    name = st.get("_lastAction")
    if name not in action_params:
        raise Unencodable(f"action '{name}' is not one of the model's")
    args = []
    for p in action_params[name]:
        g = ghost_for_param(p)
        if g not in st:
            raise Unencodable(f"step '{name}' records no {g}")
        args.append(lean_literal(st[g]))
    refused = "true" if st.get(REFUSED_VAR) is True else "false"
    return f"((.{name}{''.join(' ' + a for a in args)}), {refused})"


def collect_traces(area_name, root, lean_invs):
    """(runs, predicates): the area's traces that record their actions, and
    the counterexample / CTI traces of each Lean invariant (by id)."""
    tdir = Path(root) / "specs" / area_name / "traces"
    runs, preds = [], []
    if not tdir.exists():
        return runs, preds
    own = {}
    for inv in lean_invs:
        for suffix in ("cex", "cti"):
            own[f"{inv['id']}.{suffix}.itf.json"] = inv
    for p in sorted(tdir.rglob("*.itf.json")):
        rel = p.relative_to(Path(root) / "specs").as_posix()
        try:
            trace = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        states = trace.get("states") or []
        if not states:
            continue
        if p.name in own:
            preds.append((rel, own[p.name], states))
        elif p.name.endswith(".cti.itf.json"):
            # Another invariant's counterexample to induction: its first state
            # need not be reachable, so it is no run of the model.
            continue
        elif "_lastAction" in (trace.get("vars") or states[0]):
            runs.append((rel, states))
    return runs, preds


def replay_source(module, model_vars, action_params, runs, preds):
    """The replay file, and for each check the lines it spans (to pin a Lean
    error on the trace that caused it) — or why a trace was not encoded."""
    lines = [f"import {module}", ""]
    spans, skipped = {}, []
    for k, (rel, states) in enumerate(runs):
        try:
            sts = [lean_state(st, model_vars) for st in states]
            acts = [lean_action(st, action_params) for st in states[1:]]
        except Unencodable as e:
            skipped.append((rel, str(e)))
            continue
        start = len(lines) + 1
        lines.append(f"def QuintReplay.r{k}_states : List {module}.State := [")
        lines.extend(f"  {s}," for s in sts[:-1])
        lines.append(f"  {sts[-1]}]")
        lines.append(f"def QuintReplay.r{k}_acts : List ({module}.Action × Bool) := "
                     f"[{', '.join(acts)}]")
        lines.append(f'#eval IO.println s!"QUINT-REPLAY r{k} '
                     f'{{match Quint.replay {module}.init {module}.apply '
                     f'QuintReplay.r{k}_states QuintReplay.r{k}_acts with '
                     f'| none => "ok" | some i => toString i}}"')
        spans[f"r{k}"] = (start, len(lines), rel)
    for k, (rel, inv, states) in enumerate(preds):
        try:
            first = lean_state(states[0], model_vars)
            last = lean_state(states[-1], model_vars)
        except Unencodable as e:
            skipped.append((rel, str(e)))
            continue
        start = len(lines) + 1
        q = inv["quint_name"]
        lines.append(f"def QuintReplay.p{k}_first : {module}.State := {first}")
        lines.append(f"def QuintReplay.p{k}_last : {module}.State := {last}")
        lines.append(f'#eval IO.println s!"QUINT-PRED p{k} '
                     f'{{{module}.{q} QuintReplay.p{k}_first}} '
                     f'{{{module}.{q} QuintReplay.p{k}_last}}"')
        spans[f"p{k}"] = (start, len(lines), rel)
    return "\n".join(lines) + "\n", spans, skipped


_ERR = re.compile(r":(\d+):\d+: error: (.*)")


def parse_replay(out, spans, runs, preds, path_name):
    """Every check's outcome: {rel: (ok, detail)}."""
    seen = {}
    for line in (out or "").splitlines():
        m = re.match(r"QUINT-REPLAY (r\d+) (\S+)", line)
        if m and m.group(1) in spans:
            rel = spans[m.group(1)][2]
            idx = m.group(2)
            if idx == "ok":
                seen[rel] = (True, "")
            else:
                k = int(m.group(1)[1:])
                states = runs[k][1]
                i = int(idx)
                st = states[i] if i < len(states) else {}
                act = st.get("_lastAction")
                if i == 0:
                    why = "the initial state is not init"
                elif st.get(REFUSED_VAR) is True:
                    why = f"step {i} ({act}) is refused in Quint, and Lean's apply allows it"
                else:
                    why = f"step {i} ({act}) is not a step of Lean's apply"
                seen[rel] = (False, why)
            continue
        m = re.match(r"QUINT-PRED (p\d+) (true|false) (true|false)", line)
        if m and m.group(1) in spans:
            rel = spans[m.group(1)][2]
            first, last = m.group(2) == "true", m.group(3) == "true"
            if first and not last:
                seen[rel] = (True, "")
            else:
                seen[rel] = (False, f"the Lean invariant is {str(first).lower()} on the "
                                    f"first state and {str(last).lower()} on the last; "
                                    f"the trace says true, then false")
    for line in (out or "").splitlines():
        if path_name not in line:
            continue
        m = _ERR.search(line)
        if not m:
            continue
        ln = int(m.group(1))
        for key, (a, b, rel) in spans.items():
            if a <= ln <= b and rel not in seen:
                seen[rel] = (False, f"does not elaborate: {m.group(2)[:160]}")
    for key, (_a, _b, rel) in spans.items():
        seen.setdefault(rel, (False, "produced no result"))
    return seen


# ── One area ─────────────────────────────────────────────────────────────────

def state_vars(qnt_file, seen=None):
    """{var: module} for every variable a state of this model holds: its own
    and, recursively, those of every module it imports. An imported module's
    variables appear in its traces UNQUALIFIED (`sessions`, not
    `auth::sessions`), so each becomes an ordinary field of the Lean State.
    Two modules declaring the same name would make that field ambiguous;
    that is refused rather than guessed."""
    from quint_ir import parse_qnt
    qnt_file = Path(qnt_file).resolve()
    seen = set() if seen is None else seen
    if qnt_file in seen:
        return {}
    seen.add(qnt_file)
    ir = parse_qnt(qnt_file)
    if not ir:
        raise Unencodable(f"{qnt_file.name} does not parse (an import not found?)")
    me = ir.get("module_name") or qnt_file.stem
    out = {v: me for v in ir.get("vars") or []}
    for imp in ir.get("imports") or []:
        src = imp.get("from")
        if not src:
            continue
        path = (qnt_file.parent / src)
        if path.suffix != ".qnt":
            path = path.with_name(path.name + ".qnt")
        if not path.exists():
            raise Unencodable(f"imported module {imp.get('module')} not found at {path.name}")
        for v, mod in state_vars(path, seen).items():
            if v in out and out[v] != mod:
                raise Unencodable(f"variable '{v}' is declared by both {out[v]} and {mod}; "
                                  f"a trace names it unqualified, so the Lean State cannot "
                                  f"tell the two apart")
            out[v] = mod
    return out


def model_shape(qnt_file):
    """(state variables, own and imported; action -> parameter names)."""
    from quint_ir import parse_qnt
    fields = sorted(state_vars(qnt_file))
    ir = parse_qnt(qnt_file) or {}
    # Every action, with or without parameters: the regex reader lists a
    # parameterless one in `actions` only.
    params = {a: [] for a in ir.get("actions") or []}
    params.update(ir.get("action_params") or {})
    return fields, params


class AreaRun:
    """The Lean checks of one area. Build, replay and audit results are
    computed once and shared by every Lean invariant of the area."""

    def __init__(self, area, area_name, root, project, out_dir, quint=None):
        self.area, self.area_name, self.root = area, area_name, Path(root)
        self.cfg = config(project, root)
        self.out_dir = Path(out_dir)
        self._quint = quint
        self._lake = None
        self._built = {}
        self._replay = {}
        self._generated = {}

    def lake(self):
        if self._lake is None:
            self._lake = find_lake(self.cfg)
        return self._lake

    def lean_invariants(self):
        return [i for i in self.area.get("invariants") or [] if i.get("proof") == "lean"]

    def _generate(self, module):
        """Regenerate `<module>.Model` from the area's Quint model when the
        proof module imports it, before anything is built. None when the
        model is current (or hand-written), else why it cannot be had."""
        if module in self._generated:
            return self._generated[module]
        res = None
        mfile = module_file(self.cfg["project_dir"], module)
        if generated_model_of(mfile, module):
            qnt = (self.area.get("formal_model") or {}).get("quint_file")
            if not qnt:
                res = "no formal_model.quint_file to generate the Lean model from"
            else:
                import lean_gen
                quint = self._quint() if callable(self._quint) else (self._quint or "quint")
                try:
                    text = lean_gen.generate(self.root / "specs" / qnt, module, quint=quint)
                except lean_gen.LeanGenError as e:
                    res = f"no Lean model generated from {qnt}: {e}"
                else:
                    out = module_file(self.cfg["project_dir"], module + ".Model")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists() or out.read_text(encoding="utf-8") != text:
                        out.write_text(text, encoding="utf-8")
        self._generated[module] = res
        return res

    def _build(self, module):
        if module not in self._built:
            self._built[module] = build(self.lake(), self.cfg["project_dir"], module,
                                        self.cfg["timeout"])
        return self._built[module]

    def _replay_for(self, module):
        if module in self._replay:
            return self._replay[module]
        qnt = (self.area.get("formal_model") or {}).get("quint_file")
        if not qnt:
            res = {"error": "no formal_model.quint_file: nothing to replay against"}
            self._replay[module] = res
            return res
        try:
            model_vars, action_params = model_shape(self.root / "specs" / qnt)
        except Unencodable as e:
            res = {"error": f"no Lean State for this model: {e}"}
            self._replay[module] = res
            return res
        invs = [i for i in self.lean_invariants()
                if module_of(i.get("lean_theorem")) == module and i.get("quint_name")]
        runs, preds = collect_traces(self.area_name, self.root, invs)
        src, spans, skipped = replay_source(module, model_vars, action_params, runs, preds)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"Replay_{module.replace('.', '_')}.lean"
        path.write_text(src, encoding="utf-8")
        results = {}
        if spans:
            proc, why, _dur = run_file(self.lake(), self.cfg["project_dir"], path.resolve(),
                                       self.cfg["timeout"])
            if proc is None:
                res = {"error": f"replay {why}"}
                self._replay[module] = res
                return res
            out = (proc.stdout or "") + (proc.stderr or "")
            results = parse_replay(out, spans, runs, preds, path.name)
        res = {"results": results, "skipped": skipped,
               "runs": sum(1 for k in spans if k.startswith("r")),
               "predicates": {spans[k][2] for k in spans if k.startswith("p")}}
        self._replay[module] = res
        return res

    def check(self, item):
        """The check_results entry for one `proof: "lean"` invariant, and the
        formal_status it earns."""
        iid, qname, thm = item.get("id"), item.get("quint_name"), item.get("lean_theorem")
        entry = {"id": iid, "kind": "invariant", "backend": "lean", "proof": "lean",
                 "result": "error"}
        if qname:
            entry["quint_name"] = qname
        if thm:
            entry["theorem"] = thm
        module = module_of(thm)
        if not module:
            entry["error"] = ("proof is 'lean' but no `@lean <Module>.<theorem>` names "
                              "the proof.")
            return entry, None
        if not qname:
            entry["error"] = ("proof is 'lean' but the invariant has no quint_name: the "
                              "Lean invariant is found by that name, and the Quint one "
                              "is what the replay checks it against.")
            return entry, None
        mfile = module_file(self.cfg["project_dir"], module)
        if not mfile.exists():
            entry["error"] = f"no Lean module {module} ({mfile.relative_to(self.root)})."
            return entry, None
        chain = toolchain(self.cfg["project_dir"])
        if chain:
            entry["toolchain"] = chain
        started = datetime.now(timezone.utc)

        def done(result, status, **fields):
            entry["result"] = result
            entry["duration_s"] = round(
                (datetime.now(timezone.utc) - started).total_seconds(), 1)
            entry.update(fields)
            return entry, status

        gerr = self._generate(module)
        if gerr:
            return done("not-checkable", "not-checkable", note=gerr)
        bres, bdetail, _ = self._build(module)
        if bres != "ok":
            return done("not-checkable", "not-checkable",
                        note=f"lake build {module}: {bres}" + (f"\n{bdetail}" if bdetail else ""))

        self.out_dir.mkdir(parents=True, exist_ok=True)
        apath = self.out_dir / f"Audit_{re.sub(r'[^A-Za-z0-9]', '_', iid)}.lean"
        apath.write_text(audit_source(module, qname, thm), encoding="utf-8")
        proc, why, _ = run_file(self.lake(), self.cfg["project_dir"], apath.resolve(),
                                self.cfg["timeout"])
        if proc is None:
            return done("timeout", None, error=f"audit {why}")
        report = parse_audit((proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode != 0 or report is None:
            return done("not-checkable", "not-checkable",
                        note=(f"{thm} does not prove `{statement(module, qname)}`:\n"
                              + _tail(proc)))
        axioms = sorted(report.get("axioms") or [])
        entry["axioms"] = axioms
        entry["statement_sha"] = statement_sha(report, chain)
        extra = [a for a in axioms if a not in ALLOWED_AXIOMS]
        if extra:
            named = {"sorryAx": "sorry", "Lean.ofReduceBool": "native_decide"}
            what = ", ".join(f"{a} ({named[a]})" if a in named else a for a in extra)
            return done("not-checkable", "not-checkable",
                        note=f"the proof rests on axioms outside the kernel's own: {what}")

        rep = self._replay_for(module)
        if rep.get("error"):
            return done("not-checkable", "not-checkable", note=rep["error"])
        results = rep["results"]
        drift = [(rel, d) for rel, (ok, d) in sorted(results.items()) if not ok]
        own_preds = sorted(rel for rel in rep["predicates"]
                           if Path(rel).name.startswith(f"{iid}."))
        entry["replay"] = {"runs": rep["runs"], "predicates": len(own_preds),
                           "skipped": len(rep["skipped"])}
        if drift:
            rel, d = drift[0]
            more = f" (and {len(drift) - 1} more)" if len(drift) > 1 else ""
            return done("not-checkable", "not-checkable",
                        note=f"drift: the Lean model disagrees with {rel}: {d}{more}")
        if rep["runs"] == 0:
            why = "; ".join(f"{r}: {w}" for r, w in rep["skipped"][:3])
            return done("not-checkable", "not-checkable",
                        note=("no trace replayed through the Lean model, so nothing "
                              "links it to the Quint one" + (f" ({why})" if why else "")))
        return done("verified", "verified-lean")


# ── CLI: the CI gate ─────────────────────────────────────────────────────────

def _audit_cli(args):
    import spec_source
    root = Path(args.root).resolve()
    project = json.loads((root / ".spec" / "project.json").read_text(encoding="utf-8"))
    areas = [a["name"] for a in project.get("areas") or [] if a.get("name")]
    if args.area:
        areas = [args.area]
    bad = 0
    gen = Path(args.out) if args.out else None
    for name in areas:
        try:
            area = spec_source.load_area(root, name)
        except Exception as e:  # noqa: BLE001 — an unreadable area is lint's finding
            print(f"{name}: unreadable ({e})", file=sys.stderr)
            continue
        invs = [i for i in area.get("invariants") or [] if i.get("proof") == "lean"]
        if not invs:
            continue
        recorded = {c.get("id"): c for c in
                    ((area.get("check_results") or {}).get("checks") or [])}
        run = AreaRun(area, name, root, project,
                      gen or root / "specs" / name / "gen" / "lean")
        # A generated model is regenerated by run.check; what was committed
        # must already be that text, or the repo shows a model no proof is
        # about.
        committed = {}
        for inv in invs:
            module = module_of(inv.get("lean_theorem"))
            if module and module not in committed and generated_model_of(
                    module_file(run.cfg["project_dir"], module), module):
                mpath = module_file(run.cfg["project_dir"], module + ".Model")
                committed[module] = (mpath, mpath.read_text(encoding="utf-8")
                                     if mpath.exists() else None)
        for inv in invs:
            try:
                entry, status = run.check(inv)
            except LeanSetupError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
            have = inv.get("formal_status")
            rec = recorded.get(inv["id"]) or {}
            problems = []
            if status != have:
                problems.append(f"recorded {have}, now {status}")
            if status == "verified-lean" and rec.get("statement_sha") != entry.get("statement_sha"):
                problems.append("statement_sha moved: the definitions the proof is about "
                                "changed since it was recorded — re-run spec-record check")
            line = f"{name}:{inv['id']:<10} {status or entry['result']}"
            if entry.get("note") or entry.get("error"):
                line += f"  {(entry.get('note') or entry.get('error')).splitlines()[0]}"
            print(line)
            for p in problems:
                print(f"  FAIL {p}")
            bad += bool(problems)
        for module, (mpath, before) in committed.items():
            if mpath.exists() and mpath.read_text(encoding="utf-8") != before:
                print(f"{name}: FAIL {mpath.relative_to(root)} was not what "
                      f"tools/lean_gen.py generates from the Quint model — re-run "
                      f"spec-record check and commit the regenerated file")
                bad += 1
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="recompute every Lean verdict; fail if one moved")
    a.add_argument("--root", default=".")
    a.add_argument("--area")
    a.add_argument("--out", help="where generated Lean files go (default specs/<area>/gen/lean)")
    args = ap.parse_args(argv)
    sys.exit(_audit_cli(args))


if __name__ == "__main__":
    main()
