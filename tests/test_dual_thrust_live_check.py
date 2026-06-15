"""Unit tests for the Dual Thrust live-check parsing (CI-safe — no network).

The live check itself (`scripts/dual_thrust_live_check.py`) hits OKX REST and
the harness; these tests cover only the pure OKX-row parser so a format change
is caught without network.
"""

import importlib.util
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "dual_thrust_live_check",
    os.path.join(os.path.dirname(__file__), "..", "scripts",
                 "dual_thrust_live_check.py"),
)


def _load_module():
    # Importing runs the harness import at module top; skip cleanly if absent.
    try:
        mod = importlib.util.module_from_spec(_SPEC)
        _SPEC.loader.exec_module(mod)
        return mod
    except SystemExit:
        pytest.skip("harness (~/jesse-research) not available in this env")


def test_rows_to_df_filters_unconfirmed_and_sorts():
    mod = _load_module()
    # OKX returns newest-first; one unconfirmed (confirm="0") must be dropped.
    rows = [
        ["3000", "12", "13", "11", "12.5", "5", "0", "0", "0"],  # unconfirmed
        ["2000", "10", "11", "9", "10.5", "4", "0", "0", "1"],
        ["1000", "9", "10", "8", "9.5", "3", "0", "0", "1"],
    ]
    df = mod._rows_to_df(rows)
    assert list(df["timestamp"]) == [1000, 2000]          # sorted oldest-first
    assert df["timestamp"].dtype == "int64"
    assert df["open"].iloc[0] == 9.0
    assert df["volume"].iloc[-1] == 4.0
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_rows_to_df_dedupes():
    mod = _load_module()
    rows = [
        ["2000", "10", "11", "9", "10.5", "4", "0", "0", "1"],
        ["2000", "10", "11", "9", "10.5", "4", "0", "0", "1"],  # dup ts
        ["1000", "9", "10", "8", "9.5", "3", "0", "0", "1"],
    ]
    df = mod._rows_to_df(rows)
    assert list(df["timestamp"]) == [1000, 2000]
