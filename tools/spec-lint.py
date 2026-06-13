#!/usr/bin/env python3
"""
spec-lint.py — Consistency checker for the spec project.

Reads .spec/project.json + each specs/<area>.area.json (or .contract.json —
the filename suffix encodes the kind) and the .qnt sidecar, and
checks cross-file consistency: ID format, broken references, drift between the
area JSON and its sidecar, missing patterns/protocols, topology orphans,
unverified critical invariants, unresolved questions, EARS requirement
structure, witness-trace obligations (every requirement must be
demonstrably reachable in the model — see METHODOLOGY.md), change
manifests (specs/changes/*.change.json — targets and ids must resolve), and
journeys (specs/journeys/*.journey.json — cross-area step refs must resolve).

This is much smaller than the per-file lint of the previous methodology because
the new methodology has fewer files: one JSON per area, one sidecar, one project
config, two catalogs.

Usage:
  tools/spec-lint.py                       # lint every area in .spec/project.json (specs/*.area.json / *.contract.json)
  tools/spec-lint.py <area>                # lint one area
  tools/spec-lint.py --json                # JSON output
  tools/spec-lint.py --strict              # exit 1 on warnings too
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import area_json_path, changes_dir, journeys_dir  # noqa: E402

# Severity constants.
PASS = "pass"
WARN = "warn"
FAIL = "fail"

# ID patterns.
ID_PATTERNS = {
    "REQ":  re.compile(r"^REQ(-CONTRACT)?-\d{3}$"),
    "UI":   re.compile(r"^UI-\d{3}$"),
    "INV":  re.compile(r"^INV(-CONTRACT)?-\d{3}$"),
    "PROP": re.compile(r"^PROP(-CONTRACT)?-\d{3}$"),
    "CON":  re.compile(r"^CON(-CONTRACT)?-\d{3}$"),
    "DEC":  re.compile(r"^DEC-\d{3}$"),
    "Q":    re.compile(r"^Q-\d{3}$"),
}


class Finding:
    def __init__(self, severity, category, check, area, description, ref=None):
        self.severity = severity
        self.category = category
        self.check = check
        self.area = area
        self.description = description
        self.ref = ref

    def to_dict(self):
        return {
            "severity": self.severity,
            "category": self.category,
            "check": self.check,
            "area": self.area,
            "description": self.description,
            "ref": self.ref,
        }


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        # Return a sentinel object with the error embedded — a broken file
        # is a FAIL finding, never a crashed lint run.
        return {"__parse_error__": str(e)}


def add(findings, severity, category, check, area, description, ref=None):
    findings.append(Finding(severity, category, check, area, description, ref))


# ── Sidecar parsing ───────────────────────────────────────────────────────────
# Single engine: tools/quint_ir.py (Quint compiler's typed JSON IR when the
# CLI is available, its regex fallback otherwise). The two files ship
# together; if quint_ir.py is missing the install is broken — fail loudly
# rather than lint with a divergent parser.

sys.path.insert(0, str(Path(__file__).parent))
try:
    from quint_ir import parse_qnt as _ir_parse_qnt
    from itf_tools import compute_model_sha as _compute_model_sha
    from itf_tools import load_trace as _load_trace
except ImportError as e:
    sys.exit(f"ERROR: spec-lint needs tools/quint_ir.py and tools/itf_tools.py "
             f"next to it ({e}).")

# Optional: full JSON Schema validation when the jsonschema lib is installed
# (CI installs it; locally it's a pip away). Lint is then the single validity
# authority — no separate schema-validation step anywhere else. Several other
# checks deliberately defer malformed-shape detection to schema validation,
# so running without the lib leaves real holes: main() emits a WARN finding
# whenever it is unavailable, instead of silently skipping.
try:
    import jsonschema as _jsonschema
except ImportError:
    _jsonschema = None


def build_schema_validator(root, schema_name="area.schema.json"):
    if _jsonschema is None:
        return None
    schema_path = root / "schemas" / schema_name
    if not schema_path.exists():
        # Project without its own schemas/ (e.g. the template's examples/):
        # fall back to the schemas shipped next to this tool.
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / schema_name
        if not schema_path.exists():
            return None
    return _jsonschema.Draft7Validator(json.loads(schema_path.read_text(encoding="utf-8")))


def check_schema(area_data, validator, area_name, findings):
    if validator is None:
        return
    for err in validator.iter_errors(area_data):
        where = "/".join(str(p) for p in err.path) or "(root)"
        add(findings, FAIL, "schema", "schema-violation", area_name,
            f"{where}: {err.message}")


def parse_sidecar(path):
    if not path.exists():
        return None
    ir = _ir_parse_qnt(path)
    if ir is None:
        return {"__no_module__": True}
    return {
        "module_name":      ir["module_name"],
        "imports":          [i["module"] for i in ir["imports"]],
        "named":            set(ir["vals"]) | set(ir["temporals"]),
        "actions":          set(ir["actions"]),
        "action_mutations": ir["action_mutations"],
        "const_values":     ir.get("const_values") or {},
    }


# ── Checks ────────────────────────────────────────────────────────────────────

def check_area_meta(area_data, area_name, findings):
    """Required fields, version format, status."""
    for required in ("kind", "area", "version"):
        if required not in area_data:
            add(findings, FAIL, "meta", "missing-required", area_name,
                f"Required field '{required}' is missing.")

    if area_data.get("area") and area_data["area"] != area_name:
        add(findings, FAIL, "meta", "name-mismatch", area_name,
            f"area field is '{area_data['area']}' but file is "
            f"specs/{area_name}.{area_data.get('kind', 'area')}.json.")

    if area_data.get("kind") == "contract" and not area_data.get("spans"):
        add(findings, FAIL, "meta", "contract-no-spans", area_name,
            "kind=contract requires spans[] listing the participant areas.")

    if area_data.get("kind") == "ui":
        add(findings, FAIL, "meta", "removed-kind-ui", area_name,
            "kind=ui no longer exists — use kind: area with screens[] + navigation[]; "
            "UI lint/readback/codegen trigger on block presence, not kind.")

    if area_data.get("screens") and not area_data.get("navigation"):
        add(findings, FAIL, "meta", "screens-no-navigation", area_name,
            "screens[] is declared but navigation[] is empty — an interactive "
            "surface needs its transitions.")


def check_ids(area_data, area_name, findings):
    """ID format and uniqueness within each list."""
    seen = defaultdict(set)
    for list_name, prefix in [
        ("requirements", "REQ"), ("invariants", "INV"), ("properties", "PROP"),
        ("constraints", "CON"), ("decisions", "DEC"), ("open_questions", "Q"),
    ]:
        for item in area_data.get(list_name, []) or []:
            iid = item.get("id")
            if not iid:
                add(findings, FAIL, "ids", "missing-id", area_name,
                    f"Item in {list_name}[] has no id.")
                continue
            # UI requirements may use UI-NNN
            if list_name == "requirements" and iid.startswith("UI-"):
                pattern = ID_PATTERNS["UI"]
            else:
                pattern = ID_PATTERNS.get(prefix)
            if pattern and not pattern.match(iid):
                add(findings, FAIL, "ids", "bad-id-format", area_name,
                    f"ID '{iid}' in {list_name}[] doesn't match expected pattern for {prefix}-NNN.",
                    ref=iid)
            if iid in seen[list_name]:
                add(findings, FAIL, "ids", "duplicate-id", area_name,
                    f"Duplicate ID '{iid}' in {list_name}[].", ref=iid)
            seen[list_name].add(iid)


def check_quint_refs(area_data, sidecar, area_name, findings):
    """Each requirement's quint_ref maps to a real action; each invariant's
    quint_name maps to a real val/invariant/temporal."""
    if not sidecar or "__no_module__" in sidecar:
        return

    for req in area_data.get("requirements", []) or []:
        ref = req.get("quint_ref")
        if ref and ref not in sidecar["actions"]:
            add(findings, WARN, "quint", "quint-ref-missing", area_name,
                f"{req.get('id', '?')}.quint_ref '{ref}' has no matching action in the sidecar.",
                ref=req.get("id"))

    for inv in area_data.get("invariants", []) or []:
        name = inv.get("quint_name")
        if name and name not in sidecar["named"]:
            add(findings, FAIL, "quint", "invariant-quint-missing", area_name,
                f"{inv.get('id', '?')}.quint_name '{name}' has no matching val/invariant in the sidecar.",
                ref=inv.get("id"))
    for prop in area_data.get("properties", []) or []:
        name = prop.get("quint_name")
        if name and name not in sidecar["named"]:
            add(findings, FAIL, "quint", "property-quint-missing", area_name,
                f"{prop.get('id', '?')}.quint_name '{name}' has no matching temporal in the sidecar.",
                ref=prop.get("id"))


# Requirement statuses early enough that missing structure is expected.
EARLY_STATUSES = ("raw", "needs-validation")


def check_ears(area_data, area_name, findings):
    """EARS structure per requirement. The pattern is DERIVED from which
    fields are filled (trigger → 'When', state → 'While', feature →
    'Where', none → ubiquitous); only `unwanted` is declared. So there's
    nothing to cross-check — just three rules."""
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        status = req.get("status", "raw")
        if status == "deferred":
            continue
        ears = req.get("ears")
        if not ears:
            if status not in EARLY_STATUSES:
                add(findings, WARN, "ears", "unstructured-requirement", area_name,
                    f"{rid} has status '{status}' but no ears structure. "
                    f"Run /spec to capture trigger/state/response.",
                    ref=rid)
            continue
        if not ears.get("response"):
            add(findings, FAIL, "ears", "ears-missing-response", area_name,
                f"{rid}.ears has no response ('the system shall ...').", ref=rid)
        if ears.get("unwanted") and not ears.get("trigger"):
            add(findings, FAIL, "ears", "ears-unwanted-no-trigger", area_name,
                f"{rid}.ears is unwanted-behavior handling but names no trigger "
                f"('If <what goes wrong>, then ...').", ref=rid)
        if not (ears.get("trigger") or ears.get("state") or ears.get("feature")
                or ears.get("unwanted")):
            add(findings, WARN, "ears", "ubiquitous-requirement", area_name,
                f"{rid} has no trigger/state/feature — a 'shall always' statement "
                f"is an invariant, not a behavior. Move it to invariants[] so "
                f"Apalache proves it; a witness adds nothing to an always-true "
                f"statement.", ref=rid)


# Words that make a response untestable. Curated for signal, not coverage —
# every entry is a word whose presence almost always means the response
# doesn't say WHAT state results or WHERE it's visible. (ARM-style lint.)
AMBIGUOUS_TERMS = re.compile(
    r"\b(gracefully|appropriately?|properly|quickly|efficiently|robustly?|"
    r"seamlessly?|intuitive(?:ly)?|user-friendly|flexible|timely|"
    r"as needed|as appropriate|as required|if necessary|if needed|"
    r"reasonable|sufficient(?:ly)?|adequate(?:ly)?|minimal|optimal|"
    r"normally|usually|generally|etc\.?|and/or|TBD|TODO)\b",
    re.IGNORECASE,
)


def check_ambiguity(area_data, area_name, findings):
    """Vague words in ears.response make the requirement unwitnessable —
    'handle errors gracefully' can never get a witness.predicate. Flag at
    lint time so the sharpening happens before formalization, not after a
    no-witness result."""
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred":
            continue
        response = (req.get("ears") or {}).get("response") or ""
        hits = sorted({m.group(0).lower() for m in AMBIGUOUS_TERMS.finditer(response)})
        if hits:
            add(findings, WARN, "ears", "ambiguous-response", area_name,
                f"{rid}.ears.response contains untestable wording: {', '.join(hits)}. "
                f"Sharpen: what state results, visible where?",
                ref=rid)


def check_state_binding(area_data, area_name, findings):
    """ears.state should be phrased with a DECLARED entity state name
    ('the Session is Active', not 'the user is logged in') — that's what
    makes the requirement↔state-machine link reviewable and matrix triage
    mechanical. Only fires when the area declares states at all."""
    declared = set()
    for ent in (area_data.get("concepts") or {}).get("entities", []) or []:
        declared.update(ent.get("states") or [])
    for sm in area_data.get("state_machines", []) or []:
        declared.update(s.get("name") for s in sm.get("states") or [] if s.get("name"))
    if not declared:
        return
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(s) for s in sorted(declared)) + r")\b",
        re.IGNORECASE,
    )
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred":
            continue
        state_text = (req.get("ears") or {}).get("state")
        if state_text and not pattern.search(state_text):
            add(findings, WARN, "ears", "state-not-bound", area_name,
                f"{rid}.ears.state ('{state_text}') names no declared entity "
                f"state ({', '.join(sorted(declared))}). Phrase preconditions "
                f"with declared state names so the requirement↔state-machine "
                f"link is checkable.",
                ref=rid)


def check_fit_criteria(area_data, area_name, findings):
    """Non-functional requirements are exempt from witness obligations, so
    their precision mechanism is the fit_criterion: metric, target,
    measurement. 'Fast' is not a requirement until it carries all three."""
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("type") != "non-functional" or req.get("status") == "deferred":
            continue
        fc = req.get("fit_criterion") or {}
        missing = [k for k in ("metric", "target", "measurement") if not fc.get(k)]
        if missing:
            severity = WARN if req.get("status", "raw") in EARLY_STATUSES else FAIL
            add(findings, severity, "ears", "nfr-no-fit-criterion", area_name,
                f"{rid} is non-functional but fit_criterion is missing "
                f"{', '.join(missing)} — unmeasurable until it says what is "
                f"measured, the bound, and how it's measured.",
                ref=rid)


def check_witnesses(root, area_data, area_name, findings):
    """Witness obligations: every claimed behavior must be demonstrable in
    the model. A verified-but-unwitnessed spec can be vacuous (invariants
    hold over an empty state space). /spec-check produces the traces; this
    check enforces their presence and freshness (model_sha pinning — a
    trace found against an older model proves nothing about this one)."""
    approved = area_data.get("status") == "approved"
    current_sha = _compute_model_sha(root, area_name, area_data)
    any_witnessed = any(
        (r.get("witness") or {}).get("status") == "witnessed"
        for r in area_data.get("requirements", []) or []
    )
    if any_witnessed and current_sha is None:
        add(findings, FAIL, "witness", "model-files-missing", area_name,
            "Requirements are marked witnessed but the model files can't be "
            "hashed (sidecar missing, or formal_model.probes_file recorded but "
            "absent). Freshness is unverifiable — every witness is suspect. "
            "Restore the files or re-run /spec-check.")
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred":
            continue
        if req.get("type") == "non-functional":
            # Exempt from witness obligations by design: no reachable state
            # change to demonstrate. The obligation it carries instead is a
            # measurable fit_criterion — enforced in check_fit_criteria.
            continue
        witness = req.get("witness") or {}
        predicate = witness.get("predicate")
        trace_rel = witness.get("trace")
        wstatus = witness.get("status", "not-run")

        if wstatus == "skipped":
            # Deliberate opt-out — legitimate for rejection requirements
            # (no state change to witness; an invariant carries the proof).
            # Only a JUSTIFIED skip discharges the obligation.
            if not witness.get("justification"):
                add(findings, FAIL, "witness", "skipped-no-justification", area_name,
                    f"{rid}.witness is skipped without a justification — an "
                    f"unjustified skip does not discharge the obligation. "
                    f"Rejection requirement? Point at the invariant that "
                    f"enforces it.",
                    ref=rid)
            continue

        # Approval gate: every non-deferred functional REQ must be witnessed
        # or justified-skipped — REGARDLESS of how incomplete its witness
        # block is. (Checked before any early-return below, so a predicate-less
        # requirement can't slip through an approved area.)
        if approved and wstatus != "witnessed":
            add(findings, FAIL, "witness", "approved-unwitnessed", area_name,
                f"Area is approved but {rid} witness status is '{wstatus}'. "
                f"Run /spec-check before approval.",
                ref=rid)

        # Trace presence/validity/freshness run UNCONDITIONALLY — they must
        # not depend on the predicate being present, or deleting the
        # predicate would silence the freshness FAILs on a witnessed REQ.
        if not predicate and req.get("status") not in EARLY_STATUSES:
            # Past the draft phase (raw/needs-validation), an absent predicate
            # is the mechanized vagueness gate: a functional response you can't
            # write a boolean witness for is too vague to ever be witnessed or
            # verified ("handle errors gracefully" — what observable state?).
            # FAIL, not WARN — vagueness must block, not nag. The ambiguous-
            # wording lint (check_ambiguous) catches phrasing; this catches the
            # absence. Draft statuses (raw/needs-validation) are exempt so
            # elicitation isn't blocked mid-capture.
            add(findings, FAIL, "witness", "no-witness-predicate", area_name,
                f"{rid} (status '{req.get('status')}') has no witness.predicate — the "
                f"behavior can't be demonstrated reachable, so it's too vague to verify. "
                f"Sharpen the response to a named observable state, capture a predicate "
                f"via /spec, then run /spec-check.",
                ref=rid)

        if trace_rel:
            trace_path = root / "specs" / trace_rel
            if not trace_path.exists():
                add(findings, FAIL, "witness", "witness-trace-missing", area_name,
                    f"{rid}.witness.trace '{trace_rel}' does not exist under specs/. "
                    f"Re-run /spec-check to regenerate it.",
                    ref=rid)
            elif wstatus == "witnessed":
                _, trace_errs = _load_trace(trace_path)
                if trace_errs:
                    add(findings, FAIL, "witness", "witness-trace-invalid", area_name,
                        f"{rid}.witness.trace '{trace_rel}' is not a valid ITF trace: "
                        f"{trace_errs[0]}",
                        ref=rid)
                stamped = witness.get("model_sha")
                if not stamped:
                    add(findings, FAIL, "witness", "witness-unstamped", area_name,
                        f"{rid}.witness has no model_sha — freshness can't be "
                        f"checked, so the 'every witness fresh' obligation is "
                        f"unenforceable. Re-run /spec-check to pin it.",
                        ref=rid)
                elif current_sha and stamped != current_sha:
                    add(findings, FAIL, "witness", "witness-stale", area_name,
                        f"{rid}.witness.model_sha doesn't match the current model "
                        f"(.qnt/.probes.qnt changed since the trace was found). "
                        f"The trace proves nothing about the current model — "
                        f"re-run /spec-check.",
                        ref=rid)
        elif wstatus == "witnessed":
            add(findings, FAIL, "witness", "witnessed-without-trace", area_name,
                f"{rid}.witness.status is 'witnessed' but no trace file is recorded.",
                ref=rid)

        if wstatus == "no-witness":
            add(findings, FAIL, "witness", "no-witness-found", area_name,
                f"{rid}: /spec-check found NO witness — the behavior is unreachable "
                f"in the model (impossible guard or missing action?). Fix the model "
                f"or the requirement.",
                ref=rid)


def check_constraint_values(area_data, sidecar, area_name, findings):
    """constraints[].value must equal the model's literal constant of the
    same name — otherwise Apalache proves things about a different number
    than the spec promises."""
    if not sidecar or "__no_module__" in sidecar:
        return
    const_values = sidecar.get("const_values") or {}
    for con in area_data.get("constraints", []) or []:
        name = con.get("name")
        if name and name in const_values and const_values[name] != con.get("value"):
            add(findings, FAIL, "quint", "constraint-value-mismatch", area_name,
                f"{con.get('id', '?')}: constraints[].{name} = {con.get('value')!r} "
                f"but the model says {const_values[name]!r}. The checker is "
                f"verifying a different number than the spec promises.",
                ref=con.get("id"))


# Actions that are model plumbing, never requirement-bearing.
PLUMBING_ACTIONS = {"init", "step", "initP", "stepP"}


def check_orphan_actions(area_data, sidecar, area_name, findings):
    """Every sidecar action must be referenced by something: a requirement's
    quint_ref, a state-machine transition, or lifecycle_actions. An
    unreferenced action is either a missing requirement or dead spec text.
    (Coverage of *referenced* actions is proven by their path-constrained
    witness traces — no model-checker run needed here.) Skipped for
    contracts and for areas with navigation[]: contract vals and UI
    navigation triggers don't map 1:1 to actions."""
    if area_data.get("kind") == "contract":
        return
    if area_data.get("navigation"):
        return
    if not sidecar or "__no_module__" in sidecar:
        return
    referenced = set()
    for req in area_data.get("requirements", []) or []:
        if req.get("quint_ref"):
            referenced.add(req["quint_ref"])
    for sm in area_data.get("state_machines", []) or []:
        for t in sm.get("transitions", []) or []:
            if t.get("quint_action"):
                referenced.add(t["quint_action"])
        referenced.update(sm.get("lifecycle_actions") or [])
    for action in sorted(set(sidecar["actions"]) - referenced - PLUMBING_ACTIONS):
        add(findings, WARN, "quint", "orphan-action", area_name,
            f"Action '{action}' is not referenced by any requirement, transition, "
            f"or lifecycle_actions — missing requirement or dead spec text.",
            ref=action)


