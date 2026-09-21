"""PIX → DePix on-ramp through AQUA's Ankara backend.

All API traffic in this module targets ``ANKARA_API_URL``.  Ankara owns the
Eulen credentials, EUID mapping, and Noviuz reconciliation; this client never
calls Eulen or the Noviuz API directly and never accepts identity PII.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from typing import Any, Callable, Optional

from .ankara import (
    ANKARA_API_URL,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
    SessionExpiredError,
    _redact,
    _scrub_text,
)

EULEN_BASE_PATH = "/api/v1/eulen"
TERMINAL_STATUSES = frozenset(
    {"depix_sent", "canceled", "error", "refunded", "expired"}
)
KNOWN_STATUSES = frozenset(
    {
        "pending",
        "pending_pix2fa",
        "verified_pix2fa",
        "under_review",
        "delayed",
        *TERMINAL_STATUSES,
    }
)

_STATUS_MESSAGES = {
    "pending": "Waiting for the PIX payment.",
    "pending_pix2fa": "The deposit is waiting for PIX 2FA verification.",
    "verified_pix2fa": "PIX 2FA was verified; settlement is still pending.",
    "under_review": "The payment is under compliance review.",
    "delayed": "The DePix transfer is delayed and still processing.",
    "depix_sent": "DePix was delivered to the Liquid wallet.",
    "canceled": "The deposit was canceled.",
    "error": "The deposit failed.",
    "refunded": "The PIX payment was refunded; no DePix was issued.",
    "expired": "The PIX charge expired before payment.",
}


def format_brl(amount_cents: int) -> str:
    """Render integer BRL cents using Brazilian formatting."""
    integer, fraction = divmod(amount_cents, 100)
    return f"R${integer:,}".replace(",", ".") + f",{fraction:02d}"


class EulenAPIError(ValueError):
    """Structured error returned by Ankara's Eulen API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        details: Any = None,
    ) -> None:
        self.code = code
        self.status = status
        self.details = details
        super().__init__(message)


@dataclass
class PixSwap:
    """Locally persisted metadata for an Ankara Eulen deposit."""

    swap_id: str
    amount_cents: int
    account_email: str
    wallet_name: str
    depix_address: str
    qr_copy_paste: str
    status: str
    network: str
    created_at: str
    qr_image_url: Optional[str] = None
    blockchain_txid: Optional[str] = None
    fee_cents: Optional[int] = None
    net_amount_cents: Optional[int] = None
    eulen_deposit_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PixSwap":
        """Load a persisted record, ignoring unknown keys."""
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


