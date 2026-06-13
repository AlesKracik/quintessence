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
)
from quint_ir import _strip_noise  # noqa: E402

GHOST_PARAM_RE = re.compile(r"^_last(?!Action$)")
MAX_DIAGRAM_STEPS = 12

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
    if w.get("status") == "skipped" and w.get("justification"):
        return "⊘"
    if w.get("status") == "no-witness":
        return "✗"
    if w.get("status") == "witnessed":
        return "◐"
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
        params = []
        for g in ghosts:
            val = render_value(s.get(g)).strip('"')
            if val and val not in params:
                params.append(val)
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
        if st in ("not-run", "no-witness", "STALE", "UNSTAMPED", "UNVERIFIABLE",
                  "MISSING-FILE", "INVALID", "SKIPPED-UNJUSTIFIED"):
            label = {"not-run": "Unchecked requirement",
                     "no-witness": "UNREACHABLE behavior (no witness)"}.get(st, f"Witness {st}")
            items.append(f"**{label}** — {rid}" + (f": {detail}" if detail else ""))
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
    for q in area.get("open_questions", []) or []:
        if q.get("status", "open") in ("open", "deferred"):
            src = f" _(source: {q['source']})_" if q.get("source") else ""
            items.append(f"**Open question** — {q.get('id')}: {q.get('question')}{src}")
    log = area.get("verification_log") or []
    if log and log[-1].get("drift_detected"):
        items.append("**Drift** — spec-traced code changed outside /spec-apply and "
                     "verification fails. Revert the code or codify via /spec.")
    return items


# ── Section renderers (area) ─────────────────────────────────────────────────

def header_bar(area, area_name):
    reqs = [r for r in area.get("requirements", []) or [] if r.get("status") != "deferred"]
    n_ver = sum(1 for r in reqs if r.get("status") == "verified")
    n_wit = sum(1 for r in reqs if (r.get("witness") or {}).get("status") == "witnessed")
    invs = area.get("invariants", []) or []
    n_inv_ver = sum(1 for i in invs if i.get("formal_status") == "verified")
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
    return (f"**Status:** {area.get('status', 'raw')}  |  "
            f"**Requirements:** {n_ver}/{len(reqs)} verified, {n_wit}/{len(reqs)} witnessed  |  "
            f"**Invariants:** {n_inv_ver}/{len(invs)} verified  |  "
            f"**Coverage:** {cov}  |  "
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
    if req.get("type") == "non-functional":
        fc = req.get("fit_criterion") or {}
        lines.append(f"> **Fit:** {fc.get('metric', '?')} — {fc.get('target', '?')} — "
                     f"measured by {fc.get('measurement', '?')}")
        lines.append("")
        return lines
    w = req.get("witness") or {}
    if w.get("status") == "skipped" and w.get("justification"):
        lines.append(f"> **Witness skipped:** {w['justification']}")
        lines.append("")
    elif w.get("trace") and w.get("status") == "witnessed":
        lines.append(f"> **Witness:** {witness_one_liner(root, w['trace'])}")
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
    lines = ["## What Must Always Be True", ""]
    for inv in sorted(invs, key=lambda i: i.get("id", "")):
        st = inv.get("formal_status", "specified")
        mark = {"verified": "✓", "counterexample-found": "✗",
                "accepted-risk": "⚠ accepted-risk"}.get(st, "⏳")
        tail = " — see Needs Your Attention." if st == "counterexample-found" else ""
        lines.append(f"- **{inv.get('id')}** (`{inv.get('quint_name', '—')}`) — "
                     f"{inv.get('description', '')} Criticality: "
                     f"{inv.get('criticality', 'high')}. {mark}{tail}")
    lines.append("")
    props = area.get("properties", []) or []
    if props:
        lines.append("## What Must Eventually Happen")
        lines.append("")
        for p in sorted(props, key=lambda i: i.get("id", "")):
            st = p.get("formal_status", "specified")
            mark = "✓ (bounded)" if st == "verified" else ("✗" if st == "counterexample-found" else "⏳")
            lines.append(f"- **{p.get('id')}** (`{p.get('quint_name', '—')}`) — "
                         f"{p.get('description', '')} {mark}")
        lines.append("")
        lines.append("_Liveness results are bounded — Apalache proves them up to the "
                     "configured step limit only._")
        lines.append("")
    return lines


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
        ent_strs = []
        for e in ents:
            states = f" ({' / '.join(e['states'])})" if e.get("states") else ""
            ent_strs.append(f"**{e.get('name')}**{states} — {e.get('description', '')}")
        lines.append("**Entities:** " + " · ".join(ent_strs))
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
        lines.append("_No code generated yet. Run /spec-apply._")
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
    lines.append(header_bar(area, area_name))
    lines.append("")
    lines.append(LEGEND)
    lines.append("")
    if area.get("purpose"):
        lines += ["## Purpose", "", area["purpose"], ""]
    items = attention_items(root, area_name, area)
    lines.append("## ⚠ Needs Your Attention")
    lines.append("")
    if items:
        lines += [f"- {i}" for i in items]
    else:
        lines.append("**Nothing needs attention.**")
    lines.append("")
    lines += ui_sections(area)
    lines += what_the_system_does(root, area_name, area, journeys)
    lines += invariants_section(area)
    lines += limits_section(root, area_name, area)
    if not area.get("screens"):
        lines += state_machines_section(area)
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


def emit_change(root, slug):
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
                st = item.get("formal_status", "specified")
                mark = {"verified": "✓", "counterexample-found": "✗"}.get(st, "⏳")
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
    pc.set_defaults(func=lambda a: emit_change(Path(a.root), a.slug))

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
