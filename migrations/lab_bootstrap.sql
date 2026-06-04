-- ════════════════════════════════════════════════════════════════════
-- LAB SUPABASE BOOTSTRAP — one-paste schema for the LAB (paper) project
-- ════════════════════════════════════════════════════════════════════
-- Run this ENTIRE file in the LAB Supabase SQL editor to create every
-- table the system needs. All statements are idempotent (IF NOT EXISTS /
-- guarded DO blocks) — safe to re-run. Order matters: core tables first,
-- then session tables, then the ALTERs that depend on them.
--
-- RLS is intentionally NOT included here — the lab uses the service-role
-- key (which bypasses RLS) for both the agent and the dashboard, and
-- skipping RLS avoids the "anon key returns empty" confusion in the lab.
-- If you want RLS on the lab too, run 20260602_enable_rls.sql afterward.
-- Generated 2026-06-04 from the tested prod migrations.
-- ════════════════════════════════════════════════════════════════════


-- ╭─────────────────────────────────────────────────────────────────╮
-- │ SECTION: 20260602_capture_core_schema.sql
-- ╰─────────────────────────────────────────────────────────────────╯
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


-- ╭─────────────────────────────────────────────────────────────────╮
-- │ SECTION: 20260526_session_tables.sql
-- ╰─────────────────────────────────────────────────────────────────╯
-- Migration: 2026-05-26 session — adds 4 tables for new signals and price alerts.
-- Paste this entire file into Supabase SQL editor (one shot, idempotent).
--
-- Tables created:
--   insider_cache       — SEC Form 4 cluster-buy signal (24h cache)
--   analyst_cache       — analyst revisions signal (24h cache)
--   weekly_trend_cache  — weekly trend signal (6h cache)
--   price_alerts        — "ping me when X hits $Y" alerts
--
-- Safe to re-run: all CREATE TABLE statements use IF NOT EXISTS.

-- ─────────────────────────────────────────────────────────────────────
-- Insider activity cache (SEC Form 4 cluster buys/sells via yfinance)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insider_cache (
  ticker     TEXT PRIMARY KEY,
  checked_at TIMESTAMPTZ NOT NULL,
  payload    JSONB NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────
-- Analyst revisions cache (upgrades/downgrades via yfinance)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analyst_cache (
  ticker     TEXT PRIMARY KEY,
  checked_at TIMESTAMPTZ NOT NULL,
  payload    JSONB NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────
-- Weekly trend cache (multi-timeframe alignment via yfinance / resample)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS weekly_trend_cache (
  ticker     TEXT PRIMARY KEY,
  checked_at TIMESTAMPTZ NOT NULL,
  payload    JSONB NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────
-- Price alerts ("ping me when NVDA hits $200")
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_alerts (
  id          BIGSERIAL PRIMARY KEY,
  ticker      TEXT NOT NULL,
  target      NUMERIC NOT NULL,
  direction   TEXT NOT NULL CHECK (direction IN ('above', 'below')),
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  fired       BOOLEAN NOT NULL DEFAULT FALSE,
  fired_at    TIMESTAMPTZ,
  fired_price NUMERIC
);

-- Useful indexes (the active-alerts query filters on fired=false)
CREATE INDEX IF NOT EXISTS price_alerts_active_idx
  ON price_alerts (fired, ticker) WHERE fired = FALSE;

CREATE INDEX IF NOT EXISTS price_alerts_ticker_idx
  ON price_alerts (ticker);

-- ─────────────────────────────────────────────────────────────────────
-- Model drift check log (weekly calibration drift report)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_drift_log (
  id         BIGSERIAL PRIMARY KEY,
  checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  total_rows INT,
  max_drift  NUMERIC,
  flagged    BOOLEAN,
  report     JSONB
);


-- ╭─────────────────────────────────────────────────────────────────╮
-- │ SECTION: 20260527_price_alerts_auto_score.sql
-- ╰─────────────────────────────────────────────────────────────────╯
-- Migration: 2026-05-27 — add auto_score flag to price_alerts.
--
-- When TRUE (default), the ticker on this alert is also fed into the scan
-- universe so it gets scored alongside S&P 500 + watchlist on every cycle.
-- When FALSE, the alert only fires a price-cross Slack ping — no analysis.
--
-- Paste into Supabase SQL editor.

ALTER TABLE price_alerts
  ADD COLUMN IF NOT EXISTS auto_score BOOLEAN NOT NULL DEFAULT TRUE;


-- ╭─────────────────────────────────────────────────────────────────╮
-- │ SECTION: 20260527_price_alerts_optional_target.sql
-- ╰─────────────────────────────────────────────────────────────────╯
-- Migration: 2026-05-27 — make target + direction optional on price_alerts.
--
-- A row with target=NULL means "watch-only" — the ticker gets included in
-- the scan universe (if auto_score=true) but no price-cross ping fires.
-- A row with target set works as a full price alert as before.
--
-- Paste into Supabase SQL editor.

ALTER TABLE price_alerts ALTER COLUMN target    DROP NOT NULL;
ALTER TABLE price_alerts ALTER COLUMN direction DROP NOT NULL;

-- Replace the old CHECK constraint that required direction IN ('above','below')
-- with one that ALSO allows NULL (for watch-only entries).
ALTER TABLE price_alerts DROP CONSTRAINT IF EXISTS price_alerts_direction_check;
ALTER TABLE price_alerts ADD  CONSTRAINT price_alerts_direction_check
  CHECK (direction IS NULL OR direction IN ('above', 'below'));


-- ╭─────────────────────────────────────────────────────────────────╮
-- │ SECTION: 20260528_trades_execution_path.sql
-- ╰─────────────────────────────────────────────────────────────────╯
-- 2026-05-28  Add execution_path to trades
-- Tracks whether a trade was placed via:
--   "rule_confirmed" — score≥MIN_SCORE AND confidence≥Medium (both gates fired)
--   "model_bypass"   — score≥HIGH_SCORE_BYPASS (model alone qualified, Low conf)
--   ""               — blocked/closed rows (not applicable)
-- After 2+ weeks of data, compare win rates between the two paths.

ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS execution_path TEXT DEFAULT '';