def check_formal_model_consistency(area_data, sidecar, area_name, findings):
    """formal_model.quint_file must point at a parseable module. (Module name
    and imports are read from the sidecar via quint_ir — never mirrored in
    the JSON, so there's nothing else to reconcile.)"""
    fm = area_data.get("formal_model") or {}
    if (not sidecar or "__no_module__" in sidecar) and fm.get("quint_file"):
        add(findings, FAIL, "quint", "sidecar-missing-or-empty", area_name,
            "formal_model.quint_file is set but the sidecar is missing or has no module declaration.")


def check_cross_refs(area_data, all_areas, area_name, findings):
    """cross_refs of form '<area>.<ID>' should resolve."""
    for list_name in ("requirements", "invariants", "properties"):
        for item in area_data.get(list_name, []) or []:
            for xref in item.get("cross_refs", []) or []:
                if "." not in xref:
                    add(findings, WARN, "cross-refs", "bad-cross-ref-format", area_name,
                        f"{item.get('id')}.cross_refs entry '{xref}' is not in '<area>.<ID>' form.",
                        ref=item.get("id"))
                    continue
                other_area, other_id = xref.split(".", 1)
                if other_area not in all_areas:
                    add(findings, WARN, "cross-refs", "unknown-area", area_name,
                        f"{item.get('id')}.cross_refs points to area '{other_area}' which has no spec file (specs/{other_area}.area.json or .contract.json).",
                        ref=item.get("id"))
                    continue
                other = all_areas[other_area]
                if isinstance(other, dict) and "__parse_error__" not in other:
                    other_ids = set()
                    for src in ("requirements", "invariants", "properties", "constraints"):
                        for o in other.get(src, []) or []:
                            if o.get("id"):
                                other_ids.add(o["id"])
                    if other_id not in other_ids:
                        add(findings, FAIL, "cross-refs", "broken-cross-ref", area_name,
                            f"{item.get('id')}.cross_refs entry '{xref}' — ID '{other_id}' does not exist in {other_area}.",
                            ref=item.get("id"))


