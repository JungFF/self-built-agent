# builder/factory.py
"""把 factory/ 源渲染成可部署的出厂 payload。"""

import shutil
from pathlib import Path

FACTORY_SRC = Path(__file__).resolve().parent.parent / "factory"


def _render_soul(src: Path) -> str:
    """把 soul.txt 渲染成 SOUL.md 的内容。

    soul.txt 的第一行是源文件头注释（跟 builder/paths.py、builder/factory.py 一样，
    方便人在仓库里认出这是哪个文件），但 SOUL.md 的内容会原样进 agent 的 system
    prompt —— 一行 "# factory/soul.txt —— ..." 会被渲染成一条突兀的 markdown 标题，
    混进 persona 里。所以剥掉这一行头注释，只留正文。
    """
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    return "".join(lines).lstrip("\n")


def render_factory(dest: Path) -> Path:
    payload = dest / "factory_payload"
    if payload.exists():
        shutil.rmtree(payload)
    shutil.copytree(FACTORY_SRC, payload, ignore=shutil.ignore_patterns("*.txt"))
    soul = _render_soul(FACTORY_SRC / "soul.txt")
    (payload / "SOUL.md").write_text(soul, encoding="utf-8")
    return payload
