from app import ai_memory


def test_shared_memory_persists_and_skips_secret_text(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_memory, "DB_PATH", str(tmp_path / "memory.sqlite3"))
    ai_memory.remember(-1001, "Первая группа", 12, "user_a", "Вчера мы нашли смешного кота в игре")
    ai_memory.remember(-1002, "Вторая группа", 13, "user_b", "мой API key: abcdefghijklmnop")
    rows = ai_memory.recall(10)
    assert len(rows) == 1
    assert rows[0]["chat_title"] == "Первая группа"
    assert "смешного кота" in rows[0]["body"]


def test_memory_is_bounded_by_requested_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_memory, "DB_PATH", str(tmp_path / "memory.sqlite3"))
    for i in range(5):
        ai_memory.remember(-1001, "Чат", i, f"u{i}", f"Обычное сообщение номер {i}", limit=3)
    rows = ai_memory.recall(10)
    assert len(rows) == 3
    assert rows[-1]["body"].endswith("4")
