from __future__ import annotations
"""
Illuminati — full agent pipeline.

  ARGUS   — fetch universe OHLCV + filter
  CIPHER  — parallel Reddit + RSS + news sentiment
  PYTHIA  — XGBoost + sentiment → scored picks
  THEMIS  — Kelly Criterion position sizing
  APEX    — Alpaca execution (paper mode by default)
  ORACLE  — backfill actuals, weekly post-mortem

Run:
    python agent.py              # full scan + email + Alpaca (if configured)
    python agent.py --no-email   # skip email
    python agent.py --no-trade   # skip Alpaca execution
    python agent.py --postmortem # run learning agent only
"""
import os
import sys
import time
import pandas as pd
from datetime import datetime

from data.universe import get_universe
from data.fetcher import get_ohlcv_batch, get_earnings_days
from data.research import research_universe
from signals.sentiment import get_sentiment_with_velocity
from signals.kelly import annotate_picks
from signals.market_regime import get_market_regime
from signals.options_flow import enrich_with_options
from model.predictor import predict_universe, model_available
from analyst.claude_analyst import explain_picks
from alerts.slack import send_trade_alert, send_daily_digest
from risk.portfolio_guard import check_trade, increment_daily_count
from config import (TOP_N_CLAUDE_ANALYSIS, MIN_SCORE_TO_ALERT,
                    AUTO_EXECUTE_MIN_SCORE, BANKROLL, ENABLE_OPTIONS_FLOW,
                    ENABLE_VOLATILITY_FILTER, MAX_ENTRY_ATR_PCT)
import db


def _backfill_actual_moves(ohlcv_map: dict | None = None) -> None:
    """Fill predictions.actual_move_5d for aged (>=7d) predictions — the model's
    feedback loop. Delegates to the standalone backfiller (backfill_actuals.run),
    which queries the rows that ACTUALLY need filling.

    The previous in-line version loaded db.load_predictions() (PostgREST-capped at
    ~1000 rows = always the most-recent, all <7 days old), so its ">=7 days" filter
    skipped every single row and it backfilled 0 of 5,229 predictions in 3 weeks
    (caught 2026-06-09). run() is idempotent and cheap once caught up — it fetches
    OHLCV only when there are aged-null rows to fill (typically the day's first scan)."""
    if not db.db_available():
        return
    try:
        import backfill_actuals
        res = backfill_actuals.run()
        if res.get("written"):
            print(f"[ARGUS] Backfilled {res['written']} actual moves "
                  f"({res.get('nodata', 0)} no-data).")
    except Exception as e:
        print(f"[ARGUS] backfill skipped: {e}")


