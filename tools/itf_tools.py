#!/usr/bin/env python3
"""
itf_tools.py — Work with ITF traces (Informal Trace Format).

ITF is the JSON trace format emitted by Apalache and `quint run/verify
--out-itf`. In this methodology every requirement gets a *witness trace*:
an ITF file under specs/<area>/traces/ proving the behavior is reachable
in the model. This tool is the deterministic half of that story — it
validates, summarizes, and renders traces without any LLM involvement, so
what reviewers see in the readback is exactly what the model checker found.

Subcommands:
  validate  <trace.itf.json>            structural check; exit 1 if malformed
  summarize <trace.itf.json>            one line per state: action + changed vars
  mermaid   <trace.itf.json> [--title]  Mermaid sequenceDiagram for readbacks
  status    <area> [--root <path>]      witness coverage table; exit 1 on
                                        missing/invalid/STALE traces
  sha       <area> [--root <path>]      canonical model_sha of the area's
                                        model (.qnt + .probes.qnt)

Freshness: /spec-check stamps witness.model_sha (= `sha` output) when it
writes a trace. `status` recomputes it — a mismatch means the model changed
after the trace was found, so the trace proves nothing about the current
model: STALE, exit 1. Re-run /spec-check to regenerate.

Action labels: if the trace records the acting step (var `mbt::actionTaken`
from `quint run --mbt`, or a ghost var named `lastAction`), steps are
labeled with the action name; otherwise "step N". Override with
--action-var.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ACTION_VAR_CANDIDATES = ("mbt::actionTaken", "lastAction", "_lastAction")
MAX_NOTE_LEN = 60
# Ghost vars that carry replay bookkeeping, not model state.
GHOST_PREFIXES = ("last", "mbt::")


def compute_model_sha(root, area_name, area_data):
    """Canonical sha256 over the model the witnesses were checked against:
    bytes of formal_model.quint_file, then formal_model.probes_file (if it
    exists), both resolved relative to specs/. Returns hex digest or None
    if the sidecar is missing."""
    fm = area_data.get("formal_model") or {}
    quint_file = fm.get("quint_file") or f"{area_name}.qnt"
    h = hashlib.sha256()
    sidecar = Path(root) / "specs" / quint_file
    if not sidecar.exists():
        return None
    h.update(sidecar.read_bytes())
    probes_file = fm.get("probes_file")
    if probes_file:
        probes = Path(root) / "specs" / probes_file
        if probes.exists():
            h.update(probes.read_bytes())
    return h.hexdigest()


# ── ITF value rendering ───────────────────────────────────────────────────────

def render_value(v, depth=0):
    """Compact, human-readable rendering of an ITF-encoded value."""
    if isinstance(v, dict):
        if "#bigint" in v:
            return v["#bigint"]
        if "#set" in v:
            return "{" + ", ".join(render_value(x, depth + 1) for x in v["#set"]) + "}"
        if "#map" in v:
            pairs = ", ".join(
                f"{render_value(k, depth + 1)}: {render_value(val, depth + 1)}"
                for k, val in v["#map"]
            )
            return "{" + pairs + "}"
        if "#tup" in v:
            return "(" + ", ".join(render_value(x, depth + 1) for x in v["#tup"]) + ")"
        if "#unserializable" in v:
            return str(v["#unserializable"])
        if set(v.keys()) == {"tag", "value"}:  # variant constructor
            inner = render_value(v["value"], depth + 1)
            return v["tag"] if inner in ("()", "{}", "") else f"{v['tag']}({inner})"
        pairs = ", ".join(f"{k}: {render_value(val, depth + 1)}"
                          for k, val in v.items() if not k.startswith("#"))
        return "{" + pairs + "}"
    if isinstance(v, list):
        return "[" + ", ".join(render_value(x, depth + 1) for x in v) + "]"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# ── Loading and validation ────────────────────────────────────────────────────

def load_trace(path):
    """Load and structurally validate an ITF trace.
    Returns (trace_dict, errors). errors non-empty means invalid."""
    errors = []
    p = Path(path)
    if not p.exists():
        return None, [f"file not found: {p}"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, [f"not valid JSON: {e}"]

    if not isinstance(data, dict):
        return None, ["top level is not an object"]
    states = data.get("states")
    if not isinstance(states, list) or not states:
        errors.append("missing or empty 'states' array")
        return data, errors
    declared_vars = data.get("vars")
    if declared_vars is not None and not isinstance(declared_vars, list):
        errors.append("'vars' is not an array")
    for i, s in enumerate(states):
        if not isinstance(s, dict):
            errors.append(f"state {i} is not an object")
            continue
        if declared_vars:
            missing = [v for v in declared_vars if v not in s]
            if missing:
                errors.append(f"state {i} missing declared vars: {missing}")
    return data, errors


def state_vars(trace):
    declared = trace.get("vars")
    if declared:
        return [v for v in declared if not v.startswith("#")]
    first = trace["states"][0]
    return [k for k in first.keys() if not k.startswith("#")]


def detect_action_var(trace, override=None):
    if override:
        return override
    for cand in ACTION_VAR_CANDIDATES:
        if cand in trace["states"][0]:
            return cand
    return None


def step_label(state, idx, action_var):
    if action_var and action_var in state:
        return render_value(state[action_var]).strip('"')
    return "init" if idx == 0 else f"step {idx}"


def changed_vars(prev, cur, var_names, action_var):
    """[(name, rendered_new_value)] for vars that differ from prev state.
    Ghost bookkeeping vars (lastAction/lastUid/..., mbt::*) are skipped —
    they label steps, they aren't model state."""
    out = []
    for v in var_names:
        if v == action_var or v.startswith(GHOST_PREFIXES):
            continue
        if prev is None or prev.get(v) != cur.get(v):
            out.append((v, render_value(cur.get(v))))
    return out


