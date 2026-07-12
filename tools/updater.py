# tools/updater.py
"""静默更新客户端：拉签名清单，校验，落 versions/<v>，切 current.txt。

运行环境决定了这份代码必须多防御：这不是终端里手动跑的脚本，而是 Windows
上“登录时触发”的计划任务，以非交互身份静默执行在爸妈的机器上，维护者在
一万公里外，失败时没有人会看 stderr。四条设计结论落地成下面这些代码：

1. 原子性——current.txt 是唯一的“生效开关”。新版本先在一个不被任何东西
   引用的 staging 目录里完整解压，成功之后才用目录级原子 rename 换到最终
   名字 versions/<version>；current.txt 本身也用“写临时文件 + os.replace”
   保证不会被写成一半。任何一步中途失败（断网/断电/解压出错），current.txt
   都还指向解压前的旧版本、旧版本目录完好——等价于“完全没升级”，绝不会
   出现“半新半旧”的启动失败态。previous.txt 必须先于 current.txt 落盘：
   这样即便在这两次写入之间崩溃，current.txt 没变，previous.txt 最坏也只是
   跟 current.txt 指向同一个（已经在磁盘上、完好的）版本，不会出现“回滚
   目标指向一个已被清理/根本不存在的版本”这种更糟的情况。

   还有一条同样致命、但只有在两个实例同时跑时才暴露的：安装根
   C:\\Users\\Public\\xiaozhushou 是**全用户共享**的，两个 Windows 账号各自的
   ONLOGON 计划任务会同时打到同一个根上（维护者远程手动跑更新时也一样）。
   不加锁的话，后来者的 _cleanup_stale_temp 会删掉前一个实例正在用的 staging
   目录，两个实例还抢同一个最终目录名，最后 current.txt 指向一个已经被删掉
   的版本 = 开机即挂。所以 apply_update 全程持有一把 OS 级排他锁（见 _try_lock）。

2. 磁盘不会无限膨胀——每个发行版整包 2.87GB。versions/ 下只保留 current
   和 previous 两份（成功切换后清理掉其余的），回滚要用 previous，绝不
   清理它。上一次崩溃/断电留下的半解压 staging 目录、半下载的 zip、没换名
   成功的 current.txt/previous.txt 临时文件，在下一次运行开始时无条件清掉
   ——它们从未被 current.txt/previous.txt 引用过，是安全孤儿。下载前还会先
   预检查剩余空间（峰值需求约 9.5GB），不够就静默放弃，不留半成品。

3. 内存不会跟着包体走——包体 895MB，整包 r.read() 读进内存的峰值是包体的
   两倍（约 1.8GB）。爸妈那台 4GB 的笔记本在开机时（杀软 + 启动项都驻留着）
   很可能直接 MemoryError，而一旦在这里 OOM，这台机器就再也收不到任何更新
   （包括安全更新）——更新通道本身就死了。所以下载是流式的：一边分块写盘
   一边喂 sha256，峰值内存是一个 chunk，与包体大小无关。

4. 网络不可达是静默任务的常态，不是异常——机器经常没网就开机。拉不到
   manifest/签名/包体这三步失败，一律静默返回 None，把“重试”留给下一次
   开机，绝不能让计划任务崩溃退出。注意 http.client.IncompleteRead（声明了
   Content-Length 却只发来一半就断开，也就是“下载中途断网”最典型的样子）
   **不是** OSError 的子类，必须显式一起捕获。但这条“对失败宽容”只覆盖
   “连不上”，不覆盖“内容不可信/不合法”：签名校验失败、sha256 不匹配、清单
   不是合法 JSON，都必须清晰地抛异常——宽容的是网络，不是内容。已拉黑的版本
   （bad_versions.txt，由 Task 6 启动器在版本启动即崩时写入）永远不再自动
   装，否则会陷入“装 → 崩 → 回滚 → 又装 → 又崩”的死循环，机器彻底废掉。

5. 更新不只换代码，还要把新版本的**出厂状态**应用到 data/（tools/factory_state.py，
   铁律三）。只换 versions/<v>/ 的话，新版本永远改不了 persona/配置——而 ADR-0003 里
   “发行版”的定义恰恰是“钉死的 Hermes + 出厂配置/persona/技能”。反过来，data/ 里
   属于用户的东西（.env、sessions、memories、习得技能）绝不能被更新碰到一个字节：
   旧布局把它们放在 versions/<v>/ 里，第一次自动更新就会连同旧版本目录一起剪掉，
   激活码没了 = 产品直接变砖。出厂母版在切版本**之前**校验（不合格的包整个拒掉），
   出厂状态在切版本**之后**应用（失败态分析见 factory_state 的模块 docstring）。
"""
import argparse
import base64
import errno
import hashlib
import http.client
import json
import os
import shutil
import urllib.request
import uuid
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from builder.paths import (
    BAD_VERSIONS_FILE,
    CHANNEL_FILE,
    CURRENT_FILE,
    DATA_DIR_REL,
    FACTORY_DIR_REL,
    LOCK_FILE,
    PREVIOUS_FILE,
    VERSIONS_DIR,
)
from tools.factory_state import (
    apply_factory_state,
    assert_factory_complete,
    atomic_write,
    default_workspace_dir,
    factory_master,
    factory_state_is_current,
    precheck_machine_state,
    tmp_path,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_DOWNLOAD_TMP_NAME = "download.tmp.zip"
_CHUNK_SIZE = 1024 * 1024

# staging 目录名必须**尽量短**。Win10/11 家庭版默认没开 LongPathsEnabled，MAX_PATH=260；
# 最终布局实测 68 字符（安全），但解压是在 versions/<staging>/ 底下做的，而 node_modules
# 嵌套很深——staging 名字每多一个字符，树里最长的那条路径就多一个字符。
# `.staging-<version>-<pid>-<uuid8>` 比最终名字长 24 个字符，`.s-<uuid8>` 只长 6 个。
#
# 长到咬人时会发生什么：extractall 抛的 OSError **不在** `except _UNREACHABLE` 的覆盖
# 范围里（那个 except 只包着 _download），异常一路逃到 main() → SystemExit(1)。于是
# 每次开机重下 895MB、解压失败、退出 1，永远装不上——把爸妈的带宽烧光，而维护者收不到
# 任何信号。
#
# 版本号和 pid 从名字里拿掉不影响互斥：互斥是 _try_lock 那把 OS 级排他锁提供的，随机
# 后缀只是"锁万一在某个诡异文件系统上失灵"时的最后一点防撞。
_STAGING_PREFIX = ".s-"

# 清理时要认的**全部**历史 staging 前缀。上一版更新器用的是 `.staging-<version>-<pid>-<uuid8>`，
# 而更新器自己是随版本下发的——所以磁盘上完全可能躺着一个由旧更新器留下的 `.staging-*` 孤儿
# （它崩在解压中途，然后这台机器更新到了带新前缀的这一版）。只 glob `.s-*` 够不着它，
# _prune_old_versions 又跳过点开头的名字：那 2.87GB 就**永远**收不回来，还会把
# _require_free_space 永久压在"空间不够"那一侧——这台机器从此再也收不到任何更新。
# 下一个发行版之后可以把 `.staging-` 拿掉（那时磁盘上不可能再有旧前缀的孤儿）。
_STALE_STAGING_PREFIXES = (_STAGING_PREFIX, ".staging-")

# 解压后约是压缩包的 3.2 倍（895MB → 2.87GB），加上压缩包自身 1 份，再留点余量。
_SPACE_FACTOR = 4.5

# “通道够不到”的全部形态。urllib.error.URLError（DNS/连接失败/超时/HTTP 错误）
# 是 OSError 的子类，但 http.client.IncompleteRead（声明了 N 字节只发来一半）
# 不是——它是 HTTPException。漏掉后者，一次普通的中途断网就会让计划任务以非零
# 码崩溃退出，而没有人会看那条 stderr。
_UNREACHABLE = (OSError, http.client.HTTPException)


class _NotEnoughSpace(OSError):
    """磁盘剩余空间撑不住这次更新（下载前预检查，不是写到一半才发现）。

    继承 OSError 是为了让它走和“通道够不到”同一条路：静默返回 None，下次开机
    再试。磁盘满是持续性本地故障，每次开机抛一条没人看的 traceback 毫无意义。
    """


def parse_version(v: str) -> tuple[int, ...]:
    """把 "0.10.0" 解析成 (0, 10, 0) 用于数值比较。字符串比较会把 "0.10.0"
    判定为小于 "0.9.9"（逐字符比较，'1' < '9'），是错的。"""
    return tuple(int(x) for x in v.strip().split("."))


def _fetch(url: str) -> bytes:
    """只用来拉小文件（manifest / 签名）。包体走 _download，绝不能整包进内存。"""
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def _content_length(response) -> int | None:
    raw = response.headers.get("Content-Length", "")
    return int(raw) if raw.strip().isdigit() else None


def _require_free_space(where: Path, package_size: int) -> None:
    """下载前先看磁盘够不够：压缩包 1 份 + 解压后约 3.2 份。峰值大约是
    previous(2.87) + current(2.87) + staging(2.87) + zip(0.9) ≈ 9.5GB。不够就抛
    _NotEnoughSpace 静默放弃——总比解压到一半炸出 ENOSPC、还白占几个 GB 的半成品
    staging 目录强（那会每次开机重演一遍）。

    这道预检查是有条件的：只有响应带了 Content-Length（调用方能提前知道包体大小）
    才会跑；分块/HTTP-1.0 那种没有 Content-Length 的响应会跳过它，此时空间不够
    会在写盘写到一半时才炸出 OSError，走的是同一条"静默返回 None、状态不变"的
    路径，不会导致半成品残留或崩溃退出，只是少了"提前放弃、不留半成品"这一层。"""
    need = int(package_size * _SPACE_FACTOR)
    free = shutil.disk_usage(where).free
    if free < need:
        raise _NotEnoughSpace(
            errno.ENOSPC, f"磁盘空间不足：这次更新需要约 {need} 字节，只剩 {free} 字节"
        )


def _download(url: str, dest: Path) -> str:
    """流式下载到 dest，一边写盘一边喂 sha256，返回 hexdigest。

    绝不能 r.read() 把 895MB 整包读进内存（峰值 ~1.8GB，4GB 的机器在开机时会
    OOM，而 OOM 的机器再也收不到任何更新）。分块之后峰值内存是一个 chunk。
    """
    h = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(url, timeout=60) as r:
        declared = _content_length(r)
        if declared is not None:
            _require_free_space(dest.parent, declared)
        with dest.open("wb") as f:
            while chunk := r.read(_CHUNK_SIZE):
                h.update(chunk)
                f.write(chunk)
                written += len(chunk)
    if declared is not None and written != declared:
        # r.read(n) 分块读的时候，连接中途断掉不会自己抛 IncompleteRead——它只是
        # 提前返回 b""（见 CPython http/client.py 里那句 "Ideally, we would raise
        # IncompleteRead ... but it might break compatibility"），安静地留下一个
        # 截断的文件。必须自己比对 Content-Length：否则“下载中途断网”会伪装成
        # “sha256 不匹配”（= 包被篡改）抛出去，把一次正常的网络抖动升级成计划
        # 任务崩溃退出。
        raise http.client.IncompleteRead(b"", declared - written)
    return h.hexdigest()


def verify_manifest(manifest_bytes: bytes, sig_b64: bytes, pubkey_hex: str) -> dict:
    """验签是整个更新通道的信任根：只有维护者的私钥能产出合法签名，任何人
    往 OSS 桶里塞包都不能让这里通过。验证失败抛 InvalidSignature，调用方
    绝不能吞掉这个异常——这是唯一挡住"任何人都能往通道里塞包"的闸门。

    注意签的是**清单正文字节**：拿到 OSS 写权限但没有私钥的攻击者，最自然的
    动作是保留维护者那份合法签名、只替换正文（抬高 version、把 package/sha256
    指向自己的包）——正文一改，原签名就对不上，这里就会拦下来。"""
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
    pub.verify(base64.b64decode(sig_b64), manifest_bytes)
    return json.loads(manifest_bytes)


def _manifest_fields(manifest: dict) -> tuple[str, str, str]:
    """从（签名已验证的）清单里取出三个字段。内容是维护者签过名的，所以字段缺失
    或版本号不是数字（"0.1.1-rc1"）只可能是维护者自己的发版流程有 bug——但也不该
    以一条裸的 KeyError / int() ValueError traceback 的形式出现在没人看的 stderr
    里，包一层读得懂的错误。"""
    try:
        version, package, sha256 = manifest["version"], manifest["package"], manifest["sha256"]
        parse_version(version)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"清单内容非法（签名有效，但字段缺失或格式不对）：{exc!r}") from exc
    return version, package, sha256


