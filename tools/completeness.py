"""
completeness.py — what the probe module checks, computed once.

spec-probes writes these checks into the probe module; spec-record runs
them and records each verdict under the id it gets here; spec-readback
renders them as one decision table per action; spec-lint holds the
recorded verdicts to account. They all read the same list from here, so
none of them can disagree with the generator about which checks exist or
what they are called.

Per requirement: its obligation and witness (requirement_checks).
Per action `step` calls (mirrored_actions):

  coverage     every step of it is under some requirement's @while or some
               verdict's — no step is one nothing says anything about
  explanation  per observable var it may change: every step that changes
               it is one where a requirement reading it applied (or a GAP
               / OUT-OF-SCOPE verdict did)
  verdict      per IMPOSSIBLE / NO-OP verdict on it: no step from its
               @while happens / every such step changes no observable var
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spec_source  # noqa: E402
from itf_tools import (ghost_for_var, pre_ghost, owes_obligation,  # noqa: E402
                       coverage_name, explanation_name, verdict_name,
                       verdict_ghost, verdict_label, completeness_check_id)


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


def env_at_init(area):
    """{action: {"values": {...} or None, "ignore": [...]}} from
    formal_model.env_at_init, in order. A bare name derives the fold; an
    object may carry the author's value set per write unit (`var`, or
    `var.field` of a record var) and the units it deliberately leaves to
    init (`ignore`)."""
    out = {}
    for item in ((area or {}).get("formal_model") or {}).get("env_at_init") or []:
        if isinstance(item, str):
            out[item] = {"values": None, "ignore": []}
        elif isinstance(item, dict) and item.get("action"):
            out[item["action"]] = {"values": item.get("values") or None,
                                   "ignore": list(item.get("ignore") or [])}
    return out


def step_actions(area, ir):
    """The actions stepP takes: the ones `step` calls, minus the environment
    settings formal_model.env_at_init moves into initP."""
    env = env_at_init(area)
    return [a for a in mirrored_actions(ir) if a not in env]


# A WRITE UNIT is what an action changes: a whole var, or `var.field` for a
# record var whose new value the IR could resolve to fields
# (quint_ir.scan_field_effects). A record-state model writes `st` in every
# action; compared per var, no two actions would ever be independent.

def units_overlap(a, b):
    """`st` overlaps `st.cfg`; `st.cfg` does not overlap `st.items`."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def _units(ir, action, kind):
    plain = set((ir.get(f"action_{kind}") or {}).get(action) or [])
    fields = (ir.get(f"action_field_{kind}") or {}).get(action) or {}
    out = {v for v in plain if v not in fields}
    for var, fs in fields.items():
        out |= {f"{var}.{f}" for f in fs}
    return out


def action_writes(ir, action, _seen=None):
    """Write units `action` changes (not `x' = x`, not `{ ...st, f: st.f }`),
    through the helper actions it calls."""
    seen = _seen if _seen is not None else set()
    if action in seen:
        return set()
    seen.add(action)
    out = _units(ir, action, "mutations")
    for callee in (ir.get("action_calls") or {}).get(action) or []:
        out |= action_writes(ir, callee, seen)
    return out


def action_read_units(ir, action, _seen=None):
    """Units `action` reads: a record var's fields where its body can be
    resolved (helper defs followed), the whole var otherwise; through the
    helper actions it calls."""
    seen = _seen if _seen is not None else set()
    if action in seen:
        return set()
    seen.add(action)
    plain = set((ir.get("action_reads") or {}).get(action) or [])
    fields = (ir.get("action_field_reads") or {}).get(action) or {}
    callees = (ir.get("action_calls") or {}).get(action) or []
    out = set()
    for callee in callees:
        out |= action_read_units(ir, callee, seen)
    # action_reads is transitive over called actions. A var this body was
    # resolved for (fields present, even empty) is answered by its fields
    # plus the callees' own units; one it was not is read whole.
    out |= {v for v in plain if v not in fields}
    for var, fs in fields.items():
        out |= {f"{var}.{f}" for f in fs}
    return out


