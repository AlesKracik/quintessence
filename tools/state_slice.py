#!/usr/bin/env python3
"""
state_slice.py — cone-of-influence slicing of model state, one check at a time.

A slice (spec-probes.slice_text) drops the probe ghosts its check never
reads. That leaves the model's own state whole: the slice imports the area
module unchanged and stepP calls its actions, so a record field that one
action writes and two requirements read is encoded, and paid for, in every
check of the area. This module cuts the state down too.

THE CONE of a check is the state its claim can depend on, as WRITE UNITS
(completeness.py): a whole var, or `var.field` of a record var.

  1. Seed. What the check's predicate reads, through the defs and vals it
     calls (the probe module's pre_/post_/old_ defs and the model's
     helpers); what every ghost the slice keeps is assigned from in stepP
     (an event requirement's @while, its @old's); what stepP's nondet
     domains read; and everything read by each action the predicate names
     (`_lastAction == "a"`: the action a requirement sits on), guard
     included.
  2. Close. Every action stepP takes that writes a unit in the cone adds
     everything it reads, guards and helpers included. Repeat to a fixpoint.
     Reads and writes are the field-level ones of quint_ir (_read_fields,
     _changed_fields); where a body cannot be resolved the whole var is
     read, or written. A var the model does not declare (an imported
     module's) is never frozen, so every action writing one is relevant.
  3. Freeze the rest. Everything outside the cone keeps its init value for
     the whole check: in a copy of the area model made for this check, each
     step action's assignment to it becomes the identity (`v' = v`; a frozen
     field of a record literal gets `st.f`; a new state the text cannot take
     apart is wrapped, `{ ...(e), f: st.f }`). Types are unchanged. Actions
     reachable from init / initP are left alone, so the initial states are
     the model's own.

WHY THE VERDICT IS THE SAME. Call an action RELEVANT when it writes the cone
or the predicate names it. A relevant action reads only the cone (step 1 for
named ones, step 2 for writers), so its enabledness and its effect on the
cone are the same whatever the frozen state holds; an irrelevant action
writes nothing in the cone, so on the cone it is a stutter. Hence a trace of
either model, with its irrelevant steps dropped, is a trace of the other
with the same cone values at every remaining state (and the same _arg_, _pre_
and _old_ ghosts there: each is assigned from the cone by the step it
belongs to).
  - The generated checks (oblig_, witness_, reached_, cover_, explain_,
    verdict_) are either state predicates over the model, the same before
    and after a stutter, or guarded by `_lastAction == "a"`, true at every
    step of an action they do not name. So no irrelevant step falsifies
    one, and a falsifying state of one model maps to a falsifying state of
    the other at a depth no greater. Bounded checks see every depth up to
    the bound, so "holds to k" and "falsified within k" agree at every k:
    universal claims (obligations, invariants) and existential ones
    (witnesses, whose counterexample is the witness) alike.
  - A hand-written over:"probes" invariant can be falsified BY a stutter
    (`x > _prevX`); for one, every action is relevant (all their reads seed
    the cone) and only state no action reads is frozen.
  - A model invariant reads only state: stutter-invariant like a state
    predicate.
  - The guard edge case: an action whose guard reads a frozen field can be
    enabled in one model and not in the other. If it writes the cone it is
    relevant, and its guard's reads are in the cone by step 2, so it is
    never frozen. If it does not, its steps are stutters that are dropped.
  - Deadlock. Apalache reports a state with no enabled step as a violation
    of whatever it checks. An irrelevant action's enabledness is NOT kept,
    so the two models may differ in where they deadlock. spec-record never
    takes a sliced counterexample on trust: each one is replayed through the
    full model (see replay below) and re-proven on the whole model when it
    does not replay, so a sliced-only deadlock cannot become a verdict. A
    deadlock of the full model that an irrelevant step papers over in the
    slice is not a verdict on the claim either (it says nothing about the
    check's formula); a model that can deadlock is better given an idle step.

Traces: a counterexample of the sliced model has the frozen state at its
init values. spec-record replays it — irrelevant steps dropped, each kept
step stepP's own branch with the recorded arguments — through the full
model with `quint test --out-itf`, asserting the cone at every state and the
check's formula at the end, and writes the replayed trace: a trace of the
real model, which is what conformance replays against the code.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_source  # noqa: E402
import quint_ir as qi  # noqa: E402

# A name read as a value: not a field or method (`.x`), not primed (`x'`).
_READ = re.compile(r"(?<![\w.])([A-Za-z_]\w*)(?![\w'])")
_PRIMED = re.compile(r"(?<![\w.])([A-Za-z_]\w*)'\s*=(?![=>])")
_IDENTITY = re.compile(r"(?<![\w.])([A-Za-z_]\w*)'\s*=\s*\1(?=\s*(?:[,)}\]]|$))")
GUARDED = re.compile(r"^(oblig|witness|reached|cover|explain|verdict)_")
IDLE = "_idle"


def _code(text):
    """Strings blanked, `...` spreads opened up: `...st` copies st."""
    return spec_source._code_only(text or "").replace("...", "   ")


def _mask(text):
    """Length-preserving: string interiors and // comments blanked, so
    brackets and commas found here are code."""
    out, i, n, in_str = list(text), 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
                continue
            if c == '"':
                in_str = False
            elif c != "\n":
                out[i] = " "
        elif c == '"':
            in_str = True
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _strip_comments(text):
    """`text` with its // comments blanked, strings kept."""
    mask = _mask(text)
    out = list(text)
    i = 0
    while i < len(text):
        if text.startswith("//", i) and mask[i] == " ":
            while i < len(text) and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


