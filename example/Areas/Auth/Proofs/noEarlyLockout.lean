import Areas.Auth.Model

namespace Auth.Proofs.noEarlyLockout
open Spec

theorem contains_aux {α β : Type} [DecidableEq α] (l : List (α × β)) (k : α) :
    (l.map Prod.fst).contains k = (getList? l k).isSome := by
  induction l with
  | nil => simp [getList?]
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    by_cases h : k = a
    · simp [getList?, h]
    · have h' : ¬ a = k := fun e => h e.symm
      have := ih
      simp only [getList?, if_neg h, List.map_cons, List.contains_cons, beq_iff_eq, h', Bool.false_or, this]
      have hk : (k == a) = false := by simp [h]
      rw [hk]; rfl

theorem keySet_contains {α β : Type} [DecidableEq α] (m : FMap α β) (k : α) :
    (m.keySet).contains k = (m.get? k).isSome := by
  simp only [FMap.keySet, FSet.contains, FMap.keys, FMap.get?]
  exact contains_aux m.entries k

theorem isLocked_iff (s : State) (u : UserId) :
    isLocked s u = true ↔ s.accountStatus.get? u = some .Locked := by
  unfold isLocked
  rw [keySet_contains]
  cases h : s.accountStatus.get? u with
  | none => simp
  | some v => simp [FMap.get, h]

theorem attemptsOf_eq (s : State) (u : UserId) :
    attemptsOf s u = (s.failedAttempts.get? u).getD 0 := by
  unfold attemptsOf
  rw [keySet_contains]
  cases h : s.failedAttempts.get? u with
  | none => simp
  | some v => simp [FMap.get, h]

def strong (s : State) : Prop :=
  ∀ u, s.accountStatus.get? u = some .Locked →
    ∃ w, s.failedAttempts.get? u = some w ∧ w ≥ 5

theorem strong_init : strong init := by
  intro u h
  simp [init, FMap.empty, FMap.get?, getList?] at h

theorem strong_step : ∀ a s t, strong s → apply a s = some t → strong t := by
  intro a s t hs h
  cases a with
  | expire_session sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact hs
  | login uid sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    intro u hu
    by_cases hd : u = uid
    · subst hd
      exfalso
      have : isLocked s u = true := (isLocked_iff s u).2 (by simpa using hu)
      simp_all
    · simp [hd] at hu ⊢; exact hs u hu
  | login_failed uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    intro u hu
    by_cases hd : u = uid
    · subst hd
      simp [attemptsOf_eq, MAX_FAILED_ATTEMPTS] at hu ⊢
      omega
    · simp [hd] at hu ⊢; exact hs u hu
  | logout sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact hs
  | tick =>
    simp only [apply] at h; simp at h; subst h; exact hs
  | unlock_account uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro u hu
    by_cases hd : u = uid
    · subst hd; simp at hu
    · simp [hd] at hu ⊢; exact hs u hu

theorem noEarlyLockout_holds : ∀ s, Reachable init apply s → noEarlyLockout s = true := by
  intro s hr
  have hs := Reachable.induct strong strong_init strong_step s hr
  simp only [noEarlyLockout, FMap.keySet, FSet.all, List.all_eq_true]
  intro u hu
  obtain ⟨v, hv⟩ := FMap.get?_of_mem_keys hu
  by_cases hl : isLocked s u = true
  · obtain ⟨w, hw, h5⟩ := hs u ((isLocked_iff s u).1 hl)
    rw [hv] at hw
    cases hw
    simp [attemptsOf_eq, hv, MAX_FAILED_ATTEMPTS]
    omega
  · simp [hl]

end Auth.Proofs.noEarlyLockout
