# Proof-rate spike — results

> Historical record. This spike led to the Lean-only design now in this repository. The scripts, models and proofs it refers to (under `spike/proof-rate/`) and the Quint tooling were removed afterwards; they are in the git history at commit `9aff340`.

Run on 2026-11-11 against the three example areas (`auth`, `subscription`, `auth-ui`). Question: can a cheaper AI model (Sonnet), working unattended, prove this repo's invariants at the upper levels of the model ladder in [GOAL.md](../GOAL.md)?

**Answer: yes, for models of this size. Every target was proven, each in one to two minutes, and every result passed an independent re-check.**

A third round measured what a Lean-only design depends on: exhaustive search written in Lean matched TLC's state count and ran faster on one core than TLC on ten, and proofs broken by a model edit were repaired in under a minute. See "Round 3" below.

A second round on 2026-11-12 repeated the measurement on three real models of up to 2,600 lines and added Dafny. **The unattended rate did not fall: Lean and Dafny reached the correct verdict on every invariant. What broke was tooling**, namely the Quint-to-Lean generator and the Apalache route on the largest model. See "Round 2" below.

## Round 1 results (example areas)

| Arm | Ladder level reached | Targets | Proven | Agent effort |
|---|---|---|---|---|
| Lean 4 (kernel-checked) | 1 — all sizes, all steps | 9 invariants | 9 | 1–10 builds, 22–150 s each |
| Apalache inductive invariant | 2 — all steps, fixed finite set of ids | 10 invariants in 3 areas | 10 | 2–11 checker runs per area, 50–142 s per area |
| Writing a new Quint model from a paragraph of English | — | 2 models | 2 pass type-check, runs and random simulation | first clean type-check on attempt 1 and 2 |
| TLC exhaustive (no AI, baseline) | 2 | 2 models | see below | — |

All 14 agents together used about 736k tokens.

### Lean, per invariant

| Invariant | Builds | Proof lines | Strengthening the agent had to find |
|---|---|---|---|
| auth `atMostOneActiveSession` | 2 | 83 | none (restated per key) |
| auth `noSessionWhileLocked` | 7 | 116 | none (restated over all users) |
| auth `noAccessWithInvalidToken` | 10 | 164 | a member of a user's active set is an Active session owned by that user |
| auth `noEarlyLockout` | 4 | 98 | a locked account has at least 5 recorded attempts |
| auth `noEarlyExpiry` | 1 | 175 | an Expired session has a clock entry above the limit |
| subscription `noBillingAfterCancel` | 2 | 71 | none |
| subscription `noRefundIssued` | 2 | 61 | restriction to keys the status map holds |
| subscription `retriesWithinBudget` | 2 | 69 | every stored retry counter is at most 3 |
| auth-ui `guardedDashboard` | 2 | 62 | none |

### Apalache inductive, per area

| Area | Targets proven | Checker runs | Time per check | What the agent added |
|---|---|---|---|---|
| auth | 5 of 5 | 11 | about 8 s | type constraints for six variables plus seven strengthening facts |
| subscription | 4 of 4 | 5 | about 6 s | type constraints plus a status-to-flags table; this made `unacceptedCancelLeavesActive` inductive, which the repo records as not provable this way |
| auth-ui | 1 of 1 | 2 | about 6 s | type constraints only |

The auth clock grows without bound; Apalache accepted it typed as an unbounded integer, so no cap was needed.

### TLC exhaustive baseline (no AI)

| Model | Result |
|---|---|
| subscription | all four invariants, 144 states, under 1 s |
| auth as written | did not finish in 10 minutes: the clock is unbounded, so the state space is infinite |
| auth with the clock capped just above the timeout | all five invariants, 14.5M states, 3 min 10 s on 10 cores |

### Quint fluency

Both models were written from a one-paragraph description, with the template and one example model as reference: `approval.qnt` (239 lines, 6 requirements, 4 invariants, 5 runs) and `reservation.qnt` (214 lines, 6 requirements, 3 invariants, 6 runs). Errors made:

- a `match` arm that tried to bind a variable, which Quint does not allow (one failed type-check);
- `quint test` with its default name filter ran zero runs and reported success, in both sessions. This is a tooling trap rather than a language problem: a green result with nothing executed.

## Round 2: real models and Dafny

Three models from real projects built on this template, anonymised here as A, B and C. Their files, the agents' ports and the proofs were kept out of this repository and deleted after the audit; only the numbers below remain.

| Model | Size | Invariants |
|---|---|---|
| A | about 500 lines, 6 actions | 2, both true |
| B | about 900 lines, 13 actions, imports A | 7, all true |
| C | about 2,600 lines, 17 actions, all state in one large record | 5: 2 true, 3 false |

The three false invariants of C were already recorded as violated in that project's own ledger, each with a counterexample. The agents were not told.

### Verdicts

