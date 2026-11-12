import Areas.Subscription.Model

namespace Subscription.Proofs.unacceptedCancelLeavesActive
open Spec

def strong (s : State) : Prop :=
  (∀ c r, s.lastBillingResult.get? c = some r → r ≠ .Success →
    ∀ v, s.status.get? c = some v → v = .Active ∧ s.billingEnabled.get c = true) ∧
  (∀ c v, s.status.get? c = some v → v = .Active → s.billingEnabled.get c = true)

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
  constructor
  · intro c r h hr v hv
    simp only [FMap.get?, getList?] at h
    by_cases ha : c = "alice" <;> by_cases hb : c = "bob" <;> simp_all
  · intro c v h hv
    simp only [FMap.get, FMap.get?, getList?] at h ⊢
    by_cases ha : c = "alice" <;> by_cases hb : c = "bob" <;> simp_all

theorem get_some (m : FMap CustomerId SubStatus) (d : CustomerId) (x : SubStatus)
    (hg : m.get d = x) (hne : x ≠ .Active) : m.get? d = some x := by
  unfold FMap.get at hg
  cases hx : m.get? d with
  | none => simp [hx] at hg; exact absurd hg.symm hne
  | some w => simp [hx] at hg; simp [hg]

theorem strong_step : ∀ a s t, strong s → apply a s = some t → strong t := by
  intro a s t hs h
  obtain ⟨h1, h2⟩ := hs
  cases a with
  | cancel_subscription c n =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    constructor
    · intro d r hv hr w hw
      by_cases hd : d = c
      · subst hd; simp at hv; exact absurd hv.symm hr
      · simp [hd] at hv hw ⊢; exact h1 d r hv hr w hw
    · intro d v hv hva
      by_cases hd : d = c
      · subst hd; simp at hv; subst hv; simp at hva
      · simp [hd] at hv ⊢; exact h2 d v hv hva
  | cancel_declined c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    constructor
    · intro d r hv hr w hw
      by_cases hd : d = c
      · subst hd
        simp at hw
        have hw' : w = .Active := by
          have : s.status.get d = w := FMap.get_of_get? hw
          rw [(by simpa using hg : s.status.get d = .Active)] at this; exact this.symm
        subst hw'
        exact ⟨rfl, h2 d _ hw rfl⟩
      · simp [hd] at hv hw ⊢; exact h1 d r hv hr w hw
    · simpa using h2
  | cancel_unanswered c timedOut =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    have hg : (s.status.get c == SubStatus.Active) = true := by simp at hg; simpa using hg.1
    constructor
    · intro d r hv hr w hw
      by_cases hd : d = c
      · subst hd
        simp at hw
        have hw' : w = .Active := by
          have : s.status.get d = w := FMap.get_of_get? hw
          rw [(by simpa using hg : s.status.get d = .Active)] at this; exact this.symm
        subst hw'
        exact ⟨rfl, h2 d _ hw rfl⟩
      · simp [hd] at hv hw ⊢; exact h1 d r hv hr w hw
    · simpa using h2
  | cancel_again c =>
    simp only [apply] at h; split at h <;> simp at h; subst h; exact ⟨h1, h2⟩
  | expire_period c =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    constructor
    · intro d r hv hr w hw
      by_cases hd : d = c
      · subst hd
        have hc := get_some s.status d .Cancelled (by simpa using hg) (by simp)
        have := h1 d r hv hr _ hc
        simp at this
      · simp [hd] at hv hw ⊢; exact h1 d r hv hr w hw
    · intro d v hv hva
      by_cases hd : d = c
      · subst hd; simp at hv; subst hv; simp at hva
      · simp [hd] at hv ⊢; exact h2 d v hv hva
  | request_refund c => simp [apply] at h

theorem unacceptedCancelLeavesActive_holds : ∀ s, Reachable init apply s → unacceptedCancelLeavesActive s = true := by
  intro s hr
  have hs := (Reachable.induct strong strong_init strong_step s hr).1
  simp only [unacceptedCancelLeavesActive, FMap.keySet, FSet.all, List.all_eq_true]
  intro c hc
  obtain ⟨v, hv⟩ := FMap.get?_of_mem_keys hc
  have hsv := FMap.get_of_get? hv
  by_cases hr : s.lastBillingResult.get c = .Success
  · simp [hr]
  · have hl : s.lastBillingResult.get? c = some (s.lastBillingResult.get c) := by
      unfold FMap.get
      cases hx : s.lastBillingResult.get? c with
      | none =>
        have : s.lastBillingResult.get c = default := by unfold FMap.get; simp [hx]
        exact absurd (this.trans rfl) hr
      | some w => simp
    have := hs c _ hl hr v hv
    rw [hsv]
    simp [this.1, this.2]

end Subscription.Proofs.unacceptedCancelLeavesActive
