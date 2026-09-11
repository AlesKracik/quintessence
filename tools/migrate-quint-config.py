#!/usr/bin/env python3
"""
migrate-quint-config.py — one-shot migration for the quint-surface change.

The schema change it accompanies is ADDITIVE: .spec/project.json gains an
optional `quint` block, and area JSONs gain check_results.simulation plus two
optional fields on checks[]. Every existing file therefore still validates,
and a project that never runs this script keeps working on the defaults.

So this is not a data migration in the "your rows are in the wrong shape"
sense. It does two things worth doing anyway:

  1. Writes the `quint` block explicitly into .spec/project.json, with the
     values the tools would have used implicitly. A setting nobody can see is
     a setting nobody tunes, and the two that matter here — which evaluator
     the simulator uses, and which backend liveness is checked on — are worth
     having in front of a reviewer.

  2. Reports (never rewrites) area JSONs holding a PROPERTY that was recorded
     before the temporal fix. Those entries were produced by passing a
     temporal formula to --invariant, which is not a check of that formula at
     all, so their formal_status is not evidence of anything. The script
     flags them; re-run `/spec-check <area>` to replace them with a real
     result. It does not clear them on your behalf — deleting a recorded
     verdict is the kind of edit that should be a visible diff someone
     approved, not a side effect of running a migration.

Usage:
  python tools/migrate-quint-config.py [--root .] [--apply]

Without --apply nothing is written — it only prints what it would do. The
exit code reports the audit either way: 0 = nothing needs attention,
1 = property verdicts found that predate the temporal fix.
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULTS = {
    "eval_backend": "rust",
    "max_samples": 10000,
    "simulate_timeout_seconds": 120,
    "batch_invariants": True,
    "temporal_backend": "tlc",
}


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        sys.exit(f"ERROR: {path} unreadable: {e}")


def migrate_project(root, apply_changes):
    path = root / ".spec" / "project.json"
    project = load(path)
    if project is None:
        print(f"-  {path} not found — nothing to seed (bootstrap the project first)")
        return
    if "quint" in project:
        print(f"-  {path} already has a `quint` block — left alone")
        return
    if not apply_changes:
        print(f"-> would add `quint` to {path}: {json.dumps(DEFAULTS)}")
        return
    # Insert after `apalache` when present so the model-checking knobs read
    # together; otherwise append.
    out = {}
    placed = False
    for key, value in project.items():
        out[key] = value
        if key == "apalache":
            out["quint"] = dict(DEFAULTS)
            placed = True
    if not placed:
        out["quint"] = dict(DEFAULTS)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"OK added `quint` block to {path}")


def audit_areas(root):
    """Property verdicts recorded before the temporal fix are not evidence:
    they came from checking a temporal formula as if it were a state
    predicate. Report them; let a human re-check."""
    stale = []
    specs = root / "specs"
    for path in sorted(specs.glob("*.area.json")) + sorted(specs.glob("*.contract.json")):
        area = load(path)
        if not isinstance(area, dict):
            continue
        backends = {c.get("id"): c.get("backend")
                    for c in ((area.get("check_results") or {}).get("checks") or [])}
        for prop in area.get("properties") or []:
            pid, status = prop.get("id"), prop.get("formal_status")
            if status in (None, "specified", "not-run"):
                continue
            # `backend` on a property's check entry is written only by the
            # temporal path, so its presence is exactly the marker that this
            # verdict came from --temporal rather than from --invariant.
            if backends.get(pid):
                continue
            stale.append((path, pid, status))
    if not stale:
        print("-  no property verdicts predate the temporal fix")
        return 0
    print("\n!  property verdicts recorded before the temporal fix "
          "(a temporal formula was passed to --invariant, so these say "
          "nothing about the property):")
    for path, pid, status in stale:
        print(f"     {path.name:<32} {pid}  formal_status={status}")
    print("   Re-run `/spec-check <area>` for each. This script does not clear "
          "them: removing a recorded verdict should be a diff someone approved.")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--apply", action="store_true",
                    help="Write the changes. Without it, this is a dry run.")
    args = ap.parse_args()
    root = Path(args.root)
    migrate_project(root, args.apply)
    sys.exit(audit_areas(root))


if __name__ == "__main__":
    main()
