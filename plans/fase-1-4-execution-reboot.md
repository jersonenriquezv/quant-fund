# Plan: Bot Reboot — 4 Fases

## FASE 1: Execution Works (2-3 días)
**Objetivo**: El bot puede abrir posición con SL+TP correctamente en OKX live.

- [x] Simplificar exits: quitar TP1/TP2/TP3, un solo TP a 2:1 R:R por 100%
- [x] SL moves to breakeven cuando price cruza 1:1 R:R (poll ticker)
- [x] Margin mode: ISOLATED confirmado
- [x] Script de test live (`tests/test_execution_live.py`)
- [x] **Validar en live**: correr script, confirmar SL+TP visibles en OKX
- [x] Correr 2 trades via script → 2/2 con SL+TP attached en exchange (SL=conditional algo, TP=limit reduceOnly)
- [x] Bug fix: `risk_service` → `_risk_service` en main.py (crasheaba todo el pipeline de BTC)
- [x] SL+TP ahora van attached al entry order (OKX los crea atómicamente al fill)

## FASE 2: Strategy Backtest (1 semana)
**Objetivo**: Probar que los setups generan edge real en data histórica.

- [ ] Construir backtest engine (`tests/backtest.py`) con 3-6 meses de velas
- [ ] Evaluar cada setup: win rate, avg R:R, profit factor, max DD
- [ ] Priorizar Setup A y B. Desactivar los que no tengan >45% WR con 1.5:1 R:R
- [ ] Quick setups (C/D/E) desactivados hasta que A/B probados
- [ ] Mínimo 100 trades por setup

## FASE 3: Filtros Calibrados (3-5 días)
**Objetivo**: AI y Risk dejan pasar buenos trades (hoy bloquean ~95%).

- [ ] Relajar Risk basado en backtest (MIN_RISK_REWARD, COOLDOWN, etc.)
- [ ] Calibrar Claude: correr sobre setups históricos (ganadores vs perdedores)
- [ ] Forward test 1 semana: >40% de setups pasan filtros
- [ ] Done when: bot genera trades reales con filtros que agregan valor

## FASE 4: Trailing Exit (después de Fase 3)
**Objetivo**: Exit management simple y efectivo.

- [ ] Trailing SL: después de 1:1 R:R → SL a breakeven. Después de 1.5:1 → SL a 1:1
- [ ] Evaluar early exit signal basado en CHoCH contra posición
- [ ] TP fijo siempre existe como fallback en exchange