def env_analysis(area, ir, action, ignore=()):
    """What env_at_init needs to know about `action`:
      own       the units it writes that no other step writes: the setting
      shared    {unit: [other actions]} for units it writes that others write
                too (minus `ignore`)
      blockers  every reason it cannot be fixed per trace, as sentences;
                empty when it can
    The shape of its body (flat or not) is not judged here: a non-flat one
    only needs declared `values`."""
    out = {"own": set(), "shared": {}, "blockers": []}
    blockers = out["blockers"]
    mirrored = mirrored_actions(ir)
    if action not in mirrored:
        blockers.append("`step` does not call it")
        return out
    hosted = sorted({c["rid"] for c in requirement_checks(area)
                     if c["action"] == action})
    if hosted:
        blockers.append(f"it hosts {', '.join(hosted)}, which would never fire "
                        f"once it leaves stepP")
    checked = [v["verdict"] for v in area.get("verdicts") or []
               if v.get("action") == action
               and v.get("verdict") in ("IMPOSSIBLE", "NO-OP")]
    if checked:
        blockers.append(f"it carries an {checked[0]} verdict, a check on its steps")
    ignored = set(ignore or ())
    writes = {a: action_writes(ir, a) for a in mirrored}
    mine = {u for u in writes[action]
            if not any(units_overlap(u, i) and len(u) >= len(i) for i in ignored)}
    for u in sorted(mine):
        others = sorted(o for o in mirrored if o != action
                        and any(units_overlap(u, w) for w in writes[o]))
        if others:
            out["shared"][u] = others
        else:
            out["own"].add(u)
    if not writes[action]:
        blockers.append("it writes no state var")
    elif not out["own"] and not out["shared"]:
        blockers.append("everything it writes is listed under `ignore`")
    shared = out["shared"]
    if shared and not out["own"]:
        blockers.append("other actions also write "
                        + "; ".join(f"`{u}` ({', '.join(os)})"
                                    for u, os in sorted(shared.items()))
                        + ", so a check could miss it changing mid-trace")
    elif shared:
        blockers.append("it also writes "
                        + ", ".join(f"`{u}`" for u in sorted(shared))
                        + ", which other actions write ("
                        + "; ".join(f"{u}: {', '.join(os)}"
                                    for u, os in sorted(shared.items()))
                        + "): drop that assignment from the env action, or "
                          "list it under `ignore` in its env_at_init entry to "
                          "leave it at init's value")
    reads = {r for r in action_read_units(ir, action)
             if not any(units_overlap(r, w) and len(r) >= len(w)
                        for w in writes[action])}
    if reads:
        blockers.append(f"it reads {', '.join(f'`{r}`' for r in sorted(reads))}, "
                        f"which it does not write: fixed at init it would read "
                        f"them before init set them")
    return out


def env_blockers(area, ir, action, ignore=()):
    """Why `action` cannot be fixed per trace by env_at_init, as sentences;
    empty when it can (see env_analysis)."""
    return env_analysis(area, ir, action, ignore)["blockers"]


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


CHECKED_VERDICTS = ("IMPOSSIBLE", "NO-OP")


def completeness(area, ir, actions, checks):
    """Per mirrored action, what makes it complete — the input of the
    coverage, explanation and verdict checks:

      reqs      the formal requirements on it (spec-probes' checks)
      verdicts  the @verdicts on it
      everywhere  the first requirement or verdict with no @while, which
                covers every step by itself (no coverage check needed)
      changes   the observable vars it may change (the model's own vars
                minus @internal ones, that the action assigns other than
                to themselves), each with the requirements whose @shall /
                @outcome / @old reads it, helpers included.
      open      its GAP and OUT-OF-SCOPE verdicts: where they apply,
                behavior is acknowledged as unspecified, so a change there
                is not reported again as unexplained.

    Whether a requirement reads a var is decided on its text and the
    model's declarations: `isActive(c)` reads `status` when isActive's body
    does. Words are never involved."""
    internal = {i.get("var") for i in area.get("internal_vars") or []}
    own = [v for v in ir.get("vars") or [] if v not in internal]
    mutations = ir.get("action_mutations") or {}
    try:
        decls = spec_source.declarations(Path(ir.get("file") or "").read_text(
            encoding="utf-8"))
    except OSError:
        decls = {}
    out = []
    for action in actions:
        reqs = [c for c in checks if c["form"] == "event" and c["action"] == action]
        verdicts = [v for v in area.get("verdicts") or []
                    if v.get("action") == action]
        everywhere = next(iter(
            [c["rid"] for c in reqs if not c["while"]]
             + [verdict_label(v) for v in verdicts if not (v.get("while") or "").strip()]),
            None)
        changes = []
        for var in own:
            if var not in (mutations.get(action) or []):
                continue
            explainers = []
            for c in reqs:
                if c["modality"] == "forbidden":
                    continue   # never fires, so it explains nothing
                text = " ".join([c["shall"] or ""]
                                + [o.get("predicate") or "" for o in c["outcomes"]]
                                + [o.get("expr") or "" for o in c["old"]])
                roots = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
                if var in spec_source.name_closure(decls, roots):
                    explainers.append(c)
            changes.append({"var": var, "explainers": explainers})
        open_ = [v for v in verdicts if v.get("verdict") in ("GAP", "OUT-OF-SCOPE")]
        out.append({"action": action, "reqs": reqs, "verdicts": verdicts,
                    "everywhere": everywhere, "changes": changes,
                    "open": open_})
    return out


