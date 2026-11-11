import Spec.Auth.Model

namespace Spec.Auth
open Quint

theorem keys_contains {β : Type} (l : List (String × β)) (k : String) :
    (l.map Prod.fst).contains k = (getList? l k).isSome := by
  induction l with
  | nil => simp [getList?]
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    by_cases h : k = a
    · simp [getList?, h]
    · have hb : (k == a) = false := by simpa using h
      simp only [List.map, List.contains, List.elem, getList?, if_neg h, hb] at ih ⊢
      exact ih

theorem keySet_contains {β : Type} (m : QMap String β) (k : String) :
    (m.keySet).contains k = (m.get? k).isSome := by
  simp only [QMap.keySet, QSet.contains, QMap.keys, QMap.get?]
  exact keys_contains _ _

theorem act_set (s : State) (k : String) (v : QSet String) (uid : String) :
    activeSessionsOf { s with userActiveSession := s.userActiveSession.set k v } uid
      = if uid = k then v else activeSessionsOf s uid := by
  unfold activeSessionsOf
  simp only [keySet_contains, QMap.get_set, QMap.get?_set]
  by_cases h : uid = k <;> simp [h]

theorem act_other (s : State) (uid : String) (f : QMap String Int) :
    activeSessionsOf { s with failedAttempts := f } uid = activeSessionsOf s uid := rfl

def P (s : State) : Prop :=
  ∀ uid, isLocked s uid = true → (activeSessionsOf s uid).elems = []

theorem P_init : P init := by
  intro uid h
  simp [isLocked, init, keySet_contains, QMap.empty, QMap.get?, getList?] at h

theorem lock_set (s : State) (k : String) (v : AccountStatus) (uid : String) :
    isLocked { s with accountStatus := s.accountStatus.set k v } uid
      = if uid = k then decide (v = AccountStatus.Locked) else isLocked s uid := by
  unfold isLocked
  simp only [keySet_contains, QMap.get_set, QMap.get?_set]
  by_cases h : uid = k <;> simp [h]; cases v <;> decide

theorem P_step : ∀ a s t, P s → apply a s = some t → P t := by
  intro a s t hs h
  cases a with
  | expire_session sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro uid hl
    have hl' : isLocked s uid = true := hl
    have hs' : (activeSessionsOf s uid).elems = [] := hs uid hl'
    simp only [activeSessionsOf, keySet_contains, QMap.get?_set, QMap.get_set] at hs' ⊢
    generalize (if (s.sessionOwner.get? sid).isSome = true then s.sessionOwner.get sid else "") = o
    by_cases ho : uid = o
    · subst ho; simp [QSet.diff, hs']
    · simp [ho]; exact hs'
  | login uid sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    intro u hl
    have hl' : isLocked s u = true := hl
    have hs' : (activeSessionsOf s u).elems = [] := hs u hl'
    by_cases hu : u = uid
    · subst hu; simp [hl'] at hg
    · simp only [activeSessionsOf, keySet_contains, QMap.get?_set, QMap.get_set] at hs' ⊢
      simp [hu]; exact hs'
  | login_failed uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro u hl
    simp only [isLocked, keySet_contains, QMap.get_set, QMap.get?_set] at hl
    by_cases hu : u = uid
    · by_cases hc : MAX_FAILED_ATTEMPTS ≤ attemptsOf s uid + 1
      · simp [activeSessionsOf, keySet_contains, hu, hc]; rfl
      · simp [hu, hc] at hl
    · have hl' : isLocked s u = true := by simpa [isLocked, keySet_contains, hu] using hl
      have hs' : (activeSessionsOf s u).elems = [] := hs u hl'
      by_cases hc : MAX_FAILED_ATTEMPTS ≤ attemptsOf s uid + 1 <;>
        simpa [activeSessionsOf, keySet_contains, hu, hc] using hs'
  | logout sid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro uid hl
    have hl' : isLocked s uid = true := hl
    have hs' : (activeSessionsOf s uid).elems = [] := hs uid hl'
    simp only [activeSessionsOf, keySet_contains, QMap.get?_set, QMap.get_set] at hs' ⊢
    generalize (if (s.sessionOwner.get? sid).isSome = true then s.sessionOwner.get sid else "") = o
    by_cases ho : uid = o
    · subst ho; simp [QSet.diff, hs']
    · simp [ho]; exact hs'
  | tick =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    intro u hl
    exact hs u hl
  | unlock_account uid =>
    simp only [apply] at h; split at h <;> simp at h; subst h
    rename_i hg
    intro u hl
    simp only [isLocked, keySet_contains, QMap.get_set, QMap.get?_set] at hl
    by_cases hu : u = uid
    · simp [hu] at hl
    · have hl' : isLocked s u = true := by simpa [isLocked, keySet_contains, hu] using hl
      exact hs u hl'

theorem noSessionWhileLocked_holds : ∀ s, Reachable init apply s → noSessionWhileLocked s = true := by
  intro s hr
  have hs := Reachable.induct P P_init P_step s hr
  simp only [noSessionWhileLocked, QMap.keySet, QSet.all, List.all_eq_true]
  intro uid _
  by_cases hl : isLocked s uid = true
  · have := hs uid hl
    simp [hl, QSet.size, this]
  · simp [hl]

end Spec.Auth
