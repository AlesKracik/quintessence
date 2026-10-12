#!/usr/bin/env python3
"""
spec_source.py — where an area's spec lives, and the one place that reads it.

QUINT-FIRST. An area is three files, each the single source of one thing:

  specs/<area>.qnt            the formal model, AND every per-ID record that
                              is checkable against it — requirements,
                              invariants, properties, constants, examples —
                              as `///` doc comments on the declarations that
                              realize them. (A purely relational contract's
                              records sit on the `.als` assertions instead.)
  specs/<area>.intent.json    what a model cannot say: kind, purpose, brief,
                              scope, boundary, vocabulary, externals,
                              assumptions, decisions, open questions, state
                              machines, architecture, configuration.
  specs/<area>.records.json   ledgers no human types: check results,
                              witness verdicts, freshness pins,
                              verification log, traceability, provenance,
                              and the three triage ledgers.

No tool reads those files directly. `load_area()` DERIVES the area view —
the dict every tool has always consumed (schemas/area.schema.json still
describes it) — and `save_area()` writes back only the ledger part, refusing
a tool that tries to change anything authored. So the tools keep one input
shape, and the authored text stays where its referent is: a requirement sits
on the predicate that witnesses it, a constant's value is the model's value,
an example is the `run` that executes it. There is no copy left to drift, and
none of the lint rules that existed only to keep copies honest has anything
left to catch.

The parser is pure Python on purpose: Tier 1 (lint, readback) must work with
no Quint CLI installed. `quint parse` keeps the same `///` text on each
declaration in its IR (`declarations[].doc`), so the CLI agrees with this
reader about what is documented where; the Python reader is the canonical
one because it must also run where the CLI does not.

── Doc-comment grammar ────────────────────────────────────────────────────

A `///` block documents the declaration right below it (the HOST). One tag
per line. A record starts at a record tag and runs to the next record tag,
so one block can carry several records (several prohibitions on the action
they refuse, say). Lines before the first record tag are an ordinary doc
comment and are ignored.

  Record tags        @req ID · @inv ID · @prop ID · @con ID · @example ID
                     @screen <Name>              (one screen of a UI)
                     @nav [NAV-NNN] <From> -> <To>
                                                 (one navigation edge; the id
                                                 is optional and makes it a
                                                 maps_to / @refs target;
                                                 From `*` = every screen)
                     @verdict IMPOSSIBLE|NO-OP|GAP|OUT-OF-SCOPE
                                                 (what an action does where no
                                                 requirement says; on the
                                                 action, no id)
                     @internal <why>             (a `var` no requirement has
                                                 to explain; on the var)

Untagged lines right after the record tag are the record's DESCRIPTION
(invariant statement, constant description, example title, a requirement's
sentence in plain words). Untagged lines after an attribute tag CONTINUE that
tag's value. A blank `///` line ends a continuation.

  REQ   @while <expr>                            ears.state: FORMAL, a Quint
                                                 boolean over the state BEFORE
                                                 the step; it may read the
                                                 host action's parameters by
                                                 their own names
        @shall <expr>                            ears.response: FORMAL, a
                                                 Quint boolean over the state
                                                 AFTER the step, same names
        @old <name>: <Type> = <expr>             a value read BEFORE the step
                                                 (JML's old()), that @shall /
                                                 @outcome may name: "shall
                                                 increment" is
                                                 `count(u) == before + 1`
        @outcome <name>: <expr>                  one permitted outcome of a
                                                 `may` requirement (formal,
                                                 like @shall)
        @when <text>  @where <text>  @unwanted   ears.trigger/feature/unwanted
                                                 (words: the trigger IS the
                                                 host action)
        @type @status @modality @determinism @source
        @fit <metric> | <target> | <measurement>
        @error <External>.<OUTCOME> [idempotent|not-idempotent]: <effect>
        @error-note <text>                       note on the @error above
        @verified-by <path>, <path>              verified_by: the unit
                                                 test(s) that check a
                                                 computed requirement
        @refs ID, ID                             cross_refs: `<area>.<ID>`
                                                 for another area, a bare ID
                                                 for this one (qualified when
                                                 the view is derived). Any
                                                 declared kind: REQ INV PROP
                                                 CON EX DEC ASM Q
        @evidence @confidence @inferred-by @fingerprint   extraction.*
  INV   @criticality @proof @over @alloy <command> @smt <file>
        @quint-name <name> @predicate <expr> @refs @source
        + extraction tags
  PROP  (host: a `temporal`)  @refs
  CON   @unit @pairs INV-ID @value <json> @source  + extraction tags
  EX    @given <json> @when <action> [<json args>] @expect <json>
        @refs @source @trace
        (@expect is needed only on an EX hosted on an action: on a `run`
        whose body ends in `.expect(<cond>)`, that condition IS the
        expectation, derived as expect = {"run.expect": "<cond>"})
  SCREEN  description = purpose  @auth-required  @components A, B
  VERDICT description = the reason  @while <expr> (where on the action it
        applies; absent = everywhere)  @question Q-ID (a GAP's open
        question)  @scope <scope.excluded item> (an OUT-OF-SCOPE's anchor)
        @error <External>.<OUTCOME> (an external outcome it answers)
  NAV     description = the trigger, in the user's words  @guard <text>

Hosting — what the declaration under the block means for the record:

  REQ on `action <name>`           form "event": quint_ref = the action.
                                   Every step of it from a state where
                                   @while holds must end where @shall holds
                                   (must), where one @outcome holds (may),
                                   or must not happen at all (forbidden).
                                   Several requirements may share an action.
  REQ on a `pure def|pure val`     form "computed": a derived value no step
                                   carries, verified by @verified-by tests.
  REQ on the `module` with @shall  form "state": in every reachable state
                                   where @while holds, @shall holds.
  REQ on the `module`, no @shall   no form yet: raw, or non-functional.
  INV on `val <name>`              quint_name = name; on a `def` whose
                                   params are _prev* ghosts: a transition
                                   property, `over: probes`, body =
                                   predicate, probe val name = @quint-name.
  INV on an Alloy `assert|check <name>`  proof structural, alloy_command = name.
  PROP on `temporal <name>`        quint_name = name.
  CON on `pure val|val|const`      name = host; value = the literal body.
  EX on `run <name>`               quint_run = name; when.action = the
                                   @when action, else the run's last call.
  EX on `action <name>`            when.action = name (no run yet).
  SCREEN on `type <T>`             the screen is the variant <Name> of T;
                                   one record per variant, all in T's block,
                                   and every variant has one (lint).
  NAV on `action <name>`           navigation[].action = name; the action
                                   must read the screen var at <From> and
                                   set it to <To> (lint). From `*` (every
                                   screen) only has to set it. Several edges may
                                   share an action.
  SCREEN / NAV on the `module`     a UI with no model yet (Tier 1).
  VERDICT on `action <name>`       verdicts[].action = name, numbered per
                                   action in source order. IMPOSSIBLE: no
                                   step of it from @while happens. NO-OP:
                                   every such step changes no var. Both are
                                   checked (spec-probes). GAP and
                                   OUT-OF-SCOPE are statements of intent:
                                   they close the action's coverage there,
                                   and a GAP blocks approval.
  INTERNAL on `var <name>`         internal_vars[].var = name: the
                                   explanation check skips it.

Usage:
  tools/spec_source.py derive <area> [--root .]        # print the derived view
  tools/spec_source.py schemas [--write]               # intent/records schemas,
                        # carved out of schemas/area.schema.json
"""

import argparse
import copy
import json
import re
import sys
from pathlib import Path

INTENT_SUFFIX = ".intent.json"
RECORDS_SUFFIX = ".records.json"

# ── Ownership: which file holds which field ─────────────────────────────────
# ONE table. derive/save, spec-separation (claims vs bookkeeping) and
# the schemas are all read off it, so they cannot disagree about where a
# field lives.

MODEL_KEYS = ("requirements", "invariants", "properties", "constraints",
              "examples")
# An interactive surface's screens and navigation edges are model-owned too:
# a screen is a variant of the model's screen type, an edge is the action
# that moves between two of them. They are keyed by name / position, not by
# id, so they ride beside MODEL_KEYS rather than in it.
UI_KEYS = ("screens", "navigation")
# Completeness records on the model, also positional: a verdict says what an
# action does where no requirement does (spec-probes compiles it into a
# check), and an internal var is bookkeeping no requirement has to explain.
ACTION_KEYS = ("verdicts", "internal_vars")
POSITIONAL_KEYS = UI_KEYS + ACTION_KEYS
VERDICTS = ("IMPOSSIBLE", "NO-OP", "GAP", "OUT-OF-SCOPE")
RECORD_KEYS = ("check_results", "verification_log", "traceability",
               "generated_from", "extracted_from", "extraction_triage")
# Everything else at the top level is intent.

