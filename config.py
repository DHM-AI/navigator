from dotenv import load_dotenv
import os

load_dotenv()

# ── Environment: "prod" (live system) or "lab" (paper sandbox for experiments) ──
# Defaults to "prod" → ZERO behavior change for the live system. When the LAB
# copy sets SYSTEM_ENV=lab, alerts + dashboard are clearly marked [LAB] so the
# experimental paper copy can never be confused with the real-money system.
SYSTEM_ENV = os.getenv("SYSTEM_ENV", "prod").strip().lower()
IS_LAB     = SYSTEM_ENV == "lab"

# ── API Keys ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
ALPHA_VANTAGE_KEY  = os.getenv("ALPHA_VANTAGE_KEY", "")
SLACK_WEBHOOK_URL  = os.getenv("SLACK_WEBHOOK_URL", "")
GMAIL_USER         = os.getenv("GMAIL_USER", "")        # legacy — kept for optional fallback
GMAIL_APP_PASS     = os.getenv("GMAIL_APP_PASS", "")    # legacy
ALERT_EMAIL        = os.getenv("ALERT_EMAIL", "renato@deltahubmedia.com")  # legacy

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ── Alpaca ────────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# NOTE: There is no ALPACA_BASE_URL. The alpaca-py SDK selects the live vs paper
# endpoint solely from the TradingClient(paper=...) flag, which we drive from
# ALPACA_LIVE_MODE below. Setting a base URL has no effect — DO NOT add one back.
# To go live: set ALPACA_LIVE_MODE=true (plus live ALPACA_API_KEY/SECRET).
ALPACA_LIVE_MODE  = os.getenv("ALPACA_LIVE_MODE", "false").lower() == "true"

# ── Universe ──────────────────────────────────────────────────────────────────
FUTURES = ["ES=F", "NQ=F", "RTY=F", "CL=F", "GC=F", "ZB=F"]

# ── Watchlist — always scanned even if not in S&P 500 ─────────────────────────
# These are scanned on every run regardless of index membership.
# S&P 500 stocks are included automatically — list only extras here.
WATCHLIST = [
    # ── Airlines (high volume, volatile, often not in S&P 500) ────────────────
    "AAL",   # American Airlines  — removed from S&P 500 in 2020
    "UAL",   # United Airlines    — may rotate in/out of S&P 500
    "DAL",   # Delta Air Lines    — may rotate in/out of S&P 500
    "LUV",   # Southwest Airlines — may rotate in/out of S&P 500
    "JBLU",  # JetBlue            — not in S&P 500

    # ── High-profile tech / growth not always in S&P 500 ─────────────────────
    "TSLA",  # Tesla              — in S&P 500 (deduped automatically if so)
    "RIVN",  # Rivian             — EV, not in S&P 500
    "LCID",  # Lucid Motors       — EV, not in S&P 500
    "HOOD",  # Robinhood          — fintech, not in S&P 500
    "SOFI",  # SoFi Technologies  — fintech, not in S&P 500
    "RBLX",  # Roblox             — gaming/metaverse
    "COIN",  # Coinbase           — crypto exchange
    "PLTR",  # Palantir           — AI/data (may be in S&P 500)
    "SNOW",  # Snowflake          — cloud data
    "PATH",  # UiPath             — automation/AI

    # ── Recent high-volume IPOs (auto-updated via get_recent_ipos()) ──────────
    # Dynamic IPO list is appended at runtime — see universe.py
]

# How many months back to look for recent IPOs
IPO_LOOKBACK_MONTHS = 12
# Min avg daily volume for an IPO to be worth scanning
IPO_MIN_VOLUME = 1_000_000

# ── Signal thresholds ─────────────────────────────────────────────────────────
BB_SQUEEZE_PERCENTILE  = 10
ATR_COMPRESSION_RATIO  = 0.75
VOLUME_SURGE_RATIO     = 2.0
RSI_OVERBOUGHT         = 72
RSI_OVERSOLD           = 28
EARNINGS_PROXIMITY_DAYS = 10

