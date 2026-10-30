# API contract for connecting the existing site backend

Do not couple the bot directly to the website database. Agree on a stable service API.

## Suggested endpoints (not implemented as money-moving endpoints)
- `GET /v1/me` — authenticated user's profile and account status.
- `GET /v1/games` — currently enabled games and rule versions.
- `GET /v1/history?cursor=...` — cursor-paginated game/payment history.
- `POST /v1/support` — support ticket.
- `POST /v1/payments/intents` — create a local payment intent; requires idempotency key.
- `GET /v1/payments/{id}` — read payment status.
- `POST /v1/webhooks/payment-provider` — платёжное уведомление, проверка подписи и сверка статуса.
- `POST /v1/games/{game}/rounds` — only after licensing/compliance and game-engine reviews.

## Required conventions
- Service-to-service authentication (short-lived credentials or signed requests).
- Use integer smallest units or decimal strings; never binary floating point for money.
- Every write endpoint accepts an idempotency key.
- Use stable internal UUIDs and provider references.
- Webhooks are asynchronous notifications, not the source of truth by themselves.
- Keep provider secrets out of logs and user-visible responses.
- Cursor pagination, API versioning and explicit error codes.
