"""Shared password/secret resolution helpers for CLI commands."""

import os
import sys
from typing import Optional

import click

from ..storage import read_password_file

# Shared by every command that takes --password-stdin.
password_file_option = click.option(
    "--password-file",
    "password_file",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Read the wallet password from this file (trailing newline stripped). "
        "Cannot be combined with --password-stdin."
    ),
)


def read_secret(prompt_label: str) -> str:
    """Read one line from piped stdin, or prompt interactively if stdin is a TTY."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    return click.prompt(prompt_label, hide_input=True)


def resolve_secret(
    prompt_label: str,
    use_stdin: bool,
    env_var: Optional[str] = None,
    required: bool = True,
    password_file: Optional[str] = None,
) -> Optional[str]:
    """Resolve a secret from --*-stdin > --password-file > env var > interactive prompt.

    - use_stdin=True      -> read_secret() (piped stdin or TTY prompt).
    - password_file set   -> read_password_file(); with use_stdin -> UsageError.
    - env_var set & value -> os.environ[env_var].strip() (whitespace-only treated as unset).
    - required=True       -> click.prompt(prompt_label, hide_input=True).
    - required=False      -> None.
    """
    if use_stdin and password_file:
        raise click.UsageError("--password-stdin and --password-file are mutually exclusive.")
    if use_stdin:
        return read_secret(prompt_label)
    if password_file:
        try:
            return read_password_file(password_file)
        except ValueError as e:
            raise click.UsageError(str(e)) from e
    if env_var:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    if required:
        return click.prompt(prompt_label, hide_input=True)
    return None


def handle_password_retry(fn, kwargs):
    """Call fn(**kwargs); if password is required and missing, prompt and retry once."""
    try:
        return fn(**kwargs)
    except ValueError as e:
        if "password required" in str(e).lower() and kwargs.get("password") is None:
            kwargs["password"] = click.prompt("Password", hide_input=True)
            return fn(**kwargs)
        raise
