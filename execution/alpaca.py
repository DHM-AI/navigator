from __future__ import annotations
"""
Alpaca execution module — bracket orders with automatic stop loss + take profit.

Every trade placed is a BRACKET ORDER:
  - Entry at market
  - Stop loss:   -3% from entry  (long) / +3% (short)
  - Take profit: +5% from entry  (long) / -5% (short)

Alpaca monitors these automatically — you don't need to watch.

SAFETY DEFAULTS:
  - Paper trading mode unless ALPACA_LIVE_MODE=true
  - Max single position: 10% of bankroll (Kelly hard cap)
  - Daily loss limit: 5% — agent stops all trading if hit
  - Minimum score ≥ 70 for auto-execution
  - All trades logged to Supabase

To enable live trading:
  Set ALPACA_BASE_URL=https://api.alpaca.markets AND ALPACA_LIVE_MODE=true
  DO NOT go live before 30 days of paper trading validation.
"""
import os
from datetime import datetime
from config import (BANKROLL, ALPACA_API_KEY, ALPACA_SECRET_KEY,
                    ALPACA_BASE_URL, ALPACA_LIVE_MODE,
                    MAX_POSITION_PCT, DAILY_LOSS_LIMIT_PCT,
                    KELLY_LOSS_PCT, MOVE_TARGET_PCT,
                    DAY_TRADE_STOP_PCT, DAY_TRADE_TARGET_PCT,
                    ENABLE_ATR_STOPS, ATR_STOP_MULT,
                    ATR_STOP_FLOOR_PCT, ATR_STOP_CAP_PCT)


def _resolve_stop_and_size(dollar_amount: float, atr_pct: float,
                           is_day_trade: bool, ticker: str = "") -> tuple:
    """Return (stop_pct, effective_notional) for a bracket entry.

    Swing trades use a volatility-adjusted stop = ATR_STOP_MULT x ATR%, clamped
    to [ATR_STOP_FLOOR_PCT, ATR_STOP_CAP_PCT]. Position notional is then rescaled
    so the DOLLAR risk per trade stays constant regardless of stop width:
    risk budget = dollar_amount x floor%  (the original 3% risk), and
    effective_notional = risk_budget / stop_pct. So a wider (more volatile) stop
    -> smaller position, same $ at risk. Floor stop -> notional unchanged.

    DAY trades and the disabled/zero-ATR path keep the legacy fixed stop and the
    full notional (no rescale) — identical to prior behavior.
    """
    if is_day_trade:
        return DAY_TRADE_STOP_PCT, dollar_amount
    if not ENABLE_ATR_STOPS or not atr_pct or atr_pct <= 0:
        return KELLY_LOSS_PCT, dollar_amount
    raw = ATR_STOP_MULT * float(atr_pct)
    stop_pct = max(ATR_STOP_FLOOR_PCT, min(raw, ATR_STOP_CAP_PCT))
    # Keep $ risk constant vs the floor (legacy 3%) baseline.
    risk_budget = dollar_amount * ATR_STOP_FLOOR_PCT
    effective_notional = risk_budget / stop_pct if stop_pct > 0 else dollar_amount
    # Never upsize beyond the requested dollar amount (only hold or shrink).
    effective_notional = min(effective_notional, dollar_amount)
    if abs(effective_notional - dollar_amount) > 1:
        print(f"[APEX] {ticker}: ATR stop {stop_pct*100:.1f}% "
              f"(ATR {atr_pct*100:.1f}% x{ATR_STOP_MULT}) -> notional "
              f"${dollar_amount:.0f} -> ${effective_notional:.0f} (constant $ risk)")
    return stop_pct, effective_notional


def _get_client():
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY,
                             paper=not ALPACA_LIVE_MODE)
    except ImportError:
        raise RuntimeError("alpaca-py not installed. Run: pip install alpaca-py")


def is_configured() -> bool:
    return bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


def is_live_mode() -> bool:
    return ALPACA_LIVE_MODE


def get_account() -> dict:
    client = _get_client()
    acct = client.get_account()
    # daytrading_buying_power is what bracket orders (TimeInForce.DAY) actually
    # consume. Different from regt_buying_power (overnight) and buying_power
    # (overall). With many open positions, DT-BP is what gets exhausted first.
    dt_bp = getattr(acct, "daytrading_buying_power", None) or getattr(acct, "day_trading_buying_power", None)
    return {
        "buying_power":            float(acct.buying_power),
        "daytrading_buying_power": float(dt_bp) if dt_bp is not None else float(acct.buying_power),
        "regt_buying_power":       float(getattr(acct, "regt_buying_power", acct.buying_power)),
        "portfolio_value":         float(acct.portfolio_value),
        "cash":                    float(acct.cash),
        "equity":                  float(acct.equity),
        "last_equity":             float(acct.last_equity) if acct.last_equity else float(acct.equity),
        "paper":                   not ALPACA_LIVE_MODE,
    }


# ── Active order statuses (CRITICAL: covers all SDK string formats) ──────
# The Alpaca Python SDK can stringify an OrderStatus enum two ways:
#   "OrderStatus.HELD"  or  "held"
# Some statuses (ACCEPTED, PENDING_NEW) have bitten us repeatedly when only
# one form was listed. ALWAYS use this set for "is this order active?".
ACTIVE_ORDER_STATUSES = {
    "open", "new", "held", "pending_new", "accepted",
    "orderstatus.open", "orderstatus.new", "orderstatus.held",
    "orderstatus.pending_new", "orderstatus.accepted",
}


def is_active_order(o) -> bool:
    """Single source of truth: is this order currently active (held/open/etc)?"""
    return str(getattr(o, "status", "")).lower() in ACTIVE_ORDER_STATUSES


# ── Order-status normalization ─────────────────────────────────────────────
# Audit C-3/C-4/C-5: Three safety controls (MAX_DAILY_TRADES counter,
# DUSK day-trade detector, ZEUS audit) were silently broken because
# `str(OrderStatus.FILLED) == "OrderStatus.FILLED"`, never `"filled"`.
# Use this helper EVERYWHERE you compare order.status against literals.
def order_status(o) -> str:
    """Normalize Alpaca order status to a plain lowercase string.

    Handles both string forms the SDK emits:
        "filled"             → "filled"
        "OrderStatus.FILLED" → "filled"
    """
    return str(getattr(o, "status", "")).lower().replace("orderstatus.", "")


# Buying power safety factor — leaves headroom for slippage and same-scan orders
_BP_SAFETY_FACTOR = 0.90
# In-process cache: once DT-BP is found exhausted, short-circuit the rest of
# the scan so we don't fire 10 redundant API calls + log lines.
_dt_bp_exhausted_for_scan = False


