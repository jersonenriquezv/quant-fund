# Execution Service (Layer 5)
> Última actualización: 2026-03-11 (Split entries, expectancy filters, execution metrics.)
> Estado: **Fase 1 — COMPLETADA**. Entry + SL + TP atómicos (attached). Breakeven + trailing SL via price polling. CampaignMonitor para HTF position trades. PnL tracking con fee deduction (TRADING_FEE_RATE 0.05% per side).

El brazo ejecutor del bot. Recibe trades aprobados por Risk Service y los ejecuta en OKX via ccxt.

## Arquitectura

```
ExecutionService (facade)
├── OrderExecutor     — wrapper ccxt para órdenes (limit, stop-market, TP, cancel)
├── PositionMonitor   — loop async que gestiona el ciclo de vida de posiciones intraday
├── ManagedPosition   — estado mutable de cada posición intraday (modelo interno)
└── CampaignMonitor   — loop async para HTF position trades (pyramid adds + trailing SL)
    ├── PositionCampaign — estado mutable de la campaña (initial + adds + SL)
    └── CampaignAdd      — datos de un add individual
```

## Flujo de una operación

1. `execute(setup, approval, ai_confidence)` recibe trade aprobado
2. **Valida precio ordering** — Long: `sl < entry < tp2`. Short: inverso.
3. **Chequea posición existente** — Si hay pending_entry → reemplaza. Si hay adoptada → permite coexistencia. Si hay activa del bot → rechaza.
4. Configura el par (margin mode isolated + leverage). `defaultMarginMode` seteado a nivel de exchange en ccxt para evitar fallback a `cross`.
5. **Split entry check**: si `setup.entry2_price > 0` y es swing setup y live mode:
   - Divide `position_size` 50/50 entre entry1 (OB 50%) y entry2 (OB 75%)
   - **Min order size guard**: si la mitad < `MIN_ORDER_SIZES[pair]`, fallback a single entry
   - Entry1: limit con SL+TP attached (cubre la mitad del tamaño)
   - Entry2: limit SIN attached (se consolida después)
   - Si entry2 falla al colocar → continúa con entry1 sola (half size)
   - Si ambas fallan → sin ejecución
6. **Single entry** (original): coloca limit entry order al precio calculado (50% OB / 75% FVG) **con SL+TP attached** — OKX crea SL/TP atómicamente cuando el entry se llena.
   - **Contracts conversion**: `amount` en base currency (ETH/BTC) se convierte a contratos OKX internamente via `_to_contracts()`. OKX SWAP `ctVal`: BTC=0.01, ETH=0.1.
7. Notifica Risk Service inmediatamente (en PLACE, no en fill)
7. **Telegram: ORDER PLACED** — envía notificación con par, dirección, entry, SL, TP, size, leverage
8. Registra la posición en el monitor

## Máquina de estados (Fase 1 — simplificada)

```
pending_entry ──[fill]──────> active         (SL+TP ya attached; monitor busca IDs)
pending_entry ──[4h/1h]─────> closed         (cancela entry)

active ──[TP fills]──> closed                (profit — 100% at tp2)
active ──[SL fills]──> closed                (loss, breakeven, or trailing)
active ──[price >= tp1 (1:1)]──> SL moves to breakeven
active ──[price >= midpoint(tp1,tp2) (1.5:1)]──> SL moves to tp1 (trailing)
active ──[12h/4h]────> closed                (market close)
active ──[SL fail]───> emergency_pending     (retry x3)

emergency_pending ──[retry ok]──> closed
emergency_pending ──[3 fails]──> emergency_failed  (intervención manual)
```

### Split Entry State Machine (`_check_split_pending`)

Cuando `is_split_entry == True`, el monitor usa `_check_split_pending()` en vez de `_check_pending_entry()`:

1. **Poll entry1** → si filled, marca `entry1_filled = True`, guarda `entry1_fill_price/size`
2. **Poll entry2** (solo si entry1 ya filled) → si filled, marca `entry2_filled = True`
3. **Timeout**:
   - Ambas sin fill → cancel both, close as `cancelled`
   - Entry1 filled, entry2 no → activa con entry1 only (SL/TP attached ya correcto para half size)
