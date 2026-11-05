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


FOLD_SET = '''module cfg {
  type Feat = str
  var cfg: str -> bool
  var on: Set[Feat]
  pure val KEYS = Set("a", "b")
  pure val ALL_FEATS = Set("fa", "fb")
  pure def enabled(c: str -> bool): Set[Feat] =
    c.keys().fold(Set(), (acc, k) => if (c.get(k)) acc.union(Set("f" + k)) else acc)
  pure def tally(c: str -> bool): { seen: Set[str], n: int } =
    c.keys().fold({ seen: Set(), n: 0 }, (acc, k) => { seen: acc.seen.union(Set(k)), n: acc.n + 1 })
  pure def unused(c: str -> bool): Set[str] =
    c.keys().fold(Set(), (acc, k) => acc.union(Set(k)))
  pure def count(c: str -> bool): int = c.keys().fold(0, (acc, k) => acc + 1)
  action init = all { cfg' = KEYS.mapBy(_ => false), on' = Set() }
  action flip(k: str): bool = all {
    cfg' = cfg.set(k, not(cfg.get(k))), on' = enabled(cfg).union(tally(cfg).seen),
  }
  action step = { nondet k = oneOf(KEYS) flip(k) }
}
'''


def test_a_set_built_by_fold_from_an_action_is_flagged(tmp_path):
    qnt = tmp_path / "cfg.qnt"
    qnt.write_text(FOLD_SET, encoding="utf-8")
    found = lint.fold_set_builders(qnt.read_text())
    # enabled (Set accumulator) and tally (record with a Set field) are on
    # flip's path; `unused` is not reachable, `count` folds an int.
    assert sorted(d for d, *_ in found) == ["enabled", "tally"]
    rep, checks = _cost(tmp_path, FOLD_SET.replace("module cfg", "module shop"),
                        domains={"str": "KEYS"})
    assert "fold-builds-set" in checks


def test_a_filter_over_a_constant_set_is_quiet():
    text = FOLD_SET.replace(
        'c.keys().fold(Set(), (acc, k) => if (c.get(k)) acc.union(Set("f" + k)) else acc)',
        'ALL_FEATS.filter(f => c.get(f.slice(1, 2)))')
    assert "enabled" not in [d for d, *_ in lint.fold_set_builders(text)]


# ── 8. Environment settings fixed at init ───────────────────────────────────

VM = '''module vm {
  type Vm = str
  type Cfg = str
  var vmConfig: Vm -> Cfg
  var running: Set[Vm]
  pure val VMS = Set("a", "b")
  pure val CFGS = Set("small", "big")

  action init = all {
    vmConfig' = VMS.mapBy(_ => "small"),
    running' = Set(),
  }

  /// @env the operator sizes each VM before anything runs
  action env_vm_config(v: Vm, c: Cfg): bool = all {
    VMS.contains(v),
    vmConfig' = vmConfig.set(v, c),
    running' = running,
  }

  /// @req REQ-001
  /// Starting a VM runs it.
  /// @status specified
  /// @shall running.contains(v)
  action start(v: Vm): bool = all {
    not(running.contains(v)),
    running' = running.union(Set(v)),
    vmConfig' = vmConfig,
  }

  action step = {
    nondet v = oneOf(VMS)
    nondet c = oneOf(CFGS)
    any { env_vm_config(v, c), start(v) }
  }
}
'''


def _vm(tmp_path, text=VM, env=("env_vm_config",)):
    import spec_source
    from quint_ir import parse_qnt
    qnt = tmp_path / "vm.qnt"
    qnt.write_text(text, encoding="utf-8")
    area = {"kind": "area", "area": "vm",
            "formal_model": {"quint_file": "vm.qnt",
                             "probe_domains": {"Vm": "VMS", "Cfg": "CFGS"},
                             "env_at_init": list(env)}}
    area.update(spec_source.derive_records(text, "vm.qnt")[0])
    return area, parse_qnt(qnt)


def _probes(area, ir, text=VM):
    return "\n".join(probes.build(area, "vm", ir, "vm.qnt", text=text))


def test_an_env_tag_is_derived_onto_the_area():
    import spec_source
    out = spec_source.derive_records(VM, "vm.qnt")[0]
    assert out["env_actions"] == [{"action": "env_vm_config",
                                   "reason": "the operator sizes each VM before anything runs"}]


