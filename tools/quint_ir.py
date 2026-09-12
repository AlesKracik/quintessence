#!/usr/bin/env python3
"""
quint_ir.py — Structured access to Quint sidecar files.

Single entry point for every tool that needs to know what's inside a .qnt
file (spec-lint, spec-matrix, /spec-check probe generation, readback).
Replaces ad-hoc regex scraping with the Quint compiler's own typed JSON IR
when the `quint` CLI is available, falling back to the legacy regex parser
when it isn't (so pre-commit hooks keep working on machines without Node).

Normalized output (same shape from both engines):

  {
    "source":      "quint-cli" | "regex",
    "file":        "<path>",
    "module_name": "auth",
    "imports":     [{"module": "billing", "from": "./billing"}, ...],
    "types":       ["SessionStatus", ...],
    "type_variants": {"SessionStatus": ["Active", "Expired", ...], ...},
    "consts":      ["MAX_FAILED_ATTEMPTS", ...],
    "vars":        ["sessions", ...],
    "actions":     ["login", ...],
    "vals":        ["atMostOneActiveSession", ...],   # top-level val/invariant
    "temporals":   ["eventualLogout", ...],
    "runs":        ["happyPath", ...],
    "action_mutations": {"login": ["sessions", ...], ...},   # x' = <new>
    "action_preserves": {"login": ["accountStatus", ...], ...},  # x' = x
    "action_produces":  {"login": ["Active"], ...},   # variants this action builds
    "action_param_types": {"login": [("uid", "UserId"), ...], ...},  # as written
    "var_type_strs":    {"sessions": "SessionId -> SessionStatus", ...},
    "type_aliases":     {"UserId": "str", ...},       # plain aliases only
    "action_reads":     {"login": ["accountStatus", ...], ...},  # transitive
    "var_types":        {"accountStatus": ["UserId", "AccountStatus"], ...},
    "produced_variants": ["Active", "Locked", ...],  # variants an assign builds
    "action_params":    {"login": ["uid", "sid"], ...},
    "action_calls":     {"step": ["login", "logout"], ...}  # action -> actions
                                                            # it calls
  }

Usage (CLI):
  tools/quint_ir.py specs/auth.qnt                # human-readable summary
  tools/quint_ir.py specs/auth.qnt --json         # normalized JSON
  tools/quint_ir.py specs/auth.qnt --engine regex # force the fallback parser

Usage (import):
  sys.path.insert(0, str(Path(__file__).parent))
  from quint_ir import parse_qnt
  ir = parse_qnt(Path("specs/auth.qnt"))
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Engine selection: 'auto' (CLI then regex), 'cli' (authoritative — fail if
# the quint CLI can't parse), 'regex' (offline fallback only). CI should set
# QUINT_IR_ENGINE=cli so lint verdicts never depend on a lossy regex parse;
# local pre-commit hooks may stay on auto.
DEFAULT_ENGINE = os.environ.get("QUINT_IR_ENGINE", "auto")

# ── Engine 1: Quint CLI typed IR ──────────────────────────────────────────────

QUINT_TIMEOUT_S = 60


def _quint_bin():
    """Locate the quint CLI (handles npm .cmd shims on Windows)."""
    for name in ("quint", "quint.cmd"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _walk_expr(node, visit):
    """Depth-first walk over a Quint IR expression tree."""
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk_expr(v, visit)
    elif isinstance(node, list):
        for v in node:
            _walk_expr(v, visit)


def _collect_mutations(expr):
    """Vars assigned (`x' = e`) inside an expression. Skips identity
    assignments (`x' = x`) — Quint requires every var assigned in every
    action; identity is the explicit 'no change' idiom, not a mutation."""
    mutated = []

    def visit(node):
        if node.get("kind") == "app" and node.get("opcode") == "assign":
            args = node.get("args") or []
            if args and isinstance(args[0], dict) and args[0].get("kind") == "name":
                name = args[0].get("name")
                rhs = args[1] if len(args) > 1 else None
                if (
                    isinstance(rhs, dict)
                    and rhs.get("kind") == "name"
                    and rhs.get("name") == name
                ):
                    return  # identity
                if name and name not in mutated:
                    mutated.append(name)

    _walk_expr(expr, visit)
    return mutated


def _collect_preserved(expr):
    """Vars EXPLICITLY held constant (`x' = x`) inside an expression.

    The mirror of _collect_mutations, which skips exactly these. Recorded
    rather than discarded because a requirement whose response is a
    preservation ("shall LEAVE the subscription Active") is implemented by
    an identity assignment: without this, that correct model looks like an
    action that ignores the variable its sentence is about."""
    preserved = []

    def visit(node):
        if node.get("kind") == "app" and node.get("opcode") == "assign":
            args = node.get("args") or []
            lhs = args[0] if args else None
            rhs = args[1] if len(args) > 1 else None
            if not (isinstance(lhs, dict) and lhs.get("kind") == "name"):
                return
            name = lhs.get("name")
            if (isinstance(rhs, dict) and rhs.get("kind") == "name"
                    and name and rhs.get("name") == name
                    and name not in preserved):
                preserved.append(name)

    _walk_expr(expr, visit)
    return preserved


def _collect_reads(expr):
    """Var names READ inside an expression, in first-seen order.

    Two exclusions make the answer mean something:
      - the LHS of an assignment (`x' = ...`) is the target, not a read;
      - the RHS of an IDENTITY assignment (`x' = x`) is the explicit
        no-change idiom. Quint requires every var to be assigned in every
        action, so counting those would make almost every action 'read'
        almost every var, and any check built on this would be vacuous.

    Over-collects otherwise (params, vals and operator names land here too);
    the caller narrows to declared vars."""
    names = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "app" and node.get("opcode") == "assign":
                args = node.get("args") or []
                lhs = args[0] if args else None
                rhs = args[1] if len(args) > 1 else None
                lhs_name = (lhs.get("name")
                            if isinstance(lhs, dict) and lhs.get("kind") == "name"
                            else None)
                if (isinstance(rhs, dict) and rhs.get("kind") == "name"
                        and lhs_name and rhs.get("name") == lhs_name):
                    return
                walk(rhs)  # the LHS is deliberately not walked
                return
            if node.get("kind") == "name" and node.get("name"):
                names.append(node["name"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(expr)
    return names


def _collect_assign_rhs(expr):
    """Every name appearing on the RIGHT of an assignment. A variant
    constructor found here is one the model can actually PRODUCE, as opposed
    to one it only ever compares against."""
    names = []

    def walk(node, in_rhs=False):
        if isinstance(node, dict):
            if node.get("kind") == "app" and node.get("opcode") == "assign":
                args = node.get("args") or []
                if len(args) > 1:
                    walk(args[1], True)
                return
            if in_rhs and node.get("kind") == "name" and node.get("name"):
                names.append(node["name"])
            if in_rhs and node.get("opcode"):
                names.append(node["opcode"])
            for v in node.values():
                walk(v, in_rhs)
        elif isinstance(node, list):
            for v in node:
                walk(v, in_rhs)

    walk(expr)
    return names


def _type_names(type_expr):
    """Type names mentioned anywhere in a type annotation. `SessionId ->
    SessionStatus` yields both, which is what lets a declared state name be
    traced back to the var that holds it."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str):
                found.append(name)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(type_expr)
    return found


