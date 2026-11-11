# Goal

Every statement here has been confirmed; no assumptions or open questions remain. Section 4 records the decisions that change which option wins.

## 1. Problem

AI agents are flexible, which is their strength for building software and their weakness for critical systems: a critical system must behave correctly every time, and an agent's output cannot be trusted on its own. Separately, requirements are usually incomplete, and incomplete requirements are the most common root cause of wrong systems, whoever writes the code.

## 2. Goal in one sentence

A general framework in which requirements are mathematical objects that are driven toward completeness by iteration with a human, proven consistent by machine, and used to deterministically check code — newly generated or already existing, in any language — so that correctness never rests on trusting the agent, and every claim carries the strongest assurance that is feasible within a declared budget, stated as what it is.

## 3. Required properties

Each is meant to be testable: an option either has it or does not.

1. **Provable requirements.** Every requirement has a formal statement with a machine-checked verdict. A verdict is never written or asserted by an agent.
2. **Untrusted agent.** Agents may write anything (models, proofs, code, lemmas). Whatever they write is accepted only through a checker. An agent error can cost time, never correctness.
3. **Completeness as a loop.** The system actively finds what the requirements leave undecided or do not mention, turns each finding into a concrete question a person can answer quickly, records the answer as a durable artifact, and re-measures. Completeness is stated relative to a declared scope, and the measure is visible per change.
4. **Self-verification of the requirements.** As a side effect of 1: conflicts between requirements, unreachable states, dead behavior and vacuous claims are detected.
5. **Deterministic code check.** Given the requirements and the code, the check of code against requirements gives the same answer every time, with no agent judgment in the verdict.
6. **Feasible verification with a sane downgrade.** Some unbounded proofs, and some bounded ones, cannot be completed in reasonable time, or at all. The system must stay useful when that happens:
   - **Target and budget.** Each claim has a target assurance level set by its criticality, and each check runs within a declared budget.
   - **Fixed ladder.** A check that cannot reach its target within budget falls to the next level of a fixed ladder instead of failing or hanging. There are two ladders, one for the model and one for the code, and every rule in this property applies to both. The model ladder (how well a requirement is shown to hold in the model), strongest first:
     1. proven for all sizes and all steps;
     2. all steps, within a declared finite scope (for example up to 3 sites, 4 groups);
     3. no counterexample within N steps;
     4. randomized runs, with the count and coverage stated;
     5. not verified.
   - **Code ladder** (how well the code is shown to match the model), strongest first:
     1. full proof that the code matches the model (needs a formal semantics of that language);
     2. every path through the code checked up to a bound by a symbolic checker (per-language tool);
     3. every event sequence up to depth k in a declared scope, run against the code (any language);
     4. model-driven random runs, with the count and coverage stated (any language);
     5. sampled traces replayed against the code (any language; the minimum in decision 2);
     6. not checked.
   - **Two verdicts per requirement.** Each requirement carries a model verdict and a code verdict. Its overall assurance is the weaker of the two, and the review surface shows both.
   - **Climb from cheap.** The ladder describes the result, not the order of work. A check starts with the cheap levels for fast feedback and climbs while the budget lasts; the recorded level is the highest one reached.
   - **Never silent.** The achieved level, the target, the budget spent and the reason for the gap are recorded on the verdict and shown to the reviewer. A lower level is never rendered as a higher one.
   - **The claim does not bend.** A downgrade changes the evidence, never the statement. Constants, domains and requirement text are not altered to fit the checker; any reduced scope is a parameter of the check, recorded on the verdict. Restating a claim so that it becomes provable is a requirement change and goes through review.
   - **Critical claims below target need a person.** A critical claim that lands below its target blocks approval until a person accepts the risk with a reason. The acceptance expires when the claim or what it depends on changes (decision 11). Where possible it is backed by a compensating control before production, such as extra generated tests aimed at that claim. The framework attempts this and reports whether one exists, so the person accepting sees it; its absence does not block the acceptance.
   - **Before deployment is what counts.** Validation happens before code reaches production. Finding a violation in production is too late to count as assurance; checking executions in production is a nice-to-have a project may add, never a substitute for a level on the ladder (decision 2).
   - **Upgradable.** A downgraded claim stays queued and can be retried later with a larger budget or a stronger method; its level can rise without the statement changing.
   - **Reproducible.** The same inputs and the same budget give the same level. Budgets are counted in deterministic units (solver steps, states explored) where the tool allows, not only wall-clock time.
     - A person sets and sees budgets in time (minutes, hours); the recorded level is decided by work units where the tool has them; a wall-clock cutoff remains as a safety net against a runaway check.
     - A level once reached stays valid however long it took: a proof found on a slow machine is still a proof. Only "did not finish" results depend on the machine.
     - Where a tool offers only a timeout, a downgrade caused by it is labelled as cut off by time, so the reviewer knows a retry might climb.
   - **Fast enough to iterate.** The completeness loop (property 3) needs feedback in minutes, typically (decision 11); only changed claims are re-checked, and deep checks run separately from the interactive loop.
