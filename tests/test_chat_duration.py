from app.chat_utils import _parse_duration_minutes

def test_parse_russian_compound_duration():
    assert _parse_duration_minutes("20д 2ч") == 20 * 1440 + 120
    assert _parse_duration_minutes("2ч 30м") == 150
    assert _parse_duration_minutes("15м") == 15

def test_parse_duration_rejects_reason_and_caps():
    assert _parse_duration_minutes("2ч спам") is None
    assert _parse_duration_minutes("40д") == 30 * 24 * 60