# ── Subcommands ───────────────────────────────────────────────────────────────

def cmd_validate(args):
    trace, errors = load_trace(args.trace)
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        sys.exit(1)
    meta = trace.get("#meta") or {}
    print(f"OK: {args.trace} — {len(trace['states'])} states, "
          f"vars: {', '.join(state_vars(trace))}"
          + (f", source: {meta.get('source')}" if meta.get("source") else ""))


def cmd_summarize(args):
    trace, errors = load_trace(args.trace)
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        sys.exit(1)
    var_names = state_vars(trace)
    action_var = detect_action_var(trace, args.action_var)
    prev = None
    for i, s in enumerate(trace["states"]):
        label = step_label(s, i, action_var)
        delta = changed_vars(prev, s, var_names, action_var)
        rendered = "; ".join(f"{n} = {v}" for n, v in delta) or "(no change)"
        print(f"[{i}] {label:<24} {rendered}")
        prev = s


def _truncate(text, limit=MAX_NOTE_LEN):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mermaid_escape(text):
    return text.replace(";", ",")


def cmd_mermaid(args):
    trace, errors = load_trace(args.trace)
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        sys.exit(1)
    var_names = state_vars(trace)
    action_var = detect_action_var(trace, args.action_var)
    actor = args.actor

    lines = ["sequenceDiagram"]
    if args.title:
        lines.append(f"    title {args.title}")
    lines.append(f"    participant E as {actor}")
    lines.append("    participant S as System")
    prev = None
    for i, s in enumerate(trace["states"]):
        label = step_label(s, i, action_var)
        delta = changed_vars(prev, s, var_names, action_var)
        if i == 0 and not args.show_init:
            prev = s
            continue
        lines.append(f"    E->>S: {_mermaid_escape(label)}")
        for n, v in delta:
            lines.append(f"    Note over S: {_mermaid_escape(_truncate(f'{n} = {v}'))}")
        prev = s
    print("\n".join(lines))


def cmd_sha(args):
    root = Path(args.root)
    area_path = root / "specs" / f"{args.area}.json"
    if not area_path.exists():
        print(f"ERROR: {area_path} not found", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    sha = compute_model_sha(root, args.area, area)
    if sha is None:
        print(f"ERROR: sidecar for '{args.area}' not found", file=sys.stderr)
        sys.exit(2)
    print(sha)


def cmd_status(args):
    root = Path(args.root)
    area_path = root / "specs" / f"{args.area}.json"
    if not area_path.exists():
        print(f"ERROR: {area_path} not found", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    current_sha = compute_model_sha(root, args.area, area)

    rows = []
    missing = 0
    for req in area.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred":
            continue
        w = req.get("witness") or {}
        trace_rel = w.get("trace")
        status = w.get("status", "not-run")
        detail = ""
        if trace_rel:
            trace_path = root / "specs" / trace_rel
            if not trace_path.exists():
                status, detail = "MISSING-FILE", str(trace_path)
                missing += 1
            else:
                t, errs = load_trace(trace_path)
                detail = f"{len(t['states'])} states" if not errs else f"invalid: {errs[0]}"
                if errs:
                    status = "INVALID"
                    missing += 1
                elif status == "witnessed":
                    stamped = w.get("model_sha")
                    if not stamped:
                        detail += ", unstamped — re-run /spec-check to pin model_sha"
                    elif current_sha and stamped != current_sha:
                        status = "STALE"
                        detail = "model changed since trace was found — re-run /spec-check"
                        missing += 1
        elif status == "witnessed":
            status, detail = "MISSING-FILE", "(status says witnessed but no trace recorded)"
            missing += 1
        rows.append((rid, status, trace_rel or "—", detail))

    if not rows:
        print(f"{args.area}: no requirements declared.")
        return
    width = max(len(r[0]) for r in rows)
    for rid, status, trace_rel, detail in rows:
        print(f"{rid:<{width}}  {status:<14} {trace_rel}"
              + (f"  ({detail})" if detail else ""))
    unwitnessed = sum(1 for r in rows if r[1] != "witnessed")
    print(f"\n{len(rows) - unwitnessed}/{len(rows)} requirements witnessed.")
    if missing:
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="ITF witness-trace toolkit.")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("validate", help="Structurally validate an ITF trace.")
    pv.add_argument("trace")
    pv.set_defaults(func=cmd_validate)

    ps = sub.add_parser("summarize", help="One line per state.")
    ps.add_argument("trace")
    ps.add_argument("--action-var", help="State var holding the action name.")
    ps.set_defaults(func=cmd_summarize)

    pm = sub.add_parser("mermaid", help="Emit a Mermaid sequence diagram.")
    pm.add_argument("trace")
    pm.add_argument("--title", help="Diagram title (e.g. 'REQ-003 witness').")
    pm.add_argument("--actor", default="Env", help="Left participant label.")
    pm.add_argument("--action-var", help="State var holding the action name.")
    pm.add_argument("--show-init", action="store_true",
                    help="Include the init state as a step.")
    pm.set_defaults(func=cmd_mermaid)

    pt = sub.add_parser("status", help="Witness coverage table for an area.")
    pt.add_argument("area")
    pt.add_argument("--root", default=".")
    pt.set_defaults(func=cmd_status)

    ph = sub.add_parser("sha", help="Canonical model_sha (.qnt + .probes.qnt).")
    ph.add_argument("area")
    ph.add_argument("--root", default=".")
    ph.set_defaults(func=cmd_sha)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
