"""Entry-quality test — are we buying too extended, and would a filter/pullback help?

Renato's observation: INTC was bought at $128.86, well above its range. Tests, on
the ≥0.90 conviction tier with the NEW exit (no-trail, +20% TP, ATR cap 12%):
  1. Do EXTENDED entries underperform? Bucket picks by ema50_pct (% above EMA50)
     and rsi_value (overbought) at signal — both already in the model's features.
  2. EXTENSION FILTER sweep: skip picks above an ema50_pct threshold → PF + %kept.
  3. PULLBACK ENTRY: instead of market-buy at signal, place a limit X% below the
     signal close; only fill if the dip is touched within 2 days → fill-rate + edge.
Reuses the proven engine. Changes nothing.

Usage: python backtest_entry.py    (SMOKE=1 for a fast recent-window check)
"""
from __future__ import annotations
import os, warnings
import numpy as np
import pandas as pd
from xgboost import XGBClassifier
import config  # noqa
from backtest_regime import fetch_ohlc, atr_pct_at, CACHE, FEATURE_COLS
from backtest_stopwidth import sim, COST   # sim(d, sig_date, atr_cap, trail_pct) → (ret%, reason, recov)

warnings.filterwarnings("ignore")
SMOKE = os.environ.get("SMOKE") == "1"
TEST_START = "2025-06" if SMOKE else "2023-01"
TRAIN_MIN_MONTHS = 16
THRESH = 0.90
CAP, TRAIL = 0.12, None     # the NEW exit we just deployed (no trail, cap 12%)
HOLD = 10; TP = 0.20; ATR_FLOOR = 0.03; HARD_MAX = 0.14


def walk_forward_feat(df, start):
    out = []
    months = sorted(m for m in df["ym"].unique() if str(m) >= start)
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
        out.append(te[["ticker", "date", "prob", "ema50_pct", "rsi_value"]])
        print(f"    oos {m}: {len(te):,}")
    return pd.concat(out, ignore_index=True)


def sim_pullback(d, sig_date, pullback_pct, max_wait=2):
    """Limit entry pullback_pct below the signal-day close; fill only if the low
    touches it within max_wait sessions, then run the NEW exit from the fill."""
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    if p < 1 or p + 1 >= len(idx) - 2:
        return None
    sig_close = float(d["Close"].iloc[p])
    limit = sig_close * (1 - pullback_pct)
    # look for a fill in the next max_wait sessions
    for k in range(p + 1, min(p + 1 + max_wait, len(idx))):
        if float(d["Low"].iloc[k]) <= limit:
            # filled at the limit on day k; run exit from k as the entry bar
            entry = limit
            stop = max(entry * (1 - min(max(2.0 * atr_pct_at(d, k - 1), ATR_FLOOR), CAP)),
                       entry * (1 - HARD_MAX))
            tp_abs = entry * (1 + TP)
            for j in range(k, min(k + HOLD, len(idx))):
                o = float(d["Open"].iloc[j]); h = float(d["High"].iloc[j]); l = float(d["Low"].iloc[j])
                if l <= stop:
                    return (stop / entry - 1) * 100, True
                if h >= tp_abs:
                    return (max(o, tp_abs) / entry - 1) * 100, True
            j = min(k + HOLD - 1, len(idx) - 1)
            return (float(d["Close"].iloc[j]) / entry - 1) * 100, True
    return None, False   # never filled (missed)


def edge(x):
    x = pd.Series(x)
    if len(x) == 0:
        return "n=0"
    gp = x[x > 0].sum(); gl = x[x <= 0].sum(); pf = gp / abs(gl) if gl else float("inf")
    return f"n={len(x):>5}  avg {x.mean():>+6.2f}%  win {(x>0).mean()*100:>3.0f}%  PF {pf:.2f}"


def dist_from_high(d, sig_date, lookback=10):
    """% of the entry (next-open) vs the prior `lookback`-session HIGH at signal.
    >0 = bought at a NEW high (breakout chase); <0 = bought BELOW the high (pullback).
    This is the metric the INTC/AMD/HOOD/TER charts showed (losers ≈ at the high,
    HUM winner ≈ below it) — distinct from the 5-day run-up magnitude."""
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    if p < lookback or p + 1 >= len(idx):
        return None
    entry = float(d["Open"].iloc[p + 1])
    prior_high = float(d["High"].iloc[p - lookback + 1:p + 1].max())
    return (entry / prior_high - 1) * 100 if prior_high > 0 else None


def run_up_at(d, sig_date, lookback=5):
    """% run-up over the prior `lookback` sessions at the signal date (the parabolic
    chase metric the INTC chart showed — distinct from distance-above-EMA)."""
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    if p < lookback:
        return None
    c0 = float(d["Close"].iloc[p - lookback]); c1 = float(d["Close"].iloc[p])
    return (c1 / c0 - 1) * 100 if c0 > 0 else None