def test_an_env_setting_moves_from_stepP_into_initP(tmp_path):
    area, ir = _vm(tmp_path)
    out = _probes(area, ir)
    init = out[out.index("action initP"):out.index("action stepP")]
    step = out[out.index("action stepP"):]
    # Applied once per key, the other argument chosen per key up front.
    assert "nondet _env_env_vm_config_c = oneOf(setOfMaps(VMS, CFGS))" in init
    assert "VMS.fold({ vmConfig: VMS.mapBy(_ => \"small\") }, (_acc, _k) =>" in init
    assert "_acc.vmConfig.set(_k, _env_env_vm_config_c.get(_k))" in init
    assert "vmConfig' = _env_env_vm_config.vmConfig" in init
    assert "running' = Set()" in init and "    init," not in init
    # Never a step, and no CFGS choice left in stepP; an idle step keeps a
    # model whose env action was the one always enabled from deadlocking.
    assert "env_vm_config" not in step and "oneOf(CFGS)" not in step
    assert '_lastAction\' = "_idle"' in step
    assert "fixed per trace" in out.lower()


def test_without_env_at_init_the_module_is_unchanged(tmp_path):
    area, ir = _vm(tmp_path, env=())
    out = _probes(area, ir)
    assert "action initP = all {\n    init," in out
    assert '"env_vm_config"' in out and "_idle" not in out


@pytest.mark.skipif(not _apalache_runs(), reason="quint/Apalache cannot run here")
def test_a_fixed_env_setting_still_checks_clean(tmp_path):
    area, ir = _vm(tmp_path)
    (tmp_path / "vm.probes.qnt").write_text(_probes(area, ir) + "\n", encoding="utf-8")
    def verify(inv):
        p = run_group(["quint", "verify", f"--invariant={inv}", "--max-steps=4",
                       "--init=initP", "--step=stepP",
                       f"--server-endpoint={_free_endpoint()}",
                       str(tmp_path / "vm.probes.qnt")], timeout=300)
        return p.stdout + p.stderr
    assert "[ok]" in verify("oblig_REQ_001")          # no deadlock either
    assert "counterexample" in verify("witness_REQ_001")


@pytest.mark.parametrize("edit,needle", [
    # untagged: no @env, no OUT-OF-SCOPE verdict
    (lambda t: t.replace("  /// @env the operator sizes each VM before anything runs\n", ""),
     "neither tagged"),
    # another action writes the same var
    (lambda t: t.replace("vmConfig' = vmConfig,\n  }\n\n  action step",
                         "vmConfig' = vmConfig.set(v, \"big\"),\n  }\n\n  action step"),
     "other actions also write"),
    # a requirement sits on it
    (lambda t: t.replace("  /// @env the operator",
                         "  /// @req REQ-002\n  /// Configures.\n  /// @status specified\n"
                         "  /// @shall vmConfig.get(v) == c\n  /// @env the operator"),
     "hosts REQ-002"),
    # it reads a var it does not write
    (lambda t: t.replace("VMS.contains(v),\n    vmConfig'",
                         "not(running.contains(v)),\n    vmConfig'"),
     "reads `running`"),
])
def test_an_env_setting_that_could_hide_behavior_is_refused(tmp_path, capsys, edit, needle):
    text = edit(VM)
    area, ir = _vm(tmp_path, text=text)
    with pytest.raises(SystemExit):
        _probes(area, ir, text)
    assert needle in capsys.readouterr().err


def test_a_body_that_is_not_flat_needs_declared_values(tmp_path, capsys):
    text = VM.replace("""all {
    VMS.contains(v),
    vmConfig' = vmConfig.set(v, c),
    running' = running,
  }""", """if (VMS.contains(v)) all { vmConfig' = vmConfig.set(v, c), running' = running }
    else all { vmConfig' = vmConfig, running' = running }""")
    area, ir = _vm(tmp_path, text=text)
    with pytest.raises(SystemExit):
        _probes(area, ir, text)
    assert '"values"' in capsys.readouterr().err
    area, ir = _vm(tmp_path, text=text, env=[{"action": "env_vm_config",
                                              "values": {"vmConfig": "setOfMaps(VMS, CFGS)"}}])
    out = _probes(area, ir, text)
    assert "nondet _env_env_vm_config_vmConfig = oneOf(setOfMaps(VMS, CFGS))" in out
    assert "vmConfig' = _env_env_vm_config_vmConfig" in out


