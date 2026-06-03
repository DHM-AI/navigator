from __future__ import annotations
"""
Slack Webhook alert module — replaces Gmail SMTP.

No server, no credentials setup, no spam filters. Just a webhook URL.

Setup (one-time, ~5 minutes):
  1. https://api.slack.com/apps → "Create New App" → "From scratch"
  2. Give it a name (e.g. "Illuminati") → pick your workspace
  3. Left sidebar → "Incoming Webhooks" → toggle On
  4. "Add New Webhook to Workspace" → pick a channel (e.g. #trading or #alerts)
  5. Copy the Webhook URL (starts with https://hooks.slack.com/services/...)
  6. Add to .env:   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
  7. Add to GitHub Actions secrets (same key name)

Public functions:
  send_daily_digest(picks_df, explanations)  – top trade picks after each scan
  send_health_report(report)                  – daily health check summary
  send_trade_alert(result)                    – fires immediately when a bracket order is placed
"""
import json
import urllib.request
import urllib.error
from datetime import datetime
from config import SLACK_WEBHOOK_URL


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post(payload: dict) -> bool:
    """POST a JSON payload to the configured Slack webhook. Returns True on success."""
    if not SLACK_WEBHOOK_URL:
        print("[slack] SLACK_WEBHOOK_URL not set — skipping notification.")
        return False
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data    = data,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"[slack] HTTP {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"[slack] Error: {e}")
        return False


def _direction_emoji(direction: str) -> str:
    return {"bullish": "📈", "bearish": "📉"}.get(direction.lower(), "➡️")


def _confidence_emoji(conf: str) -> str:
    return {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(conf, "⚪")


def _status_color(status: str) -> str:
    return {"PASS": "good", "WARN": "warning", "FAIL": "danger"}.get(status, "#888")


def _status_emoji(status: str) -> str:
    return {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(status, "•")


# ── Daily Trade Digest ────────────────────────────────────────────────────────

def send_daily_digest(picks_df, explanations: dict) -> bool:
    """
    Send the top trade picks after each market scan.
    Sends one rich attachment per pick (max 10) with color-coded direction.
    """
    if picks_df is None or picks_df.empty:
        return _post({
            "text": "📊 *Market Scan complete* — no setups above threshold today.",
        })

    date_str = datetime.today().strftime("%b %d, %Y")
    n        = len(picks_df)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Market Scan — {date_str}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{n} setup{'s' if n != 1 else ''} flagged* · Score ≥ 50 · Top {min(n, 10)} shown",
            },
        },
        {"type": "divider"},
    ]

    attachments = []
    for _, row in picks_df.head(10).iterrows():
        ticker     = row.get("ticker", "?")
        score      = row.get("score", 0)
        direction  = row.get("direction", "—")
        confidence = row.get("confidence", "—")
        duration   = row.get("duration", "—")
        dollar     = row.get("dollar_amount", 0)
        pct        = row.get("pct_of_bankroll", 0)
        risk_lvl   = row.get("risk_level", "—")
        explanation = explanations.get(ticker, "")[:300]

        signals = row.get("signals_triggered", []) or []
        if isinstance(signals, str):
            signals = [s.strip() for s in signals.replace("[","").replace("]","").replace('"',"").split(";") if s.strip()] or [signals]
        sig_str = " · ".join(signals) if signals else "—"

        dir_emoji  = _direction_emoji(direction)
        conf_emoji = _confidence_emoji(confidence)
        color      = "#00d084" if direction == "bullish" else "#ff2d78"

        fields = [
            {"title": "Score",      "value": f"{score:.0f}/100",          "short": True},
            {"title": "Direction",  "value": f"{dir_emoji} {direction.upper()}", "short": True},
            {"title": "Confidence", "value": f"{conf_emoji} {confidence}", "short": True},
            {"title": "Window",     "value": duration,                     "short": True},
        ]

        if dollar > 0:
            fields.append({"title": "Position Size",
                           "value": f"${dollar:,.0f} ({pct:.1f}% bankroll · {risk_lvl})",
                           "short": False})

        if sig_str != "—":
            fields.append({"title": "Signals", "value": sig_str, "short": False})

        if explanation:
            fields.append({"title": "Analysis", "value": explanation, "short": False})

        attachments.append({
            "color":       color,
            "title":       ticker,
            "title_link":  f"https://finance.yahoo.com/quote/{ticker}",
            "fields":      fields,
            "footer":      "Illuminati",
            "ts":          int(datetime.now().timestamp()),
        })

    payload = {"blocks": blocks, "attachments": attachments}
    ok = _post(payload)
    if ok:
        print(f"[slack] Digest sent — {n} picks")
    return ok


# ── Health Check Report ───────────────────────────────────────────────────────

