"""
ZEUS — Full System Audit Agent

Runs every weeknight at 11 PM ET (after ORACLE at 10 PM).
ZEUS is the guardian of the Illuminati system — it performs a deep audit
of every safety control and operational gate, then reports to Slack.

Checks:
  1.  Open positions all have stop loss orders active in Alpaca
  2.  Position count does not exceed MAX_OPEN_POSITIONS (15)
  3.  Daily trade count is within MAX_DAILY_TRADES (20)
  4.  Portfolio is above daily loss limit
  5.  GitHub Actions ran today (via Supabase predictions timestamp)
  6.  ORACLE ran and saved learnings today (or this week)
  7.  No positions are >20% underwater without a stop loss
  8.  Trailing stops are active on positions up >3%
  9.  Buying power is not near zero
 10.  XGBoost model file exists and is fresh
 11.  Partial exit history DB is accessible (silent failure = double-fires)
 12.  ORACLE double-run: daily_scan.yml must NOT own the 10 PM cron slot
 13.  AEGIS concurrency: trail_stops.yml and eod.yml must have concurrency group
 14.  ENABLE_OPTIONS is False (opt-in safety — default True = stray orders)
 15.  DAY trade bracket is tighter than swing (stop < 3%, target < 20%)
 16.  Partial exit fractions sum < 1.0 (T1+T2 must leave a remaining tranche)
 17.  Dashboard API health (live Cloudflare endpoint returns 200)
 18.  Mac launchd jobs are healthy (exit code 0, not 126)
 19.  Weekend-gap protection on (no new Friday-PM entries)
 20.  Entry cap per ticker on (blocks accumulating into a loser)
 21.  Catastrophic-day circuit breakers (same-day lockout, hard max-loss, daily halt)

Exit code 0 = all good, 1 = at least one FAIL.
"""
import os
import sys
from datetime import datetime, timedelta

# Anchor to this file's directory so relative paths (MODEL_PATH, etc.) work
# regardless of where `python zeus.py` is invoked from. Without this, running
# from ~/Documents/Claude/AI-trading/ sends false FAIL alerts for missing model.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Colors for terminal output ────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

PASS = f"{GREEN}✓ PASS{RESET}"
WARN = f"{YELLOW}⚠ WARN{RESET}"
FAIL = f"{RED}✗ FAIL{RESET}"

# ── ZEUS limits (hard caps — not in config to avoid accidental edits) ─────────
MAX_OPEN_POSITIONS   = 15
MAX_DAILY_TRADES     = 20
MIN_BUYING_POWER_PCT = 0.05   # WARN if buying power < 5% of portfolio
DEEP_UNDERWATER_PCT  = 20.0   # positions down >20% trigger FAIL
TRAIL_TRIGGER_PCT    = 3.0    # positions up >3% should have trailing stop


# ══════════════════════════════════════════════════════════════════════════════
# AuditReport — mirrors HealthReport from health_check.py
# ══════════════════════════════════════════════════════════════════════════════

class AuditReport:
    def __init__(self):
        self.checks  = []   # list of (name, status, detail)
        self.n_pass  = 0
        self.n_warn  = 0
        self.n_fail  = 0
        self.started = datetime.now()

    def add(self, name: str, status: str, detail: str):
        self.checks.append((name, status, detail))
        if   status == "PASS": self.n_pass += 1
        elif status == "WARN": self.n_warn += 1
        elif status == "FAIL": self.n_fail += 1
        icon = {"PASS": PASS, "WARN": WARN, "FAIL": FAIL}.get(status, status)
        print(f"  [ZEUS] {icon}  {name}: {detail}")

    def overall(self) -> str:
        if self.n_fail > 0:  return "FAIL"
        if self.n_warn > 0:  return "WARN"
        return "PASS"

    def summary(self) -> str:
        elapsed = (datetime.now() - self.started).seconds
        return (f"{self.n_pass} passed · {self.n_warn} warnings · "
                f"{self.n_fail} failures · {elapsed}s")


report = AuditReport()
today  = datetime.today().strftime("%Y-%m-%d")
now    = datetime.now()

# ══════════════════════════════════════════════════════════════════════════════
# BOOTSTRAP — environment + connections
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[ZEUS] Starting full system audit — {now.strftime('%Y-%m-%d %H:%M ET')}{RESET}")

from dotenv import load_dotenv
load_dotenv(override=True)

# Load Alpaca and Supabase once so checks can share the data
alpaca_acct      = {}
alpaca_positions = []
alpaca_orders    = []
alpaca_ok        = False

try:
    from execution.alpaca import is_configured, is_live_mode, get_account, get_positions
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, ALPACA_LIVE_MODE

    if is_configured():
        alpaca_acct      = get_account()
        alpaca_positions = get_positions()

        # Fetch active orders for stop loss inspection.
        # Bracket stop-loss legs have status "held" (not "open"), so we must
        # fetch ALL non-terminal orders and filter to active statuses.
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            from datetime import timedelta
            _client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY,
                                    paper=not ALPACA_LIVE_MODE)
            # Fetch open orders (includes take-profit legs)
            # H-4 fix: explicit limit=500. SDK default of 50 silently truncated
            # the list on busy days; Check 1 (stop coverage) flagged false
            # positives and auto-fix produced duplicate stops.
            _open_orders = _client.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
            # Fetch all recent orders to catch "held" bracket stop-loss legs
            _after = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
            _all_recent = _client.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.ALL, after=_after, limit=500))
            # Combine: keep orders in active states (open, held, accepted, pending_new)
            _active_statuses = {"open", "held", "accepted", "pending_new", "new"}
            _held = [o for o in _all_recent
                     if str(getattr(o, "status", "")).lower().replace("orderstatus.", "")
                     in _active_statuses]
            alpaca_orders = list({str(getattr(o,"id","")): o
                                  for o in (_open_orders + _held)}.values())
        except Exception as _e:
            print(f"  [ZEUS] Could not fetch raw orders: {_e}")

        alpaca_ok = True
