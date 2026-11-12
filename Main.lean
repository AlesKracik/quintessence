import Areas

/-!
The checker program: `lake exe spec`, run from the repository root. Runs every area, writes `ledger/<area>.json`
(committed) and `out/review.html` (the page a person reviews), prints one line
per area, and fails if any claim is violated, any example fails, or code and
model differ.

    --since <rev>   compare with that git revision: the review shows what changed
                    since, and code checks are re-run only where something did
    --full          re-run every code check
-/

def main (args : List String) : IO UInt32 := do
  let since := match args.dropWhile (· != "--since") with
    | _ :: ref :: _ => some ref
    | _ => none
  IO.FS.createDirAll "ledger"
  IO.FS.createDirAll "out"
  let mut reviews := #[]
  let mut failed := []
  for e in Areas.all do
    let r ← e.report since (args.contains "--full")
    IO.FS.writeFile s!"ledger/{e.name}.json" (r.ledger.pretty ++ "\n")
    IO.println r.summary
    reviews := reviews.push r.review
    failed := failed ++ r.failed.map (s!"{e.name}: " ++ ·)
  let data := (Lean.Json.arr reviews).compress.replace "</" "<\\/"
  IO.FS.writeFile "out/review.html" ((← IO.FS.readFile "Spec/review.html").replace "/*DATA*/[]" data)
  for f in failed do IO.eprintln f
  IO.println "review: out/review.html"
  return if failed.isEmpty then 0 else 1
