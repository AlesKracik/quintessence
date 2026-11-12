import Lean
import Areas

/-!
The gate on what counts as proven. The checker program cannot be built unless
this file compiles, and it compiles only if

* every proof attached to an area rests on nothing but Lean's three standard
  axioms (so no `sorry`, no `native_decide`, no added axiom), and
* no file under `Areas/` uses a construct that lets compiled code differ from
  the definitions the proofs are about, or that skips the kernel.

Lake re-runs it whenever the compiled content of any area changes.
-/

open Lean Elab Command in
elab "#audit " id:ident : command => do
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

#audit Areas.all