def test_lint_hints_an_eligible_env_setting_until_it_is_listed(tmp_path):
    def checks(env):
        area, _ir = _vm(tmp_path, env=env)
        sidecar = lint.parse_sidecar(tmp_path / "vm.qnt")
        findings = []
        lint.check_cost(tmp_path, area, sidecar, "vm", findings)
        return {f.check for f in findings}
    assert "env-at-init-eligible" in checks(())
    assert "env-at-init-eligible" not in checks(("env_vm_config",))


# ── 9. Record-state models and helper functions ─────────────────────────────
# A model whose state is one record (`var st: State`) writes `st` in every
# action. Compared per var, no env action was ever independent of the rest;
# compared per FIELD, the one that sets `st.cfg` is.

RS = '''module rs {
  type Key = str
  type Item = str
  type Cfg = { region: str, tier: int }
  type State = {
    items: Item -> bool,
    cfg: Key -> Cfg,
    dirty: bool,
  }
  var st: State

  pure val KEYS = Set("k1", "k2")
  pure val ITEMS = Set("i1", "i2")
  pure val CFG0: Cfg = { region: "eu", tier: 0 }
  pure val CFGS = Set(CFG0, { region: "us", tier: 1 })

  pure def commit(s: State, i: Item): State = { ...s, items: s.items.set(i, true), dirty: true }
  pure def opaque(s: State, k: Key, c: Cfg): State =
    s.with("cfg", s.cfg.set(k, c))

  action init = { st' = { items: ITEMS.mapBy(_ => false), cfg: KEYS.mapBy(_ => CFG0), dirty: false } }

  /// @verdict OUT-OF-SCOPE
  /// Configuration is resolved outside this area.
  /// @scope configuration resolution
  action env_set_cfg(k: Key, c: Cfg): bool =
    st' = { ...st, cfg: st.cfg.set(k, c) }

  /// @req REQ-001
  /// Adding an item marks it present.
  /// @status specified
  /// @shall st.items.get(i)
  action add(i: Item): bool = all {
    not(st.items.get(i)),
    st' = commit(st, i),
  }

  action step = {
    nondet k = oneOf(KEYS)
    nondet c = oneOf(CFGS)
    nondet i = oneOf(ITEMS)
    any { env_set_cfg(k, c), add(i) }
  }

  /// @example EX-001
  /// Configure, then add.
  /// @refs REQ-001
  run configureThenAdd = init.then(env_set_cfg("k1", { region: "us", tier: 1 }))
    .then(add("i1")).expect(st.items.get("i1") and st.cfg.get("k1").tier == 1)
}
'''


def _rs(tmp_path, text=RS, env=()):
    import spec_source
    from quint_ir import parse_qnt
    qnt = tmp_path / "rs.qnt"
    qnt.write_text(text, encoding="utf-8")
    area = {"kind": "area", "area": "rs",
            "scope": {"excluded": [{"item": "configuration resolution"}]},
            "formal_model": {"quint_file": "rs.qnt",
                             "probe_domains": {"Key": "KEYS", "Cfg": "CFGS",
                                               "Item": "ITEMS"},
                             "env_at_init": list(env)}}
    area.update(spec_source.derive_records(text, "rs.qnt")[0])
    return area, parse_qnt(qnt)


def _rs_cost(tmp_path, text=RS, env=()):
    area, _ir = _rs(tmp_path, text, env)
    findings = []
    lint.check_cost(tmp_path, area, lint.parse_sidecar(tmp_path / "rs.qnt"),
                    "rs", findings)
    return [f for f in findings if f.check == "env-at-init-eligible"]


RS_ONE_LINE = RS.replace('''type State = {
    items: Item -> bool,
    cfg: Key -> Cfg,
    dirty: bool,
  }''', "type State = { items: Item -> bool, cfg: Key -> Cfg, dirty: bool }")


