"""
Supabase persistence layer — replaces logs/predictions.csv and logs/sentiment_cache.json.

Two tables:
  predictions     — one row per (date, ticker) prediction
  sentiment_cache — one row per (ticker, date) sentiment score
"""
import os
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client

_SCHEMA_COLS = {
    # ── Core prediction columns (in Supabase table) ───────────────────────────
    "date", "ticker", "score", "direction", "duration", "confidence",
    "signals_triggered", "rsi", "bb_pct", "atr_ratio", "volume_ratio",
    "sentiment_score", "earnings_days", "xgb_prob", "actual_move_5d",
    "dollar_amount", "kelly_fraction", "pct_of_bankroll", "risk_level",
    # ── Extended columns — add with ALTER TABLE before enabling ───────────────
    # "ema50_pct", "sentiment_velocity",
    # "pattern", "pattern_side",
    # "options_side", "options_pcr", "options_unusual", "options_detail",
}


def get_client() -> Client:
    """
    Build a Supabase client. Prefers SUPABASE_SERVICE_KEY (admin, bypasses RLS)
    over the anon-level SUPABASE_KEY. The service key is server-side ONLY —
    never expose it to a browser.

    Why both? During the transition we keep accepting the anon key as fallback
    so nothing breaks if SUPABASE_SERVICE_KEY isn't set yet.
    """
    url     = os.environ.get("SUPABASE_URL", "")
    service = os.environ.get("SUPABASE_SERVICE_KEY", "")
    anon    = os.environ.get("SUPABASE_KEY", "")
    key     = service or anon

    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL plus one of (SUPABASE_SERVICE_KEY, SUPABASE_KEY) "
            "must be set in environment or .env"
        )
    return create_client(url, key)


def _using_service_key() -> bool:
    """Returns True iff the service-role key is in use (writes will bypass RLS)."""
    return bool(os.environ.get("SUPABASE_SERVICE_KEY", ""))


# Streamlit was decommissioned 2026-06-02 (dashboard is Cloudflare Pages now).
# Kept get_cached_client as an alias for backward compatibility with any caller.
get_cached_client = get_client


def _client() -> Client:
    return get_client()


# ── Predictions ───────────────────────────────────────────────────────────────

def load_predictions(limit: int = 2000) -> list[dict]:
    result = (_client().table("predictions")
              .select("*").order("date", desc=True).limit(limit).execute())
    return result.data or []


def load_evaluated_predictions(limit: int = 4000) -> list[dict]:
    """Predictions WITH a backfilled outcome (actual_move_5d not null) — the
    only rows ORACLE / the learning agent can actually learn from.

    AUDIT H-15/M-33 (2026-06-09): both learners filtered load_predictions(),
    which PostgREST caps at the most-recent ~1000 rows (~3 days, all too new
    to have outcomes) — so the primary feedback path NEVER had data, the same
    bug class as the dead backfill. Query the evaluated rows directly."""
    result = (_client().table("predictions")
              .select("*")
              .not_.is_("actual_move_5d", "null")
              .order("date", desc=True).limit(limit).execute())
    return result.data or []


def load_predictions_for_date(date_str: str) -> list[dict]:
    result = (
        _client()
        .table("predictions")
        .select("*")
        .eq("date", date_str)
        .order("score", desc=True)
        .execute()
    )
    return result.data or []


def _sanitize(v):
    """Convert NaN / Infinity / whole-float / nested objects to JSON-safe types."""
    import math

    # numpy scalars → Python native first
    try:
        import numpy as np
        if isinstance(v, np.bool_):
            return bool(v)
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            v = float(v)   # fall through to float handling below
    except ImportError:
        pass

    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        # Whole-number floats → int so Supabase INTEGER columns accept them
        if v == int(v):
            return int(v)
        return v

    if isinstance(v, list):
        return "; ".join(str(i) for i in v)

    if isinstance(v, dict):
        return str(v)   # dicts (e.g. breakdown) become strings

    return v


def append_predictions(rows: list[dict]) -> None:
    if not rows:
        return
    # Strip columns not in the DB schema + sanitize NaN/inf/nested types
    clean = [
        {k: _sanitize(v)
         for k, v in r.items()
         if k in _SCHEMA_COLS}
        for r in rows
    ]
    _client().table("predictions").upsert(clean, on_conflict="date,ticker").execute()


def update_actual_move(date_str: str, ticker: str, move: float) -> None:
    (
        _client()
        .table("predictions")
        .update({"actual_move_5d": round(move, 2)})
        .eq("date", date_str)
        .eq("ticker", ticker)
        .execute()
    )


# ── Sentiment cache ───────────────────────────────────────────────────────────

