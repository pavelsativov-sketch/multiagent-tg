"""Автономный движок задач: Света разбирает задачу → агенты выполняют через LLM.

Работает БЕЗ Telegram — используется напрямую из дашборда. Каждый агент
получает подзадачу и отвечает через свою LLM-модель. Результаты сохраняются
в TaskTracker и транслируются в дашборд через сообщения.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from multiagent_tg.bridge import StatusBoard
from multiagent_tg.config import AgentConfig, AppConfig
from multiagent_tg.llm import ChatMessage, LLMClient
from multiagent_tg.task_tracker import TaskTracker
from multiagent_tg.tools import FilesystemTool

log = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    """Одно сообщение в чате задачи."""
    agent_name: str
    display_name: str
    role: str
    text: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskConversation:
    """Разговор по задаче — все сообщения агентов."""
    task_id: str
    messages: list[AgentMessage] = field(default_factory=list)
    status: str = "processing"  # processing | done | failed

    def add(self, msg: AgentMessage) -> None:
        self.messages.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "messages": [m.to_dict() for m in self.messages],
        }


class TaskEngine:
    """Автономный движок: принимает задачу, Света разбивает, агенты выполняют."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.tracker = TaskTracker(config.data_dir)
        self.status = StatusBoard(config.data_dir)

        self._agents: dict[str, AgentConfig] = {}
        self._llm_clients: dict[str, LLMClient] = {}
        self._fs_tools: dict[str, FilesystemTool] = {}
        self._conversations: dict[str, TaskConversation] = {}
        self._queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_task: asyncio.Task | None = None

        for ac in config.all_agents:
            self._agents[ac.name] = ac
            model = ac.model or config.llm_model
            self._llm_clients[ac.name] = LLMClient(
                config.llm_base_url, config.llm_api_key, model,
            )
            if "filesystem" in ac.tools:
                self._fs_tools[ac.name] = FilesystemTool(config.workspace_dir)

        self._init_status()

    def _init_status(self) -> None:
        for ac in self.config.all_agents:
            self.status.set_agent(
                ac.name,
                display_name=ac.display_name,
                role=ac.role,
                model=ac.model or self.config.llm_model,
                state="idle",
            )

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._worker_task = loop.create_task(self._worker())

    async def _worker(self) -> None:
        log.info("TaskEngine worker started")
        while True:
            try:
                task_id, text, target = await self._queue.get()
                await self._process_task(task_id, text, target)
            except Exception as e:
                log.exception("TaskEngine error: %s", e)

    def submit(self, task_id: str, text: str, target_agent: str | None = None) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._queue.put((task_id, text, target_agent)),
                self._loop,
            )

    def get_conversation(self, task_id: str) -> dict[str, Any] | None:
        conv = self._conversations.get(task_id)
        return conv.to_dict() if conv else None

    def get_all_conversations(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._conversations.values()]

    async def _process_task(self, task_id: str, text: str, target: str | None) -> None:
        conv = TaskConversation(task_id=task_id)
        self._conversations[task_id] = conv

        self.tracker.reload()
        self.tracker.set_status(task_id, "in_progress")
        self.status.set_current_task(text, assigned_to=target)

        try:
            if target and target != "sveta":
                await self._direct_agent_task(conv, task_id, text, target)
            else:
                await self._sveta_delegates(conv, task_id, text)

            self.tracker.set_status(task_id, "done")
            conv.status = "done"
        except Exception as e:
            log.exception("Task %s failed: %s", task_id, e)
            err_str = str(e)
            if "free-models-per-day" in err_str:
                user_msg = (
                    "Дневной лимит бесплатных запросов исчерпан (50/день). "
                    "Добавьте $0.10 на https://openrouter.ai/settings/credits "
                    "для 1000 запросов/день, или подождите до завтра."
                )
            elif "429" in err_str or "rate" in err_str.lower():
                user_msg = (
                    "Модели временно перегружены (rate limit). "
                    "Подождите 1-2 минуты и попробуйте снова."
                )
            elif "404" in err_str or "NOT_FOUND" in err_str:
                user_msg = (
                    "Модель не найдена на сервере. Система автоматически "
                    "попробует другие модели при следующем запросе."
                )
            else:
                user_msg = f"Ошибка при выполнении: {e}"
            conv.add(AgentMessage(
                agent_name="system", display_name="Система", role="system",
                text=user_msg,
            ))
            self.tracker.set_status(task_id, "failed")
            conv.status = "failed"
        finally:
            self.status.clear_current_task()
            for ac in self.config.all_agents:
                self.status.set_agent(ac.name, state="idle")
            self.status.increment_processed()

    async def _sveta_delegates(self, conv: TaskConversation, task_id: str, text: str) -> None:
        """Света анализирует задачу и распределяет подзадачи агентам."""
        sveta_cfg = self._agents.get("sveta")
        if not sveta_cfg:
            raise RuntimeError("Agent 'sveta' not found in config")

        sveta_llm = self._llm_clients["sveta"]
        self.status.set_agent("sveta", state="thinking")

        delegation_prompt = self._build_delegation_prompt(text)

        try:
            response = await sveta_llm.chat_text(
                [
                    ChatMessage(role="system", content=sveta_cfg.system_prompt + self._sveta_engine_addendum()),
                    ChatMessage(role="user", content=delegation_prompt),
                ],
                temperature=sveta_cfg.temperature,
                max_tokens=sveta_cfg.max_tokens,
            )
        except Exception as e:
            log.warning("Света LLM failed: %s", e)
            self.status.set_agent("sveta", state="error")
            raise

        self.status.set_agent("sveta", state="idle")
        conv.add(AgentMessage(
            agent_name="sveta", display_name="Света", role="director",
            text=response,
        ))
        self.tracker.add_result(task_id, "sveta", response[:500])

        subtasks = self._parse_subtasks(response)
        if not subtasks:
            subtasks = self._fallback_subtasks(text)

        chat_history = [{"sender": "Заказчик", "text": text}, {"sender": "Света", "text": response}]

        for agent_name, subtask_text in subtasks:
            agent_cfg = self._agents.get(agent_name)
            if not agent_cfg:
                conv.add(AgentMessage(
                    agent_name="system", display_name="Система", role="system",
                    text=f"Агент '{agent_name}' не найден, пропускаю подзадачу.",
                ))
                continue

            self.status.set_agent(agent_name, state="thinking")
            self.tracker.assign(task_id, agent_name)

            agent_response = await self._run_agent(agent_name, agent_cfg, subtask_text, chat_history)

            self.status.set_agent(agent_name, state="idle")
            conv.add(AgentMessage(
                agent_name=agent_name,
                display_name=agent_cfg.display_name,
                role=agent_cfg.role,
                text=agent_response,
            ))
            self.tracker.add_result(task_id, agent_name, agent_response[:500])
            chat_history.append({"sender": agent_cfg.display_name, "text": agent_response})

        self.status.set_agent("sveta", state="thinking")
        summary = await self._sveta_summarize(sveta_cfg, sveta_llm, text, chat_history)
        self.status.set_agent("sveta", state="idle")
        conv.add(AgentMessage(
            agent_name="sveta", display_name="Света", role="director",
            text=summary,
        ))
        self.tracker.add_result(task_id, "sveta", f"[Итог] {summary[:500]}")

    async def _direct_agent_task(self, conv: TaskConversation, task_id: str, text: str, target: str) -> None:
        """Задача напрямую конкретному агенту."""
        agent_cfg = self._agents.get(target)
        if not agent_cfg:
            raise RuntimeError(f"Agent '{target}' not found")

        self.tracker.assign(task_id, target)
        self.status.set_agent(target, state="thinking")

        chat_history = [{"sender": "Заказчик", "text": text}]
        agent_response = await self._run_agent(target, agent_cfg, text, chat_history)

        self.status.set_agent(target, state="idle")
        conv.add(AgentMessage(
            agent_name=target,
            display_name=agent_cfg.display_name,
            role=agent_cfg.role,
            text=agent_response,
        ))
        self.tracker.add_result(task_id, target, agent_response[:500])

    async def _run_agent(
        self,
        agent_name: str,
        agent_cfg: AgentConfig,
        task_text: str,
        chat_history: list[dict[str, str]],
    ) -> str:
        """Запускает LLM агента с tool-calling для выполнения подзадачи."""
        llm = self._llm_clients[agent_name]
        fs = self._fs_tools.get(agent_name)

        history_text = "\n".join(f"{m['sender']}: {m['text']}" for m in chat_history)

        system_msg = agent_cfg.system_prompt + (
            f"\n\n[Мета] Ты {agent_cfg.display_name}, роль: {agent_cfg.role}. "
            f"Тебе дали подзадачу. Выполни её и коротко ответь что сделал (1-5 предложений). "
            f"Если нужно создать файл — используй write_file. "
            f"Не задавай вопросов — сразу делай."
        )

        messages = [
            ChatMessage(role="system", content=system_msg),
            ChatMessage(role="user", content=(
                f"Контекст разговора:\n{history_text}\n\n"
                f"Твоя подзадача: {task_text}"
            )),
        ]

        tools = self._build_tools(agent_name, agent_cfg) if fs else None
        allowed_tools = {t["function"]["name"] for t in (tools or []) if "function" in t}

        for _round in range(agent_cfg.max_rounds):
            try:
                resp = await llm.chat(
                    messages,
                    temperature=agent_cfg.temperature,
                    max_tokens=agent_cfg.max_tokens,
                    tools=tools,
                )
            except Exception as e:
                log.warning("[%s] LLM failed: %s", agent_name, e)
                return "(Ошибка LLM: модель временно недоступна)"

            if not resp.choices:
                log.warning("[%s] Empty response from LLM", agent_name)
                return "(Пустой ответ от модели)"

            choice = resp.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []

            if not tool_calls:
                return (choice.content or "").strip() or "(пустой ответ)"

            assistant_text = choice.content or ""
            tool_results: list[str] = []

            for call in tool_calls:
                fn_name = call.function.name
                try:
                    fn_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                result = self._exec_tool(agent_name, fn_name, fn_args, allowed_tools)
                tool_results.append(f"[{fn_name}] → {result}")

            messages.append(ChatMessage(role="assistant", content=assistant_text))
            messages.append(ChatMessage(
                role="user",
                content="Результаты инструментов:\n" + "\n".join(tool_results)
                + "\n\nПродолжай. Ответь коротко что сделал.",
            ))

        return "Задача выполнена (достигнут лимит шагов)."

    def _exec_tool(self, agent_name: str, fn_name: str, args: dict, allowed: set[str]) -> str:
        if fn_name not in allowed:
            return f"Tool '{fn_name}' не доступен"
        fs = self._fs_tools.get(agent_name)
        if not fs:
            return "Filesystem не настроен"
        try:
            if fn_name == "write_file":
                return fs.write_file(args["path"], args["content"])
            if fn_name == "read_file":
                return fs.read_file(args["path"])
            if fn_name == "list_dir":
                return json.dumps(fs.list_dir(args.get("path", ".")), ensure_ascii=False)
            return f"Unknown tool: {fn_name}"
        except Exception as e:
            return f"Ошибка: {e}"

    def _build_tools(self, agent_name: str, cfg: AgentConfig) -> list[dict] | None:
        fs = self._fs_tools.get(agent_name)
        if not fs or "filesystem" not in cfg.tools:
            return None
        return fs.openai_schema()

    def _build_delegation_prompt(self, task_text: str) -> str:
        return (
            f"[Задача от заказчика]\n{task_text}\n\n"
            "Разбей задачу на подзадачи и распредели между командой. "
            "Для каждой подзадачи напиши ОТДЕЛЬНУЮ строку в формате:\n"
            "@Имя: описание подзадачи\n\n"
            "Доступные агенты:\n"
            "- @Игорь — разработчик (код, файлы, проекты)\n"
            "- @Аня — дизайнер (UX/UI, палитры, структура страниц)\n"
            "- @Костя — QA (тесты, ревью, edge cases)\n\n"
            "После списка подзадач — коротко напиши общий план."
        )

    def _sveta_engine_addendum(self) -> str:
        return (
            "\n\n[Системная инструкция: ты работаешь через движок задач дашборда, "
            "НЕ через Telegram. Распределяй задачи в формате '@Имя: задача' — "
            "каждая строка начинающаяся с @ будет автоматически отправлена нужному агенту. "
            "Пиши по-русски.]"
        )

    def _parse_subtasks(self, sveta_response: str) -> list[tuple[str, str]]:
        """Парсит ответ Светы и извлекает подзадачи: (@Имя: задача)."""
        name_map = {
            "игорь": "igor", "igor": "igor",
            "аня": "anya", "anya": "anya",
            "костя": "kostya", "kostya": "kostya",
        }
        subtasks: list[tuple[str, str]] = []
        lines = sveta_response.split("\n")

        for line in lines:
            line = line.strip()
            match = re.match(r"@?(\w+)[,:]\s*(.+)", line, re.IGNORECASE)
            if match:
                name_raw = match.group(1).lower()
                task_text = match.group(2).strip()
                agent_name = name_map.get(name_raw)
                if agent_name and task_text:
                    subtasks.append((agent_name, task_text))

        return subtasks

    def _fallback_subtasks(self, text: str) -> list[tuple[str, str]]:
        """Если Света не дала чёткого распределения — fallback."""
        lower = text.lower()
        subtasks: list[tuple[str, str]] = []

        has_design = any(w in lower for w in ("дизайн", "ui", "ux", "страниц", "визуал", "палитр"))
        has_code = any(w in lower for w in (
            "код", "проект", "сайт", "приложение", "бот", "api",
            "создай", "напиши", "разработай", "сделай", "html", "react",
        ))

        if has_design:
            subtasks.append(("anya", f"Разработай дизайн-концепцию: {text[:300]}"))
        if has_code or not subtasks:
            subtasks.append(("igor", f"Реализуй техническую часть: {text[:300]}"))
        subtasks.append(("kostya", f"Проверь план и укажи edge cases: {text[:300]}"))

        return subtasks

    async def _sveta_summarize(
        self,
        cfg: AgentConfig,
        llm: LLMClient,
        original_task: str,
        chat_history: list[dict[str, str]],
    ) -> str:
        history_text = "\n".join(f"{m['sender']}: {m['text']}" for m in chat_history)
        try:
            return await llm.chat_text(
                [
                    ChatMessage(role="system", content=(
                        "Ты Света, директор. Подведи КРАТКИЙ итог выполнения задачи. "
                        "Перечисли что сделал каждый агент и каков общий результат. "
                        "1-3 предложения. Если созданы файлы — укажи пути."
                    )),
                    ChatMessage(role="user", content=(
                        f"Задача: {original_task}\n\nРазговор команды:\n{history_text}\n\n"
                        "Подведи итог для заказчика."
                    )),
                ],
                temperature=0.3,
                max_tokens=400,
            )
        except Exception as e:
            log.warning("Света summarize failed: %s", e)
            return "Задача обработана командой. Подробности — в сообщениях выше."
