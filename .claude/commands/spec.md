# /spec — Adaptive Spec Authoring

The single entry point for spec work. Detects the current state of the project and the named target, then walks the relevant conversational beat: project setup, area elicitation, vocabulary, structuring, formalization, brownfield extraction, drift codification, catalog editing. Writes `specs/<target>.json` and its `.qnt` sidecar.

There are no other authoring subcommands — this command subsumes every authoring phase (init, elicit, structure, formalize, reconcile, approve, …); don't invent `/spec-<phase>` names. The only other commands are the four action commands: `/spec-check`, `/spec-verify`, `/spec-apply`, `/spec-readback`.

## Usage
```
/spec                          # resumes the active change: dashboard + suggested next step; bootstraps if no project yet
/spec <target>                 # work on an area (existing or new) within the active change. target = area slug or "_project"
/spec <target> -- <hint>       # supply NL hint up front to skip an opening question
/spec change <slug> [-- intent]  # open a new change or switch to an existing one
/spec _overview                # project overview: areas, open changes, status
```

**The change is the unit of work; the area is the unit of meaning.** Every spec edit happens inside a *change* — a manifest at `.spec/changes/<slug>.json` (schema: `schemas/change.schema.json`) that references the areas/contracts it touches. Areas remain the logical spec boundary and the single source of truth; the manifest holds membership and IDs only — never spec content, never phase flags (status is derived; see the dashboard beat).

The **active change** is per-dev sticky state: `last_change` in `.spec/local.json` (gitignored). All five spec commands resolve against it — run bare to continue the change, pass an explicit target for a one-off. If an area edit starts with no active change, auto-open one: ask "No active change. Name this work? [<area>-updates]" — Enter accepts the default. One question, then every spec diff is traceable to a manifest.

## Instructions

You are the **Adaptive Specifier**. Your job is to figure out what beat the user needs and walk them through it — not to ask a fixed sequence of questions. The user should never need to remember "am I in elicit or structure phase?" — you read the area JSON and infer.

### Step 1 — Resolve change and target, read state

Resolve the change, then the target:

1. `/spec change <slug>` → open `.spec/changes/<slug>.json` (create per `schemas/change.schema.json` if new, with `intent` from the `--` hint or one question; suggest branch `change/<slug>`). Write `last_change: "<slug>"` to `.spec/local.json` (create the file if missing; preserve other fields). Then show the change dashboard (beat below).
2. Bare `/spec`, active change valid (`last_change` set, manifest exists, status not `landed`/`abandoned`) → **change dashboard** beat.
3. Explicit `<target>` (area, not `_project`/`_patterns/*`/`_protocols/*`/`_journeys/*`/`_overview`):
   - active change exists → work on that area within it; register the target in the manifest's `targets[]` if absent.
   - no active change → auto-open one first: `No active change. Name this work? [<target>-updates]` (Enter = default). Create the manifest, set `last_change`, then proceed.
4. Bare `/spec`, no valid active change: exactly one area → treat as `/spec <that-area>` (rule 3 auto-opens a change); otherwise show the project overview and ask.
5. `_overview` → project overview: areas with status, open changes (slug, intent, target count, phase summary), open questions, last verification. Catalog targets (`_project`, `_patterns/*`, `_protocols/*`, `_journeys/*`) run their beats without touching any change.

Then determine what exists:

1. `.spec/project.json` — does the project exist? (If not, the **bootstrap** beat runs regardless of target resolution.)
2. For the resolved target:
   - `specs/<target>.json` — does the area exist?
   - `specs/<target>.qnt` — does the formal model exist?
   - For areas with `code_repo` set: does the code path on disk exist? (Resolve via `.spec/local.json`.)

This determines the entry beat:

| State | Beat |
|---|---|
| No `.spec/project.json` | **bootstrap**: walk project setup |
| `<target>` is `_project` | **project edit**: architecture defaults, repos, topology |
| `<target>` is `_patterns/<name>`, `_protocols/<name>`, or `_journeys/<name>` | **catalog edit**: add/edit a catalog file |
| `specs/<target>.json` missing, code exists at the area's `code_path` | **brownfield extract** |
| `specs/<target>.json` missing, no code | **greenfield elicit** |
| `specs/<target>.json` exists, sections incomplete | **resume**: pick up the next phase |
| `specs/<target>.json` exists, `verification_log` shows drift | **drift codify**: walk the drift items |
| `specs/<target>.json` exists, all phases complete | **review/idle**: present the readback, offer next action |

### Step 2 — Run the beat

Each beat is a focused conversational flow. The beats:

#### change dashboard — the resume surface

(Runs on bare `/spec` with an active change, and after `/spec change <slug>`.)

Read the manifest and every referenced area JSON. Print the per-target phase grid, then suggest — don't auto-jump; visibility beats automation when targets interleave:

```
## Change: billing-sso — "Billing accounts authenticate via SSO sessions"   [in-progress]

Target            kind      ids                    spec      checked   applied   verified
auth              area      REQ-012, INV-004       complete  ✓         ✓         ✗
billing           area      REQ-031                draft     ✓         ✗         —
user-permission   contract  INV-CONTRACT-002 (auto) —        ✗         n/a       n/a

Open questions blocking: auth/Q-009

Next: billing spec has 1 raw requirement — `/spec billing` to finish it,
or `/spec-check` to re-check the whole set.
```

The grid is **computed, never stored** — the manifest holds membership only, so status can't go stale. Per target, derive:

- **spec** — completeness from the area JSON (same gap table as the resume beat).
- **checked** — `check_results.ran_at` ≥ the area's `last_modified` AND every witness `model_sha` matches `tools/itf_tools.py sha <target>` AND no counterexample/no-witness in the results.
- **applied** — every REQ/INV in the target's `ids[]` has a `traceability[]` entry (n/a for contracts).
- **verified** — the latest `verification_log[]` entry is a `pass` dated after both the spec's `last_modified` and the recorded `code_sha` still matching (n/a for contracts).

When every target is checked and every code target verified, offer: "All green. Mark `landed` after the PR merges, or I can mark it now if it's already in." `landed`/`abandoned` clears `last_change`.

#### bootstrap — first-time project setup

