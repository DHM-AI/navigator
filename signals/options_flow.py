"""
Options Flow Signal — detects unusual institutional positioning.

Uses yfinance options chains (completely free):
  - Put/Call OI ratio: PCR > 1.2 = bearish pressure, PCR < 0.7 = bullish
  - Open Interest concentration: large OI at one strike = magnet (max pain)
  - IV proxy: ratio of near-term vs far-term implied volatility
  - Unusual activity: OI spike vs typical levels for that ticker

IMPORTANT: Slow — 1-3 seconds per ticker. Only run for the top picks
after initial scoring, not for the full 500-ticker universe.

Returns:
    {
        "triggered":      bool,
        "side":           "bull" | "bear" | "neutral",
        "put_call_ratio": float,    # total put OI / total call OI
        "max_pain":       float,    # strike where most options expire worthless
        "net_iv_skew":    float,    # puts_iv - calls_iv (>0 = bearish skew)
        "unusual":        bool,     # total OI unusually high
        "detail":         str,
    }
"""

import yfinance as yf
import numpy as np
import pandas as pd


def get_options_flow(ticker: str, max_expiries: int = 3) -> dict:
    """
    Fetch and analyze options chain for a single ticker.

    Args:
        ticker:       e.g. "NVDA"
        max_expiries: number of nearest expiration dates to analyze (default 3)

    Returns:
        Options flow dict (see module docstring).
    """
    neutral = {
        "triggered":      False,
        "side":           "neutral",
        "put_call_ratio": 1.0,
        "max_pain":       None,
        "net_iv_skew":    0.0,
        "unusual":        False,
        "detail":         "no options data",
    }

    try:
        t           = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return neutral

        # Use the nearest N expiration dates
        selected = list(expirations[:max_expiries])

        total_call_oi  = 0
        total_put_oi   = 0
        call_iv_list   = []
        put_iv_list    = []
        all_calls      = []
        all_puts       = []

        for exp in selected:
            try:
                chain = t.option_chain(exp)
                calls = chain.calls
                puts  = chain.puts

                if calls.empty or puts.empty:
                    continue

                # OI totals
                c_oi = calls["openInterest"].fillna(0).sum()
                p_oi = puts["openInterest"].fillna(0).sum()
                total_call_oi += c_oi
                total_put_oi  += p_oi

                # IV — weight by OI
                if "impliedVolatility" in calls.columns:
                    c_iv = (calls["impliedVolatility"].fillna(0) *
                            calls["openInterest"].fillna(0)).sum()
                    if c_oi > 0:
                        call_iv_list.append(c_iv / c_oi)

                if "impliedVolatility" in puts.columns:
                    p_iv = (puts["impliedVolatility"].fillna(0) *
                            puts["openInterest"].fillna(0)).sum()
                    if p_oi > 0:
                        put_iv_list.append(p_iv / p_oi)

                all_calls.append(calls)
                all_puts.append(puts)

            except Exception:
                continue

        if total_call_oi + total_put_oi == 0:
            return neutral

        # ── Put/Call Ratio ────────────────────────────────────────────────────
        pcr = total_put_oi / max(total_call_oi, 1)

        # ── Max Pain (strike where most options expire worthless) ─────────────
        max_pain_strike = None
        if all_calls and all_puts:
            try:
                calls_df = pd.concat(all_calls, ignore_index=True)
                puts_df  = pd.concat(all_puts,  ignore_index=True)

                # Get current price approximate (mid of first expiry strikes)
                strikes = sorted(set(calls_df["strike"].tolist()) &
                                 set(puts_df["strike"].tolist()))

                if len(strikes) > 5:
                    min_pain   = float("inf")
                    best_strike = strikes[len(strikes)//2]  # fallback

                    for s in strikes:
                        call_pain = calls_df[calls_df["strike"] <= s]["openInterest"].fillna(0).sum() * s
                        put_pain  = puts_df[puts_df["strike"] >= s]["openInterest"].fillna(0).sum() * s
                        total_pain = call_pain + put_pain
                        if total_pain < min_pain:
                            min_pain    = total_pain
                            best_strike = s

                    max_pain_strike = float(best_strike)
            except Exception:
                pass

        # ── IV Skew ───────────────────────────────────────────────────────────
        avg_put_iv  = float(np.mean(put_iv_list))  if put_iv_list  else 0.0
        avg_call_iv = float(np.mean(call_iv_list)) if call_iv_list else 0.0
        net_iv_skew = avg_put_iv - avg_call_iv  # positive = puts more expensive = bearish

        # ── Unusual activity (total OI > typical) ────────────────────────────
        # Heuristic: if total OI > 10K it's notable; > 50K it's heavy
        total_oi = total_call_oi + total_put_oi
        unusual  = total_oi > 50_000

        # ── Signal determination ──────────────────────────────────────────────
        bull_points = 0
        bear_points = 0

        if pcr < 0.7:
            bull_points += 2    # calls dominate → bullish
        elif pcr < 1.0:
            bull_points += 1
        elif pcr > 1.5:
            bear_points += 2    # puts dominate → bearish
        elif pcr > 1.2:
            bear_points += 1

        if net_iv_skew > 0.05:
            bear_points += 1    # put IV elevated → fear
        elif net_iv_skew < -0.05:
            bull_points += 1    # call IV elevated → demand

        triggered = bull_points >= 2 or bear_points >= 2
        if bull_points > bear_points:
            side = "bull"
        elif bear_points > bull_points:
            side = "bear"
        else:
            side = "neutral"

        detail_parts = [f"PCR={pcr:.2f}"]
        if net_iv_skew != 0:
            detail_parts.append(f"IV skew={net_iv_skew:+.3f}")
        if unusual:
            detail_parts.append(f"heavy OI ({total_oi:,.0f})")
        if max_pain_strike:
            detail_parts.append(f"max pain=${max_pain_strike:.0f}")

        return {
            "triggered":      triggered and side != "neutral",
            "side":           side,
            "put_call_ratio": round(pcr, 3),
            "max_pain":       max_pain_strike,
            "net_iv_skew":    round(net_iv_skew, 4),
            "unusual":        unusual,
            "detail":         " · ".join(detail_parts),
        }

    except Exception as e:
        neutral["detail"] = f"error: {e}"
        return neutral


def enrich_with_options(picks_df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """
    Add options flow columns to a scored picks DataFrame.
    Modifies score for picks where options give a strong directional signal.
    Only run this on the TOP picks (score ≥ 60) to keep scan time reasonable.

    Columns added:
        options_side, options_pcr, options_unusual, options_detail
    Score adjustment: +5 if options confirm direction, -10 if contradict.
    """
    if picks_df is None or picks_df.empty:
        return picks_df

    options_sides   = []
    options_pcrs    = []
    options_unusual = []
    options_details = []
    score_adj       = []

    for _, row in picks_df.iterrows():
        ticker    = row["ticker"]
        direction = row.get("direction", "bullish")

        if verbose:
            print(f"      [options] {ticker}...", end=" ", flush=True)

        flow = get_options_flow(ticker)

        options_sides.append(flow["side"])
        options_pcrs.append(flow["put_call_ratio"])
        options_unusual.append(flow["unusual"])
        options_details.append(flow["detail"])

        # Score adjustment
        adj = 0
        _dir_prefix = {"bullish": "bull", "bearish": "bear"}.get(direction, "")
        if flow["triggered"]:
            if flow["side"] == _dir_prefix and _dir_prefix:
                adj = +5
                if verbose:
                    print(f"confirms ({flow['detail']})", flush=True)
            elif flow["side"] not in ("neutral",):
                adj = -10
                if verbose:
                    print(f"contradicts ({flow['detail']})", flush=True)
            else:
                if verbose:
                    print(f"neutral", flush=True)
        else:
            if verbose:
                print(f"no signal", flush=True)

        score_adj.append(adj)

    picks_df = picks_df.copy()
    picks_df["options_side"]    = options_sides
    picks_df["options_pcr"]     = options_pcrs
    picks_df["options_unusual"] = options_unusual
    picks_df["options_detail"]  = options_details
    picks_df["score"]           = (picks_df["score"] + score_adj).clip(0, 100)

    return picks_df.sort_values("score", ascending=False).reset_index(drop=True)

# Finding #18 (2026-06-08): removed dead get_short_interest() — zero callers
# (short-squeeze scoring uses an OHLCV proxy in scorer.py, not this slow
# yfinance .info call). yfinance (yf) import retained: still used by
# get_options_flow above.
