#!/usr/bin/env bash
# Push the working tree to the board and prove it landed.
# Run from the repo root on the dev host:  ./deploy.sh  [--no-tests]
#
# The counterpart of install.sh, which provisions the board itself. This one only
# ships code, and it syncs the WHOLE repo rather than just app/: config/fw_layout.json,
# config/fw_catalog.json and the dtc/ tables live outside app/, and an app/-only
# deploy silently left the board running stale data more than once.
#
# Board-side data is out of reach by construction — ECU logs (/root/k-line), firmware
# images (/root/firmware) and the live config (/etc/onboard-logger) all sit outside
# DEST — while .venv and bin/5am_util are excluded, which also protects them from
# --delete.
#
# The board's address and password are NOT in this file: the repository is public.
# Put them in .deploy.env next to it (git-ignored, see .deploy.env.example), or pass
# BOARD_HOST / BOARD_USER / BOARD_PASS in the environment. With no password set the
# script uses ssh key auth.
set -euo pipefail

cd "$(dirname "$0")"
[ -f .deploy.env ] && . ./.deploy.env

HOST="${BOARD_HOST:-}"
USER_ON_BOARD="${BOARD_USER:-}"
PASS="${BOARD_PASS:-}"
DEST=/opt/onboard-logger
RUN_TESTS=1
PY="${PY:-.venv/bin/python}"     # override when the venv is not beside the checkout

for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$HOST" ] || die "no board address — set BOARD_HOST in .deploy.env (cp .deploy.env.example .deploy.env)"
[ -n "$USER_ON_BOARD" ] || die "no login — set BOARD_USER in .deploy.env"

if [ -n "$PASS" ] && command -v sshpass >/dev/null 2>&1; then
  RSH="sshpass -p $PASS ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8"
else
  RSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8"   # key auth
fi
BOARD="$USER_ON_BOARD@$HOST"
EXCLUDES=(--exclude '.git' --exclude '.venv' --exclude 'bin' --exclude '__pycache__'
          --exclude '*.pyc' --exclude '.claude' --exclude '.remember'
          --exclude 'old_logs' --exclude '.DS_Store' --exclude 'CONTEXT.md')

if [ "$RUN_TESTS" -eq 1 ]; then
  log "offline test suite (skip with --no-tests)"
  [ -x "$PY" ] || die "no interpreter at $PY — make a venv, or pass PY=/path/to/python"
  for f in tests/*.py; do
    "$PY" "$f" >/dev/null || die "$f failed — fix it before it reaches the bike"
  done
  "$PY" -c "import app.main" >/dev/null || die "app.main does not import"
  if command -v node >/dev/null 2>&1; then
    node -e "new Function(require('fs').readFileSync('app/static/app.js','utf8'))" \
      || die "app.js has a syntax error"
  fi
fi

# The board has no repository of its own, so the only way it can name the code it
# runs is a file we put there. VERSION is tracked and hand-bumped; BUILD adds the
# commit and the date and is generated, never committed (.gitignore) — a file the
# board wrote inside DEST would break the checksum verification below.
log "stamp BUILD"
printf '%s %s %s %s\n' \
  "$(cat VERSION 2>/dev/null || echo 0.0.0)" \
  "$(git describe --always --dirty 2>/dev/null || echo unknown)" \
  "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo -)" \
  "$(date '+%Y-%m-%d %H:%M')" > BUILD
cat BUILD

log "sync -> $BOARD:$DEST"
rsync -a --delete "${EXCLUDES[@]}" -e "$RSH" ./ "$BOARD:$DEST/" \
  || die "rsync failed — is the board reachable at $HOST?"

log "restart onboard-logger"
state=$($RSH "$BOARD" 'systemctl restart onboard-logger; sleep 3; systemctl is-active onboard-logger' || true)
[ "$state" = "active" ] || die "service is '$state' — check: journalctl -u onboard-logger -n 50"

# The whole point of this script: never report a deploy that was not verified.
# Checksums, not timestamps, and in both directions.
log "verify (checksums, both directions)"
out=$(rsync -acn "${EXCLUDES[@]}" --delete -i -e "$RSH" ./ "$BOARD:$DEST/")
[ -z "$out" ] || die "the board still differs from the working tree:
$out"
out=$(rsync -acn "${EXCLUDES[@]}" -i -e "$RSH" "$BOARD:$DEST/" ./)
[ -z "$out" ] || die "the board holds files the working tree does not:
$out"

# /etc/onboard-logger wins over the repo's config/ for the layered assets, and
# install.sh only ever seeds them ("[ -f ] || cp"). So a params.json edit that is
# rsynced here still does not reach the running board — that is how the gear ->
# neutral rename shipped without taking effect on 2026-09-05. Report the drift;
# do not overwrite, the board's copy may hold a hand edit.
log "layered config: /etc vs repo"
for f in params.json ecu_id.json actuators.json status_maps.json profiles.json; do
  [ -f "config/$f" ] || continue
  mine=$(md5 -q "config/$f" 2>/dev/null || md5sum "config/$f" | cut -d" " -f1)
  theirs=$($RSH "$BOARD" "test -f /etc/onboard-logger/$f && md5sum /etc/onboard-logger/$f | cut -d' ' -f1" || true)
  [ -n "$theirs" ] || continue          # not in /etc -> the repo copy is what runs
  [ "$mine" = "$theirs" ] || printf '    \033[1;33m!\033[0m %s differs from /etc/onboard-logger/%s — the board runs its own copy\n' "config/$f" "$f"
done

log "board data left alone:"
$RSH "$BOARD" 'du -sh /root/k-line /root/firmware 2>/dev/null; ls /opt/onboard-logger/bin'
log "done — http://$HOST/"
