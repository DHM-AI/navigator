"""Illuminati Options — market-data layer (READ-ONLY, OPRA-aware).

Wraps the Alpaca options REST endpoints the equity engine already has creds for:

  - /v2/options/contracts          (trading API)  -> list tradable contracts
  - /v1beta1/options/snapshots/{u} (data API)     -> bid/ask/last per contract
  - /v1beta1/options/bars          (data API)     -> historical bars (OPRA-gated)

Design notes / decisions
------------------------
* Reuses ALPACA_API_KEY / ALPACA_SECRET_KEY from the equity engine's config —
  nothing is hardcoded. This module is read-only: it NEVER submits an order.
* The contracts API returns numeric fields as STRINGS (e.g. strike_price="410");
  we coerce to float defensively and never crash on a missing field.
* Snapshots carry NO greeks until the OPRA agreement is signed, so greeks come
  back as None and select_contract falls back to ATM moneyness in that case.
* get_option_bars handles the OPRA 403 gracefully by returning
  {"_error": "opra_unsigned"} instead of raising — callers (e.g. the backtest)
  branch on that to use the Black-Scholes approximation.
* Underlying price for strike selection comes from yfinance fast_info, with a
  daily-history fallback, and finally the snapshot's underlying trade if present.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

import requests

try:
    # Reuse the equity engine's Alpaca creds — do NOT hardcode.
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
except Exception:  # pragma: no cover - defensive import for isolated test runs
    import os
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

# ---------------------------------------------------------------------------
# Endpoints / headers
# ---------------------------------------------------------------------------
TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
_TIMEOUT = 30


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "accept": "application/json",
    }


def _to_float(value, default: Optional[float] = None) -> Optional[float]:
    """Coerce a possibly-string / possibly-missing numeric field to float."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 1) Contract discovery  — /v2/options/contracts
