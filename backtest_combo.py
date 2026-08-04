"""Combo backtest — does (≥0.90 conviction + SPY-uptrend gate + wider/run TP)
beat the CURRENT account-2 config (≥0.80, +20% TP + trail)?

Reuses the proven walk-forward + OHLC engine. The regime gate uses ONLY data
known at entry (SPY close vs its 200-day SMA on the SIGNAL date) — no lookahead.
Each lever is added cumulatively so its marginal effect is visible.

Outputs per config:
  • per-trade edge   : n, avg net%, win%, profit factor   (the trustworthy signal)
  • portfolio sim    : 12% sizing, max 12 concurrent, compounded on realized exits
                       → total return, CAGR, max DD, monthly-return profile
                       (REALIZED-ONLY approximation — see caveats printed at end)

Usage:  python backtest_combo.py           # full (2023-01 → data end)
        SMOKE=1 python backtest_combo.py   # fast recent-window sanity run
"""
from __future__ import annotations
import os, warnings, collections
import numpy as np
import pandas as pd
import yfinance as yf
from xgboost import XGBClassifier

import config  # noqa: F401
from backtest_regime import fetch_ohlc, atr_pct_at, CACHE, FEATURE_COLS

warnings.filterwarnings("ignore")

# Exit constants pulled from the LIVE config (config.py) so the backtest can't drift
# from what the system actually trades (Codex review 2026-06-30 — these were hardcoded
# 0.10/0.12, stale vs the live 0.12/0.14).
ATR_MULT  = config.ATR_STOP_MULT        # 2.0
ATR_FLOOR = config.ATR_STOP_FLOOR_PCT   # 0.03
ATR_CAP   = config.ATR_STOP_CAP_PCT     # 0.12 (live; was 0.10)
HARD_MAX  = config.HARD_MAX_LOSS_PCT    # 0.14 (live; was 0.12)
COST = 0.20                      # round-trip cost %, same as prior backtests
TRAIN_MIN_MONTHS = 16
SMOKE = os.environ.get("SMOKE") == "1"
TEST_START = "2025-06" if SMOKE else "2023-01"


# ── walk-forward OOS probs (inlined so SMOKE can use a recent start) ───────────
def walk_forward_oos(df, start):
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
        out.append(te[["ticker", "date", "prob"]])
        print(f"  oos {m}: {len(te):,} rows")
    return pd.concat(out, ignore_index=True)


# ── SPY regime, computed with trailing data only (no lookahead) ───────────────
def build_spy_regime():
    # SPY from Alpaca SIP (adjusted) — the same feed the live system trades on;
    # yfinance fallback if Alpaca is unavailable. 200d SMA, trailing only (no LA).
    from backtest_regime import _alpaca_bars
    a = _alpaca_bars(["SPY"], "2021-01-01", "2026-07-01")
    if a.get("SPY") is not None and len(a["SPY"]) > 250:
        c = a["SPY"]["Close"].dropna()
        src = "alpaca-sip"
    else:
        spy = yf.download("SPY", start="2021-01-01", end="2026-07-01",
                          progress=False, auto_adjust=True)
        c = spy["Close"]
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        c = c.dropna()
        c.index = pd.to_datetime(c.index).tz_localize(None)
        src = "yfinance"
    print(f"  [spy regime] source={src}, {len(c)} bars")
    return pd.DataFrame({"close": c,
                         "sma50": c.rolling(50).mean(),
                         "sma200": c.rolling(200).mean()})


def spy_uptrend_at(reg, sig_date, mode):
    """SPY state as of the last session <= sig_date (entry-time knowable)."""
    sub = reg[reg.index <= sig_date]
    if sub.empty:
        return False
    r = sub.iloc[-1]
    if pd.isna(r["sma200"]):
        return False
    if mode == "none":
        return True
    if mode == "sma200":
        return bool(r["close"] > r["sma200"])
    if mode == "strict":
        return bool(r["close"] > r["sma50"] and r["sma50"] > r["sma200"])
    return True


