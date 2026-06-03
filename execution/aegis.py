from __future__ import annotations
"""
AEGIS — trailing stop manager extracted from execution/alpaca.py.

Contains trail_positions(), called every 30 min during market hours.
Helpers (_get_client, get_positions, etc.) are imported INSIDE the function
to avoid a circular import: alpaca.py does `from execution.aegis import
trail_positions` at module level, so aegis.py must not import from
execution.alpaca at module level.
"""
from datetime import datetime

from config import KELLY_LOSS_PCT, MOVE_TARGET_PCT


def trail_positions(
    trigger_pct: float | None = None,
    trail_pct:   float | None = None,
) -> list[dict]:
    """
    Trailing stop manager — call every 30 min during market hours.

    For every LONG position that is up ≥ trigger_pct:
      1. Cancel the existing fixed stop-loss order (if still open)
      2. Leave the take-profit limit order untouched
      3. Place an Alpaca native trailing stop (GTC) at trail_pct below peak

    Partial exit (two-tier scale-out) — if ENABLE_PARTIAL_EXIT is True:
      Tier 1 at +7%:  close 20% of original, move stop to breakeven
      Tier 2 at +12%: close another 20% (same qty as T1, = 20% of original)
      Remaining 60%:  rides with AEGIS trailing stop
      Each tier logged to Supabase so it never fires twice per position.

    Already-trailing positions are detected by order type and skipped,
    so this is safe to call repeatedly.

    Returns a list of dicts for positions that were upgraded to trailing.
    """
    # Deferred imports to break circular dependency with execution.alpaca
    from execution.alpaca import (
        _get_client,
        get_positions,
        is_active_order,
        is_live_mode,
        close_position,
        order_status,
        is_configured,
    )
    from config import (TRAIL_TRIGGER_PCT, TRAIL_PCT, TRAIL_TIGHTEN_LEVELS,
                        ENABLE_PARTIAL_EXIT, PARTIAL_EXIT_MOVE_TO_BE,
                        PARTIAL_EXIT_TIER1_TRIGGER, PARTIAL_EXIT_TIER1_FRACTION,
                        PARTIAL_EXIT_TIER2_TRIGGER, PARTIAL_EXIT_TIER2_FRACTION)

    trigger = trigger_pct if trigger_pct is not None else TRAIL_TRIGGER_PCT
    trail   = trail_pct   if trail_pct   is not None else TRAIL_PCT

    def _target_trail(pct_gain_decimal: float) -> float:
        """Return the tightest applicable trail % for this gain level."""
        for min_gain, t_pct in TRAIL_TIGHTEN_LEVELS:
            if pct_gain_decimal >= min_gain:
                return t_pct
        return trail

    if not is_configured():
        return []

    results = []
    try:
        from alpaca.trading.requests import (GetOrdersRequest,
                                              TrailingStopOrderRequest,
                                              MarketOrderRequest,
                                              StopOrderRequest)
        from alpaca.trading.enums import (QueryOrderStatus, OrderSide,
                                          TimeInForce)

        client    = _get_client()
        positions = get_positions()
        if not positions:
            return []

        # CRITICAL audit C-6: load partial-exit history filtered to currently-
        # open tickers ONLY. Otherwise a stale t1/t2 record from a CLOSED prior
        # position in the same ticker makes the new position skip its scale-out.
        _open_tickers = {p["ticker"] for p in positions}
        partial_history: dict = {}
        _partial_exit_db_ok = True   # track if DB is available for partial exits
        if ENABLE_PARTIAL_EXIT:
            try:
                from db import get_partial_exit_history
                _ph = get_partial_exit_history(open_tickers=_open_tickers)
                if _ph is None:
                    # DB error — disable partial exits this cycle to prevent double-firing
                    _partial_exit_db_ok = False
                    print("[AEGIS] Partial exit history unavailable — skipping T1/T2 this cycle")
                else:
                    partial_history = _ph
            except Exception as _dbe:
                _partial_exit_db_ok = False
                print(f"[AEGIS] Could not load partial exit history: {_dbe}")

        # Build map: ticker → active order list
        # Must include HELD orders — bracket stop/TP children are held, not open.
        # Without held orders, AEGIS can't see or cancel bracket stops.
        from alpaca.trading.enums import QueryOrderStatus as _QOS
        _all_active = client.get_orders(
            GetOrdersRequest(status=_QOS.ALL, limit=400))
        open_orders = [o for o in _all_active if is_active_order(o)]
        orders_by_ticker: dict = {}
        for o in open_orders:
            orders_by_ticker.setdefault(o.symbol, []).append(o)

        # Load penny-stock floor — rule: no positions below MIN_STOCK_PRICE
        try:
            from config import MIN_STOCK_PRICE as _MIN_PRICE
        except Exception:
            _MIN_PRICE = 5.0

        for p in positions:
            ticker   = p["ticker"]
            pct_gain = p.get("unrealized_pl_pct", 0)  # already in %
            qty      = abs(float(p["qty"]))  # shorts come through as negative
            raw_side = str(p.get("side", "")).lower()
            is_long  = "long" in raw_side or "buy" in raw_side
            cur_px   = float(p.get("current_price", 0) or 0)

            ticker_orders = orders_by_ticker.get(ticker, [])
            pct_gain_decimal = pct_gain / 100.0

            # ══════════════════════════════════════════════════════════════════
            # CIRCUIT BREAKER: hard per-trade max loss (the safety net under the
            # normal stop). The 3% stop should never let a position reach -8%, but
            # on 2026-05-27/28 FLY (-15.9%), VOYG (-15.5%), YMAT (-10.8%) each lost
            # ~$1,000+ on ONE trade because the stop never fired (status "manual").
            # If a position is down past HARD_MAX_LOSS_PCT, force-close at market
            # NOW — don't wait for a stop that clearly isn't working.
            # Runs FIRST: a blown-through position must exit before anything else.
            # ══════════════════════════════════════════════════════════════════
            try:
                from config import HARD_MAX_LOSS_PCT
                if pct_gain_decimal <= -HARD_MAX_LOSS_PCT:
                    print(f"[AEGIS] {ticker} HARD MAX LOSS breached ({pct_gain:.1f}% "
                          f"≤ -{HARD_MAX_LOSS_PCT*100:.0f}%) — force-closing at market")
                    try:
                        close_position(ticker)
                        results.append({
                            "ticker": ticker, "pct_gain": round(pct_gain, 2),
                            "trail_pct": 0, "order_id": "n/a",
                            "cancelled_sl": 0, "status": "force_closed_max_loss",
                            "timestamp": datetime.now().isoformat(),
                        })
                        from alerts.slack import _post
                        _post({"text": (
                            f"🛑 *HARD MAX-LOSS force-close — {ticker}*\n"
                            f">{('LONG' if is_long else 'SHORT')} {qty:g} @ ${cur_px:.2f} "
                            f"· P&L *{pct_gain:+.1f}%* (past -{HARD_MAX_LOSS_PCT*100:.0f}% ceiling)\n"
                            f">The normal stop did not fire — AEGIS closed it as a safety net."
                        )})
                    except Exception as _fce:
                        print(f"[AEGIS] {ticker} force-close FAILED: {_fce}")
                        try:
                            from alerts.slack import _post
                            _post({"text": (
                                f"🚨 *{ticker} past -{HARD_MAX_LOSS_PCT*100:.0f}% but "
                                f"force-close FAILED: {str(_fce)[:120]} — CLOSE MANUALLY*"
                            )})
                        except Exception:
                            pass
                    continue   # position closing — skip all other logic
            except Exception as _hml:
                print(f"[AEGIS] {ticker} hard-max-loss check error: {_hml}")

            # ══════════════════════════════════════════════════════════════════
            # RULE: no penny stocks. If a position drifted below MIN_STOCK_PRICE,
            # close it at market — wide spreads + thin liquidity are killers.
            # Runs FIRST so we don't waste an AEGIS cycle placing stops on a
            # position we're about to close anyway.
            # ══════════════════════════════════════════════════════════════════
            if cur_px > 0 and cur_px < _MIN_PRICE:
                try:
                    print(f"[AEGIS] {ticker} below ${_MIN_PRICE} floor (cur ${cur_px:.2f}) — auto-closing")
                    _close_result = close_position(ticker)
                    results.append({
                        "ticker": ticker, "pct_gain": round(pct_gain, 2),
                        "trail_pct": 0, "order_id": "n/a",
                        "cancelled_sl": 0, "status": "closed_penny_stock",
                        "timestamp": datetime.now().isoformat(),
                    })
                    try:
                        from alerts.slack import _post
                        _post({"text": (
                            f"🪙 *Penny-stock auto-close — {ticker}*\n"
                            f">{('LONG' if is_long else 'SHORT')} {qty:g} @ ${cur_px:.2f} "
                            f"(below ${_MIN_PRICE:.2f} floor) · P&L {pct_gain:+.1f}%"
                        )})
                    except Exception:
                        pass
                except Exception as _ce:
                    print(f"[AEGIS] {ticker} penny-close FAILED: {_ce}")
                continue   # skip stop / trailing logic — position is closing

            # ══════════════════════════════════════════════════════════════════
            # RULE: every position MUST have a stop, ALWAYS.
            # Runs BEFORE partial-exit / trailing logic so naked LOSING positions
            # get rescued too (those don't qualify for any other branch).
            # ══════════════════════════════════════════════════════════════════
            _has_stop = any(
                "stop" in str(getattr(o, "type", "")).lower()
                for o in ticker_orders
            )
            if not _has_stop:
                _entry_px = float(p.get("avg_entry_price", 0) or 0)
                _cur_px   = float(p.get("current_price", _entry_px))
                if _entry_px > 0 and _cur_px > 0 and qty > 0:
                    # Constraint:
                    #   LONG  → stop MUST be BELOW current price
                    #   SHORT → stop MUST be ABOVE current price
                    # For positions still in profit (or near entry), use the
                    # tighter of (entry-based 3% risk) and (current ±3%).
                    # For positions already underwater past the entry stop,
                    # the entry-based stop is invalid (wrong side of current);
                    # cap loss from CURRENT price instead.
                    if is_long:
                        risk_stop = round(_entry_px * (1 - KELLY_LOSS_PCT), 2)
                        cur_stop  = round(_cur_px   * (1 - KELLY_LOSS_PCT), 2)
                        if risk_stop < _cur_px:
                            # Entry-based stop still valid — use tighter of the two
                            new_stop = max(risk_stop, cur_stop)
                        else:
                            # Already underwater past entry stop → cap from current
                            new_stop = cur_stop
                        stop_side = OrderSide.SELL
                    else:
                        risk_stop = round(_entry_px * (1 + KELLY_LOSS_PCT), 2)
                        cur_stop  = round(_cur_px   * (1 + KELLY_LOSS_PCT), 2)
                        if risk_stop > _cur_px:
                            new_stop = min(risk_stop, cur_stop)
                        else:
                            new_stop = cur_stop
                        stop_side = OrderSide.BUY
                    # H-8 fix: Alpaca rejects fractional-qty stop orders. For
                    # fractional positions, use whole-share qty (floor) for the
                    # stop; the fractional dust is unprotected but small enough
                    # that the dollar risk is acceptable, and at least the bulk
                    # of the position gets covered (previously it stayed fully
                    # naked forever because every retry failed).
                    import math as _math
                    _is_fractional = qty != int(qty)
                    _stop_qty = int(_math.floor(qty)) if _is_fractional else qty
                    if _stop_qty <= 0:
                        # Pure-fractional (e.g. 0.7 BTC) — close instead of stop
                        try:
                            print(f"[AEGIS] {ticker} pure-fractional naked → close_position")
                            close_position(ticker)
                        except Exception as _cf:
                            print(f"[AEGIS] {ticker} fractional close failed: {_cf}")
                        continue
                    try:
                        from alpaca.trading.requests import StopOrderRequest as _SOR
                        # If shares are held by a stale TP limit order (from a
                        # partially-failed bracket cancel in a prior AEGIS run),
                        # cancel ALL active orders for this ticker first so the
                        # rescue stop can actually be placed.
                        _rescue_req = _SOR(
                            symbol=ticker, qty=_stop_qty, side=stop_side,
                            stop_price=new_stop, time_in_force=TimeInForce.GTC,
                        )
                        try:
                            _rescue_ord = client.submit_order(_rescue_req)
                        except Exception as _first_err:
                            _first_err_s = str(_first_err).lower()
                            if ("insufficient" in _first_err_s or "held_for_orders" in _first_err_s
                                    or "40310000" in str(_first_err)):
                                # Stale orders holding the shares — sweep and retry once
                                print(f"[AEGIS] {ticker} rescue stop hit held_for_orders — cancelling all orders and retrying")
                                try:
                                    _all_t = client.get_orders(GetOrdersRequest(status=_QOS.ALL, limit=200))
                                    for _ot in _all_t:
                                        if _ot.symbol == ticker and is_active_order(_ot):
                                            try:
                                                client.cancel_order_by_id(str(_ot.id))
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                                import time as _rescue_time
                                _rescue_time.sleep(2.0)
                                # Retry GTC then DAY
                                try:
                                    _rescue_ord = client.submit_order(_rescue_req)
                                except Exception:
                                    _rescue_req.time_in_force = TimeInForce.DAY
                                    _rescue_ord = client.submit_order(_rescue_req)
                            else:
                                # Not a held_for_orders error — try DAY fallback then give up
                                _rescue_req.time_in_force = TimeInForce.DAY
                                _rescue_ord = client.submit_order(_rescue_req)
                        _frac_note = f" (fractional dust {qty - _stop_qty:.4f} uncovered)" if _is_fractional else ""
                        print(f"[AEGIS] {ticker} was NAKED — placed rescue stop @ ${new_stop:.2f}{_frac_note}")
                        results.append({
                            "ticker": ticker, "pct_gain": round(pct_gain, 2),
                            "trail_pct": KELLY_LOSS_PCT, "order_id": str(_rescue_ord.id),
                            "cancelled_sl": 0, "status": "rescue_stop_placed",
                            "timestamp": datetime.now().isoformat(),
                        })
                        # Slack ping — naked positions are a safety incident.
                        # N-7 fix: dedup per ticker per day so a stuck naked
                        # position doesn't fire 32 Slack messages/day (one
                        # per 15-min AEGIS run).
                        try:
                            import db as _db
                            _send = True
                            if _db.db_available():
                                from datetime import datetime as _dt, timedelta as _td
                                _cutoff = (_dt.utcnow() - _td(hours=8)).isoformat()
                                _hits = (_db._client().table("trades")
                                            .select("timestamp")
                                            .eq("status", f"aegis_rescue_alert:{ticker}")
                                            .gte("timestamp", _cutoff)
                                            .limit(1).execute())
                                if _hits.data:
                                    _send = False
                                else:
                                    _db.save_trade({
                                        "order_id": f"aegis-alert-{ticker}-{int(datetime.now().timestamp())}",
                                        "ticker": ticker, "side": "alert", "dollar_amount": 0,
                                        "mode": "LIVE" if is_live_mode() else "PAPER",
                                        "status": f"aegis_rescue_alert:{ticker}",
                                        "reason": "naked_rescue", "timestamp": datetime.now().isoformat(),
                                    })
                            if _send:
                                from alerts.slack import _post
                                _post({"text": (
                                    f"🩹 *Naked position rescued — {ticker}*\n"
                                    f">{('LONG' if is_long else 'SHORT')} {qty:g} @ avg ${_entry_px:.2f} · "
                                    f"current ${_cur_px:.2f} · P&L {pct_gain:+.1f}%\n"
                                    f">Placed rescue stop @ ${new_stop:.2f} "
                                    f"({KELLY_LOSS_PCT*100:.0f}% risk cap from "
                                    f"{'entry' if new_stop == risk_stop else 'current price'})\n"
                                    f">_Further rescue alerts for {ticker} silenced for 8h_"
                                )})
                        except Exception:
                            pass
                        # Re-fetch this ticker's orders so subsequent logic sees the rescue stop
                        ticker_orders = ticker_orders + [_rescue_ord]
                    except Exception as _re:
                        print(f"[AEGIS] {ticker} naked-rescue FAILED: {_re}")
                        # Even louder Slack — couldn't place a stop, requires human
                        try:
                            from alerts.slack import _post
                            _post({"text": (
                                f"🚨 *NAKED POSITION — could not place stop: {ticker}*\n"
                                f">{('LONG' if is_long else 'SHORT')} {qty:g} @ ${_entry_px:.2f}\n"
                                f">Error: {str(_re)[:200]}\n"
                                f">*Action required:* manually place a stop or close the position."
                            )})
                        except Exception:
                            pass
            # ══════════════════════════════════════════════════════════════════

            # ── Two-tier partial exit (scale-out) ─────────────────────────────
            # Tier 1 at +7%:  close 20% of ORIGINAL, move stop to breakeven
            # Tier 2 at +12%: close another 20% (= same qty as T1, so 40% closed)
            # Remaining 60%:  rides with multi-level trailing stop
            if ENABLE_PARTIAL_EXIT and _partial_exit_db_ok:
                _hist = partial_history.get(ticker, {"t1": False, "t2": False, "t1_qty": 0.0})
                _abs_qty   = abs(float(qty))
                _entry_px  = float(p.get("avg_entry_price", 0))
                _current_px = float(p.get("current_price", 0))

                def _fire_tier(tier_name: str, fraction_to_close: float,
                               trigger_pct: float, move_to_be: bool):
                    """Inner helper — fire one partial exit tier."""
                    nonlocal _abs_qty
                    import math as _math

                    # qty to close = 20% of ORIGINAL position (the entry qty, not the
                    # current remaining qty, which can differ if external sells happened).
                    # T1 records original_qty so T2 can close the same absolute amount.
                    if tier_name == "t1":
                        raw_qty = _abs_qty * fraction_to_close
                        # Store the original position size so T2 can reference it —
                        # in-memory only (no schema change needed; protects the same-
                        # cycle case where both tiers fire in one AEGIS run).
                        _hist["original_qty"] = _abs_qty
                    else:
                        # T2 qty: prefer same-absolute-qty as T1 (closes equal tranches).
                        # Priority: stored t1_qty (most accurate) > inferred from
                        # original_qty > fallback to current remaining * fraction.
                        t1_qty_stored = _hist.get("t1_qty", 0.0)
                        orig_qty = _hist.get("original_qty", 0.0)
                        if t1_qty_stored > 0:
                            raw_qty = t1_qty_stored          # exact same shares as T1
                        elif orig_qty > 0:
                            raw_qty = orig_qty * fraction_to_close   # infer from stored original
                        else:
                            raw_qty = _abs_qty * fraction_to_close   # fallback: use current qty
                        raw_qty = min(raw_qty, _abs_qty)     # never try to sell more than held

                    # ALWAYS floor to whole shares — cleaner orders, no Alpaca
                    # complaints on non-fractionable assets, no weird .05 / .79
                    # share displays in Slack alerts. The dropped fractional part
                    # rides with the trailing stop alongside the remaining 60%.
                    close_qty  = int(_math.floor(raw_qty))
                    if close_qty <= 0:
                        return None
                    # Remaining can still be fractional if the position was opened
                    # that way (fractional buy) — that's fine for the stop order.
                    remain_qty = round(_abs_qty - close_qty, 4)
                    exit_side  = OrderSide.SELL if is_long else OrderSide.BUY

                    # ── Snapshot existing stops AND bracket TP legs BEFORE
                    # cancelling. Restore on failure. CRITICAL audit C-7: was
                    # only cancelling stops — the bracket's LIMIT TP leg was
                    # left at original qty, so when remaining tranche hits TP
                    # it tries to sell MORE shares than held → Alpaca rejects
                    # → uncovered position.
                    # ── Part B fix (2026-06-03): cancel using a LIVE order fetch,
                    # not the stale `ticker_orders` snapshot. The snapshot could
                    # miss an order placed by a prior run or a HELD leg, so the
                    # cancel silently skipped it; that order kept "holding" the
                    # full qty and every replacement then failed with
                    # held_for_orders (40310000) → naked/un-trailed remainder
                    # (the INTC incident). Re-fetch live, cancel ALL active
                    # stop/TP legs, then VERIFY they actually cleared before we
                    # place replacements — only then is the held qty truly freed.
                    _stop_snapshots = []
                    _tp_snapshots   = []
                    _cancelled_ids  = []
                    try:
                        from alpaca.trading.requests import GetOrdersRequest as _GOR
                        from alpaca.trading.enums import QueryOrderStatus as _QOS
                        _live_orders = [o for o in client.get_orders(
                            _GOR(status=_QOS.ALL, limit=200))
                            if o.symbol == ticker and is_active_order(o)]
                    except Exception as _lf_err:
                        print(f"[AEGIS] {ticker} {tier_name}: live order re-fetch failed "
                              f"({str(_lf_err)[:60]}) — falling back to snapshot list")
                        _live_orders = [o for o in ticker_orders if is_active_order(o)]
                    for o in _live_orders:
                        otype = str(getattr(o, "type", "") or getattr(o, "order_type", "")).lower()
                        if "stop" in otype or "trail" in otype:
                            _stop_snapshots.append({
                                "type":  otype,
                                "qty":   float(o.qty) if o.qty else _abs_qty,
                                "side":  o.side,
                                "stop_price":    float(o.stop_price) if o.stop_price else None,
                                "trail_percent": float(getattr(o, "trail_percent", 0) or 0),
                            })
                            try:
                                client.cancel_order_by_id(str(o.id)); _cancelled_ids.append(str(o.id))
                            except Exception:
                                pass
                        elif otype == "limit":
                            # Any active limit on this symbol is a TP leg (bracket
                            # child OR a standalone TP from a prior reissue) — it
                            # also holds qty, so cancel + resize after the partial.
                            _tp_snapshots.append({
                                "qty":         float(o.qty) if o.qty else _abs_qty,
                                "side":        o.side,
                                "limit_price": float(o.limit_price) if o.limit_price else None,
                            })
                            try:
                                client.cancel_order_by_id(str(o.id)); _cancelled_ids.append(str(o.id))
                            except Exception:
                                pass
                    # VERIFY cancellations cleared (frees held_for_orders qty) before
                    # we place any replacement. Poll briefly; canceled/expired/filled
                    # all count as "no longer holding qty".
                    if _cancelled_ids:
                        import time as _time
                        from alpaca.trading.enums import QueryOrderStatus as _QOS2
                        from alpaca.trading.requests import GetOrdersRequest as _GOR2
                        for _attempt in range(6):   # ~3s max
                            try:
                                _still = {str(o.id) for o in client.get_orders(
                                    _GOR2(status=_QOS2.ALL, limit=200))
                                    if o.symbol == ticker and is_active_order(o)
                                    and str(o.id) in _cancelled_ids}
                            except Exception:
                                _still = set()
                            if not _still:
                                break
                            _time.sleep(0.5)
                        else:
                            print(f"[AEGIS] {ticker} {tier_name}: ⚠ {len(_still)} order(s) "
                                  f"still active after cancel poll — replacements may conflict")

                    # Market-close the tier qty — if it fails, RESTORE the stop
                    mreq = MarketOrderRequest(
                        symbol=ticker, qty=close_qty, side=exit_side,
                        time_in_force=TimeInForce.DAY,
                    )
                    try:
                        close_order = client.submit_order(mreq)
                    except Exception as _sell_err:
                        # CRITICAL: restore the stops AND TP we just cancelled.
                        # H-9 fix: restore trailing stops as TrailingStopOrderRequest
                        # NEVER as a fixed StopOrderRequest with snapshotted price —
                        # snapshot captured peak-derived stop_price which is now stale.
                        for _snap in _stop_snapshots:
                            try:
                                if "trailing" in _snap["type"] and _snap["trail_percent"]:
                                    from alpaca.trading.requests import TrailingStopOrderRequest as _TSR
                                    _restore = _TSR(
                                        symbol=ticker, qty=_snap["qty"], side=_snap["side"],
                                        time_in_force=TimeInForce.GTC,
                                        trail_percent=_snap["trail_percent"],
                                    )
                                elif "trailing" in _snap["type"]:
                                    # Trailing but no trail_percent snapshot — skip
                                    # rather than degrade to a stale fixed stop.
                                    print(f"[AEGIS] {ticker} skipped trailing restore (no trail_percent)")
                                    continue
                                else:
                                    _restore = StopOrderRequest(
                                        symbol=ticker, qty=_snap["qty"], side=_snap["side"],
                                        stop_price=_snap["stop_price"],
                                        time_in_force=TimeInForce.GTC,
                                    )
                                client.submit_order(_restore)
                            except Exception:
                                try:
                                    _restore.time_in_force = TimeInForce.DAY
                                    client.submit_order(_restore)
                                except Exception:
                                    pass
                        # Also restore TP legs at ORIGINAL qty (sell didn't happen)
                        for _tp in _tp_snapshots:
                            try:
                                from alpaca.trading.requests import LimitOrderRequest as _LOR
                                _tpr = _LOR(symbol=ticker, qty=_tp["qty"], side=_tp["side"],
                                            limit_price=_tp["limit_price"],
                                            time_in_force=TimeInForce.GTC)
                                client.submit_order(_tpr)
                            except Exception:
                                pass
                        print(f"[AEGIS] {ticker} {tier_name} sell failed: {_sell_err} — restored stops+TP")
                        raise

                    # T1 moves remaining stop to breakeven
                    # T2 leaves stop at breakeven (already there from T1)
                    # CRITICAL: after a successful partial sell, we MUST place a stop.
                    # If the ideal stop (breakeven) fails, fall back to any stop below
                    # current price — naked is never acceptable after a sell.
                    if remain_qty > 0 and _entry_px > 0:
                        be_side = OrderSide.SELL if is_long else OrderSide.BUY
                        # Try ideal stop (breakeven if move_to_be, else use current-3%)
                        _stop_px = round(_entry_px, 2) if move_to_be else round(
                            (_current_px * 0.97 if is_long else _current_px * 1.03), 2)
                        _stop_placed = False
                        for _tif in [TimeInForce.GTC, TimeInForce.DAY]:
                            try:
                                client.submit_order(StopOrderRequest(
                                    symbol=ticker, qty=remain_qty, side=be_side,
                                    stop_price=_stop_px, time_in_force=_tif))
                                _stop_placed = True
                                break
                            except Exception:
                                pass
                        if not _stop_placed:
                            # Last-resort: place stop 3% below/above current — anything > naked
                            _fallback_px = round((_current_px * 0.97 if is_long else _current_px * 1.03), 2)
                            try:
                                client.submit_order(StopOrderRequest(
                                    symbol=ticker, qty=remain_qty, side=be_side,
                                    stop_price=_fallback_px, time_in_force=TimeInForce.DAY))
                                _stop_placed = True
                                print(f"[AEGIS] {ticker} {tier_name}: breakeven stop failed — placed fallback stop @ ${_fallback_px:.2f}")
                            except Exception as _se:
                                print(f"[AEGIS] {ticker} {tier_name}: ALL stop placements failed: {_se}")
                        if not _stop_placed:
                            # Genuinely naked after partial sell — fire Slack alert
                            try:
                                from alerts.slack import _post
                                _post({"text": (
                                    f"🚨 *NAKED after partial exit — {ticker}*\n"
                                    f">Sold {close_qty} shares but FAILED to place replacement stop\n"
                                    f">Remaining: {remain_qty} shares · *Manual stop required NOW*"
                                )})
                            except Exception:
                                pass

                    # CRITICAL audit C-7: reissue bracket TP sized to remain_qty.
                    # Was leaving orphan TP at original qty → would over-sell on
                    # exit and Alpaca would reject → no TP coverage on the tranche.
                    # R-2 fix: reissue TP sized to remain_qty. If broker had a
                    # bracket TP, reuse its limit price. If not (position came
                    # from _place_simple_order / naked rescue / sentiment_guard
                    # replacement / crypto), synthesize one at MOVE_TARGET_PCT
                    # from entry — otherwise the tranche has NO upside exit at all.
                    if remain_qty > 0:
                        try:
                            from alpaca.trading.requests import LimitOrderRequest as _LOR
                            from config import MOVE_TARGET_PCT as _MTP
                            if _tp_snapshots:
                                _tp0 = _tp_snapshots[0]
                                _tp_side, _tp_px = _tp0["side"], _tp0["limit_price"]
                                _src = "broker"
                            else:
                                _tp_side = OrderSide.SELL if is_long else OrderSide.BUY
                                _tp_px = (round(_entry_px * (1 + _MTP), 2)
                                          if is_long else
                                          round(_entry_px * (1 - _MTP), 2))
                                _src = "synthesized"
                            _tp_req = _LOR(
                                symbol=ticker, qty=remain_qty, side=_tp_side,
                                limit_price=_tp_px,
                                time_in_force=TimeInForce.GTC,
                            )
                            client.submit_order(_tp_req)
                            print(f"[AEGIS] {ticker} TP ({_src}) sized to {remain_qty} @ ${_tp_px:.2f}")
                        except Exception as _tpe:
                            print(f"[AEGIS] {ticker} could not reissue TP at remain_qty: {_tpe}")

                    # Log to Supabase — status encodes which tier fired.
                    # CRITICAL: if this write fails and is not retried, the next
                    # AEGIS run reads the DB, sees no t1/t2 record, and re-fires
                    # the same tier (double partial exit). Retry once with a short
                    # sleep before accepting failure.
                    _trade_record = {
                        "order_id":     str(close_order.id),
                        "ticker":       ticker,
                        "side":         "sell_partial" if is_long else "buy_partial",
                        "dollar_amount": round(close_qty * _current_px, 2),
                        "mode":         "LIVE" if is_live_mode() else "PAPER",
                        "status":       f"partial_exit_{tier_name}",
                        "reason":       (f"Tier-{tier_name.upper()[1]} scale-out "
                                         f"{fraction_to_close*100:.0f}% at +{pct_gain:.1f}% "
                                         f"gain qty={close_qty}"),
                        "timestamp":    datetime.now().isoformat(),
                    }
                    try:
                        from db import save_trade
                        save_trade(_trade_record)
                    except Exception as dbe:
                        print(f"[AEGIS] Could not log {tier_name} to DB: {dbe} — retrying in 2s")
                        import time as _save_time
                        _save_time.sleep(2.0)
                        try:
                            save_trade(_trade_record)
                            print(f"[AEGIS] {ticker} {tier_name} DB write succeeded on retry")
                        except Exception as dbe2:
                            print(f"[AEGIS] {ticker} {tier_name} DB retry also failed: {dbe2} "
                                  f"— WARNING: next AEGIS run may re-fire {tier_name} "
                                  f"(in-memory guard active for this run only)")

                    pnl_locked = round(close_qty * (_current_px - _entry_px)
                                       * (1 if is_long else -1), 2)
                    mode_tag   = "LIVE" if is_live_mode() else "PAPER"
                    tier_lbl   = "T1" if tier_name == "t1" else "T2"
                    print(f"[AEGIS] [{mode_tag}] ✂️ PARTIAL EXIT {tier_lbl} {ticker}: "
                          f"closed {close_qty} shares at +{pct_gain:.1f}% "
                          f"(locked ${pnl_locked:+.2f}), remaining {remain_qty} shares")

                    try:
                        from alerts.slack import _post
                        be_note = f" · stop → breakeven (${_entry_px:.2f})" if move_to_be else ""
                        _post({"text": (
                            f"✂️ *Partial exit {tier_lbl} — {ticker}* (+{pct_gain:.1f}%)\n"
                            f">Closed *{fraction_to_close*100:.0f}%* "
                            f"({close_qty} shares) · locked in *${pnl_locked:+.2f}*\n"
                            f">Remaining *{remain_qty} shares*{be_note}"
                        )})
                    except Exception:
                        pass

                    _hist[tier_name] = True
                    if tier_name == "t1":
                        _hist["t1_qty"] = close_qty
                    partial_history[ticker] = _hist
                    _abs_qty = remain_qty  # update for any subsequent tier this run

                    return {
                        "ticker": ticker, "action": f"partial_exit_{tier_name}",
                        "pct_gain": round(pct_gain, 2), "qty_closed": close_qty,
                        "qty_remaining": remain_qty, "pnl_locked": pnl_locked,
                        "order_id": str(close_order.id),
                        "timestamp": datetime.now().isoformat(),
                    }

                fired_any = False

                # ── Re-fetch this ticker's orders right before _fire_tier ──────
                # The global ticker_orders snapshot (built at loop start) can miss
                # orders placed externally between snapshot and now. _fire_tier
                # cancels stops and TP legs from ticker_orders — a stale snapshot
                # means those externally-placed orders survive the partial exit at
                # their ORIGINAL qty, leading to an over-sell when they fill later.
                # Refreshing here is cheap (one API call) and keeps the cancel list
                # accurate. ticker_orders is a closure variable — updating it here
                # is visible to _fire_tier when it's called below.
                if (_hist.get("t1", False) is False or _hist.get("t2", False) is False):
                    try:
                        _fresh_all = client.get_orders(
                            GetOrdersRequest(status=_QOS.ALL, limit=200))
                        ticker_orders = [o for o in _fresh_all
                                         if is_active_order(o) and o.symbol == ticker]
                        print(f"[AEGIS] {ticker} refreshed orders for _fire_tier "
                              f"({len(ticker_orders)} active)")
                    except Exception as _ro_err:
                        print(f"[AEGIS] {ticker} could not refresh orders: {_ro_err} "
                              f"— using snapshot ({len(ticker_orders)} orders)")

                # Tier 1
                if not _hist.get("t1", False) and pct_gain_decimal >= PARTIAL_EXIT_TIER1_TRIGGER:
                    try:
                        r = _fire_tier("t1", PARTIAL_EXIT_TIER1_FRACTION,
                                       PARTIAL_EXIT_TIER1_TRIGGER,
                                       move_to_be=PARTIAL_EXIT_MOVE_TO_BE)
                        if r:
                            results.append(r)
                            fired_any = True
                    except Exception as e:
                        print(f"[AEGIS] Tier 1 exit failed for {ticker}: {e}")

                # Tier 2 — only check if T1 already fired AND gain ≥ 12%
                if (_hist.get("t1", False) and not _hist.get("t2", False)
                        and pct_gain_decimal >= PARTIAL_EXIT_TIER2_TRIGGER):
                    try:
                        r = _fire_tier("t2", PARTIAL_EXIT_TIER2_FRACTION,
                                       PARTIAL_EXIT_TIER2_TRIGGER,
                                       move_to_be=False)  # stop already at breakeven
                        if r:
                            results.append(r)
                            fired_any = True
                    except Exception as e:
                        print(f"[AEGIS] Tier 2 exit failed for {ticker}: {e}")

                if fired_any:
                    continue  # remaining qty gets trailing stop next AEGIS run
            # ── End partial exit ──────────────────────────────────────────────

            # Handle BOTH longs and shorts.
            # For a SHORT position that's profitable (price has gone DOWN),
            # the trailing stop is a BUY order (to cover) that trails DOWN
            # behind the price. Alpaca's TrailingStopOrderRequest figures
            # out the direction from the order side + trail_percent; we just
            # need to pass the right side (BUY to cover a short).
            trail_side = OrderSide.SELL if is_long else OrderSide.BUY

            # Not profitable enough yet for trailing stop
            if pct_gain < trigger * 100:
                continue

            target_trail = _target_trail(pct_gain_decimal)

            # Check for existing trailing stop
            existing_trail = next(
                (o for o in ticker_orders
                 if "trailing" in str(getattr(o, "type", "")).lower()),
                None
            )
            if existing_trail:
                # Already trailing — check if we need to tighten.
                existing_pct = float(getattr(existing_trail, "trail_percent", 0) or 0)
                # If Alpaca returned trail_percent=0 (can happen when order was placed
                # with trail_price dollar amount), treat it as the widest possible trail
                # so we always tighten to the correct level.
                if existing_pct == 0:
                    effective_pct = 100.0   # assume worst-case, force tighten
                    print(f"[AEGIS] {ticker} trail_percent=0 from Alpaca (dollar trail?) — forcing tighten to {target_trail*100:.1f}%")
                else:
                    effective_pct = existing_pct
                if target_trail >= effective_pct / 100:
                    # Already at or tighter than target — no action
                    print(f"[AEGIS] {ticker} trailing at {effective_pct:.1f}% · target {target_trail*100:.1f}% — no tighten needed")
                    continue
                # Tighten: cancel old trailing stop, place tighter one
                try:
                    client.cancel_order_by_id(str(existing_trail.id))
                    print(f"[AEGIS] {ticker} tightening trail: {effective_pct:.1f}% → {target_trail*100:.1f}% (gain {pct_gain:.1f}%)")
                except Exception as _te:
                    # M-2 fix: was silent `continue`. If the same ticker fails
                    # to tighten over and over, we'd never see why.
                    print(f"[AEGIS] {ticker} trail-tighten cancel failed: {_te} — leaving existing trail in place")
                    continue  # if cancel fails, skip to avoid duplicates
            else:
                # No trailing stop — only activate if gain >= trigger
                if pct_gain < trigger * 100:
                    continue

            # Process each position in its own try/except so one failure
            # (e.g. fractional share DAY-only restriction) doesn't kill the loop
            try:
                # Cancel existing fixed stop-loss order(s).
                # Bracket child stops (HELD status) often can't be cancelled directly —
                # Alpaca requires cancelling the parent bracket order instead.
                # Strategy: try direct cancel first; on failure try parent; on failure
                # cancel ALL orders for this ticker (nuclear option — reissue TP after).
                cancelled = 0
                _tp_price_to_reissue = None  # track TP price in case we cancel the whole bracket
                _cancelled_parent    = False  # True when we cancelled a bracket parent order
                for o in ticker_orders:
                    otype = str(getattr(o, "type", "")).lower()
                    if otype == "limit" and getattr(o, "parent_order_id", None):
                        # Capture bracket TP price so we can reissue it if the parent gets cancelled
                        if o.limit_price and not _tp_price_to_reissue:
                            _tp_price_to_reissue = float(o.limit_price)
                    if "stop" in otype and "limit" not in otype:
                        try:
                            client.cancel_order_by_id(str(o.id))
                            cancelled += 1
                            print(f"[AEGIS] {ticker} cancelled stop {str(o.id)[:8]} directly")
                        except Exception as _ce1:
                            # Direct cancel failed — try cancelling the parent bracket order
                            parent_id = getattr(o, "parent_order_id", None)
                            if parent_id:
                                try:
                                    client.cancel_order_by_id(str(parent_id))
                                    cancelled += 1
                                    _cancelled_parent = True
                                    print(f"[AEGIS] {ticker} cancelled parent bracket {str(parent_id)[:8]} (child was HELD)")
                                except Exception as _ce2:
                                    print(f"[AEGIS] {ticker} could not cancel stop or parent: {_ce1} / {_ce2}")
                            else:
                                print(f"[AEGIS] {ticker} could not cancel SL (no parent): {_ce1}")

                # Race-condition fix: Alpaca's held_for_orders count doesn't update
                # instantly after a bracket parent cancel. Without a pause, the trailing
                # stop placement hits "insufficient qty available (available: 0)" even
                # though the cancel succeeded. Sleep here so the position is actually
                # free before we try to place a new order on it.
                if _cancelled_parent:
                    import time as _time_mod
                    _time_mod.sleep(1.5)
                    print(f"[AEGIS] {ticker} waited 1.5s for bracket cancel to propagate")

                # Place native Alpaca trailing stop — try GTC first, DAY fallback for fractionals.
                # If the first attempt fails with "insufficient qty / held_for_orders" (race
                # condition where Alpaca hasn't released the shares yet), wait another 2s and retry.
                trail_pct_val = target_trail * 100   # Alpaca wants e.g. 3.0 for 3%
                if not (0.5 <= trail_pct_val <= 20):
                    raise ValueError(f"trail_percent {trail_pct_val!r} out of sane range [0.5, 20]")
                order = None
                _trail_attempts = 0
                while _trail_attempts < 2 and order is None:
                    _trail_attempts += 1
                    for tif in [TimeInForce.GTC, TimeInForce.DAY]:
                        try:
                            trail_req = TrailingStopOrderRequest(
                                symbol        = ticker,
                                qty           = qty,
                                side          = trail_side,   # SELL for longs, BUY to cover shorts
                                time_in_force = tif,
                                trail_percent = trail_pct_val,
                            )
                            order = client.submit_order(trail_req)
                            break  # success
                        except Exception as oe:
                            if "fractional" in str(oe).lower() and tif == TimeInForce.GTC:
                                print(f"[AEGIS] {ticker} is fractional — retrying with DAY order")
                                continue
                            # On held_for_orders / insufficient qty, wait and retry once
                            _err_str = str(oe).lower()
                            if "insufficient" in _err_str or "held_for_orders" in _err_str or "40310000" in str(oe):
                                if _trail_attempts == 1:
                                    # first attempt — sleep and let outer while retry
                                    import time as _time_mod2
                                    print(f"[AEGIS] {ticker} trailing stop hit held_for_orders — waiting 2s (attempt {_trail_attempts})")
                                    _time_mod2.sleep(2.0)
                                else:
                                    print(f"[AEGIS] {ticker} trailing stop held_for_orders on attempt {_trail_attempts} — giving up, will alert")
                                break  # always break on held_for_orders, NEVER raise — lets "if order is None" fire
                            raise  # only re-raise for non-held_for_orders errors

                if order is None:
                    print(f"[AEGIS] {ticker}: could not place trailing stop — skipping")
                    continue

                # If we cancelled a bracket parent, the TP child was also cancelled.
                # Reissue the TP as a standalone limit order at the captured price.
                if _cancelled_parent and _tp_price_to_reissue:
                    try:
                        from alpaca.trading.requests import LimitOrderRequest as _LOR
                        _tp_side = OrderSide.SELL if is_long else OrderSide.BUY
                        _tp_req = _LOR(
                            symbol=ticker, qty=qty, side=_tp_side,
                            limit_price=_tp_price_to_reissue,
                            time_in_force=TimeInForce.GTC,
                        )
                        client.submit_order(_tp_req)
                        print(f"[AEGIS] {ticker} reissued TP @ ${_tp_price_to_reissue:.2f} (bracket parent was cancelled)")
                    except Exception as _tpe:
                        print(f"[AEGIS] {ticker} could not reissue TP after bracket cancel: {_tpe}")

                mode = "LIVE" if is_live_mode() else "PAPER"
                print(f"[AEGIS] [{mode}] {ticker} up {pct_gain:.1f}% → "
                      f"trailing stop {trail*100:.0f}% activated "
                      f"(cancelled {cancelled} fixed SL)")

                result = {
                    "ticker":       ticker,
                    "pct_gain":     round(pct_gain, 2),
                    "trail_pct":    trail,
                    "order_id":     str(order.id),
                    "cancelled_sl": cancelled,
                    "status":       "trailing",
                    "timestamp":    datetime.now().isoformat(),
                }
                results.append(result)

                # Instant Slack ping
                try:
                    from alerts.slack import _post
                    _post({"text": (
                        f"🔒 *Trailing stop activated — {ticker}*\n"
                        f">Up *{pct_gain:.1f}%* · fixed SL replaced with "
                        f"*{trail*100:.0f}% trailing stop* below peak\n"
                        f">Take-profit target unchanged"
                    )})
                except Exception:
                    pass

            except Exception as e:
                # Alpaca won't allow trailing stops on fractional positions.
                # Simulate trailing: move the fixed stop to lock in profit.
                # For LONG  → stop is BELOW current, must be ABOVE entry
                # For SHORT → stop is ABOVE current, must be BELOW entry
                if "fractional" in str(e).lower():
                    _simulated = False
                    try:
                        current_price = p.get("current_price") or p.get("avg_entry_price", 0)
                        entry_px  = p.get("avg_entry_price", 0)
                        if is_long:
                            new_stop = round(current_price * (1 - trail), 2)
                            locks_profit = new_stop > entry_px
                        else:
                            new_stop = round(current_price * (1 + trail), 2)
                            locks_profit = new_stop < entry_px
                        if locks_profit:
                            # Cancel old stops
                            for o in ticker_orders:
                                otype = str(getattr(o, "type", "")).lower()
                                if "stop" in otype and "limit" not in otype and "trailing" not in otype:
                                    try:
                                        client.cancel_order_by_id(str(o.id))
                                    except Exception:
                                        pass
                            # Place new fixed stop that locks in profit
                            from alpaca.trading.requests import StopOrderRequest
                            stop_req = StopOrderRequest(
                                symbol        = ticker,
                                qty           = qty,
                                side          = trail_side,   # SELL for longs, BUY for shorts
                                time_in_force = TimeInForce.GTC,
                                stop_price    = new_stop,
                            )
                            try:
                                sord = client.submit_order(stop_req)
                            except Exception:
                                stop_req.time_in_force = TimeInForce.DAY
                                sord = client.submit_order(stop_req)
                            print(f"[AEGIS] {ticker} fractional — simulated trail: "
                                  f"stop moved to ${new_stop:.2f} (locks in profit)")
                            _simulated = True
                            results.append({
                                "ticker":       ticker,
                                "pct_gain":     round(pct_gain, 2),
                                "trail_pct":    trail,
                                "order_id":     str(sord.id),
                                "cancelled_sl": 0,
                                "status":       "simulated_trailing",
                                "timestamp":    datetime.now().isoformat(),
                            })
                    except Exception as se:
                        print(f"[AEGIS] {ticker} simulated trail failed: {se}")
                    if not _simulated:
                        print(f"[AEGIS] {ticker}: fractional, stop already above entry — no move needed")
                else:
                    print(f"[AEGIS] {ticker} trailing stop error: {e} — skipping")
                    try:
                        from alerts.slack import _post
                        _post({"text": (
                            f"⚠️ *AEGIS trailing stop FAILED — {ticker}*\n"
                            f">Position up *{pct_gain:.1f}%* but trailing stop could not be placed\n"
                            f">Error: `{str(e)[:200]}`\n"
                            f">*Action: check Alpaca dashboard — may need manual trailing stop*"
                        )})
                    except Exception:
                        pass

    except Exception as e:
        print(f"[AEGIS] trail_positions error: {e}")

    return results


