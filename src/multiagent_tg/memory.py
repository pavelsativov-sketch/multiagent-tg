"""SQLite-хранилище истории сообщений в группе."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)


@dataclass
class StoredMessage:
    id: int
    tg_message_id: int
    chat_id: int
    sender_name: str  # display_name агента или "human" / "<external>"
    text: str
    timestamp: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    text TEXT NOT NULL,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, timestamp DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_tgid ON messages(chat_id, tg_message_id);
"""


class Memory:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def save(
        self,
        tg_message_id: int,
        chat_id: int,
        sender_name: str,
        text: str,
        timestamp: float,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO messages(tg_message_id, chat_id, sender_name, text, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (tg_message_id, chat_id, sender_name, text, timestamp),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                # Уже сохранено - другим агентом, который тоже видит это сообщение.
                pass

    async def recent(self, chat_id: int, limit: int = 40) -> list[StoredMessage]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, tg_message_id, chat_id, sender_name, text, timestamp "
                "FROM messages WHERE chat_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (chat_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        rows = list(reversed(rows))
        return [
            StoredMessage(
                id=r["id"],
                tg_message_id=r["tg_message_id"],
                chat_id=r["chat_id"],
                sender_name=r["sender_name"],
                text=r["text"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    async def last_senders(self, chat_id: int, n: int = 5) -> list[str]:
        """Кто отправлял последние N сообщений (новые - в конце)."""
        msgs = await self.recent(chat_id, limit=n)
        return [m.sender_name for m in msgs]
