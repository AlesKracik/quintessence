# /spec — Adaptive Spec Authoring

The single entry point for spec work. Detects the current state of the project and the named target, then walks the relevant conversational beat: project setup, area elicitation, vocabulary, structuring, formalization, brownfield extraction, drift codification, catalog editing. Writes `specs/<target>.*.json` and its `.qnt` sidecar.

There are no other authoring subcommands — this command subsumes every authoring phase (init, elicit, structure, formalize, reconcile, approve, …); don't invent `/spec-<phase>` names. The only other commands are the four action commands: `/spec-check`, `/spec-code-verify`, `/spec-code-generate`, `/spec-readback`.

## Usage
```
/spec                          # resumes the active change: dashboard + suggested next step; bootstraps if no project yet
/spec <target>                 # work on an area (existing or new) within the active change. target = area slug or "_project"
/spec <target> -- <hint>       # supply NL hint up front to skip an opening question
/spec change <slug> [-- intent]  # open a new change or switch to an existing one
/spec _overview                # project overview: areas, open changes, status
```

**File naming:** every spec JSON carries its type in the filename — `specs/<name>.area.json`, `specs/<name>.contract.json`, `specs/changes/<slug>.change.json`, `specs/journeys/<slug>.journey.json`. `specs/<target>.*.json` below means the target's `.area.json` or `.contract.json` file (the suffix must match the JSON's `kind`; `spec-lint` enforces it).

**The change is the unit of work; the area is the unit of meaning.** Every spec edit happens inside a *change* — a manifest at `specs/changes/<slug>.change.json` (schema: `schemas/change.schema.json`) that references the areas/contracts it touches. Areas remain the logical spec boundary and the single source of truth; the manifest holds membership and IDs only — never spec content, never phase flags (status is derived; see the dashboard beat).

The **active change** is per-dev sticky state: `last_change` in `.spec/local.json` (gitignored). All five spec commands resolve against it — run bare to continue the change, pass an explicit target for a one-off. If an area edit starts with no active change, auto-open one: ask "No active change. Name this work? [<area>-updates]" — Enter accepts the default. One question, then every spec diff is traceable to a manifest.

## Instructions

You are the **Adaptive Specifier**. Your job is to figure out what beat the user needs and walk them through it — not to ask a fixed sequence of questions. The user should never need to remember "am I in elicit or structure phase?" — you read the area JSON and infer.

### Step 1 — Resolve change and target, read state

Resolve the change, then the target:

1. `/spec change <slug>` → open `specs/changes/<slug>.change.json` (create per `schemas/change.schema.json` if new, with `intent` from the `--` hint or one question; suggest branch `change/<slug>`). Write `last_change: "<slug>"` to `.spec/local.json` (create the file if missing; preserve other fields). Then show the change dashboard (beat below).
2. Bare `/spec`, active change valid (`last_change` set, manifest exists, status not `landed`/`abandoned`) → **change dashboard** beat.
3. Explicit `<target>` (area, not `_project`/`_patterns/*`/`_protocols/*`/`_journeys/*`/`_overview`):
   - active change exists → work on that area within it; register the target in the manifest's `targets[]` if absent.
   - no active change → auto-open one first: `No active change. Name this work? [<target>-updates]` (Enter = default). Create the manifest, set `last_change`, then proceed.
4. Bare `/spec`, no valid active change: exactly one area → treat as `/spec <that-area>` (rule 3 auto-opens a change); otherwise show the project overview and ask.
5. `_overview` → project overview: areas with status, open changes (slug, intent, target count, phase summary), open questions, last verification. Catalog targets (`_project`, `_patterns/*`, `_protocols/*`, `_journeys/*`) run their beats without touching any change.

Then determine what exists:

1. `.spec/project.json` — does the project exist? (If not, the **bootstrap** beat runs regardless of target resolution.)
2. For the resolved target:
   - `specs/<target>.*.json` — does the area exist?
   - `specs/<target>.qnt` — does the formal model exist?
   - For areas with `code_repo` set: does the code path on disk exist? (Resolve via `.spec/local.json`.)

This determines the entry beat:

| State | Beat |
|---|---|
| No `.spec/project.json` | **bootstrap**: walk project setup |
| `<target>` is `_project` | **project edit**: architecture defaults, repos, topology |
| `<target>` is `_patterns/<name>`, `_protocols/<name>`, or `_journeys/<name>` | **catalog edit**: add/edit a catalog file |
| `specs/<target>.*.json` missing, code exists at the area's `code_path` | **brownfield extract** |
| `specs/<target>.*.json` missing, no code | **greenfield elicit** |
| `specs/<target>.*.json` exists, code at `code_path` changed since the last extraction | **re-extract**: reconcile the spec against the code as it is now |
| `specs/<target>.*.json` exists, sections incomplete | **resume**: pick up the next phase |
| `specs/<target>.*.json` exists, `verification_log` shows drift | **drift codify**: walk the drift items |
| `specs/<target>.*.json` exists, all phases complete | **review/idle**: present the readback, offer next action |

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

The grid is **computed, never stored** — the manifest holds membership only, so status can't go stale. Don't derive it by hand:

```bash
tools/spec-readback.py status <slug> --json
```

returns the derived phase grid (spec / checked / applied / verified per target — rules documented in the tool's docstring and METHODOLOGY → "Unit of Work: Changes"). Render it as the table above and add the suggested next step.

When every target is checked and every code target verified, offer: "All green. Mark `landed` after the PR merges, or I can mark it now if it's already in." `landed`/`abandoned` clears `last_change`.

#### bootstrap — first-time project setup

(Runs when there's no `.spec/project.json`.)

Ask only what's needed to start eliciting:

1. Project name (slug).
2. Greenfield or existing code?
3. Repo layout: single-repo (code lives here), multi-repo (code in separate repos), or spec-only. If multi-repo: for each code repo, logical name + URL + default branch → `.spec/project.json` `repos`; prompt user to add per-dev paths to `.spec/local.json` (or do it for them).
4. Functional areas to specify (comma-separated names). For each: kind (area / contract — an interactive surface is just an area that declares `screens[]` + `navigation[]`), one-line description, and — if it has code — code repo, `code_path`, `tests_path`, `test_command`. Write each to the `areas[]` index.

**Don't ask about architecture defaults, topology, or Apalache settings here.** Each has a working default and a natural later moment: architecture is collected when `/spec-code-generate` first needs it (it asks for missing fields and writes them back) or anytime via `/spec _project`; topology when there are 2+ deployment units. Apalache settings need no moment at all: the defaults carry a two-pass step ladder (`shallow_steps` 3, `max_steps` 10) and a run budget (`budget_seconds` 900), so a check's cost is declared up front rather than discovered by waiting for it. Don't raise it here — the point is that the default is safe, not that it wants configuring. Front-loading them spends the user's attention before a single requirement is captured — requirements are where that attention pays.

Write `.spec/project.json`. Scaffold each declared area's `specs/<name>.<kind>.json` as a minimal skeleton with just `kind`, `area`, `version: "0.1.0"`, `status: "raw"`, and `formal_model: {"quint_file": "<name>.qnt"}` — the pointer names where `/spec-check` will write the sidecar, so it is aimed before the file exists. Lint WARNs about the missing sidecar while the area is `raw`/`draft` and only FAILs from `in-review` on, so a freshly bootstrapped project lints clean.

5. **Open the first change** — the change is the unit of work, so bootstrap ends inside one, not before one. Ask: `Name the first change? [initial-spec]` (Enter = default; intent defaults to "Initial specification of <area list>"). Create `specs/changes/<slug>.change.json` per `schemas/change.schema.json` with every declared area as a target (`status: "open"`, empty `ids[]`), write `last_change` to `.spec/local.json`, and suggest branch `change/<slug>`.

Install pre-commit hook via `bash tools/setup-hooks.sh` (idempotent).

Print: "Project initialized, change `<slug>` open. Next: `/spec <area>` for each area you declared — edits land in the change; bare `/spec` shows the dashboard."

#### project edit — `.spec/project.json`

(Runs on `/spec _project`.)

Show current project config; ask which section to edit. Sections: repos, areas index, architecture defaults, topology, Apalache settings. Walk only the chosen section.

#### catalog edit — patterns/protocols/journeys

(Runs on `/spec _patterns/<name>`, `/spec _protocols/<name>`, or `/spec _journeys/<name>`.)

For journeys (`schemas/journey.schema.json`, files at `specs/journeys/<name>.journey.json`): a journey is THE use-case mechanism — a named user-visible flow, steps as qualified `<area>.<ID>` refs in temporal order; most live inside one area, some cross boundaries, same shape either way. Journeys are born where the flow is: in elicitation, one story told is one journey; in brownfield, one reachable entry point is one journey, deduced from the call graph and updated on re-extraction. This beat is for stitching or editing them directly. Creating one: ask for the actor and the story end to end, then map each step to an existing REQ (offer candidates from the areas' requirements); a step with no matching REQ is a gap — capture it in the owning area first (`/spec <area>`), then finish the journey.

If the file doesn't exist: walk creation per `schemas/pattern.schema.json` or `schemas/protocol.schema.json`. If it exists: show contents, ask which fields to update. Write back. Don't modify any area JSON references (the user opts those in separately).

If the user types `/spec _patterns`, `/spec _protocols`, or `/spec _journeys` (no name): list cataloged entries with one-line descriptions and ask which to edit (or "new").

#### brownfield extract

(Runs when `specs/<target>.*.json` is missing AND code exists at the area's `code_path`.)

Tell the user: "No spec for `<target>` yet, but code exists at `<resolved-code-path>`. I'll extract a draft spec."

**Brownfield is the strong case, not the awkward one.** Greenfield elicitation has only the user's memory to work from. Here there is a running implementation that answers any question you ask it — every threshold, every branch, every error path is already decided and readable. Extraction should therefore produce a *stronger* spec than elicitation, not a weaker one.

**What you are producing is a spec that is true of this code.** That is the deliverable and its whole value: the team can review behavior nobody wrote down, reason about a change before making it, and read in the readback what the system does today. Nothing has to be regenerated for that to pay off, so do not steer the user toward a rewrite they did not ask for, and do not open the beat by asking them to design a substitution boundary. If they *are* rewriting, there is machinery to measure how completely the spec captured the code — offer it at the end, as step 6.

Extraction is also not a one-time event. Say so when you finish: the spec is true of the code as of today, and `/spec <target>` re-extracts when the code moves on.

##### 1. Read fields out of the code, not prose

The code already made these decisions. Extract them as **fields**, not as descriptions:

| In the code | Becomes | Why it matters for fidelity |
|---|---|---|
| numeric/string literal in a guard | `CON-NNN` + `paired_invariant` | The threshold AND its other side; `>= 5` regenerated as `> 5` is invisible without both |
| branch condition | `ears.state` / `ears.trigger`, phrased with declared state names | An unspecified branch cannot be regenerated |
| `catch` / error branch per dependency | `externals[].outcomes[]` + `error_outcomes[]` (with `idempotent`) | Failure behavior is where extracted specs are thinnest |
| enum / union / status column | entity `states[]` + `closed: true` | Stops regeneration inventing a fourth state |
| early return / throw guard | `modality: "forbidden"` + `refusal.artifact` | Rejections have no witness; without an artifact nothing checks them |
| a choice the code makes that clients see (error codes, ordering, ID format) | `DEC-NNN` with `affects[]` | Legitimate latitude the spec should allow, already spent — pin it or regeneration will pick differently |
| a choice genuinely free | `modality: "may"` + one outcome per branch | Do not let the extracted branch narrow a permission into a rule |

Apply the four capture-time checks **against the code rather than the user** — the answers are all in there:

1. **Witness test.** Can you write a `witness.predicate` bound to the action's parameters? If not, you have not understood the function well enough to specify it.
2. **Unwanted counterpart.** Every error path in the source is an unwanted-behavior requirement someone already wrote. Read them out.
3. **Boundary semantics.** The code *knows* whether it is `>=` or `>`. Do not ask; read it, and record it in the response ("locks on the 5th failure").
4. **Quantifier scope.** The data structure answers it: `Map<UserId, int>` is per-user, a bare `int` is global.

Mark every extracted item `source: "extracted"`, `status: "needs-validation"`, and fill `extraction`:

```json
"extraction": { "evidence": "authService.ts:78-91", "confidence": "high", "inferred_by": "agent" }
```

`confidence: "low"` is the honest label when the code was ambiguous and you guessed — the readback surfaces it and lint flags it at review. Guessing silently is what makes an extracted spec untrustworthy.

##### 2. Account for the code you did NOT specify

Run `tools/spec-extract-audit.py <target> --emit --record`. **`--record` is not optional.** Without it the audit prints to the terminal and nothing is written down: `check_results.extraction` stays absent, and the readback, the ship verdict and lint all read absent as "nothing to say". That is how an area reaches full witnesses, full invariants, zero untriaged matrix cells and zero lint failures with most of its code unaccounted for. The number has to land in the JSON or it does not exist.

It enumerates the decision sites — branches, guard literals, error handlers, early exits — and prints triage stubs for every one no spec element claims.

This is the only check in the framework that runs **code → spec**, and it is the one that matters here: the reason a regenerated implementation diverges is almost always a branch nobody wrote down, and nothing spec-shaped can look for a branch the spec does not mention. Give every site a verdict in `extraction_triage[]`:

- `MAPPED` (+ `maps_to`) — realizes these spec ids.
- `NOT-BEHAVIOR` — logging, metrics, tracing.
- `DEFENSIVE` — unreachable by construction, kept as a belt.
- `DEAD` — unreachable. A finding about the code; say so.
- `GAP` (+ `Q-NNN`) — real behavior nobody specified. The extraction hole.
- `OUT-OF-SCOPE` (+ `scope_ref`) — outside the declared boundary.

Sites are keyed by fingerprint, not line number, so the ledger survives reformatting. Work through the GAPs with the user; they are the highest-value questions in the whole beat.

##### 3. Deduce the journeys from the code

The flows are in the code too, and reading them there is cheaper than asking someone to recall them. In greenfield a journey is born at capture time — one story told is one journey file, because the story already arrives with a name and an order. Here the order is in the call graph: every entry point a user or client can reach (route handler, CLI command, public method, queue consumer, scheduled job) starts one flow, and what it calls, in the order it calls it, is that flow.

Write one `specs/journeys/<slug>.journey.json` per entry point worth naming. Name it for what the actor is doing, not for the handler (`sign-in`, not `postAuthLogin`), and append the requirements extracted in step 1 to `steps[]` as qualified `<area>.<ID>` refs in call order — each happy-path step followed by the error branches that step can take, which are already requirements (the unwanted counterparts read out of the error paths). A call into another area stays one journey; a step whose REQ does not exist yet is the same signal it is in elicitation — a gap, captured in its owning area before the journey lints clean.

The journey carries no confidence field of its own: it is references only, and every step it points at already carries its `extraction.confidence`. What it does need is honesty about order — where the sequence is dynamic (dispatch table, event bus, retry loop), say so in the step `note` instead of inventing one.

Re-extraction **updates** journeys, it does not duplicate them: match the existing file by name, re-derive `steps[]` from the current call order, and report added, removed and reordered steps along with the rest of the extraction diff. A journey whose entry point no longer exists is a finding — surface it, do not silently delete it.

##### 4. Harvest examples instead of inventing them

Ask for real call sequences — from logs, from existing tests, from a recording session. Each becomes an `examples[]` entry with `source: "extracted-from-production"` and, where a recording exists, `trace` pointing at the ITF file. Greenfield examples are guesses about what matters; harvested ones are evidence, and they replay through the same machinery as a witness trace.

##### 5. Keep it true as the code moves

**Stamp what you read it from**, before telling the user anything about keeping it current:

```bash
tools/spec-record.py stamp <target> --extracted --code-path <subtree>
```

That records `extracted_from` — the code repo's git sha and the subtree you read. One line in the area JSON, and the thing that makes re-extraction able to say *what changed and since when* instead of only *which fingerprints are new*. Do it now: the sha you need is the one you just read, and it is unrecoverable later. If the code repo is not a git repo, the tool refuses and says so — carry on without it rather than inventing a value.

Then tell the user how to keep the spec current, because an extracted spec that is never revisited becomes a confident description of a system that no longer exists. `/spec <target>` on an area whose code has changed since extraction routes to **re-extract** — no `/spec-code-verify`, adapter or test command needed first. Re-stamp at the end of each reconciliation, so the next one has a fresh baseline.

##### 6. Then formalize — and offer fidelity measurement only if it fits

Write `specs/<target>.*.json` and the Quint sidecar. Present extracted items in batches for confirm/edit/discard — with `extraction.evidence`, the user can jump to the code instead of reconstructing your reasoning.

End with the ladder:

```
Draft spec written from <n> files. <m> sites triaged, <k> GAPs open.
<j> journeys deduced from the entry points.

  /spec-check <target>     the model holds and nothing is vacuous
  /spec-code-verify <target>    the code conforms to the spec
  /spec <target>           re-extract when the code moves on

The spec now describes this code. Keep it that way and it stays worth reading.
```

Then, and only if the user has said they are rewriting, re-platforming or porting this area, offer the fidelity measurement as a separate thing:

```
Planning to rebuild this area? Fidelity can be measured rather than assumed:
declare a `boundary`, then

  /spec-code-generate <target> --parallel   regenerate beside the original
  spec-record equiv <target>        drive BOTH through the same sequences

That answers "is this spec complete enough to rebuild from?" — a different
question from the ones above, and only worth the cost if you are rebuilding.
```

##### Optional: declare a substitution boundary

Only for an area being rebuilt — skip it otherwise. `boundary` is what makes "the same" mean something, and the differential comparator has nothing to diff without it:
Before extracting behavior, ask what "the same" would mean. Fill `boundary`:

- **entry_points** — the callable surface clients depend on, with argument shape.
- **observable_state** — what a caller can read back, and therefore can notice changing. These become the differential comparator's diff targets and usually mirror the conformance adapter's getters.
- **emits** — events, webhooks, audit records: observable whether or not anyone calls them API.
- **persistence_contract** — is the stored representation part of the contract? The one people forget: behavior can match perfectly while a regenerated schema orphans every existing row. Answer even when the answer is "none, the store is private".
- **free** — what a replacement may legitimately do differently: file layout, log wording, internal structure, algorithm. Naming this is what keeps the spec from becoming a transliteration.

#### greenfield elicit

(Runs when `specs/<target>.*.json` is missing AND no code exists.)

Standard elicitation, organized as conversational clusters (not a rigid order — pick what's needed first):

- **Purpose**: one sentence on what this area is for.
- **Domain vocabulary**: entities (with states if applicable), actors, verbs.
- **State machines**: any entity captured with `states[]` → run the state-machine beat (below) right away, not at resume. The early matrix pass needs `state_machines[]` to exist, and transition capture surfaces missing behaviors while the user is still describing the domain.
- **Behavior (EARS-structured)**: ask for scenarios as stories ("walk me through a login — then walk me through one going wrong"), not field-by-field. **Each story is a journey**: write `specs/journeys/<slug>.journey.json` (name, primary actor, one-line description) and append every REQ drafted from that story to `steps[]` as qualified `<area>.<ID>` refs in the order the story told them — happy-path step, then its unwanted counterparts. This costs nothing at capture time (the story already has a name and an order) and is what makes the readback digestible; reconstructing flows later from a flat ID list is guesswork. A story that crosses into another area stays one journey — if the foreign step's REQ doesn't exist yet, note it as a placeholder and capture it in its owning area before the journey lints clean. From each story, **draft the EARS fields yourself**, render the sentence, and read drafts back in batches of ~5 for the user to confirm or correct — confirm-and-correct converges faster and more accurately than interrogation. The pattern is derived from which fields are filled; never ask the user to pick one. Use targeted questions only for fields the story left open:
  - trigger unclear → "What kicks this off?" → `ears.trigger`
  - state unclear → "Always, or only in some state?" → `ears.state`. Phrase it using the entity's **declared state names** ("the Session is Active", not "the user is logged in") — it makes the requirement↔state-machine link reviewable and matrix triage mechanical.
  - `ears.response` is always required.

  Four checks **at capture time** — each is one question and each kills a class of wrong or missing requirement:
  1. **Witness test for vagueness.** If you can't sketch a `witness.predicate` for the response (an observable state change or output), the response is too vague to ever be witnessed or verified — sharpen it now ("handle errors gracefully" → what state results, visible where?). `spec-lint` backstops this with an ambiguous-wording WARN, but the cheap moment to fix it is now.
  2. **Unwanted counterpart.** For every happy-path REQ: "and if that goes wrong / arrives in the wrong state?" → a counterpart REQ with `ears.unwanted: true` (+ its trigger). This is where most missed requirements live.
  3. **Boundary semantics.** Whenever a REQ references a CON: "at exactly N, or after N?" Encode the answer in the response ("locks the account **on the 5th** failed attempt"), not just the constant — off-by-one is the classic wrong-rule bug witness traces exist to catch; settle it before formalizing.
  4. **Quantifier scope.** Whenever the response touches a collection: "per user, per session, or globally?" ("at most one active session" — per user or system-wide?). Second-most-common ambiguity after boundaries, and it changes the shape of the Quint state (`int` vs `UserId -> int`); settle it before formalizing.

  **Then distil the meaning.** For every requirement past `raw`, write `requirements[].meaning` — one or two plain sentences saying what the requirement *means* to someone who has never seen the code — and pin it: `tools/itf_tools.py meaning-sha <area> --req <ID>` → `meaning.written_against`, `author: "agent"`. This is the sentence the readback leads with; the EARS sentence moves into the collapsed details beside the Quint action.

  It is a distillation, not a rewording. The EARS fields speak the system's vocabulary and will name identifiers, fields and exception classes — that is correct there, and the Quint model needs them. But "If an update request carries a `serviceLevelPolicyId` different from the persisted one, then the system shall refuse with `NonMatchingTopologyException` on `serviceLevelPolicyId`" is a sentence only the implementation can check; a reviewer nodding at it is nodding at a name. The meaning says what actually holds: "If an update asks for a different service-level policy than the one already stored, the system refuses the change and leaves the stored policy in place." Keep declared state, screen and entity names — those are the shared vocabulary, not leakage — and keep CON names, which the readback resolves to their values inline. Drop nothing the requirement asserts; a meaning that is vaguer than the EARS fields is a worse sentence, not a plainer one. **Re-pin whenever you touch the requirement's EARS fields, modality, type or fit criterion** — a stale meaning is not rendered at all, and lint FAILs it from `in-review` on. Never rewrite one whose `author` is `human` without asking.

  Store the fields in `requirements[].ears`; render `description` from them ("While `<state>`, when `<trigger>`, the system shall `<response>`."). A requirement that can't be expressed in the fields is usually two requirements or a vague one — split or sharpen. An answer with no trigger/state ("always true") is an invariant — capture it as INV-NNN, not REQ-NNN.
- **Invariants**: "what must always be true?" Capture INV-NNN candidates with criticality. If an invariant is purely **relational** — cardinality, referential integrity, ownership, "no two X share a Y" — and the area declares `formal_model.alloy_file`, it can carry `proof: "structural"` + `alloy_command` and be checked by Alloy over a finite scope instead of by Apalache over traces (METHODOLOGY.md → "The Structural Backend: Alloy"). Purely arithmetic/data invariants can carry `proof: "smt"` + `smt_file` for z3. Only offer either on an area that already uses that backend; never introduce a second sidecar language on your own initiative.
- **Scope**: "what is this area explicitly NOT responsible for?" Capture `scope.included[]` and `scope.excluded[]` with a reason each. Ask early — it is the boundary every later completeness claim is relative to, and it gives OUT-OF-SCOPE triage something real to point at.
- **Externals**: "what does this area call, and what can each of those return when it goes wrong?" Capture `externals[]` with the FULL outcome set — DECLINED, TIMEOUT, NETWORK_ERROR, not just the happy one. Then, per requirement, `error_outcomes[]`: what happens on each, and whether a retry is safe. Anything the spec relies on but does not establish goes in `assumptions[]` as `ASM-NNN` with `affects[]`.
- **Modality**: for each requirement, is it required (`must`), merely permitted (`may` — then capture a predicate per permitted outcome in `witness.outcomes[]`), or prohibited (`forbidden` — then name the invariant that enforces it in `witness.enforced_by`)? Ask whenever the answer is "it could do either" — that is a `may`, and recording it as a `must` is how latitude gets lost. If a requirement is discharged in prose instead of by a predicate, `witness.justification` alone does nothing: a discharge is read only at `witness.status: "skipped"`, so write both or the requirement is simply unwitnessed (`justification-without-skip`).
- **Closed worlds**: when an entity's `states[]` is exhaustive, set `closed: true`. It holds the sidecar's variant type to exactly that list, which is what stops a model or an implementation quietly growing a `Paused`. If an invariant is purely **relational** — cardinality, referential integrity, ownership, "no two X share a Y" — and the area declares `formal_model.alloy_file`, it can carry `proof: "structural"` + `alloy_command` and be checked by Alloy over a finite scope instead of by Apalache over traces (METHODOLOGY.md → "The Structural Backend: Alloy"). Only offer this on contracts that already use the backend; never introduce a second sidecar language on your own initiative.
- **Constraints**: numeric thresholds, bounds, max/min — capture CON-NNN (with units!).
- **Non-functional requirements**: when the user says "fast", "secure", "scalable", capture a REQ with `type: "non-functional"` and immediately pin its `fit_criterion` — metric (what's measured, with units), target (the bound), measurement (how/where it's checked). "Fast" is not a requirement until all three exist; `spec-lint` FAILs an NFR without them past raw status. NFRs carry no witness obligation (nothing reachable to demonstrate) — the fit criterion IS their precision mechanism.
- **Decisions**: architectural choices being made, with alternatives.
- **Open questions**: anything the user can't answer yet; mark `Q-NNN` `status: open`.

**Early matrix pass**: as soon as `state_machines[]` and a first batch of REQs exist, run `tools/spec-matrix.py <target>` and triage the `?` cells in this conversation — classify into the GAP / IMPOSSIBLE / OUT-OF-SCOPE buckets defined in `/spec-check` Step 4a, recording each verdict in the area JSON's `matrix_triage[]` (committed — decisions in the gitignored CSV don't survive a clone). A gap found now, while the user is describing the domain, becomes a REQ in one exchange; the same gap found later by `/spec-check` becomes a stale entry in the Q-NNN queue. Same tool, earlier moment.

**Closing gap sweep**: when the clusters are exhausted (before formalizing), run one short red-team moment while the user is still in the conversation. From `/spec-check` Step 4b's category list, keep only the categories this domain plausibly touches; ask at most ~8 questions whose answer would add or change a requirement, each citing the REQ/INV/entity it touches (or "absent"). Answers become REQs/INVs/CONs in the same exchange; what the user can't answer becomes a Q-NNN. Same logic as the early matrix: a gap found now is a requirement in one exchange. The full `--reality` pass at check time is for depth — it shouldn't be the first time these questions are asked.

Write to `specs/<target>.*.json` as you go. After enough is captured, draft a Quint module in `specs/<target>.qnt` (structure convention: `templates/spec.qnt.template`) — the EARS fields map mechanically: `trigger` → action, `state` → `require` guard, `response` → effect. While formalizing, also draft each requirement's `witness.predicate` (the Quint boolean over state that's true exactly when the behavior has happened — `/spec-check` uses it to produce the witness trace). Show the module for confirmation; offer `/spec-check` next.

#### resume — pick up the next phase

(Runs when `specs/<target>.*.json` exists but some sections are incomplete.)

Inspect the JSON for gaps:

| Gap | Suggested beat |
|---|---|
| `purpose` empty | mini-elicit (one question) |
| `concepts` empty or shallow | vocabulary cluster |
| Entities with `states[]` but no matching `state_machines[]` entry | state-machine beat (see below) |
| `requirements[]` has items without IDs (raw strings) | structure pass |
| `requirements[]` items have `status: "raw"` | elicit refinement per item |
| Functional REQs not referenced by any journey step (`specs/journeys/*.journey.json`) | **journey pass**: walk the unassigned REQs, ask which flow each belongs to and where in it ("what happens right before/after?"); new flows get a `specs/journeys/<slug>.journey.json`. Brownfield journeys are deduced from the call graph during extraction, so what lands here is the leftovers — requirements no entry point walked through. |
| Requirements past raw status without `ears` structure | EARS pass: walk each one, fill trigger/state/response (+unwanted) |
| Requirements without `witness.predicate` (and sidecar exists) | witness pass: draft predicates, confirm, then suggest `/spec-check` |
| `formal_model.quint_file` set but file missing | formalize: write the sidecar |
| `formal_model.quint_file` exists but `check_results` shows failures | check: re-run, address counterexamples |
| `traceability[]` empty but code exists | suggest `/spec-code-generate` |
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

Writing state_machines[<Entity>] in specs/<target>.*.json.
```

After writing, suggest running `spec-lint` (or `/spec-check`) — the state-machine lints fire immediately if the declared structure conflicts with the Quint sidecar.

Tell the user what you noticed and what you propose to work on next; let them confirm or redirect.

#### re-extract

(Runs when `specs/<target>.*.json` exists, the area has a `code_path`, and the code there has changed since the spec was extracted or last reconciled — compare `extraction_triage[]` fingerprints and `extraction.evidence` against the current sources. Offer it, do not force it: say what looks stale and ask.)

An extracted spec is true of the code on the day it is written and decays from there. This beat exists so keeping it true is a routine, not a project. It is deliberately reachable **without** `/spec-code-verify`, `traceability[]`, a conformance adapter or a test command — those check code against a spec, which is a different job, and requiring them first is what would stop anyone from doing this at all.

Tell the user: "`<target>`'s spec was extracted against code that has changed. I'll re-read `<resolved-code-path>` and show you what no longer matches."

1. **Ask what moved, then ask which sites are new.** `tools/spec-record.py changed <target>` first: if the area was stamped (`extracted_from`), this diffs the code the spec was actually read from against HEAD and shows you the changed files, narrowed to the extracted subtree and to the files `traceability[]` claims to describe. It answers *what changed and since when* — which a set of fingerprints cannot, because the ledger does not store the previous text. If it reports no baseline, say so and carry on with step 1b; the audit still works, it just cannot tell you when anything moved.

   1b. **Re-run the audit.** `tools/spec-extract-audit.py <target> --emit --record` — `--record` every time, so the coverage number in `check_results.extraction` tracks the code instead of freezing at whenever someone last remembered the flag. A rising unclaimed count between runs IS the drift signal: the code grew behavior the spec has not caught up with. Fingerprints are stable across reformatting, so what surfaces is real movement: decision sites the ledger has never seen, and ledger entries matching no current site — the code moved out from under a triaged decision. The diff says what moved; the fingerprints say which decision sites are new. Use both.

2. **Re-read the fields that carry literals.** Every `CON-NNN` whose value came from a guard, every `closed: true` state set, every `externals[].outcomes[]` read out of a `catch`. These drift silently: a threshold changes in code and the spec keeps asserting the old number, which is worse than saying nothing because `spec-check` will happily prove things about it.

3. **Walk each difference with the user.** Three resolutions, and naming which one it is matters more than the edit:

   | The code and the spec disagree because | Resolve as |
   |---|---|
   | behavior deliberately changed and nobody updated the spec | update the spec; if a decision moved with it, record or amend the `DEC-NNN` |
   | the spec was always wrong here | correct it — the original extraction was incomplete, not the code |
   | the code is wrong | leave the spec alone and file the finding against the code |

   The third is why this is worth doing at all: reconciling against an accurate spec is how an extracted spec starts finding bugs instead of just recording them.

4. **Re-stamp what you touched.** Updated items get fresh `extraction.evidence` and honest `confidence`; anything whose behavior changed goes back to `status: "needs-validation"`. Editing a requirement invalidates its witness freshness automatically (the model sha moves), so `/spec-check` is the natural next step — say so.

5. **Land it in the change.** Re-extraction is a spec edit like any other: register the target in the active change's `targets[]` and add every touched ID to `ids[]`.

Finish with what moved, not just a count: "3 requirements updated, 1 new GAP (Q-004), 1 constraint whose code value changed — CON-002 said 5, the guard says 3."

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

After walking all drift items, suggest: `/spec-check <target>` (the new spec still needs Apalache), then `/spec-code-verify <target>` (should pass now).

#### review / idle

(Runs when everything looks complete.)

Print a digest:

- Purpose, kind, version, last verified
- Counts: REQ / INV / PROP / CON / DEC / Q
- Open questions still open
- Architecture summary (resolved with project defaults)
- Last verification result
- Suggested next action (`/spec-code-verify` if it's been a while; `/spec-check` if Quint was edited; nothing if all green)

### Step 3 — Always write incrementally

Don't accumulate state in memory. After each meaningful turn:

- Update `specs/<target>.*.json` (or `.spec/project.json`, catalog file, etc.)
- Update `last_modified`
- Bump `version` only when the user signals a meaningful change (added requirement, modified invariant, etc.) — minor for additions, patch for refinements, major for breaking changes
- **Update the change manifest**: any ID added or modified in `specs/<target>.*.json` goes into the manifest target's `ids[]`. (No phase flags to maintain — staleness is automatic: editing the spec bumps `last_modified`/changes the model sha, which un-derives "checked"/"verified".) When a touched area is spanned by a contract, add that contract to `targets[]` with `auto: true` if not already present. Status `open` → `in-progress` on first spec edit.
- Commit hint: at sensible checkpoints, suggest `git add specs/<target>.*.json specs/<target>.qnt specs/changes/<change>.change.json && git commit -m "spec(<change>): <what>"`

### Step 4 — Suggest next action

End every turn with a concrete suggested next command or beat. Examples:

- "I've drafted the formal model. Next: `/spec-check auth`."
- "Three items still need invariants. Want to keep going, or check Apalache on what we have?"
- "Looks complete. Next: `/spec-code-verify auth` to make sure code matches."

Never just stop — always offer the next move.

### What `/spec` doesn't do

- **Doesn't run Apalache.** That's `/spec-check`.
- **Doesn't run tests.** That's `/spec-code-verify`.
- **Doesn't generate code.** That's `/spec-code-generate`.
- **Doesn't generate the human-readable review document.** That's `/spec-readback`.
- **Doesn't enforce a workflow.** No propose/approve/sync gates. Git + PRs are your workflow.
