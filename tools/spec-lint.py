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
    "ASM":  re.compile(r"^ASM-\d{3}$"),
    "EX":   re.compile(r"^EX-\d{3}$"),
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
    from quint_ir import cli_available as _quint_cli_available
    from quint_ir import DEFAULT_ENGINE as _quint_engine
    # Shared rejection definition — lint, spec-record and the readback must
    # not disagree about which requirements owe a refusal artifact.
    from itf_tools import is_rejection, witness_entries, skip_discharge
    from itf_tools import brief_status as _brief_status
    from itf_tools import meaning_status as _meaning_status
    from itf_tools import compute_model_sha as _compute_model_sha
    from itf_tools import compute_spec_sha as _compute_spec_sha
    from itf_tools import load_trace as _load_trace
    from itf_tools import is_hollow as _is_hollow
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
        "runs":             set(ir.get("runs") or []),
        "action_params":    ir.get("action_params") or {},
        "type_variants":    ir.get("type_variants") or {},
        "vars":             set(ir["vars"]),
        "action_mutations": ir["action_mutations"],
        # Left as None, not {}, when the parser did not report one: the
        # orphan-action reachability walk treats absent differently from
        # empty, and an empty graph would call every action unreachable.
        "action_calls":     ir.get("action_calls"),
        "const_values":     ir.get("const_values") or {},
        # The EARS↔model bridge: which var holds a declared state
        # (var_types × type_variants), what each action reads, and which
        # variants any assignment can actually produce.
        "var_types":        ir.get("var_types") or {},
        "action_reads":     ir.get("action_reads") or {},
        "action_preserves": ir.get("action_preserves") or {},
        "action_produces":  ir.get("action_produces") or {},
        "produced_variants": ir.get("produced_variants") or [],
        # Which engine answered. `action_reads` is only complete under the
        # CLI parser, so a check that FAILs on a missing read has to know.
        "source":           ir.get("source"),
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
        ("assumptions", "ASM"), ("examples", "EX"),
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


