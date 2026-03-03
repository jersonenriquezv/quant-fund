# Strategy Service
> Última actualización: 2026-03-03
> Estado: implementado (completo, integrado en main.py)

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

### `strategy_service/order_blocks.py` — Order Blocks
- Bullish OB: última vela roja antes de impulso alcista + BOS
- Bearish OB: última vela verde antes de impulso bajista + BOS
- Entry: 50% del body de la vela
- Validación: volumen >1.5x promedio, máximo 48h de edad
- Deduplicación por break asociado

### `strategy_service/fvg.py` — Fair Value Gaps
- Gap de 3 velas donde wick de vela 1 no toca wick de vela 3
- Tamaño mínimo: 0.1% del precio
- Expiración: 48 horas
- Tracking de fill parcial/total

### `strategy_service/liquidity.py` — Sweeps + Premium/Discount
- Detecta equal highs (BSL) y equal lows (SSL)
- Sweep: wick rompe nivel pero cierra dentro del rango
- Volumen mínimo 2x para confirmar sweep institucional
- Zonas premium (>50%), discount (<50%), equilibrium (50%)

### `strategy_service/setups.py` — Setup A/B + Confluencia
- **Setup A** (primario): Sweep + CHoCH + OB en discount/premium
- **Setup B** (secundario): BOS + FVG adyacente a OB
- Mínimo 2 confluencias obligatorio
- Cálculo de TP1 (1:1), TP2 (1:2), TP3 (trailing/liquidity)
- Validación premium/discount alignment

### `strategy_service/service.py` — Facade
- `StrategyService(data_service)` — obtiene candles del DataService
- `evaluate(pair, candle)` — evalúa LTF candles, retorna `TradeSetup | None`
- Coordina todos los módulos internos

### `strategy_service/__init__.py`
- Exporta `StrategyService`

## Tests
64 tests en 5 archivos:
- `test_market_structure.py` — swings, BOS, CHoCH
- `test_order_blocks.py` — detección, volumen, expiración, mitigación
- `test_fvg.py` — detección, fill, expiración
- `test_liquidity.py` — clustering, sweeps, premium/discount
- `test_setups.py` — Setup A/B, confluencia, TPs, PD alignment
