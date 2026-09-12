#!/usr/bin/env python3
"""
spec-readback.py — Deterministic readback generator.

The readback is the review surface humans trust, so it is RENDERED BY A
TOOL, not by an agent following a style guide: identical input produces
byte-identical output, every sentence is derived from a JSON field, and
the document cannot diverge from what the checker actually verified.
/spec-readback orchestrates this tool and adds nothing to the artifacts.

Subcommands:
  area <name>          → specs/<name>.readback.md
  change [slug]        → specs/changes/<slug>.readback.md + refreshed
                         per-target area readbacks (slug defaults to
                         last_change in .spec/local.json)
  project              → .spec/readback.md
  all                  → project + every area
  status <slug> --json → derived phase grid for the change (the /spec
                         dashboard's mechanical source — phases are never
                         stored, always computed)

Derived phase rules (single implementation, used by `change` and `status`):
  spec     = purpose present, ≥1 requirement, every non-raw REQ has ears
  checked  = check_results.ran_at ≥ last_modified, no counterexample /
             error / timeout among checks, and witness_status reports
             zero undischarged obligations
  applied  = every REQ/INV in the target's manifest ids[] has a
             traceability[] entry (contracts: n/a)
  verified = newest verification_log entry is a pass, not drifted, and
             dated ≥ last_modified (contracts: n/a)
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import (  # noqa: E402
    load_trace, mermaid_lines, render_value, state_vars,
    detect_action_var, witness_status, compute_model_sha,
    area_json_path, changes_dir, journeys_dir,
    skip_discharge, brief_status, action_params, GHOST_PARAM_RE,
    compute_spec_sha,
)
from quint_ir import _strip_noise  # noqa: E402

MAX_DIAGRAM_STEPS = 12
CHANGED_HINT = " — `spec-record changed <area>` says what has moved since."


def short_sha(sha, width=7):
    """First `width` chars of a sha, or an em dash when it is absent.

    A named helper rather than an inline conditional inside the f-string:
    the em dash has to be written as an escape to keep this file's source
    ASCII-safe, and a backslash inside an f-string expression is a
    SyntaxError before Python 3.12 (PEP 701). This tooling is still
    byte-compiled for 3.8."""
    return sha[:width] if sha else "—"

LEGEND = ("*Legend: ✓ verified — witness trace replayed green against real code · "
          "◐ witnessed — proven possible in the model, not yet demonstrated in code · "
          "✗ no witness — claimed behavior is UNREACHABLE in the model · "
          "⏳ not checked yet · "
          "⊘ skipped with justification (rejection-style requirement; an invariant carries the proof)*")


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_doc(path, lines):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {path}")


# ── Derivations ───────────────────────────────────────────────────────────────

def status_mark(req):
    w = req.get("witness") or {}
    if req.get("status") == "verified":
        return "✓"
    if w.get("status") == "skipped" and skip_discharge(w):
        return "⊘"
    if w.get("status") in ("no-witness", "hollow"):
        # Both are failures to demonstrate the behavior, so both render as
        # a cross. Which one it is comes from the Needs-Your-Attention
        # entry, where the fix differs: unreachable means look at the
        # guard, hollow means the predicate never distinguished before
        # from after.
        return "✗"
    if w.get("status") == "witnessed":
        return "◐"
    return "⏳"


def check_bound(area):
    """Effective Apalache step bound for this area's last check — the depth a
    bounded ✓ is valid to. None when not recorded (older runs / not yet run)."""
    b = (area.get("check_results") or {}).get("max_steps")
    return b if isinstance(b, int) and b > 0 else None


def check_scopes(area):
    """Per-invariant finite scope from the last Alloy run, keyed by ID. A
    structural ✓ is bounded by scope the way a bounded ✓ is bounded by depth,
    so the scope has to reach the renderer or the mark would overclaim."""
    out = {}
    for c in ((area.get("check_results") or {}).get("checks") or []):
        if c.get("backend") == "alloy" and c.get("id") and c.get("scope"):
            out[c["id"]] = c["scope"]
    return out


def check_backends(area):
    """ID -> which validator actually produced the last result for it. A
    liveness ✓ from TLC and a liveness ✓ from Apalache are bounded by
    different things — a finite state space vs a step depth — so the mark
    cannot be rendered without knowing which one ran."""
    out = {}
    for c in ((area.get("check_results") or {}).get("checks") or []):
        if c.get("id") and c.get("backend"):
            out[c["id"]] = c["backend"]
    return out


def assumption_index(area):
    """ID -> [ASM-NNN, ...] for everything an assumption bears on.

    A result that depends on an unstated assumption overclaims; one that
    depends on a STATED assumption should still not read as unconditional.
    So every mark whose ID appears in some assumptions[].affects carries the
    assumption with it \u2014 the same honesty rule as the step bound and the
    Alloy scope, applied to the environment rather than to the search."""
    out = {}
    for asm in area.get("assumptions", []) or []:
        aid = asm.get("id")
        if not aid:
            continue
        for target in asm.get("affects", []) or []:
            out.setdefault(target, []).append(aid)
    return {k: sorted(v) for k, v in out.items()}


def under_assumptions(mark, asms):
    """Qualify a mark with the assumptions it rests on. Only ✓-shaped marks
    are qualified: a counterexample or an unchecked item makes no claim that
    an assumption could be propping up."""
    if not asms or not mark.startswith("\u2713"):
        return mark
    return f"{mark} \u00b7 under {', '.join(asms)}"


def invariant_mark(inv, bound, scope=None):
    """Honest render of an invariant's formal status. A bounded model check is
    NOT a proof — it only says 'no counterexample within N steps' — so a
    bounded ✓ always carries its depth, distinct from an inductive proof and
    from the requirement ✓ (which means 'witness replayed green in code').
    A structural ✓ carries its Alloy scope for the same reason: it says 'no
    counterexample among structures this size', which is a different claim
    again."""
    st = inv.get("formal_status", "specified")
    if st == "verified-inductive":
        return "✓ proven"
    if st == "verified":
        return f"✓ (≤{bound} steps)" if bound else "✓ (bounded)"
    if st == "verified-in-scope":
        return f"✓ (scope: {scope})" if scope else "✓ (in scope)"
    if st == "verified-smt":
        return "✓ (smt)"
    if st == "not-checkable":
        return "⊘ not checkable by this backend"
    if st == "counterexample-found":
        return "✗"
    if st == "accepted-risk":
        return "⚠ accepted-risk"
    return "⏳"


def ears_sentence(req):
    """Render the EARS sentence from the fields (source of truth); fall back
    to description for unstructured requirements."""
    ears = req.get("ears")
    if not ears:
        return req.get("description") or "(no description)"
    parts = []
    if ears.get("feature"):
        parts.append(f"Where {ears['feature']}, ")
    if ears.get("state"):
        parts.append(f"While {ears['state']}, ")
    if ears.get("trigger"):
        joiner = "if" if ears.get("unwanted") else "when"
        parts.append(f"{joiner} {ears['trigger']}, ")
    shall = "then the system shall" if ears.get("unwanted") else "the system shall"
    parts.append(f"{shall} {ears.get('response', '…')}.")
    sentence = "".join(parts)
    return sentence[0].upper() + sentence[1:]


def resolve_constraints(sentence, constraints):
    """Append '(= value unit, CON-ID)' after each constraint name mentioned —
    the boundary number a reviewer must confirm, in place."""
    for con in constraints:
        name = con.get("name")
        if name and re.search(rf"\b{re.escape(name)}\b", sentence):
            unit = f" {con['unit']}" if con.get("unit") else ""
            sentence = re.sub(
                rf"\b{re.escape(name)}\b",
                f"{name} (= {con.get('value')}{unit}, {con.get('id')})",
                sentence, count=1)
    return sentence


def witness_one_liner(root, trace_rel):
    """'6 steps: login_failed(bob) ×5 → accountStatus = {bob: Locked}' —
    compressed action runs plus the final state delta."""
    trace, errs = load_trace(Path(root) / "specs" / trace_rel)
    if errs:
        return f"trace invalid: {errs[0]}"
    states = trace["states"]
    action_var = detect_action_var(trace)
    ghosts = [v for v in state_vars(trace) if GHOST_PARAM_RE.match(v)]

    labels = []
    for s in states[1:]:
        action = render_value(s.get(action_var)).strip('"') if action_var else "step"
        # Ghost vars OR quint's own mbt::nondetPicks record, depending on
        # which tool produced the trace — itf_tools.action_params knows both,
        # so an --mbt trace renders 'login(bob, s1)' like a probe trace does
        # instead of losing its arguments to a name this loop doesn't know.
        params = action_params(s, ghosts)
        labels.append(f"{action}({', '.join(params)})" if params else action)
    compressed = []
    for lbl in labels:
        if compressed and compressed[-1][0] == lbl:
            compressed[-1][1] += 1
        else:
            compressed.append([lbl, 1])
    seq = " → ".join(f"{l} ×{n}" if n > 1 else l for l, n in compressed)

    model_vars = [v for v in state_vars(trace)
                  if v != action_var and not v.startswith(("_last", "mbt::"))]
    final_delta = [
        f"{v} = {render_value(states[-1].get(v))}"
        for v in model_vars
        if len(states) > 1 and states[-2].get(v) != states[-1].get(v)
    ]
    out = f"{len(states)} steps: {seq}"
    if final_delta:
        out += " → " + "; ".join(final_delta)
    return out


def quint_excerpt(root, area, quint_ref):
    """(lines, start_line, end_line) of `action <quint_ref>` in the sidecar —
    brace-matched on noise-stripped text, excerpt sliced from the raw text so
    the reviewer sees it verbatim, with file:line for trust-but-verify."""
    fm = area.get("formal_model") or {}
    qnt_rel = fm.get("quint_file") or f"{area.get('area')}.qnt"
    qnt_path = Path(root) / "specs" / qnt_rel
    if not qnt_path.exists():
        return None, None, None, qnt_rel
    raw = qnt_path.read_text(encoding="utf-8")
    clean = _strip_noise(raw)
    m = re.search(rf"^[ \t]*action[ \t]+{re.escape(quint_ref)}\b", clean, re.MULTILINE)
    if not m:
        return None, None, None, qnt_rel
    brace = clean.find("{", m.start())
    eq = clean.find("=", m.end())
    if brace == -1 or (eq != -1 and eq < brace):
        # `action f = all { ... }` — start matching from the first brace after '='
        brace = clean.find("{", eq if eq != -1 else m.end())
    if brace == -1:
        return None, None, None, qnt_rel
    depth, i = 0, brace
    while i < len(clean):
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    start_line = clean.count("\n", 0, m.start()) + 1
    end_line = clean.count("\n", 0, i) + 1
    excerpt = "\n".join(raw.splitlines()[start_line - 1:end_line])
    return excerpt, start_line, end_line, qnt_rel


def landed_changes_for_id(root, area_name, item_id):
    """Slugs of LANDED change manifests whose target ids include this id —
    the per-ID changelog (answers 'why is it 7 — which change did that')."""
    out = []
    cdir = changes_dir(root)
    if not cdir.exists():
        return out
    for p in sorted(cdir.glob("*.change.json")):
        d = load_json(p)
        if not isinstance(d, dict) or d.get("status") != "landed":
            continue
        for t in d.get("targets", []) or []:
            if t.get("name") == area_name and item_id in (t.get("ids") or []):
                out.append(d.get("change") or p.name[:-len(".change.json")])
    return out


def referenced_by(con, area):
    name = con.get("name")
    if not name:
        return []
    refs = []
    pat = re.compile(rf"\b{re.escape(name)}\b")
    for r in area.get("requirements", []) or []:
        text = (r.get("description") or "") + " " + json.dumps(r.get("ears") or {})
        if pat.search(text):
            refs.append(r.get("id"))
    for inv in area.get("invariants", []) or []:
        if pat.search(inv.get("description") or ""):
            refs.append(inv.get("id"))
    return [r for r in refs if r]


def load_journeys(root):
    out = []
    jdir = journeys_dir(root)
    if not jdir.exists():
        return out
    for p in sorted(jdir.glob("*.journey.json")):
        d = load_json(p)
        if isinstance(d, dict):
            d["_file"] = p.name[:-len(".journey.json")]
            out.append(d)
    return out


def attention_items(root, area_name, area):
    """The Needs-Your-Attention list — every entry derived from a field."""
    items = []
    req_by_id = {r.get("id"): r for r in area.get("requirements", []) or []}
    constraints = area.get("constraints", []) or []
    cr = area.get("check_results") or {}
    for c in cr.get("checks") or []:
        if c.get("result") == "counterexample":
            nl = (c.get("counterexample") or {}).get("nl_explanation")
            items.append(f"**Counterexample** — {c.get('id')} (`{c.get('quint_name')}`): "
                         + (nl or f"violated; trace at `specs/{c.get('trace')}`"))
        elif c.get("result") in ("timeout", "error"):
            items.append(f"**Check {c.get('result')}** — {c.get('id')} (`{c.get('quint_name')}`)"
                         + (f": {c.get('error')}" if c.get("error") else ""))
    rows, _missing, _ok = witness_status(root, area_name, area)
    for rid, st, _trace, detail in rows:
        if st in ("not-run", "no-witness", "hollow", "HOLLOW", "STALE",
                  "UNSTAMPED", "UNVERIFIABLE",
                  "MISSING-FILE", "INVALID", "SKIPPED-UNJUSTIFIED"):
            hollow_label = ("HOLLOW witness — the predicate already held "
                            "before anything happened; add witness.delta")
            label = {"not-run": "Unchecked requirement",
                     "no-witness": "UNREACHABLE behavior (no witness)",
                     "hollow": hollow_label,
                     "HOLLOW": hollow_label}.get(st, f"Witness {st}")
            # Render the EARS sentence inline — a reviewer shouldn't have to
            # scroll to learn what 'UI-001' is.
            req = req_by_id.get(rid)
            sentence = resolve_constraints(ears_sentence(req), constraints) if req else ""
            line = f"**{label}** — {rid}" + (f": {sentence}" if sentence else "")
            if detail:
                line += f" _({detail})_" if sentence else f": {detail}"
            items.append(line)
    matrix = cr.get("matrix")
    if matrix:
        if matrix.get("uncovered", 0) > 0:
            items.append(f"**Coverage gaps** — {matrix['uncovered']} untriaged state×event "
                         f"cell(s); run /spec-check to triage.")
        if matrix.get("gaps"):
            items.append(f"**Open coverage GAPs** — {', '.join(matrix['gaps'])} "
                         f"(spec is silent on triaged-real cells).")
    elif area.get("state_machines"):
        items.append("**Coverage unknown** — state machines declared but the "
                     "state×event matrix has never been recorded (run "
                     "`tools/spec-matrix.py` with `--record`).")
    outcomes = (area.get("check_results") or {}).get("outcomes") or {}
    if outcomes.get("uncovered"):
        items.append(f"**Unhandled failure behavior** — {outcomes['uncovered']} declared "
                     f"external outcome(s) have no requirement saying what happens and no "
                     f"triage saying why not. Run `tools/spec-matrix.py <area> --outcomes`.")
    extraction = (area.get("check_results") or {}).get("extraction") or {}
    if extraction_state(area)[0] == "unaudited":
        items.append(
            "**Code never audited** \u2014 this area describes code, and no "
            "extraction audit has been recorded against it. Every completeness "
            "number here measures the spec against itself; this is the only "
            "one that measures it against the implementation, so until it runs "
            "the spec could be missing behavior entirely and every other mark "
            "would still be green. Run `tools/spec-extract-audit.py <area> "
            "--record`.")
    if extraction.get("unclaimed"):
        items.append(f"**Unaccounted code** \u2014 {extraction['unclaimed']} decision site(s) "
                     f"in the implementation that no spec element claims and no triage "
                     f"explains. These are what a regenerated implementation gets wrong.")
    differential = (area.get("check_results") or {}).get("differential") or {}
    if differential.get("result") == "diverged":
        items.append(f"**Not substitutable** \u2014 {differential.get('divergences', 'some')} "
                     f"divergence(s) between the original and the regenerated "
                     f"implementation. Either the spec is missing something, or the "
                     f"difference is intentional and undeclared.")
    for asm in area.get("assumptions", []) or []:
        if asm.get("status", "accepted") == "open":
            items.append(f"**Open assumption** — {asm.get('id')}: {asm.get('statement')} "
                         f"(undecided: results that depend on it are provisional)")
    for q in area.get("open_questions", []) or []:
        if q.get("status", "open") in ("open", "deferred"):
            src = f" _(source: {q['source']})_" if q.get("source") else ""
            items.append(f"**Open question** — {q.get('id')}: {q.get('question')}{src}")
    log = area.get("verification_log") or []
    if log and log[-1].get("drift_detected"):
        items.append("**Drift** — spec-traced code changed outside /spec-code-generate and "
                     "verification fails. Revert the code or codify via /spec.")
    return items


# ── Section renderers (area) ─────────────────────────────────────────────────

def area_has_code(area):
    """Does this area claim to describe code that exists?

    Decided from the area alone, because the verdict and the header bar are
    functions of the area alone. Three signals, any of which means an
    extraction audit has something to run against: traceability entries
    naming files, a triage ledger (someone has already looked at sites), or a
    requirement carrying extraction evidence (brownfield capture).

    The distinction this exists to draw: an area with no code has nothing to
    audit and must render `n/a`, while an area WITH code and no audit must
    render a warning. Collapsing those two into one blank is the defect \u2014
    "unknown" and "fine" have to look different or nobody looks."""
    for t in area.get("traceability") or []:
        if (t.get("code") or "").strip():
            return True
    if area.get("extraction_triage"):
        return True
    for req in area.get("requirements") or []:
        if ((req.get("extraction") or {}).get("evidence") or "").strip():
            return True
    return False


def extraction_state(area):
    """(state, detail) for every surface that renders extraction coverage,
    so the verdict, the header bar, the attention list and the dimension
    grid cannot disagree about it.

    state is 'n/a' (no code to audit), 'unaudited' (code, no recorded run),
    'unclaimed' (sites nothing accounts for) or 'clean'."""
    stats = (area.get("check_results") or {}).get("extraction") or {}
    if not stats:
        if not area_has_code(area):
            return "n/a", "n/a (no code)"
        return "unaudited", ("not audited \u2014 run `tools/spec-extract-audit.py "
                             "<area> --record`")
    sites = stats.get("sites", 0)
    unclaimed = stats.get("unclaimed", 0)
    accounted = sites - unclaimed
    if unclaimed:
        return "unclaimed", (f"{accounted}/{sites} sites accounted, "
                             f"{unclaimed} unclaimed")
    return "clean", f"{accounted}/{sites} sites accounted"


def ship_verdict(area):
    """One-line go/no-go above the stats bar — a reviewer should know ship-
    readiness at a glance, not by doing arithmetic on the header. READY means
    every behavior is verified against code, every invariant holds, and nothing
    is open. Deterministic, like everything else in the readback."""
    reqs = [r for r in area.get("requirements", []) or [] if r.get("status") != "deferred"]
    invs = area.get("invariants", []) or []
    if not reqs and not invs:
        return "**⏳ EMPTY** — no requirements or invariants captured yet."
    blockers = []
    n_unver = sum(1 for r in reqs if r.get("status") != "verified")
    if n_unver:
        blockers.append(f"{n_unver} of {len(reqs)} requirement(s) not verified against code")
    n_inv_bad = sum(1 for i in invs
                    if i.get("formal_status") not in ("verified", "verified-inductive",
                                                      "verified-in-scope", "verified-smt"))
    if n_inv_bad:
        blockers.append(f"{n_inv_bad} of {len(invs)} invariant(s) not holding")
    n_q = sum(1 for q in area.get("open_questions", []) or []
              if q.get("status", "open") == "open")
    if n_q:
        blockers.append(f"{n_q} open question(s)")
    log = area.get("verification_log") or []
    if log and log[-1].get("drift_detected"):
        blockers.append("drift detected")
    # The only check that runs code -> spec, so the only one that can find
    # behavior the spec never mentions. It belongs next to unverified
    # requirements and open questions, not in terminal scrollback.
    ex_state, _ex_detail = extraction_state(area)
    if ex_state == "unclaimed":
        stats = (area.get("check_results") or {}).get("extraction") or {}
        blockers.append(f"{stats.get('unclaimed', 0)} of {stats.get('sites', 0)} "
                        f"code site(s) unaccounted")
    elif ex_state == "unaudited":
        blockers.append("code never audited against the spec "
                        "(`spec-extract-audit --record`)")
    outcomes = (area.get("check_results") or {}).get("outcomes") or {}
    if outcomes.get("uncovered"):
        blockers.append(f"{outcomes['uncovered']} external outcome(s) with no "
                        f"declared behavior")
    if blockers:
        return "**⚠ NOT READY** — " + "; ".join(blockers) + "."
    # "Closed" is only ever closed relative to a boundary. Saying which one
    # keeps READY from reading as a claim about everything.
    scope = area.get("scope") or {}
    if scope.get("included"):
        # The verdict is a one-line glance. A long scope list belongs in the
        # Scope section, not inlined here where it would bury the verdict.
        inc = scope["included"]
        joined = ", ".join(inc)
        rel = (" — scope: " + joined if len(joined) <= 80
               else f" — scope: {len(inc)} declared area(s), see Scope")
    elif scope.get("excluded"):
        rel = " — relative to the declared scope"
    else:
        rel = " — NOTE: no scope declared, so 'complete' has no boundary to be complete against"
    return ("**✓ READY** — all requirements verified against code, all invariants "
            "hold (bounded, in scope, or proven), no open questions" + rel + ".")


def header_bar(area, area_name):
    reqs = [r for r in area.get("requirements", []) or [] if r.get("status") != "deferred"]
    n_ver = sum(1 for r in reqs if r.get("status") == "verified")
    n_wit = sum(1 for r in reqs if (r.get("witness") or {}).get("status") == "witnessed")
    invs = area.get("invariants", []) or []
    n_inv_proven = sum(1 for i in invs if i.get("formal_status") == "verified-inductive")
    n_inv_bounded = sum(1 for i in invs if i.get("formal_status") == "verified")
    n_inv_scoped = sum(1 for i in invs if i.get("formal_status") == "verified-in-scope")
    bound = check_bound(area)
    matrix = (area.get("check_results") or {}).get("matrix")
    if matrix:
        cov = (f"{matrix.get('cells', 0)} cells, {matrix.get('covered', 0)} covered, "
               f"{matrix.get('triaged', 0)} triaged, {matrix.get('uncovered', 0)} untriaged")
    else:
        cov = "matrix not run"
    n_q = sum(1 for q in area.get("open_questions", []) or []
              if q.get("status", "open") == "open")
    log = area.get("verification_log") or []
    last_ver = log[-1]["date"][:10] if log else "never"
    bsuffix = f" (≤{bound})" if bound else ""
    inv_cell = f"{n_inv_proven} proven + {n_inv_bounded} bounded{bsuffix} / {len(invs)}"
    if n_inv_scoped:
        # Only shown when structural checks exist, so the bar of an
        # Apalache-only area renders exactly as it did before.
        inv_cell = (f"{n_inv_proven} proven + {n_inv_bounded} bounded{bsuffix} "
                    f"+ {n_inv_scoped} in-scope / {len(invs)}")
    return (f"**Status:** {area.get('status', 'raw')}  |  "
            f"**Requirements:** {n_ver}/{len(reqs)} verified, {n_wit}/{len(reqs)} witnessed  |  "
            f"**Invariants:** {inv_cell}  |  "
            f"**Coverage:** {cov}  |  "
            f"**Extraction:** {extraction_state(area)[1]}  |  "
            f"**Open questions:** {n_q}  |  "
            f"**Last verified:** {last_ver}")


def render_requirement(root, area, req, constraints, rendered_full):
    """Markdown block for one requirement. rendered_full: set of ids already
    fully rendered (later occurrences become one-line links)."""
    rid = req.get("id", "?")
    lines = []
    if rid in rendered_full:
        return [f"- {rid} — see above.", ""]
    rendered_full.add(rid)
    mark = status_mark(req)
    tag = "  *(failure path)*" if (req.get("ears") or {}).get("unwanted") else ""
    lines.append(f"#### {rid}")
    lines.append("")
    sentence = resolve_constraints(ears_sentence(req), constraints)
    lines.append(f"{mark}{tag} {sentence}")
    lines.append("")
    prov = req.get("extraction") or {}
    if prov.get("evidence"):
        conf = prov.get("confidence")
        # A low-confidence extraction is a guess about ambiguous code. Saying so
        # is the difference between a reviewable draft and a plausible-looking
        # assertion.
        flag = " \u26a0 **low confidence** \u2014 the code was ambiguous here" \
            if conf == "low" else (f" ({conf} confidence)" if conf else "")
        lines.append(f"> _Extracted from `{prov['evidence']}`{flag}._")
        lines.append("")
    if req.get("type") == "non-functional":
        fc = req.get("fit_criterion") or {}
        lines.append(f"> **Fit:** {fc.get('metric', '?')} — {fc.get('target', '?')} — "
                     f"measured by {fc.get('measurement', '?')}")
        lines.append("")
        return lines
    w = req.get("witness") or {}
    modality = req.get("modality", "must")
    if modality == "forbidden":
        enforced = w.get("enforced_by")
        lines.append("> **Forbidden** \u2014 this must never happen, so there is no trace to "
                     "find. The proof is the invariant that stays true"
                     + (f": **{enforced}**." if enforced else " (none named yet)."))
        lines.append("")
    elif modality == "may":
        lines.append("> **Permitted, not required** — the system MAY do any of the "
                     "following, and the choice is not this spec's to make. Each needs "
                     "its own witness, or the permission has quietly become a rule:")
        for oc in (w.get("outcomes") or []):
            ocm = {"witnessed": "◐", "no-witness": "✗",
                   "hollow": "✗"}.get(oc.get("status"), "⏳")
            trace = ""
            if oc.get("trace") and oc.get("status") == "witnessed":
                trace = f" — {witness_one_liner(root, oc['trace'])}"
            lines.append(f">   - {ocm} **{oc.get('name', '?')}**: "
                         f"`{oc.get('predicate', '?')}`{trace}")
        if not (w.get("outcomes") or []):
            lines.append(">   - _no permitted outcomes declared yet_")
        lines.append("")
    if req.get("determinism") == "nondeterministic" and modality != "may":
        lines.append("> **Nondeterministic** — more than one result is legal here by "
                     "design; do not read the witness as the only allowed outcome.")
        lines.append("")
    for eo in req.get("error_outcomes", []) or []:
        idem = " (safe to retry)" if eo.get("idempotent") else ""
        lines.append(f"> **On {eo.get('external')}/{eo.get('outcome')}:** "
                     f"{eo.get('effect')}{idem}")
    if req.get("error_outcomes"):
        lines.append("")
    refusal = req.get("refusal") or {}
    if refusal:
        mark = {"passing": "\u2713", "failing": "\u2717"}.get(refusal.get("status"), "\u23f3")
        unchanged = ", ".join(f"`{v}`" for v in (refusal.get("unchanged") or []))
        lines.append(f"> **Refusal check:** {mark} `{refusal.get('artifact', '—')}` "
                     f"\u2014 drives the code into "
                     f"{refusal.get('blocking_state') or 'the blocking state'}, attempts "
                     f"the call, asserts it is refused"
                     + (f" and that {unchanged} did not move." if unchanged else "."))
        lines.append("> _Replay cannot cover this: a rejection has no trace to replay._")
        lines.append("")
    if w.get("status") == "skipped" and w.get("justification"):
        lines.append(f"> **Witness skipped:** {w['justification']}")
        lines.append("")
    elif w.get("trace") and w.get("status") == "witnessed":
        lines.append(f"> **Witness:** {witness_one_liner(root, w['trace'])}")
        delta = (w.get("delta") or {}).get("pre")
        if delta:
            lines.append(f"> **Starting from:** `{delta}` \u2014 the step had to move the "
                         f"state, not merely find it already there.")
        lines.append("")
    details = []
    qref = req.get("quint_ref")
    if qref:
        excerpt, ln1, ln2, qnt_rel = quint_excerpt(root, area, qref)
        sha = compute_model_sha(root, area.get("area"), area)
        pin = f" · model `{sha[:12]}`" if sha else ""
        if excerpt:
            details.append(f"`specs/{qnt_rel}:L{ln1}-L{ln2}`{pin}")
            details.append("")
            details.append("```quint")
            details.append(excerpt)
            details.append("```")
        else:
            details.append(f"_action `{qref}` not found in `specs/{qnt_rel}`_")
        details.append("")
    if w.get("predicate"):
        details.append(f"**Witness predicate:** `{w['predicate']}` — true exactly when "
                       f"the behavior has happened.")
        details.append("")
    trace_rel = w.get("trace")
    if trace_rel and w.get("status") == "witnessed":
        trace, errs = load_trace(Path(root) / "specs" / trace_rel)
        if not errs:
            if len(trace["states"]) <= MAX_DIAGRAM_STEPS:
                details.append("```mermaid")
                details.extend(mermaid_lines(trace, title=f"{rid} witness"))
                details.append("```")
            else:
                details.append(f"_trace too long to diagram — see `specs/{trace_rel}`_")
            details.append("")
    if details:
        summary = f"Quint action `{qref}`, witness predicate + trace" if qref \
            else "Witness predicate + trace"
        lines.append(f"<details><summary>{summary}</summary>")
        lines.append("")
        lines.extend(details)
        lines.append("</details>")
        lines.append("")
    return lines


def what_the_system_does(root, area_name, area, journeys):
    lines = ["## What the System Does", ""]
    constraints = area.get("constraints", []) or []
    reqs = {r.get("id"): r for r in area.get("requirements", []) or []
            if r.get("id") and r.get("status") != "deferred"}
    rendered_full = set()
    touching = [j for j in journeys
                if any(s.get("ref", "").startswith(f"{area_name}.")
                       for s in j.get("steps", []) or [])]
    for j in touching:
        gloss = " — ".join(filter(None, [j.get("actor"), j.get("description")]))
        lines.append(f"### {j.get('name', j['_file'])}" + (f" — *{gloss}*" if gloss else ""))
        lines.append("")
        for idx, step in enumerate(j.get("steps", []) or [], 1):
            ref = step.get("ref", "")
            if "." not in ref:
                continue
            a, rid = ref.split(".", 1)
            note = f" — {step['note']}" if step.get("note") else ""
            if a != area_name:
                lines.append(f"*(step {idx}: [{ref}]({a}.readback.md#{rid.lower()})"
                             f"{note} — see [{a}]({a}.readback.md))*")
                lines.append("")
            elif rid in reqs:
                if step.get("note") and rid not in rendered_full:
                    lines.append(f"*step {idx}{note}:*")
                    lines.append("")
                lines.extend(render_requirement(root, area, reqs[rid], constraints,
                                                rendered_full))
            else:
                lines.append(f"*(step {idx}: {ref} — requirement not found)*")
                lines.append("")
    leftover = [r for rid, r in reqs.items() if rid not in rendered_full]
    if leftover:
        if touching:
            lines.append("### Other behaviors")
            lines.append("")
        for r in sorted(leftover, key=lambda x: x.get("id", "")):
            lines.extend(render_requirement(root, area, r, constraints, rendered_full))
    if not touching and reqs:
        lines.insert(2, "_No journeys reference this area. Run `/spec _journeys/<name>` "
                        "to group requirements into flows._")
        lines.insert(3, "")
    return lines


def invariants_section(area):
    invs = area.get("invariants", []) or []
    if not invs:
        return []
    bound = check_bound(area)
    scopes = check_scopes(area)
    asms = assumption_index(area)
    legend = ("_Invariant legend: ✓ proven — inductive, holds in ALL reachable states · "
              "✓ (≤N steps) — bounded model check to depth N; no counterexample found within N, "
              "NOT a proof · ✗ counterexample · ⚠ accepted-risk · ⏳ not checked. Upgrade a "
              "bounded ✓ by raising the bound or marking the invariant `proof: inductive`._")
    if scopes:
        legend = legend[:-1] + (" · ✓ (scope: …) — Alloy found no counterexample among "
                                "structures that size; outside that scope it was never "
                                "checked, so widen the scope in the .als to strengthen it._")
    lines = ["## What Must Always Be True", "", legend, ""]
    for inv in sorted(invs, key=lambda i: i.get("id", "")):
        st = inv.get("formal_status", "specified")
        mark = invariant_mark(inv, bound, scopes.get(inv.get("id")))
        tail = " — see Needs Your Attention." if st == "counterexample-found" else ""
        # A structural invariant has no Quint name — it points at the Alloy
        # check that carries it, so the reader can find the actual assertion.
        mark = under_assumptions(mark, asms.get(inv.get("id")))
        name = (inv.get("quint_name") or inv.get("alloy_command")
                or inv.get("smt_file") or "—")
        # A transition property is checked against the probe module, where
        # the pre-state is a variable. Same bound, different module — and a
        # reader comparing two ✓ marks should be able to tell which is which.
        over = " _(transition property, checked over the probe module)_"             if (inv.get("over") or "model") == "probes" else ""
        lines.append(f"- **{inv.get('id')}** (`{name}`) — "
                     f"{inv.get('description', '')} Criticality: "
                     f"{inv.get('criticality', 'high')}. {mark}{tail}{over}")
    lines.append("")
    props = area.get("properties", []) or []
    if props:
        backends = check_backends(area)
        lines.append("## What Must Eventually Happen")
        lines.append("")
        for p in sorted(props, key=lambda i: i.get("id", "")):
            st = p.get("formal_status", "specified")
            # A liveness ✓ is only readable next to the checker that produced
            # it: TLC enumerates the model's whole finite state space (no step
            # bound), Apalache checks temporal properties up to max_steps and
            # only partially. Rendering both as a bare "✓ (bounded)" would
            # understate one and overstate the other.
            if st == "verified":
                mark = "✓ (TLC)" if backends.get(p.get("id")) == "tlc" else "✓ (bounded)"
            elif st == "counterexample-found":
                mark = "✗"
            else:
                mark = "⏳"
            lines.append(f"- **{p.get('id')}** (`{p.get('quint_name', '—')}`) — "
                         f"{p.get('description', '')} {mark}")
        lines.append("")
        if any(backends.get(p.get("id")) == "tlc" for p in props):
            lines.append("_Liveness legend: ✓ (TLC) — explicit-state check over this "
                         "model's whole finite state space: no step bound, but no larger "
                         "instance than the model declares either, and TLC writes no "
                         "counterexample trace, so a ✗ here has no diagram to show. "
                         "✓ (bounded) — checked by Apalache up to the configured step "
                         "limit only._")
        else:
            lines.append("_Liveness results are bounded — Apalache proves them up to the "
                         "configured step limit only._")
        lines.append("")
    return lines


def paired_note(con):
    """A numeric bound is only two-sided when something checks the other side."""
    paired = con.get("paired_invariant")
    return f" Bounded below by **{paired}**." if paired else ""


def limits_section(root, area_name, area):
    cons = area.get("constraints", []) or []
    if not cons:
        return []
    lines = ["## Limits and Bounds", "",
             "| ID | Name | Value | Unit | What it is | Referenced by | History |",
             "|---|---|---|---|---|---|---|"]
    for con in sorted(cons, key=lambda c: c.get("id", "")):
        refs = ", ".join(referenced_by(con, area)) or "—"
        hist = ", ".join(landed_changes_for_id(root, area_name, con.get("id"))) or "—"
        lines.append(f"| {con.get('id')} | `{con.get('name')}` | {con.get('value')} | "
                     f"{con.get('unit', '—')} | {con.get('description', '—')} | {refs} | {hist} |")
    lines.append("")
    lines.append("_Confirm every number AND its boundary semantics (on the Nth, or after N?) "
                 "— off-by-one is the classic wrong-rule bug; the witness one-liners above "
                 "show the machine-found count._")
    lines.append("")
    return lines


def state_machines_section(area):
    machines = area.get("state_machines", []) or []
    if not machines:
        return []
    lines = ["## State Machines", ""]
    for sm in machines:
        lines.append(f"### {sm.get('entity')}")
        lines.append("")
        lines.append("```mermaid")
        lines.append("stateDiagram-v2")
        init = sm.get("initial_state")
        lifecycle = sm.get("lifecycle_actions") or []
        if init:
            label = f": {', '.join(lifecycle)}" if lifecycle else ""
            lines.append(f"  [*] --> {init}{label}")
        terminal = {s.get("name") for s in sm.get("states", []) or [] if s.get("terminal")}
        for t in sm.get("transitions", []) or []:
            actor = f" ({t['actor']})" if t.get("actor") else ""
            lines.append(f"  {t.get('from')} --> {t.get('to')}: {t.get('trigger')}{actor}")
        for s in sorted(terminal):
            lines.append(f"  {s} --> [*]")
        lines.append("```")
        lines.append("")
    return lines


def ui_sections(area):
    screens = area.get("screens", []) or []
    if not screens:
        return []
    lines = ["## Navigation", "", "```mermaid", "graph TB"]
    for nav in area.get("navigation", []) or []:
        trig = nav.get("trigger", "").replace("|", "/")
        lines.append(f"  {nav.get('from')} --> |{trig}| {nav.get('to')}")
    gated = [s["name"] for s in screens if s.get("auth_required")]
    if gated:
        lines.append("  classDef auth_required fill:#fff5b1")
        lines.append(f"  class {','.join(gated)} auth_required")
    lines.append("```")
    lines.append("")
    lines.append("## Screens")
    lines.append("")
    lines.append("| Screen | Auth required | Purpose | Components |")
    lines.append("|---|---|---|---|")
    for s in screens:
        comps = ", ".join(s.get("components") or []) or "—"
        lines.append(f"| {s.get('name')} | {'yes' if s.get('auth_required') else 'no'} | "
                     f"{s.get('purpose', '—')} | {comps} |")
    lines.append("")
    ui_comps = area.get("ui_components", []) or []
    if ui_comps:
        lines.append("## UI Components")
        lines.append("")
        for c in ui_comps:
            bits = []
            if c.get("fields"):
                bits.append(f"fields: {', '.join(c['fields'])}")
            if c.get("states"):
                bits.append(f"states: {', '.join(c['states'])}")
            if c.get("visible_when"):
                bits.append(f"visible when: {c['visible_when']}")
            lines.append(f"- **{c.get('name')}**" + (f" — {'; '.join(bits)}" if bits else ""))
        lines.append("")
    return lines


def reference_section(root, area, project):
    lines = ["## Reference", "",
             "<details><summary>Concepts, architecture, decisions, resolved questions, "
             "traceability, verification history</summary>", ""]
    concepts = area.get("concepts") or {}
    ents = concepts.get("entities") or []
    if ents:
        # One per line. Each entity carries a name, a state set AND a prose
        # description, and the descriptions are the long part — joined with
        # "·" they run together into a paragraph whose separator is invisible
        # and in which no single entity can be found by scanning.
        lines.append("**Entities:**")
        lines.append("")
        for e in ents:
            states = f" ({' / '.join(e['states'])})" if e.get("states") else ""
            desc = e.get("description", "")
            lines.append(f"- **{e.get('name')}**{states}"
                         + (f" — {desc}" if desc else ""))
        lines.append("")
    if concepts.get("actors"):
        lines.append(f"**Actors:** {', '.join(concepts['actors'])}")
        lines.append("")
    proj_arch = (project or {}).get("architecture") or {}
    area_arch = area.get("architecture") or {}
    merged = dict(proj_arch) if area_arch.get("inherits_project", True) else {}
    for k, v in area_arch.items():
        if k == "inherits_project":
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        elif isinstance(v, list) and isinstance(merged.get(k), list):
            merged[k] = sorted(set(merged[k]) | set(map(str, v))) if all(
                isinstance(x, str) for x in v + merged[k]) else v
        else:
            merged[k] = v
    if merged:
        bits = []
        stack = merged.get("stack") or {}
        if stack:
            bits.append(" ".join(str(v) for v in stack.values()))
        pers = merged.get("persistence") or {}
        if pers:
            bits.append("persistence: " + " ".join(str(v) for v in pers.values()))
        if merged.get("patterns"):
            bits.append("patterns: " + ", ".join(map(str, merged["patterns"])))
        if merged.get("protocols"):
            bits.append("protocols: " + ", ".join(map(str, merged["protocols"])))
        if bits:
            lines.append("**Architecture (resolved project ⊕ area):** " + " · ".join(bits))
            lines.append("")
        comps = (area_arch.get("components") or [])
        if comps:
            comp_strs = [f"**{c.get('name')}** ({c.get('role')}) implements "
                         f"{', '.join(c.get('implements') or []) or '—'}" for c in comps]
            lines.append("**Components:** " + " · ".join(comp_strs))
            lines.append("")
    decs = area.get("decisions", []) or []
    if decs:
        lines.append("**Decisions:**")
        lines.append("")
        for d in sorted(decs, key=lambda x: x.get("id", "")):
            when = f", {d['decided_at'][:10]}" if d.get("decided_at") else ""
            alts = "; ".join(
                f"{a.get('option')} (rejected: {a.get('reason_rejected', '—')})"
                for a in d.get("alternatives_considered") or [])
            lines.append(f"- **{d.get('id')}** ({d.get('status')}{when}) — {d.get('title')}. "
                         f"{d.get('decision', '')} Rationale: {d.get('rationale', '—')}"
                         + (f" Alternatives: {alts}." if alts else ""))
        lines.append("")
    resolved = [q for q in area.get("open_questions", []) or []
                if q.get("status") == "resolved"]
    if resolved:
        lines.append("**Resolved questions:**")
        lines.append("")
        for q in sorted(resolved, key=lambda x: x.get("id", "")):
            when = f" ({q['resolved_at'][:10]})" if q.get("resolved_at") else ""
            lines.append(f"- {q.get('id')}{when}: {q.get('question')} — "
                         f"**{q.get('resolution', '—')}**")
        lines.append("")
    trace = area.get("traceability", []) or []
    if trace:
        lines.append("**Traceability:**")
        lines.append("")
        lines.append("| ID | Quint | Component | Code | Verified |")
        lines.append("|---|---|---|---|---|")
        for t in sorted(trace, key=lambda x: x.get("id", "")):
            lines.append(f"| {t.get('id')} | `{t.get('quint', '—')}` | "
                         f"{t.get('component') or '—'} | `{t.get('code', '—')}` | "
                         f"{'✓' if t.get('verified') else '✗'} |")
        lines.append("")
    else:
        lines.append("_No code generated yet. Run /spec-code-generate._")
        lines.append("")
    # Provenance before history: "what was this built from" is the question
    # a reader has before "when was it last checked", and the two are
    # different facts. An absent block reads as unknown, never as current —
    # so it is rendered as absent rather than omitted.
    gen = area.get("generated_from") or {}
    ext = area.get("extracted_from") or {}
    if gen or ext:
        lines.append("**Provenance:**")
        lines.append("")
        if ext:
            ext_sha = short_sha(ext.get("code_sha"))
            lines.append(
                f"- Spec read from code @ `{ext_sha}`"
                + (f" ({ext['code_repo']})" if ext.get("code_repo") else "")
                + (f", subtree `{ext['code_path']}`" if ext.get("code_path") else "")
                + (f" on {ext['date'][:10]}" if ext.get("date") else "")
                + CHANGED_HINT)
        if gen:
            claims = gen.get("spec_content_sha")
            current = compute_spec_sha(area)
            moved = bool(claims and current and claims != current)
            gen_sha = short_sha(gen.get("spec_sha"))
            now = current[:7] if current else "?"
            stale = (f"  \u2014 \u26a0 the spec's claims have moved since (now "
                     f"`{now}`): this code predates the current requirements."
                     ) if moved else ""
            lines.append(
                f"- Code generated from spec @ `{gen_sha}`"
                + (f", claims @ `{claims[:7]}`" if claims else "")
                + (f", landing in code @ `{gen['code_sha'][:7]}`"
                   if gen.get("code_sha") else "")
                + (f" on {gen['date'][:10]}" if gen.get("date") else "")
                + stale)
        lines.append("")
    log = area.get("verification_log") or []
    if log:
        lines.append("**Verification history (last 5):**")
        lines.append("")
        for e in log[-5:]:
            lines.append(f"- `{e.get('date', '')[:19]}`: {e.get('status', '?').upper()} | "
                         f"{e.get('summary', '')} | spec @ {(e.get('spec_sha') or '—')[:7]} "
                         f"⇄ code @ {(e.get('code_sha') or '—')[:7]} | "
                         f"drift: {'yes' if e.get('drift_detected') else 'no'}")
        lines.append("")
    lines.append("_Approval lives in PR history — audit trail: "
                 f"`git log --follow {area_json_path(root, area.get('area') or '').relative_to(root).as_posix()}`_")
    lines.append("")
    lines.append("</details>")
    return lines


BRIEF_FACETS = [
    ("how_it_fits", "How it fits together"),
    ("why_this_way", "Why it is this way"),
    ("watch_out_for", "What to watch out for"),
]


def brief_section(area):
    """The one authored section in a generated document.

    Rendered verbatim from `brief` in the area JSON — never composed here —
    so identical input still yields byte-identical output. Marked as prose so
    a reader never mistakes it for something the checker established, and
    marked STALE the moment its pin no longer matches the spec: an
    orientation written for a previous version of the area is worse than none,
    because it reads with the same authority as the derived sections below it.
    """
    brief = area.get("brief") or {}
    text = (brief.get("text") or "").strip()
    if not text:
        return []
    state, detail = brief_status(area)
    lines = ["## In Brief", ""]
    if state == "stale":
        lines += [
            f"> **⚠ This brief may be out of date** — {detail}. Everything below "
            f"it is regenerated from the spec and is current; this section is not.",
            "",
        ]
    lines += [text, ""]
    for key, heading in BRIEF_FACETS:
        value = (brief.get(key) or "").strip()
        if value:
            lines += [f"**{heading}.** {value}", ""]
    who = brief.get("author")
    pin = "unpinned" if state == "stale" and not brief.get("written_against") else detail
    lines += [
        f"*Written by {who or 'unknown'}; prose, not machine-checked. "
        f"Spec pin: `{pin}`. Every section below is derived from the spec itself.*",
        "",
    ]
    return lines


def at_a_glance(area):
    """A one-screen index of the requirements before the per-REQ detail.

    The detail sections are complete but flat: a reviewer meets Quint
    excerpts and witness predicates before knowing how many requirements
    there are or which ones are in trouble. This is the layer between.
    """
    reqs = [r for r in area.get("requirements", []) or []
            if isinstance(r, dict) and r.get("status") != "deferred"]
    if len(reqs) < 2:
        return []
    lines = ["## At a Glance", "",
             "| | ID | Behavior | Modality |", "|---|---|---|---|"]
    for r in reqs:
        rid = r.get("id", "?")
        response = ((r.get("ears") or {}).get("response") or "").strip()
        if len(response) > 88:
            response = response[:87].rstrip() + "…"
        modality = r.get("modality") or "must"
        anchor = rid.lower()
        lines.append(f"| {status_mark(r)} | [{rid}](#{anchor}) | {response or '—'} | {modality} |")
    lines.append("")
    return lines


def shape_diagram(area):
    """The area's shape in one picture: entities with their state counts, the
    externals it depends on, and the areas it spans. Derived entirely from
    declared fields — no layout choices that could vary between runs."""
    entities = [e for e in ((area.get("concepts") or {}).get("entities") or [])
                if isinstance(e, dict) and e.get("name")]
    externals = [x for x in (area.get("externals") or [])
                 if isinstance(x, dict) and x.get("name")]
    spans = [x for x in (area.get("spans") or []) if x]
    if not entities and not externals:
        return []
    name = area.get("area", "area")
    lines = ["## Shape", "", "```mermaid", "flowchart LR"]
    lines.append(f'    subgraph AREA["{name}"]')
    if entities:
        for e in entities:
            states = e.get("states") or []
            label = e["name"] if not states else f"{e['name']}<br/>{len(states)} states"
            closed = " ▪ closed" if e.get("closed") else ""
            lines.append(f'        E_{_slug(e["name"])}["{label}{closed}"]')
    else:
        lines.append(f'        E_none["(no entities declared)"]')
    lines.append("    end")
    for x in externals:
        outcomes = x.get("outcomes") or []
        suffix = f"<br/>{len(outcomes)} outcomes" if outcomes else ""
        lines.append(f'    X_{_slug(x["name"])}(["{x["name"]}{suffix}"])')
        lines.append(f'    AREA --> X_{_slug(x["name"])}')
    for sp in spans:
        lines.append(f'    S_{_slug(sp)}[["{sp}"]]')
        lines.append(f'    S_{_slug(sp)} --> AREA')
    lines += ["```", ""]
    if externals:
        lines += ["*Rounded nodes are outside systems this area depends on; "
                  "each declared outcome is a cell the coverage matrix requires "
                  "an answer for.*", ""]
    return lines


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))


# A comma-joined list stops being a list the moment one of its items contains
# a comma: "create, update, soft delete" is ONE scope item that reads as
# three, and the reader has no way to tell where one ends. Length matters too,
# but ambiguity is the real failure — so the comma test alone forces bullets,
# at any length.
INLINE_MAX_ITEMS = 3
INLINE_MAX_CHARS = 72


def render_list(label, items, code=False, trailing=""):
    """A labelled list, inline when that stays readable and bulleted when it
    does not. Returns markdown lines (with the trailing blank), or [] for an
    empty list.

    Bullets whenever any item contains a comma (the separator would be
    ambiguous), there are more than INLINE_MAX_ITEMS of them, or the joined
    line would run past INLINE_MAX_CHARS. Deterministic in the items alone,
    so identical input still produces identical output — the readback is
    reviewed as a diff, and a layout that drifted with anything else would
    make that diff unreadable."""
    items = [str(i) for i in (items or []) if str(i).strip()]
    if not items:
        return []
    shown = [f"`{i}`" for i in items] if code else items
    joined = ", ".join(shown)
    if (len(items) <= INLINE_MAX_ITEMS
            and len(joined) <= INLINE_MAX_CHARS
            and not any("," in i for i in items)):
        return [f"**{label}:** {joined}{trailing}", ""]
    out = [f"**{label}:**" + (f" {trailing.strip()}" if trailing.strip() else ""), ""]
    out += [f"- {i}" for i in shown]
    out.append("")
    return out


def scope_section(area):
    """Gap A. Printed before anything claims completeness, because every such
    claim below is relative to this boundary."""
    scope = area.get("scope") or {}
    inc, exc = scope.get("included") or [], scope.get("excluded") or []
    if not inc and not exc:
        return []
    lines = ["## Scope", ""]
    if inc:
        lines += render_list("In scope", inc)
    if exc:
        lines += ["**Deliberately out of scope** — these are decisions, not oversights:", ""]
        for e in exc:
            owner = f" _(owned by {e['owner']})_" if e.get("owner") else ""
            lines.append(f"- **{e.get('item')}** — {e.get('reason', '')}{owner}")
        lines.append("")
    return lines


def boundary_section(area):
    """What a replacement would have to preserve. Printed next to Scope
    because the two answer different questions people routinely conflate:
    scope says what this area is responsible for, boundary says what 'the
    same behavior' means."""
    b = area.get("boundary") or {}
    if not b:
        return []
    lines = ["## What a Replacement Must Preserve", ""]
    lines += render_list("Entry points", b.get("entry_points"), code=True)
    lines += render_list("Observable state (what the differential comparator diffs)",
                         b.get("observable_state"), code=True)
    lines += render_list("Emits", b.get("emits"))
    if b.get("persistence_contract"):
        lines += ["**Persistence:** " + b["persistence_contract"], ""]
    lines += render_list("Deliberately free", b.get("free"),
                         trailing=" \u2014 a replacement may do these differently.")
    diff = (area.get("check_results") or {}).get("differential") or {}
    if diff:
        result = diff.get("result")
        if result == "equivalent-in-sequences":
            lines += [f"**Differential:** \u2713 no sequence out of "
                      f"{diff.get('sequences', '?')} distinguished the regenerated "
                      f"implementation from the original. That is the absence of a "
                      f"counterexample at this budget, not equivalence.", ""]
        elif result == "diverged":
            lines += [f"**Differential:** \u2717 {diff.get('divergences', 'some')} "
                      f"divergence(s) over {diff.get('sequences', '?')} sequences \u2014 "
                      f"each is a missing spec element or an undeclared intentional "
                      f"difference.", ""]
        else:
            lines += [f"**Differential:** \u23f3 {result}.", ""]
    return lines


