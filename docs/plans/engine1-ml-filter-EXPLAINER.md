# Cómo construí un filtro de IA para mi bot de trading — explicación simple

*Documento de explicación, no un guion. Para entender (y poder contar) qué hicimos, por qué, y cómo funciona. Todo en palabras llanas.*

---

## El problema de fondo

Tengo un bot que detecta un patrón de mercado llamado **engine1** (trend-pullback: el precio tiene una tendencia, hace un retroceso, y entras a favor de la tendencia). El bot lleva meses en "modo shadow" — detecta señales y anota qué hubiera pasado, **pero no mete plata real**. Es un simulador en vivo.

El resultado crudo era decepcionante: **engine1, tomando TODAS sus señales, pierde dinero.** En los datos más recientes y limpios, da pérdida. Si lo hubiera puesto a operar tal cual, habría quemado capital.

Pero había una pista: no todas las señales son iguales. Algunas se ven mucho mejores que otras. La pregunta fue: **¿se puede separar las buenas de las malas ANTES de entrar?**

---

## Qué es el "score" y el "top tercio"

Entrené un modelo de machine learning (un clasificador) cuyo único trabajo es mirar una señal de engine1 y darle una **nota del 0 al 1**: *"qué tan probable es que esta señal sea ganadora"*. Eso es el **score**.

- Score cerca de 1 = el modelo cree que es de las buenas.
- Score cerca de 0 = el modelo cree que es de las malas.

Ahora, ordeno todas las señales por su score, de mayor a menor, y las parto en tres montones iguales (tres "tercios"). El **top tercio** = el 33% de señales con el score más alto. Esas son las que el modelo considera las mejores.

La regla en vivo es **una sola línea, súper simple**: hay un número de corte fijo (≈ **0.847**). Si la señal saca un score **igual o mayor a 0.847**, opero. Si saca menos, la dejo pasar. Nada más. No hay matices, no hay "apostar más si está más seguro". Es un sí/no con un número congelado.

**Por qué tan simple a propósito:** mientras menos perillas tenga el sistema, menos formas hay de auto-engañarse. Un solo número de corte, fijado de antemano y no tocado, es difícil de "ajustar hasta que dé bonito". La simpleza ES la defensa contra el sobreajuste.

---

## Lo que pasó cuando aplicamos el filtro

Con ese filtro, los números cambian por completo:

- Sin filtro (todas las señales): **pierde**.
- Solo el top tercio (score ≥ 0.847): **gana**, y con buen margen.

Y lo más importante: el modelo separa limpio. Las señales que califica bajo son justo donde se concentran las pérdidas. Las que califica alto son donde están casi todas las ganadoras. No es casualidad ni ruido — hay una estructura real que el modelo encontró.

---

## Por qué esperamos "los 30 trades" (la parte clave)

Acá está lo más importante de toda la historia, y es lo que separa el trading serio del trading de ilusión.

**Es muy fácil que un modelo se vea genial mirando el pasado.** Si entreno un modelo con datos viejos y luego lo pruebo con esos MISMOS datos viejos, casi siempre se ve espectacular — porque ya los vio, prácticamente los memorizó. Eso no prueba nada. La pregunta de verdad es: **¿funciona con datos que NUNCA vio?**

Ya nos quemamos antes con esto. Hace semanas tuve otro filtro (el "impulse-gate") que en los datos de entrenamiento se veía increíble — ganancia teórica enorme. Lo llevé a la prueba honesta y **murió**: con datos nuevos apenas empataba. Era una ilusión del pasado. Lo maté.

Para no repetir el error, esta vez hice esto:

1. **Congelé el modelo** en una fecha (23 de junio). A partir de ahí, el modelo queda "sellado" — no aprende nada nuevo.
2. **Dejé que el bot siguiera en shadow** generando señales nuevas, que el modelo congelado **nunca vio al entrenarse**.
3. **Esperé a juntar al menos 30 de esas señales nuevas resueltas.** ¿Por qué 30? Porque con menos de 30 cualquier resultado es ruido puro — podrías tener 5 ganadoras seguidas por pura suerte y creerte millonario. 30 es el mínimo razonable para que el número empiece a significar algo.

Eso es lo que celebró la notificación de Telegram que te llegó: **"ya juntamos los 30 trades nuevos, ya se puede juzgar de verdad"**. No era el bot ganando, era el bot **acumulando suficiente evidencia limpia para decidir**.

