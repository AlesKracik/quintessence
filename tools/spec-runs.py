#!/usr/bin/env python3
"""
spec-runs.py — Every example and journey run, as a trace the code replays.

A witness trace shows a requirement CAN happen, along whatever path the
checker found. An example says what a person approved: this call, from this
state, ends here — or is refused. A journey run says the flow works end to
end. Both are checked against the MODEL by `quint test`; this tool makes
them checkable against the CODE, through the same replay harness the
witness traces go through.

For each `@example` run and each journey `run` of an area it

  1. runs the run under `quint test` and keeps its trace (the states the
     model passes through);
  2. labels every step the way a witness trace is labelled — the action in
     `_lastAction`, each argument in its `_last<Param>` ghost — so the
     harness replays it with the code it already has;
  3. adds a state for every call the run expects to be REFUSED
     (`A(args).fail()`): the model's state does not move, so the state is a
     copy of the one before, marked `_refused: true`. The harness calls the
     action, asserts the code refuses it, and asserts nothing changed. This
     is the only code-side evidence a prohibition gets.

Traces land in specs/<area>/traces/runs/<run>.itf.json, stamped with the
model_sha they were made from; `--check` (no quint needed) reports any that
are missing or older than the model, and spec-record verify refuses to
replay until they are current.

Usage:
  tools/spec-runs.py <area>            # (re)generate the area's run traces
  tools/spec-runs.py <area> --check    # are they all there and current?

Exit codes: 0 = done / current, 1 = --check found traces missing or stale,
2 = setup problem.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spec_source  # noqa: E402
from itf_tools import (compute_model_sha, ghost_for_param, journeys_dir,  # noqa: E402
                       soften_stdout)
from quint_ir import parse_qnt  # noqa: E402

REFUSED_VAR = "_refused"
ACTION_VAR = "_lastAction"


def fail_setup(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


# ── Which runs ──────────────────────────────────────────────────────────────

def area_runs(root, area_name, area, decls):
    """[(run name, label)]: every example run, then every journey run that
    lives in this area's model. A run is listed once, under its first label."""
    runs = {n for n, h in decls.items() if h.get("kind") == "run"}
    out, seen = [], set()
    for ex in area.get("examples") or []:
        name = ex.get("quint_run")
        if name in runs and name not in seen:
            out.append((name, ex.get("id")))
            seen.add(name)
    jdir = journeys_dir(root)
    for p in sorted(jdir.glob("*.journey.json")) if jdir.exists() else []:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ref = j.get("run") or ""
        if "." in ref:
            a, name = ref.split(".", 1)
            if a == area_name and name in runs and name not in seen:
                out.append((name, f"journey: {j.get('name', p.stem)}"))
                seen.add(name)
    return out


def trace_path(root, area_name, run):
    return Path(root) / "specs" / area_name / "traces" / "runs" / f"{run}.itf.json"


def status(root, area_name, area=None):
    """[(run, label, status)] with status current | missing | stale | invalid —
    without quint: it reads the stamped model_sha."""
    area = area if area is not None else spec_source.load_area(root, area_name)
    rel = (area.get("formal_model") or {}).get("quint_file") or f"{area_name}.qnt"
    try:
        text = (spec_source.specs_dir(root) / rel).read_text(encoding="utf-8")
    except OSError:
        return []
    if not spec_source.has_model(text):
        return []
    decls = spec_source.declarations(text)
    sha = compute_model_sha(root, area_name, area)
    out = []
    for run, label in area_runs(root, area_name, area, decls):
        p = trace_path(root, area_name, run)
        if not p.exists():
            out.append((run, label, "missing"))
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8")).get("#meta") or {}
        except (OSError, ValueError):
            out.append((run, label, "invalid"))
            continue
        out.append((run, label, "current" if meta.get("model_sha") == sha else "stale"))
    return out


# ── Quint literals -> ITF ───────────────────────────────────────────────────

class NotLiteral(Exception):
    pass


TOKEN = re.compile(r'\s*(?:("(?:\\.|[^"\\])*")|(-?\d+)|([A-Za-z_][A-Za-z0-9_]*'
                   r'(?:::[A-Za-z_][A-Za-z0-9_]*)?)|(->|[(){}\[\],:]))')


