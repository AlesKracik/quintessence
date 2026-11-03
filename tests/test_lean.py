"""The optional Lean backend (`proof: "lean"`, tools/lean_backend.py).

Everything here is pure Python except the last test, which builds the
example's Lake project and is skipped where no `lake` can be found. The
pieces a Lean toolchain would run are faked at the seam the backend itself
uses (build / run_file), so what is tested is the bookkeeping that decides
a verdict: the statement, the axiom audit, the trace replay and its
attribution, and the gates spec-lint applies without Lean.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
REPO = TOOLS.parent
EXAMPLES = REPO / "examples"
sys.path.insert(0, str(TOOLS))
import lean_backend as lb  # noqa: E402
import spec_source  # noqa: E402

from test_tooling import _Args, _quint_project, get_area  # noqa: E402


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


record = _load("spec_record_lean", "spec-record.py")
lint = _load("spec_lint_lean", "spec-lint.py")
readback = _load("spec_readback_lean", "spec-readback.py")

V = lambda tag: {"tag": tag, "value": {"#tup": []}}  # noqa: E731


# ── Names and the statement ──────────────────────────────────────────────────

def test_the_module_is_the_theorems_namespace():
    assert lb.module_of("Spec.Subscription.inv_holds") == "Spec.Subscription"
    assert lb.module_of("inv_holds") is None, "a bare name says nowhere"
    assert lb.module_of("Spec.bad name") is None


def test_the_statement_is_written_by_the_tool():
    """The author proves; the tool says what. A proof of anything weaker
    fails the audit's defeq check instead of being recorded."""
    assert lb.statement("Spec.S", "inv") == (
        "∀ s, Quint.Reachable Spec.S.init Spec.S.apply s → Spec.S.inv s = true")
    src = lb.audit_source("Spec.S", "inv", "Spec.S.inv_holds")
    assert "theorem QuintAudit.target :\n    ∀ s, Quint.Reachable" in src
    assert ":=\n  Spec.S.inv_holds\n" in src
    assert "collectAxioms ``QuintAudit.target" in src


# ── Forbidden constructs ─────────────────────────────────────────────────────

def test_forbidden_constructs_are_found_outside_comments_and_strings():
    text = '''
-- sorry, in a line comment, is fine
/- native_decide, in a block /- nested -/ comment -/
def msg := "sorry is just a word here"
theorem t : True := by
  sorry
'''
    assert lb.forbidden_in(text) == ["sorry"]
    assert lb.forbidden_in("axiom cheat : False\n") == ["axiom"]
    assert lb.forbidden_in("theorem t : 1 = 1 := by native_decide") == ["native_decide"]
    assert lb.forbidden_in("@[implemented_by f] def g := 1") == ["implemented_by"]
    assert lb.forbidden_in("theorem axiomatic : True := trivial") == []


# ── The audit report ─────────────────────────────────────────────────────────

def test_the_audit_line_is_found_among_lean_output():
    out = ('info: x.lean:40:0: QUINT-AUDIT {"axioms":["propext"],'
           '"statement":"S","closure":[{"name":"a","body":"def 1"}]}\n')
    assert lb.parse_audit(out)["axioms"] == ["propext"]
    assert lb.parse_audit("error: nothing") is None


def test_statement_sha_is_meaning_not_order():
    a = {"statement": "S", "closure": [{"name": "a", "body": "1"}, {"name": "b", "body": "2"}]}
    b = {"statement": "S", "closure": [{"name": "b", "body": "2"}, {"name": "a", "body": "1"}]}
    c = {"statement": "S", "closure": [{"name": "a", "body": "1"}, {"name": "b", "body": "3"}]}
    sha = lb.statement_sha(a, "v1")
    assert sha.startswith("lean:")
    assert sha == lb.statement_sha(b, "v1"), "the closure is a set"
    assert sha != lb.statement_sha(c, "v1"), "a changed definition moves it"
    assert sha != lb.statement_sha(a, "v2"), "so does the toolchain that elaborated it"


