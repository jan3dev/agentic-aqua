# Plan de Tests: `lbtc_pay_lightning_invoice` + `lbtc_swap_lightning_status`

**Fecha:** 2026-03-05
**Status:** Aprobado para fase RED
**Basado en:** `design_boltz_lbtc_pay_invoice.md`

---

## Resumen del Feature

Dos tools nuevas que permiten pagar Lightning invoices usando L-BTC via Boltz submarine swaps:

1. **`lbtc_pay_lightning_invoice`** -- Crea el swap, envia L-BTC al lockup y retorna `swap_id` + estado inicial
2. **`lbtc_swap_lightning_status`** -- Consulta el estado del swap (el AI assistant llama esto repetidamente)

Nuevo modulo: `src/aqua_mcp/boltz.py` (BoltzClient, SwapInfo, generate_keypair, verify_preimage)
Cambios en: `storage.py`, `tools.py`, `server.py`, `pyproject.toml`

---

## Capa 1: Unit -- Utilidades criptograficas (`boltz.py`)

**Archivo:** `tests/test_boltz.py`

- [ ] **1.1** `test_generate_keypair_returns_hex_strings`
  - **Valida:** `generate_keypair()` retorna una tupla `(privkey_hex, pubkey_hex)` donde ambos son strings hex validos
  - **Setup/mocks:** Ninguno (usa `coincurve` real + `secrets`)
  - **Resultado esperado:** `privkey` tiene 64 chars hex (32 bytes), `pubkey` tiene 66 chars hex (33 bytes comprimidos, empieza con `02` o `03`)

- [ ] **1.2** `test_generate_keypair_unique_per_call`
  - **Valida:** Dos llamadas consecutivas producen keypairs diferentes
  - **Setup/mocks:** Ninguno
  - **Resultado esperado:** `privkey1 != privkey2` y `pubkey1 != pubkey2`

- [ ] **1.3** `test_verify_preimage_valid`
  - **Valida:** `verify_preimage` retorna `True` cuando `SHA256(preimage) == expected_hash`
  - **Setup/mocks:** Preimage conocido: `preimage = "aa" * 32`, calcular `expected = sha256(bytes.fromhex(preimage)).hexdigest()`
  - **Resultado esperado:** `True`

- [ ] **1.4** `test_verify_preimage_invalid`
  - **Valida:** `verify_preimage` retorna `False` cuando el hash no coincide
  - **Setup/mocks:** Preimage valido + hash incorrecto (`"bb" * 32`)
  - **Resultado esperado:** `False`

- [ ] **1.5** `test_verify_preimage_invalid_hex_raises`
  - **Valida:** `verify_preimage` lanza excepcion con hex invalido
  - **Setup/mocks:** `preimage_hex = "xyz_not_hex"`
  - **Resultado esperado:** `ValueError`

---

## Capa 2: Unit -- BoltzClient HTTP (`boltz.py`)

**Archivo:** `tests/test_boltz.py`

Todas estas tests mockean `urllib.request.urlopen` para no hacer llamadas reales a Boltz.

- [ ] **2.1** `test_get_submarine_pairs_returns_lbtc_btc`
  - **Valida:** `get_submarine_pairs()` hace GET a `/v2/swap/submarine` y retorna la info del par L-BTC/BTC
  - **Setup/mocks:** Mock `urlopen` retornando JSON con `{"L-BTC/BTC": {"rate": 1, "fees": {...}, "limits": {...}}}`
  - **Resultado esperado:** Dict con key `"L-BTC/BTC"`, contiene `fees.percentage`, `fees.minerFees`, `limits.minimal`, `limits.maximal`

- [ ] **2.2** `test_create_submarine_swap_sends_correct_body`
  - **Valida:** `create_submarine_swap(invoice, refund_pubkey)` hace POST con el body correcto (`invoice`, `from: "L-BTC"`, `to: "BTC"`, `refundPublicKey`)
  - **Setup/mocks:** Mock `urlopen`, capturar el request body
  - **Resultado esperado:** Body contiene exactamente los 4 campos esperados

