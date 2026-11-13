# Quintessence

Requirements as mathematical objects, so that the correctness of AI-written work does not rest on trusting the AI.

A requirement here is a statement about a state machine, written in [Lean 4](https://lean-lang.org). The machine checks it. A person reviews a generated page, not the Lean. The goal and every decision behind it are in [GOAL.md](GOAL.md).

## Working with it

Everything is done by talking to an AI coding agent; the commands are written for Claude Code. You do not write Lean, edit configuration or run tools. You need the agent, git, and the address of this repository:

```
https://github.com/AlesKracik/quintessence
```

### 1. Start a project

Open the agent where the requirements should live: a new repository, or a directory in an existing one. Tell it:

> Set up a Quintessence project here. The framework is at https://github.com/AlesKracik/quintessence, branch `framework-redesign`. Read `SETUP.md` there and follow it.

It will ask you for a name, which repositories hold the code and where you have them, and what is written about the system (documents of any kind; none are required). It may ask to install Lean. It ends by running the checker once. From then on the four commands below are available.

### 2. Describe a part of the system, or have it extracted from code

Use `/spec` and say what you want in your own words:

> /spec We sell seats for events. A customer can put a seat on hold for a short time, then buy it or lose it.

> /spec Extract the billing component from the existing code. Start from docs/billing.md.

The agent asks which business cases that part of the system serves, takes each as a journey, and writes the requirements behind them. Then it checks its own work and comes back with what the check found: a run in which two requirements contradict each other, a situation no requirement covers, a place where a document and the code say different things. It asks these one at a time. You answer in plain words; it never answers for you.

### 3. Read it back

> /spec-readback

The readback is the model in a form you can read and correct. It is cut two ways, and both are generated: one page per area, the area as it stands; and one page per change, across every area the change touches. Each area's page opens with the journeys, each business case drawn as a graph, then the assumptions the model makes, every requirement with how strongly it is verified and where it comes from, the open questions that are yours to answer, and the decisions already taken. The page of a change shows only what that change did. The agent generates it and tells you what to look at. Say what is wrong and the agent goes back to step 2. Approving the pull request is the approval.

### 4. Have the code written

> /spec-code-generate

The agent asks where the code goes and in which language, writes it from the requirements, and connects it to them. For code that already exists this step is skipped.

### 5. Check the code against the requirements

> /spec-code-verify

The checker drives the code through the model and compares every step. When they differ, the agent shows you the run and you decide: the code is wrong, the requirement is wrong, or the change is intended and the requirement changes first.

### Later

A change is the same loop on a new branch:

> /spec Holds should last 15 minutes, not 2.

The readback then shows only what the change did, and the code check re-runs only where git shows something changed. Several changes can be in progress at once.

To use a newer version of the framework:

> Move this project to the latest framework revision, following SETUP.md.

### The stages at a glance

They are dependencies rather than a schedule: any stage can be re-entered at any time, and a change to the requirements sends the later ones round again.

| Stage | Command | What happens | Done when |
|---|---|---|---|
| Spec | `/spec` | Write or change the model and its claims with the person; check; prove; answer open questions. For existing code, extract the model from it. | No violated claim; questions answered or knowingly left open |
| Readback | `/spec-readback` | Generate the readback, the model as a page a person can read (journeys, assumptions, claims, open questions, decisions), and walk the person through it | The person has no corrections; the pull request review approves |
| Code generation | `/spec-code-generate` | Write the code and its binding from the model, on the change's branch in the code repository | Code committed, binding in place |
| Code validation | `/spec-code-verify` | Drive the code through the model; resolve any drift with the person | The ledger records agreement |

The agent writes the model, the proofs and the code, and none of them needs to be trusted. A proof that is wrong does not compile. A requirement that the model breaks comes back with the shortest run that breaks it. Code that differs from the model comes back with the run that shows it. What the agent cannot settle is whether a requirement says what you meant, which is what the readback is for.

Underneath, every command runs the same program, the **checker**: `lake exe check`. It is deterministic and involves no AI. The agent runs it, and so does CI. The rest of this page describes what it checks and how; you can use the framework without reading it.

## How it works

Everything about one part of a system lives in an **area**:

| Part | What it is |
|---|---|
| Model | The state, the actions, and one function `step : action → state → new state or refused` |
| Invariants | What must hold in every reachable state |
| Requirements | "When this holds and that action arrives, the system shall …" or "… shall refuse" |
| Journeys | The business cases a person recognises the area by, each as its usual course: steps in order, a loop where a step repeats, and every way the system turns a step down |
| State machines | The principal state machines: one per kind of thing with a life of its own, generic over all instances, drawn from what the search saw |
| Walkthroughs | Specific runs worth knowing, told moment by moment in the person's words, with the outcome checked where it matters: a failure and what follows, a boundary, a message that arrives twice |
| Scope | The finite set of actions (and an optional bound on states) the search explores |
| Code (optional) | How the area maps onto existing code: what to run for each action, what to read after each step |

Every requirement, invariant, journey and walkthrough also records where it comes from, as a history: extracted from the code, later changed by a person, confirmed against a document. The reviewer sees that history next to the claim.

The checker (`lake exe check`) goes through every area and does four things.

1. **Audits the proofs.** The program cannot even be built unless every attached proof is accepted by the Lean kernel and rests on nothing but Lean's three standard axioms.
2. **Searches the scope.** It runs the model through every reachable state of the scope, or until the budget is spent, and checks every invariant and requirement on the way; journeys and walkthroughs are replayed step by step.
3. **Checks the code**, for areas bound to code (see below).
4. **Reports.** It writes `ledger/<area>.json` (committed, so a change in what is verified shows up in the diff) and the readback under `out/readback/` (the pages a person reads: one per area, and one per change), and fails if anything is violated.

### How strong is a claim

Each claim is reported at the strongest level it reached. The claim never changes to fit the level.

| Level | Meaning | How it is reached |
|---|---|---|
| 1 | Proven for all sizes and all steps | A kernel-checked proof is attached |
| 2 | Holds in every reachable state of the declared scope | The search finished |
| 3 | No counterexample within N steps | The search stopped at the budget |
| 5 | Not verified | The requirement never applied, or the claim is violated |

Invariants are expected to reach level 1 and requirements level 2. Anything weaker is shown as "below target".

### Journeys and walkthroughs

People understand a system through what it is for, so the readback opens with the journeys. A journey is one business case, "enrolling a VM" or "a protection run", in general terms: no particular VM or customer, no counts, only the thresholds the requirements state. It is drawn as a graph: its usual course as steps from top to bottom, a loop on a step that can repeat, and beside every step each way the system can turn it down. The author writes only the usual course. The turned-down leaves are the area's refusal requirements for that step's action, so a negative decision cannot be forgotten, and a leaf cannot claim a refusal the model does not make. The course itself is replayed on the model, and a step the model does not take fails the check.

A walkthrough is one specific run worth knowing, told moment by moment: a run that fails and the next one, a boundary, a message that arrives twice. It is shown under the journey it is a case of, as a storyboard: one card per moment with what happens in plain words, whether the system did it or turned it down, the requirements at work, and what changed. A moment can state what must be true afterwards, and that is checked. Every turned-down leaf of a journey names the walkthroughs in which it happens, and the checker asks about a leaf that none shows. Walkthroughs are what examples and test scenarios are elsewhere; there is no separate kind.

Both are generated from the replay, so neither can say something the model does not do, and both are replayed against bound code, whatever the budget, so what a person has read is what the code was seen to follow.

A journey stays inside one area. A business case that crosses areas is told as one journey per area for now.

### Open questions

The search also finds what the requirements do not say, and reports it as questions for a person:

- an action is accepted in a situation no requirement covers;
- an action is refused in a situation no requirement covers;
- a requirement never applies anywhere in the scope;
- a requirement shows in no journey and no walkthrough, or the area has no journey at all;
- a journey's step can be turned down in a way that no walkthrough shows happening;
- for an area extracted from code, something the code offers that the model leaves out;
- anything the agent wrote down while reading documents and code: a document and the code disagree, or a document leaves something open;
- nobody has decided whether an action can interleave with others inside the implementation.

Answering a question means adding a requirement, a walkthrough, or a decision. The requirements are complete, relative to the scope, when the list is empty.

## Existing code and drift

An area can be bound to code that already exists. The binding is a table, not a program: for every action the call to make in the code, and for every observation the expression that reads it. A small per-language adapter executes the table; it judges nothing. There are adapters for Python and for Kotlin; the header of each says what the table's entries mean in that language.

On every run the checker drives the code through the model: every state of the scope, nearest first, every action the model accepts there, and every action it refuses in the state that follows. Each run starts from a fresh system, and after every step the checker compares what the code shows with what the model expects. The first difference is reported as drift, with the run that shows it:

- the model accepts an action and the code refuses it, or the other way round;
- an observation differs after a step;
- the code has an operation the model never uses (reported as an open question).

**Extraction** uses the same check. An agent reads the code, and the documents around it, and writes the model and the binding; the check then says, run by run, where that model is not what the code does. When the two agree, a person confirms the requirements and the binding on the readback page. `example/Areas/Reservation` was extracted this way from `example/app/reservation.py`.

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
  Spec/readback.html  the readback page template
  adapters/python.py  runs model-chosen action sequences against Python code
  adapters/kotlin.py  the same for Kotlin and other JVM code (compiles the binding once, then drives it)
  .claude/commands/   the four commands
  SETUP.md            how an agent sets up a project, step by step
  example/            a complete project, laid out as below

A project (its own repository)
  lakefile.toml       requires the framework
  Main.lean           three fixed lines: `checker_main Areas.all`
  Areas.lean          the list of areas
  Areas/<Name>.lean   one area: claims, scope, binding to code
  Areas/<Name>/       its model and proofs
  ledger/             what was verified, per area (committed)
  out/readback/       the readback: index.html, <area>.html, change.html (generated, not committed)
  .spec/project.json  repositories of the system and the documents written about it
  .spec/local.json    where this developer has those repositories (not committed)
```

Code is rarely the only source. Architecture and design documents, READMEs, comments, tests and product notes say what a system is for and how it is cut into parts, and they often say it differently from the code. The framework takes both: documents tell the agent what an area is, where its code lives and what was intended; the code tells it what actually happens, and stays the reference for the model. Where a document and the code disagree, or a document promises something the code does not do, the agent does not choose: it records a question in the area (`questions`), and the question stays on the readback page until a person answers it. No particular set or layout of documents is required. A repository of architecture notes that point to code repositories works, and so does a single README.

## Changes and git

**The area is the unit of meaning; the change is the unit of work.** A change is a git branch: `change/<name>` here, and the same branch name in every code repository it touches. Git tracks the change, the pull request review is the approval, and the committed ledger is the record of what was verified. There is no other bookkeeping, so several changes can be open at once, each on its own branch. When two changes touch the same area and their ledgers conflict, re-running the checker settles it, because the ledger is generated.

Git also answers "did anything change?":

- `lake exe check --since <revision>` compares with that revision and also writes `out/readback/change.html`: every area the change touches, and in each what the change added, removed or altered. A change that crosses areas is read there in one place, and it is all a reviewer of the change has to read.
- A code check is re-run only when git reports different content for what its verdict depends on: the area's own files, the checker, the adapter and the code's files. The ledger records the state git reported when the verdict was reached; if git reports the same state now, committed or not, the verdict stands. With `--since`, the ledger of that revision is the one that counts as recorded, which is what a pull request check uses: it trusts the base branch, not the ledger the change brings. `--full` re-runs everything.
- A binding names where its code lives: this repository, or another one by name, with the local checkout given in `.spec/local.json` (`{"repo_paths": {"name": "/path"}}`, not committed). For another repository the ledger records the code commit the verdict is about, and a verdict on uncommitted code is never taken as recorded.
- An area says which commit of each repository it was read from (`readAt`). When a file it cites has changed since, the checker asks, until someone reads the file again and moves the commit. This also covers areas that are not bound to code, and documents.

## What is trusted

- The Lean kernel.
- `Spec/Data.lean` and the record types in `Spec/Check.lean`: they fix what "reachable" and "proven" mean.
- `checker_main` in `Spec/Main.lean`: it rejects `sorry`, added axioms, `native_decide`, and constructs that let compiled code differ from the definitions the proofs are about. A project's `Main.lean` must stay the three fixed lines; a change to it, to the lakefile or to the CI workflow is a change to what counts as verified.
- For levels 2 and 3 only: the search in `Spec/Check.lean`, the Lean compiler, and 64-bit state fingerprints (two different states can, with very small probability, be taken for one). These are argued and tested, not proven.
- For the code verdict only: the adapter for the code's language, and the binding, which a person confirms on the readback page.

## Not built yet

- Adapters for languages other than Python and Kotlin. The protocol is a few JSON messages, described at the top of `adapters/python.py`.
- Help with the surroundings of real code: a binding's `setup` has to build the system with its database, network and neighbours under the test's control, and nothing assists with that yet.
- Stronger code verdicts: every action sequence to a depth, symbolic checking, proof that the code matches the model.
- A code verdict per requirement. Today there is one per area.
- Recording a person's acceptance of a claim that stays below its target.
- Keeping a level reached by a long run when a later run has a smaller budget.
- For the model: randomized runs (level 4), and level 1 proofs for requirements.
- Checking that the requirements determine the behaviour completely, beyond the questions listed above.

## Requirements

An AI coding agent, git, and Lean through [elan](https://github.com/leanprover/elan) (the agent sets it up with your consent; the version is pinned in `lean-toolchain`). The adapters need `python3`; Kotlin code also needs `kotlinc` and `java`.

The measurements that led to this design are in [docs/spike-results.md](docs/spike-results.md).
