#!/usr/bin/env bash
# Verify Java (>=17), Quint, and Apalache are available.
# Prints platform-aware install hints for whatever's missing, following the
# official Apalache JVM install guide: https://apalache-mc.org/docs/apalache/installation/jvm.html
#
# Usage:
#   tools/check-tooling.sh           # warn-only: prints status, exits 0
#   tools/check-tooling.sh --strict  # exit 1 if any tool is missing
#
# Used by:
#   tools/bootstrap.sh              (warn-only, end of bootstrap)
#   /spec-check                     (--strict, before any quint/apalache call)

set -u

strict=0
[ "${1:-}" = "--strict" ] && strict=1

missing=()
warn=()

# -- Java (Apalache prereq: 17+) --------------------------------------------

if command -v java >/dev/null 2>&1; then
  java_line="$(java -version 2>&1 | head -1)"
  # Parse the major version. Handles modern (`17.0.8`) and legacy (`1.8.0_392`).
  major="$(printf '%s\n' "$java_line" | sed -E 's/.*version "?([0-9]+)\..*/\1/')"
  if [ -n "$major" ] && [ "$major" -ge 17 ] 2>/dev/null; then
    echo "✓ java       ${java_line}"
  else
    echo "⚠ java       ${java_line}  (Apalache requires JVM 17+)"
    warn+=("java-too-old")
  fi
else
  echo "✗ java       not found  (Apalache requires JVM 17+)"
  missing+=("java")
fi

# -- Quint ------------------------------------------------------------------

if command -v quint >/dev/null 2>&1; then
  qver="$(quint --version 2>/dev/null | head -1)"
  echo "✓ quint      ${qver:-(version unknown)}"
else
  echo "✗ quint      not found"
  missing+=("quint")
fi

# -- Apalache ---------------------------------------------------------------
# `quint verify` shells out to Apalache and auto-fetches a JAR on first use,
# so apalache-mc on PATH is technically optional. /spec-check runs more
# smoothly (offline, no surprise download) with a real install.

if command -v apalache-mc >/dev/null 2>&1; then
  aver="$(apalache-mc version 2>/dev/null | head -1)"
  echo "✓ apalache   ${aver:-(version unknown)}"
else
  echo "✗ apalache   not found  (quint verify auto-fetches on first run; install for offline/repeatable use)"
  missing+=("apalache")
fi

if [ ${#missing[@]} -eq 0 ] && [ ${#warn[@]} -eq 0 ]; then
  exit 0
fi

echo
[ ${#missing[@]} -gt 0 ] && echo "Missing: ${missing[*]}"
[ ${#warn[@]} -gt 0 ]    && echo "Warnings: ${warn[*]}"
echo

# -- Platform detection -----------------------------------------------------

case "$(uname -s)" in
  Darwin) platform=macos ;;
  Linux)  platform=linux ;;
  CYGWIN*|MINGW*|MSYS*) platform=windows ;;
  *)      platform=other ;;
esac

# -- Install hints ----------------------------------------------------------

needs_java=0
case " ${missing[*]} ${warn[*]} " in
  *" java "*|*" java-too-old "*) needs_java=1 ;;
esac

if [ $needs_java -eq 1 ]; then
  echo "Install Java 17+ (Eclipse Temurin recommended; required by Apalache):"
  case "$platform" in
    macos)
      echo "  brew install --cask temurin"
      ;;
    linux)
      echo "  Ubuntu/Debian: sudo apt install -y openjdk-17-jdk"
      echo "  Fedora/RHEL:   sudo dnf install -y java-17-openjdk-devel"
      echo "  Arch:          sudo pacman -S jdk17-openjdk"
      echo "  Or download:   https://adoptium.net/temurin/releases/?version=17"
      ;;
    windows)
      echo "  winget install EclipseAdoptium.Temurin.17.JDK"
      echo "  Or download:   https://adoptium.net/temurin/releases/?version=17"
      ;;
    *)
      echo "  https://adoptium.net/temurin/releases/?version=17"
      ;;
  esac
  echo
fi

case " ${missing[*]} " in *" quint "*)
  echo "Install Quint:"
  echo "  npm install -g @informalsystems/quint"
  echo "  (Verify: quint --version. Other install paths at https://github.com/informalsystems/quint.)"
  echo
;; esac

case " ${missing[*]} " in *" apalache "*)
  echo "Install Apalache (JVM-based, requires Java 17+):"
  echo "  Official docs: https://apalache-mc.org/docs/apalache/installation/jvm.html"
  echo
  echo "  1. Download the latest release tarball from:"
  echo "       https://github.com/apalache-mc/apalache/releases"
  echo "     (asset name pattern: apalache-<version>.tgz — pick the latest)"
  echo
  case "$platform" in
    macos|linux)
      echo "  2. Recipe for macOS / Linux (adjust the URL to the current release):"
      echo "       mkdir -p \"\$HOME/.local/share\""
      echo "       curl -L -o /tmp/apalache.tgz \\"
      echo "         \"\$(curl -s https://api.github.com/repos/apalache-mc/apalache/releases/latest \\"
      echo "             | grep browser_download_url | grep '\\.tgz\"' | head -1 | cut -d'\"' -f4)\""
      echo "       tar -xzf /tmp/apalache.tgz -C \"\$HOME/.local/share\""
      echo "       ln -sfn \"\$HOME/.local/share/apalache\"-* \"\$HOME/.local/share/apalache\""
      echo
      echo "  3. Add bin/ to PATH (pick the right rc file: ~/.zshrc, ~/.bashrc, etc.):"
      echo "       echo 'export PATH=\"\$HOME/.local/share/apalache/bin:\$PATH\"' >> ~/.zshrc"
      echo "       source ~/.zshrc"
      echo
      echo "  4. Verify:"
      echo "       apalache-mc version"
      ;;
    windows)
      echo "  2. Extract the archive to a directory of your choice."
      echo "  3. Run via the batch script: <extracted-dir>\\bin\\apalache-mc.bat version"
      echo "     Or add <extracted-dir>\\bin to your PATH."
      ;;
    *)
      echo "  2. Extract the archive; run ./bin/apalache-mc on Unix or ./bin/apalache-mc.bat on Windows."
      echo "     See the official docs link above for platform notes."
      ;;
  esac
  echo
;; esac

if [ $strict -eq 1 ] && [ ${#missing[@]} -gt 0 ]; then
  exit 1
fi
exit 0
