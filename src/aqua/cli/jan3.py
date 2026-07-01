"""JAN3 Accounts CLI — multi-account login + sessions + paid captchaless login.

Two login flows, both multi-account (one session per email):

  * Default (free email-OTP):
      1. `aqua jan3 login --email …` emails an OTP.
      2. `aqua jan3 verify --email … --otp …` saves the session.
  * Fallback (paid captchaless), for accounts that can't use the free flow:
      1. `aqua jan3 login-start --email …` pays the fee in L-BTC; emails an OTP.
      2. `aqua jan3 login-complete --email … --otp …` saves the session.
"""

from __future__ import annotations

import logging

import click

from ..tools import (
    jan3_list_sessions,
    jan3_login,
    jan3_login_complete,
    jan3_login_start,
    jan3_logout,
    jan3_session_info,
    jan3_verify,
)
from .output import run_tool
from .password import handle_password_retry, read_secret, resolve_secret

logger = logging.getLogger(__name__)

_PASSWORD_HELP = (
    "Read wallet password from stdin (piped) or prompt interactively. "
    "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
    "then to no password."
)


@click.group()
def jan3():
    """JAN3 account login + sessions (multi-account).

    Default flow (free email-OTP): `aqua jan3 login` → `aqua jan3 verify`.
    Fallback flow (paid captchaless): `aqua jan3 login-start` → `login-complete`.
    """


@jan3.command("login")
@click.option("--email", required=True, help="Your JAN3 account email (an OTP will be sent here).")
@click.option(
    "--language", default="en", show_default=True,
    type=click.Choice(["en", "es", "pt"]), help="OTP email language.",
)
@click.pass_obj
def login(ctx, email, language):
    """Request an OTP code by email to start a JAN3 session (free, default flow)."""
    run_tool(ctx, lambda: jan3_login(email=email, language=language))


@jan3.command("verify")
@click.option("--email", required=True, help="The same email used in `login`.")
@click.option(
    "--otp", "otp_code", default=None,
    help="6-digit code from the email. Omit to be prompted / read from stdin.",
)
@click.option(
    "--otp-stdin", is_flag=True, default=False,
    help="Read the OTP code from piped stdin (or prompt interactively).",
)
@click.option("--fingerprint", default=None, help="Optional device fingerprint string.")
@click.pass_obj
def verify(ctx, email, otp_code, otp_stdin, fingerprint):
    """Verify the OTP and store the JAN3 session locally."""
    if otp_code is None:
        otp_code = read_secret("OTP code") if otp_stdin else click.prompt("OTP code")
    run_tool(ctx, lambda: jan3_verify(email=email, otp_code=otp_code, fingerprint=fingerprint))


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
