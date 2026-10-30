from decimal import Decimal
from app.payments import deposit_store


def test_withdrawal_reserves_balance_once_and_finishes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "withdrawals.sqlite3"))
    deposit_store.init_store()
    deposit_store.init_withdrawal_store()
    with deposit_store._connect() as conn:
        conn.execute("INSERT INTO users(telegram_id,balance_usd) VALUES (?,?)", (123, "10.00"))
    assert deposit_store.reserve_withdrawal("w1", 123, 456, Decimal("5.00")) is True
    assert deposit_store.get_balance(123) == Decimal("5.00")
    assert deposit_store.reserve_withdrawal("w1", 123, 456, Decimal("5.00")) is False
    assert deposit_store.finish_withdrawal("w1", "paid", transfer_id="tr1") is True
    assert deposit_store.finish_withdrawal("w1", "paid", transfer_id="tr1") is False
    assert deposit_store.get_balance(123) == Decimal("5.00")


def test_definitive_failure_can_refund_once(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "withdrawals.sqlite3"))
    deposit_store.init_store()
    deposit_store.init_withdrawal_store()
    with deposit_store._connect() as conn:
        conn.execute("INSERT INTO users(telegram_id,balance_usd) VALUES (?,?)", (123, "10.00"))
    assert deposit_store.reserve_withdrawal("w2", 123, 456, Decimal("5.00"))
    assert deposit_store.finish_withdrawal("w2", "failed", error="rejected", refund=True)
    assert deposit_store.get_balance(123) == Decimal("10.00")
    assert deposit_store.finish_withdrawal("w2", "failed", refund=True) is False


def test_withdrawal_rejects_insufficient_balance(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "withdrawals.sqlite3"))
    deposit_store.init_store()
    deposit_store.init_withdrawal_store()
    with deposit_store._connect() as conn:
        conn.execute("INSERT INTO users(telegram_id,balance_usd) VALUES (?,?)", (123, "1.00"))
    assert deposit_store.reserve_withdrawal("w3", 123, 456, Decimal("5.00")) is False
    assert deposit_store.get_balance(123) == Decimal("1.00")
