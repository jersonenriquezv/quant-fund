# Strategy Service
> Última actualización: 2026-04-27
> Estado: SHADOW-only (`ENABLED_SETUPS=[]`). Todos los setups activos enrutan a shadow monitor para colectar ML data. ML_FEATURE_VERSION=17. Source of truth: `docs/SYSTEM_BASELINE.md`.

## Qué hace
Detecta patrones SMC determinísticos (BOS/CHoCH, OB, FVG, sweeps, premium/discount) sobre LTF candles (5m/15m). Cuando hay confluencia mínima genera un `TradeSetup`. 100% Python — sin IA, sin ML.

## Archivos

### `market_structure.py` — BOS/CHoCH
- Swing highs/lows con lookback configurable.
- BOS: cierre 0.1%+ más allá del swing previo (continuación).
- CHoCH: ruptura en dirección opuesta al trend (reversión).
- Requiere cierre completo (no wick).
- **Un break por candle** — si una vela rompe múltiples niveles, solo se registra el más significativo.

### `order_blocks.py` — Order Blocks + Breaker Blocks
- Bullish OB: última vela roja antes de impulso alcista + BOS. Bearish: vela verde antes de impulso bajista.
- Entry: 50% del body (midpoint).
- Validación: volumen >1.5x promedio, edad ≤ `OB_MAX_AGE_HOURS` (84h intraday, 168h HTF).
- `break_timestamp`: mitigación solo evalúa velas posteriores (evita auto-invalidación).
- `impulse_score` (0-1): fuerza del desplazamiento post-OB. Componentes 50/50 (precio + volumen). ≥0.6 → `ob_impulse_strong`, ≥0.35 → `ob_impulse_moderate`.
- `retest_count`: velas que wican dentro del OB sin mitigarlo. First-touch OBs son más fuertes.
- **Breaker Blocks**: OB mitigado → breaker con dirección invertida. Expiran a `OB_MAX_AGE_HOURS`.

### `fvg.py` — Fair Value Gaps
- Gap de 3 velas donde wick de vela 1 no toca wick de vela 3.
- Tamaño mínimo 0.1% del precio.
- Expiración 48h (HTF: 168h).

### `liquidity.py` — Sweeps + Premium/Discount
- Equal highs (BSL) / equal lows (SSL) con tolerancia `EQUAL_LEVEL_TOLERANCE_PCT` (0.2%).
- Sweep: wick rompe nivel pero cierre queda dentro. Volumen ≥2x para confirmar.
- Zonas premium (>51%), discount (<49%), equilibrium (49-51%) — banda `PD_EQUILIBRIUM_BAND` (0.01).
- `swept` status persiste entre llamadas.
- Temporal guard: solo evalúa candles posteriores a `max(level.timestamps)`.

### `setups.py` — Swing Setups (A, B, F, G)

Status actual (`SHADOW_MODE_SETUPS`): A, B, F. Live: ninguno (`ENABLED_SETUPS=[]`).

#### Setup A — Sweep + CHoCH + OB
- **Estado: SHADOW** (short-only via `SHADOW_DIRECTION_FILTER` — long 5% WR proven broken).
- Bidireccional: LTF CHoCH determina dirección. HTF bias = contexto.
- `SETUP_A_MODE` (env, default `"continuation"` — changed from `"both"` 2026-04-02 after 17/17 SL on counter-trend shadow): `"continuation"` (CHoCH alinea HTF), `"reversal"` (CHoCH opone HTF), `"both"` (sin check).
- Orden temporal obligatorio: sweep ANTES del CHoCH.
- Sweep dentro de `SETUP_A_MAX_SWEEP_CHOCH_GAP` (45 candles — Optuna-validated; was 60).
- `SETUP_A_MIN_SWEEP_TOUCH_COUNT` (3): rechaza sweeps de niveles 2-touch (noise).
- `SETUP_A_MIN_CHOCH_DISPLACEMENT_PCT` (0.002, 0.2%): rechaza micro-CHoCH.
- Entry: `SETUP_A_ENTRY_PCT` (0.50 — deepened 2026-04-02 from 0.65; shadow showed shallow entry kept SL within noise) del OB body.
- Entry distance cap: `SETUP_A_MAX_ENTRY_DISTANCE_PCT` (0.05 = 5%, added 2026-04-15 for consistency with B/F).
- AI bypass (en `AI_BYPASS_SETUP_TYPES`).

