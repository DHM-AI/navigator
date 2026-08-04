"""
train_uponly.py — DIRECTIONAL SHADOW MODEL trainer (Renato, 2026-06-16).

Builds an UP-ONLY twin of the live XGBoost model and saves it to SEPARATE
artifacts. The live model (xgb_model.pkl) is NEVER touched.

Why: the live model is trained on a two-sided label (a >=+20% move in EITHER
direction within 5 days), but we trade it LONG-only — so a high score means
"big move coming," not "going up." This trains the identical pipeline on an
UP-ONLY label (forward 5-day max close >= +20%) so the model actually predicts
direction for a long book. Validated promising in backtest_directional.py
(>=0.90 long PF 1.50 vs two-sided 1.43); this productionizes it as a SHADOW
for a ~4-week paper run before any promotion.

Faithful twin of model/trainer.py:train() — same 70/10/20 chronological split,
same XGBClassifier hyperparams, same Platt calibrator on the 10% slice, same
save format (model + calibrator pickled separately, feature_names json). The
ONLY differences are (1) the up-only label and (2) the *_uponly output paths.

Run (from market-predictions/, with .venv):
    .venv/bin/python train_uponly.py

Reuses the cached parquet's FEATURES (no slow feature refetch); only fetches
OHLC to recompute the up-only label, exactly as backtest_directional does.
"""
from __future__ import annotations
import json, pickle, warnings
import numpy as np
import pandas as pd
from xgboost import XGBClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, roc_auc_score, brier_score_loss

import config  # noqa: F401  (loads .env / paths)
from backtest_regime import fetch_ohlc, FEATURE_COLS, CACHE
from backtest_directional import add_uponly_label

warnings.filterwarnings("ignore")

# ── Shadow output paths (NEVER the live xgb_model.pkl) ─────────────────────
MODEL_OUT   = "model/saved/xgb_model_uponly.pkl"
CALIB_OUT   = "model/saved/calibrator_uponly.pkl"
FEATS_OUT   = "model/saved/feature_names_uponly.json"


def main() -> None:
    print(f"[uponly] Loading cached features from {CACHE} ...")
    df = pd.read_parquet(CACHE)
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    print(f"[uponly] {len(df):,} feature rows ({df['date'].min().date()} → {df['date'].max().date()})")

    # ── Recompute the UP-ONLY label (forward 5d max close >= +20%) ──────────
    tickers = sorted(df["ticker"].unique())
    print(f"[uponly] Fetching OHLC for {len(tickers)} tickers to relabel up-only ...")
    ohlc = fetch_ohlc(tickers, df["date"].min(), df["date"].max())
    df = add_uponly_label(df, ohlc)
    before = len(df)
    df = df.dropna(subset=["up_label"]).reset_index(drop=True)
    lost = (before - len(df)) / before * 100
    print(f"[uponly] Relabeled. Row loss in merge: {lost:.1f}% "
          f"({before:,} → {len(df):,})")
    df["up_label"] = df["up_label"].astype(int)

    # ── Same chronological 70/10/20 split as trainer.train() ───────────────
    df = df.sort_values("date").reset_index(drop=True)
    X = df[FEATURE_COLS].values
    y = df["up_label"].values
    n = len(df)
    train_end = int(n * 0.70)
    calib_end = int(n * 0.80)
    X_train, X_calib, X_test = X[:train_end], X[train_end:calib_end], X[calib_end:]
    y_train, y_calib, y_test = y[:train_end], y[train_end:calib_end], y[calib_end:]
    print(f"[uponly] Train: {len(X_train):,} | Calib: {len(X_calib):,} | Test: {len(X_test):,}")

    pos_rate = y_train.mean()
    two_sided_rate = df["label"].mean()
    print(f"[uponly] Positive rate (up-only, train): {pos_rate:.3%}  "
          f"(two-sided full: {two_sided_rate:.3%} — up-only ~halves it, as expected)")
    scale_pos_weight = (1 - pos_rate) / pos_rate if pos_rate > 0 else 1.0

    # ── Same XGBoost hyperparams as the live trainer ───────────────────────
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
    print("[uponly] Training XGBoost (up-only label)...")
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=50)

    # ── Evaluate (uncalibrated) ────────────────────────────────────────────
    y_pred = model.predict(X_test)
    y_prob_raw = model.predict_proba(X_test)[:, 1]
    print("\n[uponly] Classification Report (test, uncalibrated):")
    print(classification_report(y_test, y_pred))
    try:
        auc = roc_auc_score(y_test, y_prob_raw)
        print(f"[uponly] AUC-ROC: {auc:.4f}  |  Brier(raw): {brier_score_loss(y_test, y_prob_raw):.4f}")
    except Exception:
        pass

    # ── Platt calibrator on the 10% calib slice (same as trainer) ──────────
    print("[uponly] Fitting Platt scaling on calibration slice...")
    calib_probs_raw = model.predict_proba(X_calib)[:, 1].reshape(-1, 1)
    calibrator = LogisticRegression(solver="lbfgs", max_iter=1000)
    calibrator.fit(calib_probs_raw, y_calib)

    # ── Gate-tuning aid: where does the raw prob put ~the live pick volume? ──
    # The live gate is MIN_XGB_PROB_TO_TRADE on the RAW prob (calibration OFF).
    # Up-only shifts the distribution, so report raw-prob quantiles + how many
    # test rows clear several candidate thresholds — used to pick the shadow gate.
    print("\n[uponly] Raw-prob distribution on test set (for gate re-tuning):")
    for q in (0.90, 0.95, 0.975, 0.99, 0.995, 0.999):
        thr = float(np.quantile(y_prob_raw, q))
        n_above = int((y_prob_raw >= thr).sum())
        hit = float(y_test[y_prob_raw >= thr].mean()) if n_above else 0.0
        print(f"   q{q:<6} thr={thr:.4f}   rows≥thr={n_above:<6}  up_label_rate={hit:.3%}")
    for thr in (0.50, 0.70, 0.80, 0.90):
        n_above = int((y_prob_raw >= thr).sum())
        hit = float(y_test[y_prob_raw >= thr].mean()) if n_above else 0.0
        print(f"   thr={thr:.2f}    rows≥thr={n_above:<6}  up_label_rate={hit:.3%}")

    # ── Save SHADOW artifacts (live model untouched) ───────────────────────
    with open(MODEL_OUT, "wb") as f:
        pickle.dump(model, f)
    with open(CALIB_OUT, "wb") as f:
        pickle.dump(calibrator, f)
    with open(FEATS_OUT, "w") as f:
        json.dump(FEATURE_COLS, f)
    print(f"\n[uponly] ✅ Saved SHADOW model   → {MODEL_OUT}")
    print(f"[uponly] ✅ Saved SHADOW calib   → {CALIB_OUT}")
    print(f"[uponly] ✅ Saved feature names  → {FEATS_OUT}")
    print("[uponly] Live xgb_model.pkl was NOT touched.")


if __name__ == "__main__":
    main()
