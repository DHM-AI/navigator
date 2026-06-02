"""
Technical signal computation — uses the `ta` library (no numba/llvmlite dependency).
"""
import pandas as pd
import numpy as np
from config import (BB_SQUEEZE_PERCENTILE, ATR_COMPRESSION_RATIO,
                    VOLUME_SURGE_RATIO, RSI_OVERBOUGHT, RSI_OVERSOLD)

try:
    from ta.momentum import RSIIndicator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.trend import EMAIndicator
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False


def _require_min_rows(df: pd.DataFrame, n: int) -> bool:
    return len(df) >= n


# ── Pure-pandas fallbacks ─────────────────────────────────────────────────────

def _ema_pandas(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi_pandas(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _bollinger_pandas(series: pd.Series, window: int = 20) -> tuple:
    mid = series.rolling(window).mean()
    std = series.rolling(window).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    width = (upper - lower) / mid.replace(0, np.nan)
    return upper, mid, lower, width


def _atr_pandas(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


# ── Signal functions ──────────────────────────────────────────────────────────

def bb_squeeze(df: pd.DataFrame, window: int = 50) -> dict:
    if not _require_min_rows(df, window + 20):
        return {"triggered": False, "width_percentile": 50.0, "width": None}
    try:
        close = df["Close"]
        if _TA_AVAILABLE:
            bb = BollingerBands(close, window=20, window_dev=2)
            upper = bb.bollinger_hband()
            mid   = bb.bollinger_mavg()
            lower = bb.bollinger_lband()
            width = ((upper - lower) / mid.replace(0, np.nan)).dropna()
        else:
            _, _, _, width = _bollinger_pandas(close)
            width = width.dropna()

        if len(width) < window:
            return {"triggered": False, "width_percentile": 50.0, "width": None}

        current = float(width.iloc[-1])
        rolling = width.iloc[-window:]
        pct = float((rolling <= current).mean() * 100)
        return {"triggered": pct <= BB_SQUEEZE_PERCENTILE,
                "width_percentile": round(pct, 1),
                "width": round(current, 4)}
    except Exception:
        return {"triggered": False, "width_percentile": 50.0, "width": None}


def atr_compression(df: pd.DataFrame, atr_period: int = 14,
                    avg_period: int = 50) -> dict:
    # "pct" = raw ATR as a % of price (volatility level). Distinct from "ratio"
    # (current ATR vs its own 50-day avg = compression). pct drives the
    # volatility filter + ATR-based stop sizing; ratio drives the squeeze signal.
    if not _require_min_rows(df, avg_period + atr_period):
        return {"triggered": False, "ratio": 1.0, "pct": 0.0}
    try:
        if _TA_AVAILABLE:
            atr = AverageTrueRange(df["High"], df["Low"], df["Close"],
                                   window=atr_period).average_true_range().dropna()
        else:
            atr = _atr_pandas(df["High"], df["Low"], df["Close"],
                              period=atr_period).dropna()

        if len(atr) < avg_period:
            return {"triggered": False, "ratio": 1.0, "pct": 0.0}
        current_atr = float(atr.iloc[-1])
        avg_atr = float(atr.iloc[-avg_period:].mean())
        price   = float(df["Close"].iloc[-1])
        atr_pct = (current_atr / price) if price else 0.0
        if avg_atr == 0:
            return {"triggered": False, "ratio": 1.0, "pct": round(atr_pct, 4)}
        ratio = current_atr / avg_atr
        return {"triggered": ratio < ATR_COMPRESSION_RATIO,
                "ratio": round(ratio, 3), "pct": round(atr_pct, 4)}
    except Exception:
        return {"triggered": False, "ratio": 1.0, "pct": 0.0}


def volume_surge(df: pd.DataFrame, avg_period: int = 20) -> dict:
    if not _require_min_rows(df, avg_period + 1):
        return {"triggered": False, "ratio": 1.0}
    try:
        vol = df["Volume"].dropna()
        today_vol = float(vol.iloc[-1])
        avg_vol = float(vol.iloc[-(avg_period + 1):-1].mean())
        if avg_vol == 0:
            return {"triggered": False, "ratio": 1.0}
        ratio = today_vol / avg_vol
        return {"triggered": ratio >= VOLUME_SURGE_RATIO, "ratio": round(ratio, 2)}
    except Exception:
        return {"triggered": False, "ratio": 1.0}


def rsi_signal(df: pd.DataFrame, period: int = 14) -> dict:
    if not _require_min_rows(df, period + 5):
        return {"value": 50.0, "extreme": False, "side": "neutral"}
    try:
        if _TA_AVAILABLE:
            rsi = RSIIndicator(df["Close"], window=period).rsi().dropna()
        else:
            rsi = _rsi_pandas(df["Close"], period=period).dropna()

        if rsi.empty:
            return {"value": 50.0, "extreme": False, "side": "neutral"}
        val = float(rsi.iloc[-1])
        if val >= RSI_OVERBOUGHT:
            return {"value": round(val, 1), "extreme": True, "side": "bear"}
        if val <= RSI_OVERSOLD:
            return {"value": round(val, 1), "extreme": True, "side": "bull"}
        return {"value": round(val, 1), "extreme": False, "side": "neutral"}
    except Exception:
        return {"value": 50.0, "extreme": False, "side": "neutral"}


def price_vs_ema(df: pd.DataFrame, period: int = 50) -> dict:
    if not _require_min_rows(df, period):
        return {"ema50_pct": 0.0}
    try:
        if _TA_AVAILABLE:
            ema = EMAIndicator(df["Close"], window=period).ema_indicator().dropna()
        else:
            ema = _ema_pandas(df["Close"], period).dropna()

        if ema.empty:
            return {"ema50_pct": 0.0}
        ema_val = float(ema.iloc[-1])
        price   = float(df["Close"].iloc[-1])
        pct = (price - ema_val) / ema_val * 100 if ema_val != 0 else 0.0
        return {"ema50_pct": round(pct, 2)}
    except Exception:
        return {"ema50_pct": 0.0}


def compute_all(df: pd.DataFrame) -> dict:
    return {
        "bb":  bb_squeeze(df),
        "atr": atr_compression(df),
        "vol": volume_surge(df),
        "rsi": rsi_signal(df),
        "ema": price_vs_ema(df),
    }


def compute_feature_row(df: pd.DataFrame) -> dict:
    """Flat feature dict for ML training/inference."""
    bb  = bb_squeeze(df)
    atr = atr_compression(df)
    vol = volume_surge(df)
    rsi = rsi_signal(df)
    ema = price_vs_ema(df)
    return {
        "bb_width_pct":    bb["width_percentile"],
        "bb_squeeze":      int(bb["triggered"]),
        "atr_ratio":       atr["ratio"],
        "atr_compression": int(atr["triggered"]),
        "volume_ratio":    vol["ratio"],
        "volume_surge":    int(vol["triggered"]),
        "rsi_value":       rsi["value"],
        "rsi_extreme":     int(rsi["extreme"]),
        "rsi_bull":        int(rsi["side"] == "bull"),
        "rsi_bear":        int(rsi["side"] == "bear"),
        "ema50_pct":       ema["ema50_pct"],
    }
