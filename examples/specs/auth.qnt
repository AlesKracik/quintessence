// Quint sidecar for specs/auth.json — the formal source of truth.
// Module name and imports are read from this file directly (quint_ir);
// the JSON only points here via formal_model.quint_file.

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

  pure val MAX_FAILED_ATTEMPTS: int = 5    // CON-001
  pure val MAX_SESSION_AGE: int = 24       // CON-002

  // -- State --------------------------------------------------------

  var sessions:          SessionId -> SessionStatus   // REQ-001, REQ-002, REQ-004
  var sessionOwner:      SessionId -> UserId          // REQ-001
  var accountStatus:     UserId -> AccountStatus      // REQ-003
  var failedAttempts:    UserId -> int                // REQ-003, CON-001
  var userActiveSession: UserId -> Set[SessionId]     // INV-001

  // -- Helpers ------------------------------------------------------

  pure def isActive(sid: SessionId): bool =
    sessions.get(sid) == Active

  pure def isLocked(uid: UserId): bool =
    accountStatus.getOrElse(uid, Unlocked) == Locked

  pure def activeSessionsOf(uid: UserId): Set[SessionId] =
    userActiveSession.getOrElse(uid, Set())

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
    require(not(isLocked(uid))),
    require(not(sessions.keys().contains(sid))),
    require(activeSessionsOf(uid).size() == 0),  // INV-001 guard
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
    require(sessions.keys().contains(sid))
    require(isActive(sid))
    val uid = sessionOwner.get(sid)
    all {
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
  action login_failed(uid: UserId): bool = {
    require(not(isLocked(uid)))
    val cur = failedAttempts.getOrElse(uid, 0)
    val next = cur + 1
    val newStatus = if (next >= MAX_FAILED_ATTEMPTS) Locked else Unlocked
    all {
      sessions'          = sessions,
      sessionOwner'      = sessionOwner,
      accountStatus'     = accountStatus.put(uid, newStatus),
      failedAttempts'    = failedAttempts.put(uid, next),
      userActiveSession' = userActiveSession,
    }
  }

  // REQ-004: An Active session expires after inactivity.
  action expire_session(sid: SessionId): bool = {
    require(sessions.keys().contains(sid))
    require(isActive(sid))
    val uid = sessionOwner.get(sid)
    all {
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
    require(isLocked(uid)),
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
      (sessions.get(sid) != Active) implies
        userActiveSession.keys().forall(uid =>
          not(activeSessionsOf(uid).contains(sid))
        )
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

  run sessionExpiry = init
    .then(login("alice", "s1"))
    .then(expire_session("s1"))
    .expect(not(isActive("s1")))

}