def _try_lock(install_root: Path):
    """在安装根上取一把 OS 级排他锁；拿不到就返回 None（另一个实例正在更新）。

    为什么必须有锁：INSTALL_ROOT 是全用户共享的（C:\\Users\\Public\\xiaozhushou，
    故意的——venv 里烧死的绝对路径不能带用户名）。两个 Windows 账号各自的 ONLOGON
    计划任务会同时打到同一个根上；维护者远程 ToDesk 手动跑更新时，计划任务可能正好
    在下载。MultipleInstancesPolicy 只能在“同一个用户的任务注册”内部串行，管不了
    这种情况。解压 2.87GB 要几十秒，重叠窗口很宽。

    为什么是文件句柄上的锁、而不是 O_EXCL 标记文件：标记文件在断电后会永久残留，
    它自己就变成一块砖（此后每次开机都以为“有别人在跑”，永远不再更新）。OS 级锁
    在进程消失时（正常退出/崩溃/断电重启）由内核自动释放，不可能留下这种残留。

    不在这里创建 install_root：一台真实装机的机器上安装根本来就存在（installer 建的），
    这里再顺手 mkdir(parents=True) 只会把调用方传错路径（typo）的问题掩盖掉——凭空
    在磁盘上长出一棵谁都没打算要的空目录树，而不是在调用处就报错。

    但打不开锁文件（根不存在、盘没挂载、无权限）也只能返回 None，不能抛：本模块是
    开机时静默跑的计划任务，任何异常都会变成 SystemExit(1)、每次开机崩一遍。没有安装
    根 = 没有东西可更新，安静退出即可。
    """
    try:
        fh = (install_root / LOCK_FILE).open("a+b")  # 不截断：锁文件长期存在，内容无关紧要
    except OSError:
        return None
    try:
        fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _unlock(fh) -> None:
    try:
        fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()  # 关句柄本身也会释放锁，所以即使上面失败，锁也不会泄漏


