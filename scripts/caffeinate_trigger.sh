#!/bin/bash
# Illuminati — keep the Mac awake during market hours so the launchd trading jobs
# (scan, AEGIS, DUSK, EOD, options) actually run. Without this the Mac can idle-
# sleep mid-session and everything stops until it wakes.
#
# Launched by com.illuminati.caffeinate at 7:00 AM Mountain (RunAtLoad too), runs
# caffeinate for ~8h → awake from ~7 AM MT to ~3 PM MT (= 9 AM–5 PM ET), covering
# the whole trading day + the 4:15 PM ET EOD report. caffeinate exits on its own,
# so the Mac is free to sleep after hours. Weekdays only.
#
# NOTE: -s prevents system sleep only on AC power, and a closed laptop lid can
# still clamshell-sleep. Keep the Mac plugged in (and lid open, or set "prevent
# sleep when the display is off") for full coverage.

LOG="/tmp/illuminati-caffeinate.log"

DOW=$(date +%u)   # 1=Mon .. 7=Sun
if [ "$DOW" -gt 5 ]; then
  echo "[$(date)] weekend — not caffeinating" >> "$LOG"
  exit 0
fi

echo "[$(date)] caffeinate ON for ~8h (market hours)" >> "$LOG"
# -i no idle sleep · -s no system sleep (AC) · -m no disk sleep · -t 8h then exit
exec /usr/bin/caffeinate -i -s -m -t 28800
