#!/bin/bash
# Illuminati — Mac launchd SCAN trigger
# Runs agent.py LOCALLY every ~30 min during market hours.
# Immune to GitHub Actions cron failures (which fired ZERO scans on 2026-06-05).
# If the Mac is on during market hours, scans run — no GitHub dependency, no PAT.
# Mirrors aegis_trigger.sh (which does the same for trailing stops).

LOG="/tmp/illuminati-scan.log"
MARKET_PREDICTIONS="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PYTHON="${MARKET_PREDICTIONS}/.venv/bin/python"

echo "[$(date)] Scan trigger fired" >> "$LOG"

# Weekdays only (1=Mon .. 7=Sun)
DOW=$(date +%u)
if [ "$DOW" -gt "5" ]; then
  echo "[$(date)] Weekend — skipping" >> "$LOG"; exit 0
fi

# Market-hours gate (ET wall clock, DST-aware via macOS TZ).
# 9:30 AM – 3:30 PM ET. 3:30 = NO_ENTRY_AFTER_ET (config.py) — the last moment a
# new entry is allowed; scanning must cover the whole entry window (#17 2026-06-08:
# was 3:00, leaving a 3:00–3:30 gap where entries were permitted but no scan ran).
# 10# forces base-10 (avoids the 08/09 octal-parse error).
HOUR_ET=$(TZ="America/New_York" date +%H)
MIN_ET=$(TZ="America/New_York" date +%M)
TIME_ET=$((10#$HOUR_ET * 60 + 10#$MIN_ET))
OPEN_ET=570    # 9:30 AM
CLOSE_ET=930   # 3:30 PM = NO_ENTRY_AFTER_ET
if [ "$TIME_ET" -lt "$OPEN_ET" ] || [ "$TIME_ET" -gt "$CLOSE_ET" ]; then
  echo "[$(date)] Outside scan window (ET ${HOUR_ET}:${MIN_ET}) — skipping" >> "$LOG"; exit 0
fi

cd "$MARKET_PREDICTIONS" || { echo "[$(date)] cannot cd to $MARKET_PREDICTIONS" >> "$LOG"; exit 1; }
if [ ! -f .env ]; then
  echo "[$(date)] ERROR: .env missing — cannot run scan" >> "$LOG"; exit 1
fi

# Don't stack scans — if agent.py is already running, skip this tick
if pgrep -f "agent.py" > /dev/null; then
  echo "[$(date)] A scan is already running — skipping this tick" >> "$LOG"; exit 0
fi

echo "[$(date)] Running scan locally (ET ${HOUR_ET}:${MIN_ET})..." >> "$LOG"
set -a && . .env && set +a
"$PYTHON" agent.py >> "$LOG" 2>&1
echo "[$(date)] Scan finished (exit $?)" >> "$LOG"
