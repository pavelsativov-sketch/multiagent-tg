"""Точка входа CLI: login сессий, запуск всех агентов."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from telethon import TelegramClient

from multiagent_tg.agent import AgentRuntime, make_telegram_client
from multiagent_tg.config import load_config
from multiagent_tg.llm import LLMClient
from multiagent_tg.memory import Memory
from multiagent_tg.orchestrator import Orchestrator
from multiagent_tg.tools import FilesystemTool, ShellTool, TelegramTool

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Multiagent Telegram bots.")
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=False)],
    )


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Перезаписать существующий .env."),
) -> None:
    """Интерактивно создать .env (спросит TG_API_ID, TG_API_HASH и т.д.)."""
    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"
    example_path = project_root / ".env.example"

    if env_path.exists() and not force:
        console.print(f"[yellow].env уже существует ({env_path}). Используйте --force, чтобы перезаписать.[/]")
        return

    console.rule("[bold]Создание .env")
    console.print(
        "Вам понадобятся [bold]TG_API_ID[/] и [bold]TG_API_HASH[/] с https://my.telegram.org/apps\n"
        "(раздел 'API development tools' → создать приложение → скопировать api_id и api_hash).\n"
    )

    while True:
        tg_api_id = console.input("[bold]TG_API_ID[/] (число): ").strip()
        if tg_api_id.isdigit():
            break
        console.print("[red]Должно быть число. Попробуйте ещё раз.[/]")

    while True:
        tg_api_hash = console.input("[bold]TG_API_HASH[/] (длинная hex-строка): ").strip()
        if len(tg_api_hash) >= 16:
            break
        console.print("[red]Слишком короткая - проверьте что скопировали целиком.[/]")

    tg_group_id = console.input(
        "[bold]TG_GROUP_ID[/] [dim](Enter если ещё не создали группу - впишете потом)[/]: "
    ).strip() or "0"

    console.print(
        "\n[dim]LLM по умолчанию = Google Gemini (быстро, бесплатно в free tier).[/]\n"
        "[dim]Ключ берётся на https://aistudio.google.com/app/apikey[/]"
    )
    llm_provider = console.input(
        "[bold]LLM провайдер[/] [dim](Enter = gemini; варианты: gemini / openrouter / ollama / openai)[/]: "
    ).strip().lower() or "gemini"

    if llm_provider == "ollama":
        llm_base_url = "http://localhost:11434/v1"
        llm_api_key = "ollama"
        console.print(
            "[dim]Лучшие модели: qwen3:32b (мощь), qwen3:14b (баланс), llama4:scout (Meta)[/]"
        )
        llm_model = console.input(
            "[bold]LLM_MODEL[/] [dim](Enter для qwen3:14b)[/]: "
        ).strip() or "qwen3:14b"
    elif llm_provider == "openrouter":
        llm_base_url = "https://openrouter.ai/api/v1"
        llm_api_key = console.input("[bold]OpenRouter API key[/] (sk-or-v1-...): ").strip()
        console.print(
            "[dim]Лучшие бесплатные: deepseek/deepseek-r1:free, "
            "qwen/qwen3-235b-a22b:free, meta-llama/llama-4-maverick:free[/]"
        )
        llm_model = console.input(
            "[bold]LLM_MODEL[/] [dim](Enter для deepseek/deepseek-r1:free)[/]: "
        ).strip() or "deepseek/deepseek-r1:free"
    elif llm_provider == "openai":
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = console.input("[bold]OpenAI API key[/] (sk-...): ").strip()
        console.print(
            "[dim]Лучшие: o4-mini (reasoning, $1.10/M), gpt-4.1-mini (быстрый, $0.40/M), gpt-4.1 (код, $2/M)[/]"
        )
        llm_model = console.input(
            "[bold]LLM_MODEL[/] [dim](Enter для gpt-4.1-mini)[/]: "
        ).strip() or "gpt-4.1-mini"
    else:  # gemini
        llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        while True:
            llm_api_key = console.input("[bold]Gemini API key[/] (AIza...): ").strip()
            if len(llm_api_key) >= 20:
                break
            console.print("[red]Слишком короткий - проверьте что скопировали целиком.[/]")
        llm_model = console.input(
            "[bold]LLM_MODEL[/] [dim](Enter для gemini-2.5-flash)[/]: "
        ).strip() or "gemini-2.5-flash"

    # Шаблон
    template = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
    replacements = {
        "TG_API_ID=1234567": f"TG_API_ID={tg_api_id}",
        "TG_API_HASH=abcdef1234567890abcdef1234567890": f"TG_API_HASH={tg_api_hash}",
        "TG_GROUP_ID=-1001234567890": f"TG_GROUP_ID={tg_group_id}",
        "LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/": f"LLM_BASE_URL={llm_base_url}",
        "LLM_API_KEY=YOUR_GEMINI_API_KEY": f"LLM_API_KEY={llm_api_key}",
        "LLM_MODEL=gemini-2.5-flash": f"LLM_MODEL={llm_model}",
    }
    out = template
    for k, v in replacements.items():
        out = out.replace(k, v)
    env_path.write_text(out, encoding="utf-8")

    console.print(f"\n[green]Готово![/] Записал {env_path}")
    if tg_group_id == "0":
        console.print(
            "[yellow]Внимание:[/] TG_GROUP_ID=0. Создайте группу в Telegram, добавьте туда "
            "Светлу (после login), узнайте id через `multiagent-tg group-id sveta` и впишите в .env вручную."
        )


@app.command()
def agents() -> None:
    """Показать сконфигурированных агентов (включая disabled)."""
    cfg = load_config()
    table = Table(title="Agents")
    table.add_column("name")
    table.add_column("enabled")
    table.add_column("display")
    table.add_column("role")
    table.add_column("phone")
    table.add_column("session file")
    table.add_column("model")
    table.add_column("tools")
    for a in cfg.all_agents:
        session_file = cfg.sessions_dir / f"{a.session}.session"
        exists = "OK" if session_file.exists() else "—"
        en = "[green]on[/]" if a.enabled else "[dim]off[/]"
        table.add_row(
            a.name,
            en,
            a.display_name,
            a.role,
            a.phone,
            f"{session_file.name} [{exists}]",
            a.model or cfg.llm_model,
            ",".join(a.tools) or "—",
        )
    console.print(table)


@app.command()
def login(
    only: str | None = typer.Option(
        None, help="Логинить только агента с этим name (по умолчанию - все)."
    ),
) -> None:
    """Интерактивный логин каждого агента (запрос SMS-кода/пароля 2FA).

    Запускайте ОДИН РАЗ для каждого аккаунта. После этого .session-файл
    хранит авторизацию.
    """
    cfg = load_config()
    _setup_logging(cfg.log_level)

    async def _login_one(client: TelegramClient, phone: str) -> None:
        await client.connect()
        if await client.is_user_authorized():
            console.print("[green]уже залогинен[/]")
            return
        await client.send_code_request(phone)
        code = console.input(f"[bold]Код из SMS/Telegram для {phone}:[/] ")
        try:
            await client.sign_in(phone=phone, code=code)
        except Exception:
            password = console.input("[bold]Пароль 2FA: [/]", password=True)
            await client.sign_in(password=password)
        me = await client.get_me()
        console.print(f"[green]OK[/] - {me.first_name} (@{me.username}) id={me.id}")

    async def _run() -> None:
        for agent_cfg in cfg.all_agents:
            if only and agent_cfg.name != only:
                continue
            console.rule(f"[bold]{agent_cfg.display_name} ({agent_cfg.name})")
            session_path = cfg.sessions_dir / agent_cfg.session
            client = await make_telegram_client(session_path, cfg.tg_api_id, cfg.tg_api_hash)
            try:
                await _login_one(client, agent_cfg.phone)
            finally:
                await client.disconnect()

    asyncio.run(_run())


@app.command()
def group_id(
    agent_name: str = typer.Argument(..., help="Имя агента, чьим клиентом подключаться."),
) -> None:
    """Подсказать TG_GROUP_ID: подключиться, перечислить диалоги.

    Пример: добавьте всех агентов в группу, потом запустите эту команду -
    в выводе будет id нужного чата (как правило, отрицательное число).
    """
    cfg = load_config()
    _setup_logging(cfg.log_level)
    target = next((a for a in cfg.all_agents if a.name == agent_name), None)
    if not target:
        console.print(f"[red]Агент {agent_name} не найден.[/]")
        sys.exit(1)

    async def _run() -> None:
        session_path = cfg.sessions_dir / target.session
        client = await make_telegram_client(session_path, cfg.tg_api_id, cfg.tg_api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            console.print(f"[red]Агент {agent_name} не залогинен. Запустите login.[/]")
            return
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                console.print(f"id={dialog.id}  name={dialog.name!r}")
        await client.disconnect()

    asyncio.run(_run())


@app.command()
def doctor() -> None:
    """Проверить, что .env, agents.yaml и LLM endpoint в порядке."""
    cfg = load_config()
    _setup_logging(cfg.log_level)
    console.print(f"[bold]config OK[/]: {len(cfg.agents)} agents, group={cfg.tg_group_id}")

    async def _check_llm() -> None:
        llm = LLMClient(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)
        from multiagent_tg.llm import ChatMessage as _CM

        text = await llm.chat_text(
            [_CM(role="user", content="Скажи 'ok' одним словом.")],
            temperature=0,
            max_tokens=100,
        )
        console.print(f"[green]LLM ответил:[/] {text!r}")

    try:
        asyncio.run(_check_llm())
    except Exception as e:
        console.print(f"[red]LLM не отвечает:[/] {e}")
        if "googleapis" in cfg.llm_base_url:
            console.print(
                "Похоже, проблема с Gemini. Проверьте что LLM_API_KEY в .env "
                "правильный (https://aistudio.google.com/app/apikey) "
                "и есть доступ в интернет."
            )
        elif "localhost" in cfg.llm_base_url:
            console.print(
                "Похоже, локальная LLM не запущена. Если Ollama: "
                f"`ollama serve` + `ollama pull {cfg.llm_model}`."
            )
        else:
            console.print(f"Проверьте LLM_BASE_URL={cfg.llm_base_url} и LLM_API_KEY в .env.")
        sys.exit(1)


@app.command()
def dashboard(
    host: str = typer.Option(None, help="Хост дашборда (по умолчанию из .env или 127.0.0.1)."),
    port: int = typer.Option(None, help="Порт дашборда (по умолчанию из .env или 8000)."),
) -> None:
    """Запустить 3D-дашборд отдельно (без ТГ-агентов).

    Дашборд показывает команду в 3D, позволяет ставить задачи через форму,
    отслеживать статусы агентов и просматривать проекты в workspace/.
    """
    cfg = load_config()
    _setup_logging(cfg.log_level)
    from multiagent_tg.dashboard import serve as _serve_dashboard

    actual_host = host or cfg.dashboard_host
    actual_port = port or cfg.dashboard_port
    console.print(f"[bold]Дашборд:[/] http://{actual_host}:{actual_port}/")
    _serve_dashboard(cfg, host=actual_host, port=actual_port)


@app.command()
def run(
    with_dashboard: bool = typer.Option(True, help="Запустить 3D-дашборд вместе с агентами."),
) -> None:
    """Запуск всех агентов. Висит в форграунде, Ctrl+C - выход."""
    cfg = load_config()
    _setup_logging(cfg.log_level)
    if cfg.tg_group_id == 0:
        console.print(
            "[red]TG_GROUP_ID не задан![/] Впишите id вашей группы в .env. "
            "Узнать id можно через `multiagent-tg group-id sveta` или groupid.bat."
        )
        sys.exit(1)
    console.print(f"[bold]LLM endpoint:[/] {cfg.llm_base_url}")
    console.print(f"[bold]Group:[/] {cfg.tg_group_id}")
    console.print(
        "[bold]Agents:[/] "
        + ", ".join(f"{a.display_name}({a.role}, {a.model or cfg.llm_model})" for a in cfg.agents)
    )

    if with_dashboard:
        from multiagent_tg.dashboard import serve_in_background

        serve_in_background(cfg)
        console.print(
            f"[bold]Дашборд:[/] http://{cfg.dashboard_host}:{cfg.dashboard_port}/"
        )

    async def _run() -> None:
        memory = Memory(cfg.data_dir / "history.db")
        await memory.init()

        runtimes: list[AgentRuntime] = []
        for agent_cfg in cfg.agents:
            session_path = cfg.sessions_dir / agent_cfg.session
            client = await make_telegram_client(session_path, cfg.tg_api_id, cfg.tg_api_hash)
            fs = FilesystemTool(cfg.workspace_dir) if "filesystem" in agent_cfg.tools else None
            sh = ShellTool(cfg.workspace_dir) if "shell" in agent_cfg.tools else None
            tg = (
                TelegramTool(client, cfg.tg_group_id, cfg.workspace_dir)
                if "telegram" in agent_cfg.tools
                else None
            )
            agent_llm = LLMClient(
                cfg.llm_base_url, cfg.llm_api_key, agent_cfg.model or cfg.llm_model
            )
            runtimes.append(
                AgentRuntime(config=agent_cfg, client=client, llm=agent_llm, fs=fs, sh=sh, tg=tg)
            )

        orchestrator = Orchestrator(cfg, runtimes, memory)
        try:
            await orchestrator.start()
            await asyncio.gather(*(r.client.run_until_disconnected() for r in runtimes))
        finally:
            for r in runtimes:
                try:
                    await r.client.disconnect()
                except Exception:
                    pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Остановлено пользователем.[/]")


if __name__ == "__main__":
    app()
