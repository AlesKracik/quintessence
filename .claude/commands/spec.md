# /spec — write or change the requirements of an area

Requirements here are Lean 4 objects checked by `lake exe check`. The framework is the Lake package `quintessence`; read its `README.md` first if you have not (in a project it is under `.lake/packages/quintessence/`). Its `example/` directory is a complete project: `example/Areas/Auth` is the pattern for an area written from scratch, `example/Areas/Reservation` for one extracted from existing code.

The person you work with decides what the system must do. You write the Lean, run the checker, and bring back what it found. Never decide a requirement on the person's behalf, and never weaken a claim to make the check pass.

## Starting a project (once)

If the directory has no `lakefile.toml` yet, set the project up first: follow `SETUP.md` of the framework (in a project that already has the package it is at `.lake/packages/quintessence/SETUP.md`).

## 0. The change

Work happens in a change, and a change is a git branch. If you are on the default branch, ask for a short name and create `change/<name>` from it; use the same branch name in every code repository the change touches. Commit as `spec(<name>): ...`. Several changes can be open at once, each on its own branch. Do not merge or close a change yourself: the pull request review is the approval.

## 1. Model and claims

An area is what the person calls one: a part of the system with its own purpose ("protection", "inventory"). When an area is too large for one state machine, give it parts, each its own model and `Area` record, named `<area>/<part>` (`protection/configuration`, `protection/runs`); the readback shows the parts together on the area's page. What one part treats as given, another part decides: say so in its `assumptions`.

Ask what the area does, then write or change:

- `Areas/<Name>/Model.lean`: the state, the actions, `init`, the step function (`none` means refused), and the invariant predicates. Import only `Spec.Data`.
- `Areas/<Name>.lean`: the `Area` record with journeys, walkthroughs, invariants, requirements, scope, assumptions and views. It imports `Spec.Check`, `Areas.<Name>.Model`, and every proof file. Register it in `Areas.lean` (one import, one entry in the list).

Use what is already written. Before asking, read the documents listed in `.spec/project.json` (`documents`) and anything the person points you to: product notes, design documents, READMEs, tickets. They are sources for journeys and requirements, not the truth: say what you took from where, and let the person confirm it. No particular documents or structure are required; when there are none, everything comes from the person.

Start from the journeys. A journey is one business case of the area ("enrolling VMs", "a protection run"), told as its usual course: what the person or the business wants, and the steps that get there when nothing goes wrong. Ask the person which business cases the area serves, and keep them apart: a story that puts VMs into a group and then protects them is two journeys. A journey is generic. It speaks of "a VM", "a customer", "a run", never of a particular one, and it carries no numbers (how many VMs, which run) except a threshold that a requirement states ("the fifth wrong password locks the account"). Enrolling is one step, "the operator puts a VM into the group", however many VMs there are. Write each as a `Journey` with `steps`, each `.step "<what happens, in general words>" [<concrete actions>]`. The concrete actions are only the evidence that the model takes the step; the readback does not show them or any value they produce. Give two when the step can repeat, and the readback draws a loop on it. When a journey does not start from nothing, say where it starts in `given`, in the same general words ("the group has VMs"), and give a run that gets there in `setup`. Do not write refusals into a journey. The readback draws the journey as a graph and hangs on every step each way the system can turn it down, and it takes those from the refusal requirements that speak about the step's action, so the negative decisions cannot be left out or invented. No identifiers or technical terms in `say`.

Then the walkthroughs. A `Walkthrough` is one specific run worth knowing, told moment by moment in the person's own words: a failure and what follows it, a boundary, something that arrives twice or never. Use `.step` for a moment the system goes along with, `.refusal` for one it turns down, and `.outcome` when what is true afterwards is the point: it carries that as a check on the state. Name the journey it is a case of in `journey`. Every way a journey's step is turned down must happen in at least one walkthrough: the journey says that the system can say no there, the walkthrough shows it saying no. The checker asks about each one that no walkthrough shows. Keep a walkthrough to a handful of moments. There are no separate examples: a concrete case with an expected result is a walkthrough that ends in an `.outcome` or a `.refusal`.

