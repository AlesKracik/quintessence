"""Example and journey runs as traces the code replays (tools/spec-runs.py).

Pure Python: the Quint-literal reader, the step labelling and the freshness
check run directly; generation runs against a fake quint that writes the
trace `quint test` would. The real quint runs it in CI over the examples.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
EXAMPLES = TOOLS.parent / "examples"


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load("spec_runs_t", "spec-runs.py")
sys.path.insert(0, str(TOOLS))
import spec_source  # noqa: E402


SHOP = '''module shop {
  type Status = Open | Closed
  var status: str -> Status
  var count: int

  pure val LIMIT: int = 2

  action init = all { status' = Map("a" -> Open), count' = 0 }

  action add(who: str, n: int): bool = all {
    status.get(who) == Open, count + n <= LIMIT,
    count' = count + n, status' = status,
  }
  action close(who: str): bool = all {
    status.get(who) == Open, status' = status.set(who, Closed), count' = count,
  }
  action step = { nondet w = oneOf(Set("a")) any { add(w, 1), close(w) } }

  /// @example EX-001
  /// Closed means closed
  run closedStaysClosed = init
    .then(add("a", LIMIT))
    .then(close("a"))
    .expect(status.get("a") == Closed)
    .then(add("a", 1).fail())

  run journeyRun = init.then(add("a", 1)).then(close("a"))
}
'''

OPEN = {"tag": "Open", "value": {"#tup": []}}
CLOSED = {"tag": "Closed", "value": {"#tup": []}}


def _st(i, status, count):
    return {"#meta": {"index": i}, "status": {"#map": [["a", status]]},
            "count": {"#bigint": str(count)}}


RAW = {"#meta": {"format": "ITF"}, "vars": ["status", "count"],
       "states": [_st(0, OPEN, 0), _st(1, OPEN, 2), _st(2, CLOSED, 2)]}


# ── Literals ────────────────────────────────────────────────────────────────

def test_quint_literals_read_as_itf_values():
    v = {"Open", "Closed", "Some"}
    assert sr.to_itf('"alice"') == "alice"
    assert sr.to_itf("-3") == {"#bigint": "-3"}
    assert sr.to_itf("true") is True
    assert sr.to_itf("Open", v) == OPEN
    assert sr.to_itf("auth::Open", v) == OPEN
    assert sr.to_itf("Some(5)", v) == {"tag": "Some", "value": {"#bigint": "5"}}
    assert sr.to_itf('Set("a", "b")') == {"#set": ["a", "b"]}
    assert sr.to_itf('Map("a" -> 1)') == {"#map": [["a", {"#bigint": "1"}]]}
    assert sr.to_itf('("a", 1)') == {"#tup": ["a", {"#bigint": "1"}]}
    assert sr.to_itf('{ who: "a", n: 2 }') == {"who": "a", "n": {"#bigint": "2"}}
    assert sr.to_itf("[1, 2]") == [{"#bigint": "1"}, {"#bigint": "2"}]
    assert sr.to_itf("LIMIT", v, {"LIMIT": "2"}) == {"#bigint": "2"}
    with pytest.raises(sr.NotLiteral):
        sr.to_itf("count + 1")
    with pytest.raises(sr.NotLiteral):
        sr.to_itf("someHelper")


# ── Labelling ───────────────────────────────────────────────────────────────

def _labelled(raw=RAW, run="closedStaysClosed"):
    decls = spec_source.declarations(SHOP)
    return sr.labelled(
        raw, spec_source.run_calls(decls, run),
        lambda action, q=None: [p for p, _t in decls[action]["params"]],
        {"Open", "Closed"}, {"LIMIT": "2"}, {"run": run, "model_sha": "abc"})


def test_every_step_is_labelled_like_a_witness_trace():
    out = _labelled()
    states = out["states"]
    assert [s["_lastAction"] for s in states] == ["init", "add", "close", "add"]
    assert states[1]["_lastWho"] == "a" and states[1]["_lastN"] == {"#bigint": "2"}
    assert states[2]["_lastWho"] == "a" and states[2]["_lastN"] == {"#bigint": "2"}, (
        "a ghost the step does not set keeps its value")
    assert states[0]["_lastN"] == {"#bigint": "2"}, "before it is set: its first value"
    assert out["vars"] == ["status", "count", "_lastAction", "_refused", "_lastWho", "_lastN"]
    assert all(set(s) - {"#meta"} == set(out["vars"]) for s in states)
    assert out["#meta"]["model_sha"] == "abc" and out["#meta"]["format"] == "ITF"


def test_a_refused_call_is_a_state_where_nothing_moves():
    states = _labelled()["states"]
    refused = states[3]
    assert refused["_refused"] is True and states[2]["_refused"] is False
    assert refused["_lastN"] == {"#bigint": "1"}, "the refused call's own argument"
    for var in ("status", "count"):
        assert refused[var] == states[2][var]


def test_a_trace_that_does_not_fit_the_run_is_an_error():
    short = dict(RAW, states=RAW["states"][:2])
    with pytest.raises(ValueError, match="should have 3"):
        _labelled(short)


# ── Which runs, and whether their traces are current ───────────────────────

def _project(tmp_path):
    specs = tmp_path / "specs"
    (specs / "journeys").mkdir(parents=True)
    (specs / "shop.qnt").write_text(SHOP, encoding="utf-8")
    (specs / "shop.intent.json").write_text(json.dumps(
        {"kind": "area", "area": "shop", "version": "0.1.0", "status": "formalized"}),
        encoding="utf-8")
    (specs / "journeys" / "buy.journey.json").write_text(json.dumps(
        {"name": "Buy", "run": "shop.journeyRun", "steps": []}), encoding="utf-8")
    return tmp_path


def test_examples_and_journey_runs_are_the_runs_to_replay(tmp_path):
    root = _project(tmp_path)
    area = spec_source.load_area(root, "shop")
    assert sr.area_runs(root, "shop", area, spec_source.declarations(SHOP)) == [
        ("closedStaysClosed", "EX-001"), ("journeyRun", "journey: Buy")]
    assert [s for _r, _l, s in sr.status(root, "shop")] == ["missing", "missing"]


class FakeProc:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = code, stdout, stderr


def test_generation_writes_current_traces_and_check_reads_them(tmp_path, monkeypatch):
    root = _project(tmp_path)

    def fake_run(cmd, cwd=None, capture_output=True, text=True, timeout=None):
        if cmd[1:3] == ["test", "--help"]:
            return FakeProc(stdout="  --out-itf  write a trace\n  --max-samples\n")
        out = next(a.split("=", 1)[1] for a in cmd if a.startswith("--out-itf="))
        run = cmd[cmd.index("--match") + 1].strip("^$")
        trace = RAW if run == "closedStaysClosed" else dict(
            RAW, states=[_st(0, OPEN, 0), _st(1, OPEN, 1), _st(2, CLOSED, 1)])
        Path(out).write_text(json.dumps(trace), encoding="utf-8")
        return FakeProc(stdout="ok")

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    monkeypatch.setattr(sr.shutil, "which", lambda name: "/usr/bin/quint")
    assert sr.generate(root, "shop", jobs=2) == 0
    written = json.loads(sr.trace_path(root, "shop", "closedStaysClosed").read_text())
    assert written["states"][-1]["_refused"] is True
    assert sr.main(["shop", "--root", str(root), "--check"]) == 0
    # The model moves on; the traces do not.
    (root / "specs" / "shop.qnt").write_text(SHOP.replace("LIMIT: int = 2", "LIMIT: int = 3"))
    assert [s for _r, _l, s in sr.status(root, "shop")] == ["stale", "stale"]
    assert sr.main(["shop", "--root", str(root), "--check"]) == 1


def test_the_shipped_examples_list_their_runs():
    area = spec_source.load_area(EXAMPLES, "subscription")
    text = (EXAMPLES / "specs" / "subscription.qnt").read_text(encoding="utf-8")
    runs = sr.area_runs(EXAMPLES, "subscription", area, spec_source.declarations(text))
    assert ("refundRefused", "EX-006") in runs
    assert ("cancelJourney", "journey: Cancel a subscription") in runs
    ui = spec_source.load_area(EXAMPLES, "auth-ui")
    text = (EXAMPLES / "specs" / "auth-ui.qnt").read_text(encoding="utf-8")
    assert ("signInJourney", "journey: Sign in") in sr.area_runs(
        EXAMPLES, "auth-ui", ui, spec_source.declarations(text))


record = _load("spec_record_r", "spec-record.py")


def test_verify_refuses_to_replay_until_the_run_traces_are_current(tmp_path, capsys):
    root = _project(tmp_path)
    (root / ".spec").mkdir()
    (root / ".spec" / "project.json").write_text(json.dumps(
        {"project": "p", "areas": [{"name": "shop", "kind": "area", "code_path": "src/"}]}),
        encoding="utf-8")
    intent = json.loads((root / "specs" / "shop.intent.json").read_text())
    intent["conformance"] = {"command": "true"}
    (root / "specs" / "shop.intent.json").write_text(json.dumps(intent), encoding="utf-8")
    (root / "src").mkdir()
    monkeypatch_argv = ["spec-record.py", "verify", "shop", "--root", str(root),
                        "--skip-tests", "--code-root", str(root)]
    old = sys.argv
    sys.argv = monkeypatch_argv
    try:
        with pytest.raises(SystemExit):
            record.main()
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert "2 example/journey run trace(s) not current" in out
    assert "closedStaysClosed missing" in out and "tools/spec-runs.py shop" in out
    assert "CONFORMANCE: PASS" not in out


lint = _load("spec_lint_r", "spec-lint.py")


def test_lint_warns_about_run_traces_once_a_harness_exists(tmp_path):
    root = _project(tmp_path)
    area = spec_source.load_area(root, "shop")
    findings = []
    lint.check_run_traces(root, area, "shop", findings)
    assert findings == [], "no harness yet: nothing replays them"
    area["conformance"] = {"command": "true"}
    lint.check_run_traces(root, area, "shop", findings)
    assert [(f.severity, f.check) for f in findings] == [(lint.WARN, "run-traces-not-current")]
    assert "closedStaysClosed: missing" in findings[0].description
