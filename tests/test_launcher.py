# tests/test_launcher.py
"""启动器的安全测试。

启动器是**最终用户双击桌面「小助手」图标时**跑的那个东西，也是自动更新通道的
安全网。它有两种失败方式，代价完全不对称：

- **漏判一次真崩溃**（新版本起不来却当成成功）：爸妈双击了没反应 → 打越洋电话。
  很吵，但维护者知道了，能救。
- **误判一次正常使用**（把好版本当成崩溃回滚掉 + 拉黑）：机器**静默地**退回旧版本，
  而且**永远**装不上那个被拉黑的版本了（更新器认 bad_versions.txt）。没有任何人
  会发现——这正是本项目最怕的那类故障。

所以测试不只钉"崩了要回滚"，更要钉住"**什么时候绝不回滚**"：用户自己关窗口、
双击第二下把已经在跑的应用又"启动"一次、某个早就跑得好好的版本今天崩了——
这三种都不是"新版本启动即崩"，一次都不许回滚。

而且"崩了一次"本身就不够格开火：回滚 + 拉黑要求**两次独立观察**
（launcher.CORROBORATING_FAILURES）。所以下面几乎每条回滚测试都要先跑一次
_first_failure()——真正的坏版本每次双击都崩，代价只是长辈多按一下；而单实例回弹、
手滑关窗口这类假崩溃不会重演。
"""

import os
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import yaml

from builder.paths import (
    BAD_VERSIONS_FILE,
    DESKTOP_APP_REL,
    DESKTOP_PID_FILE,
    ELECTRON_EXE_REL,
    FACTORY_STAMP,
    HERMES_HOME_ENV,
    LAST_GOOD_FILE,
    LAUNCH_LOCK_FILE,
    PLAYWRIGHT_ENV,
    STARTUP_FAILURES_FILE,
    WORKSPACE_DIRNAME,
)
from tools import launcher
from tools.launcher import run


def _ws(tmp_path: Path) -> Path:
    """桌面上的「小助手」工作台目录。测试**必须**显式传它：run() 会把它渲染进
    config.yaml、还会在它不存在时建出来。让 run() 自己去推导"真实桌面"的话，跑一次
    测试就会在开发机（和 CI）的 ~/Desktop 下真的长出一个「小助手」文件夹。"""
    return tmp_path / "desktop" / WORKSPACE_DIRNAME


def _factory(root: Path, version: str) -> None:
    """在 versions/<version>/factory/ 下铺一份形状完整的出厂母版（真实发行版的形状）。"""
    f = root / "versions" / version / "factory"
    (f / "skills" / "demo").mkdir(parents=True)
    (f / "config.yaml.tmpl").write_text(
        f'model:\n  default: "{version}"\nterminal:\n  cwd: "{{{{WORKSPACE_DIR}}}}"\n',
        encoding="utf-8",
    )
    (f / "SOUL.md").write_text(f"我是小助手 {version}", encoding="utf-8")
    (f / "skills" / "demo" / "SKILL.md").write_text(f"出厂技能 {version}", encoding="utf-8")


def _root(
    tmp_path: Path,
    cur: str,
    prev: str | None = None,
    *,
    versions: tuple[str, ...] = ("0.1.0", "0.1.1"),
    last_good: str | None = None,
    factories: bool = False,
) -> Path:
    """一台装好了的机器：current.txt（+ 可选 previous.txt）、磁盘上真实存在的版本目录。

    版本目录必须真的存在——previous.txt 指向一个不存在的目录是"假回滚目标"，
    见 test_previous_pointing_at_a_missing_dir_is_not_a_rollback_target。
    """
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    for v in versions:
        (root / "versions" / v).mkdir(parents=True, exist_ok=True)
        if factories:
            _factory(root, v)
    (root / "current.txt").write_text(cur, encoding="utf-8")
    if prev:
        (root / "previous.txt").write_text(prev, encoding="utf-8")
    if last_good:
        (root / "last_good.txt").write_text(last_good, encoding="utf-8")
    return root


class _FakeProc:
    """假桌面端进程。code=None 表示"活过健康窗口"（wait 会超时）。"""

    def __init__(self, code: int | None, alive: float):
        self._code, self._alive = code, alive

    def wait(self, timeout=None):
        if self._code is None:
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("fake-desktop", timeout)
        time.sleep(self._alive)
        return self._code


def _fake_spawn(*outcomes: tuple[int | None, float]):
    """按顺序返回 outcomes 里的进程结局；记录每次 spawn 时的 argv/env/current.txt。

    记录 current.txt 是关键：它证明**回滚那次 spawn 发生在 current.txt 切过去之后**
    ——顺序反了的话，进程起来了但机器还指着坏版本，下次开机原地再崩一次。
    """
    calls: list[types.SimpleNamespace] = []

    def spawn(argv, env):
        code, alive = outcomes[min(len(calls), len(outcomes) - 1)]
        # current.txt 可能不存在（那正是 test_missing_current_txt_... 要测的状态）。这里绝不
        # 能因此抛 OSError：_attempt 会把它当成"版本起不来"接住，于是那条测试就会**因为
        # 错误的原因**变绿——假进程自己炸了，而不是被测代码拒绝了启动。
        current = Path(env[HERMES_HOME_ENV]).parent / "current.txt"
        calls.append(
            types.SimpleNamespace(
                argv=argv,
                env=env,
                current=current.read_text(encoding="utf-8").strip() if current.is_file() else None,
            )
        )
        return _FakeProc(code, alive)

    return spawn, calls


HEALTHY = (None, 0.0)  # 活过健康窗口
INSTANT_CRASH = (1, 0.0)  # 起来就以非零码挂了
INSTANT_EXIT_0 = (0, 0.0)  # 起来就以 0 退出（Electron 崩溃**未必**返回非零码）

FAST = {"health_seconds": 0.4, "startup_seconds": 0.2}


def _blacklisted(root: Path) -> list[str]:
    """bad_versions.txt 里的版本（文件不存在 = 空名单）。"""
    f = root / BAD_VERSIONS_FILE
    return f.read_text(encoding="utf-8").split() if f.exists() else []


def _first_failure(
    root: Path,
    tmp_path: Path,
    outcome=INSTANT_CRASH,
    *,
    exe_argv: tuple[str, ...] = ("fake",),
    blacklist: list[str] | None = None,
) -> None:
    """把"第一次启动失败"这次观察走完（机器状态不该变）。

    回滚 + 拉黑要求两次独立观察（CORROBORATING_FAILURES）：一个真坏的版本每次双击都崩，
    而单实例回弹 / 手滑关窗口不会重演。所以每条"该回滚"的测试都得先经过这一步。

    blacklist = 机器上本来就有的那份名单。缺省 None 表示名单文件**根本不存在**（没有回滚
    就没有拉黑的意义）；传一个列表则表示"名单上本来就有别的坏版本"——这一次观察同样一个
    字节都不许动它。"""
    spawn, _ = _fake_spawn(outcome)
    assert run(root, _ws(tmp_path), exe_argv=list(exe_argv), spawn=spawn, **FAST) != 0
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"  # 只观察了一次：不许动
    if blacklist is None:
        assert not (root / BAD_VERSIONS_FILE).exists()
    else:
        assert _blacklisted(root) == blacklist


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_healthy_launch_returns_zero_and_changes_nothing(tmp_path: Path):
    """活过健康窗口 = 启动成功：进程脱管继续跑，机器状态一个字节都不该动。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    spawn, calls = _fake_spawn(HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert [c.current for c in calls] == ["0.1.1"]
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"


def test_a_healthy_version_is_recorded_as_proven(tmp_path: Path):
    """活过健康窗口的版本要被记下来（last_good.txt）——它是回滚的"军械开关"：
    只有**从没被证明健康过**的版本才允许被回滚掉。没有这个记录，一次普通的
    "用户把窗口关掉"就会把一个跑了几个月的好版本拉黑。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    spawn, _ = _fake_spawn(HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    assert (root / "last_good.txt").read_text(encoding="utf-8").strip() == "0.1.1"


def test_hermes_home_and_playwright_are_exported_to_the_real_child(tmp_path: Path):
    """不设 HERMES_HOME 的后果：Hermes 会**静默地**在 %LOCALAPPDATA%\\hermes 建一个
    全新的空 home——没有激活码、没有技能、没有历史——**而且不报任何错**。整个 data/
    分离架构无声蒸发，一万公里外收不到任何信号。

    这条必须用**真子进程**测：断言"我们给 Popen 传了 env"证明不了子进程真的收到了。
    子进程把自己看到的两个环境变量写进文件，我们读那个文件。
    """
    root = _root(tmp_path, "0.1.1", "0.1.0")
    seen = tmp_path / "child-env.txt"
    child = [
        sys.executable,
        "-c",
        "import os,sys,time;"
        "open(sys.argv[1],'w',encoding='utf-8').write("
        f"os.environ.get({HERMES_HOME_ENV!r},'<未设置>')+'\\n'"
        f"+os.environ.get({PLAYWRIGHT_ENV!r},'<未设置>'));"
        "time.sleep(60)",
        str(seen),
    ]

    spawned = []

    def spy(argv, env):
        proc = launcher._spawn(argv, env)
        spawned.append(proc)
        return proc

    try:
        assert (
            run(root, _ws(tmp_path), exe_argv=child, spawn=spy, health_seconds=1.5,
                startup_seconds=0.2)
            == 0
        )
    finally:
        for proc in spawned:  # 别把一个真进程留给测试套件（也别留下 ResourceWarning）
            proc.kill()
            proc.wait()

    hermes_home, playwright = seen.read_text(encoding="utf-8").splitlines()
    assert Path(hermes_home) == root / "data"
    assert Path(playwright) == root / "versions" / "0.1.1" / "ms-playwright"


# --------------------------------------------------------------------------
# 崩溃 → 回滚
# --------------------------------------------------------------------------


def test_crash_rolls_back_to_previous_and_blacklists_the_bad_version(tmp_path: Path):
    """新版本启动即崩（两次独立观察） → 切回上一版 + 拉黑坏版本（更新器读
    bad_versions.txt，否则会陷入"装 → 崩 → 回滚 → 又装"的死循环，把爸妈的带宽烧光）。

    第二次 spawn 时 current.txt 必须**已经**是旧版本：顺序反了的话，旧版本是起来了，
    但机器还指着坏版本，下次开机原地再崩一次。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    spawn, calls = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert [c.current for c in calls] == ["0.1.1", "0.1.0"]
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert _blacklisted(root) == ["0.1.1"]


def test_an_exit_code_of_zero_inside_the_startup_window_is_still_a_crash(tmp_path: Path):
    """Electron 崩溃时**未必**返回非零码（渲染进程炸了、顶层 handler 吞掉异常后
    app.quit()、配置读不动就自己退了——都可能是 0）。只认非零码的话，最典型的
    "双击之后窗口一闪就没了"会被当成成功，安全网整个失效。

    一个在启动窗口内就消失的窗口，对爸妈来说就是"坏了"——不管退出码是几。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path, INSTANT_EXIT_0)
    spawn, calls = _fake_spawn(INSTANT_EXIT_0, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert [c.current for c in calls] == ["0.1.1", "0.1.0"]
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert _blacklisted(root) == ["0.1.1"]


def test_a_version_that_cannot_even_be_spawned_rolls_back(tmp_path: Path):
    """新版本的包坏了（electron.exe 没打进去、被杀软隔离了）：Popen 直接抛
    FileNotFoundError。这和"起来就崩"是同一类故障——这个版本跑不了——必须走同一条
    回滚路径，而不是让异常一路逃出去（那样爸妈双击了没反应，而且什么都没被修）。

    ⚠️ 只有**坏版本**起不来：旧版本照常起得来（下面那条测试钉的是"两个版本用同一条
    命令一起失败"——那是命令错了，不是版本坏了，绝不能拉黑）。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    spawned = []

    def spawn(argv, env):
        spawned.append(argv)
        if "0.1.1" in argv[0]:
            raise FileNotFoundError(f"[Errno 2] No such file or directory: {argv[0]!r}")
        return _FakeProc(None, 0.0)  # 旧版本起得来

    argv_of = launcher.default_exe_argv  # 真实启动命令（按版本拼出来的）
    _first_failure(root, tmp_path)  # 第一次观察：这个版本崩了一次
    assert run(root, _ws(tmp_path), spawn=spawn, **FAST) == 0

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert _blacklisted(root) == ["0.1.1"]
    assert len(spawned) == 2  # 两个版本都试过了
    assert spawned[0] == argv_of(root, "0.1.1") and spawned[1] == argv_of(root, "0.1.0")


def test_a_launch_command_that_fails_on_both_versions_never_blacklists(tmp_path: Path):
    """**两个版本用同一条命令、以同一种方式失败** = 压倒性的证据说明**命令错了**，
    而不是版本坏了（default_exe_argv 至今没在真机上验证过——Task 2 的 spike 从没启动过
    GUI）。此时如果把 current 留在黑名单里，就等于用一个 launcher 自己的 bug，永久毒化
    这台机器的更新通道：bad_versions.txt 只增不减，更新器只装比 current 更新的版本，
    没有任何东西会主动告诉一万公里外的维护者。

    所以回滚目标也起不来时，必须把拉黑**撤销**、把 current.txt / previous.txt 复原：
    机器还是起不来（响亮地失败，非零退出码 + 日志），但没有任何一个好版本被毁掉。

    这里故意不注入 exe_argv：走的是 default_exe_argv() 拼出来的真实启动命令，
    磁盘上并没有 electron.exe——两个版本都会在 argv[0] 上抛 FileNotFoundError。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    spawned = []

    def spawn(argv, env):
        spawned.append(argv)
        return launcher._spawn(argv, env)  # 真 Popen：文件不存在 → FileNotFoundError

    _first_failure(root, tmp_path)  # 第一次观察：这条命令崩了一次
    assert run(root, _ws(tmp_path), spawn=spawn, **FAST) != 0  # 机器没被救回来：响亮地失败

    assert len(spawned) == 2  # 两个版本都试过了
    assert spawned[0][0].endswith("electron.exe")
    assert _blacklisted(root) == []  # 没有任何版本被拉黑
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"  # 复原了
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"  # 没被消费掉


def test_a_rollback_that_also_fails_is_not_reported_as_success(tmp_path: Path):
    """回滚之后**再启动一次**，然后就报成功——这就是本项目抓过 7 次的那个 bug：
    "报告成功、实际什么都没做"。旧版本要是也起不来（杀软把两个版本都锁了、启动命令
    本身就是错的），退出码必须是非零：机器**没有**被救回来。

    而且此时那个被拉黑的 current 必须被**放回来**：两个版本一起失败说明问题不在版本
    （见上一条）。终局状态绝不能是"好版本被永久拉黑 + 回滚目标被消费掉 + 机器还是
    起不来"——那是所有可能的终局里最坏的一个。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    spawn, calls = _fake_spawn(INSTANT_CRASH, INSTANT_CRASH)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 2
    log = (root / "data" / "logs" / "launcher.log").read_text(encoding="utf-8")
    assert "0.1.0" in log and "起不来" in log
    assert _blacklisted(root) == []
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"


def test_undoing_the_blacklist_keeps_the_other_bad_versions_on_the_list(tmp_path: Path):
    """撤销拉黑只能把**这一个**版本从名单里拿掉。整份文件被重写成一行的话，之前那些
    真坏的版本就被静默地"洗白"了——更新器下次开机又会把它们装回来。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    (root / BAD_VERSIONS_FILE).write_text("0.0.9\n", encoding="utf-8")
    _first_failure(root, tmp_path, blacklist=["0.0.9"])
    spawn, _ = _fake_spawn(INSTANT_CRASH, INSTANT_CRASH)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert _blacklisted(root) == ["0.0.9"]


def test_blacklisting_keeps_the_versions_already_on_the_list(tmp_path: Path):
    """拉黑是**读-改-写**：写的时候必须把名单里已经有的版本原样带上。丢掉它们 =
    那些真坏的版本被更新器重新装回来。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    (root / BAD_VERSIONS_FILE).write_text("0.0.9\n", encoding="utf-8")
    _first_failure(root, tmp_path, blacklist=["0.0.9"])
    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert _blacklisted(root) == ["0.0.9", "0.1.1"]


def test_the_blacklist_is_written_atomically(tmp_path: Path):
    """bad_versions.txt 必须和其他状态文件一样走 atomic_write（tmp + os.replace）。
    直接 append 的话，断电/磁盘满会留下**半行**——而更新器是 set(...split()) 读它，
    半行匹配不上任何版本号 → 那个坏版本被静默地重新装回来。

    钉法：让 atomic_write 失败。原子写的实现下，文件一个字节都不会变；append 写法
    则会把那一行写进去（甚至写进去半行）。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    (root / BAD_VERSIONS_FILE).write_text("0.0.9\n", encoding="utf-8")
    _first_failure(root, tmp_path, blacklist=["0.0.9"])
    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)

    with patch.object(launcher, "atomic_write", side_effect=OSError("磁盘满了")):
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    # 逐字节比对，不是 _blacklisted()：原子写的实现下文件一个字节都不会变，而 append
    # 写法会留下多出来的（甚至半截的）一行。
    assert (root / BAD_VERSIONS_FILE).read_text(encoding="utf-8") == "0.0.9\n"  # 没被动过


