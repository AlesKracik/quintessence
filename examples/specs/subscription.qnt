// ============================================================
// Quint Specification: subscription
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
// make. What the intent file adds on top of this model: the
// declared boundary (scope), the provider's full outcome set
// (externals), what the environment is assumed to do
// (assumptions), which behaviors are required vs merely
// permitted (modality), and what happens on each failure
// (error_outcomes).
//
// Every one of those claims is encoded here, not only written
// down: each answer the provider can give is produced by an
// action, the retry budget is a guard, the confirmation channel
// is state, and the refused refund is a call that is refused.
// ============================================================

module subscription {

  // ----------------------------------------------------------
  // TYPES
  // SubStatus is CLOSED in the intent file: exactly these three
  // states exist. spec-lint holds this type to that list, so a
  // fourth state cannot appear here without the intent admitting
  // it.
  // ----------------------------------------------------------

  type CustomerId = str
  type SubStatus = Active | Cancelled | Expired

  // Every answer the billing provider can give. Modelling the
  // unhappy ones is the point: each is produced by an action
  // below, so what the spec says about it is checked.
  type BillingResult = Success | Declined | Timeout | NetworkError

  // How a successful cancellation was confirmed. REQ-007 leaves
  // the channel to the system, so the model records which one it
  // took — that is what makes the two permitted outcomes
  // distinguishable at all.
  type Notice = NoNotice | Email | InApp

  // ----------------------------------------------------------
  // STATE
  // ----------------------------------------------------------

  var status: CustomerId -> SubStatus
  var billingEnabled: CustomerId -> bool
  var accessUntilPeriodEnd: CustomerId -> bool
  var lastBillingResult: CustomerId -> BillingResult
  var unansweredTries: CustomerId -> int
  var notice: CustomerId -> Notice

  pure val CUSTOMERS: Set[CustomerId] = Set("alice", "bob")

  /// @con CON-002
  /// Retry budget after TIMEOUT or NETWORK_ERROR. Bounds ASM-001.
  /// @pairs INV-004
  pure val MAX_CANCEL_RETRIES: int = 3

  def triesOf(c: CustomerId): int =
    if (unansweredTries.keys().contains(c)) unansweredTries.get(c) else 0

  // ----------------------------------------------------------
  // ACTIONS
  // ----------------------------------------------------------

  // The provider accepts: billing stops, access survives to the
  // end of the paid period (REQ-002), and the customer is told by
  // one of the two channels REQ-007 permits.
  /// @req REQ-007
  /// When a cancellation succeeds, the system shall confirm it to the
  /// customer.
  /// @status specified
  /// @modality may
  /// @determinism nondeterministic
  /// @when a cancellation succeeds
  /// @shall confirm to the customer by email or in-app notice
  action cancel_subscription(c: CustomerId, n: Notice): bool = all {
    status.get(c) == Active,
    n != NoNotice,
    lastBillingResult' = lastBillingResult.set(c, Success),
    status' = status.set(c, Cancelled),
    billingEnabled' = billingEnabled.set(c, false),
    accessUntilPeriodEnd' = accessUntilPeriodEnd.set(c, true),
    unansweredTries' = unansweredTries.set(c, 0),
    notice' = notice.set(c, n),
  }

  // REQ-008: the provider refuses the change (DECLINED): nothing
  // moves but the recorded answer.
  action cancel_declined(c: CustomerId): bool = all {
    status.get(c) == Active,
    lastBillingResult' = lastBillingResult.set(c, Declined),
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    unansweredTries' = unansweredTries.set(c, 0),
    notice' = notice,
  }

  // REQ-004: the provider does not answer — it times out, or the
  // call never reaches it. The subscription MUST remain Active,
  // and the try counts against the retry budget. ASM-001 says the
  // provider answers within MAX_CANCEL_RETRIES tries; the guard is
  // that assumption, written where the checker holds the model to
  // it.
  action cancel_unanswered(c: CustomerId, timedOut: bool): bool = all {
    status.get(c) == Active,
    triesOf(c) < MAX_CANCEL_RETRIES,
    lastBillingResult' = lastBillingResult.set(c, if (timedOut) Timeout else NetworkError),
    unansweredTries' = unansweredTries.set(c, triesOf(c) + 1),
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    notice' = notice,
  }

