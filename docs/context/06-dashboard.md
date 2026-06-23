# Dashboard — FastAPI + Next.js

## Arquitectura

Dos contenedores separados del bot:
- **api** (FastAPI, puerto 8000) — Lee PostgreSQL + Redis, endpoints read-only
- **web** (Next.js, puerto 3000) — Dashboard UI, se conecta al API

Si el dashboard crashea, el bot sigue operando normalmente.

## API — Endpoints

| Endpoint | Fuente | Devuelve |
|----------|--------|----------|
| `GET /api/health` | Redis ping + PG ping + env | Estado del sistema + `sandbox` boolean |
| `GET /api/market/{pair}` | Redis (candle, funding, OI) | Precio live, funding, OI |
| `GET /api/trades?status=&limit=50` | PostgreSQL trades | Lista de trades paginada |
| `GET /api/trades/{id}` | PG trades + ai_decisions | Detalle de trade con AI reasoning |
| `GET /api/ai/decisions?limit=20` | PG ai_decisions | Evaluaciones recientes de Claude |
| `GET /api/risk` | Redis + PG risk_events | DD, cooldown, eventos recientes. Filters `pending_entry` from open position count (only counts filled positions). |
| `GET /api/candles/{pair}/{tf}?count=100` | PG candles | OHLCV + volume_quote para sparklines y liquidation estimator |
| `GET /api/stats` | PG trades (closed) | Win rate, P&L, profit factor |
| `GET /api/whales?hours=24` | Redis (whale_movements) | Whale movements last N hours |
| `WS /api/ws` | Redis poll cada 2s | Precio live + posiciones |
| `GET /api/strategy/order-blocks` | Redis (`qf:bot:order_blocks`) | OBs activos (ambos pares, LTF) |
| `GET /api/strategy/htf-bias` | Redis (`qf:bot:htf_bias`) | HTF bias por par |
| `GET /api/sentiment` | Redis (`qf:bot:news:fear_greed`) | Fear & Greed score + label |
| `GET /api/headlines` | Redis (`qf:bot:news:headlines:{BTC,ETH}`) | Recent news headlines (CryptoCompare) |
| `POST /api/trades/{pair}/cancel` | Redis write (`qf:cancel_request:{pair}`) | Solicita cancelación de posición (TTL 60s) |
| `GET /api/liquidation/heatmap/{pair}` | PG candles + Redis OI + cache | Estimated liquidation levels (bins con long/short USD) |
| `GET /api/chart/config` | Static | TradingView UDF Datafeed config (resoluciones soportadas: 5/15/60/240/D) |
| `GET /api/chart/symbols?symbol=` | Static | resolveSymbol — LibrarySymbolInfo (solo BTC/USDT, ETH/USDT) |
| `GET /api/chart/search?query=` | Static | searchSymbols — restringido al allowlist BTC/ETH |
| `GET /api/chart/history?symbol=&resolution=&from=&to=` | PG candles | getBars — OHLCV por rango (from/to en segundos UDF), cap 5000 bars |
| `GET /api/chart/live?symbol=&resolution=` | Redis | Vela FORMANDO (en progreso) para el tick real-time. Lee `qf:livecandle:{pair}:5m` (la cachea el bot vía WS, ~1-2s; fallback al `qf:candle:` confirmado). El front la pollea cada 2s y agrega a TFs mayores client-side. `/history` solo da velas cerradas, por eso esto es lo que hace mover la vela intra-barra |
| `GET /api/chart/detections?symbol=&resolution=&to=` | PG candles + detectores OB/FVG (in-memory, read-only) | Overlay de detecciones del bot: zonas OB/FVG activas as-of `to`. Replay incremental, expiración por `current_time_ms`=ts de la barra (sin reloj wall-clock); window 600 barras, corre off event-loop. ~2.5s/call (O(n²)) — uso single-shot |
| `GET /api/chart/detection_timeline?symbol=&resolution=&to=` | igual que `/detections` | **Perf + MTF**: replay por cada TF (HTF de sesgo `1d`+`4h` SIEMPRE + el TF del chart, deduped, en paralelo vía `asyncio.gather`). Devuelve `{zones, as_of, timeframes}`; cada zona trae lifecycle (`born_ts`/`expire_ts`/`spent_ts`), `significant` (por TF) y `source_tf`. Zonas vivas en la última barra del replay → `expire_ts = ZONE_OPEN_TS` (sentinel ~año 3000) para que una zona HTF se renderice as-of cualquier tiempo en un chart LTF. El front lo pide una vez por símbolo/resolución (+ por nueva vela live) y filtra client-side (`zonesAsOf()`) → cero llamadas por-barra |
| `GET /api/shadow/trades?status=&setup_type=&experiment_id=&limit=50` | PG `ml_setups` (read-only) | Shadow "trades" del modo shadow-only. `status=open` → `outcome_type IS NULL` + bound 48h (evita orphans antiguos); `status=closed` → whitelist terminal (`shadow_tp/sl/breakeven/time_stop/timeout`). Scope por `EXPERIMENT_ID` (default settings, overridable). NO toca `trades` (congelada en 43 rows desde 2026-04-09) |
| `GET /api/shadow/stats?setup_type=&experiment_id=` | PG `ml_setups` (read-only) | WR/PF/profit sobre outcomes terminales + breakdown `by_setup_type`. `pnl_usd` YA neto de fees ×2 — no re-deducir. OJO: el headline agregado mezcla brazos benchmark (`bench_engine1_*`); el breakdown los separa |
| `POST /api/manual/calculate` | Pure math | Position sizing & R:R calculator (linear + inverse) |
| `POST /api/manual/trades` | PG manual_trades | Create manual trade (planned) |
| `GET /api/manual/trades` | PG manual_trades | List trades (filter by status/pair) |
| `GET /api/manual/trades/{id}` | PG manual_trades + partials | Trade detail + partial closes |
| `PATCH /api/manual/trades/{id}` | PG manual_trades | Update trade (status transitions, PnL auto-calc) |
| `DELETE /api/manual/trades/{id}` | PG manual_trades | Hard delete trade |
| `POST /api/manual/trades/{id}/partial-close` | PG manual_partial_closes | Record partial close (auto-closes at 100%) |
| `GET /api/manual/balances` | PG manual_balances | Per-pair balance tracking |
| `PUT /api/manual/balances/{pair}` | PG manual_balances | Set/update balance |
| `GET /api/manual/price/{pair}` | Redis candle cache | Current price (maps USD→USDT) |
| `GET /api/manual/analytics` | PG manual_trades + partials | Win rate, avg R, PnL, TP hit rates, breakdowns |
| `GET /manual` | HTML file | Manual trading UI (standalone page, no /api prefix) |

