"""Phase 1a — Dual Thrust live check (no bot wiring, zero risk).

Two jobs, on FRESH OKX candles fetched right now (i.e. data the optimizer never
saw — true out-of-sample):

  1. FRESH PARITY — re-run the engine vs the harness ``backtest()`` on current
     candles and confirm they still match trade-for-trade, with special focus on
     bars AFTER the optimization window end (2026-06-11). Proves the ported
     brain stays faithful on new data, not just the cached training window.

  2. CURRENT SIGNAL — what would Dual Thrust say *right now* for ETH 6h and 4h:
     price vs the upper/lower thrust, the raw long/short/flat signal, and how far
     price is from each trigger. This is the live brain in action.

Runnable weekly as the forward-validation heartbeat before any live capital.
Needs the harness repo (~/jesse-research) for the authoritative comparison.

Run:  source venv/bin/activate && PYTHONPATH=. python scripts/dual_thrust_live_check.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

HARNESS_DIR = os.path.expanduser("~/jesse-research/project")
if HARNESS_DIR not in sys.path:
    sys.path.insert(0, HARNESS_DIR)
try:
    import okx_revalidation as H  # noqa: E402
except Exception as e:  # pragma: no cover
    print(f"FATAL: cannot import harness from {HARNESS_DIR}: {e}")
    sys.exit(2)

from strategy_service.engines import dual_thrust as DT  # noqa: E402
from scripts.dual_thrust_parity import _fill_sim_with_engine, _cmp_trades  # noqa: E402

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
INST = "ETH-USDT-SWAP"
# OKX bar codes: 6h must be UTC-aligned ('6Hutc'); 4H is already UTC on OKX.
TF_BAR = {"6h": "6Hutc", "4h": "4H"}
# Optimization window end (params were fit up to here). Bars after = out-of-sample.
OOS_AFTER_MS = int(datetime(2026, 6, 11, tzinfo=timezone.utc).timestamp() * 1000)


def _rows_to_df(rows: list[list]) -> pd.DataFrame:
    """Parse raw OKX candle rows -> clean OHLCV df (oldest-first, confirmed only).

    OKX columns: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]. Pure +
    testable (no network). Keeps only ``confirm == "1"`` (closed) bars.
    """
    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "vol", "volccy",
        "volccyq", "confirm"])
    df = df[df["confirm"] == "1"]
    df = df.astype({"timestamp": "int64", "open": float, "high": float,
                    "low": float, "close": float, "vol": float})
    df = df.rename(columns={"vol": "volume"})
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def fetch_okx_bars(bar: str, n: int = 1000) -> pd.DataFrame:
    """Fetch the most recent ~n closed OKX candles, oldest-first, confirmed only.

    Paginates the history-candles endpoint (100/req) backwards via ``after``.
    """
    rows: list[list] = []
    after = ""
    while len(rows) < n:
        params = {"instId": INST, "bar": bar, "limit": "100"}
        if after:
            params["after"] = after
        data = requests.get(OKX_HISTORY_URL, params=params, timeout=15).json()
        batch = data.get("data", [])
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1][0]  # oldest ts in this batch -> next page older
        if len(batch) < 100:
            break
    return _rows_to_df(rows)


def fresh_parity(tf: str, df: pd.DataFrame) -> bool:
    hp = DT.DUAL_THRUST_PARAMS[tf]
    df1d = H._resample_ohlc(df, "1D")
    ref_m, ref_t, _ = H.backtest(df, df1d, hp=hp, funding=None)
    test_m, test_t = _fill_sim_with_engine(df, hp)
    diffs = _cmp_trades(ref_t, test_t)
    oos_ref = [t for t in ref_t if t["entry_ts"] > OOS_AFTER_MS]
    ok = not diffs and all(
        abs(float(ref_m[k]) - float(test_m[k])) <= 1e-6
        for k in ("sharpe", "net_pct", "trades", "final_balance"))
    print(f"\n--- FRESH PARITY {tf} (candles={len(df)}, "
          f"last={_fmt(df['timestamp'].iloc[-1])}) ---")
    print(f"  harness: trades={ref_m['trades']} sharpe={ref_m['sharpe']} "
          f"net%={ref_m['net_pct']}  | engine: trades={test_m['trades']} "
          f"sharpe={test_m['sharpe']} net%={test_m['net_pct']}")
    print(f"  out-of-sample trades (after 2026-06-11): {len(oos_ref)}")
    print(f"  {'PARITY PASS on fresh data' if ok else 'PARITY FAIL'} "
          f"{'OK' if ok else diffs[:5]}")
    return ok


def current_signal(tf: str, df: pd.DataFrame) -> None:
    hp = DT.DUAL_THRUST_PARAMS[tf]
    bars = DT.replay_signals(
        df["timestamp"].to_numpy(), df["open"].to_numpy(), df["high"].to_numpy(),
        df["low"].to_numpy(), df["close"].to_numpy(), hp)
    if not bars:
        print(f"\n=== CURRENT SIGNAL {tf}: insufficient history ===")
        return
    b = bars[-1]
    side = {1: "LONG", -1: "SHORT", 0: "FLAT"}[b.sig]
    print(f"\n=== CURRENT SIGNAL {tf} — bar close {_fmt(b.timestamp)} ===")
    print(f"  price={b.price:.2f}  anchor(1D open)={b.anchor_open:.2f}  "
          f"atr={b.atr:.2f}")
    if b.up_thrust is not None:
        up_gap = (b.up_thrust - b.price) / b.price * 100
        dn_gap = (b.price - b.down_thrust) / b.price * 100
        print(f"  upper_thrust={b.up_thrust:.2f} ({up_gap:+.2f}% away)  "
              f"lower_thrust={b.down_thrust:.2f} ({dn_gap:+.2f}% away)")
    print(f"  >>> SIGNAL: {side}")


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def main():
    print(f"Dual Thrust live check — {_fmt(int(datetime.now(timezone.utc).timestamp()*1000))}")
    results = {}
    for tf in ("6h", "4h"):
        df = fetch_okx_bars(TF_BAR[tf], n=1000)
        results[tf] = fresh_parity(tf, df)
        current_signal(tf, df)
    print()
    allok = all(results.values())
    print("OVERALL FRESH PARITY:", "PASS ✅" if allok else "FAIL ❌", results)
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