def _current(install_root: Path) -> str | None:
    """当前版本号；current.txt 不存在（或为空）时返回 None——**不返回 "0.0.0"
    这种占位值**。占位值会一路流进 previous.txt（伪造出一个磁盘上根本不存在的
    回滚目标）和 _prune_old_versions 的 keep 集合（"0.0.0" 保护不了磁盘上任何
    东西，磁盘上唯一那个能启动的旧版本反而被当成垃圾剪掉）——一次运行同时毁掉
    安全网和回滚目标。"""
    f = install_root / CURRENT_FILE
    text = f.read_text(encoding="utf-8").strip() if f.exists() else ""
    return text or None


def _bad_versions(install_root: Path) -> set[str]:
    f = install_root / BAD_VERSIONS_FILE
    return set(f.read_text(encoding="utf-8").split()) if f.exists() else set()


def _atomic_write(path: Path, text: str) -> None:
    """current.txt / previous.txt 的写入口（都是纯文本）。

    落盘机制不在这里实现：写 .tmp + os.replace 换名（"要么整份旧、要么整份新，绝不留下
    截断的半个文件"）与出厂状态的写入共用 factory_state.atomic_write 那一份实现。两份
    实现会静默分叉，而临时文件的命名规则同时还是 _cleanup_stale_temp 的清理依据
    （factory_state.tmp_path）——写的和清的一旦对不上，孤儿临时文件就永远收不回来。"""
    atomic_write(path, text.encode("utf-8"))


