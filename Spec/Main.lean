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
-/

namespace Spec

def run (areas : List Entry) (args : List String) : IO UInt32 := do
  let since := match args.dropWhile (· != "--since") with
    | _ :: ref :: _ => some ref
    | _ => none
  IO.FS.createDirAll "ledger"
  IO.FS.createDirAll "out/readback"
  let template ← IO.FS.readFile s!"{← platformDir}/Spec/readback.html"
  -- one page of the readback: what kind it is, and the areas it shows
  let page (file kind : String) (data : Array Lean.Json) : IO Unit :=
    IO.FS.writeFile s!"out/readback/{file}.html" <|
      (template.replace "/*DATA*/[]" ((Lean.Json.arr data).compress.replace "</" "<\\/")).replace "/*PAGE*/{}"
        (Lean.Json.mkObj [("kind", .str kind), ("since", (since.map Lean.Json.str).getD .null)]).compress
  let mut pages := #[]
  let mut ledgers := #[]
  let mut failed := []
  for e in areas do
    let r ← e.report since (args.contains "--full")
    IO.FS.writeFile s!"ledger/{e.name}.json" (r.ledger.pretty ++ "\n")
    IO.println r.summary
    pages := pages.push r.readback
    ledgers := ledgers.push r.ledger
    page e.name "area" #[r.readback]  -- the area as it stands
    failed := failed ++ r.failed.map (s!"{e.name}: " ++ ·)
  page "index" "index" ledgers  -- the list of areas
  -- the change, across every area it touches
  if since.isSome then page "change" "change" pages
  for f in failed do IO.eprintln f
  IO.println <| "readback: out/readback/index.html"
    ++ (if since.isSome then ", the change: out/readback/change.html" else "")
  return if failed.isEmpty then 0 else 1

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
