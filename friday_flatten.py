"""Pre-closure flatten — close ALL equity positions before any market closure.

Renato's rule (2026-06-12): no position carries weekend gap risk. This closes every
open EQUITY position at market on the LAST trading session before a market closure —
not just Fridays, but the day before any holiday too. Crypto is HELD (trades 24/7,
no gap to fear).

Calendar-aware (2026-06-22): the old "Friday only" gate broke on holiday-Fridays —
when a Friday is itself a market holiday (e.g. Juneteenth 6/19, or 7/3 before the
Saturday 7/4), the Friday flatten can't run (market closed) AND never ran on the
prior session (it wasn't a Friday), so positions rode the whole long weekend — the
exact risk this exists to kill. Gate 2 now uses the Alpaca trading calendar: flatten
iff today is a trading day AND the next calendar day is NOT (weekend or holiday).

Triggered by com.illuminati.friday_flatten (launchd, every weekday 10:00 MT = 12:00 PM
ET) via scripts/friday_flatten_trigger.sh. SAFETY-GATED so a stray/coalesced wake can
never flatten at the wrong time:
  1. config CLOSE_ALL_FRIDAY must be True
  2. today must be the last trading session before a closure (calendar-aware; fails
     SAFE back to the legacy Friday rule if the calendar can't be fetched)
  3. the ET clock must be inside the flatten window AND the market must be OPEN
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _now_et():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:                      # pragma: no cover
        return datetime.utcnow() - timedelta(hours=4)


def _is_last_session_before_closure(now_et) -> bool:
    """True iff TODAY (ET) is a trading day AND the next calendar day is NOT a trading
    day — i.e. holding past today would carry a market-closure gap (weekend OR holiday).

    Uses the Alpaca trading calendar, so holidays are handled automatically: it flattens
    every normal Friday (Sat closed) AND the day before any holiday (e.g. Thursday before
    a holiday-Friday like 7/3). Fails SAFE: any calendar-fetch error falls back to the
    legacy "Friday only" rule so a transient API hiccup can't silently disable Fridays.
    """
    today = now_et.date()
    tomorrow = today + timedelta(days=1)
    try:
        from execution.alpaca import _get_client
        from alpaca.trading.requests import GetCalendarRequest
        cal = _get_client().get_calendar(
            GetCalendarRequest(start=today, end=today + timedelta(days=7)))
        trading_days = {c.date for c in cal}          # c.date is a datetime.date
        if today not in trading_days:
            return False                              # market closed today — nothing to do
        return tomorrow not in trading_days           # gap tomorrow → today is the last session
    except Exception as e:
        print(f"[FRIDAY-FLATTEN] calendar check failed ({e}) — falling back to Friday rule.")
        return now_et.weekday() == 4                  # legacy: Friday only (Mon=0 … Fri=4)


def run(force: bool = False) -> list[dict]:
    """Close all equity positions. `force=True` skips the day/time gate (manual use)."""
    from execution.alpaca import (get_positions, close_position, is_configured,
                                  cancel_all_orders, _get_client)
    from data.universe import is_crypto
    try:
        from config import CLOSE_ALL_FRIDAY, FRIDAY_FLATTEN_ET
    except ImportError:
        CLOSE_ALL_FRIDAY, FRIDAY_FLATTEN_ET = True, 15.0

    if not CLOSE_ALL_FRIDAY and not force:
        print("[FRIDAY-FLATTEN] CLOSE_ALL_FRIDAY is False — disabled.")
        return []
    if not is_configured():
        print("[FRIDAY-FLATTEN] Alpaca not configured — skipping.")
        return []

    now = _now_et()
    et_frac = now.hour + now.minute / 60.0
    if not force:
        # Gate 2 (calendar-aware): flatten only on the LAST trading session before a
        # market closure — today is a trading day AND tomorrow is NOT (weekend OR
        # holiday). Covers normal Fridays AND the day before a holiday (e.g. Thursday
        # before a holiday-Friday like 7/3) — which the old "Friday only" gate missed.
        if not _is_last_session_before_closure(now):
            print(f"[FRIDAY-FLATTEN] {now:%a %Y-%m-%d} — not the last session before a "
                  f"market closure (next calendar day still trades) — skip.")
            return []
        # Gate 3: inside the flatten window (target .. target+45min). A late/early
        # coalesced wake outside this band is skipped so we never flatten at 9 AM.
        if not (FRIDAY_FLATTEN_ET - 0.10 <= et_frac <= FRIDAY_FLATTEN_ET + 0.75):
            print(f"[FRIDAY-FLATTEN] {now:%H:%M} ET outside flatten window "
                  f"(~{FRIDAY_FLATTEN_ET:.0f}:00) — skip.")
            return []

    # Market must be OPEN — a market order after the close is rejected and leaves
    # the position open over the weekend (the exact thing we're preventing).
    try:
        if not _get_client().get_clock().is_open:
            print("[FRIDAY-FLATTEN] Market CLOSED — cannot flatten; alerting.")
            _alert("🚨 *FRIDAY FLATTEN could not run — market already CLOSED.* "
                   "Positions may carry over the weekend. Check manually.")
            return []
    except Exception as e:
        # FAIL CLOSED (2026-06-30, Codex review): if we can't confirm the market is
        # OPEN, do NOT proceed. close_position() cancels the protective stops before
        # it sends the market close, so a clock/API hiccup that lets us proceed could
        # strip stops and then fail to close — leaving the position NAKED. Skip + alert.
        print(f"[FRIDAY-FLATTEN] Could not verify market clock ({e}) — failing closed (skip).")
        _alert(f"⚠️ *FRIDAY FLATTEN skipped — could not verify the market clock* ({e}). "
               "Positions were NOT flattened and keep their stops. Check manually.")
        return []

    positions = get_positions()
    equities = [p for p in positions if not is_crypto(p["ticker"])]
    crypto = [p for p in positions if is_crypto(p["ticker"])]
    if not equities:
        print("[FRIDAY-FLATTEN] No equity positions to flatten "
              f"({len(crypto)} crypto held — trades 24/7, no weekend gap).")
        return []

    print(f"[FRIDAY-FLATTEN] {now:%a %H:%M} ET — flattening {len(equities)} "
          f"equity position(s) before the weekend:")
    results = []
    for p in equities:
        tk = p["ticker"]; pl = p.get("unrealized_pl", 0); pct = p.get("unrealized_pl_pct", 0)
        print(f"  closing {tk:6s}  P&L ${pl:+.2f} ({pct:+.1f}%)")
        res = close_position(tk)          # cancels the position's orders, then market-closes
        ok = res.get("status") in ("submitted", "closed", "ok")
        results.append({"ticker": tk, "pl": round(pl, 2), "pct": round(pct, 1),
                        "status": res.get("status"), "ok": ok})

    closed_ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    total_pl = sum(r["pl"] for r in results)
    lines = [f"🧹 *FRIDAY FLATTEN — {len(closed_ok)}/{len(results)} closed before the weekend*",
             f"> Day P&L on flattened names: ${total_pl:+,.0f}"]
    for r in results:
        mark = "✓" if r["ok"] else "⚠️"
        lines.append(f">{mark} {r['ticker']}  ${r['pl']:+.0f} ({r['pct']:+.1f}%)")
    if failed:
        lines.append(f">🚨 {len(failed)} did NOT close — CHECK MANUALLY before close: "
                     f"{', '.join(r['ticker'] for r in failed)}")
    if crypto:
        lines.append(f">_held (24/7, no weekend gap): {', '.join(p['ticker'] for p in crypto)}_")
    _alert("\n".join(lines))
    print(f"[FRIDAY-FLATTEN] done — {len(closed_ok)} closed, {len(failed)} failed.")
    return results


def _alert(text: str) -> None:
    try:
        from alerts.slack import _post
        _post({"text": text})
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    run(force="--force" in sys.argv)