def safety_sweep() -> list:
    """AEGIS BACKSTOP (2026-06-03) — runs AFTER trail_positions(), independent of
    the partial-exit/trail internals (which have had handoff bugs that left INTC
    and IMSR un-trailed / briefly naked). Two guarantees, every run:

      1. NO naked position — any position with no active stop gets a protective stop.
      2. Any position at/above TRAIL_TRIGGER_PCT has a TRAILING stop — upgrades a
         lingering fixed stop the main pass failed to convert.

    Idempotent: a position already correctly protected (trailing when it should
    trail, fixed when below the trigger) is skipped — no order churn. Upgrades
    cancel the old stop, place the trailing, and VERIFY; if the trailing fails
    (e.g. TP still holds the qty), a protective fixed stop is re-placed so the
    position is NEVER left naked. If even that fails, it Slack-alerts loudly.
    """
    from execution.alpaca import _get_client, get_positions, is_active_order
    from alpaca.trading.requests import (GetOrdersRequest, TrailingStopOrderRequest,
                                         StopOrderRequest)
    from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce
    from config import TRAIL_TRIGGER_PCT, TRAIL_PCT, KELLY_LOSS_PCT

    def _ot(o):
        return str(getattr(o, "order_type", "") or getattr(o, "type", "")).lower()

    actions = []
    try:
        client = _get_client()
        positions = get_positions() or []
        if not positions:
            print("[SWEEP] No open positions.")
            return actions

        stops_by_sym = {}
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)):
            if is_active_order(o) and ("stop" in _ot(o) or "trail" in _ot(o)):
                stops_by_sym.setdefault(o.symbol, []).append(o)

        trigger_pct = TRAIL_TRIGGER_PCT * 100   # config stores a decimal
        for p in positions:
            sym = p["ticker"]
            qty = abs(float(p.get("qty") or 0))
            if qty <= 0:
                continue
            is_long = "short" not in str(p.get("side", "")).lower()
            entry = float(p.get("avg_entry_price") or 0)
            cur   = float(p.get("current_price") or 0)
            pct   = ((cur - entry) / entry * 100 * (1 if is_long else -1)) if entry else 0.0
            close_side = OrderSide.SELL if is_long else OrderSide.BUY

            sym_stops    = stops_by_sym.get(sym, [])
            has_trailing = any("trail" in _ot(o) for o in sym_stops)
            has_fixed    = any("stop" in _ot(o) and "trail" not in _ot(o) for o in sym_stops)

            def _place_fixed_fallback():
                sp = round(cur * (1 - KELLY_LOSS_PCT), 2) if is_long else round(cur * (1 + KELLY_LOSS_PCT), 2)
                client.submit_order(StopOrderRequest(
                    symbol=sym, qty=qty, side=close_side,
                    time_in_force=TimeInForce.GTC, stop_price=sp))
                return sp

            def _alert_naked(err):
                print(f"[SWEEP] 🚨 {sym} could not be protected: {err}")
                try:
                    from alerts.slack import _post
                    _post({"text": f"🚨 *AEGIS sweep* — {sym} is NAKED and could not be "
                                   f"auto-protected ({str(err)[:60]}). CHECK MANUALLY NOW."})
                except Exception:
                    pass
                actions.append({"ticker": sym, "action": "sweep_naked_ALERT"})

            # (1) Already trailing — correct, skip (idempotent, no churn).
            if has_trailing:
                continue

            # (2) Should be trailing but isn't — upgrade safely.
            if pct >= trigger_pct:
                for o in sym_stops:               # free the held qty first
                    try: client.cancel_order_by_id(str(o.id))
                    except Exception: pass
                try:
                    client.submit_order(TrailingStopOrderRequest(
                        symbol=sym, qty=qty, side=close_side,
                        time_in_force=TimeInForce.GTC, trail_percent=round(TRAIL_PCT * 100, 2)))
                    print(f"[SWEEP] {sym} +{pct:.1f}% → placed trailing stop {TRAIL_PCT*100:.0f}% (was un-trailed)")
                    actions.append({"ticker": sym, "action": "sweep_trailing", "pct": round(pct, 1)})
                except Exception as e:
                    print(f"[SWEEP] {sym} trailing placement failed ({str(e)[:60]}) — fixed-stop fallback")
                    try:
                        sp = _place_fixed_fallback()
                        print(f"[SWEEP] {sym} fallback fixed stop @ ${sp}")
                        actions.append({"ticker": sym, "action": "sweep_fallback_fixed", "stop": sp})
                    except Exception as e2:
                        _alert_naked(e2)
                continue

            # (3) Below trigger and NAKED — place a protective fixed stop.
            if not has_fixed:
                try:
                    sp = _place_fixed_fallback()
                    print(f"[SWEEP] {sym} was NAKED ({pct:+.1f}%) → placed fixed stop @ ${sp}")
                    actions.append({"ticker": sym, "action": "sweep_naked_fixed", "stop": sp})
                except Exception as e:
                    _alert_naked(e)

    except Exception as e:
        print(f"[SWEEP] safety_sweep error: {e}")

    print(f"[SWEEP] Safety sweep complete — {len(actions)} action(s)."
          if actions else "[SWEEP] All positions correctly protected — no action needed.")
    return actions
