"""Indra swap provider (L-BTC -> Lightning), AQUA's Boltz-compatible service.

Indra (`https://indra.aquabtc.com`, service name `swaps-api`) mirrors the Boltz
v2 API, so the transport in `aqua.boltz` is reused verbatim and only the base
URL, provider label and amount limits differ.

Mainnet only: there is no testnet deployment (`test.indra.aquabtc.com` does not
resolve), so `IndraClient(network="testnet")` raises instead of silently
falling back to another host. Testnet is covered by selecting the `boltz`
provider.
"""

import os

from .boltz import BoltzClient

INDRA_API = {
    "mainnet": os.environ.get("INDRA_API_URL", "https://indra.aquabtc.com"),
}

PROVIDER_LABEL = "Indra"

# Client-side swap amount limits (satoshis), from the live L-BTC -> BTC pair.
# The effective check is the pair's own `limits` (see `lightning.pay_invoice`);
# these constants only bound what aqua will even attempt.
MIN_SWAP_AMOUNT_SATS = 1_000
MAX_SWAP_AMOUNT_SATS = 100_000


class IndraClient(BoltzClient):
    """HTTP client for the Indra swap API (Boltz v2 protocol)."""

    def __init__(self, network: str = "mainnet", tls_context=None):
        super().__init__(
            network=network,
            tls_context=tls_context,
            api_urls=INDRA_API,
            provider_label=PROVIDER_LABEL,
        )
