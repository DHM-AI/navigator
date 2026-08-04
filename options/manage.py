from __future__ import annotations
"""Illuminati options — active exit management (PAPER-ONLY).

A long option with no exit rots to zero (theta). This module walks the current
PAPER option positions and decides, per position, whether to CLOSE (take-profit,
stop, or time-exit) or HOLD. Closing routes through
`execution.close_option_position` (sell_to_close, paper endpoint, hard live gate).

Decision rules (per position), in priority order:
  1. pnl_pct >= OPT_TP_PCT          -> close, reason "tp"
  2. pnl_pct <= -OPT_STOP_PCT       -> close, reason "stop"
  3. dte is not None and dte <= OPT_EXIT_DTE -> close, reason "time_exit"
  4. otherwise                       -> hold, reason "hold"

where
  premium_basis = abs(avg_entry_price * qty * 100)   # contract multiplier 100
  pnl_pct       = unrealized_pl / premium_basis       # guarded against div-by-zero
  dte           = calendar days from today to the OCC expiration

A single bad position NEVER crashes the sweep — each is wrapped in try/except and
recorded as an error/hold row. Paper-only end to end (relies on the execution gates).

Built 2026-06-08. Isolated from the equity engine.
"""

import re
from datetime import date, datetime

# Exit-management config. Support both package and flat import; fall back to safe
# defaults only if the constants are somehow absent (keeps the sweep operable).
try:
    from options.config import OPT_TP_PCT, OPT_STOP_PCT, OPT_EXIT_DTE
except Exception:  # pragma: no cover - fallback when run inside the options/ dir
    try:
        from config import OPT_TP_PCT, OPT_STOP_PCT, OPT_EXIT_DTE  # type: ignore
    except Exception:  # pragma: no cover - last-resort defaults
        OPT_TP_PCT = 0.50
        OPT_STOP_PCT = 0.50
        OPT_EXIT_DTE = 21

# Execution layer (positions read + close path). Support package + flat import.
try:
    from options import execution
except Exception:  # pragma: no cover - fallback when run inside the options/ dir
    import execution  # type: ignore

# Shares per option contract (matches OPT_CONTRACT_MULTIPLIER = 100).
_CONTRACT_MULTIPLIER = 100

# OCC option symbol: ROOT + YYMMDD + (C|P) + 8-digit strike.
# Root is 1-6 alphanumerics; we anchor off the 6-digit date + C/P + 8-digit strike
# at the END so a variable-length root never throws off the parse.
_OCC_RE = re.compile(r"^(?P<root>.+?)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_occ_expiration(occ_symbol: str) -> str | None:
    """Extract the expiration date from an OCC option symbol -> 'YYYY-MM-DD'.

    OCC format: ROOT + YYMMDD + (C|P) + strike(8 digits), e.g. 'AAPL260619C00150000'
    -> '2026-06-19'. Robust to root length (matches the fixed-width tail). Returns
    None on any malformed input rather than raising.
    """
    if not occ_symbol or not isinstance(occ_symbol, str):
        return None
    m = _OCC_RE.match(occ_symbol.strip())
    if not m:
        return None
    try:
        yy = int(m.group("yy"))
        mm = int(m.group("mm"))
        dd = int(m.group("dd"))
        # OCC years are 2-digit; pivot to 2000s (options run far past 2000).
        year = 2000 + yy
        return date(year, mm, dd).isoformat()
    except (ValueError, TypeError):
        return None


def _compute_dte(expiration_iso: str | None, today: date | None = None) -> int | None:
    """Calendar days from today to the expiration date. None if unknown."""
    if not expiration_iso:
        return None
    try:
        exp = datetime.strptime(expiration_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    ref = today or date.today()
    return (exp - ref).days


def manage_options(dry_run: bool = True) -> list[dict]:
    """Sweep current PAPER option positions and close/hold each per the exit rules.

    Returns one row per position:
      {"symbol", "action": "close"|"hold", "reason", "pnl_pct", "dte",
       "result"(present only when a close was attempted)}

    Never raises on a single bad position — it is recorded as a hold/error row and
    the sweep continues. Closing uses execution.close_option_position (paper-only).
    """
    try:
        positions = execution.get_option_positions()
    except Exception as exc:
        # Could not even read positions — return a single diagnostic row, never raise.
        return [{
            "symbol": None,
            "action": "hold",
            "reason": f"positions_read_failed: {exc}",
            "pnl_pct": None,
            "dte": None,
        }]

    if not positions:
        return []

    today = date.today()
    out: list[dict] = []

    for pos in positions:
        # Per-position guard: one bad position must never crash the sweep.
        symbol = None
        try:
            if not isinstance(pos, dict):
                out.append({
                    "symbol": None,
                    "action": "hold",
                    "reason": "bad_position_shape",
                    "pnl_pct": None,
                    "dte": None,
                })
                continue

            symbol = pos.get("symbol")

            # Numeric fields — coerce defensively.
            try:
                avg_entry_price = float(pos.get("avg_entry_price") or 0.0)
            except (TypeError, ValueError):
                avg_entry_price = 0.0
            try:
                qty = float(pos.get("qty") or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                unrealized_pl = float(pos.get("unrealized_pl") or 0.0)
            except (TypeError, ValueError):
                unrealized_pl = 0.0

            # Premium basis (cost of the position). Guard div-by-zero.
            premium_basis = abs(avg_entry_price * qty * _CONTRACT_MULTIPLIER)
            pnl_pct = (unrealized_pl / premium_basis) if premium_basis > 0 else 0.0

            # Days to expiration from the OCC symbol.
            expiration = parse_occ_expiration(symbol or "")
            dte = _compute_dte(expiration, today)

            # Decide (priority: TP -> stop -> time-exit -> hold).
            action = "hold"
            reason = "hold"
            if pnl_pct >= OPT_TP_PCT:
                action, reason = "close", "tp"
            elif pnl_pct <= -OPT_STOP_PCT:
                action, reason = "close", "stop"
            elif dte is not None and dte <= OPT_EXIT_DTE:
                action, reason = "close", "time_exit"

            row: dict = {
                "symbol": symbol,
                "action": action,
                "reason": reason,
                "pnl_pct": pnl_pct,
                "dte": dte,
            }

            if action == "close":
                close_qty = abs(int(qty))
                try:
                    row["result"] = execution.close_option_position(
                        symbol, close_qty, dry_run=dry_run
                    )
                except Exception as exc:
                    # Closing failed — record it, do NOT raise, position stays as-is.
                    row["result"] = {"status": "error", "reason": f"close_failed: {exc}"}

            out.append(row)

        except Exception as exc:
            # Catch-all per position — never abort the whole sweep.
            out.append({
                "symbol": symbol,
                "action": "hold",
                "reason": f"position_error: {exc}",
                "pnl_pct": None,
                "dte": None,
            })

    return out
