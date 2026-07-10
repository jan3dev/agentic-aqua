"""Feature flag system for MCP tools and CLI commands.

Provides runtime config-driven gating of MCP tools and their corresponding CLI
commands. The single source of truth is `Config.enabled_tools` persisted in
`~/.aqua/config.json`. See `.omc/plans/feature-flags-mcp-cli.md`.
"""

from __future__ import annotations

import logging

from .storage import Config, Storage
from .tools import TOOLS

logger = logging.getLogger(__name__)


# Tools shipped disabled-by-default (currently empty; add a name to opt a tool out).
_SHIPPED_DISABLED: frozenset[str] = frozenset()

assert _SHIPPED_DISABLED <= TOOLS.keys(), (
    f"unknown tool in _SHIPPED_DISABLED: {_SHIPPED_DISABLED - TOOLS.keys()}"
)

# Shipped defaults: every currently-known MCP tool, with `_SHIPPED_DISABLED`
# flipped to False. Maintainers add tool names to `_SHIPPED_DISABLED` to ship
# them disabled-by-default.
SHIPPED_DEFAULTS_ENABLED_TOOLS: dict[str, bool] = {
    name: name not in _SHIPPED_DISABLED for name in TOOLS
}


# Mapping from (CLI group name, CLI command name) to MCP tool name.
# Empty string for the group means top-level (registered directly on `cli`).
# Verbatim from .omc/plans/feature-flags-mcp-cli.md Appendix A.
CLI_COMMAND_TO_MCP_TOOL: dict[tuple[str, str], str] = {
    # Top-level
    ("", "balance"): "unified_balance",

    # wallet group (cli/wallet.py)
    ("wallet", "generate-mnemonic"): "lw_generate_mnemonic",
    ("wallet", "import-mnemonic"): "lw_import_mnemonic",
    ("wallet", "list"): "lw_list_wallets",
    ("wallet", "delete"): "delete_wallet",

    # liquid group (cli/liquid.py)
    ("liquid", "balance"): "lw_balance",
    ("liquid", "address"): "lw_address",
    ("liquid", "transactions"): "lw_transactions",
    ("liquid", "send"): "lw_send",
    ("liquid", "send-asset"): "lw_send_asset",
    ("liquid", "sweep"): "lw_sweep",
    ("liquid", "assets"): "lw_list_assets",
    ("liquid", "tx-status"): "lw_tx_status",
    ("liquid", "import-descriptor"): "lw_import_descriptor",
    ("liquid", "export-descriptor"): "lw_export_descriptor",

    # btc group (cli/btc.py)
    ("btc", "balance"): "btc_balance",
    ("btc", "address"): "btc_address",
    ("btc", "transactions"): "btc_transactions",
    ("btc", "send"): "btc_send",
    ("btc", "sweep"): "btc_sweep",
    ("btc", "import-descriptor"): "btc_import_descriptor",
    ("btc", "export-descriptor"): "btc_export_descriptor",

    # lightning group (cli/lightning.py)
    ("lightning", "receive"): "lightning_receive",
    ("lightning", "send"): "lightning_send",
    ("lightning", "status"): "lightning_transaction_status",
    ("lightning", "decode"): "lightning_decode",

    # changelly group (cli/changelly.py)
    ("changelly", "currencies"): "changelly_list_currencies",
    ("changelly", "quote"): "changelly_quote",
    ("changelly", "send"): "changelly_send",
    ("changelly", "receive"): "changelly_receive",
    ("changelly", "status"): "changelly_status",

    # sideshift group (cli/sideshift.py)
    ("sideshift", "coins"): "sideshift_list_coins",
    ("sideshift", "pair-info"): "sideshift_pair_info",
    ("sideshift", "quote"): "sideshift_quote",
    ("sideshift", "recommend"): "sideshift_recommend",
    ("sideshift", "send"): "sideshift_send",
    ("sideshift", "receive"): "sideshift_receive",
    ("sideshift", "status"): "sideshift_status",

    # sideswap group (cli/sideswap.py)
    ("sideswap", "status"): "sideswap_server_status",
    ("sideswap", "recommend"): "sideswap_recommend",
    ("sideswap", "peg-quote"): "sideswap_peg_quote",
    ("sideswap", "peg-in"): "sideswap_peg_in",
    ("sideswap", "peg-out"): "sideswap_peg_out",
    ("sideswap", "peg-status"): "sideswap_peg_status",
    ("sideswap", "assets"): "sideswap_list_assets",
    ("sideswap", "quote"): "sideswap_quote",
    ("sideswap", "swap"): "sideswap_execute_swap",
    ("sideswap", "swap-status"): "sideswap_swap_status",

    # wapupay group (cli/wapupay.py)
    ("wapupay", "rates"): "wapupay_exchange_rates",
    ("wapupay", "quote"): "wapupay_quote",
    ("wapupay", "create-order"): "wapupay_create_order",
    ("wapupay", "fund-order"): "wapupay_fund_order",
    ("wapupay", "order-status"): "wapupay_order_status",
    ("wapupay", "orders"): "wapupay_orders",
    ("wapupay", "transactions"): "wapupay_transactions",
    ("wapupay", "transaction"): "wapupay_transaction",
    ("wapupay", "spending-limit"): "wapupay_spending_limit",
    ("wapupay", "provision-account"): "wapupay_provision_account",

    # qr group (cli/qr.py)
    ("qr", "decode"): "qr_decode",

    # Top-level diagnostic (cli/doctor.py) — always registered, gated only for MCP.
    ("", "doctor"): "doctor",

    # jan3 group (cli/jan3.py) — JAN3 account login + sessions + Lightning Address
    ("jan3", "login"): "jan3_login",
    ("jan3", "verify"): "jan3_verify",
    ("jan3", "login-start"): "jan3_login_start",
    ("jan3", "login-complete"): "jan3_login_complete",
    ("jan3", "session-info"): "jan3_session_info",
    ("jan3", "list-sessions"): "jan3_list_sessions",
    ("jan3", "logout"): "jan3_logout",
    ("jan3", "user-info"): "jan3_user_info",
    ("jan3", "enable-lightning-address"): "jan3_enable_lightning_address",
    ("jan3", "rebind-wallet"): "jan3_rebind_wallet",
    ("jan3", "ln-check-username"): "jan3_ln_check_username",
    ("jan3", "purchase-ln-username"): "jan3_purchase_ln_username",
}


