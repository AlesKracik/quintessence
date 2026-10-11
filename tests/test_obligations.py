"""Every requirement compiles to two checks: an OBLIGATION (every reachable
step of its action from @while ends in @shall) and a WITNESS (such a step
can happen). These tests hold the grammar, the derived form, the lint gates
and the discharge rule to that, and — when Apalache is available — show the
obligation catching a model that a witness alone passes."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import spec_source  # noqa: E402
import quint_ir  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lint = _load("spec_lint_oblig", "spec-lint.py")
probes = _load("spec_probes_oblig", "spec-probes.py")
itf = _load("itf_tools_oblig", "itf_tools.py")
readback = _load("spec_readback_oblig", "spec-readback.py")

COUNTER = """module c {
  type UserId = str
  var attempts: UserId -> int
  var locked: UserId -> bool
  pure val MAX: int = 3
  def attemptsOf(u: UserId): int = if (attempts.keys().contains(u)) attempts.get(u) else 0
  def isLocked(u: UserId): bool = locked.keys().contains(u) and locked.get(u)

  /// @req REQ-001
  /// When a sign-in fails, the system shall count it and lock the account
  /// once the count reaches MAX.
  /// @status specified
  /// @when a sign-in fails
  /// @while not(isLocked(uid))
  /// @old before: int = attemptsOf(uid)
  /// @shall attemptsOf(uid) == before + 1 and (isLocked(uid) iff attemptsOf(uid) >= MAX)
  ///
  /// @req REQ-002
  /// While the account is locked, the system shall refuse failures.
  /// @status specified
  /// @modality forbidden
  /// @while isLocked(uid)
  action fail_login(uid: UserId): bool = all {
    not(isLocked(uid)),
    val n = attemptsOf(uid) + 1
    all { attempts' = attempts.put(uid, n), locked' = locked.put(uid, n >= MAX) }
  }

  /// @req REQ-003
  /// The lock threshold is MAX.
  /// @status specified
  /// @verified-by tests/threshold.test.ts
  pure def threshold(n: int): bool = n >= MAX

  action reset(uid: UserId): bool = all {
    attempts' = attempts.put(uid, 0), locked' = locked.put(uid, false) }

  action init = all { attempts' = Map(), locked' = Map() }
  action step = { nondet u = oneOf(Set("a")) any { fail_login(u), reset(u) } }
}
"""


def _derive(text=COUNTER):
    problems = []
    area, _info = spec_source.derive_records(text, "c.qnt", problems=problems)
    assert not problems, problems
    return {r["id"]: r for r in area["requirements"]}


# ── grammar and form ────────────────────────────────────────────────────────

def test_where_a_requirement_sits_decides_its_form():
    reqs = _derive()
    assert reqs["REQ-001"]["form"] == "event" and reqs["REQ-001"]["quint_ref"] == "fail_login"
    assert reqs["REQ-002"]["form"] == "event" and reqs["REQ-002"]["modality"] == "forbidden"
    assert reqs["REQ-003"]["form"] == "computed"
    assert reqs["REQ-001"]["old"] == [{"name": "before", "type": "int",
                                       "expr": "attemptsOf(uid)"}]
    assert reqs["REQ-001"]["ears"]["state"] == "not(isLocked(uid))"


def test_a_raw_requirement_has_no_form_and_owes_nothing_yet():
    reqs = _derive("/// @req REQ-009\n/// @shall lock it\nmodule m { }\n")
    assert "form" not in reqs["REQ-009"]
    assert not itf.owes_obligation(reqs["REQ-009"])


def test_a_formal_requirement_on_the_module_is_a_state_requirement():
    reqs = _derive("/// @req REQ-009\n/// @status specified\n/// @shall x >= 0\n"
                   "module m {\n  var x: int\n}\n")
    assert reqs["REQ-009"]["form"] == "state"
    assert itf.owes_obligation(reqs["REQ-009"])
    assert itf.witness_exempt(reqs["REQ-009"]), "no @while: an invariant, nothing to reach"


def test_the_witness_is_exempt_only_by_what_the_requirement_is():
    reqs = _derive()
    assert itf.witness_exempt(reqs["REQ-001"]) is None
    assert "forbidden" in itf.witness_exempt(reqs["REQ-002"])
    assert "tests/threshold.test.ts" in itf.witness_exempt(reqs["REQ-003"])


# ── generation ──────────────────────────────────────────────────────────────

def _probe_module(tmp_path, text=COUNTER):
    (tmp_path / "c.qnt").write_text(text, encoding="utf-8")
    ir = quint_ir.parse_qnt(tmp_path / "c.qnt")
    area, _info = spec_source.derive_records(text, "c.qnt")
    area["formal_model"] = {"quint_file": "c.qnt",
                            "probe_domains": {"UserId": 'Set("a")'}}
    return "\n".join(probes.build(area, "c", ir, "c.qnt"))


def test_old_and_while_are_read_before_the_step(tmp_path):
    out = _probe_module(tmp_path)
    assert "_pre_REQ_001' = pre_REQ_001(uid)" in out
    assert "_old_REQ_001_before' = old_REQ_001_before(uid)" in out
    assert "def post_REQ_001(uid: UserId, before: int): bool" in out
    assert "post_REQ_001(_lastUid, _old_REQ_001_before)" in out


def test_a_prohibition_is_an_obligation_that_its_step_never_starts(tmp_path):
    out = _probe_module(tmp_path)
    assert "val oblig_REQ_002: bool =" in out
    assert 'not(_lastAction == "fail_login" and _pre_REQ_002)' in out
    assert "witness_REQ_002" not in out


@pytest.mark.skipif(not quint_ir.cli_available(), reason="quint CLI not installed")
def test_the_generated_module_typechecks(tmp_path):
    (tmp_path / "c.probes.qnt").write_text(_probe_module(tmp_path) + "\n", encoding="utf-8")
    r = subprocess.run(["quint", "typecheck", str(tmp_path / "c.probes.qnt")],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr


# ── lint ────────────────────────────────────────────────────────────────────

def _form_findings(req):
    findings = []
    lint.check_requirement_form({"requirements": [req]}, "x", findings)
    return {f.check for f in findings}


def test_authored_text_never_names_a_probe_ghost():
    req = {"id": "REQ-001", "status": "specified", "form": "event",
           "quint_ref": "f", "ears": {"response": "n == _prevN + 1"}}
    assert "requirement-reads-ghost" in _form_findings(req)


def test_past_raw_a_requirement_must_be_formal():
    req = {"id": "REQ-001", "status": "specified", "ears": {"trigger": "asked"}}
    assert "requirement-not-formal" in _form_findings(req)
    req["status"] = "raw"
    assert "requirement-not-formal" not in _form_findings(req)


def test_a_prohibition_does_not_say_what_its_step_does():
    req = {"id": "REQ-002", "status": "specified", "form": "event", "quint_ref": "f",
           "modality": "forbidden", "ears": {"state": "x > 0", "response": "x == 0"}}
    assert "forbidden-with-response" in _form_findings(req)


def test_old_needs_a_step_to_read_before():
    req = {"id": "REQ-009", "status": "specified", "form": "state",
           "ears": {"response": "x >= b"}, "old": [{"name": "b", "type": "int", "expr": "x"}]}
    assert "old-without-step" in _form_findings(req)


def test_a_shall_must_say_something_about_state(tmp_path):
    (tmp_path / "m.qnt").write_text("module m {\n  var x: int\n  def two: int = 2\n}\n",
                                    encoding="utf-8")
    sidecar = lint.parse_sidecar(tmp_path / "m.qnt")
    decls = spec_source.declarations((tmp_path / "m.qnt").read_text(encoding="utf-8"))

    def found(shall):
        area = {"requirements": [{"id": "REQ-001", "status": "specified",
                                  "ears": {"response": shall}}]}
        findings = []
        lint.check_shall_sanity(area, sidecar, {}, decls, "m", findings)
        return {f.check for f in findings}

    assert found("(true)") == {"shall-constant"}
    assert found("two == 2") == {"shall-no-state"}
    assert found("x == two") == set()


def test_a_violated_obligation_fails_at_any_status(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.qnt").write_text("module a { var x: int }", encoding="utf-8")
    area = {"area": "a", "status": "formalized", "formal_model": {"quint_file": "a.qnt"},
            "requirements": [{"id": "REQ-001", "status": "specified", "form": "event",
                              "quint_ref": "f", "ears": {"response": "x > 0"},
                              "obligation": {"formal_status": "counterexample-found",
                                             "trace": "a/traces/REQ-001.violation.itf.json"}}]}
    findings = []
    lint.check_witnesses(tmp_path, area, "a", findings)
    assert "obligation-violated" in {f.check for f in findings}


# ── discharge ───────────────────────────────────────────────────────────────

def _discharge_area(tmp_path, obligation):
    (tmp_path / "specs" / "a" / "traces").mkdir(parents=True)
    (tmp_path / "specs" / "a.qnt").write_text("module a { var x: int }", encoding="utf-8")
    (tmp_path / "specs" / "a" / "traces" / "REQ-001.itf.json").write_text(
        json.dumps({"vars": ["x"], "states": [{"x": 0}, {"x": 1}]}), encoding="utf-8")
    area = {"area": "a", "formal_model": {"quint_file": "a.qnt"},
            "requirements": [{"id": "REQ-001", "status": "specified", "form": "event",
                              "quint_ref": "f", "ears": {"response": "x > 0"},
                              "witness": {"status": "witnessed",
                                          "trace": "a/traces/REQ-001.itf.json"}}]}
    sha = itf.compute_model_sha(tmp_path, "a", area)
    area["requirements"][0]["witness"]["model_sha"] = sha
    if obligation is not None:
        area["requirements"][0]["obligation"] = dict(obligation, model_sha=sha)
    return area


@pytest.mark.parametrize("obligation,status", [
    (None, "OBLIGATION-NOT-RUN"),
    ({"formal_status": "counterexample-found"}, "VIOLATED"),
    ({"formal_status": "verified", "steps": 4}, "witnessed"),
])
def test_a_witness_alone_does_not_discharge(tmp_path, obligation, status):
    area = _discharge_area(tmp_path, obligation)
    rows, missing, discharged = itf.witness_status(tmp_path, "a", area)
    assert rows[0][1] == status
    assert discharged == (1 if status == "witnessed" else 0)
    assert readback.req_is_discharged(area["requirements"][0]) == (status == "witnessed")


def test_a_stale_obligation_does_not_discharge(tmp_path):
    area = _discharge_area(tmp_path, {"formal_status": "verified"})
    area["requirements"][0]["obligation"]["model_sha"] = "0" * 64
    rows, missing, _ok = itf.witness_status(tmp_path, "a", area)
    assert rows[0][1] == "STALE" and missing == 1


# ── the point of it, end to end ─────────────────────────────────────────────
# Opt-in: Apalache needs a JVM and a first-run download.

@pytest.mark.skipif(not (shutil.which("quint") and os.environ.get("QUINTESSENCE_APALACHE")),
                    reason="set QUINTESSENCE_APALACHE=1 with quint + Apalache installed")
def test_the_obligation_catches_what_the_witness_cannot(tmp_path):
    """A counter that sticks on its second failure: the first failure is
    counted (so the witness is found, and an existential check passes),
    the second is not — only the every-step check sees it."""
    sticky = COUNTER.replace("val n = attemptsOf(uid) + 1",
                             "val n = if (attemptsOf(uid) == 1) 1 else attemptsOf(uid) + 1")
    assert sticky != COUNTER

    def verify(text, name):
        (tmp_path / "c.qnt").write_text(text, encoding="utf-8")
        (tmp_path / "c.probes.qnt").write_text(_probe_module(tmp_path, text) + "\n",
                                               encoding="utf-8")
        r = subprocess.run(["quint", "verify", "--max-steps=4", f"--invariant={name}",
                            "--init=initP", "--step=stepP", str(tmp_path / "c.probes.qnt")],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=600)
        return r.returncode

    assert verify(COUNTER, "oblig_REQ_001") == 0
    assert verify(sticky, "witness_REQ_001") != 0, "a failure is still counted once"
    assert verify(sticky, "oblig_REQ_001") != 0, "but not every failure is"
