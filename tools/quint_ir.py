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
    "consts":      ["MAX_FAILED_ATTEMPTS", ...],
    "vars":        ["sessions", ...],
    "actions":     ["login", ...],
    "vals":        ["atMostOneActiveSession", ...],   # top-level val/invariant
    "temporals":   ["eventualLogout", ...],
    "runs":        ["happyPath", ...],
    "action_mutations": {"login": ["sessions", ...], ...}
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
        "consts": [],
        "vars": [],
        "actions": [],
        "vals": [],
        "temporals": [],
        "runs": [],
        "action_mutations": {},
    }

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
        elif kind == "const":
            out["consts"].append(name)
        elif kind == "var":
            out["vars"].append(name)
        elif kind == "def":
            q = d.get("qualifier")
            if q == "action":
                out["actions"].append(name)
                out["action_mutations"][name] = _collect_mutations(d.get("expr"))
            elif q == "run":
                out["runs"].append(name)
            elif q == "temporal":
                out["temporals"].append(name)
            elif q in ("val", "pureval"):
                out["vals"].append(name)
            # def/puredef/nondet: helpers, not surfaced
    return out


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
CONST_RE    = re.compile(r"^\s*const\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", re.MULTILINE)
TYPE_RE     = re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
VAL_RE      = re.compile(r"^\s*(?:val|invariant)\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", re.MULTILINE)
TEMPORAL_RE = re.compile(r"^\s*temporal\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", re.MULTILINE)
RUN_RE      = re.compile(r"^\s*run\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
MUTATION_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)'\s*=")
LOCAL_VAL_RE = re.compile(r"^\s*val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")
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
    Returns (mutations, locals)."""
    mutations = {}
    locals_ = set()
    current = None
    action_depth = 0
    depth = 0
    for line in text.splitlines():
        depth_before = depth

        action_match = ACTION_RE.match(line)
        if action_match:
            current = action_match.group(1)
            mutations.setdefault(current, [])
            action_depth = depth_before

        if current is not None and (depth_before > action_depth or action_match):
            for m in MUTATION_RE.finditer(line):
                name = m.group(1)
                rest_match = re.search(rf"\b{re.escape(name)}'\s*=\s*(.*)", line)
                if rest_match:
                    rest = rest_match.group(1).strip().rstrip(",").rstrip(";").strip()
                    if rest == name:
                        continue
                if name not in mutations[current]:
                    mutations[current].append(name)
            lv = LOCAL_VAL_RE.match(line)
            if lv:
                locals_.add(lv.group(1))

        depth += line.count("{") - line.count("}")
        if current is not None and depth <= action_depth:
            current = None
            action_depth = 0
    return mutations, locals_


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

    mutations, action_locals = _parse_action_bodies(text)
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
        "consts": CONST_RE.findall(text),
        "vars": VAR_RE.findall(text),
        "actions": actions,
        "vals": vals,
        "temporals": TEMPORAL_RE.findall(text),
        "runs": RUN_RE.findall(text),
        "action_mutations": mutations,
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
