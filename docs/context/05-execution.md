# Execution Service (Layer 5)
> Última actualización: 2026-03-04
> Estado: **implementado** — 28 tests passing. Audited — 5 CRITICAL + 6 IMPORTANT + 3 MINOR fixes applied.

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
2. **Valida precio ordering** — Long: `sl < entry < tp1 < tp2 < tp3`. Short: inverso. Rechaza si inválido.
3. Configura el par (margin mode isolado + leverage)
4. Coloca limit entry order al precio calculado (50% OB/FVG)
5. Notifica Risk Service inmediatamente (en PLACE, no en fill — para conteo correcto)
6. Registra la posición en el monitor
7. El monitor (polling cada 5s) gestiona el ciclo de vida

## Máquina de estados

```
pending_entry ──[fill]──────> active         (coloca SL + TP1/TP2/TP3)
pending_entry ──[15min]─────> closed         (cancela entry, NO cuenta como trade en Risk)

active ──[TP1 fills]──> tp1_hit              (SL → breakeven)
active ──[SL fills]───> closed               (cancela todos los TPs)
active ──[12h]────────> closed               (market close todo)
active ──[SL fail]────> emergency_pending    (SL placement fails → emergency retry)

tp1_hit ──[TP2 fills]──> tp2_hit             (SL → nivel TP1)
tp1_hit ──[SL fills]───> closed

tp2_hit ──[TP3 fills]──> closed              (posición completamente cerrada)
tp2_hit ──[SL fills]───> closed

emergency_pending ──[retry ok]──> closed     (market close exitoso)
emergency_pending ──[3 fails]──> emergency_failed  (requiere intervención manual)
```

## Tipos de órdenes

| Orden | Tipo | Por qué |
|-------|------|---------|
| Entry | Limit | Control de slippage. Cancela si no se llena en 15 min |
| Stop Loss | Stop-market (algo order) | Ejecución garantizada en crashes. OKX `ordType: "conditional"` para routing correcto de algo orders |
| TP1/TP2/TP3 | Limit (reduceOnly) | Precios exactos, sin slippage en take profits |

## Distribución de TPs

- **TP1**: 50% de la posición a 1:1 R:R → SL se mueve a breakeven
- **TP2**: 30% a 1:2 R:R → SL se mueve a nivel TP1
- **TP3**: 20% restante → trailing o siguiente nivel de liquidez

## Reglas de seguridad críticas

1. **Validación de precios en execute().** Long: `sl < entry < tp1 < tp2 < tp3`. Short: `sl > entry > tp1 > tp2 > tp3`. Rechaza trades con precios inválidos antes de tocar el exchange.
2. **Entry fill + SL falla → EMERGENCY market close con retry.** Nunca hay posición abierta sin SL. Máximo 3 reintentos (fase `emergency_pending`). Tras 3 fallos → `emergency_failed`, se mantiene en tracking para intervención manual. Envía alerta Telegram.
3. **TP placement falla → EMERGENCY close.** Si cualquier TP falla al colocarse, cancela todos los TPs y SL colocados, y cierra por market. Un TP faltante impide mover SL a breakeven (TP1 nunca llena → SL nunca se ajusta).
4. **Ajuste de SL: nuevo ANTES de cancelar viejo.** Cero ventana sin protección. Race window mitigada por `reduceOnly` — si ambos SL se ejecutan, el segundo cierra size=0. TODO: migrar a OKX amend-order API para updates atómicos.
5. **Notificación a Risk: en PLACE, no en fill.** Si hay 2 entries pendientes, Risk los cuenta como 2 posiciones abiertas.
6. **Cancelled entries no cuentan como trades.** Si el entry timeout cancela una orden que nunca se llenó, no se notifica a Risk ni se envía Telegram de trade cerrado.
7. **Shutdown: cancela entries pendientes, NO cierra posiciones activas.** Los SL/TP viven en el exchange y sobreviven al bot.
8. **Telegram notifications:** Entry fill → `notify_trade_opened`, position close → `notify_trade_closed`, SL/TP fail → `notify_emergency`. Fire-and-forget via `_safe_notify()` con error logging callback (no `ensure_future`).
9. **DB persistence guards.** `_persist_trade_open/close` verifica que tanto `_data_store` como `.postgres` no sean None antes de escribir.

## Slippage tracking

Cada fill logea precio esperado vs real con % de diferencia:
```
Slippage: BTC/USDT expected=50000.00 actual=50025.00 diff=0.0500%
```

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `execution_service/__init__.py` | Exporta ExecutionService |
| `execution_service/service.py` | Facade — execute(), start(), stop(), health() |
| `execution_service/executor.py` | Wrapper ccxt — place/cancel/fetch orders (con fallback a algo orders) |
| `execution_service/monitor.py` | Background loop — máquina de estados + notificaciones Telegram |
| `execution_service/models.py` | ManagedPosition (estado mutable interno, incluye `emergency_retries`, `realized_pnl_usd`) |

## Settings

| Setting | Default | Descripción |
|---------|---------|-------------|
| `ENTRY_TIMEOUT_SECONDS` | 900 (15 min) | Tiempo máximo de espera para fill |
| `ORDER_POLL_INTERVAL` | 5.0s | Intervalo de polling del monitor |
| `MARGIN_MODE` | "isolated" | Modo de margen (más seguro) |
| `MAX_TRADE_DURATION_SECONDS` | 43200 (12h) | Duración máxima de un trade |