def reset_bp_cache() -> None:
    """Call at start of each scan so the cache doesn't persist across scans."""
    global _dt_bp_exhausted_for_scan
    _dt_bp_exhausted_for_scan = False


def _check_buying_power(dollar_amount: float, ticker: str,
                        notify: bool = True) -> tuple[bool, str]:
    """
    Pre-trade DT-BP check. Returns (ok, reason).
    Bracket orders (TimeInForce.DAY) consume daytrading_buying_power.
    Reject early if the trade would exceed 90% of available DT-BP.

    notify: when True (default), posts a Slack alert ONCE per scan when
            DT-BP first hits exhaustion. Set False from diagnostic/test
            callers to avoid polluting Slack.
    """
    global _dt_bp_exhausted_for_scan
    if _dt_bp_exhausted_for_scan:
        return False, "day-trading buying power exhausted earlier in this scan"
    try:
        acct = get_account()
        dt_bp = acct.get("daytrading_buying_power", 0)
        usable = dt_bp * _BP_SAFETY_FACTOR
        if dollar_amount > usable:
            # Mark exhausted so subsequent orders short-circuit
            _dt_bp_exhausted_for_scan = True
            # Notify Slack once per process (not per ticker spam, not for tests)
            if notify:
                try:
                    from alerts.slack import _post
                    _post({
                        "text": (
                            f"⚠️ *DT-BP exhausted* — skipping remaining trades this scan\n"
                            f"> Available: ${dt_bp:,.0f}  ·  Need: ${dollar_amount:,.0f}  ·  "
                            f"Tried: `{ticker}`\n"
                            f"> Close some positions or wait until next session."
                        ),
                    })
                except Exception:
                    pass
            return False, (
                f"insufficient day-trading buying power "
                f"(need ${dollar_amount:,.0f}, have ${dt_bp:,.0f}, "
                f"usable ${usable:,.0f})"
            )
        return True, ""
    except Exception as e:
        return False, f"BP check failed: {e}"


def get_current_price(ticker: str) -> float | None:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req   = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote = data_client.get_stock_latest_quote(req)
        return float(quote[ticker].ask_price)
    except Exception:
        return None


def _check_daily_loss_limit() -> bool:
    """Returns True if safe to trade, False if daily loss limit breached."""
    try:
        acct        = _get_client().get_account()
        equity      = float(acct.equity)
        last_equity = float(acct.last_equity)
        daily_loss  = (equity - last_equity) / last_equity
        if daily_loss < -DAILY_LOSS_LIMIT_PCT:
            print(f"[APEX] ⚠ Daily loss limit breached ({daily_loss:.1%}). Halting trades.")
            return False
        return True
    except Exception as e:
        print(f"[APEX] ⚠ Could not check daily loss limit ({e}) — blocking trades for safety.")
        return False  # fail closed — never trade blind


