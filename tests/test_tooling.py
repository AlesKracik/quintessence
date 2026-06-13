"""Unit tests for the spec tooling changes.

Covers the deterministic, quint-free logic touched by the robustness pass:
  - bounded-vs-inductive invariant rendering (spec-readback honesty)
  - the recorded step bound that makes a bounded ✓ honest
  - the tightened drift file-match (spec-record path_match)
  - the witness-predicate FAIL gate past draft (spec-lint vagueness gate)

The tool files use hyphenated names, so they're loaded by path. None of
these tests need quint/Apalache/Java — they exercise pure Python only.

Run:  python -m pytest tests/ -q     (from the repo root)
"""

import importlib.util
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
