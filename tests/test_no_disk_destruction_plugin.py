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
import time
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
    # 冒号后的"终止形状"曾经只认 ASCII 空白（`[ \t]`），中文默认界面下
    # 全角空格（U+3000）、不换行空格（NBSP/U+00A0）在中文输入法/中文软件
    # 生成的文本里很常见，不需要刻意构造就可能出现，曾经因此漏检。
    "format C:　/q",  # 全角空格（U+3000）
    "format C: /q",  # 不换行空格 NBSP（U+00A0）
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


# --------------------------------------------------------- ReDoS 回归测试：同一行任意内容分支必须有长度上限
#
# 独立 review 实测：`_CONNECTOR` 的"同一行任意内容"分支曾经是不设上限的 `.*`。
# 对着 `("vssadmin delete " * n) + "X"` 这种"关键词重复出现、但永远凑不出
# "shadows" 从而永远无法完整匹配"的输入，回溯耗时随 n 呈多项式级增长——
# n=800（约 12.8KB）实测 6~8 秒，推算 51KB 输入要 7+ 分钟。这不需要精心构造
# 的恶意输入，LLM 生成代码进入"重复退化"这种真实失败模式就可能意外撞上，
# 而 `pre_tool_call` 钩子同步挡在每次 `terminal`/`execute_code` 调用之前，
# 卡死几秒到几十分钟对不懂技术的用户来说就是"软件坏了"。
#
# 加上 256 字符的长度上限（`.{0,256}`）后，同样的对抗输入应该在两位数毫秒内
# 出结果——用一个远超真实阈值的时间预算（2 秒）做断言，既能在回归发生时
# 可靠失败，也不会因为跑测试的机器一时慢一点就误报（真实耗时通常在
# 100ms 以内，2 秒的预算留了充分余量）。


@pytest.mark.parametrize("rule_name,repeated_unit,never_completes", [
    # vssadmin/wbadmin 是三词规则，cipher 是两词规则——重复单元里少一个词，
    # 但对抗输入的结构一样：不断重复"能起头却凑不齐"的前缀，最后拼一个绝不
    # 会让规则收尾的 "X"。cipher 的连接符结构跟另两条相同，一并覆盖。
    ("vssadmin", "vssadmin delete ", "shadows"),
    ("wbadmin", "wbadmin delete ", "catalog"),
    ("cipher", "cipher ", "/w"),
])
def test_redos_guard_polynomial_backtracking_input_stays_fast(plugin, rule_name, repeated_unit, never_completes):
    """重复关键词但永远凑不齐完整匹配的对抗输入，不应该让正则回溯耗时随输入
    长度失控增长。n=3200（约 51KB，对应独立 review 报告里最严重的那个规模）
    在修复后的实测耗时是两位数毫秒，这里用 2 秒的宽松预算防止未来回归。"""
    n = 3200
    command = (repeated_unit * n) + "X"
    assert never_completes not in command  # 收尾词永不出现，规则本来就凑不齐完整匹配

    start = time.perf_counter()
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    elapsed = time.perf_counter() - start

    assert result is None  # 凑不出完整命令，本来就不该拦
    assert elapsed < 2.0, f"{rule_name} 规则回溯耗时 {elapsed:.2f}s，疑似 ReDoS 回归（应在两位数毫秒内完成）"


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


# =================================================== 已知限制（KNOWN LIMITATION）回归测试
#
# 下面几条测试断言的是"当前的实际行为"，不是"期望的行为"——都是 review
# 发现、维护者判断为可以维持现状不修的取舍（漏报或误伤都有；前三条是本轮
# 修复之前就存在的残留问题，第四条是本轮 ReDoS 修复本身引入、经权衡后接受
# 的代价），没有配套测试锁定的话，容易被以后的改动无意间加重，或者被人
# 误以为是 bug 顺手"修复"掉而没人注意到这其实是权衡后的既有决策。测试名统一带
# `test_known_limitation_` 前缀，docstring 里明确写"这是已知限制，不是要求
# 修复的目标"，看到失败时不要直接改代码让它变绿，先确认是不是这条限制本身
# 被有意收紧/放宽了。


def test_known_limitation_vssadmin_comma_enumeration_prose_is_false_positive(plugin):
    """已知限制（不要求修复）：`vssadmin`/`delete`/`shadows` 顺序出现在一句
    自然语言散文里（不是真实命令），也会被误判拦截。这是"同一行内任意内容"
    连接方式本身固有的精度局限——`_CONNECTOR` 无法区分"这是被标点隔开的
    真实命令参数"还是"这只是一句提到这几个词的解释性文字"。维护者判断为
    可以接受的取舍（优先不漏报真实破坏命令），这条测试只是记录现状。"""
    command = "vssadmin, delete, shadows are three related concepts"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None and result["action"] == "block"  # 现状：会被误伤


def test_known_limitation_cipher_explanatory_prose_is_false_positive(plugin):
    """已知限制（不要求修复）：一段解释"cipher /w"这个命令语法本身的说明性
    文字（比如教程/文档/agent 转述），只要字面上原样出现了"cipher"和"/w"，
    也会被判定为命中——规则本身就是按字面语法形状匹配，不理解"这是在引用
    命令语法"还是"这是在真的执行它"。记录现状，不要求修复。"""
    command = (
        "This paragraph is unrelated to Windows EFS cipher /w secure erase; "
        "it is just explanatory text about what that command syntax means."
    )
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None and result["action"] == "block"  # 现状：会被误伤


def test_known_limitation_caret_line_continuation_bypasses_vssadmin_rule(plugin):
    """已知限制（不要求修复）：cmd.exe 批处理脚本用 `^` 续行符换行，
    `_LINE_SPLIT_GLUE` 的胶水字符类没有包含 `^`，所以这种跨行拆分方式目前
    不会被拦——是真实存在的绕过手法，但维护者判断这次不在范围内，留给
    以后的任务处理。这条测试记录现状（漏报），不代表这是期望行为。"""
    command = "vssadmin ^\ndelete ^\nshadows /all /quiet"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is None  # 现状：会漏过


def test_known_limitation_long_same_line_padding_bypasses_connector_bound(plugin):
    """已知限制（不要求修复，本轮 ReDoS 修复本身带来的、经权衡后接受的代价）：
    `_CONNECTOR` 的同一行分支从无界 `.*` 收紧成 `.{0,256}` 后，如果关键词
    之间塞了超过约 256 个任意字符的填充物，会因为超出长度上限、又不是
    "跨行胶水字符"而匹配失败——旧的无界版本会不管填充多长都命中。这是
    修掉灾难性回溯必须付出的代价（无法同时做到"完全不限长度"和"不会
    在对抗输入上回溯爆炸"），真实的 vssadmin 命令语法里两个关键词间从不会
    隔这么远，记录现状，不代表这是需要堵上的漏洞。"""
    command = "vssadmin " + ("A" * 300) + " delete shadows"
    result = plugin._on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is None  # 现状：填充物太长会漏过（收紧长度上限前会命中）


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