def extraction_section(area):
    """The code the spec does not describe. Only rendered for an area that has
    a ledger \u2014 which in practice means one extracted from existing code."""
    rows = area.get("extraction_triage", []) or []
    stats = (area.get("check_results") or {}).get("extraction") or {}
    if not rows and not stats:
        return []
    lines = ["## What the Code Does That the Spec Does Not", "",
             "_The only check here that runs code \u2192 spec. A branch nobody wrote down "
             "cannot be regenerated, cannot be witnessed, and cannot be missed by any "
             "spec-shaped check \u2014 because nothing spec-shaped knows it exists._", ""]
    if stats:
        lines += [f"**{stats.get('mapped', 0)} mapped · {stats.get('triaged', 0)} triaged "
                  f"· {stats.get('unclaimed', 0)} unclaimed** of {stats.get('sites', 0)} "
                  f"decision site(s).", ""]
    buckets = {}
    for row in rows:
        buckets.setdefault(row.get("verdict", "?"), []).append(row)
    for verdict in ("GAP", "DEAD", "OUT-OF-SCOPE", "DEFENSIVE", "NOT-BEHAVIOR"):
        hits = buckets.get(verdict) or []
        if not hits:
            continue
        lines.append(f"**{verdict}** ({len(hits)})")
        lines.append("")
        for row in hits[:12]:
            where = f"`{row.get('file')}:{row.get('line', '?')}`"
            tail = f" \u2014 {row.get('question')}" if row.get("question") else ""
            lines.append(f"- {where} {row.get('reason', '')}{tail}")
        if len(hits) > 12:
            lines.append(f"- _\u2026 and {len(hits) - 12} more_")
        lines.append("")
    mapped = buckets.get("MAPPED") or []
    if mapped:
        lines += [f"_{len(mapped)} site(s) MAPPED to spec elements \u2014 listed in the "
                  f"ledger, not repeated here._", ""]
    return lines


