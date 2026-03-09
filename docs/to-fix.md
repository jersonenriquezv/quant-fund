# Issues Pendientes — To Fix

Backlog generado por auditoría completa de los 5 layers (2026-03-04).
12 CRITICAL corregidos en el mismo commit. Estos IMPORTANT y MINOR quedan para v2.

---

## Layer 1: Data Service

### IMPORTANT

- **I-D1** `data_service/exchange_client.py:133-157` — Backfill pagination puede producir candles duplicados u out-of-order. El cálculo de `since` asume spacing exacto; gaps causan fetches solapados. No hay deduplication en `all_ohlcv` antes de conversión.
- **I-D2** `data_service/exchange_client.py:187` — `volume_quote` en backfill usa `vol * close_price` (aproximación). WebSocket lee el valor real de `candle_data[7]`. Semántica inconsistente entre candles backfilled y live.
- **I-D4** `data_service/cvd_calculator.py:257-268` — CVD `buy_volume`/`sell_volume` solo computados para ventana 1h. `CVDSnapshot` no documenta a qué ventana corresponden. Downstream (AI prompt) puede asumir que matchean 5m/15m CVD values.
- **I-D5** `data_service/oi_liquidation_proxy.py:118-137` — OI proxy toma snapshot más cercano sin validar edad. Al startup con Redis data stale, puede comparar snapshots con 1h de diferencia en vez de 5min, causando falsos cascades.
- **I-D6** `data_service/etherscan_client.py:193-202` — Deduplication de transacciones puede fallar. Usa un solo `last_seen` hash marker. Si 21+ txs nuevas llegan entre polls, el marker nunca se encuentra y todas 20 se tratan como nuevas. Mismo issue en `btc_whale_client.py:158-167`.
- **I-D7** `data_service/etherscan_client.py:240` — No hay deduplication en lista de whale movements. `_movements.append()` sin check de tx_hash existente. `WhaleMovement` no tiene campo `tx_hash`. Mismo issue en `btc_whale_client.py:226`.
- **I-D8** `data_service/service.py:287-303` — `_fetch_initial_indicators` bloquea el event loop. Llamadas ccxt REST sync (4-6 HTTP requests) durante `start()`. Backfill usa `run_in_executor` correctamente, pero este método no.
- **I-D9** `data_service/data_store.py:76-84` — Redis `is_connected` property hace una llamada de red bloqueante. `self._client.ping()` es sync, llamado cada 30s desde health check + dashboard API.

### MINOR

- **M-D1** `config/settings.py:184` — Funding rate polling interval (28800s/8h) puede ser demasiado largo. Polling cada 1-4h daría datos más oportunos al AI durante períodos volátiles.
- **M-D3** `data_service/exchange_client.py:~90`, `websocket_feeds.py:~320` — Validación de candle no verifica `high >= low`. Un candle malformado pasaría validación y causaría resultados nonsensical.
- **M-D4** `data_service/service.py:125-139` — Sin detección de staleness para market data. `get_market_snapshot()` ensambla datos sin verificar edad. OI o funding stale se pasan al AI sin warning.
- **M-D5** `data_service/websocket_feeds.py:112-120` — `store_candles` deduplication itera TODAS las keys en cada llamada, no solo las que recibieron datos nuevos.
- **M-D7** `data_service/btc_whale_client.py:42-44,181-195` — BTC whale client hace lowercase de direcciones base58. Para bech32 (`bc1...`) es correcto, pero para legacy (`1...`, `3...`), `.lower()` corrompe el display. Logs/dashboard muestran case incorrecto.

---

## Layer 2: Strategy Service

### IMPORTANT

