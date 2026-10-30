"""Persistent, shared cross-group memory for PULSAR's public group banter.

Only short public group messages are stored. Private chats are never stored.
Memory is bounded and deliberately avoids saving obvious secrets/credentials.
"""
from __future__ import annotations
import os, re, sqlite3, time
from pathlib import Path

DB_PATH = os.getenv("PULSAR_AI_MEMORY_DB", str(Path(__file__).resolve().parents[1] / "pulsar_ai_memory.sqlite3"))
_SECRET = re.compile(r"(?i)(api[_ -]?key|token|password|пароль|токен|seed phrase|private key|приватн\w* ключ|\b\d{12,}\b)")

def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_memory():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS ai_public_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, chat_title TEXT NOT NULL DEFAULT '',
            user_id INTEGER NOT NULL, username TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
            created_at INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_recent ON ai_public_memory(created_at DESC)")

def remember(chat_id: int, chat_title: str, user_id: int, username: str, body: str, limit: int = 15000) -> None:
    body = " ".join((body or "").split())[:500]
    if len(body) < 3 or _SECRET.search(body):
        return
    init_memory()
    now = int(time.time())
    with _connect() as c:
        c.execute("INSERT INTO ai_public_memory(chat_id,chat_title,user_id,username,body,created_at) VALUES(?,?,?,?,?,?)",
                  (chat_id, (chat_title or str(chat_id))[:150], user_id, (username or "")[:100], body, now))
        c.execute("DELETE FROM ai_public_memory WHERE id NOT IN (SELECT id FROM ai_public_memory ORDER BY id DESC LIMIT ?)", (limit,))

def recall(limit: int = 18) -> list[dict]:
    init_memory()
    with _connect() as c:
        rows = c.execute("SELECT chat_title,username,body,created_at FROM ai_public_memory ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]
