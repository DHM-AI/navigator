from __future__ import annotations
import os
import json
import pickle
import pandas as pd
import numpy as np
from signals.technicals import compute_feature_row
from signals.sentiment import normalize_score
from signals.scorer import score_universe as rule_score_universe
from config import (
    MODEL_PATH, FEATURE_NAMES_PATH, CALIBRATOR_PATH, ENABLE_CALIBRATION,
    XGB_WEIGHT, SENTIMENT_WEIGHT, MIN_SCORE_TO_ALERT, MIN_PRICE,
)

_model = None
_calibrator = None
_feature_names: list[str] = []
_model_consistency_error: str | None = None   # set at load; checked loudly in predict_universe
_xgb_calls = 0     # inference attempts this scan
_xgb_fails = 0     # inference exceptions this scan (model-level failure signal)


def _load_model():
    """Lazy-load the XGB model, the Platt-scaling calibrator, and feature names."""
    global _model, _calibrator, _feature_names, _model_consistency_error
    if _model is not None:
        return _model
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH, "rb") as f:
        _model = pickle.load(f)
    if os.path.exists(FEATURE_NAMES_PATH):
        with open(FEATURE_NAMES_PATH) as f:
            _feature_names = json.load(f)
    # Audit H1 (2026-06-02): a model whose feature count disagrees with
    # feature_names.json silently produces WRONG predictions (mismatched columns)
    # that still drive the auto-exec gate + Kelly sizing. Record any mismatch so
    # predict_universe can fail LOUDLY instead of inferring on garbage. (Stored,
    # not raised here, because _xgb_prob's per-ticker try/except would swallow a
    # raise into silent zeros — the exact failure mode we're closing.)
    _nfeat = getattr(_model, "n_features_in_", None)
    if _nfeat is not None and _feature_names and _nfeat != len(_feature_names):
        _model_consistency_error = (
            f"Model expects {_nfeat} features but feature_names.json has "
            f"{len(_feature_names)} — mismatched columns. Regenerate feature_names "
            f"on every retrain.")
    elif _nfeat is not None and not _feature_names:
        _model_consistency_error = (
            "Model loaded but feature_names.json missing/empty — cannot build the "
            "feature vector.")
    else:
        _model_consistency_error = None
    # Load calibrator if present + enabled. Missing calibrator just means we
    # fall back to raw XGB probabilities (no error).
    if ENABLE_CALIBRATION and os.path.exists(CALIBRATOR_PATH):
        try:
            with open(CALIBRATOR_PATH, "rb") as f:
                _calibrator = pickle.load(f)
            print(f"[predictor] Loaded Platt calibrator → {CALIBRATOR_PATH}")
        except Exception as e:
            print(f"[predictor] Calibrator load failed: {e} (falling back to raw)")
            _calibrator = None
    return _model


def model_available() -> bool:
    return os.path.exists(MODEL_PATH)


def calibrator_available() -> bool:
    _load_model()  # ensure load attempted
    return _calibrator is not None


def _xgb_prob(df: pd.DataFrame) -> float:
    """Return calibrated probability if calibrator is loaded, else raw XGB prob."""
    global _xgb_calls, _xgb_fails
    model = _load_model()
    if model is None or df.empty:
        return 0.0
    if not _feature_names:
        return 0.0
    _xgb_calls += 1
    try:
        features = compute_feature_row(df)
        row = [features.get(col, 0.0) for col in _feature_names]
        X = np.array(row).reshape(1, -1)
        raw_prob = float(model.predict_proba(X)[0][1])

        # Platt scaling — maps raw XGB prob to calibrated probability.
        # Without this, "0.72" doesn't mean 72% win rate.
        if _calibrator is not None:
            cal_prob = float(_calibrator.predict_proba(np.array([[raw_prob]]))[0][1])
            return max(0.0, min(1.0, cal_prob))

        return max(0.0, min(1.0, raw_prob))
    except Exception:
        # Audit H2: still return 0 (per-ticker safety) but COUNT it — a model-level
        # failure shows up as a high fail ratio and is alerted in predict_universe,
        # so a broken model no longer looks identical to a quiet market.
        _xgb_fails += 1
        return 0.0


