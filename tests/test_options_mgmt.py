"""Tests for the Illuminati options EXIT-MANAGEMENT + runner layer.

Covers:
    options/manage.py   — parse_occ_expiration + manage_options TP/stop/time-exit/hold
    options/execution.py — close_option_position dry_run never submits
    options/run.py       — get_option_picks filtering + run(dry_run=True) shape

Runs two ways:
    python3 -m pytest tests/test_options_mgmt.py -q
    python3 tests/test_options_mgmt.py

SAFETY: these tests NEVER submit a real order. Every execution / management path
uses dry_run=True, and positions / picks / close calls are STUBBED via monkeypatch
so the suite is deterministic and offline. The single most important invariant —
options never trade live — is preserved: nothing here flips OPT_ENABLE_LIVE and
nothing submits.
"""

import os
import sys
from datetime import date, timedelta

# --- make the package root importable (market-predictions/) ---------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# pytest is optional: under `python3 tests/test_options_mgmt.py` it may be absent,
# so fall back to a tiny shim providing .skip / .approx / .raises.
try:
    import pytest  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - standalone fallback
    class _Skip(Exception):
        pass

    class _Approx:
        def __init__(self, v, rel=1e-6, abs=1e-9):
            self.v, self.rel, self.abs = v, rel, abs

        def __eq__(self, other):
            return abs(other - self.v) <= max(self.abs, self.rel * abs(self.v))

        def __repr__(self):
            return f"approx({self.v})"

    class _PytestShim:
        Skipped = _Skip

        @staticmethod
        def skip(msg=""):
            raise _Skip(msg)

        @staticmethod
        def approx(v, **k):
            return _Approx(v, **k)

    pytest = _PytestShim()  # type: ignore

# --- import the modules under test, skip cleanly if a sibling is missing ---
# (manage.py / run.py are authored in parallel; absence => skip, not error)
try:
    from options import config as opt_config
except Exception as exc:  # pragma: no cover - import guard
    opt_config = None
    _CONFIG_ERR = exc
else:
    _CONFIG_ERR = None

try:
    from options import execution as opt_exec
except Exception as exc:  # pragma: no cover
    opt_exec = None
    _EXEC_ERR = exc
else:
    _EXEC_ERR = None

try:
    from options import manage as opt_manage
except Exception as exc:  # pragma: no cover
    opt_manage = None
    _MANAGE_ERR = exc
else:
    _MANAGE_ERR = None

try:
    from options import run as opt_run
except Exception as exc:  # pragma: no cover
    opt_run = None
    _RUN_ERR = exc
else:
    _RUN_ERR = None


def _need(mod, err, name):
    if mod is None:
        pytest.skip(f"options.{name} not importable yet: {err}")


# =========================================================================
# helpers — build OCC symbols + synthetic positions
# =========================================================================
def _occ(root: str, expiry: date, cp: str, strike: float) -> str:
    """Build an OCC option symbol: ROOT + YYMMDD + (C|P) + strike*1000 (8 digits)."""
    yymmdd = expiry.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    return f"{root}{yymmdd}{cp}{strike_int:08d}"


def _pos(symbol, qty, avg_entry_price, unrealized_pl):
    """Shape a position the way execution.get_option_positions() returns it."""
    return {
        "symbol": symbol,
        "qty": float(qty),
        "avg_entry_price": float(avg_entry_price),
        "market_value": float(avg_entry_price) * float(qty) * 100.0
        + float(unrealized_pl),
        "unrealized_pl": float(unrealized_pl),
    }


class _CloseSpy:
    """Records every close_option_position call; returns a dry_run-shaped dict."""

    def __init__(self):
        self.calls = []

    def __call__(self, contract_symbol, qty, limit_price=None, dry_run=True):
        self.calls.append({
            "symbol": contract_symbol, "qty": qty,
            "limit_price": limit_price, "dry_run": dry_run,
        })
        # Never submit — always behave like a dry-run preview.
        return {"status": "dry_run", "request": {"symbol": contract_symbol,
                                                 "qty": str(qty),
                                                 "side": "sell",
                                                 "position_intent": "sell_to_close"}}


