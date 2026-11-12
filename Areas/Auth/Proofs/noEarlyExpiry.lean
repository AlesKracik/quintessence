import Areas.Auth.Model

namespace Auth.Proofs.noEarlyExpiry
open Spec

theorem mem_iff_getList (l : List (String × β)) (k : String) :
    k ∈ l.map Prod.fst ↔ (getList? l k).isSome = true := by
  induction l with
  | nil => simp [getList?]
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    by_cases h : k = a
    · simp [getList?, h]
    · simp [getList?, h, ih]

theorem contains_iff (m : FMap String β) (k : String) :
    (m.keySet).contains k = (m.get? k).isSome := by
  have := mem_iff_getList m.entries k
  simp only [FMap.keySet, FSet.contains, FMap.keys, FMap.get?]
  cases h : (getList? m.entries k).isSome <;> simp_all

theorem foldl_map_get (f : String → γ) (l' : List String) (acc : List (String × γ)) (k : String) :
    getList? ((l'.map fun x => (x, f x)).foldl
      (fun acc (kv : String × γ) => putList acc kv.1 kv.2) acc) k
      = if k ∈ l' then some (f k) else getList? acc k := by
  induction l' generalizing acc with
  | nil => simp
  | cons a rest ih =>
    simp only [List.map, List.foldl, ih, getList?_putList, List.mem_cons]
    by_cases h1 : k = a
    · simp [h1]
    · by_cases h2 : k ∈ rest <;> simp [h1, h2]

theorem fold_expired (l : List String) (acc : FMap String SessionStatus) (k : String)
    (h : (l.foldl (fun acc s_ => acc.set s_ SessionStatus.LoggedOut) acc).get? k = some SessionStatus.Expired) :
    acc.get? k = some SessionStatus.Expired := by
  induction l generalizing acc with
  | nil => simpa using h
  | cons a rest ih =>
    have := ih _ h
    simp only [FMap.get?_set] at this
    split at this
    · simp at this
    · exact this

def P (s : State) : Prop :=
  ∀ sid, s.sessions.get? sid = some SessionStatus.Expired →
    ∃ n, s.idleFor.get? sid = some n ∧ n > 3

theorem statusOf_eq (m : FMap String SessionStatus) (sid : String) :
    statusOf m sid = (m.get? sid).getD SessionStatus.LoggedOut := by
  unfold statusOf
  rw [contains_iff]
  unfold FMap.get
  cases m.get? sid <;> simp

theorem idleOf_eq (s : State) (sid : String) :
    idleOf s sid = (s.idleFor.get? sid).getD 0 := by
  unfold idleOf
  rw [contains_iff]
  unfold FMap.get
  cases s.idleFor.get? sid <;> simp

theorem P_init : P init := by
  intro sid h
  simp [init, FMap.empty, FMap.get?, getList?] at h

theorem P_step : ∀ a s t, P s → apply a s = some t → P t := by
  intro a s t hP h
  cases a with
  | expire_session sid =>
    simp only [apply] at h
    split at h
    · rename_i hg
      simp only [Option.some.injEq] at h
      subst h
      simp only [Bool.and_eq_true, decide_eq_true_eq] at hg
      obtain ⟨⟨_, _⟩, hi⟩ := hg
      intro k hk
      simp only [FMap.get?_set] at hk ⊢
      by_cases hks : k = sid
      · subst hks
        rw [idleOf_eq] at hi
        cases hh : s.idleFor.get? k with
        | none => simp [hh, MAX_SESSION_AGE] at hi
        | some n => simp [hh, MAX_SESSION_AGE] at hi; exact ⟨n, rfl, by omega⟩
      · simp [hks] at hk
        exact hP k hk
    · simp at h
  | login uid sid =>
    simp only [apply] at h
    split at h
    · simp only [Option.some.injEq] at h
      subst h
      intro k hk
      simp only [FMap.get?_set] at hk ⊢
      by_cases hks : k = sid
      · simp [hks] at hk
      · simp [hks] at hk ⊢
        exact hP k hk
    · simp at h
  | login_failed uid =>
    simp only [apply] at h
    split at h
    · simp only [Option.some.injEq] at h
      subst h
      intro k hk
      simp only at hk ⊢
      split at hk
      · exact hP k (fold_expired _ _ k (by simpa [FSet.fold] using hk))
      · exact hP k hk
    · simp at h
  | logout sid =>
    simp only [apply] at h
    split at h
    · simp only [Option.some.injEq] at h
      subst h
      intro k hk
      simp only [FMap.get?_set] at hk ⊢
      by_cases hks : k = sid
      · simp [hks] at hk
      · simp [hks] at hk
        exact hP k hk
    · simp at h
  | tick =>
    simp only [apply] at h
    simp only [Option.some.injEq] at h
    subst h
    intro k hk
    simp only at hk ⊢
    obtain ⟨n, hn, hn3⟩ := hP k hk
    have hmem : k ∈ s.idleFor.keySet.elems := by
      have := (mem_iff_getList s.idleFor.entries k).2 (by simp [FMap.get?] at hn; simp [hn])
      simpa [FMap.keySet, FMap.keys] using this
    refine ⟨n, ?_, hn3⟩
    simp only [FSet.mapBy, FMap.ofList, FMap.get?]
    rw [foldl_map_get (fun sid => if isActive s sid = true then s.idleFor.get sid + 1 else s.idleFor.get sid)
      s.idleFor.keySet.elems [] k]
    simp only [hmem, if_true]
    have : isActive s k = false := by
      unfold isActive
      rw [statusOf_eq, hk]; simp
    simp [this, FMap.get, hn]
  | unlock_account uid =>
    simp only [apply] at h
    split at h
    · simp only [Option.some.injEq] at h
      subst h
      intro k hk
      simp only at hk ⊢
      obtain ⟨n, hn, hn3⟩ := hP k hk
      exact ⟨n, hn, hn3⟩
    · simp at h

theorem noEarlyExpiry_holds : ∀ s, Reachable init apply s → noEarlyExpiry s = true := by
  intro s hr
  have hP := Reachable.induct P P_init P_step s hr
  unfold noEarlyExpiry
  simp only [FMap.keySet, FSet.all, FMap.keys, List.all_eq_true, List.mem_map]
  intro sid _
  simp only [Bool.or_eq_true, Bool.not_eq_true', beq_eq_false_iff_ne, ne_eq, beq_iff_eq,
    decide_eq_true_eq]
  by_cases hE : statusOf s.sessions sid = SessionStatus.Expired
  · right
    rw [statusOf_eq] at hE
    have : s.sessions.get? sid = some SessionStatus.Expired := by
      cases hh : s.sessions.get? sid <;> simp_all
    obtain ⟨n, hn, h3⟩ := hP sid this
    rw [idleOf_eq, hn]
    simp [MAX_SESSION_AGE]; omega
  · left; simp [hE]

end Auth.Proofs.noEarlyExpiry
