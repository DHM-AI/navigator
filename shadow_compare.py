"""
shadow_compare.py — evaluate the DIRECTIONAL SHADOW vs the live model (Renato, 2026-06-16).

Reads the head-to-head picks logged by shadow_scan.py and scores each model's
would-trade picks on their realized FORWARD 5-trading-day return (long). Reports,
for the live (two-sided) model and the up-only shadow model:
  • N completed picks (forward window elapsed)
  • directional HIT RATE = % with a positive 5-day return  ← the Jun-30 gate metric (≥53%)
  • avg 5-day return
  • PROFIT FACTOR = gross wins / gross losses              ← the Jun-30 gate metric (>1.3)
  • a pending count (picks too recent to have a completed window)

This is a transparent forward read, NOT the full exit stack — it answers the one
question the shadow exists to answer: does the up-only model rank UP moves better
than the two-sided model on the SAME days? Run anytime; it only scores picks whose
5-day window has closed. After ~4 weeks of logs, this is the promotion decision.

Run (from market-predictions/, with .venv):
    .venv/bin/python shadow_compare.py
    .venv/bin/python shadow_compare.py --since 2026-06-17   # forward window only
"""
from __future__ import annotations
import sys, json, warnings
import numpy as np
import pandas as pd

from backtest_regime import fetch_ohlc

warnings.filterwarnings("ignore")

LOG_PATH = "model/saved/shadow_picks.jsonl"
HORIZON = 5   # trading days forward


def _stats(returns: list[float]) -> dict:
    if not returns:
        return {"n": 0, "hit": 0.0, "avg": 0.0, "pf": 0.0}
    r = np.array(returns)
    wins = r[r > 0].sum()
    losses = -r[r < 0].sum()
    return {
        "n": len(r),
        "hit": float((r > 0).mean()) * 100,
        "avg": float(r.mean()) * 100,
        "pf": float(wins / losses) if losses > 0 else float("inf"),
    }


def main() -> None:
    since = None
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]

    try:
        recs = [json.loads(l) for l in open(LOG_PATH) if l.strip()]
    except FileNotFoundError:
        raise SystemExit(f"[compare] no log yet at {LOG_PATH} — run shadow_scan.py first")
    df = pd.DataFrame(recs)
    if since:
        df = df[df["date"] >= since]
    if df.empty:
        raise SystemExit("[compare] no picks in range")

    tickers = sorted(df["ticker"].unique())
    start = pd.Timestamp(df["date"].min())
    end = pd.Timestamp(df["date"].max()) + pd.Timedelta(days=HORIZON * 3 + 10)
    print(f"[compare] {len(df)} logged rows · {len(tickers)} tickers · "
          f"{df['date'].min()}→{df['date'].max()}  (fetching forward prices…)")
    ohlc = fetch_ohlc(tickers, start, end)

    live_ret, up_ret, pending = [], [], 0
    for _, row in df.iterrows():
        d = ohlc.get(row["ticker"])
        if d is None or d.empty:
            continue
        close = d["Close"].astype(float)
        idx = close.index
        pos = idx.searchsorted(pd.Timestamp(row["date"]))
        # entry = first bar on/after the scan date; exit = +HORIZON trading days
        if pos >= len(idx) or pos + HORIZON >= len(idx):
            if row["would_trade_live"] or row["would_trade_up"]:
                pending += 1
            continue
        entry = float(close.iloc[pos])
        exit_ = float(close.iloc[pos + HORIZON])
        if entry <= 0:
            continue
        ret = (exit_ - entry) / entry
        if row["would_trade_live"]:
            live_ret.append(ret)
        if row["would_trade_up"]:
            up_ret.append(ret)

    L, U = _stats(live_ret), _stats(up_ret)
    print(f"\n{'model':<22}{'picks':>7}{'hit%':>9}{'avg5d%':>9}{'PF':>8}")
    print("-" * 55)
    for name, s in (("LIVE (two-sided)", L), ("UP-ONLY (shadow)", U)):
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{name:<22}{s['n']:>7}{s['hit']:>8.1f}{s['avg']:>9.2f}{pf:>8}")
    print(f"\npending (window not yet closed): {pending}")
    print("Jun-30 gate (either model): PF > 1.3 AND hit% ≥ 53 to be promotable.")
    if U["n"] >= 20 and L["n"] >= 20:
        verdict = ("UP-ONLY better" if (U["pf"] > L["pf"] and U["hit"] >= L["hit"])
                   else "LIVE better/tie" if L["pf"] >= U["pf"] else "mixed")
        print(f"head-to-head (n≥20 both): {verdict}")
    else:
        print("head-to-head: need ≥20 completed picks each for a read (keep logging).")


if __name__ == "__main__":
    main()
