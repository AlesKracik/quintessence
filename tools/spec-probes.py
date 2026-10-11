#!/usr/bin/env python3
"""
spec-probes.py — Generate the witness probe module from the area (spec_source view).

Until now there was a template and a prose instruction to "generate/refresh
the probe module", which meant the module was hand-rolled every time. Two
failure modes came with that, and both are silent:

  - the probes go STALE against the model. An action gains a parameter, the
    hand-written `stepP` still calls it with the old arity, and the module
    stops matching the thing it is supposed to instrument.
  - an action is LEFT OUT of `stepP`. Every probe whose requirement points at
    that action can then never fire, so those requirements report no-witness
    — or worse, are quietly not attempted — and the run still looks healthy.

Both are mechanical properties of the generated text, so they are checked
here rather than hoped for: every action `step` calls gets a branch, or
generation FAILS. Nothing partial is ever written. Helper actions — shared
logic route actions call, never a step of their own — are not mirrored;
write them as `def`s.

What it emits, per the convention in templates/probes.qnt.template:

  ghosts     _lastAction, one _last<Param> per distinct action parameter,
             one _pre_<REQ> boolean per requirement with an @while (stepP
             assigns it from the state BEFORE the step), and one _prev<Var>
             per state var a transition invariant reads
  initP      init plus explicit ghost initial values (never `= <var>`:
             before init there is no prior state to read)
  stepP      the area module's step, mirrored, one branch per action it
             calls, tagging _lastAction and the param ghosts, and setting
             each _pre_<REQ> of a requirement on that action
  defs       pre_<REQ> / post_<REQ> / outcome_<REQ>_<name>: the
             requirement's @while / @shall / @outcome, verbatim, as defs
             over the host action's own parameters — so nothing is rewritten
             and the ghosts are passed as arguments
  oblig      one `val oblig_<REQ>` per requirement: its OBLIGATION, asserted
             positively. Every reachable step of the requirement's action
             from a state where @while held ends where @shall holds (must),
             where one @outcome holds (may), or never happens (forbidden); a
             state requirement holds in every reachable state.
  witness    one `val witness_<REQ>` per requirement that owes one: the
             NEGATION of "the step happened from @while and ended in @shall"
             — the model checker's counterexample to it is the witness trace,
             proof the obligation is not vacuous. A `may` gets one per
             outcome, each with the other outcomes false.

Parameter domains cannot be inferred and are not guessed. `nondet uid =
oneOf(<what?>)` is a scope decision — it decides how much of the state space
the probes explore — so it is declared in the area's intent file under
formal_model.probe_domains, keyed by TYPE (types are stable; parameter names
vary per action) or by `param:<name>` for one parameter. Missing one is a
setup error that prints the exact JSON to add. Ghost initial values are built
from the declared types (records field by field, sums as their first
variant); formal_model.ghost_zeros overrides or fills in.

Usage:
  tools/spec-probes.py <area> [--root .]          # write the module
  tools/spec-probes.py <area> --stdout            # print it, write nothing
  tools/spec-probes.py <area> --check             # CI gate: is it current?

Exit codes: 0 = written / current, 1 = --check found it stale,
2 = setup problem (missing sidecar, undeclared domain, unknown ghost type).
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spec_source  # noqa: E402
from itf_tools import (area_json_path, ghost_for_param,  # noqa: E402
                       ghost_for_var, probe_name, outcome_probe_name,
                       reach_name, obligation_name, pre_ghost, old_ghost,
                       witness_exempt, owes_obligation, is_computed)
from quint_ir import parse_qnt, zero_value as _ir_zero  # noqa: E402

def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def ghost_zeros(area):
    """formal_model.ghost_zeros: explicit initial values, keyed by type."""
    return ((area.get("formal_model") or {}).get("ghost_zeros")) or {}


def mirrored_actions(ir):
    """The actions stepP mirrors: the ones `step` calls, in its order.

    Not every declared action. A helper action shared by route actions —
    `action refuse(r: Result, ...)`, `action commit(s1: State, ...)` — is
    never a step of its own, and mirroring it put a ghost on each of its
    parameters: name clashes between helpers, and ghosts of function type
    that no literal can initialize. Only when `step` names no action (or
    does not exist) does this fall back to every action but init/step."""
    actions = [a for a in (ir.get("actions") or []) if a not in ("init", "step")]
    called = [a for a in (ir.get("action_calls") or {}).get("step") or []
              if a in actions]
    return called or actions


def domain_for(name, type_str, domains):
    """The probe domain for one parameter: `param:<name>` first, then its
    type. Keyed by type by default because types are stable while names vary
    per action; the per-parameter key is for the model whose parameters all
    share `str` and would otherwise draw role, collection and id from one
    set."""
    return domains.get(f"param:{name}", domains.get(type_str))


def prev_readers(area):
    """Every piece of authored Quint in this area that may read a `_prev`
    ghost: the predicates of invariants checked over this module (transition
    properties). Requirements never do — their pre-state is @while, which
    stepP evaluates before the step into a _pre_<REQ> boolean."""
    return [predicate for _inv, _name, predicate in transition_invariants(area)]


def collect_delta_vars(area, model_vars):
    """State vars a transition invariant reads through a `_prev` ghost.

    Only the ones needed: a _prev ghost per state var would double the state
    space for snapshots nothing compares against."""
    needed = set()
    blob = "\n".join(prev_readers(area))
    for var in model_vars:
        if re.search(rf"\b{re.escape(ghost_for_var(var))}\b", blob):
            needed.add(var)
    return sorted(needed)


def transition_invariants(area):
    """(inv, quint_name, predicate) per invariant declared over: "probes".

    An invariant whose truth depends on the state BEFORE as well as after —
    "X never changes", "Y only moves one way" — is not expressible over a
    single state, and `always(P(next(x)))` is no way out: quint compiles it to
    `[](x = x')`, which TLA+ rejects because it wants box-action form
    `[][A]_vars`. What does work is making the pre-state an ordinary variable,
    which is exactly what the `_prev*` ghosts in this module are — so the
    invariant is checked here, with --init=initP --step=stepP.

    Its Quint text therefore cannot live in the sidecar (the ghosts are not in
    scope there), and this module is generated — so it lives in the model as a def over the _prev* ghosts (its body lands here)
    as invariants[].predicate and is emitted from here. Before that field
    existed, the documented `over: "probes"` path had nowhere to declare the
    val at all and spec-lint FAILed every invariant that took it."""
    out = []
    for inv in area.get("invariants") or []:
        if (inv.get("over") or "model") != "probes":
            continue
        iid = inv.get("id", "?")
        name, predicate = inv.get("quint_name"), inv.get("predicate")
        if not name or not predicate:
            missing = " and ".join(f for f in (
                None if name else "quint_name",
                None if predicate else "predicate") if f)
            fail(f'{iid} declares over: "probes" but has no {missing}.\n'
                 f"A transition invariant is checked in THIS module, so this "
                 f"generator writes its val: it needs the name to declare "
                 f"(quint_name) and the Quint expression to declare it as "
                 f"(predicate), which may read the _prev* ghosts.\n"
                 f"Either add both to {iid}, or drop over: \"probes\" and "
                 f"state it over a single state in the sidecar.")
        out.append((inv, name, predicate))
    return out


def def_name(prefix, rid, suffix=None):
    base = f"{prefix}_" + re.sub(r"[^A-Za-z0-9]+", "_", rid)
    if suffix:
        base += "_" + (re.sub(r"[^A-Za-z0-9]+", "_", suffix).strip("_") or "outcome")
    return base


def requirement_checks(area):
    """One entry per requirement the model must prove, in declaration order:
    {req, rid, form, action, while, shall, outcomes, modality}. Skipped:
    deferred and non-functional requirements (no behavior in this model),
    computed ones (their proof is a unit test), and anything not yet formal
    (spec-lint FAILs that past `raw` — an empty check here would hide it)."""
    out = []
    for req in area.get("requirements") or []:
        rid = req.get("id")
        if not rid or not owes_obligation(req):
            continue
        ears = req.get("ears") or {}
        outs = [oc for oc in (req.get("witness") or {}).get("outcomes") or []
                if oc.get("predicate")]
        out.append({"req": req, "rid": rid, "form": req.get("form"),
                    "action": req.get("quint_ref"),
                    "while": (ears.get("state") or "").strip() or None,
                    "shall": (ears.get("response") or "").strip() or None,
                    "outcomes": outs if req.get("modality") == "may" else [],
                    "old": req.get("old") or [],
                    "modality": req.get("modality") or "must"})
    return out


def probe_requirements(area):
    """(req, probe_name, outcome_name) per witness probe this module emits —
    what spec-record looks up. A requirement whose witness is exempt by what
    it is (witness_exempt) has none."""
    out = []
    for c in requirement_checks(area):
        if witness_exempt(c["req"]):
            continue
        if c["outcomes"]:
            out += [(c["req"], outcome_probe_name(c["rid"], oc.get("name")),
                     oc.get("name")) for oc in c["outcomes"]]
        else:
            out.append((c["req"], probe_name(c["rid"]), None))
    return out


def build(area, area_name, ir, quint_rel):
    """The whole module as a list of lines. Pure: same inputs, same bytes."""
    module = ir.get("module_name") or area_name
    actions = mirrored_actions(ir)
    if not actions:
        fail(f"{quint_rel} declares no actions besides init/step — there is "
             f"nothing for stepP to mirror.")

    param_types = ir.get("action_param_types") or {}
    var_type_strs = ir.get("var_type_strs") or {}
    domains = ((area.get("formal_model") or {}).get("probe_domains")) or {}
    overrides = ghost_zeros(area)

    def zero(type_str):
        return _ir_zero(type_str, ir, overrides)

    # ── parameter ghosts, and the domains stepP will draw from ──────────
    params = {}          # param name -> type as written
    for action in actions:
        for name, type_str in param_types.get(action) or []:
            if name in params and params[name] != type_str:
                fail(f"parameter '{name}' is declared as '{params[name]}' in one "
                     f"action and '{type_str}' in another. One ghost cannot hold "
                     f"both — rename one of the parameters.")
            params[name] = type_str

    checks = requirement_checks(area)
    # A requirement on an action stepP never takes can never be exercised:
    # its obligation would hold vacuously and its witness never fire. Said
    # here, where the cause is known.
    unmirrored = sorted({(c["rid"], c["action"]) for c in checks
                         if c["form"] == "event" and c["action"] not in actions})
    if unmirrored:
        fail("requirement on an action step never calls: "
             + ", ".join(f"{rid} on {ref}" for rid, ref in unmirrored)
             + f".\nstepP mirrors only the actions `step` calls ("
             + ", ".join(actions) + "). Put the requirement on the route-level "
               "action the helper is called from, or call it from step.")

    missing_domains = sorted({t for n, t in params.items()
                              if domain_for(n, t, domains) is None})
    if missing_domains:
        suggestion = json.dumps(
            {"probe_domains": {t: "Set(/* values to explore */)"
                               for t in missing_domains}}, indent=2)
        fail("no probe domain declared for: " + ", ".join(missing_domains) +
             ".\nstepP has to draw each parameter from a finite set, and which "
             "values to explore is a scope decision this tool will not guess — "
             "too small and probes cannot fire, too large and every check pays "
             "for it.\nAdd to formal_model in specs/" + area_name +
             ".intent.json (keys are types; `param:<name>` targets one "
             "parameter):\n\n" + suggestion)

    missing_zero = {}
    for name, type_str in params.items():
        if zero(type_str) is None:
            missing_zero[name] = type_str
    delta_vars = collect_delta_vars(area, ir.get("vars") or [])
    for var in delta_vars:
        if zero(var_type_strs.get(var, "")) is None:
            missing_zero[ghost_for_var(var)] = var_type_strs.get(var, "?")
    if missing_zero:
        fail("cannot synthesize an initial value for: "
             + ", ".join(f"{g} ({t})" for g, t in sorted(missing_zero.items()))
             + ".\nGhost initial values are written explicitly — before init "
               "there is no prior state to read. This tool builds the zero of "
               "str/int/bool, maps, sets, lists, tuples, records (field by "
               "field) and sum types (their first variant). For anything else, "
               "declare one in formal_model.ghost_zeros, keyed by the type as "
               "written: {\"ghost_zeros\": {\"<Type>\": \"<Quint literal>\"}}.")

    transitions = transition_invariants(area)

    # A transition invariant reading a ghost that does not exist is the same
    # staleness this generator exists to catch: `_prevSesions` for
    # `_prevSessions` compiles to an unresolved name at check time, long after
    # the spec looked fine. The ghosts that DO exist are the _prev of a state
    # var, so an unknown one is answerable here.
    known_ghosts = {ghost_for_var(v) for v in (ir.get("vars") or [])}
    for inv, name, predicate in transitions:
        unknown = sorted({g for g in re.findall(r"\b_prev\w+\b", predicate)
                          if g not in known_ghosts})
        if unknown:
            fail(f"{inv.get('id', '?')}.predicate reads "
                 + ", ".join(unknown)
                 + ", which is not a ghost of any state var in the sidecar.\n"
                   "The pre-state ghosts available are: "
                 + (", ".join(sorted(known_ghosts)) or "(the sidecar declares "
                    "no state vars)") + ".")

    # Requirements whose @while needs a _pre ghost: those on an action. A
    # state requirement's @while is read in the same state as its @shall.
    pre_reqs = [c for c in checks if c["form"] == "event" and c["while"]]
    old_vals = [(c, o) for c in checks if c["form"] == "event" for o in c["old"]]
    for c, o in old_vals:
        if zero(o["type"]) is None:
            fail(f"{c['rid']} @old {o['name']}: cannot build an initial value "
                 f"for type '{o['type']}' — declare one in "
                 f"formal_model.ghost_zeros.")

    def sig(action):
        ps = param_types.get(action) or []
        return ", ".join(f"{n}: {t}" for n, t in ps)

    def ghost_args(action):
        return ", ".join(ghost_for_param(n) for n, _ in param_types.get(action) or [])

    def call(name, args):
        return f"{name}({args})" if args else name

    def head(name, action, olds=(), ret="bool"):
        sg = ", ".join(x for x in [sig(action)] + [f"{o['name']}: {o['type']}"
                                                   for o in olds] if x)
        return f"  def {name}({sg}): {ret} =" if sg else f"  val {name}: {ret} ="

    def post_args(c):
        """The arguments a post/outcome def is called with in the probe
        module: the action's parameter ghosts, then the @old ghosts."""
        return ", ".join(x for x in [ghost_args(c["action"])]
                         + [old_ghost(c["rid"], o["name"]) for o in c["old"]] if x)

    L = []
    a = L.append
    a("// " + "=" * 58)
    a(f"// Requirement Probe Module: {area_name}")
    a("// GENERATED by tools/spec-probes.py — regenerate, never hand-edit.")
    a(f"// Source: the @req records in specs/{area_name}.qnt — each")
    a("// requirement's @while / @shall / @outcome, verbatim, as defs over its")
    a("// action's own parameters.")
    a("//")
    a("// Every requirement is checked twice:")
    a("//   oblig_<REQ>    its OBLIGATION, asserted positively: every reachable")
    a("//                  step of its action from a state where @while held")
    a("//                  ends where @shall holds (or never happens, for a")
    a("//                  forbidden one). A counterexample is a VIOLATION.")
    a("//   witness_<REQ>  the NEGATION of \"such a step happened\": the model")
    a("//                  checker's counterexample IS the witness trace, proof")
    a("//                  the obligation is not vacuous.")
    a("// _pre_<REQ> is set by stepP from the state BEFORE the step, so @while")
    a("// is read where the sentence says it holds. The path conjunct comes")
    a("// first: at init no action has run and no ghost may be read as a key.")
    a("// " + "=" * 58)
    a("")
    a(f"module {module}_probes {{")
    a("")
    a(f'  import {module}.* from "./{Path(quint_rel).stem}"')
    a("")
    a("  // -- Ghost instrumentation ---------------------------------")
    a('  var _lastAction: str')
    for name in sorted(params):
        a(f"  var {ghost_for_param(name)}: {params[name]}")
    for c in pre_reqs:
        a(f"  var {pre_ghost(c['rid'])}: bool")
    for c, o in old_vals:
        a(f"  var {old_ghost(c['rid'], o['name'])}: {o['type']}")
    for var in delta_vars:
        a(f"  var {ghost_for_var(var)}: {var_type_strs.get(var, 'int')}")
    if checks:
        a("")
        a("  // -- What each requirement says, over its action's parameters --")
    for c in checks:
        rid, action = c["rid"], c["action"]
        at = action if c["form"] == "event" else None
        olds = c["old"] if at else []
        if c["while"]:
            a(head(def_name("pre", rid), at))
            a(f"    {c['while']}")
        for o in olds:
            a(head(def_name("old", rid, o["name"]), at, ret=o["type"]))
            a(f"    {o['expr']}")
        if c["shall"] and c["modality"] == "must":
            a(head(def_name("post", rid), at, olds))
            a(f"    {c['shall']}")
        for oc in c["outcomes"]:
            a(head(def_name("outcome", rid, oc.get("name")), at, olds))
            a(f"    {oc['predicate'].strip()}")
    a("")
    a("  action initP = all {")
    a("    init,")
    a('    _lastAction\' = "init",')
    for name in sorted(params):
        a(f"    {ghost_for_param(name)}' = {zero(params[name])},")
    for c in pre_reqs:
        a(f"    {pre_ghost(c['rid'])}' = false,")
    for c, o in old_vals:
        a(f"    {old_ghost(c['rid'], o['name'])}' = {zero(o['type'])},")
    for var in delta_vars:
        a(f"    {ghost_for_var(var)}' = "
          f"{zero(var_type_strs.get(var, ''))},")
    a("  }")
    a("")
    a("  action stepP = {")
    for name in sorted(params):
        a(f"    nondet {name} = oneOf({domain_for(name, params[name], domains)})")
    a("    any {")
    for action in actions:
        args = [n for n, _ in (param_types.get(action) or [])]
        callx = f"{action}({', '.join(args)})" if args else action
        a(f"      all {{ {callx},")
        a(f'            _lastAction\' = "{action}",')
        for name in sorted(params):
            ghost = ghost_for_param(name)
            # A branch that does not take this parameter identity-assigns its
            # ghost: Quint requires every var assigned in every action, and
            # carrying the previous value is what "this action did not touch
            # it" means.
            a(f"            {ghost}' = {name if name in args else ghost},")
        for c in pre_reqs:
            # Read BEFORE the step: the right-hand side is the unprimed state.
            value = (call(def_name("pre", c["rid"]), ", ".join(args))
                     if c["action"] == action else "false")
            a(f"            {pre_ghost(c['rid'])}' = {value},")
        for c, o in old_vals:
            g = old_ghost(c["rid"], o["name"])
            value = (call(def_name("old", c["rid"], o["name"]), ", ".join(args))
                     if c["action"] == action else g)
            a(f"            {g}' = {value},")
        for var in delta_vars:
            a(f"            {ghost_for_var(var)}' = {var},")
        a("      },")
    a("    }")
    a("  }")
    claimed = set()
    if transitions:
        a("")
        a("  // -- Transition invariants ----------------------------------")
        a("  // Declared over: \"probes\" because they read the pre-state. These")
        a("  // are asserted POSITIVELY, unlike the probes below: a")
        a("  // counterexample to one of these is a VIOLATION, not a witness.")
        generated = ({probe_name(c["rid"]) for c in checks}
                     | {obligation_name(c["rid"]) for c in checks}
                     | {outcome_probe_name(c["rid"], o.get("name"))
                        for c in checks for o in c["outcomes"]})
        for inv, name, predicate in transitions:
            if name in generated or name in claimed:
                fail(f"{inv.get('id', '?')}.quint_name '{name}' collides with "
                     f"a generated name in the probe module. Rename the "
                     f"invariant's val — generated names are derived from "
                     f"requirement ids and cannot move.")
            claimed.add(name)
            desc = (inv.get("description") or "").strip().replace("\n", " ")
            a("")
            a(f"  /// {inv.get('id', '?')}: {desc}")
            a(f"  val {name}: bool =")
            a(f"    {predicate.strip()}")

    a("")
    a("  // -- Obligations: every reachable step ----------------------")
    if not checks:
        a("  // No requirement in this area is formal yet.")
    witnesses = []
    for c in checks:
        rid, req, action = c["rid"], c["req"], c["action"]
        desc = (req.get("description") or "").strip().replace("\n", " ")
        if c["form"] == "event":
            g = post_args(c)
            fired = [f'_lastAction == "{action}"']
            if c["while"]:
                fired.append(pre_ghost(rid))
            if c["modality"] == "forbidden":
                body = "not(" + " and ".join(fired) + ")"
                result = None
            elif c["modality"] == "may":
                outs = [call(def_name("outcome", rid, o.get("name")), g)
                        for o in c["outcomes"]]
                result = " or ".join(outs)
                body = (f"not({' and '.join(fired)})\n      or ({result})")
                for k, o in enumerate(c["outcomes"]):
                    others = [f"not({x})" for j, x in enumerate(outs) if j != k]
                    witnesses.append((outcome_probe_name(rid, o.get("name")),
                                      f"{rid} / {o.get('name')}", desc,
                                      fired + [outs[k]] + others))
            else:
                result = call(def_name("post", rid), g)
                body = f"not({' and '.join(fired)})\n      or {result}"
                if not witness_exempt(req):
                    witnesses.append((probe_name(rid), rid, desc,
                                      fired + [result]))
        else:   # state
            post = call(def_name("post", rid), "")
            if c["while"]:
                pre = call(def_name("pre", rid), "")
                body = f"not({pre}) or {post}"
                witnesses.append((probe_name(rid), rid, desc, [pre, post]))
            else:
                body = post
        a("")
        a(f"  /// {rid}: {desc}")
        a(f"  val {obligation_name(rid)}: bool =")
        a(f"    {body}")

    a("")
    a("  // -- Witnesses: the step can happen -------------------------")
    if not witnesses:
        a("  // No requirement in this area owes a witness.")
    for name, label, desc, conjuncts in witnesses:
        a("")
        a(f"  /// Witness for {label}: {desc}")
        a(f"  val {name}: bool =")
        a("    not(" + ("\n        and ".join(conjuncts)) + ")")
    if witnesses:
        a("")
        a("  // -- The same probes, the right way up ----------------------")
        a("  // For the simulator. `quint run --witnesses=<name>` counts the")
        a("  // runs in which a formula held at least once, and each probe above")
        a("  // is a negation, which holds at init. These count the runs that")
        a("  // reached each behavior: the vacuity pre-screen spec-record runs")
        a("  // before the model checker, and the example gate in CI.")
        for name, _l, _d, _c in witnesses:
            a(f"  val {reach_name(name)}: bool = not({name})")
    a("")
    # Requirements with nothing here are named, not silently absent: an
    # unexplained gap in this file is indistinguishable from the omission
    # bug this generator exists to prevent.
    notes = []
    in_checks = {c["rid"] for c in checks}
    for c in checks:
        why = witness_exempt(c["req"])
        if why:
            notes.append(f"{c['rid']}: no witness — {why}")
    for req in area.get("requirements") or []:
        rid = req.get("id")
        if not rid or rid in in_checks:
            continue
        if is_computed(req):
            notes.append(f"{rid}: computed — verified by "
                         + (", ".join(req.get("verified_by") or [])
                            or "NO TEST NAMED (add @verified-by)"))
        elif req.get("type") == "non-functional":
            notes.append(f"{rid}: non-functional — fit criterion, not a trace")
        elif req.get("status") == "deferred":
            notes.append(f"{rid}: deferred")
        else:
            notes.append(f"{rid}: NOT FORMAL YET — no @shall / @outcome to check")
    if notes:
        a("  // Not checked here, or checked without a witness:")
        for line in notes:
            a(f"  //   {line}")
        a("")
    a("}")

    # Every declared action reached stepP. This is the miscount that silently
    # left actions out of hand-written modules, so it is asserted on the
    # generated text rather than trusted.
    body = "\n".join(L)
    absent = [x for x in actions
              if not re.search(rf'_lastAction\' = "{re.escape(x)}"', body)]
    if absent:
        fail("internal: actions missing from stepP: " + ", ".join(absent))
    return L


