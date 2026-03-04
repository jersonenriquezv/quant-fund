# Arquitectura del Sistema
> Última actualización: 2026-03-04
> Estado: **5/5 capas implementadas** — pipeline completo Data → Strategy → AI → Risk → Execution. Auditoría completa realizada: 12 CRITICALs corregidos.

## Qué hace (para entenderlo rápido)
El sistema es un bot de trading que funciona como una línea de ensamblaje. Los datos entran por un lado, pasan por 5 filtros en orden, y si todos dicen "sí", se ejecuta el trade. Si cualquier filtro dice "no", el trade se descarta.

## Por qué existe
Sin esta arquitectura, tendríamos un solo programa gigante donde todo está mezclado. Si algo falla, todo falla. Con 5 servicios separados, si el AI Service se cae, el Risk Service sigue protegiendo el capital. Cada pieza hace una sola cosa bien.

## Diagrama del sistema

```
                    ┌─────────────┐
                    │   OKX API   │ ← Exchange de crypto
                    │  Etherscan  │ ← Datos on-chain ETH
                    └──────┬──────┘
                           │ datos en tiempo real
                           ▼
                ┌──────────────────┐
                │  1. DATA SERVICE │ ← Recoge y limpia datos
                │  (el periodista) │
                └────────┬─────────┘
                         │ OHLCV, volumen, OI, funding, on-chain
                         ▼
              ┌────────────────────┐
              │ 2. STRATEGY SERVICE│ ← Detecta patrones SMC
              │  (el detective)    │
              └────────┬───────────┘
                       │ "Encontré un Setup A/B en BTC/USDT"
                       ▼
              ┌────────────────────┐
              │  3. AI SERVICE     │ ← Claude evalúa contexto
              │  (el consultor)    │
              └────────┬───────────┘
                       │ "Aprobado, confianza 0.75"
                       ▼
              ┌────────────────────┐
              │  4. RISK SERVICE   │ ← Verifica guardrails
              │  (el guardián)     │
              └────────┬───────────┘
                       │ "Aprobado, position size = 0.05 ETH"
                       ▼
              ┌────────────────────┐
              │ 5. EXECUTION       │ ← Ejecuta la orden
              │  (el ejecutor)     │
              └────────┬───────────┘
                       │ orden de compra/venta
                       ▼
                ┌──────────────┐
                │   OKX API    │
                └──────────────┘

        ┌─────────────────────────────┐
        │  TELEGRAM NOTIFIER          │ ← Push al celular
        │  (observador silencioso)    │
        └─────────────────────────────┘
          ↑ Notifica en cada evento clave:
          │ setup detectado, AI aprobó/rechazó,
          │ risk rechazó, trade abierto/cerrado,
          │ emergencias
```

## Cómo se comunican los servicios
1. Data Service recoge datos de OKX, Etherscan (liquidaciones via OI proxy, no Binance)
2. Cuando hay una vela nueva (cada 5m/15m), manda los datos al Strategy Service
3. Strategy Service analiza los datos buscando patrones SMC
4. Si encuentra un setup completo (Setup A o B con confluencia), lo manda al AI Service
5. AI Service le pregunta a Claude (Sonnet): "¿el contexto apoya este trade?"
6. Si Claude dice sí (confianza ≥ 0.60), el setup pasa al Risk Service
7. Risk Service verifica TODOS los guardrails y calcula el position size
8. Execution Service coloca la orden limit en OKX, con SL (stop-market) y 3 TPs (limit)
9. PositionMonitor gestiona el ciclo de vida: entry fill → TP1 (SL→breakeven) → TP2 (SL→TP1) → TP3/SL

**Regla clave:** Si CUALQUIER servicio dice NO, el trade se descarta. No hay "pero" ni "tal vez".

**Notificaciones Telegram:** En cada paso del pipeline (setup detectado, AI decision, risk rejection, trade abierto/cerrado, emergencias), el bot envía push notification al celular via Telegram Bot API. Fire-and-forget — si Telegram falla, el bot sigue operando.

## Detalles técnicos

### Comunicación entre servicios
Por ahora: llamadas directas entre módulos Python (simple, sin overhead). Si el bot crece, se puede migrar a Redis pub/sub o message queues.

### Almacenamiento
- **Redis:** Cache de datos en tiempo real. Último precio, última vela, estado del bot.
- **PostgreSQL:** Histórico de trades, velas pasadas, logs de decisiones.

### Infraestructura
- **Servidor:** Acer Nitro 5 (i5-9300H, 16GB RAM) con Ubuntu Server 24.04
- **IP:** 192.168.1.238
- **Contenedores:** Docker Compose (bot + PostgreSQL + Redis)
- **Desarrollo:** VS Code Remote SSH desde PC principal

### Docker Compose — Deployment

El bot corre en 3 containers via `docker-compose.yml`:

| Servicio | Imagen | Puerto | Propósito |
|----------|--------|--------|-----------|
| `postgres` | postgres:16-alpine | 127.0.0.1:5432 | Almacenamiento histórico (candles, trades, AI decisions) |
| `redis` | redis:7-alpine | 127.0.0.1:6379 | Cache en tiempo real (último precio, OI, funding, estado) |
| `bot` | python:3.12-slim (build local) | — | Bot de trading (5 capas) |

