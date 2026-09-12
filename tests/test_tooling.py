"""Unit tests for the spec tooling changes.

Covers the deterministic, quint-free logic touched by the robustness pass:
  - bounded-vs-inductive invariant rendering (spec-readback honesty)
  - the recorded step bound that makes a bounded ✓ honest
  - the tightened drift file-match (spec-record path_match)
  - the witness-predicate FAIL gate past draft (spec-lint vagueness gate)
  - the opt-in Alloy structural backend (scope-honest rendering, the lint
    gate on proof: "structural", and the receipt.json verdict reader)
  - declared scope and the anchoring of OUT-OF-SCOPE triage
  - externals, their outcome-coverage matrix, and assumption annotations
  - closed-world entities checked against the sidecar's variant type
  - modality: a witness per permitted outcome, an invariant per prohibition
  - the semantic diff (spec-diff) and the completeness dimension grid
  - witness soundness: argument binding, the delta conjunct, paired invariants
  - refusal artifacts (code-side evidence for prohibitions)
  - spec-mutate (do the gates fail?) and spec-separation (claims vs code)
  - brownfield fidelity: the substitution boundary, extraction site detection
    and its ledger, provenance, and the differential/extraction dimensions
  - the self-review regressions: per-outcome witness traces (they broke lint
    AND the verify preflight), the audit before traceability exists, site
    fingerprint collisions, and spec-diff's modality rendering

The tool files use hyphenated names, so they're loaded by path. None of
these tests need quint/Apalache/Java — they exercise pure Python only.

Run:  python -m pytest tests/ -q     (from the repo root)
"""

import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load(modname, filename):
    path = TOOLS / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


readback = _load("spec_readback", "spec-readback.py")
record = _load("spec_record", "spec-record.py")
lint = _load("spec_lint", "spec-lint.py")
matrix = _load("spec_matrix", "spec-matrix.py")
diff = _load("spec_diff", "spec-diff.py")
quint_ir = _load("quint_ir_mod", "quint_ir.py")
probes = _load("spec_probes", "spec-probes.py")
itf = _load("itf_tools_mod", "itf_tools.py")
mutate = _load("spec_mutate", "spec-mutate.py")
sep = _load("spec_separation", "spec-separation.py")
audit = _load("spec_extract_audit", "spec-extract-audit.py")


# ── Finding 1: honest bounded/inductive invariant rendering ──────────────────

@pytest.mark.parametrize("status,bound,expected", [
    ("verified-inductive", 10, "✓ proven"),
    ("verified-inductive", None, "✓ proven"),
    ("verified", 10, "✓ (≤10 steps)"),
    ("verified", 7, "✓ (≤7 steps)"),
    ("verified", None, "✓ (bounded)"),     # bound not recorded → no false depth
    ("counterexample-found", 10, "✗"),
    ("accepted-risk", 10, "⚠ accepted-risk"),
    ("specified", 10, "⏳"),
    ("not-run", 10, "⏳"),
])
def test_invariant_mark(status, bound, expected):
    assert readback.invariant_mark({"formal_status": status}, bound) == expected


def test_invariant_mark_defaults_to_unchecked():
    assert readback.invariant_mark({}, 10) == "⏳"


def test_check_bound_reads_recorded_max_steps():
    assert readback.check_bound({"check_results": {"max_steps": 12}}) == 12


@pytest.mark.parametrize("area", [
    {},
    {"check_results": {}},
    {"check_results": {"max_steps": 0}},      # invalid bound rejected
    {"check_results": {"max_steps": -3}},
    {"check_results": {"max_steps": "10"}},    # wrong type rejected
])
def test_check_bound_absent_or_invalid(area):
    assert readback.check_bound(area) is None


def test_header_bar_distinguishes_proven_from_bounded():
    area = {
        "status": "formalized",
        "requirements": [],
        "invariants": [
            {"id": "INV-001", "formal_status": "verified-inductive"},
            {"id": "INV-002", "formal_status": "verified"},
            {"id": "INV-003", "formal_status": "specified"},
        ],
        "check_results": {"max_steps": 10},
    }
    bar = readback.header_bar(area, "auth")
    assert "1 proven + 1 bounded (≤10) / 3" in bar
    # The old unconditional "N/N verified" overclaim for invariants is gone.
    assert "3 verified" not in bar


def test_header_bar_invariant_cell_without_recorded_bound():
    area = {
        "status": "formalized", "requirements": [],
        "invariants": [{"id": "INV-001", "formal_status": "verified"}],
    }
    bar = readback.header_bar(area, "auth")
    assert "0 proven + 1 bounded / 1" in bar


# ── Drift file-match nit: whole-segment matching, not bare endswith ──────────

def test_path_match_identical():
    assert record.path_match("src/auth/authService.ts", "src/auth/authService.ts")


def test_path_match_suffix_on_segment_boundary():
    assert record.path_match("src/auth/authService.ts", "authService.ts")
    assert record.path_match("authService.ts", "src/auth/authService.ts")


def test_path_match_rejects_substring_without_boundary():
    # 'authStore.ts'.endswith('store.ts') is True — the old code's false drift.
    assert not record.path_match("src/auth/authStore.ts", "store.ts")
    assert not record.path_match("store.ts", "src/auth/authStore.ts")


def test_path_match_rejects_cross_dir_same_basename():
    assert not record.path_match("a/foo.ts", "b/foo.ts")


def test_path_match_normalizes_separators_and_dotslash():
    assert record.path_match("src\\auth\\x.ts", "./src/auth/x.ts")


def test_path_match_empty_is_false():
    assert not record.path_match("", "x.ts")
    assert not record.path_match("x.ts", "")


# ── Finding 2: witness-predicate FAIL gate past draft (vagueness gate) ───────

def _area_with(reqs):
    return {
        "kind": "area", "area": "auth", "version": "1.0.0",
        "status": "formalized", "requirements": reqs,
        "formal_model": {"quint_file": "auth.qnt"},
    }


def _predicate_findings(tmp_path, reqs):
    findings = []
    lint.check_witnesses(tmp_path, _area_with(reqs), "auth", findings)
    return [f for f in findings if f.check == "no-witness-predicate"]


def test_missing_predicate_fails_past_draft(tmp_path):
    hits = _predicate_findings(tmp_path, [
        {"id": "REQ-001", "status": "specified",
         "ears": {"trigger": "x", "response": "y"}},
    ])
    assert len(hits) == 1
    assert hits[0].severity == lint.FAIL


@pytest.mark.parametrize("draft_status", ["raw", "needs-validation"])
def test_missing_predicate_exempt_in_draft(tmp_path, draft_status):
    hits = _predicate_findings(tmp_path, [{"id": "REQ-001", "status": draft_status}])
    assert hits == []


def test_present_predicate_passes(tmp_path):
    hits = _predicate_findings(tmp_path, [
        {"id": "REQ-001", "status": "specified",
         "witness": {"predicate": "sessions.keys().size() > 0"}},
    ])
    assert hits == []


def test_nonfunctional_requirement_exempt(tmp_path):
    # NFRs carry a fit_criterion, not a witness — no predicate gate.
    hits = _predicate_findings(tmp_path, [
        {"id": "REQ-009", "status": "specified", "type": "non-functional"},
    ])
    assert hits == []


# ── Finding A (pass 2): predicate sanity — no fake witnesses ─────────────────

_SIDECAR = {"vars": {"sessions", "accountStatus"},
            "action_mutations": {"login": ["sessions"], "lock": ["accountStatus"]}}


def _sanity_codes(reqs, sidecar=_SIDECAR, sidecars=None, area=None):
    area = area or {"requirements": reqs}
    area.setdefault("requirements", reqs)
    findings = []
    lint.check_predicate_sanity(area, sidecar, sidecars, "auth", findings)
    return [(f.severity, f.check) for f in findings]


@pytest.mark.parametrize("pred", ["true", "false", "  (true) ", "((false))"])
def test_constant_predicate_fails(pred):
    codes = _sanity_codes([{"id": "REQ-1", "quint_ref": "login",
                            "witness": {"predicate": pred}}])
    assert (lint.FAIL, "predicate-constant") in codes


def test_predicate_with_no_state_var_fails():
    codes = _sanity_codes([{"id": "REQ-1", "quint_ref": "login",
                            "witness": {"predicate": "1 + 1 == 2"}}])
    assert (lint.FAIL, "predicate-no-state") in codes


def test_good_predicate_clean():
    assert _sanity_codes([{"id": "REQ-1", "quint_ref": "login",
                           "witness": {"predicate": "sessions.size() > 0"}}]) == []


def test_predicate_off_action_warns():
    # login assigns `sessions`; a predicate over `accountStatus` may witness a
    # side condition, not this requirement's effect.
    codes = _sanity_codes([{"id": "REQ-1", "quint_ref": "login",
                            "witness": {"predicate": "accountStatus.size() > 0"}}])
    assert (lint.WARN, "predicate-off-action") in codes


def test_predicate_on_action_clean():
    assert _sanity_codes([{"id": "REQ-1", "quint_ref": "lock",
                           "witness": {"predicate": "accountStatus.size() > 0"}}]) == []


@pytest.mark.parametrize("req", [
    {"id": "REQ-1", "status": "deferred", "witness": {"predicate": "true"}},
    {"id": "REQ-1", "type": "non-functional", "witness": {"predicate": "true"}},
    {"id": "REQ-1", "witness": {"status": "skipped", "predicate": "true"}},
])
def test_predicate_sanity_skips(req):
    assert _sanity_codes([req]) == []


def test_predicate_sanity_no_module_returns():
    assert lint.check_predicate_sanity(
        {"requirements": [{"id": "R", "witness": {"predicate": "x == 1"}}]},
        None, None, "a", []) is None


def test_no_false_no_state_when_var_set_unknown():
    codes = _sanity_codes([{"id": "REQ-1", "witness": {"predicate": "foo == 1"}}],
                          sidecar={"vars": set(), "action_mutations": {}})
    assert [c for c in codes if c[1] == "predicate-no-state"] == []


def test_spanned_vars_unioned():
    # A contract/UI predicate over an imported area's var must not false-FAIL.
    codes = _sanity_codes(
        [{"id": "INV-X", "witness": {"predicate": "billing_accounts.size() > 0"}}],
        sidecar={"vars": set(), "action_mutations": {}},
        sidecars={"billing": {"vars": {"billing_accounts"}}},
        area={"spans": ["billing"]})
    assert codes == []


# ── Finding B (pass 2): precision lints gate at approval ─────────────────────

def _ambiguity_codes(status, response):
    findings = []
    lint.check_ambiguity(
        {"status": status,
         "requirements": [{"id": "REQ-1", "status": "specified",
                           "ears": {"response": response}}]},
        "auth", findings)
    return [(f.severity, f.check) for f in findings]


@pytest.mark.parametrize("status,sev", [
    ("formalized", "warn"), ("structured", "warn"),
    ("in-review", "fail"), ("approved", "fail"),
])
def test_ambiguity_severity_by_status(status, sev):
    codes = _ambiguity_codes(status, "handle errors gracefully")
    assert (sev, "ambiguous-response") in codes


def test_clean_response_no_ambiguity():
    assert _ambiguity_codes("approved", "navigate to the Login screen") == []


def _state_binding_codes(status):
    findings = []
    lint.check_state_binding(
        {"status": status,
         "concepts": {"entities": [{"name": "Session", "states": ["Active", "Expired"]}]},
         "requirements": [{"id": "REQ-1", "status": "specified",
                           "ears": {"state": "the user is logged in"}}]},
        "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_state_binding_warns_then_fails_at_approval():
    assert (lint.WARN, "state-not-bound") in _state_binding_codes("formalized")
    assert (lint.FAIL, "state-not-bound") in _state_binding_codes("approved")


# ── Finding C (pass 2): ship verdict ─────────────────────────────────────────

def test_verdict_empty():
    assert readback.ship_verdict({}).startswith("**⏳ EMPTY")


def test_verdict_not_ready_counts_unverified():
    v = readback.ship_verdict({"requirements": [{"id": "R", "status": "specified"}]})
    assert v.startswith("**⚠ NOT READY")
    assert "1 of 1 requirement(s) not verified" in v


def test_verdict_ready_with_inductive():
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "invariants": [{"id": "I", "formal_status": "verified-inductive"}],
        "open_questions": []})
    assert v.startswith("**✓ READY")


def test_verdict_ready_with_bounded_invariant():
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "invariants": [{"id": "I", "formal_status": "verified"}]})
    assert v.startswith("**✓ READY")


def test_verdict_blocks_on_open_question():
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "open_questions": [{"id": "Q", "status": "open"}]})
    assert v.startswith("**⚠ NOT READY")


def test_verdict_blocks_on_drift():
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "verification_log": [{"drift_detected": True}]})
    assert "drift detected" in v


# ── Alloy structural backend (opt-in) ────────────────────────────────────────
# None of these need Java or the Alloy jar: the runner's verdict comes from a
# receipt.json the CLI writes, so it can be exercised against a fixture, and
# lint's .als view is regex-only by design (Tier 1 stays Python-only).

def test_invariant_mark_in_scope_carries_the_scope():
    inv = {"formal_status": "verified-in-scope"}
    assert readback.invariant_mark(inv, 10, "3 Account, 3 User") == "✓ (scope: 3 Account, 3 User)"


def test_invariant_mark_in_scope_without_recorded_scope():
    # No recorded scope → still never a bare ✓, same rule as a bounded check
    # whose step bound wasn't recorded.
    assert readback.invariant_mark({"formal_status": "verified-in-scope"}, 10) == "✓ (in scope)"


def test_invariant_mark_scope_arg_ignored_for_other_statuses():
    assert readback.invariant_mark({"formal_status": "verified"}, 10, "3 User") == "✓ (≤10 steps)"
    assert readback.invariant_mark({"formal_status": "verified-inductive"}, 10, "3 User") == "✓ proven"


def test_check_scopes_reads_only_alloy_entries():
    area = {"check_results": {"checks": [
        {"id": "INV-001", "backend": "alloy", "scope": "3 Account"},
        {"id": "INV-002", "scope": "ignored — apalache entry"},
        {"id": "INV-003", "backend": "alloy"},
    ]}}
    assert readback.check_scopes(area) == {"INV-001": "3 Account"}


def test_check_scopes_empty_when_no_checks():
    assert readback.check_scopes({}) == {}


def test_header_bar_shows_in_scope_only_when_present():
    area = {
        "status": "formalized", "requirements": [],
        "invariants": [
            {"id": "INV-001", "formal_status": "verified-inductive"},
            {"id": "INV-002", "formal_status": "verified-in-scope"},
        ],
        "check_results": {"max_steps": 10},
    }
    assert "1 proven + 0 bounded (≤10) + 1 in-scope / 2" in readback.header_bar(area, "c")
    # An Apalache-only area's bar is byte-identical to before.
    plain = {"status": "formalized", "requirements": [],
             "invariants": [{"id": "INV-001", "formal_status": "verified"}]}
    assert "0 proven + 1 bounded / 1" in readback.header_bar(plain, "auth")
    assert "in-scope" not in readback.header_bar(plain, "auth")


def test_verdict_ready_with_structural_invariant():
    # A scope-bounded ✓ is a holding invariant; if it didn't count, a
    # structural contract could never reach READY.
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "invariants": [{"id": "I", "formal_status": "verified-in-scope"}]})
    assert v.startswith("**✓ READY")


def test_verdict_still_blocks_on_unchecked_structural_invariant():
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "invariants": [{"id": "I", "formal_status": "specified"}]})
    assert v.startswith("**⚠ NOT READY")


# ── parse_als: lint's static view of the Alloy sidecar ───────────────────────

_ALS = """module userPermission
sig User {}
sig Account { owner: one User }
assert noOrphanAccounts { all a: Account | a.owner in User }
check noOrphanAccounts for 3 Account, 3 User expect 0
check implicitScope
run someWitness for 4
-- check commentedOut for 3
// check alsoCommented for 3
"""


def test_parse_als_finds_named_commands_with_scope():
    cmds = lint.parse_als(_ALS)
    assert cmds["noOrphanAccounts"]["kind"] == "check"
    assert cmds["noOrphanAccounts"]["scope"] == "3 Account, 3 User expect 0"


def test_parse_als_marks_implicit_scope_as_none():
    assert lint.parse_als(_ALS)["implicitScope"]["scope"] is None


def test_parse_als_distinguishes_run_from_check():
    assert lint.parse_als(_ALS)["someWitness"]["kind"] == "run"


def test_parse_als_skips_comments():
    cmds = lint.parse_als(_ALS)
    assert "commentedOut" not in cmds
    assert "alsoCommented" not in cmds


def test_parse_als_empty_input():
    assert lint.parse_als("") == {}
    assert lint.parse_als(None) == {}


# ── check_alloy_backend: the structural gate ────────────────────────────────

def _alloy_area(invs, alloy_file="user-permission.als", status="formalized"):
    fm = {"quint_file": "user-permission.qnt"}
    if alloy_file:
        fm["alloy_file"] = alloy_file
    return {"kind": "contract", "area": "user-permission", "version": "1.0.0",
            "status": status, "spans": ["auth", "billing"],
            "invariants": invs, "formal_model": fm}


def _alloy_codes(tmp_path, invs, als_text=_ALS, alloy_file="user-permission.als",
                 status="formalized"):
    if als_text is not None and alloy_file:
        specs = tmp_path / "specs"
        specs.mkdir(parents=True, exist_ok=True)
        (specs / alloy_file).write_text(als_text, encoding="utf-8")
    findings = []
    lint.check_alloy_backend(tmp_path, _alloy_area(invs, alloy_file, status),
                             "user-permission", findings)
    return [(f.severity, f.check) for f in findings]


_GOOD_INV = {"id": "INV-CONTRACT-001", "description": "No orphan accounts.",
             "proof": "structural", "alloy_command": "noOrphanAccounts"}


def test_alloy_clean_case_is_silent(tmp_path):
    assert _alloy_codes(tmp_path, [_GOOD_INV]) == []


def test_alloy_untouched_when_backend_unused(tmp_path):
    # The overwhelmingly common case: no structural invariant, no .als.
    # Not one finding, so existing areas lint exactly as before.
    assert _alloy_codes(tmp_path, [{"id": "INV-001", "description": "x"}],
                        als_text=None, alloy_file=None) == []


def test_alloy_structural_without_command_fails(tmp_path):
    inv = {"id": "INV-CONTRACT-001", "description": "x", "proof": "structural"}
    assert (lint.FAIL, "structural-without-command") in _alloy_codes(tmp_path, [inv])


def test_alloy_structural_without_sidecar_fails(tmp_path):
    codes = _alloy_codes(tmp_path, [_GOOD_INV], als_text=None, alloy_file=None)
    assert (lint.FAIL, "structural-without-sidecar") in codes


def test_alloy_missing_file_fails(tmp_path):
    codes = _alloy_codes(tmp_path, [_GOOD_INV], als_text=None)
    assert (lint.FAIL, "alloy-file-missing") in codes


def test_alloy_unknown_command_fails(tmp_path):
    inv = dict(_GOOD_INV, alloy_command="noSuchCheck")
    assert (lint.FAIL, "alloy-command-missing") in _alloy_codes(tmp_path, [inv])


def test_alloy_run_instead_of_check_fails(tmp_path):
    # A `run` reports the opposite verdict — finding an instance would read
    # as success while it actually proves nothing about the invariant.
    inv = dict(_GOOD_INV, alloy_command="someWitness")
    assert (lint.FAIL, "alloy-command-not-check") in _alloy_codes(tmp_path, [inv])


def test_alloy_implicit_scope_warns_while_authoring(tmp_path):
    inv = dict(_GOOD_INV, alloy_command="implicitScope")
    assert (lint.WARN, "alloy-scope-implicit") in _alloy_codes(tmp_path, [inv])


@pytest.mark.parametrize("status", ["in-review", "approved"])
def test_alloy_implicit_scope_fails_at_review(tmp_path, status):
    inv = dict(_GOOD_INV, alloy_command="implicitScope")
    codes = _alloy_codes(tmp_path, [inv], status=status)
    assert (lint.FAIL, "alloy-scope-implicit") in codes


def test_alloy_declared_but_unused_warns(tmp_path):
    codes = _alloy_codes(tmp_path, [{"id": "INV-001", "description": "x"}])
    assert (lint.WARN, "alloy-file-unused") in codes


def test_alloy_critical_in_scope_passes_approval_gate():
    findings = []
    lint.check_critical_invariants(
        {"status": "approved",
         "invariants": [{"id": "INV-CONTRACT-001", "criticality": "critical",
                         "formal_status": "verified-in-scope"}]},
        "user-permission", findings)
    assert findings == []


# ── read_alloy_receipt: the runner's verdict, from the CLI's own DTO ────────

def _receipt(tmp_path, payload):
    (tmp_path / "receipt.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_receipt_no_solution_is_verified_in_scope(tmp_path):
    # Alloy semantics inverted for `check`: UNSAT means no counterexample
    # exists at this size.
    d = _receipt(tmp_path, {"solver": "sat4j", "commands": {
        "noOrphanAccounts": {"type": "check", "scopes": ["3 Account", "3 User"],
                             "solution": []}}})
    result, detail, scope, solver, instance = record.read_alloy_receipt(d, "noOrphanAccounts")
    assert result == "verified"
    assert scope == "3 Account, 3 User"
    assert solver == "sat4j"
    assert instance is None


def test_receipt_solution_is_counterexample(tmp_path):
    d = _receipt(tmp_path, {"solver": "minisat", "commands": {
        "noOrphanAccounts": {"type": "check", "scopes": ["3 Account"],
                             "solution": [{"instances": []}]}}})
    result, _, scope, solver, instance = record.read_alloy_receipt(d, "noOrphanAccounts")
    assert result == "counterexample"
    assert scope == "3 Account"
    assert solver == "minisat"
    assert instance == "noOrphanAccounts-solution-0.xml"


def test_receipt_unknown_command_is_error_not_pass(tmp_path):
    # A typo'd command name must never read as a clean check.
    d = _receipt(tmp_path, {"commands": {"other": {"type": "check", "solution": []}}})
    result, detail, _, _, _ = record.read_alloy_receipt(d, "noOrphanAccounts")
    assert result == "error"
    assert "no command 'noOrphanAccounts'" in detail
    assert "other" in detail


def test_receipt_missing_is_error(tmp_path):
    result, detail, _, _, _ = record.read_alloy_receipt(tmp_path, "anything")
    assert result == "error"
    assert "receipt.json" in detail


def test_receipt_unparseable_is_error(tmp_path):
    (tmp_path / "receipt.json").write_text("{not json", encoding="utf-8")
    result, detail, _, _, _ = record.read_alloy_receipt(tmp_path, "anything")
    assert result == "error"
    assert "unreadable" in detail


def test_receipt_falls_back_to_scope_string(tmp_path):
    d = _receipt(tmp_path, {"commands": {
        "c": {"type": "check", "scope": "for 5", "scopes": [], "solution": []}}})
    _, _, scope, _, _ = record.read_alloy_receipt(d, "c")
    assert scope == "for 5"


# ── run_structural_check: misconfiguration never reads as green ─────────────

def _boom(*_args, **_kwargs):          # must never be reached
    raise AssertionError("the Alloy jar was resolved for a misconfigured check")


def test_structural_without_command_errors_before_launching_alloy(tmp_path):
    item = {"id": "INV-CONTRACT-001", "proof": "structural"}
    entry = record.run_structural_check(
        item, "INV-CONTRACT-001", "c.als", tmp_path / "c.als", tmp_path,
        "user-permission", _boom, "sat4j", 300)
    assert entry["result"] == "error"
    assert entry["backend"] == "alloy"
    assert "formal_status" not in item        # no verdict invented


def test_structural_without_sidecar_errors_before_launching_alloy(tmp_path):
    item = {"id": "INV-CONTRACT-001", "proof": "structural",
            "alloy_command": "noOrphanAccounts"}
    entry = record.run_structural_check(
        item, "INV-CONTRACT-001", None, None, tmp_path,
        "user-permission", _boom, "sat4j", 300)
    assert entry["result"] == "error"
    assert entry["alloy_command"] == "noOrphanAccounts"
    assert "formal_status" not in item


# ── cmd_check end to end for a purely structural area (no quint, no jar) ─────

class _Args:
    def __init__(self, root, area):
        self.root, self.area = str(root), area
        self.steps = self.timeout = self.only = None
        self.no_witness = True
        self.emit_json = False
        # Simulator pre-gate: off by default here so a test that doesn't
        # opt in never reaches a quint binary these tests don't have.
        self.no_simulate = True
        self.only_simulate = False
        self.samples = self.seed = None


def _structural_project(tmp_path, invariants, solution=None):
    (tmp_path / ".spec").mkdir()
    (tmp_path / ".spec" / "project.json").write_text(
        json.dumps({"project": "p", "alloy": {"solver": "sat4j"}}), encoding="utf-8")
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "session-ownership.als").write_text(
        "module so\nsig Account {}\ncheck noSharedSessions for 4 Account\n", encoding="utf-8")
    area_path = specs / "session-ownership.contract.json"
    area_path.write_text(json.dumps({
        "kind": "contract", "area": "session-ownership", "version": "1.0.0",
        "status": "formalized", "spans": ["auth", "billing"],
        "invariants": invariants,
        "formal_model": {"alloy_file": "session-ownership.als"},
    }), encoding="utf-8")
    return area_path


