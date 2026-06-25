"""AQUA account auth CLI — email-OTP login against JAN3's Ankara backend.

`login` emails a 6-digit OTP; `verify` exchanges it for a session stored locally.
This AQUA account login is a separate concern from WapuPay's API key: the WapuPay
direct-fiat commands (`aqua wapupay`) authenticate with `WAPUPAY_API_KEY`, not
with this session.
"""

from __future__ import annotations

import click

from ..tools import aqua_login, aqua_logout, aqua_session, aqua_verify
from .output import run_tool
from .password import read_secret


@click.group()
def auth():
    """AQUA account login (email OTP) via JAN3's Ankara backend."""


@auth.command("login")
@click.option("--email", required=True, help="Your AQUA account email (an OTP will be sent here).")
@click.option(
    "--language", default="en", show_default=True,
    type=click.Choice(["en", "es", "pt"]), help="OTP email language.",
)
@click.pass_obj
def login(ctx, email, language):
    """Request an OTP code by email to start an AQUA session."""
    run_tool(ctx, lambda: aqua_login(email=email, language=language))


@auth.command("verify")
@click.option("--email", required=True, help="The same email used in `login`.")
@click.option(
    "--otp-code", default=None,
    help="6-digit code from the email. Omit to be prompted / read from stdin.",
)
@click.option(
    "--otp-stdin", is_flag=True, default=False,
    help="Read the OTP code from piped stdin (or prompt interactively).",
)
@click.pass_obj
def verify(ctx, email, otp_code, otp_stdin):
    """Verify the OTP and store the AQUA session locally."""
    if otp_code is None:
        otp_code = read_secret("OTP code") if otp_stdin else click.prompt("OTP code")
    run_tool(ctx, lambda: aqua_verify(email=email, otp_code=otp_code))


@auth.command("logout")
@click.pass_obj
def logout(ctx):
    """Forget the local AQUA session."""
    run_tool(ctx, lambda: aqua_logout())


@auth.command("session")
@click.pass_obj
def session(ctx):
    """Show whether an AQUA session is active."""
    run_tool(ctx, lambda: aqua_session())
