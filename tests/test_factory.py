# tests/test_factory.py
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
