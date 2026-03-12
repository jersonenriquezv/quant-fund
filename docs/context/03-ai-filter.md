# AI Service
> Última actualización: 2026-03-12
> Estado: implementado but **currently bypassed for all active setups**. setup_a is in `AI_BYPASS_SETUP_TYPES` (89.6% approval = no value). setup_d_bos/setup_d_choch are in `QUICK_SETUP_TYPES` (skip AI by design). Setup B and F are disabled. No Claude API calls in the trading pipeline currently. Code and infrastructure remain for re-enable when recalibrated.

## Qué hace (30 segundos)
El AI Service es el filtro del sistema. Recibe cada trade setup del Strategy Service y lo pasa por Claude (Sonnet) para que evalúe si el contexto de mercado apoya ejecutarlo. Claude evalúa scoring dimensions (setup quality, market support, contradiction, data sufficiency) usando datos de funding rate, open interest, CVD, liquidaciones, whale movements y precio reciente. Si confidence >= 0.50 y approved=true, el trade pasa al Risk Service. Si no, se descarta.

## Por qué existe
El Strategy Service es determinístico — detecta patrones SMC con reglas fijas. Pero las reglas no capturan contexto macro, anomalías en funding/OI, ni correlaciones de mercado. Un CHoCH+OB válido durante un crash no debería ejecutarse. Claude actúa como filtro que revisa cada setup antes de aprobar. Target: aprobar 30-60% de los setups.

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
  ├── Claude responde con JSON: scores, supporting_factors, contradicting_factors, adjustments, warnings
  ├── Service construye reasoning desde factors, almacena scores en adjustments
  ├── Double check: approved=true AND confidence >= 0.50
  └── Si API falla → rechaza (fail-safe)
  │
  ▼