def _execute_trades(picks_df: pd.DataFrame, explanations: dict,
                    regime: dict | None = None,
                    min_score: int | None = None,
                    earnings_map: dict | None = None) -> list[dict]:
    """Auto-execute High-confidence picks via Alpaca (paper by default)."""
    from execution.alpaca import (is_configured, place_order, is_live_mode,
                                  get_positions, get_account, reset_bp_cache,
                                  place_crypto_order, place_iron_butterfly)
    from data.universe import is_crypto
    from config import (CRYPTO_YFINANCE_TO_ALPACA, ENABLE_CRYPTO,
                        ENABLE_OPTIONS, OPTIONS_MIN_SCORE, OPTIONS_EARNINGS_WINDOW)
    if not is_configured():
        print("[APEX] Alpaca not configured — skipping execution.")
        return []

    mode = "LIVE 🔴" if is_live_mode() else "PAPER 📄"
    print(f"\n[APEX] Alpaca execution ({mode})")

    # Regime gate: if market is in bear regime, skip bullish auto-exec
    if regime and not regime.get("auto_exec_ok", True):
        print(f"[APEX] ⚠ Regime gate: {regime.get('warning')} — skipping bullish auto-exec")

    # Fetch current positions + account for portfolio guard.
    # AUDIT H-14 (2026-06-09): on failure this fed an EMPTY position list into
    # check_trade — voiding the duplicate guard, position cap, and sector cap
    # exactly when Alpaca is flaky. FAIL CLOSED: no position snapshot, no entries.
    try:
        open_positions  = get_positions()
        account         = get_account()
        portfolio_value = account.get("portfolio_value", BANKROLL)
    except Exception as _pe:
        print(f"[APEX] Could not fetch positions/account ({_pe}) — "
              f"SKIPPING all entries this scan (fail-closed)")
        return []

    _effective_min_score = min_score if min_score is not None else AUTO_EXECUTE_MIN_SCORE
    results    = []
    # Reset per-scan DT-BP exhaustion cache so a previous scan's state
    # doesn't carry over.
    reset_bp_cache()

    # Track positions and trades opened THIS run so limits are enforced
    # even before Alpaca fills propagate back through the API
    _new_positions = 0
    _new_trades    = 0

    # ── Auto-execute gate (Option B — 2026-05-28) ──────────────────────────
    # OLD rule: score ≥ MIN  AND  confidence in {High, Medium}
    # NEW rule: score ≥ HIGH_SCORE_BYPASS  (model-only path, very high bar)
    #       OR (score ≥ MIN  AND  confidence in {High, Medium})
    #
    # Rationale: in strong uptrend regimes (today: bull, VIX 15, +11% vs 200MA)
    # most picks are pure trend-continuation — the model spots them fine but
    # the rule signals (BB squeeze / ATR compress / volume surge) don't fire
    # because there's no compression pattern, it's just "stocks going up."
    # Allowing the very-high-score model-only path catches those without
    # opening the RYOJ trap (RYOJ scored 78 not 85).
    from config import HIGH_SCORE_BYPASS_THRESHOLD
    try:
        from config import ENABLE_HIGH_SCORE_BYPASS
    except ImportError:
        ENABLE_HIGH_SCORE_BYPASS = False
    _ALLOWED_CONFIDENCE = {"High", "Medium"}
    score_ok       = picks_df["score"] >= _effective_min_score
    # Bypass DISABLED by default (2026-06-02 revert): only when explicitly enabled
    # does a score≥85 Low-confidence pick qualify. Otherwise it's purely the
    # score+confidence gate — the rule that was profitable through 5/26.
    if ENABLE_HIGH_SCORE_BYPASS:
        high_score_ok = picks_df["score"] >= HIGH_SCORE_BYPASS_THRESHOLD
    else:
        high_score_ok = picks_df["score"] < 0   # never true → bypass off
    confidence_ok  = picks_df.get("confidence", "Low").isin(_ALLOWED_CONFIDENCE)
    qualifies      = high_score_ok | (score_ok & confidence_ok)

    # ── Raw-model conviction gate (2026-06-10, walk-forward backtest) ──────────
    # The edge is in the RAW xgb_prob top tier, not the blended score. Require
    # xgb_prob >= MIN_XGB_PROB_TO_TRADE as an ADDITIONAL filter — this is the
    # only slice that cleared PF 1.3 after real costs + the exit stack. Set the
    # config knob to 0.0 to disable. Picks lacking xgb_prob (defensive) are kept
    # only if they already qualified, so a missing column can't widen trading.
    try:
        from config import MIN_XGB_PROB_TO_TRADE as _MIN_PROB
    except ImportError:
        _MIN_PROB = 0.0
    if _MIN_PROB > 0 and "xgb_prob" in picks_df.columns:
        conviction_ok = picks_df["xgb_prob"].fillna(0) >= _MIN_PROB
        _pre = int(qualifies.sum())
        qualifies = qualifies & conviction_ok
        _post = int(qualifies.sum())
        if _pre != _post:
            print(f"[THEMIS] Conviction gate: {_pre} → {_post} pick(s) "
                  f"(xgb_prob ≥ {_MIN_PROB:.2f} — the only tier with after-cost edge)")
    auto_picks     = picks_df[qualifies]

    # Log what got dropped — and split the reason (low conf vs below high-score bypass)
    dropped = picks_df[score_ok & ~qualifies]
    if not dropped.empty:
        print(f"[THEMIS] Skipped {len(dropped)} pick(s) — score≥{_effective_min_score} "
              f"but neither score≥{HIGH_SCORE_BYPASS_THRESHOLD} (model bypass) "
              f"nor confidence≥Medium:")
        for _, _r in dropped.iterrows():
            print(f"         · {_r['ticker']:6s}  score={_r['score']:.0f}  dir={_r.get('direction','?')}  conf={_r.get('confidence','?')}")
    # Also log what got through via the high-score bypass — useful to track
    # whether the new path is causing problems
    bypass_picks = picks_df[high_score_ok & ~confidence_ok]
    if not bypass_picks.empty:
        print(f"[THEMIS] Allowed {len(bypass_picks)} pick(s) via HIGH-SCORE BYPASS "
              f"(score≥{HIGH_SCORE_BYPASS_THRESHOLD}, Low confidence):")
        for _, _r in bypass_picks.iterrows():
            print(f"         · {_r['ticker']:6s}  score={_r['score']:.0f}  dir={_r.get('direction','?')}")
    # ── Block-reason recorder ───────────────────────────────────────────────
    # Persist WHY an eligible pick didn't execute so the dashboard can show
    # the specific reason in its "ELIGIBLE — guard blocked" tooltip instead
    # of the user digging through scan logs. Written to the trades table as a
    # status="blocked" row (same pattern as vigil_alert/aegis_rescue/closed).
    # These rows are excluded from FIFO matching, recent-trades, and
    # partial-exit history (all of which filter by side/status).
    import datetime as _dt_mod; _today_str = _dt_mod.date.today().strftime("%Y-%m-%d")
    def _record_block(_tk, _reason):
        try:
            if db.db_available():
                db.save_trade({
                    "order_id": f"blocked-{_tk}-{_today_str}",
                    "ticker":   _tk,
                    "side":     "blocked",
                    "dollar_amount": 0,
                    "mode":     "LIVE" if os.getenv("ALPACA_LIVE_MODE", "").lower() == "true" else "PAPER",
                    "status":   "blocked",
                    "reason":   _reason[:300],
                    "timestamp": _dt_mod.datetime.now().isoformat(),
                })
        except Exception as _rbe:
            # H-7 fix: was a silent bare except — the NameError above hid here
            # for weeks and no block reason ever reached the dashboard.
            print(f"[THEMIS] _record_block failed for {_tk}: {_rbe}")

    for _, row in auto_picks.iterrows():
        ticker    = row["ticker"]
        direction = row.get("direction", "bullish")
        dollar    = row.get("dollar_amount", 0)
        duration  = row.get("duration", "")
        # FORCE_DAY_TRADES: override every signal to day-trade mode so DUSK closes
        # it by 3:50 PM ET — zero overnight exposure. Bracket uses tighter params
        # (-1.5% stop / +3% TP). Data: 68% wr / +$92/trade vs swing 58% / -$13.
        try:
            from config import FORCE_DAY_TRADES as _fdt
        except ImportError:
            _fdt = False
        if _fdt:
            duration = "1d (day trade)"
        _cd_msg   = None   # reset each iteration — prevents leak from prior ticker (C-1 audit fix)
        if dollar <= 0:
            _record_block(ticker, "Kelly sizing returned $0 — no position size allocated")
            continue

        # ── Long-only mode (2026-06-09, 60d autopsy) ─────────────────────────
        # Shorts: -$1,596 / PF 0.57 over 147 round-trips; longs +$3,280 / PF 1.23.
        # Skip bearish picks here so they don't consume daily-trade slots;
        # place_order() has the matching hard gate as the safety net.
        try:
            from config import ENABLE_SHORTS as _shorts_ok
        except ImportError:
            _shorts_ok = False
        if direction == "bearish" and not _shorts_ok:
            _msg = "Long-only mode — bearish picks not traded (shorts PF 0.57 over 60d)"
            print(f"  {ticker}: SKIPPED — {_msg}")
            _record_block(ticker, _msg)
            continue
        # AUDIT C-1 (2026-06-09): 'mixed' = scorer's tied/zero-vote label, i.e.
        # explicitly directionless. The executor's old binary side selection
        # turned these into SHORTS. Never trade a pick with no direction.
        if direction not in ("bullish", "bearish"):
            _msg = f"Direction '{direction}' is directionless — no trade (audit C-1)"
            print(f"  {ticker}: SKIPPED — {_msg}")
            _record_block(ticker, _msg)
            continue

        # ── Volatility filter (added 2026-06-02) ─────────────────────────────
        # Block auto-exec on ultra-volatile names (ATR% > MAX_ENTRY_ATR_PCT).
        # Backtest of real stop-outs: these junk/micro-cap names (e.g. YMAT 37%
        # ATR, RYOJ 26%) ride any stop straight down and are the true -30% days.
        # No stop setting fixes them — the only fix is not trading them.
        _atr_pct = float(row.get("atr_pct", 0.0) or 0.0)
        if ENABLE_VOLATILITY_FILTER and _atr_pct > MAX_ENTRY_ATR_PCT:
            _msg = (f"Volatility filter — ATR {_atr_pct*100:.1f}% exceeds "
                    f"{MAX_ENTRY_ATR_PCT*100:.0f}% cap (too volatile to swing-trade)")
            print(f"  {ticker}: BLOCKED — {_msg}")
            _record_block(ticker, _msg)
            continue
        # Audit M4 (2026-06-02): atr_pct == 0 means INSUFFICIENT HISTORY (new IPO,
        # thin/illiquid name) — NOT "low volatility." technicals returns pct=0.0
        # when there aren't enough rows. Such names are exactly the junk profile
        # the filter targets, and a 0 would otherwise bypass BOTH the filter and
        # ATR sizing (falling back to the flat 3% stop). Treat unknown as block.
        if ENABLE_VOLATILITY_FILTER and _atr_pct <= 0:
            _msg = ("Volatility filter — ATR unknown (insufficient price history); "
                    "blocking auto-exec on thin/new ticker")
            print(f"  {ticker}: BLOCKED — {_msg}")
            _record_block(ticker, _msg)
            continue

        # Hard stop — enforce position + trade limits using in-run counters
        # N-5 fix: refresh open_positions live from Alpaca every 5 picks so
        # the position-count gate doesn't get fooled by stale state from the
        # AEGIS pre-scan (which may have closed penny stocks) or manual closes.
        from risk.portfolio_guard import MAX_OPEN_POSITIONS, MAX_DAILY_TRADES
        if (_new_positions % 5) == 0:
            try:
                open_positions = get_positions() or open_positions
            except Exception:
                pass
        current_positions = len(open_positions) + _new_positions
        if current_positions >= MAX_OPEN_POSITIONS:
            _msg = f"Position limit reached ({current_positions}/{MAX_OPEN_POSITIONS} open)"
            print(f"  {ticker}: BLOCKED — {_msg}")
            _record_block(ticker, _msg)
            continue
        if _new_trades >= MAX_DAILY_TRADES:
            _msg = f"Daily trade limit reached ({_new_trades}/{MAX_DAILY_TRADES} today)"
            print(f"  {ticker}: BLOCKED — {_msg}")
            _record_block(ticker, _msg)
            break

        # Regime gate — skip mixed/bullish trades in bear regime
        if regime and not regime.get("auto_exec_ok", True):
            if direction in ("bullish", "mixed"):
                _msg = f"Bear regime active — {direction} auto-exec paused"
                print(f"  {ticker}: SKIPPED — {_msg}")
                _record_block(ticker, _msg)
                continue

        # Regime multiplier — reduce bullish position size when market is risky
        if regime and direction == "bullish":
            multiplier = regime.get("bull_multiplier", 1.0)
            if multiplier < 1.0:
                dollar = round(dollar * multiplier, 2)
                print(f"  {ticker}: position reduced to ${dollar:.0f} (regime multiplier {multiplier:.0%})")

        # Portfolio guard check (duplicate detection, sector limits, etc.)
        ok, guard_reason = check_trade(ticker, dollar, direction, open_positions, portfolio_value)
        if not ok:
            print(f"  {ticker}: BLOCKED by portfolio guard — {guard_reason}")
            _record_block(ticker, guard_reason)
            continue
        if guard_reason != "ok":
            print(f"  {ticker}: {guard_reason}")

        reason = explanations.get(ticker, "")[:120]

        # ── Execution path tag (for win-rate tracking by gate type) ──────────
        # model_bypass: score≥HIGH_SCORE_BYPASS_THRESHOLD with Low confidence
        # rule_confirmed: qualified via score+confidence gate (rules also fired)
        _is_model_bypass = (
            row["score"] >= HIGH_SCORE_BYPASS_THRESHOLD and
            row.get("confidence", "Low") not in _ALLOWED_CONFIDENCE
        )
        _execution_path = "model_bypass" if _is_model_bypass else "rule_confirmed"

        # ── Cooldown rule: block counter-direction after big winner ──────────
        # If a ticker closed with +5%+ gain recently, block the opposite direction
        # for COOLDOWN_HOURS to prevent the system fighting itself (e.g. long DELL
        # made +15% then immediately shorting DELL)
        try:
            from config import COOLDOWN_WIN_THRESHOLD, COOLDOWN_HOURS
            from execution.alpaca import get_closed_trade_pnl
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            _recent = get_closed_trade_pnl(days=2)
            # UTC everywhere: closed_at is UTC; bare _dt.now() is LOCAL (Mountain on
            # the Mac launchd), which skewed this cooldown window by ~6-7h once scans
            # moved to local execution. (audit 2026-06-08)
            _cutoff = _dt.now(_tz.utc) - _td(hours=COOLDOWN_HOURS)
            for _ct in _recent:
                if _ct["ticker"] != ticker:
                    continue
                try:
                    _closed_at = _dt.strptime(_ct["closed_at"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=_tz.utc)
                except Exception:
                    continue
                if _closed_at < _cutoff:
                    continue
                _prior_pct = _ct["realized_pnl_pct"] / 100.0
                if abs(_prior_pct) < COOLDOWN_WIN_THRESHOLD:
                    continue
                # CRITICAL audit C-1: Was inferring direction from P&L sign,
                # which mislabels profitable SHORTS as "long". The trade dict
                # already carries `side` — use it. Falls back to old behavior
                # only if `side` is missing.
                _side = str(_ct.get("side", "")).lower()
                if _side in ("long", "short"):
                    _prior_long = (_side == "long")
                else:
                    _prior_long = _ct["realized_pnl"] > 0   # legacy fallback
                _cur_bearish = direction == "bearish"
                if _prior_long and _cur_bearish and _prior_pct > 0:
                    _hrs = int(((_dt.now(_tz.utc)-_closed_at).total_seconds()/3600))
                    _cd_msg = (f"Cooldown — closed long +{_prior_pct:.1%} {_hrs}h ago, "
                               f"blocking bearish signal for {COOLDOWN_HOURS}h")
                    print(f"  {ticker}: COOLDOWN — {_cd_msg}")
                    dollar = 0  # skip this trade
                    break
                if not _prior_long and not _cur_bearish and _prior_pct > 0:
                    _hrs = int(((_dt.now(_tz.utc)-_closed_at).total_seconds()/3600))
                    _cd_msg = (f"Cooldown — closed short +{_prior_pct:.1%} {_hrs}h ago, "
                               f"blocking bullish signal for {COOLDOWN_HOURS}h")
                    print(f"  {ticker}: COOLDOWN — {_cd_msg}")
                    dollar = 0
                    break
        except Exception:
            pass

        if dollar <= 0:
            _record_block(ticker, _cd_msg or "Dollar amount reduced to $0")
            continue

        # ── Route: crypto vs equity ───────────────────────────────────────
        if is_crypto(ticker) and ENABLE_CRYPTO:
            alpaca_sym = CRYPTO_YFINANCE_TO_ALPACA.get(ticker, ticker)
            result = place_crypto_order(alpaca_sym, dollar, direction, reason,
                                        execution_path=_execution_path,
                                        atr_pct=_atr_pct)
        else:
            result = place_order(ticker, dollar, direction, reason,
                                 execution_path=_execution_path,
                                 duration=duration, atr_pct=_atr_pct)

        results.append(result)
        _status = result.get("status")
        print(f"  {ticker}: {_status} ${dollar:.0f} {direction}")
        # Finding #11 (2026-06-08): a "submitted_unprotected" result is a NAKED
        # market-order entry (bracket failed → simple-order fallback could not
        # attach a stop). Fire a clearly-worded, DISTINCT alert so the operator
        # knows the position is UNPROTECTED until AEGIS re-protects it, instead
        # of treating it like a normal protected bracket entry.
        if _status == "submitted_unprotected":
            try:
                from alerts.slack import _post
                _sl = result.get("stop_loss")
                _post({"text": (
                    f"⚠️ *UNPROTECTED ENTRY — {ticker} {direction.upper()}*\n"
                    f">Bracket failed; placed a NAKED market order "
                    f"(${dollar:.0f}) and could NOT attach a stop"
                    f"{f' (intended ~${_sl:.2f})' if _sl else ''}.\n"
                    f">Position is UNPROTECTED — AEGIS will attempt to protect it "
                    f"on its next run. Check manually."
                )})
            except Exception:
                pass
        else:
            send_trade_alert(result)
        # Both "submitted" and "submitted_unprotected" are real entries → count them.
        if _status in ("submitted", "submitted_unprotected"):
            increment_daily_count()
            _new_positions += 1
            _new_trades    += 1

    # ── Options pass: iron butterfly on high-score earnings plays ─────────
    if ENABLE_OPTIONS and earnings_map:
        from execution.options_utils import get_atm_strike, get_next_expiry, get_wing_increment
        from datetime import datetime, timedelta
        options_candidates = picks_df[
            (picks_df["score"] >= OPTIONS_MIN_SCORE) &
            (~picks_df["ticker"].apply(is_crypto))
        ]
        for _, row in options_candidates.iterrows():
            ticker       = row["ticker"]
            days_to_earn = earnings_map.get(ticker)
            if days_to_earn is None or not (0 <= days_to_earn <= OPTIONS_EARNINGS_WINDOW):
                continue
            try:
                from data.fetcher import get_ohlcv
                df_c = get_ohlcv(ticker, period="5d")
                price = float(df_c["Close"].iloc[-1]) if not df_c.empty else None
                if not price:
                    continue
                earnings_dt = datetime.today() + timedelta(days=days_to_earn)
                expiry      = get_next_expiry(earnings_dt)
                atm         = get_atm_strike(price)
                wing        = get_wing_increment(price)
                reason      = explanations.get(ticker, "")[:120]
                print(f"\n[APEX] Options play: {ticker} score={row['score']:.0f} "
                      f"earnings in {days_to_earn}d → iron butterfly")
                result = place_iron_butterfly(ticker, expiry, atm, wing, reason=reason)
                results.append(result)
                if result.get("status") == "submitted":
                    _new_trades += 1
            except Exception as oe:
                print(f"[APEX] Options play failed for {ticker}: {oe}")

    return results


def run_scan(send_email: bool = True,
             execute_trades: bool = True,
             verbose: bool = True) -> pd.DataFrame:
    start = time.time()
    today = datetime.today().strftime("%Y-%m-%d")

    # ── Deduplication guard for fallback cron triggers ───────────────────
    # Fallback triggers set SCAN_FALLBACK=true. If today already has predictions,
    # the primary cron ran fine → skip. If no predictions yet, run as normal.
    if os.getenv("SCAN_FALLBACK") == "true" and db.db_available():
        try:
            _existing = db.load_predictions_for_date(today)
            if _existing:
                print(f"[ARGUS] ⏭  Fallback trigger skipped — {len(_existing)} predictions "
                      f"already written for {today} (primary cron ran fine).")
                return pd.DataFrame()
            else:
                print(f"[ARGUS] 🔁 Fallback trigger activating — no predictions yet for {today}.")
        except Exception:
            pass   # if check fails, proceed normally

    print(f"\n{'='*62}")
    print(f"  Illuminati  |  {today}")
    print(f"  ARGUS · CIPHER · PYTHIA · THEMIS · APEX · ORACLE")
    print(f"  Model:   {'XGBoost ✓' if model_available() else 'Rule-based (no model)'}")
    print(f"  DB:      {'Supabase ✓' if db.db_available() else 'local only'}")
    print(f"  Bankroll: ${BANKROLL:,.0f}")
    print(f"{'='*62}\n")

    # ── ORACLE directives — read before every scan ────────────────
    oracle_directives: dict = {}
    try:
        from analyst.oracle import get_latest_directives
        oracle_directives = get_latest_directives()
        if oracle_directives:
            print(f"[ORACLE] Active directive → {oracle_directives.get('scanner_directive','none')}")
            if oracle_directives.get("avoid_sectors"):
                print(f"[ORACLE] Avoiding sectors: {oracle_directives['avoid_sectors']}")
            if oracle_directives.get("favor_directions"):
                print(f"[ORACLE] Favoring: {oracle_directives['favor_directions']}")
    except Exception as e:
        print(f"[ORACLE] Could not load directives: {e}")

    # Apply ORACLE threshold adjustment.
    # AUDIT H-8/12/18/25 (2026-06-09): this is raw LLM JSON — validate + clamp.
    # int(None) crashed the scan; a negative adjust silently LOWERED the 80
    # floor. ORACLE may only make the gate STRICTER (0..+10), never looser.
    from config import AUTO_EXECUTE_MIN_SCORE as _BASE_MIN_SCORE
    try:
        _raw_adj = oracle_directives.get("confidence_threshold_adjust", 0)
        _adj = int(float(_raw_adj)) if _raw_adj is not None else 0
    except (TypeError, ValueError):
        print(f"[ORACLE] Ignoring non-numeric confidence_threshold_adjust: {_raw_adj!r}")
        _adj = 0
    _adj = max(0, min(10, _adj))   # clamp: stricter only, capped at +10
    _effective_min_score = _BASE_MIN_SCORE + _adj
    if _effective_min_score != _BASE_MIN_SCORE:
        print(f"[ORACLE] Adjusted auto-execute threshold: {_BASE_MIN_SCORE} → {_effective_min_score}")

    # ── 0a. AEGIS pre-scan sweep ─────────────────────────────────
    # H-11 fix: previously AEGIS only ran AFTER the scan finished. If the scan
    # hung on yfinance / Alpaca data, AEGIS never ran on this trigger — the
    # exact failure mode the piggyback was supposed to prevent. Now it runs
    # FIRST so trailing stops + naked rescue always get a touch every cycle.
    try:
        from execution.alpaca import trail_positions as _trail_pre, is_configured as _alp_ok_pre
        if _alp_ok_pre():
            print(f"[AEGIS pre-scan] Trailing stop + partial exit sweep...")
            _pre_results = _trail_pre()
            if _pre_results:
                print(f"[AEGIS pre-scan] {len(_pre_results)} actions: "
                      f"{', '.join(r['ticker'] for r in _pre_results[:8])}"
                      + (f" +{len(_pre_results)-8} more" if len(_pre_results) > 8 else ""))
    except Exception as _pe:
        print(f"[AEGIS pre-scan] error: {_pe}")

    # ── 0. Market Regime ─────────────────────────────────────────
    print(f"[REGIME] VIX + SPY trend + sector breadth")
    _neutral_regime = {"regime":"neutral","vix":20.0,"vix_level":"normal",
                       "spy_vs_200ma_pct":0.0,"spy_vs_50ma_pct":0.0,"spy_trend":"sideways",
                       "sectors_above_50ma":5,"breadth":"normal","bull_multiplier":1.0,
                       "auto_exec_ok":True,"warning":None}
    try:
        regime = get_market_regime()
        regime_icon = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}.get(regime["regime"], "⚪")
        print(f"      {regime_icon} {regime['regime'].upper()} · VIX {regime['vix']} · "
              f"SPY {regime['spy_vs_200ma_pct']:+.1f}% vs 200MA · "
              f"{regime['sectors_above_50ma']}/11 sectors above 50MA")
        if regime.get("warning"):
            print(f"      ⚠ {regime['warning']}")
    except Exception as e:
        print(f"      ⚠ Regime check failed ({e}) — proceeding as neutral")
        regime = _neutral_regime

    # ── 1. Scan ───────────────────────────────────────────────────
    from config import FUTURES
    tickers = [t for t in get_universe() if t not in FUTURES]  # exclude futures — not tradeable via Alpaca equities
    print(f"\n[ARGUS] SCAN — {len(tickers)} tickers")
    ohlcv_map = get_ohlcv_batch(tickers, period="1y", chunk_size=50)
    print(f"      Got data for {len(ohlcv_map)} tickers")
    _backfill_actual_moves(ohlcv_map)

    # ── 2. Research (parallel) ────────────────────────────────────
    print(f"\n[CIPHER] RESEARCH — parallel Reddit + RSS + news")
    blended_sentiment = research_universe(tickers)

    # Persist sentiment velocity via existing cache
    sentiment_map: dict = {}
    for ticker in tickers:
        cached = get_sentiment_with_velocity(ticker)
        blended = blended_sentiment.get(ticker, {})
        # Blend cached velocity with new multi-source score
        combined_score = (cached.get("score", 0.0) * 0.4 +
                          blended.get("score", 0.0) * 0.6)
        sentiment_map[ticker] = {
            **blended,
            "score": round(combined_score, 4),
            "velocity": cached.get("velocity", 0.0),
            "spike": abs(cached.get("velocity", 0.0)) >= 0.3 or blended.get("score", 0) > 0.4,
        }

    # ── 3. Predict ────────────────────────────────────────────────
    print(f"\n[PYTHIA] PREDICT — XGBoost + blended sentiment")
    # Fetch earnings dates in parallel (610 sequential calls would timeout GitHub Actions)
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    earnings_map: dict = {}
    print(f"      Fetching earnings dates for {len(tickers)} tickers (parallel)...")
    with ThreadPoolExecutor(max_workers=30) as _pool:
        _futures = {_pool.submit(get_earnings_days, t): t for t in tickers}
        for _fut in _as_completed(_futures):
            t = _futures[_fut]
            try:
                earnings_map[t] = _fut.result()
            except Exception:
                earnings_map[t] = None
    print(f"      Earnings fetch done ({sum(1 for v in earnings_map.values() if v is not None)} with upcoming dates)")

    picks_df = predict_universe(tickers, ohlcv_map, sentiment_map, earnings_map)

    if picks_df is None or picks_df.empty:
        print("[PYTHIA] No setups above threshold today.")
        return pd.DataFrame()

    print(f"      {len(picks_df)} setups flagged (score ≥ {MIN_SCORE_TO_ALERT})")

    # ORACLE direction bias — boost favored direction picks
    _favor = oracle_directives.get("favor_directions", "")
    if _favor in ("bullish", "bearish") and not picks_df.empty:
        mask = picks_df["direction"] == _favor
        picks_df.loc[mask, "score"] = (picks_df.loc[mask, "score"] + 3).clip(upper=100)
        picks_df.loc[~mask & (picks_df["direction"] != "mixed"), "score"] = (picks_df.loc[~mask & (picks_df["direction"] != "mixed"), "score"] - 2).clip(lower=0)
        print(f"[ORACLE] Score bias applied — favoring {_favor}")

    # ── 3.5. Enrich top picks with options flow ───────────────────
    if ENABLE_OPTIONS_FLOW and not picks_df.empty:
        top_picks = picks_df[picks_df["score"] >= 60].head(15)
        if not top_picks.empty:
            try:
                print(f"\n[3.5] ENRICH — options flow ({len(top_picks)} tickers)")
                enriched = enrich_with_options(top_picks, verbose=True)
                for idx in enriched.index:
                    t    = enriched.loc[idx, "ticker"]
                    mask = picks_df["ticker"] == t
                    picks_df.loc[mask, "score"] = enriched.loc[idx, "score"]
                    for col in ["options_side","options_pcr","options_unusual","options_detail"]:
                        if col in enriched.columns:
                            picks_df.loc[mask, col] = enriched.loc[idx, col]
                picks_df = picks_df.sort_values("score", ascending=False).reset_index(drop=True)
            except Exception as e:
                print(f"      ⚠ Options enrichment failed ({e}) — continuing without it")

    # ── 4. Risk — Kelly Criterion ─────────────────────────────────
    # Bankroll = LIVE account equity at prior close (get_bankroll), not the
    # static config number — so sizing tracks the real account (Renato 2026-06-03).
    try:
        from execution.alpaca import get_bankroll as _get_bankroll
        _bankroll = _get_bankroll()
    except Exception:
        _bankroll = BANKROLL
    print(f"\n[THEMIS] RISK — Kelly Criterion sizing (bankroll ${_bankroll:,.0f} · live account)")
    picks_df = annotate_picks(picks_df, bankroll=_bankroll)
    for _, row in picks_df.head(10).iterrows():
        print(f"      {row['ticker']:6s}  score={row['score']:.0f}  "
              f"${row.get('dollar_amount',0):,.0f}  ({row.get('risk_level','')})")

    # Claude explanations
    print(f"\n      Generating Claude analysis for top {TOP_N_CLAUDE_ANALYSIS}...")
    explanations = explain_picks(picks_df, top_n=TOP_N_CLAUDE_ANALYSIS,
                                 oracle_directive=oracle_directives.get("scanner_directive", ""))

    # ── 5. Learn — persist + optionally execute ───────────────────
    print(f"\n[ORACLE] LEARN — persist predictions")
    if db.db_available():
        rows = picks_df.copy()
        rows["date"] = today
        rows["actual_move_5d"] = None
        # FORCE_DAY_TRADES: persist duration as "1d (day trade)" so the SAVED
        # prediction matches how it actually trades — dashboard shows DAY (not
        # SWING) and DUSK reads "1d" and closes it at 3:50 PM. Without this the
        # scorer's "5-7d" leaks into the DB even though the bracket is day-trade.
        try:
            from config import FORCE_DAY_TRADES as _fdt
        except ImportError:
            _fdt = False
        if _fdt and "duration" in rows.columns:
            rows["duration"] = "1d (day trade)"
        # AUDIT H-19 (2026-06-09): every 30-min scan upserts the same
        # (date,ticker) row, so a later weaker scan OVERWROTE the score that
        # actually triggered a trade — corrupting the edge measurement the
        # go-live decision rests on. Keep the day's STRONGEST row per ticker:
        # only write rows that are new today or improve on the stored score.
        _out_rows = rows.to_dict(orient="records")
        try:
            _existing = {r["ticker"]: r.get("score") or 0
                         for r in db.load_predictions_for_date(today)}
            if _existing:
                _before = len(_out_rows)
                _out_rows = [r for r in _out_rows
                             if (r.get("score") or 0) > (_existing.get(r.get("ticker"), -1))]
                if len(_out_rows) < _before:
                    print(f"      Keeping {_before - len(_out_rows)} stronger "
                          f"earlier-scan row(s) (score-preserving upsert)")
        except Exception as _me:
            print(f"      score-preserve merge skipped ({_me}) — writing all rows")
        db.append_predictions(_out_rows)
        print(f"      Saved {len(_out_rows)} predictions to Supabase")

    # Alpaca execution
    if execute_trades:
        trade_results = _execute_trades(picks_df, explanations, regime=regime,
                                        min_score=_effective_min_score,
                                        earnings_map=earnings_map)
    else:
        print("      [APEX] Alpaca execution skipped (--no-trade)")
        trade_results = []

    # ── AEGIS piggyback ─────────────────────────────────────────────────────
    # The dedicated trail_stops.yml workflow runs every 30 min but GitHub's
    # free-tier scheduler skips crons under load. We've gone 24+ hours with
    # zero AEGIS runs. So every daily_scan ALSO triggers AEGIS at the end —
    # guarantees ≥12 AEGIS runs per market day even if the dedicated workflow
    # is completely skipped.
    try:
        from execution.alpaca import trail_positions, is_configured as _alp_ok
        if _alp_ok():
            print(f"\n  [AEGIS piggyback] Trailing stop + partial exit sweep...")
            aegis_results = trail_positions()
            if aegis_results:
                print(f"  [AEGIS piggyback] {len(aegis_results)} actions: "
                      f"{', '.join(r['ticker'] for r in aegis_results[:8])}"
                      + (f" +{len(aegis_results)-8} more" if len(aegis_results) > 8 else ""))
            else:
                print(f"  [AEGIS piggyback] No stops to upgrade right now.")
    except Exception as _ae:
        print(f"  [AEGIS piggyback] error: {_ae}")

    elapsed = time.time() - start
    print(f"\n{'='*62}")
    print(f"  Done in {elapsed:.1f}s | {len(picks_df)} setups | "
          f"{len(trade_results)} trades placed")
    print(f"{'='*62}\n")

    # ── Slack scan digest ─────────────────────────────────────────────────────
    # Per-scan summary to Slack. OFF by default (Renato 2026-06-11): the scan runs
    # every ~30 min so this fired all day. Trade fills, the EOD summary, and
    # health/ZEUS reports still post; only this noisy per-scan digest is gated.
    # Flip SLACK_SCAN_DIGEST=True in config to restore it.
    try:
        from config import SLACK_SCAN_DIGEST as _SCAN_DIGEST
    except ImportError:
        _SCAN_DIGEST = False
    if send_email and _SCAN_DIGEST:
        try:
            send_daily_digest(picks_df, explanations)
        except Exception as _de:
            print(f"  [slack] scan digest send failed: {_de}")

    return picks_df


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--postmortem" in args:
        from analyst.learning_agent import run_postmortem
        run_postmortem()
    else:
        run_scan(
            send_email="--no-email" not in args,
            execute_trades="--no-trade" not in args,
        )