#### Setup B — BOS + FVG adyacente a OB
- **Estado: SHADOW**.
- Dirección BOS = dirección trade.
- Entry: FVG 75% (`FVG_ENTRY_PCT`). SL: OB wick.
- FVG-OB adjacency: `FVG_OB_MAX_GAP_PCT` (0.5%).
- BOS recency: `SETUP_B_MAX_BOS_AGE_CANDLES` (12 candles, ~1h en 5m).
- Entry distance: `SETUP_B_MAX_ENTRY_DISTANCE_PCT` (0.02 = 2%).
- AI bypass.

#### Setup F — Pure OB Retest (BOS + OB, sin FVG)
- **Estado: SHADOW**.
- Igual que B sin requerir FVG. Evaluado después de B.
- BOS recency: `SETUP_F_MAX_BOS_AGE_CANDLES` (60 candles, ~15h en 15m).
- BOS displacement: `SETUP_F_MIN_BOS_DISPLACEMENT_PCT` (0.001 = 0.1%).
- OB-BOS gap: `SETUP_F_MAX_OB_BOS_GAP_CANDLES` (20 candles).
- OB score floor: `SETUP_F_MIN_OB_SCORE` (0.35).
- Entry distance: `SETUP_F_MAX_ENTRY_DISTANCE_PCT` (0.025 = 2.5%, tightened 2026-04-15 from 5% — 3/5 unfilled_timeout in April shadow at 5%).
- Confluencias mínimas: `SETUP_F_MIN_CONFLUENCES` (2 — BOS + OB).
- DEBUG logs en cada early return.
- AI bypass.

#### Setup G — Breaker Block Retest
- **Estado: REMOVED 2026-04-16** (0/4 WR shadow). Código presente pero no en `SHADOW_MODE_SETUPS`.

### Lógica común swing setups
- **Swing OBs**: `SWING_OB_TIMEFRAMES = ["1h", "4h"]`. Swing setups consumen OBs 1H (primario) o 4H (fallback). 15m OBs producían SLs dentro del ruido.
- **Geometry cascade**: `_cascade_geometry()` prueba múltiples entry/SL (3 entries × 2 SLs = 6 max), selecciona mejor R:R. Entry candidates per-setup en `GEOMETRY_CASCADE_ENTRIES`. SL candidates: OB wick + ATR floor (`ATR_SL_FLOOR_MULTIPLIER × ATR(14)`). Early exit a R:R ≥ 3.0. Fallback rígido si `GEOMETRY_CASCADE_ENABLED=false`.
- **Orderbook depth**: `_enrich_with_ob_depth()` analiza L2 (20 niveles) sobre zona dinámica `max(OB body, ATR) × OB_DEPTH_ZONE_MULTIPLIER`. Ratio ≥1.0 + concentración ≥0.2 → confluencia `ob_depth_confirmed`. No es hard gate.
- **OB scoring** `_score_ob()`: composite (impulse 25%, volume 20%, freshness 20%, proximity 15%, retest penalty 10%, body size 10%). Filter floor: body ≥ `OB_MIN_BODY_PCT` (0.0015), distancia ≤ `OB_MAX_DISTANCE_PCT` (0.08).
- **Volume Profile** (`volume_profile.py`): aproximación VP desde 4H candles (200 bins). POC/VAH/VAL/HVNs/LVNs. Cache per-pair, recalcula con cada nueva 4H. Usado para TPs estructurales y `vp_*_confluence` en OB quality.
- **VP OB quality**: OB cerca POC (≤1×ATR) → `vp_poc_confluence`. OB cerca HVN → `vp_hvn_confluence`. OB en LVN → `vp_lvn_warning`.
- **SL direction validation**: bearish exige `sl > entry`, bullish exige `sl < entry`.
- **Confluencias mínimas**: 2 estructurales (hardcoded). Solo cuentan: BOS, CHoCH, FVG, order_block, liquidity_sweep, breaker_block, pd_zone, initiating_ob, bos_confirmed. Métricas (CVD/OI/funding/ratios) son features ML, NO inflan el gate.
- **`_check_volume_confirmation()`** — método compartido (A/B/F):
  - OB volume ≥ `OB_MIN_VOLUME_RATIO` (1.3x).
  - `impulse_score`: ≥0.6 → strong, ≥0.35 → moderate.
  - Sweep graduado: 1.5-2.5x → 1 conf, 2.5-4x → +`sweep_strong`, 4x+ → +`sweep_extreme`.
  - OI flush events (boolean + USD).
  - CVD divergence (precio↓/CVD↑) + MTF agreement (5m+15m+1h). Buy/sell dominance: 55%+ moderate, 60%+ strong.
  - OI delta graduado: 0.5-2% mild, 2-5% moderate, 5%+ strong. Dropping >2% = `oi_dropping_Xpct`.
  - Funding graduado simétrico: mild (0.01-0.03%), moderate (0.03-0.06%), extreme (0.06%+).