def place_order(ticker: str, dollar_amount: float, direction: str,
                reason: str = "", execution_path: str = "",
                duration: str = "", atr_pct: float = 0.0) -> dict:
    """
    Place a BRACKET ORDER: entry + stop loss + take profit in one shot.

    Stop loss:   KELLY_LOSS_PCT  below entry for longs  (default 3%)
    Take profit: MOVE_TARGET_PCT above entry for longs  (default 20% ceiling)
    Reversed for shorts.

    For DAY trades (duration starts with "1d"): uses tighter bracket —
      Stop: DAY_TRADE_STOP_PCT (-1.5%)  Target: DAY_TRADE_TARGET_PCT (+3%)
    DUSK closes any remaining DAY positions at 3:50 PM ET automatically.
    """
    _is_day_trade = str(duration).startswith("1d")
    if not is_configured():
        return {"status": "skipped", "reason": "Alpaca not configured"}

    # ── Market-hours gate ─────────────────────────────────────────────────────
    # APEX must NEVER place an equity entry while the market is closed. On
    # 2026-06-01 an AMD bracket SHORT was placed at 4:09 PM ET (9 min after
    # close); it sat unfilled overnight and would have filled at the next 9:30
    # open at whatever price AMD gapped to — an uncontrolled entry, the exact
    # gap risk we've been eliminating. A DAY market order after close is junk.
    # Refuse here (equities only — crypto trades 24/7 via place_crypto_order).
    try:
        _clock = _get_client().get_clock()
        if not _clock.is_open:
            print(f"[APEX] {ticker} skipped — market is CLOSED (no entries outside RTH)")
            return {"status": "skipped", "ticker": ticker,
                    "reason": "Market closed — no entries outside regular trading hours"}
    except Exception as _clk_err:
        # FAIL CLOSED (audit 2026-06-02): previously proceeded on clock error,
        # which re-opened the exact after-hours-gap hole this gate exists to stop.
        # Fall back to an ET regular-trading-hours window check; if we cannot
        # positively confirm we're inside RTH, SKIP the entry.
        print(f"[APEX] {ticker} market-hours API check failed: {_clk_err} — falling back to ET clock")
        try:
            from datetime import datetime as _dt
            try:
                from zoneinfo import ZoneInfo
                _now_et = _dt.now(ZoneInfo("America/New_York"))
            except Exception:
                # No tz database — derive ET from UTC (EDT=-4; conservative)
                from datetime import timezone as _tz, timedelta as _td
                _now_et = _dt.now(_tz.utc).astimezone(_tz(_td(hours=-4)))
            _mins = _now_et.hour * 60 + _now_et.minute
            _is_rth = (_now_et.weekday() < 5) and (9 * 60 + 30 <= _mins < 16 * 60)
            if not _is_rth:
                print(f"[APEX] {ticker} skipped — ET fallback says market CLOSED "
                      f"({_now_et:%a %H:%M ET})")
                return {"status": "skipped", "ticker": ticker,
                        "reason": "Market closed (ET fallback) — no entries outside RTH"}
            print(f"[APEX] {ticker} ET fallback confirms RTH ({_now_et:%a %H:%M ET}) — proceeding")
        except Exception as _fb_err:
            # Cannot confirm RTH at all -> fail closed.
            print(f"[APEX] {ticker} ET fallback also failed: {_fb_err} — SKIPPING (fail-closed)")
            return {"status": "skipped", "ticker": ticker,
                    "reason": "Market-hours unverifiable — skipping entry (fail-closed)"}

    if not _check_daily_loss_limit():
        return {"status": "halted", "reason": "Daily loss limit breached (5%). No new trades today."}

    # Cap position size
    max_allowed = BANKROLL * MAX_POSITION_PCT
    if dollar_amount > max_allowed:
        dollar_amount = max_allowed

    # ── Pre-trade day-trading buying-power check ──────────────────────────
    # Bracket orders consume DT-BP, not regular BP. With 10+ positions open
    # this gets exhausted fast. Skipping early avoids the noisy Slack-spam
    # of 10 simultaneous "insufficient day trading buying power" failures.
    bp_ok, bp_reason = _check_buying_power(dollar_amount, ticker)
    if not bp_ok:
        print(f"[APEX] {ticker} skipped — {bp_reason}")
        return {"status": "skipped", "ticker": ticker, "reason": bp_reason}

    mode = "LIVE" if is_live_mode() else "PAPER"
    side = "buy" if direction == "bullish" else "sell"

    # For short orders, verify the asset is actually shortable on Alpaca
    # before attempting — avoids code 42210000 "asset cannot be sold short"
    if side == "sell":
        try:
            asset = _get_client().get_asset(ticker)
            if not getattr(asset, "shortable", False):
                print(f"[APEX] {ticker} is not shortable on Alpaca — skipping bearish order")
                return {"status": "skipped",
                        "reason": f"{ticker} is not shortable on Alpaca",
                        "ticker": ticker}
            if not getattr(asset, "easy_to_borrow", False):
                print(f"[APEX] {ticker} is hard-to-borrow — skipping bearish order")
                return {"status": "skipped",
                        "reason": f"{ticker} is hard-to-borrow",
                        "ticker": ticker}
        except Exception as e:
            print(f"[APEX] Could not verify shortability for {ticker}: {e} — skipping")
            return {"status": "skipped",
                    "reason": f"Could not verify shortability for {ticker}",
                    "ticker": ticker}

    # Get current price to calculate SL/TP and convert notional → shares
    price = get_current_price(ticker)

    # Fallback: try yfinance if Alpaca data feed fails.
    # N-9 fix: yfinance.download() doesn't expose a `timeout` param so a
    # stalled HTTP call can hang the whole scan past CF Workers' 30s wall.
    # Run it in a daemon thread with a hard 5s timeout — if it doesn't return,
    # skip the trade rather than stalling.
    if price is None or price <= 0:
        try:
            import threading
            _result = [None]
            def _yf_fetch():
                try:
                    import yfinance as yf
                    hist = yf.download(ticker, period="1d", interval="1m",
                                       progress=False, auto_adjust=True)
                    if not hist.empty:
                        _result[0] = float(hist["Close"].iloc[-1])
                except Exception:
                    pass
            _th = threading.Thread(target=_yf_fetch, daemon=True)
            _th.start()
            _th.join(timeout=5.0)
            if _result[0] is not None:
                price = _result[0]
                print(f"[APEX] Used yfinance price for {ticker}: ${price:.2f}")
            elif _th.is_alive():
                print(f"[APEX] yfinance price fetch for {ticker} timed out (5s) — skipping")
        except Exception as _ye:
            print(f"[APEX] yfinance fallback failed for {ticker}: {_ye}")

    if price is None or price <= 0:
        print(f"[APEX] Could not get price for {ticker} — skipping (no unprotected order placed)")
        return {"status": "skipped", "reason": f"Could not fetch price for {ticker}"}

    # Stop loss and take profit prices — DAY trades use tighter bracket
    _tp_pct = DAY_TRADE_TARGET_PCT if _is_day_trade else MOVE_TARGET_PCT
    _sl_pct, _effective_notional = _resolve_stop_and_size(
        dollar_amount, atr_pct, _is_day_trade, ticker)
    # Audit H1 (2026-06-02): if the ATR-rescaled notional rounds to < 1 share,
    # SKIP rather than force max(1,...). Forcing 1 share breaks the constant-$
    # risk guarantee (1 share of a high-priced name can exceed the risk budget).
    qty = round(_effective_notional / price)
    if qty < 1:
        print(f"[APEX] {ticker} skipped — ATR-sized notional ${_effective_notional:.0f} "
              f"@ ${price:.2f} rounds to <1 share (would break constant-$ risk)")
        return {"status": "skipped", "ticker": ticker,
                "reason": f"Position too small after ATR sizing (<1 share @ ${price:.2f})"}
    if side == "buy":
        stop_price  = round(price * (1 - _sl_pct), 2)
        limit_price = round(price * (1 + _tp_pct), 2)
    else:
        stop_price  = round(price * (1 + _sl_pct), 2)
        limit_price = round(price * (1 - _tp_pct), 2)

    print(f"[APEX] [{mode}] BRACKET {side.upper()} {qty} {ticker} @ ~${price:.2f}")
    _trade_type = "DAY" if _is_day_trade else "SWING"
    print(f"         Stop loss: ${stop_price:.2f} ({_sl_pct*100:.1f}% risk) [{_trade_type}]")
    print(f"         Take profit: ${limit_price:.2f} ({_tp_pct*100:.1f}% target) [{_trade_type}]")

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import (MarketOrderRequest,
                                              TakeProfitRequest, StopLossRequest)
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        client    = _get_client()
        order_req = MarketOrderRequest(
            symbol         = ticker,
            qty            = qty,
            side           = OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force  = TimeInForce.DAY,   # Alpaca requires DAY for bracket orders
            order_class    = OrderClass.BRACKET,
            take_profit    = TakeProfitRequest(limit_price=limit_price),
            stop_loss      = StopLossRequest(stop_price=stop_price),
        )
        order = client.submit_order(order_req)

        result = {
            "status":        "submitted",
            "order_id":      str(order.id),
            "ticker":        ticker,
            "side":          side,
            "qty":           qty,
            "entry_price":   price,
            "stop_loss":     stop_price,
            "take_profit":   limit_price,
            "dollar_amount": round(qty * price, 2),
            "mode":          mode,
            "timestamp":     datetime.now().isoformat(),
            "reason":        reason,
            "execution_path": execution_path,
        }

        try:
            import db
            if db.db_available():
                db.save_trade(result)
        except Exception:
            pass

        return result

    except Exception as e:
        err_str = str(e)
        # Alpaca rejects when our estimated price differs from actual market price.
        # Parse base_price from error and retry with corrected stop/TP.
        if "base_price" in err_str and "stop_price" in err_str:
            try:
                import json, re
                m = re.search(r'"base_price"\s*:\s*"?([\d.]+)"?', err_str)
                if m:
                    actual_price = float(m.group(1))
                    # Use _sl_pct/_tp_pct (DAY-aware) not hardcoded KELLY/MOVE constants
                    if side == "buy":
                        stop_price  = round(actual_price * (1 - _sl_pct), 2)
                        limit_price = round(actual_price * (1 + _tp_pct), 2)
                    else:
                        stop_price  = round(actual_price * (1 + _sl_pct), 2)
                        limit_price = round(actual_price * (1 - _tp_pct), 2)
                    qty = round(_effective_notional / actual_price)
                    if qty < 1:
                        print(f"[APEX] {ticker} retry skipped — <1 share at corrected ${actual_price:.2f}")
                        return {"status": "skipped", "ticker": ticker,
                                "reason": "Position <1 share after price correction (constant-$ risk)"}
                    print(f"[APEX] Retrying bracket with corrected price ${actual_price:.2f} "
                          f"→ SL ${stop_price:.2f} / TP ${limit_price:.2f}")
                    from alpaca.trading.client import TradingClient
                    from alpaca.trading.requests import (MarketOrderRequest,
                                                          TakeProfitRequest, StopLossRequest)
                    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
                    client    = _get_client()
                    order_req = MarketOrderRequest(
                        symbol        = ticker,
                        qty           = qty,
                        side          = OrderSide.BUY if side == "buy" else OrderSide.SELL,
                        time_in_force = TimeInForce.DAY,
                        order_class   = OrderClass.BRACKET,
                        take_profit   = TakeProfitRequest(limit_price=limit_price),
                        stop_loss     = StopLossRequest(stop_price=stop_price),
                    )
                    order = client.submit_order(order_req)
                    result = {
                        "status": "submitted", "order_id": str(order.id),
                        "ticker": ticker, "side": side, "qty": qty,
                        "entry_price": actual_price, "stop_loss": stop_price,
                        "take_profit": limit_price,
                        "dollar_amount": round(qty * actual_price, 2),
                        "mode": mode, "timestamp": datetime.now().isoformat(),
                        "reason": reason, "execution_path": execution_path,
                    }
                    try:
                        import db
                        if db.db_available():
                            db.save_trade(result)
                    except Exception:
                        pass
                    return result
            except Exception as retry_err:
                print(f"[APEX] Bracket retry failed: {retry_err}")
        # H-7 fix: do NOT blindly fall back to a NAKED simple order on every
        # bracket failure. Only fall back for KNOWN-RECOVERABLE errors. For
        # anything else, refuse and Slack-alert — naked positions are worse
        # than missed trades.
        # R-1 fix: explicit parens — Python's `and` binds tighter than `or`,
        # but readability + future-proofing matters; also tightened the
        # base_price match to require an Alpaca-specific phrase rather than
        # any error string that happens to contain "base_price".
        _err_lc = str(e).lower()
        _recoverable = (
            ("base_price" in _err_lc and "stop" in _err_lc)
            or ("stop_loss"   in _err_lc and "must be" in _err_lc)
            or ("take_profit" in _err_lc and "must be" in _err_lc)
        )
        if _recoverable:
            print(f"[APEX] Bracket failed ({e}) — recoverable, falling back to simple order.")
            return _place_simple_order(ticker, dollar_amount, side, mode, reason)
        # Non-recoverable — refuse and alert
        print(f"[APEX] Bracket failed ({e}) — NOT falling back (would create naked position).")
        try:
            from alerts.slack import _post
            _post({"text": (
                f"🚨 *Bracket order REFUSED — {ticker}*\n"
                f">Error: `{e}`\n"
                f">Did NOT fall back to simple order (would leave position unprotected)."
            )})
        except Exception:
            pass
        return {"status": "rejected_bracket_unknown", "ticker": ticker,
                "reason": f"Bracket failed with non-recoverable error: {e}"}