def _cleanup_stale_temp(install_root: Path) -> None:
    """清掉上一次运行中途崩溃/断电留下的临时产物：没解压完的 staging 目录、
    没写完的下载 zip、没换名成功的 current.txt/previous.txt 临时文件。这些
    从未被 current.txt/previous.txt 引用过，是安全孤儿，可以无条件清理
    ——否则每崩溃一次就白占 2.87GB 磁盘，永远收不回来。

    **只能在持有排他锁的前提下调用**：锁保证此刻没有别的实例在跑，磁盘上残留的
    staging 目录才一定是“死人的遗物”而不是“活人正在用的”。没有锁的话，这个函数
    会删掉另一个实例正在解压的 staging 目录，把它推向 FileNotFoundError，最后
    current.txt 指向一块虚空。锁文件本身（update.lock）不在清理范围内。"""
    (install_root / _DOWNLOAD_TMP_NAME).unlink(missing_ok=True)
    for name in (CURRENT_FILE, PREVIOUS_FILE):
        tmp_path(install_root / name).unlink(missing_ok=True)
    versions_dir = install_root / VERSIONS_DIR
    if versions_dir.is_dir():
        for prefix in _STALE_STAGING_PREFIXES:
            for stale in versions_dir.glob(f"{prefix}*"):
                shutil.rmtree(stale, ignore_errors=True)


