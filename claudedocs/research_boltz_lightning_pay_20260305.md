# Research: Implementar `lw_pay_invoice` — Pagar Lightning Invoices desde Liquid Bitcoin via Boltz

**Fecha:** 2026-03-05
**Autor:** Claude (investigación para agente de implementación)
**Objetivo:** Recopilar todo el material necesario para implementar una herramienta MCP que permita pagar Lightning invoices usando fondos en Liquid Bitcoin (L-BTC), vía Boltz Exchange.

---

## Resumen Ejecutivo

Boltz Exchange permite hacer **submarine swaps** de L-BTC a Lightning (BTC) de forma trustless. El flujo es: el usuario envía L-BTC a una dirección de lockup → Boltz paga el Lightning invoice → Boltz reclama el L-BTC.

La librería Python oficial (`boltz_client`) **solo funciona en Linux x86_64** (archived, sin wheels para macOS/Windows). La solución recomendada es una **implementación directa contra el REST API de Boltz**, usando las librerías Python del proyecto existente (LWK para construir transacciones Liquid) más librerías de criptografía multiplataforma.

El proyecto ya tiene `lwk` como dependencia, que puede usarse para enviar L-BTC a la dirección de lockup de Boltz. La parte compleja es el **MuSig2 cooperativo** para el claim de vuelta.

---

## 1. Cómo Funciona el Submarine Swap L-BTC → Lightning

### Flujo completo

```
Usuario (aqua-mcp)                    Boltz API                    Lightning
       │                                  │                              │
       │  POST /v2/swap/submarine         │                              │
       │  {invoice, from:"L-BTC",        │                              │
       │   to:"BTC", refundPublicKey}     │                              │
       │ ─────────────────────────────►  │                              │
       │  ◄─────────────────────────────  │                              │
       │  {id, address (lockup),          │                              │
       │   claimPublicKey, swapTree,      │                              │
       │   timeoutBlockHeight}            │                              │
       │                                  │                              │
       │  Enviar L-BTC a `address`        │                              │
       │  (usando lw_send internamente)   │                              │
       │ ─────────────────────────────►  │                              │
       │                                  │                              │
       │  WebSocket: status updates       │                              │
       │  "transaction.mempool"           │                              │
       │  "transaction.confirmed"         │                              │
       │  "invoice.pending"               │                              │
       │                                  │──── paga invoice ──────────► │
       │                                  │                              │
       │  "transaction.claim.pending"     │                              │
       │                                  │                              │
       │  GET /v2/swap/submarine/{id}/claim                              │
       │ ─────────────────────────────►  │                              │
       │  ◄─────────────────────────────  │                              │
       │  {preimage, transactionHash,     │                              │
       │   pubNonce}                      │                              │
       │                                  │                              │
       │  VERIFICAR: SHA256(preimage)     │                              │
       │           == invoice.paymentHash │                              │
       │                                  │                              │
       │  POST /v2/swap/submarine/{id}/claim                             │
       │  {pubNonce, partialSignature}    │                              │
       │ ─────────────────────────────►  │                              │
       │                                  │                              │
       │  "transaction.claimed" ✅        │                              │
```

### Estados del swap

| Estado | Descripción | Acción requerida |
|--------|-------------|-----------------|
| `swap.created` | Swap iniciado | Enviar L-BTC al `address` |
| `transaction.mempool` | L-BTC detectado en mempool | Esperar |
| `transaction.confirmed` | L-BTC confirmado | Esperar |
| `invoice.pending` | Boltz pagando el invoice | Esperar |
| `invoice.paid` | Invoice pagado | Esperar claim |
| `transaction.claim.pending` | **Necesita firma MuSig2** | Firmar cooperativamente |
| `transaction.claimed` | **Swap completado** ✅ | — |
| `invoice.failedToPay` | Boltz no pudo pagar | Refund |
| `swap.expired` | Timeout | Refund si se enviaron fondos |

---

## 2. API Endpoints