def externals_section(area):
    """Gap B + E. One row per outcome the outside world can produce, with what
    this area does about it. The unhappy rows are the point."""
    externals = area.get("externals", []) or []
    assumptions = area.get("assumptions", []) or []
    if not externals and not assumptions:
        return []
    lines = ["## What the Outside World Can Do", ""]
    if externals:
        handled = {}
        for req in area.get("requirements", []) or []:
            for eo in req.get("error_outcomes", []) or []:
                handled.setdefault((eo.get("external"), eo.get("outcome")), []).append(
                    (req.get("id"), eo.get("effect"), eo.get("idempotent")))
        triage = {(t.get("external"), t.get("outcome")): t
                  for t in area.get("outcome_triage", []) or []}
        lines += ["| Dependency | Outcome | What we do | Where |", "|---|---|---|---|"]
        for ext in externals:
            for oc in ext.get("outcomes", []) or []:
                key = (ext.get("name"), oc.get("name"))
                rows = handled.get(key)
                if rows:
                    effect = "; ".join(
                        f"{eff}{' (retry-safe)' if idem else ''}" for _, eff, idem in rows)
                    where = ", ".join(rid for rid, _, _ in rows)
                elif key in triage:
                    t = triage[key]
                    effect = f"_{t.get('verdict')}_ — {t.get('reason', '')}"
                    where = t.get("question") or t.get("scope_ref") or "—"
                else:
                    effect = "**⚠ nothing specified**"
                    where = "—"
                lines.append(f"| {ext.get('name')} | {oc.get('name')} | {effect} | {where} |")
        lines.append("")
    if assumptions:
        lines += ["**Assumptions this specification rests on.** Results that depend on "
                  "them are annotated `under ASM-NNN` \u2014 they hold if the assumption "
                  "does, and not otherwise.", ""]
        for asm in sorted(assumptions, key=lambda a: a.get("id", "")):
            state = asm.get("status", "accepted")
            badge = {"accepted": "", "open": " **(OPEN \u2014 undecided)**",
                     "retired": " _(retired)_"}.get(state, "")
            disch = (asm.get("discharged_by") or "").rstrip()
            checked = (f" Checked in production by: {disch}"
                       + ("" if disch.endswith((".", "!", "?")) else ".")
                       if disch else " _Believed, not checked._")
            affects = (" Bears on: " + ", ".join(asm["affects"]) + "."
                       if asm.get("affects") else "")
            lines.append(f"- **{asm.get('id')}**{badge} — {asm.get('statement')}"
                         f"{affects}{checked}")
        lines.append("")
    return lines


