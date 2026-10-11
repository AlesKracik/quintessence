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

  /// @con CON-001
  /// Failed-login threshold before locking.
  /// @pairs INV-004
  pure val MAX_FAILED_ATTEMPTS: int = 5    // CON-001, paired with INV-004
  /// @con CON-002
  /// How long a session may sit idle before it expires, in ticks of the
  /// model's clock. Kept small so the checker reaches an expiry within its
  /// step bound; the production timeout is this many clock units.
  /// @unit ticks
  /// @pairs INV-005
  pure val MAX_SESSION_AGE: int = 3        // CON-002, paired with INV-005

  // -- State --------------------------------------------------------

  var sessions:          SessionId -> SessionStatus   // REQ-001, REQ-002, REQ-004
  var sessionOwner:      SessionId -> UserId          // REQ-001
  var accountStatus:     UserId -> AccountStatus      // REQ-003
  var failedAttempts:    UserId -> int                // REQ-003, CON-001
  var userActiveSession: UserId -> Set[SessionId]     // INV-001
  var idleFor:           SessionId -> int             // REQ-004, CON-002

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

  def idleOf(sid: SessionId): int =
    if (idleFor.keys().contains(sid)) idleFor.get(sid) else 0

  // -- Init ---------------------------------------------------------

  action init = all {
    sessions'          = Map(),
    sessionOwner'      = Map(),
    accountStatus'     = Map(),
    failedAttempts'    = Map(),
    userActiveSession' = Map(),
    idleFor'           = Map(),
  }

  // -- Actions ------------------------------------------------------

  // Signing in. Two requirements govern this step: what a login does
  // (REQ-001), and when it must not happen at all (REQ-005).
  /// @req REQ-001
  /// When a registered user submits valid credentials, the system shall
  /// create an Active session owned by that user.
  /// @status specified
  /// @when a registered user submits valid credentials
  /// @shall isActive(sid) and sessionOwner.get(sid) == uid
  ///   and activeSessionsOf(uid).contains(sid)
  ///
  /// @req REQ-005
  /// While the account is Locked, if the user submits valid credentials,
  /// then the system shall refuse the login.
  /// @status specified
  /// @modality forbidden
  /// @unwanted
  /// @when the user submits valid credentials
  /// @while isLocked(uid)
  /// @refs INV-002
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
    idleFor'           = idleFor.put(sid, 0),
  }

  /// @req REQ-002
  /// While a user's session is Active, when the user requests logout, the
  /// system shall transition that session to LoggedOut.
  /// @status specified
  /// @when the user requests logout
  /// @while isActive(sid)
  /// @shall statusOf(sessions, sid) == LoggedOut
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
      idleFor'           = idleFor,
    }
  }

  // Locking also ENDS the user's live sessions. Without that, locking an
  // account left its Active session alive and INV-002 was violated.
  // Found by checking this file after a rewrite; the Session state
  // machine records the edge.
  //
  // REQ-003 says both sides of the threshold in one obligation: the
  // account is Locked after this failure IF AND ONLY IF the count has
  // reached MAX_FAILED_ATTEMPTS. Loosen the guard to `>= 1` and the
  // every-step check fails on the first failure — no separate
  // "not early" invariant is needed for that.
  /// @req REQ-003
  /// If a login attempt fails while the account is Unlocked, then the system
  /// shall increment failedAttempts and lock the account when it reaches
  /// MAX_FAILED_ATTEMPTS.
  /// @status specified
  /// @unwanted
  /// @when a login attempt fails
  /// @while not(isLocked(uid))
  /// @old before: int = attemptsOf(uid)
  /// @shall attemptsOf(uid) == before + 1
  ///   and (isLocked(uid) iff attemptsOf(uid) >= MAX_FAILED_ATTEMPTS)
  action login_failed(uid: UserId): bool = {
    val nextCount = attemptsOf(uid) + 1
    val locking = nextCount >= MAX_FAILED_ATTEMPTS
    val doomed = activeSessionsOf(uid)
    all {
      not(isLocked(uid)),
      sessions' = if (locking)
                    doomed.fold(sessions, (acc, s) => acc.put(s, LoggedOut))
                  else sessions,
      sessionOwner'      = sessionOwner,
      accountStatus'     = accountStatus.put(uid, if (locking) Locked else Unlocked),
      failedAttempts'    = failedAttempts.put(uid, nextCount),
      userActiveSession' = if (locking)
                             userActiveSession.put(uid, Set())
                           else userActiveSession,
      idleFor'           = idleFor,
    }
  }

  // An Active session expires once it has been idle longer than
  // MAX_SESSION_AGE ticks — the rule the sentence states, read here.
  /// @req REQ-004
  /// While a session is Active and has been inactive for longer than
  /// MAX_SESSION_AGE, the system shall transition it to Expired.
  /// @status specified
  /// @when the session's idle time runs out
  /// @while isActive(sid) and idleOf(sid) > MAX_SESSION_AGE
  /// @shall statusOf(sessions, sid) == Expired
  action expire_session(sid: SessionId): bool = {
    val uid = if (sessionOwner.keys().contains(sid)) sessionOwner.get(sid) else ""
    all {
      sessions.keys().contains(sid),
      isActive(sid),
      idleOf(sid) > MAX_SESSION_AGE,
      sessions'          = sessions.put(sid, Expired),
      sessionOwner'      = sessionOwner,
      accountStatus'     = accountStatus,
      failedAttempts'    = failedAttempts,
      userActiveSession' = userActiveSession.put(
        uid,
        activeSessionsOf(uid).exclude(Set(sid))
      ),
      idleFor'           = idleFor,
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
    idleFor'           = idleFor,
  }

  // The model's clock: every Active session gets one tick older. Expiry
  // (REQ-004) reads that age, so the session timeout is a rule the model
  // enforces rather than a number in a sentence.
  action tick: bool = all {
    sessions.keys().exists(sid => isActive(sid)),
    sessions'          = sessions,
    sessionOwner'      = sessionOwner,
    accountStatus'     = accountStatus,
    failedAttempts'    = failedAttempts,
    userActiveSession' = userActiveSession,
    idleFor'           = idleFor.keys().mapBy(sid =>
                           if (isActive(sid)) idleFor.get(sid) + 1 else idleFor.get(sid)),
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
      tick,
    }
  }

  // -- Invariants ---------------------------------------------------

  // INV-001: A user has at most one Active session.
  /// @inv INV-001
  /// At most one Active session per user.
  /// @criticality critical
  val atMostOneActiveSession: bool =
    userActiveSession.keys().forall(uid =>
      activeSessionsOf(uid).size() <= 1
    )

  // INV-002: A Locked account has no Active session.
  /// @inv INV-002
  /// A Locked account has no Active session.
  /// @criticality critical
  val noSessionWhileLocked: bool =
    accountStatus.keys().forall(uid =>
      isLocked(uid) implies activeSessionsOf(uid).size() == 0
    )

  // INV-003: Expired/LoggedOut sessions never appear in any user's active set.
  /// @inv INV-003
  /// Expired/LoggedOut sessions never appear in any user's active set.
  /// @criticality high
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
  /// @inv INV-004
  /// Below MAX_FAILED_ATTEMPTS, the account stays unlocked.
  /// @criticality high
  val noEarlyLockout: bool =
    failedAttempts.keys().forall(uid =>
      (attemptsOf(uid) < MAX_FAILED_ATTEMPTS) implies not(isLocked(uid))
    )

  // INV-005: paired with CON-002 (MAX_SESSION_AGE). The witness for REQ-004
  // proves a session CAN expire; this is the other side of the boundary:
  // nothing expires before it has been idle longer than MAX_SESSION_AGE.
  /// @inv INV-005
  /// No session expires before it has been idle longer than MAX_SESSION_AGE.
  /// @criticality high
  val noEarlyExpiry: bool =
    sessions.keys().forall(sid =>
      (statusOf(sessions, sid) == Expired) implies idleOf(sid) > MAX_SESSION_AGE
    )

  // -- Examples -----------------------------------------------------
  // Each run is an example: `quint test` executes it, the readback shows it
  // as a Given / When / Then row (derived from the run, never typed beside
  // it), and the code's conformance harness replays it.

  /// @example EX-001
  /// Sign in
  /// @refs REQ-001
  run signIn = init
    .then(login("alice", "s1"))
    .expect(isActive("s1") and activeSessionsOf("alice") == Set("s1"))

  /// @example EX-002
  /// Sign in, then out
  /// @refs REQ-002
  run happyPath = init
    .then(login("alice", "s1"))
    .then(logout("s1"))
    .expect(statusOf(sessions, "s1") == LoggedOut)

  /// @example EX-003
  /// The fifth failure locks the account
  /// @refs REQ-003
  run lockoutScenario = init
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .expect(isLocked("bob"))

  // The boundary, asserted from the other side: four failures must NOT lock.
  // This is the scenario that fails when someone loosens the guard.
  /// @example EX-004
  /// Four failures do not lock it
  /// @refs REQ-003, INV-004
  run belowThresholdScenario = init
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .expect(not(isLocked("bob")))

  /// @example EX-005
  /// A session idle for four ticks expires
  /// @refs REQ-004
  run sessionExpiry = init
    .then(login("alice", "s1"))
    .then(tick)
    .then(tick)
    .then(tick)
    .then(tick)
    .then(expire_session("s1"))
    .expect(statusOf(sessions, "s1") == Expired)

  // The boundary from the other side: idle for exactly MAX_SESSION_AGE
  // ticks, the session cannot expire yet.
  /// @example EX-007
  /// Three idle ticks are not enough to expire
  /// @refs REQ-004, INV-005
  run notExpiredYet = init
    .then(login("alice", "s1"))
    .then(tick)
    .then(tick)
    .then(tick)
    .expect(isActive("s1") and idleOf("s1") == 3)
    .then(expire_session("s1").fail())

  // A refusal is shown, not asserted: drive the model into the blocking
  // state, assert it, and END with the call `.fail()` — the run fails if
  // the call goes through. A refused step leaves the model's state as it
  // was (Quint assigns nothing after `.fail()`, so nothing can follow it);
  // that nothing moved in the CODE is what conformance replay checks.
  /// @example EX-006
  /// A locked account cannot sign in
  /// @refs REQ-005
  run lockedLoginRefused = init
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .then(login_failed("bob"))
    .expect(isLocked("bob") and attemptsOf("bob") == 5)
    .then(login("bob", "s2").fail())
}
