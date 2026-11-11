// Quint model: meeting-room reservation.
module reservation {

  type UserId = str
  type RoomId = str
  type SlotId = int

  type Cell =
    | Free
    | Active(UserId)
    | Completed(UserId)

  /// @con CON-001
  /// Most active reservations per user.
  /// @unit reservations
  /// @pairs INV-002
  pure val MAX_ACTIVE: int = 2

  pure val ROOMS: Set[RoomId] = Set("r1", "r2")
  pure val SLOTS: Set[SlotId] = Set(1, 2, 3)
  pure val USERS: Set[UserId] = Set("alice", "bob", "carol")
  pure val ALL_CELLS: Set[(RoomId, SlotId)] = tuples(ROOMS, SLOTS)

  var cells: (RoomId, SlotId) -> Cell
  /// @internal The clock: slots numbered up to `now` have passed.
  var now: int

  pure def isActiveCell(c: Cell): bool =
    match c {
      | Active(_) => true
      | _ => false
    }

  pure def isCompletedCell(c: Cell): bool =
    match c {
      | Completed(_) => true
      | _ => false
    }

  pure def ownedBy(c: Cell, u: UserId): bool =
    match c {
      | Active(x) => x == u
      | _ => false
    }

  pure def completeIfPassed(c: Cell, s: SlotId, t: int): Cell =
    match c {
      | Active(u) => if (s <= t) Completed(u) else c
      | _ => c
    }

  def cellOf(r: RoomId, s: SlotId): Cell = cells.get((r, s))

  def isFree(r: RoomId, s: SlotId): bool = cellOf(r, s) == Free

  def isActiveAt(r: RoomId, s: SlotId): bool = isActiveCell(cellOf(r, s))

  def isCompletedAt(r: RoomId, s: SlotId): bool = isCompletedCell(cellOf(r, s))

  def heldBy(u: UserId, r: RoomId, s: SlotId): bool = ownedBy(cellOf(r, s), u)

  def activeCount(u: UserId): int =
    ALL_CELLS.filter(k => ownedBy(cells.get(k), u)).size()

  def hasPassed(s: SlotId): bool = s <= now

  action init = all {
    cells' = ALL_CELLS.mapBy(k => Free),
    now' = 0,
  }

  /// @req REQ-001
  /// When a user reserves a room for a slot while the room is free in that
  /// slot, the system shall record an active reservation for that user.
  /// @title Reserve a free room
  /// @status specified
  /// @when a user reserves a room for a slot
  /// @while isFree(r, s) and not(hasPassed(s))
  /// @shall heldBy(u, r, s)
  ///
  /// @req REQ-002
  /// While a user holds 2 active reservations, the system shall refuse a
  /// further reservation by that user.
  /// @title Refuse a third reservation
  /// @status specified
  /// @modality forbidden
  /// @unwanted
  /// @when a user reserves a room for a slot
  /// @while activeCount(u) >= MAX_ACTIVE
  ///
  /// @req REQ-003
  /// While a room is already reserved in a slot, the system shall refuse a
  /// reservation of it for that slot by anyone.
  /// @title Refuse a taken room
  /// @status specified
  /// @modality forbidden
  /// @unwanted
  /// @when a user reserves a room for a slot
  /// @while not(isFree(r, s))
  action reserve(u: UserId, r: RoomId, s: SlotId): bool = all {
    isFree(r, s),
    not(hasPassed(s)),
    activeCount(u) < MAX_ACTIVE,
    cells' = cells.set((r, s), Active(u)),
    now' = now,
  }

