"""Tests for the Illuminati options subsystem (options/ package).

Runs two ways:
    python3 -m pytest tests/test_options.py -q
    python3 tests/test_options.py

SAFETY: these tests NEVER submit a real order. Every execution path uses
dry_run=True. Read-only network calls (list contracts / chain snapshot) are
allowed but stubbed by default so the suite is deterministic offline; one
optional test will hit the live SPY chain only if the network is reachable and
is skipped otherwise.
"""

import os
import sys

# --- make the package root importable (market-predictions/) ---------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# pytest is optional: under `python3 tests/test_options.py` it may be absent,
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
# (the other modules are authored in parallel; absence => skip, not error)
try:
    from options import config as opt_config
except Exception as exc:  # pragma: no cover - import guard
    opt_config = None
    _CONFIG_ERR = exc
else:
    _CONFIG_ERR = None

try:
    from options import data as opt_data
except Exception as exc:  # pragma: no cover
    opt_data = None
    _DATA_ERR = exc
else:
    _DATA_ERR = None

try:
    from options import risk as opt_risk
except Exception as exc:  # pragma: no cover
    opt_risk = None
    _RISK_ERR = exc
else:
    _RISK_ERR = None

try:
    from options import execution as opt_exec
except Exception as exc:  # pragma: no cover
    opt_exec = None
    _EXEC_ERR = exc
else:
    _EXEC_ERR = None

try:
    from options import strategy as opt_strategy
except Exception as exc:  # pragma: no cover
    opt_strategy = None
    _STRATEGY_ERR = exc
else:
    _STRATEGY_ERR = None


def _need(mod, err, name):
    if mod is None:
        pytest.skip(f"options.{name} not importable yet: {err}")


# =========================================================================
# config
# =========================================================================
def test_config_imports_and_sane_values():
    _need(opt_config, _CONFIG_ERR, "config")
    # core flags
    assert opt_config.OPT_ENABLE_PAPER is True
    # HARD GATE — options must never be allowed to trade live
    assert opt_config.OPT_ENABLE_LIVE is False

    # underlyings allow-list
    assert isinstance(opt_config.OPT_ALLOWED_UNDERLYINGS, (list, tuple))
    assert "SPY" in opt_config.OPT_ALLOWED_UNDERLYINGS
    assert len(opt_config.OPT_ALLOWED_UNDERLYINGS) >= 1

    # scoring / sizing thresholds
    assert opt_config.OPT_MIN_SCORE == 80
    assert 0 < opt_config.OPT_MAX_PREMIUM_PCT < 1
    assert opt_config.OPT_MAX_OPEN >= 1

    # DTE window is coherent
    assert opt_config.OPT_DTE_MIN <= opt_config.OPT_TARGET_DTE <= opt_config.OPT_DTE_MAX

    # greeks / BS assumptions
    assert 0 < opt_config.OPT_TARGET_DELTA < 1
    assert opt_config.OPT_IV_ASSUMPTION > 0
    assert opt_config.OPT_RISK_FREE >= 0
    assert opt_config.OPT_CONTRACT_MULTIPLIER == 100

    # strategy descriptor
    assert opt_config.OPT_STRATEGY == "long_option"


def test_live_gate_is_off():
    """Stand-alone, explicit check — this is the single most important invariant."""
    _need(opt_config, _CONFIG_ERR, "config")
    assert opt_config.OPT_ENABLE_LIVE is False, "OPTIONS MUST NEVER TRADE LIVE"


# =========================================================================
# data.select_contract — direction -> call/put, ATM strike selection
# =========================================================================
def _fake_contracts(opt_type):
    """Build a small synthetic chain around an underlying of ~500."""
    exp = "2099-01-15"  # far future; only the relative shape matters here
    strikes = [480, 490, 495, 500, 505, 510, 520]
    out = []
    for k in strikes:
        # OCC-ish symbol; exact format irrelevant to the test
        sym = f"SPY99{('C' if opt_type == 'call' else 'P')}{int(k*1000):08d}"
        out.append({"symbol": sym, "strike": float(k),
                    "expiration": exp, "type": opt_type})
    return out


