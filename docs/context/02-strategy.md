# Strategy Service
> Última actualización: 2026-03-19
> Estado: implementado. **Post-review (2026-03-19):** ENABLED_SETUPS: setup_a, setup_c, setup_d_choch, setup_e, setup_f — 5 tipos activos. Setup H deshabilitado (11% WR, adverse selection). Setup B deshabilitado (audit 03-18). MIN_RISK_DISTANCE_PCT restaurado a 0.5% (0.8% mataba OBs válidos). **TP2 per-setup** via `SETUP_TP2_RR` dict: reversals (A,C,E) 2.0 R:R, continuations/scalps (B,D,F,G,H) 1.5 R:R. Expectancy filters: MIN_ATR_PCT 0.35%, MIN_TARGET_SPACE_R 1.4 (Optuna validated).

## Qué hace (30 segundos)
El Strategy Service es el detective del sistema. Analiza los datos del Data Service buscando patrones de Smart Money Concepts (SMC): rupturas de estructura (BOS/CHoCH), order blocks, fair value gaps, sweeps de liquidez, y zonas premium/discount. Cuando encuentra un setup con suficiente confluencia, genera un `TradeSetup` para evaluación.

## Por qué existe
El bot necesita reglas determinísticas para detectar oportunidades. Sin el Strategy Service, no hay señales de trading. Es 100% Python puro — sin IA, sin ML. Reglas claras y reproducibles.

## Archivos implementados

### `strategy_service/market_structure.py` — BOS/CHoCH
- Detecta swing highs/lows con lookback configurable
- BOS: precio cierra 0.1%+ más allá del swing previo (continuación)
- CHoCH: ruptura en dirección opuesta al trend (reversión)
- Requiere cierre de vela completo (no solo wick)
- **Solo un break por candle** — si una vela rompe múltiples niveles, solo se registra el más significativo (mayor distancia). Elimina ruido en flash crashes.

### `strategy_service/order_blocks.py` — Order Blocks + Breaker Blocks
- Bullish OB: última vela roja antes de impulso alcista + BOS
- Bearish OB: última vela verde antes de impulso bajista + BOS
- Entry: 50% del body de la vela (midpoint — balancea fill rate vs risk)
- Validación: volumen >1.5x promedio, máximo 48h de edad (overrideable via `max_age_hours` param — HTF campaigns use 168h/7 days)
- Deduplicación por break asociado
- **`break_timestamp`:** Cada OB almacena el timestamp de la vela que rompió estructura. La mitigación solo evalúa velas posteriores al `break_timestamp`, evitando que la propia vela de ruptura (o anteriores) invalide el OB prematuramente.
- **Breaker Blocks:** Cuando un OB es mitigado (precio cierra a través de él), se crea un breaker block con dirección invertida. Bullish OB mitigado → bearish breaker (resistencia). Bearish OB mitigado → bullish breaker (soporte). Almacenados en `_breaker_blocks` dict, accesibles via `get_breaker_blocks(pair, timeframe)`. Expiran después de `OB_MAX_AGE_HOURS`.

### `strategy_service/fvg.py` — Fair Value Gaps
- Gap de 3 velas donde wick de vela 1 no toca wick de vela 3
- Tamaño mínimo: 0.1% del precio
- Expiración: 48 horas (overrideable via `max_age_hours` param — HTF campaigns use 168h/7 days)
- Tracking de fill parcial/total

### `strategy_service/liquidity.py` — Sweeps + Premium/Discount
- Detecta equal highs (BSL) y equal lows (SSL) con tolerancia `EQUAL_LEVEL_TOLERANCE_PCT` (0.2% — ~$146 para BTC, ~$4.3 para ETH). Originalmente 0.05% ($36 BTC) — demasiado estricto, ETH 15m producía 1 nivel + 0 sweeps. Con 0.2%: 6 niveles + 4 sweeps.
- Sweep: wick rompe nivel pero cierre queda dentro del rango
- Volumen mínimo 2x para confirmar sweep institucional
- Zonas premium (>51%), discount (<49%), **equilibrium (49%-51%)** con banda de tolerancia configurable (`PD_EQUILIBRIUM_BAND` = 0.01)
- **Persistencia de swept status** — niveles que ya fueron sweepados mantienen su estado entre llamadas para evitar sweeps duplicados
- **Temporal guard:** Solo evalúa candles cuyo timestamp es > `max(level.timestamps)`. Previene que velas históricas (usadas para formar el nivel) lo "sweepeen" falsamente.