- [ ] **2.3** `test_create_submarine_swap_returns_swap_data`
  - **Valida:** La respuesta del API se parsea correctamente en un dict con `id`, `address`, `expectedAmount`, `claimPublicKey`, `swapTree`, `timeoutBlockHeight`
  - **Setup/mocks:** Mock `urlopen` retornando `MOCK_SWAP_RESPONSE`
  - **Resultado esperado:** Dict con todos los campos esperados

- [ ] **2.4** `test_get_swap_lightning_status_returns_status`
  - **Valida:** `get_swap_status(swap_id)` retorna el estado actual del swap
  - **Setup/mocks:** Mock `urlopen` retornando `{"status": "transaction.mempool"}`
  - **Resultado esperado:** `result["status"] == "transaction.mempool"`

- [ ] **2.5** `test_get_claim_details_returns_preimage`
  - **Valida:** `get_claim_details(swap_id)` retorna preimage y transactionHash
  - **Setup/mocks:** Mock `urlopen` retornando `{"preimage": "aa"*32, "transactionHash": "bb"*32}`
  - **Resultado esperado:** Dict con `preimage` y `transactionHash`

- [ ] **2.6** `test_api_request_http_error_raises`
  - **Valida:** Un error HTTP del API de Boltz (ej. 400, 500) se propaga como excepcion descriptiva
  - **Setup/mocks:** Mock `urlopen` levantando `urllib.error.HTTPError` con codigo 400
  - **Resultado esperado:** Excepcion con informacion del error HTTP

- [ ] **2.7** `test_api_request_timeout_raises`
  - **Valida:** Un timeout de red se propaga como excepcion
  - **Setup/mocks:** Mock `urlopen` levantando `urllib.error.URLError("timeout")`
  - **Resultado esperado:** Excepcion con mensaje de timeout/conectividad

- [ ] **2.8** `test_api_request_invalid_json_raises`
  - **Valida:** Una respuesta no-JSON del API se maneja correctamente
  - **Setup/mocks:** Mock `urlopen` retornando contenido no-JSON
  - **Resultado esperado:** Excepcion descriptiva

---

## Capa 3: Unit -- SwapInfo dataclass (`boltz.py`)

**Archivo:** `tests/test_boltz.py`

- [ ] **3.1** `test_swap_info_to_dict_roundtrip`
  - **Valida:** `SwapInfo` se puede serializar a dict y reconstruir sin perdida de datos
  - **Setup/mocks:** Crear `SwapInfo` con todos los campos, llamar `to_dict()`, reconstruir con `SwapInfo(**data)`
  - **Resultado esperado:** Objeto reconstruido == original

- [ ] **3.2** `test_swap_info_optional_fields_default_none`
  - **Valida:** Los campos opcionales (`lockup_txid`, `preimage`, `claim_txid`) son `None` por defecto
  - **Setup/mocks:** Crear `SwapInfo` sin esos campos
  - **Resultado esperado:** `swap.lockup_txid is None`, etc.

---

## Capa 4: Unit -- Persistencia de swaps (`storage.py`)

**Archivo:** `tests/test_storage.py` (agregar clase nueva)

- [ ] **4.1** `test_swaps_dir_created_on_init`
  - **Valida:** Al inicializar Storage, se crea el directorio `swaps/`
  - **Setup/mocks:** `temp_storage` fixture existente
  - **Resultado esperado:** `temp_storage.swaps_dir.exists() is True`

- [ ] **4.2** `test_save_and_load_swap`
  - **Valida:** Un `SwapInfo` guardado se puede cargar correctamente
  - **Setup/mocks:** Crear `SwapInfo` de ejemplo, guardar con `save_swap`, cargar con `load_swap`
  - **Resultado esperado:** Todos los campos del swap cargado coinciden con el original

- [ ] **4.3** `test_load_swap_not_found_returns_none`
  - **Valida:** `load_swap("nonexistent")` retorna `None`
  - **Setup/mocks:** Storage vacio
  - **Resultado esperado:** `None`

