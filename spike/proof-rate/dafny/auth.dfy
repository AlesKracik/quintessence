module Auth {
  datatype Option<T> = None | Some(value: T)

  type UserId = string
  type SessionId = string
  datatype SessionStatus = Active | Expired | LoggedOut
  datatype AccountStatus = Unlocked | Locked

  const MAX_FAILED_ATTEMPTS: int := 5
  const MAX_SESSION_AGE: int := 3

  datatype State = State(
    sessions: map<SessionId, SessionStatus>,
    sessionOwner: map<SessionId, UserId>,
    accountStatus: map<UserId, AccountStatus>,
    failedAttempts: map<UserId, int>,
    userActiveSession: map<UserId, set<SessionId>>,
    idleFor: map<SessionId, int>
  )

  datatype Action =
    | Login(uid: UserId, sid: SessionId)
    | Logout(sid: SessionId)
    | LoginFailed(uid: UserId)
    | ExpireSession(sid: SessionId)
    | UnlockAccount(uid: UserId)
    | Tick

  function statusOf(m: map<SessionId, SessionStatus>, sid: SessionId): SessionStatus {
    if sid in m then m[sid] else LoggedOut
  }
  function isActive(s: State, sid: SessionId): bool { statusOf(s.sessions, sid) == Active }
  function isLocked(s: State, uid: UserId): bool {
    if uid in s.accountStatus then s.accountStatus[uid] == Locked else false
  }
  function attemptsOf(s: State, uid: UserId): int {
    if uid in s.failedAttempts then s.failedAttempts[uid] else 0
  }
  function activeSessionsOf(s: State, uid: UserId): set<SessionId> {
    if uid in s.userActiveSession then s.userActiveSession[uid] else {}
  }
  function idleOf(s: State, sid: SessionId): int {
    if sid in s.idleFor then s.idleFor[sid] else 0
  }
  function ownerOrEmpty(s: State, sid: SessionId): UserId {
    if sid in s.sessionOwner then s.sessionOwner[sid] else ""
  }

  function Init(): State {
    State(map[], map[], map[], map[], map[], map[])
  }

  function Apply(a: Action, s: State): Option<State> {
    match a
    case Login(uid, sid) =>
      if !isLocked(s, uid) && sid !in s.sessions && |activeSessionsOf(s, uid)| == 0 then
        Some(s.(sessions := s.sessions[sid := Active],
                sessionOwner := s.sessionOwner[sid := uid],
                failedAttempts := s.failedAttempts[uid := 0],
                userActiveSession := s.userActiveSession[uid := activeSessionsOf(s, uid) + {sid}],
                idleFor := s.idleFor[sid := 0]))
      else None
    case Logout(sid) =>
      if sid in s.sessions && isActive(s, sid) then
        var uid := ownerOrEmpty(s, sid);
        Some(s.(sessions := s.sessions[sid := LoggedOut],
                userActiveSession := s.userActiveSession[uid := activeSessionsOf(s, uid) - {sid}]))
      else None
    case LoginFailed(uid) =>
      if !isLocked(s, uid) then
        var nextCount := attemptsOf(s, uid) + 1;
        var locking := nextCount >= MAX_FAILED_ATTEMPTS;
        var doomed := activeSessionsOf(s, uid);
        Some(s.(sessions := if locking then s.sessions + (map x | x in doomed :: LoggedOut) else s.sessions,
                accountStatus := s.accountStatus[uid := (if locking then Locked else Unlocked)],
                failedAttempts := s.failedAttempts[uid := nextCount],
                userActiveSession := if locking then s.userActiveSession[uid := {}] else s.userActiveSession))
      else None
    case ExpireSession(sid) =>
      if sid in s.sessions && isActive(s, sid) && idleOf(s, sid) > MAX_SESSION_AGE then
        var uid := ownerOrEmpty(s, sid);
        Some(s.(sessions := s.sessions[sid := Expired],
                userActiveSession := s.userActiveSession[uid := activeSessionsOf(s, uid) - {sid}]))
      else None
    case UnlockAccount(uid) =>
      if isLocked(s, uid) then
        Some(s.(accountStatus := s.accountStatus[uid := Unlocked],
                failedAttempts := s.failedAttempts[uid := 0]))
      else None
    case Tick =>
      if exists sid | sid in s.sessions :: isActive(s, sid) then
        Some(s.(idleFor := map sid | sid in s.idleFor :: (if isActive(s, sid) then s.idleFor[sid] + 1 else s.idleFor[sid])))
      else None
  }

  // ---- targets ----
  predicate atMostOneActiveSession(s: State) {
    forall uid | uid in s.userActiveSession.Keys :: |activeSessionsOf(s, uid)| <= 1
  }
  predicate noSessionWhileLocked(s: State) {
    forall uid | uid in s.accountStatus.Keys :: isLocked(s, uid) ==> |activeSessionsOf(s, uid)| == 0
  }
  predicate noAccessWithInvalidToken(s: State) {
    forall sid | sid in s.sessions.Keys ::
      statusOf(s.sessions, sid) != Active ==>
        (forall uid | uid in s.userActiveSession.Keys :: sid !in activeSessionsOf(s, uid))
  }
  predicate noEarlyLockout(s: State) {
    forall uid | uid in s.failedAttempts.Keys :: attemptsOf(s, uid) < MAX_FAILED_ATTEMPTS ==> !isLocked(s, uid)
  }
  predicate noEarlyExpiry(s: State) {
    forall sid | sid in s.sessions.Keys :: statusOf(s.sessions, sid) == Expired ==> idleOf(s, sid) > MAX_SESSION_AGE
  }

  // ---- strengthened invariant ----
  ghost predicate Inv(s: State) {
    atMostOneActiveSession(s) &&
    noSessionWhileLocked(s) &&
    noAccessWithInvalidToken(s) &&
    noEarlyLockout(s) &&
    noEarlyExpiry(s) &&
    // members of an active set are Active sessions owned by that user
    (forall uid, x :: x in activeSessionsOf(s, uid) ==>
        x in s.sessions && s.sessions[x] == Active && x in s.sessionOwner && s.sessionOwner[x] == uid) &&
    // locked accounts have reached the threshold
    (forall uid :: isLocked(s, uid) ==> uid in s.accountStatus && attemptsOf(s, uid) >= MAX_FAILED_ATTEMPTS) &&
    // expired sessions have a recorded idle time above the limit
    (forall sid :: sid in s.sessions && s.sessions[sid] == Expired ==> sid in s.idleFor && s.idleFor[sid] > MAX_SESSION_AGE)
  }

  lemma InitHolds() ensures Inv(Init()) {}

  lemma StepHolds(a: Action, s: State)
    requires Inv(s) && Apply(a, s).Some?
    ensures Inv(Apply(a, s).value)
  {
    var t := Apply(a, s).value;
    match a
    case Login(uid, sid) =>
    case Logout(sid) =>
    case LoginFailed(uid) =>
      var doomed := activeSessionsOf(s, uid);
      forall u2, x | x in activeSessionsOf(t, u2)
        ensures x in t.sessions && t.sessions[x] == Active && x in t.sessionOwner && t.sessionOwner[x] == u2
      {
        if u2 != uid { assert x in activeSessionsOf(s, u2); assert s.sessionOwner[x] == u2; if x in doomed { assert s.sessionOwner[x] == uid; } assert x !in doomed; }
      }
      forall x | x in t.sessions && t.sessions[x] != Active
        ensures forall u2 | u2 in t.userActiveSession.Keys :: x !in activeSessionsOf(t, u2)
      {
        forall u2 | u2 in t.userActiveSession.Keys ensures x !in activeSessionsOf(t, u2) {
          if x in activeSessionsOf(t, u2) {
            assert x in activeSessionsOf(s, u2);
          }
        }
      }
    case ExpireSession(sid) =>
    case UnlockAccount(uid) =>
    case Tick =>
  }

  lemma Implies_atMostOneActiveSession(s: State) requires Inv(s) ensures atMostOneActiveSession(s) {}
  lemma Implies_noSessionWhileLocked(s: State) requires Inv(s) ensures noSessionWhileLocked(s) {}
  lemma Implies_noAccessWithInvalidToken(s: State) requires Inv(s) ensures noAccessWithInvalidToken(s) {}
  lemma Implies_noEarlyLockout(s: State) requires Inv(s) ensures noEarlyLockout(s) {}
  lemma Implies_noEarlyExpiry(s: State) requires Inv(s) ensures noEarlyExpiry(s) {}
}
