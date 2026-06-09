"""
ORACLE — Daily Intelligence Agent

Runs every weekday at 10 PM ET. Reviews today's trades and predictions,
learns what worked and what didn't, and writes structured insights to
Supabase that every other agent reads at the start of each scan.

The learning loop:
  1. ORACLE reviews predictions vs actual outcomes
  2. Identifies which signals are working, which are failing
  3. Flags market regime patterns
  4. Saves structured directives to Supabase learnings table
  5. Scanner reads ORACLE's latest directives before each scan
     and adjusts its analysis accordingly

ORACLE teaches the other agents so the system gets smarter every day.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
import pandas as pd
import anthropic
from config import ANTHROPIC_API_KEY, MOVE_TARGET_PCT

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _build_prompt(hits_df: pd.DataFrame, misses_df: pd.DataFrame,
                  recent_trades: list[dict]) -> str:
    def summarize(df: pd.DataFrame, label: str) -> str:
        if df.empty:
            return f"{label}: none this period"
        from collections import Counter
        signals_flat = []
        for s in df.get("signals_triggered", pd.Series(dtype=str)).dropna():
            signals_flat.extend([x.strip() for x in str(s).split(";")])
        top      = Counter(signals_flat).most_common(5)
        avg_score = df["score"].mean() if "score" in df else 0
        avg_move  = df["actual_move_5d"].abs().mean() if "actual_move_5d" in df else 0
        dirs      = df.get("direction", pd.Series(dtype=str)).value_counts().to_dict()
        return (
            f"{label} ({len(df)} trades): "
            f"avg score={avg_score:.1f}, avg move={avg_move:.1f}%, "
            f"directions={dirs}, top signals={[s for s,_ in top]}"
        )

    trade_summary = ""
    if recent_trades:
        wins   = [t for t in recent_trades if t.get("realized_pnl", 0) > 0]
        losses = [t for t in recent_trades if t.get("realized_pnl", 0) < 0]
        trade_summary = (
            f"\nRecent closed trades: {len(recent_trades)} total, "
            f"{len(wins)} wins (+${sum(t.get('realized_pnl',0) for t in wins):.2f}), "
            f"{len(losses)} losses (${sum(t.get('realized_pnl',0) for t in losses):.2f})"
        )

    return f"""You are ORACLE, the intelligence core of an AI trading system.
Your job is to learn from recent performance and issue directives that improve every future scan.

RECENT PERFORMANCE:
{summarize(hits_df, 'HITS (target reached)')}
{summarize(misses_df, 'MISSES (target not reached)')}
{trade_summary}