### `strategy_service/setups.py` — Swing Setups A/B/F/G + Confluencia
- **Setup A** (primario): Sweep + CHoCH + OB en discount/premium — **HABILITADO**
  - **Bidireccional**: LTF CHoCH/BOS determina dirección del trade. HTF bias es contexto, no un gate.
  - `REQUIRE_HTF_LTF_ALIGNMENT` default `False` — permite counter-trend setups con estructura LTF clara.
  - **`SETUP_A_MODE`** (env var, default `"both"`): Controls CHoCH vs HTF alignment. `"continuation"` = CHoCH must align with HTF. `"reversal"` = CHoCH must oppose HTF. `"both"` = no alignment check. Legacy `REQUIRE_HTF_LTF_ALIGNMENT` still respected in "both" mode.
  - **Orden temporal obligatorio**: sweep ANTES del CHoCH
  - **Proximidad temporal**: sweep dentro de `SETUP_A_MAX_SWEEP_CHOCH_GAP` candles del CHoCH (60 candles = ~300min en 5m, ~15h en 15m — aggressive mode: was 45)
  - **Entry depth configurable**: `SETUP_A_ENTRY_PCT` (default 0.65, env var override). Shallower entry for higher fill rate (Optuna 03-15: was 0.50).
  - **SL-too-close early filter**: `MIN_RISK_DISTANCE_PCT` (0.5%) check runs in strategy layer for ALL setups (A, D, E, F, G, H) before building TradeSetup. Also checked in Risk guardrails as backup. History: 0.2% → 0.5% → 0.8% (too aggressive, killed valid 15m OBs) → 0.5% restored.
  - **AI bypass**: In `AI_BYPASS_SETUP_TYPES` — AI filter skipped, synthetic AIDecision(confidence=1.0) generated. AI v2 had 89.6% approval rate = no value added.
  - **Backtest 60d aggressive**: 46 trades, 47.8% WR, +$2,510. El bottleneck principal era `no_aligned_sweep` — gap=20 solo producía 11 trades. Gap=40 captura sweeps más lejanos sin degradar calidad.
- **Setup B** (secundario): BOS + FVG adyacente a OB — **DESHABILITADO** (audit 03-18: 0-7.7% WR, F es estrictamente mejor — F = B sin gate de FVG débil)
  - Dirección BOS determina dirección del trade (bidireccional como Setup A)
  - **Entry: FVG 75%** `fvg.low + FVG_ENTRY_PCT * range` (bullish) / `fvg.high - FVG_ENTRY_PCT * range` (bearish) — shallower que midpoint para mayor fill rate. Configurable via `FVG_ENTRY_PCT` (default 0.75). SL ancho desde el OB wick.
  - SL: OB wick (igual que A/F)
  - FVG-OB adjacency threshold: `FVG_OB_MAX_GAP_PCT` (0.5%)
  - **Hardened filters (2026-03-13):** Root cause: accepted stale BOS hours after impulse move, placing zombie entries 2-3% from price.
    - BOS recency: must be within `SETUP_B_MAX_BOS_AGE_CANDLES` (30) candles (~7.5h on 15m, aggressive mode: was 12)
    - Entry distance: must be within `SETUP_B_MAX_ENTRY_DISTANCE_PCT` (4%, aggressive mode: was 2%) of current price
    - Direction bug fixed: entry branch now uses "bullish"/"bearish" (was "long" — never matched, affected bullish entry placement)
  - **Backtest 60d aggressive**: 55 trades, 52.7% WR, +$5,169. Antes con OB 75% entry (previo cambio): 29.8% WR, -$1,680.