def _fake_alloy(monkeypatch, result="verified"):
    """Stand in for the jar: cmd_check must reach the backend seam without
    java or Alloy installed, and must never touch quint on this path."""
    monkeypatch.setattr(record, "find_alloy", lambda project: ("java", "alloy.jar"))
    monkeypatch.setattr(record, "find_quint",
                        lambda: pytest.fail("quint resolved for a structural-only area"))
    monkeypatch.setattr(record, "run_alloy",
                        lambda *a, **k: (result, "", 0.4,
                                         {"scope": "4 Account", "solver": "sat4j",
                                          "instance": None if result == "verified"
                                          else "noSharedSessions-solution-0.xml"}))


def test_cmd_check_records_structural_verdict(tmp_path, monkeypatch):
    # Regression: a structural invariant carries no quint_name (an Alloy check
    # holds it, not a Quint val). The Apalache presence gate must not skip it.
    area_path = _structural_project(tmp_path, [
        {"id": "INV-CONTRACT-001", "description": "No shared sessions.",
         "proof": "structural", "alloy_command": "noSharedSessions"},
    ])
    _fake_alloy(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "session-ownership"))
    assert exc.value.code == 0

    written = json.loads(area_path.read_text(encoding="utf-8"))
    assert written["invariants"][0]["formal_status"] == "verified-in-scope"
    entry = written["check_results"]["checks"][0]
    assert entry["id"] == "INV-CONTRACT-001"
    assert entry["backend"] == "alloy"
    assert entry["result"] == "verified"
    assert entry["scope"] == "4 Account"
    assert entry["solver"] == "sat4j"


def test_cmd_check_records_structural_counterexample(tmp_path, monkeypatch):
    area_path = _structural_project(tmp_path, [
        {"id": "INV-CONTRACT-001", "description": "No shared sessions.",
         "proof": "structural", "alloy_command": "noSharedSessions"},
    ])
    _fake_alloy(monkeypatch, result="counterexample")
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "session-ownership"))
    assert exc.value.code == 1          # a counterexample is a failing run

    written = json.loads(area_path.read_text(encoding="utf-8"))
    assert written["invariants"][0]["formal_status"] == "counterexample-found"
    entry = written["check_results"]["checks"][0]
    # The instance lives under the gitignored gen/ tree — regenerable, never
    # committed, because Alloy's choice among many instances isn't stable.
    assert entry["instance"] == (
        "session-ownership/gen/alloy/noSharedSessions/noSharedSessions-solution-0.xml")


# ── Gap A: scope, and the anchoring of OUT-OF-SCOPE triage ──────────────────

