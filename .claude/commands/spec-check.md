# /spec-check — Apalache Model Checker + Witness Obligations + Reality-Gap Sweep

Run Apalache on area's Quint sidecar, discharge every requirement (checked on every step of its action, and demonstrated reachable), then checks every action for coverage and explanation and every external outcome for a handler. Adversarial red-team runs only with `--reality` — it's per-revision work, not per-run (you re-run check 5× while fixing a counterexample; don't regenerate 25 questions each time). Findings land in the area's ledger (`specs/<target>.records.json`); judgment you write (`@while` / `@shall`, invariants, triage) goes where it lives — the model's doc comments, the intent file. Cascade Apalache to every contract whose `spans` includes target area.

Three obligation classes (rationale: METHODOLOGY.md → "Witness Obligations"):
1. **Invariants hold** — Apalache finds no counterexample.
2. **Behaviors are reachable** — every REQ gets a machine-found, path-constrained *witness trace*. Action coverage falls out: a witnessed REQ proves its `quint_ref` fires; actions the model cannot reach — from `init`/`step` or from anything the area's records name — are spec-lint's `orphan-action` warning, not a checker run.
3. **Coverage is total** — every action step is under a requirement or verdict, every observable change is explained; red-team gaps surfaced.

## Usage
```
/spec-check [target]
/spec-check [target] --only INV-001,INV-002    # specific Apalache checks only
/spec-check [target] --steps 20                # override max_steps (the deep pass's ceiling)
/spec-check [target] --shallow-steps 5         # override shallow_steps (the first pass's bound)
/spec-check [target] --budget 1800             # override budget_seconds (cap on the WHOLE run)
/spec-check [target] --no-cascade              # skip contract cascade
/spec-check [target] --no-witness              # skip witness probes
/spec-check [target] --reality                 # ALSO run the adversarial red-team pass
```

`[target]` is optional: without it, **every target of the active change** (`last_change` in `.spec/local.json` → `specs/changes/<slug>.change.json`) is checked, with the contract cascade deduplicated across the set — each contract runs once even when several of its spanned areas changed. Explicit `[target]` checks just that one (cascade still applies). Works for any kind: `area` (with or without UI blocks), `contract`.

## Instructions

You are the **Checker**. The division of labor is strict — mechanism over trust applies to the bookkeeping itself:

- **`tools/spec-record.py check <target>`** runs `quint verify` for every invariant, property, and witness probe, parses outcomes, saves traces, and writes `check_results`, `formal_status`, and the `witness` verdicts (status/trace/checked_at/model_sha) into `specs/<target>.records.json` **mechanically** — through `spec_source.save_area`, which refuses to change anything authored. You never hand-edit those fields.
- **Structural invariants route to Alloy instead**, when an invariant carries `proof: "structural"` and the area declares `formal_model.alloy_file` (see METHODOLOGY.md → "The Structural Backend: Alloy"). Same runner, same rule that you never write the verdict: it runs `alloy exec -c <alloy_command>` and reads the outcome from the run's `receipt.json`. A pass is `verified-in-scope` — bounded by the scope declared in the `.als`, not by `max_steps` — and renders as `✓ (scope: …)`, never a bare ✓. Off entirely unless an area opts in; if no `alloy_file` is declared, nothing about this command changes.
- **You** do the judgment work it can't: formalize requirements still missing `@while` / `@shall`, regenerate the probes module, translate counterexamples into plain language (filling `counterexample.nl_explanation` — the one free-text field), close completeness findings with requirements or verdicts, run the red-team.

### Step 1 — Resolve target and load context

Resolve the target set:

- Explicit `[target]` → check just it (plus its contract cascade). Doesn't alter the active change.
- No target → read `last_change` from `.spec/local.json` and load `specs/changes/<slug>.change.json`. Target set = every entry in `targets[]`, ordered areas-first then contracts. Dedupe the cascade: collect every contract spanning any checked area, run each **once** after its areas, drop contracts already in the set from per-area cascading. Print one line: `Change: <slug> — checking <n> targets (pass a target name for just one)`.
- No active change → if `.spec/project.json` has exactly one area, use it; otherwise ask the user which (and suggest `/spec change <slug>` for multi-target work).

The manifest stores **no phase flags** — "checked" is derived, never written: a target counts as checked when `check_results.ran_at` ≥ the area's `last_modified` and every witness `model_sha` matches `tools/itf_tools.py sha <target>`. Run every target even when an early one fails — the point of set-checking is the complete picture; report failures together at the end.

Read:
- `specs/<target>.intent.json` + `specs/<target>.qnt` (records as doc comments) + `specs/<target>.records.json` — the area (the intent file must exist; otherwise tell the user to run `/spec <target>` first). `tools/spec_source.py derive <target>` prints the view every tool reads.
- `specs/<target>.qnt` — the Quint sidecar (must exist; otherwise this area isn't formalized yet — tell the user).
- `.spec/project.json` — for Apalache settings (`timeout_seconds`, `max_steps`, `shallow_steps`, `budget_seconds`). `timeout_seconds` caps **one** check; `budget_seconds` (default 900) caps **the whole run**, so a 20-check area declares its cost before it starts instead of discovering it by waiting. `shallow_steps` (default 3) is the first pass's bound, `max_steps` the ceiling the second aims at. All four have working defaults — a project with no `apalache` block needs none of them.

Use `tools/quint_ir.py specs/<target>.qnt --json` to get the structured view of the sidecar (module name, imports, actions, vars, vals) before running Apalache — it errors if the file doesn't parse.

### Step 2 — Formalize requirements and generate the probe module (judgment work)

Before the runner can do anything:

1. **Ensure every requirement is formal.** Each functional requirement past `raw` sits on the action it governs with `@while` (pre-state condition) and `@shall` (post-state condition) as Quint booleans over that action's own parameter names, `@old <name>: <Type> = <expr>` for any value the response compares against before the step, `@outcome <name>: <expr>` per outcome for a `may`, and only `@while` for a `forbidden`. If a clause is missing or English, draft it from the sentence + the action and confirm with the user. `spec-lint` FAILs a `@shall` that is constant or names no state (`shall-constant`, `shall-no-state`) and any `_`-ghost in authored text (`requirement-reads-ghost`).

2. **Generate/refresh the probe module** at `specs/<target>.probes.qnt` (see `templates/probes.qnt.template`). It imports the area module, instruments steps with ghost vars, and declares, per requirement, an obligation `oblig_<REQ_ID with - → _>` (an invariant: every step of the action from `@while` ends in `@shall`) and a negated witness probe `witness_<REQ_ID with - → _>`:

   - **Ghost vars** — underscore-prefixed so they can never collide with real model vars: `_lastAction` (which action produced the state), one param ghost per parameter TYPE POSITION (`_arg_UserId_1`, `_arg_bool_2`, … — the k-th parameter of that type in an action's signature, so what a model costs to check does not depend on what its parameters are called; the header lists the name → slot map), one `_pre_<REQ>` boolean per requirement (its `@while`, evaluated before the step over the step's own arguments), one `_old_<REQ>_<name>` per `@old`, and one `_prev<Var>` snapshot per state var a transition invariant reads. Ghost initial values in `initP` are written explicitly (`_prevSessions' = Map()`), never `= sessions`: before init there is no prior state to read. `initP`/`stepP` wrap the area's `init`/`step`, tagging every branch. Every trace the recorder writes from this module is restored to `_last<Param>` names (`_lastUid`, `_lastSid`, … — each holding what its action last passed), and the replay harness reads call parameters from those — never infer them from state diffs.
   - **Path-constrained checks**: both the obligation and the witness are about steps where `_lastAction == <quint_ref>` and `_pre_<REQ>` held, and they call `post_<REQ>(_arg_UserId_1, …, _old_…)`, the generated `def` of `@shall` over the step's own arguments. So the claim is about *this call*: a state some other call produced cannot satisfy it.
   - **Transition invariants** — any invariant declaring `over: "probes"` is emitted here too, as `val <quint_name>: bool = <invariants[].predicate>`, **positively**: a probe is negated so its counterexample is the witness, while a counterexample to one of these is a violation. The `_prev*` ghosts such a predicate reads are declared and snapshotted for it. It is authored in the model as a `def` whose parameters ARE the `_prev*` ghosts (`def stable_over(_prevSt: UserId -> St): bool = st == _prevSt`, record `@inv INV-NNN` + `@quint-name stable`) — the parameters make it typecheck in the model, the generator emits its body into the probe module where the ghosts live; see "Transition properties" below.

```quint
module auth_probes {
  import auth.* from "./auth"
  var _lastAction: str
  var _arg_UserId_1: UserId
  // initP / stepP wrap init / step, tagging branches (see template)

  // REQ-003, generated from @while / @old / @shall on login_failed
  def post_REQ_003(uid: UserId, before: int): bool =
    attemptsOf(uid) == before + 1
      and (isLocked(uid) iff attemptsOf(uid) >= MAX_FAILED_ATTEMPTS)
  // Every step: violated ⇔ some login_failed from @while broke it
  val oblig_REQ_003: bool =
    not(_lastAction == "login_failed" and _pre_REQ_003)
      or post_REQ_003(_arg_UserId_1, _old_REQ_003_before)
  // Some step: violated ⇔ such a step happened (the trace is the witness)
  val witness_REQ_003: bool =
    not(_lastAction == "login_failed" and _pre_REQ_003
        and post_REQ_003(_arg_UserId_1, _old_REQ_003_before))
}
```

Record the file in `formal_model.probes_file`. The probe module is generated — regenerate, never hand-edit.

**Multi-module areas** (`kind: contract`, or any area whose `spans[]` names other areas — typical for areas with UI blocks): the probe module imports *every* spanned module and `stepP` is an `any` over all of their wrapped actions, so joint behaviors are explored; `@while` / `@shall` range over the joint state. Pattern is in the template.

### Step 2a — Run the recorder (mechanical work)

```bash
tools/spec-record.py check <target> [--steps N] [--only INV-001,REQ-003] [--no-witness] [--parallel N]
```

The runner does, deterministically: an advisory `quint run` pre-gate (seconds, writes `check_results.simulation` and never a verdict — `--only-simulate` for the fast loop while drafting, `--no-simulate` to skip), every invariant check (batched into one `quint verify` first, per-id with counterexample traces to `specs/<target>/traces/<ID>.cex.itf.json` when the batch isn't clean), every property as a temporal formula (`--temporal`, on TLC — no step bound, no ITF trace), every witness probe (`--init=initP --step=stepP`, violation trace = the witness), skip-if-fresh (a REQ already `witnessed` against the current `model_sha` is not re-proven), and all JSON write-backs: `check_results`, `formal_status`, `witness.{status,trace,checked_at,model_sha}` — written **as each check completes**, so a run stopped by Ctrl-C or by its budget keeps what it proved (`check_results.ran_at`, the field that means "this area was checked", is the one thing stamped only at the end). Examples and journey runs are checked by `quint test` (`quint test specs/<target>.qnt`; CI's spec gates run it): a failing example is a model that disagrees with a case a human approved, and a failing journey run is a flow whose steps do not work one after another — report both as loudly as a counterexample. If it reports `quint` missing, run `tools/check-tooling.sh` for install hints — which also reports which of the optional flags (`run --backend`, `run --witnesses`, `verify --invariants`, `verify --temporal`) your quint has.

**Bounded checks run in two passes, not one.** A counterexample is shallow — the pre-gate falsifies invariants in milliseconds — while depth is only needed to *fail* to find one. So:

- **Pass 1 (shallow, `shallow_steps`)** runs every bounded invariant and every witness probe at the shallow bound. A counterexample here is **final**: its trace is recorded and it is never re-run, because depth cannot unfind it. A timeout or error is final for the same reason — running the same check deeper will not make it answer.
- **Pass 2 (deep, `max_steps`)** re-runs only what came back clean, for as long as `budget_seconds` lasts. Pass 1 itself is never budget-gated: it is the cheap half *and* the half that carries the findings, so every selected check always gets at least a shallow verdict.
- **Ordering:** pass 1 runs the invariants the pre-gate already falsified first (`check_results.simulation.invariants.falsified`) — guaranteed counterexamples, cheaply. Witness probes are ordered by the same signal inverted: a probe the simulator *hit* is about to be witnessed in milliseconds, a probe it never reached is the expensive tail. A run with `--no-simulate` has no such signal and keeps declaration order.
- **The ladder does not apply** to `proof: "inductive"` (unbounded), `proof: "structural"` (Alloy, finite scope), `proof: "smt"` (z3), or temporal properties (TLC, no step bound). A step ladder is meaningless for all four, and each keeps its existing single-pass path.
- **Pass 1 says what it will cost.** After the first few checks the runner prints a projection (`pass 1: 9/48 checks, ~3.5m/check × 5 parallel → ~6m projected; budget 15m …`) and records it in `check_results.cost`; when the budget stops the deep pass, `check_results.cost.deep_skipped` says how many checks rest on the shallow bound and the readback names the cause. Report the projection to the user when it exceeds the budget, and point at `tools/spec-lint.py <target> --cost` for where the cost comes from.
- **Checks run in parallel** (`--parallel N`, default `apalache.parallel` or half the CPUs): they are independent, and one Apalache server answers concurrent queries concurrently. Verdicts are still written one at a time, in the same order, as each is reached.
- **Each probe-module check runs on its own slice** of the probe module (under `specs/<target>/gen/slices/`, gitignored): the same `stepP` and the same formula, without the ghosts the check never reads. A witness carries only its own `_pre_`/`_old_` ghosts, not every requirement's. The committed `specs/<target>.probes.qnt` stays the artifact CI typechecks; `quint.slice_probes: false` turns slicing off.
- **A witness the simulator already reached is taken from the simulator.** A witness is existential — one run that fired the step from `@while` and ended in `@shall` is the proof — so for every event-form probe the pre-gate hit, the runner asks `quint run` for that violating trace and records it with `witness.found_by: "simulator"` and `witness.steps` = the trace's own length. Only the probes it never hit go to Apalache. The positive answer is the only one ever taken from the simulator: **`NO-WITNESS` always comes from Apalache**, and obligations and invariants (universal claims) always do. `quint.simulator_witnesses: false` sends every witness to Apalache.
- **Server hygiene:** once any bounded check times out, the remaining ones add `quint verify --server=false`. Killing a timed-out check leaves its query inside the shared Apalache server, and without this the deep pass would inherit that congestion. Probe-gated — a quint without the flag behaves as before.

`--only` narrows the set of checks but still runs each survivor through the ladder. It is a tool for extracting one trace, not a substitute for the two passes — don't reach for a hand-written `--only` list to make a run cheaper.

Interpret its output:

- **`NO-WITNESS`** → report loudly: the behavior is unreachable — impossible guard, missing action, or bound too small. The line says which depth it was looked for at (`witness.steps`): a probe whose deep pass the budget stopped is unreachable *within the shallow bound only*, which is a far weaker statement than unreachable within the ceiling — never report the second when the record says the first. Offer: (a) inspect the guard and the `@while`, (b) raise `--steps` (or `--budget`, when the deep pass never ran), (c) if the requirement is really a rejection, make it `@modality forbidden` with `@while` saying when; its obligation is then the proof and the recorder writes `status: "skipped"` itself (see METHODOLOGY.md → "Obligations and Witnesses" and "Refusal Examples"). (a)/(b) are yours to act on; (c) changes the claim, so only with the user's confirmation.
- **`no-probe` / `no-probes-file`** → regenerate the probes module: `tools/spec-probes.py <target>`. Do NOT hand-roll it. The generator reads the derived area and the model's IR and fails rather than emitting a module that omits an action — the failure mode of writing it by hand is that a miscounted branch leaves actions out of `stepP`, and every probe pointing at them then silently cannot fire. It needs `formal_model.probe_domains` (one Quint set per parameter TYPE); if that is missing it prints the exact JSON to add to the intent file.
- **`VIOLATED` (obligation)** → the checker found a reachable step of the action, starting where `@while` held, that does not end in `@shall`. This is the requirement failing in the model; the trace (`<REQ>.violation.itf.json`) shows the step. Translate it into plain language and ask which is wrong: the model (fix the action), or the requirement (a missing `@while` condition, or a `@shall` that says too much). Never weaken `@shall` to make it pass without the user's confirmation.
- **`HOLLOW`** → the probe found a counterexample of ONE state, so nothing was demonstrated. Witnesses require `_lastAction == <quint_ref>`, so this means the probe module is stale or hand-edited: regenerate it and re-run.

- **`NO-WITNESS` on one outcome of a `may`** → each outcome's probe requires the other outcomes' predicates to be FALSE, so a permitted outcome the state cannot tell apart from its siblings is unreachable by construction. That is the finding: the model has no state that distinguishes "confirmed by email" from "confirmed in-app". Give each `@outcome` a condition over state that tells it apart (usually: model the thing that differs), regenerate the probes, re-run.
- **`simulate` lines** → these come from the advisory `quint run` pre-gate, and they are **never** verdicts. `no violation in explored runs` is not `verified`; only the `quint verify` lines below it decide anything. Two are worth acting on: a `VIOLATION FOUND` tells you the bug is shallow enough that a random walk hit it (the Apalache line right below will carry the trace), and `0 traces` on a witness says no random execution ever reached that behavior — treat it as an early `NO-WITNESS` warning and start looking at the guard before the model checker confirms it.
- **Properties (PROP-NNN) honesty rule:** a PROP is a **temporal** formula, so it runs as `quint verify --temporal=<quint_name> --backend=tlc`, not as an invariant — Apalache's temporal support is partial, and the checker for this path is TLC (override in `.spec/project.json` → `quint.temporal_backend`). Report a TLC `verified` as *exhaustive over this model's finite state space*, and say the second half out loud: no step bound, but no instance larger than the model declares either, so widening the model's own domains is what strengthens it. TLC writes **no ITF trace**, so a PROP counterexample arrives with nothing to diagram — say that rather than leaving the user hunting for a trace file. On `timeout` (state explosion is TLC's failure mode), suggest demoting the PROP to a witness-traced scenario (a `run` demonstrating the eventuality once) plus a fairness note — don't leave the user believing liveness was proven.
- **Transition properties (`over: "probes"`):** an invariant that needs the state BEFORE as well as after — "X never changes", "Y only moves one way", "DELETED is terminal" — is not expressible over a single state and must not be written off as unenforceable prose. Do **not** reach for `always(P(next(x)))`: quint compiles it to `[](x = x')`, which TLA+ rejects (it wants box-action form `[][A]_vars`) — the route that works is making the pre-state a variable, which is what the `_prev*` ghosts are. So host the invariant on a `def` whose parameters are the ghosts it reads, and give the probe val a name: `/// @inv INV-004` · `/// @quint-name policyStable` above `def policyStable_over(_prevGroups: GroupId -> Group): bool = groups.keys().forall(g => groups.get(g).policy == _prevGroups.get(g).policy)` (`@over probes` is implied by the `_prev*` parameters). Then regenerate (`tools/spec-probes.py <target>`), which emits the body as `val policyStable` into the probe module, and `spec-record` checks it with `--init=initP --step=stepP`. `over: "probes"` with no predicate FAILs `invariant-over-probes-without-predicate` — nothing would declare the val. When a requirement's enforcing invariant seems impossible to state, check this first: the usual reason is that the property is a transition, not a state.
- **Invariants (INV-NNN) honesty rule:** by default `quint verify` checks invariants by **bounded** model checking. `formal_status: "verified"` therefore means *no counterexample within N steps* — NOT a proof, and the readback renders it `✓ (≤N steps)`. **N is per invariant**, recorded in `check_results.checks[].steps`: the two-pass ladder deepens what it has budget for and leaves the rest at `shallow_steps`, so two `✓` marks in the same area can rest on different depths. `check_results.max_steps` is the configured *ceiling*, not a claim that everything reached it. Read a check whose entry carries a budget note as *clean to the shallow bound, and never tried deeper* — the fix is `--budget` (or `apalache.budget_seconds`), or `--only` on the one invariant that matters, and saying so out loud beats letting a `✓ (≤3 steps)` be read as a `✓ (≤10 steps)`. When the user wants an unbounded proof for a load-bearing invariant (e.g. an auth guard), tag it `@proof inductive` in its record; `spec-record` runs `quint verify --inductive-invariant=<quint_name>` and a pass becomes `verified-inductive` (`✓ proven`). If quint reports the invariant isn't constrained enough ("x is used before it is assigned"), that's an honest non-proof — help the user strengthen the predicate, don't fall back to bounded and call it proven. Suggest upgrading any `critical` invariant the reviewer would read as "always true" to inductive.

Witness traces are inputs to `/spec-code-verify`'s conformance replay and to `/spec-readback`'s sequence diagrams — they are committed artifacts, not temp files.

Cascade economy: cascade only to contracts whose `spans` include areas actually changed (git diff against the last check), not every contract in the project.

(No separate action-coverage pass: witnessed REQs prove their actions fire; `spec-lint` flags unreachable actions as `orphan-action` statically — reachability, so a `step` wrapper is live without a requirement of its own.)

### Step 3 — Cascade to contracts (areas only)

If the target's `kind == "area"` and `--no-cascade` was not passed:

Enumerate `specs/*.intent.json` for files with `kind: "contract"` and `spans` containing this area. For each, run Apalache against `specs/<contract>.qnt` the same way.

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

### Step 4a — Per-action completeness

Apalache proves what the requirements say. It does NOT catch what an action does where no requirement speaks. `spec-probes` generates two checks per action for that, and `spec-record check` (Step 2a) already ran them with the obligations:

- `cover-<action>` — every step of the action is under some requirement's or verdict's `@while`. A counterexample is a state where the action fires and nobody said what should happen.
- `explain-<action>-<var>` — every step that changes an observable var is under a requirement that reads it, or an open GAP / OUT-OF-SCOPE verdict. A counterexample is an unstated side effect.
- `verdict-<action>-<n>` — an IMPOSSIBLE verdict's action never fires under its `@while`; a NO-OP verdict's action changes nothing observable there.

Lint names what is missing: `coverage-gap`, `change-unexplained`, `verdict-contradicted`, `action-unspecified` (an action with no requirement and no verdict).

**Close each finding (LLM).** Read the trace, then decide which one holds:
1. **The spec is silent and the behavior matters** — write the requirement (in conversation with the user when the answer is theirs), or put `/// @verdict GAP` on the action with `@while` narrowing it to the uncovered case and `@question Q-NNN`, and append that `Q-NNN` to `open_questions[]` with `source: "coverage"` (or `"explanation"` for an unexplained change).
2. **It cannot happen** — `/// @verdict IMPOSSIBLE` with the `@while` it cannot happen under and the argument as its description. The checker then proves it.
3. **It happens and deliberately changes nothing** — `/// @verdict NO-OP`, naming the deciding requirement in the description. Proven the same way.
4. **Outside the boundary** — `/// @verdict OUT-OF-SCOPE` with `@scope <scope.excluded item>`.
5. **The var is bookkeeping no requirement should talk about** (a clock, an index) — `/// @internal <why>` on its `var`.

Then regenerate probes (`tools/spec-probes.py <target>`) and re-run `spec-record check`. Surface ambiguous cases to the user instead of guessing; a verdict is a claim about intent.

A guard can hide a gap: when an action refuses to fire in some state, no check sees that state. When a refused case matters ("what happens on a failed login to a locked account?"), it belongs in `open_questions[]`.

### Step 4a-ter — Code → spec coverage

Run `tools/spec-extract-audit.py <target> --record` whenever the area has code (`traceability[]` entries, a triage ledger, or requirements carrying `extraction.evidence`). Skip it only for an area with no implementation yet, and say so rather than passing over it silently.

Why it belongs in `/spec-check` and not only in extraction: every other completeness pass measures the spec against itself — coverage against its own requirements and verdicts, the outcome pass against its own `externals[]`, the witnesses against the model. This is the only one that measures the spec against the **implementation**, which makes it the only one that can find behavior the spec never mentions. Running it once during extraction leaves the number resting on whoever last remembered the flag; running it every check keeps it true.

Read a **rising** unclaimed count as drift: the code grew behavior the spec has not caught up with. Report the number either way — `spec-lint` FAILs `extraction-sites-unclaimed` from `in-review` on, and the ship verdict carries it (`⚠ NOT READY — … 270 of 341 code site(s) unaccounted`), so a silent pass here is not available to you.

### Step 4a-bis — External × outcome completeness

Whenever the area declares `externals[]`, every declared outcome must be handled: by a requirement's `@error Ext.OUTCOME`, or by a `@verdict` on the action that calls the external carrying `@error Ext.OUTCOME` (IMPOSSIBLE: it cannot reach this area; NO-OP: it changes nothing, name the deciding requirement; OUT-OF-SCOPE: `@scope`; GAP: `@question Q-NNN`). Lint FAILs `outcome-unhandled` and, from approval, `outcome-open-gap`.

An unhandled TIMEOUT is the single most common real-world semantic hole; this pass exists to make it impossible to ship one silently.

### Step 4c — What the examples leave open (model mutation)

Run `tools/spec-questions.py <target> --record` once the checks above are clean and the area has examples. It needs quint and takes minutes (mutants run in parallel, one per CPU), so run it when the model or its examples changed, not on every fix-and-recheck loop. `.github/workflows/spec-mutation.yml` also runs it nightly and on every PR that changes a model, and writes the questions to the run's summary page.

What it does: it changes the model one small step at a time (a comparison at its boundary, a dropped guard, a dropped effect, a state written as another variant, a constant moved by one) and runs every `run` and invariant against each change. A change something turns red on is pinned. A change nothing notices is a behavior the model decided and nobody approved, and the tool finds the first step where the model and the changed model part. It records each one as a question in `check_results.mutation.questions`: Given / When, a yes/no question, and the example run that answers it as the model does.

The questions are the user's to answer, never yours. Ask them one at a time (AskUserQuestion when available), in the words of the readback's "Questions the Examples Leave Open" section:

- **Yes** → add the printed run to the model as an `@example` on the requirement in `refs` (give it a one-line title and a real name in place of `q_<key>`). Rerun: that mutant now dies.
- **No** → the model is wrong there. Ask what should happen, fix the model, and add the example with the expectation they gave. This is the cheapest bug there will ever be: no code exists yet.
- **Doesn't matter** → that is an answer too: the behavior is genuinely free. Record it as an `open_questions[]` entry with `status: "answered"` and the reason, so the next run's identical question can be recognised.

Report the score (`score`: the share of behavior-changing mutants the examples kill) and how many questions are open. A low score is not a failure; it says how much of the model is the agent's choice rather than the user's.

### Step 4b — Adversarial red-team

Runs **only when `--reality` was passed**. Suggest it when the requirement set changed materially since the last red-team (new REQs/INVs, version minor bump) — not during counterexample-fix loops.

Spawn an Agent (subagent_type=general-purpose) in skeptical-SRE mode. Pass it: `entities[]`, `requirements[]`, `invariants[]`, `properties[]`, `concepts` from the derived area (`tools/spec_source.py derive <target>`). Prompt:

> You operate this product for a demanding customer (SRE / security reviewer / compliance auditor). Read these requirements, invariants, and entity model. Category list: authentication & RBAC, multi-tenancy isolation, audit trail, observability (metrics/alerts/SLOs), idempotency of user actions, API/CLI surface beyond UX, quotas & rate limits, crash recovery of control plane mid-operation, time/timezone semantics, encryption at rest/transit, backward compatibility & migration, concurrent operators. First discard the categories this area plausibly never touches (one line each: category — why not). For the remaining categories, generate the questions the spec does NOT answer — scale to the spec, roughly one question per 3 requirements, max 20. Every question must cite which REQ/INV/entity the gap touches, or state "no anchor — entirely absent"; drop any question you can neither anchor nor justify as an absence. Categorize each as `critical` / `important` / `nice-to-have`. Return JSON array.

For each returned question:
- **critical** → append to `open_questions[]` as `Q-NNN` with `source: "red-team:critical"`.
- **important** → append with `source: "red-team:important"`.
- **nice-to-have** → write to `specs/<target>/gen/redteam-backlog.md` (not into the spec, and gitignored — anything that must survive gets promoted to a Q-NNN).

De-dupe against existing `open_questions[]` by semantic similarity before appending — red-team will re-raise prior gaps every run.

### Step 5 — Fill in the judgment fields

`spec-record` already wrote `check_results`, `formal_status`, and the `witness` blocks. Two things remain yours:

For each `result: "counterexample"` entry in `check_results.checks[]`, write `counterexample.nl_explanation` — the Step 4 translation, condensed to a paragraph. (The runner carries an existing explanation over when the result is unchanged; write it once per new counterexample.)

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
Settings:       max_steps=10 (shallow 3), timeout=300s/check, budget=900s/run

Formal results (Apalache):
  ✓ INV-001 singleSession         VERIFIED (≤10 steps)        (2.3s)   bounded — not a proof
  ✓ INV-002 guardedDashboard      VERIFIED (inductive)        (2.9s)   proven over all reachable states
  ✗ INV-003 noLockedSession       COUNTEREXAMPLE              (1.8s)
  ⏱ PROP-001 eventualLogout       TIMEOUT after 300s — bounded liveness only; consider demoting to a witnessed scenario
  ✓ INV-CONTRACT-001 noOrphan     VERIFIED (≤10 steps)        (3.1s, via cascade from user-permission)

Structural results (Alloy — only when an invariant declares proof: "structural"):
  ✓ INV-CONTRACT-002 noSharedSess  VERIFIED IN SCOPE           (0.4s)   4 Session, 4 Account — not a proof

Witness obligations:
  ✓ REQ-001 login reachable        WITNESSED  → auth/traces/REQ-001.itf.json (4 states)
  ✓ REQ-003 lockout reachable      WITNESSED  → auth/traces/REQ-003.itf.json (6 states)
  ✗ REQ-004 expiry reachable       NO WITNESS — expire_session unreachable from init. Guard impossible?

Reality-gap results:
  coverage:  5 actions; 4 covered, 1 uncovered (unlock_account → GAP verdict, Q-008)
  explained: 9 var changes; 8 explained, 1 unexplained (logout changes failedAttempts)
  red-team:  (not run — pass --reality after material spec changes)

Counterexamples translated above. 12 new open questions (Q-007..Q-019).

Next:
  - Fix INV-002 in specs/auth.qnt (suggested edit shown above), then re-run /spec-check.
  - Triage open_questions[] with /spec auth — critical red-team gaps (Q-010..Q-012) likely need new REQs.
```

Always end with a concrete suggested next step.

### Step 7 — Commit

```bash
git add specs/<target>.qnt specs/<target>.intent.json specs/<target>.records.json specs/<target>.probes.qnt specs/<target>/traces/
git commit -m "spec(<target>): check — <summary>"
```

(`specs/<target>/gen/` is gitignored — the red-team backlog is regenerable; omit probes/traces if `--no-witness` was passed)

Show the commit message; wait for confirmation.
