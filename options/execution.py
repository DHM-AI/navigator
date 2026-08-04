from __future__ import annotations
"""Illuminati options — execution (PAPER-ONLY, HARD live gate).

Order placement + position reads for the isolated options subsystem. This module
NEVER touches the live endpoint:
  - OPT_ENABLE_PAPER must be True or every call returns {"status":"disabled"}.
  - OPT_ENABLE_LIVE is a HARD GATE. It must stay False. If it were ever flipped to
    True, place_option_order REFUSES with {"status":"refused_live"} rather than
    submitting — there is no live code path here at all.

All HTTP goes to the Alpaca PAPER trading API (https://paper-api.alpaca.markets)
using plain `requests`. Long-option only (buy_to_open). dry_run=True builds and
validates the order request WITHOUT submitting (the default, safe behavior).

Built 2026-06-08. Isolated from the equity engine — imports creds from the shared
`config` but writes nothing to equity state.
"""

import requests

try:  # shared equity-engine credentials (never hardcode)
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
except Exception:  # pragma: no cover - config must exist in this project
    ALPACA_API_KEY = ""
    ALPACA_SECRET_KEY = ""

# Options-subsystem config (gates + multiplier). Support both package and flat import.
try:
    from options.config import (
        OPT_ENABLE_PAPER, OPT_ENABLE_LIVE, OPT_CONTRACT_MULTIPLIER,
    )
except Exception:  # pragma: no cover - fallback when run inside the options/ dir
    from config import (  # type: ignore
        OPT_ENABLE_PAPER, OPT_ENABLE_LIVE, OPT_CONTRACT_MULTIPLIER,
    )

# HARD-CODED PAPER endpoint. There is intentionally NO live URL in this module.
PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"

_TIMEOUT = 15


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def _is_option_symbol(symbol: str, asset_class: str | None = None) -> bool:
    """Heuristic: an OCC option symbol is long (>6 chars) and embeds the
    YYMMDD + C/P + 8-digit strike. Prefer Alpaca's asset_class when present."""
    if asset_class is not None:
        return asset_class == "us_option"
    if not symbol:
        return False
    # OCC: <root><yymmdd><C|P><strike*1000 padded to 8>. Min length ~ 1+6+1+8 = 16.
    return len(symbol) > 6 and any(ch.isdigit() for ch in symbol)