def _install_manage_stubs(monkeypatch, positions, close_spy):
    """Patch manage_options' view of positions + the close fn (wherever it reads them)."""
    # get_option_positions: patch on manage module if it re-exports, else execution.
    if hasattr(opt_manage, "get_option_positions"):
        monkeypatch.setattr(opt_manage, "get_option_positions", lambda: positions)
    if opt_exec is not None and hasattr(opt_exec, "get_option_positions"):
        monkeypatch.setattr(opt_exec, "get_option_positions", lambda: positions)

    # close_option_position: patch wherever manage_options dispatches the close.
    if hasattr(opt_manage, "close_option_position"):
        monkeypatch.setattr(opt_manage, "close_option_position", close_spy)
    if opt_exec is not None and hasattr(opt_exec, "close_option_position"):
        monkeypatch.setattr(opt_exec, "close_option_position", close_spy)

    # manage.py may reach execution via a module handle (e.g. `execution.close_...`).
    if hasattr(opt_manage, "execution"):
        monkeypatch.setattr(opt_manage.execution, "get_option_positions",
                            lambda: positions)
        monkeypatch.setattr(opt_manage.execution, "close_option_position", close_spy)


def _row_for(results, symbol):
    for r in results:
        if r.get("symbol") == symbol:
            return r
    return None


# =========================================================================
# config — new exit-management constants exist + are sane
# =========================================================================
def test_config_has_exit_constants():
    _need(opt_config, _CONFIG_ERR, "config")
    assert hasattr(opt_config, "OPT_TP_PCT")
    assert hasattr(opt_config, "OPT_STOP_PCT")
    assert hasattr(opt_config, "OPT_EXIT_DTE")
    assert hasattr(opt_config, "OPT_RESPECT_EQUITY_WINDOW")
    assert 0 < opt_config.OPT_TP_PCT
    assert 0 < opt_config.OPT_STOP_PCT
    assert opt_config.OPT_EXIT_DTE >= 0
    assert isinstance(opt_config.OPT_RESPECT_EQUITY_WINDOW, bool)


# =========================================================================
# parse_occ_expiration
# =========================================================================
def test_parse_occ_expiration_known_symbol():
    _need(opt_manage, _MANAGE_ERR, "manage")
    # SPY 2026-01-16 Call $500.00 -> SPY 260116 C 00500000
    sym = "SPY260116C00500000"
    assert opt_manage.parse_occ_expiration(sym) == "2026-01-16"


def test_parse_occ_expiration_put_and_short_root():
    _need(opt_manage, _MANAGE_ERR, "manage")
    # single-char root, put: F 260918 P 00012500 -> 2026-09-18
    assert opt_manage.parse_occ_expiration("F260918P00012500") == "2026-09-18"
    # long root (5 chars)
    assert opt_manage.parse_occ_expiration("GOOGL260320C00150000") == "2026-03-20"


def test_parse_occ_expiration_bad_input_returns_none():
    _need(opt_manage, _MANAGE_ERR, "manage")
    for bad in ("", "NOTASYMBOL", "SPY", None, "SPY2601"):
        assert opt_manage.parse_occ_expiration(bad) is None


def test_parse_occ_expiration_roundtrips_builder():
    _need(opt_manage, _MANAGE_ERR, "manage")
    d = date(2027, 7, 2)
    sym = _occ("AAPL", d, "C", 195.0)
    assert opt_manage.parse_occ_expiration(sym) == "2027-07-02"


# =========================================================================
# manage_options — TP / stop / time-exit / hold decisions (STUBBED positions)
# =========================================================================
def _far_expiry():
    """An expiration comfortably beyond OPT_EXIT_DTE so DTE never triggers."""
    horizon = (getattr(opt_config, "OPT_EXIT_DTE", 21) or 21) + 60
    return date.today() + timedelta(days=horizon)


def test_manage_options_take_profit(monkeypatch):
    _need(opt_manage, _MANAGE_ERR, "manage")
    _need(opt_config, _CONFIG_ERR, "config")
    far = _far_expiry()
    sym = _occ("SPY", far, "C", 500.0)
    # premium_basis = 5.00 * 2 * 100 = 1000. +60% PnL = +600 unrealized.
    tp = opt_config.OPT_TP_PCT
    pl = (tp + 0.10) * (5.00 * 2 * 100)  # comfortably above TP
    positions = [_pos(sym, 2, 5.00, pl)]
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, positions, spy)

    results = opt_manage.manage_options(dry_run=True)
    row = _row_for(results, sym)
    assert row is not None
    assert row["action"] == "close"
    assert row["reason"] == "tp"
    assert row["pnl_pct"] == pytest.approx(tp + 0.10, rel=1e-6)
    # closed exactly the held qty, dry_run propagated, nothing submitted
    assert len(spy.calls) == 1
    assert spy.calls[0]["symbol"] == sym
    assert spy.calls[0]["qty"] == 2
    assert spy.calls[0]["dry_run"] is True
    assert row.get("result", {}).get("status") == "dry_run"