def predict_universe(
    tickers: list[str],
    ohlcv_map: dict[str, pd.DataFrame],
    sentiment_map: dict[str, dict] | None = None,
    earnings_map: dict[str, int | None] | None = None,
) -> pd.DataFrame:
    """
    Score all tickers. Uses XGBoost if model is trained, otherwise falls back
    to rule-based scorer. Blends XGB probability with live sentiment.
    """
    use_xgb = model_available()

    if not use_xgb:
        print("[predictor] No trained model found — using rule-based scorer.")
        return rule_score_universe(tickers, ohlcv_map, sentiment_map, earnings_map)

    from signals.scorer import score_ticker  # for direction/duration/signals logic

    # Audit H1: fail LOUD on a model/feature mismatch instead of silently scoring
    # everything on mismatched columns. _load_model populates the flag.
    global _xgb_calls, _xgb_fails
    _xgb_calls = 0
    _xgb_fails = 0
    _load_model()
    if _model_consistency_error:
        msg = f"PYTHIA model consistency FAILURE: {_model_consistency_error}"
        print(f"[PYTHIA] 🚨 {msg}")
        try:
            from alerts.slack import _post
            _post({"text": f"🚨 *PYTHIA model broken* — {_model_consistency_error} "
                           f"Scan aborted; no trades on mismatched model."})
        except Exception:
            pass
        raise RuntimeError(msg)

    rows = []
    skipped_errors = 0
    for ticker in tickers:
        # CRITICAL audit C-9: one ticker with a corrupted bar / split-adjusted
        # NaN / rename used to crash the WHOLE scan loop. Per-ticker try/except
        # contains the blast radius — at worst we drop that one ticker.
        try:
            df = ohlcv_map.get(ticker, pd.DataFrame())
            sentiment = (sentiment_map or {}).get(ticker, {})
            earnings = (earnings_map or {}).get(ticker)

            if df is None or df.empty:
                continue

            # Finding #24 (2026-06-08): mirror score_universe's penny-stock skip on
            # the LIVE model path. score_universe skips sub-$MIN_PRICE names but the
            # live agent runs predict_universe, which had no such filter — junk got
            # scored/ranked and could eat a TOP_N slot. Skip BEFORE the expensive
            # _xgb_prob/model work. (Hard exec gate in alpaca.py is the 2nd layer.)
            try:
                if float(df["Close"].iloc[-1]) < MIN_PRICE:
                    continue
            except Exception:
                pass

            # Get XGB probability (0-1)
            xgb_prob = _xgb_prob(df)

            # Normalize sentiment score to 0-1, clamped to prevent overflow
            sent_score_norm = max(0.0, min(1.0, normalize_score(sentiment.get("score", 0.0))))

            # Blend: weighted average → 0-100 scale
            blended = (XGB_WEIGHT * xgb_prob + SENTIMENT_WEIGHT * sent_score_norm) * 100

            if blended < MIN_SCORE_TO_ALERT:
                continue

            # Re-use rule scorer for direction/duration/signals labels (no re-fetch)
            meta = score_ticker(ticker, df, sentiment, earnings)
            meta["score"] = round(blended, 1)
            meta["xgb_prob"] = round(xgb_prob, 4)        # calibrated if available
            meta["calibrated"] = calibrator_available()  # transparency flag

            # Override confidence using the BLENDED score, not the raw rule score.
            # scorer.py sets confidence from raw_score (rule signals only), which
            # always reads "Low" in bull trends when no squeeze patterns fire.
            # The blended XGB score is the authoritative confidence signal.
            #   ≥ 80  → High   (model strongly confident)
            #   ≥ 65  → Medium (model moderately confident)
            #   < 65  → Low
            blended_score = meta["score"]
            if blended_score >= 80:
                meta["confidence"] = "High"
            elif blended_score >= 65:
                meta["confidence"] = "Medium"
            else:
                meta["confidence"] = "Low"

            rows.append(meta)
        except Exception as _e:
            skipped_errors += 1
            if skipped_errors <= 5:    # avoid log spam on systemic failure
                print(f"[PYTHIA] {ticker} skipped: {type(_e).__name__}: {_e}")

    if skipped_errors > 5:
        print(f"[PYTHIA] ...and {skipped_errors - 5} more tickers skipped silently.")

    # Audit H2: if model inference failed on a large fraction of tickers, the
    # model (not the market) is broken — alert loudly instead of reporting a
    # quiet "no setups" day driven by all-zero probabilities.
    if _xgb_calls >= 20 and _xgb_fails / _xgb_calls > 0.5:
        ratio = _xgb_fails / _xgb_calls
        print(f"[PYTHIA] 🚨 Model inference FAILED on {_xgb_fails}/{_xgb_calls} "
              f"tickers ({ratio:.0%}) — model likely broken, not a quiet market.")
        try:
            from alerts.slack import _post
            _post({"text": f"🚨 *PYTHIA model failing* — XGB inference errored on "
                           f"{ratio:.0%} of {_xgb_calls} tickers this scan. "
                           f"Predictions are defaulting to sentiment-only. Check the model."})
        except Exception:
            pass

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    return result