def _resolve_reads(raw_reads, raw_calls, var_set, def_names):
    """Reads, closed transitively over the module's own defs.

    An action that gates through a helper (`not(isLocked(uid))`) never names
    the var itself — the helper does. Without the closure, every guard
    written through a helper would look like no guard at all, which is the
    idiomatic way to write these models and would make the check worse than
    useless."""
    direct = {name: [n for n in names if n in var_set]
              for name, names in raw_reads.items()}
    callees = {name: [n for n in names if n in def_names and n != name]
               for name, names in raw_calls.items()}
    resolved = {name: list(vals) for name, vals in direct.items()}
    changed = True
    while changed:
        changed = False
        for name in resolved:
            for callee in callees.get(name) or []:
                for v in resolved.get(callee) or []:
                    if v not in resolved[name]:
                        resolved[name].append(v)
                        changed = True
    return resolved


def _collect_calls(expr):
    """Every name an expression mentions — an application's callee opcode and
    every bare name reference. Deliberately over-collects: vars, vals and
    operators land here too, and the caller narrows to declared actions. That
    narrowing is what makes the two engines agree, since the regex fallback
    cannot tell a callee from any other identifier."""
    names = []

    def visit(node):
        op = node.get("opcode")
        if op:
            names.append(op)
        if node.get("kind") == "name" and node.get("name"):
            names.append(node["name"])

    _walk_expr(expr, visit)
    return names


