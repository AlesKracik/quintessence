import Spec.Auth.Model

namespace Spec.Auth
open Quint

def PM (m : QMap UserId (QSet SessionId)) : Prop :=
  ∀ k v, m.get? k = some v → v.elems.length ≤ 1

def P (s : State) : Prop := PM s.userActiveSession

theorem PM_set {m : QMap UserId (QSet SessionId)} {k : UserId} {v : QSet SessionId}
    (hm : PM m) (hv : v.elems.length ≤ 1) : PM (m.set k v) := by
  intro k' w h
  rw [QMap.get?_set] at h
  split at h
  · cases h; exact hv
  · exact hm k' w h

theorem active_len {s : State} (hs : P s) (k : UserId) :
    (activeSessionsOf s k).elems.length ≤ 1 := by
  unfold activeSessionsOf
  split
  · rename_i hc
    have hk : k ∈ s.userActiveSession.keys := by
      simpa [QMap.keySet, QSet.contains] using hc
    obtain ⟨v, hv⟩ := QMap.get?_of_mem_keys hk
    rw [QMap.get_of_get? hv]
    exact hs k v hv
  · simp [QSet.empty]

theorem P_init : P init := by
  intro k v h
  simp [init, QMap.empty, QMap.get?, getList?] at h

theorem diff_len (a b : QSet SessionId) (h : a.elems.length ≤ 1) :
    (a.diff b).elems.length ≤ 1 := by
  simp only [QSet.diff]
  exact Nat.le_trans (List.length_filter_le _ _) h

theorem P_step : ∀ a s t, P s → apply a s = some t → P t := by
  intro a s t hs h
  cases a with
  | expire_session sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact PM_set hs (diff_len _ _ (active_len hs _))
  | login uid sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    have h0 : (activeSessionsOf s uid).elems = [] := by
      simp [QSet.size] at hg
      exact hg.2
    have h1 : activeSessionsOf s uid = ⟨[]⟩ := by
      cases hx : activeSessionsOf s uid
      simp [hx] at h0; simp [h0]
    apply PM_set hs
    rw [h1]
    simp [QSet.union, QSet.ofList, QSet.insertList]
  | login_failed uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    show PM _
    split
    · exact PM_set hs (by simp [QSet.empty])
    · exact hs
  | logout sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact PM_set hs (diff_len _ _ (active_len hs _))
  | tick =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact hs
  | unlock_account uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact hs

theorem atMostOneActiveSession_holds : ∀ s, Reachable init apply s → atMostOneActiveSession s = true := by
  intro s hr
  have hs := Reachable.induct P P_init P_step s hr
  simp only [atMostOneActiveSession, QMap.keySet, QSet.all, List.all_eq_true]
  intro uid _
  have := active_len hs uid
  simp only [QSet.size, decide_eq_true_eq]
  omega

end Spec.Auth
