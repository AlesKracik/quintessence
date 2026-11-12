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

People read an area through its journeys (one business case each, as its
usual course) and walkthroughs (specific runs worth knowing). Both are
replayed on the model, and what the readback shows of them comes from that.

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

/-- One step of a journey's usual course: what happens, in general words, and
the evidence that the model takes the step: one or more concrete actions, in
order. More than one says that the step can repeat, and the readback draws a
loop on it. The actions are evidence only. A journey is generic, and the
readback shows neither them nor any value they produce. -/
structure Step (α : Type) where
  say : String
  actions : List α

/-- A step of a journey: `.step "The operator adds a VM." [.enroll "a", .enroll "b"]`. -/
def Step.step {α : Type} (say : String) (actions : List α) : Step α := ⟨say, actions⟩

/-- A journey: one business case, such as "enrolling a VM" or "a protection
run", as the usual course of events from start to end, in general terms: no
particular VM, customer or run, and no numbers except thresholds that a
requirement states. The readback draws it as a graph: the steps in order, a
loop on a step that can repeat, and hanging off each step every way the system
can turn that step down. Those are not written here: they are the area's
refusal requirements for the step's action, so none can be left out. The
concrete actions are the evidence that the course can be taken: every one of
them must be accepted, in order. -/
structure Journey (α : Type) where
  id : String
  title : String
  /-- Who wants what. -/
  goal : String
  /-- Where the journey starts, in general words ("the group has VMs") ... -/
  given : String := ""
  /-- ... and the run that gets there from the initial state. -/
  setup : List α := []
  steps : List (Step α)
  /-- Where the journey comes from, as a history (see `Requirement.sources`). -/
  sources : List String := []

/-- One moment of a walkthrough: what happens, in the words of the person it
happens to, the action that does it, whether the system turns it down, and
optionally what must be true once the moment is over. -/
structure Moment (σ α : Type) where
  say : String
  action : α
  refused : Bool := false
  expect : Option (σ → Bool) := none

/-- A moment in which the system does what is asked. -/
def Moment.step {σ α : Type} (say : String) (action : α) : Moment σ α := { say, action }
/-- A moment in which the system turns the request down; nothing changes. -/
def Moment.refusal {σ α : Type} (say : String) (action : α) : Moment σ α := { say, action, refused := true }
/-- A moment in which the system does what is asked, and what must then be
true. `say` tells the outcome; `expect` is that outcome as a check. -/
def Moment.outcome {σ α : Type} (say : String) (action : α) (expect : σ → Bool) : Moment σ α :=
  { say, action, expect := some expect }

/-- A walkthrough: one specific run that matters, told moment by moment: a
failure and the recovery from it, a boundary, a message that arrives twice.
Where a journey shows the usual course of a business case, a walkthrough
shows one telling instance. Every moment must go the way it is told, and
every stated outcome must hold. -/
structure Walkthrough (σ α : Type) where
  id : String
  title : String
  /-- Why this run matters. -/
  goal : String := ""
  /-- The id of the journey this is a case of, if any; the readback shows it there. -/
  journey : String := ""
  moments : List (Moment σ α)
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
  journeys : List (Journey α) := []
  walkthroughs : List (Walkthrough σ α) := []
  /-- Named projections of the state in words a reader knows ("seat s1" is
  "held by alice"), one per thing in the scope. The readback tells the
  walkthroughs in them: a moment shows which views changed, not the raw state. -/
  views : List (String × (σ → String)) := []
  /-- The principal state machines of the area: one per kind of thing with a
  life of its own (a group, a run, a copy of a recovery point at a site), with
  its states in the person's words. The function gives the state of every
  possible instance of that kind, always the same number of them in the same
  order ("none" where there is none yet). The readback draws one diagram per
  kind, over all instances together: generic, like a journey, with no
  particular instance and no counters in it. -/
  lifecycles : List (String × (σ → List String)) := []
  /-- Per action name: the decision on whether it can interleave with other
  events inside the implementation. A missing entry is an open question. -/
  atomic : List (String × String) := []
  /-- Questions for a person that the checker cannot find by itself: what a
  document says against what the code does, what a document leaves unsaid.
  They stay in the readback until answered; removing one means it was. -/
  questions : List String := []
  /-- What the model takes for granted, simplifies or leaves out, one plain
  sentence each: what the scope cuts off, what stands in for the outside
  world, what was not looked at. Every claim holds only under these, so the
  readback shows them to the person and the ledger records them. -/
  assumptions : List String := []
  code : Option (Code σ) := none

