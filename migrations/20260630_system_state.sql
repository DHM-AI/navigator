-- system_state: tiny key/value table for operational signals.
-- First use: last_scan_at — the scanner stamps this on EVERY run (even idle scans
-- that upsert no predictions), so heartbeat.py has a freshness signal that can't
-- freeze when scans re-pick the same tickers. (Codex review, 2026-06-30.)
--
-- Apply once in the Supabase SQL editor. Safe to re-run (idempotent).
-- Until applied, db.mark_scan_heartbeat() no-ops and heartbeat.py falls back to
-- max(predictions.created_at), so nothing breaks in the interim.

create table if not exists system_state (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);

-- seed the heartbeat row so the first read after deploy has something
insert into system_state (key, value)
values ('last_scan_at', now()::text)
on conflict (key) do nothing;
