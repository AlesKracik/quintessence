/-!
# Quint prelude

The data a Quint model is made of, in a form both the kernel and the replay
can compute with, and the one notion of "reachable" every proof is about.

`QMap` and `QSet` are association lists kept sorted by key, with no key
twice. Every operation keeps that canonical form, so two maps holding the
same entries are the same list and `=` on a state is decidable: the ITF
replay compares a Lean step's result with the next trace state by `==`.

`spec-record` writes the statement each proof must prove:

    ∀ s, Quint.Reachable N.init N.apply s → N.<quint_name> s = true

so this file fixes what "proven" means for every `proof: "lean"`
invariant. Edit it and every Lean verdict's `statement_sha` moves.
-/

namespace Quint

/-- A finite map: sorted by key, no key twice. Quint's `a -> b`. -/
structure QMap (α : Type) (β : Type) where
  entries : List (α × β)
  deriving DecidableEq, Repr, Inhabited

/-- A finite set: sorted, no element twice. Quint's `Set[a]`. -/
structure QSet (α : Type) where
  elems : List α
  deriving DecidableEq, Repr, Inhabited

section
variable {α β : Type} [LT α] [DecidableRel (α := α) (· < ·)] [DecidableEq α]

/-- Insert or replace, keeping the list sorted. Total, where Quint's
`set` on a missing key is an error: the Lean model admits at least
every transition the Quint one does, which is the direction a proof
about it needs. -/
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

namespace QMap

def ofList (l : List (α × β)) : QMap α β :=
  ⟨l.foldl (fun acc (kv : α × β) => putList acc kv.1 kv.2) []⟩

def get? (m : QMap α β) (k : α) : Option β := getList? m.entries k

/-- Quint's `m.get(k)`. A missing key is an error in Quint, a step that
never happens; here it reads `default`, so the Lean model admits at least
every transition the Quint one does (and some on keys Quint never reads
while they are missing). A proof about all of the Lean model's reachable
states is then a proof about all of Quint's. -/
def get [Inhabited β] (m : QMap α β) (k : α) : β := (m.get? k).getD default

/-- Quint's `m.set(k, v)` and `m.put(k, v)`. -/
def set (m : QMap α β) (k : α) (v : β) : QMap α β := ⟨putList m.entries k v⟩

def keys (m : QMap α β) : List α := m.entries.map Prod.fst

/-- Quint's `Map()`. -/
def empty : QMap α β := ⟨[]⟩

/-- Quint's `m.put(k, v)`: `set` is already total. -/
def put (m : QMap α β) (k : α) (v : β) : QMap α β := m.set k v

/-- Quint's `m.setBy(k, f)`. -/
def setBy [Inhabited β] (m : QMap α β) (k : α) (f : β → β) : QMap α β :=
  m.set k (f (m.get k))

/-- Quint's `m.keys().contains(k)`. -/
def contains (m : QMap α β) (k : α) : Bool := (m.get? k).isSome

/-- Quint's `m.keys().forall(k => p(k))`. -/
def all (m : QMap α β) (p : α → Bool) : Bool := m.keys.all p

omit [LT α] [DecidableRel (α := α) (· < ·)] in
theorem get_of_get? [Inhabited β] {m : QMap α β} {k : α} {v : β}
    (h : m.get? k = some v) : m.get k = v := by
  simp [get, h]

omit [LT α] [DecidableRel (α := α) (· < ·)] in
theorem get?_of_mem_keys {m : QMap α β} {k : α} (h : k ∈ m.keys) :
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

@[simp] theorem get?_set (m : QMap α β) (k k' : α) (v : β) :
    (m.set k v).get? k' = if k' = k then some v else m.get? k' :=
  getList?_putList _ _ _ _

@[simp] theorem get_set [Inhabited β] (m : QMap α β) (k k' : α) (v : β) :
    (m.set k v).get k' = if k' = k then v else m.get k' := by
  unfold get; rw [get?_set]; split <;> rfl

end QMap

namespace QSet

def insertList : List α → α → List α
  | [], x => [x]
  | y :: rest, x =>
    if x < y then x :: y :: rest
    else if x = y then y :: rest
    else y :: insertList rest x

def ofList (l : List α) : QSet α := ⟨l.foldl insertList []⟩

/-- Quint's `Set()`. -/
def empty : QSet α := ⟨[]⟩
def insert (s : QSet α) (x : α) : QSet α := ⟨insertList s.elems x⟩
def contains (s : QSet α) (x : α) : Bool := s.elems.contains x
def all (s : QSet α) (p : α → Bool) : Bool := s.elems.all p
def any (s : QSet α) (p : α → Bool) : Bool := s.elems.any p
/-- Quint's `size()`, an `int`. -/
def size (s : QSet α) : Int := s.elems.length
def union (s t : QSet α) : QSet α := ⟨t.elems.foldl insertList s.elems⟩
def inter (s t : QSet α) : QSet α := ⟨s.elems.filter t.contains⟩
/-- Quint's `exclude`. -/
def diff (s t : QSet α) : QSet α := ⟨s.elems.filter (fun x => !t.contains x)⟩
def subseteq (s t : QSet α) : Bool := s.elems.all t.contains
def filter (s : QSet α) (p : α → Bool) : QSet α := ⟨s.elems.filter p⟩
/-- Quint's `fold`, in ascending order: Quint leaves the order open, so a
model whose result depends on it is one the replay will catch. -/
def fold {γ : Type} (s : QSet α) (init : γ) (f : γ → α → γ) : γ := s.elems.foldl f init

end QSet

/-- Quint's `S.map(f)`. -/
def QSet.map {γ : Type} [LT γ] [DecidableRel (α := γ) (· < ·)] [DecidableEq γ]
    (s : QSet α) (f : α → γ) : QSet γ := QSet.ofList (s.elems.map f)

/-- Quint's `S.mapBy(f)`: the map from each element `x` of `S` to `f(x)`. -/
def QSet.mapBy (s : QSet α) (f : α → β) : QMap α β := QMap.ofList (s.elems.map fun k => (k, f k))

/-- Quint's `m.keys()`. The entries are sorted with no key twice, which is
already a set. -/
def QMap.keySet (m : QMap α β) : QSet α := ⟨m.keys⟩

/-- Quint's `a.to(b)`. -/
def QSet.range (a b : Int) : QSet Int :=
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

/-- The ITF replay's check: `none` when `states` is a run of `apply` from
`init` taking `acts` in order, else the index of the first state that is
not (0 = the first state is not `init`). An action marked refused (the
trace's `_refused`: a call the model must turn down) must have a false
guard, `apply a s = none`, and leave the state where it was. Computed,
never proven: a mismatch is drift between the Lean and Quint models,
and the replay is how spec-record finds it. -/
def replayFrom {σ α : Type} [DecidableEq σ] (apply : α → σ → Option σ) :
    Nat → σ → List σ → List (α × Bool) → Option Nat
  | _, _, [], [] => none
  | i, s, t :: ts, (a, false) :: as =>
    if apply a s = some t then replayFrom apply (i + 1) t ts as else some i
  | i, s, t :: ts, (a, true) :: as =>
    if apply a s = none ∧ t = s then replayFrom apply (i + 1) t ts as else some i
  | i, _, _, _ => some i

def replay {σ α : Type} [DecidableEq σ] (init : σ) (apply : α → σ → Option σ)
    (states : List σ) (acts : List (α × Bool)) : Option Nat :=
  match states with
  | [] => some 0
  | s0 :: rest => if s0 = init then replayFrom apply 1 s0 rest acts else some 0

end Quint