class ConeModel:
    """The area model's per-action reads and writes, computed once and shared
    by every check of the area."""

    def __init__(self, model_text, ir, extra_text=None, model_dir=None):
        self.text = model_text
        self.decls = spec_source.declarations(model_text)
        extra = spec_source.declarations(extra_text) if extra_text else {}
        # The model's own declarations win: a probe module only adds.
        self.all_decls = {**extra, **self.decls}
        self.vars = [n for n, h in self.decls.items() if h["kind"] == "var"]
        fields = qi.record_var_fields(ir or {})
        self.records = {v: list(fs) for v, fs in fields.items() if v in self.vars}
        self.plain = [v for v in self.vars if v not in self.records]
        self.actions = {n for n, h in self.decls.items() if h["kind"] == "action"}
        self._reads, self._writes = {}, {}
        self.foreign = self._foreign_names(model_text, model_dir)

    @staticmethod
    def _foreign_names(text, model_dir):
        """Names through which an action reaches another module's state:
        `M::x` for a qualified import (any `::` will do), and the actions
        and vars of each `import M.*` module, read off its file. None when
        an unqualified import cannot be read: then every action is taken
        to touch foreign state."""
        names = set()
        for m in re.finditer(r'(?m)^\s*import\s+([\w:]+)\.\*\s+from\s+"([^"]+)"', text):
            path = Path(model_dir or ".") / (m.group(2) + ".qnt")
            try:
                other = spec_source.declarations(path.read_text(encoding="utf-8"))
            except OSError:
                return None
            names |= {n for n, h in other.items() if h["kind"] in ("action", "var")}
        return names

    # ── units ─────────────────────────────────────────────────────────────
    def whole(self, var):
        if var in self.records:
            return {f"{var}.{f}" for f in self.records[var]}
        return {var}

    def universe(self):
        out = set()
        for v in self.vars:
            out |= self.whole(v)
        return out

    # ── reads ─────────────────────────────────────────────────────────────
    def reads(self, texts, stop=()):
        """Units read by `texts`, through every def / val / action they name
        (except the names in `stop`)."""
        decls = self.all_decls
        bodies = [t for t in texts if t]
        seen, frontier = set(), []
        for t in bodies:
            frontier += _READ.findall(_code(t))
        while frontier:
            name = frontier.pop()
            if name in seen:
                continue
            seen.add(name)
            h = decls.get(name)
            if (not h or name in stop or not h.get("body")
                    or h["kind"] not in ("def", "val", "action")):
                continue
            bodies.append(h["body"])
            params = {p for p, _t in h.get("params") or []}
            frontier += [t for t in _READ.findall(_code(h["body"])) if t not in params]
        out = set()
        for b in bodies:
            code = _IDENTITY.sub(" ", _code(b))
            names = set(_READ.findall(code))
            out |= {v for v in self.plain if v in names}
            for v, fs in self.records.items():
                if v not in names:
                    continue
                got = qi._read_fields(b, v, fs, decls)
                out |= (self.whole(v) if got is None
                        else {f"{v}.{f}" for f in got})
        return out

    def action_reads(self, action):
        if action not in self._reads:
            h = self.decls.get(action) or {}
            self._reads[action] = self.reads([h.get("body") or ""])
        return self._reads[action]

    # ── writes ────────────────────────────────────────────────────────────
    def _action_closure(self, action):
        """`action` and every action its body calls, transitively."""
        seen, frontier = [], [action]
        while frontier:
            a = frontier.pop()
            if a in seen or a not in self.actions:
                continue
            seen.append(a)
            frontier += _READ.findall(_code(self.decls[a].get("body") or ""))
        return seen

    def action_writes(self, action):
        """Units `action` changes, through the actions it calls. A var the
        model does not declare comes back by name: never frozen."""
        if action in self._writes:
            return self._writes[action]
        out = set()
        for a in self._action_closure(action):
            body = self.decls[a].get("body") or ""
            code = _code(body)
            if (self.foreign is None or "::" in code
                    or set(_READ.findall(code)) & self.foreign):
                out.add("<foreign>")   # another module's state: never frozen
            b = body.strip()
            locals_ = dict(qi._local_vals(b[1:-1] if b.startswith("{") else b)[0])
            for v in dict.fromkeys(_PRIMED.findall(_code(body))):
                rhs = qi._assigned_rhs(body, v)
                if v in self.records:
                    changed = set()
                    for r in rhs:
                        c = qi._changed_fields(r, v, self.records[v], self.decls,
                                               locals_=locals_)
                        if c is None:
                            changed = set(self.records[v])
                            break
                        changed |= c
                    out |= {f"{v}.{f}" for f in changed}
                elif v in self.plain:
                    if any(qi._norm_expr(r) != v for r in rhs):
                        out.add(v)
                else:
                    out.add(v)
        self._writes[action] = out
        return out

    def init_actions(self, roots):
        """Actions reachable from `roots` (init, initP): never edited."""
        out = set()
        decls = self.all_decls
        seen, frontier = set(), list(roots)
        while frontier:
            n = frontier.pop()
            if n in seen:
                continue
            seen.add(n)
            h = decls.get(n)
            if not h or not h.get("body"):
                continue
            if n in self.actions:
                out.add(n)
            frontier += _READ.findall(_code(h["body"]))
        return out

    # ── the cone ──────────────────────────────────────────────────────────
    def cone(self, seed, step_actions, named=(), all_relevant=False):
        """(cone units, relevant actions)."""
        universe = self.universe()
        cone = set(seed)
        relevant = set(step_actions if all_relevant else
                       [a for a in named if a in step_actions])
        for a in relevant:
            cone |= self.action_reads(a)
        changed = True
        while changed:
            changed = False
            for a in step_actions:
                if a in relevant:
                    continue
                w = self.action_writes(a)
                if (w & cone) or (w - universe):
                    relevant.add(a)
                    cone |= self.action_reads(a)
                    changed = True
        return cone & universe, relevant

    # ── freezing ──────────────────────────────────────────────────────────
    def frozen_text(self, frozen, keep_actions=()):
        """The model text with every assignment to a frozen unit made the
        identity, in every action outside `keep_actions`. Returns (text,
        number of assignments edited)."""
        frozen = set(frozen)
        plain = {v for v in self.plain if v in frozen}
        fields = {v: {f for f in fs if f"{v}.{f}" in frozen}
                  for v, fs in self.records.items()}
        fields = {v: fs for v, fs in fields.items() if fs}
        if not plain and not fields:
            return self.text, 0
        lines = self.text.split("\n")
        starts = [0]
        for ln in lines:
            starts.append(starts[-1] + len(ln) + 1)
        edits = []
        for name in sorted(self.actions - set(keep_actions)):
            h = self.decls[name]
            first = h["line"] - 1
            n = len((h.get("source") or "").split("\n"))
            lo, hi = starts[first], starts[first] + len("\n".join(lines[first:first + n]))
            seg = self.text[lo:hi]
            mask = _mask(seg)
            body = h.get("body") or ""
            b = body.strip()
            locals_ = dict(qi._local_vals(b[1:-1] if b.startswith("{") else b)[0])
            for m in _PRIMED.finditer(mask):
                v = m.group(1)
                if v not in plain and v not in fields:
                    continue
                s = m.end()
                e, depth = s, 0
                while e < len(mask):
                    c = mask[e]
                    if c in "([{":
                        depth += 1
                    elif c in ")]}":
                        depth -= 1
                        if depth < 0:
                            break
                    elif c == "," and depth == 0:
                        break
                    e += 1
                rhs = _strip_comments(seg[s:e]).replace("\n", " ").strip()
                if not rhs:
                    continue
                if v in plain:
                    new = v
                else:
                    new = _freeze(rhs, v, fields[v], self.records[v], self.decls,
                                  locals_)
                if qi._norm_expr(new) != qi._norm_expr(rhs):
                    raw = seg[s:e]
                    lead = raw[:len(raw) - len(raw.lstrip())]
                    trail = raw[len(raw.rstrip()):]
                    edits.append((lo + s, lo + e, lead + new + trail))
        out = self.text
        for s, e, new in sorted(edits, reverse=True):
            out = out[:s] + new + out[e:]
        return out, len(edits)


