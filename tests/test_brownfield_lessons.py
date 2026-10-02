"""Regressions from a brownfield test run: /spec bootstrapped a spec-only repo
for an existing Fastify/MongoDB API in another repo, then extracted one area.
Every test here is a problem that run actually hit.

Run:  python -m pytest tests/ -q     (from the repo root)
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import itf_tools as itf  # noqa: E402
import quint_ir  # noqa: E402
import spec_source  # noqa: E402


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probes = _load("bl_spec_probes", "spec-probes.py")
lint = _load("bl_spec_lint", "spec-lint.py")
audit = _load("bl_spec_extract_audit", "spec-extract-audit.py")
router = _load("bl_spec_route", "spec-route.py")
readback = _load("bl_spec_readback", "spec-readback.py")


# ── helpers ─────────────────────────────────────────────────────────────────

def write_project(root, areas, local=None):
    (root / ".spec").mkdir(parents=True, exist_ok=True)
    (root / "specs").mkdir(parents=True, exist_ok=True)
    (root / ".spec" / "project.json").write_text(json.dumps(
        {"project": "p", "areas": areas}), encoding="utf-8")
    if local is not None:
        (root / ".spec" / "local.json").write_text(json.dumps(local), encoding="utf-8")


def write_area(root, name, qnt, intent=None, records=None):
    specs = root / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    base = {"kind": "area", "area": name, "version": "0.1.0", "status": "structured",
            "formal_model": {"quint_file": f"{name}.qnt"}}
    if intent:
        for k, v in intent.items():
            if k == "formal_model":
                base["formal_model"].update(v)
            else:
                base[k] = v
    (specs / f"{name}.intent.json").write_text(json.dumps(base), encoding="utf-8")
    (specs / f"{name}.qnt").write_text(qnt, encoding="utf-8")
    if records is not None:
        (specs / f"{name}.records.json").write_text(json.dumps(records), encoding="utf-8")


def lint_json(root, area=None):
    cmd = [sys.executable, str(TOOLS / "spec-lint.py"), "--root", str(root), "--json"]
    if area:
        cmd.insert(2, area)
    out = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return json.loads(out.stdout)


def checks(report, area=None):
    return {(f["severity"].upper(), f["check"], f.get("ref")) for f in report["findings"]
            if area is None or f["area"] == area}


def check_names(report, area=None):
    return {c for _s, c, _r in checks(report, area)}


# ── 1. Routing reaches brownfield extract for bootstrapped areas ────────────

SCAFFOLD_QNT = "module ents { }\n"


def _bootstrapped(tmp_path, code=True, **entry):
    e = {"name": "ents", "kind": "area", "code_path": "src/"}
    e.update(entry)
    write_project(tmp_path, [e])
    write_area(tmp_path, "ents", SCAFFOLD_QNT, {"status": "raw"})
    if code:
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "ents.ts").write_text(
            "export function put(x) {\n  if (x > 5) { throw new Error('big') }\n"
            "  return x\n}\n", encoding="utf-8")


def test_a_bootstrapped_area_with_code_routes_to_brownfield_extract(tmp_path):
    """Bootstrap scaffolds the intent file, so 'intent missing + code' never
    matched and every such area went to resume."""
    _bootstrapped(tmp_path)
    r = router.route(tmp_path, "ents")
    assert r["beat"] == "brownfield-extract", r
    assert r["code_files"] == 1 and r["sites"] > 0


def test_after_extraction_the_same_area_routes_to_resume(tmp_path):
    _bootstrapped(tmp_path)
    (tmp_path / "specs" / "ents.records.json").write_text(json.dumps(
        {"extracted_from": {"date": "2026-01-01T00:00:00Z", "code_sha": "abc"}}),
        encoding="utf-8")
    assert router.route(tmp_path, "ents")["beat"] == "resume"


def test_requirements_already_written_route_to_resume(tmp_path):
    _bootstrapped(tmp_path)
    write_area(tmp_path, "ents", "module ents {\n/// @req REQ-001\n/// @status raw\n"
               "/// stores entities\n}\n", {"status": "raw"})
    assert router.route(tmp_path, "ents")["beat"] == "resume"


def test_no_code_routes_to_greenfield(tmp_path):
    _bootstrapped(tmp_path, code=False)
    assert router.route(tmp_path, "ents")["beat"] == "greenfield-elicit"


def test_no_project_routes_to_bootstrap(tmp_path):
    assert router.route(tmp_path, "x")["beat"] == "bootstrap"


def test_a_code_repo_without_a_local_path_asks_for_it(tmp_path):
    _bootstrapped(tmp_path, code_repo="api")
    assert router.route(tmp_path, "ents")["beat"] == "configure-repo"
    (tmp_path / ".spec" / "local.json").write_text(
        json.dumps({"repo_paths": {"api": str(tmp_path)}}), encoding="utf-8")
    assert router.route(tmp_path, "ents")["beat"] == "brownfield-extract"


# ── 11. Target typos do not silently become new areas ───────────────────────

@pytest.mark.parametrize("typo", ["values-stream", "values-streams", "value-stream"])
def test_a_near_miss_target_asks_did_you_mean(tmp_path, typo):
    write_project(tmp_path, [{"name": "value-streams", "kind": "area"}])
    write_area(tmp_path, "value-streams", SCAFFOLD_QNT)
    r = router.route(tmp_path, typo)
    assert r["beat"] == "confirm-target"
    assert r["candidates"][0] == "value-streams"
    assert "Did you mean `value-streams`?" in r["ask"]


def test_an_unrelated_new_name_is_not_a_typo(tmp_path):
    write_project(tmp_path, [{"name": "value-streams", "kind": "area"}])
    write_area(tmp_path, "value-streams", SCAFFOLD_QNT)
    assert router.route(tmp_path, "billing")["beat"] == "greenfield-elicit"


# ── 12. Large areas are suggested a pass ────────────────────────────────────

def test_a_large_area_is_suggested_passes(tmp_path, monkeypatch):
    _bootstrapped(tmp_path)
    monkeypatch.setattr(audit, "SLICE_SITES", 1)
    monkeypatch.setattr(router, "_audit", lambda: audit)
    assert "passes" in router.route(tmp_path, "ents")


def test_deferred_exclusions_are_listed_with_their_sites():
    area = {"scope": {"excluded": [
        {"item": "read-api", "reason": "Deferred to pass 2 of this area: GET routes"},
        {"item": "admin", "reason": "owned elsewhere", "pass": 3},
        {"item": "billing", "reason": "another area"}]},
        "extraction_triage": [
            {"fingerprint": "aaaaaaaa", "verdict": "OUT-OF-SCOPE", "scope_ref": "read-api"},
            {"fingerprint": "bbbbbbbb", "verdict": "OUT-OF-SCOPE", "scope_ref": "read-api"},
            {"fingerprint": "cccccccc", "verdict": "OUT-OF-SCOPE", "scope_ref": "billing"}]}
    got = {d["item"]: (d["pass"], d["sites"]) for d in audit.deferred_exclusions(area)}
    assert got == {"read-api": (2, 2), "admin": (3, 0)}


# ── 2. Probe ghosts for record-typed state ──────────────────────────────────

RECORD_QNT = '''module ents {
  type Id = str
  type Status = Draft | Published(int)
  type Result =
    | Ok
    | Refused(str)
  type State = {
    items: Id -> Status,
    version: int,
    flags: Set[str],
  }
  var st: State
  var last: Result
  action init = all { st' = { items: Map(), version: 0, flags: Set() }, last' = Ok }
  action store(id: Id): bool = all {
    st' = { ...st, items: st.items.put(id, Draft), version: st.version + 1 },
    last' = Ok }
  action step = { nondet i = oneOf(Set("a")) store(i) }
}
'''


def _ir_of(tmp_path, text, engine="regex"):
    p = tmp_path / "m.qnt"
    p.write_text(text, encoding="utf-8")
    return quint_ir.parse_qnt(p, engine=engine)


def test_zero_value_records_sums_tuples_and_overrides(tmp_path):
    ir = _ir_of(tmp_path, RECORD_QNT)
    assert quint_ir.zero_value("State", ir) == \
        "{ items: Map(), version: 0, flags: Set() }"
    assert quint_ir.zero_value("Result", ir) == "Ok"
    assert quint_ir.zero_value("Status", ir) == "Draft"
    assert quint_ir.zero_value("(int, Id)", ir) == '(0, "")'
    assert quint_ir.zero_value("{ s: Status, n: List[int] }", ir) == "{ s: Draft, n: [] }"
    assert quint_ir.zero_value("Result", ir, {"Result": 'Refused("")'}) == 'Refused("")'
    assert quint_ir.zero_value("Unknown", ir) is None
    assert quint_ir.zero_value("State => State", ir) is None


def test_a_sum_whose_first_variant_has_a_payload_zeroes_the_payload(tmp_path):
    ir = _ir_of(tmp_path, "module m {\n  type R = Err(str) | Ok\n  var x: R\n}\n")
    assert quint_ir.zero_value("R", ir) == 'Err("")'


def test_a_recursive_type_has_no_zero_and_does_not_hang(tmp_path):
    ir = _ir_of(tmp_path, "module m {\n  type T = { next: T }\n  var x: T\n}\n")
    assert quint_ir.zero_value("T", ir) is None


def _record_area(delta, extra_fm=None):
    fm = {"quint_file": "ents.qnt", "probe_domains": {"Id": 'Set("a", "b")'}}
    fm.update(extra_fm or {})
    return {"kind": "area", "area": "ents", "formal_model": fm, "invariants": [],
            "requirements": [{"id": "REQ-001", "description": "store stores a Draft",
                              "quint_ref": "store",
                              "witness": {"predicate": "st.items.get(_lastId) == Draft",
                                          "delta": {"pre": delta}}}]}


def test_probe_module_snapshots_a_record_state_var(tmp_path):
    """`var st: State` is the shape a conformance adapter with st()/last()
    getters produces; it used to fail with 'cannot synthesize an initial
    value for: _prevSt (State)'."""
    ir = _ir_of(tmp_path, RECORD_QNT)
    area = _record_area("not(_prevSt.items.keys().contains(_lastId))")
    out = "\n".join(probes.build(area, "ents", ir, "ents.qnt"))
    assert "var _prevSt: State" in out
    assert "_prevSt' = { items: Map(), version: 0, flags: Set() }," in out


