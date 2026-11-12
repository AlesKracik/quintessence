import Areas.Reservation.Model

namespace Reservation.Proofs.holdDeadline
open Spec Reservation

def P (s : State) : Prop :=
  ∀ k c d, s.seats.get? k = some (Slot.Held c d) → d ≤ s.now + HOLD_TTL

theorem P_init : P init := by
  intro k c d h
  have e : init.seats.entries = [("s1", Slot.Free), ("s2", Slot.Free), ("s3", Slot.Free)] := by decide
  simp only [FMap.get?, e, getList?] at h
  split at h <;> (try split at h) <;> (try split at h) <;> simp at h

theorem P_set {s : State} (hP : P s) (seat : Seat) (x : Slot)
    (hx : ∀ c d, x ≠ Slot.Held c d) : P { s with seats := s.seats.set seat x } := by
  intro k c d hk
  simp only [FMap.get?_set] at hk ⊢
  by_cases hks : k = seat
  · simp [hks] at hk
    exact absurd hk (fun e => hx c d e)
  · simp [hks] at hk
    exact hP k c d hk

theorem P_step : ∀ a s t, P s → apply a s = some t → P t := by
  intro a s t hP h
  cases a with
  | hold seat c =>
    simp only [apply] at h
    split at h
    · simp only [Option.some.injEq] at h
      subst h
      intro k c' d hk
      simp only [FMap.get?_set] at hk ⊢
      by_cases hks : k = seat
      · simp [hks] at hk
        omega
      · simp [hks] at hk
        exact hP k c' d hk
    · simp at h
  | confirm seat c =>
    simp only [apply] at h
    split at h <;> simp at h
    obtain ⟨_, rfl⟩ := h
    exact P_set hP seat (.Sold c) (by simp)
  | cancel seat c =>
    simp only [apply] at h
    split at h <;> simp at h <;> (obtain ⟨_, rfl⟩ := h; exact P_set hP seat .Free (by simp))
  | tick =>
    simp only [apply, Option.some.injEq] at h
    subst h
    intro k c d hk
    have := hP k c d hk
    simp only
    omega

theorem holdDeadline_holds : ∀ s, Reachable init apply s → holdDeadline s = true := by
  intro s hr
  have hP := Reachable.induct P P_init P_step s hr
  unfold holdDeadline
  simp only [List.all_eq_true]
  intro k hk
  obtain ⟨v, hv⟩ := FMap.get?_of_mem_keys hk
  rw [hv]
  cases v with
  | Held c d => simpa using hP k c d hv
  | Free => simp
  | Sold c => simp

end Reservation.Proofs.holdDeadline
