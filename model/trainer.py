from __future__ import annotations
"""
Train XGBoost classifier on 5 years of S&P 500 history + Platt-scaling
calibration on a held-out slice.

Run once before first agent scan:
    python -m model.trainer

Saves model artifacts to model/saved/:
    - xgb_model.pkl       (the trained classifier)
    - feature_names.json  (feature order for inference)
    - calibrator.pkl      (LogisticRegression for Platt scaling)
"""
import os
import json
import pickle
import time
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from xgboost import XGBClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import classification_report, roc_auc_score, brier_score_loss
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

from data.universe import get_sp500_tickers, FUTURES
from signals.technicals import compute_feature_row
from config import (
    TRAIN_YEARS, TRAIN_TEST_SPLIT, MOVE_TARGET_PCT,
    MODEL_PATH, FEATURE_NAMES_PATH, CALIBRATOR_PATH,
)

SAVE_DIR = "model/saved"
CACHE_PARQUET = "model/saved/training_data.parquet"
CACHE_PICKLE  = "model/saved/training_data.pkl"   # fallback when parquet engine missing

# ── Directional label parameters (A1 fix, 2026-07-30 forensic) ──────────────
# The label must describe the trade the system ACTUALLY takes: a long, entered
# at the next session's open, held 5 sessions, that has to survive its stop.
# LABEL_STOP_PCT mirrors the live ATR stop clamp (config ATR_STOP_FLOOR_PCT 0.03
# .. ATR_STOP_CAP_PCT 0.12); the midpoint is used as a single representative
# width since per-row ATR is not available at label time. LABEL_COST_PCT is the
# round-trip cost the return must clear to count as a win (~0.2% modeled cost,
# consistent with the backtests' cost assumption).
LABEL_STOP_PCT = 0.08     # stop-hit threshold on the intra-hold low vs entry
LABEL_COST_PCT = 0.002    # net return must beat round-trip costs
# LABEL_HOLD_DAYS must equal the LIVE time exit or the model is trained on a trade the
# system never makes. Was an implicit 5 while CLOSE_ALL_FRIDAY=True truncated holds to a
# measured mean of 2.96 sessions (only 18.7% ever reached 5) — i.e. it never matched
# execution even then. Now tracks config.MAX_HOLD_SESSIONS (21).
try:
    from config import MAX_HOLD_SESSIONS as LABEL_HOLD_DAYS
except Exception:
    LABEL_HOLD_DAYS = 21


def _save_training_cache(df: pd.DataFrame) -> str:
    """Save training data — prefer parquet, fall back to pickle if engine missing."""
    try:
        df.to_parquet(CACHE_PARQUET, index=False)
        return CACHE_PARQUET
    except (ImportError, ValueError) as e:
        print(f"[trainer] parquet save failed ({e}); falling back to pickle")
        df.to_pickle(CACHE_PICKLE)
        return CACHE_PICKLE


def _load_training_cache() -> pd.DataFrame | None:
    """Load training data — try parquet first, then pickle fallback."""
    if os.path.exists(CACHE_PARQUET):
        try:
            return pd.read_parquet(CACHE_PARQUET)
        except Exception as e:
            print(f"[trainer] parquet read failed ({e}); trying pickle")
    if os.path.exists(CACHE_PICKLE):
        return pd.read_pickle(CACHE_PICKLE)
    return None


