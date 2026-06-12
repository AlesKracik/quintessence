#!/usr/bin/env python3
"""
spec-record.py — Deterministic check runner and results ledger.

Runs `quint verify` for an area's invariants, properties, and witness
probes, parses the outcomes, and writes them back into specs/<area>.json
(check_results, formal_status, witness blocks) MECHANICALLY.

Why this exists: the methodology's chain is "held by mechanisms, not by
trust in the AI". That must include the bookkeeping itself — an agent
hand-editing `witness.status: "witnessed"` into JSON is exactly the
unverified step the framework is built to eliminate. With this tool the
agent's job in /spec-check shrinks to judgment work only: drafting
predicates, regenerating the probe module, translating counterexamples
into natural language (the one free-text field this tool never touches:
counterexample.nl_explanation), and triaging matrix gaps.

Subcommands:
  check <area> [--root .] [--steps N] [--timeout S]
               [--only INV-001,REQ-003] [--no-witness] [--json]

What `check` does, in order:
  1. Invariants + properties (quint_name set): `quint verify
     --invariant=<name>` each; counterexample traces saved to
     specs/<area>/traces/<ID>.cex.itf.json. Stale .cex files of
     now-verified checks are removed.
  2. Witness probes (unless --no-witness): for every requirement with a
     witness.predicate (not skipped/deferred/non-functional), runs the
     probe `witness_<ID>` from the probes module with --init=initP
     --step=stepP. Violation = witness found (trace saved, model_sha
     pinned); no violation = no-witness (vacuity red flag).
     Skip-if-fresh: a requirement already witnessed against the current
     model_sha is not re-proven.
  3. Writes check_results (preserving the matrix block and carrying over
     nl_explanation for counterexamples whose result didn't change),
     formal_status per invariant/property, and each witness block.

Exit codes: 0 = all verified/witnessed/fresh; 1 = any counterexample,
no-witness, error, or timeout; 2 = setup problem (missing files/tools).
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import compute_model_sha, load_trace  # noqa: E402
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


def find_quint():
    exe = shutil.which("quint")
    if not exe:
        fail_setup("`quint` not on PATH. Run tools/check-tooling.sh for install hints.")
    return exe


def run_verify(quint, qnt_file, invariant, max_steps, timeout,
               init=None, step=None, out_itf=None):
    """Run one `quint verify`. Returns (result, detail, duration_s) where
    result ∈ verified | counterexample | timeout | error.
    'counterexample' means a violation was found — for witness probes that
    is the GOOD outcome (the violation trace IS the witness).

    Violation detection: the presence of the freshly-written --out-itf file
    — NOT output-text grepping (any error message containing the word
    'counterexample' would misclassify). The stale file is deleted before
    the run so its existence afterwards is unambiguous."""
    out_path = Path(out_itf) if out_itf else None
    if out_path and out_path.exists():
        out_path.unlink()
    cmd = [quint, "verify", f"--invariant={invariant}", f"--max-steps={max_steps}"]
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


def probe_name(req_id):
    return "witness_" + req_id.replace("-", "_")


def cmd_check(args):
    root = Path(args.root)
    area_path = root / "specs" / f"{args.area}.json"
    area = load_json(area_path)
    if area is None:
        fail_setup(f"{area_path} not found. Run /spec {args.area} first.")
    fm = area.get("formal_model") or {}
    qnt_file = root / "specs" / (fm.get("quint_file") or f"{args.area}.qnt")
    if not qnt_file.exists():
        fail_setup(f"{qnt_file} not found — the area isn't formalized yet.")

    project = load_json(root / ".spec" / "project.json") or {}
    apalache = project.get("apalache") or {}
    max_steps = args.steps or apalache.get("max_steps", 10)
    timeout = args.timeout or apalache.get("timeout_seconds", 300)

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

    quint = find_quint()
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
            if not iid or not qname:
                continue
            if only and iid not in only:
                continue
            cex_rel = f"{args.area}/traces/{iid}.cex.itf.json"
            cex_path = root / "specs" / cex_rel
            result, detail, duration = run_verify(
                quint, qnt_file, qname, max_steps, timeout, out_itf=cex_path
            )
            entry = {
                "id": iid, "kind": kind, "quint_name": qname,
                "result": result, "duration_s": round(duration, 1),
            }
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
                item["formal_status"] = "verified"
            else:
                bad += 1
                entry["error"] = detail
                # timeout/error: keep the prior formal_status untouched.
            checks.append(entry)
            print(f"{iid:<12} {result:<16} ({duration:.1f}s)"
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
                quint, probes_file, pname, max_steps, timeout,
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
    save_area(area_path, area)
    print(f"\nrecorded check_results + witness blocks in {area_path}")

    if args.emit_json:
        print(json.dumps({"area": args.area, "ran_at": cr["ran_at"],
                          "checks": checks, "failures": bad}, indent=2))
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

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
