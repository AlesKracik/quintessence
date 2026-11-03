"""
Tests for tools/lean_gen.py — the Lean model generated from the Quint one —
and the places that rely on it: spec-record regenerating it before a build,
spec-lint noticing a stale one, and the generated models of the examples
replaying their traces.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
REPO = TOOLS.parent
EXAMPLES = REPO / "examples"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lean_backend as lb  # noqa: E402
import lean_gen  # noqa: E402

from test_lean import _check, _fake_lean, _lake, _lean_area, _lint_codes  # noqa: E402

needs_quint = pytest.mark.skipif(shutil.which("quint") is None,
                                 reason="no quint: the generator reads its typed IR")


def _gen(tmp_path, body, name="m"):
    q = tmp_path / f"{name}.qnt"
    q.write_text(f"module {name} {{\n{body}\n}}\n", encoding="utf-8")
    return lean_gen.generate(q, "Spec.M")


# ── The shipped example is what generation produces ─────────────────────────

@needs_quint
def test_the_example_model_is_what_the_generator_writes():
    """The committed model is a translation, not an edit: regenerating it
    from subscription.qnt gives the same bytes."""
    text = lean_gen.generate(EXAMPLES / "specs" / "subscription.qnt", "Spec.Subscription")
    committed = EXAMPLES / "specs" / "lean" / "Spec" / "Subscription" / "Model.lean"
    assert committed.read_text(encoding="utf-8") == text, \
        "regenerate it: tools/lean_gen.py examples/specs/subscription.qnt " \
        "--namespace Spec.Subscription --out " + str(committed.relative_to(REPO))


@needs_quint
def test_the_subscription_model_has_the_conventions_shape():
    text = lean_gen.generate(EXAMPLES / "specs" / "subscription.qnt", "Spec.Subscription")
    assert text.startswith("import Quint\n")
    assert "inductive SubStatus where\n  | Active\n  | Cancelled\n  | Expired" in text
    assert "  status : (Quint.QMap CustomerId SubStatus)" in text
    assert "  | cancel_unanswered (c : CustomerId) (timedOut : Bool)" in text
    # a def that reads state takes it; an invariant is a Bool of the state
    assert "def triesOf (s : State) (c : CustomerId) : Int :=" in text
    assert "def unacceptedCancelLeavesActive (s : State) : Bool :=" in text
    # an action whose only assignments are identities keeps the state as is
    assert "| .cancel_again c, s =>\n    if ((s.status.get c) == SubStatus.Cancelled) then\n" \
           "      some s\n" in text
    assert f"source_sha: {lean_gen.source_sha(EXAMPLES / 'specs' / 'subscription.qnt')}" in text


@needs_quint
def test_an_imported_module_is_qualified_and_its_actions_inlined():
    """auth-ui calls auth's actions inside its own: each call is inlined, so
    auth-ui's `submit_success` is auth's `login` guard and effect plus the
    UI's own, under one constructor the traces name."""
    text = lean_gen.generate(EXAMPLES / "specs" / "auth-ui.qnt", "Spec.AuthUi")
    assert "inductive auth.SessionStatus where" in text
    assert "def auth.isLocked (s : State) (uid : UserId) : Bool :=" in text
    assert "  sessions : (Quint.QMap SessionId auth.SessionStatus)" in text
    assert "  current : Screen" in text
    # the parameter named `s` cannot shadow the state
    assert "  | submit_success (u : UserId) (s_ : SessionId)" in text
    branch = text.split("| .submit_success u s_, s =>")[1].split("\n  | .")[0]
    assert "(!(auth.isLocked s u))" in branch
    assert "sessions := (s.sessions.set s_ auth.SessionStatus.Active)" in branch
    assert "current := Screen.Dashboard" in branch


@needs_quint
def test_a_layout_only_edit_regenerates_the_same_model(tmp_path):
    """Reordering declarations changes neither the generated text nor its
    source_sha: otherwise moving an action would move the Action type, and
    with it every Lean verdict's statement_sha."""
    src = (EXAMPLES / "specs" / "subscription.qnt").read_text(encoding="utf-8")
    a, b = "  action request_refund", "  action cancel_subscription"
    i, j = src.index(b), src.index(a)
    k = src.index("\n  // ---", j)
    moved = src[:i] + src[j:k] + "\n" + src[i:j] + src[k:]
    assert moved != src
    (tmp_path / "subscription.qnt").write_text(moved, encoding="utf-8")
    assert lean_gen.generate(tmp_path / "subscription.qnt", "Spec.Subscription") == \
        lean_gen.generate(EXAMPLES / "specs" / "subscription.qnt", "Spec.Subscription")


