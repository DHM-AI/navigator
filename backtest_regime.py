"""Regime robustness — does the edge hold when the market ISN'T rising?

Every prior backtest number (PF 1.9, the conviction edge, the +12% exit) was
measured over a 2023-25 BULL market. This re-runs the walk-forward on the
DEPLOYED config (top-0.5% conviction entry + '+12% TP or ATR stop, no trail/no
partials' exit) and splits every simulated trade by the CONCURRENT market move
(SPY return over the trade's holding window):
  - UP-window trades   (SPY rose while held)
  - DOWN-window trades (SPY fell while held)
If the edge only exists in UP windows, it's bull-market beta, not skill. If it
survives DOWN windows, that's the reassurance the go-live case needs.

Usage: python backtest_regime.py
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
TIER_Q = 0.995
HOLD = 10
ATR_MULT, ATR_FLOOR, ATR_CAP = 2.0, 0.03, 0.10
HARD_MAX = 0.12
TP = 0.12                  # the deployed fixed take-profit
COST = 0.20


def walk_forward_oos(df):
    out = []
    months = sorted(m for m in df["ym"].unique() if str(m) >= TEST_START)
    for m in months:
        tr = df[df["ym"] < m]; te = df[df["ym"] == m]
        if tr["ym"].nunique() < TRAIN_MIN_MONTHS or te.empty or tr["label"].sum() < 20:
            continue
        spw = (len(tr) - tr["label"].sum()) / max(tr["label"].sum(), 1)
        model = XGBClassifier(n_estimators=300, max_depth=5, learning_rate=0.05,
                              subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
                              eval_metric="logloss", random_state=42, n_jobs=-1)
        model.fit(tr[FEATURE_COLS].values, tr["label"].values, verbose=False)
        te = te.copy(); te["prob"] = model.predict_proba(te[FEATURE_COLS].values)[:, 1]
        out.append(te[["ticker", "date", "prob"]])
    return pd.concat(out, ignore_index=True)


def fetch_ohlc(tickers, dmin, dmax):
    start = (pd.Timestamp(dmin) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(dmax) + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    out = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        print(f"  ohlc {i}/{len(tickers)} ...")
        raw = yf.download(chunk, start=start, end=end, progress=False,
                          auto_adjust=True, group_by="ticker", threads=True)
        for t in chunk:
            try:
                d = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d[["Open", "High", "Low", "Close"]].dropna()
                d.index = pd.to_datetime(d.index).tz_localize(None)
                if len(d) > 30:
                    out[t] = d
            except Exception:
                pass
    return out


def atr_pct_at(d, pos):
    if pos < 15:
        return 0.05
    w = d.iloc[pos - 14:pos + 1]
    tr = pd.concat([w["High"] - w["Low"],
                    (w["High"] - w["Close"].shift()).abs(),
                    (w["Low"] - w["Close"].shift()).abs()], axis=1).max(axis=1)
    px = float(d["Close"].iloc[pos])
    return float(tr.mean() / px) if px > 0 else 0.05


def simulate(d, sig_date):
    """Deployed exit: +12% TP or ATR stop, no trail/no partials. Returns (ret%, exit_idx_entry)."""
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    ei = p + 1
    if ei >= len(idx) - 2:
        return None
    entry = float(d["Open"].iloc[ei])
    if entry <= 0:
        return None
    stop = entry * (1 - min(max(ATR_MULT * atr_pct_at(d, p), ATR_FLOOR), ATR_CAP))
    hard = entry * (1 - HARD_MAX)
    tp_abs = entry * (1 + TP)
    for j in range(ei, min(ei + HOLD, len(idx))):
        o = float(d["Open"].iloc[j]); h = float(d["High"].iloc[j]); l = float(d["Low"].iloc[j])
        eff = max(stop, hard)
        if o <= eff:
            return (o / entry - 1) * 100, ei
        if l <= eff:
            return (eff / entry - 1) * 100, ei
        if h >= tp_abs:
            return (max(o, tp_abs) / entry - 1) * 100, ei
    return (float(d["Close"].iloc[min(ei + HOLD - 1, len(idx) - 1)]) / entry - 1) * 100, ei


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print("walk-forward (OOS probs)...")
    oos = walk_forward_oos(df)
    thr = oos["prob"].quantile(TIER_Q)
    picks = oos[oos["prob"] >= thr].copy()
    print(f"top-0.5% picks: {len(picks):,}")

    # SPY for regime labeling
    print("fetching SPY + pick OHLC...")
    spy = yf.download("SPY", start="2022-11-01", end="2026-07-01", progress=False, auto_adjust=True)
    # single-ticker yfinance MultiIndex is (PriceField, Ticker) -> level 0 is "Close"
    spyc = spy["Close"]
    if hasattr(spyc, "columns"):          # still a frame -> take the single column
        spyc = spyc.iloc[:, 0]
    spyc = spyc.dropna()
    spyc.index = pd.to_datetime(spyc.index).tz_localize(None)
    ohlc = fetch_ohlc(sorted(picks["ticker"].unique()), picks["date"].min(), picks["date"].max())

    rows = []
    for _, r in picks.iterrows():
        d = ohlc.get(r["ticker"])
        if d is None:
            continue
        res = simulate(d, r["date"])
        if res is None:
            continue
        ret, ei = res
        # concurrent SPY move over the same ~HOLD window from entry date
        edate = d.index[ei]
        sp = spyc[spyc.index >= edate].head(HOLD + 1)
        spy_move = (float(sp.iloc[-1]) / float(sp.iloc[0]) - 1) * 100 if len(sp) >= 2 else 0.0
        rows.append({"ret": ret, "spy": spy_move})
    res = pd.DataFrame(rows)
    res["net"] = res["ret"] - COST

    def stats(x):
        if len(x) == 0:
            return "n=0"
        gp = x[x > 0].sum(); gl = x[x <= 0].sum()
        pf = gp / abs(gl) if gl else float("inf")
        return f"n={len(x):>5}  avg {x.mean():+.2f}%  win {(x>0).mean()*100:>3.0f}%  PF {pf:.2f}"

    print("\n" + "=" * 64)
    print("DEPLOYED CONFIG (conviction entry + +12%/ATR exit) BY MARKET REGIME")
    print("=" * 64)
    print(f"  ALL trades:        {stats(res['net'])}")
    up = res[res["spy"] > 0]["net"]; dn = res[res["spy"] <= 0]["net"]
    print(f"  SPY-UP windows:    {stats(up)}")
    print(f"  SPY-DOWN windows:  {stats(dn)}   <-- the real test")
    # harsher cut: SPY down >1%
    hard_dn = res[res["spy"] <= -1.0]["net"]
    print(f"  SPY-DOWN >1%:      {stats(hard_dn)}")
    print(f"\n  market split: {(res['spy']>0).mean()*100:.0f}% of trades in UP windows, "
          f"{(res['spy']<=0).mean()*100:.0f}% in DOWN windows")
    print("  VERDICT: if SPY-DOWN PF stays > ~1.2, the edge isn't just bull beta.")
    print("  If it collapses below 1.0, the strategy is long-bull-tape-dependent —")
    print("  size tiny / only deploy in confirmed uptrends.")


if __name__ == "__main__":
    main()