AIDecision { confidence, approved, reasoning (from factors), adjustments (+ scores), warnings }
```

### Fail-Safe
- API key no configurada → rechaza todos los trades, log warning
- API timeout/error → rechaza el trade, no crashea
- JSON inválido de Claude → rechaza el trade
- Campos requeridos faltantes (confidence, approved, scores) → rechaza
- scores no es dict → rechaza
- Confidence > 1.0 o < 0.0 → clamped a [0, 1]
- Claude dice approved=true pero confidence < 0.50 → rechazado

### El Prompt — Scoring Rubric (v2)
**System prompt** (se reconstruye por evaluación con threshold actual):
- Rol: trade filter (sin narrativa doctrinal — no Wyckoff/ICT/VSA)
- Método: scoring rubric con 4 dimensiones (0-5 cada una):
  1. **setup_quality** — fuerza de confluencias técnicas (0-1 weak, 2-3 moderate, 4-5 strong con OB volume >2x)
  2. **market_support** — datos disponibles que apoyan el trade (0 ninguno, 3-4 múltiples señales, 5 fuerte alineación)
  3. **contradiction** — evidencia CONTRA el trade (0 ninguna, 3-4 múltiples contradicciones, 5 fuerte contradicción)
  4. **data_sufficiency** — cuántos datos relevantes están disponibles (0-1 mayoría ausente, 4-5 comprensivos)
- Decision rules:
  - APPROVE: setup_quality >= 3 AND contradiction <= 2 AND confidence >= threshold
  - REJECT: contradiction >= 3, OR setup_quality <= 1, OR insufficient supporting evidence
  - "Insufficient edge" es rechazo VÁLIDO — no todo setup merece aprobación
  - Aprobación REQUIERE evidencia positiva, no solo ausencia de contradicción
- Confidence calibration: 0.80+ (strong), 0.60-0.79 (good), 0.50-0.59 (marginal), <0.50 (reject)
- Factor reading guide: neutral language, no interpretive framing
  - Funding: "directional crowding" (no "liquidation fuel")
  - CVD: context-dependent (reversal vs continuation)
  - Liquidations: "directional fuel spent" / "opposing positions cleared"
  - Whales: "net exchange withdrawals reduce sell-side supply" (no "bullish accumulation")
  - News: "contrarian context at extremes" (no "institutional accumulation narrative")
- Absent data = neutral, neither penalize nor reward
- Cada factor es weak signal — solo combinaciones importan

**Output JSON de Claude:**
```json
{
    "approved": bool,
    "confidence": float,
    "scores": {
        "setup_quality": int 0-5,
        "market_support": int 0-5,
        "contradiction": int 0-5,
        "data_sufficiency": int 0-5
    },
    "supporting_factors": ["concise factor", ...],
    "contradicting_factors": ["concise factor", ...],
    "adjustments": {"sl_price": float|null, "tp2_price": float|null},
    "warnings": ["warning", ...]
}
```

**User prompt** (por cada evaluación):
- Setup completo: pair, direction, entry, SL, TP1 (breakeven trigger), TP2 (single TP), R:R to TP2, HTF bias labeled as "aligned" or "COUNTER-TREND"
- Confluences etiquetadas: cada una marcada como [SUPPORTING] o [CONTEXT] con descripción factual (sin narrativa)
- Funding rate con interpretación neutral ("directional crowding on long/short side")
- Open interest (snapshot sin tendencia — solo contexto de tamaño de mercado)
- CVD con buy dominance %
- Liquidaciones recientes (long vs short)
- Whale activity: net exchange flow (deposits vs withdrawals, sin labels "bullish/bearish"), individual movements grouped by type
- News sentiment: Fear & Greed Index (score/100 + label) + recent headlines
- Price context (cambio 1h, 4h)

## Archivos implementados

### `ai_service/prompt_builder.py` — Construcción de prompts
- Clase: `PromptBuilder`
- `build_system_prompt()` → system prompt con threshold dinámico de `settings.AI_MIN_CONFIDENCE`
- `build_evaluation_prompt(setup, snapshot, candles_context)` → user prompt con datos concretos
- `_format_confluences(confluences, direction)` → convierte labels internos a descripción factual con tags [SUPPORTING]/[CONTEXT]
- Computa R:R simple (reward to tp2 / risk) en el setup section
- Interpreta funding rate: normal/extreme basado en `FUNDING_EXTREME_THRESHOLD` (0.03%) — neutral language ("directional crowding")
- OI marcado como snapshot-only (sin tendencia)
- Whale section: net exchange flow sin labels "bullish/bearish", solo "net withdrawal" / "net deposit"
- Maneja datos faltantes gracefully ("Not available")
- **HTF position trade note:** Cuando `setup.ob_timeframe` es "4h" o "1h", agrega nota al prompt

### `ai_service/claude_client.py` — Wrapper del API
- Clase: `ClaudeClient`
- `evaluate(system_prompt, user_prompt)` → dict parsedo | None
- Usa SDK oficial de Anthropic (`AsyncAnthropic`)
- `temperature=0.3` para decisiones consistentes
- `max_tokens=500` (respuesta JSON es ~200-400 tokens)
- Timeout configurable (`AI_TIMEOUT_SECONDS`)
- `max_retries=2` con backoff del SDK
- Strip de markdown code fences si Claude wrappea el JSON
- Validación de campos requeridos: `confidence`, `approved`, `scores` (dict)
- Type checks: confidence = numeric, approved = bool, scores = dict
- Logs token usage on every successful call: `Claude tokens: input=X output=Y total=Z`
- Todo error → retorna None (fail-safe)

### `ai_service/service.py` — Facade (AIService)
- Clase: `AIService(data_service=None)`
- `evaluate(setup, snapshot)` → `AIDecision`
- Obtiene candles context del DataService para price change
- Construye `reasoning` desde `supporting_factors` + `contradicting_factors` (formato: "Supporting: X; Y | Against: Z")
- Almacena `scores` dentro de `adjustments["scores"]` (junto con SL/TP adjustments)
- Double check: `approved=True` AND `confidence >= AI_MIN_CONFIDENCE` (0.50)
- Clamp confidence a [0, 1]
- Log incluye scores: `AI APPROVED/REJECTED: pair=X confidence=Y scores=[setup_quality=4 ...]`
- Sin API key → disabled, rechaza todo
- API failure → rechaza con razón clara en logs

### `ai_service/__init__.py`
- Exporta `AIService`

## Configuración (`config/settings.py`)

| Setting | Default | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | API key de Anthropic |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Modelo a usar |
| `AI_MIN_CONFIDENCE` | `0.50` | Confianza mínima para aprobar |
| `AI_TIMEOUT_SECONDS` | `30.0` | Timeout por request |
| `AI_TEMPERATURE` | `0.3` | Temperatura (menor = más consistente) |
| `AI_MAX_TOKENS` | `500` | Tokens máximos de respuesta |
| `FUNDING_EXTREME_THRESHOLD` | `0.0003` | Threshold para funding "extreme" (0.03%) |

## Tests

52 tests en 3 archivos:
- `test_prompt_builder.py` — system prompt (scoring rubric, no narrative doctrine, JSON format), evaluation prompt, datos faltantes, funding extremo, non-exchange whale labels, dynamic threshold, confluence formatting (dynamic patterns, malformed strings)
- `test_claude_client.py` — JSON válido/inválido, code fences, API errors, timeout, rate limit, approved string type validation, scores dict validation
- `test_ai_service.py` — approval/rejection, confidence clamping, double check, API failure, disabled mode, confidence thresholds (0.50 default), reasoning constructed from factors, scores stored in adjustments, data service integration

## Persistence

Every AI decision (approved or rejected) is persisted to PostgreSQL `ai_decisions` table via `main.py:_persist_ai_decision()`. The record includes:
- `pair`, `direction`, `setup_type` — from the TradeSetup being evaluated
- `approved` — boolean result of the decision
- `confidence`, `reasoning` (constructed from factors), `adjustments` (includes scores), `warnings` — from the AIDecision
- `trade_id` — linked to trades table if the trade was ultimately executed (None for rejections)

The dashboard AILog component shows pair, direction badge, and approved/rejected status for each decision.

## Pre-Filter

Antes de llamar a Claude API, `main.py:_pre_filter_for_claude()` ejecuta 3 checks determinísticos que rechazan setups obvios sin gastar tokens. Los checks son conservadores — si los datos no están disponibles, el check se salta (no genera falsos rechazos).

**Nota:** El check de HTF bias conflict fue removido — HTF bias ahora es contexto para Claude, no un hard gate. Esto permite counter-trend setups con estructura LTF clara.

### Check 1: Funding extreme contra dirección
- Long + `funding_rate > FUNDING_EXTREME_THRESHOLD` (0.03%) → rechaza
- Short + `funding_rate < -FUNDING_EXTREME_THRESHOLD` → rechaza
- Si `snapshot.funding` es None → skip

### Check 2: Fear & Greed extreme contra dirección
- Long + `F&G < NEWS_EXTREME_FEAR_THRESHOLD` (15) → rechaza ("Extreme Fear — rejecting long")
- Short + `F&G > NEWS_EXTREME_GREED_THRESHOLD` (85) → rechaza ("Extreme Greed — rejecting short")
- Si `snapshot.news_sentiment` es None → skip

### Check 3: CVD divergencia fuerte contra dirección
- Long + `buy_dominance < 40%` → rechaza
- Short + `buy_dominance > 60%` → rechaza
- Si `snapshot.cvd` es None o volumen total = 0 → skip

**Qué pasa cuando el pre-filter rechaza:**
- Log: `"AI PRE-FILTERED: {reason}"`
- PostgreSQL: `approved=False, confidence=0.0, reasoning="Pre-filter: {reason}"`
- Telegram: "AI PRE-FILTERED" con razón
- Dashboard AILog: aparece como decisión rechazada (approved=False)

## Setup Dedup Cache

`main.py` mantiene un cache de deduplicación para evitar re-enviar el mismo setup a Claude cada 5 minutos (cuando cierra la misma candle LTF):

- Key: `(pair, direction, setup_type, entry_price_rounded)`
- TTL: 1 hora (`_SETUP_DEDUP_TTL_SECONDS = 3600`) — prevents re-sending while limit order is pending
- Si el setup ya fue evaluado dentro del TTL → skip (log debug, return None)
- El cache se actualiza DESPUÉS de la evaluación exitosa de Claude

## Pipeline AI

```
Setup detected
  |
  +-- Dedup cache hit? --> skip (ya evaluado, covers ALL setup types)
  |
  +-- QUICK_SETUP_TYPES? --> synthetic AIDecision(confidence=1.0), skip to Risk
  |
  +-- AI_BYPASS_SETUP_TYPES? --> synthetic AIDecision(confidence=1.0), skip to Risk
  |
  +-- pre-filter (funding, F&G, CVD) --> rechaza sin Claude
  |
  +-- Claude evalúa --> AIDecision (scores + factors)
  |
  +-- approved + confidence >= 0.50? --> Risk Service
