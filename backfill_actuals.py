"""Backfill predictions.actual_move_5d — the dead feedback loop, repaired.

WHY THIS EXISTS: the in-scan _backfill_actual_moves() (agent.py) called
db.load_predictions(), which PostgREST caps at ~1000 rows — always the most-recent
ones, all < 7 days old — so its ">=7 days" filter skipped every row and it
backfilled 0 of 5,229 predictions in 3 weeks. The model never recorded a single
outcome, so its edge could never be measured.

This tool loads the rows that ACTUALLY need backfilling (actual_move_5d IS NULL and
date <= today-7d, fully paginated), fetches each ticker's OHLCV once, computes the
5-day move (same definition as the original: the largest absolute move over the 5
trading days after the prediction date), and writes it back. Idempotent — only
touches null, aged rows — so it's safe to run daily.

Usage:
  python backfill_actuals.py --limit 25 --verify   # small test, read back
  python backfill_actuals.py                        # full backfill
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

import config  # noqa: F401 — side effect loads .env into os.environ

SUPA_URL = os.environ.get("SUPABASE_URL")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
HDR = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}
LOOKAHEAD_DAYS = 7          # a prediction is backfillable once it's this old
MIN_FUTURE_BARS = 3         # need >=3 of the 5 forward bars to compute a move


def fetch_backfillable() -> list[dict]:
    """All predictions with actual_move_5d NULL and date <= today-7d (paginated)."""
    cutoff = (date.today() - timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    out, off = [], 0
    while True:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/predictions",
            headers={**HDR, "Range": f"{off}-{off+999}"},
            params={"select": "date,ticker", "actual_move_5d": "is.null",
                    "date": f"lte.{cutoff}", "order": "date.asc"},
            timeout=60,
        )
        b = r.json()
        if not isinstance(b, list) or not b:
            break
        out += b
        if len(b) < 1000:
            break
        off += 1000
    return out


def _close_series(data, ticker: str, multi: bool):
    try:
        df = data[ticker] if multi else data
        s = df["Close"].dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None)  # tz-naive (the old bug)
        return s
    except Exception:
        return None


def compute_move(close: "pd.Series", pred_date: "pd.Timestamp"):
    """Largest absolute % move over the 5 trading days AFTER pred_date, or None."""
    if close is None or close.empty:
        return None
    past = close[close.index <= pred_date]
    future = close[close.index > pred_date].head(5)
    if past.empty or len(future) < MIN_FUTURE_BARS:
        return None
    entry = float(past.iloc[-1])
    if entry <= 0:
        return None
    mx = (float(future.max()) - entry) / entry * 100
    mn = (float(future.min()) - entry) / entry * 100
    return round(mx if abs(mx) >= abs(mn) else mn, 2)


def _patch(date_str: str, ticker: str, move: float) -> bool:
    r = requests.patch(
        f"{SUPA_URL}/rest/v1/predictions",
        headers={**HDR, "Prefer": "return=minimal"},
        params={"date": f"eq.{date_str}", "ticker": f"eq.{ticker}"},
        json={"actual_move_5d": move}, timeout=30,
    )
    return r.status_code in (200, 204)


def run(limit: int | None = None, verify: bool = False) -> dict:
    if not SUPA_URL or not SUPA_KEY:
        print("[backfill] no Supabase creds"); return {}
    rows = fetch_backfillable()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("[backfill] nothing to backfill (all aged predictions already have outcomes)")
        return {"rows": 0}
    tickers = sorted({r["ticker"] for r in rows})
    dmin, dmax = min(r["date"] for r in rows), max(r["date"] for r in rows)
    print(f"[backfill] {len(rows)} rows · {len(tickers)} tickers · {dmin} -> {dmax}")
    start = (pd.to_datetime(dmin) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    end = (pd.to_datetime(dmax) + pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    print(f"[backfill] fetching OHLCV {start} -> {end} ...")
    data = yf.download(tickers, start=start, end=end, progress=False,
                       group_by="ticker", threads=True, auto_adjust=True)
    multi = len(tickers) > 1
    # Pre-build the close series per ticker once.
    closes = {tk: _close_series(data, tk, multi) for tk in tickers}

    written = failed = nodata = 0
    updates = []
    for r in rows:
        mv = compute_move(closes.get(r["ticker"]), pd.to_datetime(r["date"]))
        if mv is None:
            nodata += 1
            continue
        updates.append((r["date"], r["ticker"], mv))

    print(f"[backfill] computed {len(updates)} moves ({nodata} had no usable price data); writing...")
    # Write concurrently (per-row PATCH; PostgREST can't batch differing values).
    def _do(u):
        return _patch(*u)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_do, u): u for u in updates}
        for i, fut in enumerate(as_completed(futs), 1):
            ok = False
            try:
                ok = fut.result()
            except Exception:
                ok = False
            written += 1 if ok else 0
            failed += 0 if ok else 1
            if i % 250 == 0:
                print(f"  [backfill] {i}/{len(updates)} written (failed {failed})...")

    print(f"[backfill] DONE — wrote {written}, failed {failed}, no-data {nodata}")
    if verify and updates:
        d, t, m = updates[0]
        chk = requests.get(f"{SUPA_URL}/rest/v1/predictions",
                           headers=HDR,
                           params={"select": "date,ticker,actual_move_5d",
                                   "date": f"eq.{d}", "ticker": f"eq.{t}"}, timeout=20).json()
        print(f"[backfill] verify sample {t} {d}: wrote {m}, db now = {chk}")
    return {"rows": len(rows), "written": written, "failed": failed, "nodata": nodata}


if __name__ == "__main__":
    args = sys.argv[1:]
    lim = None
    if "--limit" in args:
        lim = int(args[args.index("--limit") + 1])
    run(limit=lim, verify="--verify" in args)
