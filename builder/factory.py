# builder/factory.py
"""把 factory/ 源渲染成**这个版本的出厂母版**：versions/<版本号>/factory/。

形状由契约定义（builder/paths.py），而且必须**逐字**符合：config.yaml.tmpl + SOUL.md +
skills/。差一点点都不行——运行时那道闸门（tools/factory_state.assert_factory_complete）
会把不合格的母版整个拒掉，而拒掉的代价全落在爸妈的机器上：每次开机重下 895MB、解压
2.87GB、被拒、退出 1，周而复始。所以本模块在返回之前**自己跑一遍同一个闸门函数**
（不是"照着契约再写一遍检查"——那正是契约与实现静默脱钩的方式）：不合格就在装配机上
当场炸掉，那里有维护者盯着。
"""

import shutil
from collections.abc import Callable
from pathlib import Path

from builder.paths import FACTORY_DIR_REL, FACTORY_SKILLS, FACTORY_SOUL
from tools.factory_state import assert_factory_complete

FACTORY_SRC = Path(__file__).resolve().parent.parent / "factory"

# 明确判定"用不了、也修不好"的出厂技能，逐个有名有姓、维护者亲口拍板排除——不是"控制器
# 单方面砍技能"（那正是 2026-07-13 被维护者否决过的事）。每一条都必须能说清楚"为什么留着
# 也没用"：
#   - nano-pdf（PDF 编辑）：2026-07-21 拍板排除。装它需要联网装包，装上了还要另一把从没
#     配置过的第三方 LLM key（它自己的 SKILL.md 原文："requires an API key"）——爸妈这边
#     走这条路必炸。且 assemble_payload 已经把"PDF 处理"这件事用 pymupdf 铺了另一条能走
#     通的路，留着 nano-pdf 只会制造"到底走哪条路"的混乱。
EXCLUDED_SKILLS = ("productivity/nano-pdf",)


def _ignore_excluded_skills(skills_src: Path) -> Callable[[str, list[str]], set[str]]:
    """给 shutil.copytree 用的 ignore 回调：跳过 EXCLUDED_SKILLS 里点名的技能目录，
    其余原样保留。只精确匹配点名的相对路径，不会误伤同目录下的其它技能。"""
    excluded = {tuple(p.split("/")) for p in EXCLUDED_SKILLS}
    root = skills_src.resolve()

    def ignore(dir_path: str, names: list[str]) -> set[str]:
        rel = Path(dir_path).resolve().relative_to(root).parts
        return {name for name in names if rel + (name,) in excluded}

    return ignore


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


def render_factory(dest: Path, skills_src: Path) -> Path:
    """在 dest 下渲染出 factory/（出厂母版），返回它的路径。

    skills_src 是**必传的输入**，不是可选项，本模块也不会替你造一个空的 skills/：
    - 本仓库没有自带技能。出厂技能来自装配机上的 Hermes 快照（Task 7 负责把它并进来），
      仓库里凭空造不出来。
    - 那为什么不干脆铺一个空的 skills/ 把闸门喂饱？因为闸门只检查目录**存在**。一个空的
      出厂技能母版能过闸门、能装机、平时看不出任何异常——直到维护者远程跑一次深度恢复
      （ADR-0005 的"终极刹车"）：它会把机器上的技能全删光，再从这个空母版拷 0 个文件回来。
      一台一个技能都不剩的机器，而 CLI 印的是「小助手深度恢复完成！」。宁可在装配机上
      当场失败——那里有维护者盯着，改一行命令就修好了。
    """
    if not skills_src.is_dir() or not any(p.is_file() for p in skills_src.rglob("*")):
        raise ValueError(
            f"出厂技能母版为空或不存在：{skills_src}。出厂技能来自装配机上的 Hermes 快照，"
            "必须显式传进来（空母版会让深度恢复把机器上的技能清成零）。未产出任何母版。"
        )

    factory = dest / FACTORY_DIR_REL
    if factory.exists():
        shutil.rmtree(factory)
    # 源里的 *.txt 是渲染前的原料（soul.txt），不进母版；config.yaml.tmpl 原样带走。
    shutil.copytree(FACTORY_SRC, factory, ignore=shutil.ignore_patterns("*.txt"))
    (factory / FACTORY_SOUL).write_text(_render_soul(FACTORY_SRC / "soul.txt"), encoding="utf-8")
    shutil.copytree(skills_src, factory / FACTORY_SKILLS, ignore=_ignore_excluded_skills(skills_src))

    # 打包器的产物必须能过运行时那道闸门——**同一个函数**，不是"看起来一样的一份检查"。
    # 顺带也挡住"打包时手滑把 .env / 某台机器的 sessions/ 或一份渲染好的 config.yaml
    # 混进了技能快照"这类事故。
    assert_factory_complete(factory)
    return factory
