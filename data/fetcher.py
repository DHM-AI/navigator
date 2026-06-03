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

        # Map yfinance interval → Alpaca TimeFrame
        tf = TimeFrame.Day
        if interval in ("1wk", "1week"):
            tf = TimeFrame.Week
        elif interval in ("1h", "60m"):
            tf = TimeFrame.Hour
        elif interval in ("1m", "5m", "15m", "30m"):
            tf = TimeFrame.Minute

        start = _period_to_start_date(period)
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=tf, start=start)
        bars = client.get_stock_bars(req)
        df = bars.df
        if df is None or df.empty:
            return pd.DataFrame()
        # Alpaca returns a multi-index (symbol, timestamp). Drop symbol level.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level=0) if ticker in df.index.get_level_values(0) else df.droplevel(0)
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

        start = _period_to_start_date(period)
        req = StockBarsRequest(
            symbol_or_symbols=stock_tickers,
            timeframe=TimeFrame.Day,
            start=start,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df is None or df.empty:
            return {}
        out: dict[str, pd.DataFrame] = {}
        # Multi-index (symbol, timestamp) — split into per-symbol DataFrames
        if isinstance(df.index, pd.MultiIndex):
            for symbol in df.index.get_level_values(0).unique():
                sub = df.xs(symbol, level=0).copy()
                sub = sub.rename(columns={
                    "open":   "Open",  "high":   "High",  "low":    "Low",
                    "close":  "Close", "volume": "Volume",
                })
                keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in sub.columns]
                sub = sub[keep]
                if sub.index.tz is not None:
                    sub.index = sub.index.tz_convert("UTC").tz_localize(None)
                out[symbol] = sub
        else:
            # Single ticker came back without multi-index
            df = df.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            })
            if df.index.tz is not None:
                df.index = df.index.tz_convert("UTC").tz_localize(None)
            out[stock_tickers[0]] = df
        return out
    except Exception as e:
        print(f"[fetcher] Alpaca batch failed ({len(stock_tickers)} tickers): {e}")
        return {}


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
