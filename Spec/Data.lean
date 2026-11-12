/-!
# Data and reachability

The data a model is made of, in a form both the proof kernel and the compiled
checker compute with, and the one notion of "reachable" every proof is about.

`FMap` and `FSet` are lists kept sorted by key with no key twice. Every
operation keeps that form, so two maps holding the same entries are the same
list, and equality and hashing of a state are the plain structural ones.

This file is part of the root of trust: it fixes what "proven" means.
-/

namespace Spec

/-- A finite map: sorted by key, no key twice. `a -> b`. -/
structure FMap (α : Type) (β : Type) where
  entries : List (α × β)
  deriving DecidableEq, Inhabited, Hashable

/-- A finite set: sorted, no element twice. `Set[a]`. -/
structure FSet (α : Type) where
  elems : List α
  deriving DecidableEq, Inhabited, Hashable

section
variable {α β : Type} [LT α] [DecidableRel (α := α) (· < ·)] [DecidableEq α]

/-- Insert or replace, keeping the list sorted. -/
def putList : List (α × β) → α → β → List (α × β)
  | [], k, v => [(k, v)]
  | (k', v') :: rest, k, v =>
    if k < k' then (k, v) :: (k', v') :: rest
    else if k = k' then (k, v) :: rest
    else (k', v') :: putList rest k v

def getList? : List (α × β) → α → Option β
  | [], _ => none
  | (k', v') :: rest, k => if k = k' then some v' else getList? rest k

