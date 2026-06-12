# Quintessence

*No hearsay requirements — every claim produces a witness.*

A template repo for starting a software project with a **formal-requirements-first** workflow: distill vague natural language to its quintessence — EARS-structured requirements, a Quint formal model, machine-found witness traces, and code verified against them.

You don't "use" this repo directly — you **clone it**, **bootstrap it**, and the clone becomes the root of your new project.

What you get:

- A methodology for moving rough requirements → **EARS-structured requirements** → formal Quint specification → Apalache-verified invariants **+ machine-found witness traces per requirement** → generated code **+ conformance replay** (the model's own traces re-run against the implementation) → drift-detection over time, with every requirement traceable to code and tests.
- **Five Claude Code commands**: `/spec`, `/spec-check`, `/spec-verify`, `/spec-apply`, `/spec-readback`. The first is an adaptive entry point that walks all spec-authoring phases conversationally; the others run Apalache + witness probes, replay traces and run tests, generate code + the conformance adapter, and produce a human-readable Markdown review document with embedded Mermaid diagrams (including witness traces rendered as sequence diagrams).
- **One JSON file per area** at `specs/<name>.json`, with a Quint sidecar `specs/<name>.qnt`. Two kinds: `area` (functional; declare `screens[]` + `navigation[]` to model an interactive surface formally) and `contract` (cross-area invariants). Requirements group into **journeys** (`.spec/journeys/` — use cases with steps in temporal order, single- or cross-area) — readbacks render flows as a human walks them, not ID-sorted lists.
- **Changes as the unit of work**: a manifest per change at `.spec/changes/<slug>.json` records which areas/contracts a piece of work touches (membership + IDs only — check/apply/verify status is derived from the area JSONs, so it can't go stale). Bare commands operate on the whole change — `/spec auth` once, then `/spec-check`, `/spec-apply`, `/spec-verify`, `/spec-readback` need no target. Areas stay the logical boundary; the manifest holds only references.
- **Anti-vacuity guarantees**: a requirement counts as demonstrated only when the checker produces a witness trace for it; a spec whose invariants pass over an unreachable state space is caught, not celebrated.
- Seven optional architecture layers (0–6: stack, components, patterns, ADRs, topology, protocols, readbacks) and a multi-repo story via config-driven paths (no submodules).
- Python tooling: `spec-lint` (consistency + EARS + precision lints + witness obligations), `spec-record` (deterministic check **and** verify runner — model checking, conformance replay, drift, and every ledger write-back are mechanical; the AI never hand-edits a verdict), `spec-readback` (deterministic readback generator — the review document is rendered by code, byte-identical for identical input, so its git diff IS the review), `spec-matrix` (state×event completeness with a `--strict` CI gate), `quint_ir` (typed view of `.qnt` files), `itf_tools` (witness-trace validate/summarize/Mermaid), and a ready-made CI workflow.

Read [METHODOLOGY.md](METHODOLOGY.md) for the full picture. It travels with every project created from this template.

---

## Quick Start

```bash
# 1. Clone (or "Use this template" on GitHub)
git clone <this-template-url> my-project
cd my-project

# 2. Bootstrap — strips template-only files
./tools/bootstrap.sh

# 3. (Optional) Confirm your tooling — Java 17+, Quint, Apalache
./tools/check-tooling.sh

# 4. Open in Claude Code and run the adaptive setup
claude
> /spec
```

`tools/check-tooling.sh` is the **fastest way to find out what's missing**. It checks `java -version` (must be 17+ for Apalache), confirms `quint` and `apalache-mc` are on PATH, and prints platform-specific install commands for whatever it doesn't find — `brew install --cask temurin` on macOS, `apt install openjdk-17-jdk` on Ubuntu, the full GitHub-release recipe for Apalache, etc. Run it before `/spec-check` if you're not sure your machine is ready.

`/spec` detects there's no `.spec/project.json` yet and walks the project bootstrap (areas, repos, architecture defaults). Then `/spec <area>` walks each area conversationally — eliciting requirements, identifying entities, formalizing Quint, all in one chat.

For an **existing codebase**, use `./tools/bootstrap.sh --in-place` (keeps your README, `.git`, and existing files untouched). Then `/spec <area>` recognizes the brownfield case and walks extraction.

---

## Example Flows

Six end-to-end scenarios showing what the methodology feels like in practice. All use the same five commands — what differs is what `/spec` detects from project state.

### 1. Greenfield — starting a new project

After `./tools/bootstrap.sh` (fresh mode):

```
> /spec
Spec: No .spec/project.json — let me set up the project.
  Project name? > orderly
  Repo layout? > single-repo
  Areas? > orders, inventory, notifications
  Language? > TypeScript 5.4 / Express / Vitest
  Persistence? > postgres via Prisma
  ...
✓ Wrote .spec/project.json
✓ Scaffolded specs/{orders,inventory,notifications}.json (status: raw)

Next: /spec orders
```

```
> /spec orders
Spec: orders has no requirements yet. Walking elicitation.
  Purpose? > Customers place orders; pending → confirmed → shipped → delivered
  Entities? > Order (with those states), LineItem, Customer
  Happy path? > [walks; captures REQ-001..REQ-004]
  Invariants? > [INV-001 "no confirmed order has unreserved inventory", critical]
  ...
✓ Updated specs/orders.json (status: structured)

Ready to formalize. Draft a Quint module?
> yes

✓ Wrote specs/orders.qnt
✓ Updated specs/orders.json (status: formalized)

Next: /spec-check orders
```

```
> /spec-check orders
  ✓ INV-001 noUnreservedInventory  VERIFIED
  ✗ INV-002 noUnpaidShipped        COUNTEREXAMPLE
    ship_order missing payment guard. Suggested fix in specs/orders.qnt:
+   payments.get(o) == Paid,

Apply?  > yes
  ✓ All invariants VERIFIED

Witness obligations:
  ✓ REQ-001..REQ-004 WITNESSED → specs/orders/traces/*.itf.json
    (each requirement has a machine-found trace proving it reachable
     via its own action — so action coverage comes for free)

> /spec-readback orders   # generates the Markdown review doc
> /spec-apply orders      # generates types, store, service, tests
> /spec-verify orders     # 12 tests pass; spec ↔ code linked
```

End-state for one area: `specs/orders.json` + `specs/orders.qnt` + `specs/orders.readback.md` + actual code in `src/orders/` and `tests/orders/`. PR carries all of it together.

### 2. Brownfield — adding spec to existing code

`./tools/bootstrap.sh --in-place`, then `/spec billing` sees code at `src/billing/` with no spec and extracts a draft: inferred actions, entities with states, guards as candidate invariants — each presented for accept/edit/reject (`status: needs-validation`). `/spec-check` then typically finds a missing precondition in the inferred model; `/spec-verify` flags code behavior the spec doesn't mention yet. Iterate until clean. **Net effect**: existing code gains a formal model + machine-checked invariants, no rewrite.

### 3. Adding a feature to an existing area

`/spec orders` on an approved area asks "what's the change?", captures the new REQ/CON/INV items conversationally, updates the Quint, and the usual loop reruns: `/spec-check` → `/spec-apply` (diffs + new tests) → `/spec-verify` → `/spec-readback`. One PR carries spec diff + Quint diff + readback diff + code diff. No ceremony.

### 4. Drift detected, then codified

A teammate ships a hotfix at 2am loosening lockout from 5 to 7 attempts. CI runs `/spec-verify auth`:

```
> /spec-verify auth
✗ INV-002 noLockoutWithFewerThanMax  FAIL
  Code at authService.ts:78 uses >= 7; spec CON-001 says 5.
⚠ Drift detected: spec-traced code modified outside /spec-apply.

Two paths:
  1. Code is wrong — revert.
  2. Code is right — codify. Run /spec auth.
```

The hotfix was deliberate. Codify it:

```
> /spec auth
Spec: Last verify shows drift on CON-001.

DRIFT: CON-001 MAX_FAILED_ATTEMPTS
  Spec: 5  Code: 7 (since commit a1b2c3)

Options: 1) revert in code  2) codify  3) skip
> 2

Rationale? (becomes a DEC ADR)
> Helpdesk pushback after 2026-05-13 customer call. Bumped to 7.

✓ CON-001: 5 → 7
✓ DEC-005 (architecture): Lockout threshold raise. Alternatives considered: captcha after 3 (vendor uncertainty), exponential backoff (UX confusion).
✓ Updated specs/auth.qnt (const MAX_FAILED_ATTEMPTS = 7)
✓ Version bumped 1.4.0 → 1.5.0

> /spec-check auth   # the new spec still needs Apalache
  ✓ All VERIFIED

> /spec-verify auth
  ✓ PASS. No drift.
```

The hotfix is now legitimate: ADR captures *why*, the spec catches up, verification green. The methodology didn't prevent the drift (and shouldn't — that'd mean locking devs out) but caught it and made codification cheap.

### 5. Cross-area contract

A contract area (`kind: contract`, `spans: [auth, billing]`) holds joint invariants in a Quint module importing both areas — e.g. `noOrphanAccounts`: every `billing.Account.userId` exists in `auth.users`. From then on `/spec-check auth` cascades automatically: an auth change that breaks the joint invariant surfaces as a contract counterexample ("after `delete_user(alice)`, billing still has alice") — fix the area or evolve the contract. Cross-area discipline without atomic multi-area branches.

### 6. UI navigation modeled formally

You want to add the login UI and *prove* Dashboard can never be reached without authentication:

```
> /spec auth-ui
  Kind? > area — declaring screens + navigation makes it an interactive surface
  Spans which areas? > auth

  Screens? > Home, Login, Dashboard (auth-required), LockedNotice
  Navigation?
    Home → Login (click 'Sign in')
    Login → Dashboard (submit valid creds)
    Login → LockedNotice (account locked)
    Login → Login (invalid creds, error state)
    Dashboard → Home (logout)
    LockedNotice → Home (after lockout expires)
  Components? > LoginForm (email, password; states: idle/submitting/error), Header (always)
  Critical invariant? > Dashboard reachable only when authenticated

Drafting Quint — screens as a variant type, navigation as actions, auth as a guard...
```

```quint
module authUi {
  import auth.* from "./auth"
  type Screen = Home | Login | Dashboard | LockedNotice
  var current: Screen
  var authenticated: bool

  action submit_success = all {
    current == Login, not(authenticated),
    current' = Dashboard, authenticated' = true,
  }

  val guardedDashboard: bool = (current == Dashboard) implies authenticated
}
```

```
> /spec-check auth-ui
  ✓ guardedDashboard  VERIFIED — proven across all reachable states.
```

Machine-checked: no sequence of user actions reaches Dashboard without `authenticated == true`. Not unit tests, not a code-review checklist — a formal property.

```
> /spec-readback auth-ui   # writes specs/auth-ui.readback.md with navigation diagram inline
> /spec-apply auth-ui      # generates React components with router guards
> /spec-verify auth-ui     # all UI navigation tests pass
```

PR reviewers see the navigation graph render in GitHub and the auth-guard invariant flagged `Status: ✓ verified by Apalache`.

### What's notable across all six

- **Same five commands** for every scenario. You never have to remember which phase you're in.
- **No branch ceremony.** Each flow uses git differently — branches, direct-to-main, doesn't matter to the methodology.
- **One PR per change** carries spec + code together; reviewers see them as one diff.
- **The readback file is the PR's review surface** — humans look at `<area>.readback.md`, not raw JSON.
- **Apalache earns its keep** in every flow: catches the missing payment guard, proves the auth-UI dashboard guard, validates cross-area contracts.
- **Brownfield and drift are first-class**, not separate workflows — `/spec` adapts to context.

---

## Prerequisites

- **[Claude Code](https://claude.com/claude-code)** — to run the `/spec-*` commands.
- **[Apalache](https://apalache-mc.org/)** — symbolic model checker for Quint. Used by `/spec-check`. Requires Java 17+. See the [JVM install guide](https://apalache-mc.org/docs/apalache/installation/jvm.html) or run `tools/check-tooling.sh` for platform-specific install hints.
- **Python 3** — for the `tools/` scripts (lint, matrix, ITF trace tooling).
- **Git** — but no branch ceremony; use git however your team uses git.

---

## What's in the Template

```
spec-template/
├── README.md                  ← this file (bootstrap replaces it with a project stub)
├── METHODOLOGY.md             ← the methodology — stays in every project
├── .claude/commands/          ← the 5 /spec-* commands
├── schemas/                   ← 6 JSON schemas (area, change, journey, project, pattern, protocol)
├── templates/                 ← spec.qnt.template, probes.qnt.template (sidecar + probe conventions)
├── .github/workflows/         ← spec-ci.yml (lint → matrix → typecheck → quint test; skips pre-bootstrap)
├── tools/                     ← spec-lint, spec-record, spec-readback, spec-matrix, quint_ir, itf_tools, bootstrap.sh (self-removes)
└── examples/                  ← sample auth area + auth-ui area (stripped by bootstrap)
```

After `tools/bootstrap.sh` runs, `examples/` and this README are gone, and `tools/bootstrap.sh` self-removes. Everything else (including the rest of `tools/`) is part of your project.

---

## Examples

Before bootstrapping (or by browsing this repo on GitHub), look at:

- `examples/.spec/project.json` — a real project config with two areas.
- `examples/specs/auth.json` + `examples/specs/auth.qnt` — login/lockout area, with invariants Apalache verifies.
- `examples/specs/auth-ui.json` + `examples/specs/auth-ui.qnt` — UI area with navigation modeled as a Quint state machine ("Dashboard is unreachable without auth" is a model-checked invariant).

Reference material, not starter content. The bootstrap removes it.

---

## Updating the Methodology in an Existing Project

The methodology evolves. To pull updates into a project that was created from this template:

- `git remote add template <this-template-url>` once
- `git fetch template` and cherry-pick or merge changes from `METHODOLOGY.md`, `.claude/commands/`, `schemas/`, or `tools/` as needed

There's no automatic upgrade tool — these files are part of your project's history once bootstrapped.