def main():
    df = pd.read_parquet(CACHE); df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print(f"walk-forward (SMOKE={SMOKE})...")
    oos = walk_forward_feat(df, TEST_START)
    picks = oos[oos["prob"] >= THRESH].copy()
    if SMOKE:
        picks = picks.sample(min(len(picks), 1500), random_state=1)
    print(f"≥0.90 picks: {len(picks):,} · fetching OHLC...")
    ohlc = fetch_ohlc(sorted(picks["ticker"].unique()), picks["date"].min(), picks["date"].max())

    rows = []
    for _, r in picks.iterrows():
        d = ohlc.get(r["ticker"])
        if d is None:
            continue
        res = sim(d, r["date"], CAP, TRAIL)
        if res is None:
            continue
        ru = run_up_at(d, r["date"], 5)
        vh = dist_from_high(d, r["date"], 10)
        rows.append({"net": res[0] - COST, "ema50_pct": r["ema50_pct"], "rsi": r["rsi_value"],
                     "run5": ru if ru is not None else 0.0,
                     "vs_high": vh if vh is not None else 0.0})
    res = pd.DataFrame(rows)

    print("\n" + "=" * 70 + "\n  1) OUTCOME BY EXTENSION (ema50_pct = % above 50-day EMA at entry)\n" + "=" * 70)
    res["eb"] = pd.cut(res["ema50_pct"], [-100, 0, 3, 6, 10, 100],
                       labels=["<0% (below EMA)", "0-3%", "3-6%", "6-10%", ">10% (stretched)"])
    for b in res["eb"].cat.categories:
        print(f"    {b:>18}: {edge(res[res['eb']==b]['net'])}")

    print("\n  2) OUTCOME BY RSI at entry")
    res["rb"] = pd.cut(res["rsi"], [0, 50, 60, 70, 100], labels=["<50", "50-60", "60-70", "70+ (overbought)"])
    for b in res["rb"].cat.categories:
        print(f"    {b:>18}: {edge(res[res['rb']==b]['net'])}")

    print("\n" + "=" * 70 + "\n  3) EXTENSION FILTER SWEEP — skip picks above ema50_pct threshold\n" + "=" * 70)
    base = res["net"]
    print(f"    {'no filter (current)':>22}: {edge(base)}  · keeps 100%")
    for thr in (10, 8, 6, 4):
        kept = res[res["ema50_pct"] <= thr]["net"]
        print(f"    {f'skip >{thr}% above EMA':>22}: {edge(kept)}  · keeps {len(kept)/len(res)*100:.0f}%")

    print("\n" + "=" * 70 + "\n  4) PULLBACK ENTRY — limit X% below signal close (fill within 2d, else skip)\n" + "=" * 70)
    for pb in (0.0, 0.02, 0.03, 0.05):
        nets = []; filled = 0; total = 0
        for _, r in picks.iterrows():
            d = ohlc.get(r["ticker"])
            if d is None:
                continue
            total += 1
            if pb == 0.0:
                res2 = sim(d, r["date"], CAP, TRAIL)
                if res2:
                    nets.append(res2[0] - COST); filled += 1
            else:
                out = sim_pullback(d, r["date"], pb)
                if out and out[0] is not None:
                    nets.append(out[0] - COST); filled += 1
        tag = "market @ signal" if pb == 0.0 else f"limit -{pb*100:.0f}% pullback"
        print(f"    {tag:>22}: {edge(nets)}  · fill-rate {filled/total*100:.0f}% (missed {100-filled/total*100:.0f}%)")

    print("\n" + "=" * 70 + "\n  5) OUTCOME BY RUN-UP at entry (% over prior 5 sessions) — the INTC pattern\n" + "=" * 70)
    res["ru"] = pd.cut(res["run5"], [-200, 0, 10, 20, 30, 500],
                       labels=["<0% (pulled back in)", "0-10%", "10-20%", "20-30%", ">30% (parabolic)"])
    for b in res["ru"].cat.categories:
        print(f"    {b:>20}: {edge(res[res['ru']==b]['net'])}")

    print("\n" + "=" * 70 + "\n  6) PARABOLIC FILTER SWEEP — skip picks up more than X% in prior 5 sessions\n" + "=" * 70)
    print(f"    {'no filter (current)':>22}: {edge(res['net'])}  · keeps 100%")
    for thr in (30, 25, 20, 15):
        kept = res[res["run5"] <= thr]["net"]
        print(f"    {f'skip >+{thr}% run-up':>22}: {edge(kept)}  · keeps {len(kept)/len(res)*100:.0f}%")

    print("\n  READ: INTC entered ~+30% over 6 sessions. If the '>30% parabolic' bucket")
    print("  underperforms AND the filter raises PF while keeping enough trades, THEN a")
    print("  run-up filter helps — the thing the EMA-distance test missed.")

    print("\n" + "=" * 70 + "\n  7) OUTCOME BY DISTANCE-FROM-10d-HIGH at entry — the breakout-vs-pullback axis\n" + "=" * 70)
    print("  (>0 = bought at a NEW high / breakout chase · <0 = bought below the high / pullback)")
    res["vh"] = pd.cut(res["vs_high"], [-100, -7, -3, 0, 3, 100],
                       labels=["< -7% (deep pullback)", "-7 to -3%", "-3 to 0% (near high)",
                               "0 to +3% (new high)", "> +3% (extended breakout)"])
    for b in res["vh"].cat.categories:
        print(f"    {b:>24}: {edge(res[res['vh']==b]['net'])}")

    print("\n" + "=" * 70 + "\n  8) BREAKOUT FILTER SWEEP — skip picks bought at/above a recent high\n" + "=" * 70)
    print(f"    {'no filter (current)':>26}: {edge(res['net'])}  · keeps 100%")
    for thr in (3, 0, -2, -4):
        kept = res[res["vs_high"] <= thr]["net"]
        lbl = f"skip if within {abs(thr)}% of high" if thr <= 0 else f"skip if >{thr}% above high"
        print(f"    {lbl:>26}: {edge(kept)}  · keeps {len(kept)/len(res)*100:.0f}%")

    print("\n  READ: the 5 chart examples (4 losers at/above high, HUM winner below) suggest")
    print("  breakout entries underperform. If the '>0% (new high)' buckets are weak AND the")
    print("  filter raises PF on the FULL 2,412 trades, it's real — not just this week's noise.")


if __name__ == "__main__":
    main()
