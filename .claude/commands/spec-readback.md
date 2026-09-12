# /spec-readback — Generate Human-Readable Review Document

The readback is the review surface humans trust, so it is **rendered by a tool, not by you**: `tools/spec-readback.py` derives every section, sentence, table, diagram, and status mark from the area JSONs, sidecars, journeys, and change manifests. Identical input → byte-identical output; `git diff specs/<area>.readback.md` on a PR **is** the change review. Your job is orchestration and follow-up, never content.

## Usage
```
/spec-readback [target]            # per-area readback
/spec-readback                     # change readback for the active change + refreshed targets
/spec-readback _project            # project-wide overview readback
/spec-readback --all               # project overview + every area
```

## Instructions

1. Resolve the mode and run the generator:

| Invocation | Command |
|---|---|
| `/spec-readback <area>` | `tools/spec-readback.py area <area>` |
| `/spec-readback` (active change in `.spec/local.json`) | `tools/spec-readback.py change` |
| `/spec-readback` (no active change) / `_project` | `tools/spec-readback.py project` |
| `/spec-readback --all` | `tools/spec-readback.py all` |

2. Read the generated file(s) and tell the user, briefly: where they were written, what the "Needs Your Attention" section flags, and the suggested next command (e.g. `/spec-check` if witnesses are unchecked, `/spec <area>` if open questions block).

3. Suggest the commit:

```bash
git add specs/<target>.readback.md .spec/readback.md specs/changes/<slug>.readback.md
git commit -m "spec(<target>): readback — <summary>"
```

## What the generator emits (reference — the tool is the source of truth)

**Per-area** (`specs/<area>.readback.md`): one-line **ship verdict** (`✓ READY` / `⚠ NOT READY — <blockers>` / `⏳ EMPTY`) → header status bar (incl. matrix Coverage) + legend with consequence glosses → Purpose → **⚠ Needs Your Attention** (counterexamples with NL explanations, unchecked/unreachable/stale/unverifiable witnesses — each with its EARS sentence inline, coverage gaps, open questions, drift) → Navigation/Screens/UI Components (areas with UI blocks) → **What the System Does** — requirements grouped as journey slices in temporal order, each block: status mark, `(failure path)` tag for unwanted REQs, the requirement's plain-words `meaning` when one is pinned and current (the EARS sentence moves into the collapsed details as **As specified (EARS)**) and the rendered EARS sentence otherwise, with constraint values resolved inline either way (`MAX_FAILED_ATTEMPTS (= 5, CON-001)`), witness one-liner (`6 steps: login_failed(bob) ×5 → account Locked`), collapsed details with the verbatim Quint action + `file:Lx-Ly` + short model sha, the witness predicate, and the trace sequence diagram (≤12 steps); NFRs render their fit criterion instead → What Must Always Be True / Eventually Happen → **Limits and Bounds** (CON table: value, unit, description, referenced-by, history from landed change manifests) → State Machines → collapsed Reference (concepts, resolved architecture, decisions, resolved questions, traceability, verification history, audit-trail pointer).

**Change** (`specs/changes/<slug>.readback.md`): intent/status/branch → Targets table with **derived** phase columns (the manifest stores membership only) → attention roll-up scoped to the change's targets → What This Change Does: per manifest ID, the EARS sentence/statement with anchor links into per-area readbacks and the collapsed Quint+predicate inline (the EARS↔Quint review happens on this one page) → blocking questions. Regenerates every touched target's area readback alongside.

**Project** (`.spec/readback.md`): attention roll-up across areas → areas table → journeys as step tables (sentence pulled from the owning area, status marks, anchor links).

**`status <slug> --json`**: the derived phase grid — the `/spec` change dashboard's mechanical source.

## What `/spec-readback` does NOT do

- **Two authored fields are the exception, and neither is an exception to determinism.** Both live in the *area JSON* and are rendered verbatim; you never type prose into the `.md`.
  - **`brief`** — lead paragraphs plus optional `how_it_fits`, `why_this_way`, `watch_out_for`. After writing or revising it, re-pin: `tools/itf_tools.py spec-sha <area>` → `brief.written_against`.
  - **`requirements[].meaning`** — the plain-words sentence the readback leads with, per requirement. This is the one place a model's understanding is allowed to replace the mechanical rendering: the EARS fields say the behavior in the system's own vocabulary, identifiers and exception names included, and the meaning says what that amounts to for a reviewer who has never seen the code. Written once, re-pinned per requirement: `tools/itf_tools.py meaning-sha <area> --req <ID>` → `meaning.written_against`. A **stale** meaning is not rendered at all — the headline falls back to the EARS sentence and the page says why — because a distillation of an older version of the requirement reads like it was reviewed when it wasn't.
  - Leaving either unpinned or stale is a lint finding, and FAILs once the area is `in-review`.
- **Don't rewrite a `brief` or a `meaning` whose `author` is `human`** without asking — offer an edit instead.
- **Never writes readback content yourself** — not a sentence, not a table row. If something is missing from the output, the fix is in the source JSON (or in `tools/spec-readback.py`), never prose patched into the generated file.
- **Does not modify area JSONs or sidecars** — generator and command are read-only over them.
- **Does not render images** — Mermaid is text; viewers render it.
