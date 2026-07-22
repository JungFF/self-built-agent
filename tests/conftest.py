# tests/conftest.py
"""跨平台地把文件/目录**真的**锁住，供"杀软锁住了它"这类用例使用。

为什么需要这一层：这批用例模拟的是中国杀软套装把 data/ 里的东西**锁住**（而不是删掉）
——那是这个产品最真实、也最难查的一类故障。原来的写法直接用 `os.chmod(path, 0)`，
在 POSIX 上确实能让文件读不出来，但 Windows 的 chmod **只切换只读属性**：

    os.chmod(file, 0)      → Windows 上文件照样读得出来
    os.chmod(dir, 0o500)   → Windows 上目录照样写得进去

2026-07-21 整套测试第一次在 Windows 上跑才暴露这件事。后果不是"测试失败"那么简单：
在 Windows 上这些用例**根本没在测它们声称要测的东西**——造不出故障，走的是正常路径，
断言自然对不上。而 Windows 是这个产品唯一的交付平台，也就是说这批最贴近真实事故的
用例，恰恰在唯一要跑的平台上是静默失效的。

Windows 上真正能造出"拒绝访问"的是 ACL：`icacls /deny` 之后 Python 拿到的是货真价实的
PermissionError，跟杀软锁住时抛的是同一个异常。注意这一招对**提权**进程无效
（管理员绕过 ACL），所以测试要在普通权限下跑——那也正是爸妈机器上的真实身份。
"""
import contextlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

_WINDOWS = os.name == "nt"


def _me() -> str:
    """当前用户的 `域\\用户名`，icacls 用它指定 ACL 条目的主体。"""
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}"


def _icacls(*args: str) -> None:
    # capture_output：icacls 会往 stdout 印一行 "Successfully processed 1 files"，
    # 让它污染 pytest 的输出没有意义；check=True 保证 ACL 没设上时用例响亮地失败，
    # 而不是"以为锁住了、其实没锁"——那正是这个模块要消灭的那种静默。
    subprocess.run(["icacls", *args], check=True, capture_output=True)


@contextlib.contextmanager
def denied_read(path: Path) -> Iterator[None]:
    """让 path 真的读不出来（读它抛 PermissionError），退出时恢复。"""
    if _WINDOWS:
        # 只拒 RD（FILE_READ_DATA，"读内容"），不能用通用的 (R)：(R) 连 READ_CONTROL
        # 一起拒掉，等于把自己读 ACL 的权限也拒了，退出时那句 /remove:d 会以
        # Access denied 失败——用例本体明明过了，却栽在清理上（2026-07-21 亲测）。
        _icacls(str(path), "/deny", f"{_me()}:(RD)")
        try:
            yield
        finally:
            _icacls(str(path), "/remove:d", _me())
    else:
        original = path.stat().st_mode
        os.chmod(path, 0)
        try:
            yield
        finally:
            os.chmod(path, original)


@contextlib.contextmanager
def denied_write(path: Path) -> Iterator[None]:
    """让 path（通常是一个目录）真的写不进去，退出时恢复。

    对应杀软的"目录保护"：目录还在、读得到，就是不让往里写。
    """
    if _WINDOWS:
        _icacls(str(path), "/deny", f"{_me()}:(W)")
        try:
            yield
        finally:
            _icacls(str(path), "/remove:d", _me())
    else:
        original = path.stat().st_mode
        os.chmod(path, 0o500)
        try:
            yield
        finally:
            os.chmod(path, original)