# ── ITF -> Lean ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,lean", [
    (True, "true"),
    ({"#bigint": "-3"}, "(-3)"),
    (7, "7"),
    ('say "hi"\n', '"say \\"hi\\"\\n"'),
    (V("Active"), ".Active"),
    ({"tag": "Locked", "value": {"#bigint": "2"}}, "(.Locked 2)"),
    ({"tag": "Pair", "value": {"#tup": ["a", 1]}}, '(.Pair "a" 1)'),
    ({"#set": ["b", "a"]}, '(Quint.QSet.ofList ["b", "a"])'),
    ({"#map": [["k", V("Email")]]}, '(Quint.QMap.ofList [("k", .Email)])'),
    ({"#tup": []}, "()"),
    ({"id": "x", "n": 1}, '{ id := "x", n := 1 }'),
])
def test_an_itf_value_becomes_a_lean_term(value, lean):
    assert lb.lean_literal(value) == lean


def test_a_value_with_no_lean_form_is_refused():
    with pytest.raises(lb.Unencodable):
        lb.lean_literal({"#unserializable": "x"})


def test_a_step_is_its_action_and_its_parameter_ghosts():
    st = {"_lastAction": "cancel", "_lastC": "alice", "_lastN": V("Email")}
    assert lb.lean_action(st, {"cancel": ["c", "n"]}) == '((.cancel "alice" .Email), false)'
    assert lb.lean_action(dict(st, _refused=True), {"cancel": ["c", "n"]}).endswith(", true)")
    with pytest.raises(lb.Unencodable):
        lb.lean_action(st, {"other": []})
    with pytest.raises(lb.Unencodable):
        lb.lean_action({"_lastAction": "cancel"}, {"cancel": ["c"]})


def test_a_state_holds_only_the_models_variables():
    st = {"x": 1, "_prevX": 0, "_lastAction": "a"}
    assert lb.lean_state(st, ["x"]) == "{ x := 1 }"
    with pytest.raises(lb.Unencodable):
        lb.lean_state(st, ["x", "y"])
    with pytest.raises(lb.Unencodable):
        lb.lean_state({"auth::s": 1}, ["auth::s"])


# ── Which traces, and what each one must show ────────────────────────────────

def _trace(path, states, vars_=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"vars": vars_ or sorted(states[0]), "states": states}),
                    encoding="utf-8")


def test_runs_and_the_invariants_own_traces_are_told_apart(tmp_path):
    t = tmp_path / "specs" / "a" / "traces"
    _trace(t / "REQ-001.itf.json", [{"x": 0, "_lastAction": "init"}])
    _trace(t / "runs" / "ex.itf.json", [{"x": 0, "_lastAction": "init"}])
    _trace(t / "INV-001.cti.itf.json", [{"x": 5}, {"x": 6}])
    _trace(t / "INV-009.cex.itf.json", [{"x": 5}])  # another invariant's: neither
    _trace(t / "INV-008.cti.itf.json", [{"x": 7, "_lastAction": "inc"}])  # unreachable start
    runs, preds = lb.collect_traces("a", tmp_path, [{"id": "INV-001", "quint_name": "inv"}])
    assert [r for r, _ in runs] == ["a/traces/REQ-001.itf.json", "a/traces/runs/ex.itf.json"]
    assert [(r, i["id"]) for r, i, _ in preds] == [("a/traces/INV-001.cti.itf.json", "INV-001")]


def _replay_fixture():
    runs = [("a/traces/R1.itf.json", [{"x": 0, "_lastAction": "init"},
                                       {"x": 1, "_lastAction": "inc"}]),
            ("a/traces/R2.itf.json", [{"x": 0, "_lastAction": "init"},
                                       {"x": 0, "_lastAction": "inc", "_refused": True}]),
            ("a/traces/R3.itf.json", [{"x": 0, "_lastAction": "init"},
                                       {"x": 9, "_lastAction": "inc"}])]
    preds = [("a/traces/INV-001.cti.itf.json", {"id": "INV-001", "quint_name": "inv"},
              [{"x": 3}, {"x": 4}])]
    src, spans, skipped = lb.replay_source("M", ["x"], {"inc": []}, runs, preds)
    return runs, preds, src, spans, skipped


