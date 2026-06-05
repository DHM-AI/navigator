"""
Intraday Position Closer — runs at 3:50 PM ET via GitHub Actions.

Rule:
  - Positions with SHORT duration ("1d", "1-3d earnings") → close at 3:50 PM
  - Positions with SWING duration ("5-7d", "2-3w") → hold overnight
    even if they were opened today

This means a swing trade entered this morning is NOT closed EOD —
it stays open until its SL, TP, or trailing stop is hit.
"""
from datetime import datetime, timezone
from execution.alpaca import get_positions, close_position, _get_client, is_configured


def _get_todays_entries() -> set[str]:
    """Return tickers where a BUY order was filled today."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from execution.alpaca import order_status   # CRITICAL audit C-4
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        orders    = _get_client().get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=today_utc, limit=500))
        # Fix C-4: order_status() normalizes "OrderStatus.FILLED" → "filled"
        # so this set actually matches. Previously DUSK never closed anything.
        return {
            o.symbol for o in orders
            if "buy" in str(getattr(o, "side", "")).lower()
            and order_status(o) in ("filled", "partially_filled")
        }
    except Exception as e:
        print(f"[DUSK] Could not fetch today's orders: {e}")
        return set()


# ONLY truly 1-day trades close at EOD.
# "1-3d" means up to 3 days — hold it.
# "2d", "3d", "5-7d", "2-3w" all hold until their natural exit.
_EOD_CLOSE_DURATIONS = ("1d", "intraday", "same-day", "same day")


def _is_eod_close(duration: str) -> bool:
    """Only pure 1-day trades close at EOD. 2d, 3d, 1-3d etc. hold."""
    d = duration.lower().strip()
    return any(d.startswith(k) or d == k for k in _EOD_CLOSE_DURATIONS)


def _get_duration_map(tickers: set[str]) -> dict[str, str]:
    """Pull today's predicted duration for each ticker from Supabase."""
    try:
        import db
        if db.db_available():
            from datetime import date
            rows = db.load_predictions_for_date(date.today().isoformat())
            return {r["ticker"]: str(r.get("duration", "")) for r in rows
                    if r.get("ticker") in tickers}
    except Exception as e:
        print(f"[DUSK] DB lookup failed: {e}")
    return {}


def run() -> list[dict]:
    if not is_configured():
        print("[DUSK] Alpaca not configured — skipping.")
        return []

    positions      = get_positions()
    todays_entries = _get_todays_entries()

    if not positions:
        print("[DUSK] No open positions.")
        return []

    opened_today  = {p["ticker"] for p in positions if p["ticker"] in todays_entries}
    duration_map  = _get_duration_map(opened_today)

    to_close = []
    to_hold  = []

    from data.universe import is_crypto
    try:
        from config import FORCE_DAY_TRADES
    except ImportError:
        FORCE_DAY_TRADES = False
    for p in positions:
        ticker   = p["ticker"]
        duration = duration_map.get(ticker, "")

        # Crypto trades 24/7 — never close at EOD, let GTC SL/TP handle exit
        if is_crypto(ticker):
            to_hold.append((p, "crypto — 24/7 GTC orders"))
            continue

        if ticker not in todays_entries:
            # Opened on a previous day — hold until its natural exit
            to_hold.append((p, f"prev day ({duration})"))
        elif FORCE_DAY_TRADES or _is_eod_close(duration):
            # FORCE_DAY_TRADES: close EVERY position opened today — NO overnight
            # holds in day-trade mode. Critical: the duration override happens at
            # execution, so the SAVED prediction may still say "5-7d" — without
            # this, DUSK would skip these and leave tight-stop positions exposed
            # to overnight gaps (Renato 2026-06-05). Otherwise: only true 1-day
            # trades close at EOD.
            to_close.append(p)
        else:
            # Opened today but holds 2d, 3d, 5-7d etc — respect its duration
            to_hold.append((p, f"{duration or 'no duration'}"))

    print(f"[DUSK] {len(positions)} positions:")
    print(f"  Closing  : {len(to_close)}")
    print(f"  Holding  : {len(to_hold)}")
    for p, reason in to_hold:
        print(f"    → Hold {p['ticker']:6s}  ({reason})")

    # Market-hours gate — if the workflow fires after 4 PM, Alpaca rejects
    # market orders silently and positions get left open overnight.
    _market_open = True
    try:
        from alpaca.trading.client import TradingClient as _DuskTC
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_LIVE_MODE
        _dusk_client = _DuskTC(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=not ALPACA_LIVE_MODE)
        _clock = _dusk_client.get_clock()
        if not _clock.is_open:
            print("[DUSK] Market is closed — skipping position close (workflow may have fired outside market hours)")
            _market_open = False
        else:
            _market_open = True
    except Exception as _ce:
        print(f"[DUSK] Could not verify market hours: {_ce} — proceeding anyway")
        _market_open = True

    if not _market_open:
        try:
            from alerts.slack import _post
            _post({"text": (
                f"⚠️ *DUSK skipped position close — market is closed*\n"
                f"> Workflow fired outside market hours. "
                f"{len(to_close)} day-trade position(s) NOT closed.\n"
                f"> Manual review required."
            )})
        except Exception:
            pass
        return []

    results = []
    for p in to_close:
        ticker = p["ticker"]
        pl     = p.get("unrealized_pl", 0)
        pl_pct = p.get("unrealized_pl_pct", 0)
        dur    = duration_map.get(ticker, "")
        print(f"  Closing {ticker:6s}  {dur}  P&L: ${pl:+.2f} ({pl_pct:+.1f}%)")
        result = close_position(ticker)
        results.append({
            "ticker":   ticker,
            "duration": dur,
            "pl":       round(pl, 2),
            "pl_pct":   round(pl_pct, 2),
            "status":   result.get("status"),
        })

    if results:
        wins  = [r for r in results if r["pl"] >= 0]
        losses= [r for r in results if r["pl"] < 0]
        net   = sum(r["pl"] for r in results)
        lines = "\n".join(
            f">{'✅' if r['pl'] >= 0 else '🔴'} *{r['ticker']}*  "
            f"{'+'if r['pl']>=0 else ''}{r['pl']:.2f} ({r['pl_pct']:+.1f}%)  {r['duration']}"
            for r in results
        )
        try:
            from alerts.slack import _post
            _post({"text": (
                f"🔔 *Intraday Close — 3:50 PM ET*\n"
                f"Closed *{len(results)}* short-term position(s)\n"
                f"{lines}\n"
                f">Net: *{'+'if net>=0 else ''}{net:.2f}*  ·  "
                f"{len(wins)} wins  ·  {len(losses)} losses\n"
                f">{len(to_hold)} swing trade(s) held overnight"
            )})
        except Exception:
            pass

    elif opened_today:
        print("[DUSK] All positions opened today are swing trades — holding overnight.")

    return results


if __name__ == "__main__":
    print(f"[DUSK] Intraday Closer — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print("=" * 50)
    results = run()
    print("=" * 50)
    print(f"Closed {len(results)} position(s)." if results else "Nothing closed.")
