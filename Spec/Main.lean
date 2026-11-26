import Lean
import Spec.Check

/-!
# The checker program

A project's `Main.lean` is three lines:

    import Spec.Main
    import Areas
    checker_main Areas.all

`checker_main` is the gate on what counts as proven, and then defines the
program. It compiles only if

* every proof attached to an area rests on nothing but Lean's three standard
  axioms (so no `sorry`, no `native_decide`, no added axiom), and
* no file under `Areas/` uses a construct that lets compiled code differ from
  the definitions the proofs are about, or that skips the kernel.

The program, `lake exe check`, is run from the project's root. It runs every
area, writes `ledger/<area>.json` (committed) and the readback under `out/readback/` (one page per
area, and with `--since` one page for the change across areas), prints one line per area, and fails if any claim is
violated, any example fails, or code and model differ.

    --since <rev>   compare with that git revision: the readback shows what changed
                    since, and that revision's ledger is what counts as recorded
    --full          re-run every code check
    --only <name>   run only that area or part (`protection`, `protection/runs`);
                    the others are still compared with it, and keep their ledgers.
                    The readback is written only for areas that were run whole.
    --fresh-within <days>
                    how old the last fetch of a source checkout may be before it
                    is fetched again, for this run: `0` fetches now, a large
                    number uses what is there. Without it the project's
                    `fresh_within_days` in `.spec/project.json` counts, or one day.

Sources are read fresh. Before anything else the program looks at every
checkout named in `.spec/local.json` (`repo_paths`): one that was not fetched
within the threshold is fetched (the remote's state only; the working tree is
not touched), and one that is behind its remote is named, so that nobody reads
from it unawares. What an area was read from (`readAt`) is then compared with
the remote as fetched.
-/

namespace Spec

