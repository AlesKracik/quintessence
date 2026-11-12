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

An area bound to code (`Area.code`) is also checked against it: the code is
driven through the model's transitions and compared after every step. Any
difference is drift. Code and model both live in git, and git says whether
anything changed: the ledger records the state git reports for what a code
verdict depends on, and the check is re-run only when that state differs.
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
  /-- Where the claim comes from, as a history (see `Requirement.sources`). -/
  sources : List String := []

/-- "When `when` holds, the system shall `shall`", or with `refused := true`,
"shall refuse". Checked on every explored state and action: where `when`
holds, the step must succeed and satisfy `shall` (or must be refused). -/
structure Requirement (σ α : Type) where
  id : String
  text : String
  when : σ → α → Bool
  shall : σ → α → σ → Bool := fun _ _ _ => true
  refused : Bool := false
  /-- Where the requirement comes from, as a history, oldest first: one entry
  per origin, each a person, a document or a place in the code. A requirement
  extracted from code and later changed by a person has both entries, e.g.
  `["code: reservation.py, hold", "Ann, 2026-03-02: holds last 15 minutes"]`.
  Free text, shown to the reviewer. -/
  sources : List String := []

/-- A concrete run from the initial state: every action in `run` must be
accepted, `expect` must hold at the end, and `refused` (if any) must then be
turned down. -/
structure Example (σ α : Type) where
  id : String
  text : String
  run : List α
  expect : σ → Bool := fun _ => true
  refused : Option α := none

/-- One moment of a journey: what happens, in the words of the person it
happens to, and the action that does it. -/
structure Moment (α : Type) where
  say : String
  action : α
  refused : Bool

/-- A moment in which the system does what is asked. -/
def Moment.step {α : Type} (say : String) (action : α) : Moment α := ⟨say, action, false⟩
/-- A moment in which the system turns the request down. -/
def Moment.refusal {α : Type} (say : String) (action : α) : Moment α := ⟨say, action, true⟩

/-- A customer journey: a story a person recognises, told as one run of the
model. The review opens with the journeys and shows, for every moment, the
requirements at work and what changed. Every moment must go the way it is
told: accepted for `.step`, turned down for `.refusal`. -/
structure Journey (α : Type) where
  id : String
  title : String
  /-- Who wants what. -/
  goal : String
  moments : List (Moment α)
  /-- Where the story comes from, as a history (see `Requirement.sources`). -/
  sources : List String := []

/-- The binding of an area to code. The mapping is data: what to run in the
code for each action, and what to read from it after every step. A language
adapter (`adapters/`) executes it; the checker compares. -/
structure Code (σ : Type) where
  /-- The command that starts the adapter. -/
  adapter : List String
  /-- The git repository the code lives in: `"."` for this one, otherwise a
  name that `.spec/local.json` maps to a local checkout (`repo_paths`). -/
  repo : String := "."
  /-- The code's files or directories, relative to that repository's root. -/
  paths : List String
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
  journeys : List (Journey α) := []
  /-- Named projections of the state in words a reader knows ("seat s1" is
  "held"). The review draws each as a diagram and tells the journeys in
  them: a moment shows which views changed, not the raw state. -/
  views : List (String × (σ → String)) := []
  /-- Per action name: the decision on whether it can interleave with other
  events inside the implementation. A missing entry is an open question. -/
  atomic : List (String × String) := []
  /-- Questions for a person that the checker cannot find by itself: what a
  document says against what the code does, what a document leaves unsaid.
  They stay in the review until answered; removing one means it was. -/
  questions : List String := []
  code : Option (Code σ) := none

/-- What one area reports. `ledger` is committed; `review` adds the traces. -/
structure Report where
  ledger : Json
  review : Json
  failed : List String
  summary : String

structure Entry where
  name : String
  /-- `since`: the git revision to compare against; its ledger, not the one on
  disk, is then what counts as recorded. `full`: re-run the code check regardless. -/
  report : (since : Option String) → (full : Bool) → IO Report

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

