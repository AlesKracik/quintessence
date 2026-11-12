# /spec-code-verify — check the code against the model

Run `lake exe check` from the repository root. For every area bound to code it drives the code through the model and compares after every step. Git decides whether that is needed: the ledger records the state git reported for the area's files, the checker, the adapter and the code when agreement was reached, and while git reports the same state the agreement stands and the check is not re-run. `--full` re-runs it regardless.

Read the `code` part of each summary line:

- **agrees**: commit `ledger/<area>.json`. That commit is the record that this code and this model agreed. For code in another repository the ledger names the code commit; commit the code there first, because a verdict on uncommitted code is not a record.
- **agrees, within N steps**: the budget ran out before the scope did. Say so, and raise `budget` in the binding if the person wants more.
- **drift**: show the person the run (on the area's readback page, `out/readback/<area>.html`, section "Code"). Exactly one of three things happened, and the person decides which:
  1. the code is wrong: fix the code;
  2. the model is wrong: fix the model and its claims through `/spec`;
  3. the change is deliberate: change the requirement first through `/spec`, then the code.
- **error**: the check could not run (adapter, setup, missing checkout). Fix that; it is not a verdict.
- "The code has operation X that the model does not use" is an open question for the person.

Never edit a ledger by hand, and never change the binding to hide a difference.