@pytest.mark.parametrize("text", [RS, RS_ONE_LINE], ids=["multi-line", "one-line"])
def test_field_writes_and_reads_are_read_off_a_record_state(text):
    import quint_ir
    import tempfile
    assert text != RS or "type State = {\n" in text
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rs.qnt"
        p.write_text(text, encoding="utf-8")
        for engine in ("regex",) + (("cli",) if quint_ir.cli_available() else ()):
            ir = quint_ir.parse_qnt(p, engine)
            muts, reads = ir["action_field_mutations"], ir["action_field_reads"]
            assert muts["env_set_cfg"] == {"st": ["cfg"]}
            # add's new state comes from a helper: followed one level.
            assert muts["add"] == {"st": ["dirty", "items"]}
            assert reads["env_set_cfg"] == {"st": ["cfg"]}
            assert reads["add"] == {"st": ["items"]}


def test_a_record_state_env_setting_is_fixed_by_field(tmp_path):
    hint = _rs_cost(tmp_path)
    assert hint and "env_set_cfg" in hint[0].description
    assert not _rs_cost(tmp_path, env=["env_set_cfg"])

    area, ir = _rs(tmp_path, env=["env_set_cfg"])
    out = "\n".join(probes.build(area, "rs", ir, "rs.qnt", text=RS))
    init = out[out.index("action initP"):out.index("action stepP")]
    step = out[out.index("action stepP"):]
    # Only st.cfg is fixed; every other field keeps init's value.
    assert ("st' = { items: ITEMS.mapBy(_ => false), "
            "cfg: _env_env_set_cfg.st__cfg, dirty: false }") in init
    assert ("KEYS.fold({ st__cfg: KEYS.mapBy(_ => CFG0) }, (_acc, _k) => "
            "{ st__cfg: _acc.st__cfg.set(_k, _env_env_set_cfg_c.get(_k)) })") in init
    assert "env_set_cfg" not in step

    from quint_ir import cli_available
    if not cli_available():
        return
    (tmp_path / "rs.probes.qnt").write_text(out + "\n", encoding="utf-8")
    tc = run_group(["quint", "typecheck", str(tmp_path / "rs.probes.qnt")], timeout=300)
    assert tc.returncode == 0, tc.stdout + tc.stderr
    # The model is untouched: its example still calls the env action.
    t = run_group(["quint", "test", "--match=configureThenAdd",
                   str(tmp_path / "rs.qnt")], timeout=300)
    assert t.returncode == 0 and "configureThenAdd passed" in t.stdout, t.stdout + t.stderr


def test_another_writer_of_the_same_field_blocks_it_by_name(tmp_path):
    import completeness
    text = RS.replace("items: s.items.set(i, true), dirty: true",
                      "items: s.items.set(i, true), cfg: s.cfg.set(\"k1\", CFG0), dirty: true")
    area, ir = _rs(tmp_path, text)
    why = " ".join(completeness.env_blockers(area, ir, "env_set_cfg"))
    assert "other actions also write `st.cfg` (add)" in why
    assert not _rs_cost(tmp_path, text)


def test_a_shared_bookkeeping_field_is_named_and_can_be_left_to_init(tmp_path, capsys):
    import completeness
    text = RS.replace("st' = { ...st, cfg: st.cfg.set(k, c) }",
                      "st' = { ...st, cfg: st.cfg.set(k, c), dirty: true }")
    area, ir = _rs(tmp_path, text)
    why = completeness.env_blockers(area, ir, "env_set_cfg")
    assert len(why) == 1 and "it also writes `st.dirty`" in why[0] and "ignore" in why[0]
    # The hint says how to list it.
    hint = _rs_cost(tmp_path, text)
    assert hint and '"ignore": ["st.dirty"]' in hint[0].description
    # Listed without `ignore`: refused, naming the field.
    area, ir = _rs(tmp_path, text, env=["env_set_cfg"])
    with pytest.raises(SystemExit):
        probes.build(area, "rs", ir, "rs.qnt", text=text)
    assert "`st.dirty`" in capsys.readouterr().err
    # With it: st.dirty stays at init's value, st.cfg is fixed.
    area, ir = _rs(tmp_path, text, env=[{"action": "env_set_cfg",
                                         "ignore": ["st.dirty"]}])
    out = "\n".join(probes.build(area, "rs", ir, "rs.qnt", text=text))
    assert "cfg: _env_env_set_cfg.st__cfg, dirty: false }" in out
    assert "left to init: st.dirty" in out