  // REQ-003: cancelling twice is harmless — the second call is a
  // no-op, not an error.
  action cancel_again(c: CustomerId): bool = all {
    status.get(c) == Cancelled,
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    lastBillingResult' = lastBillingResult,
    unansweredTries' = unansweredTries,
    notice' = notice,
  }

  // REQ-005: the paid period runs out; access finally stops.
  action expire_period(c: CustomerId): bool = all {
    status.get(c) == Cancelled,
    status' = status.set(c, Expired),
    accessUntilPeriodEnd' = accessUntilPeriodEnd.set(c, false),
    billingEnabled' = billingEnabled,
    lastBillingResult' = lastBillingResult,
    unansweredTries' = unansweredTries,
    notice' = notice,
  }

  // REQ-006: a customer may ask for a refund; the policy (DEC-001)
  // is that the answer is always no. The request is a call the
  // system refuses in every state, so the refusal can be shown —
  // an example attempts it with `.fail()`.
  /// @req REQ-006
  /// The system shall never issue a refund for the unused part of a paid
  /// period.
  /// @status specified
  /// @modality forbidden
  /// @when the customer asks for a refund
  /// @unwanted
  /// @shall refuse the refund and leave the subscription as it is
  /// @enforced-by INV-002
  /// @witness skipped: A prohibition has no witness trace — there is no
  ///   reachable state in which 'no refund happened' becomes visible. INV-002
  ///   carries the model-side proof; EX-006 shows the refusal.
  action request_refund(c: CustomerId): bool = all {
    false,   // DEC-001: refunds are never granted
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    lastBillingResult' = lastBillingResult,
    unansweredTries' = unansweredTries,
    notice' = notice,
  }

  // ----------------------------------------------------------
  // INVARIANTS
  // ----------------------------------------------------------

  /// @inv INV-001
  /// A cancelled or expired subscription is never billed again.
  /// @criticality critical
  /// @proof inductive
  val noBillingAfterCancel: bool =
    status.keys().forall(c =>
      (status.get(c) == Cancelled or status.get(c) == Expired)
        implies not(billingEnabled.get(c)))

  // INV-002: carries the FORBIDDEN requirement (REQ-006, no
  // refund): a cancelled subscriber keeps access to the end of the
  // period they paid for, which a refund would take back.
  /// @inv INV-002
  /// No refund is ever issued for the unused period.
  /// @criticality high
  val noRefundIssued: bool =
    status.keys().forall(c => accessUntilPeriodEnd.get(c) or
                              status.get(c) != Cancelled)

  // INV-003: a cancellation the provider did not accept — declined,
  // timed out, or never delivered — leaves the subscription billable
  // and Active. Without this, "remain active" is a sentence in a
  // document rather than a checked property.
  /// @inv INV-003
  /// A cancellation the provider did not accept leaves the subscription
  /// Active and billable.
  /// @criticality critical
  val unacceptedCancelLeavesActive: bool =
    status.keys().forall(c =>
      (lastBillingResult.get(c) != Success) implies
        (status.get(c) == Active and billingEnabled.get(c)))

  // INV-004: paired with CON-002. The other side of the retry
  // budget: unanswered tries never exceed it.
  /// @inv INV-004
  /// Unanswered cancellation tries never exceed MAX_CANCEL_RETRIES.
  /// @criticality high
  val retriesWithinBudget: bool =
    status.keys().forall(c => triesOf(c) <= MAX_CANCEL_RETRIES)

  // ----------------------------------------------------------
  // INIT + STEP
  // ----------------------------------------------------------

