# Dual Thrust ETH 6h — research + forward validation scripts

Durable backup of the standalone research harness for the `dual_thrust_eth`
strategy. The **live** copies run from `~/jesse-research/project/` (separate Jesse
research env, outside this repo); these tracked copies are the canonical source so
they survive a server loss. The forward CSV data is **not** backed up — it is fully
regenerable (the re-sim is deterministic and rebuilds it from fresh OKX candles).

Background, results, and decision gates: `docs/audits/jesse-strategy-research-2026-06-12.md`
and `docs/plans/engine2-dual-thrust.md`.

## Files
| File | Purpose |
|---|---|
| `okx_revalidation.py` | Phase 1/2. Faithful pandas Dual Thrust. Fidelity check vs Jesse (Binance from `jesse_db`) + OKX fixed-param gate + funding + Monte Carlo. Run: `python okx_revalidation.py` (Phase 1) / `--phase2`. |
| `forward_resim.py` | Phase 3/4. Re-runs the faithful strategy on fresh OKX candles, slices out-of-sample forward trades by `FREEZE_DATE`. Pings Telegram on new forward trade / decision-ready. Imports `okx_revalidation`. |
| `systemd/dual-thrust-forward.{service,timer}` | Weekly user-timer that runs `forward_resim.py`. |

## Run (uses the bot venv — needs ccxt/requests/pandas/psycopg2)
```bash
~/quant-fund/venv/bin/python ~/jesse-research/project/forward_resim.py
```
Notes:
- `forward_resim.py` only needs OKX REST (internet) + the bot `config/.env` for
  Telegram. It does NOT need `jesse_db` (only `okx_revalidation.run()`'s Binance
  fidelity leg does).
- Key params live at the top of each script: `HP` (frozen winner params),
  `FREEZE_DATE`, `DECISION_MIN_TRADES`, `DECISION_MAX_DAYS`, `DECISION_PF_BAR`.
- **OKX 6h MUST be UTC-aligned (`6Hutc`).** The Hong-Kong `6H` bar collapses the
  result (Sharpe 0.21 vs 2.0).

## Restore the scheduler on a fresh server
```bash
mkdir -p ~/jesse-research/project ~/.config/systemd/user
cp scripts/dual_thrust/okx_revalidation.py scripts/dual_thrust/forward_resim.py ~/jesse-research/project/
cp scripts/dual_thrust/systemd/dual-thrust-forward.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dual-thrust-forward.timer
loginctl enable-linger "$USER"   # so the timer fires while logged out
```
Verify: `systemctl --user list-timers dual-thrust-forward.timer`.