4. **Ambas filled** → consolida:
   - `actual_entry_price = VWAP(fill1, fill2)`
   - `filled_size = sum`
   - `_cancel_attached_orders()` → cancela SL/TP de entry1 (que cubría solo la mitad)
   - `_on_entry_filled()` → coloca SL/TP consolidados para el tamaño total
5. **Entry1 filled + entry2 cancelled** (antes del timeout) → activa con entry1 only

`_cancel_attached_orders()` cancela todas las algo orders pendientes + TP limit orders (reduceOnly) del par.

## Exit Management (simplificado)

- **SL+TP attached**: al colocar la entry order, se pasan `stopLoss` y `takeProfit` como params ccxt. OKX crea las órdenes atómicamente cuando el entry se llena (SL como `conditional` algo order, TP como limit `reduceOnly`).
- **Monitor discovery**: después del fill, espera 0.5s y busca órdenes attached:
  - SL: en `find_pending_algo_orders()` (tipos `trigger` + `conditional`), match por `slTriggerPx` con 0.5% tolerancia
  - TP: en `fetch_open_orders()`, match por price con 0.5% tolerancia
  - **Fallback**: si no se encuentran attached, coloca SL/TP manualmente (3 retries para SL)
  - **Contracts→base**: `filled` de ccxt se convierte de contratos a base currency via `contracts_to_base()`
- **Breakeven**: el monitor poll ticker cada 5s. Cuando price cruza `tp1_price` (1:1 R:R), mueve SL a `entry_price`
- **TP falla**: posición queda abierta con SL only (no emergency close — SL protege)

## Tipos de órdenes

| Orden | Tipo | Por qué |
|-------|------|---------|
| Entry | Limit | Control de slippage. Cancela si no se llena en 4h (swing) / 1h (quick) |
| Stop Loss | Stop-market (algo order) | Ejecución garantizada. Sin `reduceOnly` (OKX error 51205 en net mode) |
| TP | Limit (reduceOnly) | Precio exacto, sin slippage |

## Reglas de seguridad

1. **Validación de precios** — Long: `sl < entry < tp2`. Short: `sl > entry > tp2`.
2. **SL vs market validation** — Antes de colocar la orden, verifica que el SL no esté ya "adentro" del mercado. Short con SL < market → skip (el SL se activaría inmediatamente, OKX rechaza con 51053). Long con SL > market → skip. Fetch ticker para obtener `last` price. Solo en live mode (no sandbox).
3. **SL placement retries** — 3 intentos con delays 0.3s/0.6s. Si falla → emergency market close.
4. **TP falla → SL protege** — Antes se hacía emergency close si TP fallaba. Ahora el SL queda activo y es suficiente.
5. **Ajuste SL: nuevo ANTES de cancelar viejo** — Cero ventana sin protección.
6. **Notificación a Risk: en PLACE, no en fill.**
7. **Cancelled entries no cuentan como trades.**
8. **Shutdown: cancela entries pendientes, NO cierra posiciones activas.**
9. **Post-fill SL distance validation** — Después de que la entry se llena, el monitor compara el fill price real con el SL. Si `abs(fill - sl) / fill < MIN_RISK_DISTANCE_PCT`, el SL efectivo es demasiado pequeño (slippage comió el margen). La posición se cierra inmediatamente con `exit_reason = "sl_too_close"` para evitar trades donde las comisiones consumen toda la ganancia potencial.
10. **Failed OB check pre-execute** — En `main.py:on_candle_confirmed`, antes de llamar a `execute()`, se consulta `strategy_service.is_ob_failed(pair, sl_price, entry_price)`. Si el OB ya resultó en pérdida en esta sesión, el trade se descarta. El callback `on_sl_hit` en el monitor notifica a `StrategyService.mark_ob_failed()` cuando un trade cierra con PnL negativo. El callback está protegido con try/catch — si falla, `_close_position()` siempre se ejecuta (bugfix: antes una excepción en `mark_ob_failed()` impedía cerrar la posición, causando un loop infinito de "SL hit").
11. **Max slippage guard** — Después del fill, si `abs(actual_entry - entry) / entry > MAX_SLIPPAGE_PCT` (0.3%), la posición se cierra inmediatamente con `exit_reason = "excessive_slippage"`. Cancela SL/TP, market close. Skipped en sandbox mode (fills sintéticos). Configurable en `settings.MAX_SLIPPAGE_PCT`.

