"""Test-only: write an area given as a view-shaped dict to the files it lives
in (records as `///` doc comments on the model, intent and ledger beside it).

Tests describe areas as the derived view (`spec_source.load_area`) is
shaped, because that is what the tools read; this places each record where
the grammar says it lives:

  requirement   on `on` (a test-only key naming any declaration), else on
                its `quint_ref` action, else on the module
  invariant     on its `val <quint_name>`; `over: probes` with a predicate
                becomes a `def <name>_over(<ghosts>)` the reader maps back
  constant      on its `pure val|const <name>`, appended when absent
  example       on its `run`, else on its `when` action
  screen / nav  on the screen type / the action that moves between them

A field the grammar has no tag for raises, so a test written against a
removed field fails loudly instead of silently losing it."""

import copy
import json
import re

import spec_source as ss
from spec_source import SpecSourceError

WRAP = 76

EMIT_ORDER = {
    "requirements": ("type", "status", "modality", "determinism", "source",
                     "where", "when", "unwanted", "while", "old", "shall",
                     "outcome", "fit", "error", "verified-by", "refs",
                     "evidence", "confidence", "inferred-by", "fingerprint"),
    "invariants": ("criticality", "proof", "over", "quint-name", "predicate",
                   "alloy", "smt", "refs", "source", "evidence", "confidence",
                   "inferred-by", "fingerprint"),
    "properties": ("quint-name", "refs"),
    "constraints": ("name", "unit", "pairs", "value", "source", "evidence",
                    "confidence", "inferred-by", "fingerprint"),
    "examples": ("given", "when", "expect", "refs", "source", "trace"),
}
SCREEN_FIELDS = {"name", "auth_required", "purpose", "components"}
NAV_FIELDS = {"id", "from", "to", "trigger", "guard", "action"}


def _wrap(prefix, text, indent, cont="///   "):
    text = str(text)
    if "\n" in text:
        raise SpecSourceError(f"cannot write a multi-line value into a doc tag: {text[:40]!r}")
    head = f"{indent}/// {prefix}"
    if text != " ".join(text.split()) or len(head) + len(text) + 1 <= WRAP + len(indent):
        return [f"{head} {text}".rstrip()]
    lines, cur = [], head
    for w in text.split():
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


def _values(kind, tag, item):
    if tag in ss.TAGS.get(kind, {}):
        path, vk = ss.TAGS[kind][tag]
        v = ss._get(item, path)
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
            out.append((f"{eo['external']}.{eo['outcome']}{flag}: {eo['effect']}", eo.get("note")))
        return out
    if tag == "outcome":
        return [f"{oc['name']}: {oc['predicate']}"
                for oc in (item.get("witness") or {}).get("outcomes") or []]
    if tag == "old":
        return [f"{o['name']}: {o['type']} = {o['expr']}" for o in item.get("old") or []]
    if tag == "when" and kind == "examples":
        w = item.get("when") or {}
        if not w:
            return []
        args = f" {json.dumps(w['args'], ensure_ascii=False)}" if w.get("args") else ""
        return [f"{w['action']}{args}"]
    return []


def emit_doc(tag, rid, item, kind, indent="  ", skip=()):
    lines = [f"{indent}/// @{tag} {rid}"]
    desc = item.get("title" if kind == "examples" else "description")
    if kind == "requirements" and item.get("ears") and desc == ss.ears_sentence(
            {k: v for k, v in item.items() if k != "description"}):
        desc = None
    lines += _wrap_desc(desc, indent)
    for t in EMIT_ORDER[kind]:
        if t in skip:
            continue
        for v in _values(kind, t, item):
            if t == "error":
                v, note = v
                lines += _wrap(f"@{t}", v, indent)
                if note:
                    lines += _wrap("@error-note", note, indent)
                continue
            lines += _wrap(f"@{t}", v, indent) if v != "" else [f"{indent}/// @{t}"]
    return lines


def _covered(kind):
    paths = {p for p, _ in ss.TAGS[kind].values()} | set(ss.MACHINE_PATHS[kind]) | {"id"}
    return paths | {
        "requirements": {"description", "fit_criterion", "error_outcomes", "status",
                         "witness.outcomes", "old", "quint_ref", "form", "on",
                         "obligation"},
        "invariants": {"description", "quint_name", "alloy_command"},
        "properties": {"description", "quint_name"},
        "constraints": {"name", "value", "description"},
        "examples": {"title", "when", "quint_run"},
    }[kind]


def _leaf_paths(d, prefix=""):
    out = []
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v and k not in ("given", "expect", "args", "when",
                                                    "fit_criterion", "obligation"):
            out += _leaf_paths(v, p)
        else:
            out.append(p)
    return out


def _ghost_def(name, predicate, ghost_types, indent):
    ghosts = sorted(set(re.findall(r"\b(_prev[A-Z][A-Za-z0-9_]*)\b", predicate)))
    params = ", ".join(f"{g}: {ghost_types[g]}" if g in ghost_types else g for g in ghosts)
    return [f"{indent}def {name}({params}): bool =", f"{indent}  {predicate}"]


