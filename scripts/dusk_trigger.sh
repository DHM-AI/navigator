#!/bin/bash
# Illuminati — Mac launchd DUSK trigger
# Closes all day-trade positions at ~3:50 PM ET via close_intraday.py.
# Immune to GitHub Actions cron failure (which fired ZERO jobs on 2026-06-05).
# CRITICAL for day-trade mode: without a reliable EOD close, tight-stop positions
# get held overnight and exposed to gaps — the exact risk day-trading avoids.

LOG="/tmp/illuminati-dusk.log"
MP="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PY="${MP}/.venv/bin/python"

echo "[$(date)] DUSK trigger fired" >> "$LOG"

# Weekdays only
DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then echo "[$(date)] weekend — skip" >> "$LOG"; exit 0; fi

# Only close inside 3:45-3:59 PM ET (before the 4 PM close, so market orders fill
# same-day). A late/coalesced wake-run outside this window is skipped so we never
# fire close orders after hours (which would fill at the next open = gap risk).
H=$(TZ="America/New_York" date +%H); M=$(TZ="America/New_York" date +%M)
T=$((10#$H*60+10#$M))
if [ "$T" -lt 945 ] || [ "$T" -gt 959 ]; then
  echo "[$(date)] outside DUSK window (ET ${H}:${M}) — skip" >> "$LOG"; exit 0
fi

cd "$MP" || { echo "[$(date)] cannot cd" >> "$LOG"; exit 1; }
[ -f .env ] || { echo "[$(date)] .env missing" >> "$LOG"; exit 1; }
set -a && . .env && set +a
echo "[$(date)] Running DUSK close (ET ${H}:${M})..." >> "$LOG"
"$PY" close_intraday.py >> "$LOG" 2>&1
echo "[$(date)] DUSK finished (exit $?)" >> "$LOG"
