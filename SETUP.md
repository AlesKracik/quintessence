# Setting up a project

Instructions for the AI agent. A person asked you to set up a Quintessence project, or to move one to a newer framework version. Do the steps yourself and talk to the person in plain words. Ask only what is marked **Ask**; do not make them edit files or run commands. Without their yes, do not install anything, clone any repository other than the framework, commit, or push.

If you are reading this from a temporary checkout of the framework, keep it until you are done; `README.md` next to this file explains what the framework does, and `example/` is a complete project to compare with.

## A new project

1. **Ask** where the project should live: the current directory, a new repository, or a directory inside an existing repository (for instance next to the code or the documents). Everything below happens in that directory.

2. **Ask** what you need to know, in one go:
   - a short name for the project;
   - which repositories hold the code, and where the person has them checked out;
   - what is written about the system: architecture or design documents, READMEs, runbooks, product notes. Nothing is required, and no structure is expected; take what there is, or nothing;
   - which framework revision to pin. Offer the commit you are reading these instructions from (`git rev-parse HEAD` in the framework checkout), so the project stays on a known version.

3. Check the tools: `lake --version` and `git --version`; `python3 --version` when any area will be bound to code; `kotlinc -version` and `java -version` when that code is Kotlin. If Lean is missing, **ask** before installing it; the installer is elan (https://github.com/leanprover/elan).

4. Create four files and one directory. In `lakefile.toml`, `git` is the address the person gave you for the framework.

`lean-toolchain`, the same as the framework's:

```
leanprover/lean4:v4.22.0
```

`lakefile.toml`. `rev` is a branch, tag or commit of the framework; a commit or tag keeps the project on a known version:

```toml
name = "myproject"
defaultTargets = ["check"]

[[require]]
name = "quintessence"
git = "https://github.com/AlesKracik/quintessence"
rev = "framework-redesign"

[[lean_lib]]
name = "Areas"
globs = ["Areas", "Areas.+"]

[[lean_exe]]
name = "check"
root = "Main"
```

`Main.lean`, exactly these lines, in every project:

```lean
import Spec.Main
import Areas
checker_main Areas.all
```

`Areas.lean`, the list of areas, empty for now:

```lean
import Spec.Check

/-- Every area the checker runs. -/
def Areas.all : List Spec.Entry := []
```

   Also create the directory `Areas/` with an empty file `Areas/.gitkeep` in it: the build needs the directory to exist, and git does not keep an empty one.

5. Fetch the framework and take its commands:

   ```
   lake update
   mkdir -p .claude && cp -r .lake/packages/quintessence/.claude/commands .claude/
   ```

   `lake update` puts the framework under `.lake/packages/quintessence/` and writes `lake-manifest.json`, which pins the exact version and is committed. The agent reads `.claude/commands/` from the directory it is started in: when the project is a subdirectory of a repository, tell the person to start the agent there, or copy the commands to the repository root.

6. Add to `.gitignore`: `.lake/`, `out/`, `.spec/local.json`.

7. Write down where things are, from the answers in step 2.

`.spec/project.json` (committed): every repository the code lives in, and whatever is written about the system: architecture and design documents, READMEs, runbooks, product notes. Nothing here is required, and no structure is expected of the documents; list what exists, as files, directories, or `<repo>:<path>`.

```json
{ "documents": ["docs/", "platform-docs:services/billing.md", "billing:README.md"],
  "repos": { "billing": "git@example.com:org/billing.git",
             "platform-docs": "git@example.com:org/platform-docs.git" } }
```

`.spec/local.json` (not committed): where you have those repositories checked out.

```json
{ "repo_paths": { "billing": "/home/me/src/billing", "platform-docs": "/home/me/src/platform-docs" } }
```

   When there are no other repositories and no documents, the files are `{ "documents": [], "repos": {} }` and `{ "repo_paths": {} }`.

8. Run `lake exe check`. With no areas yet it builds the checker (a minute or two the first time) and prints `readback: out/readback/index.html`. If it does not, fix that before going on.

9. **Ask** whether the project uses GitHub Actions. If so, copy `.github/workflows/check.yml` from the framework and replace `example` by the project's directory, or remove those settings when the project is the repository root.

10. **Ask** before committing. In a new repository, propose one commit on the default branch (`main`): the setup changes no requirement, and later changes branch off it. In an existing repository, propose a branch `change/setup`.

11. Tell the person what now exists, in three or four sentences, and that the next step is `/spec`. If the commands are not offered, the agent has to be restarted in the project directory.

## Where the code is

A binding names the code it checks:

- in another repository: `repo := "billing"`, with paths relative to that repository's root;
- in the project directory: `repo` is left out, and paths are relative to the project directory (as in `example/app/`);
- in the same repository but outside the project directory: paths start with `../`.

## A newer framework version

Change `rev` in `lakefile.toml`, run `lake update quintessence`, copy the commands again (step 5; they are the framework's, so overwrite), run `lake exe check --full`, and show the person what changed in the ledger before committing it together with `lake-manifest.json`.
