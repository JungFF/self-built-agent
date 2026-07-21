# builder/release.py
"""把装配机上的快照打成一个**发行版**：签了名、可以直接扔进 OSS 通道的整体包（ADR-0003）。

    uv run python -m builder.release --snapshot C:\\snapshot --skills C:\\snapshot\\skills \\
        --version 0.1.0 --out C:\\dist --key secrets\\channel.key \\
        --heartbeat secrets\\heartbeat.json --bucket <通道桶名>

===============================================================================
包是什么形状
===============================================================================

zip 解压后**就是** versions/<版本号>/ 的**内容**（没有外层包裹目录——多一层，更新器
os.replace 之后路径就全错了）。布局的唯一真相来源是 builder/paths.py：

    dist-0.1.0.zip  =  hermes-agent\\ + python\\ + ms-playwright\\ + factory\\ + tools\\
                       + builder\\paths.py + heartbeat.json

===============================================================================
为什么这个模块几乎全是守卫
===============================================================================

**一个坏包上了通道，是这个项目里唯一能同时毁掉所有机器的操作。** 通道是单向的：包一旦
上去，每台机器都会在下一次开机时静默地把它装上，而维护者在一万公里外，收不到任何信号。
所以打包器宁可在装配机上当场炸掉——那里有维护者盯着，改一行命令就好了。

每一道守卫挡的都是一个**具体的、已经想清楚的灾难**：

1. 包里没有 factory/（或它形状不对）→ 每台机器：下 895MB → 解压 2.87GB →
   assert_factory_complete 拒收 → 退出 1 → **每次开机重演一遍，永远**。所以打包前跑的
   是消费方**同一个函数**，不是"照着契约再写一遍检查"（那正是契约与实现静默脱钩的方式）。

2. 包里没有 tools/（或它 import 不起来）→ 更新器自己没了。更新器是**唯一的远程修复
   通道**：它死了，连那个本来能救这台机器的下一个发行版都装不上。所以这里真的**跑一遍**
   `import tools.updater`（在 payload 根下、清掉 PYTHONPATH），而不是数一数文件在不在。

3. 包里混进 .env → 维护者的激活码流向每一台机器；更糟的是 apply_factory_state 会拿它
   盖掉每台机器**自己的**激活码。

4. 包里混进 channel.key → **这是唯一无法回收的灾难**。公钥装机时就烧进了 channel.json，
   机器上没有任何东西能换掉它；私钥泄漏之后，任何人都能签一个包让每台机器静默执行任意
   代码，**永远**。包就挂在公共读的 OSS 上，谁都能下。所以按名字扫一遍，再按内容扫一遍。

5. 版本号更新器解析不了（"0.1.0-rc1"）→ 验签通过、_manifest_fields 抛 ValueError →
   **每台机器每次开机 SystemExit(1)**。所以版本号要过消费方的 parse_version。

最后，签完之后还要用消费方的 verify_manifest 把自己签的清单验一遍——以机器的视角。
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

from builder import keys
from builder.factory import render_factory
from builder.keys import public_key_hex, sign
from builder.paths import (
    AGENT_DIR_REL,
    BASE_PYTHON_REL,
    BUILDER_DIR_REL,
    CHANNEL_PREFIX,
    DESKTOP_APP_REL,
    ELECTRON_EXE_REL,
    ENV_FILE,
    FACTORY_CONFIG_TMPL,
    FACTORY_DIR_REL,
    HEARTBEAT_CRED_FIELDS,
    HEARTBEAT_CRED_FILE,
    HEARTBEAT_PREFIX,
    INSTALL_ROOT,
    PLAYWRIGHT_DIR_REL,
    TOOLS_DIR_REL,
    VENV_PYTHON_REL,
)
from tools.factory_state import assert_factory_complete
from tools.updater import parse_version, verify_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 通道里的三个对象名。**这三样是通道前缀底下允许出现的全部东西**（桶是公共读的）。
MANIFEST_NAME = "manifest.json"
SIG_NAME = "manifest.sig"

# 快照必须带来的东西（相对版本目录）。少一样，机器一装上就坏：
#   - electron.exe 没了 → 长辈双击 → 启动器 spawn 失败 → 判"这个版本跑不了" → 两次观察
#     之后回滚 + **永久拉黑**这个版本（bad_versions.txt 只增不减）。
#   - venv 里的 python.exe 没了 → 更新器/启动器连解释器都没有。
_SNAPSHOT_REQUIRED = (
    AGENT_DIR_REL,
    VENV_PYTHON_REL,
    BASE_PYTHON_REL,
    ELECTRON_EXE_REL,
    DESKTOP_APP_REL,
    PLAYWRIGHT_DIR_REL,
)
# 打包器自己往包里放的东西：工具层（随版本走，更新器才能更新它自己）、路径契约（tools/ 里
# 每个模块都 import 它）、出厂母版（铁律三：出厂状态属于版本）。
REQUIRED_IN_PAYLOAD = (*_SNAPSHOT_REQUIRED, TOOLS_DIR_REL, BUILDER_DIR_REL, FACTORY_DIR_REL)

# tools/ 里**必须**进包的模块。少一个不会在打包时报错（它只是不在包里），但会在机器上
# 变成"双击图标没反应"（launcher.py）、"修复按钮没反应"（recover.py）、"再也收不到更新"
# （updater.py）——三种都是静默的。
_ESSENTIAL_TOOLS = ("updater.py", "launcher.py", "recover.py", "factory_state.py", "__init__.py")
# builder/ 里进包的**只有**路径契约。keys.py（签名）和 release.py（打包）跟机器无关，
# 而且 keys.py 出现在包里只会诱导下一个人把 secrets/ 一起拷进来。
_SHIPPED_BUILDER = ("__init__.py", "paths.py")

# 机器上真的 import 一遍这三个入口——三者合起来把 tools/ 和 builder/paths.py 的完整性
# 全覆盖了（它们互相 import）。
_IMPORT_PROBE = "import tools.updater, tools.launcher, tools.recover"

_CHUNK = 1024 * 1024


def rel_path(rel: str) -> Path:
    """把契约里那些 Windows 风味的相对路径常量（``hermes-agent\\venv\\...``）变成 Path。

    直接 `payload / VENV_PYTHON_REL` 在 POSIX 上会得到**一个名字里带反斜杠的文件**，
    而不是一棵目录树——打包器在 Mac 上跑（测试、或者维护者本机验一遍）时，"必备文件在不在"
    这道检查就会全部误判。反斜杠在这里是契约的一部分，不是分隔符的口味问题。
    """
    return Path(*rel.split("\\"))


def package_name(version: str) -> str:
    """包名带版本号：新包**绝不覆盖**旧包。

    覆盖的话，正在下载旧包的机器会在半路上拿到新字节 → sha256 对不上 → 抛"包被篡改"。
    而且 previous 版本的机器万一要重下，通道里也得留着那份。"""
    return f"dist-{version}.zip"


# =============================================================================
# 装配：快照 + 出厂母版 + 工具层 → payload（= versions/<版本号>/ 的内容）
# =============================================================================


def assemble_payload(snapshot_root: Path, skills_src: Path, dest: Path, version: str) -> Path:
    """把装配机上的快照装配成一棵完整的 payload，返回它。

    skills_src 是**必传的**：出厂技能母版只能来自装配机上装好的 Hermes（`skills/`），
    仓库里一个出厂技能都没有（见 builder/factory.render_factory）。

    version 是**这次装配的目标版本号**——不是快照自己以为的版本号。快照里的 venv 可能是
    从另一个版本借来的（见 _rewrite_venv_home），必须显式告诉装配器"这次装的是哪个版本"，
    不能信快照自带的任何路径。
    """
    snapshot_root, dest = Path(snapshot_root), Path(dest)
    _assert_has(snapshot_root, _SNAPSHOT_REQUIRED, "快照")
    if dest.exists() and any(dest.iterdir()):
        # 脏目录 = 上一次装配的残留（或者随手扔进去的任何东西）会**静默地**混进这个发行版。
        raise ValueError(
            f"装配目标 {dest} 不是空的——上一次装配的残留会静默地混进这个发行版。"
            f"先删掉它（rm -rf {dest}）再重来。未做任何修改。"
        )

    shutil.copytree(snapshot_root, dest, symlinks=True, dirs_exist_ok=True)
    agent_dir = dest / rel_path(AGENT_DIR_REL)
    _clean_git_worktree(agent_dir)
    _rewrite_venv_home(agent_dir, version)
    render_factory(dest, skills_src)  # 出厂母版（它自己会跑一遍消费方的闸门）

    tools_dst = dest / TOOLS_DIR_REL
    tools_dst.mkdir(parents=True, exist_ok=True)
    for src in sorted((REPO_ROOT / TOOLS_DIR_REL).glob("*.py")):
        shutil.copy2(src, tools_dst / src.name)
    missing = [n for n in _ESSENTIAL_TOOLS if not (tools_dst / n).is_file()]
    if missing:
        raise ValueError(f"工具层不完整：缺 {'、'.join(missing)}——机器上没有它们就没有修复通道。")

    builder_dst = dest / BUILDER_DIR_REL
    builder_dst.mkdir(parents=True, exist_ok=True)
    for name in _SHIPPED_BUILDER:
        shutil.copy2(REPO_ROOT / BUILDER_DIR_REL / name, builder_dst / name)
    return dest


def _clean_git_worktree(agent_dir: Path) -> None:
    """真机 2026-07-20 复现过的故障：装配机上 `git checkout` 之后，hermes-agent 仓库里一批
    文本文件在 git 眼里"被改动"——内容跟 HEAD 逐字节等价（`git diff --ignore-all-space` 验证
    过是空的，换行符是头号嫌疑），但 git 不知道。这份脏工作区如果原样打进包，机器首次启动时
    Hermes 自己的 bootstrap 会跑 `git checkout <钉死的 commit>`——git 见工作区有本地修改，为了
    不丢数据直接 Aborting（退出码 1），桌面端弹"Hermes couldn't start"。跟能不能连网无关：装配机
    上就地复现过。装配时把它强制清成跟 HEAD 一致，机器上那次 checkout 才能保证拿到干净仓库。

    `git checkout -f -- .` 只还原**已跟踪文件**的改动——不清除未跟踪文件（`git clean`
    的地盘），也不递归子模块。对这里要治的病（一个已经在钉死 commit 上的仓库，被跟踪文件
    的换行符漂移搞脏）这个范围是够的：未跟踪文件不会触发"local changes would be
    overwritten by checkout"这个 abort。hermes-agent 目前没有子模块，如果将来有，这里
    需要重新评估。
    """
    if not (agent_dir / ".git").exists():
        return  # 不是 git 仓库（测试快照、或者 Hermes 未来换分发方式）——无需清理
    result = subprocess.run(  # noqa: S603 - 参数是常量，解释器是当前这个
        ["git", "checkout", "-f", "--", "."],
        cwd=agent_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"清理 {agent_dir} 的 git 工作区失败（exit {result.returncode}）："
            f"{result.stderr.strip()}——这份脏工作区如果原样打包，机器首次启动时 Hermes 自己的 "
            "bootstrap 跑 git checkout 会当场 Aborting。未产出任何东西。"
        )


def _rewrite_venv_home(agent_dir: Path, version: str) -> None:
    """真机 2026-07-21 复现过的故障：`hermes-agent\\venv\\pyvenv.cfg` 里的 `home` 是**创建
    这个 venv 时**烧下的绝对路径（比如 `...\\versions\\0.1.0\\python-base`）。快照如果是从
    另一个版本借来的（省得重新走一遍 Windows Native 安装），这个路径就跟这次真正要装配的
    版本号对不上；全新机器上根本没有那个旧版本目录，Python 解释器启动时找不到 stdlib，
    直接 `ModuleNotFoundError: No module named 'encodings'`——比 Hermes 自己的 bootstrap
    还早，装机流程在 `launcher --init` 那一步就以错误码 1 崩溃，装出一个只有 `.env` 的空壳。

    不能信快照自带的那份路径是对的，必须显式重写成**这次装配的目标版本号**对应的路径。
    """
    cfg_path = agent_dir / "venv" / "pyvenv.cfg"
    if not cfg_path.is_file():
        return  # 没有真的 venv（测试快照，或者 Hermes 未来换 Python 打包方式）——无需改
    base_python_dir = BASE_PYTHON_REL.rsplit("\\", 1)[0]  # "python-base"
    new_home = f"home = {INSTALL_ROOT}\\versions\\{version}\\{base_python_dir}"
    lines = cfg_path.read_text(encoding="utf-8").splitlines()

    def is_home_line(line: str) -> bool:
        return line.split("=", 1)[0].strip() == "home"

    # 显式判断有没有 home 行——不能用"重写后内容有没有变"来推断：同版本装配（home 本来就
    # 已经指对了目标版本）时内容一字不差，那不是"没有 home 行"，而是"已经对了"，照样得放行。
    if not any(is_home_line(ln) for ln in lines):
        raise ValueError(
            f"{cfg_path} 里没有 home = 这一行——不是一个正常的 venv pyvenv.cfg，装出来的 "
            "Python 解释器大概率起不来。未产出任何东西。"
        )
    rewritten = [new_home if is_home_line(ln) else ln for ln in lines]
    cfg_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


# =============================================================================
# 打包 + 签名
# =============================================================================


def build_release(
    payload_root: Path,
    version: str,
    out_dir: Path,
    priv_hex: str,
    *,
    heartbeat: dict | None,
    expect_pubkey: str | None = None,
) -> dict:
    """把 payload 打成通道发行物：dist-<版本>.zip + manifest.sig + manifest.json。

    heartbeat 是**关键字参数、没有默认值**：带不带心跳（ADR-0004）是维护者每次发版都要
    做的决定，不能靠"忘了传"来默默决定。显式传 None = 这个版本不带心跳。

    expect_pubkey（**强烈建议每次都给**）是期望的通道公钥 hex——装机时烧进 channel.json 的
    那把（Task 8 产物）。给了它，签名**之前**先核对"这把私钥推出来的公钥 == 它"，对不上就当场
    炸。这是唯一能在**打包时**抓住"签错了钥匙"的守卫：_verify_as_a_machine_would 用同一把私钥
    现推公钥来自验，对任何合法私钥都通过，钉不住"维护者把 --key 指到了一把已经作废/被换掉的
    旧密钥上"——那种包每台机器都验签失败、更新通道静默停摆，而打包器一路报成功。

    返回 {"zip", "manifest", "sig", "sha256", "version", "heartbeat"}。
    """
    payload_root, out_dir = Path(payload_root).resolve(), Path(out_dir).resolve()

    # ---- 闸门全部先于任何产出。一个坏包连产出都不许有 ----
    parse_version(version)  # 消费方的解析器：过不了这一关的版本号 = 每台机器每次开机退出 1
    keys.load_private_key(priv_hex)  # 私钥现在就解一遍：解不开的话，压完 2.87GB 再炸毫无意义
    _assert_signing_key_is_expected(priv_hex, expect_pubkey)  # 签错钥匙 = 整个机队静默停止更新
    if out_dir.is_relative_to(payload_root):
        raise ValueError(f"产物目录 {out_dir} 在 payload（{payload_root}）里面——zip 会把自己打进自己。")
    _assert_has(payload_root, REQUIRED_IN_PAYLOAD, "payload")
    _assert_no_symlinks(payload_root)
    assert_factory_complete(payload_root / FACTORY_DIR_REL)  # 消费方的那道门，同一个函数
    _assert_no_secrets(payload_root, priv_hex)
    _assert_tools_importable(payload_root)
    cred = _heartbeat_bytes(heartbeat, priv_hex)

    # ---- 产出。清单**最后**落盘 ----
    # "out_dir 里有清单 ⟺ 一次完整且已自验的构建"这条只在**版本号变化**时成立。**同版本重构建**
    # （合法的发版前工作流：build → 检查 → 修 → 重构建 → 发布一次）会破坏它：下面 _write_zip 的
    # os.replace 原子**覆盖**掉上一份同名 dist-<v>.zip，而事后任一道闸门失败（最典型：--payload
    # 带着陈旧 heartbeat.json、这版又 --no-heartbeat，撞 _verify 的心跳次数校验）都发生在覆盖
    # 之后、清单重写之前——留下"新 zip + 旧清单"这个 sha 对不上、却仍合法签名的三件套。两道防线：
    #   (1) 下面 try 里任一道闸门失败，就删掉刚覆盖出来的 zip（那个版本从此没有包，而不是一个跟
    #       存活清单对不上的包）；
    #   (2) publish_commands 发布前重新 hash 一遍包、跟清单 sha256 对一遍（机器自己那道校验，
    #       字节进公共桶之前先做）。
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / package_name(version)
    _write_zip(zip_path, payload_root, cred)  # 成功返回 = 新 zip 已原子覆盖同名旧包
    try:
        sha256 = _sha256(zip_path)  # 磁盘上那个 zip 的哈希，不是"我们打算写的那个"

        manifest_bytes = json.dumps(
            {"version": version, "package": zip_path.name, "sha256": sha256}, ensure_ascii=False
        ).encode("utf-8")
        sig = sign(manifest_bytes, priv_hex)
        _verify_as_a_machine_would(zip_path, manifest_bytes, sig, priv_hex, cred)

        (out_dir / SIG_NAME).write_bytes(sig)
        (out_dir / MANIFEST_NAME).write_bytes(manifest_bytes)
    except BaseException:
        # 刚写的 zip 已经原子覆盖了同名旧包。任一道事后闸门失败（含 Ctrl-C），都不能把这个被
        # 覆盖的 zip 跟上一次留下的、仍然合法签名的 manifest/sig 凑成 sha 对不上的三件套——那会
        # 让每台机器下完 895MB 后校验失败、每次开机 SystemExit(1)、永远装不上。删掉它。
        zip_path.unlink(missing_ok=True)
        raise
    return {
        "zip": str(zip_path),
        "manifest": str(out_dir / MANIFEST_NAME),
        "sig": str(out_dir / SIG_NAME),
        "sha256": sha256,
        "version": version,
        "heartbeat": cred is not None,
    }


def _assert_signing_key_is_expected(priv_hex: str, expect_pubkey: str | None) -> None:
    """签名**之前**核对：这把私钥推出来的公钥，就是维护者期望的那把（烧进机器 channel.json 的）。

    不给 expect_pubkey 就跳过（可选）——但**强烈建议每次都给**：这是唯一能在打包时抓住"签错了
    钥匙"的守卫。维护者手里可能有好几把（当前的、上一代的、备份的），把 --key 指到一把**已经
    作废/被换掉**的旧密钥上时，打包器和它的自验（_verify_as_a_machine_would，用同一把私钥现推
    公钥）都会happily放行，而每台机器（channel.json 里烧的是新公钥）都验签失败 → SystemExit(1)
    → 更新通道停摆，直到用对的钥匙重新发一版。可恢复（小助手本身照跑当前版本，是停摆不是变砖），
    但维护者在一万公里外只看到"打包成功"，无从下手。
    """
    if expect_pubkey is None:
        return
    actual = public_key_hex(priv_hex)
    expected = expect_pubkey.strip().lower()
    if actual != expected:
        raise ValueError(
            f"--key 推出来的公钥（{actual}）跟 --expect-pubkey 期望的（{expected}）对不上——"
            "多半是 --key 指到了一把**已经作废/被换掉**的旧密钥上。用它签出来的清单，每台机器"
            "（channel.json 里烧的是期望的那把公钥）都会验签失败 → SystemExit(1) → 更新通道停摆，"
            "直到用对的钥匙重新发一版。未产出任何东西。"
        )


def _assert_has(root: Path, required: tuple[str, ...], what: str) -> None:
    for entry in required:
        path = root / rel_path(entry)
        if not path.exists():
            raise ValueError(
                f"{what} 不完整：缺 {entry}（{path}）——这台机器一装上就坏。未产出任何东西。"
            )


def _assert_no_symlinks(root: Path) -> None:
    """payload 里不许有符号链接：zip 会把链接**指向的内容**原样打进包（可能是维护者机器上
    的任何东西），而 os.walk 撞上一个环还会转不出来。正常的 Windows 快照里没有符号链接。"""
    for path in _entries(root):
        if path.is_symlink():
            raise ValueError(f"payload 里有符号链接：{path}——它指向的内容会被原样打进包。未产出任何东西。")


def _assert_no_secrets(root: Path, priv_hex: str) -> None:
    """payload 里绝不能有激活码（.env）和通道私钥（channel.key，或它的内容出现在任何文件里）。

    为什么按内容也要扫一遍：`secrets/` 被整个拷进 payload、payload 根就是仓库根、维护者
    把私钥贴进了一个备忘文件……名字对不上的漏法有很多，而私钥泄漏是**不可回收**的
    （公钥烧在每台机器的 channel.json 里，换不掉）。多读一遍 2.87GB 是几十秒的事，
    换的是"这件事绝不可能发生"。
    """
    for path in _entries(root):
        if path.name == ENV_FILE:  # 目录也算：叫 .env 的目录同样拒掉
            raise ValueError(
                f"payload 里混进了 {ENV_FILE}（{path}）——激活码会随包流向每一台机器，"
                "而且应用出厂状态时会盖掉每台机器自己的激活码。未产出任何东西。"
            )
        if path.name == keys.KEY_FILE:
            raise ValueError(
                f"payload 里混进了 {keys.KEY_FILE}（{path}）——通道私钥是整个更新通道的信任根，"
                "泄漏之后任何人都能签一个包让每台机器执行任意代码，而公钥烧在机器上换不掉。"
                "未产出任何东西。"
            )
    leaked = _find_bytes(root, _key_needles(priv_hex))
    if leaked is not None:
        raise ValueError(
            f"payload 里的 {leaked} 含有通道私钥的内容——私钥泄漏是这个项目里唯一无法回收的"
            "灾难（包就挂在公共读的 OSS 上，谁都能下）。未产出任何东西。"
        )


def _key_needles(priv_hex: str) -> tuple[bytes, ...]:
    """要在 payload 里搜的那几串字节。调用方**必须**先 load_private_key 验过这把钥匙：
    一把空钥匙会让针变成 b""（在每个文件里都"找得到"），而 _find_bytes 的重叠量会变成负数
    ——tail 于是每读一块就长一块，把整个文件读进内存。"""
    # 残余风险（已接受，不修）：只覆盖小写/大写 hex。同一把私钥若以 base64 或 0x 前缀的形式
    # 存在一个**非 channel.key** 名字的文件里，会躲过这遍内容扫描。可接受，因为在用的这把私钥
    # 保证是 hex（load_private_key 已验），而按名字那道扫描兜住了 channel.key——所以这是一条很窄
    # 的残留，不值得为它把针扩成一套编码矩阵。
    text = priv_hex.strip()
    return (text.lower().encode("ascii"), text.upper().encode("ascii"))


def _find_bytes(root: Path, needles: tuple[bytes, ...]) -> Path | None:
    """在整棵树里找这几串字节。分块读 + 重叠，所以跨 chunk 边界也躲不掉；峰值内存是一个块。"""
    overlap = max(len(n) for n in needles) - 1
    for path in _entries(root):
        if not path.is_file():
            continue
        tail = b""
        with path.open("rb") as f:
            while chunk := f.read(_CHUNK):
                buf = tail + chunk
                if any(n in buf for n in needles):
                    return path
                tail = buf[-overlap:]
    return None


def _assert_tools_importable(root: Path) -> None:
    """在 payload 根下真的 `import tools.updater`（以及启动器、修复器）一遍。

    数文件在不在是不够的：tools/ 里每个模块都 `from builder.paths import ...`，包里少了
    builder/paths.py，机器上每次开机就是 ModuleNotFoundError——而更新器是唯一能把这台机器
    救回来的东西。

    ⚠️ 这一跑同时钉住了一条**调用约定**：工作目录必须是版本目录（或者用 `-m`）。
    `python versions\\<v>\\tools\\updater.py` 会把 sys.path[0] 设成 tools\\，`builder` 和
    `tools` 两个包都 import 不到——稳定入口的 .cmd 必须先 cd 到版本目录（Task 8）。

    PYTHONPATH 必须清掉：维护者机器上要是恰好指着仓库，这道守卫就会拿仓库里的 builder/
    把包里缺的那份补上，然后放行一个装到机器上必然 ImportError 的包。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(  # noqa: S603 - 参数是常量，解释器是当前这个
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"包里的工具层 import 不起来（在 {root} 下跑 `{_IMPORT_PROBE}`）：\n"
            f"{proc.stderr.strip()}\n"
            "机器上的更新器是唯一的远程修复通道——它起不来，这台机器就再也收不到任何东西。"
            "未产出任何东西。"
        )