- **I-S1** `strategy_service/liquidity.py:219-228` — Algoritmo de clustering tiene chain drift. Compara contra `current_cluster[-1]` en vez de centroide. Una cadena de swings 100.0, 100.05, 100.10, 100.15 termina en un solo cluster aunque primero y último estén 0.15% apart.
- **I-S2** `strategy_service/setups.py:200-326` — Setup B no tiene check de recencia temporal para BOS. Un BOS stale de muchas candles atrás combinado con OB y FVG posterior podría triggear. No hay validación de que BOS esté dentro de N candles del candle actual.
- **I-S3** `strategy_service/setups.py:244-249` — Lógica de selección de OB en Setup B es buggy. Una vez que `best_ob` se setea, cada OB subsiguiente near price lo reemplaza sin métrica "mejor". Primer OB aceptado sin verificar price proximity.
- **I-S4** `config/settings.py:127` — `EQUAL_LEVEL_TOLERANCE_PCT = 0.0005` (0.05%) demasiado ajustado para BTC. A $60K, tolerancia = $30. Swing highs a $60,000 y $60,031 serían clusters separados. Debería ser 0.001-0.002.
- **I-S5** `config/settings.py:144` — `OB_PROXIMITY_PCT = 0.003` (0.3%) demasiado ajustado para BTC. A $60K, margin = $180. Candles de 15m se mueven $200-500 rutinariamente. Debería ser 0.005-0.008.
- **I-S6** `strategy_service/liquidity.py:300-339` — Sweeps solo se registran una vez por level. Después del temporal guard fix, un level swept no puede ser re-testeado. Double sweeps son un patrón SMC válido que se perdería.
- **I-S7** `tests/test_setups.py` — Sin test coverage para lado short en Setup A o B. Todos los tests solo cubren dirección long/bullish. Bugs direccionales son comunes en trading bots.
- **I-S8** Todos los detectors de strategy — Sin validación de que candles estén sorted cronológicamente. Todos aceptan `candles: list[Candle]` y asumen oldest-first. Orden incorrecto produce resultados silenciosamente incorrectos.

### MINOR

- **M-S1** `strategy_service/market_structure.py:286` — `_determine_trend` usa solo el último break. En mercados choppy, trend flip en cada break. Más robusto: considerar balance de breaks recientes o requerir HH/HL confirmation.
- **M-S3** `strategy_service/setups.py:419` — `_check_volume_confirmation` retorna `confirmed` que nunca se usa. Dead return value. CLAUDE.md dice que volume spike es required (step 8), pero código lo trata como optional confluence.
- **M-S4** `strategy_service/setups.py:160` — TradeSetup timestamp usa wall-clock time en vez de candle timestamp. `int(time.time() * 1000)` hace tests no-determinísticos y complica replay.
- **M-S5** `strategy_service/market_structure.py:131,146` — Swing detection: equal high/low con neighbors causa exclusión estricta. `>=` significa que si dos candles tienen mismo high, ninguno califica.
- **M-S6** `tests/` — Sin integration test de `StrategyService.evaluate()` end-to-end. Solo tests unitarios individuales. No hay test que verifique pipeline completo candle → TradeSetup por el facade.
- **M-S7** `strategy_service/setups.py:384-388` — Volume confirmation threshold Setup A no matchea spec. CLAUDE.md requiere "Volume spike >2x + liquidations visible" como hard requirement, código lo trata como optional.
- **M-S8** `strategy_service/order_blocks.py:185` — `_compute_avg_volume` incluye el OB candle en el promedio. El volumen del OB está en la ventana, inflando el denominador y bajando el ratio del OB. Bias menor.

---

## Layer 3: AI Service

### IMPORTANT

- **I-A3** `ai_service/prompt_builder.py:130-139` — Sección OI muestra solo snapshot point-in-time, no trend. System prompt dice evaluar "OI rising + price rising" pero solo provee un número sin valor previo para comparación. Limitación documentada en prompt.

### MINOR

- **M-A2** `ai_service/prompt_builder.py:141-157` — Sección CVD podría incluir flag pre-computado de divergencia price-CVD. Números raw pasados, pero system prompt instruye a Claude a checkear divergencia.
- **M-A3** `ai_service/service.py:116` — Price context solo usa últimas 2 candles de 10 fetched. Multi-period trend daría más contexto a Claude.

---

## Layer 4: Risk Service

### IMPORTANT

- **I-R1** `risk_service/state_tracker.py:64-68` — Position close matchea solo por `pair`, no por `(pair, direction)`. Si dos posiciones existen en mismo par, `record_trade_closed` remueve el primer match sin importar dirección.

### MINOR

- **M-R1** `risk_service/state_tracker.py:123-136` — Comparación `tm_yday` tiene edge case teórico en boundary de año. Usar `date()` comparison en vez de `tm_yday`.
- **M-R2** `risk_service/state_tracker.py:76-83` — PnL fraction summation es aproximación cuando capital cambia mid-day. Error máximo con configuración actual: ~0.2%. Negligible pero técnicamente impreciso.
- **M-R3** `risk_service/service.py:129-136` — Fire-and-forget risk event persistence sin failure counter. Errores en `_persist_risk_event` se loguean pero no se trackea con counter/métrica.
- **M-R4** `tests/test_risk_service.py` — Sin test para path `ValueError` en position sizer alcanzado vía `check()`. Actualmente imposible por orden de guardrails.
- **M-R5** `tests/test_claude_client.py` — Sin test para campo `approved` retornado como string `"true"` en vez de boolean. Claude podría retornarlo; validación lo catchea pero falta test.