def _scope_codes(area):
    findings = []
    lint.check_scope(area, "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_out_of_scope_without_ref_warns_while_authoring():
    codes = _scope_codes({
        "status": "formalized",
        "matrix_triage": [{"entity": "S", "state": "A", "event": "e",
                           "verdict": "OUT-OF-SCOPE", "reason": "because"}]})
    assert (lint.WARN, "out-of-scope-unanchored") in codes


@pytest.mark.parametrize("status", ["in-review", "approved"])
def test_out_of_scope_without_ref_fails_at_review(status):
    codes = _scope_codes({
        "status": status,
        "matrix_triage": [{"entity": "S", "state": "A", "event": "e",
                           "verdict": "OUT-OF-SCOPE"}]})
    assert (lint.FAIL, "out-of-scope-unanchored") in codes


def test_scope_ref_must_resolve():
    codes = _scope_codes({
        "status": "formalized",
        "scope": {"excluded": [{"item": "refunds", "reason": "policy"}]},
        "matrix_triage": [{"entity": "S", "state": "A", "event": "e",
                           "verdict": "OUT-OF-SCOPE", "scope_ref": "proration"}]})
    assert (lint.FAIL, "scope-ref-dangling") in codes


def test_anchored_out_of_scope_is_clean():
    assert _scope_codes({
        "status": "approved",
        "scope": {"excluded": [{"item": "refunds", "reason": "policy"}]},
        "matrix_triage": [{"entity": "S", "state": "A", "event": "e",
                           "verdict": "OUT-OF-SCOPE", "scope_ref": "refunds"}]}) == []


def test_no_op_needs_a_reason():
    # A deliberate no-op is a decision; without the reason it is
    # indistinguishable from an oversight, which is the whole point of triage.
    codes = _scope_codes({
        "status": "formalized",
        "matrix_triage": [{"entity": "S", "state": "A", "event": "e",
                           "verdict": "NO-OP"}]})
    assert (lint.FAIL, "no-op-without-reason") in codes
    assert _scope_codes({
        "status": "formalized",
        "matrix_triage": [{"entity": "S", "state": "A", "event": "e",
                           "verdict": "NO-OP", "reason": "REQ-003 decided this"}]}) == []


def test_verdict_names_the_scope_it_is_relative_to():
    v = readback.ship_verdict({
        "requirements": [{"id": "R", "status": "verified"}],
        "scope": {"included": ["cancellation"]}})
    assert v.startswith("**✓ READY")
    assert "scope: cancellation" in v


def test_verdict_flags_a_missing_boundary():
    # READY without a declared boundary is a claim about nothing in particular.
    v = readback.ship_verdict({"requirements": [{"id": "R", "status": "verified"}]})
    assert "no scope declared" in v


# ── Gap B: externals, outcome coverage, assumptions ─────────────────────────

_EXT = [{"name": "BillingProvider",
         "outcomes": [{"name": "SUCCESS"}, {"name": "TIMEOUT"}]}]


def _ext_codes(area):
    findings = []
    lint.check_externals(area, "billing", findings)
    return [(f.severity, f.check) for f in findings]


def test_declared_outcome_with_no_handler_warns_then_fails():
    area = {"status": "formalized", "externals": _EXT, "requirements": []}
    assert (lint.WARN, "outcome-unhandled") in _ext_codes(area)
    area["status"] = "approved"
    assert (lint.FAIL, "outcome-unhandled") in _ext_codes(area)


def test_handled_outcomes_are_clean():
    area = {"status": "approved", "externals": _EXT, "requirements": [
        {"id": "REQ-001", "error_outcomes": [
            {"external": "BillingProvider", "outcome": "SUCCESS", "effect": "Cancelled"},
            {"external": "BillingProvider", "outcome": "TIMEOUT", "effect": "NO_CHANGE"}]}]}
    assert _ext_codes(area) == []


def test_error_outcome_must_name_a_declared_external():
    area = {"status": "formalized", "externals": _EXT, "requirements": [
        {"id": "REQ-001", "error_outcomes": [
            {"external": "Stripe", "outcome": "SUCCESS", "effect": "x"}]}]}
    assert (lint.FAIL, "unknown-external") in _ext_codes(area)


def test_error_outcome_must_name_a_declared_outcome():
    area = {"status": "formalized", "externals": _EXT, "requirements": [
        {"id": "REQ-001", "error_outcomes": [
            {"external": "BillingProvider", "outcome": "EXPLODED", "effect": "x"}]}]}
    assert (lint.FAIL, "unknown-outcome") in _ext_codes(area)


def test_error_outcomes_without_any_external_fail():
    area = {"status": "formalized", "requirements": [
        {"id": "REQ-001", "error_outcomes": [
            {"external": "BillingProvider", "outcome": "SUCCESS", "effect": "x"}]}]}
    assert (lint.FAIL, "error-outcome-without-external") in _ext_codes(area)


def test_open_gap_triage_blocks_approval():
    area = {"status": "approved", "externals": _EXT,
            "requirements": [{"id": "REQ-001", "error_outcomes": [
                {"external": "BillingProvider", "outcome": "SUCCESS", "effect": "x"}]}],
            "outcome_triage": [{"external": "BillingProvider", "outcome": "TIMEOUT",
                                "verdict": "GAP", "reason": "unknown"}]}
    assert (lint.FAIL, "outcome-open-gap") in _ext_codes(area)


def test_area_without_externals_is_untouched():
    assert _ext_codes({"status": "approved", "requirements": [{"id": "REQ-001"}]}) == []


def _asm_codes(area, all_areas=None):
    findings = []
    lint.check_assumptions(area, all_areas or {}, "billing", findings)
    return [(f.severity, f.check) for f in findings]


def test_assumption_affects_must_resolve():
    area = {"status": "formalized", "invariants": [{"id": "INV-001"}],
            "assumptions": [{"id": "ASM-001", "statement": "x",
                             "affects": ["INV-001", "INV-404"]}]}
    codes = _asm_codes(area)
    assert codes.count((lint.FAIL, "assumption-affects-dangling")) == 1


def test_assumption_external_must_be_declared():
    area = {"status": "formalized", "externals": _EXT,
            "assumptions": [{"id": "ASM-001", "statement": "x", "external": "Stripe"}]}
    assert (lint.FAIL, "assumption-unknown-external") in _asm_codes(area)


def test_open_assumption_blocks_approval():
    area = {"status": "approved",
            "assumptions": [{"id": "ASM-001", "statement": "x", "status": "open"}]}
    assert (lint.FAIL, "assumption-open-on-approved") in _asm_codes(area)
    area["status"] = "formalized"
    assert _asm_codes(area) == []


def test_assumptions_annotate_the_marks_they_bear_on():
    area = {"assumptions": [{"id": "ASM-001", "statement": "x",
                             "affects": ["INV-001", "REQ-004"]},
                            {"id": "ASM-002", "statement": "y", "affects": ["INV-001"]}]}
    idx = readback.assumption_index(area)
    assert idx == {"INV-001": ["ASM-001", "ASM-002"], "REQ-004": ["ASM-001"]}
    assert readback.under_assumptions("✓ proven", idx["INV-001"]) == \
        "✓ proven · under ASM-001, ASM-002"


def test_assumptions_never_qualify_a_failure():
    # Only ✓-shaped marks rest on assumptions; a counterexample is not
    # "false, given ASM-001" — it is just false.
    assert readback.under_assumptions("✗", ["ASM-001"]) == "✗"
    assert readback.under_assumptions("⏳", ["ASM-001"]) == "⏳"
    assert readback.under_assumptions("✓ proven", []) == "✓ proven"


# ── Gap B/E: the external × outcome matrix axis ─────────────────────────────

def test_outcome_matrix_counts_coverage():
    area = {"externals": _EXT, "requirements": [
        {"id": "REQ-001", "error_outcomes": [
            {"external": "BillingProvider", "outcome": "SUCCESS", "effect": "Cancelled"}]}]}
    rows = matrix.outcome_rows(area)
    idx = matrix.outcome_coverage_index(area)
    assert rows == [("BillingProvider", "SUCCESS"), ("BillingProvider", "TIMEOUT")]
    assert idx == {("BillingProvider", "SUCCESS"): "REQ-001:Cancelled"}


def test_outcome_matrix_emits_and_tallies(tmp_path):
    area = {"externals": _EXT, "requirements": [
        {"id": "REQ-001", "error_outcomes": [
            {"external": "BillingProvider", "outcome": "SUCCESS", "effect": "Cancelled"}]}]}
    buf = io.StringIO()
    stats = matrix.emit_outcomes_csv(matrix.outcome_rows(area),
                                     matrix.outcome_coverage_index(area), {}, buf)
    assert stats == {"total": 2, "covered": 1, "uncovered": 1, "triaged": 0}
    assert "REQ-001:Cancelled" in buf.getvalue()


def test_outcome_matrix_counts_triage_as_answered():
    area = {"externals": _EXT, "requirements": []}
    triage = {("BillingProvider", "TIMEOUT"): ("IMPOSSIBLE", "never happens")}
    buf = io.StringIO()
    stats = matrix.emit_outcomes_csv(matrix.outcome_rows(area), {}, triage, buf)
    assert stats["triaged"] == 1 and stats["uncovered"] == 1


# ── Gap C: closed worlds ────────────────────────────────────────────────────

def _closed_codes(area, sidecar):
    findings = []
    lint.check_closed_worlds(area, sidecar, "sub", findings)
    return [(f.severity, f.check) for f in findings]


_SM = [{"entity": "Subscription", "quint_type": "SubStatus"}]


def _closed_area(states, closed=True):
    return {"concepts": {"entities": [{"name": "Subscription", "states": states,
                                       "closed": closed}]},
            "state_machines": _SM}


def test_closed_entity_matching_the_model_is_clean():
    sidecar = {"type_variants": {"SubStatus": ["Active", "Cancelled", "Expired"]}}
    assert _closed_codes(_closed_area(["Active", "Cancelled", "Expired"]), sidecar) == []


def test_closed_entity_catches_a_state_the_model_invented():
    # The failure this marker exists for: a generated model grows a state
    # nobody specified, and everything downstream absorbs it silently.
    sidecar = {"type_variants": {"SubStatus": ["Active", "Cancelled", "Expired", "Paused"]}}
    codes = _closed_codes(_closed_area(["Active", "Cancelled", "Expired"]), sidecar)
    assert (lint.FAIL, "closed-world-extra-state") in codes


def test_closed_entity_catches_a_state_the_model_lacks():
    sidecar = {"type_variants": {"SubStatus": ["Active"]}}
    codes = _closed_codes(_closed_area(["Active", "Cancelled"]), sidecar)
    assert (lint.FAIL, "closed-world-missing-state") in codes


def test_open_entity_is_never_checked():
    sidecar = {"type_variants": {"SubStatus": ["Active", "Whatever"]}}
    assert _closed_codes(_closed_area(["Active"], closed=False), sidecar) == []


def test_closed_without_state_machine_is_unverifiable_not_verified():
    area = {"concepts": {"entities": [{"name": "Subscription", "states": ["Active"],
                                       "closed": True}]}}
    assert (lint.WARN, "closed-unverifiable") in _closed_codes(area, {"type_variants": {}})


def test_closed_without_states_fails():
    area = {"concepts": {"entities": [{"name": "S", "closed": True}]}}
    assert (lint.FAIL, "closed-without-states") in _closed_codes(area, {})


def test_closed_type_absent_from_sidecar_warns():
    codes = _closed_codes(_closed_area(["Active"]), {"type_variants": {"Other": ["X"]}})
    assert (lint.WARN, "closed-type-not-found") in codes


def test_quint_ir_extracts_sum_constructors_not_aliases(tmp_path):
    src = tmp_path / "m.qnt"
    src.write_text("module m {\n"
                   "  type UserId = str\n"
                   "  type SubStatus = Active | Cancelled | Expired\n"
                   "  type Res = Ok(int) | Err(str)\n"
                   "  var x: int\n}\n", encoding="utf-8")
    ir = quint_ir.parse_qnt(src, engine="regex")
    assert ir["type_variants"]["SubStatus"] == ["Active", "Cancelled", "Expired"]
    assert ir["type_variants"]["Res"] == ["Ok", "Err"]
    assert "UserId" not in ir["type_variants"]     # an alias is not a closed set


# ── Gap D: modality ─────────────────────────────────────────────────────────

def _modality_codes(reqs, invs=None, status="formalized"):
    findings = []
    lint.check_modality({"status": status, "requirements": reqs,
                         "invariants": invs or [{"id": "INV-002"}]}, "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_forbidden_must_name_its_enforcing_invariant():
    codes = _modality_codes([{"id": "REQ-006", "status": "specified",
                              "modality": "forbidden", "witness": {"status": "skipped"}}])
    assert (lint.FAIL, "forbidden-without-enforcer") in codes


def test_forbidden_enforcer_must_exist():
    codes = _modality_codes([{"id": "REQ-006", "status": "specified",
                              "modality": "forbidden",
                              "witness": {"status": "skipped", "enforced_by": "INV-404"}}])
    assert (lint.FAIL, "forbidden-enforcer-dangling") in codes


def test_forbidden_with_enforcer_is_clean():
    assert _modality_codes([{"id": "REQ-006", "status": "specified",
                             "modality": "forbidden",
                             "witness": {"status": "skipped",
                                         "enforced_by": "INV-002"}}]) == []


def test_forbidden_carrying_a_witness_predicate_warns():
    codes = _modality_codes([{"id": "REQ-006", "status": "specified",
                              "modality": "forbidden",
                              "witness": {"status": "skipped", "enforced_by": "INV-002",
                                          "predicate": "x > 0"}}])
    assert (lint.WARN, "forbidden-with-predicate") in codes


def test_may_needs_a_witness_per_permitted_outcome():
    # One trace would prove one allowed behavior reachable and say nothing
    # about the others — a MAY narrowed to a MUST by omission.
    one = [{"id": "REQ-007", "status": "specified", "modality": "may",
            "witness": {"outcomes": [{"name": "email", "predicate": "p"}]}}]
    assert (lint.FAIL, "may-needs-outcomes") in _modality_codes(one)
    two = [{"id": "REQ-007", "status": "specified", "modality": "may",
            "witness": {"outcomes": [{"name": "email", "predicate": "p"},
                                     {"name": "in-app", "predicate": "q"}]}}]
    assert _modality_codes(two) == []


def test_may_outcome_needs_a_predicate():
    codes = _modality_codes([{"id": "REQ-007", "status": "specified", "modality": "may",
                              "witness": {"outcomes": [{"name": "a", "predicate": "p"},
                                                       {"name": "b"}]}}])
    assert (lint.FAIL, "may-outcome-without-predicate") in codes


def test_may_outcomes_must_all_be_witnessed_at_review():
    reqs = [{"id": "REQ-007", "status": "specified", "modality": "may",
             "witness": {"outcomes": [{"name": "a", "predicate": "p", "status": "witnessed"},
                                      {"name": "b", "predicate": "q", "status": "not-run"}]}}]
    codes = _modality_codes(reqs, status="in-review")
    assert codes.count((lint.FAIL, "may-outcome-unwitnessed")) == 1


def test_may_and_deterministic_contradict():
    codes = _modality_codes([{"id": "REQ-007", "status": "specified", "modality": "may",
                              "determinism": "deterministic",
                              "witness": {"outcomes": [{"name": "a", "predicate": "p"},
                                                       {"name": "b", "predicate": "q"}]}}])
    assert (lint.FAIL, "may-but-deterministic") in codes


def test_may_predicates_satisfy_the_vagueness_gate(tmp_path):
    # Regression: the witness gate reads witness.predicate, which a correctly
    # written 'may' requirement does not have.
    area = {"kind": "area", "area": "auth", "version": "1.0.0", "status": "formalized",
            "formal_model": {"quint_file": "auth.qnt"},
            "requirements": [{"id": "REQ-007", "status": "specified", "modality": "may",
                              "witness": {"outcomes": [
                                  {"name": "a", "predicate": "sessions.size() > 0"},
                                  {"name": "b", "predicate": "sessions.size() > 1"}]}}]}
    findings = []
    lint.check_witnesses(tmp_path, area, "auth", findings)
    assert [f for f in findings if f.check == "no-witness-predicate"] == []


def test_may_outcome_predicates_still_get_the_fake_witness_check():
    codes = _sanity_codes([{"id": "REQ-007", "quint_ref": "login", "modality": "may",
                            "witness": {"outcomes": [
                                {"name": "a", "predicate": "sessions.size() > 0"},
                                {"name": "b", "predicate": "true"}]}}])
    assert (lint.FAIL, "predicate-constant") in codes


def test_may_probe_names_are_distinct_per_outcome():
    assert record.outcome_probe_name("REQ-007", "in-app") == "witness_REQ_007_in_app"
    assert record.outcome_probe_name("REQ-007", "email") == "witness_REQ_007_email"


# ── Gap H: decision blast radius ────────────────────────────────────────────

def test_decision_affects_must_resolve():
    findings = []
    lint.check_decision_affects(
        {"requirements": [{"id": "REQ-001"}], "invariants": [{"id": "INV-001"}],
         "decisions": [{"id": "DEC-001", "affects": ["REQ-001", "INV-001", "REQ-999"]}]},
        {}, "auth", findings)
    assert [(f.severity, f.check) for f in findings] == \
        [(lint.FAIL, "decision-affects-dangling")]


# ── Gap I: examples ─────────────────────────────────────────────────────────

_EX_SIDECAR = {"actions": {"cancel"}, "runs": {"happy"}, "vars": {"status"},
               "action_mutations": {}}


def _example_codes(examples, area=None):
    area = area or {}
    area["examples"] = examples
    findings = []
    lint.check_examples(area, _EX_SIDECAR, {}, "sub", findings)
    return [(f.severity, f.check) for f in findings]


def test_example_action_must_exist():
    codes = _example_codes([{"id": "EX-001", "when": {"action": "nope"},
                             "expect": {"status": "Cancelled"}}])
    assert (lint.FAIL, "example-action-missing") in codes


def test_example_run_must_exist():
    codes = _example_codes([{"id": "EX-001", "when": {"action": "cancel"},
                             "expect": {"status": "x"}, "quint_run": "missing"}])
    assert (lint.FAIL, "example-run-missing") in codes


def test_example_refs_must_resolve():
    codes = _example_codes([{"id": "EX-001", "when": {"action": "cancel"},
                             "expect": {"status": "x"}, "refs": ["REQ-404"]}],
                           area={"requirements": [{"id": "REQ-001"}]})
    assert (lint.FAIL, "example-ref-dangling") in codes


def test_example_asserting_nothing_warns():
    codes = _example_codes([{"id": "EX-001", "when": {"action": "cancel"}, "expect": {}}])
    assert (lint.WARN, "example-asserts-nothing") in codes


def test_good_example_is_clean():
    assert _example_codes([{"id": "EX-001", "when": {"action": "cancel"},
                            "expect": {"status": "Cancelled"}, "quint_run": "happy"}]) == []


# ── Gap J: the completeness dimension grid ──────────────────────────────────

def _dim_row(area, name):
    for line in readback.dimensions_section(area):
        if line.startswith(f"| {name} |"):
            return line
    raise AssertionError(f"no row for {name}")


def test_unmeasured_dimensions_are_dashes_not_ticks():
    # The specific dishonesty this guards against: "no external outcome
    # matrix" rendering as a discharged dimension.
    area = {"requirements": [{"id": "R", "ears": {"unwanted": True}}]}
    assert "| — |" in _dim_row(area, "Failure behavior")
    assert "| — |" in _dim_row(area, "External systems")
    assert "| — |" in _dim_row(area, "Adversarial review")


def test_measured_dimensions_report_their_verdict():
    area = {"check_results": {"outcomes": {"cells": 4, "covered": 4, "uncovered": 0},
                              "matrix": {"cells": 12, "covered": 2, "triaged": 10,
                                         "uncovered": 0}}}
    assert "| ✓ |" in _dim_row(area, "Failure behavior")
    assert "| ✓ |" in _dim_row(area, "State space")
    area["check_results"]["outcomes"]["uncovered"] = 2
    assert "| ! |" in _dim_row(area, "Failure behavior")


def test_open_world_is_not_a_defect_but_half_closed_is():
    open_world = {"concepts": {"entities": [{"name": "A", "states": ["x"]}]}}
    assert "| — |" in _dim_row(open_world, "Data model")
    closed = {"concepts": {"entities": [{"name": "A", "states": ["x"], "closed": True}]}}
    assert "| ✓ |" in _dim_row(closed, "Data model")
    mixed = {"concepts": {"entities": [{"name": "A", "states": ["x"], "closed": True},
                                       {"name": "B", "states": ["y"]}]}}
    assert "| ! |" in _dim_row(mixed, "Data model")


def test_redteam_never_run_is_not_a_pass():
    ran_clean = {"open_questions": [{"id": "Q-001", "source": "red-team:critical",
                                     "status": "resolved"}]}
    assert "| ✓ |" in _dim_row(ran_clean, "Adversarial review")
    ran_open = {"open_questions": [{"id": "Q-001", "source": "red-team:critical",
                                    "status": "open"}]}
    assert "| ! |" in _dim_row(ran_open, "Adversarial review")


# ── Gap F: semantic diff ────────────────────────────────────────────────────

def _area(**kw):
    base = {"area": "auth", "requirements": [], "invariants": [], "constraints": []}
    base.update(kw)
    return base


def test_diff_reports_a_widened_constraint_with_direction():
    old = _area(constraints=[{"id": "CON-001", "name": "MAX", "value": 5}])
    new = _area(constraints=[{"id": "CON-001", "name": "MAX", "value": 7}])
    rep = diff.diff_area(old, new)
    entry = rep["domain"][0]
    assert entry["id"] == "CON-001" and "widened" in entry["now"]
    assert any("re-run the model" in o for o in rep["obligations"])


def test_diff_reports_a_narrowed_constraint():
    old = _area(constraints=[{"id": "CON-001", "name": "MAX", "value": 7}])
    new = _area(constraints=[{"id": "CON-001", "name": "MAX", "value": 5}])
    assert "narrowed" in diff.diff_area(old, new)["domain"][0]["now"]


def test_diff_flags_a_may_narrowed_to_a_must():
    old = _area(requirements=[{"id": "REQ-001", "modality": "may",
                               "ears": {"response": "r"}}])
    new = _area(requirements=[{"id": "REQ-001", "modality": "must",
                               "ears": {"response": "r"}}])
    notes = [e["note"] for e in diff.diff_area(old, new)["behavior"]]
    assert any("removes permitted outcomes" in n for n in notes)


def test_diff_flags_a_lost_verdict_as_an_obligation():
    old = _area(invariants=[{"id": "INV-001", "formal_status": "verified"}])
    new = _area(invariants=[{"id": "INV-001", "formal_status": "not-run"}])
    rep = diff.diff_area(old, new)
    assert any(e["note"] == "LOST its verdict" for e in rep["evidence"])
    assert any("re-establish" in o for o in rep["obligations"])


def test_diff_flags_a_lost_witness():
    old = _area(requirements=[{"id": "REQ-001", "ears": {"response": "r"},
                               "witness": {"status": "witnessed"}}])
    new = _area(requirements=[{"id": "REQ-001", "ears": {"response": "r"},
                               "witness": {"status": "no-witness"}}])
    rep = diff.diff_area(old, new)
    assert any(e["note"] == "lost its witness" for e in rep["evidence"])


def test_diff_flags_a_new_state_on_a_closed_entity_as_breaking():
    old = _area(concepts={"entities": [{"name": "S", "states": ["A"], "closed": True}]})
    new = _area(concepts={"entities": [{"name": "S", "states": ["A", "B"], "closed": True}]})
    rep = diff.diff_area(old, new)
    assert "breaking change" in rep["domain"][0]["note"]
    assert any("S.B" in o for o in rep["obligations"])


def test_diff_flags_a_lifted_scope_exclusion():
    old = _area(scope={"included": ["x"], "excluded": [{"item": "refunds", "reason": "r"}]})
    new = _area(scope={"included": ["x"], "excluded": []})
    notes = [e["note"] for e in diff.diff_area(old, new)["boundary"]]
    assert any("unanchored" in n for n in notes)


def test_diff_reports_first_scope_declaration_once():
    old = _area()
    new = _area(scope={"included": ["a", "b"], "excluded": [{"item": "c", "reason": "r"}]})
    boundary = diff.diff_area(old, new)["boundary"]
    assert len(boundary) == 1 and "scope declared" in boundary[0]["now"]


def test_diff_flags_a_new_external_outcome_as_an_obligation():
    old = _area(externals=[{"name": "P", "outcomes": [{"name": "OK"}]}])
    new = _area(externals=[{"name": "P", "outcomes": [{"name": "OK"}, {"name": "TIMEOUT"}]}])
    rep = diff.diff_area(old, new)
    assert any("P/TIMEOUT" in o for o in rep["obligations"])


def test_diff_surfaces_the_decision_blast_radius():
    old = _area(constraints=[{"id": "CON-001", "name": "MAX", "value": 5}],
                decisions=[{"id": "DEC-001", "decision": "five", "affects": ["CON-001"]}])
    new = _area(constraints=[{"id": "CON-001", "name": "MAX", "value": 7}],
                decisions=[{"id": "DEC-001", "decision": "seven", "affects": ["CON-001"]}])
    blast = diff.diff_area(old, new)["blast"]
    assert blast and blast[0]["id"] == "DEC-001" and "CON-001" in blast[0]["affects"]


def test_diff_surfaces_a_decision_whose_ground_moved():
    # The decision text is unchanged, but something it governs is not.
    old = _area(requirements=[{"id": "REQ-001", "ears": {"response": "old"}}],
                decisions=[{"id": "DEC-001", "decision": "d", "affects": ["REQ-001"]}])
    new = _area(requirements=[{"id": "REQ-001", "ears": {"response": "new"}}],
                decisions=[{"id": "DEC-001", "decision": "d", "affects": ["REQ-001"]}])
    blast = diff.diff_area(old, new)["blast"]
    assert blast and blast[0].get("indirect") is True


def test_diff_is_silent_when_nothing_semantic_changed():
    a = _area(requirements=[{"id": "REQ-001", "ears": {"response": "r"}}],
              last_modified="2026-01-01T00:00:00Z")
    b = _area(requirements=[{"id": "REQ-001", "ears": {"response": "r"}}],
              last_modified="2026-09-09T00:00:00Z")   # touched, not changed
    assert not diff.has_changes(diff.diff_area(a, b))


def test_diff_over_real_git_history(tmp_path):
    import subprocess

    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True,
                       capture_output=True, text=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    specs = tmp_path / "specs"
    specs.mkdir()
    area = specs / "auth.area.json"
    area.write_text(json.dumps(_area(
        constraints=[{"id": "CON-001", "name": "MAX_FAILED_ATTEMPTS", "value": 5}])),
        encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "v1")
    area.write_text(json.dumps(_area(
        constraints=[{"id": "CON-001", "name": "MAX_FAILED_ATTEMPTS", "value": 7}])),
        encoding="utf-8")

    reports = diff.build_reports("HEAD", diff.WORKTREE, tmp_path)
    assert "auth" in reports
    assert "widened" in reports["auth"]["domain"][0]["now"]
    rendered = "\n".join(diff.render(reports, True, "HEAD", diff.WORKTREE))
    assert "## What Changed" in rendered and "MAX_FAILED_ATTEMPTS" in rendered


# ── Witness soundness 1: argument binding ───────────────────────────────────

_BIND_SIDECAR = {"vars": {"sessions", "accountStatus"},
                 "actions": {"login", "tick"},
                 "action_params": {"login": ["uid", "sid"]},
                 "action_mutations": {"login": ["sessions"]},
                 "runs": set(), "type_variants": {}}


def _bind_codes(reqs, status="formalized", sidecar=_BIND_SIDECAR):
    findings = []
    lint.check_witness_binding({"status": status, "requirements": reqs},
                               sidecar, "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_ghost_name_convention():
    assert lint.ghost_for("uid") == "_lastUid"
    assert lint.ghost_for("sid") == "_lastSid"
    assert lint.ghost_for("c") == "_lastC"


def test_unbound_existential_is_flagged():
    # The exact shape that passes today while proving nothing about this call.
    codes = _bind_codes([{"id": "REQ-001", "status": "specified", "quint_ref": "login",
                          "witness": {"predicate":
                                      "sessions.keys().exists(s => sessions.get(s) == Active)"}}])
    assert (lint.WARN, "witness-unbound") in codes


def test_bound_predicate_passes():
    assert _bind_codes([{"id": "REQ-001", "status": "specified", "quint_ref": "login",
                         "witness": {"predicate":
                                     "statusOf(sessions, _lastSid) == Active"}}]) == []


def test_binding_fails_at_review():
    codes = _bind_codes([{"id": "REQ-001", "status": "specified", "quint_ref": "login",
                          "witness": {"predicate": "sessions.size() > 0"}}],
                        status="approved")
    assert (lint.FAIL, "witness-unbound") in codes


def test_parameterless_action_has_nothing_to_bind_to():
    assert _bind_codes([{"id": "REQ-001", "status": "specified", "quint_ref": "tick",
                         "witness": {"predicate": "sessions.size() > 0"}}]) == []


def test_rejection_is_exempt_from_binding():
    # A prohibition has no predicate to bind; it carries an enforcing invariant.
    assert _bind_codes([{"id": "REQ-005", "status": "specified", "quint_ref": "login",
                         "modality": "forbidden",
                         "witness": {"status": "skipped", "enforced_by": "INV-002"}}]) == []


def test_binding_reports_once_per_requirement():
    codes = _bind_codes([{"id": "REQ-007", "status": "specified", "quint_ref": "login",
                          "modality": "may",
                          "witness": {"outcomes": [{"name": "a", "predicate": "sessions.size() > 0"},
                                                   {"name": "b", "predicate": "sessions.size() > 1"}]}}])
    assert codes.count((lint.WARN, "witness-unbound")) == 1


def test_quint_ir_exposes_action_parameters(tmp_path):
    src = tmp_path / "m.qnt"
    src.write_text("module m {\n"
                   "  var sessions: int\n"
                   "  action login(uid: UserId, sid: SessionId): bool = all { true }\n"
                   "  action tick = all { true }\n}\n", encoding="utf-8")
    ir = quint_ir.parse_qnt(src, engine="regex")
    assert ir["action_params"]["login"] == ["uid", "sid"]
    assert "tick" not in ir["action_params"]      # parameterless: simply absent


# ── Witness soundness 2: the delta conjunct ─────────────────────────────────

def _delta_codes(tmp_path, reqs, status="formalized", probes=None):
    area = {"status": status, "requirements": reqs,
            "formal_model": {"quint_file": "a.qnt"}}
    if probes is not None:
        specs = tmp_path / "specs"
        specs.mkdir(parents=True, exist_ok=True)
        (specs / "a.probes.qnt").write_text(probes, encoding="utf-8")
        area["formal_model"]["probes_file"] = "a.probes.qnt"
    findings = []
    lint.check_witness_delta(tmp_path, area, {}, "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_missing_delta_warns_then_fails(tmp_path):
    req = [{"id": "REQ-001", "status": "specified", "witness": {"predicate": "p"}}]
    assert (lint.WARN, "witness-delta-missing") in _delta_codes(tmp_path, req)
    assert (lint.FAIL, "witness-delta-missing") in _delta_codes(tmp_path, req,
                                                                status="approved")


def test_present_delta_passes(tmp_path):
    req = [{"id": "REQ-001", "status": "specified",
            "witness": {"predicate": "p", "delta": {"pre": "not(_prevX.contains(k))"}}}]
    assert _delta_codes(tmp_path, req) == []


def test_delta_dropped_by_the_probe_generator_fails(tmp_path):
    # A recorded delta the probe does not contain is worse than none: the JSON
    # claims a check the model never performs.
    probes = 'val witness_REQ_001: bool =\n  not(p and _lastAction == "login")\n'
    req = [{"id": "REQ-001", "status": "specified",
            "witness": {"predicate": "p", "delta": {"pre": "not(_prevX.contains(k))"}}}]
    codes = _delta_codes(tmp_path, req, probes=probes)
    assert (lint.FAIL, "witness-delta-not-in-probe") in codes


def test_delta_in_the_probe_passes_despite_wrapping(tmp_path):
    # The generator wraps long conjuncts across lines; the check is about the
    # conjunct being there, not about its formatting.
    probes = ('val witness_REQ_001: bool =\n'
              '  not(p and _lastAction == "login"\n'
              '      and not(_prevX.contains(k)))\n')
    req = [{"id": "REQ-001", "status": "specified",
            "witness": {"predicate": "p",
                        "delta": {"pre": "not(_prevX.contains(k))"}}}]
    assert _delta_codes(tmp_path, req, probes=probes) == []


def test_rejection_owes_no_delta(tmp_path):
    req = [{"id": "REQ-005", "status": "specified", "modality": "forbidden",
            "witness": {"status": "skipped", "enforced_by": "INV-002"}}]
    assert _delta_codes(tmp_path, req) == []


# ── Witness soundness 3: paired invariants ──────────────────────────────────

def _paired_codes(tmp_path, constraints, invariants=None, qnt=None, status="formalized"):
    specs = tmp_path / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "auth.qnt").write_text(
        qnt if qnt is not None else
        "module auth {\n  pure val MAX_FAILED_ATTEMPTS: int = 5\n"
        "  action f(u: str): bool = all { n < MAX_FAILED_ATTEMPTS }\n}\n",
        encoding="utf-8")
    area = {"status": status, "constraints": constraints,
            "invariants": invariants or [{"id": "INV-004"}],
            "formal_model": {"quint_file": "auth.qnt"}}
    findings = []
    lint.check_paired_invariants(tmp_path, area, {"vars": set()}, "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_read_constant_without_paired_invariant_warns(tmp_path):
    codes = _paired_codes(tmp_path, [{"id": "CON-001", "name": "MAX_FAILED_ATTEMPTS",
                                      "value": 5}])
    assert (lint.WARN, "constraint-without-paired-invariant") in codes


def test_read_constant_fails_at_review(tmp_path):
    codes = _paired_codes(tmp_path, [{"id": "CON-001", "name": "MAX_FAILED_ATTEMPTS",
                                      "value": 5}], status="in-review")
    assert (lint.FAIL, "constraint-without-paired-invariant") in codes


def test_paired_constant_is_clean(tmp_path):
    assert _paired_codes(tmp_path, [{"id": "CON-001", "name": "MAX_FAILED_ATTEMPTS",
                                     "value": 5, "paired_invariant": "INV-004"}]) == []


def test_paired_invariant_must_exist(tmp_path):
    codes = _paired_codes(tmp_path, [{"id": "CON-001", "name": "MAX_FAILED_ATTEMPTS",
                                      "value": 5, "paired_invariant": "INV-999"}])
    assert (lint.FAIL, "paired-invariant-dangling") in codes


def test_declared_but_unused_constant_needs_no_pairing(tmp_path):
    # Declared and never read by any action: no threshold to get wrong.
    qnt = "module auth {\n  pure val MAX_SESSION_AGE: int = 24\n}\n"
    assert _paired_codes(tmp_path, [{"id": "CON-002", "name": "MAX_SESSION_AGE",
                                     "value": 24}], qnt=qnt) == []


def test_non_numeric_constraint_is_exempt(tmp_path):
    assert _paired_codes(tmp_path, [{"id": "CON-003", "name": "MODE",
                                     "value": "strict"}]) == []


# ── Refusal artifacts ───────────────────────────────────────────────────────

def test_is_rejection_shared_definition():
    assert itf.is_rejection({"modality": "forbidden"})
    assert itf.is_rejection({"witness": {"status": "skipped", "justification": "x"}})
    # unwanted-behavior handling that CHANGES state is witnessable, and replay
    # already covers it — it is not a refusal.
    assert not itf.is_rejection({"ears": {"unwanted": True}})
    assert not itf.is_rejection({"witness": {"status": "skipped"}})
    assert not itf.is_rejection({})
    assert not itf.is_rejection(None)


def _refusal_codes(reqs, status="formalized"):
    findings = []
    lint.check_refusal_artifacts({"status": status, "requirements": reqs},
                                 "auth", findings)
    return [(f.severity, f.check) for f in findings]


def test_rejection_without_artifact_warns_then_fails():
    req = [{"id": "REQ-005", "modality": "forbidden",
            "witness": {"status": "skipped", "enforced_by": "INV-002"}}]
    assert (lint.WARN, "refusal-without-artifact") in _refusal_codes(req)
    assert (lint.FAIL, "refusal-without-artifact") in _refusal_codes(req, "approved")


def test_refusal_without_unchanged_vars_warns():
    # A rejection that throws after incrementing the counter is not a rejection.
    req = [{"id": "REQ-005", "modality": "forbidden",
            "refusal": {"artifact": "t.ts"}}]
    assert (lint.WARN, "refusal-without-unchanged") in _refusal_codes(req)


def test_complete_refusal_is_clean():
    req = [{"id": "REQ-005", "modality": "forbidden",
            "refusal": {"artifact": "t.ts", "unchanged": ["sessions"]}}]
    assert _refusal_codes(req) == []


def test_failing_refusal_blocks_review():
    req = [{"id": "REQ-005", "modality": "forbidden",
            "refusal": {"artifact": "t.ts", "unchanged": ["sessions"],
                        "status": "failing"}}]
    assert (lint.FAIL, "refusal-failing") in _refusal_codes(req, "in-review")


def test_ordinary_requirement_owes_no_refusal():
    assert _refusal_codes([{"id": "REQ-001", "witness": {"predicate": "p"}}]) == []


# ── Assumptions amendment ───────────────────────────────────────────────────

def test_accepted_risk_needs_a_rationale():
    area = {"status": "formalized",
            "assumptions": [{"id": "ASM-001", "statement": "clock is monotonic",
                             "status": "accepted-risk"}]}
    findings = []
    lint.check_assumptions(area, {}, "auth", findings)
    codes = [(f.severity, f.check) for f in findings]
    assert (lint.FAIL, "accepted-risk-without-rationale") in codes

    area["assumptions"][0]["rationale"] = "Accepted by the platform team on 2026-05-01."
    findings = []
    lint.check_assumptions(area, {}, "auth", findings)
    assert findings == []


# ── spec-mutate ─────────────────────────────────────────────────────────────

def test_mask_preserves_length_and_blanks_noise():
    src = ('// limit >= 5 in a comment\n'
           'const MAX = 5;\n'
           'if (n >= MAX) { throw new Error("too many >= tries"); }\n')
    masked = mutate.mask_noise(src)
    assert len(masked) == len(src)
    assert "limit" not in masked and "too many" not in masked
    assert "MAX" in masked and "throw" in masked


def test_mask_handles_block_comments_and_escapes():
    src = 'a /* >= */ b = "he said \\"x >= y\\"";\n'
    masked = mutate.mask_noise(src)
    assert len(masked) == len(src)
    assert masked.count(">=") == 0


def test_operators_find_the_shapes_that_matter():
    masked = mutate.mask_noise('if (n >= 5 && ok) { throw new Error("no"); }')
    assert any("cmp >= -> >" == label for label, *_ in mutate.OPERATORS["cmp"](masked))
    assert any("lit 5 -> 6" == label for label, *_ in mutate.OPERATORS["lit"](masked))
    assert any("&&" in label for label, *_ in mutate.OPERATORS["logic"](masked))
    assert any("throw" in label for label, *_ in mutate.OPERATORS["throw"](masked))


def test_python_raise_is_mutable():
    masked = mutate.mask_noise("    if locked:\n        raise ValueError('no')\n")
    assert list(mutate.OPERATORS["throw"](masked))


def test_traced_files_only(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "authService.ts").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "untraced.ts").write_text("x", encoding="utf-8")
    area = {"traceability": [{"id": "REQ-001", "code": "src/authService.ts:login"},
                             {"id": "REQ-002", "code": "src/missing.ts:foo"}]}
    files = mutate.traced_files(area, tmp_path)
    assert [f.name for f in files] == ["authService.ts"]


def test_mutant_application_always_restores(tmp_path, monkeypatch):
    target = tmp_path / "code.js"
    original = "if (n >= 5) { return 1; }\n"
    target.write_text(original, encoding="utf-8")
    masked = mutate.mask_noise(original)
    label, start, end, repl = next(iter(mutate.OPERATORS["cmp"](masked)))
    seen = {}

    def fake_run(command, repo_root, timeout):
        seen["during"] = target.read_text(encoding="utf-8")
        return 1

    # monkeypatch, not assignment: a bare `mutate.run_gate = fake_run` leaks
    # the stub into every later test in the session.
    monkeypatch.setattr(mutate, "run_gate", fake_run)
    code = mutate.apply_and_run(
        {"file": str(target), "start": start, "end": end, "replacement": repl},
        "gate", tmp_path, 10)
    assert code == 1
    assert seen["during"] != original            # the mutant really was applied
    assert target.read_text(encoding="utf-8") == original   # and always restored


# ── spec-separation ─────────────────────────────────────────────────────────

def _spec(**kw):
    base = {"area": "auth",
            "requirements": [{"id": "REQ-001", "ears": {"response": "r"},
                              "status": "specified",
                              "witness": {"predicate": "p", "status": "not-run"}}],
            "invariants": [{"id": "INV-001", "description": "d",
                            "formal_status": "specified"}],
            "constraints": [{"id": "CON-001", "name": "MAX", "value": 5}],
            "check_results": {"ran_at": "t"}, "last_modified": "t"}
    base.update(kw)
    return json.dumps(base)


def test_separation_ignores_mechanical_bookkeeping():
    before = _spec()
    after = json.loads(before)
    after["check_results"] = {"ran_at": "later"}
    after["last_modified"] = "later"
    after["requirements"][0]["status"] = "verified"
    after["requirements"][0]["witness"]["status"] = "witnessed"
    after["requirements"][0]["witness"]["trace"] = "auth/traces/REQ-001.itf.json"
    after["invariants"][0]["formal_status"] = "verified"
    assert sep.claim_diff(sep.claims(before), sep.claims(json.dumps(after))) == []


def test_separation_ignores_reformatting():
    before = _spec()
    reflowed = json.dumps(json.loads(before), indent=4, sort_keys=True)
    assert sep.claim_diff(sep.claims(before), sep.claims(reflowed)) == []


def test_separation_catches_a_weakened_predicate():
    before = _spec()
    after = json.loads(before)
    after["requirements"][0]["witness"]["predicate"] = "true"
    assert sep.claim_diff(sep.claims(before), sep.claims(json.dumps(after))) == \
        ["requirements.REQ-001"]


def test_separation_catches_a_widened_constraint():
    before = _spec()
    after = json.loads(before)
    after["constraints"][0]["value"] = 7
    assert sep.claim_diff(sep.claims(before), sep.claims(json.dumps(after))) == \
        ["constraints"]


def test_separation_catches_a_dropped_invariant_conjunct():
    before = _spec()
    after = json.loads(before)
    after["invariants"][0]["description"] = "weaker"
    assert sep.claim_diff(sep.claims(before), sep.claims(json.dumps(after))) == \
        ["invariants.INV-001"]


def test_separation_refusal_status_is_a_result_not_a_claim():
    before = json.loads(_spec())
    before["requirements"][0]["refusal"] = {"artifact": "t.ts", "unchanged": ["s"],
                                            "status": "not-run"}
    after = json.loads(json.dumps(before))
    after["requirements"][0]["refusal"]["status"] = "passing"
    assert sep.claim_diff(sep.claims(json.dumps(before)),
                          sep.claims(json.dumps(after))) == []
    after["requirements"][0]["refusal"]["unchanged"] = []
    assert sep.claim_diff(sep.claims(json.dumps(before)),
                          sep.claims(json.dumps(after))) == ["requirements.REQ-001"]


def test_separation_path_matching_is_segment_wise():
    traced = {"src/auth/authService.ts", "src/billing/"}
    assert sep.touches_implementation("src/auth/authService.ts", traced)
    assert sep.touches_implementation("repo/src/auth/authService.ts", traced)
    assert sep.touches_implementation("src/billing/invoice.ts", traced)
    assert not sep.touches_implementation("src/authz/authService.ts", traced)
    assert not sep.touches_implementation("docs/auth.md", traced)


def test_separation_end_to_end_over_git(tmp_path):
    import subprocess

    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True, text=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (tmp_path / "specs").mkdir()
    (tmp_path / "src").mkdir()
    spec = tmp_path / "specs" / "auth.area.json"
    code = tmp_path / "src" / "authService.ts"
    payload = json.loads(_spec())
    payload["traceability"] = [{"id": "REQ-001", "code": "src/authService.ts:login"}]
    spec.write_text(json.dumps(payload), encoding="utf-8")
    code.write_text("export const login = () => 1;\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "v1")

    # Bookkeeping-only spec edit plus a code edit: not a mixed commit.
    payload["check_results"] = {"ran_at": "later"}
    spec.write_text(json.dumps(payload), encoding="utf-8")
    code.write_text("export const login = () => 2;\n", encoding="utf-8")
    run("git", "add", "-A")
    files, before, after = sep.changed_files(
        type("A", (), {"range": None, "rev": None})(), tmp_path)
    traced = sep.traced_paths(tmp_path)
    impl = [f for f in files if not f.startswith("specs/")
            and sep.touches_implementation(f, traced)]
    claim_changed = [f for f in files if f.startswith("specs/")
                     and sep.claim_diff(sep.claims(sep.blob(before, f, tmp_path)),
                                        sep.claims(sep.blob(after, f, tmp_path)))]
    assert impl and not claim_changed

    # Now weaken a claim in the same commit as the code change: mixed.
    payload["requirements"][0]["witness"]["predicate"] = "true"
    spec.write_text(json.dumps(payload), encoding="utf-8")
    run("git", "add", "-A")
    claim_changed = [f for f in files if f.startswith("specs/")
                     and sep.claim_diff(sep.claims(sep.blob(before, f, tmp_path)),
                                        sep.claims(sep.blob(None, f, tmp_path)))]
    assert claim_changed and impl


def test_python_deletion_keeps_the_file_parsable(tmp_path):
    # A deleted `raise` that leaves an empty suite turns the gate red with an
    # IndentationError — the mutant then reads as killed while the gate never
    # exercised the refusal at all. Substituting `pass` is what makes a gate
    # blind to the refusal path show up as the survivor it is.
    src = ("def check(n, locked):\n"
           "    if locked:\n"
           "        raise ValueError('no')\n"
           "    return n\n")
    path = tmp_path / "auth.py"
    path.write_text(src, encoding="utf-8")
    label, start, end, repl = next(iter(mutate.OPERATORS["throw"](mutate.mask_noise(src))))
    fixed = mutate.keep_syntax_valid(path, src, "throw", start, end, repl)
    mutated = src[:start] + fixed + src[end:]
    compile(mutated, "auth.py", "exec")          # must still parse
    assert "raise" not in mutated                # and the refusal must be gone


def test_braced_languages_need_no_substitute(tmp_path):
    src = 'if (locked) { throw new Error("no"); }\n'
    path = tmp_path / "auth.ts"
    label, start, end, repl = next(iter(mutate.OPERATORS["throw"](mutate.mask_noise(src))))
    assert mutate.keep_syntax_valid(path, src, "throw", start, end, repl) == ""


# ── Brownfield 1: the substitution boundary ─────────────────────────────────

def _boundary_codes(area):
    findings = []
    lint.check_boundary(area, "cart", findings)
    return [(f.severity, f.check) for f in findings]


def test_differential_without_boundary_fails():
    # "Equivalent" would mean "equivalent in ways nobody wrote down".
    codes = _boundary_codes({"status": "formalized",
                             "conformance": {"differential": {"command": "x"}}})
    assert (lint.FAIL, "differential-without-boundary") in codes


def test_boundary_that_pins_everything_warns():
    # A spec with nothing free is a transliteration: it can no longer disagree
    # with the code, so it inherits its bugs as truth.
    codes = _boundary_codes({"status": "formalized",
                             "boundary": {"entry_points": ["f()"],
                                          "observable_state": ["s"],
                                          "persistence_contract": "none"}})
    assert (lint.WARN, "boundary-pins-everything") in codes


def test_complete_boundary_is_clean():
    assert _boundary_codes({"status": "formalized", "boundary": {
        "entry_points": ["f()"], "observable_state": ["s"],
        "persistence_contract": "none", "free": ["layout"]}}) == []


def test_boundary_silent_on_persistence_warns_at_review():
    area = {"status": "approved", "boundary": {
        "entry_points": ["f()"], "observable_state": ["s"], "free": ["layout"]}}
    assert (lint.WARN, "boundary-silent-on-persistence") in _boundary_codes(area)


def test_no_boundary_no_findings():
    assert _boundary_codes({"status": "formalized"}) == []


# ── Brownfield 2: extraction site detection ─────────────────────────────────

_JS = '''const MAX = 20;
function addItem(cart, sku) {
  if (cart.status === "closed") {
    throw new Error("closed");
  }

  if (cart.items.length >= 5) {
    return null;
  }
  try {
    save(cart);
  } catch (err) {
    return { ok: false };
  }
  return cart;
}

function clear(cart) {
  return cart;
}
'''


def _sites(tmp_path, text=_JS, name="cart.js"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return audit.scan_file(path, tmp_path)


def test_branches_do_not_swallow_blank_lines(tmp_path):
    # `\\s*` after the closing brace eats newlines, and the site then swallows
    # the blank line plus whatever followed it.
    sites = _sites(tmp_path)
    for site in sites:
        assert "\n\n" not in site["snippet"]
    branches = [s["snippet"] for s in sites if s["kind"] == "branch"]
    assert any(s.startswith("if (cart.items.length >= 5)") for s in branches)


def test_brace_catch_form_is_found(tmp_path):
    # `} catch (e) {` is the common shape; missing it would miss every error
    # handler in a braced language, which is the half specs are thinnest on.
    kinds = [s["kind"] for s in _sites(tmp_path)]
    assert "error-handler" in kinds


def test_identical_statements_in_different_functions_are_distinct_sites(tmp_path):
    # Two `return cart;` lines collide on text alone; one ledger row would
    # silently claim both decisions.
    returns = [s for s in _sites(tmp_path) if s["snippet"] == "return cart;"]
    assert len(returns) == 2
    assert returns[0]["fingerprint"] != returns[1]["fingerprint"]
    assert {r["owner"] for r in returns} == {"addItem", "clear"}


def test_guard_literals_are_their_own_site(tmp_path):
    # "the branch is specified" and "the number in it is specified" are
    # different claims.
    lits = [s for s in _sites(tmp_path) if s["kind"] == "guard-literal"]
    assert any("5" in s["snippet"] for s in lits)
    # The constant declaration is not a guard, so MAX = 20 is not a site.
    assert not any("20" in s["snippet"] for s in lits)


def test_comments_and_strings_produce_no_sites(tmp_path):
    text = 'function f() {\n  // if (x) { throw new Error("no"); }\n  const s = "if (y) return 1;";\n  return s;\n}\n'
    sites = _sites(tmp_path, text)
    assert [s["snippet"] for s in sites] == ["return s;"]


def test_fingerprint_survives_reindentation(tmp_path):
    a = _sites(tmp_path, "function f() {\n  if (x > 1) {\n    return 1;\n  }\n}\n")
    b = _sites(tmp_path, "function f() {\n      if  (x > 1)   {\n        return 1;\n      }\n}\n")
    assert {s["fingerprint"] for s in a} == {s["fingerprint"] for s in b}


def test_fingerprint_changes_when_the_condition_changes(tmp_path):
    a = _sites(tmp_path, "function f() {\n  if (x > 1) { return 1; }\n}\n")
    b = _sites(tmp_path, "function f() {\n  if (x > 2) { return 1; }\n}\n")
    assert {s["fingerprint"] for s in a} != {s["fingerprint"] for s in b}


# ── Brownfield 3: the ledger reconciliation ─────────────────────────────────

def _audit(sites, area):
    return audit.audit(area, sites)


def test_unclaimed_sites_are_counted(tmp_path):
    sites = _sites(tmp_path)
    report = _audit(sites, {})
    assert report["unclaimed"] == len(sites)
    assert report["mapped"] == 0


def test_mapped_site_must_name_an_existing_id(tmp_path):
    sites = _sites(tmp_path)[:1]
    area = {"requirements": [{"id": "REQ-001"}], "extraction_triage": [
        {"file": "cart.js", "fingerprint": sites[0]["fingerprint"],
         "verdict": "MAPPED", "maps_to": ["REQ-404"]}]}
    assert any("REQ-404" in p for p in _audit(sites, area)["problems"])


def test_stale_ledger_entries_are_reported(tmp_path):
    # The code moved out from under a decision; silently keeping the row would
    # let the ledger drift into fiction while still looking complete.
    sites = _sites(tmp_path)[:1]
    area = {"extraction_triage": [
        {"file": "cart.js", "fingerprint": sites[0]["fingerprint"],
         "verdict": "NOT-BEHAVIOR", "reason": "x"},
        {"file": "cart.js", "fingerprint": "deadbeef",
         "verdict": "NOT-BEHAVIOR", "reason": "gone"}]}
    assert _audit(sites, area)["stale"] == ["deadbeef"]


def test_out_of_scope_site_must_cite_an_exclusion(tmp_path):
    sites = _sites(tmp_path)[:1]
    area = {"scope": {"excluded": [{"item": "pricing", "reason": "billing"}]},
            "extraction_triage": [
                {"file": "cart.js", "fingerprint": sites[0]["fingerprint"],
                 "verdict": "OUT-OF-SCOPE", "reason": "x", "scope_ref": "nope"}]}
    assert any("scope_ref" in p for p in _audit(sites, area)["problems"])


def test_emit_produces_paste_ready_stubs(tmp_path):
    stubs = audit.emit_stubs(_sites(tmp_path)[:2])
    assert len(stubs) == 2
    assert all(s["verdict"] == "GAP" and s["fingerprint"] for s in stubs)


# ── Brownfield 4: ledger coherence in lint ──────────────────────────────────

def _ledger_codes(rows, area=None):
    area = dict(area or {})
    area["extraction_triage"] = rows
    findings = []
    lint.check_extraction_ledger(area, "cart", findings)
    return [(f.severity, f.check) for f in findings]


def test_mapped_without_target_fails():
    assert (lint.FAIL, "mapped-without-target") in _ledger_codes(
        [{"file": "a.js", "fingerprint": "aaaaaaaa", "verdict": "MAPPED"}])


def test_verdict_without_reason_fails():
    assert (lint.FAIL, "extraction-verdict-without-reason") in _ledger_codes(
        [{"file": "a.js", "fingerprint": "aaaaaaaa", "verdict": "DEFENSIVE"}])


def test_duplicate_site_fails():
    rows = [{"file": "a.js", "fingerprint": "aaaaaaaa", "verdict": "DEAD", "reason": "x"},
            {"file": "a.js", "fingerprint": "aaaaaaaa", "verdict": "DEFENSIVE", "reason": "y"}]
    assert (lint.FAIL, "duplicate-extraction-site") in _ledger_codes(rows)


def test_gap_blocks_approval():
    rows = [{"file": "a.js", "fingerprint": "aaaaaaaa", "verdict": "GAP",
             "reason": "unspecified", "question": "Q-001"}]
    assert _ledger_codes(rows, {"status": "formalized"}) == []
    assert (lint.FAIL, "extraction-gap-on-approved") in _ledger_codes(
        rows, {"status": "approved"})


def test_dead_code_is_a_finding_about_the_code():
    assert (lint.WARN, "extraction-dead-code") in _ledger_codes(
        [{"file": "a.js", "fingerprint": "aaaaaaaa", "verdict": "DEAD",
          "reason": "unreachable"}])


# ── Brownfield 5: provenance ────────────────────────────────────────────────

def _prov_codes(tmp_path, items, status="formalized", key="requirements"):
    findings = []
    lint.check_extraction_provenance(tmp_path, {"status": status, key: items},
                                     "cart", findings)
    return [(f.severity, f.check) for f in findings]


def test_extracted_without_evidence_warns_then_fails(tmp_path):
    item = [{"id": "REQ-001", "source": "extracted"}]
    assert (lint.WARN, "extracted-without-evidence") in _prov_codes(tmp_path, item)
    assert (lint.FAIL, "extracted-without-evidence") in _prov_codes(
        tmp_path, item, status="approved")


def test_evidence_satisfies_the_gate(tmp_path):
    item = [{"id": "REQ-001", "source": "extracted",
             "extraction": {"evidence": "cart.js:10-20", "confidence": "high"}}]
    assert _prov_codes(tmp_path, item) == []


def test_low_confidence_surfaces_at_review(tmp_path):
    item = [{"id": "REQ-001", "source": "extracted",
             "extraction": {"evidence": "cart.js:10", "confidence": "low"}}]
    assert (lint.WARN, "low-confidence-at-review") in _prov_codes(
        tmp_path, item, status="in-review")


def test_elicited_items_need_no_evidence(tmp_path):
    assert _prov_codes(tmp_path, [{"id": "REQ-001", "source": "elicited"}]) == []


# ── Brownfield 6: harvested example traces ──────────────────────────────────

def test_harvested_trace_must_exist(tmp_path):
    findings = []
    lint.check_harvested_examples(
        tmp_path, {"examples": [{"id": "EX-001", "trace": "cart/traces/EX-001.itf.json"}]},
        "cart", findings)
    assert [(f.severity, f.check) for f in findings] == \
        [(lint.FAIL, "harvested-trace-missing")]

    (tmp_path / "specs" / "cart" / "traces").mkdir(parents=True)
    (tmp_path / "specs" / "cart" / "traces" / "EX-001.itf.json").write_text("{}", encoding="utf-8")
    findings = []
    lint.check_harvested_examples(
        tmp_path, {"examples": [{"id": "EX-001", "trace": "cart/traces/EX-001.itf.json"}]},
        "cart", findings)
    assert findings == []


# ── Brownfield 7: the readback surfaces ─────────────────────────────────────

def test_boundary_section_names_what_is_free():
    lines = readback.boundary_section({"boundary": {
        "entry_points": ["addItem(c, s, q)"], "observable_state": ["cart.status"],
        "persistence_contract": "the stored shape is the contract",
        "free": ["file layout"]}})
    text = "\n".join(lines)
    assert "addItem(c, s, q)" in text and "cart.status" in text
    assert "Deliberately free" in text and "file layout" in text


def test_differential_result_is_never_reported_as_equivalence():
    lines = readback.boundary_section({
        "boundary": {"observable_state": ["s"]},
        "check_results": {"differential": {"result": "equivalent-in-sequences",
                                           "sequences": 2000, "divergences": 0}}})
    text = "\n".join(lines)
    assert "2000" in text
    assert "not equivalence" in text


def test_divergence_is_surfaced_in_attention(tmp_path):
    items = readback.attention_items(tmp_path, "cart", {
        "check_results": {"differential": {"result": "diverged", "divergences": 3}}})
    assert any("Not substitutable" in i for i in items)


def test_unclaimed_sites_are_surfaced_in_attention(tmp_path):
    items = readback.attention_items(tmp_path, "cart", {
        "check_results": {"extraction": {"unclaimed": 4, "sites": 18}}})
    assert any("Unaccounted code" in i for i in items)


def test_extraction_section_groups_by_verdict():
    lines = readback.extraction_section({
        "check_results": {"extraction": {"sites": 3, "mapped": 1, "triaged": 2,
                                         "unclaimed": 0}},
        "extraction_triage": [
            {"file": "a.js", "line": 1, "fingerprint": "aaaaaaaa", "verdict": "MAPPED",
             "maps_to": ["REQ-001"]},
            {"file": "a.js", "line": 9, "fingerprint": "bbbbbbbb", "verdict": "GAP",
             "reason": "unspecified rejection", "question": "Q-001"},
            {"file": "a.js", "line": 12, "fingerprint": "cccccccc", "verdict": "DEAD",
             "reason": "unreachable"}]})
    text = "\n".join(lines)
    assert "**GAP** (1)" in text and "**DEAD** (1)" in text
    assert "Q-001" in text
    assert "1 site(s) MAPPED" in text


def test_low_confidence_extraction_is_flagged_on_the_requirement(tmp_path):
    lines = readback.render_requirement(
        tmp_path,
        {"area": "cart", "requirements": []},
        {"id": "REQ-001", "status": "needs-validation",
         "ears": {"response": "do the thing"},
         "extraction": {"evidence": "cart.js:31-35", "confidence": "low"}},
        [], set())
    text = "\n".join(lines)
    assert "cart.js:31-35" in text and "low confidence" in text


def test_dimensions_report_extraction_and_substitutability():
    unmeasured = readback.dimensions_section({})
    joined = "\n".join(unmeasured)
    assert "| Extraction coverage | — |" in joined
    assert "| Substitutability | — |" in joined

    measured = readback.dimensions_section({"check_results": {
        "extraction": {"sites": 18, "mapped": 9, "triaged": 9, "unclaimed": 0},
        "differential": {"result": "equivalent-in-sequences", "sequences": 2000,
                         "divergences": 0}}})
    joined = "\n".join(measured)
    assert "| Extraction coverage | ✓ |" in joined
    assert "| Substitutability | ✓ |" in joined

    diverged = readback.dimensions_section({"check_results": {
        "extraction": {"sites": 18, "mapped": 9, "triaged": 4, "unclaimed": 5},
        "differential": {"result": "diverged", "sequences": 2000, "divergences": 3}}})
    joined = "\n".join(diverged)
    assert "| Extraction coverage | ! |" in joined
    assert "| Substitutability | ! |" in joined


# ── The worked brownfield example stays accounted for ───────────────────────

def test_cart_example_ledger_covers_every_site():
    """The example is the regression test for the whole loop: if a site is
    added to cart.js without a verdict, this fails."""
    repo = Path(__file__).resolve().parent.parent
    area = json.loads((repo / "examples" / "specs" / "cart.area.json")
                      .read_text(encoding="utf-8"))
    examples_root = repo / "examples"
    sites = []
    for path in mutate.traced_files(area, examples_root):
        sites.extend(audit.scan_file(path, examples_root))
    report = audit.audit(area, sites)
    assert report["unclaimed"] == 0, report["unclaimed_sites"]
    assert report["problems"] == []
    assert report["stale"] == []
    assert report["mapped"] > 0 and report["triaged"] > 0


# ── Regressions from the self-review ────────────────────────────────────────

def _may_area(root, outcomes):
    (root / "specs" / "a" / "traces").mkdir(parents=True, exist_ok=True)
    (root / "specs" / "a.qnt").write_text("module a { var x: int }", encoding="utf-8")
    for oc in outcomes:
        if oc.get("trace"):
            # Two states, not one. These tests are about per-outcome trace
            # LOOKUP, and the content was a placeholder — but a one-state
            # counterexample means the predicate already held at init, which
            # the hollow-witness gate now (correctly) refuses to discharge.
            # A trace that discharges anything has to contain a step.
            (root / "specs" / oc["trace"]).write_text(
                json.dumps({"vars": ["x"], "states": [{"x": 0}, {"x": 1}]}),
                encoding="utf-8")
    return {"kind": "area", "area": "a", "version": "1.0.0", "status": "formalized",
            "formal_model": {"quint_file": "a.qnt"},
            "requirements": [{"id": "REQ-007", "status": "specified", "modality": "may",
                              "quint_ref": "x",
                              "witness": {"status": "witnessed", "outcomes": outcomes}}]}


def test_may_requirement_with_per_outcome_traces_passes_lint(tmp_path):
    """A `may` requirement keeps its traces per permitted outcome and has no
    top-level witness.trace. Reading only that field FAILed every correctly
    discharged permission with 'witnessed-without-trace'."""
    outcomes = [
        {"name": "email", "predicate": "p", "status": "witnessed",
         "trace": "a/traces/REQ-007.email.itf.json"},
        {"name": "in-app", "predicate": "q", "status": "witnessed",
         "trace": "a/traces/REQ-007.in-app.itf.json"},
    ]
    area = _may_area(tmp_path, outcomes)
    sha = itf.compute_model_sha(tmp_path, "a", area)
    for oc in outcomes:
        oc["model_sha"] = sha
    findings = []
    lint.check_witnesses(tmp_path, area, "a", findings)
    assert findings == []


def test_may_requirement_discharges_the_verify_preflight(tmp_path):
    """The same defect in witness_status made spec-record verify refuse
    conformance replay permanently for any area with a permission."""
    outcomes = [
        {"name": "email", "predicate": "p", "status": "witnessed",
         "trace": "a/traces/REQ-007.email.itf.json"},
        {"name": "in-app", "predicate": "q", "status": "witnessed",
         "trace": "a/traces/REQ-007.in-app.itf.json"},
    ]
    area = _may_area(tmp_path, outcomes)
    sha = itf.compute_model_sha(tmp_path, "a", area)
    for oc in outcomes:
        oc["model_sha"] = sha
    rows, missing, discharged = itf.witness_status(tmp_path, "a", area)
    assert (missing, discharged) == (0, 1)
    assert rows[0][1] == "witnessed"


def test_partially_witnessed_permission_still_gates(tmp_path):
    """Proving one of several allowed behaviors reachable says nothing about
    the others, so one green outcome must not discharge the requirement."""
    outcomes = [
        {"name": "email", "predicate": "p", "status": "witnessed",
         "trace": "a/traces/REQ-007.email.itf.json"},
        {"name": "in-app", "predicate": "q", "status": "no-witness"},
    ]
    area = _may_area(tmp_path, outcomes)
    outcomes[0]["model_sha"] = itf.compute_model_sha(tmp_path, "a", area)
    rows, missing, discharged = itf.witness_status(tmp_path, "a", area)
    assert missing == 1 and discharged == 0
    assert "in-app" in rows[0][3]


def test_stale_outcome_trace_is_caught(tmp_path):
    outcomes = [
        {"name": "email", "predicate": "p", "status": "witnessed",
         "trace": "a/traces/REQ-007.email.itf.json", "model_sha": "0" * 64},
        {"name": "in-app", "predicate": "q", "status": "witnessed",
         "trace": "a/traces/REQ-007.in-app.itf.json", "model_sha": "0" * 64},
    ]
    area = _may_area(tmp_path, outcomes)
    findings = []
    lint.check_witnesses(tmp_path, area, "a", findings)
    assert any(f.check == "witness-stale" for f in findings)


def test_must_requirement_witness_handling_is_unchanged(tmp_path):
    (tmp_path / "specs" / "a" / "traces").mkdir(parents=True)
    (tmp_path / "specs" / "a.qnt").write_text("module a { var x: int }", encoding="utf-8")
    area = {"kind": "area", "area": "a", "version": "1.0.0", "status": "formalized",
            "formal_model": {"quint_file": "a.qnt"},
            "requirements": [{"id": "REQ-001", "status": "specified", "quint_ref": "x",
                              "witness": {"status": "witnessed", "predicate": "p"}}]}
    findings = []
    lint.check_witnesses(tmp_path, area, "a", findings)
    assert any(f.check == "witnessed-without-trace" for f in findings)


def test_witness_entries_handles_both_shapes():
    single = itf.witness_entries({"id": "REQ-001", "witness": {"trace": "t"}})
    assert [label for label, _ in single] == ["REQ-001"]
    multi = itf.witness_entries({"id": "REQ-007", "witness": {"outcomes": [
        {"name": "a"}, {"name": "b"}]}})
    assert [label for label, _ in multi] == ["REQ-007/a", "REQ-007/b"]


def test_extract_audit_runs_before_traceability_exists(tmp_path):
    """The brownfield beat says to run the audit DURING extraction, but
    traceability[] is written by /spec-code-generate, which has not run yet. Hard-
    requiring it made the documented workflow unexecutable."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text(
        "function f(x) {\n  if (x > 1) {\n    return 1;\n  }\n  return 0;\n}\n",
        encoding="utf-8")
    area = {"kind": "area", "area": "a", "version": "1.0.0"}   # no traceability yet
    assert mutate.traced_files(area, tmp_path) == []
    files = sorted(f for f in (tmp_path / "src").rglob("*")
                   if f.is_file() and f.suffix in audit.SOURCE_SUFFIXES)
    assert files, "the code_path fallback must find the source"
    sites = audit.scan_file(files[0], tmp_path)
    assert any(s["kind"] == "branch" for s in sites)


def test_identical_statements_in_the_same_function_are_distinct_sites(tmp_path):
    """The first fix keyed sites on the enclosing declaration, which only
    disambiguated ACROSS functions — two identical statements in one function
    still collided, and one ledger row silently claimed both decisions."""
    src = ("function first(cart) {\n"
           "  if (a) {\n    return cart;\n  }\n"
           "  if (b) {\n    return cart;\n  }\n"
           "  return cart;\n}\n")
    path = tmp_path / "x.js"
    path.write_text(src, encoding="utf-8")
    returns = [s for s in audit.scan_file(path, tmp_path) if s["snippet"] == "return cart;"]
    assert len(returns) == 3
    assert len({s["fingerprint"] for s in returns}) == 3


def test_site_owner_prefers_the_callable_over_a_local(tmp_path):
    """Keying on the nearest declaration of ANY kind meant a local `const cart`
    owned the sites below it, so renaming a local invalidated unrelated rows."""
    src = ("function handler(id) {\n"
           "  const cart = load(id);\n"
           "  if (cart.open) {\n    return 1;\n  }\n"
           "  return 0;\n}\n")
    path = tmp_path / "x.js"
    path.write_text(src, encoding="utf-8")
    assert {s["owner"] for s in audit.scan_file(path, tmp_path)} == {"handler"}


def test_renaming_a_local_does_not_move_the_fingerprints(tmp_path):
    a = ("function handler(id) {\n  const cart = load(id);\n"
         "  if (ok) {\n    return 1;\n  }\n}\n")
    b = ("function handler(id) {\n  const basket = load(id);\n"
         "  if (ok) {\n    return 1;\n  }\n}\n")
    (tmp_path / "a.js").write_text(a, encoding="utf-8")
    (tmp_path / "b.js").write_text(b, encoding="utf-8")
    fa = [s["fingerprint"] for s in audit.scan_file(tmp_path / "a.js", tmp_path)]
    fb = [s["fingerprint"] for s in audit.scan_file(tmp_path / "b.js", tmp_path)]
    # Same sites, different FILE names, so compare the owner-and-text part by
    # recomputing against one file name.
    assert len(fa) == len(fb)
    same = [audit.fingerprint("x.js", s["kind"], s["snippet"], s["owner"], 0)
            for s in audit.scan_file(tmp_path / "a.js", tmp_path)]
    other = [audit.fingerprint("x.js", s["kind"], s["snippet"], s["owner"], 0)
             for s in audit.scan_file(tmp_path / "b.js", tmp_path)]
    assert same == other


def test_diff_renders_a_removed_determinism_without_None():
    """`x or 'must' if c else x` parses as `(x or 'must') if c else x`, which
    rendered a removed determinism as 'determinism=None'."""
    old = {"area": "a", "requirements": [
        {"id": "R", "ears": {"response": "r"}, "determinism": "deterministic"}]}
    new = {"area": "a", "requirements": [{"id": "R", "ears": {"response": "r"}}]}
    entries = diff.diff_area(old, new)["behavior"]
    assert entries and "None" not in entries[0]["now"]
    assert entries[0]["now"] == "determinism=unspecified"


def test_diff_still_defaults_modality_to_must():
    old = {"area": "a", "requirements": [{"id": "R", "ears": {"response": "r"}}]}
    new = {"area": "a", "requirements": [
        {"id": "R", "ears": {"response": "r"}, "modality": "may"}]}
    entries = diff.diff_area(old, new)["behavior"]
    assert entries[0]["was"] == "modality=must"


# ── Executable bit on shipped scripts ───────────────────────────────────────
# The docs invoke the tools directly (`./tools/bootstrap.sh`,
# `tools/spec-record.py check <area>`), so a script committed 100644 is a
# "permission denied" on every fresh POSIX clone. Windows checkouts have
# core.filemode=false and cannot see the loss, so the index is the only
# reliable place to assert it.

def _git_index_modes():
    import subprocess
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "ls-files", "-s", "--", "tools"],
                         cwd=root, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    modes = {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]
    return root, modes


def test_shipped_scripts_are_executable_in_the_index():
    root, modes = _git_index_modes()
    not_exec = []
    for path, mode in sorted(modes.items()):
        f = root / path
        if not f.is_file():
            continue
        with open(f, "rb") as fh:
            if fh.read(2) != b"#!":
                continue
        if mode != "100755":
            not_exec.append(f"{path} is {mode}, want 100755")
    assert not not_exec, "\n".join(not_exec)


# ── Fresh projects must lint green ──────────────────────────────────────────
# /spec bootstrap scaffolds `formal_model.quint_file` pointing at the sidecar
# /spec-check will later write, so pointer-set-file-absent is the normal
# opening state of every project — and the /spec triage table already
# prescribes "formalize: write the sidecar" for it. Graded like the other
# precision lints: WARN while authoring, FAIL once the area is up for review.

def _sidecar_findings(status, fm=None):
    area = {"kind": "area", "area": "auth", "version": "0.1.0", "status": status,
            "formal_model": {"quint_file": "auth.qnt"} if fm is None else fm}
    findings = []
    lint.check_formal_model_consistency(area, None, "auth", findings)
    return [f for f in findings if f.check == "sidecar-missing-or-empty"]


@pytest.mark.parametrize("status", ["raw", "draft", "specified", "formalized"])
def test_aimed_pointer_without_sidecar_only_warns_while_authoring(status):
    hits = _sidecar_findings(status)
    assert len(hits) == 1
    assert hits[0].severity == lint.WARN
    assert "/spec-check" in hits[0].description


@pytest.mark.parametrize("status", ["in-review", "approved"])
def test_aimed_pointer_without_sidecar_fails_at_review(status):
    hits = _sidecar_findings(status)
    assert len(hits) == 1
    assert hits[0].severity == lint.FAIL


@pytest.mark.parametrize("fm", [{}, {"quint_file": ""}])
def test_unaimed_pointer_is_silent(fm):
    assert _sidecar_findings("in-review", fm) == []


def test_sidecar_without_module_declaration_still_gates(tmp_path):
    area = {"kind": "area", "area": "auth", "status": "approved",
            "formal_model": {"quint_file": "auth.qnt"}}
    findings = []
    lint.check_formal_model_consistency(area, {"__no_module__": True}, "auth", findings)
    assert [f.severity for f in findings] == [lint.FAIL]


# ── The report never crashes on a legacy console ────────────────────────────
# cp1252 cannot encode the status marks or the em dashes in finding text, and
# print() raises UnicodeEncodeError rather than degrading — so a Windows
# console turned every WARN into a traceback.

class _Stream(io.StringIO):
    def __init__(self, encoding):
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self):
        return self._encoding


def test_icons_stay_unicode_when_the_stream_can_encode_them():
    assert lint.pick_icons(_Stream("utf-8")) is lint.UNICODE_ICONS


@pytest.mark.parametrize("encoding", ["cp1252", "ascii", "latin-1"])
def test_icons_fall_back_to_ascii_on_a_narrow_stream(encoding):
    assert lint.pick_icons(_Stream(encoding)) is lint.ASCII_ICONS


def test_icons_fall_back_when_the_stream_reports_no_encoding():
    assert lint.pick_icons(io.StringIO()) is lint.ASCII_ICONS


def test_icons_fall_back_on_an_unknown_encoding():
    assert lint.pick_icons(_Stream("not-a-real-codec")) is lint.ASCII_ICONS


def test_report_renders_on_a_cp1252_console(monkeypatch, capsys):
    """The end-to-end regression: a WARN whose description carries an em dash,
    printed to a stream that cannot encode either it or the icon."""
    import sys as _sys

    class _Narrow(io.TextIOBase):
        encoding = "cp1252"

        def __init__(self):
            self.text = []
            self.softened = False

        def write(self, s):
            if not self.softened:
                s.encode("cp1252")   # raises exactly as the real console did
            self.text.append(s)
            return len(s)

        def reconfigure(self, **kw):
            if kw.get("errors") == "replace":
                self.softened = True

    narrow = _Narrow()
    monkeypatch.setattr(_sys, "stdout", narrow)
    findings = [lint.Finding(lint.WARN, "schema", "jsonschema-unavailable",
                             "_project", "not installed \u2014 validation SKIPPED", None)]
    lint.print_report(findings, ["_project"], use_color=False)
    out = "".join(narrow.text)
    assert "jsonschema-unavailable" in out
    assert lint.ASCII_ICONS[lint.WARN] in out


# ── Bootstrap must not gate its own tooling check on the exec bit ───────────
# `[ -x tools/check-tooling.sh ]` conflates "absent" with "present but not
# marked executable". The second is not a reason to skip: bootstrap invokes
# the script as a child and can supply the interpreter itself. Gating on it
# silently skipped the one check whose job is reporting what's missing, and
# `-x` is exactly the attribute a clone loses.

BOOTSTRAP = TOOLS / "bootstrap.sh"


def _bootstrap_text():
    # bootstrap.sh deletes itself as its last act, so it is absent in every
    # project created FROM the template — where these two tests have nothing
    # left to assert. Skip there; the guard matters in the template repo.
    if not BOOTSTRAP.exists():
        pytest.skip("bootstrap.sh already removed (bootstrapped project)")
    return BOOTSTRAP.read_text(encoding="utf-8")


def test_bootstrap_guards_check_tooling_on_presence_not_exec_bit():
    text = _bootstrap_text()
    assert "check-tooling.sh" in text, "guard moved — retarget this test"
    offenders = [ln.strip() for ln in text.splitlines()
                 if "-x " in ln and "check-tooling.sh" in ln]
    assert not offenders, "exec-bit guard is back: " + "; ".join(offenders)
    assert text.count('if [ -f "$ROOT/tools/check-tooling.sh" ]; then') == 2


def test_bootstrap_invokes_check_tooling_through_bash():
    """Supplying the interpreter is what makes the presence guard sufficient."""
    text = _bootstrap_text()
    calls = [ln.strip() for ln in text.splitlines()
             if "check-tooling.sh" in ln and "$ROOT" in ln and "[ -f" not in ln]
    assert len(calls) == 2, calls
    for call in calls:
        assert call.startswith('bash "$ROOT/tools/check-tooling.sh"'), call


# ── Assumptions belong in a change manifest ─────────────────────────────────
# check_changes re-enumerated the area's ID lists inline and the copy drifted:
# it omitted assumptions[] and examples[], so a change that touched ASM-001 or
# EX-001 could not record it — /spec says "any ID added or modified" goes in
# targets[].ids, and the gate called those two dangling. local_ids() is the
# single definition of what an area declares; the manifest check now uses it.

def _change_project(tmp_path, ids):
    (tmp_path / "specs" / "changes").mkdir(parents=True)
    area = {
        "kind": "area", "area": "auth", "version": "0.1.0", "status": "raw",
        "requirements": [{"id": "REQ-001", "ears": {"response": "r"}}],
        "invariants": [{"id": "INV-001", "statement": "s"}],
        "properties": [{"id": "PROP-001", "statement": "s"}],
        "constraints": [{"id": "CON-001", "statement": "s"}],
        "decisions": [{"id": "DEC-001", "decision": "d"}],
        "open_questions": [{"id": "Q-001", "question": "q"}],
        "assumptions": [{"id": "ASM-001", "statement": "clock is monotonic"}],
        "examples": [{"id": "EX-001", "title": "happy path"}],
    }
    manifest = {"change": "initial", "intent": "i", "status": "in-progress",
                "targets": [{"name": "auth", "kind": "area", "ids": ids}]}
    (tmp_path / "specs" / "changes" / "initial.change.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    findings = []
    lint.check_changes(tmp_path, {"auth": area}, findings)
    return [f for f in findings if f.check == "dangling-id"]


@pytest.mark.parametrize("iid", ["REQ-001", "INV-001", "PROP-001", "CON-001",
                                 "DEC-001", "Q-001", "ASM-001", "EX-001"])
def test_every_declared_id_may_appear_in_a_manifest(tmp_path, iid):
    assert _change_project(tmp_path, [iid]) == []


def test_all_declared_ids_together(tmp_path):
    every = ["REQ-001", "INV-001", "PROP-001", "CON-001",
             "DEC-001", "Q-001", "ASM-001", "EX-001"]
    assert _change_project(tmp_path, every) == []


@pytest.mark.parametrize("iid", ["ASM-999", "EX-042", "REQ-404"])
def test_an_id_the_area_does_not_declare_still_gates(tmp_path, iid):
    hits = _change_project(tmp_path, [iid])
    assert len(hits) == 1
    assert hits[0].severity == lint.FAIL
    assert hits[0].ref == iid


def test_manifest_id_check_tracks_local_ids(tmp_path):
    """The regression guard proper: the manifest gate and local_ids() must not
    diverge again, whichever list a future area block adds."""
    area = {"kind": "area", "area": "auth",
            "assumptions": [{"id": "ASM-001", "statement": "s"}],
            "examples": [{"id": "EX-001", "title": "t"}]}
    assert lint.local_ids(area) == {"ASM-001", "EX-001"}
    assert _change_project(tmp_path, sorted(lint.local_ids(area))) == []


# ── orphan-action asks reachability, not direct mention ─────────────────────
# The check credited only area-JSON references (quint_ref, quint_action,
# lifecycle_actions), so `step` dispatching to a per-branch wrapper — the
# ordinary shape once actions take differing parameters, since the hoisted
# nondet in the templates only works when branches share a parameter set —
# flagged every wrapper as dead spec text. An action the model actually runs
# is not dead, whatever the JSON says. Roots are the model's entry points
# plus everything the JSON names; anything reachable from a root is live.

WRAPPER_MODEL = """module auth {
  var sessions: int
  action init = all { sessions' = 0 }
  action login(uid: str): bool = all { sessions' = sessions + 1 }
  action logout(sid: str): bool = all { sessions' = sessions - 1 }
  action loginStep  = { nondet u = oneOf(Set("a")) login(u) }
  action logoutStep = { nondet s = oneOf(Set("s1")) logout(s) }
  action purgeAll = all { sessions' = 0 }
  action deadB = all { sessions' = 1 }
  action deadA = deadB
  action step = any { loginStep, logoutStep }
}
"""


@contextlib.contextmanager
def _forced_regex_engine():
    """Pin the parse to the regex fallback for the duration.

    lint captures quint_ir.DEFAULT_ENGINE from the environment at import, so
    an ambient QUINT_IR_ENGINE=cli on a machine without the Quint CLI makes
    every sidecar parse as "no module" — which reads as "no orphans" rather
    than as a failure. These tests are about the reachability rule, not about
    engine selection, so they state the engine instead of inheriting it.
    """
    original = lint._ir_parse_qnt
    lint._ir_parse_qnt = lambda path: quint_ir.parse_qnt(path, engine="regex")
    try:
        yield
    finally:
        lint._ir_parse_qnt = original


def _orphans(tmp_path, model=WRAPPER_MODEL, refs=("login", "logout")):
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    qnt = tmp_path / "specs" / "auth.qnt"
    qnt.write_text(model, encoding="utf-8")
    area = {"kind": "area", "area": "auth", "status": "draft",
            "formal_model": {"quint_file": "auth.qnt"},
            "requirements": [{"id": f"REQ-{i:03d}", "quint_ref": r}
                             for i, r in enumerate(refs, 1)]}
    findings = []
    with _forced_regex_engine():
        sidecar = lint.parse_sidecar(qnt)
    assert "__no_module__" not in sidecar, "sidecar failed to parse"
    lint.check_orphan_actions(area, sidecar, "auth", findings)
    return sorted(f.ref for f in findings if f.check == "orphan-action")


def test_step_wrappers_are_not_orphans(tmp_path):
    assert "loginStep" not in _orphans(tmp_path)
    assert "logoutStep" not in _orphans(tmp_path)


def test_actions_reached_only_through_a_wrapper_are_not_orphans(tmp_path):
    """login is referenced by a REQ; logout's wrapper is the only path to it
    from step. Neither the wrapper nor its callee may be flagged."""
    assert _orphans(tmp_path, refs=("login",)) == ["deadA", "deadB", "purgeAll"]


def test_a_genuinely_unreferenced_action_still_warns(tmp_path):
    assert "purgeAll" in _orphans(tmp_path)


def test_a_dead_cluster_is_reported_whole(tmp_path):
    """deadA calls deadB and nothing runs deadA. Crediting "called by some
    action" would clear deadB; reachability from roots reports both."""
    found = _orphans(tmp_path)
    assert "deadA" in found and "deadB" in found


def test_the_shipped_example_has_no_orphans(tmp_path):
    """examples/specs/auth.qnt uses the hoisted-nondet shape — the idiom the
    templates show — and must stay clean under the new rule."""
    root = TOOLS.parent / "examples"
    area = json.loads((root / "specs" / "auth.area.json").read_text(encoding="utf-8"))
    with _forced_regex_engine():
        sidecar = lint.parse_sidecar(root / "specs" / "auth.qnt")
    assert "__no_module__" not in sidecar, "sidecar failed to parse"
    findings = []
    lint.check_orphan_actions(area, sidecar, "auth", findings)
    assert [f.ref for f in findings] == []


def test_absent_call_graph_falls_back_to_direct_references(tmp_path):
    """A parser that reports no call graph must not make every action look
    reachable. None means "unknown", which is not the same as an empty graph."""
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    qnt = tmp_path / "specs" / "auth.qnt"
    qnt.write_text(WRAPPER_MODEL, encoding="utf-8")
    with _forced_regex_engine():
        sidecar = lint.parse_sidecar(qnt)
    sidecar["action_calls"] = None
    area = {"kind": "area", "area": "auth", "status": "draft",
            "requirements": [{"id": "REQ-001", "quint_ref": "login"}]}
    findings = []
    lint.check_orphan_actions(area, sidecar, "auth", findings)
    assert "loginStep" in [f.ref for f in findings]


def test_call_graph_is_narrowed_to_declared_actions(tmp_path):
    """quint_ir over-collects identifiers on purpose so the two engines can
    agree; only declared actions survive, and never self-reference."""
    qnt = tmp_path / "auth.qnt"
    qnt.write_text(WRAPPER_MODEL, encoding="utf-8")
    calls = quint_ir.parse_qnt(qnt, engine="regex")["action_calls"]
    assert calls["step"] == ["loginStep", "logoutStep"]
    assert calls["loginStep"] == ["login"]
    assert calls["login"] == []
    assert all(name not in targets for name, targets in calls.items())


# ── Both engines must report the same call graph ────────────────────────────
# CI pins QUINT_IR_ENGINE=cli so verdicts never rest on the regex fallback,
# which means the IR-side collector is the one that actually gates in CI and
# the one a machine without the Quint CLI never exercises. Drive it directly
# from a hand-built IR so it is covered either way.

def _ir_module(decls):
    return {"modules": [{"name": "auth", "declarations": decls}]}


def _ir_action(name, expr):
    return {"kind": "def", "qualifier": "action", "name": name, "expr": expr}


def _ir_app(opcode, args):
    return {"kind": "app", "opcode": opcode, "args": args}


def _ir_name(name):
    return {"kind": "name", "name": name}


def test_ir_engine_collects_callees_from_applications_and_names():
    ir = _ir_module([
        _ir_action("login", _ir_app("assign", [_ir_name("sessions"),
                                               _ir_name("sessions")])),
        _ir_action("loginStep", _ir_app("login", [_ir_name("u")])),
        _ir_action("step", _ir_app("any", [_ir_name("loginStep")])),
    ])
    calls = quint_ir._normalize_ir(ir, "auth.qnt")["action_calls"]
    assert calls["step"] == ["loginStep"]        # bare name reference
    assert calls["loginStep"] == ["login"]       # application opcode
    assert calls["login"] == []                  # vars are not actions


def test_ir_engine_narrows_and_drops_self_reference():
    ir = _ir_module([
        _ir_action("tick", _ir_app("ite", [_ir_name("tick"),
                                           _ir_name("someVal"),
                                           _ir_app("oneOf", [])])),
    ])
    assert quint_ir._normalize_ir(ir, "auth.qnt")["action_calls"] == {"tick": []}


def test_ir_engine_credits_an_action_declared_later_in_the_file():
    """Narrowing runs after the whole module is read, so forward references
    resolve — otherwise `step` at the top would credit nothing."""
    ir = _ir_module([
        _ir_action("step", _ir_app("any", [_ir_name("login")])),
        _ir_action("login", _ir_app("assign", [_ir_name("s"), _ir_name("s")])),
    ])
    assert quint_ir._normalize_ir(ir, "auth.qnt")["action_calls"]["step"] == ["login"]


def test_both_engines_agree_on_the_wrapper_model(tmp_path):
    """The regex fallback's answer for the model in WRAPPER_MODEL, stated
    explicitly: if the two engines ever diverge, one of these two tests moves."""
    qnt = tmp_path / "auth.qnt"
    qnt.write_text(WRAPPER_MODEL, encoding="utf-8")
    regex = quint_ir.parse_qnt(qnt, engine="regex")
    assert regex["source"] == "regex"
    assert regex["action_calls"]["step"] == ["loginStep", "logoutStep"]
    assert regex["action_calls"]["deadA"] == ["deadB"]


# ── A prohibition's typed discharge is honoured by every gate ───────────────
# spec-record writes witness.status "skipped" for every `forbidden`
# requirement carrying enforced_by, and never writes a justification. Three
# separate copies of the discharge gate read only `justification`, so the
# documented flow failed its own gate on the first run: authoring the
# prohibition FAILed no-witness-predicate, and running the recorder FAILed
# skipped-no-justification. enforced_by is the form the schema and lint both
# steer prohibitions toward — a checkable ID beats a sentence.

def _forbidden_area(witness, rid="REQ-002", modality="forbidden"):
    return {
        "kind": "area", "area": "auth", "version": "0.1.0", "status": "draft",
        "invariants": [{"id": "INV-001", "statement": "s", "criticality": "high"}],
        "requirements": [{"id": rid, "modality": modality, "quint_ref": "login",
                          "ears": {"trigger": "t", "response": "rejected"},
                          "witness": witness}],
    }


def _witness_findings(tmp_path, area):
    findings = []
    lint.check_witnesses(tmp_path, area, "auth", findings)
    return {f.check for f in findings if f.ref == area["requirements"][0]["id"]}


@pytest.mark.parametrize("witness", [
    {"enforced_by": "INV-001"},                      # as authored
    {"status": "skipped", "enforced_by": "INV-001"},  # as spec-record writes it
])
def test_typed_prohibition_discharges_the_witness_gate(tmp_path, witness):
    assert _witness_findings(tmp_path, _forbidden_area(witness)) == set()


def test_prose_justification_still_discharges(tmp_path):
    """The older form stays valid \u2014 this fix widens the gate, never narrows it."""
    area = _forbidden_area({"status": "skipped",
                            "justification": "rejection \u2014 enforced by INV-001"},
                           modality=None)
    assert "skipped-no-justification" not in _witness_findings(tmp_path, area)


def test_skip_with_neither_form_still_fails(tmp_path):
    area = _forbidden_area({"status": "skipped"}, rid="REQ-003", modality=None)
    assert "skipped-no-justification" in _witness_findings(tmp_path, area)


def test_a_non_forbidden_requirement_still_needs_a_predicate(tmp_path):
    """The predicate exemption is scoped to prohibitions carrying enforced_by,
    not handed to anything that omits a predicate."""
    area = _forbidden_area({}, rid="REQ-004", modality=None)
    assert "no-witness-predicate" in _witness_findings(tmp_path, area)


def test_forbidden_without_enforced_by_is_not_exempt(tmp_path):
    """modality alone must not buy the exemption \u2014 check_modality FAILs the
    missing enforced_by, and the predicate gate must not go quiet meanwhile."""
    assert "no-witness-predicate" in _witness_findings(tmp_path, _forbidden_area({}))


@pytest.mark.parametrize("witness,expected", [
    ({"status": "skipped", "enforced_by": "INV-001"}, "enforced by INV-001"),
    ({"status": "skipped", "justification": "rejection \u2014 see INV-002"},
     "rejection \u2014 see INV-002"),
    ({"status": "skipped"}, None),
    ({}, None),
    ("not-a-dict", None),
])
def test_skip_discharge_is_the_single_definition(witness, expected):
    assert itf.skip_discharge(witness) == expected


def test_enforced_by_wins_over_prose_when_both_are_present():
    """The checkable form is the one worth reporting."""
    assert itf.skip_discharge({"status": "skipped", "enforced_by": "INV-001",
                               "justification": "prose"}) == "enforced by INV-001"


def test_witness_status_counts_a_typed_skip_as_discharged(tmp_path):
    """The readback's ledger reads the same definition, so a prohibition no
    longer renders as SKIPPED-UNJUSTIFIED, and it counts as discharged rather
    than against the gate."""
    area = {"kind": "area", "area": "auth",
            "requirements": [{"id": "REQ-002", "modality": "forbidden",
                              "witness": {"status": "skipped",
                                          "enforced_by": "INV-001"}}]}
    rows, missing, discharged = itf.witness_status(tmp_path, "auth", area)
    assert [r[1] for r in rows] == ["skipped"]
    assert "INV-001" in rows[0][3]
    assert (missing, discharged) == (0, 1)


def test_witness_status_still_gates_an_undischarged_skip(tmp_path):
    area = {"kind": "area", "area": "auth",
            "requirements": [{"id": "REQ-003",
                              "witness": {"status": "skipped"}}]}
    rows, missing, discharged = itf.witness_status(tmp_path, "auth", area)
    assert rows[0][1] == "SKIPPED-UNJUSTIFIED"
    assert (missing, discharged) == (1, 0)


def test_a_typed_skip_is_a_rejection_for_refusal_purposes():
    assert itf.is_rejection({"witness": {"status": "skipped",
                                         "enforced_by": "INV-001"}})
    assert not itf.is_rejection({"witness": {"status": "skipped"}})


# ── A missing parser must not report clean ──────────────────────────────────
# QUINT_IR_ENGINE=cli demands the compiler's typed IR. With the CLI absent,
# _parse_via_cli returned None, parse_sidecar turned that into
# {"__no_module__": True}, and every check reading a sidecar returned early.
# Verdicts were reported without being computed: quint_ref resolution went
# quiet, and spec-matrix --strict — a CI gate — exited 0 over an empty event
# axis, reporting complete because it found nothing to be complete about.

def test_cli_available_reports_whether_the_binary_resolves(monkeypatch):
    monkeypatch.setattr(quint_ir, "_quint_bin", lambda: None)
    assert quint_ir.cli_available() is False
    monkeypatch.setattr(quint_ir, "_quint_bin", lambda: "/usr/bin/quint")
    assert quint_ir.cli_available() is True


def test_an_unparseable_sidecar_always_fails_regardless_of_status():
    """Distinct from a sidecar that has not been written yet, which stays
    graded by status. A file that is THERE and yields nothing is broken at
    every authoring stage, and it silences every check that reads it."""
    for status in ("raw", "draft", "in-review", "approved"):
        area = {"kind": "area", "area": "auth", "status": status,
                "formal_model": {"quint_file": "auth.qnt"}}
        findings = []
        lint.check_formal_model_consistency(area, {"__no_module__": True},
                                            "auth", findings)
        assert [(f.check, f.severity) for f in findings] == [
            ("sidecar-unparseable", lint.FAIL)], status


@pytest.mark.parametrize("status,severity", [
    ("raw", "warn"), ("draft", "warn"),
    ("in-review", "fail"), ("approved", "fail"),
])
def test_a_missing_sidecar_is_still_graded_by_status(status, severity):
    """The earlier fix must survive: not-yet-written is not the same defect."""
    area = {"kind": "area", "area": "auth", "status": status,
            "formal_model": {"quint_file": "auth.qnt"}}
    findings = []
    lint.check_formal_model_consistency(area, None, "auth", findings)
    assert [(f.check, f.severity) for f in findings] == [
        ("sidecar-missing-or-empty", severity)]


def test_matrix_refuses_an_empty_event_axis_when_the_parse_fails(tmp_path,
                                                                 monkeypatch):
    """The gate must refuse rather than pass over nothing."""
    (tmp_path / "specs").mkdir(parents=True)
    qnt = tmp_path / "specs" / "auth.qnt"
    qnt.write_text("module auth {\n  action step = all { true }\n}\n",
                   encoding="utf-8")
    monkeypatch.setattr(matrix, "parse_qnt", lambda *a, **k: None)
    with pytest.raises(SystemExit) as excinfo:
        matrix.discover_qnt_actions(tmp_path, {}, "auth")
    assert "did not parse" in str(excinfo.value)


def test_matrix_refuses_when_the_demanded_engine_is_absent(tmp_path,
                                                           monkeypatch):
    (tmp_path / "specs").mkdir(parents=True)
    (tmp_path / "specs" / "auth.qnt").write_text("module auth { }\n",
                                                 encoding="utf-8")
    monkeypatch.setattr(matrix, "DEFAULT_ENGINE", "cli")
    monkeypatch.setattr(matrix, "cli_available", lambda: False)
    with pytest.raises(SystemExit) as excinfo:
        matrix.discover_qnt_actions(tmp_path, {}, "auth")
    assert "not on PATH" in str(excinfo.value)


def test_matrix_still_returns_empty_for_an_area_with_no_sidecar(tmp_path):
    """An absent sidecar is a stage, not a failure — the area is not
    formalized yet, and that must not become a hard error."""
    (tmp_path / "specs").mkdir(parents=True)
    assert matrix.discover_qnt_actions(tmp_path, {}, "auth") == []


# ── Documented slash commands must exist ────────────────────────────────────
# The tier table's column was headed "Commands" and listed `/spec`,
# `/spec-readback`, `spec-lint`, `spec-matrix`, `spec-diff` — the last three
# are tools/*.py scripts, not commands, and the section two screens below is
# titled "The Five Commands". A reader types /spec-lint and it does not
# exist. Docs and .claude/commands/ are the same duplicated-fact problem as
# the rest, so pin them to each other.

REPO_ROOT = TOOLS.parent
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"
DOC_FILES = ["METHODOLOGY.md", "README.md"]
SLASH_RE = re.compile(r"`(/spec[a-z-]*)`")


def _declared_commands():
    return {"/" + p.stem for p in COMMANDS_DIR.glob("*.md")}


def test_command_files_exist():
    assert _declared_commands(), "no command files found — retarget this test"


@pytest.mark.parametrize("doc", DOC_FILES)
def test_every_documented_slash_command_is_real(doc):
    """A `/name` in backticks is a promise the reader can type it."""
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    declared = _declared_commands()
    # `/spec <area>` and `/spec-readback --all` carry arguments; the command
    # is the first token.
    used = {m.split()[0] for m in SLASH_RE.findall(text)}
    unknown = sorted(used - declared)
    assert not unknown, f"{doc} documents commands that do not exist: {unknown}"


def test_the_five_commands_section_matches_the_command_files():
    """METHODOLOGY calls them "The Five Commands" — if a command is added or
    removed, that heading and this test move together."""
    declared = _declared_commands()
    assert len(declared) == 5, sorted(declared)
    text = (REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    assert "## The Five Commands" in text


def _tier_rows():
    text = (REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines()
            if ln.startswith("| **1 — Precision core**")
            or ln.startswith("| **2 — Formal proof**")]


def test_the_tier_table_commands_column_holds_only_commands():
    """The regression proper: the column is headed Commands, so every entry in
    it must be one. Scripts belong in the prose that describes them, where the
    document already spells them tools/<name>.py."""
    rows = _tier_rows()
    assert len(rows) == 2, rows
    declared = _declared_commands()
    for row in rows:
        column = row.rstrip().rstrip("|").rsplit("|", 1)[-1]
        entries = [e.strip().strip("`") for e in column.split(",") if e.strip()]
        assert entries, row
        assert all(e in declared for e in entries), entries


def test_no_script_names_leak_into_the_tier_commands_column():
    for row in _tier_rows():
        column = row.rstrip().rstrip("|").rsplit("|", 1)[-1]
        for script in ("spec-lint", "spec-matrix", "spec-diff", "spec-record",
                       "spec-mutate", "spec-separation", "spec-extract-audit"):
            assert script not in column, f"{script} listed under Commands"


# ── Brownfield extraction is ongoing, not one-time ──────────────────────────
# The chapter and the /spec beat were organised around substitutability: the
# boundary was step 1, and the closing table said the differential run "is
# the one that decides whether a brownfield spec is worth anything". That is
# a fidelity measurement for a rewrite, not the reason to extract. The reason
# is a spec that is true of the code and stays true — which needs a route
# back into extraction after the code moves, and there was none: extract
# fired only when the spec was MISSING, and the only other way in was drift
# codify, gated behind /spec-code-verify.

SPEC_COMMAND = COMMANDS_DIR / "spec.md"


def test_reextract_is_reachable_from_the_routing_table():
    text = SPEC_COMMAND.read_text(encoding="utf-8")
    routing = text.split("### Step 2")[0]
    assert "re-extract" in routing, "no routing entry for re-extraction"
    row = next(ln for ln in routing.splitlines() if "re-extract" in ln)
    assert "exists" in row, "re-extract must trigger on an area that HAS a spec"


def test_reextract_beat_exists_and_does_not_require_the_verify_chain():
    """Requiring traceability, an adapter or a test command before you can
    reconcile a spec with its code is what would stop anyone doing it."""
    text = SPEC_COMMAND.read_text(encoding="utf-8")
    assert "#### re-extract" in text
    beat = text.split("#### re-extract")[1].split("#### drift codify")[0]
    assert "without" in beat and "/spec-code-verify" in beat


def test_the_boundary_is_no_longer_the_first_extraction_step():
    text = SPEC_COMMAND.read_text(encoding="utf-8")
    beat = text.split("#### brownfield extract")[1].split("#### re-extract")[0]
    first_step = next(ln for ln in beat.splitlines() if ln.startswith("##### 1."))
    assert "boundary" not in first_step.lower(), first_step
    assert "Optional" in beat, "the boundary step must survive, marked optional"
    assert "boundary" in beat, "the boundary content must not be deleted"


def test_methodology_brownfield_chapter_leads_with_keeping_the_spec_true():
    text = (REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    heading = next(ln for ln in text.splitlines() if ln.startswith("## Brownfield"))
    assert "Rebuild From" not in heading, heading
    chapter = text.split("## Brownfield")[1].split("## Mutation and Separation")[0]
    # the fidelity machinery is kept, but demoted
    assert "Optional: measuring extraction fidelity" in chapter
    assert "spec-record equiv" in chapter, "differential machinery must not be deleted"
    assert "substitutab" in chapter, "the substitutability argument must not be deleted"


def test_no_stale_cross_reference_to_the_old_chapter_title():
    for doc in DOC_FILES:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert "Specs You Could Rebuild From" not in text, doc


# ── The code-facing commands say so in their names ──────────────────────────
# /spec-check and /spec-verify did not distinguish what each acts on: one
# checks the spec against itself (Apalache, witness reachability, coverage —
# no code involved), the other checks CODE against the spec (trace replay
# through a real adapter, the test suite, drift). Renamed to /spec-code-verify
# and /spec-code-generate so the code-facing pair is obvious at a glance.

RENAMED_AWAY = ("/spec-verify", "/spec-apply")


@pytest.mark.parametrize("old", RENAMED_AWAY)
def test_the_old_command_names_are_gone(old):
    """Clean rename, no aliases — a stale name in the docs would send the
    reader to a command that does not exist."""
    for doc in DOC_FILES:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert old not in text, f"{doc} still references {old}"
    for cmd in COMMANDS_DIR.glob("*.md"):
        assert old not in cmd.read_text(encoding="utf-8"), f"{cmd.name} still references {old}"


def test_the_code_facing_commands_exist_and_are_named_for_code():
    declared = _declared_commands()
    assert {"/spec-code-verify", "/spec-code-generate"} <= declared, sorted(declared)
    assert "/spec-check" in declared, "the spec-side command keeps its name"


def test_spec_check_stays_spec_side_only():
    """The distinction the rename encodes: /spec-check never runs code. If it
    grows a step that does, the names stop being honest."""
    text = (COMMANDS_DIR / "spec-check.md").read_text(encoding="utf-8")
    assert "conformance replay" not in text.split("## Instructions")[1].split("### Step 2")[0]


@pytest.mark.parametrize("name", ["spec-code-verify", "spec-code-generate"])
def test_renamed_command_files_carry_matching_titles(name):
    text = (COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8")
    assert text.startswith(f"# /{name} "), text.splitlines()[0]


def test_spec_record_subcommands_were_not_renamed():
    """spec-record's own `check`/`verify` subcommands are separated by a
    space, not a hyphen — the rename must not have reached them."""
    text = (COMMANDS_DIR / "spec-code-verify.md").read_text(encoding="utf-8")
    assert "spec-record.py verify" in text
    assert "spec-record.py spec-code-verify" not in text


# ── The README lists the commands, the methodology explains everything ──────
# The opening carried a 17-bullet "What you get" that restated the whole
# methodology inline — a second copy of a document that ships beside it, free
# to drift. The README keeps the command surface; METHODOLOGY.md keeps the
# reasoning.

def test_readme_commands_table_covers_every_command():
    """If a command is added or renamed, the README table moves with it."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in sorted(_declared_commands()):
        assert f"`{cmd} " in readme or f"`{cmd}`" in readme, f"{cmd} missing from README"


def test_readme_defers_the_detail_to_the_methodology():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    opening = readme.split("## Quick Start")[0]
    assert "[METHODOLOGY.md](METHODOLOGY.md)" in opening
    assert "What you get:" not in opening, "the inlined methodology summary is back"


# ── The iterative claim sits where it is read ───────────────────────────────
# "It's not a ceremony — do any part at any time" was the last line of Core
# Concepts, below the repo layout, after a left-to-right flowchart that reads
# as a pipeline you march through. A reader forms the wrong model of the
# workflow well before reaching the sentence that corrects it.

def test_the_iterative_claim_is_in_the_opening():
    text = (REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    opening = text.split("## Core Concepts")[0]
    assert "### The arrows are dependencies, not a schedule" in opening
    assert "in any order, at any time" in opening
    # it must land after the diagram it reinterprets, not before
    assert opening.index("```mermaid") < opening.index("### The arrows are dependencies")


def test_the_workflow_machinery_denial_survived_the_move():
    """The load-bearing half: no propose/approve/sync, git is the tracker,
    PR review is the approval, /spec-code-verify is the gate."""
    opening = (REPO_ROOT / "METHODOLOGY.md").read_text(
        encoding="utf-8").split("## Core Concepts")[0]
    for claim in ("propose", "PR review is the approval", "/spec-code-verify"):
        assert claim in opening, claim


def test_the_phase_list_matches_the_renamed_commands():
    """The list said "check -> verify -> apply", which put verifying code
    before generating it and used the old command's name."""
    opening = (REPO_ROOT / "METHODOLOGY.md").read_text(
        encoding="utf-8").split("## Core Concepts")[0]
    line = next(ln for ln in opening.splitlines() if "elicit" in ln and "formalize" in ln)
    assert "generate \u2192 verify" in line, line
    assert "apply" not in line, line


# ── Core Concepts defines, The Two Kinds explains ───────────────────────────
# Core Concepts carried per-kind bullets that restated "The Two Kinds of
# Area" — including the UI-blocks summary — so the same facts sat in two
# places with nothing keeping them in step. Core Concepts now names the two
# kinds and points; the detail has one home.

def _section(text, heading, next_heading):
    return text.split(heading)[1].split(next_heading)[0]


def test_core_concepts_does_not_restate_the_two_kinds():
    text = (REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    core = _section(text, "## Core Concepts", "## Quick Start")
    assert "The Two Kinds of Area" in core, "it must point at the detail"
    # the giveaway of the old duplication: per-kind bullet lines
    assert '- `kind: "area"`' not in core
    assert '- `kind: "contract"`' not in core
    assert "ui_components" not in core, "UI blocks are explained in The Two Kinds"


def test_the_two_kinds_still_carries_the_detail():
    text = (REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    kinds = _section(text, "## The Two Kinds of Area", "## EARS")
    for needle in ("spans", "ui_components", "navigation", "login, billing, search"):
        assert needle in kinds, needle


def test_core_concepts_keeps_what_only_it_says():
    """Dedup must not drop the definitions that live nowhere else."""
    core = _section((REPO_ROOT / "METHODOLOGY.md").read_text(encoding="utf-8"),
                    "## Core Concepts", "## Quick Start")
    for needle in ("specs/<name>.area.json", "suffix encodes the `kind`",
                   ".spec/project.json", ".spec/local.json"):
        assert needle in core, needle


# ── The one authored section, pinned like a witness ─────────────────────────
# The readback is deterministic by construction, which is what makes its git
# diff the review — but it went from a one-line purpose straight into Quint
# excerpts, with no altitude in between. A prose brief supplies that, as an
# INPUT rendered verbatim rather than composed at render time, so
# determinism survives. Prose cannot be checked the way a witness can, so it
# carries the same freshness pin a witness trace does.

def _area_with_brief(**brief):
    area = {"kind": "area", "area": "auth", "version": "0.1.0", "status": "formalized",
            "purpose": "p",
            "requirements": [{"id": "REQ-001", "ears": {"response": "a session exists"}}]}
    area["brief"] = brief
    return area


def test_spec_sha_ignores_bookkeeping_churn():
    """Re-running the checker must not invalidate prose it cannot affect."""
    area = _area_with_brief(text="x")
    before = itf.compute_spec_sha(area)
    area["check_results"] = {"ran_at": "2026-01-01", "checks": [{"id": "INV-001"}]}
    area["requirements"][0]["witness"] = {"status": "witnessed", "trace": "t.json"}
    area["verification_log"] = [{"at": "now"}]
    assert itf.compute_spec_sha(area) == before


def test_spec_sha_moves_when_a_claim_moves():
    area = _area_with_brief(text="x")
    before = itf.compute_spec_sha(area)
    area["requirements"][0]["ears"]["response"] = "something else entirely"
    assert itf.compute_spec_sha(area) != before


@pytest.mark.parametrize("mutate", [
    lambda a: a["requirements"].append({"id": "REQ-002", "ears": {"response": "r"}}),
    lambda a: a.update(purpose="a different purpose"),
    lambda a: a.update(scope={"included": ["x"]}),
    lambda a: a.update(constraints=[{"id": "CON-001", "name": "MAX", "value": 5}]),
])
def test_spec_sha_covers_what_a_brief_could_be_wrong_about(mutate):
    area = _area_with_brief(text="x")
    before = itf.compute_spec_sha(area)
    mutate(area)
    assert itf.compute_spec_sha(area) != before


def test_brief_status_reports_absent_current_and_stale():
    area = _area_with_brief(text="")
    assert itf.brief_status(area)[0] == "absent"
    area = _area_with_brief(text="a real brief")
    assert itf.brief_status(area)[0] == "stale", "unpinned prose is not trustworthy"
    area["brief"]["written_against"] = itf.compute_spec_sha(area)
    assert itf.brief_status(area)[0] == "current"
    area["requirements"][0]["ears"]["response"] = "moved"
    assert itf.brief_status(area)[0] == "stale"


@pytest.mark.parametrize("status,severity", [
    ("raw", "warn"), ("formalized", "warn"),
    ("in-review", "fail"), ("approved", "fail"),
])
def test_a_stale_brief_warns_while_authoring_and_fails_at_review(status, severity):
    area = _area_with_brief(text="a real brief", written_against="0" * 64)
    area["status"] = status
    findings = []
    lint.check_brief(area, "auth", findings)
    hits = [f for f in findings if f.check == "brief-stale"]
    assert len(hits) == 1 and hits[0].severity == severity


def test_no_brief_means_no_findings():
    findings = []
    lint.check_brief({"kind": "area", "area": "auth"}, "auth", findings)
    assert findings == []


def test_the_brief_is_rendered_verbatim_not_composed():
    """Determinism depends on this: the generator copies the text through."""
    area = _area_with_brief(text="EXACT PROSE HERE", how_it_fits="FACET TEXT")
    area["brief"]["written_against"] = itf.compute_spec_sha(area)
    out = "\n".join(readback.brief_section(area))
    assert "EXACT PROSE HERE" in out and "FACET TEXT" in out
    assert "How it fits together" in out
    assert "prose, not machine-checked" in out


def test_a_stale_brief_is_marked_in_the_readback():
    area = _area_with_brief(text="prose", written_against="0" * 64)
    out = "\n".join(readback.brief_section(area))
    assert "may be out of date" in out


def test_brief_section_is_empty_without_a_brief():
    assert readback.brief_section({"kind": "area", "area": "auth"}) == []


def test_at_a_glance_indexes_the_requirements():
    area = {"requirements": [
        {"id": "REQ-001", "ears": {"response": "first thing"}},
        {"id": "REQ-002", "ears": {"response": "second thing"}, "modality": "may"},
        {"id": "REQ-003", "ears": {"response": "gone"}, "status": "deferred"},
    ]}
    out = "\n".join(readback.at_a_glance(area))
    assert "REQ-001" in out and "REQ-002" in out
    assert "REQ-003" not in out, "deferred requirements are not part of the picture"
    assert "may" in out


def test_at_a_glance_skips_itself_when_there_is_nothing_to_index():
    assert readback.at_a_glance({"requirements": [{"id": "REQ-001"}]}) == []


def test_shape_diagram_is_derived_and_deterministic():
    area = {"area": "auth", "concepts": {"entities": [
                {"name": "Session", "states": ["Active", "Expired"], "closed": True}]},
            "externals": [{"name": "BillingProvider", "outcomes": ["OK", "TIMEOUT"]}]}
    once = readback.shape_diagram(area)
    assert once == readback.shape_diagram(area), "same input, same output"
    out = "\n".join(once)
    assert "```mermaid" in out and "2 states" in out and "closed" in out
    assert "BillingProvider" in out and "2 outcomes" in out


def test_shape_diagram_absent_when_there_is_nothing_to_draw():
    assert readback.shape_diagram({"area": "auth"}) == []


def test_a_typed_prohibition_renders_as_discharged_not_pending():
    """status_mark read only `justification`, so a forbidden requirement
    discharged by enforced_by showed as not-yet-checked."""
    assert readback.status_mark(
        {"witness": {"status": "skipped", "enforced_by": "INV-001"}}) == "\u2298"
    assert readback.status_mark(
        {"witness": {"status": "skipped", "justification": "prose"}}) == "\u2298"
    assert readback.status_mark({"witness": {"status": "skipped"}}) == "\u23f3"


def test_the_shipped_example_carries_a_current_brief():
    area = json.loads((TOOLS.parent / "examples" / "specs" / "auth.area.json")
                      .read_text(encoding="utf-8"))
    assert itf.brief_status(area)[0] == "current", "example brief pin is stale"


# ── The meaning: the requirement in plain words, pinned per requirement ─────
# EARS fields speak the system's vocabulary, identifiers and exception names
# included. `meaning.text` says what that amounts to for a reviewer who has
# never seen the code. It is authored prose, so it is pinned, not derived —
# and a stale one is never rendered.

def _req_with_meaning(text="In plain words: the thing happens.", **meaning):
    req = {"id": "REQ-001", "status": "specified",
           "ears": {"trigger": "an update carries a different serviceLevelPolicyId",
                    "unwanted": True,
                    "response": "refuse with NonMatchingTopologyException"}}
    if text:
        req["meaning"] = dict(text=text, **meaning)
    return req


def test_meaning_sha_ignores_bookkeeping_and_moves_with_the_requirement():
    req = _req_with_meaning()
    before = itf.compute_meaning_sha(req)
    req["witness"] = {"status": "witnessed", "trace": "t.json"}
    req["extraction"] = {"evidence": "svc.ts:10-20"}
    assert itf.compute_meaning_sha(req) == before, \
        "re-running the checker cannot make a plain-words sentence wrong"
    req["ears"]["response"] = "accept the update"
    assert itf.compute_meaning_sha(req) != before


def test_meaning_pins_are_independent_per_requirement():
    a, b = _req_with_meaning(), _req_with_meaning()
    b["id"] = "REQ-002"
    b_before = itf.compute_meaning_sha(b)
    a["ears"]["response"] = "moved"
    assert itf.compute_meaning_sha(b) == b_before, \
        "editing one requirement must not unpin another's meaning"


def test_meaning_status_reports_absent_current_and_stale():
    assert itf.meaning_status(_req_with_meaning(text=""))[0] == "absent"
    req = _req_with_meaning()
    assert itf.meaning_status(req)[0] == "stale", "unpinned prose is not trustworthy"
    req["meaning"]["written_against"] = itf.compute_meaning_sha(req)
    assert itf.meaning_status(req)[0] == "current"
    req["ears"]["response"] = "moved"
    assert itf.meaning_status(req)[0] == "stale"


@pytest.mark.parametrize("status,severity", [
    ("structured", "warn"), ("formalized", "warn"),
    ("in-review", "fail"), ("approved", "fail"),
])
@pytest.mark.parametrize("check,req", [
    ("meaning-missing", _req_with_meaning(text="")),
    ("meaning-stale", _req_with_meaning(written_against="0" * 64)),
])
def test_missing_or_stale_meaning_warns_then_fails_at_review(status, severity,
                                                             check, req):
    findings = []
    lint.check_meaning({"status": status, "requirements": [json.loads(json.dumps(req))]},
                       "auth", findings)
    hits = [f for f in findings if f.check == check]
    assert len(hits) == 1 and hits[0].severity == severity


@pytest.mark.parametrize("status", ["raw", "deferred"])
def test_requirements_with_nothing_to_distil_are_exempt(status):
    req = _req_with_meaning(text="")
    req["status"] = status
    findings = []
    lint.check_meaning({"status": "approved", "requirements": [req]}, "auth", findings)
    assert findings == []


def test_a_current_meaning_is_the_sentence_the_readback_leads_with():
    req = _req_with_meaning()
    req["meaning"]["written_against"] = itf.compute_meaning_sha(req)
    out = "\n".join(readback.render_requirement(".", {"area": "auth"}, req, [], set()))
    assert "In plain words: the thing happens." in out
    # What was specified stays reachable, one click away.
    assert "**As specified (EARS):** If an update carries a different " \
           "serviceLevelPolicyId" in out
    assert "EARS sentence" in out, "the fold has to say what it hides"


def test_a_stale_meaning_is_not_rendered_as_the_headline():
    """A distillation of an earlier requirement reads like it was reviewed."""
    req = _req_with_meaning(text="STALE PROSE", written_against="0" * 64)
    out = "\n".join(readback.render_requirement(".", {"area": "auth"}, req, [], set()))
    headline = [ln for ln in out.split("\n") if ln.startswith("⏳")][0]
    assert "NonMatchingTopologyException" in headline, "fell back to EARS"
    assert "Plain-words summary is stale" in out
    assert "STALE PROSE" in out, "the superseded sentence is quoted, not hidden"


def test_meaning_carries_constraint_values_inline_like_the_ears_sentence():
    req = _req_with_meaning(text="The account locks once failures reach MAX_TRIES.")
    req["meaning"]["written_against"] = itf.compute_meaning_sha(req)
    cons = [{"id": "CON-001", "name": "MAX_TRIES", "value": 5, "unit": "attempts"}]
    assert "MAX_TRIES (= 5 attempts, CON-001)" in readback.req_sentence(req, cons)


def test_missing_meanings_roll_up_in_needs_your_attention(tmp_path):
    area = {"area": "auth", "requirements": [
        _req_with_meaning(text=""),
        dict(_req_with_meaning(text=""), id="REQ-002"),
        dict(_req_with_meaning(text=""), id="REQ-003", status="raw"),
    ]}
    items = readback.attention_items(str(tmp_path), "auth", area)
    hits = [i for i in items if "No plain-words summary" in i]
    assert len(hits) == 1
    assert "2 requirement(s)" in hits[0] and "REQ-003" not in hits[0]


def test_the_shipped_examples_carry_current_meanings():
    for name in ("auth", "cart", "subscription", "auth-ui"):
        area = json.loads((TOOLS.parent / "examples" / "specs" / f"{name}.area.json")
                          .read_text(encoding="utf-8"))
        for req in area["requirements"]:
            if req.get("status") in ("raw", "deferred"):
                continue
            assert itf.meaning_status(req)[0] == "current", \
                f"{name}/{req['id']} meaning pin is stale"


# ── The quint surface beyond `verify --invariant` ───────────────────────────
# Six accelerations/corrections landed at once, and five of them are
# optimisations of an existing path. The property that matters across all of
# them: none may turn a verdict green that the old path called red, and none
# may write a verdict at all from the simulator. These tests pin that.


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _capture_cmds(monkeypatch, module, outcomes):
    """Record every argv `module` shells out, answering each with the next
    entry of `outcomes` (a _FakeProc, or a callable taking the cmd)."""
    seen = []
    queue = list(outcomes)

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        nxt = queue.pop(0) if queue else _FakeProc()
        return nxt(cmd) if callable(nxt) else nxt

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return seen


def test_a_property_is_checked_as_temporal_not_as_an_invariant(monkeypatch, tmp_path):
    """The bug this fixes: properties[] are liveness formulas, and they were
    passed to --invariant like state predicates. A temporal formula is not a
    state predicate, so that was wrong regardless of the outcome."""
    seen = _capture_cmds(monkeypatch, record, [_FakeProc(0)])
    result, _, _ = record.run_verify(
        "quint", tmp_path / "a.qnt", "eventualLogout", 10, 60, temporal=True)
    assert result == "verified"
    cmd = seen[0]
    assert "--temporal=eventualLogout" in cmd
    assert not any(c.startswith("--invariant") for c in cmd)
    # TLC by default: Apalache's temporal support is only partial.
    assert "--backend=tlc" in cmd
    # ...and --max-steps / --out-itf are Apalache-only flags.
    assert not any(c.startswith("--max-steps") for c in cmd)
    assert not any(c.startswith("--out-itf") for c in cmd)


def test_a_temporal_violation_is_read_from_quints_own_marker(monkeypatch, tmp_path):
    """TLC writes no ITF, so the usual 'did the trace file appear' test cannot
    decide. The marker decides instead — and its ABSENCE must report error,
    never a guessed verdict."""
    _capture_cmds(monkeypatch, record,
                  [_FakeProc(1, stdout="[violation] Found an issue")])
    assert record.run_verify("quint", tmp_path / "a.qnt", "p", 10, 60,
                             temporal=True)[0] == "counterexample"

    _capture_cmds(monkeypatch, record,
                  [_FakeProc(1, stderr="error: could not resolve name p")])
    assert record.run_verify("quint", tmp_path / "a.qnt", "p", 10, 60,
                             temporal=True)[0] == "error"


def test_batched_verify_uses_repeated_flags_and_reports_only_the_batch(
        monkeypatch, tmp_path):
    """One space-separated list would let the array-typed flag swallow the
    positional input file, so each name gets its own --invariants."""
    seen = _capture_cmds(monkeypatch, record, [_FakeProc(0)])
    result, _, _ = record.run_verify_batch(
        "quint", tmp_path / "a.qnt", ["invA", "invB"], 10, 60)
    assert result == "verified"
    assert seen[0].count("--invariants=invA") == 1
    assert seen[0].count("--invariants=invB") == 1

    _capture_cmds(monkeypatch, record, [_FakeProc(1, stdout="[violation]")])
    # 'not-verified' is deliberately not a per-name verdict: it means only
    # "re-run these one at a time", which is where attribution and traces
    # come from.
    assert record.run_verify_batch(
        "quint", tmp_path / "a.qnt", ["invA"], 10, 60)[0] == "not-verified"


def test_the_simulator_reports_witness_counts_and_never_a_verdict(
        monkeypatch, tmp_path):
    out = ("Witnesses: witness_REQ_001 was witnessed in 7094 trace(s) out of "
           "10000 explored (70.94%)\n"
           "Witnesses: witness_REQ_002 was witnessed in 0 trace(s) out of "
           "10000 explored (0.00%)\n"
           "[ok] No violation found (599ms).")
    _capture_cmds(monkeypatch, record, [_FakeProc(0, stdout=out)])
    result, _, _, counts = record.run_simulate(
        "quint", tmp_path / "a.qnt", [], 10000, 20, 60,
        witnesses=["witness_REQ_001", "witness_REQ_002"])
    # "ok", not "verified". The word choice is the point: the simulator
    # explored, it did not prove.
    assert result == "ok"
    assert counts["witness_REQ_001"] == (7094, 10000)
    assert counts["witness_REQ_002"] == (0, 10000)


def test_quint_supports_degrades_instead_of_failing(monkeypatch):
    """An older quint must run exactly the command line it ran before, not
    die on a flag it has never heard of."""
    record._QUINT_CAPS.clear()
    _capture_cmds(monkeypatch, record,
                  [_FakeProc(0, stdout="Options:\n  --max-samples\n")])
    assert record.quint_supports("quint", "run", "--backend") is False
    # Probed once, then cached: one --help per flag, not one per area.
    _capture_cmds(monkeypatch, record, [])
    assert record.quint_supports("quint", "run", "--backend") is False
    record._QUINT_CAPS.clear()


def _quint_project(tmp_path, area="auth", invariants=(), properties=(),
                   project=None):
    (tmp_path / ".spec").mkdir(exist_ok=True)
    (tmp_path / ".spec" / "project.json").write_text(
        json.dumps(project or {"project": "p"}), encoding="utf-8")
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    (specs / (area + ".qnt")).write_text("module auth {\n}\n", encoding="utf-8")
    area_path = specs / (area + ".area.json")
    area_path.write_text(json.dumps({
        "area": area, "version": "1.0.0", "status": "formalized",
        "invariants": list(invariants), "properties": list(properties),
        "formal_model": {"quint_file": area + ".qnt"},
    }), encoding="utf-8")
    return area_path


def test_a_clean_batch_records_the_same_verdicts_for_one_apalache_start(
        tmp_path, monkeypatch):
    area_path = _quint_project(tmp_path, invariants=[
        {"id": "INV-001", "description": "a", "quint_name": "invA"},
        {"id": "INV-002", "description": "b", "quint_name": "invB"},
    ])
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_verify_batch",
                        lambda *a, **k: ("verified", "", 1.5))
    monkeypatch.setattr(record, "run_verify",
                        lambda *a, **k: pytest.fail("per-id run after a clean batch"))
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "auth"))
    assert exc.value.code == 0
    written = json.loads(area_path.read_text(encoding="utf-8"))
    assert [i["formal_status"] for i in written["invariants"]] == ["verified", "verified"]
    # Recorded as batched: a batched check and a per-id one should not read alike.
    assert all(c["batched"] for c in written["check_results"]["checks"])


def test_a_dirty_batch_falls_back_to_exactly_the_old_per_id_path(
        tmp_path, monkeypatch):
    """The safety property of the whole optimisation: a batch that is not
    clean must change nothing — same verdicts, same traces, same exit code."""
    area_path = _quint_project(tmp_path, invariants=[
        {"id": "INV-001", "description": "a", "quint_name": "invA"},
        {"id": "INV-002", "description": "b", "quint_name": "invB"},
    ])
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_verify_batch",
                        lambda *a, **k: ("not-verified", "[violation]", 1.0))
    calls = []

    def per_id(quint, qnt, name, *a, **k):
        calls.append(name)
        return ("counterexample", "", 0.2) if name == "invB" else ("verified", "", 0.1)

    monkeypatch.setattr(record, "run_verify", per_id)
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "auth"))
    assert exc.value.code == 1
    assert calls == ["invA", "invB"]
    written = json.loads(area_path.read_text(encoding="utf-8"))
    assert [i["formal_status"] for i in written["invariants"]] == [
        "verified", "counterexample-found"]
    assert not any(c.get("batched") for c in written["check_results"]["checks"])


def test_only_simulate_writes_no_verdict_and_does_not_stamp_ran_at(
        tmp_path, monkeypatch):
    """check_results.ran_at is what every phase-flag consumer reads as 'this
    area was checked'. A run that verified nothing must not set it."""
    area_path = _quint_project(tmp_path, invariants=[
        {"id": "INV-001", "description": "a", "quint_name": "invA"},
    ])
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_simulate",
                        lambda *a, **k: ("violation", "[violation] found", 0.6, {}))
    monkeypatch.setattr(
        record, "run_verify",
        lambda *a, **k: pytest.fail("model checker ran under --only-simulate"))
    args = _Args(tmp_path, "auth")
    args.no_simulate, args.only_simulate = False, True
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(args)
    assert exc.value.code == 0
    written = json.loads(area_path.read_text(encoding="utf-8"))
    cr = written["check_results"]
    assert cr["simulation"]["invariants"]["result"] == "violation"
    assert "ran_at" not in cr, "an advisory run must not mark the area checked"
    assert "checks" not in cr
    # And above all: a simulator violation writes no formal_status.
    assert "formal_status" not in written["invariants"][0]


def test_action_params_reads_quints_nondet_record_and_the_ghost_vars():
    """Two conventions, one fact. --mbt traces carry one option-wrapped
    record; probe-module traces carry one var per parameter."""
    mbt_state = {"mbt::actionTaken": "login",
                 "mbt::nondetPicks": {"uid": {"tag": "Some", "value": "bob"},
                                      "sid": {"tag": "Some", "value": "s1"},
                                      "amount": {"tag": "None", "value": {}}}}
    assert itf.action_params(mbt_state) == ["bob", "s1"]

    ghost_state = {"_lastAction": "login", "_lastUid": "bob", "_lastSid": "s1"}
    assert itf.action_params(ghost_state) == ["bob", "s1"]
    # _lastAction is the label, never a parameter.
    assert "login" not in itf.action_params(ghost_state)


def test_a_liveness_check_is_rendered_with_the_checker_that_produced_it():
    """A check from TLC is bounded by the model's finite state space; one from
    Apalache is bounded by a step depth. One mark cannot honestly mean both."""
    area = {"area": "auth",
            "properties": [{"id": "PROP-001", "description": "Sessions end.",
                            "quint_name": "eventualLogout",
                            "formal_status": "verified"}],
            "invariants": [{"id": "INV-001", "description": "x",
                            "quint_name": "inv", "formal_status": "verified"}],
            "check_results": {"checks": [
                {"id": "PROP-001", "kind": "property", "backend": "tlc",
                 "result": "verified"}]}}
    out = "\n".join(readback.invariants_section(area))
    assert "✓ (TLC)" in out
    assert "explicit-state" in out and "no step bound" in out

    area["check_results"]["checks"][0]["backend"] = "apalache"
    out = "\n".join(readback.invariants_section(area))
    assert "✓ (bounded)" in out and "✓ (TLC)" not in out


def test_a_temporal_counterexample_records_no_trace_it_does_not_have(
        tmp_path, monkeypatch):
    """TLC writes no ITF. A `trace` field pointing at a file that was never
    written is exactly what the witness gate FAILs on elsewhere — so the
    absence has to be recorded as an absence, with a note saying why."""
    area_path = _quint_project(tmp_path, properties=[
        {"id": "PROP-001", "description": "Sessions end.",
         "quint_name": "eventualLogout"},
    ])
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(
        record, "run_verify",
        lambda *a, **k: ("counterexample", "no trace: --out-itf is "
                         "Apalache-only, this ran on tlc", 3.0))
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "auth"))
    assert exc.value.code == 1
    written = json.loads(area_path.read_text(encoding="utf-8"))
    assert written["properties"][0]["formal_status"] == "counterexample-found"
    entry = written["check_results"]["checks"][0]
    assert entry["backend"] == "tlc"
    assert "trace" not in entry
    assert "Apalache-only" in entry["note"]


# ── The EARS↔model bridge ───────────────────────────────────────────────────
# Everything exact in this linter used to live on ONE side of the bridge:
# predicate↔action, constraint↔literal. Everything crossing into prose was a
# regex on the sentence alone — `state-not-bound` checks that ears.state names
# a declared state, and never that the model gates on it. These three checks
# cross it, using only facts the typed IR already carries: which var holds a
# declared state, what each action reads, and which variant an assignment
# builds. No NL understanding, no new authoring burden.

BRIDGE_SIDECAR = {
    "source": "quint-cli",
    "type_variants": {"AccountStatus": ["Locked", "Unlocked"],
                      "SessionStatus": ["Active", "Expired"]},
    "var_types": {"accountStatus": ["UserId", "AccountStatus"],
                  "sessions": ["SessionId", "SessionStatus"]},
    "action_reads": {"login": ["sessions"], "lock": ["accountStatus"]},
    "action_mutations": {"login": ["sessions"], "lock": ["accountStatus"]},
    "action_preserves": {"login": ["accountStatus"], "lock": ["sessions"]},
    "action_produces": {"login": ["Active"], "lock": ["Locked"]},
    "produced_variants": ["Active", "Locked"],
}

BRIDGE_AREA = {
    "area": "auth", "status": "approved",
    "concepts": {"entities": [
        {"name": "Account", "states": ["Locked", "Unlocked"]},
        {"name": "Session", "states": ["Active", "Expired"]}]},
}


def _bridge(check, reqs, sidecar=None):
    """Run one bridge check over a one-requirement area; return its codes."""
    area = dict(BRIDGE_AREA, requirements=list(reqs))
    findings = []
    check(area, sidecar or BRIDGE_SIDECAR, "auth", findings)
    return [f.check for f in findings]


def test_a_precondition_the_action_never_reads_is_not_a_translation_of_it():
    """'While the account is Locked' over an action that never looks at
    accountStatus. The sentence and the model disagree, and which var holds
    'Locked' is derivable — var_types × type_variants — so nobody has to
    write the mapping down."""
    assert _bridge(lint.check_ears_guard_correspondence,
                   [{"id": "REQ-001", "quint_ref": "login",
                     "ears": {"state": "While the account is Locked",
                              "response": "x"}}]) == ["guard-not-in-action"]
    # The same sentence over the action that does read it: silent.
    assert _bridge(lint.check_ears_guard_correspondence,
                   [{"id": "REQ-002", "quint_ref": "lock",
                     "ears": {"state": "While the account is Locked",
                              "response": "x"}}]) == []


def test_the_guard_check_stands_down_under_the_regex_parser():
    """The fallback scans action bodies only, so a var a guard reaches
    through a helper (`not(isLocked(uid))`) is invisible to it. FAILing on a
    read the parser cannot see would fail correct models, so the check asks
    which engine answered."""
    lossy = dict(BRIDGE_SIDECAR, source="regex")
    assert _bridge(lint.check_ears_guard_correspondence,
                   [{"id": "REQ-001", "quint_ref": "login",
                     "ears": {"state": "While the account is Locked",
                              "response": "x"}}], lossy) == []


def test_a_response_promising_a_state_the_action_never_writes():
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-004", "quint_ref": "login",
                     "ears": {"response": "the account shall become Locked"}}]
                   ) == ["effect-not-in-action"]


def test_the_right_variable_moved_to_the_wrong_state():
    """`login` does write `sessions` — so a var-level check passes it. The
    CLI parser knows it builds `Active` and never `Expired`, which is the
    sharper question and the one worth asking when it can be answered."""
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-003", "quint_ref": "login",
                     "ears": {"response": "the session shall become Expired"}}]
                   ) == ["effect-wrong-variant"]
    # Under the regex parser, which misses a variant built across a
    # continuation line, the check drops to var-level rather than guessing.
    lossy = dict(BRIDGE_SIDECAR, source="regex")
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-003", "quint_ref": "login",
                     "ears": {"response": "the session shall become Expired"}}],
                   lossy) == []