def test_manage_options_stop_loss(monkeypatch):
    _need(opt_manage, _MANAGE_ERR, "manage")
    _need(opt_config, _CONFIG_ERR, "config")
    far = _far_expiry()
    sym = _occ("QQQ", far, "P", 400.0)
    stop = opt_config.OPT_STOP_PCT
    basis = 3.00 * 1 * 100  # 300
    pl = -(stop + 0.05) * basis  # below the -stop threshold
    positions = [_pos(sym, 1, 3.00, pl)]
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, positions, spy)

    results = opt_manage.manage_options(dry_run=True)
    row = _row_for(results, sym)
    assert row is not None
    assert row["action"] == "close"
    assert row["reason"] == "stop"
    assert row["pnl_pct"] == pytest.approx(-(stop + 0.05), rel=1e-6)
    assert len(spy.calls) == 1
    assert spy.calls[0]["qty"] == 1
    assert spy.calls[0]["dry_run"] is True


def test_manage_options_time_exit(monkeypatch):
    _need(opt_manage, _MANAGE_ERR, "manage")
    _need(opt_config, _CONFIG_ERR, "config")
    # Expire INSIDE the exit-DTE window but PnL flat (no TP/stop trigger).
    near = date.today() + timedelta(days=max(0, opt_config.OPT_EXIT_DTE - 1))
    sym = _occ("NVDA", near, "C", 120.0)
    positions = [_pos(sym, 3, 4.00, 0.0)]  # flat PnL -> only time can trigger
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, positions, spy)

    results = opt_manage.manage_options(dry_run=True)
    row = _row_for(results, sym)
    assert row is not None
    assert row["action"] == "close"
    assert row["reason"] == "time_exit"
    assert row["dte"] is not None and row["dte"] <= opt_config.OPT_EXIT_DTE
    assert len(spy.calls) == 1
    assert spy.calls[0]["qty"] == 3
    assert spy.calls[0]["dry_run"] is True


def test_manage_options_hold(monkeypatch):
    _need(opt_manage, _MANAGE_ERR, "manage")
    _need(opt_config, _CONFIG_ERR, "config")
    far = _far_expiry()
    sym = _occ("AAPL", far, "C", 190.0)
    # small positive PnL well under TP, far from expiry -> HOLD, no close call.
    basis = 6.00 * 2 * 100
    pl = (opt_config.OPT_TP_PCT * 0.2) * basis  # ~20% of TP threshold
    positions = [_pos(sym, 2, 6.00, pl)]
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, positions, spy)

    results = opt_manage.manage_options(dry_run=True)
    row = _row_for(results, sym)
    assert row is not None
    assert row["action"] == "hold"
    assert row["reason"] == "hold"
    assert "result" not in row or row.get("result") in (None, {})
    assert len(spy.calls) == 0, "HOLD must not call close_option_position"


def test_manage_options_tp_precedes_time_exit(monkeypatch):
    """A winner that is ALSO near expiry should close with reason 'tp' (TP checked first)."""
    _need(opt_manage, _MANAGE_ERR, "manage")
    _need(opt_config, _CONFIG_ERR, "config")
    near = date.today() + timedelta(days=max(0, opt_config.OPT_EXIT_DTE - 2))
    sym = _occ("MSFT", near, "C", 400.0)
    basis = 5.00 * 1 * 100
    pl = (opt_config.OPT_TP_PCT + 0.20) * basis
    positions = [_pos(sym, 1, 5.00, pl)]
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, positions, spy)

    results = opt_manage.manage_options(dry_run=True)
    row = _row_for(results, sym)
    assert row is not None
    assert row["reason"] == "tp", "TP must take precedence over time_exit"