- **Structural TPs** (`STRUCTURAL_TP_ENABLED=true`): TPs apuntan a swing highs/lows 4H/1H, VP POC/VAH/VAL/HVNs, liquidity. TP1 = nivel más cercano (mín 1:1). TP2 = siguiente nivel. Min separation `STRUCTURAL_TP_MIN_SEPARATION_PCT` (0.3%). Fallback fixed R:R nunca degrada. Per-setup R:R fallback en `SETUP_TP2_RR`: A=2.0, B=2.0, F=2.0, G=2.0, D_bos=1.5, D_choch=1.5.
- **R:R simple**: `abs(tp2 - entry) / abs(entry - sl) ≥ MIN_RISK_REWARD`.
- **PD validation**: equilibrium permite trades (`ALLOW_EQUILIBRIUM_TRADES=True`).
- **PD override diferido**: setups con ≥`PD_OVERRIDE_MIN_CONFLUENCES` (5) operan contra PD.
- **`PD_AS_CONFLUENCE`** (env, default `true`): PD como confluence factor en vez de hard gate. Sobreescribe `REQUIRE_PD_ALIGNMENT` y `PD_OVERRIDE_*` cuando `true`.

### Split Entry (`entry2_price`)
Setups A/B/F calculan `entry2_price` via `_compute_entry2()`:
- Bullish: `body_low + 0.25 × body_range`. Bearish: `body_high - 0.25 × body_range`.
- Fallback `ob.entry_price` si `body_range == 0`.
- Execution coloca split 50/50 si `entry2_price > 0` (solo swing setups en live; no quick, no sandbox).

### Expectancy Filters (`_apply_expectancy_filters`)
Post-detection, pre-return:
1. **ATR filter**: rechaza si `ATR(14) / entry_price < MIN_ATR_PCT` (0.0035 = 0.35%).
2. **Target space filter**: rechaza si swing high/low (1H/4H) más cercano en dirección del trade < `MIN_TARGET_SPACE_R` (1.4) × riesgo.

### `quick_setups.py` — Quick Setups (D variants)

Status actual:
- **Setup D — LTF Structure Scalp**: CHoCH/BOS en 5m + OB fresco. HTF bias + PD alineados. Entry 50% OB. TP1 = 1:1 (BE trigger), TP2 = 1.5 R:R single TP. Recibe `market_snapshot` para CVD alignment.
  - Split en variantes: `setup_d_bos`, `setup_d_choch`. Determinado por `latest_break.break_type`.
  - Ambas en `QUICK_SETUP_TYPES` — skip AI, entry timeout 1h.
  - **Estado: SHADOW** (ambas).
  - `SETUP_D_MIN_DISPLACEMENT_PCT` (env, default 0.0 = disabled): filtra weak BOS/CHoCH.
  - **Structural TP**: `evaluate_setup_d` recibe `swing_highs_htf`, `swing_lows_htf`, `volume_profile`. TP2 snap a niveles estructurales si beats fixed R:R. Fallback `SETUP_TP2_RR["setup_d_*"]=1.5`.
