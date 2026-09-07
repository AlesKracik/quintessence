"""Unit tests for the spec tooling changes.

Covers the deterministic, quint-free logic touched by the robustness pass:
  - bounded-vs-inductive invariant rendering (spec-readback honesty)
  - the recorded step bound that makes a bounded ✓ honest
  - the tightened drift file-match (spec-record path_match)
  - the witness-predicate FAIL gate past draft (spec-lint vagueness gate)
  - the opt-in Alloy structural backend (scope-honest rendering, the lint
    gate on proof: "structural", and the receipt.json verdict reader)

The tool files use hyphenated names, so they're loaded by path. None of
these tests need quint/Apalache/Java — they exercise pure Python only.

Run:  python -m pytest tests/ -q     (from the repo root)
"""

import importlib.util
import json
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