| Model | Lean | Dafny | Apalache inductive |
|---|---|---|---|
| A | 2 proven, on the generated model | 2 proven | 2 proven |
| B | 7 proven, on a model the agent ported | 7 proven | 7 proven, on the original model |
| C | 2 proven, 3 refuted, on a model the agent ported | 2 proven, 3 refuted | 0 proven |
| Correct verdicts out of 14 | 14 | 14 | 9 |

Dafny was also run on two example areas from round 1: `auth` 5 of 5 (for any number of users and sessions) and `subscription` 4 of 4. Those two files are in `dafny/`.

For the three false invariants of C, neither the Lean nor the Dafny agent forced a proof. Both reported them false, gave action sequences, and identified the same root causes the project's ledger records. The Dafny file states each counterexample as a lemma, and those lemmas verify. The two true invariants of C were proven for all steps; the project's ledger had them only to 3 steps, its deep pass skipped for budget.

### Effort

| Model | Lean | Dafny | Apalache |
|---|---|---|---|
| A | about 50 s per invariant | 34 s, 1 verifier run | 48 s, 3 checker runs |
| B | 18 min, 224k tokens, 914-line proof | 17 min, 119k tokens, 10 runs | 3.5 min, 9 runs, 11 s per check |
| C | 7.5 min, 150k tokens, 32 builds | 4.4 min, 82k tokens, 7 runs | 14 min, nothing proven |

The round used about 1.07M tokens. Dafny was the cheapest to drive: most lemmas verified with empty bodies and a full check took 2 to 28 seconds. Two solver timeouts occurred; the agent resolved both by splitting a lemma per action.

### Where it broke

- **The Quint-to-Lean generator handles one of the three models.** On C its output does not compile: the model has its own type called `State`, which clashes with the generator's state structure, and record fields are lost. On B it refuses a construct it cannot translate (`actionAll`). For B and C the Lean agent therefore ported the model by hand, as the Dafny agent always does.
- **The Apalache inductive route failed on C.** Apalache needs a membership test for each state variable before any other use. C keeps all state in one record, Quint has no constructor for a set of whole records, and enumerating the records would need on the order of 10^8 values for one field alone. No type constraint was accepted, so nothing could be checked.
- **Bounded checking of C is slow.** One counterexample at 4 steps took 193 s; another bounded check was stopped after 20 minutes.

### How round 2 was checked

- **Lean:** rebuilt; scanned for forbidden constructs; the prelude is unchanged; each theorem has the required form and rests only on `propext`, `Classical.choice` and `Quot.sound`.
- **Dafny:** re-verified with a 120 s limit per condition: 0 errors, no `assume`, `{:axiom}`, `{:verify false}` or `expect`; the required lemmas are present with the required pre- and postconditions.
- **Apalache:** every passing command re-run on a separate port; the models only gained declarations.
- **Refutations of C:** matched against the invariants' recorded status and counterexamples in that project's ledger.

### What round 2 does not show

1. **The ports are untrusted.** For B and C the Lean and Dafny models were written by the agent, which [GOAL.md](../GOAL.md) decision 8 counts as not trusted. Their fidelity is supported only by agreement: Lean and Dafny agree with each other, with Apalache on the original model B, and with the project's ledger on C.
2. **Fixed id sets.** The real models fix their entity ids as constants, so these proofs cover any number of steps, not any number of entities.
3. **One attempt per target**, and effort figures are self-reported.
4. **Not covered:** one two-state invariant of B, Verus and TLAPS.

## Round 3: is a Lean-only stack feasible in time?

Run on 2026-11-12 on the example `auth` model. Two questions a Lean-only design depends on: is exhaustive search fast enough when written in Lean, and how much repair do proofs need when the model changes?

### Exhaustive search in Lean against TLC

`lean/Search/Auth.lean` is a 75-line depth-first search over the generated Lean model, compiled to native code. Same scope as the TLC baseline: three users, three sessions, clock capped one tick above the timeout.

| | Lean search | TLC |
|---|---|---|
| Distinct states | 14,461,775 | 14,461,775 |
| Violations | 0 | 0 |
| Wall time | 113 s | 191 s |
| Cores used | 1 | 10 (1,689 CPU-seconds) |
| Peak memory | 1.2 GB | not measured |

The state counts are identical, which cross-checks the two tools. Notes: both remember visited states by 64-bit fingerprint, so a collision could hide a state; TLC generated about four times as many successor states because the Quint `step` draws a user and a session for every action, including those that ignore them; the search program itself is not proven correct.

Reproduce with `cd spike/proof-rate/lean && lake build authsearch && .lake/build/bin/authsearch`.

### Proof repair after a model edit

Three edits applied to copies of the Lean model and the Dafny port of `auth`, each with five proven invariants.

