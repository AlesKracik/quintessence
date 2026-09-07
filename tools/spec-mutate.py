#!/usr/bin/env python3
"""
spec-mutate.py — Would any of this notice if the code were wrong?

Three questions, and until now only two were answered:

  Apalache      "is the model safe?"            — invariant checking
  witnesses     "is the model vacuous?"         — reachability probes
  (nothing)     "would the gates catch a bug?"  — this tool

A green /spec-verify says the implementation passed the checks that exist. It
says nothing about whether those checks are capable of failing. This tool
answers that directly: break the implementation on purpose, run the area's own
gates, and see whether they turn red. A mutant that SURVIVES is a finding — a
class of defect the whole chain would ship.

Discipline, because this edits real source:

  - only files named in the area's traceability[] (never tests, never specs);
  - one mutant at a time, always restored, including on Ctrl-C and on crash;
  - refuses to run on a dirty working tree, so a failed restore is visible as
    an ordinary diff rather than lost in unrelated edits;
  - never mutates inside comments or string literals, where a change proves
    nothing about behavior.

Kills are an UPPER bound on gate strength: a mutant that fails to compile also
turns the command red, and that is not the gate noticing a behavior change.
Survivors are the trustworthy half of the output.

Usage:
  tools/spec-mutate.py <area>                     # mutate, run gates, report
  tools/spec-mutate.py <area> --limit 20          # cap mutants (default 25)
  tools/spec-mutate.py <area> --operators cmp,lit # subset of operators
  tools/spec-mutate.py <area> --command "npm test -- auth"   # override gate
  tools/spec-mutate.py <area> --dry-run           # list mutants, run nothing
  tools/spec-mutate.py <area> --json              # machine-readable report

Exit codes: 0 = every mutant killed, 1 = survivors found, 2 = setup problem.
"""

import argparse
import json
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import area_json_path  # noqa: E402

DEFAULT_LIMIT = 25


