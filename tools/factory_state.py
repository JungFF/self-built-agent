# tools/factory_state.py
"""把某个版本的**出厂状态**应用到 data/（HERMES_HOME）。每次切版本都要跑这一步。

铁律二和铁律三的张力就化解在这里（原文见 builder/paths.py 的模块 docstring）：
用户的东西（.env、聊天记录、习得技能）跨版本存活；出厂状态（config.yaml、SOUL.md、
出厂技能）属于**版本**，随版本下发。所以：

    versions/<v>/factory/config.yaml.tmpl  --渲染--> data/config.yaml
    versions/<v>/factory/SOUL.md           --覆盖--> data/SOUL.md
    versions/<v>/factory/skills/*          --覆盖--> data/skills/*   （只覆盖，绝不删除）
    data/.env、sessions/、memories/、logs/  --绝不触碰-->

不跑这一步的后果：新版本只能改代码，永远改不了 persona/配置——而 ADR-0003 里"发行版"
的定义恰恰是"钉死的 Hermes + 出厂配置/persona/技能，打成一个整体包"，Task 10 的验收项
就是"改一行 SOUL.md 发个新版"。

为什么是独立模块，而不是塞在 updater.py 里：
- Task 6 的启动器**回滚**时也必须应用一遍出厂状态（回滚回旧版本 = 一次版本切换），
  而启动器 import updater 会连带 import cryptography（验签用）。那是启动路径上一条
  没必要的依赖：cryptography 一旦装坏（轮子跟 Python 版本对不上、DLL 被杀软隔离），
  启动器就起不来——而回滚恰恰是用来救这种机器的。本模块只依赖标准库。
- recover.py（长辈按的那个「修复」按钮）要用同一份渲染逻辑。渲染规则只能有一份：
  两份就会静默分叉，而分叉的表现是"修复完之后配置跟出厂不一样"，没人看得出来。

⚠️ 失败时机器处在什么状态（这是 apply_factory_state 唯一不能"当作没发生"的地方）：
调用方（更新器）必须在**切完 current.txt 之后**才调用本函数。反过来的顺序（先应用出厂
状态再切 current.txt）会得到"旧代码 + 新配置"，那是更危险的一半：新配置里的键旧代码可能
根本不认。代价是本函数落在**提交点之后**——它失败的时候，current.txt 已经指向新版本了。
所以失败态要分两类，它们的收敛方式完全不同：

  - **每次都会以同样方式失败的（机器状态）**：包里的出厂母版不合格（缺文件、模板没占位符、
    混进了 .env）、data/skills 是符号链接、工作台目录建不出来。这类失败重跑一万次也是同一个
    结果，绝不能让它发生在提交点之后。所以调用方必须在**切 current.txt 之前**就跑
    assert_factory_complete()（校验解压出来的新母版）和 precheck_machine_state()（校验这台
    机器）——不合格的包/机器整个拒掉，机器完全没变，响亮地失败。

  - **重跑可能就好的（瞬时 I/O）**：权限、磁盘满、杀软临时锁住某个文件。这类失败可以落在
    提交点之后。此时机器跑的是新代码 + 部分（或全部）旧的出厂状态。本函数写的每个文件
    （config.yaml、SOUL.md、每一个出厂技能文件）都是 tmp + os.replace 原子换名写的，所以
    每个文件要么整份新、要么整份旧，绝不会是截断的半个 YAML / 半个 SKILL.md。旧的
    config.yaml / SOUL.md 是上一版下发的、合法的文件，新代码读它能起来——机器**可用**，
    只是 persona 陈旧。

  - 中间态怎么收敛：本函数幂等，重跑即可。但"总会有人重跑它"不能是一句愿望——它必须被
    **凭据**驱动。data/.factory_version（FACTORY_STAMP）就是那个凭据：本函数在**全部写完
    之后**才落这个戳。current.txt 里的版本和戳对不上 = 出厂状态没收敛，必须重新应用。
    更新器每次登录都跑（而且持着锁），它在自己每次运行的开头做这次校对——不做的话，一次
    失败的应用会让机器**永久**停在"新代码 + 旧 persona"上：下一次运行看到 current.txt
    已经是最新版本，直接判"无需更新"、退出 0，心跳还报着新版本号，维护者收不到任何信号。
    Task 6 的启动器也该做同样的校对（纵深防御），但更新器不能依赖它。
"""

import contextlib
import os
from pathlib import Path