/-- What one area reports. `ledger` is committed; `readback` adds the traces. -/
structure Report where
  ledger : Json
  readback : Json
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
  /-- Whether requirement `j` applied to action `i` anywhere: at `j * actions + i`. -/
  reqAct : Array Bool
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
  let lifes := A.lifecycles.toArray
  -- paired with their positions once: the loops below run for every state and action
  let actsI := acts.zipIdx
  let invsI := invs.zipIdx
  let lifesI := lifes.zipIdx
  let root := hash A.init
  let mut seen : Std.HashMap UInt64 (UInt64 × Nat) := Std.HashMap.emptyWithCapacity 1024
  seen := seen.insert root (root, 0)
  let mut invFail : Array (Option UInt64) :=
    invs.map fun i => if i.holds A.init then none else some root
  let mut reqFail : Array (Option (UInt64 × Nat)) := reqs.map fun _ => none
  let mut reqHits : Array Nat := reqs.map fun _ => 0
  let mut reqAct : Array Bool := Array.replicate (reqs.size * acts.size) false
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
      let ls := lifes.map fun l => l.2 s
      for (a, i) in actsI do
        let r := A.step a s
        let speaking := applying reqs s a reqs.size []
        for (q, j) in speaking do
          reqHits := reqHits.modify j (· + 1)
          reqAct := reqAct.set! (j * acts.size + i) true
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
          -- an instance that changed state is one transition of its kind's machine
          for (l, k) in lifesI do
            for (a, b) in ls[k]!.zip (l.2 t) do
              if a != b then edges := edges.insert (k, a, names[i]!, b)
          let ft := hash t
          if !seen.contains ft then
            seen := seen.insert ft (fs, i)
            next := next.push t
            for (inv, j) in invsI do
              if !inv.holds t && invFail[j]!.isNone then invFail := invFail.set! j (some ft)
    if !stopped then depth := depth + 1
    frontier := next
  return { seen, depth, exhaustive := !stopped, cut, invFail, reqFail, reqHits, reqAct, gaps, edges }

/-- The actions leading from the initial state to the state with this fingerprint. -/
private def pathTo (seen : Std.HashMap UInt64 (UInt64 × Nat)) (root : UInt64) :
    Nat → UInt64 → List Nat → List Nat
  | 0, _, acc => acc
  | n + 1, fp, acc =>
    if fp == root then acc else
    match seen[fp]? with
    | none => acc
    | some (parent, i) => pathTo seen root n parent (i :: acc)

/-- A run from the initial state as rows for the readback: action, state after
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
  /-- The run that shows the drift, as rows for the readback. -/
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
  -- the runs a person has read: every journey and every walkthrough
  let told := A.journeys.map (fun j => j.setup ++ j.steps.flatMap (·.actions))
    ++ A.walkthroughs.map (·.moments.map (·.action))
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
    ("calls", .arr ((A.actions ++ told.flatten).map (call ∘ label)).eraseDups.toArray)]
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
  -- first the runs a person has read, whatever the budget
  let mut drift : Option (String × Json) := none
  for run in told do
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
             verdict := s!"the code agrees with the model on every journey and walkthrough, and on every transition and refusal {reach} "
               ++ s!"({transitions} transitions, {used} code steps)" }