- **Setup F** — Pure OB Retest: BOS + OB, sin FVG requerido — **HABILITADO** (aggressive validation mode 2026-03-15, historical 34.8% WR, hardened, params relajados: displacement 0.1%, min confluences 2)
  - Igual que Setup B pero sin necesitar FVG adyacente al OB
  - Dispara cuando hay BOS + OB alineados pero no hay FVG nearby
  - Evaluado después de B — si B matchea primero, F no se evalúa
  - **Hardened filters (2026-03-12):** Root cause of 34.8% WR: accepted stale BOS, unrelated OBs, inflated confluences with CVD/funding.
    - BOS recency: must be within `SETUP_F_MAX_BOS_AGE_CANDLES` (40) candles (~10h on 15m, aggressive mode: was 20)
    - BOS displacement: must exceed `SETUP_F_MIN_BOS_DISPLACEMENT_PCT` (0.1%, aggressive mode: was 0.2%) beyond broken level
    - OB-BOS temporal association: OB must be within `SETUP_F_MAX_OB_BOS_GAP_CANDLES` (20) candles of BOS (aggressive mode: was 10)
    - OB quality floor: composite score must be >= `SETUP_F_MIN_OB_SCORE` (0.35)
    - Entry distance: must be within `SETUP_F_MAX_ENTRY_DISTANCE_PCT` (5%, aggressive mode: was 3%) of current price
    - CVD and funding now included in confluences (audit 03-18: enriched signals via shared `_check_volume_confirmation`)
    - Minimum confluences: `SETUP_F_MIN_CONFLUENCES` (2, aggressive mode: was 3) — BOS + OB sufficient
  - DEBUG logs en cada early return (HTF undefined, no BOS, BOS too old, BOS displacement, BOS≠HTF, PD misaligned, no OBs near BOS, no OBs in range, OB score, entry distance, confluences, R:R)
- **Setup G** — Breaker Block Retest: OB mitigado con dirección invertida
  - Bullish OB mitigado → bearish breaker → short entry en retest
  - Bearish OB mitigado → bullish breaker → long entry en retest
  - DEBUG logs en cada early return (HTF undefined, no breakers, no aligned, PD misaligned, no in range, confluences, R:R)
  - Requiere HTF bias alineado con dirección del breaker + PD zone + min 2 confluencias
  - Usa `get_breaker_blocks()` de OrderBlockDetector
- **Swing setups evalúan solo 15m** — `SWING_SETUP_TIMEFRAMES = ["15m"]`. Detectors corren en 5m también (quick setups D/H los necesitan) pero swing setups (A/B/F/G) solo consideran OBs de 15m. `MIN_RISK_DISTANCE_PCT` (0.5%) filtra micro-SLs.
- **Zone-based orders** — no requiere proximidad al OB. El bot coloca limit orders al 50% del OB body (configurable via `SETUP_A_ENTRY_PCT`) y espera fill. SL siempre en `ob.low` (long) / `ob.high` (short) — wick-to-wick, independiente del entry.
  - `_find_best_ob()` selecciona by composite scoring via `_score_ob()`: volume (35%), freshness (30%), proximity (20%), body size (15%). Replaces old "highest volume_ratio + tiebreak by timestamp" selector.
  - `_score_ob()` returns -1 (filtered) for OBs below `OB_MIN_BODY_PCT` (0.15%) or beyond `OB_MAX_DISTANCE_PCT` (8%). Otherwise returns 0-1 composite score.
  - `OB_MIN_BODY_PCT` (0.15%) filters micro-OBs that produce tiny SLs eaten by commissions
  - `_is_ob_within_range()` filtra OBs más allá de `OB_MAX_DISTANCE_PCT` (8%) del precio actual
  - `_is_price_near_ob()` se mantiene para notificaciones de OB summary, pero no bloquea setups