@pytest.mark.skipif(not quint_ir.cli_available(), reason="quint CLI not installed")
def test_the_generated_record_probe_module_typechecks(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "ents.qnt").write_text(RECORD_QNT, encoding="utf-8")
    ir = quint_ir.parse_qnt(specs / "ents.qnt")
    area = _record_area("not(_prevSt.items.keys().contains(_lastId))")
    (specs / "ents.probes.qnt").write_text(
        "\n".join(probes.build(area, "ents", ir, "ents.qnt")) + "\n", encoding="utf-8")
    r = subprocess.run(["quint", "typecheck", str(specs / "ents.probes.qnt")],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr


FUNC_VAR_QNT = '''module ents {
  type Id = str
  type Box[a] = Empty | Full(a)
  var st: Box[int]
  action init = st' = Empty
  action store(id: Id): bool = st' = Full(1)
  action step = { nondet i = oneOf(Set("a")) store(i) }
}
'''


def test_lint_flags_a_prev_ghost_with_no_zero_at_authoring_time(tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", FUNC_VAR_QNT.replace(
        "  action store(id: Id)",
        "  /// @req REQ-001\n  /// @status draft\n  /// put fills it\n"
        "  /// @via store\n  /// @pre _prevSt == Empty\n"
        "  def shall_REQ_001(_lastId: Id): bool = st != Empty\n\n  action store(id: Id)"))
    rep = lint_json(tmp_path, "ents")
    assert ("FAIL", "ghost-zero-unsynthesizable", "REQ-001") in checks(rep)

    # ...and an explicit ghost_zeros entry settles it.
    intent = json.loads((tmp_path / "specs" / "ents.intent.json").read_text())
    intent["formal_model"]["ghost_zeros"] = {"Box[int]": "Empty"}
    (tmp_path / "specs" / "ents.intent.json").write_text(json.dumps(intent))
    assert "ghost-zero-unsynthesizable" not in check_names(lint_json(tmp_path, "ents"))


