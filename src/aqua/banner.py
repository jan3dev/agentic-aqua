"""Welcome banner shown on wallet-creation entry points.

Displays the AQUA ASCII logo plus a security reminder the first time a
seed/descriptor-handling tool is touched in a process (= MCP session) or
whenever the user runs ``aqua --help``.
"""

from importlib.resources import files

TRIGGER_TOOLS = frozenset(
    {
        "lw_generate_mnemonic",
        "lw_import_mnemonic",
        "lw_import_descriptor",
        "btc_import_descriptor",
    }
)

WELCOME_MESSAGE = (
    "Welcome to Agentic AQUA!\n"
    "- Never share your seed phrase.\n"
    "- Use it with small amounts; it is not designed for savings.\n"
)

_shown = False


def load_logo() -> str:
    return files("aqua").joinpath("static/logo_ascii_31_chars.txt").read_text()


def render_banner() -> str:
    return f"{load_logo()}\n{WELCOME_MESSAGE}\n"


def consume_once() -> str | None:
    """Return the banner on first call in this process; ``None`` thereafter."""
    global _shown
    if _shown:
        return None
    _shown = True
    return render_banner()


def reset_for_tests() -> None:
    global _shown
    _shown = False