/-- The requirements, with their positions, whose `when` holds for doing `a` in
`s`. Asked for every state and action, and nearly always answered "none", so it
is a plain loop that allocates only for the ones that apply. -/
private def applying (reqs : Array (Requirement σ α)) (s : σ) (a : α) :
    Nat → List (Requirement σ α × Nat) → List (Requirement σ α × Nat)
  | 0, acc => acc
  | j + 1, acc =>
    if h : j < reqs.size then
      applying reqs s a j (if reqs[j].when s a then (reqs[j], j) :: acc else acc)
    else applying reqs s a j acc

/-- Breadth-first over the scope. States are remembered by 64-bit fingerprint. -/
private def search [Hashable σ] (A : Area σ α) (names : Array String) : Found := Id.run do
  let acts := A.actions.toArray
  let reqs := A.requirements.toArray
  let invs := A.invariants.toArray
  let views := A.views.toArray
  -- paired with their positions once: the loops below run for every state and action
  let actsI := acts.zipIdx
  let invsI := invs.zipIdx
  let viewsI := views.zipIdx
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
      for (a, i) in actsI do
        let r := A.step a s
        let speaking := applying reqs s a reqs.size []
        for (q, j) in speaking do
          reqHits := reqHits.modify j (· + 1)
          let ok := match r with
            | none => q.refused
            | some t => !q.refused && q.shall s a t
          if !ok && reqFail[j]!.isNone then reqFail := reqFail.set! j (some (fs, i))
        let g := 2 * i + (if r.isNone then 1 else 0)
        if speaking.isEmpty && gaps[g]!.isNone then gaps := gaps.set! g (some fs)
        match r with
        | none => pure ()
        | some t =>
          if !A.bound t then cut := cut + 1 else
          for (v, k) in viewsI do
            let w := v.2 t
            if w != vs[k]! then edges := edges.insert (k, vs[k]!, names[i]!, w)
          let ft := hash t
          if !seen.contains ft then
            seen := seen.insert ft (fs, i)
            next := next.push t
            for (inv, j) in invsI do
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
  /-- The state git reports for everything the verdict depends on here. -/
  inputs : String := ""
  /-- For code in another repository: the commit the verdict is about. -/
  commit : String := ""
  reused : Bool := false

/-- Run git in `dir`; `none` when it fails or exits non-zero. -/
private def git (dir : String) (args : List String) : IO (Option String) := do
  try
    let out ← IO.Process.output { cmd := "git", args := (["-C", dir] ++ args).toArray }
    return if out.exitCode == 0 then some out.stdout.trim else none
  catch _ => return none

/-- Where the framework's own files are: here, or where Lake keeps the package. -/
def platformDir : IO String := do
  if ← System.FilePath.pathExists "Spec/Check.lean" then return "."
  let manifest := (Json.parse (← try IO.FS.readFile "lake-manifest.json" catch _ => pure "")).toOption.getD .null
  let text (j : Json) (k : String) : String := (j.getObjVal? k >>= Json.getStr?).toOption.getD ""
  for p in (manifest.getObjVal? "packages" >>= Json.getArr?).toOption.getD #[] do
    if text p "name" == "quintessence" then
      return if text p "type" == "path" then text p "dir" else s!"{text manifest "packagesDir"}/quintessence"
  return "."

/-- A framework file named in a binding (`adapters/python.py`), where it really is. -/
private def platformFile (path : String) : IO String := do
  let there := s!"{← platformDir}/{path}"
  return if !(← System.FilePath.pathExists path) && (← System.FilePath.pathExists there) then there else path

/-- This area's ledger as recorded at a git revision (`none`: as it is on disk). -/
private def ledgerAt (area : String) (ref : Option String) : IO Json := do
  let text ← match ref with
    | some r => pure ((← git "." ["show", s!"{r}:./ledger/{area}.json"]).getD "")
    | none => try IO.FS.readFile s!"ledger/{area}.json" catch _ => pure ""
  return (Json.parse text).toOption.getD .null

/-- What git says the content of these paths is: one fingerprint over the
object ids of their files, committed or not (ignored files left out). -/
private def gitState (dir : String) (paths : List String) : IO String := do
  let listed := (← git dir (["ls-files", "-co", "--exclude-standard", "--"] ++ paths)).getD ""
  let mut files : List String := []
  for f in listed.splitOn "\n" do
    if f != "" && (← System.FilePath.pathExists (System.FilePath.mk dir / f)) then files := f :: files
  let sorted := (files.toArray.qsort (· < ·)).toList  -- the same whether a file is committed yet or not
  let ids := (← git dir (["hash-object", "--"] ++ sorted)).getD "not in git"
  return toString (hash (sorted, ids))

