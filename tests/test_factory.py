# tests/test_factory.py
from pathlib import Path

from builder import paths
from builder.factory import render_factory

ROOT = Path(__file__).resolve().parent.parent


def _config_tmpl() -> str:
    return (ROOT / "factory" / "config.yaml.tmpl").read_text(encoding="utf-8")


def test_render_factory_produces_payload(tmp_path: Path):
    payload = render_factory(tmp_path)
    text = (payload / "SOUL.md").read_text(encoding="utf-8")
    assert "小助手" in text
    assert "铁律" in text
    assert (payload / "config.yaml.tmpl").exists()
    assert not list(payload.rglob("*.txt"))  # 源文件不进 payload


def test_render_factory_is_idempotent(tmp_path: Path):
    a = render_factory(tmp_path)
    b = render_factory(tmp_path)
    assert a == b
    assert (b / "SOUL.md").exists()


def test_soul_md_has_no_source_file_header_comment(tmp_path: Path):
    """soul.txt 的源文件头注释不能泄漏进 SOUL.md——那会直接进 agent 的 system prompt。"""
    payload = render_factory(tmp_path)
    text = (payload / "SOUL.md").read_text(encoding="utf-8")
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
