"""
backtest_counterfactual.py — replay ALL realized trades under THIS session's new
strategy and compare to what actually happened (Renato, 2026-06-16).

"New strategy" = the changes deployed this session:
  • shorts OFF (ENABLE_SHORTS=False)          → short entries would not be taken
  • near-high entry filter (NEAR_HIGH_BAND%)  → skip entries within 2% of the
                                                 prior 10-day high
  • exits: NO trailing, NO partials           → ride to a fixed +TP or the ATR
                                                 stop cap (−12%), no early harvest

For every realized round-trip it: drops shorts, applies the near-high filter at
entry, then re-simulates the exit on daily bars (stop-first if both hit same
day). Compares realized (old exits) vs new-strategy on the SAME entries.

⚠️ Caveat (stated in output): the realized record is ~3 weeks. The new exits hold
to TP/stop (days–weeks) vs the old fast exits (hours), so with data only to today
many entries are "still open" under the new rules. The statistically robust answer
is the 3-yr walk-forward (backtest_combo.py: PF 1.42→2.26). This replay is a
reality-check on the ACTUAL trades, not the primary evidence.
"""
from __future__ import annotations
import warnings
import numpy as np
import pandas as pd

import trade_autopsy as ta
from backtest_regime import fetch_ohlc
import config as c

warnings.filterwarnings("ignore")

TP_PCT  = float(getattr(c, "SWING_TP_PCT", 0.12))        # main acct +12%
STOP_PCT = float(getattr(c, "ATR_STOP_CAP_PCT", 0.12))   # ATR cap −12%
NH_BAND = float(getattr(c, "NEAR_HIGH_BAND_PCT", 2.0))
HORIZON = 20   # trading days to ride to TP/stop


def _stats(xs):
    if not xs:
        return dict(n=0, win=0.0, avg=0.0, pf=0.0, tot=0.0)
    a = np.array(xs)
    w = a[a > 0].sum(); l = -a[a < 0].sum()
    return dict(n=len(a), win=float((a > 0).mean()) * 100,
                avg=float(a.mean()), pf=(w / l if l > 0 else float("inf")),
                tot=float(a.sum()))


def _row(name, s):
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"{name:<34}{s['n']:>6}{s['win']:>8.0f}{s['avg']:>9.2f}{pf:>8}")


def main():
    print(f"new-strategy params: TP +{TP_PCT:.0%} · stop −{STOP_PCT:.0%} · "
          f"near-high band {NH_BAND:.0f}% · shorts OFF · horizon {HORIZON}d\n")
    fills = ta.fetch_fills(220)
    trips = ta.round_trips(fills)
    tickers = sorted(set(t["tk"] for t in trips))
    start = pd.Timestamp(min(t["entry_t"] for t in trips).date()) - pd.Timedelta(days=25)
    end = pd.Timestamp.today().normalize()
    print(f"replaying {len(trips)} realized round-trips · {len(tickers)} tickers · "
          f"{start.date()}→{end.date()} (fetching bars…)\n")
    ohlc = fetch_ohlc(tickers, start, end)

    realized_all, longs_realized = [], []
    shorts_removed, nh_skipped_realized = [], []
    new_resolved, still_open = [], []
    kept_realized = []   # realized% of the trades the new strategy KEEPS (for apples-apples)

    for t in trips:
        realized_all.append(t["pnl_pct"])
        if t["dir"] == "short":
            shorts_removed.append(t["pnl_pct"]); continue   # new strategy: no shorts
        longs_realized.append(t["pnl_pct"])
        d = ohlc.get(t["tk"])
        if d is None or d.empty:
            continue
        high = d["High"].astype(float); low = d["Low"].astype(float); close = d["Close"].astype(float)
        idx = close.index
        pos = idx.searchsorted(pd.Timestamp(t["entry_t"].date()))
        if pos >= len(idx):
            continue
        entry = float(t["entry_px"])
        # near-high filter at entry (prior 10-day high, excludes entry bar)
        if pos >= 11:
            ph = float(high.iloc[pos - 10:pos].max())
            if ph > 0 and (entry / ph - 1) * 100 > -NH_BAND:
                nh_skipped_realized.append(t["pnl_pct"]); continue
        # re-simulate new exit (long): stop-first if both hit same bar
        stop_px, tp_px = entry * (1 - STOP_PCT), entry * (1 + TP_PCT)
        endpos = min(pos + HORIZON, len(idx) - 1)
        outcome = None
        for j in range(pos + 1, endpos + 1):
            if float(low.iloc[j]) <= stop_px:
                outcome = -STOP_PCT * 100; break
            if float(high.iloc[j]) >= tp_px:
                outcome = TP_PCT * 100; break
        if outcome is None:
            mtm = (float(close.iloc[endpos]) - entry) / entry * 100
            if endpos < pos + HORIZON:      # ran out of forward data → still open
                still_open.append(mtm); continue
            outcome = mtm                    # held full horizon → mark to close
        new_resolved.append(outcome)
        kept_realized.append(t["pnl_pct"])

    print(f"{'cohort':<34}{'N':>6}{'win%':>8}{'avg%':>9}{'PF':>8}")
    print("-" * 65)
    _row("REALIZED — all actual trades", _stats(realized_all))
    _row("REALIZED — longs only", _stats(longs_realized))
    print("-" * 65)
    _row("NEW STRATEGY — resolved (TP/stop)", _stats(new_resolved))
    _row("  (same entries, REALIZED exits)", _stats(kept_realized))
    print("-" * 65)
    print("Effect of each new rule on the realized record:")
    sr, ns = _stats(shorts_removed), _stats(nh_skipped_realized)
    print(f"  • shorts REMOVED:     {sr['n']:>4} trades, realized avg {sr['avg']:+.2f}%, "
          f"PF {('inf' if sr['pf']==float('inf') else f'{sr['pf']:.2f}')}, net {sr['tot']:+.1f}% "
          f"→ {'DROPS losers ✓' if sr['avg']<0 else 'drops winners ✗'}")
    print(f"  • near-high SKIPPED:  {ns['n']:>4} trades, realized avg {ns['avg']:+.2f}%, "
          f"net {ns['tot']:+.1f}% → {'DROPS losers ✓' if ns['avg']<0 else 'drops winners ✗'}")
    print(f"  • still-open under new exits (no resolve yet): {len(still_open)} "
          f"(avg mark {np.mean(still_open):+.2f}%)" if still_open else
          "  • still-open under new exits: 0")
    print("\nNOTE: realized exits were fast (hrs); new exits hold to ±TP/stop, so the "
          "\nresolved set is partial. Robust answer = 3-yr walk-forward PF 1.42→2.26.")


if __name__ == "__main__":
    main()
