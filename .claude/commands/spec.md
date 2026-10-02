# /spec — Adaptive Spec Authoring

The single entry point for spec work. Detects the current state of the project and the named target, then walks the relevant conversational beat: project setup, area elicitation, vocabulary, structuring, formalization, brownfield extraction, drift codification, catalog editing. Writes the area's three files: `specs/<target>.qnt` (the model, with every record as a `///` doc comment), `specs/<target>.intent.json` and — through the tools only — `specs/<target>.records.json`.

There are no other authoring subcommands — this command subsumes every authoring phase (init, elicit, structure, formalize, reconcile, approve, …); don't invent `/spec-<phase>` names. The only other commands are the four action commands: `/spec-check`, `/spec-code-verify`, `/spec-code-generate`, `/spec-readback`.

## Usage
```
/spec                          # resumes the active change: dashboard + suggested next step; bootstraps if no project yet
/spec <target>                 # work on an area (existing or new) within the active change. target = area slug or "_project"
/spec <target> -- <hint>       # supply NL hint up front to skip an opening question
/spec change <slug> [-- intent]  # open a new change or switch to an existing one
/spec _overview                # project overview: areas, open changes, status
```

**Where the spec lives (Quint-first).** An area is three files, each the only source of what it holds (full grammar: the docstring of `tools/spec_source.py`; rationale: METHODOLOGY.md "Where the Spec Lives"):

| File | Holds | Who writes it |
|---|---|---|
| `specs/<target>.qnt` | the formal model **and** every record checkable against it — requirements, invariants, properties, constants, examples, screens, navigation edges — as `///` doc comments on the declaration that realizes each one | you, in this command |
| `specs/<target>.intent.json` | what a model cannot say: `kind`, purpose, brief, scope, boundary, concepts, externals, assumptions, decisions, open questions, state machines, `ui_components`, architecture, `formal_model` config (schema: `schemas/intent.schema.json`) | you, in this command |
| `specs/<target>.records.json` | ledgers: check results, witness/refusal verdicts, freshness pins, verification log, traceability, provenance — and the three triage ledgers (`extraction_triage`, `matrix_triage`, `outcome_triage`), which you DO write | the tools; you only for triage verdicts |

`specs/<target>.intent.json` is the file that makes an area exist; "the area" below always means what `tools/spec_source.py derive <target>` prints — the view every tool reads. A contract is `"kind": "contract"` in its intent file; a purely relational contract keeps its records on the `.als` assertions instead of a `.qnt`. There is no `*.area.json` any more: lint FAILs one (`legacy-spec-file`) and `tools/spec_source.py migrate <target> --write` converts it losslessly.

**Writing a record.** One tag per line; untagged lines right after the record tag are the description; a tag's value continues onto following untagged lines.

```quint
  /// @req REQ-003
  /// @status specified
  /// @while the account is Unlocked
  /// @when a login attempt fails
  /// @unwanted
  /// @shall increment failedAttempts and lock the account when it reaches
  ///   MAX_FAILED_ATTEMPTS
  /// @meaning Every failed sign-in counts against the account, and the attempt
  ///   that reaches MAX_FAILED_ATTEMPTS locks it.
  /// @via login_failed
  /// @pre not(_prevAccountStatus.keys().contains(_lastUid) and
  ///   _prevAccountStatus.get(_lastUid) == Locked)
  def shall_REQ_003(_lastUid: UserId): bool =
    accountStatus.keys().contains(_lastUid) and accountStatus.get(_lastUid) == Locked
```