- **SL direction validation** — `_validate_sl_direction()` en todos los setup types (A/B/F/G). Rechaza si SL está del lado incorrecto del entry (bearish: sl debe ser > entry, bullish: sl debe ser < entry). Fix para bug donde Setup B con FVG encima del OB producía entry > ob.high = SL invertido.
- Mínimo 2 confluencias obligatorio (no configurable — hardcoded)
- **`_check_volume_confirmation()`** — método compartido por todos los swing setups (A/B/F/G). Señales graduadas (v5):
  - OB volume ratio vs `OB_MIN_VOLUME_RATIO` (1.3)
  - **Sweep graduado**: 1.5-2.5x = 1 confluence, 2.5-4x = +`sweep_strong`, 4x+ = +`sweep_strong`+`sweep_extreme` (thresholds: `SWEEP_STRONG_VOLUME_RATIO`, `SWEEP_EXTREME_VOLUME_RATIO`)
  - OI flush events (boolean + USD amount)
  - **CVD divergence + magnitud**: divergencia precio↓/CVD↑ = señal más fuerte. MTF agreement (5m+15m+1h). Fallback a simple alignment. **Buy/sell dominance tiers**: 55%+ = `buy_dominance_moderate`, 60%+ = `buy_dominance_strong` (thresholds: `BUY_DOMINANCE_MODERATE_PCT`, `BUY_DOMINANCE_STRONG_PCT`)
  - **OI delta graduado**: trackea OI USD entre evaluaciones por pair. 0.5-2% = `oi_rising_mild`, 2-5% = `oi_rising_moderate`, 5%+ = `oi_rising_strong`. Dropping >2% = `oi_dropping_Xpct`. Raw `oi_delta_X.XXpct` siempre incluido para ML. (thresholds: `OI_DELTA_MILD_PCT`, `OI_DELTA_MODERATE_PCT`, `OI_DELTA_STRONG_PCT`)
  - **Funding graduado simétrico**: mild (0.01-0.03%) = 1 confluence CONTEXT, moderate (0.03-0.06%) = SUPPORTING, extreme (0.06%+) = 2 confluences. Labels: `funding_mild_long/short`, `funding_moderate_long/short`, `funding_extreme_long/short`. (thresholds: `FUNDING_MILD_THRESHOLD`, `FUNDING_MODERATE_THRESHOLD`, `FUNDING_EXTREME_THRESHOLD`)
- Cálculo de TP1 (1:1 R:R, breakeven trigger) y TP2 (**per-setup** via `SETUP_TP2_RR` dict, fallback `TP2_RR_RATIO`=2.0). Reversals (A, C, E): 2.0. Continuations/scalps (B, D, F, G, H): 1.5.
- **R:R simple** — `abs(tp2 - entry) / abs(entry - sl)` ≥ `MIN_RISK_REWARD`
- **Validación premium/discount** — equilibrium zone permite trades por defecto (`ALLOW_EQUILIBRIUM_TRADES = True`)
- **PD override diferido** — el check de PD alignment se difiere hasta después de contar confluencias. Si un setup tiene ≥ `PD_OVERRIDE_MIN_CONFLUENCES` (5) confluencias, puede operar contra la zona PD. Evita lockout total cuando bearish bias + discount zone bloquea todo. Log INFO cuando se activa override.
- **`PD_AS_CONFLUENCE`** (env var, default `false`): When true, PD zone becomes a confluence factor instead of a hard gate. Aligned PD adds `pd_zone_X` confluence; misaligned PD omits it but does NOT reject. Applied to all setups (A/B/D/F/G). Overrides both `REQUIRE_PD_ALIGNMENT` and `PD_OVERRIDE_MIN_CONFLUENCES` behavior when enabled.
### Split Entry (`entry2_price`)
Setups A/B/F calculan `entry2_price` via `_compute_entry2()` en `setups.py`:
- Bullish: `body_low + 0.25 × body_range` (25% from bottom = deeper into OB)
- Bearish: `body_high - 0.25 × body_range` (25% from top = deeper into OB)
- Si `body_range == 0`, fallback a `ob.entry_price`
- `entry2_price` se almacena en `TradeSetup` (default 0.0 = single entry)
- El execution service usa `entry2_price > 0` para decidir si colocar split entries (50/50 size)
- Solo para swing setups (A/B/F) en live mode (no quick setups, no sandbox)

### Expectancy Filters (`_apply_expectancy_filters`)

Post-detection filters aplicados a cada swing setup (A/B/F/G) antes de retornar:

1. **ATR filter** — rechaza si volatilidad (ATR 14 / entry_price) < `MIN_ATR_PCT` (0.35%). Restaurado a valor Optuna (audit 03-18: 0.20% dejaba pasar ruido de baja volatilidad).
2. **Target space filter** — rechaza si el swing high/low (1H/4H) más cercano en dirección del trade está a menos de `MIN_TARGET_SPACE_R` (1.4) veces el riesgo. Restaurado a valor Optuna (audit 03-18: 1.0 apenas filtraba).

