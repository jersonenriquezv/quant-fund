# Risk Service
> Última actualización: 2026-03-03
> Estado: implementado (completo, integrado en main.py)

## Qué hace (30 segundos)
El Risk Service es el guardián del capital. Antes de que cualquier trade se ejecute, pasa por 6 checks obligatorios (guardrails) y un cálculo de tamaño de posición. Si cualquier check falla, el trade NO se ejecuta. Sin excepciones.

## Por qué existe
Sin control de riesgo, un solo trade malo puede destruir la cuenta. El Risk Service implementa las reglas de CLAUDE.md: máximo 2% riesgo por trade, 3% drawdown diario, 5% semanal, 5x apalancamiento, y cooldown de 30 min después de pérdida.

## Cómo funciona (5 minutos)

### Flujo de datos
```
TradeSetup (del Strategy Service)
  │
  ▼
RiskService.check(setup)
  │
  ├── check R:R ratio >= 1.5 (usa TP2 vs entry/SL)
  ├── check cooldown (30 min post-loss)
  ├── check max trades/día (5)
  ├── check max posiciones abiertas (3)
  ├── check drawdown diario < 3%
  ├── check drawdown semanal < 5%
  ├── calcular tamaño posición: (Capital × Risk%) / |Entry - SL|
  └── enforce max leverage (5x)
  │
  ▼
RiskApproval { approved, position_size, leverage, risk_pct, reason }
```

### Fail Fast
Los guardrails se evalúan en orden. El primero que falle rechaza el trade inmediatamente — no se ejecutan los demás checks.

### Estado en memoria
El Risk Service trackea estado in-memory (no depende de PostgreSQL ni Redis para funcionar):
- Trades cerrados hoy (para contar trades/día y drawdown diario)
- Posiciones abiertas actuales
- P&L diario y semanal (drawdown)
- Timestamp de la última pérdida (para cooldown)
- Capital actual ($100 demo)

Auto-reset: contadores diarios se resetean a medianoche UTC, semanales el lunes UTC.

## Archivos implementados

### `risk_service/position_sizer.py` — Calculadora de posición
- Clase: `PositionSizer`
- Fórmula: `position_size = (capital × risk_pct) / abs(entry - sl)`
- Leverage: `(position_size × entry) / capital`
- Si leverage > MAX_LEVERAGE (5x), recorta la posición para que leverage = 5x exacto
- Validaciones: entry == sl → error, capital ≤ 0 → error, risk ≤ 0 → error

### `risk_service/guardrails.py` — 6 checks puros
- Clase: `Guardrails`
- Cada método retorna `tuple[bool, str]` (passed, reason)
- **Sin estado** — funciones puras, reciben valores y retornan veredicto
- Checks:
  - `check_rr_ratio(setup)` — R:R de TP2 >= MIN_RISK_REWARD (1.5)
  - `check_cooldown(last_loss_time, current_time)` — COOLDOWN_MINUTES (30) elapsed?
  - `check_max_trades_today(count)` — < MAX_TRADES_PER_DAY (5)?
  - `check_max_open_positions(count)` — < MAX_OPEN_POSITIONS (3)?
  - `check_daily_drawdown(dd_pct)` — < MAX_DAILY_DRAWDOWN (3%)?
  - `check_weekly_drawdown(dd_pct)` — < MAX_WEEKLY_DRAWDOWN (5%)?

### `risk_service/state_tracker.py` — Estado en memoria
- Clase: `RiskStateTracker`
- Lifecycle del trade:
  - `record_trade_opened(pair, direction, entry_price, timestamp)`
  - `record_trade_closed(pair, pnl_pct, timestamp)` — actualiza DD, activa cooldown si pérdida. Si el pair no está en posiciones abiertas, emite `logger.warning()` (protección contra bugs silenciosos en el pipeline).
- Getters para guardrails: `get_trades_today_count()`, `get_open_positions_count()`, `get_daily_dd_pct()`, `get_weekly_dd_pct()`, `get_last_loss_time()`
- `_check_date_reset()` — auto-reset al cambiar día/semana UTC

