#!/usr/bin/env python3
"""
spec-questions.py — Model mutation: what do the examples leave open?

The examples are the part of a spec a person approves, row by row. The model
is the part an agent writes. Between them is a seam: a model can pass every
approved example and still decide something nobody approved — reset a
counter, allow a step, pick a branch. This tool measures that seam and turns
what it finds into questions a person can answer with yes or no.

  1. MUTATE the model one small change at a time, only where behavior lives:
     the actions, the helpers they read and the constants they use. Never an
     invariant or a witness predicate — those are claims ABOUT the behavior.
       cmp      a comparison flipped at its boundary (`>` <-> `>=`, `==` <-> `!=`)
       guard    a precondition of an action dropped (replaced by `true`)
       effect   an assignment dropped (`x' = e` -> `x' = x`)
       variant  a state written as another variant of its type
       const    a numeric constant moved by one
  2. RUN what a person approved against each mutant: every `run` under
     `quint test` (examples and journey runs), then every invariant under the
     simulator. A mutant that turns something red is KILLED — that behavior
     is pinned, and the report says by what.
  3. A mutant nothing notices SURVIVED. Random runs of the model are replayed
     on the mutant, step by step with the state asserted after each step,
     and the first step where they disagree becomes a scenario: Given the
     calls before it, When that call — and a question. "Should X be Y? The
     model says so; a model where it is not passes every example too." If
     the model's runs never part from the mutant's, the mutant's own runs are
     replayed on the model, which finds a step the mutant allows and the
     model refuses.

The answer to a question is an example. Yes: add the run printed with it
(an `@example` on the requirement it names), and that behavior is pinned —
the mutant dies on the next run. No: the model is wrong, found before any
code exists.

Spec mutation score = mutants killed by examples (journey runs included) /
mutants that change behavior. Left out of both: mutants that do not compile
("stillborn") and survivors no replay told apart from the model ("no
difference found" — an equivalent mutant, or a difference deeper than the
bounds). A survivor with a question counts against the score; a mutant only
an invariant or an un-annotated run kills counts in the denominator only —
the examples did not pin it, but nothing is open either.

Needs quint. Nothing here is a verdict: replays are bounded and random, so
"no difference found" is not "equivalent".

Usage:
  tools/spec-questions.py <area> --dry-run         # list the mutants, run nothing
  tools/spec-questions.py <area>                   # mutate, run, report
  tools/spec-questions.py <area> --record          # also write check_results.mutation
  tools/spec-questions.py <area> --limit 40 --ops cmp,guard --seed 3
  tools/spec-questions.py <area> --json            # machine-readable report

Exit codes: 0 = ran (survivors are findings, not failures), 2 = setup problem
(no quint, the model fails its own examples or invariants, nothing to mutate).
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spec_source  # noqa: E402
from itf_tools import compute_model_sha, soften_stdout  # noqa: E402
from quint_ir import parse_qnt  # noqa: E402

OPS = ("cmp", "guard", "effect", "variant", "const")
DEFAULT_LIMIT = 40

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
QNAME = r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?"
ASSIGN_RE = re.compile(rf"^\s*({QNAME})\s*'\s*=(?!=)")
CALL_RE = re.compile(rf"^\s*({QNAME})\s*(\(.*\))?\s*$", re.S)
CMP_RE = re.compile(r">=|<=|==|!=|>|<")
CMP_SWAP = {">=": ">", ">": ">=", "<=": "<", "<": "<=", "==": "!=", "!=": "=="}
CONST_RE = re.compile(r"^\s*pure\s+val\s+([A-Za-z_]\w*)\s*:\s*int\s*=\s*(-?\d+)\b")
IMPORT_RE = re.compile(r'^\s*import\s+([A-Za-z_]\w*)(?:\.\*)?\s+from\s+"([^"]+)"', re.M)
PROPERTY_TAGS = {"req", "outcome-of", "inv", "prop"}


def fail_setup(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


# ── Reading the model ───────────────────────────────────────────────────────

def mask_quint(text):
    """`text` with comments and string contents blanked, length and lines
    preserved — every offset in the mask addresses the same character in the
    text. A string keeps its quotes so brackets and commas still balance."""
    out = list(text)
    i, n = 0, len(text)
    state = None
    while i < n:
        ch, nxt = text[i], text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == '"':
                state = "str"
            elif ch == "/" and nxt == "/":
                state = "line"
                out[i] = " "
            elif ch == "/" and nxt == "*":
                state = "block"
                out[i] = " "
        elif state == "line":
            if ch == "\n":
                state = None
            else:
                out[i] = " "
        elif state == "block":
            if ch == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 1
                state = None
            elif ch != "\n":
                out[i] = " "
        else:
            if ch == "\\":
                out[i] = " "
                if i + 1 < n and text[i + 1] != "\n":
                    out[i + 1] = " "
                i += 1
            elif ch == '"':
                state = None
            elif ch != "\n":
                out[i] = " "
        i += 1
    return "".join(out)


def _line_starts(text):
    starts, pos = [0], 0
    for line in text.split("\n")[:-1]:
        pos += len(line) + 1
        starts.append(pos)
    return starts


def decl_spans(text, decls):
    """name -> (start, end) offsets of each declaration's source."""
    starts = _line_starts(text)
    out = {}
    for name, h in decls.items():
        src = h.get("source")
        if not src or not h.get("line"):
            continue
        s = starts[h["line"] - 1]
        out[name] = (s, s + len(src))
    return out


