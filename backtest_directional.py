"""Directional-model experiment — THE core fix test.

The live model is trained on a TWO-SIDED label (a +/-20% move in EITHER direction)
but traded LONG-ONLY, so a high prob means 'big move coming, ~39% of them down'.
This rebuilds an UP-ONLY label (max_move >= +20% in next 5d, ignore down moves),
walk-forward-trains a model on it, and compares — apples-to-apples on the SAME
features, picks-simulation, costs — against the current two-sided label:

  per model (two-sided vs up-only), for the >=0.90 OOS tier traded LONG:
    • per-trade edge      : n, avg net%, win%, PF
    • calibration         : empirical UP-hit-rate (did it actually rise >=20%?) by prob bin
    • regime split        : SPY-up vs SPY-down at entry (does the edge survive down tape?)
    • cost sweep          : PF at 0.20 / 0.30 / 0.40 / 0.50% round-trip

SAFETY: does NOT touch the live model. Reuses the proven engine from
backtest_combo/backtest_regime. Reads the existing parquet's features; only the
LABEL is recomputed from freshly fetched prices.

Usage:  python backtest_directional.py          # full
        SMOKE=1 python backtest_directional.py   # fast recent-window sanity
"""
from __future__ import annotations
import os, warnings
import numpy as np
import pandas as pd
import yfinance as yf
from xgboost import XGBClassifier

import config  # noqa
from backtest_regime import fetch_ohlc, atr_pct_at, CACHE, FEATURE_COLS
from backtest_combo import simulate, build_spy_regime, spy_uptrend_at, edge, COST

warnings.filterwarnings("ignore")

SMOKE = os.environ.get("SMOKE") == "1"
TEST_START = "2025-06" if SMOKE else "2023-01"
TRAIN_MIN_MONTHS = 16
UP_TARGET = 0.20          # up-only label: +20% in next 5 days
HOLD = 10
THRESH = 0.90             # the live conviction tier


def add_uponly_label(df, ohlc):
    """Recompute an up-only label (forward 5-day max close >= +20%) per row,
    merged onto the existing parquet rows by (ticker, date)."""
    recs = []
    for ticker, d in ohlc.items():
        close = d["Close"].astype(float)
        idx = close.index
        arr = close.values
        n = len(arr)
        for i in range(n):
            j = min(i + 6, n)
            if j > i + 1 and arr[i] > 0:
                fmax = arr[i + 1:j].max()
                mm = (fmax - arr[i]) / arr[i]
                recs.append((ticker, idx[i], 1 if mm >= UP_TARGET else 0))
    lab = pd.DataFrame(recs, columns=["ticker", "date", "up_label"])
    return df.merge(lab, on=["ticker", "date"], how="left")


def walk_forward(df, start, label_col):
    out = []
    months = sorted(m for m in df["ym"].unique() if str(m) >= start)
    for m in months:
        tr = df[df["ym"] < m]; te = df[df["ym"] == m]
        if tr["ym"].nunique() < TRAIN_MIN_MONTHS or te.empty or tr[label_col].sum() < 20:
            continue
        spw = (len(tr) - tr[label_col].sum()) / max(tr[label_col].sum(), 1)
        model = XGBClassifier(n_estimators=300, max_depth=5, learning_rate=0.05,
                              subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
                              eval_metric="logloss", random_state=42, n_jobs=-1)
        model.fit(tr[FEATURE_COLS].values, tr[label_col].values, verbose=False)
        te = te.copy(); te["prob"] = model.predict_proba(te[FEATURE_COLS].values)[:, 1]
        out.append(te[["ticker", "date", "prob", "up_label", "label"]])
        print(f"    {label_col} oos {m}: {len(te):,}")
    return pd.concat(out, ignore_index=True)


