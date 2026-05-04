Use this skill when the user pastes a technical bot report, log dump, diagnostic output, backtest summary, shadow report, or trading-system status and wants a short, actionable read-out in Spanish.

## Goal

Translate noisy technical artifacts into a compact Spanish ops brief. Minimize tokens. Never bury critical risk. Never over-conclude edge from small samples.

## Style

- Write in Spanish.
- Short, but do not omit important risks.
- No giant logs. No huge tables. Quote at most one short line of raw output if essential.
- Explain technical terms only when the user is unlikely to know them.
- Separate facts (observed) from hypotheses (inferred) from recommendations.
- Numbers right-rounded. No false precision.
- Do not infer edge from <30 paired outcomes. Say so when sample is small.

## Required output format

Always emit exactly these sections, in this order. Omit no section except `Detalle técnico corto`, which is optional. Do not add new sections.

```
Conclusión:
- 1-2 líneas.

Estado:
- SANO / SOSPECHOSO / ROTO

Impacto:
- Dinero real: Sí/No — razón breve.
- Experimento: Sí/No — razón breve.

Evidencia clave:
- Máx 3 bullets con los números/hechos más importantes.

Acción recomendada:
- Máx 3 bullets.

No tocar:
- Máx 3 bullets.

Detalle técnico corto:
- Opcional. Máx 5 bullets. Solo si aporta.
```

## Decision rules — Estado

- **SANO**: pipeline funcionando, sin errores críticos, recolección de datos válida, ningún invariante roto.
- **SOSPECHOSO**: warnings, drift parcial, sample chico, datos parciales o un subsistema degradado, pero el bot sigue operando y la data sigue siendo utilizable.
- **ROTO**: bot unhealthy, errores repetidos de DB/schema, leakage real entre pares, pendings stale >24h, shadow monitor sin resolver outcomes, corrupción de datos, o cualquier condición que invalide la data que se está recolectando.

Si hay duda entre dos niveles, elegir el más conservador (peor) y explicarlo en una línea.

## Distinciones obligatorias para reportes de trading/estrategia

Antes de concluir, separar mentalmente:

1. **Pipeline health** — ¿el bot detecta, persiste, resuelve outcomes correctamente?
2. **Strategy performance** — ¿la estrategia tiene edge? Solo válido con sample suficiente.
3. **Benchmark/reporting artifact** — ¿el número raro viene de cómo se mide (dedup, filtros, orphans, ventanas), no de la estrategia?
4. **Sample size limitation** — ¿el N permite cualquier conclusión? Si N<30 paired, decirlo explícito.

Un mismo reporte puede ser SANO en (1), SOSPECHOSO en (3) y mudo en (2). Reflejarlo así.

## Reglas duras

- Nunca recomendar cambiar lógica de trading (thresholds, geometría, sizing, gating) salvo evidencia fuerte (sample grande + efecto consistente + causa identificada).
- Con sample chico, default a `esperar / monitorear / investigar`. No a `ajustar`.
- Si un número parece anómalo, primero sospechar del reporte/medición antes que de la estrategia.
- Si el reporte mezcla varios pares, varios setups o varias ventanas, decir explícitamente cuál se está evaluando en cada bullet.
- No inventar números que no estén en el input. Si falta data clave, decir "no presente en el reporte" y mover ese item a `Acción recomendada` (pedir el dato).
