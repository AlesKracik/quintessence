#!/usr/bin/env python3
"""
spec-record.py — Deterministic check runner and results ledger.

Runs `quint verify` for an area's invariants, properties, and witness
probes, parses the outcomes, and writes them into the area's ledger,
specs/<area>.records.json (check_results, formal_status, witness verdicts)
MECHANICALLY — through spec_source.save_area, which refuses to touch anything
authored.

Why this exists: the methodology's chain is "held by mechanisms, not by
trust in the AI". That must include the bookkeeping itself — an agent
hand-editing `witness.status: "witnessed"` into JSON is exactly the
unverified step the framework is built to eliminate. With this tool the
agent's job in /spec-check shrinks to judgment work only: drafting
predicates, regenerating the probe module, translating counterexamples
into natural language (the one free-text field this tool never touches:
counterexample.nl_explanation), and writing a verdict where an action
runs uncovered.

Subcommands:
  check  <area> [--root .] [--steps N] [--shallow-steps N] [--timeout S]
                [--budget S] [--only INV-001,REQ-003] [--no-witness] [--json]
                [--no-simulate] [--only-simulate] [--samples N] [--seed S]
  verify <area> [--root .] [--code-root PATH]
                [--skip-conformance] [--skip-tests] [--json]
  equiv  <area> [--root .] [--code-root PATH] [--sequences N] [--json]
  stamp  <area> (--generated | --extracted) [--root .] [--code-root PATH]
                [--code-path SUBTREE]
  changed <area> [--root .] [--code-root PATH] [--since SHA] [--code-path P]

What `check` does, in order:
  0. Simulator pre-gate (unless --no-simulate): `quint run` over the area's
     bounded invariants and, with --witnesses, over the witness probes.
     Invariants declared over: "probes" (they compare a step with the one
     before, through the _prev* ghosts) run against the probe module with
     --init=initP --step=stepP — the same place Apalache checks them. A run
     that errors (a name the module does not declare) is recorded as
     `error`, never as having falsified anything.
     ADVISORY ONLY — the simulator's "[ok] No violation found" means "not in
     the executions I explored", never "verified", so this step writes
     check_results.simulation and NEVER a formal_status. What it buys is the
     workflow quint's own docs recommend: seconds of randomized exploration
     ahead of minutes of Apalache, so a shallow bug lands at the TOP of the
     run, and a witness that no random trace ever reaches is flagged before
     the model checker is asked to find it. --only-simulate stops after this
     step (and then writes nothing but the simulation block — bumping
     check_results.ran_at would make the area read as "checked").
  1. Invariants + properties (quint_name set): `quint verify
     --invariant=<name>` each; counterexample traces saved to
     specs/<area>/traces/<ID>.cex.itf.json. Stale .cex files of
     now-verified checks are removed.
     1-fast. Bounded invariants are first tried in ONE batched run
     (`quint verify --invariants=a --invariants=b ...`). Each `quint verify`
     pays JVM start + Apalache compile, so the green path — the common one
     in CI — collapses from N of those to one. A batch that is not green
     falls straight through to the per-ID loop below, which reports and
     traces exactly what it always did.
     1-ladder. Every bounded check runs in TWO PASSES, because the cost and
     the information are not spent in the same place. A counterexample is
     shallow — the `quint run` pre-gate falsifies invariants in
     milliseconds — while depth is only needed to FAIL to find one. So
     pass 1 runs every bounded check at apalache.shallow_steps (default 3);
     any counterexample there is FINAL and its trace is recorded. Pass 2
     re-runs only the checks that came back clean, at apalache.max_steps,
     for as long as apalache.budget_seconds lasts. timeout_seconds caps ONE
     check; budget_seconds caps the whole run, so a 20-check area declares
     its cost up front instead of discovering it by waiting.
     A check the budget stopped keeps its SHALLOW verdict and records why it
     was not deepened — a shallow pass is never silently upgraded to a deep
     one. The bound each check actually reached is written per check
     (check_results.checks[].steps); check_results.max_steps stays the
     CONFIGURED ceiling. Pass 1 itself is never budget-gated: it is the
     cheap half and the half that carries the findings, so every selected
     check always gets at least a shallow verdict.
     1-order. Pass 1 runs the invariants the simulator pre-gate already
     falsified FIRST (check_results.simulation.invariants.falsified). They
     are guaranteed to produce a counterexample, they are cheap, and they
     carry the only information in the run; everything the simulator could
     not falsify is the expensive tail. Witness probes are ordered by the
     same signal inverted — a probe the simulator HIT is about to be
     witnessed cheaply, a probe it never reached is the expensive tail.
     The ladder applies to bounded invariants and witness probes only.
     `proof: "inductive"` (unbounded), `proof: "structural"` (Alloy, finite
     scope), `proof: "smt"` (z3) and temporal properties (TLC, no step
     bound) stay single-pass — a step ladder is meaningless for all four.
     1t. PROPERTIES ARE TEMPORAL, not state predicates: they run as
     `quint verify --temporal=<name> --backend=tlc`. Apalache's temporal
     support is partial (quint's docs: temporal properties can only be
     checked with --backend=tlc); TLC is explicit-state, so --max-steps and
     --out-itf do not apply and a temporal violation yields no ITF trace.
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
  1r. Requirement OBLIGATIONS (unless --no-witness): for every formal
     requirement, `oblig_<ID>` from the probes module — every reachable step
     of its action from a state where @while held ends where @shall holds
     (or never happens, if forbidden). It rides the bounded ladder like a
     transition invariant; the verdict goes to the requirement's
     obligation.formal_status (with steps and model_sha), a violation's
     trace to specs/<area>/traces/<ID>.violation.itf.json.
  2. Witness probes (unless --no-witness): for every formal requirement
     whose witness is not exempt (forbidden, computed, or holding in every
     state — itf_tools.witness_exempt), runs the probe `witness_<ID>` from
     the probes module with --init=initP --step=stepP. Violation = witness
     found (trace saved, model_sha pinned); no violation = no-witness
     (vacuity red flag: the obligation above holds of nothing).
     Skip-if-fresh: a requirement already witnessed against the current
     model_sha is not re-proven.
     2b. Requirements with modality "may" get ONE PROBE PER PERMITTED
     OUTCOME (witness.outcomes[]) instead of a single witness. A lone
     trace would only show that one of the allowed behaviors is
     reachable \u2014 exactly how a permission silently narrows into a
     requirement. The requirement counts as witnessed only when every
     declared outcome has its own trace.
  3. Writes check_results (carrying over
     nl_explanation for counterexamples whose result didn't change),
     formal_status per invariant/property, and each witness block.
     INCREMENTALLY: every verdict is merged into the ledger and saved the
     moment it is known, so a run stopped by Ctrl-C, by its budget, or by a
     crash keeps what it actually proved instead of discarding all of it.
     check_results.ran_at is the ONE field stamped only at the very end —
     every phase-flag consumer reads it as "this area was checked", and an
     interrupted run has not checked the area.

What `verify` does, in order (the deterministic half of /spec-code-verify —
the LLM keeps the judgment dimensions: completeness/correctness/coherence
reads of the code):
  1. Requirement preflight via itf_tools.witness_status — refuses
     conformance replay while any requirement's witness or obligation is
     undischarged (stale/missing/not-run/violated).
     1b. Refusal preflight: every rejection requirement must be shown by
     an example (an EX whose @refs names it). A rejection has no witness
     trace by design, so its code-side evidence is that example's run,
     replayed by the conformance harness; without one the requirement
     class most likely to be wrong in the code carries no evidence.
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

What `stamp` and `changed` do (spec↔code provenance):
  verification_log records that a spec commit and a code commit were once
  CHECKED together. It does not record what the code was BUILT TO, or what
  the spec was READ FROM — different facts, established at different
  moments, and the ones that answer "what has moved since".
  `stamp --generated` writes generated_from (spec sha + the content hash of
  the area's claims + the code sha) at the end of /spec-code-generate;
  `stamp --extracted` writes extracted_from (code sha + subtree) after a
  brownfield extraction. Both read git themselves — an agent must never
  type a sha, for the same reason it never types a verdict.
  `changed` then answers the question re-extraction actually has: what moved
  in the code since the spec was read out of it. Fingerprints stay the
  identity mechanism (they survive reformatting and rebases, a sha does
  not); this adds the narrative a set of fingerprints cannot carry — what
  changed, and since when.

Exit codes: 0 = all verified/witnessed/fresh; 1 = any counterexample,
no-witness, error, timeout, or failed replay/tests; 2 = setup problem
(missing files/tools).
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import (compute_model_sha, compute_spec_sha, model_index,
                       restamp_area_wide,  # noqa: E402
                       load_trace, witness_status, is_hollow,
                       area_json_path, is_rejection, witness_exempt,
                       owes_obligation, probe_name, outcome_probe_name,
                       reach_name, obligation_name, pre_ghost,
                       param_slots, restore_param_names)
from spec_source import (load_area, save_area as save_records,  # noqa: E402
                         records_path, SpecSourceError)
import spec_source  # noqa: E402
from quint_ir import parse_qnt  # noqa: E402
from quint_ir import cli_available, DEFAULT_ENGINE  # noqa: E402
from completeness import (area_completeness, requirement_checks,  # noqa: E402
                          mirrored_actions)
from proc import run_group, kill_all  # noqa: E402

# How many checks pass 1 times before it projects what the rest will cost.
PROJECT_AFTER = 3
# Every check pool a run opens, so an interrupt can cancel what is queued
# before it kills what is running.
_POOLS = []


def fmt_secs(s):
    s = int(round(s))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def default_parallel():
    """Half the CPUs: a check is one Apalache query plus a quint process,
    and the other half is left for the server's own threads."""
    return max(1, (os.cpu_count() or 2) // 2)


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


def load_area_or_fail(root, name, hint=""):
    """The derived area view (spec_source), or a setup failure."""
    try:
        area = load_area(root, name)
    except SpecSourceError as e:
        fail_setup(str(e))
    if area is None:
        fail_setup(f"{area_json_path(root, name)} not found.{hint}")
    return area


def save_area(root, name, data):
    """Write what this run recorded to specs/<name>.records.json — the
    ledger. Everything authored stays where it is; spec_source refuses a
    save that would change any of it."""
    try:
        save_records(root, name, data)
    except SpecSourceError as e:
        fail_setup(str(e))


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
        proc = run_group(cmd, timeout=timeout)
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
        proc = run_group([z3, "-smt2", str(smt_file)], timeout=timeout)
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


# quint's own violation marker. Used ONLY where no ITF file can settle the
# question (TLC writes none): the file test stays the primary signal because
# it cannot be confused by an error message that happens to contain a word.
VIOLATION_MARKER = "[violation]"
# The shared Apalache server died (a timed-out check is killed with its
# whole process group, the server it started included) and two parallel
# checks raced to start the next one: the loser could not bind the port.
# Not an answer about the model — the run is retried once.
SERVER_RACE = ("Address already in use", "Failed to bind to address")


def run_quint(cmd, timeout):
    proc = run_group(cmd, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and any(m in out for m in SERVER_RACE):
        time.sleep(3 + 3 * (hash(threading.current_thread().name) % 3))
        proc = run_group(cmd, timeout=timeout)
    return proc
OK_MARKER = "[ok]"

# "Witnesses: someName was witnessed in 7094 trace(s) out of 10000 explored"
WITNESS_RE = re.compile(
    r"(\S+) was witnessed in (\d+) trace\(s\) out of (\d+) explored")

_QUINT_CAPS = {}


def quint_supports(quint, subcommand, flag, timeout=60):
    """True when `quint <subcommand> --help` advertises `flag`.

    The flags this runner wants landed in different quint releases
    (--backend on run/test, --invariants on verify). A missing flag must
    degrade to the older command line, never fail the run: probing --help
    once per (subcommand, flag) is cheaper than an exception per area and
    keeps an older quint working exactly as it worked before."""
    key = (subcommand, flag)
    if key not in _QUINT_CAPS:
        try:
            proc = run_group([quint, subcommand, "--help"], timeout=timeout)
            # Whole-flag match: "--server" must not match "--server-endpoint".
            _QUINT_CAPS[key] = re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])",
                                         (proc.stdout or "") + (proc.stderr or "")) is not None
        except (OSError, subprocess.SubprocessError):
            _QUINT_CAPS[key] = False
    return _QUINT_CAPS[key]


