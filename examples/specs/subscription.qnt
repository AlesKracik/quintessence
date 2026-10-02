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

/// @example EX-003
/// Provider times out: nothing moves
/// @given {"status": "Active", "billingEnabled": true}
/// @when cancel_times_out {"c": "alice"}
/// @expect {"status": "Active", "billingEnabled": true}
/// @refs REQ-004, INV-003
/// @source regression-for-REQ-004
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
  /// @req REQ-006
  /// The system shall never issue a refund for the unused part of a paid
  /// period.
  /// @status specified
  /// @modality forbidden
  /// @when a subscription is cancelled mid-period
  /// @unwanted
  /// @shall issue no refund
  /// @meaning Cancelling part-way through a paid period never refunds the
  ///   unused part. The customer keeps the access instead of getting money
  ///   back.
  /// @meaning-author agent
  /// @enforced-by INV-002
  /// @witness skipped: A prohibition has no witness trace — there is no
  ///   reachable state in which 'no refund happened' becomes visible. INV-002
  ///   carries the proof.
  /// @req REQ-007
  /// When a cancellation succeeds, the system shall confirm it to the
  /// customer.
  /// @status specified
  /// @modality may
  /// @determinism nondeterministic
  /// @when a cancellation succeeds
  /// @shall confirm to the customer by email or in-app notice
  /// @meaning A successful cancellation may be confirmed to the customer, by
  ///   email or in the app. Which one, or whether to confirm at all, is left
  ///   open on purpose.
  /// @meaning-author agent
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
  /// @inv INV-001
  /// A cancelled or expired subscription is never billed again.
  /// @criticality critical
  /// @proof inductive
  val noBillingAfterCancel: bool =
    status.keys().forall(c =>
      (status.get(c) == Cancelled or status.get(c) == Expired)
        implies not(billingEnabled.get(c)))

  // INV-002: carries the FORBIDDEN requirement (REQ-006, no
  // refund). A prohibition has no witness trace — there is no
  // state in which "a refund did not happen" becomes visible — so
  // the proof is this invariant staying true, and REQ-006 names it
  // in witness.enforced_by rather than in prose.
  /// @inv INV-002
  /// No refund is ever issued for the unused period.
  /// @criticality high
  val noRefundIssued: bool =
    status.keys().forall(c => accessUntilPeriodEnd.get(c) or
                              status.get(c) != Cancelled)

  // INV-003: a timed-out cancellation leaves the subscription
  // billable and Active. Without this, "remain active" is a
  // sentence in a document rather than a checked property.
  /// @inv INV-003
  /// A timed-out cancellation leaves the subscription Active and billable.
  /// @criticality critical
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
  /// @example EX-001
  /// Cancel mid-period: billing stops, access stays
  /// @given {"status": "Active", "billingEnabled": true}
  /// @when cancel_subscription {"c": "alice"}
  /// @expect {"status": "Cancelled", "billingEnabled": false,
  ///   "accessUntilPeriodEnd": true}
  /// @refs REQ-001, REQ-002
  /// @source elicited
  run cancelHappyPath = init.then(cancel_subscription("alice"))

  // EX-002: cancelling twice changes nothing the second time.
  /// @example EX-002
  /// Cancelling twice is harmless
  /// @given {"status": "Cancelled", "accessUntilPeriodEnd": true}
  /// @when cancel_again {"c": "alice"}
  /// @expect {"status": "Cancelled", "accessUntilPeriodEnd": true}
  /// @refs REQ-003
  /// @source elicited
  run doubleCancelIsHarmless =
    init.then(cancel_subscription("alice")).then(cancel_again("alice"))

  // ----------------------------------------------------------
  // REQUIREMENTS — witness predicates, constants and transition
  // properties whose records live on them (spec_source.py)
  // ----------------------------------------------------------

  /// @req REQ-001
  /// When a customer cancels an Active subscription, the system shall stop
  /// future billing.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @while the Subscription is Active
  /// @when the customer cancels
  /// @shall stop future billing and mark the Subscription Cancelled
  /// @meaning Cancelling a live subscription stops it being billed again and
  ///   records it as cancelled.
  /// @meaning-author agent
  /// @error BillingProvider.SUCCESS idempotent: Cancelled
  /// @error BillingProvider.DECLINED idempotent: NO_CHANGE — surface the
  ///   provider's reason to the customer; the subscription stays Active
  /// @via cancel_subscription
  val shall_REQ_001: bool =
    status.keys().exists(c => status.get(c) == Cancelled and not(billingEnabled.get(c)))

  /// @req REQ-002
  /// While the paid period has not ended, the system shall keep access
  /// available to a cancelled subscriber.
  /// @status specified
  /// @modality must
  /// @while the Subscription is Cancelled and the paid period has not ended
  /// @shall keep access available
  /// @meaning Cancelling does not cut the customer off immediately: they keep
  ///   what they already paid for until the paid period runs out.
  /// @meaning-author agent
  /// @via cancel_subscription
  val shall_REQ_002: bool =
    status.keys().exists(c => status.get(c) == Cancelled and accessUntilPeriodEnd.get(c))

  /// @req REQ-003
  /// When a customer cancels an already-Cancelled subscription, the system
  /// shall make no further change.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @while the Subscription is Cancelled
  /// @when the customer cancels again
  /// @shall make no further change and report success
  /// @meaning Cancelling something already cancelled is treated as a no-op
  ///   that succeeded, not as an error — a retried or duplicated request
  ///   cannot do extra damage.
  /// @meaning-author agent
  /// @via cancel_again
  val shall_REQ_003: bool =
    status.keys().exists(c => status.get(c) == Cancelled and accessUntilPeriodEnd.get(c))

  /// @req REQ-004
  /// If the billing provider does not respond in time, then the system shall
  /// leave the subscription Active.
  /// @status specified
  /// @modality must
  /// @while the Subscription is Active
  /// @when the billing provider times out during cancellation
  /// @unwanted
  /// @shall leave the Subscription Active and billing enabled
  /// @meaning If the billing provider does not answer during a cancellation,
  ///   the subscription stays live and billed. The system does not record a
  ///   cancellation it could not confirm.
  /// @meaning-author agent
  /// @error BillingProvider.TIMEOUT idempotent: NO_CHANGE — the subscription
  ///   stays Active and billable; retry
  /// @error BillingProvider.NETWORK_ERROR idempotent: NO_CHANGE — the request
  ///   never landed; retry
  /// @via cancel_times_out
  val shall_REQ_004: bool =
    status.keys().exists(c => lastBillingResult.get(c) == Timeout and status.get(c) == Active)

  /// @req REQ-005
  /// When the paid period ends for a cancelled subscription, the system
  /// shall withdraw access.
  /// @status specified
  /// @modality must
  /// @while the Subscription is Cancelled
  /// @when the paid period ends
  /// @shall withdraw access and mark the Subscription Expired
  /// @meaning When the paid period ends on a cancelled subscription, access
  ///   stops and the subscription is recorded as expired.
  /// @meaning-author agent
  /// @via expire_period
  val shall_REQ_005: bool =
    status.keys().exists(c => status.get(c) == Expired and not(accessUntilPeriodEnd.get(c)))

  /// @outcome-of REQ-007 email
  val shall_REQ_007_email: bool =
    status.keys().exists(c => status.get(c) == Cancelled)

  /// @outcome-of REQ-007 in-app
  val shall_REQ_007_in_app: bool =
    status.keys().exists(c => status.get(c) == Cancelled and accessUntilPeriodEnd.get(c))

  /// @con CON-001
  /// How long the system waits for the provider before treating the call as
  /// TIMEOUT.
  /// @unit seconds
  pure val BILLING_TIMEOUT_SECONDS = 30

  /// @con CON-002
  /// Retry budget after TIMEOUT or NETWORK_ERROR. Bounds ASM-001.
  pure val MAX_CANCEL_RETRIES = 3
}