def test_manage_options_zero_basis_no_div0(monkeypatch):
    """avg_entry_price 0 -> premium_basis 0; must not raise (div-by-zero guard)."""
    _need(opt_manage, _MANAGE_ERR, "manage")
    far = _far_expiry()
    sym = _occ("AMD", far, "C", 100.0)
    positions = [_pos(sym, 1, 0.0, 0.0)]
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, positions, spy)
    # should simply not crash, and not erroneously fire a TP/stop on a div0
    results = opt_manage.manage_options(dry_run=True)
    assert isinstance(results, list)
    row = _row_for(results, sym)
    assert row is not None  # the position is still reported


def test_manage_options_survives_one_bad_position(monkeypatch):
    """A malformed position must not abort management of the good ones."""
    _need(opt_manage, _MANAGE_ERR, "manage")
    _need(opt_config, _CONFIG_ERR, "config")
    far = _far_expiry()
    good_sym = _occ("SPY", far, "C", 500.0)
    basis = 5.00 * 1 * 100
    good = _pos(good_sym, 1, 5.00, (opt_config.OPT_TP_PCT + 0.1) * basis)
    bad = {"symbol": None, "qty": "oops"}  # garbage
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, [bad, good], spy)

    results = opt_manage.manage_options(dry_run=True)  # must NOT raise
    assert isinstance(results, list)
    good_row = _row_for(results, good_sym)
    assert good_row is not None and good_row["action"] == "close"
    assert good_row["reason"] == "tp"


def test_manage_options_empty_positions(monkeypatch):
    _need(opt_manage, _MANAGE_ERR, "manage")
    spy = _CloseSpy()
    _install_manage_stubs(monkeypatch, [], spy)
    results = opt_manage.manage_options(dry_run=True)
    assert results == []
    assert len(spy.calls) == 0


# =========================================================================
# execution.close_option_position — dry_run never submits
# =========================================================================
def _enable_paper_gate(monkeypatch):
    """Open the OPT_ENABLE_PAPER gate so behavioral tests reach the logic under
    test. Production keeps the gate False (options OFF, 2026-06-09 revert) —
    these tests verify the dry-run/qty/live-refusal contracts BEHIND the gate."""
    if hasattr(opt_exec, "OPT_ENABLE_PAPER"):
        monkeypatch.setattr(opt_exec, "OPT_ENABLE_PAPER", True)
    else:  # pragma: no cover - defensive
        monkeypatch.setattr(opt_config, "OPT_ENABLE_PAPER", True)


def test_close_option_position_dry_run_no_submit(monkeypatch):
    _need(opt_exec, _EXEC_ERR, "execution")
    _enable_paper_gate(monkeypatch)
    res = opt_exec.close_option_position("SPY260116C00500000", 2,
                                         limit_price=5.0, dry_run=True)
    assert isinstance(res, dict)
    assert res.get("status") == "dry_run"
    assert "request" in res and isinstance(res["request"], dict)
    req = res["request"]
    # closing a long: SELL + sell_to_close (NOT a short)
    assert req.get("side") == "sell"
    assert req.get("position_intent") == "sell_to_close"
    assert req.get("symbol") == "SPY260116C00500000"
    assert req.get("qty") == "2"


def test_close_option_position_zero_qty_skipped(monkeypatch):
    _need(opt_exec, _EXEC_ERR, "execution")
    _enable_paper_gate(monkeypatch)
    res = opt_exec.close_option_position("SPY260116C00500000", 0, dry_run=True)
    assert res.get("status") == "skipped_zero_qty"


def test_close_option_position_negative_qty_skipped(monkeypatch):
    _need(opt_exec, _EXEC_ERR, "execution")
    _enable_paper_gate(monkeypatch)
    res = opt_exec.close_option_position("SPY260116C00500000", -2, dry_run=True)
    assert res.get("status") == "skipped_zero_qty"


def test_close_option_position_respects_paper_gate(monkeypatch):
    _need(opt_exec, _EXEC_ERR, "execution")
    if hasattr(opt_exec, "OPT_ENABLE_PAPER"):
        monkeypatch.setattr(opt_exec, "OPT_ENABLE_PAPER", False)
    else:  # pragma: no cover - defensive
        monkeypatch.setattr(opt_config, "OPT_ENABLE_PAPER", False)
    res = opt_exec.close_option_position("SPY260116C00500000", 1, dry_run=True)
    assert res.get("status") == "disabled"


