# Changelog — One-Man Quant Fund

## [2026-03-06] — Claude Code tooling cleanup
**Qué cambió:**
- `.claude/commands/doc-update.md` — Ahora scoped a `git diff` (antes leía todos los servicios ciegamente)
- `.claude/commands/test.md` — Conciso: solo pass/fail + assertions
- `.claude/commands/status.md` — Separado de `/test` (ya no corre pytest)
- `.claude/commands/review.md` — Nuevo comando: corre checklist de @reviewer sobre cambios uncommitted
- `.claude/settings.local.json` — Limpiado de 85 líneas de comandos one-off a 21 patrones reusables. Credenciales eliminadas.
- `.claude/agents/` — 5 agentes obsoletos eliminados (architect, data-engineer, documenter, risk-guard, smc-engine). Solo quedan 4: coder, planner, reviewer, debugger.

**Por qué:** Optimización de tokens — comandos vagos forzaban a Claude a explorar todo el proyecto. Settings tenía credenciales en texto plano (GitHub PAT, Etherscan key). Agentes duplicados no se usaban.

## [2026-03-06] — Prompt refinement: R:R, labeled confluences, HTF-aware system prompt
**Qué cambió:**
- `ai_service/prompt_builder.py` — System prompt reescrito: HTF alignment ya no es factor de evaluación (pre-filter lo garantiza), OI marcado como snapshot-only, "30-60%" eliminado (sin cuota), reasoning ahora "2-4 sentences, decisive factor first". Setup section ahora incluye R:R computado (por TP y blended). Confluences ahora son human-readable con tags [SUPPORTING]/[CONTEXT]. Nuevo método `_format_confluences()`.
- `tests/test_prompt_builder.py` — Test actualizado para reflejar eliminación de "30-60%".
- `docs/context/03-ai-filter.md` — Actualizado con cambios al prompt.

**Por qué:** Claude rechazaba 100% de setups citando "HTF conflict" — pero el pre-filter ya elimina esos antes de llegar a Claude. El prompt tenía info que Claude no podía usar (OI trend sin datos de tendencia). Confluences eran labels internos que Claude no podía interpretar. Sin R:R explícito, Claude tenía que hacer aritmética mental.

## [2026-03-06] — Profile cleanup, AI bypass removed, sandbox slippage fix
**Qué cambió:**
- `config/settings.py` — Scalping profile eliminado. Aggressive rediseñado: PD alignment ON, HTF alignment ON (1H suficiente), AI siempre obligatorio, DD 5%/10% (era 20%/40%), R:R 1.2 (era 1.0), 10 trades/día (era 20). `FORCE_MAX_LEVERAGE` eliminado. Nuevo setting `SANDBOX_LIMIT_TOLERANCE_PCT = 0.0005`.
- `main.py` — Todo AI bypass eliminado (`_persist_ai_skip`, scalping/aggressive auto-approve). Pipeline simplificado: setup → dedup → pre-filter (HTF bias + funding + CVD) → Claude → risk → execute. Setup dedup cache (15min TTL) evita re-evaluar mismo setup.
- `execution_service/executor.py` — Nuevo `fetch_ticker()` para obtener ask/bid actual.
- `execution_service/service.py` — Sandbox usa limit orders a ask/bid + 0.05% tolerancia (era market orders con 13.8% slippage).
- `risk_service/position_sizer.py` — `FORCE_MAX_LEVERAGE` branch eliminado. Risk-based sizing siempre.
- `ai_service/service.py` — Eliminada lógica de LTF candles para scalping.
- `ai_service/prompt_builder.py` — Eliminada sección de perfil scalping y anotación HTF informational.
- `shared/notifier.py` — `notify_ai_skipped()` eliminado.
- `dashboard/api/routes/profile.py` — Scalping removido de perfiles disponibles.
- `.claude/agents/` — 6 agentes domain-based reemplazados por 4 role-based: @coder, @debugger, @planner, @reviewer. Reducción de ~19K a ~3.2K tokens (83%).

**Por qué:** Los profiles aggressive y scalping desactivaban protecciones core de SMC (PD alignment, HTF alignment) y bypaseaban Claude, resultando en trades de baja calidad. Market orders en sandbox causaban slippage inaceptable. Los agentes tenían responsabilidades por dominio con mucha info duplicada de CLAUDE.md.
**Impacto:** config/, main.py, execution_service/, risk_service/, ai_service/, shared/, dashboard/, .claude/agents/, tests/
**Tests:** 299 passed, 0 failed.

---

## [2026-03-05] — Whale notification enrichment + 4H OB summary
**Qué cambió:**
- `shared/models.py` — `WhaleMovement` dataclass: nuevo campo `wallet_label: str = ""` para nombre legible de la wallet (e.g., "Vitalik Buterin", "Galaxy Digital").
- `data_service/etherscan_client.py` — `_process_transaction()` ahora pasa `wallet_label=label` a los 4 constructores de WhaleMovement (deposit, withdrawal, transfer_out, transfer_in). Label viene de `WHALE_WALLETS` dict (address → name).
- `data_service/btc_whale_client.py` — Misma lógica: `label = self._whale_wallets.get(wallet, "")` y `wallet_label=label` en los 5 constructores de WhaleMovement.
- `shared/notifier.py` — `notify_whale_movement()` ahora muestra wallet name en bold (o dirección truncada como fallback). Nuevo `notify_ob_summary()` para resumen de Order Blocks activos cuando cierra la vela 4H.
- `main.py` — En `on_candle_confirmed()`, cuando `candle.timeframe == "4h"`, recolecta OBs activos y envía resumen via Telegram.

**Por qué:** Las notificaciones de whale solo mostraban el monto y la acción, sin identificar quién. Ahora muestran el nombre de la wallet. El resumen de OBs en 4H permite monitorear qué zonas está trackeando el bot sin revisar logs.
**Impacto:** shared/models.py, data_service/, shared/notifier.py, main.py
**Tests:** 303/303 passing (sin nuevos tests — cambios en notificaciones y labels).

---

## [2026-03-05] — Aggressive profile overhaul + FORCE_MAX_LEVERAGE + PD alignment optional
**Qué cambió:**
- `config/settings.py` — 2 nuevos settings: `FORCE_MAX_LEVERAGE: bool = False` (modo sizing fijo), `REQUIRE_PD_ALIGNMENT: bool = True` (validación premium/discount). Perfil `aggressive` expandido masivamente: `REQUIRE_PD_ALIGNMENT: False`, `FORCE_MAX_LEVERAGE: True`, `MAX_DAILY_DRAWDOWN: 0.20` (20%), `MAX_WEEKLY_DRAWDOWN: 0.40` (40%), `COOLDOWN_MINUTES: 10`, `MAX_TRADES_PER_DAY: 20`, `AI_MIN_CONFIDENCE: 0.50`, `OB_PROXIMITY_PCT: 0.008`, `OB_MIN_VOLUME_RATIO: 1.0`, `SWEEP_MIN_VOLUME_RATIO: 1.2`, `MIN_RISK_REWARD: 1.0`, `OB_MAX_AGE_HOURS: 72`, `FVG_MAX_AGE_HOURS: 72`.
- `risk_service/position_sizer.py` — Nuevo modo `FORCE_MAX_LEVERAGE`: `position_size = capital * MAX_LEVERAGE / entry_price` (ignora risk-based sizing, usa capital completo con max leverage).
- `strategy_service/setups.py` — `_check_pd_alignment()` respeta `REQUIRE_PD_ALIGNMENT` — retorna True siempre si desactivado.
- `docker-compose.yml` — `STRATEGY_PROFILE: aggressive` en env del bot service.
- `tests/test_execution.py` — 4 tests nuevos: `TestAlgoOrderFetch` (pending, filled, cancelled, error throttling).
- `tests/test_position_sizer.py` — 3 tests nuevos: `TestForceMaxLeverage` (BTC, ETH, ignores SL distance).

