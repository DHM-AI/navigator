"""Walk-forward backtest — does the model actually have edge, out of sample?

LAYER 1 (this file, no price fetch): time-honest walk-forward over the cached
training data. The production AUC 0.76 came from a RANDOM 80/20 split, which
leaks time (test rows can predate train rows; same-ticker temporal correlation
inflates the score). This retrains XGBoost using ONLY the past at each step and
scores the next month it has never seen — the honest measure of generalization.

Reports out-of-sample AUC + decile lift: if a high predicted-probability bucket
does NOT show a higher actual big-move rate than a low one, the model has no
real predictive power and no risk management can rescue it.

Usage: python backtest_walkforward.py
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

CACHE = "model/saved/training_data.parquet"
FEATURE_COLS = [
    "bb_width_pct", "bb_squeeze", "atr_ratio", "atr_compression",
    "volume_ratio", "volume_surge", "rsi_value", "rsi_extreme",
    "rsi_bull", "rsi_bear", "ema50_pct",
]
TRAIN_MIN_MONTHS = 16          # initial training history before the first test month
TEST_START = "2023-01"         # first out-of-sample month


def _xgb(scale_pos_weight: float) -> XGBClassifier:
    # Mirror model/trainer.py exactly.
    return XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss", random_state=42, n_jobs=-1,
    )


def main():
    df = pd.read_parquet(CACHE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date").reset_index(drop=True)
    df["ym"] = df["date"].dt.to_period("M")
    months = sorted(m for m in df["ym"].unique() if str(m) >= TEST_START)
    print(f"loaded {len(df):,} rows · {df['ticker'].nunique()} tickers · "
          f"{df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"walk-forward: train on all data before each month, predict that month\n")
    print(f"  {'test month':10} {'train rows':>11} {'test rows':>9} {'OOS AUC':>8} {'base%':>6}")

    oos_prob, oos_label = [], []
    aucs = []
    for m in months:
        tr = df[df["ym"] < m]
        te = df[df["ym"] == m]
        if tr["ym"].nunique() < TRAIN_MIN_MONTHS or te.empty:
            continue
        if tr["label"].sum() < 20 or te["label"].sum() < 1:
            continue
        Xtr, ytr = tr[FEATURE_COLS].values, tr["label"].values
        Xte, yte = te[FEATURE_COLS].values, te["label"].values
        spw = (len(ytr) - ytr.sum()) / max(ytr.sum(), 1)
        model = _xgb(spw)
        model.fit(Xtr, ytr, verbose=False)
        prob = model.predict_proba(Xte)[:, 1]
        oos_prob.extend(prob.tolist())
        oos_label.extend(yte.tolist())
        try:
            a = roc_auc_score(yte, prob)
            aucs.append(a)
        except ValueError:
            a = float("nan")
        print(f"  {str(m):10} {len(tr):>11,} {len(te):>9,} {a:>8.3f} {te['label'].mean()*100:>5.1f}%")

    oos_prob = np.array(oos_prob)
    oos_label = np.array(oos_label)
    print("\n" + "=" * 64)
    overall = roc_auc_score(oos_label, oos_prob)
    print(f"POOLED OUT-OF-SAMPLE AUC: {overall:.4f}   "
          f"(mean monthly {np.nanmean(aucs):.3f})")
    print(f"  0.50 = no skill (coin flip) · 0.55 = faint · 0.60+ = usable")
    print(f"  test rows {len(oos_label):,} · big-move base rate {oos_label.mean()*100:.2f}%")

    # Decile lift: sort OOS predictions into 10 buckets, show actual big-move rate.
    # A working model => top decile rate >> bottom decile rate (monotone).
    order = np.argsort(oos_prob)
    deciles = np.array_split(order, 10)
    base = oos_label.mean()
    print("\nDECILE LIFT (does a higher predicted prob => higher ACTUAL big-move rate?)")
    print(f"  {'decile':8} {'n':>7} {'avg pred':>9} {'actual%':>8} {'lift x base':>11}")
    for i, idx in enumerate(deciles, 1):
        ap = oos_prob[idx].mean()
        ar = oos_label[idx].mean()
        print(f"  {i:>2} {'(low)' if i==1 else '(high)' if i==10 else '':6} "
              f"{len(idx):>7,} {ap:>9.4f} {ar*100:>7.2f}% {ar/base if base else 0:>10.2f}x")

    top, bot = oos_label[deciles[-1]].mean(), oos_label[deciles[0]].mean()
    print(f"\n  top-decile vs bottom-decile big-move rate: {top*100:.2f}% vs {bot*100:.2f}% "
          f"= {top/bot if bot else float('inf'):.1f}x")
    verdict = ("MODEL GENERALIZES — real predictive power out of sample" if overall >= 0.58
               else "MARGINAL — weak but non-zero signal" if overall >= 0.54
               else "NO OUT-OF-SAMPLE EDGE — the model does not generalize")
    print(f"\n  LAYER-1 VERDICT: {verdict}")
    print("  (Layer 1 tests the classifier on its OWN ±20%-move label. If this is")
    print("   coin-flip, there is nothing for risk management or direction logic to")
    print("   work with. If it shows lift, Layer 2 measures whether it makes money.)")


if __name__ == "__main__":
    main()