- **Setup C — Funding Squeeze**: REMOVED 2026-04-13 (no OB anchor; señal ahora es confluence booster).
- **Setup E — Cascade Reversal**: REMOVED 2026-04-13 (no OB anchor).
- **Setup H — Momentum**: REMOVED 2026-04-13 (0/13 WR, retail momentum chase).

**Diferencias quick vs swing:**
- Skip Claude AI filter.
- R:R mínimo: `MIN_RISK_REWARD_QUICK` (1.0).
- Timeout: `MAX_TRADE_DURATION_QUICK` (4h, 14400s).
- Cooldown: `QUICK_SETUP_COOLDOWN` (1h por par/tipo).

### `service.py` — Facade
- `StrategyService(data_service)` — obtiene candles del DataService. Inicializa `VolumeProfileAnalyzer` si `VP_ENABLED`.
- `evaluate(pair, candle)` — evalúa LTF candles en orden: A → B → F → G → D. Retorna `TradeSetup | None`. Pasa swing_highs/lows HTF + volume_profile a swing setups.
- `evaluate_scalp(pair, trigger_candle)` — entry point del experimento scalp shadow. Gate `SCALP_SHADOW_ENABLED`, dedup `SCALP_DEDUP_WINDOW_SECONDS` por par. Pull `count=50` candles (warmup ADX). Orden: liq_reclaim → sweep_choch → vol_cvd_div → funding_extreme → random_baseline. Orderbook se cachea (`_get_cached_orderbook`, TTL `SCALP_ORDERBOOK_CACHE_TTL_SECONDS`) y se inyecta a `evaluate_sweep_choch` (filtro book_imbalance v2) y `evaluate_vol_cvd_divergence` (spread chaos).
- `evaluate_htf(pair, candle)` — evalúa 4H candles para HTF campaigns. Bias desde Daily. Detectores corren con params HTF (OB age 168h, distance 10%, FVG age 168h). Overrides temporales de settings durante evaluación. Gate: `HTF_ENABLED_SETUPS` (default: A, B, F).
- `get_htf_swing_levels(pair)` — `(swing_highs, swing_lows)` de 4H. Usado por CampaignMonitor para trailing SL.
- **`ENABLED_SETUPS` gate** — post-detection, verifica `setup.setup_type in settings.ENABLED_SETUPS`. Si no está habilitado → debug + continúa. **Actual: `[]`** (SHADOW-only). `SHADOW_MODE_SETUPS` enruta a shadow monitor.
- **`SHADOW_DIRECTION_FILTER`**: `{"setup_a": ["short"], "engine1_trend_pullback": ["short"]}` — setup_a long bloqueado en shadow (5% WR proven broken); Engine 1 v1b aislado a shorts. Outcome genérico: `shadow_direction_filtered`.
- **`SHADOW_PAIR_FILTER`**: quick setups `setup_d_*` siguen en BTC+ETH; Engine 1 v1b y benchmarks están aislados a `ETH/USDT`. Quarantenas fuera de scope usan `shadow_pair_filtered` cuando pasan por main.py; Engine 1 se pre-filtra antes de co-emitir benchmarks para evitar orphans.
- Cooldown tracking per (pair, setup_type) para quick setups.
- **Failed OB tracking** — `mark_ob_failed(pair, sl_price, entry_price)` registra OBs perdedores en memoria. `is_ob_failed(...)` consulta antes de ejecutar. Breakeven (PnL=0) NO marca como fallido. Resetea en restart.

## Settings (config/settings.py)

### Setup gating
- `ENABLED_SETUPS: list = []` — live execution gate (vacío = SHADOW-only).
- `SHADOW_MODE_SETUPS` includes legacy shadow setups plus redesign Engine 1 tracks. See `docs/SYSTEM_BASELINE.md` for the current authoritative list.
- `SHADOW_DIRECTION_FILTER = {"setup_a": ["short"], "engine1_trend_pullback": ["short"]}`.
- `ENGINE1_IMPULSE_GATE_ENABLED` (default `false`) + `ENGINE1_IMPULSE_GATE_MAX` (`2.24`) — gate low-impulse en `engines/trend_pullback.py`: cuando se habilita, suprime entradas con `impulse.atr_multiple > MAX` (Lane A 2026-06-08: low-impulse concentra el edge, PF v1d ~1.0→~4.5 OOS, 5/5 walk-forward). Default OFF = sin cambio de comportamiento. Filtra feature existente → no ML version bump. Encender solo tras validación forward. Plan: `docs/plans/engine1-entry-gate.md`.
- `QUICK_SETUP_TYPES = ("setup_c", "setup_d_bos", "setup_d_choch", "setup_e")` — `setup_c`/`setup_e` remain in the legacy tuple for compatibility but are removed from active/shadow setup lists.
- `AI_BYPASS_SETUP_TYPES = ("setup_a", "setup_b", "setup_f")`.