/-- Sources are read fresh: fetch every checkout that was not fetched within
the threshold, and name the ones that are behind their remote. Fetching takes
the remote's state only; no working tree is changed. -/
def freshen (override : Option Nat) : IO Unit := do
  let file (path : String) : IO Lean.Json :=
    return (Lean.Json.parse (← try IO.FS.readFile path catch _ => pure "")).toOption.getD .null
  let days := override.getD <|
    (((← file ".spec/project.json").getObjVal? "fresh_within_days" >>= Lean.Json.getNat?).toOption.getD 1)
  let now := ((← IO.Process.output { cmd := "date", args := #["+%s"] }).stdout.trim.toNat?).getD 0
  let checkouts := match (← file ".spec/local.json").getObjVal? "repo_paths" with
    | .ok (.obj kvs) => kvs.fold (fun (acc : List (String × String)) k (v : Lean.Json) => acc ++ [(k, v.getStr?.toOption.getD "")]) []
    | _ => []
  for (name, dir) in checkouts do
    -- when the remote was last asked: the last fetch, or the clone
    let mut last := 0
    for f in [".git/FETCH_HEAD", ".git/HEAD"] do
      if last == 0 then
        let seen ← try some <$> System.FilePath.metadata (System.FilePath.mk dir / f) catch _ => pure none
        last := match seen with | some m => m.modified.sec.toNat | none => 0
    if last == 0 then continue  -- not a git checkout: nothing to ask
    -- a project whose sources must not be asked for (snapshots, no network) says `"fetch": false`
    let mayFetch := ((← file ".spec/project.json").getObjVal? "fetch" >>= Lean.Json.getBool?).toOption.getD true
    if mayFetch && now ≥ last + days * 86400 then
      -- never wait for a password or a hanging connection
      let quiet : Array (String × Option String) :=
        #[("GIT_TERMINAL_PROMPT", some "0"), ("GIT_SSH_COMMAND", some "ssh -oBatchMode=yes -oConnectTimeout=10")]
      let fetch : IO.Process.SpawnArgs := { cmd := "git", args := #["-C", dir, "fetch", "--quiet"], env := quiet }
      let fetched ← try (fun (out : IO.Process.Output) => out.exitCode == 0) <$> IO.Process.output fetch catch _ => pure false
      IO.eprintln <| if fetched then s!"sources: fetched `{name}` (last asked {(now - last) / 86400} days ago)"
        else s!"sources: `{name}` could not be fetched; what was last fetched {(now - last) / 86400} days ago is used"
    let behind := ((← git dir ["rev-list", "--count", "HEAD..@{u}"]).bind (·.toNat?)).getD 0
    if behind > 0 then
      IO.eprintln s!"sources: the checkout of `{name}` ({dir}) is {behind} commits behind its remote; bring it up to date, or read from a fresh copy, before reading from it"

/-- The ways into the code, counted from the code itself and not from what an
agent listed. `.spec/project.json` says for each repository what a way in
looks like: `"ways_in": {"orders-service": {"files": [".kt"], "under":
["src/main"], "marks": ["@PostMapping", "@KafkaListener", "@Scheduled"]}}`
(plain text, no patterns; `under` and `skip` are parts of the path). Every
line of the checkout that carries a mark is a way in. It is listed when some
part names its file with lines that reach it (a mark stands a few lines above
what it marks), or names the file with no lines. The rest are in no part:
nobody has said whose they are.

The variants behind the ways in are counted the same way, from `"variants"`
with the same fields: the marks say what a place looks like where the code
has one course per kind (a class per kind: `: ProtectionJob(`; the entries of
an enumeration the code switches on). Such a line is named when some way in
says its variants (`.branches`) and gives the line's file among the places
where the code picks one. The rest are named on no way in: no journey can
show them. -/
def counted (key word loose' : String) (cited : List (String × List (Nat × Nat))) : IO (List String × Lean.Json) := do
  let file (path : String) : IO Lean.Json :=
    return (Lean.Json.parse (← try IO.FS.readFile path catch _ => pure "")).toOption.getD .null
  let strs (j : Lean.Json) (k : String) : List String :=
    ((j.getObjVal? k >>= Lean.Json.getArr?).toOption.getD #[]).toList.filterMap (·.getStr?.toOption)
  let here ← file ".spec/local.json"
  let has (t w : String) : Bool := (t.splitOn w).length > 1
  let mut lines : List String := []
  let mut out : List (String × Lean.Json) := []
  match (← file ".spec/project.json").getObjVal? key with
  | .ok (.obj repos) =>
    for (repo, cfg) in repos.fold (fun (acc : List (String × Lean.Json)) k v => acc ++ [(k, v)]) [] do
      let some dir := (here.getObjVal? "repo_paths" >>= (·.getObjVal? repo) >>= Lean.Json.getStr?).toOption
        | lines := lines ++ [s!"{word}: `{repo}` has no checkout in .spec/local.json; not counted"]
      let exts := strs cfg "files"; let under := strs cfg "under"; let marks := strs cfg "marks"
      let skip := strs cfg "skip" ++ ["/.git/", "/node_modules/", "/build/", "/test/", "/tests/"]
      let mut found := 0
      let mut loose : List String := []
      let all ← try System.FilePath.walkDir dir (enter := fun d =>
          pure (!([".git", "node_modules", "build"].contains (d.fileName.getD "")))) catch _ => pure #[]
      for f in all.qsort (·.toString < ·.toString) do
        let path := f.toString
        if !(exts.any path.endsWith) || skip.any (has path) || !(under.isEmpty || under.any (has path)) then continue
        let text ← try IO.FS.readFile f catch _ => pure ""
        let rel := (path.drop dir.length).dropWhile (· == '/')
        for (l, k) in (text.splitOn "\n").zipIdx do
          if marks.any (has l) then
            found := found + 1
            let n := k + 1
            let listed := cited.any fun (c, ls) =>
              ((c.splitOn "/").reverse.zip (rel.splitOn "/").reverse).all (fun (x, y) => x == y)
                && (ls.isEmpty || ls.any fun (lo, hi) => lo ≤ n + 8 && n ≤ hi)
            if !listed then loose := loose ++ [s!"{rel}:{n}  {(l.trim.take 110)}"]
      lines := lines ++ [s!"{word}: `{repo}`: {found} in the code, {loose.length} {loose'}"]
      out := out ++ [(repo, Lean.Json.mkObj [("found", Lean.Json.num found), ("inNoPart", .arr (loose.map Lean.Json.str).toArray)])]
  | _ => pure ()
  return (lines, Lean.Json.mkObj out)

def run (areas : List Entry) (args : List String) : IO UInt32 := do
  let since := match args.dropWhile (· != "--since") with
    | _ :: ref :: _ => some ref
    | _ => none
  let only := match args.dropWhile (· != "--only") with
    | _ :: name :: _ => some name
    | _ => none
  -- anything else on the command line is a mistake, not a reason to run everything
  let fresh := match args.dropWhile (· != "--fresh-within") with
    | _ :: days :: _ => days.toNat?
    | _ => none
  let rec known : List String → Bool
    | [] => true
    | "--full" :: rest => known rest
    | "--since" :: _ :: rest | "--only" :: _ :: rest => known rest
    | "--fresh-within" :: days :: rest => days.toNat?.isSome && known rest
    | _ => false
  if !known args then
    IO.eprintln "usage: lake exe check [--since <rev>] [--full] [--only <area or part>] [--fresh-within <days>]"
    return (1 : UInt32)
  -- the names: an area is `<area>`, a part of an area is `<area>/<part>`, and nothing else
  let word (w : String) : Bool := w != "" && w.all fun c => c.isLower || c.isDigit || c == '-'
  let names := areas.map (·.name)
  let misnamed := names.filter fun n => !((n.splitOn "/").length ≤ 2 && (n.splitOn "/").all word)
  let twice := names.filter fun n => (names.filter (· == n)).length > 1
  let both := names.filter fun n => names.any (·.startsWith (n ++ "/"))
  if !misnamed.isEmpty || !twice.isEmpty || !both.isEmpty then
    for n in misnamed do
      IO.eprintln s!"names: `{n}` is not a name of an area (`protection`) or of a part of an area (`protection/runs`): lower-case words joined by hyphens, one slash at most"
    for n in twice.eraseDups do IO.eprintln s!"names: `{n}` is named twice"
    for n in both do IO.eprintln s!"names: `{n}` has parts and a model of its own; an area with parts is only its parts"
    return (1 : UInt32)
  freshen fresh
  let wanted (name : String) : Bool := match only with
    | some o => name == o || name.startsWith (o ++ "/")
    | none => true
  if only.isSome && !areas.any (wanted ·.name) then
    IO.eprintln s!"no area or part is called {only.getD ""}"
    return (1 : UInt32)
  IO.FS.createDirAll "ledger"
  IO.FS.createDirAll "out/readback"
  let template ← IO.FS.readFile s!"{← platformDir}/Spec/readback.html"
  -- one page of the readback: what kind it is, and the areas it shows
  let page (file kind : String) (data : Array Lean.Json) (more : List (String × Lean.Json) := []) : IO Unit :=
    IO.FS.writeFile s!"out/readback/{file}.html" <|
      (template.replace "/*DATA*/[]" ((Lean.Json.arr data).compress.replace "</" "<\\/")).replace "/*PAGE*/{}"
        ((Lean.Json.mkObj ([("kind", .str kind), ("since", (since.map Lean.Json.str).getD .null)] ++ more)).compress.replace "</" "<\\/")
  -- An area can have parts, each its own state machine: `protection/configuration`,
  -- `protection/runs`. They are read together, on the page of the area.
  let top (name : String) : String := (name.splitOn "/").headD name
  let mut pages := #[]
  let mut ledgers := #[]
  let mut failed := []
  for e in areas.filter (wanted ·.name) do
    let r ← e.report since (args.contains "--full")
      ((areas.filter (·.name != e.name)).map (·.peer))
    let file := System.FilePath.mk s!"ledger/{e.name}.json"
    if let some dir := file.parent then IO.FS.createDirAll dir
    IO.FS.writeFile file (r.ledger.pretty ++ "\n")
    IO.println r.summary
    pages := pages.push r.readback
    ledgers := ledgers.push r.ledger
    failed := failed ++ r.failed.map (s!"{e.name}: " ++ ·)
  -- each area as it stands, with all its parts
  let ran := areas.filter (wanted ·.name)
  for a in (ran.map (top ·.name)).eraseDups do
    if areas.all fun e => top e.name != a || wanted e.name then
      page a "area" ((ran.zip pages.toList).filterMap fun (e, p) => if top e.name == a then some p else none).toArray
  if only.isNone then
    -- what the code offers against what the parts list, counted from the code
    let surface := areas.flatMap (·.peer.surface)
    let (said, ways) ← counted "ways_in" "ways in" "in no part" (surface.filter (!·.elsewhere) |>.flatMap (places ·.code))
    for l in said do IO.println l
    if !said.isEmpty then IO.FS.writeFile "ledger/ways-in.json" (ways.pretty ++ "\n")
    -- and what the code branches into behind them, against the variants the ways in name
    let (said, variants) ← counted "variants" "variants" "named on no way in"
      (surface.filter (!·.variants.isEmpty) |>.flatMap (places ·.branchesAt))
    for l in said do IO.println l
    if !said.isEmpty then IO.FS.writeFile "ledger/variants.json" (variants.pretty ++ "\n")
    page "index" "index" ledgers [("waysIn", ways), ("variants", variants)]  -- the list of areas
    -- the change, across every area it touches
    if since.isSome then page "change" "change" pages
    else try IO.FS.removeFile "out/readback/change.html" catch _ => pure ()  -- an earlier comparison's page would be stale
  for f in failed do IO.eprintln f
  IO.println <| (if only.isNone then "readback: out/readback/index.html" else s!"only {only.getD ""} was run")
    ++ (if since.isSome && only.isNone then ", the change: out/readback/change.html" else "")
  return (if failed.isEmpty then 0 else 1 : UInt32)

open Lean Elab Command in
elab "checker_main " id:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverloadWithInfo id
  let allowed := [``propext, ``Classical.choice, ``Quot.sound]
  let bad := (← collectAxioms name).filter (!allowed.contains ·)
  unless bad.isEmpty do
    throwError "not proven: the areas rest on {bad}; only {allowed} are accepted"
  let banned := ["implemented_by", "extern", "unsafe", "skipKernelTC"]
  for file in #[("Areas.lean" : System.FilePath)] ++ (← System.FilePath.walkDir "Areas") do
    if file.extension == some "lean" then
      let text ← IO.FS.readFile file
      for word in banned do
        if (text.splitOn word).length > 1 then
          throwError "not accepted: {file} uses `{word}`"
  elabCommand (← `(def $(mkIdent `main) (args : List String) : IO UInt32 := Spec.run $id args))

end Spec
