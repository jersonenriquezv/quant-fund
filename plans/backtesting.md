# Backtesting — Sistema de Validacion Estadistica

**Status:** Pendiente
**Prioridad:** Alta
**Esfuerzo:** ~8-10 horas

## What

Construir un backtester propio (ni backtrader ni vectorbt) que replaye velas historicas candle-by-candle a traves del StrategyService existente, simule fills de entry/SL/TP, y produzca metricas reales de performance (win rate, PnL, max drawdown, Sharpe, profit factor).

## Why

El bot esta LIVE con ~$108 en OKX pero no tenemos validacion estadistica de que los setups (A, B, F, G, C, D, E) sean rentables. El backtester actual (`scripts/backtest.py`) solo cuenta cuantos setups detecta -- no simula fills ni calcula PnL. Necesitamos saber win rate, max drawdown y profit factor antes de escalar capital. Capital preservation #1.

## Decision: Ni backtrader ni vectorbt -- backtester propio

**Por que NO backtrader:**
- Proyecto abandonado. Sin release ni commits significativos en 3+ anios.
- Problemas con Python 3.10+ y dependencias modernas.
- Event-driven generico -- forzaria re-empaquetar toda la logica SMC en "Strategy" de backtrader, duplicando codigo.

**Por que NO vectorbt:**
- Diseniado para estrategias vectorizadas (cruces de medias, RSI). Los setups SMC son stateful y multi-timeframe -- requieren estado acumulado entre candles (OBs activos, FVGs, sweeps), incompatible con vectorizacion pura.
- La version free ya no se desarrolla activamente. PRO es de pago.

**Por que SI un backtester propio:**
- Ya existe el 80% del trabajo: `scripts/backtest.py` tiene `BacktestDataService`, `SimulatedClock`, replay candle-by-candle, y conecta con `StrategyService.evaluate()` directamente.
- **Zero duplicacion**: usa exactamente el mismo `StrategyService`, `MarketStructureAnalyzer`, `OrderBlockDetector`, `FVGDetector`, `LiquidityAnalyzer`, `SetupEvaluator` que el bot live.
- Solo falta: (1) simulador de fills, (2) tracker de PnL, (3) generador de metricas, (4) fetch de mas datos historicos.
- Complejidad: ~400-500 lineas de Python.

## Current State (verificado leyendo codigo)

### Lo que existe:
- **`scripts/backtest.py`** -- Backtester v0 funcional:
  - `BacktestDataService` -- mock del DataService con cursor temporal
  - `SimulatedClock` -- patchea `time.time()` para OB/FVG expiration
  - `RejectTracker` -- captura razones de rechazo via loguru sink
  - Replay candle-by-candle, warmup configurable, soporte de perfiles
  - **NO simula fills** -- solo cuenta setups detectados

### Datos disponibles en PostgreSQL:
| Par | TF | Candles | Periodo | Dias |
|-----|-----|---------|---------|------|
| BTC/USDT | 5m | 1,804 | Mar 2-9 | 6 |
| BTC/USDT | 15m | 935 | Feb 27 - Mar 9 | 10 |
| BTC/USDT | 1h | 606 | Feb 12 - Mar 9 | 25 |
| BTC/USDT | 4h | 526 | Dec 11 - Mar 9 | 88 |
| ETH/USDT | 5m | 1,804 | Mar 2-9 | 6 |
| ETH/USDT | 15m | 934 | Feb 27 - Mar 9 | 10 |
| ETH/USDT | 1h | 606 | Feb 12 - Mar 9 | 25 |
| ETH/USDT | 4h | 526 | Dec 11 - Mar 9 | 88 |

**Problema**: 6-10 dias de datos LTF no son suficientes para validacion estadistica. Necesitamos 60-90 dias minimo (5m y 15m).

## Steps

### Paso 1: Ampliar datos historicos → `scripts/fetch_history.py` (nuevo)
- Script standalone que use `ExchangeClient.backfill_candles()` para bajar 90 dias de velas en todos los TFs (5m, 15m, 1h, 4h) para BTC/USDT y ETH/USDT
- Almacenar en PostgreSQL via `PostgresStore.store_candles()` (ON CONFLICT ya maneja dedup)
- 90 dias de 5m = 25,920 candles por par. OKX da 100 por request = ~260 requests por par/tf
- Total estimado: ~4 minutos con rate limiting

