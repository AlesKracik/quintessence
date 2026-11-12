# /spec-code-generate — write the code for an area

Use this when an area has no code yet, or when a change to its requirements has to be carried into the code. For code that already exists and has no model, use `/spec` (existing code) instead.

Before you start: `lake exe check` must show no violated claim and no failed example for the area, and the person must have seen the requirements (`/spec-readback`). Ask the person where the code goes (repository and path) and in which language, unless the project already answers that.

Rules:

- **Do not invent behaviour.** The code does what the model's step function does, for every action, accepted and refused. Where the model says nothing, stop and bring the question to the person; do not fill the gap in code.
- Write the code a maintainer would write: its own structure and names, not a transliteration of the Lean. It must be a full implementation, not a wrapper around the model.
- Name the requirement each function serves in one comment (`# REQ-003`).
- Work on the change's branch in the code repository (same branch name as the spec change). Commit the code on its own, as `code(<name>): ...`; do not mix code and requirement changes in one commit.

Then bind the area to the code with `code := some { ... }` in `Areas/<Name>.lean`: `repo`, `paths`, `setup`, one entry in `actions` per action name, and `observe` for everything a caller can see (see `/spec`, "Existing code", for the fields). The binding is generated from the model: every action is mapped and every observable state is read. It must stay a table of short calls.

Finish with `/spec-code-verify`. The code is not done until the check says it agrees.
