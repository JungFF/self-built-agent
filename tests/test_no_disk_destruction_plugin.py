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
    "format D:\\",  # 盘符冒号后直接跟路径分隔符，也是真实语法
    "format C:",  # 命令到盘符为止，没有更多参数，也应该拦
    # 用 shell 分隔符串联的真实破坏性单行命令：盘符冒号后不是路径分隔符/
    # `/` 参数，而是 `&&`/`;`/`|`/`&`/`>` 这类 shell 分隔符，同样是真实语法，
    # 必须继续命中（枚举"允许哪些后缀"的写法会漏掉这几种）。
    "format D: && echo done",
    "format D:; shutdown /s",
    "format D: | more",
    "format D:& calc.exe",
    "format D: > nul",
    # 盘符命令独占一行，后面另起一行接普通命令——这种写法比 shell 分隔符
    # 串联更常见，之前用负向前瞻实现时曾经漏检（`\s` 本身包含换行，会把
    # "下一行开头是普通单词"误判成"这不是真实命令"）。
    "format D:\nshutdown /s",
    "format C:\ndel important.txt",
    # 同样是"独占一行"，但用 Windows 原生的 CRLF 换行——裸 `\n` 的写法
    # 曾经漏检这个变体（`\r` 既不算路径分隔符/shell 分隔符，也不在
    # `\n` 这个字面量里）。
    "format D:\r\nshutdown /s",
    "powershell -Command Clear-Disk -Number 1 -RemoveData",
    "diskpart",
    "Remove-Partition -DiskNumber 1 -PartitionNumber 2",
    "Format-Volume -DriveLetter D",
    "vssadmin delete shadows /all /quiet",
    "wbadmin delete catalog -quiet",
    "cipher /w:C",
    "bcdedit /set {default} recoveryenabled No",
    # 同一行内、显式 .exe 后缀/完整路径调用，也是真实存在的调用方式（不是
    # 刻意规避检测），必须继续命中。
    "vssadmin.exe delete shadows",
    r"C:\Windows\System32\wbadmin.exe delete catalog -quiet",
    # PowerShell 反引号续行符，跟 bash 反斜杠续行符是同一类绕过手法，
    # PowerShell 又是本文件其他规则（Clear-Disk 等）本来就要覆盖的场景。
    "vssadmin `\ndelete `\nshadows /all /quiet",
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
    # 枚举选项句式：面向不懂技术的老年用户，agent 生成"格式 A: xxx 还是
    # B: yyy"这类提示文案是这个产品的核心场景，不能被 format 规则误伤
    # （冒号后接空格+普通单词，不满足真实 format 命令的语法形状）。
    'print("Please choose a format A: PDF or B: Word")',
    "format the file as A: docx or B: pdf",
    "Please format the report as A: summary or B: detail",
    # 同一句式换个标点包装（括号/引号/Markdown 加粗），负向前瞻版本的实现
    # 只排除了"冒号后紧跟字母数字"，对这些标点毫无阻拦，曾经在这里漏判
    # 成误伤；正向枚举版本按真实语法形状判断，不受标点影响。
    'print("Please choose a format A: (PDF) or B: (Word)")',
    'print("format A: **PDF** or B: **Word**")',
    "format A: 'PDF' or B: 'Word'",
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
    """delete/shadows 都是高频英文词，无界跨行的多词正则会把一段完全无关的
    脚本/注释误判成"vssadmin delete shadows"命令——这几个关键词在这段文本里
    分散得很开（间隔远超过规则允许的有界跨行窗口），真实的破坏性调用
    （即便被拆成相邻几行）不会隔这么远，所以不应该命中。"""
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


# --------------------------------------------------------- 有界跨行"胶水字符"窗口：绕过修复的回归测试
#
# 去掉 DOTALL 后曾经把 `.*` 限定成"只在同一行内匹配"，代价是 vssadmin/wbadmin
# 这类多词规则能被最普通的换行/续行符绕过——`terminal` 工具（本插件最核心的
# 防护对象）用一个 bash 反斜杠续行符就能让 "vssadmin delete shadows /all
# /quiet" 这种真实会被 bash -c 当成单条命令执行的破坏调用逃过检测。下面几条
# 复现这个绕过场景，验证换成有界跨行"胶水字符"窗口（`_LINE_SPLIT_GLUE`，
# 只允许空白/续行反斜杠/逗号/引号，不允许真正的字母/汉字）后不会再被绕过，
# 同时上面 3 条 DOTALL 回归测试依然全部通过。


def test_vssadmin_rule_catches_bash_line_continuation(plugin):
    """普通 bash 续行符（反斜杠 + 换行），不是任何 Python 语法——在 bash -c 里
    会被当成完整的 "vssadmin delete shadows /all /quiet" 执行。"""
    command = "vssadmin \\\ndelete \\\nshadows /all /quiet"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None
    assert result["action"] == "block"


def test_wbadmin_rule_catches_bash_line_continuation(plugin):
    command = "wbadmin \\\ndelete \\\ncatalog -quiet"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None
    assert result["action"] == "block"


def test_vssadmin_rule_catches_python_list_literal_split_across_lines(plugin):
    """攻击者/生成代码把命令拆成跨行的 Python list 字面量，同样应该被拦。"""
    code = (
        "import subprocess\n"
        "subprocess.run([\n"
        '    "vssadmin",\n'
        '    "delete",\n'
        '    "shadows",\n'
        '    "/all",\n'
        "])\n"
    )
    result = plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code})
    assert result is not None
    assert result["action"] == "block"


def test_vssadmin_rule_catches_command_split_by_plain_newlines(plugin):
    """就算只是简单地把命令按空格拆成几行（不用续行符），也应该被拦。"""
    command = "vssadmin\ndelete\nshadows /all /quiet"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None
    assert result["action"] == "block"


def test_cipher_rule_catches_bash_line_continuation(plugin):
    command = "cipher \\\n/w:C"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None
    assert result["action"] == "block"


# --------------------------------------------------------- 胶水字符类限制：不能只靠长度分辨"拆开的命令"和"分行的无关文本"
#
# 独立 review 用具体例子复现过一种更隐蔽的误伤：一段简短的、每行一个关键词的
# 无关注释/文档字符串，关键词之间的间隔跟真实被拆开的命令是同一个量级（几个
# 字符），单纯的长度上限（`[\s\S]{0,20}`）分不出这两种情况——`_LINE_SPLIT_GLUE`
# 把连接部分限定成"胶水字符"（空白/续行反斜杠/逗号/引号），一旦中间出现真正
# 的字母/汉字（哪怕只有几个字符）就不会命中，靠这个而不是长度来分辨。


def test_vssadmin_rule_does_not_match_short_unrelated_multiline_docstring(plugin):
    """跟上面的绕过测试相比，这里关键词间隔同样很短（几个字符），但间隔里是
    真正的英文词（"can"/"old"），不是语法胶水——不应该被拦。"""
    code = (
        '"""\n'
        "Backup helper notes:\n"
        "vssadmin\n"
        "can delete\n"
        "old shadows if disk is full\n"
        '"""\n'
    )
    assert plugin._on_pre_tool_call(tool_name="execute_code", args={"code": code}) is None


def test_vssadmin_rule_does_not_match_short_unrelated_line_comments(plugin):
    code = (
        "# vssadmin\n"
        "# please delete\n"
        "# old shadows copies only if confirmed\n"
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