def _freeze(e, var, frozen, fields, decls, locals_):
    """`e`, a new value of record var `var`, with the fields in `frozen`
    kept at `var.f`. Literals and conditionals are edited in place; anything
    else that may change a frozen field is wrapped."""
    u = qi._unparen(e)
    if qi._norm_expr(u) == var:
        return e.strip()
    m = re.match(r"if\s*\(", u)
    if m:
        close = qi._match_close(u, m.end() - 1)
        br = qi._split_if(u)
        if close >= 0 and br:
            return (f"{u[:close + 1]} "
                    f"{_freeze(br[0], var, frozen, fields, decls, locals_)} else "
                    f"{_freeze(br[1], var, frozen, fields, decls, locals_)}")
    rec = qi.split_record_literal(u)
    if rec is not None:
        base, items = rec
        listed = {f for f, _v in items}
        parts = []
        if base is not None:
            need = frozen - listed
            ch = qi._changed_fields(base, var, fields, decls, locals_=locals_)
            if need and (ch is None or ch & need):
                base = _freeze(base, var, need, fields, decls, locals_)
            parts.append(f"...{base}")
        # Over the var itself a frozen field is simply not listed:
        # `{ ...st, graph: g }` becomes `{ ...st }`, i.e. `st`.
        own = base is not None and qi._norm_expr(base) == var
        rest = [(f, val) for f, val in items if not (own and f in frozen)]
        if own and not rest:
            return var
        parts += [f"{f}: {var}.{f}" if f in frozen else f"{f}: {val}"
                  for f, val in rest]
        return "{ " + ", ".join(parts) + " }"
    ch = qi._changed_fields(u, var, fields, decls, locals_=locals_)
    if ch is not None and not (ch & frozen):
        return e.strip()
    keep = sorted(frozen if ch is None else ch & frozen)
    return ("{ ...(" + e.strip() + "), "
            + ", ".join(f"{f}: {var}.{f}" for f in keep) + " }")


