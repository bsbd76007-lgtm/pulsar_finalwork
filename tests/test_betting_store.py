from decimal import Decimal
import importlib


def test_bet_debit_settle_once_and_refund(tmp_path, monkeypatch):
    import app.payments.deposit_store as store
    monkeypatch.setattr(store, 'DB_PATH', str(tmp_path / 'test.sqlite3'))
    store.init_store()
    # seed wallet through the same ledger schema
    with store._connect() as conn:
        conn.execute("INSERT INTO users(telegram_id,balance_usd) VALUES (7,'5.00')")
    assert store.place_bet('round1', 7, 'dice', Decimal('1.00')) is True
    assert store.get_balance(7) == Decimal('4.00')
    settled = store.settle_bet('round1', 6, Decimal('5.00'))
    assert settled['payout'] == Decimal('5.00')
    assert store.settle_bet('round1', 6, Decimal('5.00')) is None
    assert store.get_balance(7) == Decimal('9.01')
    assert store.place_bet('round2', 7, 'dice', Decimal('2.00')) is True
    assert store.refund_bet('round2') is True
    assert store.refund_bet('round2') is False
    assert store.get_balance(7) == Decimal('9.01')


def test_cannot_bet_more_than_balance(tmp_path, monkeypatch):
    import app.payments.deposit_store as store
    monkeypatch.setattr(store, 'DB_PATH', str(tmp_path / 'test.sqlite3'))
    store.init_store()
    assert store.place_bet('round-low', 9, 'dice', Decimal('0.50')) is False
    assert store.get_balance(9) == Decimal('0.00')