def test_previous_txt_is_dropped_once_it_has_been_consumed(tmp_path: Path):
    """回滚之后 previous.txt 就该没了：它的语义是"能回滚到哪儿"，而现在 current 已经
    **就是**它了。留着它 = 一个指向自己的假回滚目标，正是这个代码库反复警告的那类谎言。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    assert not (root / "previous.txt").exists()


def test_the_bad_version_is_blacklisted_before_current_is_switched(tmp_path: Path):
    """顺序不能反。先切 current.txt、还没来得及写 bad_versions.txt 就断电的话：机器
    回到了旧版本（能用），但坏版本没被拉黑 → 下次开机更新器看见通道里那个"更新的"
    版本、又下 895MB、又装、又崩……每次开机重演一遍，把爸妈的带宽烧光。

    先拉黑、再切：断在中间最坏也只是"还停在坏版本上，但它已经被拉黑了"——下次开机
    启动器再崩一次、再回滚一次就收敛了，而更新器绝不会再把它装回来。

    钉法：只让 current.txt 的那一次原子写失败（bad_versions.txt 的照常写）。顺序反了
    的话，current.txt 先炸 → 根本走不到拉黑那一步 → bad_versions.txt 不存在。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)
    real_atomic_write = launcher.atomic_write

    def only_current_txt_fails(path: Path, data: bytes) -> None:
        if path.name == "current.txt":
            raise OSError("断电")
        real_atomic_write(path, data)

    with patch.object(launcher, "atomic_write", side_effect=only_current_txt_fails):
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert _blacklisted(root) == ["0.1.1"]
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"  # 切换没成功


