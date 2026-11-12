import Spec.Auth.Model
import Std.Data.HashSet

/-!
Exhaustive search of the auth model within a finite scope, as compiled Lean.

A measurement, not a verdict tool: how fast can the model the proofs are
about be explored by running it? Scope: three users, three sessions, the
clock capped one tick above the timeout (the same cap the TLC baseline in
RESULTS.md used). Visited states are remembered by 64-bit fingerprint, as
TLC does.

    lake build authsearch && .lake/build/bin/authsearch
-/

open Quint Spec.Auth

deriving instance Hashable for AccountStatus
deriving instance Hashable for SessionStatus
instance {α β : Type} [Hashable α] [Hashable β] : Hashable (QMap α β) := ⟨fun m => hash m.entries⟩
instance {α : Type} [Hashable α] : Hashable (QSet α) := ⟨fun s => hash s.elems⟩
deriving instance Hashable for State

def users : List String := ["alice", "bob", "charlie"]
def sids : List String := ["s1", "s2", "s3"]

/-- Every action the scope allows: what the Quint model's `step` could pick. -/
def actions : Array Action :=
  ((users.flatMap fun u => sids.map fun s => Action.login u s)
    ++ sids.map Action.logout
    ++ users.map Action.login_failed
    ++ sids.map Action.expire_session
    ++ users.map Action.unlock_account
    ++ [Action.tick]).toArray

def clockCap : Int := MAX_SESSION_AGE + 1

/-- The check's own bound on the unbounded clock; the model is untouched. -/
def clamp (s : State) : State :=
  { s with idleFor := ⟨s.idleFor.entries.map fun (k, v) => (k, if v > clockCap then clockCap else v)⟩ }

def step (a : Action) (s : State) : Option State := (apply a s).map clamp

def allInvariants (s : State) : Bool :=
  atMostOneActiveSession s && noSessionWhileLocked s && noAccessWithInvalidToken s
    && noEarlyLockout s && noEarlyExpiry s

def search (limit : Nat) : IO Unit := do
  let start ← IO.monoMsNow
  let mut seen : Std.HashSet UInt64 := {}
  let mut stack : Array State := #[init]
  seen := seen.insert (hash init)
  let mut explored := 0
  let mut generated := 0
  let mut violations := 0
  while explored < limit do
    match stack.back? with
    | none => break
    | some s =>
      stack := stack.pop
      explored := explored + 1
      if !(allInvariants s) then violations := violations + 1
      for a in actions do
        match step a s with
        | none => pure ()
        | some t =>
          generated := generated + 1
          let h := hash t
          if !seen.contains h then
            seen := seen.insert h
            stack := stack.push t
  let ms := (← IO.monoMsNow) - start
  let done := if stack.isEmpty then "complete" else "stopped at limit"
  IO.println s!"{done}: distinct {seen.size}, explored {explored}, generated {generated}, violations {violations}, {ms} ms"

def main (args : List String) : IO Unit :=
  search ((args.head? >>= String.toNat?).getD 1000000000)
