# tests/test_factory.py
import ast
from pathlib import Path

import pytest

from builder import paths
from builder.factory import render_factory
from tools.factory_state import assert_factory_complete

ROOT = Path(__file__).resolve().parent.parent


def _config_tmpl() -> str:
    return (ROOT / "factory" / "config.yaml.tmpl").read_text(encoding="utf-8")


@pytest.fixture()
def skills_src(tmp_path: Path) -> Path:
    """装配机上的出厂技能母版（来自 Hermes 快照，Task 7 负责把它并进来）。

    仓库里没有自带技能，所以它只能是 render_factory 的**输入**，不能由 render_factory
    凭空造一个空目录出来（见 render_factory 的 docstring）。
    """
    src = tmp_path / "snapshot-skills"
    (src / "creative" / "ascii-art").mkdir(parents=True)
    (src / "creative" / "ascii-art" / "SKILL.md").write_text("出厂技能", encoding="utf-8")
    return src


def test_render_factory_produces_the_contracts_factory_master(tmp_path: Path, skills_src: Path):
    """母版的目录名和形状由契约（builder/paths.py）定义：versions/<v>/factory/，
    里面必须有 config.yaml.tmpl + SOUL.md + skills/。名字或形状不对 = 每台机器都要
    重新下 895MB、解压 2.87GB、被闸门拒掉、退出 1——每次开机重演一遍。"""
    factory = render_factory(tmp_path / "out", skills_src)
    assert factory.name == paths.FACTORY_DIR_REL
    text = (factory / paths.FACTORY_SOUL).read_text(encoding="utf-8")
    assert "小助手" in text
    assert "铁律" in text
    assert (factory / paths.FACTORY_CONFIG_TMPL).exists()
    assert (factory / paths.FACTORY_SKILLS / "creative" / "ascii-art" / "SKILL.md").exists()
    assert not list(factory.glob("*.txt"))  # 源文件（soul.txt）不进母版


def test_builder_output_passes_the_runtime_gate(tmp_path: Path, skills_src: Path):
    """打包器的产物必须能过运行时那道闸门——**同一个函数**，不是"看起来一样"。

    这条测试是契约与实现之间唯一的连接点：没有它，builder 可以静默地继续产出一个
    每台机器都会拒收的母版（这正是本次返工发现的实况：builder 产 factory_payload/、
    没有 skills/，而 assert_factory_complete 会把它整个拒掉）。"""
    factory = render_factory(tmp_path / "out", skills_src)
    assert_factory_complete(factory)  # 不合格就抛 ValueError


def test_render_factory_is_idempotent(tmp_path: Path, skills_src: Path):
    a = render_factory(tmp_path / "out", skills_src)
    b = render_factory(tmp_path / "out", skills_src)
    assert a == b
    assert (b / paths.FACTORY_SOUL).exists()
    assert (b / paths.FACTORY_SKILLS / "creative" / "ascii-art" / "SKILL.md").exists()


def test_render_factory_refuses_an_empty_skills_master(tmp_path: Path):
    """空的（或不存在的）技能母版必须在**装配机上**就失败——那是维护者盯着看的地方。

    放过去的话，母版形状是"合格"的（skills/ 目录存在），但深度恢复会把机器上的技能
    全删光、再从一个空母版拷 0 个文件回来：一台一个技能都不剩的机器，而 CLI 印的是
    「小助手深度恢复完成！」。"""
    with pytest.raises(ValueError):
        render_factory(tmp_path / "out", tmp_path / "does-not-exist")
    empty = tmp_path / "empty-skills"
    empty.mkdir()
    with pytest.raises(ValueError):
        render_factory(tmp_path / "out", empty)


def test_soul_md_has_no_source_file_header_comment(tmp_path: Path, skills_src: Path):
    """soul.txt 的源文件头注释不能泄漏进 SOUL.md——那会直接进 agent 的 system prompt。"""
    factory = render_factory(tmp_path / "out", skills_src)
    text = (factory / paths.FACTORY_SOUL).read_text(encoding="utf-8")
    assert not text.startswith("#")
    assert text.startswith("你是「小助手」")


def test_config_template_uses_zh_not_zh_hans():
    """display.language 的合法值只有 en/zh/ja/...；写 zh-Hans 会静默回退成英文界面。"""
    tmpl = _config_tmpl()
    assert 'language: "zh"' in tmpl
    assert "zh-Hans" not in tmpl


def test_config_template_has_no_nonexistent_keys():
    """计划里的 security.yolo / file_access / learning 段在 Hermes 里根本不存在。"""
    tmpl = _config_tmpl()
    for bogus in ("file_access:", "learning:", "yolo:"):
        assert bogus not in tmpl


def test_activation_env_key_is_dashscope():
    """激活码走 DASHSCOPE_API_KEY，不是 OPENAI_API_KEY（Hermes 原生支持 DashScope）。"""
    assert paths.ACTIVATION_ENV_KEY == "DASHSCOPE_API_KEY"
    assert "dashscope.aliyuncs.com" in paths.DASHSCOPE_BASE_URL


