"""
AEGIS — Trailing Stop & Partial Exit Manager
Runs every 30 minutes during market hours via GitHub Actions.

Two things happen in every run:

1. PARTIAL EXIT (scale-out) — two tiers per position:
   - T1 at +7%:  closes 20% at market, moves stop to breakeven
   - T2 at +12%: closes another 20%, remaining 60% rides the trailing stop
   - Logs to Supabase so it never fires twice
   - Next run applies trailing stop to the remaining half

2. TRAILING STOP — fires when position is up ≥ 3%:
   - Cancels fixed bracket SL, places Alpaca native trailing stop
   - Tightens as gains grow: +3%→3% trail, +7%→2%, +10%→1.5%, +15%→1%
   - Fractional positions use simulated trailing (fixed stop moved up)

Toggle ENABLE_PARTIAL_EXIT = False in config.py to revert to
single-exit mode instantly.
"""
import os
import sys

# Anchor to this file's directory — prevents false errors when invoked from
# a parent directory (e.g. cd AI-trading && python market-predictions/trail_stops.py)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from execution.alpaca import trail_positions, is_configured

if not is_configured():
    print("Alpaca not configured — skipping AEGIS check.")
    sys.exit(0)

print("[AEGIS] Running partial exit + trailing stop check...")

# M-5 fix: BEFORE trailing logic, sweep for bracket exits that fired since the
# last AEGIS cycle and write closed-row to Supabase. Without this, partial-exit
# history lookups (get_partial_exit_history) couldn't tell if a stale T1/T2
# record applied to a NEW re-entry of the same ticker — only the open_tickers
# filter covered it, leaving a same-cycle re-entry edge case.
try:
    from execution.alpaca import _get_client, get_positions, is_configured as _aok, order_status
    from alpaca.trading.requests import GetOrdersRequest as _GOR
    from alpaca.trading.enums import QueryOrderStatus as _QOS
    import db as _db
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    if _aok() and _db.db_available():
        _c = _get_client()
        _cutoff = (_dt.now(_tz.utc) - _td(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _recent_fills = _c.get_orders(_GOR(status=_QOS.ALL, after=_cutoff, limit=200))
        _open_tickers = {p["ticker"] for p in (get_positions() or [])}

        # An order that filled in the last 20 min AND its ticker is no longer
        # in open positions = closed via bracket child (TP/SL/trailing).
        for o in _recent_fills:
            if order_status(o) != "filled":
                continue
            sym = o.symbol
            if sym in _open_tickers:
                continue
            otype = str(getattr(o, "type", "")).lower()
            if "limit" in otype:
                _status = "tp_hit"
            elif "trailing" in otype:
                _status = "trail_hit"
            elif "stop" in otype:
                _status = "sl_hit"
            else:
                continue   # not a bracket child
            try:
                _db.save_trade({
                    "order_id": f"bracket-{_status}-{sym}-{str(o.id)[:8]}",
                    "ticker":   sym,
                    "side":     "close",
                    "dollar_amount": float(getattr(o, "filled_avg_price", 0) or 0) * float(getattr(o, "filled_qty", 0) or 0),
                    "mode":     "LIVE" if _aok() else "PAPER",
                    "status":   _status,
                    "reason":   f"Bracket {_status.replace('_hit', '').upper()} fill detected by AEGIS sweep",
                    "timestamp": _dt.now(_tz.utc).isoformat(),
                })
                print(f"[AEGIS] Logged {_status} for {sym}")
            except Exception as _bf:
                print(f"[AEGIS] Could not log bracket fill for {sym}: {_bf}")
except Exception as _be:
    print(f"[AEGIS] Bracket-fill sweep error: {_be}")

results = trail_positions()

# BACKSTOP (2026-06-03): after the main pass, guarantee every position is
# protected — no naked positions, and anything over +3% is trailing. Runs
# regardless of what trail_positions did (it has had handoff bugs). Safe +
# idempotent: skips already-correct positions.
try:
    from execution.alpaca import safety_sweep
    safety_sweep()
except Exception as _se:
    print(f"[SWEEP] safety_sweep failed to run: {_se}")

if not results:
    print("[AEGIS] Nothing to act on — all positions below trigger thresholds.")
else:
    # Partial exits use action names "partial_exit_t1" / "partial_exit_t2".
    # Match by prefix so they don't leak into trail_upgrades (which would crash
    # on the missing 'trail_pct' key).
    partial_exits  = [r for r in results if str(r.get("action", "")).startswith("partial_exit")]
    trail_upgrades = [r for r in results if not str(r.get("action", "")).startswith("partial_exit")]

    if partial_exits:
        print(f"\n✂️  {len(partial_exits)} partial exit(s) fired:")
        for r in partial_exits:
            print(f"  ✂️  {r['ticker']:6s}  +{r['pct_gain']:.1f}%  →  "
                  f"closed {r['qty_closed']} shares  "
                  f"locked ${r['pnl_locked']:+.2f}  "
                  f"remaining {r['qty_remaining']} @ breakeven  "
                  f"[{r['order_id'][:8]}...]")

    if trail_upgrades:
        print(f"\n🔒  {len(trail_upgrades)} trailing stop upgrade(s):")
        for r in trail_upgrades:
            mode = "simulated" if r.get("status") == "simulated_trailing" else "native"
            print(f"  🔒  {r['ticker']:6s}  +{r['pct_gain']:.1f}%  →  "
                  f"trail {r['trail_pct']*100:.0f}% [{mode}]  "
                  f"[{r['order_id'][:8]}...]")

print("\n[AEGIS] Done.")
