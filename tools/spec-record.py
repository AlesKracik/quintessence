#!/usr/bin/env python3
"""
spec-record.py — Deterministic check runner and results ledger.

Runs `quint verify` for an area's invariants, properties, and witness
probes, parses the outcomes, and writes them back into specs/<area>.area.json
(or .contract.json — check_results, formal_status, witness blocks) MECHANICALLY.

Why this exists: the methodology's chain is "held by mechanisms, not by
trust in the AI". That must include the bookkeeping itself — an agent
hand-editing `witness.status: "witnessed"` into JSON is exactly the
unverified step the framework is built to eliminate. With this tool the
agent's job in /spec-check shrinks to judgment work only: drafting
predicates, regenerating the probe module, translating counterexamples
into natural language (the one free-text field this tool never touches:
counterexample.nl_explanation), and triaging matrix gaps.

Subcommands:
  check  <area> [--root .] [--steps N] [--timeout S]
                [--only INV-001,REQ-003] [--no-witness] [--json]
  verify <area> [--root .] [--code-root PATH]
                [--skip-conformance] [--skip-tests] [--json]
  equiv  <area> [--root .] [--code-root PATH] [--sequences N] [--json]

What `check` does, in order:
  1. Invariants + properties (quint_name set): `quint verify
     --invariant=<name>` each; counterexample traces saved to
     specs/<area>/traces/<ID>.cex.itf.json. Stale .cex files of
     now-verified checks are removed.
     1a. OPT-IN SMT backend: an invariant marked `proof: "smt"` is
     discharged by running z3 over its smt_file, which asserts the
     NEGATION of the invariant. unsat = no violating assignment
     exists = proven (modulo the encoding); sat = counterexample;
     unknown = not-checkable, recorded as an absent answer rather
     than as a pass.
     1b. OPT-IN structural backend: an invariant marked
     `proof: "structural"` is routed to Alloy instead — `alloy exec
     -c <alloy_command>` over formal_model.alloy_file, verdict read
     from the run's receipt.json (empty solution[] = no counterexample
     within the scope declared in the .als). Off unless an area
     declares alloy_file; nothing about the Apalache path changes.
  2. Witness probes (unless --no-witness): for every requirement with a
     witness.predicate (not skipped/deferred/non-functional), runs the
     probe `witness_<ID>` from the probes module with --init=initP
     --step=stepP. Violation = witness found (trace saved, model_sha
     pinned); no violation = no-witness (vacuity red flag).
     Skip-if-fresh: a requirement already witnessed against the current
     model_sha is not re-proven.
     2b. Requirements with modality "may" get ONE PROBE PER PERMITTED
     OUTCOME (witness.outcomes[]) instead of a single witness. A lone
     trace would only show that one of the allowed behaviors is
     reachable \u2014 exactly how a permission silently narrows into a
     requirement. The requirement counts as witnessed only when every
     declared outcome has its own trace.
  3. Writes check_results (preserving the matrix block and carrying over
     nl_explanation for counterexamples whose result didn't change),
     formal_status per invariant/property, and each witness block.

What `verify` does, in order (the deterministic half of /spec-verify —
the LLM keeps the judgment dimensions: completeness/correctness/coherence
reads of the code):
  1. Witness preflight via itf_tools.witness_status — refuses conformance
     replay while any obligation is undischarged (stale/missing/not-run).
     1b. Refusal preflight: every rejection requirement must name a
     refusal.artifact that exists on disk. A rejection has no witness
     trace by design and replay only replays traces, so without this
     the requirement class most likely to be wrong in the code carries
     no code-side evidence at all.
  2. Runs conformance.command (trace replay incl. the tampered self-test)
     and the area's test_command from the code repo root; records exit
     codes. Counts are facts, not judgments.
  3. Mechanical drift: git log between the last verification_log entry's
     code_sha and HEAD, intersected with traceability[] code paths —
     drift_detected = overall failure AND a traced file changed.
  4. Appends the verification_log entry (shas via git rev-parse), flips
     requirements[].status -> "verified" and traceability[].verified for
     witnessed REQs ONLY when the replay was green, trims the log to the
     newest 50 entries. No hand-written verdicts anywhere.

What `equiv` does (brownfield only, and only where it is configured):
  Runs the differential comparator \u2014 the original implementation and the
  regenerated one, driven through identical sequences, with
  boundary.observable_state diffed after every step. This is the one
  question only a brownfield area can ask, because only it has a second
  implementation to ask. Preflight refuses to report a verdict without a
  declared observation boundary (nothing to compare), without a parallel
  path distinct from the original (the oracle must survive the
  experiment), and without a comparator command. Writes
  check_results.differential mechanically; the verdict is
  'equivalent-in-sequences' and always carries the sequence count, because
  no number of sequences proves equivalence.

Exit codes: 0 = all verified/witnessed/fresh; 1 = any counterexample,
no-witness, error, timeout, or failed replay/tests; 2 = setup problem
(missing files/tools).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import (compute_model_sha, load_trace, witness_status,  # noqa: E402
                       area_json_path, is_rejection)
from quint_ir import parse_qnt  # noqa: E402


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fail_setup(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        fail_setup(f"{path} unreadable: {e}")


def save_area(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def find_alloy(project):
    """Resolve (java, alloy_jar) for the optional structural backend.

    Alloy shares Apalache's JVM 17+ requirement, so the only new artifact is
    the jar. Machine-specific paths belong in the environment (ALLOY_JAR),
    with .spec/project.json alloy.jar_path as the committed fallback."""
    java = shutil.which("java")
    if not java:
        fail_setup("`java` not on PATH — the Alloy backend needs JVM 17+ "
                   "(the same one Apalache needs). Run tools/check-tooling.sh.")
    jar = os.environ.get("ALLOY_JAR") or ((project.get("alloy") or {}).get("jar_path"))
    if not jar:
        fail_setup("No Alloy jar configured. Set ALLOY_JAR, or alloy.jar_path in "
                   ".spec/project.json, to org.alloytools.alloy.dist.jar (6.2+ — "
                   "the CLI this runner drives was introduced in 6.2.0).")
    jar_path = Path(jar).expanduser()
    if not jar_path.exists():
        fail_setup(f"Alloy jar not found at {jar_path}.")
    return java, str(jar_path)


def read_alloy_receipt(outdir, command):
    """Extract (result, detail, scope, solver, instance_name) for one command
    from an `alloy exec` run directory.

    The verdict comes from receipt.json — the structured DTO the CLI writes
    per run — never from stdout text, the same discipline the Quint path
    follows by detecting violations through the ITF file's presence rather
    than by grepping output for the word 'counterexample'.

    Alloy semantics are inverted for `check`: SATISFIABLE means an instance
    violating the assertion was found, i.e. a counterexample. An empty
    solution[] means none exists *within the declared scope* — bounded by
    structure, which is why the caller stamps 'verified-in-scope' and the
    scope string travels with the verdict instead of being dropped."""
    receipt = Path(outdir) / "receipt.json"
    if not receipt.exists():
        return "error", "alloy wrote no receipt.json (run failed before solving)", None, None, None
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "error", f"unreadable receipt.json: {exc}", None, None, None
    solver = data.get("solver")
    commands = data.get("commands") or {}
    entry = commands.get(command)
    if entry is None:
        known = ", ".join(sorted(commands)) or "none"
        return ("error",
                f"no command '{command}' in the .als (commands found: {known})",
                None, solver, None)
    scopes = entry.get("scopes") or []
    scope = ", ".join(str(x) for x in scopes) or (entry.get("scope") or None)
    if not (entry.get("solution") or []):
        return "verified", "", scope, solver, None
    return "counterexample", "", scope, solver, f"{command}-solution-0.xml"


def run_alloy(java, jar, als_file, command, outdir, solver, timeout):
    """Run one Alloy `check` command headless. Returns
    (result, detail, duration_s, meta) using run_verify's result vocabulary
    (verified | counterexample | timeout | error) so the caller's bookkeeping
    stays backend-agnostic.

    Flags deliberately NOT passed: -d/--depth and -y/--ymmetry. In Alloy
    6.2's CLI the symmetry option reads depth() rather than ymmetry(), so -y
    is ignored and -d silently changes symmetry breaking as well; taking the
    defaults keeps runs reproducible. -f recreates the output directory so a
    stale instance from an earlier run can never be read as this run's.

    The Alloy CLI has no timeout flag, so the cap is enforced here —
    exactly as it already is for quint."""
    outdir = Path(outdir)
    if outdir.exists():
        shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [java, "-jar", jar, "exec",
           "-c", command,
           "-t", "xml",
           "-o", str(outdir),
           "-f", "-q",
           "-s", solver,
           str(als_file)]
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", f"timed out after {timeout}s", timeout, {}
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    result, detail, scope, solver_used, instance = read_alloy_receipt(outdir, command)
    if result == "error" and proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join(out.strip().splitlines()[-5:])
        detail = tail or detail
    return result, detail, duration, {"scope": scope,
                                      "solver": solver_used or solver,
                                      "instance": instance}


def find_z3(project):
    """Resolve the z3 binary for the optional SMT backend."""
    cfg = project.get("z3") or {}
    exe = os.environ.get("Z3_BIN") or cfg.get("binary") or shutil.which("z3")
    if not exe:
        fail_setup("`z3` not found. Put it on PATH, or set Z3_BIN / z3.binary in "
                   ".spec/project.json. The SMT backend is optional \u2014 only invariants "
                   "marked proof: 'smt' need it.")
    if not shutil.which(exe) and not Path(exe).exists():
        fail_setup(f"z3 binary not found at {exe}.")
    return exe


def run_smt(z3, smt_file, timeout):
    """Run one SMT-LIB2 query. Returns (result, detail, duration_s, model).

    The query asserts the NEGATION of the invariant, so the mapping inverts
    the same way the witness probes do:
        unsat   -> no violating assignment exists -> verified
        sat     -> the model IS a counterexample
        unknown -> the solver declined to decide. Recorded as not-checkable,
                   never as a pass: an absent answer is not a proof, and
                   collapsing the two is precisely the dishonesty the result
                   taxonomy exists to prevent.
    The verdict is read from the first status token z3 prints, not by
    searching the whole output for the word 'unsat' \u2014 error text mentioning
    it would otherwise be misread as success."""
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run([z3, "-smt2", str(smt_file)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", f"timed out after {timeout}s", timeout, None
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    out = (proc.stdout or "").strip()
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    status = lines[0] if lines else ""
    if status == "unsat":
        return "verified", "", duration, None
    if status == "sat":
        return "counterexample", "", duration, "\n".join(lines[1:])
    if status == "unknown":
        reason = "\n".join(lines[1:])[:200]
        return "not-checkable", f"z3 returned unknown{': ' + reason if reason else ''}", duration, None
    tail = ((proc.stderr or "") + out).strip().splitlines()[-5:]
    return "error", "\n".join(tail), duration, None


def run_smt_check(item, iid, root, area_name, need_z3, timeout):
    """Check one `proof: "smt"` invariant and return its check_results entry,
    stamping formal_status the same way every other backend does."""
    entry = {"id": iid, "kind": "invariant", "backend": "z3", "proof": "smt",
             "result": "error"}
    rel = item.get("smt_file")
    if not rel:
        entry["error"] = "proof is 'smt' but no smt_file is set \u2014 nothing to solve."
        print(f"{iid:<12} {'error':<26} no smt_file")
        return entry
    smt_path = root / "specs" / rel
    if not smt_path.exists():
        entry["error"] = f"smt_file '{rel}' does not exist."
        print(f"{iid:<12} {'error':<26} missing {rel}")
        return entry

    z3 = need_z3()
    result, detail, duration, model = run_smt(z3, smt_path, timeout)
    entry["result"] = result
    entry["duration_s"] = round(duration, 1)
    if result == "verified":
        item["formal_status"] = "verified-smt"
        print(f"{iid:<12} {'verified (smt)':<26} ({duration:.1f}s)")
    elif result == "counterexample":
        item["formal_status"] = "counterexample-found"
        gen = root / "specs" / area_name / "gen" / "smt"
        gen.mkdir(parents=True, exist_ok=True)
        model_path = gen / f"{iid}.model.txt"
        model_path.write_text(model or "", encoding="utf-8")
        entry["instance"] = f"{area_name}/gen/smt/{model_path.name}"
        print(f"{iid:<12} {'counterexample':<26} ({duration:.1f}s)  "
              f"model in specs/{area_name}/gen/smt/")
    elif result == "not-checkable":
        # An honest non-answer. It must not inherit a previous green.
        item["formal_status"] = "not-checkable"
        entry["error"] = detail
        print(f"{iid:<12} {'not-checkable':<26} {detail}")
    else:
        entry["error"] = detail
        print(f"{iid:<12} {result:<26} {detail}")
    return entry


def find_quint():
    exe = shutil.which("quint")
    if not exe:
        fail_setup("`quint` not on PATH. Run tools/check-tooling.sh for install hints.")
    return exe


def run_verify(quint, qnt_file, invariant, max_steps, timeout,
               init=None, step=None, out_itf=None, inductive=False):
    """Run one `quint verify`. Returns (result, detail, duration_s) where
    result ∈ verified | counterexample | timeout | error.
    'counterexample' means a violation was found — for witness probes that
    is the GOOD outcome (the violation trace IS the witness).

    inductive=True checks `invariant` as an INDUCTIVE invariant — quint runs
    Apalache for the base case (holds in all init states) and the inductive
    step (holds after one transition from any state satisfying it), proving it
    over ALL reachable states, not just to a step bound. `verified` here is an
    unbounded proof; the caller stamps formal_status 'verified-inductive'. An
    invariant that isn't constrained enough yields a quint error ('x is used
    before it is assigned') — reported as 'error', not a false proof.

    Violation detection: the presence of the freshly-written --out-itf file
    — NOT output-text grepping (any error message containing the word
    'counterexample' would misclassify). The stale file is deleted before
    the run so its existence afterwards is unambiguous."""
    out_path = Path(out_itf) if out_itf else None
    if out_path and out_path.exists():
        out_path.unlink()
    if inductive:
        # quint orchestrates base + one-step preservation internally.
        cmd = [quint, "verify", f"--inductive-invariant={invariant}",
               "--max-steps=1"]
    else:
        cmd = [quint, "verify", f"--invariant={invariant}",
               f"--max-steps={max_steps}"]
    if init:
        cmd.append(f"--init={init}")
    if step:
        cmd.append(f"--step={step}")
    if out_itf:
        cmd.append(f"--out-itf={out_itf}")
    cmd.append(str(qnt_file))
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "timeout", f"timed out after {timeout}s", timeout
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    if proc.returncode == 0:
        return "verified", "", duration
    if out_path and out_path.exists():
        return "counterexample", "", duration
    # Non-zero without a violation trace: compile/CLI error.
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-5:])
    return "error", tail, duration


def run_structural_check(item, iid, als_rel, als_file, root, area_name,
                         need_alloy, solver, timeout):
    """Check one `proof: "structural"` invariant through the Alloy backend and
    return its check_results entry, mutating item["formal_status"] the same way
    the Apalache path does. Verdicts stay mechanical: the agent never writes one.

    Two ways this returns 'error' before Alloy is ever launched — both are
    configuration mistakes that must not read as green:
      - the invariant declares no alloy_command (nothing to run);
      - the area declares no alloy_file (nowhere to run it).

    The instance XML goes under specs/<area>/gen/ (gitignored): Alloy returns
    one satisfying instance out of many and that choice is not stable across
    solver or tool versions, so committing it would churn diffs and imply a
    canonicity the tool does not provide. The verdict, its scope and its solver
    are committed instead — enough to re-derive the run.
    """
    entry = {"id": iid, "kind": "invariant", "backend": "alloy",
             "proof": "structural", "result": "error"}
    command = item.get("alloy_command")
    if not command:
        entry["error"] = ("proof is 'structural' but no alloy_command is set "
                          "— nothing to run.")
        print(f"{iid:<12} {'error':<26} no alloy_command")
        return entry
    entry["alloy_command"] = command
    if not als_rel:
        entry["error"] = ("proof is 'structural' but formal_model.alloy_file "
                          "is not set — no .als to run it against.")
        print(f"{iid:<12} {'error':<26} no formal_model.alloy_file")
        return entry

    java, jar = need_alloy()
    outdir = root / "specs" / area_name / "gen" / "alloy" / command
    result, detail, duration, meta = run_alloy(
        java, jar, als_file, command, outdir, solver, timeout)
    entry["result"] = result
    entry["duration_s"] = round(duration, 1)
    if meta.get("scope"):
        entry["scope"] = meta["scope"]
    if meta.get("solver"):
        entry["solver"] = meta["solver"]

    if result == "verified":
        # Scope-bounded, not proven: a different bound from the step bound,
        # so it gets its own status rather than borrowing 'verified'.
        item["formal_status"] = "verified-in-scope"
        scope = meta.get("scope") or "declared scope"
        print(f"{iid:<12} {'verified (in scope)':<26} ({duration:.1f}s)  {scope}")
    elif result == "counterexample":
        item["formal_status"] = "counterexample-found"
        if meta.get("instance"):
            rel = f"{area_name}/gen/alloy/{command}/{meta['instance']}"
            entry["instance"] = rel
        print(f"{iid:<12} {'counterexample':<26} ({duration:.1f}s)  "
              f"instance in specs/{area_name}/gen/alloy/{command}/")
    else:
        # timeout/error: leave the prior formal_status untouched, exactly as
        # the Apalache path does — an unfinished run is not a new verdict.
        entry["error"] = detail
        print(f"{iid:<12} {result:<26} {detail}")
    return entry


def probe_name(req_id):
    return "witness_" + req_id.replace("-", "_")


def outcome_probe_name(req_id, outcome_name):
    """Probe for one permitted outcome of a `may` requirement. Distinct name
    per outcome, since each is proven separately."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", outcome_name or "").strip("_")
    return probe_name(req_id) + "_" + (slug or "outcome")


