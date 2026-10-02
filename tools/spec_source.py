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
                              witness and refusal verdicts, freshness pins,
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
                     @outcome-of REQ-ID <name>   (one permitted outcome of a
                                                 `may` requirement)
                     @screen <Name>              (one screen of a UI)
                     @nav <From> -> <To>         (one navigation edge)

Untagged lines right after the record tag are the record's DESCRIPTION
(invariant statement, constant description, example title, a requirement's
free text). Untagged lines after an attribute tag CONTINUE that tag's value.
A blank `///` line ends a continuation.

  REQ   @while @when @where @unwanted @shall     ears.state/trigger/feature/
                                                 unwanted/response
        @meaning <text>  @meaning-author <who>   meaning.text / .author
        @type @status @modality @determinism @source
        @fit <metric> | <target> | <measurement>
        @error <External>.<OUTCOME> [idempotent|not-idempotent]: <effect>
        @error-note <text>                       note on the @error above
        @via <action>                            quint_ref — the action the
                                                 witness must arrive through
        @pre <expr>  @pre-note <text>            witness.delta (over the
                                                 probe module's _prev*/_last*
                                                 ghosts, so it is text: those
                                                 ghosts exist only there)
        @enforced-by INV-ID                      witness.enforced_by
        @witness skipped[: <reason>]             a deliberately skipped
                                                 witness (+ justification)
        @justification <text>                    justification, no skip
        @refusal <path>  @blocking <expr>  @unchanged a, b
        @refs ID, ID                             cross_refs
        @evidence @confidence @inferred-by @fingerprint   extraction.*
  INV   @criticality @proof @over @alloy <command> @smt <file>
        @quint-name <name> @predicate <expr> @refs  + extraction tags
  PROP  (host: a `temporal`)  @refs
  CON   @unit @pairs INV-ID @value <json>  + extraction tags
  EX    @given <json> @when <action> [<json args>] @expect <json>
        @refs @source @trace
  SCREEN  description = purpose  @auth-required  @components A, B
  NAV     description = the trigger, in the user's words  @guard <text>

Hosting — what the declaration under the block means for the record:

  REQ on `val|def <name>` + @via   witnessed: the declaration's BODY is the
                                   witness predicate. Parameters named like
                                   probe ghosts (`_lastSid: SessionId`) make
                                   it typecheck in the model while the probe
                                   generator binds them; the name is free,
                                   `shall_REQ_NNN` by convention (the probe
                                   module already owns `witness_REQ_NNN`).
  REQ on `action <name>`           quint_ref = the action (unless @via);
                                   no predicate — a prohibition, a may with
                                   outcomes, or not yet formalized.
  REQ on the `module` / elsewhere  no host in the model yet (raw, NFR,
                                   read path realized by a pure def).
  @outcome-of on `val|def`         the body is that outcome's predicate.
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
                                   set it to <To> (lint). Several edges may
                                   share an action.
  SCREEN / NAV on the `module`     a UI with no model yet (Tier 1).

Usage:
  tools/spec_source.py derive <area> [--root .]        # print the derived view
  tools/spec_source.py migrate <area> [--root .] [--write]
                        # JSON-first specs/<area>.area.json (or .contract.json)
                        # -> .qnt doc comments + .intent.json + .records.json
  tools/spec_source.py roundtrip <area> [--root .]     # migrate in memory,
                        # derive back, diff against the legacy JSON
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
LEGACY_SUFFIXES = (".area.json", ".contract.json")

# ── Ownership: which file holds which field ─────────────────────────────────
# ONE table. derive/save/migrate, spec-separation (claims vs bookkeeping) and
# the schemas are all read off it, so they cannot disagree about where a
# field lives.

MODEL_KEYS = ("requirements", "invariants", "properties", "constraints",
              "examples")
# An interactive surface's screens and navigation edges are model-owned too:
# a screen is a variant of the model's screen type, an edge is the action
# that moves between two of them. They are keyed by name / position, not by
# id, so they ride beside MODEL_KEYS rather than in it.
UI_KEYS = ("screens", "navigation")
RECORD_KEYS = ("check_results", "verification_log", "traceability",
               "generated_from", "extracted_from", "extraction_triage",
               "matrix_triage", "outcome_triage")
# Everything else at the top level is intent.

