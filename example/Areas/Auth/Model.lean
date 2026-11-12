import Spec.Data

/-! The auth model: state, actions, one step, and the invariant predicates. -/

namespace Auth
open Spec
-- an action's parameter its guard and effect never read is still its own
set_option linter.unusedVariables false

inductive AccountStatus where
  | Unlocked
  | Locked
  deriving DecidableEq, Repr, Inhabited, Hashable
instance : LT AccountStatus := ⟨fun a b => a.toCtorIdx < b.toCtorIdx⟩
instance : DecidableRel (α := AccountStatus) (· < ·) :=
  fun a b => inferInstanceAs (Decidable (a.toCtorIdx < b.toCtorIdx))

abbrev SessionId := String

inductive SessionStatus where
  | Active
  | Expired
  | LoggedOut
  deriving DecidableEq, Repr, Inhabited, Hashable
instance : LT SessionStatus := ⟨fun a b => a.toCtorIdx < b.toCtorIdx⟩
instance : DecidableRel (α := SessionStatus) (· < ·) :=
  fun a b => inferInstanceAs (Decidable (a.toCtorIdx < b.toCtorIdx))

abbrev UserId := String

def MAX_FAILED_ATTEMPTS : Int :=
  (5 : Int)

def MAX_SESSION_AGE : Int :=
  (3 : Int)

def statusOf (m : (FMap SessionId SessionStatus)) (sid : SessionId) : SessionStatus :=
  (if ((m.keySet).contains sid) then (m.get sid) else SessionStatus.LoggedOut)

structure State where
  accountStatus : (FMap UserId AccountStatus)
  failedAttempts : (FMap UserId Int)
  idleFor : (FMap SessionId Int)
  sessionOwner : (FMap SessionId UserId)
  sessions : (FMap SessionId SessionStatus)
  userActiveSession : (FMap UserId (FSet SessionId))
  deriving DecidableEq, Repr, Hashable

def activeSessionsOf (s : State) (uid : UserId) : (FSet SessionId) :=
  (if ((s.userActiveSession.keySet).contains uid) then (s.userActiveSession.get uid) else (FSet.empty : (FSet SessionId)))

def atMostOneActiveSession (s : State) : Bool :=
  ((s.userActiveSession.keySet).all (fun uid => (decide (((activeSessionsOf s uid).size) ≤ (1 : Int)))))

def attemptsOf (s : State) (uid : UserId) : Int :=
  (if ((s.failedAttempts.keySet).contains uid) then (s.failedAttempts.get uid) else (0 : Int))

def idleOf (s : State) (sid : SessionId) : Int :=
  (if ((s.idleFor.keySet).contains sid) then (s.idleFor.get sid) else (0 : Int))

def isActive (s : State) (sid : SessionId) : Bool :=
  ((statusOf s.sessions sid) == SessionStatus.Active)

def isLocked (s : State) (uid : UserId) : Bool :=
  (if ((s.accountStatus.keySet).contains uid) then ((s.accountStatus.get uid) == AccountStatus.Locked) else false)

def noAccessWithInvalidToken (s : State) : Bool :=
  ((s.sessions.keySet).all (fun sid => (!((statusOf s.sessions sid) != SessionStatus.Active) || ((s.userActiveSession.keySet).all (fun uid => (!((activeSessionsOf s uid).contains sid)))))))

def noEarlyExpiry (s : State) : Bool :=
  ((s.sessions.keySet).all (fun sid => (!((statusOf s.sessions sid) == SessionStatus.Expired) || (decide ((idleOf s sid) > MAX_SESSION_AGE)))))

def noEarlyLockout (s : State) : Bool :=
  ((s.failedAttempts.keySet).all (fun uid => (!(decide ((attemptsOf s uid) < MAX_FAILED_ATTEMPTS)) || (!(isLocked s uid)))))

def noSessionWhileLocked (s : State) : Bool :=
  ((s.accountStatus.keySet).all (fun uid => (!(isLocked s uid) || (((activeSessionsOf s uid).size) == (0 : Int)))))

