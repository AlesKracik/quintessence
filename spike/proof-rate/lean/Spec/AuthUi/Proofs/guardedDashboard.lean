import Spec.AuthUi.Model

namespace Spec.AuthUi
open Quint

theorem mem_putList (l : List (String × auth.SessionStatus)) (k : String) (v : auth.SessionStatus) :
    k ∈ (putList l k v).map Prod.fst := by
  induction l with
  | nil => simp [putList]
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    by_cases h1 : k < a
    · simp [putList, h1]
    · by_cases h2 : k = a
      · simp [putList, h1, h2]
      · simp [putList, h1, h2, ih]

theorem contains_set (m : QMap String auth.SessionStatus) (k : String) (v : auth.SessionStatus) :
    ((m.set k v).keySet).contains k = true := by
  obtain ⟨l⟩ := m
  simp only [QMap.set, QMap.keySet, QMap.keys, QSet.contains, List.contains_eq_any_beq]
  have := mem_putList l k v
  simp only [List.any_eq_true, beq_iff_eq]
  exact ⟨k, this, rfl⟩

theorem step_ok : ∀ a s t, guardedDashboard s = true → apply a s = some t →
    guardedDashboard t = true := by
  intro a s t hs h
  cases a with
  | authUnchanged => simp only [apply] at h; cases h; exact hs
  | click_logout =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard]
  | click_sign_in =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard]
  | go_home =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard]
  | session_expired =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard]
  | submit_invalid u =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard]
  | submit_locked u =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard]
  | submit_success u s_ =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simp [guardedDashboard, authenticated, auth.isActive, auth.statusOf, contains_set]
  | time_passes =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    simpa [guardedDashboard, authenticated, auth.isActive, auth.statusOf] using hs

theorem init_ok : guardedDashboard init = true := by
  decide

theorem guardedDashboard_holds : ∀ s, Reachable init apply s → guardedDashboard s = true :=
  Reachable.induct _ init_ok step_ok

end Spec.AuthUi
