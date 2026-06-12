#!/usr/bin/env bash
# Bootstrap a new spec project from this template.
#
# Two modes:
#
#   (default) — fresh project. Strips template-only files (this template's
#               README, examples/), optionally re-initializes git history,
#               writes a project-stub README pointing at METHODOLOGY.md.
#
#   --in-place — retrofit mode for adding the methodology to an existing repo.
#                Leaves README, .git, and existing project files alone.
#                Only removes its own self-removal artifacts. Use this when you
#                want to add /spec to an existing codebase.
#
# Either mode self-removes tools/bootstrap.sh on success; tools/ itself stays
# (spec-lint, spec-record, spec-matrix, quint_ir, itf_tools, hooks installer live there).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# -- Args --------------------------------------------------------------------

IN_PLACE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --in-place|--retrofit)
      IN_PLACE=1
      ;;
    --help|-h)
      cat <<USAGE
Usage: $0 [--in-place]

Modes:
  (default)     Fresh project. Replaces README, strips examples/, optionally
                re-inits git. Use after cloning the template for a NEW project.
  --in-place    Retrofit mode. Keeps existing README, examples/, and .git. Use
                when adding the methodology to an EXISTING codebase.

Both modes self-remove tools/bootstrap.sh on success.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Run with --help for usage." >&2
      exit 1
      ;;
  esac
  shift
done

# -- Refuse if the project has already been scaffolded ----------------------

if [ -f ".spec/project.json" ]; then
  echo "This project already has .spec/project.json — it's already bootstrapped."
  echo "If you want to re-bootstrap, remove .spec/ first (you'll lose work)."
  exit 1
fi

if [ ! -f "METHODOLOGY.md" ] || [ ! -d ".claude/commands" ]; then
  echo "This doesn't look like a spec-template checkout (no METHODOLOGY.md or .claude/commands/)."
  echo "Run this script from the root of a fresh template clone."
  exit 1
fi

# -- In-place / retrofit mode -----------------------------------------------

if [ "$IN_PLACE" -eq 1 ]; then
  echo "In-place bootstrap: adding the methodology to this existing repo."
  echo
  echo "What this will do:"
  echo "  - leave your README.md, .git, and all existing project files untouched"
  echo "  - leave examples/ alone (delete manually if you don't want the reference)"
  echo "  - remove memory/ (Claude auto-memory; user-specific, never travels)"
  echo "  - self-remove tools/bootstrap.sh"
  echo
  read -r -p "Proceed? [y/N] " confirm
  if [[ ! "${confirm:-n}" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi

  rm -rf memory/

  # Optional tooling check (soft — never blocks).
  echo
  echo "Checking for Quint and Apalache..."
  if [ -x "$ROOT/tools/check-tooling.sh" ]; then
    "$ROOT/tools/check-tooling.sh" || true
  else
    echo "  (tools/check-tooling.sh missing or not executable — skipping)"
  fi

  rm -f "$0"
  echo "  removed tools/bootstrap.sh (self)"

  echo
  echo "In-place bootstrap complete."
  echo
  echo "Next: open Claude Code and run /spec"
  echo "  - /spec will detect this is an existing repo and walk project setup."
  echo "  - For each area with existing code, /spec <area> will offer brownfield"
  echo "    extraction (reads source, drafts a spec, marks everything"
  echo "    needs-validation for you to review)."
  exit 0
fi

# -- Fresh-project mode (default) -------------------------------------------

echo "Bootstrapping a new spec project from this template."
echo

read -r -p "Project name (slug; used in the README stub) [my-spec-project]: " project_name
project_name="${project_name:-my-spec-project}"

read -r -p "Re-initialize git history? Recommended for a fresh project. [y/N] " reinit_git
reinit_git="${reinit_git:-n}"

read -r -p "Remove examples/ now? They are reference material. [Y/n] " strip_examples
strip_examples="${strip_examples:-y}"

echo

# -- Strip template-only files ----------------------------------------------

echo "Removing template-only files..."

if [[ "$strip_examples" =~ ^[Yy]$ ]]; then
  rm -rf examples/
  echo "  removed examples/"
fi

# Claude auto-memory is user-/conversation-specific — never travels in templates.
rm -rf memory/

# -- Write a project-stub README --------------------------------------------

cat > README.md <<EOF
# ${project_name}

A project built with the formal-requirements methodology in [METHODOLOGY.md](METHODOLOGY.md).

## Getting Started

Open this directory in Claude Code and run:

\`\`\`
/spec
\`\`\`

The adaptive entry point walks project setup (areas, repos, architecture
defaults) and per-area authoring (elicit, vocabulary, structure, formalize)
conversationally. No fixed phase sequence to memorize.

## The Five Commands

- \`/spec [target]\` — adaptive spec authoring (elicit, vocab, structure, formalize, brownfield extract, drift codify, project edit)
- \`/spec-check [target]\` — run Apalache on the area's Quint sidecar; cascade to involving contracts
- \`/spec-verify [target]\` — run the area's test command; validate traceability; detect drift; append to verification_log
- \`/spec-apply [target]\` — generate code from the spec + architecture; per-component when declared
- \`/spec-readback [target]\` — generate a human-readable Markdown review document with embedded Mermaid diagrams

See [METHODOLOGY.md](METHODOLOGY.md) for the complete reference.

## Layout

- \`specs/<area>.json\` — one file per area (kind: area, contract, or ui)
- \`specs/<area>.qnt\` — the Quint formal model sidecar
- \`specs/<area>.readback.md\` — auto-generated review document
- \`.spec/project.json\` — project meta, areas index, code-repo paths, architecture defaults, topology

## Prerequisites

- [Claude Code](https://claude.com/claude-code)
- [Apalache](https://apalache-mc.org/) — for \`/spec-check\`. Requires Java 17+. Install: see [JVM guide](https://apalache-mc.org/docs/apalache/installation/jvm.html), or run \`tools/check-tooling.sh\` for platform-specific hints.
- Python 3 — for the \`tools/\` scripts (lint, matrix, ITF tooling)
- Git
EOF
echo "  wrote project README.md"

# -- Optional fresh git history ---------------------------------------------

if [[ "$reinit_git" =~ ^[Yy]$ ]]; then
  rm -rf .git
  git init -q
  echo "  re-initialized git (fresh history)"
fi

# -- Check external tooling (soft) ------------------------------------------

echo
echo "Checking for Quint and Apalache..."
if [ -x "$ROOT/tools/check-tooling.sh" ]; then
  "$ROOT/tools/check-tooling.sh" || true   # warn-only: never block bootstrap
else
  echo "  (tools/check-tooling.sh missing or not executable — skipping)"
fi

# -- Self-remove ------------------------------------------------------------

# Drop just this script; tools/ stays (it holds spec-lint, hooks, etc.).
rm -f "$0"
echo "  removed tools/bootstrap.sh (self)"

echo
echo "Bootstrap complete."
echo
echo "Next: open this directory in Claude Code and run /spec"
echo "  - /spec will ask for the project name, areas, and architecture defaults."
echo "  - If Quint/Apalache are missing, install them before /spec-check."