def _prune_old_versions(install_root: Path, keep: set[str]) -> None:
    """versions/ 下只留 current 和 previous 两份（keep），其余的删掉。每个
    版本 2.87GB，不清理的话连续更新几次磁盘就会被堆满。previous 必须由
    调用方放进 keep——这个函数本身不做任何"猜哪个能删"的判断，只认调用方
    给的 keep 集合。清理失败（比如文件被占用）不影响本次更新已经成功这个
    事实，忽略即可，下次运行再试。"""
    versions_dir = install_root / VERSIONS_DIR
    if not versions_dir.is_dir():
        return
    for entry in versions_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in keep:
            continue
        shutil.rmtree(entry, ignore_errors=True)


def _drop_lying_previous(install_root: Path) -> None:
    """previous.txt 要么指向一个磁盘上真实存在的版本目录，要么干脆不存在。
    指向一个不存在的目录是最坏的一种状态：启动器（Task 6）会以为“有回滚目标”，
    回滚过去发现是空的，机器彻底救不回来。没有 previous.txt 是诚实的“没有回滚
    目标”，启动器至少知道自己没有退路。"""
    f = install_root / PREVIOUS_FILE
    if not f.exists():
        return
    previous = f.read_text(encoding="utf-8").strip()
    # 空字符串要单独挡掉：Path("versions") / "" 还是 versions/ 本身，is_dir() 为真，
    # 会把一个内容为空的 previous.txt 当成“有效回滚目标”放过去。
    if not previous or not (install_root / VERSIONS_DIR / previous).is_dir():
        f.unlink(missing_ok=True)


def apply_update(
    install_root: Path, channel_url: str, pubkey_hex: str, workspace_dir: Path
) -> str | None:
    """检查通道，必要时静默安装新版本。返回新版本号，或 None（已是最新 /
    通道当前不可达 / 磁盘空间不够 / 新版本已被拉黑 / 另一个实例正在更新）。

    workspace_dir（桌面的「小助手」文件夹）要**显式传进来**，本模块不自己去推导：
    推导（default_workspace_dir）只发生在 main() 那一层。这样测试里不会有哪个用例
    在开发机/CI 的真实桌面上凭空建出一个「小助手」文件夹来。
    """
    lock = _try_lock(install_root)
    if lock is None:
        # 另一个实例正在更新同一个共享安装根。这不是错误：安静让路，那个实例
        # 会把活干完；真要有什么没干完，下次开机再跑一次就是了。
        return None
    try:
        return _apply_update_locked(
            install_root, channel_url.rstrip("/"), pubkey_hex, workspace_dir
        )
    finally:
        _unlock(lock)