def test_a_preservation_response_is_verified_not_exempted():
    """'LEAVE the subscription Active' is implemented by `x' = x`, which
    action_mutations deliberately drops as a non-mutation. Demanding a
    mutation there fails the model for being right — the shipped
    subscription example is exactly that case. So a preservation is checked
    against action_preserves instead, which also makes the opposite error
    reportable: a sentence promising no change over an action that makes
    one."""
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-005", "quint_ref": "login",
                     "ears": {"response": "leave the account Locked"}}]) == []
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-006", "quint_ref": "login",
                     "ears": {"response": "the session shall remain Active"}}]
                   ) == ["effect-contradicts-action"]


def test_a_correct_requirement_produces_no_bridge_findings():
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-007", "quint_ref": "login",
                     "ears": {"response": "the session shall become Active"}}]) == []


def test_a_prohibition_is_not_asked_to_implement_what_it_forbids():
    assert _bridge(lint.check_ears_effect_correspondence,
                   [{"id": "REQ-008", "quint_ref": "login",
                     "modality": "forbidden",
                     "ears": {"response": "the account shall not become Locked"}}]) == []


def test_a_declared_state_no_assignment_can_produce():
    """One level above a vacuous witness: nothing can enter the state at
    all, so every requirement and invariant naming it holds for free."""
    codes = _bridge(lint.check_unproducible_states, [])
    assert codes == ["state-never-produced", "state-never-produced"]
    # Produced states are not flagged; only Expired and Unlocked are missing
    # from produced_variants.
    area = dict(BRIDGE_AREA, requirements=[])
    findings = []
    lint.check_unproducible_states(area, BRIDGE_SIDECAR, "auth", findings)
    assert sorted(f.ref for f in findings) == ["Expired", "Unlocked"]
    assert all(f.severity == lint.WARN for f in findings), (
        "detection is conservative — a state built only inside a helper's "
        "return value is missed, so this reports, it does not gate")