# ── What it refuses to guess ─────────────────────────────────────────────────

BASE = """  var x: int
  action init = x' = 0
"""


@needs_quint
@pytest.mark.parametrize("body,why", [
    (BASE + "  action inc: bool = { nondet k = oneOf(Set(1, 2))\n x' = x + k }\n"
            "  action step = inc", "nondet k` inside an action"),
    (BASE + "  action inc: bool = any { x' = x + 1, x' = x + 2 }\n  action step = inc",
     "action-level `any`"),
    (BASE + "  action inc: bool = x' = x + 1\n  action step = all { x > 0, inc }",
     "does more than choose among the module's actions"),
    (BASE + "  action inc: bool = x' = List(1).length()\n  action step = inc",
     "no Lean translation"),
])
def test_a_construct_it_cannot_translate_is_named(tmp_path, body, why):
    with pytest.raises(lean_gen.LeanGenError, match=None) as e:
        _gen(tmp_path, body)
    assert why in str(e.value)


@needs_quint
def test_an_invariant_it_cannot_translate_is_left_out_not_fatal(tmp_path):
    """Only what the actions and init reach must translate; a val nothing
    reaches is skipped with a comment, so the model still builds for the
    invariants that do translate."""
    text = _gen(tmp_path, BASE + "  action inc: bool = x' = x + 1\n  action step = inc\n"
                "  val odd: bool = List(1).length() > 0\n  val pos: bool = x >= 0")
    assert "def pos (s : State) : Bool :=" in text
    assert "-- not translated: odd:" in text


# ── The source fingerprint ───────────────────────────────────────────────────

def test_source_sha_covers_the_file_and_its_imports(tmp_path):
    (tmp_path / "a.qnt").write_text("module a {\n  var y: int\n}\n", encoding="utf-8")
    b = tmp_path / "b.qnt"
    b.write_text('module b {\n  import a from "./a"\n  var x: int\n}\n', encoding="utf-8")
    first = lean_gen.source_sha(b)
    assert first == lean_gen.source_sha(b)
    (tmp_path / "a.qnt").write_text("module a {\n  var y: str\n}\n", encoding="utf-8")
    assert lean_gen.source_sha(b) != first, "an imported module's edit moves it"
    assert lean_gen.header_sha(f"/-!\nsource_sha: {first}\n-/") == first
    assert lean_gen.header_sha("theorem t : True := trivial") is None


# ── spec-record regenerates before it builds ─────────────────────────────────

def _generated_area(tmp_path):
    area = _lean_area(tmp_path)
    (tmp_path / "specs" / "lean" / "Spec" / "A.lean").write_text(
        "import Spec.A.Model\nnamespace Spec.A\nend Spec.A\n", encoding="utf-8")
    return area


def test_a_proof_that_imports_its_model_gets_it_regenerated(tmp_path, monkeypatch):
    area = _generated_area(tmp_path)
    _fake_lean(monkeypatch)
    calls = []

    def fake_generate(qnt, namespace, **kw):
        calls.append((Path(qnt).name, namespace))
        return "import Quint\n-- generated\n"
    monkeypatch.setattr(lean_gen, "generate", fake_generate)
    entry, status = _check(tmp_path, area)
    assert status == "verified-lean", entry.get("note")
    assert calls == [("a.qnt", "Spec.A")]
    model = tmp_path / "specs" / "lean" / "Spec" / "A" / "Model.lean"
    assert model.read_text(encoding="utf-8") == "import Quint\n-- generated\n"


def test_a_model_that_cannot_be_generated_is_not_checkable(tmp_path, monkeypatch):
    area = _generated_area(tmp_path)
    _fake_lean(monkeypatch)

    def fail(*a, **kw):
        raise lean_gen.LeanGenError("an action-level `any` (action inc)")
    monkeypatch.setattr(lean_gen, "generate", fail)
    entry, status = _check(tmp_path, area)
    assert status == "not-checkable"
    assert "no Lean model generated from a.qnt: an action-level `any`" in entry["note"]