def _reconcile_factory_state(install_root: Path, workspace_dir: Path) -> None:
    """自愈：current.txt 指着的那个版本，它的出厂状态**真的**落到 data/ 上了吗？

    出厂状态是在切完 current.txt 之后才应用的（顺序反过来更危险，见 factory_state 的模块
    docstring），也就是说它落在提交点之后。它失败一次（杀软锁了某个文件、磁盘瞬时满），
    机器就停在"新代码 + 旧 persona + 旧配置"上——而这个中间态**不会自己好起来**：下一次
    开机，parse_version(新) <= parse_version(current) → return None → 印「已是最新版本」→
    退出 0。心跳还报着新版本号。维护者为"别再瞎编价格"发的那版 SOUL.md 修复，永远不会落地，
    而且他收不到任何信号。这正是本项目最坏的那类故障：一个报告成功的静默空操作。

    所以每次运行（更新器每次登录都跑，而且此刻正持着锁）先拿 current.txt 跟 data/ 里的戳
    对一下，对不上就把出厂状态重新应用一遍——**必须在"已是最新、无需更新"那个提前返回之前**，
    也必须在任何网络调用之前（通道够不到是常态，收敛不能等一个可能永远不来的新版本）。

    Task 6 的启动器也该做同样的校对（纵深防御），但更新器不能依赖它：启动器还不存在，
    而这个故障今天就已经能发生了。

    本函数必须**结构上不可能向外抛**。收敛只有一种机制：戳没落，下次开机重跑。所以吞掉
    一次失败的代价只是"多停一个开机周期的旧 persona"；而让它抛出去的代价是 SystemExit(1)
    挂在**每次**开机上——更新器是这台机器唯一的远程修复通道（见 builder/paths.py），抛
    出去等于亲手拆掉它：连能救这台机器的下一个发行版都装不上。这两种代价完全不对称，
    所以下面的 try 必须盖住这条路上的**每一次读写**（漏掉任何一次，那一次就是逃逸路径）、
    **同时**接住两类异常：

    - factory_state_is_current 读 data/.factory_version 这个戳：见 try 里的注释——戳是
      data/ 里的文件，被杀软锁住就读不出来（PermissionError），被写坏就解不出 UTF-8
      （UnicodeDecodeError，ValueError 的子类）。
    - assert_factory_complete 校验当前版本的母版：不存在/不完整时抛 ValueError（母版
      从来没带、或者装好之后整个被删/被换了内容）；但杀软"隔离"惯常的做法是**锁住**
      文件而不是删除它——锁住的文件对 is_dir()/is_file() 照样健在，撞的是后面
      read_text() 那一下，抛的是 PermissionError（OSError 的子类），不是 ValueError。
      磁盘读错误同理是 OSError。只接 ValueError 的话，这两种"母版其实还在、只是读不出来"
      的情况会一路逃出这个函数。
    - apply_factory_state 真正把母版落到 data/：即便母版本身完全合格，这台机器也可能
      应用不上——data/ 被杀软的目录保护锁成只读（中国杀软套装常见行为）会在写文件时抛
      PermissionError；data/skills 是符号链接会被它内部的 assert_skills_not_symlink 抛
      ValueError。这两种失败和"母版不合格"是同一个范畴——机器状态问题，不是内容不可信
      ——必须和上面那次调用共享同一条"吞掉、下次重试"的收敛路径，而不是被排除在 try 之外、
      绕开这份宽容、原样逃到 main()。

    注意这不是"静默的空操作"：机器没有在假装自己已经是新出厂状态（戳还停在旧版本，
    下一次更新会把它补上），而且这条中文提示在维护者远程 ToDesk 手动跑更新时看得见。
    """
    version = _current(install_root)
    if version is None:
        return  # 还没装过任何版本，没有"该收敛到哪儿"可言

    try:
        # 读戳本身也必须在 try 里面。戳是 data/ 里的文件，而 data/ 正是最容易被杀软和长辈
        # 碰到的那棵树：被"隔离"锁住的 .factory_version 对 is_file() 照样健在，撞的是
        # read_text() 那一下（PermissionError）；被写坏成非 UTF-8 则抛 UnicodeDecodeError
        # （ValueError 的子类）。把这一句留在 try 之外，等于给这个函数留了唯一一条逃逸路径
        # ——而逃出去的代价就是本函数存在的全部理由：SystemExit(1) 挂在每次开机上，连能救
        # 这台机器的下一个发行版都装不上。读不出戳 = 无法证明已收敛，按"没收敛"处理即可。
        if factory_state_is_current(install_root, version):
            return  # 戳对得上：data/ 已经是这个版本的出厂状态（绝大多数开机走这条快路）

        # 必须和下面 apply_factory_state 真正读取的是同一个母版路径（factory_master 只定义
        # 在一处）：校验了 A、应用了 B 的话，戳照样会落下去，而出厂状态来自别处。
        assert_factory_complete(factory_master(install_root, version))
        apply_factory_state(install_root, version, workspace_dir)
    except (ValueError, OSError) as exc:
        # 戳读不出来、母版不可用（缺失、被弄坏、被杀软锁住读不出来），或者母版本身没问题
        # 但这台机器套用不上（data/ 只读、data/skills 是符号链接）——都收敛不到，但**绝不能**
        # 因此把更新通道也一起堵死：下一个发行版带的母版会在切版本前被校验、切完之后被
        # 应用——那才是这台机器的出路。在这里抛异常 = 每次开机 SystemExit(1)、永远装不上
        # 新版本 = 亲手拆掉唯一的救援通道。
        print(f"小助手：当前版本的出厂状态自愈失败（{exc}）——跳过，继续检查更新")


