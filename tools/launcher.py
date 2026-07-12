# tools/launcher.py
"""桌面「小助手」图标背后的那个进程：启动 current 版本；新版本启动即崩就切回上一版。

这是产品的第一道门——爸妈双击图标时跑的就是它。它挂了，他们看到的就是"双击了没反应"，
然后给美国打越洋电话。它同时是自动更新通道的**安全网**：更新器在无人看管的情况下把新版本
切成 current，如果那个版本起不来，只有这里能把机器救回去。

六个判断题的结论（每一条都有一条"删掉它就变红"的测试）：

1. **"启动即崩"怎么定义？** 不能只认非零退出码——Electron 崩溃时未必返回非零码（渲染
   进程炸了、顶层 handler 吞掉异常后 app.quit()、配置读不动就自己退了，都可能是 0）。
   一个双击之后一闪就没了的窗口，对爸妈来说就是"坏了"。所以：
   - 在**启动窗口**（STARTUP_SECONDS）内退出 = 崩溃，**不管退出码是几**；
   - 启动窗口之后、健康窗口之内以 **0** 退出 = 用户自己把窗口关了（人要看见窗口、把鼠标
     移到叉号上点一下，怎么也得好几秒；而启动崩溃发生在窗口画出来之前）；
   - 健康窗口内以**非零**码退出 = 崩溃；
   - spawn 直接失败（electron.exe 没打进包里、被杀软隔离）= 崩溃——这个版本跑不了；
   - 活过健康窗口 = 成功：进程脱管继续跑，启动器退出。

2. **"崩了"不等于"该回滚"。** 这两件事必须分开判，因为两种误判的代价完全不对称：
   - 漏判一次真崩溃 → 爸妈双击没反应 → 打电话 → 维护者知道了，能救。**很吵。**
   - 误判一次正常使用 → 机器**静默地**退回旧版本，而且那个被拉黑的好版本**永远**装不
     回来了（更新器认 bad_versions.txt）。没有任何人会发现。
   所以回滚只在有证据时开火：**这个版本从没在这台机器上被证明健康过**（last_good.txt）。
   跑了几个月的版本今天崩了，那不是新版本引入的故障，回滚治不了它、只会毁掉一个好版本
   ——响亮地失败，把机器留给「小助手修复」和维护者。

   ⚠️ 由此推出一条**贯穿全模块的定向规则**：回滚 + 拉黑是**不可逆**的
   （bad_versions.txt 只增不减；更新器只装比 current 更新的版本；没有任何东西会主动告诉
   一万公里外的维护者）。所以**凡是读不出来的证据，只能往"不开火"的方向倒**——读不出
   last_good.txt 就当这个版本已被证明健康（解除武装），读不出 previous.txt 就当没有回滚
   目标，读不出 bad_versions.txt 就拒绝改写它。用一个被杀软锁住的文件去武装一个不可逆的
   动作，是这套设计能犯的最坏的错。

3. **开火前要旁证：一次"启动即崩"的观察不够。** "瞬间消失"这个观察本身**会误报**——单实例
   回弹、长辈手滑把窗口关掉、杀软扫到一半锁住某个 DLL，都和真崩溃长得一模一样。而真正坏掉
   的版本**每次双击都会崩**，假崩溃不会重演。所以要求 CORROBORATING_FAILURES 次**独立观察**
   （持久化在 startup_failures.txt）才回滚 + 拉黑。代价：长辈多按一下。收益：任何一次性的
   假崩溃都不可能毁掉一个好版本。（这台机器要是根本写不下记账文件，计数就永远凑不满 →
   永远不回滚——那正是安全方向。）

   与之配套的是**观察，而不是推断**：活过健康窗口的桌面端 PID 记在 desktop.pid 里；下一次
   双击如果撞见"瞬间以 0 退出"，先看那个 PID 还活着没——活着就**证明**小助手已经在跑了，
   这是单实例回弹，不是崩溃。

4. **回滚目标也起不来 = 证据指向启动命令，不是版本。** 两个版本用**同一条**命令、以**同一种**
   方式失败（尤其是在 argv[0] 上抛 OSError），说明错的是那条命令（default_exe_argv 至今没在
   真机上验证过）或者 data/ 坏了——而拉黑惩罚的恰恰是"版本"。此时必须**撤销拉黑、复原
   current.txt / previous.txt**：机器依然起不来（响亮地失败），但没有任何好版本被毁掉。
   **分不清"版本坏了"和"启动命令错了"的时候，就没有资格拉黑。**

5. **健康窗口里绝不允许第二个启动器插进来。** 启动器要阻塞几十秒等健康窗口，而长辈没立刻
   看见窗口就再双击一下是常态。Electron 的单实例锁会让第二个实例瞬间以 0 退出——那和"启动
   即崩"在退出码和存活时间上一模一样，而此刻第一个启动器还没来得及记下 last_good。第二个
   启动器于是把一个完全正常的新版本回滚掉 + 永久拉黑，每次自动更新之后都可能发生，而且
   完全静默。所以整个 run() 持一把 OS 级排他锁（launch.lock），拿不到就安静退出。

6. **没有回滚目标时不猜、也不装死。** previous.txt 可能不存在（更新器有意为之的语义：诚实
   地表示"无回滚目标"，好过一个看着有效的假目标）。此时绝不能从 versions/ 里挑一个"看起来
   最新的"顶上——那正是编造假回滚目标。响亮地失败：非零退出码 + 日志。

**维护者可见性**：回滚在**本机**留下三处痕迹（current.txt 变了、bad_versions.txt 多一行、
data/logs/launcher.log 有一条中文记录），维护者 ToDesk 进去一眼能看见。但**没有任何东西
会主动告诉一万公里外的维护者**——那要等 ADR-0004 的心跳（另一个 Task）：心跳那一行应该
带上 bad_versions.txt 的内容，否则"这台机器回滚过"这个信号会止步于一个没人读的文件。

⚠️ 本模块只依赖标准库（+ tools.factory_state，它同样只依赖标准库）。**绝不 import
tools.updater**：那会把 cryptography 拖到启动路径上，而 cryptography 一旦装坏（轮子跟
Python 版本对不上、DLL 被杀软隔离），启动器就起不来——而回滚恰恰是用来救这种机器的。
"""