**Base URL mainnet:** `https://api.boltz.exchange`
**WebSocket mainnet:** `wss://api.boltz.exchange/v2/ws`
**Swagger:** `https://api.boltz.exchange/swagger`

### Par disponible

Confirmado via `/v2/swap/submarine` (GET):

```json
{
  "L-BTC/BTC": {
    "rate": 1.0,
    "fees": {
      "percentage": 0.1,
      "minerFees": 19
    },
    "limits": {
      "maximal": 25000000,
      "minimal": 1000,
      "maximalZeroConf": 500000
    }
  }
}
```

**Fee:** 0.1% + 19 sats de miner fee (muy barato gracias a Liquid).
**Límites:** 1,000 - 25,000,000 sats. Zero-conf hasta 500,000 sats.

### Endpoints clave

```
POST /v2/swap/submarine          # Crear submarine swap
GET  /v2/swap/submarine/{id}     # Estado del swap
GET  /v2/swap/submarine/{id}/claim   # Obtener datos para claim
POST /v2/swap/submarine/{id}/claim   # Enviar firma parcial (MuSig2)
```

### Crear swap

```http
POST https://api.boltz.exchange/v2/swap/submarine
Content-Type: application/json

{
  "invoice": "lnbc...",
  "from": "L-BTC",
  "to": "BTC",
  "refundPublicKey": "02abc123..."
}
```

**Respuesta:**
```json
{
  "id": "abc123",
  "address": "lq1qq...",
  "claimPublicKey": "03def456...",
  "swapTree": {
    "claimLeaf": { "version": 192, "output": "..." },
    "refundLeaf": { "version": 192, "output": "..." }
  },
  "timeoutBlockHeight": 2500000
}
```

### Claim (POST)

```http
POST https://api.boltz.exchange/v2/swap/submarine/{id}/claim
Content-Type: application/json

{
  "pubNonce": "02...",
  "partialSignature": "..."
}
```

---

## 3. Operaciones Criptográficas Requeridas

Esta es la parte más compleja. El swap usa **MuSig2 con Taproot** (BIP-340/341).

### Operaciones del lado del cliente

1. **Generar keypair efímero** (refund key)
   - Genera una clave privada aleatoria secp256k1
   - Extrae la public key (33 bytes, comprimida)
   - Esta public key se envía como `refundPublicKey` al crear el swap

2. **Recibir y parsear el `swapTree`**
   - El swapTree contiene los scripts Taproot (claim leaf + refund leaf)
   - Necesario para construir la dirección de lockup y la firma

3. **MuSig2 para el claim cooperativo**
   - Crear instancia MuSig2 con las keys de Boltz y del usuario
   - Hacer Taproot key tweak con el taptree del swap
   - Generar nonce público propio
   - Recibir el nonce de Boltz desde el endpoint `/claim`
   - Agregar nonces y crear sesión de firma
   - Firmar el hash de transacción con `signPartial()`
   - Enviar nonce público + firma parcial a Boltz
   - Boltz agrega las firmas y broadcast la transacción

4. **Verificar preimage**
   - `SHA256(preimage_from_boltz)` debe ser igual al `paymentHash` del invoice
   - **Seguridad crítica:** sin esta verificación, Boltz podría no haber pagado

### Librerías Python disponibles

| Librería | Plataforma | MuSig2 | Taproot | Disponibilidad |
|----------|-----------|--------|---------|----------------|
| `coincurve` (21.0.0) | Linux/macOS/Win | ❌ | Schnorr ✅ | ✅ Multiplataforma |
| `secp256k1-zkp` | Linux-focused | ✅ | ✅ | ⚠️ Limitada |
| `boltz_client` (Rust) | Linux x86_64 only | ✅ | ✅ | ❌ No macOS/Win |
| `python-bitcoinlib` | Multiplataforma | ❌ | Parcial | ✅ |

**Conclusión:** No existe una librería Python multiplataforma con MuSig2 completo. Las opciones son:

1. **Usar el endpoint de firma cooperativa simplificado** — Boltz ofrece un modo donde el claim puede hacerse sin MuSig2 si el invoice ya fue pagado y solo se necesita broadcast
2. **Subprocess a `boltzcli`** — Llamar al CLI de Boltz (solo Linux + Docker)
3. **Implementar MuSig2 manualmente** con `coincurve` (complejo, arriesgado)
4. **Usar `boltz_client` condicionalmente** en Linux + fallback de error en macOS

---

## 4. Análisis del boltz-client CLI

El **boltz-client** (`boltzd` + `boltzcli`) es un daemon Go que:
- Se conecta a un nodo Lightning (CLN o LND)
- Gestiona swaps automáticamente
- Expone gRPC + REST

**Para aqua-mcp NO es adecuado** porque:
- Solo funciona en Linux (amd64/arm64) y Docker
- Requiere un nodo Lightning propio (CLN/LND)
- Es un daemon persistente, no una librería
- Está diseñado para rebalanceo de canales, no para pagos one-shot

---

## 5. Análisis de la librería `boltz_client` (PyPI)

**Fuente:** `SatoshiPortal/boltz-rust` con bindings Python via PyO3
**Versión actual:** 0.3.0.post1

### Compatibilidad de plataformas

| Plataforma | Wheel disponible |
|-----------|-----------------|
| Linux x86_64 (glibc ≥2.34) | ✅ `manylinux_2_34_x86_64` |
| Linux ARM64 | ❌ |
| macOS (cualquier arquitectura) | ❌ |
| Windows | ❌ |

**Conclusión: Solo funciona en Linux x86_64.** Aqua-mcp corre en macOS del desarrollador, esto es un blocker.

Además: La librería fue **archivada en agosto 2025** (read-only). No tiene mantenimiento activo.

El proyecto aqua-mcp requiere Python ≥ 3.13, y boltz_client requiere Python ≥ 3.10 — compatible en versión, pero sin wheel para macOS.

---

## 6. Estrategia Recomendada para la Implementación

### Opción A: REST API Directo (Recomendada) ✅

Implementar todo el flujo directamente contra la API REST de Boltz usando Python puro + `httpx` (async) + `websockets`.

**Para las operaciones criptográficas:**
- Usar `coincurve` para generación de keypairs y firma Schnorr
- Para MuSig2: investigar si `lwk` (ya instalado) expone utilidades MuSig o si `bdkpython` puede ayudar
- **Alternativa pragmática para MuSig2:** Implementar el protocolo de firma MuSig2 manualmente usando operaciones básicas de secp256k1 que `coincurve` provee (add_tweak, multiply, etc.)

**Para enviar L-BTC al lockup:**
- Usar la función existente `lw_send` internamente (ya implementada en el proyecto)

**Ventajas:**
- Multiplataforma (Linux, macOS, Windows)
- Sin dependencias extra de Rust
- Control total sobre el flujo
- Puede ser async (ya el servidor MCP es async)

**Desventajas:**
- Necesita implementar MuSig2 (complejo)
- Más código a mantener

### Opción B: boltz_client con fallback de error ⚠️

Solo disponible en Linux. Elevar un error claro en macOS/Windows.

```python
try:
    from boltz_client import BoltzClient
    BOLTZ_AVAILABLE = True
except ImportError:
    BOLTZ_AVAILABLE = False

def lbtc_pay_invoice(...):
    if not BOLTZ_AVAILABLE:
        raise RuntimeError(
            "boltz_client not available on this platform. "
            "Install on Linux or use Docker."
        )
```

**Para el entorno de desarrollo (macOS):** Bloqueante.

### Opción C: Implementación Híbrida (Recomendada Pragmática) 🔥

1. Implementar el **flujo completo en REST API** sin MuSig2 en un primer paso
2. Para el **claim cooperativo**: Boltz tiene un modo donde si no se envía firma, ellos igual pueden reclamar via script path (script path spend con la clave de Boltz después del timeout)
3. Esto no es ideal desde el punto de vista de privacidad, pero es funcional
4. Agregar MuSig2 en una segunda iteración cuando se haya resuelto la librería

