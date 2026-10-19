"""Eulen PIX → DePix commands backed exclusively by Ankara."""

from __future__ import annotations

import sys
import time

import click

from ..pix import (
    KNOWN_STATUSES,
    KYC_MAX_AMOUNT_CENTS,
    KYC_POLL_SECONDS,
    KYC_SESSION_TTL,
    KYC_VERIFICATION_METHODS,
)
from ..tools import (
    eulen_kyc_confirm,
    eulen_kyc_session,
    pix_list,
    pix_receive,
    pix_status,
)
from .output import run_tool


def _run_eulen_tool(ctx, call):
    """Render a PIX tool result and fail on a returned error envelope."""
    result = run_tool(ctx, call)
    if "error" in result:
        sys.exit(1)
    return result


@click.group()
def eulen():
    """PIX → DePix via Ankara, including hosted KYC."""


@eulen.command("kyc-session")
@click.option("--email", required=True, help="Logged-in JAN3 account email.")
@click.option(
    "--method",
    "methods",
    multiple=True,
    type=click.Choice(KYC_VERIFICATION_METHODS),
    help="Verification method (repeatable): otp (free, default) and/or pix.",
)
@click.option(
    "--amount-cents",
    type=click.IntRange(min=1, max=KYC_MAX_AMOUNT_CENTS),
    help="Required with --method pix: BRL cents paid and returned as DePix.",
)
@click.option(
    "--wallet-name",
    default="default",
    show_default=True,
    help="Liquid wallet that receives the DePix for --method pix.",
)
@click.pass_obj
def kyc_session(ctx, email, methods, amount_cents, wallet_name):
    """Create or reuse a Noviuz Hosted KYC session."""
    _run_eulen_tool(
        ctx,
        lambda: eulen_kyc_session(
            email=email,
            verification_methods=list(methods) or ["otp"],
            amount_cents=amount_cents,
            wallet_name=wallet_name,
        ),
    )


def _confirm_until_settled(email: str, session_id: str) -> dict:
    """Poll kyc-confirm while the session is 'created' or Ankara rate-limits us."""
    deadline = time.monotonic() + KYC_SESSION_TTL.total_seconds()
    while True:
        result = eulen_kyc_confirm(email=email, session_id=session_id)
        if "error" in result:
            delay = result["error"].get("retry_after_seconds")
            if delay is None:
                return result
        elif result.get("session_status") != "created":
            return result
        else:
            delay = result.get("poll_after_seconds") or KYC_POLL_SECONDS
        if time.monotonic() + delay > deadline:
            return result
        click.echo(f"KYC still pending; checking again in {delay}s...", err=True)
        time.sleep(delay)


@eulen.command("kyc-confirm")
@click.option("--email", required=True, help="JAN3 account email that owns the session.")
@click.option("--session-id", required=True, help="Opaque id returned by kyc-session.")
@click.option(
    "--wait",
    is_flag=True,
    help="Poll every 15s while the session is 'created' (up to 30 minutes).",
)
@click.pass_obj
def kyc_confirm(ctx, email, session_id, wait):
    """Reconcile hosted KYC through Ankara."""
    if wait:
        _run_eulen_tool(ctx, lambda: _confirm_until_settled(email, session_id))
        return
    _run_eulen_tool(ctx, lambda: eulen_kyc_confirm(email=email, session_id=session_id))


@eulen.command("receive")
@click.option("--email", required=True, help="Logged-in JAN3 account with approved KYC.")
@click.option(
    "--amount-cents",
    required=True,
    type=click.IntRange(min=1),
    help="Gross PIX amount in BRL cents (100 = R$1.00).",
)
@click.option("--wallet-name", default="default", show_default=True)
@click.pass_obj
def receive(ctx, email, amount_cents, wallet_name):
    """Create a PIX charge that pays DePix to the Liquid wallet."""
    _run_eulen_tool(
        ctx,
        lambda: pix_receive(
            email=email,
            amount_cents=amount_cents,
            wallet_name=wallet_name,
        ),
    )


@eulen.command("list")
@click.option("--email", required=True, help="JAN3 account email that owns the deposits.")
@click.option("--deposit-id", type=click.IntRange(min=1), help="Filter by Ankara deposit id.")
@click.option("--date-from", help="Include deposits on or after this UTC date (YYYY-MM-DD).")
@click.option("--date-to", help="Include deposits on or before this UTC date (YYYY-MM-DD).")
@click.option(
    "--status",
    type=click.Choice(sorted(KNOWN_STATUSES)),
    help="Filter by deposit status.",
)
@click.pass_obj
def list_deposits(ctx, email, deposit_id, date_from, date_to, status):
    """List deposits from Ankara using optional filters."""
    _run_eulen_tool(
        ctx,
        lambda: pix_list(
            email=email,
            deposit_id=deposit_id,
            date_from=date_from,
            date_to=date_to,
            status=status,
        ),
    )


@eulen.command("status")
@click.option("--swap-id", required=True, help="Ankara deposit id returned by receive.")
@click.option("--email", required=True, help="JAN3 account email that owns the deposit.")
@click.pass_obj
def status(ctx, swap_id, email):
    """Refresh a PIX → DePix deposit status."""
    _run_eulen_tool(ctx, lambda: pix_status(swap_id=swap_id, email=email))
