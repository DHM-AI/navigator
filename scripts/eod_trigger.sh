#!/bin/bash
# Illuminati — Mac launchd EOD-REPORT trigger (CHRONICLE)
# Sends the end-of-day closed-P&L report via eod_report.py at ~4:15 PM ET.
# Added 2026-06-08 (audit Findings #2/#12): the EOD report previously ran ONLY on
# the GitHub Actions eod.yml 4:15 cron — the same unreliable cron that fired zero
# scans on 2026-06-05, and which drifts 1h in winter (EST). DUSK (close_intraday)
# is already on launchd via dusk_trigger.sh; this gives the report the same
# reliable, DST-safe local path so the daily P&L summary actually arrives.
#
# Launched by com.illuminati.eod.plist at 14:15 Mountain (= 4:15 PM ET; MT is a
# constant 2h behind ET year-round, so this is DST-safe). The ET window gate
# below is the real guard — a coalesced/late wake outside the window is skipped.

LOG="/tmp/illuminati-eod.log"
MP="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PY="${MP}/.venv/bin/python"

echo "[$(date)] EOD trigger fired" >> "$LOG"

# Weekdays only
DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then echo "[$(date)] weekend — skip" >> "$LOG"; exit 0; fi

# Only send between 4:10 PM and 5:00 PM ET (after the 4:00 close). A late/coalesced
# wake outside this window is skipped so we never fire a stale report at, say, 9 AM.
# 10# forces base-10 (no octal parse error on 08/09).
H=$(TZ="America/New_York" date +%H); M=$(TZ="America/New_York" date +%M)
T=$((10#$H*60+10#$M))
if [ "$T" -lt 970 ] || [ "$T" -gt 1020 ]; then
  echo "[$(date)] outside EOD window (ET ${H}:${M}) — skip" >> "$LOG"; exit 0
fi

cd "$MP" || { echo "[$(date)] cannot cd" >> "$LOG"; exit 1; }
[ -f .env ] || { echo "[$(date)] .env missing" >> "$LOG"; exit 1; }
set -a && . .env && set +a
echo "[$(date)] Running EOD report (ET ${H}:${M})..." >> "$LOG"
"$PY" eod_report.py >> "$LOG" 2>&1
echo "[$(date)] EOD report finished (exit $?)" >> "$LOG"
