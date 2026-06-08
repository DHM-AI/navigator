from __future__ import annotations
"""
Portfolio-level risk guard — prevents dangerous concentration before any new order.

Checks (in order):
  1. Max concurrent positions (default 10) — don't overload
  2. Sector concentration — max 35% of portfolio in any one GICS sector
  3. Direction balance — if > 80% of open positions are one-directional,
     warn before adding more of the same
  4. Daily trade count — max 5 new orders per day (avoid churning)

Usage in agent.py:
    from risk.portfolio_guard import check_trade
    ok, reason = check_trade(ticker, dollar_amount, direction, positions)
    if not ok:
        print(f"Blocked: {reason}")
        continue
"""

import yfinance as yf
from config import CANARY_MODE, CANARY_MAX_OPEN_POSITIONS, CANARY_MAX_DAILY_TRADES

# ── Config ────────────────────────────────────────────────────────────────────
# Canary (live ramp-up) shrinks position count + daily trades; full-size values
# live in the else branch. Flip CANARY_MODE in config.py to switch back.
MAX_OPEN_POSITIONS  = CANARY_MAX_OPEN_POSITIONS if CANARY_MODE else 10  # max concurrent holdings
MAX_SECTOR_PCT      = 0.35   # max 35% portfolio in one sector
MAX_DAILY_TRADES    = CANARY_MAX_DAILY_TRADES if CANARY_MODE else 20    # max new auto-executions per day
MAX_DIRECTION_PCT   = 0.80   # warn if >80% of positions same direction

_sector_cache: dict[str, str] = {}  # ticker → sector (cached across calls)
_daily_trade_count: int = 0
_daily_trade_date:  str = ""


def _get_sector(ticker: str) -> str:
    """Return GICS sector string for a ticker, using cache."""
    if ticker in _sector_cache:
        return _sector_cache[ticker]
    try:
        info   = yf.Ticker(ticker).info
        sector = info.get("sector", "Unknown") or "Unknown"
        _sector_cache[ticker] = sector
        return sector
    except Exception:
        _sector_cache[ticker] = "Unknown"
        return "Unknown"


