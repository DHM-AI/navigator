"""Illuminati — daily OPTIONS (paper) summary.

Read-only. NEVER trades. Pulls the paper account's option fills + open positions,
computes what was opened/closed today and the realized + unrealized P&L, and posts
a Slack summary marked OPTIONS (PAPER) so the demo options experiment can be judged
on real data. Designed to run at EOD alongside the equity report (eod_trigger.sh).

Run: python -m options.report
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import requests

try:
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
except Exception:  # pragma: no cover - isolated test runs
    import os
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

_BASE = "https://paper-api.alpaca.markets"          # PAPER only — this is a demo report
_H = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
_MULT = 100                                         # option contract multiplier


def _is_option(sym: str) -> bool:
    return isinstance(sym, str) and len(sym) > 6 and any(c.isdigit() for c in sym)


def _today_et() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def _option_fills(days: int = 30) -> list[dict]:
    """All option FILL activities over the window (paginated). [] on any error."""
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out, page_token = [], None
    for _ in range(20):
        params = {"after": after, "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(f"{_BASE}/v2/account/activities/FILL",
                             headers=_H, params=params, timeout=30).json()
        except Exception:
            break
        if not isinstance(r, list) or not r:
            break
        out += [x for x in r if isinstance(x, dict)]
        if len(r) < 100:
            break
        page_token = r[-1].get("id")
    return [x for x in out if _is_option(x.get("symbol", ""))]


def build_summary() -> dict:
    today = _today_et()
    fills = sorted(_option_fills(), key=lambda z: z.get("transaction_time", ""))
    by_contract: dict[str, list] = defaultdict(list)
    for f in fills:
        by_contract[f["symbol"]].append(f)

    closed_today: list[dict] = []
    opened_today: list[dict] = []
    # Long-only: buy_to_open -> sell_to_close. FIFO-match per contract.
    for sym, fl in by_contract.items():
        lots: deque = deque()
        for f in fl:
            side = str(f.get("side", "")).lower()
            try:
                q = float(f.get("qty") or 0)
                px = float(f.get("price") or 0)
            except (TypeError, ValueError):
                continue
            d = str(f.get("transaction_time", ""))[:10]
            if "buy" in side:
                lots.append([q, px])
                if d == today:
                    opened_today.append({"sym": sym, "qty": q, "px": px})
            else:  # sell / sell_to_close
                r = q
                while r > 1e-9 and lots:
                    lq, lp = lots[0]
                    m = min(r, lq)
                    if d == today:
                        closed_today.append({
                            "sym": sym, "qty": m, "entry": lp, "exit": px,
                            "pnl": (px - lp) * m * _MULT,
                        })
                    lots[0][0] -= m
                    r -= m
                    if lots[0][0] <= 1e-9:
                        lots.popleft()

    try:
        positions = [p for p in requests.get(f"{_BASE}/v2/positions", headers=_H, timeout=20).json()
                     if isinstance(p, dict) and p.get("asset_class") == "us_option"]
    except Exception:
        positions = []

    realized = sum(t["pnl"] for t in closed_today)
    wins = [t for t in closed_today if t["pnl"] > 0]
    losses = [t for t in closed_today if t["pnl"] < 0]
    unrl = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    return {
        "today": today, "opened_today": opened_today, "closed_today": closed_today,
        "realized": realized, "wins": len(wins), "losses": len(losses),
        "open_pos": positions, "unrl": unrl,
    }


def format_slack(s: dict) -> str:
    lines = [f"\U0001F9EA *OPTIONS (PAPER) — {s['today']}*"]
    ct = s["closed_today"]
    if ct:
        lines.append(f"*Closed today:* {len(ct)} ({s['wins']}W/{s['losses']}L) · realized ${s['realized']:+,.0f}")
        for t in ct[:8]:
            lines.append(f"   • {t['sym']} {t['qty']:.0f}x  ${t['entry']:.2f}→${t['exit']:.2f}  ${t['pnl']:+,.0f}")
    else:
        lines.append("*Closed today:* none")
    if s["opened_today"]:
        names = ", ".join(o["sym"] for o in s["opened_today"][:6])
        lines.append(f"*Opened today:* {len(s['opened_today'])} — {names}")
    op = s["open_pos"]
    if op:
        lines.append(f"*Open now:* {len(op)} · unrealized ${s['unrl']:+,.0f}")
        for p in op[:8]:
            lines.append(f"   • {p['symbol']} {p.get('qty')}x  ${float(p.get('unrealized_pl') or 0):+,.0f}")
    else:
        lines.append("*Open now:* flat")
    lines.append(f"_Day total (realized + open): ${s['realized'] + s['unrl']:+,.0f} · paper/demo, OPT_ENABLE_LIVE=False_")
    return "\n".join(lines)


def daily_options_report() -> str:
    try:
        msg = format_slack(build_summary())
    except Exception as e:  # never raise from a report
        msg = f"\U0001F9EA OPTIONS (PAPER) report error: {e}"
    print(msg)
    try:
        from alerts.slack import _post
        _post({"text": msg})
    except Exception as e:
        print(f"[options.report] slack post skipped: {e}")
    return msg


if __name__ == "__main__":  # pragma: no cover - manual / scheduler entry point
    daily_options_report()