class EulenClient:
    """Stateless HTTP client for Ankara's authenticated Eulen endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or ANKARA_API_URL).rstrip("/")
        self.access_token = access_token

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            cleaned = {key: value for key, value in query.items() if value is not None}
            if cleaned:
                url += "?" + urllib.parse.urlencode(cleaned)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.access_token:
                raise ValueError("Authenticated Eulen request requires a JAN3 access token")
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read().decode()
                parsed = json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode()
            except Exception:
                pass
            try:
                error = json.loads(raw) if raw.strip() else {}
            except (TypeError, ValueError):
                error = {}
            if exc.code == 401:
                raise SessionExpiredError(
                    f"AQUA session token rejected (401 {method} {path})"
                ) from exc
            code = str(error.get("error_code") or f"HTTP_{exc.code}")
            message = error.get("message") or f"AQUA Eulen API error ({exc.code})"
            raise EulenAPIError(
                code,
                _scrub_text(str(message)),
                status=exc.code,
                details=_redact(error.get("details")),
            ) from exc
        except urllib.error.URLError as exc:
            raise ValueError(
                f"AQUA Eulen API unreachable ({method} {path}): {exc.reason}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"AQUA Eulen API returned invalid JSON ({method} {path})"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"AQUA Eulen API returned a non-object ({method} {path})")
        return parsed

    def create_kyc_session(self) -> dict[str, Any]:
        return self._api_request("POST", f"{EULEN_BASE_PATH}/kyc/session/", body={})

    def confirm_kyc_session(self, session_id: str) -> dict[str, Any]:
        return self._api_request(
            "POST",
            f"{EULEN_BASE_PATH}/kyc/confirm/",
            body={"session_id": session_id},
        )

    def fee_for_gross(self, amount_cents: int) -> dict[str, Any]:
        return self._api_request(
            "GET",
            f"{EULEN_BASE_PATH}/pix-depix-fee/",
            query={"gross_amount_brl_cents": amount_cents},
        )

    def create_deposit(self, amount_cents: int, depix_address: str) -> dict[str, Any]:
        return self._api_request(
            "POST",
            f"{EULEN_BASE_PATH}/deposit/",
            body={
                "amount_brl_cents": amount_cents,
                "liquid_depix_address": depix_address,
            },
        )

    def list_deposits(
        self,
        *,
        deposit_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._api_request(
            "GET",
            f"{EULEN_BASE_PATH}/deposits/",
            query={
                "deposit_id": deposit_id,
                "date_from": date_from,
                "date_to": date_to,
                "status": status,
            },
        )


class PixManager:
    """Wallet-aware PIX orchestration using JAN3 authentication."""

    def __init__(
        self,
        storage,
        wallet_manager,
        jan3_manager,
        base_url: Optional[str] = None,
    ) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager
        self.jan3 = jan3_manager
        self.base_url = (base_url or ANKARA_API_URL).rstrip("/")

    @staticmethod
    def _normalize_email(email: str) -> str:
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("email is required")
        return email

    def _with_auth(
        self,
        email: str,
        call: Callable[[EulenClient], dict[str, Any]],
    ) -> dict[str, Any]:
        email = self._normalize_email(email)
        return self.jan3.with_auth_retry(
            email,
            lambda token: call(EulenClient(self.base_url, token)),
        )

    def create_kyc_session(self, email: str) -> dict[str, Any]:
        email = self._normalize_email(email)
        result = self._with_auth(email, lambda client: client.create_kyc_session())
        if not result.get("session_id") or not result.get("status"):
            raise ValueError("AQUA KYC session response is missing required fields")
        return {
            **result,
            "email": email,
            "next_step": (
                "Open Noviuz Hosted KYC with session_id and operator_id. "
                "After the user finishes, call eulen_kyc_confirm. "
                "Only Ankara's confirm result is authoritative."
            ),
        }

    def confirm_kyc_session(self, email: str, session_id: str) -> dict[str, Any]:
        email = self._normalize_email(email)
        session_id = (session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        result = self._with_auth(
            email, lambda client: client.confirm_kyc_session(session_id)
        )
        if not result.get("session_id") or not result.get("session_status"):
            raise ValueError("AQUA KYC confirm response is missing required fields")
        status = result["session_status"]
        if status == "approved" and result.get("verification_status") == "VERIFIED":
            next_step = "KYC approved. You can now call pix_receive."
        elif status in {"created", "under_review"}:
            next_step = "KYC is not final. Retry eulen_kyc_confirm later."
        else:
            next_step = f"KYC ended with status {status!r}; do not create a deposit."
        return {**result, "email": email, "next_step": next_step}

    def create_deposit(
        self,
        email: str,
        amount_cents: int,
        wallet_name: str = "default",
    ) -> PixSwap:
        email = self._normalize_email(email)
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
            raise ValueError("amount_cents must be an integer (100 = R$1.00)")
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        wallet = self.storage.load_wallet(wallet_name)
        if wallet is None:
            raise ValueError(f"Wallet {wallet_name!r} not found")
        if wallet.network != "mainnet":
            raise ValueError("PIX → DePix is only available on Liquid mainnet")

        # Validate the session and retrieve the authoritative fee before consuming
        # a receive index.
        fee = self._with_auth(
            email, lambda client: client.fee_for_gross(amount_cents)
        )
        net_amount = fee.get("net_amount_brl_cents")
        charges = fee.get("extra_charges_brl_cents")
        if not isinstance(net_amount, int) or not isinstance(charges, int):
            raise ValueError("AQUA fee response is missing integer amount fields")

        depix_address = self.wallet_manager.get_address(wallet_name).address
        result = self._with_auth(
            email,
            lambda client: client.create_deposit(amount_cents, depix_address),
        )
        deposit_id = result.get("deposit_id")
        qr_copy_paste = result.get("qr_copy_paste")
        if not isinstance(deposit_id, int) or not qr_copy_paste:
            raise ValueError("AQUA deposit response is missing deposit_id or QR data")
        swap = PixSwap(
            swap_id=str(deposit_id),
            amount_cents=amount_cents,
            account_email=email,
            wallet_name=wallet_name,
            depix_address=depix_address,
            qr_copy_paste=str(qr_copy_paste),
            qr_image_url=result.get("qr_image_url"),
            status="pending",
            network=wallet.network,
            created_at=datetime.now(UTC).isoformat(),
            fee_cents=charges,
            net_amount_cents=net_amount,
        )
        self.storage.save_pix_swap(swap)
        return swap

    def _upsert_deposit_rows(
        self, email: str, payload: dict[str, Any]
    ) -> list[PixSwap]:
        rows = payload.get("deposits")
        if not isinstance(rows, list):
            raise ValueError("AQUA deposits response is missing the deposits list")

        swaps: list[PixSwap] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("AQUA deposits response contains an invalid row")
            deposit_id = row.get("deposit_id")
            amount_cents = row.get("amount_brl_cents")
            status = row.get("status")
            if (
                not isinstance(deposit_id, int)
                or isinstance(deposit_id, bool)
                or not isinstance(amount_cents, int)
                or isinstance(amount_cents, bool)
                or status not in KNOWN_STATUSES
            ):
                raise ValueError("AQUA deposits response contains invalid deposit fields")

            local = self.storage.load_pix_swap(str(deposit_id))
            if local is not None and local.account_email.casefold() != email.casefold():
                raise ValueError(
                    f"PIX deposit {deposit_id} belongs to a different JAN3 account"
                )

            swap = PixSwap(
                swap_id=str(deposit_id),
                eulen_deposit_id=(
                    str(row["eulen_deposit_id"])
                    if row.get("eulen_deposit_id")
                    else None
                ),
                amount_cents=amount_cents,
                account_email=email,
                wallet_name=local.wallet_name if local is not None else "",
                depix_address=str(row.get("depix_address") or ""),
                qr_copy_paste=str(row.get("qr_copy_paste") or ""),
                qr_image_url=row.get("qr_image_url") or None,
                status=str(status),
                network="mainnet",
                created_at=str(row.get("created") or ""),
                blockchain_txid=(
                    str(row["blockchain_tx_id"])
                    if row.get("blockchain_tx_id")
                    else None
                ),
                fee_cents=local.fee_cents if local is not None else None,
                net_amount_cents=(
                    local.net_amount_cents if local is not None else None
                ),
            )
            self.storage.save_pix_swap(swap)
            swaps.append(swap)
        return swaps

    @staticmethod
    def _status_response(swap: PixSwap) -> dict[str, Any]:
        response: dict[str, Any] = {
            "swap_id": swap.swap_id,
            "deposit_id": int(swap.swap_id),
            "status": swap.status,
            "amount_cents": swap.amount_cents,
            "amount_brl": format_brl(swap.amount_cents),
            "wallet_name": swap.wallet_name,
            "depix_address": swap.depix_address,
            "network": swap.network,
            "message": _STATUS_MESSAGES[swap.status],
        }
        if swap.eulen_deposit_id:
            response["eulen_deposit_id"] = swap.eulen_deposit_id
        if swap.blockchain_txid:
            response["blockchain_txid"] = swap.blockchain_txid
        response["created_at"] = swap.created_at
        return response

    def list_deposits(
        self,
        email: str,
        *,
        deposit_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        email = self._normalize_email(email)
        if deposit_id is not None and (
            not isinstance(deposit_id, int)
            or isinstance(deposit_id, bool)
            or deposit_id <= 0
        ):
            raise ValueError("deposit_id must be a positive integer")
        if status is not None and status not in KNOWN_STATUSES:
            raise ValueError(f"Unknown PIX deposit status: {status!r}")

        parsed_dates: dict[str, date] = {}
        for name, value in (("date_from", date_from), ("date_to", date_to)):
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{name} must use YYYY-MM-DD format")
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{name} must use YYYY-MM-DD format") from exc
            if parsed.isoformat() != value:
                raise ValueError(f"{name} must use YYYY-MM-DD format")
            parsed_dates[name] = parsed
        if (
            "date_from" in parsed_dates
            and "date_to" in parsed_dates
            and parsed_dates["date_from"] > parsed_dates["date_to"]
        ):
            raise ValueError("date_to must be on or after date_from")

        payload = self._with_auth(
            email,
            lambda client: client.list_deposits(
                deposit_id=deposit_id,
                date_from=date_from,
                date_to=date_to,
                status=status,
            ),
        )
        swaps = self._upsert_deposit_rows(email, payload)
        return {
            "email": email,
            "count": len(swaps),
            "deposits": [self._status_response(swap) for swap in swaps],
        }

    def get_deposit_status(self, swap_id: str, email: str) -> dict[str, Any]:
        swap_id = str(swap_id).strip()
        email = self._normalize_email(email)
        try:
            deposit_id = int(swap_id)
        except ValueError as exc:
            raise ValueError("PIX swap id is not an Ankara deposit id") from exc
        if deposit_id <= 0:
            raise ValueError("PIX swap id must be a positive Ankara deposit id")

        local = self.storage.load_pix_swap(swap_id)
        if local is not None and local.account_email.casefold() != email.casefold():
            raise ValueError(
                f"PIX deposit {local.swap_id} belongs to a different JAN3 account"
            )

        result = self.list_deposits(email, deposit_id=deposit_id)
        if not result["deposits"]:
            raise ValueError(f"PIX deposit not found in Ankara: {swap_id}")
        return result["deposits"][0]
