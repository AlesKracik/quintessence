"""What keeps /spec-check fast on real-sized models, and keeps its verdicts.

A timed-out child takes its own children down with it (tools/proc.py); the
rest of this file covers the probe generator and the check runner.
"""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
from proc import run_group  # noqa: E402


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for every purpose here.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


# ── 1. No orphans ────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_timeout_kills_grandchildren(tmp_path):
    """`quint` (node) starts the Rust evaluator as ITS child. Killing only the
    direct child left the evaluator re-parented to pid 1, burning a core for
    hours after the check it belonged to had timed out."""
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 300 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_group(["sh", "-c", script], timeout=1)
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid), "the grandchild outlived the timeout"


def test_run_group_keeps_subprocess_run_shape():
    p = run_group([sys.executable, "-c", "import sys; print('out'); "
                   "print('err', file=sys.stderr); sys.exit(3)"], timeout=30)
    assert p.returncode == 3
    assert p.stdout.strip() == "out" and p.stderr.strip() == "err"


itf = _load("itf_tools_speed", "itf_tools.py")
probes = _load("spec_probes_speed", "spec-probes.py")
record = _load("spec_record_speed", "spec-record.py")
EXAMPLES = TOOLS.parent / "examples"


# ── 4. Parameter ghosts by type position ────────────────────────────────────

def test_slots_are_allocated_by_type_position_not_by_name():
    """`items`, `newItems` and `moved` were three ghosts; one action never
    takes more than one Set[ItemId], so one slot holds all of them."""
    types = {"create": [("g", "OwnerId"), ("items", "Set[ItemId]"), ("active", "bool")],
             "update": [("g", "OwnerId"), ("newItems", "Set[ItemId]"),
                        ("draft", "bool"), ("pinned", "bool")],
             "move": [("src", "OwnerId"), ("dst", "OwnerId"), ("moved", "Set[ItemId]")]}
    slots = itf.param_slots(types, list(types))
    used = {sl for row in slots.values() for _n, _t, sl in row}
    assert used == {"_arg_OwnerId_1", "_arg_OwnerId_2", "_arg_Set_ItemId_1",
                    "_arg_bool_1", "_arg_bool_2"}
    assert [sl for _n, _t, sl in slots["move"]] == [
        "_arg_OwnerId_1", "_arg_OwnerId_2", "_arg_Set_ItemId_1"]


def test_one_name_with_two_types_is_no_longer_an_error(tmp_path):
    """The generator used to refuse a model whose `src` had one type in one
    action and another type in a second — forcing a rename to suit it."""
    qnt = tmp_path / "m.qnt"
    qnt.write_text('''module m {
  type G = str
  type P = str
  var x: int
  action init = x' = 0
  action a(src: G): bool = x' = x + 1
  action b(src: P): bool = x' = x
  action step = { nondet s = oneOf(Set("1")) any { a(s), b(s) } }
}
''', encoding="utf-8")
    from quint_ir import parse_qnt
    ir = parse_qnt(qnt)
    area = {"kind": "area", "area": "m", "requirements": [],
            "formal_model": {"quint_file": "m.qnt",
                             "probe_domains": {"G": 'Set("g")', "P": 'Set("p")'}}}
    out = "\n".join(probes.build(area, "m", ir, "m.qnt"))
    assert "var _arg_G_1: G" in out and "var _arg_P_1: P" in out
    assert "a(arg_G_1)" in out and "b(arg_P_1)" in out
    assert "//   a: src -> _arg_G_1" in out, "the name -> slot map is in the header"


