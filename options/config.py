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
OPT_ENABLE_PAPER = True          # options run on the PAPER endpoint
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
OPT_MAX_PREMIUM_PCT = 0.02       # max premium per trade as a fraction of bankroll
OPT_MAX_OPEN = 3                 # max concurrent open option positions

# --- Strategy ------------------------------------------------------------------
OPT_STRATEGY = "long_option"     # simplest defined-risk: buy a call (bullish) / put (bearish)

# --- Backtest / pricing assumptions -------------------------------------------
OPT_IV_ASSUMPTION = 0.30         # implied-vol assumption for the BS-approx backtest
OPT_RISK_FREE = 0.04             # risk-free rate for Black-Scholes approximation
OPT_CONTRACT_MULTIPLIER = 100    # shares per option contract