## PnL Calculation — Blended

El PnL se calcula de forma blended: acumula PnL realizado de cada TP fill + PnL no realizado del tamaño restante al precio de salida.

```
total_pnl_usd = realized_from_TPs + unrealized_remainder
pnl_pct = total_pnl_usd / (entry_price × filled_size)
```

Cada vez que un TP llena, `_accumulate_realized_pnl()` calcula y suma el PnL de esa tranche a `pos.realized_pnl_usd`. Al cerrar (SL, timeout, TP3), `_calculate_pnl()` combina ambos para el PnL final reportado a Risk Service.

## Tests (28)

- Facade: disabled sin API key, happy path, short/sell side, pair ya gestionado, fallos
- **SL/TP validation**: long inválido (SL arriba de entry), short inválido (SL abajo de entry)
- Entry fill: coloca SL + 3 TPs
- Entry timeout: cancela después de 15 min
- TP1 hit: SL → breakeven
- TP2 hit: SL → nivel TP1
- **TP3 hit**: posición cerrada, SL cancelado
- SL hit: cancela todos los TPs
- 12h timeout: market close + cancela todo
- Emergency close: SL falla → market close
- **SL adjustment failure**: mantiene SL viejo si nuevo falla
- Slippage: logging verificado
- PnL: cálculo correcto long/short profit/loss, **blended PnL con realized**

## OKX Algo Order Handling

OKX trata stop-market orders como "algo orders" con routing separado:
- **`place_stop_market()`** envía `params["ordType"] = "conditional"` para que ccxt/OKX use el endpoint de algo orders.
- **`fetch_order()`** intenta primero fetch normal; si recibe `OrderNotFound`, hace fallback a `_fetch_algo_order()`.
- **`_fetch_algo_order()`** busca en `fetch_open_orders` y `fetch_canceled_and_closed_orders` con `{"ordType": "conditional"}`.
- Usa `asyncio.get_running_loop()` (no el deprecated `get_event_loop()`).

## Decisiones de diseño

| Decisión | Elección | Por qué |
|----------|----------|---------|
| Monitoreo | Polling 5s | 5-15 trades/semana no justifica WebSocket |
| Margin | Isolated | Cada posición tiene su propio margen |
| Estado | In-memory (MVP) | SL/TP viven en exchange, sobreviven crash. Redis en v2 |
| Position mode | One-way (net) | No long+short simultáneo en mismo par |
| Modelos internos | execution_service/models.py | No son inter-capa, no van en shared/ |

## Limitaciones conocidas

- Estado de posiciones se pierde en restart (SL/TP siguen en exchange)
- No hay detección de posiciones huérfanas al reiniciar (v2)
- No hay trailing stop para TP3 (usa limit fijo por ahora — ver roadmap v2)
- Sin persistencia Redis del estado del monitor (v2)
- `AIDecision.adjustments` no se aplica a SL/TP (v2)

## Roadmap v2 — Trailing Stop para TP3

### Contexto
CLAUDE.md especifica para TP3: *"trailing stop or next liquidity level for remaining 20%"*. La implementación actual usa una **limit order fija** al `tp3_price` calculado por el Strategy Service (siguiente nivel de liquidez). Esto funciona pero deja dinero en la mesa cuando el precio sigue moviéndose a favor.

### Plan de implementación

**1. Nuevo setting:**
```python
# config/settings.py
TRAILING_STOP_CALLBACK_PCT: float = 0.005  # 0.5% callback ratio
TRAILING_STOP_ENABLED: bool = False         # Off por default hasta validar en sandbox
```

**2. API de OKX para trailing stop:**
OKX soporta trailing stops via `trigger-order` con parámetros:
- `ordType: "move_order_stop"`
- `callbackRatio`: porcentaje de retroceso que activa el stop (e.g., "0.005" = 0.5%)
- `callbackSpread`: alternativa en precio absoluto
- `triggerPxType`: "last" (precio de último trade)

**3. Cambio en el monitor (PositionMonitor):**
Cuando `phase` transiciona a `tp2_hit`:
1. Cancelar el TP3 limit order existente
2. Colocar trailing stop con `callbackRatio` configurado
3. Guardar el nuevo order ID en `pos.tp3_order_id`
4. El monitor sigue igual — cuando el trailing stop se ejecuta, `status == "closed"` y se cierra la posición

**4. Nuevo método en OrderExecutor:**
```python
async def place_trailing_stop(
    self, pair: str, side: str, amount: float, callback_pct: float
) -> Optional[dict]:
    """Place trailing stop order. Used for TP3 after TP2 fills."""
```

**5. Consideraciones:**
- **Testing:** Los trailing stops se comportan diferente a limits en mercados volátiles. Requiere 2+ semanas de observación en sandbox antes de activar en live.
- **Fallback:** Si `TRAILING_STOP_ENABLED=False` o si la colocación falla, mantener el TP3 limit original (no perder la protección).
- **Slippage:** Los trailing stops se ejecutan como market orders cuando se activan — el slippage será mayor que con limits. Logear y monitorear.
- **OKX quirks:** Verificar si OKX permite trailing stops en sandbox mode (algunos features solo están en live).

### Otras mejoras v2
- Persistencia de estado del monitor en Redis (sobrevivir restarts)
- Detección de posiciones huérfanas al reiniciar (query `fetch_positions()` y reconciliar)
- Aplicar `AIDecision.adjustments` a SL/TP antes de ejecutar