theorem getList?_putList (l : List (α × β)) (k k' : α) (v : β) :
    getList? (putList l k v) k' = if k' = k then some v else getList? l k' := by
  induction l with
  | nil => simp [putList, getList?]
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    by_cases hlt : k < a
    · simp only [putList, if_pos hlt]
      by_cases h1 : k' = k
      · simp only [getList?, if_pos h1]
      · simp only [getList?, if_neg h1]
    · by_cases hka : k = a
      · simp only [putList, if_neg hlt, if_pos hka]
        by_cases h1 : k' = k
        · simp only [getList?, if_pos h1]
        · simp only [getList?, if_neg h1, if_neg (hka ▸ h1)]
      · simp only [putList, if_neg hlt, if_neg hka]
        by_cases h2 : k' = a
        · have h1 : ¬ k' = k := fun h => hka (h ▸ h2)
          simp only [getList?, if_pos h2, if_neg h1]
        · simp only [getList?, if_neg h2, ih]

namespace FMap

def ofList (l : List (α × β)) : FMap α β :=
  ⟨l.foldl (fun acc (kv : α × β) => putList acc kv.1 kv.2) []⟩

def get? (m : FMap α β) (k : α) : Option β := getList? m.entries k

/-- `m.get(k)`. A missing key reads `default`. -/
def get [Inhabited β] (m : FMap α β) (k : α) : β := (m.get? k).getD default

/-- `m.set(k, v)` and `m.put(k, v)`. -/
def set (m : FMap α β) (k : α) (v : β) : FMap α β := ⟨putList m.entries k v⟩

def keys (m : FMap α β) : List α := m.entries.map Prod.fst

/-- `Map()`. -/
def empty : FMap α β := ⟨[]⟩

/-- `m.put(k, v)`: `set` is already total. -/
def put (m : FMap α β) (k : α) (v : β) : FMap α β := m.set k v

/-- `m.setBy(k, f)`. -/
def setBy [Inhabited β] (m : FMap α β) (k : α) (f : β → β) : FMap α β :=
  m.set k (f (m.get k))

/-- `m.keys().contains(k)`. -/
def contains (m : FMap α β) (k : α) : Bool := (m.get? k).isSome

/-- `m.keys().forall(k => p(k))`. -/
def all (m : FMap α β) (p : α → Bool) : Bool := m.keys.all p

omit [LT α] [DecidableRel (α := α) (· < ·)] in
theorem get_of_get? [Inhabited β] {m : FMap α β} {k : α} {v : β}
    (h : m.get? k = some v) : m.get k = v := by
  simp [get, h]

omit [LT α] [DecidableRel (α := α) (· < ·)] in
theorem get?_of_mem_keys {m : FMap α β} {k : α} (h : k ∈ m.keys) :
    ∃ v, m.get? k = some v := by
  obtain ⟨l⟩ := m
  simp only [keys, get?] at *
  induction l with
  | nil => simp at h
  | cons hd rest ih =>
    obtain ⟨a, b⟩ := hd
    by_cases hk : k = a
    · exact ⟨b, by simp [getList?, hk]⟩
    · simp only [List.map, List.mem_cons, hk, false_or] at h
      simpa [getList?, hk] using ih h

@[simp] theorem get?_set (m : FMap α β) (k k' : α) (v : β) :
    (m.set k v).get? k' = if k' = k then some v else m.get? k' :=
  getList?_putList _ _ _ _

@[simp] theorem get_set [Inhabited β] (m : FMap α β) (k k' : α) (v : β) :
    (m.set k v).get k' = if k' = k then v else m.get k' := by
  unfold get; rw [get?_set]; split <;> rfl

end FMap

namespace FSet

def insertList : List α → α → List α
  | [], x => [x]
  | y :: rest, x =>
    if x < y then x :: y :: rest
    else if x = y then y :: rest
    else y :: insertList rest x

def ofList (l : List α) : FSet α := ⟨l.foldl insertList []⟩

/-- `Set()`. -/
def empty : FSet α := ⟨[]⟩
def insert (s : FSet α) (x : α) : FSet α := ⟨insertList s.elems x⟩
def contains (s : FSet α) (x : α) : Bool := s.elems.contains x
def all (s : FSet α) (p : α → Bool) : Bool := s.elems.all p
def any (s : FSet α) (p : α → Bool) : Bool := s.elems.any p
/-- `size()`, an `int`. -/
def size (s : FSet α) : Int := s.elems.length
def union (s t : FSet α) : FSet α := ⟨t.elems.foldl insertList s.elems⟩
def inter (s t : FSet α) : FSet α := ⟨s.elems.filter t.contains⟩
/-- `exclude`. -/
def diff (s t : FSet α) : FSet α := ⟨s.elems.filter (fun x => !t.contains x)⟩
def subseteq (s t : FSet α) : Bool := s.elems.all t.contains
def filter (s : FSet α) (p : α → Bool) : FSet α := ⟨s.elems.filter p⟩
/-- `fold`, in ascending order. -/
def fold {γ : Type} (s : FSet α) (init : γ) (f : γ → α → γ) : γ := s.elems.foldl f init

end FSet

/-- `S.map(f)`. -/
def FSet.map {γ : Type} [LT γ] [DecidableRel (α := γ) (· < ·)] [DecidableEq γ]
    (s : FSet α) (f : α → γ) : FSet γ := FSet.ofList (s.elems.map f)

/-- `S.mapBy(f)`: the map from each element `x` of `S` to `f(x)`. -/
def FSet.mapBy (s : FSet α) (f : α → β) : FMap α β := FMap.ofList (s.elems.map fun k => (k, f k))

/-- `m.keys()`. The entries are sorted with no key twice, which is
already a set. -/
def FMap.keySet (m : FMap α β) : FSet α := ⟨m.keys⟩

/-- `a.to(b)`. -/
def FSet.range (a b : Int) : FSet Int :=
  ⟨(List.range (b - a + 1).toNat).map fun (i : Nat) => a + (i : Int)⟩

end

/-- The states a model reaches from `init` by steps of `apply`. `apply a s`
is `none` when action `a`'s guard is false in `s`. -/
inductive Reachable {σ α : Type} (init : σ) (apply : α → σ → Option σ) : σ → Prop
  | init : Reachable init apply init
  | step {s t : σ} (a : α) :
      Reachable init apply s → apply a s = some t → Reachable init apply t

/-- The one proof rule: a predicate true at `init` and kept by every step
holds on every reachable state. Prove a STRONGER `P` when the invariant
alone is not kept (a counterexample to induction), then weaken. -/
theorem Reachable.induct {σ α : Type} {init : σ} {apply : α → σ → Option σ}
    (P : σ → Prop) (h0 : P init)
    (hs : ∀ a s t, P s → apply a s = some t → P t) :
    ∀ s, Reachable init apply s → P s := by
  intro s h
  induction h with
  | init => exact h0
  | step a _ hst ih => exact hs a _ _ ih hst

instance {α β : Type} [Repr α] [Repr β] : Repr (FMap α β) where
  reprPrec m _ := "{" ++ Std.Format.joinSep (m.entries.map fun (k, v) => repr k ++ " ↦ " ++ repr v) ", " ++ "}"

instance {α : Type} [Repr α] : Repr (FSet α) where
  reprPrec s _ := "{" ++ Std.Format.joinSep (s.elems.map repr) ", " ++ "}"

end Spec
