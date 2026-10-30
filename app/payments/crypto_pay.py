import hashlib
import hmac

def verify_crypto_pay_signature(raw_body: bytes, signature: str, api_token: str) -> bool:
    """Validate Crypto Pay webhook signature over exact raw request bytes.

    Crypto Pay's secret key for this check is SHA256(api_token); signature is
    HMAC-SHA-256 of the raw body. This verifies authenticity only, not payment finality.
    """
    if not raw_body or not signature or not api_token:
        return False
    secret = hashlib.sha256(api_token.encode("utf-8")).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
