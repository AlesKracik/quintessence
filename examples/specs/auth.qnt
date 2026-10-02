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
  /// Modeled as a step counter for bounded checking.
  /// @unit step-units
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
  /// @req REQ-005
  /// While the account is Locked, if the user submits valid credentials,
  /// then the system shall refuse the login.
  /// @status specified
  /// @modality forbidden
  /// @while the Account is Locked
  /// @when the user submits valid credentials
  /// @unwanted
  /// @shall refuse the login and leave every session and counter untouched
  /// @meaning A locked account cannot be signed into, even with the right
  ///   password. The attempt changes nothing — no new session, and the
  ///   failure count does not move either.
  /// @meaning-author agent
  /// @enforced-by INV-002
  /// @witness skipped: A prohibition has no reachable state to witness —
  ///   there is no step in which 'the login did not happen' becomes visible.
  ///   INV-002 (noSessionWhileLocked) carries the model-side proof.
  /// @refusal tests/auth/refusal/locked-login.test.ts
  /// @blocking accountStatus[uid] == Locked
  /// @unchanged sessions, userActiveSession, failedAttempts
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

  // ----------------------------------------------------------
  // REQUIREMENTS — witness predicates, constants and transition
  // properties whose records live on them (spec_source.py)
  // ----------------------------------------------------------

  /// @req REQ-001
  /// @status specified
  /// @when a registered user submits valid credentials
  /// @shall create an Active session owned by that user
  /// @meaning Signing in with correct credentials gives the user a live
  ///   session of their own — the thing every later request is checked
  ///   against.
  /// @meaning-author agent
  /// @via login
  /// @pre not(_prevSessions.keys().contains(_lastSid))
  /// @pre-note The session must not have existed before the call — otherwise
  ///   'some Active session exists' is satisfied by one an earlier login
  ///   created.
  def shall_REQ_001(_lastSid: SessionId, _lastUid: UserId): bool =
    sessions.keys().contains(_lastSid) and statusOf(sessions, _lastSid) == Active and activeSessionsOf(_lastUid).contains(_lastSid)

  /// @req REQ-002
  /// While a user's session is Active, when the user requests logout, the
  /// system shall transition that session to LoggedOut.
  /// @status specified
  /// @while the user's session is Active
  /// @when the user requests logout
  /// @shall transition that session to LoggedOut
  /// @meaning Logging out ends the session it was asked about. The session
  ///   record stays, marked as logged out, rather than disappearing.
  /// @meaning-author agent
  /// @via logout
  /// @pre statusOf(_prevSessions, _lastSid) == Active
  /// @pre-note Logging out something already logged out proves nothing.
  def shall_REQ_002(_lastSid: SessionId): bool =
    statusOf(sessions, _lastSid) == LoggedOut

  /// @req REQ-003
  /// If a login attempt fails while the account is Unlocked, then the system
  /// shall increment failedAttempts and lock the account when it reaches
  /// MAX_FAILED_ATTEMPTS.
  /// @status specified
  /// @while the account is Unlocked
  /// @when a login attempt fails
  /// @unwanted
  /// @shall increment failedAttempts and lock the account when it reaches
  ///   MAX_FAILED_ATTEMPTS
  /// @meaning Every failed sign-in on an account that is still usable counts
  ///   against it, and once the count reaches MAX_FAILED_ATTEMPTS the account
  ///   is locked. The lock happens on the attempt that reaches the limit, not
  ///   the one after it.
  /// @meaning-author agent
  /// @via login_failed
  /// @pre not(_prevAccountStatus.keys().contains(_lastUid) and
  ///   _prevAccountStatus.get(_lastUid) == Locked)
  /// @pre-note The account must have been unlocked before the failure that
  ///   locked it — a state where it was already Locked witnesses nothing
  ///   about the lockout rule.
  def shall_REQ_003(_lastUid: UserId): bool =
    accountStatus.keys().contains(_lastUid) and accountStatus.get(_lastUid) == Locked

  /// @req REQ-004
  /// While a session is Active and has been inactive for longer than
  /// MAX_SESSION_AGE, the system shall transition it to Expired.
  /// @status specified
  /// @while the session is Active and has been inactive for longer than
  ///   MAX_SESSION_AGE
  /// @shall transition it to Expired
  /// @meaning A session left idle for longer than MAX_SESSION_AGE stops being
  ///   usable on its own, without anyone having to log out.
  /// @meaning-author agent
  /// @via expire_session
  /// @pre statusOf(_prevSessions, _lastSid) == Active
  /// @pre-note Only an Active session can expire.
  def shall_REQ_004(_lastSid: SessionId): bool =
    statusOf(sessions, _lastSid) == Expired
}