# --------------------------------------------------------------------------
# 绝不回滚（误判一次好版本 = 静默地把这台机器钉死在旧版本上）
# --------------------------------------------------------------------------


def test_relaunching_an_already_running_app_never_rolls_back(tmp_path: Path):
    """爸妈双击了图标、没立刻看见窗口，于是**又双击一下**——这是长辈用电脑最常见的
    动作。Electron 应用普遍带单实例锁：第二个实例会立刻以 **0** 退出、把已有窗口
    切到前台。

    这个"瞬间以 0 退出"和"启动即崩"在退出码/存活时间上**完全一样**。靠它们区分不了，
    只能靠"这个版本之前活过健康窗口吗"（last_good.txt）：活过 = 不是新版本引入的故障,
    一次都不许回滚。否则每台机器的每次自动更新之后，只要长辈手快双击两下，就会静默
    回滚 + 永久拉黑一个完全正常的版本。

    双击**两次**（长辈没看见窗口是会一直点的）：旁证机制（CORROBORATING_FAILURES）
    只是第二道网，这一条钉的是第一道——被证明健康过的版本，一次都不许算作启动失败。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", last_good="0.1.1")
    spawn, calls = _fake_spawn(INSTANT_EXIT_0)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert len(calls) == 2  # 两次都只启动了 current，没有回滚
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()


def test_a_proven_version_crashing_today_is_not_rolled_back(tmp_path: Path):
    """跑了几个月的版本今天崩了（杀软吃掉了一个 DLL、data/ 坏了）。这不是"新版本
    启动即崩"，回滚治不了它——但回滚**会**把一个好版本永久拉黑。响亮地失败（非零
    退出码 + 日志），把机器留给「小助手修复」和维护者。

    崩**两次**（长辈会反复双击）：旁证机制只是第二道网，这一条钉的是第一道。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", last_good="0.1.1")
    spawn, calls = _fake_spawn(INSTANT_CRASH)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0
    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 2  # 两次都只启动了 current，没有回滚
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()


def test_the_user_closing_the_window_is_not_a_crash(tmp_path: Path):
    """新版本第一次启动，爸妈打开、问了一句、20 秒后自己关掉了。窗口起来过、用得好
    好的——这不是崩溃。把"健康窗口内的任何退出"一律判成崩溃的话，这一次正常使用就会
    静默回滚 + 永久拉黑一个好版本。

    区分点是**存活时间**：启动崩溃发生在窗口画出来之前（几秒内）；人要看见窗口、
    再移到叉号上点一下，怎么也得好几秒。启动窗口之后的 0 退出 = 用户自己关的。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")  # 没有 last_good：这是它第一次启动
    spawn, calls = _fake_spawn((0, 0.3))  # 活过启动窗口(0.2s)，在健康窗口(0.6s)内以 0 退出

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn,
               health_seconds=0.6, startup_seconds=0.2) == 0

    assert len(calls) == 1
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()


def test_a_second_double_click_during_the_health_window_launches_nothing(tmp_path: Path):
    """健康窗口里启动器阻塞等着（30 秒）。爸妈这时又双击了一下 → 第二个启动器进程。

    没有排他锁的话，第二个启动器会再 spawn 一个桌面端；单实例锁让它瞬间以 0 退出；
    而此刻这个版本**还没**被记成 healthy（第一个启动器还在等）——于是第二个启动器
    判定"启动即崩"，把一个完全正常的新版本回滚掉 + 永久拉黑。每次自动更新之后都可能
    发生，而且**完全静默**。

    锁是 OS 级的（进程消失时内核自动释放），不会像标记文件那样在断电后留下永久残骸。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    nested: list[int] = []

    def spawn(argv, env):
        return _Reentrant(root, tmp_path, nested)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert nested == [0]  # 第二个启动器安静让路
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()


class _Reentrant:
    """第一个启动器的假桌面端：它在"等健康窗口"的当口，让第二个启动器整个跑一遍
    （复现爸妈双击第二下）。第二个启动器必须拿不到锁、什么都不 spawn。"""

    def __init__(self, root: Path, tmp_path: Path, nested: list[int]):
        self._root, self._tmp, self._nested = root, tmp_path, nested

    def wait(self, timeout=None):
        def must_not_spawn(argv, env):  # pragma: no cover - 跑到这里就是 bug
            raise AssertionError("第二个启动器不该再 spawn 一个桌面端")

        self._nested.append(
            run(self._root, _ws(self._tmp), exe_argv=["fake"], spawn=must_not_spawn, **FAST)
        )
        raise subprocess.TimeoutExpired("fake-desktop", timeout)


# --------------------------------------------------------------------------
# 没有回滚目标：不猜、不装死
# --------------------------------------------------------------------------


def test_crash_without_previous_does_not_guess_a_rollback_target(tmp_path: Path):
    """previous.txt **可能不存在**——那是更新器有意为之的语义（诚实地表示"无回滚
    目标"，好过一个看着有效的假目标）。此时不能装死（报 0），也不能拿 versions/ 下
    随便一个目录当回滚目标（那正是"编造一个假回滚目标"）。

    响亮地失败：非零退出码 + 日志。爸妈会看见"双击了没反应"然后打电话——很吵，但
    维护者因此知道了。静默返回 0 才是真正的灾难。"""
    root = _root(tmp_path, "0.1.1", None, versions=("0.1.0", "0.1.1"))
    _first_failure(root, tmp_path)  # 旁证够了，挡住它的只能是"没有回滚目标"
    spawn, calls = _fake_spawn(INSTANT_CRASH)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 1  # 没有第二次启动：没东西可回滚
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"  # 什么都没改
    assert not (root / BAD_VERSIONS_FILE).exists()  # 没有回滚就没有拉黑的意义
    assert (root / "versions" / "0.1.0").is_dir()  # 更没有删掉任何东西


