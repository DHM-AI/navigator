from __future__ import annotations
import pandas as pd
from signals.technicals import compute_all
from signals.sentiment import get_sentiment_with_velocity, normalize_score
from signals.patterns import detect_patterns
from signals.insider import get_insider_signal
from signals.analyst import get_analyst_signal
from signals.weekly_trend import get_weekly_trend, weekly_modifier
from signals.sector_momentum import get_sector_momentum
from data.fetcher import get_earnings_days
from config import WEIGHTS, EARNINGS_PROXIMITY_DAYS, MIN_SCORE_TO_ALERT, MIN_PRICE


SECTOR_MOMENTUM_BONUS = 5   # soft +5 for picks whose sector ETF is above 50MA


def _determine_direction(technicals: dict, sentiment: dict) -> str:
    votes_bull = 0
    votes_bear = 0

    rsi = technicals["rsi"]
    if rsi["side"] == "bull":
        votes_bull += 1
    elif rsi["side"] == "bear":
        votes_bear += 1

    ema_pct = technicals["ema"]["ema50_pct"]
    if ema_pct > 2:
        votes_bull += 1
    elif ema_pct < -2:
        votes_bear += 1

    sent_score = sentiment.get("score", 0.0)
    if sent_score > 0.1:
        votes_bull += 1
    elif sent_score < -0.1:
        votes_bear += 1

    if votes_bull > votes_bear:
        return "bullish"
    if votes_bear > votes_bull:
        return "bearish"
    return "mixed"


def _determine_duration(technicals: dict, earnings_days: int | None) -> str:
    if earnings_days is not None and 0 <= earnings_days <= EARNINGS_PROXIMITY_DAYS:
        return "1-3d (earnings catalyst)"

    bb = technicals["bb"]["triggered"]
    atr = technicals["atr"]["triggered"]
    rsi_extreme = technicals["rsi"]["extreme"]
    vol_surge = technicals["vol"]["triggered"]

    # DAY trade: very high volume surge + strong directional RSI + no squeeze setup.
    # These are fast-moving names that can deliver a quick 2-3% in a single session.
    # DUSK will close them at 3:50 PM ET if still open.
    vol_raw     = technicals.get("vol", {}).get("ratio", 1.0)   # current vol / avg vol
    rsi_val     = technicals.get("rsi", {}).get("value", 50)
    extreme_vol = vol_raw >= 3.0                      # 3× average volume — real intraday momentum
    rsi_strong  = 62 <= rsi_val <= 71 or 29 <= rsi_val <= 38  # directional but NOT extreme
    # DAY trade: strong vol + directional RSI + no BB squeeze + RSI not extreme
    # Exclude extreme RSI (≥72 or ≤28) — those are momentum runners that need the 2-3w bracket
    # PAUSED 2026-06-02: DAY trades (added 5/29) were one of three changes that
    # coincided with the profitability swing and have no standalone validation.
    # Reverted to swing-only until validated separately. Set ENABLE_DAY_TRADES=True
    # to re-enable. When off, these setups fall through to the normal swing bracket.
    try:
        from config import ENABLE_DAY_TRADES
    except ImportError:
        ENABLE_DAY_TRADES = False
    if ENABLE_DAY_TRADES and extreme_vol and rsi_strong and not bb and not rsi_extreme:
        return "1d (day trade)"

    if bb and atr:
        return "5-7d (squeeze setup)"
    if rsi_extreme and vol_surge:
        return "2-3w (momentum surge)"
    return "5-7d (default)"


def _determine_confidence(score: float) -> str:
    if score >= 70:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"


