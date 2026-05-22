"""Tests for LightningManager behavior."""

from unittest.mock import MagicMock, patch

import pytest

from aqua.lightning import LightningManager


class _StorageStub:
    def __init__(self):
        self.wallet = MagicMock(
            watch_only=False,
            encrypted_mnemonic=None,
            network="mainnet",
        )

    def load_wallet(self, wallet_name):
        return self.wallet if wallet_name == "tuna" else None

    def is_mnemonic_encrypted(self, value):
        return False

    def save_lightning_swap(self, swap):
        return None


class _WalletManagerStub:
    def get_balance(self, wallet_name):
        return [MagicMock(ticker="L-BTC", amount=10_000)]

    def send(self, wallet_name, address, amount, password=None):
        return "txid123"


class TestLightningManagerPayInvoice:
    @patch("aqua.lightning.generate_keypair", return_value=("11" * 32, "03" + "22" * 32))
    @patch("aqua.lightning.BoltzClient")
    def test_pay_invoice_accepts_uppercase_bolt11(self, mock_client_cls, _mock_keys):
        mock_client = MagicMock()
        mock_client.get_submarine_pairs.return_value = {"L-BTC": {"BTC": {"enabled": True}}}
        mock_client.create_submarine_swap.return_value = {
            "id": "swap123",
            "expectedAmount": 420,
            "address": "lq1qqexampleaddress",
            "timeoutBlockHeight": 123,
        }
        mock_client_cls.return_value = mock_client

        manager = LightningManager(_StorageStub(), _WalletManagerStub())
        invoice = (
            "LNBC4U1P4PPSS2DQVWPE82ETZVYCSNP4QGT72S92AK77WSSZT7DQS8SHKJY0RE5R8FS8TNSAY4ZG7GPJEKRR7"
            "PP5ZZZWZYNN2TDA7ZCKPLFZZWYPYJRJYJDYRU6PQPV8LJKGVX4Z2NGSSP58M0Y52ZKMU54RT7R8AJPLFZYYS"
            "JLV7NFXMNTJ3EA4T9W7UW7PE7S9QYYSGQCQZP2XQYZ5VQ4LZJZEMW8MNUFCAHKTC608TDVML9PVUVW7JXEYSL"
            "TAFET3FG3HKZ658N93F6YMH2ZS9GXVLUVYEAH4K8VQLMAH6K7SHL7C0TCCW59USPDRH0FJ"
        )

        swap = manager.pay_invoice(invoice=invoice, wallet_name="tuna")

        assert swap.swap_id == "swap123"
        called_invoice = mock_client.create_submarine_swap.call_args.args[0]
        assert called_invoice == invoice.lower()

    def test_pay_invoice_still_rejects_garbage_string(self):
        manager = LightningManager(_StorageStub(), _WalletManagerStub())
        with pytest.raises(ValueError, match="Invalid invoice"):
            manager.pay_invoice(invoice="NOT_AN_INVOICE", wallet_name="tuna")