def test_previous_pointing_at_a_missing_dir_is_not_a_rollback_target(tmp_path: Path):
    """previous.txt 指向一个磁盘上根本不存在的版本目录（被剪掉了、被长辈删了）。
    照着它回滚 = current.txt 指向虚空 = 这台机器**再也起不来了**，比原地崩着还糟。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", versions=("0.1.1",))  # 0.1.0 目录不在
    _first_failure(root, tmp_path)  # 旁证够了，挡住它的只能是"回滚目标是假的"
    spawn, calls = _fake_spawn(INSTANT_CRASH)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 1
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()


def test_missing_current_txt_is_not_reported_as_success(tmp_path: Path):
    """current.txt 丢了（断电写到一半、磁盘错误、人手删）——不知道该启动哪个版本。
    绝不猜（versions/ 下挑一个"看起来最新的"就是编造）。非零退出 + 日志。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    (root / "current.txt").unlink()
    spawn, calls = _fake_spawn(HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0
    assert calls == []


# --------------------------------------------------------------------------
# 出厂状态校对（builder/paths.py 的契约要求）
# --------------------------------------------------------------------------


def test_factory_state_is_reconciled_before_the_desktop_starts(tmp_path: Path):
    """契约要求（不是建议）：每个会切版本的进程，都必须在自己每次运行的开头做一次
    出厂状态校对。更新器的出厂状态是在提交点**之后**应用的——它失败过一次，机器就停在
    "新代码 + 旧 persona"上。启动器是纵深防御的第二道。

    而且必须在 spawn **之前**：桌面端起来之后才去改它正在读的 config.yaml/SOUL.md，
    等于什么都没修（Hermes 已经把旧的读进内存了）。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", factories=True)
    workspace = _ws(tmp_path)
    seen: list[str] = []

    def spawn(argv, env):
        seen.append((root / "data" / "SOUL.md").read_text(encoding="utf-8"))
        return _FakeProc(None, 0.0)

    assert run(root, workspace, exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert seen == ["我是小助手 0.1.1"]  # spawn 那一刻，出厂状态已经落地了
    assert (root / "data" / FACTORY_STAMP).read_text(encoding="utf-8").strip() == "0.1.1"


def test_rollback_re_renders_config_from_the_old_versions_factory_master(tmp_path: Path):
    """回滚只切代码，而 data/config.yaml 可能是被那个坏版本写成**新 schema** 的。
    切回旧代码却留着新配置 = 旧代码读不懂自己的配置 = 回滚之后照样起不来，而我们还
    报了成功。

    所以回滚之后必须用**旧版本的**出厂母版把 config.yaml / SOUL.md / 出厂技能重新
    渲染一遍。（用旧版本的母版，不是新版本的——那正是我们刚刚判定为坏的那个。）"""
    root = _root(tmp_path, "0.1.1", "0.1.0", factories=True)
    workspace = _ws(tmp_path)
    data = root / "data"
    data.mkdir()
    (data / "SOUL.md").write_text("我是小助手 0.1.1", encoding="utf-8")
    (data / "config.yaml").write_text('新 schema: 旧代码读不懂\n', encoding="utf-8")
    (data / FACTORY_STAMP).write_text("0.1.1", encoding="utf-8")
    _first_failure(root, tmp_path)

    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)
    assert run(root, workspace, exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.0"
    loaded = yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["model"]["default"] == "0.1.0"  # 用旧版本的母版重渲染过了
    assert loaded["terminal"]["cwd"] == str(workspace)
    assert (data / FACTORY_STAMP).read_text(encoding="utf-8").strip() == "0.1.0"


def test_rollback_re_renders_config_even_when_the_stamp_says_it_need_not(tmp_path: Path):
    """戳（data/.factory_version）只记录 **apply_factory_state 写过什么**，它对"那个坏版本
    在运行时自己改写了 data/config.yaml"一无所知。

    复现（一个坏包同时踩中两件事，这很常见——它本来就是个坏包）：更新器切到 0.1.1 之后，
    应用出厂状态那一步失败了（0.1.1 的出厂母版本身就是坏的），所以**戳还停在 0.1.0**。
    0.1.1 跑起来把 config.yaml 改成了新 schema，然后崩了。现在回滚到 0.1.0——而戳**已经**
    是 0.1.0 了：一次"戳对得上就跳过"的校对会**直接跳过**，留下一份旧代码读不懂的新 schema
    配置。回滚之后照样起不来，而我们还报了成功。

    所以回滚之后那一次校对必须是**无条件**的：戳只记录 apply_factory_state 写过什么，它对
    "那个坏版本在运行时自己改写了 config.yaml"一无所知。（apply_factory_state 幂等，多铺
    一遍的代价只是几个文件。）"""
    root = _root(tmp_path, "0.1.1", "0.1.0", versions=("0.1.0",), factories=True)
    (root / "versions" / "0.1.1" / "factory").mkdir(parents=True)  # 0.1.1 的母版是坏的（空的）
    workspace = _ws(tmp_path)
    data = root / "data"
    data.mkdir()
    (data / "config.yaml").write_text("新 schema: 旧代码读不懂\n", encoding="utf-8")
    (data / FACTORY_STAMP).write_text("0.1.0", encoding="utf-8")  # 戳说"已经是 0.1.0 的出厂状态"
    _first_failure(root, tmp_path)

    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)
    assert run(root, workspace, exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert (data / FACTORY_STAMP).read_text(encoding="utf-8").strip() == "0.1.0"  # 戳没被动过
    loaded = yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["model"]["default"] == "0.1.0"  # 无条件重渲染过了，不是"戳对得上就跳过"
    assert loaded["terminal"]["cwd"] == str(workspace)


def test_previous_pointing_at_current_itself_is_not_a_rollback_target(tmp_path: Path):
    """previous.txt == current.txt（更新器崩在两次写入之间时的合法中间态：它**故意**让
    previous 先落盘，最坏结果就是两者相同）。"回滚"到自己身上什么都改变不了——只会把
    current 拉黑之后再启动同一个坏版本一次，还报成功。"""
    root = _root(tmp_path, "0.1.1", "0.1.1")
    _first_failure(root, tmp_path)  # 旁证够了，挡住它的只能是"回滚目标指向自己"
    spawn, calls = _fake_spawn(INSTANT_CRASH)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 1  # 没有"回滚到自己"再启动一次
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert not (root / BAD_VERSIONS_FILE).exists()


def test_user_data_survives_a_rollback(tmp_path: Path):
    """铁律二：回滚是一次版本切换，绝不能碰用户的东西。激活码没了 = 产品直接变砖，
    而且是静默变砖。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", factories=True)
    data = root / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "sessions" / "chat.jsonl").write_text("聊天记录", encoding="utf-8")
    (data / ".env").write_text("DASHSCOPE_API_KEY=sk-keep-me", encoding="utf-8")
    learned = data / "skills" / "learned" / "SKILL.md"
    learned.parent.mkdir(parents=True)
    learned.write_text("习得技能", encoding="utf-8")
    _first_failure(root, tmp_path)

    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)
    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert (data / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"
    assert (data / "sessions" / "chat.jsonl").read_text(encoding="utf-8") == "聊天记录"
    assert learned.read_text(encoding="utf-8") == "习得技能"


def test_a_broken_factory_master_does_not_block_the_launch(tmp_path: Path):
    """出厂母版被弄坏了（杀软隔离了 SOUL.md）→ 校对收敛不过去。这**绝不能**变成
    "拒绝启动"：出厂状态陈旧的机器还能用（旧 persona/旧配置都是合法的），起不来的
    机器什么都不是。戳没落 → 更新器下次开机会再收敛一次。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")  # 根本没有 factory/ 母版
    spawn, calls = _fake_spawn(HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert len(calls) == 1  # 照样启动了
    assert not (root / "data" / FACTORY_STAMP).exists()  # 没有伪造"已收敛"的凭据
    assert "出厂状态" in (root / "data" / "logs" / "launcher.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 维护者可见性
# --------------------------------------------------------------------------


def test_the_rollback_leaves_a_readable_trace_for_the_maintainer(tmp_path: Path):
    """维护者在一万公里外，没人会看 stderr。"这台机器回滚过"必须在磁盘上留下痕迹，
    ToDesk 进去一眼能看见（心跳遥测是另一个 Task 的事，见 ADR-0004）。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    spawn, _ = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    log = (root / "data" / "logs" / "launcher.log").read_text(encoding="utf-8")
    assert "0.1.1" in log and "0.1.0" in log and "回滚" in log


def test_a_read_only_data_dir_does_not_stop_the_launch(tmp_path: Path):
    """日志写不进去（data/ 被杀软的目录保护锁成只读）绝不能把启动本身弄挂——
    "记不下这件事"比"起不来"轻得多。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    data = root / "data"
    data.mkdir()
    os.chmod(data, 0o500)
    spawn, calls = _fake_spawn(HEALTHY)
    try:
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    finally:
        os.chmod(data, 0o700)
    assert len(calls) == 1


def test_a_missing_install_root_is_not_reported_as_success(tmp_path: Path):
    """路径敲错 / 盘没挂载。绝不能返回 0（"启动成功"），也绝不能凭空长出一棵目录树。"""
    missing = tmp_path / "never-installed"
    spawn, calls = _fake_spawn(HEALTHY)

    assert run(missing, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0
    assert calls == []
    assert not missing.exists()


def test_default_exe_argv_is_built_from_the_layout_contract(tmp_path: Path):
    """启动命令必须由 builder/paths.py 的常量拼出来，而且必须指向**这个版本自己的**目录
    （回滚之后要启动旧版本的 electron，不能还指着刚被拉黑的那个版本目录）。

    ⚠️ 这个默认命令**还没在真机上验证过**（Task 2 的 spike 只确认了桌面端是 Electron，
    ECS 上没有桌面会话，从没实际启动过 GUI）。Task 8 会在装配机上实测。"""
    vdir = tmp_path / "versions" / "0.1.1"
    assert launcher.default_exe_argv(tmp_path, "0.1.1") == [
        str(vdir / ELECTRON_EXE_REL),
        str(vdir / DESKTOP_APP_REL),
    ]


def test_health_window_default_is_not_silently_zero():
    """健康窗口默认值定得住：0 的话每次启动都会立刻"超时"→ 判定成功 → 安全网整个失效。"""
    assert launcher.HEALTH_SECONDS >= 10
    assert 0 < launcher.STARTUP_SECONDS < launcher.HEALTH_SECONDS


# --------------------------------------------------------------------------
# 记账文件读不出来（杀软锁住 / 被写坏）：证据读不出来，绝不能武装一个不可逆的动作
#
# 这正是 updater.py 里已经修过一次的那个 bug 类（"更新器读自己的记账文件那一行在 try
# 外面 → 杀软锁住文件就每次开机崩"）。last_good.txt **每次健康启动都会被重写一遍**，
# 是下一次开机时杀软扫描的头号目标。
# --------------------------------------------------------------------------


def test_an_unreadable_last_good_never_stops_a_healthy_launch(tmp_path: Path):
    """last_good.txt 被杀软锁住（读不出来）。这台机器上的版本**完全没问题**——
    读不出一个记账文件绝不能变成"拒绝启动"：爸妈双击了没反应，而机器好好的。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", last_good="0.1.1")
    (root / LAST_GOOD_FILE).chmod(0o000)
    spawn, calls = _fake_spawn(HEALTHY)

    try:
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    finally:
        (root / LAST_GOOD_FILE).chmod(0o600)

    assert len(calls) == 1  # 桌面端照样起来了


def test_a_corrupt_last_good_never_stops_a_healthy_launch(tmp_path: Path):
    """last_good.txt 被写坏成非 UTF-8（断电写到一半）。read_text() 抛的是
    UnicodeDecodeError——**ValueError 的子类，不是 OSError**：只接 OSError 的话它会一路
    逃出 run()，长辈看到的是"双击了没反应"，而这台机器的版本好好的。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    (root / LAST_GOOD_FILE).write_bytes(b"\xff\xfe 0.1.1")
    spawn, calls = _fake_spawn(HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    assert len(calls) == 1


def test_an_unreadable_last_good_disarms_the_rollback(tmp_path: Path):
    """读不出来的证据只能往**解除武装**的方向倒。last_good 是回滚的军械开关：读不出它
    = 无法证明这个版本没被证明过健康——那就当它**已经**被证明过（不回滚、照常启动）。
    反过来（当作"没证明过"）= 用一个读不出来的文件，武装一个永久拉黑好版本的动作。"""
    root = _root(tmp_path, "0.1.1", "0.1.0", last_good="0.1.1")
    (root / LAST_GOOD_FILE).chmod(0o000)
    spawn, calls = _fake_spawn(INSTANT_EXIT_0)  # 单实例回弹，和"启动即崩"一模一样

    try:
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    finally:
        (root / LAST_GOOD_FILE).chmod(0o600)

    assert len(calls) == 2
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert _blacklisted(root) == []


def test_an_unreadable_previous_is_not_a_rollback_target(tmp_path: Path):
    """previous.txt 读不出来 = 不知道能回滚到哪儿。绝不能猜，更不能拿一个读不出来的文件
    当"有回滚目标"的凭据去拉黑 current。响亮地失败。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    (root / "previous.txt").chmod(0o000)
    spawn, calls = _fake_spawn(INSTANT_CRASH)

    try:
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0
    finally:
        (root / "previous.txt").chmod(0o600)

    assert len(calls) == 1  # 没有回滚
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert _blacklisted(root) == []


def test_a_corrupt_bad_versions_file_never_escapes_as_a_traceback(tmp_path: Path):
    """bad_versions.txt 被写坏成非 UTF-8。拉黑是一次**读-改-写**：读不出旧名单就没法安全
    地改写它（整份重写会把别的坏版本静默洗白）。响亮地失败，机器状态一个字节都不动——
    但绝不能让 UnicodeDecodeError 逃出 run()。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)
    (root / BAD_VERSIONS_FILE).write_bytes(b"\xff 0.0.9\n")
    spawn, calls = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 1  # 没有回滚
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (root / BAD_VERSIONS_FILE).read_bytes() == b"\xff 0.0.9\n"  # 名单没被动过


# --------------------------------------------------------------------------
# 拉黑之前必须有旁证：一次观察不够
# --------------------------------------------------------------------------


def test_one_startup_failure_is_not_enough_to_roll_back(tmp_path: Path):
    """回滚 + 拉黑是**不可逆**的（bad_versions.txt 只增不减，更新器只装比 current 更新的
    版本，没有任何东西会主动告诉一万公里外的维护者）。而"启动即崩"这个观察本身是**会误报**
    的：单实例回弹、长辈手滑把窗口关掉、一次瞬时的杀软锁——都长得一模一样。

    真正坏掉的版本每次双击都会崩；假崩溃不会重演。所以开火前要求两次独立观察，代价只是
    长辈多按一下。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    spawn, calls = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0  # 响亮地失败

    assert len(calls) == 1  # 没有回滚：只观察到一次
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert _blacklisted(root) == []
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"  # 回滚目标还在


def test_a_previous_versions_failure_count_does_not_arm_the_new_version(tmp_path: Path):
    """"两次独立观察"必须是**同一个版本**的两次观察——所以旁证账本是按版本记的
    （`<版本号> <次数>`，见 launcher._version_keyed_int）。

    丢掉那个版本键，上一个版本的前科就会算到新版本头上：0.1.0 崩过一次（瞬时故障，账本上
    留下 "0.1.0 1"），更新器装上 0.1.1，而 0.1.1 第一次出现"瞬间以 0 退出"——很可能只是长辈
    重复双击被单实例锁弹了回来——计数直接凑满 2，**第一次观察就开火**：一个完全正常的新版本
    被回滚 + **永久拉黑**，而这台机器再也装不回它。旁证机制存在的全部意义就此失效。

    （这条是拿掉 `parts[0] != version` 之后唯一会变红的测试：没有它，那行检查删掉，
    140 条测试全绿。）"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    # 上一个版本的前科：0.1.0 在被换掉之前崩过一次。
    (root / STARTUP_FAILURES_FILE).write_text("0.1.0 1\n", encoding="utf-8")
    spawn, calls = _fake_spawn(INSTANT_CRASH, HEALTHY)

    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0  # 响亮地失败

    assert len(calls) == 1  # 没有回滚：0.1.1 自己只被观察到过一次
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert _blacklisted(root) == []  # 好版本没有被拉黑
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"  # 回滚目标还在


def test_a_launch_that_survives_the_startup_window_forgets_the_earlier_failure(tmp_path: Path):
    """0.1.1 崩过一次（瞬时故障：杀软那一刻正锁着某个 DLL）。之后长辈打开它、用了一会儿、
    自己关掉了——窗口画出来过，这个版本在这台机器上**能跑**。那次旧的失败观察必须作废，
    否则它会一直躺在计数器里，等着和几个月后某一次无关的失败凑成"两次"，回滚掉一个每天
    都在用的好版本。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    _first_failure(root, tmp_path)  # 第一次观察

    spawn, _ = _fake_spawn((0, 0.3))  # 活过启动窗口，用户自己关掉（不写 last_good）
    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    spawn, calls = _fake_spawn(INSTANT_CRASH, HEALTHY)
    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert len(calls) == 1  # 计数清零过了：这又是"第一次"，不许回滚
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert _blacklisted(root) == []


def test_a_failed_last_good_write_does_not_arm_the_rollback(tmp_path: Path):
    """**记账失败绝不能给回滚上膛。** 复现（一次瞬时文件锁就够，不需要任何进程异常死亡）：

      启动 1：健康（桌面端在跑），但写 last_good.txt 那一下撞上杀软的锁 → 记不下。
      t=40s：长辈没看见窗口，又双击了一下 → Electron 单实例锁让第二个实例瞬间以 0 退出
              ——退出码和存活时间和"启动即崩"完全一样，而 last_good 是空的。

    只靠 last_good 的话，这里会回滚 + **永久拉黑一个完全正常的版本**。所以还要**观察**，
    而不是推断：上一次活过健康窗口的桌面端 PID 记在磁盘上，它还活着 = 小助手就是在跑。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    real_atomic_write = launcher.atomic_write

    def last_good_is_locked(path: Path, data: bytes) -> None:
        if path.name == LAST_GOOD_FILE:
            raise PermissionError(f"[Errno 13] Permission denied: {path}")
        real_atomic_write(path, data)

    procs: list[subprocess.Popen] = []

    def spawn_a_real_desktop(argv, env):
        proc = launcher._spawn([sys.executable, "-c", "import time; time.sleep(60)"], env)
        procs.append(proc)
        return proc

    try:
        with patch.object(launcher, "atomic_write", side_effect=last_good_is_locked):
            assert (
                run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn_a_real_desktop, **FAST) == 0
            )
        assert not (root / LAST_GOOD_FILE).exists()  # 军械开关没记下——但桌面端在跑

        spawn, calls = _fake_spawn(INSTANT_EXIT_0)  # 长辈的第二下双击：单实例回弹
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()

    assert len(calls) == 1
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert _blacklisted(root) == []  # 好版本没有被拉黑
    assert (root / DESKTOP_PID_FILE).read_text(encoding="utf-8").split() == [
        "0.1.1", str(procs[0].pid)
    ]


def test_the_recorded_pid_is_only_trusted_while_it_is_alive(tmp_path: Path):
    """PID 记录不是"免死金牌"：那个桌面端**已经退出**之后，同一个瞬间以 0 退出的观察就
    真的是启动失败了。留着一条死 PID 当"小助手已经在跑"的证据 = 报告成功、实际什么都没
    启动（本项目抓过 7 次的那类 bug）。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    dead = subprocess.Popen([sys.executable, "-c", ""])  # 立刻退出的进程
    dead.wait()
    (root / DESKTOP_PID_FILE).write_text(f"0.1.1 {dead.pid}\n", encoding="utf-8")

    _first_failure(root, tmp_path, INSTANT_EXIT_0)
    spawn, calls = _fake_spawn(INSTANT_EXIT_0, HEALTHY)
    assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) == 0

    assert len(calls) == 2  # 回滚了：那条 PID 记录是死的，不能拿来免死
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert _blacklisted(root) == ["0.1.1"]