def _stage_package(
    install_root: Path, channel_url: str, package: str, want_sha256: str
) -> Path | None:
    """下载 → 校验 sha256 → 解压进一个 staging 目录，返回它的路径。

    这里就是"宽容网络、绝不宽容内容"那条分界线（模块 docstring 第 4 条）：
    - 通道够不到（断网 / 超时 / 中途断流 / 磁盘空间不够）→ 返回 None，静默把重试留给
      下一次开机。此时 current.txt 一个字节都没被碰过，机器状态完全没变。
    - 包对不上 sha256（被篡改或下载损坏）→ 抛。绝不能把一个不可信的包解压进 versions/。

    解压失败（比如 MAX_PATH 咬人）留下的半棵 staging 树不在这里清：它从未被 current.txt
    引用过，是安全孤儿，由下一次运行的 _cleanup_stale_temp 无条件收走。
    """
    tmp_zip = install_root / _DOWNLOAD_TMP_NAME
    try:
        try:
            got_sha256 = _download(f"{channel_url}/{package}", tmp_zip)
        except _UNREACHABLE:
            return None

        if got_sha256 != want_sha256:
            raise ValueError(f"sha256 mismatch：包被篡改或下载损坏（{package}）")

        # staging 目录名带随机串：即便锁机制在某个诡异的文件系统上失灵，两个实例
        # 也不会撞到同一个 staging 目录名上、把对方的树解压进自己的树。名字为什么
        # 必须短（而不是把版本号和 pid 也拼进来）见 _STAGING_PREFIX 的注释。
        # （versions/ 到这一步才建：包没通过校验之前，安装根上不留任何痕迹。）
        staging = install_root / VERSIONS_DIR / f"{_STAGING_PREFIX}{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True)
        with zipfile.ZipFile(tmp_zip) as z:
            z.extractall(staging)
        return staging
    finally:
        tmp_zip.unlink(missing_ok=True)


