#!/usr/bin/env python3
"""
spec-diff.py — Semantic diff between two revisions of the specs.

A textual diff tells you a line changed. It does not tell you that the domain
of an operation just widened, that three requirements lost their proof, or
that a decision you reversed has fourteen obligations hanging off it. Those
are the questions a reviewer actually has, and they are all derivable from the
spec JSONs — so they are derived here rather than left to whoever reads the
patch carefully enough.

Everything is read through git, so this works on any two points in history:

  tools/spec-diff.py HEAD~1                 # that revision vs the working tree
  tools/spec-diff.py v1.2.0 HEAD            # between two revisions
  tools/spec-diff.py HEAD~5 --area auth     # one area only
  tools/spec-diff.py HEAD~1 --markdown      # embeddable section
  tools/spec-diff.py HEAD~1 --json          # machine-readable

What it reports, per area:

  BEHAVIOR      requirements added, removed, or changed in meaning (EARS
                fields, modality, determinism)
  DOMAIN        constraint values widened or narrowed, states added or
                removed, external outcomes appearing or disappearing
  BOUNDARY      scope inclusions and exclusions moving
  EVIDENCE      witnesses invalidated by a model change, invariants that lost
                a verdict, proof modes downgraded
  OBLIGATIONS   what must be re-established because of all of the above
  BLAST RADIUS  decisions whose affects[] point at anything that moved

Exit codes: 0 = no semantic change, 1 = semantic change found, 2 = setup
problem. The nonzero-on-change is deliberate: a CI job can use it to require
that a spec change ships with a readback update.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

WORKTREE = "<worktree>"


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def git(args, root, check=True):
    try:
        proc = subprocess.run(["git"] + args, cwd=str(root),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"git {' '.join(args)} failed: {exc}")
    if check and proc.returncode != 0:
        fail((proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def repo_prefix(root):
    """Path of `root` inside the repository, '' at the repo root.

    Every git lookup is repo-relative while every filesystem lookup is
    root-relative, so the two have to be bridged explicitly — otherwise
    --root on a subdirectory silently compares nothing."""
    return git(["rev-parse", "--show-prefix"], root).strip()


def spec_paths(ref, root, prefix=""):
    """Spec JSON paths (repo-relative) at a revision or in the working tree."""
    if ref == WORKTREE:
        base = Path(root) / "specs"
        if not base.exists():
            return []
        return sorted(prefix + "specs/" + f.name for f in base.glob("*.json")
                      if f.name.endswith((".area.json", ".contract.json")))
    out = git(["ls-tree", "-r", "--name-only", ref, "--", prefix + "specs/"], root)
    return sorted(ln for ln in out.splitlines()
                  if ln.endswith((".area.json", ".contract.json")))


def load_at(ref, path, root, prefix=""):
    if ref == WORKTREE:
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        f = Path(root) / rel
        if not f.exists():
            return None
        text = f.read_text(encoding="utf-8")
    else:
        proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        text = proc.stdout
    try:
        return json.loads(text)
    except ValueError:
        return None


def area_of(path):
    name = Path(path).name
    for suffix in (".area.json", ".contract.json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def by_id(items):
    return {i["id"]: i for i in (items or []) if isinstance(i, dict) and i.get("id")}


def ears_text(req):
    e = req.get("ears") or {}
    parts = []
    for field in ("state", "trigger", "feature", "response"):
        if e.get(field):
            parts.append(f"{field}={e[field]}")
    if e.get("unwanted"):
        parts.append("unwanted")
    return "; ".join(parts) or (req.get("description") or "")


# ── the comparisons ─────────────────────────────────────────────────────────

def diff_requirements(old, new, out):
    o, n = by_id(old.get("requirements")), by_id(new.get("requirements"))
    for rid in sorted(set(n) - set(o)):
        req = n[rid]
        out["behavior"].append({
            "id": rid, "kind": "added",
            "now": ears_text(req),
            "note": f"new behavior ({req.get('modality', 'must')})",
        })
        out["obligations"].append(f"{rid}: needs a witness trace before approval")
    for rid in sorted(set(o) - set(n)):
        out["behavior"].append({
            "id": rid, "kind": "removed", "was": ears_text(o[rid]),
            "note": "behavior no longer specified — is the code still doing it?",
        })
    for rid in sorted(set(o) & set(n)):
        a, b = o[rid], n[rid]
        if ears_text(a) != ears_text(b):
            out["behavior"].append({
                "id": rid, "kind": "changed",
                "was": ears_text(a), "now": ears_text(b),
                "note": "meaning changed",
            })
            out["obligations"].append(f"{rid}: witness must be re-found for the new wording")
        for field, label, default in (("modality", "modality", "must"),
                                      ("determinism", "determinism", "unspecified")):
            av, bv = a.get(field), b.get(field)
            if av != bv and (av or bv):
                # Each field's own default, and parenthesised: `x or 'must' if
                # c else x` parses as `(x or 'must') if c else x`, which
                # rendered a removed determinism as "determinism=None".
                out["behavior"].append({
                    "id": rid, "kind": "changed",
                    "was": f"{label}={av or default}",
                    "now": f"{label}={bv or default}",
                    "note": ("a MAY narrowed to a MUST removes permitted outcomes"
                             if bv == "must" and av == "may" else
                             "a MUST widened to a MAY needs a witness per outcome"
                             if bv == "may" else f"{label} changed"),
                })
                if bv == "may":
                    out["obligations"].append(
                        f"{rid}: one witness per permitted outcome (witness.outcomes[])")


def diff_constraints(old, new, out):
    o, n = by_id(old.get("constraints")), by_id(new.get("constraints"))
    for cid in sorted(set(o) & set(n)):
        a, b = o[cid].get("value"), n[cid].get("value")
        if a == b:
            continue
        direction = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            direction = " (widened)" if b > a else " (narrowed)"
        out["domain"].append({
            "id": cid, "kind": "changed",
            "was": f"{o[cid].get('name', cid)} = {a}",
            "now": f"{n[cid].get('name', cid)} = {b}{direction}",
            "note": "every requirement and invariant referencing it changes meaning",
        })
        out["obligations"].append(f"{cid}: re-run the model — guards using it moved")
    for cid in sorted(set(n) - set(o)):
        out["domain"].append({"id": cid, "kind": "added",
                              "now": f"{n[cid].get('name', cid)} = {n[cid].get('value')}"})
    for cid in sorted(set(o) - set(n)):
        out["domain"].append({"id": cid, "kind": "removed",
                              "was": f"{o[cid].get('name', cid)} = {o[cid].get('value')}",
                              "note": "anything that referenced it now resolves to nothing"})


def diff_states(old, new, out):
    def states(area):
        acc = {}
        for e in ((area.get("concepts") or {}).get("entities") or []):
            if e.get("name"):
                acc[e["name"]] = (set(e.get("states") or []), bool(e.get("closed")))
        return acc

    o, n = states(old), states(new)
    for ent in sorted(set(o) & set(n)):
        (os_, oc), (ns_, nc) = o[ent], n[ent]
        added, removed = sorted(ns_ - os_), sorted(os_ - ns_)
        if added or removed:
            note = "newly reachable domain" if added else "domain removed"
            if oc or nc:
                note += " — entity is CLOSED, so this is a breaking change for anything downstream"
            out["domain"].append({
                "id": ent, "kind": "changed",
                "was": ", ".join(sorted(os_)) or "—",
                "now": ", ".join(sorted(ns_)) or "—",
                "note": note,
            })
            for st in added:
                out["obligations"].append(
                    f"{ent}.{st}: new state — every event needs a matrix verdict for it")
        if oc != nc:
            out["boundary"].append({
                "id": ent, "kind": "changed",
                "was": f"closed={oc}", "now": f"closed={nc}",
                "note": ("the state set is now exhaustive — the model is held to it"
                         if nc else "the state set is now open — extra states no longer FAIL"),
            })


def diff_scope(old, new, out):
    def parts(area):
        sc = area.get("scope") or {}
        return (set(sc.get("included") or []),
                {e.get("item") for e in (sc.get("excluded") or []) if e.get("item")})

    (oi, oe), (ni, ne) = parts(old), parts(new)
    if not (old.get("scope") or {}) and (ni or ne):
        # First declaration is one event, not one per line item: the area did
        # not just gain N capabilities, it gained a boundary.
        out["boundary"].append({
            "id": new.get("area", "?"), "kind": "added",
            "now": f"scope declared ({len(ni)} in, {len(ne)} out)",
            "note": "completeness claims below are now relative to a stated boundary",
        })
        return
    for item in sorted(ni - oi):
        out["boundary"].append({"id": item, "kind": "added", "now": f"in scope: {item}",
                                "note": "newly answerable for this"})
        out["obligations"].append(f"{item}: now in scope — needs requirements")
    for item in sorted(oi - ni):
        out["boundary"].append({"id": item, "kind": "removed", "was": f"in scope: {item}",
                                "note": "no longer claimed"})
    for item in sorted(ne - oe):
        out["boundary"].append({"id": item, "kind": "added", "now": f"excluded: {item}",
                                "note": "deliberately dropped from the boundary"})
    for item in sorted(oe - ne):
        out["boundary"].append({
            "id": item, "kind": "removed", "was": f"excluded: {item}",
            "note": "exclusion lifted — cells triaged OUT-OF-SCOPE against it are now unanchored",
        })


def diff_externals(old, new, out):
    def pairs(area):
        acc = set()
        for ext in area.get("externals") or []:
            for oc in ext.get("outcomes") or []:
                if ext.get("name") and oc.get("name"):
                    acc.add((ext["name"], oc["name"]))
        return acc

    o, n = pairs(old), pairs(new)
    for ext, oc in sorted(n - o):
        out["domain"].append({"id": f"{ext}/{oc}", "kind": "added",
                              "now": f"{ext} can now return {oc}",
                              "note": "a new way the outside world can behave"})
        out["obligations"].append(
            f"{ext}/{oc}: needs an error_outcomes entry or a triage verdict")
    for ext, oc in sorted(o - n):
        out["domain"].append({"id": f"{ext}/{oc}", "kind": "removed",
                              "was": f"{ext} could return {oc}",
                              "note": "handling for it is now dead code"})


def diff_assumptions(old, new, out):
    o, n = by_id(old.get("assumptions")), by_id(new.get("assumptions"))
    for aid in sorted(set(n) - set(o)):
        out["evidence"].append({
            "id": aid, "kind": "added", "now": n[aid].get("statement", ""),
            "note": "results that depend on it are now explicitly conditional",
        })
    for aid in sorted(set(o) - set(n)):
        out["evidence"].append({
            "id": aid, "kind": "removed", "was": o[aid].get("statement", ""),
            "note": "either it was discharged, or a dependency just became invisible",
        })
    for aid in sorted(set(o) & set(n)):
        a, b = o[aid].get("status", "accepted"), n[aid].get("status", "accepted")
        if a != b:
            out["evidence"].append({"id": aid, "kind": "changed",
                                    "was": f"status={a}", "now": f"status={b}"})


def diff_evidence(old, new, out):
    o, n = by_id(old.get("invariants")), by_id(new.get("invariants"))
    for iid in sorted(set(n) - set(o)):
        out["evidence"].append({"id": iid, "kind": "added",
                                "now": n[iid].get("description", ""),
                                "note": "new rule to establish"})
        out["obligations"].append(f"{iid}: must be checked before approval")
    for iid in sorted(set(o) - set(n)):
        out["evidence"].append({"id": iid, "kind": "removed",
                                "was": o[iid].get("description", ""),
                                "note": "a rule the system no longer promises"})
    for iid in sorted(set(o) & set(n)):
        a, b = o[iid], n[iid]
        ap, bp = a.get("proof", "bounded"), b.get("proof", "bounded")
        if ap != bp:
            out["evidence"].append({
                "id": iid, "kind": "changed", "was": f"proof={ap}", "now": f"proof={bp}",
                "note": ("weaker evidence than before" if ap == "inductive" and bp != "inductive"
                         else "stronger evidence than before"),
            })
        as_, bs = a.get("formal_status", "specified"), b.get("formal_status", "specified")
        if as_ != bs:
            lost = as_.startswith("verified") and not bs.startswith("verified")
            out["evidence"].append({
                "id": iid, "kind": "changed", "was": as_, "now": bs,
                "note": "LOST its verdict" if lost else "verdict changed",
            })
            if lost:
                out["obligations"].append(f"{iid}: re-establish — it no longer has a verdict")

    # Witness freshness: a changed model sha invalidates the trace beneath it.
    ro, rn = by_id(old.get("requirements")), by_id(new.get("requirements"))
    for rid in sorted(set(ro) & set(rn)):
        wo = (ro[rid].get("witness") or {})
        wn = (rn[rid].get("witness") or {})
        if wo.get("status") == "witnessed" and wn.get("status") != "witnessed":
            out["evidence"].append({
                "id": rid, "kind": "changed",
                "was": "witnessed", "now": wn.get("status", "not-run"),
                "note": "lost its witness",
            })
            out["obligations"].append(f"{rid}: witness must be re-found")
        elif (wo.get("model_sha") and wn.get("model_sha")
                and wo["model_sha"] != wn["model_sha"]):
            out["evidence"].append({
                "id": rid, "kind": "changed",
                "was": f"model {wo['model_sha'][:12]}", "now": f"model {wn['model_sha'][:12]}",
                "note": "re-proven against a changed model",
            })


def diff_triage(old, new, out):
    def cells(area, key, fields):
        return {tuple(c.get(f) for f in fields): c.get("verdict")
                for c in (area.get(key) or [])}

    for key, fields, label in (
            ("matrix_triage", ("entity", "state", "event"), "state×event"),
            ("outcome_triage", ("external", "outcome"), "external×outcome")):
        o, n = cells(old, key, fields), cells(new, key, fields)
        for cell in sorted(set(o) - set(n), key=str):
            out["obligations"].append(
                f"{label} {'/'.join(str(x) for x in cell)}: triage verdict '{o[cell]}' "
                f"disappeared — the cell needs coverage or a new verdict")
        for cell in sorted(set(o) & set(n), key=str):
            if o[cell] != n[cell]:
                out["boundary"].append({
                    "id": "/".join(str(x) for x in cell), "kind": "changed",
                    "was": o[cell], "now": n[cell], "note": f"{label} triage",
                })


def diff_decisions(old, new, out, moved_ids):
    o, n = by_id(old.get("decisions")), by_id(new.get("decisions"))
    for did in sorted(set(o) & set(n)):
        a, b = o[did], n[did]
        if a.get("decision") != b.get("decision") or a.get("status") != b.get("status"):
            hit = sorted(set(b.get("affects") or []) | set(a.get("affects") or []))
            out["blast"].append({
                "id": did, "was": f"{a.get('status')}: {a.get('decision', '')}",
                "now": f"{b.get('status')}: {b.get('decision', '')}",
                "affects": hit,
            })
    # A decision whose affects[] point at something that moved this diff is
    # worth surfacing even when the decision itself did not change: the ground
    # under it shifted.
    for did, dec in n.items():
        touched = sorted(set(dec.get("affects") or []) & moved_ids)
        if touched and not any(b["id"] == did for b in out["blast"]):
            out["blast"].append({
                "id": did, "was": "", "now": dec.get("decision", ""),
                "affects": touched, "indirect": True,
            })


def diff_examples(old, new, out):
    o, n = by_id(old.get("examples")), by_id(new.get("examples"))
    for eid in sorted(set(n) - set(o)):
        out["evidence"].append({"id": eid, "kind": "added",
                                "now": n[eid].get("title", eid),
                                "note": "new worked example (regression pin)"})
    for eid in sorted(set(o) - set(n)):
        out["evidence"].append({"id": eid, "kind": "removed",
                                "was": o[eid].get("title", eid),
                                "note": "a case nobody is pinning any more"})


def diff_area(old, new):
    out = {"behavior": [], "domain": [], "boundary": [], "evidence": [],
           "obligations": [], "blast": []}
    if old is None:
        out["behavior"].append({"id": new.get("area", "?"), "kind": "added",
                                "now": "area created", "note": "everything in it is new"})
        return out
    if new is None:
        out["behavior"].append({"id": old.get("area", "?"), "kind": "removed",
                                "was": "area deleted",
                                "note": "was the code deleted with it?"})
        return out
    diff_requirements(old, new, out)
    diff_constraints(old, new, out)
    diff_states(old, new, out)
    diff_scope(old, new, out)
    diff_externals(old, new, out)
    diff_assumptions(old, new, out)
    diff_evidence(old, new, out)
    diff_triage(old, new, out)
    diff_examples(old, new, out)
    moved = {e["id"] for section in ("behavior", "domain", "evidence")
             for e in out[section] if e.get("id")}
    diff_decisions(old, new, out, moved)
    return out


def has_changes(report):
    return any(v for k, v in report.items() if k != "obligations") or report["obligations"]


# ── rendering ───────────────────────────────────────────────────────────────

SECTIONS = [("behavior", "BEHAVIOR CHANGE"), ("domain", "DOMAIN CHANGE"),
            ("boundary", "BOUNDARY CHANGE"), ("evidence", "EVIDENCE CHANGE")]


def render_entry(e, markdown):
    bullet = "- " if markdown else "  "
    bold = "**" if markdown else ""
    head = f"{bullet}{bold}{e.get('id', '?')}{bold}"
    bits = []
    if e.get("was"):
        bits.append(f"was: {e['was']}")
    if e.get("now"):
        bits.append(f"now: {e['now']}")
    line = head + (" — " + " · ".join(bits) if bits else "")
    if e.get("note"):
        line += f" _({e['note']})_" if markdown else f"  [{e['note']}]"
    return line


def render(reports, markdown, ref_a, ref_b):
    lines = []
    if markdown:
        lines += ["## What Changed", "",
                  f"_Semantic diff of the specs between `{ref_a}` and "
                  f"`{ref_b if ref_b != WORKTREE else 'the working tree'}`. Not a text "
                  f"diff: these are the changes in what the system promises._", ""]
    else:
        lines.append(f"SEMANTIC SPEC DIFF  {ref_a} .. "
                     f"{ref_b if ref_b != WORKTREE else 'working tree'}")
    for area, rep in sorted(reports.items()):
        if not has_changes(rep):
            continue
        lines.append("")
        lines.append(f"### {area}" if markdown else f"[{area}]")
        for key, title in SECTIONS:
            if not rep[key]:
                continue
            lines.append("")
            lines.append(f"**{title}**" if markdown else f"  {title}")
            lines += [render_entry(e, markdown) for e in rep[key]]
        if rep["blast"]:
            lines.append("")
            lines.append("**DECISION BLAST RADIUS**" if markdown else "  DECISION BLAST RADIUS")
            for b in rep["blast"]:
                tail = (", ".join(b["affects"]) or "nothing recorded")
                lead = "(ground moved under it) " if b.get("indirect") else ""
                entry = f"{b['id']} — {lead}{b['now']}"
                if b.get("was"):
                    entry = f"{b['id']} — was: {b['was']} · now: {b['now']}"
                lines.append(("- " if markdown else "  ") + entry + f" → affects: {tail}")
        if rep["obligations"]:
            lines.append("")
            lines.append("**NEW VERIFICATION OBLIGATIONS**" if markdown
                         else "  NEW VERIFICATION OBLIGATIONS")
            seen = []
            for ob in rep["obligations"]:
                if ob not in seen:
                    seen.append(ob)
            lines += [("- " if markdown else "  ") + ob for ob in seen]
    if len(lines) <= (4 if markdown else 1):
        lines.append("")
        lines.append("_No semantic change._" if markdown else "  No semantic change.")
    lines.append("")
    return lines


def build_reports(ref_a, ref_b, root, only_area=None):
    prefix = repo_prefix(root)
    paths = set(spec_paths(ref_a, root, prefix)) | set(spec_paths(ref_b, root, prefix))
    reports = {}
    for path in sorted(paths):
        area = area_of(path)
        if only_area and area != only_area:
            continue
        old = load_at(ref_a, path, root, prefix)
        new = load_at(ref_b, path, root, prefix)
        if old is None and new is None:
            continue
        reports[area] = diff_area(old, new)
    return reports


def main():
    p = argparse.ArgumentParser(
        description="Semantic diff between two revisions of the specs.")
    p.add_argument("ref_a", help="Base revision (tag, sha, branch).")
    p.add_argument("ref_b", nargs="?", default=WORKTREE,
                   help="Revision to compare against (default: the working tree).")
    p.add_argument("--root", default=".", help="Project root (default: cwd).")
    p.add_argument("--area", help="Restrict to one area.")
    p.add_argument("--markdown", action="store_true",
                   help="Emit the embeddable 'What Changed' section.")
    p.add_argument("--json", dest="emit_json", action="store_true",
                   help="Emit the raw report as JSON.")
    args = p.parse_args()

    root = Path(args.root)
    if not (root / ".git").exists():
        # A worktree or submodule keeps .git as a file; only a missing path is fatal.
        git(["rev-parse", "--git-dir"], root)

    reports = build_reports(args.ref_a, args.ref_b, root, args.area)
    if args.emit_json:
        print(json.dumps(reports, indent=2))
    else:
        print("\n".join(render(reports, args.markdown, args.ref_a, args.ref_b)))
    sys.exit(1 if any(has_changes(r) for r in reports.values()) else 0)


if __name__ == "__main__":
    main()