import argparse
import contextlib
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from builder.paths import (
    BAD_VERSIONS_FILE,
    CURRENT_FILE,
    DATA_DIR_REL,
    DESKTOP_APP_REL,
    DESKTOP_PID_FILE,
    ELECTRON_EXE_REL,
    HERMES_HOME_ENV,
    LAST_GOOD_FILE,
    LAUNCH_LOCK_FILE,
    LAUNCHER_LOG,
    LOGS_DIR,
    PLAYWRIGHT_DIR_REL,
    PLAYWRIGHT_ENV,
    PREVIOUS_FILE,
    STARTUP_FAILURES_FILE,
    VERSIONS_DIR,
)
from tools.factory_state import (
    apply_factory_state,
    atomic_write,
    default_workspace_dir,
    factory_state_is_current,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# 活过这么久就算启动成功（进程脱管，桌面端继续跑）。
HEALTH_SECONDS = 30
# 这么快就消失 = 窗口根本没画出来 = 崩溃，不管退出码是几（见模块 docstring 第 1 条）。
# 它必须**明显短于**"人看见窗口、把鼠标移到叉号上点一下"所需的时间，否则一次正常的
# "打开看一眼就关掉"会被误判成崩溃。
#
# ⚠️ 待 Task 8 在装配机上实测——**这个 5 秒今天没有任何实测依据，不许信它**：它是从
# **进程 spawn** 那一刻算起的，因此**整个 Electron 冷启动都算在里面**。目标机器是一台装着
# 国产杀软套装、机械硬盘、要加载 895MB 包的老电脑，冷启动到画出窗口很可能是 3~10 秒——
# 也就是说一台**只是慢**的机器会被判成"启动即崩"。旁证机制（CORROBORATING_FAILURES）挡住了
# 由此回滚好版本的最坏后果，但长辈仍然会被告知"起不来"。真正的修法不是把这个数字调大（那是
# 换一个猜数），而是让桌面端在窗口画出来时写一个就绪标记（例如 data/logs/desktop.ready），
# 启动器轮询它——把"窗口画出来了"从**猜**变成**观察**。Task 8 实测冷启动时间之后再定。
STARTUP_SECONDS = 5

# 回滚 + 拉黑之前，同一个版本必须被**独立观察到**这么多次"启动即崩"（见模块 docstring 第 3 条）。
# 2 = 一次假崩溃（单实例回弹 / 手滑关窗口 / 杀软瞬时锁）绝不可能独自扣动扳机；而一个真坏的
# 版本每次双击都崩，长辈按第二下的时候安全网就开火了。
CORROBORATING_FAILURES = 2

# 一次启动的三种结局。
HEALTHY = "healthy"  # 活过健康窗口
CLOSED = "closed"  # 起来过，用户自己关掉的
CRASHED = "crashed"  # 启动即崩（或者根本 spawn 不起来）


class _Attempt(NamedTuple):
    """启动一次桌面端之后**观察到**的东西（_attempt 的返回值）。"""

    outcome: str  # HEALTHY / CLOSED / CRASHED
    code: int | None  # 退出码；活过健康窗口时进程还在跑，没有退出码
    pid: int | None  # 桌面端进程的 PID；连 spawn 都没成功时没有


def _version_dir(install_root: Path, version: str) -> Path:
    return install_root / VERSIONS_DIR / version


def default_exe_argv(install_root: Path, version: str) -> list[str]:
    """启动桌面端的命令行。全部从 builder/paths.py 的常量拼出来，不在这里重抄路径。

    ⚠️ **这个命令还没有在真机上验证过。** Task 2 的 spike 只确认了桌面端是 Electron
    （node_modules\\electron\\dist\\electron.exe 加载 apps\\desktop），但 ECS 上没有桌面
    会话，从没实际启动过 GUI。Task 8 会在装配机上实测确切命令并在这里改正。

    猜错的代价是有边界的，因为它不会静默：electron.exe 不存在 → Popen 抛
    FileNotFoundError → 按"这个版本跑不了"处理（回滚）；命令形式不对 → 进程立刻退出 →
    同样按崩溃处理，而且回滚过去的旧版本会用**同一条**错误命令再失败一次 → run() 返回
    非零 + 日志里写着"回滚之后仍然起不来"。那正是维护者需要的诊断：命令错了，不是版本坏了。
    """
    vdir = _version_dir(install_root, version)
    return [str(vdir / ELECTRON_EXE_REL), str(vdir / DESKTOP_APP_REL)]


def _child_env(install_root: Path, version: str) -> dict[str, str]:
    """桌面端进程的环境变量。

    不设 HERMES_HOME 的后果：Hermes 会**静默地**在 %LOCALAPPDATA%\\hermes 建一个全新的空
    home——没有激活码、没有技能、没有历史——**而且不报任何错**。整个 data/ 分离架构无声蒸发，
    长辈看到的是"我的东西都不见了"，维护者收不到任何信号。PLAYWRIGHT_BROWSERS_PATH 同理
    （spike 实测它是浏览器落点的唯一控制项），而且它是**按版本**的：回滚之后必须指向旧版本
    自己的 ms-playwright，不能还指着那个刚被拉黑的版本目录（下次更新它就被剪掉了）。
    """
    env = os.environ.copy()
    env[HERMES_HOME_ENV] = str(install_root / DATA_DIR_REL)
    env[PLAYWRIGHT_ENV] = str(_version_dir(install_root, version) / PLAYWRIGHT_DIR_REL)
    return env


def _spawn(argv: list[str], env: dict[str, str]):
    return subprocess.Popen(argv, env=env)


def _logger(install_root: Path):
    """往 data/logs/launcher.log 追加一行中文记录。

    为什么必须落盘：没有人会看这个进程的 stdout（它跑在 pythonw 下，连控制台都没有——
    见 main()）。"这台机器回滚过""这台机器起不来了"这两件事，如果只 print 出去，就等于
    没有发生过；维护者 ToDesk 进去时唯一能读的就是这个文件。

    写日志本身绝不能把启动弄挂：data/ 被杀软的目录保护锁成只读时，"记不下这件事"比
    "起不来"轻得多。同理，安装根不存在时**绝不**顺手 mkdir 出来——那只会把"路径敲错了"
    掩盖成"凭空长出一棵谁都没打算要的目录树"。
    """
    path = install_root / DATA_DIR_REL / LOGS_DIR / LAUNCHER_LOG

    def log(message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} 小助手：{message}"
        if install_root.is_dir():
            with contextlib.suppress(OSError):
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        # 维护者远程手动跑的时候看得见（长辈那边没人会看 stdout：桌面快捷方式跑在 pythonw
        # 下，连控制台都没有）。
        #
        # 注：pythonw.exe 下 sys.stdout 是 None，但 print() **不会抛异常**——CPython 的
        # print() 见到 sys.stdout is None 就直接返回，是个空操作（已实测）。这里的 suppress
        # 不是为了挡它。它挡的是别的：stdout 被重定向到一个已关闭的文件（ValueError）、管道
        # 对端没了（BrokenPipeError）、控制台编码塞不下中文（UnicodeEncodeError）。日志的
        # 原则不变——"记不下这件事"比"起不来"轻得多，一句 print 永远不许把启动弄挂。
        with contextlib.suppress(Exception):
            print(line)

    return log


def _try_lock(install_root: Path):
    """在安装根上取一把 OS 级排他锁；拿不到就返回 None（另一个启动器正在跑）。

    为什么必须有锁：见模块 docstring 第 5 条——长辈双击第二下会造出一个假的"启动即崩"。

    为什么是文件句柄上的锁、而不是 O_EXCL 标记文件：标记文件在断电后会永久残留，它自己
    就变成一块砖（此后每次双击都以为"有别人在启动"，小助手再也打不开）。OS 级锁在进程消失
    时（正常退出/崩溃/断电重启）由内核自动释放，不可能留下这种残留。

    打不开锁文件（安装根不存在、盘没挂载、无权限）**不在这里吞掉**：那不是"别人在跑"，
    那是这台机器坏了。把它和"拿不到锁"混成同一个返回值，就会变成"安静地返回 0、什么都没
    启动"——本项目抓过 7 次的那类 bug。让 OSError 抛出去，由 run() 记日志 + 返回非零。
    """
    fh = (install_root / LAUNCH_LOCK_FILE).open("a+b")  # 不截断：锁文件长期存在，内容无关紧要
    try:
        # seek 必须在下面那个 try **之外**：msvcrt.locking 锁的是"从当前位置起的 N 个字节"，
        # 所以两个启动器必须锁同一个字节（"a+b" 打开后位置未必是 0）。但 seek 失败是**盘出
        # 问题了**，不是"别人拿着锁"——混进同一个 except 的话，一次 I/O 错误会被读成"另一个
        # 启动器正在跑"，于是安静返回 0、什么都不启动。那正是本项目抓过 7 次的那类 bug。
        fh.seek(0)
    except OSError:
        fh.close()  # 抛给 run()：它会记日志 + 返回非零（而不是伪装成"有别人在跑"）
        raise
    try:
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


def _read(path: Path) -> str | None:
    """读一个记账文件。**"" 和 None 是两件不同的事，绝不能混**：

    - `""` = 文件不在、或者是空的。这是一个**已知的事实**（更新器就是靠"previous.txt 不
      存在"来诚实地表达"无回滚目标"的）。
    - `None` = **读不出来**：被杀软锁住（PermissionError）、被写坏成非 UTF-8
      （UnicodeDecodeError——它是 **ValueError** 的子类，不是 OSError）、盘 I/O 错误。
      这是"证据缺失"，不是任何一个事实。调用方必须各自按"安全方向"处理它
      （见 _is_proven / _rollback_target / _blacklist，以及模块 docstring 第 2 条的定向规则）。

    而**读自己的记账文件，绝不允许把启动弄挂**。这正是 updater.py 里已经修过一次的那个
    bug：读记账文件那一行留在 try 之外 → 杀软锁住它 → 每次开机都崩。启动器这边更狠一层：
    last_good.txt **每次健康启动都会被重写一遍**，是下一次开机时杀软扫描的头号目标——
    而这台机器上的版本可能完全没问题。读不出一个记账文件就拒绝启动 = 爸妈双击了没反应，
    而机器好好的。
    """
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except (OSError, ValueError):
        return None


def _version_keyed_int(path: Path, version: str) -> int | None:
    """读一个 `<版本号> <整数>` 形状的旁证账本（startup_failures.txt 和 desktop.pid 都是它）。

    不存在 / 是空的 / **读不出来** / 记的是别的版本 / 内容不成形 → 一律 None = **没有这份
    证据**。五种情况倒向同一个方向，因为它们说的是同一件事：这里拿不出可用的旁证。调用方
    各自把 None 翻译成自己那一侧的安全默认值（失败次数 0、没有活着的 PID），而两者的安全
    方向都是"别开火"（模块 docstring 第 3 条的定向规则）。

    只认版本对得上的记录：更新 / 回滚之后，账本上那条记录说的是**另一个**版本——上一个版本
    的前科跟这个版本无关，它的 PID 更不能拿来给这个版本免死。
    """
    record = _read(path)
    if not record:
        return None
    parts = record.split()
    if len(parts) != 2 or parts[0] != version:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _is_proven(install_root: Path, current: str, log) -> bool:
    """这个版本在这台机器上活过一次健康窗口吗（last_good.txt）？它是回滚的军械开关。

    **读不出来时返回 True（= 解除武装）。** 方向不能反：读不出证据 = 无法证明"这个版本
    从没被证明过健康"，而回滚 + 拉黑是不可逆的。当作"没证明过"的话，杀软对 last_good.txt
    的一次瞬时锁就足以把一个好版本永久踢出这台机器的更新通道，且完全静默。反过来的代价是
    对称的另一半：一个真坏的版本可能因此不被回滚——那是**响亮**的失败（双击没反应 →
    打电话），维护者能救。
    """
    last_good = _read(install_root / LAST_GOOD_FILE)
    if last_good is None:
        log(f"{LAST_GOOD_FILE} 读不出来（被锁住或已损坏）——无法证明 {current} 没被证明过健康，"
            "按「已证明健康」处理：照常启动，绝不回滚")
        return True
    return last_good == current


def _pid_alive(pid: int) -> bool:
    """这个 PID 现在还活着吗？

    ⚠️ Windows 上**绝不能**用 os.kill(pid, 0) 来探测：CPython 在 Windows 上把 os.kill
    实现成 TerminateProcess(handle, sig)——signal 0 不是"只做检查"，而是"用退出码 0 把它
    杀掉"。那会在长辈眼前把正在跑的小助手直接干掉。只能走 OpenProcess + GetExitCodeProcess。
    """
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)  # POSIX：signal 0 只做存在性/权限检查，不发信号
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程在，只是不归当前用户管
    return True


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87  # 没有这个 PID——事实，不是缺证据
_ERROR_ACCESS_DENIED = 5  # 进程在，但查不动：另一个 Windows 账号 / 杀软 hook 了 OpenProcess / 已提权