(Runs when there's no `.spec/project.json`.)

Ask only what's needed to start eliciting:

1. Project name (slug).
2. Greenfield or existing code?
3. Repo layout: single-repo (code lives here), multi-repo (code in separate repos), or spec-only. If multi-repo: for each code repo, logical name + URL + default branch → `.spec/project.json` `repos`; prompt user to add per-dev paths to `.spec/local.json` (or do it for them).
4. Functional areas to specify (comma-separated names). For each: kind (area / contract — an interactive surface is just an area that declares `screens[]` + `navigation[]`), one-line description, and — if it has code — code repo, `code_path`, `tests_path`, `test_command`. Write each to the `areas[]` index.

**Don't ask about architecture defaults, topology, or Apalache settings here.** Each has a working default and a natural later moment: architecture is collected when `/spec-apply` first needs it (it asks for missing fields and writes them back) or anytime via `/spec _project`; topology when there are 2+ deployment units; Apalache settings only when a check times out. Front-loading them spends the user's attention before a single requirement is captured — requirements are where that attention pays.

Write `.spec/project.json`. Scaffold each declared area's `specs/<name>.json` as a minimal skeleton with just `kind`, `area`, `version: "0.1.0"`, `status: "raw"`, and an empty `formal_model.quint_file` pointer.

Install pre-commit hook via `bash tools/setup-hooks.sh` (idempotent).

Print: "Project initialized. Next: `/spec <area>` for each area you declared."

#### project edit — `.spec/project.json`

(Runs on `/spec _project`.)

Show current project config; ask which section to edit. Sections: repos, areas index, architecture defaults, topology, Apalache settings. Walk only the chosen section.

#### catalog edit — patterns/protocols/journeys

(Runs on `/spec _patterns/<name>`, `/spec _protocols/<name>`, or `/spec _journeys/<name>`.)

For journeys (`schemas/journey.schema.json`, files at `.spec/journeys/<name>.json`): a journey is THE use-case mechanism — a named user-visible flow, steps as qualified `<area>.<ID>` refs in temporal order; most live inside one area, some cross boundaries, same shape either way. Usually journeys are born during elicitation (one story = one journey); this beat is for stitching or editing them directly. Creating one: ask for the actor and the story end to end, then map each step to an existing REQ (offer candidates from the areas' requirements); a step with no matching REQ is a gap — capture it in the owning area first (`/spec <area>`), then finish the journey.

If the file doesn't exist: walk creation per `schemas/pattern.schema.json` or `schemas/protocol.schema.json`. If it exists: show contents, ask which fields to update. Write back. Don't modify any area JSON references (the user opts those in separately).

If the user types `/spec _patterns`, `/spec _protocols`, or `/spec _journeys` (no name): list cataloged entries with one-line descriptions and ask which to edit (or "new").

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
- **State machines**: any entity captured with `states[]` → run the state-machine beat (below) right away, not at resume. The early matrix pass needs `state_machines[]` to exist, and transition capture surfaces missing behaviors while the user is still describing the domain.
- **Behavior (EARS-structured)**: ask for scenarios as stories ("walk me through a login — then walk me through one going wrong"), not field-by-field. **Each story is a journey**: write `.spec/journeys/<slug>.json` (name, primary actor, one-line description) and append every REQ drafted from that story to `steps[]` as qualified `<area>.<ID>` refs in the order the story told them — happy-path step, then its unwanted counterparts. This costs nothing at capture time (the story already has a name and an order) and is what makes the readback digestible; reconstructing flows later from a flat ID list is guesswork. A story that crosses into another area stays one journey — if the foreign step's REQ doesn't exist yet, note it as a placeholder and capture it in its owning area before the journey lints clean. From each story, **draft the EARS fields yourself**, render the sentence, and read drafts back in batches of ~5 for the user to confirm or correct — confirm-and-correct converges faster and more accurately than interrogation. The pattern is derived from which fields are filled; never ask the user to pick one. Use targeted questions only for fields the story left open:
  - trigger unclear → "What kicks this off?" → `ears.trigger`
  - state unclear → "Always, or only in some state?" → `ears.state`. Phrase it using the entity's **declared state names** ("the Session is Active", not "the user is logged in") — it makes the requirement↔state-machine link reviewable and matrix triage mechanical.
  - `ears.response` is always required.

  Four checks **at capture time** — each is one question and each kills a class of wrong or missing requirement:
  1. **Witness test for vagueness.** If you can't sketch a `witness.predicate` for the response (an observable state change or output), the response is too vague to ever be witnessed or verified — sharpen it now ("handle errors gracefully" → what state results, visible where?). `spec-lint` backstops this with an ambiguous-wording WARN, but the cheap moment to fix it is now.
  2. **Unwanted counterpart.** For every happy-path REQ: "and if that goes wrong / arrives in the wrong state?" → a counterpart REQ with `ears.unwanted: true` (+ its trigger). This is where most missed requirements live.
  3. **Boundary semantics.** Whenever a REQ references a CON: "at exactly N, or after N?" Encode the answer in the response ("locks the account **on the 5th** failed attempt"), not just the constant — off-by-one is the classic wrong-rule bug witness traces exist to catch; settle it before formalizing.
  4. **Quantifier scope.** Whenever the response touches a collection: "per user, per session, or globally?" ("at most one active session" — per user or system-wide?). Second-most-common ambiguity after boundaries, and it changes the shape of the Quint state (`int` vs `UserId -> int`); settle it before formalizing.

  Store the fields in `requirements[].ears`; render `description` from them ("While `<state>`, when `<trigger>`, the system shall `<response>`."). A requirement that can't be expressed in the fields is usually two requirements or a vague one — split or sharpen. An answer with no trigger/state ("always true") is an invariant — capture it as INV-NNN, not REQ-NNN.
- **Invariants**: "what must always be true?" Capture INV-NNN candidates with criticality.
- **Constraints**: numeric thresholds, bounds, max/min — capture CON-NNN (with units!).
- **Non-functional requirements**: when the user says "fast", "secure", "scalable", capture a REQ with `type: "non-functional"` and immediately pin its `fit_criterion` — metric (what's measured, with units), target (the bound), measurement (how/where it's checked). "Fast" is not a requirement until all three exist; `spec-lint` FAILs an NFR without them past raw status. NFRs carry no witness obligation (nothing reachable to demonstrate) — the fit criterion IS their precision mechanism.
- **Decisions**: architectural choices being made, with alternatives.
- **Open questions**: anything the user can't answer yet; mark `Q-NNN` `status: open`.

**Early matrix pass**: as soon as `state_machines[]` and a first batch of REQs exist, run `tools/spec-matrix.py <target>` and triage the `?` cells in this conversation — classify into the GAP / IMPOSSIBLE / OUT-OF-SCOPE buckets defined in `/spec-check` Step 4a, recording each verdict in the area JSON's `matrix_triage[]` (committed — decisions in the gitignored CSV don't survive a clone). A gap found now, while the user is describing the domain, becomes a REQ in one exchange; the same gap found later by `/spec-check` becomes a stale entry in the Q-NNN queue. Same tool, earlier moment.

**Closing gap sweep**: when the clusters are exhausted (before formalizing), run one short red-team moment while the user is still in the conversation. From `/spec-check` Step 4b's category list, keep only the categories this domain plausibly touches; ask at most ~8 questions whose answer would add or change a requirement, each citing the REQ/INV/entity it touches (or "absent"). Answers become REQs/INVs/CONs in the same exchange; what the user can't answer becomes a Q-NNN. Same logic as the early matrix: a gap found now is a requirement in one exchange. The full `--reality` pass at check time is for depth — it shouldn't be the first time these questions are asked.

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
| Functional REQs not referenced by any journey step (`.spec/journeys/*.json`) | **journey pass**: walk the unassigned REQs, ask which flow each belongs to and where in it ("what happens right before/after?"); new flows get a `.spec/journeys/<slug>.json`. Brownfield extracts land here — group extracted REQs into flows during the confirm pass. |
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
- **Update the change manifest**: any ID added or modified in `specs/<target>.json` goes into the manifest target's `ids[]`. (No phase flags to maintain — staleness is automatic: editing the spec bumps `last_modified`/changes the model sha, which un-derives "checked"/"verified".) When a touched area is spanned by a contract, add that contract to `targets[]` with `auto: true` if not already present. Status `open` → `in-progress` on first spec edit.
- Commit hint: at sensible checkpoints, suggest `git add specs/<target>.json specs/<target>.qnt .spec/changes/<change>.json && git commit -m "spec(<change>): <what>"`

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
