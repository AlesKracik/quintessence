import Areas.Reservation.Model

namespace Reservation.Proofs.holdLimit
open Spec Reservation

/-- What one stored slot adds to the number of seats `c0` holds right now. -/
def f (now : Int) (c0 : Customer) : Slot → Nat
  | .Held c' d => if d ≤ now then 0 else if c' = c0 then 1 else 0
  | _ => 0

/-- What a caller sees of a stored slot at time `now`. -/
def eff (now : Int) : Slot → Slot
  | .Held c' d => if d ≤ now then .Free else .Held c' d
  | x => x

/-- The model never adds or removes a seat, so its map always has this shape. -/
def Shape (s : State) (a b c : Slot) : Prop :=
  s.seats.entries = [("s1", a), ("s2", b), ("s3", c)]

def P (s : State) : Prop :=
  ∃ a b c, Shape s a b c ∧ ∀ c0, f s.now c0 a + f s.now c0 b + f s.now c0 c ≤ MAX_HOLDS

theorem effective_s1 {s : State} {a b c : Slot} (h : Shape s a b c) :
    effective s "s1" = eff s.now a := by
  simp only [Shape] at h
  simp only [effective, FMap.get?, h, getList?, if_true]
  cases a <;> rfl

theorem effective_s2 {s : State} {a b c : Slot} (h : Shape s a b c) :
    effective s "s2" = eff s.now b := by
  simp only [Shape] at h
  simp only [effective, FMap.get?, h, getList?]
  simp
  cases b <;> rfl

theorem effective_s3 {s : State} {a b c : Slot} (h : Shape s a b c) :
    effective s "s3" = eff s.now c := by
  simp only [Shape] at h
  simp only [effective, FMap.get?, h, getList?]
  simp
  cases c <;> rfl

