import Spec.Check
import Areas.Auth.Proofs.atMostOneActiveSession
import Areas.Auth.Proofs.noSessionWhileLocked
import Areas.Auth.Proofs.noAccessWithInvalidToken
import Areas.Auth.Proofs.noEarlyLockout
import Areas.Auth.Proofs.noEarlyExpiry

/-! The auth area: what is claimed about the model, and the scope it is searched in. -/

namespace Auth
open Spec

def users : List UserId := ["alice", "bob"]
def sids : List SessionId := ["s1", "s2"]

def sessionView (sid : SessionId) (s : State) : String :=
  match s.sessions.get? sid with
  | none => "no session"
  | some .Active => "active"
  | some .Expired => "expired"
  | some .LoggedOut => "logged out"

def accountView (uid : UserId) (s : State) : String :=
  if isLocked s uid then "locked" else "unlocked"

def area : Area State Action where
  name := "auth"
  init := init
  step := apply
  actions :=
    (users.flatMap fun u => sids.map fun sid => Action.login u sid)
      ++ sids.map .logout ++ users.map .login_failed ++ sids.map .expire_session
      ++ users.map .unlock_account ++ [.tick]
  -- the clock is unbounded; the search stops one tick past the timeout
  bound := fun s => s.idleFor.keySet.all fun sid => decide (idleOf s sid ≤ MAX_SESSION_AGE + 1)
  invariants := [
    { id := "INV-001", text := "A user has at most one active session.",
      holds := atMostOneActiveSession,
      proof := some ⟨Proofs.atMostOneActiveSession.atMostOneActiveSession_holds⟩ },
    { id := "INV-002", text := "A locked account has no active session.",
      holds := noSessionWhileLocked,
      proof := some ⟨Proofs.noSessionWhileLocked.noSessionWhileLocked_holds⟩ },
    { id := "INV-003", text := "An expired or logged-out session is never in a user's active set.",
      holds := noAccessWithInvalidToken,
      proof := some ⟨Proofs.noAccessWithInvalidToken.noAccessWithInvalidToken_holds⟩ },
    { id := "INV-004", text := "Below five failed attempts the account stays unlocked.",
      holds := noEarlyLockout,
      proof := some ⟨Proofs.noEarlyLockout.noEarlyLockout_holds⟩ },
    { id := "INV-005", text := "No session expires before it has been idle longer than the timeout.",
      holds := noEarlyExpiry,
      proof := some ⟨Proofs.noEarlyExpiry.noEarlyExpiry_holds⟩ }]
  requirements := [
    { id := "REQ-001",
      text := "When a user who is not locked and has no active session signs in with a fresh session id, the session becomes active, belongs to the user, and the failed-attempt count resets.",
      when := fun s a => match a with
        | .login u sid => !isLocked s u && !s.sessions.contains sid && (activeSessionsOf s u).size == 0
        | _ => false,
      shall := fun _ a t => match a with
        | .login u sid => isActive t sid && t.sessionOwner.get sid == u
            && (activeSessionsOf t u).contains sid && attemptsOf t u == 0
        | _ => true },
    { id := "REQ-005",
      text := "While the account is locked, sign-in is refused.",
      when := fun s a => match a with
        | .login u _ => isLocked s u
        | _ => false,
      refused := true },
    { id := "REQ-002",
      text := "When the user logs out of an active session, the session is logged out.",
      when := fun s a => match a with
        | .logout sid => isActive s sid
        | _ => false,
      shall := fun _ a t => match a with
        | .logout sid => statusOf t.sessions sid == .LoggedOut
        | _ => true },
    { id := "REQ-003",
      text := "When a sign-in attempt fails on an unlocked account, the failed-attempt count goes up by one, and the account is locked exactly when the count reaches five.",
      when := fun s a => match a with
        | .login_failed u => !isLocked s u
        | _ => false,
      shall := fun s a t => match a with
        | .login_failed u => attemptsOf t u == attemptsOf s u + 1
            && isLocked t u == decide (attemptsOf t u ≥ MAX_FAILED_ATTEMPTS)
        | _ => true },
    { id := "REQ-006",
      text := "When a failed sign-in locks the account, every active session of that user ends.",
      when := fun s a => match a with
        | .login_failed u => !isLocked s u
        | _ => false,
      shall := fun _ a t => match a with
        | .login_failed u => !isLocked t u
            || t.sessions.keySet.all fun sid => t.sessionOwner.get sid != u || !isActive t sid
        | _ => true },
    { id := "REQ-004",
      text := "When an active session has been idle longer than the timeout and is expired, it becomes expired.",
      when := fun s a => match a with
        | .expire_session sid => isActive s sid && decide (idleOf s sid > MAX_SESSION_AGE)
        | _ => false,
      shall := fun _ a t => match a with
        | .expire_session sid => statusOf t.sessions sid == .Expired
        | _ => true }]
  examples := [
    { id := "EX-001", text := "Sign in",
      run := [.login "alice" "s1"],
      expect := fun s => isActive s "s1" && activeSessionsOf s "alice" == FSet.ofList ["s1"] },
    { id := "EX-002", text := "Sign in, then out",
      run := [.login "alice" "s1", .logout "s1"],
      expect := fun s => statusOf s.sessions "s1" == .LoggedOut },
    { id := "EX-003", text := "The fifth failure locks the account",
      run := List.replicate 5 (.login_failed "bob"),
      expect := fun s => isLocked s "bob" },
    { id := "EX-004", text := "Four failures do not lock it",
      run := List.replicate 4 (.login_failed "bob"),
      expect := fun s => !isLocked s "bob" },
    { id := "EX-005", text := "A session idle for four ticks expires",
      run := [.login "alice" "s1", .tick, .tick, .tick, .tick, .expire_session "s1"],
      expect := fun s => statusOf s.sessions "s1" == .Expired },
    { id := "EX-007", text := "Three idle ticks are not enough to expire",
      run := [.login "alice" "s1", .tick, .tick, .tick],
      expect := fun s => isActive s "s1" && idleOf s "s1" == 3,
      refused := some (.expire_session "s1") },
    { id := "EX-006", text := "A locked account cannot sign in",
      run := List.replicate 5 (.login_failed "bob"),
      expect := fun s => isLocked s "bob" && attemptsOf s "bob" == 5,
      refused := some (.login "bob" "s2") },
    { id := "EX-008", text := "The lockout ends a signed-in user's session",
      run := .login "alice" "s1" :: List.replicate 5 (.login_failed "alice"),
      expect := fun s => isLocked s "alice" && !isActive s "s1" }]
  views := [("session s1", sessionView "s1"), ("account alice", accountView "alice")]

def entry : Entry := area.entry

end Auth