from builder.paths import (
    CONFIG_FILE,
    DATA_DIR_REL,
    ENV_FILE,
    FACTORY_CONFIG_TMPL,
    FACTORY_DIR_REL,
    FACTORY_SKILLS,
    FACTORY_SOUL,
    FACTORY_STAMP,
    SKILLS_DIR,
    USER_OWNED,
    VERSIONS_DIR,
    WORKSPACE_DIRNAME,
)

WORKSPACE_PLACEHOLDER = "{{WORKSPACE_DIR}}"


def default_workspace_dir() -> Path:
    """桌面上的「小助手」工作台绝对路径（config.yaml 里 terminal.cwd 的值）。

    为什么是"每次现算"而不是"安装时存一份下来"：修复工具是坏掉的机器上最后一根稻草，
    它不能依赖任何自己正要修的状态——存在 data/ 里的任何东西都可能正是被弄坏的那个。
    现算是确定性的，而且安装器 / 更新器 / 修复器共用这一个函数，三者算出来的值必然
    一致（各算各的才会静默分叉：更新器把 cwd 渲染到一个跟安装器不同的目录，长辈的
    文件就"不见了"）。算错了也有退路：两个 CLI 都有 --workspace 覆盖，维护者远程
    ToDesk 进去能手动纠正。

    ⚠️ 这个值是**按 Windows 用户算的**（桌面是每个用户各自的）。所以"登录时更新"的
    计划任务和桌面快捷方式都必须**以最终用户的身份**运行，不能是 SYSTEM——SYSTEM 的
    桌面是 C:\\Windows\\system32\\config\\systemprofile\\Desktop，渲染进 config.yaml
    之后长辈永远看不到 agent 写出来的文件。Task 6 / Task 8 注册计划任务时要盯住这条。
    """
    return _desktop_dir() / WORKSPACE_DIRNAME


def _desktop_dir() -> Path:
    """桌面的真实位置。

    不能想当然地用 %USERPROFILE%\\Desktop：OneDrive 的"备份桌面"和组策略重定向都会把
    桌面挪到别处（C:\\Users\\<u>\\OneDrive\\Desktop），此时 %USERPROFILE%\\Desktop 要么
    不存在、要么是个没人看的空壳。SHGetKnownFolderPath(FOLDERID_Desktop) 是官方唯一
    能拿到真实位置的接口。拿不到（老系统、shell32 被动过）就退回 ~/Desktop——那是
    "不算更差"的兜底，不是正确答案。

    ⚠️ 待 Task 8 在装配机/真机上实测：开了 OneDrive 桌面备份的机器上这里返回什么。
    """
    if os.name == "nt":
        with contextlib.suppress(Exception):
            return _known_folder_desktop()
    return Path.home() / "Desktop"


def _known_folder_desktop() -> Path:  # pragma: no cover - 只在 Windows 上走这条路
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
    folderid = _GUID(
        0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(*bytes.fromhex("B0297FE99A87C641"))
    )
    out = ctypes.c_wchar_p()
    hresult = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(folderid), 0, None, ctypes.byref(out)
    )
    if hresult != 0:
        raise OSError(f"SHGetKnownFolderPath(FOLDERID_Desktop) 失败：0x{hresult & 0xFFFFFFFF:08X}")
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def render_config(tmpl_text: str, workspace_dir) -> str:
    """把 config.yaml.tmpl 渲染成 config.yaml 的正文。

    占位符出现在一个 YAML **双引号**标量里（`cwd: "{{WORKSPACE_DIR}}"`），所以替换进去
    的值必须按双引号标量的规则转义。Windows 路径全是反斜杠：`C:\\Users\\...` 原样塞进
    双引号里，`\\U` 会被 YAML 当成"8 位 Unicode 转义"，解析直接报错——Hermes 读不了
    config.yaml 就起不来，而「小助手修复」按钮渲染出来的正是这份坏配置：长辈越修越坏，
    一万公里外收不到任何信号。
    """
    escaped = str(workspace_dir).replace("\\", "\\\\").replace('"', '\\"')
    return tmpl_text.replace(WORKSPACE_PLACEHOLDER, escaped)