**Archivos Docker:**
- `.dockerignore` — Excluye .git, venv, tests, docs, .env del build
- `Dockerfile` — python:3.12-slim, pip install, `python -u main.py`, healthcheck via pgrep
- `docker-compose.yml` — 3 servicios con healthchecks, named volumes, `restart: unless-stopped`

**Configuración clave:**
- **Bot usa `network_mode: host`** — Docker bridge no tiene NAT configurado en el server. Con host network, el bot accede a Postgres/Redis en localhost directamente y tiene acceso a internet para OKX/Etherscan/Claude API.
- **Build usa `network: host`** — Para que `pip install` pueda descargar paquetes de PyPI durante el build.
- **Dos archivos `.env`:**
  - Root `.env` — Docker Compose variable interpolation (`${POSTGRES_PASSWORD}`)
  - `config/.env` — Secrets del bot (OKX, Anthropic, Etherscan). Montado read-only en `/app/config/.env`
- **Volumes:** `pgdata` y `redisdata` persisten datos entre restarts.
- **Puertos:** Solo `127.0.0.1` (no expuestos a la red externa).
- **Redis persistence:** `--appendonly yes` para durabilidad.
- **Graceful shutdown:** `stop_grace_period: 30s` para que el bot cierre WebSockets y cancele entries pendientes.

**Comandos:**
```bash
docker compose up -d          # Arrancar todo
docker compose logs bot -f    # Ver logs del bot en vivo
docker compose down           # Parar (volumes se preservan)
docker compose down -v        # Parar y borrar volumes (reset total)
docker compose build --no-cache  # Rebuild después de cambios en código
```

## Glosario
- **BOS:** Break of Structure. Cuando el precio rompe un máximo/mínimo anterior, confirmando la tendencia.
- **CHoCH:** Change of Character. Cuando el precio rompe en dirección opuesta — posible cambio de tendencia.
- **OB:** Order Block. Zona donde las instituciones acumularon órdenes grandes. Es como una "huella" que dejan.
- **FVG:** Fair Value Gap. Un "hueco" en el precio que el mercado tiende a llenar después.
- **Sweep:** Cuando el precio barre los stop losses de otros traders y regresa. Las instituciones "cazan" la liquidez.
- **CVD:** Cumulative Volume Delta. Muestra quién está comprando más vs vendiendo más en un periodo.
- **OI:** Open Interest. Cuántos contratos de futuros están abiertos. Indica flujo de capital nuevo.
- **HTF/LTF:** Higher/Lower Time Frame. Timeframes grandes (4H, 1H) vs pequeños (15m, 5m).
- **SMC:** Smart Money Concepts. Teoría de trading que estudia cómo operan las instituciones para seguir sus movimientos.
- **Setup:** Una combinación de patrones que indica una oportunidad de trade.
- **Confluencia:** Múltiples señales apuntando en la misma dirección. Más confluencia = más confianza.

## Estado actual de cada capa

| Capa | Estado | Tests | Archivo principal |
|------|--------|-------|-------------------|
| 1. Data Service | Implementado + auditoría | 81 | `data_service/service.py` |
| 2. Strategy Service | Implementado + auditoría | 76 | `strategy_service/service.py` |
| 3. AI Service | Implementado | 34 | `ai_service/service.py` |
| 4. Risk Service | Implementado | 69 | `risk_service/service.py` |
| 5. Execution Service | Implementado + auditoría | 20 | `execution_service/service.py` |
| **Total** | **5/5 completas** | **280** | `main.py` (pipeline completo) |

## Roadmap v2

### Trailing stop para TP3
Actualmente TP3 usa una limit order fija al siguiente nivel de liquidez. CLAUDE.md especifica "trailing stop or next liquidity level". La implementación v2:
- OKX soporta trailing stops via API (`trigger-order` con `callbackRatio`)
- Cuando `phase == "tp2_hit"`, cancelar el TP3 limit y colocar trailing stop
- Nuevo setting: `TRAILING_STOP_CALLBACK_PCT` (e.g., 0.5% = $250 en BTC a $50k)
- Nuevo estado en máquina de estados: `tp2_hit` → trailing en vez de limit fijo
- Requiere testing extensivo en sandbox — trailing stops se comportan diferente a limits en volátil

### Otras mejoras v2
- Persistencia de estado del monitor en Redis (sobrevivir restarts)
- Detección de posiciones huérfanas al reiniciar
- Aplicar `AIDecision.adjustments` a SL/TP antes de ejecutar
- Reconstruir estado de Risk Service desde PostgreSQL al arrancar
- Ver `docs/to-fix.md` para backlog completo (~30 IMPORTANT + 29 MINOR issues)

## Cambios recientes
- 2026-03-04: **Auditoría completa** — 12 CRITICAL corregidos (PG reconnection, pipeline locks, OKX algo orders, emergency close retry, sweep temporal guard, OB break_timestamp, etc.). 28 IMPORTANT + 29 MINOR documentados en `docs/to-fix.md`.
- 2026-03-04: BTC whale tracking via mempool.space.
- 2026-03-04: Telegram notifications — push al celular en cada evento clave del pipeline (`shared/notifier.py`).
- 2026-03-04: Docker Compose deployment — bot + PostgreSQL + Redis containerizados.
- 2026-03-04: Las 5 capas implementadas. Pipeline completo Data → Strategy → AI → Risk → Execution.
- 2026-03-03: Documento inicial creado con arquitectura de 5 capas.