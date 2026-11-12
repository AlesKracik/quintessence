# Quintessence

Requirements as mathematical objects, so that the correctness of AI-written work does not rest on trusting the AI.

A requirement here is a statement about a state machine, written in [Lean 4](https://lean-lang.org). The machine checks it. A person reviews a generated page, not the Lean. The goal and every decision behind it are in [GOAL.md](GOAL.md).

## How it works

Everything about one part of a system lives in an **area**:

| Part | What it is |
|---|---|
| Model | The state, the actions, and one function `step : action → state → new state or refused` |
| Invariants | What must hold in every reachable state |
| Requirements | "When this holds and that action arrives, the system shall …" or "… shall refuse" |
| Examples | Concrete runs with an expected outcome |
| Scope | The finite set of actions (and an optional bound on states) the search explores |

One command checks every area:

```
lake exe spec
```

It does three things.

1. **Audits the proofs.** The program cannot even be built unless every attached proof is accepted by the Lean kernel and rests on nothing but Lean's three standard axioms.
2. **Searches the scope.** It runs the model through every reachable state of the scope, or until the budget is spent, and checks every invariant, requirement and example on the way.
3. **Reports.** It writes `ledger/<area>.json` (committed, so a change in what is verified shows up in the diff) and `out/review.html` (the page a person reviews), and fails if anything is violated.

### How strong is a claim

Each claim is reported at the strongest level it reached. The claim never changes to fit the level.

| Level | Meaning | How it is reached |
|---|---|---|
| 1 | Proven for all sizes and all steps | A kernel-checked proof is attached |
| 2 | Holds in every reachable state of the declared scope | The search finished |
| 3 | No counterexample within N steps | The search stopped at the budget |
| 5 | Not verified | The requirement never applied, or the claim is violated |

Invariants are expected to reach level 1 and requirements level 2. Anything weaker is shown as "below target".

### Open questions

The search also finds what the requirements do not say, and reports it as questions for a person:

- an action is accepted in a situation no requirement covers;
- an action is refused in a situation no requirement covers;
- a requirement never applies anywhere in the scope;
- nobody has decided whether an action can interleave with others inside the implementation.

Answering a question means adding a requirement, an example, or a decision. The requirements are complete, relative to the scope, when the list is empty.

## Layout

```
Spec/Data.lean      finite maps and sets, and the definition of "reachable"
Spec/Check.lean     the area records and the checker
Spec/review.html    the review page template
Audit.lean          the gate on what counts as proven
Main.lean           the `spec` program
Areas.lean          the list of areas
Areas/<Name>.lean   one area: claims and scope
Areas/<Name>/       its model and proofs
ledger/             what was verified, per area (committed)
out/                the review page (generated, not committed)
```

`Areas/Auth` is the worked example. Copy its shape for a new area and add it to `Areas.lean`.

## Working with an AI agent

The `/spec` command in `.claude/commands/` drives the loop: write or change the model and its claims with the person, run the check, prove the invariants, and walk the open questions one at a time.

The agent writes the model and the proofs, and neither needs to be trusted. A proof that is wrong does not compile. A requirement that the model breaks comes back with the shortest run that breaks it. What the agent cannot settle is whether a requirement says what the person meant, which is what the review page is for.

## What is trusted

- The Lean kernel.
- `Spec/Data.lean` and the record types in `Spec/Check.lean`: they fix what "reachable" and "proven" mean.
- `Audit.lean`: it rejects `sorry`, added axioms, `native_decide`, and constructs that let compiled code differ from the definitions the proofs are about.
- For levels 2 and 3 only: the search in `Spec/Check.lean`, the Lean compiler, and 64-bit state fingerprints (two different states can, with very small probability, be taken for one). These are argued and tested, not proven.

## Not built yet

- Checking code against the model. Every ledger says `"code": "not checked"`.
- Extracting a model from existing code, and detecting drift between code and model.
- Recording a person's acceptance of a claim that stays below its target.
- Keeping a level reached by a long run when a later run has a smaller budget.
- Randomized runs (level 4), and level 1 proofs for requirements.
- Checking that the requirements determine the behaviour completely, beyond the questions listed above.

## Requirements

[elan](https://github.com/leanprover/elan). The Lean version is pinned in `lean-toolchain`. There are no other dependencies.

The measurements that led to this design are in [docs/spike-results.md](docs/spike-results.md).
