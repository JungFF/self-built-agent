# tests/test_factory_state.py
"""切版本时把版本的出厂状态应用到 data/（铁律二 vs 铁律三的化解点）。

这一步是"发行版"这个概念能成立的前提：没有它，新版本只能改代码，永远改不了
persona/配置（Task 10 的验收项恰恰是"改一行 SOUL.md 发个新版"）。反过来，
它一旦多写一个字节到 .env / sessions / memories，第一次自动更新就会静默毁掉
用户的激活码和聊天记录——维护者在一万公里外收不到任何信号。

所以这里的测试分两半：
  1. 出厂状态**确实**被应用了（config.yaml 渲染出来了、SOUL.md 换了、出厂技能覆盖了）
  2. 用户的东西**一个字节都没动**（.env、sessions、memories、logs、习得技能）
"""
import os
import stat
from pathlib import Path, PureWindowsPath

import pytest
import yaml

from builder import paths
from tools.factory_state import (
    apply_factory_state,
    atomic_write,
    factory_state_is_current,
    render_config,
)


def _mk(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_atomic_write_survives_a_read_only_destination(tmp_path: Path):
    """目标文件被设成只读时，原子写必须照样成功。

    这不是洁癖，是平台差异咬人：os.replace 在 POSIX 上换的是目录项、看的是**目录**权限，
    目标文件只读照样换得动；Windows 上目标只读直接抛 PermissionError（[WinError 5]）。
    而 atomic_write 是 current.txt / previous.txt / .factory_version / config.yaml /
    SOUL.md **共用的唯一写入口**——Windows 又是这个产品唯一的交付平台。

    2026-07-21 真机实测：整套测试第一次在 Windows 上跑，test_updater.py 里那条"戳被杀软
    锁住"的用例就栽在这里——异常从 apply_factory_state 一路逃出去。把文件锁成只读（而不是
    删掉）正是中国杀软套装的惯常做法，也正是那批用例存在的理由。真落在 current.txt 上，
    这台机器就再也切不了版本，而更新通道是维护者唯一的远程救援手段。

    清只读位不是跟杀软抢地盘：这些是我们自己安装根里的状态文件，不是用户的文档。
    """
    path = tmp_path / "current.txt"
    path.write_text("0.1.0", encoding="utf-8")
    os.chmod(path, stat.S_IREAD)
    try:
        atomic_write(path, b"0.1.1")
        assert path.read_text(encoding="utf-8") == "0.1.1"
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)  # 让 tmp_path 的清理能删掉它


def _factory(version_dir: Path) -> Path:
    """一个形状完整的出厂母版（versions/<v>/factory/）。"""
    factory = version_dir / "factory"
    _mk(factory / "config.yaml.tmpl", 'display:\n  language: "zh"\nterminal:\n  cwd: "{{WORKSPACE_DIR}}"\n')
    _mk(factory / "SOUL.md", "我是小助手 v2")
    _mk(factory / "skills" / ".bundled_manifest", "ascii-art:def456\n")
    _mk(factory / "skills" / "creative" / "ascii-art" / "SKILL.md", "出厂技能 v2")
    return factory


@pytest.fixture()
def world(tmp_path: Path):
    """一台已经在跑的机器：data/ 里有用户资产 + 上一版的出厂状态。"""
    root = tmp_path / "root"
    _factory(root / "versions" / "0.2.0")

    data = root / "data"
    _mk(data / ".env", "DASHSCOPE_API_KEY=sk-keep-me")
    _mk(data / "config.yaml", "旧版本渲染出来的 config")
    _mk(data / "SOUL.md", "我是小助手 v1")
    _mk(data / "sessions" / "2026-07-11.jsonl", "聊天记录")
    _mk(data / "memories" / "notes.md", "记住的事")
    _mk(data / "logs" / "hermes.log", "日志")
    _mk(data / "skills" / "creative" / "ascii-art" / "SKILL.md", "出厂技能 v1")
    _mk(data / "skills" / "business" / "quote-sheet" / "SKILL.md", "习得技能")

    workspace = tmp_path / "desktop" / paths.WORKSPACE_DIRNAME
    workspace.mkdir(parents=True)
    return root, data, workspace


# ---------------------------------------------------------------- 渲染 config.yaml


def test_render_config_substitutes_the_workspace_placeholder():
    out = render_config('cwd: "{{WORKSPACE_DIR}}"\n', PureWindowsPath(r"C:\Users\ma\Desktop\小助手"))
    assert "{{WORKSPACE_DIR}}" not in out


