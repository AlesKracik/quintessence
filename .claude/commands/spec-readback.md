# /spec-readback — Generate Human-Readable Review Document

The readback is the review surface humans trust, so it is **rendered by a tool, not by you**: `tools/spec-readback.py` derives every section, sentence, table, diagram, and status mark from each area's model doc comments, intent and records files (through `tools/spec_source.py`), journeys, and change manifests. Identical input → byte-identical output; `git diff specs/<area>.readback.md` on a PR **is** the change review. Your job is orchestration and follow-up, never content.

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

**Per-area** (`specs/<area>.readback.md`): one-line **ship verdict** (`✓ READY` / `⚠ NOT READY — <blockers>` / `⏳ EMPTY`) → header status bar (incl. action Coverage) + legend with consequence glosses → Purpose → **Shape** (entities, externals, spanned areas, plus the areas that own what this one only reads — `scope.excluded[].owner` — and one dashed *later pass* node for `scope.excluded[].pass`) → **⚠ Needs Your Attention** (counterexamples with NL explanations, unchecked/unreachable/stale/unverifiable witnesses — each with its EARS sentence inline, coverage gaps, open questions, drift) → Navigation/Screens/UI Components (areas with UI blocks) → **At a Glance** (a `pie` of proof status — proven to ≤N, proven only to the shallow bound, broken, not checked, unit test — drawn only when there are two slices or more; then one row per requirement, grouped under the journey that first walks it, in journey order, with a status tally per group and a last group for requirements in no journey; the **Kind** column names only what differs: `refusal`, `computed`, a non-`must` modality, else `happy path`) → **What the System Does** — requirements grouped as journey slices in temporal order. Each journey card: who, story, route (main steps only, each with its `@title` and a `(+N refusals)` suffix), and **Runs as** (✓ the run that walks the flow and how many steps it covers, ✗ where it breaks, ⚠ no run yet), then a **journey flowchart** (one box per main step, coloured by status, refusal counts hanging off it; 2–16 steps only). The steps sit in a `<details>` (the first journey's open). **Refusals fold under the step they refuse**: an `@unwanted` requirement step goes into a nested `↳ Step N is refused in K cases: …` fold under the main step before it, so a 5-step flow reads as 5 steps (a journey that *starts* with a refusal keeps it as a step). Each requirement block keeps in view what a reviewer judges it by: status mark, `(failure path)` tag for unwanted REQs, the `@title` in bold, the rendered EARS sentence with constraint values resolved inline (`MAX_FAILED_ATTEMPTS (= 5, CON-001)`), its **example rows** (Given / When / Then, derived from the example runs; a refused call reads `— refused`), one **Proof** line (`reachable (2-step trace) · ∀ holds on every reachable step (≤10 steps)`), and anything broken or uncertain (`∀✗`, forbidden/may/nondeterministic blocks, `@error` outcomes, Left open, a **low-confidence** extraction). Everything else is evidence, behind one fold whose summary names what it holds: provenance, **Guarded by**, the witness one-liner (only the fields the last step moved: `6 steps: login_failed(bob) ×5 → `accountStatus[bob] = Locked``), the verbatim Quint action + `file:Lx-Ly` + short model sha, the formal claim (`@old` / `@while` / `@shall` / `@outcome`), and the trace sequence diagram (≤12 steps; each arrow carries the call's arguments, each note lists only the leaves that changed — `groups[g1].name = "GOLD"`, `slpMembers[awsSlp] += {"g1"}`); NFRs render their fit criterion instead → **What Each Action Does** (per action: a top-down **refusal flowchart** of its `if (c) refuse(st, R) else if …` chain when the model writes one — the first refusal that applies is the answer — then **Guarded by** once for the action, then its decision table folded: when, what it changes, the requirement or verdict that says so) → What Must Always Be True / Eventually Happen → **Examples** (every example as a row, with what it illustrates; folded behind a count) → **What the Code Does That the Spec Does Not** (brownfield: a `pie` of triage verdicts plus unclaimed, and the ledger folded) → **Limits and Bounds** (CON table: value, unit, description, referenced-by, history from landed change manifests) → State Machines (a state no assignment in the model produces is drawn dashed, with a one-line note — lint's `state-never-produced`, drawn) → collapsed Reference (concepts, resolved architecture, decisions, resolved questions, traceability, verification history, audit-trail pointer).

Nothing is dropped by the folds; it moves behind `<details>`. **Known limitation:** a link such as `#req-041` that points inside a closed fold scrolls nowhere on some Markdown viewers. Chrome opens the fold on fragment navigation; others do not — open the journey's **Steps** fold first.

**Change** (`specs/changes/<slug>.readback.md`): intent/status/branch → Targets table with **derived** phase columns (the manifest stores membership only) → attention roll-up scoped to the change's targets → What This Change Does: per manifest ID, the EARS sentence/statement with anchor links into per-area readbacks and the collapsed Quint + formal claim inline (the EARS↔Quint review happens on this one page) → blocking questions. Regenerates every touched target's area readback alongside.

**Project** (`.spec/readback.md`): attention roll-up across areas → areas table → journeys as step tables (sentence pulled from the owning area, status marks, anchor links).

**`status <slug> --json`**: the derived phase grid — the `/spec` change dashboard's mechanical source.

## What `/spec-readback` does NOT do

- **One authored field is the exception, and it is not an exception to determinism.** The **`brief`** in `specs/<area>.intent.json` — lead paragraphs plus optional `how_it_fits`, `why_this_way`, `watch_out_for` — is rendered verbatim; you never type prose into the `.md`. After writing or revising it, re-pin: `tools/itf_tools.py pin <area> --brief`. A stale brief is a lint finding, and FAILs once the area is `in-review`.
- **There is no per-requirement prose.** A requirement reads as its rendered EARS sentence and its example rows. If a reviewer cannot follow a requirement, the fix is a better example (a concrete case in the user's words, as an `@example` run), not a paraphrase beside it.
- **Don't rewrite a `brief` whose `author` is `human`** without asking — offer an edit instead.
- **Never writes readback content yourself** — not a sentence, not a table row. If something is missing from the output, the fix is in the spec's source files (or in `tools/spec-readback.py`), never prose patched into the generated file.
- **Does not modify any spec file** — generator and command are read-only over them.
- **Does not render images** — Mermaid is text; viewers render it.
