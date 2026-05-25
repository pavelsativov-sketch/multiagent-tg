"""Дымовой тест: проверяет filesystem tool, shell tool и memory без Telegram/LLM."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from multiagent_tg.memory import Memory
from multiagent_tg.tools.filesystem import FilesystemError, FilesystemTool
from multiagent_tg.tools.shell import ShellError, ShellTool


async def main() -> None:
    tmp = Path(__file__).parent / "tmp_smoke"
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    fs = FilesystemTool(tmp / "workspace")
    print(fs.write_file("site/index.html", "<h1>hi</h1>"))
    assert (tmp / "workspace" / "site" / "index.html").read_text() == "<h1>hi</h1>"
    print("read:", fs.read_file("site/index.html"))
    print("ls:", fs.list_dir())

    try:
        fs.write_file("../../etc/passwd", "evil")
    except FilesystemError as e:
        print("ok, jailbreak attempt blocked:", e)

    sh = ShellTool(tmp / "workspace")
    res = await sh.run("echo hello")
    print("shell:", res)
    assert res["exit_code"] == 0 and "hello" in res["stdout"]

    try:
        await sh.run("rm -rf /")
    except ShellError as e:
        print("ok, 'rm' blocked:", e)

    mem = Memory(tmp / "history.db")
    await mem.init()
    await mem.save(1, -100, "Света", "Привет всем!", 1.0)
    await mem.save(2, -100, "Игорь", "Хай", 2.0)
    await mem.save(3, -100, "Света", "Как дела?", 3.0)
    msgs = await mem.recent(-100)
    print("history:", [(m.sender_name, m.text) for m in msgs])
    assert [m.sender_name for m in msgs] == ["Света", "Игорь", "Света"]

    senders = await mem.last_senders(-100, n=3)
    print("last_senders:", senders)

    print("\nALL OK")


asyncio.run(main())