def check_contract_spans(area_data, all_areas, area_name, findings):
    """For kind=contract, every spans[] entry must reference an existing area."""
    if area_data.get("kind") != "contract":
        return
    for s in area_data.get("spans", []) or []:
        if s not in all_areas:
            add(findings, FAIL, "contract", "missing-span", area_name,
                f"spans[] references area '{s}' which has no spec file (specs/{s}.area.json or .contract.json).", ref=s)


def check_open_questions(area_data, area_name, findings):
    """Open questions block approval; deferred is OK."""
    open_count = 0
    for q in area_data.get("open_questions", []) or []:
        if q.get("status", "open") == "open":
            open_count += 1
    if area_data.get("status") == "approved" and open_count > 0:
        add(findings, FAIL, "questions", "open-questions-on-approved", area_name,
            f"Area is marked approved but has {open_count} open question(s).")
    elif open_count > 0:
        add(findings, WARN, "questions", "open-questions", area_name,
            f"{open_count} open question(s) remain.")


def check_critical_invariants(area_data, area_name, findings):
    """Critical invariants must be verified before approval."""
    if area_data.get("status") != "approved":
        return
    for inv in area_data.get("invariants", []) or []:
        if inv.get("criticality") == "critical" and inv.get("formal_status") not in ("verified", "verified-inductive", "accepted-risk"):
            add(findings, FAIL, "invariants", "critical-not-verified", area_name,
                f"Critical invariant {inv.get('id')} has formal_status '{inv.get('formal_status')}' — must be verified before approval.",
                ref=inv.get("id"))


