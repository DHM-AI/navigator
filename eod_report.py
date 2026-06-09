"""
End-of-Day Report — runs at 4:15 PM ET via GitHub Actions.

Sends a Slack summary with:
  - Wins today (closed profitable)
  - Losses today (closed at a loss)
  - Net P&L for the day
  - Monthly goal progress
  - Open positions held overnight
"""
from datetime import datetime, timezone
from execution.alpaca import get_account, get_positions, get_closed_trade_pnl, is_configured
from config import BANKROLL, MONTHLY_TARGET_PCT


def run():
    if not is_configured():
        print("[CHRONICLE] Alpaca not configured.")
        return

    acct      = get_account()
    positions = get_positions()
    closed    = get_closed_trade_pnl(days=1)

    portfolio   = acct["portfolio_value"]
    last_equity = acct["last_equity"]
    unrealized  = sum(p.get("unrealized_pl", 0) for p in positions)
    total_today = portfolio - last_equity
    # AUDIT H-11 (2026-06-09): 'realized' was total_today minus LIFETIME
    # unrealized P&L — wrong whenever any open position predates today.
    # Use the actual closed-trade sum (get_closed_trade_pnl now windows on
    # FILL time, so days=1 is exactly "closed today").
    realized    = sum(t.get("realized_pnl", 0) for t in closed)

    wins   = [t for t in closed if t["realized_pnl"] > 0]
    losses = [t for t in closed if t["realized_pnl"] < 0]
    wins_total   = sum(t["realized_pnl"] for t in wins)
    losses_total = sum(t["realized_pnl"] for t in losses)

    # Monthly goal
    from calendar import monthrange
    today_dt      = datetime.today()
    days_in_month = monthrange(today_dt.year, today_dt.month)[1]
    day_of_month  = today_dt.day
    # 10% of current account value — target grows as account grows
    target        = portfolio * MONTHLY_TARGET_PCT
    # Month-to-date P&L: use today's total_today as a proxy for recent performance.
    # We accumulate daily moves to estimate MTD. Since we lack a start-of-month
    # snapshot in Alpaca paper, we use total open unrealized + closed today as MTD estimate.
    monthly_pl    = total_today   # today's P&L (realized + unrealized) as best MTD proxy
    monthly_pct   = monthly_pl / portfolio * 100 if portfolio else 0
    pace_needed   = target * (day_of_month / days_in_month)
    on_pace       = monthly_pl >= pace_needed
    pace_emoji    = "✅" if on_pace else "⚠️"

    # Build trade lines
    def trade_lines(trades, emoji):
        return "\n".join(
            f">{emoji} *{t['ticker']}*  {'+' if t['realized_pnl'] >= 0 else ''}"
            f"{t['realized_pnl']:.2f} ({t['realized_pnl_pct']:+.1f}%)"
            for t in trades
        ) or ">None"

    # Load tickers that had a partial exit (so we can flag half-size positions)
    try:
        from db import get_partial_exit_tickers
        partial_exit_tickers = get_partial_exit_tickers()
    except Exception:
        partial_exit_tickers = set()

    # Overnight positions
    overnight_lines = []
    for p in positions:
        tk      = p["ticker"]
        pl      = p["unrealized_pl"]
        pl_pct  = p["unrealized_pl_pct"]
        sl      = p.get("stop_loss", "—")
        tp      = p.get("take_profit", "—")
        half    = " ✂️½" if tk in partial_exit_tickers else ""
        sl_str  = f"${sl:.2f}" if isinstance(sl, float) else f"${sl}" if sl != "—" else "—"
        tp_str  = f"${tp:.2f}" if isinstance(tp, float) else f"${tp}" if tp != "—" else "—"
        overnight_lines.append(
            f">📌 *{tk}*{half}  {'+' if pl >= 0 else ''}{pl:.2f} ({pl_pct:+.1f}%)  "
            f"SL {sl_str}  TP {tp_str}"
        )
    overnight = "\n".join(overnight_lines) or ">None"

    msg = (
        f"📊 *End of Day Report — {today_dt.strftime('%b %d, %Y')}*\n\n"

        f"*Today's P&L*\n"
        f">🟢 *Realized (closed)*: {'+'if realized>=0 else ''}{realized:.2f}\n"
        f">📈 *Unrealized (open)*: {'+'if unrealized>=0 else ''}{unrealized:.2f}\n"
        f">💰 *Total*: *{'+'if total_today>=0 else ''}{total_today:.2f}*\n\n"

        f"*Closed Trades*\n"
        f">✅ Wins ({len(wins)}):  +${wins_total:.2f}\n"
        f"{trade_lines(wins, '✅')}\n"
        f">🔴 Losses ({len(losses)}):  ${losses_total:.2f}\n"
        f"{trade_lines(losses, '🔴')}\n\n"

        f"*Monthly Goal (10% = ${target:,.0f})*\n"
        f">{pace_emoji} ${monthly_pl:+,.2f} ({monthly_pct:+.1f}%) · "
        f"Day {day_of_month}/{days_in_month} · "
        f"{'Ahead' if on_pace else 'Behind'} pace\n\n"

        f"*Holding Overnight ({len(positions)} positions)*\n"
        f"{overnight}"
    )

    try:
        from alerts.slack import _post
        ok = _post({"text": msg})
        print(f"[CHRONICLE] Report sent — {len(wins)} wins, {len(losses)} losses, "
              f"net {total_today:+.2f}")
        return ok
    except Exception as e:
        print(f"[CHRONICLE] Slack error: {e}")


if __name__ == "__main__":
    run()