def check_quint_refs(area_data, sidecar, area_name, findings, probes=None):
    """Each requirement's quint_ref maps to a real action; each invariant's
    quint_name maps to a real val/invariant/temporal.

    An invariant declared `over: "probes"` is looked up in the PROBE module
    instead: it reads the `_prev*` ghosts, so its val lives where those are
    declared. Checking it against the model sidecar would FAIL every correct
    transition property."""
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
        if not name:
            continue
        if (inv.get("over") or "model") == "probes":
            if probes is None or "__no_module__" in probes:
                add(findings, FAIL, "quint", "invariant-over-probes-unparseable",
                    area_name,
                    f"{inv.get('id', '?')} declares over: 'probes' but the probe "
                    f"module is missing or unparseable — the invariant cannot be "
                    f"checked at all. Generate it with tools/spec-probes.py.",
                    ref=inv.get("id"))
            elif name not in probes["named"]:
                add(findings, FAIL, "quint", "invariant-quint-missing", area_name,
                    f"{inv.get('id', '?')}.quint_name '{name}' has no matching "
                    f"val/invariant in the PROBE module (it declares over: "
                    f"'probes'). Transition invariants live alongside the "
                    f"`_prev*` ghosts they read.",
                    ref=inv.get("id"))
            continue
        if name not in sidecar["named"]:
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
    no-witness result. WARN during authoring; FAIL once the area reaches
    in-review/approved — 'approved' must mean precise, not precise-ish."""
    gating = area_data.get("status") in ("in-review", "approved")
    severity = FAIL if gating else WARN
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred":
            continue
        response = (req.get("ears") or {}).get("response") or ""
        hits = sorted({m.group(0).lower() for m in AMBIGUOUS_TERMS.finditer(response)})
        if hits:
            add(findings, severity, "ears", "ambiguous-response", area_name,
                f"{rid}.ears.response contains untestable wording: {', '.join(hits)}. "
                f"Sharpen: what state results, visible where?",
                ref=rid)


def check_state_binding(area_data, area_name, findings):
    """ears.state should be phrased with a DECLARED entity state name
    ('the Session is Active', not 'the user is logged in') — that's what
    makes the requirement↔state-machine link reviewable and matrix triage
    mechanical. Only fires when the area declares states at all."""
    # Shared with the EARS↔model bridge checks: two definitions of "a
    # declared state" drifting apart would let one check disagree with
    # another about the same sentence.
    declared = declared_states(area_data)
    if not declared:
        return
    gating = area_data.get("status") in ("in-review", "approved")
    severity = FAIL if gating else WARN
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
            add(findings, severity, "ears", "state-not-bound", area_name,
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
        if not predicate and req.get("modality") == "forbidden" and witness.get("enforced_by"):
            # A prohibition has no state change to witness — that is the whole
            # reason it names an enforcing invariant instead. Demanding a
            # predicate here failed every correctly-written prohibition before
            # spec-record had even run (which is what sets status 'skipped').
            # check_modality separately FAILs a forbidden REQ with no
            # enforced_by, and a dangling one.
            continue
        if not predicate and req.get("modality") == "may":
            # A 'may' requirement carries one predicate per permitted outcome
            # instead of a single one; the gate is satisfied when they exist
            # (check_modality enforces that there are at least two, each with
            # a predicate). Reading only witness.predicate here would FAIL
            # every correctly-written permission.
            outs = witness.get("outcomes") or []
            if outs and all(o.get("predicate") for o in outs):
                predicate = outs[0]["predicate"]
        wstatus = witness.get("status", "not-run")
        # A `may` requirement's traces live one per permitted outcome; reading
        # only witness.trace would FAIL every correctly written permission.
        entries = witness_entries(req)
        per_outcome = len(entries) > 1 or bool(witness.get("outcomes"))

        if wstatus == "skipped":
            # Deliberate opt-out — legitimate for rejection requirements
            # (no state change to witness; an invariant carries the proof).
            # Only a JUSTIFIED skip discharges the obligation.
            if skip_discharge(witness) is None:
                add(findings, FAIL, "witness", "skipped-no-justification", area_name,
                    f"{rid}.witness is skipped with neither witness.enforced_by "
                    f"nor a justification — an undischarged skip proves nothing. "
                    f"Rejection requirement? Set modality 'forbidden' and "
                    f"witness.enforced_by to the invariant that enforces it.",
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

        for label, entry in entries:
            # For a `must` requirement this loop runs once over the witness
            # itself, so the single-witness behaviour is unchanged. For a
            # `may` it runs once per permitted outcome, where the traces
            # actually live.
            e_trace = entry.get("trace")
            e_status = entry.get("status", "not-run") if per_outcome else wstatus
            if e_trace:
                trace_path = root / "specs" / e_trace
                if not trace_path.exists():
                    add(findings, FAIL, "witness", "witness-trace-missing", area_name,
                        f"{label}.witness.trace '{e_trace}' does not exist under "
                        f"specs/. Re-run /spec-check to regenerate it.", ref=rid)
                elif e_status == "witnessed":
                    trace_obj, trace_errs = _load_trace(trace_path)
                    if trace_errs:
                        add(findings, FAIL, "witness", "witness-trace-invalid",
                            area_name,
                            f"{label}.witness.trace '{e_trace}' is not a valid ITF "
                            f"trace: {trace_errs[0]}", ref=rid)
                    elif _is_hollow(trace_obj):
                        # The stamp claims a witness; the trace shows a single
                        # state, i.e. the predicate already held before
                        # anything ran. Checked here as well as in the recorder
                        # so a status hand-edited into the JSON, or minted
                        # before this gate existed, still cannot pass review.
                        add(findings, FAIL, "witness", "witness-hollow", area_name,
                            f"{label}.witness.trace '{e_trace}' has ONE state: the "
                            f"predicate was already true in the initial state, so "
                            f"the trace proves the starting position satisfies it, "
                            f"not that the behavior happens. Add a witness.delta so "
                            f"the probe must show the state moving, and sharpen the "
                            f"predicate if it cannot tell before from after.",
                            ref=rid)
                    stamped = entry.get("model_sha")
                    if not stamped:
                        add(findings, FAIL, "witness", "witness-unstamped", area_name,
                            f"{label}.witness has no model_sha — freshness can't be "
                            f"checked, so the 'every witness fresh' obligation is "
                            f"unenforceable. Re-run /spec-check to pin it.", ref=rid)
                    elif current_sha and stamped != current_sha:
                        add(findings, FAIL, "witness", "witness-stale", area_name,
                            f"{label}.witness.model_sha doesn't match the current "
                            f"model (.qnt/.probes.qnt changed since the trace was "
                            f"found). The trace proves nothing about the current "
                            f"model — re-run /spec-check.", ref=rid)
            elif e_status == "witnessed":
                add(findings, FAIL, "witness", "witnessed-without-trace", area_name,
                    f"{label}.witness.status is 'witnessed' but no trace file is "
                    f"recorded.", ref=rid)

        if wstatus == "hollow":
            add(findings, FAIL, "witness", "witness-hollow-status", area_name,
                f"{rid}: /spec-check found only a HOLLOW witness — the predicate "
                f"already held in the initial state, so nothing was demonstrated. "
                f"Add witness.delta (a pre-state the step must start from, over the "
                f"`_prev*` ghosts) and re-run.",
                ref=rid)

        if wstatus == "no-witness":
            add(findings, FAIL, "witness", "no-witness-found", area_name,
                f"{rid}: /spec-check found NO witness — the behavior is unreachable "
                f"in the model (impossible guard or missing action?). Fix the model "
                f"or the requirement.",
                ref=rid)


def check_predicate_sanity(area_data, sidecar, sidecars, area_name, findings):
    """A witness.predicate must be a real postcondition. spec-lint can't judge
    full semantics, but it kills the two fakes that otherwise sail the whole
    pipeline as a green 'witnessed':

      - a CONSTANT predicate. `true` makes the probe
        `not(true and _lastAction == X)` == `not(_lastAction == X)`, which the
        checker violates the instant action X fires — so the requirement is
        'witnessed' having demonstrated nothing but that its action runs.
      - a predicate naming NO state variable — it can't assert an observable
        state change, so it isn't a postcondition.

    Both degrade the anti-vacuity guarantee ('every claim a witness') to the
    far weaker 'every action fires'. This check is the backstop."""
    if not sidecar or sidecar.get("__no_module__"):
        return
    # Available state vars: this module's, plus spanned modules' (a contract
    # or UI predicate ranges over imported state).
    vars_avail = set(sidecar.get("vars") or set())
    for span in area_data.get("spans") or []:
        sp = (sidecars or {}).get(span) or {}
        vars_avail |= set(sp.get("vars") or set())
    mutations = sidecar.get("action_mutations") or {}
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred" or req.get("type") == "non-functional":
            continue
        witness = req.get("witness") or {}
        if witness.get("status") == "skipped":
            continue
        # A 'may' requirement keeps one predicate per permitted outcome. Each
        # gets the same scrutiny as a single one, or `true` would sail through
        # simply by being written a level deeper.
        preds = [(rid, (witness.get("predicate") or "").strip())]
        for oc in (witness.get("outcomes") or []):
            preds.append((f"{rid}/{oc.get('name', '?')}",
                          (oc.get("predicate") or "").strip()))
        for label, pred in preds:
            if pred:
                sanity_one_predicate(label, pred, req, vars_avail, mutations,
                                     area_name, findings)


def sanity_one_predicate(rid, pred, req, vars_avail, mutations, area_name, findings):
    """The two fakes, checked against one predicate."""
    bare = pred
    while bare.startswith("(") and bare.endswith(")"):
        bare = bare[1:-1].strip()
    if bare in ("true", "false"):
        add(findings, FAIL, "witness", "predicate-constant", area_name,
            f"{rid}.witness.predicate is the constant `{bare}` — it witnesses "
            f"nothing (the probe reduces to 'the action fired'). Write a boolean "
            f"over state that is true exactly when the behavior has occurred.",
            ref=rid)
        return
    referenced = {v for v in vars_avail if re.search(rf"\b{re.escape(v)}\b", pred)}
    # Only assert no-state when we actually know the var set — an empty
    # vars_avail (unparseable spanned sidecar) must not false-FAIL.
    if vars_avail and not referenced:
        add(findings, FAIL, "witness", "predicate-no-state", area_name,
            f"{rid}.witness.predicate references no state variable "
            f"({', '.join(sorted(vars_avail))}) — it can't be a postcondition. "
            f"A witness must assert an observable state change.",
            ref=rid)
        return
    # WARN: predicate names no var the requirement's OWN action assigns —
    # it may be witnessing a side condition, not this requirement's effect.
    qref = req.get("quint_ref")
    assigned = set(mutations.get(qref) or []) if qref else set()
    if assigned and referenced and not (referenced & assigned):
        add(findings, WARN, "witness", "predicate-off-action", area_name,
            f"{rid}.witness.predicate references {sorted(referenced)} but its action "
            f"`{qref}` assigns {sorted(assigned)} — the predicate may not capture "
            f"this requirement's own effect. Confirm it's the right postcondition.",
            ref=rid)


def declared_states(area_data):
    """Entity states the area declares, from concepts and state machines."""
    declared = set()
    for ent in (area_data.get("concepts") or {}).get("entities", []) or []:
        declared.update(ent.get("states") or [])
    for sm in area_data.get("state_machines", []) or []:
        declared.update(s.get("name") for s in sm.get("states") or [] if s.get("name"))
    return {s for s in declared if s}


def _state_to_vars(sidecar):
    """Declared state name -> the vars that can hold it.

    Two hops through the IR: `type_variants` says which type declares the
    variant `Locked`, `var_types` says which vars are annotated with that
    type. `var accountStatus: UserId -> AccountStatus` therefore answers
    "which variable does 'the account is Locked' talk about" without anyone
    writing that mapping down."""
    if not sidecar or "__no_module__" in sidecar:
        return {}
    holders = {}
    for type_name, variants in (sidecar.get("type_variants") or {}).items():
        for var, annotation in (sidecar.get("var_types") or {}).items():
            if type_name in (annotation or []):
                for variant in variants:
                    holders.setdefault(variant, set()).add(var)
    return holders


def _states_named(text, states):
    """Declared state names appearing in a sentence, word-boundary matched.
    Case-sensitive on purpose: state names are identifiers in the sidecar,
    and 'locked' in prose is a word while `Locked` is the variant."""
    if not text:
        return set()
    return {s for s in states
            if re.search(rf"\b{re.escape(s)}\b", text)}


def check_ears_guard_correspondence(area_data, sidecar, area_name, findings):
    """ears.state names a state; the requirement's own action must READ the
    variable that holds it.

    `state-not-bound` already checks that the sentence names a declared
    state. This checks the other half — that the model actually gates on it.
    A requirement saying "While the account is Locked" whose action never
    looks at `accountStatus` is not a translation of that sentence, and that
    is a mechanical fact, not a judgement about prose.

    CLI engine only. The regex fallback scans action bodies and cannot see a
    var a guard reaches through a helper (`not(isLocked(uid))`), so under it
    a correct model would fail this check."""
    if not sidecar or "__no_module__" in sidecar:
        return
    if sidecar.get("source") != "quint-cli":
        return
    states = declared_states(area_data)
    holders = _state_to_vars(sidecar)
    if not states or not holders:
        return
    reads = sidecar.get("action_reads") or {}
    gating = area_data.get("status") in ("in-review", "approved")
    severity = FAIL if gating else WARN
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred" or req.get("type") == "non-functional":
            continue
        action = req.get("quint_ref")
        if not action or action not in reads:
            continue
        named = _states_named((req.get("ears") or {}).get("state"), states)
        for state in sorted(named):
            candidates = holders.get(state) or set()
            if not candidates:
                continue
            if candidates & set(reads.get(action) or []):
                continue
            add(findings, severity, "ears", "guard-not-in-action", area_name,
                f"{rid}.ears.state names '{state}', which lives in "
                f"{sorted(candidates)}, but its action `{action}` never reads "
                f"{'that variable' if len(candidates) == 1 else 'any of those'} "
                f"— the model does not gate on the precondition the sentence "
                f"states. Add the guard, or fix the sentence.",
                ref=rid)


PRESERVE_TERMS = re.compile(
    r"\b(leave|leaves|remain|remains|stay|stays|keep|keeps|unchanged|"
    r"still|continue|continues|preserve|preserves|retain|retains)\b",
    re.IGNORECASE)


def check_ears_effect_correspondence(area_data, sidecar, area_name, findings):
    """ears.response names a state; the requirement's own action must write
    the variable that holds it.

    The effect half of the bridge. `predicate-off-action` already asks
    whether the WITNESS touches what the action assigns; this asks whether
    the SENTENCE does.

    Two kinds of response, and the difference is load-bearing:

      - a CHANGE ("shall lock the account") is implemented by a mutation,
        so the var must appear in action_mutations;
      - a PRESERVATION ("shall LEAVE the subscription Active") is
        implemented by an identity assignment `x' = x`, which
        action_mutations deliberately excludes. Demanding a mutation there
        would fail the model for being right.

    So a preservation response is checked against action_preserves instead —
    which makes the check stronger, not weaker: a response promising the
    state is left alone, over an action that assigns it, is a contradiction
    this reports rather than a case it skips. Both engines: mutations and
    preserves are computed by the regex fallback too.

    Granularity follows the engine, because a claim is only worth making at
    the precision the parser supports. Under the CLI parser the check is
    VARIANT-level: the response says 'Expired', so the action's own
    assignment must build `Expired`. The regex fallback reads assignments
    line by line and misses a variant built across a continuation, so under
    it the check drops to VAR-level — the action must write the variable
    that holds the state. Weaker, never wrong.

    The preservation/change split is read off the prose with a wordlist —
    the one soft edge here, and the reason a miss lands as a finding about
    the sentence rather than about the model."""
    if not sidecar or "__no_module__" in sidecar:
        return
    states = declared_states(area_data)
    holders = _state_to_vars(sidecar)
    if not states or not holders:
        return
    mutations = sidecar.get("action_mutations") or {}
    preserves = sidecar.get("action_preserves") or {}
    produces = (sidecar.get("action_produces") or {}
                if sidecar.get("source") == "quint-cli" else {})
    gating = area_data.get("status") in ("in-review", "approved")
    severity = FAIL if gating else WARN
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred" or req.get("type") == "non-functional":
            continue
        if req.get("modality") == "forbidden":
            # A prohibition's response describes what does NOT happen. Its
            # proof is an invariant, and demanding an assignment here would
            # ask the model to implement the thing it forbids.
            continue
        action = req.get("quint_ref")
        if not action or action not in mutations:
            continue
        response = (req.get("ears") or {}).get("response")
        keeping = bool(response and PRESERVE_TERMS.search(response))
        wrote = set(mutations.get(action) or [])
        held = set(preserves.get(action) or [])
        for state in sorted(_states_named(response, states)):
            candidates = holders.get(state) or set()
            if not candidates:
                continue
            if keeping:
                if candidates & held:
                    continue
                if candidates & wrote:
                    add(findings, severity, "ears", "effect-contradicts-action",
                        area_name,
                        f"{rid}.ears.response says '{state}' is left as it is, "
                        f"but its action `{action}` assigns "
                        f"{sorted(candidates & wrote)} — the sentence promises "
                        f"no change and the model makes one.",
                        ref=rid)
                    continue
            elif candidates & wrote:
                # The action writes the right variable. Where the parser can
                # see WHICH variant it builds, ask the sharper question.
                built = produces.get(action)
                if not built or state in built:
                    continue
                add(findings, severity, "ears", "effect-wrong-variant", area_name,
                    f"{rid}.ears.response says the system reaches '{state}', but "
                    f"its action `{action}` writes {sorted(candidates & wrote)} "
                    f"to {sorted(built)} and never to '{state}' — the model "
                    f"moves the right variable to the wrong state.",
                    ref=rid)
                continue
            add(findings, severity, "ears", "effect-not-in-action", area_name,
                f"{rid}.ears.response says the system reaches '{state}', which "
                f"lives in {sorted(candidates)}, but its action `{action}` "
                f"assigns {sorted(wrote) or 'nothing'} — the model never writes "
                f"the state the sentence promises.",
                ref=rid)


def check_unproducible_states(area_data, sidecar, area_name, findings):
    """A declared state no assignment in the model ever produces.

    The spec claims the system enters this state; nothing in the sidecar can
    put it there. Every requirement, invariant and matrix cell mentioning it
    is then vacuously satisfied — the failure mode witnesses exist to catch,
    one level up, at the state itself.

    WARN, not FAIL, and deliberately: `produced_variants` counts variants
    appearing on the right of an assignment. A state built only inside a
    helper's RETURN value, never named in an assignment, is missed — so a
    finding here is 'look at this', not 'this is broken'."""
    if not sidecar or "__no_module__" in sidecar:
        return
    variants = {v for vs in (sidecar.get("type_variants") or {}).values() for v in vs}
    if not variants:
        return
    produced = set(sidecar.get("produced_variants") or [])
    states = declared_states(area_data)
    for state in sorted(states & variants):
        if state in produced:
            continue
        add(findings, WARN, "quint", "state-never-produced", area_name,
            f"Declared state '{state}' is a variant in the sidecar, but no "
            f"assignment in the model ever produces it — nothing can enter "
            f"it, so every requirement and invariant mentioning it holds "
            f"vacuously. Add the transition, or drop the state.",
            ref=state)


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
    # Reachability, not direct mention. An action the model actually calls is
    # not dead text, whatever the JSON says about it: `step` dispatching to a
    # per-branch wrapper is the ordinary shape once actions take differing
    # parameters, and crediting only JSON references flagged every one of
    # those wrappers. Roots are the model's own entry points plus everything
    # the JSON names; anything reachable from a root is live.
    calls = sidecar.get("action_calls")
    declared = set(sidecar["actions"])
    if calls is None:
        # Parser too old to report a call graph — fall back to the direct
        # check rather than silently calling every action reachable.
        reachable = set(referenced)
    else:
        reachable, stack = set(), list((referenced | PLUMBING_ACTIONS) & declared)
        stack += [a for a in referenced if a not in declared]
        while stack:
            action = stack.pop()
            if action in reachable:
                continue
            reachable.add(action)
            stack.extend(calls.get(action, []))
    for action in sorted(declared - reachable - PLUMBING_ACTIONS):
        add(findings, WARN, "quint", "orphan-action", area_name,
            f"Action '{action}' is unreachable — no requirement, transition or "
            f"lifecycle_action names it, and no action the model runs calls it. "
            f"Missing requirement or dead spec text.",
            ref=action)


def check_formal_model_consistency(area_data, sidecar, area_name, findings):
    """formal_model.quint_file must point at a parseable module. (Module name
    and imports are read from the sidecar via quint_ir — never mirrored in
    the JSON, so there's nothing else to reconcile.)

    Graded by status like the other precision lints: while an area is being
    authored the pointer legitimately runs ahead of the sidecar — /spec
    scaffolds the pointer at bootstrap, /spec-check writes the file — so this
    WARNs and reads as "formalize next", which is exactly the next action the
    /spec triage table already prescribes for it. It FAILs from in-review on,
    where an aimed pointer with no module means the area is up for review
    claiming a formal model it does not have.
    """
    fm = area_data.get("formal_model") or {}
    if sidecar and "__no_module__" in sidecar:
        # The file is THERE and the parser got nothing out of it. That is a
        # broken model at any authoring stage, never a not-yet-written one,
        # and it silences every check that reads a sidecar — so it is always
        # a FAIL, and it is never graded by status.
        add(findings, FAIL, "quint", "sidecar-unparseable", area_name,
            "The sidecar exists but yielded no module. Either the Quint is "
            "malformed, or the configured parser is unavailable — check for a "
            "quint/engine-unavailable finding before editing the model. Every "
            "check that reads the sidecar is skipped meanwhile.")
    elif not sidecar and fm.get("quint_file"):
        gating = at_review(area_data)
        add(findings, FAIL if gating else WARN, "quint",
            "sidecar-missing-or-empty", area_name,
            "formal_model.quint_file is set but the sidecar file does not exist."
            + ("" if gating else " Write it with /spec-check."))


ALS_COMMAND_RE = re.compile(
    r"^\s*(?P<kind>check|run)\s+(?P<name>[A-Za-z_][A-Za-z_0-9']*)\b(?P<rest>.*)$")


def parse_als(text):
    """Static view of an Alloy sidecar: {name: {"kind", "scope"}} for every
    NAMED check/run command. Regex-only and deterministic, deliberately
    mirroring quint_ir's fallback role — lint must stay Python-only (Tier 1),
    and the runner never relies on this: it reads verdicts from the CLI's own
    receipt.json. scope is None when the command declares no `for` clause,
    which matters because Alloy then silently defaults to scope 3.

    Anonymous commands (`check { ... } for 3`) are skipped: an invariant has
    to name the command it maps to, so an unnamed one can carry no ID."""
    out = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or stripped.startswith("//"):
            continue
        m = ALS_COMMAND_RE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        fm = re.search(r"\bfor\b(?P<scope>[^{]*)$", rest)
        scope = fm.group("scope").strip() if fm else None
        out[m.group("name")] = {"kind": m.group("kind"), "scope": scope or None}
    return out


def check_alloy_backend(root, area_data, area_name, findings):
    """The optional structural backend, gated so it can never silently
    half-work: an invariant claiming `proof: structural` must actually resolve
    to a runnable Alloy command, or its status would be decided by nothing.

    Deliberately not checked here: whether the Alloy assertion means the same
    thing as the invariant's prose. That is the same human-reviewed step the
    Quint mapping has, and pretending otherwise would fake a guarantee."""
    fm = area_data.get("formal_model") or {}
    als_rel = fm.get("alloy_file")
    invs = area_data.get("invariants", []) or []
    structural = [i for i in invs if i.get("proof") == "structural"]

    if not structural and not als_rel:
        return

    commands, als_text = {}, None
    if als_rel:
        als_path = root / "specs" / als_rel
        if not als_path.exists():
            add(findings, FAIL, "alloy", "alloy-file-missing", area_name,
                f"formal_model.alloy_file '{als_rel}' does not exist.")
        else:
            try:
                als_text = als_path.read_text(encoding="utf-8")
            except OSError as exc:
                add(findings, FAIL, "alloy", "alloy-file-unreadable", area_name,
                    f"formal_model.alloy_file '{als_rel}' could not be read: {exc}")
            else:
                commands = parse_als(als_text)

    if als_rel and not structural:
        add(findings, WARN, "alloy", "alloy-file-unused", area_name,
            f"formal_model.alloy_file '{als_rel}' is declared but no invariant has "
            f"proof: 'structural' — nothing routes to the Alloy backend.")

    approved = area_data.get("status") in ("in-review", "approved")
    for inv in structural:
        iid = inv.get("id", "?")
        command = inv.get("alloy_command")
        if not command:
            add(findings, FAIL, "alloy", "structural-without-command", area_name,
                f"{iid} has proof: 'structural' but no alloy_command — there is "
                f"nothing for the backend to run.", ref=iid)
            continue
        if not als_rel:
            add(findings, FAIL, "alloy", "structural-without-sidecar", area_name,
                f"{iid} has proof: 'structural' but the area declares no "
                f"formal_model.alloy_file.", ref=iid)
            continue
        if als_text is None:
            continue  # sidecar problem already reported
        entry = commands.get(command)
        if entry is None:
            known = ", ".join(sorted(commands)) or "none"
            add(findings, FAIL, "alloy", "alloy-command-missing", area_name,
                f"{iid}.alloy_command '{command}' has no matching named check/run "
                f"in {als_rel} (found: {known}).", ref=iid)
            continue
        if entry["kind"] != "check":
            add(findings, FAIL, "alloy", "alloy-command-not-check", area_name,
                f"{iid}.alloy_command '{command}' is a `run`, not a `check`. An "
                f"invariant is refuted by finding a counterexample; a `run` "
                f"finding an instance would report the opposite verdict.", ref=iid)
            continue
        if entry["scope"] is None:
            # Alloy falls back to scope 3, and a bound nobody wrote down is a
            # bound nobody reviewed. Same escalation shape as the precision
            # lints: WARN while authoring, FAIL once the area is up for review.
            sev = FAIL if approved else WARN
            add(findings, sev, "alloy", "alloy-scope-implicit", area_name,
                f"{iid}.alloy_command '{command}' declares no `for` scope, so Alloy "
                f"silently uses its default of 3. State the scope explicitly.",
                ref=iid)


def local_ids(area_data):
    """Every ID this area declares, for resolving intra-area references."""
    out = set()
    for src in ("requirements", "invariants", "properties", "constraints",
                "decisions", "open_questions", "assumptions", "examples"):
        for item in area_data.get(src, []) or []:
            if item.get("id"):
                out.add(item["id"])
    return out


def resolve_ref(ref, area_data, all_areas):
    """True when `ref` names something real, in this area or another
    (qualified '<area>.<ID>' form). Unknown areas resolve as True so a
    partially-checked-out multi-repo project doesn't produce noise."""
    if "." in ref:
        other_area, other_id = ref.split(".", 1)
        other = (all_areas or {}).get(other_area)
        if not isinstance(other, dict) or "__parse_error__" in other:
            return True
        return other_id in local_ids(other)
    return ref in local_ids(area_data)


def at_review(area_data):
    """Precision lints WARN while authoring and FAIL once the area is up for
    review \u2014 'approved' has to mean precise, not precise-ish."""
    return area_data.get("status") in ("in-review", "approved")


def check_scope(area_data, area_name, findings):
    """Gap A. Completeness is only meaningful relative to a declared boundary.

    An OUT-OF-SCOPE triage verdict with nothing to point at is an assertion
    that cannot be reviewed \u2014 the one escape hatch in the completeness gate
    would otherwise absorb every awkward cell. Requiring a scope_ref turns it
    into a reference to a boundary someone agreed to."""
    scope = area_data.get("scope") or {}
    excluded = {e.get("item") for e in (scope.get("excluded") or []) if e.get("item")}
    review = at_review(area_data)

    for list_name, label in (("matrix_triage", "state\u00d7event"),
                             ("outcome_triage", "external\u00d7outcome")):
        for cell in area_data.get(list_name, []) or []:
            if cell.get("verdict") == "NO-OP" and not cell.get("reason"):
                where = (f"{cell.get('entity')}/{cell.get('state')}/{cell.get('event')}"
                         if list_name == "matrix_triage"
                         else f"{cell.get('external')}/{cell.get('outcome')}")
                add(findings, FAIL, "scope", "no-op-without-reason", area_name,
                    f"{label} cell {where} is triaged NO-OP but gives no reason. A "
                    f"deliberate no-op is a decision — say which requirement made it, "
                    f"or it is indistinguishable from an oversight.", ref=where)
            if cell.get("verdict") != "OUT-OF-SCOPE":
                continue
            where = (f"{cell.get('entity')}/{cell.get('state')}/{cell.get('event')}"
                     if list_name == "matrix_triage"
                     else f"{cell.get('external')}/{cell.get('outcome')}")
            ref = cell.get("scope_ref")
            if not ref:
                add(findings, FAIL if review else WARN, "scope", "out-of-scope-unanchored",
                    area_name,
                    f"{label} cell {where} is triaged OUT-OF-SCOPE but names no "
                    f"scope_ref. Declare the boundary in scope.excluded[] and point at it.",
                    ref=where)
            elif ref not in excluded:
                add(findings, FAIL, "scope", "scope-ref-dangling", area_name,
                    f"{label} cell {where} has scope_ref '{ref}', which is not in "
                    f"scope.excluded[] (declared: {', '.join(sorted(excluded)) or 'none'}).",
                    ref=where)


def check_externals(area_data, area_name, findings):
    """Gaps B + E. An external system with declared outcomes creates one
    obligation per outcome: some requirement says what happens, or triage says
    why it cannot. Integration failure modes are where real-world semantics go
    missing, so they get the same completeness treatment as state\u00d7event."""
    externals = area_data.get("externals", []) or []
    if not externals and not (area_data.get("outcome_triage") or []):
        for req in area_data.get("requirements", []) or []:
            if req.get("error_outcomes"):
                add(findings, FAIL, "externals", "error-outcome-without-external",
                    area_name,
                    f"{req.get('id', '?')} declares error_outcomes but the area declares "
                    f"no externals[]. Declare the dependency and its outcomes first.",
                    ref=req.get("id"))
        return

    declared = {}
    for ext in externals:
        name = ext.get("name")
        if not name:
            continue
        if name in declared:
            add(findings, FAIL, "externals", "duplicate-external", area_name,
                f"External '{name}' is declared twice.", ref=name)
        outcomes, seen = [], set()
        for oc in ext.get("outcomes", []) or []:
            oname = oc.get("name")
            if not oname:
                continue
            if oname in seen:
                add(findings, FAIL, "externals", "duplicate-outcome", area_name,
                    f"External '{name}' declares outcome '{oname}' twice.", ref=name)
            seen.add(oname)
            outcomes.append(oname)
        declared[name] = outcomes

    covered = set()
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        for eo in req.get("error_outcomes", []) or []:
            ext_name, outcome = eo.get("external"), eo.get("outcome")
            if ext_name not in declared:
                add(findings, FAIL, "externals", "unknown-external", area_name,
                    f"{rid}.error_outcomes references external '{ext_name}', which is "
                    f"not declared in externals[].", ref=rid)
                continue
            if outcome not in declared[ext_name]:
                add(findings, FAIL, "externals", "unknown-outcome", area_name,
                    f"{rid}.error_outcomes references '{ext_name}/{outcome}', which is not "
                    f"a declared outcome of that external "
                    f"(declared: {', '.join(declared[ext_name]) or 'none'}).", ref=rid)
                continue
            covered.add((ext_name, outcome))

    triaged = {}
    for cell in area_data.get("outcome_triage", []) or []:
        ext_name, outcome = cell.get("external"), cell.get("outcome")
        if ext_name not in declared:
            add(findings, FAIL, "externals", "triage-unknown-external", area_name,
                f"outcome_triage references external '{ext_name}', which is not declared.",
                ref=ext_name)
            continue
        if outcome not in declared[ext_name]:
            add(findings, FAIL, "externals", "triage-unknown-outcome", area_name,
                f"outcome_triage references '{ext_name}/{outcome}', which is not a declared "
                f"outcome of that external.", ref=ext_name)
            continue
        triaged[(ext_name, outcome)] = cell.get("verdict")

    # The completeness gate itself: every declared outcome is handled, triaged,
    # or \u2014 while the area is still being authored \u2014 flagged as unfinished.
    review = at_review(area_data)
    for ext_name, outcomes in declared.items():
        for outcome in outcomes:
            cell = (ext_name, outcome)
            if cell in covered:
                continue
            verdict = triaged.get(cell)
            if verdict is None:
                add(findings, FAIL if review else WARN, "externals", "outcome-unhandled",
                    area_name,
                    f"{ext_name}/{outcome} is declared but no requirement handles it and "
                    f"no outcome_triage entry explains why. What does the system do?",
                    ref=f"{ext_name}/{outcome}")
            elif verdict == "GAP":
                add(findings, FAIL if review else WARN, "externals", "outcome-open-gap",
                    area_name,
                    f"{ext_name}/{outcome} is triaged GAP \u2014 an acknowledged hole in the "
                    f"failure behavior. Resolve it before approval.",
                    ref=f"{ext_name}/{outcome}")


def check_assumptions(area_data, all_areas, area_name, findings):
    """Gap B. An assumption is what the spec relies on but does not establish.
    Left implicit, it turns every result that depends on it into an overclaim;
    written down, it can be pointed at from the readback's \u2713."""
    assumptions = area_data.get("assumptions", []) or []
    if not assumptions:
        return
    ext_names = {e.get("name") for e in (area_data.get("externals") or []) if e.get("name")}
    approved = area_data.get("status") == "approved"

    for asm in assumptions:
        aid = asm.get("id", "?")
        if not asm.get("statement"):
            add(findings, FAIL, "assumptions", "assumption-empty", area_name,
                f"{aid} has no statement.", ref=aid)
        ext = asm.get("external")
        if ext and ext not in ext_names:
            add(findings, FAIL, "assumptions", "assumption-unknown-external", area_name,
                f"{aid}.external '{ext}' is not a declared external.", ref=aid)
        for target in asm.get("affects", []) or []:
            if not resolve_ref(target, area_data, all_areas):
                add(findings, FAIL, "assumptions", "assumption-affects-dangling", area_name,
                    f"{aid}.affects references '{target}', which does not exist.", ref=aid)
        if asm.get("status") == "accepted-risk" and not asm.get("rationale"):
            add(findings, FAIL, "assumptions", "accepted-risk-without-rationale", area_name,
                f"{aid} is 'accepted-risk' but records no rationale. An assumption "
                f"knowingly carried as risk needs its argument written down, or the next "
                f"reader cannot tell a considered bet from an oversight.", ref=aid)
        if approved and asm.get("status", "accepted") == "open":
            add(findings, FAIL, "assumptions", "assumption-open-on-approved", area_name,
                f"{aid} is still 'open' on an approved area \u2014 an undecided assumption is "
                f"an undecided specification.", ref=aid)


def check_closed_worlds(area_data, sidecar, area_name, findings):
    """Gap C. `closed: true` says states[] is exhaustive. The claim is only
    worth anything if the model is held to it \u2014 otherwise a generated model
    (or generated code) quietly grows a state nobody specified, which is one
    of the specific ways LLM-assisted work drifts."""
    entities = ((area_data.get("concepts") or {}).get("entities") or [])
    closed = [e for e in entities if e.get("closed")]
    if not closed:
        return
    variants = (sidecar or {}).get("type_variants") or {}
    quint_types = {}
    for sm in area_data.get("state_machines", []) or []:
        if sm.get("entity") and sm.get("quint_type"):
            quint_types[sm["entity"]] = sm["quint_type"]

    for ent in closed:
        name = ent.get("name", "?")
        states = [s for s in (ent.get("states") or []) if s]
        if not states:
            add(findings, FAIL, "closed-world", "closed-without-states", area_name,
                f"Entity '{name}' is marked closed but declares no states[] \u2014 an "
                f"exhaustive list of nothing.", ref=name)
            continue
        qtype = quint_types.get(name)
        if not qtype:
            add(findings, WARN, "closed-world", "closed-unverifiable", area_name,
                f"Entity '{name}' is marked closed but no state_machines[] entry gives its "
                f"quint_type, so the model cannot be held to the list. Declare the state "
                f"machine, or drop the closed marker.", ref=name)
            continue
        if not sidecar or "__no_module__" in sidecar:
            continue
        if qtype not in variants:
            add(findings, WARN, "closed-world", "closed-type-not-found", area_name,
                f"Entity '{name}' is closed and names quint_type '{qtype}', but no variant "
                f"type by that name was found in the sidecar \u2014 the claim is unchecked.",
                ref=name)
            continue
        declared, modelled = set(states), set(variants[qtype])
        extra = sorted(modelled - declared)
        missing = sorted(declared - modelled)
        if extra:
            add(findings, FAIL, "closed-world", "closed-world-extra-state", area_name,
                f"Entity '{name}' is CLOSED over {sorted(declared)}, but the sidecar type "
                f"'{qtype}' also has {extra}. Either the state is real (add it to the JSON "
                f"and to whatever else the addition implies) or the model invented it.",
                ref=name)
        if missing:
            add(findings, FAIL, "closed-world", "closed-world-missing-state", area_name,
                f"Entity '{name}' declares states {missing} that the sidecar type '{qtype}' "
                f"does not have. A closed set must match the model exactly.", ref=name)


def check_modality(area_data, area_name, findings):
    """Gap D. MUST / MAY / FORBIDDEN decide what discharging the requirement
    means, so each needs its own evidence:

      must      \u2014 one witness trace (the existing obligation).
      may       \u2014 one witness PER permitted outcome. A single trace would show
                   that one allowed behavior is reachable, which is exactly how
                   a permission silently narrows into a requirement.
      forbidden \u2014 no witness can exist for a non-event, so it must name the
                   invariant carrying the proof, as an ID rather than prose.
    """
    inv_ids = {i.get("id") for i in (area_data.get("invariants") or []) if i.get("id")}
    review = at_review(area_data)
    draft = ("raw", "needs-validation")

    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        modality = req.get("modality", "must")
        witness = req.get("witness") or {}
        early = req.get("status", "raw") in draft

        if req.get("determinism") == "deterministic" and modality == "may":
            add(findings, FAIL, "modality", "may-but-deterministic", area_name,
                f"{rid} is modality 'may' (several outcomes permitted) but declares "
                f"determinism 'deterministic' (exactly one observable result). Pick one.",
                ref=rid)

        if modality == "forbidden":
            enforced = witness.get("enforced_by")
            if not enforced:
                if not early:
                    add(findings, FAIL, "modality", "forbidden-without-enforcer", area_name,
                        f"{rid} is modality 'forbidden' but names no witness.enforced_by. "
                        f"A non-event has no witness trace \u2014 the proof has to be the "
                        f"invariant that stays true.", ref=rid)
            elif enforced not in inv_ids:
                add(findings, FAIL, "modality", "forbidden-enforcer-dangling", area_name,
                    f"{rid}.witness.enforced_by '{enforced}' is not a declared invariant.",
                    ref=rid)
            if witness.get("predicate"):
                add(findings, WARN, "modality", "forbidden-with-predicate", area_name,
                    f"{rid} is 'forbidden' but carries a witness predicate. A forbidden "
                    f"behavior that IS witnessed is a contradiction \u2014 if the predicate is "
                    f"right, the requirement is not forbidden.", ref=rid)

        elif modality == "may":
            outcomes = witness.get("outcomes") or []
            if len(outcomes) < 2:
                add(findings, WARN if early else FAIL, "modality", "may-needs-outcomes",
                    area_name,
                    f"{rid} is modality 'may' but declares {len(outcomes)} witness "
                    f"outcome(s). A permission needs a witness per permitted outcome "
                    f"(at least two), or it is indistinguishable from a 'must'.", ref=rid)
            names = set()
            for oc in outcomes:
                oname = oc.get("name")
                if oname in names:
                    add(findings, FAIL, "modality", "duplicate-may-outcome", area_name,
                        f"{rid} declares witness outcome '{oname}' twice.", ref=rid)
                names.add(oname)
                if not oc.get("predicate"):
                    add(findings, FAIL, "modality", "may-outcome-without-predicate",
                        area_name,
                        f"{rid} witness outcome '{oname}' has no predicate \u2014 nothing to "
                        f"prove reachable.", ref=rid)
                elif review and oc.get("status") != "witnessed":
                    add(findings, FAIL, "modality", "may-outcome-unwitnessed", area_name,
                        f"{rid} witness outcome '{oname}' is '{oc.get('status', 'not-run')}'. "
                        f"Every permitted outcome must be demonstrated before approval.",
                        ref=rid)

        elif witness.get("status") == "skipped" and witness.get("justification") and review:
            # The old free-text escape hatch still works, but at review time say
            # so: an ID can be checked, a sentence cannot.
            add(findings, WARN, "modality", "untyped-skip-justification", area_name,
                f"{rid} skips its witness with a prose justification. If this is a "
                f"rejection requirement, set modality 'forbidden' and "
                f"witness.enforced_by so the link to the enforcing invariant is checkable.",
                ref=rid)


def check_decision_affects(area_data, all_areas, area_name, findings):
    """Gap H. A decision's blast radius is what makes reversing it tractable:
    when D-017 flips, these are the obligations back in play. A dangling entry
    means the radius is already wrong."""
    for dec in area_data.get("decisions", []) or []:
        for target in dec.get("affects", []) or []:
            if not resolve_ref(target, area_data, all_areas):
                add(findings, FAIL, "decisions", "decision-affects-dangling", area_name,
                    f"{dec.get('id', '?')}.affects references '{target}', which does not "
                    f"exist.", ref=dec.get("id"))


def check_examples(area_data, sidecar, all_areas, area_name, findings):
    """Gap I. Author-written examples are seeds and regressions, never proof \u2014
    but a broken one is worse than none, so the references have to hold."""
    examples = area_data.get("examples", []) or []
    if not examples:
        return
    have_module = sidecar and "__no_module__" not in sidecar
    for ex in examples:
        eid = ex.get("id", "?")
        action = (ex.get("when") or {}).get("action")
        if not action:
            add(findings, FAIL, "examples", "example-without-action", area_name,
                f"{eid}.when declares no action.", ref=eid)
        elif have_module and action not in sidecar["actions"]:
            add(findings, FAIL, "examples", "example-action-missing", area_name,
                f"{eid}.when.action '{action}' has no matching action in the sidecar.",
                ref=eid)
        if not (ex.get("expect") or {}):
            add(findings, WARN, "examples", "example-asserts-nothing", area_name,
                f"{eid} has an empty expect block \u2014 it exercises the action but claims "
                f"nothing about the result.", ref=eid)
        run = ex.get("quint_run")
        if run and have_module and run not in (sidecar.get("runs") or []):
            add(findings, FAIL, "examples", "example-run-missing", area_name,
                f"{eid}.quint_run '{run}' has no matching run in the sidecar.", ref=eid)
        for target in ex.get("refs", []) or []:
            if not resolve_ref(target, area_data, all_areas):
                add(findings, FAIL, "examples", "example-ref-dangling", area_name,
                    f"{eid}.refs references '{target}', which does not exist.", ref=eid)


def ghost_for(param):
    """Ghost var name for an action parameter: uid -> _lastUid.

    The convention is fixed rather than configurable precisely so this check
    can exist: a generated probe module and a hand-written predicate have to
    agree on the name without consulting each other."""
    return "_last" + param[:1].upper() + param[1:]


def check_witness_binding(area_data, sidecar, area_name, findings):
    """Rule 1. `_lastAction == login` pins WHICH action ran last, not that
    this call produced the postcondition.

    A bare existential — `sessions.keys().exists(s => sessions.get(s) == Active)`
    — is satisfied by a session some unrelated earlier call created. The
    requirement then goes green while its own action misbehaves, which is the
    failure the path constraint was supposed to prevent and does not. Binding
    the predicate to the param ghosts closes it: the postcondition must hold
    OF THE THING THIS CALL ACTED ON."""
    if not sidecar or sidecar.get("__no_module__"):
        return
    params_by_action = sidecar.get("action_params") or {}
    review = at_review(area_data)
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") in EARLY_STATUSES or req.get("status") == "deferred":
            continue
        if req.get("type") == "non-functional" or is_rejection(req):
            continue
        qref = req.get("quint_ref")
        params = params_by_action.get(qref) or []
        if not params:
            continue                      # parameterless action: nothing to bind to
        witness = req.get("witness") or {}
        preds = [witness.get("predicate") or ""]
        preds += [oc.get("predicate") or "" for oc in (witness.get("outcomes") or [])]
        ghosts = [ghost_for(x) for x in params]
        for pred in preds:
            if not pred.strip():
                continue
            if not any(re.search(rf"\b{re.escape(g)}\b", pred) for g in ghosts):
                add(findings, FAIL if review else WARN, "witness", "witness-unbound",
                    area_name,
                    f"{rid}.witness.predicate is not bound to the arguments `{qref}` was "
                    f"called with (expected one of {', '.join(ghosts)}). An unbound "
                    f"existential is satisfied by state an unrelated call produced, so "
                    f"the witness can pass while this requirement's own action is wrong.",
                    ref=rid)
                break          # one finding per requirement, not per predicate


def check_witness_delta(root, area_data, sidecar, area_name, findings):
    """Rule 2. A postcondition alone proves reachability, not causation.

    The vacuity case the methodology already covers is a guard too STRONG to
    fire. Its mirror image is a guard that already implies its own
    postcondition: the action becomes a no-op that can only fire once its
    result holds, the probe finds a state where the postcondition is true and
    that action ran, and dead text witnesses green. Requiring a pre-state
    delta means the trace has to show the state actually moving."""
    review = at_review(area_data)
    fm = area_data.get("formal_model") or {}
    probes_rel = fm.get("probes_file")
    probes_text = None
    if probes_rel:
        probes_path = Path(root) / "specs" / probes_rel
        if probes_path.exists():
            try:
                probes_text = probes_path.read_text(encoding="utf-8")
            except OSError:
                probes_text = None

    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") in EARLY_STATUSES or req.get("status") == "deferred":
            continue
        if req.get("type") == "non-functional" or is_rejection(req):
            continue
        witness = req.get("witness") or {}
        if not witness.get("predicate") and not (witness.get("outcomes") or []):
            continue                      # absence is the vagueness gate's job
        delta = (witness.get("delta") or {}).get("pre")
        if not delta:
            add(findings, FAIL if review else WARN, "witness", "witness-delta-missing",
                area_name,
                f"{rid} has no witness.delta.pre. Without a pre-state condition the "
                f"probe accepts a step that changed nothing \u2014 a guard implying its own "
                f"postcondition witnesses green over dead text.", ref=rid)
            continue
        # A recorded delta that the generated probe dropped is worse than none:
        # the JSON claims a check the model never performs.
        if probes_text is not None:
            probe = "witness_" + rid.replace("-", "_")
            idx = probes_text.find("val " + probe)
            if idx != -1:
                end = probes_text.find("val ", idx + 4)
                body = probes_text[idx:end if end != -1 else len(probes_text)]
                # Compare modulo whitespace: the check is about the
                # conjunct being present, not about how the generator
                # wrapped it across lines.
                def squash(t):
                    return re.sub(r"\s+", " ", t).strip()
                if squash(delta) not in squash(body):
                    add(findings, FAIL, "witness", "witness-delta-not-in-probe", area_name,
                        f"{rid}.witness.delta.pre is recorded but the generated probe "
                        f"`{probe}` does not contain it \u2014 the JSON claims a check the "
                        f"model does not make. Regenerate the probes module.", ref=rid)


def check_paired_invariants(root, area_data, sidecar, area_name, findings):
    """Rule 3. Reachability is one-sided.

    A witness shows a threshold CAN fire; nothing shows it cannot fire EARLY.
    With MAX_FAILED_ATTEMPTS = 5, changing the guard to `>= 1` leaves every
    invariant holding, the witness still found (in one step, which nothing
    looks at), and lint clean. Every numeric constant an action reads needs an
    invariant bounding it from the other side."""
    inv_ids_early = {i.get("id") for i in (area_data.get("invariants") or []) if i.get("id")}
    # The dangling-reference half needs no sidecar, so it runs first: an area
    # with no Quint model yet (a fresh brownfield extraction, for instance)
    # would otherwise skip the check entirely and keep a broken pointer.
    for con in area_data.get("constraints", []) or []:
        paired = con.get("paired_invariant")
        if paired and paired not in inv_ids_early:
            add(findings, FAIL, "constraints", "paired-invariant-dangling", area_name,
                f"{con.get('id', '?')}.paired_invariant '{paired}' is not a declared "
                f"invariant.", ref=con.get("id"))
    if not sidecar or sidecar.get("__no_module__"):
        return
    fm = area_data.get("formal_model") or {}
    qnt_rel = fm.get("quint_file") or f"{area_name}.qnt"
    qnt_path = Path(root) / "specs" / qnt_rel
    try:
        text = qnt_path.read_text(encoding="utf-8")
    except OSError:
        return
    inv_ids = {i.get("id") for i in (area_data.get("invariants") or []) if i.get("id")}
    review = at_review(area_data)
    for con in area_data.get("constraints", []) or []:
        name, cid = con.get("name"), con.get("id", "?")
        if not name or not isinstance(con.get("value"), (int, float)):
            continue
        # "Read by the model" = mentioned somewhere other than its own
        # declaration. Engine-independent: the raw sidecar text is the same
        # for the compiler IR and the regex fallback.
        hits = [m for m in re.finditer(rf"\b{re.escape(name)}\b", text)]
        decl = re.search(rf"^\s*(pure\s+)?(val|const)\s+{re.escape(name)}\b",
                         text, re.MULTILINE)
        used = len(hits) > (1 if decl else 0)
        paired = con.get("paired_invariant")
        if paired and paired not in inv_ids:
            continue          # already reported above, before the sidecar guard
        if used and not paired:
            add(findings, FAIL if review else WARN, "constraints",
                "constraint-without-paired-invariant", area_name,
                f"{cid} ({name}) is read by an action guard but names no "
                f"paired_invariant. A witness proves the threshold CAN fire; nothing "
                f"proves it cannot fire early \u2014 loosening the guard to `>= 1` would "
                f"pass every gate here.", ref=cid)


def check_refusal_artifacts(area_data, area_name, findings):
    """Rule 4. Rejections have no witness, and replay only replays witnesses.

    So the requirement class most likely to be wrong in the implementation had
    no code-side evidence at all: a login() written without the locked-account
    check replays every happy-path trace green, passes the tampered
    self-tests, and /spec-code-verify reports pass. The refusal artifact is the
    missing half \u2014 drive the code into the blocking state, attempt the call,
    assert it is refused AND that nothing observable moved."""
    review = at_review(area_data)
    for req in area_data.get("requirements", []) or []:
        if not is_rejection(req):
            continue
        rid = req.get("id", "?")
        refusal = req.get("refusal") or {}
        if not refusal.get("artifact"):
            add(findings, FAIL if review else WARN, "refusal", "refusal-without-artifact",
                area_name,
                f"{rid} is a rejection requirement with no refusal.artifact. It has no "
                f"witness trace by design, and conformance replay only replays witness "
                f"traces \u2014 so nothing in the chain checks the implementation actually "
                f"refuses.", ref=rid)
            continue
        if not refusal.get("unchanged"):
            add(findings, WARN, "refusal", "refusal-without-unchanged", area_name,
                f"{rid}.refusal names no `unchanged` vars. A rejection that throws "
                f"after incrementing the counter is not a rejection \u2014 list what must "
                f"be identical after the refused call.", ref=rid)
        if review and refusal.get("status") == "failing":
            add(findings, FAIL, "refusal", "refusal-failing", area_name,
                f"{rid}.refusal.status is 'failing' \u2014 the implementation does not "
                f"refuse.", ref=rid)


def check_provenance(area_data, area_name, findings):
    """Has the spec's MEANING moved since the code was generated from it?

    `generated_from.spec_content_sha` is a hash of the area's claims — EARS
    fields, modality, invariant and property statements, constraint values,
    entity states, scope — not of the file. That is the point: re-running
    the checker, recording a witness trace or appending to the verification
    log all change the file and none of them change what the code was built
    to. Comparing content hashes therefore fires on a real divergence and
    stays quiet through bookkeeping churn.

    WARN, never FAIL. A spec that moved after generation is the normal case
    the moment anyone edits a requirement; it says "this code predates the
    current claims", which is a thing to know before trusting a green
    /spec-code-verify, not a thing to block a commit on. The blocking
    question — does the code still satisfy the spec — is conformance
    replay's, and it is asked there.

    The git shas are recorded but deliberately not compared: spec_sha moves
    on every commit to the spec repo, including ones that changed nothing
    this area claims, so a check on it would cry wolf on the first unrelated
    typo fix.
    """
    gen = area_data.get("generated_from") or {}
    stamped = gen.get("spec_content_sha")
    if not stamped:
        return
    current = _compute_spec_sha(area_data)
    if not current or current == stamped:
        return
    add(findings, WARN, "provenance", "generated-from-stale", area_name,
        f"The code was generated against a different version of this spec's "
        f"claims (generated at {stamped[:7]}, now {current[:7]}"
        + (f", code @ {gen['code_sha'][:7]}" if gen.get("code_sha") else "")
        + f"). Requirements, constraints or scope have moved since. Re-run "
        f"/spec-code-verify before trusting the traceability, and re-stamp "
        f"with `spec-record stamp {area_name} --generated` once the code "
        f"matches again.")


def check_meaning(area_data, area_name, findings):
    """Each requirement carries its behavior twice: the EARS fields, which say
    it in the system's own vocabulary, and `meaning.text`, which says what that
    amounts to for someone who has never seen the code.

    The EARS fields alone are not review material once they lean on
    identifiers — 'refuse with NonMatchingTopologyException on
    serviceLevelPolicyId' is a sentence only the implementation can check, and
    a reviewer nodding at it is nodding at a name. Distilling the meaning is a
    judgement no pattern match can make, which is exactly why it is authored
    prose and pinned rather than derived.

    Graded like every other precision lint: WARN while the area is being
    authored, FAIL from in-review on. `raw` requirements are exempt — they have
    no EARS fields to distil yet.
    """
    gating = at_review(area_data)
    severity = FAIL if gating else WARN
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") in ("deferred", "raw"):
            continue
        state, detail = _meaning_status(req)
        if state == "absent":
            add(findings, severity, "meaning", "meaning-missing", area_name,
                f"{rid} has no `meaning.text` — the readback renders it as the "
                f"EARS fields, identifiers and exception names included. Write "
                f"one or two plain sentences saying what the requirement means, "
                f"then pin them with `tools/itf_tools.py meaning-sha "
                f"{area_name} --req {rid}`.", ref=rid)
        elif state == "stale":
            add(findings, severity, "meaning", "meaning-stale", area_name,
                f"{rid}'s plain-words meaning is stale — {detail}. It is the "
                f"sentence the readback leads with, and nothing else on the "
                f"page can contradict it. Reread it against the requirement, "
                f"then re-pin with `tools/itf_tools.py meaning-sha {area_name} "
                f"--req {rid}`.", ref=rid)
        elif not (req.get("meaning") or {}).get("author"):
            add(findings, WARN, "meaning", "meaning-unattributed", area_name,
                f"{rid}.meaning records no `author`. A human-written meaning is "
                f"not re-authored without asking; an unattributed one gives the "
                f"agent no way to know that.", ref=rid)


def check_brief(area_data, area_name, findings):
    """A prose brief is the one unverifiable thing in the readback, so it is
    pinned like a witness rather than trusted like a README.

    Graded the way every other precision lint is: WARN while the area is
    being authored (the brief legitimately lags a spec still moving under
    it), FAIL from in-review on, where a reviewer reads the brief first and
    has no way to tell it is describing a previous version of the area.
    """
    brief = area_data.get("brief") or {}
    if not brief:
        return
    state, detail = _brief_status(area_data)
    if state == "stale":
        gating = at_review(area_data)
        add(findings, FAIL if gating else WARN, "brief", "brief-stale", area_name,
            f"The prose brief is stale — {detail}. It is the first thing a "
            f"reviewer reads and nothing else in the readback can contradict it. "
            f"Re-read it, then re-pin with `tools/itf_tools.py spec-sha "
            f"{area_name}`.")
    text = (brief.get("text") or "").strip()
    if text and len(text) < 40:
        add(findings, WARN, "brief", "brief-too-thin", area_name,
            "The brief is a sentence. `purpose` already carries the one-liner — "
            "a brief that adds no orientation is a section a reader learns to skip.")
    for facet in ("how_it_fits", "why_this_way", "watch_out_for"):
        value = brief.get(facet)
        if value is not None and not str(value).strip():
            add(findings, WARN, "brief", "brief-empty-facet", area_name,
                f"brief.{facet} is present but empty — drop the key rather than "
                f"rendering an empty heading.", ref=facet)


def check_boundary(area_data, area_name, findings):
    """Gap: 'the regenerated code matches' has no referent without a declared
    substitution boundary, and the differential comparator has nothing to diff.

    Also guards the other direction: a boundary that declares nothing FREE is
    a boundary that pins everything, and a spec that pins everything is a
    transliteration \u2014 it can no longer disagree with the code, so it inherits
    its bugs as truth."""
    boundary = area_data.get("boundary") or {}
    diff_cfg = ((area_data.get("conformance") or {}).get("differential") or {})
    review = at_review(area_data)

    if diff_cfg and not boundary.get("observable_state"):
        add(findings, FAIL, "boundary", "differential-without-boundary", area_name,
            "conformance.differential is configured but boundary.observable_state is "
            "empty \u2014 the comparator would report 'equivalent' about nothing in "
            "particular.")
    if not boundary:
        return
    if not boundary.get("entry_points"):
        add(findings, WARN, "boundary", "boundary-without-entry-points", area_name,
            "boundary declares no entry_points \u2014 name the callable surface a "
            "replacement has to accept.")
    if boundary.get("persistence_contract") is None and review:
        add(findings, WARN, "boundary", "boundary-silent-on-persistence", area_name,
            "boundary says nothing about persistence. Behavior can match perfectly "
            "while a regenerated schema orphans every existing row \u2014 state it, "
            "including 'none: the store is private'.")
    if not boundary.get("free"):
        add(findings, WARN, "boundary", "boundary-pins-everything", area_name,
            "boundary names nothing as free. A spec that pins every detail is a "
            "transliteration of the code and inherits its bugs as truth \u2014 say what "
            "a replacement may legitimately do differently.")


def _declares_code(area_data):
    """Does the area claim to describe code? Same three signals the readback
    uses (traceability, a triage ledger, extraction evidence) \u2014 the two must
    agree, or lint and the verdict would disagree about the same area."""
    for t in area_data.get("traceability") or []:
        if (t.get("code") or "").strip():
            return True
    if area_data.get("extraction_triage"):
        return True
    for req in area_data.get("requirements") or []:
        if ((req.get("extraction") or {}).get("evidence") or "").strip():
            return True
    return False


def check_extraction_coverage(area_data, area_name, findings):
    """Was the audit RUN, and did it come back clean?

    Distinct from the ledger check below, which only asks whether the rows
    that exist are coherent. A ledger can be perfectly coherent and cover a
    fraction of the code \u2014 which is how an area reaches full witnesses, full
    invariants, zero untriaged matrix cells and zero lint failures with most
    of its implementation unaccounted for. Every other completeness number
    here measures the spec against itself; this is the only one that measures
    it against the code, so an absent one cannot read the same as a clean
    one.

    `--record` is what makes the number exist. The audit was always
    runnable, but nothing required the flag, so check_results.extraction
    stayed absent and every surface read absent as 'nothing to say'."""
    if not _declares_code(area_data):
        return                      # nothing to audit; not a finding
    review = at_review(area_data)
    stats = (area_data.get("check_results") or {}).get("extraction") or {}
    if not stats:
        if area_data.get("extraction_triage"):
            # Someone ran the audit and triaged from it, but never with
            # --record, so the coverage number was never written down.
            add(findings, FAIL if review else WARN, "extraction",
                "extraction-audit-never-recorded", area_name,
                "extraction_triage[] has entries, so the audit has been run \u2014 "
                "but check_results.extraction is absent, so no coverage number "
                "was ever recorded and nothing downstream can tell a "
                "well-triaged area from a barely-audited one. Re-run with "
                "`tools/spec-extract-audit.py " + area_name + " --record`.")
        else:
            add(findings, FAIL if review else WARN, "extraction",
                "extraction-audit-missing", area_name,
                "This area describes code and has never been audited against "
                "it. Every other completeness check measures the spec against "
                "itself; this is the only one that runs code \u2192 spec, so until "
                "it does the spec can be missing behavior entirely with every "
                "other mark green. Run `tools/spec-extract-audit.py "
                + area_name + " --record`.")
        return
    unclaimed = stats.get("unclaimed", 0)
    if unclaimed:
        add(findings, FAIL if review else WARN, "extraction",
            "extraction-sites-unclaimed", area_name,
            f"{unclaimed} of {stats.get('sites', 0)} decision site(s) in the "
            f"traced code are neither mapped to a spec element nor triaged in "
            f"extraction_triage[]. This is the only check that runs code \u2192 "
            f"spec: each unclaimed site is behavior the implementation has and "
            f"the spec has not accounted for. Triage them with "
            f"`tools/spec-extract-audit.py {area_name} --emit`.")


def check_extraction_ledger(area_data, area_name, findings):
    """The code\u2192spec direction. Full site enumeration needs the source, which
    is tools/spec-extract-audit.py's job; what lint owns is the ledger's own
    coherence, which is checkable from the JSON alone. Whether the audit ran
    at all, and whether it came back clean, is check_extraction_coverage."""
    rows = area_data.get("extraction_triage", []) or []
    if not rows:
        return
    ids = local_ids(area_data)
    excluded = {e.get("item")
                for e in ((area_data.get("scope") or {}).get("excluded") or [])}
    approved = area_data.get("status") == "approved"
    seen = set()

    for row in rows:
        fp = row.get("fingerprint", "?")
        where = f"{row.get('file', '?')}#{fp}"
        if fp in seen:
            add(findings, FAIL, "extraction", "duplicate-extraction-site", area_name,
                f"Site {fp} is triaged twice \u2014 two verdicts for one decision.", ref=fp)
        seen.add(fp)
        verdict = row.get("verdict")

        if verdict == "MAPPED":
            targets = row.get("maps_to") or []
            if not targets:
                add(findings, FAIL, "extraction", "mapped-without-target", area_name,
                    f"{where} is MAPPED but names no spec id.", ref=fp)
            for target in targets:
                if "." not in target and target not in ids:
                    add(findings, FAIL, "extraction", "extraction-maps-to-dangling",
                        area_name,
                        f"{where} maps to '{target}', which does not exist.", ref=fp)
        else:
            if not row.get("reason"):
                add(findings, FAIL, "extraction", "extraction-verdict-without-reason",
                    area_name,
                    f"{where} is {verdict} with no reason. Every verdict except MAPPED "
                    f"is a judgement about code the spec does not describe \u2014 record it.",
                    ref=fp)
            if verdict == "OUT-OF-SCOPE":
                ref = row.get("scope_ref")
                if not ref:
                    add(findings, FAIL, "extraction", "extraction-out-of-scope-unanchored",
                        area_name,
                        f"{where} is OUT-OF-SCOPE but names no scope_ref.", ref=fp)
                elif ref not in excluded:
                    add(findings, FAIL, "extraction", "extraction-scope-ref-dangling",
                        area_name,
                        f"{where} has scope_ref '{ref}', which is not in "
                        f"scope.excluded[].", ref=fp)
            if verdict == "GAP":
                if not row.get("question"):
                    add(findings, WARN, "extraction", "extraction-gap-untracked",
                        area_name,
                        f"{where} is a GAP with no Q-NNN. An acknowledged hole nobody "
                        f"is tracking is an unacknowledged hole.", ref=fp)
                if approved:
                    add(findings, FAIL, "extraction", "extraction-gap-on-approved",
                        area_name,
                        f"{where} is still a GAP on an approved area \u2014 the code does "
                        f"something the spec does not describe.", ref=fp)
            if verdict == "DEAD":
                add(findings, WARN, "extraction", "extraction-dead-code", area_name,
                    f"{where} is triaged DEAD. That is a finding about the CODE, not "
                    f"the spec \u2014 delete it or explain why it stays.", ref=fp)


def check_extraction_provenance(root, area_data, area_name, findings):
    """An extracted claim without evidence cannot be reviewed against the thing
    it was extracted from, which is the only way to tell inference from
    invention."""
    review = at_review(area_data)
    for list_name in ("requirements", "invariants", "constraints"):
        for item in area_data.get(list_name, []) or []:
            iid = item.get("id", "?")
            source = (item.get("source") or "")
            prov = item.get("extraction") or {}
            if source.startswith("extracted") and not prov.get("evidence"):
                add(findings, FAIL if review else WARN, "extraction",
                    "extracted-without-evidence", area_name,
                    f"{iid} is marked '{source}' but records no extraction.evidence. "
                    f"Without the file:line it came from, the confirm pass is guessing.",
                    ref=iid)
            if prov.get("confidence") == "low" and review:
                add(findings, WARN, "extraction", "low-confidence-at-review", area_name,
                    f"{iid} was extracted with low confidence \u2014 the code was ambiguous "
                    f"and someone guessed. Confirm it before approval.", ref=iid)


def check_harvested_examples(root, area_data, area_name, findings):
    """A harvested example whose trace is missing is a claim about production
    with nothing behind it."""
    for ex in area_data.get("examples", []) or []:
        trace = ex.get("trace")
        if not trace:
            continue
        if not (Path(root) / "specs" / trace).exists():
            add(findings, FAIL, "examples", "harvested-trace-missing", area_name,
                f"{ex.get('id', '?')}.trace '{trace}' does not exist. A harvested "
                f"example is evidence only while its recording is there.",
                ref=ex.get("id"))


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
        if inv.get("criticality") == "critical" and inv.get("formal_status") not in ("verified", "verified-inductive", "verified-in-scope", "accepted-risk"):
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
            f"Either remove from architecture.components or run /spec-code-generate to generate code.")

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
            # local_ids() is the single definition of "every ID this area
            # declares". Enumerating the lists again here is how ASM-* and
            # EX-* fell out of it: assumptions are first-class and /spec puts
            # "any ID added or modified" in the manifest, so a change that
            # touched an assumption could not be recorded without failing the
            # dangling-id gate. One source of truth, so it cannot drift again.
            area_ids = local_ids(area_data)
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
              schema_validator=None, sidecars=None):
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
    check_predicate_sanity(area_data, sidecar, sidecars, area_name, findings)
    # The probe module, when the area has one: an invariant declared
    # `over: "probes"` names a val that lives there, not in the sidecar.
    _probes_rel = (area_data.get("formal_model") or {}).get("probes_file")
    probes_sidecar = (parse_sidecar(Path(root) / "specs" / _probes_rel)
                      if _probes_rel else None)
    check_quint_refs(area_data, sidecar, area_name, findings,
                     probes=probes_sidecar)
    check_orphan_actions(area_data, sidecar, area_name, findings)
    check_constraint_values(area_data, sidecar, area_name, findings)
    check_ears_guard_correspondence(area_data, sidecar, area_name, findings)
    check_ears_effect_correspondence(area_data, sidecar, area_name, findings)
    check_unproducible_states(area_data, sidecar, area_name, findings)
    check_meaning(area_data, area_name, findings)
    check_brief(area_data, area_name, findings)
    check_provenance(area_data, area_name, findings)
    check_extraction_coverage(area_data, area_name, findings)
    check_formal_model_consistency(area_data, sidecar, area_name, findings)
    check_alloy_backend(root, area_data, area_name, findings)
    check_scope(area_data, area_name, findings)
    check_externals(area_data, area_name, findings)
    check_assumptions(area_data, all_areas, area_name, findings)
    check_closed_worlds(area_data, sidecar, area_name, findings)
    check_modality(area_data, area_name, findings)
    check_decision_affects(area_data, all_areas, area_name, findings)
    check_examples(area_data, sidecar, all_areas, area_name, findings)
    check_witness_binding(area_data, sidecar, area_name, findings)
    check_witness_delta(root, area_data, sidecar, area_name, findings)
    check_paired_invariants(root, area_data, sidecar, area_name, findings)
    check_refusal_artifacts(area_data, area_name, findings)
    check_boundary(area_data, area_name, findings)
    check_extraction_ledger(area_data, area_name, findings)
    check_extraction_provenance(root, area_data, area_name, findings)
    check_harvested_examples(root, area_data, area_name, findings)
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

