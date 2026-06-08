#!/bin/bash
# Illuminati — Mac launchd OPTIONS trigger (PAPER-ONLY).
# Runs the isolated options runner (options/run.py) LOCALLY every ~30 min during
# market hours. Mirrors scan_trigger.sh exactly in structure (weekday gate +
# ET market-hours gate with 10# base-10), but drives the OPTIONS subsystem.
#
# HARD RULE: options NEVER trade live. options/run.py relies on the execution
# layer's paper-only gates (OPT_ENABLE_PAPER / OPT_ENABLE_LIVE). This script does
# NOT change that posture — it only schedules the paper runner.
#
# DO NOT load the plist until after a manual test (the operator loads it).

LOG="/tmp/illuminati-options.log"
MARKET_PREDICTIONS="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PY="${MARKET_PREDICTIONS}/.venv/bin/python"

echo "[$(date)] Options trigger fired" >> "$LOG"

# Weekdays only (1=Mon .. 7=Sun)
DOW=$(date +%u)
if [ "$DOW" -gt "5" ]; then
  echo "[$(date)] Weekend — skipping" >> "$LOG"; exit 0
fi

# Market-hours gate (ET wall clock, DST-aware via macOS TZ).
# 9:30 AM – 3:30 PM ET. 3:30 = NO_ENTRY_AFTER_ET (config.py) — the last moment a
# new entry is allowed; the runner must cover the whole entry window. Exits
# (manage_options) also run inside this window, which is when they matter.
# 10# forces base-10 (avoids the 08/09 octal-parse error).
HOUR_ET=$(TZ="America/New_York" date +%H)
MIN_ET=$(TZ="America/New_York" date +%M)
TIME_ET=$((10#$HOUR_ET * 60 + 10#$MIN_ET))
OPEN_ET=570    # 9:30 AM
CLOSE_ET=930   # 3:30 PM = NO_ENTRY_AFTER_ET
if [ "$TIME_ET" -lt "$OPEN_ET" ] || [ "$TIME_ET" -gt "$CLOSE_ET" ]; then
  echo "[$(date)] Outside options window (ET ${HOUR_ET}:${MIN_ET}) — skipping" >> "$LOG"; exit 0
fi

cd "$MARKET_PREDICTIONS" || { echo "[$(date)] cannot cd to $MARKET_PREDICTIONS" >> "$LOG"; exit 1; }
if [ ! -f .env ]; then
  echo "[$(date)] ERROR: .env missing — cannot run options" >> "$LOG"; exit 1
fi

# Don't stack runs — if the options runner is already running, skip this tick.
if pgrep -f "options.run" > /dev/null; then
  echo "[$(date)] An options run is already running — skipping this tick" >> "$LOG"; exit 0
fi

echo "[$(date)] Running options locally (ET ${HOUR_ET}:${MIN_ET})..." >> "$LOG"
set -a && . .env && set +a
"$PY" -m options.run >> "$LOG" 2>&1
echo "[$(date)] Options run finished (exit $?)" >> "$LOG"