def check_architecture_patterns_protocols(area_data, catalog, area_name, findings):
    """Referenced patterns/protocols must exist in the catalog."""
    arch = area_data.get("architecture") or {}
    for ref in arch.get("patterns", []) or []:
        if ref not in catalog["patterns"]:
            add(findings, FAIL, "architecture", "missing-pattern", area_name,
                f"architecture.patterns references '{ref}' but .spec/patterns/{ref}.json doesn't exist.",
                ref=ref)
    for ref in arch.get("protocols", []) or []:
        if ref not in catalog["protocols"]:
            add(findings, FAIL, "architecture", "missing-protocol", area_name,
                f"architecture.protocols references '{ref}' but .spec/protocols/{ref}.json doesn't exist.",
                ref=ref)
    for comp in arch.get("components", []) or []:
        for ref in comp.get("patterns", []) or []:
            if ref not in catalog["patterns"]:
                add(findings, FAIL, "architecture", "missing-pattern", area_name,
                    f"component '{comp.get('name')}' references pattern '{ref}' but .spec/patterns/{ref}.json doesn't exist.",
                    ref=ref)
        for ref in comp.get("protocols", []) or []:
            if ref not in catalog["protocols"]:
                add(findings, FAIL, "architecture", "missing-protocol", area_name,
                    f"component '{comp.get('name')}' references protocol '{ref}' but .spec/protocols/{ref}.json doesn't exist.",
                    ref=ref)