# Per-ID fields the tools write. Paths are dotted; `witness.outcomes[]`
# entries are keyed by outcome name.
WITNESS_MACHINE = ("status", "trace", "checked_at", "steps", "model_sha")
MACHINE_PATHS = {
    "requirements": ("witness.status", "witness.trace", "witness.checked_at",
                     "witness.steps", "witness.model_sha",
                     "refusal.status", "refusal.checked_at",
                     "meaning.written_against"),
    "invariants": ("formal_status",),
    "properties": ("formal_status",),
    "constraints": (),
    "examples": (),
}

ID_KIND = {"req": "requirements", "inv": "invariants", "prop": "properties",
           "con": "constraints", "example": "examples"}
RECORD_TAGS = tuple(ID_KIND) + ("outcome-of", "screen", "nav")

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
        "meaning": ("meaning.text", "text"),
        "meaning-author": ("meaning.author", "text"),
        "type": ("type", "text"),
        "status": ("status", "text"),
        "modality": ("modality", "text"),
        "determinism": ("determinism", "text"),
        "source": ("source", "text"),
        "via": ("quint_ref", "text"),
        "predicate": ("witness.predicate", "text"),
        "pre": ("witness.delta.pre", "text"),
        "pre-note": ("witness.delta.note", "text"),
        "enforced-by": ("witness.enforced_by", "text"),
        "justification": ("witness.justification", "text"),
        "refusal": ("refusal.artifact", "text"),
        "blocking": ("refusal.blocking_state", "text"),
        "unchanged": ("refusal.unchanged", "list"),
        "refs": ("cross_refs", "list"),
        **EXTRACTION_TAGS,
        # special: fit, error, error-note, witness
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
    "outcome": {
        "pre": ("delta.pre", "text"),
        "pre-note": ("delta.note", "text"),
    },
    "screens": {
        "auth-required": ("auth_required", "flag"),
        "components": ("components", "list"),
    },
    "navigation": {
        "guard": ("guard", "text"),
    },
}
SPECIAL_TAGS = {"requirements": ("fit", "error", "error-note", "witness"),
                "examples": ("when",)}
# The order migrate emits tags in (and so the order a reader meets them).
EMIT_ORDER = {
    "requirements": ("type", "status", "modality", "determinism", "source",
                     "where", "while", "when", "unwanted", "shall",
                     "meaning", "meaning-author", "fit", "error", "via",
                     "predicate", "pre", "pre-note", "enforced-by", "witness",
                     "justification", "refusal", "blocking", "unchanged",
                     "refs", "evidence", "confidence", "inferred-by",
                     "fingerprint"),
    "invariants": ("criticality", "proof", "over", "quint-name", "predicate",
                   "alloy", "smt", "refs", "evidence", "confidence",
                   "inferred-by", "fingerprint"),
    "properties": ("quint-name", "refs"),
    "constraints": ("name", "unit", "pairs", "value", "evidence", "confidence",
                    "inferred-by", "fingerprint"),
    "examples": ("given", "when", "expect", "refs", "source", "trace"),
    "outcome": ("pre", "pre-note"),
    "screens": ("auth-required", "components"),
    "navigation": ("guard",),
}


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


def legacy_path(root, name):
    for suffix in LEGACY_SUFFIXES:
        p = specs_dir(root) / f"{name}{suffix}"
        if p.exists():
            return p
    return None


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
    hashes. Editing a meaning or an EARS field changes what a reviewer reads,
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
    after = code[code.find(host["name"]) + len(host["name"]):] if host["name"] else ""
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


# ── Derive ──────────────────────────────────────────────────────────────────

def ears_sentence(req):
    """The EARS sentence, rendered from the fields (the source of truth);
    the description for an unstructured requirement. ONE renderer: the
    readback prints it, derive fills `description` with it, migrate drops a
    description that only restated it."""
    ears = req.get("ears")
    if not ears:
        return req.get("description") or "(no description)"
    parts = []
    if ears.get("feature"):
        parts.append(f"Where {ears['feature']}, ")
    if ears.get("state"):
        parts.append(f"While {ears['state']}, ")
    if ears.get("trigger"):
        joiner = "if" if ears.get("unwanted") else "when"
        parts.append(f"{joiner} {ears['trigger']}, ")
    shall = "then the system shall" if ears.get("unwanted") else "the system shall"
    parts.append(f"{shall} {ears.get('response', '…')}.")
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
            elif name == "witness":
                m = re.match(r"^skipped(?:\s*:\s*(.*))?$", value)
                if not m:
                    problems.append(f"{_where(rec)}: {rec['id']}: @witness takes "
                                    f"'skipped[: reason]'")
                    continue
                _set(item, "witness.status", "skipped")
                if m.group(1):
                    _set(item, "witness.justification", m.group(1).strip())
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


def _natural(rid):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", rid or "")]


