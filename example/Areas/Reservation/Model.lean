import Spec.Data

/-! The reservation model: state, actions, one step, and the invariant predicates.
It follows `examples/python/reservation.py`, including its quirks. -/

namespace Reservation
open Spec

abbrev Seat := String
abbrev Customer := String

/-- What the service stores for a seat. A hold is stored with the time it runs out. -/
inductive Slot where
  | Free
  | Held (customer : Customer) (deadline : Int)
  | Sold (customer : Customer)
  deriving DecidableEq, Repr, Inhabited, Hashable

/-- Time units a hold stays valid. -/
def HOLD_TTL : Int := 2

/-- Seats one customer may hold at the same time. -/
def MAX_HOLDS : Nat := 2

structure State where
  seats : FMap Seat Slot
  now : Int
  deriving DecidableEq, Repr, Hashable

inductive Action where
  | hold (seat : Seat) (customer : Customer)
  | confirm (seat : Seat) (customer : Customer)
  | cancel (seat : Seat) (customer : Customer)
  | tick
  deriving Repr

/-- Three seats; the model never adds or removes one. -/
def seatNames : List Seat := ["s1", "s2", "s3"]

def init : State :=
  { seats := FMap.ofList (seatNames.map fun x => (x, Slot.Free)), now := 0 }

/-- What a caller sees of a seat: a hold whose time has run out (deadline at or
before now) reads as free. -/
def effective (s : State) (seat : Seat) : Slot :=
  match s.seats.get? seat with
  | some (.Held c u) => if u ≤ s.now then .Free else .Held c u
  | some x => x
  | none => .Free

def known (s : State) (seat : Seat) : Bool := s.seats.contains seat

/-- The seats `c` holds right now. -/
def heldBy (s : State) (c : Customer) : Nat :=
  (s.seats.keys.filter fun k => match effective s k with
    | .Held c' _ => c' == c
    | _ => false).length

def statusOf (s : State) (seat : Seat) : String :=
  match effective s seat with
  | .Free => "free"
  | .Held _ _ => "held"
  | .Sold _ => "sold"

def ownerOf (s : State) (seat : Seat) : String :=
  match effective s seat with
  | .Free => "none"
  | .Held c _ => c
  | .Sold c => c

/-- A customer never has more than `MAX_HOLDS` seats on hold. -/
def holdLimit (s : State) : Bool :=
  s.seats.keys.all fun k => match effective s k with
    | .Held c _ => decide (heldBy s c ≤ MAX_HOLDS)
    | _ => true

/-- No hold runs longer than `HOLD_TTL` from now. -/
def holdDeadline (s : State) : Bool :=
  s.seats.keys.all fun k => match s.seats.get? k with
    | some (.Held _ u) => decide (u ≤ s.now + HOLD_TTL)
    | _ => true

/-- One step. `none` = the action is refused. -/
def apply : Action → State → Option State
  | .hold seat c, s =>
    if known s seat && effective s seat == .Free && decide (heldBy s c < MAX_HOLDS) then
      some { s with seats := s.seats.set seat (.Held c (s.now + HOLD_TTL)) }
    else none
  | .confirm seat c, s =>
    match effective s seat with
    | .Held c' _ =>
      if c' == c then some { s with seats := s.seats.set seat (.Sold c) } else none
    | _ => none
  | .cancel seat c, s =>
    match effective s seat with
    | .Held c' _ | .Sold c' =>
      if c' == c then some { s with seats := s.seats.set seat .Free } else none
    | .Free => none
  | .tick, s => some { s with now := s.now + 1 }

end Reservation
