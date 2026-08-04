"""
Health Check Agent — runs daily to verify every system component is working.

Checks:
  1. Supabase connection + today's predictions written
  2. Alpaca connection + account health + daily loss limit
  3. XGBoost model file exists and loads correctly
  4. All required environment variables are configured
  5. Latest GitHub Actions scan ran today (via predictions timestamp)
  6. Open positions have valid stop loss / take profit orders
  7. Monthly goal progress

Sends a color-coded email report with PASS / WARN / FAIL per check.
Exit code 0 = all good, 1 = at least one FAIL.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# Anchor to script directory so relative paths (MODEL_PATH, .env) resolve correctly
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


class HealthReport:
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
        print(f"  {icon}  {name}: {detail}")

    def overall(self) -> str:
        if self.n_fail > 0:  return "FAIL"
        if self.n_warn > 0:  return "WARN"
        return "PASS"

    def summary(self) -> str:
        elapsed = (datetime.now() - self.started).seconds
        return (f"{self.n_pass} passed · {self.n_warn} warnings · "
                f"{self.n_fail} failures · {elapsed}s")


report = HealthReport()
today  = datetime.today().strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — Environment variables
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[1/9] Environment Variables{RESET}")
from dotenv import load_dotenv
load_dotenv(override=True)

REQUIRED_VARS = {
    "ANTHROPIC_API_KEY":  "Claude analysis (non-fatal if missing)",
    "SUPABASE_URL":       "Database persistence — CRITICAL",
    "SUPABASE_KEY":       "Database persistence — CRITICAL",
    "ALPACA_API_KEY":     "Trading execution — CRITICAL",
    "ALPACA_SECRET_KEY":  "Trading execution — CRITICAL",
    "ALPHA_VANTAGE_KEY":  "News sentiment (falls back gracefully)",
    "SLACK_WEBHOOK_URL":  "Slack alerts (non-fatal if missing)",
}
missing_critical = []
missing_optional = []

for var, note in REQUIRED_VARS.items():
    val = os.environ.get(var, "")
    if not val:
        if "CRITICAL" in note:
            missing_critical.append(var)
        else:
            missing_optional.append(var)

if missing_critical:
    report.add("Env Vars", "FAIL",
               f"Missing critical: {', '.join(missing_critical)}")
elif missing_optional:
    report.add("Env Vars", "WARN",
               f"Missing optional: {', '.join(missing_optional)}")
else:
    report.add("Env Vars", "PASS", "All required variables are set")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 2 — Supabase connection + today's predictions
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[2/9] Supabase / Database{RESET}")
db_predictions = []
try:
    import db
    if not db.db_available():
        report.add("Supabase Connection", "FAIL", "SUPABASE_URL or SUPABASE_KEY not set")
    else:
        # Test connection
        db_predictions = db.load_predictions_for_date(today)
        n = len(db_predictions)
        if n == 0:
            # Check if market is open today (weekday)
            if datetime.today().weekday() < 5:
                # H-1 fix: use ET timezone correctly via zoneinfo (DST-aware)
                # instead of hardcoded UTC-4 (which is wrong in winter).
                try:
                    from zoneinfo import ZoneInfo
                    now_et_hour = datetime.now(ZoneInfo("America/New_York")).hour
                except Exception:
                    now_et_hour = (datetime.now(timezone.utc).hour - 4) % 24   # fallback
                if now_et_hour >= 13:
                    report.add("Supabase — Today's Predictions", "WARN",
                               f"No predictions written for {today} after 1 PM ET — scans may be failing")
                else:
                    report.add("Supabase — Today's Predictions", "PASS",
                               f"No predictions yet for {today} (before 1 PM ET — scans still running)")
            else:
                report.add("Supabase — Today's Predictions", "PASS",
                           "Weekend — no predictions expected")
        else:
            report.add("Supabase — Today's Predictions", "PASS",
                       f"{n} predictions written for {today}")

        # Check last prediction date
        all_rows = db.load_predictions()
        if all_rows:
            latest = max(r.get("date","") for r in all_rows)
            days_ago = (datetime.today() - datetime.strptime(latest, "%Y-%m-%d")).days
            if days_ago > 2 and datetime.today().weekday() < 5:
                report.add("Supabase — Data Freshness", "WARN",
                           f"Latest prediction is {days_ago} days old ({latest})")
            else:
                report.add("Supabase — Data Freshness", "PASS",
                           f"Latest prediction: {latest}")
        else:
            report.add("Supabase — Data Freshness", "WARN",
                       "No predictions in database at all — first run?")
except Exception as e:
    report.add("Supabase", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 3 — Alpaca connection + account health
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[3/9] Alpaca Trading{RESET}")
alpaca_acct = {}
alpaca_positions = []
try:
    from execution.alpaca import (is_configured, is_live_mode,
                                   get_account, get_positions)
    from config import BANKROLL, DAILY_LOSS_LIMIT_PCT, MONTHLY_TARGET_PCT

    if not is_configured():
        report.add("Alpaca Connection", "FAIL",
                   "ALPACA_API_KEY or ALPACA_SECRET_KEY not set")
    else:
        alpaca_acct      = get_account()
        alpaca_positions = get_positions()
        portfolio        = alpaca_acct.get("portfolio_value", 0)
        buying_power     = alpaca_acct.get("buying_power", 0)
        mode             = "LIVE 🔴" if is_live_mode() else "PAPER 📄"

        report.add("Alpaca Connection", "PASS",
                   f"{mode} · Portfolio ${portfolio:,.2f} · "
                   f"Buying power ${buying_power:,.2f}")

        # Daily loss check — compare today's equity to yesterday's close
        last_equity      = alpaca_acct.get("last_equity", portfolio)
        daily_change_pct = (portfolio - last_equity) / last_equity if last_equity else 0
        if daily_change_pct < -DAILY_LOSS_LIMIT_PCT:
            report.add("Alpaca — Daily Loss Limit", "FAIL",
                       f"Down {abs(daily_change_pct):.1%} — exceeds {DAILY_LOSS_LIMIT_PCT:.0%} limit. Trading halted.")
        elif daily_change_pct < -(DAILY_LOSS_LIMIT_PCT * 0.5):
            report.add("Alpaca — Daily Loss Limit", "WARN",
                       f"Down {abs(daily_change_pct):.1%} today — approaching {DAILY_LOSS_LIMIT_PCT:.0%} limit")
        else:
            report.add("Alpaca — Daily Loss Limit", "PASS",
                       f"Daily P&L: {daily_change_pct:+.2%} — within limits")

        # Open positions
        n_pos = len(alpaca_positions)
        if n_pos > 0:
            total_pl = sum(p.get("unrealized_pl", 0) for p in alpaca_positions)
            report.add("Alpaca — Open Positions", "PASS",
                       f"{n_pos} position(s) · Unrealized P&L: ${total_pl:+,.2f}")
        else:
            report.add("Alpaca — Open Positions", "PASS", "No open positions")

        # ── AEGIS watchdog — every open position MUST have a stop ─────────
        # (queries ALL active orders, not just OPEN — bracket children live in HELD)
        from alpaca.trading.requests import GetOrdersRequest as _GOR
        from alpaca.trading.enums import QueryOrderStatus as _QOS
        from execution.alpaca import _get_client
        from execution.alpaca import is_active_order
        _client_inst = _get_client()
        _all_ord = _client_inst.get_orders(_GOR(status=_QOS.ALL, limit=400))
        _stopped = {o.symbol for o in _all_ord
                    if is_active_order(o)
                    and "stop" in str(getattr(o,"type","")).lower()}
        # alpaca_positions is already a list of dicts from get_positions();
        # use the 'ticker' key (not pos.symbol on a dict)
        naked = [p.get("ticker") or p.get("symbol") for p in alpaca_positions
                 if (p.get("ticker") or p.get("symbol")) not in _stopped]
        if naked:
            report.add("AEGIS — Stop Coverage", "FAIL",
                       f"{len(naked)} position(s) WITHOUT stops: {', '.join(naked[:10])}")
            # Auto-fire AEGIS to fix it immediately
            try:
                from execution.alpaca import trail_positions
                fixed = trail_positions()
                report.add("AEGIS — Auto-Recovery", "WARN" if fixed else "FAIL",
                           f"Triggered AEGIS, placed/upgraded {len(fixed)} stop(s)")
            except Exception as _re:
                report.add("AEGIS — Auto-Recovery", "FAIL", f"Failed: {str(_re)[:80]}")
        else:
            report.add("AEGIS — Stop Coverage", "PASS",
                       f"All {n_pos} position(s) protected")

except Exception as e:
    report.add("Alpaca", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 4 — XGBoost model
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[4/9] XGBoost Model{RESET}")
try:
    from config import MODEL_PATH, FEATURE_NAMES_PATH
    import os

    if not os.path.exists(MODEL_PATH):
        report.add("XGBoost Model File", "FAIL",
                   f"Model not found at {MODEL_PATH} — run: python -m model.trainer")
    else:
        model_age_days = (datetime.now().timestamp() -
                          os.path.getmtime(MODEL_PATH)) / 86400
        import pickle
        with open(MODEL_PATH, "rb") as f:
            model = pickle.load(f)

        if not os.path.exists(FEATURE_NAMES_PATH):
            report.add("XGBoost Feature Names", "WARN",
                       "feature_names.json missing — predictions may break")
        else:
            import json
            with open(FEATURE_NAMES_PATH) as f:
                feats = json.load(f)
            age_str = f"{model_age_days:.0f}d old"
            if model_age_days > 30:
                report.add("XGBoost Model", "WARN",
                           f"Model is {age_str} — consider retraining monthly")
            else:
                report.add("XGBoost Model", "PASS",
                           f"Loads OK · {len(feats)} features · {age_str}")

except Exception as e:
    report.add("XGBoost Model", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 5 — Data pipeline (universe + OHLCV sample)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[5/9] Data Pipeline{RESET}")
try:
    from data.universe import get_universe
    tickers = get_universe()
    if len(tickers) < 400:
        report.add("Universe", "WARN",
                   f"Only {len(tickers)} tickers — expected 500+. Wikipedia may be down.")
    else:
        report.add("Universe", "PASS", f"{len(tickers)} tickers loaded")

    # Quick OHLCV test on one ticker
    from data.fetcher import get_ohlcv
    df = get_ohlcv("AAPL", period="5d")
    if df.empty:
        report.add("OHLCV Fetch (AAPL)", "FAIL",
                   "yfinance returned empty data — check network / API limits")
    else:
        latest_date = str(df.index[-1])[:10]
        report.add("OHLCV Fetch (AAPL)", "PASS",
                   f"{len(df)} rows · latest: {latest_date}")

except Exception as e:
    report.add("Data Pipeline", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 6 — Scoring + Kelly pipeline
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[6/9] Scoring & Kelly Pipeline{RESET}")
try:
    from signals.scorer import score_ticker
    from signals.kelly  import position_size
    from data.fetcher   import get_ohlcv

    df_test   = get_ohlcv("AAPL", period="1y")
    sentiment = {"score": 0.1, "velocity": 0.05, "spike": False}
    result    = score_ticker("AAPL", df_test, sentiment, earnings_days=None)
    score     = result.get("score", 0)

    sizing = position_size(win_prob=0.60)
    dollar = sizing.get("dollar_amount", 0)

    if score < 0 or score > 100:
        report.add("Scorer", "FAIL", f"Score out of range: {score}")
    else:
        report.add("Scorer", "PASS",
                   f"AAPL test score: {score:.0f}/100 · "
                   f"direction: {result.get('direction')} · "
                   f"confidence: {result.get('confidence')}")

    if dollar <= 0:
        report.add("Kelly Sizer", "FAIL", "Returned $0 position size")
    else:
        report.add("Kelly Sizer", "PASS",
                   f"60% win prob → ${dollar:,.0f} position "
                   f"({sizing.get('pct_of_bankroll',0):.1f}% bankroll)")

except Exception as e:
    report.add("Scoring & Kelly", "FAIL", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 7 — Monthly goal progress
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[7/9] Monthly Goal Progress{RESET}")
try:
    from calendar import monthrange
    from config import BANKROLL, MONTHLY_TARGET_PCT

    today_dt      = datetime.today()
    days_in_month = monthrange(today_dt.year, today_dt.month)[1]
    day_of_month  = today_dt.day
    portfolio_val  = alpaca_acct.get("portfolio_value", BANKROLL)
    last_equity    = alpaca_acct.get("last_equity", portfolio_val)
    target         = portfolio_val * MONTHLY_TARGET_PCT   # 10% of current account value
    # Use today's P&L (equity delta + unrealized open) as a proxy for MTD progress
    # since Alpaca paper doesn't expose a start-of-month snapshot
    unrealized_pl  = sum(p.get("unrealized_pl", 0) for p in alpaca_positions)
    today_delta    = portfolio_val - last_equity
    total_pl_today = today_delta  # realized + unrealized move today

    # Progress estimate
    progress_pct  = (total_pl_today / target * 100) if target else 0
    pace_needed   = target * (day_of_month / days_in_month)

    pace_label = "ahead of pace" if total_pl_today >= pace_needed else f"behind pace (need ${pace_needed:,.0f} by day {day_of_month})"
    report.add("Monthly Goal (10%)", "PASS",
               f"Today ${total_pl_today:+,.0f} · target ${target:,.0f} · "
               f"{progress_pct:.1f}% · {pace_label}")

except Exception as e:
    report.add("Monthly Goal", "WARN", str(e)[:100])


# ══════════════════════════════════════════════════════════════════════════════
# 8. DASHBOARD AVAILABILITY
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[8/9] Dashboard API Health{RESET}")
# ⛔⛔ POLARITY INVERTED 2026-08-04 — a 401 here is the CORRECT answer.
# /api/dashboard was gated behind DASHBOARD_TOKEN on 2026-08-03. This check used
# to PASS on `HTTP 200 and '"account"' in body` — i.e. it passed precisely when it
# could read live account state off a public URL, and failed once that stopped
# being possible. It is the same defect ZEUS check 17 had; see zeus.py.
# ⭐ The old success condition is now the alarm: finding "account" in an
#    unauthenticated response means the gate is gone.
# ⛔ Do not "restore" the 200 assertion to make this green.
try:
    import urllib.request, urllib.error
    _DASH_URL = "https://illuminati-dashboard.pages.dev/api/dashboard"
    req = urllib.request.Request(_DASH_URL, headers={"User-Agent": "illuminati-health"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _code = resp.getcode()
            _ctype = resp.headers.get("Content-Type") or ""
            _body = resp.read(200).decode("utf-8", errors="ignore")
            if _code == 200 and _ctype.startswith("text/html"):
                report.add("Dashboard API", "FAIL",
                           "/api/dashboard is NOT ROUTED — 200 text/html SPA fallback "
                           "is answering, the Function is gone")
            elif _code == 200:
                report.add("Dashboard API", "FAIL",
                           "🚨 HTTP 200 UNAUTHENTICATED" +
                           (" with account data in the body" if '"account"' in _body else "") +
                           " — the DASHBOARD_TOKEN gate is GONE and live account state "
                           "is public. Redeploy functions/api/dashboard.js.")
            else:
                report.add("Dashboard API", "FAIL",
                           f"unexpected HTTP {_code} (expected 401 = gated)")
    except urllib.error.HTTPError as he:
        if he.code == 401:
            report.add("Dashboard API", "PASS",
                       "HTTP 401 — Function alive and the DASHBOARD_TOKEN gate is enforced")
        elif he.code == 503:
            report.add("Dashboard API", "FAIL",
                       "HTTP 503 — DASHBOARD_TOKEN is not set on Cloudflare Pages, so "
                       "close-all / close-position are refused too")
        else:
            report.add("Dashboard API", "FAIL",
                       f"HTTP {he.code} — dashboard may be returning 1102/503")
    except urllib.error.URLError as ue:
        report.add("Dashboard API", "WARN",
                   f"Network error, gate unverified: {str(ue)[:80]}")
except Exception as e:
    report.add("Dashboard API", "WARN", f"Check skipped: {str(e)[:80]}")


# ══════════════════════════════════════════════════════════════════════════════
# 9. SELF-HEALING — trigger scan if none ran today (market hours only)
# Runs silently whether health check is triggered at 5 PM or mid-day.
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[9/9] Self-Heal ARGUS{RESET}")
try:
    import urllib.request as _req2
    from datetime import datetime as _dt2

    from zoneinfo import ZoneInfo as _ZI_hc
    _now_et_hour = int(_dt2.now(_ZI_hc("America/New_York")).strftime("%H"))
    _market_open = 9 <= _now_et_hour < 16   # only attempt during market hours

    if db.db_available() and _market_open:
        _today = datetime.today().strftime("%Y-%m-%d")
        _existing = db.load_predictions_for_date(_today)
        if not _existing:
            # No scan today — auto-trigger via GitHub API
            _gh_token = os.getenv("GITHUB_TOKEN", "")
            if _gh_token:
                import urllib.request as _ureq
                import json as _json
                _api_url = "https://api.github.com/repos/DHM-AI/navigator/actions/workflows/daily_scan.yml/dispatches"
                _payload = _json.dumps({"ref": "main"}).encode()
                _gh_req = _ureq.Request(_api_url, data=_payload, headers={
                    "Authorization": f"token {_gh_token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                })
                try:
                    with _ureq.urlopen(_gh_req, timeout=10) as _r:
                        report.add("Self-Heal (ARGUS)", "WARN",
                                   f"No scan today — auto-dispatched via GitHub API (HTTP {_r.status})")
                except Exception as _ge:
                    report.add("Self-Heal (ARGUS)", "FAIL",
                               f"No scan today AND dispatch failed: {str(_ge)[:80]}")
            else:
                report.add("Self-Heal (ARGUS)", "WARN",
                           "No scan today — GITHUB_TOKEN not set, cannot auto-trigger")
        else:
            report.add("Self-Heal (ARGUS)", "PASS",
                       f"Scan confirmed — {len(_existing)} predictions for {_today}")
except Exception as e:
    report.add("Self-Heal (ARGUS)", "WARN", f"Self-heal check skipped: {str(e)[:60]}")


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
overall = report.overall()
overall_icon = {
    "PASS": f"{GREEN}{BOLD}✓ ALL SYSTEMS OPERATIONAL{RESET}",
    "WARN": f"{YELLOW}{BOLD}⚠ OPERATIONAL WITH WARNINGS{RESET}",
    "FAIL": f"{RED}{BOLD}✗ SYSTEM FAILURE DETECTED{RESET}",
}[overall]

print(f"\n{'═'*60}")
print(f"  Illuminati — PULSE  |  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
print(f"  {overall_icon}")
print(f"  {report.summary()}")
print(f"{'═'*60}")
for name, status, detail in report.checks:
    icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[status]
    print(f"  {icon} {name}: {detail}")
print(f"{'═'*60}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SLACK REPORT
# ══════════════════════════════════════════════════════════════════════════════
try:
    from alerts.slack import send_health_report
    send_health_report(report)
except Exception as e:
    print(f"[slack] Report skipped: {e}")


# Exit with non-zero code if any failures
sys.exit(1 if report.n_fail > 0 else 0)