NAV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*->\s*([A-Za-z_][A-Za-z0-9_]*)$")
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
        problems.append(f"{_where(rec)}: @nav takes '<From> -> <To>', got {edge!r}")
        return
    item = {"from": m.group(1), "to": m.group(2)}
    if desc:
        item["trigger"] = desc
    else:
        problems.append(f"{_where(rec)}: @nav {edge}: no trigger — say what the user "
                        f"does on the line below the tag")
    _apply_tags(rec, "navigation", item, problems)
    if host.get("kind") == "action":
        item["action"] = host["name"]
    out["navigation"].append(item)


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

    out = {k: [] for k in MODEL_KEYS + UI_KEYS}
    by_id, origin, outcomes, screens_seen = {}, {}, [], {}
    for rec in recs:
        if rec["tag"] == "outcome-of":
            outcomes.append(rec)
            continue
        if rec["tag"] in ("screen", "nav"):
            _derive_ui(rec, out, screens_seen, problems)
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
            if hk in ("val", "def") and item.get("quint_ref"):
                if host.get("body"):
                    _set(item, "witness.predicate", host["body"])
            elif hk == "action" and not item.get("quint_ref"):
                item["quint_ref"] = hn
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
                if "when" not in item:
                    a = _run_last_action(host.get("body"), actions)
                    if a:
                        item["when"] = {"action": a}
            elif hk == "action" and "when" not in item:
                item["when"] = {"action": hn}
            item.setdefault("expect", {})
        by_id[rid] = item
        origin[rid] = _where(rec)
        out[kind].append(item)

    for rec in outcomes:
        rid, name = rec["id"], rec["extra"]
        req = by_id.get(rid)
        if req is None or req not in out["requirements"]:
            problems.append(f"{_where(rec)}: @outcome-of {rid}: no such requirement")
            continue
        host = rec["host"] or {}
        oc = {"name": name}
        if host.get("kind") in ("val", "def") and host.get("body"):
            oc["predicate"] = host["body"]
        else:
            problems.append(f"{_where(rec)}: @outcome-of {rid} {name}: host must be "
                            f"a val/def whose body is the outcome's predicate")
        for tname, value in rec["tags"]:
            if tname in TAGS["outcome"]:
                _set(oc, TAGS["outcome"][tname][0], value)
            else:
                problems.append(f"{_where(rec)}: @outcome-of {rid}: unknown tag @{tname}")
        req.setdefault("witness", {}).setdefault("outcomes", []).append(oc)

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
                if path == "witness.status" and _get(item, "witness.status") == "skipped":
                    continue  # an authored skip is the author's call
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
    for k in MODEL_KEYS + UI_KEYS:
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
                if path == "witness.status" and v == "skipped":
                    continue  # stays authored when the tag set it; see save
                if v is not None:
                    _set(m, path, v)
                    _del(item, path)
            if kind == "requirements":
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


# ── Migrate (JSON-first area -> Quint-first files) ──────────────────────────

WRAP = 76


def _wrap(prefix, text, indent, cont="///   "):
    """`/// @tag value` lines, wrapped when wrapping is lossless."""
    text = str(text)
    if "\n" in text:
        raise SpecSourceError(f"cannot write a multi-line value into a doc tag: {text[:40]!r}")
    head = f"{indent}/// {prefix}"
    lossless = text == " ".join(text.split())
    if not lossless or len(head) + len(text) + 1 <= WRAP + len(indent):
        return [f"{head} {text}".rstrip()]
    words, lines, cur = text.split(), [], head
    for w in words:
        if len(cur) + 1 + len(w) > WRAP + len(indent) and cur != head:
            lines.append(cur)
            cur = f"{indent}{cont}{w}"
        else:
            cur = f"{cur} {w}"
    lines.append(cur)
    return lines


def _wrap_desc(text, indent):
    if not text:
        return []
    return [l.replace("///  ", "/// ", 1) if i == 0 else l
            for i, l in enumerate(_wrap("", text, indent, cont="/// "))]


