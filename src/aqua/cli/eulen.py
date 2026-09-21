"""Eulen PIX → DePix commands backed exclusively by Ankara."""

from __future__ import annotations

import sys

import click

from ..pix import KNOWN_STATUSES
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
@click.pass_obj
def kyc_session(ctx, email):
    """Create or reuse a Noviuz Hosted KYC session."""
    _run_eulen_tool(ctx, lambda: eulen_kyc_session(email=email))


@eulen.command("kyc-confirm")
@click.option("--email", required=True, help="JAN3 account email that owns the session.")
@click.option("--session-id", required=True, help="Opaque id returned by kyc-session.")
@click.pass_obj
def kyc_confirm(ctx, email, session_id):
    """Reconcile hosted KYC through Ankara."""
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
