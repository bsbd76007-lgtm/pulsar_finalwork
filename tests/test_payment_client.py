import asyncio
from decimal import Decimal
import pytest
from app.payments.crypto_pay_client import CryptoPayClient

def test_invoice_disabled_by_default():
    client = CryptoPayClient(token="token", base_url="https://example.invalid/api", enabled=False)
    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(client.create_invoice("TON", Decimal("1"), "test"))

def test_invoice_rejects_non_positive_amount():
    client = CryptoPayClient(token="token", base_url="https://example.invalid/api", enabled=True)
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(client.create_invoice("TON", Decimal("0"), "test"))

def test_invoice_requires_token():
    client = CryptoPayClient(token="", base_url="https://example.invalid/api", enabled=True)
    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(client.create_invoice("TON", Decimal("1"), "test"))
