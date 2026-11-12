import Spec.Data
import Std.Data.HashMap
import Std.Data.HashSet
import Lean.Data.Json.Printer

/-!
# Areas and the checker

An `Area` is one state machine with everything claimed about it. `Area.entry`
runs it and reports, per claim, the strongest level reached:

1. proven for all sizes and steps (a kernel-checked proof is attached);
2. holds in every reachable state of the declared scope (search finished);
3. no counterexample within N steps (search stopped at the budget);
5. not verified.

Invariants target level 1, requirements level 2. The search also produces the
open questions that drive the requirements toward completeness.
-/

namespace Spec
open Lean (Json)

/-- Something that must hold in every reachable state. `proof`, when given,
is checked by the kernel against exactly this statement. -/
structure Invariant {σ α : Type} (init : σ) (step : α → σ → Option σ) where
  id : String
  text : String
  holds : σ → Bool
  proof : Option (PLift (∀ s, Reachable init step s → holds s = true)) := none

/-- "When `when` holds, the system shall `shall`", or with `refused := true`,
"shall refuse". Checked on every explored state and action: where `when`
holds, the step must succeed and satisfy `shall` (or must be refused). -/
structure Requirement (σ α : Type) where
  id : String
  text : String
  when : σ → α → Bool
  shall : σ → α → σ → Bool := fun _ _ _ => true
  refused : Bool := false

/-- A concrete run from the initial state: every action in `run` must be
accepted, `expect` must hold at the end, and `refused` (if any) must then be
turned down. -/
structure Example (σ α : Type) where
  id : String
  text : String
  run : List α
  expect : σ → Bool := fun _ => true
  refused : Option α := none

structure Area (σ α : Type) where
  name : String
  init : σ
  /-- One step; `none` means the action is refused in that state. -/
  step : α → σ → Option σ
  /-- Scope of the search: the actions tried in every state. -/
  actions : List α
  /-- Scope of the search: states outside are not explored. -/
  bound : σ → Bool := fun _ => true
  /-- Budget of the search, in distinct states. -/
  budget : Nat := 1000000
  invariants : List (Invariant init step) := []
  requirements : List (Requirement σ α) := []
  examples : List (Example σ α) := []
  /-- Named projections of the state, drawn as diagrams in the review. -/
  views : List (String × (σ → String)) := []
  /-- Per action name: the decision on whether it can interleave with other
  events inside the implementation. A missing entry is an open question. -/
  atomic : List (String × String) := []

/-- What one area reports. `ledger` is committed; `review` adds the traces. -/
structure Report where
  ledger : Json
  review : Json
  failed : List String
  summary : String

structure Entry where
  name : String
  report : Unit → Report

/-! ## Display -/

private def oneLine {β : Type} [Repr β] (b : β) : String := (repr b).pretty 1000000

/-- Qualified names without their prefix, outside string literals:
`Auth.Action.login "a.b"` becomes `login "a.b"`. -/
private def short (text : String) : String :=
  let (out, word, _, _) := text.foldl
    (fun (acc : String × String × Bool × Bool) c =>
      let (out, word, inStr, esc) := acc
      if inStr then (out.push c, "", esc || c != '"', !esc && c == '\\')
      else if c == '"' then ((out ++ word).push c, "", true, false)
      else if c == '.' && word != "" && !word.front.isDigit then (out, "", false, false)
      else if c.isAlphanum || c == '_' then (out, word.push c, false, false)
      else ((out ++ word).push c, "", false, false))
    ("", "", false, false)
  out ++ word

private def label {α : Type} [Repr α] (a : α) : String := short (oneLine a)

private def actionName (l : String) : String := l.takeWhile (· != ' ')

