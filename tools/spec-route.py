#!/usr/bin/env python3
"""
spec-route.py — Which /spec beat a target enters, and why.

The beat table in /spec routed on whether `specs/<area>.intent.json`
EXISTS. Bootstrap scaffolds that file (status raw, empty model) for every
declared area, so every bootstrapped area with code went to `resume`, and
`brownfield extract` — the strong case — was unreachable without someone
overriding the routing by hand. Routing is now on what has HAPPENED, not on
which files exist:

  brownfield-extract   the area has code (code_paths / code_path resolves to
                       at least one file), no `extracted_from` in its ledger,
                       and no requirements yet — whether or not bootstrap
                       already scaffolded the intent file.

The rest of the table, in order:

  bootstrap            no .spec/project.json
  project-edit         target `_project`
  catalog-edit         `_patterns/*`, `_protocols/*`, `_journeys/*`
  overview             `_overview`
  confirm-target       no such area, and its name is close to one that
                       exists (`values-streams` vs `value-streams`): ask
                       "Did you mean …? [Y/n/new]" before creating anything
  configure-repo       the area's code_repo has no repo_paths entry in
                       .spec/local.json, so whether code exists is unknown
  greenfield-elicit    no requirements, no extraction, no code
  drift-codify         newest verification_log entry has drift_detected
  re-extract           extracted, and the code repo's HEAD moved since
                       `extracted_from.code_sha`
  resume               everything else (the resume beat finds the next gap,
                       or the review beat when there is none)

A brownfield route also says whether to extract in PASSES: an area whose
code scope spans more than SLICE_FILES files or SLICE_SITES decision sites
is too big for one extraction (see spec-extract-audit.py).

Usage:
  tools/spec-route.py <target> [--root .] [--json]

Exit codes: 0 always (it answers a question); 2 = unreadable project.
"""

import argparse
import difflib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spec_source  # noqa: E402

CATALOG_PREFIXES = ("_patterns/", "_protocols/", "_journeys/")


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _audit():
    spec = importlib.util.spec_from_file_location(
        "_spec_extract_audit", Path(__file__).parent / "spec-extract-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tokens(name):
    return {t.rstrip("s") for t in re.split(r"[-_./\s]+", name.lower()) if t}


def close_matches(name, existing):
    """Existing areas `name` is probably a typo of: edit-distance close, or
    the same words (plurals folded). Exact matches are not 'close'."""
    out = []
    for other in existing:
        if other == name:
            continue
        ratio = difflib.SequenceMatcher(None, name.lower(), other.lower()).ratio()
        a, b = _tokens(name), _tokens(other)
        overlap = len(a & b) / max(len(a | b), 1)
        if ratio >= 0.8 or overlap >= 0.99 or (overlap >= 0.5 and ratio >= 0.7):
            out.append((max(ratio, overlap), other))
    return [o for _s, o in sorted(out, key=lambda x: (-x[0], x[1]))]


def _git_head(path):
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def route(root, target):
    """{"beat", "reason", ...} for `/spec <target>`."""
    root = Path(root)
    project = _load_json(root / ".spec" / "project.json")
    if project is None:
        return {"beat": "bootstrap", "reason": "no .spec/project.json"}
    if target == "_project":
        return {"beat": "project-edit", "reason": "target is _project"}
    if target == "_overview":
        return {"beat": "overview", "reason": "target is _overview"}
    if target.startswith(CATALOG_PREFIXES) or target in (
            "_patterns", "_protocols", "_journeys"):
        return {"beat": "catalog-edit", "reason": f"catalog target {target}"}

    entries = {a.get("name"): a for a in project.get("areas", []) or []
               if a.get("name")}
    known = sorted(set(entries) | set(spec_source.list_areas(root)))
    entry = entries.get(target) or {}
    exists = spec_source.area_exists(root, target)

    if not exists and target not in entries:
        near = close_matches(target, known)
        if near:
            return {"beat": "confirm-target", "candidates": near,
                    "reason": f"no area '{target}'; close to "
                              + ", ".join(f"'{n}'" for n in near),
                    "ask": f"Did you mean `{near[0]}`? [Y/n/new]"}

    local = _load_json(root / ".spec" / "local.json") or {}
    if entry.get("code_repo"):
        base = (local.get("repo_paths") or {}).get(entry["code_repo"])
        if not base:
            return {"beat": "configure-repo",
                    "reason": f"area maps to repo '{entry['code_repo']}' but "
                              f".spec/local.json has no repo_paths entry for it, "
                              f"so whether code exists is unknown",
                    "ask": f"Where is '{entry['code_repo']}' checked out?"}
        repo_root = Path(base)
    else:
        repo_root = root

    audit = _audit()
    includes, excludes = spec_source.code_scope(entry)
    files = (spec_source.code_files(repo_root, includes, excludes,
                                    audit.SOURCE_SUFFIXES) if includes else [])

    area = None
    if exists:
        try:
            area = spec_source.load_area(root, target)
        except spec_source.SpecSourceError as e:
            return {"beat": "resume", "reason": f"area does not derive: {e}"}
    area = area or {}
    extracted = area.get("extracted_from") or {}
    has_reqs = bool(area.get("requirements"))

    if files and not extracted and not has_reqs:
        out = {"beat": "brownfield-extract", "code_files": len(files),
               "code_paths": includes,
               "reason": f"{len(files)} source file(s) under "
                         f"{', '.join(includes)}, no extracted_from, no "
                         f"requirements yet"
                         + (" (intent file is bootstrap's scaffold)" if exists else "")}
        sites = sum(len(audit.scan_file(f, repo_root)) for f in files)
        out["sites"] = sites
        if len(files) > audit.SLICE_FILES or sites > audit.SLICE_SITES:
            out["passes"] = (f"{len(files)} files / {sites} decision sites — "
                             f"extract in passes: one coherent slice now, the "
                             f"rest in scope.excluded[] with `pass: 2`")
        return out

    if not exists or (not has_reqs and not extracted and not files):
        return {"beat": "greenfield-elicit",
                "reason": "no requirements and no code"
                          + ("" if includes else " (no code_paths/code_path declared)")}

    log = area.get("verification_log") or []
    if log and log[-1].get("drift_detected"):
        return {"beat": "drift-codify",
                "reason": "the newest verification_log entry detected drift"}

    if extracted.get("code_sha"):
        head = _git_head(repo_root)
        if head and head != extracted["code_sha"]:
            return {"beat": "re-extract",
                    "reason": f"code moved since extraction "
                              f"({extracted['code_sha'][:7]} -> {head[:7]}); "
                              f"offer it, do not force it",
                    "next": f"tools/spec-record.py changed {target}"}

    return {"beat": "resume", "reason": "area exists; pick up the next gap"}


def main():
    p = argparse.ArgumentParser(description="Which /spec beat a target enters.")
    p.add_argument("target")
    p.add_argument("--root", default=".")
    p.add_argument("--json", dest="emit_json", action="store_true")
    args = p.parse_args()
    r = route(args.root, args.target)
    if args.emit_json:
        print(json.dumps(r, indent=2))
    else:
        print(f"{r['beat']}: {r['reason']}")
        for key in ("passes", "ask", "next"):
            if r.get(key):
                print(f"  {key}: {r[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