The requirements come out of both: every step and every moment needs a requirement that covers it, and the checker asks about every requirement that shows in no journey and no walkthrough.

Rules for the claims:

- Every `text` is one plain sentence the person can confirm or reject.
- When the person has not said what happens in some situation, do not decide it. Let the model refuse the action there and write no requirement for it: the checker then reports "refused in a situation no requirement covers" with an example run, and that open question is the record. Never write a requirement to silence a question the person has not answered.
- A claim you add on your own, such as an invariant you found worth stating, is a proposal: say so in its `sources` ("proposed by the agent, not yet confirmed") and tell the person.
- Give every requirement, invariant, journey and walkthrough its `sources`: where it comes from, as a history with the oldest origin first. One entry per origin: the person (and when), a document and where in it, a place in the code. When you change an existing claim, keep the entries it has and add the origin of the change, so a requirement extracted from code and later changed by a person shows both. The reviewer sees the list next to the claim. Do not invent an origin; leave the list empty when you do not know, and ask.
- A requirement's `when` is the full condition under which the action is accepted (or, with `refused := true`, refused). Where `when` holds, the checker demands that the step succeeds and satisfies `shall`.
- Keep the scope small enough that the search finishes within the budget. Add a `bound` only for values that grow without limit.
- Say what the model assumes, in `assumptions`: one plain sentence for every bound of the scope and what it cuts off ("the group takes 2 VMs here; the real limit is 100"), for everything that stands in for the outside world, for every part of the behaviour left out, and for what you took from a document without checking it in the code. The person reads these on the readback next to the journeys; a comment in the Lean reaches nobody.
- Give `views` for everything a journey or walkthrough talks about, named and valued in the person's words ("seat s1" is "held by alice"), one per thing in the scope. The readback tells the walkthroughs in them, moment by moment; a moment that changes no view shows the reader nothing.
- Give `lifecycles` for the principal state machines of the area: one per kind of thing that has a life of its own (a protection group, a run, a copy of a recovery point at a site, a session), not one per instance and not one per view. Each is a name and a function that gives the state, in the person's words, of every possible instance of that kind: always the same number of them in the same order, with a word such as "none" where there is no instance yet. Keep the states few and free of particulars: no names, no counters, no lists. The readback draws one diagram per kind over all instances together.

## 2. Check

Run `lake exe check` from the repository root.