def _narrow_to_actions(raw_calls, actions):
    """Keep only references to declared actions, drop self-reference, preserve
    first-seen order. Applied after the whole module is read, so an action may
    call one declared further down the file."""
    known = set(actions)
    out = {}
    for name, raw in raw_calls.items():
        seen, kept = set(), []
        for n in raw:
            if n in known and n != name and n not in seen:
                seen.add(n)
                kept.append(n)
        out[name] = kept
    return out


def _sum_variants(type_node):
    """Constructor labels of a Quint sum type, or [] for any other type.

    Shape-tolerant on purpose: the compiler's IR spells a variant row as
    {kind: "sum", fields: {kind: "row", fields: [{fieldName: "Active", ...}]}},
    but the nesting has moved between Quint versions and a KeyError here
    would take down every lint run. Anything unrecognized yields [], which
    the caller treats as 'no constructor list available' rather than as
    'the type has no constructors' \u2014 an unchecked CLOSED entity is reported
    as unverifiable, never as verified."""
    if not isinstance(type_node, dict):
        return []
    if type_node.get("kind") != "sum":
        return []
    row = type_node.get("fields")
    if isinstance(row, dict):
        row = row.get("fields")
    if not isinstance(row, list):
        return []
    names = []
    for f in row:
        if isinstance(f, dict):
            label = f.get("fieldName") or f.get("name")
            if label:
                names.append(label)
    return names


def _lambda_params(expr):
    """Parameter names of an action, from the lambda its definition wraps.

    Needed because a witness predicate has to be BOUND: `_lastAction == login`
    pins which action ran last, not that this call caused the postcondition.
    A bare existential is satisfied by a session some unrelated call created,
    so the requirement goes green while its own action misbehaves. Checking
    the binding means knowing the parameter names."""
    if not isinstance(expr, dict):
        return []
    if expr.get("kind") != "lambda":
        # Some IR versions wrap the lambda one level down (e.g. in an opdef
        # body). Look one level rather than guessing at the whole shape.
        for key in ("expr", "body"):
            inner = expr.get(key)
            if isinstance(inner, dict) and inner.get("kind") == "lambda":
                expr = inner
                break
        else:
            return []
    names = []
    for param in expr.get("params") or []:
        if isinstance(param, dict) and param.get("name"):
            names.append(param["name"])
        elif isinstance(param, str):
            names.append(param)
    return names