7. **Human-friendly review.** Internals (models, proofs, generated artifacts) do not need to be human-readable. What a person approves must be: small, in plain language or concrete scenarios, and limited to what actually changed.
   - **Visual first.** As much of the information as possible is presented visually, because walls of text are extremely hard for a person to grasp. State machines, flows, scenario timelines, coverage grids, and before/after comparisons are shown as diagrams or tables; prose is used only for what a picture cannot carry.
   - **Generated, not drawn.** Every visual is generated from the formal source, never drawn by hand or by an agent's judgment, so it cannot disagree with what is proven.
   - **Interactive.** Where it helps, the visuals are interactive: a person can step through a scenario or explore the state machine, not only read a static document.
8. **No framework constraint.** Existing frameworks, including those used in this repo, are optional. Building a new one is acceptable if it fits better.
9. **Brownfield and drift.** The system works on existing code that was not written from a model, not only on code it generated.
   - **Extraction.** A model and requirements can be derived from existing code, with each derived item traceable to the code it came from and presented to a person for confirmation.
   - **Drift detection.** Any divergence between code and model is detected, in both directions: code that does something the model does not say, and a model that says something the code does not do. This holds as either side keeps changing.
   - **Resolution.** Each drift is resolved one of three ways, and the choice is recorded: the code is wrong, the model is wrong, or the change was deliberate and the requirements are updated.

## 4. Decisions that shape the solution

1. **Proof strength.** The target for critical invariants is unbounded proof (any number of users, any number of steps); the target for non-critical claims is bounded or small-scope evidence. Targets are not guarantees: property 6 says what happens when one is out of reach.
2. **Strength of the code guarantee.** The implementation is a full, independent implementation, not a thin wrapper around the mathematical model.
   - **Minimum:** sampled traces and tests replayed against the code.
   - **Ideal:** full proof about the production code.
   - **Not needed:** checking every execution in production at run time.
   - **Not the approach:** shipping the proven model itself as the decision logic.
   - Between minimum and ideal, the code check follows the code ladder and the downgrade rules in property 6: strongest feasible level per claim, recorded and shown.
3. **Target systems.** Any system. The core abstraction is the state machine, which covers services, workflows and UI navigation alike. Two known weak spots:
   - **Data-heavy logic.** Logic that is mainly computation over data (a pricing formula, a parser) is supported, but with function-level methods and typically weaker evidence than state-machine behavior, stated as such on the verdict.
   - **Concurrency inside the implementation.** The model treats each event as one atomic step; real code handling one event can interleave with other requests, and sequence-based code checks do not exercise that. At minimum the framework has a stub for this: for each event that could run concurrently in the implementation, it asks the person whether to follow up at this level of detail.
     - **Yes:** a follow-up is recorded for that event (a finer-grained model, interleaving tests or code proof). The stub only records and tracks the follow-up; the mechanisms that carry it out can come later.
     - **No:** "this event is handled atomically" is recorded as a declared assumption with the person's reason, and shown wherever a claim depends on it.
     - **Not yet answered:** it stays an open question. Atomicity is never taken for granted silently.
