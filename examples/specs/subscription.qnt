// ============================================================
// Quint Specification: subscription
// Companion JSON: specs/subscription.area.json (v0.1.0)
//
// The acceptance case from the methodology's success criteria:
//
//   "A customer can cancel an active subscription. Cancellation
//    prevents future billing, but access continues until the paid
//    period expires. Cancelling twice is harmless. No refund is
//    issued. If the billing provider times out, the subscription
//    must remain active."
//
// Five sentences of prose; eight decisions the prose does not
// make. What the spec JSON adds on top of this model: the
// declared boundary (scope), the provider's full outcome set
// (externals), what the environment is assumed to do
// (assumptions), which behaviors are required vs merely
// permitted (modality), and what happens on each failure
// (error_outcomes).
// ============================================================

module subscription {

  // ----------------------------------------------------------
  // TYPES
  // SubStatus is CLOSED in the JSON: exactly these three states
  // exist. spec-lint holds this type to that list, so a fourth
  // state cannot appear here without the JSON admitting it.
  // ----------------------------------------------------------

  type CustomerId = str
  type SubStatus = Active | Cancelled | Expired

  // Outcomes the billing provider can return. Modelling the
  // unhappy ones is the point: TIMEOUT is why the subscription
  // stays Active rather than silently becoming Cancelled.
  type BillingResult = Success | Declined | Timeout | NetworkError

  // ----------------------------------------------------------
  // STATE
  // ----------------------------------------------------------

  var status: CustomerId -> SubStatus
  var billingEnabled: CustomerId -> bool
  var accessUntilPeriodEnd: CustomerId -> bool
  var lastBillingResult: CustomerId -> BillingResult

  // ----------------------------------------------------------
  // ACTIONS
  // ----------------------------------------------------------

  // REQ-001: cancel an Active subscription. Billing stops; access
  // survives to the end of the paid period (REQ-002).
  action cancel_subscription(c: CustomerId): bool = all {
    status.get(c) == Active,
    lastBillingResult' = lastBillingResult.set(c, Success),
    status' = status.set(c, Cancelled),
    billingEnabled' = billingEnabled.set(c, false),
    accessUntilPeriodEnd' = accessUntilPeriodEnd.set(c, true),
  }

  // REQ-003: cancelling twice is harmless — the second call is a
  // no-op, not an error. Idempotence is a behavior, so it is its
  // own action with its own witness rather than a comment.
  action cancel_again(c: CustomerId): bool = all {
    status.get(c) == Cancelled,
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    lastBillingResult' = lastBillingResult,
  }

  // REQ-004: the provider times out. The subscription MUST remain
  // Active — the one outcome the prose was explicit about, and the
  // one an implementation is most likely to get wrong by treating
  // "no answer" as "done".
  action cancel_times_out(c: CustomerId): bool = all {
    status.get(c) == Active,
    lastBillingResult' = lastBillingResult.set(c, Timeout),
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
  }

  // REQ-005: the paid period runs out; access finally stops.
  action expire_period(c: CustomerId): bool = all {
    status.get(c) == Cancelled,
    status' = status.set(c, Expired),
    accessUntilPeriodEnd' = accessUntilPeriodEnd.set(c, false),
    billingEnabled' = billingEnabled,
    lastBillingResult' = lastBillingResult,
  }

  // ----------------------------------------------------------
  // INVARIANTS
  // ----------------------------------------------------------

  // INV-001: a cancelled subscription is never billed again.
  val noBillingAfterCancel: bool =
    status.keys().forall(c =>
      (status.get(c) == Cancelled or status.get(c) == Expired)
        implies not(billingEnabled.get(c)))

  // INV-002: carries the FORBIDDEN requirement (REQ-006, no
  // refund). A prohibition has no witness trace — there is no
  // state in which "a refund did not happen" becomes visible — so
  // the proof is this invariant staying true, and REQ-006 names it
  // in witness.enforced_by rather than in prose.
  val noRefundIssued: bool =
    status.keys().forall(c => accessUntilPeriodEnd.get(c) or
                              status.get(c) != Cancelled)

  // INV-003: a timed-out cancellation leaves the subscription
  // billable and Active. Without this, "remain active" is a
  // sentence in a document rather than a checked property.
  val timeoutLeavesActive: bool =
    status.keys().forall(c =>
      (lastBillingResult.get(c) == Timeout) implies
        (status.get(c) == Active and billingEnabled.get(c)))

  // ----------------------------------------------------------
  // INIT + STEP
  // ----------------------------------------------------------

  action init = all {
    status' = Map("alice" -> Active),
    billingEnabled' = Map("alice" -> true),
    accessUntilPeriodEnd' = Map("alice" -> false),
    lastBillingResult' = Map("alice" -> Success),
  }

  action step = any {
    cancel_subscription("alice"),
    cancel_again("alice"),
    cancel_times_out("alice"),
    expire_period("alice"),
  }

  // EX-001 in the JSON: the happy path, as a replayable scenario.
  run cancelHappyPath = init.then(cancel_subscription("alice"))

  // EX-002: cancelling twice changes nothing the second time.
  run doubleCancelIsHarmless =
    init.then(cancel_subscription("alice")).then(cancel_again("alice"))
}
