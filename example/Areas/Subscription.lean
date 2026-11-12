import Spec.Check
import Areas.Subscription.Proofs.noBillingAfterCancel
import Areas.Subscription.Proofs.noRefundIssued
import Areas.Subscription.Proofs.retriesWithinBudget
import Areas.Subscription.Proofs.unacceptedCancelLeavesActive

/-! The subscription area: what is claimed about the model, and the scope it is searched in. -/

namespace Subscription
open Spec

def customers : List CustomerId := ["alice", "bob"]

def statusView (c : CustomerId) (s : State) : String :=
  match s.status.get c with
  | .Active => "active"
  | .Cancelled => "cancelled, access until period end"
  | .Expired => "expired"

def retryView (c : CustomerId) (s : State) : String :=
  s!"{triesOf s c} unanswered tries"

def area : Area State Action where
  name := "subscription"
  init := init
  step := apply
  actions :=
    (customers.flatMap fun c =>
      [.cancel_subscription c .Email, .cancel_subscription c .InApp,
       .cancel_declined c, .cancel_unanswered c true, .cancel_unanswered c false,
       .cancel_again c, .expire_period c, .request_refund c])
  invariants := [
    { id := "INV-001",
      sources := ["the earlier Quint specification of this example"], text := "A cancelled or expired subscription is never billed again.",
      holds := noBillingAfterCancel,
      proof := some ⟨Proofs.noBillingAfterCancel.noBillingAfterCancel_holds⟩ },
    { id := "INV-002",
      sources := ["the earlier Quint specification of this example"], text := "A cancelled subscriber keeps access until the paid period ends.",
      holds := noRefundIssued,
      proof := some ⟨Proofs.noRefundIssued.noRefundIssued_holds⟩ },
    { id := "INV-003",
      sources := ["the earlier Quint specification of this example"], text := "A cancellation the provider did not accept leaves the subscription Active and billable.",
      holds := unacceptedCancelLeavesActive,
      proof := some ⟨Proofs.unacceptedCancelLeavesActive.unacceptedCancelLeavesActive_holds⟩ },
    { id := "INV-004",
      sources := ["the earlier Quint specification of this example"], text := "Unanswered cancellation tries never exceed the retry budget of three.",
      holds := retriesWithinBudget,
      proof := some ⟨Proofs.retriesWithinBudget.retriesWithinBudget_holds⟩ }]
  requirements := [
    { id := "REQ-001",
      sources := ["the earlier Quint specification of this example"],
      text := "When a customer cancels an active subscription and the provider accepts, future billing stops.",
      when := fun s a => match a with
        | .cancel_subscription c n => s.status.get c == .Active && n != .NoNotice
        | _ => false,
      shall := fun _ a t => match a with
        | .cancel_subscription c _ => t.status.get c == .Cancelled && !t.billingEnabled.get c
            && t.lastBillingResult.get c == .Success
        | _ => true },
    { id := "REQ-002",
      sources := ["the earlier Quint specification of this example"],
      text := "When a cancellation succeeds, access stays available until the paid period ends.",
      when := fun s a => match a with
        | .cancel_subscription c n => s.status.get c == .Active && n != .NoNotice
        | _ => false,
      shall := fun _ a t => match a with
        | .cancel_subscription c _ => t.status.get c == .Cancelled && t.accessUntilPeriodEnd.get c
        | _ => true },
    { id := "REQ-007",
      sources := ["the earlier Quint specification of this example"],
      text := "When a cancellation succeeds, the customer is told by email or by an in-app notice.",
      when := fun s a => match a with
        | .cancel_subscription c n => s.status.get c == .Active && n != .NoNotice
            && s.notice.get c == .NoNotice
        | _ => false,
      shall := fun _ a t => match a with
        | .cancel_subscription c _ => t.notice.get c == .Email || t.notice.get c == .InApp
        | _ => true },
    { id := "REQ-008",
      sources := ["the earlier Quint specification of this example"],
      text := "If the billing provider declines a cancellation, the subscription stays active and billable.",
      when := fun s a => match a with
        | .cancel_declined c => s.status.get c == .Active
        | _ => false,
      shall := fun _ a t => match a with
        | .cancel_declined c => t.lastBillingResult.get c == .Declined && t.status.get c == .Active
            && t.billingEnabled.get c
        | _ => true },
    { id := "REQ-004",
      sources := ["the earlier Quint specification of this example"],
      text := "If the billing provider does not answer a cancellation, the subscription stays active and billable.",
      when := fun s a => match a with
        | .cancel_unanswered c _ => s.status.get c == .Active && decide (triesOf s c < MAX_CANCEL_RETRIES)
        | _ => false,
      shall := fun _ a t => match a with
        | .cancel_unanswered c _ =>
            (t.lastBillingResult.get c == .Timeout || t.lastBillingResult.get c == .NetworkError)
              && t.status.get c == .Active && t.billingEnabled.get c
        | _ => true },
    { id := "REQ-003",
      sources := ["the earlier Quint specification of this example"],
      text := "When a customer cancels an already cancelled subscription, nothing changes.",
      when := fun s a => match a with
        | .cancel_again c => s.status.get c == .Cancelled
        | _ => false,
      shall := fun s a t => match a with
        | .cancel_again c => t.status.get c == .Cancelled && t.accessUntilPeriodEnd.get c
            && t.billingEnabled.get c == s.billingEnabled.get c && t.notice.get c == s.notice.get c
        | _ => true },
    { id := "REQ-005",
      sources := ["the earlier Quint specification of this example"],
      text := "When the paid period of a cancelled subscription ends, access is withdrawn.",
      when := fun s a => match a with
        | .expire_period c => s.status.get c == .Cancelled
        | _ => false,
      shall := fun _ a t => match a with
        | .expire_period c => t.status.get c == .Expired && !t.accessUntilPeriodEnd.get c
        | _ => true },
    { id := "REQ-006",
      sources := ["the earlier Quint specification of this example"],
      text := "A refund for the unused part of a paid period is never issued.",
      when := fun _ a => match a with
        | .request_refund _ => true
        | _ => false,
      refused := true }]
  examples := [
    { id := "EX-001", text := "Cancel mid-period: billing stops, access stays",
      run := [.cancel_subscription "alice" .Email],
      expect := fun s => s.status.get "alice" == .Cancelled && !s.billingEnabled.get "alice"
        && s.accessUntilPeriodEnd.get "alice" && s.notice.get "alice" == .Email },
    { id := "EX-002", text := "Cancelling twice is harmless",
      run := [.cancel_subscription "alice" .Email, .cancel_again "alice"],
      expect := fun s => s.status.get "alice" == .Cancelled && s.accessUntilPeriodEnd.get "alice" },
    { id := "EX-003", text := "Provider times out: nothing moves",
      run := [.cancel_unanswered "alice" true],
      expect := fun s => s.status.get "alice" == .Active && s.billingEnabled.get "alice" },
    { id := "EX-004", text := "Provider declines: the subscription stays Active",
      run := [.cancel_declined "alice"],
      expect := fun s => s.status.get "alice" == .Active
        && s.lastBillingResult.get "alice" == .Declined },
    { id := "EX-005", text := "After three unanswered tries there is no fourth",
      run := [.cancel_unanswered "alice" true, .cancel_unanswered "alice" false,
              .cancel_unanswered "alice" true],
      expect := fun s => s.status.get "alice" == .Active && triesOf s "alice" == 3,
      refused := some (.cancel_unanswered "alice" true) },
    { id := "EX-006", text := "A refund request after cancelling is refused",
      run := [.cancel_subscription "alice" .InApp],
      expect := fun s => s.status.get "alice" == .Cancelled && s.accessUntilPeriodEnd.get "alice",
      refused := some (.request_refund "alice") },
    { id := "EX-007", text := "The paid period ends: access stops",
      run := [.cancel_subscription "alice" .InApp, .expire_period "alice"],
      expect := fun s => s.status.get "alice" == .Expired && !s.accessUntilPeriodEnd.get "alice" },
    { id := "EX-008", text := "A decline after an unanswered try resets the retry count",
      run := [.cancel_unanswered "alice" false, .cancel_declined "alice"],
      expect := fun s => triesOf s "alice" == 0 },
    { id := "EX-009", text := "A successful cancel after an unanswered try resets the retry count",
      run := [.cancel_unanswered "alice" true, .cancel_subscription "alice" .InApp],
      expect := fun s => triesOf s "alice" == 0 },
    { id := "EX-010", text := "Cancel journey: retry succeeds, repeat is harmless, period ends, refund refused",
      run := [.cancel_unanswered "alice" true, .cancel_subscription "alice" .Email,
              .cancel_again "alice", .expire_period "alice"],
      expect := fun s => s.status.get "alice" == .Expired && !s.billingEnabled.get "alice",
      refused := some (.request_refund "alice") },
    { id := "EX-011", text := "Declined journey: the provider declines, the retry goes through",
      run := [.cancel_declined "alice", .cancel_subscription "alice" .InApp],
      expect := fun s => s.status.get "alice" == .Cancelled && !s.billingEnabled.get "alice" }]
  journeys := [
    { id := "J-001",
      sources := ["written for this example"], title := "Cancelling a subscription",
      goal := "Alice no longer wants to pay, but has paid for the current period.",
      moments := [
        .step "Alice cancels; the provider accepts and she gets an email." (.cancel_subscription "alice" .Email),
        .step "She presses cancel again, and nothing changes." (.cancel_again "alice"),
        .refusal "She asks for a refund of the unused part of the period and is turned down." (.request_refund "alice"),
        .step "The paid period ends and her access ends with it." (.expire_period "alice")] },
    { id := "J-002",
      sources := ["written for this example"], title := "The provider does not cooperate",
      goal := "Bob wants to cancel while the billing provider is having a bad day.",
      moments := [
        .step "Bob cancels, but the provider does not answer in time." (.cancel_unanswered "bob" true),
        .step "A second try gets no answer either." (.cancel_unanswered "bob" false),
        .step "A third try is declined by the provider. Bob is still subscribed and still billed." (.cancel_declined "bob"),
        .step "The next try goes through, and Bob is told in the app." (.cancel_subscription "bob" .InApp)] }]
  views := customers.map (fun c => (s!"subscription of {c}", statusView c))
    ++ customers.map (fun c => (s!"cancellation tries of {c}", retryView c))
  assumptions := [
    "Two customers (alice, bob) stand for any number; each already has an active subscription.",
    "The billing provider is outside the model: its answer to a cancellation (accepted, declined, no answer) arrives as part of the action.",
    "Refunds are never issued in this area; asking for one is always turned down."]
  code := some {
    adapter := ["python3", "adapters/kotlin.py", "app/kotlin"]
    paths := ["app/kotlin"]
    setup := "import billing.*
import java.io.IOException
class ScriptedProvider : BillingProvider {
    var answer: () -> ProviderAnswer = { error(\"the provider must not be called\") }
    override fun cancelSubscription(customerId: String): ProviderAnswer = answer()
    fun accepts() { answer = { ProviderAnswer.Accepted } }
    fun declines() { answer = { ProviderAnswer.Declined } }
    fun staysSilent(timedOut: Boolean) { answer = { if (timedOut) throw ProviderTimeoutException() else throw IOException() } }
    fun mustNotBeCalled() { answer = { error(\"the provider must not be called\") } }
}
val provider = ScriptedProvider()
val system = SubscriptionService(provider).also { it.subscribe(\"alice\"); it.subscribe(\"bob\") }"
    actions := [
      ("cancel_subscription", "provider.accepts(); if (system.cancel({0}, Notice.valueOf({1})) != CancelOutcome.Cancelled) refuse()"),
      ("cancel_declined", "provider.declines(); if (system.cancel({0}, Notice.Email) != CancelOutcome.Declined) refuse()"),
      ("cancel_unanswered", "provider.staysSilent({1}); if (!system.cancel({0}, Notice.Email).unanswered) refuse()"),
      ("cancel_again", "provider.mustNotBeCalled(); if (system.cancel({0}, Notice.Email) != CancelOutcome.AlreadyCancelled) refuse()"),
      ("expire_period", "system.expirePeriod({0})"),
      ("request_refund", "system.requestRefund({0})")]
    observe := customers.flatMap (fun c => [
      (s!"status {c}", fun s => match s.status.get c with
          | .Active => "Active" | .Cancelled => "Cancelled" | .Expired => "Expired",
        s!"system.statusOf(\"{c}\")"),
      (s!"billed {c}", fun s => toString (s.billingEnabled.get c), s!"system.isBilled(\"{c}\")"),
      (s!"access until period end {c}", fun s => toString (s.accessUntilPeriodEnd.get c),
        s!"system.hasAccessUntilPeriodEnd(\"{c}\")"),
      (s!"notice {c}", fun s => match s.notice.get c with
          | .NoNotice => "none" | .Email => "Email" | .InApp => "InApp",
        s!"system.noticeSentTo(\"{c}\")"),
      (s!"unanswered tries {c}", fun s => toString (triesOf s c), s!"system.unansweredTries(\"{c}\")")])
    budget := 100000 }

def entry : Entry := area.entry

end Subscription