def score_ticker(
    ticker: str,
    ohlcv_df: pd.DataFrame,
    sentiment: dict | None = None,
    earnings_days: int | None = None,
) -> dict:
    if ohlcv_df is None or ohlcv_df.empty:
        return {"ticker": ticker, "score": 0, "error": "no data"}

    # Finding #24 (2026-06-08): enforce the penny-stock skip HERE too, not just in
    # score_universe's loop. predict_universe (LIVE model path) calls score_ticker
    # directly, bypassing score_universe's filter, so sub-$MIN_PRICE names were
    # getting scored on the live path. Skip BEFORE the expensive technicals/pattern
    # work and return a non-tradeable (score=0) result so callers drop it.
    try:
        if float(ohlcv_df["Close"].iloc[-1]) < MIN_PRICE:
            return {"ticker": ticker, "score": 0, "error": f"below MIN_PRICE (${MIN_PRICE})"}
    except Exception:
        pass

    technicals = compute_all(ohlcv_df)
    if sentiment is None:
        sentiment = get_sentiment_with_velocity(ticker)

    signals_triggered = []
    breakdown = {}
    raw_score = 0

    # BB squeeze
    if technicals["bb"]["triggered"]:
        raw_score += WEIGHTS["bb_squeeze"]
        breakdown["bb_squeeze"] = WEIGHTS["bb_squeeze"]
        signals_triggered.append(f"BB squeeze (pct={technicals['bb']['width_percentile']})")
    else:
        breakdown["bb_squeeze"] = 0

    # ATR compression
    if technicals["atr"]["triggered"]:
        raw_score += WEIGHTS["atr_compression"]
        breakdown["atr_compression"] = WEIGHTS["atr_compression"]
        signals_triggered.append(f"ATR compression (ratio={technicals['atr']['ratio']})")
    else:
        breakdown["atr_compression"] = 0

    # Volume surge
    if technicals["vol"]["triggered"]:
        raw_score += WEIGHTS["volume_surge"]
        breakdown["volume_surge"] = WEIGHTS["volume_surge"]
        signals_triggered.append(f"Volume surge ({technicals['vol']['ratio']}x avg)")
    else:
        breakdown["volume_surge"] = 0

    # Sentiment spike
    if sentiment.get("spike", False):
        raw_score += WEIGHTS["sentiment_spike"]
        breakdown["sentiment_spike"] = WEIGHTS["sentiment_spike"]
        signals_triggered.append(f"Sentiment spike (Δ={sentiment.get('velocity', 0):.2f})")
    else:
        breakdown["sentiment_spike"] = 0

    # RSI extreme
    if technicals["rsi"]["extreme"]:
        raw_score += WEIGHTS["rsi_extreme"]
        breakdown["rsi_extreme"] = WEIGHTS["rsi_extreme"]
        signals_triggered.append(f"RSI extreme ({technicals['rsi']['value']})")
    else:
        breakdown["rsi_extreme"] = 0

    # Earnings proximity
    if earnings_days is not None and 0 <= earnings_days <= EARNINGS_PROXIMITY_DAYS:
        raw_score += WEIGHTS["earnings_proximity"]
        breakdown["earnings_proximity"] = WEIGHTS["earnings_proximity"]
        signals_triggered.append(f"Earnings in {earnings_days}d")
    else:
        breakdown["earnings_proximity"] = 0

    # ── Candlestick patterns ────────────────────────────────────────────────
    direction_early = _determine_direction(technicals, sentiment)
    pattern_result  = detect_patterns(ohlcv_df)
    pattern_name    = pattern_result.get("pattern")
    pattern_side    = pattern_result.get("side", "neutral")
    pattern_weight  = WEIGHTS.get("candlestick", 8)

    _dir_prefix = {"bullish": "bull", "bearish": "bear"}.get(direction_early, "neutral")
    if pattern_result.get("triggered") and pattern_side == _dir_prefix:
        # Pattern confirms direction — full weight
        pts = round(pattern_weight * pattern_result.get("strength", 0.5))
        raw_score += pts
        breakdown["candlestick"] = pts
        signals_triggered.append(f"{pattern_name} (strength={pattern_result['strength']:.2f})")
    elif pattern_result.get("triggered") and pattern_side != "neutral" and pattern_side != _dir_prefix:
        # Pattern contradicts direction — penalize slightly
        raw_score  = max(0, raw_score - round(pattern_weight * 0.3))
        breakdown["candlestick"] = 0
    else:
        breakdown["candlestick"] = 0

    # ── Short squeeze proxy (from OHLCV only, no API call) ─────────────────
    # Proxy: RSI recovering from oversold + price breaking above 20-day high
    # + volume surge — all classic squeeze precursors
    squeeze_pts = 0
    if (technicals["rsi"]["value"] is not None
            and 35 < technicals["rsi"]["value"] < 55   # recovering from oversold
            and technicals["vol"]["triggered"]           # volume surge
            and technicals["bb"]["triggered"]):          # squeeze setup
        squeeze_pts = WEIGHTS.get("short_squeeze", 5)
        raw_score  += squeeze_pts
        breakdown["short_squeeze"] = squeeze_pts
        signals_triggered.append(f"Squeeze proxy (RSI recovering + volume surge)")
    else:
        breakdown["short_squeeze"] = 0

    # ── Insider activity (SEC Form 4 cluster buys/sells, cached 24h) ───────
    insider_data = {"side": "neutral", "triggered": False, "strength": 0.0, "summary": ""}
    try:
        insider_data = get_insider_signal(ticker)
    except Exception as e:
        print(f"[scorer] insider fetch failed for {ticker}: {e}")
    insider_weight = WEIGHTS.get("insider_activity", 8)
    direction_so_far = _determine_direction(technicals, sentiment)
    _dir_prefix2 = {"bullish": "bull", "bearish": "bear"}.get(direction_so_far, "neutral")
    if insider_data["triggered"] and insider_data["side"] == _dir_prefix2:
        pts = round(insider_weight * insider_data["strength"])
        raw_score += pts
        breakdown["insider_activity"] = pts
        if insider_data["summary"]:
            signals_triggered.append(insider_data["summary"])
    elif insider_data["triggered"] and insider_data["side"] != "neutral" and insider_data["side"] != _dir_prefix2:
        # Insider activity contradicts current direction — penalize
        penalty = round(insider_weight * 0.4)
        raw_score = max(0, raw_score - penalty)
        breakdown["insider_activity"] = -penalty
    else:
        breakdown["insider_activity"] = 0

    # ── Analyst revisions (upgrades vs downgrades over 30d, cached 24h) ────
    analyst_data = {"side": "neutral", "triggered": False, "strength": 0.0, "summary": ""}
    try:
        analyst_data = get_analyst_signal(ticker)
    except Exception as e:
        print(f"[scorer] analyst fetch failed for {ticker}: {e}")
    analyst_weight = WEIGHTS.get("analyst_revisions", 7)
    if analyst_data["triggered"] and analyst_data["side"] == _dir_prefix2:
        pts = round(analyst_weight * analyst_data["strength"])
        raw_score += pts
        breakdown["analyst_revisions"] = pts
        if analyst_data["summary"]:
            signals_triggered.append(analyst_data["summary"])
    elif analyst_data["triggered"] and analyst_data["side"] != "neutral" and analyst_data["side"] != _dir_prefix2:
        penalty = round(analyst_weight * 0.4)
        raw_score = max(0, raw_score - penalty)
        breakdown["analyst_revisions"] = -penalty
    else:
        breakdown["analyst_revisions"] = 0

    # ── Weekly trend (multi-timeframe, Mode B soft booster) ────────────────
    # +10 when daily direction aligns with weekly trend (×strength)
    # −15 when daily direction is counter-trend to weekly (×strength)
    #   0 when weekly is neutral
    direction_final = _determine_direction(technicals, sentiment)
    weekly_data = {"trend": "neutral", "strength": 0.0, "summary": ""}
    try:
        # Reuse the daily OHLCV we already have — avoids a second API call
        weekly_data = get_weekly_trend(ticker, ohlcv_df=ohlcv_df)
    except Exception as e:
        print(f"[scorer] weekly_trend fetch failed for {ticker}: {e}")
    weekly_mod = weekly_modifier(direction_final, weekly_data["trend"], weekly_data["strength"])
    if weekly_mod != 0:
        raw_score = max(0, raw_score + weekly_mod)
        breakdown["weekly_trend"] = weekly_mod
        # Only surface in signals list if it's a meaningful aligned bonus —
        # we don't want "weekly bearish, you traded against it" cluttering picks
        if weekly_mod > 0 and weekly_data["summary"]:
            signals_triggered.append(weekly_data["summary"])
        elif weekly_mod < 0 and weekly_data["summary"]:
            signals_triggered.append(f"⚠ counter-trend: {weekly_data['summary']}")
    else:
        breakdown["weekly_trend"] = 0

    # ── Sector momentum (sector ETF above 50MA, no extra API call) ─────────
    # Soft +5 bonus only — no penalty. Weak-sector winners are still possible.
    sector_data = {"triggered": False, "sector_etf": None, "summary": ""}
    try:
        sector_data = get_sector_momentum(ticker)
    except Exception as e:
        print(f"[scorer] sector_momentum fetch failed for {ticker}: {e}")
    if sector_data["triggered"] and direction_final == "bullish":
        raw_score += SECTOR_MOMENTUM_BONUS
        breakdown["sector_momentum"] = SECTOR_MOMENTUM_BONUS
        if sector_data["summary"]:
            signals_triggered.append(sector_data["summary"])
    else:
        breakdown["sector_momentum"] = 0

    raw_score  = min(raw_score, 100)   # hard cap — weights can exceed 100 with bonuses
    direction  = direction_final
    duration   = _determine_duration(technicals, earnings_days)
    confidence = _determine_confidence(raw_score)

    return {
        "ticker":             ticker,
        "score":              raw_score,
        "direction":          direction,
        "duration":           duration,
        "confidence":         confidence,
        "signals_triggered":  signals_triggered,
        "breakdown":          breakdown,
        "rsi":                technicals["rsi"]["value"],
        "bb_pct":             technicals["bb"]["width_percentile"],
        "atr_ratio":          technicals["atr"]["ratio"],
        "atr_pct":            technicals["atr"].get("pct", 0.0),
        # close vs the PRIOR-10-session high (EXCLUDING the current bar [-11:-1]) — must
        # match the backtest/dashboard. (Bug 2026-06-16: was [-10:] incl current bar →
        # near_high ≤0 → filter rarely fired.) >-2% = at/above the high (PF 1.67 vs 2.30).
        "near_high_pct":      ((float(ohlcv_df["Close"].iloc[-1]) / float(ohlcv_df["High"].iloc[-11:-1].max()) - 1) * 100
                               if len(ohlcv_df) >= 11 and float(ohlcv_df["High"].iloc[-11:-1].max()) > 0 else -99.0),
        "volume_ratio":       technicals["vol"]["ratio"],
        "ema50_pct":          technicals["ema"]["ema50_pct"],
        "sentiment_score":    sentiment.get("score", 0.0),
        "sentiment_velocity": sentiment.get("velocity", 0.0),
        "earnings_days":      earnings_days,
        "pattern":            pattern_name,
        "pattern_side":       pattern_side,
        "insider_net":        insider_data.get("net_dollar", 0.0),
        "insider_side":       insider_data.get("side", "neutral"),
        "analyst_net":        analyst_data.get("net", 0),
        "analyst_side":       analyst_data.get("side", "neutral"),
        "weekly_trend":       weekly_data.get("trend", "neutral"),
        "weekly_mod":         weekly_mod,
        "sector_etf":         sector_data.get("sector_etf"),
        "sector_momentum_on": sector_data.get("triggered", False),
    }


def score_universe(
    tickers: list[str],
    ohlcv_map: dict[str, pd.DataFrame],
    sentiment_map: dict[str, dict] | None = None,
    earnings_map: dict[str, int | None] | None = None,
    min_score: int = MIN_SCORE_TO_ALERT,
) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        df = ohlcv_map.get(ticker, pd.DataFrame())
        # Penny-stock filter: never score sub-$MIN_PRICE names (they caused 100%
        # of net losses in paper). The hard entry gate in alpaca.py enforces it
        # again at execution; this just keeps junk out of the scored picks.
        if not df.empty:
            try:
                if float(df["Close"].iloc[-1]) < MIN_PRICE:
                    continue
            except Exception:
                pass
        sentiment = (sentiment_map or {}).get(ticker)
        earnings = (earnings_map or {}).get(ticker)
        result = score_ticker(ticker, df, sentiment, earnings)
        if result.get("score", 0) >= min_score:
            rows.append(result)
    if not rows:
        return pd.DataFrame()
    scored = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    return scored