def load_sentiment_cache() -> dict:
    """Return {ticker: {date_str: score}} for the last 30 days."""
    cutoff = (datetime.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    result = (
        _client()
        .table("sentiment_cache")
        .select("ticker,date,score")
        .gte("date", cutoff)
        .execute()
    )
    cache: dict = {}
    for row in result.data or []:
        t = row["ticker"]
        d = str(row["date"])
        cache.setdefault(t, {})[d] = row["score"]
    return cache


def save_sentiment_entry(ticker: str, date_str: str, score: float) -> None:
    (
        _client()
        .table("sentiment_cache")
        .upsert(
            {"ticker": ticker, "date": date_str, "score": score},
            on_conflict="ticker,date",
        )
        .execute()
    )


def prune_sentiment_cache(days: int = 30) -> None:
    cutoff = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    _client().table("sentiment_cache").delete().lt("date", cutoff).execute()


def db_available() -> bool:
    """Return True if Supabase credentials are configured.

    R-3 fix: was checking SUPABASE_KEY only. Service-key-only deployments
    silently lost EVERY DB feature (prediction persistence, partial-exit
    history, sentiment cache, ORACLE learnings).
    """
    return bool(
        os.environ.get("SUPABASE_URL")
        and (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY"))
    )


# ── Learnings ─────────────────────────────────────────────────────────────────

def save_learning(record: dict) -> None:
    _client().table("learnings").insert(record).execute()


def load_learnings() -> list[dict]:
    result = _client().table("learnings").select("*").order("week_of", desc=True).execute()
    return result.data or []


# ── Trades ────────────────────────────────────────────────────────────────────

_TRADE_COLS = {
    # ── Existing columns ────────────────────────────────────
    "order_id", "ticker", "side", "dollar_amount",
    "mode", "status", "reason", "timestamp",
    # ── Added 2026-05-28 ────────────────────────────────────
    "execution_path",  # "rule_confirmed" | "model_bypass" | "" (blocked/closed rows)
    # ── Add with ALTER TABLE below, then uncomment ──────────
    # "qty", "entry_price", "stop_loss", "take_profit",
}

def save_trade(record: dict) -> None:
    """Save an Alpaca trade execution record."""
    clean = {k: _sanitize(v) for k, v in record.items() if k in _TRADE_COLS}
    _client().table("trades").upsert(clean, on_conflict="order_id").execute()


def load_trades() -> list[dict]:
    result = _client().table("trades").select("*").order("timestamp", desc=True).execute()
    return result.data or []


def get_partial_exit_history(lookback_days: int = 90,
                              open_tickers=None) -> dict:
    """
    Return {ticker: {'t1': bool, 't2': bool, 't1_qty': float}} for tickers that
    have had partial exits within the lookback window.

    Used by AEGIS to know which tiers have already fired per ticker so it
    doesn't double-fire.

    CRITICAL audit C-6: A partial-exit record for ticker X from a position that
    has SINCE BEEN CLOSED was making a NEW position in X skip its T1/T2
    scale-out (real-money risk). Fix: only count exits that occurred AFTER the
    most recent fully-closed trade for that ticker (i.e., still apply to the
    currently-open position). If no open position for ticker, drop history.
    """
    history: dict = {}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        result = (
            _client().table("trades")
            .select("ticker, status, dollar_amount, reason, timestamp")
            .in_("status", ["partial_exit", "partial_exit_t1", "partial_exit_t2"])
            .gte("timestamp", cutoff)
            .execute()
        )
        # Audit M-2 (2026-06-02): compare timestamps as parsed datetimes, not raw
        # strings. Lexical compare only works if every row is identical-format UTC
        # ISO; a mixed tz-offset / naive-vs-aware string would mis-order and could
        # let a stale partial suppress a real T1/T2 scale-out (real-money risk).
        def _ts_epoch(s) -> float:
            try:
                return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0
        # Find the most recent "position closed" timestamp per ticker so we
        # only count partials AFTER that point as still-active.
        last_close: dict[str, float] = {}
        try:
            closes = (
                _client().table("trades")
                .select("ticker, status, timestamp")
                .in_("status", ["closed", "sl_hit", "tp_hit", "manual_close", "trail_hit"])
                .gte("timestamp", cutoff)
                .execute()
            )
            for c in (closes.data or []):
                tk = c["ticker"]
                ts = _ts_epoch(c["timestamp"])
                if ts > last_close.get(tk, 0.0):
                    last_close[tk] = ts
        except Exception:
            pass   # if close lookup fails, fall through to old behavior

        for r in (result.data or []):
            tk = r["ticker"]
            st = r["status"]
            # Drop history if ticker is not currently held (no open position)
            if open_tickers is not None and tk not in open_tickers:
                continue
            # Drop history if a close happened AFTER this partial (different position)
            if _ts_epoch(r["timestamp"]) < last_close.get(tk, 0.0):
                continue
            history.setdefault(tk, {"t1": False, "t2": False, "t1_qty": 0.0})
            # Legacy "partial_exit" entries count as Tier 1 fired
            if st in ("partial_exit", "partial_exit_t1"):
                history[tk]["t1"] = True
                # Try to extract qty from reason field (format: "... qty=X ...")
                import re as _re
                m = _re.search(r"qty=([\d.]+)", str(r.get("reason", "")))
                if m:
                    history[tk]["t1_qty"] = float(m.group(1))
            elif st == "partial_exit_t2":
                history[tk]["t2"] = True
    except Exception as _e:
        # CRITICAL: silent pass here caused AEGIS to double-fire partial exits when
        # Supabase was unavailable (history returned {} → T1 always looked unfired).
        # Now we log AND return None as a sentinel so callers can detect the failure.
        print(f"[DB] get_partial_exit_history FAILED: {_e} — returning None (AEGIS will skip partial exits)")
        return None   # caller must check: `if partial_history is None: skip partial exit`
    return history


def get_partial_exit_tickers(lookback_days: int = 90) -> set:
    """Backward-compat: just the set of tickers with Tier 1 already fired."""
    hist = get_partial_exit_history(lookback_days)
    return {tk for tk, info in hist.items() if info.get("t1")}