def run_model(name, oos, ohlc, reg):
    """For the >=0.90 OOS tier, simulate LONG trades and report edge/calibration/regime."""
    # calibration: empirical up_label rate by prob bin (does high prob => actual rise?)
    print(f"\n{'='*72}\n  MODEL: {name}\n{'='*72}")
    bins = [0.5, 0.7, 0.8, 0.9, 0.95, 1.01]
    print("  CALIBRATION (empirical UP >=+20% rate by predicted prob):")
    for lo, hi in zip(bins, bins[1:]):
        seg = oos[(oos["prob"] >= lo) & (oos["prob"] < hi)]
        if len(seg):
            print(f"    prob {lo:.2f}-{hi:.2f}: n={len(seg):>6}  up-rate {seg['up_label'].mean()*100:>5.1f}%")
    picks = oos[oos["prob"] >= THRESH]
    print(f"  >=0.90 picks: {len(picks):,}")

    rows = []
    for _, r in picks.iterrows():
        d = ohlc.get(r["ticker"])
        if d is None:
            continue
        # live long exit, config-aligned (Codex review 2026-06-30): +20% TP, NO trail
        # (was hardcoded trail_pct=0.03 — live trailing is OFF).
        res = simulate(d, r["date"], tp=config.SWING_TP_PCT,
                       trail_pct=(None if not config.ENABLE_TRAILING_STOP else 0.03),
                       hold=HOLD)
        if res is None:
            continue
        ret, edate, _ = res
        rows.append({"net": ret - COST, "raw": ret, "entry": edate})
    if not rows:
        print("  no simulated trades"); return
    res = pd.DataFrame(rows)
    print(f"  LONG edge (>=0.90, +20%TP/trail, 0.20% cost): {edge(res['net'])}")
    # regime split by SPY trend at entry
    res["up_reg"] = res["entry"].map(lambda e: spy_uptrend_at(reg, e, "sma200"))
    print(f"    SPY-uptrend entries: {edge(res[res['up_reg']]['net'])}")
    print(f"    SPY-downtrend entries: {edge(res[~res['up_reg']]['net'])}   <-- the real test")
    # cost sweep
    print("  COST SWEEP (PF):", end=" ")
    for c in (0.20, 0.30, 0.40, 0.50):
        x = res["raw"] - c
        gp = x[x > 0].sum(); gl = x[x <= 0].sum()
        pf = gp / abs(gl) if gl else float("inf")
        print(f"{c:.2f}%→{pf:.2f}", end="  ")
    print()


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    tickers = sorted(df["ticker"].unique())
    print(f"parquet: {len(df):,} rows, {len(tickers)} tickers, {df['date'].min().date()}→{df['date'].max().date()}")

    print("fetching OHLCV for up-only relabel (all tickers)...")
    ohlc = fetch_ohlc(tickers, df["date"].min(), df["date"].max())
    print(f"  fetched {len(ohlc)}/{len(tickers)} tickers")

    df = add_uponly_label(df, ohlc)
    before = len(df)
    df = df.dropna(subset=["up_label"])
    df["up_label"] = df["up_label"].astype(int)
    print(f"up-only label built: base rate {df['up_label'].mean()*100:.3f}% "
          f"(two-sided was {df['label'].mean()*100:.3f}%) · kept {len(df):,}/{before:,} rows")

    if SMOKE:
        keep = df["ym"].astype(str) >= "2024-06"
        df = df[keep]

    reg = build_spy_regime()

    print("\nwalk-forward: TWO-SIDED model (current)...")
    oos_two = walk_forward(df, TEST_START, "label")
    print("walk-forward: UP-ONLY model (directional fix)...")
    oos_up = walk_forward(df, TEST_START, "up_label")

    run_model("TWO-SIDED label (current live model)", oos_two, ohlc, reg)
    run_model("UP-ONLY label (directional fix)", oos_up, ohlc, reg)

    print("\n" + "=" * 72)
    print("READ: up-only WINS if its >=0.90 tier has (a) a higher empirical UP-rate")
    print("(better directional calibration), (b) a higher LONG PF, and (c) survives")
    print("SPY-downtrend entries better than two-sided (PF closer to/above ~1.2).")
    print("If up-only's down-tape PF is still <1.0, the fix isn't enough alone — a")
    print("regime gate stays mandatory. CAVEAT: 2023-25 is bull-heavy; costs 0.20%+.")


if __name__ == "__main__":
    main()