# ── Scoring weights (must sum to 100) ─────────────────────────────────────────
WEIGHTS = {
    "bb_squeeze":         15,   # was 18 — trimmed for insider/analyst
    "atr_compression":     8,   # was 10
    "volume_surge":       13,   # was 15
    "sentiment_spike":    13,   # was 15
    "rsi_extreme":         9,   # was 10
    "earnings_proximity":  6,   # was 7
    "candlestick":         7,   # was 8
    "options_flow":        9,   # was 10
    "short_squeeze":       5,   # was 7
    "insider_activity":    8,   # NEW — SEC Form 4 cluster buys
    "analyst_revisions":   7,   # NEW — upgrade/downgrade momentum
}
# Total: 15+8+13+13+9+6+7+9+5+8+7 = 100

# ── Market regime ──────────────────────────────────────────────────────────────
REGIME_CACHE_MINUTES  = 30   # re-fetch regime every 30 minutes during scan
ENABLE_OPTIONS_FLOW   = True  # set False to skip options API (faster scans)

# ── Goal ──────────────────────────────────────────────────────────────────────
MONTHLY_TARGET_PCT  = 0.10   # 10% per month target return

# ── Prediction ────────────────────────────────────────────────────────────────
MIN_SCORE_TO_ALERT  = 50
TOP_N_CLAUDE_ANALYSIS = 5
MOVE_TARGET_PCT     = 0.20   # 20% ceiling TP — trailing stops handle normal exits at 3-7%

# ── Day trade bracket parameters (tighter than swing) ────────────────────────
# PAUSED 2026-06-02: DAY trades (added 5/29) were one of three changes during the
# +$7.6k→-$5.1k swing; no standalone validation. Off until validated separately.
ENABLE_DAY_TRADES    = True   # classifier active — signals can be tagged "1d (day trade)"
DAY_TRADE_STOP_PCT   = 0.015  # -1.5% stop (tighter than swing's -3%)
DAY_TRADE_TARGET_PCT = 0.03   # +3% take profit (quick win, closed by DUSK at 3:50 PM)

# FORCE_DAY_TRADES: when True, EVERY new position is treated as a day trade —
# tighter bracket (-1.5% / +3%), DAY TIF, closed by DUSK at 3:50 PM ET.
# Zero overnight exposure. Data showed day-trade style: 68% wr / +$92/trade
# vs swing: 58% wr / -$13/trade (Renato 2026-06-05). Flip to False to restore
# swing mode. ENABLE_DAY_TRADES must also be True for classifier to fire.
FORCE_DAY_TRADES     = True

# ── Volatility filter + ATR-based stops (added 2026-06-02) ───────────────────
# Backtest of 42 real stop-outs (6/2): the flat -3% swing stop sits INSIDE one
# normal day of noise on every name traded (avg ATR% ~6%). 88% of stopped-out
# trades recovered above entry within 7 days = noise stopouts, not broken theses.
# BUT naively widening to 2x ATR LOST -181% — because ultra-volatile junk
# micro-caps (YMAT 37% ATR, RYOJ 26%) ride a wide stop straight down. The winning
# combo (backtest: -181% -> +35%, win 21% -> 60%): FILTER out the junk, THEN use
# ATR stops on the quality names that remain.
#
# Part 1 — volatility filter: block auto-exec on names whose ATR% exceeds this.
# These are lottery tickets no stop setting fixes; they are the real -30% days.
ENABLE_VOLATILITY_FILTER = True
MAX_ENTRY_ATR_PCT        = 0.10   # skip auto-trade if 14-day ATR > 10% of price
#
# Part 2 — ATR-based stops: stop = entry -/+ (ATR_STOP_MULT x ATR), clamped to
# [floor, cap]. Position size auto-rescales so $ risk per trade stays constant
# regardless of how wide the volatility-adjusted stop is (volatility sizing).
ENABLE_ATR_STOPS   = True
ATR_STOP_MULT      = 2.0    # 2x ATR = swing-trading standard (research: 1.5-2.5x)
ATR_STOP_FLOOR_PCT = 0.03   # never tighter than 3% (current behavior as the floor)
ATR_STOP_CAP_PCT   = 0.10   # never wider than 10% (cap caught the junk in backtest)

