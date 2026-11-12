import Areas.Auth.Model

namespace Auth.Proofs.noAccessWithInvalidToken
open Spec

theorem contains_keySet {α β : Type} [LT α] [DecidableRel (α := α) (· < ·)] [DecidableEq α]
    (m : FMap α β) (k : α) : (m.keySet).contains k = (m.get? k).isSome := by
  obtain ⟨l⟩ := m
  induction l with
  | nil => simp [FMap.keySet, FMap.keys, FSet.contains, FMap.get?, getList?]
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    simp only [FMap.keySet, FMap.keys, FSet.contains, FMap.get?] at ih ⊢
    by_cases hk : k = a
    · simp [getList?, hk]
    · have hb : (k == a) = false := by simp [hk]
      simp [getList?, hk, hb] at ih ⊢
      exact ih

def aso (m : FMap UserId (FSet SessionId)) (uid : UserId) : FSet SessionId :=
  if ((m.keySet).contains uid) then (m.get uid) else (FSet.empty : (FSet SessionId))

theorem aso_eq (s : State) (uid : UserId) : activeSessionsOf s uid = aso s.userActiveSession uid := rfl

theorem aso_set (m : FMap UserId (FSet SessionId)) (k : UserId) (v : FSet SessionId) (uid : UserId) :
    aso (m.set k v) uid = if uid = k then v else aso m uid := by
  unfold aso
  simp only [contains_keySet]
  by_cases h : uid = k <;> simp [h]

theorem insertList_mem (l : List SessionId) (y x : SessionId) :
    x ∈ FSet.insertList l y ↔ x ∈ l ∨ x = y := by
  induction l with
  | nil => simp [FSet.insertList]
  | cons a rest ih =>
    simp only [FSet.insertList]
    by_cases h1 : y < a
    · simp [h1]; grind
    · by_cases h2 : y = a
      · simp [h2]; grind
      · simp [h1, h2, ih]; grind

theorem fold_loggedOut (l : List SessionId) (m : FMap SessionId SessionStatus) (k : SessionId) :
    (l.foldl (fun acc s_ => acc.set s_ SessionStatus.LoggedOut) m).get? k
      = if k ∈ l then some SessionStatus.LoggedOut else m.get? k := by
  induction l generalizing m with
  | nil => simp
  | cons a rest ih =>
    simp only [List.foldl, ih, FMap.get?_set, List.mem_cons]
    by_cases h1 : k = a <;> by_cases h2 : k ∈ rest <;> simp [h1, h2]

def P (s : State) : Prop :=
  ∀ uid sid, sid ∈ (activeSessionsOf s uid).elems →
    s.sessions.get? sid = some .Active ∧ s.sessionOwner.get? sid = some uid

theorem P_init : P init := by
  intro uid sid h
  simp [init, activeSessionsOf, FMap.keySet, FMap.keys, FSet.contains, FMap.empty, FSet.empty] at h

theorem statusOf_of_get {m : FMap SessionId SessionStatus} {sid : SessionId}
    (h : m.get? sid = some .Active) : statusOf m sid = .Active := by
  simp [statusOf, contains_keySet, h, FMap.get_of_get? h]

theorem remove_core (s : State) (sid : SessionId) (v : SessionStatus) (o : UserId)
    (hv : v ≠ .Active) (hs : P s)
    (ho : ∀ u, s.sessionOwner.get? sid = some u → o = u) :
    P { s with
        sessions := (s.sessions.set sid v)
        userActiveSession := (s.userActiveSession.set o ((aso s.userActiveSession o).diff (FSet.ofList [sid] : (FSet SessionId)))) } := by
  intro uid x hx
  simp only [aso_eq, aso_set] at hx
  by_cases huo : uid = o
  · rw [if_pos huo] at hx
    simp [FSet.diff, FSet.ofList, FSet.contains, FSet.insertList] at hx
    obtain ⟨hx1, hx2⟩ := hx
    subst huo
    have := hs uid x hx1
    simp [FMap.get?_set, hx2]
    exact this
  · rw [if_neg huo] at hx
    have h1 := hs uid x hx
    have hne : x ≠ sid := by
      intro e; subst e
      exact huo (ho uid h1.2).symm
    simp [FMap.get?_set, hne]
    exact h1

theorem P_step : ∀ a s t, P s → apply a s = some t → P t := by
  intro a s t hs h
  cases a with
  | expire_session sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact remove_core s sid .Expired _ (by decide) hs
      (by intro u hu; simp [contains_keySet, hu, FMap.get_of_get? hu])
  | login uid sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    have hns : s.sessions.get? sid = none := by
      simp [contains_keySet] at hg
      exact hg.1.2
    intro u x hx
    simp only [aso_eq, aso_set] at hx
    have hne : ∀ u', x ∈ (activeSessionsOf s u').elems → x ≠ sid := by
      intro u' h1 e; subst e
      rw [(hs u' x h1).1] at hns; simp at hns
    split at hx
    · rename_i hu
      simp [FSet.union, FSet.ofList, FSet.insertList, insertList_mem] at hx
      rcases hx with hx | hx
      · have h1 := hs _ x (hu ▸ hx)
        have := hne _ (hu ▸ hx)
        simp [FMap.get?_set, this]
        exact h1
      · subst hx; simp [hu]
    · have h1 := hs u x hx
      have := hne _ hx
      simp [FMap.get?_set, this]
      exact h1
  | login_failed uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    by_cases hc : MAX_FAILED_ATTEMPTS ≤ attemptsOf s uid + 1
    · simp only [hc, if_true]
      intro u x hx
      simp only [aso_eq, aso_set] at hx
      split at hx
      · simp [FSet.empty] at hx
      · rename_i hu
        have h1 := hs u x hx
        simp only [FSet.fold, fold_loggedOut]
        by_cases hm : x ∈ (activeSessionsOf s uid).elems
        · have h2 := hs uid x hm
          rw [h1.2] at h2
          exact absurd (Option.some.inj h2.2) hu
        · simp [hm]
          exact h1
    · simp only [hc, if_false]
      exact hs
  | logout sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact remove_core s sid .LoggedOut _ (by decide) hs
      (by intro u hu; simp [contains_keySet, hu, FMap.get_of_get? hu])
  | tick =>
    simp only [apply] at h; simp at h; subst h
    exact hs
  | unlock_account uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    exact hs

theorem noAccessWithInvalidToken_holds : ∀ s, Reachable init apply s → noAccessWithInvalidToken s = true := by
  intro s hr
  have hs := Reachable.induct P P_init P_step s hr
  simp only [noAccessWithInvalidToken, FMap.keySet, FSet.all, List.all_eq_true]
  intro sid _
  by_cases ha : statusOf s.sessions sid = .Active
  · simp [ha]
  · simp [ha]
    intro uid _
    cases hc : (activeSessionsOf s uid).contains sid
    · rfl
    have hm : sid ∈ (activeSessionsOf s uid).elems := by
      simpa [FSet.contains] using hc
    exact (ha (statusOf_of_get (hs uid sid hm).1)).elim

end Auth.Proofs.noAccessWithInvalidToken