# The report is the one thing every user sees, so it must never be the thing
# that crashes. A legacy Windows console reports cp1252, which cannot encode
# the status marks below — nor the em dashes running through most finding
# descriptions — and print() raises UnicodeEncodeError rather than degrading.
# Pick an icon set the stream can actually carry, and set errors="replace" as
# the backstop for description text whose characters we do not control.
UNICODE_ICONS = {PASS: "✓", WARN: "⚠", FAIL: "✗"}
ASCII_ICONS = {PASS: "OK", WARN: "!", FAIL: "X"}
ICONS = UNICODE_ICONS


def _stream_encodes(stream, probe):
    enc = getattr(stream, "encoding", None)
    if not enc:
        return False
    try:
        probe.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def pick_icons(stream=None):
    """Unicode marks when the stream can encode them, ASCII marks otherwise."""
    stream = sys.stdout if stream is None else stream
    return UNICODE_ICONS if _stream_encodes(stream, "✓⚠✗") else ASCII_ICONS


def soften_stdout(stream=None):
    """Last-resort guard: never let an unencodable character abort the report.
    Only touches error handling, never the encoding — re-encoding a cp1252
    console as UTF-8 would trade the crash for mojibake."""
    stream = sys.stdout if stream is None else stream
    if _stream_encodes(stream, "—"):
        return
    try:
        stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
