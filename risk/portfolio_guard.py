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
        # AUDIT H-30 (2026-06-09): was fail-OPEN — an Alpaca hiccup let
        # duplicate entries through exactly when state is least knowable.
        # FAIL CLOSED: this setup can be taken next scan (30 min).
        return False, (f"Pending-order dedup check failed ({str(e)[:80]}) — "
                       f"blocking entry (fail-closed); retry next scan")

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
        # Fail CLOSED (2026-06-09): if we can't resolve the clock we can't
        # prove it's NOT Friday afternoon — don't open weekend-gap risk.
        return False, (f"Friday-entry time check failed ({str(e)[:80]}) — "
                       f"blocking entry (fail-closed)")

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
        return False, (f"Entry-window time check failed ({str(e)[:80]}) — "
                       f"blocking entry (fail-closed)")

    # ── Check 1a2c: No new entries at the OPEN (9:30-10:00 chop) ──────────────
    # Data (60d audit, 2026-06-08): open-hour entries were the worst window
    # (-$3,649 / 60d + most <30-min stop-outs); the 10:00 hour was the best
    # (+$6,383). The open has the widest spreads + gap volatility + stop-runs.
    # Block NEW entries before NO_ENTRY_BEFORE_ET; scans still run (visibility),
    # only entries wait for the chop to settle.
    try:
        from config import NO_ENTRY_BEFORE_ET
        from zoneinfo import ZoneInfo
        _now_et2 = datetime.now(ZoneInfo("America/New_York"))
        if (_now_et2.hour + _now_et2.minute / 60.0) < NO_ENTRY_BEFORE_ET:
            return False, (
                f"{_now_et2.strftime('%H:%M')} ET — before the {NO_ENTRY_BEFORE_ET:.2f} "
                f"open-chop cutoff. No new positions in the first 30 min "
                f"(widest spreads / gap volatility). Waiting for the open to settle."
            )
    except Exception as e:
        return False, (f"Open-chop time check failed ({str(e)[:80]}) — "
                       f"blocking entry (fail-closed)")

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
        return False, (f"Entry-cap check failed ({str(e)[:80]}) — "
                       f"blocking entry (fail-closed); retry next scan")

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
    # Single source of truth: count_daily_entries() (below). ZEUS audits the SAME
    # function so the cap and the audit can never disagree (the "13 vs 5" false
    # alarm came from ZEUS counting differently — fixed 2026-06-08).
    trades_today = count_daily_entries()

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


def count_daily_entries() -> int:
    """Authoritative count of NEW position ENTRIES opened today — the number the
    MAX_DAILY_TRADES cap is enforced against AND what ZEUS audits (one source so
    the two can never drift; the "13 vs 5" false alarm was ZEUS counting its own way).

    Counts only OPENS, never exits:
      - Equity entries are BRACKET parents (order_class=bracket, longs + shorts).
        nested=True nests the TP/SL exit legs INSIDE the parent so they don't appear
        as top-level orders — important because Alpaca leaves parent_order_id EMPTY
        on filled bracket legs, which otherwise let exit sells be miscounted as
        entries (the morning's 3 stop-outs inflated the count 5 -> 8).
      - Crypto entries aren't brackets (separate stop/limit) — also count crypto BUY
        market orders (crypto is long-only: a buy = an open).
      - Options are a separate, isolated subsystem (not bracket/crypto) and are
        excluded — they never consume the equity daily cap.
    Falls back to the in-memory counter only if Alpaca can't be read.
    """
    try:
        from datetime import datetime, timezone
        from execution.alpaca import _get_client, is_configured, order_status
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        if not is_configured():
            return _daily_trade_count
        # AUDIT H-13 (2026-06-09): "today" must be the ET TRADING day, not UTC.
        # UTC midnight = 8 PM ET: ZEUS's 11 PM ET audit counted ~0, and the
        # live cap window rolled over mid-evening.
        try:
            from zoneinfo import ZoneInfo
            _et_mid = datetime.now(ZoneInfo("America/New_York")).replace(
                hour=0, minute=0, second=0, microsecond=0)
            _after = _et_mid.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            _after = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        orders = _get_client().get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=_after, limit=500, nested=True))
        _ACTIVE_OR_FILLED = {"filled", "partially_filled", "new",
                             "accepted", "pending_new", "held"}

        def _is_entry(o):
            if getattr(o, "parent_order_id", None):
                return False                       # bracket exit leg (when populated)
            oc = str(getattr(o, "order_class", "") or "").lower()
            if "bracket" in oc:
                return True                        # equity entry parent (long or short)
            sym  = str(getattr(o, "symbol", "") or "")
            side = str(getattr(o, "side", "") or "").lower()
            otyp = str(getattr(o, "type", "") or "").lower()
            return ("/" in sym and side == "buy" and "market" in otyp)  # crypto open

        return sum(1 for o in orders
                   if order_status(o) in _ACTIVE_OR_FILLED and _is_entry(o))
    except Exception as e:
        print(f"[THEMIS] Could not fetch daily entry count from Alpaca ({e}) — in-memory fallback")
        return _daily_trade_count


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


# Finding #20 (2026-06-08): removed dead get_sector_breakdown() — zero callers in
# either repo (the dashboard computes sector exposure client-side / server-side in
# functions/api/dashboard.js, not via this). Restore from git history if a
# health/alert payload ever needs server-side sector totals.