def _tokens(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise NotLiteral(text)
        out.append(next(g for g in m.groups() if g is not None))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


def to_itf(text, variants=(), consts=None):
    """A Quint literal as an ITF value: strings, integers, booleans,
    variants (with or without a payload), Set, Map, List, tuples, records,
    and a `pure val` constant by name. Anything else is not a literal, and an
    example's call should not need one."""
    consts = consts or {}
    toks = _tokens(text)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take(expected=None):
        nonlocal pos
        tok = peek()
        if tok is None or (expected and tok != expected):
            raise NotLiteral(text)
        pos += 1
        return tok

    def items(close):
        out = []
        if peek() == close:
            take(close)
            return out
        while True:
            out.append(value())
            if peek() == ",":
                take(",")
                if peek() == close:
                    take(close)
                    return out
                continue
            take(close)
            return out

    def value():
        tok = take()
        if tok.startswith('"'):
            return json.loads(tok)
        if re.fullmatch(r"-?\d+", tok):
            return {"#bigint": str(int(tok))}
        if tok in ("true", "false"):
            return tok == "true"
        if tok == "(":
            vals = items(")")
            return vals[0] if len(vals) == 1 else {"#tup": vals}
        if tok == "[":
            return items("]")
        if tok == "{":
            rec = {}
            while peek() != "}":
                field = take()
                take(":")
                rec[field] = value()
                if peek() == ",":
                    take(",")
            take("}")
            return rec
        if tok in ("Set", "List") and peek() == "(":
            take("(")
            vals = items(")")
            return {"#set": vals} if tok == "Set" else vals
        if tok == "Map" and peek() == "(":
            take("(")
            pairs = []
            while peek() != ")":
                k = value()
                take("->")
                pairs.append([k, value()])
                if peek() == ",":
                    take(",")
            take(")")
            return {"#map": pairs}
        bare = tok.split("::")[-1]
        if bare in variants:
            if peek() == "(":
                take("(")
                vals = items(")")
                payload = vals[0] if len(vals) == 1 else {"#tup": vals}
                return {"tag": bare, "value": payload}
            return {"tag": bare, "value": {"#tup": []}}
        if tok in consts:
            return to_itf(consts[tok], variants, {k: v for k, v in consts.items() if k != tok})
        raise NotLiteral(tok)

    v = value()
    if pos != len(toks):
        raise NotLiteral(text)
    return v


# ── Labelling a run's trace ─────────────────────────────────────────────────

def labelled(trace, steps, params_of, variants, consts, meta):
    """The run's `quint test` trace with every step labelled.

    `steps` are the run's calls (spec_source.run_calls); `params_of(action)`
    gives an action's parameter names. A call the run refuses gets a state of
    its own — a copy of the state before it, `_refused: true`. Ghosts a step
    does not set keep their value, and before a ghost is first set it holds
    the first value it takes: the harness reads only the ghosts of the action
    it calls."""
    states = [dict(s) for s in trace.get("states") or []]
    moving = [s for s in steps if not s.get("fails")]
    # quint writes a state for a refused step too — with nothing assigned in
    # it, since the refused action assigned nothing. A trace without one is
    # read the same way.
    per_step = len(states) == len(steps) + 1
    if not per_step and len(states) != len(moving) + 1:
        raise ValueError(f"the trace has {len(states)} states; the run makes "
                         f"{len(steps)} call(s), {len(moving)} of them moving it, so it "
                         f"should have {len(steps) + 1} or {len(moving) + 1}")
    labels = []
    for s in steps:
        names = params_of(s["action"], s.get("qualifier"))
        args = spec_source._split_top(s.get("args") or "") if s.get("parens") else []
        if len(args) != len(names):
            raise ValueError(f"`{s['action']}` takes {len(names)} argument(s); the run "
                             f"passes {len(args)}")
        values = {}
        for n, a in zip(names, args):
            try:
                values[ghost_for_param(n)] = to_itf(a, variants, consts)
            except NotLiteral as e:
                raise ValueError(f"argument `{a}` of `{s['action']}` is not a literal "
                                 f"({e}); give the run literal arguments") from None
        name = f"{s['qualifier']}::{s['action']}" if s.get("qualifier") else s["action"]
        labels.append((name, values, bool(s.get("fails"))))

    ghosts = []
    for _n, values, _f in labels:
        for g in values:
            if g not in ghosts:
                ghosts.append(g)
    first = {g: next(v[g] for _n, v, _f in labels if g in v) for g in ghosts}

    out = []
    base = {k: v for k, v in states[0].items() if k != "#meta"}
    current = dict(first)
    out.append(dict(base, **{ACTION_VAR: "init", REFUSED_VAR: False}, **current))
    i = 1
    for name, values, refused in labels:
        current = dict(current, **values)
        if refused:
            model = {k: v for k, v in out[-1].items()
                     if k not in ghosts and k not in (ACTION_VAR, REFUSED_VAR)}
            if per_step:
                written = {k: v for k, v in states[i].items() if k != "#meta"}
                # Unassigned is fine (that is what quint writes); a value that
                # differs from the state before is not.
                diff = sorted(k for k in written if written[k] != model.get(k))
                if diff:
                    raise ValueError(
                        f"the model's state moved on `{name}`, a call the run expects "
                        f"refused: " + "; ".join(
                            f"{k}: {json.dumps(model.get(k))[:120]} -> "
                            f"{json.dumps(written.get(k))[:120]}" for k in diff[:3]))
                i += 1
        else:
            model = {k: v for k, v in states[i].items() if k != "#meta"}
            i += 1
        out.append(dict(model, **{ACTION_VAR: name, REFUSED_VAR: refused}, **current))
    for idx, s in enumerate(out):
        s["#meta"] = {"index": idx}
    variables = list(trace.get("vars") or [k for k in base if not k.startswith("#")])
    variables += [v for v in [ACTION_VAR, REFUSED_VAR] + ghosts if v not in variables]
    return {"#meta": dict(meta, format="ITF",
                          **{"format-description":
                             "https://apalache-mc.org/docs/adr/015adr-trace.html"}),
            "vars": variables, "states": out}


# ── Running quint ───────────────────────────────────────────────────────────

def _out_flag(exe):
    help_text = subprocess.run([exe, "test", "--help"], capture_output=True,
                               text=True).stdout
    for flag in ("--out-itf", "--output"):
        if flag in help_text:
            return flag
    fail_setup("this quint's `test` writes no ITF traces (no --out-itf or --output).")


def generate(root, area_name, jobs=4, timeout=180):
    area = spec_source.load_area(root, area_name)
    if area is None:
        fail_setup(f"no area '{area_name}' under {root}/specs")
    rel = (area.get("formal_model") or {}).get("quint_file") or f"{area_name}.qnt"
    model = spec_source.specs_dir(root) / rel
    try:
        text = model.read_text(encoding="utf-8")
    except OSError:
        fail_setup(f"{model} not found — the area has no model to run.")
    decls = spec_source.declarations(text)
    runs = area_runs(root, area_name, area, decls)
    if not runs:
        print(f"{area_name}: no example or journey runs")
        return 0
    exe = shutil.which("quint")
    if not exe:
        fail_setup("`quint` not on PATH. Run tools/check-tooling.sh for install hints.")
    ir = parse_qnt(model, engine="regex") or {}
    imported = spec_source.imported_declarations(root, text)
    variants = {v for vs in (ir.get("type_variants") or {}).values() for v in vs}
    for other in imported.values():
        for h in other.values():
            if h.get("kind") == "type":
                variants |= set(spec_source.type_variants(h.get("body")))
    consts = {n: (h.get("body") or "").strip() for n, h in decls.items()
              if h.get("kind") == "val" and h.get("pure")}

    def params_of(action, qualifier=None):
        host = (imported.get(qualifier) or {}).get(action) if qualifier else decls.get(action)
        return [p for p, _t in (host or {}).get("params") or []]

    sha = compute_model_sha(root, area_name, area)
    flag = _out_flag(exe)
    outdir = trace_path(root, area_name, "x").parent
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        for p in spec_source.specs_dir(root).glob("*.qnt"):
            shutil.copy(p, Path(tmp) / p.name)

        def one(item):
            run, label = item
            raw = Path(tmp) / f"{run}.raw.itf.json"
            cmd = [exe, "test", Path(rel).name, "--match", f"^{re.escape(run)}$",
                   f"{flag}={raw}"]
            p = subprocess.run(cmd + ["--max-samples=1"], cwd=tmp, capture_output=True,
                               text=True, timeout=timeout)
            if p.returncode != 0 and "max-samples" in (p.stderr or ""):
                p = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                                   timeout=timeout)
            if p.returncode != 0 or not raw.exists():
                tail = " ".join(((p.stdout or "") + (p.stderr or "")).split())[-400:]
                return run, label, False, f"quint test did not produce a trace: {tail}"
            try:
                trace = json.loads(raw.read_text(encoding="utf-8"))
                out = labelled(trace, spec_source.run_calls(decls, run), params_of,
                               variants, consts,
                               {"source": f"{rel} run {run} (tools/spec-runs.py)",
                                "run": run, "label": label, "model_sha": sha})
            except (ValueError, KeyError) as e:
                return run, label, False, str(e)
            trace_path(root, area_name, run).write_text(
                json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            refusals = sum(1 for s in out["states"] if s.get(REFUSED_VAR))
            return run, label, True, (f"{len(out['states']) - 1} step(s)"
                                      + (f", {refusals} refused" if refusals else ""))

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            results = list(pool.map(one, runs))

    bad = 0
    for run, label, ok, what in results:
        bad += not ok
        print(f"{'ok ' if ok else 'ERR'} {run:<28} {label:<28} {what}")
    keep = {f"{r}.itf.json" for r, _l in runs}
    for p in outdir.glob("*.itf.json"):
        if p.name not in keep:
            p.unlink()
    return 2 if bad else 0


def main(argv=None):
    soften_stdout()
    ap = argparse.ArgumentParser(description="Example and journey runs as replayable traces.")
    ap.add_argument("area")
    ap.add_argument("--root", default=".")
    ap.add_argument("--check", action="store_true",
                    help="report missing or stale run traces; needs no quint")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.check:
        rows = status(root, args.area)
        for run, label, st in rows:
            print(f"{st:<8} {run:<28} {label}")
        off = [r for r in rows if r[2] != "current"]
        if off:
            print(f"\n{len(off)} of {len(rows)} run trace(s) not current — run "
                  f"tools/spec-runs.py {args.area}")
            return 1
        return 0
    return generate(root, args.area, jobs=args.jobs)


if __name__ == "__main__":
    sys.exit(main())