## Breakeven Logic

1. En cada poll cycle (5s), si `breakeven_hit == False`:
2. Fetch ticker via `fetch_ticker(pair)`
3. Long: si `current_price >= tp1_price` → trigger
4. Short: si `current_price <= tp1_price` → trigger
5. On trigger: `_adjust_sl(pos, actual_entry_price)`, set `breakeven_hit = True`
6. Solo se dispara una vez (idempotente)

## Trailing SL Logic

1. Después del breakeven check, si `breakeven_hit == True` y `trailing_sl_moved == False`:
2. Calcula midpoint = `(tp1_price + tp2_price) / 2`
3. Fetch ticker via `fetch_ticker(pair)`
4. Long: si `current_price >= midpoint` → trigger
5. Short: si `current_price <= midpoint` → trigger
6. On trigger: `_adjust_sl(pos, tp1_price)`, set `trailing_sl_moved = True`
7. Solo se dispara una vez (idempotente)
8. Misma mecánica que breakeven: nuevo SL se coloca ANTES de cancelar el viejo

## SL vanished fallback

Cuando el SL algo order no se encuentra por 12 polls consecutivos (~60s):
- **Position gone** → SL triggered, mark OB as failed (via `_on_sl_hit` callback), close in monitor
- **Position exists** → re-place SL at `current_sl_price`
- **Network error** (`fetch_position` returns `None`) → skip, retry next cycle
- **No position** (`fetch_position` returns `POSITION_EMPTY`) → SL triggered, close
- **Re-place fails** → `emergency_pending`

Also handles SL cancelled externally: re-places SL immediately.

## OKX Algo Order Handling

- `place_limit_order()` acepta `sl_trigger_price` y `tp_price` opcionales → ccxt los pasa como `stopLoss`/`takeProfit` params → OKX crea attached algo orders al fill
- `place_stop_market()` usa ccxt unified API → OKX crea `trigger` type algo order
- `find_pending_algo_orders()` busca en AMBOS tipos: `trigger` Y `conditional` — OKX pone attached SL en `conditional`
- `fetch_order()` intenta fetch normal; si `OrderNotFound` → fallback a `_fetch_algo_order()`
- `cancel_order()` intenta cancel normal; si `OrderNotFound` → fallback a `_cancel_algo_order()` (POST /trade/cancel-algos)
- Error throttling: solo logea primer error y cada 12vo

## Position adoption

Al startup, `sync_exchange_positions()` consulta OKX por posiciones abiertas. Las no trackeadas se adoptan como `ManagedPosition(setup_type="manual")`:
- Monitor las vigila via `fetch_position()` polling
- Si la posición desaparece → `manual_close`
- Permite nueva entry del bot en el mismo par (OKX net mode stacking)

## Orphaned trade reconciliation

Al startup, después de `sync_exchange_positions()`, `_reconcile_orphaned_trades()` detecta trades "huérfanos":
1. Consulta PostgreSQL por todos los trades con `status='open'`
2. Para cada trade: verifica si existe una posición activa en OKX para ese par
3. Si no hay posición en exchange → marca el trade como `status='closed', exit_reason='orphaned_restart'` con PnL 0
4. Logea WARNING por cada trade reconciliado

