# tests/test_factory.py
import ast
from pathlib import Path

import pytest
import yaml

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


def test_render_factory_excludes_skills_that_cannot_work_for_the_end_user(
    tmp_path: Path, skills_src: Path
):
    """维护者 2026-07-21 亲自拍板排除的技能：`nano-pdf`（PDF 编辑）——装它需要联网装包，
    装上了还要另一把从没配置过的第三方 LLM key（见它自己的 SKILL.md："requires an API
    key"），爸妈这边走这条路必炸；而且我们已经用 pymupdf 把"PDF 处理"这件事用另一条路
    铺好了，不留着制造"到底走哪条路"的混乱。

    这不是"控制器单方面砍技能"（那正是维护者之前否决过的事）——只排除这一个、有名有姓、
    维护者亲口认定"没法修也没必要留"的技能，其余 71 个原样保留。"""
    (skills_src / "productivity" / "nano-pdf").mkdir(parents=True)
    (skills_src / "productivity" / "nano-pdf" / "SKILL.md").write_text(
        "name: nano-pdf", encoding="utf-8"
    )

    factory = render_factory(tmp_path / "out", skills_src)

    assert not (factory / paths.FACTORY_SKILLS / "productivity" / "nano-pdf").exists()
    # 排除名单只精确匹配这一个技能——同目录下的其它技能、以及别的分类，必须原样保留。
    assert (factory / paths.FACTORY_SKILLS / "creative" / "ascii-art" / "SKILL.md").exists()


def test_excluded_skills_are_dropped_from_the_bundled_manifest_too(
    tmp_path: Path, skills_src: Path
):
    """排掉一个技能，必须连它在 `.bundled_manifest` 里的那一行一起排掉。

    2026-07-21 真机实测（0.1.4 装机后量的）：`data/skills/` 下确实只剩 71 个 SKILL.md，
    而 `.bundled_manifest` 仍是 72 行、`nano-pdf:ffc0c90fc7ed18952ccbf1b69ab3aabb` 好端端
    躺在里面——ignore 回调只跳过技能**目录**，而清单是 skills 根目录下的一个普通文件，
    被 copytree 原样拷走了。

    后果不是"多一行没用的文本"：清单是"哪些技能属于出厂自带"的权威名录。名字还在名录里、
    目录却不存在，等于对 agent 宣告"你有 PDF 编辑这个能力"——它真去调用的时候才发现没有。
    那是一次没人在场的静默失败，而排除 nano-pdf 的全部理由恰恰就是"它在爸妈那边必炸"。
    排掉技能却留着名字，等于把"必炸"换成了"必炸得更晚、更难查"。
    """
    (skills_src / "productivity" / "nano-pdf").mkdir(parents=True)
    (skills_src / "productivity" / "nano-pdf" / "SKILL.md").write_text(
        "name: nano-pdf", encoding="utf-8"
    )
    (skills_src / paths.FACTORY_SKILLS_MANIFEST).write_text(
        "ascii-art:def456\nnano-pdf:ffc0c90f\n", encoding="utf-8"
    )

    factory = render_factory(tmp_path / "out", skills_src)

    manifest = (factory / paths.FACTORY_SKILLS / paths.FACTORY_SKILLS_MANIFEST).read_text(
        encoding="utf-8"
    )
    assert "nano-pdf" not in manifest
    # 只删被排除的那一行，其余条目必须原样保留——清单少一行是"技能没了"，多删一行是
    # "出厂技能被误判成习得技能"，两种都会让深度恢复算错该保留什么。
    assert "ascii-art:def456" in manifest


def test_stripping_the_manifest_does_not_rewrite_its_line_endings(
    tmp_path: Path, skills_src: Path
):
    """删掉一行，就只删那一行——绝不能顺手把整份清单的换行符也改掉。

    Path.read_text/write_text 默认都做换行转换：在 Windows 上读进来 \\r\\n 收成 \\n、
    写出去 \\n 又放成 \\r\\n。于是"删一行"会变成"整份文件每一行都被改写"。

    这不是洁癖。2026-07-21 花了大半天查的那个 bug——Hermes 仓库的 git 工作区一克隆出来
    就有 324 个"已修改"文件、`git checkout <钉死 commit>` 直接 abort、桌面端起不来——
    根因正是这种"看不见的整文件换行符改写"。清单虽然不进 git，但同一个坑不该在同一个
    项目里种第二次：文件内容只应该按我们明确打算改的那样变。
    """
    (skills_src / "productivity" / "nano-pdf").mkdir(parents=True)
    (skills_src / "productivity" / "nano-pdf" / "SKILL.md").write_text(
        "name: nano-pdf", encoding="utf-8"
    )
    # 故意用 CRLF 落盘，且绕开 write_text 的换行转换，确保磁盘上就是这个字节序列。
    (skills_src / paths.FACTORY_SKILLS_MANIFEST).write_bytes(
        b"ascii-art:def456\r\nnano-pdf:ffc0c90f\r\napple-notes:abc123\r\n"
    )

    factory = render_factory(tmp_path / "out", skills_src)

    raw = (factory / paths.FACTORY_SKILLS / paths.FACTORY_SKILLS_MANIFEST).read_bytes()
    assert raw == b"ascii-art:def456\r\napple-notes:abc123\r\n"


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


def test_config_template_enables_the_no_disk_destruction_plugin():
    """插件默认不加载（Hermes opt-in 设计），必须在 config.yaml.tmpl 里显式声明
    plugins.enabled 才会真的生效——用 yaml.safe_load 解析验证，不能只做字符串包含
    检查（字符串包含测不出缩进/嵌套写错导致 plugins.enabled 解析成别的类型，或者
    整份 YAML 直接语法错误）。"""
    tmpl = _config_tmpl()
    loaded = yaml.safe_load(tmpl)
    assert "no-disk-destruction" in loaded["plugins"]["enabled"]


def test_render_factory_includes_the_no_disk_destruction_plugin(tmp_path: Path, skills_src: Path):
    """插件源码放在仓库的 factory/plugins/no-disk-destruction/ 下（不是测试 fixture
    造出来的），render_factory() 的通用 shutil.copytree 会原样带走，不需要碰它一行
    代码。这里钉住两件事：母版里真的带上了插件文件；多出来的 plugins/ 子目录不会
    把 assert_factory_complete 的既有校验弄挂（它只检查必需文件、禁止用户资产，
    不限制母版里还能有什么别的东西）。"""
    factory = render_factory(tmp_path / "out", skills_src)
    plugin_dir = factory / "plugins" / "no-disk-destruction"
    assert (plugin_dir / "plugin.yaml").is_file()
    assert (plugin_dir / "__init__.py").is_file()
    assert_factory_complete(factory)  # 不会因为多了 plugins/ 而被拒


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
