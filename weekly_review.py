"""
Weekly Review — the discipline report (TRADING_PRINCIPLES.md #4)

Runs Sundays. Judges the system on the DISTRIBUTION, not any single day:
  win rate · avg win vs avg loss · profit factor · expectancy · rule violations

Why: a 60% win-rate system has losing streaks by design. The danger is
reacting to a bad day — revenge trading, sizing up, overriding guards. Seeing
the rolling distribution every week is what keeps you trusting the rules.

Posts a single rich Slack card. Read-only — never places or cancels orders.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

LOOKBACK_DAYS = 30   # rolling window for the distribution


def _classify_violations(closed: list[dict]) -> list[str]:
    """Flag trades that broke a TRADING_PRINCIPLES rule, for awareness."""
    violations = []
    # Rule: never average up into a loser — >2 entries per ticker in the window.
    from collections import Counter
    entry_counts = Counter(t["ticker"] for t in closed)
    for tk, n in entry_counts.items():
        if n > 3:   # closed trades; >3 closes on one ticker hints at churn
            violations.append(f"{tk}: {n} closes in window — possible over-trading")
    return violations


def build_review() -> dict:
    from execution.alpaca import get_closed_trade_pnl, is_live_mode
    closed = get_closed_trade_pnl(days=LOOKBACK_DAYS)
    if not closed:
        return {"empty": True}

    wins   = [t for t in closed if t.get("realized_pnl", 0) > 0]
    losses = [t for t in closed if t.get("realized_pnl", 0) < 0]
    n      = len(closed)
    n_w, n_l = len(wins), len(losses)

    gross_win  = sum(t["realized_pnl"] for t in wins)
    gross_loss = abs(sum(t["realized_pnl"] for t in losses))
    net        = gross_win - gross_loss

    win_rate     = (n_w / n * 100) if n else 0
    avg_win      = (gross_win / n_w) if n_w else 0
    avg_loss     = (gross_loss / n_l) if n_l else 0
    profit_factor = (gross_win / gross_loss) if gross_loss else float("inf")
    # Expectancy per trade = (win% * avg_win) − (loss% * avg_loss)
    expectancy = ((n_w / n) * avg_win - (n_l / n) * avg_loss) if n else 0
    win_loss_ratio = (avg_win / avg_loss) if avg_loss else float("inf")

    best  = max(closed, key=lambda t: t.get("realized_pnl", 0))
    worst = min(closed, key=lambda t: t.get("realized_pnl", 0))

    return {
        "empty": False,
        "mode": "LIVE" if is_live_mode() else "PAPER",
        "n": n, "n_w": n_w, "n_l": n_l,
        "win_rate": win_rate, "net": net,
        "gross_win": gross_win, "gross_loss": gross_loss,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": profit_factor, "win_loss_ratio": win_loss_ratio,
        "expectancy": expectancy,
        "best": best, "worst": worst,
        "violations": _classify_violations(closed),
    }


def post_review(r: dict) -> bool:
    from config import SLACK_WEBHOOK_URL
    if not SLACK_WEBHOOK_URL:
        print("[weekly] SLACK_WEBHOOK_URL not set — printing instead.")
        print(r)
        return False

    import json, urllib.request
    now = datetime.now(timezone.utc)

    if r.get("empty"):
        text = f"*📊 Weekly Review* — no closed trades in the last {LOOKBACK_DAYS} days."
        payload = {"text": text}
    else:
        pf = ("∞" if r["profit_factor"] == float("inf")
              else f"{r['profit_factor']:.2f}×")
        wlr = ("∞" if r["win_loss_ratio"] == float("inf")
               else f"{r['win_loss_ratio']:.2f}×")
        # Health read on the distribution (principle #4 + #5)
        health = []
        if r["win_rate"] >= 55: health.append("✅ win rate ≥ 55%")
        else: health.append(f"⚠️ win rate {r['win_rate']:.0f}% (target ≥55%)")
        if r["profit_factor"] != float("inf") and r["profit_factor"] >= 1.5:
            health.append("✅ profit factor ≥ 1.5×")
        elif r["profit_factor"] != float("inf"):
            health.append(f"⚠️ profit factor {pf} (target ≥1.5×)")
        if r["expectancy"] > 0: health.append("✅ positive expectancy")
        else: health.append("⚠️ negative expectancy")

        lines = [
            f"*Last {LOOKBACK_DAYS} days · {r['mode']}* — judge the distribution, not any one day.",
            "",
            f"• *Trades:* {r['n']}  ({r['n_w']}W / {r['n_l']}L · *{r['win_rate']:.0f}%* win rate)",
            f"• *Net P&L:* ${r['net']:+,.2f}   (wins ${r['gross_win']:+,.0f} · losses ${-r['gross_loss']:,.0f})",
            f"• *Avg win / avg loss:* ${r['avg_win']:,.0f} vs ${r['avg_loss']:,.0f}  (*{wlr}* ratio)",
            f"• *Profit factor:* {pf}   ·   *Expectancy/trade:* ${r['expectancy']:+,.0f}",
            f"• *Best:* {r['best']['ticker']} ${r['best']['realized_pnl']:+,.0f}   ·   "
            f"*Worst:* {r['worst']['ticker']} ${r['worst']['realized_pnl']:+,.0f}",
            "",
            "*Health:* " + " · ".join(health),
        ]
        if r["violations"]:
            lines.append("")
            lines.append("*⚠️ Watch:* " + "; ".join(r["violations"]))
        lines.append("")
        lines.append("_Reminder: lose small, let winners run. Don't override the "
                     "guards on a red day. (see TRADING_PRINCIPLES.md)_")

        color = "good" if (r["win_rate"] >= 55 and r["expectancy"] > 0) else "warning"
        payload = {
            "attachments": [{
                "color": color,
                "pretext": f"*📊 Weekly Review — {now.strftime('%a %b %d, %Y')}*",
                "text": "\n".join(lines),
                "mrkdwn_in": ["pretext", "text"],
                "footer": "Illuminati — Weekly Discipline Review",
                "ts": int(now.timestamp()),
            }]
        }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(SLACK_WEBHOOK_URL, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        print(f"[weekly] Slack review sent: {ok}")
        return ok
    except Exception as e:
        print(f"[weekly] Slack error: {e}")
        return False


if __name__ == "__main__":
    review = build_review()
    post_review(review)
    sys.exit(0)