def _fetch_training_data(tickers: list[str], years: int = 3) -> pd.DataFrame:
    period = f"{years}y"
    print(f"[trainer] Fetching {years}y history for {len(tickers)} tickers...")
    rows = []
    chunk_size = 30

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        pct_done = i / len(tickers) * 100
        print(f"  {pct_done:.0f}% — chunk {i//chunk_size + 1}/{(len(tickers)-1)//chunk_size + 1}")
        try:
            raw = yf.download(
                chunk, period=period, interval="1d",
                progress=False, auto_adjust=True, group_by="ticker"
            )
            for ticker in chunk:
                try:
                    if len(chunk) == 1:
                        df = raw.copy()
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                    else:
                        df = raw[ticker].copy()
                    df = df.dropna(how="all")
                    if df.empty or len(df) < 80:
                        continue
                    df.index = pd.to_datetime(df.index)

                    # Compute features and labels for each row
                    for idx in range(60, len(df) - 6):
                        window = df.iloc[:idx + 1]
                        try:
                            features = compute_feature_row(window)
                        except Exception:
                            continue

                        # ── LABELS ────────────────────────────────────────────
                        # A1 fix (2026-07-30 forensic review). The ONLY label used
                        # to be `label_unsigned` below: 1 if EITHER a +20% or a
                        # -20% excursion occurred. That is a volatility/big-move
                        # detector with NO direction — yet the live system trades
                        # long-only, taking its side from an unrelated RSI/EMA/
                        # sentiment vote in scorer.py. A name flagged precisely
                        # because it was about to fall 20% still got BOUGHT.
                        # Forward result of that mismatch: 45% direction accuracy,
                        # corr(score, move) = -0.05 on 344 traded picks.
                        #
                        # `label` is now the LONG trade we actually take: enter at
                        # the next session's open, hold LABEL_HOLD_DAYS sessions, and
                        # require the net return to clear round-trip costs WITHOUT the
                        # ATR stop being hit first. Modeling the stop matters — a name
                        # that ends +8% after dropping 15% intraday is a LOSS live, and
                        # the old label happily called it a win.
                        future = df["Close"].iloc[idx + 1:idx + 1 + LABEL_HOLD_DAYS]
                        current_close = df["Close"].iloc[idx]
                        if len(future) < LABEL_HOLD_DAYS or current_close == 0:
                            continue
                        max_move = float((future.max() - current_close) / current_close)
                        min_move = float((future.min() - current_close) / current_close)
                        # Legacy unsigned target — kept for A/B comparison only.
                        label_unsigned = 1 if (max_move >= MOVE_TARGET_PCT
                                               or min_move <= -MOVE_TARGET_PCT) else 0

                        # Executable long: next-open entry (what the backtest and
                        # any honest sim can actually fill), held LABEL_HOLD_DAYS.
                        # ⛔ 2026-07-31: horizon 5 -> LABEL_HOLD_DAYS (21). The live exit
                        # contract CHANGED — CLOSE_ALL_FRIDAY went False and MAX_HOLD_SESSIONS
                        # = 21 became the time exit — so a 5-session label no longer describes
                        # any trade the system makes. Training on a horizon the executor does
                        # not trade is the ORIGINAL defect of this system (unsigned ±20% label
                        # vs long-only execution); do not let it back in via the horizon.
                        try:
                            entry = float(df["Open"].iloc[idx + 1])
                        except Exception:
                            continue
                        if entry <= 0:
                            continue
                        exit_close = float(df["Close"].iloc[idx + LABEL_HOLD_DAYS])
                        gross_ret = exit_close / entry - 1.0
                        # Did the intended stop get hit first? Use the LOW over the
                        # hold window vs the same ATR-derived stop the live bracket
                        # would place (floor/cap clamped, mirroring config).
                        try:
                            lows = df["Low"].iloc[idx + 1:idx + 1 + LABEL_HOLD_DAYS]
                            worst = float(lows.min() / entry - 1.0)
                        except Exception:
                            worst = float(min_move)
                        stop_pct = LABEL_STOP_PCT
                        stopped = worst <= -stop_pct
                        label = 1 if (not stopped and gross_ret > LABEL_COST_PCT) else 0

                        row = {"ticker": ticker, "date": df.index[idx], "label": label,
                               "label_unsigned": label_unsigned,
                               "fwd_ret": gross_ret, "fwd_worst": worst}
                        row.update(features)
                        rows.append(row)
                except Exception:
                    continue
        except Exception as e:
            print(f"  [trainer] batch error: {e}")
        time.sleep(0.5)

    df_out = pd.DataFrame(rows)
    print(f"[trainer] Built {len(df_out):,} labeled rows.")
    return df_out


