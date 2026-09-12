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
from `quint run --mbt`, or a ghost var named `_lastAction`), steps are
labeled with the action name; otherwise "step N". Override with
--action-var.

Ghost convention: probe-module bookkeeping vars are underscore-prefixed
(`_lastAction`, `_lastUid`, ...) so they can never collide with real model
vars like `lastLoginTime`. Plain `lastAction` is accepted as an action-label
var for older traces, but only `_last*` / `mbt::*` are filtered from state
output.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ACTION_VAR_CANDIDATES = ("mbt::actionTaken", "_lastAction", "lastAction")
MAX_NOTE_LEN = 60
# Ghost vars that carry replay bookkeeping, not model state. Underscore
# prefix is the convention precisely so model vars ("lastLoginTime") are
# never silently dropped from summaries/diagrams.
GHOST_PREFIXES = ("_last", "mbt::")
# Params, not the action label: every _last* ghost except _lastAction itself,
# and quint's own nondet record when the trace came from `quint run --mbt`.
GHOST_PARAM_RE = re.compile(r"^_last(?!Action$)")
NONDET_PICKS_VAR = "mbt::nondetPicks"

# Spec-file layout: every spec JSON carries a type suffix in its filename.
#   specs/<name>.area.json | specs/<name>.contract.json
#   specs/changes/<slug>.change.json
#   specs/journeys/<slug>.journey.json
AREA_SUFFIXES = ("area", "contract")

# Model plumbing, never domain events. Shared so the matrix and the
# orphan-action lint cannot disagree about which actions are bookkeeping.
PLUMBING_ACTIONS = {"init", "step", "initP", "stepP"}

# The verdicts a triaged (state, event) or (external, outcome) cell may carry.
# Shared so the matrix generator and the schema-backed lint stay in step.
TRIAGE_VALUES = {"GAP", "IMPOSSIBLE", "NO-OP", "OUT-OF-SCOPE"}


# ── Console encoding ──────────────────────────────────────────────────────────
# A tool's output must never be the thing that crashes it. A legacy Windows
# console reports cp1252, which cannot encode the arrows, em dashes and status
# marks these tools emit, and print()/csv.writer raise UnicodeEncodeError
# rather than degrading. Shared here because every tool that writes to stdout
# needs the same guard — spec-matrix did not have it and died mid-CSV on the
# first "→" of a coverage cell.

def stream_encodes(stream, probe):
    enc = getattr(stream, "encoding", None)
    if not enc:
        return False
    try:
        probe.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def soften_stdout(stream=None):
    """Last-resort guard: never let an unencodable character abort the output.
    Only touches error handling, never the encoding — re-encoding a cp1252
    console as UTF-8 would trade the crash for mojibake."""
    stream = sys.stdout if stream is None else stream
    if stream_encodes(stream, "—→"):
        return
    try:
        stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


# ── Generated-name conventions ────────────────────────────────────────────────
# The probe generator writes these names, the recorder looks them up, and the
# lint checks predicates against them. The convention is fixed rather than
# configurable precisely so those three can agree without consulting each
# other — which only holds while there is ONE implementation of it.

def ghost_for_param(name):
    """uid -> _lastUid. One ghost per distinct parameter NAME across the
    module, not per action: two actions taking `uid` are talking about the
    same argument, and the replay harness reads one field for it."""
    return "_last" + name[:1].upper() + name[1:]


def ghost_for_var(name):
    """status -> _prevStatus. The pre-state snapshot a witness delta reads."""
    return "_prev" + name[:1].upper() + name[1:]


def probe_name(req_id):
    """The `val` a requirement's witness probe is emitted as."""
    return "witness_" + req_id.replace("-", "_")