def _emit_value(kind, tag, item):
    """Tag value(s) for one tag from an item, or [] when absent."""
    if kind in TAGS and tag in TAGS[kind]:
        path, vk = TAGS[kind][tag]
        v = _get(item, path)
        if v is None:
            return []
        if vk == "flag":
            return [""] if v else []
        if vk == "list":
            return [", ".join(v)] if v else []
        if vk == "json":
            return [json.dumps(v, ensure_ascii=False)]
        return [str(v)]
    if tag == "fit":
        f = item.get("fit_criterion")
        return [f"{f['metric']} | {f['target']} | {f['measurement']}"] if f else []
    if tag == "error":
        out = []
        for eo in item.get("error_outcomes") or []:
            flag = {True: " idempotent", False: " not-idempotent"}.get(eo.get("idempotent"), "")
            out.append((f"{eo['external']}.{eo['outcome']}{flag}: {eo['effect']}",
                        eo.get("note")))
        return out
    if tag == "witness":
        w = item.get("witness") or {}
        if w.get("status") == "skipped":
            return [f"skipped: {w['justification']}" if w.get("justification") else "skipped"]
        return []
    if tag == "when":
        w = item.get("when") or {}
        if not w:
            return []
        args = f" {json.dumps(w['args'], ensure_ascii=False)}" if w.get("args") else ""
        return [f"{w['action']}{args}"]
    return []


def emit_doc(tag, rid, item, kind, indent="  ", extra="", skip=()):
    lines = [f"{indent}/// @{tag} {rid}{(' ' + extra) if extra else ''}"]
    desc_key = {"requirements": "description", "examples": "title"}.get(kind, "description")
    desc = item.get(desc_key)
    if kind == "requirements" and item.get("ears") and desc == ears_sentence(
            {k: v for k, v in item.items() if k != "description"}):
        desc = None  # derive renders it back from the EARS tags
    if desc:
        lines += _wrap_desc(desc, indent)
    for t in EMIT_ORDER[kind]:
        if t in skip:
            continue
        for v in _emit_value(kind, t, item):
            if t == "error":
                v, note = v
                lines += _wrap(f"@{t}", v, indent)
                if note:
                    lines += _wrap("@error-note", note, indent)
                continue
            if t == "justification" and _get(item, "witness.status") == "skipped":
                continue  # carried by @witness skipped: <reason>
            lines += _wrap(f"@{t}", v, indent) if v != "" else [f"{indent}/// @{t}"]
    return lines


def _covered_paths(kind):
    paths = {p for p, _ in TAGS[kind].values()}
    paths |= set(MACHINE_PATHS[kind])
    paths |= {"id"}
    extra = {
        "requirements": {"description", "fit_criterion", "error_outcomes",
                         "witness.status", "witness.justification",
                         "witness.predicate", "witness.outcomes", "status"},
        "invariants": {"description", "quint_name", "alloy_command"},
        "properties": {"description", "quint_name"},
        "constraints": {"name", "value", "description"},
        "examples": {"title", "when", "quint_run"},
    }[kind]
    return paths | extra


def _leaf_paths(d, prefix=""):
    out = []
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v and k not in ("given", "expect", "args", "when", "fit_criterion"):
            out += _leaf_paths(v, p)
        else:
            out.append(p)
    return out


def _ghost_types(ir):
    """Ghost name -> Quint type string, for typing a witness host's params."""
    from itf_tools import ghost_for_param, ghost_for_var  # noqa: WPS433
    types = {"_lastAction": "str"}
    for _act, params in (ir.get("action_param_types") or {}).items():
        for pname, ptype in params:
            if ptype:
                types.setdefault(ghost_for_param(pname), ptype)
    for var, t in (ir.get("var_type_strs") or {}).items():
        types.setdefault(ghost_for_var(var), t)
    return types


def _witness_host(name, predicate, ghost_types, indent):
    ghosts = sorted(set(re.findall(r"\b(_(?:last|prev)[A-Z][A-Za-z0-9_]*)\b", predicate)))
    if ghosts:
        # Typed where the model says what the ghost stands for; left to
        # Quint's inference where no action parameter or var does (lint's
        # witness-unbound reports that case on its own terms).
        params = ", ".join(f"{g}: {ghost_types[g]}" if g in ghost_types else g
                           for g in ghosts)
        head = f"{indent}def {name}({params}): bool ="
    else:
        head = f"{indent}val {name}: bool ="
    return [head, f"{indent}  {predicate}"]


def host_name(rid, suffix=None):
    base = "shall_" + re.sub(r"[^A-Za-z0-9]+", "_", rid)
    if suffix:
        base += "_" + (re.sub(r"[^A-Za-z0-9]+", "_", suffix).strip("_") or "outcome")
    return base