def test_traces_are_written_back_under_parameter_names():
    """Replay and the readback ask for an argument by name. A slot holds a
    different parameter in each step, so the trace is rewritten to the
    _last<Param> convention: each holds what its action last passed."""
    slots = {"login": [("uid", "U", "_arg_U_1"), ("sid", "S", "_arg_S_1")],
             "logout": [("sid", "S", "_arg_S_1")]}
    trace = {"vars": ["x", "_lastAction", "_arg_U_1", "_arg_S_1"], "states": [
        {"x": 0, "_lastAction": "init", "_arg_U_1": "", "_arg_S_1": ""},
        {"x": 1, "_lastAction": "login", "_arg_U_1": "bob", "_arg_S_1": "s1"},
        {"x": 2, "_lastAction": "logout", "_arg_U_1": "bob", "_arg_S_1": "s2"},
    ]}
    itf.restore_param_names(trace, slots)
    st = trace["states"]
    assert st[0]["_lastUid"] == "" and st[0]["_lastSid"] == ""
    assert (st[1]["_lastUid"], st[1]["_lastSid"]) == ("bob", "s1")
    assert (st[2]["_lastUid"], st[2]["_lastSid"]) == ("bob", "s2")
    assert not any(k.startswith("_arg_") for s in st for k in s)
    assert "_lastUid" in trace["vars"] and "_arg_U_1" not in trace["vars"]
    assert itf.action_params(st[1]) == ["bob", "s1"]


# ── 3. One slice per check ──────────────────────────────────────────────────

MODULE = '''module a_probes {
  import a.* from "./a"
  var _lastAction: str
  var _arg_U_1: U
  var _pre_REQ_001: bool
  var _pre_REQ_002: bool
  var _old_REQ_002_before: int
  var _prevX: int
  def post_REQ_001(u: U): bool = x > 0
  action initP = all {
    init,
    _lastAction' = "init",
    _arg_U_1' = "",
    _pre_REQ_001' = false,
    _pre_REQ_002' = false,
    _old_REQ_002_before' = 0,
    _prevX' = 0,
  }
  action stepP = {
    nondet arg_U_1 = oneOf(Set("u"))
    any {
      all { f(arg_U_1),
            _lastAction' = "f",
            _arg_U_1' = arg_U_1,
            _pre_REQ_001' = true,
            _pre_REQ_002' = true,
            _old_REQ_002_before' = x,
            _prevX' = x,
      },
    }
  }

  /// INV-001: x only grows
  val grows: bool =
    x >= _prevX

  /// REQ-001: ...
  val oblig_REQ_001: bool =
    not(_lastAction == "f" and _pre_REQ_001)
      or post_REQ_001(_arg_U_1)

  /// REQ-002: ...
  val oblig_REQ_002: bool =
    not(_lastAction == "f" and _pre_REQ_002) or x == _old_REQ_002_before + 1

  /// Witness for REQ-001
  val witness_REQ_001: bool =
    not(_lastAction == "f"
        and _pre_REQ_001
        and post_REQ_001(_arg_U_1))
  val reached_REQ_001: bool = not(witness_REQ_001)
}
'''


def test_a_slice_keeps_only_what_its_check_reads():
    out = probes.slice_text(MODULE, ["oblig_REQ_001"], "a_slice", "../../..")
    assert "module a_slice {" in out and 'import a.* from "../../../a"' in out
    # Always kept: the step label, and every argument slot (a trace must
    # carry every step's arguments for replay).
    assert "var _lastAction" in out and "var _arg_U_1" in out
    assert "var _pre_REQ_001" in out and "_pre_REQ_001' = true" in out
    for gone in ("_pre_REQ_002", "_old_REQ_002_before", "_prevX"):
        assert f"var {gone}" not in out and f"{gone}' =" not in out
    # Vals reading a dropped ghost go; so do vals reading a dropped val.
    assert "val grows" not in out and "val oblig_REQ_002" not in out
    # Kept: defs, and the check itself.
    assert "def post_REQ_001" in out and "val oblig_REQ_001" in out
    assert "Ghosts kept: 3 of 6" in out


def test_a_slice_of_an_unknown_check_is_refused():
    with pytest.raises(KeyError):
        probes.slice_text(MODULE, ["oblig_REQ_009"], "a_slice", "./a")


def _free_endpoint():
    """A port of this test process's own. quint starts an Apalache server per
    call on its endpoint's port; parallel test workers sharing quint's
    default race to bind it, and the loser reads as Apalache not running."""
    import socket
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return f"localhost:{sock.getsockname()[1]}"


