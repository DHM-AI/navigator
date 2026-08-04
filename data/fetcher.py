from __future__ import annotations
"""
OHLCV data fetcher — Alpaca Market Data primary, yfinance fallback.

Alpaca's market data API is:
  - FREE for trading account holders (we already have keys)
  - Fast (broker-native)
  - Reliable (official SLA, no rate-limit roulette)
  - Knows tradable universe (no delisted-ticker timeout disasters)

yfinance is kept as a fallback because Alpaca:
  - Doesn't cover crypto with the same symbol format (uses BTC/USD)
  - May reject some delisted/illiquid tickers — fall back to yf for those
  - Doesn't expose earnings dates / news / company info (still need yf)

Public API (unchanged from before):
  get_ohlcv(ticker, period, interval)         -> pd.DataFrame
  get_ohlcv_batch(tickers, period, chunk_size) -> dict[str, pd.DataFrame]
  get_earnings_days(ticker)                    -> int | None
  get_recent_news(ticker)                      -> list[dict]
"""
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import yfinance as yf


# ─── Period parsing — convert yfinance-style strings to lookback days ───────
_PERIOD_DAYS = {
    "1d":   1,  "5d":   5,
    "1mo": 30,  "3mo": 95,  "6mo": 185,
    "1y":  370, "2y":  740, "5y": 1830, "10y": 3700,
    "max": 7300,
}


def _period_to_start_date(period: str) -> datetime:
    """Convert yfinance period string to a UTC datetime start."""
    days = _PERIOD_DAYS.get(period.lower().strip(), 370)
    return datetime.now(timezone.utc) - timedelta(days=days)


# ─── Alpaca data client (lazy, reused across calls) ──────────────────────────
_alpaca_client = None
_alpaca_init_failed = False


def _get_alpaca_data_client():
    """Lazily create the Alpaca StockHistoricalDataClient. Returns None if
    keys missing or import fails — caller should fall back to yfinance."""
    global _alpaca_client, _alpaca_init_failed
    if _alpaca_client is not None:
        return _alpaca_client
    if _alpaca_init_failed:
        return None
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        _alpaca_init_failed = True
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        _alpaca_client = StockHistoricalDataClient(api_key, secret_key)
        return _alpaca_client
    except Exception as e:
        print(f"[fetcher] Alpaca data client init failed: {e}")
        _alpaca_init_failed = True
        return None


def _is_crypto_symbol(t: str) -> bool:
    """Yahoo crypto: 'BTC-USD'. We let yfinance handle crypto; Alpaca uses
    a different symbol space (BTC/USD) for crypto, not stock bars."""
    s = (t or "").upper()
    return s.endswith("-USD") or "/" in s or s in ("BTC", "ETH", "SOL", "DOGE")


def _is_futures_symbol(t: str) -> bool:
    return (t or "").upper().endswith("=F")


# ─── Alpaca single-ticker fetch ──────────────────────────────────────────────