(Nota: hay otra alerta distinta, la de "500 trades", que es para re-entrenar el modelo. Esa todavía no llega. Son cosas separadas — no confundirlas.)

---

## El resultado de la prueba honesta

Con esos datos nuevos que el modelo nunca vio:

- Sin filtro: pierde (como siempre).
- **Con el filtro del top tercio: gana.** En datos limpios, nunca vistos.

Esta es la prueba que importa. La misma prueba que mató al filtro anterior, este la **pasó**. Por eso decidimos dar el siguiente paso.

**Importante, lo digo claro:** que tenga "ventaja" (edge) no es lo mismo que "ganar plata seguro". La ventaja es estadística y la muestra todavía es chica (30-some trades, ~90 ganadoras). Pasar la prueba significa "vale la pena arriesgar un poco de plata real para confirmarlo", NO "esto es una máquina de imprimir dinero".

---

## Por qué NO necesita "calibración"

Una pregunta técnica que surgió: ¿hay que "calibrar" el modelo? Calibrar = ajustar el modelo para que cuando diga "0.7" realmente signifique "70% de probabilidad".

La respuesta es **no, y la razón es simple**: yo no uso el número como probabilidad. No voy a apostar más cuando el modelo está "más seguro". Solo lo uso para **ordenar** las señales y quedarme con el tercio de arriba. Para ordenar, solo importa que las buenas tengan número más alto que las malas — no importa si el número exacto está "bien calibrado". Es como ordenar alumnos por nota: para saber quiénes son el mejor tercio, no necesito que las notas estén en una escala perfecta, solo que el mejor tenga más que el peor.

Así que saltamos la calibración. Una cosa menos que puede salir mal.

---

## Cuánto dinero vamos a usar, y por qué tan poco

Tengo unos **$86** en la cuenta. Podría mandar $100 más. **La decisión: no mandar más todavía.**

El motivo: la primera fase en vivo **no es para ganar dinero, es para probar la plomería.** Quiero confirmar que en la vida real pasa lo mismo que en el simulador:

- ¿Mi orden límite realmente se llena, o el precio se va sin mí?
- ¿Cuánto "slippage" (deslizamiento de precio) hay de verdad?
- ¿Los breakeven y stops se comportan como en shadow?

Para responder eso, **no necesito mucha plata** — necesito el tamaño mínimo que permita el exchange y unos 15-20 trades reales. Si meto más capital ahora, solo aumento mi exposición a algo que todavía no confirmé en vivo. Mandar el dinero extra viene **después**, cuando ya sepa que lo real coincide con el simulador. Primero plomería barata, luego financiar.

---

## El criterio de salida — cuándo apagarlo (la versión racional)

Mi primer instinto fue "si pierde $20, lo apago". Pero eso es arbitrario y peligroso: **un modelo sano TAMBIÉN tiene rachas malas**. Si lo apago a la primera mala racha, mato algo que igual se iba a recuperar.

Entonces lo hice con datos. Simulé 20,000 veces cómo se vería la peor caída ("drawdown") de una versión **sana** de este modelo, jugando 30 trades. Resultado:

- Una caída normal anda por 3-4 unidades de riesgo.
- El 99% de las veces, ni la peor mala racha de un modelo sano pasa de **~10 unidades de riesgo** ("10R", donde 1R = lo que arriesgo por trade).

Entonces el criterio racional es:

> **Lo dejo perder hasta 10R. Eso es lo máximo que una versión SANA de este modelo perdería por mala suerte. Si cae más que eso, ya no es mala suerte — la ventaja se rompió — y ahí sí apago.**

Más dos señales de apoyo:
- **7 pérdidas seguidas** — eso le pasa a un modelo sano menos del 1% del tiempo. Si ocurre, es bandera roja.
- **El factor de ganancia (PF) de los últimos 20 trades cae bajo 1.2** — confirma que la ventaja dejó de transferirse.

Cualquiera de esas tres = apago, vuelvo a shadow, y re-analizo. Por debajo de 10R = aguanto, es ruido normal.

Así no mato el modelo por pánico, pero tampoco me quedo aferrado mientras se desangra. El límite está puesto donde la estadística dice "esto ya no es normal".

---

## Resumen en una frase

Entrené un modelo que le pone nota a cada señal de engine1, **probé que esa nota sirve incluso con datos que el modelo nunca vio** (la prueba que mató a mi intento anterior), y ahora voy a arriesgar muy poco dinero real solo para confirmar que lo de la vida real coincide con el simulador — con un punto de apagado calculado, no inventado.
