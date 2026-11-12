import Spec.Data
import Std.Data.HashMap
import Std.Data.HashSet
import Lean.Data.Json.Printer
import Lean.Data.Json.Parser

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

An area bound to existing code (`Area.code`) is also checked against it: the
code is driven through the model's transitions and compared after every step.
Any difference is drift.
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

/-- The binding of an area to code. The mapping is data: what to run in the
code for each action, and what to read from it after every step. A language
adapter (`adapters/`) executes it; the checker compares. -/
structure Code (σ : Type) where
  /-- The command that starts the adapter. -/
  adapter : List String
  /-- Code that builds a fresh system; run before every run. -/
  setup : String
  /-- Per action name, the code to run; `{0}`, `{1}` are the action's arguments.
  The action counts as refused when that code raises an error. -/
  actions : List (String × String)
  /-- What is compared after every step: a name, the value the model expects,
  and the expression that reads it from the code. -/
  observe : List (String × (σ → String) × String)
  /-- Budget of the code check, in executed code steps. -/
  budget : Nat := 100000

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
  code : Option (Code σ) := none

/-- What one area reports. `ledger` is committed; `review` adds the traces. -/
structure Report where
  ledger : Json
  review : Json
  failed : List String
  summary : String

structure Entry where
  name : String
  report : IO Report

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

/-! ## Code check -/