# ── Model blending weights ────────────────────────────────────────────────────
XGB_WEIGHT       = 0.70
SENTIMENT_WEIGHT = 0.30

# ── Kelly Criterion / position sizing ─────────────────────────────────────────
# Sizing base = the REAL account. Per-trade 8% × 10 positions = ~80% deployed,
# 20% cash buffer. At ~$153k that's ~$12.3k/trade, ~$370 risk/trade @ 3% stop.
#
# DYNAMIC BANKROLL (2026-06-03): the live bankroll is resolved at sizing time by
# execution.alpaca.get_bankroll(), which reads the account's `last_equity` (prior
# market close) — so the bankroll tracks the real account and "upgrades" each day
# at the close, instead of a fixed/imaginary number drifting out of reality.
# This BANKROLL constant is now only the FALLBACK used if the live account can't
# be read (and the `BANKROLL` env/secret can still override the fallback).
BANKROLL          = float(os.getenv("BANKROLL", "153000"))
KELLY_WIN_PCT     = 0.10   # expected gain on a win (trailing stops avg ~10% on winners)
KELLY_LOSS_PCT    = 0.03   # expected loss if wrong (3% stop)
KELLY_FRACTION    = 0.5    # use half-Kelly for safety
# ── Live ramp-up ("canary") ────────────────────────────────────────────────────
# Small real-money positions for the first weeks live, so a bad day costs ~$100
# instead of real money. SAME strategy / signals / stops — only position SIZE and
# COUNT shrink. To return to full size after live validation: set CANARY_MODE =
# False (one line) and everything reverts to the full values below.
CANARY_MODE               = True
CANARY_MAX_POSITION_PCT   = 0.04   # ~$1,000/trade on a $25k account (raised 2%→4%, Renato 2026-06-05)
CANARY_MAX_OPEN_POSITIONS = 5      # up to 5 concurrent (raised 3→5, Renato 2026-06-05)
CANARY_MAX_DAILY_TRADES   = 10     # day-trade turnover (raised 4→10, Renato 2026-06-05)
FULL_MAX_POSITION_PCT     = 0.08   # 8% per trade (rebased to real account, Option 3)
MAX_POSITION_PCT  = CANARY_MAX_POSITION_PCT if CANARY_MODE else FULL_MAX_POSITION_PCT
DAILY_LOSS_LIMIT_PCT = 0.05  # halt trading if down 5% in a day

# Minimum share price to TRADE. Penny stocks (<$5 — the SEC's penny-stock line)
# caused 100% of the system's net losses in paper testing: RYOJ alone lost
# -$4,170 on a $3.78 stock. Excluding sub-$5 names flips the closed-trade record
# from -$2,655 to +$1,020. Enforced as a HARD pre-trade gate (execution/alpaca.py)
# AND a pre-scoring skip (signals/scorer.py) so junk is never scored or traded.
# (Renato 2026-06-04)
MIN_PRICE = 5.0

# ── Sentiment Guard ───────────────────────────────────────────────────────────
# Monitors open positions for sentiment reversal and takes protective action.
# Scores range from -1.0 (very bearish) to +1.0 (very bullish).
GUARD_WARN_THRESHOLD    = -0.15  # LONG:  alert when sentiment drops below this
GUARD_TIGHTEN_THRESHOLD = -0.35  # LONG:  tighten stop to 1.5% when below this
GUARD_CLOSE_THRESHOLD   = -0.60  # LONG:  close position when sentiment this bad
# (reversed for SHORT positions)