# ── The probe module side ───────────────────────────────────────────────────

def _decl_body(text, name):
    return (spec_source.declarations(text).get(name) or {}).get("body") or ""


def step_branches(probe_text):
    """{action: branch text} for stepP's branches, idle included."""
    import witness_replay
    body = _decl_body(probe_text, "stepP")
    code = spec_source._code_only(body)
    m = re.search(r"\bany\s*\{", code)
    if not m:
        return {}
    i = witness_replay._close(code, m.end() - 1, "{", "}")
    out = {}
    for part in spec_source._split_top(body[m.end():i]):
        am = witness_replay._ACTION_RE.search(part)
        if am:
            out.setdefault(am.group(1), part)
    return out


def probe_seed(cm, probe_text, target):
    """(seed units, named actions, stutter-sensitive) for `target`, a val
    of the (ghost-sliced) probe module `probe_text`."""
    pdecls = spec_source.declarations(probe_text)
    if target not in pdecls:
        raise KeyError(target)
    # The predicate and the probe vals it reads (reached_X reads witness_X).
    texts, seen, frontier = [], set(), [target]
    while frontier:
        n = frontier.pop()
        if n in seen:
            continue
        seen.add(n)
        h = pdecls.get(n)
        if not h or h["kind"] != "val" or not h.get("body"):
            continue
        texts.append(h["body"])
        frontier += _READ.findall(_code(h["body"]))
    named = set()
    for t in texts:
        named |= set(re.findall(r'"([A-Za-z_]\w*)"', t))
    import witness_replay
    ghosts = [n for n, h in pdecls.items() if h["kind"] == "var"]
    step = _decl_body(probe_text, "stepP")
    rhs = []
    for g in ghosts:
        rhs += qi._assigned_rhs(step, g)
    rhs += list(witness_replay.step_branches(probe_text)[0].values())
    seed = cm.reads(texts + rhs, stop=cm.actions)
    sensitive = not GUARDED.match(target)
    return seed, named, sensitive