  action init = all {
    status' = CUSTOMERS.mapBy(_ => Active),
    billingEnabled' = CUSTOMERS.mapBy(_ => true),
    accessUntilPeriodEnd' = CUSTOMERS.mapBy(_ => false),
    lastBillingResult' = CUSTOMERS.mapBy(_ => Success),
    unansweredTries' = CUSTOMERS.mapBy(_ => 0),
    notice' = CUSTOMERS.mapBy(_ => NoNotice),
  }

  action step = {
    nondet c = oneOf(CUSTOMERS)
    nondet n = oneOf(Set(Email, InApp))
    nondet timedOut = oneOf(Set(true, false))
    any {
      cancel_subscription(c, n),
      cancel_declined(c),
      cancel_unanswered(c, timedOut),
      cancel_again(c),
      expire_period(c),
      request_refund(c),
    }
  }

  // ----------------------------------------------------------
  // EXAMPLES — each run is a Given / When / Then row in the
  // readback, a `quint test`, and a conformance case.
  // ----------------------------------------------------------

  /// @example EX-001
  /// Cancel mid-period: billing stops, access stays
  /// @refs REQ-001, REQ-002, REQ-007
  /// @source elicited
  run cancelHappyPath = init
    .then(cancel_subscription("alice", Email))
    .expect(status.get("alice") == Cancelled and not(billingEnabled.get("alice"))
            and accessUntilPeriodEnd.get("alice") and notice.get("alice") == Email)

  /// @example EX-002
  /// Cancelling twice is harmless
  /// @refs REQ-003
  /// @source elicited
  run doubleCancelIsHarmless = init
    .then(cancel_subscription("alice", Email))
    .then(cancel_again("alice"))
    .expect(status.get("alice") == Cancelled and accessUntilPeriodEnd.get("alice"))

  /// @example EX-003
  /// Provider times out: nothing moves
  /// @refs REQ-004, INV-003
  /// @source regression-for-REQ-004
  run providerTimesOut = init
    .then(cancel_unanswered("alice", true))
    .expect(status.get("alice") == Active and billingEnabled.get("alice"))

  /// @example EX-004
  /// Provider declines: the subscription stays Active
  /// @refs REQ-008, INV-003
  run providerDeclines = init
    .then(cancel_declined("alice"))
    .expect(status.get("alice") == Active and lastBillingResult.get("alice") == Declined)

  /// @example EX-005
  /// After three unanswered tries there is no fourth
  /// @refs INV-004, ASM-001
  run retryBudgetSpent = init
    .then(cancel_unanswered("alice", true))
    .then(cancel_unanswered("alice", false))
    .then(cancel_unanswered("alice", true))
    .expect(status.get("alice") == Active and triesOf("alice") == 3)
    .then(cancel_unanswered("alice", true).fail())

  /// @example EX-006
  /// A refund request after cancelling is refused
  /// @refs REQ-006
  run refundRefused = init
    .then(cancel_subscription("alice", InApp))
    .expect(status.get("alice") == Cancelled and accessUntilPeriodEnd.get("alice"))
    .then(request_refund("alice").fail())

  /// @example EX-007
  /// The paid period ends: access stops
  /// @refs REQ-005, REQ-007
  run periodEnds = init
    .then(cancel_subscription("alice", InApp))
    .then(expire_period("alice"))
    .expect(status.get("alice") == Expired and not(accessUntilPeriodEnd.get("alice")))

  // The "Cancel a subscription" journey
  // (specs/journeys/cancel-subscription.journey.json), end to end:
  // the first try goes unanswered, the retry succeeds, a second
  // cancel is harmless, and the period runs out.
  run cancelJourney = init
    .then(cancel_unanswered("alice", true))
    .then(cancel_subscription("alice", Email))
    .then(cancel_again("alice"))
    .then(expire_period("alice"))
    .expect(status.get("alice") == Expired and not(billingEnabled.get("alice")))

