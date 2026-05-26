"""Файловая шина между оркестратором и дашбордом.

Дашборд и оркестратор — разные процессы. Сессионный файл Telethon
(sveta.session, sqlite) одновременно из двух процессов открыть нельзя,
поэтому дашборд НЕ говорит с TG напрямую: он только пишет задачи в
очередь, а оркестратор разгребает её и постит сообщения от Светы
в TG-группу. Статус агентов наоборот — пишется оркестратором, читается
дашбордом.

Все файлы лежат в `data/` рядом с history.db.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


TASK_QUEUE_FILENAME = "task_queue.jsonl"
TASK_DONE_FILENAME = "task_queue.done"  # offset до которого оркестратор уже обработал
STATUS_FILENAME = "agent_status.json"


@dataclass
class TaskRequest:
    """Задача от человека (через дашборд) для команды агентов."""
    id: str               # уникальный id (timestamp + случайное)
    text: str             # текст задачи
    target_agent: str | None = None  # имя агента (sveta/igor/...) или None = адресовать Свете
    created_at: float = field(default_factory=time.time)
    source: str = "dashboard"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> TaskRequest:
        data = json.loads(line)
        return cls(**data)


class TaskQueue:
    """Append-only JSONL-очередь. Один писатель (дашборд), один читатель (оркестратор)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.data_dir / TASK_QUEUE_FILENAME
        self.done_path = self.data_dir / TASK_DONE_FILENAME

    def append(self, task: TaskRequest) -> None:
        # Атомарно дописываем строку в очередь.
        with open(self.queue_path, "a", encoding="utf-8") as f:
            f.write(task.to_json() + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    def read_new(self) -> list[TaskRequest]:
        """Читает все новые строки очереди (после offset из task_queue.done).
        Возвращает список новых задач и обновляет offset."""
        if not self.queue_path.exists():
            return []
        try:
            offset = int(self.done_path.read_text(encoding="utf-8").strip()) if self.done_path.exists() else 0
        except ValueError:
            offset = 0

        size = self.queue_path.stat().st_size
        if offset >= size:
            return []

        new_tasks: list[TaskRequest] = []
        with open(self.queue_path, encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    new_tasks.append(TaskRequest.from_json(line))
                except Exception as e:
                    log.warning("Битая строка в очереди задач: %s (%s)", line[:100], e)
            new_offset = f.tell()
        self.done_path.write_text(str(new_offset), encoding="utf-8")
        return new_tasks


class StatusBoard:
    """Снапшот текущего состояния агентов: онлайн, что делает.

    Пишется оркестратором (после каждого изменения), читается дашбордом
    (поллингом). Простой JSON-файл."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STATUS_FILENAME
        self._state: dict[str, Any] = {
            "updated_at": time.time(),
            "agents": {},
            "tasks_processed": 0,
            "current_task": None,
        }
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def _flush(self) -> None:
        self._state["updated_at"] = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set_agent(self, name: str, **fields: Any) -> None:
        self._load()  # merge with changes from other process
        agents = self._state.setdefault("agents", {})
        cur = agents.setdefault(name, {})
        cur.update(fields)
        cur["updated_at"] = time.time()
        self._flush()

    def increment_processed(self) -> None:
        self._load()
        self._state["tasks_processed"] = int(self._state.get("tasks_processed", 0)) + 1
        self._flush()

    def set_current_task(self, text: str, assigned_to: str | None = None) -> None:
        self._load()
        self._state["current_task"] = {
            "text": text[:200],
            "assigned_to": assigned_to,
            "started_at": time.time(),
        }
        self._flush()

    def clear_current_task(self) -> None:
        self._load()
        self._state["current_task"] = None
        self._flush()

    def snapshot(self) -> dict[str, Any]:
        self._load()
        return dict(self._state)
