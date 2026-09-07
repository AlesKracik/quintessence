// Quint sidecar for specs/auth.area.json — the formal source of truth.
// Module name and imports are read from this file directly (quint_ir);
// the JSON only points here via formal_model.quint_file.
//
// IDIOM NOTE (this file must actually compile — an example that does not
// teaches the wrong thing and silently drops every consumer onto the regex
// fallback parser):
//   - There is no `require` in Quint. A precondition is a bare boolean
//     conjunct inside `all { ... }`.
//   - There is no builtin `getOrElse`. Map access is `m.get(k)`, and
//     `get` on an absent key is UNDEFINED — so guard it:
//     `if (m.keys().contains(k)) m.get(k) else <default>`.
//   - A `{ ... }` body may bind local `val`s and then yield ONE expression.
//     Bare statements in a block are a parse error.

module auth {

  // -- Types --------------------------------------------------------

  type UserId    = str
  type SessionId = str

  type SessionStatus =
    | Active
    | Expired
    | LoggedOut

  type AccountStatus =
    | Unlocked
    | Locked

  // -- Constants (spec-lint cross-checks against constraints[]) -----

  pure val MAX_FAILED_ATTEMPTS: int = 5    // CON-001, paired with INV-004
  pure val MAX_SESSION_AGE: int = 24       // CON-002

  // -- State --------------------------------------------------------

  var sessions:          SessionId -> SessionStatus   // REQ-001, REQ-002, REQ-004
  var sessionOwner:      SessionId -> UserId          // REQ-001
  var accountStatus:     UserId -> AccountStatus      // REQ-003
  var failedAttempts:    UserId -> int                // REQ-003, CON-001
  var userActiveSession: UserId -> Set[SessionId]     // INV-001

  // -- Helpers ------------------------------------------------------

  pure def statusOf(m: SessionId -> SessionStatus, sid: SessionId): SessionStatus =
    if (m.keys().contains(sid)) m.get(sid) else LoggedOut

  def isActive(sid: SessionId): bool =
    statusOf(sessions, sid) == Active

  def isLocked(uid: UserId): bool =
    if (accountStatus.keys().contains(uid)) accountStatus.get(uid) == Locked else false

  def attemptsOf(uid: UserId): int =
    if (failedAttempts.keys().contains(uid)) failedAttempts.get(uid) else 0

  def activeSessionsOf(uid: UserId): Set[SessionId] =
    if (userActiveSession.keys().contains(uid)) userActiveSession.get(uid) else Set()

  // -- Init ---------------------------------------------------------

  action init = all {
    sessions'          = Map(),
    sessionOwner'      = Map(),
    accountStatus'     = Map(),
    failedAttempts'    = Map(),
    userActiveSession' = Map(),
  }

  // -- Actions ------------------------------------------------------

