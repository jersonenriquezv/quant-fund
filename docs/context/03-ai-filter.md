# AI Service
> Última actualización: 2026-03-05
> Estado: implementado (completo, integrado en main.py). Pre-filter determinístico + hybrid scalping mode.

## Qué hace (30 segundos)
El AI Service es el consultor senior del sistema. Recibe cada trade setup del Strategy Service y lo pasa por Claude (Sonnet) para que evalúe si el contexto de mercado apoya ejecutarlo. Claude analiza funding rate, open interest, CVD, liquidaciones, whale movements y precio reciente. Si confidence >= 0.60 y approved=true, el trade pasa al Risk Service. Si no, se descarta.

## Por qué existe
El Strategy Service es determinístico — detecta patrones SMC con reglas fijas. Pero las reglas no capturan contexto macro, anomalías en funding/OI, ni correlaciones de mercado. Un CHoCH+OB válido durante un crash no debería ejecutarse. Claude actúa como el trader senior que revisa cada setup antes de aprobar. Target: aprobar 30-60% de los setups.

## Cómo funciona (5 minutos)

### Flujo de datos
```
TradeSetup + MarketSnapshot (del Strategy/Data Service)
  │
  ▼
AIService.evaluate(setup, snapshot)
  │
  ├── PromptBuilder construye prompt con datos de mercado
  ├── ClaudeClient envía a Claude API (Sonnet)
  ├── Claude responde con JSON: confidence, approved, reasoning, adjustments, warnings
  ├── Double check: approved=true AND confidence >= 0.60
  └── Si API falla → rechaza (fail-safe)
  │
  ▼
AIDecision { confidence, approved, reasoning, adjustments, warnings }
```

### Fail-Safe
- API key no configurada → rechaza todos los trades, log warning
- API timeout/error → rechaza el trade, no crashea
- JSON inválido de Claude → rechaza el trade
- Confidence > 1.0 o < 0.0 → clamped a [0, 1]
- Claude dice approved=true pero confidence < 0.60 → rechazado

### El Prompt
**System prompt** (se cachea, no cambia):
- Rol: senior crypto trading analyst en fondo cuantitativo
- Instrucción: responder SOLO con JSON válido
- 7 factores a evaluar: funding, OI, CVD, liquidaciones, whales, HTF confluence, calidad del setup
- Reglas críticas: no aprobar solo por patrón válido, funding extremo = escepticismo, CVD divergente = warning

**User prompt** (por cada evaluación):
- Setup completo: pair, direction, entry, SL, TPs, confluences
- Funding rate con interpretación (normal/extreme)
- Open interest
- CVD con buy dominance %
- Liquidaciones recientes (long vs short)
- Whale activity (exchange transfers = bearish/bullish signal, non-exchange transfers = neutral/informational)
- Price context (cambio 1h, 4h)

## Archivos implementados

### `ai_service/prompt_builder.py` — Construcción de prompts
- Clase: `PromptBuilder`
- `build_system_prompt()` → system prompt cacheado
- `build_evaluation_prompt(setup, snapshot, candles_context)` → user prompt con datos concretos
- Interpreta funding rate: normal/extreme basado en `FUNDING_EXTREME_THRESHOLD` (0.03%)
- Maneja datos faltantes gracefully ("Not available")
- Agrega interpretaciones para que Claude no tenga que calcular (e.g., buy dominance %)

### `ai_service/claude_client.py` — Wrapper del API
- Clase: `ClaudeClient`
- `evaluate(system_prompt, user_prompt)` → dict parsedo | None
- Usa SDK oficial de Anthropic (`AsyncAnthropic`)
- `temperature=0.3` para decisiones consistentes
- `max_tokens=500` (respuesta JSON es ~200-400 tokens)
- Timeout configurable (`AI_TIMEOUT_SECONDS`)
- `max_retries=2` con backoff del SDK
- Strip de markdown code fences si Claude wrappea el JSON
- Validación de campos requeridos (confidence, approved, reasoning)
- Logs token usage on every successful call: `Claude tokens: input=X output=Y total=Z`
- Todo error → retorna None (fail-safe)

### `ai_service/service.py` — Facade (AIService)
- Clase: `AIService(data_service=None)`
- `evaluate(setup, snapshot)` → `AIDecision`
- Obtiene candles context del DataService para price change
- Double check: `approved=True` AND `confidence >= AI_MIN_CONFIDENCE`
- Clamp confidence a [0, 1]
- Sin API key → disabled, rechaza todo
- API failure → rechaza con razón clara en logs

### `ai_service/__init__.py`
- Exporta `AIService`

## Configuración (`config/settings.py`)

| Setting | Default | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | API key de Anthropic |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Modelo a usar |
| `AI_MIN_CONFIDENCE` | `0.60` | Confianza mínima para aprobar |
| `AI_TIMEOUT_SECONDS` | `30.0` | Timeout por request |
| `AI_TEMPERATURE` | `0.3` | Temperatura (menor = más consistente) |
| `AI_MAX_TOKENS` | `500` | Tokens máximos de respuesta |
| `FUNDING_EXTREME_THRESHOLD` | `0.0003` | Threshold para funding "extreme" (0.03%) |

## Tests

