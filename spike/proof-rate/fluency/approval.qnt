module approval {

  type UserId = str
  type DocId = str

  type Status =
    | Absent
    | Draft
    | Submitted
    | Approved
    | Rejected
    | Published
    | Closed

  type Doc = { status: Status, author: UserId, approver: UserId, rejections: int }

  /// @con CON-001
  /// Most rejections a document can take before it is closed.
  /// @unit rejections
  /// @pairs INV-002
  pure val MAX_REJECTIONS: int = 3

  pure val USERS: Set[UserId] = Set("alice", "bob", "carol")
  pure val DOCS: Set[DocId] = Set("d1", "d2")

  var docs: DocId -> Doc
  /// @internal The document as it was when it first became Published or
  ///   Closed; lets INV-003 say such a document never changes.
  var frozenSnap: DocId -> Doc

  pure val NO_DOC: Doc = { status: Absent, author: "", approver: "", rejections: 0 }

  pure def isFrozen(x: Doc): bool = x.status == Published or x.status == Closed

  def docOf(d: DocId): Doc =
    if (docs.keys().contains(d)) docs.get(d) else NO_DOC

  def statusOf(d: DocId): Status = docOf(d).status
  def authorOf(d: DocId): UserId = docOf(d).author
  def approverOf(d: DocId): UserId = docOf(d).approver
  def rejectionsOf(d: DocId): int = docOf(d).rejections

  // All writes go through commit so the snapshot stays in step.
  action commit(newDocs: DocId -> Doc): bool = all {
    docs' = newDocs,
    frozenSnap' = DOCS.mapBy(d =>
      if (isFrozen(docs.get(d))) frozenSnap.get(d) else newDocs.get(d)),
  }

  action init = all {
    docs' = DOCS.mapBy(d => NO_DOC),
    frozenSnap' = DOCS.mapBy(d => NO_DOC),
  }

  /// @req REQ-001
  /// When an author creates a draft, the system shall record a Draft owned by that author.
  /// @title Create a draft
  /// @status specified
  /// @when an author creates a draft
  /// @while statusOf(d) == Absent
  /// @shall statusOf(d) == Draft and authorOf(d) == u and rejectionsOf(d) == 0
  action create(u: UserId, d: DocId): bool = all {
    statusOf(d) == Absent,
    commit(docs.put(d, { status: Draft, author: u, approver: "", rejections: 0 })),
  }

  /// @req REQ-002
  /// When the author submits a Draft, the system shall move it to Submitted.
  /// @title Submit a draft
  /// @status specified
  /// @when the author submits a draft
  /// @while statusOf(d) == Draft and authorOf(d) == u
  /// @shall statusOf(d) == Submitted
  action submit(u: UserId, d: DocId): bool = all {
    statusOf(d) == Draft,
    authorOf(d) == u,
    commit(docs.put(d, { ...docOf(d), status: Submitted })),
  }

  /// @req REQ-003
  /// While a document is Submitted, when a reviewer other than its author approves
  /// it, the system shall mark it Approved and record the approver.
  /// @title Non-author approves
  /// @status specified
  /// @when a reviewer approves a submitted document
  /// @while statusOf(d) == Submitted and u != authorOf(d)
  /// @shall statusOf(d) == Approved and approverOf(d) == u
  action approve(u: UserId, d: DocId): bool = all {
    statusOf(d) == Submitted,
    u != authorOf(d),
    commit(docs.put(d, { ...docOf(d), status: Approved, approver: u })),
  }

  /// @req REQ-004
  /// While a document is Submitted, when a reviewer other than its author rejects it,
  /// the system shall count the rejection and mark it Rejected, or Closed on the third.
  /// @title Non-author rejects
  /// @status specified
  /// @when a reviewer rejects a submitted document
  /// @while statusOf(d) == Submitted and u != authorOf(d)
  /// @old before: int = rejectionsOf(d)
  /// @shall rejectionsOf(d) == before + 1
  ///   and (statusOf(d) == Closed iff rejectionsOf(d) >= MAX_REJECTIONS)
  action reject(u: UserId, d: DocId): bool = {
    val n = rejectionsOf(d) + 1
    all {
      statusOf(d) == Submitted,
      u != authorOf(d),
      commit(docs.put(d, { ...docOf(d),
        status: if (n >= MAX_REJECTIONS) Closed else Rejected,
        rejections: n })),
    }
  }

  /// @req REQ-005
  /// While a document is Rejected, when its author resubmits it, the system shall
  /// move it to Submitted.
  /// @title Author resubmits
  /// @status specified
  /// @when the author revises and resubmits a rejected document
  /// @while statusOf(d) == Rejected and authorOf(d) == u
  /// @shall statusOf(d) == Submitted
  action resubmit(u: UserId, d: DocId): bool = all {
    statusOf(d) == Rejected,
    authorOf(d) == u,
    commit(docs.put(d, { ...docOf(d), status: Submitted })),
  }

  /// @req REQ-006
  /// When an Approved document is published, the system shall mark it Published.
  /// @title Publish an approved document
  /// @status specified
  /// @when someone publishes an approved document
  /// @while statusOf(d) == Approved
  /// @shall statusOf(d) == Published
  action publish(u: UserId, d: DocId): bool = all {
    statusOf(d) == Approved,
    commit(docs.put(d, { ...docOf(d), status: Published })),
  }

  action step = {
    nondet u = oneOf(USERS)
    nondet d = oneOf(DOCS)
    any {
      create(u, d),
      submit(u, d),
      approve(u, d),
      reject(u, d),
      resubmit(u, d),
      publish(u, d),
    }
  }

  /// @inv INV-001
  /// No document is published unless a non-author approved it.
  /// @criticality critical
  val publishedWasApprovedByNonAuthor: bool =
    DOCS.forall(d =>
      (statusOf(d) == Published) implies
        (approverOf(d) != "" and approverOf(d) != authorOf(d)))

  /// @inv INV-002
  /// The rejection count never exceeds 3.
  /// @criticality critical
  val rejectionsBounded: bool =
    DOCS.forall(d => rejectionsOf(d) <= MAX_REJECTIONS)

  /// @inv INV-003
  /// A published or closed document never changes again: it always equals
  /// the snapshot taken when it became published or closed.
  /// @criticality critical
  val frozenNeverChanges: bool =
    DOCS.forall(d => isFrozen(docOf(d)) implies docOf(d) == frozenSnap.get(d))

  /// @inv INV-004
  /// A document is Closed exactly when it has taken the maximum rejections
  /// and was not resubmitted.
  /// @criticality high
  val closedAfterThirdRejection: bool =
    DOCS.forall(d => (statusOf(d) == Closed) implies rejectionsOf(d) == MAX_REJECTIONS)

  val allInvariants: bool =
    publishedWasApprovedByNonAuthor and rejectionsBounded
      and frozenNeverChanges and closedAfterThirdRejection

  /// @example EX-001
  /// A draft is submitted, approved by another user and published
  /// @refs REQ-001, REQ-002, REQ-003, REQ-006
  run happyPathTest = init
    .then(create("alice", "d1"))
    .then(submit("alice", "d1"))
    .then(approve("bob", "d1"))
    .then(publish("alice", "d1"))
    .expect(statusOf("d1") == Published and approverOf("d1") == "bob")

  /// @example EX-002
  /// The author cannot approve their own document
  /// @refs REQ-003
  run authorCannotApproveTest = init
    .then(create("alice", "d1"))
    .then(submit("alice", "d1"))
    .expect(statusOf("d1") == Submitted)
    .then(approve("alice", "d1").fail())

  /// @example EX-003
  /// A rejected document is resubmitted and then approved
  /// @refs REQ-004, REQ-005
  run reviseAndApproveTest = init
    .then(create("alice", "d1"))
    .then(submit("alice", "d1"))
    .then(reject("bob", "d1"))
    .expect(statusOf("d1") == Rejected and rejectionsOf("d1") == 1)
    .then(resubmit("alice", "d1"))
    .then(approve("carol", "d1"))
    .expect(statusOf("d1") == Approved)

  /// @example EX-004
  /// The third rejection closes the document for good
  /// @refs REQ-004, REQ-005
  run closedAfterThreeTest = init
    .then(create("alice", "d1"))
    .then(submit("alice", "d1"))
    .then(reject("bob", "d1"))
    .then(resubmit("alice", "d1"))
    .then(reject("bob", "d1"))
    .then(resubmit("alice", "d1"))
    .then(reject("carol", "d1"))
    .expect(statusOf("d1") == Closed and rejectionsOf("d1") == 3)
    .then(resubmit("alice", "d1").fail())

  /// @example EX-005
  /// A draft cannot be published without approval
  /// @refs REQ-006
  run publishNeedsApprovalTest = init
    .then(create("alice", "d1"))
    .then(submit("alice", "d1"))
    .expect(statusOf("d1") == Submitted)
    .then(publish("bob", "d1").fail())
}