def fail_setup(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


# ── Masking: never mutate comments or string literals ───────────────────────

def mask_noise(text):
    """Return a copy of `text` with comments and string literals blanked to
    spaces, preserving length and line structure.

    Length preservation is what makes this usable: every offset found in the
    masked text addresses the same character in the real one. Changing a word
    inside a comment or a message string proves nothing about behavior, and a
    "surviving mutant" from one would be pure noise."""
    out = list(text)
    i, n = 0, len(text)
    state = None          # None | "line" | "block" | quote char
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch in "\"'`":
                state = ch
                out[i] = " "
            elif ch == "/" and nxt == "/":
                state = "line"
                out[i] = out[i + 1] = " "
                i += 1
            elif ch == "#":
                state = "line"
                out[i] = " "
            elif ch == "/" and nxt == "*":
                state = "block"
                out[i] = out[i + 1] = " "
                i += 1
        elif state == "line":
            if ch == "\n":
                state = None
            else:
                out[i] = " "
        elif state == "block":
            out[i] = " "
            if ch == "*" and nxt == "/":
                out[i + 1] = " "
                i += 1
                state = None
        else:                                   # inside a string literal
            out[i] = " "
            if ch == "\\":
                if i + 1 < n:
                    out[i + 1] = " "
                i += 1
            elif ch == state:
                state = None
        i += 1
    return "".join(out)


# ── Operators ───────────────────────────────────────────────────────────────
# Each yields (label, start, end, replacement) over the MASKED text, so every
# hit is real code.

COMPARISONS = [(">=", ">"), ("<=", "<"), (">", ">="), ("<", "<="),
               ("==", "!="), ("!=", "=="), ("===", "!=="), ("!==", "===")]

LOGIC = [("&&", "||"), ("||", "&&"), (" and ", " or "), (" or ", " and ")]

# Two shapes, because both are common and only one is line-anchored:
#   raise ValueError(...)          on its own line   (Python, Ruby)
#   if (locked) { throw new E(); } inline in a block (JS/TS, Java, C#)
THROW_LINE_RE = re.compile(r"^[ \t]*(throw|raise)\b[^\n]*\n", re.MULTILINE)
THROW_INLINE_RE = re.compile(r"(?<![\w.])(throw|raise)\b[^;\n]*;")
NUMBER_RE = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
CMP_RE = re.compile(r"(===|!==|>=|<=|==|!=|>|<)")


def mutants_cmp(masked):
    """Comparison inversion — the boundary bug that ships most often."""
    swap = dict(COMPARISONS)
    for m in CMP_RE.finditer(masked):
        op = m.group(1)
        if op in swap:
            yield (f"cmp {op} -> {swap[op]}", m.start(), m.end(), swap[op])


def mutants_lit(masked):
    """Off-by-one on a numeric literal: the threshold moves, the shape does not."""
    for m in NUMBER_RE.finditer(masked):
        val = m.group(1)
        try:
            shifted = str(int(val) + 1)
        except ValueError:
            continue
        yield (f"lit {val} -> {shifted}", m.start(), m.end(), shifted)


def mutants_logic(masked):
    """Connective inversion — guards that should be conjunctive rarely are not."""
    for pat, repl in LOGIC:
        start = 0
        while True:
            idx = masked.find(pat, start)
            if idx == -1:
                break
            yield (f"logic {pat.strip()} -> {repl.strip()}", idx, idx + len(pat), repl)
            start = idx + len(pat)


def mutants_throw(masked):
    """Delete a throw/raise. This is the refusal mutant: the check is still
    there, the rejection is not. Anything that only replays happy-path traces
    cannot see it."""
    seen = []
    for regex in (THROW_LINE_RE, THROW_INLINE_RE):
        for m in regex.finditer(masked):
            # The line form subsumes an inline hit on the same line; report
            # each site once.
            if any(m.start() >= a and m.end() <= b for a, b in seen):
                continue
            seen.append((m.start(), m.end()))
            line = m.group(0).strip()
            yield (f"drop `{line[:48]}`", m.start(), m.end(), "")


OPERATORS = {"cmp": mutants_cmp, "lit": mutants_lit,
             "logic": mutants_logic, "throw": mutants_throw}


# ── Target discovery ────────────────────────────────────────────────────────

def traced_files(area, repo_root):
    """Implementation files named in traceability[] — and only those.

    The spec's own claim about which files realize it is exactly the right
    scope: mutating a file no requirement traces to would be measuring the
    coverage of code the spec never claimed."""
    out = []
    for row in area.get("traceability", []) or []:
        code = row.get("code")
        if not code:
            continue
        path = code.split(":")[0].strip()      # "authService.ts:login" -> file
        if not path:
            continue
        candidate = repo_root / path
        if candidate.exists() and candidate.is_file():
            if candidate not in out:
                out.append(candidate)
    return out


def keep_syntax_valid(path, text, op, start, end, repl):
    """Keep a deletion from breaking the parser instead of the behavior.

    Removing `raise ...` from an indented Python block leaves an empty suite
    and an IndentationError, which turns the gate red for the wrong reason:
    the mutant reads as killed while the gate never actually exercised the
    refusal. Substituting `pass` keeps the file parsable, so a gate blind to
    the refusal path shows up as the survivor it is. Braced languages need no
    equivalent — an empty block is already valid there."""
    if op != "throw" or path.suffix != ".py":
        return repl
    segment = text[start:end]
    # The line-anchored match starts AT the indentation, so the indent is the
    # segment's own leading whitespace — not the text before `start`, which is
    # empty here.
    indent = segment[:len(segment) - len(segment.lstrip(" \t"))]
    if "\n" in segment.strip():             # multi-line statement: leave it alone
        return repl
    if text.rfind("\n", 0, start) + 1 != start:
        return repl                         # not alone on its line
    return indent + "pass" + ("\n" if segment.endswith("\n") else "")


def collect_mutants(files, operators, limit, seed):
    found = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        masked = mask_noise(text)
        for name in operators:
            for label, start, end, repl in OPERATORS[name](masked):
                repl = keep_syntax_valid(path, text, name, start, end, repl)
                found.append({"file": str(path), "op": name, "label": label,
                              "start": start, "end": end, "replacement": repl})
    # Deterministic sample: a fixed seed keeps runs comparable, and spreading
    # across files stops one large file monopolizing the budget.
    random.Random(seed).shuffle(found)
    found.sort(key=lambda m: m["file"])
    per_file = {}
    picked = []
    quota = max(1, limit // max(1, len(files)))
    for m in found:
        if per_file.get(m["file"], 0) < quota:
            picked.append(m)
            per_file[m["file"]] = per_file.get(m["file"], 0) + 1
    # Top up by identity, not by dict equality: `m not in picked` compares
    # every field of every dict against every pick, which is quadratic on a
    # real codebase.
    chosen = {id(m) for m in picked}
    for m in found:
        if len(picked) >= limit:
            break
        if id(m) not in chosen:
            picked.append(m)
            chosen.add(id(m))
    return picked[:limit]


# ── Running ─────────────────────────────────────────────────────────────────

def git_dirty(repo_root):
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_root),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None                       # not a git repo: caller decides
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def run_gate(command, repo_root, timeout):
    try:
        proc = subprocess.run(command, shell=True, cwd=str(repo_root),
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124                        # a hung gate is not a kill
    except OSError as exc:
        print(f"  gate could not run: {exc}", file=sys.stderr)
        return 125


def apply_and_run(mutant, command, repo_root, timeout):
    """Apply one mutant, run the gate, restore unconditionally."""
    path = Path(mutant["file"])
    original = path.read_text(encoding="utf-8")
    mutated = original[:mutant["start"]] + mutant["replacement"] + original[mutant["end"]:]
    try:
        path.write_text(mutated, encoding="utf-8")
        return run_gate(command, repo_root, timeout)
    finally:
        # Restore in a finally, so Ctrl-C and any exception still put the
        # source back. A mutation tool that can leave the tree broken is worse
        # than no mutation tool.
        path.write_text(original, encoding="utf-8")


def main():
    p = argparse.ArgumentParser(
        description="Mutate the implementation and check the spec's gates turn red.")
    p.add_argument("area")
    p.add_argument("--root", default=".", help="Project root (default: cwd)")
    p.add_argument("--code-root", help="Override the resolved code repo root.")
    p.add_argument("--command", help="Gate command (default: the area's "
                                     "conformance.command, else its test_command).")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"Maximum mutants (default {DEFAULT_LIMIT}).")
    p.add_argument("--operators", default="cmp,lit,logic,throw",
                   help="Comma-separated subset of: cmp, lit, logic, throw.")
    p.add_argument("--seed", type=int, default=1, help="Sampling seed (default 1).")
    p.add_argument("--timeout", type=int, default=900, help="Per-run gate timeout.")
    p.add_argument("--dry-run", action="store_true", help="List mutants; run nothing.")
    p.add_argument("--json", dest="emit_json", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    if not area_path.exists():
        fail_setup(f"{area_path} not found.")
    area = json.loads(area_path.read_text(encoding="utf-8"))

    operators = [o.strip() for o in args.operators.split(",") if o.strip()]
    unknown = [o for o in operators if o not in OPERATORS]
    if unknown:
        fail_setup(f"unknown operator(s): {', '.join(unknown)}. "
                   f"Available: {', '.join(OPERATORS)}")

    project = json.loads((root / ".spec" / "project.json").read_text(encoding="utf-8")) \
        if (root / ".spec" / "project.json").exists() else {}
    local = root / ".spec" / "local.json"
    local_cfg = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    entry = next((a for a in project.get("areas", []) or []
                  if a.get("name") == args.area), {})
    if args.code_root:
        repo_root = Path(args.code_root)
    elif entry.get("code_repo"):
        base = (local_cfg.get("repo_paths") or {}).get(entry["code_repo"])
        if not base:
            fail_setup(f"no local path for repo '{entry['code_repo']}' "
                       f"(set repo_paths in .spec/local.json).")
        repo_root = Path(base)
    else:
        repo_root = root

    command = (args.command
               or (area.get("conformance") or {}).get("command")
               or entry.get("test_command"))
    if not command and not args.dry_run:
        fail_setup("no gate command: set conformance.command or the area's "
                   "test_command, or pass --command.")

    files = traced_files(area, repo_root)
    if not files:
        fail_setup(f"no traceability[] code files found under {repo_root}. "
                   f"Run /spec-apply, or check .spec/local.json paths.")

    dirty = git_dirty(repo_root)
    if dirty and not args.dry_run:
        fail_setup(f"working tree at {repo_root} has {len(dirty)} uncommitted "
                   f"change(s). Mutation edits real files and restores them; a "
                   f"clean tree is what makes a failed restore visible. Commit or "
                   f"stash first.")

    mutants = collect_mutants(files, operators, args.limit, args.seed)
    if not mutants:
        fail_setup("no mutable sites found (after masking comments and strings).")

    print(f"spec-mutate {args.area}: {len(mutants)} mutant(s) across "
          f"{len(files)} traced file(s)")
    print(f"gate: {command or '(dry run)'}")
    print()

    if args.dry_run:
        for m in mutants:
            rel = Path(m["file"]).name
            print(f"  {rel:<28} {m['label']}")
        sys.exit(0)

    baseline = run_gate(command, repo_root, args.timeout)
    if baseline != 0:
        fail_setup(f"the gate already fails on unmutated code (exit {baseline}). "
                   f"Mutation testing measures a gate that passes; fix it first.")

    killed, survived = [], []
    for i, m in enumerate(mutants, 1):
        rel = Path(m["file"]).name
        code = apply_and_run(m, command, repo_root, args.timeout)
        if code == 0:
            survived.append(m)
            print(f"  {i:>3}/{len(mutants)}  SURVIVED  {rel:<24} {m['label']}")
        else:
            killed.append(m)
            print(f"  {i:>3}/{len(mutants)}  killed    {rel:<24} {m['label']}")

    print()
    print(f"killed {len(killed)} / {len(mutants)}   survived {len(survived)}")
    print("NOTE: kills are an UPPER bound — a mutant that fails to compile also "
          "turns the gate red, which is not the gate noticing a behavior change. "
          "The survivors are the finding.")

    if survived:
        print("\nSURVIVORS — defects of this shape would ship:")
        for m in survived:
            print(f"  {Path(m['file']).name}: {m['label']}")

    if args.emit_json:
        print(json.dumps({"area": args.area, "gate": command,
                          "killed": len(killed), "survived": len(survived),
                          "survivors": survived}, indent=2))

    sys.exit(1 if survived else 0)


if __name__ == "__main__":
    main()