# Per-ID fields the tools write. Paths are dotted; `witness.outcomes[]`
# entries are keyed by outcome name.
WITNESS_MACHINE = ("status", "trace", "checked_at", "steps", "found_by",
                   "model_sha")
# A requirement carries two verdicts: the WITNESS (its antecedent is
# reachable: the requirement is not vacuous) and the OBLIGATION (every
# reachable step it governs satisfies it). See itf_tools.obligation_name.
OBLIGATION_MACHINE = ("formal_status", "trace", "checked_at", "steps",
                      "model_sha")
MACHINE_PATHS = {
    "requirements": ("witness.status", "witness.trace", "witness.checked_at",
                     "witness.steps", "witness.found_by", "witness.model_sha")
                    + tuple(f"obligation.{f}" for f in OBLIGATION_MACHINE),
    "invariants": ("formal_status",),
    "properties": ("formal_status",),
    "constraints": (),
    "examples": (),
}

ID_KIND = {"req": "requirements", "inv": "invariants", "prop": "properties",
           "con": "constraints", "example": "examples"}
RECORD_TAGS = tuple(ID_KIND) + ("screen", "nav", "verdict", "internal")

# tag -> (dotted path, value kind). Kinds: text, flag, list, json.
EXTRACTION_TAGS = {
    "evidence": ("extraction.evidence", "text"),
    "confidence": ("extraction.confidence", "text"),
    "inferred-by": ("extraction.inferred_by", "text"),
    "fingerprint": ("extraction.fingerprint", "text"),
}
TAGS = {
    "requirements": {
        "while": ("ears.state", "text"),
        "when": ("ears.trigger", "text"),
        "where": ("ears.feature", "text"),
        "unwanted": ("ears.unwanted", "flag"),
        "shall": ("ears.response", "text"),
        "type": ("type", "text"),
        "status": ("status", "text"),
        "modality": ("modality", "text"),
        "determinism": ("determinism", "text"),
        "source": ("source", "text"),
        "verified-by": ("verified_by", "list"),
        "refs": ("cross_refs", "list"),
        **EXTRACTION_TAGS,
        # special: fit, error, error-note, outcome
    },
    "invariants": {
        "criticality": ("criticality", "text"),
        "proof": ("proof", "text"),
        "over": ("over", "text"),
        "alloy": ("alloy_command", "text"),
        "smt": ("smt_file", "text"),
        "quint-name": ("quint_name", "text"),
        "predicate": ("predicate", "text"),
        "refs": ("cross_refs", "list"),
        "source": ("source", "text"),
        **EXTRACTION_TAGS,
    },
    "properties": {
        "quint-name": ("quint_name", "text"),
        "refs": ("cross_refs", "list"),
    },
    "constraints": {
        "unit": ("unit", "text"),
        "name": ("name", "text"),
        "pairs": ("paired_invariant", "text"),
        "value": ("value", "json"),
        "source": ("source", "text"),
        **EXTRACTION_TAGS,
    },
    "examples": {
        "given": ("given", "json"),
        "expect": ("expect", "json"),
        "refs": ("refs", "list"),
        "source": ("source", "text"),
        "trace": ("trace", "text"),
        # special: when
    },
    "screens": {
        "auth-required": ("auth_required", "flag"),
        "components": ("components", "list"),
    },
    "navigation": {
        "guard": ("guard", "text"),
    },
    "verdicts": {
        "while": ("while", "text"),
        "question": ("question", "text"),
        "scope": ("scope_ref", "text"),
        # special: error
    },
    "internal_vars": {},
}
SPECIAL_TAGS = {"requirements": ("fit", "error", "error-note", "outcome", "old"),
                "examples": ("when",),
                "verdicts": ("error",)}


class SpecSourceError(Exception):
    """An area's files cannot be read as a spec, or a tool tried to write a
    field it does not own."""


# ── Paths ───────────────────────────────────────────────────────────────────

def specs_dir(root):
    return Path(root) / "specs"


def intent_path(root, name):
    return specs_dir(root) / f"{name}{INTENT_SUFFIX}"


def records_path(root, name):
    return specs_dir(root) / f"{name}{RECORDS_SUFFIX}"


def area_exists(root, name):
    return intent_path(root, name).exists()


def list_areas(root):
    """Every area with an intent file, sorted."""
    d = specs_dir(root)
    if not d.is_dir():
        return []
    return sorted(p.name[:-len(INTENT_SUFFIX)] for p in d.glob(f"*{INTENT_SUFFIX}"))


def model_file(intent, name):
    fm = (intent or {}).get("formal_model") or {}
    return fm.get("quint_file") or f"{name}.qnt"


def alloy_file(intent):
    return ((intent or {}).get("formal_model") or {}).get("alloy_file")


# ── Where an area's code is ─────────────────────────────────────────────────
# One answer for the audit, `spec-record stamp/changed` and the beat router.

# Excluded unless an area says otherwise: test files are not the behavior
# under extraction, and triaging them by hand one NOT-BEHAVIOR at a time is
# what made an audit report 207 of them.
DEFAULT_CODE_EXCLUDES = ("**/__tests__/**", "**/*.test.*", "**/*.spec.*",
                         "**/test_*.py", "**/*_test.py", "**/*_test.go")


def _glob_re(pattern):
    """A path glob as a regex: `**` spans directories, `*` and `?` do not.
    A pattern with no glob characters is a directory or file prefix."""
    pat = pattern.replace("\\", "/")
    if pat.startswith("./"):
        pat = pat[2:]
    if not any(c in pat for c in "*?["):
        base = pat.rstrip("/")
        return re.compile("^" + re.escape(base) + "(?:/.*)?$")
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile("^" + out + "$")


def code_scope(entry, override=None):
    """(includes, excludes) for an area's `.spec/project.json` entry.

    `code_paths: [glob, ...]` for an area whose code is scattered (routes/,
    utils/, services/), `code_path` for one that is not; `override` (a CLI
    --code-path, repeatable) replaces both. Excludes are the area's
    `exclude[]`, its `tests_path`, and test files by default — set
    `include_tests: true` to scan them."""
    entry = entry or {}
    if override:
        includes = [override] if isinstance(override, str) else list(override)
    else:
        includes = list(entry.get("code_paths") or [])
        if not includes and entry.get("code_path"):
            includes = [entry["code_path"]]
    excludes = list(entry.get("exclude") or [])
    if not entry.get("include_tests"):
        excludes += list(DEFAULT_CODE_EXCLUDES)
        if entry.get("tests_path"):
            excludes.append(entry["tests_path"])
    return includes, excludes


def in_code_scope(rel, includes, excludes):
    """True when repo-relative `rel` is inside the scope."""
    rel = rel.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if not any(_glob_re(p).match(rel) for p in includes):
        return False
    return not any(_glob_re(p).match(rel) for p in excludes)


def code_files(repo_root, includes, excludes, suffixes=None):
    """Files under repo_root in scope, sorted, optionally narrowed to
    source suffixes."""
    root = Path(repo_root)
    out = set()
    for pattern in includes:
        # Walk from the longest literal prefix, not the whole repo.
        literal = []
        for part in pattern.replace("\\", "/").split("/"):
            if any(c in part for c in "*?["):
                break
            literal.append(part)
        base = root.joinpath(*[p for p in literal if p and p != "."])
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = (f for f in base.rglob("*") if f.is_file())
        else:
            continue
        for f in candidates:
            rel = str(f.relative_to(root)).replace("\\", "/")
            if suffixes and f.suffix not in suffixes:
                continue
            if in_code_scope(rel, [pattern], excludes):
                out.add(f)
    return sorted(out)


# ── Doc-comment reader ──────────────────────────────────────────────────────

QUINT_HEADER = re.compile(
    r"^(?P<indent>\s*)(?:(?P<pure>pure)\s+)?"
    r"(?P<kind>module|type|const|var|val|def|action|run|temporal|nondet|import|export|assume)\b"
    r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)?")
ALLOY_HEADER = re.compile(
    r"^(?P<indent>\s*)(?P<kind>module|assert|check|pred|fact|fun|sig|run|abstract\s+sig|one\s+sig)\b"
    r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)?")
DOC_RE = re.compile(r"^\s*///(?: ?)(.*)$")
TAG_RE = re.compile(r"^@([a-z][a-z-]*)(?:\s+(.*))?$")


def strip_docs(text):
    """The model with its `///` record comments removed — what model_sha
    hashes. Editing a description or an EARS field changes what a reviewer reads,
    not what the checker explored, so it must not stale every witness."""
    return "".join(line for line in (text or "").splitlines(keepends=True)
                   if not DOC_RE.match(line))


def _strip_line_comment(line):
    """Drop a trailing `//` comment that is not inside a string literal."""
    out, in_str, i = [], False, 0
    while i < len(line):
        c = line[i]
        if c == '"':
            in_str = not in_str
        elif not in_str and line.startswith("//", i):
            break
        out.append(c)
        i += 1
    return "".join(out)