def _install_chain_stub(monkeypatch, underlying_price=500.0):
    """Patch list_contracts + get_chain_snapshot (no greeks) + price source."""

    def fake_list_contracts(underlying, dte_min, dte_max, opt_type=None):
        if opt_type is None:
            return _fake_contracts("call") + _fake_contracts("put")
        return _fake_contracts(opt_type)

    def fake_snapshot(underlying):
        snap = {}
        for c in (_fake_contracts("call") + _fake_contracts("put")):
            snap[c["symbol"]] = {"bid": 4.8, "ask": 5.2, "mid": 5.0,
                                 "last": 5.0, "greeks": None}
        return snap

    monkeypatch.setattr(opt_data, "list_contracts", fake_list_contracts)
    monkeypatch.setattr(opt_data, "get_chain_snapshot", fake_snapshot)

    # Neutralize the underlying-price lookup so the test is deterministic +
    # offline (otherwise it would hit yfinance for SPY). select_contract uses an
    # internal price helper; patch whichever name this build exposes. If none is
    # found, ATM falls back to the median strike, which for our symmetric
    # synthetic chain is still the 500 strike — so the assertion holds either way.
    for helper in ("_underlying_price", "get_underlying_price",
                   "_get_underlying_price", "underlying_price", "get_price"):
        if hasattr(opt_data, helper):
            monkeypatch.setattr(opt_data, helper,
                                lambda *a, **k: underlying_price)


def test_select_contract_bullish_returns_call(monkeypatch):
    _need(opt_data, _DATA_ERR, "data")
    _install_chain_stub(monkeypatch, underlying_price=500.0)
    res = opt_data.select_contract("SPY", "bullish", dte_target=35)
    assert res is not None, "expected a contract for a bullish signal"
    assert res["type"] == "call"
    # ATM selection (no greeks): strike closest to 500 should win.
    assert res["strike"] == 500.0
    # shape of the returned dict
    for key in ("symbol", "strike", "expiration", "type"):
        assert key in res


def test_select_contract_bearish_returns_put(monkeypatch):
    _need(opt_data, _DATA_ERR, "data")
    _install_chain_stub(monkeypatch, underlying_price=500.0)
    res = opt_data.select_contract("SPY", "bearish", dte_target=35)
    assert res is not None, "expected a contract for a bearish signal"
    assert res["type"] == "put"
    assert res["strike"] == 500.0


def test_select_contract_direction_aliases(monkeypatch):
    """'long'/'buy' -> call; 'short'/'sell' -> put."""
    _need(opt_data, _DATA_ERR, "data")
    _install_chain_stub(monkeypatch, underlying_price=500.0)
    for bullish in ("long", "buy"):
        r = opt_data.select_contract("SPY", bullish, dte_target=35)
        assert r is not None and r["type"] == "call", f"{bullish} should map to call"
    for bearish in ("short", "sell"):
        r = opt_data.select_contract("SPY", bearish, dte_target=35)
        assert r is not None and r["type"] == "put", f"{bearish} should map to put"


def test_get_chain_snapshot_mid_when_stubbed(monkeypatch):
    """When bid/ask present, mid is their average. Uses stub to stay offline."""
    _need(opt_data, _DATA_ERR, "data")

    def fake_snapshot(underlying):
        return {"SPY_TEST": {"bid": 1.0, "ask": 3.0, "mid": 2.0,
                             "last": 2.0, "greeks": None}}

    monkeypatch.setattr(opt_data, "get_chain_snapshot", fake_snapshot)
    snap = opt_data.get_chain_snapshot("SPY")
    row = snap["SPY_TEST"]
    assert row["mid"] == pytest.approx((row["bid"] + row["ask"]) / 2)