Esto resuelve el bug donde trades quedaban como "open" en la DB permanentemente después de un reinicio, porque el PositionMonitor perdía su estado in-memory.

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `service.py` | Facade — execute(), start(), stop(), health(). Position adoption converts contracts→base. `_emit_metric()` wired to executor for Grafana. Accepts `on_sl_hit` callback for failed OB tracking. Sends ORDER PLACED Telegram notification on successful order placement. |
| `executor.py` | Wrapper ccxt — place/cancel/fetch orders. Contracts conversion (`_to_contracts`, `contracts_to_base`). Attached SL/TP on entry. Algo cancel fallback. `find_pending_algo_orders()`. Optional `metrics_callback` emits `okx_order_latency_ms` per order. `fetch_position()` returns `POSITION_EMPTY` ({}) when API succeeds but no position exists (vs `None` on error). |
| `monitor.py` | Background loop — attached SL/TP discovery + manual fallback, breakeven + trailing SL via price polling. Post-fill SL distance check (`sl_too_close` close). Slippage guard (`excessive_slippage` close). Sends TRADE CLOSED + EMERGENCY Telegram notifications. Per-position try/catch in poll loop prevents one position's error from blocking others. |
| `models.py` | ManagedPosition (intraday, includes split entry fields: `is_split_entry`, `entry2_price`, `entry1/2_filled/fill_price/fill_size`) + PositionCampaign (HTF) + CampaignAdd (pyramid entries) |
| `campaign_monitor.py` | Background loop para HTF campaigns — entry fill tracking, pyramid adds, trailing SL en 4H swing levels, SL vanished fallback, timeout 7d. Persiste en PostgreSQL `campaigns` table. Notifica CAMPAIGN CLOSED via AlertManager. |

## HTF Campaign Monitor (`campaign_monitor.py`)

Position trades en 4H con pyramid adds y trailing SL. Separado del PositionMonitor intraday.

### Lifecycle

```
pending_initial ──[fill]──────> active        (place SL, no TP)
pending_initial ──[timeout 24h]> closed       (cancel entry)
active ──[add fill]───────────> active        (update SL for total size)
active ──[SL fills]───────────> closed        (trailing SL hit)
active ──[timeout 7d]─────────> closed        (max duration)
```

### Diferencias clave vs intraday
- **Sin TP orders** — sale solo via trailing SL o timeout
- **Pyramid adds:** hasta 3 adds con margen decreciente ($30 initial → $15 → $10 → $5 = $60 total)
- **Trailing SL:** sigue 4H swing lows (long) / swing highs (short) via `get_htf_swing_levels()`
- **Un solo SL** cubre toda la posición stacked (OKX net mode)
- **Entry timeout:** 24h para limit orders HTF (vs 4h intraday)
- **Duration timeout:** 7 días (vs 12h intraday)

### Pyramid adds
- Condiciones: (1) `len(adds) < HTF_MAX_ADDS`, (2) campaign profitable >= `HTF_ADD_MIN_RR` (1.0 R:R), (3) nuevo setup en misma dirección
- Margen decreciente: add 1 = $15, add 2 = $10, add 3 = $5
- Después de fill de add: SL se reemplaza para cubrir total_size
- Add timeout: 4h (después se cancela, campaign sigue)

### Persistencia
- **PostgreSQL:** `insert_campaign()` al activarse, `update_campaign()` al cerrarse
- **Redis:** `set_bot_state("htf_campaign", ...)` con datos de la campaña activa (para dashboard)
- **Risk Service:** `on_trade_opened/closed/cancelled` igual que intraday

### Modelos (`models.py`)
- **PositionCampaign** — estado de la campaña: phase, initial entry/SL, weighted entry (VWAP), total_size, adds list, campaign SL, PnL. Métodos: `update_weighted_entry()`, `get_add_margin(n)`, `current_rr()`.
- **CampaignAdd** — datos de un pyramid add individual: add_number, margin, size, entry/actual price, filled status, order_id.

## Settings

| Setting | Default | Descripción |
|---------|---------|-------------|
| `ENTRY_TIMEOUT_SECONDS` | 86400 (24h) | Tiempo máximo de espera para fill (swing) |
| `ENTRY_TIMEOUT_QUICK_SECONDS` | 3600 (1h) | Tiempo máximo de espera para fill (quick) |
| `ORDER_POLL_INTERVAL` | 5.0s | Intervalo de polling del monitor |
| `MARGIN_MODE` | "isolated" | Modo de margen |
| `MAX_TRADE_DURATION_SECONDS` | 43200 (12h) | Duración máxima trade swing |
| `MAX_TRADE_DURATION_QUICK` | 14400 (4h) | Duración máxima quick |
| `MAX_SLIPPAGE_PCT` | 0.003 (0.3%) | Slippage máximo antes de cerrar (live only) |
| `TRADING_FEE_RATE` | 0.0005 (0.05%) | Fee per side (OKX taker). Deducted from PnL on all exit paths |
| `HTF_CAMPAIGN_ENABLED` | false | Master switch para HTF campaigns (env var) |
| `HTF_INITIAL_MARGIN` | $30 | Margen de la entry inicial |
| `HTF_ADD1_MARGIN` / `ADD2` / `ADD3` | $15 / $10 / $5 | Margen decreciente por pyramid add |
| `HTF_MAX_ADDS` | 3 | Máximo de pyramid adds (4 entries total) |
| `HTF_ADD_MIN_RR` | 1.0 | R:R mínimo antes de permitir primer add |
| `HTF_MAX_CAMPAIGN_DURATION` | 604800 (7d) | Duración máxima de la campaña |
| `HTF_ENTRY_TIMEOUT_SECONDS` | 86400 (24h) | Timeout de entry para limit orders HTF |
| `HTF_MAX_CAMPAIGNS` | 1 | Máximo de campañas concurrentes |

