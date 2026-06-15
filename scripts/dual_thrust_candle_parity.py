"""Candle-parity tracer — bot's PG candle store vs OKX REST (zero risk, no orders).

Born as the Dual Thrust Phase 1b-P1 tracer; generalized to the verification gate
for the partial-candle backfill fix (docs/plans/partial-candle-backfill-fix.md).

Per (pair, tf), over the overlapping window of the last N closed bars:

  1. CANDLE PARITY — bar-for-bar OHLC equality between the bot's PostgreSQL
     ``candles`` and OKX REST. Tolerance is float rounding only. This is the
     general data-integrity check (any pair/TF).

  2. SIGNAL PARITY — ``replay_signals`` on bot vs REST candles must be identical.
     Only runs for timeframes Dual Thrust is parameterized for (4h/6h); even tiny
     OHLC drift could flip a thrust-cross signal.

Places NO orders, touches no risk/execution path, does not write the DB.

Run:
  source venv/bin/activate && PYTHONPATH=. python scripts/dual_thrust_candle_parity.py
  ... --pair SOL/USDT --tf 1h
  ... --all            # loop 7 pairs x {4h,1h,15m}
Exit: 0 = all PASS, 1 = any mismatch, 2 = setup error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

from data_service.data_store import PostgresStore
from data_service.metadata import OKX_SWAP_INSTRUMENTS
from scripts.repair_partial_candles import TF_BAR, fetch_closed_candles
from strategy_service.engines import dual_thrust as DT

MIN_BARS = 200  # gate: >=200 overlapping bars compared
# Float tolerance: OKX serves OHLC as strings; bot parses to float. Allow only
# rounding-level drift — anything above this is a real divergence.
PRICE_EPS = 1e-6
DEFAULT_PAIRS = list(OKX_SWAP_INSTRUMENTS)
DEFAULT_TFS = ["4h", "1h", "15m"]  # 5m spot-checked separately (partials ~1%)


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _candles_to_df(candles) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [c.timestamp for c in candles],
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
    }).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def _signal_stream(df: pd.DataFrame, tf: str) -> dict[int, int]:
    """ts -> raw signal over the window. Uses the verbatim Dual Thrust brain."""
    hp = DT.DUAL_THRUST_PARAMS[tf]
    bars = DT.replay_signals(
        df["timestamp"].to_numpy(), df["open"].to_numpy(), df["high"].to_numpy(),
        df["low"].to_numpy(), df["close"].to_numpy(), hp)
    return {b.timestamp: b.sig for b in bars}


def check(pair: str, tf: str, store: PostgresStore) -> bool:
    """Run parity for one (pair, tf). Returns True on PASS. Prints a report."""
    bot_candles = store.load_candles(pair, tf, count=MIN_BARS + 100)
    if not bot_candles:
        print(f"\n--- {pair} {tf}: FAIL — no stored candles in PostgreSQL")
        return False
    bot = _candles_to_df(bot_candles)
    rest = _candles_to_df(fetch_closed_candles(pair, tf, count=MIN_BARS + 100))

    common = sorted(set(bot["timestamp"]) & set(rest["timestamp"]))
    if len(common) < MIN_BARS:
        print(f"\n--- {pair} {tf}: FAIL — only {len(common)} overlapping bars "
              f"(need >={MIN_BARS}). bot={len(bot)} rest={len(rest)}")
        return False

    common_set = set(common)
    b = bot[bot["timestamp"].isin(common_set)].sort_values("timestamp").reset_index(drop=True)
    r = rest[rest["timestamp"].isin(common_set)].sort_values("timestamp").reset_index(drop=True)

    # --- Check 1: bar-for-bar OHLC parity ---
    ohlc_diffs = []
    for i in range(len(b)):
        ts = int(b["timestamp"].iloc[i])
        for col in ("open", "high", "low", "close"):
            bv, rv = float(b[col].iloc[i]), float(r[col].iloc[i])
            if abs(bv - rv) > PRICE_EPS:
                ohlc_diffs.append((ts, col, bv, rv))

    print(f"\n--- CANDLE PARITY {pair} {tf} "
          f"({len(common)} bars, {_fmt(common[0])} -> {_fmt(common[-1])}) ---")
    if ohlc_diffs:
        print(f"  OHLC MISMATCHES: {len(ohlc_diffs)} (showing first 5)")
        for ts, col, bv, rv in ohlc_diffs[:5]:
            print(f"    {_fmt(ts)} {col}: bot={bv} rest={rv} (d={bv-rv:+.6f})")
    else:
        print("  OHLC: 0 mismatches ✅")

    # --- Check 2: signal-stream parity (DT timeframes only) ---
    sig_diffs = []
    if tf in DT.DUAL_THRUST_PARAMS:
        sig_bot = _signal_stream(b, tf)
        sig_rest = _signal_stream(r, tf)
        shared = sorted(set(sig_bot) & set(sig_rest))
        sig_diffs = [(ts, sig_bot[ts], sig_rest[ts])
                     for ts in shared if sig_bot[ts] != sig_rest[ts]]
        print(f"  SIGNAL PARITY (shared={len(shared)}): "
              + ("identical ✅" if not sig_diffs
                 else f"{len(sig_diffs)} MISMATCHES {sig_diffs[:5]}"))
    else:
        print("  SIGNAL PARITY: skipped (no Dual Thrust params for this TF)")

    ok = not ohlc_diffs and not sig_diffs
    print(f"  >>> {pair} {tf}: {'PASS ✅' if ok else 'FAIL ❌'} "
          f"(ohlc_diffs={len(ohlc_diffs)}, sig_diffs={len(sig_diffs)})")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description="Candle-parity tracer (PG vs OKX REST).")
    ap.add_argument("--pair", default="ETH/USDT")
    ap.add_argument("--tf", default="4h", choices=list(TF_BAR))
    ap.add_argument("--all", action="store_true",
                    help=f"loop {len(DEFAULT_PAIRS)} pairs x {DEFAULT_TFS}")
    args = ap.parse_args()

    print(f"Candle-parity tracer — "
          f"{_fmt(int(datetime.now(timezone.utc).timestamp()*1000))}")
    store = PostgresStore()
    if not store.connect():
        print("FATAL: cannot connect to PostgreSQL (is the bot DB up?)")
        sys.exit(2)

    if args.all:
        combos = [(p, tf) for p in DEFAULT_PAIRS for tf in DEFAULT_TFS]
    else:
        combos = [(args.pair, args.tf)]

    results = {f"{p} {tf}": check(p, tf, store) for p, tf in combos}
    allok = all(results.values())
    fails = [k for k, v in results.items() if not v]
    print(f"\nOVERALL: {'ALL PASS ✅' if allok else 'FAIL ❌ ' + str(fails)} "
          f"({sum(results.values())}/{len(results)} passed)")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