def test_close_option_position_refuses_live(monkeypatch):
    """HARD GATE — if OPT_ENABLE_LIVE were ever True, closes refuse, never submit.
    Paper gate must be opened first: the 'disabled' check precedes the live check,
    so with PAPER=False the call returns 'disabled' before the refusal under test."""
    _need(opt_exec, _EXEC_ERR, "execution")
    _enable_paper_gate(monkeypatch)
    if hasattr(opt_exec, "OPT_ENABLE_LIVE"):
        monkeypatch.setattr(opt_exec, "OPT_ENABLE_LIVE", True)
    else:  # pragma: no cover
        monkeypatch.setattr(opt_config, "OPT_ENABLE_LIVE", True)
    res = opt_exec.close_option_position("SPY260116C00500000", 1, dry_run=True)
    assert res.get("status") == "refused_live"


# =========================================================================
# run.get_option_picks — filtering (score + allowed underlyings), maps shape
# =========================================================================
def _install_predictions_stub(monkeypatch, rows):
    """Feed `rows` to get_option_picks() via whatever read seam it uses.

    get_option_picks() reads the latest equity predictions. The cleanest seam is
    the module-internal _latest_predictions() helper that returns the raw rows;
    patch it directly when present. Otherwise patch the underlying db reads
    (db_available True so the query path is taken, today's-date read empty so it
    falls through, then load_predictions returns rows)."""
    patched = False
    # Preferred: patch the internal "give me the latest rows" helper directly.
    if hasattr(opt_run, "_latest_predictions"):
        monkeypatch.setattr(opt_run, "_latest_predictions", lambda: list(rows))
        patched = True

    # Also patch the db reads (covers builds without _latest_predictions).
    try:
        import db as _db  # market-predictions/db.py
        if hasattr(_db, "db_available"):
            monkeypatch.setattr(_db, "db_available", lambda: True)
        if hasattr(_db, "load_predictions_for_date"):
            monkeypatch.setattr(_db, "load_predictions_for_date",
                                lambda *a, **k: [])
        if hasattr(_db, "load_predictions"):
            monkeypatch.setattr(_db, "load_predictions", lambda *a, **k: list(rows))
            patched = True
    except Exception:
        pass

    # Or run.py imported the read fn(s) into its own namespace.
    if hasattr(opt_run, "load_predictions"):
        monkeypatch.setattr(opt_run, "load_predictions", lambda *a, **k: list(rows))
        patched = True
    if hasattr(opt_run, "db") and hasattr(opt_run.db, "load_predictions"):
        monkeypatch.setattr(opt_run.db, "load_predictions",
                            lambda *a, **k: list(rows))
        patched = True

    if not patched:
        pytest.skip("could not locate a predictions read fn to stub for get_option_picks")


def test_get_option_picks_filters_score_and_underlying(monkeypatch):
    _need(opt_run, _RUN_ERR, "run")
    _need(opt_config, _CONFIG_ERR, "config")
    allowed = opt_config.OPT_ALLOWED_UNDERLYINGS[0]  # e.g. SPY
    minsc = opt_config.OPT_MIN_SCORE
    rows = [
        {"ticker": allowed, "score": minsc + 5, "direction": "bullish",
         "date": "2026-06-08"},                                  # keep
        {"ticker": allowed, "score": minsc - 1, "direction": "bullish",
         "date": "2026-06-08"},                                  # drop: low score
        {"ticker": "GME", "score": minsc + 20, "direction": "bullish",
         "date": "2026-06-08"},                                  # drop: not allowed
        {"ticker": allowed, "score": minsc, "direction": "bearish",
         "date": "2026-06-08"},                                  # keep: == min
    ]
    _install_predictions_stub(monkeypatch, rows)

    picks = opt_run.get_option_picks()
    assert isinstance(picks, list)
    tickers = [p.get("ticker") for p in picks]
    assert "GME" not in tickers, "disallowed underlying must be filtered out"
    # every surviving pick clears the score floor + is in the allow-list
    for p in picks:
        assert p["ticker"] in opt_config.OPT_ALLOWED_UNDERLYINGS
        assert p["score"] >= minsc
        assert "direction" in p
    # the two qualifying allowed rows survive
    assert len([p for p in picks if p["ticker"] == allowed]) == 2


