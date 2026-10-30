"""TON deposits are deliberately unavailable until a trusted verifier is configured.

Never accept a transaction hash or a screenshot as proof of payment. A production
implementation must verify network, recipient, asset, exact amount, success/finality,
unique payment reference and replay protection with a trusted TON indexer.
"""


class TonDepositVerifier:
    async def verify(self, transaction_id: str):
        if not transaction_id or not transaction_id.strip():
            raise ValueError("transaction_id is required")
        raise RuntimeError(
            "TON deposits are disabled: trusted transaction verification has not been configured"
        )