def send_health_report(report) -> bool:
    """
    Send the daily health check summary.
    report: HealthReport object with .checks, .n_pass, .n_warn, .n_fail, .overall(), .summary()
    """
    overall = report.overall()
    color   = {"PASS": "good", "WARN": "warning", "FAIL": "danger"}[overall]
    header  = {
        "PASS": "✅ All Systems Operational",
        "WARN": "⚠️ Operational with Warnings",
        "FAIL": "❌ System Failure Detected",
    }[overall]

    date_str = datetime.now().strftime("%a %b %d, %Y · %H:%M ET")
    summary  = report.summary()

    # Build check list as compact text
    lines = []
    for name, status, detail in report.checks:
        icon = _status_emoji(status)
        lines.append(f"{icon} *{name}* — {detail}")

    checks_text = "\n".join(lines)

    payload = {
        "attachments": [
            {
                "color":    color,
                "pretext":  f"*🩺 Health Check — {date_str}*",
                "title":    header,
                "text":     f"_{summary}_\n\n{checks_text}",
                "mrkdwn_in": ["pretext", "text"],
                "footer":   "Illuminati · PULSE",
                "ts":       int(datetime.now().timestamp()),
            }
        ]
    }

    ok = _post(payload)
    if ok:
        print(f"[slack] Health report sent — {overall}")
    return ok


# ── Individual Trade Alert ────────────────────────────────────────────────────

def send_trade_alert(result: dict) -> bool:
    """
    Fire immediately when a bracket order is placed.
    result: dict returned by execution.alpaca.place_order()
    """
    if not result or result.get("status") not in ("submitted", "error", "halted"):
        return False

    status = result.get("status", "unknown")
    ticker = result.get("ticker", "?")
    side   = result.get("side", "?").upper()
    mode   = result.get("mode", "PAPER")
    qty    = result.get("qty", 0)
    entry  = result.get("entry_price")
    sl     = result.get("stop_loss")
    tp     = result.get("take_profit")
    dollar = result.get("dollar_amount", 0)
    reason = result.get("reason", "")

    if status == "halted":
        return _post({"text": f"🛑 *Trading HALTED — daily loss limit hit*\n>{result.get('reason','')}"})

    if status == "error":
        payload = {
            "text": f"❌ *Trade FAILED — {ticker}*\n>{result.get('reason', 'unknown error')}"
        }
        return _post(payload)

    mode_tag  = "🔴 LIVE" if "LIVE" in mode else "📄 PAPER"
    dir_emoji = "📈" if side == "BUY" else "📉"
    color     = "#00d084" if side == "BUY" else "#ff2d78"

    fields = [
        {"title": "Side",    "value": f"{dir_emoji} {side}",             "short": True},
        {"title": "Mode",    "value": mode_tag,                          "short": True},
        {"title": "Qty",     "value": f"{qty} shares",                   "short": True},
        {"title": "Value",   "value": f"${dollar:,.2f}",                 "short": True},
    ]
    if entry:
        fields.append({"title": "Entry",       "value": f"${entry:.2f}",  "short": True})
    if sl:
        fields.append({"title": "Stop Loss",   "value": f"${sl:.2f} 🛑",  "short": True})
    if tp:
        fields.append({"title": "Take Profit", "value": f"${tp:.2f} 🎯",  "short": True})
    if reason:
        fields.append({"title": "Reason",      "value": reason[:200],     "short": False})

    order_id = result.get("order_id", "")
    footer   = f"Order {order_id}" if order_id else "Illuminati"

    payload = {
        "attachments": [
            {
                "color":     color,
                "pretext":   f"*🤖 Bracket Order Placed*",
                "title":     f"{ticker} — {side}",
                "title_link": f"https://finance.yahoo.com/quote/{ticker}",
                "fields":    fields,
                "footer":    footer,
                "ts":        int(datetime.now().timestamp()),
                "mrkdwn_in": ["pretext"],
            }
        ]
    }

    ok = _post(payload)
    if ok:
        print(f"[slack] Trade alert sent — {ticker} {side} ${dollar:.0f}")
    return ok


# ── Quick test helper ─────────────────────────────────────────────────────────

def send_test_message() -> bool:
    """Send a test ping to verify the webhook is working."""
    return _post({
        "text": (
            "🤖 *Illuminati — Slack webhook connected!*\n"
            "Agents online:\n"
            "• ARGUS (scan) · CIPHER (research) · PYTHIA (predict)\n"
            "• THEMIS (risk) · APEX (execution) · VIGIL (sentiment guard)\n"
            "• AEGIS (trail stops) · DUSK (intraday close) · ORACLE (learning)\n"
            "• CHRONICLE (EOD report) · PULSE (health check) · GENESIS (monthly retrain)\n\n"
            "You'll receive:\n"
            "• 📊 Trade picks after each of the 7 daily scans\n"
            "• 🩺 PULSE health check reports (weekdays 5PM + Sunday 11AM ET)\n"
            "• 🔔 Instant alerts when APEX places a bracket order"
        )
    })