def _norm_name(s):
    """Normalization for stem↔module matching: lowercase, alphanumerics only.
    Makes 'auth.probes' (file stem) match 'auth_probes' (module name) — the
    underscore/dot mismatch previously sent every probes file to the
    last-module heuristic."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _pick_main(named_modules, qnt_path):
    """Shared main-module selection for BOTH engines: name matches file stem
    (normalized), else last module with state vars, else last module.
    named_modules: [(name, has_vars, payload)]."""
    stem = _norm_name(qnt_path.stem)
    main = None
    for name, _has_vars, payload in named_modules:
        if _norm_name(name) == stem:
            main = payload
    if main is None:
        with_vars = [p for _n, hv, p in named_modules if hv]
        main = (with_vars or [p for _n, _hv, p in named_modules])[-1]
    return main


def _normalize_ir(ir_json, qnt_path):
    """Map the Quint compiler's IR JSON to the normalized shape."""
    modules = ir_json.get("modules") or []
    if not modules:
        return None

    named = [
        (m.get("name", ""),
         any(d.get("kind") == "var" for d in m.get("declarations") or []),
         m)
        for m in modules
    ]
    main = _pick_main(named, Path(qnt_path))

    out = {
        "source": "quint-cli",
        "file": str(qnt_path),
        "module_name": main.get("name"),
        "imports": [],
        "types": [],
        "type_variants": {},
        "consts": [],
        "vars": [],
        "actions": [],
        "vals": [],
        "temporals": [],
        "runs": [],
        "action_mutations": {},
        "action_params": {},
        "action_calls": {},
        "var_types": {},
        "action_reads": {},
        "action_preserves": {},
        "action_produces": {},
        "produced_variants": [],
    }
    raw_calls = {}
    # Reads and calls for EVERY def, not just actions: the guard an action
    # states through a helper is only visible once the helper is resolved.
    all_reads, all_calls = {}, {}
    assign_rhs = []

    for d in main.get("declarations") or []:
        kind = d.get("kind")
        name = d.get("name")
        if kind == "import":
            entry = {"module": d.get("protoName") or d.get("name")}
            if d.get("fromSource"):
                entry["from"] = d["fromSource"]
            if entry["module"]:
                out["imports"].append(entry)
        elif kind == "typedef":
            out["types"].append(name)
            variants = _sum_variants(d.get("type"))
            if variants:
                out["type_variants"][name] = variants
        elif kind == "const":
            out["consts"].append(name)
        elif kind == "var":
            out["vars"].append(name)
            out["var_types"][name] = _type_names(d.get("type"))
        elif kind == "def":
            q = d.get("qualifier")
            all_reads[name] = _collect_reads(d.get("expr"))
            all_calls[name] = _collect_calls(d.get("expr"))
            assign_rhs.extend(_collect_assign_rhs(d.get("expr")))
            if q == "action":
                out["actions"].append(name)
                out["action_mutations"][name] = _collect_mutations(d.get("expr"))
                out["action_preserves"][name] = _collect_preserved(d.get("expr"))
                out["action_produces"][name] = _collect_assign_rhs(d.get("expr"))
                out["action_params"][name] = _lambda_params(d.get("expr"))
                raw_calls[name] = _collect_calls(d.get("expr"))
            elif q == "run":
                out["runs"].append(name)
            elif q == "temporal":
                out["temporals"].append(name)
            elif q in ("val", "pureval"):
                out["vals"].append(name)
            # def/puredef/nondet: helpers, not surfaced
    out["action_calls"] = _narrow_to_actions(raw_calls, out["actions"])
    resolved = _resolve_reads(all_reads, all_calls, set(out["vars"]),
                              set(all_reads))
    out["action_reads"] = {a: resolved.get(a, []) for a in out["actions"]}
    variants = {v for vs in out["type_variants"].values() for v in vs}
    out["produced_variants"] = [v for v in sorted(variants) if v in set(assign_rhs)]
    module_text = _module_text(qnt_path)
    param_types, var_type_strs = _scan_surface_types(module_text)
    out["action_param_types"] = param_types
    out["var_type_strs"] = var_type_strs
    out["type_aliases"] = _scan_type_aliases(module_text)
    out["action_produces"] = {
        a: sorted(variants & set(names))
        for a, names in out["action_produces"].items()
    }
    return out


def cli_available():
    """True when the quint CLI can actually be located.

    Exposed because "the CLI is missing" and "this file does not parse" are
    different facts that _parse_via_cli() flattens into the same None. Under
    engine='cli' the first one silently turns every sidecar into "no module",
    and every check that reads a sidecar then passes on nothing — a whole
    class of verdicts reported clean without being computed. Callers that
    demand the authoritative engine ask this first and say so out loud.
    """
    return _quint_bin() is not None


