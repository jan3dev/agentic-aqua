"""Output formatting for CLI commands."""

import json
import sys


def _detect_format(fmt: str | None) -> str:
    """Detect output format: explicit flag wins, otherwise TTY-aware."""
    if fmt is not None:
        return fmt
    return "pretty" if sys.stdout.isatty() else "json"


def render(data: dict, fmt: str | None = None) -> str:
    """Render a tool result dict for the terminal."""
    fmt = _detect_format(fmt)
    if fmt == "json":
        return json.dumps(data, indent=2)
    # Pretty: key-value pairs, nested dicts/lists indented
    return _pretty(data, indent=0)


def _pretty(obj, indent: int = 0) -> str:
    """Recursively format a dict/list for human reading."""
    prefix = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            label = key.replace("_", " ").title()
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{label}:")
                lines.append(_pretty(value, indent + 1))
            else:
                lines.append(f"{prefix}{label}: {value}")
    elif isinstance(obj, list):
        if not obj:
            lines.append(f"{prefix}(none)")
        for i, item in enumerate(obj):
            if isinstance(item, dict):
                if i > 0:
                    lines.append(f"{prefix}---")
                lines.append(_pretty(item, indent))
            else:
                lines.append(f"{prefix}- {item}")
    else:
        lines.append(f"{prefix}{obj}")
    return "\n".join(lines)


def render_error(code: str, message: str, fmt: str | None = None) -> str:
    """Render an error matching the MCP error JSON shape."""
    fmt = _detect_format(fmt)
    error = {"error": {"code": code, "message": message}}
    if fmt == "json":
        return json.dumps(error, indent=2)
    return f"Error [{code}]: {message}"