# --------------------------------------------------------------------------
# _pid_alive_windows：Windows 分支此前从未在任何机器上执行过一次
# （之前是 `# pragma: no cover`）。这里把真实的 kernel32 调用换成一个可注入的假对象
# （launcher._win_kernel32），这样就能在非 Windows 机器上驱动 OpenProcess 的三种
# 失败原因，钉住模块 docstring 开头那条定向规则："读不出来的证据只能解除武装，
# 绝不能武装"。
# --------------------------------------------------------------------------


class _FakeKernel32:
    """假 kernel32：只实现 _pid_alive_windows 用到的三个方法。"""

    def __init__(self, open_result: tuple[int, int], exit_code: int | None = None):
        self._open_result = open_result  # (handle, last_error)
        self._exit_code = exit_code
        self.closed: list[int] = []

    def open_process(self, pid: int) -> tuple[int, int]:
        return self._open_result

    def get_exit_code(self, handle) -> int | None:
        return self._exit_code

    def close(self, handle) -> None:
        self.closed.append(handle)


def test_pid_alive_windows_missing_pid_is_no_evidence_of_life():
    """OpenProcess 失败、GetLastError == ERROR_INVALID_PARAMETER（87）：这个 PID
    根本不存在——是一个事实，不是缺证据。放心当"没有它还活着的证据"。"""
    fake = _FakeKernel32(open_result=(0, 87))
    with patch.object(launcher, "_win_kernel32", fake):
        assert launcher._pid_alive_windows(4242) is False


