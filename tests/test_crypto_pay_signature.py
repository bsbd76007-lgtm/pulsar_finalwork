import hashlib
import hmac
from app.payments.crypto_pay import verify_crypto_pay_signature

def sign(body: bytes, token: str) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()

def test_valid_signature():
    body = b'{"update_type":"invoice_paid","payload":{"invoice_id":123}}'
    token = "test-token"
    assert verify_crypto_pay_signature(body, sign(body, token), token)

def test_tampered_body_fails():
    token = "test-token"
    body = b'{"ok":true}'
    assert not verify_crypto_pay_signature(body + b' ', sign(body, token), token)

def test_wrong_token_fails():
    body = b'{"ok":true}'
    assert not verify_crypto_pay_signature(body, sign(body, "token-a"), "token-b")

def test_missing_inputs_fail():
    assert not verify_crypto_pay_signature(b"", "abc", "token")
    assert not verify_crypto_pay_signature(b"body", "", "token")
    assert not verify_crypto_pay_signature(b"body", "sig", "")
