"""Загрузка и валидация конфигурации (.env + config/agents.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class AgentConfig:
    name: str
    display_name: str
    role: str
    session: str
    phone: str
    system_prompt: str
    enabled: bool = True
    temperature: float = 0.7
    max_tokens: int = 600
    tools: list[str] = field(default_factory=list)
    max_rounds: int = 20
    model: str | None = None  # если None — берётся llm_model из AppConfig


@dataclass
class AppConfig:
    tg_api_id: int
    tg_api_hash: str
    tg_group_id: int

    llm_base_url: str
    llm_api_key: str
    llm_model: str

    reply_delay_min: float
    reply_delay_max: float
    context_window: int
    max_consecutive_replies: int

    workspace_dir: Path
    sessions_dir: Path
    data_dir: Path
    log_level: str

    agents: list[AgentConfig]  # только enabled

    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    all_agents: list[AgentConfig] = field(default_factory=list)  # вкл. disabled


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Переменная окружения {name} не задана. Скопируйте .env.example в .env "
            f"и заполните её."
        )
    return value


def load_config(
    env_path: Path | str | None = None,
    agents_yaml_path: Path | str | None = None,
    project_root: Path | str | None = None,
) -> AppConfig:
    """Загрузить .env + config/agents.yaml и собрать AppConfig.

    Корень проекта определяется так:
      - если задан project_root - он
      - иначе родительский каталог пакета (../..)
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent
    project_root = Path(project_root)

    env_path = Path(env_path) if env_path else project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    agents_yaml_path = (
        Path(agents_yaml_path) if agents_yaml_path else project_root / "config" / "agents.yaml"
    )
    if not agents_yaml_path.exists():
        raise FileNotFoundError(f"Конфиг агентов не найден: {agents_yaml_path}")

    with open(agents_yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    defaults = raw.get("defaults") or {}
    default_temperature = float(defaults.get("temperature", 0.7))
    default_max_tokens = int(defaults.get("max_tokens", 600))
    default_max_rounds = int(os.getenv("MAX_ROUNDS", defaults.get("max_rounds", 20)))
    default_model = defaults.get("model") or None

    all_agents: list[AgentConfig] = []
    for entry in raw.get("agents", []):
        all_agents.append(
            AgentConfig(
                name=entry["name"],
                display_name=entry["display_name"],
                role=entry["role"],
                session=entry["session"],
                phone=entry["phone"],
                system_prompt=entry["system_prompt"].strip(),
                enabled=bool(entry.get("enabled", True)),
                temperature=float(entry.get("temperature", default_temperature)),
                max_tokens=int(entry.get("max_tokens", default_max_tokens)),
                tools=list(entry.get("tools", [])),
                max_rounds=int(entry.get("max_rounds", default_max_rounds)),
                model=entry.get("model") or default_model,
            )
        )

    if not all_agents:
        raise RuntimeError("В config/agents.yaml нет ни одного агента.")

    names = [a.name for a in all_agents]
    if len(names) != len(set(names)):
        raise RuntimeError(f"Имена агентов должны быть уникальны: {names}")

    agents = [a for a in all_agents if a.enabled]
    if not agents:
        raise RuntimeError(
            "В config/agents.yaml нет ни одного агента с enabled: true. "
            "Включите хотя бы одного."
        )

    workspace_dir = Path(os.getenv("WORKSPACE_DIR", project_root / "workspace"))
    if not workspace_dir.is_absolute():
        workspace_dir = project_root / workspace_dir
    workspace_dir.mkdir(parents=True, exist_ok=True)

    sessions_dir = project_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Если у агента не указана модель — берём LLM_MODEL из .env как fallback.
    env_model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    for a in all_agents:
        if not a.model:
            a.model = env_model

    return AppConfig(
        tg_api_id=int(_require_env("TG_API_ID")),
        tg_api_hash=_require_env("TG_API_HASH"),
        tg_group_id=int(_require_env("TG_GROUP_ID")),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", "ollama"),
        llm_model=env_model,
        reply_delay_min=float(os.getenv("REPLY_DELAY_MIN", "4")),
        reply_delay_max=float(os.getenv("REPLY_DELAY_MAX", "18")),
        context_window=int(os.getenv("CONTEXT_WINDOW", "40")),
        max_consecutive_replies=int(os.getenv("MAX_CONSECUTIVE_REPLIES", "2")),
        workspace_dir=workspace_dir.resolve(),
        sessions_dir=sessions_dir.resolve(),
        data_dir=data_dir.resolve(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8000")),
        agents=agents,
        all_agents=all_agents,
    )
