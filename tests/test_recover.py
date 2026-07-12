# tests/test_recover.py
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from builder import paths
from tools.recover import recover

ROOT = Path(__file__).resolve().parent.parent


def _mk(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _run_cli(home: Path, factory: Path, workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, "-m", "tools.recover",
            str(home), str(factory), "--workspace", str(workspace), *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


@pytest.fixture()
def world(tmp_path: Path):
    """一个"用户把配置弄坏了 + agent 学了新技能 + agent 还就地改写了一个出厂技能"的现场。

    出厂母版住在版本目录里（versions/<v>/factory/，只读、agent 够不到），不在 data/ 里
    ——拿运行时的 skills/ 当重铺来源等于拿一棵可能已经被污染的树去"恢复出厂"（ADR-0005）。
    """
    home = tmp_path / "root" / "data"
    factory = tmp_path / "root" / "versions" / "0.2.0" / "factory"
    workspace = tmp_path / "desktop" / paths.WORKSPACE_DIRNAME

    # 版本自带的出厂母版
    _mk(factory / "config.yaml.tmpl", 'display:\n  language: "zh"\nterminal:\n  cwd: "{{WORKSPACE_DIR}}"\n')
    _mk(factory / "SOUL.md", "factory-soul")
    _mk(factory / "skills" / ".bundled_manifest", "apple-notes:abc123\nascii-art:def456\n")
    _mk(factory / "skills" / "apple" / "apple-notes" / "SKILL.md", "出厂技能：apple-notes")
    _mk(factory / "skills" / "creative" / "ascii-art" / "SKILL.md", "出厂技能：ascii-art")

    # 用户机现场
    _mk(home / "config.yaml", "user-broken-config")            # 被弄坏的配置
    _mk(home / "SOUL.md", "被改过的 persona")
    _mk(home / ".env", "DASHSCOPE_API_KEY=sk-keep-me")          # 激活码，绝不能动
    _mk(home / "skills" / ".bundled_manifest", "apple-notes:abc123\nascii-art:def456\n")
    _mk(home / "skills" / "apple" / "apple-notes" / "SKILL.md", "出厂技能：apple-notes")
    # agent 就地改写了一个出厂技能——ADR-0005 说这是最可能的行为漂移方式
    _mk(home / "skills" / "creative" / "ascii-art" / "SKILL.md", "被 agent 就地改写过的出厂技能")
    # 习得技能
    _mk(home / "skills" / "business" / "quote-sheet" / "SKILL.md", "learned skill")
    return home, factory, workspace


# ---------------------------------------------------------------- 一键恢复


def test_recover_renders_config_from_the_template(world):
    """出厂母版里只有 config.yaml.tmpl（带 {{WORKSPACE_DIR}} 占位符）。修复必须把它
    **渲染**成 config.yaml——config.yaml 才是 Hermes 真正读的那个文件，也是最可能被
    弄坏的那个。只是把模板原样拷过去的话，「小助手修复完成！」印出来了，坏配置一个
    字节都没修——又一句对长辈的自信的谎言。"""
    home, factory, workspace = world
    recover(home, factory, workspace)
    loaded = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["terminal"]["cwd"] == str(workspace)
    assert loaded["display"]["language"] == "zh"


def test_recover_does_not_leave_the_template_in_data(world):
    """data/ 里躺一个 config.yaml.tmpl 没有任何用处：Hermes 不读它。"""
    home, factory, workspace = world
    recover(home, factory, workspace)
    assert not (home / "config.yaml.tmpl").exists()


def test_recover_restores_soul(world):
    home, factory, workspace = world
    recover(home, factory, workspace)
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "factory-soul"


def test_recover_preserves_env(world):
    home, factory, workspace = world
    recover(home, factory, workspace)
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_recover_preserves_learned_skills(world):
    """一键恢复是长辈自己按的那个按钮：修配置，不清技能。习得技能是 agent 自我改进
    沉淀下来的资产，不该因为一次修复就清零（要清是 --deep 的事）。"""
    home, factory, workspace = world
    recover(home, factory, workspace)
    assert (home / "skills" / "business" / "quote-sheet" / "SKILL.md").exists()


def test_recover_restores_an_in_place_edited_bundled_skill(world):
    """一键恢复必须至少做到一次普通更新做到的事（apply_factory_state：覆盖出厂技能、
    保留习得技能）。

    ADR-0005 说 agent **就地改写某个出厂技能**是最可能的行为漂移方式。旧实现里一键恢复
    完全跳过 skills/，于是长辈唯一会按的那个按钮，恰恰修不了最可能坏的那个东西——而一次
    静默的例行更新反倒能修。"保留习得技能"这个理由站不住：覆盖本来就保留习得技能。"""
    home, factory, workspace = world
    recover(home, factory, workspace)
    assert (home / "skills" / "creative" / "ascii-art" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "出厂技能：ascii-art"


def test_recover_does_not_delete_unrelated_user_files(world):
    home, factory, workspace = world
    _mk(home / "notes.txt", "我自己的笔记")
    recover(home, factory, workspace)
    assert (home / "notes.txt").read_text(encoding="utf-8") == "我自己的笔记"


def test_recover_creates_the_workspace_dir_if_it_is_missing(world):
    """长辈把桌面的「小助手」文件夹删了：config.yaml 的 terminal.cwd 指向一个不存在的
    目录，Hermes 起不来。修复按钮顺手把它补回来。"""
    home, factory, workspace = world
    assert not workspace.exists()
    recover(home, factory, workspace)
    assert workspace.is_dir()


# ---------------------------------------------------------------- 深度恢复（ADR-0005）


def test_deep_recover_relays_an_in_place_edited_bundled_skill(world):
    """ADR-0005 的全部理由：按名字做差集的旧算法**刹不住**"agent 就地改写出厂技能"
    ——名字还在清单里，被判为出厂技能而保留。而那恰恰是自我改进最可能的漂移方式，
    等于终极刹车对最该刹住的东西无效。现在直接从版本目录里的只读母版重铺。"""
    home, factory, workspace = world
    recover(home, factory, workspace, deep=True)
    assert (home / "skills" / "creative" / "ascii-art" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "出厂技能：ascii-art"


def test_deep_recover_wipes_learned_skills(world):
    home, factory, workspace = world
    recover(home, factory, workspace, deep=True)
    assert not (home / "skills" / "business" / "quote-sheet").exists()


def test_deep_recover_wipes_a_learned_skill_that_shadows_a_bundled_name(world):
    """旧算法的已知限制（名字撞车的习得技能会被当成出厂技能保留）随重铺一起消失。"""
    home, factory, workspace = world
    _mk(home / "skills" / "business" / "ascii-art" / "SKILL.md", "同名的习得技能")
    recover(home, factory, workspace, deep=True)
    assert not (home / "skills" / "business" / "ascii-art").exists()


def test_deep_recover_restores_all_bundled_skills_and_the_manifest(world):
    home, factory, workspace = world
    recover(home, factory, workspace, deep=True)
    assert (home / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()
    assert (home / "skills" / ".bundled_manifest").exists()


def test_deep_recover_removes_junk_dropped_into_the_skills_root(world):
    home, factory, workspace = world
    _mk(home / "skills" / "SKILL.md", "手滑掉在 skills/ 根目录的文件")
    recover(home, factory, workspace, deep=True)
    assert not (home / "skills" / "SKILL.md").exists()
    assert (home / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()


def test_deep_recover_keeps_env(world):
    """激活码属于本机，跟行为漂移无关。没了 key 产品直接不能用（ADR-0002）。"""
    home, factory, workspace = world
    recover(home, factory, workspace, deep=True)
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_deep_recover_also_restores_config_and_soul(world):
    home, factory, workspace = world
    recover(home, factory, workspace, deep=True)
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "factory-soul"
    loaded = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["terminal"]["cwd"] == str(workspace)


def test_deep_recover_lays_down_skills_on_a_machine_that_has_none(world):
    """机器上根本没有 skills/（全新装/被删光）：重铺语义下这不是"无事可做"，而是
    "把出厂技能铺回去"。"""
    home, factory, workspace = world
    shutil.rmtree(home / "skills")
    recover(home, factory, workspace, deep=True)
    assert (home / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()


# ---------------------------------------------------------------- 闸门（全部先于任何写入）


def test_recover_rejects_missing_factory_dir(world):
    """出厂母版目录不存在（打包遗漏、快捷方式路径写错）：旧版对不存在的目录 rglob
    静默返回空列表，recover() "成功"返回 []，CLI 印出「小助手修复完成！」、退出码 0，
    而被弄坏的配置原封不动——长辈看到的是一句自信的谎言。"""
    home, factory, workspace = world
    missing = factory.parent / "does-not-exist"
    with pytest.raises(ValueError):
        recover(home, missing, workspace)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"


def test_factory_containing_env_is_rejected(world):
    """出厂母版里混入 .env = 维护者打包时手滑把自己的激活码打进了发行版。"""
    home, factory, workspace = world
    _mk(factory / ".env", "DASHSCOPE_API_KEY=sk-leaked")
    with pytest.raises(ValueError):
        recover(home, factory, workspace)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_factory_containing_a_directory_named_env_is_rejected(world):
    """混进来的不一定是文件，也可能是一个叫 .env 的目录。只看 is_file() 会漏网放行。"""
    home, factory, workspace = world
    _mk(factory / ".env" / "key.txt", "leaked-looking directory")
    with pytest.raises(ValueError):
        recover(home, factory, workspace)
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_incomplete_factory_aborts_before_any_write(world):
    home, factory, workspace = world
    (factory / "SOUL.md").unlink()
    with pytest.raises(ValueError):
        recover(home, factory, workspace)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"


def test_deep_recover_without_a_factory_skills_master_aborts_without_mutation(world):
    """重铺的来源没了。如果不在动手之前拦下来，rmtree 会先把 skills/ 删光，再从一个
    不存在的母版拷 0 个文件回来——机器上一个技能都不剩，而 CLI 印的是「深度恢复完成！」。
    这是"深度恢复"能造成的最坏后果，必须在任何写入之前中止。"""
    home, factory, workspace = world
    shutil.rmtree(factory / "skills")
    with pytest.raises(ValueError):
        recover(home, factory, workspace, deep=True)
    assert (home / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()
    assert (home / "skills" / "business" / "quote-sheet" / "SKILL.md").exists()
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"


@pytest.mark.parametrize("deep", [False, True])
def test_recover_refuses_when_skills_dir_is_symlink(world, tmp_path: Path, deep: bool):
    """home/skills 是符号链接的话，深度恢复的 rmtree 会跟着链接删到 hermes_home 之外，
    一键恢复的"覆盖出厂技能"也会把文件写到外面去。两种语义都要拦。"""
    home, factory, workspace = world
    outside_target = tmp_path / "outside-skills"
    real_skills = home / "skills"
    real_skills.rename(outside_target)
    real_skills.symlink_to(outside_target, target_is_directory=True)
    with pytest.raises(ValueError):
        recover(home, factory, workspace, deep=deep)
    assert outside_target.exists()
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"


def test_recover_refuses_a_hermes_home_that_does_not_exist(world):
    """维护者远程 ToDesk 进去手敲路径，敲错一个字符：旧实现会**凭空建出**一棵 data/ 树，
    把出厂状态铺进去，然后印「小助手修复完成！」、退出 0——而真正坏掉的那台机器上的
    data/ 一个字节都没动。修复工具报的成功必须是真的成功。"""
    home, factory, workspace = world
    typo = home.parent / "dta"
    with pytest.raises(ValueError):
        recover(typo, factory, workspace)
    assert not typo.exists(), "不该凭空长出一棵谁都没打算要的 data/"
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"


# ---------------------------------------------------------------- CLI


def test_cli_runs_recover(world):
    home, factory, workspace = world
    result = _run_cli(home, factory, workspace)
    assert result.returncode == 0, result.stderr
    loaded = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["terminal"]["cwd"] == str(workspace)
    assert "小助手" in result.stdout


def test_cli_deep_flag_relays_skills(world):
    home, factory, workspace = world
    result = _run_cli(home, factory, workspace, "--deep")
    assert result.returncode == 0, result.stderr
    assert not (home / "skills" / "business" / "quote-sheet").exists()
    assert (home / "skills" / "creative" / "ascii-art" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "出厂技能：ascii-art"


def test_cli_reports_failure_instead_of_lying(world):
    """闸门拦下来的时候，CLI 必须以非零码退出并说"失败"——绝不能印「修复完成」。"""
    home, factory, workspace = world
    result = _run_cli(home, factory.parent / "does-not-exist", workspace)
    assert result.returncode == 1
    assert "失败" in result.stdout
    assert "完成" not in result.stdout


def test_cli_refuses_a_typo_in_hermes_home(world):
    """远程排障时手敲错 HERMES_HOME 的路径：CLI 必须失败，而不是建一棵假的 data/ 树
    再印「修复完成」——那台真正坏掉的机器还在坏着，而维护者以为修好了。"""
    home, factory, workspace = world
    result = _run_cli(home.parent / "dta", factory, workspace)
    assert result.returncode == 1
    assert "失败" in result.stdout
    assert not (home.parent / "dta").exists()