def test_the_ir_separates_a_mutation_from_an_identity_assignment():
    """`x' = x` is the no-change idiom and Quint requires every var to be
    assigned in every action. Counting those as mutations would make the
    effect check meaningless; discarding them entirely would make every
    preservation look like a model that ignores the variable."""
    qnt = TOOLS.parent / "examples" / "specs" / "subscription.qnt"
    ir = quint_ir.parse_qnt(qnt, engine="regex")
    assert ir["action_mutations"]["cancel_times_out"] == ["lastBillingResult"]
    assert "status" in ir["action_preserves"]["cancel_times_out"]
    assert "status" not in ir["action_mutations"]["cancel_times_out"]


def test_the_ir_maps_a_declared_state_to_the_var_that_holds_it():
    qnt = TOOLS.parent / "examples" / "specs" / "auth.qnt"
    ir = quint_ir.parse_qnt(qnt, engine="regex")
    assert ir["var_types"]["accountStatus"] == ["UserId", "AccountStatus"]
    assert "Locked" in ir["type_variants"]["AccountStatus"]
    assert ir["action_produces"]["login"] == ["Active"]
    assert set(ir["produced_variants"]) == {
        "Active", "Expired", "Locked", "LoggedOut", "Unlocked"}


def test_the_shipped_examples_trip_no_bridge_check():
    """These checks ship enabled. An example that fails them would teach the
    wrong thing on the first run."""
    specs = TOOLS.parent / "examples" / "specs"
    for area_file in sorted(specs.glob("*.area.json")) + sorted(
            specs.glob("*.contract.json")):
        area = json.loads(area_file.read_text(encoding="utf-8"))
        name = area_file.name.split(".")[0]
        qnt = specs / f"{name}.qnt"
        if not qnt.exists():
            continue
        sidecar = lint.parse_sidecar(qnt)
        findings = []
        lint.check_ears_guard_correspondence(area, sidecar, name, findings)
        lint.check_ears_effect_correspondence(area, sidecar, name, findings)
        lint.check_unproducible_states(area, sidecar, name, findings)
        assert not findings, [
            f"{name}: {f.check} [{f.ref}] {f.description}" for f in findings]


