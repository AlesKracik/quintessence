#!/usr/bin/env python3
"""Re-validate a stale witness by replaying its trace (spec-record check).

A witness is a concrete trace: initP, then steps of stepP, ending in a state
that violates the probe `witness_<REQ>`. When the model changes, the stamp
goes stale, but the trace may still be an execution of the NEW initP/stepP
that still ends where the probe says. Then the witness still holds, and
proving it again from scratch adds nothing.

The trace becomes one `run` in the probe module:

    initP.expect(<state 0>)
         .expect(<domain of each pick>).then(<stepP's branch, picks bound>)
         .expect(<state 1>) ...
         .expect(not(witness_<REQ>))

Each step is stepP's own branch for the action the trace took, with stepP's
`nondet` names bound to the arguments the trace recorded, after checking
each pick is still in its `oneOf` domain: so every step is a stepP step.
Every state asserts the model's variables and _lastAction exactly, and the
last one asserts the probe is violated. `quint test` runs them all in one
call; a run that passes re-stamps its witness, anything else goes to
Apalache as before. Witnesses only: an obligation or invariant is a claim
about every step, and one surviving trace says nothing about it.

A trace this cannot render (an initial state the model picks
nondeterministically, a stepP branch it cannot read, a value with no Quint
literal) is not replayed — it is never counted as revalidated.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_source  # noqa: E402

_NONDET_RE = re.compile(r"\bnondet\s+([A-Za-z_]\w*)\s*=\s*oneOf\(")
_ACTION_RE = re.compile(r"_lastAction'\s*=\s*\"([^\"]+)\"")
_CALL_RE = re.compile(r"^\s*all\s*\{\s*([A-Za-z_][\w:]*)\s*(?:\(([^()]*)\))?\s*,")
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")


class Unreplayable(Exception):
    pass


def _close(code, start, open_ch, close_ch):
    """Index of the bracket closing the one at `start`, or None."""
    depth = 0
    for i in range(start, len(code)):
        if code[i] == open_ch:
            depth += 1
        elif code[i] == close_ch:
            depth -= 1
            if not depth:
                return i
    return None


def step_branches(probes_text):
    """(nondets {name: domain expr}, branches {action: (branch text, [arg
    expr]})) from the probe module's stepP."""
    decls = spec_source.declarations(probes_text)
    body = (decls.get("stepP") or {}).get("body") or ""
    code = spec_source._code_only(body)
    nondets = {}
    for m in _NONDET_RE.finditer(code):
        end = _close(code, m.end() - 1, "(", ")")
        if end is not None:
            nondets[m.group(1)] = body[m.end():end].strip()
    m = re.search(r"\bany\s*\{", code)
    i = _close(code, m.end() - 1, "{", "}") if m else None
    if i is None:
        return nondets, {}
    branches = {}
    for part in spec_source._split_top(body[m.end():i]):
        am, cm = _ACTION_RE.search(part), _CALL_RE.match(part)
        if not (am and cm):
            continue
        args = [a.strip() for a in spec_source._split_top(cm.group(2))] if cm.group(2) else []
        branches.setdefault(am.group(1), (part.strip(), args))
    return nondets, branches


