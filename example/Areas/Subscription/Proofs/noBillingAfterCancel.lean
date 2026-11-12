import Areas.Subscription.Model

namespace Subscription.Proofs.noBillingAfterCancel
open Spec

def strong (s : State) : Prop :=
  ∀ c v, s.status.get? c = some v →
    (v = .Cancelled ∨ v = .Expired) → s.billingEnabled.get c = false

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
  intro c v h hcv
  simp only [FMap.get, FMap.get?, getList?] at h ⊢
  by_cases ha : c = "alice" <;> by_cases hb : c = "bob" <;> simp_all
  all_goals (subst h; simp at hcv)

theorem strong_step : ∀ a s t, strong s → apply a s = some t → strong t := by
  intro a s t hs h
  cases a with
  | cancel_subscription c n =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv hcv
    by_cases hd : d = c
    · subst hd; simp
    · simp [hd] at hv ⊢; exact hs d v hv hcv
  | cancel_declined c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv hcv
    simpa using hs d v hv hcv
  | cancel_unanswered c timedOut =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro d v hv hcv
    simpa using hs d v hv hcv
  | cancel_again c =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact hs
  | expire_period c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    intro d v hv hcv
    by_cases hd : d = c
    · subst hd
      have hc : s.status.get? d = some .Cancelled := by
        have hg' : s.status.get d = .Cancelled := by simpa using hg
        unfold FMap.get at hg'
        cases hx : s.status.get? d with
        | none => simp [hx] at hg'
        | some w => simp [hx] at hg'; simp [hg']
      simpa using hs d .Cancelled hc (Or.inl rfl)
    · simp [hd] at hv ⊢; exact hs d v hv hcv
  | request_refund c => simp [apply] at h

theorem noBillingAfterCancel_holds : ∀ s, Reachable init apply s → noBillingAfterCancel s = true := by
  intro s hr
  have hs := Reachable.induct strong strong_init strong_step s hr
  simp only [noBillingAfterCancel, FMap.keySet, FSet.all, List.all_eq_true]
  intro c hc
  obtain ⟨v, hv⟩ := FMap.get?_of_mem_keys hc
  have hg := FMap.get_of_get? hv
  rw [hg]
  cases v <;> simp [hs c _ hv]

end Subscription.Proofs.noBillingAfterCancel