**Por qué:** El bot no entraba en ningún trade con el perfil default/aggressive anterior. Investigación reveló que 40% de setups se rechazaban por PD misalignment (bloqueador #1) y el risk-based sizing generaba posiciones de $2 en vez de $100. El perfil aggressive ahora permite explorar la estrategia con capital completo a 5x leverage.
**Impacto:** config/, risk_service/, strategy_service/, execution_service/, docker-compose.yml, tests/
**Tests:** 303/303 passing (7 nuevos).

---

## [2026-03-05] — Algo order fetch rewrite (OKX native API)
**Qué cambió:**
- `execution_service/executor.py` — `_fetch_algo_order()` completamente reescrito. Antes usaba `fetch_open_orders` y `fetch_canceled_and_closed_orders` con `{"ordType": "conditional"}`, que internamente llamaba a `fetchCanceledAndClosedOrders()` — método no soportado por ccxt 4.5.40 para OKX. Causaba ~6,871 errores repetidos en logs. Ahora usa OKX native API methods: `privateGetTradeOrdersAlgoPending` (paso 1: busca en pending), `privateGetTradeOrdersAlgoHistory` con `state: "effective"` (paso 2: busca triggered/filled), y con `state: "canceled"` (paso 3: busca cancelados). Nuevo `self._algo_fetch_errors: dict[str, int]` para throttling de errores (logea solo el primero y cada 12vo).

**Por qué:** ccxt v4.5.40 `fetch_open_orders` con `{"ordType": "conditional"}` se redirigía internamente a `fetchCanceledAndClosedOrders()` que no está implementado para OKX, generando miles de errores por hora sin resultado útil.
**Impacto:** execution_service/executor.py, tests/test_execution.py
**Tests:** 303/303 passing (4 nuevos en TestAlgoOrderFetch).

---

## [2026-03-05] — Fix: SL placement failing on OKX (error 50015)
**Qué cambió:**
- `execution_service/executor.py` — `place_stop_market()` params changed from `{"triggerPrice": x, "ordType": "conditional"}` to `{"stopLossPrice": x}`. The old params caused OKX to reject every SL order with error 50015 ("Either parameter tpTriggerPx or slTriggerPx is required"), triggering EMERGENCY CLOSE on every trade. The ccxt unified `stopLossPrice` param correctly maps to OKX's `slTriggerPx` internally.
- `docs/context/05-execution.md` — Updated algo order handling section and order types table to reflect the fix.

**Por qué:** Every trade the bot executed was immediately emergency-closed because the SL order was rejected by OKX. The bot was placing entries but could never protect them with a stop-loss. Root cause: ccxt 4.5.40 on OKX requires `stopLossPrice` (unified API), not `triggerPrice` + `ordType` (which was a guess at OKX-native params that ccxt didn't map correctly).
**Impacto:** execution_service/executor.py, docs/context/
**Tests:** 25/25 passing (no test changes needed — tests mock ccxt).

---

## [2026-03-05] — Docs sync: test counts, pipeline description, dates
**Qué cambió:**
- `docs/context/00-architecture.md` — Test counts actualizados (291→296). Pipeline steps 5-7 describen pre-filter + hybrid scalping. Cambios recientes actualizado. Notificaciones Telegram incluyen pre-filter.
- `docs/context/01-data-service.md` — main.py description menciona pre-filter y hybrid scalping.
- `docs/context/03-ai-filter.md` — Fecha actualizada, test counts corregidos (35→41 con desglose por archivo).
- `docs/context/04-risk.md` — Test counts corregidos (73→72 con desglose real por archivo: 13+23+23+13).
- `docs/context/05-execution.md` — Test count corregido (28→25).

**Por qué:** Los conteos de tests estaban desactualizados después de múltiples adiciones. Los docs no reflejaban el pre-filter y hybrid scalping.
**Impacto:** docs/context/

---

## [2026-03-05] — AI Filter: Hybrid Scalping Mode + Pre-Filter
**Qué cambió:**
- `main.py` — Scalping profile ahora es híbrido: HTF-aligned scalps pasan por Claude (via `_evaluate_with_claude()`), pure LTF scalps bypasean Claude (como antes). Nuevo `_pre_filter_for_claude()` que rechaza setups obvios antes de llamar a Claude (todas las profiles): funding extreme contra dirección y CVD divergencia fuerte. Nuevo `_persist_ai_pre_filter()` para audit trail. `_evaluate_with_claude()` centraliza el flujo pre-filter → Claude → persist → notify.
- `shared/notifier.py` — Nuevo `notify_ai_pre_filtered()` para notificación Telegram de rechazos pre-filter.
- `docs/context/03-ai-filter.md` — Sección "Pre-Filter" nueva, sección "Scalping Profile" reescrita para modo híbrido.

**Por qué:** El bypass puro de scalping descartaba HTF-aligned scalps que se benefician del análisis macro de Claude. Además, todas las profiles gastaban tokens en setups que Claude rechazaría obviamente (funding extreme, CVD divergence). El pre-filter ahorra tokens; el modo híbrido preserva la calidad en scalps de mayor probabilidad.
**Impacto:** main.py, shared/notifier.py, docs/context/

---

## [2026-03-05] — AI Filter: Bypass Claude for scalping profile
**Qué cambió:**
- `main.py` — When `STRATEGY_PROFILE == "scalping"`, pipeline skips AI evaluation entirely. Logs "AI SKIPPED", persists synthetic AI decision to PostgreSQL (audit trail), sends Telegram notification. New `_persist_ai_skip()` helper.
- `shared/notifier.py` — New `notify_ai_skipped()` method for Telegram skip notification.
- `docs/context/03-ai-filter.md` — Scalping profile section rewritten: Claude bypassed, not softened.

**Por qué:** Claude evaluates macro context (1h+ CVD, funding, OI) irrelevant for 5-30 minute scalps. Previous approach (softening Claude's prompt) still caused rejections for valid scalping setups. Institutional HFT desks use deterministic rules without AI in the hot path — scalping profile now does the same. Risk Service guardrails (R:R, DD, position limits, cooldown) remain fully enforced.
**Impacto:** main.py, shared/notifier.py, docs/context/

---

## [2026-03-05] — AI Filter: Profile-aware evaluation (scalping-friendly)
**Qué cambió:**
- `ai_service/prompt_builder.py` — New `_build_profile_section()`: when scalping profile is active, prepends a section telling Claude that HTF bias is "INFORMATIONAL ONLY". HTF Bias line annotated with "(informational — scalping profile does not require alignment)". No changes for default/aggressive profiles.
- `ai_service/service.py` — `_get_candles_context()` includes 5m/15m timeframes when scalping profile is active, giving Claude LTF momentum data.
- `config/settings.py` — `STRATEGY_PROFILES["scalping"]` gets `AI_MIN_CONFIDENCE: 0.50` (down from 0.60).
- `tests/test_prompt_builder.py` — 3 new tests: scalping section included, default has no scalping section, HTF bias annotated.
- `tests/test_ai_service.py` — 2 new tests: scalping approves at 0.50, default rejects at 0.50.
- `docs/context/03-ai-filter.md` — New "Profile-Aware Evaluation" section.

**Por qué:** Scalping profile relaxes HTF alignment in Strategy Service, but Claude didn't know about it — system prompt treated HTF conflict as a dealbreaker, causing 100% rejection of scalping shorts. Fix injects profile context in the user prompt (not system prompt, which is cached at init).
**Impacto:** ai_service/, config/, tests/, docs/context/

---

## [2026-03-05] — AI Decisions: Add pair/direction/setup_type/approved to persistence
**Qué cambió:**
- `data_service/data_store.py` — `ai_decisions` table: 4 new columns (`pair VARCHAR(20)`, `direction VARCHAR(5)`, `setup_type VARCHAR(10)`, `approved BOOLEAN`). `ALTER TABLE ADD COLUMN IF NOT EXISTS` migration for existing DBs. `insert_ai_decision()` accepts and inserts new fields.
- `main.py` — `_persist_ai_decision()` passes `setup.pair`, `setup.direction`, `setup.setup_type`, `decision.approved` to insert.
- `dashboard/api/models.py` — `AIDecisionRecord`: 4 new optional fields.
- `dashboard/api/routes/ai.py` — Maps new fields from query rows.
- `dashboard/web/src/lib/api.ts` — `AIDecision` interface: 4 new fields.
- `dashboard/web/src/components/AILog.tsx` — Shows pair, direction badge (LONG/SHORT), and APPROVED/REJECTED badge per decision.
- `docs/context/03-ai-filter.md` — New "Persistence" section documenting what gets saved.

**Por qué:** AI decisions were saved to PostgreSQL but missing crucial context (which pair? which direction? approved or rejected?). Without these, the dashboard AILog and historical analysis couldn't distinguish decisions.
**Impacto:** data_service/, main.py, dashboard/, docs/context/

---

## [2026-03-04] — AI Service: Token usage logging
**Qué cambió:**
- `ai_service/claude_client.py` — Loguea `response.usage` (input_tokens, output_tokens, total) después de cada llamada exitosa a Claude. Permite rastrear consumo real de tokens por evaluación.
- `docs/context/03-ai-filter.md` — Actualizado: documenta token logging, corrige estimación de costo (~1,100 input + ~200 output ≈ $0.006/eval)

---

## [2026-03-04] — Dashboard: Order Block Panel + HTF Bias Indicator
**Qué cambió:**

**Strategy Service (`strategy_service/service.py`):**
- Nuevo `_cached_htf_bias` dict — cachea HTF bias en cada `evaluate()` (antes solo existía en scope local)
- `get_active_order_blocks(pair)` — devuelve OBs activos de todos los LTF timeframes
- `get_htf_bias(pair)` — devuelve bias cacheado ("bullish"/"bearish"/"undefined")

**Bot (`main.py`):**
- `_publish_strategy_state(pair)` — serializa OBs + bias a Redis después de cada `evaluate()`, ANTES del `if setup is None: return` (publica siempre, no solo cuando hay setup)
- Redis keys: `qf:bot:order_blocks` (JSON array, TTL 600s), `qf:bot:htf_bias` (JSON dict, TTL 600s)

**Dashboard API:**
- `dashboard/api/routes/strategy.py` — 2 endpoints: `GET /api/strategy/order-blocks`, `GET /api/strategy/htf-bias`
- `dashboard/api/models.py` — `OrderBlockRecord`, `HTFBiasResponse`
- `dashboard/api/main.py` — strategy router registrado

**Dashboard Frontend:**
- `OrderBlockPanel.tsx` — Tabla de OBs activos con distance% desde precio live (WS), sorted por cercanía. Highlighting amarillo para distance <0.5% y vol ratio ≥2x. Mobile: oculta Range y Vol Ratio
- `PricePanel.tsx` — Badge de HTF bias (BULLISH verde/BEARISH rojo/UNDEFINED gris) junto al nombre del par
- `api.ts` — `OrderBlockData`, `HTFBiasResponse` interfaces
- `page.tsx` — OrderBlockPanel entre trade/AI row y whale row
- `globals.css` — `.ob-panel { grid-column: 1/-1 }`, grid-template-rows actualizado, `.col-range`/`.col-vol` ocultos en mobile

---

## [2026-03-04] — Strategy Profiles + Backtester + Dashboard Profile Switcher
**Qué cambió:**

**Backtester (`scripts/backtest.py`):**
- Nuevo script que replays candles históricos (desde PostgreSQL) a través de StrategyService
- Mock DataService con cursor temporal, SimulatedClock (patcha `time.time()`), RejectTracker (categoriza rechazos via loguru sink)
- Flags: `--profile default|aggressive|scalping`, `--verbose`, `--pair`, `--warmup`
- Resultados en 5.4 días de datos: default=6 setups (~1.1/día), aggressive=15 (~2.8/día), scalping=143 (~26.4/día)

**Strategy Profiles (`config/settings.py`):**
- 3 perfiles definidos en `STRATEGY_PROFILES`: default, aggressive, scalping
- `apply_profile()` / `reset_profile()` para aplicar/resetear overrides
- Se aplican al startup via env var `STRATEGY_PROFILE` o en runtime via Redis (`qf:bot:strategy_profile`)
- 3 nuevos settings configurables (antes hardcoded):
  - `REQUIRE_HTF_LTF_ALIGNMENT` — LTF debe alinear con HTF (default: True, scalping: False)
  - `ALLOW_EQUILIBRIUM_TRADES` — permitir trades en zona equilibrium (default: False, scalping: True)
  - `HTF_BIAS_REQUIRE_4H` — 4H debe definir trend (default: True, scalping: False)

**Strategy code changes:**
- `strategy_service/setups.py` — 3 checks hardcoded ahora respetan settings (CHoCH vs HTF, BOS vs HTF, equilibrium zone)
- `strategy_service/service.py` — HTF bias logic respeta `HTF_BIAS_REQUIRE_4H`

**Bot (`main.py`):**
- `_sync_profile_from_redis()` — lee perfil de Redis al inicio de cada pipeline cycle, aplica hot-switch sin restart

**Dashboard API:**
- `dashboard/api/routes/profile.py` — GET/POST `/api/profile` (lee/escribe Redis)
- `dashboard/api/main.py` — CORS actualizado a GET+POST, ruta profile montada

**Dashboard frontend:**
- `ProfileSelector.tsx` — dropdown con color-coded dot (verde/amarillo/rojo), warning badge pulsante cuando no está en default
- `Header.tsx` — acepta children, ProfileSelector integrado junto a DEMO banner
- `api.ts` — `postApi()` helper, tipos `ProfileResponse`/`ProfileInfo`
- `globals.css` — estilos profile selector (responsive incluido)

**Por qué:** El bot corría horas sin trades. El backtester demostró que la estrategia sí produce setups (~1/día con default), pero el mercado estaba en condiciones que no alineaban HTF con LTF. Los perfiles permiten experimentar con diferentes niveles de agresividad durante paper trading sin riesgo.
**Impacto:** config/, strategy_service/, scripts/, dashboard/, main.py, docs/context/
**Tests:** 291 passing (sin cambios en tests existentes).

## [2026-03-04] — Dashboard — Mobile Responsiveness
**Qué cambió:**
- `dashboard/web/src/app/globals.css` — 2 breakpoints: tablet (≤1023px, grid 2 columnas) y mobile (≤639px, grid 1 columna). Columnas low-priority ocultas en mobile, tablas con scroll horizontal, cards con padding reducido.
- `dashboard/web/src/components/Header.tsx` — Clase `header-inner` para flex-wrap en mobile.
- `dashboard/web/src/components/PricePanel.tsx` — Clase `price-value` para reducir font de 28→22px en mobile.
- `dashboard/web/src/components/PositionCard.tsx` — Clase `position-grid` para layout 2×2 en mobile (antes 4 columnas).
- `dashboard/web/src/components/TradeLog.tsx` — Clases `col-type`, `col-pnl-usd`, `col-exit` para ocultar columnas en mobile.
- `dashboard/web/src/components/WhaleLog.tsx` — Clases `col-sig`, `wallet-addr` para ocultar columnas en mobile.
- `dashboard/web/src/components/HealthGrid.tsx` — Clase `health-inner` para flex-wrap en mobile.
- `docs/context/06-dashboard.md` — Sección "Responsive" documentada, limitación "Sin responsive mobile" removida.

**Por qué:** Dashboard era inutilizable en móvil — grid fijo de 3 columnas, tablas de 6-9 columnas sin scroll. Ahora funciona en 375px+ (iPhone SE).
**Impacto:** dashboard/web/, docs/context/

## [2026-03-04] — Fix liquidity level clustering tolerance
**Qué cambió:**
- `config/settings.py` — `EQUAL_LEVEL_TOLERANCE_PCT`: 0.0005 (0.05%) → 0.002 (0.2%). Para BTC a $73k, la tolerancia pasa de $36.50 a $146. Para ETH a $2.1k, de $1.07 a $4.30.
- `docs/context/02-strategy.md` — Documentación actualizada con nuevo valor de tolerancia.

**Por qué:** Diagnóstico reveló que el bot detectaba 0 sweeps de liquidez porque la tolerancia para agrupar swing highs/lows en niveles era demasiado estricta. A 0.05%, dos swing highs de BTC debían estar dentro de $36.50 para formar un nivel — imposible en velas de 15m/5m. Con 0.2%, ETH 15m pasó de 0 a 2 niveles y 1 sweep detectado en el primer ciclo.

**Análisis de sensibilidad ejecutado:**
| Tolerancia | ETH 15m levels | ETH 15m sweeps | BTC 15m levels |
|---|---|---|---|
| 0.05% (antes) | 1 | 0 | 3 |
| 0.20% (ahora) | 6 | 4 | 3 |

**Impacto:** config/settings.py, docs/context/
**Tests:** 45/45 passing (liquidity + setups).

## [2026-03-04] — Safety-Critical Fixes: Execution + Risk Service
**Qué cambió:**
Batch de fixes safety-critical del audit (6 IMPORTANT + 3 MINOR en Execution, 1 IMPORTANT + 5 MINOR en Risk).

**Execution Service:**
- **I-E4** `service.py` — Validación de precio ordering en `execute()`. Long: `sl < entry < tp1 < tp2 < tp3`, Short: inverso. Rechaza trades malformados antes de tocar exchange.
- **I-E2** `monitor.py` — TP placement failure ahora dispara emergency close (cancela TPs+SL colocados, cierra por market). Antes solo logeaba WARNING y continuaba — un TP faltante impedía mover SL a breakeven.
- **I-E3** `models.py` + `monitor.py` — PnL blended: acumula `realized_pnl_usd` en cada TP fill, calcula PnL combinado (realized + unrealized remainder) al cerrar. Antes asumía 100% del size al precio de salida.
- **I-E7** `monitor.py` — Guard `.postgres is None` en ambos persist methods (antes crasheaba si DataService estaba up pero PG down).
- **I-E8** `monitor.py` — 3 `asyncio.ensure_future` reemplazados por `_safe_notify()` con error-logging callback (patrón `_pipeline_task_done` de websocket_feeds).
- **I-E1** `monitor.py` — Documentación de race window en `_adjust_sl`: por qué `reduceOnly` mitiga el doble close, TODO para OKX amend-order API.
- **M-E3** `monitor.py` — Comentario: slippage se persiste via `actual_entry` en PG.

**Risk Service:**
- **I-R1** `state_tracker.py` + `service.py` + `monitor.py` — `record_trade_closed` ahora recibe `direction`, matchea por `(pair, direction)`. **Breaking change** — todos los callers actualizados.
- **M-R1** `state_tracker.py` — `_check_date_reset` usa `date()` en vez de `tm_yday` (fix: Dec 31 tm_yday=365 → Jan 1 tm_yday=1 no dispararía reset si tm_yday se comparaba como int).
- **M-R2** `state_tracker.py` — Docstring: PnL summation es aproximación negligible a esta escala.
- **M-R3** `service.py` — `_persist_failures` counter con warning log tras 5 fallos consecutivos.
- **M-R4** `test_risk_service.py` — Docstring: ValueError path en position_sizer es unreachable (guardrails lo atrapan antes).
- **M-R5** `test_claude_client.py` — Test: `"approved": "true"` (string) → retorna None.

**Tests:** 291 passing (9 nuevos: tp3_full_close, adjust_sl_failure, 2 sl/tp validation, blended_pnl, year_boundary, 2 direction_matching, approved_string_true).

**Por qué:** Fixes pre-producción que afectan directamente dinero real: SL/TP placement failures, PnL calculation, position tracking, date boundaries.
**Impacto:** execution_service/, risk_service/, tests/

## [2026-03-04] — Whale Tracking — All Large Movements (not just exchange)
**Qué cambió:**
- `shared/models.py` — `WhaleMovement` ahora documenta 4 acciones: `exchange_deposit`, `exchange_withdrawal`, `transfer_out`, `transfer_in`. Campo `exchange` puede ser nombre de exchange O dirección truncada (`0xab12...ef34`).
- `data_service/etherscan_client.py` — Dos nuevas ramas `elif` en `_process_transaction()`: si el wallet envía a dirección no-exchange → `transfer_out`; si recibe de dirección no-exchange → `transfer_in`. Mismos thresholds (WHALE_MIN_ETH) y lógica de significancia.
- `data_service/btc_whale_client.py` — Case 1 extendido: si wallet es sender y ningún output va a exchange, suma outputs non-self → `transfer_out`. Nuevo Case 3: si wallet recibe y ningún input es exchange → `transfer_in`. Mismos thresholds (WHALE_MIN_BTC).
- `ai_service/prompt_builder.py` — Ternario binario reemplazado por `_ACTION_LABELS` dict con 4 acciones ("deposited to", "withdrew from", "transferred out to", "received from"). Nota en system prompt: transferencias non-exchange = señal neutral.
- `shared/notifier.py` — Ternario reemplazado por `_WHALE_ACTION_MAP` class variable. Transfers usan círculo amarillo + "NEUTRAL".
- `dashboard/web/src/components/WhaleLog.tsx` — `isDeposit` ternario reemplazado por `actionConfig` object con 4 acciones → badge class + label. Nuevos labels: "transfer out", "transfer in".
- `dashboard/web/src/app/globals.css` — Nueva clase `.badge-neutral` (gris muted).
- Tests: `test_non_exchange_transfer_ignored` → `test_non_exchange_transfer_out_tracked` (ahora espera movement). Nuevo `test_non_exchange_transfer_in_tracked`. Nuevo `test_non_exchange_whale_labels` en prompt builder.

**Por qué:** El sistema solo creaba WhaleMovement cuando la transferencia involucraba un exchange conocido. 90%+ de la actividad whale se descartaba silenciosamente. Ahora TODO movimiento grande se trackea — exchange transfers siguen siendo bearish/bullish, non-exchange transfers son neutral/informational.
**Impacto:** shared/, data_service/, ai_service/, dashboard/, tests/
**Tests:** 291 passing (3 nuevos, 1 renombrado).

## [2026-03-04] — Full Audit — 12 CRITICAL fixes
**Qué cambió:**
Auditoría completa de las 5 capas. 12 issues CRITICAL corregidos, 28 IMPORTANT + 29 MINOR documentados en `docs/to-fix.md`.

**Fixes aplicados:**
1. **Wallet dedup** (`config/settings.py`) — Removidas 5 ETH exchange hot wallets de `WHALE_WALLETS` que también estaban en `EXCHANGE_ADDRESSES` (Binance, Kraken, Bithumb, Crypto.com, Gate.io). Removidos 3 BTC duplicados (MicroStrategy/Bitfinex Hack Recovery, Bitfinex exchange, Binance).
2. **PostgreSQL reconnection** (`data_service/data_store.py`) — `_ensure_connected()` con `SELECT 1` health check. Todos los métodos DB retry una vez en `OperationalError`/`InterfaceError`.
3. **Pipeline serialization** (`data_service/websocket_feeds.py`) — Per-pair `asyncio.Lock` previene pipelines concurrentes en el mismo par. `task.add_done_callback()` para logging de excepciones.
4. **asyncio.get_running_loop()** (`data_service/service.py`, `execution_service/executor.py`) — Reemplaza todas las llamadas a `get_event_loop()` (deprecated).
5. **OKX algo orders** (`execution_service/executor.py`) — `ordType: "conditional"` para SL stop-market orders. `fetch_order()` con fallback a `_fetch_algo_order()`.
6. **Emergency close retry** (`execution_service/monitor.py`) — Verifica return value de `close_position_market()`. Fase `emergency_pending` con máximo 3 reintentos. Tras 3 fallos → `emergency_failed`.
7. **Cancelled entries** (`execution_service/monitor.py`) — Entries canceladas (timeout sin fill) no notifican a Risk ni envían Telegram de trade cerrado.
8. **Sweep temporal guard** (`strategy_service/liquidity.py`) — Solo evalúa candles con timestamp > `max(level.timestamps)` para prevenir sweeps falsos.
9. **OB break_timestamp** (`strategy_service/order_blocks.py`) — Campo `break_timestamp` en OrderBlock. Mitigación solo evalúa candles posteriores a la vela de ruptura.
10. **Setup A documentation** (`strategy_service/setups.py`) — Comentario explicando que Setup A es patrón de CONTINUACIÓN (CHoCH alineado con HTF bias) — decisión intencional, no bug.
11. **Whale notification stability** (`data_service/service.py`) — Usa `id()` snapshot antes del polling para detectar nuevos movimientos sin depender de índices de lista.

**Nuevos campos:**
- `ManagedPosition.emergency_retries: int = 0`
- `OrderBlock.break_timestamp: int = 0`

**Tests:** 280/280 passing. 3 tests actualizados (PG no-connection mocking, cancelled entry assertion).

**Por qué:** Auditoría pre-producción para eliminar bugs críticos antes de activar trading en sandbox.
**Impacto:** config/, data_service/, strategy_service/, execution_service/, tests/, docs/

## [2026-03-04] — BTC Whale Movement Tracking
**Qué cambió:**
- `shared/models.py` — WhaleMovement generalizado: `amount_eth` → `amount`, nuevo campo `chain` ("ETH" o "BTC"). Soporta ambas cadenas.
- `config/settings.py` — 15 BTC whale wallets (mega-wallets, gobiernos, Mt. Gox, MicroStrategy, etc.), 11 exchange addresses (Binance, Robinhood, Bitfinex, OKX, Kraken, etc.), `WHALE_MIN_BTC=10`, `WHALE_HIGH_BTC=100`, `MEMPOOL_CHECK_INTERVAL=300`.
- `data_service/btc_whale_client.py` — NUEVO. Cliente mempool.space REST API. Parsea modelo UTXO (vin/vout) para detectar deposits/withdrawals a exchanges conocidos. Rate limit 0.5s entre calls. Misma API pública que EtherscanClient.
- `data_service/etherscan_client.py` — Usa `amount=` y `chain="ETH"`. `serialize_movements()` incluye `chain` en JSON.
- `data_service/service.py` — Integra `BtcWhaleClient`. `get_whale_movements()` y `get_market_snapshot()` mergean ETH+BTC. Nuevo `_btc_whale_loop()`. `_publish_whale_movements()` centraliza publicación a Redis (ETH+BTC combinados).
- `ai_service/prompt_builder.py` — Sección whale dinámica: "150.0 ETH" o "10.5 BTC" según chain.
- `dashboard/api/models.py` — `WhaleMovementRecord`: `amount_eth` → `amount`, nuevo `chain`.
- `dashboard/web/src/lib/api.ts` — Interface actualizada: `amount`, `chain`.
- `dashboard/web/src/components/WhaleLog.tsx` — Título "Whale Movements (24h)", columna Amount muestra "500.00 ETH" o "10.5000 BTC", decimales dinámicos por chain.
- `tests/` — Fixtures actualizadas en test_data_service.py y test_prompt_builder.py.

**Por qué:** ETH whale tracking estaba funcionando pero BTC no se monitoreaba. BTC es el par principal del bot. mempool.space es gratis, sin API key, y cubre Bitcoin mainnet.
**Impacto:** shared/, config/, data_service/, ai_service/, dashboard/, tests/

## [2026-03-04] — Dashboard — Whale Movements Section
**Qué cambió:**
- `config/settings.py` — `WHALE_WALLETS` cambiado de `List[str]` a `dict[str, str]` (address → label). Permite mostrar nombres legibles en el dashboard.
- `data_service/etherscan_client.py` — Itera `.keys()` del dict. Nuevo método `serialize_movements()` que incluye label de cada wallet en el JSON.
- `data_service/data_store.py` — 2 métodos nuevos en RedisStore: `set_whale_movements()` / `get_whale_movements()`. Key: `qf:bot:whale_movements`, TTL 600s.
- `data_service/service.py` — Etherscan ya no se lanza como tarea independiente. Nuevo `_etherscan_loop()` que ejecuta el poll y publica a Redis después de cada ciclo.
- `dashboard/api/models.py` — Nuevo modelo `WhaleMovementRecord` (timestamp, wallet, label, action, amount_eth, exchange, significance).
- `dashboard/api/routes/whales.py` — Nuevo endpoint `GET /api/whales?hours=24`. Lee Redis, filtra por timestamp.
- `dashboard/api/main.py` — Router de whales registrado.
- `dashboard/web/src/lib/api.ts` — Interface `WhaleMovement` TypeScript.
- `dashboard/web/src/components/WhaleLog.tsx` — Tabla: Time, Wallet (label + truncated addr), Action (deposit=red, withdrawal=green badge), Amount ETH, Exchange, Significance. Polls cada 30s.
- `dashboard/web/src/app/page.tsx` — WhaleLog agregado al grid entre trade/AI logs y health bar.
- `dashboard/web/src/app/globals.css` — Grid row 5 para whale-log (full width), health bar movido a row 6.
- `tests/test_data_service.py` — Tests Etherscan actualizados para usar dict en vez de list.

**Por qué:** Whale movements solo vivían en memoria del bot. El dashboard (proceso separado) necesita leerlos via Redis.
**Impacto:** config/, data_service/, dashboard/, tests/

## [2026-03-04] — Telegram Notifications
**Qué cambió:**
- `shared/notifier.py` — Nuevo módulo `TelegramNotifier`. Envía mensajes via Telegram Bot API (httpx POST). Fire-and-forget: si Telegram falla, el bot continúa. 6 métodos: `notify_setup_detected`, `notify_ai_decision`, `notify_risk_rejected`, `notify_trade_opened`, `notify_trade_closed`, `notify_emergency`.
- `config/settings.py` — 2 settings nuevos: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (desde .env).
- `main.py` — Inicializa `TelegramNotifier` al startup. Notifica en pipeline: setup detected, AI decision (approved/rejected), risk rejected.
- `execution_service/service.py` — Acepta `notifier` parameter, pasa a `PositionMonitor`.
- `execution_service/monitor.py` — Acepta `notifier` parameter. Notifica: trade opened (entry fill), trade closed (SL/TP/timeout), emergency close (SL placement failure).

**Por qué:** Monitorear el bot desde el celular sin revisar el dashboard constantemente. Push notifications instantáneas en Telegram para cada evento clave.
**Impacto:** shared/notifier.py, config/settings.py, main.py, execution_service/

## [2026-03-04] — Dashboard — Phase 6
**Qué cambió:**
- **Trade persistence** — Bot ahora escribe a PostgreSQL: `insert_trade()`, `update_trade()`, `insert_ai_decision()`, `insert_risk_event()` en `data_store.py`. Wired en `monitor.py` (entry fill/close), `main.py` (AI decisions), `risk_service/service.py` (guardrail events). Redis key `qf:bot:positions` para posiciones abiertas.
- **FastAPI backend** (`dashboard/api/`) — 8 endpoints read-only + WebSocket. asyncpg + redis.asyncio. Pydantic schemas. Health check con PG + Redis ping.
- **Next.js frontend** (`dashboard/web/`) — Single-page "VAULT" theme: dark (#0a0e17), monospace, tabular-nums. 8 componentes: Header, PricePanel, RiskGauge (SVG arc DD gauges), PositionCard, PnLChart (SVG sparkline), TradeLog, AILog (confidence bars), HealthGrid.
- **Docker** — 2 nuevos servicios en `docker-compose.yml`: `api` (python:3.12-slim, port 8000) y `web` (node:20-alpine, port 3000). `network_mode: host`.
- `execution_service/models.py` — Campo `db_trade_id` para tracking en PostgreSQL
- `execution_service/monitor.py` — `data_store` parameter, `_persist_trade_open/close()`, `_update_positions_cache()`
- `execution_service/service.py` — Acepta `data_service` parameter, pasa a PositionMonitor
- `risk_service/service.py` — Acepta `data_service` parameter, `_persist_risk_event()` en guardrail hits
- `data_service/service.py` — Properties `postgres` y `redis` para acceso directo
- `data_service/data_store.py` — 4 métodos nuevos en PostgresStore, `set/get_positions` en RedisStore
- `main.py` — Pasa `data_service` a Risk y Execution. Persiste AI decisions y risk events en pipeline.
- `docs/context/06-dashboard.md` — Documentación completa

**Por qué:** Sexto paso — monitorear el bot sin `docker compose logs`. Dashboard en http://192.168.1.238:3000.
**Impacto:** dashboard/, data_service/, execution_service/, risk_service/, main.py, docker-compose.yml, docs/

## [2026-03-04] — Docker Compose deployment
**Qué cambió:**
- `.dockerignore` — Excluye .git, venv, tests, docs, .env del build context
- `Dockerfile` — python:3.12-slim, pip install, `python -u main.py`, healthcheck via pgrep
- `docker-compose.yml` — 3 servicios: postgres:16-alpine, redis:7-alpine, bot. Healthchecks, named volumes (`pgdata`, `redisdata`), `restart: unless-stopped`, `stop_grace_period: 30s`
- `.env` (root) — Variable `POSTGRES_PASSWORD` para Docker Compose interpolation
- `config/.env` — Template con todos los secrets del bot (montado read-only en container)
- `docs/context/00-architecture.md` — Sección "Docker Compose — Deployment" con tabla de servicios, archivos, configuración, y comandos
- `docs/context/01-data-service.md` — Status actualizado de "ready for integration" a "running in Docker"
- `main.py` — Docstring actualizado (ya no dice "stub")
- `docs/to-fix.md` — Issues pendientes documentados

**Nota técnica:** Bot usa `network_mode: host` porque Docker bridge no tiene NAT configurado en el server. Build usa `network: host` para pip install. Dos `.env` necesarios: root para Compose interpolation, `config/.env` para el bot Python.

**Por qué:** Sexto paso — containerizar el bot con sus dependencias (PostgreSQL, Redis) para correr 24/7.
**Impacto:** Dockerfile, docker-compose.yml, .dockerignore, .env, config/.env, docs/

## [2026-03-04] — Docs sync + trailing stop v2 roadmap
**Qué cambió:**
- `docs/context/00-architecture.md` — Estado actualizado a "5/5 capas implementadas". Removido Coinglass. Tabla de estado por capa con conteo de tests (280 total). Sección roadmap v2 con trailing stop y otras mejoras.
- `docs/context/01-data-service.md` — main.py ya no es "stub", pipeline completo.
- `docs/context/03-ai-filter.md` — "adjustments" actualizado: Execution Service ya existe, aplicar ajustes es para v2.
- `docs/context/04-risk.md` — 3 referencias a "Execution Service (futuro)" actualizadas a "(implementado)". Max trade duration ahora es enforceado por PositionMonitor.
- `docs/context/05-execution.md` — Roadmap v2 detallado para trailing stop: API de OKX, settings nuevos, cambios en monitor/executor, consideraciones de testing y fallback.

**Por qué:** Los docs referenciaban Execution Service como futuro cuando ya está implementado. El usuario pidió documentar trailing stop para v2.
**Impacto:** docs/context/ (6 archivos)

## [2026-03-04] — Execution Service — Layer 5 implementado
**Qué cambió:**
- `execution_service/models.py` — ManagedPosition: estado mutable del ciclo de vida de cada trade (phase, order IDs, fills, PnL)
- `execution_service/executor.py` — OrderExecutor: wrapper ccxt para OKX (limit, stop-market, TP, cancel, fetch, emergency close, fetch_position)
- `execution_service/monitor.py` — PositionMonitor: loop async polling cada 5s, máquina de estados (pending_entry → active → tp1_hit → tp2_hit → closed)
- `execution_service/service.py` — ExecutionService facade: execute(), start(), stop(), health()
- `execution_service/__init__.py` — Exporta ExecutionService
- `config/settings.py` — 4 settings nuevos: ENTRY_TIMEOUT_SECONDS, ORDER_POLL_INTERVAL, MARGIN_MODE, MAX_TRADE_DURATION_SECONDS
- `main.py` — Pipeline completo 5 capas: Data → Strategy → AI → Risk → Execution. Variable scoping fix para decision/approval. Graceful shutdown del Execution Service.
- `tests/test_execution.py` — 20 tests (facade, entry fill/timeout, TP1/TP2/SL lifecycle, emergency close, slippage, PnL, health)
- `docs/context/05-execution.md` — Documentación completa del servicio

**Por qué:** Quinto y último paso del build order. Sin Execution Service, los trades aprobados nunca se ejecutaban en OKX.
**Impacto:** execution_service/, config/settings.py, main.py, tests/, docs/context/

## [2026-03-03] — Strategy Service review — 6 fixes
**Qué cambió:**
- `strategy_service/market_structure.py` — Solo un break por candle. Si una vela grande rompe múltiples swing levels, solo se registra el más significativo (mayor distancia). Los demás se marcan como consumidos.
- `strategy_service/setups.py` — 3 fixes:
  - **Orden temporal en Setup A**: sweep debe ocurrir ANTES del CHoCH, con proximidad máxima configurable (`SETUP_A_MAX_SWEEP_CHOCH_GAP=20` candles).
  - **R:R blended**: validación ponderada real (50%×TP1 + 30%×TP2 + 20%×TP3) en vez de check muerto contra TP2.
  - **Proximidad OB**: margen basado en % del precio (`OB_PROXIMITY_PCT=0.3%`) en vez de 50% del body del OB.
- `strategy_service/liquidity.py` — 2 fixes:
  - **Equilibrium band**: zona equilibrium es ahora 48%-52% (±`PD_EQUILIBRIUM_BAND`), no exacto 50%.
  - **Swept persistence**: niveles de liquidez mantienen su estado `swept` entre llamadas via merge por proximidad de precio.
- `config/settings.py` — 3 settings nuevos: `PD_EQUILIBRIUM_BAND`, `OB_PROXIMITY_PCT`, `SETUP_A_MAX_SWEEP_CHOCH_GAP`
- `tests/` — 12 tests nuevos: blended R:R, OB proximity, temporal ordering, equilibrium band, swept persistence, single break per candle
- `docs/context/02-strategy.md` — Documentación actualizada con todos los cambios

**Por qué:** Revisión del @planner encontró 6 issues (3 críticos, 2 significativos, 1 menor). 192/192 tests passing.
**Impacto:** strategy_service/, config/settings.py, tests/, docs/context/

## [2026-03-03] — Binance → OI Proxy migration
**Qué cambió:**
- `data_service/oi_liquidation_proxy.py` — Nuevo módulo: detecta cascadas de liquidación via OI drops >2% en 5min. Ring buffer de 12 snapshots por par. Misma API pública que BinanceLiquidationFeed.
- `data_service/service.py` — Reemplaza `BinanceLiquidationFeed` por `OILiquidationProxy`. OI proxy alimentado desde `_oi_loop()` y `_fetch_initial_indicators()`. Removido Binance async task.
- `config/settings.py` — 2 settings nuevas: `OI_DROP_THRESHOLD_PCT` (0.02), `OI_DROP_WINDOW_SECONDS` (300)
- `ai_service/prompt_builder.py` — Prompt actualizado: "OI Proxy" en vez de "Binance feed offline". Sección de liquidaciones muestra source y disclaimer.
- `tests/test_oi_proxy.py` — 13 tests nuevos (detección, aislamiento por par, stats, edge cases, threshold custom)
- Tests existentes actualizados: `source="binance_forceOrder"` → `source="oi_proxy"` en test_setups.py, test_liquidity.py, test_prompt_builder.py
- `CLAUDE.md` — 6 referencias a Binance actualizadas. oi_liquidation_proxy.py agregado a estructura.
- `docs/context/01-data-service.md` — Sección Binance reemplazada por OI proxy. FAQ actualizada.

**Por qué:** Binance Futures WebSocket (`forceOrder`) está geo-bloqueado desde Canadá. OI proxy detecta las mismas cascadas indirectamente via OKX Open Interest (ya polled cada 5 min).
**Impacto:** data_service/, ai_service/, config/, tests/, CLAUDE.md, docs/

## [2026-03-03] — Risk Service review + settings cleanup
**Qué cambió:**
- `risk_service/state_tracker.py` — Warning log cuando `record_trade_closed()` no encuentra el pair en posiciones abiertas. Previene bugs silenciosos en el pipeline.
- `config/settings.py` — Todos los comentarios y docstrings traducidos de español a inglés (cumplimiento de CLAUDE.md language rule).
- `docs/context/04-risk.md` — Actualizado: conteo correcto de tests (14 en test_risk_service.py, no 10), sección de limitaciones conocidas, FAQ sobre warning log, cambios recientes.

**Por qué:** Revisión del @planner encontró 2 issues menores. Ningún bug crítico — 69/69 tests passing, 100% alineado con CLAUDE.md.
**Impacto:** risk_service/state_tracker.py, config/settings.py, docs/context/04-risk.md

## [2026-03-03] — AI Service — Layer 3 implementado
**Qué cambió:**
- `ai_service/prompt_builder.py` — System + evaluation prompts para Claude. Interpreta funding, OI, CVD, liquidaciones, whales.
- `ai_service/claude_client.py` — Wrapper async de Anthropic SDK. Timeout 30s, 2 retries, JSON parsing, code fence stripping.
- `ai_service/service.py` — Facade AIService.evaluate(setup, snapshot) → AIDecision. Double check confidence >= 0.60. Fail-safe en todo error.
- `ai_service/__init__.py` — Exporta AIService
- `config/settings.py` — 4 settings nuevas: AI_TIMEOUT_SECONDS, AI_TEMPERATURE, AI_MAX_TOKENS, FUNDING_EXTREME_THRESHOLD
- `requirements.txt` — Agregado anthropic==0.84.0
- `main.py` — Pipeline completo Data→Strategy→AI→Risk. data_service como variable module-level. Graceful shutdown de AI client. Validación de ANTHROPIC_API_KEY.
- `tests/test_prompt_builder.py` — 14 tests (system prompt, eval prompt, datos faltantes, funding extremo)
- `tests/test_claude_client.py` — 8 tests (JSON parsing, API errors, timeout, rate limit)
- `tests/test_ai_service.py` — 12 tests (approval/rejection, confidence clamping, API failure, disabled mode)
- `docs/context/03-ai-filter.md` — Documentación completa del servicio

**Por qué:** Cuarto paso del build order. El AI filter evalúa contexto de mercado que las reglas determinísticas no capturan.
**Impacto:** ai_service/, tests/, main.py, config/, docs/context/

## [2026-03-03] — Risk Service — Layer 4 implementado
**Qué cambió:**
- `risk_service/position_sizer.py` — Calculadora: (capital × risk%) / |entry - sl|, cap a MAX_LEVERAGE (5x)
- `risk_service/guardrails.py` — 6 checks puros: R:R, cooldown, max trades/día, max posiciones, DD diario, DD semanal
- `risk_service/state_tracker.py` — Estado in-memory: trades hoy, posiciones abiertas, P&L, cooldown, auto-reset diario/semanal
- `risk_service/service.py` — Facade RiskService.check(setup) → RiskApproval. Fail fast.
- `risk_service/__init__.py` — Exporta RiskService
- `main.py` — Integrado en pipeline: setup detectado → risk.check() → log approval/rejection. Capital $100 demo.
- `tests/test_position_sizer.py` — 10 tests (fórmula, leverage cap, edge cases)
- `tests/test_guardrails.py` — 17 tests (cada regla pass/fail/boundary)
- `tests/test_state_tracker.py` — 18 tests (lifecycle, DD, cooldown, date reset)
- `tests/test_risk_service.py` — 10 tests (check() integración completa)
- `docs/context/04-risk.md` — Documentación completa del servicio
- `docs/context/02-strategy.md` — Actualizado de "pendiente" a "implementado"

**Por qué:** Tercer paso del build order. Sin Risk Service, no hay protección del capital.
**Impacto:** risk_service/, tests/, main.py, docs/context/

## [2026-03-03] — Exchange Revert — Hyperliquid → OKX
**Qué cambió:**
- Reverted ALL files from Hyperliquid back to OKX. Same files as the migration entry below, but in reverse.
- `CLAUDE.md` — OKX via ccxt + native WebSocket. Pairs: BTC/USDT, ETH/USDT. API key auth. Demo mode. Rate limits 20req/2s.
- `config/settings.py` — WALLET_PRIVATE_KEY/WALLET_ADDRESS/HYPERLIQUID_TESTNET → OKX_API_KEY/SECRET/PASSPHRASE/SANDBOX. TRADING_PAIRS back to USDT. FUNDING_RATE_INTERVAL 1h→8h.
- `shared/models.py` — All docstrings: Hyperliquid → OKX, USDC → USDT, 1h → 8h funding.
- `shared/logger.py` — Example log messages back to OKX.
- `data_service/exchange_client.py` — Full rewrite: ccxt.hyperliquid → ccxt.okx. API key/secret/passphrase auth. BTC-USDT-SWAP format. 100 candles/req.
- `data_service/websocket_feeds.py` — Full rewrite: Hyperliquid WS → OKX WS (wss://ws.okx.com:8443/ws/v5/public). OKX subscription format. Candle confirmation via confirm="1".
- `data_service/cvd_calculator.py` — Full rewrite: Hyperliquid trades WS → OKX trades WS. Side "buy"/"sell" directly.
- `data_service/binance_liq.py` — Pair mapping: BTC/USDC:USDC → BTC/USDT.
- `data_service/service.py` — HyperliquidWebSocketFeed → OKXWebSocketFeed. Task names reverted.
- `data_service/data_store.py` — Redis key examples back to USDT pairs.
- `main.py` — Config validation for OKX API key / sandbox.
- `.claude/agents/data-engineer.md` — Full rewrite: Hyperliquid API → OKX API section.
- `.claude/agents/architect.md` — Hyperliquid → OKX context.
- `.claude/agents/risk-guard.md` — Hyperliquid → OKX. DEX risk → CEX risk.
- `.claude/agents/smc-engine.md` — Hyperliquid OI proxy → OKX OI proxy.
- `docs/context/01-data-service.md` — Full rewrite for OKX.

**Por qué:** Reverted Hyperliquid migration — API geo-blocked from Canada. OKX API confirmed working from server. OKX website is blocked in Canada but the API works fine, and the bot only uses the API.
**Impacto:** Todo el proyecto excepto etherscan_client.py.

## [2026-03-03] — Exchange Migration — OKX → Hyperliquid
**Qué cambió:**
- `CLAUDE.md` — Exchange changed to Hyperliquid (DEX). Pairs: BTC/USDC:USDC, ETH/USDC:USDC. Settlement in USDC. Wallet auth. Funding hourly. Rate limits updated.
- `config/settings.py` — OKX_API_KEY/SECRET/PASSPHRASE/SANDBOX → WALLET_PRIVATE_KEY/WALLET_ADDRESS/HYPERLIQUID_TESTNET. TRADING_PAIRS to USDC. FUNDING_RATE_INTERVAL 8h→1h.
- `shared/models.py` — All docstrings updated: OKX → Hyperliquid, USDT → USDC, 8h → 1h funding.
- `data_service/exchange_client.py` — Full rewrite: ccxt.okx → ccxt.hyperliquid. Wallet key auth. Symbol format BTC/USDC:USDC.
- `data_service/websocket_feeds.py` — Full rewrite: OKX WS → Hyperliquid WS (wss://api.hyperliquid.xyz/ws). New subscription format. Candle confirmation via open-time tracking (no confirm flag).
- `data_service/cvd_calculator.py` — Full rewrite: OKX trades WS → Hyperliquid trades WS. Side mapping B/A → buy/sell.
- `data_service/binance_liq.py` — Pair mapping updated (BTCUSDT → BTC/USDC:USDC). Binance WS itself unchanged (public, no account needed).
- `data_service/service.py` — OKXWebSocketFeed → HyperliquidWebSocketFeed. Comments updated.
- `data_service/data_store.py` — Redis key examples updated to USDC pairs.
- `main.py` — Config validation updated for wallet key / testnet.
- `.claude/agents/data-engineer.md` — Entire OKX API section → Hyperliquid API section. File structure updated.
- `.claude/agents/architect.md` — OKX section → Hyperliquid section. DEX context added.
- `.claude/agents/risk-guard.md` — OKX refs → Hyperliquid. Exchange risk updated for DEX.
- `.claude/agents/smc-engine.md` — OKX OI proxy → Hyperliquid OI proxy.
- `docs/context/01-data-service.md` — Full rewrite for Hyperliquid.

**Por qué:** Migrated from OKX to Hyperliquid — Canada restricts all major CEX derivatives (OKX, Binance, Bybit, Kraken). Hyperliquid is a DEX with no KYC and no geographic restrictions.
**Impacto:** Todo el proyecto excepto etherscan_client.py y shared/logger.py.

## [2026-03-03] — Data Service — CVD, Etherscan, Data Store
**Qué cambió:**
- `data_service/cvd_calculator.py` — OKX trades WebSocket with 5-second batching, CVD for 5m/15m/1h windows
- `data_service/etherscan_client.py` — Whale wallet monitoring, exchange deposit/withdrawal detection
- `data_service/data_store.py` — Redis (real-time state cache with TTLs) + PostgreSQL (historical candles, 4 tables from CLAUDE.md schema)
- `config/settings.py` — Added WHALE_MIN_ETH, WHALE_HIGH_ETH, WHALE_WALLETS, EXCHANGE_ADDRESSES
- `docs/context/01-data-service.md` — Complete documentation of all 7 data service modules

**Por qué:** Complete the Data Service with all data sources before building Strategy Service.
**Impacto:** data_service/, config/, docs/

## [2026-03-03] — Data Service — Core implementation
**Qué cambió:**
- `shared/models.py` — 10 dataclasses (Candle, FundingRate, OpenInterest, CVDSnapshot, LiquidationEvent, WhaleMovement, MarketSnapshot, TradeSetup, AIDecision, RiskApproval)
- `shared/logger.py` — loguru setup with stdout + daily rotated files, 30-day retention
- `data_service/exchange_client.py` — OKX REST via ccxt (backfill 500 candles, funding rate, OI)
- `data_service/websocket_feeds.py` — OKX WebSocket for real-time candles (8 channels, confirm="1" only)
- `data_service/binance_liq.py` — Binance Futures WebSocket for liquidation data (forceOrder channel)
- `config/settings.py` — removed Coinglass references (deferred to future phase)
- `docs/context/01-data-service.md` — full documentation of implemented modules

**Por qué:** First step in build order. Data Service must work before Strategy Service can detect patterns.
**Impacto:** shared/, data_service/, config/, docs/

## [2026-03-03] — Proyecto — Setup inicial
**Qué cambió:** Estructura completa del proyecto creada con 5 servicios, agentes, documentación.
**Por qué:** Primer día de construcción del bot.
**Impacto:** Todo el proyecto.