**IMPORTANTE:** El claim cooperativo es **mejor para el usuario** porque:
- Script path spend (sin cooperación) es visible on-chain y más caro
- Key path spend (MuSig2) es más privado y barato

---

## 7. Plan de Implementación Detallado

### Nueva herramienta: `lbtc_pay_invoice`

**Parámetros:**
- `wallet_name: str` (opcional, usa el default)
- `invoice: str` — Lightning invoice BOLT11
- `passphrase: str` (opcional, para wallets con passphrase)

**Flujo de implementación:**

```python
async def lbtc_pay_invoice(wallet_name, invoice, passphrase=None):
    # 1. Parsear invoice → extraer amount_sats y payment_hash
    # 2. Verificar balance suficiente de L-BTC (balance + fees)
    # 3. Generar keypair efímero (refund key)
    # 4. POST /v2/swap/submarine → obtener {id, address, claimPublicKey, swapTree, timeoutBlockHeight}
    # 5. Calcular monto total a enviar (amount + fees)
    # 6. Confirmar con usuario (opcional o directo)
    # 7. Enviar L-BTC a `address` usando lw_send interno
    # 8. Suscribirse al WebSocket y esperar estados
    # 9. Cuando "transaction.claim.pending":
    #    a. GET /v2/swap/submarine/{id}/claim
    #    b. Verificar SHA256(preimage) == payment_hash
    #    c. Firmar MuSig2 cooperativamente
    #    d. POST /v2/swap/submarine/{id}/claim
    # 10. Esperar "transaction.claimed"
    # 11. Retornar {txid, amount_paid, preimage, fees}
```

### Módulo nuevo: `src/aqua_mcp/boltz.py`

Responsabilidades:
- `BoltzClient` clase con métodos async
- `parse_invoice(invoice: str) -> dict` — extraer amount y payment_hash
- `create_submarine_swap(invoice, refund_pubkey) -> dict`
- `get_claim_details(swap_id) -> dict`
- `submit_claim_signature(swap_id, pub_nonce, partial_sig) -> dict`
- `watch_swap_status(swap_id) -> AsyncIterator[str]` — WebSocket listener
- `generate_refund_keypair() -> tuple[bytes, bytes]` — privkey, pubkey
- `verify_preimage(preimage_hex: str, expected_hash: str) -> bool`
- `musig2_sign(...)` — la parte compleja

### Dependencias nuevas a agregar

```toml
# pyproject.toml
dependencies = [
    ...
    "httpx>=0.27.0",         # HTTP client async
    "websockets>=13.0",      # WebSocket para status updates
    "coincurve>=21.0.0",     # secp256k1 multiplataforma
]
```

Para invoice parsing: `bolt11` o implementación manual (invoice es un bech32 con campos TLV).

### Manejo del MuSig2

**Opción pragmática para v1:** Usar `coincurve` para operaciones básicas e implementar MuSig2 simplificado:

```python
import coincurve
import secrets
import hashlib

def generate_refund_keypair():
    privkey = secrets.token_bytes(32)
    pubkey = coincurve.PublicKey.from_secret(privkey).format(compressed=True)
    return privkey, pubkey

def musig2_sign(privkey: bytes, server_pubkey_hex: str,
                server_nonce_hex: str, tx_hash: bytes) -> tuple[str, str]:
    # Implementación MuSig2 BIP-327 simplificada
    # Requiere investigación adicional o usar una implementación existente
    ...
```

**Alternativa investigar:** Si `lwk` (LWK Python bindings de Blockstream) expone funcionalidad MuSig2 ya que Liquid usa el mismo secp256k1-zkp internamente.

---

## 8. Consideraciones de Seguridad

1. **Verificar preimage** antes de cualquier claim — crítico para la atomicidad del swap
2. **Guardar refund key** en caso de que el swap falle (para poder hacer refund manual)
3. **Timeout handling** — si `timeoutBlockHeight` se acerca, iniciar refund
4. **No guardar** el invoice en texto plano en logs
5. **Monto mínimo** del invoice: 1,000 sats
6. **Monto máximo**: 25,000,000 sats

