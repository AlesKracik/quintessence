"""model_sha hashes what the model means, not how it is laid out
(itf_tools.compute_model_sha).

A stamp goes stale when what it proved could have changed, and only then:
layout, comments and runs leave the sha alone; every edit that reaches the
checker moves it.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
EXAMPLES = TOOLS.parent / "examples"
sys.path.insert(0, str(TOOLS))
import itf_tools as itf  # noqa: E402
import quint_ir  # noqa: E402
import spec_source  # noqa: E402


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sq = _load("spec_questions_sha", "spec-questions.py")

DECL_START = re.compile(r"^  (?:///|pure |val |def |action |run |var |const |type |temporal )")


@pytest.fixture
def root(tmp_path):
    shutil.copytree(EXAMPLES, tmp_path / "ex",
                    ignore=shutil.ignore_patterns("_apalache-out", "gen"))
    return tmp_path / "ex"


def _sha(root, area, include_runs=False):
    return itf.compute_model_sha(root, area, spec_source.load_area(root, area),
                                 include_runs=include_runs)


def _lint(root, area):
    out = subprocess.run([sys.executable, str(TOOLS / "spec-lint.py"), "--json",
                          "--root", str(root), area],
                         capture_output=True, text=True, timeout=120)
    report = json.loads(out.stdout)
    report["summary"].pop("ran_at", None)
    return report


def _move_block(text, header, before):
    """Move the declaration starting `header` (its `///` lines with it) to
    just before the one starting `before`."""
    lines = text.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith(header))
    s = i
    while lines[s - 1].startswith("  ///"):
        s -= 1
    e = i + 1
    while e < len(lines) and not DECL_START.match(lines[e]) and lines[e] != "}":
        e += 1
    block = lines[s:e]
    rest = lines[:s] + lines[e:]
    j = next(k for k, ln in enumerate(rest) if ln.startswith(before))
    while rest[j - 1].startswith("  ///"):
        j -= 1
    return "\n".join(rest[:j] + block + rest[j:])


def _relayout(text, move):
    text = re.sub(r"\n\n(  (?:///|action ))", r"\n\n  // a comment the checker never reads\n\n\1", text)
    text = re.sub(r",(\s*\n\s*\})", r"\1", text)          # trailing commas
    text = re.sub(r"(?m)^(    \S.*) (&&|and|or|\+|==) ", r"\1  \2  ", text)
    text = text.replace(" = all {", " = all  {")
    # Split one action header over several lines, and join a wrapped one.
    text = re.sub(r"(?m)^(  action \w+\()(\w+: \w+), ", r"\1\n    \2,\n    ", text, count=1)
    text = re.sub(r"\.put\(\n\s+", ".put(", text)
    return _move_block(text, *move)


@pytest.mark.parametrize("area,move", [
    ("auth", ("  action tick", "  action login(")),
    ("subscription", ("  action request_refund", "  action cancel_subscription")),
])
def test_a_layout_only_edit_leaves_the_sha_and_lint_alone(root, area, move):
    qnt = root / "specs" / f"{area}.qnt"
    sha, lint = _sha(root, area), _lint(root, area)
    text = qnt.read_text(encoding="utf-8")
    edited = _relayout(text, move)
    assert edited != text
    qnt.write_text(edited, encoding="utf-8")
    assert _sha(root, area) == sha
    assert _lint(root, area) == lint


@pytest.mark.parametrize("area", ["auth", "subscription"])
def test_adding_or_removing_a_run_leaves_the_sha_alone(root, area):
    qnt = root / "specs" / f"{area}.qnt"
    sha, sha_runs = _sha(root, area), _sha(root, area, include_runs=True)
    text = qnt.read_text(encoding="utf-8")
    # Remove the first run, add one with a helper only it uses.
    added = _move_block(text, "  run ", "}").rsplit("\n}", 1)[0]
    lines = added.split("\n")
    first = max(k for k, ln in enumerate(lines) if ln.startswith("  run "))
    lines = lines[:first]
    lines += ["  pure def onlyForTheRun(n: int): int = n + 1",
              "  run aNewExample = init.then(step).expect(onlyForTheRun(1) == 2)", "}", ""]
    qnt.write_text("\n".join(lines), encoding="utf-8")
    assert _sha(root, area) == sha
    assert _sha(root, area, include_runs=True) != sha_runs, \
        "evidence the runs produced must still see the runs"


@pytest.mark.parametrize("area,before,after", [
    ("auth", "    idleOf(sid) > MAX_SESSION_AGE,", "    idleOf(sid) >= MAX_SESSION_AGE,"),
    ("auth", "    not(isLocked(uid)),\n", ""),                              # drop a guard
    ("auth", "pure val MAX_FAILED_ATTEMPTS: int = 5", "pure val MAX_FAILED_ATTEMPTS: int = 6"),
    ("subscription", "    n != NoNotice,", "    n == NoNotice,"),
])
def test_an_edit_that_reaches_the_checker_moves_the_sha(root, area, before, after):
    qnt = root / "specs" / f"{area}.qnt"
    sha = _sha(root, area)
    area_data = spec_source.load_area(root, area)
    old = itf.model_index(root, area, area_data)
    text = qnt.read_text(encoding="utf-8")
    assert before in text
    qnt.write_text(text.replace(before, after, 1), encoding="utf-8")
    assert _sha(root, area) != sha
    # ... and every reachability verdict with it: the step relation moved.
    new = itf.model_index(root, area, area_data)
    for req in area_data["requirements"]:
        assert new.verdict_sha(req["id"]) != old.verdict_sha(req["id"]), req["id"]


def test_a_run_only_helper_still_counts_when_a_verdict_names_it():
    text = """module m {
  var x: int
  action init = x' = 0
  action step = x' = x + 1
  val inv: bool = x >= 0
  def helper: bool = x < 100
  run r = init.then(step).expect(helper and inv)
}
"""
    edited = text.replace("x < 100", "x < 99")
    assert itf.canonical_quint(text) == itf.canonical_quint(edited)
    # `inv` is reached by the run too, but a verdict names it (a record's
    # quint_name, a probe): a root is never "only reached by runs".
    weaker = text.replace("x >= 0", "x > 0")
    assert itf.canonical_quint(text, roots={"inv"}) != itf.canonical_quint(weaker, roots={"inv"})
    assert (itf.canonical_quint(text, roots={"helper"})
            != itf.canonical_quint(edited, roots={"helper"}))


@pytest.mark.parametrize("area", ["auth", "auth-ui", "subscription"])
def test_every_mutant_moves_the_sha(area):
    path = EXAMPLES / "specs" / f"{area}.qnt"
    text = path.read_text(encoding="utf-8")
    ir = quint_ir.parse_qnt(path, engine="regex")
    sites = sq.mutation_sites(text, spec_source.declarations(text), ir,
                              sq.imported_decls(EXAMPLES, text))
    assert sites
    base = itf.canonical_quint(text)
    same = [m["label"] for m in sites if itf.canonical_quint(sq.apply(text, m)) == base]
    assert not same, same


# ── One fingerprint per verdict, and why it went stale ─────────────────────

readback = _load("spec_readback_sha", "spec-readback.py")
lint = _load("spec_lint_sha", "spec-lint.py")


def _lint_checks(root, area):
    area_data = spec_source.load_area(root, area)
    findings = []
    lint.check_witnesses(root, area_data, area, findings)
    return [f for f in findings if f.check.endswith("-stale") or f.check.endswith("-unstamped")]


def _edit(path, before, after):
    text = path.read_text(encoding="utf-8")
    assert before in text, before
    path.write_text(text.replace(before, after, 1), encoding="utf-8")


def test_the_examples_are_stamped_per_verdict_and_fresh(root):
    for area in ("auth", "auth-ui", "subscription"):
        assert _lint_checks(root, area) == [], area
        rows, _missing, _ok = itf.witness_status(root, area, spec_source.load_area(root, area))
        assert not [r for r in rows if r[1] in ("STALE", "UNSTAMPED")], area


def test_an_action_edit_stales_every_proof_in_one_grouped_entry(root):
    _edit(root / "specs" / "auth.qnt", "    idleOf(sid) > MAX_SESSION_AGE,",
          "    idleOf(sid) >= MAX_SESSION_AGE,")
    found = _lint_checks(root, "auth")
    kinds = sorted(f.check for f in found)
    # Every verdict through the transition relation, one finding per kind.
    assert kinds == ["check-stale", "obligation-stale", "witness-stale"], kinds
    assert all("`expire_session` changed" in f.description for f in found)
    area = spec_source.load_area(root, "auth")
    items = readback.attention_items(root, "auth", area)
    stale = [i for i in items if "stale**" in i]
    assert len(stale) == 1, stale
    assert "`expire_session` changed" in stale[0]
    assert not [i for i in items if "Witness STALE" in i]


def test_editing_one_invariant_stales_only_that_invariant(root):
    area = spec_source.load_area(root, "auth")
    inv = next(c for c in area["check_results"]["checks"]
               if c.get("quint_name") == "noEarlyExpiry")
    _edit(root / "specs" / "auth.qnt", "  val noEarlyExpiry: bool =",
          "  val noEarlyExpiry: bool = true and")
    found = _lint_checks(root, "auth")
    assert [f.check for f in found] == ["check-stale"], found
    assert found[0].ref == inv["id"]
    assert "`noEarlyExpiry` changed" in found[0].description


def test_editing_one_requirements_predicate_stales_only_that_requirement(root):
    # What /spec-probes emits for REQ-002's @while: a def only its probes read.
    _edit(root / "specs" / "auth.probes.qnt", "  def pre_REQ_002(sid: SessionId): bool =\n    isActive(sid)",
          "  def pre_REQ_002(sid: SessionId): bool =\n    isActive(sid) and true")
    found = _lint_checks(root, "auth")
    assert found and {f.ref for f in found} == {"REQ-002"}, [(f.check, f.ref) for f in found]
    assert all("`pre_REQ_002` changed" in f.description for f in found)


def test_an_area_wide_stamp_is_fresh_until_the_model_moves_then_unstamped(root):
    def area_wide():
        area = spec_source.load_area(root, "auth")
        sha = _sha(root, "auth")
        for req in area["requirements"]:
            for block in (req.get("witness") or {}, req.get("obligation") or {}):
                if block.get("model_sha"):
                    block["model_sha"] = sha
        return area, sha

    area, sha = area_wide()
    spec_source.save_area(root, "auth", area)
    assert _lint_checks(root, "auth") == []
    # The next check swaps a still-matching area-wide stamp for the verdict's own.
    index = itf.model_index(root, "auth", area)
    assert itf.restamp_area_wide(area, sha, index) > 0
    assert all(b["model_sha"].startswith("cone:")
               for r in area["requirements"] for b in itf._stamped_blocks(r)
               if b.get("model_sha"))
    # Once the model moves, an area-wide stamp cannot say what it rested on.
    _edit(root / "specs" / "auth.qnt", "    idleOf(sid) > MAX_SESSION_AGE,",
          "    idleOf(sid) >= MAX_SESSION_AGE,")
    found = [f for f in _lint_checks(root, "auth") if not f.check.startswith("check-")]
    assert found and {f.check for f in found} == {"witness-unstamped", "obligation-unstamped"}


# ── A stale witness is replayed before it is re-proven ─────────────────────

record = _load("spec_record_sha", "spec-record.py")
replay = _load("witness_replay_sha", "witness_replay.py")
needs_quint = pytest.mark.skipif(shutil.which("quint") is None, reason="needs quint")


def _replay(root, area):
    area_data = spec_source.load_area(root, area)
    fm = area_data["formal_model"]
    index = itf.model_index(root, area, area_data)
    n = record.replay_stale_witnesses(
        root, area, area_data, root / "specs" / fm["probes_file"],
        root / "specs" / fm["quint_file"], index.verdict_sha, lambda r: True,
        lambda: shutil.which("quint"), 120)
    return n, area_data, index


def test_a_replay_steps_through_stepP_and_ends_on_the_violated_probe(root):
    area = spec_source.load_area(root, "auth")
    probes = (root / "specs" / "auth.probes.qnt").read_text(encoding="utf-8")
    nondets, branches = replay.step_branches(probes)
    assert set(branches) >= {"login", "logout", "tick"}
    trace = json.loads((root / "specs" / area["requirements"][1]["witness"]["trace"])
                       .read_text(encoding="utf-8"))
    run = replay.replay_run("r", trace, "witness_REQ_002", nondets, branches)
    assert run.startswith("  run r = initP.expect(")
    assert '.then(all { logout("s1"),' in run                  # stepP's own branch
    assert 'Set("s1", "s2").contains("s1")' in run              # each pick in its domain
    assert run.endswith(".expect(not(witness_REQ_002))")
    # A trace whose step names no stepP branch is not replayed at all.
    trace["states"][1]["_lastAction"] = "nosuchaction"
    with pytest.raises(replay.Unreplayable):
        replay.replay_run("r", trace, "witness_REQ_002", nondets, branches)


@needs_quint
def test_a_stale_witness_that_still_replays_is_restamped_without_apalache(root, monkeypatch):
    calls = []
    real = record.run_group
    monkeypatch.setattr(record, "run_group",
                        lambda cmd, **kw: calls.append(cmd[1]) or real(cmd, **kw))
    # A guard every trace satisfies: every witness is stale, every one still holds.
    _edit(root / "specs" / "auth.qnt", "      isActive(sid),\n      sessions'          = sessions.put(sid, LoggedOut),",
          "      isActive(sid),\n      sid != \"\",\n      sessions'          = sessions.put(sid, LoggedOut),")
    assert {f.check for f in _lint_checks(root, "auth")} >= {"witness-stale"}
    n, area, index = _replay(root, "auth")
    assert n == 5
    assert set(calls) <= {"test"}, calls          # quint test only: no verify
    for req in area["requirements"]:
        w = req.get("witness") or {}
        if w.get("status") == "witnessed":
            assert w["model_sha"] == index.verdict_sha(req["id"])
            assert w["revalidated_by"] == "replay"


@needs_quint
def test_a_stale_witness_that_no_longer_replays_stays_stale(root):
    # logout now needs an inactive session: REQ-002's trace logs out an
    # active one, so its replay fails; the others never call logout.
    _edit(root / "specs" / "auth.qnt", "      isActive(sid),\n      sessions'          = sessions.put(sid, LoggedOut),",
          "      not(isActive(sid)),\n      sessions'          = sessions.put(sid, LoggedOut),")
    n, area, index = _replay(root, "auth")
    req2 = next(r for r in area["requirements"] if r["id"] == "REQ-002")
    assert req2["witness"]["model_sha"] != index.verdict_sha("REQ-002")
    assert "revalidated_by" not in req2["witness"]
    # ... so the witness pass sends it to the checker.
    assert [c[0] for c in record.stale_witnesses(root, area, index.verdict_sha,
                                                 lambda r: True)] == ["REQ-002"]
    assert n == 4