def examples_section(area):
    """Gap I. Concrete cases the author wrote down. Seeds and regressions \u2014
    the proof of reachability is still the machine-found witness trace."""
    examples = area.get("examples", []) or []
    if not examples:
        return []
    lines = ["## Worked Examples", "",
             "_Author-written scenarios: illustration and regression pins, not proof. "
             "A witness trace shows a behavior is reachable at all; an example only "
             "asserts the one case somebody thought of._", ""]
    for ex in sorted(examples, key=lambda e: e.get("id", "")):
        title = ex.get("title") or ex.get("id")
        lines.append(f"**{ex.get('id')} — {title}**")
        lines.append("")
        given = ex.get("given") or {}
        if given:
            lines.append("- Given: " + ", ".join(f"`{k}` = `{v}`" for k, v in given.items()))
        when = ex.get("when") or {}
        args = when.get("args") or {}
        arglist = ", ".join(f"{k}={v}" for k, v in args.items())
        lines.append(f"- When: `{when.get('action', '?')}({arglist})`")
        expect = ex.get("expect") or {}
        if expect:
            lines.append("- Expect: " + ", ".join(f"`{k}` = `{v}`" for k, v in expect.items()))
        if ex.get("refs"):
            lines.append("- Illustrates: " + ", ".join(ex["refs"]))
        lines.append("")
    return lines


