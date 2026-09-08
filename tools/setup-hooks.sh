#!/usr/bin/env bash
# Install spec-lint as a git pre-commit hook.
# Run once after cloning or initializing the repo: bash tools/setup-hooks.sh

set -euo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")

if [ -z "$ROOT" ]; then
  echo "ERROR: not inside a git repository. Run 'git init' first."
  exit 1
fi

HOOKS_DIR="$ROOT/.git/hooks"
HOOK="$HOOKS_DIR/pre-commit"
SOURCE="$ROOT/tools/pre-commit"

if [ ! -f "$SOURCE" ]; then
  echo "ERROR: tools/pre-commit not found at $SOURCE"
  exit 1
fi

if [ -f "$HOOK" ] && ! cmp -s "$SOURCE" "$HOOK"; then
  echo "WARNING: existing pre-commit hook found at $HOOK"
  echo "Backing up to $HOOK.bak"
  mv "$HOOK" "$HOOK.bak"
fi

# Copy, don't symlink: `ln -sf` on Windows Git Bash silently copies anyway
# (or fails without Developer Mode), so a copy is the one behavior that's
# identical everywhere. Re-run this script after updating tools/pre-commit.
cp "$SOURCE" "$HOOK"
chmod +x "$SOURCE" "$HOOK"

echo "✓ spec-lint pre-commit hook installed (copied — re-run this script after updating tools/pre-commit)."
echo "  Runs automatically on: git commit (when specs/ or .spec/ files are staged)"
echo "  Blocks commit on: FAIL findings"
echo "  Warns but allows: WARN findings"
echo ""
echo "To run manually:"
echo "  python tools/spec-lint.py            # all areas"
echo "  python tools/spec-lint.py <area>     # one area"