def run_verify(quint, qnt_file, invariant, max_steps, timeout,
               init=None, step=None, out_itf=None, inductive=False,
               temporal=False, backend=None, no_server=False, endpoint=None):
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

    temporal=True checks `invariant` as a TEMPORAL property — `--temporal=`,
    not `--invariant=`. A liveness formula is not a state predicate, and
    Apalache's temporal support is partial (quint's docs: temporal properties
    can only be checked with --backend=tlc), so this path defaults to TLC.
    TLC is explicit-state: --max-steps and --out-itf are Apalache-only flags
    and are omitted, so a temporal violation produces NO ITF trace.

    no_server=True adds `--server=false`, asking quint to run Apalache
    one-shot instead of through its persistent server. A run-level budget is
    not enough on its own: killing this process on TimeoutExpired leaves the
    timed-out query running inside a server that the NEXT check then has to
    share, so one slow check congests every check after it — and the ladder's
    deep pass would inherit exactly that. The caller sets this once a check
    has timed out. It is probe-gated by the caller (quint_supports) because
    the flag arrived in a later quint than the rest of this command line;
    where it is unavailable, the behavior is what it always was.

    Violation detection: the presence of the freshly-written --out-itf file
    — NOT output-text grepping (any error message containing the word
    'counterexample' would misclassify). The stale file is deleted before
    the run so its existence afterwards is unambiguous. Where no ITF can be
    written at all (TLC), quint's own `[violation]` marker is the fallback,
    and its ABSENCE reports 'error' rather than guessing a verdict."""
    out_path = Path(out_itf) if out_itf else None
    if out_path and out_path.exists():
        out_path.unlink()
    backend = backend or ("tlc" if temporal else None)
    # --max-steps, --out-itf and --random-transitions are Apalache-only.
    itf_capable = (backend or "apalache") == "apalache"
    if temporal:
        cmd = [quint, "verify", f"--temporal={invariant}"]
    elif inductive:
        # quint orchestrates base + one-step preservation internally.
        cmd = [quint, "verify", f"--inductive-invariant={invariant}",
               "--max-steps=1"]
    else:
        cmd = [quint, "verify", f"--invariant={invariant}",
               f"--max-steps={max_steps}"]
    if backend:
        cmd.append(f"--backend={backend}")
    if no_server:
        cmd.append("--server=false")
    if endpoint and itf_capable:
        # A parallel worker's own Apalache server: quint starts one per
        # call on the endpoint's port when none is running there, and two
        # workers sharing a port race to bind it.
        cmd.append(f"--server-endpoint={endpoint}")
    if init:
        cmd.append(f"--init={init}")
    if step:
        cmd.append(f"--step={step}")
    if out_itf and itf_capable:
        cmd.append(f"--out-itf={out_itf}")
    cmd.append(str(qnt_file))
    started = datetime.now(timezone.utc)
    try:
        proc = run_quint(cmd, timeout)
    except subprocess.TimeoutExpired:
        return "timeout", f"timed out after {timeout}s", timeout
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    if proc.returncode == 0:
        return "verified", "", duration
    if out_path and out_path.exists():
        return "counterexample", "", duration
    out = (proc.stdout or "") + (proc.stderr or "")
    if not itf_capable and VIOLATION_MARKER in out:
        return ("counterexample",
                f"no trace: --out-itf is Apalache-only, this ran on {backend}",
                duration)
    # Non-zero without a violation trace: compile/CLI error.
    tail = "\n".join(out.strip().splitlines()[-5:])
    return "error", tail, duration


def run_verify_batch(quint, qnt_file, names, max_steps, timeout,
                     no_server=False, init=None, step=None):
    """Check every name in `names` in ONE `quint verify`. Returns
    (result, detail, duration_s) for the BATCH — result ∈
    verified | not-verified | timeout | error — never a per-name verdict.

    Why batch: each `quint verify` pays JVM start plus an Apalache compile of
    the whole module. On the green path (the common one in CI) N invariants
    pay that N times. One batched run collapses it to once.

    Why the caller still owns the red path: a batch says *something* failed,
    and the ledger needs per-ID attribution AND a per-ID counterexample
    trace. So 'not-verified' means only "re-run these individually" — the
    existing per-ID loop then reports exactly what it always reported. This
    function can therefore never turn a red result green or mis-attribute
    one; the worst it can do is spend one extra Apalache run.

    Repeated `--invariants=<name>` rather than one space-separated list: the
    flag is typed as an array, and a bare list would swallow the positional
    input file as another element."""
    cmd = ([quint, "verify"]
           + [f"--invariants={n}" for n in names]
           + [f"--max-steps={max_steps}"]
           + (["--server=false"] if no_server else [])
           + ([f"--init={init}"] if init else [])
           + ([f"--step={step}"] if step else [])
           + [str(qnt_file)])
    started = datetime.now(timezone.utc)
    try:
        proc = run_quint(cmd, timeout)
    except subprocess.TimeoutExpired:
        return "timeout", f"batch timed out after {timeout}s", timeout
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    if proc.returncode == 0:
        return "verified", "", duration
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-5:])
    return "not-verified", tail, duration


def run_simulate(quint, qnt_file, invariants, max_samples, max_steps, timeout,
                 seed=None, backend=None, init=None, step=None, witnesses=None):
    """Run the SIMULATOR (`quint run`) over `invariants` (and, when given,
    report on `witnesses`). Returns (result, detail, duration_s, counts,
    falsified) with result ∈ ok | violation | timeout | error | not-run,
    counts a {witness_name: (hit_traces, explored_traces)} dict, and
    falsified the sorted subset of `invariants` the simulator NAMED in its
    output as violated.

    `falsified` is attribution, not a verdict: it orders the model-checking
    passes so the checks already known to have a counterexample run first.
    A quint that does not name the violated invariant when several are
    checked at once yields an empty list, and the caller then keeps
    declaration order — the ordering degrades, nothing else does.

    ADVISORY, by construction. `[ok] No violation found` from the simulator
    means "not in the executions I explored" — it is not a verification
    verdict, and nothing downstream of this function writes formal_status.
    What it buys is the workflow quint's own docs recommend: seconds of
    randomized exploration before minutes of model checking, so a shallow
    bug is reported at the top of a run rather than after it.

    `--witnesses` reports, per name, in how many of the explored traces the
    predicate held. A witness at 0/N is a strong hint that the behavior is
    unreachable — the vacuity red flag, found cheap. It is only a hint: the
    simulator is incomplete, so the model checker still runs."""
    cmd = [quint, "run", f"--max-samples={max_samples}",
           f"--max-steps={max_steps}"]
    for name in invariants or []:
        cmd.append(f"--invariants={name}")
    for name in witnesses or []:
        cmd.append(f"--witnesses={name}")
    if backend:
        cmd.append(f"--backend={backend}")
    if seed:
        cmd.append(f"--seed={seed}")
    if init:
        cmd.append(f"--init={init}")
    if step:
        cmd.append(f"--step={step}")
    cmd.append(str(qnt_file))
    started = datetime.now(timezone.utc)
    try:
        proc = run_group(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", f"timed out after {timeout}s", timeout, {}, []
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    out = (proc.stdout or "") + (proc.stderr or "")
    counts = {m.group(1): (int(m.group(2)), int(m.group(3)))
              for m in WITNESS_RE.finditer(out)}
    tail = "\n".join(out.strip().splitlines()[-5:])
    if proc.returncode == 0:
        return "ok", "", duration, counts, []
    # Matched against the WHOLE output, not the recorded tail: quint prints
    # the trace between the violation and the summary, so the name can be
    # far from the end. A name that turns up for some other reason costs
    # at most a worse ordering.
    if VIOLATION_MARKER in out:
        falsified = sorted(n for n in (invariants or []) if n in out)
        return "violation", tail, duration, counts, falsified
    # Not a violation: the run did not start (a name the module does not
    # declare, a type error). The invariant's name in a "Name not found"
    # message is the CAUSE of the error, not a counterexample to it — read
    # as falsified, it was recorded as one and reordered the shallow pass.
    return "error", tail, duration, counts, []


def run_simulate_trace(quint, qnt_file, invariant, max_samples, max_steps,
                       timeout, out_itf, seed=None, backend=None, init=None,
                       step=None):
    """Ask the SIMULATOR for one execution that violates `invariant`, written
    to `out_itf`. Returns (result, detail, duration_s) with result ∈
    counterexample | ok | timeout | error — the same vocabulary as
    run_verify, so a caller can take either engine's answer.

    Used for witness probes only, and only for the POSITIVE answer. A
    witness is existential: one concrete run that fired the step from
    @while and ended in @shall is the proof, whoever found it. 'ok' here
    means "not in the runs I tried", never "unreachable" — the caller sends
    that probe to Apalache, which alone may say no-witness.

    One invariant per run: `quint run` stops at the first violation, so a
    violation of a single named invariant can only be that invariant's."""
    out_path = Path(out_itf)
    if out_path.exists():
        out_path.unlink()
    cmd = [quint, "run", f"--invariant={invariant}",
           f"--max-samples={max_samples}", f"--max-steps={max_steps}",
           f"--out-itf={out_path}"]
    if backend:
        cmd.append(f"--backend={backend}")
    if seed:
        cmd.append(f"--seed={seed}")
    if init:
        cmd.append(f"--init={init}")
    if step:
        cmd.append(f"--step={step}")
    cmd.append(str(qnt_file))
    started = datetime.now(timezone.utc)
    try:
        proc = run_group(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        if out_path.exists():
            out_path.unlink()
        return "timeout", f"timed out after {timeout}s", timeout
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        if out_path.exists():
            out_path.unlink()   # a sample trace, not a violation
        return "ok", "", duration
    if VIOLATION_MARKER in out and out_path.exists():
        return "counterexample", "", duration
    if out_path.exists():
        out_path.unlink()
    return "error", "\n".join(out.strip().splitlines()[-5:]), duration


def simulated_witness_ok(trace, action, pre_ghost_name=None):
    """Does a simulator trace end in the step its probe is about? The
    mechanical half of trusting it: quint's evaluator reported the probe
    violated at the last state; this checks, from the trace alone, the
    conjuncts that can be read off it — the step is the requirement's own
    action (not init: an event probe cannot be hollow) and, where the
    requirement has an @while, its pre-state ghost is true."""
    states = (trace or {}).get("states") or []
    if len(states) < 2:
        return False
    last = states[-1]
    if last.get("_lastAction") != action:
        return False
    if pre_ghost_name and pre_ghost_name in last and last[pre_ghost_name] is not True:
        return False
    return True


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


def roll_up_outcomes(witness, verdict_sha):
    """Derive a `may` requirement's witness status from its outcomes.

    Extracted so it can be applied TWICE: once after the shallow pass and
    again after the deep one. A requirement whose last outcome is only
    witnessed at depth must not keep the roll-up the shallow pass wrote."""
    statuses = [oc.get("status") for oc in (witness.get("outcomes") or [])]
    if statuses and all(st == "witnessed" for st in statuses):
        witness["status"] = "witnessed"
        if verdict_sha:
            witness["model_sha"] = verdict_sha
    elif any(st == "hollow" for st in statuses):
        # Reported ahead of no-witness: an unreachable outcome and a vacuous
        # one need different fixes, and the vacuous one is the more dangerous
        # to leave unnamed because it looks like a pass.
        witness["status"] = "hollow"
    elif any(st == "no-witness" for st in statuses):
        witness["status"] = "no-witness"
    else:
        witness["status"] = "not-run"
    witness["checked_at"] = now_iso()


def outcome_trace_rel(area_name, rid, oname):
    return f"{area_name}/traces/{rid}.{re.sub(r'[^A-Za-z0-9]+', '-', oname)}.itf.json"


def record_may_outcomes(req, rid, witness, probes_ir, probes_file, root, area_name,
                        probe_run, steps, verdict_sha, deep_queue=None,
                        stamp=None):
    """Prove each permitted outcome of a `may` requirement separately.

    Returns the number of failures. The requirement is only witnessed when
    EVERY declared outcome has its own trace: proving one of several allowed
    behaviors reachable says nothing about whether the others are, which is
    how a MAY quietly becomes a MUST.

    `probe_run(pname, trace_path, steps)` runs one probe; `steps` is the bound
    THIS pass uses. An outcome that finds no witness at a shallow bound is
    recorded as no-witness at that bound and, when `deep_queue` is given,
    queued for the deep pass — the caller re-runs it and calls
    roll_up_outcomes again. `stamp(oc, trace_path)` records which engine
    found a witnessed outcome's trace. `verdict_sha` is the requirement's own
    fingerprint (ModelIndex.verdict_sha)."""
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
        if (oc.get("status") == "witnessed" and verdict_sha
                and oc.get("model_sha") == verdict_sha):
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
        trace_rel = outcome_trace_rel(area_name, rid, oname)
        trace_path = root / "specs" / trace_rel
        result, detail, duration = probe_run(pname, trace_path, steps)
        oc["checked_at"] = now_iso()
        oc["steps"] = steps
        if result == "counterexample":
            trace, errs = load_trace(trace_path)
            if errs:
                oc["status"] = "not-run"
                print(f"{label:<24} error            (invalid trace: {errs[0]})")
                bad += 1
                continue
            if is_hollow(trace):
                # Same refusal as the single-witness path. A permission whose
                # outcome "happens" at init has not been shown to be a choice
                # the system makes.
                oc["status"] = "hollow"
                oc.pop("model_sha", None)
                oc.pop("found_by", None)
                oc.pop("revalidated_by", None)
                oc["trace"] = trace_rel
                bad += 1
                print(f"{label:<24} HOLLOW           (predicate already true in the "
                      f"initial state)")
                continue
            oc["status"] = "witnessed"
            oc["trace"] = trace_rel
            if verdict_sha:
                oc["model_sha"] = verdict_sha
            if stamp:
                stamp(oc, trace_path)
            print(f"{label:<24} WITNESSED        -> specs/{trace_rel} ({duration:.1f}s"
                  + (", simulator" if oc.get("found_by") == "simulator" else "") + ")")
        elif result == "verified":
            oc["status"] = "no-witness"
            oc.pop("model_sha", None)
            oc.pop("found_by", None)
            oc.pop("revalidated_by", None)
            bad += 1
            if deep_queue is not None:
                # Provisional: recorded pessimistically at this bound so an
                # interrupted run keeps the honest finding, and queued so the
                # deep pass can still find the trace.
                deep_queue.append({"oc": oc, "label": label, "pname": pname, "rid": rid,
                                   "trace_rel": trace_rel,
                                   "trace_path": trace_path,
                                   "witness": witness})
            print(f"{label:<24} NO-WITNESS       (permitted outcome unreachable "
                  f"up to {steps} steps — the permission may be narrower than "
                  f"written)")
        else:
            bad += 1
            print(f"{label:<24} {result:<16} {detail}")

    roll_up_outcomes(witness, verdict_sha)
    return bad


def stale_witnesses(root, area, sha_for, selected):
    """[(label, probe, trace, rid, block, witness)] for every witnessed
    block whose stamp no longer matches its requirement's fingerprint and
    whose trace is on disk, valid and not hollow."""
    out = []
    for req in area.get("requirements") or []:
        rid = req.get("id")
        if (not rid or not selected(req) or req.get("status") == "deferred"
                or witness_exempt(req) or not owes_obligation(req)):
            continue
        w = req.get("witness") or {}
        if req.get("modality") == "may":
            blocks = [(f"{rid}/{oc.get('name')}", outcome_probe_name(rid, oc.get("name")), oc)
                      for oc in w.get("outcomes") or []]
        else:
            blocks = [(rid, probe_name(rid), w)]
        for label, probe, block in blocks:
            stamp = block.get("model_sha")
            if (block.get("status") != "witnessed" or not block.get("trace")
                    or not stamp or stamp == sha_for(rid)):
                continue
            trace, errs = load_trace(root / "specs" / block["trace"])
            if errs or is_hollow(trace):
                continue
            out.append((label, probe, trace, rid, block, w))
    return out


def replay_stale_witnesses(root, area_name, area, probes_file, qnt_file, sha_for,
                           selected, need_quint, timeout):
    """Replay every stale witness's trace through the current probe module
    (tools/witness_replay.py) in one `quint test`; re-stamp each that still
    holds. Returns how many were re-stamped. Whatever does not replay is
    left stale, and the witness pass proves it again as before."""
    cands = stale_witnesses(root, area, sha_for, selected)
    if not cands:
        return 0
    wr = load_tool("witness_replay.py", "witness_replay")
    sp = load_tool("spec-probes.py", "spec_probes_slicer")
    text = probes_file.read_text(encoding="utf-8")
    m = re.search(r"^module (\w+) \{$", text, re.MULTILINE)
    out_dir = root / "specs" / area_name / "gen" / "replay"
    name = f"{m.group(1) if m else area_name.replace('-', '_')}_replay"
    prefix = Path(os.path.relpath(probes_file.parent, out_dir)).as_posix()

    def rebase(t, mod, pre):
        # Every val kept: every ghost stepP's branches assign must exist.
        return sp.slice_text(t, re.findall(r"(?m)^  val (\w+)\s*:", t), mod, pre)

    try:
        module, runs, skipped = wr.build(text, qnt_file.read_text(encoding="utf-8"),
                                         cands, name, prefix, rebase)
    except (KeyError, ValueError, OSError) as e:
        print(f"{'replay':<12} not run ({e}) — stale witnesses go to the checker")
        return 0
    for label, why in skipped:
        print(f"{label:<24} not replayed     ({why})")
    if module is None:
        return 0
    quint = need_quint()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.qnt"
    target.write_text(module, encoding="utf-8")
    cmd = [quint, "test", str(target), "--match", "^replay_"]
    if quint_supports(quint, "test", "--max-samples"):
        cmd.append("--max-samples=1")
    try:
        proc = run_group(cmd, timeout=timeout)
        results = wr.parse_test_output((proc.stdout or "") + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        results = {}
    if not results:
        print(f"{'replay':<12} quint test gave no results — stale witnesses go "
              f"to the checker")
        return 0
    n, may = 0, {}
    for run, (label, _probe, _trace, rid, block, w) in runs.items():
        if results.get(run) != "passed":
            print(f"{label:<24} replay failed    (the trace no longer runs, or no "
                  f"longer ends where the probe says — re-proving)")
            continue
        block["model_sha"] = sha_for(rid)
        block["revalidated_by"] = "replay"
        block["checked_at"] = now_iso()
        if block is not w:
            may[id(w)] = (w, rid)
        n += 1
        print(f"{label:<24} fresh            (trace replayed against the changed model)")
    for w, rid in may.values():
        roll_up_outcomes(w, sha_for(rid))
    return n


def cmd_check(args):
    root = Path(args.root)
    area_path = records_path(root, args.area)
    area = load_area_or_fail(root, args.area, f" Run /spec {args.area} first.")
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
    # Two-pass ladder. Counterexamples are shallow — the simulator pre-gate
    # falsifies invariants in milliseconds — while depth is only needed to
    # FAIL to find one. shallow_steps is pass 1's bound; max_steps stays the
    # ceiling pass 2 aims at, and never less than shallow (a --steps 2 run
    # must not silently check deeper than it was told to).
    shallow_steps = min(
        getattr(args, "shallow_steps", None) or apalache.get("shallow_steps", 3),
        max_steps)
    # timeout caps ONE check; this caps all of them. Without it a 20-check
    # area has a 20 x timeout worst case that is declared nowhere before it
    # starts. The clock starts HERE, so the pre-gate counts against it too.
    budget_seconds = (getattr(args, "budget", None)
                      or apalache.get("budget_seconds", 900))
    deadline = time.monotonic() + budget_seconds

    def budget_left():
        return deadline - time.monotonic()
    # Independent checks at once. 1 is the serial run exactly as it was.
    parallel = max(1, getattr(args, "parallel", None)
                   or apalache.get("parallel") or default_parallel())
    # What this run cost, and whether the budget reached the deep pass:
    # check_results.cost. Filled by the pass-1 projection and the deep pass.
    cost = {}
    alloy_cfg = project.get("alloy") or {}
    alloy_solver = alloy_cfg.get("solver", "sat4j")
    alloy_timeout = args.timeout or alloy_cfg.get("timeout_seconds", 300)

    # `quint` block: the simulator pre-gate and the batched verify. Every
    # value has a working default, and every one of them can be turned off,
    # because both features are accelerations of an existing path — never a
    # new source of verdicts.
    quint_cfg = project.get("quint") or {}
    eval_backend = quint_cfg.get("eval_backend", "rust")
    sim_samples = args.samples or quint_cfg.get("max_samples", 10000)
    sim_steps = quint_cfg.get("max_steps", max_steps)
    sim_seed = args.seed or quint_cfg.get("seed")
    sim_timeout = quint_cfg.get("simulate_timeout_seconds", 120)
    batch_enabled = quint_cfg.get("batch_invariants", True)
    # A probe the pre-screen HIT has a witness the simulator can hand over
    # directly; off, every witness is constructed by Apalache as before.
    sim_witnesses = quint_cfg.get("simulator_witnesses", True)
    # A witness the model changed under is replayed before it is re-proven:
    # a trace that still runs through the new initP/stepP still witnesses.
    replay_witnesses = quint_cfg.get("replay_witnesses", True)
    temporal_backend = quint_cfg.get("temporal_backend", "tlc")

    # The completeness checks (coverage, explanation, verdicts), keyed by
    # the probe val that states each one — completeness.py is the one
    # source of what exists and what it is called.
    _comp, comp_checks = (area_completeness(root, area)
                          if qnt_file.exists() else ([], {}))

    only = None
    if args.only:
        only = {t.strip() for t in args.only.split(",") if t.strip()}
        known = {c["id"] for c in comp_checks.values()}
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

    # Probe module IR, parsed once. Hoisted above the simulator gate because
    # both the witness pre-screen (step 0) and the probe loop (step 2) name
    # the same `witness_<ID>` vals; parse_qnt is pure, so reading it earlier
    # changes nothing about step 2.
    probes_rel = fm.get("probes_file")
    probes_file = root / "specs" / probes_rel if probes_rel else None
    probes_ir = parse_qnt(probes_file) if probes_file and probes_file.exists() else None
    probe_vals = set(probes_ir["vals"]) if probes_ir else set()

    def selected(item):
        return not only or item.get("id") in only

    # One slice of the probe module per check (spec-probes.slice_text): the
    # same transition relation and the same formula, without the ghosts the
    # check never reads. Derived from the committed module, never committed.
    # Anything that cannot be sliced runs against the whole module, as before.
    slice_enabled = quint_cfg.get("slice_probes", True)
    slice_dir = root / "specs" / args.area / "gen" / "slices"
    slices = {}
    slice_lock = threading.Lock()

    def slice_for(val):
        if not (slice_enabled and probes_file and probes_file.exists()):
            return probes_file
        with slice_lock:
            if val in slices:
                return slices[val]
            target = probes_file
            try:
                sp = load_tool("spec-probes.py", "spec_probes_slicer")
                text = probes_file.read_text(encoding="utf-8")
                m = re.search(r"^module (\w+) \{$", text, re.MULTILINE)
                name = sp.slice_module_name(m.group(1), val)
                prefix = Path(os.path.relpath(probes_file.parent, slice_dir))
                slice_dir.mkdir(parents=True, exist_ok=True)
                target = slice_dir / f"{name}.qnt"
                target.write_text(sp.slice_text(text, [val], name, prefix.as_posix()),
                                  encoding="utf-8")
            except (KeyError, AttributeError, OSError, ValueError) as e:
                print(f"{'slice':<12} {val}: {e} — checked against the whole "
                      f"probe module", flush=True)
                target = probes_file
            slices[val] = target
            return target

    # The probe module carries parameters in slots (one ghost per type
    # position); every trace this run writes from it is restored to
    # _last<Param> names before anything reads it.
    model_ir = parse_qnt(qnt_file) if qnt_file.exists() else None
    slots = (param_slots(model_ir.get("action_param_types") or {},
                         mirrored_actions(model_ir)) if model_ir else {})

    def restore_names(trace_path):
        path = Path(trace_path)
        if not slots or not path.exists():
            return
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return   # load_trace reports it
        if isinstance(trace, dict) and any(
                k.startswith("_arg_") for k in ((trace.get("states") or [{}])[0])):
            restore_param_names(trace, slots)
            path.write_text(json.dumps(trace, separators=(",", ":")), encoding="utf-8")

    # Event-form probes, by name: the action the step must be and the
    # @while ghost that must be true at its end. Only these may take a
    # simulator trace — `_lastAction` is "init" in the initial state, so an
    # event probe cannot be violated there, and "hollow" (a verdict that
    # rests on Apalache returning the SHORTEST trace) cannot arise. A state
    # requirement's probe can hold at init, so it stays with Apalache.
    event_probes = {}
    for c in requirement_checks(area):
        if c["form"] != "event":
            continue
        pg = pre_ghost(c["rid"]) if c["while"] else None
        names = ([outcome_probe_name(c["rid"], o.get("name")) for o in c["outcomes"]]
                 if c["outcomes"] else [probe_name(c["rid"])])
        for n in names:
            event_probes[n] = (c["action"], pg)

    current_sha = compute_model_sha(root, args.area, area)
    # Each verdict is stamped with its own fingerprint: the transition
    # relation plus its own predicate (itf_tools.ModelIndex). A stamp an
    # older tool wrote is area-wide; one that still matches the whole model
    # is fresh by construction, so it is re-stamped here, not re-proven.
    index = model_index(root, args.area, area) if current_sha else None

    def sha_for(rid):
        return index.verdict_sha(rid) if index is not None else None

    if index is not None:
        restamp_area_wide(area, current_sha, index)

    # ── 0. Simulator pre-gate (advisory — never a verdict) ───────────────
    # The pre-gate's findings are the only ORDERING signal in the run: an
    # invariant it falsified is guaranteed to produce a counterexample and
    # will do so cheaply, and a probe it hit is about to be witnessed
    # cheaply. Both sets stay empty when the pre-gate is skipped, and the
    # ladder then keeps declaration order.
    simulation = None
    sim_falsified = set()      # quint_names the simulator violated
    sim_probe_hits = {}        # probe val name -> traces it appeared in
    sim_problems = []          # what --strict fails on
    if not args.no_simulate and qnt_file.exists():
        sim_pool = [i for i in (area.get("invariants") or [])
                    if i.get("quint_name") and selected(i)
                    and i.get("proof") not in ("structural", "smt")]
        # A transition invariant compares each step with the one before: it
        # reads the _prev* ghosts, which exist only in the probe module. So
        # it is simulated THERE, with initP/stepP — exactly as Apalache
        # checks it below. Against the main module it is an unknown name.
        sim_invs = [i["quint_name"] for i in sim_pool
                    if (i.get("over") or "model") != "probes"]
        sim_trans = [i["quint_name"] for i in sim_pool
                     if (i.get("over") or "model") == "probes"
                     and i["quint_name"] in probe_vals]
        # Requirement obligations live in the probe module too, and are
        # simulated beside the transition invariants for the same reason.
        sim_trans += [obligation_name(r["id"])
                      for r in area.get("requirements") or []
                      if r.get("id") and selected(r) and owes_obligation(r)
                      and obligation_name(r["id"]) in probe_vals]
        # And the completeness checks, which are transition properties too.
        sim_trans += [name for name, c in comp_checks.items()
                      if name in probe_vals and selected(c)]
        sim_probes = []
        for r in area.get("requirements") or []:
            if not (r.get("id") and selected(r)):
                continue
            names = [probe_name(r["id"])] + [
                outcome_probe_name(r["id"], oc.get("name"))
                for oc in ((r.get("witness") or {}).get("outcomes") or [])]
            # Screened through the probe's positive form: the probe itself
            # is a negation, true at init, so every run "witnesses" it.
            sim_probes += [n for n in names if n in probe_vals
                           and reach_name(n) in probe_vals]
        if sim_invs or sim_trans or sim_probes:
            quint = need_quint()
            backend = eval_backend if quint_supports(quint, "run", "--backend") else None
            simulation = {"ran_at": now_iso(), "max_samples": sim_samples,
                          "max_steps": sim_steps}
            if backend:
                simulation["backend"] = backend
            if sim_seed:
                simulation["seed"] = str(sim_seed)
            for key, names, target, init, step in (
                    ("invariants", sim_invs, qnt_file, None, None),
                    ("transition_invariants", sim_trans, probes_file, "initP", "stepP")):
                if not names:
                    continue
                res, detail, dur, _, falsified = run_simulate(
                    quint, target, names, sim_samples, sim_steps,
                    sim_timeout, seed=sim_seed, backend=backend,
                    init=init, step=step)
                simulation[key] = {"result": res, "checked": len(names)}
                if res in ("violation", "error"):
                    sim_problems.append(f"{key}: {res}" + (f" — {detail}" if detail else ""))
                if detail:
                    simulation[key]["detail"] = detail
                if falsified:
                    # Recorded, not merely used: this is what pass 1's order
                    # below is derived from, and an ordering nobody can read
                    # back is an ordering nobody can check.
                    simulation[key]["falsified"] = falsified
                    sim_falsified.update(falsified)
                # A simulator violation is real (it found an execution), so it
                # is worth shouting about — but the ledger entry still comes
                # from the model checker below, which also produces the trace.
                # An error is neither: the run never explored anything.
                mark = {"ok": "no violation in explored runs",
                        "violation": "VIOLATION FOUND — Apalache below will trace it",
                        "error": "ERROR — simulator did not run (not a verdict)",
                        }.get(res, res)
                what = ("invariant(s)" if key == "invariants"
                        else "transition invariant(s), probe module")
                print(f"{'simulate':<12} {mark:<26} ({dur:.1f}s, "
                      f"{len(names)} {what}, {sim_samples} samples)")
            if sim_probes and quint_supports(quint, "run", "--witnesses"):
                res, detail, dur, counts, _ = run_simulate(
                    quint, probes_file, [], sim_samples, sim_steps, sim_timeout,
                    seed=sim_seed, backend=backend, init="initP", step="stepP",
                    witnesses=[reach_name(n) for n in sim_probes])
                rows = []
                for name in sim_probes:
                    hits, explored = counts.get(reach_name(name), (None, None))
                    if hits is None:
                        sim_problems.append(f"{name}: the simulator reported no count")
                        continue
                    rows.append({"probe": name, "traces": hits,
                                 "explored": explored})
                    sim_probe_hits[name] = hits
                    if hits == 0:
                        sim_problems.append(f"{name}: reached in none of {explored} runs")
                        print(f"{'simulate':<12} {'0 traces':<26} {name} — "
                              f"unreachable in {explored} random runs "
                              f"(vacuity red flag; Apalache still decides)")
                    else:
                        print(f"{'simulate':<12} {f'{hits}/{explored} traces':<26} {name}")
                if rows:
                    simulation["witnesses"] = rows
                if not counts and res != "ok":
                    # The run did not get as far as counting — an error in
                    # the probe module. Said, not swallowed: a silent pre-
                    # screen reads exactly like one that found nothing wrong.
                    print(f"{'simulate':<12} {'ERROR':<26} witness pre-screen did "
                          f"not run: {detail}")
                print(f"{'simulate':<12} {'witness pre-screen':<26} ({dur:.1f}s, "
                      f"{len(rows)} probe(s) reported)")
            elif sim_probes:
                # An older quint: nothing was screened, and --strict must not
                # read that as nothing being wrong.
                sim_problems.append("this quint has no `run --witnesses`, so no "
                                    "probe was screened")
                print(f"{'simulate':<12} {'skipped':<26} witness pre-screen — this "
                      f"quint has no `run --witnesses`")

    if args.only_simulate:
        # Write ONLY the simulation block. Touching check_results.ran_at here
        # would make the area read as "checked" to every phase-flag consumer
        # on the strength of a run that verified nothing.
        cr = area.setdefault("check_results", {})
        if simulation is None:
            print("nothing to simulate (no Quint invariants or probes selected)")
        else:
            cr["simulation"] = simulation
            save_area(root, args.area, area)
            print(f"\nrecorded check_results.simulation in {area_path} "
                  f"(no verdicts written — simulation is advisory)")
        if getattr(args, "strict", False) and sim_problems:
            print("\n--strict: " + "; ".join(sim_problems))
            sys.exit(1)
        sys.exit(0)

    # ── 1. Invariants + properties ───────────────────────────────────────
    # Incremental write-back. Every verdict is merged into the ledger and
    # saved the MOMENT it is known, not at the end: a run stopped by Ctrl-C,
    # by its budget, or by a crash must keep what it actually proved. ran_at
    # is deliberately NOT written here — every phase-flag consumer reads it
    # as "this area was checked", and an interrupted run has not checked it.
    def flush():
        cr = area.setdefault("check_results", {})
        new_by_id = {c["id"]: c for c in checks}
        merged = []
        comp_ids = {c["id"] for c in comp_checks.values()}
        for prior in (cr.get("checks") or []):
            pid = prior.get("id")
            if (prior.get("kind") in ("coverage", "explanation", "verdict")
                    and pid not in comp_ids):
                continue   # the action, var or verdict it checked is gone
            merged.append(new_by_id.pop(pid) if pid in new_by_id else prior)
        merged.extend(new_by_id[cid] for cid in [c["id"] for c in checks]
                      if cid in new_by_id)
        cr["checks"] = merged
        # The CONFIGURED ceiling. The depth each check actually reached is
        # checks[].steps — with a ladder one global number cannot describe it.
        cr["max_steps"] = max_steps
        if cost:
            cr["cost"] = dict(cost)
        if simulation is not None:
            cr["simulation"] = simulation  # advisory; carries no formal_status
        save_area(root, args.area, area)

    # Apalache server congestion. A run-level budget is not enough on its
    # own: killing a timed-out `quint verify` leaves its query running inside
    # the persistent Apalache server that the NEXT check has to share, so one
    # slow check taxes every check after it — and the ladder's deep pass would
    # inherit exactly that. Once anything has timed out, the remaining bounded
    # runs go one-shot instead. Probe-gated, so a quint without the flag runs
    # the command line it always ran.
    server_state = {"dirty": False}

    def server_off():
        return (server_state["dirty"]
                and quint_supports(need_quint(), "verify", "--server"))

    def bounded_verify(job, steps):
        target = (slice_for(job["quint_name"]) if job["over"] == "probes"
                  else job["target"])
        result, detail, duration = run_verify(
            need_quint(), target, job["quint_name"], steps, timeout,
            init=job["init"], step=job["step"], out_itf=job["cex_path"],
            no_server=server_off(), endpoint=endpoint())
        if result == "timeout":
            server_state["dirty"] = True
        if result == "counterexample" and job["over"] == "probes":
            restore_names(job["cex_path"])
        return result, detail, duration

    def record_bounded(job, result, detail, duration, steps,
                       batched=False, note=None):
        """Write one bounded invariant's verdict, stamped with the depth it
        actually reached. `steps` is the honest half of the record: an
        invariant clean only to 3 and one clean to 10 are different claims,
        and check_results.max_steps can no longer tell them apart."""
        nonlocal bad
        iid, item, over = job["id"], job["item"], job["over"]
        cex_path, cex_rel = job["cex_path"], job["cex_rel"]
        entry = {
            "id": iid, "kind": "invariant", "quint_name": job["quint_name"],
            "result": result, "duration_s": round(duration, 1),
            "steps": steps,
        }
        if over == "probes":
            entry["over"] = "probes"
        if job.get("kind"):
            entry["kind"] = job["kind"]
        if job.get("obligation"):
            entry["kind"] = "obligation"
            item["checked_at"] = now_iso()
            item["steps"] = steps
        # What this verdict rested on: the transition relation and its own
        # predicate (an obligation's is its requirement's fingerprint).
        if index is not None and result in ("verified", "counterexample"):
            entry["model_sha"] = (sha_for(iid) if job.get("obligation")
                                  else index.check_sha(job["quint_name"]))
        if batched:
            entry["batched"] = True
        notes = [n for n in ((detail if result == "counterexample" else None), note)
                 if n]
        if notes:
            entry["note"] = "; ".join(notes)
        if result == "counterexample":
            bad += 1
            # Recorded only when a trace was actually written — a `trace`
            # pointing at a file that isn't there is the kind of ledger entry
            # the witness gate already FAILs on.
            if cex_path.exists():
                entry["trace"] = cex_rel
            prior = prior_checks.get(iid) or {}
            if (prior.get("result") == "counterexample"
                    and isinstance(prior.get("counterexample"), dict)):
                entry["counterexample"] = prior["counterexample"]
            item["formal_status"] = "counterexample-found"
            if job.get("obligation"):
                item.pop("model_sha", None)
                if cex_path.exists():
                    item["trace"] = cex_rel
        elif result == "verified":
            if cex_path.exists():
                cex_path.unlink()  # stale counterexample of a now-green check
            item["formal_status"] = "verified"
            if job.get("obligation"):
                item.pop("trace", None)
                if current_sha:
                    item["model_sha"] = sha_for(iid)
        else:
            bad += 1
            entry["error"] = detail
            # timeout/error: keep the prior formal_status untouched.
        checks.append(entry)
        flush()
        suffix = ""
        if result == "verified":
            suffix = f" (≤{steps} steps)"
            if over == "probes":
                suffix += " probes"
            if batched:
                suffix += " batched"
        print(f"{iid:<12} {result + suffix:<26} ({duration:.1f}s)"
              + (f"  {detail}" if detail and result == "error" else "")
              + (f"  {note}" if note else ""))

    def record_single(entry, item=None, status=None):
        """Record one single-pass check (SMT, structural, inductive,
        temporal). No `steps`: a step ladder is meaningless for all four —
        two are unbounded, one is scope-bounded, one has no step bound."""
        nonlocal bad
        if entry.get("result") != "verified":
            bad += 1
        if item is not None and status:
            item["formal_status"] = status
        checks.append(entry)
        flush()
        return entry

    # Bounded jobs are COLLECTED rather than run: the ladder needs the whole
    # set before it can decide what to run first and what to run deep.
    ladder = []

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
                record_single(run_smt_check(item, iid, root, args.area,
                                            need_z3, timeout))
                continue
            if structural:
                record_single(run_structural_check(
                    item, iid, als_rel, als_file, root, args.area,
                    need_alloy, alloy_solver, alloy_timeout,
                ))
                continue
            # Inductive proof is opt-in per invariant (proof: "inductive").
            # Default invariants stay bounded — behavior unchanged.
            inductive = (kind == "invariant" and item.get("proof") == "inductive")
            # A property is a LIVENESS formula, so it is checked with
            # --temporal on a backend that supports temporal operators, not
            # with --invariant. There is no step bound and no ITF trace on
            # that path; see run_verify.
            temporal = (kind == "property")
            # An invariant declared `over: "probes"` is a TRANSITION property:
            # it reads the `_prev*` ghosts, which exist only in the probe
            # module, so it has to be checked there with initP/stepP. Without
            # this the whole class ("X never changes", "Y only moves one way")
            # has no checkable form and ends up as prose.
            over = (item.get("over") or "model") if kind == "invariant" else "model"
            target = qnt_file
            init = step = None
            if over == "probes":
                if not probes_file or not probes_file.exists():
                    record_single({"id": iid, "kind": kind, "quint_name": qname,
                                   "over": "probes", "result": "error",
                                   "error": ("over: 'probes' but formal_model."
                                             "probes_file is missing — generate "
                                             "it with tools/spec-probes.py, then "
                                             "re-run.")})
                    print(f"{iid:<12} {'error':<26} no probes module for over:probes")
                    continue
                target, init, step = probes_file, "initP", "stepP"
            cex_rel = f"{args.area}/traces/{iid}.cex.itf.json"
            cex_path = root / "specs" / cex_rel
            if not inductive and not temporal:
                # The one laddered class: bounded invariants. Queued, not run.
                ladder.append({
                    "item": item, "id": iid, "quint_name": qname, "over": over,
                    "target": target, "init": init, "step": step,
                    "cex_rel": cex_rel, "cex_path": cex_path,
                    "seq": len(ladder),
                })
                continue
            # Unbounded (inductive) or unboundable (temporal): single pass,
            # exactly the path each took before the ladder existed.
            result, detail, duration = run_verify(
                need_quint(), target, qname, max_steps, timeout,
                init=init, step=step,
                out_itf=cex_path, inductive=inductive, temporal=temporal,
                backend=temporal_backend if temporal else None,
            )
            entry = {
                "id": iid, "kind": kind, "quint_name": qname,
                "result": result, "duration_s": round(duration, 1),
            }
            if inductive:
                entry["proof"] = "inductive"
            if over == "probes":
                entry["over"] = "probes"
            if temporal:
                entry["backend"] = temporal_backend
            if result == "counterexample":
                if detail:
                    entry["note"] = detail
                # Recorded only when a trace was actually written: TLC emits
                # none, and a `trace` pointing at a file that isn't there is
                # the kind of ledger entry the witness gate already FAILs on.
                if cex_path.exists():
                    entry["trace"] = cex_rel
                prior = prior_checks.get(iid) or {}
                if (prior.get("result") == "counterexample"
                        and isinstance(prior.get("counterexample"), dict)):
                    entry["counterexample"] = prior["counterexample"]
                record_single(entry, item, "counterexample-found")
            elif result == "verified":
                if cex_path.exists():
                    cex_path.unlink()  # stale counterexample of a now-green check
                record_single(entry, item,
                              "verified-inductive" if inductive else "verified")
            else:
                entry["error"] = detail
                # timeout/error: keep the prior formal_status untouched.
                record_single(entry)
            suffix = ""
            if result == "verified":
                if over == "probes":
                    suffix = " (probes)" + (" inductive" if inductive else "")
                elif inductive:
                    suffix = " (inductive)"
                elif temporal:
                    suffix = f" ({temporal_backend})"
            print(f"{iid:<12} {result + suffix:<26} ({duration:.1f}s)"
                  + (f"  {detail}" if detail and result == "error" else ""))

    # ── 1b. Requirement obligations ──────────────────────────────────────
    # The universal half of every requirement: each reachable step it governs
    # satisfies it. An obligation is an invariant of the probe module, so it
    # rides the same ladder as a transition invariant; its verdict is written
    # on the requirement (obligation.*), not on an invariant.
    if not args.no_witness:
        for req in area.get("requirements") or []:
            rid = req.get("id")
            if not rid or not selected(req) or not owes_obligation(req):
                continue
            ob = req.setdefault("obligation", {})
            oname = obligation_name(rid)
            if (ob.get("formal_status") == "verified" and current_sha
                    and ob.get("model_sha") == sha_for(rid)):
                print(f"{rid:<12} {'obligation fresh':<26} (nothing it rests on changed — skipped)")
                continue
            if not probes_file or oname not in probe_vals:
                print(f"{rid:<12} {'no-obligation':<26} ('{oname}' missing — "
                      f"regenerate the probe module with tools/spec-probes.py)")
                bad += 1
                continue
            cex_rel = f"{args.area}/traces/{rid}.violation.itf.json"
            ladder.append({
                "item": ob, "id": rid, "quint_name": oname, "over": "probes",
                "target": probes_file, "init": "initP", "step": "stepP",
                "cex_rel": cex_rel, "cex_path": root / "specs" / cex_rel,
                "seq": len(ladder), "obligation": True,
            })

    # ── 1c. Completeness: coverage, explanation, verdicts ────────────────
    # Every step of every action is under some requirement or verdict; every
    # observable change is one a requirement asked for; every IMPOSSIBLE /
    # NO-OP verdict is true of the model. All three are invariants of the
    # probe module, so they ride the ladder too. Their verdicts live in
    # check_results.checks only: a gap belongs to the action, not to any
    # one requirement.
    if not args.no_witness:
        for name, c in comp_checks.items():
            if not selected(c):
                continue
            if not probes_file or name not in probe_vals:
                print(f"{c['id']:<12} {'missing':<26} ('{name}' is not in the "
                      f"probe module — regenerate it with tools/spec-probes.py)")
                bad += 1
                continue
            cex_rel = f"{args.area}/traces/{c['id']}.cex.itf.json"
            ladder.append({
                "item": {}, "id": c["id"], "quint_name": name, "over": "probes",
                "target": probes_file, "init": "initP", "step": "stepP",
                "cex_rel": cex_rel, "cex_path": root / "specs" / cex_rel,
                "seq": len(ladder), "kind": c["kind"],
            })

    # ── 1-ladder. Two passes over the bounded invariants ─────────────────
    # Pass 1 is ordered by the only information the run already holds: an
    # invariant the simulator falsified WILL produce a counterexample and
    # will produce it cheaply, so it goes first. Everything the simulator
    # could not falsify is the expensive tail. Declaration order breaks ties,
    # so a run with no pre-gate behaves exactly as it did before.
    ladder.sort(key=lambda j: (0 if j["quint_name"] in sim_falsified else 1,
                               j["seq"]))

    def batch_pass(jobs, steps):
        """Try the jobs in ONE batched run per module at `steps`. Returns
        ({clean id: its batch's duration}, total duration); a job not in the
        map takes the per-id path, which is exactly the path it took before
        batching existed — so this can never turn a red result green or
        mis-attribute one."""
        if not batch_enabled:
            return {}, 0.0
        # One batch per module: a probe-module check runs against a
        # different file with a different init/step, so it cannot share a
        # run with the model's invariants — batched with them it would be
        # checked against the wrong module.
        groups = {}
        for j in jobs:
            groups.setdefault((str(j["target"]), j["init"], j["step"]), []).append(j)
        clean, total = {}, 0.0
        for (target, init, step), batchable in groups.items():
            if len(batchable) < 2 or not Path(target).exists():
                continue
            # A batch the simulator already saw fail cannot come back green:
            # it would cost one run of the whole module and decide nothing.
            if any(j["quint_name"] in sim_falsified for j in batchable) or \
                    (simulation or {}).get(
                        "transition_invariants" if init else "invariants",
                        {}).get("result") == "violation":
                print(f"{'batch':<12} {'skipped — per-id below':<26} (the "
                      f"simulator already found a violation here)")
                continue
            quint = need_quint()
            if not quint_supports(quint, "verify", "--invariants"):
                return {}, 0.0
            res, _, dur = run_verify_batch(
                quint, target, [j["quint_name"] for j in batchable], steps,
                timeout, no_server=server_off(), init=init, step=step)
            total += dur
            if res == "timeout":
                server_state["dirty"] = True
            where = "probes" if init else "model"
            if res == "verified":
                label = f"verified (≤{steps} steps)"
                print(f"{'batch':<12} {label:<26} ({dur:.1f}s, "
                      f"{len(batchable)} {where} checks in one run)")
                clean.update({j["id"]: dur for j in batchable})
            else:
                print(f"{'batch':<12} {res + ' — per-id below':<26} "
                      f"({dur:.1f}s, {where})")
        return clean, total

    # How each witness trace was found, keyed by its path: probe_run's
    # callers stamp it on the witness (found_by, and for a simulator trace
    # its own length as steps).
    found = {}
    sim_tried = set()

    def stamp(block, trace_path):
        engine, n = found.get(str(trace_path), ("apalache", None))
        block["found_by"] = engine
        block.pop("revalidated_by", None)
        if engine == "simulator":
            # The trace's own length: the simulator ran to no bound worth
            # recording, and this is the depth the witness actually needed.
            block["steps"] = n

    def simulator_witness(pname, trace_path):
        """The simulator's trace for an event-form probe, or None. Tried
        once per probe per run, on the probe's slice: for a probe the
        pre-screen hit, and for one it could not report on (on a large
        module the whole-module pre-screen can run out of time — the slice
        is smaller). Not for a probe it explored and never reached: that one
        is Apalache's to decide. Whatever this does not hand over goes to
        Apalache exactly as before."""
        if not (sim_witnesses and not args.no_simulate
                and pname in event_probes and pname not in sim_tried
                and sim_probe_hits.get(pname) != 0):
            return None
        sim_tried.add(pname)
        quint = need_quint()
        backend = eval_backend if quint_supports(quint, "run", "--backend") else None
        # Bounded by the SHALLOW pass, not the pre-screen's depth: a random
        # walk is not the shortest run, and a witness is read and replayed —
        # one that wanders ten steps to show a three-step behavior is worse
        # evidence than the one Apalache would construct.
        r, d, dur = run_simulate_trace(
            quint, slice_for(pname), pname, sim_samples, shallow_steps, sim_timeout,
            trace_path, seed=sim_seed, backend=backend, init="initP",
            step="stepP")
        if r != "counterexample":
            return None
        restore_names(trace_path)
        trace, errs = load_trace(trace_path)
        action, pg = event_probes[pname]
        if errs or not simulated_witness_ok(trace, action, pg):
            # Not a trace we can stand behind; Apalache constructs one.
            Path(trace_path).unlink()
            return None
        found[str(trace_path)] = ("simulator", len(trace["states"]) - 1)
        return r, "", dur

    def run_probe(pname, trace_path, steps):
        sim = simulator_witness(pname, trace_path)
        if sim is not None:
            return sim
        found[str(trace_path)] = ("apalache", steps)
        result, detail, duration = run_verify(
            need_quint(), slice_for(pname), pname, steps, timeout,
            init="initP", step="stepP", out_itf=trace_path,
            no_server=server_off(), endpoint=endpoint())
        if result == "timeout":
            server_state["dirty"] = True
        if result == "counterexample":
            restore_names(trace_path)
        return result, detail, duration

    def probe_order_key(item):
        """Probes the simulator HIT first — they are about to be witnessed in
        milliseconds. Probes it never reached last: those are the ones that
        will run to full depth and still find nothing. Probes it said nothing
        about keep declaration order in between."""
        seq, req = item
        hits = sim_probe_hits.get(probe_name(req.get("id") or ""))
        rank = 1 if hits is None else (0 if hits > 0 else 2)
        return (rank, seq)

    def probe_run(pname, trace_path, steps):
        """One probe's answer: the one pass 1 already started in parallel if
        there is one, else run here."""
        return consume(("probe", pname, str(trace_path), steps),
                       run_probe, pname, trace_path, steps)

    def is_fresh(block, rid):
        """Witnessed against this model, with its trace still on disk and
        valid — the probe will be skipped, so it is not planned."""
        if not (block.get("status") == "witnessed" and current_sha
                and block.get("model_sha") == sha_for(rid)):
            return False
        t_rel = block.get("trace")
        t_path = root / "specs" / t_rel if t_rel else None
        return bool(t_path and t_path.exists() and not load_trace(t_path)[1])

    def planned_probes():
        """(probe, trace path) for every shallow probe
        the witness loop below will run, in the order it will run them —
        what pass 1 submits ahead and counts in its projection. A probe
        missed here is not lost: the loop runs it where it is consumed."""
        out = []
        if probes_ir is None:
            return out
        for _, req in sorted(enumerate(area.get("requirements", []) or []),
                             key=probe_order_key):
            rid = req.get("id")
            if (not rid or not selected(req) or req.get("status") == "deferred"
                    or req.get("type") == "non-functional"):
                continue
            w = req.get("witness") or {}
            if req.get("modality") == "may":
                for oc in w.get("outcomes") or []:
                    oname = oc.get("name") or "?"
                    pname = outcome_probe_name(rid, oname)
                    if oc.get("predicate") and not is_fresh(oc, rid) and pname in probe_vals:
                        tp = root / "specs" / outcome_trace_rel(args.area, rid, oname)
                        out.append((pname, tp))
                continue
            if witness_exempt(req) or not owes_obligation(req) or is_fresh(w, rid):
                continue
            pname = probe_name(rid)
            if pname in probe_vals:
                tp = root / "specs" / f"{args.area}/traces/{rid}.itf.json"
                out.append((pname, tp))
        return out

    # ── Running checks in parallel ───────────────────────────────────────
    # The checks are independent, and one Apalache server answers concurrent
    # queries concurrently, so a pass is submitted N-wide up front and then
    # CONSUMED in its own order: each verdict is still written the moment the
    # loop reaches it, in the order it always was, by this thread alone — the
    # ledger never has two writers. A check nobody submitted (or --parallel
    # 1) simply runs where it is consumed, which is the serial path unchanged.
    pool = ThreadPoolExecutor(max_workers=parallel) if parallel > 1 else None
    # One Apalache port per worker thread (apalache.server_port + 1 + k); the
    # main thread keeps quint's default, so a serial run is unchanged.
    port_base = apalache.get("server_port", 8822)
    worker = threading.local()
    worker_ids = {"next": 0, "lock": threading.Lock()}

    def endpoint():
        if pool is None or threading.current_thread() is threading.main_thread():
            return None
        if not quint_supports(need_quint(), "verify", "--server-endpoint"):
            return None
        if not hasattr(worker, "port"):
            with worker_ids["lock"]:
                worker.port = port_base + 1 + worker_ids["next"]
                worker_ids["next"] += 1
        return f"localhost:{worker.port}"
    if pool is not None:
        _POOLS.append(pool)
    futures = {}
    timing = {"lock": threading.Lock(), "durations": [], "projected": False}

    def timed(fn):
        def run(*a):
            started = time.monotonic()
            out = fn(*a)
            with timing["lock"]:
                timing["durations"].append(time.monotonic() - started)
                ready = (not timing["projected"]
                         and len(timing["durations"]) >= PROJECT_AFTER)
                if ready:
                    timing["projected"] = True
            if ready:
                project_pass1()
            return out
        return run

    def submit(key, fn, *a):
        if pool is not None and key not in futures:
            futures[key] = pool.submit(timed(fn), *a)

    def consume(key, fn, *a):
        fut = futures.pop(key, None)
        return fut.result() if fut is not None else timed(fn)(*a)

    # ── Pass-1 projection ────────────────────────────────────────────────
    # Pass 1 is never budget-gated, so nothing else bounds it — and on
    # a model the size of a real brownfield area it can run for most of an hour
    # with no word about it. After the first few checks the run says what
    # pass 1 will cost and whether the budget will reach the deep pass, and
    # records it, so a readback can say why every ✓ rests on the shallow
    # bound instead of leaving the reader to guess.
    pass1 = {"total": 0, "started": None}

    def project_pass1():
        with timing["lock"]:
            durs = list(timing["durations"])
        if not durs or not pass1["total"]:
            return
        avg = sum(durs) / len(durs)
        left = max(pass1["total"] - len(durs), 0)
        projected = (time.monotonic() - pass1["started"]) + avg * left / parallel
        cost["pass1"] = {"checks": pass1["total"], "after_checks": len(durs),
                         "avg_check_s": round(avg, 1),
                         "projected_s": round(projected),
                         "parallel": parallel, "budget_seconds": budget_seconds}
        fits = projected < budget_seconds
        cover = ("leaves budget for pass 2" if fits else
                 "budget covers pass 2 only if pass 1 ends early — the deep "
                 "pass will likely not run")
        print(f"{'projection':<12} pass 1: {len(durs)}/{pass1['total']} checks, "
              f"~{fmt_secs(avg)}/check"
              + (f" × {parallel} parallel" if parallel > 1 else "")
              + f" → ~{fmt_secs(projected)} projected; budget "
              f"{fmt_secs(budget_seconds)} {cover}", flush=True)

    # Stale witnesses first: one `quint test` replays every stored trace
    # through the new probe module, and each that still holds is re-stamped
    # here — so is_fresh() leaves it out of the plan below.
    if (replay_witnesses and not args.no_witness and index is not None
            and probes_file and probes_file.exists() and qnt_file.exists()):
        if replay_stale_witnesses(root, args.area, area, probes_file, qnt_file,
                                  sha_for, selected, need_quint, timeout):
            flush()

    # Pass 1 (shallow). Never budget-gated: it is the cheap half AND the half
    # that carries the findings, so every selected check gets at least a
    # shallow verdict even when the budget is already spent.
    deep_queue = []
    pass1["started"] = time.monotonic()
    witness_plan = planned_probes() if not args.no_witness else []
    clean_ids = batch_pass(ladder, shallow_steps)[0] if ladder else {}
    pending = [j for j in ladder if j["id"] not in clean_ids]
    pass1["total"] = len(pending) + len(witness_plan)
    for job in pending:
        submit(("ladder", job["id"], shallow_steps), bounded_verify, job, shallow_steps)
    # The witness probes join the same queue, so pass 1 stays N-wide from
    # the first ladder check to the last probe.
    for pname, trace_path in witness_plan:
        submit(("probe", pname, str(trace_path), shallow_steps),
               run_probe, pname, trace_path, shallow_steps)
    for job in ladder:
        if job["id"] in clean_ids:
            job["shallow"] = ("verified", "", clean_ids[job["id"]], True)
        else:
            r, d, dur = consume(("ladder", job["id"], shallow_steps),
                                bounded_verify, job, shallow_steps)
            job["shallow"] = (r, d, dur, False)
        r, d, dur, was_batched = job["shallow"]
        if r == "verified" and shallow_steps < max_steps:
            deep_queue.append(job)   # clean, and there is depth left to add
        else:
            # A counterexample is FINAL — depth cannot unfind it. So is a
            # timeout or an error: running the same check deeper will not
            # make it answer.
            record_bounded(job, r, d, dur, shallow_steps, batched=was_batched)

    # Pass 2 (deep). Only the checks clean at shallow, and only while the run
    # budget lasts. A check the budget stopped keeps its SHALLOW verdict and
    # says so — a shallow pass is never silently upgraded to a deep one.
    # Run AFTER every shallow witness probe (below): pass 1 is the whole
    # shallow sweep, and the budget pays for depth only once it is done.
    def budget_note():
        return (f"deep pass skipped: the run budget of {budget_seconds}s was "
                f"spent, so this verdict is the shallow pass's ≤{shallow_steps} "
                f"steps, not the configured {max_steps}")

    def deep_or_none(job):
        """The deep run of one ladder check, or None when the budget was
        spent before it could start."""
        if budget_left() <= 0:
            return None
        return bounded_verify(job, max_steps)

    def deep_ladder():
        if not deep_queue:
            return
        if budget_left() <= 0:
            print(f"{'budget':<12} {'exhausted before deep pass':<26} "
                  f"({budget_seconds}s spent; {len(deep_queue)} check(s) stay "
                  f"at ≤{shallow_steps} steps)")
            cost["deep_skipped"] = cost.get("deep_skipped", 0) + len(deep_queue)
            for job in deep_queue:
                r, d, dur, wb = job["shallow"]
                record_bounded(job, r, d, dur, shallow_steps, batched=wb,
                               note=budget_note())
            return
        # No batch at depth when checks run in parallel: one run of the whole
        # module to the full depth is the slowest query this runner can make,
        # and on a large model it times out and decides nothing, while the
        # same checks per id, on slices, run N-wide.
        clean_ids = (batch_pass(deep_queue, max_steps)[0] if parallel == 1
                     else {})
        for job in deep_queue:
            if job["id"] not in clean_ids:
                submit(("ladder", job["id"], max_steps), deep_or_none, job)
        stopped = 0
        for job in deep_queue:
            sr, sd, sdur, swb = job["shallow"]
            if job["id"] in clean_ids:
                record_bounded(job, "verified", "", clean_ids[job["id"]], max_steps,
                               batched=True)
                continue
            res = consume(("ladder", job["id"], max_steps), deep_or_none, job)
            if res is None:
                stopped += 1
                record_bounded(job, sr, sd, sdur, shallow_steps,
                               batched=swb, note=budget_note())
                continue
            r, d, dur = res
            if r in ("verified", "counterexample"):
                record_bounded(job, r, d, dur, max_steps)
            else:
                # The deep run did not answer (timeout/error). Recording
                # THAT would discard a verdict already held — clean to
                # shallow_steps. Keep the shallow one and say what the
                # deep pass did instead.
                record_bounded(job, sr, sd, sdur, shallow_steps,
                               batched=swb,
                               note=(f"deep pass at {max_steps} steps "
                                     f"returned {r}"
                                     + (f": {d}" if d else "")))
        if stopped:
            cost["deep_skipped"] = cost.get("deep_skipped", 0) + stopped
            print(f"{'budget':<12} {'exhausted during deep pass':<26} "
                  f"({budget_seconds}s spent; {stopped} check(s) stay at "
                  f"≤{shallow_steps} steps)")

    # ── 2. Witness probes ────────────────────────────────────────────────
    # Same ladder, same reason. A witness probe's GOOD outcome is a
    # counterexample (the violation trace IS the witness), so a shallow run
    # that finds one is final and cheap; depth is only needed to keep FAILING
    # to find one, which is the expensive half — and the answer nobody wants.
    witness_deep = []   # single-witness reqs that found nothing at shallow
    may_deep = []       # `may` outcomes that found nothing at shallow
    if not args.no_witness:
        ordered = sorted(enumerate(area.get("requirements", []) or []),
                         key=probe_order_key)
        for _, req in ordered:
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
                    probes_file, root, args.area, probe_run, shallow_steps,
                    sha_for(rid),
                    deep_queue=may_deep if shallow_steps < max_steps else None,
                    stamp=stamp)
                flush()
                continue
            exempt = witness_exempt(req)
            if exempt:
                # Nothing to find, by what the requirement IS — derived, never
                # authored. Its obligation (above) carries the proof.
                req["witness"] = witness
                witness["status"] = "skipped"
                print(f"{rid:<12} no witness       ({exempt})")
                continue
            if not owes_obligation(req):
                print(f"{rid:<12} not-formal       (no @shall / @outcome yet — "
                      f"formalize it via /spec, then re-run)")
                bad += 1
                continue
            req["witness"] = witness  # persist only for reqs we actually process
            if (witness.get("status") == "witnessed"
                    and current_sha and witness.get("model_sha") == sha_for(rid)):
                # Fresh by sha — but only if the trace is actually present
                # and valid; a deleted trace with a surviving stamp is not
                # fresh, it's gone.
                t_rel = witness.get("trace")
                t_path = root / "specs" / t_rel if t_rel else None
                if t_path and t_path.exists() and not load_trace(t_path)[1]:
                    print(f"{rid:<12} fresh            (stamp current — probe skipped)")
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
            result, detail, duration = probe_run(pname, trace_path, shallow_steps)
            witness["checked_at"] = now_iso()
            witness["steps"] = shallow_steps
            if result == "counterexample":
                # Violation of the negated predicate = the behavior happened:
                # the trace IS the witness.
                trace, errs = load_trace(trace_path)
                if errs:
                    print(f"{rid:<12} error            (trace written but invalid: {errs[0]})")
                    witness["status"] = "not-run"
                    bad += 1
                    flush()
                    continue
                if is_hollow(trace):
                    # One state = the predicate held before anything ran. The
                    # checker returns the shortest counterexample, so there was
                    # no shorter run to find: nothing happened. Refused at the
                    # moment it would have been minted, because a false proof
                    # that reaches the ledger is indistinguishable from a real
                    # one to everything downstream.
                    witness["status"] = "hollow"
                    witness.pop("model_sha", None)
                    witness.pop("found_by", None)
                    witness.pop("revalidated_by", None)
                    witness["trace"] = trace_rel
                    bad += 1
                    print(f"{rid:<12} HOLLOW           (predicate already true in the "
                          f"initial state — 1-state counterexample proves nothing "
                          f"happened)")
                    flush()
                    continue
                witness["status"] = "witnessed"
                witness["trace"] = trace_rel
                if current_sha:
                    witness["model_sha"] = sha_for(rid)
                stamp(witness, trace_path)
                print(f"{rid:<12} WITNESSED        -> specs/{trace_rel} ({duration:.1f}s"
                      + (", simulator" if witness["found_by"] == "simulator" else "")
                      + ")")
            elif result == "verified":
                # Recorded PESSIMISTICALLY at the shallow bound, then queued:
                # an interrupted run keeps the honest finding, and the deep
                # pass can still upgrade it to witnessed.
                witness["status"] = "no-witness"
                witness.pop("model_sha", None)
                witness.pop("found_by", None)
                witness.pop("revalidated_by", None)
                bad += 1
                if shallow_steps < max_steps:
                    witness_deep.append({"req": req, "rid": rid, "witness": witness,
                                         "pname": pname, "trace_rel": trace_rel,
                                         "trace_path": trace_path})
                else:
                    print(f"{rid:<12} NO-WITNESS       (unreachable up to "
                          f"{shallow_steps} steps — impossible guard, missing "
                          f"action, or bound too small)")
            else:
                bad += 1
                print(f"{rid:<12} {result:<16} {detail}")
            flush()

    # ── 2-deep. The probes that found nothing at the shallow bound ───────
    # The only place deepening can change the answer, and the only expensive
    # part of the witness pass. Budget-gated: a probe the budget stopped keeps
    # its shallow no-witness AND the bound it was found at, so nobody reads
    # "unreachable" as "unreachable at the configured depth".
    def deep_probe_or_none(pname, trace_path):
        if budget_left() <= 0:
            return None
        return run_probe(pname, trace_path, max_steps)

    def deepen_probe(pname, trace_path):
        """Re-run one probe at max_steps. Returns (result, detail, duration)
        or None when the budget is spent."""
        return consume(("deep", pname, str(trace_path)),
                       deep_probe_or_none, pname, trace_path)

    # Pass 1 ends here. What it cost is recorded whether or not a
    # projection was printed: it is the number the deep pass's absence is
    # explained by.
    if pass1["total"]:
        p1 = cost.setdefault("pass1", {"checks": pass1["total"],
                                       "parallel": parallel,
                                       "budget_seconds": budget_seconds})
        p1["elapsed_s"] = round(time.monotonic() - pass1["started"])
        flush()

    # Pass 2: the deep witness probes are queued first — an unreachable
    # behavior is the finding the deep pass exists for — then the ladder's.
    for job in witness_deep + may_deep:
        submit(("deep", job["pname"], str(job["trace_path"])),
               deep_probe_or_none, job["pname"], job["trace_path"])
    deep_ladder()

    for job in witness_deep:
        rid, witness = job["rid"], job["witness"]
        res = deepen_probe(job["pname"], job["trace_path"])
        if res is None:
            print(f"{rid:<12} NO-WITNESS       (unreachable up to {shallow_steps} "
                  f"steps; the {budget_seconds}s run budget stopped the deep pass, "
                  f"so {max_steps} steps was never tried)")
            flush()
            continue
        result, detail, duration = res
        witness["checked_at"] = now_iso()
        if result == "counterexample":
            trace, errs = load_trace(job["trace_path"])
            if errs:
                print(f"{rid:<12} error            (trace written but invalid: {errs[0]})")
                witness["status"] = "not-run"
                flush()
                continue
            if is_hollow(trace):
                witness["status"] = "hollow"
                witness.pop("model_sha", None)
                witness.pop("found_by", None)
                witness.pop("revalidated_by", None)
                witness["trace"] = job["trace_rel"]
                witness["steps"] = max_steps
                print(f"{rid:<12} HOLLOW           (predicate already true in the "
                      f"initial state — 1-state counterexample proves nothing "
                      f"happened)")
                flush()
                continue
            # The deep pass found it: the shallow bound was simply too small.
            witness["status"] = "witnessed"
            witness["trace"] = job["trace_rel"]
            witness["steps"] = max_steps
            witness["found_by"] = "apalache"
            witness.pop("revalidated_by", None)
            if current_sha:
                witness["model_sha"] = sha_for(rid)
            bad -= 1   # the provisional no-witness the shallow pass counted
            print(f"{rid:<12} WITNESSED        -> specs/{job['trace_rel']} "
                  f"({duration:.1f}s, needed {max_steps} steps)")
        elif result == "verified":
            witness["steps"] = max_steps
            print(f"{rid:<12} NO-WITNESS       (unreachable up to {max_steps} steps "
                  f"— impossible guard, missing action, or bound too small)")
        else:
            # The deep run did not answer. The shallow finding stands, with
            # its own bound — not the one that was never reached.
            print(f"{rid:<12} NO-WITNESS       (unreachable up to {shallow_steps} "
                  f"steps; the deep pass at {max_steps} returned {result})")
        flush()

    # Keyed by identity, not by value: two `may` requirements can carry
    # identical witness blocks, and rolling up only one of them would leave
    # the other's status derived from outcomes that have since changed.
    touched_may = {}
    for job in may_deep:
        oc, label = job["oc"], job["label"]
        res = deepen_probe(job["pname"], job["trace_path"])
        touched_may.setdefault(id(job["witness"]), (job["witness"], job["rid"]))
        if res is None:
            print(f"{label:<24} NO-WITNESS       (unreachable up to {shallow_steps} "
                  f"steps; the {budget_seconds}s run budget stopped the deep pass)")
            continue
        result, detail, duration = res
        oc["checked_at"] = now_iso()
        if result == "counterexample":
            trace, errs = load_trace(job["trace_path"])
            if errs:
                oc["status"] = "not-run"
                print(f"{label:<24} error            (invalid trace: {errs[0]})")
                continue
            if is_hollow(trace):
                oc["status"] = "hollow"
                oc.pop("model_sha", None)
                oc.pop("found_by", None)
                oc.pop("revalidated_by", None)
                oc["trace"] = job["trace_rel"]
                oc["steps"] = max_steps
                print(f"{label:<24} HOLLOW           (predicate already true in the "
                      f"initial state)")
                continue
            oc["status"] = "witnessed"
            oc["trace"] = job["trace_rel"]
            oc["steps"] = max_steps
            oc["found_by"] = "apalache"
            oc.pop("revalidated_by", None)
            if current_sha:
                oc["model_sha"] = sha_for(job["rid"])
            bad -= 1
            print(f"{label:<24} WITNESSED        -> specs/{job['trace_rel']} "
                  f"({duration:.1f}s, needed {max_steps} steps)")
        elif result == "verified":
            oc["steps"] = max_steps
            print(f"{label:<24} NO-WITNESS       (permitted outcome unreachable up "
                  f"to {max_steps} steps — the permission is narrower than written)")
        else:
            print(f"{label:<24} NO-WITNESS       (unreachable up to {shallow_steps} "
                  f"steps; the deep pass at {max_steps} returned {result})")
    for witness, rid in touched_may.values():
        # The roll-up the shallow pass wrote was derived from outcomes that
        # have since changed. Derive it again from what is now recorded.
        roll_up_outcomes(witness, sha_for(rid))
    if touched_may:
        flush()

    if pool is not None:
        pool.shutdown(wait=True)

    # ── 3. Write back ────────────────────────────────────────────────────
    # The ledger itself was already merged and saved after every check (see
    # flush() above) — MERGED, never replaced wholesale, because a --only run
    # (or an all-fresh run) must not erase results it didn't re-derive. What is
    # left for the end is the one field that means "this area was checked":
    # ran_at. Written here and nowhere else, so an interrupted run keeps every
    # verdict it earned without claiming to have been a full check.
    flush()
    cr = area["check_results"]
    cr["ran_at"] = now_iso()
    # What the model looked like, part by part, so a later stale stamp can
    # say what moved (itf_tools.stale_changes). A --only run re-derived only
    # some verdicts; the rest still rest on the parts recorded before it.
    index = model_index(root, args.area, area)
    if index is not None and (not args.only or not cr.get("model_parts")):
        cr["model_parts"] = dict(sorted(index.parts.items()))
    save_area(root, args.area, area)
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
    area_path = records_path(root, args.area)
    area = load_area_or_fail(root, args.area)
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
                   "comparator with /spec-code-generate --parallel first.")
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
        fail_setup(f"{repo_root / parallel} does not exist \u2014 run /spec-code-generate "
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
    save_area(root, args.area, area)

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
        proc = run_group(command, shell=True, cwd=str(cwd), timeout=timeout)
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


def load_tool(filename, modname):
    """A sibling tool whose file name is not an importable module name."""
    if modname not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            modname, Path(__file__).parent / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[modname] = mod
    return sys.modules[modname]


def run_traces_status(root, area_name, area):
    """[(run, label, status)] for the area's example and journey run traces —
    tools/spec-runs.py's own reading of them, so the two cannot disagree."""
    return load_tool("spec-runs.py", "spec_runs").status(root, area_name, area)


def cmd_verify(args):
    root = Path(args.root)
    area_path = records_path(root, args.area)
    area = load_area_or_fail(root, args.area)
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
    # A rejection produces no state change, so it has no witness trace. Its
    # evidence is an example: a run that reaches the blocking state and ends
    # with the refused call (`.fail()`) — replayed against the code by the
    # conformance harness, which asserts the code refuses and nothing moves.
    rejections = [r for r in (area.get("requirements") or []) if is_rejection(r)]
    shown = {ref.split(".", 1)[-1] for ex in (area.get("examples") or [])
             for ref in (ex.get("refs") or [])}
    unshown = [r.get("id", "?") for r in rejections if r.get("id") not in shown]
    if unshown:
        bad += 1
        notes.append(f"refusal preflight: {len(unshown)} rejection(s) with no example")
        print(f"PREFLIGHT: {len(unshown)} rejection requirement(s) shown by no "
              f"example — " + ", ".join(unshown))

    # ── 1c. Run-trace preflight ──────────────────────────────────────────
    # Every example and journey run is replayed too (tools/spec-runs.py
    # writes them under traces/runs/, labelled like witness traces, a refused
    # call marked `_refused`). A trace made from an older model replays an
    # older spec, so — like a stale witness — it blocks the replay.
    run_rows = run_traces_status(root, args.area, area)
    off = [(r, s) for r, _l, s in run_rows if s != "current"]
    if off:
        bad += 1
        preflight_ok = False
        notes.append(f"run-trace preflight: {len(off)} of {len(run_rows)} example/journey "
                     f"run trace(s) missing or stale")
        print(f"PREFLIGHT: {len(off)} example/journey run trace(s) not current ("
              + ", ".join(f"{r} {s}" for r, s in off[:6])
              + (" …" if len(off) > 6 else "")
              + f") — conformance replay refused. Run tools/spec-runs.py {args.area}.")

    # ── 2. Conformance replay ────────────────────────────────────────────
    conf_result = None  # None = not run
    traces_replayed = 0
    if args.skip_conformance:
        notes.append("conformance: skipped (--skip-conformance)")
    elif not conformance.get("command"):
        notes.append("conformance: not set up (run /spec-code-generate)")
    elif preflight_ok:
        traces_dir = root / "specs" / (conformance.get("traces_dir") or f"{args.area}/traces")
        traces_replayed = len([
            p for p in (traces_dir.glob("**/*.itf.json") if traces_dir.exists() else [])
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
        # Naming the command that would work is the difference between a note
        # someone acts on and one they scroll past. `quint run --mbt` writes
        # out<N>.itf.json traces carrying mbt::actionTaken and
        # mbt::nondetPicks — the same action-plus-arguments the probe module's
        # ghosts carry, which is why the adapter replay already generates can
        # consume them unchanged.
        notes.append("pbt: not configured (conformance.pbt_command). The adapter "
                     "is already a stateful-PBT interface; the zero-new-code "
                     "option is `quint run --mbt --n-traces=1000 "
                     "--out-itf=<dir>/out.itf.json <area>.qnt` replayed through "
                     "it, or fast-check/Hypothesis against the same adapter.")

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
    save_area(root, args.area, area)
    print(f"\nrecorded verification_log entry ({status}) in {area_path}")

    if args.emit_json:
        print(json.dumps(entry_out, indent=2))
    sys.exit(1 if bad else 0)


# ── stamp / changed (spec↔code provenance) ───────────────────────────────────


def cmd_stamp(args):
    """Record which spec version code was generated from, or which code
    version a spec was extracted from.

    Mechanism over trust applies here too: these are git shas and a content
    hash, so an agent must never type them. It calls this; the tool reads
    them. That is the same reason check_results and witness blocks are
    written by a runner rather than by whoever ran it.

    Why the pair is not already in verification_log: that log records that a
    spec commit and a code commit were once CHECKED together. It does not
    record what the code was built to, or what the spec was read from —
    different facts, established at different moments, and the ones that
    answer "what has moved since".
    """
    root = Path(args.root)
    area_path = records_path(root, args.area)
    area = load_area_or_fail(root, args.area)
    project = load_json(root / ".spec" / "project.json") or {}
    repo_root, entry = resolve_code_root(root, args.area, project, args.code_root)

    code_sha = git_head(repo_root)
    if not code_sha:
        fail_setup(f"{repo_root} is not a git repo (or git failed) — there is "
                   f"no code version to record. Commit the code first; a "
                   f"provenance block with no sha in it would be worse than "
                   f"none, because it reads as 'recorded'.")

    if args.generated:
        spec_sha = git_head(root)
        block = {"date": now_iso(), "code_sha": code_sha}
        if spec_sha:
            block["spec_sha"] = spec_sha
        content_sha = compute_spec_sha(area)
        if content_sha:
            block["spec_content_sha"] = content_sha
        if entry.get("code_repo"):
            block["code_repo"] = entry["code_repo"]
        area["generated_from"] = block
        label = "generated_from"
    else:
        block = {"date": now_iso(), "code_sha": code_sha}
        if entry.get("code_repo"):
            block["code_repo"] = entry["code_repo"]
        includes, excludes = spec_source.code_scope(entry, args.code_path)
        if len(includes) == 1 and not any(c in includes[0] for c in "*?["):
            block["code_path"] = includes[0]
        elif includes:
            block["code_paths"] = includes
        user_excludes = [e for e in excludes
                         if e not in spec_source.DEFAULT_CODE_EXCLUDES]
        if user_excludes:
            block["exclude"] = user_excludes
        area["extracted_from"] = block
        label = "extracted_from"

    save_area(root, args.area, area)
    print(f"{label}: code @ {code_sha[:7]}"
          + (f", spec @ {block['spec_sha'][:7]}" if block.get("spec_sha") else "")
          + (f", claims @ {block['spec_content_sha'][:7]}"
             if block.get("spec_content_sha") else "")
          + (f", path {block['code_path']}" if block.get("code_path") else "")
          + (f", paths {', '.join(block['code_paths'])}" if block.get("code_paths") else ""))
    print(f"recorded in {area_path}")
    sys.exit(0)


def cmd_changed(args):
    """What moved in the code since this spec was read out of it.

    The question re-extraction actually has, and the one fingerprints cannot
    answer. `extraction_triage[]` is content-addressed on purpose — a
    fingerprint survives reformatting and rebases, where a line number or a
    sha does not — but a set of fingerprints only ever says WHICH sites are
    new. It cannot say what a changed guard used to be, or how many commits
    ago it moved, because the previous text is not in the ledger.

    So: fingerprints stay the identity mechanism, and this adds the
    narrative. Run it before `spec-extract-audit`, not instead of it.

    Baseline order, most specific first: extracted_from.code_sha (what the
    spec was read from), then generated_from.code_sha (what was generated),
    then the newest verification_log entry (what was last checked). Each is a
    weaker answer to the question than the one before it, so the fallback is
    named in the output rather than applied silently.
    """
    root = Path(args.root)
    area_path = records_path(root, args.area)
    area = load_area_or_fail(root, args.area)
    project = load_json(root / ".spec" / "project.json") or {}
    repo_root, entry = resolve_code_root(root, args.area, project, args.code_root)

    sources = [
        ("extracted_from", (area.get("extracted_from") or {}).get("code_sha"),
         "the code this spec was read from"),
        ("generated_from", (area.get("generated_from") or {}).get("code_sha"),
         "the code generated from this spec"),
    ]
    log = area.get("verification_log") or []
    sources.append(
        ("verification_log",
         next((e.get("code_sha") for e in reversed(log) if e.get("code_sha")), None),
         "the last code version verified against this spec"))
    if args.since:
        sources.insert(0, ("--since", args.since, "the baseline you named"))

    baseline = next(((name, sha, why) for name, sha, why in sources if sha), None)
    if baseline is None:
        print(f"No baseline recorded for '{args.area}'. Nothing pins this spec "
              f"to a code version, so 'what changed since' has no answer yet.\n"
              f"  After a brownfield extraction:  "
              f"tools/spec-record.py stamp {args.area} --extracted\n"
              f"  After /spec-code-generate:      "
              f"tools/spec-record.py stamp {args.area} --generated\n"
              f"Until then, tools/spec-extract-audit.py {args.area} still "
              f"reports unclaimed sites by fingerprint — it just cannot say "
              f"what moved, or when.")
        sys.exit(2)

    source, sha, why = baseline
    head = git_head(repo_root)
    if head is None:
        fail_setup(f"{repo_root} is not a git repo (or git failed).")
    if head == sha:
        print(f"{args.area}: code unchanged since {source} ({sha[:7]} — {why}).")
        sys.exit(0)

    changed = git_changed_files(repo_root, sha)
    if changed is None:
        fail_setup(f"git could not diff {sha[:7]}..HEAD in {repo_root}. The "
                   f"recorded baseline may be gone (rebased, squashed, or a "
                   f"shallow clone) — re-stamp after reconciling.")

    # Two lenses on the same diff, because they answer different questions.
    ext = area.get("extracted_from") or {}
    if args.code_path:
        includes, excludes = spec_source.code_scope({}, args.code_path)
    else:
        includes = list(ext.get("code_paths") or
                        ([ext["code_path"]] if ext.get("code_path") else []))
        excludes = list(ext.get("exclude") or []) + list(spec_source.DEFAULT_CODE_EXCLUDES)
    scope = ", ".join(includes)
    in_scope = [c for c in changed
                if not includes or spec_source.in_code_scope(c, includes, excludes)]
    traced = {(t.get("code") or "").split(":", 1)[0]
              for t in area.get("traceability", []) or []}
    traced.discard("")
    touching_traced = sorted({c for c in changed for t in traced if path_match(c, t)})

    print(f"{args.area}: {len(changed)} file(s) changed in {repo_root} "
          f"since {source} @ {sha[:7]} ({why}) -> HEAD @ {head[:7]}")
    if scope:
        print(f"\nIn the extracted subtree ({scope}) — {len(in_scope)}:")
        for c in in_scope:
            print(f"  {c}")
    if traced:
        print(f"\nTouching a traced file ({len(touching_traced)} of "
              f"{len(traced)} traced) — these are the ones a requirement "
              f"claims to describe:")
        for c in touching_traced:
            print(f"  {c}")
    if not scope and not traced:
        print("\n(no extracted_from.code_path and no traceability[] — showing "
              "the whole diff; narrow it by stamping --extracted with "
              "--code-path, or by filling traceability[])")
        for c in changed:
            print(f"  {c}")

    print(f"\nNext: tools/spec-extract-audit.py {args.area} --emit — the diff "
          f"says what moved, the fingerprints say which decision sites are "
          f"new. Re-stamp once the spec is reconciled.")
    sys.exit(1 if (in_scope or touching_traced or not (scope or traced)) else 0)


def main():
    p = argparse.ArgumentParser(description="Deterministic check runner + ledger.")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("check", help="Run Apalache checks + witness probes; record results.")
    pc.add_argument("area")
    pc.add_argument("--root", default=".")
    pc.add_argument("--steps", type=int, help="Override apalache.max_steps.")
    pc.add_argument("--shallow-steps", dest="shallow_steps", type=int,
                    help="Override apalache.shallow_steps — the first pass's "
                         "bound. Clamped to --steps.")
    pc.add_argument("--timeout", type=int, help="Override apalache.timeout_seconds.")
    pc.add_argument("--budget", type=int,
                    help="Override apalache.budget_seconds — the wall-clock cap "
                         "on the WHOLE run. --timeout caps one check.")
    pc.add_argument("--parallel", type=int,
                    help="Checks to run at once (default apalache.parallel, "
                         "else half the CPUs). 1 runs them one at a time.")
    pc.add_argument("--only", help="Comma-separated IDs (INV/PROP/REQ) to run.")
    pc.add_argument("--no-witness", action="store_true",
                    help="Skip the requirement checks (obligations and witness probes).")
    pc.add_argument("--no-simulate", action="store_true",
                    help="Skip the advisory `quint run` pre-gate.")
    pc.add_argument("--only-simulate", action="store_true",
                    help="Run the simulator pre-gate and stop. Records "
                         "check_results.simulation only — no verdicts, and "
                         "check_results.ran_at is left alone.")
    pc.add_argument("--strict", action="store_true",
                    help="With --only-simulate: exit 1 when the simulator "
                         "found an invariant violation or did not run, or a "
                         "witness probe was reached in none of the explored "
                         "runs. The example gate in CI; still not a verdict.")
    pc.add_argument("--samples", type=int,
                    help="Override quint.max_samples for the simulator.")
    pc.add_argument("--seed", help="Simulator seed, for a reproducible pre-gate.")
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

    ps = sub.add_parser("stamp", help="Record which spec version code was "
                                      "generated from, or which code version "
                                      "a spec was extracted from.")
    ps.add_argument("area")
    ps.add_argument("--root", default=".")
    ps.add_argument("--code-root", dest="code_root")
    ps.add_argument("--code-path", dest="code_path", action="append",
                    help="Subtree or glob the extraction read, relative to the "
                         "code repo root; repeatable. Defaults to the area's "
                         "code_paths / code_path. Narrows what `changed` reports.")
    mode = ps.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generated", action="store_true",
                      help="Code was just generated from this spec.")
    mode.add_argument("--extracted", action="store_true",
                      help="This spec was just read out of the code.")
    ps.set_defaults(func=cmd_stamp)

    pg = sub.add_parser("changed", help="What moved in the code since this "
                                        "spec was read out of it.")
    pg.add_argument("area")
    pg.add_argument("--root", default=".")
    pg.add_argument("--code-root", dest="code_root")
    pg.add_argument("--code-path", dest="code_path", action="append",
                    help="Override the recorded subtree(s) for this run; repeatable.")
    pg.add_argument("--since", help="Diff from this sha instead of the "
                                    "recorded baseline.")
    pg.set_defaults(func=cmd_changed)

    args = p.parse_args()

    # This tool WRITES verdicts. Running it with the authoritative engine
    # demanded but absent would record check_results and witness blocks
    # derived from sidecars that all parsed as "no module" — a ledger of
    # results nothing actually computed, and the ledger is the thing the
    # whole method asks people to trust.
    if DEFAULT_ENGINE == "cli" and not cli_available():
        sys.exit("ERROR: QUINT_IR_ENGINE=cli but the quint CLI is not on PATH. "
                 "Refusing to record verdicts from an unparsed model. Install "
                 "quint (tools/check-tooling.sh prints how) or unset "
                 "QUINT_IR_ENGINE.")

    try:
        args.func(args)
    except KeyboardInterrupt:
        # Queued checks first, so nothing new starts; then every child still
        # running, with its own children. The ledger already holds every
        # verdict reached so far — it is written as each one is known.
        for pool in _POOLS:
            pool.shutdown(wait=False, cancel_futures=True)
        kill_all()
        print("\ninterrupted: verdicts reached so far are recorded; "
              "check_results.ran_at is not stamped", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