def test_lint_is_quiet_for_a_record_prev_ghost(tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", RECORD_QNT.replace(
        "  action store(id: Id)",
        "  /// @req REQ-001\n  /// @status draft\n  /// put stores a Draft\n"
        "  /// @via store\n  /// @pre not(_prevSt.items.keys().contains(_lastId))\n"
        "  def shall_REQ_001(_lastId: Id): bool = st.items.get(_lastId) == Draft\n\n"
        "  action store(id: Id)"))
    assert "ghost-zero-unsynthesizable" not in check_names(lint_json(tmp_path, "ents"))


# ── 3. Helper actions are not mirrored ──────────────────────────────────────

HELPER_QNT = '''module ents {
  type Id = str
  type Role = str
  type State = { items: Set[Id] }
  type Result = Ok | Refused(str)
  var st: State
  var last: Result
  action init = all { st' = { items: Set() }, last' = Ok }
  action refuse(r: Result): bool = all { last' = r, st' = st }
  action commit(s1: State, r: Result): bool = all { st' = s1, last' = r }
  action occUpsert(id: Id, apply: State => State): bool = commit(apply(st), Ok)
  action createEntity(id: Id, role: Role): bool =
    if (role == "admin") commit({ items: st.items.union(Set(id)) }, Ok)
    else refuse(Refused("forbidden"))
  action deleteEntity(id: Id): bool =
    occUpsert(id, s => { items: s.items.exclude(Set(id)) })
  action step = {
    nondet i = oneOf(Set("a"))
    nondet r = oneOf(Set("admin", "user"))
    any { createEntity(i, r), deleteEntity(i) }
  }
}
'''


