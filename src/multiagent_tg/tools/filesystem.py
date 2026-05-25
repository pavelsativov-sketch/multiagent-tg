"""Файловые операции для dev-агента. Всё ограничено корнем workspace/."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class FilesystemError(Exception):
    pass


class FilesystemTool:
    """Безопасная запись/чтение файлов внутри workspace/.

    Любой путь вне workspace отвергается. Так dev-агент не может случайно
    (или специально) пописать в .env или sessions/.
    """

    def __init__(self, workspace_dir: Path):
        self.root = workspace_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.written_files: list[str] = []

    def _resolve(self, relative: str) -> Path:
        # Нормализуем и проверяем, что не вылезли за корень.
        p = (self.root / relative).resolve()
        try:
            p.relative_to(self.root)
        except ValueError as e:
            raise FilesystemError(
                f"Путь '{relative}' вне разрешённого workspace ({self.root})"
            ) from e
        return p

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.written_files.append(str(target.relative_to(self.root)))
        log.info("filesystem.write_file %s (%d байт)", target, len(content))
        return f"Записано {target.relative_to(self.root)} ({len(content)} байт)"

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            raise FilesystemError(f"Файл не найден: {path}")
        return target.read_text(encoding="utf-8")

    def list_dir(self, path: str = ".") -> list[str]:
        target = self._resolve(path)
        if not target.exists():
            return []
        return sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())

    @staticmethod
    def openai_schema() -> list[dict]:
        """JSON-schema инструментов для OpenAI tool-calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": (
                        "Записать содержимое в файл внутри ./workspace/. "
                        "Создаёт промежуточные папки автоматически. "
                        "Используется для скаффолдинга сайтов."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Относительный путь от workspace/, "
                                "например 'my-site/package.json'",
                            },
                            "content": {
                                "type": "string",
                                "description": "Полное содержимое файла",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Прочитать файл из workspace/.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "description": "Список содержимого папки в workspace/.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "default": "."},
                        },
                    },
                },
            },
        ]