def test_values_are_keyed_by_field(tmp_path):
    area, ir = _rs(tmp_path, env=[{"action": "env_set_cfg",
                                   "values": {"st.cfg": "setOfMaps(KEYS, CFGS)"}}])
    out = "\n".join(probes.build(area, "rs", ir, "rs.qnt", text=RS))
    assert "nondet _env_env_set_cfg_st__cfg = oneOf(setOfMaps(KEYS, CFGS))" in out
    assert "cfg: _env_env_set_cfg_st__cfg, dirty: false }" in out


def test_an_unresolvable_record_write_falls_back_to_the_whole_var(tmp_path):
    import completeness
    text = RS.replace("st' = { ...st, cfg: st.cfg.set(k, c) }",
                      "st' = opaque(st, k, c)")
    area, ir = _rs(tmp_path, text)
    assert "env_set_cfg" not in ir["action_field_mutations"]
    assert completeness.action_writes(ir, "env_set_cfg") == {"st"}
    why = " ".join(completeness.env_blockers(area, ir, "env_set_cfg"))
    assert "other actions also write `st`" in why
    assert not _rs_cost(tmp_path, text)


HELPER_FOLD = '''module bag {
  type Key = str
  type Bag = { xs: Set[str], ys: Set[str] }
  type State = { members: Key -> Set[str], built: Bag, total: int }
  var st: State
  pure val KEYS = Set("g")
  pure val EMPTY: Bag = { xs: Set(), ys: Set() }
  pure def merge(a: Bag, b: Bag): Bag = { xs: a.xs.union(b.xs), ys: a.ys.union(b.ys) }
  pure def partFor(m: str): Bag = { xs: Set(m), ys: Set() }
  pure def plus(a: int, b: int): int = a + b
  def build(s: State, g: Key): Bag =
    s.members.get(g).fold(EMPTY, (acc, m) => merge(acc, partFor(m)))
  def names(s: State, g: Key): Set[str] =
    s.members.get(g).fold(Set(), (acc, m) => acc.add(m))
  def count(s: State, g: Key): int =
    s.members.get(g).fold(0, (acc, m) => plus(acc, 1))
  action init = st' = { members: KEYS.mapBy(_ => Set("a")), built: EMPTY, total: 0 }
  action rebuild(g: Key): bool = st' = { ...st, built: build(st, g),
    total: count(st, g) + names(st, g).size() }
  action step = { nondet g = oneOf(KEYS) rebuild(g) }
}
'''


def test_a_fold_that_unions_through_a_helper_is_flagged_by_name(tmp_path):
    found = {d: via for d, _init, via in lint.fold_set_builders(HELPER_FOLD)}
    assert found == {"build": "merge", "names": None}, "count folds an int"
    qnt = tmp_path / "shop.qnt"
    qnt.write_text(HELPER_FOLD.replace("module bag", "module shop"), encoding="utf-8")
    area = {"kind": "area", "area": "shop", "requirements": [],
            "formal_model": {"quint_file": "shop.qnt",
                             "probe_domains": {"Key": "KEYS"}}}
    findings = []
    lint.check_cost(tmp_path, area, lint.parse_sidecar(qnt), "shop", findings)
    msgs = [f.description for f in findings if f.check == "fold-builds-set"]
    assert any("unions through `merge`" in m and "`build`" in m for m in msgs)


# ── 10. Refusal chains and multi-line record types ──────────────────────────
# The shape /spec recommends: one `type State = {` field per line, route
# actions built from `refuse` / `commit` helpers in an if / else-if chain.
# Each used to resolve to "writes the whole var": the type because only its
# first line, `{`, reached type_aliases; the chain because `if` was opaque.