theorem heldBy_eq {s : State} {a b c : Slot} (h : Shape s a b c) (c0 : Customer) :
    heldBy s c0 = f s.now c0 a + f s.now c0 b + f s.now c0 c := by
  have e : s.seats.keys = ["s1", "s2", "s3"] := by simp [FMap.keys, Shape] at *; simp [h]
  simp only [heldBy, e, List.filter, effective_s1 h, effective_s2 h, effective_s3 h]
  cases a <;> cases b <;> cases c <;> simp [f, eff]
  all_goals (repeat' split) <;> simp_all

theorem effective_other {s : State} {a b c : Slot} (h : Shape s a b c) {seat : Seat}
    (h1 : seat ≠ "s1") (h2 : seat ≠ "s2") (h3 : seat ≠ "s3") : effective s seat = .Free := by
  simp only [Shape] at h
  simp [effective, FMap.get?, h, getList?, h1, h2, h3]

theorem seat_cases {s : State} {a b c : Slot} (h : Shape s a b c) (seat : Seat)
    (hne : effective s seat ≠ .Free) : seat = "s1" ∨ seat = "s2" ∨ seat = "s3" := by
  refine Classical.byContradiction fun hn => ?_
  simp only [not_or] at hn
  exact hne (effective_other h hn.1 hn.2.1 hn.2.2)

theorem shape_set1 {s : State} {a b c : Slot} (h : Shape s a b c) (x : Slot) :
    Shape { s with seats := s.seats.set "s1" x } x b c := by
  simp only [Shape] at *
  simp [FMap.set, h, putList]

theorem shape_set2 {s : State} {a b c : Slot} (h : Shape s a b c) (x : Slot) :
    Shape { s with seats := s.seats.set "s2" x } a x c := by
  simp only [Shape] at *
  simp [FMap.set, h, putList]

theorem shape_set3 {s : State} {a b c : Slot} (h : Shape s a b c) (x : Slot) :
    Shape { s with seats := s.seats.set "s3" x } a b x := by
  simp only [Shape] at *
  simp [FMap.set, h, putList]

theorem f_new (now : Int) (c0 c1 : Customer) :
    f now c1 (.Held c0 (now + HOLD_TTL)) = if c0 = c1 then 1 else 0 := by
  have : ¬ (now + HOLD_TTL ≤ now) := by unfold HOLD_TTL; omega
  simp [f, this]

theorem f_free {s : State} {a : Slot}
    (he : eff s.now a = Slot.Free)
    (c1 : Customer) : f s.now c1 a = 0 := by
  cases a with
  | Free => rfl
  | Sold _ => rfl
  | Held c' d =>
    simp only [eff] at he
    split at he
    · simp [f, *]
    · simp at he

theorem f_mono (now : Int) (c1 : Customer) (x : Slot) : f (now + 1) c1 x ≤ f now c1 x := by
  cases x with
  | Free => simp [f]
  | Sold _ => simp [f]
  | Held c' d =>
    simp only [f]
    split <;> split <;> (try split) <;> omega

theorem P_replace {s : State} (hP : P s) (seat : Seat) (x : Slot)
    (hx : ∀ now c1, f now c1 x = 0) (hne : effective s seat ≠ .Free) :
    P { s with seats := s.seats.set seat x } := by
  obtain ⟨a, b, c, hsh, hs⟩ := hP
  rcases seat_cases hsh seat hne with rfl | rfl | rfl
  · exact ⟨x, b, c, shape_set1 hsh x, fun c1 => by have := hs c1; simp only [hx]; omega⟩
  · exact ⟨a, x, c, shape_set2 hsh x, fun c1 => by have := hs c1; simp only [hx]; omega⟩
  · exact ⟨a, b, x, shape_set3 hsh x, fun c1 => by have := hs c1; simp only [hx]; omega⟩

theorem P_init : P init := by
  refine ⟨.Free, .Free, .Free, by simp only [Shape]; rfl, fun c0 => by simp [f]⟩

theorem P_step : ∀ a s t, P s → apply a s = some t → P t := by
  intro a s t hP h
  cases a with
  | hold seat c0 =>
    simp only [apply] at h
    split at h
    · simp only [Option.some.injEq] at h
      subst h
      rename_i hg
      simp only [Bool.and_eq_true, decide_eq_true_eq, beq_iff_eq] at hg
      obtain ⟨⟨hk, he⟩, hl⟩ := hg
      obtain ⟨a, b, c, hsh, hs⟩ := hP
      rw [heldBy_eq hsh] at hl
      have hm : seat = "s1" ∨ seat = "s2" ∨ seat = "s3" := by
        refine Classical.byContradiction fun hn => ?_
        simp only [not_or] at hn
        have := effective_other hsh hn.1 hn.2.1 hn.2.2
        simp only [Shape] at hsh
        simp [known, FMap.contains, FMap.get?, hsh, getList?, hn.1, hn.2.1, hn.2.2] at hk
      rcases hm with rfl | rfl | rfl
      · rw [effective_s1 hsh] at he
        refine ⟨.Held c0 (s.now + HOLD_TTL), b, c, shape_set1 hsh _, fun c1 => ?_⟩
        have := hs c1
        have ha := f_free he c1
        rw [f_new]
        by_cases hc : c0 = c1
        · subst hc; simp only [if_true]; have := f_free he c0; omega
        · simp only [hc, if_false]; omega
      · rw [effective_s2 hsh] at he
        refine ⟨a, .Held c0 (s.now + HOLD_TTL), c, shape_set2 hsh _, fun c1 => ?_⟩
        have := hs c1
        have ha := f_free he c1
        rw [f_new]
        by_cases hc : c0 = c1
        · subst hc; simp only [if_true]; have := f_free he c0; omega
        · simp only [hc, if_false]; omega
      · rw [effective_s3 hsh] at he
        refine ⟨a, b, .Held c0 (s.now + HOLD_TTL), shape_set3 hsh _, fun c1 => ?_⟩
        have := hs c1
        have ha := f_free he c1
        rw [f_new]
        by_cases hc : c0 = c1
        · subst hc; simp only [if_true]; have := f_free he c0; omega
        · simp only [hc, if_false]; omega
    · simp at h
  | confirm seat c0 =>
    simp only [apply] at h
    generalize hE : effective s seat = E at h
    cases E <;> simp at h
    obtain ⟨_, rfl⟩ := h
    exact P_replace hP seat _ (fun _ _ => rfl) (by rw [hE]; simp)
  | cancel seat c0 =>
    simp only [apply] at h
    generalize hE : effective s seat = E at h
    cases E <;> simp at h <;>
      (obtain ⟨_, rfl⟩ := h
       exact P_replace hP seat _ (fun _ _ => rfl) (by rw [hE]; simp))
  | tick =>
    simp only [apply, Option.some.injEq] at h
    subst h
    obtain ⟨a, b, c, hsh, hs⟩ := hP
    refine ⟨a, b, c, hsh, fun c1 => ?_⟩
    have := hs c1
    have := f_mono s.now c1 a
    have := f_mono s.now c1 b
    have := f_mono s.now c1 c
    simp only
    omega

theorem holdLimit_holds : ∀ s, Reachable init apply s → holdLimit s = true := by
  intro s hr
  obtain ⟨a, b, c, hsh, hs⟩ := Reachable.induct P P_init P_step s hr
  unfold holdLimit
  simp only [List.all_eq_true]
  intro k _
  split
  · simp only [decide_eq_true_eq, heldBy_eq hsh]
    exact hs _
  · rfl

end Reservation.Proofs.holdLimit
