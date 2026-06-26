# Issues Pre-Escalado — Review 2026-03-10
> Status: PENDIENTE — monitorear durante forward test, resolver antes de escalar a $1,000+
> Contexto: El bot está LIVE con $106, ~$0.03 de riesgo real por trade. Estos issues no son blockers a esta escala, pero SÍ lo son antes de subir capital.

---

## WARNING 1: Risk State se resetea en cada restart

### Qué pasa
`RiskStateTracker` (risk_service/state_tracker.py) es 100% in-memory. Cuando el bot se reinicia:
- PnL diario/semanal → 0 (los límites de drawdown se "olvidan")
- Cooldown después de pérdida → desaparece
- Contador de trades/día → 0 (podría exceder el máximo)
- Posiciones abiertas → 0 (mitigado parcialmente: execution_service sincroniza con exchange al inicio)

### Por qué importa
A $106 con $0.03 de riesgo, un restart no duele. A $1,000+ con 2% real de riesgo ($20/trade), un restart después de 3 pérdidas seguidas podría saltarse el drawdown limit y seguir tradeando.

### Qué esperar mientras corre
Nada grave. Si ves que el bot se reinicia (Docker restart, crash, etc.), revisa que no abra trades inmediatamente después de una racha perdedora.

### Fix
Persistir el state en Redis: daily_pnl, weekly_pnl, last_loss_time, trades_today. ~50 líneas. Hacer ANTES de escalar.

---

## WARNING 2: Setup D re-entra repetidamente en el mismo OB

### Qué pasa
Setup D detecta un Order Block, entra, pierde (SL), y 1 hora después vuelve a entrar en el MISMO OB. Hoy pasó 4 veces con ETH short en ~$2042. El sistema tiene `mark_ob_failed()` pero solo marca el OB como fallido si `pnl_pct < 0`. Si el SL fue breakeven (pnl=0.00%), el OB no se marca y se reutiliza.

### Por qué importa
Quema capital en OBs débiles. 4 entradas × $0.03 = $0.12 hoy. A mayor escala, el efecto se amplifica.

### Qué esperar mientras corre
Verás trades repetidos en el mismo precio del mismo par. En los logs: múltiples `ORDER PLACED` con entry/SL casi idénticos en el mismo día. Es el comportamiento más visible que vas a notar.

### Fix
Marcar OB como failed también en breakeven (`pnl_pct <= 0` en vez de `< 0`). ~3 líneas en strategy_service/service.py.

---

## INFO 1: PositionSizer existe pero no se usa

### Qué pasa
`risk_service/position_sizer.py` implementa sizing basado en riesgo: `size = (capital × 2%) / distancia_al_SL`. Pero `RiskService.check()` usa fixed-notional: `size = capital × 15%` sin importar la distancia al SL.

### Por qué importa
Con fixed-notional, el riesgo real varía:
- SL a 0.2% de distancia → riesgo real ~0.03% del capital (~$0.03)
- SL a 1.0% de distancia → riesgo real ~0.15% del capital (~$0.16)

Nunca llega al 2% que describe CLAUDE.md. Es más conservador, no más peligroso.

### Qué esperar mientras corre
Trades con tamaño siempre igual (~$15.96 notional) sin importar si el SL está cerca o lejos. Los trades con SL lejano tendrán mayor riesgo absoluto que los de SL cercano, pero siempre por debajo del 2%.

### Fix
Activar el PositionSizer existente en RiskService.check(). Evaluar después de 50+ trades para ver si el sizing fijo está generando resultados aceptables antes de cambiar.

---

## INFO 2: Headlines API (cryptocurrency.cv) retornando 403

### Qué pasa
La fuente de noticias crypto devuelve HTTP 403 en cada polling. El Fear & Greed index (alternative.me) sí funciona. Claude recibe menos contexto para evaluar swing setups.

### Por qué importa
Poco ahora mismo — setup_d (el más activo) no pasa por Claude. Para swing setups (A/B/F), Claude tendría menos datos de contexto. No bloquea ningún trade.

### Qué esperar mientras corre
En los logs verás `ERROR` de Etherscan timeouts (no-critical) y 403 de headlines. Ignorar ambos.

### Fix
Buscar API alternativa de noticias o eliminar el polling de headlines si no aporta valor. Prioridad baja.

---

## INFO 3: Setup D bypasses Claude AI completamente

### Qué pasa
Quick setups (setup_d) reciben `ai_confidence=1.0` automáticamente en main.py:156-163. No llaman a la API de Claude. El 100% de los trades de hoy fueron setup_d → 0 llamadas a Claude.

### Por qué importa
No es un bug — es by design ("the data IS the signal" para quick setups). Pero significa que:
- No estás pagando nada de Anthropic API ($0.00 hoy)
- Tampoco estás validando si Claude aporta valor en quick setups
- Si el bot solo genera quick setups, Claude nunca se ejercita

### Qué esperar mientras corre
La mayoría de trades serán setup_d sin filtro AI. Los swing setups (A/B/F) sí pasarán por Claude cuando se detecten, pero son menos frecuentes. Revisa en Grafana el ratio de setup types.

### Fix
No requiere fix. Monitorear. Cuando tengas 50+ trades, analizar si setup_d con Claude sería más rentable (usar el forward-test de rejected setups del plan existente).

---

## Recomendación: Cuánto tiempo dejarlo correr

### Mínimo recomendado: 3-4 semanas sin tocar nada (hasta ~3-7 abril)

**Por qué 3-4 semanas:**
- Necesitas ~50 trades mínimo para tener significancia estadística
- A ~1.6 trades/día (ritmo de hoy), eso son ~25 días
- Cubre al menos 1 ciclo semanal completo de drawdown tracking
- Incluye diferentes condiciones de mercado (hoy F&G=13, Extreme Fear — verás cómo se comporta cuando cambie a neutral/greed)
- El backtest agresivo generó 97 trades en 60 días (~1.6/día), consistente con lo que ves live

### Qué monitorear (sin tocar código):
1. **Win rate** — target >45% (backtest dio 51.5%)
2. **Profit factor** — target >1.5 (backtest dio 1.81)
3. **Max drawdown** — que no supere 5% diario ni 10% semanal
4. **Setup distribution** — cuántos D vs A/B/F
5. **Re-entry pattern** — cuántas veces re-entra en el mismo OB (WARNING 2)
6. **Telegram alerts** — si ves EMERGENCY, investiga

### Proyección de ganancias (escenario basado en backtest)

El backtest agresivo 60 días dio **+$7,558 sobre $10,000 de capital** (75.6% return).

Escalando proporcionalmente a $106 de capital:

| Escenario | Return % | Ganancia estimada | Nota |
|-----------|----------|-------------------|------|
| Optimista (replica backtest) | 75% en 60d | ~$80 | Improbable — live siempre underperforma backtest |
| Realista (50% del backtest) | 37% en 60d | ~$40 | Slippage, latencia, condiciones diferentes |
| Conservador (25% del backtest) | 19% en 60d | ~$20 | Más realista para primera iteración live |
| Break-even | 0% | $0 | Validar que no pierde dinero ya es un win |

**Expectativa honesta para las primeras 4 semanas:** entre -$5 y +$20.

El objetivo real de este período NO es ganar dinero — es validar que:
1. El bot no pierde capital de forma descontrolada
2. Los patrones detectados en backtest se reproducen live
3. Los fail-safes funcionan bajo condiciones reales
4. Tienes datos reales para decidir si escalar a $500-1,000

**Si después de 4 semanas el bot es break-even o mejor → escalar. Si pierde >10% → revisar antes de meter más capital.**
