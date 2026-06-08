"""Illuminati — Options strategy orchestration (paper-only, gated).

run_options_scan() turns high-conviction equity model picks into long-option
(call/put) PAPER plans. It is the glue between the equity signal layer and the
options data/risk/execution modules. dry_run=True (the default) NEVER submits an
order — it only builds and returns validated plans.

Hard guarantees:
  - Long-option only (defined risk: a bought call or put).
  - Respects can_open_option() gating (allowed underlying, min score, max open).
  - Respects size_option_qty() premium cap.
  - place_option_order() carries the live gate; this layer never bypasses it.

Built 2026-06-08. Lives apart from the equity engine — never modifies it.
"""

from __future__ import annotations

# Package-relative imports keep the options subsystem self-contained. Each
# dependency falls back to a flat import so the module also works if executed
# from inside the options/ directory.
try:
    from .data import select_contract
    from .risk import can_open_option, size_option_qty
    from .execution import place_option_order, get_option_positions
    from . import config as opt_config
except ImportError:  # pragma: no cover - fallback for flat execution
    from data import select_contract            # type: ignore
    from risk import can_open_option, size_option_qty  # type: ignore
    from execution import place_option_order, get_option_positions  # type: ignore
    import config as opt_config                  # type: ignore


def _get_bankroll() -> float:
    """Resolve current bankroll from the equity execution layer.

    Falls back to config.BANKROLL, then a conservative default, and never
    raises — a scan must always be able to size something.
    """
    try:
        from execution.alpaca import get_bankroll  # equity engine helper
        bankroll = get_bankroll()
        if bankroll and bankroll > 0:
            return float(bankroll)
    except Exception:
        pass
    try:
        import config as equity_config  # top-level equity config
        bankroll = getattr(equity_config, "BANKROLL", None)
        if bankroll and bankroll > 0:
            return float(bankroll)
    except Exception:
        pass
    return 100000.0


def _direction_to_word(direction: str) -> str:
    """Normalise an equity pick direction to the option intent word.

    Bullish-ish -> 'bullish' (a call). Bearish-ish -> 'bearish' (a put).
    Anything unrecognised defaults to 'bullish' so select_contract still works,
    but callers should pass the canonical words.
    """
    d = (direction or "").strip().lower()
    if d in ("bearish", "short", "sell", "put", "down"):
        return "bearish"
    return "bullish"


