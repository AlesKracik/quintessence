"""Model mutation and the questions it raises (tools/spec-questions.py).

Pure Python like the rest of the suite: the mutants, the ITF -> Quint
rendering, the replay runs and the questions are checked here directly, and
the orchestration runs against a fake quint whose answers are scripted per
mutant. The real quint runs it in CI over the examples.
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


sq = _load("spec_questions_t", "spec-questions.py")
sys.path.insert(0, str(TOOLS))
import spec_source  # noqa: E402
import quint_ir  # noqa: E402


SHOP = '''module shop {
  type Status = Open | Closed

  var status: Status
  var count: int

  /// @con CON-001
  /// Most items in a basket.
  pure val LIMIT: int = 2

  action init = all { status' = Open, count' = 0 }

  /// @req REQ-001
  /// @status specified
  /// @when a shopper adds an item
  /// @shall count it, up to LIMIT
  /// @via add
  action add: bool = all {
    status == Open,
    count < LIMIT,   // "never above"
    count' = count + 1,
    status' = status,
  }

  /// @req REQ-002
  /// @status specified
  /// @when the shopper closes the basket
  /// @shall close it
  /// @via close
  action close: bool = all {
    status == Open,
    status' = Closed,
    count' = count,
  }

  action step = any { add, close }

  /// @inv INV-001
  /// Never above the limit.
  val withinLimit: bool = count <= LIMIT

  /// @example EX-001
  /// Two adds
  /// @refs REQ-001
  run twoAdds = init.then(add).then(add).expect(count == 2)
}
'''


def _sites(text=SHOP, ops=sq.OPS):
    p = Path("/nonexistent/shop.qnt")
    decls = spec_source.declarations(text)
    ir = quint_ir.parse_qnt_text(text) if hasattr(quint_ir, "parse_qnt_text") else None
    if ir is None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shop.qnt"
            p.write_text(text, encoding="utf-8")
            ir = quint_ir.parse_qnt(p, engine="regex")
    return sq.mutation_sites(text, decls, ir, {}, ops)


# ── Mutants ─────────────────────────────────────────────────────────────────

def test_the_mask_blanks_comments_and_strings_and_keeps_every_offset():
    text = 'x == "a, b" // c == d\n  y < 1 /* > */ z'
    masked = sq.mask_quint(text)
    assert len(masked) == len(text) and masked.count("\n") == 1
    assert '"    "' in masked and "c == d" not in masked and ">" not in masked
    assert masked.index("y < 1") == text.index("y < 1")


def test_mutants_change_behavior_and_never_a_claim_about_it():
    sites = _sites()
    labels = {(s["op"], s["decl"], s["label"]) for s in sites}
    assert ("cmp", "add", "`count < LIMIT`: `<` -> `<=`") in labels
    assert ("guard", "add", "drop the guard `count < LIMIT`") in labels
    assert ("guard", "close", "drop the guard `status == Open`") in labels
    assert ("effect", "add", "drop the effect on `count` (keep it as it was)") in labels
    assert ("variant", "close", "`status` gets `Open` where the model writes `Closed`") in labels
    assert ("const", "LIMIT", "`LIMIT` = 3 instead of 2") in labels
    assert ("const", "LIMIT", "`LIMIT` = 1 instead of 2") in labels
    decls = {s["decl"] for s in sites}
    assert "withinLimit" not in decls, "an invariant is a claim, not behavior"
    assert "step" not in decls and "twoAdds" not in decls
    # `x' = x` is already the dropped effect; the comment's `"never above"`
    # is not code.
    assert not any(s["op"] == "effect" and s["orig"] == "status" for s in sites)
    assert all(s["op"] != "cmp" or s["decl"] != "init" for s in sites)


def test_an_action_call_is_not_a_guard():
    ui = SHOP.replace("action step = any { add, close }",
                      "action addTwice: bool = all { add, status' = status }\n"
                      "  action step = any { add, close }")
    assert not any(s["op"] == "guard" and s["decl"] == "addTwice" for s in _sites(ui))


def test_a_mutant_is_one_change_and_selection_spreads_across_operators():
    sites = _sites()
    picked = sq.select(list(sites), 5)
    assert len({s["op"] for s in picked}) == 5
    assert [s["id"] for s in picked] == ["M01", "M02", "M03", "M04", "M05"]
    m = next(s for s in sites if s["op"] == "cmp" and s["orig"] == "<")
    mutated = sq.apply(SHOP, m)
    assert "count <= LIMIT,   //" in mutated
    assert mutated.replace("count <= LIMIT,   //", "count < LIMIT,   //") == SHOP


def test_the_shipped_examples_all_yield_mutants():
    for area in ("auth", "auth-ui", "subscription"):
        text = (EXAMPLES / "specs" / f"{area}.qnt").read_text(encoding="utf-8")
        ir = quint_ir.parse_qnt(EXAMPLES / "specs" / f"{area}.qnt", engine="regex")
        sites = sq.mutation_sites(text, spec_source.declarations(text), ir,
                                  sq.imported_decls(EXAMPLES, text))
        assert {s["op"] for s in sites} >= {"guard", "effect", "variant"}, area
        # The UI signs in by calling auth's action: that call is not a guard
        # to drop, and the guard that reads auth through a helper is.
        if area == "auth-ui":
            guards = {s["label"] for s in sites if s["op"] == "guard"}
            assert "drop the guard `accountLocked(u)`" in guards
            assert not any("auth::login" in g for g in guards)


# ── ITF -> Quint, and back to a reader ──────────────────────────────────────

V = {"tag": "Active", "value": {"#tup": []}}


def test_itf_values_render_as_quint_literals():
    assert sq.literal(V) == "Active"
    assert sq.literal({"#map": [["alice", V]]}) == 'Map("alice" -> Active)'
    assert sq.literal({"#set": []}) == "Set()"
    assert sq.literal({"#bigint": "-3"}) == "-3"
    assert sq.literal({"a": 1, "b": "x"}) == '{ a: 1, b: "x" }'
    assert sq.literal({"tag": "Some", "value": 5}) == "Some(5)"
    with pytest.raises(sq.Unrenderable):
        sq.literal({"#unserializable": "f"})


def test_a_map_is_named_down_to_the_entry():
    state = {"status": {"#map": [["alice", V], ["bob", V]]}, "count": 2,
             "#meta": {"index": 1}, "mbt::actionTaken": "add"}
    got = sq.facts(state)
    assert ('status.get("alice") == Active', "status[alice] = Active") in got
    assert ("count == 2", "count = 2") in got
    assert all("mbt" not in c and "meta" not in c for c, _ in got)
    after = dict(state, count=3)
    assert sq.changed_facts(state, after) == [("count == 3", "count = 3")]


# ── Rebuilding calls from a trace, and replaying them ──────────────────────

STEPPED = SHOP.replace(
    "action step = any { add, close }",
    "action shout(who: str): bool = all { status' = status, count' = count }\n"
    "  action step = {\n    nondet w = oneOf(Set(\"ann\", \"bo\"))\n"
    "    any { add, close, shout(w) }\n  }")


def _state(status, count, action=None, picks=None):
    s = {"status": {"tag": status, "value": {"#tup": []}}, "count": count}
    if action:
        s["mbt::actionTaken"] = action
        s["mbt::nondetPicks"] = picks or {}
    return s


TRACE = {"states": [
    _state("Open", 0, "init"),
    _state("Open", 1, "add", {"w": {"tag": "Some", "value": "ann"}}),
    _state("Open", 1, "shout", {"w": {"tag": "Some", "value": "bo"}}),
    _state("Closed", 1, "close", {"w": {"tag": "None", "value": {"#tup": []}}}),
]}


def test_a_trace_becomes_calls_with_the_values_it_picked():
    nondets, templates = sq.step_templates(spec_source.declarations(STEPPED))
    assert nondets == ["w"] and templates["shout"] == "shout(w)"
    steps = sq.trace_steps(TRACE, nondets, templates)
    assert [s["call"] for s in steps] == [None, "add", 'shout("bo")', "close"]
    broken = {"states": TRACE["states"][:1] + [_state("Open", 0, "vanish")]}
    assert sq.trace_steps(broken, nondets, templates) is None


def test_replay_runs_chain_one_step_at_a_time_and_land_inside_the_module():
    nondets, templates = sq.step_templates(spec_source.declarations(STEPPED))
    steps = sq.trace_steps(TRACE, nondets, templates)
    lines = sq.replay_runs("qr", [(0, steps)])
    assert lines[0] == "  run qr_0_0_s = init.expect(count == 0 and status == Open)"
    assert lines[1] == "  run qr_0_1_a = qr_0_0_s.then(add)"
    assert lines[2].startswith("  run qr_0_1_s = qr_0_1_a.expect(count == 1")
    assert lines[3] == '  run qr_0_2_a = qr_0_1_s.then(shout("bo"))'
    text = sq.with_runs(STEPPED, lines)
    assert text.rstrip().endswith("}") and text.index("qr_0_0_s") < text.rstrip().rindex("}")
    assert spec_source.declarations(text)["qr_0_2_a"]["kind"] == "run"


def test_a_whole_trace_is_one_run_when_nothing_needs_locating():
    nondets, templates = sq.step_templates(spec_source.declarations(STEPPED))
    steps = sq.trace_steps(TRACE, nondets, templates)
    (line,) = sq.whole_runs("qr", [(4, steps)], {"count": "shop::count"})
    assert line.startswith("  run qr_4 = init.expect(shop::count == 0 and status == Open)"
                           ".then(add).expect(shop::count == 1")
    assert line.count(".then(") == 3 and line.count(".expect(") == 4


def test_an_imported_var_is_named_through_its_module():
    decls = spec_source.declarations("module ui {\n  var screen: str\n}\n")
    imported = {"auth": spec_source.declarations(
        "module auth {\n  var sessions: int\n  var screen: int\n}\n"),
                "util": {}}
    assert sq.var_names(decls, imported) == {"sessions": "auth::sessions"}
    # A variant of an imported type is named through its module too.
    imported["auth"].update(spec_source.declarations(
        "module auth {\n  type S = | Active | Gone\n}\n"))
    names = sq.var_names(decls, imported)
    assert names["tag:Active"] == "auth::Active"
    assert sq.literal({"#map": [["s1", V]]}, names) == 'Map("s1" -> auth::Active)'
    assert sq.state_expr({"sessions": {"#map": [["s1", V]]}}, names) == \
        'auth::sessions == Map("s1" -> auth::Active)'


def test_the_first_parting_is_the_earliest_failing_run_of_any_trace():
    traces = [(0, [None] * 5), (1, [None] * 5)]
    assert sq.first_divergence("qr", traces, set()) is None
    assert sq.first_divergence("qr", traces, {"qr_0_3_s", "qr_0_4_a", "qr_1_2_a"}) == (
        1, 2, "refused")
    assert sq.first_divergence("qr", traces, {"qr_0_3_s"}) == (0, 3, "state")
    assert sq.first_divergence("qr", traces, {"qr_1_0_s"}) == (1, 0, "initial")


def test_quint_test_output_is_read_per_run():
    out = ("\n  shop\n    \x1b[32mok\x1b[39m twoAdds passed 1 test(s)\n"
           "    1) qr_0_1_s failed after 1 test(s)\n    ok qr_0_0_s passed 1 test(s)\n\n"
           "  2 passing (12ms)\n  1 failed\n")
    assert sq.parse_test_output(out) == {"twoAdds": "passed", "qr_0_1_s": "failed",
                                         "qr_0_0_s": "passed"}


# ── Questions ───────────────────────────────────────────────────────────────

def test_a_survivor_reads_as_a_scenario_and_a_yes_no_question():
    q = sq.question("outcome", ["add", 'shout("bo")'], "close",
                    [("count == 1", "count = 1")], ["REQ-002"], "shop", "ab12cd34")
    assert q["given"] == "add; then shout(bo)" and q["when"] == "close"
    assert q["question"].startswith("After add; then shout(bo), when close: should "
                                    "count = 1 hold?")
    assert q["example"] == ('run q_ab12cd34 = init.then(add).then(shout("bo"))'
                            '.then(close).expect(count == 1)')
    r = sq.question("refused", ["close"], "add", [], [], "shop", "k")
    assert r["question"].startswith("After close, should add be refused?")
    assert r["example"] == "run q_k = init.then(close).then(add.fail())"
    a = sq.question("allowed", [], "add", [("count == 1", "count = 1")], [], "shop", "k")
    assert a["given"] == "the initial state" and "can add happen?" in a["question"]
    i = sq.question("initial", [], None, [("count == 0", "count = 0")], [], "shop", "k")
    assert i["example"] == "run q_k = init.expect(count == 0)"


# ── The whole run, against a scripted quint ────────────────────────────────

class FakeQuint:
    """Answers by which mutant the written file carries.

      LIMIT = 3      -> twoAdds still passes; the simulator finds INV-001 broken
      LIMIT = 1      -> twoAdds fails (killed by EX-001)
      close keeps status Open (variant swap) -> survives; the model's trace
                        replayed on it parts after `close`: state differs
      add's guard `status == Open` dropped -> survives, no replay tells it apart
    """
    script = {}

    def __init__(self, exe, cwd, timeout):
        self.cwd = Path(cwd)
        self.calls = 0

    def _which(self, file):
        text = (self.cwd / file).read_text(encoding="utf-8")
        for tag, needle in self.script.items():
            if needle(text):
                return tag
        return "model"

    def test(self, file, match):
        self.calls += 1
        which = self._which(file)
        text = (self.cwd / file).read_text(encoding="utf-8")
        names = [d for d, h in spec_source.declarations(text).items()
                 if h.get("kind") == "run" and __import__("re").match(match, d)]
        failed = set()
        if which == "killed-by-example" and "twoAdds" in names:
            failed.add("twoAdds")
        if which == "survivor" and match == "^qr_":
            # Round one: the whole trace fails; round two: from step 3 on.
            failed |= {n for n in names if n == "qr_0" or n.startswith("qr_0_3_s")}
        if which == "survivor" and match == "^qd_":
            failed |= {"qd_1"}
        res = {n: ("failed" if n in failed else "passed") for n in names}
        out = "\n".join(f"    {'1)' if s == 'failed' else 'ok'} {n} {s} 1 test(s)"
                        for n, s in res.items())
        return (1 if failed else 0), out, res

    def simulate(self, file, invariants, samples, steps, seed):
        self.calls += 1
        if self._which(file) == "killed-by-invariant":
            return "violation", "[violation] withinLimit", ["withinLimit"]
        return "ok", "[ok]", []

    def traces(self, file, samples, steps, seed, outdir):
        self.calls += 1
        return [json.loads(json.dumps(TRACE))]


def test_the_run_kills_some_mutants_and_turns_a_survivor_into_a_question(
        tmp_path, monkeypatch):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "shop.qnt").write_text(STEPPED, encoding="utf-8")
    (specs / "shop.intent.json").write_text(json.dumps(
        {"kind": "area", "area": "shop", "version": "0.1.0", "status": "formalized"}),
        encoding="utf-8")
    FakeQuint.script = {
        "killed-by-invariant": lambda t: "pure val LIMIT: int = 3" in t,
        "killed-by-example": lambda t: "pure val LIMIT: int = 1" in t,
        "survivor": lambda t: "status' = Open,\n    count' = count," in t,
    }
    monkeypatch.setattr(sq, "Quint", FakeQuint)
    monkeypatch.setattr(sq.shutil, "which", lambda name: "/usr/bin/quint")
    args = sq.argparse.Namespace(
        ops="const,variant,guard", limit=40, seed=7, steps=6, traces=3, samples=10,
        timeout=10, dry_run=False, json=False, record=True)
    report, summary = sq.run_area(tmp_path, "shop", args)
    rows = {r["label"]: r for r in report["results"]}
    assert rows["`LIMIT` = 1 instead of 2"]["killed_by"] == ["EX-001"]
    assert rows["`LIMIT` = 1 instead of 2"]["by_example"] is True
    assert rows["`LIMIT` = 3 instead of 2"]["killed_by"] == ["withinLimit"]
    survivor = rows["`status` gets `Open` where the model writes `Closed`"]
    assert survivor["outcome"] == "survived"
    q = next(q for q in summary["questions"] if q["key"] == survivor["question"])
    assert q["kind"] == "outcome" and q["when"] == "close"
    assert q["given"] == "add; then shout(bo)"
    assert q["model"] == "status = Closed", "only the entry that differs is asked about"
    assert q["refs"] == ["REQ-002"]
    assert q["example"].endswith(".then(close).expect(status == Closed)")
    assert summary["score"] is not None and 0 < summary["score"] < 1
    assert summary["killed_by_examples"] == 1


def test_a_dry_run_lists_mutants_and_needs_no_quint(monkeypatch):
    monkeypatch.setattr(sq.shutil, "which", lambda name: None)
    args = sq.argparse.Namespace(ops=None, limit=10, seed=7, steps=6, traces=3,
                                 samples=10, timeout=10, dry_run=True, json=False,
                                 record=False)
    report, summary = sq.run_area(EXAMPLES, "subscription", args)
    assert summary is None and len(report["planned"]) == 10


# ── Recorded, and read back ─────────────────────────────────────────────────

readback = _load("spec_readback_q", "spec-readback.py")

QUESTION = {"key": "ab12cd34", "kind": "outcome", "given": "cancel_unanswered(alice, true)",
            "when": "cancel_declined(alice)", "model": "unansweredTries[alice] = 0",
            "question": "After cancel_unanswered(alice, true), when cancel_declined(alice): "
                        "should unansweredTries[alice] = 0 hold? The model says so; a model "
                        "where it does not passes every example too.",
            "example": 'run q_ab12cd34 = init.then(cancel_unanswered("alice", true))'
                       '.then(cancel_declined("alice")).expect(unansweredTries.get("alice") == 0)',
            "refs": ["REQ-008"], "change": "cancel_declined: drop the effect on `unansweredTries`",
            "mutants": ["ab12cd34"]}


def _with_mutation(sha=None):
    area = spec_source.load_area(EXAMPLES, "subscription")
    area["check_results"]["mutation"] = {
        "ran_at": "2026-10-10T15:00:00+00:00",
        "model_sha": sha or readback.compute_model_sha(EXAMPLES, "subscription", area),
        "mutants": 38, "stillborn": 0, "killed_by_examples": 27, "killed_otherwise": 3,
        "survived": 1, "no_difference": 7, "score": 0.871, "questions": [QUESTION]}
    return area


def test_the_readback_asks_the_questions_and_says_how_to_answer():
    area = _with_mutation()
    lines = "\n".join(readback.questions_section(EXAMPLES, "subscription", area))
    assert "## Questions the Examples Leave Open" in lines
    assert "### ab12cd34 · [REQ-008](#req-008)" in lines
    assert "**Given** cancel_unanswered(alice, true) **When** cancel_declined(alice)" in lines
    assert "> After cancel_unanswered(alice, true)" in lines
    assert "```quint\nrun q_ab12cd34 = init" in lines
    assert "older model" not in lines
    assert readback.mutation_cell(area) == "87% pinned by examples, 1 question(s) open"
    items = readback.attention_items(EXAMPLES, "subscription", area)
    assert any(i.startswith("**The examples leave 1 behavior(s) open**") for i in items)
    req = next(r for r in area["requirements"] if r["id"] == "REQ-008")
    card = "\n".join(readback.render_requirement(
        EXAMPLES, area, req, area.get("constraints") or [], set()))
    assert "**Left open** · [ab12cd34](#ab12cd34) cancel_declined(alice)" in card


def test_questions_raised_against_another_model_say_so():
    area = _with_mutation(sha="0" * 64)
    lines = "\n".join(readback.questions_section(EXAMPLES, "subscription", area))
    assert "raised against an older model" in lines
    assert any("older model" in i for i in readback.attention_items(
        EXAMPLES, "subscription", area))


def test_a_recorded_run_fits_the_records_schema(tmp_path, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "shop.qnt").write_text(STEPPED, encoding="utf-8")
    (specs / "shop.intent.json").write_text(json.dumps(
        {"kind": "area", "area": "shop", "version": "0.1.0", "status": "formalized"}),
        encoding="utf-8")
    FakeQuint.script = {
        "killed-by-example": lambda t: "pure val LIMIT: int = 1" in t,
        "survivor": lambda t: "status' = Open,\n    count' = count," in t,
    }
    monkeypatch.setattr(sq, "Quint", FakeQuint)
    monkeypatch.setattr(sq.shutil, "which", lambda name: "/usr/bin/quint")
    summary_md = tmp_path / "summary.md"
    assert sq.main(["shop", "--root", str(tmp_path), "--record", "--ops", "const,variant",
                    "--summary", str(summary_md)]) == 0
    md = summary_md.read_text(encoding="utf-8")
    assert md.startswith("### shop — ") and "```quint\nrun q_" in md
    records = json.loads((specs / "shop.records.json").read_text(encoding="utf-8"))
    mutation = records["check_results"]["mutation"]
    assert mutation["questions"] and mutation["killed_by_examples"] == 1
    schema = json.loads((TOOLS.parent / "schemas" / "records.schema.json").read_text())
    jsonschema.validate(records, schema)


class ShrinkQuint:
    """The difference needs `add` before `close`; nothing else matters."""

    def __init__(self, cwd):
        self.cwd = Path(cwd)

    def test(self, file, match):
        text = (self.cwd / file).read_text(encoding="utf-8")
        mutant = "MUTANT" in text
        res = {}
        for name, h in spec_source.declarations(text).items():
            if h.get("kind") != "run" or not name.startswith("qs_"):
                continue
            body = h.get("body") or ""
            failed = mutant and not name.endswith("_x") and ".then(add)" in body
            res[name] = "failed" if failed else "passed"
        return (1 if "failed" in res.values() else 0), "", res


def test_a_scenario_keeps_only_the_calls_the_difference_needs(tmp_path):
    q = ShrinkQuint(tmp_path)

    def write(text, name=None):
        (tmp_path / (name or "shop.qnt")).write_text(text, encoding="utf-8")
        return name or "shop.qnt"
    ctx = {"text": STEPPED}
    mutant = STEPPED.replace("module shop {", "module shop {\n  // MUTANT")
    got = sq.shrink(ctx, q, write, mutant, "outcome",
                    ['shout("ann")', "add", 'shout("bo")'], "close", "count == 1")
    assert got == ["add"]
    assert sq.shrink(ctx, q, write, mutant, "initial", ["add"], None, "") == ["add"]