def dimensions_section(area):
    """Gap J. Completeness is multidimensional, so it is reported per dimension
    with the obligations behind each \u2014 never as one number. Every row is
    derived from what the spec actually declares; a dash means the dimension
    has no inputs yet, which is itself information."""
    reqs = [r for r in area.get("requirements", []) or [] if r.get("status") != "deferred"]
    invs = area.get("invariants", []) or []
    cr = area.get("check_results") or {}
    matrix, outcomes = cr.get("matrix") or {}, cr.get("outcomes") or {}
    extraction = cr.get("extraction") or {}
    differential = cr.get("differential") or {}
    entities = ((area.get("concepts") or {}).get("entities") or [])
    externals = area.get("externals", []) or []
    assumptions = area.get("assumptions", []) or []
    examples = area.get("examples", []) or []
    props = area.get("properties", []) or []
    unwanted = [r for r in reqs if (r.get("ears") or {}).get("unwanted")]
    rejections = [r for r in reqs
                  if r.get("modality") == "forbidden"
                  or ((r.get("witness") or {}).get("status") == "skipped"
                      and (r.get("witness") or {}).get("justification"))]
    witnessed = [r for r in reqs
                 if (r.get("witness") or {}).get("status") in ("witnessed", "skipped")]
    closed = [e for e in entities if e.get("closed")]
    all_redteam = [q for q in area.get("open_questions", []) or []
                   if str(q.get("source", "")).startswith("red-team")]
    open_redteam = [q for q in all_redteam if q.get("status", "open") == "open"]
    stateful = [e for e in entities if e.get("states")]
    n_inv_ok = sum(1 for i in invs
                   if i.get("formal_status") in ("verified", "verified-inductive",
                                                 "verified-in-scope", "verified-smt",
                                                 "accepted-risk"))

    def row(name, ok, detail):
        mark = "\u2713" if ok is True else ("!" if ok is False else "\u2014")
        return f"| {name} | {mark} | {detail} |"

    # A dimension is ✓ only when its obligations are discharged, ! only when
    # something is outstanding, and — when nothing has been declared to
    # measure. An open world is a legitimate choice, not a defect; a half-
    # pinned one (some entities closed, some not) is the signal worth raising.
    if not stateful:
        data_model = None
    elif len(closed) == len(stateful):
        data_model = True
    elif closed:
        data_model = False
    else:
        data_model = None

    rows = [
        row("Data model", data_model,
            f"{len(entities)} entities, {len(stateful)} stateful, {len(closed)} closed"
            if entities else "no entities declared"),
        row("State space",
            (matrix.get("uncovered") == 0) if matrix else None,
            f"{matrix.get('covered', 0)}/{matrix.get('cells', 0)} cells covered, "
            f"{matrix.get('triaged', 0)} triaged, {matrix.get('uncovered', 0)} untriaged"
            if matrix else "matrix not run"),
        row("Operations",
            (len(witnessed) == len(reqs)) if reqs else None,
            f"{len(witnessed)}/{len(reqs)} requirements with a discharged witness"
            if reqs else "no requirements"),
        # Unwanted-path requirements are evidence somebody thought about
        # failure, not evidence the failure space is covered. Without the
        # outcome matrix this dimension is unmeasured, never discharged.
        row("Failure behavior",
            (outcomes.get("uncovered") == 0) if outcomes else None,
            f"{outcomes.get('covered', 0)}/{outcomes.get('cells', 0)} external outcomes "
            f"handled, {outcomes.get('uncovered', 0)} unspecified"
            if outcomes else f"{len(unwanted)} unwanted-path requirement(s); no external "
                             f"outcome matrix \u2014 unmeasured"),
        row("External systems",
            bool(externals) or None,
            f"{len(externals)} declared, "
            f"{sum(len(e.get('outcomes') or []) for e in externals)} outcomes"
            if externals else "none declared \u2014 if the area calls anything, this is a gap"),
        row("Assumptions",
            all(a.get("status", "accepted") != "open" for a in assumptions) if assumptions
            else None,
            f"{len(assumptions)} recorded, "
            f"{sum(1 for a in assumptions if a.get('status') == 'open')} open"
            if assumptions else "none recorded"),
        row("Temporal behavior", bool(props) or None,
            f"{len(props)} liveness propert(ies)" if props else "none declared"),
        # `\u2014` means "nothing declared to measure" everywhere else in this
        # grid, so it may only appear here when there is genuinely no code.
        # An area WITH code and no audit is outstanding work: `!`.
        row("Extraction coverage",
            {"clean": True, "unclaimed": False, "unaudited": False,
             "n/a": None}[extraction_state(area)[0]],
            f"{extraction.get('mapped', 0)} mapped, {extraction.get('triaged', 0)} "
            f"triaged, {extraction.get('unclaimed', 0)} unclaimed of "
            f"{extraction.get('sites', 0)} site(s)"
            if extraction else extraction_state(area)[1]),
        row("Substitutability",
            (differential.get("result") == "equivalent-in-sequences")
            if differential.get("result") in ("equivalent-in-sequences", "diverged")
            else None,
            f"{differential.get('divergences', '?')} divergence(s) over "
            f"{differential.get('sequences', '?')} sequences"
            if differential.get("result") in ("equivalent-in-sequences", "diverged")
            else "not measured \u2014 needs a parallel build and `spec-record equiv`"),
        row("Refusal coverage",
            all((r.get("refusal") or {}).get("status") == "passing" for r in rejections)
            if rejections else None,
            f"{sum(1 for r in rejections if (r.get('refusal') or {}).get('artifact'))}"
            f"/{len(rejections)} rejection(s) with an artifact, "
            f"{sum(1 for r in rejections if (r.get('refusal') or {}).get('status') == 'passing')}"
            f" passing"
            if rejections else "no rejection requirements"),
        row("Examples", bool(examples) or None,
            f"{len(examples)} worked example(s)" if examples else "none written"),
        row("Invariants", (n_inv_ok == len(invs)) if invs else None,
            f"{n_inv_ok}/{len(invs)} holding" if invs else "none declared"),
        # Never run and run-clean are different states; only the second is ✓.
        row("Adversarial review", (not open_redteam) if all_redteam else None,
            f"{len(open_redteam)} open of {len(all_redteam)} red-team question(s)"
            if all_redteam else "never run \u2014 `/spec-check --reality`"),
    ]
    return ["## Completeness by Dimension", "",
            "_One number would hide which half is missing. Each row is derived from "
            "declared obligations: \u2713 discharged, ! outstanding, \u2014 nothing declared "
            "(which may itself be the gap)._", "",
            "| Dimension | | Obligations |", "|---|---|---|"] + rows + [""]


