"""
Learning Agent — weekly post-mortem.

Runs every Sunday. Loads all predictions from the past week where
actual_move_5d is known, identifies what worked and what didn't,
generates a Claude analysis, and saves insights to Supabase.

Also triggers model retraining if enough new data exists.
"""
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


def _build_analysis_prompt(hits_df: pd.DataFrame, misses_df: pd.DataFrame) -> str:
    def summarize(df: pd.DataFrame, label: str) -> str:
        if df.empty:
            return f"{label}: none"
        signals_flat = []
        for s in df.get("signals_triggered", pd.Series(dtype=str)).dropna():
            signals_flat.extend([x.strip() for x in str(s).split(";")])
        from collections import Counter
        top = Counter(signals_flat).most_common(5)
        avg_score = df["score"].mean() if "score" in df else "N/A"
        avg_move = df["actual_move_5d"].abs().mean() if "actual_move_5d" in df else "N/A"
        return (
            f"{label} ({len(df)} trades): "
            f"avg score={avg_score:.1f}, avg move={avg_move:.1f}%, "
            f"top signals={[s for s,_ in top]}"
        )

    return f"""You are a quantitative trading analyst reviewing last week's prediction results.

{summarize(hits_df, 'HITS (moved 5%+)')}
{summarize(misses_df, 'MISSES (did not move 5%+)')}

Direction accuracy:
- Bullish calls that went up: {len(hits_df[(hits_df.get('direction','') == 'bullish') & (hits_df.get('actual_move_5d', 0) > 0)]) if not hits_df.empty else 0}
- Bearish calls that went down: {len(hits_df[(hits_df.get('direction','') == 'bearish') & (hits_df.get('actual_move_5d', 0) < 0)]) if not hits_df.empty else 0}

Write a concise post-mortem in 4 bullet points:
1. What signal combinations are most predictive of success?
2. What patterns appear in the misses (false positives)?
3. What should the system do differently next week?
4. Any market regime observations (trending/volatile/quiet)?

IMPORTANT — before recommending any weight change, classify each miss by CAUSE:
- EXOGENOUS (don't down-weight signals): weekend/overnight GAP through the stop,
  a market-wide reversal, or an earnings surprise. These are entry-timing /
  variance, not signal failure. Example: 2026-06-01 HOOD scored 89 (highest) yet
  lost most — it gapped down over a weekend. The signal was fine; the entry
  timing wasn't. Guardrails (no Friday-PM entries, max 2 entries/ticker) address
  these, NOT weight changes.
- ENDOGENOUS (consider down-weighting): the signal genuinely fired on a setup
  that played out the opposite way in normal continuous trading.
Only recommend weight adjustments for ENDOGENOUS misses. A 60% win-rate system
will have losing streaks by design — distinguish variance from degradation.

Be specific and actionable. No filler."""


def run_postmortem() -> dict:
    """
    Load last week's evaluated predictions, analyze hits vs misses,
    generate Claude insights, save to Supabase.
    Returns the learning record.
    """
    import db
    if not db.db_available():
        print("[learning] No Supabase — skipping postmortem.")
        return {}

    rows = db.load_predictions()
    if not rows:
        print("[learning] No predictions to analyze yet.")
        return {}

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])

    # Only look at predictions old enough to have actual moves
    cutoff = pd.Timestamp.today() - timedelta(days=5)
    evaluated = df[df["date"] <= cutoff].dropna(subset=["actual_move_5d"])

    if len(evaluated) < 5:
        print(f"[learning] Only {len(evaluated)} evaluated predictions — need 5+ to run postmortem.")
        return {}

    hits = evaluated[evaluated["actual_move_5d"].abs() >= MOVE_TARGET_PCT * 100]
    misses = evaluated[evaluated["actual_move_5d"].abs() < MOVE_TARGET_PCT * 100]
    hit_rate = len(hits) / len(evaluated)

    print(f"[learning] {len(evaluated)} predictions | Hit rate: {hit_rate:.1%} | Hits: {len(hits)} | Misses: {len(misses)}")

    # Claude analysis
    prompt = _build_analysis_prompt(hits, misses)
    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        claude_analysis = response.content[0].text
        print(f"[learning] Claude analysis:\n{claude_analysis}")
    except Exception as e:
        claude_analysis = f"Analysis unavailable: {e}"

    # Top signals in hits vs misses
    def top_signals(df: pd.DataFrame, n: int = 3) -> str:
        from collections import Counter
        all_sigs = []
        for s in df.get("signals_triggered", pd.Series(dtype=str)).dropna():
            all_sigs.extend([x.strip() for x in str(s).split(";")])
        return ", ".join(s for s, _ in Counter(all_sigs).most_common(n))

    record = {
        "week_of": (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d"),
        "total_predictions": len(evaluated),
        "hit_rate": round(hit_rate, 4),
        "top_hit_signals": top_signals(hits),
        "top_miss_signals": top_signals(misses),
        "claude_analysis": claude_analysis,
        "weight_adjustments": json.dumps({}),  # future: auto-adjust weights
    }

    db.save_learning(record)
    print(f"[learning] Saved postmortem for week of {record['week_of']}")

    # Trigger model retraining if hit rate < 50% (needs improvement)
    if hit_rate < 0.50 and len(evaluated) >= 20:
        print("[learning] Hit rate below 50% with sufficient data — triggering model retrain...")
        try:
            from model.trainer import train
            train(force_refetch=False)
        except Exception as e:
            print(f"[learning] Retrain failed: {e}")

    return record


if __name__ == "__main__":
    run_postmortem()