- A violated claim comes with the shortest run that shows it (on the area's readback page, `out/readback/<area>.html`). Show the run to the person and ask whether the model or the claim is wrong. Then fix that one.
- A claim "below target" is not a failure, but say so.

## 3. Prove the invariants

For each invariant without a proof, write `Areas/<Name>/Proofs/<invariant>.lean` proving exactly

```lean
theorem <invariant>_holds : ∀ s, Reachable init apply s → <invariant> s = true
```

and attach it with `proof := some ⟨...⟩`. Use `Reachable.induct`. When the invariant is not preserved by a step on its own, prove a stronger predicate and weaken it at the end. Put each proof file in its own namespace, as `Areas/Auth/Proofs` does.

`sorry`, `native_decide`, new axioms, `implemented_by`, `extern`, `unsafe` and `debug.skipKernelTC` are rejected by the build. If you cannot prove an invariant, leave the proof out and report it. If you find it is false, bring the counterexample to the person.

There is no Mathlib: no `tauto`, `linarith`, `by_contra` and the like; use `simp`, `omega`, `cases`, `by_cases`, `Classical.byContradiction`. The data library (`Spec/Data.lean` in the framework) has the lemmas about maps you will need most, `FMap.get?_set`, `FMap.get_set`, `FMap.get?_of_mem_keys`, `FMap.get_of_get?`, and the proofs under `example/Areas/*/Proofs/` in the framework show the usual shape.

Proof writing and proof repair after a model change are mechanical: delegate them to a cheaper model when you can. The kernel checks the result.

## 4. Open questions

The checker lists what the requirements do not say. Go through the list with the person, one question at a time, showing the situation from the readback page. Each answer becomes a requirement, a refusal requirement, a walkthrough, or an entry in `atomic` (the decision on whether an action can interleave with others inside the implementation, with the reason). Re-run the check after each answer. Leave a question open if the person has not answered it.

## 5. Existing code

When the area describes code that already exists, the model is extracted from the code:

0. Orient yourself before reading code. Read what is written about the system: the documents listed in `.spec/project.json` (`documents`), and what lies around the code itself, such as READMEs, design notes, comments and tests. Follow them to the repositories they point to (`repos`, with the local checkouts in `.spec/local.json`). None of this is required to exist or to have a particular shape; use what there is. Use it to decide what one area is (one component with its own state and operations, not a whole system), where its entry points are, which repository and paths its code lives in, and what the behaviour was meant to be. Tell the person which area you will extract and from where.
   The documents give orientation and intent; the code is the reference for the model. Documents age: when one says which revision of the code it was written against, compare that with the checkout, and read what changed in between before trusting its details. Do not settle a disagreement yourself. When a document says something the code does not do, or the code does something no document mentions and it looks unintended, add it to the area's `questions` as one plain sentence naming both sources ("docs/holds.md says a hold lasts 15 minutes; the code releases it after 2"). It stays on the readback page until the person answers; then remove it and change the model, the code, or neither.
   The extent of the area is the person's. When they have named it ("protection"), model all of it; do not narrow it to the part that is easiest to model. If you must leave something for later, it goes into the list below, where the person sees it.
1. List the surface before you model. Go through the code and write down every way into this area: every endpoint, every message or event handler, every scheduled job that serves it. Put each in the area's `surface`: `.modelled "<what it is for, in plain words>" "<where in the code>" ["<action>", ...]` when actions of the model stand for it, `.readOnly "<what>" "<where>"` when it only reads, `.leftOut "<what>" "<where>" "<why>"` when it belongs to this area and the model does not cover it. Make the list from the code, not from the documents and not from what you chose to model. It is how the person sees which share of the area's code the model speaks about: the readback shows it with the counts, and every left-out entry is an open question until the person accepts it.
   The measure is the area, not the repository. One repository or service often hosts several areas; code that serves another area (its endpoints, its handlers), test-only endpoints and technical start-up code are not this area's surface and are not listed, not even as left out. When you cannot tell whether something belongs to this area, do not list it and do not drop it: name it in one question to the person ("the code also does X and Y; are they part of this area?"). When the model has an action for something another area decides, because this area depends on it, say so with `.outside "<what>" ["<action>"]`.
2. Read the code. Write the model so that it does what the code does, including what looks like a quirk. Do not tidy the behaviour up; a quirk is a question for the person, not something to fix in the model.
3. Write the requirements as plain statements of what the code does, so the person can confirm or reject each one.
4. Bind the area to the code with `code := some { ... }`. Pick the adapter for the code's language (`adapters/python.py`, `adapters/kotlin.py`) and read its header: it says exactly what `setup`, `actions` and `observe` mean in that language. In general:
   - `repo` and `paths` say where the code lives in git (`repo` is `"."` for this repository, otherwise a name that `.spec/local.json` maps to a checkout under `repo_paths`);
   - `setup` builds a fresh system in a variable named `system`; put the clock and other surroundings under the test's control there;
   - `actions` gives, for every action name, the code to run (`{0}`, `{1}` are the action's arguments). The action counts as refused when that code raises. Call `refuse()` to turn a return value into a refusal;
   - `observe` lists what is compared after every step: a name, the value the model expects as text (`true`, `3`, `none`, plain text), and the expression that reads it from the code. Observe everything a caller can see.
   The mapping must stay a table of short calls. If it needs logic to make the code fit the model, the model is wrong.
5. Run `lake exe check` until the code agrees. Each difference comes with the run that shows it. During extraction the code is the reference, so fix the model.
6. "The code has operation X that the model does not use" means the model is missing something. Add it, or leave the question for the person.

## 6. Next

Commit the area together with `ledger/<area>.json`, then suggest the next step: `/spec-readback` to put the change in front of the person, `/spec-code-generate` when the area has no code yet, `/spec-code-verify` when it has.
