"""Registry of L-BTC -> Lightning submarine swap providers.

Two providers speak the same Boltz v2 protocol: `indra` (AQUA's own service,
the default) and `boltz` (the original, kept as the fallback and the only one
with a testnet endpoint).

Import discipline: this module imports only `boltz`, `indra` and `storage`.
It must NOT import `features` or `tools`, which would close the
`features -> tools -> lightning` import cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from . import boltz, indra

ENV_VAR = "AQUA_LIGHTNING_PROVIDER"

DEFAULT_PROVIDER = "indra"


@dataclass(frozen=True)
class SwapProvider:
    """A submarine swap backend: how to build its client and what it accepts."""

    name: str
    client_factory: Callable[..., boltz.BoltzClient]
    min_sats: int
    max_sats: int
    label: str


PROVIDERS: dict[str, SwapProvider] = {
    "indra": SwapProvider(
        name="indra",
        # Late-bound so tests can patch aqua.indra.IndraClient.
        client_factory=lambda network="mainnet": indra.IndraClient(network=network),
        min_sats=indra.MIN_SWAP_AMOUNT_SATS,
        max_sats=indra.MAX_SWAP_AMOUNT_SATS,
        label=indra.PROVIDER_LABEL,
    ),
    "boltz": SwapProvider(
        name="boltz",
        client_factory=lambda network="mainnet": boltz.BoltzClient(network=network),
        min_sats=boltz.MIN_SWAP_AMOUNT_SATS,
        max_sats=boltz.MAX_SWAP_AMOUNT_SATS,
        label="Boltz",
    ),
}


def get_provider(name: str) -> SwapProvider:
    """Look up a provider by name, raising on an unknown one."""
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown Lightning swap provider {name!r}. "
            f"Valid values: {', '.join(sorted(PROVIDERS))}."
        ) from None


def resolve_send_provider(config) -> SwapProvider:
    """Pick the provider for a new send swap.

    Precedence: `AQUA_LIGHTNING_PROVIDER` env var, then
    `config.lightning_provider`, then `DEFAULT_PROVIDER`.
    """
    name = os.environ.get(ENV_VAR) or getattr(
        config, "lightning_provider", None
    ) or DEFAULT_PROVIDER
    if not isinstance(name, str):
        raise ValueError(
            f"Unknown Lightning swap provider {name!r}: expected a string, got "
            f"{type(name).__name__}. Valid values: {', '.join(sorted(PROVIDERS))}."
        )
    return get_provider(name.strip().lower())