def _helper_area(quint_ref="createEntity", domains=None):
    return {"kind": "area", "area": "ents", "invariants": [],
            "formal_model": {"quint_file": "ents.qnt",
                             "probe_domains": domains or {"Id": 'Set("a")', "Role": 'Set("admin", "user")'}},
            "requirements": [{"id": "REQ-001", "description": "admin creates",
                              "quint_ref": quint_ref,
                              "witness": {"predicate": "st.items.contains(_lastId)"}}]}


@pytest.mark.parametrize("engine", ["regex", "auto"])
def test_step_p_mirrors_only_the_actions_step_calls(tmp_path, engine):
    ir = _ir_of(tmp_path, HELPER_QNT, engine)
    out = "\n".join(probes.build(_helper_area(), "ents", ir, "ents.qnt"))
    assert '_lastAction\' = "createEntity"' in out
    assert '_lastAction\' = "deleteEntity"' in out
    for helper in ("refuse", "commit", "occUpsert"):
        assert f'_lastAction\' = "{helper}"' not in out
    # No ghost for a helper's parameters — `apply: State => State` has none.
    assert "_lastApply" not in out and "_lastS1" not in out and "_lastR:" not in out
    assert "var _lastRole: Role" in out


def test_a_witness_through_a_helper_action_is_refused_by_name(tmp_path, capsys):
    ir = _ir_of(tmp_path, HELPER_QNT)
    with pytest.raises(SystemExit):
        probes.build(_helper_area(quint_ref="commit"), "ents", ir, "ents.qnt")
    assert "REQ-001 @via commit" in capsys.readouterr().err


# ── 10. Probe domains per parameter ─────────────────────────────────────────

def test_param_domain_key_wins_over_the_type_key(tmp_path):
    qnt = HELPER_QNT.replace("  type Role = str\n", "").replace("role: Role", "role: str")
    ir = _ir_of(tmp_path, qnt)
    area = _helper_area(domains={"Id": 'Set("a")', "str": 'Set("x")',
                                 "param:role": 'Set("admin", "user")'})
    out = "\n".join(probes.build(area, "ents", ir, "ents.qnt"))
    assert 'nondet role = oneOf(Set("admin", "user"))' in out


# ── 4. Computed requirements are not refusals ───────────────────────────────

COMPUTED_QNT = '''module score {
  /// @req REQ-010
  /// @status specified
  /// @shall compute effort as the sum of issue effort
  /// @witness skipped: a derived value, no transition to witness
  /// @verified-by backend/src/utils/__tests__/score.test.ts
  pure def effort(xs: List[int]): int = xs.foldl(0, (a, b) => a + b)

  /// @req REQ-011
  /// @status specified
  /// @shall compute TCV by priority
  /// @witness skipped: derived value
  pure def tcv(p: int): int = p * 2

  var x: int
  action init = x' = 0
  action step = x' = x
}
'''


def test_computed_requirements_owe_a_test_not_a_refusal(tmp_path):
    write_project(tmp_path, [{"name": "score", "kind": "area"}])
    write_area(tmp_path, "score", COMPUTED_QNT)
    area = spec_source.load_area(tmp_path, "score")
    reqs = {r["id"]: r for r in area["requirements"]}
    assert reqs["REQ-010"]["verified_by"] == ["backend/src/utils/__tests__/score.test.ts"]
    assert itf.is_computed(reqs["REQ-010"]) and not itf.is_rejection(reqs["REQ-010"])
    found = checks(lint_json(tmp_path, "score"))
    names = {c for _s, c, _r in found}
    assert "refusal-without-artifact" not in names
    assert "refusal-without-unchanged" not in names
    assert ("WARN", "computed-without-verified-by", "REQ-011") in found
    assert not any(c == "computed-without-verified-by" and r == "REQ-010"
                   for _s, c, r in found)