# ── trade simulation: ATR stop unchanged; parameterized TP + optional trail ───
def simulate(d, sig_date, tp, trail_pct, hold):
    idx = d.index
    p = idx.searchsorted(sig_date)
    if p >= len(idx) or idx[p] != sig_date:
        p = idx.searchsorted(sig_date) - 1
    if p < 1:
        return None
    ei = p + 1
    if ei >= len(idx) - 2:
        return None
    # No-lookahead guard: if sig_date is missing (data gap/halt) the fallback can
    # point ei at a bar weeks after the signal — entering there uses future info.
    if abs((pd.Timestamp(idx[ei]) - pd.Timestamp(sig_date)).days) > 5:
        return None
    entry = float(d["Open"].iloc[ei])
    if entry <= 0:
        return None
    stop = max(entry * (1 - min(max(ATR_MULT * atr_pct_at(d, p), ATR_FLOOR), ATR_CAP)),
               entry * (1 - HARD_MAX))
    tp_abs = entry * (1 + tp) if tp is not None else None
    hi = entry
    for j in range(ei, min(ei + hold, len(idx))):
        o = float(d["Open"].iloc[j]); h = float(d["High"].iloc[j]); l = float(d["Low"].iloc[j])
        if o <= stop:                                   # gap through stop
            return (o / entry - 1) * 100, idx[ei], idx[j]
        if l <= stop:                                   # intraday stop
            return (stop / entry - 1) * 100, idx[ei], idx[j]
        if tp_abs is not None and h >= tp_abs:          # take profit
            return (max(o, tp_abs) / entry - 1) * 100, idx[ei], idx[j]
        if trail_pct is not None:                       # ratchet trail off new high
            hi = max(hi, h)
            stop = max(stop, hi * (1 - trail_pct))
    j = min(ei + hold - 1, len(idx) - 1)
    return (float(d["Close"].iloc[j]) / entry - 1) * 100, idx[ei], idx[j]


# ── portfolio sim: 12% per slot, max 12 concurrent, compound on realized exits ─
def portfolio_sim(trades, size_pct=0.12, max_pos=12):
    if not trades:
        return None
    entries_by_day = collections.defaultdict(list)
    for t in trades:
        entries_by_day[t["entry"]].append(t)
    start = min(t["entry"] for t in trades)
    end = max(t["exit"] for t in trades)
    days = pd.bdate_range(start, end)
    equity = 1.0
    open_pos = []
    curve = []
    for day in days:
        keep = []
        for pos in open_pos:
            if pos["exit"] <= day:
                equity += pos["capital"] * (pos["net"] / 100.0)
            else:
                keep.append(pos)
        open_pos = keep
        for t in sorted(entries_by_day.get(day, []), key=lambda x: -x.get("prob", 0)):
            if len(open_pos) >= max_pos:
                break
            open_pos.append({"exit": t["exit"], "capital": equity * size_pct, "net": t["net"]})
        curve.append((day, equity))
    eq = pd.Series(dict(curve))
    monthly = eq.resample("ME").last().pct_change().dropna() * 100
    roll_max = eq.cummax()
    max_dd = ((eq - roll_max) / roll_max).min() * 100
    yrs = max((end - start).days / 365.25, 1e-9)
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    return {"total_ret": (eq.iloc[-1] - 1) * 100, "cagr": cagr, "max_dd": max_dd,
            "monthly": monthly, "n_months": len(monthly)}


def edge(x):
    if len(x) == 0:
        return "n=0"
    gp = x[x > 0].sum(); gl = x[x <= 0].sum()
    pf = gp / abs(gl) if gl else float("inf")
    return f"n={len(x):>5}  avg {x.mean():+.2f}%  win {(x>0).mean()*100:>3.0f}%  PF {pf:.2f}"


