"""Illuminati — Options backtest (first-pass Black-Scholes approximation).

backtest_options() is a FIRST-PASS approximation, not a production sim. It pulls
underlying DAILY bars via yfinance (which ARE available without OPRA), runs a
simple trend signal, opens a long call (bullish) / put (bearish), and values the
option each day with a Black-Scholes price (scipy.stats.norm). Trades close at
OPT_TARGET_DTE, a +100% premium gain, or a -100% premium loss (option expires /
goes to ~0 in the BS world).

If real option bars become available (OPRA signed) get_option_bars() would feed
real prices and method="real"; until then the OPRA 403 is handled gracefully and
method="approx_bs".

Built 2026-06-08. Lives apart from the equity engine — never modifies it.
"""

from __future__ import annotations

import math
from datetime import datetime

# Black-Scholes via scipy when present; fall back to a math.erf-based normal CDF
# so the backtest still runs if scipy is unavailable.
try:
    from scipy.stats import norm

    def _norm_cdf(x):
        return float(norm.cdf(x))
except Exception:  # pragma: no cover - fallback path
    def _norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

try:
    from . import config as opt_config
    from .data import get_option_bars
except ImportError:  # pragma: no cover - flat execution fallback
    import config as opt_config            # type: ignore
    from data import get_option_bars       # type: ignore


# ---- Black-Scholes ---------------------------------------------------------