def test_an_unwanted_skip_is_still_a_refusal():
    req = {"ears": {"unwanted": True},
           "witness": {"status": "skipped", "justification": "INV-001"}}
    assert itf.is_rejection(req) and not itf.is_computed(req)
    assert not itf.is_computed({"modality": "forbidden", "witness": {"status": "skipped"}})


def test_the_readback_calls_it_computed(tmp_path):
    req = {"id": "REQ-010", "verified_by": ["t/score.test.ts"],
           "witness": {"status": "skipped", "justification": "derived"}}
    assert itf.skip_discharge(req["witness"], req) == "verified by t/score.test.ts"
    assert readback.status_mark(req) == "⊘"


# ── 5. @source on INV and CON ───────────────────────────────────────────────

def test_source_is_accepted_on_invariants_and_constants():
    text = '''module m {
  /// @con CON-001
  /// Max items.
  /// @source extracted
  /// @evidence routes/x.ts:10
  pure val MAX: int = 5
  var n: int
  /// @inv INV-001
  /// Never above MAX.
  /// @source extracted
  val bounded: bool = n <= MAX
}
'''
    problems = []
    part, _ = spec_source.derive_records(text, "m.qnt", problems=problems)
    assert problems == []
    assert part["constraints"][0]["source"] == "extracted"
    assert part["invariants"][0]["source"] == "extracted"


# ── 6. Unqualified refs and decision ids ────────────────────────────────────

REFS_QNT = '''module ents {
  /// @req REQ-030
  /// @status raw
  /// the base requirement
  /// @req REQ-031
  /// @status raw
  /// refines the base
  /// @refs REQ-030, DEC-002
  /// @req REQ-032
  /// @status raw
  /// dangling
  /// @refs REQ-099
}
'''


def test_unqualified_refs_are_same_area_and_decisions_resolve(tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", REFS_QNT,
               {"decisions": [{"id": "DEC-002", "title": "error codes",
                               "decision": "409 on conflict"}]})
    area = spec_source.load_area(tmp_path, "ents")
    by = {r["id"]: r for r in area["requirements"]}
    assert by["REQ-031"]["cross_refs"] == ["ents.REQ-030", "ents.DEC-002"]
    found = checks(lint_json(tmp_path, "ents"))
    assert not any(c == "bad-cross-ref-format" for _s, c, _r in found)
    assert not any(r == "REQ-031" and "cross-ref" in c for _s, c, r in found)
    assert ("FAIL", "broken-cross-ref", "REQ-032") in found


# ── 7. A run ending in .expect(...) asserts something ───────────────────────

EXPECT_QNT = '''module ents {
  var n: int
  action init = n' = 0
  action bump: bool = n' = n + 1
  action step = bump

  /// @example EX-001
  /// bumping once
  run bumpOnce = init.then(bump).expect(n == 1)

  /// @example EX-002
  /// silent
  run bumpSilently = init.then(bump)
}
'''


