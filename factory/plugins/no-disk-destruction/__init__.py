# factory/plugins/no-disk-destruction/__init__.py
"""Hermes 官方插件：拦截 Windows 磁盘/分区级破坏命令。

**解决什么问题**：Hermes 自带的危险命令检测（`tools/approval.py` 的
`DANGEROUS_PATTERNS`）不覆盖磁盘/分区级破坏操作——`format`、`diskpart`、
PowerShell 的 `Clear-Disk`/`Remove-Partition`/`Format-Volume`、
`vssadmin delete shadows`（删光系统还原点/卷影备份）、`wbadmin delete catalog`
（删光备份目录）、`cipher /w`（安全擦除可用空间，让已删数据不可恢复）、
`bcdedit`（改写动机配置）。这些命令一旦被执行（无论是任务里的误判，还是被
拖入的文件里藏的 prompt injection 诱导），没有任何撤销手段。

**为什么直接 block、不做 approve 弹窗**：这个产品面向完全不懂技术的老年
用户。一个"是否继续格式化 D 盘？"的确认弹窗对这个用户群体形同虚设——他们
没有能力判断这个操作意味着什么，大概率会直接点"是"。所以这里的默认动作是
直接拒绝执行，同时把拒绝原因写进 message，让 agent 能用大白话转述给用户
（"这个操作可能会清空硬盘内容，我不能执行"），而不是把决策权交给一个不
可能做出正确判断的人。

**职责边界**：只扫 `terminal` 工具的 `command` 参数、`execute_code` 工具的
`code` 参数（攻击者可能用 `subprocess.run(["diskpart"])` 这类方式在 Python
代码里间接调用系统命令）。不扫其他工具（`write_file`/`patch`/`read_file`
等）——通用危险内容扫描是 `security-guidance` 插件的职责，这里不重复建设。
"""
import re
from typing import Any

# 工具名 -> 需要扫描的参数名。只覆盖这两个：其余工具不是本插件的职责范围。
_SCANNED_ARG_BY_TOOL = {
    "terminal": "command",
    "execute_code": "code",
}

# (人类可读描述, 原始正则)。描述会出现在拒绝消息里，方便日志/调试定位是哪条规则
# 命中的。统一用 IGNORECASE 编译（cmd.exe/PowerShell 大小写不敏感，攻击者也可能
# 故意变换大小写绕过）。
#
# 下面三条多词规则（vssadmin/wbadmin/cipher）里的连接部分用 `_CONNECTOR`，
# 而不是裸 `.*`（同一行内任意字符，因为没加 DOTALL）。`_CONNECTOR` 是两种
# 匹配方式的"或"：
#   (a) 同一行内任意字符（`.*`，跟原始设计完全一样，不加长度限制）——保留
#       同一行里"关键词之间夹杂任意字符"都应该命中的原有行为，例如
#       `vssadmin.exe delete shadows`、带完整路径的
#       `C:\Windows\System32\vssadmin.exe delete shadows`。这部分从未
#       改过，只是不再是唯一的连接方式。
#   (b) 跨行时只允许"胶水字符"（空白、续行反斜杠/反引号、逗号、引号），
#       且有长度上限（`{0,20}`）——用来接住关键词被拆到相邻几行的场景
#       （bash `\` 续行、PowerShell `` ` `` 续行、Python list 字面量换行、
#       或者干脆按空格拆成几行），但不允许中间夹真正的字母/汉字。
#
# 为什么不能换成更简单的连接写法（下面几种更简单的等价尝试都试过，各有各的漏洞）：
#   - 只用 (a) 但去掉长度限制、只留"同一行"这个约束（本插件更早一版的
#     写法）：`vssadmin`/`wbadmin` 这类多词规则一旦被换行拆开就完全逃过
#     检测——`.` 不跨行，跨行的调用直接漏检。
#   - 只用 (a) 但放开跨行（即整体加 DOTALL）：`delete`/`shadows`/`catalog`
#     都是高频英文词，会把一段完全无关的多行脚本/注释/文档误判成破坏命令
#     ——这是本插件早期版本出过的真实 bug，下面 3 条回归测试
#     （test_{vssadmin,wbadmin,cipher}_rule_does_not_span_unrelated_lines）
#     就是复现并锁定这个 bug 不会重现的护栏。
#   - 只用 (b)（把它当成唯一的连接方式，同一行也套用"只许胶水字符"的
#     限制）：会让 `vssadmin.exe delete shadows` 这类同一行、但关键词之间
#     夹着真实字符（`.exe`）的调用误判成"没命中"——这是本插件上一版修复
#     跨行绕过时引入的真实回归，被第二轮独立 review 用这个例子实测复现。
#   - 单纯的"长度上限、字符不限"（`[\s\S]{0,N}`，不区分同一行还是跨行）：
#     看似两头兼顾，实测会漏一种更隐蔽的情况——一段简短的、每行一个关键词
#     的无关注释/文档（比如"备份小工具说明\nvssadmin\n可以 delete\n旧的
#     shadows"），关键词间隔本来就短（跟真实换行拆分的命令同一个量级），
#     纯粹靠长度分不出"这是被拆开的命令"还是"这是一句话被换行印成几行"
#     ——这两种情况的本质区别不是间隔多长，而是跨行时间隔里有没有"真正的
#     词"，所以只有跨行部分才限制成胶水字符，同一行不受此限制。
_LINE_SPLIT_GLUE = r"[\s\\,\"'`]{0,20}"
_CONNECTOR = r"(?:.*|" + _LINE_SPLIT_GLUE + r")"