def emit_area(root, area_name):
    area_path = area_json_path(root, area_name)
    area = load_json(area_path)
    if area is None:
        sys.exit(f"ERROR: specs/{area_name}.area.json (or .contract.json) not found")
    project = load_json(Path(root) / ".spec" / "project.json") or {}
    journeys = load_journeys(root)

    title = "Contract Readback" if area.get("kind") == "contract" else "Spec Readback"
    lines = [f"# {title}: {area_name} — v{area.get('version', '?')}", "",
             f"> Auto-generated by `tools/spec-readback.py` from `specs/{area_path.name}`. "
             f"Do not edit; regenerate after spec changes.", ""]
    if area.get("kind") == "contract":
        lines.append(f"**Spans:** {', '.join(area.get('spans') or [])}")
        lines.append("")
    lines.append(ship_verdict(area))
    lines.append("")
    lines.append(header_bar(area, area_name))
    lines.append("")
    lines.append(LEGEND)
    lines.append("")
    if area.get("purpose"):
        lines += ["## Purpose", "", area["purpose"], ""]
    # Authored orientation first, then the picture, then the index, then the
    # machine detail: a reader descends to the depth they need instead of
    # meeting Quint excerpts on the way in.
    lines += brief_section(area)
    lines += shape_diagram(area)
    lines += scope_section(area)
    lines += boundary_section(area)
    items = attention_items(root, area_name, area)
    lines.append("## ⚠ Needs Your Attention")
    lines.append("")
    if items:
        lines += [f"- {i}" for i in items]
    else:
        lines.append("**Nothing needs attention.**")
    lines.append("")
    lines += ui_sections(area)
    lines += at_a_glance(area)
    lines += what_the_system_does(root, area_name, area, journeys)
    lines += invariants_section(area)
    lines += externals_section(area)
    lines += examples_section(area)
    lines += extraction_section(area)
    lines += limits_section(root, area_name, area)
    if not area.get("screens"):
        lines += state_machines_section(area)
    lines += dimensions_section(area)
    lines += reference_section(root, area, project)
    write_doc(Path(root) / "specs" / f"{area_name}.readback.md", lines)