def test_install_root_has_no_username():
    """固定根不能含用户名——这是整个快照可移植性方案成立的前提。"""
    assert paths.INSTALL_ROOT == r"C:\Users\Public\xiaozhushou"
    assert "Users\\Public" in paths.INSTALL_ROOT


# =============================================================================
# 真机实测回填（2026-07-11，装配机首次端到端跑通）——见 builder/paths.py 模块
# docstring 里"真机实测确认"那一节。
# =============================================================================


def test_tools_and_builder_are_flat_siblings_under_the_version_dir():
    """Finding 1 & 2：tools/ 里每个模块都 `from builder.paths import ...`，所以
    builder/ 必须跟 tools/ **平级**地挂在 versions/<v>/ 下——嵌套（builder 塞进
    tools 内部，或反过来）会让 `cd versions/<v>/ && python -m tools.updater` 里的
    import 直接失败（真机验证过：平级放好时 tools.updater 和 builder.paths 都解析到
    我们自己的文件）。

    两者作为**顶层包名**平级放置，还是 Finding 2 那次撞车（Hermes 自己也有一个顶层
    tools 包）能靠 `cd` 化解的前提——如果 tools/builder 不是版本目录下的顶层平级目录，
    "cd 到版本目录、import 顶层包"这条修复手段根本无从谈起。

    这里钉住的是"平级"这个形状本身：两个常量各自都必须是单层名字（不含路径分隔符），
    互不包含、互不相等。谁把 BUILDER_DIR_REL 悄悄改成 "tools/builder" 之类的嵌套路径
    （或者反过来把 tools 塞进 builder），这条测试立刻炸。
    """
    assert paths.TOOLS_DIR_REL not in ("", ".")
    assert paths.BUILDER_DIR_REL not in ("", ".")
    assert paths.TOOLS_DIR_REL != paths.BUILDER_DIR_REL
    for rel in (paths.TOOLS_DIR_REL, paths.BUILDER_DIR_REL):
        assert "/" not in rel and "\\" not in rel, f"{rel!r} 不是单层名字——嵌套进另一个目录了"

    version_dir = Path("versions") / "0.1.0"
    tools_dir = version_dir / paths.TOOLS_DIR_REL
    builder_dir = version_dir / paths.BUILDER_DIR_REL
    assert tools_dir.parent == builder_dir.parent == version_dir
    assert not builder_dir.is_relative_to(tools_dir)
    assert not tools_dir.is_relative_to(builder_dir)


def _string_literal_fragments(node: ast.expr):
    """从一个 AST 表达式节点里抠出所有会出现在运行时字符串里的字面量片段。

    用 ast.walk 递归整棵子树，不能只看 node 顶层的类型：print() 的参数常见形态是三元
    表达式（`"深度恢复完成" if args.deep else "修复完成"`）或字符串拼接，字面量散在子树
    里，只看顶层会直接漏扫（曾经因此漏过一个藏在三元表达式里的 emoji）。f-string
    （JoinedStr）里的字面量部分本身就是普通的 ast.Constant 子节点，会被一并捕获；
    插值表达式（FormattedValue 里求值的那部分）不是 Constant，不会被收进来——那是
    运行时值，静态扫不出来，也不是这条测试要钉的范围。
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _gbk_unsafe_runtime_strings(source: str, filename: str) -> list[str]:
    """扫描一个模块：print() 的参数、argparse 的 description=/help= 参数，
    把其中 GBK（zh-CN Windows 的默认 stdout 编码）编不了的字符串片段收集出来。"""
    tree = ast.parse(source, filename=filename)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fragments = []
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            for arg in node.args:
                fragments.extend(_string_literal_fragments(arg))
        for kw in node.keywords:
            if kw.arg in ("description", "help"):
                fragments.extend(_string_literal_fragments(kw.value))
        for frag in fragments:
            try:
                frag.encode("gbk")
            except UnicodeEncodeError:
                bad.append(frag)
    return bad


def test_tools_runtime_strings_are_gbk_safe():
    """Finding 3：zh-CN Windows 上 Python 的默认 stdout 编码是 GBK（cp936）。AST 扫描过
    tools/*.py 里全部 print() 与 argparse 的 help=/description= 字符串字面量，确认都是
    纯中文、不含 emoji，GBK 可编码——这几条运行时路径是安全的。

    这条测试把那次扫描钉成回归防线：以后谁往这些运行时会打印/会喂给 argparse 的字符串
    里塞一个 emoji（比如 ⚠️），这里立刻炸，而不是等爸妈的机器在真机上 UnicodeEncodeError。

    模块 docstring / 注释里的 ⚠️ 不在扫描范围内（它们不会被打印或喂给 argparse）——但见
    builder/paths.py 里"待验证（GBK）"那条：Task 8 如果哪天把某个 docstring 直接接给
    argparse 当 description，就会撞上这条测试原本要防的那类真机故障。
    """
    offenders = {}
    for file in sorted((ROOT / "tools").glob("*.py")):
        bad = _gbk_unsafe_runtime_strings(file.read_text(encoding="utf-8"), file.name)
        if bad:
            offenders[file.name] = bad
    assert not offenders, f"以下文件的运行时字符串含 GBK 编不了的字符：{offenders}"
