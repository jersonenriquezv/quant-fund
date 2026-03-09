# Signal Mode — Semi-Manual Trading

**Status:** Pendiente
**Prioridad:** Media
**Esfuerzo:** ~2-3 horas

## What

Modo donde el bot detecta setups pero NO ejecuta — solo manda señales por Telegram con entry/SL/TP. El usuario abre manualmente y el bot monitorea la posición.

## Why

- Bot lleva pocos días live, capital es $108
- Permite validar calidad de señales sin arriesgar ejecución automática
- Fase de confianza: ver que los setups son buenos antes de soltar el control
- **Debe ser temporal** (2 semanas max), no modo permanente

## Current State

Ya existe (~80%):
- `notify_ai_decision()` manda cada setup aprobado por Telegram (pero sin entry/SL/TP exactos)
- Position adoption detecta posiciones manuales al reiniciar y las monitorea
- Monitor ya mueve SL a breakeven, manda alertas de cierre

Falta:
- Flag para no ejecutar después de Risk approve
- Notificación enriquecida con niveles exactos
- Position adoption en tiempo real (no solo al restart)

## Steps

1. **Agregar `SIGNAL_ONLY` flag** → `config/settings.py` → Done when: `SIGNAL_ONLY=true` deshabilita ejecución automática

2. **Notificación de señal enriquecida** → `shared/notifier.py` → Done when: `notify_signal()` manda por Telegram:
   - Par, dirección, setup type
   - Entry price exacto
   - SL price + distancia %
   - TP1/TP2/TP3 prices
   - R:R ratio
   - Confluencias detectadas
   - Reasoning de Claude
   - Confianza AI

3. **Condicional en pipeline** → `main.py` → Done when: si `SIGNAL_ONLY=true`, después de Risk approve llama `notify_signal()` en vez de `execution_service.execute()`

4. **Position adoption en tiempo real** → `execution_service/monitor.py` → Done when: polling cada 30s detecta posiciones nuevas que el usuario abre manualmente en OKX (no solo al restart)

5. **Dashboard indicator** → `dashboard/` → Done when: UI muestra si el bot está en signal mode o auto mode

## Risks

| Riesgo | Mitigación |
|--------|------------|
| Usuario se queda en signal mode forever | Recordatorio en Telegram después de 2 semanas: "ya deberías pasar a auto" |
| Latencia: señal llega tarde, precio se movió | Incluir timestamp en señal + "válido por X minutos" |
| Position adoption no detecta posición manual | Fallback: comando manual en dashboard para registrar posición |
| Usuario abre trade con SL/TP diferente al sugerido | Bot monitorea posición real del exchange, no los niveles sugeridos |

## Out of Scope

- No se cambia la lógica de detección de setups (strategy service intacto)
- No se cambia el AI filter ni risk service
- No se implementa ejecución parcial (señal + bot pone SL/TP)
- No se agregan nuevos canales de notificación (solo Telegram)