def run_config(name, picks, ohlc, reg, thresh, gate, tp, trail_pct, hold):
    sel = picks[picks["prob"] >= thresh]
    trades = []
    for _, r in sel.iterrows():
        if not spy_uptrend_at(reg, r["date"], gate):
            continue
        d = ohlc.get(r["ticker"])
        if d is None:
            continue
        res = simulate(d, r["date"], tp, trail_pct, hold)
        if res is None:
            continue
        ret, edate, xdate = res
        trades.append({"net": ret - COST, "entry": edate, "exit": xdate, "prob": r["prob"]})
    nets = pd.Series([t["net"] for t in trades])
    port = portfolio_sim(trades)
    print("\n" + "─" * 72)
    print(f"  {name}")
    print(f"    thresh≥{thresh} · gate={gate} · TP={tp} · trail={trail_pct} · hold={hold}d")
    print(f"    EDGE/trade : {edge(nets)}")
    if port:
        m = port["monthly"]
        print(f"    PORTFOLIO  : total {port['total_ret']:+.0f}% · CAGR {port['cagr']:+.0f}% · "
              f"maxDD {port['max_dd']:.0f}% · {port['n_months']} months")
        print(f"    MONTHLY    : mean {m.mean():+.2f}% · median {m.median():+.2f}% · "
              f">+10%: {(m>=10).mean()*100:.0f}% of months · worst {m.min():+.1f}% · best {m.max():+.1f}%")
    return {"name": name, "nets": nets, "port": port}


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    print(f"walk-forward OOS (start {TEST_START}, SMOKE={SMOKE})...")
    oos = walk_forward_oos(df, TEST_START)
    print(f"OOS rows: {len(oos):,} | prob ≥0.80: {(oos['prob']>=0.80).sum():,} | "
          f"≥0.90: {(oos['prob']>=0.90).sum():,}")
    picks = oos[oos["prob"] >= 0.80].copy()          # superset; configs re-filter
    if SMOKE:
        picks = picks.sample(min(len(picks), 1200), random_state=1)
    reg = build_spy_regime()
    print("fetching pick OHLC...")
    ohlc = fetch_ohlc(sorted(picks["ticker"].unique()), picks["date"].min(), picks["date"].max())

    # Anchor #1 reflects the ACTUAL live config (Codex review 2026-06-30): TP from
    # config, trailing OFF (live), hold ~5 = the weekly/holiday flatten cap. Configs
    # 2-5 are exploratory "what-if" variants and keep their own params on purpose.
    _live_trail = None if not config.ENABLE_TRAILING_STOP else 0.03
    configs = [
        # name, thresh, gate, tp, trail_pct, hold
        ("1) CURRENT live (≥0.80, +20% TP, NO trail, ~weekly flatten)", 0.80, "none", config.SWING_TP_PCT, _live_trail, 5),
        ("2) + concentrate (≥0.90 only)",     0.90, "none",   config.SWING_TP_PCT, _live_trail, 5),
        ("3) + SPY-uptrend gate",             0.90, "sma200", config.SWING_TP_PCT, _live_trail, 5),
        ("4) + wider TP (+35%, longer hold)", 0.90, "sma200", 0.35, 0.03, 15),
        ("5) run winners (trail-only)",       0.90, "sma200", None, 0.06, 20),
    ]
    results = [run_config(*([c[0], picks, ohlc, reg] + list(c[1:]))) for c in configs]

    print("\n" + "=" * 72)
    print("CAVEATS: walk-forward is 2023→2025 (mostly bull). Portfolio sim compounds on")
    print("REALIZED exits only (no daily mark-to-market) so monthly figures are lumpy.")
    print("Costs 0.20%/round-trip. Next-open entry / daily-bar exits. Gate uses SPY vs")
    print("200d SMA known at entry. ≥0.90 is the live conviction tier.")
    return results


if __name__ == "__main__":
    main()
