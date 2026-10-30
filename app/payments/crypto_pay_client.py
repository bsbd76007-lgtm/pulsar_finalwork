from decimal import Decimal
import httpx


class CryptoPayClient:
    """Async Crypto Pay API client. Never trusts client-side payment claims."""
    def __init__(self, token: str, base_url: str, enabled: bool = False):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.enabled = enabled

    async def _request(self, method: str, payload: dict | None = None) -> dict:
        if not self.enabled:
            raise RuntimeError("Payments are disabled")
        if not self.token:
            raise RuntimeError("Crypto Pay API token is not configured")
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/{method}",
                headers={"Crypto-Pay-API-Token": self.token},
                json=payload or {},
            )
            response.raise_for_status()
            data = response.json()
        if data.get("ok") is not True or not isinstance(data.get("result"), (dict, list)):
            raise RuntimeError("Crypto Pay returned an unexpected response")
        return data["result"]

    async def create_invoice(self, asset: str, amount: Decimal, description: str, payload: str | None = None) -> dict:
        if amount <= 0:
            raise ValueError("amount must be positive")
        body = {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": f"{amount:.2f}",
            "accepted_assets": asset,
            "description": description[:1024],
            "payload": payload or "",
            "allow_comments": False,
            "allow_anonymous": False,
        }
        return await self._request("createInvoice", body)

    async def get_invoice(self, invoice_id: str | int) -> dict | None:
        result = await self._request("getInvoices", {"invoice_ids": str(invoice_id)})
        if isinstance(result, dict):
            items = result.get("items", [])
        else:
            items = result
        return items[0] if items else None

    async def transfer(self, user_id: int, asset: str, amount: Decimal, spend_id: str, comment: str) -> dict:
        """Transfer Crypto Pay funds to a Telegram user. API token needs transfer permissions."""
        if user_id <= 0 or amount <= 0:
            raise ValueError("user_id and amount must be positive")
        return await self._request("transfer", {
            "user_id": int(user_id), "asset": asset.upper(),
            "amount": f"{amount:.2f}", "spend_id": spend_id,
            "comment": comment[:128],
            "disable_send_notification": False,
        })
