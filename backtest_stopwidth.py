"""Stop-width sweep — is the ATR stop too tight for the 5-7 day hold?

Question: most live stops are pinned at the 10% CAP, meaning 2×ATR wanted wider
and got clipped. Does loosening the cap / multiplier REDUCE premature noise
stop-outs enough to beat the bigger loss-per-stop? Sweeps ATR_MULT × ATR_CAP on
the live conviction tier (two-sided ≥0.90, long, +20%TP/3%trail, 10d hold).
Reuses the proven walk-forward + OHLC engine. Changes nothing.

Usage: python backtest_stopwidth.py   (SMOKE=1 for a fast recent-window check)
"""
from __future__ import annotations
import os, warnings
import pandas as pd
import config  # noqa
from backtest_regime import fetch_ohlc, atr_pct_at, CACHE, FEATURE_COLS
from backtest_combo import walk_forward_oos as walk_forward, COST

warnings.filterwarnings("ignore")
SMOKE = os.environ.get("SMOKE") == "1"
TEST_START = "2025-06" if SMOKE else "2023-01"
HOLD = 10; THRESH = 0.90; TP = 0.20; TRAIL = 0.03; ATR_FLOOR = 0.03; HARD_MAX = 0.12


def sim(d, sig_date, atr_cap, trail_pct, atr_mult=2.0):
    """Returns (ret%, reason) where reason ∈ atr_stop/trail/tp/time. trail_pct=None → no trail."""
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    if p < 1:
        return None
    ei = p + 1
    if ei >= len(idx) - 2:
        return None
    entry = float(d["Open"].iloc[ei])
    if entry <= 0:
        return None
    init_stop = max(entry * (1 - min(max(atr_mult * atr_pct_at(d, p), ATR_FLOOR), atr_cap)),
                    entry * (1 - HARD_MAX))
    stop = init_stop; tp_abs = entry * (1 + TP); hi = entry
    end = min(ei + HOLD, len(idx))
    for j in range(ei, end):
        o = float(d["Open"].iloc[j]); h = float(d["High"].iloc[j]); l = float(d["Low"].iloc[j])
        if o <= stop or l <= stop:
            px = o if o <= stop else stop
            reason = "trail" if stop > init_stop + 1e-9 else "atr_stop"
            # PREMATURE check: would price have recovered to >= entry later in the hold?
            recov = False
            if j + 1 < end:
                fut_hi = max(float(d["High"].iloc[k]) for k in range(j + 1, end))
                recov = fut_hi >= entry
            return (px / entry - 1) * 100, reason, recov
        if h >= tp_abs:
            return (max(o, tp_abs) / entry - 1) * 100, "tp", False
        if trail_pct is not None:
            hi = max(hi, h); stop = max(stop, hi * (1 - trail_pct))
    j = min(ei + HOLD - 1, len(idx) - 1)
    return (float(d["Close"].iloc[j]) / entry - 1) * 100, "time", False


def main():
    df = pd.read_parquet(CACHE); df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print(f"walk-forward (SMOKE={SMOKE}, start {TEST_START})...")
    oos = walk_forward(df, TEST_START)
    picks = oos[oos["prob"] >= THRESH]
    if SMOKE:
        picks = picks.sample(min(len(picks), 1500), random_state=1)
    print(f"≥0.90 picks: {len(picks):,} · fetching OHLC...")
    ohlc = fetch_ohlc(sorted(picks["ticker"].unique()), picks["date"].min(), picks["date"].max())

    print(f"\n  {'config':>22} {'n':>5} {'avgNet':>8} {'win%':>5} {'PF':>6} | "
          f"{'atrStop%':>8} {'trail%':>7} {'tp%':>5} {'time%':>6} | {'premATRstops':>12}")
    # trail=0.03 is the LIVE acct2 config; None isolates the ATR stop alone
    for trail, tlab in [(0.03, "trail3%"), (None, "no-trail")]:
        for cap in (0.10, 0.12, 0.15, 0.20):
            nets = []; reasons = {"atr_stop": 0, "trail": 0, "tp": 0, "time": 0}
            atr_n = 0; atr_prem = 0
            for _, r in picks.iterrows():
                d = ohlc.get(r["ticker"])
                if d is None:
                    continue
                res = sim(d, r["date"], cap, trail)
                if res is None:
                    continue
                ret, reason, recov = res
                nets.append(ret - COST); reasons[reason] += 1
                if reason == "atr_stop":
                    atr_n += 1; atr_prem += 1 if recov else 0
            x = pd.Series(nets); n = len(x)
            gp = x[x > 0].sum(); gl = x[x <= 0].sum(); pf = gp / abs(gl) if gl else float("inf")
            prem = f"{atr_prem}/{atr_n} ({atr_prem/atr_n*100:.0f}%)" if atr_n else "—"
            cur = " ←CURRENT" if (trail == 0.03 and cap == 0.10) else ""
            print(f"  {f'{tlab} ATRcap{cap:.0%}':>22} {n:>5} {x.mean():>+7.2f}% {(x>0).mean()*100:>4.0f}% {pf:>6.2f} | "
                  f"{reasons['atr_stop']/n*100:>7.0f}% {reasons['trail']/n*100:>6.0f}% "
                  f"{reasons['tp']/n*100:>4.0f}% {reasons['time']/n*100:>5.0f}% | {prem:>12}{cur}")
    print("\n  READ: 'premATRstops' = of trades stopped on the ATR stop, how many RECOVERED")
    print("  to >= entry later in the hold (= the stop was too tight / premature). High % +")
    print("  PF rising with a wider cap ⇒ the ATR stop IS too tight; widen it. The 'no-trail'")
    print("  rows isolate the ATR stop (trail=3% otherwise harvests winners before TP).")


if __name__ == "__main__":
    main()