def test_pid_alive_windows_access_denied_disarms_not_arms():
    """OpenProcess 失败、GetLastError == ERROR_ACCESS_DENIED（5）：进程属于**另一个
    Windows 账号**、被**国产杀软 hook 住了 OpenProcess**、或者进程已提权——这三种
    恰恰是这张 PID 网最该起作用的场景。进程可能好好地在跑，我们只是查不动：必须解除
    武装（当成"活着"），绝不能当成"它不在了"去武装一个不可逆的回滚 + 拉黑。

    这是从没在任何机器上执行过的代码里藏着的那个方向错误：旧实现不分青红皂白地把
    "拿不到句柄"一律读成 False，这条测试对着旧实现会红。"""
    fake = _FakeKernel32(open_result=(0, 5))
    with patch.object(launcher, "_win_kernel32", fake):
        assert launcher._pid_alive_windows(4242) is True


def test_pid_alive_windows_unknown_open_process_error_disarms():
    """OpenProcess 失败、GetLastError 是一个没见过的错误码：同样是"查不动"，不是
    "它不在了"——未知失败是不可用证据，按模块定向规则只能解除武装。"""
    fake = _FakeKernel32(open_result=(0, 1450))  # ERROR_NO_SYSTEM_RESOURCES，随便一个陌生码
    with patch.object(launcher, "_win_kernel32", fake):
        assert launcher._pid_alive_windows(4242) is True