def _place_simple_order(ticker: str, dollar_amount: float, side: str,
                         mode: str, reason: str) -> dict:
    """Fallback: plain market order without bracket (futures, etc.)

    For SELL (short) orders we always use qty — Alpaca rejects notional shorts.
    For BUY orders we prefer notional, fall back to qty if needed.
    """
    # CRITICAL audit C-10: daily-loss kill switch was only checked in place_order.
    # Fallback paths bypassed it — bracket failure could keep bleeding capital
    # past the documented 5% hard limit. Check here too.
    if not _check_daily_loss_limit():
        return {"status": "rejected_loss_limit",
                "reason": "Daily loss limit hit — simple-order path also halted"}
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client     = _get_client()
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        # Shorts must use qty (whole shares) — Alpaca forbids fractional shorts
        if side == "sell":
            price = get_current_price(ticker)
            if not price or price <= 0:
                return {"status": "error",
                        "reason": f"Could not fetch price for {ticker} (short order)",
                        "ticker": ticker}
            qty       = max(1, round(dollar_amount / price))
            order_req = MarketOrderRequest(
                symbol        = ticker,
                qty           = qty,
                side          = order_side,
                time_in_force = TimeInForce.DAY,
            )
        else:
            qty       = None
            order_req = MarketOrderRequest(
                symbol        = ticker,
                notional      = round(dollar_amount, 2),
                side          = order_side,
                time_in_force = TimeInForce.DAY,
            )

        order  = client.submit_order(order_req)
        actual_qty = qty or round(dollar_amount / (get_current_price(ticker) or 1))
        result = {
            "status": "submitted", "order_id": str(order.id),
            "ticker": ticker, "side": side,
            "qty": actual_qty,
            "dollar_amount": dollar_amount, "mode": mode,
            "timestamp": datetime.now().isoformat(), "reason": reason,
        }
        try:
            import db
            if db.db_available(): db.save_trade(result)
        except Exception:
            pass
        return result
    except Exception as e:
        return {"status": "error", "reason": str(e), "ticker": ticker}