def _heartbeat_bytes(heartbeat: dict | None, priv_hex: str) -> bytes | None:
    """校验心跳凭证（ADR-0004），返回要注进包里的字节；None = 这个版本不带心跳。

    坏凭证不会吵——它只是让每一次 PUT 静静地 403，而维护者以为自己有可见性。这比"没有
    心跳"更坏：`ossutil ls` 一片空白到底是"所有机器都掉队了"还是"凭证是坏的"？
    """
    if heartbeat is None:
        return None
    if not isinstance(heartbeat, dict):
        raise ValueError(f"心跳凭证必须是一个 JSON 对象，拿到的是 {type(heartbeat).__name__}。")

    unknown = set(heartbeat) - set(HEARTBEAT_CRED_FIELDS)
    missing = set(HEARTBEAT_CRED_FIELDS) - set(heartbeat)
    if unknown or missing:
        raise ValueError(
            f"心跳凭证的字段不对（多了 {sorted(unknown)}，少了 {sorted(missing)}）——"
            f"更新器按 {HEARTBEAT_CRED_FIELDS} 读它，读不到就静默地不发心跳。未产出任何东西。"
        )
    for field, value in heartbeat.items():
        if not isinstance(value, str) or not value.strip() or "<" in value or ">" in value:
            raise ValueError(
                f"心跳凭证的 {field} 是空的或还是个占位符（{value!r}）——每一次 PUT 都会 403，"
                "而维护者会以为自己有可见性。未产出任何东西。"
            )
    if heartbeat["prefix"] != HEARTBEAT_PREFIX:
        raise ValueError(
            f"心跳凭证的 prefix 是 {heartbeat['prefix']!r}，而 RAM 策略只允许写 "
            f"{HEARTBEAT_PREFIX}/（见 heartbeat_ram_policy）——每一次 PUT 都会 403。未产出任何东西。"
        )

    cred = json.dumps(heartbeat, ensure_ascii=False, indent=2).encode("utf-8")
    # 凭证是**注进 zip 里**的，绕开了对 payload 那遍私钥扫描——所以这里必须单独再拦一次。
    if any(n in cred for n in _key_needles(priv_hex)):
        raise ValueError("心跳凭证里出现了通道私钥的内容——那把私钥绝不能随包发出去。未产出任何东西。")
    return cred


