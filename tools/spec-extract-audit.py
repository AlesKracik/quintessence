#!/usr/bin/env python3
"""
spec-extract-audit.py — Coverage of the CODE by the SPEC.

Every other check in this framework runs spec → code: does the implementation
do what the spec says? For a brownfield area that is the wrong way round. The
spec was read out of the code, so the interesting question is the inverse:

    is there anything the code DOES that the spec never mentions?

That is exactly where a regenerated implementation diverges. A branch nobody
wrote down cannot be generated back, cannot be witnessed, and cannot be missed
by any spec-shaped check — because nothing spec-shaped knows it exists.

This tool enumerates the decision sites in the traced implementation —
branches, guard literals, error handlers, early exits — and requires each to
be claimed in the area's `extraction_triage[]`:

    MAPPED        realizes these spec ids
    NOT-BEHAVIOR  logging, metrics, tracing
    DEFENSIVE     unreachable by construction, kept as a belt
    DEAD          unreachable — a finding about the code
    GAP           real behavior nobody specified (tracked by a Q-NNN)
    OUT-OF-SCOPE  outside the declared boundary (cites scope.excluded)

Sites are keyed by a FINGERPRINT of their normalized text, not by line number:
a ledger keyed on line numbers rots on the first reformat, and a rotted ledger
is worse than none because it looks complete.

Changing how a fingerprint is computed invalidates every committed ledger, so
that is a breaking change to this tool. It degrades safely rather than
silently: every previously-triaged row then matches no site and is reported as
stale, which is the loud failure you want, not a quiet one.

Honest limits: this is a regex scanner over masked source, not a parser. It
finds the shapes that carry behavior in mainstream languages and will miss
exotic control flow. Under-counting sites means the audit is optimistic — so
treat a clean run as "nothing OBVIOUS is unclaimed", never as proof.

Usage:
  tools/spec-extract-audit.py <area>              # report
  tools/spec-extract-audit.py <area> --strict     # exit 1 on unclaimed/GAP
  tools/spec-extract-audit.py <area> --record     # stamp check_results.extraction
  tools/spec-extract-audit.py <area> --emit       # print triage stubs to paste
  tools/spec-extract-audit.py <area> --json

Exit codes: 0 = clean, 1 = with --strict, any unclaimed site, any ledger
problem (a MAPPED row pointing at nothing, an unanchored OUT-OF-SCOPE), or any
stale row whose site no longer exists. 2 = setup problem.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from itf_tools import area_json_path  # noqa: E402

# Reuse spec-mutate's masker and traced-file resolution: comments and string
# literals must not produce sites for the same reason they must not produce
# mutants, and "which files realize this area" has one right answer.
# Loaded by path because the filename is hyphenated.
_mutate_spec = importlib.util.spec_from_file_location(
    "_spec_mutate", Path(__file__).parent / "spec-mutate.py")
_mutate = importlib.util.module_from_spec(_mutate_spec)
_mutate_spec.loader.exec_module(_mutate)
mask_noise = _mutate.mask_noise
traced_files = _mutate.traced_files


# ── Site detection ──────────────────────────────────────────────────────────

# Scanned when falling back to code_path. Deliberately narrow: a regex site
# scanner over an unknown file type produces noise, not findings.
SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".py", ".rb", ".go", ".java",
                   ".kt", ".cs", ".rs", ".php", ".scala", ".swift", ".m", ".mjs"}

SITE_PATTERNS = [
    # A branch is a place the program chose. Each one either realizes a
    # requirement or is something the spec has decided not to care about.
    # `[ \t]*` after the brace, never `\s*`: `\s` eats newlines and the site
    # would swallow the blank line plus whatever followed it.
    ("branch", re.compile(r"^[ \t]*(?:\}[ \t]*)?(?:else\s+if|elif|if)\b[^\n]*", re.MULTILINE)),
    ("branch", re.compile(r"^[ \t]*(?:case|when)\b[^\n]*", re.MULTILINE)),
    ("branch", re.compile(r"^[ \t]*(?:default|else)\s*[:{][^\n]*", re.MULTILINE)),
    # Error handling: the half of behavior most often absent from an extracted spec.
    # `} catch (e) {` is the common form; anchoring on `catch` alone misses
    # every one of them, and error handlers are the sites this must not miss.
    ("error-handler",
     re.compile(r"^[ \t]*(?:\}[ \t]*)?(?:catch|except|rescue)\b[^\n]*", re.MULTILINE)),
    # An early exit is a guard with a decision behind it.
    ("early-exit", re.compile(r"^[ \t]*(?:return|throw|raise)\b[^\n]*", re.MULTILINE)),
    ("loop", re.compile(r"^[ \t]*(?:for|while|foreach)\b[^\n]*", re.MULTILINE)),
]

LITERAL_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
CONDITION_RE = re.compile(r"(?:if|elif|else\s+if|while|when|case)\b(.*)$")


def normalize(text):
    """Whitespace-insensitive form, so reindentation does not change identity."""
    return re.sub(r"\s+", " ", text).strip()


KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "do", "else",
            "try", "with", "await", "typeof", "new", "case"}

DECL_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:async\s+)?"
    r"(?:function\s+(?P<fn>[A-Za-z_$][\w$]*)"
    r"|def\s+(?P<py>[A-Za-z_][\w]*)"
    r"|class\s+(?P<cls>[A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+(?P<assigned>[A-Za-z_$][\w$]*)\s*="
    r"|(?P<method>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{)",
    re.MULTILINE)


def enclosing_name(text, index):
    """Nearest declaration above `index` — the site's rough owner.

    Needed because identical statements collide: two `return cart;` lines in
    different methods hash the same, and one ledger row would silently claim
    both decisions. The owner name disambiguates without reintroducing line
    numbers, which is what a fingerprint exists to avoid. Renaming the
    function does invalidate its sites, and that is right: a renamed function
    is a changed site worth re-confirming."""
    best = ""
    for m in DECL_RE.finditer(text, 0, index):
        # Prefer a real callable: `const cart = ...` is a local variable, and
        # keying sites on it means renaming a local invalidates unrelated
        # decisions. Fall back to it only when nothing better has been seen.
        callable_name = m.group("fn") or m.group("py") or m.group("cls") or m.group("method")
        name = callable_name or m.group("assigned")
        if not name or name in KEYWORDS:
            continue
        if callable_name or not best:
            best = name
    return best


def fingerprint(file_rel, kind, text, owner="", nth=0):
    """Stable id for a site: file + owner + kind + normalized text.

    Includes the file, so moving a guard between modules reads as a new site
    needing a fresh decision. Excludes the line number, so reformatting does
    not. Includes the enclosing callable and an occurrence index within it, so
    two identical statements stay two sites whether they sit in different
    functions or the same one \u2014 one ledger row claiming both decisions is a
    silent merge, and silent merges are what this ledger exists to prevent."""
    h = hashlib.sha256()
    h.update(f"{file_rel}\x00{owner}\x00{kind}\x00{normalize(text)}\x00{nth}"
             .encode("utf-8"))
    return h.hexdigest()[:8]


def line_of(text, index):
    return text.count("\n", 0, index) + 1


def scan_file(path, repo_root):
    """Decision sites in one file, de-duplicated by span."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    masked = mask_noise(text)
    try:
        rel = str(path.relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        rel = path.name

    sites, spans = [], []
    # Occurrence counter per (owner, kind, text). Two identical statements in
    # the same function are still two decisions, and one ledger row claiming
    # both is exactly the silent merge this is here to prevent. Scoped to the
    # owner rather than the file, so inserting a site elsewhere does not
    # renumber \u2014 which is what makes a positional component tolerable at all.
    occurrences = {}
    for kind, regex in SITE_PATTERNS:
        for m in regex.finditer(masked):
            start, end = m.start(), m.end()
            if any(start >= a and end <= b for a, b in spans):
                continue
            spans.append((start, end))
            snippet = text[start:end].strip()
            if not snippet:
                continue
            owner = enclosing_name(text, start)
            key = (owner, kind, normalize(snippet))
            nth = occurrences.get(key, 0)
            occurrences[key] = nth + 1
            sites.append({
                "file": rel, "kind": kind, "line": line_of(text, start),
                "owner": owner, "snippet": snippet[:160],
                "fingerprint": fingerprint(rel, kind, snippet, owner, nth),
            })
            # A literal inside a condition is a threshold someone chose. It
            # gets its own site, because "the branch is specified" and "the
            # number in it is specified" are different claims.
            cond = CONDITION_RE.search(normalize(masked[start:end]))
            if cond:
                for lit in LITERAL_RE.finditer(cond.group(1)):
                    key = f"{snippet}::{lit.group(1)}"
                    lit_key = (owner, "guard-literal", normalize(key))
                    lit_nth = occurrences.get(lit_key, 0)
                    occurrences[lit_key] = lit_nth + 1
                    sites.append({
                        "file": rel, "kind": "guard-literal",
                        "line": line_of(text, start), "owner": owner,
                        "snippet": f"{lit.group(1)} in {snippet[:120]}",
                        "fingerprint": fingerprint(rel, "guard-literal", key, owner,
                                                   lit_nth),
                    })
    return sites


# ── Ledger reconciliation ───────────────────────────────────────────────────

def audit(area, sites):
    ledger = {}
    for row in area.get("extraction_triage", []) or []:
        if row.get("fingerprint"):
            ledger[row["fingerprint"]] = row

    spec_ids = set()
    for key in ("requirements", "invariants", "properties", "constraints",
                "decisions", "examples"):
        for item in area.get(key, []) or []:
            if isinstance(item, dict) and item.get("id"):
                spec_ids.add(item["id"])
    excluded = {e.get("item") for e in ((area.get("scope") or {}).get("excluded") or [])}

    result = {"sites": len(sites), "mapped": 0, "triaged": 0, "unclaimed": 0,
              "problems": [], "unclaimed_sites": [], "gaps": []}
    seen = set()
    for site in sites:
        fp = site["fingerprint"]
        seen.add(fp)
        row = ledger.get(fp)
        if row is None:
            result["unclaimed"] += 1
            result["unclaimed_sites"].append(site)
            continue
        verdict = row.get("verdict")
        if verdict == "MAPPED":
            result["mapped"] += 1
            targets = row.get("maps_to") or []
            if not targets:
                result["problems"].append(
                    f"{fp} {site['file']}:{site['line']} MAPPED but maps_to is empty")
            for t in targets:
                if "." not in t and t not in spec_ids:
                    result["problems"].append(
                        f"{fp} maps_to '{t}' does not exist in this area")
        else:
            result["triaged"] += 1
            if verdict == "GAP":
                result["gaps"].append(row.get("question") or fp)
            if verdict != "MAPPED" and not row.get("reason"):
                result["problems"].append(f"{fp} {verdict} with no reason")
            if verdict == "OUT-OF-SCOPE":
                ref = row.get("scope_ref")
                if not ref:
                    result["problems"].append(f"{fp} OUT-OF-SCOPE names no scope_ref")
                elif ref not in excluded:
                    result["problems"].append(
                        f"{fp} scope_ref '{ref}' is not in scope.excluded[]")

    # A ledger entry with no matching site is a site that moved or vanished.
    # Silently keeping it would let the ledger drift into fiction.
    result["stale"] = [fp for fp in ledger if fp not in seen]
    return result


def emit_stubs(unclaimed):
    """Paste-ready triage rows, so the human decides instead of transcribing."""
    rows = []
    for site in unclaimed:
        rows.append({
            "file": site["file"], "fingerprint": site["fingerprint"],
            "line": site["line"], "kind": site["kind"],
            "snippet": site["snippet"],
            "verdict": "GAP",
            "reason": "TODO: MAPPED (+maps_to) / NOT-BEHAVIOR / DEFENSIVE / DEAD / "
                      "GAP (+question) / OUT-OF-SCOPE (+scope_ref)",
        })
    return rows


def main():
    p = argparse.ArgumentParser(description="Audit code sites against the spec.")
    p.add_argument("area")
    p.add_argument("--root", default=".")
    p.add_argument("--code-root", help="Override the resolved code repo root.")
    p.add_argument("--code-path",
                   help="Directory to scan when traceability[] is empty (during a "
                        "brownfield extraction it always is). Defaults to the area's "
                        "code_path.")
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 while any site is unclaimed or triaged GAP.")
    p.add_argument("--record", action="store_true",
                   help="Write check_results.extraction into the area JSON.")
    p.add_argument("--emit", action="store_true",
                   help="Print extraction_triage stubs for the unclaimed sites.")
    p.add_argument("--json", dest="emit_json", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    if not area_path.exists():
        print(f"ERROR: {area_path} not found.", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))

    project_path = root / ".spec" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8")) if project_path.exists() else {}
    local_path = root / ".spec" / "local.json"
    local = json.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}
    entry = next((a for a in project.get("areas", []) or []
                  if a.get("name") == args.area), {})
    if args.code_root:
        repo_root = Path(args.code_root)
    elif entry.get("code_repo"):
        base = (local.get("repo_paths") or {}).get(entry["code_repo"])
        if not base:
            print(f"ERROR: no local path for repo '{entry['code_repo']}'.", file=sys.stderr)
            sys.exit(2)
        repo_root = Path(base)
    else:
        repo_root = root

    files = traced_files(area, repo_root)
    source = "traceability[]"
    if not files:
        # traceability[] is written by /spec-apply, which has not run during a
        # brownfield EXTRACTION \u2014 exactly when this audit is most useful. Fall
        # back to the area's declared code_path so the documented workflow is
        # actually executable.
        code_path = args.code_path or entry.get("code_path")
        if code_path:
            base = repo_root / code_path
            files = sorted(f for f in base.rglob("*")
                           if f.is_file() and f.suffix in SOURCE_SUFFIXES)
            source = f"code_path {code_path}"
    if not files:
        print(f"ERROR: no source files found under {repo_root}. Set the area's "
              f"code_path in .spec/project.json, pass --code-path, or run "
              f"/spec-apply so traceability[] exists.", file=sys.stderr)
        sys.exit(2)

    sites = []
    for path in files:
        sites.extend(scan_file(path, repo_root))
    report = audit(area, sites)

    if args.emit_json:
        print(json.dumps({**report, "unclaimed_sites": report["unclaimed_sites"]}, indent=2))
    else:
        print(f"extraction audit {args.area}: {report['sites']} site(s) across "
              f"{len(files)} file(s) from {source}")
        print(f"  mapped {report['mapped']}   triaged {report['triaged']}   "
              f"unclaimed {report['unclaimed']}")
        for site in report["unclaimed_sites"][:25]:
            print(f"    ? {site['file']}:{site['line']} [{site['fingerprint']}] "
                  f"{site['kind']}: {site['snippet'][:80]}")
        if len(report["unclaimed_sites"]) > 25:
            print(f"    … and {len(report['unclaimed_sites']) - 25} more")
        for problem in report["problems"]:
            print(f"    ! {problem}")
        if report["stale"]:
            print(f"    ! {len(report['stale'])} ledger entr(ies) match no current site "
                  f"(the code moved): {', '.join(report['stale'][:8])}")
        print("\nNOTE: a regex scan under-counts exotic control flow. A clean run means "
              "nothing OBVIOUS is unclaimed, not that the spec is complete.")

    if args.emit and report["unclaimed_sites"]:
        print("\n// paste into extraction_triage[] and replace each verdict:")
        print(json.dumps(emit_stubs(report["unclaimed_sites"]), indent=2))

    if args.record:
        cr = area.setdefault("check_results", {})
        cr["extraction"] = {
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sites": report["sites"], "mapped": report["mapped"],
            "triaged": report["triaged"], "unclaimed": report["unclaimed"],
        }
        gaps = sorted({g for g in report["gaps"] if str(g).startswith("Q-")})
        if gaps:
            cr["extraction"]["gaps"] = gaps
        area_path.write_text(json.dumps(area, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        print(f"recorded check_results.extraction in {area_path}", file=sys.stderr)

    blocking = report["unclaimed"] or report["problems"] or report["stale"]
    if args.strict and blocking:
        print("STRICT: the code does things the spec does not account for.", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