Respond in this exact JSON format (no markdown, no extra text):
{{
  "summary": "2-sentence plain-English summary of what happened",
  "winning_signals": ["signal1", "signal2"],
  "failing_signals": ["signal1", "signal2"],
  "regime_note": "one sentence about current market conditions",
  "scanner_directive": "one specific instruction for the scanner to follow — be concrete, e.g. 'Increase weight on volume_surge for bullish picks' or 'Avoid bearish plays in Technology sector this week'",
  "confidence_threshold_adjust": 0,
  "avoid_sectors": [],
  "favor_directions": "bullish"
}}"""


def run() -> dict:
    """
    Run ORACLE's daily analysis. Saves structured learnings to Supabase
    so every other agent reads and applies them.
    """
    import db
    if not db.db_available():
        print("[ORACLE] No Supabase — skipping.")
        return {}

    print(f"[ORACLE] Daily intelligence cycle — {datetime.today().strftime('%Y-%m-%d %H:%M')}")

    # AUDIT H-15 (2026-06-09): load_predictions(limit=500) returned only the
    # newest ~1.5 days of rows — none old enough to have outcomes, so this
    # primary path was permanently empty and ORACLE always fell back to
    # closed-trade cold-start. Query the evaluated rows directly.
    rows = db.load_evaluated_predictions(limit=4000)
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])

    # ── PRIMARY DATA: predictions with actual_move_5d backfilled ──
    evaluated = df.dropna(subset=["actual_move_5d"]) if not df.empty else pd.DataFrame()

    # ── FALLBACK DATA: actual closed Alpaca trades (immediate, no 5-day wait) ──
    # This lets ORACLE start learning from day 1 instead of waiting a week.
    closed_trades = []
    try:
        from execution.alpaca import get_closed_trade_pnl, is_configured
        if is_configured():
            closed_trades = get_closed_trade_pnl(days=30)
    except Exception as e:
        print(f"[ORACLE] Could not load closed trades: {e}")

    # Cold-start fallback: synthesize "evaluated" rows from closed Alpaca trades
    # so ORACLE can learn from actual wins/losses immediately.
    if len(evaluated) < 5 and closed_trades:
        print(f"[ORACLE] Only {len(evaluated)} backfilled predictions — using {len(closed_trades)} closed trades as primary signal.")
        synth_rows = []
        for ct in closed_trades:
            tk = ct.get("ticker")
            pnl_pct = float(ct.get("realized_pnl_pct", 0))
            side = ct.get("side", "long")
            synth_rows.append({
                "date":            pd.to_datetime(ct.get("closed_at", datetime.today().isoformat())),
                "ticker":          tk,
                "direction":       "bullish" if side == "long" else "bearish",
                "actual_move_5d":  pnl_pct,
                "signals_triggered": "",   # unknown from closed trade data
                "score":           75,
            })
        if synth_rows:
            evaluated = pd.DataFrame(synth_rows)

    if len(evaluated) < 3:   # lowered from 5 to enable earlier learning
        print(f"[ORACLE] Only {len(evaluated)} data points total — need 3+ to analyze. Skipping.")
        return {}

    # Hit definition differs by data source:
    # - Backfilled predictions: "hit" = price moved >= MOVE_TARGET_PCT (20%) in 5 days
    # - Closed Alpaca trades:   "hit" = trade closed profitably (any positive P&L)
    # AUDIT H-16 (2026-06-09): the old bar was abs(move) >= 20% (the TP
    # CEILING, direction ignored) — a stock that crashed -25% against a
    # bullish call counted as a "hit" and the guaranteed ~8% hit rate fed
    # ORACLE a permanently negative self-assessment. A hit is a move IN THE
    # PREDICTED DIRECTION; same definition as measure_edge.
    def _dir_sign(d):
        d = str(d).lower()
        return 1 if d in ("bullish", "long", "buy") else (-1 if d in ("bearish", "short", "sell") else 0)
    if "direction" in evaluated.columns:
        _dirmove = evaluated["actual_move_5d"] * evaluated["direction"].map(_dir_sign)
    else:
        _dirmove = evaluated["actual_move_5d"]
    hits     = evaluated[_dirmove > 0]
    misses   = evaluated[_dirmove <= 0]
    hit_rate = len(hits) / len(evaluated) if len(evaluated) else 0

    print(f"[ORACLE] {len(evaluated)} data points | Hit rate: {hit_rate:.1%} | "
          f"Hits: {len(hits)} | Misses: {len(misses)} | "
          f"Sources: {len(df.dropna(subset=['actual_move_5d']) if not df.empty else [])} predictions + {len(closed_trades)} closed trades")

    # Use already-fetched closed_trades (last 7 days for prompt context)
    recent_trades = [
        ct for ct in closed_trades
        if ct.get("closed_at", "") >= (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    ]

    prompt = _build_prompt(hits, misses, recent_trades)
    try:
        response = _get_client().messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 600,
            messages   = [{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text.strip()
        # Strip markdown code fences if Claude wraps the JSON in ```json ... ```
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(
                l for l in lines
                if not l.strip().startswith("```")
            ).strip()
        directives = json.loads(raw_text)
        print(f"[ORACLE] Directive → {directives.get('scanner_directive','')}")
    except Exception as e:
        print(f"[ORACLE] Claude error: {e}")
        directives = {}

    def top_signals(df: pd.DataFrame, n: int = 3) -> str:
        from collections import Counter
        sigs = []
        for s in df.get("signals_triggered", pd.Series(dtype=str)).dropna():
            sigs.extend([x.strip() for x in str(s).split(";")])
        return ", ".join(s for s, _ in Counter(sigs).most_common(n))

    # H-6 fix: ORACLE runs at 10 PM ET = 2 AM UTC NEXT DAY. datetime.today()
    # returns UTC date which is TOMORROW relative to the trading day this run
    # actually summarizes. Use ET date so the label matches the data.
    try:
        from zoneinfo import ZoneInfo
        _et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        _et_today = datetime.today().strftime("%Y-%m-%d")

    record = {
        "week_of":            _et_today,
        "total_predictions":  len(evaluated),
        "hit_rate":           round(hit_rate, 4),
        "top_hit_signals":    top_signals(hits),
        "top_miss_signals":   top_signals(misses),
        "claude_analysis":    directives.get("summary", ""),
        "weight_adjustments": json.dumps(directives),
    }

    db.save_learning(record)
    print(f"[ORACLE] Learnings saved for {record['week_of']}")

    try:
        from alerts.slack import _post
        _post({"text": (
            f"🔮 *ORACLE — {datetime.today().strftime('%b %d, %Y')}*\n"
            f">{directives.get('summary', '')}\n\n"
            f"*Hit rate:* {hit_rate:.1%}  ·  "
            f"*Winning signals:* {', '.join(directives.get('winning_signals', []))}\n"
            f"*Weak signals:* {', '.join(directives.get('failing_signals', []))}\n\n"
            f"📡 *Tomorrow's directive:* _{directives.get('scanner_directive', '—')}_"
        )})
    except Exception:
        pass

    if hit_rate < 0.50 and len(evaluated) >= 20:
        print("[ORACLE] Hit rate below 50% — triggering model retrain...")
        try:
            from model.trainer import train
            train(force_refetch=False)
        except Exception as e:
            print(f"[ORACLE] Retrain failed: {e}")

    return record


def get_latest_directives() -> dict:
    """
    Called by the scanner at the start of every scan.
    Returns ORACLE's most recent structured directives so the scanner
    can apply them — which signals to trust, which sectors to avoid,
    which direction to favor.
    """
    try:
        import db
        if not db.db_available():
            return {}
        learnings = db.load_learnings()
        if not learnings:
            return {}
        raw = learnings[0].get("weight_adjustments", "{}")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


if __name__ == "__main__":
    run()