def assert_factory_complete(factory: Path) -> None:
    """出厂母版的形状校验。**必须在任何写入之前**跑（更新器甚至在切 current.txt 之前
    就跑，不合格的包整个拒掉——见模块 docstring 里的失败态分析）。

    校验的两类东西，坏起来的方式完全不同：
    - 缺文件 / 模板里没占位符：维护者的打包流程出了 bug。响亮地失败，让维护者看见。
      静默放过的话，渲染出来的 config.yaml 会把工作台指到别处，或者 SOUL.md 停在
      上一版——"发新版改 persona"这个能力静默失效，没人会发现。
    - 母版里混进了用户资产（.env / sessions / memories / logs）：维护者打包时手滑，
      把自己的激活码或者某台机器的聊天记录打进了发行版。这不只是密钥泄露——应用
      出厂状态时会**盖掉每一台机器**的激活码和聊天记录。
    """
    if not factory.is_dir():
        raise ValueError(f"出厂母版目录不存在：{factory}。未做任何修改。")

    leaked = next(factory.rglob(ENV_FILE), None)  # 不限文件/目录：叫 .env 的目录同样要拒
    if leaked is not None:
        raise ValueError(
            f"出厂母版里混入了 {ENV_FILE}（{leaked}）——绝不能把激活码打进发行版再分发"
            "出去，而且应用出厂状态时会盖掉每台机器的激活码。已拒绝执行，未做任何修改。"
        )
    for name in USER_OWNED:
        if (factory / name).exists():
            raise ValueError(
                f"出厂母版里混入了用户资产 {name}（{factory / name}）——它会盖掉每台机器"
                "上对应的用户数据。已拒绝执行，未做任何修改。"
            )

    if (factory / CONFIG_FILE).exists():
        # 打包手滑（比如把某台机器的 data/ 整个拷进了母版）。restore_factory_files 会先把
        # 模板渲染成 config.yaml，再把母版里这个 config.yaml **原样盖上去**——data/config.yaml
        # 于是留着字面量 cwd: "{{WORKSPACE_DIR}}"，Hermes 拿一个叫 {{WORKSPACE_DIR}} 的目录
        # 当工作台，而全程没有任何人报错。母版里该有的是 .tmpl，不是渲染好的成品。
        raise ValueError(
            f"出厂母版里混入了 {CONFIG_FILE}（{factory / CONFIG_FILE}）——它会盖掉刚渲染好的"
            f"配置，把工作台指向字面量占位符。母版里只该有 {FACTORY_CONFIG_TMPL}。未做任何修改。"
        )

    for required in (FACTORY_CONFIG_TMPL, FACTORY_SOUL):
        if not (factory / required).is_file():
            raise ValueError(f"出厂母版不完整：缺 {required}（{factory}）。未做任何修改。")
    if not (factory / FACTORY_SKILLS).is_dir():
        # 深度恢复（ADR-0005）要拿它当重铺母版；没有它，"恢复出厂"会把 skills/ 删光
        # 之后从一个不存在的源拷 0 个文件回来。
        raise ValueError(f"出厂母版不完整：缺 {FACTORY_SKILLS}/（{factory}）。未做任何修改。")

    # 钉住**带双引号的**那个形状，而不只是"占位符还在"：render_config 是按双引号 YAML 标量
    # 转义的（反斜杠翻倍）。把模板改成单引号或裸标量（改模板是常规发版动作），渲染出来的
    # cwd: 'C:\\Users\\ma\\Desktop\\小助手' 依然是**合法 YAML**——只是路径多了一倍反斜杠，
    # 指向一个不存在的文件夹。没有任何东西会报错：agent 把长辈的文件写进那个虚构的目录，
    # 「小助手修复」还会一模一样地再渲染一遍。
    quoted = f'"{WORKSPACE_PLACEHOLDER}"'
    tmpl = (factory / FACTORY_CONFIG_TMPL).read_text(encoding="utf-8")
    if quoted not in tmpl:
        raise ValueError(
            f"出厂模板 {FACTORY_CONFIG_TMPL} 里没有 {quoted}（必须是**双引号** YAML 标量，"
            f"当前写法：单引号/裸标量/占位符缺失）——渲染出来的 config.yaml 会把工作台指到"
            "一个不存在的目录，而且是合法 YAML，没有任何人会报错。未做任何修改。"
        )


def factory_state_is_current(install_root: Path, version: str) -> bool:
    """data/ 里的出厂状态是不是已经收敛到 version（读 data/.factory_version）。"""
    stamp = install_root / DATA_DIR_REL / FACTORY_STAMP
    if not stamp.is_file():
        return False
    return stamp.read_text(encoding="utf-8").strip() == version