# ── Spec↔code provenance ────────────────────────────────────────────────────
# verification_log records that a spec commit and a code commit were once
# CHECKED together. It does not record what the code was BUILT TO, or what the
# spec was READ FROM — different facts, established at different moments, and
# the ones that answer "what has moved since". generated_from and
# extracted_from carry those; `spec-record changed` reads them.


class _StampArgs:
    def __init__(self, root, area, generated=False, extracted=False,
                 code_path=None, code_root=None, since=None):
        self.root, self.area = str(root), area
        self.generated, self.extracted = generated, extracted
        self.code_path, self.code_root, self.since = code_path, code_root, since


def _git_repo(path, files):
    """A real git repo — these commands shell out to git on purpose, so a
    faked sha would test the mock rather than the behavior."""
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    for cmd in (["git", "init", "-q", "."],
                ["git", "config", "user.email", "t@t.t"],
                ["git", "config", "user.name", "t"],
                ["git", "add", "-A"],
                ["git", "commit", "-qm", "initial"]):
        subprocess.run(cmd, cwd=str(path), capture_output=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                         capture_output=True, text=True)
    return out.stdout.strip()


def _prov_project(tmp_path):
    (tmp_path / ".spec").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".spec" / "project.json").write_text(json.dumps(
        {"project": "demo",
         "areas": [{"name": "auth", "kind": "area", "code_path": "src/auth"}]}),
        encoding="utf-8")
    (tmp_path / "specs").mkdir(exist_ok=True)
    area_path = tmp_path / "specs" / "auth.area.json"
    area_path.write_text(json.dumps({
        "kind": "area", "area": "auth", "version": "1.0.0",
        "status": "formalized",
        "requirements": [{"id": "REQ-001", "description": "lock after 5",
                          "ears": {"response": "lock the account"}}],
        "traceability": [{"id": "REQ-001", "code": "src/auth/login.js:2"}],
    }), encoding="utf-8")
    return area_path