def outcome_probe_name(req_id, outcome_name):
    """Probe for one permitted outcome of a `may` requirement. Distinct name
    per outcome, since each is proven separately."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", outcome_name or "").strip("_")
    return probe_name(req_id) + "_" + (slug or "outcome")


def witness_entries(req):
    """Every witness-bearing entry of a requirement, as (label, entry).

    A `must` requirement carries one witness with one trace. A `may`
    requirement carries one per PERMITTED OUTCOME, in witness.outcomes[], and
    nothing on the witness itself \u2014 so a consumer that reads only
    witness.trace sees a fully discharged permission as a missing trace. That
    was a spurious FAIL in lint and, worse, a permanent refusal in the
    /spec-code-verify preflight.

    Shared here for the same reason is_rejection() is: the gate, the ledger
    and the readback must agree on what "witnessed" means."""
    w = req.get("witness") or {}
    rid = req.get("id", "?")
    outcomes = w.get("outcomes") or []
    if outcomes:
        return [(f"{rid}/{o.get('name', '?')}", o) for o in outcomes]
    return [(rid, w)]


def is_rejection(req):
    """True when a requirement forbids a behavior rather than requiring one.

    Shared so lint, spec-record and the readback cannot disagree about which
    requirements owe a refusal artifact. Two forms count:

      - modality "forbidden" (the typed form), or
      - a deliberately SKIPPED witness carrying a justification (the older
        prose form, still valid).

    Deliberately NOT keyed on ears.unwanted alone: much unwanted-behavior
    handling does change state (a timeout that moves the order to PENDING),
    is witnessable, and is already covered by trace replay. The distinguishing
    property of a refusal is that there is no state change to witness."""
    if not isinstance(req, dict):
        return False
    if req.get("modality") == "forbidden":
        return True
    w = req.get("witness") or {}
    return w.get("status") == "skipped" and skip_discharge(w) is not None


def skip_discharge(witness):
    """What a `skipped` witness offers in place of a trace, or None.

    Two forms discharge the obligation, and they are the same claim written
    at different precisions:

      - `enforced_by`: the enforcing invariant as an ID. The typed form, and
        the one the schema and spec-lint both steer prohibitions toward,
        because a dangling ID FAILs where a sentence cannot be checked.
      - `justification`: the older prose form, still valid.

    Shared so the gate has one definition. It previously read only the prose
    field in three separate places, which meant spec-record wrote the typed
    form for every `forbidden` requirement and spec-lint then failed it — the
    documented flow breaking its own gate on the first run.
    """
    if not isinstance(witness, dict):
        return None
    enforced = witness.get("enforced_by")
    if enforced:
        return f"enforced by {enforced}"
    return witness.get("justification") or None


def compute_spec_sha(area_data):
    """Canonical sha256 over the SEMANTIC content a prose brief describes.

    The brief is the one long-form thing an agent writes into the readback,
    and prose cannot be checked the way a witness can — so it gets the same
    treatment a witness trace gets: a freshness pin. Written against this sha,
    compared against it later; a mismatch means the spec moved and the brief
    is describing something that is no longer there.

    Deliberately covers only what a brief could be WRONG about: the EARS
    fields, modality and status of each requirement, invariant and property
    statements, constraint values, entity states, scope, and the declared
    externals. Bookkeeping the brief never claims anything about — witness
    traces, check results, verification logs, extraction evidence — is
    excluded, so re-running the checker does not invalidate prose it cannot
    have affected.
    """
    if not isinstance(area_data, dict):
        return None

    def reqs():
        for r in area_data.get("requirements", []) or []:
            if not isinstance(r, dict):
                continue
            yield {
                "id": r.get("id"),
                # `description` as well as `ears`: it is the field the schema
                # requires, and the one the readback renders when a
                # requirement has no EARS structure yet. Hashing only `ears`
                # left every unstructured requirement outside the pin.
                "description": r.get("description"),
                "ears": r.get("ears"),
                "modality": r.get("modality"),
                "determinism": r.get("determinism"),
                "type": r.get("type"),
                "status": r.get("status"),
            }

    def named(key, *fields):
        out = []
        for item in area_data.get(key, []) or []:
            if isinstance(item, dict):
                out.append({f: item.get(f) for f in fields})
        return out

    payload = {
        "purpose": area_data.get("purpose"),
        "scope": area_data.get("scope"),
        "requirements": list(reqs()),
        # `description` is the field the schema declares (and requires) for
        # both. Hashing a "statement" key that no invariant has left every
        # rule in the area outside the pin: the text could be rewritten
        # wholesale and the brief still read as current.
        "invariants": named("invariants", "id", "description", "criticality"),
        "properties": named("properties", "id", "description"),
        "constraints": named("constraints", "id", "name", "value"),
        "assumptions": named("assumptions", "id", "statement"),
        "decisions": named("decisions", "id", "decision"),
        "externals": named("externals", "name", "outcomes"),
        "entities": [
            {"name": e.get("name"), "states": e.get("states"), "closed": e.get("closed")}
            for e in ((area_data.get("concepts") or {}).get("entities") or [])
            if isinstance(e, dict)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_meaning_sha(req):
    """Canonical sha256 over the ONE requirement a distilled meaning restates.

    The meaning is prose — the plain-words sentence a reviewer actually reads,
    written by an agent that understood what the identifiers and exception
    names stand for. Prose cannot be checked the way a witness can, so it is
    pinned, exactly like a brief. Pinned per requirement rather than per area:
    editing REQ-007 must not silently unpin the other forty meanings, and it
    must certainly unpin REQ-007's own.

    Covers only what the meaning restates — the EARS fields, modality, type and
    fit criterion. Witness traces, check results and extraction bookkeeping are
    excluded: re-running the checker cannot make a plain-words sentence wrong.
    """
    if not isinstance(req, dict):
        return None
    payload = {
        "id": req.get("id"),
        # Same reason as compute_spec_sha: the meaning restates whichever of
        # the two the readback would otherwise render, and for an
        # unstructured requirement that is `description`.
        "description": req.get("description"),
        "ears": req.get("ears"),
        "modality": req.get("modality"),
        "determinism": req.get("determinism"),
        "type": req.get("type"),
        "fit_criterion": req.get("fit_criterion"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def meaning_status(req):
    """(state, detail) for a requirement's distilled meaning: "absent",
    "stale" or "current"."""
    meaning = (req or {}).get("meaning") or {}
    if not (meaning.get("text") or "").strip():
        return "absent", None
    pinned = meaning.get("written_against")
    current = compute_meaning_sha(req)
    if not pinned:
        return "stale", "no written_against pin — freshness unverifiable"
    if current and pinned != current:
        return "stale", (f"the requirement has changed since the meaning was "
                         f"written ({pinned[:12]})")
    return "current", (current or "")[:12]


def brief_status(area_data):
    """(state, detail) for a prose brief: "absent", "stale" or "current"."""
    brief = (area_data or {}).get("brief") or {}
    if not brief.get("text"):
        return "absent", None
    pinned = brief.get("written_against")
    current = compute_spec_sha(area_data)
    if not pinned:
        return "stale", "no written_against pin — freshness unverifiable"
    if current and pinned != current:
        return "stale", f"spec has changed since the brief was written ({pinned[:12]})"
    return "current", (current or "")[:12]


def area_json_path(root, name):
    """Resolve specs/<name>.area.json or specs/<name>.contract.json —
    whichever exists. Falls back to the .area.json path (for new files /
    error messages) when neither does."""
    for kind in AREA_SUFFIXES:
        p = Path(root) / "specs" / f"{name}.{kind}.json"
        if p.exists():
            return p
    return Path(root) / "specs" / f"{name}.area.json"


def changes_dir(root):
    return Path(root) / "specs" / "changes"


def journeys_dir(root):
    return Path(root) / "specs" / "journeys"


def compute_model_sha(root, area_name, area_data):
    """Canonical sha256 over the model the witnesses were checked against:
    bytes of formal_model.quint_file, then formal_model.probes_file, both
    resolved relative to specs/. Returns None if the sidecar is missing OR
    if probes_file is recorded but absent — a half-hashable model must not
    produce a sha that 'matches', it must fail loudly (witnesses were found
    via the probes module; without it freshness is unverifiable)."""
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
        if not probes.exists():
            return None
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


def action_params(state, ghost_vars=None):
    """Rendered call arguments of the action that produced `state`, in
    declaration order, deduplicated, empties dropped.

    Two conventions carry the same fact, and both are read here rather than
    at each call site:
      - `mbt::nondetPicks` (from `quint run --mbt`) — ONE record var, one
        field per nondet choice, each wrapped in an option: Some(v) was the
        value picked, None means that choice did not apply to this action.
      - `_last*` ghost vars (from the generated probe module) — one var per
        parameter. This is what `quint verify` traces carry, because Apalache
        has no --mbt: the probe module instruments the model instead.

    Reading picks first and ghosts second means a trace that has both (a
    probe module simulated with --mbt) reports quint's own record rather than
    the hand-written mirror of it."""
    out = []
    picks = state.get(NONDET_PICKS_VAR)
    if isinstance(picks, dict):
        for key, val in picks.items():
            if key.startswith("#"):
                continue
            if isinstance(val, dict) and set(val.keys()) == {"tag", "value"}:
                # Option-wrapped: None = this choice was not made this step.
                if val["tag"] in ("None", "none"):
                    continue
                val = val["value"]
            rendered = render_value(val).strip('"')
            if rendered and rendered not in out:
                out.append(rendered)
        if out:
            return out
    if ghost_vars is None:
        ghost_vars = [v for v in state if GHOST_PARAM_RE.match(v)]
    for ghost in ghost_vars:
        rendered = render_value(state.get(ghost)).strip('"')
        if rendered and rendered not in out:
            out.append(rendered)
    return out


def step_label(state, idx, action_var):
    if action_var and action_var in state:
        return render_value(state[action_var]).strip('"')
    return "init" if idx == 0 else f"step {idx}"


def changed_vars(prev, cur, var_names, action_var):
    """[(name, rendered_new_value)] for vars that differ from prev state.
    Ghost bookkeeping vars (_lastAction/_lastUid/..., mbt::*) are skipped —
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


