"""Welcome banner shown on ``aqua --help``."""

from importlib.resources import files

WELCOME_MESSAGE = (
    "Welcome to Agentic AQUA!\n"
    "- Never share your seed phrase.\n"
    "- Use it with small amounts; it is not designed for savings.\n"
)


def load_logo() -> str:
    return files("aqua").joinpath("static/logo_ascii_31_chars.txt").read_text()


def render_banner() -> str:
    return f"{load_logo()}\n{WELCOME_MESSAGE}\n"
