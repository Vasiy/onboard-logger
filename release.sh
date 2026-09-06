#!/usr/bin/env bash
# Build the archive the board's "update" button accepts.
# Run from the repo root on the dev host:  ./release.sh  [--no-tests]
#
# The counterpart of deploy.sh for a board you cannot reach over the network:
# the phone downloads this file, Config -> System takes it, and the board checks
# it, tests it and swaps it in. Same content as a deploy, different delivery.
#
# Only tracked files go in (plus the generated VERSION), so nothing local ever
# rides along: no .deploy.env, no logs, no firmware images.
set -euo pipefail

cd "$(dirname "$0")"

RUN_TESTS=1
PY="${PY:-.venv/bin/python}"
OUT_DIR="${OUT_DIR:-dist}"

for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

# The board runs the suite again before it swaps anything in, but a broken
# archive should never leave the dev host in the first place.
if [ "$RUN_TESTS" -eq 1 ]; then
  log "offline test suite (skip with --no-tests)"
  [ -x "$PY" ] || die "no interpreter at $PY — make a venv, or pass PY=/path/to/python"
  for f in tests/*.py; do
    "$PY" "$f" >/dev/null || die "$f failed — it would fail on the board too"
  done
  "$PY" -c "import app.main" >/dev/null || die "app.main does not import"
  if command -v node >/dev/null 2>&1; then
    node -e "new Function(require('fs').readFileSync('app/static/app.js','utf8'))" \
      || die "app.js has a syntax error"
  fi
fi

REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""
git diff --quiet 2>/dev/null || DIRTY="-dirty"
[ -z "$DIRTY" ] || printf '\033[1;33m!\033[0m working tree is dirty — the archive holds HEAD, not your edits\n'

VER="$(cat VERSION 2>/dev/null || echo 0.0.0)"
printf '%s %s %s %s\n' \
  "$VER" \
  "$(git describe --always 2>/dev/null || echo unknown)" \
  "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo -)" \
  "$(date '+%Y-%m-%d %H:%M')" > BUILD

mkdir -p "$OUT_DIR"
NAME="onboard-logger-$VER-$REV.tar.gz"
TMP="$OUT_DIR/.build.tar"

log "archive HEAD -> $OUT_DIR/$NAME"
git archive --format=tar HEAD -o "$TMP"
# BUILD is generated per build and therefore not tracked, so it is appended here
tar -rf "$TMP" BUILD
gzip -cn "$TMP" > "$OUT_DIR/$NAME"
rm -f "$TMP"

log "$(cat BUILD)"
log "done: $OUT_DIR/$NAME ($(wc -c < "$OUT_DIR/$NAME" | tr -d ' ') bytes)"
log "upload it in Config → System on the board"
