#!/bin/bash
# Illuminati — Mac launchd AEGIS trigger
# Runs trail_stops.py locally every 20 min during market hours.
# Backup layer — fires independently of GitHub Actions.
# If the Mac is on, AEGIS runs. No GitHub dependency needed.
# (Committed to repo 2026-06-08 — audit Finding #3; previously lived only in
#  ~/Library/Scripts/Illuminati/ and was untracked.)

LOG="/tmp/illuminati-aegis.log"
MARKET_PREDICTIONS="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PYTHON="${MARKET_PREDICTIONS}/.venv/bin/python"

echo "[$(date)] AEGIS trigger fired" >> "$LOG"

# Only run Mon-Fri
DOW=$(date +%u)  # 1=Mon, 7=Sun
if [ "$DOW" -gt "5" ]; then
  echo "[$(date)] Weekend — skipping" >> "$LOG"
  exit 0
fi

# DST-aware market hours check using America/New_York wall clock.
# 10# forces base-10 so a leading-zero hour/min (08, 09) is NOT parsed as octal.
# (Audit Finding #3, 2026-06-08: the bare $((HOUR_ET*60+MIN_ET)) threw
#  "value too great for base" at :08/:09, blanked TIME_ET, and the gate
#  fail-OPENED so AEGIS ran outside its window during the whole 9 AM hour.)
HOUR_ET=$(TZ="America/New_York" date +%H)
MIN_ET=$(TZ="America/New_York" date +%M)
TIME_ET=$((10#$HOUR_ET * 60 + 10#$MIN_ET))
MARKET_OPEN_ET=570    # 9:30 AM ET = 570 min
MARKET_CLOSE_ET=1215  # 4:15 PM ET = 1215 min (15 min buffer after close)

if [ "$TIME_ET" -lt "$MARKET_OPEN_ET" ] || [ "$TIME_ET" -gt "$MARKET_CLOSE_ET" ]; then
  echo "[$(date)] Outside market hours (ET ${HOUR_ET}:${MIN_ET}) — skipping" >> "$LOG"
  exit 0
fi

# Run AEGIS locally
cd "$MARKET_PREDICTIONS" || { echo "[$(date)] Cannot cd to $MARKET_PREDICTIONS" >> "$LOG"; exit 1; }

# Verify .env exists before sourcing — missing .env = no credentials = silent failure
if [ ! -f .env ]; then
  echo "[$(date)] ERROR: .env missing in $MARKET_PREDICTIONS — cannot run AEGIS" >> "$LOG"
  exit 1
fi

echo "[$(date)] Running AEGIS locally (ET ${HOUR_ET}:${MIN_ET})..." >> "$LOG"
set -a && . .env && set +a
"$PYTHON" trail_stops.py >> "$LOG" 2>&1
EC=$?

echo "[$(date)] AEGIS finished (exit $EC)" >> "$LOG"