def test_the_replay_file_asks_one_question_per_trace():
    _runs, _preds, src, spans, skipped = _replay_fixture()
    assert src.startswith("import M\n")
    assert "List (M.Action × Bool)" in src
    assert "Quint.replay M.init M.apply QuintReplay.r0_states QuintReplay.r0_acts" in src
    assert "{M.inv QuintReplay.p0_first} {M.inv QuintReplay.p0_last}" in src
    assert set(spans) == {"r0", "r1", "r2", "p0"} and not skipped


def test_each_answer_lands_on_its_trace():
    runs, preds, src, spans, _ = _replay_fixture()
    err_line = spans["r2"][0] + 1
    out = "\n".join([
        "QUINT-REPLAY r0 ok",
        "QUINT-REPLAY r1 1",
        f"Replay_M.lean:{err_line}:4: error: type mismatch",
        "QUINT-PRED p0 true true",
    ])
    seen = lb.parse_replay(out, spans, runs, preds, "Replay_M.lean")
    assert seen["a/traces/R1.itf.json"] == (True, "")
    assert seen["a/traces/R2.itf.json"][1] == (
        "step 1 (inc) is refused in Quint, and Lean's apply allows it")
    assert seen["a/traces/R3.itf.json"][1].startswith("does not elaborate: type mismatch")
    ok, why = seen["a/traces/INV-001.cti.itf.json"]
    assert not ok and "true on the first state and true on the last" in why


def test_a_trace_lean_never_answered_is_not_a_pass():
    runs, preds, _src, spans, _ = _replay_fixture()
    seen = lb.parse_replay("", spans, runs, preds, "Replay_M.lean")
    assert all(not ok for ok, _ in seen.values())


# ── One invariant, end to end, with Lean faked at its seam ───────────────────

