#!/usr/bin/env python3
"""
spec-separation.py — A commit may change the spec's claims, or the traced
implementation, but not both.

`spec-record` removed hand-written verdicts from the ledger. It did not remove
the other direction: weakening a claim until the existing implementation
satisfies it. Loosening a witness predicate, dropping a conjunct from an
invariant, widening a constraint — each is legal, each leaves every gate
green, and lint catches only the crudest forms (a constant `true`, a predicate
naming no state variable).

The one reliable signal is timing. A commit that moves a claim AND the code it
judges has, in that commit, no independent check left: the claim was adjusted
with the implementation in view. Separating them does not prove either is
right; it makes the adjustment visible as its own reviewable step.

What counts as a claim change is deliberately narrow — parsed VALUES, not
text. Reformatting, key reordering, and mechanical bookkeeping written by the
tools (check_results, verification_log, witness status/trace/model_sha,
formal_status, last_modified) are not claim changes, or the rule would fire on
every /spec-check and become something people route around.

Usage:
  tools/spec-separation.py                 # staged changes (pre-commit)
  tools/spec-separation.py --rev HEAD      # a commit's changes vs its parent
  tools/spec-separation.py --range A..B    # everything in a range
  tools/spec-separation.py --json

Escape hatch: QUINT_ALLOW_MIXED_COMMIT=1 in the environment. Genuinely mixed
commits exist (renaming a field across spec and code); the point is that they
are opted into rather than arrived at.

Exit codes: 0 = separated (or empty), 1 = mixed commit, 2 = setup problem.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Everything a tool writes on its own. Changes confined to these are
# bookkeeping, never claims.
MECHANICAL_TOP = {"check_results", "verification_log", "last_modified"}
MECHANICAL_REQ = {"status", "witness", "refusal", "traceability"}
MECHANICAL_INV = {"formal_status"}


def fail_setup(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def git(args, root):
    try:
        proc = subprocess.run(["git"] + args, cwd=str(root),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        fail_setup(f"git {' '.join(args)}: {exc}")
    if proc.returncode != 0:
        fail_setup((proc.stderr or proc.stdout).strip())
    return proc.stdout


def blob(rev, path, root):
    """File content at a revision, or None when it did not exist there."""
    if rev is None:                                  # the index
        proc = subprocess.run(["git", "show", f":{path}"], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
    else:
        proc = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else None


# ── The claim projection ────────────────────────────────────────────────────

def claims(spec_text):
    """Reduce a spec JSON to the claims it makes, dropping everything the
    tools write. Two specs with the same projection make the same promises,
    however differently they are formatted."""
    if spec_text is None:
        return None
    try:
        data = json.loads(spec_text)
    except ValueError:
        return {"__unparseable__": True}
    if not isinstance(data, dict):
        return {"__unparseable__": True}

    out = {k: v for k, v in data.items() if k not in MECHANICAL_TOP}

    reqs = {}
    for req in out.pop("requirements", []) or []:
        if not isinstance(req, dict) or not req.get("id"):
            continue
        kept = {k: v for k, v in req.items() if k not in MECHANICAL_REQ}
        # The witness PREDICATE is a claim ("this is what the behavior means");
        # its status, trace and sha are results. Keep the first, drop the rest.
        w = req.get("witness") or {}
        if w.get("predicate"):
            kept["_witness_predicate"] = w["predicate"]
        if w.get("delta"):
            kept["_witness_delta"] = w["delta"]
        if w.get("enforced_by"):
            kept["_witness_enforced_by"] = w["enforced_by"]
        for oc in (w.get("outcomes") or []):
            kept.setdefault("_witness_outcomes", []).append(
                {"name": oc.get("name"), "predicate": oc.get("predicate")})
        # Same split for the refusal block: the artifact and the vars that must
        # not move are claims; the pass/fail status is a result.
        r = req.get("refusal") or {}
        if r:
            kept["_refusal"] = {k: v for k, v in r.items()
                                if k not in ("status", "checked_at")}
        reqs[req["id"]] = kept
    out["requirements"] = reqs

    invs = {}
    for inv in out.pop("invariants", []) or []:
        if isinstance(inv, dict) and inv.get("id"):
            invs[inv["id"]] = {k: v for k, v in inv.items() if k not in MECHANICAL_INV}
    out["invariants"] = invs

    out.pop("traceability", None)      # a map to code, written by /spec-code-generate
    return out


def claim_diff(before, after):
    """Named claim-level differences, or [] when only bookkeeping moved."""
    if before is None or after is None:
        return ["area added or removed"]
    if before == after:
        return []
    changed = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if isinstance(b, dict) and isinstance(a, dict):
            for sub in sorted(set(b) | set(a)):
                if b.get(sub) != a.get(sub):
                    changed.append(f"{key}.{sub}")
        else:
            changed.append(key)
    return changed


# ── Change-set discovery ────────────────────────────────────────────────────

def changed_files(args, root):
    """(paths, before_rev, after_rev). after_rev None means the index."""
    if args.range:
        parts = args.range.split("..")
        if len(parts) != 2 or not all(parts):
            fail_setup("--range wants the form A..B")
        out = git(["diff", "--name-only", args.range], root)
        return [p for p in out.splitlines() if p], parts[0], parts[1]
    if args.rev:
        out = git(["diff", "--name-only", f"{args.rev}~1", args.rev], root)
        return [p for p in out.splitlines() if p], f"{args.rev}~1", args.rev
    out = git(["diff", "--cached", "--name-only"], root)
    return [p for p in out.splitlines() if p], "HEAD", None


def traced_paths(root):
    """Implementation files any area's traceability[] points at, plus the
    conformance adapter and harness. Spec-adjacent files are excluded: a
    change to specs/ is a spec change by definition.

    Read from the WORKING TREE, deliberately: a commit that adds traceability
    for a file and edits that file in the same breath is the case this rule
    exists to catch, and reading the old revision would miss it."""
    paths = set()
    specs = root / "specs"
    if not specs.exists():
        return paths
    for spec_file in specs.glob("*.json"):
        try:
            data = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in data.get("traceability", []) or []:
            code = (row.get("code") or "").split(":")[0].strip()
            if code:
                paths.add(code)
        conf = data.get("conformance") or {}
        for key in ("adapter", "harness"):
            if conf.get(key):
                paths.add(conf[key])
    return paths


def touches_implementation(path, traced):
    """True when a changed path is implementation the spec judges.

    Matched on whole path segments so `src/auth/` never matches
    `src/authz/`, and suffix-matched because traceability records code-repo
    relative paths while git reports repo-relative ones."""
    norm = path.replace("\\", "/")
    for t in traced:
        tn = t.replace("\\", "/").strip("/")
        if not tn:
            continue
        if norm == tn or norm.endswith("/" + tn) or tn.endswith("/" + norm):
            return True
        if norm.startswith(tn + "/") or tn.startswith(norm + "/"):
            return True
    return False


def main():
    p = argparse.ArgumentParser(
        description="Refuse a commit that changes spec claims and traced code together.")
    p.add_argument("--root", default=".")
    p.add_argument("--rev", help="Check this commit against its parent.")
    p.add_argument("--range", help="Check a range, A..B.")
    p.add_argument("--json", dest="emit_json", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    files, before_rev, after_rev = changed_files(args, root)
    if not files:
        sys.exit(0)

    traced = traced_paths(root)
    spec_files = [f for f in files if f.startswith("specs/") and f.endswith(".json")]
    impl_files = [f for f in files
                  if not f.startswith("specs/") and not f.startswith(".spec/")
                  and touches_implementation(f, traced)]

    claim_changes = {}
    for path in spec_files:
        before = claims(blob(before_rev, path, root))
        after = claims(blob(after_rev, path, root))
        diff = claim_diff(before, after)
        if diff:
            claim_changes[path] = diff

    report = {"claim_changes": claim_changes, "implementation_changes": impl_files}

    if args.emit_json:
        print(json.dumps(report, indent=2))

    if claim_changes and impl_files:
        if os.environ.get("QUINT_ALLOW_MIXED_COMMIT") == "1":
            print("spec-separation: mixed commit allowed by "
                  "QUINT_ALLOW_MIXED_COMMIT=1.")
            sys.exit(0)
        print("SEPARATION: this change moves spec CLAIMS and TRACED CODE together.",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("  claims changed:", file=sys.stderr)
        for path, fields in sorted(claim_changes.items()):
            print(f"    {path}: {', '.join(fields[:6])}"
                  + (" …" if len(fields) > 6 else ""), file=sys.stderr)
        print("  implementation changed:", file=sys.stderr)
        for path in sorted(impl_files)[:10]:
            print(f"    {path}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  A claim adjusted with the implementation in view has no "
              "independent check left in that commit. Split it: land the claim "
              "change, see the gates go red, then land the code.", file=sys.stderr)
        print("  Genuinely mixed change? QUINT_ALLOW_MIXED_COMMIT=1 git commit …",
              file=sys.stderr)
        sys.exit(1)

    if not args.emit_json:
        if claim_changes:
            print(f"spec-separation: claim-only change ({len(claim_changes)} spec file(s)).")
        elif impl_files:
            print(f"spec-separation: implementation-only change ({len(impl_files)} file(s)).")
        else:
            print("spec-separation: nothing claim-bearing changed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