# ---------------------------------------------------------------------------
def list_contracts(
    underlying: str,
    dte_min: int,
    dte_max: int,
    opt_type: Optional[str] = None,
) -> list[dict]:
    """List active option contracts for ``underlying`` within a DTE window.

    Returns ``[{"symbol", "strike", "expiration", "type"}]`` where ``strike`` is
    a float, ``expiration`` is ``"YYYY-MM-DD"`` and ``type`` is "call"/"put".
    Never raises on a transport/HTTP error — returns ``[]`` instead.
    """
    today = _dt.date.today()
    gte = (today + _dt.timedelta(days=int(dte_min))).isoformat()
    lte = (today + _dt.timedelta(days=int(dte_max))).isoformat()

    params = {
        "underlying_symbols": underlying.upper(),
        "expiration_date_gte": gte,
        "expiration_date_lte": lte,
        "status": "active",
        "limit": 10000,
    }
    if opt_type in ("call", "put"):
        params["type"] = opt_type

    out: list[dict] = []
    try:
        resp = requests.get(
            f"{TRADING_BASE}/v2/options/contracts",
            params=params,
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return out
        body = resp.json() or {}
    except (requests.RequestException, ValueError):
        return out

    for c in body.get("option_contracts", []) or []:
        sym = c.get("symbol")
        strike = _to_float(c.get("strike_price"))
        exp = c.get("expiration_date")
        ctype = c.get("type")
        if not sym or strike is None or not exp or ctype not in ("call", "put"):
            continue
        out.append(
            {"symbol": sym, "strike": strike, "expiration": exp, "type": ctype}
        )
    return out


# ---------------------------------------------------------------------------
# 2) Chain snapshot  — /v1beta1/options/snapshots/{underlying}
# ---------------------------------------------------------------------------
def get_chain_snapshot(underlying: str) -> dict:
    """Quote snapshot for the whole chain of ``underlying``.

    Returns ``{contract_symbol: {"bid", "ask", "mid", "last", "greeks"}}``.
    ``mid`` = (bid+ask)/2 when both sides present, else None. ``greeks`` is the
    raw greeks dict when Alpaca returns one (requires OPRA) else None. Light
    pagination (best-effort, a few pages); never crashes on missing fields.
    """
    result: dict[str, dict] = {}
    page_token: Optional[str] = None
    pages = 0
    MAX_PAGES = 5  # snapshots can be large; cap the walk to stay light

    while pages < MAX_PAGES:
        params: dict = {"limit": 1000}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(
                f"{DATA_BASE}/v1beta1/options/snapshots/{underlying.upper()}",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                break
            body = resp.json() or {}
        except (requests.RequestException, ValueError):
            break

        snaps = body.get("snapshots", {}) or {}
        for sym, snap in snaps.items():
            if not isinstance(snap, dict):
                continue
            quote = snap.get("latestQuote") or {}
            trade = snap.get("latestTrade") or {}

            bid = _to_float(quote.get("bp"))
            ask = _to_float(quote.get("ap"))
            last = _to_float(trade.get("p"))

            mid = None
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                mid = round((bid + ask) / 2.0, 4)

            greeks = snap.get("greeks")
            if not isinstance(greeks, dict):
                greeks = None

            result[sym] = {
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": last,
                "greeks": greeks,
            }

        page_token = body.get("next_page_token")
        pages += 1
        if not page_token:
            break

    return result


# ---------------------------------------------------------------------------
# Underlying price helper (yfinance fast_info -> history -> snapshot fallback)
# ---------------------------------------------------------------------------
def _underlying_price(underlying: str) -> Optional[float]:
    """Best-effort current underlying price. Returns None if unavailable."""
    try:
        import yfinance as yf

        tkr = yf.Ticker(underlying.upper())
        # fast_info supports __getitem__ but is not a dict; guard everything.
        try:
            fi = tkr.fast_info
            px = None
            try:
                px = fi["last_price"]
            except Exception:
                px = getattr(fi, "last_price", None)
            px = _to_float(px)
            if px and px > 0:
                return px
        except Exception:
            pass
        # history fallback
        try:
            hist = tkr.history(period="1d")
            if hist is not None and not hist.empty:
                px = _to_float(hist["Close"].iloc[-1])
                if px and px > 0:
                    return px
        except Exception:
            pass
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 3) Contract selection
# ---------------------------------------------------------------------------
def _norm_direction(direction: str) -> Optional[str]:
    """Map a trade direction to an option type. None if unrecognized."""
    d = (direction or "").strip().lower()
    if d in ("bullish", "long", "buy", "call"):
        return "call"
    if d in ("bearish", "short", "sell", "put"):
        return "put"
    return None


def select_contract(
    underlying: str,
    direction: str,
    dte_target: int = 35,
    delta_target: Optional[float] = None,
) -> Optional[dict]:
    """Pick the single best contract for a directional view.

    Steps:
      1. list_contracts within [dte_target±] (we ask the broker over a window
         derived from the config DTE band) and keep the expiration nearest
         ``dte_target``.
      2. Get the current underlying price.
      3. If snapshot greeks are present, choose the strike whose |delta| is
         closest to ``delta_target``; otherwise choose ATM (strike nearest the
         underlying price).

    Returns ``{"symbol","strike","expiration","type","bid","ask","mid",
    "underlying_price"}`` or None when nothing suitable is found.
    """
    opt_type = _norm_direction(direction)
    if opt_type is None:
        return None

    # Resolve the DTE search window from config (fall back to ±14d of target).
    try:
        from options import config as _cfg

        dte_min = int(getattr(_cfg, "OPT_DTE_MIN", max(0, dte_target - 14)))
        dte_max = int(getattr(_cfg, "OPT_DTE_MAX", dte_target + 14))
        if delta_target is None:
            delta_target = getattr(_cfg, "OPT_TARGET_DELTA", None)
    except Exception:
        dte_min = max(0, dte_target - 14)
        dte_max = dte_target + 14

    contracts = list_contracts(underlying, dte_min, dte_max, opt_type)
    if not contracts:
        return None

    today = _dt.date.today()

    def _dte(exp: str) -> Optional[int]:
        try:
            return (_dt.date.fromisoformat(exp) - today).days
        except (ValueError, TypeError):
            return None

    # 1) Pick the expiration whose DTE is closest to the target.
    dated = [(c, _dte(c["expiration"])) for c in contracts]
    dated = [(c, d) for c, d in dated if d is not None]
    if not dated:
        return None
    best_dte = min({d for _, d in dated}, key=lambda d: abs(d - dte_target))
    expiration = next(c["expiration"] for c, d in dated if d == best_dte)
    candidates = [c for c in contracts if c["expiration"] == expiration]
    if not candidates:
        return None

    # 2) Underlying price (needed for ATM; nice-to-have for the output either way)
    und_px = _underlying_price(underlying)

    # 3) Snapshot for quotes + (maybe) greeks on this expiration's contracts.
    snapshot = get_chain_snapshot(underlying)
    cand_syms = {c["symbol"] for c in candidates}
    have_greeks = any(
        isinstance(snapshot.get(s), dict)
        and isinstance(snapshot[s].get("greeks"), dict)
        and snapshot[s]["greeks"].get("delta") is not None
        for s in cand_syms
    )

    chosen: Optional[dict] = None

    if have_greeks and delta_target is not None:
        # Choose strike whose |delta| is closest to delta_target.
        best_diff = None
        for c in candidates:
            snap = snapshot.get(c["symbol"]) or {}
            greeks = snap.get("greeks") or {}
            delta = _to_float(greeks.get("delta"))
            if delta is None:
                continue
            diff = abs(abs(delta) - float(delta_target))
            if best_diff is None or diff < best_diff:
                best_diff = diff
                chosen = c

    if chosen is None:
        # ATM fallback: strike closest to underlying price. If we somehow lack a
        # price, fall back to the median strike so we still return something sane.
        if und_px is not None:
            chosen = min(candidates, key=lambda c: abs(c["strike"] - und_px))
        else:
            ordered = sorted(candidates, key=lambda c: c["strike"])
            chosen = ordered[len(ordered) // 2]

    snap = snapshot.get(chosen["symbol"]) or {}
    return {
        "symbol": chosen["symbol"],
        "strike": chosen["strike"],
        "expiration": chosen["expiration"],
        "type": chosen["type"],
        "bid": snap.get("bid"),
        "ask": snap.get("ask"),
        "mid": snap.get("mid"),
        "underlying_price": und_px,
    }


# ---------------------------------------------------------------------------
# 4) Historical option bars  — /v1beta1/options/bars  (OPRA-gated)
# ---------------------------------------------------------------------------
def get_option_bars(
    symbols: list[str],
    start: str,
    end: str,
    timeframe: str = "1Day",
) -> dict:
    """Historical option bars for ``symbols`` between ``start`` and ``end``.

    On HTTP 403 (OPRA agreement not signed) returns ``{"_error":
    "opra_unsigned"}``. On any other transport error returns ``{"_error":
    "request_failed"}``. On success returns ``{symbol: [{t,o,h,l,c,v}, ...]}``.
    """
    if not symbols:
        return {}

    params = {
        "symbols": ",".join(symbols),
        "start": start,
        "end": end,
        "timeframe": timeframe,
        "limit": 10000,
    }
    try:
        resp = requests.get(
            f"{DATA_BASE}/v1beta1/options/bars",
            params=params,
            headers=_headers(),
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return {"_error": "request_failed"}

    if resp.status_code == 403:
        return {"_error": "opra_unsigned"}
    if resp.status_code != 200:
        return {"_error": "request_failed"}

    try:
        body = resp.json() or {}
    except ValueError:
        return {"_error": "request_failed"}

    out: dict[str, list] = {}
    for sym, bars in (body.get("bars", {}) or {}).items():
        rows = []
        for b in bars or []:
            rows.append(
                {
                    "t": b.get("t"),
                    "o": _to_float(b.get("o")),
                    "h": _to_float(b.get("h")),
                    "l": _to_float(b.get("l")),
                    "c": _to_float(b.get("c")),
                    "v": _to_float(b.get("v")),
                }
            )
        out[sym] = rows
    return out