/-- `login "alice" "s1"` as an adapter receives it: the action's name, and its
arguments as JSON values. -/
private def call (label : String) : Json :=
  let (parts, cur, _, _, _) := label.foldl
    (fun (acc : Array String × String × Nat × Bool × Bool) c =>
      let (parts, cur, depth, inStr, esc) := acc
      if inStr then (parts, cur.push c, depth, esc || c != '"', !esc && c == '\\')
      else if c == '"' then (parts, cur.push c, depth, true, false)
      else if c == '(' then (parts, cur, depth + 1, false, false)
      else if c == ')' then (parts, cur, depth - 1, false, false)
      else if c == ' ' && depth == 0 then (parts.push cur, "", depth, false, false)
      else (parts, cur.push c, depth, false, false))
    (#[], "", 0, false, false)
  let words := (parts.push cur).toList.filter (· != "")
  let arg (w : String) : Json := match Json.parse w with
    | .ok (.str s) => .str s
    | .ok (.num n) => .num n
    | .ok (.bool b) => .bool b
    | _ => .str w
  Json.mkObj [("action", .str (words.headD "")), ("args", .arr ((words.drop 1).map arg).toArray)]

private structure CodeResult where
  status : String := "not checked"
  verdict : String := "not checked"
  /-- The run that shows the drift, as rows for the review. -/
  drift : Option Json := none
  questions : List String := []

/-- Drive the code through the model: for every state of the scope, nearest
first, every accepted action, and every refused action in the state it leads
to. Each run starts from a fresh system. Stops at the first difference. -/
private def checkCode [Hashable σ] [Repr α] (A : Area σ α) (c : Code σ) : IO CodeResult := do
  let names := A.actions.map (actionName ∘ label)
  if let some n := names.find? fun n => !(c.actions.any (·.1 == n)) then
    return { status := "error", verdict := s!"action `{n}` has no code mapping" }
  let child ← IO.Process.spawn {
    cmd := c.adapter.headD "", args := (c.adapter.drop 1).toArray, stdin := .piped, stdout := .piped }
  let ask (j : Json) : IO Json := do
    child.stdin.putStrLn j.compress
    child.stdin.flush
    let reply ← IO.ofExcept (Json.parse (← child.stdout.getLine))
    if let .ok e := reply.getObjVal? "error" then throw (IO.userError (e.getStr?.toOption.getD e.compress))
    return reply
  let hello ← ask <| Json.mkObj [("setup", .str c.setup),
    ("actions", Json.mkObj (c.actions.map fun (n, code) => (n, .str code))),
    ("observe", .arr (c.observe.map fun (_, _, e) => .str e).toArray)]
  let mapped := c.setup :: c.actions.map (·.2) ++ c.observe.map (·.2.2)
  let operations := ((hello.getObjVal? "operations" >>= Json.getArr?).toOption.getD #[]).toList.filterMap
    (·.getStr?.toOption)
  let questions := (operations.filter fun op => !(mapped.any fun m => (m.splitOn op).length > 1)).map
    (s!"The code has operation `{·}` that the model does not use. Should the model cover it?")
  let expect (s : σ) : List String := c.observe.map (·.2.1 s)
  let row (action : String) (refused : Bool) (model code : List String) : Json := Json.mkObj [
    ("action", .str action), ("refused", .bool refused),
    ("state", .arr ((c.observe.zip (model.zip code)).map fun ((n, _, _), m, v) =>
      .arr #[.str n, .str (if m == v then m else s!"{m} in the model, {v} in the code")]).toArray)]
  let differ (model code : List String) : Option String :=
    (c.observe.zip (model.zip code)).findSome? fun ((n, _, _), m, v) =>
      if m == v then none else some s!"`{n}` is {m} in the model and {v} in the code"
  -- one run against the code; `some (what, rows)` on the first difference
  let runOne (run : List α) : IO (Option (String × Json)) := do
    let reply ← ask <| Json.mkObj [("run", .arr (run.map (call ∘ label)).toArray)]
    let texts (j : Json) (k : String) : List String :=
      ((j.getObjVal? k >>= Json.getArr?).toOption.getD #[]).toList.map fun v => v.getStr?.toOption.getD v.compress
    let mut s := A.init
    let mut rows := #[row "" false (expect s) (texts reply "initial")]
    if let some d := differ (expect s) (texts reply "initial") then
      return some (s!"At the start, {d}.", .arr rows)
    for (a, r) in run.zip ((reply.getObjVal? "steps" >>= Json.getArr?).toOption.getD #[]).toList do
      let refused := (r.getObjVal? "refused" >>= Json.getBool?).toOption.getD false
      let next := A.step a s
      rows := rows.push (row (label a) next.isNone (expect (next.getD s)) (texts r "observed"))
      if next.isNone && !refused then
        return some (s!"The model refuses `{label a}` here; the code accepts it.", .arr rows)
      if next.isSome && refused then
        return some (s!"The model accepts `{label a}` here; the code refuses it ({(r.getObjVal? "error" >>= Json.getStr?).toOption.getD ""}).", .arr rows)
      s := next.getD s
      if let some d := differ (expect s) (texts r "observed") then
        return some (s!"After `{label a}`, {d}.", .arr rows)
    return none
  let refusedIn (s : σ) : List α := A.actions.filter fun a => (A.step a s).isNone
  let mut drift ← runOne (refusedIn A.init)
  let mut seen : Std.HashSet UInt64 := ({} : Std.HashSet UInt64).insert (hash A.init)
  let mut queue : Array (σ × List α) := #[(A.init, [])]  -- each state with the run that reaches it, reversed
  let mut i := 0
  let mut used := 0
  let mut transitions := 0
  let mut stoppedAt : Option Nat := none
  while i < queue.size && drift.isNone && stoppedAt.isNone do
    let some (s, path) := queue[i]? | break
    i := i + 1
    for a in A.actions do
      let some t := A.step a s | continue
      if !A.bound t then continue
      if used ≥ c.budget then
        stoppedAt := some path.length
        break
      let isNew := !seen.contains (hash t)
      let run := (a :: path).reverse ++ (if isNew then refusedIn t else [])
      used := used + run.length
      transitions := transitions + 1
      drift ← runOne run
      if drift.isSome then break
      if isNew then
        seen := seen.insert (hash t)
        queue := queue.push (t, a :: path)
  let (stdin, child) ← child.takeStdin  -- closing its input ends the adapter
  discard child.wait
  match drift with
  | some (what, rows) => return { status := "drift", verdict := what, drift := some rows, questions }
  | none =>
    let reach := match stoppedAt with
      | none => "of the scope"
      | some d => s!"within {d} steps of the start"
    return { status := "agrees", questions,
             verdict := s!"the code agrees with the model on every transition and refusal {reach} "
               ++ s!"({transitions} transitions, {used} code steps)" }

private def build [Hashable σ] [Repr σ] [Repr α] (A : Area σ α) : IO Report := do
  let code ← match A.code with
    | none => pure {}
    | some c => try checkCode A c catch e =>
        pure { status := "error", verdict := s!"the code check could not run: {e}" : CodeResult }
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
  for q in code.questions do
    questions := questions.push (q, none)
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
    ("code", Json.mkObj <| [("status", .str code.status), ("verdict", .str code.verdict),
        ("level", num (if code.status == "agrees" then 4 else 6))]
      ++ (A.code.map fun c => [("setup", Json.str c.setup),
          ("actions", Json.mkObj (c.actions.map fun (n, x) => (n, .str x))),
          ("observe", .arr (c.observe.map fun (n, _, x) => .arr #[.str n, .str x]).toArray)]).getD []
      ++ (if full then (code.drift.map (("steps", ·))).toList else [])),
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
    ++ (examples.toList.filterMap fun (id, _, ok, _) => if ok then none else some s!"{id} failed")
    ++ (if code.status == "drift" || code.status == "error" then [s!"code: {code.verdict}"] else [])
  let below := (claims.filter fun (_, _, _, level, target, verdict, _) =>
    level > target && verdict != "violated").size
  let summary := s!"{A.name}: {states} states, "
    ++ (if f.exhaustive then "scope exhausted" else s!"stopped at budget, depth {f.depth}")
    ++ s!"; {claims.size} claims, {below} below target, {failed.length} failed; "
    ++ s!"{questions.size} open questions; code {code.status}"
  return { ledger := json false, review := json true, failed, summary }

/-- Package an area for the checker program. -/
def Area.entry [Hashable σ] [Repr σ] [Repr α] (A : Area σ α) : Entry :=
  { name := A.name, report := build A }

end
end Spec