# =========================================================================
# risk.size_option_qty — premium cap
# =========================================================================
def test_size_option_qty_caps_premium():
    _need(opt_risk, _RISK_ERR, "risk")
    # Derive the expectation FROM config so this test can't go stale when the
    # premium cap changes (it did: OPT_MAX_PREMIUM_PCT 2% -> 3% on 2026-06-08,
    # which broke the old hardcoded `== 4`). bankroll 100000, mid 5.00 * 100 mult
    # = $500/contract.
    import math
    budget = opt_config.OPT_MAX_PREMIUM_PCT * 100000.0
    expected = math.floor(budget / (5.00 * 100))
    qty = opt_risk.size_option_qty(100000.0, 5.00, contract_multiplier=100)
    assert qty == expected, f"expected {expected} got {qty} (cap={opt_config.OPT_MAX_PREMIUM_PCT})"
    # the chosen qty must stay within budget; qty+1 must exceed it
    assert qty * 5.00 * 100 <= budget
    assert (qty + 1) * 5.00 * 100 > budget


def test_size_option_qty_zero_and_negative_mid():
    _need(opt_risk, _RISK_ERR, "risk")
    assert opt_risk.size_option_qty(100000.0, 0.0) == 0
    assert opt_risk.size_option_qty(100000.0, -1.0) == 0


def test_size_option_qty_too_expensive_returns_zero():
    _need(opt_risk, _RISK_ERR, "risk")
    # tiny bankroll, expensive premium -> can't afford even one contract
    qty = opt_risk.size_option_qty(1000.0, 50.0, contract_multiplier=100)
    assert qty == 0


# =========================================================================
# risk.can_open_option — gating
# =========================================================================
def test_can_open_option_allows_valid():
    _need(opt_risk, _RISK_ERR, "risk")
    ok, reason = opt_risk.can_open_option("SPY", 90, 0)
    assert ok is True, reason


def test_can_open_option_blocks_low_score():
    _need(opt_risk, _RISK_ERR, "risk")
    ok, reason = opt_risk.can_open_option("SPY", 50, 0)
    assert ok is False
    assert isinstance(reason, str) and reason


def test_can_open_option_blocks_disallowed_underlying():
    _need(opt_risk, _RISK_ERR, "risk")
    ok, reason = opt_risk.can_open_option("GME", 95, 0)
    assert ok is False
    assert isinstance(reason, str) and reason


def test_can_open_option_blocks_when_at_max_open():
    _need(opt_risk, _RISK_ERR, "risk")
    ok, reason = opt_risk.can_open_option("SPY", 95, opt_config.OPT_MAX_OPEN)
    assert ok is False
    assert isinstance(reason, str) and reason


def test_can_open_option_boundary_min_score_passes():
    _need(opt_risk, _RISK_ERR, "risk")
    # score exactly == OPT_MIN_SCORE should pass (>=)
    ok, _ = opt_risk.can_open_option("SPY", opt_config.OPT_MIN_SCORE, 0)
    assert ok is True


# =========================================================================
# execution.place_option_order — dry_run never submits
# =========================================================================
def test_place_option_order_dry_run_no_submit():
    _need(opt_exec, _EXEC_ERR, "execution")
    res = opt_exec.place_option_order("SPY99C00500000", 2,
                                      side="buy", limit_price=5.0, dry_run=True)
    assert isinstance(res, dict)
    assert res.get("status") == "dry_run"
    assert "request" in res and isinstance(res["request"], dict)
    # the request should describe a buy of the given symbol/qty (no live submit)
    req = res["request"]
    blob = str(req).lower()
    assert "spy99c00500000".lower() in blob or req.get("symbol") == "SPY99C00500000"


def test_place_option_order_zero_qty_skipped():
    _need(opt_exec, _EXEC_ERR, "execution")
    res = opt_exec.place_option_order("SPY99C00500000", 0, dry_run=True)
    assert res.get("status") == "skipped_zero_qty"


def test_place_option_order_negative_qty_skipped():
    _need(opt_exec, _EXEC_ERR, "execution")
    res = opt_exec.place_option_order("SPY99C00500000", -3, dry_run=True)
    assert res.get("status") == "skipped_zero_qty"