def main():
    ap = argparse.ArgumentParser(description="Generate the witness probe module.")
    ap.add_argument("area")
    ap.add_argument("--root", default=".")
    ap.add_argument("--stdout", action="store_true",
                    help="Print the module instead of writing it.")
    ap.add_argument("--check", action="store_true",
                    help="Exit 1 if the file on disk is not what would be "
                         "generated. For CI: catches a probe module that has "
                         "gone stale against its model.")
    args = ap.parse_args()

    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    try:
        area = spec_source.load_area(root, args.area)
    except spec_source.SpecSourceError as e:
        fail(str(e))
    if area is None:
        fail(f"{area_path} not found")

    fm = area.get("formal_model") or {}
    quint_rel = fm.get("quint_file") or f"{args.area}.qnt"
    quint_path = root / "specs" / quint_rel
    if not quint_path.exists():
        fail(f"{quint_path} not found — the area has no Quint sidecar to probe.")
    ir = parse_qnt(quint_path)
    if ir is None:
        fail(f"{quint_path} did not parse as a Quint module.")

    lines = build(area, args.area, ir, quint_rel)
    text = "\n".join(lines).rstrip() + "\n"

    if args.stdout:
        sys.stdout.write(text)
        return 0

    probes_rel = fm.get("probes_file") or f"{args.area}.probes.qnt"
    probes_path = root / "specs" / probes_rel

    if args.check:
        if not probes_path.exists():
            print(f"STALE: {probes_path} does not exist. Run "
                  f"tools/spec-probes.py {args.area}")
            return 1
        current = probes_path.read_text(encoding="utf-8")
        if current != text:
            print(f"STALE: {probes_path} is not what the area and its model "
                  f"generate. Every witness checked against it proves something "
                  f"about a module that no longer matches the model.\n"
                  f"Run: tools/spec-probes.py {args.area}")
            return 1
        print(f"current: {probes_path}")
        return 0

    probes_path.parent.mkdir(parents=True, exist_ok=True)
    probes_path.write_text(text, encoding="utf-8")
    n_probes = sum(1 for ln in lines if ln.startswith("  val witness_"))
    n_obl = sum(1 for ln in lines if ln.startswith("  val oblig_"))
    n_actions = sum(1 for ln in lines if "_lastAction' = \"" in ln) - 1
    print(f"wrote {probes_path}: {n_obl} obligation(s), {n_probes} witness "
          f"probe(s), {n_actions} action branch(es) in stepP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