def assert_skills_not_symlink(skills_dir: Path) -> None:
    """skills/ 必须是真实目录。是符号链接的话，往里写（覆盖出厂技能）会写到 hermes_home
    之外，深度恢复的 rmtree 更会跟着链接删到外面去。正常出厂形态里它一定是真实目录；
    这里显式拒绝，而不是靠"碰巧没人这么部署"的隐性安全。

    注意判断顺序：is_symlink() 必须先于 is_dir()——指向目录的符号链接 is_dir() 也是 True。
    """
    if skills_dir.is_symlink():
        raise ValueError(
            f"{skills_dir} 是符号链接，会把写入/删除导向 hermes_home 之外。"
            "请检查安装是否被篡改。未做任何修改。"
        )


def precheck_machine_state(hermes_home: Path, workspace_dir: Path) -> None:
    """这台机器**能不能**被应用出厂状态。调用方必须在切 current.txt **之前**跑它。

    为什么单拎出来（而不是留在 apply_factory_state 里当第一道闸门）：这里检查的两件事都是
    **机器状态**，不是瞬时 I/O 抖动——data/skills 是符号链接、桌面的工作台目录建不出来
    （路径上有个同名文件、权限不足、盘没挂载），它们每次运行都会以完全相同的方式失败。
    留在提交点之后的话，切版本会"成功"，出厂状态永远应用不上，而更新器每次开机都报
    「已是最新版本」——机器永久跑在"新代码 + 旧 persona"上。在提交点之前撞见它们，机器
    完全没变，响亮地失败，维护者还有得救。

    workspace_dir 的 mkdir 就是这里的检查本身（建不出来就抛）。提前建出来是安全的：它只是
    桌面上一个空文件夹，就算这次更新后面又中止了，下次启动 Hermes 照样要用它。
    """
    assert_skills_not_symlink(hermes_home / SKILLS_DIR)
    hermes_home.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)  # 长辈可能把桌面的「小助手」删了


def tmp_path(path: Path) -> Path:
    """原子写用的临时文件名：目标文件**同目录**、同名加 .tmp 后缀。

    同目录是硬要求——os.replace 只有在同一个文件系统内才是原子换名。这条规则只定义在
    这一处，因为它是"写的人"和"清的人"之间的契约：更新器的 _cleanup_stale_temp 按同一条
    规则去清上次崩溃留下的孤儿临时文件。两边各写各的、一旦漂移，孤儿就再也收不回来
    ——磁盘被垃圾压住 → _require_free_space 永久失败 → 这台机器从此收不到任何更新。
    """
    return path.with_name(path.name + ".tmp")


def atomic_write(path: Path, data: bytes) -> None:
    """写临时文件再 os.replace 换名：读到的要么是整份旧内容、要么是整份新内容，绝不会
    是截断了一半的 YAML（那会让 Hermes 起不来）。os.replace 在 POSIX 和 Windows 上都是
    目录项的原子替换。tmp 名字固定，不累积垃圾——中途崩了留下的那个 .tmp 会被下一次写入
    原样盖掉。

    收 bytes 而不是 str：出厂母版里迟早会出现非文本文件（技能里的图片/示例数据）。

    更新器写 current.txt / previous.txt 也走这里（见 updater._atomic_write）——"绝不留下
    半个文件"这条保证只能有一份实现，两份会静默分叉。
    """
    tmp = tmp_path(path)
    tmp.write_bytes(data)
    os.replace(tmp, path)


def factory_master(install_root: Path, version: str) -> Path:
    """某个版本的出厂母版目录：versions/<version>/factory/。

    只定义在这一处：更新器每次运行开头**校验**的那个母版（_reconcile_factory_state）
    必须和 apply_factory_state 真正**读取**的那个母版是同一个路径。两处各拼各的，就可能
    校验了 A、应用了 B——而这条路上的失败是静默的（戳照样落下去，出厂状态却来自别处）。
    """
    return install_root / VERSIONS_DIR / version / FACTORY_DIR_REL