def test_stamp_reads_the_shas_itself_rather_than_being_told_them(tmp_path):
    """Mechanism over trust applies to provenance too. An agent that could
    type a sha could type the wrong one, for the same reason it must never
    type a verdict."""
    sha = _git_repo(tmp_path, {"src/auth/login.js": "// x\n"})
    area_path = _prov_project(tmp_path)
    with pytest.raises(SystemExit) as exc:
        record.cmd_stamp(_StampArgs(tmp_path, "auth", extracted=True,
                                    code_path="src/auth"))
    assert exc.value.code == 0
    block = json.loads(area_path.read_text(encoding="utf-8"))["extracted_from"]
    assert block["code_sha"] == sha
    assert block["code_path"] == "src/auth"
    assert block["date"]


def test_stamp_generated_pins_the_claims_not_just_the_commit(tmp_path):
    """spec_sha moves on every commit to the spec repo, including ones that
    changed nothing this area claims. spec_content_sha moves only when the
    claims move, which is why it is the one the staleness check compares."""
    _git_repo(tmp_path, {"src/auth/login.js": "// x\n"})
    area_path = _prov_project(tmp_path)
    with pytest.raises(SystemExit):
        record.cmd_stamp(_StampArgs(tmp_path, "auth", generated=True))
    area = json.loads(area_path.read_text(encoding="utf-8"))
    block = area["generated_from"]
    assert block["spec_content_sha"] == itf.compute_spec_sha(area)
    assert block["code_sha"]


def test_stamp_refuses_when_there_is_no_code_version_to_record(tmp_path):
    """A provenance block with no sha in it reads as 'recorded' while
    answering nothing — worse than its absence, which reads as unknown."""
    (tmp_path / "src" / "auth").mkdir(parents=True)
    _prov_project(tmp_path)
    with pytest.raises(SystemExit) as exc:
        record.cmd_stamp(_StampArgs(tmp_path, "auth", extracted=True))
    assert exc.value.code == 2


def test_changed_says_what_moved_and_narrows_to_what_the_spec_claims(tmp_path):
    _git_repo(tmp_path, {"src/auth/login.js": "if (n >= 5) {}\n",
                         "src/other.js": "x\n"})
    area_path = _prov_project(tmp_path)
    with pytest.raises(SystemExit):
        record.cmd_stamp(_StampArgs(tmp_path, "auth", extracted=True,
                                    code_path="src/auth"))

    # Nothing has moved yet.
    with pytest.raises(SystemExit) as exc:
        record.cmd_changed(_StampArgs(tmp_path, "auth"))
    assert exc.value.code == 0

    # The threshold changes, and an untraced file is touched alongside it.
    (tmp_path / "src" / "auth" / "login.js").write_text("if (n >= 3) {}\n",
                                                        encoding="utf-8")
    (tmp_path / "src" / "other.js").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-qm", "lower"], cwd=str(tmp_path),
                   capture_output=True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit) as exc:
            record.cmd_changed(_StampArgs(tmp_path, "auth"))
    assert exc.value.code == 1
    out = buf.getvalue()
    assert "src/auth/login.js" in out
    # The traced lens is what a requirement claims to describe; the untraced
    # file changed too and must not be reported under it.
    traced_section = out.split("Touching a traced file")[1]
    assert "src/auth/login.js" in traced_section
    assert "src/other.js" not in traced_section
    assert json.loads(area_path.read_text(encoding="utf-8"))["extracted_from"]


def test_changed_names_which_baseline_it_fell_back_to(tmp_path):
    """extracted_from, then generated_from, then the verification log. Each
    is a weaker answer to 'what changed since the spec was read' than the one
    before it, so the fallback is stated rather than applied silently."""
    first = _git_repo(tmp_path, {"src/auth/login.js": "// x\n"})
    area_path = _prov_project(tmp_path)
    (tmp_path / "src" / "auth" / "login.js").write_text("// y\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=str(tmp_path),
                   capture_output=True)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    area["verification_log"] = [{"date": "2026-01-01T00:00:00+00:00",
                                 "status": "pass", "code_sha": first}]
    area_path.write_text(json.dumps(area), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit):
            record.cmd_changed(_StampArgs(tmp_path, "auth"))
    assert "verification_log" in buf.getvalue()