/-- The files a code verdict depends on in this repository: the area's own
files (`Areas/<Name>.lean` and `Areas/<Name>/`), the checker and the adapter
(their files, or the Lake manifest that pins them), and the code when it
lives here. -/
private def inputsOf (area : String) (c : Code σ) : IO (List String) := do
  let plain (s : String) : String := (s.toLower.replace "-" "").replace "_" ""
  let own := (← System.FilePath.readDir "Areas").toList.filterMap fun e =>
    if plain (e.fileName.takeWhile (· != '.')) == plain area then some s!"Areas/{e.fileName}" else none
  let platform ← platformDir
  let adapter ← (← c.adapter.mapM platformFile).filterM fun (a : String) =>
    System.FilePath.pathExists (System.FilePath.mk a)
  return own ++ [s!"{platform}/Spec/Check.lean", s!"{platform}/Spec/Data.lean", "lake-manifest.json"]
    ++ adapter ++ (if c.repo == "." then c.paths else [])

/-- Drive the code through the model: for every state of the scope, nearest
first, every accepted action, and every refused action in the state it leads
to. Each run starts from a fresh system. Stops at the first difference. -/
private def checkCode [Hashable σ] [Repr α] (A : Area σ α) (c : Code σ)
    (since : Option String) (full : Bool) : IO CodeResult := do
  let names := A.actions.map (actionName ∘ label)
  if let some n := names.find? fun n => !(c.actions.any (·.1 == n)) then
    return { status := "error", verdict := s!"action `{n}` has no code mapping" }
  let inputs ← gitState "." (← inputsOf A.name c)
  -- where the code is, and for another repository, which commit of it
  let mut root := "."
  let mut commit := ""
  if c.repo != "." then
    let config := (Json.parse (← try IO.FS.readFile ".spec/local.json" catch _ => pure "")).toOption.getD .null
    let some path := ((config.getObjValD "repo_paths").getObjVal? c.repo >>= Json.getStr?).toOption
      | return { status := "error", verdict :=
          s!"repository `{c.repo}` has no local checkout; add it to `repo_paths` in .spec/local.json" }
    root := path
    let last := (← git root (["log", "-1", "--format=%H", "--"] ++ c.paths)).getD "unknown"
    let dirty := (← git root (["status", "--porcelain", "--"] ++ c.paths)) != some ""
    commit := if dirty then s!"{last} with uncommitted changes" else last
  let adapter ← c.adapter.mapM platformFile
  let child ← IO.Process.spawn {
    cmd := adapter.headD "", args := (adapter.drop 1 ++ [root]).toArray, stdin := .piped, stdout := .piped }
  let ask (j : Json) : IO Json := do
    child.stdin.putStrLn j.compress
    child.stdin.flush
    let reply ← IO.ofExcept (Json.parse (← child.stdout.getLine))
    if let .ok e := reply.getObjVal? "error" then throw (IO.userError (e.getStr?.toOption.getD e.compress))
    return reply
  let hello ← ask <| Json.mkObj [("setup", .str c.setup),
    ("actions", Json.mkObj (c.actions.map fun (n, code) => (n, .str code))),
    ("observe", .arr (c.observe.map fun (_, _, e) => .str e).toArray),
    -- every concrete call that can follow, for adapters that compile the table
    ("calls", .arr ((A.actions ++ A.journeys.flatMap (·.moments.map (·.action))
        ++ A.examples.flatMap (fun e => e.run ++ e.refused.toList)).map (call ∘ label)).eraseDups.toArray)]
  let mapped := c.setup :: c.actions.map (·.2) ++ c.observe.map (·.2.2)
  let operations := ((hello.getObjVal? "operations" >>= Json.getArr?).toOption.getD #[]).toList.filterMap
    (·.getStr?.toOption)
  let questions := (operations.filter fun op => !(mapped.any fun m => (m.splitOn op).length > 1)).map
    (s!"The code has operation `{·}` that the model does not use. Should the model cover it?")
  let finish : IO Unit := do  -- closing its input ends the adapter
    let (_, child) ← child.takeStdin
    discard child.wait
  -- git reports the same content as when agreement was recorded: the verdict stands
  let old := (← ledgerAt A.name since).getObjValD "code"
  let was (k : String) : String := (old.getObjVal? k >>= Json.getStr?).toOption.getD ""
  if !full && was "status" == "agrees" && was "inputs" == inputs && was "commit" == commit
      && !commit.endsWith "changes" then
    finish
    return { status := "agrees", verdict := was "verdict", questions, inputs, commit, reused := true }
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
    let mut done : List String := []  -- the actions taken so far
    for (a, r) in run.zip ((reply.getObjVal? "steps" >>= Json.getArr?).toOption.getD #[]).toList do
      let refused := (r.getObjVal? "refused" >>= Json.getBool?).toOption.getD false
      let next := A.step a s
      let after := if done.isEmpty then "At the start" else s!"After {", ".intercalate done.reverse}"
      rows := rows.push (row (label a) next.isNone (expect (next.getD s)) (texts r "observed"))
      if next.isNone && !refused then
        return some (s!"{after}, the model refuses `{label a}`; the code accepts it.", .arr rows)
      if next.isSome && refused then
        return some (s!"{after}, the model accepts `{label a}`; the code refuses it ({(r.getObjVal? "error" >>= Json.getStr?).toOption.getD ""}).", .arr rows)
      s := next.getD s
      if next.isSome then done := label a :: done
      if let some d := differ (expect s) (texts r "observed") then
        return some (s!"After {", ".intercalate done.reverse}, {d}.", .arr rows)
    return none
  let refusedIn (s : σ) : List α := A.actions.filter fun a => (A.step a s).isNone
  -- first the runs a person has read, whatever the budget: journeys and examples
  let mut drift : Option (String × Json) := none
  for run in A.journeys.map (·.moments.map (·.action)) ++ A.examples.map (fun e => e.run ++ e.refused.toList) do
    if drift.isNone then drift ← runOne run
  if drift.isNone then drift ← runOne (refusedIn A.init)
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
  finish
  match drift with
  | some (what, rows) =>
    return { status := "drift", verdict := what, drift := some rows, questions, inputs, commit }
  | none =>
    let reach := match stoppedAt with
      | none => "of the scope"
      | some d => s!"within {d} steps of the start"
    return { status := "agrees", questions, inputs, commit,
             verdict := s!"the code agrees with the model on every journey and example, and on every transition and refusal {reach} "
               ++ s!"({transitions} transitions, {used} code steps)" }

private def build [Hashable σ] [Repr σ] [Repr α] (A : Area σ α)
    (since : Option String) (rerun : Bool) : IO Report := do
  -- what was recorded at the revision this run is compared with, for the review
  let before ← match since with
    | some r => ledgerAt A.name (some r)
    | none => pure .null
  let code ← match A.code with
    | none => pure {}
    | some c => try checkCode A c since rerun catch e =>
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
  let mut questions : Array (String × Option (List α)) := A.questions.toArray.map (·, none)
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
  -- journeys: the stories a person reads, replayed on the model
  let stateJson (s : σ) : Json :=
    .arr ((fields (short (oneLine s))).map fun (k, v) => .arr #[.str k, .str v]).toArray
  let viewsJson (s : σ) : Json := .arr (A.views.map fun (n, v) => .arr #[.str n, .str (v s)]).toArray
  let mut journeys : Array (String × Bool × (Bool → Json)) := #[]
  let mut told : List String := []  -- the requirements some journey shows at work
  for j in A.journeys do
    let mut s := A.init
    let mut ok := true
    let mut rows : Array (Bool → Json) := #[]
    for m in j.moments do
      let next := A.step m.action s
      let reqs := (A.requirements.filter (·.when s m.action)).map (·.id)
      let after := next.getD s
      told := told ++ reqs
      if next.isNone != m.refused then ok := false
      rows := rows.push fun full => Json.mkObj <| [
        ("say", .str m.say), ("action", .str (label m.action)), ("refused", .bool next.isNone),
        ("asTold", .bool (next.isNone == m.refused)), ("requirements", .arr (reqs.map .str).toArray)]
        ++ (if full then [("views", viewsJson after), ("state", stateJson after)] else [])
      s := after
    let status := if ok then "ok" else "failed"
    journeys := journeys.push (j.id, ok, fun full => Json.mkObj <| [
      ("id", .str j.id), ("title", .str j.title), ("goal", .str j.goal), ("sources", .arr (j.sources.map .str).toArray),
      ("status", .str status),
      ("moments", .arr (rows.map (· full)))]
      ++ (if full then [("startViews", viewsJson A.init), ("start", stateJson A.init)] else []))
  if A.journeys.isEmpty then
    questions := questions.push
      ("The area has no journey. Which stories should a person recognise it by?", none)
  else
    for q in A.requirements do
      if !told.contains q.id then
        questions := questions.push
          (s!"{q.id} is not part of any journey. Is there a story in which it matters?", none)
  -- open questions, one per action name
  let distinct := names.foldl (fun acc n => if acc.contains n then acc else acc.push n) #[]
  for n in distinct do
    for refused in [false, true] do
      let hit := (List.range acts.size).findSome? fun i =>
        if names[i]! == n then (f.gaps[2 * i + (if refused then 1 else 0)]!).map (·, i) else none
      if let some (fp, i) := hit then
        let run := (trace fp (some i)).map label
        let last := run.getLast?.getD n
        let after := if run.length ≤ 1 then "at the start" else s!"after {", ".intercalate run.dropLast}"
        questions := questions.push
          (if refused
            then s!"`{last}` is refused {after}, and no requirement covers that. Is refusing right there?"
            else s!"`{last}` is accepted {after}, and no requirement covers that. What must it do there?",
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
  let sources := A.invariants.map (fun i => (i.id, i.sources)) ++ A.requirements.map (fun q => (q.id, q.sources))
  let json (full : Bool) : Json := Json.mkObj <| (if full then [("since", (since.map Json.str).getD .null), ("before", before)] else []) ++ [
    ("area", .str A.name),
    ("search", Json.mkObj [
      ("states", num states), ("depth", num f.depth), ("exhaustive", .bool f.exhaustive),
      ("budget", num A.budget), ("actions", .arr (labels.map .str)), ("cutByBound", num f.cut)]),
    ("code", Json.mkObj <| [("status", .str code.status), ("verdict", .str code.verdict),
        ("level", num (if code.status == "agrees" then 4 else 6))]
      ++ (A.code.map fun c => [("inputs", Json.str code.inputs), ("repo", Json.str c.repo), ("paths", .arr (c.paths.map .str).toArray),
          ("setup", Json.str c.setup),
          ("actions", Json.mkObj (c.actions.map fun (n, x) => (n, .str x))),
          ("observe", .arr (c.observe.map fun (n, _, x) => .arr #[.str n, .str x]).toArray)]).getD []
      ++ (if code.commit == "" then [] else [("commit", .str code.commit)])
      ++ (if full then (code.drift.map (("steps", ·))).toList else [])),
    ("claims", .arr <| claims.map fun (id, kind, text, level, target, verdict, cex) =>
      withSteps full cex [("id", .str id), ("kind", .str kind), ("text", .str text),
        ("level", num level), ("target", num target), ("verdict", .str verdict),
        ("status", .str (status level target verdict)), ("sources", .arr (((sources.lookup id).getD []).map .str).toArray)]),
    ("journeys", .arr <| journeys.map fun (_, _, json) => json full),
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
    ++ (journeys.toList.filterMap fun (id, ok, _) => if ok then none else some s!"{id} does not go as told")
  let codeFailed := if code.status == "drift" || code.status == "error" then [s!"code: {code.verdict}"] else []
  let below := (claims.filter fun (_, _, _, level, target, verdict, _) =>
    level > target && verdict != "violated").size
  let summary := s!"{A.name}: {states} states, "
    ++ (if f.exhaustive then "scope exhausted" else s!"stopped at budget, depth {f.depth}")
    ++ s!"; {claims.size} claims, {below} below target, {failed.length} failed; "
    ++ s!"{questions.size} open questions; code {code.status}"
    ++ (if code.reused then " (unchanged in git since recorded, not re-run)" else "")
  return { ledger := json false, review := json true, failed := failed ++ codeFailed, summary }

/-- Package an area for the checker program. -/
def Area.entry [Hashable σ] [Repr σ] [Repr α] (A : Area σ α) : Entry :=
  { name := A.name, report := build A }

end
end Spec
