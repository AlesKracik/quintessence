#!/usr/bin/env python3
"""
spec-matrix.py — Generate state × event coverage matrix for a spec area.

For each stateful entity, the matrix enumerates (state × event) for the events
scoped to that entity. Per-entity event scope =
  - triggers in that entity's `state_machines[].transitions[]`, plus
  - requirements' `quint_ref` events whose requirement TEXT (description AND
    the ears fields — descriptions are derived and may not exist yet) mentions
    the entity name.

This avoids a full cartesian (entity × every verb in the project), which would
mostly emit IMPOSSIBLE-by-construction noise (e.g. `expire_session` on Account).

Coverage is TRANSITION-PRECISE: only a declared transition for exactly
(state, event) covers a cell. A requirement that merely references the event
does not — "while Active, logout" says nothing about logout on an Expired
session; surfacing that silence is the point of the matrix.

Uncovered cells are marked `?` for LLM triage (real-gap / impossible /
out-of-scope) by /spec-check Step 4a. Triage decisions written into
covered_by (GAP / IMPOSSIBLE / OUT-OF-SCOPE) survive regeneration; cells
that gain real coverage drop their stale triage automatically. `--strict`
exits 1 while any `?` remains — the CI completeness gate.

Verbs and Quint actions that aren't scoped to ANY entity are written to
`specs/<area>/gen/matrix-orphans.txt` as candidate missing entity↔event links.

Usage:
  tools/spec-matrix.py <area>                # writes specs/<area>/gen/matrix.csv
  tools/spec-matrix.py <area> --stdout       # write to stdout, no file
  tools/spec-matrix.py <area> --strict       # exit 1 while any '?' cell remains
  tools/spec-matrix.py <area> --record       # also write coverage stats into
                                             #   specs/<area>.json check_results.matrix
                                             #   (the reviewable summary; the CSV is gitignored)
  tools/spec-matrix.py <area> --root <path>  # project root (default: cwd)

Outputs land in specs/<area>/gen/ (gitignored — regenerable artifacts).
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from quint_ir import parse_qnt  # noqa: E402

# Triage values an LLM (or human) writes into the covered_by column during
# /spec-check Step 4a. Preserved across regenerations.
TRIAGE_VALUES = {"GAP", "IMPOSSIBLE", "OUT-OF-SCOPE"}


def load_area(root: Path, area: str) -> dict:
    path = root / "specs" / f"{area}.json"
    if not path.exists():
        sys.exit(f"ERROR: {path} does not exist")
    return json.loads(path.read_text(encoding="utf-8"))


def discover_qnt_actions(root: Path, area_data: dict, area: str) -> list:
    quint_file = (area_data.get("formal_model") or {}).get("quint_file") or f"{area}.qnt"
    qnt_path = root / "specs" / quint_file
    ir = parse_qnt(qnt_path)
    return ir["actions"] if ir else []


def collect_rows(area_data: dict) -> list:
    """Each row = (entity, state). Stateless entities contribute no rows —
    state×event completeness has no meaning without a state dimension."""
    rows = []
    for ent in (area_data.get("concepts") or {}).get("entities", []) or []:
        name = ent.get("name")
        for s in ent.get("states") or []:
            rows.append((name, s))
    # Also include states declared in state_machines but missing from entities[].
    declared = {(r[0], r[1]) for r in rows}
    for sm in area_data.get("state_machines", []) or []:
        ent = sm.get("entity")
        for st in sm.get("states", []) or []:
            key = (ent, st.get("name"))
            if key not in declared:
                rows.append(key)
                declared.add(key)
    return rows


# Model plumbing, never domain events — excluding them kills the noise
# orphans (init/step showed up as "candidate missing entity↔event links").
PLUMBING_ACTIONS = {"init", "step", "initP", "stepP"}


def all_events(area_data: dict, qnt_actions: list) -> list:
    seen = []
    for v in (area_data.get("concepts") or {}).get("verbs", []) or []:
        if v not in seen:
            seen.append(v)
    for a in qnt_actions:
        if a not in seen and a not in PLUMBING_ACTIONS:
            seen.append(a)
    return seen


def scope_events_per_entity(area_data: dict, all_evt: list) -> dict:
    """Return {entity_name: [events_in_scope]}.

    Scope = events appearing as a trigger in that entity's state_machines
    transitions, plus verbs whose REQ text mentions the entity name.
    """
    scope: dict = {}
    entity_names = [
        e.get("name") for e in (area_data.get("concepts") or {}).get("entities", []) or []
        if e.get("name")
    ]
    # Also include any entity referenced by a state_machine.
    for sm in area_data.get("state_machines", []) or []:
        ent = sm.get("entity")
        if ent and ent not in entity_names:
            entity_names.append(ent)

    for ent in entity_names:
        scope[ent] = []

    # From state_machines transitions.
    for sm in area_data.get("state_machines", []) or []:
        ent = sm.get("entity")
        if ent is None:
            continue
        for t in sm.get("transitions", []) or []:
            trig = t.get("trigger")
            if trig and trig not in scope[ent]:
                scope[ent].append(trig)

    # From REQs that name the entity. Scan description AND the ears fields —
    # description is derived from ears and may not exist yet for ears-only
    # requirements; scanning only it silently drops their events from scope.
    for req in area_data.get("requirements", []) or []:
        ears = req.get("ears") or {}
        text = " ".join(filter(None, [
            req.get("description"),
            ears.get("state"), ears.get("trigger"),
            ears.get("feature"), ears.get("response"),
        ]))
        qref = req.get("quint_ref")
        if not qref or qref not in all_evt:
            continue
        for ent in entity_names:
            if ent and re.search(rf"\b{re.escape(ent)}\b", text, re.IGNORECASE):
                if qref not in scope[ent]:
                    scope[ent].append(qref)

    return scope


def orphan_events(scope: dict, all_evt: list) -> list:
    scoped = {e for events in scope.values() for e in events}
    return [e for e in all_evt if e not in scoped]


def build_coverage_index(area_data: dict) -> dict:
    """(entity, from-state, trigger) -> list of covering transition refs.

    ONLY declared state-machine transitions cover a cell — they're the only
    artifact precise about (state, event). A requirement merely mentioning
    the event must NOT blanket every state: 'while Active, logout → ...'
    says nothing about logout on an Expired session, and that silence is
    exactly what this matrix exists to surface."""
    idx: dict = {}
    for sm in area_data.get("state_machines", []) or []:
        ent = sm.get("entity")
        for t in sm.get("transitions", []) or []:
            key = (ent, t.get("from"), t.get("trigger"))
            ref = f"{ent}:{t.get('from')}→{t.get('to')}({t.get('quint_action') or t.get('trigger')})"
            idx.setdefault(key, []).append(ref)
        # 'from: *' transitions cover every non-terminal state.
        terminal = {s.get("name") for s in sm.get("states") or [] if s.get("terminal")}
        for t in sm.get("transitions", []) or []:
            if t.get("from") == "*":
                for s in sm.get("states") or []:
                    name = s.get("name")
                    if name and name not in terminal:
                        key = (ent, name, t.get("trigger"))
                        ref = f"{ent}:*→{t.get('to')}({t.get('quint_action') or t.get('trigger')})"
                        idx.setdefault(key, []).append(ref)
    return {"transitions": idx}


def cell_coverage(entity: str, state: str, event: str, idx: dict) -> str:
    refs = idx["transitions"].get((entity, state, event), [])
    return "; ".join(refs) if refs else "?"


def load_prior_triage(path: Path) -> dict:
    """Read an existing matrix.csv and keep the LLM/human triage decisions
    (GAP / IMPOSSIBLE / OUT-OF-SCOPE) so regeneration doesn't erase them."""
    if not path.exists():
        return {}
    triage = {}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                covered = (row.get("covered_by") or "").strip()
                if covered in TRIAGE_VALUES:
                    key = (row.get("entity"), row.get("state"), row.get("event"))
                    triage[key] = (covered, row.get("behavior") or "")
    except (OSError, csv.Error):
        return {}
    return triage