- [ ] **4.4** `test_list_swaps_empty`
  - **Valida:** `list_swaps()` retorna lista vacia cuando no hay swaps
  - **Setup/mocks:** Storage vacio
  - **Resultado esperado:** `[]`

- [ ] **4.5** `test_list_swaps_returns_ids`
  - **Valida:** `list_swaps()` retorna los IDs de todos los swaps guardados
  - **Setup/mocks:** Guardar 2 swaps con IDs diferentes
  - **Resultado esperado:** Lista con ambos IDs

- [ ] **4.6** `test_save_swap_updates_existing`
  - **Valida:** Guardar un swap con el mismo ID sobreescribe los datos anteriores (para actualizar `status`, `lockup_txid`, etc.)
  - **Setup/mocks:** Guardar swap, modificar status, guardar de nuevo, cargar
  - **Resultado esperado:** Datos cargados reflejan la segunda escritura

- [ ] **4.7** `test_swap_file_permissions`
  - **Valida:** El archivo del swap se crea con permisos `0o600`
  - **Setup/mocks:** Guardar un swap, verificar permisos del archivo
  - **Resultado esperado:** `mode == 0o600` (skip en Windows)

---

## Capa 5: Integracion -- Tool `lbtc_pay_lightning_invoice` (`tools.py`)

**Archivo:** `tests/test_tools_lightning.py`

Estas tests mockean `BoltzClient` (inyectado o parcheado) y `WalletManager.send` para no hacer llamadas de red.

- [ ] **5.1** `test_pay_lightning_invoice_happy_path`
  - **Valida:** El flujo completo: valida invoice -> crea swap via Boltz -> envia L-BTC al lockup -> persiste swap -> retorna `swap_id`, `lockup_txid`, `status`, `expected_amount`
  - **Setup/mocks:** Mock `BoltzClient.get_submarine_pairs` (retorna par L-BTC/BTC), Mock `BoltzClient.create_submarine_swap` (retorna `MOCK_SWAP_RESPONSE`), Mock `WalletManager.send` (retorna txid), Mock balance >= `expectedAmount`
  - **Resultado esperado:** Dict con `swap_id`, `lockup_txid`, `status`, `expected_amount`, `timeout_block_height`. Swap persistido en disco.

- [ ] **5.2** `test_pay_lightning_invoice_invalid_invoice_format`
  - **Valida:** Invoice que no empieza con `lnbc` es rechazado inmediatamente
  - **Setup/mocks:** Ninguno
  - **Resultado esperado:** `ValueError` con mensaje sobre formato de invoice invalido

- [ ] **5.3** `test_pay_lightning_invoice_empty_invoice_raises`
  - **Valida:** Invoice vacio es rechazado
  - **Setup/mocks:** Ninguno
  - **Resultado esperado:** `ValueError`

- [ ] **5.4** `test_pay_lightning_invoice_insufficient_balance`
  - **Valida:** Si el balance de L-BTC es menor que `expectedAmount`, se rechaza antes de enviar
  - **Setup/mocks:** Mock Boltz con `expectedAmount: 50069`, Mock balance L-BTC = 1000 sats
  - **Resultado esperado:** `ValueError` con info de balance insuficiente

- [ ] **5.5** `test_pay_lightning_invoice_watch_only_wallet_raises`
  - **Valida:** No se puede pagar desde una wallet watch-only
  - **Setup/mocks:** Importar wallet como watch-only (descriptor)
  - **Resultado esperado:** `ValueError` con mensaje sobre watch-only

- [ ] **5.6** `test_pay_lightning_invoice_passphrase_required`
  - **Valida:** Wallet encriptada sin passphrase lanza error claro
  - **Setup/mocks:** Importar con passphrase, limpiar signer cached, intentar sin passphrase
  - **Resultado esperado:** `ValueError` con "passphrase required"