def _apalache_runs():
    """quint is on PATH AND its Apalache starts. CI's tool-test job has the
    CLI but a Java too old for the Apalache quint downloads: there a slice
    and the whole module both "fail" alike, which would prove nothing."""
    from quint_ir import cli_available
    if not cli_available():
        return False
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        m = Path(d) / "smoke.qnt"
        m.write_text("module smoke {\n  var x: int\n  action init = x' = 0\n"
                     "  action step = x' = x\n  val ok = x == 0\n}\n", encoding="utf-8")
        try:
            p = run_group(["quint", "verify", "--invariant=ok", "--max-steps=1",
                           f"--server-endpoint={_free_endpoint()}", str(m)],
                          timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "[ok]" in (p.stdout + p.stderr)


@pytest.mark.skipif(not _apalache_runs(), reason="quint/Apalache cannot run here")
@pytest.mark.parametrize("area,checks", [
    ("auth", ["oblig_REQ_003", "witness_REQ_001", "witness_REQ_004",
              "verdict_tick_1"]),
    ("subscription", ["oblig_REQ_001", "witness_REQ_007_email"]),
])
def test_a_slice_gives_the_verdict_the_whole_module_gives(tmp_path, area, checks):
    """Same transition relation, same formula: the same answer, on the
    examples, for an obligation, a witness that is reachable, one that is not
    (auth REQ-004) and a completeness check."""
    import shutil
    specs = tmp_path / "specs"
    shutil.copytree(EXAMPLES / "specs", specs,
                    ignore=shutil.ignore_patterns("traces", "gen"))
    full = specs / f"{area}.probes.qnt"
    text = full.read_text(encoding="utf-8")
    slice_dir = specs / area / "gen" / "slices"
    slice_dir.mkdir(parents=True)

    def verdict(path, name):
        p = run_group(["quint", "verify", f"--invariant={name}", "--init=initP",
                       "--step=stepP", "--max-steps=3",
                       f"--server-endpoint={_free_endpoint()}", str(path)], timeout=600)
        out = p.stdout + p.stderr
        assert "[ok]" in out or "[violation]" in out, out[-800:]
        return p.returncode

    for name in checks:
        mod = probes.slice_module_name(f"{area.replace('-', '_')}_probes", name)
        sl = slice_dir / f"{mod}.qnt"
        sl.write_text(probes.slice_text(text, [name], mod, "../../.."),
                      encoding="utf-8")
        assert verdict(sl, name) == verdict(full, name), name


# ── 2. Simulator witnesses ──────────────────────────────────────────────────

def test_a_simulated_witness_must_end_in_its_own_step():
    tr = {"states": [{"_lastAction": "init"}, {"_lastAction": "f", "_pre_REQ_1": True}]}
    assert record.simulated_witness_ok(tr, "f", "_pre_REQ_1")
    assert not record.simulated_witness_ok(tr, "g", None)
    assert not record.simulated_witness_ok(
        {"states": [{"_lastAction": "init"}]}, "init", None), "never hollow"
    tr["states"][1]["_pre_REQ_1"] = False
    assert not record.simulated_witness_ok(tr, "f", "_pre_REQ_1")


def test_the_simulator_hands_over_a_trace_only_for_a_violation(tmp_path, monkeypatch):
    out = tmp_path / "w.itf.json"
    seen = []

    def fake(cmd, timeout=None, **k):
        seen.append(cmd)
        if "--invariant=witness_A" in cmd:
            Path(out).write_text('{"states": []}', encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 1, "[violation] Found an issue", "")
        Path(out).write_text('{"states": []}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "[ok] No violation found", "")

    monkeypatch.setattr(record, "run_group", fake)
    r, _d, _t = record.run_simulate_trace("quint", "p.qnt", "witness_A", 100, 3, 60, out,
                                          seed="7", init="initP", step="stepP")
    assert r == "counterexample" and out.exists()
    assert {"--invariant=witness_A", "--seed=7", "--init=initP", "--step=stepP",
            f"--out-itf={out}"} <= set(seen[0])
    r, _d, _t = record.run_simulate_trace("quint", "p.qnt", "witness_B", 100, 3, 60, out)
    assert r == "ok" and not out.exists(), "a sample run is not a witness"


def test_fmt_secs_reads_like_a_duration():
    assert record.fmt_secs(42) == "42s"
    assert record.fmt_secs(210) == "3m30s"
    assert record.fmt_secs(8100) == "2h15m"


# ── 7. What a model costs to check ──────────────────────────────────────────

lint = _load("spec_lint_speed", "spec-lint.py")

COSTLY = '''module shop {
  type Id = str
  var items: Id -> int
  var pubs: int
  var tags: Set[Id]
  pure val IDS = Set("a", "b", "c")
  action init = all { items' = Map(), pubs' = 0, tags' = Set() }
  action add(i: Id, t: Set[Id]): bool = all {
    items' = items.put(i, 1), pubs' = pubs + 1, tags' = t,
  }
  action keep(i: Id): bool = all { items' = items, pubs' = pubs, tags' = tags }
  action step = {
    nondet i = oneOf(IDS)
    nondet t = oneOf(IDS.powerset())
    any { add(i, t), keep(i) }
  }
}
'''


def _cost(tmp_path, text, reqs=(), domains=None):
    qnt = tmp_path / "shop.qnt"
    qnt.write_text(text, encoding="utf-8")
    sidecar = lint.parse_sidecar(qnt)
    area = {"kind": "area", "area": "shop", "requirements": list(reqs),
            "formal_model": {"quint_file": "shop.qnt",
                             "probe_domains": domains or {"Id": "IDS",
                                                          "Set[Id]": "IDS.powerset()"}}}
    findings = []
    rep = lint.cost_report(tmp_path, area, sidecar)
    lint.check_cost(tmp_path, area, sidecar, "shop", findings, report=rep)
    return rep, {f.check for f in findings}


def test_the_cost_rules_name_the_shapes_that_dominate_a_check(tmp_path):
    req = {"id": "REQ-001", "quint_ref": "add", "form": "event",
           "ears": {"response": "pubs == before + 1"},
           "old": [{"name": "before", "type": "int", "expr": "pubs"}]}
    rep, checks = _cost(tmp_path, COSTLY, [req])
    assert {"growing-map", "powerset-nondet", "counter-only-incremented"} <= checks
    # add: 3 ids × 8 subsets; keep: 3 ids.
    assert rep["branching"] == 3 * 8 + 3
    assert rep["factors"][0][1] == "add"


def test_a_counter_compared_to_a_bound_is_not_flagged(tmp_path):
    req = {"id": "REQ-001", "quint_ref": "add", "form": "event",
           "ears": {"response": "pubs <= 3"}}
    _rep, checks = _cost(tmp_path, COSTLY, [req])
    assert "counter-only-incremented" not in checks


def test_a_total_map_and_small_domains_are_quiet(tmp_path):
    text = (COSTLY.replace("items' = Map()", "items' = IDS.mapBy(_ => 0)")
            .replace("items.put(i, 1)", "items.set(i, 1)")
            .replace("oneOf(IDS.powerset())", "oneOf(Set(Set(), Set(\"a\")))"))
    _rep, checks = _cost(tmp_path, text, domains={"Id": "IDS",
                                                  "Set[Id]": 'Set(Set(), Set("a"))'})
    assert not checks & {"growing-map", "powerset-nondet", "branching"}


def test_domain_sizes_are_read_off_the_text():
    consts = {"IDS": 'Set("a", "b")', "M": 'Map("x" -> 1, "y" -> 2, "z" -> 3)'}
    assert lint.domain_size("IDS", consts) == 2
    assert lint.domain_size("IDS.powerset()", consts) == 4
    assert lint.domain_size("0.to(2)", consts) == 3
    assert lint.domain_size("M.keys()", consts) == 3
    assert lint.domain_size("someCall(x)", consts) is None


def test_a_lost_race_for_the_apalache_port_is_retried_not_recorded(tmp_path, monkeypatch):
    """A timed-out check is killed with its process group, and the shared
    Apalache server it started goes too. Two parallel checks then race to
    start the next one; the loser's bind failure says nothing about the
    model, so it is run again instead of being recorded as an error."""
    calls = []

    def fake(cmd, timeout=None, **k):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "Error while starting Apalache "
                                               "server: Failed to bind to address "
                                               "0.0.0.0:8822: Address already in use")
        return subprocess.CompletedProcess(cmd, 0, "[ok] No violation found", "")

    monkeypatch.setattr(record, "run_group", fake)
    monkeypatch.setattr(record.time, "sleep", lambda s: None)
    r, _d, _t = record.run_verify("quint", tmp_path / "a.qnt", "inv", 3, 60)
    assert r == "verified" and len(calls) == 2