def black_scholes_price(S, K, T, r, sigma, opt_type="call"):
    """European Black-Scholes option price.

    Args:
        S: underlying spot price.
        K: strike.
        T: time to expiry in YEARS.
        r: annual risk-free rate.
        sigma: annualised implied volatility.
        opt_type: "call" or "put".

    Returns:
        Option premium (per share, not per contract). At/after expiry (T<=0)
        returns intrinsic value. Degenerate sigma/S/K return intrinsic too.
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        # Intrinsic value at expiry.
        if opt_type == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)

    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    if opt_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# ---- Data helpers ----------------------------------------------------------

def _fetch_daily_closes(ticker, start, end):
    """Return [(date_str, close_float), ...] of daily closes via yfinance.

    Tries yf.Ticker().history first, then yf.download. Returns [] on any
    failure (network, empty) — the backtest treats that as "skip this name".
    """
    try:
        import yfinance as yf
    except Exception:
        return []

    # Attempt 1: Ticker.history
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            out = []
            for idx, row in hist.iterrows():
                d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                c = float(row["Close"])
                if c > 0:
                    out.append((d, c))
            if out:
                return out
    except Exception:
        pass

    # Attempt 2: yf.download
    try:
        df = yf.download(ticker, start=start, end=end, interval="1d",
                         progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            # Handle possible MultiIndex columns from yf.download.
            close_col = None
            if "Close" in df.columns:
                close_col = df["Close"]
            elif isinstance(df.columns, object) and ("Close", ticker) in list(df.columns):
                close_col = df[("Close", ticker)]
            if close_col is not None:
                out = []
                for idx, val in close_col.items():
                    try:
                        c = float(val)
                    except (TypeError, ValueError):
                        continue
                    if c > 0:
                        d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                        out.append((d, c))
                if out:
                    return out
    except Exception:
        pass

    return []


# ---- Backtest core ---------------------------------------------------------

def backtest_options(underlyings, start, end):
    """First-pass options backtest over the given underlyings and window.

    Strategy proxy (per underlying, per trading day with no open position):
      - Signal: close > N-day SMA -> bullish (long call); else bearish (long put).
      - Strike: ATM (= underlying close at entry).
      - Entry premium: Black-Scholes(S, K=ATM, T=OPT_TARGET_DTE/365, r, sigma).
      - Hold: re-value each day with decaying T; exit on +100%/-100% premium move
        or when held OPT_TARGET_DTE calendar days (whichever first).
      - Exit premium: real option bar if available, else Black-Scholes.

    Args:
        underlyings: list of tickers (e.g. ["SPY","QQQ"]).
        start, end: "YYYY-MM-DD" date strings.

    Returns:
        dict {
          "trades": int, "win_rate": float, "total_pnl": float,
          "profit_factor": float, "method": "approx_bs"|"real",
          "note": str, "sample": [ ...first few trades... ]
        }
        total_pnl is in dollars (per 1 contract * OPT_CONTRACT_MULTIPLIER).
    """
    sigma = float(getattr(opt_config, "OPT_IV_ASSUMPTION", 0.30))
    r = float(getattr(opt_config, "OPT_RISK_FREE", 0.04))
    target_dte = int(getattr(opt_config, "OPT_TARGET_DTE", 35))
    multiplier = int(getattr(opt_config, "OPT_CONTRACT_MULTIPLIER", 100))
    sma_window = 20  # trend lookback for the entry signal

    method = "approx_bs"
    note = (
        "FIRST-PASS APPROXIMATION. Underlying daily bars via yfinance; option "
        "premiums modeled with Black-Scholes (ATM strike, OPT_IV_ASSUMPTION vol, "
        "OPT_RISK_FREE rate, time decay). Real OPRA option bars are unavailable "
        "until the OPRA agreement is signed, so this is NOT a tradeable backtest "
        "— it ignores bid/ask spread, slippage, early exercise, IV smile/skew, "
        "and realized-vol vs assumed-vol differences."
    )

    trades = []

    for ticker in (underlyings or []):
        closes = _fetch_daily_closes(ticker, start, end)
        if len(closes) <= sma_window + 1:
            continue

        n = len(closes)
        i = sma_window  # earliest index with a full SMA window
        while i < n:
            date_i, price_i = closes[i]
            window = [c for (_, c) in closes[i - sma_window:i]]
            sma = sum(window) / len(window) if window else price_i

            opt_type = "call" if price_i > sma else "put"
            strike = price_i  # ATM

            # Entry premium (per share) at full target DTE.
            t0_years = target_dte / 365.0
            entry_premium = black_scholes_price(price_i, strike, t0_years, r,
                                                sigma, opt_type)
            if entry_premium <= 0:
                i += 1
                continue

            # Walk forward up to target_dte trading-ish steps (we use trading
            # days from the close series; T decays by calendar fraction).
            exit_premium = None
            exit_date = None
            exit_reason = None
            max_steps = target_dte  # cap holding by ~target DTE in bars
            j = i + 1
            steps = 0
            while j < n and steps < max_steps:
                date_j, price_j = closes[j]
                steps += 1
                remaining_days = max(target_dte - steps, 0)
                t_years = remaining_days / 365.0
                cur_premium = black_scholes_price(price_j, strike, t_years, r,
                                                  sigma, opt_type)
                ret = (cur_premium - entry_premium) / entry_premium

                if ret >= 1.0:  # +100% gain
                    exit_premium, exit_date, exit_reason = cur_premium, date_j, "tp_+100%"
                    break
                if ret <= -1.0 or remaining_days == 0:  # -100% or expiry
                    exit_premium, exit_date, exit_reason = cur_premium, date_j, (
                        "sl_-100%" if ret <= -1.0 else "expiry"
                    )
                    break
                j += 1

            # If the loop ran out of data without a trigger, mark-to-last.
            if exit_premium is None:
                last_idx = min(j, n - 1)
                date_last, price_last = closes[last_idx]
                remaining_days = max(target_dte - steps, 0)
                exit_premium = black_scholes_price(price_last, strike,
                                                   remaining_days / 365.0, r,
                                                   sigma, opt_type)
                exit_date = date_last
                exit_reason = "eod_mark"

            pnl = (exit_premium - entry_premium) * multiplier
            trades.append({
                "underlying": ticker,
                "type": opt_type,
                "entry_date": date_i,
                "exit_date": exit_date,
                "strike": round(strike, 2),
                "entry_premium": round(entry_premium, 4),
                "exit_premium": round(exit_premium, 4),
                "pnl": round(pnl, 2),
                "reason": exit_reason,
            })

            # Advance past the trade window (no overlapping positions per name).
            i = max(j, i + 1)

    # ---- Aggregate stats ----
    num_trades = len(trades)
    if num_trades == 0:
        return {
            "trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
            "profit_factor": 0.0, "method": method,
            "note": note + " (No trades generated — check date range / data.)",
            "sample": [],
        }

    wins = [t for t in trades if t["pnl"] > 0]
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / num_trades
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    return {
        "trades": num_trades,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "profit_factor": (round(profit_factor, 4)
                          if profit_factor != float("inf") else float("inf")),
        "method": method,
        "note": note,
        "sample": trades[:5],
    }


if __name__ == "__main__":  # pragma: no cover - manual self-check
    import json
    res = backtest_options(["SPY"], "2025-06-08", "2026-06-08")
    summary = {k: v for k, v in res.items() if k != "sample"}
    print(json.dumps(summary, indent=2, default=str))
    print("sample[0]:", res["sample"][0] if res["sample"] else None)