def test_a_hand_written_model_is_left_alone(tmp_path, monkeypatch):
    area = _lean_area(tmp_path)
    _fake_lean(monkeypatch)
    monkeypatch.setattr(lean_gen, "generate",
                        lambda *a, **kw: pytest.fail("generated a hand-written model"))
    _entry, status = _check(tmp_path, area)
    assert status == "verified-lean"


# ── spec-lint ────────────────────────────────────────────────────────────────

def test_lint_warns_a_generated_model_missing_or_stale(tmp_path):
    area = _generated_area(tmp_path)
    assert "lean-model-missing" in _lint_codes(tmp_path, area)
    model = tmp_path / "specs" / "lean" / "Spec" / "A" / "Model.lean"
    model.parent.mkdir(parents=True)
    sha = lean_gen.source_sha(tmp_path / "specs" / "a.qnt")
    model.write_text(f"import Quint\n/-!\nsource_sha: {sha}\n-/\n", encoding="utf-8")
    assert _lint_codes(tmp_path, area) == []
    qnt = tmp_path / "specs" / "a.qnt"
    qnt.write_text(qnt.read_text(encoding="utf-8") + "// a comment moves nothing\n",
                   encoding="utf-8")
    assert _lint_codes(tmp_path, area) == []
    qnt.write_text(qnt.read_text(encoding="utf-8").replace("x + 1", "x + 2"),
                   encoding="utf-8")
    assert _lint_codes(tmp_path, area) == ["lean-model-stale"]


def test_lint_scans_the_generated_model_for_forbidden_constructs(tmp_path):
    area = _generated_area(tmp_path)
    model = tmp_path / "specs" / "lean" / "Spec" / "A" / "Model.lean"
    model.parent.mkdir(parents=True)
    sha = lean_gen.source_sha(tmp_path / "specs" / "a.qnt")
    model.write_text(f"import Quint\n/-!\nsource_sha: {sha}\n-/\naxiom cheat : False\n",
                     encoding="utf-8")
    assert "lean-forbidden" in _lint_codes(tmp_path, area)


# ── The generated models agree with the Quint ones, trace for trace ─────────

@needs_quint
@pytest.mark.skipif(_lake() is None, reason="no lake: the Lean toolchain is optional")
@pytest.mark.parametrize("area,module", [("auth", "Spec.GenAuth"),
                                         ("auth-ui", "Spec.GenAuthUi")])
def test_a_generated_model_replays_its_areas_traces(tmp_path, area, module):
    """The generator's regression suite is the replay: every trace of the
    area that records its actions is a run of the generated `apply`."""
    src = EXAMPLES / "specs" / "lean"
    pdir = tmp_path / "lean"
    shutil.copytree(src, pdir, ignore=shutil.ignore_patterns(".lake"))
    if (src / ".lake").exists():  # reuse the built prelude
        shutil.copytree(src / ".lake", pdir / ".lake", symlinks=True)
    text = lean_gen.generate(EXAMPLES / "specs" / f"{area}.qnt", module)
    lb.module_file(pdir, module).write_text(text, encoding="utf-8")
    lake = _lake()
    res, detail, _ = lb.build(lake, pdir, module, 600)
    assert res == "ok", detail
    fields, params = lb.model_shape(EXAMPLES / "specs" / f"{area}.qnt")
    runs, preds = lb.collect_traces(area, EXAMPLES, [])
    lean_src, spans, _skipped = lb.replay_source(module, fields, params, runs, preds)
    assert spans, "no trace to replay"
    path = tmp_path / "Replay.lean"
    path.write_text(lean_src, encoding="utf-8")
    proc, why, _ = lb.run_file(lake, pdir, path.resolve(), 600)
    assert proc is not None, why
    seen = lb.parse_replay((proc.stdout or "") + (proc.stderr or ""), spans, runs, preds,
                           path.name)
    bad = {k: d for k, (ok, d) in seen.items() if not ok}
    assert not bad, bad