inductive Action where
  | expire_session (sid : SessionId)
  | login (uid : UserId) (sid : SessionId)
  | login_failed (uid : UserId)
  | logout (sid : SessionId)
  | tick
  | unlock_account (uid : UserId)
  deriving Repr

def init : State :=
  { accountStatus := (FMap.empty : (FMap UserId AccountStatus)),
    failedAttempts := (FMap.empty : (FMap UserId Int)),
    idleFor := (FMap.empty : (FMap SessionId Int)),
    sessionOwner := (FMap.empty : (FMap SessionId UserId)),
    sessions := (FMap.empty : (FMap SessionId SessionStatus)),
    userActiveSession := (FMap.empty : (FMap UserId (FSet SessionId))) }

/-- One step. `none` = the action's guard is false. -/
def apply : Action → State → Option State
  | .expire_session sid, s =>
    if ((s.sessions.keySet).contains sid) &&
      (isActive s sid) &&
      (decide ((idleOf s sid) > MAX_SESSION_AGE)) then
      some { s with
        sessions := (s.sessions.set sid SessionStatus.Expired)
        userActiveSession := (s.userActiveSession.set (if ((s.sessionOwner.keySet).contains sid) then (s.sessionOwner.get sid) else "") ((activeSessionsOf s (if ((s.sessionOwner.keySet).contains sid) then (s.sessionOwner.get sid) else "")).diff (FSet.ofList [sid] : (FSet SessionId)))) }
    else none
  | .login uid sid, s =>
    if (!(isLocked s uid)) &&
      (!((s.sessions.keySet).contains sid)) &&
      (((activeSessionsOf s uid).size) == (0 : Int)) then
      some { s with
        failedAttempts := (s.failedAttempts.set uid (0 : Int))
        idleFor := (s.idleFor.set sid (0 : Int))
        sessionOwner := (s.sessionOwner.set sid uid)
        sessions := (s.sessions.set sid SessionStatus.Active)
        userActiveSession := (s.userActiveSession.set uid ((activeSessionsOf s uid).union (FSet.ofList [sid] : (FSet SessionId)))) }
    else none
  | .login_failed uid, s =>
    if (!(isLocked s uid)) then
      some { s with
        accountStatus := (s.accountStatus.set uid (if (decide (((attemptsOf s uid) + (1 : Int)) ≥ MAX_FAILED_ATTEMPTS)) then AccountStatus.Locked else AccountStatus.Unlocked))
        failedAttempts := (s.failedAttempts.set uid ((attemptsOf s uid) + (1 : Int)))
        sessions := (if (decide (((attemptsOf s uid) + (1 : Int)) ≥ MAX_FAILED_ATTEMPTS)) then ((activeSessionsOf s uid).fold s.sessions (fun acc s_ => (acc.set s_ SessionStatus.LoggedOut))) else s.sessions)
        userActiveSession := (if (decide (((attemptsOf s uid) + (1 : Int)) ≥ MAX_FAILED_ATTEMPTS)) then (s.userActiveSession.set uid (FSet.empty : (FSet SessionId))) else s.userActiveSession) }
    else none
  | .logout sid, s =>
    if ((s.sessions.keySet).contains sid) &&
      (isActive s sid) then
      some { s with
        sessions := (s.sessions.set sid SessionStatus.LoggedOut)
        userActiveSession := (s.userActiveSession.set (if ((s.sessionOwner.keySet).contains sid) then (s.sessionOwner.get sid) else "") ((activeSessionsOf s (if ((s.sessionOwner.keySet).contains sid) then (s.sessionOwner.get sid) else "")).diff (FSet.ofList [sid] : (FSet SessionId)))) }
    else none
  -- time passes whether or not anyone is signed in
  | .tick, s =>
    some { s with
      idleFor := (FSet.mapBy (s.idleFor.keySet) (fun sid => (if (isActive s sid) then ((s.idleFor.get sid) + (1 : Int)) else (s.idleFor.get sid)))) }
  | .unlock_account uid, s =>
    if (isLocked s uid) then
      some { s with
        accountStatus := (s.accountStatus.set uid AccountStatus.Unlocked)
        failedAttempts := (s.failedAttempts.set uid (0 : Int)) }
    else none

end Auth