_RULES: list[tuple[str, str]] = [
    # 裸 \bformat\b 会跟 format_output、"字符串格式化" 这类正常代码/自然语言
    # 撞车，产生大量误报——必须限定为"format 后跟一个盘符"这个真实语法形状。
    # 仅要求盘符+冒号还不够：老年用户产品里 agent 生成"格式 A: PDF 还是
    # B: Word"这类枚举句式极其自然，`[a-zA-Z]:` 会把选项字母误判成盘符。
    #
    # 用正向枚举"盘符冒号后允许出现什么"，而不是负向排除"不允许出现字母
    # 数字"——两种都试过，负向排除版本（`(?!\s*[a-zA-Z0-9])`）有两个真实
    # bug：(1) `\s` 本身就包含换行，排除条件会"看到下一行"，导致
    # `format D:\nshutdown /s` 这种盘符命令独占一行、后面另起一行接普通
    # 命令的真实破坏性写法完全漏检；(2) 只排除了字母/数字，圆括号、引号、
    # Markdown 强调符、破折号这些标点毫无阻拦——"format A: (PDF) 还是
    # B: (Word)"、"format A: **PDF** 还是 B: **Word**"这类换了个标点包装的
    # 枚举句式照样被误伤，而这正是这条规则本来要修的问题。
    #
    # 正向枚举把"允许出现什么"限定到真实语法会出现的几种终止形状：直接跟
    # 路径分隔符（`C:\`）、空白后跟 `/` 开头的参数（`C: /q`）、空白后跟
    # shell 分隔符（`&`/`;`/`|`/`<`/`>`，覆盖 `format D: && echo done`、
    # `format D:; shutdown /s` 这类用分隔符串联的单行命令）、换行——`\r?\n`
    # 而不是裸 `\n`，兼容 Windows 原生的 CRLF 换行（覆盖 `format D:\n<下一条
    # 命令>` 这种独占一行的写法，不管文件是 LF 还是 CRLF 结尾）、或者命令
    # 到此为止。只要盘符冒号后（跳过可选的空格/制表符）紧跟的不是这几种
    # 之一，就判定为普通文字而不匹配——不管后面紧跟的是普通单词还是任意
    # 标点。
    ("format 磁盘格式化", r"\bformat\b\s+[a-zA-Z]:(?:[\\/]|[ \t]*(?:[/&;|<>]|\r?\n|$))"),
    ("diskpart", r"\bdiskpart\b"),
    ("PowerShell Clear-Disk（清空磁盘）", r"\bclear-disk\b"),
    ("PowerShell Remove-Partition（删除分区）", r"\bremove-partition\b"),
    ("PowerShell Format-Volume（格式化卷）", r"\bformat-volume\b"),
    # 真实语法是 "vssadmin delete shadows ..."：delete 与 shadows 顺序固定，
    # 但中间可能夹着同一行的其他字符，或被拆到相邻几行，不要求三个词紧邻
    # （只要求中间是 `_CONNECTOR`，见上面的说明）。只有 list/query 之类非
    # delete 的子命令不会命中，避免把纯查询操作也拦下来。
    ("vssadmin delete shadows（删除卷影副本/系统还原点）", r"\bvssadmin\b" + _CONNECTOR + r"\bdelete\b" + _CONNECTOR + r"\bshadows\b"),
    ("wbadmin delete catalog（删除备份目录）", r"\bwbadmin\b" + _CONNECTOR + r"\bdelete\b" + _CONNECTOR + r"\bcatalog\b"),
    ("cipher /w（安全擦除可用空间）", r"\bcipher\b" + _CONNECTOR + r"/w\b"),
    ("bcdedit（启动配置）", r"\bbcdedit\b"),
]

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (description, re.compile(pattern, re.IGNORECASE)) for description, pattern in _RULES
]


def _matched_rule(text: str) -> str | None:
    """扫描一段命令/代码文本，命中就返回规则描述，否则返回 None。"""
    for description, pattern in _PATTERNS:
        if pattern.search(text):
            return description
    return None


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, str] | None:
    arg_name = _SCANNED_ARG_BY_TOOL.get(tool_name)
    if arg_name is None or not isinstance(args, dict):
        return None

    text = args.get(arg_name)
    if not isinstance(text, str) or not text:
        return None

    rule = _matched_rule(text)
    if rule is None:
        return None

    return {
        "action": "block",
        "message": (
            f"这个操作已被拦截（命中规则：{rule}）。这类命令会永久删除磁盘数据、"
            "系统还原点或备份，一旦执行无法恢复，所以直接拒绝，不提供确认继续的"
            "选项。如果确实需要做磁盘/分区级操作，请联系维护者手动处理。"
        ),
    }


def register(ctx) -> None:
    """插件入口：Hermes 加载插件时调用，注册 pre_tool_call 钩子。"""
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
