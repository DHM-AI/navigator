"""Layer 4 — exit-policy sweep: can a better exit recover the halved edge?

Layer 3 showed the live exit stack cuts the raw edge in half (frictionless
+3.4% -> +0.9% gross) because the 20/20/60 partials + tight 3% trail cap the
big winners this strategy lives on. This sweeps alternative exit policies over
the SAME top-0.5% conviction picks (xgb_prob walk-forward) and reports which
maximizes after-cost Profit Factor. Same realism as Layer 3: next-open entry,
ATR stop, gaps fill at the open, conservative stop-before-target.

Usage: python backtest_layer4_exits.py
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
TIER_Q = 0.995                  # top 0.5% conviction
# Stop constants from the LIVE config (Codex review 2026-06-30 — were hardcoded
# 0.10/0.12, stale vs live 0.12/0.14). The POLICIES dict below is an intentional
# exit-policy SWEEP and stays parameterized.
ATR_MULT  = config.ATR_STOP_MULT        # 2.0
ATR_FLOOR = config.ATR_STOP_FLOOR_PCT   # 0.03
ATR_CAP   = config.ATR_STOP_CAP_PCT     # 0.12 (live; was 0.10)
HARD_MAX  = config.HARD_MAX_LOSS_PCT    # 0.14 (live; was 0.12)
COST = 0.20                     # round-trip cost % used for the after-cost ranking

# Exit policies to compare. Each: partials [(trigger,frac),...], trail %, move-to-BE
# after first partial, max hold (trading days), optional hard take-profit (exit all).
POLICIES = {
    "BASELINE 20/20/60 trail3 (OLD live pre-2026-06-10)": dict(parts=[(0.07, 0.20), (0.12, 0.20)], trail=0.03, be=True,  hold=7,  tp=None),
    "wider trail 5%":                   dict(parts=[(0.07, 0.20), (0.12, 0.20)], trail=0.05, be=True,  hold=10, tp=None),
    "no partials, trail 4%":            dict(parts=[],                            trail=0.04, be=False, hold=10, tp=None),
    "no partials, trail 6%":            dict(parts=[],                            trail=0.06, be=False, hold=12, tp=None),
    "later/smaller 10&20, trail4":      dict(parts=[(0.10, 0.15), (0.20, 0.15)], trail=0.04, be=True,  hold=12, tp=None),
    "let runner ride (10/0/90) trail5": dict(parts=[(0.10, 0.10)],               trail=0.05, be=False, hold=12, tp=None),
    "CURRENT live: fixed TP +20% or stop, no trail/no partials": dict(parts=[], trail=None, be=False, hold=5, tp=config.SWING_TP_PCT),
    "longer hold 15d, trail4":          dict(parts=[(0.07, 0.20), (0.12, 0.20)], trail=0.04, be=True,  hold=15, tp=None),
}


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


def simulate(d, sig_date, pol):
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
    stop_pct = min(max(ATR_MULT * atr_pct_at(d, p), ATR_FLOOR), ATR_CAP)
    stop = entry * (1 - stop_pct)
    hard = entry * (1 - HARD_MAX)
    tp_abs = entry * (1 + pol["tp"]) if pol["tp"] else None
    parts = [(entry * (1 + t), f) for t, f in pol["parts"]]
    done = [False] * len(parts)
    realized, remaining, peak = 0.0, 1.0, entry
    for j in range(ei, min(ei + pol["hold"], len(idx))):
        o = float(d["Open"].iloc[j]); h = float(d["High"].iloc[j]); l = float(d["Low"].iloc[j]); c = float(d["Close"].iloc[j])
        eff = max(stop, hard)
        if o <= eff:
            realized += remaining * (o / entry - 1); remaining = 0; break
        if l <= eff:
            realized += remaining * (eff / entry - 1); remaining = 0; break
        if tp_abs and h >= tp_abs:
            realized += remaining * (max(o, tp_abs) / entry - 1); remaining = 0; break
        for k, (lvl, frac) in enumerate(parts):
            if not done[k] and h >= lvl:
                realized += frac * (max(o, lvl) / entry - 1); remaining -= frac; done[k] = True
                if pol["be"] and k == 0:
                    stop = max(stop, entry)
        peak = max(peak, h)
        if pol["trail"] is not None:
            stop = max(stop, peak * (1 - pol["trail"]))
    if remaining > 1e-9:
        realized += remaining * (float(d["Close"].iloc[min(ei + pol["hold"] - 1, len(idx) - 1)]) / entry - 1)
    return realized * 100


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print("walk-forward (OOS probs)...")
    oos = walk_forward_oos(df)
    thr = oos["prob"].quantile(TIER_Q)
    picks = oos[oos["prob"] >= thr].copy()
    print(f"top-0.5% picks: {len(picks):,} (prob >= {thr:.3f})")
    ohlc = fetch_ohlc(sorted(picks["ticker"].unique()), picks["date"].min(), picks["date"].max())

    print(f"\n{'EXIT POLICY':36} {'avgGross':>8} {'avgNet':>7} {'win%':>5} {'PFnet':>6} {'med':>6} {'worst':>7}")
    results = []
    for name, pol in POLICIES.items():
        rets = []
        for _, r in picks.iterrows():
            d = ohlc.get(r["ticker"])
            if d is None:
                continue
            g = simulate(d, r["date"], pol)
            if g is not None:
                rets.append(g)
        rets = np.array(rets)
        if len(rets) == 0:
            continue
        net = rets - COST
        gp, gl = net[net > 0].sum(), net[net <= 0].sum()
        pf = gp / abs(gl) if gl else float("inf")
        results.append((name, rets.mean(), net.mean(), (net > 0).mean() * 100, pf, np.median(net), rets.min()))
    results.sort(key=lambda x: -x[4])   # rank by net PF
    for name, g, n, w, pf, med, worst in results:
        star = "  <-- best" if (name, g, n, w, pf, med, worst) == results[0] else ""
        print(f"  {name:34} {g:>+7.2f}% {n:>+6.2f}% {w:>4.0f}% {pf:>6.2f} {med:>+5.2f}% {worst:>+6.1f}%{star}")
    print(f"\n  (after-cost @ {COST:.2f}%/trade · top-0.5% conviction tier · 2023-2026 walk-forward)")
    print(f"  BASELINE is the live policy. Anything with materially higher PFnet =")
    print(f"  edge the current exits are leaving on the table. Bull-market caveat applies.")


if __name__ == "__main__":
    main()
