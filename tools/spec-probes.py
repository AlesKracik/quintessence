#!/usr/bin/env python3
"""
spec-probes.py — Generate the witness probe module from the area JSON.

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
here rather than hoped for: every action declared in the sidecar gets a
branch, or generation FAILS. Nothing partial is ever written.

What it emits, per the convention in templates/probes.qnt.template:

  ghosts     _lastAction, one _last<Param> per distinct action parameter,
             one _prev<Var> per state var some witness.delta reads
  initP      init plus explicit ghost initial values (never `= <var>`:
             before init there is no prior state to read)
  stepP      the area module's step, mirrored, one branch per action,
             tagging _lastAction and the param ghosts, snapshotting _prev
  probes     one `val witness_<REQ>` per requirement carrying a predicate,
             composed of the three conjuncts the methodology requires:
             the bound predicate, the path constraint `_lastAction ==
             <quint_ref>`, and the delta over the `_prev*` ghosts

Parameter domains cannot be inferred and are not guessed. `nondet uid =
oneOf(<what?>)` is a scope decision — it decides how much of the state space
the probes explore — so it is declared in the area JSON under
formal_model.probe_domains, keyed by TYPE (types are stable; parameter names
vary per action). Missing one is a setup error that prints the exact JSON to
add.

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
from itf_tools import (area_json_path, ghost_for_param,  # noqa: E402
                       ghost_for_var, probe_name, outcome_probe_name)
from quint_ir import parse_qnt  # noqa: E402

BASE_ZERO = {
    "str": '""',
    "int": "0",
    "bool": "false",
}


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def resolve_alias(type_str, aliases, seen=None):
    """Follow `type UserId = str` to its base. Cycle-guarded, because a
    malformed sidecar should produce a diagnosable error, not a hang."""
    seen = seen or set()
    current = (type_str or "").strip()
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current].strip()
    return current


def zero_value(type_str, aliases):
    """The initial value for a ghost of this type, or None when there isn't
    an obvious one.

    Ghost initial values are written EXPLICITLY rather than as `= <var>`,
    because before init there is no prior state to read — so every ghost
    needs a literal, and a type this cannot answer for has to be declared
    rather than guessed."""
    base = resolve_alias(type_str, aliases)
    if base in BASE_ZERO:
        return BASE_ZERO[base]
    if "->" in base:
        return "Map()"
    if base.startswith("Set["):
        return "Set()"
    if base.startswith("List["):
        return "[]"
    return None


def collect_delta_vars(area, model_vars):
    """State vars some witness.delta actually reads, via its `_prev` ghost.

    Only the ones needed: a _prev ghost per state var would double the state
    space for snapshots nothing compares against."""
    needed = set()
    text = []
    for req in area.get("requirements") or []:
        w = req.get("witness") or {}
        pre = (w.get("delta") or {}).get("pre")
        if pre:
            text.append(pre)
        for oc in w.get("outcomes") or []:
            oc_pre = (oc.get("delta") or {}).get("pre")
            if oc_pre:
                text.append(oc_pre)
    blob = "\n".join(text)
    for var in model_vars:
        if re.search(rf"\b{re.escape(ghost_for_var(var))}\b", blob):
            needed.add(var)
    return sorted(needed)


def probe_requirements(area):
    """(req, probe_name, predicate, delta_pre) for everything that owes a
    probe, permitted outcomes included.

    Skipped on purpose, each for a different reason:
      - deferred / non-functional: no behavior to witness in this model;
      - forbidden with enforced_by: a prohibition has no reachable state to
        witness, and the named invariant carries the proof;
      - status 'skipped': the author discharged it in prose;
      - no predicate: nothing to assert (spec-lint FAILs this separately —
        emitting an empty probe here would hide it)."""
    out = []
    for req in area.get("requirements") or []:
        rid = req.get("id")
        if not rid or req.get("status") in ("deferred",) or \
                req.get("type") == "non-functional":
            continue
        w = req.get("witness") or {}
        if w.get("status") == "skipped" or (req.get("modality") == "forbidden"
                                            and w.get("enforced_by")):
            continue
        if req.get("modality") == "may":
            for oc in w.get("outcomes") or []:
                if oc.get("predicate"):
                    out.append((req, outcome_probe_name(rid, oc.get("name")),
                                oc["predicate"],
                                (oc.get("delta") or {}).get("pre"),
                                oc.get("name")))
            continue
        if w.get("predicate"):
            out.append((req, probe_name(rid), w["predicate"],
                        (w.get("delta") or {}).get("pre"), None))
    return out


def build(area, area_name, ir, quint_rel):
    """The whole module as a list of lines. Pure: same inputs, same bytes."""
    module = ir.get("module_name") or area_name
    actions = [a for a in (ir.get("actions") or []) if a not in ("init", "step")]
    if not actions:
        fail(f"{quint_rel} declares no actions besides init/step — there is "
             f"nothing for stepP to mirror.")

    param_types = ir.get("action_param_types") or {}
    aliases = ir.get("type_aliases") or {}
    var_type_strs = ir.get("var_type_strs") or {}
    domains = ((area.get("formal_model") or {}).get("probe_domains")) or {}

    # ── parameter ghosts, and the domains stepP will draw from ──────────
    params = {}          # param name -> type as written
    for action in actions:
        for name, type_str in param_types.get(action) or []:
            if name in params and params[name] != type_str:
                fail(f"parameter '{name}' is declared as '{params[name]}' in one "
                     f"action and '{type_str}' in another. One ghost cannot hold "
                     f"both — rename one of the parameters.")
            params[name] = type_str

    missing_domains = sorted({t for t in params.values() if t not in domains})
    if missing_domains:
        suggestion = json.dumps(
            {"probe_domains": {t: "Set(/* values to explore */)"
                               for t in missing_domains}}, indent=2)
        fail("no probe domain declared for: " + ", ".join(missing_domains) +
             ".\nstepP has to draw each parameter from a finite set, and which "
             "values to explore is a scope decision this tool will not guess — "
             "too small and probes cannot fire, too large and every check pays "
             "for it.\nAdd to formal_model in specs/" + area_name +
             ".area.json:\n\n" + suggestion)

    missing_zero = {}
    for name, type_str in params.items():
        zero = zero_value(type_str, aliases)
        if zero is None:
            missing_zero[name] = type_str
    delta_vars = collect_delta_vars(area, ir.get("vars") or [])
    for var in delta_vars:
        if zero_value(var_type_strs.get(var, ""), aliases) is None:
            missing_zero[ghost_for_var(var)] = var_type_strs.get(var, "?")
    if missing_zero:
        fail("cannot synthesize an initial value for: "
             + ", ".join(f"{g} ({t})" for g, t in sorted(missing_zero.items()))
             + ".\nGhost initial values are written explicitly — before init "
               "there is no prior state to read — and this tool only knows the "
               "zero of str/int/bool, maps, sets and lists. Give the type a "
               "plain alias to one of those, or snapshot a different var.")

    probes = probe_requirements(area)

    L = []
    a = L.append
    a("// " + "=" * 58)
    a(f"// Witness Probe Module: {area_name}")
    a("// GENERATED by tools/spec-probes.py — regenerate, never hand-edit.")
    a(f"// Source of predicates: requirements[].witness in specs/{area_name}.area.json")
    a("//")
    a("// To prove a behavior is REACHABLE, assert its negation as an")
    a("// invariant — the checker's counterexample IS the witness trace.")
    a("//")
    a("// Each probe carries up to THREE conjuncts, and each closes a")
    a("// different way a witness can prove less than the requirement claims:")
    a("//   1. PREDICATE, bound to the param ghosts.")
    a("//   2. PATH CONSTRAINT (_lastAction == quint_ref) — the trace must")
    a("//      arrive via the requirement's own action.")
    a("//   3. DELTA over the _prev* ghosts — the pre-state the step had to")
    a("//      start from. Without it a predicate already true at init")
    a("//      witnesses green over nothing; spec-record refuses that trace")
    a("//      as HOLLOW.")
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
    for var in delta_vars:
        a(f"  var {ghost_for_var(var)}: {var_type_strs.get(var, 'int')}")
    a("")
    a("  action initP = all {")
    a("    init,")
    a('    _lastAction\' = "init",')
    for name in sorted(params):
        a(f"    {ghost_for_param(name)}' = {zero_value(params[name], aliases)},")
    for var in delta_vars:
        a(f"    {ghost_for_var(var)}' = "
          f"{zero_value(var_type_strs.get(var, ''), aliases)},")
    a("  }")
    a("")
    a("  action stepP = {")
    for name in sorted(params):
        a(f"    nondet {name} = oneOf({domains[params[name]]})")
    a("    any {")
    for action in actions:
        args = [n for n, _ in (param_types.get(action) or [])]
        call = f"{action}({', '.join(args)})" if args else action
        a(f"      all {{ {call},")
        a(f'            _lastAction\' = "{action}",')
        for name in sorted(params):
            ghost = ghost_for_param(name)
            # A branch that does not take this parameter identity-assigns its
            # ghost: Quint requires every var assigned in every action, and
            # carrying the previous value is what "this action did not touch
            # it" means.
            a(f"            {ghost}' = {name if name in args else ghost},")
        for var in delta_vars:
            a(f"            {ghost_for_var(var)}' = {var},")
        a("      },")
    a("    }")
    a("  }")
    a("")
    a("  // -- Witness probes -----------------------------------------")
    if not probes:
        a("  // No requirement in this area carries a witness.predicate yet.")
    for req, name, predicate, delta_pre, outcome in probes:
        rid = req.get("id")
        desc = (req.get("description") or "").strip().replace("\n", " ")
        label = f"{rid}" + (f" / {outcome}" if outcome else "")
        a("")
        a(f"  /// Witness probe for {label}: {desc}")
        conjuncts = [predicate.strip()]
        qref = req.get("quint_ref")
        if qref:
            conjuncts.append(f'_lastAction == "{qref}"')
        if delta_pre:
            conjuncts.append(delta_pre.strip())
        a(f"  val {name}: bool =")
        a("    not(" + ("\n        and ".join(conjuncts)) + ")")
    a("")
    # Requirements that owe no probe are named, not silently absent: an
    # unexplained gap in this file is indistinguishable from the omission
    # bug this generator exists to prevent.
    skipped = []
    for req in area.get("requirements") or []:
        rid = req.get("id")
        if not rid or any(p[0] is req for p in probes):
            continue
        w = req.get("witness") or {}
        if req.get("modality") == "forbidden" and w.get("enforced_by"):
            skipped.append(f"{rid}: forbidden — proof is {w['enforced_by']}")
        elif w.get("status") == "skipped":
            skipped.append(f"{rid}: discharged in prose (witness.status skipped)")
        elif req.get("type") == "non-functional":
            skipped.append(f"{rid}: non-functional — fit criterion, not a trace")
        elif req.get("status") == "deferred":
            skipped.append(f"{rid}: deferred")
        elif not (w.get("predicate") or w.get("outcomes")):
            skipped.append(f"{rid}: NO PREDICATE YET — spec-lint FAILs this")
    if skipped:
        a("  // No probe, by design or because one is owed:")
        for line in skipped:
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
        area = json.loads(Path(area_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        fail(f"{area_path} unreadable: {e}")

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
            print(f"STALE: {probes_path} is not what the area JSON and sidecar "
                  f"generate. Every witness checked against it proves something "
                  f"about a module that no longer matches the model.\n"
                  f"Run: tools/spec-probes.py {args.area}")
            return 1
        print(f"current: {probes_path}")
        return 0

    probes_path.parent.mkdir(parents=True, exist_ok=True)
    probes_path.write_text(text, encoding="utf-8")
    n_probes = sum(1 for ln in lines if ln.startswith("  val witness_"))
    n_actions = sum(1 for ln in lines if "_lastAction' = \"" in ln) - 1
    print(f"wrote {probes_path}: {n_probes} probe(s), {n_actions} action "
          f"branch(es) in stepP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
