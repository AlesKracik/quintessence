# /spec — write, check and complete the requirements of an area

You are working in a repository where requirements are Lean 4 objects checked by `lake exe spec`. Read `README.md` first if you have not. `Areas/Auth` is the pattern to follow.

The person you work with decides what the system must do. You write the Lean, run the checker, and bring back what it found. Never decide a requirement on the person's behalf, and never weaken a claim to make the check pass.

## 1. Model and claims

Ask what the area does, then write or change:

- `Areas/<Name>/Model.lean`: the state, the actions, `init`, the step function (`none` means refused), and the invariant predicates. Import only `Spec.Data`.
- `Areas/<Name>.lean`: the `Area` record with invariants, requirements, examples, scope and views. Register it in `Areas.lean`.

Rules for the claims:

- Every `text` is one plain sentence the person can confirm or reject.
- A requirement's `when` is the full condition under which the action is accepted (or, with `refused := true`, refused). Where `when` holds, the checker demands that the step succeeds and satisfies `shall`.
- Keep the scope small enough that the search finishes within the budget. Add a `bound` only for values that grow without limit, and say in a comment what it cuts off.
- Give one or two `views` so the review page can draw the lifecycle.

## 2. Check

Run `lake exe spec` from the repository root.

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

When the area describes code that already exists, the model is extracted from the code, and the checker then compares the two on every run of `lake exe spec`.

Extraction:

1. Read the code. Write the model so that it does what the code does, including what looks like a quirk. Do not tidy the behaviour up; a quirk is a question for the person, not something to fix in the model.
2. Write the requirements as plain statements of what the code does, so the person can confirm or reject each one.
3. Bind the area to the code with `code := some { ... }` (see `Areas/Reservation.lean`):
   - `setup` builds a fresh system in a variable named `system`; put the clock and other surroundings under the test's control there;
   - `actions` gives, for every action name, the code to run (`{0}`, `{1}` are the action's arguments). The action counts as refused when that code raises. Call `refuse()` to turn a return value into a refusal;
   - `observe` lists what is compared after every step: a name, the value the model expects as text (`true`, `3`, `none`, plain text), and the expression that reads it from the code. Observe everything a caller can see.
   The mapping must stay a table of short calls. If it needs logic to make the code fit the model, the model is wrong.
4. Run `lake exe spec` until the code agrees. Each difference comes with the run that shows it. During extraction the code is the reference, so fix the model.
5. "The code has operation X that the model does not use" means the model is missing something. Add it, or leave the question for the person.

Drift, later: when the check reports a difference on an area that used to agree, show the run to the person. There are three outcomes and the person picks one: the code is wrong (fix the code), the model is wrong (fix the model and its claims), or the change is deliberate (change the requirement first, then both).

## 6. Hand over

Tell the person to open `out/review.html`. Commit the area together with `ledger/<area>.json`.
