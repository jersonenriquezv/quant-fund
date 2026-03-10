# Execution Service (Layer 5)
> Última actualización: 2026-03-10 (Max slippage guard: close if entry slippage > 0.3%.)
> Estado: **Fase 1 — COMPLETADA**. Entry + SL + TP atómicos (attached). Breakeven + trailing SL via price polling.

El brazo ejecutor del bot. Recibe trades aprobados por Risk Service y los ejecuta en OKX via ccxt.

## Arquitectura

```
ExecutionService (facade)
├── OrderExecutor     — wrapper ccxt para órdenes (limit, stop-market, TP, cancel)
├── PositionMonitor   — loop async que gestiona el ciclo de vida de posiciones
└── ManagedPosition   — estado mutable de cada posición (modelo interno)
```

## Flujo de una operación

1. `execute(setup, approval, ai_confidence)` recibe trade aprobado
2. **Valida precio ordering** — Long: `sl < entry < tp2`. Short: inverso.
3. **Chequea posición existente** — Si hay pending_entry → reemplaza. Si hay adoptada → permite coexistencia. Si hay activa del bot → rechaza.
4. Configura el par (margin mode isolated + leverage). `defaultMarginMode` seteado a nivel de exchange en ccxt para evitar fallback a `cross`.
5. Coloca limit entry order al precio calculado (75% OB/FVG) **con SL+TP attached** — OKX crea SL/TP atómicamente cuando el entry se llena.
   - **Contracts conversion**: `amount` en base currency (ETH/BTC) se convierte a contratos OKX internamente via `_to_contracts()`. OKX SWAP `ctVal`: BTC=0.01, ETH=0.1.
6. Notifica Risk Service inmediatamente (en PLACE, no en fill)
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
10. **Failed OB check pre-execute** — En `main.py:on_candle_confirmed`, antes de llamar a `execute()`, se consulta `strategy_service.is_ob_failed(pair, sl_price, entry_price)`. Si el OB ya resultó en pérdida en esta sesión, el trade se descarta. El callback `on_sl_hit` en el monitor notifica a `StrategyService.mark_ob_failed()` cuando un trade cierra con PnL negativo.
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
- **Position gone** → SL triggered, close in monitor
- **Position exists** → re-place SL at `current_sl_price`
- **Network error** → skip, retry next cycle
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

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `service.py` | Facade — execute(), start(), stop(), health(). Position adoption converts contracts→base. `_emit_metric()` wired to executor for Grafana. Accepts `on_sl_hit` callback for failed OB tracking. Sends ORDER PLACED Telegram notification on successful order placement. |
| `executor.py` | Wrapper ccxt — place/cancel/fetch orders. Contracts conversion (`_to_contracts`, `contracts_to_base`). Attached SL/TP on entry. Algo cancel fallback. `find_pending_algo_orders()`. Optional `metrics_callback` emits `okx_order_latency_ms` per order. |
| `monitor.py` | Background loop — attached SL/TP discovery + manual fallback, breakeven + trailing SL via price polling. Post-fill SL distance check (`sl_too_close` close). Slippage guard (`excessive_slippage` close). Sends TRADE CLOSED + EMERGENCY Telegram notifications. |
| `models.py` | ManagedPosition (SL/TP IDs, breakeven + trailing tracking) |

## Settings

| Setting | Default | Descripción |
|---------|---------|-------------|
| `ENTRY_TIMEOUT_SECONDS` | 14400 (4h) | Tiempo máximo de espera para fill (swing) |
| `ENTRY_TIMEOUT_QUICK_SECONDS` | 3600 (1h) | Tiempo máximo de espera para fill (quick) |
| `ORDER_POLL_INTERVAL` | 5.0s | Intervalo de polling del monitor |
| `MARGIN_MODE` | "isolated" | Modo de margen |
| `MAX_TRADE_DURATION_SECONDS` | 43200 (12h) | Duración máxima trade swing |
| `MAX_TRADE_DURATION_QUICK` | 14400 (4h) | Duración máxima quick |
| `MAX_SLIPPAGE_PCT` | 0.003 (0.3%) | Slippage máximo antes de cerrar (live only) |

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

## Limitaciones conocidas

- Estado de posiciones se pierde en restart (SL/TP siguen en exchange)
- Sin persistencia Redis del estado del monitor (v2)
- `AIDecision.adjustments` no se aplica a SL/TP (v2)