def _first_assign(text):
    """Index of the declaration's own `=` (not ==, =>, <=, >=, !=)."""
    depth = 0
    in_str = False
    for i, c in enumerate(text):
        if c == '"':
            in_str = not in_str
        if in_str:
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "=" and depth == 0:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if prev in "=<>!" or nxt in "=>":
                continue
            return i
    return -1


def _split_top(text, sep=","):
    parts, depth, cur, in_str = [], 0, [], False
    for c in text:
        if c == '"':
            in_str = not in_str
        if not in_str:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == sep and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
                continue
        cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _host_extent(lines, i, indent):
    """Lines of the declaration starting at line i: up to the next non-blank
    line indented no deeper than the header (a closing bracket at the
    header's indent belongs to it)."""
    out = [lines[i]]
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if line.strip():
            ind = len(line) - len(line.lstrip())
            if ind <= indent:
                if ind == indent and line.strip()[0] in "})]":
                    out.append(line)
                break
        out.append(line)
        j += 1
    while out and not out[-1].strip():
        out.pop()
    return out


def _parse_host(lines, i, lang):
    m = (QUINT_HEADER if lang == "quint" else ALLOY_HEADER).match(lines[i])
    if not m:
        return None
    kind = re.sub(r"\s+", " ", m.group("kind"))
    host = {"kind": kind, "pure": bool(m.groupdict().get("pure")),
            "name": m.group("name"), "line": i + 1, "params": [], "body": None}
    if kind == "module":
        return host
    extent = _host_extent(lines, i, len(m.group("indent")))
    host["source"] = "\n".join(extent)
    if lang != "quint":
        return host
    code = " ".join(s for s in (_strip_line_comment(l).strip() for l in extent) if s)
    # The name as a whole word: a plain substring search finds `f` inside
    # `def` and `a` inside `action`, and loses the parameters.
    nm = re.search(rf"(?<![A-Za-z0-9_]){re.escape(host['name'])}(?![A-Za-z0-9_])",
                   code) if host["name"] else None
    after = code[nm.end():] if nm else ""
    if after.startswith("("):
        depth = 0
        for k, c in enumerate(after):
            depth += c == "("
            depth -= c == ")"
            if depth == 0:
                inner, after = after[1:k], after[k + 1:]
                break
        for p in _split_top(inner):
            pname, _, ptype = p.partition(":")
            host["params"].append((pname.strip(), ptype.strip() or None))
    eq = _first_assign(after)
    if eq >= 0:
        host["body"] = after[eq + 1:].strip()
    return host


def _parse_records(doc_lines, host, filename):
    """Split one `///` block, given as (line number, text) pairs, into
    records. Returns a list of dicts:
    {tag, id, extra, desc: [..], tags: [(name, value)], host, where} — where
    `where` is the record tag's own line, not the block's."""
    records, cur, mode = [], None, "skip"
    for lineno, raw in doc_lines:
        line = raw.strip()
        m = TAG_RE.match(line)
        if m and m.group(1) in RECORD_TAGS:
            rest = (m.group(2) or "").strip()
            rid, _, extra = rest.partition(" ")
            cur = {"tag": m.group(1), "id": rid, "extra": extra.strip(),
                   "desc": [], "tags": [], "host": host,
                   "where": {"file": filename, "line": lineno}}
            records.append(cur)
            mode = "desc"
            continue
        if cur is None:
            continue
        if m:
            cur["tags"].append([m.group(1), (m.group(2) or "").strip()])
            mode = "cont"
        elif not line:
            mode = "desc"
        elif mode == "cont":
            last = cur["tags"][-1]
            last[1] = (last[1] + " " + line).strip()
        else:
            cur["desc"].append(line)
    return records


def read_records(text, lang="quint", filename=None):
    """Every record in a model file, in source order."""
    lines = (text or "").splitlines()
    out, doc = [], []
    for i, line in enumerate(lines):
        m = DOC_RE.match(line)
        if m:
            doc.append((i + 1, m.group(1)))
            continue
        if doc:
            if not line.strip() or line.strip().startswith("//"):
                continue  # a blank or plain comment between doc and host
            host = _parse_host(lines, i, lang)
            out.extend(_parse_records(doc, host, filename))
            doc = []
    if doc:
        out.extend(_parse_records(doc, None, filename))
    return out


# ── Declarations: what the model's names reach ─────────────────────────────
# Pure Python on purpose, like the record reader: the encoding checks and the
# journey and example rendering must run where the Quint CLI does not.

IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def declarations(text):
    """Every module-member declaration in a Quint file, documented or not:
    name -> host (kind, pure, body, params, line, source). Members are the
    headers at the indent directly inside a `module`; local `val`s inside an
    action body sit deeper and are part of that action's body instead."""
    lines = (text or "").splitlines()
    out = {}
    mod_indent = None
    member_indent = None
    for i, line in enumerate(lines):
        if DOC_RE.match(line) or line.strip().startswith("//"):
            continue
        m = QUINT_HEADER.match(line)
        if not m:
            continue
        indent = len(m.group("indent"))
        if m.group("kind") == "module":
            mod_indent, member_indent = indent, None
            continue
        if mod_indent is None or indent <= mod_indent:
            continue
        if member_indent is None:
            member_indent = indent
        if indent != member_indent or not m.group("name"):
            continue
        host = _parse_host(lines, i, "quint")
        if host and host["name"] not in out:
            out[host["name"]] = host
    return out