FEATURE_COLS = [
    "bb_width_pct", "bb_squeeze", "atr_ratio", "atr_compression",
    "volume_ratio", "volume_surge", "rsi_value", "rsi_extreme",
    "rsi_bull", "rsi_bear", "ema50_pct",
]


def train(force_refetch: bool = False) -> None:
    os.makedirs(SAVE_DIR, exist_ok=True)

    # Load or build training data — robust to parquet engine missing
    cached = None if force_refetch else _load_training_cache()
    if cached is not None:
        print(f"[trainer] Loaded cached training data ({len(cached):,} rows)")
        data = cached
    else:
        tickers = get_sp500_tickers()
        # Skip futures for training (less history, different dynamics)
        data = _fetch_training_data(tickers, years=TRAIN_YEARS)
        if data.empty:
            print("[trainer] No training data — aborting.")
            return
        saved_to = _save_training_cache(data)
        print(f"[trainer] Cached training data to {saved_to}")

    # Prepare features
    data = data.dropna(subset=FEATURE_COLS + ["label"])
    data = data.sort_values("date").reset_index(drop=True)

    X = data[FEATURE_COLS].values
    y = data["label"].values

    # ── Walk-forward 3-way split (no lookahead) ────────────────────────────
    # train (70%)  → fit XGBoost
    # calib (10%)  → fit Platt scaling on XGB outputs
    # test  (20%)  → final evaluation (calibrated + uncalibrated)
    n = len(data)
    train_end = int(n * 0.70)
    calib_end = int(n * 0.80)
    X_train, X_calib, X_test = X[:train_end], X[train_end:calib_end], X[calib_end:]
    y_train, y_calib, y_test = y[:train_end], y[train_end:calib_end], y[calib_end:]

    print(
        f"[trainer] Train: {len(X_train):,} | Calib: {len(X_calib):,} | Test: {len(X_test):,}"
    )
    pos_rate = y_train.mean()
    print(f"[trainer] Positive rate (train): {pos_rate:.2%}")

    scale_pos_weight = (1 - pos_rate) / pos_rate if pos_rate > 0 else 1.0

    # ── Train XGBoost ──────────────────────────────────────────────────────
    model = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
    )
    print("[trainer] Training XGBoost...")
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=50,
    )

    # ── Evaluate uncalibrated model on test set ────────────────────────────
    y_pred = model.predict(X_test)
    y_prob_raw = model.predict_proba(X_test)[:, 1]
    print("\n[trainer] Classification Report (test set, uncalibrated):")
    print(classification_report(y_test, y_pred))
    try:
        auc = roc_auc_score(y_test, y_prob_raw)
        brier_raw = brier_score_loss(y_test, y_prob_raw)
        print(f"[trainer] AUC-ROC: {auc:.4f}  |  Brier (raw): {brier_raw:.4f}")
    except Exception:
        pass

    # ── Fit Platt scaling on the calibration slice ─────────────────────────
    # Platt = logistic regression mapping raw XGB prob -> calibrated prob.
    # Trained on a SEPARATE slice (calib) so it doesn't leak from train or test.
    print("\n[trainer] Fitting Platt scaling on calibration slice...")
    calib_probs_raw = model.predict_proba(X_calib)[:, 1].reshape(-1, 1)
    calibrator = LogisticRegression(solver="lbfgs", max_iter=1000)
    calibrator.fit(calib_probs_raw, y_calib)

    # Evaluate calibrated probabilities on test set
    test_probs_calibrated = calibrator.predict_proba(y_prob_raw.reshape(-1, 1))[:, 1]
    try:
        brier_cal = brier_score_loss(y_test, test_probs_calibrated)
        print(f"[trainer] Brier (calibrated): {brier_cal:.4f}  "
              f"(lower = better; expect drop from {brier_raw:.4f})")

        # Reliability check: bucket predictions by decile and compare predicted vs actual
        print("\n[trainer] Reliability table (calibrated):")
        print("  bucket   pred_avg   actual_rate   count")
        for lo, hi in [(0.0, 0.3), (0.3, 0.4), (0.4, 0.5),
                       (0.5, 0.6), (0.6, 0.7), (0.7, 1.0)]:
            mask = (test_probs_calibrated >= lo) & (test_probs_calibrated < hi)
            n_b = int(mask.sum())
            if n_b > 0:
                p_avg = test_probs_calibrated[mask].mean()
                a_rate = y_test[mask].mean()
                print(f"  {lo:.2f}-{hi:.2f}   {p_avg:.3f}      {a_rate:.3f}        {n_b:,}")
    except Exception as e:
        print(f"[trainer] Calibration eval skipped: {e}")

    # ── Refit a DEPLOY model on ALL mature rows ────────────────────────────
    # A7 fix (2026-07-30 forensic). We used to pickle `model` — the estimator fit
    # on only the FIRST 70% of time-sorted rows. With calibration disabled at
    # inference, the newest 30% had ZERO influence on live predictions: the July
    # 2026 artifact was last trained on 2025-01-24 data, ~18 months stale, and
    # monthly GENESIS retrains rewrote the file without ever fixing that (each run
    # just re-derived the same 70% boundary from a slightly longer cache).
    # `model` above stays the honest EVALUATION estimator (its test slice is
    # genuinely out-of-sample); what we DEPLOY is a fresh estimator with identical
    # hyperparameters fit on every row whose 5-day label is fully mature.
    print("\n[trainer] Refitting deploy model on all mature rows...")
    deploy_pos_rate = y.mean()
    deploy_model = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=((1 - deploy_pos_rate) / deploy_pos_rate
                          if deploy_pos_rate > 0 else 1.0),
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
    )
    deploy_model.fit(X, y, verbose=False)
    _trained_through = str(pd.to_datetime(data["date"]).max().date())
    print(f"[trainer] Deploy model trained through {_trained_through} "
          f"({len(X):,} rows) vs eval model through "
          f"{pd.to_datetime(data['date']).iloc[train_end-1].date()}")

    # ── Save model + calibrator + feature names ────────────────────────────
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(deploy_model, f)
    with open(CALIBRATOR_PATH, "wb") as f:
        pickle.dump(calibrator, f)
    with open(FEATURE_NAMES_PATH, "w") as f:
        json.dump(FEATURE_COLS, f)
    # Provenance so staleness can never hide behind a fresh file mtime again.
    try:
        with open(os.path.join(SAVE_DIR, "model_metadata.json"), "w") as f:
            json.dump({
                "trained_at": datetime.now().isoformat(),
                "trained_through": _trained_through,
                "rows": int(len(X)),
                "tickers": int(data["ticker"].nunique()),
                "label": ("long next-open, 5-session hold, must clear "
                          f"{LABEL_COST_PCT:.3%} cost without hitting "
                          f"-{LABEL_STOP_PCT:.0%} stop"),
                "label_positive_rate": float(deploy_pos_rate),
                "eval_auc": float(auc) if "auc" in locals() else None,
                "deploy_fit": "all mature rows (not the 70% eval split)",
            }, f, indent=2)
    except Exception as _me:
        print(f"[trainer] metadata write skipped: {_me}")
    print(f"\n[trainer] Saved model      → {MODEL_PATH}")
    print(f"[trainer] Saved calibrator → {CALIBRATOR_PATH}")
    print(f"[trainer] Saved feature names → {FEATURE_NAMES_PATH}")


if __name__ == "__main__":
    import sys
    force = "--refetch" in sys.argv
    train(force_refetch=force)