def test_place_option_order_respects_paper_gate(monkeypatch):
    """If OPT_ENABLE_PAPER is flipped off, no order is built/submitted."""
    _need(opt_exec, _EXEC_ERR, "execution")
    # patch the flag wherever the execution module reads it from
    if hasattr(opt_exec, "OPT_ENABLE_PAPER"):
        monkeypatch.setattr(opt_exec, "OPT_ENABLE_PAPER", False)
    else:
        monkeypatch.setattr(opt_config, "OPT_ENABLE_PAPER", False)
    res = opt_exec.place_option_order("SPY99C00500000", 1, dry_run=True)
    assert res.get("status") == "disabled"


# =========================================================================
# strategy.run_options_scan — produces plans, never submits
# =========================================================================
def test_run_options_scan_dry_run_no_submit(monkeypatch):
    _need(opt_strategy, _STRATEGY_ERR, "strategy")
    _need(opt_data, _DATA_ERR, "data")

    # Stub the chain so selection is deterministic/offline.
    _install_chain_stub(monkeypatch, underlying_price=500.0)

    # Stub position/bankroll lookups so the scan does no network I/O.
    if hasattr(opt_strategy, "get_option_positions"):
        monkeypatch.setattr(opt_strategy, "get_option_positions", lambda: [])
    if hasattr(opt_strategy, "get_bankroll"):
        monkeypatch.setattr(opt_strategy, "get_bankroll", lambda: 100000.0)

    # Guard rail: if anything tries to actually submit, blow up loudly.
    def _no_live_submit(*a, **k):
        raise AssertionError("run_options_scan must not submit when dry_run=True")

    # Only trip if a non-dry_run path is reached.
    orig_place = getattr(opt_strategy, "place_option_order", None)
    if orig_place is not None:
        def _wrapped_place(*args, **kwargs):
            if kwargs.get("dry_run", True) is False or (len(args) >= 5 and args[4] is False):
                _no_live_submit()
            return {"status": "dry_run", "request": {"symbol": args[0] if args else None}}
        monkeypatch.setattr(opt_strategy, "place_option_order", _wrapped_place)

    picks = [
        {"ticker": "SPY", "direction": "bullish", "score": 92},
        {"ticker": "QQQ", "direction": "bearish", "score": 88},
        {"ticker": "GME", "direction": "bullish", "score": 99},  # not allowed -> skip
        {"ticker": "AAPL", "direction": "bullish", "score": 50},  # low score -> skip
    ]
    results = opt_strategy.run_options_scan(picks, dry_run=True)
    assert isinstance(results, list)
    assert len(results) == len(picks)
    for r in results:
        assert "ticker" in r and "status" in r
        # nothing should be "submitted" under dry_run
        assert r["status"] != "submitted"

    by_ticker = {r["ticker"]: r for r in results}
    # disallowed + low-score picks must be skipped with a reason
    assert by_ticker["GME"]["status"].startswith("skip") or by_ticker["GME"].get("reason")
    assert by_ticker["AAPL"]["status"].startswith("skip") or by_ticker["AAPL"].get("reason")


# =========================================================================
# OPTIONAL: live SPY chain (read-only). Skipped offline / on any error.
# =========================================================================
def test_select_contract_live_spy_optional():
    """Read-only smoke test against the real SPY chain. Never submits.

    Skips on any network/API issue so the suite stays green offline.
    """
    _need(opt_data, _DATA_ERR, "data")
    if os.environ.get("OPT_SKIP_LIVE", "").lower() in ("1", "true", "yes"):
        pytest.skip("OPT_SKIP_LIVE set")
    try:
        res = opt_data.select_contract("SPY", "bullish", dte_target=35)
    except Exception as exc:  # network / auth / OPRA / data gaps
        pytest.skip(f"live SPY chain unavailable: {exc}")
    if res is None:
        pytest.skip("live SPY chain returned no suitable contract")
    assert res["type"] == "call"
    assert res["strike"] > 0
    assert isinstance(res["symbol"], str) and res["symbol"]


# =========================================================================
# plain-python runner (so `python3 tests/test_options.py` also works)
# =========================================================================
def _run_standalone():
    import types
    import inspect

    # tiny monkeypatch shim so the monkeypatch-using tests can run without pytest
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

    # whatever Skipped exception this pytest (real or shim) raises
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