def test_get_option_picks_empty_on_error(monkeypatch):
    """Any read error -> [] (never crash)."""
    _need(opt_run, _RUN_ERR, "run")

    def _boom(*a, **k):
        raise RuntimeError("db down")

    patched = False
    # Preferred seam: the internal latest-rows helper.
    if hasattr(opt_run, "_latest_predictions"):
        monkeypatch.setattr(opt_run, "_latest_predictions", _boom)
        patched = True
    # Also force the underlying db reads to raise (db_available True so the
    # query path is actually entered, then every read explodes).
    try:
        import db as _db  # noqa: WPS433
        if hasattr(_db, "db_available"):
            monkeypatch.setattr(_db, "db_available", lambda: True)
        if hasattr(_db, "load_predictions_for_date"):
            monkeypatch.setattr(_db, "load_predictions_for_date", _boom)
        if hasattr(_db, "load_predictions"):
            monkeypatch.setattr(_db, "load_predictions", _boom)
            patched = True
    except Exception:
        pass
    if hasattr(opt_run, "db") and hasattr(opt_run.db, "load_predictions"):
        monkeypatch.setattr(opt_run.db, "load_predictions", _boom)
        patched = True
    if hasattr(opt_run, "load_predictions"):
        monkeypatch.setattr(opt_run, "load_predictions", _boom)
        patched = True
    if not patched:
        pytest.skip("no predictions read seam to force an error for get_option_picks")

    picks = opt_run.get_option_picks()
    assert picks == []


# =========================================================================
# run.run(dry_run=True) — returns the documented shape, submits nothing
# =========================================================================
def test_run_dry_run_shape(monkeypatch):
    _need(opt_run, _RUN_ERR, "run")

    # Stub the two halves so run() does no network I/O and submits nothing.
    if hasattr(opt_run, "manage_options"):
        monkeypatch.setattr(opt_run, "manage_options",
                            lambda dry_run=True: [{"symbol": "X", "action": "hold",
                                                   "reason": "hold", "pnl_pct": 0.0,
                                                   "dte": 40}])
    if hasattr(opt_run, "get_option_picks"):
        monkeypatch.setattr(opt_run, "get_option_picks", lambda: [])
    if hasattr(opt_run, "run_options_scan"):
        monkeypatch.setattr(opt_run, "run_options_scan",
                            lambda picks, dry_run=True: [])

    out = opt_run.run(dry_run=True)
    assert isinstance(out, dict)
    assert "managed" in out and isinstance(out["managed"], list)
    assert "entered" in out and isinstance(out["entered"], list)
    assert "ts" in out
    # nothing in either half should be marked submitted under dry_run
    for r in out["managed"] + out["entered"]:
        assert r.get("status") != "submitted"


def test_run_manages_before_entering(monkeypatch):
    """run() must run EXITS (manage) before ENTRIES (scan) — theta-rot fix order."""
    _need(opt_run, _RUN_ERR, "run")
    order = []

    if hasattr(opt_run, "manage_options"):
        def _mgr(dry_run=True):
            order.append("manage")
            return []
        monkeypatch.setattr(opt_run, "manage_options", _mgr)
    else:
        pytest.skip("run.manage_options not patchable in this build")

    if hasattr(opt_run, "get_option_picks"):
        monkeypatch.setattr(opt_run, "get_option_picks", lambda: [])
    if hasattr(opt_run, "run_options_scan"):
        def _scan(picks, dry_run=True):
            order.append("scan")
            return []
        monkeypatch.setattr(opt_run, "run_options_scan", _scan)
    else:
        pytest.skip("run.run_options_scan not patchable in this build")

    opt_run.run(dry_run=True)
    assert order == ["manage", "scan"], f"expected manage->scan, got {order}"


# =========================================================================
# plain-python runner (so `python3 tests/test_options_mgmt.py` also works)
# =========================================================================
def _run_standalone():
    import types
    import inspect

    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value):
            old = getattr(target, name)
            self._undo.append((target, name, old))
            setattr(target, name, value)

        def undo(self):
            for target, name, old in reversed(self._undo):
                setattr(target, name, old)
            self._undo.clear()

    skip_exc = getattr(pytest, "Skipped", Exception)
    try:
        pytest.skip("__probe__")
    except Exception as probe:  # capture the concrete skip exception type
        skip_exc = type(probe)

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    passed = failed = skipped = 0
    for fn in tests:
        mp = _MP()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except skip_exc as s:
            print(f"SKIP {fn.__name__}: {s}")
            skipped += 1
        except AssertionError as a:
            print(f"FAIL {fn.__name__}: {a}")
            failed += 1
        except Exception as e:
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            mp.undo()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