RC = '''module rc {
  type Key = str
  type Result = | Ok | NotFound | Busy
  type Entry = { exists: bool, on: bool }
  type Cfg = { region: str }
  type State = {
    items: Key -> Entry,
    cfg: Key -> Cfg,
    last: Result,
    dirty: bool,
    count: int,
  }
  var st: State
  pure val KEYS = Set("k1", "k2")
  pure val CFGS = Set({ region: "eu" }, { region: "us" })

  pure def refuse(s0: State, r: Result): State = { ...s0, last: r, dirty: true }
  pure def opaque(s: State): State = s.with("count", 1)

  action init = st' = {
    items: KEYS.mapBy(_ => { exists: true, on: false }),
    cfg: KEYS.mapBy(_ => { region: "eu" }),
    last: Ok,
    dirty: false,
    count: 0,
  }

  action toggle(k: Key, on: bool): bool = {
    val old = st.items.get(k)
    st' =
      if (not(old.exists)) refuse(st, NotFound)
      else if (old.on == on) refuse(st, Ok)
      else { ...st, last: Ok, items: st.items.set(k, { ...old, on: on }), dirty: true }
  }

  action bump(r: Result): bool = st' = match r {
    | Ok => { ...st, count: st.count + 1 }
    | NotFound => refuse(st, r)
    | Busy => st
  }

  action later(k: Key): bool = {
    val nxt = { ...st, count: st.count + 2 }
    st' = if (st.items.get(k).on) nxt else st
  }

  action weird(k: Key): bool = st' = if (k == "k1") opaque(st) else st

  /// @verdict OUT-OF-SCOPE
  /// Configuration is resolved outside this area.
  /// @scope configuration resolution
  action env_set_cfg(k: Key, c: Cfg): bool =
    st' = { ...st, cfg: st.cfg.set(k, c), dirty: true }

  action step = {
    nondet k = oneOf(KEYS)
    nondet c = oneOf(CFGS)
    nondet on = oneOf(Set(true, false))
    nondet r = oneOf(Set(Ok, NotFound, Busy))
    any { env_set_cfg(k, c), toggle(k, on), bump(r), later(k) }
  }
}
'''


def _engines():
    import quint_ir
    return ("regex",) + (("cli",) if quint_ir.cli_available() else ())


@pytest.mark.parametrize("engine", ["regex", "cli"])
def test_a_multi_line_record_type_is_a_record_not_an_alias(tmp_path, engine):
    import quint_ir
    if engine not in _engines():
        pytest.skip("quint CLI not installed")
    p = tmp_path / "rc.qnt"
    p.write_text(RC, encoding="utf-8")
    ir = quint_ir.parse_qnt(p, engine)
    assert "State" not in ir["type_aliases"], "only its first line, `{`, used to land here"
    assert quint_ir.record_var_fields(ir) == {
        "st": ["items", "cfg", "last", "dirty", "count"]}
    assert quint_ir.zero_value("State", ir).startswith("{ items: Map()")
    assert ir["action_field_mutations"]["env_set_cfg"] == {"st": ["cfg", "dirty"]}


@pytest.mark.parametrize("engine", ["regex", "cli"])
def test_conditional_new_states_resolve_to_the_union_of_their_branches(tmp_path, engine):
    import quint_ir
    if engine not in _engines():
        pytest.skip("quint CLI not installed")
    p = tmp_path / "rc.qnt"
    p.write_text(RC, encoding="utf-8")
    ir = quint_ir.parse_qnt(p, engine)
    muts, reads = ir["action_field_mutations"], ir["action_field_reads"]
    # if / else if / else through refuse(st, r), after a local val
    assert muts["toggle"] == {"st": ["dirty", "items", "last"]}
    assert reads["toggle"] == {"st": ["items"]}
    # match: one arm at a time; `=> st` keeps everything
    assert muts["bump"] == {"st": ["count", "dirty", "last"]}
    # a local val holding a whole new state, and `else st`
    assert muts["later"] == {"st": ["count"]}
    assert reads["later"] == {"st": ["count", "items"]}
    # a branch the resolver cannot read: the whole var, as before
    assert "weird" not in muts and "weird" not in reads