def test_a_run_expect_is_the_examples_expectation(tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", EXPECT_QNT)
    area = spec_source.load_area(tmp_path, "ents")
    ex = {e["id"]: e for e in area["examples"]}
    assert ex["EX-001"]["expect"] == {spec_source.RUN_EXPECT_KEY: "n == 1"}
    assert not ex["EX-002"]["expect"]
    found = checks(lint_json(tmp_path, "ents"))
    assert ("WARN", "example-asserts-nothing", "EX-002") in found
    assert ("WARN", "example-asserts-nothing", "EX-001") not in found


def test_chained_expects_are_joined():
    # Only the TRAILING chain counts: an expect mid-run is followed by more steps.
    assert spec_source._run_expectation(
        "init.then(a).expect(x == 1).then(b).expect(y > (2))") == "y > (2)"
    assert spec_source._run_expectation(
        "init.then(a).expect(x == 1).expect(f(y) > 2)") == "x == 1 and f(y) > 2"
    assert spec_source._run_expectation("init.then(a)") is None


# ── 8. Summarize a directory of recovered traces ────────────────────────────

def test_summarize_a_directory_of_traces():
    out = subprocess.run([sys.executable, str(TOOLS / "itf_tools.py"), "summarize",
                          str(ROOT / "examples" / "specs")],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr
    assert "== auth/traces/REQ-003.itf.json" in out.stdout
    assert "login_failed(bob)" in out.stdout


# ── 9. Scattered code, test files, bulk triage ──────────────────────────────

def test_code_paths_globs_and_default_test_exclusion():
    inc, exc = spec_source.code_scope({
        "code_paths": ["backend/src/routes/entities*.ts", "backend/src/services/"],
        "exclude": ["backend/src/services/legacy/**"], "tests_path": "backend/tests/"})
    yes = ["backend/src/routes/entities.ts", "backend/src/services/a/b.ts"]
    no = ["backend/src/routes/users.ts", "backend/src/services/a.test.ts",
          "backend/src/services/__tests__/x.ts", "backend/src/services/x.spec.ts",
          "backend/src/services/legacy/old.ts", "backend/tests/x.ts"]
    assert all(spec_source.in_code_scope(p, inc, exc) for p in yes)
    assert not any(spec_source.in_code_scope(p, inc, exc) for p in no)
    inc, exc = spec_source.code_scope({"code_path": "src/", "include_tests": True})
    assert spec_source.in_code_scope("src/a.test.ts", inc, exc)


def test_audit_reads_code_paths_and_bulk_triages(tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area",
                              "code_paths": ["backend/src/routes/", "backend/src/utils/"]}])
    write_area(tmp_path, "ents", SCAFFOLD_QNT,
               {"scope": {"excluded": [{"item": "utils", "reason": "Deferred to pass 2"}]}})
    for rel, body in [("backend/src/routes/e.ts", "if (a) { return 1 }\n"),
                      ("backend/src/utils/u.ts", "if (b) { throw x }\n"),
                      ("backend/src/utils/__tests__/u.test.ts", "if (c) { return 2 }\n"),
                      ("backend/src/other/o.ts", "if (d) { return 3 }\n")]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    base = [sys.executable, str(TOOLS / "spec-extract-audit.py"), "ents",
            "--root", str(tmp_path), "--json"]
    rep = json.loads(subprocess.run(base, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL).stdout)
    files = {s["file"] for s in rep["unclaimed_sites"]}
    assert files == {"backend/src/routes/e.ts", "backend/src/utils/u.ts"}

    bad = subprocess.run(base[:-1] + ["--triage-file", "backend/src/utils/**",
                                      "--verdict", "OUT-OF-SCOPE", "--scope-ref", "nope"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert bad.returncode == 2 and "not in scope.excluded" in bad.stderr

    ok = subprocess.run(base[:-1] + ["--triage-file", "backend/src/utils/**",
                                     "--verdict", "OUT-OF-SCOPE", "--scope-ref", "utils"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert ok.returncode == 0, ok.stderr
    rep = json.loads(subprocess.run(base, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL).stdout)
    assert {s["file"] for s in rep["unclaimed_sites"]} == {"backend/src/routes/e.ts"}
    assert rep["problems"] == []
    assert rep["deferred"] == [{"item": "utils", "pass": 2,
                                "sites": rep["triaged"], "reason": "Deferred to pass 2"}]


# ── 13. No git, and local.json has a schema ─────────────────────────────────

def test_setup_hooks_outside_git_skips_and_succeeds(tmp_path):
    (tmp_path / "tools").mkdir()
    script = tmp_path / "tools" / "setup-hooks.sh"
    script.write_text((TOOLS / "setup-hooks.sh").read_text(), encoding="utf-8")
    r = subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin", "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipping the pre-commit hook" in r.stdout


def test_local_schema_accepts_the_documented_keys_and_rejects_typos():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schemas" / "local.schema.json").read_text())
    v = jsonschema.Draft7Validator(schema)
    assert not list(v.iter_errors({"repo_paths": {"api": "/x"}, "last_change": "initial-spec"}))
    assert list(v.iter_errors({"repo_path": {"api": "/x"}}))


# ── 14. A lint run without jsonschema says it is partial ────────────────────

def test_partial_lint_is_stated_at_the_top_and_bottom(capsys):
    lint.print_report([], ["a"], use_color=False, partial=["schema validation (x)"])
    out = capsys.readouterr().out
    assert out.startswith("PARTIAL RESULT")
    assert out.rstrip().endswith("PARTIAL (see top)")


def test_partial_reasons_track_the_library(monkeypatch):
    monkeypatch.setattr(lint, "_jsonschema", None)
    assert lint.partial_reasons()
    monkeypatch.setattr(lint, "_jsonschema", object())
    assert lint.partial_reasons() == []


# ── 15. Built-in names ──────────────────────────────────────────────────────

def test_a_helper_named_after_a_builtin_fails_lint(tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", '''module ents {
  var s: Set[str]
  def exists(c: Set[str], id: str): bool = c.contains(id)
  def hasId(c: Set[str], id: str): bool = c.contains(id)
  action init = s' = Set()
  action step = s' = s
}
''')
    found = checks(lint_json(tmp_path, "ents"))
    assert ("FAIL", "quint-builtin-redefined", "exists") in found
    assert not any(r == "hasId" for _s, _c, r in found)


# ── Docs agree with the tools ───────────────────────────────────────────────

def test_skill_template_and_methodology_name_the_new_rules():
    skill = (ROOT / ".claude" / "commands" / "spec.md").read_text()
    template = (ROOT / "templates" / "spec.qnt.template").read_text()
    method = (ROOT / "METHODOLOGY.md").read_text()
    assert "tools/spec-route.py" in skill
    assert "no `extracted_from`" in skill
    assert "0. Look for a prior spec" in skill and "itf_tools.py summarize" in skill
    assert "schemas/local.schema.json" in skill
    for word in ("exists", "forall", "filter", "fold", "keys", "contains", "size"):
        assert word in template
    assert "pure def" in template and "param:" in template
    assert "@verified-by" in template and "@refs REQ-" in template
    assert "@verified-by" in method and "code_paths" in method
    for text in (skill, method):
        assert "NAV-NNN" in text and "* -> " in text
        assert "transition-invariant-name-clash" in text
    assert "_over" in template and "MUST differ" in template
    assert "transition_invariants" in method


# ── 16. A transition invariant named like its own def ───────────────────────

TRANSITION_QNT = '''module ents {
  var x: int
  action init = x' = 0
  action step = x' = x + 1

  /// @inv INV-009
  /// x never decreases
  /// @quint-name NAME
  def HOST(_prevX: int): bool = x >= _prevX
}
'''


@pytest.mark.parametrize("name,host,clash", [
    ("noDecrease", "noDecrease", True),          # the reported case
    ("step", "noDecrease_over", True),           # any other model name
    ("noDecrease", "noDecrease_over", False),    # the documented shape
])
def test_a_quint_name_the_model_already_declares_fails_lint(tmp_path, name, host, clash):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", TRANSITION_QNT.replace("NAME", name).replace("HOST", host))
    found = checks(lint_json(tmp_path, "ents"))
    assert (("FAIL", "transition-invariant-name-clash", "INV-009") in found) == clash


# ── 17 / 18. Navigation ids, and edges from every screen ────────────────────

UI_QNT = '''module shop {
  /// @screen Home
  /// Landing.
  /// @screen Cart
  /// The basket.
  /// @screen Settings
  /// Reached only from the sidebar.
  type Screen = Home | Cart | Settings
  var screen: Screen
  action init = screen' = Home

  /// @nav NAV-001 Home -> Cart
  /// click "Cart"
  action openCart: bool = all { screen == Home, screen' = Cart }

  /// @nav NAV-002 Cart -> Home
  /// click "Back"
  action back: bool = all { screen == Cart, screen' = Home }

  /// @nav NAV-003 * -> Settings
  /// click "Settings" in the sidebar
  action openSettings: bool = screen' = Settings

  action step = any { openCart, back, openSettings }
}
'''


def _ui(tmp_path, qnt=UI_QNT, records=None):
    write_project(tmp_path, [{"name": "shop", "kind": "area"}])
    write_area(tmp_path, "shop", qnt, records=records)
    return spec_source.load_area(tmp_path, "shop", with_info=True)


def test_navigation_records_carry_ids_and_a_from_every_screen_edge(tmp_path):
    area, info = _ui(tmp_path)
    assert info["problems"] == []
    navs = {n.get("id"): n for n in area["navigation"]}
    assert navs["NAV-001"] == {"id": "NAV-001", "from": "Home", "to": "Cart",
                               "trigger": 'click "Cart"', "action": "openCart"}
    assert navs["NAV-003"]["from"] == "*"
    names = check_names(lint_json(tmp_path, "shop"))
    # Settings is reached from every screen: not isolated, and the action
    # that only SETS the screen is a correct model of a sidebar link.
    assert "isolated-screen" not in names
    assert "nav-action-mismatch" not in names
    assert "navigation-unknown-screen" not in names


def test_a_sidebar_edge_whose_action_does_not_go_there_still_fails(tmp_path):
    _ui(tmp_path, UI_QNT.replace("openSettings: bool = screen' = Settings",
                                 "openSettings: bool = screen' = Home"))
    assert ("FAIL", "nav-action-mismatch", "openSettings") in checks(lint_json(tmp_path, "shop"))


def test_a_parameterized_navigate_action_can_carry_sidebar_edges():
    assert spec_source.nav_moves("all { screen' = target }", "screen", "*", "Settings")
    assert not spec_source.nav_moves("all { screen' = screen }", "screen", "*", "Settings")


def test_audit_sites_map_to_navigation_ids(tmp_path):
    rows = [{"file": "ui/nav.tsx", "fingerprint": "abcd0001", "verdict": "MAPPED",
             "maps_to": ["NAV-003"]},
            {"file": "ui/nav.tsx", "fingerprint": "abcd0002", "verdict": "MAPPED",
             "maps_to": ["NAV-099"]}]
    area, _ = _ui(tmp_path, records={"extraction_triage": rows})
    found = checks(lint_json(tmp_path, "shop"))
    assert ("FAIL", "extraction-maps-to-dangling", "abcd0001") not in found
    assert ("FAIL", "extraction-maps-to-dangling", "abcd0002") in found
    sites = [{"fingerprint": "abcd0001", "file": "ui/nav.tsx", "line": 1}]
    assert audit.audit(area, sites)["problems"] == []


def test_duplicate_navigation_ids_are_a_grammar_problem(tmp_path):
    _area, info = _ui(tmp_path, UI_QNT.replace("NAV-002 Cart", "NAV-001 Cart"))
    assert any("duplicate navigation id NAV-001" in p for p in info["problems"])


def test_the_readback_draws_a_sidebar_edge_once(tmp_path):
    area, _ = _ui(tmp_path)
    text = "\n".join(readback.ui_sections(area))
    assert "ANY((every screen)) --> |click \"Settings\" in the sidebar| Settings" in text
    assert "| NAV-003 | _every screen_ | Settings |" in text


def test_journey_steps_may_cite_navigation_edges(tmp_path):
    _ui(tmp_path)
    (tmp_path / "specs" / "journeys").mkdir()
    (tmp_path / "specs" / "journeys" / "settings.journey.json").write_text(json.dumps(
        {"name": "settings", "actor": "user", "description": "open settings",
         "steps": [{"ref": "shop.NAV-003"}]}))
    names = check_names(lint_json(tmp_path))
    assert "dangling-step-ref" not in names


# ── 19. The simulator checks transition invariants on the probe module ──────

record = _load("bl_spec_record", "spec-record.py")


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_a_name_error_is_an_error_not_a_falsified_invariant(monkeypatch, tmp_path):
    """`Name not found: noDecrease` names the invariant, and was read as the
    simulator having violated it."""
    monkeypatch.setattr(record.subprocess, "run", lambda *a, **k: _FakeProc(
        1, stderr="error: [QNT404] Name 'noDecrease' not found\nerror: parsing failed"))
    result, _d, _t, _c, falsified = record.run_simulate(
        "quint", tmp_path / "a.qnt", ["noDecrease"], 10, 5, 60)
    assert result == "error" and falsified == []


SIM_QNT = '''module ents {
  var x: int
  action init = x' = 0
  action step = x' = x + 1

  /// @inv INV-001
  /// x is never negative
  val nonNegative: bool = x >= 0

  /// @inv INV-002
  /// x never decreases
  /// @quint-name noDecrease
  def noDecrease_over(_prevX: int): bool = x >= _prevX
}
'''


def test_transition_invariants_are_simulated_on_the_probe_module(monkeypatch, tmp_path):
    write_project(tmp_path, [{"name": "ents", "kind": "area"}])
    write_area(tmp_path, "ents", SIM_QNT,
               {"formal_model": {"probes_file": "ents.probes.qnt"}})
    (tmp_path / "specs" / "ents.probes.qnt").write_text(
        "module ents_probes {\n  var _prevX: int\n  val noDecrease: bool = true\n}\n")
    calls = []

    def fake_sim(quint, target, invariants, *a, init=None, step=None, **k):
        calls.append((Path(target).name, tuple(invariants), init, step))
        return "ok", "", 0.1, {}, []

    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: False)
    monkeypatch.setattr(record, "run_simulate", fake_sim)

    class Args:
        root, area = str(tmp_path), "ents"
        steps = timeout = only = None
        no_witness, emit_json = True, False
        no_simulate, only_simulate = False, True
        samples = seed = None
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(Args())
    assert exc.value.code == 0
    assert ("ents.qnt", ("nonNegative",), None, None) in calls
    assert ("ents.probes.qnt", ("noDecrease",), "initP", "stepP") in calls
    sim = spec_source.load_area(tmp_path, "ents")["check_results"]["simulation"]
    assert sim["transition_invariants"]["checked"] == 1
    assert "falsified" not in sim["invariants"]
