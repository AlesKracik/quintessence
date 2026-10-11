"""Tests for the foundations of the triangulated design.

  - the readback leads with the rendered EARS sentence;
  - encoding checks: a claim the model does not encode is reported (a cited
    constant its action never reads, an unread constant, an error outcome the
    model never produces, a `spans` without an import);
  - honest `may`: each outcome's probe requires the others to be false;
  - examples as Given / When / Then rows derived from their runs, refusals
    shown with `.fail()`;
  - executable journeys: a run that walks the steps in order, example rows
    at each step, and the journey-first readback.

Pure Python, like the rest of the suite: no quint/Apalache/Java needed.
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


lint = _load("spec_lint_f", "spec-lint.py")
readback = _load("spec_readback_f", "spec-readback.py")
probes = _load("spec_probes_f", "spec-probes.py")
quint_ir = _load("quint_ir_f", "quint_ir.py")
sys.path.insert(0, str(TOOLS))
import spec_source  # noqa: E402


SHOP = '''module shop {
  var stock: int
  var orders: int

  /// @con CON-001
  /// Per-customer order limit.
  pure val LIMIT: int = 3

  /// @con CON-002
  /// A number nothing reads.
  pure val UNUSED: int = 9

  def underLimit(n: int): bool = n < LIMIT

  action init = all { stock' = 5, orders' = 0 }

  /// @req REQ-001
  /// @status specified
  /// @when a customer orders
  /// @shall take one item, at most LIMIT per customer
  action order: bool = all { underLimit(orders), stock' = stock - 1, orders' = orders + 1 }

  /// @req REQ-002
  /// @status specified
  /// @when stock is counted
  /// @shall restock up to LIMIT
  action restock: bool = all { stock' = 5, orders' = orders }

  /// @req REQ-003
  /// @status specified
  /// @modality forbidden
  /// @when a customer orders past the limit
  /// @unwanted
  /// @shall refuse the order
  /// @enforced-by INV-001
  action order_again: bool = all { underLimit(orders), stock' = stock, orders' = orders }

  /// @inv INV-001
  /// Orders never exceed the limit.
  val withinLimit: bool = orders <= LIMIT

  action step = any { order, restock }

  /// @example EX-001
  /// One order
  /// @refs REQ-001
  run oneOrder = init.then(order).expect(orders == 1)

  /// @example EX-002
  /// Restock after two orders
  /// @refs REQ-002
  run restockAfter = oneOrder.then(order).then(restock).expect(stock == 5)

  /// @example EX-003
  /// The fourth order is refused
  /// @refs REQ-003
  run fourthRefused = init
    .then(order)
    .then(order)
    .then(order)
    .expect(orders == 3)
    .then(order_again.fail())

  run journeyRun = init.then(order).then(restock).expect(stock == 5)
  run backwards = init.then(restock).then(order).expect(orders == 1)
}
'''


def _project(tmp_path, model=SHOP, status="formalized", journeys=()):
    specs = tmp_path / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "shop.qnt").write_text(model, encoding="utf-8")
    (specs / "shop.intent.json").write_text(json.dumps(
        {"kind": "area", "area": "shop", "version": "0.1.0", "status": status}),
        encoding="utf-8")
    if journeys:
        (specs / "journeys").mkdir(exist_ok=True)
        for i, j in enumerate(journeys):
            (specs / "journeys" / f"j{i}.journey.json").write_text(json.dumps(j),
                                                                   encoding="utf-8")
    return spec_source.load_area(tmp_path, "shop")


def _codes(findings):
    return [(f.severity, f.check) for f in findings]


# ── What a reviewer reads ───────────────────────────────────────────────────

def test_the_readback_leads_with_the_rendered_ears_sentence():
    req = {"id": "REQ-001", "ears": {"trigger": "a customer orders",
                                     "response": "take at most LIMIT items"}}
    cons = [{"id": "CON-001", "name": "LIMIT", "value": 3}]
    assert readback.req_sentence(req, cons) == (
        "When a customer orders, the system shall take at most LIMIT "
        "(= 3, CON-001) items.")


# ── Declarations, closures and runs ─────────────────────────────────────────

def test_declarations_are_module_members_not_action_locals():
    d = spec_source.declarations(
        "module m {\n  var x: int\n  action a: bool = {\n    val n = x + 1\n"
        "    x' = n\n  }\n  run r = init.then(a)\n}\n")
    assert d["x"]["kind"] == "var" and d["a"]["kind"] == "action"
    assert d["r"]["kind"] == "run" and "n" not in d


def test_name_closure_reaches_through_helpers_but_not_strings_or_params():
    d = spec_source.declarations(SHOP)
    reach = spec_source.name_closure(d, ["order"])
    assert "LIMIT" in reach and "underLimit" in reach      # through the helper
    assert "LIMIT" not in spec_source.name_closure(d, ["restock"])
    d2 = spec_source.declarations('module m {\n  pure val K: int = 1\n'
                                  '  def f(K: int): bool = K > 0\n'
                                  '  action a: bool = f(2) and "K" == "K"\n}\n')
    # K reaches `a` only as a string and as f's own parameter: neither reads
    # the constant.
    assert "K" not in spec_source.name_closure(d2, ["a"])


def test_run_calls_read_steps_in_order_with_nested_runs_and_refusals():
    d = spec_source.declarations(SHOP)
    steps = spec_source.run_calls(d, "restockAfter")
    assert [s["action"] for s in steps] == ["order", "order", "restock"]
    refused = spec_source.run_calls(d, "fourthRefused")
    assert [s["fails"] for s in refused] == [False, False, False, True]
    assert refused[-1]["action"] == "order_again"


def test_run_calls_keep_arguments_and_qualifiers():
    d = spec_source.declarations(
        'module m {\n  action go(u: str): bool = true\n'
        '  run r = init.then(go("alice")).then(auth::login("bob", "s1"))'
        '.expect(seen("go"))\n}\n')
    steps = spec_source.run_calls(d, "r")
    assert steps[0] == {"action": "go", "args": '"alice"', "qualifier": None,
                        "fails": False, "parens": True}
    assert steps[1]["qualifier"] == "auth" and steps[1]["action"] == "login"
    assert len(steps) == 2, "the .expect condition is the claim, not a step"


def test_an_example_row_is_derived_from_its_run():
    d = spec_source.declarations(SHOP)
    row = spec_source.example_row({"quint_run": "fourthRefused"}, d)
    assert row == {"given": "order × 3, so `orders == 3`", "when": "order_again — refused",
                   "then": "refused — nothing changes", "then_is_code": False}
    assert spec_source.example_row({"quint_run": "oneOrder"}, d)["given"] == \
        "the initial state"


def test_an_action_hosted_example_row_reads_its_json():
    row = spec_source.example_row({"given": {"status": "Active"},
                                   "when": {"action": "cancel", "args": {"c": "alice"}},
                                   "expect": {"status": "Cancelled"}}, {})
    assert row == {"given": "status = Active", "when": "cancel(c = alice)",
                   "then": "status = Cancelled", "then_is_code": False}


def test_a_run_example_with_zero_argument_actions_gets_its_action():
    area, _ = spec_source.derive_records(SHOP, "shop.qnt")
    ex = {e["id"]: e for e in area["examples"]}
    assert ex["EX-001"]["when"] == {"action": "order"}
    assert ex["EX-003"]["when"] == {"action": "order_again"}
    # Ending in a refused call is the run's claim, so it asserts something.
    assert ex["EX-003"]["expect"] == {"run.refuses": "order_again — refused"}


# ── Encoding checks ─────────────────────────────────────────────────────────

def test_a_constant_the_sentence_cites_must_be_read_by_its_action(tmp_path):
    area = _project(tmp_path)
    findings = []
    lint.check_cited_constants(area, spec_source.declarations(SHOP), "shop", findings)
    by = {(f.ref, f.check) for f in findings}
    assert ("REQ-002", "constant-cited-not-read") in by      # restock never reads LIMIT
    assert ("REQ-001", "constant-cited-not-read") not in by  # order reads it via a helper
    assert all(f.severity == lint.WARN for f in findings)


def test_the_cited_constant_check_fails_at_review(tmp_path):
    area = _project(tmp_path, status="in-review")
    findings = []
    lint.check_cited_constants(area, spec_source.declarations(SHOP), "shop", findings)
    assert (lint.FAIL, "constant-cited-not-read") in _codes(findings)


def test_a_constant_nothing_reads_is_reported(tmp_path):
    area = _project(tmp_path)
    findings = []
    lint.check_unread_constants(area, spec_source.declarations(SHOP), "shop", findings)
    assert [(f.ref, f.check) for f in findings] == [("CON-002", "constant-unread")]


PAY = '''module pay {
  type Answer = Ok | Declined | Timeout
  var last: Answer
  action init = all { last' = Ok }
  /// @req REQ-001
  /// @when the provider answers
  /// @shall record the answer
  /// @error Provider.DECLINED: NO_CHANGE
  /// @error Provider.TIMEOUT: NO_CHANGE
  /// @error Provider.NETWORK_ERROR: NO_CHANGE
  action answer: bool = all { last' = Declined }
  action step = answer
}
'''


def _sidecar(tmp_path, text, name="m.qnt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return quint_ir.parse_qnt(p, engine="regex")


def test_an_error_outcome_must_be_produced_by_the_model(tmp_path):
    area, _ = spec_source.derive_records(PAY, "pay.qnt")
    area.update({"status": "formalized"})
    findings = []
    lint.check_error_outcomes_produced(area, _sidecar(tmp_path, PAY), "pay", findings)
    got = {f.check: f.description for f in findings}
    assert set(got) == {"error-outcome-not-produced", "error-outcome-not-modeled"}
    assert "Timeout" in got["error-outcome-not-produced"]
    assert "NETWORK_ERROR" in got["error-outcome-not-modeled"]


def test_spans_without_an_import_is_reported_and_an_import_clears_it(tmp_path):
    area = {"kind": "area", "status": "formalized", "spans": ["auth"]}
    bare = _sidecar(tmp_path, "module ui {\n  var s: int\n  action a: bool = s' = 1\n}\n")
    findings = []
    lint.check_spans_imported(area, bare, {}, "ui", findings)
    assert _codes(findings) == [(lint.WARN, "spans-without-import")]
    imported = _sidecar(tmp_path, 'module ui {\n  import auth from "./auth"\n'
                        '  var s: int\n  action a: bool = s\' = 1\n}\n', "ui2.qnt")
    findings = []
    lint.check_spans_imported(area, imported, {}, "ui", findings)
    assert findings == []
    # The lint's own sidecar keeps imports as bare module names — the shape
    # the check meets when spec-lint actually runs.
    findings = []
    lint.check_spans_imported(area, lint.parse_sidecar(tmp_path / "ui2.qnt"), {}, "ui",
                              findings)
    assert findings == []
    findings = []
    lint.check_spans_imported(dict(area, kind="contract"), bare, {}, "ui", findings)
    assert findings == [], "a contract's imports are check_contract_spans' business"


def test_may_outcomes_with_the_same_predicate_fail():
    area = {"status": "formalized", "requirements": [{
        "id": "REQ-007", "modality": "may", "status": "specified",
        "witness": {"outcomes": [{"name": "email", "predicate": "done"},
                                 {"name": "in-app", "predicate": " done "}]}}]}
    findings = []
    lint.check_modality(area, "x", findings)
    assert (lint.FAIL, "may-outcomes-identical") in _codes(findings)


def _val_body(text, name):
    """The lines of `val <name>` in a generated probe module, up to the next
    blank line."""
    lines = text.splitlines()
    i = next(i for i, l in enumerate(lines) if l.strip().startswith(f"val {name}:"))
    out = []
    for l in lines[i + 1:]:
        if not l.strip():
            break
        out.append(l.strip())
    return out


def test_each_may_outcome_probe_requires_the_others_false():
    area = spec_source.load_area(EXAMPLES, "subscription")
    req = next(r for r in area["requirements"] if r["id"] == "REQ-007")
    assert [n for _r, n, _o in probes.probe_requirements({"requirements": [req]})] == \
        ["witness_REQ_007_email", "witness_REQ_007_in_app"]
    text = (EXAMPLES / "specs" / "subscription.probes.qnt").read_text(encoding="utf-8")
    email = " ".join(_val_body(text, "witness_REQ_007_email"))
    assert "and outcome_REQ_007_email(" in email
    assert "and not(outcome_REQ_007_in_app(" in email
    # ...and the obligation: every step ends in SOME permitted outcome.
    oblig = " ".join(_val_body(text, "oblig_REQ_007"))
    assert "outcome_REQ_007_email(" in oblig and " or outcome_REQ_007_in_app(" in oblig


# ── Refusals shown by examples ──────────────────────────────────────────────

def test_a_shipped_refusal_is_shown_by_an_example():
    area = spec_source.load_area(EXAMPLES, "auth")
    findings = []
    lint.check_refusal_examples(area, "auth", findings)
    assert findings == []
    decls = spec_source.model_declarations(EXAMPLES, "auth", area)
    ex = next(e for e in area["examples"] if "REQ-005" in (e.get("refs") or []))
    assert spec_source.example_row(ex, decls)["when"] == "login(bob, s2) — refused"


# ── Executable journeys ─────────────────────────────────────────────────────

def _journey(**kw):
    j = {"name": "Buy", "steps": [{"ref": "shop.REQ-001"}, {"ref": "shop.REQ-002"}]}
    j.update(kw)
    return j


def _journey_findings(tmp_path, journey, status="formalized"):
    area = _project(tmp_path, status=status, journeys=[journey])
    findings = []
    lint.check_journeys(tmp_path, {"shop": area}, findings)
    return findings


def test_a_journey_run_that_walks_the_steps_in_order_is_clean(tmp_path):
    assert _journey_findings(tmp_path, _journey(run="shop.journeyRun")) == []


def test_a_journey_run_out_of_order_fails(tmp_path):
    findings = _journey_findings(tmp_path, _journey(run="shop.backwards"))
    assert _codes(findings) == [(lint.FAIL, "journey-run-order")]
    assert "step 2" in findings[0].description and "`restock`" in findings[0].description


def test_a_journey_run_that_does_not_exist_fails(tmp_path):
    findings = _journey_findings(tmp_path, _journey(run="shop.nope"))
    assert _codes(findings) == [(lint.FAIL, "journey-run-dangling")]


@pytest.mark.parametrize("status,severity", [("formalized", "WARN"), ("in-review", "FAIL")])
def test_a_journey_without_a_run_is_not_executable(tmp_path, status, severity):
    findings = _journey_findings(tmp_path, _journey(), status)
    assert _codes(findings) == [(getattr(lint, severity), "journey-not-executable")]


def test_step_examples_must_resolve_and_fit_the_step(tmp_path):
    j = _journey(run="shop.journeyRun", steps=[
        {"ref": "shop.REQ-001", "examples": ["shop.EX-002"]},
        {"ref": "shop.REQ-002", "examples": ["shop.EX-099"]}])
    codes = _codes(_journey_findings(tmp_path, j))
    assert (lint.WARN, "journey-example-off-step") in codes      # EX-002 ends in restock
    assert (lint.FAIL, "journey-example-dangling") in codes


def test_a_step_with_no_example_rows_is_reported(tmp_path):
    model = SHOP.replace("  /// @refs REQ-002\n", "")
    area = _project(tmp_path, model=model, journeys=[_journey(run="shop.journeyRun")])
    findings = []
    lint.check_journeys(tmp_path, {"shop": area}, findings)
    assert [(f.ref, f.check) for f in findings] == [
        ("shop.REQ-002", "journey-step-without-example")]


def test_the_journey_schema_accepts_a_run_and_step_examples():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((TOOLS.parent / "schemas" / "journey.schema.json").read_text())
    jsonschema.validate(_journey(run="shop.journeyRun", steps=[
        {"ref": "shop.REQ-001", "examples": ["shop.EX-001"]}]), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_journey(run="journeyRun"), schema)


def _shop_run(steps, run="journeyRun", model=SHOP, extra_reqs=()):
    views = {"shop": {"requirements": [
        {"id": "REQ-001", "quint_ref": "order"},
        {"id": "REQ-002", "quint_ref": "restock"}, *extra_reqs]}}
    decls = spec_source.declarations(model)
    return spec_source.journey_run(
        {"run": f"shop.{run}", "steps": [{"ref": f"shop.{s}"} for s in steps]},
        views, lambda a: decls)


def test_one_call_covers_consecutive_steps_it_realizes():
    """A cancel that stops billing and keeps access is one call and two
    requirements; the journey lists both, the run makes the call once."""
    res = _shop_run(["REQ-001", "REQ-004", "REQ-002"],
                    extra_reqs=[{"id": "REQ-004", "quint_ref": "order"}])
    assert res["order_error"] is None
    assert res["covered"] == ["shop.REQ-001", "shop.REQ-004", "shop.REQ-002"]


def test_the_same_step_twice_needs_two_calls():
    assert _shop_run(["REQ-001", "REQ-001", "REQ-002"])["order_error"] == (
        2, "shop.REQ-001", "order")
    model = SHOP.replace("run journeyRun = init.then(order)",
                         "run journeyRun = init.then(order).then(order)")
    assert _shop_run(["REQ-001", "REQ-001", "REQ-002"], model=model)["order_error"] is None


def test_a_step_in_an_imported_model_is_covered_by_the_call_that_performs_it():
    ui = '''module ui {
  import shop from "./shop"
  var screen: str
  action init = all { shop::init, screen' = "home" }
  action buy = all { screen == "home", shop::order, screen' = "done" }
  action browse = all { shop::stock' = shop::stock, shop::orders' = shop::orders, screen' = "list" }
  run buyRun = init.then(browse).then(buy).expect(screen == "done")
}
'''
    decls = spec_source.declarations(ui)
    shop = spec_source.declarations(SHOP)
    resolve = {"shop": shop}.get
    assert ("shop", "order") in spec_source.action_reach(decls, "buy", resolve)
    assert spec_source.action_reach(decls, "browse", resolve) == {(None, "browse")}, (
        "a primed name is an assignment and a read of a var is not a call")
    views = {"shop": {"requirements": [{"id": "REQ-001", "quint_ref": "order"}]},
             "ui": {"requirements": [{"id": "UI-001", "quint_ref": "buy"}]}}
    res = spec_source.journey_run(
        {"run": "ui.buyRun", "steps": [{"ref": "shop.REQ-001"}, {"ref": "ui.UI-001"}]},
        views, {"ui": decls, "shop": shop}.get)
    assert res["order_error"] is None and res["outside"] == []
    assert res["covered"] == ["shop.REQ-001", "ui.UI-001"]


def test_the_shipped_cancel_journey_runs_every_step():
    views = {"subscription": spec_source.load_area(EXAMPLES, "subscription")}
    journey = json.loads((EXAMPLES / "specs" / "journeys" /
                          "cancel-subscription.journey.json").read_text(encoding="utf-8"))
    res = spec_source.journey_run(
        journey, views,
        lambda a: spec_source.model_declarations(EXAMPLES, a, views[a]))
    assert res["found"] and res["order_error"] is None and res["outside"] == []
    assert res["covered"] == [s["ref"] for s in journey["steps"]]


def test_the_shipped_sign_in_journey_runs_over_its_auth_steps():
    views = {a: spec_source.load_area(EXAMPLES, a) for a in ("auth", "auth-ui")}
    journey = json.loads((EXAMPLES / "specs" / "journeys" / "sign-in.journey.json")
                         .read_text(encoding="utf-8"))
    res = spec_source.journey_run(
        journey, views, lambda a: spec_source.model_declarations(EXAMPLES, a, views[a]))
    assert res["found"] and res["order_error"] is None
    # The run is the UI's, and each UI step calls the auth action it causes,
    # so the one run covers both areas' steps.
    assert res["outside"] == []
    assert res["covered"] == [s["ref"] for s in journey["steps"]]


def test_the_shipped_examples_lint_without_failures():
    proc = __import__("subprocess").run(
        [sys.executable, str(TOOLS / "spec-lint.py"), "--root", str(EXAMPLES),
         "--no-color", "--json"], capture_output=True, text=True)
    report = json.loads(proc.stdout)
    fails = [f for f in report["findings"] if f.get("severity") == "FAIL"]
    assert not fails, fails


# ── The journey-first readback ──────────────────────────────────────────────

def test_the_journey_card_says_whether_the_flow_runs(tmp_path):
    area = _project(tmp_path)
    for run, expect in (("shop.journeyRun", "✓ `shop.journeyRun` — `quint test` runs it; "
                                            "it walks 2 of 2 step(s) in order."),
                        ("shop.backwards", "✗ `shop.backwards` — breaks at step 2"),
                        (None, "⚠ _nothing yet_")):
        j = _journey(run=run) if run else _journey()
        j["_file"] = "j0"
        out = "\n".join(readback.what_the_system_does(tmp_path, "shop", area, [j]))
        line = next(ln for ln in out.splitlines() if ln.startswith("> **Runs as**"))
        assert expect in line, line


def test_each_step_shows_its_example_rows_and_guarding_invariants(tmp_path):
    area = _project(tmp_path)
    j = dict(_journey(run="shop.journeyRun"), _file="j0")
    out = "\n".join(readback.what_the_system_does(tmp_path, "shop", area, [j]))
    assert "| Example | Given | When | Then |" in out
    assert "| EX-001 — One order | the initial state | order | `orders == 1` |" in out
    assert "| EX-002 — Restock after two orders | order × 2 | restock | `stock == 5` |" in out
    block = out[out.index("#### REQ-001"):out.index("#### REQ-002")]
    assert "**Guarded by** · [INV-001](#what-must-always-be-true) `withinLimit`" in block


def test_a_rejection_shown_by_no_example_needs_attention(tmp_path):
    model = SHOP.replace("  /// @refs REQ-003\n", "")
    area = _project(tmp_path, model=model)
    items = readback.attention_items(tmp_path, "shop", area)
    assert any(i.startswith("**Refusal shown by no example** — REQ-003") for i in items)


def test_the_examples_section_is_a_table_of_derived_rows(tmp_path):
    area = _project(tmp_path)
    out = "\n".join(readback.examples_section(tmp_path, "shop", area))
    assert "| Example | Given | When | Then | Illustrates |" in out
    assert "| EX-003 — The fourth order is refused | order × 3, so `orders == 3` " \
           "| order_again — refused | refused — nothing changes | REQ-003 |" in out


# ── The witness pre-screen counts the probes the right way up ───────────────

record = _load("spec_record_f", "spec-record.py")


def _prescreen_project(tmp_path):
    _project(tmp_path)
    specs = tmp_path / "specs"
    (specs / "shop.intent.json").write_text(json.dumps(
        {"kind": "area", "area": "shop", "version": "0.1.0", "status": "formalized",
         "formal_model": {"quint_file": "shop.qnt",
                          "probes_file": "shop.probes.qnt"}}), encoding="utf-8")
    (specs / "shop.probes.qnt").write_text(
        "module shop_probes {\n"
        "  val witness_REQ_001: bool = not(true)\n"
        "  val witness_REQ_002: bool = not(true)\n"
        "  val reached_REQ_001: bool = not(witness_REQ_001)\n"
        "  val reached_REQ_002: bool = not(witness_REQ_002)\n"
        "}\n", encoding="utf-8")


class _SimArgs:
    def __init__(self, root, strict):
        self.root, self.area = str(root), "shop"
        self.steps = self.timeout = self.only = None
        self.no_witness, self.emit_json = True, False
        self.no_simulate, self.only_simulate = False, True
        self.samples = self.seed = None
        self.strict = strict


@pytest.mark.parametrize("strict,code", [(False, 0), (True, 1)])
def test_the_witness_prescreen_counts_the_probes_the_right_way_up(
        tmp_path, monkeypatch, strict, code):
    """A probe is the negation of its behavior, true at init — counting the
    runs in which IT held counted every run, so a vacuous requirement could
    never show up as 0. The pre-screen counts the positive `reached_` form."""
    _prescreen_project(tmp_path)
    seen = []

    def fake(quint, target, invariants, *a, witnesses=None, **k):
        seen.append((Path(target).name, tuple(witnesses or ())))
        counts = ({"reached_REQ_001": (0, 500), "reached_REQ_002": (12, 500)}
                  if witnesses else {})
        return "ok", "", 0.1, counts, []

    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports", lambda *a, **k: True)
    monkeypatch.setattr(record, "run_simulate", fake)
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_SimArgs(tmp_path, strict))
    assert exc.value.code == code, "only --strict turns a 0 into a failure"
    assert ("shop.probes.qnt", ("reached_REQ_001", "reached_REQ_002")) in seen
    sim = spec_source.load_area(tmp_path, "shop")["check_results"]["simulation"]
    assert sim["witnesses"] == [
        {"probe": "witness_REQ_001", "traces": 0, "explored": 500},
        {"probe": "witness_REQ_002", "traces": 12, "explored": 500}]


def test_a_generated_probe_reads_its_ghosts_only_after_the_path_holds():
    """At init `_lastC` is "" and `_prevStatus` is Map(); a `get` on either
    is a runtime error in the simulator. The path conjunct is false until an
    action runs, so it goes first and evaluation stops there."""
    area = spec_source.load_area(EXAMPLES, "subscription")
    text = (EXAMPLES / "specs" / "subscription.probes.qnt").read_text(encoding="utf-8")
    body = _val_body(text, "witness_REQ_001")
    assert body[0] == 'not(_lastAction == "cancel_subscription"'
    assert "val reached_REQ_001: bool = not(witness_REQ_001)" in text
    assert area["requirements"]


def test_strict_simulation_fails_when_the_quint_cannot_screen_probes(
        tmp_path, monkeypatch):
    """An older quint without `run --witnesses` screens nothing; --strict
    must not read that silence as every requirement reached."""
    _prescreen_project(tmp_path)
    monkeypatch.setattr(record, "find_quint", lambda: "quint")
    monkeypatch.setattr(record, "quint_supports",
                        lambda q, sub, flag: flag != "--witnesses")
    monkeypatch.setattr(record, "run_simulate",
                        lambda *a, **k: ("ok", "", 0.1, {}, []))
    with pytest.raises(SystemExit) as exc:
        record.cmd_check(_SimArgs(tmp_path, True))
    assert exc.value.code == 1