private def build [Hashable σ] [Repr σ] [Repr α] (A : Area σ α)
    (since : Option String) (rerun : Bool) : IO Report := do
  -- what was recorded at the revision this run is compared with, for the readback
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
  let stateJson (s : σ) : Json :=
    .arr ((fields (short (oneLine s))).map fun (k, v) => .arr #[.str k, .str v]).toArray
  let viewsJson (s : σ) : Json := .arr (A.views.map fun (n, v) => .arr #[.str n, .str (v s)]).toArray
  let speaking (s : σ) (a : α) : List String := (A.requirements.filter (·.when s a)).map (·.id)
  let mut told : List String := []  -- the requirements some journey or walkthrough shows at work
  let mut walked : List String := []  -- those a walkthrough shows happening
  let mut leaves : List (String × String) := []  -- a refusal requirement and the journey it turns a step down in
  -- journeys: the usual course of a business case, replayed on the model
  let mut journeys : Array (String × Bool × (Bool → Json)) := #[]
  for j in A.journeys do
    let mut s := A.init
    let mut ok := !j.steps.isEmpty
    for a in j.setup do
      match A.step a s with
      | some t => s := t
      | none => ok := false
    let mut rows : Array (Bool → Json) := #[]
    for st in j.steps do
      let mut reqs : List String := []
      let mut went := !st.actions.isEmpty
      for a in st.actions do
        match A.step a s with
        | some t =>
          reqs := reqs ++ (speaking s a).filter (!reqs.contains ·)
          s := t
        | none => went := false
      -- every way the system turns this step down: the refusal requirements that speak about its action
      let stepNames := st.actions.map (actionName ∘ label)
      let turnedDown := (A.requirements.zipIdx.filter fun (q, k) =>
        q.refused && (List.range acts.size).any fun i =>
          stepNames.contains names[i]! && f.reqAct[k * acts.size + i]!).map (·.1.id)
      told := told ++ reqs
      leaves := leaves ++ turnedDown.map (·, j.id)
      if !went then ok := false
      rows := rows.push fun _ => Json.mkObj <| [
        ("say", .str st.say), ("actions", .arr (st.actions.map (.str ∘ label)).toArray),
        ("repeats", .bool (st.actions.length > 1)),
        ("asTold", .bool went), ("requirements", .arr (reqs.map .str).toArray),
        ("turnedDown", .arr (turnedDown.map .str).toArray)]
    let status := if ok then "ok" else "failed"
    journeys := journeys.push (j.id, ok, fun full => Json.mkObj <| [
      ("id", .str j.id), ("title", .str j.title), ("goal", .str j.goal), ("given", .str j.given),
      ("setup", .arr (j.setup.map (.str ∘ label)).toArray), ("sources", .arr (j.sources.map .str).toArray),
      ("status", .str status), ("steps", .arr (rows.map (· full)))])
  -- walkthroughs: specific runs that matter, replayed moment by moment
  let mut walkthroughs : Array (String × Bool × (Bool → Json)) := #[]
  for w in A.walkthroughs do
    let mut s := A.init
    let mut ok := true
    let mut rows : Array (Bool → Json) := #[]
    for m in w.moments do
      let next := A.step m.action s
      let reqs := speaking s m.action
      let after := next.getD s
      let holds := (m.expect.map (· after)).getD true
      told := told ++ reqs
      walked := walked ++ reqs
      if next.isNone != m.refused || !holds then ok := false
      rows := rows.push fun full => Json.mkObj <| [
        ("say", .str m.say), ("action", .str (label m.action)), ("refused", .bool next.isNone),
        ("asTold", .bool (next.isNone == m.refused)), ("requirements", .arr (reqs.map .str).toArray)]
        ++ (if m.expect.isSome then [("outcome", Json.bool holds)] else [])
        ++ (if full then [("views", viewsJson after), ("state", stateJson after)] else [])
      s := after
    let status := if ok then "ok" else "failed"
    walkthroughs := walkthroughs.push (w.id, ok, fun full => Json.mkObj <| [
      ("id", .str w.id), ("title", .str w.title), ("goal", .str w.goal), ("journey", .str w.journey),
      ("sources", .arr (w.sources.map .str).toArray), ("status", .str status),
      ("moments", .arr (rows.map (· full)))]
      ++ (if full then [("startViews", viewsJson A.init), ("start", stateJson A.init)] else []))
  if A.journeys.isEmpty then
    questions := questions.push
      ("The area has no journey. Which business cases should a person recognise it by?", none)
  else
    for q in A.requirements do
      -- a way of being turned down is only shown by a run in which it happens
      let on := ((leaves.filter (·.1 == q.id)).map (·.2)).eraseDups
      if !on.isEmpty && !walked.contains q.id then
        questions := questions.push
          (s!"{q.id} turns down a step of {", ".intercalate on}, and no walkthrough shows it happening. Which run shows it?", none)
      else if on.isEmpty && !told.contains q.id then
        questions := questions.push
          (s!"{q.id} shows in no journey and no walkthrough. Is there a case in which it matters?", none)
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
    ("walkthroughs", .arr <| walkthroughs.map fun (_, _, json) => json full),
    ("questions", .arr <| questions.map fun (text, run) => withSteps full run [("text", .str text)]),
    ("atomic", Json.mkObj (A.atomic.map fun (n, d) => (n, .str d))),
    ("assumptions", .arr (A.assumptions.map .str).toArray),
    ("lifecycles", .arr <| (A.lifecycles.zipIdx.map fun ((name, l), k) => Json.mkObj [
      ("name", .str name), ("initial", .arr ((l A.init).eraseDups.map .str).toArray),
      ("edges", .arr <| (sortedEdges.filter (·.1 == k)).map fun (_, a, n, b) =>
        .arr #[.str a, .str n, .str b])]).toArray)]
  let failed := (claims.toList.filterMap fun (id, _, _, _, _, verdict, _) =>
      if verdict == "violated" then some s!"{id} violated" else none)
    ++ ((journeys ++ walkthroughs).toList.filterMap fun (id, ok, _) =>
      if ok then none else some s!"{id} does not go as told")
  let codeFailed := if code.status == "drift" || code.status == "error" then [s!"code: {code.verdict}"] else []
  let below := (claims.filter fun (_, _, _, level, target, verdict, _) =>
    level > target && verdict != "violated").size
  let summary := s!"{A.name}: {states} states, "
    ++ (if f.exhaustive then "scope exhausted" else s!"stopped at budget, depth {f.depth}")
    ++ s!"; {claims.size} claims, {below} below target, {failed.length} failed; "
    ++ s!"{questions.size} open questions; code {code.status}"
    ++ (if code.reused then " (unchanged in git since recorded, not re-run)" else "")
  return { ledger := json false, readback := json true, failed := failed ++ codeFailed, summary }

/-- Package an area for the checker program. -/
def Area.entry [Hashable σ] [Repr σ] [Repr α] (A : Area σ α) : Entry :=
  { name := A.name, report := build A }

end
end Spec