### `strategy_service/quick_setups.py` — Quick Setups (C, D, E, H)
Data-driven setups con duración máxima 4h y R:R mínimo 1:1. Solo se disparan cuando no hay swing setup (A/B).

- **Setup C — Funding Squeeze: HABILITADO** (aggressive validation mode 2026-03-15). Funding rate extremo + CVD buy dominance alineado + HTF bias. Entry: precio actual. SL: 0.5%. TP1: 1:1 (breakeven trigger), TP2: per-setup R:R (single TP).
  - Long: funding < -0.03%, buy dominance > 55%
  - Short: funding > +0.03%, buy dominance < 45%
- **Setup D — LTF Structure Scalp:** CHoCH o BOS en 5m + OB fresco cerca del precio. No requiere sweep ni FVG. HTF bias + PD zone alineados. Entry: 50% del OB. TP1: 1:1 (breakeven trigger), TP2: per-setup R:R (single TP). Recibe `market_snapshot` para **CVD alignment** (agrega `cvd_aligned_*` + `buy/sell_dominance_strong` como confluences).
  - **Split into variants**: `setup_d_bos` and `setup_d_choch` for per-variant performance measurement. Variant determined by `latest_break.break_type`.
  - Both variants are in `QUICK_SETUP_TYPES` — skip AI filter, use short entry timeout (1h).
  - **setup_d_choch: HABILITADO** (75% WR in backtests)
  - **setup_d_bos: HABILITADO** (aggressive validation mode 2026-03-15, historical 20-33% WR, net negative — recolectando datos live)
  - **`SETUP_D_MIN_DISPLACEMENT_PCT`** (env var, default `0.0` = disabled): Filters weak BOS/CHoCH where `abs(break_price - broken_level) / broken_level` is below threshold. E.g. `0.002` = 0.2% minimum displacement to qualify.
  - **Backtest 60d solo**: 56 trades, 42.9% WR, +$3,596. Sharpe 8.51, PF 2.26, max DD 4.8%.
  - **Backtest 60d combinado A+B+D+F**: 9 trades D, 66.7% WR, +$2,553. Total combinado: 97 trades, 51.5% WR, +$7,558.
  - ETH dominante (47/56 trades solo, 97/97 combinado). BTC 11.1% WR pero solo 9 trades — muestra insuficiente.
- **Setup E — Cascade Reversal: HABILITADO** (aggressive validation mode 2026-03-15). Caída de OI >2% (cascade proxy) + CVD revertiendo. Long después de cascade de longs, short después de cascade de shorts. Usa OB cercano como anchor o precio actual. TP1: 1:1 (breakeven trigger), TP2: per-setup R:R (single TP).
- **Setup H — Momentum/Impulse: DESHABILITADO** (2026-03-19). 27 trades live, 11% WR, PF 0.10. Entry at impulse completion = adverse selection (AFML Ch.5). Code kept for recalibration. Was: impulsos direccionales con volumen en 5m/15m, entry at market price, SL at initiating OB.

**Diferencias clave quick vs swing:**
- Skip Claude AI filter (los datos SON la señal)
- Setup C skipea funding pre-filter (extreme funding ES el signal)
- R:R mínimo: 1.0 (vs 1.5 para swing)
- Timeout: 4h (vs 12h para swing)
- Cooldown: 1h por (par, tipo) para evitar re-triggering

