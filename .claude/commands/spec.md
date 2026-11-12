# /spec — write or change the requirements of an area

Requirements here are Lean 4 objects checked by `lake exe check`. The framework is the Lake package `quintessence`; read its `README.md` first if you have not (in a project it is under `.lake/packages/quintessence/`). Its `example/` directory is a complete project: `example/Areas/Auth` is the pattern for an area written from scratch, `example/Areas/Reservation` for one extracted from existing code.

The person you work with decides what the system must do. You write the Lean, run the checker, and bring back what it found. Never decide a requirement on the person's behalf, and never weaken a claim to make the check pass.

## Starting a project (once)

If the repository has no `lakefile.toml` yet, create these files, then run `lake update`:

- `lean-toolchain`: the same content as the framework's.
- `lakefile.toml`:

  ```toml
  name = "<project>"
  defaultTargets = ["check"]

  [[require]]
  name = "quintessence"
  git = "https://github.com/AlesKracik/quintessence"
  rev = "<branch, tag or commit>"

  [[lean_lib]]
  name = "Areas"
  globs = ["Areas", "Areas.+"]

  [[lean_exe]]
  name = "check"
  root = "Main"
  ```

- `Main.lean`, exactly these three lines (never anything else: this file decides what counts as verified):

  ```lean
  import Spec.Main
  import Areas
  checker_main Areas.all
  ```

- `Areas.lean`: `def Areas.all : List Spec.Entry := []`, plus one import and one entry per area later.
- `.gitignore`: `.lake/`, `out/`, `.spec/local.json`.
- `.spec/project.json` (committed), what an agent needs to find its way around:

  ```json
  { "architecture": ["docs/architecture/", "platform-docs:services/billing.md"],
    "repos": { "billing": "git@example.com:org/billing.git", "platform-docs": "git@example.com:org/platform-docs.git" } }
  ```

  `architecture` lists the documents that describe the system: paths in this repository, or `<repo>:<path>` in a repository named under `repos`. `repos` names every repository the project's code or documents live in.
- `.spec/local.json` (not committed), where this developer has those repositories checked out: `{"repo_paths": {"billing": "/path/to/billing"}}`. Ask the person for the paths; do not clone on your own.
- CI: copy `.github/workflows/check.yml` from the framework and drop the `example` directory settings.
- Copy the framework's `.claude/commands/` into the repository.

## 0. The change

Work happens in a change, and a change is a git branch. If you are on the default branch, ask for a short name and create `change/<name>`; use the same branch name in every code repository the change touches. Commit as `spec(<name>): ...`. Several changes can be open at once, each on its own branch. Do not merge or close a change yourself: the pull request review is the approval.

## 1. Model and claims

Ask what the area does, then write or change:

- `Areas/<Name>/Model.lean`: the state, the actions, `init`, the step function (`none` means refused), and the invariant predicates. Import only `Spec.Data`.
- `Areas/<Name>.lean`: the `Area` record with journeys, invariants, requirements, examples, scope and views. Register it in `Areas.lean`.

Start from the journeys. Ask the person for the stories of the people who use this part of the system: who wants what, and what happens, moment by moment, including the moments where the system says no. Write each as a `Journey` (`.step` for a moment the system goes along with, `.refusal` for one it turns down), in the person's own words, with no identifiers or technical terms in `say`. The requirements then come out of the journeys: every moment needs a requirement that covers it, and the checker asks about every requirement that no journey shows. Keep a journey to a handful of moments; boundary cases belong in examples.

Rules for the claims:

- Every `text` is one plain sentence the person can confirm or reject.
- A requirement's `when` is the full condition under which the action is accepted (or, with `refused := true`, refused). Where `when` holds, the checker demands that the step succeeds and satisfies `shall`.
- Keep the scope small enough that the search finishes within the budget. Add a `bound` only for values that grow without limit, and say in a comment what it cuts off.
- Give one or two `views` so the review page can draw the lifecycle.

## 2. Check

Run `lake exe check` from the repository root.

- A violated claim or failed example comes with the shortest run that shows it (in `out/review.html`). Show the run to the person and ask whether the model or the claim is wrong. Then fix that one.
- A claim "below target" is not a failure, but say so.

## 3. Prove the invariants

For each invariant without a proof, write `Areas/<Name>/Proofs/<invariant>.lean` proving exactly

```lean
theorem <invariant>_holds : ∀ s, Reachable init apply s → <invariant> s = true
```

and attach it with `proof := some ⟨...⟩`. Use `Reachable.induct`. When the invariant is not preserved by a step on its own, prove a stronger predicate and weaken it at the end. Put each proof file in its own namespace, as `Areas/Auth/Proofs` does.

`sorry`, `native_decide`, new axioms, `implemented_by`, `extern`, `unsafe` and `debug.skipKernelTC` are rejected by the build. If you cannot prove an invariant, leave the proof out and report it. If you find it is false, bring the counterexample to the person.

Proof writing and proof repair after a model change are mechanical: delegate them to a cheaper model when you can. The kernel checks the result.

## 4. Open questions

The checker lists what the requirements do not say. Go through the list with the person, one question at a time, showing the situation from the review page. Each answer becomes a requirement, a refusal requirement, an example, or an entry in `atomic` (the decision on whether an action can interleave with others inside the implementation, with the reason). Re-run the check after each answer. Leave a question open if the person has not answered it.

## 5. Existing code

When the area describes code that already exists, the model is extracted from the code:

0. Orient yourself before reading code. Read the architecture documents listed in `.spec/project.json` and follow them to the repositories they point to (`repos`, with the local checkouts in `.spec/local.json`). Use them to decide what one area is (one component with its own state and operations, not a whole system), where its entry points are, and which repository and paths its code lives in. Tell the person which area you will extract and from where, and what the documents say that the code may or may not confirm. The documents orient; the code is the reference. Where they disagree, that is a question for the person.
1. Read the code. Write the model so that it does what the code does, including what looks like a quirk. Do not tidy the behaviour up; a quirk is a question for the person, not something to fix in the model.
2. Write the requirements as plain statements of what the code does, so the person can confirm or reject each one.
3. Bind the area to the code with `code := some { ... }`:
   - `repo` and `paths` say where the code lives in git (`repo` is `"."` for this repository, otherwise a name that `.spec/local.json` maps to a checkout under `repo_paths`);
   - `setup` builds a fresh system in a variable named `system`; put the clock and other surroundings under the test's control there;
   - `actions` gives, for every action name, the code to run (`{0}`, `{1}` are the action's arguments). The action counts as refused when that code raises. Call `refuse()` to turn a return value into a refusal;
   - `observe` lists what is compared after every step: a name, the value the model expects as text (`true`, `3`, `none`, plain text), and the expression that reads it from the code. Observe everything a caller can see.
   The mapping must stay a table of short calls. If it needs logic to make the code fit the model, the model is wrong.
4. Run `lake exe check` until the code agrees. Each difference comes with the run that shows it. During extraction the code is the reference, so fix the model.
5. "The code has operation X that the model does not use" means the model is missing something. Add it, or leave the question for the person.

## 6. Next

Commit the area together with `ledger/<area>.json`, then suggest the next step: `/spec-readback` to put the change in front of the person, `/spec-code-generate` when the area has no code yet, `/spec-code-verify` when it has.