def _parse_via_cli(qnt_path):
    quint = _quint_bin()
    if not quint:
        return None
    tmp = None
    try:
        fd = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp = Path(fd.name)
        fd.close()
        result = subprocess.run(
            [quint, "parse", f"--out={tmp}", str(qnt_path)],
            capture_output=True, text=True, timeout=QUINT_TIMEOUT_S,
        )
        if result.returncode != 0 or not tmp.exists() or not tmp.stat().st_size:
            return None
        ir_json = json.loads(tmp.read_text(encoding="utf-8"))
        return _normalize_ir(ir_json, Path(qnt_path))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError):
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


# ── Engine 2: regex fallback (offline use when the quint CLI is absent) ───────

MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
IMPORT_RE = re.compile(
    r"^\s*import\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\.\*)?\s*(?:from\s+\"([^\"]+)\")?",
    re.MULTILINE,
)
ACTION_RE   = re.compile(r"^\s*action\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:\(=]")
VAR_RE      = re.compile(r"^\s*var\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", re.MULTILINE)
VAR_TYPE_RE = re.compile(
    r"^\s*var\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$", re.MULTILINE)
CONST_RE    = re.compile(r"^\s*const\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", re.MULTILINE)
TYPE_RE     = re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
# `type Status = Active | Locked(int) | Expired`, possibly wrapped across
# lines. Stops at the next top-level declaration keyword or a blank line.
TYPE_BODY_RE = re.compile(
    r"^[ \t]*type[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(?P<body>[^\n]*(?:\n[ \t]*\|[^\n]*)*)",
    re.MULTILINE)
VARIANT_RE  = re.compile(r"^[ \t]*([A-Z][A-Za-z0-9_]*)")
VAL_RE      = re.compile(r"^\s*(?:val|invariant)\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", re.MULTILINE)
TEMPORAL_RE = re.compile(r"^\s*temporal\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", re.MULTILINE)
RUN_RE      = re.compile(r"^\s*run\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
MUTATION_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)'\s*=")
LOCAL_VAL_RE = re.compile(r"^\s*val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")
IDENT_RE    = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
# Named literal constants: `pure val N: int = 5` / `const N: int = 5`.
# Used by spec-lint to cross-check constraints[].value against the model.
CONST_VALUE_RE = re.compile(
    r"^\s*(?:pure\s+val|const)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?="
    r"\s*(-?\d+|\"[^\"]*\"|true|false)\s*(?://.*)?$",
    re.MULTILINE,
)


def _scan_const_values(text):
    """{name: python-value} for top-level literal constants. Text-based on
    purpose — works identically under both engines."""
    out = {}
    for name, raw in CONST_VALUE_RE.findall(text):
        if raw in ("true", "false"):
            out[name] = raw == "true"
        elif raw.startswith('"'):
            out[name] = raw[1:-1]
        else:
            out[name] = int(raw)
    return out


ACTION_SIG_RE = re.compile(
    r"^[ \t]*action[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(([^)]*)\)",
    re.MULTILINE)


def _scan_action_params(text):
    """Regex twin of _lambda_params: `action login(uid: UserId, sid: SessionId)`
    -> {"login": ["uid", "sid"]}. Parameterless actions are simply absent."""
    out = {}
    for m in ACTION_SIG_RE.finditer(text):
        names = []
        for part in m.group(2).split(","):
            part = part.strip()
            if not part:
                continue
            names.append(part.split(":")[0].strip())
        if names:
            out[m.group(1)] = names
    return out


def _scan_type_variants(text):
    """Regex-engine twin of _sum_variants. Only sum types (a body containing
    `|`) yield entries; aliases like `type UserId = str` are not variant
    types and must not be reported as a one-constructor closed set."""
    out = {}
    for m in TYPE_BODY_RE.finditer(text):
        body = m.group("body")
        if "|" not in body:
            continue
        names = []
        for alt in body.split("|"):
            hit = VARIANT_RE.match(alt.strip("\r\n").lstrip())
            if hit:
                names.append(hit.group(1))
        if names:
            out[m.group(1)] = names
    return out