def test_rendered_config_is_valid_yaml_and_round_trips_the_windows_path():
    """占位符在一个 YAML **双引号**标量里。Windows 路径全是反斜杠，不转义直接塞
    进去，`C:\\Users` 里的 `\\U` 会被 YAML 当成 8 位 Unicode 转义 → 解析报错 →
    Hermes 起不来 = 开机即挂，而"修复"按钮渲染出的正是这份坏配置。"""
    want = PureWindowsPath(r"C:\Users\ma\Desktop\小助手")
    out = render_config('terminal:\n  cwd: "{{WORKSPACE_DIR}}"\n', want)
    loaded = yaml.safe_load(out)  # 不合法的 YAML 在这里就炸
    assert loaded["terminal"]["cwd"] == str(want)


# ---------------------------------------------------------------- 出厂状态确实被应用了


def test_apply_renders_the_new_versions_config_into_data(world):
    root, data, workspace = world
    apply_factory_state(root, "0.2.0", workspace)
    loaded = yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["terminal"]["cwd"] == str(workspace)
    assert loaded["display"]["language"] == "zh"


def test_apply_does_not_leave_the_template_in_data(world):
    """data/ 里留一个 config.yaml.tmpl 没有任何用处：Hermes 不读它，它只会让下一个
    人以为那才是配置文件。"""
    root, data, workspace = world
    apply_factory_state(root, "0.2.0", workspace)
    assert not (data / "config.yaml.tmpl").exists()


