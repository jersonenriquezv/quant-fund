# Risk Service
> Última actualización: 2026-03-12
> Estado: implementado (completo, integrado en main.py). Dual-mode position sizing: FIXED_TRADE_MARGIN ($20 default) o TRADE_CAPITAL_PCT (15% fallback). MIN_RISK_DISTANCE_PCT (0.5%) also checked in Strategy layer as early filter. Leverage always MAX_LEVERAGE (7x), not dynamic.

## Qué hace (30 segundos)
El Risk Service es el guardián del capital. Antes de que cualquier trade se ejecute, pasa por 6 checks obligatorios (guardrails) y un cálculo de tamaño de posición. Si cualquier check falla, el trade NO se ejecuta. Sin excepciones.

## Por qué existe
Sin control de riesgo, un solo trade malo puede destruir la cuenta. El Risk Service implementa las reglas de CLAUDE.md: máximo 2% riesgo por trade, 5% drawdown diario, 10% semanal, 7x apalancamiento, y cooldown de 15 min después de pérdida.

## Cómo funciona (5 minutos)

### Flujo de datos
```
TradeSetup (del Strategy Service)
  │
  ▼
RiskService.check(setup)
  │
  ├── check min risk distance >= 0.5% (SL no demasiado cerca de entry)
  ├── check R:R ratio >= 1.2 (usa TP2 vs entry/SL)
  ├── check cooldown (5 min post-loss)
  ├── check max trades/día (20)
  ├── check max posiciones abiertas (8)
  ├── check drawdown diario < 5%
  ├── check drawdown semanal < 10%
  ├── calcular tamaño posición: (Capital × Risk%) / |Entry - SL|
  └── enforce max leverage (7x)
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
- Capital actual (fetched from exchange at startup, fallback to `INITIAL_CAPITAL`)

Auto-reset: contadores diarios se resetean a medianoche UTC, semanales el lunes UTC.

## Archivos implementados

### `risk_service/position_sizer.py` — Calculadora de posición
- Clase: `PositionSizer`
- **Risk-based sizing (PositionSizer):** `position_size = (capital × risk_pct) / abs(entry - sl)`. Leverage: `(position_size × entry) / capital`. Si leverage > MAX_LEVERAGE (7x), recorta la posición. Note: PositionSizer computes dynamic leverage from risk%, but RiskService.check() uses fixed `MAX_LEVERAGE` directly (see below).
- Validaciones: entry == sl → error, capital ≤ 0 → error, risk ≤ 0 → error

### `risk_service/guardrails.py` — 6 checks puros
- Clase: `Guardrails`
- Cada método retorna `tuple[bool, str]` (passed, reason)
- **Sin estado** — funciones puras, reciben valores y retornan veredicto
- Checks:
  - `check_min_risk_distance(setup)` — SL distance >= MIN_RISK_DISTANCE_PCT (0.5%) del entry price. Rechaza noise trades donde comisiones comen el profit.
  - `check_rr_ratio(setup)` — R:R de TP2 >= MIN_RISK_REWARD (1.2 swing) o MIN_RISK_REWARD_QUICK (1.0 quick setups C/D/E)
  - `check_cooldown(last_loss_time, current_time)` — COOLDOWN_MINUTES (5) elapsed?
  - `check_max_trades_today(count)` — < MAX_TRADES_PER_DAY (20)?
  - `check_max_open_positions(count)` — < MAX_OPEN_POSITIONS (8)?
  - `check_daily_drawdown(dd_pct)` — < MAX_DAILY_DRAWDOWN (5%)?
  - `check_weekly_drawdown(dd_pct)` — < MAX_WEEKLY_DRAWDOWN (10%)?

### `risk_service/state_tracker.py` — Estado con persistencia Redis
- Clase: `RiskStateTracker(capital, redis_store=None)`
- **Redis persistence**: Si se pasa `redis_store` (de DataService), el estado se persiste en Redis en cada mutación y se restaura al iniciar. Sobrevive reinicios del bot sin perder guardrails.
  - Keys: `risk_daily_pnl`, `risk_weekly_pnl`, `risk_last_loss_time`, `risk_trades_today`, `risk_state_day`, `risk_state_week`, `risk_open_positions` (JSON array)
  - TTL: 48 horas
  - Daily values solo se restauran si el día guardado == hoy. Weekly solo si misma semana. Cooldown y open positions siempre se restauran.
  - Si Redis falla al cargar o guardar → degrada silenciosamente (fire-and-forget). El bot NO se detiene.
- Lifecycle del trade:
  - `record_trade_opened(pair, direction, entry_price, timestamp)`
  - `record_trade_closed(pair, direction, pnl_pct, timestamp)` — matchea por `(pair, direction)`. Actualiza DD, activa cooldown si pérdida.
- `record_trade_cancelled(pair, direction)` — removes cancelled pending entry from open positions without counting as a trade or affecting P&L.
- Getters para guardrails: `get_trades_today_count()`, `get_open_positions_count()`, `get_daily_dd_pct()`, `get_weekly_dd_pct()`, `get_last_loss_time()`
- `_check_date_reset()` — auto-reset al cambiar día/semana UTC. Usa `date()` objects (no `tm_yday`) para correcto reset en frontera de año.

### `risk_service/service.py` — Facade (RiskService)
- Clase: `RiskService(capital: float, data_service=None)`
- Compone: PositionSizer + Guardrails + RiskStateTracker
- **Método principal:** `check(setup: TradeSetup, ai_confidence: float = 1.0) -> RiskApproval`
  1. Corre los 7 guardrails en orden (fail fast): min risk distance, R:R ratio, cooldown, max trades/day, max positions, daily DD, weekly DD
  2. **Position sizing (dual-mode):**
     - **Modo fijo (default):** Si `FIXED_TRADE_MARGIN > 0`: `margin = $20`, `notional = margin × leverage`, `position_size = notional / entry_price`. Ejemplo: $20 × 7x = $140 notional. `risk_pct = margin / capital`.
     - **Modo porcentaje (fallback):** Si `FIXED_TRADE_MARGIN == 0`: `notional = capital × TRADE_CAPITAL_PCT`, `margin = notional / leverage`, `risk_pct = TRADE_CAPITAL_PCT`.
  3. **Bet sizing (optional, AFML Ch.10):** Si `BET_SIZING_ENABLED=true` y `ai_confidence < 1.0`: `factor = KELLY_FRACTION × (2p - 1)`, clamped a `[BET_SIZE_MIN, BET_SIZE_MAX]`. Margin se multiplica por factor. Half-Kelly por default. Inactivo cuando AI está bypassed (confidence=1.0).
  4. **Hard margin cap (AFML Ch.10):** Después de bet sizing, `margin` se limita a `MAX_MARGIN_PCT_OF_CAPITAL` (25%) del capital. Previene que `BET_SIZE_MAX > 1.0` produzca posiciones desproporcionadas. Con $108 capital, max margin = $27.
  5. Leverage siempre = `MAX_LEVERAGE` (7x). Not dynamically computed from risk% — PositionSizer is only used by backtester.
  5. Verifica min order size contra `MIN_ORDER_SIZES` por par
  6. Retorna RiskApproval (approved/rejected con razón)
- **Para Execution Service (implementado):**
  - `on_trade_opened(pair, direction, entry_price, timestamp)` — llamado al colocar entry order
  - `on_trade_closed(pair, direction, pnl_pct, timestamp)` — llamado al cerrar posición (SL, TP, timeout, emergency). Matchea por `(pair, direction)` para cerrar la posición correcta.
  - `on_trade_cancelled(pair, direction)` — llamado cuando un pending entry es cancelado (nunca llenó). Remueve de open positions sin contar como trade ni afectar P&L.
  - `update_capital(amount)` — disponible para futuro sync con balance del exchange
  - **Exchange minimum order size check**: Después de position sizing, verifica contra `MIN_ORDER_SIZES`. Si el size calculado es menor que el mínimo del exchange, rechaza con mensaje claro (e.g., "Position size 0.001 below exchange minimum 0.01 for BTC/USDT").

### `risk_service/__init__.py`
- Exporta `RiskService`

## Configuración (`config/settings.py`)

| Setting | Default | Descripción |
|---|---|---|
| `FIXED_TRADE_MARGIN` | `20` ($20) | Margin fijo por trade en USDT. Notional = margin × leverage. $20 × 5x = $100. Si 0, usa TRADE_CAPITAL_PCT. |
| `TRADE_CAPITAL_PCT` | `0.15` (15%) | Fallback: % del capital como notional por trade (solo si FIXED_TRADE_MARGIN=0) |
| `MAX_LEVERAGE` | `7` | Apalancamiento máximo permitido |
| `MAX_DAILY_DRAWDOWN` | `0.05` (5%) | DD diario máximo antes de pausar |
| `MAX_WEEKLY_DRAWDOWN` | `0.10` (10%) | DD semanal máximo antes de pausar |
| `MAX_OPEN_POSITIONS` | `8` | Posiciones simultáneas máximas (aggressive mode) |
| `MAX_TRADES_PER_DAY` | `20` | Trades por día máximo (aggressive mode) |
| `COOLDOWN_MINUTES` | `5` | Minutos de espera post-pérdida (aggressive mode) |
| `MIN_RISK_REWARD` | `1.2` | R:R mínimo para swing setups A/B (TP2 vs SL) |
| `MIN_RISK_REWARD_QUICK` | `1.0` | R:R mínimo para quick setups C/D/E |
| `MIN_RISK_DISTANCE_PCT` | `0.005` (0.5%) | Distancia mínima SL-entry como fracción del precio. Rechaza noise trades. Para ETH@$2000, SL >= $10. Now also checked in Strategy layer (early filter in evaluate_setup_a/evaluate_setup_d) before building TradeSetup. |
| `MIN_ORDER_SIZES` | `{"BTC/USDT": 0.0001, "ETH/USDT": 0.001}` | Mínimo de tamaño de orden por par (OKX contract-based: BTC min 0.01 contracts × 0.01 ctVal, ETH min 0.01 × 0.1 ctVal). Pre-check en main.py filtra antes de Claude. |
| `BET_SIZING_ENABLED` | `false` | Activa bet sizing por confianza AI (half-Kelly, AFML Ch.10). Requiere AI filter activo. |
| `KELLY_FRACTION` | `0.5` | Fracción de Kelly (0.5 = half-Kelly, conservador) |
| `BET_SIZE_MIN` | `0.25` | Floor: 25% del margin base (confidence muy baja) |
| `BET_SIZE_MAX` | `2.0` | Ceiling: 200% del margin base (confidence muy alta) |
| `MAX_MARGIN_PCT_OF_CAPITAL` | `0.25` (25%) | Hard cap: margin max por trade como fracción del capital. Previene over-bet con bet sizing. |

## Tests

81 tests en 4 archivos:
- `test_position_sizer.py` — fórmula, leverage cap, edge cases
- `test_guardrails.py` (23) — cada regla pass/fail/boundary/edge
- `test_state_tracker.py` (35) — lifecycle, DD, cooldown, date reset, year boundary, direction matching, trade cancelled (4 tests), Redis persistence round-trip (8 tests)
- `test_risk_service.py` (14) — check() integración: approvals, rejections, lifecycle, entry==SL, leverage capped

Última corrida: 81 passed, 0 failed

## FAQ

**¿Por qué R:R usa TP2 y no TP1?**
TP1 cierra 50% de la posición a 1:1 por diseño — es un partial close, no el target real. TP2 (1:3) es donde se evalúa si el trade vale la pena.

**¿Por qué estado en memoria y no en PostgreSQL?**
Los checks son CPU puro (microsegundos). Depender de una DB haría los checks lentos y frágiles. El Execution Service actualiza el estado via `on_trade_opened/closed`. Redis como backup planeado para v2.

**¿Por qué fail fast?**
Si el cooldown está activo, no tiene sentido calcular position size. El primer NO es el NO final.

**¿Qué pasa si el bot se reinicia?**
Con Redis persistence (implementado): el estado (daily PnL, weekly PnL, trades today, cooldown) se restaura automáticamente desde Redis al iniciar. Si Redis no está disponible, empieza fresh (comportamiento anterior).

**¿Qué pasa si `record_trade_closed()` recibe un pair/direction que no está abierto?**
No crashea. El trade se registra igual (P&L, cooldown, trades_today), pero no remueve ninguna posición abierta (no hay match por `(pair, direction)`).

## Limitaciones conocidas

- **Estado persiste via Redis** (TTL 7 días): daily PnL, weekly PnL, trades today count, cooldown, y open positions list sobreviven reinicios. sync_exchange_positions + reconciliation al arrancar complementa la lista restaurada con posiciones del exchange.
- **Drawdown reconciliation on restart:** `reconcile_drawdown_from_db()` cross-checks Redis drawdown against PostgreSQL `trades` table (source of truth). Uses the worse (more negative) of Redis vs DB values. Prevents under-counting drawdown after crash/restart.
- **Max trade duration (12h)**: Enforceado por el Execution Service (`PositionMonitor` cierra posiciones después de `MAX_TRADE_DURATION_SECONDS`).
- **Tracking por (pair, direction)**: El cierre matchea por par Y dirección. Si hubiera BTC long + BTC short simultáneo, se cierran independientemente.

## Cambios recientes

- **2026-03-19** — Risk audit fixes: (1) Redis TTL 48h→7d, (2) hard margin cap `MAX_MARGIN_PCT_OF_CAPITAL=25%` prevents bet sizing over-bet, (3) drawdown reconciliation from PostgreSQL on restart, (4) HTF campaign pyramid adds now check DD/weekly DD/cooldown guardrails before placing, (5) campaign `execute_campaign` uses RiskApproval position_size instead of own calculation.
- **2026-03-18** — Risk audit fixes: (1) open positions persisted to Redis as JSON (survives restarts), (2) pnl_pct now capital-based not notional-based (DD guardrails measure real account impact), (3) pre-check in main.py aligned with FIXED_TRADE_MARGIN sizing.
- **2026-03-11** — Redis persistence for RiskStateTracker. State (daily_pnl, weekly_pnl, trades_today, cooldown) survives bot restarts. 48h→7d TTL, fire-and-forget writes. 8 new tests.
- **2026-03-10** — `FIXED_TRADE_MARGIN` restored ($20 default). Was accidentally removed on 03-09, causing trades to enter with $3.25 margin instead of $20. Dual-mode: if FIXED_TRADE_MARGIN > 0, uses fixed margin; else falls back to TRADE_CAPITAL_PCT.
- **2026-03-09** — `FIXED_TRADE_MARGIN` replaced by `TRADE_CAPITAL_PCT` (0.15 = 15% of capital as notional). Position sizing simplified: no more dual-mode (fixed vs risk-based). Leverage always `MAX_LEVERAGE`. `MIN_ORDER_SIZES` updated: BTC 0.01→0.0001, ETH 0.001 added (correct OKX contract sizes).
- **2026-03-07** — `MIN_RISK_DISTANCE_PCT` 0.3% → 0.2%. Was blocking legitimate OB setups (e.g. ETH OB with $5.26 SL = 0.27%, rejected 4 times in one day despite AI approval at 0.75 confidence). 0.2% still filters noise trades ($4 min SL on ETH@$2000).
- **2026-03-07** — `check_min_risk_distance()` guardrail: rechaza setups donde SL-entry < threshold del precio (noise trades). Pre-check de min order size en `main.py` antes de Claude (ahorra tokens API).
- **2026-03-07** — `on_trade_cancelled()` method: removes cancelled pending entries from open positions without counting as trade or affecting P&L. `MIN_ORDER_SIZES` check: rejects trades below exchange minimum before reaching exchange API. `MAX_OPEN_POSITIONS` 3→5, `MAX_LEVERAGE` 5→7.
- **2026-03-06** — `FIXED_TRADE_MARGIN` setting: cuando > 0, position sizing usa margen fijo en vez de risk-based. Reemplaza `SANDBOX_MARGIN_PER_TRADE`. Capital inicial del Risk Service ahora viene del exchange balance (fallback a `INITIAL_CAPITAL`).
- **2026-03-06** — `check_rr_ratio()` ahora usa `MIN_RISK_REWARD_QUICK` (1.0) para quick setups (C/D/E) via `QUICK_SETUP_TYPES` check.
- **2026-03-06** — `FORCE_MAX_LEVERAGE` eliminado. Risk-based sizing siempre. Aggressive profile: DD 5%/10% (era 20%/40%), R:R 1.2 (era 1.0).
- **2026-03-04** — I-R1: `record_trade_closed` ahora recibe `direction`, matchea por `(pair, direction)`. Todos los callers actualizados.
- **2026-03-04** — M-R1: `_check_date_reset` usa `date()` en vez de `tm_yday` (fix año boundary Dec 31 → Jan 1).
- **2026-03-04** — M-R3: `_persist_failures` counter en RiskService — logea warning tras 5 fallos consecutivos de PostgreSQL.
- **2026-03-04** — M-R2: Docstring en state_tracker explicando que la sumación de PnL % es una aproximación (negligible a esta escala).
- **2026-03-03** — Warning log en `state_tracker.py` cuando se cierra un pair sin posición abierta (protección contra bugs silenciosos)
- **2026-03-03** — Traducción de comentarios en `config/settings.py` a inglés (cumplimiento de CLAUDE.md)
- **2026-03-03** — Revisión completa por @planner: 0 bugs críticos, 69/69 tests passing, 100% alineado con CLAUDE.md