  // REQ-001: A registered user with valid credentials can log in.
  action login(uid: UserId, sid: SessionId): bool = all {
    not(isLocked(uid)),
    not(sessions.keys().contains(sid)),
    activeSessionsOf(uid).size() == 0,          // INV-001 guard
    sessions'          = sessions.put(sid, Active),
    sessionOwner'      = sessionOwner.put(sid, uid),
    accountStatus'     = accountStatus,
    failedAttempts'    = failedAttempts.put(uid, 0),
    userActiveSession' = userActiveSession.put(
      uid,
      activeSessionsOf(uid).union(Set(sid))
    ),
  }

  // REQ-002: A user with an Active session can log out.
  action logout(sid: SessionId): bool = {
    val uid = if (sessionOwner.keys().contains(sid)) sessionOwner.get(sid) else ""
    all {
      sessions.keys().contains(sid),
      isActive(sid),
      sessions'          = sessions.put(sid, LoggedOut),
      sessionOwner'      = sessionOwner,
      accountStatus'     = accountStatus,
      failedAttempts'    = failedAttempts,
      userActiveSession' = userActiveSession.put(
        uid,
        activeSessionsOf(uid).exclude(Set(sid))
      ),
    }
  }

  // REQ-003: After MAX_FAILED_ATTEMPTS, the account is locked.
  //
  // Locking also ENDS the user's live sessions. Without that, locking an
  // account left its Active session alive and INV-002 was violated — which
  // REQ-005 leans on as its model-side proof. Found by checking this file
  // after the rewrite; the Session state machine records the edge.
  action login_failed(uid: UserId): bool = {
    val next = attemptsOf(uid) + 1
    val locking = next >= MAX_FAILED_ATTEMPTS
    val doomed = activeSessionsOf(uid)
    all {
      not(isLocked(uid)),
      sessions' = if (locking)
                    doomed.fold(sessions, (acc, s) => acc.put(s, LoggedOut))
                  else sessions,
      sessionOwner'      = sessionOwner,
      accountStatus'     = accountStatus.put(uid, if (locking) Locked else Unlocked),
      failedAttempts'    = failedAttempts.put(uid, next),
      userActiveSession' = if (locking)
                             userActiveSession.put(uid, Set())
                           else userActiveSession,
    }
  }

  // REQ-004: An Active session expires after inactivity.
  action expire_session(sid: SessionId): bool = {
    val uid = if (sessionOwner.keys().contains(sid)) sessionOwner.get(sid) else ""
    all {
      sessions.keys().contains(sid),
      isActive(sid),
      sessions'          = sessions.put(sid, Expired),
      sessionOwner'      = sessionOwner,
      accountStatus'     = accountStatus,
      failedAttempts'    = failedAttempts,
      userActiveSession' = userActiveSession.put(
        uid,
        activeSessionsOf(uid).exclude(Set(sid))
      ),
    }
  }

  // Admin recovery action (not in requirements yet; see Q-001).
  action unlock_account(uid: UserId): bool = all {
    isLocked(uid),
    sessions'          = sessions,
    sessionOwner'      = sessionOwner,
    accountStatus'     = accountStatus.put(uid, Unlocked),
    failedAttempts'    = failedAttempts.put(uid, 0),
    userActiveSession' = userActiveSession,
  }

  // -- Step ---------------------------------------------------------

  action step = {
    nondet uid = oneOf(Set("alice", "bob", "charlie"))
    nondet sid = oneOf(Set("s1", "s2", "s3"))
    any {
      login(uid, sid),
      logout(sid),
      login_failed(uid),
      expire_session(sid),
      unlock_account(uid),
    }
  }

  // -- Invariants ---------------------------------------------------

  // INV-001: A user has at most one Active session.
  val atMostOneActiveSession: bool =
    userActiveSession.keys().forall(uid =>
      activeSessionsOf(uid).size() <= 1
    )

  // INV-002: A Locked account has no Active session.
  val noSessionWhileLocked: bool =
    accountStatus.keys().forall(uid =>
      isLocked(uid) implies activeSessionsOf(uid).size() == 0
    )

  // INV-003: Expired/LoggedOut sessions never appear in any user's active set.
  val noAccessWithInvalidToken: bool =
    sessions.keys().forall(sid =>
      (statusOf(sessions, sid) != Active) implies
        userActiveSession.keys().forall(uid =>
          not(activeSessionsOf(uid).contains(sid))
        )
    )

  // INV-004: paired with CON-001 (MAX_FAILED_ATTEMPTS).
  //
  // The witness for REQ-003 proves lockout CAN happen. It cannot prove
  // lockout does not happen EARLY — with the guard loosened to `>= 1`,
  // every invariant still held, the witness was still found (in one step),
  // and lint was clean. This is the other side of the threshold: below the
  // limit, the account stays unlocked.
  val noEarlyLockout: bool =
    failedAttempts.keys().forall(uid =>
      (attemptsOf(uid) < MAX_FAILED_ATTEMPTS) implies not(isLocked(uid))
    )

  // -- Scenarios ----------------------------------------------------

  run happyPath = init
    .then(login("alice", "s1"))
    .then(logout("s1"))

  run lockoutScenario = init
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .expect(isLocked("bob"))

  // The boundary, asserted from the other side: four failures must NOT lock.
  // This is the scenario that fails when someone loosens the guard.
  run belowThresholdScenario = init
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .expect(not(isLocked("bob")))

  run sessionExpiry = init
    .then(login("alice", "s1"))
    .then(expire_session("s1"))
    .expect(not(isActive("s1")))

}