def test_apply_overwrites_soul_and_bundled_skills(world):
    """发行版的定义（ADR-0003）：改一行 SOUL.md 就能发个新版。出厂状态不随版本
    下发的话，新版本只能改代码，persona 永远停在第一版。"""
    root, data, workspace = world
    apply_factory_state(root, "0.2.0", workspace)
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 v2"
    assert (data / "skills" / "creative" / "ascii-art" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "出厂技能 v2"


def test_apply_is_idempotent(world):
    root, data, workspace = world
    first = apply_factory_state(root, "0.2.0", workspace)
    second = apply_factory_state(root, "0.2.0", workspace)
    assert first == second
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 v2"


def test_apply_writes_plugin_files_into_data(world):
    """插件（比如 no-disk-destruction）的源码经 restore_factory_files 的通用遍历
    落地到 data/plugins/...——这条路径完全不需要改 restore_factory_files 一行代码
    （它已经会把 skills/ 之外的一切原样覆盖过去，见模块 docstring）。这里钉住"母版里
    多一个新的子目录"这件事不会被该函数漏掉或错误处理，内容必须与母版逐字一致。"""
    root, data, workspace = world
    plugin_dir = root / "versions" / "0.2.0" / "factory" / "plugins" / "no-disk-destruction"
    _mk(plugin_dir / "plugin.yaml", 'name: "no-disk-destruction"\nhooks:\n  - "pre_tool_call"\n')
    _mk(plugin_dir / "__init__.py", "def register(ctx):\n    pass\n")
    apply_factory_state(root, "0.2.0", workspace)
    dest = data / "plugins" / "no-disk-destruction"
    for name in ("plugin.yaml", "__init__.py"):
        assert (dest / name).read_text(encoding="utf-8") == (plugin_dir / name).read_text(
            encoding="utf-8"
        )


def test_apply_creates_the_workspace_dir_if_the_user_deleted_it(world):
    """长辈把桌面的「小助手」文件夹删了/拖进回收站是完全可能的。config.yaml 的
    terminal.cwd 指向一个不存在的目录 = Hermes 起不来。切版本时顺手补回来。"""
    root, data, workspace = world
    workspace.rmdir()
    apply_factory_state(root, "0.2.0", workspace)
    assert workspace.is_dir()


def test_apply_bootstraps_a_data_dir_that_does_not_exist_yet(tmp_path: Path):
    root = tmp_path / "root"
    _factory(root / "versions" / "0.2.0")
    workspace = tmp_path / "desktop" / paths.WORKSPACE_DIRNAME
    apply_factory_state(root, "0.2.0", workspace)
    assert (root / "data" / "SOUL.md").exists()
    assert (root / "data" / "skills" / "creative" / "ascii-art" / "SKILL.md").exists()


# ---------------------------------------------------------------- 用户的东西一个字节都没动


def test_apply_never_touches_user_owned_assets(world):
    """铁律二：更新会整个换掉 versions/<v>/，但 data/ 里这几样东西是用户的。
    激活码没了 = 产品直接不能用；聊天记录没了 = 一万公里外救不回来。"""
    root, data, workspace = world
    apply_factory_state(root, "0.2.0", workspace)
    assert (data / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"
    assert (data / "sessions" / "2026-07-11.jsonl").read_text(encoding="utf-8") == "聊天记录"
    assert (data / "memories" / "notes.md").read_text(encoding="utf-8") == "记住的事"
    assert (data / "logs" / "hermes.log").read_text(encoding="utf-8") == "日志"


def test_apply_keeps_learned_skills(world):
    """习得技能和出厂技能混在同一棵 skills/ 树里：应用出厂状态只能**覆盖**出厂
    技能，绝不能清空目录再铺——那会把 agent 自我改进沉淀下来的资产一起删掉。"""
    root, data, workspace = world
    apply_factory_state(root, "0.2.0", workspace)
    assert (data / "skills" / "business" / "quote-sheet" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "习得技能"


# ---------------------------------------------------------------- 闸门（全部先于任何写入）


def test_factory_carrying_env_is_rejected_without_touching_anything(world):
    """维护者打包时手滑把自己的 .env 打进出厂包——一旦分发就是密钥泄露，而且
    应用出厂状态时会直接盖掉每台机器的激活码。"""
    root, data, workspace = world
    _mk(root / "versions" / "0.2.0" / "factory" / ".env", "DASHSCOPE_API_KEY=sk-leaked")
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 v1"


def test_factory_hiding_an_env_in_a_subdirectory_is_rejected(world):
    """.env 不一定躺在母版顶层——装配机上它更可能是被某个子目录（技能目录、
    hermes-agent/ 的残留）捎带进来的。顶层白名单检查漏掉这种，激活码照样被分发到
    每一台机器上。所以 .env 的检查必须是**递归**的。"""
    root, data, workspace = world
    _mk(
        root / "versions" / "0.2.0" / "factory" / "skills" / "creative" / ".env",
        "DASHSCOPE_API_KEY=sk-leaked",
    )
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 v1"


def test_factory_carrying_user_owned_dirs_is_rejected(world):
    """出厂包里出现 sessions/ 意味着打包流程把某台机器的聊天记录打了进去——
    应用时会盖掉用户的记录，而且是每台机器都被盖成同一份。"""
    root, data, workspace = world
    _mk(root / "versions" / "0.2.0" / "factory" / "sessions" / "x.jsonl", "别人的聊天记录")
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / "sessions" / "2026-07-11.jsonl").read_text(encoding="utf-8") == "聊天记录"


def test_incomplete_factory_aborts_before_any_write(world):
    """出厂母版缺文件 = 打包出 bug。必须在动手之前整体中止，而不是写一半才发现
    ——写一半就是"新代码 + 一半新一半旧的出厂状态"，谁也说不清机器在什么状态。"""
    root, data, workspace = world
    (root / "versions" / "0.2.0" / "factory" / "SOUL.md").unlink()
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / "config.yaml").read_text(encoding="utf-8") == "旧版本渲染出来的 config"


def test_factory_template_with_an_unquoted_placeholder_is_rejected(world):
    """渲染是按**双引号 YAML 标量**转义的（反斜杠翻倍）。模板改成单引号或裸标量之后，
    渲染出来的 `cwd: 'C:\\\\Users\\\\ma\\\\Desktop\\\\小助手'` 依然是合法 YAML——只是路径多了
    一倍反斜杠，指向一个根本不存在的文件夹。没有任何东西会报错：agent 把长辈的文件写
    进那个虚构的目录，「小助手修复」还会一模一样地再渲染一遍。改模板是常规发版动作，
    所以闸门必须钉住**带引号的**那个形状，而不只是"占位符还在"。"""
    root, data, workspace = world
    for bad in ("terminal:\n  cwd: '{{WORKSPACE_DIR}}'\n", "terminal:\n  cwd: {{WORKSPACE_DIR}}\n"):
        _mk(root / "versions" / "0.2.0" / "factory" / "config.yaml.tmpl", bad)
        with pytest.raises(ValueError):
            apply_factory_state(root, "0.2.0", workspace)
        assert (data / "config.yaml").read_text(encoding="utf-8") == "旧版本渲染出来的 config"


def test_factory_carrying_a_rendered_config_is_rejected(world):
    """母版里混进一个 config.yaml（打包手滑，比如把某台机器的 data/ 拷了进来）：它会在
    渲染之后被原样盖上去，data/config.yaml 里于是留着字面量 `cwd: "{{WORKSPACE_DIR}}"`
    ——Hermes 会用一个叫 `{{WORKSPACE_DIR}}` 的目录当工作台，而闸门一声不吭。"""
    root, data, workspace = world
    _mk(
        root / "versions" / "0.2.0" / "factory" / "config.yaml",
        'terminal:\n  cwd: "{{WORKSPACE_DIR}}"\n',
    )
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / "config.yaml").read_text(encoding="utf-8") == "旧版本渲染出来的 config"


