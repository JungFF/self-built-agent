# tests/test_no_disk_destruction_plugin.py
"""no-disk-destruction 插件的正则/钩子行为——不依赖 Hermes 运行时，直接把
factory/plugins/no-disk-destruction/__init__.py 当模块加载测试内部函数。

插件目录名带连字符（跟 Hermes 官方插件目录的命名习惯一致，比如
plugins/security-guidance/），不是合法的 Python 标识符，没法用点号 import
（`import factory.plugins.no-disk-destruction` 是语法错误）。用
importlib.util.spec_from_file_location 按文件路径直接加载——这也正是插件宿主
在运行时发现/加载插件目录的方式，测试这样导入更贴近真实加载路径，而不是靠
临时改名一个测试专用副本。

⚠️ 加载时必须关掉字节码缓存（sys.dont_write_bytecode）：普通 import 会在源码
旁边写一个 __pycache__/*.pyc。这里加载的是仓库里 factory/ 源目录（会被
builder.factory.render_factory 原样 copytree 进出厂母版），一旦写出
__pycache__，下一次打包就会把它一起带进发行包——这正是本文件当初触发
test_release.py 那条"payload 里不许有 __pycache__"断言失败的原因。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INIT = ROOT / "factory" / "plugins" / "no-disk-destruction" / "__init__.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("no_disk_destruction_plugin", PLUGIN_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.fixture(scope="module")
def plugin():
    return _load_plugin()


# ------------------------------------------------------------------ 正例：磁盘级破坏命令必须被拦

DANGEROUS_COMMANDS = [
    "format D: /q /y",
    "powershell -Command Clear-Disk -Number 1 -RemoveData",
    "diskpart",
    "Remove-Partition -DiskNumber 1 -PartitionNumber 2",
    "Format-Volume -DriveLetter D",
    "vssadmin delete shadows /all /quiet",
    "wbadmin delete catalog -quiet",
    "cipher /w:C",
    "bcdedit /set {default} recoveryenabled No",
]


@pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
def test_dangerous_commands_are_blocked(plugin, command):
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None
    assert result["action"] == "block"
    assert result["message"]  # 非空的中文说明，agent 要能转述给用户


# ------------------------------------------------------------------ 反例：正常命令/代码不能被误伤

SAFE_COMMANDS = [
    "python -c \"print('format check ok')\"",
    "echo formatting the report now",
    "format_output(data)",  # 常见函数名，裸 \bformat\b 会误伤
    "格式化输出的函数叫 format_output",
    "vssadmin list shadows",  # 只是查询，不是删除
    "wbadmin get versions",  # 只是查询，不是删除
    "cipher /e /s:C:\\reports",  # 加密目录，正常运维操作，不是安全擦除
]


@pytest.mark.parametrize("command", SAFE_COMMANDS)
def test_safe_commands_pass_through(plugin, command):
    assert plugin._on_pre_tool_call(tool_name="terminal", args={"command": command}) is None


def test_execute_code_tool_is_scanned_too(plugin):
    """攻击者可能用 subprocess/os.system 在 Python 代码里间接调用系统命令。"""
    code = 'import subprocess\nsubprocess.run(["diskpart"])\n'
    result = plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code})
    assert result is not None
    assert result["action"] == "block"


def test_vssadmin_rule_does_not_span_unrelated_lines(plugin):
    """delete/shadows 都是高频英文词，跨行的多词正则会把一段完全无关的脚本/注释
    误判成"vssadmin delete shadows"命令——真实的破坏性调用必须在同一行/同一条
    语句里（比如 "vssadmin delete shadows /all /quiet" 或
    subprocess.run(["vssadmin","delete","shadows",...])），按行匹配足够覆盖
    真实威胁，同时避免这类误伤。"""
    code = (
        "# 上次的技术分享提到过 vssadmin 这个工具\n"
        "if confirmed:\n"
        "    delete(old_file)\n"
        "print('the shadows effect looks great')\n"
    )
    assert plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code}) is None


def test_wbadmin_rule_does_not_span_unrelated_lines(plugin):
    code = (
        "# wbadmin was mentioned in last week's meeting notes\n"
        "if user_confirmed:\n"
        "    delete(temp_file)\n"
        "\n"
        "print('showing the product catalog now')  # 电商场景常见文案\n"
    )
    assert plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code}) is None


def test_cipher_rule_does_not_span_unrelated_lines(plugin):
    code = (
        "# this script implements a simple caesar cipher for a teaching demo\n"
        "def encode(text, key):\n"
        "    return text[::-1]\n"
        "\n"
        "log_path = 'C:/temp/output/w'  # 输出路径，恰好以 /w 结尾\n"
    )
    assert plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code}) is None


def test_execute_code_with_normal_code_passes_through(plugin):
    code = (
        "import openpyxl\n"
        "wb = openpyxl.Workbook()\n"
        "wb.active['A1'] = 'format check'\n"
        "wb.save('out.xlsx')\n"
    )
    assert plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code}) is None


# ------------------------------------------------------------------ 只管 terminal/execute_code，不越界


@pytest.mark.parametrize("tool_name", ["write_file", "read_file", "patch"])
def test_other_tools_are_never_scanned(plugin, tool_name):
    """即便传进同样危险的字符串，write_file/read_file/patch 也不在这个插件的职责
    范围——通用危险内容扫描是 security-guidance 插件的事，这里不重复/不越界。"""
    result = plugin._on_pre_tool_call(
        tool_name=tool_name,
        args={"path": "C:/x", "content": "diskpart", "command": "diskpart"},
    )
    assert result is None


def test_missing_or_malformed_args_does_not_crash(plugin):
    assert plugin._on_pre_tool_call(tool_name="terminal", args=None) is None
    assert plugin._on_pre_tool_call(tool_name="terminal", args={}) is None
    assert plugin._on_pre_tool_call() is None


# ------------------------------------------------------------------ register(ctx)


def test_register_wires_the_pre_tool_call_hook(plugin):
    calls = []

    class FakeCtx:
        def register_hook(self, hook_name, fn):
            calls.append((hook_name, fn))

    plugin.register(FakeCtx())
    assert len(calls) == 1
    hook_name, fn = calls[0]
    assert hook_name == "pre_tool_call"
    result = fn(tool_name="terminal", args={"command": "diskpart"})
    assert result is not None and result["action"] == "block"