---

## Layer 5: Execution Service

### IMPORTANT

- **I-E1** `execution_service/monitor.py:284-306` — Ventana de doble SL durante adjustment. Nuevo SL se coloca antes de cancelar viejo. Si viejo SL triggea entremedio, ambos podrían fill resultando en double close. `reduceOnly` previene full double close, pero partial fills crean riesgo.
- **I-E3** `execution_service/monitor.py:354-361` — Cálculo de PnL solo refleja la porción de exit, no partial TP fills. Después de TP1 hit + SL breakeven, PnL reportado es 0% en vez del blended positivo real. Afecta Risk Service drawdown y dashboard.
- **I-E7** `execution_service/monitor.py:400,430` — Acceso frágil a `_data_store.postgres`. Si `.postgres` es `None`, lanza `AttributeError` en vez de error limpio.

### MINOR

- **M-E1** `execution_service/monitor.py:48` — Posición keyed por pair limita a una posición por pair. Acceptable con 2 pares y max 3 posiciones, pero previene setups simultáneos en diferentes timeframes para mismo par.
- **M-E3** `execution_service/monitor.py:344-352` — Slippage se loguea pero no se persiste. No guardado en PostgreSQL ni Redis. Trackear slippage promedio ayudaría a optimizar entry strategy.
- **M-E4** `tests/test_execution.py` — Sin test para partial entry fill, TP3 fill, o concurrent positions.
- **M-E5** `tests/test_execution.py` — Sin test para path de falla de `_adjust_sl`. Cuando nuevo SL placement falla, viejo SL se mantiene (correcto), pero no está testeado.

---

## Resueltos (historical)

- ~~I-E4~~ — Validación de precios SL/TP vs entry. Resuelto: `execute()` valida `sl < entry < tp2` (long) y `sl > entry > tp2` (short). Además, SL vs market validation previene OKX 51053.
- ~~I-E2~~ — TP failure non-fatal. BY DESIGN: SL protege downside, TP es opcional. No emergency close en TP failure.
- ~~I-E8~~ — Telegram fire-and-forget. Resuelto: `_safe_notify()` wrapper con `add_done_callback()` para error logging.
- ~~M-E2~~ — `fetch_position` no usado. Resuelto: ahora usado en startup sync, adopted position monitoring, y SL vanished fallback.
- ~~I-A1~~ — Code fence stripping frágil. Resuelto: regex extraction de JSON block, maneja preamble text y fallback a primer `{`.
- ~~I-A2~~ — Prompt sin R:R ratio. Resuelto: prompt ahora incluye R:R por TP level + blended R:R.
- ~~M-A1~~ — Sin guard para response.content vacío. Resuelto: check explícito antes de acceder `[0].text`.
- ~~I-D3~~ — Serialización asimétrica ETH vs BTC. Resuelto: ambos retornan `list[dict]`.
- ~~M-D2~~ — `_timeframe_to_ms` default silencioso. Resuelto: `ValueError` en timeframe desconocido.
- ~~M-S2~~ — FVG dedup colisión. Resuelto: key ahora es `(timestamp, direction)`.
- ~~M-D6~~ — OI proxy lista unbounded. Resuelto: `_prune_old_events()` en `get_recent_liquidations()`.
- ~~M-D8~~ — Whale notification index stability. Resuelto: snapshot de `id()` antes de polling.
- ~~Backfill 4h/1h falla en OKX sandbox~~ — Resuelto: `_TIMEFRAME_MAP` convertía "1h"→"1H" y "4h"→"4H", bypassing ccxt's internal mapping. OKX sandbox no soporta ese formato. Fix: pasar timeframe directamente a ccxt (lowercase), que mapea a OKX internamente.
- ~~Whale wallets vacías~~ — Resuelto: Agregadas 6 direcciones públicas de whales en `config/settings.py`.
- ~~Etherscan "no API key" a pesar de estar en .env~~ — Resuelto: El check requería AMBOS API key Y wallets. Al agregar wallets, Etherscan polling arranca normalmente.
- ~~PostgreSQL password authentication failed~~ — Resuelto: `$` en password se interpretaba como variable de shell en Docker Compose. Cambiado a password sin caracteres especiales.
- ~~12 CRITICAL issues~~ — Resueltos en commit de auditoría (2026-03-04): duplicate wallet addresses, PG reconnection, pipeline serialization, deprecated asyncio calls, OKX algo order params, emergency close retry, cancelled trade counting, sweep temporal guard, OB break_timestamp, Setup A documentation, whale movement notification race condition.