def _code_only(body):
    """A declaration body with string literals blanked, so `"login"` in an
    argument is not read as a reference to the action `login`."""
    # Length-preserving, so positions found in the blanked text index the
    # original body too.
    return re.sub(r'"(?:\\.|[^"\\])*"',
                  lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', body or "")


def name_closure(decls, roots):
    """Every identifier reachable from `roots` through the bodies of the
    module's own declarations: what an action (or invariant) READS, helpers
    included. `login_failed` reaches MAX_FAILED_ATTEMPTS through its own
    body; `expire_session` reaches it only if some helper it calls does."""
    seen, frontier = set(), [r for r in roots if r]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        host = decls.get(name)
        if not host or not host.get("body"):
            continue
        params = {p for p, _ in host.get("params") or []}
        for tok in IDENT_TOKEN.findall(_code_only(host["body"])):
            if tok not in seen and tok not in params:
                frontier.append(tok)
    return seen


def _balanced(text, i):
    """Index just past the parenthesis group opening at text[i] == '('."""
    depth = 0
    in_str = False
    for k in range(i, len(text)):
        c = text[k]
        if c == '"' and (k == 0 or text[k - 1] != "\\"):
            in_str = not in_str
        if in_str:
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k + 1
    return len(text)


def run_calls(decls, run_name, _stack=None):
    """The actions a `run` performs, in order — the run's steps.

    Each step is {"action", "args", "qualifier", "fails"}: `args` is the
    argument text as written, `qualifier` the module of a qualified call
    (`auth::login(..)` -> "auth"), and `fails` true for `A.fail()` — a call
    the run expects to be REFUSED, the way a guard-style model shows a
    prohibition. A run that chains another run (`happyPath.then(...)`)
    contributes that run's steps in place. `init` is the starting point, not
    a step, and the trailing `.expect(..)` is the run's claim, not a step."""
    _stack = _stack or set()
    host = decls.get(run_name)
    if not host or host.get("kind") != "run" or run_name in _stack:
        return []
    _stack = _stack | {run_name}
    body = host.get("body") or ""
    cond = _run_expectation(body)
    if cond is not None:
        # Cut the trailing .expect(...) chain off before scanning for calls.
        text = body.strip()
        while text.endswith(")"):
            k = len(text) - 1
            depth = 0
            while k >= 0:
                depth += text[k] == ")"
                depth -= text[k] == "("
                if depth == 0:
                    break
                k -= 1
            if k < 0 or not text[:k].rstrip().endswith(".expect"):
                break
            text = text[:k].rstrip()[:-len(".expect")].rstrip()
        body = text
    actions = {n for n, h in decls.items() if h.get("kind") == "action"}
    runs = {n for n, h in decls.items() if h.get("kind") == "run"}
    steps = []
    code = _code_only(body)
    pos = 0
    while True:
        m = IDENT_TOKEN.search(code, pos)
        if not m:
            break
        name, start, end = m.group(0), m.start(), m.end()
        pos = end
        qualifier = None
        if start >= 2 and code[start - 2:start] == "::":
            q = re.search(r"([A-Za-z_][A-Za-z0-9_]*)::$", code[:start])
            qualifier = q.group(1) if q else None
        elif code[end:end + 2] == "::":
            continue  # the module half of a qualified name
        if start and code[start - 1] == ".":
            # A method: .then .expect .fail .reps. A mid-run `.expect(..)` is
            # a claim, not steps — skip its argument, or a qualified helper
            # it reads (`auth::isLocked("bob")`) would read as a call.
            if name == "expect" and code[end:end + 1] == "(":
                pos = _balanced(body, end)
            continue
        if name in runs and not qualifier:
            steps.extend(run_calls(decls, name, _stack))
            continue
        if name == "init" or (name not in actions and not qualifier):
            continue
        args, parens = "", code[end:end + 1] == "("
        after = end
        if parens:
            after = _balanced(body, end)
            args = " ".join(body[end + 1:after - 1].split())
        fails = bool(re.match(r"\s*\.\s*fail\s*\(\s*\)", code[after:]))
        steps.append({"action": name, "args": args, "qualifier": qualifier,
                      "fails": fails, "parens": parens})
        pos = after
    return steps


def _show_args(args):
    return re.sub(r'"([^"\\]*)"', r"\1", args or "")


def render_call(step):
    """`login("alice", "s1")` -> `login(alice, s1)`; a refused call reads
    `login(bob, s2) — refused`."""
    name = f"{step['qualifier']}::{step['action']}" if step.get("qualifier") else step["action"]
    text = f"{name}({_show_args(step.get('args'))})" if step.get("parens", True) else name
    return text + (" — refused" if step.get("fails") else "")


def collapse_calls(steps):
    """Consecutive identical calls folded into one `× n` entry, so five
    failed logins read as one line instead of five."""
    out = []
    for s in steps:
        text = render_call(s)
        if out and out[-1][0] == text:
            out[-1][1] += 1
        else:
            out.append([text, 1])
    return [t if n == 1 else f"{t} × {n}" for t, n in out]


def _expect_before_refusal(body):
    """The condition of the last `.expect(..)` before a run's refused call,
    or None — the blocking state the refused call was attempted in."""
    body = body or ""
    k = body.rfind(".fail(")
    if k < 0:
        return None
    e = body.rfind(".expect(", 0, k)
    if e < 0:
        return None
    start = e + len(".expect")
    end = _balanced(body, start)
    return " ".join(body[start + 1:end - 1].split())


def example_row(example, decls):
    """One example as a (given, when, then) row of plain strings — DERIVED
    from the run it sits on, never written beside it, so the row a reviewer
    reads is the run `quint test` executes.

    A run's last call is When, everything before it is Given (from `init`),
    its trailing `.expect(..)` is Then. A run that ENDS in a refused call
    (`.then(login("bob", "s2").fail())`) shows the refusal: When is the call
    marked refused, Then is that nothing changes, and the `.expect(..)` just
    before the refused call — the blocking state it was refused in — rides
    with Given. An example hosted on an action reads its @given / @when /
    @expect JSON instead. Returns None when there is nothing to show."""
    def kv(d):
        return ", ".join(f"{k} = {json.dumps(v) if not isinstance(v, str) else v}"
                         for k, v in (d or {}).items())
    run = example.get("quint_run")
    if run and run in decls:
        steps = run_calls(decls, run)
        if steps and steps[-1].get("fails"):
            given = "; then ".join(collapse_calls(steps[:-1])) or "the initial state"
            cond = _expect_before_refusal(decls[run].get("body"))
            if cond:
                given += f", so `{cond}`"
            return {"given": given, "when": render_call(steps[-1]),
                    "then": "refused — nothing changes", "then_is_code": False}
        if steps:
            given = collapse_calls(steps[:-1])
            then = _run_expectation(decls[run].get("body")) or kv(
                {k: v for k, v in (example.get("expect") or {}).items()
                 if k != RUN_EXPECT_KEY})
            cond = _run_expectation(decls[run].get("body"))
            return {"given": "; then ".join(given) if given else "the initial state",
                    "when": render_call(steps[-1]),
                    "then": then or "—", "then_is_code": bool(cond)}
    when = example.get("when") or {}
    if not when.get("action"):
        return None
    args = when.get("args")
    shown = (kv(args) if isinstance(args, dict) else
             ", ".join(map(str, args)) if isinstance(args, list) else "")
    expect = {k: v for k, v in (example.get("expect") or {}).items() if k != RUN_EXPECT_KEY}
    return {"given": kv(example.get("given")) or "the initial state",
            "when": f"{when['action']}({shown})",
            "then": kv(expect) or (example.get("expect") or {}).get(RUN_EXPECT_KEY) or "—",
            "then_is_code": False}


# ── Journeys: steps, their example rows, and the run that walks them ───────

def model_declarations(root, name, view):
    """name -> declaration for an area's Quint model, or None when it has no
    model yet (Tier 1: records only) or the file is missing."""
    rel = ((view or {}).get("formal_model") or {}).get("quint_file") or f"{name}.qnt"
    try:
        text = (specs_dir(root) / rel).read_text(encoding="utf-8")
    except OSError:
        return None
    return declarations(text) if has_model(text) else None


IMPORT_RE = re.compile(r'^\s*import\s+([A-Za-z_]\w*)(?:\.\*)?\s+from\s+"([^"]+)"', re.M)


def imported_declarations(root, text):
    """alias -> declarations of each module a model imports (`import auth
    from "./auth"` -> "auth"); {} for a file that is not there."""
    out = {}
    for alias, rel in IMPORT_RE.findall(text or ""):
        path = specs_dir(root) / (rel[2:] if rel.startswith("./") else rel)
        if not path.suffix:
            path = path.with_suffix(".qnt")
        try:
            out[alias] = declarations(path.read_text(encoding="utf-8"))
        except OSError:
            out[alias] = {}
    return out


def step_action(view, rid):
    """The action a journey step is realized by: a requirement's quint_ref,
    or a navigation edge's action."""
    for r in (view or {}).get("requirements") or []:
        if r.get("id") == rid:
            return r.get("quint_ref")
    for n in (view or {}).get("navigation") or []:
        if n.get("id") == rid:
            return n.get("action")
    return None


def step_examples(step, views):
    """[(qualified id, example or None, area)] shown at one journey step:
    the step's explicit `examples`, else every example of the step's area
    whose @refs names the step. None marks an id that resolves to nothing."""
    area, rid = step["ref"].split(".", 1)
    explicit = step.get("examples") or []
    if explicit:
        out = []
        for q in explicit:
            a, eid = q.split(".", 1)
            ex = next((e for e in ((views.get(a) or {}).get("examples") or [])
                       if e.get("id") == eid), None)
            out.append((q, ex, a))
        return out
    return [(f"{area}.{e['id']}", e, area)
            for e in ((views.get(area) or {}).get("examples") or [])
            if rid in (e.get("refs") or []) or f"{area}.{rid}" in (e.get("refs") or [])]


def _qualifiers(area, view):
    rel = ((view or {}).get("formal_model") or {}).get("quint_file") or f"{area}.qnt"
    return {area, Path(rel).stem, area.replace("-", "_")}


def action_reach(decls, name, resolve=None, _stack=None):
    """{(qualifier, action)} one call of `name` performs: itself, and every
    action its body calls — the module's own (qualifier None) and an
    imported module's (`auth::login(..)` -> ("auth", "login")), through
    helpers and nested actions. A UI action that signs the user in by
    calling `auth::login` performs auth's login too, and that is what lets
    a journey step in auth be covered by a run of the UI model.

    `resolve(qualifier)` returns the imported module's declarations, when
    known: a qualified name then counts only if it is an ACTION there, so
    reading `auth::sessions` is not taken for a call. Without it, every
    qualified name that is not an assignment target is kept."""
    _stack = (_stack or set()) | {name}
    out = {(None, name)}
    host = decls.get(name)
    if not host or not host.get("body"):
        return out
    params = {p for p, _ in host.get("params") or []}
    code = _code_only(host["body"])
    for m in IDENT_TOKEN.finditer(code):
        tok, start, end = m.group(0), m.start(), m.end()
        if start and code[start - 1] == ".":
            continue
        if start >= 2 and code[start - 2:start] == "::":
            q = re.search(r"([A-Za-z_][A-Za-z0-9_]*)::$", code[:start])
            # A primed name is an assignment target (`auth::x' = ..`), not
            # a call.
            if not q or code[end:end + 1] == "'":
                continue
            other = resolve(q.group(1)) if resolve else None
            if other is None or (other.get(tok) or {}).get("kind") == "action":
                out.add((q.group(1), tok))
            continue
        if code[end:end + 2] == "::" or tok in params or tok in _stack:
            continue
        if (decls.get(tok) or {}).get("kind") in ("action", "def", "val"):
            out |= action_reach(decls, tok, resolve, _stack)
    return out


def journey_run(journey, views, decls_for):
    """What the journey's run proves about the journey.

    Returns None for a journey with no run, else a dict:
      run, area      the qualified run and the area whose model holds it
      found          False when the run does not exist
      covered        step refs the run calls, in the journey's order
      outside        step refs in an area the run never calls
      order_error    (step number, ref, action) of the first step whose action
                     the run does not call after the previous step's, or None
    `decls_for(area)` returns that area's declarations (or None).

    A step is matched by a call that performs its action, directly or
    through the actions it calls (`action_reach`). One call may cover
    several consecutive steps — a cancel that stops billing (REQ-001) and
    keeps access (REQ-002) is one call and two steps — so the search for a
    step starts AT the previous step's call, not after it. The same
    requirement twice in a row is two calls."""
    ref = journey.get("run")
    if not ref or "." not in ref:
        return None
    rarea, rname = ref.split(".", 1)
    decls = decls_for(rarea) if rarea in views else None
    res = {"run": ref, "area": rarea, "found": False, "covered": [],
           "outside": [], "order_error": None}
    if not decls or (decls.get(rname) or {}).get("kind") != "run":
        return res
    res["found"] = True
    calls = run_calls(decls, rname)

    def resolve(qualifier):
        for a, view in views.items():
            if a != rarea and qualifier in _qualifiers(a, view):
                return decls_for(a)
        return None

    reach = []
    for c in calls:
        if c["qualifier"]:
            reach.append({(c["qualifier"], c["action"])})
        else:
            reach.append(action_reach(decls, c["action"], resolve))
    idx, prev = 0, None
    steps = [s for s in journey.get("steps") or [] if "." in (s.get("ref") or "")]
    for n, step in enumerate(steps, 1):
        a, rid = step["ref"].split(".", 1)
        action = step_action(views.get(a), rid)
        if a == rarea:
            quals = {None}
        else:
            quals = _qualifiers(a, views.get(a))
            if not any(q in quals for r in reach for q, _ in r):
                res["outside"].append(step["ref"])
                continue
        if not action:
            res["outside"].append(step["ref"])
            continue
        if prev is None:
            start = 0
        elif step["ref"] == prev:
            start = idx + 1
        else:
            start = idx
        k = next((i for i in range(start, len(calls))
                  if any(q in quals and act == action for q, act in reach[i])), None)
        if k is None:
            res["order_error"] = (n, step["ref"], action)
            break
        res["covered"].append(step["ref"])
        idx, prev = k, step["ref"]
    return res


# ── Derive ──────────────────────────────────────────────────────────────────

def ears_sentence(req):
    """The EARS sentence, rendered from the fields; the description for an
    unstructured requirement. Past `raw`, @while and @shall are Quint, so
    they render as code: the sentence says exactly what is checked, and the
    plain-words version a reader wants is the record's description. A raw
    requirement's clauses are still words and render as words."""
    ears = req.get("ears")
    if not ears:
        return req.get("description") or "(no description)"
    if req.get("status", "raw") == "raw":
        parts = []
        if ears.get("feature"):
            parts.append(f"Where {ears['feature']}, ")
        if ears.get("state"):
            parts.append(f"While {ears['state']}, ")
        if ears.get("trigger"):
            parts.append(f"{'if' if ears.get('unwanted') else 'when'} {ears['trigger']}, ")
        shall = "then the system shall" if ears.get("unwanted") else "the system shall"
        parts.append(f"{shall} {ears.get('response') or '…'}.")
        sentence = "".join(parts)
        return sentence[0].upper() + sentence[1:]
    parts = []
    if ears.get("feature"):
        parts.append(f"Where {ears['feature']}, ")
    if ears.get("state"):
        parts.append(f"While `{ears['state']}`, ")
    trigger = ears.get("trigger") or (
        f"`{req['quint_ref']}` runs" if req.get("quint_ref") else None)
    if trigger:
        joiner = "if" if ears.get("unwanted") else "when"
        parts.append(f"{joiner} {trigger}, ")
    shall = "then the system shall" if ears.get("unwanted") else "the system shall"
    if req.get("modality") == "forbidden":
        parts.append(f"{shall} never let it happen.")
    elif req.get("modality") == "may":
        outs = [o.get("predicate") for o in (req.get("witness") or {}).get("outcomes") or []]
        parts.append(f"{shall} end in one of: "
                     + "; ".join(f"`{o}`" for o in outs if o) + ".")
    else:
        resp = ears.get("response")
        parts.append(f"{shall} make `{resp}` hold." if resp else f"{shall} …")
    sentence = "".join(parts)
    return sentence[0].upper() + sentence[1:]


def _set(d, path, value):
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _del(d, path):
    keys = path.split(".")
    stack = []
    for k in keys[:-1]:
        if not isinstance(d, dict) or k not in d:
            return
        stack.append((d, k))
        d = d[k]
    if isinstance(d, dict):
        d.pop(keys[-1], None)
    for parent, k in reversed(stack):
        if parent[k] == {}:
            del parent[k]
        else:
            break


def _list_value(v):
    return [x.strip() for x in re.split(r",", v or "") if x.strip()]


_BAD = object()


def _json_value(v, rec, problems):
    """A tag's JSON value, or _BAD (reported) — a malformed @given is a lint
    finding, never a crashed derivation."""
    try:
        return json.loads(v)
    except json.JSONDecodeError as e:
        problems.append(f"{_where(rec)}: {rec['id']}: not JSON: {v!r} ({e.msg})")
        return _BAD


def _where(rec):
    w = rec.get("where") or {}
    return f"{w.get('file') or '?'}:{w.get('line') or '?'}"


def _literal(body):
    """A constant's value from its declaration body, or None."""
    if body is None:
        return None
    b = body.strip()
    if re.fullmatch(r"-?\d[\d_]*", b):
        return int(b.replace("_", ""))
    if b in ("true", "false"):
        return b == "true"
    if re.fullmatch(r'"[^"]*"', b):
        return b[1:-1]
    return None


ERROR_RE = re.compile(
    r"^(?P<ext>[^.\s]+)\.(?P<out>\S+?)(?:\s+(?P<flag>idempotent|not-idempotent))?\s*:\s*(?P<effect>.*)$")


def _apply_tags(rec, kind, item, problems):
    table = TAGS[kind]
    special = SPECIAL_TAGS.get(kind, ())
    for name, value in rec["tags"]:
        if name in table:
            path, vk = table[name]
            if vk == "flag":
                _set(item, path, True)
            elif vk == "list":
                _set(item, path, _list_value(value))
            elif vk == "json":
                v = _json_value(value, rec, problems)
                if v is not _BAD:
                    _set(item, path, v)
            else:
                _set(item, path, value)
        elif name in special:
            if name == "fit":
                parts = [p.strip() for p in value.split("|")]
                if len(parts) != 3:
                    problems.append(f"{_where(rec)}: {rec['id']}: @fit needs "
                                    f"'metric | target | measurement'")
                    continue
                item["fit_criterion"] = dict(zip(("metric", "target", "measurement"), parts))
            elif name == "error" and kind == "verdicts":
                m = re.match(r"^([^.\s]+)\.([^\s:]+)\s*(?::\s*(.*))?$", value)
                if not m:
                    problems.append(f"{_where(rec)}: @verdict: @error takes "
                                    f"'External.OUTCOME' (the outcome this "
                                    f"verdict answers)")
                    continue
                eo = {"external": m.group(1), "outcome": m.group(2)}
                if m.group(3):
                    eo["effect"] = m.group(3).strip()
                item.setdefault("error_outcomes", []).append(eo)
            elif name == "error":
                m = ERROR_RE.match(value)
                if not m:
                    problems.append(f"{_where(rec)}: {rec['id']}: @error needs "
                                    f"'External.OUTCOME [idempotent]: effect'")
                    continue
                eo = {"external": m.group("ext"), "outcome": m.group("out"),
                      "effect": m.group("effect").strip()}
                if m.group("flag"):
                    eo["idempotent"] = m.group("flag") == "idempotent"
                item.setdefault("error_outcomes", []).append(eo)
            elif name == "error-note":
                if not item.get("error_outcomes"):
                    problems.append(f"{_where(rec)}: {rec['id']}: @error-note before any @error")
                    continue
                item["error_outcomes"][-1]["note"] = value
            elif name == "old":
                m = re.match(r"^([a-z_][A-Za-z0-9_]*)\s*:\s*([^=]+?)\s*=\s*(\S.*)$", value)
                if not m:
                    problems.append(f"{_where(rec)}: {rec['id']}: @old takes "
                                    f"'<name>: <Type> = <Quint expression>'")
                    continue
                item.setdefault("old", []).append(
                    {"name": m.group(1), "type": m.group(2).strip(),
                     "expr": m.group(3).strip()})
            elif name == "outcome":
                m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(\S.*)$", value)
                if not m:
                    problems.append(f"{_where(rec)}: {rec['id']}: @outcome takes "
                                    f"'<name>: <Quint expression>'")
                    continue
                item.setdefault("witness", {}).setdefault("outcomes", []).append(
                    {"name": m.group(1), "predicate": m.group(2).strip()})
            elif name == "when":
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", value)
                if not m:
                    problems.append(f"{_where(rec)}: {rec['id']}: @when takes "
                                    f"'<action> [<json args>]'")
                    continue
                item["when"] = {"action": m.group(1)}
                if m.group(2).strip():
                    args = _json_value(m.group(2), rec, problems)
                    if args is not _BAD:
                        item["when"]["args"] = args
        else:
            problems.append(f"{_where(rec)}: {rec['id']}: unknown tag @{name} on "
                            f"{'an' if rec['tag'][0] in 'aeiou' else 'a'} @{rec['tag']} record")


def _run_last_action(body, actions):
    if not body:
        return None
    calls = [c for c in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", body)
             if c in actions and c != "init"]
    return calls[-1] if calls else None


RUN_EXPECT_KEY = "run.expect"
RUN_REFUSES_KEY = "run.refuses"


def _run_expectation(body):
    """The condition of the trailing `.expect(<cond>)` of a run body, or
    None. That call IS the run's assertion — quint test fails the run when it
    is false — so an example hosted on such a run already claims something,
    and asking for an `@expect {json}` beside it would be a second copy of
    the same claim, in a weaker language. Several chained `.expect`s are
    kept, joined with `and`."""
    text = (body or "").strip()
    conds = []
    while text.endswith(")"):
        depth, i = 0, len(text) - 1
        while i >= 0:
            if text[i] == ")":
                depth += 1
            elif text[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < 0 or not text[:i].rstrip().endswith(".expect"):
            break
        conds.insert(0, " ".join(text[i + 1:-1].split()))
        text = text[:i].rstrip()[:-len(".expect")].rstrip()
    return " and ".join(conds) if conds else None


def _natural(rid):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", rid or "")]


# `@nav [NAV-NNN] <From> -> <To>`. The optional id is what lets an audit
# site be MAPPED to an edge (no REQ invented to stand for it). From `*` is
# an edge from EVERY screen — a sidebar, a global header link — which a
# per-screen edge list cannot say without one record per screen.
NAV_RE = re.compile(r"^(?:(?P<id>NAV-\d{3})\s+)?(?P<from>\*|[A-Za-z_][A-Za-z0-9_]*)"
                    r"\s*->\s*(?P<to>[A-Za-z_][A-Za-z0-9_]*)$")
ANY_SCREEN = "*"
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _derive_ui(rec, out, seen, problems):
    """One @screen or @nav record -> a screens[] / navigation[] entry, in
    source order. Whether the host agrees (the screen is a variant of the
    type it sits on, the action moves between the two screens) is lint's
    call: it needs the model's declarations, not just the record."""
    host = rec["host"] or {}
    desc = " ".join(rec["desc"]).strip()
    if rec["tag"] == "screen":
        name = rec["id"]
        if not IDENT_RE.match(name or "") or rec["extra"]:
            problems.append(f"{_where(rec)}: @screen takes one screen name, got "
                            f"{(name + ' ' + rec['extra']).strip()!r}")
            return
        if name in seen:
            problems.append(f"{_where(rec)}: duplicate screen {name} (first at {seen[name]})")
            return
        seen[name] = _where(rec)
        item = {"name": name, "auth_required": False}
        if desc:
            item["purpose"] = desc
        _apply_tags(rec, "screens", item, problems)
        out["screens"].append(item)
        return
    edge = f"{rec['id']} {rec['extra']}".strip()
    m = NAV_RE.match(edge)
    if not m:
        problems.append(f"{_where(rec)}: @nav takes '<From> -> <To>' (optionally "
                        f"'NAV-NNN' first; From may be '*'), got {edge!r}")
        return
    item = {"from": m.group("from"), "to": m.group("to")}
    if m.group("id"):
        if m.group("id") in seen:
            problems.append(f"{_where(rec)}: duplicate navigation id {m.group('id')} "
                            f"(first at {seen[m.group('id')]})")
            return
        seen[m.group("id")] = _where(rec)
        item = {"id": m.group("id"), **item}
    if desc:
        item["trigger"] = desc
    else:
        problems.append(f"{_where(rec)}: @nav {edge}: no trigger — say what the user "
                        f"does on the line below the tag")
    _apply_tags(rec, "navigation", item, problems)
    if host.get("kind") == "action":
        item["action"] = host["name"]
    out["navigation"].append(item)


def _derive_completeness(rec, out, counters, problems):
    """One @verdict or @internal record -> a verdicts[] / internal_vars[]
    entry, in source order. A verdict sits on the action it is about; it is
    numbered per action (1, 2, ...) in source order, which is what its
    generated check is named after (itf_tools.verdict_name)."""
    host = rec["host"] or {}
    desc = " ".join(rec["desc"]).strip()
    if rec["tag"] == "internal":
        reason = " ".join(x for x in (rec["id"], rec["extra"], desc) if x).strip()
        if host.get("kind") != "var":
            problems.append(f"{_where(rec)}: @internal sits on the `var` it marks "
                            f"as bookkeeping, not on a {host.get('kind') or 'module'}")
            return
        if not reason:
            problems.append(f"{_where(rec)}: @internal {host['name']}: say why no "
                            f"requirement has to explain its changes")
        out["internal_vars"].append({"var": host["name"], "reason": reason})
        return
    kind = rec["id"]
    if kind not in VERDICTS or rec["extra"]:
        problems.append(f"{_where(rec)}: @verdict takes one of "
                        f"{', '.join(VERDICTS)}, got "
                        f"{(kind + ' ' + rec['extra']).strip()!r}")
        return
    if host.get("kind") != "action":
        problems.append(f"{_where(rec)}: @verdict {kind} sits on the action it is "
                        f"about, not on a {host.get('kind') or 'module'}")
        return
    action = host["name"]
    counters[action] = counters.get(action, 0) + 1
    item = {"action": action, "index": counters[action], "verdict": kind}
    if desc:
        item["reason"] = desc
    _apply_tags(dict(rec, id=f"@verdict on {action}"), "verdicts", item, problems)
    out["verdicts"].append(item)


def derive_records(model_text, model_name, als_text=None, als_name=None,
                   problems=None):
    """The model-owned part of the area view: requirements, invariants,
    properties, constraints, examples — from doc comments alone."""
    problems = problems if problems is not None else []
    recs = read_records(model_text, "quint", model_name) if model_text else []
    if als_text:
        recs += read_records(als_text, "alloy", als_name)
    actions = set()
    for m in re.finditer(r"^\s*action\s+([A-Za-z_][A-Za-z0-9_]*)", model_text or "", re.M):
        actions.add(m.group(1))
    decls = declarations(model_text) if model_text else {}

    out = {k: [] for k in MODEL_KEYS + POSITIONAL_KEYS}
    by_id, origin, screens_seen, verdict_counts = {}, {}, {}, {}
    for rec in recs:
        if rec["tag"] in ("screen", "nav"):
            _derive_ui(rec, out, screens_seen, problems)
            continue
        if rec["tag"] in ("verdict", "internal"):
            _derive_completeness(rec, out, verdict_counts, problems)
            continue
        kind = ID_KIND[rec["tag"]]
        rid = rec["id"]
        if not rid:
            problems.append(f"{_where(rec)}: @{rec['tag']} without an id")
            continue
        if rid in by_id:
            problems.append(f"{_where(rec)}: duplicate id {rid} (first at {origin[rid]})")
            continue
        host = rec["host"] or {"kind": None, "name": None}
        item = {"id": rid}
        desc = " ".join(rec["desc"]).strip()
        _apply_tags(rec, kind, item, problems)
        hk, hn = host.get("kind"), host.get("name")
        alloy = bool(als_name) and rec["where"]["file"] == als_name

        if kind == "requirements":
            if desc:
                item["description"] = desc
            elif item.get("ears"):
                item["description"] = ears_sentence(item)
            # The host decides the requirement's FORM, and the form decides
            # what it obliges (itf_tools.obligation_name, spec-probes).
            if hk == "action":
                item["quint_ref"] = hn
                item["form"] = "event"
            elif hk in ("val", "def") and host.get("pure"):
                item["form"] = "computed"
            elif hk in (None, "module"):
                # Only a formal requirement has a form: a raw one's @shall
                # is still words, and an NFR is checked by its fit criterion.
                if (item.get("ears") or {}).get("response") \
                        and item.get("status", "raw") != "raw" \
                        and item.get("type") != "non-functional":
                    item["form"] = "state"
            else:
                problems.append(
                    f"{_where(rec)}: {rid} sits on a {hk} — a requirement sits on "
                    f"the action it governs, on the pure def it computes, or on "
                    f"the module (a state requirement, or not yet formal)")
        elif kind == "invariants":
            item["description"] = desc
            if alloy and hk in ("assert", "check"):
                item.setdefault("proof", "structural")
                item.setdefault("alloy_command", hn)
            elif item.get("over") == "probes" or (
                    hk == "def" and any(p.startswith("_prev") for p, _ in host.get("params") or [])):
                item.setdefault("over", "probes")
                if hk in ("val", "def") and host.get("body"):
                    item.setdefault("predicate", host["body"])
            elif hk == "val" and not item.get("quint_name"):
                item["quint_name"] = hn
        elif kind == "properties":
            item["description"] = desc
            if hk == "temporal" and not item.get("quint_name"):
                item["quint_name"] = hn
        elif kind == "constraints":
            if hk in ("val", "const") and hn:
                item["name"] = hn
                if "value" not in item:
                    v = _literal(host.get("body"))
                    if v is not None:
                        item["value"] = v
            if desc:
                item["description"] = desc
        elif kind == "examples":
            if desc:
                item["title"] = desc
            if hk == "run":
                item["quint_run"] = hn
                cond = _run_expectation(host.get("body"))
                if cond and not item.get("expect"):
                    item["expect"] = {RUN_EXPECT_KEY: cond}
                last = (run_calls(decls, hn) or [None])[-1] if hn in decls else None
                if last and last.get("fails") and not item.get("expect"):
                    # Ending in `A.fail()` IS the run's claim: quint test fails
                    # the run when the call goes through.
                    item["expect"] = {RUN_REFUSES_KEY: render_call(last)}
                if "when" not in item:
                    # The run's own last step, zero-argument actions
                    # (`.then(click_sign_in)`) included.
                    steps = run_calls(decls, hn) if hn in decls else []
                    a = steps[-1]["action"] if steps else _run_last_action(
                        host.get("body"), actions)
                    if a:
                        item["when"] = {"action": a}
            elif hk == "action" and "when" not in item:
                item["when"] = {"action": hn}
            item.setdefault("expect", {})
        by_id[rid] = item
        origin[rid] = _where(rec)
        out[kind].append(item)

    for kind in MODEL_KEYS:
        out[kind].sort(key=lambda it: _natural(it["id"]))
    # Origins travel separately (not in the view: the schema does not know them).
    return out, {"origin": origin, "records": recs, "actions": sorted(actions)}


def _merge_machine(area, records):
    by_id = (records or {}).get("by_id") or {}
    for kind in MODEL_KEYS:
        for item in area.get(kind) or []:
            m = by_id.get(item.get("id"))
            if not m:
                continue
            for path in MACHINE_PATHS[kind]:
                v = _get(m, path)
                if v is None:
                    continue
                _set(item, path, copy.deepcopy(v))
            if kind == "requirements":
                if m.get("status") == "verified":
                    item["status"] = "verified"
                for oc in (_get(item, "witness.outcomes") or []):
                    mo = ((_get(m, "witness.outcomes") or {}).get(oc.get("name"))) or {}
                    for f in WITNESS_MACHINE:
                        if f in mo:
                            oc[f] = copy.deepcopy(mo[f])


def compose_area(intent, model_part, records):
    """intent + model-derived records + ledger -> the area view."""
    area = {}
    for k, v in (intent or {}).items():
        if k == "$schema":
            continue
        area[k] = copy.deepcopy(v)
    for k in MODEL_KEYS + POSITIONAL_KEYS:
        if model_part.get(k):
            area[k] = copy.deepcopy(model_part[k])
    for k in RECORD_KEYS:
        if (records or {}).get(k) is not None:
            area[k] = copy.deepcopy(records[k])
    pin = ((records or {}).get("brief") or {}).get("written_against")
    if pin and isinstance(area.get("brief"), dict):
        area["brief"]["written_against"] = pin
    _merge_machine(area, records)
    return area


DECL_LINE = re.compile(
    r"^\s*(?:pure\s+)?(?:const|var|val|def|action|run|temporal|type|import|assume)\b")


def has_model(text):
    """True when a model file declares anything beyond its records. A module
    holding only `///` records is where a Tier-1 area keeps them before a
    model exists; it is not a formal model, and nothing may be checked
    against it as if it were one."""
    return any(DECL_LINE.match(l) for l in strip_docs(text or "").splitlines())


def _qualify_refs(model_part, name):
    """A bare `@refs REQ-030` means this area's REQ-030. Qualified here, once,
    so every tool downstream sees the one `<area>.<ID>` form and none of them
    has to know an unqualified ref is legal."""
    for kind in ("requirements", "invariants", "properties"):
        for item in model_part.get(kind) or []:
            refs = item.get("cross_refs")
            if refs:
                item["cross_refs"] = [r if "." in r else f"{name}.{r}" for r in refs]


def derive_from_texts(name, get_text, problems=None):
    """Derive the area view from file contents. `get_text(relpath)` returns a
    file under specs/ as a string, or None — so the same derivation runs on
    the working tree, the index, or any git revision."""
    problems = problems if problems is not None else []
    raw = get_text(f"{name}{INTENT_SUFFIX}")
    if raw is None:
        return None, {}
    try:
        intent = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SpecSourceError(f"specs/{name}{INTENT_SUFFIX}: invalid JSON: {e}")
    rraw = get_text(f"{name}{RECORDS_SUFFIX}")
    try:
        records = json.loads(rraw) if rraw else {}
    except json.JSONDecodeError as e:
        raise SpecSourceError(f"specs/{name}{RECORDS_SUFFIX}: invalid JSON: {e}")
    qfile = model_file(intent, name)
    qtext = get_text(qfile)
    afile = alloy_file(intent)
    atext = get_text(afile) if afile else None
    model_part, info = derive_records(qtext, qfile, atext, afile, problems)
    _qualify_refs(model_part, name)
    area = compose_area(intent, model_part, records)
    if has_model(qtext):
        area.setdefault("formal_model", {}).setdefault("quint_file", qfile)
    info.update({"intent": intent, "records_file": records, "model_file": qfile,
                 "model_present": has_model(qtext), "alloy_file": afile,
                 "problems": problems})
    return area, info


def _fs_getter(root):
    base = specs_dir(root)

    def get(rel):
        p = base / rel
        try:
            return p.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            return None
    return get


def git_getter(root, rev, prefix=""):
    """get(relpath) over specs/ at a git revision; rev None = the index."""
    import subprocess

    def get(rel):
        spec = f"{rev}:{prefix}specs/{rel}" if rev is not None else f":{prefix}specs/{rel}"
        proc = subprocess.run(["git", "show", spec], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
        return proc.stdout if proc.returncode == 0 else None
    return get


def areas_at(root, rev, prefix=""):
    """Area names (intent files) at a git revision."""
    import subprocess
    proc = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev, "--",
                           prefix + "specs/"], cwd=str(root),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return []
    return sorted(Path(ln).name[:-len(INTENT_SUFFIX)] for ln in proc.stdout.splitlines()
                  if ln.endswith(INTENT_SUFFIX) and "/" not in ln[len(prefix + "specs/"):])


def area_of_file(path):
    """Which area a spec-side file belongs to, by its name — or None."""
    name = Path(path).name
    for suffix in (INTENT_SUFFIX, RECORDS_SUFFIX, ".probes.qnt", ".qnt", ".als"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return None


def load_area(root, name, with_info=False):
    """The derived area view for specs/<name>.*, or None if the area has no
    intent file. Grammar problems are returned in info['problems'] (lint
    reports them); they never stop the derivation."""
    area, info = derive_from_texts(name, _fs_getter(root))
    return (area, info) if with_info else area


# ── Save (ledger only) ──────────────────────────────────────────────────────

def split_machine(area):
    """(authored view, records dict) — the inverse of compose_area's merge."""
    authored = copy.deepcopy(area)
    records = {}
    for k in RECORD_KEYS:
        if k in authored:
            records[k] = authored.pop(k)
    pin = (authored.get("brief") or {}).pop("written_against", None) \
        if isinstance(authored.get("brief"), dict) else None
    if pin:
        records["brief"] = {"written_against": pin}
    by_id = {}
    for kind in MODEL_KEYS:
        for item in authored.get(kind) or []:
            m = {}
            for path in MACHINE_PATHS[kind]:
                v = _get(item, path)
                if v is not None:
                    _set(m, path, v)
                    _del(item, path)
            if kind == "requirements":
                if item.get("obligation") == {}:
                    del item["obligation"]   # wholly machine-written
                if item.get("status") == "verified":
                    m["status"] = "verified"
                outs = {}
                for oc in (_get(item, "witness.outcomes") or []):
                    mo = {f: oc.pop(f) for f in WITNESS_MACHINE if f in oc}
                    if mo:
                        outs[oc.get("name")] = mo
                if outs:
                    m.setdefault("witness", {})["outcomes"] = outs
            if m:
                by_id[item["id"]] = m
    if by_id:
        records["by_id"] = dict(sorted(by_id.items(), key=lambda kv: _natural(kv[0])))
    return authored, records


def _diff_paths(a, b, prefix=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            out += _diff_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else str(k))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            key = (x.get("id") or x.get("name")) if isinstance(x, dict) else None
            out += _diff_paths(x, y, f"{prefix}[{key if key else i}]")
        return out
    return [] if a == b else [prefix]


def _canonical_authored(area):
    authored, _ = split_machine(area)
    a = copy.deepcopy(authored)
    for item in a.get("requirements") or []:
        # The recorder writes `skipped` for a forbidden requirement; the
        # authored view treats it as a verdict, not an edit.
        if _get(item, "witness.status") == "skipped":
            _del(item, "witness.status")
        if item.get("status") == "verified":
            item.pop("status")
    return a


def save_area(root, name, area):
    """Write the ledger part of `area` to specs/<name>.records.json.

    Refuses (SpecSourceError) when anything authored differs from what the
    files currently say — a tool that changes a requirement, an invariant's
    statement or the scope is writing a claim, and claims are edited in the
    .qnt and .intent.json where a reviewer sees them, never through a
    ledger."""
    current, _ = derive_from_texts(name, _fs_getter(root))
    if current is None:
        raise SpecSourceError(f"specs/{name}{INTENT_SUFFIX} not found")
    a, b = _canonical_authored(current), _canonical_authored(area)
    changed = _diff_paths(a, b)
    if changed:
        raise SpecSourceError(
            f"{name}: refusing to save — authored field(s) changed through the "
            f"ledger: {', '.join(changed[:8])}"
            + (" …" if len(changed) > 8 else "")
            + ". Edit them in the .qnt doc comments or the .intent.json.")
    _, records = split_machine(area)
    write_records(root, name, records)


def write_records(root, name, records):
    out = {"$schema": "../schemas/records.schema.json", "area": name}
    for k in ("brief",) + RECORD_KEYS + ("by_id",):
        if records.get(k) not in (None, {}, []) or (k in RECORD_KEYS and k in records):
            out[k] = records[k]
    path = records_path(root, name)
    text = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def write_intent(root, name, intent):
    path = intent_path(root, name)
    text = json.dumps(intent, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


# ── Model shapes the UI records are checked against ─────────────────────────

def type_variants(body):
    """Constructor names of a sum type's body (`| A | B(int)`), or [] for a
    body that is not a sum type."""
    if not body or "|" not in body:
        return []
    out = []
    for part in _split_top(body, "|"):
        m = re.match(r"\s*([A-Z][A-Za-z0-9_]*)", part)
        if m:
            out.append(m.group(1))
    return out


def screen_var(model_text, type_name):
    """The state variable typed by the screen type, or None."""
    m = re.search(rf"^\s*var\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*{re.escape(type_name)}\b",
                  strip_docs(model_text or ""), re.M)
    return m.group(1) if m else None


def nav_moves(body, var, frm, to):
    """True when an action body reads `var == frm` and sets `var' = to` — the
    shape a navigation edge has in the model.

    From `*` (every screen) has no guard on the screen to read: the action
    only has to set it — to `to`, or to a parameter (`navigate(target:
    Screen)` behind a sidebar, where the destination is the argument)."""
    if not body or not var:
        return False
    v = re.escape(var)
    if frm == ANY_SCREEN:
        return bool(re.search(rf"\b{v}'\s*=\s*(?:{re.escape(to)}\b|(?!{v}\b)[a-z_][A-Za-z0-9_]*\s*(?:[,}}\n)]|$))",
                              body))
    return bool(re.search(rf"\b{v}\s*==\s*{re.escape(frm)}\b", body)
                and re.search(rf"\b{v}'\s*=\s*{re.escape(to)}\b", body))


# ── Schemas (generated from the area-view schema, never hand-edited) ───────

def source_schemas(area_schema):
    """(intent schema, records schema) carved out of schemas/area.schema.json
    along the ownership table, so a field's definition exists exactly once
    and the three schemas cannot disagree about it."""
    props = area_schema["properties"]
    defs = area_schema.get("$defs") or {}

    intent = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "intent.schema.json",
        "title": "Spec Area Intent (specs/<name>.intent.json)",
        "description": (
            "GENERATED by `tools/spec_source.py schemas --write` from "
            "area.schema.json — edit that file, then regenerate. The part of an "
            "area a model cannot say: kind, purpose, brief, scope, boundary, "
            "vocabulary, externals, assumptions, decisions, open questions, state "
            "machines, architecture, configuration. Requirements, invariants, "
            "properties, constants and examples are NOT here: they are `///` doc "
            "comments on the declarations in specs/<name>.qnt that realize them. "
            "Ledgers (verdicts, pins, logs, traceability, triage) are in "
            "specs/<name>.records.json."),
        "type": "object",
        "required": area_schema.get("required", []),
        "additionalProperties": False,
        "allOf": copy.deepcopy(area_schema.get("allOf", [])),
        "properties": {},
    }
    for k, v in props.items():
        if k in MODEL_KEYS or k in POSITIONAL_KEYS or k in RECORD_KEYS:
            continue
        v = copy.deepcopy(v)
        if k == "brief":
            v["properties"].pop("written_against", None)
        if k == "$schema":
            v = {"type": "string"}
        intent["properties"][k] = v
    if defs:
        intent["$defs"] = copy.deepcopy(defs)

    req = props["requirements"]["items"]["properties"]
    w = req["witness"]["properties"]
    oc = w["outcomes"]["items"]["properties"]
    witness_rec = {"type": "object", "additionalProperties": False,
                   "properties": {f: copy.deepcopy(w[f]) for f in WITNESS_MACHINE}}
    witness_rec["properties"]["outcomes"] = {
        "type": "object",
        "description": "Per permitted outcome of a `may` requirement, keyed by outcome name.",
        "additionalProperties": {"type": "object", "additionalProperties": False,
                                 "properties": {f: copy.deepcopy(oc[f]) for f in WITNESS_MACHINE}}}
    by_id_entry = {
        "type": "object", "additionalProperties": False,
        "description": "Machine-written fields of one record (requirement, invariant, property, constant), keyed by its id.",
        "properties": {
            "status": {"type": "string", "enum": ["verified"],
                       "description": "Written by `spec-record verify` on a green replay; overrides the authored @status."},
            "witness": witness_rec,
            "obligation": copy.deepcopy(req["obligation"]),
            "formal_status": {"type": "string", "enum": sorted(set(
                props["invariants"]["items"]["properties"]["formal_status"]["enum"]) | set(
                props["properties"]["items"]["properties"]["formal_status"]["enum"]))},
        },
    }
    records = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "records.schema.json",
        "title": "Spec Area Records (specs/<name>.records.json)",
        "description": (
            "GENERATED by `tools/spec_source.py schemas --write` from "
            "area.schema.json. The ledger of an area: what the tools recorded "
            "(check results, witness verdicts, freshness pins, "
            "verification log, traceability, provenance) and the three triage "
            "ledgers. Written through spec_source.save_area(), which refuses to "
            "change anything authored."),
        "type": "object",
        "required": ["area"],
        "additionalProperties": False,
        "properties": {
            "$schema": {"type": "string"},
            "area": {"type": "string"},
            "brief": {"type": "object", "additionalProperties": False,
                      "properties": {"written_against": copy.deepcopy(
                          props["brief"]["properties"]["written_against"])}},
            **{k: copy.deepcopy(props[k]) for k in RECORD_KEYS},
            "by_id": {"type": "object",
                      "propertyNames": {"pattern": "^[A-Z]+(-[A-Z]+)*-\\d{3}$"},
                      "additionalProperties": by_id_entry},
        },
    }
    if defs:
        records["$defs"] = copy.deepcopy(defs)
    return intent, records


def _cmd_schemas(args):
    base = Path(args.root) / "schemas"
    area_schema = json.loads((base / "area.schema.json").read_text(encoding="utf-8"))
    stale = []
    for name, schema in zip(("intent.schema.json", "records.schema.json"),
                            source_schemas(area_schema)):
        text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        path = base / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(name)
            if args.write:
                path.write_text(text, encoding="utf-8")
    if stale and not args.write:
        print(f"stale: {', '.join(stale)} — run `tools/spec_source.py schemas --write`")
        return 1
    print("schemas " + ("written: " + ", ".join(stale) if stale else "up to date"))
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cmd_derive(args):
    area, info = load_area(args.root, args.area, with_info=True)
    if area is None:
        sys.exit(f"ERROR: specs/{args.area}{INTENT_SUFFIX} not found")
    print(json.dumps(area, indent=2, ensure_ascii=False))
    for p in info["problems"]:
        print(f"problem: {p}", file=sys.stderr)
    return 1 if info["problems"] else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("derive")
    sp.add_argument("area")
    sp.add_argument("--root", default=".")
    sp = sub.add_parser("schemas", help="(re)generate intent/records schemas")
    sp.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    sp.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    return {"derive": _cmd_derive, "schemas": _cmd_schemas}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