## Frontend — Layout

### Ruta `/chart` — klinecharts (replay + overlay)
Página dedicada (`src/app/chart/page.tsx`, libs `src/lib/chartDatafeed.ts` + `src/lib/detectionOverlay.ts` + `src/lib/positionTool.ts` + `src/lib/drawingTools.ts`, componente `src/components/ChartToolbar.tsx`). Usa **klinecharts 9.8.12** (lazy en esta ruta; bundle ~58 kB; sparklines siguen SVG). Switchers BTC/ETH + 5m/15m/1h/4h/1D (1D usa candles `1d` del DB; 1W no almacenado), panel VOL aparte, tema Apple-dark. `chartDatafeed.ts` mapea las respuestas UDF de `/api/chart/*` (segundos) a klines (ms). Datos via `/api/chart/history`.
- **Live wiring (A3):** en modo live (no-replay) pollea `/api/chart/live` cada 2s (la vela en formación de Redis) y la `updateData` sobre la barra actual → el chart tickea de verdad intra-vela. En 5m usa la vela 5m directa; en TFs mayores agrega (close=precio, high/low actualizados, open=close de la barra previa). `/history` solo da velas cerradas (por eso antes parecía snapshot). (Orderbook depth viz = idea futura aparte.)
- **Reconciliación live (fix gap fantasma):** el poll de 2s solo toca la cola y NUNCA re-pedía `/history` tras la carga inicial. Con la pestaña en background el navegador throttlea/pausa el `setInterval`; al volver, un poll caía un periodo después → empujaba la barra nueva pero la(s) intermedia(s) jamás se empujaron → **hueco permanente esa sesión** (la data en PG/OKX estaba intacta — era render). Además toda barra "forming" empujada (volumen 0, OHLC aprox) nunca se reemplazaba por la vela cerrada real. Fix: `reconcile()` re-pide `/history` y reemplaza las velas cerradas preservando el scroll (`get/setOffsetRightDistance`). Se dispara en (1) `visibilitychange`→visible y (2) cuando el poll detecta salto de periodo (`formed.ts > last.ts + pms`). Silencioso (sin spinner/reset), guardado contra reentradas + debounce 2s, no corre en replay.
- **Bar replay (A5):** toggle "Replay" → barra con play/pause/step + slider + velocidad (1/2/4/8×) + label as-of. Revela historia avanzando un puntero visible-to (avance de 1 vela = `updateData`; saltos = `applyNewData`).
- **Overlay de detecciones (C2) — multi-timeframe:** toggle "Detections" pide `/api/chart/detection_timeline` UNA vez (por símbolo/resolución y por nueva vela live) y filtra client-side con `zonesAsOf()` al mover la barra → zonas aparecen/mitigan/expiran sin llamadas por-barra (scrub instantáneo). **Top-down**: siempre se pintan los gaps de sesgo `1D`+`4H` además de los del TF actual (así en 5m ves la estructura HTF, no solo ruido 5m). Rects custom de klinecharts en `detectionOverlay.ts`; labels `OB↑/↓ <TF>` `FVG↑/↓ <TF>` (ej. `FVG↓ 4H`) ancladas al borde as-of; zonas HTF (1D/4H) con borde más grueso; `lock:true`, bg transparente (el text style default de klinecharts pintaba un chip azul).
- **Toggle "Focus" (curación, ON por defecto, solo chart):** reduce a las pocas zonas accionables. Combina: (1) umbral adaptativo LuxAlgo POR TF (FVG significativo si su barra de desplazamiento `FVG.timestamp`==c2 se movió > `2× la media corrida de |body %|` de ese TF — `significant` precomputado en `detection_timeline`); (2) `curateZones()` client-side: oculta gastadas (OB mitigado / FVG llenado), descarta OBs débiles (`impulse_score < 0.5`), deja solo las que están a ≤`3%` del precio (más la más cercana por TF como ancla de sesgo aunque esté más lejos), y capa a 2 por (TF, tipo). Focus OFF = todo crudo (incl. gastadas atenuadas) para inspección. NADA toca el detector del bot (`fvg.py` sigue con `FVG_MIN_SIZE_PCT` fijo) — filtro puramente visual. Knobs: `OB_MIN_IMPULSE`, `PER_GROUP`, `MAX_DIST_PCT` en `chartDatafeed.ts`. Resultado típico BTC 1h: 11→4 zonas.
- **Position tool (A6) — `positionTool.ts` (estilo TradingView, click-to-place):** botones `+ Long` / `+ Short` **arman** la herramienta (cursor cruz + hint); el siguiente click en el chart suelta el entry en ese precio/tiempo exacto (`convertFromPixel`). Overlay custom (`positionTool`) con caja de reward verde (entry→TP) + risk roja (entry→SL) que arrancan **desde la vela donde se coloca (anchor)** y se extienden al borde derecho (NO a toda la gráfica); líneas + cajas comparten ese borde izquierdo (entry punteada) y labels a la derecha `TP/SL (±%)` + `Entry · R:R`. **Interacción (modelo klinecharts):** click en la posición la selecciona → aparecen los **handles** (puntos blancos, `needDefaultPointFigure` con `styles.point` agrandado); **arrastrar una línea = mover toda la posición** (`performEventPressedMove` traslada entry+SL+TP, R:R se preserva); **arrastrar un handle (punto) = ajustar ese nivel** (SL/TP independientes, R:R recalcula). Dirección implícita por geometría (cruzar entry voltea long↔short). Defaults 1%/2% (2R). `createPointFigures` lee los valores live en cada repaint (R:R/caja/labels sin round-trip a React); `onPressedMoving`/`onPressedMoveEnd` reflejan el R:R al chip del toolbar (el chip lo limpia). Práctica pura — sin persistencia, sin órdenes; read-only. Verificado Playwright (DB real): place/drag-línea(mueve todo)/drag-handle(ajusta nivel)/clear/re-place + 375px sin overflow.
- **Toolbox de dibujo (estilo TradingView) — `drawingTools.ts` + `ChartToolbar.tsx`:** barra vertical de iconos a la izquierda del canvas (fila horizontal scrollable en ≤639px). Herramientas: cursor, trend line (`segment`), ray (`rayLine`), línea horizontal (`horizontalStraightLine`), rectángulo (overlay custom `rectangleZone` — klinecharts trae el FIGURE `rect` pero no un overlay de caja), fib retracement (`fibonacciLine`), Long/Short (rearman el position tool A6 — los botones `+Long`/`+Short` del header se movieron aquí; el chip R:R sigue en el header) y borrar-todo. Los dibujos klinecharts son interactivos nativos (click coloca cada punto; al terminar la herramienta vuelve a cursor, estilo TV). **Persistencia por símbolo** en `localStorage` (`qf-chart-drawings:{pair}`): se guardan `{name, points}` al terminar/arrastrar y se restauran al cargar y al cambiar de símbolo (un trend de BTC no se pinta en ETH; cambiar de TF los conserva — los puntos son timestamp/value). **Borrado:** right-click sobre un dibujo lo elimina (context menu del browser suprimido en el canvas); Esc cancela el dibujo en progreso o el position-arm. GOTCHA klinecharts: dos clicks a <500ms y <5px se tragan como double-click — al dibujar, el 2º punto necesita >500ms o se ignora. Verificado Playwright (DB real): dibujar 4 tipos, persistencia tras reload y switch de símbolo, drag re-guarda, right-click borra, clear-all limpia storage, 375px sin overflow (toolbox en fila, canvas 521px).
- **Retest % histórico en labels — `scripts/chart_retest_stats.py` + `dashboard/api/data/retest_stats.json`:** el label de cada zona muestra el % histórico de que el precio vuelva a tocar zonas de su categoría (`OB↑ 1H · 65%`). El script offline replaya los MISMOS detectores (ventana deslizante 600 barras, paridad con el endpoint) sobre BTC+ETH (1d/4h/1h completos, 15m 8k barras, 5m 10k; ~4 min) y clasifica cada lifecycle: retested (salió de la zona y volvió a tocar) / no_retest (expiró por edad sin toque) / never_left y censored (EXCLUIDAS — sin salida limpia o aún viva al final = outcome desconocido, evita sesgo a la baja). Por (tipo, TF, dirección); piso `MIN_N=30` (pct null si menos). `detection_timeline` enriquece cada zona con `retest_pct` (carga el JSON una vez por proceso — re-correr script + reiniciar API para refrescar). **Hallazgo 2026-06-11 (N=6,156 zonas):** gradiente fuerte por TF — OB 5m bullish 86%, OB/FVG 15m 65–76%, 1h 50–65%, FVG 4h 38–48%, **FVG 1D solo 5–14%** (los gaps diarios casi nunca se retestean dentro de su vida útil).
- **Estilos de zona:** el botón Detections cicla Off → Boxes ▣ (rects rellenos) → Líneas ☰ (solo bordes superior/inferior finos, velas 100% legibles). Las zonas se **proyectan a la DERECHA del precio** (desde la barra as-of hasta el borde del canvas, offset derecho 140px) — no cubren las velas históricas. **OB pinta el BODY de la vela origen** (`body_high/body_low`), no wick-to-wick (la zona SMC válida es el cuerpo; el bot no cambia — entry sigue al 50% del body, SL wick-to-wick). FVG = el gap literal de 3 velas.
- **Cache del timeline (server, in-process):** el replay ~2.5s/TF está determinado por (símbolo, TF, última vela de la ventana) → se cachea en `_timeline_cache` (cap 32 FIFO, copias al leer para que el enriquecimiento no mute el cache). Medido: frío 7.0s → cacheado 0.011s. El frontend además **prefetchea** el timeline al cargar el chart (aunque Detections esté off) → el toggle es instantáneo. Se invalida solo cuando el TF imprime vela nueva.
- **Pendiente:** tuning de knobs (Focus/significancia OB), decisión de portar significancia FVG al detector `fvg.py`. (C3 gate de fidelidad ✅ `scripts/chart_c3_fidelity.py`; A7 mobile ✅ PR #73; 1W ✅ PR #74.)

```
HEADER: Status dot + "QF" + LIVE/DEMO pill + F&G pill (colored) + UTC clock (time only)
├── BTC/USDT panel (gradient bg, HTF bias badge) | ETH/USDT panel (gradient bg) | Risk gauges (arcos con glow)
├── Open Positions (rich cards: TP2/TP3/leverage/AI confidence/time open/cancel) | Equity curve
├── Trade Log (tabla, hover rows) | AI Decision Log (mini-cards con confidence ring)
├── Active Order Blocks (full width)
├── Estimated Liquidation Levels (full width, canvas heatmap, BTC/ETH tabs, 30s polling)
├── Whale Movements Log (full width)
└── System Health: Redis + PG + API status dots
```

## Estilo — Apple-inspired (black/white + glassmorphism)

- Fondo negro puro (`#000000` / `#0a0a0a`), cards con `backdrop-filter: blur(20px)` y fondo semitransparente (`rgba(255,255,255,0.04)`)
- Borders sutiles: `rgba(255,255,255,0.08)`, border-radius 12px en cards
- Gap entre cards: 8px (antes 1px), padding exterior 8px
- Verde para longs/positivo (#10b981), rojo para shorts/negativo (#ef4444)
- Azul accent (#3b82f6), amarillo warnings (#f59e0b)
- Font monospace (JetBrains Mono / system fallback)
- Números right-aligned, tabular-nums
- LIVE/DEMO: pill badge con borde coloreado (verde=live, ámbar=demo)
- Badges: border-radius 100px (pill shape)
- Hover effects: cards y table rows cambian a `rgba(255,255,255,0.06)`

## Trade Persistence (Prerrequisito)

Para que el dashboard muestre datos, el bot ahora escribe a PostgreSQL:

- **`data_store.py`** — Métodos: `insert_trade()`, `update_trade()`, `insert_ai_decision()`, `insert_risk_event()`
- **`monitor.py`** — Entry fill → `insert_trade()`, position close → `update_trade()`
- **`main.py`** — AI evaluation → `insert_ai_decision()`, risk rejection → `insert_risk_event()`
- **`risk_service/service.py`** — Guardrail hit → `insert_risk_event()`
- **Redis** — `qf:bot:positions` → JSON de posiciones abiertas actuales
- **Redis** — `qf:bot:whale_movements` → JSON de whale movements (TTL 600s, actualizado cada poll de Etherscan)
- **Redis** — `qf:bot:order_blocks` → JSON de OBs activos (TTL 600s, actualizado en cada candle confirmada)
- **Redis** — `qf:bot:htf_bias` → JSON de HTF bias por par (TTL 600s, actualizado en cada candle confirmada)
- **Redis** — `qf:bot:news:fear_greed` → JSON `{score, label}` (TTL 1800s, actualizado cada 5 min por NewsClient)

## Docker

```yaml
# docker-compose.yml (nuevos servicios)
api:
  build: {context: ., dockerfile: dashboard/api/Dockerfile}
  network_mode: host
  depends_on: [postgres, redis]

web:
  build: {context: ./dashboard/web, args: {NEXT_PUBLIC_API_URL: "http://192.168.1.238:8000"}}
  network_mode: host
  depends_on: [api]
```

### Acceso a la API desde el navegador (proxy same-origin)

El navegador NUNCA llama al puerto `:8000` directamente. `next.config.ts` define un
`rewrites()` que reenvía `/api/*` → `http://127.0.0.1:8000` (web corre en
`network_mode: host`, así que `127.0.0.1:8000` es la api). El cliente (`src/lib/api.ts`,
`getApiBase()`) usa origen relativo (`""`), por lo que todas las llamadas REST salen al
mismo origen `:3000`. Esto elimina la dependencia de que `:8000` sea alcanzable desde el
cliente — clave para acceso por Tailscale/SSH donde sólo `:3000` está expuesto — y evita
CORS por completo. **WebSocket** (`wsUrl()` / `getWsBase()`) SÍ apunta directo a
`ws://<hostname>:8000`: los rewrites de Next no proxean upgrades WS de forma fiable, así
que el ticker de precios en vivo requiere que `:8000` sea alcanzable. El build-arg
`NEXT_PUBLIC_API_URL` sólo afecta al render del servidor (SSR), no al cliente.

## Archivos

```
dashboard/
├── __init__.py
├── api/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, CORS, lifespan
│   ├── database.py      # asyncpg pool + redis.asyncio
│   ├── models.py        # Pydantic response schemas
│   ├── queries.py       # SQL queries centralizadas
│   ├── routes/
│   │   ├── health.py    # GET /api/health
│   │   ├── market.py    # GET /api/market/{pair}
│   │   ├── trades.py    # GET /api/trades
│   │   ├── ai.py        # GET /api/ai/decisions
│   │   ├── risk.py      # GET /api/risk
│   │   ├── candles.py   # GET /api/candles/{pair}/{tf}
│   │   ├── stats.py     # GET /api/stats
│   │   ├── whales.py    # GET /api/whales
│   │   ├── strategy.py     # GET /api/strategy/order-blocks, /api/strategy/htf-bias
│   │   ├── sentiment.py    # GET /api/sentiment
│   │   ├── liquidation.py  # GET /api/liquidation/heatmap/{pair}
│   │   ├── chart.py        # GET /api/chart/{config,symbols,search,history} (TradingView UDF Datafeed)
│   │   ├── shadow.py       # GET /api/shadow/{trades,stats} (ml_setups read-only, shadow-mode viewer)
│   │   └── manual_routes.py # Manual trading API + HTML page
│   ├── manual/
│   │   ├── calculator.py  # Position sizing math (linear + inverse), no external deps
│   │   ├── trade_manager.py # CRUD, partial closes, balance tracking (asyncpg)
│   │   ├── analytics.py   # Win rate, R multiples, TP hit rates, breakdowns
│   │   └── schema.sql     # CREATE TABLE for manual_trades, manual_partial_closes, manual_balances
│   ├── templates/
│   │   └── manual.html    # Standalone manual trading UI
│   ├── ws.py            # WS /api/ws
│   ├── requirements.txt
│   └── Dockerfile
└── web/
    ├── src/
    │   ├── app/          # Next.js app router (/ = bot dashboard, /manual = manual trading)
    │   ├── components/   # 13 bot components + 5 manual components (manual/ subdir)
    │   └── lib/          # API client, hooks, types
    ├── package.json
    ├── Dockerfile
    └── next.config.ts
```

## Responsive — Mobile/Tablet

CSS-first approach con 2 breakpoints en `globals.css`:

- **Tablet (≤1023px):** Grid 2 columnas, sidebar items (risk, equity, AI log) pasan a full-width
- **Mobile (≤639px):** Grid 1 columna, header wrap centrado, precios font reducido (28→22px), position cards 2×2, tablas scroll horizontal, columnas de baja prioridad ocultas (Type/P&L$/Exit en TradeLog, Significance/wallet addr en WhaleLog, Range/VolRatio en OrderBlockPanel), health grid wrap

Clases CSS añadidas a componentes para permitir override de inline styles via `!important`:
- `header-inner` (Header), `price-value` (PricePanel), `position-grid` (PositionCard), `health-inner` (HealthGrid)
- `col-type`, `col-pnl-usd`, `col-exit` (TradeLog), `col-sig`, `wallet-addr` (WhaleLog), `col-range`, `col-vol` (OrderBlockPanel)

## Cancel desde Dashboard

Mecanismo seguro y desacoplado:
1. Dashboard API escribe `qf:cancel_request:{pair}` en Redis con TTL 60s
2. El PositionMonitor del bot verifica cancel requests en cada poll cycle (antes de procesar cada posición)
3. Si encuentra uno, consume la key y ejecuta:
   - Pending entry → cancela orden de entrada
   - Active position → cancela SL/TPs + market close
4. Dashboard no habla directamente con OKX

**Redis key:** `qf:cancel_request:{pair}` (TTL 60s, consumida al leer)
**Backend:** `POST /api/trades/{pair}/cancel` → `queries.set_cancel_request()`
**Bot:** `monitor._check_cancel_request()` → `redis.pop_cancel_request()`

## PositionCard — Redesign

Cada posición muestra:
- Row 1: Pair + direction badge + setup type + phase + time open (e.g. "2h 14m")
- Row 2: P&L % (grande) + P&L USD estimado
- Row 3: 6-col grid — Entry, SL (rojo), TP1, TP2, TP3 (verde), Leverage
- Row 4: AI Confidence bar + botón Cancel (rojo, con diálogo de confirmación inline)

Mobile: 6-col grid → 3-col. Cancel button full width. Footer stacks.

## AILog — Redesign

- Mini-cards (no flat list items)
- Confidence ring SVG (círculo con porcentaje)
- Reasoning expandible (click para ver texto completo)
- Setup type badge visible
- Warnings como pills coloreados
- Empty state: "No AI evaluations yet — decisions appear when the bot detects a setup"

## Fear & Greed Pill

Pill en el Header que muestra el Fear & Greed Index en tiempo real:
- **Componente:** `FearGreedPill.tsx` — polls `GET /api/sentiment` cada 60s
- **API:** `sentiment.py` — lee `qf:bot:news:fear_greed` de Redis
- **Colores:** Rojo (0-25 Extreme Fear/Fear), naranja (26-45 Fear), gris (46-55 Neutral), verde-amarillo (56-75 Greed), verde (76-100 Extreme Greed)
- **Formato:** `F&G: 23` con tooltip completo ("Fear & Greed: 23/100 (Extreme Fear)")
- **Graceful:** Si no hay datos en Redis → no renderiza (returns null)
- **Mobile:** Pill compacto, no wrap

## Liquidation Heatmap

Estimated liquidation level chart — DIY approximation of Coinglass-style heatmap using OI + candle data.

**Backend:** `data_service/liquidation_estimator.py`
- Takes last 200 5m candles + current OI in USD
- Projects liquidation prices for 5 leverage tiers (5x/10x/25x/50x/100x) with industry-average weights (0.30/0.30/0.20/0.15/0.05)
- OI distributed across candles weighted by `volume_quote` (not uniform)
- Bins: $50 for BTC, $2 for ETH, $0.50 for SOL, $0.002 for DOGE (configurable via `LIQ_BIN_SIZE_*`)
- Result cached in Redis (`qf:liq_heatmap:{pair}`, TTL 30s via `LIQ_CACHE_TTL`)

**API:** `GET /api/liquidation/heatmap/{pair}` -> `LiqHeatmapResponse {pair, current_price, bins[]}`

**Frontend:** `LiquidationHeatmap.tsx`
- Canvas-based horizontal bar chart (no new dependencies)
- Y-axis: price, X-axis: estimated USD
- Long liquidations (red) extend left from center, short (green) extend right
- Dashed blue line for current price
- BTC/ETH/SOL/DOGE tab selector
- 30s polling via `usePolling`
- `devicePixelRatio` scaling for retina
- Mobile: 200px height (vs 300px desktop)

**Limitations vs Coinglass:** Assumed leverage distribution (not real), OKX only, candle close as entry proxy, snapshot only (no time dimension). Labeled "Estimated Liquidation Levels" to be transparent.

**Settings:** `LIQ_CANDLE_COUNT` (200), `LIQ_BIN_SIZE_BTC` (50), `LIQ_BIN_SIZE_ETH` (2), `LIQ_BIN_SIZE_SOL` (0.5), `LIQ_BIN_SIZE_DOGE` (0.002), `LIQ_CACHE_TTL` (30)

## Manual Trading Module

Módulo independiente para trades manuales — completamente separado del bot automático. No importa strategy/risk/execution services.

**Margin types:** Linear (USDT-margined, size en base asset) e Inverse (coin-margined, size en USD contracts). Calculator soporta ambos con PnL correcto.

**TP strategy:** 50/50 split automático. TP1 cierra 50% + mueve SL a breakeven. TP2 cierra el resto. Si no se proveen TPs, sugiere 1R y 2R automáticamente.

**Status flow:** `planned` → `active` (activated_at auto) → `closed` (closed_at auto, PnL auto-calc, balance auto-update)

**Partial closes:** Registra cierres parciales con porcentaje. Auto-cierra trade si total >= 100%. Auto-actualiza balance del par con PnL.

**Analytics:** Win rate, avg R multiple, total PnL, TP1/TP2 hit rates, breakdowns by pair/setup/direction, streak tracking.

**Tablas PostgreSQL:** `manual_trades`, `manual_partial_closes`, `manual_balances` — schema en `dashboard/api/manual/schema.sql`.

**Next.js dashboard (`/manual`):** Página dedicada con 5 componentes: ManualStats (balance, PnL, WR, streak), QuickCalculator (sizing + crear trade), ActiveTrades (cards con PnL live cada 10s, progreso TP1, botón close), TradeHistory (tabla expandible con thesis/mistakes/partials), ManualAnalytics (WR, avg R, profit factor, breakdown por par/dirección). Header con nav Bot/Manual.

**Validaciones:** Pair format regex en price endpoint (previene Redis key injection), leverage >= 1 (Pydantic Field).

## Bugs Conocidos (resueltos)

- **`queries.py` — `db.db.pg_pool`**: La función `get_trades()` usaba `db.db.pg_pool` en vez de `db.pg_pool`, causando `AttributeError` en cada request a `/api/trades`. Las demás queries (`get_trade_by_id`, `get_ai_decisions`, etc.) usaban `db.pg_pool` correctamente. Corregido.

## Limitaciones v1

- Sin charting library (TradingView, etc.) — sparklines SVG
- Sin modificación de SL/TP desde el dashboard — solo cancel completo
- Sin autenticación — localhost detrás del router
- Sin backtesting UI o alertas en el dashboard (notificaciones push via Telegram — `shared/notifier.py`)