def test_pid_alive_windows_unreadable_exit_code_disarms():
    """拿到了句柄，但 GetExitCodeProcess 查不出退出码（句柄失效 / 权限被收回）。
    这同样是查不动，不是"它已经退出"——必须解除武装。"""
    fake = _FakeKernel32(open_result=(1234, 0), exit_code=None)
    with patch.object(launcher, "_win_kernel32", fake):
        assert launcher._pid_alive_windows(4242) is True
    assert fake.closed == [1234]  # 句柄无论如何都必须被关掉


def test_pid_alive_windows_still_active_process_is_alive():
    """拿到句柄、GetExitCodeProcess 报 STILL_ACTIVE：进程真的还活着。"""
    fake = _FakeKernel32(open_result=(1234, 0), exit_code=259)
    with patch.object(launcher, "_win_kernel32", fake):
        assert launcher._pid_alive_windows(4242) is True
    assert fake.closed == [1234]


def test_pid_alive_windows_exited_process_is_not_alive():
    """拿到句柄、GetExitCodeProcess 报一个具体的退出码（不是 STILL_ACTIVE）：
    进程真的已经退出了，不是查不动——这是一份关于"它不在了"的正面证据。"""
    fake = _FakeKernel32(open_result=(1234, 0), exit_code=0)
    with patch.object(launcher, "_win_kernel32", fake):
        assert launcher._pid_alive_windows(4242) is False
    assert fake.closed == [1234]


# --------------------------------------------------------------------------
# 锁：拿不到锁 ≠ 锁文件读写出错
# --------------------------------------------------------------------------


def test_an_io_error_on_the_lock_file_is_not_mistaken_for_another_launcher(tmp_path: Path):
    """"拿不到锁"的语义是"另一个启动器正在跑，它会把活干完"——所以安静退出 **0**。
    而锁文件本身读写出错（盘挂了、I/O 错误）根本不是那回事：那是这台机器坏了。把两者
    混成同一个返回值 = 安静地报告成功、实际什么都没启动。"""
    root = _root(tmp_path, "0.1.1", "0.1.0")
    real_open = Path.open

    class _SeekFails:
        def __init__(self, fh):
            self._fh = fh

        def seek(self, *args, **kwargs):
            raise OSError("[Errno 5] Input/output error")

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def open_but_seek_fails(self: Path, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)
        return _SeekFails(fh) if self.name == LAUNCH_LOCK_FILE else fh

    spawn, calls = _fake_spawn(HEALTHY)
    with patch.object(Path, "open", open_but_seek_fails):
        assert run(root, _ws(tmp_path), exe_argv=["fake"], spawn=spawn, **FAST) != 0

    assert calls == []  # 什么都没启动——而且**没有**报告成功