## Execution Metrics

El monitor emite métricas a `bot_metrics` en PostgreSQL (fire-and-forget):

| Métrica | Descripción |
|---------|-------------|
| `pending_replaced` | Limit order reemplazada por un setup mejor (count=1 per event) |
| `pending_timeout` | Limit order expirada sin fill (count=1 per event) |
| `pending_filled` | Limit order filled exitosamente (count=1 per event) |
| `time_to_fill_seconds` | Segundos entre placement y fill (label: setup_type) |

**Fill rate**: `pending_filled / (pending_filled + pending_timeout + pending_replaced)` — calculable desde Grafana.

**Queries útiles:**
```sql
-- Fill rate global
SELECT
  SUM(CASE WHEN metric_name = 'pending_filled' THEN 1 ELSE 0 END)::float /
  NULLIF(COUNT(*), 0) AS fill_rate
FROM bot_metrics
WHERE metric_name IN ('pending_filled', 'pending_timeout', 'pending_replaced');

-- Average time to fill by setup
SELECT labels->>'setup_type', AVG(value) AS avg_seconds
FROM bot_metrics WHERE metric_name = 'time_to_fill_seconds'
GROUP BY labels->>'setup_type';
```

## Live Test Script

`tests/test_execution_live.py` — script manual para probar órdenes en OKX live:
1. Configura ETH/USDT (isolated, 5x leverage, TRADE_CAPITAL_PCT sizing)
2. Coloca limit buy at ask+0.1% **con SL+TP attached** ($40 below/above)
3. Espera fill, luego verifica SL/TP en exchange (algo orders + open orders)
4. Fallback a placement manual si attached no se encuentran
5. Cleanup: cancela SL/TP, cierra posición

## Phantom fill debug logging

Cuando un fill price difiere >0.5% del precio límite esperado, el monitor logea un WARNING con los campos raw de OKX:
```
Fill price mismatch: ETH/USDT expected=1937.22 actual=1990.24 diff=2.74%
  raw={status, average, price, filled, type, side, info_avgPx, info_px, info_state}
```
`info_avgPx` e `info_px` son los campos nativos de OKX antes de la transformación ccxt. Esto ayuda a diagnosticar si OKX reporta mal el precio o ccxt lo transforma incorrectamente.

## PnL Tracking

`_calculate_pnl(pos, exit_price)` computes net PnL after fees on every exit:
- `pnl_usd = raw_pnl - (entry_notional + exit_notional) × TRADING_FEE_RATE`
- Stores `actual_exit_price` on ManagedPosition
- `_persist_trade_close()` writes `actual_exit=pos.actual_exit_price` to PostgreSQL

All exit paths now compute PnL before closing:
- TP hit, SL hit, breakeven SL, trailing SL (via `_close_position` which calls `_calculate_pnl`)
- Emergency close, excessive slippage, SL too close, emergency retry, timeout — all extract close price from market close result and call `_calculate_pnl` before `_close_position`

## Limitaciones conocidas

- Estado de posiciones se pierde en restart (SL/TP siguen en exchange, positions re-adopted via sync_exchange_positions)
- Orphaned trades reconciled on startup (closed as `orphaned_restart`)
- `AIDecision.adjustments` no se aplica a SL/TP (v2)