def _strip_noise(text):
    """LENGTH-PRESERVING blanking of // line comments, /* */ block comments,
    and string-literal contents (quotes kept, body spaced; newlines kept).
    Brace counting and declaration regexes must run on this — a brace or
    '//' inside a string or comment otherwise corrupts module spans and
    action-body attribution. Because offsets are preserved, spans computed
    on the cleaned text can slice the RAW text when literal content (e.g.
    import paths) is needed."""
    out = list(text)
    i, n = 0, len(text)
    mode = 0  # 0 normal, 1 line comment, 2 block comment, 3 string

    def blank(idx):
        if out[idx] != "\n":
            out[idx] = " "

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if mode == 0:
            if c == "/" and nxt == "/":
                mode = 1
                blank(i)
                blank(i + 1)
                i += 2
                continue
            if c == "/" and nxt == "*":
                mode = 2
                blank(i)
                blank(i + 1)
                i += 2
                continue
            if c == '"':
                mode = 3
            i += 1
        elif mode == 1:
            if c == "\n":
                mode = 0
            else:
                blank(i)
            i += 1
        elif mode == 2:
            if c == "*" and nxt == "/":
                mode = 0
                blank(i)
                blank(i + 1)
                i += 2
                continue
            blank(i)
            i += 1
        else:  # string literal
            if c == "\\" and i + 1 < n:
                blank(i)
                blank(i + 1)
                i += 2
                continue
            if c == '"':
                mode = 0
            else:
                blank(i)
            i += 1
    return "".join(out)


def _module_spans(clean_text):
    """[(name, start, end)] for each top-level module, brace-matched over
    comment/string-stripped text."""
    spans = []
    for m in MODULE_RE.finditer(clean_text):
        depth = 0
        start = clean_text.index("{", m.start())
        i = start
        while i < len(clean_text):
            if clean_text[i] == "{":
                depth += 1
            elif clean_text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        spans.append((m.group(1), m.start(), i + 1))
    return spans


def _parse_action_bodies(text):
    """Brace-depth-tracked single pass over ALREADY-STRIPPED text.
    Returns (mutations, locals, raw_calls).

    raw_calls is every identifier appearing in each action's body — the
    fallback cannot distinguish a callee from a var, a type or an operator,
    so it over-collects exactly like the IR collector does and lets
    _narrow_to_actions() decide. Same input, same narrowed answer, whichever
    engine ran."""
    mutations = {}
    preserved = {}
    produces = {}
    locals_ = set()
    raw_calls = {}
    current = None
    action_depth = 0
    depth = 0
    for line in text.splitlines():
        depth_before = depth

        action_match = ACTION_RE.match(line)
        if action_match:
            current = action_match.group(1)
            mutations.setdefault(current, [])
            preserved.setdefault(current, [])
            produces.setdefault(current, [])
            raw_calls.setdefault(current, [])
            action_depth = depth_before

        if current is not None and (depth_before > action_depth or action_match):
            for m in MUTATION_RE.finditer(line):
                name = m.group(1)
                rest_match = re.search(rf"\b{re.escape(name)}'\s*=\s*(.*)", line)
                if rest_match:
                    rest = rest_match.group(1).strip().rstrip(",").rstrip(";").strip()
                    produces[current].extend(IDENT_RE.findall(rest))
                    if rest == name:
                        if name not in preserved[current]:
                            preserved[current].append(name)
                        continue
                if name not in mutations[current]:
                    mutations[current].append(name)
            lv = LOCAL_VAL_RE.match(line)
            if lv:
                locals_.add(lv.group(1))
            raw_calls[current].extend(IDENT_RE.findall(line))

        depth += line.count("{") - line.count("}")
        if current is not None and depth <= action_depth:
            current = None
            action_depth = 0
    return mutations, preserved, produces, locals_, raw_calls


def _scan_var_types(text):
    """var -> type names in its annotation. `SessionId -> SessionStatus`
    yields both. As reliable here as in the CLI engine: a var declaration is
    one line with its type on the right of the colon."""
    out = {}
    for name, annotation in VAR_TYPE_RE.findall(text):
        out[name] = IDENT_RE.findall(annotation)
    return out


