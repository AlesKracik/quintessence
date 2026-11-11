import Spec.Subscription.Model

namespace Spec.Subscription
open Quint

def strong (s : State) : Prop :=
  ∀ c v, s.status.get? c = some v → v = .Cancelled → s.accessUntilPeriodEnd.get c = true

theorem init_eq : init =
    { status               := ⟨[("alice", .Active), ("bob", .Active)]⟩
      billingEnabled       := ⟨[("alice", true), ("bob", true)]⟩
      accessUntilPeriodEnd := ⟨[("alice", false), ("bob", false)]⟩
      lastBillingResult    := ⟨[("alice", .Success), ("bob", .Success)]⟩
      unansweredTries      := ⟨[("alice", 0), ("bob", 0)]⟩
      notice               := ⟨[("alice", .NoNotice), ("bob", .NoNotice)]⟩ } := by
  decide

theorem strong_init : strong init := by
  rw [init_eq]
  intro c v h hc
  subst hc
  simp only [QMap.get?, getList?] at h
  by_cases ha : c = "alice" <;> by_cases hb : c = "bob" <;> simp_all

theorem strong_step : ∀ a s t, strong s → apply a s = some t → strong t := by
  intro a s t hs h
  cases a with
  | cancel_subscription c n =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv hc
    by_cases hd : d = c
    · subst hd; simp
    · simp [hd] at hv ⊢; exact hs d v hv hc
  | cancel_declined c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact hs
  | cancel_unanswered c timedOut =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact hs
  | cancel_again c =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact hs
  | expire_period c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv hc
    by_cases hd : d = c
    · subst hd; simp at hv; subst hv; cases hc
    · simp [hd] at hv ⊢; exact hs d v hv hc
  | request_refund c => simp [apply] at h

theorem noRefundIssued_holds : ∀ s, Reachable init apply s → noRefundIssued s = true := by
  intro s hr
  have hs := Reachable.induct strong strong_init strong_step s hr
  simp only [noRefundIssued, QMap.keySet, QSet.all, List.all_eq_true]
  intro c hc
  obtain ⟨v, hv⟩ := QMap.get?_of_mem_keys hc
  rw [QMap.get_of_get? hv]
  by_cases h : v = .Cancelled
  · subst h; simp [hs c _ hv rfl]
  · simp [h]

end Spec.Subscription