def record_may_outcomes(req, rid, witness, probes_ir, probes_file, root, area_name,
                        need_quint, max_steps, timeout, current_sha):
    """Prove each permitted outcome of a `may` requirement separately.

    Returns the number of failures. The requirement is only witnessed when
    EVERY declared outcome has its own trace: proving one of several allowed
    behaviors reachable says nothing about whether the others are, which is
    how a MAY quietly becomes a MUST."""
    outcomes = witness.get("outcomes") or []
    if not outcomes:
        print(f"{rid:<12} no-outcomes      (modality 'may' needs witness.outcomes[])")
        witness["status"] = "not-run"
        return 1

    probe_vals = set(probes_ir["vals"]) if probes_ir else set()
    bad = 0
    for oc in outcomes:
        oname = oc.get("name") or "?"
        label = f"{rid}/{oname}"
        if not oc.get("predicate"):
            print(f"{label:<24} no-predicate")
            bad += 1
            continue
        if (oc.get("status") == "witnessed" and current_sha
                and oc.get("model_sha") == current_sha):
            t_rel = oc.get("trace")
            t_path = root / "specs" / t_rel if t_rel else None
            if t_path and t_path.exists() and not load_trace(t_path)[1]:
                print(f"{label:<24} fresh")
                continue
        pname = outcome_probe_name(rid, oname)
        if probes_ir is None or pname not in probe_vals:
            print(f"{label:<24} no-probe         (expected val '{pname}')")
            bad += 1
            continue
        trace_rel = f"{area_name}/traces/{rid}.{re.sub(r'[^A-Za-z0-9]+', '-', oname)}.itf.json"
        trace_path = root / "specs" / trace_rel
        result, detail, duration = run_verify(
            need_quint(), probes_file, pname, max_steps, timeout,
            init="initP", step="stepP", out_itf=trace_path,
        )
        oc["checked_at"] = now_iso()
        if result == "counterexample":
            _, errs = load_trace(trace_path)
            if errs:
                oc["status"] = "not-run"
                print(f"{label:<24} error            (invalid trace: {errs[0]})")
                bad += 1
                continue
            oc["status"] = "witnessed"
            oc["trace"] = trace_rel
            if current_sha:
                oc["model_sha"] = current_sha
            print(f"{label:<24} WITNESSED        \u2192 specs/{trace_rel} ({duration:.1f}s)")
        elif result == "verified":
            oc["status"] = "no-witness"
            oc.pop("model_sha", None)
            bad += 1
            print(f"{label:<24} NO-WITNESS       (permitted outcome unreachable \u2014 "
                  f"the permission is narrower than written)")
        else:
            bad += 1
            print(f"{label:<24} {result:<16} {detail}")

    statuses = [oc.get("status") for oc in outcomes]
    if all(st == "witnessed" for st in statuses):
        witness["status"] = "witnessed"
        if current_sha:
            witness["model_sha"] = current_sha
    elif any(st == "no-witness" for st in statuses):
        witness["status"] = "no-witness"
    else:
        witness["status"] = "not-run"
    witness["checked_at"] = now_iso()
    return bad


