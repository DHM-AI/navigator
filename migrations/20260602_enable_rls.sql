-- ════════════════════════════════════════════════════════════════════════════
-- Enable Row-Level Security on all public tables
-- ════════════════════════════════════════════════════════════════════════════
-- Fixes Supabase Advisor "rls_disabled_in_public" (CRITICAL) on project
-- market-predictions (qqcahevxdvkphafacskm), flagged 2026-05-31.
--
-- WHY THIS IS SAFE:
--   Every code path that touches Supabase uses the SERVICE key, which has the
--   BYPASSRLS attribute — so enabling RLS does NOT affect them:
--     • db.py (trading agent / GitHub Actions)  -> SUPABASE_SERVICE_KEY
--     • illuminati-dashboard workers (dashboard.js, alerts.js, alert-*.js)
--       -> SUPABASE_SERVICE_KEY (server-side only; keys never reach the browser)
--   No BROWSER client uses the anon key. So we enable RLS with NO policies:
--   the anon/public role is denied all access (the security hole), while the
--   service key continues to read/write normally. No policies are needed because
--   there is no legitimate anon access to preserve.
--
--   CORRECTION (audit 2026-06-02): two NON-production readers DO use the anon key
--   and will silently return empty reads under RLS unless given the service key:
--     1. the local .env (dev/manual runs) — add SUPABASE_SERVICE_KEY locally
--     2. the Streamlit Cloud backup dashboard — set SUPABASE_SERVICE_KEY in its
--        Streamlit secrets, else its pages show empty (RLS returns [], not error)
--   Production (GitHub Actions + Cloudflare Pages) is correctly on the service key.
--
--   If you ever add a browser client that reads Supabase directly with the anon
--   key, you'll then add explicit SELECT policies for the `anon` role per table.
--
-- Safe to re-run: ENABLE ROW LEVEL SECURITY is idempotent; ALTER TABLE IF EXISTS
-- skips any table that doesn't exist in this project.
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE IF EXISTS public.predictions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sentiment_cache    ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.trades             ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.learnings          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.price_alerts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.insider_cache      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.analyst_cache      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.weekly_trend_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.model_drift_log    ENABLE ROW LEVEL SECURITY;

-- Verify after running — every row should show rowsecurity = true:
--   SELECT tablename, rowsecurity
--   FROM pg_tables
--   WHERE schemaname = 'public'
--   ORDER BY tablename;
