"""Trade autopsy — decompose realized P&L along every dimension to find the leaks.

Pulls 60d of Alpaca fills, rebuilds round-trips with the seeded-FIFO matcher
(same logic as the dashboard), joins each trade to its prediction (score), and
breaks net P&L down by: entry hour, weekday, direction, score band, hold time,
price band, exit type, and ticker. The output is the evidence base for strategy
changes — change nothing that this report doesn't justify.

Usage: python trade_autopsy.py [--days 60]
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests

import config  # loads .env

H = {"APCA-API-KEY-ID": config.ALPACA_API_KEY, "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY}
BASE = "https://paper-api.alpaca.markets"
SUPA = os.environ.get("SUPABASE_URL")
SKEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")


def _pts(s):
    s = s.replace("Z", "+00:00")
    m = re.match(r"(.*\.)(\d+)([+-].*)", s)
    if m:
        s = m.group(1) + (m.group(2) + "000000")[:6] + m.group(3)
    return datetime.fromisoformat(s)


def fetch_fills(days):
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    fills, tok = [], None
    for _ in range(80):
        p = {"after": after, "page_size": 100}
        if tok:
            p["page_token"] = tok
        r = requests.get(f"{BASE}/v2/account/activities/FILL", headers=H, params=p, timeout=30).json()
        if not isinstance(r, list) or not r:
            break
        fills += [f for f in r if isinstance(f, dict)]
        if len(r) < 100:
            break
        tok = r[-1].get("id")
    fills.sort(key=lambda z: z.get("transaction_time", ""))
    return fills


def is_option(sym):
    return len(sym) > 6 and any(c.isdigit() for c in sym)


def round_trips(fills):
    """Seeded-FIFO: same matcher the dashboard uses. Returns equity round-trips."""
    pos = {p["symbol"]: float(p["qty"]) for p in requests.get(f"{BASE}/v2/positions", headers=H, timeout=20).json()}
    by = defaultdict(list)
    for f in fills:
        if "/" in f["symbol"] or is_option(f["symbol"]):
            continue
        by[f["symbol"]].append(f)
    trips = []
    for tk, fl in by.items():
        inwin = sum((float(f["qty"]) if f["side"] in ("buy", "buy_to_cover") else -float(f["qty"])) for f in fl)
        start = pos.get(tk, 0.0) - inwin
        lots = deque()
        if abs(start) > 1e-6:
            lots.append([1 if start > 0 else -1, abs(start), float(fl[0]["price"]), None, True])
        for f in fl:
            q, px = float(f["qty"]), float(f["price"])
            sgn = 1 if f["side"] in ("buy", "buy_to_cover") else -1
            t = _pts(f["transaction_time"])
            if not lots or (lots[0][0] > 0) == (sgn > 0):
                lots.append([sgn, q, px, t, False])
            else:
                r = q
                while r > 1e-9 and lots and (lots[0][0] > 0) != (sgn > 0):
                    lot = lots[0]
                    m = min(r, lot[1])
                    if not lot[4] and lot[3] is not None:
                        pnl = (px - lot[2]) * m * (1 if lot[0] > 0 else -1)
                        trips.append({
                            "tk": tk, "pnl": pnl, "qty": m,
                            "entry_px": lot[2], "exit_px": px,
                            "entry_t": lot[3], "exit_t": t,
                            "dir": "long" if lot[0] > 0 else "short",
                            "pnl_pct": (px - lot[2]) / lot[2] * 100 * (1 if lot[0] > 0 else -1),
                        })
                    lot[1] -= m
                    r -= m
                    if lot[1] <= 1e-9:
                        lots.popleft()
                if r > 1e-9:
                    lots.append([sgn, r, px, t, False])
    return trips


def fetch_scores():
    """date->ticker->score map from predictions."""
    if not SUPA or not SKEY:
        return {}
    hh = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}
    out, off = {}, 0
    while True:
        r = requests.get(f"{SUPA}/rest/v1/predictions", headers={**hh, "Range": f"{off}-{off+999}"},
                         params={"select": "date,ticker,score", "order": "date.desc"}, timeout=60)
        b = r.json()
        if not isinstance(b, list) or not b:
            break
        for row in b:
            try:
                out[(row["date"], row["ticker"])] = float(row["score"])
            except (KeyError, TypeError, ValueError):
                pass
        if len(b) < 1000 or off > 9000:
            break
        off += 1000
    return out


ET = timezone(timedelta(hours=-4))  # EDT (June); fine for this window


def bucketize(trips, scores):
    for t in trips:
        et_in = t["entry_t"].astimezone(ET)
        t["hour"] = et_in.hour
        t["dow"] = et_in.strftime("%a")
        t["hold_h"] = (t["exit_t"] - t["entry_t"]).total_seconds() / 3600
        t["date"] = et_in.strftime("%Y-%m-%d")
        # join score: try entry date, then prior day (scans store the scan date)
        sc = scores.get((t["date"], t["tk"]))
        if sc is None:
            d2 = (et_in - timedelta(days=1)).strftime("%Y-%m-%d")
            sc = scores.get((d2, t["tk"]))
        t["score"] = sc
        p = t["pnl_pct"]
        t["exit_kind"] = ("stop" if p <= -2.5 else
                          "big_win" if p >= 6.5 else
                          "trail/manual")
        t["px_band"] = ("<5" if t["entry_px"] < 5 else "5-20" if t["entry_px"] < 20 else
                        "20-100" if t["entry_px"] < 100 else "100+")
        t["hold_band"] = ("<4h" if t["hold_h"] < 4 else "4-24h" if t["hold_h"] < 24 else
                          "1-3d" if t["hold_h"] < 72 else "3d+")
        s = t["score"]
        t["score_band"] = ("?" if s is None else "<70" if s < 70 else "70-75" if s < 75 else
                           "75-80" if s < 80 else "80+")


def table(trips, key, title):
    g = defaultdict(list)
    for t in trips:
        g[t[key]].append(t)
    print(f"\n=== {title} ===")
    print(f"  {'bucket':10} {'n':>5} {'net P&L':>10} {'win%':>5} {'avgW':>7} {'avgL':>7} {'PF':>5}")
    rows = sorted(g.items(), key=lambda kv: sum(x['pnl'] for x in kv[1]))
    for k, v in rows:
        w = [x["pnl"] for x in v if x["pnl"] > 0]
        l = [x["pnl"] for x in v if x["pnl"] <= 0]
        net = sum(x["pnl"] for x in v)
        pf = (sum(w) / abs(sum(l))) if l and sum(l) != 0 else float("inf")
        print(f"  {str(k):10} {len(v):>5} {net:>+10.0f} {len(w)/len(v)*100:>4.0f}% "
              f"{(sum(w)/len(w) if w else 0):>+7.0f} {(sum(l)/len(l) if l else 0):>+7.0f} {pf:>5.2f}")


def main(days=60):
    fills = fetch_fills(days)
    print(f"fills fetched: {len(fills)}")
    trips = round_trips(fills)
    scores = fetch_scores()
    bucketize(trips, scores)
    w = [t["pnl"] for t in trips if t["pnl"] > 0]
    l = [t["pnl"] for t in trips if t["pnl"] <= 0]
    print(f"\n===== OVERALL: {len(trips)} round-trips / {days}d =====")
    print(f"  net ${sum(w)+sum(l):+,.0f} | win {len(w)/max(len(trips),1)*100:.0f}% | "
          f"PF {sum(w)/abs(sum(l) or 1):.2f} | avgW ${sum(w)/max(len(w),1):+.0f} avgL ${sum(l)/max(len(l),1):+.0f}")
    table(trips, "hour", "BY ENTRY HOUR (ET)")
    table(trips, "dow", "BY WEEKDAY")
    table(trips, "dir", "BY DIRECTION")
    table(trips, "score_band", "BY MODEL SCORE AT ENTRY")
    table(trips, "hold_band", "BY HOLD TIME")
    table(trips, "px_band", "BY ENTRY PRICE BAND")
    table(trips, "exit_kind", "BY EXIT KIND (approx)")
    # worst/best tickers
    g = defaultdict(float)
    n = defaultdict(int)
    for t in trips:
        g[t["tk"]] += t["pnl"]
        n[t["tk"]] += 1
    rk = sorted(g.items(), key=lambda kv: kv[1])
    print("\n=== WORST 10 TICKERS ===")
    for k, v in rk[:10]:
        print(f"  {k:6} {v:>+8.0f}  ({n[k]} trips)")
    print("=== BEST 10 TICKERS ===")
    for k, v in rk[-10:][::-1]:
        print(f"  {k:6} {v:>+8.0f}  ({n[k]} trips)")
    # daily P&L curve, last 15 days with trades
    dg = defaultdict(float)
    for t in trips:
        dg[t["exit_t"].astimezone(ET).strftime("%Y-%m-%d")] += t["pnl"]
    print("\n=== REALIZED BY EXIT DAY (last 15) ===")
    for d in sorted(dg)[-15:]:
        print(f"  {d}  {dg[d]:>+8.0f}")


if __name__ == "__main__":
    d = 60
    if "--days" in sys.argv:
        d = int(sys.argv[sys.argv.index("--days") + 1])
    main(d)