### `strategy_service/service.py` — Facade
- `StrategyService(data_service)` — obtiene candles del DataService
- `evaluate(pair, candle)` — evalúa LTF candles: A → B → F → G → C → D → E, retorna `TradeSetup | None`
- **`evaluate_htf(pair, candle)`** — evalúa 4H candles para HTF campaigns. Usa Daily candles para bias (en vez de 4H/1H). Corre los mismos detectores SMC en 4H data con params más amplios: OB age 168h (vs 48h), OB distance 10% (vs 5%), FVG age 168h, min risk distance 0.5% (same as intraday). Overrides temporales de settings durante evaluación. Retorna `TradeSetup | None`. Gate: `HTF_ENABLED_SETUPS` (default: A, B, F).
- **`get_htf_swing_levels(pair)`** — retorna `(swing_highs, swing_lows)` de 4H data. Usado por CampaignMonitor para trailing SL.
- **`ENABLED_SETUPS` gate** — después de detectar un setup, verifica `setup.setup_type in settings.ENABLED_SETUPS`. Si no está habilitado, logea debug y continúa evaluando el siguiente tipo. **Post-review (2026-03-19):** `["setup_a", "setup_c", "setup_d_choch", "setup_e", "setup_f"]` — 5 tipos activos. Setup B deshabilitado (audit: F es mejor). Setup H deshabilitado (27 trades, 11% WR, PF 0.10). G pendiente de validación.
- Coordina todos los módulos internos
- Quick setup cooldown tracking per (pair, setup_type)
- **Failed OB tracking** — `mark_ob_failed(pair, sl_price, entry_price)` registra en memoria OBs que resultaron en pérdida (PnL < 0). `is_ob_failed(pair, sl_price, entry_price)` consulta el registro antes de ejecutar un nuevo trade: si el OB ya perdió, el setup se descarta. El tracking usa la clave `(pair, sl_price, entry_price)`. Breakeven (PnL = 0%) NO marca el OB como fallido porque el setup parcialmente funcionó. Se resetea en restart.

### `strategy_service/__init__.py`
- Exporta `StrategyService`