def _lean_area(tmp_path, traces=True):
    pdir = tmp_path / "specs" / "lean"
    (pdir / "Spec").mkdir(parents=True)
    for f in ("lakefile.toml", "Quint.lean"):
        (pdir / f).write_text("", encoding="utf-8")
    (pdir / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n", encoding="utf-8")
    (pdir / "Spec" / "A.lean").write_text("namespace Spec.A\nend Spec.A\n", encoding="utf-8")
    (tmp_path / "specs" / "a.qnt").write_text(
        "module a {\n  var x: int\n  action init = x' = 0\n"
        "  action inc: bool = x' = x + 1\n  action step = inc\n"
        "  val inv: bool = x >= 0\n}\n", encoding="utf-8")
    if traces:
        _trace(tmp_path / "specs" / "a" / "traces" / "REQ-001.itf.json",
               [{"x": 0, "_lastAction": "init"}, {"x": 1, "_lastAction": "inc"}])
    return {"area": "a", "formal_model": {"quint_file": "a.qnt"},
            "invariants": [{"id": "INV-001", "quint_name": "inv", "proof": "lean",
                            "lean_theorem": "Spec.A.inv_holds"}]}


def _fake_lean(monkeypatch, axioms=("propext",), replay="ok", audit_ok=True,
               build_ok=True):
    monkeypatch.setattr(lb, "find_lake", lambda cfg: "lake")
    monkeypatch.setattr(lb, "build", lambda *a: ("ok", "", 0.1) if build_ok
                        else ("error", "Spec/A.lean:3:2: error: unsolved goals", 0.1))

    def run_file(lake, pdir, path, timeout):
        text = Path(path).read_text(encoding="utf-8")
        if "QuintAudit.target" in text:
            report = {"axioms": list(axioms), "statement": "S",
                      "closure": [{"name": "Spec.A.inv", "body": "def ..."}]}
            out = f"QUINT-AUDIT {json.dumps(report)}" if audit_ok else \
                f"{Path(path).name}:5:2: error: type mismatch"
            return subprocess.CompletedProcess([], 0 if audit_ok else 1, out, ""), "", 0.1
        return subprocess.CompletedProcess([], 0, f"QUINT-REPLAY r0 {replay}", ""), "", 0.1

    monkeypatch.setattr(lb, "run_file", run_file)


def _check(tmp_path, area):
    run = lb.AreaRun(area, "a", tmp_path, {}, tmp_path / "gen")
    return run.check(area["invariants"][0])


def test_a_clean_proof_over_a_replayed_model_is_verified_lean(tmp_path, monkeypatch):
    area = _lean_area(tmp_path)
    _fake_lean(monkeypatch)
    entry, status = _check(tmp_path, area)
    assert status == "verified-lean" and entry["result"] == "verified"
    assert entry["backend"] == "lean" and entry["proof"] == "lean"
    assert entry["axioms"] == ["propext"]
    assert entry["statement_sha"].startswith("lean:")
    assert entry["replay"] == {"runs": 1, "predicates": 0, "skipped": 0}
    assert entry["toolchain"] == "leanprover/lean4:v4.22.0"


@pytest.mark.parametrize("fake,why", [
    ({"axioms": ("propext", "sorryAx")}, "sorryAx (sorry)"),
    ({"axioms": ("Lean.ofReduceBool",)}, "native_decide"),
    ({"audit_ok": False}, "does not prove"),
    ({"build_ok": False}, "unsolved goals"),
    ({"replay": "1"}, "drift: the Lean model disagrees with a/traces/REQ-001.itf.json"),
])
def test_anything_short_of_a_kernel_proof_is_not_checkable(tmp_path, monkeypatch, fake, why):
    area = _lean_area(tmp_path)
    _fake_lean(monkeypatch, **fake)
    entry, status = _check(tmp_path, area)
    assert status == "not-checkable", "never a fallback to some other green"
    assert why in entry["note"]


def test_a_proof_no_trace_links_to_quint_is_not_checkable(tmp_path, monkeypatch):
    area = _lean_area(tmp_path, traces=False)
    _fake_lean(monkeypatch)
    entry, status = _check(tmp_path, area)
    assert status == "not-checkable"
    assert "nothing links it to the Quint one" in entry["note"]


def test_a_missing_name_is_an_error_not_a_verdict(tmp_path, monkeypatch):
    area = _lean_area(tmp_path)
    _fake_lean(monkeypatch)
    del area["invariants"][0]["lean_theorem"]
    entry, status = _check(tmp_path, area)
    assert status is None and "@lean" in entry["error"]


# ── spec-record: Lean is the only verdict on a Lean claim ────────────────────

def test_spec_record_routes_a_lean_invariant_to_lean_alone(tmp_path, monkeypatch):
    area_path = _quint_project(tmp_path, invariants=[
        {"id": "INV-001", "description": "a", "quint_name": "invA",
         "proof": "lean", "lean_theorem": "Spec.Auth.invA_holds"},
    ])
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_verify",
                        lambda *a, **k: pytest.fail("Apalache asked about a Lean claim"))
    monkeypatch.setattr(record, "run_verify_batch",
                        lambda *a, **k: pytest.fail("Apalache asked about a Lean claim"))

    class FakeRun:
        def __init__(self, *a, **kw):
            pass

        def check(self, item):
            return ({"id": item["id"], "kind": "invariant", "backend": "lean",
                     "proof": "lean", "result": "verified", "duration_s": 0.1,
                     "statement_sha": "lean:x", "axioms": ["propext"],
                     "replay": {"runs": 1, "predicates": 0, "skipped": 0}},
                    "verified-lean")

    monkeypatch.setattr(record.lean_backend, "AreaRun", FakeRun)
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "auth"))
    assert exc.value.code == 0
    written = get_area(area_path)
    assert written["invariants"][0]["formal_status"] == "verified-lean"
    assert written["invariants"][0]["lean_theorem"] == "Spec.Auth.invA_holds"
    entry = written["check_results"]["checks"][0]
    assert entry["backend"] == "lean" and entry["statement_sha"] == "lean:x"


