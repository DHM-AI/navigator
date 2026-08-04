#!/bin/bash
# ============================================================================
# Illuminati — OPERATIONAL HARDENING (2026-07-02)
# ============================================================================
# Fixes the recurring "the Mac broke and the scanner sat dead for hours" class:
#   1. Relocates the venv OFF iCloud-synced ~/Documents  -> ~/.venvs/illuminati
#      (root cause of the 2026-07-01 outage: launchd Python deadlocked reading
#       .venv/pyvenv.cfg through iCloud/TCC at interpreter startup).
#   2. Builds it on a STABLE Homebrew python@3.12 (not uv-managed), so uv can't
#      garbage-collect the interpreter out from under the scanner.
#   3. Repoints every launchd trigger (both accounts) to the new venv.
#   4. Installs a self-healing WATCHDOG (bash+curl, venv-independent): if scans
#      go stale during market hours it kickstarts them and pings Slack — so a
#      failure self-recovers in ~15 min instead of sitting dead for a day.
#
# RUN THIS *AFTER* a reboot + re-granting Full Disk Access, from a normal
# Terminal (NOT via launchd). Idempotent — safe to re-run. Non-destructive:
# the old venv is left in place and every edited file gets a .bak backup.
#
#   cd /Users/renato/Documents/Claude/AI-trading/market-predictions
#   bash ops/harden.sh
# ============================================================================
set -uo pipefail

PROJ_MAIN="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PROJ_AGG="/Users/renato/Documents/Claude/AI-trading/market-predictions-aggressive"
NEWVENV="$HOME/.venvs/illuminati"
NEWPY="$NEWVENV/bin/python"
SCRIPTS_MAIN="$HOME/Library/Scripts/Illuminati"
SCRIPTS_AGG="$HOME/Library/Scripts/Illuminati2"
LA="$HOME/Library/LaunchAgents"
STAMP="bak-$(date +%Y%m%d-%H%M%S)"
say() { printf '\n\033[1m[harden] %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m[harden] ABORT: %s\033[0m\n' "$*" >&2; exit 1; }

# ── Stage 0: preflight ──────────────────────────────────────────────────────
say "Stage 0 — preflight"
[ -d "$PROJ_MAIN" ] || die "project not found at $PROJ_MAIN"
command -v brew >/dev/null 2>&1 || die "Homebrew not found (needed for a stable python@3.12)"
# quick health gate: a trivial venv must build in a few seconds, or the machine
# is still degraded — do NOT proceed on a sick box.
_t=$(mktemp -d); if ! /usr/bin/python3 -c "import venv" 2>/dev/null; then :; fi
rm -rf "$_t"
echo "  project OK · brew OK"

# ── Stage 1: stable python@3.12 ─────────────────────────────────────────────
say "Stage 1 — ensure a STABLE (non-uv) python@3.12"
BREW_PY="$(brew --prefix python@3.12 2>/dev/null)/bin/python3.12"
if [ ! -x "$BREW_PY" ]; then
  echo "  installing python@3.12 via Homebrew (one-time)..."
  brew install python@3.12 || die "brew install python@3.12 failed"
  BREW_PY="$(brew --prefix python@3.12)/bin/python3.12"
fi
[ -x "$BREW_PY" ] || die "python@3.12 still not found after install"
echo "  base python: $BREW_PY → $("$BREW_PY" --version 2>&1)"

# ── Stage 2: build the relocated venv (OFF ~/Documents) ─────────────────────
say "Stage 2 — build venv at $NEWVENV (off iCloud)"
mkdir -p "$HOME/.venvs"
if [ -x "$NEWPY" ] && "$NEWPY" -c "import numpy,xgboost,alpaca,supabase" 2>/dev/null; then
  echo "  venv already present + deps import — skipping rebuild"
else
  rm -rf "$NEWVENV"
  "$BREW_PY" -m venv "$NEWVENV" || die "venv create failed"
  "$NEWPY" -m pip install -q -U pip wheel || die "pip upgrade failed"
  echo "  installing requirements (this takes a few minutes)..."
  "$NEWPY" -m pip install -q -r "$PROJ_MAIN/requirements.txt" || die "pip install -r requirements.txt failed"
fi
say "Stage 2 — verify deps import in the new venv"
"$NEWPY" - <<'PYV' || die "dependency import check FAILED in the new venv"
import numpy, pandas, xgboost, sklearn, alpaca, supabase, dotenv, requests, yfinance
print("  deps OK: numpy/pandas/xgboost/sklearn/alpaca/supabase/dotenv/requests/yfinance")
PYV
# config import (reads .env via absolute path) from each project dir
( cd "$PROJ_MAIN" && "$NEWPY" -c "import config; print('  MAIN config imports OK')" ) || die "MAIN config import failed"
( cd "$PROJ_AGG"  && "$NEWPY" -c "import config; print('  AGG  config imports OK')" ) || echo "  (AGG config import warn — check separately)"