def check_trade(
    ticker:       str,
    dollar_amount: float,
    direction:    str,
    open_positions: list[dict],
    portfolio_value: float | None = None,
) -> tuple[bool, str]:
    """
    Run all portfolio-level checks before placing an order.

    Args:
        ticker:          e.g. "NVDA"
        dollar_amount:   proposed position size in dollars
        direction:       "bullish" | "bearish"
        open_positions:  list of position dicts from alpaca.get_positions()
        portfolio_value: total account value (optional, for % calculations)

    Returns:
        (True, "ok") if trade is allowed
        (False, reason_str) if trade should be blocked
    """
    from datetime import datetime, timezone

    # ── Check 1: Already in this position ─────────────────────────────────────
    tickers_held = [p.get("ticker", "") for p in open_positions]
    if ticker in tickers_held:
        return False, f"Already holding {ticker} — no duplicate positions"

    # ── Check 1a: Pending order on the same ticker ────────────────────────────
    # Today's SEGG incident: 5 separate scans each queued a SEGG short because
    # the duplicate check only looked at FILLED positions, not pending orders.
    # A pending entry order on this ticker counts as a duplicate too.
    # CRITICAL audit C-12: was OPEN-only — bracket parents in ACCEPTED /
    # PENDING_NEW / HELD weren't seen, allowing duplicate entries (the SEGG
    # cluster pattern). Use ALL + is_active_order().
    try:
        from execution.alpaca import _get_client, is_configured, is_active_order
        from alpaca.trading.requests import GetOrdersRequest as _GOR2
        from alpaca.trading.enums import QueryOrderStatus as _QOS2
        if is_configured():
            _pending = [o for o in _get_client().get_orders(
                            _GOR2(status=_QOS2.ALL, limit=500))
                        if is_active_order(o)]
            for _o in _pending:
                if _o.symbol != ticker:
                    continue
                # Skip protective children (stops/trailing/bracket TPs) —
                # those don't represent a NEW entry attempt
                _otype = str(getattr(_o, "type", "")).lower()
                if "stop" in _otype:
                    continue
                if getattr(_o, "parent_order_id", None):
                    continue
                return False, (
                    f"Pending {_o.side} order already queued for {ticker} "
                    f"(qty {_o.qty}) — no duplicate signals"
                )
    except Exception as e:
        print(f"[THEMIS] Pending-order dedup check failed for {ticker}: {e} — allowing trade")

    # ── Check 1a2: No new entries late Friday (weekend-gap protection) ────────
    # A stop can't protect against a Monday gap-down. On 2026-06-01 HOOD and WOLF
    # were both bought Fri 2:09 PM and gapped through their stops over the weekend
    # (-$539 / -$554). Block NEW entries after the Friday cutoff; the same setup
    # can be taken Monday when stops work. Existing positions are untouched.
    try:
        from config import BLOCK_FRIDAY_PM_ENTRIES, FRIDAY_ENTRY_CUTOFF_ET
        if BLOCK_FRIDAY_PM_ENTRIES:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            # weekday(): Mon=0 … Fri=4 … Sun=6
            et_hour_frac = now_et.hour + now_et.minute / 60.0
            if now_et.weekday() == 4 and et_hour_frac >= FRIDAY_ENTRY_CUTOFF_ET:
                return False, (
                    f"Friday {now_et.strftime('%H:%M')} ET — no new entries after "
                    f"{FRIDAY_ENTRY_CUTOFF_ET:.0f}:00 ET (Friday-afternoon chop / weekend gap). "
                    f"Take this setup next session."
                )
    except Exception as e:
        print(f"[THEMIS] Friday-entry check failed for {ticker}: {e} — allowing trade")

    # ── Check 1a2b: General end-of-day entry cutoff (ALL days) ────────────────
    # No new positions after NO_ENTRY_AFTER_ET. Day trades need room to work
    # before DUSK closes at 3:50 PM, and any entry after DUSK orphans overnight
    # (FPS opened 3:58 PM Fri AFTER DUSK → stuck overnight, Renato 2026-06-05).
    # Entries stop at 3:30, DUSK closes at 3:50 → no orphan window.
    try:
        from config import NO_ENTRY_AFTER_ET
        from zoneinfo import ZoneInfo
        _now_et = datetime.now(ZoneInfo("America/New_York"))
        if (_now_et.hour + _now_et.minute / 60.0) >= NO_ENTRY_AFTER_ET:
            return False, (
                f"{_now_et.strftime('%H:%M')} ET — past the {NO_ENTRY_AFTER_ET:.2f} "
                f"entry cutoff. No new positions this close to DUSK (would orphan "
                f"overnight). Next session."
            )
    except Exception as e:
        print(f"[THEMIS] EOD-cutoff check failed for {ticker}: {e} — allowing trade")

    # ── Check 1a3: Max entries per ticker (anti-accumulation) ─────────────────
    # The duplicate check (Check 1) relies on the in-memory open_positions list,
    # which clearly missed ON on 2026-06-01 (bought 5× in a week, averaging up
    # into a decline). Count actual BUY fills from Alpaca over a rolling window
    # and cap them — independent of the position snapshot, so it can't be fooled
    # by stale state or brief close/reopen between scans.
    try:
        from config import MAX_ENTRIES_PER_TICKER, ENTRY_CAP_WINDOW_DAYS
        from execution.alpaca import _get_client, is_configured, order_status
        from alpaca.trading.requests import GetOrdersRequest as _GOR3
        from alpaca.trading.enums import QueryOrderStatus as _QOS3
        if is_configured():
            _since = (datetime.now(timezone.utc)
                      - __import__("datetime").timedelta(days=ENTRY_CAP_WINDOW_DAYS)
                      ).strftime("%Y-%m-%dT00:00:00Z")
            _recent = _get_client().get_orders(
                _GOR3(status=_QOS3.ALL, after=_since, limit=500))
            # Count filled BUY entries for this ticker (exclude bracket children,
            # stops, and sells — only genuine new-entry buy fills count).
            _entry_fills = 0
            for _o in _recent:
                if _o.symbol != ticker:
                    continue
                if getattr(_o, "parent_order_id", None):
                    continue
                if "buy" not in str(getattr(_o, "side", "")).lower():
                    continue
                if str(getattr(_o, "type", "")).lower() in ("stop", "trailing_stop"):
                    continue
                if order_status(_o) in ("filled", "partially_filled"):
                    _entry_fills += 1
            if _entry_fills >= MAX_ENTRIES_PER_TICKER:
                return False, (
                    f"Entry cap: {ticker} already has {_entry_fills} buy fills in "
                    f"{ENTRY_CAP_WINDOW_DAYS}d (max {MAX_ENTRIES_PER_TICKER}) — "
                    f"no accumulating into the same name (the ON 5x pattern)"
                )
    except Exception as e:
        print(f"[THEMIS] Entry-cap check failed for {ticker}: {e} — allowing trade")

    # ── Check 1a4: Same-day re-entry lockout (anti-spiral circuit breaker) ─────
    # The single worst pattern in the data: RYOJ stopped out 13x in ONE day
    # (2026-05-27) for -$4,190. Once a ticker stops out today, lock it for the
    # rest of the day — one loss, not thirteen.
    try:
        from config import BLOCK_SAME_DAY_REENTRY, LOSS_COOLDOWN_PCT
        from execution.alpaca import get_closed_trade_pnl
        if BLOCK_SAME_DAY_REENTRY:
            _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for _t in get_closed_trade_pnl(days=1, raise_on_error=True):
                if _t.get("ticker") != ticker:
                    continue
                if not str(_t.get("closed_at", "")).startswith(_today):
                    continue
                _pnl = _t.get("realized_pnl", 0)
                _entry = _t.get("entry_price", 0) or 1
                _qty = abs(_t.get("qty", 0)) or 1
                _pct = _pnl / (_entry * _qty) if _entry * _qty else 0
                if _pct < -LOSS_COOLDOWN_PCT:   # a real loss today
                    return False, (
                        f"Same-day lockout: {ticker} already stopped out today "
                        f"(${_pnl:+,.0f}) — no re-entry until tomorrow "
                        f"(anti-spiral; RYOJ lost $4,190 from 13 re-entries)"
                    )
    except Exception as e:
        # FAIL CLOSED: if we can't verify whether the ticker already stopped out
        # today, do NOT open new risk — block. (Audit 2026-06-02: previously
        # "allowed trade" on any error, which silently disabled the anti-spiral
        # breaker on exactly the bad days it exists for.)
        print(f"[THEMIS] Same-day re-entry check ERRORED for {ticker}: {e} — BLOCKING (fail-closed)")
        return False, (
            f"Same-day lockout check unavailable for {ticker} "
            f"({str(e)[:60]}) — blocking new entry as a precaution"
        )

    # ── Check 1a5: Daily realized-loss kill-switch ────────────────────────────
    # May 27 ran unchecked to -$4,776. Once today's REALIZED (closed) P&L breaches
    # the floor, halt ALL new entries for the day. AEGIS still manages open
    # positions — this only blocks opening NEW risk while the day is bleeding.
    try:
        from config import (DAILY_REALIZED_LOSS_HALT_USD,
                            DAILY_REALIZED_LOSS_HALT_PCT, BANKROLL)
        from execution.alpaca import get_closed_trade_pnl, get_bankroll
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _realized_today = sum(
            _t.get("realized_pnl", 0)
            for _t in get_closed_trade_pnl(days=1, raise_on_error=True)
            if str(_t.get("closed_at", "")).startswith(_today)
        )
        # live account bankroll, not static (Renato 2026-06-03)
        try:
            _bankroll = get_bankroll()
        except Exception:
            _bankroll = BANKROLL
        _floor = -min(DAILY_REALIZED_LOSS_HALT_USD,
                      _bankroll * DAILY_REALIZED_LOSS_HALT_PCT)
        if _realized_today <= _floor:
            return False, (
                f"Daily loss halt: realized ${_realized_today:+,.0f} today "
                f"(floor ${_floor:,.0f}) — NO new entries for the rest of the day. "
                f"Open positions still managed by AEGIS."
            )
    except Exception as e:
        # FAIL CLOSED: if today's realized P&L can't be computed, we cannot rule
        # out that the daily loss floor has already been breached — block new
        # entries. (Audit 2026-06-02: previously allowed trades on any fetch
        # error, silently disabling the kill-switch on a bleeding day.)
        print(f"[THEMIS] Daily-loss-halt check ERRORED for {ticker}: {e} — BLOCKING (fail-closed)")
        return False, (
            f"Daily loss-halt check unavailable ({str(e)[:60]}) — "
            f"blocking new entries as a precaution. AEGIS still manages open positions."
        )

    # ── Check 1b: Recent-loss cooldown ────────────────────────────────────────
    # Prevent "death by a thousand cuts" pattern where same volume/RSI signal
    # keeps firing on a declining stock, system keeps re-buying after each
    # stop hit. Block re-entry for LOSS_COOLDOWN_HOURS after any loss close.
    try:
        from config import LOSS_COOLDOWN_HOURS, LOSS_COOLDOWN_PCT
        from execution.alpaca import get_closed_trade_pnl
        recent_closed = get_closed_trade_pnl(days=3, raise_on_error=True)
        now_ts = datetime.now(timezone.utc)
        for t in recent_closed:
            if t.get("ticker") != ticker:
                continue
            pnl = t.get("realized_pnl", 0)
            entry = t.get("entry_price", 0) or 1
            qty = abs(t.get("qty", 0)) or 1
            pct_loss = pnl / (entry * qty) if entry * qty else 0
            if pct_loss >= -LOSS_COOLDOWN_PCT:
                continue   # not a loss (or too small to count)
            # Parse close time
            closed_at = t.get("closed_at", "")
            try:
                ct = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                # Supabase stores UTC timestamps WITHOUT a tz offset (naive, e.g.
                # "2026-06-04 14:59:38"). Subtracting a naive ct from the aware
                # now_ts raises "can't subtract offset-naive and offset-aware
                # datetimes" — which fail-closes the WHOLE check and blocks re-entry
                # for EVERY name with a recent close (the AMD bug, 2026-06-05).
                # Treat naive timestamps as UTC (that's what the writer used).
                if ct.tzinfo is None:
                    ct = ct.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            hours_since = (now_ts - ct).total_seconds() / 3600
            if hours_since < LOSS_COOLDOWN_HOURS:
                return False, (
                    f"Loss cooldown on {ticker} — last stop hit "
                    f"{hours_since:.1f}h ago (${pnl:+,.0f}), "
                    f"blocking re-entry for {LOSS_COOLDOWN_HOURS}h"
                )
    except Exception as e:
        # FAIL CLOSED: can't verify recent stop-outs -> don't re-enter (audit 2026-06-02).
        print(f"[THEMIS] Loss cooldown check ERRORED for {ticker}: {e} — BLOCKING (fail-closed)")
        return False, (
            f"Loss-cooldown check unavailable for {ticker} "
            f"({str(e)[:60]}) — blocking re-entry as a precaution"
        )

    # ── Check 1c: Minimum stock price floor ───────────────────────────────────
    # Sub-$5 stocks have wide spreads and low liquidity — your position size
    # moves the market against you. Skip them entirely.
    try:
        from config import MIN_STOCK_PRICE
        from execution.alpaca import get_current_price
        cur_px = get_current_price(ticker)
        if cur_px is not None and cur_px > 0 and cur_px < MIN_STOCK_PRICE:
            return False, f"{ticker} below ${MIN_STOCK_PRICE} floor (current ${cur_px:.2f}) — too illiquid"
    except Exception as _pe:
        print(f"[GUARD] Could not verify price for {ticker}: {_pe} — blocking as fail-safe (MIN_STOCK_PRICE={MIN_STOCK_PRICE})")
        return False, f"Could not verify stock price to enforce ${MIN_STOCK_PRICE} minimum: {_pe}"

    # ── Check 2: Max concurrent positions ─────────────────────────────────────
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return False, f"Max {MAX_OPEN_POSITIONS} concurrent positions reached ({len(open_positions)} open)"

    # ── Check 3: Daily trade count — read from Alpaca (persists across runs) ──
    trades_today = 0
    try:
        from execution.alpaca import _get_client, is_configured, order_status
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        if is_configured():
            today_utc    = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
            client       = _get_client()
            orders       = client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, after=today_utc, limit=500))
            # CRITICAL fix (audit C-3): order_status() normalizes "OrderStatus.FILLED"
            # → "filled" so the membership check actually matches. Previously the
            # counter was always 0 and MAX_DAILY_TRADES never enforced.
            _ACTIVE_OR_FILLED = {"filled", "partially_filled", "new",
                                 "accepted", "pending_new", "held"}
            # Count only NEW ENTRIES, never exits. APEX places every entry as a
            # BRACKET order (order_class=bracket) — both longs and shorts. Position
            # CLOSES (DUSK, AEGIS, manual close_position) are plain market orders.
            # BUG (Renato 2026-06-05): the old rule counted any non-stop/limit
            # market order, so the morning's 4 CLOSING sells (exiting prior-day
            # positions) maxed MAX_DAILY_TRADES=4 → every new buy blocked → 0 trades
            # all day. Counting bracket entries only fixes it. Bracket TP/SL children
            # have parent_order_id (still excluded).
            trades_today = sum(1 for o in orders
                               if order_status(o) in _ACTIVE_OR_FILLED
                               and not getattr(o, "parent_order_id", None)
                               and "bracket" in str(getattr(o, "order_class", "") or "").lower())
    except Exception as e:
        print(f"[THEMIS] Could not fetch daily trade count from Alpaca ({e}) — using in-memory fallback")
        trades_today = _daily_trade_count  # fall back to in-memory counter

    if trades_today >= MAX_DAILY_TRADES:
        return False, f"Daily trade limit reached ({trades_today}/{MAX_DAILY_TRADES} trades placed today)"

    # ── Check 4: Sector concentration ─────────────────────────────────────────
    if open_positions and portfolio_value and portfolio_value > 0:
        new_sector = _get_sector(ticker)

        if new_sector != "Unknown":
            # Calculate current sector exposure
            sector_value = 0.0
            for pos in open_positions:
                pos_ticker = pos.get("ticker", "")
                pos_value  = abs(float(pos.get("market_value", 0)))
                pos_sector = _get_sector(pos_ticker)
                if pos_sector == new_sector:
                    sector_value += pos_value

            # Include the proposed new position
            proposed_sector_pct = (sector_value + dollar_amount) / portfolio_value

            if proposed_sector_pct > MAX_SECTOR_PCT:
                return False, (
                    f"Sector concentration: adding {ticker} ({new_sector}) would put "
                    f"{proposed_sector_pct:.0%} of portfolio in one sector "
                    f"(max {MAX_SECTOR_PCT:.0%})"
                )

    # ── Check 5: Direction balance ─────────────────────────────────────────────
    if len(open_positions) >= 3:
        bull_count = sum(1 for p in open_positions if "long" in str(p.get("side","")).lower() or "buy" in str(p.get("side","")).lower())
        bear_count = sum(1 for p in open_positions if "short" in str(p.get("side","")).lower() or "sell" in str(p.get("side","")).lower())
        total      = len(open_positions)

        new_is_bull = direction == "bullish"
        dominant_pct = max(bull_count, bear_count) / total

        if dominant_pct >= MAX_DIRECTION_PCT:
            dominant_dir = "bullish" if bull_count > bear_count else "bearish"
            if (new_is_bull and dominant_dir == "bullish") or (not new_is_bull and dominant_dir == "bearish"):
                return True, f"WARN: {dominant_pct:.0%} of positions are {dominant_dir} — diversify direction when possible"

    # ── All checks passed ─────────────────────────────────────────────────────
    return True, "ok"


def increment_daily_count():
    """Call this after a trade is successfully placed.

    H-5 fix: counter resets on ET midnight (not UTC). On CI, datetime.today()
    returns UTC — counter rolled over at 20:00 UTC = 4 PM ET, letting
    afternoon trades stack on top of morning trades for the same trading day.
    """
    global _daily_trade_count, _daily_trade_date
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        today = datetime.today().strftime("%Y-%m-%d")   # fallback
    if _daily_trade_date != today:
        _daily_trade_count = 0
        _daily_trade_date  = today
    _daily_trade_count += 1


def get_sector_breakdown(positions: list[dict]) -> dict[str, float]:
    """
    Returns {sector: total_market_value} for all open positions.
    Useful for dashboard display.
    """
    breakdown: dict[str, float] = {}
    for pos in positions:
        ticker = pos.get("ticker", "")
        value  = abs(float(pos.get("market_value", 0)))
        sector = _get_sector(ticker)
        breakdown[sector] = breakdown.get(sector, 0.0) + value
    return dict(sorted(breakdown.items(), key=lambda x: x[1], reverse=True))