def _ghost_types(ir):
    from itf_tools import ghost_for_var
    return {ghost_for_var(v): t for v, t in (ir.get("var_type_strs") or {}).items()}


def _open_module(lines):
    text = "\n".join(lines)
    m = re.search(r"^(\s*module\s+\w+\s*\{)(.*)$", text, re.M)
    if m and m.group(2).strip():
        text = text[:m.start()] + m.group(1) + "\n  " + m.group(2).strip() + text[m.end():]
    end = text.rstrip().rfind("}")
    if end >= 0:
        before = text[:end]
        if before[before.rfind("\n") + 1:].strip():
            text = before.rstrip() + "\n}" + text[end + 1:]
    return text.splitlines()


def write_area(view, model_text, als_text=None, ir=None):
    """View-shaped dict + its model text(s) -> (qnt, als, intent, records)."""
    name = view.get("area")
    ir = ir or {}
    intent = {"$schema": "../schemas/intent.schema.json"}
    for k, v in view.items():
        if k in ss.MODEL_KEYS or k in ss.UI_KEYS or k in ss.RECORD_KEYS or k == "$schema":
            continue
        intent[k] = copy.deepcopy(v)
    if isinstance(intent.get("brief"), dict):
        intent["brief"].pop("written_against", None)
    clean = copy.deepcopy(view)
    for r in clean.get("requirements") or []:
        r.pop("on", None)
    _authored, records = ss.split_machine(clean)

    for kind in ss.MODEL_KEYS:
        covered = _covered(kind)
        for item in view.get(kind) or []:
            for p in _leaf_paths(item):
                if p not in covered and not any(p.startswith(c + ".") for c in covered):
                    raise SpecSourceError(f"{name}: {item.get('id')}: field '{p}' has "
                                          f"no doc-comment tag")

    lines = (model_text or "").splitlines()
    if model_text is None:
        lines = [f"module {re.sub(r'[^A-Za-z0-9_]', '_', name)} {{", "}"]
    lines = _open_module(lines)
    inserts, appended, module_doc = {}, [], []
    mod_line = next((i for i, l in enumerate(lines) if re.match(r"^\s*module\s+\w+", l)), None)
    if mod_line is None:
        raise SpecSourceError(f"{name}: model has no module declaration")
    close_line = max(i for i, l in enumerate(lines) if l.strip() == "}")

    def decl_line(kinds, decl_name):
        if not decl_name:
            return None
        pat = re.compile(rf"^(\s*)(?:pure\s+)?(?:{kinds})\s+{re.escape(decl_name)}\b")
        return next((i for i, l in enumerate(lines) if pat.match(l)), None)

    def indent_of(i):
        return re.match(r"^(\s*)", lines[i]).group(1)

    def put(i, doc):
        if i in inserts:
            inserts[i].append(f"{indent_of(i)}///")
        inserts.setdefault(i, []).extend(doc)

    for r in view.get("requirements") or []:
        target = r.get("on")
        line = (decl_line("action|def|val|temporal|run|type", target) if target
                else decl_line("action", r.get("quint_ref")))
        if target and line is None:
            raise SpecSourceError(f"{name}: {r['id']}: no declaration `{target}`")
        if line is not None:
            put(line, emit_doc("req", r["id"], r, "requirements", indent_of(line)))
        else:
            module_doc.append(emit_doc("req", r["id"], r, "requirements", ""))

    gt = _ghost_types(ir)
    for inv in view.get("invariants") or []:
        iid = inv["id"]
        if inv.get("proof") == "structural":
            if als_text is None:
                raise SpecSourceError(f"{name}: {iid}: structural invariant but no .als")
            continue
        if inv.get("over") == "probes":
            if inv.get("predicate") and model_text is not None:
                gname = (inv.get("quint_name") or iid.replace("-", "_")) + "_over"
                doc = emit_doc("inv", iid, inv, "invariants", "  ", skip=("predicate",))
                appended.append(doc + _ghost_def(gname, inv["predicate"], gt, "  "))
            else:
                module_doc.append(emit_doc("inv", iid, inv, "invariants", ""))
            continue
        line = decl_line("val", inv.get("quint_name"))
        if line is not None:
            put(line, emit_doc("inv", iid, inv, "invariants", indent_of(line),
                               skip=("quint-name",)))
        else:
            module_doc.append(emit_doc("inv", iid, inv, "invariants", ""))

    for p in view.get("properties") or []:
        line = decl_line("temporal", p.get("quint_name"))
        if line is not None:
            put(line, emit_doc("prop", p["id"], p, "properties", indent_of(line),
                               skip=("quint-name",)))
        else:
            module_doc.append(emit_doc("prop", p["id"], p, "properties", ""))

    for c in view.get("constraints") or []:
        line = decl_line("val|const", c.get("name"))
        item = dict(c)
        v = c.get("value")
        if line is not None:
            body = ss._literal((ss._parse_host(lines, line, "quint") or {}).get("body"))
            if body == v and type(body) is type(v):
                item.pop("value")
            put(line, emit_doc("con", c["id"], item, "constraints", indent_of(line),
                               skip=("name",)))
        elif model_text is not None and isinstance(v, (int, bool, str)):
            lit = json.dumps(v) if isinstance(v, str) else (str(v).lower() if isinstance(v, bool) else str(v))
            item.pop("value")
            appended.append(emit_doc("con", c["id"], item, "constraints", "  ", skip=("name",))
                            + [f"  pure val {c['name']} = {lit}"])
        else:
            module_doc.append(emit_doc("con", c["id"], c, "constraints", ""))

    for ex in view.get("examples") or []:
        line = decl_line("run", ex.get("quint_run"))
        if line is not None:
            put(line, emit_doc("example", ex["id"], ex, "examples", indent_of(line)))
            continue
        act = decl_line("action", (ex.get("when") or {}).get("action"))
        if act is not None and not (ex.get("when") or {}).get("args"):
            item = {k: v for k, v in ex.items() if k != "when"}
            put(act, emit_doc("example", ex["id"], item, "examples", indent_of(act)))
        else:
            module_doc.append(emit_doc("example", ex["id"], ex, "examples", ""))

    screens, navs = view.get("screens") or [], view.get("navigation") or []
    for sc in screens:
        if set(sc) - SCREEN_FIELDS:
            raise SpecSourceError(f"{name}: screen {sc.get('name')}: untaggable fields")
    for nv in navs:
        if set(nv) - NAV_FIELDS:
            raise SpecSourceError(f"{name}: navigation: untaggable fields")
    types, actions = {}, {}
    for i, l in enumerate(lines):
        m = re.match(r"^\s*(type|action)\s+([A-Za-z_][A-Za-z0-9_]*)", l)
        if m:
            body = (ss._parse_host(lines, i, "quint") or {}).get("body") or ""
            if m.group(1) == "type":
                types[m.group(2)] = (i, ss.type_variants(body))
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
            doc += _emit_ui("screen", sc, indent_of(i))
        put(i, doc)
    else:
        module_doc.extend(_emit_ui("screen", sc, "") for sc in screens)
    svar = ss.screen_var(model_text, stype) if stype else None
    for nv in navs:
        target = nv.get("action")
        if target is not None and target not in actions:
            raise SpecSourceError(f"{name}: navigation: no action `{target}` in the model")
        if target is None:
            cands = [a for a, (_i, b) in actions.items()
                     if ss.nav_moves(b, svar, nv["from"], nv["to"])]
            target = cands[0] if len(cands) == 1 else None
        if target is None:
            module_doc.append(_emit_ui("nav", nv, ""))
            continue
        put(actions[target][0], _emit_ui("nav", nv, indent_of(actions[target][0])))

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
            for block in appended:
                out.append("")
                out += block
        if i in inserts:
            if out and ss.DOC_RE.match(out[-1] or ""):
                out.append(f"{indent_of(i)}///")
            out += inserts[i]
        out.append(l)
    qnt_out = "\n".join(out) + "\n"
    if model_text is None and not module_doc and not appended:
        qnt_out = None

    als_out = als_text
    if als_text is not None:
        alines, ains = als_text.splitlines(), {}
        for inv in view.get("invariants") or []:
            if inv.get("proof") != "structural":
                continue
            target = inv.get("alloy_command")
            idx = next((i for kw in ("assert", "check") for i, l in enumerate(alines)
                        if re.match(rf"^\s*{kw}\s+{re.escape(target or '')}\b", l)), None)
            if idx is None:
                raise SpecSourceError(f"{name}: {inv['id']}: no `assert {target}` in the .als")
            ains[idx] = emit_doc("inv", inv["id"], inv, "invariants", "", skip=("proof", "alloy"))
        res = []
        for i, l in enumerate(alines):
            res += ains.get(i, [])
            res.append(l)
        als_out = "\n".join(res) + "\n"
    return qnt_out, als_out, intent, records


def _emit_ui(tag, item, indent):
    if tag == "screen":
        lines = [f"{indent}/// @screen {item['name']}"] + _wrap_desc(item.get("purpose"), indent)
        if item.get("auth_required"):
            lines.append(f"{indent}/// @auth-required")
        if item.get("components"):
            lines += _wrap("@components", ", ".join(item["components"]), indent)
        return lines
    nid = f"{item['id']} " if item.get("id") else ""
    lines = [f"{indent}/// @nav {nid}{item['from']} -> {item['to']}"]
    lines += _wrap_desc(item.get("trigger"), indent)
    if item.get("guard"):
        lines += _wrap("@guard", item["guard"], indent)
    return lines