def check_components_implementation(area_data, area_name, findings):
    """Each declared component should have implementing traceability entries."""
    arch = area_data.get("architecture") or {}
    components = arch.get("components", []) or []
    if not components:
        return
    declared = {c["name"] for c in components if c.get("name")}
    traced = {t.get("component") for t in (area_data.get("traceability") or []) if t.get("component")}
    orphan = declared - traced
    if orphan:
        add(findings, WARN, "architecture", "component-no-traceability", area_name,
            f"Components declared but with no traceability entries: {sorted(orphan)}. "
            f"Either remove from architecture.components or run /spec-apply to generate code.")

    # Actions claimed by multiple components.
    owners = defaultdict(list)
    for c in components:
        for a in c.get("implements", []) or []:
            owners[a].append(c["name"])
    for action, names in owners.items():
        if len(names) > 1:
            add(findings, WARN, "architecture", "action-claimed-multiple", area_name,
                f"Quint action '{action}' is claimed by multiple components: {sorted(names)}. "
                f"Each action should be implemented by exactly one component.",
                ref=action)


def check_ui_navigation(area_data, area_name, findings):
    """All navigation endpoints must reference declared screens. Triggers on
    block presence — any area may carry screens/navigation."""
    if not (area_data.get("screens") or area_data.get("navigation")):
        return
    screen_names = {s["name"] for s in (area_data.get("screens") or []) if s.get("name")}
    referenced = set()
    for nav in area_data.get("navigation") or []:
        for end in ("from", "to"):
            if nav.get(end) and nav[end] not in screen_names:
                add(findings, FAIL, "ui", "navigation-unknown-screen", area_name,
                    f"navigation entry references screen '{nav[end]}' not declared in screens[].",
                    ref=nav[end])
            if nav.get(end):
                referenced.add(nav[end])
    # Screens with no incoming AND no outgoing edges (orphan screens).
    for s in screen_names:
        if s not in referenced:
            add(findings, WARN, "ui", "isolated-screen", area_name,
                f"Screen '{s}' has no navigation edges in or out — unreachable.", ref=s)


