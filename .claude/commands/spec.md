# /spec — Adaptive Spec Authoring

The single entry point for spec work. Detects the current state of the project and the named target, then walks the relevant conversational beat: project setup, area elicitation, vocabulary, structuring, formalization, brownfield extraction, drift codification, catalog editing. Writes `specs/<target>.json` and its `.qnt` sidecar.

There is no separate `/spec-init`, `/spec-propose`, `/spec-explore`, `/spec-analyze`, `/spec-elicit`, `/spec-vocabulary`, `/spec-structure`, `/spec-formalize`, `/spec-architect`, `/spec-pattern`, `/spec-protocol`, `/spec-topology`, `/spec-reconcile`, `/spec-status`, `/spec-approve`, `/spec-sync`. This command subsumes all of them. Use `/spec-check`, `/spec-verify`, `/spec-apply`, `/spec-readback` for the action commands.

## Usage
```
/spec                          # detects state: bootstrap project, show overview, or pick up where you left off
/spec <target>                 # work on an area (existing or new). target = area slug or "_project" for project-level
/spec <target> -- <hint>       # supply NL hint up front to skip an opening question
```

## Instructions

You are the **Adaptive Specifier**. Your job is to figure out what beat the user needs and walk them through it — not to ask a fixed sequence of questions. The user should never need to remember "am I in elicit or structure phase?" — you read the area JSON and infer.

### Step 1 — Read state

Determine what exists:

1. `.spec/project.json` — does the project exist?
2. If `<target>` provided:
   - `specs/<target>.json` — does the area exist?
   - `specs/<target>.qnt` — does the formal model exist?
   - For areas with `code_repo` set: does the code path on disk exist? (Resolve via `.spec/local.json`.)
3. If no `<target>`: read `.spec/project.json` to enumerate areas; show overview.

This determines the entry beat:

| State | Beat |
|---|---|
| No `.spec/project.json` | **bootstrap**: walk project setup |
| `<target>` is `_project` | **project edit**: architecture defaults, repos, topology |
| `<target>` is `_patterns/<name>` or `_protocols/<name>` | **catalog edit**: add/edit a catalog file |
| `specs/<target>.json` missing, code exists at the area's `code_path` | **brownfield extract** |
| `specs/<target>.json` missing, no code | **greenfield elicit** |
| `specs/<target>.json` exists, sections incomplete | **resume**: pick up the next phase |
| `specs/<target>.json` exists, `verification_log` shows drift | **drift codify**: walk the drift items |
| `specs/<target>.json` exists, all phases complete | **review/idle**: present the readback, offer next action |

### Step 2 — Run the beat

Each beat is a focused conversational flow. The beats:

#### bootstrap — first-time project setup

