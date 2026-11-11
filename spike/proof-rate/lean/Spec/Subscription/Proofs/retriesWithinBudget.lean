import Spec.Subscription.Model

namespace Spec.Subscription
open Quint

-- Helper definitions and lemmas go here.

def strong (s : State) : Prop :=
  ∀ c v, s.unansweredTries.get? c = some v → v ≤ 3

theorem strong_init : strong init := by
  have : init.unansweredTries = ⟨[("alice", 0), ("bob", 0)]⟩ := by decide
  intro c v h
  rw [this] at h
  simp only [QMap.get?, getList?] at h
  by_cases ha : c = "alice" <;> by_cases hb : c = "bob" <;> simp_all <;> omega

theorem triesOf_le (s : State) (h : strong s) (c : CustomerId) : triesOf s c ≤ 3 := by
  unfold triesOf
  split
  · rename_i hc
    have this : c ∈ s.unansweredTries.keys := by
      simpa [QSet.contains, QMap.keySet] using hc
    obtain ⟨v, hv⟩ := QMap.get?_of_mem_keys this
    rw [QMap.get_of_get? hv]; exact h c v hv
  · decide

theorem strong_step : ∀ a s t, strong s → apply a s = some t → strong t := by
  intro a s t hs h
  cases a with
  | cancel_again c =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact hs
  | cancel_declined c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv
    by_cases hd : d = c
    · subst hd; simp at hv; omega
    · simp [hd] at hv; exact hs d v hv
  | cancel_subscription c n =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv
    by_cases hd : d = c
    · subst hd; simp at hv; omega
    · simp [hd] at hv; exact hs d v hv
  | cancel_unanswered c timedOut =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    intro d v hv
    by_cases hd : d = c
    · subst hd; simp at hv
      simp at hg
      have := hg.2
      unfold MAX_CANCEL_RETRIES at this
      omega
    · simp [hd] at hv; exact hs d v hv
  | expire_period c =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact hs
  | request_refund c => simp [apply] at h

theorem retriesWithinBudget_holds : ∀ s, Reachable init apply s → retriesWithinBudget s = true := by
  intro s hr
  have hs := Reachable.induct strong strong_init strong_step s hr
  simp only [retriesWithinBudget, QSet.all, List.all_eq_true, decide_eq_true_eq]
  intro c _
  have := triesOf_le s hs c
  unfold MAX_CANCEL_RETRIES
  omega

end Spec.Subscription
