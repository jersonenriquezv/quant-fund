# Setup Refinement — Tracking

## Estado General
- **Inicio:** 2026-03-10
- **Setups activos:** A, F
- **Objetivo:** Validar y habilitar G, D, C, E (en ese orden). Setup B deshabilitado — WR 18-24%.

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
**Estado:** PENDIENTE

---

## Fase 3: Setup D — LTF Structure Scalp
**Estado:** PENDIENTE

---

## Fase 4: Backtester MarketSnapshot
**Estado:** PENDIENTE

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