- [ ] **5.7** `test_pay_lightning_invoice_boltz_api_error`
  - **Valida:** Error del API de Boltz al crear swap se propaga con mensaje descriptivo
  - **Setup/mocks:** Mock `BoltzClient.create_submarine_swap` que levanta excepcion
  - **Resultado esperado:** Excepcion con contexto del error de Boltz

- [ ] **5.8** `test_pay_lightning_invoice_persists_swap_before_sending`
  - **Valida:** El swap se guarda a disco ANTES de enviar L-BTC (para recovery)
  - **Setup/mocks:** Mock `storage.save_swap` para capturar la llamada, Mock `WalletManager.send`
  - **Resultado esperado:** `save_swap` se llama antes de `send`. El swap guardado tiene `lockup_txid = None` en la primera llamada.

- [ ] **5.9** `test_pay_lightning_invoice_updates_swap_with_lockup_txid`
  - **Valida:** Despues de enviar L-BTC, el swap guardado se actualiza con `lockup_txid`
  - **Setup/mocks:** Mock completo del happy path
  - **Resultado esperado:** Swap en disco tiene `lockup_txid` != None

- [ ] **5.10** `test_pay_lightning_invoice_amount_below_minimum_raises`
  - **Valida:** Si el par L-BTC/BTC tiene `limits.minimal: 1000` y el invoice es menor, se rechaza
  - **Setup/mocks:** Mock `get_submarine_pairs` con `limits.minimal = 1000`, invoice que produce `expectedAmount` < 1000
  - **Resultado esperado:** `ValueError` con mensaje sobre monto minimo

- [ ] **5.11** `test_pay_lightning_invoice_amount_above_maximum_raises`
  - **Valida:** Si el monto supera `limits.maximal`, se rechaza
  - **Setup/mocks:** Mock pairs con `limits.maximal = 25000000`, invoice que excede
  - **Resultado esperado:** `ValueError` con mensaje sobre monto maximo

- [ ] **5.12** `test_pay_lightning_invoice_wallet_not_found_raises`
  - **Valida:** Wallet inexistente produce error claro
  - **Setup/mocks:** Ninguno (wallet no existe)
  - **Resultado esperado:** `ValueError` con "not found"

---

## Capa 6: Integracion -- Tool `lbtc_swap_lightning_status` (`tools.py`)

**Archivo:** `tests/test_tools_lightning.py`

- [ ] **6.1** `test_swap_lightning_status_returns_current_status`
  - **Valida:** Consulta estado del swap via Boltz API y retorna la info combinada (datos locales + status remoto)
  - **Setup/mocks:** Swap guardado en disco, Mock `BoltzClient.get_swap_status` retornando `{"status": "transaction.mempool"}`
  - **Resultado esperado:** Dict con `swap_id`, `status: "transaction.mempool"`, `lockup_txid`, `timeout_block_height`

- [ ] **6.2** `test_swap_lightning_status_claimed_fetches_preimage`
  - **Valida:** Cuando el status es `transaction.claimed`, la tool obtiene los claim details y verifica el preimage
  - **Setup/mocks:** Mock status `"transaction.claimed"`, Mock `get_claim_details` retornando preimage valido
  - **Resultado esperado:** Dict incluye `preimage`, `status: "transaction.claimed"`

- [ ] **6.3** `test_swap_lightning_status_failure_returns_refund_info`
  - **Valida:** Cuando status es `invoice.failedToPay`, retorna info de refund (timeout, swap_id)
  - **Setup/mocks:** Mock status `"invoice.failedToPay"`, swap guardado con `timeout_block_height`
  - **Resultado esperado:** Dict con `status`, `refund_info` conteniendo timeout block height y swap_id

- [ ] **6.4** `test_swap_lightning_status_not_found_raises`
  - **Valida:** Swap ID que no existe en disco produce error
  - **Setup/mocks:** Storage vacio
  - **Resultado esperado:** `ValueError` con "not found" o "unknown swap"