### Estructura SMC
- `SWING_OB_TIMEFRAMES = ["1h", "4h"]`.
- `SWING_SETUP_TIMEFRAMES = ["15m", "5m"]`.
- `OB_MIN_VOLUME_RATIO = 1.3`.
- `OB_MAX_AGE_HOURS = 84`.
- `OB_PROXIMITY_PCT = 0.010` (1%).
- `OB_MAX_DISTANCE_PCT = 0.08` (8%).
- `OB_MIN_BODY_PCT = 0.0015` (0.15%).
- `OB_MAX_RETESTS = 4`.
- `OB_SCORE_*_W` — composite weights (sum=1.0).
- `PD_EQUILIBRIUM_BAND = 0.01`.
- `BOS_CONFIRMATION_PCT = 0.001` (0.1%).

### Setup A
- `SETUP_A_ENTRY_PCT = 0.50` (env, deepened from 0.65 on 2026-04-02).
- `SETUP_A_MODE = "continuation"` (env, default; was `"both"` until 2026-04-02).
- `SETUP_A_MAX_SWEEP_CHOCH_GAP = 45` (Optuna-validated; was 60 until doc-truth sync 2026-04-27).
- `SETUP_A_MAX_ENTRY_DISTANCE_PCT = 0.05` (env, 5%; added 2026-04-15).
- `SETUP_A_MIN_SWEEP_TOUCH_COUNT = 3` (env).
- `SETUP_A_MIN_CHOCH_DISPLACEMENT_PCT = 0.002` (env).

### Setup B
- `SETUP_B_MAX_BOS_AGE_CANDLES = 12`.
- `SETUP_B_MAX_ENTRY_DISTANCE_PCT = 0.02` (2%, tightened from 3% on 2026-04-16).
- `FVG_OB_MAX_GAP_PCT = 0.005`.

### Setup F
- `SETUP_F_MAX_BOS_AGE_CANDLES = 60`.
- `SETUP_F_MAX_OB_BOS_GAP_CANDLES = 20`.
- `SETUP_F_MIN_BOS_DISPLACEMENT_PCT = 0.001` (0.1%).
- `SETUP_F_MIN_OB_SCORE = 0.35`.
- `SETUP_F_MAX_ENTRY_DISTANCE_PCT = 0.025` (2.5%, tightened from 5% on 2026-04-15).
- `SETUP_F_MIN_CONFLUENCES = 2`.

### Setup D
- `SETUP_D_MIN_DISPLACEMENT_PCT = 0.0` (env, 0 = disabled).

### Common
- `REQUIRE_HTF_LTF_ALIGNMENT = False`.
- `REQUIRE_PD_ALIGNMENT = True`.
- `PD_OVERRIDE_MIN_CONFLUENCES = 5`.
- `PD_AS_CONFLUENCE = True`.
- `ALLOW_EQUILIBRIUM_TRADES = True`.
- `HTF_BIAS_REQUIRE_4H = False`.
- `MIN_RISK_REWARD_QUICK = 1.0`.
- `MAX_TRADE_DURATION_QUICK = 14400` (4h).
- `QUICK_SETUP_COOLDOWN = 3600` (1h).
- `STRUCTURAL_TP_ENABLED = True`.
- `STRUCTURAL_TP_MIN_SEPARATION_PCT = 0.003` (0.3%).
- `MIN_ATR_PCT = 0.0035` (0.35%).
- `MIN_TARGET_SPACE_R = 1.4`.

### Per-setup TP2 R:R fallback
`SETUP_TP2_RR`: A=2.0, B=2.0, F=2.0, G=2.0, D_bos=1.5, D_choch=1.5. (C/E/H removidos.)

