"""Telegram actions exposed as LLM tools."""

from __future__ import annotations

import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

log = logging.getLogger(__name__)


DEFAULT_REACTION = "👍"
SUPPORTED_REACTIONS = {"👍", "❤️", "🔥", "👏", "😁", "😢", "🤔", "👀", "🎉"}


def normalize_reaction(emoji: str | None) -> str:
    if not emoji:
        return DEFAULT_REACTION
    emoji = emoji.strip()
    if emoji in SUPPORTED_REACTIONS:
        return emoji
    return DEFAULT_REACTION


class TelegramTool:
    def __init__(self, client: TelegramClient, chat_id: int, workspace_dir: Path | str):
        self.client = client
        self.chat_id = chat_id
        self.workspace_dir = Path(workspace_dir).resolve()
        self.sent_files: list[str] = []

    def _resolve_workspace_file(self, relative_path: str) -> Path:
        target = (self.workspace_dir / relative_path).resolve()
        try:
            target.relative_to(self.workspace_dir)
        except ValueError as e:
            raise ValueError(f"Файл вне workspace/: {relative_path}") from e
        if not target.is_file():
            raise FileNotFoundError(f"Файл не найден в workspace/: {relative_path}")
        return target

    async def send_reaction(self, message_id: int, emoji: str = "👍") -> str:
        emoji = normalize_reaction(emoji)
        await self.client(
            SendReactionRequest(
                peer=self.chat_id,
                msg_id=message_id,
                reaction=[ReactionEmoji(emoticon=emoji)],
                big=False,
                add_to_recent=True,
            )
        )
        log.info("telegram.send_reaction chat=%s msg=%s emoji=%s", self.chat_id, message_id, emoji)
        return f"Поставлена реакция {emoji} на сообщение {message_id}"

    async def send_file(self, path: str, caption: str | None = None) -> str:
        target = self._resolve_workspace_file(path)
        await self.client.send_file(self.chat_id, file=str(target), caption=caption or "")
        self.sent_files.append(str(target.relative_to(self.workspace_dir)))
        log.info("telegram.send_file chat=%s path=%s", self.chat_id, target)
        return f"Файл отправлен в чат: {target.relative_to(self.workspace_dir)}"

    @staticmethod
    def openai_schema(allow_reaction: bool = False, allow_file: bool = True) -> list[dict]:
        schemas: list[dict] = []
        if allow_reaction:
            schemas.append(
                {
                "type": "function",
                "function": {
                    "name": "send_reaction",
                    "description": (
                        "Поставить emoji-реакцию на сообщение в текущем Telegram-чате. "
                        "Используй id сообщения из истории чата, например [id=6501]."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "integer",
                                "description": "Telegram id сообщения из истории чата.",
                            },
                            "emoji": {
                                "type": "string",
                                "description": "Emoji реакции, например 👍, ❤️, 🔥, 👀.",
                                "default": "👍",
                            },
                        },
                        "required": ["message_id"],
                    },
                },
            }
            )
        if allow_file:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": "send_file",
                        "description": (
                            "Отправить файл из workspace/ в текущий Telegram-чат. "
                            "Используй после создания HTML/CSS/JS/ZIP/текстового файла или когда пользователь просит скинуть файл."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Относительный путь внутри workspace/, например snake-game.html.",
                                },
                                "caption": {
                                    "type": "string",
                                    "description": "Короткая подпись к файлу.",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                }
            )
        return schemas