- [ ] **6.5** `test_swap_lightning_status_updates_stored_swap`
  - **Valida:** Al consultar el estado, el swap en disco se actualiza con el nuevo status (y preimage si hay)
  - **Setup/mocks:** Swap guardado con `status: "swap.created"`, mock API retorna `"transaction.mempool"`
  - **Resultado esperado:** Despues de la llamada, `load_swap` retorna `status: "transaction.mempool"`

- [ ] **6.6** `test_swap_lightning_status_boltz_api_error_returns_local_data`
  - **Valida:** Si Boltz API no responde, se retorna la info local guardada con warning
  - **Setup/mocks:** Mock `get_swap_status` levantando excepcion, swap guardado en disco
  - **Resultado esperado:** Dict con datos locales + campo `warning` indicando el error de API

---

## Capa 7: Integracion -- Registro MCP (`server.py`)

**Archivo:** `tests/test_tools_lightning.py`

- [ ] **7.1** `test_lightning_tools_registered_in_tools_dict`
  - **Valida:** Las 2 nuevas tools estan en el dict `TOOLS`
  - **Setup/mocks:** Importar `TOOLS` de `aqua_mcp.tools`
  - **Resultado esperado:** `"lbtc_pay_lightning_invoice" in TOOLS` y `"lbtc_swap_lightning_status" in TOOLS`

- [ ] **7.2** `test_lightning_tools_are_callable`
  - **Valida:** Ambas tools son callables
  - **Setup/mocks:** Importar de `TOOLS`
  - **Resultado esperado:** `callable(TOOLS["lbtc_pay_lightning_invoice"])` y `callable(TOOLS["lbtc_swap_lightning_status"])`

---

## Mock Fixtures de Referencia

```python
MOCK_SUBMARINE_PAIRS = {
    "L-BTC/BTC": {
        "rate": 1.0,
        "fees": {"percentage": 0.1, "minerFees": 19},
        "limits": {"maximal": 25000000, "minimal": 1000, "maximalZeroConf": 500000},
    }
}

MOCK_SWAP_RESPONSE = {
    "id": "test_swap_123",
    "address": "lq1qqexampleaddress",
    "expectedAmount": 50069,
    "claimPublicKey": "03" + "ab" * 32,
    "swapTree": {
        "claimLeaf": {"version": 192, "output": "a914..."},
        "refundLeaf": {"version": 192, "output": "b914..."},
    },
    "timeoutBlockHeight": 2500000,
}

MOCK_CLAIM_DETAILS = {
    "preimage": "aa" * 32,
    "transactionHash": "bb" * 32,
    "pubNonce": "cc" * 33,
}
```

---

## Decisiones (preguntas resueltas)

1. **Validacion de invoice:** Solo chequear prefijo `lnbc` para v1. Boltz valida el resto.

2. **Balance check:** Mockear `WalletManager.get_balance` para devolver una lista de `Balance` con el monto controlado.

3. **Envio de L-BTC:** Usar `WalletManager.send()` directamente (no `lw_send` de tools.py). El manager directo evita serializacion innecesaria.

4. **Validacion de limites:** Hacer la validacion en `BoltzClient` (no en la tool).

5. **Archivos de test:** Separar en `tests/test_boltz.py` (unit de boltz.py, capas 1-3) + `tests/test_tools_lightning.py` (integracion de tools, capas 5-7). Capa 4 (storage) va en `tests/test_storage.py`.

6. **Claim details cuando falla el fetch:** Retornar status sin preimage + warning. El endpoint de Boltz retorna `{"preimage": "string", "pubNonce": "string", "publicKey": "string", "transactionHash": "string"}`. Si el GET falla, `preimage` queda `None` en el resultado y se agrega campo `warning`.

---

**Total: 33 tests** (7 capas)
- Capa 1: 5 tests (crypto utils)
- Capa 2: 8 tests (BoltzClient HTTP)
- Capa 3: 2 tests (SwapInfo dataclass)
- Capa 4: 7 tests (storage swaps)
- Capa 5: 12 tests (tool lbtc_pay_lightning_invoice)
- Capa 6: 6 tests (tool lbtc_swap_lightning_status)
- Capa 7: 2 tests (registro MCP)