**Done when:** `scripts/fetch_history.py --days 90` descarga y almacena 25K+ candles de 5m por par sin errores.

### Paso 2: Simulador de fills → `scripts/backtest.py` (extender)
Clase `TradeSimulator`:

```python
class TradeSimulator:
    """Simula fills de entry, SL, y TPs candle-by-candle."""
    def on_setup(self, setup: TradeSetup, candle: Candle) -> None
    def on_candle(self, candle: Candle) -> None
    def get_results(self) -> BacktestResults
```

**Logica de fill (replicando execution_service):**
1. **Entry**: Limit order al `setup.entry_price`. Fill cuando candle toca el precio. Timeout configurable.
2. **SL**: Stop-market. Prioridad maxima (check primero en cada candle).
3. **TP1 (50% @ 1:1 RR)**: Cerrar 50%, mover SL a breakeven.
4. **TP2 (30% @ 2:1 RR)**: Cerrar 30%.
5. **TP3 (20%)**: Cerrar restante.
6. **Timeout**: Si excede `MAX_TRADE_DURATION_SECONDS`, exit al close.
7. **Position sizing**: `(capital * RISK_PER_TRADE) / abs(entry - sl)`, capped por `MAX_LEVERAGE`.

**Done when:** `TradeSimulator` produce trades con entry/exit/PnL correctos. Test unitario con escenarios: SL hit, TP1 partial, full TP3, timeout.

### Paso 3: Metricas y reporte → `scripts/backtest.py` (extender)

**Metricas (alineadas con targets del CLAUDE.md):**
- Total trades / Win rate
- Average R:R realizado
- Total PnL (USD y %)
- Max drawdown (peak-to-trough en equity curve)
- Sharpe ratio (diario, anualizado sqrt(365))
- Profit factor (gross profit / gross loss)
- Trades por semana
- Breakdown por: setup type, par, direccion
- Distribucion de exit reasons (SL, TP1, TP2, TP3, timeout)

**Output:**
- Print a consola (como el backtester actual)
- CSV: `backtest_results_{date}.csv`

**Done when:** `python scripts/backtest.py --profile aggressive --days 60` produce reporte completo.

### Paso 4 (Fase 2): MarketSnapshot sintetico
- Almacenar funding rates historicos en PostgreSQL
- Fetch historico de OI via OKX REST
- CVD aproximado de volumen buy/sell en candle
- Poblar `MarketSnapshot` sintetico para que setups C/D/E funcionen en backtest

**Done when:** Backtest con `--with-snapshot` produce setups C/D/E.

### Paso 5 (Fase 2): Comparacion de perfiles
- Correr backtest automatico con ambos perfiles (default, aggressive)
- Tabla comparativa lado a lado
- Opcional: grid search sobre 2-3 parametros clave (OB_MIN_VOLUME_RATIO, MIN_RISK_REWARD, SWING_LOOKBACK)

**Done when:** `python scripts/backtest.py --compare-profiles` imprime tabla comparativa.

## Risks

| Riesgo | Impacto | Mitigacion |
|--------|---------|------------|
| **Overfitting** con 90 dias y 2 pares | Alto | Walk-forward: optimizar en 60 dias, validar en 30. NO cambiar parametros basado solo en backtest |
| **Sin MarketSnapshot** (fase 1) | Medio -- setups C/D/E no se testean | Fase 1 testea setups A/B/F/G (los principales). Fase 2 agrega snapshots |
| **Fill assumptions optimistas** | Medio -- backtest asume fill al limit price | SL fill al SL price (conservador). Futura mejoria: modelo de slippage |
| **OKX API rate limits** en fetch historico | Bajo | ccxt tiene throttling built-in. Script con retry |

## Out of Scope

- **Backtrader / vectorbt** -- decisiones explicadas arriba
- **Live paper trading mode** -- el bot ya tiene `OKX_SANDBOX=true`
- **Visualizacion de equity curve en dashboard** -- fase futura. CSV + consola por ahora
- **Comisiones de exchange** -- OKX cobra 0.02% maker / 0.05% taker. Impacto ~$0.04 por trade con $20 margin. Fase 2
- **Optimizacion genetica de parametros** -- con 2 pares y 90 dias, overfitting garantizado
