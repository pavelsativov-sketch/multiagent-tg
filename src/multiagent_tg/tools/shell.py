"""Запуск shell-команд для dev-агента. Whitelist + рабочий каталог в workspace/."""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

log = logging.getLogger(__name__)


# Программы, которые разрешено вызывать. Любая другая -> отказ.
DEFAULT_ALLOWED = {
    "ls",
    "cat",
    "mkdir",
    "touch",
    "git",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "node",
    "python",
    "python3",
    "pip",
    "uv",
    "echo",
    "cp",
    "mv",
}


class ShellError(Exception):
    pass


class ShellTool:
    def __init__(self, workspace_dir: Path, allowed: set[str] | None = None, timeout: int = 60):
        self.cwd = workspace_dir.resolve()
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.allowed = set(allowed) if allowed else DEFAULT_ALLOWED
        self.timeout = timeout

    async def run(self, command: str) -> dict:
        try:
            parts = shlex.split(command)
        except ValueError as e:
            raise ShellError(f"Не удалось распарсить команду: {e}") from e
        if not parts:
            raise ShellError("Пустая команда")
        program = parts[0]
        if program not in self.allowed:
            raise ShellError(
                f"Команда '{program}' не разрешена. Разрешены: {sorted(self.allowed)}"
            )

        import shutil
        import sys

        args = parts[1:]
        if sys.platform == "win32":
            if program == "echo":
                resolved_args = [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))"] + args
            elif program == "cat":
                resolved_args = [sys.executable, "-c", "import sys; [print(open(f, encoding='utf-8', errors='replace').read(), end='') for f in sys.argv[1:]]"] + args
            elif program == "mkdir":
                resolved_args = [sys.executable, "-c", "import sys, os; [os.makedirs(d, exist_ok=True) for d in sys.argv[1:] if d != '-p']"] + args
            elif program == "touch":
                resolved_args = [sys.executable, "-c", "import sys, pathlib; [pathlib.Path(f).touch() for f in sys.argv[1:]]"] + args
            elif program == "ls":
                resolved_args = [sys.executable, "-c", "import sys, os; d = sys.argv[1] if len(sys.argv) > 1 else '.'; print('\\n'.join(os.listdir(d)))"] + args
            elif program == "cp":
                resolved_args = [sys.executable, "-c", "import sys, shutil; shutil.copy(sys.argv[1], sys.argv[2])"] + args
            elif program == "mv":
                resolved_args = [sys.executable, "-c", "import sys, shutil; shutil.move(sys.argv[1], sys.argv[2])"] + args
            else:
                target_prog = program
                if program == "python3" and not shutil.which("python3"):
                    target_prog = "python"
                resolved = shutil.which(target_prog)
                if not resolved:
                    raise ShellError(f"Исполняемый файл '{program}' не найден в PATH.")
                resolved_args = [resolved] + args
        else:
            resolved_args = parts

        log.info("shell.run: %s (cwd=%s)", command, self.cwd)
        proc = await asyncio.create_subprocess_exec(
            *resolved_args,
            cwd=str(self.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ShellError(f"Команда превысила таймаут {self.timeout}s") from None

        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
        }

    @staticmethod
    def openai_schema() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": (
                        "Запустить shell-команду в каталоге workspace/. "
                        "Разрешён ограниченный список команд: git, npm, npx, node, "
                        "python, uv, ls, cat, mkdir, touch, cp, mv, echo. "
                        "Возвращает exit_code/stdout/stderr."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Команда целиком, например 'npm install'",
                            },
                        },
                        "required": ["command"],
                    },
                },
            }
        ]