# ── Derived phases / change / project ─────────────────────────────────────────

def derive_phases(root, target, area):
    """The single implementation of the derived phase grid."""
    is_contract = area.get("kind") == "contract"
    reqs = [r for r in area.get("requirements", []) or [] if r.get("status") != "deferred"]
    spec_ok = bool(area.get("purpose")) and (bool(reqs) or is_contract) and all(
        r.get("ears") or r.get("status") == "raw" or r.get("type") == "non-functional"
        for r in reqs)
    cr = area.get("check_results") or {}
    _rows, missing, _d = witness_status(root, area.get("area"), area)
    results_bad = any(c.get("result") in ("counterexample", "error", "timeout")
                      for c in cr.get("checks") or [])
    checked = bool(cr.get("ran_at")) and not results_bad and missing == 0 and \
        cr.get("ran_at", "") >= (area.get("last_modified") or "")
    if is_contract:
        return {"spec": "complete" if spec_ok else "draft",
                "checked": checked, "applied": None, "verified": None}
    traced = {t.get("id") for t in area.get("traceability", []) or []}
    ids = [i for i in (target.get("ids") or []) if i.startswith(("REQ", "UI", "INV"))]
    if not ids:
        ids = [r.get("id") for r in reqs if r.get("id")]
    applied = bool(traced) and all(i in traced for i in ids)
    log = area.get("verification_log") or []
    last = log[-1] if log else {}
    verified = (last.get("status") == "pass" and not last.get("drift_detected")
                and last.get("date", "") >= (area.get("last_modified") or ""))
    return {"spec": "complete" if spec_ok else "draft",
            "checked": checked, "applied": applied, "verified": verified}


def phase_cell(v):
    if v is None:
        return "n/a"
    if isinstance(v, str):
        return v
    return "✓" if v else "✗"


def load_change(root, slug):
    if not slug:
        local = load_json(Path(root) / ".spec" / "local.json") or {}
        slug = local.get("last_change")
        if not slug:
            sys.exit("ERROR: no slug given and no last_change in .spec/local.json")
    manifest = load_json(changes_dir(root) / f"{slug}.change.json")
    if manifest is None:
        sys.exit(f"ERROR: specs/changes/{slug}.change.json not found")
    return slug, manifest


def change_grid(root, manifest):
    grid = []
    for t in manifest.get("targets", []) or []:
        area = load_json(area_json_path(root, t.get("name")))
        if area is None:
            grid.append((t, None, None))
            continue
        grid.append((t, area, derive_phases(root, t, area)))
    return grid


def cmd_status(args):
    root = Path(args.root)
    slug, manifest = load_change(root, args.slug)
    grid = change_grid(root, manifest)
    out = {
        "change": slug,
        "intent": manifest.get("intent"),
        "status": manifest.get("status"),
        "targets": [
            {"name": t.get("name"), "kind": t.get("kind"),
             "auto": bool(t.get("auto")), "ids": t.get("ids") or [],
             "phases": ph if ph else "MISSING-SPEC"}
            for t, _a, ph in grid
        ],
    }
    print(json.dumps(out, indent=2))


