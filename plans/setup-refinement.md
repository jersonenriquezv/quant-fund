# Setup Refinement — Tracking

## Estado General
- **Inicio:** 2026-03-10
- **Setups activos:** A, B, D, F
- **Objetivo:** Validar C, E (en ese orden). G descartado (6.2% WR). D habilitado (66.7% WR combinado). Backtester tiene MarketSnapshot.

## Fase 1: Setup A — Liquidity Sweep + CHoCH + OB
**Estado:** COMPLETADO

### Problema
Setup A disparaba pero muy poco (11 trades en 60d). El bottleneck principal era `no_aligned_sweep` (49K rechazos) — sweeps no alineaban temporalmente con CHoCH dentro de 20 candles.

### Diagnóstico
- [x] Mejorar logging en `evaluate_setup_a` (confluencia mínima + R:R)
- [x] Fix RejectTracker patterns en backtest.py
- [x] Backtest baseline con A habilitado (gap=20, aggressive)
- [x] Backtest con gap=40 (aggressive)

### Resultados Backtest (60 días, aggressive profile, $10K capital)

| Gap | A trades | A WR | A PnL | B trades | B WR | B PnL |
|-----|----------|------|-------|----------|------|-------|
| 20 | 11 | 54.5% | +$614 | 69 | 24.6% | -$2,579 |
| **40** | **46** | **47.8%** | **+$2,510** | 54 | 18.5% | -$3,075 |

### Decisión
**HABILITADO** con gap=40. WR>45% y PnL fuertemente positivo.
- `SETUP_A_MAX_SWEEP_CHOCH_GAP`: 20 → 40
- `ENABLED_SETUPS`: `["setup_a", "setup_b", "setup_f"]`
- Docs actualizados: `docs/context/02-strategy.md`

### Nota sobre Setup B
B mostró WR muy bajo (18-24%) en estos backtests. Investigación completa en Fase 1b abajo.

---

## Fase 1b: Setup B — BOS + FVG + OB
**Estado:** COMPLETADO — RE-HABILITADO con FVG midpoint entry

### Problema Original
Setup B cayó de ~45% WR a 18-24% WR. El cambio principal fue el entry del OB de 50% a 75% del body (commit 24edb24).

### Investigación
El entry del 75% es **GLOBAL** — se aplica en `order_blocks.py:_create_ob()`. El FVG adjacency filter selecciona OBs en zonas congestionadas → SL tight del 75% se barre constantemente.

### Fix Aplicado: FVG Midpoint Entry
**Opción 2** del plan original. En `evaluate_setup_b`, el entry cambió de `best_ob.entry_price` (75% OB body) a `(best_fvg.high + best_fvg.low) / 2` (FVG midpoint). SL sigue en OB wick.

Esto aprovecha el feature único de B (la FVG) como punto de entrada — entry más profundo en la zona de imbalance = SL más ancho = mejor supervivencia.

### Fix Adicional: SWING_SETUP_TIMEFRAMES
Swing setups (A/B/F/G) ahora solo evalúan OBs de 15m (`SWING_SETUP_TIMEFRAMES = ["15m"]`). OBs de 5m producían micro-SLs (<0.2%) que las comisiones se comían.

### Resultados Backtest (60d, aggressive, $10K)

**Setup B solo:**

| Metric | OB 75% entry | FVG midpoint entry |
|--------|-------------|-------------------|
| WR | 29.8% | **53.5%** |
| PnL | -$1,680 | **+$6,324** |
| Sharpe | -2.85 | **4.19** |
| Profit Factor | 0.77 | **1.86** |
| Max DD | 25.9% | **12.7%** |

**Combinado A+B+F:**

| Setup | Trades | WR | PnL |
|-------|--------|------|-------|
| A | 26 | 46.2% | -$1,038 |
| B | 55 | 52.7% | +$5,169 |
| F | 14 | 42.9% | +$407 |
| **Total** | **95** | **49.5%** | **+$4,538** |

### Decisión
**RE-HABILITADO.** `ENABLED_SETUPS: ["setup_a", "setup_b", "setup_f"]`. B es ahora el setup más rentable. Nota: A muestra -$1,038 en el combinado por competencia de slots — cuando A corría solo tenía +$2,510. Monitorear en live.

---

