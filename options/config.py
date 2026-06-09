"""Illuminati — Options subsystem configuration (ISOLATED + GATED, paper-only).

All OPT_* constants for the options subsystem live here. This module is the single
source of truth for the options gates, universe, sizing limits, contract selection
targets, and backtest assumptions.

HARD LIVE GATE: OPT_ENABLE_LIVE stays False. Options NEVER trade live. Execution
uses the Alpaca PAPER endpoint only. Do not flip OPT_ENABLE_LIVE to True without a
deliberate, validated go-live decision — and even then the execution layer refuses
live orders.
"""

# --- Master gates --------------------------------------------------------------
OPT_ENABLE_PAPER = False         # OPTIONS TRADING OFF (Renato 2026-06-08: "revert to
                                 # no options trade"). place_option_order /
                                 # close_option_position return {"status":"disabled"};
                                 # run_options_scan + manage_options no-op. Open position
                                 # was flattened first. Flip True to re-enable paper options.
OPT_ENABLE_LIVE  = False         # HARD GATE — options NEVER trade live

# --- Universe ------------------------------------------------------------------
# Only these high-liquidity underlyings are eligible for options trades.
OPT_ALLOWED_UNDERLYINGS = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMD", "TSLA"]

# --- Signal gating -------------------------------------------------------------
OPT_MIN_SCORE = 80               # only high-conviction equity signals -> options

# --- Contract selection (DTE / delta targets) ----------------------------------
OPT_TARGET_DTE = 35              # preferred days-to-expiration
OPT_DTE_MIN    = 21              # min days-to-expiration
OPT_DTE_MAX    = 45              # max days-to-expiration
OPT_TARGET_DELTA = 0.45          # used only when greeks available; else ATM moneyness

# --- Risk / sizing -------------------------------------------------------------
OPT_MAX_PREMIUM_PCT = 0.03       # max premium per trade as a fraction of bankroll
                                 # (raised 2%->3% on 2026-06-08 so 1 contract of pricier
                                 #  high-conviction names like AMD/NVDA fits — at 2% the
                                 #  only signal, AMD, was blocked by ~$116 and it sat idle)
OPT_MAX_OPEN = 3                 # max concurrent open option positions

# --- Strategy ------------------------------------------------------------------
OPT_STRATEGY = "long_option"     # simplest defined-risk: buy a call (bullish) / put (bearish)

# --- Exit management -----------------------------------------------------------
# A long option with no exit rots to zero on theta. These drive active exits in
# manage_options(): take-profit and stop are measured against the PREMIUM PAID
# (not the underlying), and the time-exit closes before the gamma/theta cliff.
OPT_TP_PCT   = 0.50              # take-profit: close at +50% gain on premium paid
OPT_STOP_PCT = 0.70             # stop: close at -70% loss on premium paid
                                # (widened 50%->70% on 2026-06-08: long options are
                                #  leveraged + start ~8% down from the entry spread, so a
                                #  tight stop chops out trades that recover. Max loss is
                                #  still capped at the premium = OPT_MAX_PREMIUM_PCT (3%) of
                                #  bankroll; the time-exit at 21 DTE also caps duration.)
OPT_EXIT_DTE = 21               # time-exit: close when <= 21 calendar days to expiration

# --- Entry-window reuse (carry over equity entry lessons) ----------------------
# When True, run_options_scan reuses the equity entry-window + Friday-PM guards
# (no open-chop entries, no end-of-day entries, no Friday-afternoon entries).
OPT_RESPECT_EQUITY_WINDOW = True

# --- Backtest / pricing assumptions -------------------------------------------
OPT_IV_ASSUMPTION = 0.30         # implied-vol assumption for the BS-approx backtest
OPT_RISK_FREE = 0.04             # risk-free rate for Black-Scholes approximation
OPT_CONTRACT_MULTIPLIER = 100    # shares per option contract
