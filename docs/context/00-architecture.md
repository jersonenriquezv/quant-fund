# Arquitectura del Sistema
> Última actualización: 2026-03-03
> Estado: en progreso

## Qué hace (para entenderlo rápido)
El sistema es un bot de trading que funciona como una línea de ensamblaje. Los datos entran por un lado, pasan por 5 filtros en orden, y si todos dicen "sí", se ejecuta el trade. Si cualquier filtro dice "no", el trade se descarta.

## Por qué existe
Sin esta arquitectura, tendríamos un solo programa gigante donde todo está mezclado. Si algo falla, todo falla. Con 5 servicios separados, si el AI Service se cae, el Risk Service sigue protegiendo el capital. Cada pieza hace una sola cosa bien.

## Diagrama del sistema

```
                    ┌─────────────┐
                    │   OKX API   │ ← Exchange de crypto
                    │  Etherscan  │ ← Datos on-chain ETH
                    │  Coinglass  │ ← Liquidaciones
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
```

## Cómo se comunican los servicios
1. Data Service recoge datos de OKX, Etherscan
2. Cuando hay una vela nueva (cada 5m/15m), manda los datos al Strategy Service
3. Strategy Service analiza los datos buscando patrones SMC
4. Si encuentra un setup completo (Setup A o B con confluencia), lo manda al AI Service
5. AI Service le pregunta a Claude: "¿el contexto apoya este trade?"
6. Si Claude dice sí (confianza ≥ 0.60), el setup pasa al Risk Service
7. Risk Service verifica TODOS los guardrails y calcula el position size
8. Si todo pasa, Execution Service manda la orden a OKX

**Regla clave:** Si CUALQUIER servicio dice NO, el trade se descarta. No hay "pero" ni "tal vez".

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

## Cambios recientes
- 2026-03-03: Documento inicial creado con arquitectura de 5 capas.