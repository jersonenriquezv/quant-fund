# Issues Pendientes — To Fix

## Activos

### 1. Backfill 4h/1h falla en OKX sandbox
- **Error:** `timeframe unit H is not supported`
- **Impacto:** Strategy Service no puede determinar HTF bias (tendencia en 4h/1h), por lo que no genera setups. El bot recibe candles 5m/15m pero las descarta con "No HTF bias — skipping".
- **Causa:** OKX sandbox (demo) no soporta timeframes con "H" mayuscula. ccxt envía "4H" pero sandbox espera otro formato.
- **Workaround:** En live deberia funcionar. Alternativamente, investigar si ccxt tiene un mapping para sandbox.
- **Severidad:** Alta (bloquea toda generacion de setups en demo)

### 2. Whale wallets vacias
- **Log:** `No whale wallets configured — add addresses to settings.WHALE_WALLETS`
- **Impacto:** Etherscan polling deshabilitado. El AI Service no recibe datos de whale movements.
- **Fix:** Agregar direcciones de whales conocidos en `config/settings.py` → `WHALE_WALLETS`
- **Severidad:** Baja (el bot funciona sin esto, solo pierde una senal)

### 3. Etherscan "no API key" a pesar de estar en .env
- **Log:** `Etherscan client: no API key or wallets configured, polling disabled`
- **Causa:** El check requiere AMBOS: API key Y wallets. Si wallets esta vacio, no arranca aunque la key este.
- **Fix:** Se resuelve con el punto 2 (agregar wallets)
- **Severidad:** Baja

## Resueltos
- ~~PostgreSQL password authentication failed~~ — Resuelto: `$` en password se interpretaba como variable de shell en Docker Compose. Cambiado a password sin caracteres especiales.
