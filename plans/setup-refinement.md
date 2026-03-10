# Setup Refinement — Tracking

## Estado General
- **Inicio:** 2026-03-10
- **Setups activos:** B, F
- **Objetivo:** Validar y habilitar A, G, D, C, E (en ese orden)

## Fase 1: Setup A — Liquidity Sweep + CHoCH + OB
**Estado:** EN PROGRESO

### Problema
Setup A nunca dispara en backtest. Cadena de filtros demasiado estricta.

### Diagnóstico Pendiente
- [ ] Mejorar logging en `evaluate_setup_a` para contar rechazos por gate
- [ ] Correr backtest verbose con A habilitado → ver dónde muere
- [ ] Probar `SETUP_A_MAX_SWEEP_CHOCH_GAP`: 40 y 60 (actual: 20)
- [ ] Probar con perfil aggressive (sweep volume 1.5x vs 2.0x)

### Resultados Backtest
_(pendiente)_

### Decisión
_(pendiente — WR >= 40% y P&L >= 0 para habilitar)_

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
**Estado:** PENDIENTE (solo si se identifica mejora)