def explained_everywhere(entry, change):
    """True when a requirement that reads the var, or a GAP / OUT-OF-SCOPE
    verdict, applies to every step of the action (has no @while): there is
    then nothing for an explanation check to find."""
    return (any(not c["while"] for c in change["explainers"])
            or any(not (v.get("while") or "").strip() for v in entry["open"]))


def completeness_vals(comp, meta=False):
    """(kind, val name, comment, body) per completeness check, in action
    order: coverage, then one explanation per watched var, then each checked
    verdict. Asserted positively, like an obligation: a counterexample is a
    gap, an unexplained change, or a verdict the model contradicts. With
    meta=True each tuple also carries {action, var?, index?}."""
    out = []

    def add(kind, name, comment, body, **m):
        out.append((kind, name, comment, body) + ((m,) if meta else ()))

    for e in comp:
        action = e["action"]
        ran = f'_lastAction == "{action}"'
        if e["everywhere"] is None:
            terms = ([pre_ghost(c["rid"]) for c in e["reqs"]]
                     + [verdict_ghost(v["action"], v["index"]) for v in e["verdicts"]])
            body = f'_lastAction != "{action}"'
            if terms:
                body += "\n      or " + "\n      or ".join(terms)
            covered = ", ".join([c["rid"] for c in e["reqs"]]
                                + [verdict_label(v) for v in e["verdicts"]])
            add("coverage", coverage_name(action),
                f"Coverage of {action}: every step is under "
                + (covered if covered else "NOTHING YET — no requirement "
                   "or verdict on this action"), body, action=action)
        for ch in e["changes"]:
            var, ex = ch["var"], ch["explainers"]
            if explained_everywhere(e, ch):
                continue   # a requirement or open verdict with no @while
            changed = f"{var} != {ghost_for_var(var)}"
            body = f"not({ran} and {changed})"
            terms = ([pre_ghost(c["rid"]) for c in ex]
                     + [verdict_ghost(v["action"], v["index"]) for v in e["open"]])
            if terms:
                body += "\n      or " + "\n      or ".join(terms)
            who = [c["rid"] for c in ex] + [verdict_label(v) for v in e["open"]]
            add("explanation", explanation_name(action, var),
                f"Explanation of {var} on {action}: "
                + (f"changed only where {', '.join(who)} applied"
                   if who else "NO REQUIREMENT on this action names it"),
                body, action=action, var=var)
        for v in e["verdicts"]:
            if v.get("verdict") not in CHECKED_VERDICTS:
                continue
            fired = [ran]
            if (v.get("while") or "").strip():
                fired.append(verdict_ghost(v["action"], v["index"]))
            if v["verdict"] == "IMPOSSIBLE":
                body = f"not({' and '.join(fired)})"
            else:
                same = [f"{ch['var']} == {ghost_for_var(ch['var'])}"
                        for ch in e["changes"]]
                body = (f"not({' and '.join(fired)})\n      or "
                        + (" and ".join(same) if same else "true"))
            add("verdict", verdict_name(action, v["index"]),
                f"Verdict {verdict_label(v)}: "
                + (v.get("reason") or "").replace("\n", " "), body,
                action=action, index=v["index"])
    return out


def checks_by_name(comp):
    """{probe val name: (check id, kind, label)} for every completeness
    check — what spec-record looks a val up by, and records it under."""
    out = {}
    for kind, name, comment, _body, meta in completeness_vals(comp, meta=True):
        cid = completeness_check_id(kind, meta["action"], meta.get("var"),
                                    meta.get("index"))
        out[name] = {"id": cid, "kind": kind, "action": meta["action"],
                     "var": meta.get("var"), "index": meta.get("index"),
                     "comment": comment}
    return out


def area_completeness(root, area):
    """(comp, checks_by_name) for an area with a Quint model, else
    ([], {}). Parses the model with the default IR engine."""
    from quint_ir import parse_qnt
    fm = area.get("formal_model") or {}
    rel = fm.get("quint_file")
    if not rel:
        return [], {}
    path = Path(root) / "specs" / rel
    if not path.exists():
        return [], {}
    ir = parse_qnt(path)
    if not ir:
        return [], {}
    comp = completeness(area, ir, step_actions(area, ir), requirement_checks(area))
    return comp, checks_by_name(comp)