def check_state_machines(area_data, sidecar, area_name, findings):
    """
    Structural validation of declared state machines (independent of Apalache).
    Each entry in state_machines[] is cross-checked against:
      - its own declared states and transitions (initial state in set, no
        terminal-with-outgoing, all from/to refer to declared states,
        every state reachable from initial, no orphan non-terminal states)
      - the Quint sidecar (quint_action exists; quint_var is mutated by the
        Quint actions referenced as transitions; any sidecar action mutating
        quint_var should be listed as a transition or flagged)
    """
    machines = area_data.get("state_machines") or []
    if not machines:
        return

    # Entity name set, to validate state_machines[].entity refers to a known entity.
    entity_names = {
        e.get("name") for e in (area_data.get("concepts") or {}).get("entities", []) or []
    }

    for sm in machines:
        entity = sm.get("entity", "?")

        if entity and entity not in entity_names:
            add(findings, WARN, "state-machine", "unknown-entity", area_name,
                f"state_machine for '{entity}' has no matching concepts.entities[] entry.",
                ref=entity)

        states_list = sm.get("states") or []
        state_names = {s.get("name") for s in states_list if s.get("name")}
        terminal_states = {s["name"] for s in states_list if s.get("terminal") and s.get("name")}
        initial = sm.get("initial_state")
        transitions = sm.get("transitions") or []

        if not state_names:
            add(findings, FAIL, "state-machine", "no-states", area_name,
                f"state_machine for '{entity}' declares no states[].", ref=entity)
            continue

        if initial and initial not in state_names:
            add(findings, FAIL, "state-machine", "bad-initial-state", area_name,
                f"state_machine for '{entity}': initial_state '{initial}' is not in states[].",
                ref=entity)

        # Per-transition validations.
        valid_from_states = state_names | {"*"}
        outgoing_per_state = {s: 0 for s in state_names}
        inbound_per_state = {s: 0 for s in state_names}
        for t in transitions:
            frm = t.get("from")
            to  = t.get("to")
            trig = t.get("trigger", "?")

            if frm and frm not in valid_from_states:
                add(findings, FAIL, "state-machine", "bad-from-state", area_name,
                    f"state_machine for '{entity}': transition '{trig}' has from='{frm}' which is not a declared state.",
                    ref=f"{entity}:{trig}")
            if to and to not in state_names:
                add(findings, FAIL, "state-machine", "bad-to-state", area_name,
                    f"state_machine for '{entity}': transition '{trig}' has to='{to}' which is not a declared state.",
                    ref=f"{entity}:{trig}")

            if frm == "*":
                for s in state_names - terminal_states:
                    outgoing_per_state[s] = outgoing_per_state.get(s, 0) + 1
            elif frm in state_names:
                outgoing_per_state[frm] = outgoing_per_state.get(frm, 0) + 1

            if to in state_names:
                inbound_per_state[to] = inbound_per_state.get(to, 0) + 1

            # Terminal states cannot have outgoing transitions.
            if frm in terminal_states:
                add(findings, FAIL, "state-machine", "terminal-has-outgoing", area_name,
                    f"state_machine for '{entity}': state '{frm}' is terminal but has outgoing transition '{trig}' → {to}.",
                    ref=f"{entity}:{frm}")

            # Quint action existence.
            qa = t.get("quint_action")
            if qa and sidecar and "__no_module__" not in sidecar:
                if qa not in sidecar["actions"]:
                    add(findings, FAIL, "state-machine", "quint-action-missing", area_name,
                        f"state_machine for '{entity}': transition '{trig}' references quint_action '{qa}' which is not in the sidecar.",
                        ref=f"{entity}:{trig}")

        # Reachability from initial (BFS over declared transitions).
        if initial and initial in state_names:
            reachable = {initial}
            edges = []
            for t in transitions:
                frm = t.get("from")
                to = t.get("to")
                if to not in state_names:
                    continue
                if frm == "*":
                    for s in state_names - terminal_states:
                        edges.append((s, to))
                elif frm in state_names:
                    edges.append((frm, to))
            # Iterate to fixed point.
            changed = True
            while changed:
                changed = False
                for f, t in edges:
                    if f in reachable and t not in reachable:
                        reachable.add(t)
                        changed = True
            unreachable = state_names - reachable
            for s in sorted(unreachable):
                add(findings, WARN, "state-machine", "unreachable-state", area_name,
                    f"state_machine for '{entity}': state '{s}' is not reachable from initial_state '{initial}'.",
                    ref=f"{entity}:{s}")

        # Non-terminal states with no outgoing transitions = unintended sinks.
        for s in sorted(state_names - terminal_states):
            if outgoing_per_state.get(s, 0) == 0:
                add(findings, WARN, "state-machine", "non-terminal-sink", area_name,
                    f"state_machine for '{entity}': non-terminal state '{s}' has no outgoing transitions (dangling state). Either add a transition or mark it terminal: true.",
                    ref=f"{entity}:{s}")

        # Sidecar mutations not listed as transitions.
        quint_var = sm.get("quint_var")
        if quint_var and sidecar and "__no_module__" not in sidecar:
            listed_actions = {t.get("quint_action") for t in transitions if t.get("quint_action")}
            lifecycle = set(sm.get("lifecycle_actions") or [])
            mutations = sidecar.get("action_mutations") or {}
            for action_name, mutated_vars in mutations.items():
                if (
                    quint_var in mutated_vars
                    and action_name not in listed_actions
                    and action_name not in lifecycle
                    and action_name not in ("init",)
                ):
                    add(findings, WARN, "state-machine", "sidecar-action-not-listed", area_name,
                        f"state_machine for '{entity}': Quint action '{action_name}' mutates '{quint_var}' but isn't listed as a transition or lifecycle_action. Add it to transitions[] (state change), to lifecycle_actions[] (creation/destruction/etc.), or document why it shouldn't be tracked.",
                        ref=f"{entity}:{action_name}")


def check_topology(project_data, all_areas, findings):
    """Topology refs must resolve to real components, etc. Runs once per project."""
    topo = (project_data or {}).get("topology")
    if not topo:
        return

    unit_names = {u["name"] for u in (topo.get("deployment_units") or []) if u.get("name")}
    placed = set()
    for u in topo.get("deployment_units") or []:
        for ref in u.get("components") or []:
            placed.add(ref)
            if "." not in ref:
                add(findings, WARN, "topology", "bad-component-ref", "_topology",
                    f"Deployment unit '{u.get('name')}' references '{ref}' — expected '<area>.<component>' form.")
                continue
            a, c = ref.split(".", 1)
            if a not in all_areas:
                add(findings, WARN, "topology", "unknown-area", "_topology",
                    f"Deployment unit '{u.get('name')}' references area '{a}' not in project.", ref=ref)
            else:
                area_data = all_areas[a]
                if isinstance(area_data, dict) and "__parse_error__" not in area_data:
                    components = (area_data.get("architecture") or {}).get("components", []) or []
                    component_names = {x["name"] for x in components if x.get("name")}
                    if c not in component_names:
                        add(findings, WARN, "topology", "unknown-component", "_topology",
                            f"Deployment unit '{u.get('name')}' references component '{c}' not declared in {a}.",
                            ref=ref)

    # Orphan components: declared but not placed.
    for area_name, area_data in all_areas.items():
        if not isinstance(area_data, dict) or "__parse_error__" in area_data:
            continue
        if area_data.get("kind") == "contract":
            continue
        for c in (area_data.get("architecture") or {}).get("components", []) or []:
            cname = c.get("name")
            if cname and f"{area_name}.{cname}" not in placed:
                add(findings, WARN, "topology", "orphan-component", "_topology",
                    f"Component '{area_name}.{cname}' is declared but not in any deployment unit.",
                    ref=f"{area_name}.{cname}")

    # Network boundary endpoints must match units.
    for nb in topo.get("network_boundaries") or []:
        for end in ("from", "to"):
            if nb.get(end) and nb[end] not in unit_names:
                add(findings, FAIL, "topology", "bad-boundary", "_topology",
                    f"Network boundary references unit '{nb[end]}' not in deployment_units.",
                    ref=nb[end])