# ---------------------------------------------------------------- 出厂状态戳（收敛的凭据）


def test_apply_stamps_the_version_it_applied(world):
    """data/.factory_version 是"这台机器的 data/ 已经是哪个版本的出厂状态"的唯一凭据。
    没有它，调用方无从判断出厂状态该不该重新应用——只能假设"切了版本就等于应用过了"，
    而那个假设一旦不成立（应用失败在切换之后），机器就永久停在"新代码 + 旧 persona"上。"""
    root, data, workspace = world
    assert not factory_state_is_current(root, "0.2.0")
    apply_factory_state(root, "0.2.0", workspace)
    assert (data / paths.FACTORY_STAMP).read_text(encoding="utf-8").strip() == "0.2.0"
    assert factory_state_is_current(root, "0.2.0")
    assert not factory_state_is_current(root, "0.3.0")  # 戳是旧的 = 没收敛


def test_a_crashed_apply_does_not_leave_a_stamp_claiming_success(world, monkeypatch):
    """戳必须在**全部写完之后**才落盘。提前落的话，一次半途失败的应用会留下一个说
    "已经是新出厂状态了"的戳，自愈逻辑再也不会重跑它——silent no-op，永久生效。"""
    root, data, workspace = world
    real_replace = os.replace
    target = data / "skills" / "creative" / "ascii-art" / "SKILL.md"

    def flaky(src, dst, **kw):
        if Path(dst) == target:
            raise RuntimeError("杀软锁住了 skills/")
        return real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(RuntimeError):
        apply_factory_state(root, "0.2.0", workspace)
    assert not factory_state_is_current(root, "0.2.0")


def test_bundled_skills_are_written_atomically(world, monkeypatch):
    """出厂技能不能用 copy2 直接往目标文件上写：写到一半崩（断电、杀软锁文件、磁盘满），
    SKILL.md 就是**截断的半个文件**——比"停在旧版本"糟得多，因为它看起来是有效的。
    每个文件都必须 tmp + os.replace 换名（模块 docstring 承诺的就是这个）。

    这里让换名那一步失败：原子写的实现下，机器上那个技能仍然是完整的旧内容。"""
    root, data, workspace = world
    target = data / "skills" / "creative" / "ascii-art" / "SKILL.md"
    real_replace = os.replace

    def flaky(src, dst, **kw):
        if Path(dst) == target:
            raise RuntimeError("simulated power loss")
        return real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(RuntimeError):
        apply_factory_state(root, "0.2.0", workspace)
    # copy2 的实现下这里读到的是被覆盖/截断的内容；原子写下它还是完整的旧技能。
    assert target.read_text(encoding="utf-8") == "出厂技能 v1"


def test_factory_template_without_the_placeholder_is_rejected(world):
    """模板里没有 {{WORKSPACE_DIR}} = 渲染出来的 config 会把工作台指到别处（或者
    干脆指向字面量占位符）。这只可能是打包/改模板时出的 bug，必须响亮地失败。"""
    root, data, workspace = world
    _mk(root / "versions" / "0.2.0" / "factory" / "config.yaml.tmpl", 'terminal:\n  cwd: "D:/写死了"\n')
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / "config.yaml").read_text(encoding="utf-8") == "旧版本渲染出来的 config"


def test_refuses_when_data_skills_is_a_symlink(world, tmp_path: Path):
    """data/skills 是符号链接的话，覆盖出厂技能会写到 hermes_home 之外去。"""
    root, data, workspace = world
    outside = tmp_path / "outside-skills"
    (data / "skills").rename(outside)
    (data / "skills").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        apply_factory_state(root, "0.2.0", workspace)
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 v1"  # 闸门先于写入


def test_missing_version_dir_is_rejected(world):
    root, data, workspace = world
    with pytest.raises(ValueError):
        apply_factory_state(root, "9.9.9", workspace)
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 v1"
