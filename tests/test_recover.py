# tests/test_recover.py
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.recover import recover

ROOT = Path(__file__).resolve().parent.parent


def _mk(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _run_cli(home: Path, payload: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.recover", str(home), str(payload), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


@pytest.fixture()
def world(tmp_path: Path):
    """构造一个“用户把配置弄坏了 + agent 学了新技能”的现场。"""
    home, payload = tmp_path / "home", tmp_path / "payload"

    # 出厂 payload（Task 3 的 render_factory 产物形状）
    _mk(payload / "config.yaml", "factory-config")
    _mk(payload / "SOUL.md", "factory-soul")

    # 用户机现场
    _mk(home / "config.yaml", "user-broken-config")  # 被弄坏的配置
    _mk(home / ".env", "DASHSCOPE_API_KEY=sk-keep-me")  # 激活码，绝不能动
    # 出厂技能（名字在 manifest 里）
    _mk(home / "skills" / ".bundled_manifest", "apple-notes:abc123\nascii-art:def456\n")
    _mk(home / "skills" / "apple" / "apple-notes" / "SKILL.md", "bundled skill")
    _mk(home / "skills" / "creative" / "ascii-art" / "SKILL.md", "bundled skill")
    # 习得技能（名字不在 manifest 里）
    _mk(home / "skills" / "business" / "quote-sheet" / "SKILL.md", "learned skill")
    return home, payload


def test_recover_restores_factory_files(world):
    home, payload = world
    recover(home, payload)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "factory-config"
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "factory-soul"


def test_recover_preserves_env(world):
    home, payload = world
    recover(home, payload)
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_recover_preserves_learned_skills(world):
    home, payload = world
    recover(home, payload)
    assert (home / "skills" / "business" / "quote-sheet" / "SKILL.md").exists()


def test_deep_recover_wipes_learned_skills(world):
    home, payload = world
    recover(home, payload, deep=True)
    assert not (home / "skills" / "business" / "quote-sheet").exists()


def test_deep_recover_keeps_bundled_skills(world):
    home, payload = world
    recover(home, payload, deep=True)
    assert (home / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()
    assert (home / "skills" / "creative" / "ascii-art" / "SKILL.md").exists()


def test_deep_recover_keeps_env(world):
    home, payload = world
    recover(home, payload, deep=True)
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_deep_recover_keeps_manifest(world):
    home, payload = world
    recover(home, payload, deep=True)
    assert (home / "skills" / ".bundled_manifest").exists()


def test_payload_containing_env_is_rejected(world):
    home, payload = world
    _mk(payload / ".env", "DASHSCOPE_API_KEY=sk-leaked")
    with pytest.raises(ValueError):
        recover(home, payload)
    # 拒绝执行必须是“完全没动手”——不能出现“改了一半”的中间态。
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_recover_does_not_delete_unrelated_user_files(world):
    home, payload = world
    _mk(home / "notes.txt", "我自己的笔记")
    recover(home, payload)
    assert (home / "notes.txt").read_text(encoding="utf-8") == "我自己的笔记"


def test_deep_recover_missing_manifest_aborts_without_mutation(world):
    """manifest 缺失 = 无法区分出厂/习得技能。宁可中止也不要猜——猜错了就是
    不可逆地删掉出厂技能。"""
    home, payload = world
    (home / "skills" / ".bundled_manifest").unlink()
    with pytest.raises(FileNotFoundError):
        recover(home, payload, deep=True)
    # 中止必须是原子的：连“本来无条件安全”的出厂文件还原也不能做半截，
    # 否则维护者会看到一个“既不是出厂态、又不是原来坏配置”的诡异中间态。
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"
    assert (home / "skills" / "business" / "quote-sheet").exists()
    assert (home / "skills" / "apple" / "apple-notes").exists()


def test_deep_recover_without_skills_dir_is_noop_for_skills(tmp_path: Path):
    """全新/未初始化的机器上根本没有 skills/ 目录：没有可清理的东西，
    不应该因为缺 manifest 而报错——错误信号应该只在“有东西但分不清”时触发。"""
    home, payload = tmp_path / "home", tmp_path / "payload"
    _mk(payload / "config.yaml", "factory-config")
    _mk(home / "config.yaml", "user-broken-config")
    actions = recover(home, payload, deep=True)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "factory-config"
    assert not (home / "skills").exists()
    assert any("跳过" in a for a in actions)


def test_deep_recover_irregular_skill_depth(world):
    """skills/ 结构不规整：computer-use/ 下直接是 SKILL.md，没有再套一层
    技能目录。判定不能假设固定深度。"""
    home, payload = world
    _mk(home / "skills" / "computer-use" / "SKILL.md", "bundled skill, flat layout")
    (home / "skills" / ".bundled_manifest").write_text(
        "apple-notes:abc123\nascii-art:def456\ncomputer-use:zzz999\n",
        encoding="utf-8",
    )
    recover(home, payload, deep=True)
    assert (home / "skills" / "computer-use" / "SKILL.md").exists()  # 深度不规整也不误删
    assert not (home / "skills" / "business" / "quote-sheet").exists()  # 习得技能仍被清理


def test_deep_recover_name_collision_is_a_known_limitation(world):
    """已知限制：判定完全靠“技能目录名字”跟 manifest 比对，不比对路径或哈希。
    如果习得技能的目录名恰好和某个出厂技能同名，会被当成出厂技能保留下来。
    把这条行为显式钉成测试，而不是留成没人知道的隐藏假设。"""
    home, payload = world
    _mk(home / "skills" / "business" / "ascii-art" / "SKILL.md", "同名的“习得”技能")
    recover(home, payload, deep=True)
    assert (home / "skills" / "business" / "ascii-art").exists()


def test_cli_runs_recover(world):
    home, payload = world
    result = _run_cli(home, payload)
    assert result.returncode == 0, result.stderr
    assert (home / "config.yaml").read_text(encoding="utf-8") == "factory-config"
    assert "小助手" in result.stdout


def test_cli_deep_flag_wipes_learned_skills(world):
    home, payload = world
    result = _run_cli(home, payload, "--deep")
    assert result.returncode == 0, result.stderr
    assert not (home / "skills" / "business" / "quote-sheet").exists()


def test_deep_recover_stray_skill_md_at_skills_root_does_not_destroy_tree(world):
    """回归测试（C1）：skills/ 根目录下如果直接躺着一个 SKILL.md（手滑/污染），
    旧版 _skill_dirs 会把 skills_dir 自身也当成一个“技能目录”纳入待删候选。
    因为 'skills' 这个目录名不在 manifest 里，会被判定为“习得技能”整棵删掉——
    连 .bundled_manifest 和所有出厂技能一起陪葬，然后处理下一个（更深的）
    目标时因为父目录已经没了而 FileNotFoundError 崩溃。"""
    home, payload = world
    _mk(home / "skills" / "SKILL.md", "stray file at skills root")
    recover(home, payload, deep=True)
    assert (home / "skills" / ".bundled_manifest").exists()
    assert (home / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()
    assert (home / "skills" / "creative" / "ascii-art" / "SKILL.md").exists()
    assert not (home / "skills" / "business" / "quote-sheet").exists()


def test_deep_recover_nested_skill_md_inside_bundled_skill_is_kept(world):
    """回归测试（C2）：出厂技能 ascii-art 内部有一个 examples/SKILL.md 模板/
    示例文件（很常见的技能内容形态）。旧版 _skill_dirs 把 'examples' 也当成
    独立技能目录——这个名字当然不在 manifest 里，于是被误判成“习得技能”
    静默删除，导致出厂技能被截肢，且退出码是 0，日志还印着“深度恢复完成”。"""
    home, payload = world
    nested = home / "skills" / "creative" / "ascii-art" / "examples" / "SKILL.md"
    _mk(nested, "template/example file, not an independent skill")
    recover(home, payload, deep=True)
    assert nested.exists()
    assert (home / "skills" / "creative" / "ascii-art" / "SKILL.md").exists()


def test_deep_recover_nested_skill_md_inside_learned_skill_does_not_crash(world):
    """回归测试（C3）：习得技能 quote-sheet 内部又有一个 templates/SKILL.md。
    旧版 _skill_dirs 把父子两层目录都当成独立技能目录纳入 to_remove，
    sorted() 让父目录排在前面，rmtree(父目录) 把子目录一起删了，接着处理
    子目录这一项时 rmtree 发现路径已经不存在，抛 FileNotFoundError，
    整个深度恢复在改了一半的中间态上崩溃。"""
    home, payload = world
    _mk(
        home / "skills" / "business" / "quote-sheet" / "templates" / "SKILL.md",
        "template inside a learned skill",
    )
    recover(home, payload, deep=True)
    assert not (home / "skills" / "business" / "quote-sheet").exists()


def test_payload_directory_named_env_is_rejected(world):
    """回归测试（M6）：payload 里混入的不一定是 .env 文件，也可能是一个叫
    .env 的目录（比如误建了目录）。旧版 _assert_payload_has_no_env 只对
    p.is_file() 报警，目录形态会漏网放行，直到复制阶段撞见同名的目标路径
    才炸出一个不明所以的 FileExistsError。"""
    home, payload = world
    _mk(payload / ".env" / "key.txt", "leaked-looking directory")
    with pytest.raises(ValueError):
        recover(home, payload)
    assert (home / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"


def test_recover_rejects_missing_factory_payload(world):
    """回归测试：factory_payload 目录不存在（打包遗漏、桌面快捷方式路径写错）
    时，旧版 _restore_factory_files 对不存在的目录 rglob 静默返回空列表，
    recover() “成功”返回 []，CLI 印出“小助手修复完成！”、退出码 0，但
    hermes_home 里被弄坏的配置其实原封不动——长辈看到的是一句自信的谎言。"""
    home, payload = world
    missing_payload = payload.parent / "does-not-exist"
    assert not missing_payload.exists()
    with pytest.raises(ValueError):
        recover(home, missing_payload)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "user-broken-config"


def test_deep_recover_refuses_when_skills_dir_is_symlink(world, tmp_path: Path):
    """回归测试（M7）：home/skills 如果是指向别处的符号链接，rmtree 会跟着
    链接删到 hermes_home 之外。虽然 Hermes 正常出厂形态里 skills/ 一定是
    真实目录，但深度恢复应该显式拒绝这种情况，而不是靠“碰巧没人这么干”
    的隐性安全。"""
    home, payload = world
    outside_target = tmp_path / "outside-skills"
    real_skills = home / "skills"
    real_skills.rename(outside_target)
    real_skills.symlink_to(outside_target, target_is_directory=True)
    with pytest.raises(ValueError):
        recover(home, payload, deep=True)
    assert outside_target.exists()