def what_changed_section(root, since, targets):
    """Gap F. The semantic diff, rendered into the PR's review surface.

    OPT-IN via --since, and deliberately so: the readback's guarantee is that
    identical input yields byte-identical output, and a diff depends on a
    second input — the revision being compared against. Naming that revision
    keeps the guarantee (same input + same ref = same bytes) instead of
    quietly making the document depend on ambient repo state."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "spec_diff", Path(__file__).parent / "spec-diff.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:                      # tool missing or unloadable
        return ["## What Changed", "",
                f"_Semantic diff unavailable: {exc}_", ""]
    reports = mod.build_reports(since, mod.WORKTREE, Path(root))
    if targets:
        reports = {k: v for k, v in reports.items() if k in targets}
    return mod.render(reports, True, since, mod.WORKTREE)


def emit_change(root, slug, since=None):
    slug, manifest = load_change(root, slug)
    grid = change_grid(root, manifest)
    lines = [f"# Change Readback: {slug}", "",
             "> Auto-generated by `tools/spec-readback.py`. Do not edit.", "",
             f"**Intent:** {manifest.get('intent', '—')}  |  "
             f"**Status:** {manifest.get('status', '—')}"
             + (f"  |  **Branch:** `{manifest.get('branch')}`" if manifest.get("branch") else ""),
             "", "## Targets", "",
             "| Target | Kind | IDs | Spec | Checked | Applied | Verified |",
             "|---|---|---|---|---|---|---|"]
    for t, _area, ph in grid:
        name = t.get("name")
        kind = t.get("kind", "area") + (" (auto)" if t.get("auto") else "")
        ids = ", ".join(t.get("ids") or []) or "—"
        if ph is None:
            lines.append(f"| {name} | {kind} | {ids} | MISSING | — | — | — |")
        else:
            lines.append(f"| {name} | {kind} | {ids} | {phase_cell(ph['spec'])} | "
                         f"{phase_cell(ph['checked'])} | {phase_cell(ph['applied'])} | "
                         f"{phase_cell(ph['verified'])} |")
    lines.append("")
    lines.append("_(Phase columns are derived from the area JSONs at generation time — "
                 "the manifest stores membership only.)_")
    lines.append("")
    if since:
        targets = {t.get("name") for t, _a, _p in grid if t.get("name")}
        lines += what_changed_section(root, since, targets)

    # Attention roll-up scoped to the change's targets — the PR surface must
    # show a broken invariant even when the phase row looks half-green.
    attn = []
    for t, area, _ph in grid:
        if area is None:
            attn.append(f"**{t.get('name')}** — specs/{t.get('name')}.area.json "
                        f"(or .contract.json) MISSING")
            continue
        for item in attention_items(root, t.get("name"), area):
            attn.append(f"**{t.get('name')}** — {item}")
    lines.append("## ⚠ Needs Your Attention")
    lines.append("")
    lines += ([f"- {a}" for a in attn] if attn else ["**Nothing needs attention.**"])
    lines.append("")

    lines.append("## What This Change Does")
    lines.append("")
    for t, area, _ph in grid:
        if area is None:
            continue
        name = t.get("name")
        constraints = area.get("constraints", []) or []
        by_id = {}
        for ln, key in (("requirements", "req"), ("invariants", "inv"),
                        ("constraints", "con"), ("decisions", "dec"),
                        ("properties", "prop")):
            for item in area.get(ln, []) or []:
                if item.get("id"):
                    by_id[item["id"]] = (key, item)
        ids = t.get("ids") or []
        if not ids:
            continue
        lines.append(f"### {name}")
        lines.append("")
        for iid in ids:
            kind_item = by_id.get(iid)
            anchor = f"{name}.readback.md#{iid.lower()}"
            if kind_item is None:
                lines.append(f"- {iid} — _not found in {name}'s spec_")
                continue
            key, item = kind_item
            if key == "req":
                mark = status_mark(item)
                sent = resolve_constraints(ears_sentence(item), constraints)
                lines.append(f"- {mark} **[{iid}]({anchor})** — {sent}")
                qref = item.get("quint_ref")
                w = item.get("witness") or {}
                sub = []
                if qref:
                    excerpt, ln1, ln2, qnt_rel = quint_excerpt(root, area, qref)
                    if excerpt:
                        sub.append(f"  <details><summary>Quint `{qref}` "
                                   f"(`specs/{qnt_rel}:L{ln1}-L{ln2}`) + predicate</summary>")
                        sub.append("")
                        sub.append("  ```quint")
                        sub.extend("  " + l for l in excerpt.splitlines())
                        sub.append("  ```")
                        if w.get("predicate"):
                            sub.append(f"  **Witness predicate:** `{w['predicate']}`")
                        sub.append("")
                        sub.append("  </details>")
                lines.extend(sub)
            elif key == "inv":
                mark = invariant_mark(item, check_bound(area))
                lines.append(f"- {mark} **{iid}** — {item.get('description', '')}")
            elif key == "con":
                lines.append(f"- **{iid}** — `{item.get('name')}` = {item.get('value')}"
                             + (f" {item['unit']}" if item.get("unit") else ""))
            elif key == "dec":
                lines.append(f"- **{iid}** ({item.get('status')}) — {item.get('title')}")
            else:
                lines.append(f"- **{iid}** — {item.get('description', '')}")
        lines.append("")

    oq = manifest.get("open_questions") or []
    lines.append("## Open Questions Blocking")
    lines.append("")
    lines += ([f"- {q}" for q in oq] if oq else ["None."])
    lines.append("")
    lines.append("_Content changes per target: diff the per-area readbacks — "
                 "`git diff specs/<area>.readback.md` IS the content review._")
    write_doc(changes_dir(root) / f"{slug}.readback.md", lines)
    # Refresh the touched areas' own readbacks — the change doc links into them.
    for t, area, _ph in grid:
        if area is not None:
            emit_area(root, t.get("name"))


def emit_project(root):
    project = load_json(Path(root) / ".spec" / "project.json")
    if project is None:
        sys.exit("ERROR: .spec/project.json not found")
    areas = [a.get("name") for a in project.get("areas", []) or [] if a.get("name")]
    loaded = {a: load_json(area_json_path(root, a)) for a in areas}
    journeys = load_journeys(root)

    lines = [f"# Project Readback: {project.get('project', '?')}", "",
             "> Auto-generated by `tools/spec-readback.py`. Do not edit.", ""]
    attn = []
    for a, data in loaded.items():
        if data is None:
            attn.append(f"**{a}** — specs/{a}.area.json (or .contract.json) missing")
            continue
        items = attention_items(root, a, data)
        if items:
            attn.append(f"**{a}** — {len(items)} item(s): " +
                        "; ".join(i.split("—")[0].strip("* ") for i in items[:4]) +
                        (" …" if len(items) > 4 else "") +
                        f" — see [{a}](./specs/{a}.readback.md)")
    lines.append("## ⚠ Needs Your Attention")
    lines.append("")
    lines += ([f"- {a}" for a in attn] if attn else ["**Nothing needs attention.**"])
    lines.append("")
    lines.append("## Areas")
    lines.append("")
    lines.append("| Area | Kind | Version | Status | Last verified |")
    lines.append("|---|---|---|---|---|")
    for a in areas:
        d = loaded.get(a)
        if d is None:
            lines.append(f"| {a} | ? | ? | MISSING | — |")
            continue
        log = d.get("verification_log") or []
        last = (log[-1]["date"][:10] + (" ✓" if log[-1].get("status") == "pass" else " ✗")) \
            if log else "never"
        lines.append(f"| [{a}](./specs/{a}.readback.md) | {d.get('kind')} | "
                     f"{d.get('version')} | {d.get('status')} | {last} |")
    lines.append("")
    if journeys:
        lines.append("## Journeys")
        lines.append("")
        for j in journeys:
            gloss = " — ".join(filter(None, [j.get("actor"), j.get("description")]))
            lines.append(f"### {j.get('name', j['_file'])}" + (f" — *{gloss}*" if gloss else ""))
            lines.append("")
            lines.append("| # | Step | Area | Status |")
            lines.append("|---|---|---|---|")
            for idx, step in enumerate(j.get("steps", []) or [], 1):
                ref = step.get("ref", "")
                if "." not in ref:
                    continue
                a, rid = ref.split(".", 1)
                d = loaded.get(a)
                req = next((r for r in (d or {}).get("requirements", []) or []
                            if r.get("id") == rid), None)
                mark = status_mark(req) if req else "?"
                sent = ears_sentence(req) if req else "_(not found)_"
                note = f" — {step['note']}" if step.get("note") else ""
                lines.append(f"| {idx} | [{rid}](./specs/{a}.readback.md#{rid.lower()}) — "
                             f"{sent}{note} | {a} | {mark} |")
            lines.append("")
    write_doc(Path(root) / ".spec" / "readback.md", lines)


def main():
    p = argparse.ArgumentParser(description="Deterministic readback generator.")
    sub = p.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("area", help="Per-area readback.")
    pa.add_argument("name")
    pa.add_argument("--root", default=".")
    pa.set_defaults(func=lambda a: emit_area(Path(a.root), a.name))

    pc = sub.add_parser("change", help="Change readback + refreshed target readbacks.")
    pc.add_argument("slug", nargs="?")
    pc.add_argument("--root", default=".")
    pc.add_argument("--since", metavar="REF",
                    help="Include a 'What Changed' section: the semantic diff of this "
                         "change's targets against REF (a tag, sha, or branch). Naming "
                         "the ref keeps the output reproducible.")
    pc.set_defaults(func=lambda a: emit_change(Path(a.root), a.slug, a.since))

    pp = sub.add_parser("project", help="Project-wide readback.")
    pp.add_argument("--root", default=".")
    pp.set_defaults(func=lambda a: emit_project(Path(a.root)))

    pl = sub.add_parser("all", help="Project readback + every area.")
    pl.add_argument("--root", default=".")

    def _all(a):
        root = Path(a.root)
        project = load_json(root / ".spec" / "project.json") or {}
        emit_project(root)
        for ar in project.get("areas", []) or []:
            if ar.get("name"):
                emit_area(root, ar["name"])
    pl.set_defaults(func=_all)

    ps = sub.add_parser("status", help="Derived phase grid for a change (JSON).")
    ps.add_argument("slug", nargs="?")
    ps.add_argument("--root", default=".")
    ps.add_argument("--json", dest="emit_json", action="store_true")  # always JSON; kept for symmetry
    ps.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
