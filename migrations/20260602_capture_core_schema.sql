-- ════════════════════════════════════════════════════════════════════════════
-- Capture core-table schema as source-of-truth + ensure upsert conflict keys
-- ════════════════════════════════════════════════════════════════════════════
-- Audit 2026-06-02 (H-2): the four core tables were created manually in the
-- Supabase UI and have NO migration in this repo, so the UNIQUE/PK constraints
-- that db.py's upserts depend on (on_conflict=...) are unverifiable from source.
-- They DO exist in practice (upserts succeed today); this file documents the
-- schema and idempotently RE-ASSERTS the critical constraints so they're
-- reproducible and provably correct.
--
-- Safe to run: every statement is guarded (IF NOT EXISTS / conditional DO block).
-- On the live DB these are no-ops; on a fresh project they recreate the contract.
--
-- Introspected columns (2026-06-02), for reference:
--   predictions(id, date, ticker, score, direction, duration, confidence,
--     signals_triggered, rsi, bb_pct, atr_ratio, volume_ratio, sentiment_score,
--     earnings_days, xgb_prob, actual_move_5d, created_at, dollar_amount,
--     kelly_fraction, pct_of_bankroll, risk_level)
--   trades(id, order_id, ticker, side, dollar_amount, mode, status, reason,
--     timestamp, created_at, execution_path)
--   sentiment_cache(id, ticker, date, score, updated_at)
--   learnings(id, week_of, total_predictions, hit_rate, top_hit_signals,
--     top_miss_signals, claude_analysis, weight_adjustments, created_at)
-- ════════════════════════════════════════════════════════════════════════════

-- predictions: db.py upserts on_conflict="date,ticker"
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.predictions'::regclass AND contype = 'u'
      AND conname = 'predictions_date_ticker_key'
  ) AND NOT EXISTS (
    -- skip if ANY unique index already covers (date, ticker)
    SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='predictions'
      AND indexdef ILIKE '%UNIQUE%(date, ticker)%'
  ) THEN
    ALTER TABLE public.predictions
      ADD CONSTRAINT predictions_date_ticker_key UNIQUE (date, ticker);
  END IF;
END $$;

-- sentiment_cache: db.py upserts on_conflict="ticker,date"
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.sentiment_cache'::regclass AND contype = 'u'
      AND conname = 'sentiment_cache_ticker_date_key'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='sentiment_cache'
      AND indexdef ILIKE '%UNIQUE%(ticker, date)%'
  ) THEN
    ALTER TABLE public.sentiment_cache
      ADD CONSTRAINT sentiment_cache_ticker_date_key UNIQUE (ticker, date);
  END IF;
END $$;

-- trades: db.py upserts on_conflict="order_id"
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.trades'::regclass AND contype IN ('u','p')
      AND conname = 'trades_order_id_key'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='trades'
      AND indexdef ILIKE '%UNIQUE%(order_id)%'
  ) THEN
    ALTER TABLE public.trades
      ADD CONSTRAINT trades_order_id_key UNIQUE (order_id);
  END IF;
END $$;

-- Verify after running:
--   SELECT conrelid::regclass AS table, conname, contype
--   FROM pg_constraint
--   WHERE conrelid IN ('public.predictions'::regclass,
--                      'public.sentiment_cache'::regclass,
--                      'public.trades'::regclass)
--     AND contype IN ('u','p');
