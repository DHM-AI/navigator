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
# Exit constants from the LIVE config so this can't drift from what we trade
# (Codex review 2026-06-30 — were hardcoded 0.10/0.12/TP 0.12, stale vs live
# 0.12/0.14/TP 0.20). The docstring's "+12% TP" describes the OLD deployed config.
ATR_MULT  = config.ATR_STOP_MULT        # 2.0
ATR_FLOOR = config.ATR_STOP_FLOOR_PCT   # 0.03
ATR_CAP   = config.ATR_STOP_CAP_PCT     # 0.12 (live; was 0.10)
HARD_MAX  = config.HARD_MAX_LOSS_PCT    # 0.14 (live; was 0.12)
TP = config.SWING_TP_PCT                # 0.20 live take-profit (was 0.12)
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


def _alpaca_bars(tickers, start, end):
    """Alpaca SIP, split+div-adjusted daily bars — the SAME feed/convention the
    LIVE system trades on (see data/fetcher.py). Returns {ticker: DataFrame[Open,
    High,Low,Close]} with a tz-naive MIDNIGHT DatetimeIndex so it matches both the
    yfinance shape and the parquet's date keys (searchsorted relies on this).
    Returns {} on any failure → caller falls back to yfinance."""
    import os
    key, sec = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        return {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed, Adjustment
        cli = StockHistoricalDataClient(key, sec)
    except Exception as e:
        print(f"  [alpaca bars] client init failed: {e}")
        return {}
    out, tickers = {}, list(tickers)
    # The basic data plan blocks the most-recent ~15 min of SIP, so a future/today
    # `end` fails the WHOLE request — clamp to a fully-settled boundary (2 days
    # back). This is a wall-clock dependency by necessity (it tracks data RECENCY),
    # but it can't change completed-trade results: every pick's exit window closes
    # well before this boundary, so cross-day re-runs differ only in unused trailing
    # bars. Historical windows already ending < boundary pass through unchanged.
    end = min(pd.Timestamp(end), pd.Timestamp.today().normalize() - pd.Timedelta(days=2))
    def _parse(bdf, req_syms):
        res = {}
        if bdf is None or bdf.empty:
            return res
        multi = isinstance(bdf.index, pd.MultiIndex)
        syms = list(bdf.index.get_level_values(0).unique()) if multi else req_syms[:1]
        for sym in syms:
            try:
                sub = bdf.xs(sym, level=0) if multi else bdf
                sub = sub.rename(columns={"open": "Open", "high": "High",
                                          "low": "Low", "close": "Close"})
                sub = sub[["Open", "High", "Low", "Close"]].copy()
                idx = pd.to_datetime(sub.index)
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_convert("UTC").tz_localize(None)
                sub.index = idx.normalize()             # midnight → match parquet dates
                sub = sub[~sub.index.duplicated(keep="last")].dropna()
                if len(sub) > 30:
                    res[sym] = sub
            except Exception:
                pass
        return res

    def _fetch(req_syms):
        req = StockBarsRequest(symbol_or_symbols=req_syms, timeframe=TimeFrame.Day,
                               start=pd.Timestamp(start), end=end,
                               feed=DataFeed.SIP, adjustment=Adjustment.ALL)
        return _parse(cli.get_stock_bars(req).df, req_syms)

    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        # Alpaca uses dot class-shares ('BF.B'); the universe/parquet use hyphens
        # ('BF-B'). Translate for the request; restore original keys in the output.
        amap = {t.replace("-", "."): t for t in chunk}      # alpaca_sym -> original
        try:
            got = _fetch(list(amap.keys()))
        except Exception as e:
            # One invalid symbol fails the WHOLE batch — retry per symbol so the
            # rest still come from Alpaca instead of dropping to yfinance.
            print(f"  [alpaca bars] chunk {i} batch failed ({str(e)[:50]}); per-symbol retry")
            got = {}
            for asym in amap:
                try:
                    got.update(_fetch([asym]))
                except Exception:
                    pass
        for asym, df in got.items():
            out[amap.get(asym, asym)] = df
    return out


def fetch_ohlc(tickers, dmin, dmax):
    start = (pd.Timestamp(dmin) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(dmax) + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    # Alpaca SIP (fresh, adjusted — the same feed the live system trades on) is
    # PRIMARY so backtests run on the SAME data as live execution; yfinance only
    # fills tickers Alpaca can't return.
    out = _alpaca_bars(tickers, start, end)
    missing = [t for t in tickers if t not in out]
    if missing:
        print(f"  [fetch_ohlc] Alpaca {len(out)}/{len(tickers)} · yfinance for {len(missing)}")
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            try:
                raw = yf.download(chunk, start=start, end=end, progress=False,
                                  auto_adjust=True, group_by="ticker", threads=True)
            except Exception:
                continue
            for t in chunk:
                try:
                    d = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                    d = d[["Open", "High", "Low", "Close"]].dropna()
                    d.index = pd.to_datetime(d.index).tz_localize(None).normalize()  # match Alpaca midnight keys
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
    # No-lookahead guard: if sig_date is missing from the index (data gap/halt),
    # the fallback can point ei at a bar weeks/months after the signal — entering
    # there would use future information. Skip if the entry bar is >5 days off.
    if abs((pd.Timestamp(idx[ei]) - pd.Timestamp(sig_date)).days) > 5:
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
