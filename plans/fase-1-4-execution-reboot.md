# Plan: Bot Reboot — 4 Fases

## FASE 1: Execution Works ✅
**Objetivo**: El bot puede abrir posición con SL+TP correctamente en OKX live.

- [x] Simplificar exits: quitar TP1/TP2/TP3, un solo TP a 2:1 R:R por 100%
- [x] SL moves to breakeven cuando price cruza 1:1 R:R (poll ticker)
- [x] Margin mode: ISOLATED confirmado
- [x] Script de test live (`tests/test_execution_live.py`)
- [x] **Validar en live**: correr script, confirmar SL+TP visibles en OKX
- [x] Correr 2 trades via script → 2/2 con SL+TP attached en exchange (SL=conditional algo, TP=limit reduceOnly)
- [x] Bug fix: `risk_service` → `_risk_service` en main.py (crasheaba todo el pipeline de BTC)
- [x] SL+TP ahora van attached al entry order (OKX los crea atómicamente al fill)

## FASE 2: Strategy Backtest ✅
**Objetivo**: Probar que los setups generan edge real en data histórica.

- [x] Construir backtest engine (`scripts/backtest.py`) con 90 días de velas
- [x] Evaluar cada setup: win rate, avg R:R, profit factor, max DD
- [x] Priorizar Setup B y F. Desactivar A/G (no rentables). `ENABLED_SETUPS = ["setup_b", "setup_f"]`
- [x] Quick setups (C/D/E) desactivados hasta que B/F validados
- [x] Resultados: B=56.8% WR, F=48.4% WR. MIN_RISK_DISTANCE_PCT calibrado a 0.2%

## FASE 3: Filtros Calibrados (en curso)
**Objetivo**: AI y Risk dejan pasar buenos trades (hoy bloquean ~95%).

- [x] Relajar Risk basado en backtest: MIN_RISK_DISTANCE_PCT 0.1%→0.2%, backtester incluye risk guardrails
- [x] Calibrar Claude: correr sobre setups históricos (ganadores vs perdedores) — `--ai` flag en backtester, pre-filter + Claude API evaluation
- [ ] Forward test 1 semana: >40% de setups pasan filtros — EN CURSO (bot corriendo live)
- [ ] Done when: bot genera trades reales con filtros que agregan valor

## FASE 4: Trailing Exit (mayormente completa)
**Objetivo**: Exit management simple y efectivo.

- [x] Breakeven: 1:1 R:R → SL a entry price (monitor.py `_check_breakeven` + backtest.py)
- [x] Trailing SL: 1.5:1 R:R → SL a tp1 (monitor.py `_check_trailing_sl` + backtest.py)
- [x] TP fijo siempre existe como fallback en exchange (tp2 a 2:1 R:R)
- [ ] Evaluar early exit signal basado en CHoCH contra posición (nice-to-have, no bloquea nada)
