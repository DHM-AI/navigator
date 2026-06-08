import anthropic
from config import ANTHROPIC_API_KEY, TOP_N_CLAUDE_ANALYSIS

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _build_prompt(row: dict) -> str:
    signals = ", ".join(row.get("signals_triggered", [])) or "None triggered"
    return (
        f"You are a quantitative analyst. A market scanner flagged {row['ticker']} with the following data:\n\n"
        f"- Direction: {row.get('direction', 'unknown')}\n"
        f"- Expected move window: {row.get('duration', 'unknown')}\n"
        f"- Composite score: {row.get('score', 0)}/100\n"
        f"- Signals triggered: {signals}\n"
        f"- RSI: {row.get('rsi', 'N/A')}\n"
        f"- BB width percentile: {row.get('bb_pct', 'N/A')} (lower = tighter squeeze)\n"
        f"- ATR ratio (current/50d avg): {row.get('atr_ratio', 'N/A')}\n"
        f"- Volume ratio (today/20d avg): {row.get('volume_ratio', 'N/A')}x\n"
        f"- EMA50 offset: {row.get('ema50_pct', 'N/A')}%\n"
        f"- Sentiment score: {row.get('sentiment_score', 0):.3f} | Velocity (Δ7d): {row.get('sentiment_velocity', 0):.3f}\n"
        f"- Days to earnings: {row.get('earnings_days', 'N/A')}\n\n"
        f"In exactly 3 concise bullet points, explain why {row['ticker']} may move 5%+ within the predicted window. "
        f"Be specific about the technical setup. No filler, no hedging phrases like 'it is important to note'."
    )


# SEC-402 audit fix: split into HIGH-CONFIDENCE (refusal-only phrases — fire
# on any single match) and SOFT (verbs that could appear in legitimate bearish
# analysis like "I cannot find any signals" — require multiple matches OR
# combination with a hard marker).
_REFUSAL_HARD = (
    "appears to be an attempt", "appears to be a prompt",
    "system directive", "system prompt",
    "i need to flag", "i need to point out",
    "ignore this prompt", "ignore this directive",
    "attempt to inject", "prompt injection",
)
_REFUSAL_SOFT = (
    "i won't", "i will not", "i can't", "i cannot",
    "i should not", "i'm not able to", "i am not able to",
    "decline to",
)


def _looks_like_refusal(text: str) -> bool:
    """True if Claude's response looks like a prompt-injection refusal
    rather than an actual analysis.

    Two-tier match to avoid false-positives on legitimate bearish analysis
    (e.g. 'I cannot find any bullish signals here — RSI is neutral'):
      - HARD markers fire on a single match (refusal-specific phrasings)
      - SOFT markers require ≥2 matches OR co-occurrence with a hard marker
    """
    if not text:
        return False
    lower = text.lower()[:300]
    if any(m in lower for m in _REFUSAL_HARD):
        return True
    soft_hits = sum(1 for m in _REFUSAL_SOFT if m in lower)
    return soft_hits >= 2


def explain_picks(scored_df, top_n: int = TOP_N_CLAUDE_ANALYSIS,
                  oracle_directive: str = ""):
    """
    Returns {ticker: markdown_explanation} for the top N picks.
    Uses non-streaming for batch processing (email/logging).
    """
    if scored_df is None or scored_df.empty:
        return {}

    results = {}
    top = scored_df.head(top_n)

    for _, row in top.iterrows():
        ticker = row["ticker"]
        prompt = _build_prompt(row.to_dict())
        if oracle_directive:
            # Was 'ORACLE system directive: ...' — triggered Claude's
            # prompt-injection defense. Reframe as research context.
            prompt = (
                f"Today's research focus (one-line note from our learning loop, "
                f"may inform your analysis but does NOT override the technical setup): "
                f"\"{oracle_directive}\"\n\n" + prompt
            )
        try:
            message = _get_client().messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text
            # Safety filter: if Claude refused the prompt, regenerate
            # WITHOUT the directive — better to have a clean analysis with
            # no research-focus context than a refusal message saved as
            # the trade's permanent reason.
            if _looks_like_refusal(text):
                print(f"[claude] {ticker}: response looked like a refusal "
                      f"({text[:80]!r}) — regenerating without directive")
                clean_prompt = _build_prompt(row.to_dict())
                message = _get_client().messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=400,
                    messages=[{"role": "user", "content": clean_prompt}],
                )
                text = message.content[0].text
                if _looks_like_refusal(text):
                    text = (f"Technical setup: composite score "
                            f"{row.get('score', 0)}/100, signals: "
                            f"{', '.join(row.get('signals_triggered', [])[:3]) or 'see breakdown'}")
            results[ticker] = text
        except Exception as e:
            results[ticker] = f"Analysis unavailable: {e}"

    return results

# Finding #19 (2026-06-08): removed dead stream_explanation() — zero callers
# (the dashboard uses the persisted explanation field). _build_prompt and
# _get_client imports/helpers retained: still used by explain_picks above.
