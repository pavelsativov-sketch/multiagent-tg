"""Оркестратор: слушает группу, решает кто отвечает, координирует ответы."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from telethon import events

from multiagent_tg.agent import AgentRuntime, humanlike_delay
from multiagent_tg.bridge import StatusBoard, TaskQueue, TaskRequest
from multiagent_tg.config import AppConfig
from multiagent_tg.llm import ChatMessage, LLMClient
from multiagent_tg.memory import Memory
from multiagent_tg.task_tracker import TaskTracker
from multiagent_tg.tools.telegram import DEFAULT_REACTION, SUPPORTED_REACTIONS

log = logging.getLogger(__name__)


HUMAN_SENDER = "<human>"


@dataclass
class Decision:
    agent_name: str | None  # None = молчат все
    reason: str = ""


class Orchestrator:
    def __init__(self, config: AppConfig, agents: list[AgentRuntime], memory: Memory):
        self.config = config
        self.agents = agents
        self.agents_by_name = {a.name: a for a in agents}
        self.agents_by_user_id: dict[int, AgentRuntime] = {}
        self.memory = memory
        self.router_llm = LLMClient(config.llm_base_url, config.llm_api_key, config.llm_model)
        self._lock = asyncio.Lock()
        self._busy_until: dict[str, float] = {}
        self.task_queue = TaskQueue(config.data_dir)
        self.status = StatusBoard(config.data_dir)
        self.tracker = TaskTracker(config.data_dir)
        self._tasks_loop: asyncio.Task | None = None
        self._task_counter = 0

    def _latest_workspace_file(self) -> str | None:
        files = [
            path
            for path in self.config.workspace_dir.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.relative_to(self.config.workspace_dir).parts)
        ]
        if not files:
            return None
        latest = max(files, key=lambda path: path.stat().st_mtime)
        return str(latest.relative_to(self.config.workspace_dir))

    async def start(self) -> None:
        # Подключение всех клиентов и валидация что они залогинены.
        for agent in self.agents:
            await agent.client.connect()
            if not await agent.client.is_user_authorized():
                raise RuntimeError(
                    f"Агент '{agent.name}' не залогинен. Запустите "
                    f"`multiagent-tg login` сначала."
                )
            me = await agent.client.get_me()
            agent.user_id = me.id
            self.agents_by_user_id[me.id] = agent
            log.info("Агент %s залогинен как @%s (id=%s)", agent.display_name, me.username, me.id)
            self.status.set_agent(
                agent.name,
                display_name=agent.display_name,
                role=agent.config.role,
                model=agent.model,
                username=me.username,
                user_id=me.id,
                state="idle",
            )

        # Регистрируем единого "слушателя" — клиент первого агента — на сообщения в группе.
        listener = self.agents[0]
        chat_id = self.config.tg_group_id

        @listener.client.on(events.NewMessage(chats=chat_id))
        async def handler(event):
            try:
                await self._on_new_message(event)
            except Exception as e:
                log.exception("Ошибка обработки сообщения: %s", e)

        # Фон: поллим очередь задач из дашборда.
        self._tasks_loop = asyncio.create_task(self._poll_task_queue())

        log.info("Оркестратор готов. Слушаем чат %s.", chat_id)

    async def _on_new_message(self, event) -> None:
        msg = event.message
        text = msg.message or ""
        if not text.strip():
            return

        sender = await msg.get_sender()
        sender_id = sender.id if sender else None
        sender_agent = self.agents_by_user_id.get(sender_id) if sender_id else None
        sender_name = sender_agent.display_name if sender_agent else HUMAN_SENDER

        await self.memory.save(
            tg_message_id=msg.id,
            chat_id=self.config.tg_group_id,
            sender_name=sender_name,
            text=text,
            timestamp=msg.date.timestamp(),
        )

        if await self._try_direct_reaction(msg, text):
            return

        # Track human tasks
        if sender_name == HUMAN_SENDER and len(text.strip()) > 10:
            self._task_counter += 1
            task_id = f"tg-{int(time.time())}-{self._task_counter}"
            self.tracker.create(task_id, text, source="telegram")
            self.status.set_current_task(text)

        async with self._lock:
            await self._maybe_respond(text, sender_name)

    async def _try_direct_reaction(self, msg: Any, text: str) -> bool:
        lower = text.lower()
        wants_reaction = (
            "реакц" in lower
            or "лайк" in lower
            or ("постав" in lower and any(emoji in text for emoji in SUPPORTED_REACTIONS))
        )
        if not wants_reaction:
            return False

        agent = next((a for a in self.agents if a.tg), None)
        if not agent or not agent.tg:
            log.info("Не могу поставить реакцию: нет агента с telegram tool.")
            return False

        target_message_id = getattr(msg, "reply_to_msg_id", None) or msg.id
        emoji = next((emoji for emoji in SUPPORTED_REACTIONS if emoji in text), DEFAULT_REACTION)
        await agent.tg.send_reaction(target_message_id, emoji)
        log.info(
            "Решение: %s поставил реакцию %s на сообщение %s.",
            agent.display_name,
            emoji,
            target_message_id,
        )
        return True

    async def _maybe_respond(self, last_text: str, last_sender: str) -> None:
        # Никто не отвечает на собственные сообщения.
        candidates = [a for a in self.agents if a.display_name != last_sender]
        if not candidates:
            return
        if len(candidates) == 1 and last_sender == HUMAN_SENDER:
            agent = candidates[0]
            log.info("Решение: отвечает %s. Причина: единственный доступный агент.", agent.display_name)
            await self._respond_with(agent)
            return

        # Анти-спам: если один и тот же агент уже отвечал N раз подряд - исключаем.
        last_senders_window = await self.memory.last_senders(
            self.config.tg_group_id, n=self.config.max_consecutive_replies + 1
        )
        if last_senders_window:
            tail = list(reversed(last_senders_window))  # новейшие первые
            if tail[0] != HUMAN_SENDER:
                streak_name = tail[0]
                streak = 0
                for name in tail:
                    if name != streak_name:
                        break
                    streak += 1
                if streak >= self.config.max_consecutive_replies:
                    candidates = [c for c in candidates if c.display_name != streak_name]

        if not candidates:
            log.debug("Все потенциальные ответчики на анти-спам кулдауне")
            return

        decision = await self._decide(last_text, last_sender, candidates)
        if decision.agent_name is None:
            log.info("Решение: никто не отвечает. Причина: %s", decision.reason)
            return

        agent = self.agents_by_name.get(decision.agent_name)
        if not agent:
            log.warning("Роутер вернул неизвестного агента: %s", decision.agent_name)
            return

        log.info("Решение: отвечает %s. Причина: %s.", agent.display_name, decision.reason)
        await self._respond_with(agent)

    async def _respond_with(self, agent: AgentRuntime) -> None:
        # Имитация набора + случайная задержка.
        self.status.set_agent(agent.name, state="thinking")
        await humanlike_delay(self.config.reply_delay_min, self.config.reply_delay_max)

        try:
            self.status.set_agent(agent.name, state="typing")
            async with agent.client.action(self.config.tg_group_id, "typing"):
                history = await self.memory.recent(
                    self.config.tg_group_id, limit=self.config.context_window
                )
                last_text = history[-1].text if history else ""
                written_before = len(agent.fs.written_files) if agent.fs else 0
                sent_before = len(agent.tg.sent_files) if agent.tg else 0
                reply = await agent.generate_reply(history)
                if (
                    agent.fs
                    and agent.tg
                    and AgentRuntime._wants_file_result(last_text)
                    and len(agent.fs.written_files) > written_before
                    and len(agent.tg.sent_files) == sent_before
                ):
                    latest_file = agent.fs.written_files[-1]
                    await agent.tg.send_file(latest_file, caption=f"Готово: {latest_file}")
                elif (
                    agent.tg
                    and AgentRuntime._wants_file_result(last_text)
                    and len(agent.tg.sent_files) == sent_before
                    and any(word in last_text.lower() for word in ("скинь", "пришли", "отправь"))
                ):
                    latest_file = self._latest_workspace_file()
                    if latest_file:
                        await agent.tg.send_file(latest_file, caption=f"Вот файл: {latest_file}")
            if reply:
                await agent.send(self.config.tg_group_id, reply)
            self.status.set_agent(
                agent.name,
                state="idle",
                last_message_at=time.time(),
                files_written=len(agent.fs.written_files) if agent.fs else 0,
                files_sent=len(agent.tg.sent_files) if agent.tg else 0,
            )
        except Exception as e:
            log.exception("[%s] не смог ответить: %s", agent.display_name, e)
            self.status.set_agent(agent.name, state="error", last_error=str(e)[:200])

    async def _decide(
        self,
        last_text: str,
        last_sender: str,
        candidates: list[AgentRuntime],
    ) -> Decision:
        """Решает, кто из candidates отвечает на последнее сообщение (или никто)."""
        # 1. Явные упоминания по имени - максимально приоритетно.
        lower = last_text.lower()
        for agent in candidates:
            for tag in {agent.display_name.lower(), agent.name.lower()}:
                if re.search(rf"\b{re.escape(tag)}\b", lower):
                    return Decision(agent_name=agent.name, reason=f"упомянут '{tag}'")

        # 2. Если предыдущее сообщение от агента (а не человека) - с шансом 50% молчим,
        #    чтобы агенты не превращали группу в бесконечный диалог.
        if last_sender != HUMAN_SENDER and random.random() < 0.4:
            return Decision(agent_name=None, reason="агент-агент кулдаун")

        # 3. LLM-роутер: даём список ролей и просим выбрать одну или 'none'.
        roles_block = "\n".join(
            f"- {a.name} ({a.display_name}, {a.config.role}): "
            f"{a.config.system_prompt.splitlines()[0]}"
            for a in candidates
        )
        prompt = (
            "Ты модератор группового чата. Последнее сообщение:\n"
            f"<{last_sender}>: {last_text}\n\n"
            "Возможные ответчики:\n"
            f"{roles_block}\n\n"
            "Выбери имя ОДНОГО агента, которому уместнее всего ответить, "
            "или ответь словом 'none' если никому отвечать не надо "
            "(например, реплика - короткое подтверждение, шутка, или офтоп). "
            "Отвечай ОДНИМ словом - именем агента или 'none'."
        )
        try:
            text = await self.router_llm.chat_text(
                [ChatMessage(role="user", content=prompt)],
                temperature=0.2,
                max_tokens=100,
            )
        except Exception as e:
            log.warning("Роутер LLM упал: %s. Никто не отвечает.", e)
            return Decision(agent_name=None, reason="router error")

        text = text.strip().lower().strip(".,'\"")
        if text in {"none", "никто", "нет"}:
            return Decision(agent_name=None, reason="router said none")
        for agent in candidates:
            if agent.name.lower() == text or agent.display_name.lower() == text:
                return Decision(agent_name=agent.name, reason="router pick")

        # Не смогли распарсить - молчим.
        return Decision(agent_name=None, reason=f"unparsable router response: {text!r}")

    # ------------------------------------------------------------------
    # Очередь задач из дашборда
    # ------------------------------------------------------------------
    async def _poll_task_queue(self) -> None:
        """Фон-таск: каждые 1.5с читает новые задачи из дашборда и постит
        их в группу. Падать молча не должно — ошибки логируем и продолжаем."""
        log.info("Слушаем очередь задач: %s", self.task_queue.queue_path)
        while True:
            try:
                tasks = self.task_queue.read_new()
                for task in tasks:
                    await self._execute_dashboard_task(task)
                    self.status.increment_processed()
            except Exception as e:
                log.exception("Ошибка в poll_task_queue: %s", e)
            await asyncio.sleep(1.5)

    async def _execute_dashboard_task(self, task: TaskRequest) -> None:
        """Постит задачу из дашборда в TG-группу.

        Если адресат указан и это не Света — формулировка от имени Светы
        с прямым обращением (роутер по имени отдаст реплику нужному агенту).
        Иначе — постится как обычное сообщение от Светы (она дальше сама
        распределит)."""
        director = self._director() or self.agents[0]
        chat_id = self.config.tg_group_id

        target_name = (task.target_agent or "").strip().lower() or None
        target = self.agents_by_name.get(target_name) if target_name else None

        # Track the task
        self.tracker.create(task.id, task.text, source="dashboard")
        if target_name:
            self.tracker.assign(task.id, target_name)
        self.status.set_current_task(task.text, assigned_to=target_name)

        if target and target.name != director.name:
            text = f"{target.display_name}, {task.text}"
        elif target and target.name == director.name:
            text = task.text
        else:
            text = f"[Задача от заказчика]\n{task.text}"

        log.info(
            "Задача из дашборда id=%s target=%s -> постим от %s",
            task.id,
            target_name or "auto",
            director.display_name,
        )
        try:
            await director.client.send_message(chat_id, text)
            self.tracker.set_status(task.id, "in_progress")
        except Exception as e:
            log.exception("Не смогли запостить задачу из дашборда: %s", e)
            self.tracker.set_status(task.id, "failed")

    def _director(self) -> AgentRuntime | None:
        for a in self.agents:
            if a.config.role == "director":
                return a
        return None
