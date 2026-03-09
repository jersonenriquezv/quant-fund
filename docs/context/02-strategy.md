# Strategy Service
> Última actualización: 2026-03-09 (OB entry 50%→75%)
> Estado: implementado (completo, integrado en main.py). Audited — 3 CRITICAL fixes applied. Quick Setups C/D/E added. Setups F/G added.

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
- Entry: 75% del body de la vela (más cerca del precio para mayor fill rate)
- Validación: volumen >1.5x promedio, máximo 48h de edad
- Deduplicación por break asociado
- **`break_timestamp`:** Cada OB almacena el timestamp de la vela que rompió estructura. La mitigación solo evalúa velas posteriores al `break_timestamp`, evitando que la propia vela de ruptura (o anteriores) invalide el OB prematuramente.
- **Breaker Blocks:** Cuando un OB es mitigado (precio cierra a través de él), se crea un breaker block con dirección invertida. Bullish OB mitigado → bearish breaker (resistencia). Bearish OB mitigado → bullish breaker (soporte). Almacenados en `_breaker_blocks` dict, accesibles via `get_breaker_blocks(pair, timeframe)`. Expiran después de `OB_MAX_AGE_HOURS`.

### `strategy_service/fvg.py` — Fair Value Gaps
- Gap de 3 velas donde wick de vela 1 no toca wick de vela 3
- Tamaño mínimo: 0.1% del precio
- Expiración: 48 horas
- Tracking de fill parcial/total

### `strategy_service/liquidity.py` — Sweeps + Premium/Discount
- Detecta equal highs (BSL) y equal lows (SSL) con tolerancia `EQUAL_LEVEL_TOLERANCE_PCT` (0.2% — ~$146 para BTC, ~$4.3 para ETH). Originalmente 0.05% ($36 BTC) — demasiado estricto, ETH 15m producía 1 nivel + 0 sweeps. Con 0.2%: 6 niveles + 4 sweeps.
- Sweep: wick rompe nivel pero cierre queda dentro del rango
- Volumen mínimo 2x para confirmar sweep institucional
- Zonas premium (>52%), discount (<48%), **equilibrium (48%-52%)** con banda de tolerancia configurable (`PD_EQUILIBRIUM_BAND`)
- **Persistencia de swept status** — niveles que ya fueron sweepados mantienen su estado entre llamadas para evitar sweeps duplicados
- **Temporal guard:** Solo evalúa candles cuyo timestamp es > `max(level.timestamps)`. Previene que velas históricas (usadas para formar el nivel) lo "sweepeen" falsamente.

### `strategy_service/setups.py` — Swing Setups A/B/F/G + Confluencia
- **Setup A** (primario): Sweep + CHoCH + OB en discount/premium
  - **Bidireccional**: LTF CHoCH/BOS determina dirección del trade. HTF bias es contexto para Claude, no un gate.
  - `REQUIRE_HTF_LTF_ALIGNMENT` default `False` — permite counter-trend setups con estructura LTF clara.
  - **Orden temporal obligatorio**: sweep ANTES del CHoCH
  - **Proximidad temporal**: sweep dentro de `SETUP_A_MAX_SWEEP_CHOCH_GAP` candles del CHoCH
- **Setup B** (secundario): BOS + FVG adyacente a OB
  - Dirección BOS determina dirección del trade (bidireccional como Setup A)
  - FVG-OB adjacency threshold: `FVG_OB_MAX_GAP_PCT` (0.5% — was 0.1%)
- **Setup F** — Pure OB Retest: BOS + OB, sin FVG requerido
  - Igual que Setup B pero sin necesitar FVG adyacente al OB
  - Dispara cuando hay BOS + OB alineados pero no hay FVG nearby
  - Evaluado después de B — si B matchea primero, F no se evalúa
- **Setup G** — Breaker Block Retest: OB mitigado con dirección invertida
  - Bullish OB mitigado → bearish breaker → short entry en retest
  - Bearish OB mitigado → bullish breaker → long entry en retest
  - Requiere HTF bias alineado con dirección del breaker + PD zone + min 2 confluencias
  - Usa `get_breaker_blocks()` de OrderBlockDetector
- **Zone-based orders** — no requiere proximidad al OB. El bot coloca limit orders al 75% del OB body y espera fill. SL siempre en `ob.low` (long) / `ob.high` (short) — wick-to-wick, independiente del entry.
  - `_find_best_ob()` selecciona por calidad: mayor `volume_ratio`, tiebreak por timestamp más reciente
  - `_is_ob_within_range()` filtra OBs más allá de `OB_MAX_DISTANCE_PCT` (5%) del precio actual
  - `_is_price_near_ob()` se mantiene para notificaciones de OB summary, pero no bloquea setups
- Mínimo 2 confluencias obligatorio (no configurable — hardcoded)
- Cálculo de TP1 (1:1), TP2 (1:2), TP3 (trailing/liquidity)
- **R:R blended** — validación ponderada: 50%×TP1 + 30%×TP2 + 20%×TP3 ≥ `MIN_RISK_REWARD`
- **Validación premium/discount** — equilibrium zone bloquea trades por defecto, configurable via `ALLOW_EQUILIBRIUM_TRADES`

### `strategy_service/quick_setups.py` — Quick Setups (C, D, E)
Data-driven setups con duración máxima 4h y R:R mínimo 1:1. Solo se disparan cuando no hay swing setup (A/B).

- **Setup C — Funding Squeeze:** Funding rate extremo + CVD buy dominance alineado + HTF bias. Entry: precio actual. SL: 0.5%. TPs: 1:1, 1.5:1, 2:1.
  - Long: funding < -0.03%, buy dominance > 55%
  - Short: funding > +0.03%, buy dominance < 45%
- **Setup D — LTF Structure Scalp:** CHoCH o BOS en 5m + OB fresco cerca del precio. No requiere sweep ni FVG. HTF bias + PD zone alineados. Entry: 75% del OB. TPs: 1:1, 1.5:1, 2:1.
- **Setup E — Cascade Reversal:** Caída de OI >2% (cascade proxy) + CVD revertiendo. Long después de cascade de longs, short después de cascade de shorts. Usa OB cercano como anchor o precio actual. TPs: 1:1, 1.5:1, 2:1.

**Diferencias clave quick vs swing:**
- Skip Claude AI filter (los datos SON la señal)
- Setup C skipea funding pre-filter (extreme funding ES el signal)
- R:R mínimo: 1.0 (vs 1.5 para swing)
- Timeout: 4h (vs 12h para swing)
- Cooldown: 1h por (par, tipo) para evitar re-triggering

### `strategy_service/service.py` — Facade
- `StrategyService(data_service)` — obtiene candles del DataService
- `evaluate(pair, candle)` — evalúa LTF candles: A → B → F → G → C → D → E, retorna `TradeSetup | None`
- Coordina todos los módulos internos
- Quick setup cooldown tracking per (pair, setup_type)

### `strategy_service/__init__.py`
- Exporta `StrategyService`

## Settings (config/settings.py)
- `PD_EQUILIBRIUM_BAND: float = 0.02` — banda ±2% alrededor del 50% para zona equilibrium
- `OB_PROXIMITY_PCT: float = 0.003` — 0.3% del precio como margen de proximidad al OB (solo para notificaciones)
- `OB_MAX_DISTANCE_PCT: float = 0.05` — 5% máximo de distancia del precio al OB para zone-based orders
- `SETUP_A_MAX_SWEEP_CHOCH_GAP: int = 20` — máximo candles entre sweep y CHoCH
- `FVG_OB_MAX_GAP_PCT: float = 0.005` — 0.5% gap máximo entre FVG y OB para Setup B adjacency
- `REQUIRE_HTF_LTF_ALIGNMENT: bool = False` — si True, LTF debe alinearse con HTF; default False para bidireccional
- `REQUIRE_PD_ALIGNMENT: bool = True` — premium/discount zone debe alinear con dirección (nunca se desactiva — core SMC)
- `ALLOW_EQUILIBRIUM_TRADES: bool = False` — permitir trades en zona equilibrium (aggressive: True)
- `HTF_BIAS_REQUIRE_4H: bool = True` — si 4H debe definir trend o 1H solo basta (aggressive: False)
- `MIN_RISK_REWARD_QUICK: float = 1.0` — R:R mínimo para quick setups (C/D/E)
- `MAX_TRADE_DURATION_QUICK: int = 14400` — timeout 4h para quick setups
- `QUICK_SETUP_COOLDOWN: int = 3600` — cooldown 1h por (pair, setup_type)
- `MOMENTUM_FUNDING_THRESHOLD: float = 0.0003` — umbral funding rate para Setup C
- `MOMENTUM_CVD_LONG_MIN: float = 0.55` — buy dominance mínimo para long (Setup C)
- `MOMENTUM_CVD_SHORT_MAX: float = 0.45` — buy dominance máximo para short (Setup C)
- `MOMENTUM_SL_PCT: float = 0.005` — SL distance 0.5% para Setup C
- `CASCADE_CVD_REVERSAL_LONG: float = 0.50` — buy dominance para reversal long (Setup E)
- `CASCADE_CVD_REVERSAL_SHORT: float = 0.50` — buy dominance para reversal short (Setup E)
- `CASCADE_MAX_AGE_SECONDS: int = 900` — cascade debe ser <15min (Setup E)

## Sistema de perfiles (`STRATEGY_PROFILE`)

El bot soporta 2 perfiles de estrategia, switcheables desde dashboard o env var:

| Perfil | Setups/día | Descripción |
|--------|-----------|-------------|
| `default` | ~1-2 | Conservador — todos los filtros activos, 4H requerido, R:R 1.5 |
| `aggressive` | ~3-5 | 1H suficiente para HTF, OB proximity 0.8%, R:R min 1.2, AI confidence 0.50, 10 trades/día, DD 5%/10% |

**Reglas que NUNCA cambian entre perfiles:**
- PD alignment (long=discount, short=premium) — core SMC
- AI filter obligatorio para swing setups (A/B/F/G) — todo trade pasa por Claude
- Quick setups (C/D/E) skip AI por diseño — los datos son la señal
- Max positions (5), max leverage (7x)

Los perfiles se definen en `STRATEGY_PROFILES` (config/settings.py) y se aplican via `apply_profile()`.

El perfil activo se almacena en Redis (`qf:bot:strategy_profile`) y se sincroniza al inicio de cada pipeline cycle en `main.py`.

## Tests
101 tests en 6 archivos:
- `test_market_structure.py` — swings, BOS, CHoCH, single break per candle
- `test_order_blocks.py` — detección, volumen, expiración, mitigación
- `test_fvg.py` — detección, fill, expiración
- `test_liquidity.py` — clustering, sweeps, premium/discount, equilibrium band, swept persistence
- `test_setups.py` — Setup A/B, confluencia, TPs, PD alignment, blended R:R, OB proximity, temporal ordering
- `test_quick_setups.py` — Setup C/D/E, cooldowns, R:R quick vs swing, AI bypass, data validation