---

## 9. Manejo del Refund

Si el swap falla (`invoice.failedToPay`, `swap.expired`):
- El usuario puede recuperar los fondos via **script path spend** usando la `refundPublicKey`
- Requiere esperar al `timeoutBlockHeight`
- Boltz puede asistir en la construcción del refund tx

Para v1: documentar que el usuario debe guardar el `swap_id` y la `refund_private_key` para refunds manuales si algo falla.

---

## 10. Recursos y Referencias

### Documentación oficial
- [Boltz API v2 Docs](https://api.docs.boltz.exchange/api-v2.html)
- [Boltz Client Docs](https://client.docs.boltz.exchange/)
- [Boltz Swap Lifecycle](https://api.docs.boltz.exchange/lifecycle.html)
- [Boltz Swagger/OpenAPI](https://api.boltz.exchange/swagger)

### Repositorios relevantes
- [boltz-rust (SatoshiPortal)](https://github.com/SatoshiPortal/boltz-rust) — Rust crate con Python bindings (Linux only)
- [boltz-python (archivado)](https://github.com/BoltzExchange/boltz-python) — Cliente Python original (archivado Aug 2025)
- [boltz-client-python README](https://github.com/BoltzExchange/boltz-client-python/blob/main/README.md) — Documentación del cliente Python
- [boltz-backend API docs](https://github.com/BoltzExchange/boltz-backend/blob/master/docs/api-v2.md) — Docs técnicas del backend

### Librerías Python útiles
- [coincurve (PyPI)](https://pypi.org/project/coincurve/) — secp256k1, multiplataforma ✅
- [boltz_client (PyPI)](https://pypi.org/project/boltz_client/) — Rust bindings, Linux only ⚠️
- [Breez SDK Liquid](https://github.com/breez/breez-sdk-liquid) — SDK completo, Python support

### MuSig2
- [BIP-327 MuSig2](https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki)
- [secp256k1-zkp MuSig](https://github.com/BlockstreamResearch/secp256k1-zkp/blob/master/include/secp256k1_musig.h)
- [musig2-py experimental](https://github.com/meshcollider/musig2-py) — implementación Python experimental

---

## 11. Resumen Final para el Agente de Implementación

### Qué construir
Una nueva herramienta MCP `lbtc_pay_invoice` en `src/aqua_mcp/boltz.py` que:
1. Recibe un Lightning invoice BOLT11 y un wallet_name
2. Crea un submarine swap con Boltz (L-BTC → BTC Lightning)
3. Envía automáticamente el L-BTC al lockup address usando `lw_send`
4. Monitorea el estado via WebSocket
5. Ejecuta el claim cooperativo (MuSig2) cuando está listo
6. Retorna el resultado con txid y preimage

### Desafío principal
El **claim MuSig2 cooperativo** requiere secp256k1-zkp con soporte MuSig2. Las opciones:
- Investigar si `lwk` ya instalado expone esto internamente
- Usar `coincurve` + implementar MuSig2 desde BIP-327
- Limitar a Linux con error claro en otras plataformas

### Respuesta sobre la librería boltz_client
**Solo funciona en Linux x86_64.** No hay wheels para macOS ni Windows. Está archivada desde agosto 2025. No es la solución recomendada para aqua-mcp que corre en macOS para desarrollo.

### Pares disponibles (confirmado via API)
- `L-BTC/BTC`: 0.1% fee + 19 sats miner fee, límites 1,000–25,000,000 sats

### Dependencias a agregar
- `httpx` — cliente HTTP async
- `websockets` — WebSocket
- `coincurve` — secp256k1 multiplataforma

### Herramienta en server.py
Registrar como `lbtc_pay_invoice` con parámetros: `wallet_name` (opcional), `invoice` (requerido), `passphrase` (opcional).
