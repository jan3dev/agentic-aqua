# Indra como proveedor de swaps Lightning por defecto (Boltz como opción B)

## Resumen

Se abstrae el proveedor de swaps submarine L-BTC → Lightning detrás de un registro con dos entradas: `indra` (nuevo default, `https://indra.aquabtc.com`) y `boltz` (fallback intacto). Indra expone la misma API v2 que Boltz, así que se reutiliza el cliente HTTP existente parametrizando base URL, etiqueta de proveedor y límites. La selección se hace con una nueva clave de config `lightning_provider` y la env var `AQUA_LIGHTNING_PROVIDER` que la sobreescribe. Los swaps de recibir Lightning (`lightning_receive`, que va por Ankara) quedan apagados por defecto vía feature flag; queda activo solo L-BTC → Lightning.

Trabajo previo: `git fetch` + actualizar `develop` desde `origin/develop`, y crear `feat/indra-swap-provider` a partir de ahí. Los commits se hacen con `rocket:commit`, agrupados por subsistema (cliente/registro, capa lightning, flags+docs, tests).

## Cambios de implementación

**Cliente de protocolo Boltz-v2 reutilizable (`boltz.py`)**

- `BoltzClient.__init__` acepta `api_urls` y `provider_label` (defaults `BOLTZ_API` / `"Boltz"`), de modo que los mensajes de error y los logs nombren al proveedor real. La firma actual `BoltzClient(network=..., tls_context=...)` y el chequeo de `https://` no cambian, así que `tests/test_boltz.py` sigue verde sin tocar.
- `BoltzSwapAlreadyExistsError` mantiene el nombre pero el texto del mensaje usa `provider_label`.
- Sin cambios en `SwapInfo`, `generate_keypair`, `verify_preimage` ni en el import diferido de `storage.load_swap`.

**Nuevo `indra.py`**

- `INDRA_API = {"mainnet": os.environ.get("INDRA_API_URL", "https://indra.aquabtc.com")}` — sin entrada `testnet`, porque no existe host de testnet (`test.indra.aquabtc.com` no resuelve). Instanciar `IndraClient(network="testnet")` levanta `ValueError` indicando que hay que pasar a `boltz` para testnet; nada de fallback silencioso.
- `IndraClient(BoltzClient)` fija `api_urls=INDRA_API` y `provider_label="Indra"`.
- Constantes `MIN_SWAP_AMOUNT_SATS = 1_000` y `MAX_SWAP_AMOUNT_SATS = 100_000`, tomadas del par `L-BTC → BTC` que hoy devuelve el servicio (0,1 % + 21 sats de minero).

**Registro de proveedores (`lightning_providers.py`)**

- `SwapProvider` (frozen dataclass): `name`, `client_factory`, `min_sats`, `max_sats`.
- `PROVIDERS = {"indra": ..., "boltz": ...}` y `DEFAULT_PROVIDER = "indra"`.
- `resolve_send_provider(config)`: `AQUA_LIGHTNING_PROVIDER` gana sobre `config.lightning_provider`, que gana sobre el default. Un nombre desconocido levanta `ValueError` listando los válidos.
- El módulo importa `storage`/`boltz`/`indra` únicamente; no importa `features` ni `tools`, para no crear ciclo con `features → tools → lightning`.

**Config (`storage.py`, `doctor.py`)**

- `Config.lightning_provider: str = "indra"`. `KNOWN_CONFIG_KEYS` se deriva de los campos, así que se propaga solo.
- `doctor` agrega un hallazgo con `action: "manual"` cuando el valor no está en `PROVIDERS` (no se autocorrige un proveedor elegido a mano).

**Capa Lightning (`lightning.py`)**