## Settings (config/settings.py)
- `SWING_SETUP_TIMEFRAMES: List[str] = ["15m", "5m"]` — timeframes para evaluación de swing setups (A/B/F/G). Aggressive mode: added 5m (was 15m only).
- `PD_EQUILIBRIUM_BAND: float = 0.01` — banda ±1% alrededor del 50% para zona equilibrium
- `OB_MIN_VOLUME_RATIO: float = 1.3` — 1.3x volumen promedio para validar OB (restaurado audit 03-18: 1.0 = deshabilitado, cualquier candle calificaba como OB)
- `OB_MAX_AGE_HOURS: int = 84` — horas máximas de vida de un OB (Optuna 03-15: was 72)
- `OB_PROXIMITY_PCT: float = 0.010` — 1.0% del precio como margen de proximidad al OB (aggressive mode: was 0.7% post-Optuna)
- `OB_MAX_DISTANCE_PCT: float = 0.08` — 8% máximo de distancia del precio al OB para zone-based orders (aggressive mode: reverted to 8%, Optuna had narrowed to 4%)
- `OB_MIN_BODY_PCT: float = 0.0015` — 0.15% minimum OB body size as fraction of price (Optuna 03-15: was 0.1%)
- `OB_SCORE_VOLUME_W / FRESHNESS_W / PROXIMITY_W / SIZE_W` — composite OB scoring weights (0.35 / 0.30 / 0.20 / 0.15, must sum to 1.0)
- `SETUP_A_ENTRY_PCT: float = 0.65` — fraction of OB body for Setup A entry placement (Optuna 03-15: was 0.50, higher fill rate)
- `SETUP_A_MODE: str = "both"` — Setup A CHoCH/HTF alignment mode: "continuation", "reversal", or "both" (env var)
- `SETUP_A_MAX_SWEEP_CHOCH_GAP: int = 60` — máximo candles entre sweep y CHoCH (aggressive mode: was 45 post-Optuna)
- `FVG_OB_MAX_GAP_PCT: float = 0.005` — 0.5% gap máximo entre FVG y OB para Setup B adjacency
- `SETUP_B_MAX_BOS_AGE_CANDLES: int = 30` — max candles since BOS (~7.5h on 15m, aggressive mode: was 12)
- `SETUP_B_MAX_ENTRY_DISTANCE_PCT: float = 0.04` — max entry distance from current price (4%, aggressive mode: was 2%)
- `REQUIRE_HTF_LTF_ALIGNMENT: bool = False` — si True, LTF debe alinearse con HTF; default False para bidireccional
- `REQUIRE_PD_ALIGNMENT: bool = True` — premium/discount zone debe alinear con dirección (core SMC)
- `PD_OVERRIDE_MIN_CONFLUENCES: int = 5` — setups con 5+ confluencias pueden override PD misalignment (evita lockouts totales en bearish+discount)
- `PD_AS_CONFLUENCE: bool = true` — PD zone as confluence instead of hard gate (aggressive mode: was false). Overrides PD_OVERRIDE behavior when true.
- `ALLOW_EQUILIBRIUM_TRADES: bool = True` — permitir trades en zona equilibrium
- `HTF_BIAS_REQUIRE_4H: bool = False` — si 4H debe definir trend o 1H solo basta
- `MIN_RISK_REWARD_QUICK: float = 1.0` — R:R mínimo para quick setups (C/D/E)
- `MAX_TRADE_DURATION_QUICK: int = 14400` — timeout 4h para quick setups
- `QUICK_SETUP_COOLDOWN: int = 3600` — cooldown 1h por (pair, setup_type). Mismo valor en default y aggressive — el perfil aggressive ya no reduce el cooldown.
- `SETUP_D_MIN_DISPLACEMENT_PCT: float = 0.0` — minimum BOS/CHoCH displacement for Setup D (0.0 = disabled, env var)
- `SETUP_F_MAX_BOS_AGE_CANDLES: int = 40` — max candles since BOS (~10h on 15m, aggressive mode: was 20)
- `SETUP_F_MAX_OB_BOS_GAP_CANDLES: int = 20` — max candle gap between OB and BOS (aggressive mode: was 10)
- `SETUP_F_MIN_BOS_DISPLACEMENT_PCT: float = 0.001` — min BOS displacement (0.1%, aggressive mode: was 0.2%)
- `SETUP_F_MIN_OB_SCORE: float = 0.35` — min composite OB score from `_score_ob()`
- `SETUP_F_MAX_ENTRY_DISTANCE_PCT: float = 0.05` — max entry distance from current price (5%, aggressive mode: was 3%)
- `SETUP_F_MIN_CONFLUENCES: int = 2` — min structural confluences (aggressive mode: was 3, now BOS + OB sufficient)
- `MOMENTUM_FUNDING_THRESHOLD: float = 0.0003` — umbral funding rate para Setup C
- `MOMENTUM_CVD_LONG_MIN: float = 0.52` — buy dominance mínimo para long (Setup C)
- `MOMENTUM_CVD_SHORT_MAX: float = 0.48` — buy dominance máximo para short (Setup C)
- `MOMENTUM_SL_PCT: float = 0.005` — SL distance 0.5% para Setup C
- `CASCADE_CVD_REVERSAL_LONG: float = 0.50` — buy dominance para reversal long (Setup E)
- `CASCADE_CVD_REVERSAL_SHORT: float = 0.50` — buy dominance para reversal short (Setup E)
- `CASCADE_MAX_AGE_SECONDS: int = 900` — cascade debe ser <15min (Setup E)
- **Graduated signal thresholds (v5):**
- `SWEEP_STRONG_VOLUME_RATIO: float = 2.5` — sweep strong tier (extra confluence)
- `SWEEP_EXTREME_VOLUME_RATIO: float = 4.0` — sweep extreme tier (2 extra confluences)
- `OI_DELTA_MILD_PCT: float = 0.005` — OI rising mild (0.5%)
- `OI_DELTA_MODERATE_PCT: float = 0.02` — OI rising moderate (2%)
- `OI_DELTA_STRONG_PCT: float = 0.05` — OI rising strong (5%)
- `BUY_DOMINANCE_MODERATE_PCT: float = 0.55` — buy/sell dominance moderate tier
- `BUY_DOMINANCE_STRONG_PCT: float = 0.60` — buy/sell dominance strong tier
- `FUNDING_MILD_THRESHOLD: float = 0.0001` — mild crowding (0.01%)
- `FUNDING_MODERATE_THRESHOLD: float = 0.0003` — moderate crowding (0.03%, was FUNDING_EXTREME_THRESHOLD)
- `FUNDING_EXTREME_THRESHOLD: float = 0.0006` — extreme crowding (0.06%)
- `ML_FEATURE_VERSION: int = 5` — v5: graduated signals

**HTF Campaign settings:**
- `HTF_CAMPAIGN_ENABLED: bool = False` — master switch para HTF campaigns (env var)
- `HTF_CAMPAIGN_SIGNAL_TF: str = "4h"` — timeframe para detección de setups
- `HTF_CAMPAIGN_BIAS_TF: str = "1d"` — timeframe para bias (Daily)
- `HTF_ENABLED_SETUPS: list = ["setup_a", "setup_b", "setup_f"]` — setups habilitados en HTF
- `HTF_OB_MAX_AGE_HOURS: int = 168` — 7 días (vs 48h intraday)
- `HTF_OB_MAX_DISTANCE_PCT: float = 0.10` — 10% (vs 5% intraday)
- `HTF_OB_PROXIMITY_PCT: float = 0.015` — 1.5% (vs 0.3% intraday)
- `HTF_FVG_MAX_AGE_HOURS: int = 168` — 7 días
- `HTF_MIN_RISK_DISTANCE_PCT: float = 0.005` — 0.5% (same as intraday)

