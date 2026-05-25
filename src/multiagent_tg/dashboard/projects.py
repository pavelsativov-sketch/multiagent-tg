"""Сканер проектов в workspace/ и генератор index.html-хаба.

"Проектом" считается каждая подпапка первого уровня в workspace/, а также
любые одиночные файлы прямо в корне workspace/ (например, snake-game.html).
Скрытые папки (.hub, .git и т.д.) пропускаются.

Для каждой подпапки собирается:
- имя (slug)
- путь
- список файлов (рекурсивно)
- размер
- mtime (когда последний раз меняли)
- DESIGN.md и README.md, если есть — краткая выдержка
- agents — кто из агентов писал в эту папку (по логу из status board / fs)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


HUB_DIRNAME = ".hub"
MANIFEST_FILENAME = "projects.json"
PREVIEW_BYTES = 800  # сколько символов из README/DESIGN кидать в preview


@dataclass
class ProjectFile:
    path: str          # относительно корня проекта
    size: int
    mtime: float


@dataclass
class Project:
    slug: str          # имя папки или имя одиночного файла
    kind: str          # "folder" | "file"
    path: str          # относительно workspace/
    mtime: float
    total_size: int = 0
    files: list[ProjectFile] = field(default_factory=list)
    readme_preview: str | None = None
    design_preview: str | None = None
    entry_file: str | None = None  # index.html / main.py / etc.

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "kind": self.kind,
            "path": self.path,
            "mtime": self.mtime,
            "total_size": self.total_size,
            "files": [asdict(f) for f in self.files],
            "readme_preview": self.readme_preview,
            "design_preview": self.design_preview,
            "entry_file": self.entry_file,
        }


def _read_preview(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > PREVIEW_BYTES:
        text = text[:PREVIEW_BYTES] + "…"
    return text


def _detect_entry(folder: Path) -> str | None:
    candidates = [
        "index.html", "index.htm",
        "main.py", "app.py",
        "package.json", "pyproject.toml",
        "README.md",
    ]
    for c in candidates:
        if (folder / c).exists():
            return c
    return None


def scan_workspace(workspace_dir: Path) -> list[Project]:
    """Вернуть список проектов в workspace/.

    Подпапки первого уровня — отдельные проекты. Файлы первого уровня —
    тоже отдельные проекты (как одиночные артефакты типа snake.html)."""
    workspace_dir = Path(workspace_dir)
    if not workspace_dir.exists():
        return []

    projects: list[Project] = []

    for entry in sorted(workspace_dir.iterdir(), key=lambda p: p.name.lower()):
        name = entry.name
        if name.startswith(".") or name == HUB_DIRNAME:
            continue
        if name in {"index.html"} and entry.is_file():
            # сгенерированный нами хаб-индекс игнорим, чтобы не зацикливать
            try:
                if "multiagent-tg hub" in entry.read_text(encoding="utf-8", errors="ignore")[:400]:
                    continue
            except Exception:
                pass

        if entry.is_dir():
            files: list[ProjectFile] = []
            total = 0
            latest = entry.stat().st_mtime
            for p in entry.rglob("*"):
                if p.is_file() and not any(part.startswith(".") for part in p.relative_to(entry).parts):
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    files.append(
                        ProjectFile(
                            path=str(p.relative_to(entry)).replace("\\", "/"),
                            size=st.st_size,
                            mtime=st.st_mtime,
                        )
                    )
                    total += st.st_size
                    latest = max(latest, st.st_mtime)

            readme = next((p for p in (entry / "README.md", entry / "readme.md") if p.exists()), None)
            design = next((p for p in (entry / "DESIGN.md", entry / "design.md") if p.exists()), None)

            projects.append(
                Project(
                    slug=name,
                    kind="folder",
                    path=name,
                    mtime=latest,
                    total_size=total,
                    files=files,
                    readme_preview=_read_preview(readme) if readme else None,
                    design_preview=_read_preview(design) if design else None,
                    entry_file=_detect_entry(entry),
                )
            )
        else:
            # одиночный файл-артефакт
            try:
                st = entry.stat()
            except OSError:
                continue
            preview = None
            if entry.suffix.lower() in {".md", ".txt", ".html", ".py", ".js", ".css", ".json"}:
                preview = _read_preview(entry)
            projects.append(
                Project(
                    slug=name,
                    kind="file",
                    path=name,
                    mtime=st.st_mtime,
                    total_size=st.st_size,
                    files=[ProjectFile(path=name, size=st.st_size, mtime=st.st_mtime)],
                    readme_preview=preview,
                    entry_file=name if entry.suffix.lower() in {".html", ".htm"} else None,
                )
            )

    # сортировка: новые первыми
    projects.sort(key=lambda pr: pr.mtime, reverse=True)
    return projects


def write_manifest(workspace_dir: Path, projects: list[Project]) -> Path:
    """Сохранить projects.json в workspace/.hub/."""
    hub_dir = workspace_dir / HUB_DIRNAME
    hub_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = hub_dir / MANIFEST_FILENAME
    payload = {
        "generated_at": time.time(),
        "projects": [p.to_dict() for p in projects],
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def write_index_html(workspace_dir: Path, projects: list[Project]) -> Path:
    """Собрать workspace/index.html — статичную галерею проектов."""
    cards = []
    for pr in projects:
        title = pr.slug
        meta_parts = [pr.kind, _human_size(pr.total_size), _human_time(pr.mtime)]
        if pr.entry_file:
            meta_parts.append(f"entry: {pr.entry_file}")
        meta = " · ".join(meta_parts)

        preview = pr.readme_preview or pr.design_preview or ""
        preview_html = (
            f'<pre class="preview">{_html_escape(preview)}</pre>' if preview else ""
        )

        files_list = "".join(
            f'<li><a href="{_url_quote(pr.path + "/" + f.path) if pr.kind == "folder" else _url_quote(pr.path)}" target="_blank">{_html_escape(f.path)}</a> <span class="dim">{_human_size(f.size)}</span></li>'
            for f in pr.files[:30]
        )
        more = f'<li class="dim">+{len(pr.files) - 30} ещё…</li>' if len(pr.files) > 30 else ""

        open_link = ""
        if pr.entry_file and pr.kind == "folder":
            open_link = f'<a class="open" href="{_url_quote(pr.path + "/" + pr.entry_file)}" target="_blank">открыть →</a>'
        elif pr.kind == "file":
            open_link = f'<a class="open" href="{_url_quote(pr.path)}" target="_blank">открыть →</a>'

        cards.append(
            f"""
            <article class="card">
                <header>
                    <h2>{_html_escape(title)}</h2>
                    <span class="meta">{_html_escape(meta)}</span>
                </header>
                {preview_html}
                <details>
                    <summary>файлы ({len(pr.files)})</summary>
                    <ul>{files_list}{more}</ul>
                </details>
                {open_link}
            </article>
            """
        )

    cards_html = "\n".join(cards) if cards else (
        '<p class="empty">Пока ни одного проекта. Поставьте задачу команде через дашборд.</p>'
    )

    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>multiagent-tg hub · проекты команды</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {{
  color-scheme: dark;
  --bg: #0b1020;
  --card: #131a2e;
  --fg: #e6ecff;
  --muted: #8a93b3;
  --accent: #6366f1;
  --accent-2: #22d3ee;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 32px 24px 64px;
  background: radial-gradient(1200px 600px at 10% -10%, #1a2150 0%, #0b1020 60%);
  color: var(--fg);
  font: 14px/1.45 -apple-system, "Segoe UI", system-ui, sans-serif;
}}
header.page {{
  max-width: 1100px; margin: 0 auto 24px;
}}
h1 {{ margin: 0 0 4px; font-size: 28px; }}
.subtitle {{ color: var(--muted); font-size: 13px; }}
.grid {{
  max-width: 1100px; margin: 0 auto;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 18px;
}}
.card {{
  background: linear-gradient(180deg, #161e36 0%, #111733 100%);
  border: 1px solid #25305a;
  border-radius: 14px;
  padding: 18px;
  display: flex; flex-direction: column; gap: 10px;
  transition: border-color .15s ease, transform .15s ease;
}}
.card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
.card h2 {{ margin: 0; font-size: 16px; }}
.meta {{ color: var(--muted); font-size: 12px; }}
.preview {{
  background: #0a1027; border: 1px solid #1f274a; border-radius: 8px;
  padding: 10px; margin: 0;
  max-height: 140px; overflow: auto;
  font: 12px/1.45 ui-monospace, Menlo, Consolas, monospace;
  color: #c8d3ff;
  white-space: pre-wrap; word-break: break-word;
}}
details summary {{ cursor: pointer; color: var(--muted); font-size: 12px; }}
ul {{ list-style: none; margin: 8px 0 0; padding: 0; max-height: 180px; overflow: auto; }}
ul li {{ padding: 2px 0; font-size: 12px; }}
ul li a {{ color: var(--accent-2); text-decoration: none; }}
ul li a:hover {{ text-decoration: underline; }}
.dim {{ color: var(--muted); }}
.open {{
  margin-top: auto; align-self: flex-start;
  padding: 8px 12px; border-radius: 8px;
  background: var(--accent); color: white;
  text-decoration: none; font-weight: 600; font-size: 13px;
}}
.open:hover {{ background: #4f53d8; }}
.empty {{ max-width: 1100px; margin: 0 auto; color: var(--muted); }}
</style>
</head>
<body>
<header class="page">
    <h1>multiagent-tg hub</h1>
    <div class="subtitle">Все проекты, которые сделала команда. Сгенерировано: {_human_time(time.time())}</div>
</header>
<section class="grid">
{cards_html}
</section>
</body>
</html>
"""
    out = workspace_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


# ----------------------------- утилиты -----------------------------


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _url_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="/")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _human_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
