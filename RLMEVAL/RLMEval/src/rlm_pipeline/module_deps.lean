import Lean
open Lean

def main (args : List String) : IO Unit := do
  -- Use this pinned executable's sysroot, never another `lean` on host PATH.
  initSearchPath (← getBuildDir)
  let imports := args.toArray.map fun name => ({ module := name.toName } : Import)
  let (_, state) ← (importModulesCore imports).run
  for name in state.moduleNames do
    IO.println s!"{name}\t{← findOLean name}"