def emit_csv(rows: list, scope: dict, idx: dict, out, prior_triage: dict = None) -> dict:
    prior_triage = prior_triage or {}
    w = csv.writer(out)
    w.writerow(["entity", "state", "event", "covered_by", "behavior"])
    stats = {"total": 0, "covered": 0, "uncovered": 0, "triaged": 0}
    for (entity, state) in rows:
        for event in scope.get(entity, []):
            covered = cell_coverage(entity, state, event, idx)
            stats["total"] += 1
            if covered == "?":
                prior = prior_triage.get((entity, state, event))
                if prior:
                    stats["triaged"] += 1
                    w.writerow([entity, state, event, prior[0], prior[1]])
                else:
                    stats["uncovered"] += 1
                    w.writerow([entity, state, event, "?", ""])
            else:
                stats["covered"] += 1
                w.writerow([entity, state, event, covered, ""])
    return stats


def main():
    p = argparse.ArgumentParser(description="Generate state × event coverage matrix.")
    p.add_argument("area", help="Area name; reads specs/<area>.json")
    p.add_argument("--root", default=".", help="Project root (default: cwd)")
    p.add_argument("--stdout", action="store_true", help="Write to stdout instead of specs/<area>/gen/matrix.csv")
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any cell is still '?' (untriaged). CI gate: "
                        "every cell must be covered or explicitly triaged as "
                        "GAP / IMPOSSIBLE / OUT-OF-SCOPE.")
    p.add_argument("--record", action="store_true",
                   help="Write coverage stats into specs/<area>.json "
                        "check_results.matrix so readbacks can surface "
                        "completeness (the CSV itself is gitignored).")
    args = p.parse_args()

    root = Path(args.root)
    area_data = load_area(root, args.area)
    qnt_actions = discover_qnt_actions(root, area_data, args.area)

    rows = collect_rows(area_data)
    all_evt = all_events(area_data, qnt_actions)
    scope = scope_events_per_entity(area_data, all_evt)
    orphans = orphan_events(scope, all_evt)
    idx = build_coverage_index(area_data)
    gen_dir = root / "specs" / args.area / "gen"
    out_path = gen_dir / "matrix.csv"

    # Triage decisions: the COMMITTED area-JSON matrix_triage[] is the
    # authoritative source (the gen/ CSV is gitignored scratch — triage
    # living only there evaporates on a fresh clone and breaks --strict
    # in CI). CSV values are merged in second, for migration only.
    prior_triage = {}
    csv_triage = load_prior_triage(out_path)
    if not out_path.exists():
        # Legacy flat location (pre-gen/ layout) — carry over once.
        csv_triage = load_prior_triage(root / "specs" / f"{args.area}.matrix.csv")
    prior_triage.update(csv_triage)
    json_keys = set()
    for t in area_data.get("matrix_triage", []) or []:
        key = (t.get("entity"), t.get("state"), t.get("event"))
        json_keys.add(key)
        if t.get("verdict") in TRIAGE_VALUES:
            prior_triage[key] = (t["verdict"], t.get("reason") or "")
    csv_only = {k for k in csv_triage if k not in json_keys}
    if csv_only:
        print(
            f"NOTE: {len(csv_only)} triage value(s) exist only in the "
            f"gitignored CSV — move them into specs/{args.area}.json "
            f"matrix_triage[] or they are lost on a fresh clone.",
            file=sys.stderr,
        )

    if args.stdout:
        stats = emit_csv(rows, scope, idx, sys.stdout, prior_triage)
    else:
        gen_dir.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            stats = emit_csv(rows, scope, idx, f, prior_triage)
        print(f"wrote {out_path}", file=sys.stderr)

        orphan_path = gen_dir / "matrix-orphans.txt"
        if orphans:
            orphan_path.write_text(
                "# Events declared in concepts.verbs[] / Quint sidecar but not\n"
                "# scoped to any entity (no transition, no REQ mentions an entity).\n"
                "# Candidate gaps: should one of these link to an entity?\n\n"
                + "\n".join(orphans) + "\n",
                encoding="utf-8",
            )
            print(f"wrote {orphan_path} ({len(orphans)} orphan events)", file=sys.stderr)
        elif orphan_path.exists():
            orphan_path.unlink()

    print(
        f"entities={len(scope)} state_rows={len(rows)} events={len(all_evt)} "
        f"orphans={len(orphans)} cells={stats['total']} "
        f"covered={stats['covered']} triaged={stats['triaged']} "
        f"uncovered={stats['uncovered']}",
        file=sys.stderr,
    )

    if args.record:
        area_path = root / "specs" / f"{args.area}.json"
        area_full = json.loads(area_path.read_text(encoding="utf-8"))
        cr = area_full.setdefault("check_results", {})
        cr["matrix"] = {
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cells": stats["total"],
            "covered": stats["covered"],
            "triaged": stats["triaged"],
            "uncovered": stats["uncovered"],
            "orphans": len(orphans),
        }
        # GAP question refs derive straight from matrix_triage[] — nothing
        # to carry by hand.
        gaps = sorted({
            t["question"] for t in area_full.get("matrix_triage", []) or []
            if t.get("verdict") == "GAP" and t.get("question")
        })
        if gaps:
            cr["matrix"]["gaps"] = gaps
        area_path.write_text(
            json.dumps(area_full, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"recorded check_results.matrix in {area_path}", file=sys.stderr)

    if args.strict and stats["uncovered"] > 0:
        print(
            f"STRICT: {stats['uncovered']} untriaged '?' cell(s) remain. "
            f"Run /spec-check to triage them.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
