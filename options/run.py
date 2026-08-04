"""Illuminati — Options orchestrator (paper-only, gated). The scheduler entry point.

This is the single command the launchd trigger calls. It runs in two ordered
phases on every tick:

  1. EXITS first  — manage_options() inspects each open paper option position and
     closes anything that hit take-profit, stop, or the DTE time-exit. Exits run
     before entries so freed-up OPT_MAX_OPEN slots are available this same tick.
  2. ENTRIES next — run_options_scan() turns the most-recent high-conviction
     EQUITY predictions (read-only) into long-option paper plans/orders.

Signal source: this module NEVER runs model inference. It only READS what the
equity scan already wrote to the Supabase `predictions` table via the shared
`db.py` helpers. Picks are filtered to OPT_MIN_SCORE and OPT_ALLOWED_UNDERLYINGS.

Hard guarantees:
  - Paper-only. Every order path runs through execution.place_option_order /
    execution.close_option_position, which carry the OPT_ENABLE_LIVE hard gate.
  - Never crashes. get_option_picks() returns [] on any error; run() degrades
    gracefully and always returns a well-formed dict.

Built 2026-06-08. Reads the equity layer; never writes to it.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Package-relative imports keep the options subsystem self-contained, with a flat
# fallback so the module also works when executed from inside the options/ dir.
try:
    from .manage import manage_options
    from .strategy import run_options_scan
    from . import config as opt_config
except ImportError:  # pragma: no cover - fallback for flat execution
    from manage import manage_options            # type: ignore
    from strategy import run_options_scan        # type: ignore
    import config as opt_config                   # type: ignore


def _latest_predictions() -> list[dict]:
    """Return the most-recent batch of equity prediction rows (READ-ONLY).

    Strategy: ask for today's rows first (load_predictions_for_date). If today
    has none yet (e.g. the equity scan hasn't run, or it's a holiday), fall back
    to load_predictions() and keep only the rows from the single newest date
    present. We never mix multiple days of signals into one option scan.

    The equity engine's db.py lives one level up (market-predictions/db.py). We
    import it lazily so importing options.run never requires Supabase creds.
    Any failure returns [] — picks must never crash the orchestrator.
    """
    try:
        import db as equity_db  # market-predictions/db.py (equity engine)
    except Exception:
        try:
            from .. import db as equity_db  # type: ignore
        except Exception:
            return []

    # Guard: if Supabase isn't configured, don't even attempt a query.
    try:
        if hasattr(equity_db, "db_available") and not equity_db.db_available():
            return []
    except Exception:
        return []

    # 1) Try today's predictions directly.
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = equity_db.load_predictions_for_date(today)
        if rows:
            return rows
    except Exception:
        pass

    # 2) Fall back to the latest batch overall, sliced to the newest date.
    try:
        rows = equity_db.load_predictions(limit=500) or []
    except Exception:
        return []
    if not rows:
        return []

    # load_predictions() is ordered date DESC, so the first row's date is newest.
    newest = None
    for r in rows:
        d = r.get("date") if isinstance(r, dict) else None
        if d:
            newest = str(d)
            break
    if newest is None:
        return rows
    return [r for r in rows if isinstance(r, dict) and str(r.get("date")) == newest]


def get_option_picks() -> list[dict]:
    """Read the most-recent EQUITY predictions and map them to option signals.

    Filters:
      - score >= config.OPT_MIN_SCORE        (only high-conviction signals)
      - ticker in config.OPT_ALLOWED_UNDERLYINGS  (liquid option universe)

    Returns a list of {"ticker","direction","score"} dicts. `direction` comes
    from the prediction's direction/signal field; it defaults to "bullish" when
    a row clears the score bar but carries no usable direction word. On ANY error
    returns [] — this function never raises and never runs model inference.
    """
    try:
        min_score = float(getattr(opt_config, "OPT_MIN_SCORE", 80) or 0)
    except (TypeError, ValueError):
        min_score = 80.0

    allowed_raw = getattr(opt_config, "OPT_ALLOWED_UNDERLYINGS", []) or []
    allowed = {str(t).upper() for t in allowed_raw}

    try:
        rows = _latest_predictions()
    except Exception:
        return []
    if not rows:
        return []

    picks: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ticker = str(r.get("ticker") or "").upper().strip()
            if not ticker or (allowed and ticker not in allowed):
                continue

            try:
                score = float(r.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if score < min_score:
                continue

            # Pull a direction word from whichever field carries it.
            direction = (
                r.get("direction")
                or r.get("signal")
                or r.get("side")
                or ""
            )
            direction = str(direction).strip().lower()
            if direction in ("", "none", "nan", "mixed"):
                # Cleared the conviction bar but no clear direction recorded:
                # default to a bullish (call) signal per spec.
                direction = "bullish"

            picks.append({
                "ticker": ticker,
                "direction": direction,
                "score": score,
            })
        except Exception:
            # One malformed row must never kill the whole pick list.
            continue

    return picks


def run(dry_run: bool = False) -> dict:
    """Orchestrate one options cycle: EXITS first, then ENTRIES. Paper-only.

    Order matters: manage_options() runs before run_options_scan() so any slot
    freed by a close this tick is immediately available for a new entry under the
    OPT_MAX_OPEN cap.

    Args:
        dry_run: when True, neither phase submits anything (manage builds close
            previews; scan builds entry plans). When False, both phases route to
            the Alpaca PAPER endpoint via the execution layer's hard gates —
            options can NEVER trade live regardless of this flag.

    Returns:
        {"managed": [...], "entered": [...], "ts": "<iso utc>"}
        Always well-formed; on any internal failure the offending phase yields []
        rather than raising.
    """
    ts = datetime.now(timezone.utc).isoformat()

    # 1) EXITS first — manage open positions (TP / stop / time-exit).
    try:
        managed = manage_options(dry_run=dry_run)
    except Exception as exc:
        managed = [{"status": "error", "reason": f"manage_options failed: {exc}"}]

    # 2) ENTRIES next — scan equity signals into option plans/orders.
    try:
        picks = get_option_picks()
    except Exception:
        picks = []
    try:
        entered = run_options_scan(picks, dry_run=dry_run)
    except Exception as exc:
        entered = [{"status": "error", "reason": f"run_options_scan failed: {exc}"}]

    return {"managed": managed, "entered": entered, "ts": ts}


if __name__ == "__main__":  # pragma: no cover - manual / scheduler entry point
    import json

    result = run(dry_run=False)
    print(json.dumps(result, indent=2, default=str))
