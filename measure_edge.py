"""Measure the model's edge from backfilled outcomes.

Answers the only question that matters before going live: do high-conviction picks
actually go the right way more often than low-conviction ones? Reads
predictions.actual_move_5d (populated by backfill_actuals.py) and reports, per score
bucket, the average move in the predicted direction and the directional hit rate.

METRIC (H-17, 2026-06-09): actual_move_5d is the SIGNED 5-day CLOSE-TO-CLOSE move
(redefined from max-excursion, which overstated correctness — an intraday spike
counted as a hit even when the position would have closed flat). Close-based is
conservative: it ignores bracket TP exits. Numbers measured before 2026-06-09
(56%/57% tiers) were excursion-based — NOT comparable to current output.

Usage:
  python measure_edge.py                 # all backfilled predictions
  python measure_edge.py --since 2026-06-09   # only the forward window (re-measure)

Baseline reference (close-based metric, measured 2026-06-09 after the H-17 recompute):
  TRADED ≥80 = 47% correct / +0.16%  ·  every bucket 47-51%  ·  corr(score,move)=-0.017
  -> NO measurable directional edge in the raw model score. (The earlier 56-57%
  "edge" was a max-excursion artifact.) What edge the SYSTEM has shown lives in
  trade management + condition gates (see trade_autopsy.py), not the score.
The go-live gate is therefore TWO tests on a forward window: this report AND
trade_autopsy realized PF — go live only if realized PF > 1.3 and hit-rate holds.
"""
from __future__ import annotations

import os
import statistics as st
import sys

import requests

import config  # noqa: F401 — loads .env

URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
HDR = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
TRADE_THRESHOLD = getattr(config, "AUTO_EXECUTE_MIN_SCORE", 80)


def _fetch(since: str | None):
    rows, off = [], 0
    params = {"actual_move_5d": "not.is.null",
              "select": "score,actual_move_5d,direction,date", "order": "date.asc"}
    if since:
        params["date"] = f"gte.{since}"
    while True:
        r = requests.get(URL + "/rest/v1/predictions",
                         headers={**HDR, "Range": f"{off}-{off+999}"}, params=params, timeout=60)
        b = r.json()
        if not isinstance(b, list) or not b:
            break
        rows += b
        if len(b) < 1000:
            break
        off += 1000
    return rows


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main(since: str | None = None):
    if not URL or not KEY:
        print("no Supabase creds"); return
    raw = _fetch(since)
    got = []
    for r in raw:
        am, sc = _num(r.get("actual_move_5d")), _num(r.get("score"))
        d = str(r.get("direction") or "").lower()
        if am is None or sc is None:
            continue
        sign = 1 if d in ("bullish", "long", "buy") else (-1 if d in ("bearish", "short", "sell") else 0)
        if sign == 0:
            continue
        got.append({"score": sc, "dir": am * sign, "am": am})
    if not got:
        print("No backfilled predictions in range — run backfill_actuals.py first "
              "(or the window is too recent to have 7-day outcomes yet).")
        return

    span = f"since {since}" if since else "all backfilled"
    print(f"=== MODEL EDGE ({span}) — {len(got)} predictions with outcomes ===")
    print("  score bucket    n      avg move (pred dir)    % correct")
    for lo, hi in [(0, 60), (60, 70), (70, 75), (75, 80), (80, 90), (90, 200)]:
        b = [g for g in got if lo <= g["score"] < hi]
        if not b:
            continue
        avg = sum(g["dir"] for g in b) / len(b)
        pct = sum(1 for g in b if g["dir"] > 0) / len(b) * 100
        print(f"   {lo:>3}-{hi:<3}     {len(b):>5}        {avg:+6.2f}%             {pct:.0f}%")
    print()
    for lbl, sel in ((f"TRADED (>={TRADE_THRESHOLD})", lambda g: g["score"] >= TRADE_THRESHOLD),
                     (f"NOT traded (<{TRADE_THRESHOLD})", lambda g: g["score"] < TRADE_THRESHOLD)):
        b = [g for g in got if sel(g)]
        if not b:
            continue
        avg = sum(g["dir"] for g in b) / len(b)
        pct = sum(1 for g in b if g["dir"] > 0) / len(b) * 100
        print(f"   {lbl:18} n={len(b):>5}  avg dir-move {avg:+.2f}%  correct {pct:.0f}%")
    allm = [g["am"] for g in got]
    xs = [g["score"] for g in got]
    ys = [g["dir"] for g in got]
    mx, my = st.mean(xs), st.mean(ys)
    sx, sy = st.pstdev(xs), st.pstdev(ys)
    corr = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)) / (sx * sy) if sx and sy else 0
    print(f"   baseline raw 5d drift: {sum(allm)/len(allm):+.2f}%   ·   corr(score, move) = {corr:+.3f}")
    print()
    traded = [g for g in got if g["score"] >= TRADE_THRESHOLD]
    if traded:
        pct = sum(1 for g in traded if g["dir"] > 0) / len(traded) * 100
        verdict = ("EDGE HOLDS — concentrate / consider go-live if it clears costs" if pct >= 55
                   else "EDGE WEAK — do NOT scale up or go live" if pct >= 52
                   else "NO EDGE in range — model needs work before real money")
        print(f"   VERDICT (traded tier, {len(traded)} picks, {pct:.0f}% correct): {verdict}")


if __name__ == "__main__":
    a = sys.argv[1:]
    s = a[a.index("--since") + 1] if "--since" in a else None
    main(since=s)
