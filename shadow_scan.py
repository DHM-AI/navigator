"""
shadow_scan.py — DIRECTIONAL SHADOW paper-scan (Renato, 2026-06-16).

Runs the UP-ONLY shadow model (train_uponly.py) ALONGSIDE the live two-sided
model on the same universe, every scan, and LOGS what each would pick. It
places NO trades, writes NO Supabase rows, touches NO Alpaca account. Pure
observation so we can compare the two models' picks forward (~4 weeks) and
decide whether the directional model earns promotion to live.

Design — fully self-contained, fair head-to-head:
  • loads BOTH models (live xgb_model.pkl + shadow xgb_model_uponly.pkl);
    they share the identical 11-feature vector, so one feature row scores both.
  • scans the live equity universe (get_universe minus FUTURES, minus crypto).
  • for each ticker: live_prob, up_prob, near_high_pct, last close.
  • applies the SAME gates the live engine uses (prob ≥ gate, not within
    NEAR_HIGH_BAND of the 10-day high, price ≥ MIN_PRICE) to mark would_trade
    for each model — but logs EVERY scored ticker above a low floor so the gate
    can be re-tuned later from the logged data, not just at scan time.
  • appends one JSON line per logged ticker to model/saved/shadow_picks.jsonl.

Gate note: on the walk-forward test slice, up-only ≥0.90 reproduces ~the live
≥0.90 pick volume (861 vs 991 rows), so the shadow gate is kept at 0.90 for a
clean comparison. shadow_compare.py re-derives edge at any threshold from logs.

Run (from market-predictions/, with .venv):
    .venv/bin/python shadow_scan.py
"""
from __future__ import annotations
import os, json, pickle, warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config as c
from data.universe import get_universe, is_crypto
from data.fetcher import get_ohlcv_batch
from signals.technicals import compute_feature_row

warnings.filterwarnings("ignore")

LIVE_MODEL  = "model/saved/xgb_model.pkl"
UP_MODEL    = "model/saved/xgb_model_uponly.pkl"
FEATS_PATH  = "model/saved/feature_names.json"          # identical for both
LOG_PATH    = "model/saved/shadow_picks.jsonl"

UP_GATE     = float(os.getenv("SHADOW_UP_GATE", "0.90"))   # matched to live ≥0.90 volume
LIVE_GATE   = float(getattr(c, "MIN_XGB_PROB_TO_TRADE", 0.90))
LOG_FLOOR   = float(os.getenv("SHADOW_LOG_FLOOR", "0.50"))  # log any ticker either model likes
NH_BAND     = float(getattr(c, "NEAR_HIGH_BAND_PCT", 2.0))
NH_ON       = bool(getattr(c, "ENABLE_NEAR_HIGH_FILTER", True))
MIN_PRICE   = float(getattr(c, "MIN_PRICE", 20.0))


def _near_high_pct(df: pd.DataFrame) -> float:
    """% of last close vs the PRIOR 10-day high (excludes the current bar) —
    same definition as the engine scorer + dashboard. ≥11 bars required."""
    if len(df) < 11:
        return -99.0
    prior_high = float(df["High"].iloc[-11:-1].max())
    if prior_high <= 0:
        return -99.0
    return (float(df["Close"].iloc[-1]) / prior_high - 1) * 100


def main() -> None:
    for p in (LIVE_MODEL, UP_MODEL):
        if not os.path.exists(p):
            raise SystemExit(f"[shadow] missing model {p} — run train_uponly.py first")
    live = pickle.load(open(LIVE_MODEL, "rb"))
    up   = pickle.load(open(UP_MODEL, "rb"))
    feats = json.load(open(FEATS_PATH))

    tickers = [t for t in get_universe() if t not in c.FUTURES and not is_crypto(t)]
    print(f"[shadow] scanning {len(tickers)} equities  (live gate {LIVE_GATE} / up gate {UP_GATE})")
    ohlcv = get_ohlcv_batch(tickers, period="1y", chunk_size=50)

    scan_ts = datetime.now(timezone.utc).isoformat()
    scan_day = scan_ts[:10]
    rows, n_live, n_up = [], 0, 0
    for tk in tickers:
        df = ohlcv.get(tk)
        if df is None or len(df) < 60:
            continue
        try:
            fr = compute_feature_row(df)
            X = np.array([[float(fr.get(col, 0.0) or 0.0) for col in feats]])
            lp = float(live.predict_proba(X)[0][1])
            upp = float(up.predict_proba(X)[0][1])
        except Exception:
            continue
        if max(lp, upp) < LOG_FLOOR:
            continue
        nh = _near_high_pct(df)
        last = float(df["Close"].iloc[-1])
        nh_ok = (not NH_ON) or (nh <= -NH_BAND)
        price_ok = last >= MIN_PRICE
        wt_live = bool(lp >= LIVE_GATE and nh_ok and price_ok)
        wt_up = bool(upp >= UP_GATE and nh_ok and price_ok)
        n_live += wt_live
        n_up += wt_up
        rows.append({
            "scan_ts": scan_ts, "date": scan_day, "ticker": tk,
            "live_prob": round(lp, 4), "up_prob": round(upp, 4),
            "near_high_pct": round(nh, 2), "last_close": round(last, 2),
            "would_trade_live": wt_live, "would_trade_up": wt_up,
        })

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    live_picks = sorted((r for r in rows if r["would_trade_live"]), key=lambda r: -r["live_prob"])
    up_picks   = sorted((r for r in rows if r["would_trade_up"]),   key=lambda r: -r["up_prob"])
    live_set, up_set = {r["ticker"] for r in live_picks}, {r["ticker"] for r in up_picks}
    print(f"[shadow] logged {len(rows)} rows ≥{LOG_FLOOR} → {LOG_PATH}")
    print(f"[shadow] would-trade  LIVE={n_live}  UP-ONLY={n_up}  "
          f"shared={len(live_set & up_set)}  up-only-only={len(up_set - live_set)}  "
          f"live-only={len(live_set - up_set)}")
    print(f"[shadow] LIVE picks:    {[r['ticker'] for r in live_picks][:15]}")
    print(f"[shadow] UP-ONLY picks: {[r['ticker'] for r in up_picks][:15]}")


if __name__ == "__main__":
    main()