def _scan_produced_variants(text, variants):
    """Variant constructors appearing on the RIGHT of an assignment — the
    ones the model can produce, as opposed to the ones it only compares
    against. Scans the whole module, helpers included, so a state built
    inside a helper still counts."""
    produced = set()
    for line in text.splitlines():
        for m in MUTATION_RE.finditer(line):
            rhs = line[m.end():]
            produced.update(v for v in IDENT_RE.findall(rhs) if v in variants)
    return sorted(produced)


# Written as source text because that is what consumes them: the probe
# generator emits Quint, and it needs the annotation the author wrote
# ("SessionId -> SessionStatus"), not a type tree it would have to
# pretty-print back. Both engines fill these from the module text for the
# same reason — the CLI's typed IR is the better answer to every OTHER
# question, and the wrong shape for this one.
ACTION_SIG_RE = re.compile(
    r"^\s*action\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", re.MULTILINE)


def _split_params(text):
    """`uid: UserId, sid: SessionId` -> [("uid", "UserId"), ...]. Splits on
    top-level commas only, so `m: SessionId -> SessionStatus` and
    `s: Set[(int, int)]` survive intact."""
    out, depth, current = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        out.append(current)
    params = []
    for chunk in out:
        name, sep, type_text = chunk.partition(":")
        if sep and name.strip():
            params.append((name.strip(), type_text.strip()))
    return params


TYPE_ALIAS_RE = re.compile(
    r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\n|]+)$", re.MULTILINE)


def _scan_type_aliases(text):
    """`type UserId = str` -> {"UserId": "str"}.

    Plain aliases only. A sum type is written with `|` alternatives, and its
    variants are already reported under type_variants; matching one here
    would claim `type S = | A | B` aliases the empty string. The probe
    generator follows these to work out a ghost's initial value \u2014 a
    `_lastUid: UserId` starts at `""` only because UserId resolves to str."""
    out = {}
    for name, rhs in TYPE_ALIAS_RE.findall(text):
        rhs = rhs.split("//")[0].strip()
        if rhs and "|" not in rhs:
            out[name] = rhs
    return out


def _scan_surface_types(text):
    """(action_param_types, var_type_strs) as the author wrote them."""
    param_types = {}
    for name, raw in ACTION_SIG_RE.findall(text):
        param_types[name] = _split_params(raw)
    var_types = {}
    for name, annotation in VAR_TYPE_RE.findall(text):
        # Strip a trailing line comment; `var x: int  // REQ-001` is common.
        var_types[name] = annotation.split("//")[0].strip().rstrip(",").strip()
    return param_types, var_types


