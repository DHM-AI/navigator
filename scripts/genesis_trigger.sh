#!/bin/bash
# Illuminati — Mac launchd MONTHLY RETRAIN trigger (GENESIS)
# Retrains the XGBoost model on fresh 5y data the FIRST Monday of each month.
#
# WHY (2026-06-10): the model is now the system's whole edge (the conviction gate
# trades its top picks). Its retrain used to run on the GitHub monthly_retrain
# cron — the same unreliable GitHub scheduler that fired ZERO scans on 2026-06-05
# and DID NOT fire the retrain on 2026-06-01 (had to run it by hand). A stale
# model silently degrades the conviction picks. This puts the retrain on the same
# reliable, DST-safe local launchd path as scan/AEGIS/DUSK/EOD.
#
# Launched by com.illuminati.genesis.plist every Monday at 06:00 Mountain
# (= 8 AM ET, pre-market). The first-Monday guard below makes it MONTHLY:
# a Monday with day-of-month <= 7 is the first Monday; any later Monday skips.

LOG="/tmp/illuminati-genesis.log"
MP="/Users/renato/Documents/Claude/AI-trading/market-predictions"
PY="${MP}/.venv/bin/python"

echo "[$(date)] GENESIS trigger fired" >> "$LOG"

# First Monday of the month only. date +%u: Mon=1. 10# forces base-10.
DOW=$(date +%u)
DOM=$(date +%d)
if [ "$DOW" -ne 1 ]; then
  echo "[$(date)] not Monday (dow=$DOW) — skip" >> "$LOG"; exit 0
fi
if [ "$((10#$DOM))" -gt 7 ]; then
  echo "[$(date)] Monday but not the FIRST of the month (dom=$DOM) — skip" >> "$LOG"; exit 0
fi

cd "$MP" || { echo "[$(date)] cannot cd" >> "$LOG"; exit 1; }
[ -f .env ] || { echo "[$(date)] .env missing" >> "$LOG"; exit 1; }
set -a && . .env && set +a

echo "[$(date)] First Monday — retraining model on fresh 5y data..." >> "$LOG"
"$PY" -m model.trainer --refetch >> "$LOG" 2>&1
RC=$?
echo "[$(date)] GENESIS retrain finished (exit $RC)" >> "$LOG"

# Slack notify (best-effort) — model artifacts updated in the working tree the
# launchd scans read. NOT auto-committed (avoid messy launchd commits); run a
# manual commit if you want the repo synced.
if [ -n "$SLACK_WEBHOOK_URL" ]; then
  if [ "$RC" -eq 0 ]; then
    MSG="🧬 GENESIS — monthly model retrain complete (fresh 5y data). New xgb_model.pkl live for the conviction gate."
  else
    MSG="🚨 GENESIS — monthly retrain FAILED (exit $RC). Model is STALE — check /tmp/illuminati-genesis.log and rerun: python -m model.trainer --refetch"
  fi
  curl -s -X POST -H 'Content-Type: application/json' \
    -d "{\"text\":\"${MSG}\"}" "$SLACK_WEBHOOK_URL" >> "$LOG" 2>&1
fi
