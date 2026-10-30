"""Group-wide callout support.

Telegram Bot API cannot enumerate every member of a group. This module maintains
an opt-in-by-presence roster of people whose messages the bot has observed, and
can mention that known roster when an authorized moderator calls everyone.
"""
from __future__ import annotations

import html
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = os.getenv("PULSAR_DB_PATH", str(Path(__file__).resolve().parents[1] / "pulsar_fan.sqlite3"))


def _connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    return db


def init_store() -> None:
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS group_call_members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            full_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            last_seen INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_group_call_members_seen ON group_call_members(chat_id,last_seen DESC)")


def record_member(chat_id: int, user_id: int, full_name: str, username: str = "") -> None:
    """Remember a participant when they send a message in a group the bot can read."""
    if not chat_id or not user_id:
        return
    init_store()
    with _connect() as db:
        db.execute("""INSERT INTO group_call_members(chat_id,user_id,full_name,username,last_seen)
            VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
            full_name=excluded.full_name, username=excluded.username, last_seen=excluded.last_seen""",
            (chat_id, user_id, full_name[:256], username[:64], int(time.time())))


def list_members(chat_id: int, exclude_user_id: int | None = None) -> list[dict]:
    init_store()
    with _connect() as db:
        if exclude_user_id is None:
            rows = db.execute("SELECT user_id,full_name,username FROM group_call_members WHERE chat_id=? ORDER BY last_seen DESC", (chat_id,)).fetchall()
        else:
            rows = db.execute("SELECT user_id,full_name,username FROM group_call_members WHERE chat_id=? AND user_id<>? ORDER BY last_seen DESC", (chat_id, exclude_user_id)).fetchall()
    return [dict(row) for row in rows]


def render_mentions(members: list[dict]) -> list[str]:
    """Build HTML mention chunks conservatively under Telegram's message limit."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for member in members:
        user_id = int(member["user_id"])
        name = html.escape((member.get("full_name") or member.get("username") or f"Участник {user_id}")[:100])
        mention = f'<a href="tg://user?id={user_id}">{name}</a>'
        if current and (len(current) >= 25 or length + len(mention) + 1 > 3000):
            chunks.append(" ".join(current))
            current, length = [], 0
        current.append(mention)
        length += len(mention) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks
