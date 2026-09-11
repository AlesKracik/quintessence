#!/usr/bin/env bash
# Verify Java (>=17), Quint, and Apalache are available.
# Prints platform-aware install hints for whatever's missing, following the
# official Apalache JVM install guide: https://apalache-mc.org/docs/apalache/installation/jvm.html
#
# Usage:
#   tools/check-tooling.sh           # warn-only: prints status, exits 0
#   tools/check-tooling.sh --strict  # exit 1 if anything is missing OR Java < 17
#
# Used by:
#   tools/bootstrap.sh              (warn-only, end of bootstrap)
#   Run it manually (or let /spec-check suggest it) when quint/apalache
#   calls fail — it prints platform-specific install hints.

set -u

strict=0
[ "${1:-}" = "--strict" ] && strict=1

missing=()
warn=()

# -- Java (Apalache prereq: 17+) --------------------------------------------

if command -v java >/dev/null 2>&1; then
  java_line="$(java -version 2>&1 | head -1)"
  # Parse the major version: first integer in the line. Handles modern
  # (`17.0.8`), legacy (`1.8.0_392` → 1, correctly < 17), and dotless GA
  # builds (`version "21"`). The old dot-requiring sed broke on the latter.
  major="$(printf '%s\n' "$java_line" | grep -oE '[0-9]+' | head -1)"
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
  # Optional-flag report, not a gate. spec-record probes for each of these
  # and degrades to the older command line when one is absent, so an older
  # quint still checks everything — it just pays for it.
  qrun_help="$(quint run --help 2>&1 || true)"
  qver_help="$(quint verify --help 2>&1 || true)"
  case "$qrun_help" in
    *--backend*) echo "  ✓ run --backend       (Rust evaluator available for run/test)" ;;
    *)           echo "  · run --backend       absent — simulator + quint test use the default evaluator" ;;
  esac
  case "$qrun_help" in
    *--witnesses*) echo "  ✓ run --witnesses     (cheap witness pre-screen before Apalache)" ;;
    *)             echo "  · run --witnesses     absent — witness reachability is only ever answered by the model checker" ;;
  esac
  case "$qver_help" in
    *--invariants*) echo "  ✓ verify --invariants (batched green path: one Apalache start, not N)" ;;
    *)              echo "  · verify --invariants absent — invariants are checked one model-check run each" ;;
  esac
  case "$qver_help" in
    *--temporal*) echo "  ✓ verify --temporal   (properties[] / liveness are checkable)" ;;
    *)            echo "  ✗ verify --temporal   absent — properties[] cannot be checked by this quint" ;;
  esac
else
  echo "✗ quint      not found"
  missing+=("quint")
fi

# TLC (the liveness backend behind `quint verify --temporal --backend=tlc`)
# needs no entry of its own: it is a Java tool quint fetches on first use, and
# the JVM it runs on is already checked above for Apalache.

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

# -- jsonschema (spec-lint's schema validation) ------------------------------
# Not optional in the way Alloy is: without it spec-lint SKIPS schema
# validation entirely, and several other checks defer malformed-shape
# detection to it. A project linting without the lib has real holes, and
# every run carries a WARN saying so — so report it here, where the fix
# is one line, instead of leaving it to nag on every lint.

py=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then py="$cand"; break; fi
done

if [ -z "$py" ]; then
  echo "✗ python     not found  (spec-lint and the rest of tools/ are Python 3)"
  missing+=("python")
elif "$py" -c "import jsonschema" >/dev/null 2>&1; then
  jver="$("$py" -c "import jsonschema; print(jsonschema.__version__)" 2>/dev/null)"
  echo "✓ jsonschema ${jver:-(version unknown)}"
else
  echo "⚠ jsonschema not installed  (spec-lint skips schema validation without it)"
  warn+=("jsonschema")
fi

# -- Alloy (OPTIONAL structural backend) ------------------------------------
# Never counted as missing: the Alloy backend is opt-in per invariant
# (proof: "structural") and most projects never turn it on. Reported only so
# a project that DID turn it on can see whether the jar resolves. Needs the
# same JVM 17+ as Apalache, plus Alloy 6.2+ — its CLI arrived in 6.2.0.

alloy_jar="${ALLOY_JAR:-}"
if [ -z "$alloy_jar" ] && [ -f .spec/project.json ] && command -v python3 >/dev/null 2>&1; then
  alloy_jar="$(python3 -c 'import json,sys
try:
    print((json.load(open(".spec/project.json")).get("alloy") or {}).get("jar_path") or "")
except Exception:
    print("")' 2>/dev/null)"
fi

if [ -n "$alloy_jar" ]; then
  if [ -f "$alloy_jar" ]; then
    echo "✓ alloy      ${alloy_jar}  (optional structural backend)"
  else
    echo "⚠ alloy      configured but not found at ${alloy_jar}"
    warn+=("alloy-jar-missing")
  fi
else
  echo "— alloy      not configured  (optional; only needed for proof: \"structural\" invariants)"
fi

if [ ${#missing[@]} -eq 0 ] && [ ${#warn[@]} -eq 0 ]; then
  exit 0
fi

# NB: all array expansions below use ${arr[*]:-} — bash ≤ 4.3 (macOS default
# 3.2) treats expanding an empty array under `set -u` as an unbound variable.
echo
[ ${#missing[@]} -gt 0 ] && echo "Missing: ${missing[*]:-}"
[ ${#warn[@]} -gt 0 ]    && echo "Warnings: ${warn[*]:-}"
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
case " ${missing[*]:-} ${warn[*]:-} " in
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

case " ${warn[*]:-} " in *" alloy-jar-missing "*)
  echo "Install Alloy (optional — structural backend, needs Alloy 6.2+ for its CLI):"
  echo "  Download org.alloytools.alloy.dist.jar from"
  echo "    https://github.com/AlloyTools/org.alloytools.alloy/releases"
  echo "  Then set ALLOY_JAR=/path/to/org.alloytools.alloy.dist.jar"
  echo "  (or alloy.jar_path in .spec/project.json). Verify: java -jar \"\$ALLOY_JAR\" help"
  echo
;; esac

case " ${warn[*]:-} " in *" jsonschema "*)
  echo "Install jsonschema (spec-lint needs it for schema validation):"
  echo "  ${py:-python3} -m pip install jsonschema"
  echo "  (Verify: ${py:-python3} -c 'import jsonschema')"
  echo
;; esac

case " ${missing[*]:-} " in *" quint "*)
  echo "Install Quint:"
  echo "  npm install -g @informalsystems/quint"
  echo "  (Verify: quint --version. Other install paths at https://github.com/informalsystems/quint.)"
  echo
;; esac

case " ${missing[*]:-} " in *" apalache "*)
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

# Strict mode fails on warnings too: an Apalache-incompatible JVM (java-too-old)
# is just as blocking as a missing tool.
if [ $strict -eq 1 ] && { [ ${#missing[@]} -gt 0 ] || [ ${#warn[@]} -gt 0 ]; }; then
  exit 1
fi
exit 0