def test_an_env_setting_beside_a_refusal_chain_is_eligible(tmp_path):
    import completeness
    import spec_source
    from quint_ir import parse_qnt, cli_available
    (tmp_path / "rc.qnt").write_text(RC, encoding="utf-8")
    ir = parse_qnt(tmp_path / "rc.qnt")
    assert completeness.action_writes(ir, "toggle") == {"st.dirty", "st.items", "st.last"}

    def area(env):
        a = {"kind": "area", "area": "rc",
             "scope": {"excluded": [{"item": "configuration resolution"}]},
             "formal_model": {"quint_file": "rc.qnt",
                              "probe_domains": {"Key": "KEYS", "Cfg": "CFGS",
                                                "bool": "Set(true, false)",
                                                "Result": "Set(Ok, NotFound, Busy)"},
                              "env_at_init": env}}
        a.update(spec_source.derive_records(RC, "rc.qnt")[0])
        return a

    why = completeness.env_blockers(area([]), ir, "env_set_cfg")
    assert len(why) == 1 and "`st.dirty`" in why[0], why
    findings = []
    lint.check_cost(tmp_path, area([]), lint.parse_sidecar(tmp_path / "rc.qnt"),
                    "rc", findings)
    hint = [f for f in findings if f.check == "env-at-init-eligible"]
    assert hint and '"ignore": ["st.dirty"]' in hint[0].description

    out = "\n".join(probes.build(area([{"action": "env_set_cfg", "ignore": ["st.dirty"]}]),
                                 "rc", ir, "rc.qnt", text=RC))
    init = out[out.index("action initP"):out.index("action stepP")]
    assert "cfg: _env_env_set_cfg.st__cfg" in init and "dirty: false" in init
    assert "env_set_cfg" not in out[out.index("action stepP"):]
    if not cli_available():
        return
    (tmp_path / "rc.probes.qnt").write_text(out + "\n", encoding="utf-8")
    tc = run_group(["quint", "typecheck", str(tmp_path / "rc.probes.qnt")], timeout=300)
    assert tc.returncode == 0, tc.stdout + tc.stderr


# ── 11. Conditional nesting is not helper-call depth ────────────────────────
# Every `else if` used to cost one level of the helper-call budget, so a
# validation-heavy action (6+ refusals) resolved to every field, then to
# nothing at all, and blocked env_at_init for fields it never touches.

def _chain(n, deep=False):
    conds = " ".join(f"if (k == \"e{i}\") refuse(st, E{i}) else" for i in range(n))
    variants = " | ".join(["Ok"] + [f"E{i}" for i in range(max(n, 1))])
    helpers = ""
    success = "{ ...st, last: Ok, items: st.items.set(k, true) }"
    if deep:
        # a def calling a def calling a def ... past _FIELD_DEPTH
        helpers = "\n".join(
            f"  pure def h{i}(s: State): State = h{i + 1}(s)" for i in range(4))
        helpers += "\n  pure def h4(s: State): State = { ...s, count: 1 }\n"
        success = "h0(st)"
    return f'''module ch {{
  type Result = {variants}
  type State = {{
    items: str -> bool,
    cfg: str -> int,
    last: Result,
    dirty: bool,
    count: int,
  }}
  var st: State
  pure def refuse(s0: State, r: Result): State = {{ ...s0, last: r, dirty: true }}
{helpers}
  action init = st' = {{ items: Map(), cfg: Map(), last: Ok, dirty: false, count: 0 }}
  action update(k: str): bool = st' = {conds} {success}
  action step = update("a")
}}
'''


def _effects(tmp_path, text, engine="regex"):
    import quint_ir
    p = tmp_path / "ch.qnt"
    p.write_text(text, encoding="utf-8")
    return quint_ir.parse_qnt(p, engine)["action_field_mutations"]


@pytest.mark.parametrize("n", [1, 6, 12])
def test_a_long_refusal_chain_resolves_to_the_helper_and_success_fields(tmp_path, n):
    for engine in _engines():
        muts = _effects(tmp_path, _chain(n), engine)
        assert muts["update"] == {"st": ["dirty", "items", "last"]}, (engine, n)


def test_the_cap_still_stops_helper_calls_nested_too_deep(tmp_path):
    from quint_ir import _FIELD_DEPTH
    assert _FIELD_DEPTH < 5, "the chain below must outrun the cap"
    muts = _effects(tmp_path, _chain(8, deep=True))
    assert "update" not in muts, "a branch past the cap is unresolved: whole var"


def test_a_match_with_many_arms_resolves(tmp_path):
    arms = "\n".join(f"    | E{i} => refuse(st, E{i})" for i in range(10))
    text = _chain(10).replace(
        "action update(k: str): bool = st' = ",
        f"action pick(r: Result): bool = st' = match r {{\n    | Ok => {{ ...st, count: 2 }}\n{arms}\n  }}\n  action update(k: str): bool = st' = ")
    for engine in _engines():
        muts = _effects(tmp_path, text, engine)
        assert muts["pick"] == {"st": ["count", "dirty", "last"]}, engine
