from decimal import Decimal

from app.payments import deposit_store


def test_paid_invoice_credits_once(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    deposit_store.init_store()
    deposit_store.save_invoice("12345", 77, Decimal("0.50"))
    assert deposit_store.mark_paid_and_credit("12345") is True
    assert deposit_store.mark_paid_and_credit("12345") is False
    assert deposit_store.get_balance(77) == Decimal("0.50")


def test_invoice_cannot_credit_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    deposit_store.init_store()
    assert deposit_store.mark_paid_and_credit("unknown") is False


def test_pending_deposits_returns_only_uncredited_invoices(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    deposit_store.init_store()
    deposit_store.save_invoice("pending-1", 88, Decimal("0.50"))
    assert [row["invoice_id"] for row in deposit_store.get_pending_deposits()] == ["pending-1"]
    assert deposit_store.mark_paid_and_credit("pending-1") is True
    assert deposit_store.get_pending_deposits() == []


def test_confirmed_deposit_updates_persistent_profile_xp(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    deposit_store.init_store()
    deposit_store.save_invoice("xp-invoice", 9001, Decimal("5.00"))
    assert deposit_store.mark_paid_and_credit("xp-invoice") is True
    stats = deposit_store.get_profile_stats(9001)
    assert stats["total_deposits"] == Decimal("5.00")
    assert stats["xp"] == 500
    # Polling the same invoice twice cannot award duplicate XP.
    assert deposit_store.mark_paid_and_credit("xp-invoice") is False
    assert deposit_store.get_profile_stats(9001)["xp"] == 500


def test_owner_turnover_is_usd_and_profile_stats_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    stats = deposit_store.get_profile_stats(844181709)
    assert stats["turnover"] == Decimal("70075.00")
    assert stats["level"] == "Грандмастер"
    assert stats["xp"] == 0  # monetary turnover must not be copied into XP
