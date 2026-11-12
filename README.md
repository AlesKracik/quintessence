# Quintessence

Requirements as mathematical objects, so that the correctness of AI-written work does not rest on trusting the AI.

A requirement here is a statement about a state machine, written in [Lean 4](https://lean-lang.org). The machine checks it. A person reviews a generated page, not the Lean. The goal and every decision behind it are in [GOAL.md](GOAL.md).

## Using it

You work with an AI agent through four commands (in `.claude/commands/`, for Claude Code). They are stages of one loop, and dependencies rather than a schedule: any stage can be re-entered at any time, and a change to the requirements sends the later ones round again.

| Stage | Command | What happens | Done when |
|---|---|---|---|
| Spec | `/spec` | Write or change the model and its claims with the person; check; prove; answer open questions. For existing code, extract the model from it. | No violated claim; questions answered or knowingly left open |
| Readback | `/spec-readback` | Generate the review page for the change and walk the person through it | The person has no corrections; the pull request review approves |
| Code generation | `/spec-code-generate` | Write the code and its binding from the model, on the change's branch in the code repository | Code committed, binding in place |
| Code validation | `/spec-code-verify` | Drive the code through the model; resolve any drift with the person | The ledger records agreement |

The agent writes the model, the proofs and the code, and none of them needs to be trusted. A proof that is wrong does not compile. A requirement that the model breaks comes back with the shortest run that breaks it. Code that differs from the model comes back with the run that shows it. What the agent cannot settle is whether a requirement says what the person meant, which is what the readback is for.

Underneath, every command runs the same program, the **checker**: `lake exe check`. It is deterministic and involves no AI. The agent runs it, and so does CI; you do not need to. Everything below describes what it checks and how.

## Starting a project

A project is a directory with its own `lakefile.toml`. It can be a new repository, or a directory in an existing one, for instance the repository that holds the architecture documents or the code. You need [elan](https://github.com/leanprover/elan) and git; Lake fetches the framework for you.

1. In the project directory, create four files.

   `lean-toolchain`, the same as the framework's:

   ```
   leanprover/lean4:v4.22.0
   ```

   `lakefile.toml`. `rev` is a branch, tag or commit of the framework; a commit or tag keeps the project on a known version:

   ```toml
   name = "myproject"
   defaultTargets = ["check"]

   [[require]]
   name = "quintessence"
   git = "https://github.com/AlesKracik/quintessence"
   rev = "framework-redesign"

   [[lean_lib]]
   name = "Areas"
   globs = ["Areas", "Areas.+"]

   [[lean_exe]]
   name = "check"
   root = "Main"
   ```

   `Main.lean`, exactly these lines, in every project:

   ```lean
   import Spec.Main
   import Areas
   checker_main Areas.all
   ```

   `Areas.lean`, the list of areas, empty for now:

   ```lean
   import Spec.Check

   /-- Every area the checker runs. -/
   def Areas.all : List Spec.Entry := []
   ```

2. Fetch the framework and take its commands:

   ```
   lake update
   mkdir -p .claude && cp -r .lake/packages/quintessence/.claude/commands .claude/
   ```

   `lake update` puts the framework under `.lake/packages/quintessence/` and writes `lake-manifest.json`, which pins the exact version. Commit the manifest. Claude Code reads `.claude/commands/` from the directory it is started in, so in a subdirectory project either start it there or copy the commands to the repository root.

3. Add to `.gitignore`:

   ```
   .lake/
   out/
   .spec/local.json
   ```

4. Say where things are, in `.spec/`:

   `.spec/project.json` (committed): the architecture documents, if there are any, and every repository the code or the documents live in.

   ```json
   { "architecture": ["docs/architecture/", "platform-docs:services/billing.md"],
     "repos": { "billing": "git@example.com:org/billing.git",
                "platform-docs": "git@example.com:org/platform-docs.git" } }
   ```

   `.spec/local.json` (not committed): where you have those repositories checked out.

   ```json
   { "repo_paths": { "billing": "/home/me/src/billing", "platform-docs": "/home/me/src/platform-docs" } }
   ```

5. Check that it runs: `lake exe check` builds the checker and, with no areas yet, only writes an empty review page. The first build takes a minute or two.

6. For CI, copy `.github/workflows/check.yml` from the framework and replace `example` by the project's directory (or remove those settings when the project is the repository root).

From here on, work through the commands: `/spec` to write the first area, or to extract one from existing code.

Where the code is:

- In another repository: the binding says `repo := "billing"`, with paths relative to that repository's root.
- In the project directory: `repo` is left out, and paths are relative to the project directory (`example/app/`).
- In the same repository but outside the project directory: paths start with `../`.

To move a project to a newer framework version, change `rev`, run `lake update quintessence`, copy the commands again, run `lake exe check --full`, and commit the manifest and whatever changed in the ledger.

## How it works

Everything about one part of a system lives in an **area**:

| Part | What it is |
|---|---|
| Model | The state, the actions, and one function `step : action → state → new state or refused` |
| Invariants | What must hold in every reachable state |
| Requirements | "When this holds and that action arrives, the system shall …" or "… shall refuse" |
| Journeys | Customer journeys: the stories a person recognises the area by, each told as one run, moment by moment, in the person's words |
| Examples | Concrete runs with an expected outcome, for the boundaries a story would not dwell on |
| Scope | The finite set of actions (and an optional bound on states) the search explores |
| Code (optional) | How the area maps onto existing code: what to run for each action, what to read after each step |

The checker (`lake exe check`) goes through every area and does four things.

1. **Audits the proofs.** The program cannot even be built unless every attached proof is accepted by the Lean kernel and rests on nothing but Lean's three standard axioms.
2. **Searches the scope.** It runs the model through every reachable state of the scope, or until the budget is spent, and checks every invariant, requirement and example on the way.
3. **Checks the code**, for areas bound to code (see below).
4. **Reports.** It writes `ledger/<area>.json` (committed, so a change in what is verified shows up in the diff) and `out/review.html` (the page a person reviews), and fails if anything is violated.

### How strong is a claim

Each claim is reported at the strongest level it reached. The claim never changes to fit the level.

| Level | Meaning | How it is reached |
|---|---|---|
| 1 | Proven for all sizes and all steps | A kernel-checked proof is attached |
| 2 | Holds in every reachable state of the declared scope | The search finished |
| 3 | No counterexample within N steps | The search stopped at the budget |
| 5 | Not verified | The requirement never applied, or the claim is violated |

Invariants are expected to reach level 1 and requirements level 2. Anything weaker is shown as "below target".

### Journeys

People understand a system through the stories of its users, so the review page opens with the journeys. Each is a storyboard: one card per moment with what happens in plain words, whether the system did it or turned it down, the requirements at work in that moment, and what changed. The cards are generated from the run, so a journey cannot say something the model does not do: a moment that goes the other way fails the check. Journeys are also replayed against bound code, whatever the budget, so the stories a person has read are stories the code was seen to follow.

A journey stays inside one area. A story that crosses areas is told as one journey per area for now.

### Open questions

The search also finds what the requirements do not say, and reports it as questions for a person:

- an action is accepted in a situation no requirement covers;
- an action is refused in a situation no requirement covers;
- a requirement never applies anywhere in the scope;
- a requirement is part of no journey, or the area has no journey at all;
- nobody has decided whether an action can interleave with others inside the implementation.

Answering a question means adding a requirement, an example, or a decision. The requirements are complete, relative to the scope, when the list is empty.

## Existing code and drift

An area can be bound to code that already exists. The binding is a table, not a program: for every action the call to make in the code, and for every observation the expression that reads it. A small per-language adapter executes the table; it judges nothing.

On every run the checker drives the code through the model: every state of the scope, nearest first, every action the model accepts there, and every action it refuses in the state that follows. Each run starts from a fresh system, and after every step the checker compares what the code shows with what the model expects. The first difference is reported as drift, with the run that shows it:

- the model accepts an action and the code refuses it, or the other way round;
- an observation differs after a step;
- the code has an operation the model never uses (reported as an open question).

**Extraction** uses the same check. An agent reads the code and writes the model and the binding; the check then says, run by run, where that model is not what the code does. When the two agree, a person confirms the requirements and the binding on the review page. `example/Areas/Reservation` was extracted this way from `example/app/reservation.py`.

**New code** goes the other way: the code and the binding are written from the model, and the same check says when they are done.

**Drift** is the same check failing later. A person decides which of three things happened: the code is wrong, the model is wrong, or the change is deliberate and the requirement changes first.

The code verdict sits on its own scale. Agreement on every transition of the scope, or of the first N steps when the budget runs out, is reported as level 4: model-driven runs, systematic instead of random. It is evidence, not proof: code that remembers more than the binding observes can still differ on a path the check did not take.

## Layout

The framework is a Lake package. A project is its own repository that depends on it and holds only what is the project's.

```
The framework (this repository)
  Spec/Data.lean      finite maps and sets, and the definition of "reachable"
  Spec/Check.lean     the area records and the checker
  Spec/Main.lean      the checker program and the gate on what counts as proven
  Spec/review.html    the review page template
  adapters/python.py  runs model-chosen action sequences against Python code
  .claude/commands/   the four commands
  example/            a complete project, laid out as below

A project (its own repository)
  lakefile.toml       requires the framework
  Main.lean           three fixed lines: `checker_main Areas.all`
  Areas.lean          the list of areas
  Areas/<Name>.lean   one area: claims, scope, binding to code
  Areas/<Name>/       its model and proofs
  ledger/             what was verified, per area (committed)
  out/                the review page (generated, not committed)
  .spec/project.json  architecture documents and repositories of the system
  .spec/local.json    where this developer has those repositories (not committed)
```

When the system is described by architecture documents, list them in `.spec/project.json`, together with the repositories they point to. Extraction starts from them: the agent reads the documents to decide what an area is and where its code lives, then reads the code, which stays the reference. A difference between the two is a question for the person.

## Changes and git

**The area is the unit of meaning; the change is the unit of work.** A change is a git branch: `change/<name>` here, and the same branch name in every code repository it touches. Git tracks the change, the pull request review is the approval, and the committed ledger is the record of what was verified. There is no other bookkeeping, so several changes can be open at once, each on its own branch. When two changes touch the same area and their ledgers conflict, re-running the checker settles it, because the ledger is generated.

Git also answers "did anything change?":

- `lake exe check --since <revision>` compares with that revision. The review page then shows what the change added, removed or altered, which is all a reviewer has to read.
- A code check is re-run only when git reports different content for what its verdict depends on: the area's own files, the checker, the adapter and the code's files. The ledger records the state git reported when the verdict was reached; if git reports the same state now, committed or not, the verdict stands. With `--since`, the ledger of that revision is the one that counts as recorded, which is what a pull request check uses: it trusts the base branch, not the ledger the change brings. `--full` re-runs everything.
- A binding names where its code lives: this repository, or another one by name, with the local checkout given in `.spec/local.json` (`{"repo_paths": {"name": "/path"}}`, not committed). For another repository the ledger records the code commit the verdict is about, and a verdict on uncommitted code is never taken as recorded.

## What is trusted

- The Lean kernel.
- `Spec/Data.lean` and the record types in `Spec/Check.lean`: they fix what "reachable" and "proven" mean.
- `checker_main` in `Spec/Main.lean`: it rejects `sorry`, added axioms, `native_decide`, and constructs that let compiled code differ from the definitions the proofs are about. A project's `Main.lean` must stay the three fixed lines; a change to it, to the lakefile or to the CI workflow is a change to what counts as verified.
- For levels 2 and 3 only: the search in `Spec/Check.lean`, the Lean compiler, and 64-bit state fingerprints (two different states can, with very small probability, be taken for one). These are argued and tested, not proven.
- For the code verdict only: the adapter for the code's language, and the binding, which a person confirms on the review page.

## Not built yet

- Adapters for languages other than Python. The protocol is a few JSON messages, described at the top of `adapters/python.py`.
- Stronger code verdicts: every action sequence to a depth, symbolic checking, proof that the code matches the model.
- A code verdict per requirement. Today there is one per area.
- Recording a person's acceptance of a claim that stays below its target.
- Keeping a level reached by a long run when a later run has a smaller budget.
- For the model: randomized runs (level 4), and level 1 proofs for requirements.
- Checking that the requirements determine the behaviour completely, beyond the questions listed above.

## Requirements

[elan](https://github.com/leanprover/elan). The Lean version is pinned in `lean-toolchain`. Areas bound to Python code also need `python3`.

The measurements that led to this design are in [docs/spike-results.md](docs/spike-results.md).
