"""Illuminati — Options trading subsystem (ISOLATED + GATED, paper-only).

Built 2026-06-08. Lives apart from the equity engine so it can never affect the
live equity system. Hard live gate: options NEVER trade live (OPT_ENABLE_LIVE
stays False); paper/demo only until a deliberate, validated go-live decision.
Historical option data is OPRA-gated; the backtest runs in Black-Scholes
approximation mode (underlying price history) until the OPRA agreement is signed.
"""
