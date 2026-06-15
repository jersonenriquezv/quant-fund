"""Phase 0 parity gate — prove the in-bot Dual Thrust brain reproduces the
validated harness trade-for-trade, BEFORE any live capital.

Design (non-circular):
  REFERENCE = the real harness ``backtest()`` (authoritative, the code that
              produced Sharpe ~1.999). Uses the harness's own inline signal.
  TEST      = the bot engine ``dual_thrust.replay_signals()`` driving an
              IDENTICAL fill loop (the harness's own open/close/flip state
              machine, reused verbatim from the harness module). Only the
              SIGNAL SOURCE differs — fills/sizing/fees are the harness's.

If the ported brain is faithful, every trade (entry_ts, exit_ts, side, entry,
exit, qty, pnl) matches and the metrics are byte-identical. Any divergence =
a bug in the port -> fix before live.

Run:  source venv/bin/activate && PYTHONPATH=. python scripts/dual_thrust_parity.py
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# --- import the real harness as the authoritative reference -----------------
HARNESS_DIR = os.path.expanduser("~/jesse-research/project")
if HARNESS_DIR not in sys.path:
    sys.path.insert(0, HARNESS_DIR)
try:
    import okx_revalidation as H  # noqa: E402
except Exception as e:  # pragma: no cover
    print(f"FATAL: cannot import harness from {HARNESS_DIR}: {e}")
    sys.exit(2)

from strategy_service.engines import dual_thrust as DT  # noqa: E402

CSV = os.path.join(HARNESS_DIR, "storage", "okx", "ETH-USDT-SWAP_6h.csv")


def load_okx_6h_csv() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()


def _fill_sim_with_engine(df6: pd.DataFrame, hp: dict):
    """The harness fill loop, driven by the ENGINE's signal stream.

    Copied from ``okx_revalidation.backtest`` (funding=None path). Reuses the
    harness constants + ``_metrics`` + ``_unreal`` so the ONLY thing under test
    is the engine's per-bar signal. Sizing uses the engine bar's own ATR.
    """
    o = df6["open"].to_numpy(float)
    c = df6["close"].to_numpy(float)
    h = df6["high"].to_numpy(float)
    low = df6["low"].to_numpy(float)
    ts = df6["timestamp"].to_numpy()

    # engine brain: per-bar signal + atr + anchor, keyed by timestamp
    bars = DT.replay_signals(ts, o, h, low, c, hp)
    bar_by_ts = {b.timestamp: b for b in bars}

    balance = H.START_BALANCE
    pos = None
    trades = []
    eq_ts, eq_val = [], []
    pend = None

    def close_pos(exit_price, reason, bar_ts):
        nonlocal balance, pos
        bal_before = balance
        pnl = pos["qty"] * (exit_price - pos["entry"]) * pos["side"]
        fees = (pos["entry"] + exit_price) * pos["qty"] * H.FEE_RATE
        net = pnl - fees
        balance += net
        trades.append({
            "entry_ts": pos["entry_ts"], "exit_ts": bar_ts, "side": pos["side"],
            "entry": pos["entry"], "exit": exit_price, "qty": pos["qty"],
            "pnl_net": net, "reason": reason,
            "ret": net / bal_before if bal_before else 0.0,
        })
        pos = None

    def open_pos(side, price, a, bar_ts):
        nonlocal pos
        stop = price - side * a * hp["stop_loss_atr_rate"]
        qty = (balance * H.RISK_PCT / 100.0) / abs(price - stop)
        pos = {"side": side, "entry": price, "stop": stop, "qty": qty,
               "entry_ts": bar_ts}

    upL, dnL = hp["up_length"], hp["down_length"]
    warm = max(upL, dnL, DT.ATR_PERIOD)
    for i in range(warm, len(c)):
        # (a) execute pending action at this bar's open
        if pend is not None:
            if pend[0] == "close" and pos is not None:
                close_pos(o[i], "flip", ts[i])
            elif pend[0] == "enter" and pos is None:
                open_pos(pend[1], o[i], pend[2], ts[i])
            pend = None

        # (b) intrabar SL
        if pos is not None:
            if pos["side"] == 1 and low[i] <= pos["stop"]:
                close_pos(pos["stop"], "sl", ts[i])
            elif pos["side"] == -1 and h[i] >= pos["stop"]:
                close_pos(pos["stop"], "sl", ts[i])

        # (c) signal from the ENGINE (the part under test)
        bar = bar_by_ts.get(int(ts[i]))
        price = c[i]
        if bar is None or bar.anchor_open is None or np.isnan(bar.atr):
            pend = None
            eq_ts.append(ts[i]); eq_val.append(balance + H._unreal(pos, price))
            continue
        sig = bar.sig
        a = bar.atr
        if pos is not None:
            pend = ("close",) if (sig != 0 and sig != pos["side"]) else None
        else:
            pend = ("enter", sig, a) if sig != 0 else None
        eq_ts.append(ts[i]); eq_val.append(balance + H._unreal(pos, price))

    equity = pd.DataFrame({"timestamp": eq_ts, "equity": eq_val})
    return H._metrics(equity, trades), trades


def _cmp_trades(ref, test) -> list[str]:
    diffs = []
    if len(ref) != len(test):
        diffs.append(f"trade count: ref={len(ref)} test={len(test)}")
    for i, (a, b) in enumerate(zip(ref, test)):
        for k in ("entry_ts", "exit_ts", "side"):
            if a[k] != b[k]:
                diffs.append(f"trade[{i}].{k}: ref={a[k]} test={b[k]}")
        for k in ("entry", "exit", "qty", "pnl_net"):
            if abs(float(a[k]) - float(b[k])) > 1e-6:
                diffs.append(f"trade[{i}].{k}: ref={a[k]:.8f} test={b[k]:.8f}")
    return diffs


def run_parity(tf: str) -> bool:
    hp = DT.DUAL_THRUST_PARAMS[tf]
    df6 = load_okx_6h_csv()
    df1d = H._resample_ohlc(df6, "1D")

    ref_metrics, ref_trades, _ = H.backtest(df6, df1d, hp=hp, funding=None)
    test_metrics, test_trades = _fill_sim_with_engine(df6, hp)

    diffs = _cmp_trades(ref_trades, test_trades)
    metric_keys = ["sharpe", "net_pct", "max_dd_pct", "trades", "win_rate",
                   "final_balance"]
    metric_diffs = [k for k in metric_keys
                    if abs(float(ref_metrics[k]) - float(test_metrics[k])) > 1e-6]

    print(f"\n=== Dual Thrust parity — {tf} (candles={len(df6)}) ===")
    print(f"  REF  (harness): trades={ref_metrics['trades']} "
          f"sharpe={ref_metrics['sharpe']} net%={ref_metrics['net_pct']} "
          f"final={ref_metrics['final_balance']}")
    print(f"  TEST (engine):  trades={test_metrics['trades']} "
          f"sharpe={test_metrics['sharpe']} net%={test_metrics['net_pct']} "
          f"final={test_metrics['final_balance']}")

    ok = not diffs and not metric_diffs
    if ok:
        print(f"  PARITY PASS ✅ — engine reproduces harness trade-for-trade "
              f"({len(ref_trades)} trades identical)")
    else:
        print("  PARITY FAIL ❌")
        for d in (diffs + [f"metric {k}" for k in metric_diffs])[:20]:
            print(f"    - {d}")
    return ok


def main():
    note = (df6_note := "NOTE: harness CSV only has ETH 6h candles; the 4h "
            "param set is replayed on the SAME 6h candles purely to exercise "
            "the engine's other param branch (NOT a 4h backtest).")
    results = {}
    for tf in ("6h", "4h"):
        if tf == "4h":
            print(f"\n{df6_note}")
        results[tf] = run_parity(tf)
    print()
    allok = all(results.values())
    print("OVERALL:", "PASS ✅" if allok else "FAIL ❌", results)
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
