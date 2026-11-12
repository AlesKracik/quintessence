import Spec.Check
import Areas.Reservation.Model
import Areas.Reservation.Proofs.holdLimit
import Areas.Reservation.Proofs.holdDeadline

/-! The reservation area: what is claimed about the seat reservation service, and the scope it is searched in. -/

namespace Reservation
open Spec

def customers : List Customer := ["alice", "bob"]

def seatView (seat : Seat) (s : State) : String :=
  match effective s seat with
  | .Free => "free"
  | .Held c _ => s!"held by {c}"
  | .Sold c => s!"sold to {c}"

def area : Area State Action where
  name := "reservation"
  init := init
  step := apply
  actions :=
    (seatNames.flatMap fun x => customers.flatMap fun c =>
      [Action.hold x c, .confirm x c, .cancel x c]) ++ [.tick]
  -- the clock is unbounded; the search stops at time 3, one unit after the
  -- first hold made at time 0 has run out and before later holds expire
  bound := fun s => decide (s.now ≤ 3)
  invariants := [
    { id := "INV-001",
      sources := ["code: app/reservation.py, hold (MAX_HOLDS)"], text := "A customer never has more than two seats on hold at the same time.",
      holds := holdLimit,
      proof := some ⟨Proofs.holdLimit.holdLimit_holds⟩ },
    { id := "INV-002",
      sources := ["code: app/reservation.py, hold (HOLD_TTL)"], text := "No hold is valid for longer than two time units from now.",
      holds := holdDeadline,
      proof := some ⟨Proofs.holdDeadline.holdDeadline_holds⟩ }]
  requirements := [
    { id := "REQ-001",
      sources := ["code: app/reservation.py, hold"],
      text := "When a customer asks to hold a free seat and has fewer than two seats on hold, the seat is held for that customer for two more time units.",
      when := fun s a => match a with
        | .hold x c => known s x && effective s x == .Free && heldBy s c < MAX_HOLDS
        | _ => false,
      shall := fun s a t => match a with
        | .hold x c => effective t x == .Held c (s.now + HOLD_TTL)
        | _ => true },
    { id := "REQ-002",
      sources := ["code: app/reservation.py, hold"],
      text := "A seat that is held (by anyone, including the asking customer) or sold cannot be held.",
      when := fun s a => match a with
        | .hold x _ => effective s x != .Free
        | _ => false,
      refused := true },
    { id := "REQ-003",
      sources := ["code: app/reservation.py, hold (MAX_HOLDS)"],
      text := "A customer who already has two seats on hold cannot hold a third, even when the seat is free.",
      when := fun s a => match a with
        | .hold x c => effective s x == .Free && heldBy s c ≥ MAX_HOLDS
        | _ => false,
      refused := true },
    { id := "REQ-004",
      sources := ["code: app/reservation.py, confirm"],
      text := "When a customer confirms a seat they have on hold, the seat is sold to them.",
      when := fun s a => match a with
        | .confirm x c => match effective s x with
          | .Held c' _ => c' == c
          | _ => false
        | _ => false,
      shall := fun _ a t => match a with
        | .confirm x c => effective t x == .Sold c
        | _ => true },
    { id := "REQ-005",
      sources := ["code: app/reservation.py, confirm"],
      text := "Confirming a seat that is free, sold, held by someone else, or whose hold has run out is refused.",
      when := fun s a => match a with
        | .confirm x c => match effective s x with
          | .Held c' _ => c' != c
          | _ => true
        | _ => false,
      refused := true },
    { id := "REQ-006",
      sources := ["code: app/reservation.py, cancel"],
      text := "When a customer cancels a seat that is held or sold to them, the seat becomes free.",
      when := fun s a => match a with
        | .cancel x c => match effective s x with
          | .Held c' _ => c' == c
          | .Sold c' => c' == c
          | .Free => false
        | _ => false,
      shall := fun _ a t => match a with
        | .cancel x _ => effective t x == .Free
        | _ => true },
    { id := "REQ-007",
      sources := ["code: app/reservation.py, cancel"],
      text := "Cancelling a seat that is free (including one whose hold has run out) or that belongs to another customer is refused.",
      when := fun s a => match a with
        | .cancel x c => match effective s x with
          | .Held c' _ => c' != c
          | .Sold c' => c' != c
          | .Free => true
        | _ => false,
      refused := true },
    { id := "REQ-008",
      sources := ["code: the clock passed to ReservationService"],
      text := "Time can always pass: a tick moves the clock forward by one unit.",
      when := fun _ a => match a with
        | .tick => true
        | _ => false,
      shall := fun s _ t => t.now == s.now + 1 },
    { id := "REQ-009",
      sources := ["code: app/reservation.py, _entry (HOLD_TTL)"],
      text := "A hold that has not been confirmed expires when the clock reaches its deadline, and the seat then reads as free.",
      when := fun s a => match a with
        | .tick => match effective s "s1" with
          | .Held _ u => u == s.now + 1
          | _ => false
        | _ => false,
      shall := fun _ _ t => effective t "s1" == .Free }]
  journeys := [
    { id := "J-001",
      sources := ["written for this example"], title := "Buying seats",
      goal := "A customer picks seats, gets a moment to pay, and ends up owning them.",
      steps := [
        .step "The customer puts a seat on hold." [.hold "s1" "alice", .hold "s2" "alice"],
        .step "The customer confirms a seat on hold; it is theirs." [.confirm "s1" "alice", .confirm "s2" "alice"]] },
    { id := "J-002",
      sources := ["written for this example"], title := "Giving a seat back",
      goal := "A customer who bought a seat changes their mind, and the seat goes back on sale.",
      given := "Alice has bought seat s1.",
      setup := [.hold "s1" "alice", .confirm "s1" "alice"],
      steps := [
        .step "The customer cancels the seat; it is free again." [.cancel "s1" "alice"],
        .step "Another customer can now put it on hold." [.hold "s1" "bob"]] }]
  walkthroughs := [
    { id := "W-001", journey := "J-001",
      sources := ["written for this example"], title := "Two people want the same seat",
      goal := "Alice wants seat s1 and needs a moment to pay; Bob wants it too.",
      moments := [
        .step "Alice puts seat s1 on hold." (.hold "s1" "alice"),
        .refusal "Bob wants the same seat and is turned away." (.hold "s1" "bob"),
        .refusal "Bob cannot confirm Alice's hold either." (.confirm "s1" "bob"),
        .step "A moment passes while she pays." .tick,
        .outcome "Alice confirms; the seat is sold to her." (.confirm "s1" "alice")
          (fun s => statusOf s "s1" == "sold" && ownerOf s "s1" == "alice"),
        .refusal "Bob still cannot have it." (.hold "s1" "bob")] },
    { id := "W-002", journey := "J-001",
      sources := ["written for this example"], title := "A hold that runs out",
      goal := "Alice puts a seat on hold and then does nothing.",
      moments := [
        .step "Alice puts seat s1 on hold." (.hold "s1" "alice"),
        .step "Time passes." .tick,
        .refusal "One moment is not enough for the hold to run out: Bob is still turned away." (.hold "s1" "bob"),
        .outcome "More time passes; the hold has run out and the seat is free." .tick
          (fun s => statusOf s "s1" == "free"),
        .refusal "Alice tries to confirm, too late." (.confirm "s1" "alice"),
        .outcome "Bob takes the seat instead." (.hold "s1" "bob")
          (fun s => ownerOf s "s1" == "bob" && statusOf s "s1" == "held")] },
    { id := "W-003", journey := "J-001",
      sources := ["written for this example"], title := "No more than two seats on hold",
      goal := "Alice tries to keep more seats on hold than a customer may.",
      moments := [
        .step "Alice puts seat s1 on hold." (.hold "s1" "alice"),
        .refusal "Holding the seat she already holds is turned down." (.hold "s1" "alice"),
        .step "She puts seat s2 on hold." (.hold "s2" "alice"),
        .refusal "A third seat is turned down: two on hold is the limit." (.hold "s3" "alice"),
        .step "She confirms seat s1." (.confirm "s1" "alice"),
        .outcome "With one seat bought she has room again, and holds seat s3." (.hold "s3" "alice")
          (fun s => heldBy s "alice" == 2 && statusOf s "s1" == "sold")] },
    { id := "W-004", journey := "J-002",
      sources := ["written for this example"], title := "Changing one's mind",
      goal := "Alice buys a seat and later gives it back.",
      moments := [
        .step "Alice puts seat s2 on hold." (.hold "s2" "alice"),
        .step "She confirms it." (.confirm "s2" "alice"),
        .outcome "She cancels the sold seat, and it is free again." (.cancel "s2" "alice")
          (fun s => statusOf s "s2" == "free"),
        .step "Bob can now hold it." (.hold "s2" "bob")] }]
  views := seatNames.map (fun x => (s!"seat {x}", seatView x))
    ++ customers.map (fun c => (s!"seats on hold for {c}", fun s => toString (heldBy s c)))
  assumptions := [
    "Three seats and two customers (alice, bob) stand for any number; three seats are needed to show the limit of two holds.",
    "A hold lasts 2 units of time here; the search stops at time 3, so later holds are never seen to expire.",
    "Payment is outside the model: confirming a held seat always succeeds."]
  code := some {
    adapter := ["python3", "adapters/python.py"]
    paths := ["app/reservation.py"]
    setup := "from app.reservation import ReservationService
class Clock:
    now = 0
    def tick(self):
        self.now += 1
clock = Clock()
system = ReservationService(['s1', 's2', 's3'], lambda: clock.now)"
    actions := [
      ("hold", "system.hold({0}, {1})"),
      ("confirm", "system.confirm({0}, {1})"),
      ("cancel", "system.cancel({0}, {1})"),
      ("tick", "clock.tick()")]
    observe := seatNames.flatMap fun x => [
      (s!"status {x}", fun s => statusOf s x, s!"system.status('{x}')"),
      (s!"owner {x}", fun s => ownerOf s x, s!"system.owner('{x}')")]
    budget := 300000 }

def entry : Entry := area.entry

end Reservation