def get_positions() -> list[dict]:
    """Return open positions with SL/TP info from active orders.

    CRITICAL: must fetch HELD orders too — bracket stop/TP children sit in
    'held' status. Filtering by OPEN only misses them and the dashboard
    falls back to fake calculated values (entry × 0.97).
    """
    if not is_configured():
        return []
    try:
        client    = _get_client()
        positions = client.get_all_positions()
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        # Get ALL active orders (open + held + new + accepted)
        _all = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=400))
        orders = [o for o in _all if is_active_order(o)]
        sl_tp_map    = {}   # ticker → {stop_loss, take_profit}
        trailing_set = set()  # tickers with an active native trailing stop
        for o in orders:
            sym   = o.symbol
            otype = str(getattr(o, "type", "")).lower()
            # After bracket fills, child orders appear as standalone open orders:
            # TP leg → OrderType.LIMIT  (limit_price set)
            # SL leg → OrderType.STOP   (stop_price set)
            if "limit" in otype and "stop" not in otype:
                lp = getattr(o, "limit_price", None)
                if lp:
                    sl_tp_map.setdefault(sym, {})["take_profit"] = float(lp)
            if "trailing" in otype:
                # Native trailing stop — use live calculated stop_price as the SL
                sp = getattr(o, "stop_price", None)
                if sp:
                    sl_tp_map.setdefault(sym, {})["stop_loss"] = float(sp)
                trailing_set.add(sym)
            elif "stop" in otype:
                sp = getattr(o, "stop_price", None)
                if sp:
                    sl_tp_map.setdefault(sym, {})["stop_loss"] = float(sp)
            # Also check legs (for unfilled bracket parent orders)
            for leg in (getattr(o, "legs", None) or []):
                if getattr(leg, "stop_price", None):
                    sl_tp_map.setdefault(sym, {})["stop_loss"] = float(leg.stop_price)
                if getattr(leg, "limit_price", None):
                    sl_tp_map.setdefault(sym, {})["take_profit"] = float(leg.limit_price)

        result = []
        for p in positions:
            entry = float(p.avg_entry_price)
            sym   = p.symbol
            known = sl_tp_map.get(sym, {})
            # H-2 fix: track whether SL/TP came from the broker or was COMPUTED
            # as a fallback. Dashboard / EOD report should render "computed"
            # differently so users don't see a fake number labelled as a real stop.
            is_short = "short" in str(getattr(p, "side", "")).lower()
            sl_from_broker = "stop_loss"   in known
            tp_from_broker = "take_profit" in known
            if is_short:
                sl = known.get("stop_loss")   or round(entry * (1 + KELLY_LOSS_PCT), 2)
                tp = known.get("take_profit") or round(entry * (1 - MOVE_TARGET_PCT), 2)
            else:
                sl = known.get("stop_loss")   or round(entry * (1 - KELLY_LOSS_PCT), 2)
                tp = known.get("take_profit") or round(entry * (1 + MOVE_TARGET_PCT), 2)
            result.append({
                "ticker":            sym,
                "qty":               float(p.qty),
                "market_value":      float(p.market_value),
                "unrealized_pl":     float(p.unrealized_pl),
                "unrealized_pl_pct": float(p.unrealized_plpc) * 100,
                "side":              str(p.side),
                "avg_entry_price":   entry,
                "current_price":     float(p.current_price),
                "stop_loss":         sl,
                "take_profit":       tp,
                "stop_loss_source":  "broker" if sl_from_broker else "computed",
                "take_profit_source":"broker" if tp_from_broker else "computed",
                "is_trailing":       sym in trailing_set,
            })
        return result
    except Exception as e:
        print(f"[APEX] get_positions error: {e}")
        return []


def get_closed_trade_pnl(days: int = 60, raise_on_error: bool = False) -> list[dict]:
    """
    Fetch realized P&L for ALL closed trades — bracket exits AND manual closes.

    Strategy: find all filled SELL orders, match each to its most recent BUY fill
    for the same ticker to compute entry → exit P&L.

    Returns list of dicts:
        ticker, side, qty, entry_price, exit_price, realized_pnl,
        realized_pnl_pct, outcome ('tp_hit'|'sl_hit'|'manual'), closed_at

    raise_on_error: when True, a fetch/parse FAILURE re-raises instead of
    returning []. Safety-critical callers (loss-day circuit breakers) MUST set
    this so they can tell "no closed trades" (safe, []) apart from "couldn't
    fetch" (must fail CLOSED). An empty [] from a genuine no-trades state is
    NOT an error and never raises.
    """
    if not is_configured():
        if raise_on_error:
            raise RuntimeError("Alpaca not configured — cannot fetch closed P&L")
        return []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from datetime import datetime, timedelta

        client = _get_client()
        after  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")

        # CRITICAL FIX (2026-06-02): the old code fetched a SINGLE page of 500
        # orders with no pagination. A 30-day window routinely has 500+ orders,
        # so the oldest BUY fills were silently dropped — their matching SELLs
        # then had no entry lot to pair against, producing phantom/mis-priced
        # round-trips. Result: this function reported -$5,003 when the Alpaca
        # account was actually +$3,592 (off by $8,307). Worse, the circuit
        # breakers (same-day lockout, daily halt) call this function, so they
        # were firing on fictional losses. Now paginate FULLY (oldest→newest)
        # so FIFO sees every fill and every share matches exactly once.
        all_orders = []
        until = None
        for _page in range(20):   # 20 × 500 = 10k orders, far beyond any window
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=after,
                limit=500,
                direction="desc",
                until=until,
            )
            batch = client.get_orders(req)
            if not batch:
                break
            all_orders.extend(batch)
            if len(batch) < 500:
                break
            # Page back via the oldest submitted_at in this batch
            oldest = batch[-1].submitted_at
            if oldest is None:
                break
            until = oldest

        # De-dupe by order id (pagination boundaries can overlap by one)
        _seen = set()
        _deduped = []
        for o in all_orders:
            oid = str(getattr(o, "id", ""))
            if oid in _seen:
                continue
            _seen.add(oid)
            _deduped.append(o)
        all_orders = _deduped

        # Keep only filled orders with a fill price
        filled = [o for o in all_orders
                  if o.filled_avg_price and o.filled_qty
                  and float(o.filled_qty) > 0 and o.filled_at]

        # Group by ticker
        by_ticker: dict = {}
        for o in filled:
            by_ticker.setdefault(o.symbol, []).append(o)

        # ── FIFO matching — same accounting method Alpaca uses ───────────────
        # Walks fills in chronological order, tracking remaining qty per entry.
        # An exit consumes entries from the oldest-first; if exit qty > oldest
        # entry remaining, it spills to the next entry. Each share is matched
        # exactly once → no double-counting, no inflated P&L.
        results = []
        for ticker, orders in by_ticker.items():
            # Sort all fills chronologically (mix of buy + sell)
            fills = sorted(orders, key=lambda o: o.filled_at)

            # Open lots: list of dicts tracking remaining shares per entry
            # For longs: lots have side="long", positive qty (opened by buy)
            # For shorts: lots have side="short", positive qty (opened by sell)
            open_lots: list = []

            for fill in fills:
                fill_side = str(fill.side).lower()
                fill_qty  = float(fill.filled_qty)
                fill_px   = float(fill.filled_avg_price)
                fill_type = str(getattr(fill, "type", "")).lower()
                fill_time = str(fill.filled_at)[:16].replace("T", " ")
                is_buy    = "buy" in fill_side

                # Determine if this fill OPENS or CLOSES a lot.
                # Default rule: BUY opens long, SELL opens short — UNLESS
                # there's an opposing open lot, in which case it closes.
                if is_buy:
                    # BUY closes any open SHORT lots first (FIFO cover)
                    short_lots = [l for l in open_lots if l["side"] == "short"]
                    if short_lots:
                        qty_to_close = fill_qty
                        while qty_to_close > 0 and short_lots:
                            lot = short_lots[0]
                            matched = min(qty_to_close, lot["qty"])
                            pnl = round((lot["price"] - fill_px) * matched, 2)
                            pnl_pct = round((lot["price"] / fill_px - 1) * 100, 2) if fill_px else 0
                            outcome = ("tp_hit" if "limit" in fill_type and pnl > 0
                                       else "sl_hit" if "stop" in fill_type and pnl < 0
                                       else "manual")
                            results.append({
                                "ticker": ticker, "side": "short", "qty": matched,
                                "entry_price": round(lot["price"], 2),
                                "exit_price":  round(fill_px, 2),
                                "realized_pnl": pnl, "realized_pnl_pct": pnl_pct,
                                "outcome": outcome, "closed_at": fill_time,
                            })
                            lot["qty"] -= matched
                            qty_to_close -= matched
                            if lot["qty"] <= 1e-9:
                                open_lots.remove(lot)
                                short_lots = [l for l in open_lots if l["side"] == "short"]
                        # Any remaining buy qty opens a new LONG lot
                        if qty_to_close > 1e-9:
                            open_lots.append({"side": "long", "price": fill_px, "qty": qty_to_close, "time": fill_time})
                    else:
                        # No shorts to close → open a long lot
                        open_lots.append({"side": "long", "price": fill_px, "qty": fill_qty, "time": fill_time})
                else:
                    # SELL closes any open LONG lots first (FIFO)
                    long_lots = [l for l in open_lots if l["side"] == "long"]
                    if long_lots:
                        qty_to_close = fill_qty
                        while qty_to_close > 0 and long_lots:
                            lot = long_lots[0]
                            matched = min(qty_to_close, lot["qty"])
                            pnl = round((fill_px - lot["price"]) * matched, 2)
                            pnl_pct = round((fill_px / lot["price"] - 1) * 100, 2) if lot["price"] else 0
                            outcome = ("tp_hit" if "limit" in fill_type and pnl > 0
                                       else "sl_hit" if "stop" in fill_type and pnl < 0
                                       else "manual")
                            results.append({
                                "ticker": ticker, "side": "long", "qty": matched,
                                "entry_price": round(lot["price"], 2),
                                "exit_price":  round(fill_px, 2),
                                "realized_pnl": pnl, "realized_pnl_pct": pnl_pct,
                                "outcome": outcome, "closed_at": fill_time,
                            })
                            lot["qty"] -= matched
                            qty_to_close -= matched
                            if lot["qty"] <= 1e-9:
                                open_lots.remove(lot)
                                long_lots = [l for l in open_lots if l["side"] == "long"]
                        # Any remaining sell qty opens a new SHORT lot
                        if qty_to_close > 1e-9:
                            open_lots.append({"side": "short", "price": fill_px, "qty": qty_to_close, "time": fill_time})
                    else:
                        # No longs to close → open a short lot
                        open_lots.append({"side": "short", "price": fill_px, "qty": fill_qty, "time": fill_time})

        return sorted(results, key=lambda x: x["closed_at"], reverse=True)

    except Exception as e:
        print(f"[APEX] get_closed_trade_pnl error: {e}")
        if raise_on_error:
            raise
        return []


