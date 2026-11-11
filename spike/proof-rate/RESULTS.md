# Proof-rate spike — results

Run on 2026-11-11 against the three example areas (`auth`, `subscription`, `auth-ui`). Question: can a cheaper AI model (Sonnet), working unattended, prove this repo's invariants at the upper levels of the model ladder in [GOAL.md](../../GOAL.md)?

**Answer: yes, for models of this size. Every target was proven, each in one to two minutes, and every result passed an independent re-check.** How far this carries to real, larger areas is not measured here.

## Results

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

## How the results were checked

Nothing here rests on an agent's own report.

- **Lean.** `audit_lean.py` rebuilds the project, rejects any proof containing `sorry`, `admit`, `native_decide`, `axiom` and similar, checks the theorem statement is the one the spike fixed, restates each theorem against the generated model, and lists the axioms each proof rests on. All nine rest only on `propext`, `Classical.choice` and `Quot.sound`. The three model files are byte-identical to what `tools/lean_gen.py` generates.
- **Apalache.** Each passing command was re-run by hand on a separate port; all returned no violation. A diff against the original models shows only added declarations, none changed or removed.
- **Quint fluency.** Both models were re-type-checked and their runs re-executed by name.

Reproduce the Lean audit with `python3 spike/proof-rate/audit_lean.py`.

## What this does not show

1. **The models are small.** Three areas, at most six state variables and eight actions each. Most invariants were inductive as written or needed one or two extra facts. The rate on a real area will be lower, and this spike does not say how fast it falls.
2. **One attempt per target.** There is no measure of run-to-run variation.
3. **The Lean proofs are about the generated Lean model.** That it matches the Quint model rests on the generator and on trace replay, which this spike did not run.
4. **The Apalache proofs hold for a fixed set of ids** (three users, three sessions). They cover any number of steps, not any number of users.
5. **Subscription agents had help.** A finished proof of a different invariant of the same model was available as a style reference.
6. **Build and run counts are self-reported** by the agents. The verdicts are audited; the effort numbers are not.
7. **Dafny, Verus and TLAPS were not tried.** They are not installed on this machine.
8. **Two fluency samples** is too few for a rate.

## What it changes

- **Lean is not the weak option for this kind of problem.** The published benchmark figure for Lean (about 27%) is for general program verification. For state-machine invariants over a generated model, with a small prelude and one example proof, a cheaper model proved nine of nine.
- **Both upper ladder levels are reachable unattended on the current stack**, in about a minute per claim, without a person writing a proof.
- **The two level-2 routes complement each other.** TLC needs a finite state space and no AI. The Apalache inductive route needs an agent to supply the invariant but handles an unbounded counter.
- **Two limits recorded in the repo are out of date.** The inductive checks that error in the example ledgers were missing only type constraints, which an agent writes routinely. And a Lean proof no longer needs a person.
- **The next measurement should be harder targets**: one real area, to find where the unattended rate starts to fall.

## Files

- `lean/` — one Lake project: the prelude, the three generated models and the nine proofs under `Spec/<Area>/Proofs/`.
- `apalache/` — the example models with the added inductive invariants (`indInv`).
- `fluency/` — the two agent-written models.
- `audit_lean.py` — the Lean audit.