def test_no_lake_is_a_setup_error(tmp_path, monkeypatch):
    _quint_project(tmp_path, invariants=[
        {"id": "INV-001", "description": "a", "quint_name": "invA",
         "proof": "lean", "lean_theorem": "Spec.Auth.invA_holds"},
    ])
    (tmp_path / "specs" / "lean" / "Spec").mkdir(parents=True)
    (tmp_path / "specs" / "lean" / "Spec" / "Auth.lean").write_text("", encoding="utf-8")
    monkeypatch.setattr(record, "find_quint", lambda: "quint")

    def no_lake(cfg):
        raise lb.LeanSetupError("`lake` not found.")

    monkeypatch.setattr(record.lean_backend, "find_lake", no_lake)
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_Args(tmp_path, "auth"))
    assert exc.value.code == 2


# ── spec-lint, without Lean ──────────────────────────────────────────────────

def _lint_codes(tmp_path, area):
    findings = []
    (tmp_path / ".spec").mkdir(exist_ok=True)
    (tmp_path / ".spec" / "project.json").write_text('{"project": "p"}', encoding="utf-8")
    lint.check_lean_backend(tmp_path, area, "a", findings)
    return sorted(f.check for f in findings)


def test_lint_is_quiet_on_a_sound_lean_invariant(tmp_path):
    area = _lean_area(tmp_path)
    area["invariants"][0]["formal_status"] = "verified-lean"
    area["check_results"] = {"checks": [{"id": "INV-001", "backend": "lean",
                                         "result": "verified", "statement_sha": "lean:x",
                                         "axioms": ["propext"], "replay": {"runs": 2}}]}
    assert _lint_codes(tmp_path, area) == []


def test_lint_gates_what_a_lean_proof_needs(tmp_path):
    area = _lean_area(tmp_path)
    (tmp_path / "specs" / "lean" / "Spec" / "A.lean").write_text(
        "theorem t : False := by sorry\n", encoding="utf-8")
    area["invariants"] += [
        {"id": "INV-002", "quint_name": "q", "proof": "lean"},
        {"id": "INV-003", "proof": "lean", "lean_theorem": "Spec.A.other"},
        {"id": "INV-004", "quint_name": "q", "proof": "lean", "lean_theorem": "Spec.Nope.t"},
        {"id": "INV-005", "quint_name": "q", "proof": "lean", "lean_theorem": "Spec.A.t",
         "formal_status": "verified-lean"},
    ]
    codes = _lint_codes(tmp_path, area)
    assert "lean-forbidden" in codes
    assert "lean-without-theorem" in codes
    assert "lean-without-quint-name" in codes
    assert "lean-module-missing" in codes
    assert "lean-verdict-unaudited" in codes


def test_a_critical_lean_proof_counts_for_approval():
    findings = []
    lint.check_critical_invariants(
        {"status": "approved", "invariants": [
            {"id": "I", "criticality": "critical", "formal_status": "verified-lean"}]},
        "a", findings)
    assert findings == []


# ── readback ─────────────────────────────────────────────────────────────────

def test_a_lean_proof_reads_as_its_own_kind_of_tick():
    assert readback.invariant_mark({"formal_status": "verified-lean"}, 10, None) == \
        "✓ proven (Lean)"


# ── The shipped prelude and the example ──────────────────────────────────────

def test_the_example_lake_project_ships_the_template_prelude():
    for f in ("Quint.lean", "lean-toolchain", "lakefile.toml"):
        assert (EXAMPLES / "specs" / "lean" / f).read_text(encoding="utf-8") == \
            (REPO / "templates" / "lean" / f).read_text(encoding="utf-8"), f