class Slice:
    """What one check's state slice decided."""

    def __init__(self, cone, relevant, frozen, steps):
        self.cone, self.relevant, self.frozen, self.steps = cone, relevant, frozen, steps

    def scope(self, cm):
        """(vars, {record var: cone fields}) a replay asserts: the cone, and
        nothing frozen."""
        scope, fields = set(), {}
        for v in cm.vars:
            units = cm.whole(v)
            inside = units & self.cone
            if not inside:
                continue
            scope.add(v)
            if v in cm.records and inside != units:
                fields[v] = {u.split(".", 1)[1] for u in inside}
        return scope, fields

    def header(self):
        return [f"// State frozen at init (outside this check's cone): "
                + (", ".join(sorted(self.frozen)) or "none")
                + f" — {len(self.cone)} of {len(self.cone) + len(self.frozen)} "
                  f"state units kept."]


def probe_cone(cm, probe_text, target):
    """The Slice for `target`, a val of the ghost-sliced probe module."""
    steps = [a for a in step_branches(probe_text) if a != IDLE and a in cm.actions]
    seed, named, sensitive = probe_seed(cm, probe_text, target)
    cone, relevant = cm.cone(seed, steps, named, all_relevant=sensitive)
    return Slice(cone, relevant, cm.universe() - cone, steps)


def model_cone(cm, ir, invariant):
    """The Slice for a model invariant, checked with the model's init/step."""
    import completeness
    steps = [a for a in completeness.mirrored_actions(ir) if a in cm.actions]
    h = cm.decls.get(invariant)
    if not h:
        raise KeyError(invariant)
    seed = cm.reads([h.get("body") or ""], stop=cm.actions)
    # `step` itself: its nondet domains and any guard around a call.
    seed |= cm.reads([(cm.decls.get("step") or {}).get("body") or ""], stop=cm.actions)
    cone, relevant = cm.cone(seed, steps)
    return Slice(cone, relevant, cm.universe() - cone, steps)


