from app.payments import deposit_store


def test_stars_donation_receipt_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    assert deposit_store.record_stars_donation(77, "charge-1", 50, "nova_donation:77:50") is True
    assert deposit_store.record_stars_donation(77, "charge-1", 50, "nova_donation:77:50") is False


def test_stars_donation_rejects_invalid_amount(tmp_path, monkeypatch):
    monkeypatch.setattr(deposit_store, "DB_PATH", str(tmp_path / "payments.sqlite3"))
    assert deposit_store.record_stars_donation(77, "charge-2", 999, "nova_donation:77:999") is False
