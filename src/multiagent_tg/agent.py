"""Один агент = один Telegram-аккаунт + LLM-обёртка."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.tl.custom import Message

from multiagent_tg.config import AgentConfig
from multiagent_tg.llm import ChatMessage, LLMClient
from multiagent_tg.memory import StoredMessage
from multiagent_tg.tools import FilesystemTool, ShellTool, TelegramTool

log = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    """Активный агент со всеми зависимостями для работы."""

    config: AgentConfig
    client: TelegramClient
    llm: LLMClient
    user_id: int | None = None
    fs: FilesystemTool | None = None
    sh: ShellTool | None = None
    tg: TelegramTool | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def display_name(self) -> str:
        return self.config.display_name

    @property
    def model(self) -> str:
        return self.llm.model

    @staticmethod
    def _wants_reaction(text: str) -> bool:
        lower = text.lower()
        return "реакц" in lower or "лайк" in lower or re.search(r"\bпоставь\b.*[👍❤️🔥👏😁😢🤔👀🎉]", text)

    @staticmethod
    def _wants_file_result(text: str) -> bool:
        lower = text.lower()
        direct_words = re.search(
            r"\b(напиши|создай|сделай|разработай|исправь|добавь|измени|почини|доработай|код|игра|игру|сайт|приложение|бот|бота)\b",
            lower,
        )
        tech_words = any(word in lower for word in ("html", "css", "javascript", ".js", ".py"))
        send_phrases = any(word in lower for word in ("скинь", "пришли", "отправь", "файл"))
        location_request = "где" in lower and "наход" in lower
        return bool(direct_words or tech_words or send_phrases or location_request)

    @classmethod
    def _wants_dev_tools(cls, text: str) -> bool:
        lower = text.lower()
        operational_words = (
            "прочитай",
            "покажи",
            "посмотри",
            "запусти",
            "проверь",
            "протестируй",
            "тест",
            "команд",
            "ошиб",
            "лог",
        )
        return cls._wants_file_result(text) or any(word in lower for word in operational_words)

    def _tools_schema(self, last_text: str) -> list[dict] | None:
        schemas: list[dict] = []
        wants_dev_tools = self._wants_dev_tools(last_text)
        if wants_dev_tools and self.fs and "filesystem" in self.config.tools:
            schemas.extend(self.fs.openai_schema())
        if wants_dev_tools and self.sh and "shell" in self.config.tools:
            schemas.extend(self.sh.openai_schema())
        if self.tg and "telegram" in self.config.tools:
            schemas.extend(
                self.tg.openai_schema(
                    allow_reaction=self._wants_reaction(last_text),
                    allow_file=self._wants_file_result(last_text),
                )
            )
        return schemas or None

    async def _exec_tool(
        self,
        name: str,
        args: dict[str, Any],
        allowed_tool_names: set[str],
    ) -> str:
        if name not in allowed_tool_names:
            return f"Инструмент '{name}' не доступен для этого запроса."
        try:
            if name == "write_file" and self.fs:
                return self.fs.write_file(args["path"], args["content"])
            if name == "read_file" and self.fs:
                return self.fs.read_file(args["path"])
            if name == "list_dir" and self.fs:
                return json.dumps(self.fs.list_dir(args.get("path", ".")), ensure_ascii=False)
            if name == "run_command" and self.sh:
                result = await self.sh.run(args["command"])
                return json.dumps(result, ensure_ascii=False)
            if name == "send_reaction" and self.tg:
                return await self.tg.send_reaction(
                    message_id=int(args["message_id"]),
                    emoji=args.get("emoji", "👍"),
                )
            if name == "send_file" and self.tg:
                return await self.tg.send_file(
                    path=args["path"],
                    caption=args.get("caption"),
                )
            return f"Инструмент '{name}' недоступен для этого агента"
        except Exception as e:
            log.warning("Tool %s failed: %s", name, e)
            return f"Ошибка инструмента {name}: {e}"

    async def generate_reply(self, history: list[StoredMessage]) -> str:
        """Сгенерировать реплику с учётом истории чата.

        Если у агента есть инструменты — поддерживается tool-calling
        (несколько раундов, пока модель не выдаст финальный текст).
        """
        formatted_history = self._format_history(history)
        last_text = history[-1].text if history else ""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.config.system_prompt + self._meta_addendum()},
            {"role": "user", "content": formatted_history},
        ]

        tools = self._tools_schema(last_text)
        allowed_tool_names = {
            tool["function"]["name"]
            for tool in tools or []
            if tool.get("type") == "function" and "function" in tool
        }
        max_rounds = self.config.max_rounds
        for _ in range(max_rounds):
            chat_messages = [ChatMessage(role=m["role"], content=m.get("content") or "") for m in messages if m["role"] in ("system", "user", "assistant")]
            if tools:
                resp = await self.llm.chat(
                    chat_messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    tools=tools,
                )
            else:
                resp = await self.llm.chat(
                    chat_messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
            choice = resp.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []

            if not tool_calls:
                return (choice.content or "").strip()

            # Выполняем все tool-calls и кладём результаты в контекст
            assistant_text = choice.content or ""
            tool_text_blocks: list[str] = []
            for call in tool_calls:
                fn_name = call.function.name
                try:
                    fn_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                result = await self._exec_tool(fn_name, fn_args, allowed_tool_names)
                tool_text_blocks.append(f"[tool {fn_name}({fn_args})] -> {result}")

            messages.append({"role": "assistant", "content": assistant_text})
            messages.append(
                {
                    "role": "user",
                    "content": "Результаты инструментов:\n" + "\n".join(tool_text_blocks)
                    + "\n\nПродолжай. Если ты создал файл и пользователь просил код/HTML/игру/файл — "
                    + "отправь этот файл через send_file. Если файл уже отправлен или отправлять нечего, "
                    + "напиши короткое сообщение в чат с результатом.",
                }
            )

        # Превышен лимит раундов — возвращаем последнее, что есть
        return "Слишком много шагов, остановился. Попробую позже."

    def _meta_addendum(self) -> str:
        return (
            f"\n\n[Мета-инфа для тебя — не озвучивай её в чате]\n"
            f"Тебя зовут {self.display_name}, твоя роль в чате: {self.config.role}. "
            f"Ты участвуешь в групповом чате с коллегами-агентами и людьми. "
            f"Пиши ОДНУ короткую реплику от своего имени (1-4 предложения), "
            f"как сообщение в Telegram. Не указывай своё имя в начале сообщения — "
            f"Telegram сам покажет кто пишет. Не используй ник других людей в чате "
            f"если в этом нет смысла. Не дублируй то, что уже сказал кто-то. "
            f"Если пользователь просит выполнить действие в Telegram или в проекте, сначала используй доступный инструмент. "
            f"Если подходящего инструмента нет, коротко скажи, какой инструмент должен добавить dev-агент."
        )

    def _format_history(self, history: list[StoredMessage]) -> str:
        lines = []
        for m in history:
            lines.append(f"[id={m.tg_message_id}] {m.sender_name}: {m.text}")
        if not lines:
            return "[Чат пока пустой. Скажи что-нибудь чтобы завязать разговор.]"
        return "\n".join(lines)

    async def send(self, chat_id: int, text: str) -> Message:
        log.info("[%s] -> %s: %s", self.display_name, chat_id, text[:80])
        msg = await self.client.send_message(chat_id, text)
        return msg


async def make_telegram_client(
    session_path: Path,
    api_id: int,
    api_hash: str,
) -> TelegramClient:
    client = TelegramClient(str(session_path), api_id, api_hash)
    return client


async def humanlike_delay(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))