# Tools available only via MCP — no Click command in `src/aqua/cli/`.
# Currently every MCP tool has a corresponding CLI command.
MCP_ONLY_TOOLS: frozenset[str] = frozenset()


def is_tool_enabled(name: str, config: Config) -> bool:
    """Return True if the MCP tool `name` is enabled by `config`.

    `config` is REQUIRED — never read from a module global.
    Falls back to `SHIPPED_DEFAULTS_ENABLED_TOOLS` when the user's
    `enabled_tools` map lacks the key. Unknown tool names (not present in
    `TOOLS`) default to True (forward-compat for tools the user expects to
    land); the warning for unknown user-provided keys is emitted at config
    load time in `load_config_with_merge`.
    """
    return config.enabled_tools.get(
        name, SHIPPED_DEFAULTS_ENABLED_TOOLS.get(name, True)
    )


def _merge_with_defaults(
    loaded: dict[str, bool],
) -> tuple[dict[str, bool], bool]:
    """Merge shipped defaults into `loaded`, preserving user's overrides.

    Returns `(merged, changed)`. `changed` is True if any default keys were
    inserted. The merge is in-memory only; `load_config_with_merge` no longer
    persists it (see that function's docstring).
    """
    merged = dict(loaded)
    changed = False
    for key, default in SHIPPED_DEFAULTS_ENABLED_TOOLS.items():
        if key not in merged:
            merged[key] = default
            changed = True
    return merged, changed


def load_config_with_merge(storage: Storage | None = None) -> Config:
    """Load config and merge shipped defaults IN MEMORY. Read-only — never writes.

    Startup is non-invasive: the shipped defaults are merged into the returned
    `Config` so callers see a fully-populated `enabled_tools` for gating, but
    `config.json` is left untouched on disk. A tool key absent from the file
    means "use the shipped default" (see `is_tool_enabled`), so new versions can
    change a default without clobbering the user's explicit choices, and the
    file is never re-polluted with defaults on every run.

    `doctor` (CLI `aqua doctor --fix`, MCP tool `doctor`) is the only code path
    that rewrites `config.json`. Unknown keys (typos, removed tools) are warned
    with a pointer to it — they are no longer silently persisted.
    """
    if storage is None:
        storage = Storage()

    # Startup must never crash on a broken config file — otherwise `aqua doctor`
    # (the tool that repairs it) could not run either. A corrupt/unreadable file
    # degrades to in-memory defaults (read-only, so nothing is overwritten) with
    # a pointer to `doctor`, which reads the raw JSON and reports the problem.
    try:
        config = storage.load_config()
    except (OSError, ValueError) as exc:  # ValueError ⊇ json.JSONDecodeError
        logger.warning(
            "Could not read %s (%s). Using defaults for this run; "
            "run `aqua doctor` to inspect and repair it.",
            storage.config_path,
            exc,
        )
        config = Config()

    # Warn on unknown keys (typos, removed tools). One line per key, each
    # pointing at `doctor` so the user can clean them all up at once.
    for key in config.enabled_tools:
        if key not in TOOLS:
            logger.warning(
                "Unknown tool in enabled_tools: %r (ignored). "
                "Run `aqua doctor --fix` to clean it up, or remove it from %s.",
                key,
                storage.config_path,
            )

    merged, _ = _merge_with_defaults(config.enabled_tools)
    config.enabled_tools = merged
    return config