```

**Note (2026-03-12):** Currently ALL active setups (setup_a, setup_d_bos, setup_d_choch) hit the first two bypass branches. The Claude evaluation path is not reached.

**AI filter currently bypassed for ALL active setups:**
- **Setup A**: In `AI_BYPASS_SETUP_TYPES` — synthetic AIDecision(confidence=1.0, reasoning="AI bypass (pending recalibration)"). AI v2 had 89.6% approval rate, adding latency without filtering value. Will re-enable when recalibrated.
- **Setup D variants** (setup_d_bos, setup_d_choch): In `QUICK_SETUP_TYPES` — skip AI by design (data-driven quick setups).
- **Setup B, F**: Disabled entirely (not in ENABLED_SETUPS).
- **Net effect**: Zero Claude API calls in the current pipeline. Pre-filter, prompt builder, and Claude client code remain intact for future re-enable.

## FAQ

**¿Por qué temperature 0.3 y no 0?**
Temperature 0 puede hacer que Claude sea demasiado repetitivo con las mismas razones. 0.3 da decisiones consistentes pero permite variación en el razonamiento. En fondos cuantitativos, el modelo no debe ser creativo — debe ser consistente.

**¿Cuánto cuesta por evaluación?**
~1,600 tokens input + ~200 tokens output ≈ $0.008/evaluación con Sonnet ($3/MTok input, $15/MTok output). Con 5-15 setups/semana ≈ $0.16-0.48/mes. El uso exacto se loguea en cada llamada (`Claude tokens: input=X output=Y total=Z`).

**¿Por qué scoring rubric en vez de narrativa?**
La v1 usaba un framework narrativo (ICT/Wyckoff) que permitía a Claude racionalizar aprobaciones con pseudo-lógica. El scoring rubric (v2) fuerza evaluación dimensional explícita — cada dimensión tiene escala 0-5, las reglas de decisión son mecánicas, y el output separa factores en supporting/contradicting en vez de free-form reasoning. Esto permite análisis posterior por confidence buckets y correlación scores vs outcomes.

**¿Por qué no streaming?**
La respuesta completa es ~200-400 tokens. No hay beneficio en recibir token por token. Un `await` simple es suficiente.

**¿Por qué no fallback a otro modelo?**
Si Claude no está disponible, rechazamos. Un fallback a otro modelo necesita otro prompt y testing separado. Con 99.9% uptime de Anthropic, no vale la complejidad.

**¿Qué pasa con los "adjustments"?**
Claude puede sugerir modificar SL/TP. El campo se pasa en `AIDecision.adjustments` (junto con `scores`), pero ni Risk ni Execution leen los ajustes de SL/TP todavía. Planeado para futuro — aplicar ajustes de SL/TP antes de pasar al Execution Service.