def _entries(root: Path) -> Iterator[Path]:
    """payload 里的每一个目录和文件（不跟随符号链接：跟随会转进环里出不来）。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        here = Path(dirpath)
        for name in dirnames + filenames:
            yield here / name


def _archive_members(root: Path) -> Iterator[tuple[Path, str]]:
    """(磁盘路径, 包里的名字)。名字一律相对 payload 根、用正斜杠——解压出来**就是**
    versions/<版本号>/ 的内容，没有外层包裹目录。空目录也带上（快照里有几个是空的）。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        here = Path(dirpath)
        if here != root and not dirnames and not filenames:
            yield here, here.relative_to(root).as_posix() + "/"
        for name in filenames:
            path = here / name
            yield path, path.relative_to(root).as_posix()


def _write_zip(zip_path: Path, payload_root: Path, cred: bytes | None) -> None:
    """先写 .part，再原子换名——一个半成品的 zip 绝不会以最终名字留在产物目录里
    （publish_commands 会照着清单去找它，而清单是最后才落盘的）。"""
    # ⚠️ 这道闸门必须先于**任何一个字节的压缩**。payload 根目录如果本来就带着一份同名的陈旧
    # 文件（比如 --payload 传的是上一个版本解出来的那份 payload，它按构造已经带着上一版本的
    # heartbeat.json），下面那个循环会把它当普通文件原样抄进 zip——再注一份新鲜的进去就撞成
    # 两个同名成员。zipfile 对这种事只发一句 UserWarning、照样写，不会拒绝：CPython 的
    # extractall 恰好是后写的赢，侥幸装上新鲜那份；换一个"先到先得"的 zip 读取器，装上的就是
    # 陈旧凭证，机器往后每一次心跳 PUT 都会静默 403。绝不能靠"哪个实现赢了"这种运气，必须
    # 当场拒绝——不能等 zipfile 先把警告吼出来、或者等 _verify_as_a_machine_would 秋后算账。
    #
    # 为什么拒在这里、而不是循环里那句 writestr 之前（那也算"写这份字节之前"）：压 2.87GB 要
    # 好几分钟。晚一步拒绝，维护者白等一趟，产物目录里还躺着一个 895MB 的 .part 孤儿。闸门
    # 全部先于任何产出——这也是这道守卫**唯一**能跟 _verify_as_a_machine_would 那道事后守卫
    # 区分开的地方（两道都抛 ValueError、消息里都有"陈旧"），tests/test_release.py 里那条
    # "产物目录里连一个 .part 都不许留下"的断言正是靠它才成为这道守卫独有的红灯。
    if cred is not None and (payload_root / HEARTBEAT_CRED_FILE).exists():
        raise ValueError(
            f"payload 根目录已经带着一份 {HEARTBEAT_CRED_FILE}——多半是上一次装配、或者 "
            "--payload 传的是上一个版本解出来的那份陈旧残留。这次又显式给了一份新鲜的 "
            "--heartbeat：两份重名成员写进同一个 zip，装哪一份就要看 zip 读取器的运气。"
            "先删掉 payload 里那份陈旧凭证（或者传一份干净的 payload）再重来。"
            "未产出任何东西。"
        )

    part = zip_path.with_name(zip_path.name + ".part")
    part.unlink(missing_ok=True)
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for path, arcname in _archive_members(payload_root):
            z.write(path, arcname)
        if cred is not None:
            # 落在版本目录的根上（versions/<v>/heartbeat.json）——**不在 factory/ 里**：
            # 在 factory/ 里的话，restore_factory_files 会把它拷进用户的 data/。
            z.writestr(HEARTBEAT_CRED_FILE, cred)
    os.replace(part, zip_path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _verify_as_a_machine_would(
    zip_path: Path, manifest_bytes: bytes, sig: bytes, priv_hex: str, cred: bytes | None
) -> None:
    """在清单落盘**之前**，以消费方的视角把它验一遍。

    这里只查**真的会分叉**的东西。自证式断言一条都不写：它们恒为真，看着像安全网，其实
    什么都没钉住。两条**别再加回来**（都验过：删掉它们，一条测试都不会红）：
      - "清单里的 sha256 等于我们刚算的那个 sha256"；
      - "清单里的 version 解析得动"——它就是入口那道 parse_version 已经验过的**同一个
        字符串**，只是原样 json 往返了一趟。真正钉住入口那道闸门的，是"坏版本号连一个
        zip 都不许产出"（tests/test_release.py::test_refuses_a_version_the_updater_cannot_parse）。

    - verify_manifest：更新器用的**同一个函数**、公钥从**这把私钥现推**。它挡的只有一件事：
      keys.py 内部 sign() 和 public_key_hex() 对不上了（签名根本不是这把私钥推出的公钥能验的）
      ——这也正是它那条 monkeypatch 测试唯一证明的东西。它**挡不住**"维护者手里拿错了钥匙"：
      公钥是从同一把 priv_hex 现推的，换任何一把有效私钥这里都照样通过。真正能在打包时抓住
      "签错了钥匙"（--key 指到一把已作废/被换掉的旧密钥）的，只有 build_release 的 expect_pubkey
      / CLI 的 --expect-pubkey（把私钥推出的公钥跟维护者期望的那把比一遍，见
      _assert_signing_key_is_expected）；不给它，一次拿错钥匙的签名会一路放行，而每台机器
      （channel.json 烧的是旧公钥）验签失败 → SystemExit(1) → 更新通道停摆，直到重新发一版。
    - 打开 zip 数成员：`zipfile` 读不出中央目录 = 我们写了个半截的包（磁盘满、写盘出错），
      而 sha256 会**忠实地**描述那半截包，签名也完全合法——机器于是下完 895MB、校验通过、
      解压炸掉。它还挡住外层包裹目录（`payload/hermes-agent\\...`）：那样解压出来的
      versions/<v>/ 里什么都没有，机器每次开机装一遍、每次都被出厂母版闸门拒掉。
    """
    verify_manifest(manifest_bytes, sig, public_key_hex(priv_hex))  # 验不过就抛 InvalidSignature

    with zipfile.ZipFile(zip_path) as z:  # 半截的包在这里就打不开（BadZipFile）
        names = z.namelist()
    name_set = set(names)
    required = {
        f"{FACTORY_DIR_REL}/{FACTORY_CONFIG_TMPL}",
        f"{TOOLS_DIR_REL}/updater.py",
        f"{BUILDER_DIR_REL}/paths.py",
    }
    if missing := sorted(required - name_set):
        raise ValueError(
            f"包里少了机器一定会去找的东西：{missing}——解压出来的 versions/<版本号>/ 是错的"
            "（多了一层外层目录？），每台机器都会装一遍再拒掉。未发布任何东西。"
        )
    # ⚠️ 必须数**次数**，不能只问"在不在"：set 会把两个同名成员看成跟一个一模一样。
    # zip 允许同一个名字出现两次（比如 --payload 传的是上一个版本解出来的那份 payload，
    # 它按构造已经带着一份陈旧的 heartbeat.json，这次又显式给了一份新鲜的 --heartbeat）——
    # `zipfile` 只会对着这种包发一句 UserWarning，不会拒绝写。CPython 的 extractall 恰好是
    # 后写的赢，侥幸装上新鲜那份；换一个"先到先得"的 zip 读取器，装上的就是陈旧凭证：机器
    # 往后每一次心跳 PUT 都会静默 403，维护者却以为自己有可见性。绝不能靠"哪个实现赢了"
    # 这种运气，必须当场拒绝。
    count = names.count(HEARTBEAT_CRED_FILE)
    expected = 1 if cred is not None else 0
    if count != expected:
        raise ValueError(
            f"包里的 {HEARTBEAT_CRED_FILE} 出现了 {count} 次，这次发版的心跳决定要求 {expected} 次"
            "——payload 根目录很可能带着一份陈旧凭证（比如 --payload 传的是上一个版本解出来的"
            f"那份，它按构造已经带着上一版本的 {HEARTBEAT_CRED_FILE}），跟这次的心跳决定撞在了"
            "一起。未发布任何东西。"
        )


# =============================================================================
# 发布
# =============================================================================


def publish_commands(out_dir: Path, bucket: str) -> list[list[str]]:
    """发布这一次构建的命令，**必须按返回的顺序执行**。

    为什么不是一条 `ossutil cp -rf <产物目录> oss://<桶>/channel/`（计划原文里那条）：

    1. **它会把产物目录里所有东西传上一个公共读的桶。** 维护者只要有一次把 channel.key
       （或任何别的东西）放在了那个目录里，私钥就上了公网——而私钥泄漏不可回收（公钥烧在
       每台机器的 channel.json 里）。只传点名的三个文件，这一整类事故就不存在。
    2. **上传顺序就是发布的原子性。** manifest.json 是通道里唯一的"生效开关"（跟机器上的
       current.txt 一个角色）：它最后上去，通道就永远自洽——机器要么看见旧版本，要么看见
       一个**已经完整躺在桶里**的新版本。反过来（ossutil 默认是并发上传的，顺序不可控），
       清单先上、包还在传的那几分钟里，每台开机的机器都会拿着新清单去下一个 404 的包。

    ⚠️ 中断的发布必须**补完**：manifest.sig 已经换成新的、manifest.json 还是旧的（或反过来）
    的那段时间里，每台机器的更新器都会验签失败并以 1 退出（不影响小助手本身能不能用，但
    更新通道在那段时间里是停的）。两个小文件之间的窗口只有一瞬，但发布中断在那里就会一直停着。
    """
    out_dir = Path(out_dir).resolve()
    manifest_path, sig_path = out_dir / MANIFEST_NAME, out_dir / SIG_NAME
    if not manifest_path.is_file() or not sig_path.is_file():
        raise ValueError(
            f"{out_dir} 里没有一次完整的构建（缺 {MANIFEST_NAME} 或 {SIG_NAME}）——"
            "清单是最后才落盘的，没有它就说明上一次构建没走完。绝不发布半个通道。"
        )
    if (out_dir / keys.KEY_FILE).exists():
        raise ValueError(
            f"产物目录里有 {keys.KEY_FILE}——通道桶是**公共读**的，私钥传上去就是永久泄漏"
            "（公钥烧在每台机器上，换不掉）。把它挪走再发布。"
        )

    manifest = json.loads(manifest_path.read_bytes())
    package = out_dir / manifest["package"]
    if not package.is_file():
        raise ValueError(f"清单指着的包不在产物目录里：{manifest['package']}——发布出去就是每台机器 404。")
    # 发布前重新 hash 一遍磁盘上那个包，跟已签名清单里的 sha256 对一遍——这就是**机器自己会做的
    # 那道校验**，在字节进公共读桶之前先做一遍，绝不能信"磁盘上这三样天生自洽"。它们**不**天生
    # 自洽：一次同版本重构建会原子覆盖 dist-<v>.zip，若构建在覆盖之后、重写清单之前失败，清单/
    # 签名就还描述着**上一份**包的 sha。照着发出去 = 每台机器下完 895MB 后 sha256 不匹配 →
    # 每次开机 SystemExit(1)、永远装不上、895MB 反复白下、current.txt 永不前进。
    actual_sha = _sha256(package)
    if actual_sha != manifest.get("sha256"):
        raise ValueError(
            f"产物目录里的 {manifest['package']} 的 sha256（{actual_sha}）跟已签名清单里的"
            f"（{manifest.get('sha256')}）对不上——多半是一次同版本重构建覆盖了这个 zip、却没重写"
            "清单（清单/签名还描述着上一次那份）。发布出去就是每台机器下完 895MB 后 sha256 不匹配、"
            "每次开机 SystemExit(1)、永远装不上。未发布任何东西。"
        )
    _assert_heartbeat_is_not_in(package, bucket)

    dest = f"oss://{bucket}/{CHANNEL_PREFIX}/"
    return [["ossutil", "cp", "-f", str(path), dest] for path in (package, sig_path, manifest_path)]


def _assert_heartbeat_is_not_in(package: Path, bucket: str) -> None:
    """心跳桶必须**独立于**通道桶。

    通道桶是**公共读**的（更新器匿名 GET 清单和包）。心跳落在同一个桶里 = 任何人都能匿名
    列举、读取每一台机器的 ID、版本和时间戳；而且那把随包扩散出去的写凭证从此对通道桶有了
    写权限——离"策略写宽一格就能覆盖清单"只差一次手滑。心跳桶应该是一个**私有的、除了心跳
    什么都没有的**桶。
    """
    with zipfile.ZipFile(package) as z:
        if HEARTBEAT_CRED_FILE not in z.namelist():
            return
        cred = json.loads(z.read(HEARTBEAT_CRED_FILE))
    if cred.get("bucket") == bucket:
        raise ValueError(
            f"心跳凭证指着通道桶（{bucket}）——通道桶是公共读的，心跳落在里面等于把每台机器的"
            "ID、版本、时间戳公开挂到网上，而且那把会随包扩散出去的写凭证从此对通道桶有了写权限。"
            "心跳必须用一个独立的私有桶。未发布任何东西。"
        )


def heartbeat_ram_policy(bucket: str) -> dict:
    """心跳凭证的 RAM 策略（ADR-0004）——**必须当这份凭证已经泄漏了来写它**。

    凭证随包下发，而包就挂在公共读的 OSS 上：谁都能下载、解压、把它抠出来。它是一个静态的
    共享密钥，交给"扩散到维护者控制不了的机器上"的代码用——它提供不了任何**身份认证**，只
    提供"不被顺手撞见"。所以真正限住损失的只有这份策略：

        只 PutObject       → 不能读（别人的机器 ID / 版本）、不能列举（不能枚举所有机器）、
                             **不能删**（一条已经写上去的记录谁也抹不掉）
        只 heartbeat/ 前缀 → 够不到通道里的包和清单（何况那是另一个桶）

    由此推出读心跳时唯一站得住的两个结论：
      - "某台机器**没有**心跳" —— **可信**（没有 DeleteObject，抹不掉）。这恰恰是最要紧的
        那半：掉队的机器是靠"缺席"暴露的。
      - "某台机器说它一切正常" —— **不可信**（谁都能 PUT 一条覆盖上去）。

    残余风险（接受，并说明代价）：拿到凭证的人可以往 heartbeat/ 里灌垃圾对象（存储费用，
    用生命周期规则自动过期 + 账单告警兜住），也可以伪造/覆盖某台机器的心跳行来把一台掉队
    的机器"洗白"。**心跳是运维提示，不是安全控制，绝不能拿来给任何东西授权。**

    维护者一次性配好（RAM 用户 → 自定义权限策略 → 只授这一条 → 生成 AK/SK）：

        aliyun ram CreatePolicy --PolicyName xiaozhushou-heartbeat-put \\
            --PolicyDocument "$(python -c '...heartbeat_ram_policy(\"<心跳桶>\")...')"

    心跳桶本身：**私有**（绝不公共读）、关闭版本控制、加一条生命周期规则让 heartbeat/ 下的
    对象 N 天后自动过期。
    """
    return {
        "Version": "1",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["oss:PutObject"],
                "Resource": [f"acs:oss:*:*:{bucket}/{HEARTBEAT_PREFIX}/*"],
            }
        ],
    }


