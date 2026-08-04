"""Survivorship-corrected walk-forward (2026-06-30).

Quantifies how much the backtested edge is inflated by training/testing only on
TODAY's S&P 500 (survivors). Runs the SAME walk-forward twice and compares:

  BASELINE  — original parquet (503 survivors only)            [the old number]
  CORRECTED — PIT parquet (survivors + ~70 since-removed names) with point-in-time
              membership, so a removed name is only tradeable during its actual S&P
              tenure (the walk-forward's monthly retrain auto-includes them, fixing
              BOTH training- and test-side survivorship).

Build the PIT parquet first:  python build_pit_parquet.py
Then:                         python backtest_survivorship.py

Reuses backtest_combo's proven walk-forward + simulate + portfolio engine; exits are
the LIVE config (config.SWING_TP_PCT, trailing off, ~weekly-flatten hold).
"""
from __future__ import annotations
import os
import pandas as pd

import config
import backtest_combo as bc
from backtest_regime import FEATURE_COLS
import pit_universe as pit

ORIG = "model/saved/training_data.parquet"
PIT  = "model/saved/training_data_pit.parquet"
START, END = "2021-08-01", "2026-05-31"
HOLD = 5                                   # weekly-flatten cap
TP = config.SWING_TP_PCT                    # live 0.20
TRAIL = None if not config.ENABLE_TRAILING_STOP else 0.03   # live: off


def _prep(path):
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    return df


def _pit_filter(oos):
    """Keep only OOS rows where the ticker was an actual S&P member that month."""
    tabs = pit.fetch_tables(); cur = pit.current_members(tabs); ch = pit.fetch_changes(tabs)
    memo = {}
    def members(p):
        if p not in memo:
            memo[p] = pit.members_on(p.to_timestamp(how="end"), cur, ch)
        return memo[p]
    keep = oos.apply(lambda r: r["ticker"] in members(r["date"].to_period("M")), axis=1)
    return oos[keep].reset_index(drop=True)


def _run(picks, ohlc, reg, thresh):
    sel = picks[picks["prob"] >= thresh]
    trades = []
    for _, r in sel.iterrows():
        if not bc.spy_uptrend_at(reg, r["date"], "none"):
            continue
        d = ohlc.get(r["ticker"])
        if d is None:
            continue
        res = bc.simulate(d, r["date"], TP, TRAIL, HOLD)
        if res is None:
            continue
        ret, ed, xd = res
        trades.append({"net": ret - bc.COST, "entry": ed, "exit": xd, "prob": r["prob"]})
    nets = pd.Series([t["net"] for t in trades])
    if len(nets) == 0:
        return None
    gp = nets[nets > 0].sum(); gl = nets[nets <= 0].sum()
    pf = gp / abs(gl) if gl else float("inf")
    return dict(n=len(nets), avg=nets.mean(), win=(nets > 0).mean()*100, pf=pf)


def main():
    if not os.path.exists(PIT):
        print(f"[surv] {PIT} not found — run build_pit_parquet.py first."); return
    print("[surv] walk-forward on SURVIVOR-ONLY parquet...")
    oos_base = bc.walk_forward_oos(_prep(ORIG), bc.TEST_START)
    print("[surv] walk-forward on SURVIVORSHIP-COMPLETE parquet...")
    oos_corr = _pit_filter(bc.walk_forward_oos(_prep(PIT), bc.TEST_START))

    reg = bc.build_spy_regime()
    tickers = sorted(set(oos_base["ticker"]) | set(oos_corr["ticker"]))
    print(f"[surv] fetching OHLC for {len(tickers)} tickers (incl. delisted)...")
    lo = min(oos_base["date"].min(), oos_corr["date"].min())
    hi = max(oos_base["date"].max(), oos_corr["date"].max())
    ohlc = bc.fetch_ohlc(tickers, lo, hi)

    print("\n" + "=" * 78)
    print(f"  SURVIVORSHIP CORRECTION  ·  exits: TP={TP} trail={TRAIL} hold={HOLD}d  ·  costs {bc.COST}%/rt")
    print(f"  {'tier':>6} {'run':>10} {'n':>6} {'avgNet%':>8} {'win%':>6} {'PF':>6}")
    for thresh in (0.80, 0.90):
        b = _run(oos_base, ohlc, reg, thresh)
        c = _run(oos_corr, ohlc, reg, thresh)
        for tag, x in (("survivor", b), ("corrected", c)):
            if x:
                print(f"  {('≥'+str(thresh)):>6} {tag:>10} {x['n']:>6} {x['avg']:>+8.2f} {x['win']:>6.0f} {x['pf']:>6.2f}")
        if b and c:
            print(f"  {'':>6} {'Δ PF':>10} {'':>6} {'':>8} {'':>6} {c['pf']-b['pf']:>+6.2f}  "
                  f"(survivorship inflated PF by {b['pf']-c['pf']:+.2f})")
    print("\nNOTE: walk-forward 2023→2025 (bull-heavy). CORRECTED = survivors + since-removed")
    print("names (PIT membership). The PF DROP from survivor→corrected is the survivorship bias.")


if __name__ == "__main__":
    main()
