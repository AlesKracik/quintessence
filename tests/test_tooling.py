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

The tool files use hyphenated names, so they're loaded by path. None of
these tests need quint/Apalache/Java — they exercise pure Python only.

Run:  python -m pytest tests/ -q     (from the repo root)
"""

import importlib.util
import io
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
matrix = _load("spec_matrix", "spec-matrix.py")
diff = _load("spec_diff", "spec-diff.py")
quint_ir = _load("quint_ir_mod", "quint_ir.py")
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


def test_mutant_application_always_restores(tmp_path):
    target = tmp_path / "code.js"
    original = "if (n >= 5) { return 1; }\n"
    target.write_text(original, encoding="utf-8")
    masked = mutate.mask_noise(original)
    label, start, end, repl = next(iter(mutate.OPERATORS["cmp"](masked)))
    seen = {}

    def fake_run(command, repo_root, timeout):
        seen["during"] = target.read_text(encoding="utf-8")
        return 1

    mutate.run_gate = fake_run
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
    traced = sep.traced_paths(tmp_path, before)
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