# =============================================================================
# CLI：维护者按的那一个键
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="小助手发行版打包器（打包 + 签名 + 打印发布命令）")
    parser.add_argument("--version", required=True, help="发行版版本号（纯数字 + 点，如 0.1.0）")
    parser.add_argument("--out", type=Path, required=True, help="产物目录：通道的三个文件落在这里")
    parser.add_argument("--key", type=Path, required=True, help="secrets/channel.key（绝不入库）")
    parser.add_argument(
        "--expect-pubkey",
        help="（**强烈建议每次都给**）期望的通道公钥 hex = 装机时烧进 channel.json 的那把"
        "（Task 8 产物，channel.pub 的内容）。给了它，打包器会在签名前核对 --key 推出来的公钥"
        "是否等于它——这是唯一能在打包时抓住『签错了钥匙』的守卫。不给：把 --key 指到一把已作废/"
        "被换掉的旧密钥上时，打包器照签不误，而每台机器验签失败、更新通道静默停摆。",
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path, help="装配机上的 Hermes 快照根")
    source.add_argument("--payload", type=Path, help="已经装配好的 payload（跳过装配）")
    parser.add_argument("--skills", type=Path, help="出厂技能母版（配合 --snapshot；仓库里没有）")
    parser.add_argument("--payload-dir", type=Path, help="装配到哪里（默认 <out>/../payload-<版本>）")

    beat = parser.add_mutually_exclusive_group(required=True)
    beat.add_argument("--heartbeat", type=Path, help="受限 OSS 凭证 JSON（ADR-0004）")
    beat.add_argument("--no-heartbeat", action="store_true", help="这个版本不带心跳（显式弃权）")

    parser.add_argument("--bucket", help="通道桶名；给了就打印发布命令")
    args = parser.parse_args()

    if args.snapshot and not args.skills:
        parser.error("--snapshot 必须配 --skills：出厂技能母版只能来自装配机上装好的 Hermes，仓库里没有")

    # 互斥且必填：--no-heartbeat 是**显式弃权**，不是"忘了传"（build_release 那个关键字参数
    # 没有默认值，正是为了让"忘了传"变成 TypeError，而不是一个静默不带心跳的包）。
    heartbeat = None
    if not args.no_heartbeat:
        heartbeat = json.loads(args.heartbeat.read_text(encoding="utf-8"))

    payload = args.payload
    if payload is None:
        payload = args.payload_dir or args.out.parent / f"payload-{args.version}"
        assemble_payload(args.snapshot, args.skills, payload, args.version)

    built = build_release(
        payload,
        args.version,
        args.out,
        args.key.read_text(encoding="utf-8"),
        heartbeat=heartbeat,
        expect_pubkey=args.expect_pubkey,
    )
    # 绝不打印私钥或凭证内容——这段输出会被贴进 issue、聊天窗、终端记录。
    print(f"payload：{payload}")
    print(f"包：    {built['zip']}")
    print(f"sha256：{built['sha256']}")
    print(f"心跳：  {'带（ADR-0004）' if built['heartbeat'] else '不带（显式弃权）'}")
    if args.bucket:
        print("\n按这个顺序发布（清单最后上：它是通道里唯一的生效开关）：")
        for cmd in publish_commands(args.out, args.bucket):
            print("  " + shlex.join(cmd))


if __name__ == "__main__":
    main()