- **Witnessed requirement** → a `val`/`def shall_<ID>` whose BODY is the witness predicate, with `@via <action>`. Name the parameters after the probe ghosts (`_lastUid: UserId` for the action's `uid`) so the predicate is bound to this call AND typechecks in the model. `@pre` (the delta) stays text: the `_prev*` ghosts exist only in the probe module.
- **Prohibition / `may` / not yet formalized** → on the `action` it refuses or concerns (`@via` implied); a `may`'s outcomes are separate `shall_<ID>_<outcome>` hosts tagged `@outcome-of <ID> <name>`.
- **No model yet** (raw requirement, NFR, Tier-1 area) → in the `module`'s own doc comment; `@predicate` carries a draft predicate as text until there is a model to host it.
- **Computed requirement** (a derived value or pure calculation — a score formula, a total — with no state transition) → on the `pure def` that computes it, with `@witness skipped: <why>` and `@verified-by <test path>`. Without `@unwanted` or `@modality forbidden` a skipped witness is *computed*, not a refusal: it owes the unit test that checks it (lint WARNs `computed-without-verified-by`, FAIL from in-review), never `@refusal`/`@unchanged`.
- **Invariant** → on its `val`; **property** → on its `temporal`; **constant** → on its `pure val` (the model's literal IS the value — never restate it with `@value`); **example** → on its `run`. A run that ends in `.expect(<cond>)` already states its expectation — no `@expect` needed; `@expect {json}` is only for an example hosted on an action.
- **References** → `@refs REQ-030, DEC-002, billing.REQ-004`: a bare ID is this area's (qualified on derive), `<area>.<ID>` another's; any declared kind resolves (REQ, INV, PROP, CON, EX, DEC, ASM, Q). A decision's blast radius still goes in its `decisions[].affects`.
- **Model shape the probe generator needs** (it mirrors your model into `specs/<target>.probes.qnt`):
  - **Only route-level operations are `action`s** — the ones `step` calls. Shared logic (`refuse`, `commit`, an upsert taking `apply: State => State`) is a `def`/`pure def` returning the new state; `stepP` mirrors only what `step` calls, and a `@via` must name one of those.
  - **One type alias per parameter value domain** — `type Role = str`, `type Coll = str`, `type Id = str` — because `formal_model.probe_domains` is keyed by type; parameters that all say `str` draw from one set. Where an alias would be noise, key one parameter directly: `"param:role": "Set(\"admin\", \"user\")"`.
  - **State may be a record** (`var st: State`, the shape a conformance adapter with `st()`/`last()` getters has): `_prevSt` ghosts are initialized field by field, sum types to their first variant. A type with no buildable zero (parameterized, recursive) gets `formal_model.ghost_zeros: {"<Type>": "<literal>"}`; lint flags the gap (`ghost-zero-unsynthesizable`) as soon as a delta reads it.
  - **Don't reuse Quint built-in names** for helpers — `exists`, `forall`, `map`, `filter`, `fold`, `keys`, `get`, `set`, `put`, `contains`, `size`, … fail the module with QNT101 (lint: `quint-builtin-redefined`). List: `templates/spec.qnt.template`, IDIOM.
- **Screen** → `/// @screen <Name>` on the screen sum type, one per variant, all in the type's block (description = purpose; `@auth-required`, `@components A, B`). **Navigation edge** → `/// @nav [NAV-NNN] <From> -> <To>` on the action that takes it; the line below is the trigger in the user's words, `@guard` the precondition. Give an edge a `NAV-NNN` id whenever something must point at it — an audit site MAPPED to a route or link, a journey step — instead of inventing a REQ for it. A sidebar or global link is **`* -> <To>`** (from every screen): its action sets the screen var without reading it. Lint checks every variant has a screen record and every edge's action reads and sets the screen var as claimed.
- **Transition invariant** (compares a step with the one before) → on a `def` whose parameters are `_prev*` ghosts, with `@quint-name` naming the val the probe module emits. That name must differ from the def's own name and from anything else the model declares (`def noDowngrade_over(...)` + `@quint-name noDowngrade`) — the probe module imports the model, and a duplicate does not compile (lint: `transition-invariant-name-clash`).
- After writing or re-reading a `@meaning`, pin it: `tools/itf_tools.py pin <target> --req <ID>` (the brief: `--brief`). Never type a sha.
- Never edit `records.json` verdicts, pins, logs or provenance by hand; the tools refuse to overwrite authored text and you must not overwrite theirs.

Change manifests (`specs/changes/<slug>.change.json`) and journeys (`specs/journeys/<slug>.journey.json`) are unchanged overlays: references only.

**The change is the unit of work; the area is the unit of meaning.** Every spec edit happens inside a *change* — a manifest at `specs/changes/<slug>.change.json` (schema: `schemas/change.schema.json`) that references the areas/contracts it touches. Areas remain the logical spec boundary and the single source of truth; the manifest holds membership and IDs only — never spec content, never phase flags (status is derived; see the dashboard beat).

The **active change** is per-dev sticky state: `last_change` in `.spec/local.json` (gitignored). All five spec commands resolve against it — run bare to continue the change, pass an explicit target for a one-off. If an area edit starts with no active change, auto-open one: ask "No active change. Name this work? [<area>-updates]" — Enter accepts the default. One question, then every spec diff is traceable to a manifest. Creating any new change first runs the **open-changes gate** (Step 1): if other changes are still open, the user decides whether to close them before the new one starts.

## Instructions

You are the **Adaptive Specifier**. Your job is to figure out what beat the user needs and walk them through it — not to ask a fixed sequence of questions. The user should never need to remember "am I in elicit or structure phase?" — you read the area (`tools/spec_source.py derive <target>`) and infer.

### Step 1 — Resolve change and target, read state

Resolve the change, then the target:

1. `/spec change <slug>` → open `specs/changes/<slug>.change.json` (if new: run the **open-changes gate** below, then create it per `schemas/change.schema.json`, with `intent` from the `--` hint or one question; suggest branch `change/<slug>`. Switching to an existing change skips the gate). Write `last_change: "<slug>"` to `.spec/local.json` (create the file if missing; preserve other fields). Then show the change dashboard (beat below).
2. Bare `/spec`, active change valid (`last_change` set, manifest exists, status not `landed`/`abandoned`) → **change dashboard** beat.
3. Explicit `<target>` (area, not `_project`/`_patterns/*`/`_protocols/*`/`_journeys/*`/`_overview`):
   - **not an existing area** → before creating anything, check it is not a typo of one: `tools/spec-route.py <target>` returns `confirm-target` with the close matches (edit distance or shared words, plurals folded). Ask "Did you mean `value-streams`? [Y/n/new]" — Y switches the target, `new` creates the area. Never create an area from a near-miss silently.
   - active change exists → work on that area within it; register the target in the manifest's `targets[]` if absent.
   - no active change → auto-open one first: `No active change. Name this work? [<target>-updates]` (Enter = default). Run the **open-changes gate**, create the manifest, set `last_change`, then proceed.
4. Bare `/spec`, no valid active change: exactly one area → treat as `/spec <that-area>` (rule 3 auto-opens a change); otherwise show the project overview and ask.
5. `_overview` → project overview: areas with status, open changes (slug, intent, target count, phase summary), open questions, last verification. Catalog targets (`_project`, `_patterns/*`, `_protocols/*`, `_journeys/*`) run their beats without touching any change.

**Open-changes gate — before creating any new change manifest.** List `specs/changes/*.change.json` whose `status` is `open` or `in-progress`. None → create the new change. Otherwise, before writing anything, show them with their derived phases (`tools/spec-readback.py status <slug> --json` per change) and ask in ONE question:

```
2 changes are still open:
  billing-sso   [in-progress]  "Billing accounts authenticate via SSO sessions"  2 targets — checked ✓  verified ✗
  auth-lockout  [open]         "Lock accounts after 5 failed sign-ins"           1 target  — spec draft
Close any before starting `<new-slug>`?  For each: landed (PR merged) / abandoned / keep open  [keep open]
```

- **Never close a change on your own judgment.** No answer or Enter keeps every change open: parallel changes are legitimate, and the gate exists so leftovers are a decision, not an accident.
- **`landed` only when the user confirms the PR merged.** If its phase grid is not all green, say so on the same line before accepting (`billing-sso: verified ✗ — mark landed anyway?`).
- **`abandoned` touches only the manifest.** Spec edits already committed under it stay in the areas; say so, and treat reverting them as a separate decision, not part of the gate.
- Write each chosen status into its manifest (nothing else in it changes), then create the new change; `last_change` moves to the new change either way. Commit the status changes with the new manifest: `spec(<new-slug>): open — close <slugs>`.

Then pick the entry beat. **Don't work it out by hand — ask the router:**

```bash
tools/spec-route.py <target>          # beat + the reason (--json for fields)
```

It routes on what has **happened**, not on which files exist. That matters because bootstrap scaffolds `specs/<area>.intent.json` (status raw, empty model) for every declared area: routing on "intent file missing" sent every bootstrapped area with code to resume, and brownfield extract — the strong case — was unreachable. The table it implements, first match wins:

| State | Beat |
|---|---|
| No `.spec/project.json` | **bootstrap**: walk project setup |
| `<target>` is `_project` | **project edit**: architecture defaults, repos, topology |
| `<target>` is `_patterns/<name>`, `_protocols/<name>`, or `_journeys/<name>` | **catalog edit**: add/edit a catalog file |
| no such area, name close to an existing one | **confirm target**: "Did you mean …? [Y/n/new]" (rule 3 above) |
| the area's `code_repo` has no `repo_paths` entry in `.spec/local.json` | **configure repo**: ask where it is checked out, write `repo_paths` |
| the area has code (its `code_paths`/`code_path` resolves to source files), no `extracted_from` in records.json, and no requirements yet — **whether or not the intent file exists** | **brownfield extract** (the router also says when to extract in passes) |
| no requirements, no extraction, no code | **greenfield elicit** |
| newest `verification_log` entry has `drift_detected` | **drift codify**: walk the drift items |
| spec exists and was extracted, and the code repo's HEAD moved since `extracted_from.code_sha` | **re-extract**: offer to reconcile the spec against the code as it is now |
| anything else | **resume**: pick up the next phase (or **review/idle** when nothing is incomplete) |

### Step 2 — Run the beat

Each beat is a focused conversational flow. The beats:

#### change dashboard — the resume surface

(Runs on bare `/spec` with an active change, and after `/spec change <slug>`.)

Read the manifest and every referenced area. Print the per-target phase grid, then suggest — don't auto-jump; visibility beats automation when targets interleave:

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
4. Functional areas to specify (comma-separated names). For each: kind (area / contract — an interactive surface is just an area whose model carries `@screen` + `@nav` records), one-line description, and — if it has code — code repo, where the code is, `tests_path`, `test_command`. Write each to the `areas[]` index. Where the code is: `code_path` for one directory; **`code_paths: [glob, ...]`** when the area is spread over several (`["backend/src/routes/entities*.ts", "backend/src/services/entity/"]`, plus `exclude: [glob]` if needed). Ask for the area's own files, not the whole `src/` — a scope of everything turns the audit into hundreds of OUT-OF-SCOPE verdicts. Test files (`__tests__/`, `*.test.*`, `*.spec.*`, `tests_path`) are excluded automatically.

**Don't ask about architecture defaults, topology, or Apalache settings here.** Each has a working default and a natural later moment: architecture is collected when `/spec-code-generate` first needs it (it asks for missing fields and writes them back) or anytime via `/spec _project`; topology when there are 2+ deployment units. Apalache settings need no moment at all: the defaults carry a two-pass step ladder (`shallow_steps` 3, `max_steps` 10) and a run budget (`budget_seconds` 900), so a check's cost is declared up front rather than discovered by waiting for it. Don't raise it here — the point is that the default is safe, not that it wants configuring. Front-loading them spends the user's attention before a single requirement is captured — requirements are where that attention pays.

Write `.spec/project.json`. Scaffold each declared area as `specs/<name>.intent.json` with just `kind`, `area`, `version: "0.1.0"`, `status: "raw"`, and `formal_model: {"quint_file": "<name>.qnt"}`, plus `specs/<name>.qnt` holding an empty `module <name> { }` — the module whose doc comment will carry the first raw requirements until there is a model to hang them on. A module with no declarations is not a formal model (spec_source reads it as "none yet"), so lint grades the missing model as it always has: WARN while `raw`/`draft`, FAIL from `in-review` on, and a freshly bootstrapped project lints clean.

5. **Open the first change** — the change is the unit of work, so bootstrap ends inside one, not before one. Ask: `Name the first change? [initial-spec]` (Enter = default; intent defaults to "Initial specification of <area list>"). Create `specs/changes/<slug>.change.json` per `schemas/change.schema.json` with every declared area as a target (`status: "open"`, empty `ids[]`), write `last_change` to `.spec/local.json`, and suggest branch `change/<slug>`.

**No git?** If the project is not a git repo (`git rev-parse` fails) or the user said they don't want one, say once: "No git repo — skipping the pre-commit hook and commit hints; run `python tools/spec-lint.py` by hand." Then skip the hook and every commit/branch hint for the rest of the session. Otherwise install the pre-commit hook via `bash tools/setup-hooks.sh` (idempotent; outside git it installs nothing and exits 0).

**Per-dev config** goes in `.spec/local.json` (gitignored; schema: `schemas/local.schema.json`): `repo_paths` — logical repo name → this machine's checkout path, one per `repos` entry an area uses — and `last_change`. Write it for the user when you know the paths; lint validates it.

**Check the Python tooling** once: `python3 -c "import jsonschema"`. If it is missing, offer to install it (`tools/check-tooling.sh --install`, or `python3 -m pip install jsonschema`): without it every lint run is PARTIAL — schema validation is skipped, so a malformed intent field passes unnoticed.

Print: "Project initialized, change `<slug>` open. Next: `/spec <area>` for each area you declared — edits land in the change; bare `/spec` shows the dashboard."

#### project edit — `.spec/project.json`

(Runs on `/spec _project`.)

Show current project config; ask which section to edit. Sections: repos, areas index, architecture defaults, topology, Apalache settings. Walk only the chosen section.

#### catalog edit — patterns/protocols/journeys

(Runs on `/spec _patterns/<name>`, `/spec _protocols/<name>`, or `/spec _journeys/<name>`.)

For journeys (`schemas/journey.schema.json`, files at `specs/journeys/<name>.journey.json`): a journey is THE use-case mechanism — a named user-visible flow, steps as qualified `<area>.<ID>` refs in temporal order; most live inside one area, some cross boundaries, same shape either way. Journeys are born where the flow is: in elicitation, one story told is one journey; in brownfield, one reachable entry point is one journey, deduced from the call graph and updated on re-extraction. This beat is for stitching or editing them directly. Creating one: ask for the actor and the story end to end, then map each step to an existing REQ (offer candidates from the areas' requirements); a step with no matching REQ is a gap — capture it in the owning area first (`/spec <area>`), then finish the journey.

If the file doesn't exist: walk creation per `schemas/pattern.schema.json` or `schemas/protocol.schema.json`. If it exists: show contents, ask which fields to update. Write back. Don't modify any area's references to it (the user opts those in separately).

If the user types `/spec _patterns`, `/spec _protocols`, or `/spec _journeys` (no name): list cataloged entries with one-line descriptions and ask which to edit (or "new").

#### brownfield extract

(Runs when the area has code at its `code_paths`/`code_path`, no `extracted_from` in its records, and no requirements yet — including the raw scaffold bootstrap wrote. `tools/spec-route.py <target>` decides.)

Tell the user: "No spec for `<target>` yet, but code exists at `<resolved-code-path>`. I'll extract a draft spec."

**Brownfield is the strong case, not the awkward one.** Greenfield elicitation has only the user's memory to work from. Here there is a running implementation that answers any question you ask it — every threshold, every branch, every error path is already decided and readable. Extraction should therefore produce a *stronger* spec than elicitation, not a weaker one.

**What you are producing is a spec that is true of this code.** That is the deliverable and its whole value: the team can review behavior nobody wrote down, reason about a change before making it, and read in the readback what the system does today. Nothing has to be regenerated for that to pay off, so do not steer the user toward a rewrite they did not ask for, and do not open the beat by asking them to design a substitution boundary. If they *are* rewriting, there is machinery to measure how completely the spec captured the code — offer it at the end, as step 6.

Extraction is also not a one-time event. Say so when you finish: the spec is true of the code as of today, and `/spec <target>` re-extracts when the code moves on.

##### 0. Look for a prior spec first

The spec files may be gone while their traces are not. Before reading behavior, look for what an earlier spec left in the code repo:

- **Spec ids in the code**: `grep -rnE '\b(REQ|INV|CON|DEC|PROP|EX)-[0-9]{3}\b' <code paths>` — comments, test names, error codes.
- **A conformance adapter / replay harness**: e.g. `conformance/adapter.ts` with one method per Quint action and a getter per state var, and the test that replays traces through it.
- **Witness traces**: `find <repo> -name '*.itf.json'`. Decode them: `tools/itf_tools.py summarize <dir>` prints, per trace, each step's action, its arguments and the state diff — what each old id *meant*. The file name is usually the id.

If any of these exist:
- **Keep the ids the code cites.** A comment saying `// REQ-030` must still point at REQ-030; renumbering orphans every one of them.
- **Align the model with the adapter**: same state var names, action names and parameter names, so the existing replay suite runs against the new model unchanged — a spec that matches the adapter is verifiable on day one.
- **An id the code cites that you cannot recover** (no trace, no decodable comment) becomes an open question: `Q-NNN: what did REQ-017 (cited in routes/x.ts:40) specify?` — not a guess.

Say what you found in one line ("Found 33 witness traces and a conformance adapter — reusing their ids and names") before moving on.

##### Large areas: extract in passes

If the router reports `passes` (more than ~25 files or ~300 decision sites), don't extract it all at once — ask which slice is coherent to do first (the write API, one resource). Then:

- Specify that slice fully.
- Put the rest in `scope.excluded[]` with `"pass": 2` (or a reason starting "Deferred to pass 2 of this area: …"), so the audit's OUT-OF-SCOPE verdicts for it cite a real, honest exclusion.
- Bulk-triage it instead of site by site: `tools/spec-extract-audit.py <target> --triage-file '<glob>' --verdict OUT-OF-SCOPE --scope-ref <item> --record`.

The audit lists every deferred exclusion with its site count; the next pass starts from that list (see **re-extract**).

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

Mark every extracted item `@source extracted` (requirements, invariants, constants and examples all take it) and requirements `@status needs-validation`, and record where it came from:

```quint
  /// @evidence authService.ts:78-91
  /// @confidence high
  /// @inferred-by agent
```

`confidence: "low"` is the honest label when the code was ambiguous and you guessed — the readback surfaces it and lint flags it at review. Guessing silently is what makes an extracted spec untrustworthy.

##### 2. Account for the code you did NOT specify

Run `tools/spec-extract-audit.py <target> --emit --record`. **`--record` is not optional.** Without it the audit prints to the terminal and nothing is written down: `check_results.extraction` stays absent, and the readback, the ship verdict and lint all read absent as "nothing to say". That is how an area reaches full witnesses, full invariants, zero untriaged matrix cells and zero lint failures with most of its code unaccounted for. The number has to land in the ledger (`records.json`) or it does not exist.

It enumerates the decision sites — branches, guard literals, error handlers, early exits — and prints triage stubs for every one no spec element claims.

This is the only check in the framework that runs **code → spec**, and it is the one that matters here: the reason a regenerated implementation diverges is almost always a branch nobody wrote down, and nothing spec-shaped can look for a branch the spec does not mention. Give every site a verdict in `extraction_triage[]` — in `specs/<target>.records.json`, the one ledger you write by hand:

- `MAPPED` (+ `maps_to`) — realizes these spec ids.
- `NOT-BEHAVIOR` — logging, metrics, tracing.
- `DEFENSIVE` — unreachable by construction, kept as a belt.
- `DEAD` — unreachable. A finding about the code; say so.
- `GAP` (+ `Q-NNN`) — real behavior nobody specified. The extraction hole.
- `OUT-OF-SCOPE` (+ `scope_ref`) — outside the declared boundary.

Sites are keyed by fingerprint, not line number, so the ledger survives reformatting. Work through the GAPs with the user; they are the highest-value questions in the whole beat.

Whole files with one verdict (a deferred slice, a logging module) are triaged in bulk — `--triage-file '<glob>' --verdict <V> --record` with `--scope-ref` (OUT-OF-SCOPE), `--maps-to` (MAPPED) or `--question` (GAP); it only fills unclaimed sites and never overwrites a row. Test files are not scanned at all unless the area sets `include_tests: true`.

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
tools/spec-record.py stamp <target> --extracted        # records the area's code_paths
```

That records `extracted_from` — the code repo's git sha and the paths you read (the area's `code_paths`/`code_path`; `--code-path`, repeatable, to override). One entry in the ledger, and the thing that makes re-extraction able to say *what changed and since when* instead of only *which fingerprints are new*. Do it now: the sha you need is the one you just read, and it is unrecoverable later. If the code repo is not a git repo, the tool refuses and says so — carry on without it rather than inventing a value.

Then tell the user how to keep the spec current, because an extracted spec that is never revisited becomes a confident description of a system that no longer exists. `/spec <target>` on an area whose code has changed since extraction routes to **re-extract** — no `/spec-code-verify`, adapter or test command needed first. Re-stamp at the end of each reconciliation, so the next one has a fresh baseline.

##### 6. Then formalize — and offer fidelity measurement only if it fits

Write the model with its records (`specs/<target>.qnt`) and the intent file. Present extracted items in batches for confirm/edit/discard — with `extraction.evidence`, the user can jump to the code instead of reconstructing your reasoning.

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

(Runs when `specs/<target>.intent.json` is missing AND no code exists.)

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

**Early matrix pass**: as soon as `state_machines[]` and a first batch of REQs exist, run `tools/spec-matrix.py <target>` and triage the `?` cells in this conversation — classify into the GAP / IMPOSSIBLE / OUT-OF-SCOPE buckets defined in `/spec-check` Step 4a, recording each verdict in `matrix_triage[]` in `specs/<target>.records.json` (committed — decisions in the gitignored CSV don't survive a clone). A gap found now, while the user is describing the domain, becomes a REQ in one exchange; the same gap found later by `/spec-check` becomes a stale entry in the Q-NNN queue. Same tool, earlier moment.

**Closing gap sweep**: when the clusters are exhausted (before formalizing), run one short red-team moment while the user is still in the conversation. From `/spec-check` Step 4b's category list, keep only the categories this domain plausibly touches; ask at most ~8 questions whose answer would add or change a requirement, each citing the REQ/INV/entity it touches (or "absent"). Answers become REQs/INVs/CONs in the same exchange; what the user can't answer becomes a Q-NNN. Same logic as the early matrix: a gap found now is a requirement in one exchange. The full `--reality` pass at check time is for depth — it shouldn't be the first time these questions are asked.

Write as you go: requirements as doc comments in `specs/<target>.qnt` (on the module while there is no model), everything else in `specs/<target>.intent.json`. After enough is captured, draft the model in the same file (structure convention: `templates/spec.qnt.template`) — the EARS fields map mechanically: `trigger` → action, `state` → `require` guard, `response` → effect — and move each requirement's record onto the declaration that realizes it. While formalizing, write each witnessed requirement's `shall_<ID>` host: its body is the witness predicate (the Quint boolean over state that's true exactly when the behavior has happened — `/spec-check` turns it into the witness probe), and because it is model code, `quint typecheck` now checks it. Show the module for confirmation; offer `/spec-check` next.

#### resume — pick up the next phase

(Runs when `specs/<target>.intent.json` exists but some sections are incomplete.)

Inspect the derived area for gaps:

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

Writing state_machines[<Entity>] in specs/<target>.intent.json.
```

After writing, suggest running `spec-lint` (or `/spec-check`) — the state-machine lints fire immediately if the declared structure conflicts with the Quint sidecar.

Tell the user what you noticed and what you propose to work on next; let them confirm or redirect.

#### re-extract

(Runs when `specs/<target>.intent.json` exists, the area has a `code_path`, and the code there has changed since the spec was extracted or last reconciled — compare `extraction_triage[]` fingerprints and `extraction.evidence` against the current sources. Offer it, do not force it: say what looks stale and ask.)

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

5. **Promote deferred slices.** The audit lists every `scope.excluded[]` entry deferred to a later pass, with how many sites are triaged OUT-OF-SCOPE against it. If this is that pass: remove the exclusion, delete its bulk OUT-OF-SCOPE rows, re-run the audit — those sites come back unclaimed — and extract them like step 1. Defer what is still too big to `pass: 3`.

6. **Land it in the change.** Re-extraction is a spec edit like any other: register the target in the active change's `targets[]` and add every touched ID to `ids[]`.

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

For "code is right", update the model and its records together (modify the invariant's `val` and its `@inv` text, change the constant's literal, add a requirement on the declaration that realizes it). Add a `DEC-NNN` ADR with `kind: "architecture"` explaining the codification rationale. Update `version` and `last_modified`.

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

- Update `specs/<target>.qnt` records / `specs/<target>.intent.json` (or `.spec/project.json`, catalog file, etc.)
- Update `last_modified`
- Bump `version` only when the user signals a meaningful change (added requirement, modified invariant, etc.) — minor for additions, patch for refinements, major for breaking changes
- **Update the change manifest**: any ID added or modified in the area goes into the manifest target's `ids[]`. (No phase flags to maintain — staleness is automatic: editing the spec bumps `last_modified`/changes the model sha, which un-derives "checked"/"verified".) When a touched area is spanned by a contract, add that contract to `targets[]` with `auto: true` if not already present. Status `open` → `in-progress` on first spec edit.
- Commit hint (git projects only — skip it when bootstrap found no git repo): at sensible checkpoints, suggest `git add specs/<target>.qnt specs/<target>.intent.json specs/<target>.records.json specs/changes/<change>.change.json && git commit -m "spec(<change>): <what>"`

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