## Fase 2: Setup G — Breaker Block Retest
**Estado:** COMPLETADO — NO HABILITAR (6.2% WR, -$6,741)

### Backtest (60d, aggressive, $10K, G solo)
| Metric | Result |
|--------|--------|
| Trades | 81 |
| WR | 6.2% |
| PnL | -$6,741 |
| Max DD | 68.1% |
| Sharpe | -33.57 |

### Problemas Fundamentales
1. **Mismo breaker dispara repetidamente** — ETH 2056.59 entry aparece 15+ veces, BTC 72821.97 aparece 20 veces. Dedup TTL (1h) insuficiente para breakers que persisten 48h.
2. **100% longs** — HTF bias filter solo permite entrar long en momentos bullish breves durante un periodo bearish de 60d. Compra dips que siguen cayendo.
3. **Entry 75% de zona ya mitigada** — breaker blocks fueron fully breached por definición. 93.8% de trades hit SL.

### Decisión
**DESCARTADO.** El concepto de breaker blocks como S/R invertido es válido en SMC pero la implementación requiere un rediseño fundamental (no solo ajuste de parámetros). Posibles fixes futuros:
- Invalidar breaker después del primer SL hit
- Usar midpoint entry (como Setup B) en vez de 75%
- Remover HTF bias requirement para permitir shorts
- Pero dado que hay otros setups pendientes con mayor potencial, G se pospone indefinidamente.

---

## Fase 3: Setup D — LTF Structure Scalp
**Estado:** COMPLETADO — HABILITADO

### Backtest D solo (60d, aggressive, $10K)
| Metric | Result |
|--------|--------|
| Trades | 56 |
| WR | 42.9% |
| PnL | +$3,596 |
| Max DD | 4.8% |
| Sharpe | 8.51 |
| PF | 2.26 |

### Backtest combinado A+B+D+F (60d, aggressive, $10K)
| Setup | Trades | WR | PnL |
|-------|--------|------|-------|
| A | 20 | 45.0% | -$395 |
| B | 51 | 49.0% | +$3,647 |
| **D** | **9** | **66.7%** | **+$2,553** |
| F | 17 | 58.8% | +$1,753 |
| **Total** | **97** | **51.5%** | **+$7,558** |

### Notas
- D solo genera 9 trades en combinado (A/B/F tienen prioridad en 15m). Pero cuando dispara, WR 66.7%.
- BTC 11.1% WR en D solo (9 trades) — muestra insuficiente, monitorear en live.
- ETH domina (97/97 trades en combinado). Quick setup — skip AI filter por diseño.

### Decisión
**HABILITADO.** `ENABLED_SETUPS: ["setup_a", "setup_b", "setup_d", "setup_f"]`.

---

## Fase 4: Backtester MarketSnapshot
**Estado:** COMPLETADO

### Implementación
- **PostgreSQL:** 2 nuevas tablas: `funding_rate_history`, `open_interest_history`
- **ExchangeClient:** `fetch_funding_rate_history()` y `fetch_open_interest_history()` (ccxt)
- **DataService:** Polling loops persisten funding/OI a PostgreSQL automáticamente
- **BacktestDataService:** `get_market_snapshot()` retorna `MarketSnapshot` con funding + OI históricos (binary search por timestamp más cercano)
- **fetch_history.py:** Backfill automático de funding (~3 meses) y OI (1h resolution, ~30 días)

### Limitaciones
- **CVD:** No disponible históricamente (requeriría almacenar raw trades). Queda `None` en backtest.
- **OI resolution:** Solo 1h desde OKX (no 5m). Suficiente para confluencias y cascade detection.
- **OI range:** OKX limita a ~30 días para 1h, ~99 días para 1D.
- **Whales/Liquidations:** No disponibles históricamente. Quedan vacíos en backtest.

---

## Fase 5: Setup C — Funding Squeeze
**Estado:** PENDIENTE

---

## Fase 6: Setup E — Cascade Reversal
**Estado:** PENDIENTE

---

## Fase 7: B/F Polish
**Estado:** B COMPLETADO. F pendiente solo si se observa failure mode en live.

### Setup B
**RESUELTO** en Fase 1b. FVG midpoint entry → 52.7% WR, +$5,169. Re-habilitado.

### Setup F
F funciona (42.9% WR, +$407 en combinado). No tocar a menos que se observe un failure mode específico en logs live.
