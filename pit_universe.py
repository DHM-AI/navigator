"""Point-in-time S&P 500 membership — the survivorship-bias fix (2026-06-30).

The live universe (data/universe.py) and the training parquet use TODAY's S&P 500.
Backtesting/training on today's members silently drops every company that was in the
index during the test window but was later removed (acquired, bankrupt, demoted) —
e.g. SIVB, FRC, SBNY, ATVI, PXD, SPLK. For a long-only momentum book that inflates
the backtested edge (you only ever "buy" names that survived to today).

This reconstructs which tickers were ACTUALLY in the S&P on any past date from
Wikipedia's current list + its add/remove change log, walking changes backward:
  members_on(D) = current_members - {added after D} + {removed after D}

No paid data: Alpaca's market-data API retains historical bars for delisted names
(verified — SIVB/FRC return 2022 bars), so the corrected backtest can fetch them.
"""
from __future__ import annotations
import requests
import pandas as pd
from io import StringIO

_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_UA = {"User-Agent": "Mozilla/5.0"}


def _norm(t) -> str:
    """Normalize a Wikipedia ticker to our convention (BRK.B -> BRK-B)."""
    if not isinstance(t, str):
        return ""
    return t.strip().upper().replace(".", "-")


def fetch_tables():
    r = requests.get(_WIKI, headers=_UA, timeout=30)
    r.raise_for_status()
    return pd.read_html(StringIO(r.text))


def current_members(tables=None) -> set[str]:
    tables = tables or fetch_tables()
    syms = tables[0]["Symbol"].astype(str).map(_norm)
    return {s for s in syms if s}


def fetch_changes(tables=None) -> pd.DataFrame:
    """Return tidy change log: columns date(datetime), added(str|""), removed(str|"")."""
    tables = tables or fetch_tables()
    ch = tables[1].copy()
    ch.columns = ["_".join(str(x) for x in c) if isinstance(c, tuple) else str(c)
                  for c in ch.columns]
    date_col = next(c for c in ch.columns if "Effective Date" in c)
    add_col  = next(c for c in ch.columns if c.startswith("Added") and "Ticker" in c)
    rem_col  = next(c for c in ch.columns if c.startswith("Removed") and "Ticker" in c)
    out = pd.DataFrame({
        "date":    pd.to_datetime(ch[date_col], errors="coerce"),
        "added":   ch[add_col].map(_norm),
        "removed": ch[rem_col].map(_norm),
    }).dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return out


def members_on(target_date, current: set[str], changes: pd.DataFrame) -> set[str]:
    """Membership set as of target_date, by undoing every change AFTER it."""
    target = pd.Timestamp(target_date)
    s = set(current)
    for _, row in changes.iterrows():
        if row["date"] <= target:
            continue                       # change is at/before target → already reflected
        if row["added"]:                   # added AFTER target → wasn't a member then
            s.discard(row["added"])
        if row["removed"]:                 # removed AFTER target → WAS a member then
            s.add(row["removed"])
    return s


def historical_universe(start, end, tables=None) -> set[str]:
    """Union of monthly membership over [start, end] — everyone who was an S&P
    member at any monthly snapshot in the window (current + since-removed)."""
    tables = tables or fetch_tables()
    cur = current_members(tables); ch = fetch_changes(tables)
    uni: set[str] = set()
    for d in pd.date_range(start, end, freq="MS"):
        uni |= members_on(d, cur, ch)
    uni |= members_on(end, cur, ch)
    return uni


def missing_survivors(start, end, tables=None) -> set[str]:
    """Names that were S&P members during [start,end] but are NOT in today's list —
    the ones a survivor-only backtest silently drops."""
    tables = tables or fetch_tables()
    return historical_universe(start, end, tables) - current_members(tables)


if __name__ == "__main__":
    import os, sys
    start = sys.argv[1] if len(sys.argv) > 1 else "2021-08-01"
    end   = sys.argv[2] if len(sys.argv) > 2 else "2026-05-31"
    tabs = fetch_tables()
    cur = current_members(tabs)
    uni = historical_universe(start, end, tabs)
    miss = sorted(uni - cur)
    print(f"PIT S&P 500 membership  {start} → {end}")
    print(f"  current members:        {len(cur)}")
    print(f"  point-in-time universe: {len(uni)}  (current + since-removed)")
    print(f"  MISSING from today (survivorship gap): {len(miss)}")
    print(f"  gap as % of universe: {len(miss)/max(len(uni),1)*100:.1f}%")
    print(f"  missing names: {', '.join(miss)}")
