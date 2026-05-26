"""Трекинг задач: хранение, статусы, история выполнения.

Каждая задача проходит цикл: created → assigned → in_progress → review → done/failed.
Результаты сохраняются рядом с задачей для последующего просмотра в дашборде.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TASKS_FILENAME = "tasks.json"


@dataclass
class TaskResult:
    """Результат выполнения задачи."""

    agent_name: str
    summary: str
    files: list[str] = field(default_factory=list)
    completed_at: float = field(default_factory=time.time)


@dataclass
class TrackedTask:
    """Задача с полным жизненным циклом."""

    id: str
    text: str
    status: str = "created"  # created | assigned | in_progress | review | done | failed
    source: str = "telegram"  # telegram | dashboard
    created_at: float = field(default_factory=time.time)
    assigned_to: str | None = None
    assigned_at: float | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskTracker:
    """Persistent JSON-based task storage."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / TASKS_FILENAME
        self._tasks: dict[str, TrackedTask] = {}
        self._load()

    def reload(self) -> None:
        """Reload tasks from disk (picks up changes by other processes)."""
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for task_data in raw.get("tasks", []):
                    task = TrackedTask(
                        id=task_data["id"],
                        text=task_data["text"],
                        status=task_data.get("status", "created"),
                        source=task_data.get("source", "telegram"),
                        created_at=task_data.get("created_at", 0),
                        assigned_to=task_data.get("assigned_to"),
                        assigned_at=task_data.get("assigned_at"),
                        results=task_data.get("results", []),
                        finished_at=task_data.get("finished_at"),
                    )
                    self._tasks[task.id] = task
            except Exception as exc:
                log.warning("Не удалось загрузить tasks.json: %s", exc)

    def _flush(self) -> None:
        payload = {
            "updated_at": time.time(),
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, task_id: str) -> TrackedTask | None:
        return self._tasks.get(task_id)

    def create(self, task_id: str, text: str, source: str = "telegram") -> TrackedTask:
        if task_id in self._tasks:
            log.debug("Task already exists: id=%s, skipping create", task_id)
            return self._tasks[task_id]
        task = TrackedTask(id=task_id, text=text, source=source)
        self._tasks[task_id] = task
        self._flush()
        log.info("Task created: id=%s source=%s", task_id, source)
        return task

    def assign(self, task_id: str, agent_name: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        task.assigned_to = agent_name
        task.assigned_at = time.time()
        task.status = "assigned"
        self._flush()

    def set_status(self, task_id: str, status: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        task.status = status
        if status in ("done", "failed"):
            task.finished_at = time.time()
        self._flush()

    def add_result(self, task_id: str, agent_name: str, summary: str, files: list[str] | None = None) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        result = TaskResult(agent_name=agent_name, summary=summary, files=files or [])
        task.results.append(asdict(result))
        self._flush()

    def recent(self, limit: int = 50) -> list[TrackedTask]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    def active(self) -> list[TrackedTask]:
        return [t for t in self._tasks.values() if t.status not in ("done", "failed")]

    def by_agent(self, agent_name: str) -> list[TrackedTask]:
        return [t for t in self._tasks.values() if t.assigned_to == agent_name]

    def snapshot(self) -> dict[str, Any]:
        self._load()
        return {
            "total": len(self._tasks),
            "active": len(self.active()),
            "tasks": [t.to_dict() for t in self.recent(100)],
        }