def rebase_imports(text, prefix):
    import posixpath

    def fix(m):
        return m.group(1) + posixpath.normpath(posixpath.join(prefix, m.group(2))) + m.group(3)
    return re.sub(r'(?m)^(\s*import [\w:]+(?:\.\*)? from ")([^"]+)(")', fix, text)


def sliced_model_text(cm, sl, module_name, import_prefix, init_roots=("init",)):
    """The area model renamed `module_name`, imports rebased `import_prefix`
    away, with every frozen unit's assignment made the identity. None when
    nothing is frozen, or nothing needed an edit."""
    if not sl.frozen:
        return None
    text, _n = cm.frozen_text(sl.frozen, cm.init_actions(init_roots))
    text = re.sub(r"(?m)^(\s*)module \w+ \{", rf"\1module {module_name} {{", text, count=1)
    text = rebase_imports(text, import_prefix)
    head = ["// STATE SLICE of the area model — DERIVED by tools/state_slice.py "
            "at check time, never committed."] + sl.header()
    return "\n".join(head) + "\n" + text


def model_import(probe_text):
    """(line, module, path) of the probe module's one `import M.* from`, or
    None when it imports more than one module (a multi-module area: its
    state is not sliced)."""
    imps = re.findall(r'(?m)^(  import (\w+)\.\* from "([^"]+)"\s*)$', probe_text)
    others = re.findall(r"(?m)^  import ", probe_text)
    if len(imps) != 1 or len(others) != 1:
        return None
    return imps[0]


def retarget(probe_slice_text, sl, module_name, path):
    """The probe slice importing `module_name` from `path` instead of the
    area model, with the frozen state named in its header."""
    imp = model_import(probe_slice_text)
    if imp is None:
        return None
    line, _mod, _p = imp
    out = probe_slice_text.replace(line, f'  import {module_name}.* from "{path}"', 1)
    lines = out.split("\n")
    at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("// Ghosts kept:")), 0)
    return "\n".join(lines[:at] + sl.header() + lines[at:])


def replay_module(cm, sl, ghost_text, module_name, trace, target):
    """(module text, run name, steps kept): `ghost_text` — the check's slice
    of the probe module, importing the FULL model — renamed `module_name`,
    with one run replaying `trace` (restored to _last<Param> names). Steps
    of irrelevant actions are dropped: they are stutters on the cone, and a
    frozen guard may have enabled one the full model does not. Each kept
    step is stepP's own branch with the recorded arguments; every state is
    asserted on the cone only, and the last one to falsify `target`.
    Raises witness_replay.Unreplayable."""
    import witness_replay as wr
    states = trace.get("states") or []
    kept = states[:1] + [st for st in states[1:] if st.get("_lastAction") in sl.relevant]
    nondets, branches = wr.step_branches(ghost_text)
    scope, fields = sl.scope(cm)
    name = wr.run_name(target)
    # An action without arguments has nothing for the trace to record.
    meta = dict(trace.get("#meta") or {})
    params = dict(meta.get("action_params") or {})
    for a, (_text, args) in branches.items():
        if not args:
            params.setdefault(a, [])
    meta["action_params"] = params
    run = wr.replay_run(name, dict(trace, states=kept, **{"#meta": meta}), target,
                        nondets, branches, scope, fields, min_steps=0)
    text = re.sub(r"(?m)^module \w+ \{", f"module {module_name} {{", ghost_text, count=1)
    close = text.rstrip().rfind("}")
    return (text[:close] + "  // -- replay of a state-sliced trace (spec-record) --\n"
            + run + "\n" + text[close:].rstrip() + "\n", name, len(kept) - 1)
