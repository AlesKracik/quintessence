import Spec.Data

/-! The subscription model: state, actions, one step, and the invariant predicates. -/

namespace Subscription
open Spec
-- an action's parameter its guard and effect never read is still its own
set_option linter.unusedVariables false

inductive BillingResult where
  | Success
  | Declined
  | Timeout
  | NetworkError
  deriving DecidableEq, Repr, Inhabited, Hashable
instance : LT BillingResult := ⟨fun a b => a.toCtorIdx < b.toCtorIdx⟩
instance : DecidableRel (α := BillingResult) (· < ·) :=
  fun a b => inferInstanceAs (Decidable (a.toCtorIdx < b.toCtorIdx))

abbrev CustomerId := String

inductive Notice where
  | NoNotice
  | Email
  | InApp
  deriving DecidableEq, Repr, Inhabited, Hashable
instance : LT Notice := ⟨fun a b => a.toCtorIdx < b.toCtorIdx⟩
instance : DecidableRel (α := Notice) (· < ·) :=
  fun a b => inferInstanceAs (Decidable (a.toCtorIdx < b.toCtorIdx))

inductive SubStatus where
  | Active
  | Cancelled
  | Expired
  deriving DecidableEq, Repr, Inhabited, Hashable
instance : LT SubStatus := ⟨fun a b => a.toCtorIdx < b.toCtorIdx⟩
instance : DecidableRel (α := SubStatus) (· < ·) :=
  fun a b => inferInstanceAs (Decidable (a.toCtorIdx < b.toCtorIdx))

def CUSTOMERS : (FSet String) :=
  (FSet.ofList ["alice", "bob"] : (FSet String))

def MAX_CANCEL_RETRIES : Int :=
  (3 : Int)

structure State where
  accessUntilPeriodEnd : (FMap CustomerId Bool)
  billingEnabled : (FMap CustomerId Bool)
  lastBillingResult : (FMap CustomerId BillingResult)
  notice : (FMap CustomerId Notice)
  status : (FMap CustomerId SubStatus)
  unansweredTries : (FMap CustomerId Int)
  deriving DecidableEq, Repr, Hashable

def noBillingAfterCancel (s : State) : Bool :=
  ((s.status.keySet).all (fun c => (!(((s.status.get c) == SubStatus.Cancelled) || ((s.status.get c) == SubStatus.Expired)) || (!(s.billingEnabled.get c)))))

def noRefundIssued (s : State) : Bool :=
  ((s.status.keySet).all (fun c => ((s.accessUntilPeriodEnd.get c) || ((s.status.get c) != SubStatus.Cancelled))))

def triesOf (s : State) (c : CustomerId) : Int :=
  (if ((s.unansweredTries.keySet).contains c) then (s.unansweredTries.get c) else (0 : Int))

def retriesWithinBudget (s : State) : Bool :=
  ((s.status.keySet).all (fun c => (decide ((triesOf s c) ≤ MAX_CANCEL_RETRIES))))

def unacceptedCancelLeavesActive (s : State) : Bool :=
  ((s.status.keySet).all (fun c => (!((s.lastBillingResult.get c) != BillingResult.Success) || (((s.status.get c) == SubStatus.Active) && (s.billingEnabled.get c)))))

inductive Action where
  | cancel_again (c : CustomerId)
  | cancel_declined (c : CustomerId)
  | cancel_subscription (c : CustomerId) (n : Notice)
  | cancel_unanswered (c : CustomerId) (timedOut : Bool)
  | expire_period (c : CustomerId)
  | request_refund (c : CustomerId)
  deriving Repr

def init : State :=
  { accessUntilPeriodEnd := (FSet.mapBy CUSTOMERS (fun _ => false)),
    billingEnabled := (FSet.mapBy CUSTOMERS (fun _ => true)),
    lastBillingResult := (FSet.mapBy CUSTOMERS (fun _ => BillingResult.Success)),
    notice := (FSet.mapBy CUSTOMERS (fun _ => Notice.NoNotice)),
    status := (FSet.mapBy CUSTOMERS (fun _ => SubStatus.Active)),
    unansweredTries := (FSet.mapBy CUSTOMERS (fun _ => (0 : Int))) }

/-- One step. `none` = the action's guard is false. -/
def apply : Action → State → Option State
  | .cancel_again c, s =>
    if ((s.status.get c) == SubStatus.Cancelled) then
      some s
    else none
  | .cancel_declined c, s =>
    if ((s.status.get c) == SubStatus.Active) then
      some { s with
        lastBillingResult := (s.lastBillingResult.set c BillingResult.Declined)
        unansweredTries := (s.unansweredTries.set c (0 : Int)) }
    else none
  | .cancel_subscription c n, s =>
    if ((s.status.get c) == SubStatus.Active) &&
      (n != Notice.NoNotice) then
      some { s with
        accessUntilPeriodEnd := (s.accessUntilPeriodEnd.set c true)
        billingEnabled := (s.billingEnabled.set c false)
        lastBillingResult := (s.lastBillingResult.set c BillingResult.Success)
        notice := (s.notice.set c n)
        status := (s.status.set c SubStatus.Cancelled)
        unansweredTries := (s.unansweredTries.set c (0 : Int)) }
    else none
  | .cancel_unanswered c timedOut, s =>
    if ((s.status.get c) == SubStatus.Active) &&
      (decide ((triesOf s c) < MAX_CANCEL_RETRIES)) then
      some { s with
        lastBillingResult := (s.lastBillingResult.set c (if timedOut then BillingResult.Timeout else BillingResult.NetworkError))
        unansweredTries := (s.unansweredTries.set c ((triesOf s c) + (1 : Int))) }
    else none
  | .expire_period c, s =>
    if ((s.status.get c) == SubStatus.Cancelled) then
      some { s with
        accessUntilPeriodEnd := (s.accessUntilPeriodEnd.set c false)
        status := (s.status.set c SubStatus.Expired) }
    else none
  | .request_refund c, s =>
    if false then
      some s
    else none

end Subscription