def test_the_example_lean_files_use_nothing_forbidden():
    pdir = EXAMPLES / "specs" / "lean"
    for p in pdir.rglob("*.lean"):
        if ".lake" not in p.parts:
            assert lb.forbidden_in(p.read_text(encoding="utf-8")) == [], p


def _lake():
    cfg = {"project_dir": EXAMPLES / "specs" / "lean", "lake": None}
    try:
        return lb.find_lake(cfg)
    except lb.LeanSetupError:
        return None


@pytest.mark.skipif(_lake() is None, reason="no lake: the Lean toolchain is optional")
def test_the_example_proof_builds_audits_and_replays(tmp_path):
    """The real thing: subscription INV-003, which Quint cannot prove
    inductively as written, proven in Lean over a model all of the area's
    traces replay through — and recorded with the sha it still has."""
    project = json.loads((EXAMPLES / ".spec" / "project.json").read_text(encoding="utf-8"))
    area = spec_source.load_area(EXAMPLES, "subscription")
    inv = next(i for i in area["invariants"] if i["id"] == "INV-003")
    entry, status = lb.AreaRun(area, "subscription", EXAMPLES, project,
                               tmp_path).check(inv)
    assert status == "verified-lean", entry.get("note")
    assert set(entry["axioms"]) <= set(lb.ALLOWED_AXIOMS)
    assert entry["replay"]["runs"] >= 1
    recorded = next(c for c in area["check_results"]["checks"] if c["id"] == "INV-003")
    assert recorded["statement_sha"] == entry["statement_sha"], \
        "the example's recorded Lean verdict is stale: re-run spec-record check"


# ── A model that imports another: its State holds both ───────────────────────

def test_an_imported_modules_variables_are_fields_of_the_state():
    """auth-ui imports auth qualified, and its traces name auth's variables
    unqualified. Taking only auth-ui's own three dropped auth's six, so no
    Lean model that knows auth's state could ever replay them."""
    fields, params = lb.model_shape(EXAMPLES / "specs" / "auth-ui.qnt")
    assert fields == ["accountStatus", "current", "failedAttempts", "form", "idleFor",
                      "mySession", "sessionOwner", "sessions", "userActiveSession"]
    assert params["submit_success"] == ["u", "s"]
    trace = json.loads((EXAMPLES / "specs" / "auth-ui" / "traces" / "UI-002.itf.json")
                       .read_text(encoding="utf-8"))
    for st in trace["states"]:
        lean = lb.lean_state(st, fields)
        assert "sessions := (Quint.QMap.ofList" in lean and "current := ." in lean


def test_two_modules_with_one_variable_name_are_refused(tmp_path):
    (tmp_path / "a.qnt").write_text("module a {\n  var x: int\n  action init = x' = 0\n"
                                    "  action step = x' = x\n}\n", encoding="utf-8")
    (tmp_path / "b.qnt").write_text('module b {\n  import a from "./a"\n  var x: int\n'
                                    "  action init = all { a::init, x' = 0 }\n"
                                    "  action step = all { a::step, x' = x }\n}\n",
                                    encoding="utf-8")
    with pytest.raises(lb.Unencodable, match="declared by both"):
        lb.model_shape(tmp_path / "b.qnt")


def test_a_missing_import_is_a_reason_not_a_crash(tmp_path):
    (tmp_path / "b.qnt").write_text('module b {\n  import a from "./a"\n  var x: int\n'
                                    "  action init = x' = 0\n  action step = x' = x\n}\n",
                                    encoding="utf-8")
    # The Quint CLI refuses to parse it at all; the regex reader parses it
    # and finds no file behind the import. Either way: a reason.
    with pytest.raises(lb.Unencodable, match="not found"):
        lb.state_vars(tmp_path / "b.qnt")