  /// @req REQ-004
  /// While a reservation is active, when the user who made it cancels, the
  /// system shall free the room for that slot.
  /// @title Cancel frees the room
  /// @status specified
  /// @when the owner cancels a reservation
  /// @while heldBy(u, r, s)
  /// @shall isFree(r, s)
  ///
  /// @req REQ-005
  /// While a reservation is completed, the system shall refuse to cancel it.
  /// @title Completed cannot be cancelled
  /// @status specified
  /// @modality forbidden
  /// @unwanted
  /// @when a user cancels a reservation
  /// @while isCompletedAt(r, s)
  action cancel(u: UserId, r: RoomId, s: SlotId): bool = all {
    heldBy(u, r, s),
    cells' = cells.set((r, s), Free),
    now' = now,
  }

  /// @req REQ-006
  /// When a slot's time has passed, the system shall make every reservation
  /// for that slot completed.
  /// @title Passed slots complete
  /// @status specified
  /// @when time advances past a slot
  /// @while now < 3
  /// @shall ALL_CELLS.forall(k => hasPassed(k._2) implies not(isActiveCell(cells.get(k))))
  action tick: bool = all {
    now < 3,
    now' = now + 1,
    cells' = ALL_CELLS.mapBy(k => completeIfPassed(cells.get(k), k._2, now + 1)),
  }

  action step = {
    nondet u = oneOf(USERS)
    nondet r = oneOf(ROOMS)
    nondet s = oneOf(SLOTS)
    any {
      reserve(u, r, s),
      cancel(u, r, s),
      tick,
    }
  }

  /// @inv INV-001
  /// No room is held by two active reservations for the same slot.
  /// @criticality critical
  val noDoubleBooking: bool =
    ALL_CELLS.forall(k =>
      USERS.filter(u => ownedBy(cells.get(k), u)).size() <= 1)

  /// @inv INV-002
  /// No user holds more than MAX_ACTIVE active reservations.
  /// @criticality critical
  val userLimit: bool =
    USERS.forall(u => activeCount(u) <= MAX_ACTIVE)

  /// @inv INV-003
  /// No reservation is active for a slot whose time has passed.
  /// @criticality critical
  val noActivePast: bool =
    ALL_CELLS.forall(k => hasPassed(k._2) implies not(isActiveCell(cells.get(k))))

  val allInv: bool = noDoubleBooking and userLimit and noActivePast

  /// @example EX-001
  /// Reserve a room
  /// @refs REQ-001
  run reserveOk = init
    .then(reserve("alice", "r1", 2))
    .expect(heldBy("alice", "r1", 2) and activeCount("alice") == 1)

  /// @example EX-002
  /// A third reservation is refused
  /// @refs REQ-002
  run thirdRefused = init
    .then(reserve("alice", "r1", 1))
    .then(reserve("alice", "r1", 2))
    .expect(activeCount("alice") == 2)
    .then(reserve("alice", "r2", 3).fail())

  /// @example EX-003
  /// A taken room is refused
  /// @refs REQ-003
  run takenRefused = init
    .then(reserve("alice", "r1", 2))
    .then(reserve("bob", "r1", 2).fail())

  /// @example EX-004
  /// Cancel frees the room for another user
  /// @refs REQ-004
  run cancelFrees = init
    .then(reserve("alice", "r1", 2))
    .then(cancel("alice", "r1", 2))
    .expect(isFree("r1", 2))
    .then(reserve("bob", "r1", 2))
    .expect(heldBy("bob", "r1", 2))

  /// @example EX-005
  /// A completed reservation cannot be cancelled
  /// @refs REQ-005
  run completedNoCancel = init
    .then(reserve("alice", "r1", 1))
    .then(tick)
    .expect(isCompletedAt("r1", 1))
    .then(cancel("alice", "r1", 1).fail())

  /// @example EX-006
  /// Completed reservations stop counting against the limit
  /// @refs REQ-006
  run completedFreesLimit = init
    .then(reserve("alice", "r1", 1))
    .then(reserve("alice", "r2", 1))
    .then(tick)
    .expect(activeCount("alice") == 0)
    .then(reserve("alice", "r1", 2))
    .expect(activeCount("alice") == 1)
}