| Edit | Lean: proofs broken | Lean: repair | Dafny: errors | Dafny: repair |
|---|---|---|---|---|
| Lockout threshold 5 → 7 | 1 of 5 | 26 s, 4 builds, 4 lines | 0 | none needed |
| Unlock no longer resets the attempt count | 0 of 5 | none needed | 0 | none needed |
| New action added (ends all of a user's sessions) | 5 of 5 | 35 s for all five, 1 build each, 37 lines | 1 | 19 s, 1 run, 16 lines |

- The one Lean proof broken by the constant change had the number 5 written into its helper invariant. The repair refers to the constant instead, and the repaired proof still builds when the constant is moved again to 9.
- A new action breaks every Lean proof, because each proof analyses the actions case by case. Each repair was one added case and built at the first attempt.
- All repaired proofs were re-audited: statements unchanged, no forbidden constructs, standard axioms only; the Dafny file re-verifies with 0 errors.

### What round 3 does not show

1. **One small model.** `auth` has six actions. Repair effort on a model with dozens of actions, where a new action interacts with many invariants, is not measured.
2. **The edits kept every invariant true**, and the new action resembled an existing branch, which the agent was told. An edit that needs a new strengthening fact, or that makes an invariant false, would cost more.
3. **One attempt per repair**; build counts are self-reported.
4. **Search throughput is for one model.** Wider states or larger scopes will be slower per state, and the search is single-threaded.

## How round 1 was checked

Nothing here rests on an agent's own report.

- **Lean.** `audit_lean.py` rebuilds the project, rejects any proof containing `sorry`, `admit`, `native_decide`, `axiom` and similar, checks the theorem statement is the one the spike fixed, restates each theorem against the generated model, and lists the axioms each proof rests on. All nine rest only on `propext`, `Classical.choice` and `Quot.sound`. The three model files are byte-identical to what `tools/lean_gen.py` generates.
- **Apalache.** Each passing command was re-run by hand on a separate port; all returned no violation. A diff against the original models shows only added declarations, none changed or removed.
- **Quint fluency.** Both models were re-type-checked and their runs re-executed by name.

Reproduce the Lean audit with `python3 spike/proof-rate/audit_lean.py`.

## What round 1 does not show

1. **The models are small.** Three areas, at most six state variables and eight actions each. Most invariants were inductive as written or needed one or two extra facts. Round 2 addresses this with real models.
2. **One attempt per target.** There is no measure of run-to-run variation.
3. **The Lean proofs are about the generated Lean model.** That it matches the Quint model rests on the generator and on trace replay, which this spike did not run.
4. **The Apalache proofs hold for a fixed set of ids** (three users, three sessions). They cover any number of steps, not any number of users.
5. **Subscription agents had help.** A finished proof of a different invariant of the same model was available as a style reference.
6. **Build and run counts are self-reported** by the agents. The verdicts are audited; the effort numbers are not.
7. **Dafny was not tried in round 1** (it was in round 2); Verus and TLAPS were not tried at all.
8. **Two fluency samples** is too few for a rate.

## What it changes

- **Lean is not the weak option for this kind of problem.** The published benchmark figure for Lean (about 27%) is for general program verification. For state-machine invariants over a generated model, with a small prelude and one example proof, a cheaper model proved nine of nine.
- **Both upper ladder levels are reachable unattended on the current stack**, in about a minute per claim, without a person writing a proof.
- **The two level-2 routes complement each other.** TLC needs a finite state space and no AI. The Apalache inductive route needs an agent to supply the invariant but handles an unbounded counter.
- **Two limits recorded in the repo are out of date.** The inductive checks that error in the example ledgers were missing only type constraints, which an agent writes routinely. And a Lean proof no longer needs a person.
- **AI proving is not the bottleneck at this size** (round 2). On real models of up to 2,600 lines the cheaper model still reached every correct verdict, and declined to prove what was false.
- **The path from the authored model to the prover is the bottleneck** (round 2): the generator for Lean, state typing for Apalache.
- **Authoring directly in the prover's language looks viable** (round 2). Agents wrote 350 to 780 line Lean and Dafny models from the Quint sources in minutes. That would remove the translator, which is the part that failed, but it leaves the model's fidelity to be established another way.
- **Dafny is the cheapest route to drive** (round 2), at the price of a larger trusted base and occasional solver timeouts.
- **The Apalache route has a visible ceiling** (round 2): the strongest result on model B, where it ran on the original model with no port, and no result at all on model C.

## Files

- `lean/` — one Lake project: the prelude, the three generated models, the nine proofs under `Spec/<Area>/Proofs/`, and the round 3 search program `Search/Auth.lean`.
- `apalache/` — the example models with the added inductive invariants (`indInv`).
- `fluency/` — the two agent-written models.
- `dafny/` — the round 2 Dafny ports and proofs of the two example areas (`dafny verify <file>`). Nothing from the real models is stored here.
- `audit_lean.py` — the Lean audit.
