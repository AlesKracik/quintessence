#!/usr/bin/env python3
"""Independent audit of the Lean proofs in spike/proof-rate/lean.

For each Spec/<Area>/Proofs/<invariant>.lean:
  1. it contains none of the forbidden constructs;
  2. it states the theorem exactly as the spike fixed it;
  3. the project builds (`lake build`, run once);
  4. a generated file restates the theorem against the generated model and
     prints the axioms the proof rests on; only propext, Classical.choice
     and Quot.sound are allowed.

Usage: python3 spike/proof-rate/audit_lean.py     (needs lake on PATH)
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "lean"
FORBIDDEN = ["sorry", "admit", "native_decide", "axiom", "implemented_by",
             "extern", "unsafe", "debug.skipKernelTC"]
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def strip_comments(text):
    text = re.sub(r"/-.*?-/", "", text, flags=re.S)
    return re.sub(r"--.*", "", text)


def audit(proof):
    area, inv = proof.parent.parent.name, proof.stem
    text = proof.read_text(encoding="utf-8")
    code = strip_comments(text)
    hits = [w for w in FORBIDDEN
            if re.search(rf"(?<![\w.]){re.escape(w)}(?![\w])", code)]
    if hits:
        return f"NOT PROVEN: uses {', '.join(hits)}"
    stmt = f"theorem {inv}_holds : ∀ s, Reachable init apply s → {inv} s = true"
    if stmt not in text:
        return "REJECTED: theorem statement changed"
    ns, mod = f"Spec.{area}", f"Spec.{area}.Proofs.{inv}"
    check = ROOT / "_audit.lean"
    check.write_text(
        f"import {mod}\n"
        f"example : ∀ s, Quint.Reachable {ns}.init {ns}.apply s → {ns}.{inv} s = true :=\n"
        f"  {ns}.{inv}_holds\n"
        f"#print axioms {ns}.{inv}_holds\n", encoding="utf-8")
    try:
        a = subprocess.run(["lake", "env", "lean", "_audit.lean"], cwd=ROOT,
                           capture_output=True, text=True, timeout=1800)
    finally:
        check.unlink(missing_ok=True)
    out = a.stdout + a.stderr
    if a.returncode != 0:
        return "NOT PROVEN: restated theorem does not check\n" + out[-400:]
    m = re.search(r"depends on axioms: \[(.*?)\]", out, flags=re.S)
    axioms = {x.strip() for x in m.group(1).split(",")} if m else set()
    bad = axioms - ALLOWED_AXIOMS
    if bad:
        return f"NOT PROVEN: rests on {', '.join(sorted(bad))}"
    lines = len(text.splitlines())
    return f"PROVEN  ({lines} lines; axioms: {', '.join(sorted(axioms)) or 'none'})"


def main():
    b = subprocess.run(["lake", "build"], cwd=ROOT, capture_output=True,
                       text=True, timeout=3600)
    if b.returncode != 0:
        print("lake build failed\n" + (b.stdout + b.stderr)[-800:])
        return 1
    failed = 0
    for proof in sorted(ROOT.glob("Spec/*/Proofs/*.lean")):
        verdict = audit(proof)
        failed += not verdict.startswith("PROVEN")
        print(f"{proof.parent.parent.name + '.' + proof.stem:45s} {verdict}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
