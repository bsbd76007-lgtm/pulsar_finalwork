import json
from fastapi import FastAPI, Request, HTTPException
from app.config import settings
from app.payments.crypto_pay import verify_crypto_pay_signature

app = FastAPI(title="PULLSAR CASINO API", version="0.2.0")

@app.get("/health")
async def health():
    return {"ok": True, "service": "pullsar-casino-api", "payments_enabled": bool(settings.payments_enabled and settings.crypto_pay_api_token)}

@app.get("/v1/public/config")
async def public_config():
    return {
        "brand": "PULLSAR CASINO",
        "payment_methods": ["Crypto Pay"] if settings.payments_enabled and settings.crypto_pay_api_token else [],
        "payments_enabled": bool(settings.payments_enabled and settings.crypto_pay_api_token),
        "deposit_amounts_usd": ["0.50", "1.00", "5.00", "10.00", "25.00", "50.00"],
        "notice": "Пополнение доступно на $0.50, $1, $5, $10, $25 и $50; вывод средств и ставки отключены."
    }

@app.post("/webhooks/crypto-pay")
async def crypto_pay_webhook(request: Request):
    # Always read the exact raw bytes for signature validation.
    body = await request.body()
    signature = request.headers.get("crypto-pay-api-signature", "")
    token = settings.crypto_pay_api_token
    if not token or not verify_crypto_pay_signature(body, signature, token):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    # Safe by design: do not credit balance from webhook alone.
    # Production flow: parse event, deduplicate update/invoice ID, query official API
    # server-to-server, validate paid status/asset/amount/user binding, then post ledger
    # entries transactionally with an idempotency key.
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    return {
        "ok": True,
        "status": "accepted_for_review",
        "event_type": event.get("update_type", "unknown"),
        "credited": False
    }
