# Sistema de Alertas Inteligentes

**Status:** Pendiente
**Prioridad:** Media-Alta
**Esfuerzo:** ~6-8 horas

## What

Reemplazar el `TelegramNotifier` plano por un sistema de alertas con prioridades, escalamiento, silenciamiento y agrupamiento. Sigue usando Telegram como canal (no Prometheus Alertmanager -- overkill para one-man operation).

## Why

Problemas actuales:
1. **Sin prioridades** -- un emergency close y un OB summary usan el mismo canal
2. **Spam durante issues conocidos** -- si OKX WS se desconecta 10 veces, no hay silenciamiento
3. **Whale spam** -- 10 movimientos whale en 5 min = 10 mensajes separados
4. **Health check silencioso** -- `data_service._health_check_loop()` solo loguea, nunca notifica por Telegram cuando Redis/PG/WS se caen
5. **Sin rate limit** -- nada previene 50 mensajes en 1 minuto
6. **Perdida de alertas criticas** -- si Telegram falla durante emergencia, se pierde sin retry

## Current State (verificado leyendo codigo)

- `shared/notifier.py`: 10 metodos de notificacion, todos fire-and-forget via `httpx.POST`. Timeout 10s. Si falla, loguea WARNING. Sin retry. `send()` no retorna bool.
- `execution_service/monitor.py`: usa `_safe_notify()` (fire-and-forget con `asyncio.create_task`). Emergency close llama `notify_emergency()`.
- `data_service/service.py`: health check cada 30s loguea a loguru. Whale movements notifican uno por uno.
- `main.py`: notifier se crea en `main()` y pasa a DataService y ExecutionService.

**Lo que NO existe:** prioridades, escalamiento, silenciamiento, agrupamiento, rate limiting.

## Steps

### Paso 1: Crear `shared/alert_manager.py` con AlertManager

```python
class AlertPriority(Enum):
    INFO = "info"           # OB summary, hourly status
    WARNING = "warning"     # AI rejection, WS reconnecting
    CRITICAL = "critical"   # Trade opened/closed, daily DD > 2%
    EMERGENCY = "emergency" # SL placement failed, emergency close

class AlertManager:
    def __init__(self, notifier: TelegramNotifier):
        self._notifier = notifier
        self._silenced: dict[str, float] = {}
        self._batch_buffer: dict[str, list] = {}
        self._rate_limiter: dict[str, list] = {}

    async def alert(self, priority: AlertPriority, category: str, message: str):
        """Route through silencing, dedup, rate limit, batching."""
```

**Routing por prioridad:**

| Prioridad | Rate limit | Retry | Ejemplo |
|-----------|------------|-------|---------|
| INFO | Max 10/hora | No | OB summary, hourly status |
| WARNING | Max 5/15min | No | AI rejected, WS reconnect |
| CRITICAL | Max 20/hora | 1 retry a 5s | Trade open/close |
| EMERGENCY | Sin limite | 3 retries (5s/15s/30s) | SL failed, emergency close failed |

**Done when:** AlertManager enruta correctamente por prioridad con formatting diferenciado.

### Paso 2: Silenciamiento por categoria

**Categorias predefinidas:**

| Categoria | Descripcion | Silenciable? |
|-----------|-------------|-------------|
| `ws_reconnect` | WebSocket reconnections | Si |
| `whale_movement` | Whale movements | Si |
| `ai_decision` | AI approvals/rejections | Si |
| `risk_rejection` | Risk guardrail rejections | Si |
| `health_check` | Infrastructure health | Si |
| `ob_summary` | Order block summaries | Si |
| `trade_lifecycle` | Trade open/close | No |
| `emergency` | Emergency events | **NUNCA** |

**Auto-silenciamiento:** Despues de 3 alertas de la misma categoria en 5 minutos, auto-silence por 15 minutos (excepto EMERGENCY y trade_lifecycle).

**Done when:** `silence(category, duration)` funciona. EMERGENCY nunca se puede silenciar.

### Paso 3: Agrupamiento (batching) para whale movements

- Whale movements dentro de ventana de 2 minutos se agrupan en un digest
- Formato: "WHALE DIGEST (4 movements in 5 min)" con resumen net signal
- High significance bypasea el batch y se envia inmediato

**Done when:** Multiples whale movements se agrupan en un solo mensaje.

### Paso 4: Rate limiting global

