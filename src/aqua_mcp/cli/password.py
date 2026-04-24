"""Shared password-retry helper for CLI commands."""

import sys

import click


def read_secret(prompt_label: str) -> str:
    """Read one line from piped stdin, or prompt interactively if stdin is a TTY."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    return click.prompt(prompt_label, hide_input=True)


def handle_password_retry(fn, kwargs):
    """Call fn(**kwargs); if password is required and missing, prompt and retry once."""
    try:
        return fn(**kwargs)
    except ValueError as e:
        if "password required" in str(e).lower() and kwargs.get("password") is None:
            kwargs["password"] = click.prompt("Password", hide_input=True)
            return fn(**kwargs)
        raise