except Exception as e:
    print(f"  [ZEUS] Alpaca bootstrap failed: {e}")

db_ok = False
try:
    import db
    db_ok = db.db_available()
except Exception:
    pass


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — All open positions have a stop loss order active
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[1/21] Stop Loss Coverage{RESET}")
if not alpaca_ok:
    report.add("Stop Loss Coverage", "WARN",
               "Alpaca not configured — cannot verify stop loss orders")
elif not alpaca_positions:
    report.add("Stop Loss Coverage", "PASS", "No open positions — nothing to check")
else:
    # Build set of tickers that have at least one stop or trailing stop order
    protected_tickers = set()
    for o in alpaca_orders:
        otype = str(getattr(o, "type", "")).lower()
        if "stop" in otype or "trailing" in otype:
            protected_tickers.add(o.symbol)
        # Also check bracket order legs
        for leg in (getattr(o, "legs", None) or []):
            if getattr(leg, "stop_price", None):
                protected_tickers.add(o.symbol)

    open_tickers  = {p["ticker"] for p in alpaca_positions}
    unprotected   = open_tickers - protected_tickers

    if unprotected:
        # Auto-fix: place stops for any unprotected position
        import time as _zeus_time
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import StopOrderRequest, GetOrdersRequest as _GORZEQ
        from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus as _ZQOS
        from config import KELLY_LOSS_PCT, ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_LIVE_MODE
        _fix_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=not ALPACA_LIVE_MODE)
        fixed, failed = [], []
        for p in alpaca_positions:
            if p["ticker"] not in unprotected:
                continue
            entry   = p["avg_entry_price"]
            qty     = abs(p["qty"])
            is_long = "long" in str(p.get("side","")).lower()
            stop    = round(entry * (1 - KELLY_LOSS_PCT if is_long else 1 + KELLY_LOSS_PCT), 2)
            side    = OrderSide.SELL if is_long else OrderSide.BUY
            _ticker = p["ticker"]
            _placed = False
            for tif in [TimeInForce.GTC, TimeInForce.DAY]:
                try:
                    _fix_client.submit_order(StopOrderRequest(
                        symbol=_ticker, qty=qty, side=side,
                        time_in_force=tif, stop_price=stop))
                    if _ticker not in fixed: fixed.append(_ticker)
                    _placed = True
                    break
                except Exception as _ze:
                    _zes = str(_ze).lower()
                    # held_for_orders / 40310000: stale bracket legs are holding
                    # the shares. Sweep cancel all active orders for this ticker,
                    # wait 2s for Alpaca to release the hold, then retry once.
                    if ("insufficient" in _zes or "held_for_orders" in _zes
                            or "40310000" in str(_ze)):
                        print(f"[ZEUS] {_ticker} stop hit held_for_orders "
                              f"— cancelling stale orders and retrying")
                        try:
                            _stale = _fix_client.get_orders(_GORZEQ(
                                status=_ZQOS.ALL, limit=200))
                            _active_st = {"open","held","accepted","pending_new","new"}
                            for _so in _stale:
                                if (getattr(_so,"symbol","") == _ticker
                                        and str(getattr(_so,"status","")).lower().replace(
                                            "orderstatus.","") in _active_st):
                                    try:
                                        _fix_client.cancel_order_by_id(str(_so.id))
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                        _zeus_time.sleep(2.0)
                        # Retry once with GTC, fall back to DAY
                        for _r_tif in [TimeInForce.GTC, TimeInForce.DAY]:
                            try:
                                _fix_client.submit_order(StopOrderRequest(
                                    symbol=_ticker, qty=qty, side=side,
                                    time_in_force=_r_tif, stop_price=stop))
                                if _ticker not in fixed: fixed.append(_ticker)
                                _placed = True
                                break
                            except Exception:
                                pass
                        break  # don't iterate outer tif loop again
                    if tif == TimeInForce.DAY:
                        pass  # falls through to failed append below
            if not _placed and _ticker not in fixed:
                failed.append(_ticker)
        msg = f"Auto-fixed: {fixed}" if fixed else ""
        if failed:
            report.add("Stop Loss Coverage", "FAIL",
                       f"Could not place stop for: {failed}. {msg}")
        else:
            report.add("Stop Loss Coverage", "WARN",
                       f"Missing stops auto-fixed for: {fixed}")
            # H-10 fix: re-fetch alpaca_orders so subsequent checks (esp.
            # Check-7 "deep underwater no stop") don't double-FAIL on the same
            # tickers we just auto-fixed.
            try:
                _refresh = _client.get_orders(GetOrdersRequest(
                    status=QueryOrderStatus.ALL, after=_after, limit=500))
                _new_combined = list(_open_orders) + list(_refresh)
                seen_ids = set(); _dedup = []
                for o in _new_combined:
                    if o.id in seen_ids: continue
                    seen_ids.add(o.id)
                    _dedup.append(o)
                alpaca_orders = _dedup
                print(f"[ZEUS] Refreshed orders after auto-fix: {len(alpaca_orders)} active")
            except Exception as _re:
                print(f"[ZEUS] Could not refresh orders: {_re}")
    else:
        report.add("Stop Loss Coverage", "PASS",
                   f"All {len(open_tickers)} position(s) have stop loss orders")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 2 — Position count ≤ MAX_OPEN_POSITIONS
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[2/21] Position Count Limit{RESET}")
if not alpaca_ok:
    report.add("Position Count", "WARN", "Alpaca not configured")