41 tests en 3 archivos:
- `test_prompt_builder.py` (18) — system prompt, evaluation prompt, datos faltantes, funding extremo, non-exchange whale labels, profile-aware prompts (scalping section, HTF bias annotation)
- `test_claude_client.py` (9) — JSON válido/inválido, code fences, API errors, timeout, rate limit, approved string type validation
- `test_ai_service.py` (14) — approval/rejection, confidence clamping, double check, API failure, disabled mode, profile-specific confidence thresholds, data service integration

## Persistence

Every AI decision (approved or rejected) is persisted to PostgreSQL `ai_decisions` table via `main.py:_persist_ai_decision()`. The record includes:
- `pair`, `direction`, `setup_type` — from the TradeSetup being evaluated
- `approved` — boolean result of the decision
- `confidence`, `reasoning`, `adjustments`, `warnings` — from the AIDecision
- `trade_id` — linked to trades table if the trade was ultimately executed (None for rejections)

The dashboard AILog component shows pair, direction badge, and approved/rejected status for each decision.

## Pre-Filter (todas las profiles)

Antes de llamar a Claude API, `main.py:_pre_filter_for_claude()` ejecuta 2 checks determinísticos que rechazan setups obvios sin gastar tokens. Los checks son conservadores — si los datos no están disponibles, el check se salta (no genera falsos rechazos).

### Check 1: Funding extreme contra dirección
- Long + `funding_rate > FUNDING_EXTREME_THRESHOLD` (0.03%) → rechaza
- Short + `funding_rate < -FUNDING_EXTREME_THRESHOLD` → rechaza
- Si `snapshot.funding` es None → skip

### Check 2: CVD divergencia fuerte contra dirección
- Long + `buy_dominance < 40%` → rechaza
- Short + `buy_dominance > 60%` → rechaza
- Si `snapshot.cvd` es None o volumen total = 0 → skip

**Qué pasa cuando el pre-filter rechaza:**
- Log: `"AI PRE-FILTERED: {reason}"`
- PostgreSQL: `approved=False, confidence=0.0, reasoning="Pre-filter: {reason}"`
- Telegram: "AI PRE-FILTERED" con razón
- Dashboard AILog: aparece como decisión rechazada (approved=False)

**Por qué estos checks:** Ambos checks replican reglas críticas del system prompt de Claude. Funding extreme y CVD divergence son condiciones que Claude siempre rechaza — el pre-filter evita la llamada API.

## Profile-Aware Evaluation

The AI filter adapts its behavior based on the active strategy profile (`settings.STRATEGY_PROFILE`). This is injected in the **user prompt** (not the system prompt, which is cached at `AIService.__init__`).

### Scalping Profile — Modo Híbrido
El scalping profile usa un enfoque híbrido: no todo scalp bypasea Claude.

**HTF-aligned scalps** (dirección alineada con HTF bias):
- Long + bias "bullish", o Short + bias "bearish"
- Son setups de mayor probabilidad que se benefician del análisis macro de Claude
- Pasan por pre-filter → Claude → Risk Service (mismo flujo que default/aggressive)

**Pure LTF scalps** (dirección NO alineada con HTF bias, o bias "undefined"):
- Operan solo por momentum del LTF, sin confirmación macro
- Bypasean Claude enteramente → directo a Risk Service
- Log: `"AI SKIPPED: scalping LTF-only ({direction}/{bias})"`
- PostgreSQL: `approved=True, confidence=0.0, reasoning="Scalping profile — AI filter bypassed"`
- Telegram: "AI SKIPPED (scalping)"

```
Setup detected (scalping profile)
  |
  +-- NOT HTF-aligned --> AI SKIPPED --> Risk Service
  |
  +-- HTF-aligned --> pre-filter --> Claude --> Risk Service
```

### Default / Aggressive Profiles
Todos los setups pasan por pre-filter → Claude. No cambia la lógica.

```
Setup detected (default/aggressive)
  |
  +-- pre-filter --> Claude --> Risk Service
```

### Why user prompt, not system prompt?
The system prompt is cached at `AIService.__init__` and does not update when the profile is switched via the dashboard. The user prompt is rebuilt on every evaluation, so it always reflects the current profile.

## FAQ

**¿Por qué temperature 0.3 y no 0?**
Temperature 0 puede hacer que Claude sea demasiado repetitivo con las mismas razones. 0.3 da decisiones consistentes pero permite variación en el razonamiento. En fondos cuantitativos, el modelo no debe ser creativo — debe ser consistente.

**¿Cuánto cuesta por evaluación?**
~1,100 tokens input + ~200 tokens output ≈ $0.006/evaluación con Sonnet ($3/MTok input, $15/MTok output). Con 5-15 setups/semana ≈ $0.12-0.36/mes. El uso exacto se loguea en cada llamada (`Claude tokens: input=X output=Y total=Z`).

**¿Por qué no streaming?**
La respuesta completa es ~200-400 tokens. No hay beneficio en recibir token por token. Un `await` simple es suficiente.

**¿Por qué no fallback a otro modelo?**
Si Claude no está disponible, rechazamos. Un fallback a otro modelo necesita otro prompt y testing separado. Con 99.9% uptime de Anthropic, no vale la complejidad.

**¿Qué pasa con los "adjustments"?**
Claude puede sugerir modificar SL/TP. El campo se pasa en `AIDecision.adjustments`, pero ni Risk ni Execution lo leen todavía. Planeado para v2 — aplicar ajustes de SL/TP antes de pasar al Execution Service.
