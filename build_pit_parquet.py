"""Build a survivorship-COMPLETE training parquet (2026-06-30).

The live parquet (model/saved/training_data.parquet) holds only TODAY's 503 S&P
members. This adds the ~89 names that WERE in the index during the window but were
later removed (acquired/failed/demoted) — the survivorship gap. Their OHLC comes
from Alpaca (which retains delisted-name bars), and features/labels are computed
with the SAME pipeline the trainer uses (signals.technicals.compute_feature_row +
the ±MOVE_TARGET_PCT-in-5d label), so the new rows are consistent with the parquet.

Output: model/saved/training_data_pit.parquet  (original + missing names).
Run:    python build_pit_parquet.py
"""
from __future__ import annotations
import os, time
import requests
import pandas as pd

import pit_universe as pit
from signals.technicals import compute_feature_row
from config import MOVE_TARGET_PCT

ORIG = "model/saved/training_data.parquet"
OUT  = "model/saved/training_data_pit.parquet"
START, END = "2021-08-01", "2026-05-31"
_URL = "https://data.alpaca.markets/v2/stocks/bars"
_H = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
      "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def fetch_ohlc(ticker: str) -> pd.DataFrame | None:
    """Alpaca daily bars (SIP, split/div-adjusted), fully paginated. Class shares
    use dots on Alpaca (BRK-B -> BRK.B); we restore the hyphen key."""
    alp = ticker.replace("-", ".")
    bars, pt = [], None
    for _ in range(30):
        p = {"symbols": alp, "timeframe": "1Day", "start": START, "end": END,
             "feed": "sip", "adjustment": "all", "limit": 10000}
        if pt:
            p["page_token"] = pt
        try:
            j = requests.get(_URL, headers=_H, params=p, timeout=60).json()
        except Exception as e:
            print(f"    {ticker}: fetch error {e}"); break
        bars += (j.get("bars") or {}).get(alp) or []
        pt = j.get("next_page_token")
        if not pt:
            break
    if not bars:
        return None
    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["t"]).dt.tz_localize(None).dt.normalize()
    df = (df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                             "c": "Close", "v": "Volume"})
            .drop_duplicates(subset="date").set_index("date").sort_index())
    return df[["Open", "High", "Low", "Close", "Volume"]]


def rows_for(ticker: str, df: pd.DataFrame) -> list[dict]:
    """Trainer's exact feature+label loop."""
    out = []
    for idx in range(60, len(df) - 6):
        window = df.iloc[:idx + 1]
        try:
            features = compute_feature_row(window)
        except Exception:
            continue
        future = df["Close"].iloc[idx + 1:idx + 6]
        cc = float(df["Close"].iloc[idx])
        if len(future) < 5 or cc == 0:
            continue
        mx = float((future.max() - cc) / cc)
        mn = float((future.min() - cc) / cc)
        label = 1 if (mx >= MOVE_TARGET_PCT or mn <= -MOVE_TARGET_PCT) else 0
        row = {"ticker": ticker, "date": df.index[idx], "label": label}
        row.update(features)
        out.append(row)
    return out


def main():
    orig = pd.read_parquet(ORIG)
    print(f"[pit] original parquet: {len(orig):,} rows · {orig['ticker'].nunique()} tickers")
    missing = sorted(pit.missing_survivors(START, END))
    print(f"[pit] missing survivors to add: {len(missing)}")

    new_rows, got, empty = [], [], []
    for i, t in enumerate(missing):
        df = fetch_ohlc(t)
        if df is None or len(df) < 80:
            empty.append(t); continue
        r = rows_for(t, df)
        if r:
            new_rows.extend(r); got.append(t)
        else:
            empty.append(t)
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(missing)} processed · {len(new_rows):,} rows so far")
        time.sleep(0.15)

    print(f"[pit] added {len(got)}/{len(missing)} names · {len(new_rows):,} new rows")
    print(f"[pit] no/thin data (skipped): {', '.join(empty)}")
    new_df = pd.DataFrame(new_rows)
    # align columns to the original parquet (extra cols -> NaN; combo dropna handles)
    combined = pd.concat([orig, new_df], ignore_index=True)
    combined.to_parquet(OUT, index=False)
    print(f"[pit] wrote {OUT}: {len(combined):,} rows · {combined['ticker'].nunique()} tickers "
          f"(+{combined['ticker'].nunique()-orig['ticker'].nunique()} vs survivor-only)")


if __name__ == "__main__":
    main()