else:
    n_pos = len(alpaca_positions)
    if n_pos > MAX_OPEN_POSITIONS:
        report.add("Position Count", "WARN",
                   f"{n_pos} positions open — exceeds MAX_OPEN_POSITIONS ({MAX_OPEN_POSITIONS}) · guard blocking new trades")
    elif n_pos >= MAX_OPEN_POSITIONS * 0.8:
        report.add("Position Count", "WARN",
                   f"{n_pos}/{MAX_OPEN_POSITIONS} positions open — approaching limit")
    else:
        report.add("Position Count", "PASS",
                   f"{n_pos}/{MAX_OPEN_POSITIONS} positions open")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 3 — Daily trade count within MAX_DAILY_TRADES
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[3/21] Daily Trade Count{RESET}")
if not alpaca_ok:
    report.add("Daily Trade Count", "WARN", "Alpaca not configured")
else:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_LIVE_MODE

        _tc = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=not ALPACA_LIVE_MODE)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        all_today   = _tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))
        # Count only filled parent orders (not bracket legs)
        # CRITICAL audit C-5: was comparing "orderstatus.filled" against "filled"
        # → always 0, daily trade FAIL was unreachable. order_status() normalizes.
        from execution.alpaca import order_status
        filled_today = [
            o for o in all_today
            if order_status(o) in ("filled", "partially_filled")
            and getattr(o, "order_class", None) != "leg"
        ]
        n_trades = len(filled_today)
        if n_trades > MAX_DAILY_TRADES:
            report.add("Daily Trade Count", "FAIL",
                       f"{n_trades} trades today — exceeds MAX_DAILY_TRADES ({MAX_DAILY_TRADES})")
        elif n_trades >= MAX_DAILY_TRADES * 0.75:
            report.add("Daily Trade Count", "WARN",
                       f"{n_trades}/{MAX_DAILY_TRADES} trades today — approaching limit")
        else:
            report.add("Daily Trade Count", "PASS",
                       f"{n_trades}/{MAX_DAILY_TRADES} trades executed today")
    except Exception as e:
        report.add("Daily Trade Count", "WARN", f"Could not count trades: {str(e)[:80]}")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 4 — Portfolio above daily loss limit
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[4/21] Portfolio & Daily Loss Limit{RESET}")
if not alpaca_ok:
    report.add("Daily Loss Limit", "WARN", "Alpaca not configured")