def _apply_update_locked(
    install_root: Path, channel_url: str, pubkey_hex: str, workspace_dir: Path
) -> str | None:
    _cleanup_stale_temp(install_root)  # 锁在手，磁盘上的临时产物一定是孤儿
    _reconcile_factory_state(install_root, workspace_dir)  # 先自愈，再谈更新

    try:
        manifest_bytes = _fetch(f"{channel_url}/manifest.json")
        sig = _fetch(f"{channel_url}/manifest.sig")
    except _UNREACHABLE:
        # 通道当前不可达（离线开机是常态，不是异常）：静默放弃，下次开机再试。
        return None

    manifest = verify_manifest(manifest_bytes, sig, pubkey_hex)  # 签名不对，直接向上抛
    new_version, package, want_sha256 = _manifest_fields(manifest)

    current_version = _current(install_root)
    if parse_version(new_version) <= parse_version(current_version or "0.0.0"):
        return None  # 已是最新，不重复下载 895MB
    if new_version in _bad_versions(install_root):
        return None  # 拉黑过的版本不再自动装，否则会陷入装→崩→回滚的死循环

    staging = _stage_package(install_root, channel_url, package, want_sha256)
    if staging is None:
        return None  # 通道够不到：current.txt 没被碰过，机器状态完全没变，下次开机再试

    versions_dir = install_root / VERSIONS_DIR
    try:
        # 两道闸门都必须在**切版本之前**跑，理由是同一个：它们查的东西每次运行的结果都一样
        # ——不是"重跑一次可能就好"的瞬时 I/O 抖动。撞在提交点（current.txt）之后，机器就会
        # 永久停在"新代码 + 旧出厂状态"上，而更新器此后每次开机都报「已是最新版本」、退出 0。
        #
        # 1) 包里的出厂母版不合格（缺 SOUL.md / 模板没占位符 / 混进了 .env）：维护者的打包
        #    流程有 bug。这是"内容不可信/不合法"，不是"网络连不上"，响亮地抛（机器停在旧
        #    版本是对的）。
        # 2) 这台机器应用不了出厂状态（data/skills 是符号链接、桌面的工作台目录建不出来）：
        #    机器状态问题，同样每次都会以相同方式失败。
        assert_factory_complete(staging / FACTORY_DIR_REL)
        precheck_machine_state(install_root / DATA_DIR_REL, workspace_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)  # 不留几个 GB 的 staging 垃圾
        raise

    final = versions_dir / new_version
    if final.exists():
        if final.name == _current(install_root):
            # 绝不 rmtree 一个 current.txt 正指着的目录：那一瞬间 current.txt 就
            # 指向了虚空，机器开机即挂、一万公里外救不回来。真走到这里说明已经
            # 有人把这个版本装好并切过去了（锁本该挡住，这是最后一道保险），我们
            # 无事可做。
            shutil.rmtree(staging, ignore_errors=True)
            return None
        shutil.rmtree(final)  # 上次半途而废、换了名却没切 current 的残留，覆盖掉
    os.replace(staging, final)  # 目录级原子改名：换名之前，这个版本目录根本不存在

    if current_version and (versions_dir / current_version).is_dir():
        # 顺序不能反：previous 先落盘，current 才是真正的"生效开关"。中途崩溃
        # 的话，最坏结果是 previous == current（都还是旧版本），而不是"current
        # 已经换新、previous 却指向一个可能已被清理的版本"这种更糟的情况。
        _atomic_write(install_root / PREVIOUS_FILE, current_version)
        _atomic_write(install_root / CURRENT_FILE, new_version)
        _prune_old_versions(install_root, keep={new_version, current_version})
    else:
        # current.txt 缺失/为空，或它指的版本目录根本不在磁盘上——没有可信的回滚
        # 目标。这时**拒绝猜**：既不写 previous.txt（写一个占位版本号等于伪造一个
        # 不存在的回滚目标，比没有回滚目标危险得多），也不剪 versions/（keep 集合
        # 里那个不存在的版本号保护不了任何东西，磁盘上唯一那个能启动的旧版本会被
        # 当成垃圾剪掉——那恰恰是最后的安全网）。宁可多占一份磁盘。
        _drop_lying_previous(install_root)
        _atomic_write(install_root / CURRENT_FILE, new_version)

    # 切版本已经落定（current.txt 是唯一的生效开关），现在把**新版本的出厂状态**应用到
    # data/：config.yaml 重新渲染、SOUL.md 覆盖、出厂技能覆盖；.env / sessions /
    # memories / 习得技能一个字节都不碰。没有这一步，新版本就只能改代码、永远改不了
    # persona，“发行版”这个概念本身不成立（ADR-0003）。
    #
    # 顺序为什么是“切完再应用”、以及这里失败的话机器停在什么状态、怎么收敛，见
    # tools/factory_state.py 的模块 docstring。结论：机器跑的是新代码 + 旧的、仍然合法的
    # 出厂状态（能启动，只是 persona 陈旧），而 data/.factory_version 这个戳还停在旧版本
    # ——所以**下一次运行开头的 _reconcile_factory_state 会把它补上**。收敛不依赖 Task 6
    # 的启动器（那东西还不存在），也不依赖任何人来看 stderr。
    apply_factory_state(install_root, new_version, workspace_dir)
    return new_version


def main() -> None:
    parser = argparse.ArgumentParser(description="小助手更新器")
    parser.add_argument("install_root", type=Path, help="安装根（C:\\Users\\Public\\xiaozhushou）")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="桌面的「小助手」工作台目录（默认按当前登录用户的桌面推导）",
    )
    args = parser.parse_args()

    root = args.install_root
    # 推导只在这里发生（见 apply_update 的 docstring）。⚠️ 桌面是**按 Windows 用户**算的，
    # 所以“登录时更新”的计划任务必须以最终用户身份运行，不能是 SYSTEM。
    workspace = args.workspace or default_workspace_dir()
    try:
        channel = json.loads((root / CHANNEL_FILE).read_text(encoding="utf-8"))
        got = apply_update(root, channel["url"], channel["pubkey"], workspace)
    except Exception as exc:  # 静默计划任务：不抛裸 traceback 到没人看的 stderr
        print(f"小助手更新失败：{exc}")
        raise SystemExit(1) from exc
    print(f"已更新到 {got}" if got else "已是最新版本，无需更新")


if __name__ == "__main__":
    main()
