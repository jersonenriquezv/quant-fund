# Strategy Service
> Última actualización: 2026-03-11 (Expectancy filters ATR+target space, FVG_ENTRY_PCT configurable, backtester execution fidelity: pending replacement, fill modes, execution funnel.)
> Estado: implementado (completo, integrado en main.py). Audited — 3 CRITICAL fixes applied. Quick Setups C/D/E added. Setups F/G added. HTF campaign setup detection.

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
  - **Bidireccional**: LTF CHoCH/BOS determina dirección del trade. HTF bias es contexto para Claude, no un gate.
  - `REQUIRE_HTF_LTF_ALIGNMENT` default `False` — permite counter-trend setups con estructura LTF clara.
  - **Orden temporal obligatorio**: sweep ANTES del CHoCH
  - **Proximidad temporal**: sweep dentro de `SETUP_A_MAX_SWEEP_CHOCH_GAP` candles del CHoCH (40 candles = ~200min en 5m, ~10h en 15m)
  - **Backtest 60d aggressive**: 46 trades, 47.8% WR, +$2,510. El bottleneck principal era `no_aligned_sweep` — gap=20 solo producía 11 trades. Gap=40 captura sweeps más lejanos sin degradar calidad.
- **Setup B** (secundario): BOS + FVG adyacente a OB — **HABILITADO**
  - Dirección BOS determina dirección del trade (bidireccional como Setup A)
  - **Entry: FVG 75%** `fvg.low + FVG_ENTRY_PCT * range` (bullish) — shallower que midpoint para mayor fill rate. Configurable via `FVG_ENTRY_PCT` (default 0.75). SL ancho desde el OB wick.
  - SL: OB wick (igual que A/F)
  - FVG-OB adjacency threshold: `FVG_OB_MAX_GAP_PCT` (0.5%)
  - **Backtest 60d aggressive**: 55 trades, 52.7% WR, +$5,169. Antes con OB 75% entry (previo cambio): 29.8% WR, -$1,680.
- **Setup F** — Pure OB Retest: BOS + OB, sin FVG requerido
  - Igual que Setup B pero sin necesitar FVG adyacente al OB
  - Dispara cuando hay BOS + OB alineados pero no hay FVG nearby
  - Evaluado después de B — si B matchea primero, F no se evalúa
  - DEBUG logs en cada early return (HTF undefined, no BOS, BOS≠HTF, PD misaligned, no OBs, no OBs in range, confluences, R:R)
- **Setup G** — Breaker Block Retest: OB mitigado con dirección invertida
  - Bullish OB mitigado → bearish breaker → short entry en retest
  - Bearish OB mitigado → bullish breaker → long entry en retest
  - DEBUG logs en cada early return (HTF undefined, no breakers, no aligned, PD misaligned, no in range, confluences, R:R)
  - Requiere HTF bias alineado con dirección del breaker + PD zone + min 2 confluencias
  - Usa `get_breaker_blocks()` de OrderBlockDetector
- **Swing setups solo evalúan 15m OBs** — `SWING_SETUP_TIMEFRAMES = ["15m"]`. Los detectores corren en todos los LTF (15m + 5m) para que quick setups (C/D/E) tengan datos de 5m, pero la evaluación de A/B/F/G solo usa 15m. OBs de 5m producen micro-SLs (<0.2%) que las comisiones se comen.
- **Zone-based orders** — no requiere proximidad al OB. El bot coloca limit orders al 50% del OB body y espera fill. SL siempre en `ob.low` (long) / `ob.high` (short) — wick-to-wick, independiente del entry.
  - `_find_best_ob()` selecciona por calidad: mayor `volume_ratio`, tiebreak por timestamp más reciente
  - `_is_ob_within_range()` filtra OBs más allá de `OB_MAX_DISTANCE_PCT` (5%) del precio actual
  - `_is_price_near_ob()` se mantiene para notificaciones de OB summary, pero no bloquea setups
- Mínimo 2 confluencias obligatorio (no configurable — hardcoded)
- Cálculo de TP1 (1:1 R:R, breakeven trigger) y TP2 (2:1 R:R, single TP)
- **R:R simple** — `abs(tp2 - entry) / abs(entry - sl)` ≥ `MIN_RISK_REWARD`
- **Validación premium/discount** — equilibrium zone permite trades por defecto (`ALLOW_EQUILIBRIUM_TRADES = True`)
### Expectancy Filters (`_apply_expectancy_filters`)

Post-detection filters aplicados a cada swing setup (A/B/F/G) antes de retornar:

1. **ATR filter** — rechaza si volatilidad (ATR 14 / entry_price) < `MIN_ATR_PCT` (0.25%). Mercados laterales con rango < 0.25% no tienen espacio para que un trade sea rentable.
2. **Target space filter** — rechaza si el swing high/low (1H/4H) más cercano en dirección del trade está a menos de `MIN_TARGET_SPACE_R` (1.2) veces el riesgo. Si hay resistencia/soporte HTF demasiado cerca del entry, el TP no tiene espacio.

### `strategy_service/quick_setups.py` — Quick Setups (C, D, E)
Data-driven setups con duración máxima 4h y R:R mínimo 1:1. Solo se disparan cuando no hay swing setup (A/B).

- **Setup C — Funding Squeeze:** Funding rate extremo + CVD buy dominance alineado + HTF bias. Entry: precio actual. SL: 0.5%. TP1: 1:1 (breakeven trigger), TP2: 2:1 (single TP).
  - Long: funding < -0.03%, buy dominance > 55%
  - Short: funding > +0.03%, buy dominance < 45%
- **Setup D — LTF Structure Scalp:** CHoCH o BOS en 5m + OB fresco cerca del precio. No requiere sweep ni FVG. HTF bias + PD zone alineados. Entry: 50% del OB. TP1: 1:1 (breakeven trigger), TP2: 2:1 (single TP). — **HABILITADO**
  - **Backtest 60d solo**: 56 trades, 42.9% WR, +$3,596. Sharpe 8.51, PF 2.26, max DD 4.8%.
  - **Backtest 60d combinado A+B+D+F**: 9 trades D, 66.7% WR, +$2,553. Total combinado: 97 trades, 51.5% WR, +$7,558.
  - ETH dominante (47/56 trades solo, 97/97 combinado). BTC 11.1% WR pero solo 9 trades — muestra insuficiente.
- **Setup E — Cascade Reversal:** Caída de OI >2% (cascade proxy) + CVD revertiendo. Long después de cascade de longs, short después de cascade de shorts. Usa OB cercano como anchor o precio actual. TP1: 1:1 (breakeven trigger), TP2: 2:1 (single TP).

**Diferencias clave quick vs swing:**
- Skip Claude AI filter (los datos SON la señal)
- Setup C skipea funding pre-filter (extreme funding ES el signal)
- R:R mínimo: 1.0 (vs 1.5 para swing)
- Timeout: 4h (vs 12h para swing)
- Cooldown: 1h por (par, tipo) para evitar re-triggering

### `strategy_service/service.py` — Facade
- `StrategyService(data_service)` — obtiene candles del DataService
- `evaluate(pair, candle)` — evalúa LTF candles: A → B → F → G → C → D → E, retorna `TradeSetup | None`
- **`evaluate_htf(pair, candle)`** — evalúa 4H candles para HTF campaigns. Usa Daily candles para bias (en vez de 4H/1H). Corre los mismos detectores SMC en 4H data con params más amplios: OB age 168h (vs 48h), OB distance 10% (vs 5%), FVG age 168h, min risk distance 0.5% (vs 0.2%). Overrides temporales de settings durante evaluación. Retorna `TradeSetup | None`. Gate: `HTF_ENABLED_SETUPS` (default: A, B, F).
- **`get_htf_swing_levels(pair)`** — retorna `(swing_highs, swing_lows)` de 4H data. Usado por CampaignMonitor para trailing SL.
- **`ENABLED_SETUPS` gate** — después de detectar un setup, verifica `setup.setup_type in settings.ENABLED_SETUPS`. Si no está habilitado, logea debug y continúa evaluando el siguiente tipo. Default: `["setup_a", "setup_b", "setup_d", "setup_f"]`. D habilitado con 66.7% WR en combinado (+$2,553). C, E, G pendientes de validación. G descartado (6.2% WR).
- Coordina todos los módulos internos
- Quick setup cooldown tracking per (pair, setup_type)
- **Failed OB tracking** — `mark_ob_failed(pair, sl_price, entry_price)` registra en memoria OBs que resultaron en pérdida (PnL < 0). `is_ob_failed(pair, sl_price, entry_price)` consulta el registro antes de ejecutar un nuevo trade: si el OB ya perdió, el setup se descarta. El tracking usa la clave `(pair, sl_price, entry_price)`. Breakeven (PnL = 0%) NO marca el OB como fallido porque el setup parcialmente funcionó. Se resetea en restart.

