# Strategy Service — CLAUDE.md

Operational rules for Claude when modifying `strategy_service/`. Lean by design — points to deeper sources instead of duplicating them.

## Purpose
Deterministic SMC pattern detection (BOS/CHoCH, OB, FVG, sweeps, premium/discount). Emits `TradeSetup` when confluence threshold is met. Pure Python, no ML, no AI.

## Source of truth (read before editing)
- **Detailed behavior:** `docs/context/02-strategy.md` (Spanish, deep — detector logic, setup definitions, common helpers, expectancy filters)
- **Active config / thresholds / setup status:** `docs/SYSTEM_BASELINE.md` — ENABLED_SETUPS, SHADOW_MODE_SETUPS, all numeric thresholds, changelog
- **Engines (redesign track):** `strategy_service/engines/` — Engine 1 (`engine1_trend_pullback`) lives here; benchmarks in repo root reports
- **Models:** `shared/models.py` — `TradeSetup`, `OrderBlock`, `FVG`, etc. ALWAYS read this before referencing fields

## Files
| File | Role |
|---|---|
| `service.py` | Facade. `evaluate(pair, candle)` (LTF) and `evaluate_htf(pair, candle)` (4H campaigns). Owns ENABLED_SETUPS gate, SHADOW_MODE routing, cooldowns, failed-OB tracking |
| `setups.py` | Swing setups A, B, F, G — confluence checks, geometry cascade, OB scoring, structural TPs |
| `quick_setups.py` | Quick setups D variants (`d_bos`, `d_choch`). C/E/H removed but tuple kept for compat |
| `market_structure.py` | Swing highs/lows, BOS, CHoCH (single break per candle) |
| `order_blocks.py` | OB detection, mitigation, breaker blocks, impulse score, retest count |
| `fvg.py` | Fair Value Gaps |
| `liquidity.py` | Equal highs/lows, sweeps, premium/discount zones |
| `volume_profile.py` | 4H VP — POC/VAH/VAL/HVN/LVN. Cached per-pair |
| `trade_classifier.py` | Setup type classification helper |
| `engines/` | Redesign engines (Engine 1 trend pullback). New strategies land here, not in `setups.py` |

## Rules — modifying detectors / setups
1. **Confluence gate is structural-only.** BOS, CHoCH, FVG, order_block, liquidity_sweep, breaker_block, pd_zone, initiating_ob, bos_confirmed count toward the 2-min gate. CVD/OI/funding/ratios are ML features, NOT confluences. Do not inflate the gate by adding metric-based confluences.
2. **SL direction validation is mandatory.** Bullish: `sl < entry`. Bearish: `sl > entry`. Any new setup must call `_check_sl_direction`.
3. **Min risk distance must be checked at strategy layer too.** `_check_sl_distance()` runs before risk_service. Both layers enforce `MIN_RISK_DISTANCE_PCT`.
4. **Geometry cascade is the way to compute entry/SL.** Use `_cascade_geometry()`; do not hardcode entry % or SL anchor in new setups. Cascade returns best R:R from candidates.
5. **Swing OBs only consume 1H/4H** (`SWING_OB_TIMEFRAMES`). 15m OBs produce SLs inside noise — banned for swing setups.
6. **OB scoring uses `_score_ob()` composite.** Do not bypass with custom scoring per setup. If a setup needs a different floor, set `SETUP_*_MIN_OB_SCORE`.
7. **Expectancy filters run last.** ATR filter + target space filter live in `_apply_expectancy_filters()`. New setups must route through it.
8. **Engines are isolated.** Engine 1 lives in `engines/engine1_trend_pullback.py`. Do not couple legacy setups to engines or vice versa.

## Rules — adding a new setup
1. Add detection function (or class) in the right file (`setups.py` for swing, `quick_setups.py` for quick, `engines/` for redesign engines).
2. Wire into `service.evaluate()` in evaluation order. Order matters — earlier setups can dedup later ones.
3. Add to `SHADOW_MODE_SETUPS` first. Never go directly to `ENABLED_SETUPS`. Collect ≥100 shadow outcomes or 30 days before promoting.
4. Add tests under `tests/test_setups.py` or `tests/test_quick_setups.py`. Cover: confluence pass/fail, SL direction, R:R, PD alignment, entry distance cap.
5. Add settings to `config/settings.py` and document in `docs/SYSTEM_BASELINE.md` §thresholds.
6. Update `docs/context/02-strategy.md` with detector behavior.

## Never
- Add detectors that depend on indicators not in `shared/ml_features.py` without adding the feature there too.
- Bypass `ENABLED_SETUPS` gate by checking shadow elsewhere.
- Hardcode setup-specific thresholds outside `config/settings.py`.
- Modify HTF detector params permanently — `evaluate_htf()` swaps settings temporarily; this pattern must stay.
- Touch `SHADOW_MODE_SETUPS` without updating SYSTEM_BASELINE §setup-status.

## Verify after changes
```bash
python -m pytest tests/test_setups.py tests/test_quick_setups.py tests/test_strategy_integration.py -v --tb=short
```

## Related
- ML feature extraction: `shared/ml_features.py` (must update `ML_FEATURE_VERSION` if columns change)
- Backtester: `scripts/backtest.py` — replays candles through `service.evaluate()`. Mirror live behavior; any divergence is a bug
