# /spec-check — Apalache Model Checker + Witness Obligations + Reality-Gap Sweep

Run Apalache on area's Quint sidecar, discharge witness obligations (every requirement demonstrated reachable), then the state×event matrix pass. Adversarial red-team runs only with `--reality` — it's per-revision work, not per-run (you re-run check 5× while fixing a counterexample; don't regenerate 25 questions each time). Write findings back into area JSON. Cascade Apalache to every contract whose `spans` includes target area.

Three obligation classes (rationale: METHODOLOGY.md → "Witness Obligations"):
1. **Invariants hold** — Apalache finds no counterexample.
2. **Behaviors are reachable** — every REQ gets a machine-found, path-constrained *witness trace*. Action coverage falls out: a witnessed REQ proves its `quint_ref` fires; actions referenced by no requirement are spec-lint's `orphan-action` warning, not a checker run.
3. **Coverage is total** — state×event matrix has no untriaged cells; red-team gaps surfaced.

## Usage
```
/spec-check [target]
/spec-check [target] --only INV-001,INV-002    # specific Apalache checks only
/spec-check [target] --steps 20                # override max_steps
/spec-check [target] --no-cascade              # skip contract cascade
/spec-check [target] --no-witness              # skip witness probes
/spec-check [target] --reality                 # ALSO run the adversarial red-team pass
```

`[target]` is optional: without it, **every target of the active change** (`last_change` in `.spec/local.json` → `specs/changes/<slug>.change.json`) is checked, with the contract cascade deduplicated across the set — each contract runs once even when several of its spanned areas changed. Explicit `[target]` checks just that one (cascade still applies). Works for any kind: `area` (with or without UI blocks), `contract`.

## Instructions

You are the **Checker**. The division of labor is strict — mechanism over trust applies to the bookkeeping itself:

- **`tools/spec-record.py check <target>`** runs `quint verify` for every invariant, property, and witness probe, parses outcomes, saves traces, and writes `check_results`, `formal_status`, and the `witness` blocks (status/trace/checked_at/model_sha) into the area JSON **mechanically**. You never hand-edit those fields.
- **You** do the judgment work it can't: draft missing witness predicates, regenerate the probes module, translate counterexamples into plain language (filling `counterexample.nl_explanation` — the one free-text field), triage the matrix, run the red-team.

### Step 1 — Resolve target and load context

Resolve the target set:

- Explicit `[target]` → check just it (plus its contract cascade). Doesn't alter the active change.
- No target → read `last_change` from `.spec/local.json` and load `specs/changes/<slug>.change.json`. Target set = every entry in `targets[]`, ordered areas-first then contracts. Dedupe the cascade: collect every contract spanning any checked area, run each **once** after its areas, drop contracts already in the set from per-area cascading. Print one line: `Change: <slug> — checking <n> targets (pass a target name for just one)`.
- No active change → if `.spec/project.json` has exactly one area, use it; otherwise ask the user which (and suggest `/spec change <slug>` for multi-target work).

The manifest stores **no phase flags** — "checked" is derived, never written: a target counts as checked when `check_results.ran_at` ≥ the area's `last_modified` and every witness `model_sha` matches `tools/itf_tools.py sha <target>`. Run every target even when an early one fails — the point of set-checking is the complete picture; report failures together at the end.

Read:
- `specs/<target>.*.json` — the area JSON (must exist; otherwise tell the user to run `/spec <target>` first).
- `specs/<target>.qnt` — the Quint sidecar (must exist; otherwise this area isn't formalized yet — tell the user).
- `.spec/project.json` — for Apalache settings (`timeout_seconds`, `max_steps`).

Use `tools/quint_ir.py specs/<target>.qnt --json` to get the structured view of the sidecar (module name, imports, actions, vars, vals) before running Apalache — it errors if the file doesn't parse.

### Step 2 — Prepare predicates and the probe module (judgment work)

Before the runner can do anything:

1. **Ensure predicates exist.** Each requirement needs `witness.predicate` — a Quint boolean expression over state that is true exactly when the required behavior has occurred (e.g. for "account locks after N failures": `accountStatus.keys().exists(u => accountStatus.get(u) == Locked)`). If missing, draft one from the EARS fields + `quint_ref` and confirm with the user; write it into the area JSON. The predicate must be a **real postcondition over state** — `spec-lint` FAILs a constant (`true`) or state-free predicate (it would "witness" merely that the action fired, proving nothing) and WARNs when the predicate names no variable the requirement's own action assigns. Make it reference the state the action changes.

2. **Generate/refresh the probe module** at `specs/<target>.probes.qnt` (see `templates/probes.qnt.template`). It imports the area module, instruments steps with ghost vars, and declares one negated probe per requirement, named `witness_<REQ_ID with - → _>`:

   - **Ghost vars** — underscore-prefixed so they can never collide with real model vars: `_lastAction` (which action produced the state) plus one param ghost per distinct action parameter (`_lastUid`, `_lastSid`, …). `initP`/`stepP` wrap the area's `init`/`step`, tagging every branch. The replay harness reads call parameters from these ghosts — never infer them from state diffs.
   - **Path-constrained probes**: the probe negates `predicate AND _lastAction == <quint_ref>` — the trace must reach the postcondition *via the requirement's own action*. A trace that produces the right state through some other mechanism is not a demonstration of this requirement. Drop the `_lastAction` conjunct only when the requirement has no `quint_ref` (rare — e.g. cross-ref requirements).

```quint
module auth_probes {
  import auth.* from "./auth"
  var _lastAction: str
  var _lastUid: str
  // initP / stepP wrap init / step, tagging branches (see template)

  // Witness probe for REQ-003: violated ⇔ behavior happened VIA login_failed
  val witness_REQ_003: bool =
    not(accountStatus.keys().exists(u => accountStatus.get(u) == Locked)
        and _lastAction == "login_failed")
}
```

Record the file in `formal_model.probes_file`. The probe module is generated — regenerate, never hand-edit.

**Multi-module areas** (`kind: contract`, or any area whose `spans[]` names other areas — typical for areas with UI blocks): the probe module imports *every* spanned module and `stepP` is an `any` over all of their wrapped actions, so joint behaviors are explored; predicates range over the joint state. Pattern is in the template.

### Step 2a — Run the recorder (mechanical work)

```bash
tools/spec-record.py check <target> [--steps N] [--only INV-001,REQ-003] [--no-witness]
```

The runner does, deterministically: every invariant/property check (`quint verify`, counterexample traces to `specs/<target>/traces/<ID>.cex.itf.json`), every witness probe (`--init=initP --step=stepP`, violation trace = the witness), skip-if-fresh (a REQ already `witnessed` against the current `model_sha` is not re-proven), and all JSON write-backs: `check_results`, `formal_status`, `witness.{status,trace,checked_at,model_sha}`. If it reports `quint` missing, run `tools/check-tooling.sh` for install hints.

Interpret its output:

- **`NO-WITNESS`** → report loudly: the behavior is unreachable — impossible guard, missing action, or bound too small. Offer: (a) inspect the guard, (b) raise `--steps`, (c) mark `skipped` with `justification` — correct for rejection requirements, where the proof is an invariant, not a trace (see METHODOLOGY → "Rejection requirements"). (a)/(b) are yours to act on; (c) is the one witness field you set by hand, and only with the user's confirmation.
- **`no-probe` / `no-probes-file`** → go back to Step 2 and regenerate the probes module.
- **Properties (PROP-NNN) honesty rule:** Apalache's temporal checking is bounded. Report a PROP result as `verified` ONLY with the bound stated (`verified up to N steps`). On `timeout`, suggest demoting the PROP to a witness-traced scenario (a `run` demonstrating the eventuality once) plus a fairness note — don't leave the user believing unbounded liveness was proven.
- **Invariants (INV-NNN) honesty rule:** by default `quint verify` checks invariants by **bounded** model checking to `max_steps`. `formal_status: "verified"` therefore means *no counterexample within N steps* — NOT a proof, and the readback renders it `✓ (≤N steps)`. When the user wants an unbounded proof for a load-bearing invariant (e.g. an auth guard), set `proof: "inductive"` on it in the area JSON; `spec-record` runs `quint verify --inductive-invariant=<quint_name>` and a pass becomes `verified-inductive` (`✓ proven`). If quint reports the invariant isn't constrained enough ("x is used before it is assigned"), that's an honest non-proof — help the user strengthen the predicate, don't fall back to bounded and call it proven. Suggest upgrading any `critical` invariant the reviewer would read as "always true" to inductive.

Witness traces are inputs to `/spec-verify`'s conformance replay and to `/spec-readback`'s sequence diagrams — they are committed artifacts, not temp files.

Cascade economy: cascade only to contracts whose `spans` include areas actually changed (git diff against the last check), not every contract in the project.

(No separate action-coverage pass: witnessed REQs prove their actions fire; `spec-lint` flags unreferenced actions as `orphan-action` statically.)

### Step 3 — Cascade to contracts (areas only)

If the target's `kind == "area"` and `--no-cascade` was not passed:

Enumerate `specs/*.json` for files with `kind: "contract"` and `spans` containing this area. For each, run Apalache against `specs/<contract>.qnt` the same way.

Report contract results separately:

```
## Contract Cascade

  ✓ user-permission (spans: auth, billing)    VERIFIED
  ✗ session-billing-link                       COUNTEREXAMPLE on INV-CONTRACT-003
```

If any cascaded contract fails, the overall result is FAIL — area-internal checks passing isn't enough when the change broke a cross-area contract.

If the target is a contract, no further cascade — its check is the verification.

### Step 4 — Translate counterexamples

For each counterexample found, translate the trace into natural language:

```
## Counterexample for INV-001 (singleSession)

**What the invariant says:** A user has at most one Active session.

**What the counterexample shows:** Starting from no sessions, after two consecutive login actions for the same user with different session IDs, the user ends up with two Active sessions.

**The trace:**
  Step 0: sessions = {}, accounts = {alice: Unlocked}
  Step 1: login(alice, s1) → sessions = {s1: (alice, Active)}
  Step 2: login(alice, s2) → sessions = {s1: (alice, Active), s2: (alice, Active)}  ← violates INV-001

**What this suggests:** The `login` action doesn't check whether the user already has an Active session before creating a new one. The Quint guard is missing `activeSessionsOf(u).size() == 0`.

**Want to fix?** Suggested edit to specs/auth.qnt:
  action login(u, s) = all {
+   activeSessionsOf(u).size() == 0,
    accounts.get(u) == Unlocked,
    ...
```

Translate each counterexample with this level of clarity. Don't paste raw Apalache JSON; write for a human.

### Step 4a — State × event matrix completeness

Always runs (mechanical, fast). Apalache proves invariants hold in modeled transitions. It does NOT catch transitions that were never modeled. This pass finds those silent gaps. Coverage is transition-precise: only a declared `state_machines[]` transition for exactly (state, event) covers a cell — a requirement merely mentioning the event does not.

**Build matrix (mechanical).** Run:

```bash
tools/spec-matrix.py <target> --record
```

(`--record` writes the coverage counts into the area JSON's `check_results.matrix` — that's how readbacks surface completeness; the CSV itself is gitignored.)

This enumerates (entity, state) × event cells. **Events are scoped per entity** — not a full cartesian. For each entity, scope = triggers in that entity's `state_machines[].transitions[]` plus verbs whose `requirements[]` description text mentions the entity name. This avoids trivial IMPOSSIBLE-by-construction noise like `expire_session` on Account.

The script writes into `specs/<target>/gen/` (gitignored — regenerable):
- `specs/<target>/gen/matrix.csv` — columns `entity,state,event,covered_by,behavior`. Cells with no covering transition/REQ are marked `?`.
- `specs/<target>/gen/matrix-orphans.txt` — events declared in `concepts.verbs[]` or the Quint sidecar but **not scoped to any entity** (no transition references them and no REQ description mentions any entity for them). These are candidates for missing entity↔event links.

Stderr prints `entities=… state_rows=… events=… orphans=… cells=… covered=… triaged=… uncovered=…`. The script makes no judgement — enumeration only. **Triage decisions live in the area JSON's `matrix_triage[]`** (committed — the CSV is gitignored scratch; a decision recorded only there would vanish on a fresh clone and break the CI gate). `tools/spec-matrix.py <target> --strict` exits 1 while any `?` remains — that's the CI completeness gate.

**Triage `?` cells (LLM).** Read every CSV row where `covered_by == "?"`. Classify each into one of three buckets and append an entry to `matrix_triage[]` in `specs/<target>.*.json` (`{entity, state, event, verdict, reason}`), then re-run `spec-matrix --record` to refresh the CSV and stats:
1. **Real gap** — behavior matters, spec is silent → `verdict: GAP`, `reason` = the unanswered question, `question: Q-NNN` AND append that `Q-NNN` to `open_questions[]` with `source: "matrix"`.
2. **Impossible transition** — state/event combination cannot occur (e.g. event guarded by other state) → `verdict: IMPOSSIBLE`, `reason` = the impossibility argument.
3. **Intentionally out of scope** — covered elsewhere or explicitly excluded → `verdict: OUT-OF-SCOPE`, `reason` = pointer (e.g. `see contract X` or `lifecycle action, not a transition`).

**Triage orphans (LLM).** Read `specs/<target>/gen/matrix-orphans.txt`. For each orphan event, decide:
- **Missing link** — event genuinely applies to some entity but neither a transition nor a REQ wires it → append `Q-NNN` with `source: "matrix-orphan"` asking which entity owns this event and what state transitions it should trigger.
- **Cross-cutting** — event belongs to a contract / spans multiple areas → note as out-of-scope; leave for contract spec.
- **Stale verb** — verb declared but never used → flag to user as candidate for removal from `concepts.verbs[]`.

Don't generate a question for every blank — most uncovered cells in a large matrix are trivially impossible. Surface ambiguous cells to the user instead of guessing.

Format gap question:
```json
{
  "id": "Q-NNN",
  "question": "State×event gap: when <entity>=<state> and <event> fires, spec is silent. Should this be <option-A> or <option-B>?",
  "source": "matrix",
  "status": "open"
}
```

### Step 4b — Adversarial red-team

Runs **only when `--reality` was passed**. Suggest it when the requirement set changed materially since the last red-team (new REQs/INVs, version minor bump) — not during counterexample-fix loops.

Spawn an Agent (subagent_type=general-purpose) in skeptical-SRE mode. Pass it: `entities[]`, `requirements[]`, `invariants[]`, `properties[]`, `concepts` from area JSON. Prompt:

> You operate this product for a demanding customer (SRE / security reviewer / compliance auditor). Read these requirements, invariants, and entity model. Category list: authentication & RBAC, multi-tenancy isolation, audit trail, observability (metrics/alerts/SLOs), idempotency of user actions, API/CLI surface beyond UX, quotas & rate limits, crash recovery of control plane mid-operation, time/timezone semantics, encryption at rest/transit, backward compatibility & migration, concurrent operators. First discard the categories this area plausibly never touches (one line each: category — why not). For the remaining categories, generate the questions the spec does NOT answer — scale to the spec, roughly one question per 3 requirements, max 20. Every question must cite which REQ/INV/entity the gap touches, or state "no anchor — entirely absent"; drop any question you can neither anchor nor justify as an absence. Categorize each as `critical` / `important` / `nice-to-have`. Return JSON array.

For each returned question:
- **critical** → append to `open_questions[]` as `Q-NNN` with `source: "red-team:critical"`.
- **important** → append with `source: "red-team:important"`.
- **nice-to-have** → write to `specs/<target>/gen/redteam-backlog.md` (not into JSON, and gitignored — anything that must survive gets promoted to a Q-NNN).

De-dupe against existing `open_questions[]` by semantic similarity before appending — red-team will re-raise prior gaps every run.

### Step 5 — Fill in the judgment fields

`spec-record` already wrote `check_results`, `formal_status`, and the `witness` blocks. Two things remain yours:

1. For each `result: "counterexample"` entry in `check_results.checks[]`, write `counterexample.nl_explanation` — the Step 4 translation, condensed to a paragraph. (The runner carries an existing explanation over when the result is unchanged; write it once per new counterexample.)
2. If matrix triage filed `Q-NNN` gap questions, list their ids in `check_results.matrix.gaps`.

For each unresolved counterexample, append a new `Q-NNN` entry to `open_questions[]`:

```json
{
  "id": "Q-007",
  "question": "INV-001 has a counterexample: login allows multiple Active sessions. Is the multi-session behavior intentional, or should login guard against existing sessions?",
  "status": "open"
}
```

### Step 6 — Summary and next step

```
## /spec-check auth — <date>

Quint file:     specs/auth.qnt (module auth)
Settings:       max_steps=10, timeout=300s

Formal results (Apalache):
  ✓ INV-001 singleSession         VERIFIED (≤10 steps)        (2.3s)   bounded — not a proof
  ✓ INV-002 guardedDashboard      VERIFIED (inductive)        (2.9s)   proven over all reachable states
  ✗ INV-003 noLockedSession       COUNTEREXAMPLE              (1.8s)
  ⏱ PROP-001 eventualLogout       TIMEOUT after 300s — bounded liveness only; consider demoting to a witnessed scenario
  ✓ INV-CONTRACT-001 noOrphan     VERIFIED (≤10 steps)        (3.1s, via cascade from user-permission)

Witness obligations:
  ✓ REQ-001 login reachable        WITNESSED  → auth/traces/REQ-001.itf.json (4 states)
  ✓ REQ-003 lockout reachable      WITNESSED  → auth/traces/REQ-003.itf.json (6 states)
  ✗ REQ-004 expiry reachable       NO WITNESS — expire_session unreachable from init. Guard impossible?

Reality-gap results:
  matrix:    2 entities, 5 state rows, 5 events (per-entity scoped) = 12 cells; 4 covered (transitions), 6 triaged, 2 GAP (→ Q-008, Q-009); 2 orphan events → specs/auth/gen/matrix-orphans.txt
  red-team:  (not run — pass --reality after material spec changes)

Counterexamples translated above. 12 new open questions (Q-007..Q-019).

Next:
  - Fix INV-002 in specs/auth.qnt (suggested edit shown above), then re-run /spec-check.
  - Triage open_questions[] with /spec auth — critical red-team gaps (Q-010..Q-012) likely need new REQs.
```

Always end with a concrete suggested next step.

### Step 7 — Commit

```bash
git add specs/<target>.*.json specs/<target>.probes.qnt specs/<target>/traces/
git commit -m "spec(<target>): check — <summary>"
```

(`specs/<target>/gen/` is gitignored — matrix, orphans, and red-team backlog are regenerable; omit probes/traces if `--no-witness` was passed)

Show the commit message; wait for confirmation.