def cmd_check(args):
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    area = load_json(area_path)
    if area is None:
        fail_setup(f"{area_path} not found. Run /spec {args.area} first.")
    fm = area.get("formal_model") or {}
    qnt_file = root / "specs" / (fm.get("quint_file") or f"{args.area}.qnt")
    als_rel = fm.get("alloy_file")
    als_file = root / "specs" / als_rel if als_rel else None
    if not qnt_file.exists() and not (als_file and als_file.exists()):
        # An area with a structural sidecar and no Quint model is legitimate
        # (a purely relational contract); an area with neither is not
        # formalized at all.
        fail_setup(f"{qnt_file} not found — the area isn't formalized yet.")
    if als_rel and not als_file.exists():
        fail_setup(f"{als_file} not found — formal_model.alloy_file points at nothing.")

    project = load_json(root / ".spec" / "project.json") or {}
    apalache = project.get("apalache") or {}
    max_steps = args.steps or apalache.get("max_steps", 10)
    timeout = args.timeout or apalache.get("timeout_seconds", 300)
    alloy_cfg = project.get("alloy") or {}
    alloy_solver = alloy_cfg.get("solver", "sat4j")
    alloy_timeout = args.timeout or alloy_cfg.get("timeout_seconds", 300)

    only = None
    if args.only:
        only = {t.strip() for t in args.only.split(",") if t.strip()}
        known = set()
        for ln in ("invariants", "properties", "requirements"):
            known.update(i.get("id") for i in area.get(ln, []) or [] if i.get("id"))
        unknown = only - known
        if unknown:
            fail_setup(f"--only references unknown ids: {', '.join(sorted(unknown))}. "
                       f"Known: {', '.join(sorted(known))}")

    tools = {}

    def need_quint():
        """Resolved on first use so a purely structural area doesn't require
        quint on PATH, and an Apalache-only area never requires the Alloy jar."""
        if "quint" not in tools:
            tools["quint"] = find_quint()
        return tools["quint"]

    def need_alloy():
        if "alloy" not in tools:
            tools["alloy"] = find_alloy(project)
        return tools["alloy"]

    def need_z3():
        if "z3" not in tools:
            tools["z3"] = find_z3(project)
        return tools["z3"]

    traces_dir = root / "specs" / args.area / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    prior_checks = {
        c.get("id"): c
        for c in ((area.get("check_results") or {}).get("checks") or [])
        if c.get("id")
    }
    checks = []
    bad = 0

    # ── 1. Invariants + properties ───────────────────────────────────────
    for list_name, kind in (("invariants", "invariant"), ("properties", "property")):
        for item in area.get(list_name, []) or []:
            iid, qname = item.get("id"), item.get("quint_name")
            structural = (kind == "invariant" and item.get("proof") == "structural")
            smt = (kind == "invariant" and item.get("proof") == "smt")
            # A structural or SMT invariant is carried by its own sidecar, not
            # by a Quint val, so it legitimately has no quint_name — the
            # presence gate applies only to the Apalache path.
            if not iid or (not qname and not structural and not smt):
                continue
            if only and iid not in only:
                continue
            # Structural proof is opt-in per invariant (proof: "structural")
            # and routes to Alloy instead of Apalache — a different question
            # (relational structure) with a different bound (finite scope,
            # not step depth), so it gets its own status and its own render.
            if kind == "invariant" and item.get("proof") == "smt":
                entry = run_smt_check(item, iid, root, args.area, need_z3, timeout)
                if entry.get("result") != "verified":
                    bad += 1
                checks.append(entry)
                continue
            if structural:
                entry = run_structural_check(
                    item, iid, als_rel, als_file, root, args.area,
                    need_alloy, alloy_solver, alloy_timeout,
                )
                if entry.get("result") != "verified":
                    bad += 1
                checks.append(entry)
                continue
            # Inductive proof is opt-in per invariant (proof: "inductive").
            # Default and properties stay bounded — behavior unchanged.
            inductive = (kind == "invariant" and item.get("proof") == "inductive")
            cex_rel = f"{args.area}/traces/{iid}.cex.itf.json"
            cex_path = root / "specs" / cex_rel
            result, detail, duration = run_verify(
                need_quint(), qnt_file, qname, max_steps, timeout, out_itf=cex_path,
                inductive=inductive,
            )
            entry = {
                "id": iid, "kind": kind, "quint_name": qname,
                "result": result, "duration_s": round(duration, 1),
            }
            if inductive:
                entry["proof"] = "inductive"
            if result == "counterexample":
                bad += 1
                entry["trace"] = cex_rel
                prior = prior_checks.get(iid) or {}
                if (prior.get("result") == "counterexample"
                        and isinstance(prior.get("counterexample"), dict)):
                    entry["counterexample"] = prior["counterexample"]
                item["formal_status"] = "counterexample-found"
            elif result == "verified":
                if cex_path.exists():
                    cex_path.unlink()  # stale counterexample of a now-green check
                item["formal_status"] = "verified-inductive" if inductive else "verified"
            else:
                bad += 1
                entry["error"] = detail
                # timeout/error: keep the prior formal_status untouched.
            checks.append(entry)
            label = result + (" (inductive)" if inductive and result == "verified" else "")
            print(f"{iid:<12} {label:<26} ({duration:.1f}s)"
                  + (f"  {detail}" if detail and result == "error" else ""))

    # ── 2. Witness probes ────────────────────────────────────────────────
    current_sha = compute_model_sha(root, args.area, area)
    if not args.no_witness:
        probes_rel = fm.get("probes_file")
        probes_file = root / "specs" / probes_rel if probes_rel else None
        probes_ir = parse_qnt(probes_file) if probes_file and probes_file.exists() else None
        probe_vals = set(probes_ir["vals"]) if probes_ir else set()

        for req in area.get("requirements", []) or []:
            rid = req.get("id")
            if not rid:
                continue
            if only and rid not in only:
                continue
            if (req.get("status") == "deferred"
                    or req.get("type") == "non-functional"):
                continue
            witness = req.get("witness") or {}
            if req.get("modality") == "may":
                req["witness"] = witness
                probes_rel_ok = probes_file is not None
                bad += record_may_outcomes(
                    req, rid, witness, probes_ir if probes_rel_ok else None,
                    probes_file, root, args.area, need_quint, max_steps, timeout,
                    current_sha)
                continue
            if req.get("modality") == "forbidden" and witness.get("enforced_by"):
                # A non-event has no trace; the named invariant carries the
                # proof and is checked in its own right above.
                req["witness"] = witness
                witness["status"] = "skipped"
                print(f"{rid:<12} forbidden        (enforced by "
                      f"{witness['enforced_by']})")
                continue
            if witness.get("status") == "skipped":
                if not witness.get("justification"):
                    # Same gate as spec-lint — an unjustified skip must not
                    # let this runner report green.
                    print(f"{rid:<12} SKIPPED-UNJUST.  (skip without justification "
                          f"does not discharge — justify or remove)")
                    bad += 1
                continue
            if not witness.get("predicate"):
                print(f"{rid:<12} no-predicate     (draft one via /spec, then re-run)")
                bad += 1
                continue
            req["witness"] = witness  # persist only for reqs we actually process
            if (witness.get("status") == "witnessed"
                    and current_sha and witness.get("model_sha") == current_sha):
                # Fresh by sha — but only if the trace is actually present
                # and valid; a deleted trace with a surviving stamp is not
                # fresh, it's gone.
                t_rel = witness.get("trace")
                t_path = root / "specs" / t_rel if t_rel else None
                if t_path and t_path.exists() and not load_trace(t_path)[1]:
                    print(f"{rid:<12} fresh            (model unchanged — probe skipped)")
                    continue
                print(f"{rid:<12} re-proving       (stamp fresh but trace missing/invalid)")
            if probes_ir is None:
                print(f"{rid:<12} no-probes-file   (regenerate specs/<area>.probes.qnt "
                      f"via /spec-check, then re-run)")
                bad += 1
                continue
            pname = probe_name(rid)
            if pname not in probe_vals:
                print(f"{rid:<12} no-probe         (probe val '{pname}' missing — "
                      f"regenerate the probes module)")
                bad += 1
                continue

            trace_rel = f"{args.area}/traces/{rid}.itf.json"
            trace_path = root / "specs" / trace_rel
            result, detail, duration = run_verify(
                need_quint(), probes_file, pname, max_steps, timeout,
                init="initP", step="stepP", out_itf=trace_path,
            )
            witness["checked_at"] = now_iso()
            if result == "counterexample":
                # Violation of the negated predicate = the behavior happened:
                # the trace IS the witness.
                _, errs = load_trace(trace_path)
                if errs:
                    print(f"{rid:<12} error            (trace written but invalid: {errs[0]})")
                    witness["status"] = "not-run"
                    bad += 1
                    continue
                witness["status"] = "witnessed"
                witness["trace"] = trace_rel
                if current_sha:
                    witness["model_sha"] = current_sha
                print(f"{rid:<12} WITNESSED        → specs/{trace_rel} ({duration:.1f}s)")
            elif result == "verified":
                witness["status"] = "no-witness"
                witness.pop("model_sha", None)
                bad += 1
                print(f"{rid:<12} NO-WITNESS       (unreachable up to {max_steps} steps "
                      f"— impossible guard, missing action, or bound too small)")
            else:
                bad += 1
                print(f"{rid:<12} {result:<16} {detail}")

    # ── 3. Write back ────────────────────────────────────────────────────
    # MERGE into the prior ledger, never replace it wholesale: a --only run
    # (or an all-fresh run) must not erase results it didn't re-derive.
    cr = area.setdefault("check_results", {})
    new_by_id = {c["id"]: c for c in checks}
    merged = []
    for prior in (cr.get("checks") or []):
        pid = prior.get("id")
        merged.append(new_by_id.pop(pid) if pid in new_by_id else prior)
    merged.extend(new_by_id[cid] for cid in [c["id"] for c in checks] if cid in new_by_id)
    cr["checks"] = merged  # matrix block (spec-matrix --record) is preserved
    cr["ran_at"] = now_iso()
    cr["max_steps"] = max_steps  # the bound a bounded ✓ is honest to (readback)
    save_area(area_path, area)
    print(f"\nrecorded check_results + witness blocks in {area_path}")

    if args.emit_json:
        print(json.dumps({"area": args.area, "ran_at": cr["ran_at"],
                          "checks": checks, "failures": bad}, indent=2))
    sys.exit(1 if bad else 0)