def check_changes(root, all_areas, findings, validator=None):
    """Change manifests (specs/changes/*.change.json): schema validity, slug
    matches filename, targets resolve to real areas, ids resolve in the target's
    spec, no stored phase flags (status is derived from area JSONs, never stored).
    Landed/abandoned manifests are history — parse + schema + slug only (their
    ids may legitimately have been removed by later changes; that's not drift)."""
    cdir = changes_dir(root)
    if not cdir.exists():
        return
    for p in sorted(cdir.glob("*.json")):
        if not p.name.endswith(".change.json"):
            add(findings, FAIL, "changes", "bad-filename", f"_changes/{p.stem}",
                f"specs/changes/{p.name} — change manifests must be named "
                f"<slug>.change.json.")
            continue
        slug = p.name[:-len(".change.json")]
        name = f"_changes/{slug}"
        data = load_json(p)
        if isinstance(data, dict) and "__parse_error__" in data:
            add(findings, FAIL, "changes", "parse-error", name,
                f"specs/changes/{p.name} failed to parse: {data['__parse_error__']}")
            continue
        check_schema(data, validator, name, findings)
        if data.get("change") and data["change"] != slug:
            add(findings, FAIL, "changes", "slug-mismatch", name,
                f"change field is '{data['change']}' but file is specs/changes/{slug}.change.json.")
        if data.get("status") in ("landed", "abandoned"):
            continue
        for t in data.get("targets", []) or []:
            tname = t.get("name")
            if not tname:
                continue  # schema validation reports the missing name
            if tname not in all_areas:
                add(findings, FAIL, "changes", "unknown-target", name,
                    f"targets[] references '{tname}' which has no spec file "
                    f"(specs/{tname}.area.json or .contract.json).",
                    ref=tname)
                continue
            area_data = all_areas[tname]
            if not isinstance(area_data, dict) or "__parse_error__" in area_data:
                continue
            if t.get("kind") and area_data.get("kind") and t["kind"] != area_data["kind"]:
                add(findings, WARN, "changes", "kind-mismatch", name,
                    f"targets[{tname}].kind is '{t['kind']}' but {tname}'s spec "
                    f"says '{area_data['kind']}'.",
                    ref=tname)
            stored_flags = [k for k in ("checked", "applied", "verified") if k in t]
            if stored_flags:
                add(findings, FAIL, "changes", "stored-phase-flags", name,
                    f"targets[{tname}] stores phase flags ({', '.join(stored_flags)}) "
                    f"— phase status is DERIVED from the area JSONs (witness "
                    f"freshness, check_results, traceability, verification_log), "
                    f"never stored. Remove them; the manifest holds membership only.",
                    ref=tname)
            area_ids = set()
            for src in ("requirements", "invariants", "properties",
                        "constraints", "decisions", "open_questions"):
                for o in area_data.get(src, []) or []:
                    if o.get("id"):
                        area_ids.add(o["id"])
            for iid in t.get("ids", []) or []:
                if iid not in area_ids:
                    add(findings, FAIL, "changes", "dangling-id", name,
                        f"targets[{tname}].ids references '{iid}' which does not "
                        f"exist in {tname}'s spec.",
                        ref=iid)


def check_journeys(root, all_areas, findings, validator=None):
    """Journeys (specs/journeys/*.journey.json): schema validity, unique names,
    every step ref resolves — area exists and the qualified ID is in that
    area's requirements[]. Same reference discipline as cross_refs."""
    jdir = journeys_dir(root)
    if not jdir.exists():
        return
    seen_names = set()
    for p in sorted(jdir.glob("*.json")):
        if not p.name.endswith(".journey.json"):
            add(findings, FAIL, "journeys", "bad-filename", f"_journeys/{p.stem}",
                f"specs/journeys/{p.name} — journeys must be named "
                f"<slug>.journey.json.")
            continue
        jname = f"_journeys/{p.name[:-len('.journey.json')]}"
        data = load_json(p)
        if isinstance(data, dict) and "__parse_error__" in data:
            add(findings, FAIL, "journeys", "parse-error", jname,
                f"specs/journeys/{p.name} failed to parse: {data['__parse_error__']}")
            continue
        check_schema(data, validator, jname, findings)
        name = data.get("name")
        if name:
            if name in seen_names:
                add(findings, FAIL, "journeys", "duplicate-journey", jname,
                    f"Journey name '{name}' is used by more than one file.", ref=name)
            seen_names.add(name)
        seen_refs = set()
        for step in data.get("steps", []) or []:
            ref = step.get("ref")
            if not ref or "." not in ref:
                continue  # schema validation reports malformed refs
            if ref in seen_refs:
                add(findings, FAIL, "journeys", "duplicate-step-ref", jname,
                    f"step ref '{ref}' appears twice in the journey.", ref=ref)
            seen_refs.add(ref)
            area, rid = ref.split(".", 1)
            if area not in all_areas:
                add(findings, FAIL, "journeys", "unknown-area", jname,
                    f"step ref '{ref}' — no spec file (specs/{area}.area.json "
                    f"or .contract.json).", ref=ref)
                continue
            area_data = all_areas[area]
            if not isinstance(area_data, dict) or "__parse_error__" in area_data:
                continue
            req_ids = {r.get("id") for r in (area_data.get("requirements") or []) if r.get("id")}
            if rid not in req_ids:
                add(findings, FAIL, "journeys", "dangling-step-ref", jname,
                    f"step ref '{ref}' — '{rid}' is not in {area}'s requirements[].",
                    ref=ref)


# ── Runner ────────────────────────────────────────────────────────────────────

def lint_area(root, area_name, area_data, sidecar, all_areas, catalog, findings,
              schema_validator=None):
    if area_data is None:
        add(findings, FAIL, "meta", "file-missing", area_name,
            f"specs/{area_name}.area.json (or .contract.json) not found.")
        return
    if isinstance(area_data, dict) and "__parse_error__" in area_data:
        add(findings, FAIL, "meta", "parse-error", area_name,
            f"spec for '{area_name}' failed to parse: {area_data['__parse_error__']}")
        return

    check_schema(area_data, schema_validator, area_name, findings)
    check_area_meta(area_data, area_name, findings)
    check_ids(area_data, area_name, findings)
    check_ears(area_data, area_name, findings)
    check_ambiguity(area_data, area_name, findings)
    check_state_binding(area_data, area_name, findings)
    check_fit_criteria(area_data, area_name, findings)
    check_witnesses(root, area_data, area_name, findings)
    check_quint_refs(area_data, sidecar, area_name, findings)
    check_orphan_actions(area_data, sidecar, area_name, findings)
    check_constraint_values(area_data, sidecar, area_name, findings)
    check_formal_model_consistency(area_data, sidecar, area_name, findings)
    check_cross_refs(area_data, all_areas, area_name, findings)
    check_contract_spans(area_data, all_areas, area_name, findings)
    check_open_questions(area_data, area_name, findings)
    check_critical_invariants(area_data, area_name, findings)
    check_architecture_patterns_protocols(area_data, catalog, area_name, findings)
    check_components_implementation(area_data, area_name, findings)
    check_ui_navigation(area_data, area_name, findings)
    check_state_machines(area_data, sidecar, area_name, findings)