# ── Trailing stop ──────────────────────────────────────────────────────────────
# Multi-level trailing: as gains grow, the trail tightens to lock in more profit.
# AEGIS checks all levels on every run — positions always upgrade to the tightest
# applicable level.
TRAIL_TRIGGER_PCT = 0.03   # activate trailing when position is up 3%
TRAIL_PCT         = 0.03   # initial trail — 3% below peak

# Tightening levels: (min_gain_pct, trail_pct)
# At +7% gain → tighten to 2% trail (locks in ~5%)
# At +10% gain → tighten to 1.5% trail (locks in ~8.5%)
# At +15% gain → tighten to 1% trail (locks in ~14%)
TRAIL_TIGHTEN_LEVELS = [
    (0.15, 0.010),   # up 15%+ → 1.0% trail
    (0.10, 0.015),   # up 10%+ → 1.5% trail
    (0.07, 0.020),   # up  7%+ → 2.0% trail
    (0.03, 0.030),   # up  3%+ → 3.0% trail (initial)
]

# Cooldown: after a big winner closes, block opposite-direction signals
COOLDOWN_WIN_THRESHOLD = 0.05   # block counter-signal if prior close was +5%+
COOLDOWN_HOURS         = 24     # hours to block counter-direction trades

# Loss cooldown: after stopping out of a name, block re-entry for N hours
# Prevents "death by a thousand cuts" — same volume/RSI signal keeps firing
# on a declining stock, system keeps re-buying, each stop loss bleeds capital.
# Real example: RYOJ on 2026-05-27 — 13 separate stop-outs same day = -$4.2k.
LOSS_COOLDOWN_PCT      = 0.01   # any close down >1% counts as a stop hit
LOSS_COOLDOWN_HOURS    = 48     # block re-entry for 2 trading days

# Minimum stock price — penny/low-priced stocks have wide spreads + low
# liquidity. A $7500 position in a $3 stock = 2500 shares that you can't
# exit cleanly. Skip them entirely.
MIN_STOCK_PRICE        = 5.00   # below this → skip the pick

# ── Weekend-gap protection ────────────────────────────────────────────────────
# Originally blocked Friday PM entries (after 2 PM) to prevent weekend gap risk:
# HOOD (-$539) and WOLF (-$554) were bought Fri 2:09 PM, gapped through stops
# over the weekend. Rule made sense for SWING trades that hold overnight.
#
# Disabled 2026-06-05: with FORCE_DAY_TRADES=True, DUSK closes ALL positions
# at 3:50 PM — including Friday. A trade opened at 2:30 PM Friday is GONE by
# 3:50 PM Friday. No weekend exposure at all. Blocking Friday PM entries now
# just wastes the 2 PM and 3 PM scan windows for no reason.
# Restore to True if swing mode is ever re-enabled.
# Single, uniform entry cutoff for EVERY day: no new positions after 3:30 PM ET
# (Renato 2026-06-05: "3:30 every day"). Day trades need room to work before DUSK
# closes at 3:50; entries after DUSK orphan overnight (FPS opened 3:58 PM →
# stranded). Entries stop 3:30, DUSK closes 3:50 → no orphan window. No special
# Friday rule — all weekdays identical.
NO_ENTRY_AFTER_ET       = 15.5   # 3:30 PM ET — last entry of the day (all weekdays)
BLOCK_FRIDAY_PM_ENTRIES = False  # Friday treated like any other day (3:30 cutoff applies)
FRIDAY_ENTRY_CUTOFF_ET  = 14.0   # unused while BLOCK_FRIDAY_PM_ENTRIES=False

# ── Max entries per ticker (added 2026-06-01) ─────────────────────────────────
# Stops the "accumulate into a loser" pattern. On 2026-06-01 ON was bought 5×
# in a week ($114→$122→$124→$123→$120, averaging UP into a decline). The
# loss-cooldown only fires AFTER a position closes red — it can't stop adds while
# still holding. This caps total buy fills per ticker over a rolling window,
# counted from Alpaca directly (not the in-memory position list, which the
# duplicate check relied on and which clearly missed ON).
MAX_ENTRIES_PER_TICKER  = 2      # max buy fills per ticker within the window
ENTRY_CAP_WINDOW_DAYS   = 5      # rolling window for counting entries