def close_position(ticker: str) -> dict:
    """
    Close an open position immediately at market price.

    Must cancel BOTH open and held orders before closing — bracket stop/TP
    children sit in 'held' status and hold the position's shares hostage.
    Without cancelling those, close_position() fails with 'insufficient qty'.
    """
    if not is_configured():
        return {"status": "skipped"}
    try:
        import time as _time
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        client = _get_client()

        # Cancel ALL active orders for this ticker (open + held + new + accepted)
        try:
            all_orders = client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, limit=400))
            cancelled = 0
            for o in all_orders:
                if o.symbol != ticker:
                    continue
                if not is_active_order(o):
                    continue
                try:
                    client.cancel_order_by_id(str(o.id))
                    cancelled += 1
                except Exception:
                    pass
            # Give Alpaca a moment to release the held shares
            if cancelled:
                _time.sleep(2.0)
        except Exception as _ce:
            # M-2 fix: if the WHOLE cancel-loop fails (Alpaca API down, auth
            # broken), proceed-to-close at line 730 will also fail confusingly.
            # One log line tells us why.
            print(f"[close_position] {ticker} pre-cancel loop failed: {_ce}")

        # Close the position — retry once with a longer wait if first attempt fails
        try:
            client.close_position(ticker)
        except Exception as first_err:
            if "insufficient" in str(first_err).lower() or "held" in str(first_err).lower():
                _time.sleep(1.5)
                try:
                    result = client.close_position(ticker)
                except Exception as _e2:
                    print(f"[ALPACA] close_position retry also failed for {ticker}: {_e2}")
                    return {"status": "error", "ticker": ticker, "message": f"Both close attempts failed: {_e2}"}
            else:
                raise

        # R-4 fix: write a status="closed" row so get_partial_exit_history()'s
        # last_close lookup actually works. Without this row, partial-exit
        # entries from a CLOSED prior position could leak into a future re-entry.
        try:
            import db as _db
            if _db.db_available():
                _db.save_trade({
                    "order_id":  f"close-{ticker}-{int(_time.time())}",
                    "ticker":    ticker,
                    "side":      "close",
                    "dollar_amount": 0,
                    "mode":      "LIVE" if is_live_mode() else "PAPER",
                    "status":    "closed",
                    "reason":    "close_position() called",
                    "timestamp": datetime.now().isoformat(),
                })
        except Exception as _ce:
            print(f"[close_position] could not log close to DB: {_ce}")

        return {"status": "closed", "ticker": ticker, "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"status": "error", "reason": str(e), "ticker": ticker}


def tighten_stop(ticker: str, stop_pct: float = 0.015) -> dict:
    """
    Cancel existing stop loss for ticker and replace with a tighter one.
    stop_pct: how far below current price to set the new stop (default 1.5%).
    Used by sentiment_guard when a position's sentiment turns negative.
    """
    if not is_configured():
        return {"status": "skipped"}
    try:
        from alpaca.trading.requests import (GetOrdersRequest, StopOrderRequest)
        from alpaca.trading.enums import (QueryOrderStatus, OrderSide, TimeInForce)

        client = _get_client()

        # Get current price
        price = get_current_price(ticker)
        if not price:
            return {"status": "error", "reason": "Could not fetch price"}

        # Cancel existing stop orders for this ticker.
        # CRITICAL audit C-2: Bracket SL legs sit in HELD (not OPEN). Querying
        # OPEN-only meant tighten_stop() stacked a new tighter stop ON TOP of
        # the old wider one — could result in duplicate exits or naked qty.
        active_orders = [o for o in client.get_orders(GetOrdersRequest(
                            status=QueryOrderStatus.ALL, limit=500))
                         if is_active_order(o)]
        cancelled = 0
        qty = 0
        for o in active_orders:
            if o.symbol != ticker:
                continue
            otype = str(getattr(o, "type", "")).lower()
            if "stop" in otype and "limit" not in otype and "trailing" not in otype:
                try:
                    client.cancel_order_by_id(str(o.id))
                    cancelled += 1
                    if not qty and o.qty:
                        qty = float(o.qty)
                except Exception:
                    pass

        if not qty:
            # Try to get qty from position
            positions = get_positions()
            for p in positions:
                if p["ticker"] == ticker:
                    qty = p["qty"]
                    break

        if not qty:
            return {"status": "error", "reason": "Could not determine qty"}

        # Determine if position is long or short to set correct stop side
        positions_now = get_positions()
        is_short = False
        for pos in positions_now:
            if pos["ticker"] == ticker:
                is_short = "short" in str(pos.get("side", "")).lower()
                break

        # For longs:  stop is below current price (sell to exit)
        # For shorts: stop is above current price (buy to exit)
        if is_short:
            new_stop = round(price * (1 + stop_pct), 2)
            stop_side = OrderSide.BUY
        else:
            new_stop = round(price * (1 - stop_pct), 2)
            stop_side = OrderSide.SELL

        from alpaca.trading.requests import MarketOrderRequest
        stop_req = StopOrderRequest(
            symbol        = ticker,
            qty           = qty,
            side          = stop_side,
            time_in_force = TimeInForce.GTC,
            stop_price    = new_stop,
        )
        order = client.submit_order(stop_req)
        mode  = "LIVE" if is_live_mode() else "PAPER"
        print(f"[VIGIL] [{mode}] {ticker} stop tightened → ${new_stop:.2f} "
              f"({stop_pct*100:.1f}% below ${price:.2f})")
        return {
            "status":    "tightened",
            "ticker":    ticker,
            "new_stop":  new_stop,
            "price":     price,
            "stop_pct":  stop_pct,
            "order_id":  str(order.id),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "reason": str(e), "ticker": ticker}


from execution.aegis import trail_positions  # noqa: F401


def cancel_all_orders() -> int:
    """Cancel all active orders (open + new + held + accepted + pending_new).

    H-3 fix: was OPEN-only — bracket protective legs in HELD survived,
    causing orphan orders to accumulate after emergency cancel-all.
    """
    if not is_configured():
        return 0
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        client = _get_client()
        orders = [o for o in client.get_orders(GetOrdersRequest(
                       status=QueryOrderStatus.ALL, limit=500))
                  if is_active_order(o)]
        for o in orders:
            try:
                client.cancel_order_by_id(str(o.id))
            except Exception as _e:
                print(f"[cancel_all] {o.symbol} {o.id}: {_e}")
        return len(orders)
    except Exception as _e:
        print(f"[cancel_all] failed: {_e}")
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# CRYPTO EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def place_crypto_order(alpaca_symbol: str, dollar_amount: float,
                       direction: str, reason: str = "",
                       execution_path: str = "", atr_pct: float = 0.0) -> dict:
    """
    Place a crypto market order via Alpaca.

    - alpaca_symbol: Alpaca format e.g. "BTC/USD"
    - Crypto uses GTC (not DAY) — markets are 24/7
    - No bracket orders for crypto — places separate stop + limit orders after fill
    - Fractional crypto always supported (uses notional dollar amount)

    Audit 2026-06-02: crypto now goes through the SAME risk envelope as equities —
    the MAX_POSITION_PCT cap and ATR-based stop + constant-$-risk sizing
    (_resolve_stop_and_size) — and FAILS CLOSED if it cannot attach protection
    (alerts + closes the just-opened position instead of leaving it naked).
    """
    from config import KELLY_LOSS_PCT, MOVE_TARGET_PCT, BANKROLL, MAX_POSITION_PCT
    if not is_configured():
        return {"status": "skipped", "reason": "Alpaca not configured"}

    # CRITICAL audit C-10: daily-loss kill switch also gates crypto.
    if not _check_daily_loss_limit():
        return {"status": "rejected_loss_limit",
                "reason": "Daily loss limit hit — crypto path also halted"}

    # Audit C1: cap position size (was unbounded — equities cap at 8%, crypto didn't).
    _cap = BANKROLL * MAX_POSITION_PCT
    if dollar_amount > _cap:
        print(f"[APEX] CRYPTO {alpaca_symbol}: ${dollar_amount:.0f} capped to "
              f"${_cap:.0f} (MAX_POSITION_PCT {MAX_POSITION_PCT:.0%})")
        dollar_amount = _cap
    # Audit C1: ATR stop + constant-$-risk notional rescale (same as equities).
    _sl_pct, _effective_notional = _resolve_stop_and_size(
        dollar_amount, atr_pct, False, alpaca_symbol)
    dollar_amount = _effective_notional

    mode = "LIVE" if is_live_mode() else "PAPER"
    side = "buy" if direction in ("bullish", "long") else "sell"

    try:
        from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        client    = _get_client()
        order_req = MarketOrderRequest(
            symbol        = alpaca_symbol,
            notional      = round(dollar_amount, 2),  # dollar-based for crypto
            side          = OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force = TimeInForce.GTC,          # crypto is 24/7
        )
        order = client.submit_order(order_req)
        print(f"[APEX] [{mode}] CRYPTO {side.upper()} ${dollar_amount:.0f} {alpaca_symbol}")

        # After submission, estimate price to set protective orders
        # (actual fill price may differ slightly)
        try:
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoLatestQuoteRequest
            dc    = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            quote = dc.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=alpaca_symbol))
            price = float(quote[alpaca_symbol].ask_price)
            # Audit C1: protective orders sized to the POSITION qty (was full
            # notional on BOTH legs — two full-size resting sells could exceed
            # the held position). Round qty to 6dp (crypto is fractional).
            qty   = round(dollar_amount / price, 6) if price else 0

            if side == "buy":
                sl_price = round(price * (1 - _sl_pct), 2)
                tp_price = round(price * (1 + MOVE_TARGET_PCT), 2)
                sl_side  = OrderSide.SELL
                tp_side  = OrderSide.SELL
            else:
                sl_price = round(price * (1 + _sl_pct), 2)
                tp_price = round(price * (1 - MOVE_TARGET_PCT), 2)
                sl_side  = OrderSide.BUY
                tp_side  = OrderSide.BUY

            if qty <= 0:
                raise ValueError(f"computed qty {qty} <= 0 (price={price})")
            # Stop loss (qty-based, not notional)
            client.submit_order(StopOrderRequest(
                symbol=alpaca_symbol, qty=qty,
                side=sl_side, time_in_force=TimeInForce.GTC, stop_price=sl_price))
            # Take profit (qty-based)
            client.submit_order(LimitOrderRequest(
                symbol=alpaca_symbol, qty=qty,
                side=tp_side, time_in_force=TimeInForce.GTC, limit_price=tp_price))
            print(f"         SL: ${sl_price:.2f} ({_sl_pct*100:.1f}%)  TP: ${tp_price:.2f}  qty={qty}")
        except Exception as pe:
            # Audit C2: FAIL CLOSED — never leave a filled crypto position naked.
            # Previously this printed a warning and returned "submitted". Now we
            # alert loudly AND close the just-opened position.
            print(f"[APEX] 🚨 Crypto SL/TP placement FAILED for {alpaca_symbol}: {pe} — closing to avoid naked position")
            _closed_ok = False
            try:
                _cl = close_position(alpaca_symbol)
                _closed_ok = _cl.get("status") in ("submitted", "closed", "ok")
            except Exception as _ce:
                print(f"[APEX] follow-up close also failed for {alpaca_symbol}: {_ce}")
            try:
                from alerts.slack import _post
                _post({"text": (
                    f"🚨 *APEX crypto naked-position guard* — {alpaca_symbol} {side.upper()} "
                    f"filled but stop/TP FAILED ({str(pe)[:80]}). "
                    f"{'Position auto-closed.' if _closed_ok else '⚠️ AUTO-CLOSE ALSO FAILED — CHECK MANUALLY NOW.'}"
                )})
            except Exception:
                pass
            return {
                "status": "closed_unprotected" if _closed_ok else "naked_alert",
                "ticker": alpaca_symbol, "side": side,
                "reason": f"SL/TP failed ({str(pe)[:80]}); "
                          f"{'position closed' if _closed_ok else 'CLOSE FAILED — manual action required'}",
                "asset_class": "crypto", "mode": mode,
            }

        result = {
            "status":        "submitted",
            "order_id":      str(order.id),
            "ticker":        alpaca_symbol,
            "side":          side,
            "dollar_amount": dollar_amount,
            "asset_class":   "crypto",
            "mode":          mode,
            "timestamp":     datetime.now().isoformat(),
            "reason":        reason,
            "execution_path": execution_path,
        }
        try:
            import db
            if db.db_available():
                db.save_trade(result)
        except Exception:
            pass
        return result

    except Exception as e:
        print(f"[APEX] Crypto order failed for {alpaca_symbol}: {e}")
        return {"status": "error", "reason": str(e), "ticker": alpaca_symbol}