def load_catalog(root, kind):
    """Return {name: bool} of available catalog entries in .spec/<kind>/."""
    d = root / ".spec" / kind
    if not d.exists():
        return {}
    out = {}
    for p in sorted(d.glob("*.json")):
        data = load_json(p)
        if isinstance(data, dict) and "name" in data:
            out[data["name"]] = True
        else:
            out[p.stem] = True
    return out


# ── Reporting ─────────────────────────────────────────────────────────────────

ICONS = {PASS: "✓", WARN: "⚠", FAIL: "✗"}
COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
RESET = "\033[0m"


def colorize(text, severity, use_color):
    if not use_color:
        return text
    return f"{COLORS[severity]}{text}{RESET}"


def print_report(findings, areas, use_color=True):
    by_area = defaultdict(list)
    for f in findings:
        by_area[f.area].append(f)

    for area in sorted(set(areas) | set(by_area.keys())):
        items = by_area.get(area, [])
        if not items:
            print(colorize(f"{ICONS[PASS]} {area}: clean", PASS, use_color))
            continue
        fail = sum(1 for f in items if f.severity == FAIL)
        warn = sum(1 for f in items if f.severity == WARN)
        sev = FAIL if fail else WARN
        print(colorize(f"{ICONS[sev]} {area}: {fail} fail, {warn} warn", sev, use_color))
        for f in items:
            ref = f" [{f.ref}]" if f.ref else ""
            print(f"    {ICONS[f.severity]} {f.category}/{f.check}{ref}: {f.description}")

    total_fail = sum(1 for f in findings if f.severity == FAIL)
    total_warn = sum(1 for f in findings if f.severity == WARN)
    print()
    summary = f"Total: {total_fail} fail, {total_warn} warn"
    sev = FAIL if total_fail else (WARN if total_warn else PASS)
    print(colorize(summary, sev, use_color))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="spec-lint: consistency checker")
    parser.add_argument("area", nargs="?", help="Area to lint (default: all from .spec/project.json)")
    parser.add_argument("--json", dest="emit_json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on warnings too")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    root = Path(args.root)
    project_path = root / ".spec" / "project.json"
    project_data = load_json(project_path)

    if project_data is None:
        print(f"ERROR: {project_path} not found. Run /spec to set up the project.", file=sys.stderr)
        sys.exit(2)
    if isinstance(project_data, dict) and "__parse_error__" in project_data:
        print(f"ERROR: {project_path} failed to parse: {project_data['__parse_error__']}", file=sys.stderr)
        sys.exit(2)

    all_area_names = [a["name"] for a in project_data.get("areas", []) if a.get("name")]

    if args.area:
        if args.area not in all_area_names:
            print(f"ERROR: area '{args.area}' not in .spec/project.json. Known: {', '.join(all_area_names)}", file=sys.stderr)
            sys.exit(2)
        target_areas = [args.area]
    else:
        target_areas = all_area_names

    # Load all areas (needed for cross-ref resolution). The filename suffix
    # (.area.json / .contract.json) must match the JSON's kind field.
    all_areas = {}
    sidecars = {}
    findings = []
    for a in all_area_names:
        path = area_json_path(root, a)
        all_areas[a] = load_json(path) if path.exists() else None
        sidecars[a] = parse_sidecar(root / "specs" / f"{a}.qnt")
        both = [k for k in ("area", "contract")
                if (root / "specs" / f"{a}.{k}.json").exists()]
        if len(both) > 1:
            add(findings, FAIL, "meta", "duplicate-spec-file", a,
                f"Both specs/{a}.area.json and specs/{a}.contract.json exist — "
                f"delete one; the suffix encodes the kind.")
        elif both and isinstance(all_areas[a], dict) \
                and all_areas[a].get("kind") in ("area", "contract") \
                and all_areas[a]["kind"] != both[0]:
            add(findings, FAIL, "meta", "kind-suffix-mismatch", a,
                f"specs/{a}.{both[0]}.json has kind '{all_areas[a]['kind']}' — "
                f"rename the file to specs/{a}.{all_areas[a]['kind']}.json.")

    catalog = {
        "patterns":  load_catalog(root, "patterns"),
        "protocols": load_catalog(root, "protocols"),
    }

    schema_validator = build_schema_validator(root)

    if _jsonschema is None:
        add(findings, WARN, "schema", "jsonschema-unavailable", "_project",
            "jsonschema lib not installed — schema validation SKIPPED. Other "
            "checks defer malformed-shape detection to it; install with "
            "'pip install jsonschema' for full coverage.")

    # Validate the project config itself.
    check_schema(project_data, build_schema_validator(root, "project.schema.json"),
                 "_project", findings)

    for a in target_areas:
        lint_area(root, a, all_areas[a], sidecars[a], all_areas, catalog, findings,
                  schema_validator)

    # Topology, change-manifest, and journey checks run once per project, on
    # EVERY invocation (they're cheap) — a single-area run must not report
    # clean while a manifest references a dangling ID in that very area.
    check_topology(project_data, all_areas, findings)
    check_changes(root, all_areas, findings,
                  build_schema_validator(root, "change.schema.json"))
    check_journeys(root, all_areas, findings,
                   build_schema_validator(root, "journey.schema.json"))

    if args.emit_json:
        print(json.dumps({
            "summary": {
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "areas": target_areas,
                "fail": sum(1 for f in findings if f.severity == FAIL),
                "warn": sum(1 for f in findings if f.severity == WARN),
            },
            "findings": [f.to_dict() for f in findings],
        }, indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print_report(findings, target_areas, use_color)

    has_fail = any(f.severity == FAIL for f in findings)
    has_warn = any(f.severity == WARN for f in findings)
    if has_fail or (args.strict and has_warn):
        sys.exit(1)


if __name__ == "__main__":
    main()