def _load_kernel32():  # pragma: no cover - 真实 ctypes 绑定，只在 Windows 上才会被调用
    """加载 kernel32，并给 _pid_alive_windows 用到的三个函数声明 restype/argtypes。

    不声明的后果是 Win64 ctypes 的经典坑：ctypes 默认把 restype 当成 32 位有符号 int，
    而 HANDLE 在 Win64 上是 64 位——句柄的高 32 位被截断，一个本来非空的句柄可能被读成
    0，"进程不在了"那条分支就被错误触发，这张 PID 安全网悄悄失效。use_last_error=True
    是为了让 GetLastError()（经由 ctypes.get_last_error()）在这几次调用之后可靠可读。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


class _RealKernel32:  # pragma: no cover - 真实 Windows API 调用，只在 Windows 上才会被调用
    """_pid_alive_windows 需要的三个 kernel32 调用，包成可注入的对象。

    这是让 _pid_alive_windows 能在非 Windows 机器上被测试到的全部机关：测试把模块级
    `_win_kernel32` 换成一个假对象，就能驱动 OpenProcess 的三种失败原因（PID 不存在 /
    拒绝访问 / 未知错误），不需要真的跑在 Windows 上——这张网此前从没在任何机器上
    执行过一次。
    """

    _dll = None

    def _kernel32(self):
        if self._dll is None:
            self._dll = _load_kernel32()
        return self._dll

    def open_process(self, pid: int) -> tuple[int, int]:
        """返回 (handle, last_error)。handle 为假值时 last_error 才有意义。"""
        import ctypes

        ctypes.set_last_error(0)
        handle = self._kernel32().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return handle, ctypes.get_last_error()

    def get_exit_code(self, handle) -> int | None:
        """返回退出码；查不出来（句柄失效 / 权限被收回）时返回 None。"""
        import ctypes
        from ctypes import wintypes

        code = wintypes.DWORD()
        if not self._kernel32().GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value

    def close(self, handle) -> None:
        self._kernel32().CloseHandle(handle)


_win_kernel32 = _RealKernel32()  # 测试注入点：patch 这个模块级引用即可在非 Windows 上驱动


def _pid_alive_windows(pid: int) -> bool:
    """这个 PID 现在还活着吗（Windows）。

    ⚠️ 定向规则（模块 docstring 开头那条）：**读不出来的证据只能解除武装，绝不能武装**。
    OpenProcess 失败时必须按 GetLastError 分两种截然不同的情况，不能一律当成"它不在了"：

    - ERROR_INVALID_PARAMETER（87）：这个 PID **根本不存在**——是一个事实，不是缺证据，
      可以放心当"没有它还活着的证据"（返回 False；代价有边界：只是不给这次观察免死，
      不会拉黑任何东西，见 _running_desktop_pid）。
    - 其他任何失败（尤其是 ERROR_ACCESS_DENIED=5：进程属于**另一个 Windows 账号**、被
      **国产杀软 hook 住了 OpenProcess**、或者进程已提权——这三种恰恰是这张 PID 网最该
      起作用的场景）：进程可能好好地在跑，我们只是**查不动**。查不动是"读不出来"的一种，
      必须解除武装（当成"活着"，返回 True），否则这张网会在它最该开火的场景里悄悄失效。

    以前这里不分青红皂白地把"拿不到句柄"一律读成"进程不在了"（False），这和 POSIX 分支
    （`_pid_alive` 里的 `except PermissionError: return True`）方向相反——同一个函数的
    两个分支对同一种"查不动"给出了相反的答案，而生产平台（Windows）正好是坏的那一个。

    GetExitCodeProcess 拿到句柄之后查不出退出码，同理：也是查不动，不是"它已经退出"。
    """
    handle, last_error = _win_kernel32.open_process(pid)
    if not handle:
        if last_error == _ERROR_INVALID_PARAMETER:
            return False  # 没有这份证据：这个 PID 根本不存在
        return True  # 查不动（拒绝访问 / 未知错误）——解除武装，不是"它不在了"
    try:
        code = _win_kernel32.get_exit_code(handle)
        if code is None:
            return True  # 拿到了句柄却查不出退出码——同样是查不动，解除武装
        return code == _STILL_ACTIVE
    finally:
        _win_kernel32.close(handle)


def _running_desktop_pid(install_root: Path, version: str) -> int | None:
    """上一次**活过健康窗口**的那个桌面端进程还活着吗？活着就返回它的 PID。

    这是把"小助手已经在跑了"从**推断**变成**观察**（模块 docstring 第 3 条）。长辈没看见
    窗口就再双击一下是常态：Electron 的单实例锁会让第二个实例瞬间以 0 退出——那和"启动即崩"
    在退出码和存活时间上完全一样。只靠 last_good.txt 区分的话，**记 last_good 那一次写入
    失败**（杀软锁了文件）就足以让下一次双击把一个跑得好好的版本回滚掉 + 永久拉黑。而那个
    桌面端进程此刻**就在跑**——它活着这件事是可以直接观察的，不必推断。

    PID 会被系统复用，所以它只是**第二道**网（旁证计数是第一道）：一次误信的代价是"以为
    小助手已经在跑"而返回 0，不会造成任何不可逆的后果。

    ⚠️ **已考虑、暂不实现的加固（M1）：desktop.pid 是纯 `<版本号> <PID>`，不校验这个
    PID 现在是不是"同一个"进程。** 如果记下的 PID 后来被系统回收、分配给一个恰好长期
    存活的无关进程（长辈的杀毒软件常驻进程、系统服务……），`_pid_alive` 会一直认为它
    "还活着"——于是这个版本每一次"瞬间以 0 退出"都会被读成"已经在跑"，
    startup_failures 计数永远不会被推进，一个真的坏掉的版本就再也凑不满 CORROBORATING_
    FAILURES，永远不会被回滚 + 拉黑。方向本身是吵的（长辈会一直打不开、一直打电话），
    不是"静默毁掉一个好版本"那个最坏方向，但代价是真实的，而且便宜的加固是给这条记录
    再绑一个"创建时间"之类的第二特征，要求两者都对得上。

    没有做这件事，是因为在这个模块能测试到的环境（本仓库的测试跑在 macOS 上，且很多
    测试通过 `_pid_alive` 的 POSIX 分支驱动**真实的** subprocess）里，找不到一种不引入
    新依赖、也不引入新失败面的方式来获得"进程创建时间"：标准库在 macOS 上没有等价于
    Linux `/proc/<pid>/stat` 的入口；能用的办法要么是加 `psutil`（明确不允许为此加依赖），
    要么是 shell 出去跑 `ps` 再解析其输出（`lstart` 这类字段的格式随 locale/BSD-vs-GNU
    实现而变，秒级精度在测试里两次快速 spawn 之间可能撞车——本身就是这个代码库一贯反对
    的那类"脆弱猜测"）。真正能测的实现只能把 desktop.pid 的记录形状从两个字段改成三个，
    而好几条已经钉住的安全网测试逐字节比对着这个文件的内容（如
    test_a_failed_last_good_write_does_not_arm_the_rollback、
    test_the_recorded_pid_is_only_trusted_while_it_is_alive）——改形状意味着要么让这个
    新校验只在 Windows 上生效但在这个仓库里永远测不到（重新制造"零执行"的老问题），要么
    为了测它而改动已被判定 Approved 的记账格式，两者都超出了"cheap hardening"该做的事。
    留给 Task 8：那时会有真机（Windows），可以用同一套 `_win_kernel32` 机关加
    GetProcessTimes，并在真机上验证这条记录形状的改动。
    """
    pid = _version_keyed_int(install_root / DESKTOP_PID_FILE, version)
    if pid is None or pid <= 0:  # 没有这份证据（不影响其他判断）
        return None
    return pid if _pid_alive(pid) else None


def _startup_failures(install_root: Path, version: str) -> int:
    """这个版本已经被观察到过几次"启动即崩"（startup_failures.txt）。

    没有这份证据（读不出来 / 记的是别的版本 / 内容不成形）→ 0。这个方向是安全的：计数
    凑不满就永远不会回滚 + 拉黑（模块 docstring 第 3 条）。
    """
    seen = _version_keyed_int(install_root / STARTUP_FAILURES_FILE, version)
    if seen is None:
        return 0
    return max(seen, 0)


def _count_startup_failure(install_root: Path, version: str, log) -> int:
    """又观察到一次"启动即崩"。返回这个版本累计被观察到的次数。"""
    seen = _startup_failures(install_root, version) + 1
    try:
        atomic_write(install_root / STARTUP_FAILURES_FILE, f"{version} {seen}\n".encode("utf-8"))
    except OSError as exc:
        # 记不下 = 下一次双击从头数起 = 旁证永远凑不满 = 永远不回滚。这台机器连记账文件都
        # 写不下，本来也不该让它去执行一个不可逆的动作。
        log(f"记录启动失败次数失败（{exc}）——下次双击会重新计数，安全网只会更保守")
    return seen


def _forget_startup_failures(install_root: Path) -> None:
    """窗口画出来过 = 这个版本在这台机器上**能跑**。之前那些失败观察必须作废，否则它们会
    一直躺在计数器里，等着和几个月后一次无关的失败凑成"两次"，回滚掉一个天天在用的好版本。
    """
    with contextlib.suppress(OSError):
        (install_root / STARTUP_FAILURES_FILE).unlink(missing_ok=True)


def _reconcile_factory_state(
    install_root: Path, version: str, workspace_dir: Path, log, force: bool = False
) -> None:
    """出厂状态校对。契约要求（不是建议）：**每个会切版本的进程，都必须在自己每次运行的
    开头校对一次**（builder/paths.py 的模块 docstring）。启动器是纵深防御的第二道——更新器
    的出厂状态是在提交点**之后**应用的，它失败过一次，机器就停在"新代码 + 旧 persona"上。

    force=True 用在**回滚之后**：那时不能只信戳。戳只记录 apply_factory_state 写过什么，
    而刚刚那个坏版本可能在运行时自己改写过 data/config.yaml（写成了新 schema）——戳对此
    一无所知。切回旧代码却留着新配置 = 旧代码读不懂自己的配置 = 回滚之后照样起不来。
    apply_factory_state 是幂等的，多铺一遍的代价只是几个文件。

    失败绝不能挡住启动：出厂状态陈旧的机器**还能用**（旧 persona、旧配置都是上一版下发的、
    合法的文件），起不来的机器什么都不是。戳没落 = 没有伪造"已收敛"的凭据，更新器下次开机
    会照样重试一次自愈。
    """
    try:
        if not force and factory_state_is_current(install_root, version):
            return
        apply_factory_state(install_root, version, workspace_dir)
    except (ValueError, OSError) as exc:
        # 母版不可用（缺失、被弄坏、被杀软锁住读不出来）或这台机器套用不上（data/ 只读、
        # data/skills 是符号链接）。收敛不了，但机器还能跑——继续启动。
        log(f"出厂状态自愈失败（{exc}）——继续启动，persona/配置可能是旧的")


def _attempt(
    install_root: Path,
    version: str,
    exe_argv: list[str] | None,
    health_seconds: float,
    startup_seconds: float,
    spawn,
    log,
) -> _Attempt:
    """启动某个版本的桌面端，等一个健康窗口，判定结局。

    判定规则见模块 docstring 第 1 条。这里只回答"它起来了吗"，**不**回答"要不要回滚"
    ——那是 _run_locked 的事（第 2 条）。
    """
    argv = exe_argv or default_exe_argv(install_root, version)
    started = time.monotonic()
    try:
        proc = spawn(argv, _child_env(install_root, version))
    except OSError as exc:
        # electron.exe 根本不在（包坏了 / 被杀软隔离了）。这个版本跑不了——和"起来就崩"
        # 是同一类故障，绝不能让异常一路逃出去：那样爸妈双击了没反应，而且什么都没被修。
        log(f"版本 {version} 根本起不来（{argv[0]}：{exc}）")
        return _Attempt(CRASHED, None, None)

    pid = getattr(proc, "pid", None)
    try:
        code = proc.wait(timeout=health_seconds)
    except subprocess.TimeoutExpired:
        log(f"版本 {version} 活过 {health_seconds} 秒健康窗口——启动成功")
        return _Attempt(HEALTHY, None, pid)

    alive = time.monotonic() - started
    if alive < startup_seconds or code != 0:
        log(f"版本 {version} 启动即崩：{alive:.1f} 秒后以退出码 {code} 退出")
        return _Attempt(CRASHED, code, pid)
    log(f"版本 {version} 存活 {alive:.1f} 秒后正常退出——用户自己关的窗口，不是故障")
    return _Attempt(CLOSED, code, pid)


def _mark_good(install_root: Path, version: str, pid: int | None, log) -> None:
    """记下"这个版本在这台机器上活过一次健康窗口"（last_good.txt = 回滚的军械开关），
    以及那个桌面端进程的 PID（desktop.pid = "小助手已经在跑了"的可观察凭据）。

    ⚠️ **记不下绝不是"偏保守"——这里以前的注释把方向写反了。** 记不下 last_good 的后果是：
    下一次双击时，一次单实例回弹（瞬间以 0 退出）会被读成"启动即崩"→ 回滚 + **永久拉黑一个
    完全正常的版本**。"更容易回滚"不是保守，它正是这套设计存在的全部意义所要防的**灾难方向**
    （模块 docstring 第 2 条）。

    所以这个洞不能靠"耸耸肩，反正桌面端起来了"来补，而是靠另外两道网真正兜住：
      - desktop.pid：这两次写入**通常互不牵连**（杀软锁住的往往是"刚被重写过的那个文件"，
        而 last_good.txt 每次健康启动都重写，是头号目标；desktop.pid 相对冷门）。它还在跑
        这件事可以直接观察。
      - 旁证计数（CORROBORATING_FAILURES）：一次假崩溃不足以开火。而如果这台机器**根本
        写不下任何记账文件**，计数就永远凑不满 → 永远不回滚 —— 那正是安全方向。

    两次写入都是 best-effort：桌面端已经在跑了，那才是长辈要的东西，绝不能把一次成功的启动
    报成失败。

    ⚠️ **这个双网互不牵连是这个设计的残余前提（residual precondition），不是已经证明了的
    事。** "两次写入互不牵连"是一个假设：如果这两次写入**同时**失败（比如整个 data/ 目录
    在这一刻被杀软的实时防护锁死，两个文件谁都写不进去），而 startup_failures.txt **恰好
    还写得进去**（它在 data/ 之外，见 STARTUP_FAILURES_FILE），那么这一次健康启动不会留下
    last_good 也不会留下 desktop.pid——接下来只要长辈两次双击撞上单实例回弹或手滑关窗口，
    旁证计数照样能凑满两次，一个刚刚活过健康窗口的好版本还是会被回滚 + 永久拉黑。这不是
    今天就能关掉的洞，而是"两个独立记账文件"这套设计本身的边界。真正退休这个假设（以及
    未经实测的 STARTUP_SECONDS、"没有任何正面证据证明窗口真的画出来过"这个缺口）的办法，
    是让桌面端（Electron 应用自己）写一个就绪/PID 信号——那样"活过健康窗口"就不再需要
    启动器这边靠两个 best-effort 写入去拼凑，而是有一份应用自己作证的记录。这项工作属于
    Task 8，此处不实现。
    """
    try:
        atomic_write(install_root / LAST_GOOD_FILE, version.encode("utf-8"))
    except OSError as exc:
        log(f"记录健康版本失败（{exc}）——桌面端已经起来了，本次启动仍然算成功")
    if pid is None:
        return
    try:
        atomic_write(install_root / DESKTOP_PID_FILE, f"{version} {pid}\n".encode("utf-8"))
    except OSError as exc:
        log(f"记录桌面端 PID 失败（{exc}）——桌面端已经起来了，本次启动仍然算成功")


def _rollback_target(install_root: Path, current: str) -> str | None:
    """能回滚到哪个版本，没有就返回 None（绝不猜）。

    previous.txt 可能根本不存在——那是更新器**有意为之**的语义：诚实的"无回滚目标"好过
    一个看着有效的假目标。它也可能指向一个磁盘上已经不存在的目录（被剪掉了、被长辈删了）：
    照着它回滚 = current.txt 指向虚空 = 这台机器**再也起不来**，比原地崩着还糟。更新器有
    _drop_lying_previous 来防这个，但启动器是最后一道防线，不能假设前面那道一定跑过。

    previous.txt **读不出来**（被杀软锁住、被写坏）同样返回 None：不知道能回滚到哪儿，
    就是没有回滚目标。绝不能拿一个读不出来的文件当"有退路"的凭据去拉黑 current
    （模块 docstring 第 2 条的定向规则）。
    """
    previous = _read(install_root / PREVIOUS_FILE)
    if not previous or previous == current:  # None（读不出来）也走这条：绝不猜
        return None
    if not _version_dir(install_root, previous).is_dir():
        return None
    return previous


def _write_versions(path: Path, versions: list[str]) -> None:
    atomic_write(path, "".join(f"{v}\n" for v in versions).encode("utf-8"))


def _read_blacklist(path: Path, refusal: str) -> list[str]:
    """读拉黑名单。**读不出来（杀软锁住 / 被写坏）就抛出去。**

    拉黑和撤销拉黑都是**读-改-写**，所以两者都卡在这一道上：旧名单读不出来时还硬要整份
    覆盖，会把名单上**别的**坏版本静默地"洗白"，它们会被更新器一个个重新装回来。响亮地
    失败，机器状态一个字节都不动（run() 会接住，日志 + 非零退出码）。
    """
    listed = _read(path)
    if listed is None:
        raise OSError(f"{BAD_VERSIONS_FILE} 读不出来（被锁住或已损坏），{refusal}")
    return listed.split()


def _blacklist(install_root: Path, version: str) -> None:
    """把坏版本记进 bad_versions.txt——更新器读它，永远不再自动装这个版本。

    没有这一行，更新器下次开机就会看见通道里那个"更新的"版本、又下 895MB、又装、又崩、
    又回滚……每次开机重演一遍，把爸妈的带宽烧光（updater 模块 docstring 第 4 条）。

    **读-改-写，整份原子换名**（和这个安装根里其他所有状态文件一样）。直接 append 的话，
    断电 / 磁盘满会留下**半行**——而更新器是 `set(f.read_text().split())` 读它的：半个版本号
    匹配不上任何东西，那个坏版本于是被**静默地重新装回来**，安全网整个失效。
    """
    path = install_root / BAD_VERSIONS_FILE
    versions = _read_blacklist(path, "无法安全地改写拉黑名单")
    if version in versions:
        return  # 上一次回滚崩在半路上又重来了一遍：别写重复行
    _write_versions(path, versions + [version])


def _unblacklist(install_root: Path, version: str) -> None:
    """把某个版本从 bad_versions.txt 里拿掉（见 _undo_rollback：回滚目标**也**起不来时，
    这一行拉黑必须撤销）。只动这一个版本——整份重写成一行的话，名单上真正坏的那些版本就被
    静默洗白了，更新器下次开机又会把它们装回来。"""
    path = install_root / BAD_VERSIONS_FILE
    versions = _read_blacklist(path, "无法撤销拉黑")
    _write_versions(path, [v for v in versions if v != version])


def run(
    install_root: Path,
    workspace_dir: Path,
    exe_argv: list[str] | None = None,
    health_seconds: float = HEALTH_SECONDS,
    startup_seconds: float = STARTUP_SECONDS,
    spawn=_spawn,
) -> int:
    """启动 current 版本的桌面端。返回进程退出码语义：0 = 小助手起来了（或已经在跑）。

    workspace_dir（桌面的「小助手」文件夹）要**显式传进来**，本模块不自己去推导：推导
    （default_workspace_dir）只发生在 main() 那一层。这样测试里不会有哪个用例在开发机
    （和 CI）的真实桌面上凭空建出一个「小助手」文件夹来。

    exe_argv 缺省用 default_exe_argv()（⚠️ 尚未在真机验证，见那个函数）；测试注入假进程。
    """
    log = _logger(install_root)
    try:
        lock = _try_lock(install_root)
    except OSError as exc:
        # 安装根不存在 / 盘没挂载 / 无权限。绝不能安静地返回 0：那就是"报告成功、实际什么
        # 都没做"——爸妈双击了没反应，而所有人都以为小助手起来了。
        log(f"打不开安装根 {install_root}（{exc}）——没有东西可启动")
        return 1
    if lock is None:
        # 另一个启动器正在跑（长辈双击了第二下）。它会把活干完，这里什么都不做：再 spawn
        # 一个桌面端只会造出第二个窗口，或者被单实例锁瞬间弹回、伪装成一次"启动即崩"。
        log("已经有一个小助手正在启动——本次双击忽略")
        return 0
    try:
        return _run_locked(
            install_root, workspace_dir, exe_argv, health_seconds, startup_seconds, spawn, log
        )
    except (OSError, ValueError) as exc:
        # 切版本写不下去（安装根只读、杀软锁了 current.txt）。响亮地失败，绝不报成功。
        #
        # 为什么连 ValueError 一起接：记账文件被写坏成非 UTF-8 时，read_text() 抛的是
        # UnicodeDecodeError——**ValueError 的子类，不是 OSError**。今天 _read 已经在源头
        # 把这两类都接住了（所以这一层接不接 ValueError，现有测试都是绿的——它是**兜底**，
        # 不是主防线）。留着它，是因为这条路上以后每多一次读写，就多一次"忘了包 try"的机会，
        # 而漏出去的代价是长辈双击了没反应、裸 traceback 进了一个没人看的地方。
        log(f"启动失败（{exc}）——这台机器需要维护者远程介入")
        return 1
    finally:
        _unlock(lock)


def _run_locked(
    install_root: Path,
    workspace_dir: Path,
    exe_argv: list[str] | None,
    health_seconds: float,
    startup_seconds: float,
    spawn,
    log,
) -> int:
    def attempt(version: str) -> _Attempt:
        return _attempt(
            install_root, version, exe_argv, health_seconds, startup_seconds, spawn, log
        )

    current = _read(install_root / CURRENT_FILE)
    if not current:
        # 不知道该启动哪个版本（缺失、为空、或读不出来）。绝不猜（从 versions/ 里挑一个
        # "看起来最新的"就是编造）。
        log(f"{CURRENT_FILE} 缺失、为空或读不出来——不知道该启动哪个版本，拒绝猜。需要维护者远程介入")
        return 1

    # 契约要求：会切版本的进程在每次运行开头校对一次出厂状态。必须在 spawn **之前**——
    # 桌面端起来之后再去改它正在读的 config.yaml / SOUL.md 等于什么都没修。
    _reconcile_factory_state(install_root, current, workspace_dir, log)

    # 两份证据都必须在 spawn **之前**读：spawn 之后，desktop.pid 里那条"上一次的记录"就可能
    # 被本次启动自己覆盖掉了（那就变成了拿本次的观察去证明本次，什么也证明不了）。
    proven = _is_proven(install_root, current, log)
    running = _running_desktop_pid(install_root, current)

    outcome, code, pid = attempt(current)
    if outcome == HEALTHY:
        _forget_startup_failures(install_root)
        _mark_good(install_root, current, pid, log)
        return 0
    if outcome == CLOSED:
        # 窗口画出来过、长辈用完自己关的。这个版本在这台机器上**能跑**——把之前那些失败观察
        # 作废（否则它们会一直躺在计数器里，等着和几个月后一次无关的失败凑成"两次"）。
        _forget_startup_failures(install_root)
        return 0

    # 崩了。但"崩了"不等于"该回滚"（模块 docstring 第 2 条）。
    if code == 0 and running is not None:
        # **观察**，而不是推断（第 3 条）：上一次活过健康窗口的那个桌面端进程**此刻还在跑**。
        # 那么这次"瞬间以 0 退出"就是 Electron 单实例锁把第二个实例弹了回来——长辈没看见
        # 窗口又双击了一下。这条路专门救一种情况：桌面端明明起来了，但记 last_good 的那次
        # 写入被杀软的瞬时锁挡掉了（_mark_good）——只靠 last_good 的话，这里会回滚 + 永久
        # 拉黑一个完全正常的版本。
        log(f"版本 {current} 立刻以 0 退出，而上次活过健康窗口的桌面端（PID {running}）还在跑"
            "——小助手已经在跑了（重复双击）。不回滚")
        return 0
    if proven:
        if code == 0:
            # 这个版本以前活过整整一个健康窗口，它不会今天突然变成"起来就以 0 退出"。
            # 压倒性的解释是：小助手**已经在跑了**，长辈没看见窗口又双击了一下，Electron
            # 的单实例锁把第二个实例瞬间弹了回来（退出码 0）——那和"启动即崩"在退出码和
            # 存活时间上完全一样，只能靠"这个版本被证明健康过"来区分。这是成功，不是故障。
            log(f"版本 {current} 立刻以 0 退出——小助手多半已经在跑了（重复双击）。不回滚")
            return 0
        log(
            f"版本 {current} 以前活过健康窗口，今天崩了（退出码 {code}）——这不是新版本引入的"
            "故障，回滚治不了它、只会毁掉一个好版本。不回滚。请长辈按「小助手修复」，"
            "或由维护者远程介入"
        )
        return code or 1

    # 从没被证明健康过的版本崩了。**但一次观察还不够扣扳机**（第 3 条）：单实例回弹、手滑
    # 关窗口、杀软瞬时锁，都和真崩溃长得一模一样，而回滚 + 拉黑是不可逆的。真坏的版本每次
    # 双击都会崩；假崩溃不会重演。
    seen = _count_startup_failure(install_root, current, log)
    if seen < CORROBORATING_FAILURES:
        log(
            f"版本 {current} 启动失败（第 {seen} 次观察，需要 {CORROBORATING_FAILURES} 次才回滚）"
            "——回滚 + 拉黑是不可逆的，而一次假崩溃（重复双击被单实例锁弹回、手滑关窗口、"
            "杀软瞬时锁）和真崩溃长得一模一样。请长辈再双击一次：真坏的版本会再崩一次，"
            "安全网就开火了"
        )
        return code or 1

    previous = _rollback_target(install_root, current)
    if previous is None:
        log(
            f"版本 {current} 启动即崩，而没有可用的回滚目标（{PREVIOUS_FILE} 不存在、"
            "指向自己、指向一个已经不在磁盘上的版本、或读不出来）——绝不猜一个版本回滚过去。"
            "小助手起不来了，需要维护者远程介入"
        )
        return code or 1

    log(f"版本 {current} 启动即崩（已 {seen} 次独立观察） → 回滚到 {previous}")
    return _rollback(install_root, current, previous, workspace_dir, attempt, log)


def _rollback(
    install_root: Path,
    current: str,
    previous: str,
    workspace_dir: Path,
    attempt,
    log,
) -> int:
    """开火：拉黑 current、切回 previous，**然后等着看旧版本能不能起来**。

    调用方（_run_locked）已经判定过该开火：这个版本从没被证明健康过、旁证够了、而且
    previous 是一个磁盘上真实存在的回滚目标。这里只负责按正确的顺序执行，以及**验证机器
    真的被救回来了**——回滚完再 spawn 一下就报成功，正是本项目抓过 7 次的那个 bug。
    """
    # 顺序不能反：**先拉黑，再切 current.txt**。反过来的话，断在两次写入之间 = 机器回到了
    # 旧版本（能用），但坏版本没被拉黑 → 更新器下次开机又把它装回来、又崩、又回滚，每次
    # 开机重下 895MB。先拉黑的话，断在中间最坏也只是"还停在坏版本上、但它已经被拉黑了"：
    # 下次开机启动器再崩一次、再回滚一次就收敛了，而更新器绝不会再把它装回来。
    _blacklist(install_root, current)
    atomic_write(install_root / CURRENT_FILE, previous.encode("utf-8"))
    # previous.txt 已经被消费掉了：现在 current **就是**它。留着它 = 一个指向自己的假回滚
    # 目标。必须在 current.txt 写成功**之后**才删——反过来的话，断在中间就是"current 还是
    # 坏版本，而回滚目标没了"，磁盘上明明躺着一个能启动的旧版本，机器却永远救不回来。
    (install_root / PREVIOUS_FILE).unlink(missing_ok=True)

    # 出厂状态必须在 current.txt 切过去**之后**才应用（顺序反过来会得到"旧代码 + 新配置"，
    # 更危险）。force：坏版本可能在运行时改写过 data/config.yaml，而戳对此一无所知。
    _reconcile_factory_state(install_root, previous, workspace_dir, log, force=True)

    outcome, code, pid = attempt(previous)
    if outcome == CRASHED:
        # 旧版本也起不来 = 机器**没有**被救回来。绝不报成功。
        log(f"回滚到 {previous} 之后仍然起不来——小助手起不来了，需要维护者远程介入")
        _undo_rollback(install_root, current, previous, log)
        return code or 1
    _forget_startup_failures(install_root)  # 这笔账结了：坏版本已经拉黑并切走
    if outcome == HEALTHY:
        _mark_good(install_root, previous, pid, log)
    log(f"已回滚到 {previous}；{current} 已拉黑，更新器不会再自动装它")
    return 0


def _undo_rollback(install_root: Path, current: str, previous: str, log) -> None:
    """回滚目标**也**起不来 → 撤销拉黑，复原 current.txt / previous.txt（模块 docstring 第 4 条）。

    两个版本用**同一条**命令、以**同一种**方式失败（尤其是在 argv[0] 上抛 OSError），是压倒性
    的证据：错的是**那条启动命令**（default_exe_argv 至今没在真机上验证过——Task 2 的 spike
    从没启动过 GUI），或者 data/ 坏了。两者都不是"这个版本坏了"，而拉黑惩罚的恰恰是版本。
    什么都不撤销的话，终局是所有可能里**最坏的一个**：一个好版本被永久踢出更新通道
    （bad_versions.txt 只增不减，且没有任何东西会主动告诉一万公里外的维护者）、回滚目标被
    消费掉、而机器**依然起不来**。**分不清"版本坏了"和"启动命令错了"的时候，就没有资格拉黑。**

    撤销顺序：**先撤拉黑，再复原版本指针。** 断在中间的两种残局代价不对称——
      - 先撤拉黑：最坏残局是"停在旧版本 + 坏版本没被拉黑"→ 更新器把它重新装回来 → 每次开机
        重下 895MB。很吵、可恢复。
      - 先复原 current.txt：最坏残局是"停在一个**已经被拉黑的**版本上，而回滚目标已经没了"
        → 那个（很可能是好的）版本永久出局，且完全静默。

    出厂状态此刻是 previous 的（回滚时按契约铺过去了），而 current.txt 又被复原成 current
    ——两者对不上。这**不需要**在这里补：戳（data/.factory_version）和 current.txt 对不上，
    正是更新器每次开机都会做的那次自愈校对的触发条件（builder/paths.py 的契约要求）。在这条
    已经很脆的路径上多做一次写入，只会多一个失败点。
    """
    _unblacklist(install_root, current)
    atomic_write(install_root / CURRENT_FILE, current.encode("utf-8"))
    atomic_write(install_root / PREVIOUS_FILE, previous.encode("utf-8"))
    log(
        f"版本 {current} 和回滚目标 {previous} 用同一条命令、以同一种方式失败——这是启动命令"
        f"（或 data/）的问题，不是版本的问题。已撤销对 {current} 的拉黑、复原版本指针："
        "绝不能用一个分不清病因的判断，永久毁掉一个可能完全正常的版本"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="小助手启动器")
    parser.add_argument("install_root", type=Path, help="安装根（C:\\Users\\Public\\xiaozhushou）")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="桌面的「小助手」工作台目录（默认按当前登录用户的桌面推导）",
    )
    args = parser.parse_args()

    # 推导只在这里发生（见 run 的 docstring）。⚠️ 桌面是**按 Windows 用户**算的，所以桌面
    # 快捷方式必须以最终用户身份运行，不能是 SYSTEM（见 factory_state.default_workspace_dir）。
    workspace = args.workspace or default_workspace_dir()
    log = _logger(args.install_root)
    try:
        code = run(args.install_root, workspace)
    except Exception as exc:  # 面向长辈的入口：绝不把裸 traceback 抛进一个没人看的地方
        log(f"启动器自己崩了（{exc!r}）——这是 bug，需要维护者远程介入")
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    # ⚠️ Task 8：桌面快捷方式必须用 **pythonw.exe**（或等价的隐藏窗口方式）跑这个入口。
    # 用 python.exe 的话，健康窗口那 30 秒里会有一个黑色控制台窗口挂在爸妈屏幕上——
    # 他们看到的是"小助手旁边多了个奇怪的黑框框"，然后打电话。
    main()