## Backtester — Fidelidad con Ejecución Live

### Pending Replacement (per-pair)
- `TradeSimulator.pending` es un `dict[str, SimulatedTrade]` keyed por pair (no una lista)
- Un nuevo setup para el mismo pair reemplaza el pending anterior (igual que `ExecutionService.execute()`)
- Si el pair ya tiene un trade activo, el nuevo setup es rechazado
- Trades reemplazados se trackean como `exit_reason="pending_replaced"`

### Fill Model Configurable
- `--fill-mode optimistic` (default): touch = fill (candle.low ≤ entry para longs)
- `--fill-mode conservative`: precio debe penetrar entry por `--fill-buffer` (default 0.1%)
  - Long: `candle.low ≤ entry_price × (1 - buffer)`
  - Short: `candle.high ≥ entry_price × (1 + buffer)`
- Settings: `BACKTEST_FILL_MODE`, `BACKTEST_FILL_BUFFER_PCT`

### Execution Funnel
El report incluye sección EXECUTION FUNNEL con contadores:
- `pending_created` → `pending_replaced` + `pending_timeout` + `pending_filled`
- `fill_rate` = filled / created
- Per-setup breakdown: created, filled, timeout, replaced, fill_rate por setup type
- JSON output incluye `execution_funnel` y `execution_by_setup`

### Intrabar Approximation
Active trade management (SL/TP/breakeven/trailing) usa OHLC bars. No se puede determinar el orden de high/low dentro de una candle. SL se checkea antes que TP para coincidir con la prioridad live.

## AI Calibration en Backtester
El backtester soporta `--ai` flag para evaluar swing setups con Claude sobre data histórica. Permite medir si el AI filter agrega alpha comparando backtest con/sin AI.

- `python scripts/backtest.py --days 60 --ai` — activa Claude evaluation
- Quick setups (C/D/E) bypass Claude (igual que producción)
- Swing setups (A/B/F/G) pasan por pre-filter + Claude API
- Pre-filter: funding extreme, F&G extreme, CVD divergence (mismos checks que `main.py`)
- Report incluye sección AI CALIBRATION con approval rate, avg confidence
- JSON output incluye `ai_calibration` summary + `ai_decisions` list para análisis
- Filename con sufijo `_ai` para distinguir de baseline

## Tests
101 tests en 6 archivos:
- `test_market_structure.py` — swings, BOS, CHoCH, single break per candle
- `test_order_blocks.py` — detección, volumen, expiración, mitigación
- `test_fvg.py` — detección, fill, expiración
- `test_liquidity.py` — clustering, sweeps, premium/discount, equilibrium band, swept persistence
- `test_setups.py` — Setup A/B, confluencia, TPs, PD alignment, PD override, PD_AS_CONFLUENCE, SETUP_A_MODE, SL direction validation, simple R:R, OB proximity, temporal ordering, Setup F hardening (BOS age, displacement, OB-BOS gap, OB score, CVD/funding inclusion, entry distance, min confluences), Setup B hardening (BOS age, entry distance, direction bug fix)
- `test_strategy_integration.py` — Integration tests: OB volume threshold boundaries, funding graduated tiers (mild/moderate/extreme), CVD divergence vs boolean, OI delta graduated tiers, ENABLED_SETUPS gating, rejection reasons (HTF/sweep/CHoCH/PD/OB/confluences), signal hierarchy (core triggers vs confluence), expectancy filters (ATR/target space), Setup B vs F equivalence, confluence counting, quick setup signals (C/H)
- `test_quick_setups.py` — Setup C/D/E/H, cooldowns, R:R quick vs swing, AI bypass, data validation, SETUP_D_MIN_DISPLACEMENT_PCT, PD_AS_CONFLUENCE on Setup D, Setup H exhaustion filters