def test_changed_refuses_a_baseline_git_cannot_resolve(tmp_path):
    """A rebase or a squash can delete the commit a stamp points at. Diffing
    from nothing would report the whole tree as changed, which reads as
    catastrophic drift — so this is a setup error, not a result."""
    _git_repo(tmp_path, {"src/auth/login.js": "// x\n"})
    area_path = _prov_project(tmp_path)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    area["extracted_from"] = {"code_sha": "0" * 40}
    area_path.write_text(json.dumps(area), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        record.cmd_changed(_StampArgs(tmp_path, "auth"))
    assert exc.value.code == 2


def test_changed_with_no_baseline_says_so_instead_of_inventing_one(tmp_path):
    _git_repo(tmp_path, {"src/auth/login.js": "// x\n"})
    _prov_project(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit) as exc:
            record.cmd_changed(_StampArgs(tmp_path, "auth"))
    assert exc.value.code == 2
    out = buf.getvalue()
    assert "No baseline recorded" in out
    assert "stamp auth --extracted" in out


def test_generated_from_goes_stale_when_a_claim_moves_not_when_a_trace_does():
    """The whole reason the block stores a CONTENT hash. Recording a witness
    trace changes the area file; it does not change what the code was built
    to, and a check that fired on it would be noise nobody reads."""
    area = {"area": "auth", "status": "approved",
            "requirements": [{"id": "REQ-001",
                              "ears": {"response": "lock the account"}}]}
    area["generated_from"] = {"code_sha": "c" * 40,
                              "spec_content_sha": itf.compute_spec_sha(area)}

    findings = []
    lint.check_provenance(area, "auth", findings)
    assert findings == []

    # Bookkeeping churn: still quiet.
    area["check_results"] = {"ran_at": "2026-09-11T00:00:00+00:00", "checks": []}
    area["requirements"][0]["witness"] = {"status": "witnessed",
                                          "trace": "auth/traces/REQ-001.itf.json"}
    findings = []
    lint.check_provenance(area, "auth", findings)
    assert findings == [], "bookkeeping must not invalidate generated_from"

    # A claim moves: now it fires, and only as a WARN.
    area["requirements"][0]["ears"]["response"] = "something else"
    findings = []
    lint.check_provenance(area, "auth", findings)
    assert [f.check for f in findings] == ["generated-from-stale"]
    assert findings[0].severity == lint.WARN


def test_an_unstamped_area_is_not_reported_as_fresh():
    """Absent means unknown. Silence here would let an area with no
    provenance read exactly like one whose provenance is current."""
    findings = []
    lint.check_provenance({"area": "auth"}, "auth", findings)
    assert findings == []          # nothing claimed, so nothing to contradict
    lines = readback.reference_section(Path("."), {"area": "auth"}, {})
    assert not any("Provenance" in ln for ln in lines)


def test_the_readback_shows_provenance_and_flags_moved_claims():
    area = json.loads((TOOLS.parent / "examples" / "specs" / "auth.area.json")
                      .read_text(encoding="utf-8"))
    area["extracted_from"] = {"date": "2026-09-01T10:00:00+00:00",
                              "code_sha": "a" * 40, "code_path": "src/auth"}
    area["generated_from"] = {"date": "2026-09-02T10:00:00+00:00",
                              "spec_sha": "b" * 40, "code_sha": "c" * 40,
                              "spec_content_sha": itf.compute_spec_sha(area)}
    out = "\n".join(readback.reference_section(Path("."), area, {}))
    assert "Spec read from code @ `aaaaaaa`" in out
    assert "subtree `src/auth`" in out
    assert "Code generated from spec @ `bbbbbbb`" in out
    assert "have moved since" not in out

    area["requirements"][0]["ears"]["response"] = "something else entirely"
    out = "\n".join(readback.reference_section(Path("."), area, {}))
    assert "have moved since" in out
    assert "predates the current requirements" in out


# ── Readback legibility: lists that stop being lists ────────────────────────
# A comma-joined list stops being a list the moment one of its items contains
# a comma. "Protection Group lifecycle: create, update, soft delete" is ONE
# scope item that reads as three, and nothing in the rendered text tells the
# reader where one ends. Length is the lesser problem; ambiguity is the bug.


def test_an_item_containing_a_comma_forces_bullets_at_any_length():
    out = readback.render_list("Emits", ["GroupCreated", "GroupUpdated, GroupDeleted"])
    assert out[0] == "**Emits:**"
    assert "- GroupCreated" in out
    assert "- GroupUpdated, GroupDeleted" in out
    # Short, comma-free, few: still inline, because bullets for two words
    # would be noise.
    assert readback.render_list("Emits", ["GroupCreated", "GroupDeleted"])[0] == (
        "**Emits:** GroupCreated, GroupDeleted")


def test_many_or_long_items_go_to_bullets_too():
    assert readback.render_list("X", ["a", "b", "c", "d"])[0] == "**X:**"
    long_pair = ["x" * 50, "y" * 50]
    assert readback.render_list("X", long_pair)[0] == "**X:**"


def test_render_list_is_deterministic_and_drops_empties():
    items = ["alpha", "beta, gamma", "", "   "]
    once = readback.render_list("Scope", items)
    assert once == readback.render_list("Scope", items), "same input, same output"
    assert not any(ln.strip() == "-" for ln in once)
    assert readback.render_list("Scope", []) == []
    assert readback.render_list("Scope", None) == []


def test_a_real_scope_list_renders_as_one_bullet_per_item():
    """The reported case: seven scope items, most containing commas, joined
    into a single 600-character paragraph."""
    scope = [
        "Protection Group lifecycle: create, update, soft delete, protection "
        "enable/disable, VM ordering",
        "Service Level Policy lifecycle: create, update, delete, RPO and "
        "retention validation",
        "RPO-driven scheduling of protection runs and the concurrency guard",
    ]
    out = readback.scope_section({"scope": {"included": scope}})
    body = [ln for ln in out if ln.startswith("- ")]
    assert len(body) == 3, out
    assert body[0].endswith("VM ordering")
    assert max(len(ln) for ln in out) < 130


def test_the_ship_verdict_stays_one_line_when_the_scope_is_long():
    """Go/no-go is meant to be a glance. Inlining a long scope buries it."""
    # READY is the only branch that prints the scope clause, so the area has
    # to actually be ready.
    area = {"area": "x", "open_questions": [],
            "requirements": [{"id": "REQ-001", "status": "verified"}],
            "invariants": [{"id": "INV-001", "formal_status": "verified"}],
            "scope": {"included": ["a very long scope item " * 4,
                                   "another long one " * 4]}}
    verdict = readback.ship_verdict(area)
    assert len(verdict) < 200, verdict
    assert "see Scope" in verdict

    short = dict(area, scope={"included": ["Login", "Logout"]})
    assert "Login, Logout" in readback.ship_verdict(short)


def test_entities_render_one_per_line():
    """Each entity carries a prose description; joined with a middle dot they
    run together into a paragraph with an invisible separator."""
    area = {"area": "x", "concepts": {"entities": [
        {"name": "Subscription", "states": ["Active", "Expired"],
         "description": "CLOSED: exactly these two states exist, and nothing else."},
        {"name": "Invoice", "description": "Issued per billing period."}]}}
    out = "\n".join(readback.reference_section(Path("."), area, {}))
    assert "**Entities:**\n\n- **Subscription** (Active / Expired) —" in out
    assert "\n- **Invoice** — Issued per billing period." in out
    assert " · " not in out


# ── Hollow witnesses: a proof that proves nothing ───────────────────────────
# Two requirements came back WITNESSED in under 8 seconds while demonstrating
# nothing: their predicates were already true in the initial state. Nothing
# caught it. A witness probe asserts `not(predicate)` and the checker returns
# the SHORTEST counterexample, so a one-state trace means the predicate held
# before anything ran — structural, free, and checkable without evaluating
# Quint.

def test_a_one_state_trace_is_hollow_and_a_two_state_one_is_not():
    assert itf.is_hollow({"states": [{"x": 1}]})
    assert itf.is_hollow({"states": []})
    assert itf.is_hollow({})
    assert not itf.is_hollow({"states": [{"x": 0}, {"x": 1}]})


def _hollow_area(tmp_path, states_by_req):
    (tmp_path / "specs" / "a" / "traces").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "a.qnt").write_text("module a { var x: int }",
                                              encoding="utf-8")
    reqs = []
    for rid, states in states_by_req.items():
        rel = f"a/traces/{rid}.itf.json"
        (tmp_path / "specs" / rel).write_text(
            json.dumps({"vars": ["x"], "states": states}), encoding="utf-8")
        reqs.append({"id": rid, "status": "specified", "quint_ref": "f",
                     "witness": {"predicate": "x > 0", "status": "witnessed",
                                 "trace": rel}})
    area = {"kind": "area", "area": "a", "version": "1.0.0",
            "status": "formalized", "formal_model": {"quint_file": "a.qnt"},
            "requirements": reqs}
    sha = itf.compute_model_sha(tmp_path, "a", area)
    for r in area["requirements"]:
        r["witness"]["model_sha"] = sha
    return area


def test_a_hollow_witness_does_not_discharge_even_when_stamped(tmp_path):
    """Retroactive on purpose. The two that shipped are already sitting in
    an area JSON marked witnessed with a fresh model_sha — a gate that only
    fired at mint time would never look at them again."""
    area = _hollow_area(tmp_path, {"REQ-008": [{"x": 1}],
                                   "REQ-009": [{"x": 0}, {"x": 1}]})
    rows, missing, discharged = itf.witness_status(tmp_path, "a", area)
    by_id = {rid: (st, detail) for rid, st, _t, detail in rows}
    assert by_id["REQ-008"][0] == "HOLLOW"
    assert "initial state" in by_id["REQ-008"][1]
    assert by_id["REQ-009"][0] == "witnessed"
    assert (missing, discharged) == (1, 1)


def test_lint_fails_a_hollow_trace(tmp_path):
    area = _hollow_area(tmp_path, {"REQ-008": [{"x": 1}]})
    findings = []
    lint.check_witnesses(tmp_path, area, "a", findings)
    codes = [f.check for f in findings]
    assert "witness-hollow" in codes
    assert all(f.severity == lint.FAIL for f in findings if f.check == "witness-hollow")


def test_the_recorder_refuses_to_mint_a_hollow_witness(tmp_path, monkeypatch):
    """The moment it would have been written. A false proof that reaches the
    ledger is indistinguishable from a real one to everything downstream."""
    (tmp_path / ".spec").mkdir()
    (tmp_path / ".spec" / "project.json").write_text('{"project":"p"}',
                                                     encoding="utf-8")
    (tmp_path / "specs" / "a" / "traces").mkdir(parents=True)
    (tmp_path / "specs" / "a.qnt").write_text("module a { var x: int }",
                                              encoding="utf-8")
    (tmp_path / "specs" / "a.probes.qnt").write_text(
        "module a_probes {\n  val witness_REQ_008: bool = true\n}\n",
        encoding="utf-8")
    area_path = tmp_path / "specs" / "a.area.json"
    area_path.write_text(json.dumps({
        "kind": "area", "area": "a", "version": "1.0.0", "status": "formalized",
        "formal_model": {"quint_file": "a.qnt", "probes_file": "a.probes.qnt"},
        "requirements": [{"id": "REQ-008", "status": "specified",
                          "quint_ref": "f",
                          "witness": {"predicate": "x > 0"}}],
    }), encoding="utf-8")

    def fake_verify(quint, target, name, *a, **k):
        # What Apalache writes when the predicate holds at init.
        Path(k["out_itf"]).write_text(
            json.dumps({"vars": ["x"], "states": [{"x": 1}]}), encoding="utf-8")
        return ("counterexample", "", 0.3)

    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_verify", fake_verify)

    args = _Args(tmp_path, "a")
    args.no_witness = False
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(args)
    assert exc.value.code == 1, "a hollow witness must fail the run"
    w = json.loads(area_path.read_text(encoding="utf-8"))["requirements"][0]["witness"]
    assert w["status"] == "hollow"
    assert "model_sha" not in w, "a hollow witness must not be stamped fresh"


# ── The probe generator ─────────────────────────────────────────────────────
# Hand-rolled every time, and it broke twice in one session: once stale
# against the model after an action gained a parameter, once with two actions
# miscounted out of stepP so four probes could never fire. Both are
# mechanical properties of the generated text.

PROBE_QNT = '''module a {
  type UserId = str
  type Status = | Active | Closed
  var accounts: UserId -> Status
  var count: int
  action init = all { accounts' = Map(), count' = 0 }
  action open_acct(uid: UserId): bool = all {
    accounts' = accounts.put(uid, Active), count' = count + 1 }
  action close_acct(uid: UserId): bool = all {
    accounts' = accounts.put(uid, Closed), count' = count }
  action step = { nondet u = oneOf(Set("a")) any { open_acct(u), close_acct(u) } }
}
'''


def _probe_area(tmp_path, reqs=None, domains=None, invariants=None):
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "a.qnt").write_text(PROBE_QNT, encoding="utf-8")
    fm = {"quint_file": "a.qnt", "probes_file": "a.probes.qnt"}
    if domains is not None:
        fm["probe_domains"] = domains
    area = {"kind": "area", "area": "a", "version": "1.0.0",
            "status": "formalized", "formal_model": fm,
            "invariants": invariants or [],
            "requirements": reqs if reqs is not None else [
                {"id": "REQ-001", "description": "opening makes it Active",
                 "quint_ref": "open_acct",
                 "witness": {"predicate": "accounts.get(_lastUid) == Active",
                             "delta": {"pre": "not(_prevAccounts.keys()"
                                              ".contains(_lastUid))"}}}]}
    (tmp_path / "specs" / "a.area.json").write_text(json.dumps(area),
                                                    encoding="utf-8")
    return area


def _gen(tmp_path, area):
    ir = quint_ir.parse_qnt(tmp_path / "specs" / "a.qnt")
    return "\n".join(probes.build(area, "a", ir, "a.qnt"))


def test_every_declared_action_gets_a_branch_in_step_p(tmp_path):
    """The reported miscount: two actions silently left out, so four probes
    could never fire and the run still looked healthy."""
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1", "u2")'})
    out = _gen(tmp_path, area)
    assert '_lastAction\' = "open_acct"' in out
    assert '_lastAction\' = "close_acct"' in out
    assert "open_acct(uid)" in out and "close_acct(uid)" in out
    # init and step are the harness, not actions to mirror.
    assert '_lastAction\' = "step"' not in out


def test_generation_is_deterministic(tmp_path):
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'})
    assert _gen(tmp_path, area) == _gen(tmp_path, area)


def test_a_missing_domain_is_a_setup_error_that_prints_what_to_add(tmp_path):
    """Which values to explore is a scope decision: too small and a probe
    cannot fire, too large and every check pays. Not guessable."""
    area = _probe_area(tmp_path, domains=None)
    with pytest.raises(SystemExit) as exc:
        _gen(tmp_path, area)
    assert exc.value.code == 2


def test_ghosts_cover_the_parameters_and_the_deltas_only(tmp_path):
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'})
    out = _gen(tmp_path, area)
    assert "var _lastUid: UserId" in out
    assert "var _prevAccounts: UserId -> Status" in out
    # `count` is a state var but no delta reads it: snapshotting it would
    # double the state space for a comparison nobody makes.
    assert "_prevCount" not in out
    # Explicit initial values — before init there is no prior state to read.
    assert '_lastUid\' = ""' in out
    assert "_prevAccounts' = Map()" in out


def test_the_probe_carries_predicate_path_and_delta(tmp_path):
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'})
    out = _gen(tmp_path, area)
    assert "val witness_REQ_001: bool =" in out
    assert "accounts.get(_lastUid) == Active" in out
    assert '_lastAction == "open_acct"' in out
    assert "_prevAccounts.keys()" in out


def test_a_requirement_owing_no_probe_is_named_not_silently_absent(tmp_path):
    """An unexplained gap in the generated file is indistinguishable from
    the omission bug this generator exists to prevent."""
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'}, reqs=[
        {"id": "REQ-002", "description": "never two open", "modality": "forbidden",
         "quint_ref": "open_acct", "witness": {"enforced_by": "INV-001"}},
        {"id": "REQ-003", "description": "fast", "type": "non-functional"},
    ])
    out = _gen(tmp_path, area)
    assert "REQ-002: forbidden" in out and "INV-001" in out
    assert "REQ-003: non-functional" in out
    assert "witness_REQ_002" not in out


def test_check_mode_detects_a_stale_module(tmp_path, monkeypatch, capsys):
    """The other reported failure: an action gains a parameter and the
    hand-written stepP still calls it with the old arity."""
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'})
    (tmp_path / "specs" / "a.probes.qnt").write_text(
        "module a_probes { /* written by hand, long ago */ }", encoding="utf-8")

    class Args:
        pass
    monkeypatch.setattr(sys, "argv",
                        ["spec-probes.py", "a", "--root", str(tmp_path), "--check"])
    assert probes.main() == 1
    assert "STALE" in capsys.readouterr().out

    # Generate it, and --check goes quiet.
    monkeypatch.setattr(sys, "argv",
                        ["spec-probes.py", "a", "--root", str(tmp_path)])
    assert probes.main() == 0
    monkeypatch.setattr(sys, "argv",
                        ["spec-probes.py", "a", "--root", str(tmp_path), "--check"])
    assert probes.main() == 0
    assert area is not None


# ── Invariants over the probe module ────────────────────────────────────────
# The invariant loop ran the main module with its own init/step, so no
# invariant could reference the `_prev` ghosts — which meant no transition
# property was provable, and a whole class of requirements fell back to prose.

def test_a_probe_module_invariant_runs_against_the_probe_module(tmp_path,
                                                                monkeypatch):
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'}, reqs=[],
                       invariants=[
        {"id": "INV-001", "description": "state", "quint_name": "alwaysP"},
        {"id": "INV-003", "description": "policy never changes",
         "over": "probes", "quint_name": "policyStable"}])
    (tmp_path / "specs" / "a.probes.qnt").write_text(
        "module a_probes {\n  val policyStable: bool = true\n}\n",
        encoding="utf-8")
    (tmp_path / ".spec").mkdir(exist_ok=True)
    (tmp_path / ".spec" / "project.json").write_text('{"project":"p"}',
                                                     encoding="utf-8")
    (tmp_path / "specs" / "a.area.json").write_text(json.dumps(area),
                                                    encoding="utf-8")
    seen = []

    def fake_verify(quint, target, name, *a, **k):
        seen.append((name, Path(target).name, k.get("init"), k.get("step")))
        return ("verified", "", 0.1)

    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_verify", fake_verify)
    monkeypatch.setattr(record, "run_verify_batch",
                        lambda *a, **k: pytest.fail("probe invariant was batched"))
    with pytest.raises(SystemExit):
        record.cmd_check(_Args(tmp_path, "a"))

    assert ("alwaysP", "a.qnt", None, None) in seen
    assert ("policyStable", "a.probes.qnt", "initP", "stepP") in seen
    written = json.loads(
        (tmp_path / "specs" / "a.area.json").read_text(encoding="utf-8"))
    entry = {c["id"]: c for c in written["check_results"]["checks"]}
    assert entry["INV-003"]["over"] == "probes"
    assert "over" not in entry["INV-001"]


def test_over_probes_without_a_probe_module_is_an_error_not_a_pass(tmp_path,
                                                                   monkeypatch):
    area = _probe_area(tmp_path, domains={"UserId": 'Set("u1")'}, reqs=[],
                       invariants=[{"id": "INV-003", "description": "x",
                                    "over": "probes", "quint_name": "p"}])
    area["formal_model"].pop("probes_file")
    (tmp_path / ".spec").mkdir(exist_ok=True)
    (tmp_path / ".spec" / "project.json").write_text('{"project":"p"}',
                                                     encoding="utf-8")
    (tmp_path / "specs" / "a.area.json").write_text(json.dumps(area),
                                                    encoding="utf-8")
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_verify",
                        lambda *a, **k: pytest.fail("ran without a probe module"))
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "a"))
    assert exc.value.code == 1
    written = json.loads(
        (tmp_path / "specs" / "a.area.json").read_text(encoding="utf-8"))
    assert written["check_results"]["checks"][0]["result"] == "error"


def test_lint_looks_up_a_probe_invariant_in_the_probe_module(tmp_path):
    """Checking it against the model sidecar would FAIL every correct
    transition property."""
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "a.qnt").write_text(PROBE_QNT, encoding="utf-8")
    (tmp_path / "specs" / "a.probes.qnt").write_text(
        "module a_probes {\n  val policyStable: bool = true\n}\n",
        encoding="utf-8")
    model = lint.parse_sidecar(tmp_path / "specs" / "a.qnt")
    probe_ir = lint.parse_sidecar(tmp_path / "specs" / "a.probes.qnt")
    area = {"area": "a", "invariants": [
        {"id": "INV-003", "over": "probes", "quint_name": "policyStable"}]}

    findings = []
    lint.check_quint_refs(area, model, "a", findings, probes=probe_ir)
    assert findings == [], [f.check for f in findings]

    findings = []
    lint.check_quint_refs(area, model, "a", findings, probes=None)
    assert [f.check for f in findings] == ["invariant-over-probes-unparseable"]


# ── Extraction coverage: absent must not read as clean ──────────────────────
# An area reached 24 witnessed requirements, 18 verified invariants, zero
# untriaged matrix cells and zero lint failures with 79% of its code
# unaccounted for. Every mechanism already existed — --record, the
# check_results.extraction field, the readback's three readers, the ledger
# lints — and the documented flow never invoked --record, so the field stayed
# absent and every surface read absent as "nothing to say".

_EX_BASE = {
    "kind": "area", "area": "pg", "version": "1.0.0", "status": "in-review",
    "requirements": [{"id": "REQ-001", "status": "verified",
                      "extraction": {"evidence": "pg.ts:12-40"}}],
    "invariants": [{"id": "INV-001", "formal_status": "verified"}],
    "traceability": [{"id": "REQ-001", "code": "src/pg.ts"}],
    "open_questions": [],
}


def _ex_area(**overlay):
    area = dict(_EX_BASE)
    area.update(overlay)
    return area


def _ex_stats(**kw):
    base = {"sites": 341, "mapped": 41, "triaged": 30, "unclaimed": 270}
    base.update(kw)
    return {"check_results": {"extraction": base}}


def test_an_area_with_code_and_no_audit_is_not_clean():
    """The actual defect: 'unknown' and 'fine' were indistinguishable."""
    area = _ex_area()
    assert readback.extraction_state(area)[0] == "unaudited"
    findings = []
    lint.check_extraction_coverage(area, "pg", findings)
    assert [f.check for f in findings] == ["extraction-audit-missing"]


def test_a_triaged_area_that_never_recorded_is_reported_separately():
    """The audit was run — someone triaged from its output — but never with
    --record, so no coverage number was ever written down."""
    area = _ex_area(extraction_triage=[{"file": "src/pg.ts",
                                        "fingerprint": "aa11bb22",
                                        "verdict": "MAPPED",
                                        "maps_to": "REQ-001"}])
    findings = []
    lint.check_extraction_coverage(area, "pg", findings)
    assert [f.check for f in findings] == ["extraction-audit-never-recorded"]


def test_unclaimed_sites_warn_while_authoring_and_fail_at_review():
    """Graded like every other precision lint: 'approved' has to mean
    accounted-for, not accounted-for-ish."""
    for status, severity in (("structured", lint.WARN), ("in-review", lint.FAIL),
                             ("approved", lint.FAIL)):
        area = _ex_area(status=status, **_ex_stats())
        findings = []
        lint.check_extraction_coverage(area, "pg", findings)
        assert [f.check for f in findings] == ["extraction-sites-unclaimed"]
        assert findings[0].severity == severity, status


def test_an_area_with_no_code_is_not_nagged():
    """A greenfield area has nothing to audit. Warning about it would train
    people to ignore the warning."""
    area = {"kind": "area", "area": "g", "status": "in-review",
            "requirements": [{"id": "REQ-001", "status": "verified"}],
            "invariants": [], "traceability": []}
    findings = []
    lint.check_extraction_coverage(area, "g", findings)
    assert findings == []
    assert readback.extraction_state(area) == ("n/a", "n/a (no code)")


def test_a_clean_audit_is_clean():
    area = _ex_area(**_ex_stats(mapped=300, triaged=41, unclaimed=0))
    findings = []
    lint.check_extraction_coverage(area, "pg", findings)
    assert findings == []
    assert readback.extraction_state(area)[0] == "clean"


def test_the_ship_verdict_blocks_on_unaccounted_code():
    """It sits alongside unverified requirements and open questions, because
    it answers the same question: is this area finished?"""
    v = readback.ship_verdict(_ex_area(**_ex_stats()))
    assert "NOT READY" in v and "270 of 341 code site(s) unaccounted" in v

    v = readback.ship_verdict(_ex_area())
    assert "NOT READY" in v and "never audited" in v

    v = readback.ship_verdict(_ex_area(**_ex_stats(mapped=300, triaged=41,
                                                   unclaimed=0)))
    assert "READY" in v and "NOT READY" not in v


def test_the_header_bar_carries_extraction_next_to_coverage():
    bar = readback.header_bar(_ex_area(**_ex_stats()), "pg")
    assert "**Extraction:** 71/341 sites accounted, 270 unclaimed" in bar
    assert "**Coverage:**" in bar
    assert "**Extraction:** n/a (no code)" in readback.header_bar(
        {"area": "g", "requirements": [], "invariants": []}, "g")


def test_the_dimension_grid_tells_not_measured_from_nothing_to_measure():
    """`—` means 'nothing declared to measure' everywhere else in that grid,
    so an unaudited area with code must be `!`, not `—`."""
    def cell(area):
        row = [ln for ln in readback.dimensions_section(area)
               if ln.startswith("| Extraction coverage")][0]
        return row.split("|")[2].strip()

    assert cell(_ex_area()) == "!"                       # code, never audited
    assert cell(_ex_area(**_ex_stats())) == "!"          # sites unaccounted
    assert cell(_ex_area(**_ex_stats(unclaimed=0))) == "✓"
    assert cell({"area": "g", "requirements": [], "invariants": [],
                 "traceability": []}) == "—"             # nothing to audit


def test_the_attention_list_names_a_missing_audit():
    items = "\n".join(readback.attention_items(Path("."), "pg", _ex_area()))
    assert "Code never audited" in items
    assert "only one that measures it against the implementation" in items


def test_lint_and_the_readback_agree_about_what_counts_as_code():
    """Two definitions would let lint stay quiet while the verdict blocks,
    or the reverse."""
    for overlay in ({"traceability": [{"id": "R", "code": "a.ts"}]},
                    {"extraction_triage": [{"file": "a.ts",
                                            "fingerprint": "aa11bb22"}]},
                    {"requirements": [{"id": "R",
                                       "extraction": {"evidence": "a.ts:1"}}]},
                    {}):
        area = {"kind": "area", "area": "x", "status": "in-review",
                "requirements": [], "invariants": [], "traceability": []}
        area.update(overlay)
        findings = []
        lint.check_extraction_coverage(area, "x", findings)
        assert bool(findings) == readback.area_has_code(area), overlay


def test_the_documented_flow_records_the_number():
    """The root cause was documentation, not code: every audit invocation in
    the flow said --emit and none said --record."""
    spec_md = (TOOLS.parent / ".claude" / "commands" / "spec.md").read_text(
        encoding="utf-8")
    invocations = [ln for ln in spec_md.splitlines()
                   if "spec-extract-audit.py" in ln and "`tools/" in ln]
    assert invocations, "no audit invocation found — retarget this test"
    for line in invocations:
        assert "--record" in line, f"audit invoked without --record: {line[:120]}"

    check_md = (TOOLS.parent / ".claude" / "commands" / "spec-check.md").read_text(
        encoding="utf-8")
    assert "spec-extract-audit.py" in check_md, (
        "/spec-check must run the audit: running it once at extraction leaves "
        "the number resting on whoever last remembered the flag")
    assert "--record" in check_md.split("spec-extract-audit.py")[1][:200]
