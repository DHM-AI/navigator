#!/bin/bash
# Illuminati — Mac launchd FRIDAY-FLATTEN trigger
# Closes ALL equity positions every Friday ~3:00 PM ET (1h before the close) so
# nothing carries weekend gap risk (Renato's rule, 2026-06-12).
# Launched by com.illuminati.friday_flatten.plist at 13:00 Mountain (= 3:00 PM ET;
# MT is a constant 2h behind ET, DST-safe). The ET window gate below is the real
# guard — a coalesced/late wake outside the window is skipped, and friday_flatten.py
# itself re-checks Friday + window + market-open before closing anything.

LOG="/tmp/illuminati-friday-flatten.log"
MP="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PY="${MP}/.venv/bin/python"

echo "[$(date)] FRIDAY-FLATTEN trigger fired" >> "$LOG"

# Friday only (date +%u: Mon=1 … Fri=5)
DOW=$(date +%u)
if [ "$DOW" -ne 5 ]; then echo "[$(date)] not Friday (dow=$DOW) — skip" >> "$LOG"; exit 0; fi

# Only inside ~2:55-3:45 PM ET. Well before the 4 PM close so market orders fill
# same-day; never after hours. 10# forces base-10 (no octal parse on 08/09).
H=$(TZ="America/New_York" date +%H); M=$(TZ="America/New_York" date +%M)
T=$((10#$H*60+10#$M))
if [ "$T" -lt 895 ] || [ "$T" -gt 945 ]; then
  echo "[$(date)] outside flatten window (ET ${H}:${M}) — skip" >> "$LOG"; exit 0
fi

cd "$MP" || { echo "[$(date)] cannot cd" >> "$LOG"; exit 1; }
[ -f .env ] || { echo "[$(date)] .env missing" >> "$LOG"; exit 1; }
set -a && . .env && set +a
echo "[$(date)] Running Friday flatten (ET ${H}:${M})..." >> "$LOG"
"$PY" friday_flatten.py >> "$LOG" 2>&1
echo "[$(date)] Friday flatten finished (exit $?)" >> "$LOG"
