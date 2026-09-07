// ============================================================
// Alloy structural sidecar: session-ownership
// Companion JSON: specs/session-ownership.contract.json (v1.0.0)
//
// Structure only. The behaviour of logging in, expiring and
// locking lives in auth.qnt and is checked by Apalache; nothing
// here mentions time, order or transitions. What IS here are the
// relational facts that must hold of every legal configuration
// of the two areas together — the questions that would cost a
// state-space product to ask in Quint and cost nothing here.
// ============================================================

module sessionOwnership

// ------------------------------------------------------------
// SIGNATURES — the entities of auth and auth-ui as structure.
// Cardinality lives in the field declarations: `one` here IS the
// referential-integrity constraint.
// ------------------------------------------------------------

sig Account {}

sig Session {
  owner: one Account          // every session belongs to exactly one account
}

abstract sig Screen {}

sig PublicScreen extends Screen {}

sig GuardedScreen extends Screen {
  guard: one Session          // an auth-required screen names the session it trusts
}

// ------------------------------------------------------------
// ASSERTIONS + CHECKS — one pair per structural INV-CONTRACT-*.
// Scopes are always explicit: with no `for` clause Alloy silently
// uses 3, and a bound nobody wrote down is a bound nobody
// reviewed. `expect 0` also makes each check a plain CI
// assertion — the CLI exits non-zero when a counterexample turns
// up, with no spec tooling installed.
// ------------------------------------------------------------

// INV-CONTRACT-001
assert noSharedSessions {
  all disj s1, s2: Session | s1.owner != s2.owner
}
check noSharedSessions for 4 Session, 4 Account, 4 Screen expect 0

// INV-CONTRACT-002
assert everyGuardedScreenHasAnOwner {
  all g: GuardedScreen | some g.guard.owner
}
check everyGuardedScreenHasAnOwner for 4 Session, 4 Account, 4 Screen expect 0