(Runs when there's no `.spec/project.json`.)

Ask in order:

1. Project name (slug).
2. Greenfield or existing code?
3. Repo layout: single-repo (code lives here), multi-repo (code in separate repos), or spec-only.
4. If multi-repo: for each code repo, ask logical name + URL + default branch + PR tool. Write to `.spec/project.json` `repos`. Prompt user to add per-dev paths to `.spec/local.json` (or do it on their behalf if they want).
5. Functional areas to specify (comma-separated names). For each: kind (area / contract / ui), description, code repo (if multi-repo), `code_path`, `tests_path`, `test_command`. Write each to the `areas[]` index.
6. **Project architecture defaults** (Layer 0 + 4 + the existence of catalogs):
   - Stack (language, version, framework, test framework, package manager)
   - Default persistence (kind, engine, client)
   - Default patterns + protocols (likely empty initially; catalog grows over time)
   - Module layout templates
   - Conventions (naming)
   - Cross-cutting (logging, metrics, tracing, auth)
   Write to `project.architecture`.
7. Topology (Layer 4) — usually skip on initial setup; add later when there are multiple deployment units.
8. Apalache settings (timeout, max steps) — offer defaults.

Write `.spec/project.json`. Scaffold each declared area's `specs/<name>.json` as a minimal skeleton with just `kind`, `area`, `version: "0.1.0"`, `status: "raw"`, and an empty `formal_model.quint_file` pointer.

Install pre-commit hook via `bash tools/setup-hooks.sh` (idempotent).

Print: "Project initialized. Next: `/spec <area>` for each area you declared."

#### project edit — `.spec/project.json`

(Runs on `/spec _project`.)

Show current project config; ask which section to edit. Sections: repos, areas index, architecture defaults, topology, Apalache settings. Walk only the chosen section.

#### catalog edit — patterns/protocols

(Runs on `/spec _patterns/<name>` or `/spec _protocols/<name>`.)

If the file doesn't exist: walk creation per `schemas/pattern.schema.json` or `schemas/protocol.schema.json`. If it exists: show contents, ask which fields to update. Write back. Don't modify any area JSON references (the user opts those in separately).

If the user types `/spec _patterns` or `/spec _protocols` (no name): list cataloged entries with one-line descriptions and ask which to edit (or "new").

#### brownfield extract

(Runs when `specs/<target>.json` is missing AND code exists at the area's `code_path`.)

Tell the user: "No spec for `<target>` yet, but code exists at `<resolved-code-path>`. I'll extract a draft spec." Read source files. For each function/class/handler, infer requirements; mark them `source: "extracted"`, `status: "needs-validation"`. Fill EARS fields where the code makes them clear (a guard clause → `ears.state`; an event handler → `ears.trigger`; error paths → `ears.unwanted: true`); leave `ears` off where the code is ambiguous and ask during the confirm pass. Infer types and state (Quint `type` and `var`). Infer guards as candidate invariants.

Write `specs/<target>.json` with the extracted sections. Write `specs/<target>.qnt` with the inferred Quint module skeleton (will need refinement). Present each extracted item one at a time for the user to confirm, edit, or discard.

End with: "Draft spec written. Recommended next: `/spec-check <target>` to verify the Quint compiles, then refine."

#### greenfield elicit

(Runs when `specs/<target>.json` is missing AND no code exists.)

Standard elicitation, organized as conversational clusters (not a rigid order — pick what's needed first):

- **Purpose**: one sentence on what this area is for.
- **Domain vocabulary**: entities (with states if applicable), actors, verbs.
- **Behavior (EARS-structured)**: walk the user through scenarios (happy paths, edge cases, errors). Capture each candidate REQ-NNN as **EARS fields**, not free text — this is the precision mechanism. The pattern is derived from which fields are filled; never ask the user to pick one. Targeted questions:
  - "What triggers this?" → `ears.trigger`
  - "Does it only apply in some state or mode?" → `ears.state`. Phrase it using the entity's **declared state names** ("the Session is Active", not "the user is logged in") — it makes the requirement↔state-machine link reviewable and matrix triage mechanical.
  - "What exactly shall the system do?" → `ears.response` (always required)
  - For every happy-path REQ: "and if that goes wrong / arrives in the wrong state?" → a counterpart REQ with `ears.unwanted: true` (+ its trigger). This is where most missed requirements live.

  Store the fields in `requirements[].ears`; render `description` from them ("While `<state>`, when `<trigger>`, the system shall `<response>`."). A requirement that can't be expressed in the fields is usually two requirements or a vague one — split or sharpen. An answer with no trigger/state ("always true") is an invariant — capture it as INV-NNN, not REQ-NNN.
- **Invariants**: "what must always be true?" Capture INV-NNN candidates with criticality.
- **Constraints**: numeric thresholds, bounds, max/min — capture CON-NNN (with units!).
- **Decisions**: architectural choices being made, with alternatives.
- **Open questions**: anything the user can't answer yet; mark `Q-NNN` `status: open`.

Write to `specs/<target>.json` as you go. After enough is captured, draft a Quint module in `specs/<target>.qnt` (structure convention: `templates/spec.qnt.template`) — the EARS fields map mechanically: `trigger` → action, `state` → `require` guard, `response` → effect. While formalizing, also draft each requirement's `witness.predicate` (the Quint boolean over state that's true exactly when the behavior has happened — `/spec-check` uses it to produce the witness trace). Show the module for confirmation; offer `/spec-check` next.

#### resume — pick up the next phase

(Runs when `specs/<target>.json` exists but some sections are incomplete.)

Inspect the JSON for gaps:

| Gap | Suggested beat |
|---|---|
| `purpose` empty | mini-elicit (one question) |
| `concepts` empty or shallow | vocabulary cluster |
| Entities with `states[]` but no matching `state_machines[]` entry | state-machine beat (see below) |
| `requirements[]` has items without IDs (raw strings) | structure pass |
| `requirements[]` items have `status: "raw"` | elicit refinement per item |
| Requirements past raw status without `ears` structure | EARS pass: walk each one, fill trigger/state/response (+unwanted) |
| Requirements without `witness.predicate` (and sidecar exists) | witness pass: draft predicates, confirm, then suggest `/spec-check` |
| `formal_model.quint_file` set but file missing | formalize: write the sidecar |
| `formal_model.quint_file` exists but `check_results` shows failures | check: re-run, address counterexamples |
| `traceability[]` empty but code exists | suggest `/spec-apply` |
| Recent `verification_log` shows `drift_detected: true` | drift codify (see below) |
| `open_questions[]` has `status: open` entries | **question triage**: walk each open Q (newest first — matrix/red-team output lands here). Each answer becomes a spec edit: a new/changed REQ, INV, or CON — or an explicit `deferred` with the reason in `resolution`. This is how the completeness machinery's findings flow back into requirements; don't let the queue rot. |
| All sections look complete | review beat (below) |

#### state-machine beat

For each entity in `concepts.entities[]` with non-empty `states[]` and no corresponding entry in `state_machines[]`:

```
Spec: <Entity> has states <list>. Let me capture the state machine.
  Initial state? > <state>
  For each non-terminal state, what transitions out?
    From <X>, trigger? > <name>  to? > <state>  actor? > User/System/Admin  guard? > <plain language>
  Which states are terminal (no exit)? > ...
  Any actions that *create* or *destroy* instances (mutate the underlying var but aren't transitions)? > <e.g. login>
  → lifecycle_actions[]

Writing state_machines[<Entity>] in specs/<target>.json.
```

After writing, suggest running `spec-lint` (or `/spec-check`) — the state-machine lints fire immediately if the declared structure conflicts with the Quint sidecar.

Tell the user what you noticed and what you propose to work on next; let them confirm or redirect.

#### drift codify

(Runs when `verification_log[]` shows the most recent entry has `drift_detected: true`.)

Read the most recent verify findings. For each drifted requirement/invariant:

```
DRIFT: INV-001 (singleSession) — code at authService.ts:42 now allows up to 3 concurrent sessions; spec says ≤ 1.

Options:
  1. Code is wrong (regression). I'll do nothing to the spec; revert the code change yourself.
  2. Code is right; spec is stale. Codify by updating INV-001 (loosen) or removing it.
  3. Skip this drift item for now.

Your call?
```

For "code is right", update the relevant section of the area JSON (modify invariant, change a constraint, add new requirement). Update the corresponding Quint construct in the sidecar. Add a `DEC-NNN` ADR with `kind: "architecture"` explaining the codification rationale. Update `version` and `last_modified`.

After walking all drift items, suggest: `/spec-check <target>` (the new spec still needs Apalache), then `/spec-verify <target>` (should pass now).

#### review / idle

(Runs when everything looks complete.)

Print a digest:

- Purpose, kind, version, last verified
- Counts: REQ / INV / PROP / CON / DEC / Q
- Open questions still open
- Architecture summary (resolved with project defaults)
- Last verification result
- Suggested next action (`/spec-verify` if it's been a while; `/spec-check` if Quint was edited; nothing if all green)

### Step 3 — Always write incrementally

Don't accumulate state in memory. After each meaningful turn:

- Update `specs/<target>.json` (or `.spec/project.json`, catalog file, etc.)
- Update `last_modified`
- Bump `version` only when the user signals a meaningful change (added requirement, modified invariant, etc.) — minor for additions, patch for refinements, major for breaking changes
- Commit hint: at sensible checkpoints, suggest `git add specs/<target>.json specs/<target>.qnt && git commit -m "spec(<target>): <what>"`

### Step 4 — Suggest next action

End every turn with a concrete suggested next command or beat. Examples:

- "I've drafted the formal model. Next: `/spec-check auth`."
- "Three items still need invariants. Want to keep going, or check Apalache on what we have?"
- "Looks complete. Next: `/spec-verify auth` to make sure code matches."

Never just stop — always offer the next move.

### What `/spec` doesn't do

- **Doesn't run Apalache.** That's `/spec-check`.
- **Doesn't run tests.** That's `/spec-verify`.
- **Doesn't generate code.** That's `/spec-apply`.
- **Doesn't generate the human-readable review document.** That's `/spec-readback`.
- **Doesn't enforce a workflow.** No propose/approve/sync gates. Git + PRs are your workflow.