### `risk_service/service.py` — Facade (RiskService)
- Clase: `RiskService(capital: float)`
- Compone: PositionSizer + Guardrails + RiskStateTracker
- **Método principal:** `check(setup: TradeSetup) -> RiskApproval`
  1. Corre los 6 guardrails en orden (fail fast)
  2. Calcula position size y leverage
  3. Retorna RiskApproval (approved/rejected con razón)
- **Para Execution Service (implementado):**
  - `on_trade_opened(pair, direction, entry_price, timestamp)` — llamado al colocar entry order
  - `on_trade_closed(pair, pnl_pct, timestamp)` — llamado al cerrar posición (SL, TP, timeout, emergency)
  - `update_capital(amount)` — disponible para futuro sync con balance del exchange

### `risk_service/__init__.py`
- Exporta `RiskService`

## Configuración (`config/settings.py`)

| Setting | Default | Descripción |
|---|---|---|
| `RISK_PER_TRADE` | `0.02` (2%) | % del capital arriesgado por trade |
| `MAX_LEVERAGE` | `5` | Apalancamiento máximo permitido |
| `MAX_DAILY_DRAWDOWN` | `0.03` (3%) | DD diario máximo antes de pausar |
| `MAX_WEEKLY_DRAWDOWN` | `0.05` (5%) | DD semanal máximo antes de pausar |
| `MAX_OPEN_POSITIONS` | `3` | Posiciones simultáneas máximas |
| `MAX_TRADES_PER_DAY` | `5` | Trades por día máximo |
| `COOLDOWN_MINUTES` | `30` | Minutos de espera post-pérdida |
| `MIN_RISK_REWARD` | `1.5` | R:R mínimo (TP2 vs SL) |

## Tests

69 tests en 4 archivos:
- `test_position_sizer.py` (10) — fórmula, leverage cap, edge cases
- `test_guardrails.py` (17) — cada regla pass/fail/boundary
- `test_state_tracker.py` (18) — lifecycle, DD, cooldown, date reset
- `test_risk_service.py` (14) — check() integración: 3 approvals, 6 rejections, 4 lifecycle, 1 entry==SL

Última corrida: 69 passed, 0 failed (0.33s)

## FAQ

**¿Por qué R:R usa TP2 y no TP1?**
TP1 cierra 50% de la posición a 1:1 por diseño — es un partial close, no el target real. TP2 (1:2) es donde se evalúa si el trade vale la pena.

**¿Por qué estado en memoria y no en PostgreSQL?**
Los checks son CPU puro (microsegundos). Depender de una DB haría los checks lentos y frágiles. El Execution Service actualiza el estado via `on_trade_opened/closed`. Redis como backup planeado para v2.

**¿Por qué fail fast?**
Si el cooldown está activo, no tiene sentido calcular position size. El primer NO es el NO final.

**¿Qué pasa si el bot se reinicia?**
Estado se pierde (es in-memory). Empezaría con 0 trades hoy, 0 DD. Esto es conservador — permite tradear inmediatamente. Planeado para v2: reconstruir estado desde PostgreSQL al arrancar.

**¿Qué pasa si `record_trade_closed()` recibe un pair que no está abierto?**
No crashea. El trade se registra igual (P&L, cooldown, trades_today), pero emite un `logger.warning()` porque probablemente indica un bug en el pipeline (nombre de par inconsistente entre layers, o cierre duplicado). Busca en logs: `"Closed trade for X but no matching open position found"`.

## Limitaciones conocidas

- **Estado in-memory**: Se pierde al reiniciar. Planeado para v2 (Redis persistence).
- **Max trade duration (12h)**: Enforceado por el Execution Service (`PositionMonitor` cierra posiciones después de `MAX_TRADE_DURATION_SECONDS`).
- **Tracking por pair, no por trade ID**: Si hubiera 2 trades abiertos en el mismo pair (improbable con MAX_OPEN_POSITIONS=3 y solo BTC/ETH), el cierre matchearía el primero.

## Cambios recientes

- **2026-03-03** — Warning log en `state_tracker.py` cuando se cierra un pair sin posición abierta (protección contra bugs silenciosos)
- **2026-03-03** — Traducción de comentarios en `config/settings.py` a inglés (cumplimiento de CLAUDE.md)
- **2026-03-03** — Revisión completa por @planner: 0 bugs críticos, 69/69 tests passing, 100% alineado con CLAUDE.md