/-- The fields of a state, split out of its derived `Repr`. -/
private def fields (text : String) : List (String × String) :=
  if !(text.startsWith "{ " && text.endsWith " }") then [("state", text)] else
  let body := (text.drop 2).dropRight 2
  let (parts, cur, _, _, _) := body.foldl
    (fun (acc : Array String × String × Nat × Bool × Bool) c =>
      let (parts, cur, depth, inStr, esc) := acc
      if inStr then (parts, cur.push c, depth, esc || c != '"', !esc && c == '\\')
      else if c == '"' then (parts, cur.push c, depth, true, false)
      else if c == '{' || c == '[' || c == '(' then (parts, cur.push c, depth + 1, false, false)
      else if c == '}' || c == ']' || c == ')' then (parts, cur.push c, depth - 1, false, false)
      else if c == ',' && depth == 0 then (parts.push cur, "", depth, false, false)
      else (parts, cur.push c, depth, false, false))
    (#[], "", 0, false, false)
  (parts.push cur).toList.map fun p =>
    match p.trim.splitOn " := " with
    | k :: v :: rest => (k, " := ".intercalate (v :: rest))
    | _ => ("", p.trim)

private def num (n : Nat) : Json := .num (.fromNat n)

/-! ## Search -/

section
variable {σ α : Type}

private structure Found where
  /-- Fingerprint of every state seen, with the state and action it came from. -/
  seen : Std.HashMap UInt64 (UInt64 × Nat)
  depth : Nat
  exhaustive : Bool
  cut : Nat
  invFail : Array (Option UInt64)
  reqFail : Array (Option (UInt64 × Nat))
  reqHits : Array Nat
  /-- First state where action `i` was accepted (`2*i`) or refused (`2*i+1`)
  with no requirement covering it. -/
  gaps : Array (Option UInt64)
  edges : Std.HashSet (Nat × String × String × String)

/-- Breadth-first over the scope. States are remembered by 64-bit fingerprint. -/
private def search [Hashable σ] (A : Area σ α) (names : Array String) : Found := Id.run do
  let acts := A.actions.toArray
  let reqs := A.requirements.toArray
  let invs := A.invariants.toArray
  let views := A.views.toArray
  let root := hash A.init
  let mut seen : Std.HashMap UInt64 (UInt64 × Nat) := Std.HashMap.emptyWithCapacity 1024
  seen := seen.insert root (root, 0)
  let mut invFail : Array (Option UInt64) :=
    invs.map fun i => if i.holds A.init then none else some root
  let mut reqFail : Array (Option (UInt64 × Nat)) := reqs.map fun _ => none
  let mut reqHits : Array Nat := reqs.map fun _ => 0
  let mut gaps : Array (Option UInt64) := Array.replicate (2 * acts.size) none
  let mut edges : Std.HashSet (Nat × String × String × String) := {}
  let mut cut := 0
  let mut depth := 0
  let mut stopped := false
  let mut frontier : Array σ := #[A.init]
  while !frontier.isEmpty && !stopped do
    let mut next : Array σ := #[]
    for s in frontier do
      if seen.size ≥ A.budget then
        stopped := true
        break
      let fs := hash s
      let vs := views.map fun v => v.2 s
      for (a, i) in acts.zipIdx do
        let r := A.step a s
        let mut covered := false
        for (q, j) in reqs.zipIdx do
          if q.when s a then
            covered := true
            reqHits := reqHits.modify j (· + 1)
            let ok := match r with
              | none => q.refused
              | some t => !q.refused && q.shall s a t
            if !ok && reqFail[j]!.isNone then reqFail := reqFail.set! j (some (fs, i))
        let g := 2 * i + (if r.isNone then 1 else 0)
        if !covered && gaps[g]!.isNone then gaps := gaps.set! g (some fs)
        match r with
        | none => pure ()
        | some t =>
          if !A.bound t then cut := cut + 1 else
          for (v, k) in views.zipIdx do
            let w := v.2 t
            if w != vs[k]! then edges := edges.insert (k, vs[k]!, names[i]!, w)
          let ft := hash t
          if !seen.contains ft then
            seen := seen.insert ft (fs, i)
            next := next.push t
            for (inv, j) in invs.zipIdx do
              if !inv.holds t && invFail[j]!.isNone then invFail := invFail.set! j (some ft)
    if !stopped then depth := depth + 1
    frontier := next
  return { seen, depth, exhaustive := !stopped, cut, invFail, reqFail, reqHits, gaps, edges }

/-- The actions leading from the initial state to the state with this fingerprint. -/
private def pathTo (seen : Std.HashMap UInt64 (UInt64 × Nat)) (root : UInt64) :
    Nat → UInt64 → List Nat → List Nat
  | 0, _, acc => acc
  | n + 1, fp, acc =>
    if fp == root then acc else
    match seen[fp]? with
    | none => acc
    | some (parent, i) => pathTo seen root n parent (i :: acc)

/-- A run from the initial state as rows for the review: action, state after
it, and whether the action was refused (the state then stays). -/
private def steps [Repr σ] [Repr α] (A : Area σ α) (run : List α) : Json := Id.run do
  let row (action : String) (s : σ) (refused : Bool) : Json := Json.mkObj [
    ("action", .str action), ("refused", .bool refused),
    ("state", .arr ((fields (short (oneLine s))).map fun (k, v) => .arr #[.str k, .str v]).toArray)]
  let mut s := A.init
  let mut out := #[row "" s false]
  for a in run do
    match A.step a s with
    | some t => s := t; out := out.push (row (label a) t false)
    | none => out := out.push (row (label a) s true)
  return .arr out

private def build [Hashable σ] [Repr σ] [Repr α] (A : Area σ α) : Report := Id.run do
  let acts := A.actions.toArray
  let labels := acts.map label
  let names := labels.map actionName
  let f := search A names
  let states := f.seen.size
  let root := hash A.init
  let trace (fp : UInt64) (last : Option Nat) : List α :=
    ((pathTo f.seen root states fp []) ++ last.toList).filterMap (acts[·]?)
  let reached := if f.exhaustive
    then (2, s!"holds in all {states} reachable states of the scope")
    else (3, s!"no counterexample within {f.depth} steps ({states} states)")
  -- one claim: (id, kind, text, level, target, verdict, counterexample)
  let mut claims : Array (String × String × String × Nat × Nat × String × Option (List α)) := #[]
  let mut questions : Array (String × Option (List α)) := #[]
  for (i, j) in A.invariants.zipIdx do
    claims := claims.push <| match f.invFail[j]!, i.proof with
      | some fp, _ => (i.id, "invariant", i.text, 5, 1, "violated", some (trace fp none))
      | none, some _ => (i.id, "invariant", i.text, 1, 1, "proven for all sizes and steps", none)
      | none, none => (i.id, "invariant", i.text, reached.1, 1, reached.2, none)
  for (q, j) in A.requirements.zipIdx do
    claims := claims.push <| match f.reqFail[j]! with
      | some (fp, a) => (q.id, "requirement", q.text, 5, 2, "violated", some (trace fp (some a)))
      | none =>
        if f.reqHits[j]! == 0 then (q.id, "requirement", q.text, 5, 2, "never applies in the scope", none)
        else (q.id, "requirement", q.text, reached.1, 2, reached.2, none)
    if f.reqFail[j]!.isNone && f.reqHits[j]! == 0 then
      questions := questions.push
        (s!"{q.id} never applies in the scope. Is the scope too small, or can this never happen?", none)
  let mut examples : Array (String × String × Bool × List α) := #[]
  for e in A.examples do
    let ok := match e.run.foldlM (fun s a => A.step a s) A.init with
      | none => false
      | some s => e.expect s && (e.refused.map fun a => (A.step a s).isNone).getD true
    examples := examples.push (e.id, e.text, ok, e.run ++ e.refused.toList)
  -- open questions, one per action name
  let distinct := names.foldl (fun acc n => if acc.contains n then acc else acc.push n) #[]
  for n in distinct do
    for refused in [false, true] do
      let hit := (List.range acts.size).findSome? fun i =>
        if names[i]! == n then (f.gaps[2 * i + (if refused then 1 else 0)]!).map (·, i) else none
      if let some (fp, i) := hit then
        questions := questions.push
          (if refused
            then s!"`{n}` is refused in a situation no requirement covers. Is refusing right there?"
            else s!"`{n}` is accepted in a situation no requirement covers. What must it do there?",
           some (trace fp (some i)))
  for n in distinct do
    if !(A.atomic.any (·.1 == n)) then
      questions := questions.push
        (s!"Can `{n}` interleave with other events inside the implementation, or is it atomic? Not decided.", none)
  let status (level target : Nat) (verdict : String) : String :=
    if verdict == "violated" then "violated" else if level ≤ target then "ok" else "below target"
  let withSteps (full : Bool) (run : Option (List α)) (obj : List (String × Json)) : Json :=
    Json.mkObj (obj ++ (if full then (run.map fun r => ("steps", steps A r)).toList else []))
  let sortedEdges := f.edges.toArray.qsort fun a b => oneLine a < oneLine b
  let json (full : Bool) : Json := Json.mkObj [
    ("area", .str A.name),
    ("search", Json.mkObj [
      ("states", num states), ("depth", num f.depth), ("exhaustive", .bool f.exhaustive),
      ("budget", num A.budget), ("actions", .arr (labels.map .str)), ("cutByBound", num f.cut)]),
    ("code", .str "not checked"),
    ("claims", .arr <| claims.map fun (id, kind, text, level, target, verdict, cex) =>
      withSteps full cex [("id", .str id), ("kind", .str kind), ("text", .str text),
        ("level", num level), ("target", num target), ("verdict", .str verdict),
        ("status", .str (status level target verdict))]),
    ("examples", .arr <| examples.map fun (id, text, ok, run) =>
      withSteps full (some run) [("id", .str id), ("text", .str text),
        ("status", .str (if ok then "ok" else "failed"))]),
    ("questions", .arr <| questions.map fun (text, run) => withSteps full run [("text", .str text)]),
    ("atomic", Json.mkObj (A.atomic.map fun (n, d) => (n, .str d))),
    ("views", .arr <| (A.views.zipIdx.map fun ((name, v), k) => Json.mkObj [
      ("name", .str name), ("initial", .str (v A.init)),
      ("edges", .arr <| (sortedEdges.filter (·.1 == k)).map fun (_, a, n, b) =>
        .arr #[.str a, .str n, .str b])]).toArray)]
  let failed := (claims.toList.filterMap fun (id, _, _, _, _, verdict, _) =>
      if verdict == "violated" then some s!"{id} violated" else none)
    ++ examples.toList.filterMap fun (id, _, ok, _) => if ok then none else some s!"{id} failed"
  let below := (claims.filter fun (_, _, _, level, target, verdict, _) =>
    level > target && verdict != "violated").size
  let summary := s!"{A.name}: {states} states, "
    ++ (if f.exhaustive then "scope exhausted" else s!"stopped at budget, depth {f.depth}")
    ++ s!"; {claims.size} claims, {below} below target, {failed.length} failed; "
    ++ s!"{questions.size} open questions"
  return { ledger := json false, review := json true, failed, summary }

/-- Package an area for the checker program. -/
def Area.entry [Hashable σ] [Repr σ] [Repr α] (A : Area σ α) : Entry :=
  { name := A.name, report := fun _ => build A }

end
end Spec
