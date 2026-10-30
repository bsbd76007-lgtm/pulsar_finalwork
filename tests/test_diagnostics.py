from pathlib import Path

from app.diagnostics import run_diagnostics


def test_diagnostics_reports_social_card_inventory():
    checks = run_diagnostics(Path(__file__).resolve().parents[1])
    cards = next(item for item in checks if item["name"] == "Социальные карточки")
    assert cards["status"] == "OK"
    assert "20/20" in cards["detail"]


def test_diagnostics_does_not_expose_secret_values():
    checks = run_diagnostics(Path(__file__).resolve().parents[1])
    output = repr(checks)
    assert "BOT_TOKEN=" not in output
    assert "Crypto-Pay-API-Token" not in output
