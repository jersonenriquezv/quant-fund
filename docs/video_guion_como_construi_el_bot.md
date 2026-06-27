# Video: Cómo construí mi bot de trading

Estructura para seguir. Cada sección: qué explicar + qué código mostrar + por qué se hizo así.

---

## 1. Inicio (hook)
- Qué explicar: muestra el resultado primero (dashboard funcionando).
- Código: ninguno todavía.

## 2. La idea principal
- Qué explicar: bot detecta → riesgo aprueba → ejecución ejecuta. Una capa dice NO = no hay trade.
- Código: `CLAUDE.md`
- Por qué así: capas separadas = un bug no vacía la cuenta.

## 3. Qué es SMC
- Qué explicar: 3 velas dejan un hueco (FVG), ahí entran los grandes.
- Código: `strategy_service/fvg.py` → `FVGDetector` (clase, l.34), `_detect_fvgs` (l.95)
- Por qué así: regla fija, determinística — no IA adivinando.

## 4. Las 5 capas (lo central)
- Qué explicar: una vela dispara 5 pasos en orden — Datos, Estrategia, IA, Riesgo, Ejecución.
- Código: `main.py` → `_process_pipeline_setup` (l.235, las 5 capas en secuencia)
- Por qué así: todo en un proceso, sin colas. Simple = menos se rompe.

## 5. Capa de datos
- Qué explicar: vela de OKX → Redis (rápido) → PostgreSQL (historial).
- Código: `data_service/service.py`
- Por qué así: Redis para velocidad, Postgres para entrenar ML.

## 6. Capa de riesgo
- Qué explicar: 9 chequeos en fila, el primero que falla bloquea. SL obligatorio.
- Código: `risk_service/service.py` → `check` (l.45), guardrails fail-fast (l.69-91)
- Por qué así: fail-fast. Aquí el bot dice NO.

## 7. El switch on/off
- Qué explicar: lista vacía = modo prueba. Llenarla = en vivo.
- Código: `config/settings.py` → `ENABLED_SETUPS` (l.308), `OKX_SANDBOX` (l.32)
- Por qué así: apagar todo desde un solo lugar.

## 8. Modo sombra (tu punto fuerte)
- Qué explicar: cada señal = trade de papel, cero dinero real, solo junta datos.
- Código: `execution_service/shadow_monitor.py` → `ShadowMonitor` (l.89) · `main.py` → `add_shadow` (l.334)
- Por qué así: ventaja ≠ ganar. Probar ≠ jugarse la plata. Credibilidad.

## 9. La IA que apagué
- Qué explicar: probé Claude para filtrar trades, no ayudó (aprobaba 90%), lo apagué.
- Código: `main.py` → síntesis `AIDecision` (l.392, confianza forzada = bypass)
- Por qué así: si no mejora los números, fuera.

## 10. Por qué junto datos (ML)
- Qué explicar: cada señal guarda 40 datos antes de saber el resultado.
- Código: `shared/ml_features.py` → `extract_setup_features` (l.23) · `settings.py` → `ML_FEATURE_VERSION=18` (l.1138)
- Por qué así: capturar antes del resultado = sin trampa (no data leakage).

## 11. El dashboard
- Qué explicar: monitoreo desde el celular — ganancias, win rate, curva.
- Código: `dashboard/web/src/app/shadow/page.tsx`
- Por qué así: si no lo ves, no lo manejas.

## 12. Cierre
- Qué explicar: bugs que costaron días, por qué sigo en prueba, qué sigue. CTA.
- Código: ninguno.

---

## Stack (mencionar rápido)
Python, OKX, PostgreSQL, Redis, Docker, dashboard web, servidor propio 24/7.

## Orden narrativo
Resultado → problema → concepto → arquitectura → honestidad → lecciones → futuro.
La honestidad va en el MEDIO, es tu gancho.