def restore_factory_files(hermes_home: Path, factory: Path, workspace_dir: Path) -> list[str]:
    """把出厂母版里除 skills/ 之外的东西落到 data/：config.yaml.tmpl **渲染成**
    config.yaml，其余文件原样覆盖。

    为什么模板要渲染、而不是拷过去：Hermes 读的是 config.yaml，不是 config.yaml.tmpl。
    把模板原样拷进 data/ 等于"修复"了一个谁也不读的文件，而真正坏掉的 config.yaml
    一个字节都没动——CLI 还印着「小助手修复完成！」。data/ 里也不该留下 .tmpl：它没有
    任何用处，只会让下一个人以为那才是配置文件。

    skills/ 由调用方决定怎么处理（切版本时是"覆盖出厂技能"，深度恢复时是"整棵重铺"），
    因为这两种语义对习得技能的后果完全相反。
    """
    hermes_home.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)  # 长辈可能把桌面的「小助手」删了

    tmpl_path = factory / FACTORY_CONFIG_TMPL
    skills_root = factory / FACTORY_SKILLS

    log = []
    rendered = render_config(tmpl_path.read_text(encoding="utf-8"), workspace_dir)
    atomic_write(hermes_home / CONFIG_FILE, rendered.encode("utf-8"))
    log.append(f"已按出厂模板重新生成 {CONFIG_FILE}（工作台：{workspace_dir}）")

    # 其余出厂文件原样覆盖。跳过三类：目录（下面的 dest.parent.mkdir 会顺带建出来）、
    # 模板本身（已经渲染成 config.yaml，data/ 里不该再留一份没人读的 .tmpl）、以及
    # skills/ 下的一切（覆盖还是整棵重铺由调用方决定，两种语义对习得技能的后果相反）。
    for src in sorted(factory.rglob("*")):
        if src.is_dir() or src == tmpl_path or skills_root in src.parents:
            continue
        rel = src.relative_to(factory)
        dest = hermes_home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dest, src.read_bytes())
        log.append(f"已还原出厂文件：{rel.as_posix()}")
    return log


def overwrite_bundled_skills(hermes_home: Path, factory: Path) -> list[str]:
    """把出厂技能**覆盖**到 data/skills/：母版里有的盖掉，母版里没有的（习得技能）原样保留。

    不是 shutil.copytree(dirs_exist_ok=True)：那底下是 copy2，直接往目标文件上写。写到一半
    崩（断电、杀软锁文件、磁盘满）留下的就是**截断的半个 SKILL.md**——比"停在旧版本"糟得多，
    因为一个半截的技能文件看起来完全有效，agent 会照着它跑。逐个文件 tmp + os.replace 换名
    之后，每个技能要么整份新、要么整份旧。

    调用方必须已经跑过 assert_skills_not_symlink（见 precheck_machine_state）。
    """
    src_root = factory / FACTORY_SKILLS
    dest_root = hermes_home / SKILLS_DIR
    for src in sorted(src_root.rglob("*")):
        if src.is_dir():
            continue
        dest = dest_root / src.relative_to(src_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dest, src.read_bytes())
    return [f"已覆盖出厂技能：{SKILLS_DIR}/（习得技能原样保留）"]


def apply_factory_state(install_root: Path, version: str, workspace_dir: Path) -> list[str]:
    """把 versions/<version>/factory/ 的出厂状态应用到 data/。**每次切版本都要调用**
    （升级：更新器在切完 current.txt 之后调；回滚：Task 6 的启动器在切回旧版本之后调）。

    幂等：重复调用结果相同，所以任何一次失败都可以靠"再跑一次"收敛。但"总会有人重跑它"
    不能只是一句愿望——收敛靠的是最后落下的那个戳（data/.factory_version）：调用方每次运行
    先拿 current.txt 跟戳对一下（factory_state_is_current），对不上就重新应用。戳必须在
    **所有写入都成功之后**才落，否则一次半途失败会留下一个"已经收敛了"的假凭据，
    自愈逻辑再也不会重跑它。

    对 skills/ 是**覆盖**，不是重铺：出厂技能和习得技能混在同一棵树里（Hermes 没有独立的
    learned/ 子目录），清空再铺会把 agent 自我改进沉淀下来的资产一起删掉。要清空是
    深度恢复（--deep）的语义，不是版本切换的语义。
    """
    factory = factory_master(install_root, version)
    hermes_home = install_root / DATA_DIR_REL

    # 闸门全部先于任何写入。⚠️ 这两道**同样**要由调用方在切 current.txt 之前先跑一遍
    # （见模块 docstring 与 precheck_machine_state）：它们查的是"包合不合格""机器什么状态"，
    # 每次运行的结果都一样，撞在提交点之后就再也不会自己好起来。这里保留它们，是因为本
    # 函数还会被启动器/修复按钮直接调用，不能假设调用方一定先查过。
    assert_factory_complete(factory)
    assert_skills_not_symlink(hermes_home / SKILLS_DIR)

    log = restore_factory_files(hermes_home, factory, workspace_dir)
    log.extend(overwrite_bundled_skills(hermes_home, factory))

    # 全部写完了才落戳：它是"data/ 的出厂状态已经收敛到 version"的唯一凭据。
    atomic_write(hermes_home / FACTORY_STAMP, version.encode("utf-8"))
    return log