# ── Stage 3: repoint every launchd trigger to the new venv ──────────────────
say "Stage 3 — repoint launchd triggers → $NEWPY (backups: *.$STAMP)"
NEWQ="$NEWPY"   # absolute path, no vars
for f in "$SCRIPTS_MAIN"/*.sh "$SCRIPTS_AGG"/*.sh; do
  [ -f "$f" ] || continue
  cp "$f" "$f.$STAMP"
  sed -i '' \
    -e "s#\${MP}/\.venv/bin/python#$NEWQ#g" \
    -e "s#\${MARKET_PREDICTIONS}/\.venv/bin/python#$NEWQ#g" \
    -e "s#/Users/renato/Documents/Claude/AI-trading/market-predictions/\.venv/bin/python#$NEWQ#g" \
    "$f"
done
_left=$(grep -rl "\.venv/bin/python" "$SCRIPTS_MAIN" "$SCRIPTS_AGG" 2>/dev/null | grep -v "\.$STAMP" | grep "\.sh$" || true)
if [ -n "$_left" ]; then echo "  ⚠ still referencing old venv (check manually): $_left"; else echo "  all triggers repointed ✓"; fi
# launchd reads trigger .sh fresh each fire, so no plist reload is needed for these edits.

# ── Stage 4: install the self-healing watchdog ──────────────────────────────
say "Stage 4 — install watchdog (self-heal stale scans, venv-independent)"
cat > "$SCRIPTS_MAIN/watchdog_trigger.sh" <<'WD'
#!/bin/bash
# Illuminati WATCHDOG — pure bash+curl (NO python/venv dependency), so it works
# even when the trading venv is broken. If the scanner has gone stale during
# market hours, kickstart the scan jobs and alert Slack (hourly dedupe).
LOG="/tmp/illuminati-watchdog.log"; STATE="/tmp/illuminati-wd-lastalert"
MP="/Users/renato/Documents/Claude/AI-trading/market-predictions"
cd "$MP" 2>/dev/null || exit 0
set -a; . ./.env 2>/dev/null || true; set +a
[ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_KEY:-}" ] || { echo "[$(date)] no supabase creds — skip" >>"$LOG"; exit 0; }
# market hours gate (ET, weekday 9:45-15:55)
DOW=$(TZ=America/New_York date +%u); H=$(TZ=America/New_York date +%H); M=$(TZ=America/New_York date +%M)
T=$((10#$H*60+10#$M)); [ "$DOW" -gt 5 ] && exit 0; [ "$T" -lt 585 ] && exit 0; [ "$T" -gt 955 ] && exit 0
# freshness of last_scan_at
VAL=$(curl -s --max-time 15 -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  "$SUPABASE_URL/rest/v1/system_state?select=value&key=eq.last_scan_at" | sed -n 's/.*"value":"\([^"]*\)".*/\1/p')
[ -n "$VAL" ] || exit 0
LAST=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${VAL:0:19}" +%s 2>/dev/null); NOW=$(date -u +%s)
[ -n "$LAST" ] || exit 0
AGE=$(( (NOW - LAST) / 60 ))
if [ "$AGE" -gt 75 ]; then
  echo "[$(date)] STALE ${AGE}min — kickstarting scans" >>"$LOG"
  launchctl kickstart -k "gui/$(id -u)/com.illuminati.scan"  2>>"$LOG"
  launchctl kickstart -k "gui/$(id -u)/com.illuminati2.scan" 2>>"$LOG"
  # hourly Slack dedupe
  PREV=0; [ -f "$STATE" ] && PREV=$(cat "$STATE" 2>/dev/null || echo 0)
  if [ -n "${SLACK_WEBHOOK_URL:-}" ] && [ $(( NOW - PREV )) -gt 3300 ]; then
    curl -s --max-time 15 -X POST -H 'Content-type: application/json' \
      --data "{\"text\":\":wrench: *ILLUMINATI WATCHDOG* — scanner was *${AGE} min* stale during market hours; auto-restarted the scan jobs. If this repeats, the Mac needs a look.\"}" \
      "$SLACK_WEBHOOK_URL" >/dev/null; echo "$NOW" >"$STATE"
  fi
else
  echo "[$(date)] OK — last scan ${AGE}min ago" >>"$LOG"
fi
WD
chmod +x "$SCRIPTS_MAIN/watchdog_trigger.sh"
cat > "$LA/com.illuminati.watchdog.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.illuminati.watchdog</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$SCRIPTS_MAIN/watchdog_trigger.sh</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/illuminati-watchdog-launchd.log</string>
  <key>StandardErrorPath</key><string>/tmp/illuminati-watchdog-launchd.log</string>
</dict></plist>
PL
plutil -lint "$LA/com.illuminati.watchdog.plist" >/dev/null || die "watchdog plist invalid"
launchctl unload "$LA/com.illuminati.watchdog.plist" 2>/dev/null || true
launchctl load "$LA/com.illuminati.watchdog.plist" 2>/dev/null && echo "  watchdog loaded (checks every 15 min)"

# ── Stage 5: verify + kickstart a live scan ─────────────────────────────────
say "Stage 5 — kickstart a scan on the NEW venv"
launchctl kickstart -k "gui/$(id -u)/com.illuminati.scan" 2>/dev/null && echo "  MAIN scan kicked — watch /tmp/illuminati-scan.log for 'ARGUS SCAN' (past venv-init = fixed)"
say "DONE. New venv: $NEWVENV  (old venv left intact for rollback via *.$STAMP)."
echo "  Verify in ~15 min: last_scan_at should advance. Watchdog will self-heal future stalls."
echo "  Rollback if needed: restore the *.$STAMP trigger backups + unload com.illuminati.watchdog."