def _close(masked, i, open_ch="{", close_ch="}"):
    """Index of the bracket closing the one at masked[i]."""
    depth = 0
    for k in range(i, len(masked)):
        if masked[k] == open_ch:
            depth += 1
        elif masked[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _blocks(masked, start, end, keyword):
    """(inner_start, inner_end) of every `keyword { ... }` in [start, end)."""
    out = []
    for m in re.finditer(rf"\b{keyword}\s*\{{", masked[start:end]):
        brace = start + m.end() - 1
        close = _close(masked, brace)
        if 0 < close <= end:
            out.append((brace + 1, close))
    return out


def _parts(masked, s, e):
    """Top-level comma-separated parts of masked[s:e], as trimmed spans."""
    parts, depth, cur = [], 0, s
    for k in range(s, e):
        c = masked[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append((cur, k))
            cur = k + 1
    parts.append((cur, e))
    out = []
    for a, b in parts:
        seg = masked[a:b]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if seg.strip():
            out.append((a + lead, b - trail))
    return out


def imported_decls(root, text):
    """alias -> declarations of each module this model imports."""
    out = {}
    for alias, rel in IMPORT_RE.findall(text):
        path = spec_source.specs_dir(root) / (rel[2:] if rel.startswith("./") else rel)
        if not path.suffix:
            path = path.with_suffix(".qnt")
        try:
            out[alias] = spec_source.declarations(path.read_text(encoding="utf-8"))
        except OSError:
            out[alias] = {}
    return out


def _is_action_call(part, decls, imported):
    m = CALL_RE.match(part)
    if not m:
        return False
    name = m.group(1)
    if "::" in name:
        alias, member = name.split("::", 1)
        other = imported.get(alias)
        if other is None:
            return True          # unknown module: leave it alone
        return (other.get(member) or {}).get("kind") == "action"
    return (decls.get(name) or {}).get("kind") == "action"


def behavior_decls(text, decls):
    """The declarations behavior lives in: every action but `step`, and the
    defs and vals those actions read — minus anything hosting a requirement,
    outcome, invariant or property record, which is a claim about behavior
    rather than behavior."""
    hosts = {(r.get("host") or {}).get("name")
             for r in spec_source.read_records(text) if r["tag"] in PROPERTY_TAGS
             and (r.get("host") or {}).get("kind") in ("def", "val", "temporal")}
    actions = [n for n, h in decls.items() if h.get("kind") == "action" and n != "step"]
    reach = spec_source.name_closure(decls, actions)
    out = []
    for name, h in decls.items():
        kind = h.get("kind")
        if kind == "action" and name != "step":
            out.append(name)
        elif kind in ("def", "val") and not h.get("pure") and name in reach \
                and name not in hosts:
            out.append(name)
        elif kind == "def" and h.get("pure") and name in reach and name not in hosts:
            out.append(name)
    return out


def mutation_sites(text, decls, ir, imported=None, ops=OPS):
    """Every candidate mutant of a model, as dicts:
    {op, decl, line, start, end, orig, repl, label}. Pure: reads text only."""
    imported = imported or {}
    masked = mask_quint(text)
    spans = decl_spans(text, decls)
    starts = _line_starts(text)
    variants = {}
    for tname, vs in (ir.get("type_variants") or {}).items():
        if len(vs) > 1:
            for v in vs:
                variants[v] = vs
    out = []

    def line_of(off):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def snippet(off):
        ln = line_of(off)
        line = text[starts[ln - 1]:].split("\n", 1)[0]
        return spec_source._strip_line_comment(line).strip().rstrip(",").strip()

    def add(op, decl, s, e, repl, label):
        out.append({"op": op, "decl": decl, "line": line_of(s), "start": s, "end": e,
                    "orig": text[s:e], "repl": repl, "label": label})

    for name in behavior_decls(text, decls):
        if name not in spans:
            continue
        s0, e0 = spans[name]
        kind = decls[name].get("kind")
        # Skip the header up to the `=` that opens the body: parameter types
        # are not behavior.
        eq = masked.find("=", s0, e0)
        body_s = eq + 1 if eq >= 0 else s0
        conjuncts = []
        if kind == "action":
            for bs, be in _blocks(masked, body_s, e0, "all"):
                conjuncts.extend(_parts(masked, bs, be))
        assigns = [(a, b) for a, b in conjuncts if ASSIGN_RE.match(masked[a:b])]

        if "cmp" in ops and name != "init":
            for m in CMP_RE.finditer(masked, body_s, e0):
                tok, s, e = m.group(0), m.start(), m.end()
                before = masked[s - 1] if s else " "
                after = masked[e] if e < len(masked) else " "
                if before in "=<>!-" or after in "=>":
                    continue
                if tok in (">", "<") and (before == "-" or after == "-"):
                    continue
                add("cmp", name, s, e, CMP_SWAP[tok],
                    f"`{snippet(s)}`: `{tok}` -> `{CMP_SWAP[tok]}`")

        if "guard" in ops and kind == "action" and name != "init":
            for a, b in conjuncts:
                part = masked[a:b]
                if ASSIGN_RE.match(part) or "'" in part:
                    continue
                if _is_action_call(part, decls, imported) or part.strip() == "true":
                    continue
                add("guard", name, a, b, "true",
                    f"drop the guard `{' '.join(text[a:b].split())}`")

        if "effect" in ops and kind == "action" and name != "init":
            for a, b in assigns:
                m = ASSIGN_RE.match(masked[a:b])
                lhs = m.group(1)
                rhs_s = a + m.end()
                rhs = masked[rhs_s:b].strip()
                if rhs == lhs:
                    continue
                lead = len(masked[rhs_s:b]) - len(masked[rhs_s:b].lstrip())
                add("effect", name, rhs_s + lead, b, lhs,
                    f"drop the effect on `{lhs}` (keep it as it was)")

        if "variant" in ops and kind == "action":
            for a, b in assigns:
                m = ASSIGN_RE.match(masked[a:b])
                for t in IDENT.finditer(masked, a + m.end(), b):
                    tok = t.group(0)
                    vs = variants.get(tok)
                    if not vs:
                        continue
                    prev = masked[t.start() - 1] if t.start() else ""
                    nxt = masked[t.end():t.end() + 1]
                    if prev in ".:" or nxt in ("(", ":"):
                        continue
                    repl = vs[(vs.index(tok) + 1) % len(vs)]
                    add("variant", name, t.start(), t.end(), repl,
                        f"`{m.group(1)}` gets `{repl}` where the model writes `{tok}`")

    if "const" in ops:
        for name, h in decls.items():
            if h.get("kind") != "val" or not h.get("pure") or name not in spans:
                continue
            s0, e0 = spans[name]
            first = text[s0:e0].split("\n", 1)[0]
            m = CONST_RE.match(first)
            if not m:
                continue
            value = int(m.group(2))
            num_s = s0 + m.start(2)
            num_e = s0 + m.end(2)
            for new in (value + 1, value - 1):
                if new < 0 and value >= 0:
                    continue
                add("const", name, num_s, num_e, str(new),
                    f"`{name}` = {new} instead of {value}")

    seen, uniq = set(), []
    for mt in out:
        k = (mt["start"], mt["end"], mt["repl"])
        if k not in seen:
            seen.add(k)
            uniq.append(mt)
    for mt in uniq:
        mt["key"] = hashlib.sha1(
            f"{mt['decl']}|{mt['op']}|{mt['orig']}|{mt['repl']}|{mt['start']}".encode()
        ).hexdigest()[:8]
    return uniq


def select(sites, limit):
    """Up to `limit` mutants, spread across operators and declarations so a
    cap does not spend itself on one action's comparisons."""
    by_op = {}
    for s in sites:
        by_op.setdefault(s["op"], []).append(s)
    queues = [by_op[o] for o in OPS if o in by_op]
    out = []
    while len(out) < limit and any(queues):
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    out.sort(key=lambda m: (m["start"], m["repl"]))
    for i, m in enumerate(out, 1):
        m["id"] = f"M{i:02d}"
    return out


def apply(text, mutant):
    return text[:mutant["start"]] + mutant["repl"] + text[mutant["end"]:]


# ── ITF -> Quint ────────────────────────────────────────────────────────────

class Unrenderable(Exception):
    pass


def literal(v):
    """An ITF value as a Quint expression."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(literal(x) for x in v) + "]"
    if isinstance(v, dict):
        if "#bigint" in v:
            return str(int(v["#bigint"]))
        if "#set" in v:
            return "Set(" + ", ".join(literal(x) for x in v["#set"]) + ")"
        if "#map" in v:
            return "Map(" + ", ".join(f"{literal(k)} -> {literal(x)}"
                                      for k, x in v["#map"]) + ")"
        if "#tup" in v:
            return "(" + ", ".join(literal(x) for x in v["#tup"]) + ")"
        if "#unserializable" in v:
            raise Unrenderable(str(v["#unserializable"]))
        if set(v) == {"tag", "value"}:
            inner = v["value"]
            if inner == {"#tup": []} or inner == {}:
                return v["tag"]
            return f"{v['tag']}({literal(inner)})"
        fields = [k for k in v if not k.startswith("#")]
        return "{ " + ", ".join(f"{k}: {literal(v[k])}" for k in fields) + " }"
    raise Unrenderable(repr(v))


def shown(v):
    """An ITF value for a reader: strings bare, variants by name."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if "#bigint" in v:
            return str(int(v["#bigint"]))
        if "#set" in v:
            return "{" + ", ".join(shown(x) for x in v["#set"]) + "}"
        if "#map" in v:
            return "{" + ", ".join(f"{shown(k)}: {shown(x)}" for k, x in v["#map"]) + "}"
        if "#tup" in v:
            return "(" + ", ".join(shown(x) for x in v["#tup"]) + ")"
        if set(v) == {"tag", "value"}:
            inner = v["value"]
            return v["tag"] if inner in ({"#tup": []}, {}) else f"{v['tag']}({shown(inner)})"
        return "{" + ", ".join(f"{k}: {shown(x)}" for k, x in v.items()
                               if not k.startswith("#")) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(shown(x) for x in v) + "]"
    return str(v)


MBT_KEYS = {"action_taken", "nondet_picks"}   # quint before the `mbt::` prefix


def state_vars(state):
    return {k: v for k, v in state.items()
            if not k.startswith("#") and not k.startswith("mbt::") and k not in MBT_KEYS}


def var_names(decls, imported):
    """ITF var name -> how the model refers to it. A trace names an imported
    module's variables bare (`sessions`); inside a model that imports that
    module qualified they are `auth::sessions`."""
    own = {n for n, h in decls.items() if h.get("kind") == "var"}
    out = {}
    for alias, other in (imported or {}).items():
        for n, h in other.items():
            if h.get("kind") == "var" and n not in own:
                out.setdefault(n, f"{alias}::{n}")
    return out


def facts(state, names=None):
    """[(quint condition, reader text)] — one per state var, one per key of a
    map-valued var, so a difference can be named down to the entry."""
    names = names or {}
    out = []
    for var, v in sorted(state_vars(state).items()):
        ref = names.get(var, var)
        if isinstance(v, dict) and "#map" in v and v["#map"]:
            for k, x in v["#map"]:
                out.append((f"{ref}.get({literal(k)}) == {literal(x)}",
                            f"{var}[{shown(k)}] = {shown(x)}"))
        else:
            out.append((f"{ref} == {literal(v)}", f"{var} = {shown(v)}"))
    return out


def state_expr(state, names=None):
    names = names or {}
    return " and ".join(f"{names.get(var, var)} == {literal(v)}"
                        for var, v in sorted(state_vars(state).items())) or "true"


def changed_facts(before, after, names=None):
    old = dict(facts(before, names))
    return [(c, t) for c, t in facts(after, names) if c not in old]


# ── The step relation: how a trace's actions are called ────────────────────

def step_templates(decls):
    """(nondet names, {action: call template}) from the model's `step`.

    A trace records the action taken and the values `nondet` picked; the
    call is rebuilt from the branch of `step` that names the action, with
    the nondet names replaced by what was picked."""
    step = decls.get("step") or {}
    body = step.get("body") or ""
    nondets = re.findall(r"\bnondet\s+([A-Za-z_]\w*)\s*=", body)
    code = spec_source._code_only(body)
    m = re.search(r"\bany\s*\{", code)
    templates = {}
    if m:
        close = _close(code, m.end() - 1)
        for part in spec_source._split_top(body[m.end():close]):
            cm = CALL_RE.match(part)
            if cm and cm.group(1) not in templates:
                templates[cm.group(1)] = part.strip()
    return nondets, templates


def bind(template, picks):
    """The template with each nondet name replaced by its picked value."""
    code = spec_source._code_only(template)
    out, pos = [], 0
    for m in IDENT.finditer(code):
        name = m.group(0)
        if name in picks:
            out.append(template[pos:m.start()])
            out.append(literal(picks[name]))
            pos = m.end()
    out.append(template[pos:])
    return "".join(out)


def trace_steps(trace, nondets, templates):
    """[{call, state}] for a `quint run --mbt` trace — the initial state as
    step 0 with call None — or None when a step cannot be rebuilt."""
    states = trace.get("states") or []
    if not states:
        return None
    steps = [{"call": None, "state": states[0]}]
    for st in states[1:]:
        action = st.get("mbt::actionTaken", st.get("action_taken"))
        template = templates.get(action)
        if not template:
            return None
        raw = st.get("mbt::nondetPicks", st.get("nondet_picks")) or {}
        picks = {}
        for name in nondets:
            v = raw.get(name)
            if isinstance(v, dict) and v.get("tag") == "Some":
                picks[name] = v["value"]
        try:
            call = bind(template, picks)
            literal(state_vars(st))
        except Unrenderable:
            return None
        if any(re.search(rf"\b{re.escape(n)}\b", spec_source._code_only(call))
               for n in nondets if n not in picks):
            return None
        steps.append({"call": call, "state": st})
    return steps


def replay_runs(prefix, traces, names=None):
    """Runs replaying each trace step by step, state asserted after every
    step, chained so each run is one step longer than the last:

      {prefix}_{t}_0_s = init.expect(<initial state>)
      {prefix}_{t}_k_a = {prefix}_{t}_{k-1}_s.then(<call k>)      — is it allowed?
      {prefix}_{t}_k_s = {prefix}_{t}_k_a.expect(<state k>)       — same result?

    The first failing run of a trace is where the model under test parts
    from the one the trace came from."""
    lines = []
    for t, steps in traces:
        lines.append(f"  run {prefix}_{t}_0_s = "
                     f"init.expect({state_expr(steps[0]['state'], names)})")
        for k, st in enumerate(steps[1:], 1):
            lines.append(f"  run {prefix}_{t}_{k}_a = {prefix}_{t}_{k - 1}_s.then({st['call']})")
            lines.append(f"  run {prefix}_{t}_{k}_s = {prefix}_{t}_{k}_a"
                         f".expect({state_expr(st['state'], names)})")
    return lines


def with_runs(text, lines):
    """The model text with `lines` added inside its (last) module."""
    close = text.rstrip().rfind("}")
    if close < 0:
        raise ValueError("no module to add runs to")
    return text[:close] + "\n  // -- generated by spec-questions --\n" + \
        "\n".join(lines) + "\n" + text[close:]


def first_divergence(prefix, traces, failed):
    """(trace id, step k, kind) of the earliest parting across traces, or
    None. kind: 'refused' (the call at k is not allowed), 'state' (it is,
    but the state after it differs), 'initial' (k == 0)."""
    best = None
    for t, steps in traces:
        hit = None
        if f"{prefix}_{t}_0_s" in failed:
            hit = (0, "initial")
        else:
            for k in range(1, len(steps)):
                if f"{prefix}_{t}_{k}_a" in failed:
                    hit = (k, "refused")
                    break
                if f"{prefix}_{t}_{k}_s" in failed:
                    hit = (k, "state")
                    break
        if hit and (best is None or hit[0] < best[1]):
            best = (t, hit[0], hit[1])
    return best


# ── Quint ───────────────────────────────────────────────────────────────────

TEST_LINE = re.compile(r"^\s*(?:ok|\d+\))\s+(\S+)\s+(passed|failed|ignored)\b", re.M)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_test_output(out):
    """{run name: 'passed' | 'failed' | 'ignored'} from `quint test` output."""
    return {m.group(1): m.group(2) for m in TEST_LINE.finditer(ANSI.sub("", out or ""))}


class Quint:
    def __init__(self, exe, cwd, timeout):
        self.exe, self.cwd, self.timeout = exe, cwd, timeout
        self.calls = 0
        self._help = {}

    def _run(self, args):
        self.calls += 1
        try:
            p = subprocess.run([self.exe] + args, cwd=self.cwd, capture_output=True,
                               text=True, timeout=self.timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return None, f"timed out after {self.timeout}s"

    def supports(self, sub, flag):
        if sub not in self._help:
            self._help[sub] = self._run([sub, "--help"])[1]
        return flag in self._help[sub]

    # The file goes right after the subcommand: an array option such as
    # --invariants swallows the positional arguments that follow it.

    def test(self, file, match):
        args = ["test", file, "--match", match]
        if self.supports("test", "--max-samples"):
            args.append("--max-samples=1")
        code, out = self._run(args)
        return code, out, parse_test_output(out)

    def simulate(self, file, invariants, samples, steps, seed):
        args = ["run", file, f"--max-samples={samples}", f"--max-steps={steps}",
                f"--seed={seed}"]
        for inv in invariants:
            args.append(f"--invariants={inv}")
        code, out = self._run(args)
        if code == 0:
            return "ok", out, []
        if "[violation]" in out:
            return "violation", out, sorted(i for i in invariants if i in out)
        return "error", out, []

    def traces(self, file, samples, steps, seed, outdir):
        """Random traces of `file`, with the action and nondet picks of each
        step (`--mbt`). Falls back to one sample per seed on a quint without
        --n-traces."""
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        base = ["run", file, "--mbt", f"--max-steps={steps}"]
        if self.supports("run", "--n-traces"):
            code, out = self._run(base + [f"--max-samples={samples}",
                                          f"--n-traces={samples}", f"--seed={seed}",
                                          f"--out-itf={outdir}/t_{{seq}}.itf.json"])
        else:
            for i in range(samples):
                code, out = self._run(base + ["--max-samples=1", f"--seed={seed + i}",
                                              f"--out-itf={outdir}/t_{i}.itf.json"])
        self.last_traces_output = out
        out = []
        for p in sorted(outdir.glob("*.itf.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out


# ── The run ─────────────────────────────────────────────────────────────────

def _call_dict(call):
    m = CALL_RE.match(call or "")
    if not m:
        return {"action": call, "args": "", "qualifier": None, "fails": False, "parens": False}
    name = m.group(1)
    args = (m.group(2) or "")[1:-1] if m.group(2) else ""
    return {"action": name, "args": args, "qualifier": None, "fails": False,
            "parens": bool(m.group(2))}


def _given(calls):
    return "; then ".join(spec_source.collapse_calls([_call_dict(c) for c in calls])) \
        or "the initial state"


def _when(call):
    return spec_source.render_call(_call_dict(call))


def question(kind, prefix_calls, call, model_facts, refs, area_name, key):
    """A survivor as a scenario, a yes/no question and the example that
    answers it."""
    given, chain = _given(prefix_calls), "init" + "".join(
        f".then({c})" for c in prefix_calls)
    shown_facts = " and ".join(t for _c, t in model_facts)
    cond = " and ".join(c for c, _t in model_facts)
    name = f"q_{key}"
    if kind == "initial":
        text = (f"Initially, should {shown_facts} hold? The model says so; a model "
                f"where it does not passes every example too.")
        run = f"run {name} = init.expect({cond})"
        when = "—"
    elif kind == "refused":
        when = _when(call)
        text = (f"After {given}, should {when} be refused? The model refuses it; a "
                f"model that allows it passes every example too.")
        run = f"run {name} = {chain}.then({call}.fail())"
    elif kind == "allowed":
        when = _when(call)
        text = (f"After {given}, can {when} happen? The model allows it; a model "
                f"that refuses it passes every example too.")
        run = f"run {name} = {chain}.then({call})" + (f".expect({cond})" if cond else "")
    else:
        when = _when(call)
        text = (f"After {given}, when {when}: should {shown_facts} hold? The model "
                f"says so; a model where it does not passes every example too.")
        run = f"run {name} = {chain}.then({call}).expect({cond})"
    return {"kind": kind, "given": given, "when": when,
            "model": shown_facts or None, "question": text,
            "example": run, "refs": refs}


def refs_for(area, call):
    if not call:
        return []
    action = _call_dict(call)["action"]
    out = [r["id"] for r in area.get("requirements") or []
           if r.get("quint_ref") == action and r.get("id")]
    out += [n["id"] for n in area.get("navigation") or []
            if n.get("action") == action and n.get("id")]
    return out


def run_label(name, area, journeys):
    for ex in area.get("examples") or []:
        if ex.get("quint_run") == name:
            return ex["id"]
    for j in journeys:
        if (j.get("run") or "").split(".", 1)[-1] == name:
            return f"journey: {j.get('name', name)}"
    return None


def run_area(root, area_name, args):
    area = spec_source.load_area(root, area_name)
    if area is None:
        fail_setup(f"no area '{area_name}' under {root}/specs")
    rel = (area.get("formal_model") or {}).get("quint_file") or f"{area_name}.qnt"
    model_path = spec_source.specs_dir(root) / rel
    try:
        text = model_path.read_text(encoding="utf-8")
    except OSError:
        fail_setup(f"{model_path} not found — the area has no model to mutate.")
    decls = spec_source.declarations(text)
    ir = parse_qnt(model_path, engine="regex") or {}
    imported = imported_decls(root, text)
    names = var_names(decls, imported)
    ops = tuple(o.strip() for o in (args.ops or ",".join(OPS)).split(",") if o.strip())
    sites = mutation_sites(text, decls, ir, imported, ops)
    mutants = select(sites, args.limit)
    if not mutants:
        fail_setup(f"{rel}: nothing to mutate (no actions with guards, effects or constants).")

    report = {"area": area_name, "model": rel, "candidates": len(sites),
              "planned": [dict((k, m[k]) for k in ("id", "key", "op", "decl", "line", "label"))
                          for m in mutants]}
    if args.dry_run:
        return report, None

    exe = shutil.which("quint")
    if not exe:
        fail_setup("`quint` not on PATH. Run tools/check-tooling.sh for install hints.")
    runs = sorted(n for n, h in decls.items() if h.get("kind") == "run")
    invariants = sorted(
        (r.get("host") or {}).get("name") for r in spec_source.read_records(text)
        if r["tag"] == "inv" and (r.get("host") or {}).get("kind") == "val")
    journeys = []
    jdir = spec_source.specs_dir(root) / "journeys"
    for p in sorted(jdir.glob("*.journey.json")) if jdir.exists() else []:
        try:
            journeys.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    approved = {n for n in runs if run_label(n, area, journeys)}
    match_runs = "^(" + "|".join(map(re.escape, runs)) + ")$" if runs else None

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for p in spec_source.specs_dir(root).glob("*.qnt"):
            shutil.copy(p, work / p.name)
        target = work / Path(rel).name
        q = Quint(exe, str(work), args.timeout)

        def write(t, name=None):
            path = work / (name or target.name)
            path.write_text(t, encoding="utf-8")
            return path.name

        # Baseline: the model has to pass what it is measured with.
        if match_runs:
            code, out, res = q.test(write(text), match_runs)
            if code != 0:
                fail_setup(f"{rel} fails its own runs; fix the model before mutating "
                           f"it.\n{out[-1500:]}")
            if not res:
                # Every verdict below reads this output. Unread, a killed
                # mutant would look stillborn and every replay would be empty.
                fail_setup("could not read which runs passed in `quint test`'s "
                           f"output:\n{out[-1500:]}")
        if invariants:
            res, out, _ = q.simulate(write(text), invariants, args.samples, args.steps,
                                     args.seed)
            if res != "ok":
                fail_setup(f"the simulator reports `{res}` on {rel} itself; fix that "
                           f"first.\n{out[-1500:]}")

        nondets, templates = step_templates(decls)

        def suite(prefix, source_text, seed):
            """Random traces of `source_text`, rebuilt as calls and states."""
            raw = q.traces(write(source_text, f"_src_{prefix}.qnt"), args.traces,
                           args.steps, seed, work / f"itf_{prefix}")
            shutil.rmtree(work / f"itf_{prefix}", ignore_errors=True)
            out = []
            for i, tr in enumerate(raw):
                steps = trace_steps(tr, nondets, templates)
                if steps:
                    out.append((i, steps))
            if prefix == "qr":
                report["traces_read"], report["traces_rebuilt"] = len(raw), len(out)
                if not out:
                    # Said, not swallowed: with no replay every survivor reads
                    # as "no difference found".
                    why = (getattr(q, "last_traces_output", "") or "")[-800:]
                    print(f"WARNING: no replayable trace of the model ({len(raw)} read)."
                          f"\n{why}", file=sys.stderr)
            return out

        # The model's own traces, replayed on the model itself: a run that
        # fails here is a rendering problem, not a difference, and its trace
        # is dropped before any mutant sees it.
        base = suite("qr", text, args.seed)
        if base:
            code, out, res = q.test(write(with_runs(text, replay_runs("qr", base, names))),
                                    "^qr_")
            bad = {n for n, s in res.items() if s != "passed"}
            if bad:
                print(f"WARNING: {len(bad)} replay run(s) fail on the model itself and "
                      f"their traces are dropped, e.g. {sorted(bad)[:3]}:\n{out[-800:]}",
                      file=sys.stderr)
            if not res and code != 0:
                print(f"WARNING: the model's own replay did not run:\n{out[:1500]}",
                      file=sys.stderr)
                base = []
            else:
                base = [(t, st) for t, st in base
                        if not any(n.startswith(f"qr_{t}_") for n in bad)]
        report["replay_traces"] = len(base)

        results, questions = [], []
        for m in mutants:
            mt = apply(text, m)
            row = {"id": m["id"], "key": m["key"], "op": m["op"], "decl": m["decl"],
                   "line": m["line"], "label": m["label"]}
            results.append(row)
            if match_runs:
                code, out, res = q.test(write(mt), match_runs)
                if code != 0 and not res:
                    row["outcome"] = "stillborn"
                    row["detail"] = " ".join(out.strip().splitlines()[-3:])[:300]
                    continue
                failed = sorted(n for n, s in res.items() if s == "failed")
                if code != 0 and failed:
                    row["outcome"] = "killed"
                    row["killed_by"] = [run_label(n, area, journeys) or f"run {n}"
                                        for n in failed]
                    row["by_example"] = any(n in approved for n in failed)
                    continue
            if invariants:
                res, out, falsified = q.simulate(write(mt), invariants, args.samples,
                                                 args.steps, args.seed)
                if res == "violation":
                    row["outcome"] = "killed"
                    row["killed_by"] = falsified or ["an invariant"]
                    row["by_example"] = False
                    continue
                if res == "error":
                    row["outcome"] = "stillborn"
                    row["detail"] = " ".join(out.strip().splitlines()[-3:])[:300]
                    continue

            found = None
            if base:
                code, out, res = q.test(write(with_runs(mt, replay_runs("qr", base, names))),
                                        "^qr_")
                failed = {n for n, s in res.items() if s == "failed"}
                hit = first_divergence("qr", base, failed)
                if hit:
                    t, k, kind = hit
                    steps = dict(base)[t]
                    prefix_calls = [s["call"] for s in steps[1:k]]
                    call = steps[k]["call"] if k else None
                    if kind == "refused":
                        found = ("allowed", prefix_calls, call,
                                 changed_facts(steps[k - 1]["state"], steps[k]["state"],
                                               names))
                    else:
                        # Which entries differ: one run per fact at that step.
                        cands = facts(steps[k]["state"], names)
                        head = f"qr_{t}_{k - 1}_s.then({call})" if k else "init"
                        lines = [f"  run qd_{i} = {head}.expect({c})"
                                 for i, (c, _t) in enumerate(cands)]
                        lines = replay_runs("qr", [(t, steps[:max(k, 1)])], names) + lines
                        _c, _o, dres = q.test(write(with_runs(mt, lines)), "^qd_")
                        diff = [cands[i] for i in range(len(cands))
                                if dres.get(f"qd_{i}") == "failed"]
                        found = ("initial" if k == 0 else "outcome", prefix_calls, call,
                                 diff or cands)
            if not found:
                # The other way round: what the mutant allows and the model
                # refuses never shows up in the model's own traces.
                other = suite("qb", mt, args.seed + 1)
                if other:
                    code, out, res = q.test(write(with_runs(text, replay_runs("qb", other,
                                                                               names))),
                                            "^qb_")
                    failed = {n for n, s in res.items() if s == "failed"}
                    hit = first_divergence("qb", other, failed)
                    if hit and hit[2] == "refused":
                        t, k, _ = hit
                        steps = dict(other)[t]
                        found = ("refused", [s["call"] for s in steps[1:k]],
                                 steps[k]["call"], [])
            if not found:
                row["outcome"] = "no-difference"
                continue
            kind, prefix_calls, call, fs = found
            row["outcome"] = "survived"
            qn = question(kind, prefix_calls, call, fs, refs_for(area, call), area_name,
                          m["key"])
            qn["mutants"] = [m["key"]]
            qn["change"] = f"{m['decl']}: {m['label']}"
            dup = next((x for x in questions if (x["kind"], x["given"], x["when"], x["model"])
                        == (qn["kind"], qn["given"], qn["when"], qn["model"])), None)
            if dup:
                dup["mutants"].append(m["key"])
                row["question"] = dup["key"]
            else:
                qn["key"] = m["key"]
                questions.append(qn)
                row["question"] = m["key"]
        report["quint_calls"] = q.calls

    counts = {o: sum(1 for r in results if r.get("outcome") == o)
              for o in ("stillborn", "killed", "survived", "no-difference")}
    by_example = sum(1 for r in results if r.get("outcome") == "killed" and r.get("by_example"))
    changing = counts["killed"] + counts["survived"]
    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_sha": compute_model_sha(root, area_name, area),
        "seed": args.seed, "max_steps": args.steps, "traces": args.traces,
        "mutants": len(results), "stillborn": counts["stillborn"],
        "killed_by_examples": by_example,
        "killed_otherwise": counts["killed"] - by_example,
        "survived": counts["survived"], "no_difference": counts["no-difference"],
        "score": round(by_example / changing, 3) if changing else None,
        "questions": questions,
        "results": results,
    }
    report.update(summary)
    return report, summary


def print_report(report):
    planned = report["planned"]
    print(f"spec-questions {report['area']} — {report['model']}: {len(planned)} of "
          f"{report['candidates']} candidate mutant(s)")
    if "results" not in report:
        for m in planned:
            print(f"  {m['id']} {m['op']:<8} {m['decl']}:{m['line']}  {m['label']}")
        return
    score = report.get("score")
    print(f"score {'n/a' if score is None else f'{score:.0%}'} · killed by examples "
          f"{report['killed_by_examples']} · otherwise {report['killed_otherwise']} · "
          f"survived {report['survived']} · no difference {report['no_difference']} · "
          f"stillborn {report['stillborn']} · {report.get('replay_traces', 0)} replay "
          f"trace(s) · {report.get('quint_calls', 0)} quint call(s)")
    for r in report["results"]:
        what = r.get("outcome", "?")
        if what == "killed":
            by = r.get("killed_by") or []
            what = "killed: " + ", ".join(by[:2]) + (f" +{len(by) - 2}" if len(by) > 2 else "")
        elif what == "survived":
            what = f"question {r.get('question')}"
        elif what == "stillborn" and r.get("detail"):
            what += f" ({r['detail'][:80]})"
        print(f"  {r['id']} {r['op']:<7} {r['decl']}:{r['line']}  {r['label']}  — {what}")
    for qn in report.get("questions") or []:
        print(f"\nQ {qn['key']} ({qn['kind']}; {', '.join(qn['refs']) or 'no requirement'})\n"
              f"  {qn['question']}\n  change: {qn['change']}\n  yes -> {qn['example']}")


def main(argv=None):
    soften_stdout()
    ap = argparse.ArgumentParser(description="Model mutation: what the examples leave open.")
    ap.add_argument("area")
    ap.add_argument("--root", default=".")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--ops", help=f"comma-separated subset of {','.join(OPS)}")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--steps", type=int, default=10,
                    help="step bound for replay traces and the invariant simulation")
    ap.add_argument("--traces", type=int, default=30,
                    help="random traces replayed on each survivor")
    ap.add_argument("--samples", type=int, default=2000,
                    help="simulator samples for the invariant check")
    ap.add_argument("--timeout", type=int, default=180, help="per quint call, seconds")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", action="store_true",
                    help="write check_results.mutation into the area's records")
    args = ap.parse_args(argv)
    root = Path(args.root)
    report, summary = run_area(root, args.area, args)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    if args.record and summary is not None:
        area = spec_source.load_area(root, args.area)
        area.setdefault("check_results", {})["mutation"] = summary
        spec_source.save_area(root, args.area, area)
        print(f"\nrecorded check_results.mutation for {args.area}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