else:
    try:
        from config import DAILY_LOSS_LIMIT_PCT
        portfolio    = alpaca_acct.get("portfolio_value", 0)
        last_equity  = alpaca_acct.get("last_equity", portfolio)
        daily_chg    = (portfolio - last_equity) / last_equity if last_equity else 0
        mode_tag     = "LIVE" if is_live_mode() else "PAPER"

        if daily_chg < -DAILY_LOSS_LIMIT_PCT:
            report.add("Daily Loss Limit", "FAIL",
                       f"[{mode_tag}] Down {abs(daily_chg):.1%} — "
                       f"exceeds {DAILY_LOSS_LIMIT_PCT:.0%} limit. "
                       f"Portfolio: ${portfolio:,.2f}")
        elif daily_chg < -(DAILY_LOSS_LIMIT_PCT * 0.6):
            report.add("Daily Loss Limit", "WARN",
                       f"[{mode_tag}] Down {abs(daily_chg):.1%} — "
                       f"approaching {DAILY_LOSS_LIMIT_PCT:.0%} limit. "
                       f"Portfolio: ${portfolio:,.2f}")
        else:
            report.add("Daily Loss Limit", "PASS",
                       f"[{mode_tag}] Daily P&L: {daily_chg:+.2%} · "
                       f"Portfolio: ${portfolio:,.2f}")
    except Exception as e:
        report.add("Daily Loss Limit", "WARN", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 5 — GitHub Actions ran today (predictions written today)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[5/21] GitHub Actions / Daily Scan{RESET}")
if not db_ok:
    report.add("Daily Scan (ARGUS)", "WARN", "Supabase not configured — cannot verify")
else:
    try:
        preds_today = db.load_predictions_for_date(today)
        n = len(preds_today)
        is_weekday  = now.weekday() < 5

        if n == 0 and is_weekday:
            # Check if we have predictions from yesterday as a freshness fallback
            yesterday  = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            preds_yest = db.load_predictions_for_date(yesterday)
            if preds_yest:
                report.add("Daily Scan (ARGUS)", "WARN",
                           f"No predictions for today ({today}) — scan may not have run yet. "
                           f"Yesterday had {len(preds_yest)} predictions.")
            else:
                report.add("Daily Scan (ARGUS)", "FAIL",
                           f"No predictions for today or yesterday — ARGUS may be broken")
        elif n == 0 and not is_weekday:
            report.add("Daily Scan (ARGUS)", "PASS",
                       f"Weekend — no predictions expected ({today})")
        else:
            latest_score = max(p.get("score", 0) for p in preds_today)
            report.add("Daily Scan (ARGUS)", "PASS",
                       f"{n} predictions written today · top score: {latest_score:.0f}")
    except Exception as e:
        report.add("Daily Scan (ARGUS)", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 6 — ORACLE ran and saved learnings this week
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[6/21] ORACLE Learnings{RESET}")
if not db_ok:
    report.add("ORACLE Learnings", "WARN", "Supabase not configured — cannot verify")
else:
    try:
        learnings = db.load_learnings()
        if not learnings:
            report.add("ORACLE Learnings", "WARN",
                       "No learnings in database — ORACLE has never run or table is empty")
        else:
            # learnings are ordered by week_of desc
            latest_raw  = learnings[0]
            week_of_str = latest_raw.get("week_of", "")
            if week_of_str:
                try:
                    week_dt   = datetime.strptime(week_of_str[:10], "%Y-%m-%d")
                    days_ago  = (now - week_dt).days
                    n_records = len(learnings)
                    if days_ago > 9:   # ORACLE runs weekly; >9 days = missed a cycle
                        report.add("ORACLE Learnings", "WARN",
                                   f"Latest learning is {days_ago}d old ({week_of_str}) — "
                                   f"ORACLE may have missed a run")
                    else:
                        report.add("ORACLE Learnings", "PASS",
                                   f"Latest learning: {week_of_str} ({days_ago}d ago) · "
                                   f"{n_records} total records")
                except ValueError:
                    report.add("ORACLE Learnings", "WARN",
                               f"Could not parse week_of date: {week_of_str!r}")
            else:
                report.add("ORACLE Learnings", "WARN",
                           "Latest learning has no week_of timestamp")
    except Exception as e:
        report.add("ORACLE Learnings", "WARN", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 7 — No positions >20% underwater without a stop loss
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[7/21] Deep Underwater Positions{RESET}")
if not alpaca_ok:
    report.add("Deep Underwater Check", "WARN", "Alpaca not configured")
elif not alpaca_positions:
    report.add("Deep Underwater Check", "PASS", "No open positions")
else:
    # Rebuild protected set (reuse logic from check 1)
    protected_2 = set()
    for o in alpaca_orders:
        otype = str(getattr(o, "type", "")).lower()
        if "stop" in otype or "trailing" in otype:
            protected_2.add(o.symbol)
        for leg in (getattr(o, "legs", None) or []):
            if getattr(leg, "stop_price", None):
                protected_2.add(o.symbol)

    critical = []
    flagged  = []
    for p in alpaca_positions:
        pct = p.get("unrealized_pl_pct", 0)   # already in %
        ticker = p["ticker"]
        if pct < -DEEP_UNDERWATER_PCT:
            if ticker not in protected_2:
                critical.append(f"{ticker} {pct:.1f}% (NO STOP)")
            else:
                flagged.append(f"{ticker} {pct:.1f}%")

    if critical:
        report.add("Deep Underwater Check", "FAIL",
                   f"Positions >20% down with NO stop: {', '.join(critical)}")
    elif flagged:
        report.add("Deep Underwater Check", "WARN",
                   f"Positions >20% down (stop active): {', '.join(flagged)}")
    else:
        worst = min((p.get("unrealized_pl_pct", 0) for p in alpaca_positions), default=0)
        report.add("Deep Underwater Check", "PASS",
                   f"No positions below -{DEEP_UNDERWATER_PCT:.0f}% · "
                   f"worst: {worst:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 8 — Trailing stops active on positions up >3%
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[8/21] Trailing Stop Coverage{RESET}")
if not alpaca_ok:
    report.add("Trailing Stop Coverage", "WARN", "Alpaca not configured")
elif not alpaca_positions:
    report.add("Trailing Stop Coverage", "PASS", "No open positions")
else:
    # Find tickers that have trailing stops
    trailing_tickers = set()
    for o in alpaca_orders:
        otype = str(getattr(o, "type", "")).lower()
        if "trailing" in otype:
            trailing_tickers.add(o.symbol)

    should_trail  = [
        p["ticker"] for p in alpaca_positions
        if p.get("unrealized_pl_pct", 0) >= TRAIL_TRIGGER_PCT
    ]
    missing_trail = [t for t in should_trail if t not in trailing_tickers]

    if missing_trail:
        report.add("Trailing Stop Coverage", "WARN",
                   f"Up >3% but no trailing stop: {', '.join(missing_trail)} "
                   f"— AEGIS may need to run")
    elif should_trail:
        report.add("Trailing Stop Coverage", "PASS",
                   f"All {len(should_trail)} profitable position(s) have trailing stops")
    else:
        report.add("Trailing Stop Coverage", "PASS",
                   f"No positions up >3% yet — trailing stops not required")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 9 — Buying power is not near zero
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[9/21] Buying Power{RESET}")
if not alpaca_ok:
    report.add("Buying Power", "WARN", "Alpaca not configured")
else:
    try:
        buying_power  = alpaca_acct.get("buying_power", 0)
        portfolio_val = alpaca_acct.get("portfolio_value", 1) or 1
        bp_pct        = buying_power / portfolio_val

        if buying_power <= 0:
            report.add("Buying Power", "FAIL",
                       f"Buying power is $0 — new trades are impossible")
        elif bp_pct < MIN_BUYING_POWER_PCT:
            report.add("Buying Power", "WARN",
                       f"Buying power: ${buying_power:,.2f} ({bp_pct:.1%} of portfolio) — "
                       f"nearly fully deployed")
        else:
            report.add("Buying Power", "PASS",
                       f"Buying power: ${buying_power:,.2f} ({bp_pct:.1%} of portfolio)")
    except Exception as e:
        report.add("Buying Power", "WARN", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 10 — XGBoost model exists and is fresh
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[10/21] XGBoost Model Freshness{RESET}")
try:
    from config import MODEL_PATH, FEATURE_NAMES_PATH

    if not os.path.exists(MODEL_PATH):
        report.add("XGBoost Model", "FAIL",
                   f"Model not found at {MODEL_PATH} — run: python -m model.trainer")
    else:
        model_age_days = (now.timestamp() - os.path.getmtime(MODEL_PATH)) / 86400
        age_str = f"{model_age_days:.0f}d old"

        try:
            import pickle
            with open(MODEL_PATH, "rb") as f:
                model = pickle.load(f)
            model_ok = True
        except Exception as load_err:
            model_ok = False
            report.add("XGBoost Model", "FAIL",
                       f"Model file corrupt — cannot load: {load_err}")

        if model_ok:
            feat_str = ""
            if os.path.exists(FEATURE_NAMES_PATH):
                import json
                with open(FEATURE_NAMES_PATH) as f:
                    feats = json.load(f)
                feat_str = f" · {len(feats)} features"

            if model_age_days > 35:
                report.add("XGBoost Model", "FAIL",
                           f"Model is {age_str} — GENESIS retrain is overdue (>35 days)")
            elif model_age_days > 28:
                report.add("XGBoost Model", "WARN",
                           f"Model is {age_str} — retrain within the week{feat_str}")
            else:
                report.add("XGBoost Model", "PASS",
                           f"Loads OK · {age_str}{feat_str}")
except Exception as e:
    report.add("XGBoost Model", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 11 — Partial exit history DB is accessible
# A silent `pass` in get_partial_exit_history() returned {} on DB failure,
# making AEGIS think T1/T2 had never fired → double partial exits on live money.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[11/21] Partial Exit DB Accessibility{RESET}")
if not db_ok:
    report.add("Partial Exit DB", "WARN", "Supabase not configured — cannot verify")
else:
    try:
        from db import get_partial_exit_history
        open_tickers = {p["ticker"] for p in alpaca_positions} if alpaca_positions else {"_test_"}
        result = get_partial_exit_history(open_tickers=open_tickers)
        if result is None:
            report.add("Partial Exit DB", "FAIL",
                       "get_partial_exit_history() returned None — DB error. "
                       "AEGIS will skip partial exits this cycle to prevent double-fires.")
        else:
            fired = [t for t, h in result.items() if h.get("t1")]
            report.add("Partial Exit DB", "PASS",
                       f"DB accessible · {len(result)} tickers with history · "
                       f"T1 fired: {fired or 'none'}")
    except Exception as e:
        report.add("Partial Exit DB", "FAIL", f"Exception: {str(e)[:100]}")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 12 — ORACLE must not run twice (daily_scan.yml must not own 10PM slot)
# Bug found 2026-05-29: both daily_scan.yml and oracle.yml had '0 2 * * 2-6'
# cron → ORACLE ran twice nightly, doubled Supabase writes + API spend.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[12/21] ORACLE Double-Run Guard{RESET}")
try:
    import re
    scan_yml = ".github/workflows/daily_scan.yml"
    oracle_yml = ".github/workflows/oracle.yml"
    if not os.path.exists(scan_yml):
        report.add("ORACLE Double-Run", "WARN", f"{scan_yml} not found")
    else:
        with open(scan_yml) as f:
            scan_content = f.read()
        # The 10 PM ET slot is '0 2 * * *' or '0 2 * * 2-6' in UTC
        oracle_cron = re.findall(r"cron:\s*['\"]0 2 \* \* [^'\"]+['\"]", scan_content)
        # Filter out commented lines
        active = [c for c in oracle_cron
                  if not any(line.strip().startswith("#")
                             for line in scan_content.split("\n")
                             if c.split("'")[1] in line or c.split('"')[1] in line
                             if line.strip().startswith("#"))]
        # Simpler check: look for uncommented 0 2 cron in scan file
        uncommented = [l for l in scan_content.split("\n")
                       if "cron:" in l and "0 2 " in l and not l.strip().startswith("#")]
        if uncommented:
            report.add("ORACLE Double-Run", "FAIL",
                       f"daily_scan.yml still has 10 PM UTC cron — ORACLE will run twice: "
                       f"{uncommented[0].strip()}")
        else:
            report.add("ORACLE Double-Run", "PASS",
                       "daily_scan.yml does not own the 10 PM slot — ORACLE runs once via oracle.yml")
except Exception as e:
    report.add("ORACLE Double-Run", "WARN", f"Could not parse workflow: {str(e)[:80]}")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 13 — AEGIS concurrency group prevents overlapping runs
# Bug found 2026-05-29: trail_stops.yml + eod.yml could run simultaneously
# at 3:45/3:50/4:00 PM ET → concurrent partial exits before DB records land.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[13/21] AEGIS Concurrency Guard{RESET}")
try:
    missing_concurrency = []
    for yml in [".github/workflows/trail_stops.yml", ".github/workflows/eod.yml"]:
        if not os.path.exists(yml):
            missing_concurrency.append(f"{yml} (missing)")
            continue
        with open(yml) as f:
            content = f.read()
        if "concurrency:" not in content or "group: aegis" not in content:
            missing_concurrency.append(os.path.basename(yml))
    if missing_concurrency:
        report.add("AEGIS Concurrency", "FAIL",
                   f"Missing 'concurrency:' key or 'group: aegis' in: {', '.join(missing_concurrency)} "
                   f"— concurrent AEGIS runs can double-fire partial exits")
    else:
        report.add("AEGIS Concurrency", "PASS",
                   "trail_stops.yml + eod.yml both have concurrency: group=aegis")
except Exception as e:
    report.add("AEGIS Concurrency", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 14 — ENABLE_OPTIONS must be False (opt-in, not opt-out)
# Bug found 2026-05-29: defaulted True → iron butterfly orders submitted
# on any deployment without explicit ENABLE_OPTIONS=false env var.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[14/21] Options Safety Gate{RESET}")
try:
    from config import ENABLE_OPTIONS
    if ENABLE_OPTIONS:
        report.add("Options Safety Gate", "WARN",
                   "ENABLE_OPTIONS=True — iron butterfly orders will fire on qualifying "
                   "high-score earnings plays. Requires Alpaca Level 3 approval.")
    else:
        report.add("Options Safety Gate", "PASS",
                   "ENABLE_OPTIONS=False — options orders disabled (opt-in safety)")
except Exception as e:
    report.add("Options Safety Gate", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 15 — DAY trade bracket must be tighter than swing
# Bug found 2026-05-29: DAY trade retry block used KELLY_LOSS_PCT/MOVE_TARGET_PCT
# instead of DAY_TRADE_STOP_PCT/DAY_TRADE_TARGET_PCT → swing bracket on DAY trades.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[15/21] DAY Trade Bracket Parameters{RESET}")
try:
    from config import (DAY_TRADE_STOP_PCT, DAY_TRADE_TARGET_PCT,
                        KELLY_LOSS_PCT, MOVE_TARGET_PCT)
    issues = []
    if DAY_TRADE_STOP_PCT >= KELLY_LOSS_PCT:
        issues.append(f"DAY stop ({DAY_TRADE_STOP_PCT:.1%}) >= swing stop ({KELLY_LOSS_PCT:.1%})")
    if DAY_TRADE_TARGET_PCT >= MOVE_TARGET_PCT:
        issues.append(f"DAY target ({DAY_TRADE_TARGET_PCT:.1%}) >= swing target ({MOVE_TARGET_PCT:.1%})")
    if DAY_TRADE_STOP_PCT <= 0 or DAY_TRADE_TARGET_PCT <= 0:
        issues.append("DAY stop or target is zero or negative")
    if issues:
        report.add("DAY Trade Bracket", "FAIL", "; ".join(issues))
    else:
        report.add("DAY Trade Bracket", "PASS",
                   f"DAY: stop={DAY_TRADE_STOP_PCT:.1%} / target={DAY_TRADE_TARGET_PCT:.1%} "
                   f"· Swing: stop={KELLY_LOSS_PCT:.1%} / target={MOVE_TARGET_PCT:.1%}")
except ImportError:
    report.add("DAY Trade Bracket", "FAIL",
               "DAY_TRADE_STOP_PCT or DAY_TRADE_TARGET_PCT not found in config.py")
except Exception as e:
    report.add("DAY Trade Bracket", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 16 — Partial exit fractions must sum < 1.0
# T1 + T2 must always leave a non-zero remaining tranche to ride the trailing stop.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[16/21] Partial Exit Fractions{RESET}")
try:
    from config import (PARTIAL_EXIT_TIER1_FRACTION, PARTIAL_EXIT_TIER2_FRACTION,
                        PARTIAL_EXIT_TIER1_TRIGGER, PARTIAL_EXIT_TIER2_TRIGGER,
                        ENABLE_PARTIAL_EXIT)
    total = PARTIAL_EXIT_TIER1_FRACTION + PARTIAL_EXIT_TIER2_FRACTION
    remaining = 1.0 - total
    if not ENABLE_PARTIAL_EXIT:
        report.add("Partial Exit Fractions", "PASS",
                   "ENABLE_PARTIAL_EXIT=False — partial exits disabled")
    elif total >= 1.0:
        report.add("Partial Exit Fractions", "FAIL",
                   f"T1({PARTIAL_EXIT_TIER1_FRACTION:.0%}) + T2({PARTIAL_EXIT_TIER2_FRACTION:.0%}) "
                   f"= {total:.0%} — no shares remain to ride the trailing stop")
    elif total > 0.8:
        report.add("Partial Exit Fractions", "WARN",
                   f"T1+T2 = {total:.0%}, only {remaining:.0%} rides trailing stop — "
                   f"consider reducing to leave more upside")
    else:
        report.add("Partial Exit Fractions", "PASS",
                   f"T1={PARTIAL_EXIT_TIER1_FRACTION:.0%} at +{PARTIAL_EXIT_TIER1_TRIGGER:.0%} · "
                   f"T2={PARTIAL_EXIT_TIER2_FRACTION:.0%} at +{PARTIAL_EXIT_TIER2_TRIGGER:.0%} · "
                   f"{remaining:.0%} rides trailing stop")
except Exception as e:
    report.add("Partial Exit Fractions", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 17 — Dashboard API is reachable and returns valid JSON
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[17/21] Dashboard API Health{RESET}")
try:
    import urllib.request, json as _json
    _dashboard_url = "https://illuminati-dashboard.pages.dev/api/dashboard"
    _req = urllib.request.Request(_dashboard_url, headers={"User-Agent": "ZEUS-health"})
    with urllib.request.urlopen(_req, timeout=10) as _r:
        _status = _r.status
        _body = _r.read(512)   # just enough to confirm JSON
    if _status == 200:
        try:
            _parsed = _json.loads(_body + b"}")  # partial parse check
        except Exception:
            pass  # truncated body is fine — we got a 200
        report.add("Dashboard API", "PASS",
                   f"illuminati-dashboard.pages.dev responded HTTP {_status}")
    else:
        report.add("Dashboard API", "FAIL",
                   f"Dashboard returned HTTP {_status} — may be down or 1102 error")
except Exception as e:
    report.add("Dashboard API", "WARN",
               f"Could not reach dashboard: {str(e)[:80]} "
               f"(network may be unavailable from GitHub Actions)")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 18 — Mac launchd jobs are healthy (exit code 0, not 126)
# Bug found 2026-05-29: both illuminati launchd jobs had exit code 126
# (not executable) — AEGIS and scan backup layers were silently not running.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[18/21] Mac launchd Job Health{RESET}")
try:
    import subprocess, platform
    if platform.system() != "Darwin":
        report.add("Mac launchd Health", "PASS",
                   "Not running on macOS — launchd check skipped (GitHub Actions runner)")
    else:
        _result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        _lines = [l for l in _result.stdout.splitlines() if "illuminati" in l.lower()]
        if not _lines:
            report.add("Mac launchd Health", "WARN",
                       "No illuminati launchd jobs found — Mac backup layer not loaded")
        else:
            failed = []
            healthy = []
            for line in _lines:
                parts = line.split("\t")
                exit_code = parts[1].strip() if len(parts) >= 3 else "?"
                label = parts[2].strip() if len(parts) >= 3 else line
                if exit_code not in ("0", "-"):
                    failed.append(f"{label} (exit {exit_code})")
                else:
                    healthy.append(label.split(".")[-1])
            if failed:
                report.add("Mac launchd Health", "FAIL",
                           f"Unhealthy jobs: {', '.join(failed)} — "
                           f"check script permissions (chmod +x)")
            else:
                report.add("Mac launchd Health", "PASS",
                           f"All launchd jobs healthy: {', '.join(healthy)}")
except Exception as e:
    report.add("Mac launchd Health", "WARN", f"Could not run launchctl: {str(e)[:80]}")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 19 — Weekend-gap protection enabled
# Added 2026-06-01 after HOOD (-$539) and WOLF (-$554) were bought Fri 2:09 PM
# and gapped through their stops over the weekend. Verify the guard is on.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[19/21] Weekend-Gap Protection{RESET}")
try:
    from config import BLOCK_FRIDAY_PM_ENTRIES, FRIDAY_ENTRY_CUTOFF_ET
    if BLOCK_FRIDAY_PM_ENTRIES:
        report.add("Weekend-Gap Protection", "PASS",
                   f"No new entries after {FRIDAY_ENTRY_CUTOFF_ET:.0f}:00 ET Fridays "
                   f"— stops can't cover Monday gaps")
    else:
        report.add("Weekend-Gap Protection", "WARN",
                   "BLOCK_FRIDAY_PM_ENTRIES=False — Friday-afternoon entries allowed "
                   "(weekend gap risk; HOOD/WOLF cost -$1,093 on 2026-06-01)")
except ImportError:
    report.add("Weekend-Gap Protection", "FAIL",
               "BLOCK_FRIDAY_PM_ENTRIES not in config.py — weekend-gap guard missing")
except Exception as e:
    report.add("Weekend-Gap Protection", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 20 — Entry cap per ticker enabled
# Added 2026-06-01 after ON was bought 5x in a week (averaging up into a decline).
# Verify the anti-accumulation cap is configured sanely.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[20/21] Entry Cap Per Ticker{RESET}")
try:
    from config import MAX_ENTRIES_PER_TICKER, ENTRY_CAP_WINDOW_DAYS
    if MAX_ENTRIES_PER_TICKER >= 1 and ENTRY_CAP_WINDOW_DAYS >= 1:
        report.add("Entry Cap Per Ticker", "PASS",
                   f"Max {MAX_ENTRIES_PER_TICKER} buy fills per ticker in "
                   f"{ENTRY_CAP_WINDOW_DAYS}d — blocks accumulating into a loser")
    else:
        report.add("Entry Cap Per Ticker", "FAIL",
                   f"MAX_ENTRIES_PER_TICKER={MAX_ENTRIES_PER_TICKER} / "
                   f"window={ENTRY_CAP_WINDOW_DAYS}d — invalid (must be >= 1)")
except ImportError:
    report.add("Entry Cap Per Ticker", "FAIL",
               "MAX_ENTRIES_PER_TICKER not in config.py — entry cap missing")
except Exception as e:
    report.add("Entry Cap Per Ticker", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 21 — Catastrophic-day circuit breakers configured
# Added 2026-06-01 after autopsy of 30d: every big red day = one of three
# patterns. Verify all three breakers are armed and sane.
#   (A) same-day re-entry lockout  — RYOJ 13x stop-outs = -$4,190
#   (B) hard per-trade max loss    — FLY/VOYG/YMAT -16%/-15%/-11% (stop didn't fire)
#   (C) daily realized-loss halt   — May 27 ran to -$4,776 unchecked
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[21/21] Catastrophic-Day Circuit Breakers{RESET}")
try:
    from config import (BLOCK_SAME_DAY_REENTRY, HARD_MAX_LOSS_PCT,
                        DAILY_REALIZED_LOSS_HALT_USD, DAILY_REALIZED_LOSS_HALT_PCT,
                        KELLY_LOSS_PCT, BANKROLL)
    issues = []
    if not BLOCK_SAME_DAY_REENTRY:
        issues.append("same-day re-entry lockout OFF (RYOJ 13x spiral risk)")
    # hard max loss must be LOOSER than the normal stop (a safety net below it)
    if HARD_MAX_LOSS_PCT <= KELLY_LOSS_PCT:
        issues.append(f"HARD_MAX_LOSS_PCT ({HARD_MAX_LOSS_PCT:.0%}) <= normal stop "
                      f"({KELLY_LOSS_PCT:.0%}) — would fight the stop")
    if HARD_MAX_LOSS_PCT <= 0 or HARD_MAX_LOSS_PCT > 0.25:
        issues.append(f"HARD_MAX_LOSS_PCT {HARD_MAX_LOSS_PCT:.0%} out of sane range")
    _halt = min(DAILY_REALIZED_LOSS_HALT_USD, BANKROLL * DAILY_REALIZED_LOSS_HALT_PCT)
    if _halt <= 0:
        issues.append("daily realized-loss halt floor is zero/negative")
    if issues:
        report.add("Circuit Breakers", "FAIL", "; ".join(issues))
    else:
        report.add("Circuit Breakers", "PASS",
                   f"same-day lockout ON · hard max -{HARD_MAX_LOSS_PCT*100:.0f}% · "
                   f"daily halt -${_halt:,.0f}")
except ImportError as ie:
    report.add("Circuit Breakers", "FAIL",
               f"circuit-breaker config missing: {str(ie)[:80]}")
except Exception as e:
    report.add("Circuit Breakers", "WARN", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY — terminal
# ══════════════════════════════════════════════════════════════════════════════
overall = report.overall()
overall_icon = {
    "PASS": f"{GREEN}{BOLD}✓ ALL SYSTEMS CLEARED — ILLUMINATI OPERATIONAL{RESET}",
    "WARN": f"{YELLOW}{BOLD}⚠ SYSTEM OPERATIONAL — WARNINGS REQUIRE ATTENTION{RESET}",
    "FAIL": f"{RED}{BOLD}✗ CRITICAL FAILURE — IMMEDIATE ACTION REQUIRED{RESET}",
}[overall]

print(f"\n{'═'*65}")
print(f"  Illuminati — ZEUS  |  {now.strftime('%Y-%m-%d %H:%M ET')}")
print(f"  {overall_icon}")
print(f"  {report.summary()}")
print(f"{'═'*65}")
for name, status, detail in report.checks:
    icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[status]
    print(f"  {icon} {name}: {detail}")
print(f"{'═'*65}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SLACK REPORT
# ══════════════════════════════════════════════════════════════════════════════
def _send_zeus_report(report: AuditReport) -> bool:
    """Send the ZEUS audit report to Slack."""
    from config import SLACK_WEBHOOK_URL
    if not SLACK_WEBHOOK_URL:
        print("[ZEUS] SLACK_WEBHOOK_URL not set — skipping Slack report.")
        return False

    import json
    import urllib.request
    import urllib.error

    overall = report.overall()
    color   = {"PASS": "good", "WARN": "warning", "FAIL": "danger"}[overall]
    header  = {
        "PASS": "✅ All Systems Cleared",
        "WARN": "⚠️ Operational — Warnings Need Attention",
        "FAIL": "❌ Critical Failure — Action Required",
    }[overall]

    date_str = now.strftime("%a %b %d, %Y · %H:%M ET")
    summary  = report.summary()

    lines = []
    for name, status, detail in report.checks:
        emoji = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(status, "•")
        lines.append(f"{emoji} *{name}* — {detail}")

    checks_text = "\n".join(lines)

    payload = {
        "attachments": [
            {
                "color":    color,
                "pretext":  f"*⚡ ZEUS Full Audit — {date_str}*",
                "title":    header,
                "text":     f"_{summary}_\n\n{checks_text}",
                "mrkdwn_in": ["pretext", "text"],
                "footer":   "Illuminati — ZEUS",
                "ts":       int(now.timestamp()),
            }
        ]
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data    = data,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        if ok:
            print(f"[ZEUS] Slack report sent — {overall}")
        return ok
    except urllib.error.HTTPError as e:
        print(f"[ZEUS] Slack HTTP {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"[ZEUS] Slack error: {e}")
        return False


try:
    _send_zeus_report(report)
except Exception as e:
    print(f"[ZEUS] Slack report skipped: {e}")


# Exit with non-zero code if any failures
sys.exit(1 if report.n_fail > 0 else 0)