def run_options_scan(picks, dry_run=True):
    """Convert equity model picks into long-option PAPER plans.

    Args:
        picks: list of dicts shaped {"ticker","direction","score"} coming from
            the equity model. direction in {bullish/long/buy} -> call,
            {bearish/short/sell} -> put. score is the equity conviction (0-100).
        dry_run: when True (default) NOTHING is submitted; plans are built and
            validated only. False routes to a PAPER limit order (still gated by
            execution.place_option_order; live trading is impossible here).

    Returns:
        list of dicts, one per pick, shaped:
        {"ticker","contract","side","qty","mid","status","reason"}
        - status values: "skipped" (gating/selection/sizing failed) or whatever
          place_option_order returns ("dry_run","submitted","skipped_zero_qty",
          "disabled","refused_live", ...).
        - reason carries a human-readable explanation for skips / errors.
    """
    results = []

    if not picks:
        return results

    # Entry-window guard: carry over the equity entry lessons (no open-chop
    # entries, no end-of-day entries, no Friday-PM entries). Active only when
    # OPT_RESPECT_EQUITY_WINDOW is True. The import + clock are wrapped so that
    # if the import itself errors we fail OPEN to the existing per-pick behavior
    # (never silently swallow a window-block — only a missing dependency).
    if getattr(opt_config, "OPT_RESPECT_EQUITY_WINDOW", True):
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            import config as equity_config  # top-level equity config
            no_before = float(equity_config.NO_ENTRY_BEFORE_ET)
            no_after = float(equity_config.NO_ENTRY_AFTER_ET)
            block_friday_pm = bool(equity_config.BLOCK_FRIDAY_PM_ENTRIES)
            friday_cutoff = float(equity_config.FRIDAY_ENTRY_CUTOFF_ET)
        except Exception:
            # Import / config missing -> fail open: skip the guard entirely.
            no_before = no_after = friday_cutoff = None
            block_friday_pm = False

        if no_before is not None:
            now_et = datetime.now(ZoneInfo("America/New_York"))
            hour_frac = now_et.hour + now_et.minute / 60.0
            is_friday = now_et.weekday() == 4  # Mon=0 ... Fri=4

            outside_window = (
                hour_frac < no_before
                or hour_frac >= no_after
                or (block_friday_pm and is_friday and hour_frac >= friday_cutoff)
            )
            if outside_window:
                reason = "outside entry window (open chop / EOD / Friday-PM)"
                for pick in picks:
                    ticker = ""
                    if isinstance(pick, dict):
                        ticker = (pick.get("ticker") or pick.get("symbol") or "").upper()
                    results.append({
                        "ticker": ticker or None, "contract": None, "side": None,
                        "qty": 0, "mid": None, "status": "skipped", "reason": reason,
                    })
                return results

    bankroll = _get_bankroll()
    multiplier = getattr(opt_config, "OPT_CONTRACT_MULTIPLIER", 100)
    dte_target = getattr(opt_config, "OPT_TARGET_DTE", 35)
    delta_target = getattr(opt_config, "OPT_TARGET_DELTA", 0.45)

    # Seed open_count from currently-open option positions so the OPT_MAX_OPEN
    # cap accounts for live (paper) state, then increment as we plan new opens.
    # Also collect the UNDERLYINGS already held so we never try to open a 2nd
    # option in the same name: AMD is the persistent high-score signal, and
    # re-entering it every scan triggered Alpaca "potential wash trade" rejections
    # and churn (closing one strike at a loss to open another). (2026-06-08)
    import re as _re_occ
    try:
        _open_opts = get_option_positions() or []
    except Exception:
        _open_opts = []
    open_count = len(_open_opts)
    held_underlyings = {
        _re_occ.sub(r"\d{6}[CP]\d{8}$", "", str(p.get("symbol") or "")).upper()
        for p in _open_opts
    }
    held_underlyings.discard("")

    for pick in picks:
        if not isinstance(pick, dict):
            results.append({
                "ticker": None, "contract": None, "side": None, "qty": 0,
                "mid": None, "status": "skipped", "reason": "malformed pick",
            })
            continue

        ticker = (pick.get("ticker") or pick.get("symbol") or "").upper()
        direction = pick.get("direction", "bullish")
        try:
            score = float(pick.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0

        base = {
            "ticker": ticker, "contract": None, "side": None, "qty": 0,
            "mid": None, "status": "skipped", "reason": "",
        }

        # 0) One option per underlying — never open a 2nd in a name we already
        #    hold (prevents the AMD wash-trade rejections + loss-churn).
        if ticker in held_underlyings:
            base["reason"] = f"already holding an option in {ticker} (no duplicate)"
            results.append(base)
            continue

        # 1) Gate: allowed underlying, min score, max open.
        try:
            ok, reason = can_open_option(ticker, score, open_count)
        except Exception as exc:  # defensive: gating must never crash the loop
            base["reason"] = f"gating error: {exc}"
            results.append(base)
            continue

        if not ok:
            base["reason"] = reason or "gating refused"
            results.append(base)
            continue

        # 2) Select a concrete contract (expiration nearest target DTE; strike
        #    by delta if greeks available, else ATM).
        word = _direction_to_word(direction)
        try:
            contract = select_contract(
                ticker, word, dte_target=dte_target, delta_target=delta_target
            )
        except Exception as exc:
            base["reason"] = f"contract selection error: {exc}"
            results.append(base)
            continue

        if not contract:
            base["reason"] = "no suitable contract"
            results.append(base)
            continue

        contract_symbol = contract.get("symbol")
        mid = contract.get("mid")
        # Fall back to ask, then last, if mid is unavailable from the snapshot.
        if mid is None or mid <= 0:
            mid = contract.get("ask") or contract.get("last")
        try:
            mid = float(mid) if mid is not None else 0.0
        except (TypeError, ValueError):
            mid = 0.0

        base["contract"] = contract_symbol
        base["mid"] = mid if mid > 0 else None
        base["side"] = "buy"  # long-option only (buy_to_open)

        if mid <= 0:
            base["reason"] = "no tradeable price (mid/ask/last missing)"
            results.append(base)
            continue

        # Entry must be MARKETABLE or it won't fill — a buy limit @ mid sits
        # unfilled when the quote moves (observed live on paper 2026-06-08: a
        # limit @ ask filled, a limit @ mid did not). Cross to the ask so the long
        # actually opens; size on that price so the 2% premium cap reflects what's
        # really paid. Fall back to mid if the ask is missing. (Exits already use
        # market orders in manage_options for guaranteed fills.)
        try:
            _ask = float(contract.get("ask") or 0.0)
        except (TypeError, ValueError):
            _ask = 0.0
        entry_px = _ask if _ask > 0 else mid

        # 3) Size the position under the premium cap (on the price actually paid).
        try:
            qty = size_option_qty(bankroll, entry_px, multiplier)
        except Exception as exc:
            base["reason"] = f"sizing error: {exc}"
            results.append(base)
            continue

        base["qty"] = qty
        if qty <= 0:
            base["status"] = "skipped"
            base["reason"] = "premium cap allows 0 contracts"
            results.append(base)
            continue

        # 4) Place the order (or build the plan when dry_run). The execution
        #    layer enforces the paper/live gate; this layer never bypasses it.
        try:
            order = place_option_order(
                contract_symbol, qty, side="buy",
                limit_price=entry_px, dry_run=dry_run,
            )
        except Exception as exc:
            base["status"] = "error"
            base["reason"] = f"order error: {exc}"
            results.append(base)
            continue

        status = (order or {}).get("status", "unknown")
        base["status"] = status
        # Surface a useful reason for any non-success status; keep success clean.
        if status in ("dry_run", "submitted"):
            base["reason"] = ""
            # Count it against the open cap only when we actually open/plan one.
            open_count += 1
        else:
            base["reason"] = (order or {}).get("reason", status)

        results.append(base)

    return results
