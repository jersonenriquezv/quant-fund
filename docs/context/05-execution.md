# Execution Service (Layer 5)
> Estado: **implementado** — 20 tests passing

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
2. Configura el par (margin mode isolado + leverage)
3. Coloca limit entry order al precio calculado (50% OB/FVG)
4. Notifica Risk Service inmediatamente (en PLACE, no en fill — para conteo correcto)
5. Registra la posición en el monitor
6. El monitor (polling cada 5s) gestiona el ciclo de vida

## Máquina de estados

```
pending_entry ──[fill]──────> active         (coloca SL + TP1/TP2/TP3)
pending_entry ──[15min]─────> closed         (cancela entry)

active ──[TP1 fills]──> tp1_hit              (SL → breakeven)
active ──[SL fills]───> closed               (cancela todos los TPs)
active ──[12h]────────> closed               (market close todo)

tp1_hit ──[TP2 fills]──> tp2_hit             (SL → nivel TP1)
tp1_hit ──[SL fills]───> closed

tp2_hit ──[TP3 fills]──> closed              (posición completamente cerrada)
tp2_hit ──[SL fills]───> closed
```

## Tipos de órdenes

| Orden | Tipo | Por qué |
|-------|------|---------|
| Entry | Limit | Control de slippage. Cancela si no se llena en 15 min |
| Stop Loss | Stop-market | Ejecución garantizada en crashes. NO stop-limit |
| TP1/TP2/TP3 | Limit (reduceOnly) | Precios exactos, sin slippage en take profits |

## Distribución de TPs

- **TP1**: 50% de la posición a 1:1 R:R → SL se mueve a breakeven
- **TP2**: 30% a 1:2 R:R → SL se mueve a nivel TP1
- **TP3**: 20% restante → trailing o siguiente nivel de liquidez

## Reglas de seguridad críticas

1. **Entry fill + SL falla → EMERGENCY market close.** Nunca hay posición abierta sin SL. Envía alerta Telegram.
2. **Ajuste de SL: nuevo ANTES de cancelar viejo.** Cero ventana sin protección.
3. **Notificación a Risk: en PLACE, no en fill.** Si hay 2 entries pendientes, Risk los cuenta como 2 posiciones abiertas.
4. **Shutdown: cancela entries pendientes, NO cierra posiciones activas.** Los SL/TP viven en el exchange y sobreviven al bot.
5. **Telegram notifications:** Entry fill → `notify_trade_opened`, position close → `notify_trade_closed`, SL fail → `notify_emergency`. Fire-and-forget via `asyncio.ensure_future`.

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
| `execution_service/executor.py` | Wrapper ccxt — place/cancel/fetch orders |
| `execution_service/monitor.py` | Background loop — máquina de estados + notificaciones Telegram |
| `execution_service/models.py` | ManagedPosition (estado mutable interno) |

## Settings

| Setting | Default | Descripción |
|---------|---------|-------------|
| `ENTRY_TIMEOUT_SECONDS` | 900 (15 min) | Tiempo máximo de espera para fill |
| `ORDER_POLL_INTERVAL` | 5.0s | Intervalo de polling del monitor |
| `MARGIN_MODE` | "isolated" | Modo de margen (más seguro) |
| `MAX_TRADE_DURATION_SECONDS` | 43200 (12h) | Duración máxima de un trade |

## Tests (20)

- Facade: disabled sin API key, happy path, short/sell side, pair ya gestionado, fallos
- Entry fill: coloca SL + 3 TPs
- Entry timeout: cancela después de 15 min
- TP1 hit: SL → breakeven
- TP2 hit: SL → nivel TP1
- SL hit: cancela todos los TPs
- 12h timeout: market close + cancela todo
- Emergency close: SL falla → market close
- Slippage: logging verificado
- PnL: cálculo correcto long/short profit/loss

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