### `strategy_service/__init__.py`
- Exporta `StrategyService`

## Settings (config/settings.py)
- `SWING_SETUP_TIMEFRAMES: List[str] = ["15m"]` — timeframes para evaluación de swing setups (A/B/F/G). Detectores corren en todos los LTF.
- `PD_EQUILIBRIUM_BAND: float = 0.01` — banda ±1% alrededor del 50% para zona equilibrium
- `OB_PROXIMITY_PCT: float = 0.008` — 0.8% del precio como margen de proximidad al OB (solo para notificaciones)
- `OB_MAX_DISTANCE_PCT: float = 0.08` — 8% máximo de distancia del precio al OB para zone-based orders
- `SETUP_A_MAX_SWEEP_CHOCH_GAP: int = 40` — máximo candles entre sweep y CHoCH (was 20, increased after backtest validation)
- `FVG_OB_MAX_GAP_PCT: float = 0.005` — 0.5% gap máximo entre FVG y OB para Setup B adjacency
- `REQUIRE_HTF_LTF_ALIGNMENT: bool = False` — si True, LTF debe alinearse con HTF; default False para bidireccional
- `REQUIRE_PD_ALIGNMENT: bool = True` — premium/discount zone debe alinear con dirección (nunca se desactiva — core SMC)
- `ALLOW_EQUILIBRIUM_TRADES: bool = True` — permitir trades en zona equilibrium
- `HTF_BIAS_REQUIRE_4H: bool = False` — si 4H debe definir trend o 1H solo basta
- `MIN_RISK_REWARD_QUICK: float = 1.0` — R:R mínimo para quick setups (C/D/E)
- `MAX_TRADE_DURATION_QUICK: int = 14400` — timeout 4h para quick setups
- `QUICK_SETUP_COOLDOWN: int = 3600` — cooldown 1h por (pair, setup_type). Mismo valor en default y aggressive — el perfil aggressive ya no reduce el cooldown.
- `MOMENTUM_FUNDING_THRESHOLD: float = 0.0003` — umbral funding rate para Setup C
- `MOMENTUM_CVD_LONG_MIN: float = 0.52` — buy dominance mínimo para long (Setup C)
- `MOMENTUM_CVD_SHORT_MAX: float = 0.48` — buy dominance máximo para short (Setup C)
- `MOMENTUM_SL_PCT: float = 0.005` — SL distance 0.5% para Setup C
- `CASCADE_CVD_REVERSAL_LONG: float = 0.50` — buy dominance para reversal long (Setup E)
- `CASCADE_CVD_REVERSAL_SHORT: float = 0.50` — buy dominance para reversal short (Setup E)
- `CASCADE_MAX_AGE_SECONDS: int = 900` — cascade debe ser <15min (Setup E)

**HTF Campaign settings:**
- `HTF_CAMPAIGN_ENABLED: bool = False` — master switch para HTF campaigns (env var)
- `HTF_CAMPAIGN_SIGNAL_TF: str = "4h"` — timeframe para detección de setups
- `HTF_CAMPAIGN_BIAS_TF: str = "1d"` — timeframe para bias (Daily)
- `HTF_ENABLED_SETUPS: list = ["setup_a", "setup_b", "setup_f"]` — setups habilitados en HTF
- `HTF_OB_MAX_AGE_HOURS: int = 168` — 7 días (vs 48h intraday)
- `HTF_OB_MAX_DISTANCE_PCT: float = 0.10` — 10% (vs 5% intraday)
- `HTF_OB_PROXIMITY_PCT: float = 0.015` — 1.5% (vs 0.3% intraday)
- `HTF_FVG_MAX_AGE_HOURS: int = 168` — 7 días
- `HTF_MIN_RISK_DISTANCE_PCT: float = 0.005` — 0.5% (vs 0.2% intraday)

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
- `test_setups.py` — Setup A/B, confluencia, TPs, PD alignment, simple R:R, OB proximity, temporal ordering
- `test_quick_setups.py` — Setup C/D/E, cooldowns, R:R quick vs swing, AI bypass, data validation