def place_option_order(
    contract_symbol: str,
    qty: int,
    side: str = "buy",
    limit_price: float | None = None,
    dry_run: bool = True,
) -> dict:
    """Place (or, by default, dry-run) a long-option PAPER order.

    GATES (checked in order):
      1. OPT_ENABLE_PAPER False                  -> {"status":"disabled"}
      2. OPT_ENABLE_LIVE True (must never be)     -> {"status":"refused_live"}
      3. qty <= 0                                 -> {"status":"skipped_zero_qty"}

    Long-option only: side is normalized to buy_to_open. dry_run=True (default)
    builds + validates the request dict and returns it WITHOUT submitting.
    dry_run=False submits a PAPER limit order (limit_price required for a real
    submit; if None the caller's selected mid/ask should be passed in).
    """
    # Gate 1 — paper must be enabled.
    if not OPT_ENABLE_PAPER:
        return {"status": "disabled", "reason": "OPT_ENABLE_PAPER is False"}

    # Gate 2 — HARD live gate. This module has no live path; refuse outright.
    if OPT_ENABLE_LIVE:
        return {
            "status": "refused_live",
            "reason": "OPT_ENABLE_LIVE is True — options must never trade live",
        }

    # Gate 3 — nothing to do for non-positive quantity.
    try:
        qty_int = int(qty)
    except (TypeError, ValueError):
        qty_int = 0
    if qty_int <= 0:
        return {"status": "skipped_zero_qty", "qty": qty_int}

    # Long-option only: we only ever buy_to_open. Reject anything that isn't a buy.
    side_norm = str(side).lower().strip()
    if side_norm not in ("buy", "buy_to_open"):
        return {
            "status": "refused_side",
            "reason": f"long-option only (buy_to_open); got side={side!r}",
        }

    # Build the order request. limit order if a price is given; market otherwise
    # (for the dry-run preview we still record whatever was requested).
    order_req: dict = {
        "symbol": contract_symbol,
        "qty": str(qty_int),
        "side": "buy",
        "type": "limit" if limit_price is not None else "market",
        "time_in_force": "day",
        "position_intent": "buy_to_open",
    }
    if limit_price is not None:
        order_req["limit_price"] = str(round(float(limit_price), 2))

    # dry_run — validate + return the request WITHOUT submitting.
    if dry_run:
        return {
            "status": "dry_run",
            "request": order_req,
            "endpoint": f"{PAPER_TRADING_BASE}/v2/orders",
            "live": False,
        }

    # Real submit (PAPER only). Limit orders need a price; fall back to market only
    # if the caller explicitly passed None (kept for completeness — strategy passes mid/ask).
    try:
        resp = requests.post(
            f"{PAPER_TRADING_BASE}/v2/orders",
            headers=_headers(),
            json=order_req,
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # network failure — never crash the caller
        return {"status": "error", "reason": f"request_failed: {exc}", "request": order_req}

    if resp.status_code in (200, 201):
        try:
            body = resp.json()
        except Exception:
            body = {}
        return {
            "status": "submitted",
            "order_id": body.get("id"),
            "symbol": body.get("symbol", contract_symbol),
            "qty": body.get("qty", str(qty_int)),
            "side": body.get("side", "buy"),
            "order_status": body.get("status"),
            "request": order_req,
        }

    # Non-2xx — surface the error without raising.
    try:
        err = resp.json()
    except Exception:
        err = {"message": resp.text[:300]}
    return {
        "status": "error",
        "http_status": resp.status_code,
        "reason": err.get("message", "submit_failed"),
        "request": order_req,
    }


def close_option_position(
    contract_symbol: str,
    qty: int,
    limit_price: float | None = None,
    dry_run: bool = True,
) -> dict:
    """SELL-TO-CLOSE an existing LONG option PAPER position.

    This CLOSES a long position (sells contracts already owned). It does NOT open a
    short — `position_intent` is "sell_to_close", `side` is "sell". Same gates and
    paper-only posture as place_option_order.

    GATES (checked in order):
      1. OPT_ENABLE_PAPER False                  -> {"status":"disabled"}
      2. OPT_ENABLE_LIVE True (must never be)     -> {"status":"refused_live"}
      3. qty <= 0                                 -> {"status":"skipped_zero_qty"}

    dry_run=True (default) builds + validates the request dict and returns it WITHOUT
    submitting. dry_run=False submits a PAPER order (limit if a price is given, market
    otherwise). Network/non-2xx errors return {"status":"error",...} and never raise.
    """
    # Gate 1 — paper must be enabled.
    if not OPT_ENABLE_PAPER:
        return {"status": "disabled", "reason": "OPT_ENABLE_PAPER is False"}

    # Gate 2 — HARD live gate. This module has no live path; refuse outright.
    if OPT_ENABLE_LIVE:
        return {
            "status": "refused_live",
            "reason": "OPT_ENABLE_LIVE is True — options must never trade live",
        }

    # Gate 3 — nothing to do for non-positive quantity.
    try:
        qty_int = int(qty)
    except (TypeError, ValueError):
        qty_int = 0
    if qty_int <= 0:
        return {"status": "skipped_zero_qty", "qty": qty_int}

    # Build the closing order request. SELL to close a LONG position — never a short.
    order_req: dict = {
        "symbol": contract_symbol,
        "qty": str(qty_int),
        "side": "sell",
        "type": "limit" if limit_price is not None else "market",
        "time_in_force": "day",
        "position_intent": "sell_to_close",
    }
    if limit_price is not None:
        order_req["limit_price"] = str(round(float(limit_price), 2))

    # dry_run — validate + return the request WITHOUT submitting.
    if dry_run:
        return {
            "status": "dry_run",
            "request": order_req,
            "endpoint": f"{PAPER_TRADING_BASE}/v2/orders",
            "live": False,
        }

    # Real submit (PAPER only).
    try:
        resp = requests.post(
            f"{PAPER_TRADING_BASE}/v2/orders",
            headers=_headers(),
            json=order_req,
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # network failure — never crash the caller
        return {"status": "error", "reason": f"request_failed: {exc}", "request": order_req}

    if resp.status_code in (200, 201):
        try:
            body = resp.json()
        except Exception:
            body = {}
        return {
            "status": "submitted",
            "order_id": body.get("id"),
            "symbol": body.get("symbol", contract_symbol),
            "qty": body.get("qty", str(qty_int)),
            "side": body.get("side", "sell"),
            "order_status": body.get("status"),
            "request": order_req,
        }

    # Non-2xx — surface the error without raising.
    try:
        err = resp.json()
    except Exception:
        err = {"message": resp.text[:300]}
    return {
        "status": "error",
        "http_status": resp.status_code,
        "reason": err.get("message", "submit_failed"),
        "request": order_req,
    }


def get_option_positions() -> list[dict]:
    """Return current PAPER option positions only.

    GET /v2/positions filtered to option symbols (asset_class == 'us_option', or
    an OCC-style long symbol when asset_class is absent). Never crashes — returns
    [] on any error.
    """
    try:
        resp = requests.get(
            f"{PAPER_TRADING_BASE}/v2/positions",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
    except Exception:
        return []

    if resp.status_code != 200:
        return []

    try:
        rows = resp.json()
    except Exception:
        return []
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        symbol = p.get("symbol", "")
        asset_class = p.get("asset_class")
        if not _is_option_symbol(symbol, asset_class):
            continue

        def _f(key: str) -> float:
            try:
                return float(p.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        out.append({
            "symbol": symbol,
            "qty": _f("qty"),
            "avg_entry_price": _f("avg_entry_price"),
            "market_value": _f("market_value"),
            "unrealized_pl": _f("unrealized_pl"),
        })
    return out
