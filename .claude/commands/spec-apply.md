# /spec-apply — Generate Code From Spec

Generate or update implementation code from the area's spec (Quint sidecar + architecture). Per-component when components are declared. Writes `traceability[]` back into the area JSON. Refuses on contract targets.

## Usage
```
/spec-apply [target]
/spec-apply [target] --force                # generate even if architecture is incomplete
/spec-apply [target] --tests-only           # regenerate tests, keep code
/spec-apply [target] --component <name>     # only one component
```

## Instructions

You are the **Applier**. You translate the formal Quint model into implementation code, using the architecture decisions to determine language, layout, patterns, and conventions. You do not invent behavior — every line is grounded in the spec or in the resolved architecture's patterns/protocols.

### Step 1 — Resolve target and refuse contracts

Resolve the target set:

- Explicit `[target]` → apply just it. Doesn't alter the active change.
- No target → read `last_change` from `.spec/local.json`, load `.spec/changes/<slug>.json`. Target set = every `targets[]` entry whose area has code; skip contracts with a one-line note. Print: `Change: <slug> — applying <n> code targets`. Walk targets one at a time — each gets its own architecture resolution, mapping confirmation, and generation pass (Steps 2–5); don't interleave.
- No active change → if there's exactly one area, use it; else ask.

The manifest stores no phase flags — "applied" is derived (every one of the target's manifest `ids[]` that is a REQ/INV has a `traceability[]` entry).

Read:
- `specs/<target>.json`
- `specs/<target>.qnt`
- `.spec/project.json`
- `.spec/local.json`
- For referenced patterns: `.spec/patterns/<name>.json` each
- For referenced protocols: `.spec/protocols/<name>.json` each
- For per-component overrides: `specs/<target>/components/<name>.json` each (if any)

Refuse if `kind == "contract"`:
```
/spec-apply does not apply to contract targets. Contracts are spec-only; their verification
is /spec-check. To regenerate code for an area that participates in a contract, run
/spec-apply on the area itself.
```

### Step 2 — Resolve architecture (don't re-ask)

Compute the **resolved architecture** at each level:

- Project (`.spec/project.json` `architecture`) → defaults
- Area (`specs/<target>.json` `architecture`) → overrides
- Component (per component, if declared) → finer overrides

For each field: per-component > per-area > project > undefined. Pattern and protocol references **union** across scopes.

Print the resolved view so the user can confirm:

```
## Resolved Architecture — <target>

Stack:           TypeScript 5.4 + Express + Vitest
Persistence:     postgres via Prisma
Cross-cutting:   pino logging, OpenTelemetry tracing
Patterns:        outbox (project), repository (project)
Protocols:       api-envelope (project), pagination-cursor (area)

Components:
  api         (transport)    implements: login, logout
                             patterns: outbox   protocols: api-envelope, pagination-cursor
  worker      (async)        implements: expireSession
                             patterns: outbox, saga-orchestrated
```

Verify pattern preconditions. If a pattern requires `persistence.kind == sql` but resolved persistence is something else, STOP — preconditions are hard requirements.

If required fields are missing (e.g. `stack.language`), ask. Offer to write the answer back to the right level (project / area / component).

### Step 3 — Map Quint constructs to code

Resolve the **code root** (from `.spec/project.json` + `.spec/local.json`). Verify it exists; if not (multi-repo, repo not cloned locally), tell the user where to clone it and what to add to `local.json`.

Build a mapping table (only for in-scope items if `--component` filter active):

```
## Implementation Mapping — auth

TYPES:
  AccountStatus (Unlocked | Locked)
    → enum AccountStatus { Unlocked, Locked }                in types.ts

STATE:
  var sessions: SessionId -> (UserId, SessionStatus)
    → class SessionStore { private map: Map<...> }           in authStore.ts (api)

ACTIONS → FUNCTIONS:
  action login → function login(userId, sessionId): void     in authService.ts (api)
    Guards:  accounts.get(u) == Unlocked → throw AccountLockedError
             sessions.values()...size() == 0 → throw AlreadyLoggedInError

INVARIANTS → GUARDS + TESTS:
  val singleSession   → assertion in login()                 + tests/invariants.test.ts
  val noLockedSession → assertion in login()                 + tests/invariants.test.ts

PROTOCOLS:
  api-envelope → wrap controller responses in {data, errors, meta}
  pagination-cursor → cursor-based pagination on list endpoints (n/a for this area)

PATTERNS:
  repository → SessionStore implements Repository contract
  outbox     → AccountLockedEvent written to outbox table on lock
```

Show the table; ask "Does this mapping look right?" Wait for confirmation.

### Step 4 — Generate

For each component (or the area if monolithic):

1. Use the component's resolved architecture for stack/layout/patterns/protocols.
2. Route Quint actions to the component based on `components[].implements[]`.
3. For each Quint construct:
   - `type` → language-native type/interface/enum
   - `var` → encapsulated state (store/repository)
   - `action` → function with guards → throws, mutations on store
   - `invariant` → assertion helper + test
   - `run` → integration test scenario
4. Apply pattern templates from `pattern.generates[]` (additional files: outbox writer, saga state, etc.).
5. Apply protocols at I/O boundaries (wrap responses, parse required headers, encode per protocol definition).

**On existing code:** inspect first. For each file, diff against what would be generated; show the user, ask before overwriting non-trivial existing logic. Preserve hand-edits that don't conflict.

### Step 4a — Generate the conformance adapter + replay harness

`/spec-verify` replays the witness traces (ITF files under `specs/<target>/traces/`) through an adapter against the real implementation (rationale: METHODOLOGY.md → "Conformance"). Generate the artifacts in the target stack:

1. **Adapter** — maps the formal model to the implementation:
   - one method per Quint action (`login(uid, sid)` → call the real `authService.login(...)`, catching domain errors so guard-rejections are observable);
   - one getter per Quint var, returning the *abstracted* observable state (e.g. read the sessions table and project it to `{sessionId: status}` — same shape the model uses);
   - a `reset()` that returns the system under test to the model's `init` state.

2. **Replay harness** — a test file that, for each `*.itf.json` in the traces dir:
   - parses the ITF states (ints arrive as `{"#bigint": "n"}`, maps as `{"#map": [[k,v],...]}`, variants as `{tag, value}` — mirror `tools/itf_tools.py render_value` semantics);
   - reads the action per step from the `_lastAction` ghost var and its parameters from the param ghosts (`_lastUid`, `_lastSid`, … — written by the probe module's instrumented step; for `quint run --mbt` traces use `mbt::actionTaken`/`mbt::nondetPicks`). Never infer call parameters from state diffs;
   - calls the adapter method for each step, then asserts every Quint var getter equals the trace's state under the abstraction (ghost vars are bookkeeping — excluded from the comparison);
   - reports the first diverging step with expected/observed values.

3. **Harness self-test** — proof the harness *can* fail. Generate `_selftest.tampered.itf.json` in the traces dir: a copy of one real witness trace with one final-state value deliberately corrupted (e.g. flip `Locked` → `Unlocked`). The harness must include a test asserting that replaying it **fails** (rationale: METHODOLOGY.md → "Conformance"). The `_selftest.` prefix keeps it out of the regular replay loop.

Write the config into the area JSON:

```json
"conformance": {
  "adapter":    "src/auth/conformance/adapter.ts",
  "harness":    "tests/auth/conformance/replay.test.ts",
  "command":    "pnpm vitest run tests/auth/conformance",
  "traces_dir": "auth/traces"
}
```

Keep the adapter thin — abstraction mapping only, no logic. If an action can't be mapped 1:1 to a code entry point, that's a finding: the architecture hides a spec-level behavior. Surface it instead of faking the mapping.

**Comments:** include a single line per generated function naming the spec ID: `// REQ-001` or `// INV-001 guard`. Don't write paragraph docstrings — the spec IS the documentation.

### Step 5 — Update traceability

Write `traceability[]` in `specs/<target>.json`:

```json
[
  { "id": "REQ-001", "quint": "action login", "component": "api", "code": "authService.ts:login", "tests": ["authService.test.ts:loginSuccess"], "verified": false },
  { "id": "INV-001", "quint": "val singleSession", "component": "api", "code": "authService.ts:login (guard)", "tests": ["invariants.test.ts:singleSession"], "verified": false }
]
```

`verified: false` until `/spec-verify` confirms. Code/test paths are relative to the resolved code root (so the trace stays stable when the repo is mounted at different paths on different machines).

Bump area `version` (minor for added components or new mappings, patch for code regen with no spec change).

### Step 6 — Summary

```
## Code Generated — auth

Code root:  /Users/alice/work/service-api/src/auth/
Tests root: /Users/alice/work/service-api/tests/auth/

Files written / updated:
  src/auth/types.ts
  src/auth/authStore.ts        (api component)
  src/auth/authService.ts      (api component)
  src/auth/expireWorker.ts     (worker component)
  src/auth/outbox.ts           (outbox pattern)
  src/auth/conformance/adapter.ts        (model↔code adapter)
  tests/auth/conformance/replay.test.ts  (witness-trace replay harness)
  tests/auth/authService.test.ts
  tests/auth/invariants.test.ts
  tests/auth/scenarios.test.ts

Coverage:
  REQ-*:  4/4 implemented
  INV-*:  2/2 guards + tests
  Quint runs → scenario tests: 2/2
  Conformance: adapter covers 5/5 actions, 5/5 vars

specs/auth.json — traceability[] + conformance updated

Next: /spec-verify auth — confirm it all matches.
```

### Step 7 — Commit

```bash
git add specs/<target>.json <code-root>/...
git commit -m "spec(<target>): apply — <summary>"
```

In multi-repo, you'll have two commits — one in the spec repo (updates to traceability + version), one in the code repo. Show both diffs; commit each in its own repo.