def _module_text(qnt_path):
    """The selected main module's text, or "" when the file does not parse
    as one. Shared so the CLI engine reads exactly the module the typed IR
    describes, not the whole file."""
    try:
        raw = Path(qnt_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    clean = _strip_noise(raw)
    spans = _module_spans(clean)
    if not spans:
        return ""
    named = [(name, bool(VAR_RE.search(clean[start:end])), (name, start, end))
             for name, start, end in spans]
    _mod, start, end = _pick_main(named, Path(qnt_path))
    return clean[start:end]


def _parse_via_regex(qnt_path):
    raw = Path(qnt_path).read_text(encoding="utf-8")
    clean = _strip_noise(raw)
    spans = _module_spans(clean)
    if not spans:
        return None
    # Same main-module selection as the CLI engine — verdicts must not
    # depend on which engine parsed the file.
    named = [
        (name, bool(VAR_RE.search(clean[start:end])), (name, start, end))
        for name, start, end in spans
    ]
    mod_name, start, end = _pick_main(named, Path(qnt_path))
    text = clean[start:end]  # scope EVERY scan to the selected module

    (mutations, preserved, produces, action_locals,
     raw_calls) = _parse_action_bodies(text)
    var_types = _scan_var_types(text)
    surface_params, surface_var_types = _scan_surface_types(text)
    var_set = set(VAR_RE.findall(text))
    type_variants = _scan_type_variants(text)
    all_variants = {v for vs in type_variants.values() for v in vs}
    actions = []
    for line in text.splitlines():
        am = ACTION_RE.match(line)
        if am and am.group(1) not in actions:
            actions.append(am.group(1))
    vals = [n for n in VAL_RE.findall(text) if n not in action_locals]
    imports = []
    # Import paths live inside string literals, which the cleaned text
    # blanks — _strip_noise is length-preserving, so slice the RAW text
    # at the same offsets for this one scan.
    for im in IMPORT_RE.finditer(raw[start:end]):
        entry = {"module": im.group(1)}
        if im.group(2):
            entry["from"] = im.group(2)
        imports.append(entry)
    return {
        "source": "regex",
        "file": str(qnt_path),
        "module_name": mod_name,
        "imports": imports,
        "types": TYPE_RE.findall(text),
        "type_variants": type_variants,
        "consts": CONST_RE.findall(text),
        "vars": VAR_RE.findall(text),
        "actions": actions,
        "vals": vals,
        "temporals": TEMPORAL_RE.findall(text),
        "runs": RUN_RE.findall(text),
        "action_mutations": mutations,
        "action_preserves": preserved,
        "action_produces": {
            a: sorted(set(names) & all_variants)
            for a, names in produces.items()
        },
        "action_params": _scan_action_params(text),
        "action_calls": _narrow_to_actions(raw_calls, actions),
        "var_types": var_types,
        "action_param_types": surface_params,
        "var_type_strs": surface_var_types,
        "type_aliases": _scan_type_aliases(text),
        # PARTIAL under this engine, and callers must treat it as such: the
        # fallback scans action bodies only, so a var a guard reaches through
        # a helper (`not(isLocked(uid))`) is absent here while the CLI engine
        # resolves it. A check that FAILs on a missing read must therefore
        # require source == "quint-cli", or it would fail correct models.
        "action_reads": {
            a: [n for n in dict.fromkeys(raw_calls.get(a) or []) if n in var_set]
            for a in actions
        },
        "produced_variants": _scan_produced_variants(
            text, {v for vs in type_variants.values() for v in vs}),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def parse_qnt(qnt_path, engine=None):
    """Parse a .qnt file into the normalized structure, or None on failure.
    engine: 'auto' (CLI then regex), 'cli', or 'regex'. Default comes from
    the QUINT_IR_ENGINE env var ('auto' if unset)."""
    engine = engine or DEFAULT_ENGINE
    qnt_path = Path(qnt_path)
    if not qnt_path.exists():
        return None
    ir = None
    if engine in ("auto", "cli"):
        ir = _parse_via_cli(qnt_path)
        if ir is None and engine == "cli":
            return None
    if ir is None:
        ir = _parse_via_regex(qnt_path)
    if ir is not None:
        ir["const_values"] = _scan_const_values(
            qnt_path.read_text(encoding="utf-8"))
    return ir


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Structured view of a Quint file.")
    p.add_argument("file", help="Path to the .qnt file")
    p.add_argument("--json", dest="emit_json", action="store_true")
    p.add_argument("--engine", choices=["auto", "cli", "regex"], default=DEFAULT_ENGINE)
    args = p.parse_args()

    ir = parse_qnt(args.file, engine=args.engine)
    if ir is None:
        print(f"ERROR: could not parse {args.file} "
              f"(engine={args.engine})", file=sys.stderr)
        sys.exit(2)

    if args.emit_json:
        print(json.dumps(ir, indent=2))
        return

    print(f"file:       {ir['file']}   (parsed via {ir['source']})")
    print(f"module:     {ir['module_name']}")
    if ir["imports"]:
        print(f"imports:    {', '.join(i['module'] for i in ir['imports'])}")
    for key in ("types", "consts", "vars", "actions", "vals", "temporals", "runs"):
        if ir[key]:
            print(f"{key + ':':<12}{', '.join(ir[key])}")
    if ir["action_mutations"]:
        print("mutations:")
        for a, vs in ir["action_mutations"].items():
            print(f"  {a:<20} -> {', '.join(vs) if vs else '(none)'}")


if __name__ == "__main__":
    main()