### Graduated signal thresholds
- `SWEEP_STRONG_VOLUME_RATIO = 2.5`, `SWEEP_EXTREME_VOLUME_RATIO = 4.0`.
- `OI_DELTA_MILD_PCT = 0.005`, `OI_DELTA_MODERATE_PCT = 0.02`, `OI_DELTA_STRONG_PCT = 0.05`.
- `BUY_DOMINANCE_MODERATE_PCT = 0.55`, `BUY_DOMINANCE_STRONG_PCT = 0.60`.
- `FUNDING_MILD_THRESHOLD = 0.0001`, `FUNDING_MODERATE_THRESHOLD = 0.0003`, `FUNDING_EXTREME_THRESHOLD = 0.0006`.

### ML
- `ML_FEATURE_VERSION = 17`.

### HTF Campaign
- `HTF_CAMPAIGN_ENABLED = False` (env).
- `HTF_CAMPAIGN_SIGNAL_TF = "4h"`, `HTF_CAMPAIGN_BIAS_TF = "1d"`.
- `HTF_ENABLED_SETUPS = ["setup_a", "setup_b", "setup_f"]`.
- `HTF_OB_MAX_AGE_HOURS = 168` (7d).
- `HTF_OB_MAX_DISTANCE_PCT = 0.10` (10%).
- `HTF_OB_PROXIMITY_PCT = 0.015` (1.5%).
- `HTF_FVG_MAX_AGE_HOURS = 168`.
- `HTF_MIN_RISK_DISTANCE_PCT = 0.005` (0.5%).

## Backtester — Fidelidad con Live

### Pending Replacement (per-pair)
- `TradeSimulator.pending: dict[str, SimulatedTrade]` keyed por pair.
- Nuevo setup mismo pair reemplaza pending anterior (mirror de `ExecutionService.execute()`).
- Trade activo en pair → setup rechazado.
- Reemplazos: `exit_reason="pending_replaced"`.

### Fill Model
- `--fill-mode optimistic` (default): touch = fill.
- `--fill-mode conservative`: requiere penetración por `--fill-buffer` (0.1% default).
- Settings: `BACKTEST_FILL_MODE`, `BACKTEST_FILL_BUFFER_PCT`.

### Execution Funnel
Report incluye contadores: `pending_created → replaced + timeout + filled`. Per-setup breakdown (created/filled/timeout/replaced/fill_rate). JSON con `execution_funnel`, `execution_by_setup`.

### Intrabar Approximation
SL/TP/BE/trailing usan OHLC. SL chequeado antes que TP (mirror live priority).

## AI Calibration en Backtester
`--ai` flag activa Claude evaluation:
- Quick setups bypass Claude (igual que prod).
- Swing setups (A/B/F) → pre-filter + Claude API.
- Pre-filter: funding extreme, F&G extreme, CVD divergence.
- Report: AI CALIBRATION (approval rate, avg confidence). JSON: `ai_calibration` + `ai_decisions`.
- Filename sufijo `_ai`.

## Tests
~203 tests en 7 archivos (strategy-focused):
- `test_market_structure.py` — swings, BOS, CHoCH, single break per candle.
- `test_order_blocks.py` — detección, volumen, expiración, mitigación, impulse score, retest count.
- `test_fvg.py` — detección, fill, expiración.
- `test_liquidity.py` — clustering, sweeps, premium/discount, equilibrium band, swept persistence.
- `test_setups.py` — A/B confluencia, TPs, PD alignment, PD override, PD_AS_CONFLUENCE, SETUP_A_MODE, SL direction, R:R, OB proximity, temporal ordering, F hardening (BOS age/displacement/gap/score), B hardening (BOS age/entry distance).
- `test_strategy_integration.py` — OB volume thresholds, funding/CVD/OI graduated tiers, ENABLED_SETUPS gating, rejection reasons, signal hierarchy, expectancy filters, B vs F equivalence, confluence counting.
- `test_quick_setups.py` — D variants, cooldowns, R:R, AI bypass, data validation, SETUP_D_MIN_DISPLACEMENT_PCT, PD_AS_CONFLUENCE on D.