  // ----------------------------------------------------------
  // REQUIREMENTS — witness predicates, bound to the customer the
  // call was about (`_lastC`), each with the pre-state the step
  // had to start from.
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
  /// @error BillingProvider.SUCCESS idempotent: Cancelled
  /// @via cancel_subscription
  /// @pre _prevStatus.get(_lastC) == Active
  def shall_REQ_001(_lastC: CustomerId): bool =
    status.get(_lastC) == Cancelled and not(billingEnabled.get(_lastC))

  /// @req REQ-002
  /// While the paid period has not ended, the system shall keep access
  /// available to a cancelled subscriber.
  /// @status specified
  /// @modality must
  /// @while the Subscription is Cancelled and the paid period has not ended
  /// @shall keep access available
  /// @via cancel_subscription
  /// @pre _prevStatus.get(_lastC) == Active
  def shall_REQ_002(_lastC: CustomerId): bool =
    status.get(_lastC) == Cancelled and accessUntilPeriodEnd.get(_lastC)

  /// @req REQ-003
  /// When a customer cancels an already-Cancelled subscription, the system
  /// shall make no further change.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @while the Subscription is Cancelled
  /// @when the customer cancels again
  /// @shall make no further change and report success
  /// @via cancel_again
  /// @pre _prevStatus.get(_lastC) == Cancelled
  def shall_REQ_003(_lastC: CustomerId): bool =
    status.get(_lastC) == Cancelled and accessUntilPeriodEnd.get(_lastC)

  /// @req REQ-004
  /// If the billing provider does not answer a cancellation, then the
  /// system shall leave the subscription Active.
  /// @status specified
  /// @modality must
  /// @while the Subscription is Active
  /// @when the billing provider does not answer the cancellation
  /// @unwanted
  /// @shall leave the Subscription Active and billing enabled
  /// @error BillingProvider.TIMEOUT idempotent: NO_CHANGE — the subscription
  ///   stays Active and billable; retry
  /// @error BillingProvider.NETWORK_ERROR idempotent: NO_CHANGE — the request
  ///   never landed; retry
  /// @via cancel_unanswered
  /// @pre _prevStatus.get(_lastC) == Active
  def shall_REQ_004(_lastC: CustomerId): bool =
    (lastBillingResult.get(_lastC) == Timeout or lastBillingResult.get(_lastC) == NetworkError)
      and status.get(_lastC) == Active and billingEnabled.get(_lastC)

  /// @req REQ-008
  /// If the billing provider declines a cancellation, then the system shall
  /// leave the subscription Active.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @while the Subscription is Active
  /// @when the billing provider declines the cancellation
  /// @unwanted
  /// @shall leave the Subscription Active and billing enabled
  /// @error BillingProvider.DECLINED idempotent: NO_CHANGE — the provider
  ///   refused the change; the subscription stays Active and billable
  /// @via cancel_declined
  /// @pre _prevStatus.get(_lastC) == Active
  def shall_REQ_008(_lastC: CustomerId): bool =
    lastBillingResult.get(_lastC) == Declined
      and status.get(_lastC) == Active and billingEnabled.get(_lastC)

  /// @req REQ-005
  /// When the paid period ends for a cancelled subscription, the system
  /// shall withdraw access.
  /// @status specified
  /// @modality must
  /// @while the Subscription is Cancelled
  /// @when the paid period ends
  /// @shall withdraw access and mark the Subscription Expired
  /// @via expire_period
  /// @pre _prevStatus.get(_lastC) == Cancelled
  def shall_REQ_005(_lastC: CustomerId): bool =
    status.get(_lastC) == Expired and not(accessUntilPeriodEnd.get(_lastC))

  /// @outcome-of REQ-007 email
  /// @pre _prevNotice.get(_lastC) == NoNotice
  def shall_REQ_007_email(_lastC: CustomerId): bool =
    notice.get(_lastC) == Email

  /// @outcome-of REQ-007 in-app
  /// @pre _prevNotice.get(_lastC) == NoNotice
  def shall_REQ_007_in_app(_lastC: CustomerId): bool =
    notice.get(_lastC) == InApp
}