4. **Languages and code origin.** The core is language-agnostic: any implementation language, both existing and newly generated code (property 9).
   - Consequence: everything language-specific sits behind a narrow, defined interface, so adding a language does not touch the core.
   - Consequence: full proof about production code (the ideal in decision 2) needs a formal semantics of that language, so it will be reachable for some languages before others; where it is not, the code check lands on a lower level of the ladder.
5. **Human effort in proofs.** Zero: if an agent cannot discharge a proof within budget, the claim downgrades along the ladder in property 6; a person is never asked to write a proof, only to accept or reject the resulting level for critical claims.
6. **Reviewer.** An engineer or product owner who knows the domain but not formal methods, with minutes per change, not hours.
7. **Completeness exit criterion.** "Complete relative to scope S": every in-scope event has a decided outcome in every state, requirements determine every in-scope observable except where latitude is declared, and no critical question is open. Never an absolute claim.
8. **What may be trusted.**
   - **Root of trust (trusted with nothing behind it):** a small proof kernel, the human-approved requirement statements, and declared environment assumptions. The kernel must stay small because nothing checks it.
   - **Common platform components (trust is derived, not assumed):** large components such as translators, checkers, test-harness generators and the adapters that connect the model to code. An adapter is built once per implementation language and shared by every project in that language; there can be several adapters, and each is part of the platform, not of a project. They may be big and custom, provided they are one shared solution for all projects and are "mathematically bounded": each has a defined semantics and its own soundness argument or verification, so what it can get wrong is known. How each earns its trust:
     1. **Preferred:** it is proven correct once, in the kernel.
     2. **Acceptable starting point:** it has a written soundness argument plus tests, not machine-checked. This kind enlarges what must be trusted, so each such component is listed in a published trust register and is a candidate to be proven later.
   - **Per-project mapping (data, not logic):** what tells an adapter which model event is which function or endpoint in a given project. It is always generated or confirmed, never hand-written logic:
     - for new code, the platform generates the interface from the model and the code implements it, so the mapping holds by construction;
     - for existing code, the mapping comes out of extraction, where each derived item is traceable to its code and confirmed by a person (property 9).
   - **Not trusted:** anything written ad hoc per project or per request, by an agent or a person, such as project-specific glue code and one-off test harnesses. A verdict must not rest on these.
9. **Liveness and non-functional requirements.** For now: safety first. "Eventually" properties are restated as bounded response where possible; performance and other non-functional requirements are recorded with a fit criterion but not proven.
10. **Budget and ownership.** A general framework for wide use, not a one-team tool. A large build budget can be spent if the decision is made, including on the common platform components in decision 8.
11. **Verification budgets and risk acceptance.** The whole process is iterative: every change re-measures, and nothing is accepted once and for all. An acceptance of a critical claim below its target may expire when things change.
   - It expires when the claim's statement, or the part of the model or code the claim depends on, changes; unrelated changes do not expire it.
   - **Typical waits:** around minutes in the interactive loop, around tens of hours for a deep run. These are typical values, not hard limits: a given run may take less or more. Budgets are therefore configurable per project and per run, with these as defaults. The budget used is recorded on each verdict. A budget change alone never invalidates a verdict already reached: a claim proven under a large budget stays proven when a later run uses a smaller one.
   - Who may accept a critical claim that landed below its target is set by an organisational policy outside this framework (section 5). The framework only requires that an acceptance by a person exists, and records it.

## 5. Out of scope

- Proving that a requirement matches what the human actually meant. This can only be narrowed (by questions and concrete scenarios), not proven.
- The policy for who may accept a critical claim that landed below its target. A policy will exist, but defining it is not what this framework solves.
- Verifying third-party systems; they appear only as declared outcomes and assumptions.
  - What is declared about an external system (the answers it can give, the assumptions made about it) is itself subject to the completeness loop in property 3: the framework asks whether it can answer late, twice, out of order or partially, and compares observed responses against the declared list.
  - A system that has its own model in the framework is not third-party: claims that span it and another area are checked jointly.