# ── Catastrophic-day circuit breakers (added 2026-06-01) ──────────────────────
# Diagnosed from 30 days of paper trading: every big red day was caused by ONE
# of three patterns. Each breaker below targets a confirmed pattern.
#
# (A) RE-ENTRY SPIRAL — RYOJ stopped out 13× in a single day (2026-05-27) for
#     -$4,190 = 88% of that -$4,776 day. Once a ticker stops out, lock it for
#     the rest of the trading day. The biggest single fix.
BLOCK_SAME_DAY_REENTRY  = True   # after a stop-out, no re-entry on that ticker today

# (B) STOP DIDN'T FIRE — FLY -15.9%, VOYG -15.5%, YMAT -10.8% each lost ~$1,000+
#     on ONE trade (marked "manual", not sl_hit) = stop never executed. The 3%
#     stop should never let a position reach -8%. AEGIS force-closes anything
#     past this hard ceiling at market — a safety net under the normal stop.
HARD_MAX_LOSS_PCT       = 0.08   # force-close at market if a position is down >8%

# (C) NO DAILY KILL-SWITCH — May 27 ran unchecked to -$4,776. The existing
#     DAILY_LOSS_LIMIT_PCT checks slow account-equity swing; this checks REALIZED
#     closed-trade P&L today and halts ALL new entries once it breaches the floor.
#     Open positions keep being managed by AEGIS; only NEW entries are blocked.
# At Option-3 sizing (~$370 risk/trade @ 3% stop), -$2,000 ≈ 5-6 stopped trades
# in a day before the kill-switch — a sensible "today is going badly, stop" line.
# min($2,000, 3%×bankroll): at $153k that's min(2000, 4590) = $2,000.
DAILY_REALIZED_LOSS_HALT_USD = 2000.0   # halt new entries after -$2,000 realized today
DAILY_REALIZED_LOSS_HALT_PCT = 0.03     # ...or -3% of bankroll, whichever hits first

# ── Partial Exit (Two-Tier Scale-Out) ─────────────────────────────────────────
# THREE-STAGE EXIT STRATEGY:
#   Tier 1 at +7%:  close 20% of ORIGINAL position, move stop to breakeven
#   Tier 2 at +12%: close another 20% of ORIGINAL position (same qty as Tier 1)
#   Remaining 60%:  rides with multi-level AEGIS trailing stop (catches runners)
#
# 20/20/60 vs old 33/33/34: lets 60% of the position run instead of 34%.
# More aggressive "let winners ride" — bigger payout on strong moves.
# The two early trims still lock in profit and move stop to breakeven.
#
# Set ENABLE_PARTIAL_EXIT = False to revert to single-exit mode instantly.
ENABLE_PARTIAL_EXIT          = True

PARTIAL_EXIT_TIER1_TRIGGER   = 0.07   # fire Tier 1 at +7% gain
PARTIAL_EXIT_TIER1_FRACTION  = 0.20   # close 20% of original position
PARTIAL_EXIT_MOVE_TO_BE      = True   # move remaining stop to breakeven after T1

PARTIAL_EXIT_TIER2_TRIGGER   = 0.12   # fire Tier 2 at +12% gain
PARTIAL_EXIT_TIER2_FRACTION  = 0.20   # close another 20% (= same qty as T1)

# Legacy aliases — keep existing callers working
PARTIAL_EXIT_TRIGGER_PCT = PARTIAL_EXIT_TIER1_TRIGGER
PARTIAL_EXIT_FRACTION    = PARTIAL_EXIT_TIER1_FRACTION

