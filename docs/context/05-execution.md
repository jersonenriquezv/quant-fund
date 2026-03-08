# Execution Service (Layer 5)
> Última actualización: 2026-03-08
> Estado: **Fase 1 — simplificado**. SL + single TP. Breakeven via price polling.

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
4. Configura el par (margin mode isolated + leverage)
5. Coloca limit entry order al precio calculado (50% OB/FVG)
6. Notifica Risk Service inmediatamente (en PLACE, no en fill)
7. Registra la posición en el monitor

## Máquina de estados (Fase 1 — simplificada)

```
pending_entry ──[fill]──────> active         (coloca SL + single TP a 2:1 R:R)
pending_entry ──[4h/1h]─────> closed         (cancela entry)

active ──[TP fills]──> closed                (profit)
active ──[SL fills]──> closed                (loss o breakeven)
active ──[price >= 1:1 R:R]──> SL moves to breakeven
active ──[12h/4h]────> closed                (market close)
active ──[SL fail]───> emergency_pending     (retry x3)

emergency_pending ──[retry ok]──> closed
emergency_pending ──[3 fails]──> emergency_failed  (intervención manual)
```

## Exit Management (simplificado)

- **SL**: stop-market al `sl_price` por 100% de la posición
- **TP**: limit reduceOnly al `tp2_price` (2:1 R:R) por 100%
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
2. **SL placement retries** — 3 intentos con delays 0.3s/0.6s. Si falla → emergency market close.
3. **TP falla → SL protege** — Antes se hacía emergency close si TP fallaba. Ahora el SL queda activo y es suficiente.
4. **Ajuste SL: nuevo ANTES de cancelar viejo** — Cero ventana sin protección.
5. **Notificación a Risk: en PLACE, no en fill.**
6. **Cancelled entries no cuentan como trades.**
7. **Shutdown: cancela entries pendientes, NO cierra posiciones activas.**

## Breakeven Logic

1. En cada poll cycle (5s), si `breakeven_hit == False`:
2. Fetch ticker via `fetch_ticker(pair)`
3. Long: si `current_price >= tp1_price` → trigger
4. Short: si `current_price <= tp1_price` → trigger
5. On trigger: `_adjust_sl(pos, actual_entry_price)`, set `breakeven_hit = True`
6. Solo se dispara una vez (idempotente)

## SL vanished fallback

Cuando el SL algo order no se encuentra por 12 polls consecutivos (~60s):
- **Position gone** → SL triggered, close in monitor
- **Position exists** → re-place SL at `current_sl_price`
- **Network error** → skip, retry next cycle
- **Re-place fails** → `emergency_pending`

Also handles SL cancelled externally: re-places SL immediately.

## OKX Algo Order Handling

- `place_stop_market()` usa ccxt unified API (ccxt mapea a `slTriggerPx` de OKX)
- `fetch_order()` intenta fetch normal; si `OrderNotFound` → fallback a `_fetch_algo_order()`
- `_fetch_algo_order()` usa OKX native API: pending → effective → canceled
- Error throttling: solo logea primer error y cada 12vo

## Position adoption

Al startup, `sync_exchange_positions()` consulta OKX por posiciones abiertas. Las no trackeadas se adoptan como `ManagedPosition(setup_type="manual")`:
- Monitor las vigila via `fetch_position()` polling
- Si la posición desaparece → `manual_close`
- Permite nueva entry del bot en el mismo par (OKX net mode stacking)

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `service.py` | Facade — execute(), start(), stop(), health() |
| `executor.py` | Wrapper ccxt — place/cancel/fetch orders |
| `monitor.py` | Background loop — máquina de estados simplificada |
| `models.py` | ManagedPosition (SL/TP IDs, breakeven tracking) |

## Settings

| Setting | Default | Descripción |
|---------|---------|-------------|
| `ENTRY_TIMEOUT_SECONDS` | 14400 (4h) | Tiempo máximo de espera para fill (swing) |
| `ENTRY_TIMEOUT_QUICK_SECONDS` | 3600 (1h) | Tiempo máximo de espera para fill (quick) |
| `ORDER_POLL_INTERVAL` | 5.0s | Intervalo de polling del monitor |
| `MARGIN_MODE` | "isolated" | Modo de margen |
| `MAX_TRADE_DURATION_SECONDS` | 43200 (12h) | Duración máxima trade swing |
| `MAX_TRADE_DURATION_QUICK` | 14400 (4h) | Duración máxima quick |

## Live Test Script

`tests/test_execution_live.py` — script manual para probar órdenes en OKX live:
1. Configura ETH/USDT (isolated, 3x)
2. Coloca limit buy $20 debajo del precio actual
3. Coloca SL stop-market $40 debajo del entry
4. Coloca TP limit $40 arriba del entry
5. Espera confirmación, luego cancela todo

## Limitaciones conocidas

- Estado de posiciones se pierde en restart (SL/TP siguen en exchange)
- Sin persistencia Redis del estado del monitor (v2)
- `AIDecision.adjustments` no se aplica a SL/TP (v2)