def _open_module(lines):
    """Put the module header and its closing brace on lines of their own
    (`module a { var x: int }` -> three lines), so records can be inserted
    above the header and declarations appended before the brace."""
    text = "\n".join(lines)
    m = re.search(r"^(\s*module\s+\w+\s*\{)(.*)$", text, re.M)
    if m and m.group(2).strip():
        text = text[:m.start()] + m.group(1) + "\n  " + m.group(2).strip() + text[m.end():]
    end = text.rstrip().rfind("}")
    if end >= 0:
        before = text[:end]
        last_nl = before.rfind("\n")
        if before[last_nl + 1:].strip():
            text = before.rstrip() + "\n}" + text[end + 1:]
    return text.splitlines()


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
    shape a navigation edge has in the model."""
    if not body or not var:
        return False
    v = re.escape(var)
    return bool(re.search(rf"\b{v}\s*==\s*{re.escape(frm)}\b", body)
                and re.search(rf"\b{v}'\s*=\s*{re.escape(to)}\b", body))


SCREEN_FIELDS = {"name", "auth_required", "purpose", "components"}
NAV_FIELDS = {"from", "to", "trigger", "guard", "action"}


def emit_ui(tag, item, indent):
    if tag == "screen":
        lines = [f"{indent}/// @screen {item['name']}"]
        lines += _wrap_desc(item.get("purpose"), indent)
        if item.get("auth_required"):
            lines.append(f"{indent}/// @auth-required")
        if item.get("components"):
            lines += _wrap("@components", ", ".join(item["components"]), indent)
        return lines
    lines = [f"{indent}/// @nav {item['from']} -> {item['to']}"]
    lines += _wrap_desc(item.get("trigger"), indent)
    if item.get("guard"):
        lines += _wrap("@guard", item["guard"], indent)
    return lines


def migrate_area(legacy, model_text, als_text=None, ir=None):
    """Legacy area JSON + its model text(s) -> (qnt text, als text, intent,
    records). Lossless: a field it cannot place raises SpecSourceError."""
    name = legacy.get("area")
    ir = ir or {}
    intent = {"$schema": "../schemas/intent.schema.json"}
    for k, v in legacy.items():
        if k in MODEL_KEYS or k in UI_KEYS or k in RECORD_KEYS or k == "$schema":
            continue
        intent[k] = copy.deepcopy(v)
    if isinstance(intent.get("brief"), dict):
        intent["brief"].pop("written_against", None)
    fm = intent.get("formal_model") or {}
    qfile = fm.get("quint_file") or f"{name}.qnt"
    _authored, records = split_machine(legacy)

    # Check every field has a home before writing anything.
    for kind in MODEL_KEYS:
        covered = _covered_paths(kind)
        for item in legacy.get(kind) or []:
            for p in _leaf_paths(item):
                if p not in covered and not any(p.startswith(c + ".") for c in covered):
                    if kind == "requirements" and p.startswith("witness.outcomes"):
                        continue
                    raise SpecSourceError(f"{name}: {item.get('id')}: field '{p}' has "
                                          f"no doc-comment tag — cannot migrate losslessly")

    lines = (model_text or "").splitlines()
    if model_text is None:
        lines = [f"module {re.sub(r'[^A-Za-z0-9_]', '_', name)} {{", "}"]
    lines = _open_module(lines)
    inserts = {}       # line index -> doc lines to insert above it
    appended = []      # declarations to add before the module's closing brace
    mod_line = next((i for i, l in enumerate(lines) if re.match(r"^\s*module\s+\w+", l)), None)
    if mod_line is None:
        raise SpecSourceError(f"{name}: {qfile} has no module declaration")
    close_line = max(i for i, l in enumerate(lines) if l.strip() == "}")

    def decl_line(kind, decl_name):
        if not decl_name:
            return None
        pat = re.compile(rf"^(\s*)(?:pure\s+)?{kind}\s+{re.escape(decl_name)}\b")
        for i, l in enumerate(lines):
            if pat.match(l):
                return i
        return None

    def indent_of(i):
        return re.match(r"^(\s*)", lines[i]).group(1)

    def put(i, doc):
        inserts.setdefault(i, []).extend(doc)

    module_doc = []
    gt = _ghost_types(ir) if ir else {}
    reqs = legacy.get("requirements") or []
    for r in reqs:
        rid = r["id"]
        w = r.get("witness") or {}
        outcomes = w.get("outcomes") or []
        pred = w.get("predicate")
        act = decl_line("action", r.get("quint_ref"))
        if pred and not outcomes and model_text is None:
            # No model to host it in yet (Tier 1): the predicate rides as text.
            module_doc.append(emit_doc("req", rid, r, "requirements", ""))
        elif pred and not outcomes:
            hn = host_name(rid)
            appended.append(emit_doc("req", rid, r, "requirements", "  ",
                                     skip=("predicate",)))
            appended[-1] += _witness_host(hn, pred, gt, "  ")
        elif act is not None:
            put(act, emit_doc("req", rid, r, "requirements", indent_of(act),
                              skip=("via", "predicate")))
        else:
            module_doc.append(emit_doc("req", rid, r, "requirements", ""))
        for oc in outcomes:
            if not oc.get("predicate"):
                raise SpecSourceError(f"{name}: {rid} outcome {oc.get('name')}: no predicate")
            doc = [f"  /// @outcome-of {rid} {oc['name']}"]
            if _get(oc, "delta.pre"):
                doc += _wrap("@pre", oc["delta"]["pre"], "  ")
            if _get(oc, "delta.note"):
                doc += _wrap("@pre-note", oc["delta"]["note"], "  ")
            appended.append(doc + _witness_host(host_name(rid, oc["name"]),
                                                oc["predicate"], gt, "  "))
        if pred and outcomes:
            raise SpecSourceError(f"{name}: {rid}: both a predicate and outcomes")

    for inv in legacy.get("invariants") or []:
        iid = inv["id"]
        if inv.get("proof") == "structural":
            if als_text is None:
                raise SpecSourceError(f"{name}: {iid}: structural invariant but no .als")
            continue  # handled below on the Alloy text
        if inv.get("over") == "probes":
            if inv.get("predicate") and model_text is not None:
                gname = (inv.get("quint_name") or iid.replace("-", "_")) + "_over"
                doc = emit_doc("inv", iid, inv, "invariants", "  ", skip=("predicate",))
                appended.append(doc + _witness_host(gname, inv["predicate"], gt, "  "))
            else:
                module_doc.append(emit_doc("inv", iid, inv, "invariants", ""))
            continue
        line = decl_line("val", inv.get("quint_name"))
        if line is not None:
            put(line, emit_doc("inv", iid, inv, "invariants", indent_of(line),
                               skip=("quint-name",)))
        else:
            module_doc.append(emit_doc("inv", iid, inv, "invariants", ""))

    for p in legacy.get("properties") or []:
        line = decl_line("temporal", p.get("quint_name"))
        if line is not None:
            put(line, emit_doc("prop", p["id"], p, "properties", indent_of(line),
                               skip=("quint-name",)))
        else:
            module_doc.append(emit_doc("prop", p["id"], p, "properties", ""))

    for c in legacy.get("constraints") or []:
        line = decl_line("val", c.get("name"))
        if line is None:
            line = decl_line("const", c.get("name"))
        doc_item = dict(c)
        v = c.get("value")
        if line is not None:
            body = _literal((_parse_host(lines, line, "quint") or {}).get("body"))
            if body == v and type(body) is type(v):
                doc_item.pop("value")
            put(line, emit_doc("con", c["id"], doc_item, "constraints", indent_of(line),
                               skip=("name",)))
        elif model_text is not None and isinstance(v, (int, bool, str)):
            lit = json.dumps(v) if isinstance(v, str) else (str(v).lower() if isinstance(v, bool) else str(v))
            doc_item.pop("value")
            appended.append(emit_doc("con", c["id"], doc_item, "constraints", "  ",
                                     skip=("name",))
                            + [f"  pure val {c['name']} = {lit}"])
        else:
            module_doc.append(emit_doc("con", c["id"], c, "constraints", ""))

    for ex in legacy.get("examples") or []:
        line = decl_line("run", ex.get("quint_run"))
        doc_item = dict(ex)
        if line is not None:
            put(line, emit_doc("example", ex["id"], doc_item, "examples", indent_of(line)))
            continue
        act = decl_line("action", (ex.get("when") or {}).get("action"))
        if act is not None and not (ex.get("when") or {}).get("args"):
            doc_item = {k: v for k, v in ex.items() if k != "when"}
            put(act, emit_doc("example", ex["id"], doc_item, "examples", indent_of(act)))
        else:
            module_doc.append(emit_doc("example", ex["id"], ex, "examples", ""))

    # Screens on the sum type that lists them; each edge on the one action
    # that moves the screen var between its two ends.
    screens = legacy.get("screens") or []
    navs = legacy.get("navigation") or []
    for sc in screens:
        if set(sc) - SCREEN_FIELDS:
            raise SpecSourceError(f"{name}: screen {sc.get('name')}: field(s) "
                                  f"{sorted(set(sc) - SCREEN_FIELDS)} have no doc-comment tag")
    for nv in navs:
        if set(nv) - NAV_FIELDS:
            raise SpecSourceError(f"{name}: navigation {nv.get('from')} -> {nv.get('to')}: "
                                  f"field(s) {sorted(set(nv) - NAV_FIELDS)} have no doc-comment tag")
    types, actions = {}, {}
    for i, l in enumerate(lines):
        m = re.match(r"^\s*(type|action)\s+([A-Za-z_][A-Za-z0-9_]*)", l)
        if m:
            body = (_parse_host(lines, i, "quint") or {}).get("body") or ""
            if m.group(1) == "type":
                types[m.group(2)] = (i, type_variants(body))
            else:
                actions[m.group(2)] = (i, body)
    names = {sc["name"] for sc in screens}
    stype = next((t for t, (_i, v) in types.items() if names and names <= set(v)), None)
    if stype:
        i = types[stype][0]
        doc = []
        for sc in screens:
            if doc:
                doc.append(f"{indent_of(i)}///")
            doc += emit_ui("screen", sc, indent_of(i))
        put(i, doc)
    else:
        module_doc.extend(emit_ui("screen", sc, "") for sc in screens)
    svar = screen_var(model_text, stype) if stype else None
    for nv in navs:
        target = nv.get("action")
        if target is not None and target not in actions:
            raise SpecSourceError(f"{name}: navigation {nv['from']} -> {nv['to']}: "
                                  f"no action `{target}` in the model")
        if target is None:
            cands = [a for a, (_i, b) in actions.items()
                     if nav_moves(b, svar, nv["from"], nv["to"])]
            target = cands[0] if len(cands) == 1 else None
        if target is None:
            module_doc.append(emit_ui("nav", nv, ""))
            continue
        i = actions[target][0]
        if i in inserts:
            inserts[i].append(f"{indent_of(i)}///")
        put(i, emit_ui("nav", nv, indent_of(i)))

    # Module doc: records with no host in the model yet.
    if module_doc:
        flat = []
        for block in module_doc:
            if flat:
                flat.append("///")
            flat += block
        put(mod_line, flat)

    out = []
    for i, l in enumerate(lines):
        if i == close_line and appended:
            if out and out[-1].strip():
                out.append("")
            out.append("  // ----------------------------------------------------------")
            out.append("  // REQUIREMENTS — witness predicates, constants and transition")
            out.append("  // properties whose records live on them (spec_source.py)")
            out.append("  // ----------------------------------------------------------")
            for block in appended:
                out.append("")
                out += block
        if i in inserts:
            existing_doc = bool(out) and DOC_RE.match(out[-1] or "")
            if existing_doc:
                out.append(f"{indent_of(i)}///")
            out += inserts[i]
        out.append(l)
    qnt_out = "\n".join(out) + "\n"
    if model_text is None and not module_doc and not appended:
        qnt_out = None  # nothing lives in a model this area does not have

    als_out = als_text
    if als_text is not None:
        alines = als_text.splitlines()
        ains = {}
        for inv in legacy.get("invariants") or []:
            if inv.get("proof") != "structural":
                continue
            target = inv.get("alloy_command")
            idx = next((i for kw in ("assert", "check") for i, l in enumerate(alines)
                        if re.match(rf"^\s*{kw}\s+{re.escape(target or '')}\b", l)), None)
            if idx is None:
                raise SpecSourceError(f"{name}: {inv['id']}: no `assert {target}` "
                                      f"(or `check {target}`) in the .als")
            ains[idx] = emit_doc("inv", inv["id"], inv, "invariants", "",
                                 skip=("proof", "alloy"))
        res = []
        for i, l in enumerate(alines):
            if i in ains:
                res += ains[i]
            res.append(l)
        als_out = "\n".join(res) + "\n"
    return qnt_out, als_out, intent, records


def _drop_empty(v):
    if isinstance(v, dict):
        return {k: _drop_empty(x) for k, x in v.items() if x not in ([], {}, None)}
    if isinstance(v, list):
        return [_drop_empty(x) for x in v]
    return v


def _normalize_for_compare(area):
    a = _drop_empty(copy.deepcopy(area))
    a.pop("$schema", None)
    for k in MODEL_KEYS:
        if k in a and not a[k]:
            del a[k]
        a[k] = sorted(a.get(k) or [], key=lambda it: _natural(it.get("id")))
        if not a[k]:
            a.pop(k)
    for ex in a.get("examples") or []:
        ex.setdefault("expect", {})
    for sc in a.get("screens") or []:
        sc.setdefault("auth_required", False)
    if a.get("navigation"):
        a["navigation"] = sorted(a["navigation"], key=lambda n: (
            n.get("from") or "", n.get("to") or "", n.get("trigger") or ""))
    return a


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
        if k in MODEL_KEYS or k in UI_KEYS or k in RECORD_KEYS:
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
            "refusal": {"type": "object", "additionalProperties": False,
                        "properties": {f: copy.deepcopy(req["refusal"]["properties"][f])
                                       for f in ("status", "checked_at")}},
            "meaning": {"type": "object", "additionalProperties": False,
                        "properties": {"written_against": copy.deepcopy(
                            req["meaning"]["properties"]["written_against"])}},
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
            "(check results, witness and refusal verdicts, freshness pins, "
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


def _legacy_inputs(root, name):
    path = legacy_path(root, name)
    if path is None:
        sys.exit(f"ERROR: no specs/{name}.area.json or .contract.json to migrate")
    legacy = json.loads(path.read_text(encoding="utf-8"))
    fm = legacy.get("formal_model") or {}
    qpath = specs_dir(root) / (fm.get("quint_file") or f"{name}.qnt")
    qtext = qpath.read_text(encoding="utf-8") if qpath.exists() else None
    apath = specs_dir(root) / fm["alloy_file"] if fm.get("alloy_file") else None
    atext = apath.read_text(encoding="utf-8") if apath and apath.exists() else None
    ir = {}
    if qtext is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from quint_ir import parse_qnt  # noqa: WPS433
        ir = parse_qnt(qpath) or {}
    return path, legacy, qpath, qtext, apath, atext, ir


def _cmd_migrate(args):
    path, legacy, qpath, qtext, apath, atext, ir = _legacy_inputs(args.root, args.area)
    qnt, als, intent, records = migrate_area(legacy, qtext, atext, ir)
    if not args.write:
        print(qnt)
        return 0
    if qnt is not None:
        qpath.write_text(qnt, encoding="utf-8")
    if apath and als is not None:
        apath.write_text(als, encoding="utf-8")
    write_intent(args.root, args.area, intent)
    write_records(args.root, args.area, records)
    path.unlink()
    hosts = [n.name for n, t in ((qpath, qnt), (apath, als)) if n and t is not None]
    print(f"migrated {args.area}: records -> doc comments in {', '.join(hosts)}; "
          f"{args.area}{INTENT_SUFFIX}, {args.area}{RECORDS_SUFFIX}; removed {path.name}")
    return 0


def roundtrip(legacy, qtext, atext=None, ir=None):
    """Differences between a legacy area and the view derived from its
    migration — [] when the migration is lossless."""
    name = legacy.get("area")
    qnt, als, intent, records = migrate_area(legacy, qtext, atext, ir)
    fm = intent.get("formal_model") or {}
    files = {f"{name}{INTENT_SUFFIX}": json.dumps(intent),
             f"{name}{RECORDS_SUFFIX}": json.dumps(records),
             fm.get("quint_file") or f"{name}.qnt": qnt}
    if qnt is None:
        files.pop(fm.get("quint_file") or f"{name}.qnt")
    if fm.get("alloy_file") and als is not None:
        files[fm["alloy_file"]] = als
    problems = []
    derived, _ = derive_from_texts(name, files.get, problems)
    if qtext is None and "formal_model" in derived and "formal_model" not in legacy:
        derived.get("formal_model", {}).pop("quint_file", None)
        if derived.get("formal_model") == {}:
            derived.pop("formal_model")
    if not any("action" in n for n in legacy.get("navigation") or []):
        for n in derived.get("navigation") or []:
            n.pop("action", None)  # found by migration: added, not lost
    a, b = _normalize_for_compare(legacy), _normalize_for_compare(derived)
    return problems + _diff_paths(a, b)


def _cmd_roundtrip(args):
    _path, legacy, _q, qtext, _a, atext, ir = _legacy_inputs(args.root, args.area)
    diffs = roundtrip(legacy, qtext, atext, ir)
    for d in diffs:
        print(f"differs: {d}")
    print(f"{args.area}: {'lossless' if not diffs else f'{len(diffs)} difference(s)'}")
    return 1 if diffs else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("derive", "migrate", "roundtrip"):
        sp = sub.add_parser(cmd)
        sp.add_argument("area")
        sp.add_argument("--root", default=".")
        if cmd == "migrate":
            sp.add_argument("--write", action="store_true",
                            help="write the files and remove the legacy JSON")
    sp = sub.add_parser("schemas", help="(re)generate intent/records schemas")
    sp.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    sp.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    return {"derive": _cmd_derive, "migrate": _cmd_migrate,
            "roundtrip": _cmd_roundtrip, "schemas": _cmd_schemas}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
