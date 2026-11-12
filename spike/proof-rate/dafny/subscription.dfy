module Subscription {
  datatype Option<T> = None | Some(value: T)

  type CustomerId = string
  datatype SubStatus = Active | Cancelled | Expired
  datatype BillingResult = Success | Declined | Timeout | NetworkError
  datatype Notice = NoNotice | Email | InApp

  const MAX_CANCEL_RETRIES: int := 3
  const CUSTOMERS: set<CustomerId> := {"alice", "bob"}

  datatype State = State(
    status: map<CustomerId, SubStatus>,
    billingEnabled: map<CustomerId, bool>,
    accessUntilPeriodEnd: map<CustomerId, bool>,
    lastBillingResult: map<CustomerId, BillingResult>,
    unansweredTries: map<CustomerId, int>,
    notice: map<CustomerId, Notice>
  )

  datatype Action =
    | CancelSubscription(c: CustomerId, n: Notice)
    | CancelDeclined(c: CustomerId)
    | CancelUnanswered(c: CustomerId, timedOut: bool)
    | CancelAgain(c: CustomerId)
    | ExpirePeriod(c: CustomerId)
    | RequestRefund(c: CustomerId)

  function TriesOf(s: State, c: CustomerId): int {
    if c in s.unansweredTries then s.unansweredTries[c] else 0
  }

  function Init(): State {
    State(
      map c | c in CUSTOMERS :: Active,
      map c | c in CUSTOMERS :: true,
      map c | c in CUSTOMERS :: false,
      map c | c in CUSTOMERS :: Success,
      map c | c in CUSTOMERS :: 0,
      map c | c in CUSTOMERS :: NoNotice)
  }

  // Quint's oneOf(CUSTOMERS) restricts c to the customer set; the guard
  // c in CUSTOMERS reflects that. status.get(c) on a missing key is undefined.
  function Apply(a: Action, s: State): Option<State> {
    match a {
      case CancelSubscription(c, n) =>
        if c in CUSTOMERS && c in s.status && s.status[c] == Active && n != NoNotice then
          Some(s.(lastBillingResult := s.lastBillingResult[c := Success],
                  status := s.status[c := Cancelled],
                  billingEnabled := s.billingEnabled[c := false],
                  accessUntilPeriodEnd := s.accessUntilPeriodEnd[c := true],
                  unansweredTries := s.unansweredTries[c := 0],
                  notice := s.notice[c := n]))
        else None
      case CancelDeclined(c) =>
        if c in CUSTOMERS && c in s.status && s.status[c] == Active then
          Some(s.(lastBillingResult := s.lastBillingResult[c := Declined],
                  unansweredTries := s.unansweredTries[c := 0]))
        else None
      case CancelUnanswered(c, timedOut) =>
        if c in CUSTOMERS && c in s.status && s.status[c] == Active && TriesOf(s, c) < MAX_CANCEL_RETRIES then
          Some(s.(lastBillingResult := s.lastBillingResult[c := (if timedOut then Timeout else NetworkError)],
                  unansweredTries := s.unansweredTries[c := TriesOf(s, c) + 1]))
        else None
      case CancelAgain(c) =>
        if c in CUSTOMERS && c in s.status && s.status[c] == Cancelled then Some(s) else None
      case ExpirePeriod(c) =>
        if c in CUSTOMERS && c in s.status && s.status[c] == Cancelled then
          Some(s.(status := s.status[c := Expired],
                  accessUntilPeriodEnd := s.accessUntilPeriodEnd[c := false]))
        else None
      case RequestRefund(c) => None
    }
  }

  predicate noBillingAfterCancel(s: State) {
    forall c | c in s.status.Keys ::
      (s.status[c] == Cancelled || s.status[c] == Expired) ==>
        (c in s.billingEnabled && !s.billingEnabled[c])
  }

  predicate noRefundIssued(s: State) {
    forall c | c in s.status.Keys ::
      (c in s.accessUntilPeriodEnd && s.accessUntilPeriodEnd[c]) || s.status[c] != Cancelled
  }

  predicate unacceptedCancelLeavesActive(s: State) {
    forall c | c in s.status.Keys ::
      (c in s.lastBillingResult && s.lastBillingResult[c] != Success) ==>
        (s.status[c] == Active && c in s.billingEnabled && s.billingEnabled[c])
  }

  predicate retriesWithinBudget(s: State) {
    forall c | c in s.status.Keys :: TriesOf(s, c) <= MAX_CANCEL_RETRIES
  }

  ghost predicate Inv(s: State) {
    && s.billingEnabled.Keys == s.status.Keys
    && s.accessUntilPeriodEnd.Keys == s.status.Keys
    && s.lastBillingResult.Keys == s.status.Keys
    && s.unansweredTries.Keys == s.status.Keys
    && s.notice.Keys == s.status.Keys
    && (forall c | c in s.status.Keys ::
        && (s.status[c] == Active ==> s.billingEnabled[c])
        && (s.status[c] == Cancelled ==> !s.billingEnabled[c] && s.accessUntilPeriodEnd[c])
        && (s.status[c] == Expired ==> !s.billingEnabled[c])
        && (s.lastBillingResult[c] != Success ==> s.status[c] == Active)
        && 0 <= s.unansweredTries[c] <= MAX_CANCEL_RETRIES)
  }

  lemma InitHolds() ensures Inv(Init()) {}

  lemma StepHolds(a: Action, s: State)
    requires Inv(s) && Apply(a, s).Some?
    ensures Inv(Apply(a, s).value)
  {}

  lemma Implies_noBillingAfterCancel(s: State) requires Inv(s) ensures noBillingAfterCancel(s) {}
  lemma Implies_noRefundIssued(s: State) requires Inv(s) ensures noRefundIssued(s) {}
  lemma Implies_unacceptedCancelLeavesActive(s: State) requires Inv(s) ensures unacceptedCancelLeavesActive(s) {}
  lemma Implies_retriesWithinBudget(s: State) requires Inv(s) ensures retriesWithinBudget(s) {}
}
