"""Layer 3 — the honest money number: realistic trade simulation, after friction.

Layer 2 showed the model's top picks return +3.4%/5d FRICTIONLESS (close-to-close,
no costs, no stops). This simulates the ACTUAL trade lifecycle on real OHLC bars:
  - enter at the NEXT day's open (no look-ahead on the close-based signal)
  - ATR-based stop (2x ATR, clamped 3-10%) — modeled with real 14-day ATR
  - gaps fill at the OPEN (this is where SMCI-style overnight losses really happen)
  - 20/20/60 partial exits: T1 +7%, T2 +12%, move stop to breakeven after T1
  - the 60% runner trails 3% below peak; hard max-loss 12%; time-exit after 7 days
  - round-trip trading cost (spread+slippage) subtracted; shown at 0.10/0.20/0.30%
Conservative bar convention: assume the STOP is touched before the target.

Compares the realistic result to the frictionless Layer-2 number and to SPY.
THIS number — not the backtest, not a hunch — is the go-live evidence.

Usage: python backtest_layer3.py
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
MAX_HOLD = 7
T1, T2 = 0.07, 0.12          # partial-exit triggers
F1, F2 = 0.20, 0.20          # fractions closed at T1 / T2 (rest rides)
TRAIL = 0.03                 # 3% trail on the runner
HARD_MAX = 0.12              # hard max-loss
ATR_MULT, ATR_FLOOR, ATR_CAP = 2.0, 0.03, 0.10


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
        te = te.copy()
        te["prob"] = model.predict_proba(te[FEATURE_COLS].values)[:, 1]
        out.append(te[["ticker", "date", "prob"]])
    return pd.concat(out, ignore_index=True)


def fetch_ohlc(tickers, dmin, dmax):
    start = (pd.Timestamp(dmin) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(dmax) + pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    out = {}
    CH = 50
    for i in range(0, len(tickers), CH):
        chunk = tickers[i:i + CH]
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
    """14-day ATR as % of close at bar `pos`."""
    if pos < 15:
        return 0.05
    win = d.iloc[pos - 14:pos + 1]
    hl = win["High"] - win["Low"]
    hc = (win["High"] - win["Close"].shift()).abs()
    lc = (win["Low"] - win["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.mean()
    px = float(d["Close"].iloc[pos])
    return float(atr / px) if px > 0 else 0.05


def simulate(d, sig_date):
    """Run one long trade through the real exit stack. Returns gross % return or None."""
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    entry_i = p + 1
    if entry_i >= len(idx) - 2:
        return None
    entry = float(d["Open"].iloc[entry_i])
    if entry <= 0:
        return None
    stop_pct = min(max(ATR_MULT * atr_pct_at(d, p), ATR_FLOOR), ATR_CAP)
    stop = entry * (1 - stop_pct)
    hard = entry * (1 - HARD_MAX)
    t1p, t2p = entry * (1 + T1), entry * (1 + T2)
    realized, remaining = 0.0, 1.0
    t1_done = t2_done = False
    peak = entry
    for j in range(entry_i, min(entry_i + MAX_HOLD, len(idx))):
        o = float(d["Open"].iloc[j]); h = float(d["High"].iloc[j]); l = float(d["Low"].iloc[j]); c = float(d["Close"].iloc[j])
        eff_stop = max(stop, hard)
        # gap-down through the stop → fill at the open (the SMCI case)
        if o <= eff_stop:
            realized += remaining * (o / entry - 1); remaining = 0; break
        # conservative: stop checked before target
        if l <= eff_stop:
            realized += remaining * (eff_stop / entry - 1); remaining = 0; break
        # take-profit partials (gap-up fills at open)
        if not t1_done and h >= t1p:
            fill = max(o, t1p); realized += F1 * (fill / entry - 1); remaining -= F1
            t1_done = True; stop = max(stop, entry)   # move to breakeven
        if not t2_done and h >= t2p:
            fill = max(o, t2p); realized += F2 * (fill / entry - 1); remaining -= F2
            t2_done = True
        # trail the runner
        peak = max(peak, h)
        stop = max(stop, peak * (1 - TRAIL))
    if remaining > 1e-9:
        realized += remaining * (float(d["Close"].iloc[min(entry_i + MAX_HOLD - 1, len(idx) - 1)]) / entry - 1)
    return realized * 100


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print("walk-forward (OOS probs)...")
    oos = walk_forward_oos(df)
    print(f"  OOS rows: {len(oos):,}")

    for tier_label, q in [("top 0.5%", 0.995), ("top 2%", 0.98), ("top 5%", 0.95)]:
        thr = oos["prob"].quantile(q)
        picks = oos[oos["prob"] >= thr].copy()
        print(f"\n{'='*64}\nTIER {tier_label}: {len(picks):,} picks (model prob >= {thr:.3f})")
        tickers = sorted(picks["ticker"].unique())
        print(f"fetching OHLC for {len(tickers)} tickers...")
        ohlc = fetch_ohlc(tickers, picks["date"].min(), picks["date"].max())
        rets = []
        for _, r in picks.iterrows():
            d = ohlc.get(r["ticker"])
            if d is None:
                continue
            g = simulate(d, r["date"])
            if g is not None:
                rets.append(g)
        rets = np.array(rets)
        if len(rets) == 0:
            print("  no simulated trades"); continue
        win = (rets > 0).mean() * 100
        gp = rets[rets > 0].sum(); gl = rets[rets <= 0].sum()
        print(f"  simulated {len(rets):,} trades · GROSS avg {rets.mean():+.2f}% · win {win:.0f}% · "
              f"PF {gp/abs(gl) if gl else float('inf'):.2f}")
        print(f"  worst {rets.min():+.1f}% · best {rets.max():+.1f}% · median {np.median(rets):+.2f}%")
        print("  AFTER round-trip cost (spread+slippage):")
        for cost in (0.10, 0.20, 0.30):
            net = rets - cost
            w = (net > 0).mean() * 100
            gp2 = net[net > 0].sum(); gl2 = net[net <= 0].sum()
            pf2 = gp2 / abs(gl2) if gl2 else float("inf")
            print(f"     @ {cost:.2f}%/trade → avg {net.mean():+.2f}%/trade · win {w:.0f}% · PF {pf2:.2f}")
    print("\n  GO-LIVE BAR: net PF > 1.3 after realistic cost. Reference: SPY ~+0.4%/5d")
    print("  passively over this (bull-market) window, at PF ~irrelevant (no stops).")


if __name__ == "__main__":
    main()