# ── Auto-execution threshold ──────────────────────────────────────────────────
# Two-tier gate (Option B, 2026-05-28):
#   1. score ≥ AUTO_EXECUTE_MIN_SCORE  AND  confidence in {High, Medium}   → normal path
#   2. score ≥ HIGH_SCORE_BYPASS_THRESHOLD                                  → model-only bypass
# Bypass path is for strong-trend regimes where the XGB model spots momentum
# the rule signals can't confirm (no BB squeeze / no compression — just
# "stock going up"). Set bypass ABOVE the score that bit RYOJ (78) to keep
# the RYOJ guardrail intact.
AUTO_EXECUTE_MIN_SCORE      = 75   # raised from 70 — confidence labels now based on
                                   # blended XGB score so Medium starts at 65;
                                   # raising the floor keeps execution selective
HIGH_SCORE_BYPASS_THRESHOLD = 85   # Low-conf trades only if model is REALLY sure
# DISABLED 2026-06-02: the "Option B" low-confidence bypass (landed 5/28) was one
# of three changes that coincided with the +$7.6k→-$5.1k swing. Reverting to the
# config that was demonstrably profitable through 5/26: only trade High/Medium
# confidence picks. Set True to re-enable the score≥85 Low-conf model-only path.
ENABLE_HIGH_SCORE_BYPASS    = False

# ── Paths ─────────────────────────────────────────────────────────────────────
LOGS_DIR            = "logs"
PREDICTIONS_CSV     = "logs/predictions.csv"   # legacy, kept for compat
MODEL_PATH          = "model/saved/xgb_model.pkl"
FEATURE_NAMES_PATH  = "model/saved/feature_names.json"
CALIBRATOR_PATH     = "model/saved/calibrator.pkl"   # Platt scaling on top of XGB
# Calibration is DISABLED for now — Platt scaling on the extreme class imbalance
# (~0.47% positive rate) squashes all calibrated probs under 0.30, which would
# make auto-execute (score ≥ 70) effectively impossible to trigger. The model
# itself is improved (AUC 0.65 → 0.76) so raw probs are still better than they
# were before. TODO: try isotonic regression OR oversample positives in the
# calibration slice, then re-enable.
ENABLE_CALIBRATION  = False

# ── Scheduler ─────────────────────────────────────────────────────────────────
SCAN_TIME_ET = "08:00"

# ── Training ──────────────────────────────────────────────────────────────────
TRAIN_YEARS        = 5     # bumped from 3 → 5 (now includes 2020 COVID + 2022 bear)
TRAIN_TEST_SPLIT   = 0.80

# ── Crypto ────────────────────────────────────────────────────────────────────
# Alpaca uses "BTC/USD" format; yfinance uses "BTC-USD"
ENABLE_CRYPTO = os.getenv("ENABLE_CRYPTO", "true").lower() == "true"
CRYPTO_UNIVERSE = {
    "BTC/USD":  "BTC-USD",   # Bitcoin
    "ETH/USD":  "ETH-USD",   # Ethereum
    "SOL/USD":  "SOL-USD",   # Solana
    "DOGE/USD": "DOGE-USD",  # Dogecoin
}
# Reverse map: yfinance symbol → Alpaca symbol
CRYPTO_YFINANCE_TO_ALPACA = {v: k for k, v in CRYPTO_UNIVERSE.items()}
CRYPTO_YFINANCE_TICKERS   = list(CRYPTO_UNIVERSE.values())   # ["BTC-USD", ...]
CRYPTO_ALPACA_TICKERS     = list(CRYPTO_UNIVERSE.keys())     # ["BTC/USD", ...]

# ── Options ───────────────────────────────────────────────────────────────────
# Requires Level 3 options approval on Alpaca — set ENABLE_OPTIONS=true only after
# applying and receiving approval in the Alpaca dashboard.
ENABLE_OPTIONS          = os.getenv("ENABLE_OPTIONS", "false").lower() == "true"  # opt-in, not opt-out
OPTIONS_MIN_SCORE       = 85   # only use options for very high-confidence picks
OPTIONS_EARNINGS_WINDOW = 7    # days: place iron butterfly within 7 days of earnings
