"""Illuminati — Options subsystem risk / sizing gates.

Pure, side-effect-free helpers that decide HOW MANY contracts to buy and WHETHER a
trade is allowed to open. No network, no order submission — those live in
execution.py / strategy.py. All thresholds come from options.config.
"""

from options.config import (
    OPT_ALLOWED_UNDERLYINGS,
    OPT_MAX_OPEN,
    OPT_MAX_PREMIUM_PCT,
    OPT_MIN_SCORE,
    OPT_CONTRACT_MULTIPLIER,
)


def size_option_qty(
    bankroll: float,
    mid_price: float,
    contract_multiplier: int = OPT_CONTRACT_MULTIPLIER,
) -> int:
    """Largest whole-contract quantity whose total premium stays within the
    per-trade premium budget (OPT_MAX_PREMIUM_PCT of bankroll).

    Premium per contract = mid_price * contract_multiplier. We pick the largest
    int qty such that qty * mid_price * contract_multiplier <= budget.

    Returns 0 when the trade is uninvestable: non-positive mid price, non-positive
    bankroll/multiplier, or a budget too small for a single contract. Never negative.
    """
    # Guard against bad inputs — any of these means "buy nothing".
    try:
        bankroll = float(bankroll)
        mid_price = float(mid_price)
        contract_multiplier = int(contract_multiplier)
    except (TypeError, ValueError):
        return 0

    if mid_price <= 0 or bankroll <= 0 or contract_multiplier <= 0:
        return 0

    budget = OPT_MAX_PREMIUM_PCT * bankroll
    cost_per_contract = mid_price * contract_multiplier
    if cost_per_contract <= 0:
        return 0

    qty = int(budget // cost_per_contract)
    return qty if qty > 0 else 0


def can_open_option(underlying: str, score: float, open_count: int) -> tuple[bool, str]:
    """Gate a candidate options trade against the universe, conviction, and
    concurrency limits.

    Returns (True, "ok") only when ALL hold:
      - underlying is in OPT_ALLOWED_UNDERLYINGS
      - score >= OPT_MIN_SCORE
      - open_count < OPT_MAX_OPEN
    Otherwise (False, reason) with a short human-readable reason.
    """
    # Normalize the underlying for a case-insensitive membership check.
    sym = (underlying or "").strip().upper()
    if sym not in OPT_ALLOWED_UNDERLYINGS:
        return False, f"underlying {underlying!r} not in allowed universe"

    try:
        score_val = float(score)
    except (TypeError, ValueError):
        return False, f"invalid score {score!r}"
    if score_val < OPT_MIN_SCORE:
        return False, f"score {score_val:g} < OPT_MIN_SCORE {OPT_MIN_SCORE}"

    try:
        open_n = int(open_count)
    except (TypeError, ValueError):
        return False, f"invalid open_count {open_count!r}"
    if open_n >= OPT_MAX_OPEN:
        return False, f"open_count {open_n} >= OPT_MAX_OPEN {OPT_MAX_OPEN}"

    return True, "ok"
