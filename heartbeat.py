"""Illuminati — external HEARTBEAT.

Runs in the CLOUD (GitHub Actions) so it can detect when the local Mac — which
runs every launchd trading job (scan, AEGIS, DUSK, EOD, options) — is asleep or
off during market hours and the scanner has gone silent. Posts a Slack alert.

Read-only. NEVER trades. Self-contained — only needs `requests` + env vars
(SUPABASE_URL, a Supabase key, SLACK_WEBHOOK_URL), so it doesn't drag in the
engine's heavy deps. Signal = freshness of the newest `predictions.created_at`
(the scanner writes predictions every ~30 min).

Run: python heartbeat.py
"""
from __future__ import annotations

import os
import re
import requests
from datetime import datetime, timezone

STALE_MIN = 75   # scans run every 30 min; >75 min stale during market hours = trouble


def _parse_ts(s: str) -> datetime:
    """Tolerant ISO parse (Python 3.9 fromisoformat rejects variable-precision
    fractional seconds like '...03523+00:00'); normalize fractions to 6 digits."""
    s = str(s).replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_local_env() -> None:
    """Local runs: read .env next to this file. (GitHub Actions injects env directly.)"""
    try:
        from pathlib import Path
        p = Path(__file__).with_name(".env")
        if not p.exists():
            return
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _market_hours(now_et) -> bool:
    if now_et.weekday() > 4:      # Sat/Sun
        return False
    f = now_et.hour + now_et.minute / 60.0
    return 9.75 <= f <= 16.0       # 9:45 AM – 4:00 PM ET (a hair after open, through close)


def _latest_scan_age_min():
    """(status, age_minutes) — status in {ok, nodata, nokey}."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return ("nokey", None)
    r = requests.get(
        f"{url}/rest/v1/predictions",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"select": "created_at", "order": "created_at.desc", "limit": 1},
        timeout=20,
    )
    rows = r.json() if r.status_code == 200 else []
    if not rows or not rows[0].get("created_at"):
        return ("nodata", None)
    dt = _parse_ts(rows[0]["created_at"])
    return ("ok", (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)


def _slack(text: str) -> None:
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        print("[heartbeat] no SLACK_WEBHOOK_URL — would have alerted:", text)
        return
    try:
        requests.post(hook, json={"text": text}, timeout=15)
    except Exception as e:
        print(f"[heartbeat] slack post failed: {e}")


def main() -> None:
    _load_local_env()
    now_et = _now_et()
    if not _market_hours(now_et):
        print(f"[heartbeat] outside market hours ({now_et:%a %H:%M} ET) — skip")
        return
    try:
        status, age = _latest_scan_age_min()
    except Exception as e:
        print(f"[heartbeat] check error: {e}")
        return
    if status == "nokey":
        print("[heartbeat] missing Supabase env — skip")
        return
    if status == "nodata" or age is None:
        _slack(f":warning: *ILLUMINATI HEARTBEAT* — no scan data at {now_et:%H:%M} ET "
               f"(market open). The Mac may be asleep/off or the scanner is down — check it.")
    elif age > STALE_MIN:
        _slack(f":warning: *ILLUMINATI HEARTBEAT* — last scan was *{age:.0f} min ago* "
               f"({now_et:%H:%M} ET). Scans run every 30 min, so the Mac is likely asleep "
               f"or the scanner died. Check it.")
    else:
        print(f"[heartbeat] OK — last scan {age:.0f} min ago ({now_et:%H:%M} ET)")


if __name__ == "__main__":
    main()