```python
RATE_LIMITS = {
    AlertPriority.INFO: (10, 3600),       # 10 per hour
    AlertPriority.WARNING: (5, 900),      # 5 per 15 min
    AlertPriority.CRITICAL: (20, 3600),   # 20 per hour
    AlertPriority.EMERGENCY: (100, 3600), # effectively unlimited
}
```

Cuando se excede: loguea (siempre visible en loguru), NO envia a Telegram. En hourly status reporta alertas suprimidas.

**Done when:** Sliding window rate limiter funciona correctamente.

### Paso 5: Escalamiento EMERGENCY con retry

```python
async def _send_with_escalation(self, message: str):
    delays = [0, 5, 15, 30]
    for attempt, delay in enumerate(delays):
        if delay > 0:
            await asyncio.sleep(delay)
        success = await self._notifier.send(message)
        if success:
            return True
    logger.critical(f"EMERGENCY UNDELIVERABLE: {message}")
    return False
```

**Cambio requerido:** `TelegramNotifier.send()` debe retornar `bool`.

**Done when:** EMERGENCY hace retry con backoff si Telegram falla.

### Paso 6: Health check alertas a Telegram → `data_service/service.py`

Cuando infra se cae, envia WARNING. Cuando se recupera, envia INFO. Sin spam (usa flag `_last_health_alerted`).

**Done when:** Health check notifica por Telegram cuando algo se cae/recupera.

### Paso 7: Mapear notificaciones existentes a prioridades

| Metodo actual | Prioridad | Categoria |
|---------------|-----------|-----------|
| `notify_hourly_status()` | INFO | `hourly_status` |
| `notify_ob_summary()` | INFO | `ob_summary` |
| `notify_whale_movement()` | INFO (batched) | `whale_movement` |
| `notify_ai_decision()` (approved) | WARNING | `ai_decision` |
| `notify_ai_decision()` (rejected) | INFO | `ai_decision` |
| `notify_trade_opened()` | CRITICAL | `trade_lifecycle` |
| `notify_trade_closed()` | CRITICAL | `trade_lifecycle` |
| `notify_emergency()` | EMERGENCY | `emergency` |
| Health check DOWN | WARNING | `health_check` |
| Health check RECOVERED | INFO | `health_check` |

**Archivos:** `main.py`, `execution_service/monitor.py`, `data_service/service.py`
**Done when:** Todas las llamadas pasan por `AlertManager.alert()`.

### Paso 8: Config en settings.py

```python
ALERT_RATE_LIMIT_INFO: int = 10
ALERT_RATE_LIMIT_WARNING: int = 5
ALERT_RATE_LIMIT_CRITICAL: int = 20
ALERT_WHALE_BATCH_WINDOW: int = 120       # seconds
ALERT_AUTO_SILENCE_THRESHOLD: int = 3
ALERT_AUTO_SILENCE_DURATION: int = 900    # seconds (15 min)
```

**Done when:** Settings configurables.

### Paso 9: Tests → `tests/test_alert_manager.py` (nuevo)

Cubrir: routing por prioridad, silenciamiento (EMERGENCY ignora), rate limiting, batching de whales, escalamiento con retry, auto-silence.

**Done when:** Tests pasan.

### Paso 10: Documentar → `docs/context/`

**Done when:** Docs actualizados.

## Risks

| Riesgo | Impacto | Mitigacion |
|--------|---------|------------|
| **Telegram rate limit** (20 msg/min per chat) | Bajo | Nuestro rate limiter es mas conservador |
| **Batching delay** para whales (2 min) | Bajo | High significance bypasea batch |
| **In-memory state** se pierde en restart | Bajo | Aceptable -- restart toma 5 segundos |
| **AlertManager singleton** mal instanciado | Medio | Crear en `main.py` una vez, pasar como argumento |

## Out of Scope

- **Prometheus Alertmanager** -- Overkill. Requiere Prometheus server + Alertmanager binary + config YAML + container extra. Un modulo Python de ~200 lineas hace lo mismo para un one-man bot
- **PagerDuty / Opsgenie** -- Servicios pagos para equipos
- **SMS fallback** -- Telegram ya llega al celular con push. Si es poco confiable, considerar ntfy.sh ($0)
- **Telegram bot commands** ("/silence whale 30m") -- Cool pero agrega complejidad. Fase 2
- **Dashboard integration** -- Mostrar alertas activas en UI. Fase 2
