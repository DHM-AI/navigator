"""Layer 2 — does the model's signal make MONEY, out of sample?

Layer 1 proved the model predicts BIG MOVES well (OOS AUC 0.76, 21x decile lift)
but its label is direction-agnostic (±20% either way). Layer 2 fetches real
prices, walk-forward, and answers two money questions on the OOS picks:

  A. DIRECTIONAL (what the live system does): take the model's top picks LONG,
     measure forward 5-day SIGNED return vs SPY buy-hold vs random picks.
     Hypothesis (from the 47% finding): no edge — signed return ~0.

  B. DIRECTION-AGNOSTIC (what the signal is actually built for): on those same
     top picks, how big is the ABSOLUTE 5-day move, and how often does it clear
     10/15/20%? This is the straddle/big-move thesis — profit either way.

Usage: python backtest_layer2.py
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import yfinance as yf
from xgboost import XGBClassifier

import config  # noqa: F401

warnings.filterwarnings("ignore")

CACHE = "model/saved/training_data.parquet"
FEATURE_COLS = [
    "bb_width_pct", "bb_squeeze", "atr_ratio", "atr_compression",
    "volume_ratio", "volume_surge", "rsi_value", "rsi_extreme",
    "rsi_bull", "rsi_bear", "ema50_pct",
]
TEST_START = "2023-01"
TRAIN_MIN_MONTHS = 16
FWD = 5   # trading-day horizon (matches the label)


def walk_forward_oos(df):
    """Return OOS rows with a 'prob' column (model trained only on the past)."""
    out = []
    months = sorted(m for m in df["ym"].unique() if str(m) >= TEST_START)
    for m in months:
        tr = df[df["ym"] < m]
        te = df[df["ym"] == m]
        if tr["ym"].nunique() < TRAIN_MIN_MONTHS or te.empty or tr["label"].sum() < 20:
            continue
        spw = (len(tr) - tr["label"].sum()) / max(tr["label"].sum(), 1)
        model = XGBClassifier(n_estimators=300, max_depth=5, learning_rate=0.05,
                              subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
                              eval_metric="logloss", random_state=42, n_jobs=-1)
        model.fit(tr[FEATURE_COLS].values, tr["label"].values, verbose=False)
        te = te.copy()
        te["prob"] = model.predict_proba(te[FEATURE_COLS].values)[:, 1]
        out.append(te[["ticker", "date", "prob", "label"]])
    return pd.concat(out, ignore_index=True)


def fetch_forward_returns(tickers, dmin, dmax):
    """close-to-close signed 5d return + abs max excursion per (ticker, date)."""
    start = (pd.Timestamp(dmin) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(dmax) + pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    frames = {}
    CH = 50
    for i in range(0, len(tickers), CH):
        chunk = tickers[i:i + CH]
        print(f"  prices {i}/{len(tickers)} ...")
        raw = yf.download(chunk, start=start, end=end, progress=False,
                          auto_adjust=True, group_by="ticker", threads=True)
        for t in chunk:
            try:
                c = (raw[t]["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw["Close"]).dropna()
                c.index = pd.to_datetime(c.index).tz_localize(None)
                frames[t] = c
            except Exception:
                pass
    return frames


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print("walk-forward (capturing OOS probabilities)...")
    oos = walk_forward_oos(df)
    print(f"  OOS rows: {len(oos):,}")

    print("fetching prices to compute forward returns...")
    tickers = sorted(oos["ticker"].unique())
    frames = fetch_forward_returns(tickers, oos["date"].min(), oos["date"].max())

    # compute fwd signed return + abs excursion per OOS row
    sret, aexc = [], []
    for tk, grp in oos.groupby("ticker"):
        c = frames.get(tk)
        if c is None or c.empty:
            sret += [np.nan] * len(grp); aexc += [np.nan] * len(grp); continue
        idx = c.index
        for d in grp["date"]:
            pos = idx.searchsorted(d)
            if pos >= len(idx) or idx[pos] != d:
                # nearest prior bar
                pos = min(max(idx.searchsorted(d) - 1, 0), len(idx) - 1)
            entry = float(c.iloc[pos])
            fut = c.iloc[pos + 1: pos + 1 + FWD]
            if entry <= 0 or len(fut) < 3:
                sret.append(np.nan); aexc.append(np.nan); continue
            sret.append((float(fut.iloc[-1]) - entry) / entry * 100)
            mx = (float(fut.max()) - entry) / entry * 100
            mn = (float(fut.min()) - entry) / entry * 100
            aexc.append(max(abs(mx), abs(mn)))
    # groupby reorders; rebuild aligned
    oos2 = pd.concat([g for _, g in oos.groupby("ticker")], ignore_index=True)
    oos2["sret"] = sret
    oos2["aexc"] = aexc
    oos2 = oos2.dropna(subset=["sret", "aexc"])
    print(f"  rows with prices: {len(oos2):,}\n")

    base_sret = oos2["sret"].mean()
    spy_like = base_sret  # avg 5d fwd return across ALL names ~ market drift proxy
    print("=" * 66)
    print("A. DIRECTIONAL — model's top picks taken LONG (what the system does)")
    print("=" * 66)
    print(f"  benchmark: avg 5d fwd return across ALL names = {base_sret:+.2f}% (market drift)")
    for label, q in [("top 10%", 0.90), ("top 2%", 0.98), ("top 0.5%", 0.995)]:
        thr = oos2["prob"].quantile(q)
        pick = oos2[oos2["prob"] >= thr]
        if pick.empty:
            continue
        win = (pick["sret"] > 0).mean() * 100
        avg = pick["sret"].mean()
        gp = pick.loc[pick["sret"] > 0, "sret"].sum()
        gl = pick.loc[pick["sret"] <= 0, "sret"].sum()
        pf = gp / abs(gl) if gl else float("inf")
        print(f"  {label:9} n={len(pick):>6}  avg 5d {avg:+.2f}%  win {win:.0f}%  "
              f"PF {pf:.2f}  vs drift {avg-base_sret:+.2f}%")
    # random benchmark (same n as top 2%)
    n = (oos2["prob"] >= oos2["prob"].quantile(0.98)).sum()
    rng = np.random.RandomState(42)
    rsamp = oos2.iloc[rng.choice(len(oos2), size=min(n*5, len(oos2)), replace=False)]
    print(f"  random  n={len(rsamp):>6}  avg 5d {rsamp['sret'].mean():+.2f}%  "
          f"win {(rsamp['sret']>0).mean()*100:.0f}%  (long, no skill)")

    print("\n" + "=" * 66)
    print("B. DIRECTION-AGNOSTIC — how BIG do the top picks move (either way)?")
    print("=" * 66)
    allbig = (oos2["aexc"] >= 10).mean() * 100
    print(f"  baseline: all names, avg |5d move| {oos2['aexc'].mean():.1f}% · "
          f"≥10% {allbig:.0f}% of the time")
    for label, q in [("top 10%", 0.90), ("top 2%", 0.98), ("top 0.5%", 0.995)]:
        thr = oos2["prob"].quantile(q)
        pick = oos2[oos2["prob"] >= thr]
        if pick.empty:
            continue
        a = pick["aexc"].mean()
        p10 = (pick["aexc"] >= 10).mean() * 100
        p15 = (pick["aexc"] >= 15).mean() * 100
        p20 = (pick["aexc"] >= 20).mean() * 100
        print(f"  {label:9} n={len(pick):>6}  avg |move| {a:.1f}%  "
              f"≥10% {p10:.0f}%  ≥15% {p15:.0f}%  ≥20% {p20:.0f}%")
    print("\n  (A straddle profits when |move| exceeds the option premium. For these")
    print("   high-IV names a 5d ATM straddle runs ~6-10% of spot — so the ≥10/15%")
    print("   columns are the rough 'would a both-ways bet have paid' indicator.")
    print("   Proving the options edge needs real option prices, which we don't have.)")


if __name__ == "__main__":
    main()
