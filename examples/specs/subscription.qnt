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
  /// @internal The retry budget's counter. It exists so the model can hold
  ///   ASM-001 (the provider answers within MAX_CANCEL_RETRIES tries); no
  ///   customer sees it, so no requirement has to say how it moves.
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
  // end of the paid period, and the customer is told by one of the
  // two channels REQ-007 permits. Three requirements govern this one
  // step; each is checked on EVERY step of it, not just on one.
  /// @req REQ-001
  /// When a customer cancels an Active subscription, the system shall stop
  /// future billing.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @when the customer cancels and the provider accepts
  /// @while status.get(c) == Active
  /// @shall status.get(c) == Cancelled and not(billingEnabled.get(c))
  ///   and lastBillingResult.get(c) == Success
  /// @error BillingProvider.SUCCESS idempotent: Cancelled
  ///
  /// @req REQ-002
  /// When a cancellation succeeds, the system shall keep access available
  /// until the paid period ends.
  /// @status specified
  /// @modality must
  /// @when the customer cancels and the provider accepts
  /// @while status.get(c) == Active
  /// @shall status.get(c) == Cancelled and accessUntilPeriodEnd.get(c)
  /// @refs INV-002
  ///
  /// @req REQ-007
  /// When a cancellation succeeds, the system shall confirm it to the
  /// customer by email or by an in-app notice.
  /// @status specified
  /// @modality may
  /// @determinism nondeterministic
  /// @when the customer cancels and the provider accepts
  /// @while notice.get(c) == NoNotice
  /// @outcome email: notice.get(c) == Email
  /// @outcome in-app: notice.get(c) == InApp
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

  // The provider refuses the change (DECLINED): nothing moves but
  // the recorded answer.
  /// @req REQ-008
  /// If the billing provider declines a cancellation, then the system shall
  /// leave the subscription Active.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @unwanted
  /// @when the billing provider declines the cancellation
  /// @while status.get(c) == Active
  /// @shall lastBillingResult.get(c) == Declined and status.get(c) == Active
  ///   and billingEnabled.get(c)
  /// @error BillingProvider.DECLINED idempotent: NO_CHANGE — the provider
  ///   refused the change; the subscription stays Active and billable
  action cancel_declined(c: CustomerId): bool = all {
    status.get(c) == Active,
    lastBillingResult' = lastBillingResult.set(c, Declined),
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    unansweredTries' = unansweredTries.set(c, 0),
    notice' = notice,
  }

  // The provider does not answer — it times out, or the call never
  // reaches it. The subscription MUST remain Active, and the try
  // counts against the retry budget. ASM-001 says the provider
  // answers within MAX_CANCEL_RETRIES tries; the guard is that
  // assumption, written where the checker holds the model to it.
  /// @req REQ-004
  /// If the billing provider does not answer a cancellation, then the
  /// system shall leave the subscription Active.
  /// @status specified
  /// @modality must
  /// @unwanted
  /// @when the billing provider does not answer the cancellation
  /// @while status.get(c) == Active
  /// @shall (lastBillingResult.get(c) == Timeout
  ///   or lastBillingResult.get(c) == NetworkError)
  ///   and status.get(c) == Active and billingEnabled.get(c)
  /// @error BillingProvider.TIMEOUT idempotent: NO_CHANGE — the subscription
  ///   stays Active and billable; retry
  /// @error BillingProvider.NETWORK_ERROR idempotent: NO_CHANGE — the request
  ///   never landed; retry
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

  // Cancelling twice is harmless — the second call is a no-op, not
  // an error.
  /// @req REQ-003
  /// When a customer cancels an already-Cancelled subscription, the system
  /// shall make no further change.
  /// @status specified
  /// @modality must
  /// @determinism deterministic
  /// @when the customer cancels again
  /// @while status.get(c) == Cancelled
  /// @old billed: bool = billingEnabled.get(c)
  /// @old told: Notice = notice.get(c)
  /// @shall status.get(c) == Cancelled and accessUntilPeriodEnd.get(c)
  ///   and billingEnabled.get(c) == billed and notice.get(c) == told
  action cancel_again(c: CustomerId): bool = all {
    status.get(c) == Cancelled,
    status' = status,
    billingEnabled' = billingEnabled,
    accessUntilPeriodEnd' = accessUntilPeriodEnd,
    lastBillingResult' = lastBillingResult,
    unansweredTries' = unansweredTries,
    notice' = notice,
  }

  // The paid period runs out; access finally stops.
  /// @req REQ-005
  /// When the paid period ends for a cancelled subscription, the system
  /// shall withdraw access.
  /// @status specified
  /// @modality must
  /// @when the paid period ends
  /// @while status.get(c) == Cancelled
  /// @shall status.get(c) == Expired and not(accessUntilPeriodEnd.get(c))
  action expire_period(c: CustomerId): bool = all {
    status.get(c) == Cancelled,
    status' = status.set(c, Expired),
    accessUntilPeriodEnd' = accessUntilPeriodEnd.set(c, false),
    billingEnabled' = billingEnabled,
    lastBillingResult' = lastBillingResult,
    unansweredTries' = unansweredTries,
    notice' = notice,
  }

  // A customer may ask for a refund; the policy (DEC-001) is that the
  // answer is always no. The request is a call the system refuses in
  // every state: REQ-006's obligation is that this step never
  // happens, and EX-006 shows the refusal for the code to replay.
  /// @req REQ-006
  /// The system shall never issue a refund for the unused part of a paid
  /// period.
  /// @status specified
  /// @modality forbidden
  /// @unwanted
  /// @when the customer asks for a refund
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

  // INV-002: a refund would take back the access a cancelled
  // subscriber has paid for, so this is the state-side shadow of
  // REQ-006. REQ-006 itself is checked on the refund step.
  /// @inv INV-002
  /// A cancelled subscriber keeps access until the paid period ends.
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

  // Two answered questions. tools/spec-questions.py found that nothing
  // pinned what an ANSWER from the provider does to the retry count: a
  // model that kept counting after a decline, or after a successful
  // cancel, passed every example above. Asked "should the count be 0?",
  // the answer was yes for both — the budget is for tries nobody
  // answered, and an answer ends that run of tries. Each answer is the
  // run the tool printed, given a name and a title; the mutant it was
  // asked about now dies on EX-008 / EX-009, and the question is gone
  // from the readback. (Answered here to demonstrate the loop; in a real
  // project the person who owns the behavior answers.)

  /// @example EX-008
  /// A decline after an unanswered try resets the retry count
  /// @refs REQ-008, INV-004
  /// @source answered-question
  run declineResetsRetries = init
    .then(cancel_unanswered("alice", false))
    .then(cancel_declined("alice"))
    .expect(unansweredTries.get("alice") == 0)

  /// @example EX-009
  /// A successful cancel after an unanswered try resets the retry count
  /// @refs REQ-001, INV-004
  /// @source answered-question
  run successResetsRetries = init
    .then(cancel_unanswered("alice", true))
    .then(cancel_subscription("alice", InApp))
    .expect(unansweredTries.get("alice") == 0)

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
}