# ══════════════════════════════════════════════════════════════════════════════
# OPTIONS EXECUTION — IRON BUTTERFLY
# ══════════════════════════════════════════════════════════════════════════════

def place_iron_butterfly(ticker: str, expiry_yymmdd: str,
                         atm_strike: float, wing_width: float,
                         contracts: int = 1, reason: str = "") -> dict:
    """
    Place a short iron butterfly (credit strategy) via Alpaca multi-leg order.

    Structure:
      - Sell 1 ATM call  (at atm_strike)
      - Sell 1 ATM put   (at atm_strike)
      - Buy  1 OTM call  (at atm_strike + wing_width)
      - Buy  1 OTM put   (at atm_strike - wing_width)

    Requires Level 3 options approval + ENABLE_OPTIONS=true in env.
    """
    from config import ENABLE_OPTIONS
    if not ENABLE_OPTIONS:
        return {"status": "skipped",
                "reason": "Options trading disabled — set ENABLE_OPTIONS=true after Level 3 approval"}

    if not is_configured():
        return {"status": "skipped", "reason": "Alpaca not configured"}

    # CRITICAL audit C-10: options also gated by daily loss limit.
    if not _check_daily_loss_limit():
        return {"status": "rejected_loss_limit",
                "reason": "Daily loss limit hit — options path also halted"}

    mode = "LIVE" if is_live_mode() else "PAPER"

    from execution.options_utils import build_option_symbol
    sell_call = build_option_symbol(ticker, expiry_yymmdd, "C", atm_strike)
    sell_put  = build_option_symbol(ticker, expiry_yymmdd, "P", atm_strike)
    buy_call  = build_option_symbol(ticker, expiry_yymmdd, "C", atm_strike + wing_width)
    buy_put   = build_option_symbol(ticker, expiry_yymmdd, "P", atm_strike - wing_width)

    print(f"[APEX] [{mode}] IRON BUTTERFLY {ticker} exp={expiry_yymmdd} "
          f"ATM={atm_strike} wings=±{wing_width}")
    print(f"         Sell {sell_call} · Sell {sell_put}")
    print(f"         Buy  {buy_call} · Buy  {buy_put}")

    try:
        from alpaca.trading.requests import OptionLegRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        legs = [
            OptionLegRequest(symbol=sell_call, side=OrderSide.SELL, ratio_qty=1),
            OptionLegRequest(symbol=sell_put,  side=OrderSide.SELL, ratio_qty=1),
            OptionLegRequest(symbol=buy_call,  side=OrderSide.BUY,  ratio_qty=1),
            OptionLegRequest(symbol=buy_put,   side=OrderSide.BUY,  ratio_qty=1),
        ]
        order_req = MarketOrderRequest(
            qty           = contracts,
            time_in_force = TimeInForce.DAY,
            order_class   = OrderClass.MLEG,
            legs          = legs,
        )
        client = _get_client()
        order  = client.submit_order(order_req)

        result = {
            "status":      "submitted",
            "order_id":    str(order.id),
            "ticker":      ticker,
            "side":        "iron_butterfly",
            "asset_class": "options",
            "legs":        [sell_call, sell_put, buy_call, buy_put],
            "atm_strike":  atm_strike,
            "wing_width":  wing_width,
            "expiry":      expiry_yymmdd,
            "contracts":   contracts,
            "mode":        mode,
            "timestamp":   datetime.now().isoformat(),
            "reason":      reason,
        }
        try:
            import db
            if db.db_available():
                db.save_trade(result)
        except Exception:
            pass
        return result

    except Exception as e:
        print(f"[APEX] Iron butterfly failed for {ticker}: {e}")
        return {"status": "error", "reason": str(e), "ticker": ticker}