def _literal(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        if "#bigint" in v:
            return str(int(v["#bigint"]))
        if "#set" in v:
            return "Set(" + ", ".join(_literal(x) for x in v["#set"]) + ")"
        if "#map" in v:
            return "Map(" + ", ".join(f"{_literal(k)} -> {_literal(x)}"
                                      for k, x in v["#map"]) + ")"
        if "#tup" in v:
            return "(" + ", ".join(_literal(x) for x in v["#tup"]) + ")"
        if set(v) == {"tag", "value"}:
            inner = v["value"]
            if inner in ({"#tup": []}, {}, {"tag": "UNIT"}):
                return v["tag"]
            return f"{v['tag']}({_literal(inner)})"
        if any(k.startswith("#") for k in v):
            raise Unreplayable(f"no Quint literal for {sorted(v)}")
        return "{ " + ", ".join(f"{k}: {_literal(x)}" for k, x in v.items()) + " }"
    raise Unreplayable(repr(v))


def _state(st, scope=None):
    """The model's variables and the step label, exactly as the trace has
    them. `scope`: the variables the probe module can name (an imported
    module's, imported qualified, it cannot); None for all."""
    keys = sorted(k for k in st if not k.startswith("#") and not k.startswith("mbt::")
                  and (not k.startswith("_") or k == "_lastAction")
                  and (scope is None or k in scope or k == "_lastAction"))
    return " and ".join(f"{k} == {_literal(st[k])}" for k in keys) or "true"


def _bind(text, picks):
    code = spec_source._code_only(text)
    out, pos = [], 0
    for m in _IDENT.finditer(code):
        if m.group(0) in picks:
            out.append(text[pos:m.start()])
            out.append(picks[m.group(0)])
            pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def replay_run(name, trace, probe, nondets, branches, scope=None):
    """The `run` line replaying `trace` and asserting it still violates
    `probe`. Raises Unreplayable when a step cannot be rebuilt.

    The state assertions say where a replay parts from its trace; what
    makes the witness hold is that every step is taken (a step whose action
    is not enabled fails the run) and the last state violates the probe."""
    states = trace.get("states") or []
    if len(states) < 2:
        raise Unreplayable("a witness needs at least one step")
    params = ((trace.get("#meta") or {}).get("action_params")) or {}
    chain = f"initP.expect({_state(states[0], scope)})"
    for st in states[1:]:
        action = st.get("_lastAction")
        if action not in branches:
            raise Unreplayable(f"stepP has no branch for {action!r}")
        text, args = branches[action]
        ghosts = params.get(action)
        if ghosts is None or len(ghosts) != len(args):
            raise Unreplayable(f"the trace does not say {action}'s arguments")
        picks = {}
        for arg, ghost in zip(args, ghosts):
            if arg not in nondets or ghost not in st:
                raise Unreplayable(f"{action}'s argument {arg!r} is not a plain pick")
            picks[arg] = _literal(st[ghost])
        unbound = {m.group(0) for m in _IDENT.finditer(spec_source._code_only(text))
                   if m.group(0) in nondets} - set(picks)
        if unbound:
            raise Unreplayable(f"{action} reads picks the trace does not record: "
                               f"{sorted(unbound)}")
        for arg in args:
            chain += f".expect({nondets[arg]}.contains({picks[arg]}))"
        chain += f".then({_bind(text, picks)}).expect({_state(st, scope)})"
    chain += f".expect(not({probe}))"
    return f"  run {name} = {chain}"


def run_name(probe):
    return "replay_" + probe


def with_runs(probes_text, lines, module_name, import_prefix, rebase):
    """The probe module, renamed and rebased to sit `import_prefix` away
    (`rebase(text, module_name, import_prefix)`), with `lines` added."""
    text = rebase(probes_text, module_name, import_prefix).rstrip()
    close = text.rfind("}")
    return (text[:close] + "\n  // -- replayed witnesses (spec-record) --\n"
            + "\n".join(lines) + "\n" + text[close:] + "\n")


TEST_LINE = re.compile(r"^\s*(?:ok|\d+\))\s+(\S+)\s+(passed|failed|ignored)\b", re.M)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_test_output(out):
    """{run name: 'passed' | 'failed' | 'ignored'} from `quint test` output."""
    return {m.group(1): m.group(2) for m in TEST_LINE.finditer(ANSI.sub("", out or ""))}


def build(probes_text, model_text, candidates, module_name, import_prefix, rebase):
    """(module text or None, {run name: candidate}, [(label, why not)]).

    `candidates`: [(label, probe, trace)] — the stale witnesses to replay."""
    nondets, branches = step_branches(probes_text)
    scope = {n for n, h in spec_source.declarations(model_text or "").items()
             if h.get("kind") == "var"}
    lines, runs, skipped = [], {}, []
    for cand in candidates:
        label, probe, trace = cand[:3]
        name = run_name(probe)
        try:
            lines.append(replay_run(name, trace, probe, nondets, branches, scope))
        except Unreplayable as e:
            skipped.append((label, str(e)))
            continue
        runs[name] = cand
    if not lines:
        return None, runs, skipped
    return with_runs(probes_text, lines, module_name, import_prefix, rebase), runs, skipped
