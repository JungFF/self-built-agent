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
# ⚠️ 故意不加 DOTALL：下面三条多词规则（vssadmin/wbadmin/cipher）里的 `.*` 只在
# **同一行内**匹配，不跨行。真实的破坏性调用必然在同一行/同一条语句里（终端命令
# 本身就是单行；execute_code 里也是 subprocess.run(["vssadmin","delete",
# "shadows",...]) 这类单行调用）。如果放开跨行匹配，"delete"/"shadows"/"catalog"
# 这类高频英文词会在一段完全无关的多行脚本/注释/字符串里凑齐顺序，把无辜代码
# 误判成破坏命令（实测过："# vssadmin 相关讨论\ndef xxx(): delete(...)\n# ...
# shadows appear" 这类文本会被跨行版本误伤）——这正是"正则必须避免误伤"这条约束
# 要防的事。
_RULES: list[tuple[str, str]] = [
    # 裸 \bformat\b 会跟 format_output、"字符串格式化" 这类正常代码/自然语言
    # 撞车，产生大量误报——必须限定为"format 后跟一个盘符"这个真实语法形状。
    ("format 磁盘格式化", r"\bformat\b\s+[a-zA-Z]:"),
    ("diskpart", r"\bdiskpart\b"),
    ("PowerShell Clear-Disk（清空磁盘）", r"\bclear-disk\b"),
    ("PowerShell Remove-Partition（删除分区）", r"\bremove-partition\b"),
    ("PowerShell Format-Volume（格式化卷）", r"\bformat-volume\b"),
    # 真实语法是 "vssadmin delete shadows ..."：delete 与 shadows 顺序固定，
    # 但中间可能夹着其他参数，不要求三个词紧邻（只要求同一行）。只有 list/query
    # 之类非 delete 的子命令不会命中，避免把纯查询操作也拦下来。
    ("vssadmin delete shadows（删除卷影副本/系统还原点）", r"\bvssadmin\b.*\bdelete\b.*\bshadows\b"),
    ("wbadmin delete catalog（删除备份目录）", r"\bwbadmin\b.*\bdelete\b.*\bcatalog\b"),
    ("cipher /w（安全擦除可用空间）", r"\bcipher\b.*/w\b"),
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
