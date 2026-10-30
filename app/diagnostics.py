"""Local, read-only deployment diagnostics for PULSAR.

This module never prints tokens or payment identifiers and does not call external APIs.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path

from app.config import settings
from app import community_store, ai_memory
from app.payments import deposit_store


def run_diagnostics(project_root: str | Path | None = None) -> list[dict[str, str]]:
    root = Path(project_root or Path(__file__).resolve().parents[1])
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "OK" if ok else "WARN", "detail": detail})

    add("BOT_TOKEN", bool(settings.bot_token and settings.bot_token != "replace_me"),
        "Токен задан" if settings.bot_token and settings.bot_token != "replace_me" else "Не задан; бот не сможет подключиться к Telegram")
    payment_ready = bool(settings.payments_enabled and settings.crypto_pay_api_token)
    add("Платежи", payment_ready,
        "Платежи включены; сетевой тест не выполнялся" if payment_ready else
        "Платежи выключены или не настроены; реальные платежи недоступны")
    add("DeepSeek / AI", bool(os.getenv("PULSAR_AI_API_KEY", "").strip()),
        "API-ключ задан; доступность API не проверялась" if os.getenv("PULSAR_AI_API_KEY", "").strip() else
        "Не задан; команды с ИИ будут использовать резервные ответы")

    required = ["app/bot.py", "app/community_store.py", "app/ai_memory.py",
                "app/payments/deposit_store.py", "assets/emoji_catalog.json"]
    missing = [name for name in required if not (root / name).is_file()]
    add("Основные файлы", not missing, "Все основные файлы на месте" if not missing else "Нет: " + ", ".join(missing))

    cards_dir = root / "assets" / "social_cards"
    expected_cards = {"adopt", "adult_18", "children", "confession", "date", "divorce", "family",
                      "flirt", "flowers", "gift_surprise", "hug", "kiss", "love", "marry", "massage",
                      "movie_night", "parents", "proposal", "support", "surprise"}
    missing_cards = sorted(name for name in expected_cards if not (cards_dir / f"{name}.jpg").is_file())
    add("Социальные карточки", not missing_cards,
        f"Найдено {len(expected_cards) - len(missing_cards)}/{len(expected_cards)} карточек" +
        ("; отсутствуют: " + ", ".join(missing_cards) if missing_cards else ""))

    db_paths = {
        "Casino / payments DB": Path(deposit_store.DB_PATH),
        "Community DB": Path(community_store.DB_PATH),
        "AI memory DB": Path(ai_memory.DB_PATH),
    }
    for label, path in db_paths.items():
        if not path.exists():
            add(label, False, f"Файл пока не создан: {path.name}; он создаётся при инициализации подсистемы")
            continue
        try:
            with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=2) as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
            good = bool(result and result[0] == "ok")
            add(label, good, "SQLite quick_check: ok" if good else f"SQLite quick_check: {result[0] if result else 'нет результата'}")
        except (sqlite3.Error, OSError) as exc:
            add(label, False, f"Не удалось прочитать базу: {type(exc).__name__}")

    for module in ("aiogram", "httpx", "pydantic_settings", "fastapi"):
        available = importlib.util.find_spec(module) is not None
        add(f"Зависимость {module}", available, "Установлена" if available else "Не установлена")

    add("TON", False, "Проверка TON-платежей намеренно отключена: не настроен доверенный индексатор и проверка финальности транзакций")
    return checks