# ── equiv (differential conformance) ─────────────────────────────────────────

DIVERGENCE_RE = re.compile(r"^DIVERGENCES=(\d+)\s*$", re.MULTILINE)


def cmd_equiv(args):
    """Differential conformance: is the regenerated implementation
    substitutable for the original at the declared boundary?

    Extraction fidelity is otherwise asserted by whoever ran the confirm
    pass. Brownfield is the one case where it can be MEASURED, because the
    original answers any question you ask it \u2014 and until now nothing asked."""
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    area = load_json(area_path)
    if area is None:
        fail_setup(f"{area_path} not found.")
    if area.get("kind") == "contract":
        fail_setup(f"'{args.area}' is a contract \u2014 it has no implementation to compare.")

    project = load_json(root / ".spec" / "project.json") or {}
    repo_root, entry = resolve_code_root(root, args.area, project, args.code_root)
    conformance = area.get("conformance") or {}
    diff_cfg = conformance.get("differential") or {}
    boundary = area.get("boundary") or {}

    # Preflight. Each refusal is a thing that would make the verdict a lie.
    if not boundary.get("observable_state"):
        fail_setup("boundary.observable_state is not declared \u2014 there is nothing to "
                   "diff, so 'equivalent' would mean 'equivalent in ways nobody "
                   "wrote down'.")
    command = diff_cfg.get("command")
    if not command:
        fail_setup("conformance.differential.command is not set. Generate the "
                   "comparator with /spec-apply --parallel first.")
    parallel = diff_cfg.get("parallel_path")
    if not parallel:
        fail_setup("conformance.differential.parallel_path is not set.")
    original_path = entry.get("code_path") or ""
    par_norm = str(parallel).replace(chr(92), "/").strip("/")
    orig_norm = str(original_path).replace(chr(92), "/").strip("/")
    if orig_norm and (par_norm == orig_norm or par_norm.startswith(orig_norm + "/")):
        fail_setup(f"parallel_path '{parallel}' is inside the original's code_path "
                   f"'{original_path}'. The oracle has to survive the experiment \u2014 "
                   f"generate beside it, never over it.")
    if not (repo_root / parallel).exists():
        fail_setup(f"{repo_root / parallel} does not exist \u2014 run /spec-apply "
                   f"--parallel to generate the implementation under test.")

    sequences = args.sequences or diff_cfg.get("sequences") or 1000
    env_note = f"QUINT_DIFF_SEQUENCES={sequences}"
    os.environ["QUINT_DIFF_SEQUENCES"] = str(sequences)
    os.environ["QUINT_DIFF_PARALLEL"] = str(parallel)

    print(f"DIFFERENTIAL: {args.area} \u2014 original vs {parallel} ({env_note})")
    code, tail = run_shell(command, repo_root, args.timeout or 3600)

    # The comparator reports its own count on a final DIVERGENCES=<n> line.
    # Absent, the count is unknown rather than zero \u2014 an unreported number is
    # not a good number.
    m = DIVERGENCE_RE.search(tail or "")
    divergences = int(m.group(1)) if m else None
    result = "equivalent-in-sequences" if code == 0 else "diverged"
    if code != 0 and divergences is None:
        divergences = None
    if code not in (0, 1):
        result = "error"

    entry_out = {
        "ran_at": now_iso(),
        "sequences": sequences,
        "result": result,
    }
    if divergences is not None:
        entry_out["divergences"] = divergences
    elif result == "equivalent-in-sequences":
        entry_out["divergences"] = 0
    if tail and result != "equivalent-in-sequences":
        entry_out["detail"] = "\n".join(tail.strip().splitlines()[-8:])

    area.setdefault("check_results", {})["differential"] = entry_out
    save_area(area_path, area)

    if result == "equivalent-in-sequences":
        print(f"DIFFERENTIAL: no sequence distinguished the two implementations "
              f"({sequences} sequences). That is not equivalence \u2014 it is the "
              f"absence of a counterexample at this budget.")
    elif result == "diverged":
        n = divergences if divergences is not None else "an unreported number of"
        print(f"DIFFERENTIAL: {n} divergence(s). Each is either a missing spec "
              f"element (the extraction gap) or an intentional difference \u2014 record "
              f"the second as a decision.\n{tail}")
    else:
        print(f"DIFFERENTIAL: comparator error (exit {code})\n{tail}")

    if args.emit_json:
        print(json.dumps({"area": args.area, **entry_out}, indent=2))
    sys.exit(0 if result == "equivalent-in-sequences" else 1)