- `pay_invoice` resuelve el proveedor desde `self.storage.load_config()`, valida el monto contra `min_sats`/`max_sats` del proveedor y **además** contra `limits.minimal`/`limits.maximal` del par en vivo (`GET /v2/swap/submarine`), con mensajes que nombran al proveedor. Así el tope sube solo cuando Indra lo suba.
- El registro `LightningSwap` guarda `provider=provider.name` (`"indra"` o `"boltz"`).
- `get_send_status` construye el cliente a partir de `swap.provider`, de modo que swaps viejos de Boltz siguen consultándose contra Boltz.
- `_BOLTZ_STATUS_MAP` pasa a `_SWAP_STATUS_MAP` y suma los estados que declara el enum de Indra y hoy no están mapeados: `invoice.set` y `invoice.pending` → `processing`, `invoice.paid` e `invoice.settled` → `completed`, `invoice.expired`, `invoice.failedToPay` y `transaction.lockupFailed` → `failed`.
- La respuesta de estado reemplaza `boltz_status` por `provider_status` y agrega `provider`. Es un cambio de contrato visible: se actualizan el docstring de `lightning_transaction_status`, la descripción en `server.py` y los tests que lo asertan.

**Desactivar recibir Lightning**

- `lightning_receive` entra en `_SHIPPED_DISABLED` (`features.py`). El gating existente lo saca del listado MCP y del CLI (`aqua lightning receive`) en el arranque; el código y sus tests quedan intactos y se reactiva poniéndolo en `true` en `config.json`, sin release.
- Los textos de prompt de `server.py` que instruyen usar `lightning_receive` se ajustan para aclarar que el receive está deshabilitado por defecto.

**Docs**

- `docs/CONFIG.md`: fila y ejemplo para `lightning_provider`, más la nota de que `lightning_receive` ahora viene apagado.
- `src/aqua/AGENTS.md`: filas para `indra.py` y `lightning_providers.py`, y actualización de la fila de `lightning.py`.
- `README.md`: mención de Indra como proveedor por defecto y de cómo volver a Boltz.

## Plan de pruebas

- `tests/test_indra.py`: `IndraClient` apunta a `https://indra.aquabtc.com`, respeta `INDRA_API_URL`, rechaza `network="testnet"`, y sus errores HTTP nombran a Indra. Mock sobre el método del cliente, siguiendo `tests/AGENTS.md`.
- Registro: default es `indra`; `config.lightning_provider = "boltz"` devuelve Boltz; `AQUA_LIGHTNING_PROVIDER` gana sobre la config; nombre inválido levanta `ValueError`.
- `tests/test_lightning.py`: camino feliz de `pay_invoice` contra Indra guardando `provider="indra"`; el mismo camino contra Boltz cuando la config lo pide; monto bajo el mínimo y sobre el máximo de Indra rechazados antes de crear el swap; monto rechazado por los `limits` del par en vivo aunque pase las constantes; `get_send_status` de un swap `provider="boltz"` consulta Boltz y devuelve `provider_status`.
- Mapeo de estados: los nuevos estados de Indra (`invoice.set`, `invoice.paid`, `invoice.failedToPay`, `transaction.lockupFailed`) llegan a `processing`/`completed`/`failed`.
- Flags y config: `lightning_receive` sale del listado MCP y del CLI con los defaults de fábrica; `Config` hace round-trip de `lightning_provider`; `doctor` marca un proveedor inválido como manual.
- Regresión: `uv run python -m pytest tests/` completo y `uv run ruff check src tests`.

**Fase 2 (swaps reales, después de los commits)**

- Se ejecuta `aqua lightning send` contra Indra en mainnet por un monto chico dentro de 1.000–100.000 sats, y luego `aqua lightning status` hasta ver `completed` con preimagen.
- Requiere dos cosas del usuario: un invoice BOLT11 chico generado por su wallet (aqua es quien paga) y saldo L-BTC en la wallet local que cubra monto + ~0,1 % + 21 sats. El agente no puede generar un invoice para que el usuario lo pague, porque ese es el flujo de receive que se está apagando.
- El `info.description` del OpenAPI de Indra declara "mock phase" y el broadcast de `/v2/chain/{currency}/transaction` devuelve un txid falso; si el swap real no liquida, esa es la primera hipótesis a verificar antes de tocar el código.

## Supuestos

- Indra solo tiene mainnet; testnet queda cubierto eligiendo `boltz`.
- Los límites 1.000–100.000 sats se hardcodean como constantes del proveedor pero la validación efectiva es el par en vivo, así que un cambio de tope en Indra no requiere release mientras esté dentro de las constantes.
- Renombrar `boltz_status` a `provider_status` es aceptable como cambio de contrato de la tool, y se documenta en las notas de release.
- Los swaps ya persistidos en `~/.aqua` siguen resolviéndose contra el proveedor con el que se crearon.
