"""Parity gate — the bot's ported fill loop vs the harness reference.

Proves ``execution_service.dual_thrust_shadow.simulate_fills`` (the self-contained
port wired into the live shadow tracker) reproduces the harness fill model
(``scripts.dual_thrust_parity._fill_sim_with_engine``, which imports the
~/jesse-research harness) trade-for-trade on fresh OKX REST 4h candles.

Run locally before shipping shadow changes (needs network + the harness repo):
  source venv/bin/activate && PYTHONPATH=. python scripts/dual_thrust_shadow_parity.py
Exit 0 = PASS, 1 = mismatch, 2 = setup error.
"""

from __future__ import annotations

import sys

from scripts.dual_thrust_live_check import fetch_okx_bars
from scripts.dual_thrust_parity import _fill_sim_with_engine
from strategy_service.engines import dual_thrust as DT
from execution_service.dual_thrust_shadow import simulate_fills
from shared.models import Candle


def main() -> None:
    df = fetch_okx_bars("4H", n=1000)
    hp = DT.DUAL_THRUST_PARAMS["4h"]
    _, ref_trades = _fill_sim_with_engine(df, hp)
    candles = [Candle(timestamp=int(r.timestamp), open=r.open, high=r.high,
                      low=r.low, close=r.close, volume=r.volume,
                      volume_quote=0.0, pair="ETH/USDT", timeframe="4h",
                      confirmed=True) for r in df.itertuples()]
    mine = simulate_fills(candles, hp).trades

    diffs = 0
    for a, b in zip(ref_trades, mine):
        for k, bv in (("entry_ts", b.entry_ts), ("exit_ts", b.exit_ts), ("side", b.side)):
            if int(a[k]) != int(bv):
                diffs += 1
        for k, bv in (("entry", b.entry), ("exit", b.exit), ("pnl_net", b.pnl_net)):
            if abs(float(a[k]) - float(bv)) > 1e-6:
                diffs += 1

    ok = len(ref_trades) == len(mine) and diffs == 0
    print(f"harness trades={len(ref_trades)} port trades={len(mine)} field_diffs={diffs}")
    print("PARITY PASS ✅" if ok else "PARITY FAIL ❌")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