# ── verify ───────────────────────────────────────────────────────────────────

def git_head(cwd):
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd),
                              capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_changed_files(cwd, since_sha):
    try:
        proc = subprocess.run(["git", "diff", "--name-only", f"{since_sha}..HEAD"],
                              cwd=str(cwd), capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return None


def path_match(changed, traced):
    """True iff a git-changed path and a traceability code path point at the
    same file. Match on whole path SEGMENTS — a bare endswith would pair
    'src/auth/authStore.ts' with a traced 'store.ts' (…'authStore.ts' ends with
    'store.ts'), inventing drift. Require equality or a '/'-bounded suffix."""
    a = changed.replace("\\", "/").lstrip("./")
    b = traced.replace("\\", "/").lstrip("./")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def run_shell(command, cwd, timeout):
    """Run a project-defined shell command (conformance/test). Returns
    (exit_code, tail). shell=True on purpose — these are user-authored
    command lines from project.json/area config."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-8:])
    return proc.returncode, tail


def resolve_code_root(root, area_name, project, override=None):
    """(repo_root, area_entry). repo_root = where conformance/test commands
    run. Multi-repo: .spec/local.json repo_paths[code_repo]; single-repo:
    the project root."""
    entry = next((a for a in project.get("areas", []) or []
                  if a.get("name") == area_name), {}) or {}
    if override:
        return Path(override), entry
    code_repo = entry.get("code_repo")
    if code_repo:
        local = load_json(root / ".spec" / "local.json") or {}
        repo_path = (local.get("repo_paths") or {}).get(code_repo)
        if not repo_path:
            fail_setup(f"area '{area_name}' maps to repo '{code_repo}' but "
                       f".spec/local.json has no repo_paths entry for it.")
        return Path(repo_path), entry
    return Path(root), entry


def cmd_verify(args):
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    area = load_json(area_path)
    if area is None:
        fail_setup(f"{area_path} not found.")
    if area.get("kind") == "contract":
        fail_setup(f"'{args.area}' is a contract — its verification is "
                   f"/spec-check (spec-record check).")

    project = load_json(root / ".spec" / "project.json") or {}
    repo_root, entry = resolve_code_root(root, args.area, project, args.code_root)
    conformance = area.get("conformance") or {}
    test_command = entry.get("test_command")
    bad = 0
    notes = []

    # ── 1. Witness preflight ─────────────────────────────────────────────
    _rows, missing, discharged = witness_status(root, args.area, area)
    preflight_ok = missing == 0
    if not preflight_ok:
        notes.append(f"witness preflight: {missing} undischarged obligation(s)")
        print(f"PREFLIGHT: {missing} undischarged witness obligation(s) — "
              f"conformance replay refused (run itf_tools status / spec-record check).")
        bad += 1

    # ── 1b. Refusal preflight ────────────────────────────────────────────
    # A rejection produces no state change, so it has no witness trace, so
    # replay never touches it. Its evidence is a refusal artifact: drive the
    # code into the blocking state, attempt the call, assert it is refused AND
    # that nothing observable moved.
    rejections = [r for r in (area.get("requirements") or []) if is_rejection(r)]
    missing_refusals = []
    for req in rejections:
        rid = req.get("id", "?")
        artifact = (req.get("refusal") or {}).get("artifact")
        if not artifact:
            missing_refusals.append(f"{rid} (none declared)")
        elif not (repo_root / artifact).exists():
            missing_refusals.append(f"{rid} -> {artifact} (file not found)")
    if missing_refusals:
        bad += 1
        notes.append(f"refusal preflight: {len(missing_refusals)} rejection(s) "
                     f"without a runnable artifact")
        print(f"PREFLIGHT: {len(missing_refusals)} rejection requirement(s) with no "
              f"refusal artifact — " + "; ".join(missing_refusals))

    # ── 2. Conformance replay ────────────────────────────────────────────
    conf_result = None  # None = not run
    traces_replayed = 0
    if args.skip_conformance:
        notes.append("conformance: skipped (--skip-conformance)")
    elif not conformance.get("command"):
        notes.append("conformance: not set up (run /spec-apply)")
    elif preflight_ok:
        traces_dir = root / "specs" / (conformance.get("traces_dir") or f"{args.area}/traces")
        traces_replayed = len([
            p for p in (traces_dir.glob("*.itf.json") if traces_dir.exists() else [])
            if not p.name.startswith("_selftest.") and ".cex." not in p.name
        ])
        code, tail = run_shell(conformance["command"], repo_root, args.timeout or 1800)
        conf_result = (code == 0)
        if conf_result:
            print(f"CONFORMANCE: PASS — {traces_replayed} trace(s) replayed green "
                  f"(incl. tampered self-test asserting failure)")
        else:
            bad += 1
            print(f"CONFORMANCE: FAIL (exit {code})\n{tail}")
            notes.append(f"conformance failed (exit {code})")

    # Refusal artifacts are ordinary test files inside the conformance suite,
    # so their verdict is the suite's verdict. That is FILE-GRANULAR, not
    # per-requirement: a green suite means the artifact ran green along with
    # everything else, and a red suite does not say which part failed. Recorded
    # honestly as such rather than pretending to per-requirement resolution.
    if conf_result is not None and not missing_refusals:
        for req in rejections:
            refusal = req.setdefault("refusal", {})
            refusal["status"] = "passing" if conf_result else "failing"
            refusal["checked_at"] = now_iso()
        if rejections:
            print(f"REFUSAL: {len(rejections)} artifact(s) marked "
                  f"{'passing' if conf_result else 'failing'} (from the conformance run)")

    # ── 2b. Property-based tier (optional) ───────────────────────────────
    # Replay is a handful of traces of a handful of steps: Apalache returns the
    # SHORTEST counterexample, so each requirement is exercised once, along one
    # path. The adapter is already a stateful-PBT interface (one method per
    # action, one getter per var, reset()), so the same wiring explores
    # thousands of randomized sequences. Complements replay; never replaces it.
    pbt_command = conformance.get("pbt_command")
    if pbt_command and not args.skip_conformance:
        code, tail = run_shell(pbt_command, repo_root, args.timeout or 1800)
        if code == 0:
            print("PBT: PASS — randomized sequences found no divergence")
        else:
            bad += 1
            print(f"PBT: FAIL (exit {code})\n{tail}")
            notes.append(f"property-based tier failed (exit {code})")
    elif not pbt_command:
        notes.append("pbt: not configured (conformance.pbt_command)")

    # ── 3. Test suite ────────────────────────────────────────────────────
    tests_result = None
    if args.skip_tests:
        notes.append("tests: skipped (--skip-tests)")
    elif not test_command:
        notes.append("tests: no test_command configured")
    else:
        code, tail = run_shell(test_command, repo_root, args.timeout or 1800)
        tests_result = (code == 0)
        if tests_result:
            print("TESTS: PASS")
        else:
            bad += 1
            print(f"TESTS: FAIL (exit {code})\n{tail}")
            notes.append(f"tests failed (exit {code})")

    # ── 4. Mechanical drift ──────────────────────────────────────────────
    spec_sha = git_head(root)
    code_sha = git_head(repo_root)
    drift = False
    log = area.get("verification_log") or []
    prev_sha = next((e.get("code_sha") for e in reversed(log) if e.get("code_sha")), None)
    if bad and prev_sha and code_sha and prev_sha != code_sha:
        changed = git_changed_files(repo_root, prev_sha)
        if changed is not None:
            traced = set()
            for t in area.get("traceability", []) or []:
                code_ref = (t.get("code") or "").split(":", 1)[0]
                if code_ref:
                    traced.add(code_ref)
            drift = any(
                path_match(ch, tr) for ch in changed for tr in traced
            )
    if drift:
        print("DRIFT: traced files changed since last verified code_sha AND "
              "this run is failing — spec/code divergence. Codify via /spec "
              "or revert the code.")

    # ── 5. Verdict write-back ────────────────────────────────────────────
    status = "pass" if bad == 0 else "fail"
    if conf_result:  # green replay — the only path that flips "verified"
        witnessed_ids = {
            r.get("id") for r in area.get("requirements", []) or []
            if (r.get("witness") or {}).get("status") == "witnessed"
        }
        for r in area.get("requirements", []) or []:
            if r.get("id") in witnessed_ids:
                r["status"] = "verified"
        for t in area.get("traceability", []) or []:
            if t.get("id") in witnessed_ids:
                t["verified"] = True

    entry_out = {
        "date": now_iso(),
        "spec_sha": spec_sha,
        "code_sha": code_sha,
        "code_repo": entry.get("code_repo"),
        "status": status,
        "drift_detected": drift,
        "summary": "; ".join(notes) if notes else (
            f"conformance {traces_replayed} trace(s) green; tests pass"),
        "conformance": {
            "traces_replayed": traces_replayed if conf_result is not None else 0,
            "passed": traces_replayed if conf_result else 0,
            "failed": 0 if conf_result in (True, None) else traces_replayed,
            "failed_ids": [],
        },
    }
    log.append(entry_out)
    area["verification_log"] = log[-50:]  # deterministic cap, newest kept
    save_area(area_path, area)
    print(f"\nrecorded verification_log entry ({status}) in {area_path}")

    if args.emit_json:
        print(json.dumps(entry_out, indent=2))
    sys.exit(1 if bad else 0)


def main():
    p = argparse.ArgumentParser(description="Deterministic check runner + ledger.")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("check", help="Run Apalache checks + witness probes; record results.")
    pc.add_argument("area")
    pc.add_argument("--root", default=".")
    pc.add_argument("--steps", type=int, help="Override apalache.max_steps.")
    pc.add_argument("--timeout", type=int, help="Override apalache.timeout_seconds.")
    pc.add_argument("--only", help="Comma-separated IDs (INV/PROP/REQ) to run.")
    pc.add_argument("--no-witness", action="store_true", help="Skip witness probes.")
    pc.add_argument("--json", dest="emit_json", action="store_true",
                    help="Also print a JSON summary.")
    pc.set_defaults(func=cmd_check)

    pe = sub.add_parser("equiv", help="Differential conformance vs the original implementation.")
    pe.add_argument("area")
    pe.add_argument("--root", default=".")
    pe.add_argument("--code-root", dest="code_root")
    pe.add_argument("--sequences", type=int,
                    help="Override conformance.differential.sequences.")
    pe.add_argument("--timeout", type=int)
    pe.add_argument("--json", dest="emit_json", action="store_true")
    pe.set_defaults(func=cmd_equiv)

    pv = sub.add_parser("verify", help="Replay conformance + tests; record verification_log.")
    pv.add_argument("area")
    pv.add_argument("--root", default=".")
    pv.add_argument("--code-root", help="Override the resolved code repo root.")
    pv.add_argument("--timeout", type=int, help="Per-command timeout seconds (default 1800).")
    pv.add_argument("--skip-conformance", action="store_true")
    pv.add_argument("--skip-tests", action="store_true")
    pv.add_argument("--json", dest="emit_json", action="store_true",
                    help="Also print the recorded entry as JSON.")
    pv.set_defaults(func=cmd_verify)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
