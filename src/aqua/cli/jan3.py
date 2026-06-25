"""JAN3 Accounts CLI — paid captchaless login."""

from __future__ import annotations

import logging

import click

from ..tools import (
    jan3_list_sessions,
    jan3_login_complete,
    jan3_login_start,
    jan3_logout,
    jan3_session_info,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret

logger = logging.getLogger(__name__)

_PASSWORD_HELP = (
    "Read wallet password from stdin (piped) or prompt interactively. "
    "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
    "then to no password."
)


@click.group()
def jan3():
    """JAN3 Accounts — paid captchaless login.

    The flow:

      1. `aqua jan3 login-start --email …` pays the captchaless-login fee
         in L-BTC; the server emails an OTP.
      2. `aqua jan3 login-complete --email … --otp …` saves the session.

    Base URL is overridable via the AQUA_ANKARA_API_URL env var (default:
    https://test.aquabtc.com).
    """


@jan3.command("login-start")
@click.option("--email", required=True, help="JAN3 account email (will receive the OTP).")
@click.option("--wallet-name", default="default", show_default=True)
@click.option("--language", default="en", show_default=True, help="OTP email language code.")
@click.option(
    "--password-stdin", "password_stdin", is_flag=True, default=False, help=_PASSWORD_HELP
)
@click.pass_obj
def login_start(ctx, email, wallet_name, language, password_stdin):
    """Pay the captchaless-login fee and trigger the OTP email."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            jan3_login_start,
            {
                "email": email,
                "wallet_name": wallet_name,
                "password": password,
                "language": language,
            },
        ),
    )


@jan3.command("login-complete")
@click.option("--email", required=True)
@click.option("--otp", "otp_code", required=True, help="OTP code from the verification email.")
@click.option("--fingerprint", default=None, help="Optional device fingerprint string.")
@click.pass_obj
def login_complete(ctx, email, otp_code, fingerprint):
    """Exchange the OTP for JWT tokens and persist the session."""
    run_tool(
        ctx,
        lambda: jan3_login_complete(email=email, otp_code=otp_code, fingerprint=fingerprint),
    )


@jan3.command("session-info")
@click.option("--email", required=True)
@click.pass_obj
def session_info(ctx, email):
    """Show non-sensitive metadata for a persisted JAN3 session."""
    run_tool(ctx, lambda: jan3_session_info(email))


@jan3.command("list-sessions")
@click.pass_obj
def list_sessions(ctx):
    """List all persisted JAN3 sessions (no tokens shown)."""
    run_tool(ctx, lambda: jan3_list_sessions())


@jan3.command("logout")
@click.option("--email", required=True)
@click.pass_obj
def logout(ctx, email):
    """Delete a persisted JAN3 session."""
    run_tool(ctx, lambda: jan3_logout(email))