def mermaid_lines(trace, title=None, actor="Env", action_var=None, show_init=False):
    """Mermaid sequenceDiagram lines for a loaded trace. Shared by the CLI
    subcommand and the readback generator — one renderer, one output."""
    var_names = state_vars(trace)
    action_var = detect_action_var(trace, action_var)
    lines = ["sequenceDiagram"]
    if title:
        lines.append(f"    title {title}")
    lines.append(f"    participant E as {actor}")
    lines.append("    participant S as System")
    prev = None
    for i, s in enumerate(trace["states"]):
        label = step_label(s, i, action_var)
        delta = changed_vars(prev, s, var_names, action_var)
        if i == 0 and not show_init:
            prev = s
            continue
        lines.append(f"    E->>S: {_mermaid_escape(label)}")
        for n, v in delta:
            lines.append(f"    Note over S: {_mermaid_escape(_truncate(f'{n} = {v}'))}")
        prev = s
    return lines


def cmd_mermaid(args):
    trace, errors = load_trace(args.trace)
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        sys.exit(1)
    print("\n".join(mermaid_lines(
        trace, title=args.title, actor=args.actor,
        action_var=args.action_var, show_init=args.show_init)))


def cmd_spec_sha(args):
    """The pin a prose brief is written against. Prints the sha, and says
    whether the current brief still matches it."""
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    if not area_path.exists():
        print(f"ERROR: {area_path} not found", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    sha = compute_spec_sha(area)
    print(sha)
    state, detail = brief_status(area)
    if state == "absent":
        print("no brief recorded — paste this into brief.written_against when "
              "you write one", file=sys.stderr)
    elif state == "stale":
        print(f"brief is STALE: {detail}", file=sys.stderr)
        sys.exit(1)
    else:
        print("brief is current", file=sys.stderr)


def cmd_meaning_sha(args):
    """The pin each requirement's plain-words meaning is written against.
    Prints one line per requirement — sha, state — so re-pinning after an
    edit is a copy, not a recomputation by hand. Exits 1 if any is stale."""
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    if not area_path.exists():
        print(f"ERROR: {area_path} not found", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    stale = 0
    for req in area.get("requirements", []) or []:
        if not isinstance(req, dict) or req.get("status") == "deferred":
            continue
        rid = req.get("id", "?")
        if args.req and rid != args.req:
            continue
        state, detail = meaning_status(req)
        if state == "stale":
            stale += 1
        note = {"absent": "no meaning recorded — paste this sha into "
                          "meaning.written_against when you write one",
                "stale": f"STALE: {detail}",
                "current": "current"}[state]
        print(f"{rid}\t{compute_meaning_sha(req)}\t{note}")
    if stale:
        sys.exit(1)


def cmd_sha(args):
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    if not area_path.exists():
        print(f"ERROR: {area_path} not found", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    sha = compute_model_sha(root, args.area, area)
    if sha is None:
        fm = area.get("formal_model") or {}
        probes = fm.get("probes_file")
        if probes and not (root / "specs" / probes).exists():
            print(f"ERROR: formal_model.probes_file '{probes}' is recorded but "
                  f"the file is missing — a half-hashable model gets no sha. "
                  f"Restore it or re-run /spec-check.", file=sys.stderr)
        else:
            print(f"ERROR: sidecar for '{args.area}' not found", file=sys.stderr)
        sys.exit(2)
    print(sha)


def is_hollow(trace):
    """True when a witness trace proves nothing: the predicate already held
    before anything happened.

    A witness probe asserts `not(predicate)` as an invariant, so the
    counterexample is a run in which the predicate becomes true. The checker
    returns the SHORTEST such run. A counterexample of one state therefore
    means the predicate was satisfied by the initial state — no action fired,
    nothing moved, and the "witness" is a photograph of the starting
    position. Fast to find, too: these come back in seconds, which is exactly
    why they read as healthy.

    Structural, so no Quint evaluator is needed and the answer cannot drift
    from what the checker actually produced: the trace either has a step in
    it or it does not. The remedy is a witness.delta (the probe then has to
    show the state MOVING, over the `_prev*` ghosts) and usually a predicate
    that was too weak to tell before from after.

    Deliberately independent of whether a delta is declared: a delta whose
    `pre` also holds at init still yields a one-state trace, and the trace is
    the evidence, not the field."""
    states = (trace or {}).get("states") or []
    return len(states) <= 1


def witness_status(root, area_name, area_data):
    """Witness-obligation status for every gating requirement.
    Returns (rows, missing, discharged) where rows = [(rid, status, trace_rel,
    detail)] and missing > 0 means the area must NOT be conformance-replayed
    or treated as fully checked. This is the single implementation behind
    `itf_tools status` and spec-record's verify preflight.

    Gate semantics: a requirement counts as discharged ONLY when it is
    witnessed-with-a-fresh-stamped-valid-trace or justified-skipped.
    Everything else — not-run, no-witness, stale, unstamped, invalid,
    missing file, unjustified skip, unverifiable model sha — counts
    against the gate."""
    root = Path(root)
    current_sha = compute_model_sha(root, area_name, area_data)
    rows = []
    missing = 0
    discharged = 0
    for req in area_data.get("requirements", []) or []:
        rid = req.get("id", "?")
        if req.get("status") == "deferred" or req.get("type") == "non-functional":
            continue
        w = req.get("witness") or {}
        # A permission is discharged only when EVERY permitted outcome is:
        # proving one of several allowed behaviors reachable says nothing
        # about the others.
        entries = witness_entries(req)
        multi = len(entries) > 1 or (w.get("outcomes") or [])
        trace_rel = w.get("trace")
        status = w.get("status", "not-run")
        detail = ""
        if status == "skipped":
            # Justified skip discharges the obligation (rejection requirement —
            # the proof is an invariant). Unjustified skip is a gate failure.
            discharge = skip_discharge(w)
            if discharge:
                status, detail = "skipped", discharge
                discharged += 1
            else:
                status, detail = ("SKIPPED-UNJUSTIFIED",
                                  "no justification or enforced_by — does not discharge")
                missing += 1
            rows.append((rid, status, trace_rel or "—", detail))
            continue
        worst_status, worst_detail, entry_traces, bad_entries = None, "", [], 0
        for label, entry in entries:
            e_status = entry.get("status", "not-run") if multi else status
            e_trace = entry.get("trace") if multi else trace_rel
            e_detail = ""
            ok = False
            if e_trace:
                entry_traces.append(e_trace)
                trace_path = root / "specs" / e_trace
                if not trace_path.exists():
                    e_status, e_detail = "MISSING-FILE", str(trace_path)
                else:
                    t, errs = load_trace(trace_path)
                    e_detail = (f"{len(t['states'])} states" if not errs
                                else f"invalid: {errs[0]}")
                    if errs:
                        e_status = "INVALID"
                    elif e_status == "witnessed" and is_hollow(t):
                        # The recorded status says witnessed; the trace says
                        # the predicate held at init. The trace wins. Checked
                        # here as well as at mint time so that witnesses
                        # stamped before this gate existed \u2014 or edited by
                        # hand \u2014 cannot keep a proof the evidence never
                        # supported.
                        e_status = "HOLLOW"
                        e_detail = ("predicate already true in the initial state "
                                    "(1-state counterexample) \u2014 proves nothing "
                                    "happened; add a witness.delta")
                    elif e_status == "witnessed":
                        stamped = entry.get("model_sha")
                        if current_sha is None:
                            # Model files unhashable (e.g. probes recorded but
                            # missing): freshness UNVERIFIABLE \u2014 that must gate,
                            # not silently discharge.
                            e_status = "UNVERIFIABLE"
                            e_detail = ("model files can't be hashed (probes file "
                                        "missing?) \u2014 freshness unverifiable")
                        elif not stamped:
                            e_status = "UNSTAMPED"
                            e_detail = ("no model_sha \u2014 freshness unverifiable; "
                                        "re-run /spec-check to pin")
                        elif stamped != current_sha:
                            e_status = "STALE"
                            e_detail = ("model changed since trace was found \u2014 "
                                        "re-run /spec-check")
                        else:
                            ok = True
            elif e_status == "witnessed":
                e_status = "MISSING-FILE"
                e_detail = "(status says witnessed but no trace recorded)"
            if not ok:
                bad_entries += 1
                if worst_status is None:
                    worst_status, worst_detail = e_status, e_detail
                    if multi:
                        worst_detail = f"{label}: {e_detail}" if e_detail else label

        if bad_entries:
            missing += 1
            status = worst_status or "not-run"
            detail = worst_detail
        else:
            discharged += 1
            status = "witnessed"
            detail = (f"{len(entries)} permitted outcome(s) witnessed" if multi
                      else worst_detail or detail)
        shown = ", ".join(entry_traces) if multi else (trace_rel or "\u2014")
        rows.append((rid, status, shown or "\u2014", detail))
    return rows, missing, discharged


def cmd_status(args):
    root = Path(args.root)
    area_path = area_json_path(root, args.area)
    if not area_path.exists():
        print(f"ERROR: {area_path} not found", file=sys.stderr)
        sys.exit(2)
    area = json.loads(area_path.read_text(encoding="utf-8"))
    rows, missing, discharged = witness_status(root, args.area, area)

    if not rows:
        print(f"{args.area}: no requirements declared.")
        return
    width = max(len(r[0]) for r in rows)
    for rid, status, trace_rel, detail in rows:
        print(f"{rid:<{width}}  {status:<20} {trace_rel}"
              + (f"  ({detail})" if detail else ""))
    print(f"\n{discharged}/{len(rows)} witness obligations discharged "
          f"(witnessed-and-fresh or justified-skip).")
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

    ps = sub.add_parser("spec-sha",
                        help="Semantic spec sha — the pin a prose brief is written against.")
    ps.add_argument("area")
    ps.add_argument("--root", default=".")
    ps.set_defaults(func=cmd_spec_sha)

    pms = sub.add_parser("meaning-sha",
                         help="Per-requirement sha — the pin a requirement's "
                              "plain-words meaning is written against.")
    pms.add_argument("area")
    pms.add_argument("--req", help="Only this requirement ID.")
    pms.add_argument("--root", default=".")
    pms.set_defaults(func=cmd_meaning_sha)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