RESET = "\033[0m"


def colorize(text, severity, use_color):
    if not use_color:
        return text
    return f"{COLORS[severity]}{text}{RESET}"


def print_report(findings, areas, use_color=True):
    soften_stdout()
    icons = pick_icons()
    by_area = defaultdict(list)
    for f in findings:
        by_area[f.area].append(f)

    for area in sorted(set(areas) | set(by_area.keys())):
        items = by_area.get(area, [])
        if not items:
            print(colorize(f"{icons[PASS]} {area}: clean", PASS, use_color))
            continue
        fail = sum(1 for f in items if f.severity == FAIL)
        warn = sum(1 for f in items if f.severity == WARN)
        sev = FAIL if fail else WARN
        print(colorize(f"{icons[sev]} {area}: {fail} fail, {warn} warn", sev, use_color))
        for f in items:
            ref = f" [{f.ref}]" if f.ref else ""
            print(f"    {icons[f.severity]} {f.category}/{f.check}{ref}: {f.description}")

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

    if _quint_engine == "cli" and not _quint_cli_available():
        add(findings, FAIL, "quint", "engine-unavailable", "_project",
            "QUINT_IR_ENGINE=cli demands the Quint compiler's typed IR, but the "
            "quint CLI is not on PATH. Every sidecar parses as 'no module', so "
            "every check that reads one — quint_ref resolution, action "
            "reachability, action mutations, constraint values — passes without "
            "being computed. These results are NOT authoritative. Install quint "
            "(tools/check-tooling.sh prints how) or unset QUINT_IR_ENGINE to "
            "allow the regex fallback.")

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
                  schema_validator, sidecars=sidecars)

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