def _alpaca_get_ohlcv(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Fetch a single ticker via Alpaca. Returns empty DataFrame on any
    failure (caller should fall back to yfinance)."""
    client = _get_alpaca_data_client()
    if client is None:
        return pd.DataFrame()
    if _is_crypto_symbol(ticker) or _is_futures_symbol(ticker):
        return pd.DataFrame()   # Alpaca stock-bars API doesn't cover these

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed, Adjustment

        # Map yfinance interval → Alpaca TimeFrame
        tf = TimeFrame.Day
        if interval in ("1wk", "1week"):
            tf = TimeFrame.Week
        elif interval in ("1h", "60m"):
            tf = TimeFrame.Hour
        elif interval in ("1m", "5m", "15m", "30m"):
            tf = TimeFrame.Minute

        start = _period_to_start_date(period)
        # feed=SIP (full consolidated tape, not thin IEX) + adjustment=ALL
        # (split+dividend) to MATCH the yfinance fallback's auto_adjust=True, so
        # the same ticker has one convention regardless of source, and stock
        # splits don't create false price discontinuities (the KLAC-type glitch).
        # Alpaca uses dot class-shares ('BF.B'); the universe uses hyphens ('BF-B').
        _alp = ticker.replace("-", ".")
        req = StockBarsRequest(symbol_or_symbols=_alp, timeframe=tf, start=start,
                               feed=DataFeed.SIP, adjustment=Adjustment.ALL)
        bars = client.get_stock_bars(req)
        df = bars.df
        if df is None or df.empty:
            return pd.DataFrame()
        # Alpaca returns a multi-index (symbol, timestamp). Drop symbol level.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(_alp, level=0) if _alp in df.index.get_level_values(0) else df.droplevel(0)
        # Normalize columns to yfinance shape: Open, High, Low, Close, Volume
        df = df.rename(columns={
            "open":   "Open",  "high":   "High",  "low":    "Low",
            "close":  "Close", "volume": "Volume",
        })
        # Keep only the standard 5 columns (drop trade_count, vwap etc.)
        keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
        df = df[keep]
        # Make the datetime index timezone-naive so it matches yfinance behavior
        if df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        return df
    except Exception as e:
        # Don't print on every ticker — too noisy; only print first time per process
        if not getattr(_alpaca_get_ohlcv, "_warned", False):
            print(f"[fetcher] Alpaca fetch failed for {ticker}: {e} — falling back to yfinance")
            _alpaca_get_ohlcv._warned = True
        return pd.DataFrame()


# ─── Alpaca multi-symbol batch fetch ─────────────────────────────────────────

def _alpaca_get_ohlcv_batch(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    """Batch-fetch via Alpaca (up to 200 symbols per request)."""
    client = _get_alpaca_data_client()
    if client is None:
        return {}
    # Strip out crypto/futures — Alpaca doesn't cover them via stock-bars
    stock_tickers = [t for t in tickers
                     if not _is_crypto_symbol(t) and not _is_futures_symbol(t)]
    if not stock_tickers:
        return {}
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed, Adjustment
    except Exception:
        return {}
    start = _period_to_start_date(period)

    def _parse(bdf, req_syms):
        res = {}
        if bdf is None or bdf.empty:
            return res
        multi = isinstance(bdf.index, pd.MultiIndex)
        syms = list(bdf.index.get_level_values(0).unique()) if multi else req_syms[:1]
        for sym in syms:
            try:
                sub = bdf.xs(sym, level=0).copy() if multi else bdf.copy()
                sub = sub.rename(columns={
                    "open": "Open", "high": "High", "low": "Low",
                    "close": "Close", "volume": "Volume",
                })
                keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in sub.columns]
                sub = sub[keep]
                if sub.index.tz is not None:
                    sub.index = sub.index.tz_convert("UTC").tz_localize(None)
                res[sym] = sub
            except Exception:
                pass
        return res

    def _fetch(req_syms):
        req = StockBarsRequest(symbol_or_symbols=req_syms, timeframe=TimeFrame.Day,
                               start=start, feed=DataFeed.SIP, adjustment=Adjustment.ALL)
        return _parse(client.get_stock_bars(req).df, req_syms)

    # Alpaca uses dot class-shares ('BF.B'); the universe uses hyphens ('BF-B').
    # Translate for the request, restore original keys in the output. On a batch
    # failure (one invalid symbol nukes the whole request), retry per symbol so the
    # rest still come from Alpaca instead of dropping the entire chunk to yfinance.
    amap = {t.replace("-", "."): t for t in stock_tickers}
    try:
        got = _fetch(list(amap.keys()))
    except Exception as e:
        print(f"[fetcher] Alpaca batch failed ({len(stock_tickers)} tickers): {e} — per-symbol retry")
        got = {}
        for asym in amap:
            try:
                got.update(_fetch([asym]))
            except Exception:
                pass
    out: dict[str, pd.DataFrame] = {}
    for asym, df in got.items():
        out[amap.get(asym, asym)] = df
    return out


# ─── yfinance fallbacks (original behavior, used when Alpaca fails) ──────────

def _yf_get_ohlcv(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        print(f"[fetcher] yf {ticker}: {e}")
        return pd.DataFrame()


def _yf_get_ohlcv_batch(tickers: list[str], period: str = "1y",
                        chunk_size: int = 50, delay: float = 1.0) -> dict[str, pd.DataFrame]:
    results: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            raw = yf.download(chunk, period=period, interval="1d",
                              progress=False, auto_adjust=True, group_by="ticker")
            for ticker in chunk:
                try:
                    if len(chunk) == 1:
                        df = raw.copy()
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                    else:
                        df = raw[ticker].copy()
                        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                    df = df.dropna(how="all")
                    if not df.empty:
                        results[ticker] = df
                except Exception:
                    results[ticker] = _yf_get_ohlcv(ticker, period=period)
        except Exception as e:
            print(f"[fetcher] yf batch error chunk {i}: {e}")
            for ticker in chunk:
                results[ticker] = _yf_get_ohlcv(ticker, period=period)
        if i + chunk_size < len(tickers):
            time.sleep(delay)
    return results


# ─── Complete-session guard (2026-07-14) ─────────────────────────────────────

def drop_partial_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` without TODAY's still-forming session bar.

    WHY (root-caused 2026-07-14 — "why no trades for 2 days"): the scanner runs
    INTRADAY (9:30–15:30 ET), so the last daily bar is a PARTIAL session — its
    Volume is a fraction of the eventual day and its High/Low range is squashed.
    Feeding that bar to the signal stack + the model is a correctness bug:

      • volume_surge  (13 pts) divides PARTIAL-day volume by a FULL-day 20d
        average, so inside the 10:00–13:00 entry window it needs a ~6-8x day to
        fire → effectively never fires → every score runs ~13 points light.
      • atr_compression (8 pts) FALSELY triggers: an unfinished bar has an
        artificially narrow range, which reads as a "coiled spring".
      • candlestick    (7 pts) reads a candle that has not closed yet.
      • atr_pct (stop sizing) is biased NARROW → stops set too tight.
      • the XGBoost model was TRAINED on complete daily bars, so a partial final
        bar is a feature distribution it never saw once (train/serve skew) —
        making live xgb_prob non-comparable to the backtested gate.

    Dropping it makes LIVE score/predict on the same complete-session data the
    backtest and the trainer use. Execution is NOT affected: place_order sources
    its entry price from a LIVE quote (get_current_price), never from these bars.

    Keeps today's bar once the session has CLOSED (>=16:00 ET), since by then it
    is a complete session and discarding it would throw away a real day.
    """
    if df is None or len(df) < 2:
        return df
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        last = df.index[-1]
        last_date = last.date() if hasattr(last, "date") else pd.to_datetime(last).date()
        if last_date < now_et.date():
            return df                      # last bar is an earlier, complete session
        if now_et.hour >= 16:
            return df                      # today's session has closed → bar is complete
        return df.iloc[:-1]                # today's bar is still forming → drop it
    except Exception:
        return df                          # never break the scan over this


# ─── Public API (Alpaca first, yfinance fallback) ────────────────────────────

def get_ohlcv(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Fetch single-ticker OHLCV. Alpaca first; yfinance only on miss."""
    df = _alpaca_get_ohlcv(ticker, period, interval)
    if not df.empty:
        return df
    return _yf_get_ohlcv(ticker, period=period, interval=interval)


def get_ohlcv_batch(tickers: list[str], period: str = "1y",
                    chunk_size: int = 200, delay: float = 0.0) -> dict[str, pd.DataFrame]:
    """
    Fetch many tickers. Alpaca handles all stock symbols in one shot (up to 200
    per request). Crypto/futures fall through to yfinance. Any individual stock
    Alpaca couldn't fetch (delisted, no data) also falls through to yfinance.

    Default chunk_size raised 50→200 since Alpaca handles big batches natively.
    delay defaulted to 0 (no rate limiting needed with Alpaca).
    """
    if not tickers:
        return {}

    # Round 1: try Alpaca batch in chunks of 200
    results: dict[str, pd.DataFrame] = {}
    stock_tickers   = [t for t in tickers if not _is_crypto_symbol(t) and not _is_futures_symbol(t)]
    special_tickers = [t for t in tickers if _is_crypto_symbol(t) or _is_futures_symbol(t)]

    for i in range(0, len(stock_tickers), chunk_size):
        chunk = stock_tickers[i:i + chunk_size]
        results.update(_alpaca_get_ohlcv_batch(chunk, period))

    # Round 2: anything Alpaca didn't return + all crypto/futures → yfinance
    missing = [t for t in tickers if t not in results] + special_tickers
    missing = list(dict.fromkeys(missing))   # dedupe, preserve order
    if missing:
        print(f"[fetcher] Alpaca returned {len(results)} / {len(stock_tickers)} stocks; "
              f"falling back to yfinance for {len(missing)} ticker(s)")
        # Use smaller chunks for yfinance to be polite about rate limits
        yf_results = _yf_get_ohlcv_batch(missing, period=period, chunk_size=30, delay=0.5)
        results.update(yf_results)


    # M-26 (2026-06-09): NO consumer checks bar freshness — a halted/delisted/
    # yf-stale ticker's last bar can be weeks old, and every signal downstream
    # treats iloc[-1] as "now" (then trades on it). Drop any frame whose last
    # bar is older than 5 calendar days (covers weekends + a holiday).
    try:
        _now = pd.Timestamp.utcnow().tz_localize(None)
        _stale_dropped = []
        for _t in list(results.keys()):
            _df = results[_t]
            try:
                if _df is None or _df.empty:
                    del results[_t]; _stale_dropped.append(_t); continue
                _last = pd.to_datetime(_df.index[-1])
                if getattr(_last, "tzinfo", None) is not None:
                    _last = _last.tz_localize(None)
                if (_now - _last).days > 5:
                    del results[_t]; _stale_dropped.append(_t)
            except Exception:
                pass
        if _stale_dropped:
            print(f"[fetcher] dropped {len(_stale_dropped)} STALE frame(s) "
                  f"(last bar >5d old): {', '.join(_stale_dropped[:10])}")
    except Exception:
        pass

    return results


# ─── Earnings + News still use yfinance (Alpaca doesn't expose them) ────────

def get_earnings_days(ticker: str) -> Optional[int]:
    """Days until next earnings. Yfinance-only (Alpaca doesn't surface this)."""
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            date_val = cal.get("Earnings Date")
            if date_val is None:
                return None
            if isinstance(date_val, list):
                date_val = date_val[0]
            earnings_dt = pd.to_datetime(date_val)
        elif isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.columns:
                date_val = cal["Earnings Date"].iloc[0]
            elif "Earnings Date" in cal.index:
                date_val = cal.loc["Earnings Date"].iloc[0]
            else:
                return None
            earnings_dt = pd.to_datetime(date_val)
        else:
            return None
        delta = (earnings_dt.date() - datetime.today().date()).days
        # Audit 2026-06-02: yfinance can return a STALE/past earnings date,
        # yielding a negative "days until earnings" that earnings-proximity
        # guards misread as imminent. Only return forward-looking values.
        if delta < 0:
            return None
        return delta
    except Exception:
        return None


def get_recent_news(ticker: str) -> list[dict]:
    try:
        t = yf.Ticker(ticker)
        return t.news or []
    except Exception:
        return []
