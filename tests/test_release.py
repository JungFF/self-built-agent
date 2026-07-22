# tests/test_release.py
"""发行版打包、签名、发布的安全测试。

**一个坏包上了通道，是这个项目里唯一能同时毁掉所有机器的操作。** 通道是单向的：
包一旦上去，每台机器都会在下一次开机时静默地把它装上，一万公里外的维护者收不到任何
信号。所以这里钉住的每一条，都是"如果打包器没做这件事，会发生什么"：

- 包里没有 factory/  → 每台机器：下 895MB → 解压 2.87GB → assert_factory_complete
  拒收 → 退出 1 → **每次开机重演一遍，永远**。
- 包里没有 tools/（或它 import 不起来）→ 更新器自己没了。更新器是**唯一的远程修复
  通道**：它死了，连能救这台机器的下一个发行版都装不上。
- 包里混进 .env → 维护者的激活码流向每一台机器（而且 apply_factory_state 会盖掉每台
  机器**自己的** .env）。
- 包里混进 channel.key → 私钥是整个更新通道的**信任根**，而公钥在装机时就烧进了
  channel.json：泄漏之后任何人都能签一个包让每台机器执行任意代码，**永远无法收回**。
- 版本号更新器解析不了（"0.1.0-rc1"）→ _manifest_fields 抛 ValueError → 每台机器每次
  开机 SystemExit(1)。

因此每一道守卫都必须有一条"删掉它就会变红"的测试；而最终的判据只有一个：**真的把
更新器（消费方）放到这个包上跑一遍，看它认不认。**
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature

from builder import keys, release
from builder.keys import generate_keypair, public_key_hex, sign
from builder.paths import (
    AGENT_DIR_REL,
    BASE_PYTHON_REL,
    BUILDER_DIR_REL,
    DESKTOP_APP_REL,
    ELECTRON_EXE_REL,
    ENV_FILE,
    FACTORY_DIR_REL,
    FACTORY_SOUL,
    FACTORY_STAMP,
    HEARTBEAT_CRED_FILE,
    HEARTBEAT_PREFIX,
    INSTALL_ROOT,
    PLAYWRIGHT_DIR_REL,
    TOOLS_DIR_REL,
    VENV_PYTHON_REL,
)
from builder.release import (
    MANIFEST_NAME,
    REQUIRED_IN_PAYLOAD,
    SIG_NAME,
    assemble_payload,
    build_release,
    heartbeat_ram_policy,
    package_name,
    publish_commands,
    rel_path,
)
from tools.factory_state import assert_factory_complete
from tools.updater import apply_update, verify_manifest

# 一份**假的**心跳凭证（形状对，内容是编的）。真凭证是一次性人工步骤，绝不进仓库。
FAKE_HEARTBEAT = {
    "endpoint": "oss-cn-beijing.aliyuncs.com",
    "bucket": "xiaozhushou-heartbeat",
    "prefix": HEARTBEAT_PREFIX,
    "access_key_id": "LTAI-FAKE-FOR-TESTS",
    "access_key_secret": "FAKE-SECRET-FOR-TESTS",
}
CHANNEL_BUCKET = "xiaozhushou-channel"


@pytest.fixture()
def skills_src(tmp_path: Path) -> Path:
    """装配机上 Hermes 装好之后的 skills/（出厂技能母版的唯一来源，仓库里没有）。"""
    src = tmp_path / "snapshot-skills"
    (src / "creative" / "ascii-art").mkdir(parents=True)
    (src / "creative" / "ascii-art" / "SKILL.md").write_text("出厂技能", encoding="utf-8")
    return src


@pytest.fixture()
def snapshot(tmp_path: Path) -> Path:
    """装配机上的 Hermes 快照（Task 2 的产物）：只造出契约点名要有的那几个落点。"""
    snap = tmp_path / "snapshot"
    for rel in (VENV_PYTHON_REL, BASE_PYTHON_REL, ELECTRON_EXE_REL):
        exe = snap / rel_path(rel)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ fake exe")
    (snap / rel_path(DESKTOP_APP_REL)).mkdir(parents=True, exist_ok=True)
    (snap / rel_path(PLAYWRIGHT_DIR_REL) / "chromium").mkdir(parents=True, exist_ok=True)
    return snap


@pytest.fixture()
def payload(tmp_path: Path, snapshot: Path, skills_src: Path) -> Path:
    """一棵装配好的、合格的 payload（= versions/<版本号>/ 的内容）。版本号跟 _build() 的
    默认值（"0.1.0"）保持一致——两者本该是同一个数，只是分属两个不同的调用方。"""
    return assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.0")


@pytest.fixture()
def key(tmp_path: Path) -> tuple[str, str]:
    """(私钥 hex, 公钥 hex)。每个测试一把新的，绝不碰 secrets/ 里的真钥。"""
    kf, pf = generate_keypair(tmp_path / "secrets")
    return kf.read_text(encoding="utf-8"), pf.read_text(encoding="utf-8")


def _build(payload: Path, out: Path, key: tuple[str, str], version: str = "0.1.0", **kw) -> dict:
    kw.setdefault("heartbeat", FAKE_HEARTBEAT)
    return build_release(payload, version, out, key[0], **kw)


# =============================================================================
# 密钥：信任根
# =============================================================================


def test_keypair_roundtrip_verifies_from_the_consumers_side(tmp_path: Path):
    """维护者签的东西，更新器（消费方的 verify_manifest）必须认。"""
    kf, pf = generate_keypair(tmp_path)
    priv_hex, pub_hex = kf.read_text(encoding="utf-8"), pf.read_text(encoding="utf-8")
    body = b'{"a":1}'
    assert verify_manifest(body, sign(body, priv_hex), pub_hex) == {"a": 1}
    assert public_key_hex(priv_hex) == pub_hex


@pytest.mark.parametrize("survivor", ["both", "只剩私钥", "只剩公钥"])
def test_generate_keypair_never_overwrites_an_existing_key(tmp_path: Path, survivor: str):
    """**覆盖通道密钥 = 把所有已装机的机器永久踢出更新通道。**

    公钥在装机时就烧进了 channel.json，装出去之后没有任何东西能改它。私钥被一次"再跑一遍
    生成命令"覆盖掉之后，维护者手里的新私钥签出来的包，每台机器都验不过——**再也发不出
    任何更新**（包括那个本来能修好这件事的更新）。

    三种残局都必须拒绝，而且**每一种都可能单独出现**：
    - 两个都在：最常见（手滑重跑了一遍生成命令）。
    - **只剩私钥**（公钥被删了/挪走了）：这时挡住覆盖的只有 O_EXCL。
    - **只剩公钥**（私钥丢了——那正是最该停下来的时刻）：这时挡住覆盖的只有对 channel.pub
      的那道检查。而这半个真相恰恰是最要紧的：那把公钥可能正烧在某台已经装出去的机器上，
      盖掉它 = 连"这台机器还认哪把钥匙"都不知道了。
    """
    kf, pf = generate_keypair(tmp_path)
    if survivor == "只剩私钥":
        pf.unlink()
    if survivor == "只剩公钥":
        kf.unlink()
    before = {p: p.read_bytes() for p in (kf, pf) if p.exists()}

    with pytest.raises(FileExistsError):
        generate_keypair(tmp_path)

    assert {p: p.read_bytes() for p in (kf, pf) if p.exists()} == before  # 一个字节都不许动


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_private_key_is_not_world_readable(tmp_path: Path):
    kf, _ = generate_keypair(tmp_path)
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600


def test_signature_of_a_tampered_manifest_does_not_verify(tmp_path: Path):
    """签的是清单**正文字节**：拿到 OSS 写权限但没有私钥的人改不动它。"""
    kf, pf = generate_keypair(tmp_path)
    priv_hex, pub_hex = kf.read_text(encoding="utf-8"), pf.read_text(encoding="utf-8")
    sig = sign(b'{"version":"0.1.0"}', priv_hex)
    with pytest.raises(InvalidSignature):
        verify_manifest(b'{"version":"9.9.9"}', sig, pub_hex)


# =============================================================================
# 打包：真正的判据是"更新器认不认这个包"
# =============================================================================


def test_a_real_updater_installs_the_package_we_just_built(tmp_path: Path, payload, key):
    """**这条测试是本 Task 的判据。** 把打出来的通道原样端给消费方（tools.updater 的
    apply_update，同一个函数），它必须完整地装上：切版本、出厂状态落到 data/、戳落下。

    它一条就覆盖了签名、sha256、包的内部结构（没有外层包裹目录）、factory/ 的形状。
    """
    out = tmp_path / "channel"
    _build(payload, out, key)

    root = tmp_path / "install"
    root.mkdir()
    workspace = tmp_path / "desktop" / "小助手"
    assert apply_update(root, out.as_uri(), key[1], workspace) == "0.1.0"

    version_dir = root / "versions" / "0.1.0"
    assert (version_dir / FACTORY_DIR_REL / FACTORY_SOUL).is_file()
    # 更新器随版本走：它是唯一的远程修复通道，它自己必须能被下一个包换掉。
    assert (version_dir / TOOLS_DIR_REL / "updater.py").is_file()
    assert (version_dir / BUILDER_DIR_REL / "paths.py").is_file()
    assert (version_dir / rel_path(ELECTRON_EXE_REL)).is_file()
    # 出厂状态真的落到了 data/（否则"发新版改 persona"这个能力静默失效）
    assert (root / "data" / "SOUL.md").is_file()
    assert (root / "data" / FACTORY_STAMP).read_text(encoding="utf-8") == "0.1.0"


def test_the_package_extracts_as_the_version_directory_itself(tmp_path: Path, payload, key):
    """包解压后**就是** versions/<v>/ 的内容——多一层外层目录，os.replace 之后路径全错。"""
    out = tmp_path / "channel"
    built = _build(payload, out, key)
    with zipfile.ZipFile(built["zip"]) as z:
        names = z.namelist()
    assert f"{FACTORY_DIR_REL}/{FACTORY_SOUL}" in names
    assert f"{TOOLS_DIR_REL}/updater.py" in names
    assert f"{BUILDER_DIR_REL}/paths.py" in names
    assert not [n for n in names if n.startswith("payload/")]


def test_the_manifest_covers_the_bytes_that_will_be_uploaded(tmp_path: Path, payload, key):
    """清单里的 sha256 必须是**磁盘上那个 zip** 的 sha256，不是"我们打算写的那个"。

    对不上的话，每台机器下完 895MB 之后抛 "sha256 mismatch：包被篡改" —— 一次正常发版
    被伪装成一次攻击，而且每次开机重演。
    """
    out = tmp_path / "channel"
    built = _build(payload, out, key)
    manifest = verify_manifest(
        Path(built["manifest"]).read_bytes(), Path(built["sig"]).read_bytes(), key[1]
    )
    assert manifest["version"] == "0.1.0"
    assert manifest["package"] == package_name("0.1.0") == Path(built["zip"]).name
    assert manifest["sha256"] == hashlib.sha256(Path(built["zip"]).read_bytes()).hexdigest()
    assert manifest["sha256"] == built["sha256"]


# =============================================================================
# 守卫：坏包绝不允许出厂（每一条删掉都会变红）
# =============================================================================


def test_refuses_a_payload_whose_factory_master_the_machines_would_reject(
    tmp_path: Path, payload, key
):
    """打包前必须跑**消费方的那道门**（assert_factory_complete，同一个函数）。

    不跑的话：每台机器下 895MB → 解压 2.87GB → 被拒 → 退出 1 → 每次开机重演一遍，永远。
    """
    (payload / FACTORY_DIR_REL / FACTORY_SOUL).unlink()
    out = tmp_path / "channel"
    with pytest.raises(ValueError, match=FACTORY_SOUL):
        _build(payload, out, key)
    assert not list(out.glob("*.zip"))  # 一个坏包连产出都不许有
    assert not (out / MANIFEST_NAME).exists()


def test_refuses_a_payload_that_carries_an_activation_code(tmp_path: Path, payload, key):
    """.env 在包里的任何角落都不行——不只是 factory/ 里（那是 assert_factory_complete
    管的范围）。快照来自装配机，而装配机上跑过 Hermes：hermes-agent/ 底下很可能就躺着
    一份维护者自己的 .env。

    它上了通道 = 维护者的激活码流向每一台机器。
    """
    leak = payload / "hermes-agent" / ENV_FILE
    leak.write_text("DASHSCOPE_API_KEY=sk-real", encoding="utf-8")
    with pytest.raises(ValueError, match=ENV_FILE):
        _build(payload, tmp_path / "channel", key)


def test_refuses_a_payload_that_carries_the_signing_key_by_content(tmp_path: Path, payload, key):
    """**私钥泄漏是这个项目里唯一无法回收的灾难。**

    公钥在装机时烧进 channel.json，机器上没有任何东西能换掉它。私钥一旦公开（包在
    公共读的 OSS 桶上，谁都能下），任何人都能签一个包 → 每台机器静默装上 → 任意代码
    执行，永远。所以哪怕它只是碰巧躺在 payload 的某个文件里（维护者把 secrets/ 拷进了
    payload、或者 payload 根就是仓库根），也必须当场炸掉。
    """
    priv_hex = key[0]
    (payload / "hermes-agent" / "notes.txt").write_text(
        f"备忘：通道私钥是 {priv_hex}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="私钥"):
        _build(payload, tmp_path / "channel", key)


def test_refuses_a_payload_that_carries_a_file_named_channel_key(tmp_path: Path, payload, key):
    """按**名字**也要拦一道：payload 里那把 channel.key 可能是**另一把**（上一代密钥、
    或者维护者备份的那把），内容对不上正在用的私钥，内容扫描就够不着它——而它照样是
    一把能签包的私钥。"""
    (payload / keys.KEY_FILE).write_text("00" * 32, encoding="utf-8")
    with pytest.raises(ValueError, match=keys.KEY_FILE):
        _build(payload, tmp_path / "channel", key)


def test_refuses_a_payload_whose_tools_cannot_be_imported(tmp_path: Path, payload, key):
    """**更新器必须能在机器上真的 import 起来。**

    tools/*.py 全是 `from builder.paths import ...`：包里少了 builder/paths.py，机器上
    的更新器每次开机 ModuleNotFoundError。更新器是唯一的远程修复通道——它起不来，这台
    机器就再也收不到任何东西，包括那个能救它的下一个发行版。
    """
    (payload / BUILDER_DIR_REL / "paths.py").unlink()
    with pytest.raises(ValueError, match="import"):
        _build(payload, tmp_path / "channel", key)


@pytest.mark.parametrize("missing", REQUIRED_IN_PAYLOAD)
def test_refuses_a_payload_missing_anything_the_machine_needs(
    tmp_path: Path, payload, key, missing: str
):
    """契约（builder/paths.py）点名的每一个落点都必须在包里。

    少了 electron.exe：长辈双击 → 启动器 spawn 失败 → 判"这个版本跑不了" → 两次之后
    回滚 + **永久拉黑**这个版本。少了 venv 里的 python.exe：更新器/启动器根本没有解释器
    可跑。这些都不是"下次再说"，是每台机器一装上就坏。
    """
    target = payload / rel_path(missing)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    with pytest.raises(ValueError, match="缺"):
        _build(payload, tmp_path / "channel", key)


def test_refuses_a_payload_that_contains_a_symlink(tmp_path: Path, payload, key):
    """zip 会把符号链接**指向的内容**原样打进包——链接指到维护者机器上的任何东西（~/.ssh、
    另一个项目的 .env），那份内容就随包发到每一台机器上。正常的 Windows 快照里没有符号链接。
    """
    outside = tmp_path / "维护者的私人文件.txt"
    outside.write_text("不该出现在任何人的机器上", encoding="utf-8")
    (payload / "hermes-agent" / "link.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        _build(payload, tmp_path / "channel", key)


def test_refuses_a_version_the_updater_cannot_parse(tmp_path: Path, payload, key):
    """版本号必须过消费方的 parse_version（纯数字 + 点）。

    "0.1.0-rc1" 签出去之后：更新器验签通过 → _manifest_fields 里 parse_version 抛
    ValueError → 一路逃到 main() → **每台机器每次开机 SystemExit(1)**，而通道里再也
    没有一个它能接受的清单。

    而且必须在**压任何一个字节之前**就拒掉：闸门全部先于产出，一个坏包连产出都不许有
    （把这一关挪到打完包之后，行为上"也会失败"，但产物目录里已经躺着一个 895MB 的坏包了）。
    """
    out = tmp_path / "channel"
    with pytest.raises(ValueError):
        _build(payload, out, key, version="0.1.0-rc1")
    assert not out.exists() or not list(out.glob("*.zip"))


@pytest.mark.parametrize("bad_key", ["", "   ", "не-hex", "00" * 5])
def test_refuses_a_private_key_it_cannot_even_load(tmp_path: Path, payload, bad_key: str):
    """钥匙先解一遍，再干别的。

    两个后果：(1) 解不开的钥匙要是等到压完 2.87GB 才在 sign() 那里炸，维护者白等十分钟；
    (2) **空钥匙**会让"payload 里有没有私钥"那道内容扫描的针变成 b""——它在每个文件里都
    "找得到"，而且扫描的重叠量会变成负数，把整个文件读进内存。
    """
    out = tmp_path / "channel"
    with pytest.raises(ValueError):
        build_release(payload, "0.1.0", out, bad_key, heartbeat=FAKE_HEARTBEAT)
    assert not out.exists() or not list(out.glob("*.zip"))


def test_refuses_to_build_into_the_payload_itself(tmp_path: Path, payload, key):
    """产物目录不能在 payload 里面——zip 会把自己打进自己。"""
    with pytest.raises(ValueError, match="payload"):
        _build(payload, payload / "channel", key)


def test_a_refused_build_can_never_reach_the_channel(tmp_path: Path, payload, key):
    """构建失败之后，out_dir 里绝不能留下任何"看起来可以发布"的东西。

    清单是最后一个落盘的，而包名带版本号：所以 out_dir 里的清单**永远**描述一次完整
    且已自验的构建。这条测试钉住的正是这个不变式。
    """
    out = tmp_path / "channel"
    _build(payload, out, key, version="0.1.0")  # 上一次成功的发布

    (payload / FACTORY_DIR_REL / FACTORY_SOUL).unlink()  # 现在把 payload 弄坏
    with pytest.raises(ValueError):
        _build(payload, out, key, version="0.2.0")

    assert not (out / package_name("0.2.0")).exists()
    manifest = json.loads((out / MANIFEST_NAME).read_bytes())
    assert manifest["version"] == "0.1.0"  # 还是上一次那份完整的、自洽的清单
    assert all("0.2.0" not in " ".join(cmd) for cmd in publish_commands(out, CHANNEL_BUCKET))


def test_refuses_a_package_wrapped_in_an_outer_directory(tmp_path: Path, payload, key, monkeypatch):
    """打包器在清单落盘前会**打开自己刚写的 zip 数一遍成员**。这里注入的就是它防的那个
    bug——而且这个 bug 在计划原文里是活的：`shutil.make_archive` 的 root_dir 一写错，包里
    就多出一层外层目录。

    后果：解压出来的 versions/<版本号>/ 里只有一个 payload/ 文件夹，机器上什么都没有。
    每台机器：下 895MB → 解压 2.87GB → 出厂母版闸门拒收 → 退出 1 → 每次开机重演，永远。
    """
    real = release._archive_members
    monkeypatch.setattr(
        "builder.release._archive_members",
        lambda root: ((p, f"payload/{a}") for p, a in real(root)),
    )
    out = tmp_path / "channel"
    with pytest.raises(ValueError, match="少了机器一定会去找的东西"):
        _build(payload, out, key)
    assert not (out / MANIFEST_NAME).exists()  # 坏包绝不许留下一份可发布的清单


def test_refuses_a_signature_the_machines_would_not_accept(tmp_path: Path, payload, key, monkeypatch):
    """清单必须是**烧进机器的那把公钥**验得过的。签错了钥匙（维护者手里有好几把、或者
    keys.py 哪天改坏了），每台机器都会验签失败 → SystemExit(1) → 每次开机重演。"""
    wrong_key, _ = generate_keypair(tmp_path / "另一把")
    wrong_hex = wrong_key.read_text(encoding="utf-8")
    monkeypatch.setattr("builder.release.sign", lambda data, priv_hex: sign(data, wrong_hex))
    out = tmp_path / "channel"
    with pytest.raises(InvalidSignature):
        _build(payload, out, key)
    assert not (out / MANIFEST_NAME).exists()
    assert not (out / SIG_NAME).exists()


def test_refuses_to_sign_with_a_key_that_is_not_the_expected_pubkey(tmp_path: Path, payload, key):
    """`--expect-pubkey`：唯一能在**打包时**抓住"签错了钥匙"的守卫。

    _verify_as_a_machine_would 用**同一把** priv_hex 现推公钥来自验，所以它对**任何**合法私钥
    都通过——维护者要是把 --key 指到一把**已经作废/被换掉**的旧密钥上，打包器照签不误，而每台
    机器（channel.json 里烧的是新公钥）都会验签失败 → SystemExit(1) → 更新通道停摆，直到用对的
    钥匙重新发一版。给了期望公钥（Task 8 烧进 channel.json 的那把），签名前核对私钥推出来的公钥
    是否等于它，对不上就当场炸——一个坏签名连一个 zip 都不许产出。
    """
    _, superseded_pub = generate_keypair(tmp_path / "superseded")
    superseded_hex = superseded_pub.read_text(encoding="utf-8")
    assert superseded_hex != key[1]  # 两把不同的钥匙
    out = tmp_path / "channel"
    with pytest.raises(ValueError, match="公钥"):
        build_release(
            payload, "0.1.0", out, key[0], heartbeat=FAKE_HEARTBEAT, expect_pubkey=superseded_hex
        )
    assert not out.exists() or not list(out.glob("*.zip"))  # 坏签名连产出都不许有


def test_the_expected_pubkey_guard_passes_for_the_matching_key(tmp_path: Path, payload, key):
    """给对了公钥（私钥现推的那把）就照常构建——守卫只拦"对不上"，绝不误伤正确的钥匙。"""
    out = tmp_path / "channel"
    built = build_release(
        payload, "0.1.0", out, key[0], heartbeat=FAKE_HEARTBEAT, expect_pubkey=key[1]
    )
    assert Path(built["zip"]).is_file()


def test_a_same_version_dirty_rebuild_cannot_publish_an_inconsistent_trio(
    tmp_path: Path, payload, key
):
    """**同版本重构建**（build → 检查 → 修 → 重构建 → 发布一次，合法的发版前工作流）绝不能在
    通道上留下"新 zip + 旧清单"这种 sha 对不上的三件套。

    0.1.0 干净构建一次（好的 zip+清单+签名都在 out 里）。随后 payload 根被塞进一份陈旧
    heartbeat.json（--payload 传上一版本解出来的 payload 的常态），这一版又显式 --no-heartbeat
    重构建 0.1.0：_write_zip 的 os.replace **原子覆盖**掉上一份好 zip，而事后 _verify 的"心跳
    次数"校验（数出 1、这次要求 0）才炸——清单/签名没被重写，还描述着上一份的 sha。

    两道防线都必须成立：
      (1) build_release 失败时删掉刚覆盖出来的 zip（out 里那个版本从此没有包，而不是一个跟存活
          清单对不上的包）；
      (2) publish_commands 发布前重新 hash 一遍包、跟清单的 sha256 对一遍。
    否则 publish_commands 会照着旧清单发出去 {新 zip, 旧清单, 旧签名} → 每台机器下完 895MB 后
    sha256 不匹配 → 每次开机 SystemExit(1)、永远装不上、895MB 白下、current.txt 永不前进。
    """
    out = tmp_path / "channel"
    _build(payload, out, key, version="0.1.0")  # 干净的一次：好三件套落盘
    good_manifest = json.loads((out / MANIFEST_NAME).read_bytes())

    # 上一版本解出来的 payload 按构造带着一份陈旧 heartbeat.json；这一版显式弃权。
    (payload / HEARTBEAT_CRED_FILE).write_text(json.dumps({"stale": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="陈旧"):
        build_release(payload, "0.1.0", out, key[0], heartbeat=None)

    # 防线 (1)：被覆盖的 zip 不许留下——否则它跟存活的旧清单凑成对不上的三件套。
    assert not (out / package_name("0.1.0")).exists()
    # 清单/签名还是干净那次的（这次失败的构建没碰过它们）。
    assert json.loads((out / MANIFEST_NAME).read_bytes()) == good_manifest
    # 防线 (2)：无论如何，publish_commands 绝不发布一个跟清单对不上（或缺失）的包。
    with pytest.raises(ValueError):
        publish_commands(out, CHANNEL_BUCKET)


# =============================================================================
# 心跳凭证（ADR-0004）：随版本下发，权限收到最窄
# =============================================================================


def test_the_heartbeat_credential_ships_inside_the_signed_package(tmp_path: Path, payload, key):
    """凭证随**版本**走（而不是装机时烧进 channel.json）：

    - 它是唯一能轮换它的路径——channel.json 装完就再也不会被任何东西改写；
    - 它落在签名覆盖的范围里（清单签的 sha256 覆盖整个 zip）；
    - 它**不在 factory/ 里**，所以 restore_factory_files 绝不会把它拷进 data/。
    """
    out = tmp_path / "channel"
    built = _build(payload, out, key)
    with zipfile.ZipFile(built["zip"]) as z:
        assert json.loads(z.read(HEARTBEAT_CRED_FILE)) == FAKE_HEARTBEAT
        assert not [n for n in z.namelist() if n.startswith(f"{FACTORY_DIR_REL}/heartbeat")]

    root = tmp_path / "install"
    root.mkdir()
    apply_update(root, out.as_uri(), key[1], tmp_path / "desktop" / "小助手")
    assert (root / "versions" / "0.1.0" / HEARTBEAT_CRED_FILE).is_file()
    assert not (root / "data" / HEARTBEAT_CRED_FILE).exists()  # 绝不进用户的 data/


def test_a_release_without_a_heartbeat_carries_no_credential(tmp_path: Path, payload, key):
    """heartbeat=None 是**显式**弃权（关键字参数，没有默认值：不许"忘了传"）。"""
    built = build_release(payload, "0.1.0", tmp_path / "channel", key[0], heartbeat=None)
    with zipfile.ZipFile(built["zip"]) as z:
        assert HEARTBEAT_CRED_FILE not in z.namelist()


def test_building_a_release_forces_an_explicit_heartbeat_decision(tmp_path: Path, payload, key):
    """漏传 = TypeError，不是"静默地不带心跳"。带不带心跳是维护者的决定，不是默认值。"""
    with pytest.raises(TypeError):
        build_release(payload, "0.1.0", tmp_path / "channel", key[0])


@pytest.mark.parametrize(
    "broken",
    [
        {"access_key_secret": ""},  # 空 = 每次 PUT 都 403，心跳静默死掉
        {"access_key_id": "   "},
        {"access_key_id": "<你的 AK>"},  # 占位符没填
        {"bucket": ""},
        {"prefix": "hb"},  # 跟 RAM 策略允许的前缀对不上 → 每次 PUT 403
    ],
)
def test_refuses_a_heartbeat_credential_that_would_silently_403(
    tmp_path: Path, payload, key, broken: dict
):
    """坏凭证不会吵——它只是让每一次 PUT 静静地 403。

    后果比"没有心跳"更坏：维护者以为自己有可见性（`ossutil ls` 一片空白 = "所有机器都
    掉队了"？还是"凭证坏了"？），实际上一台机器的死活他都不知道。
    """
    with pytest.raises(ValueError):
        _build(payload, tmp_path / "channel", key, heartbeat={**FAKE_HEARTBEAT, **broken})


def test_refuses_a_heartbeat_credential_with_unknown_fields(tmp_path: Path, payload, key):
    """字段名打错（access_key_secrets）= 更新器读不到它要的键 = 心跳静默死掉。"""
    cred = {**FAKE_HEARTBEAT, "acces_key_secret": "typo"}
    with pytest.raises(ValueError):
        _build(payload, tmp_path / "channel", key, heartbeat=cred)


def test_refuses_to_ship_the_signing_key_as_a_heartbeat_credential(tmp_path: Path, payload, key):
    """凭证是**注进 zip 里**的，绕开了对 payload 那遍私钥扫描——所以这里必须单独再拦一次。"""
    cred = {**FAKE_HEARTBEAT, "access_key_secret": key[0]}
    with pytest.raises(ValueError, match="私钥"):
        _build(payload, tmp_path / "channel", key, heartbeat=cred)


def test_refuses_a_stale_heartbeat_credential_already_sitting_in_the_payload_without_a_fresh_one(
    tmp_path: Path, payload, key
):
    """--payload 存在正是为了让维护者传一份**已经装配好**的 payload——比如从上一个版本的
    包里解出来的那份，它按构造已经带着上一版本的 heartbeat.json。这一版显式弃权
    （--no-heartbeat）时，payload 根目录下那份陈旧凭证必须当场拒绝：`_verify_as_a_machine_would`
    数出来的次数（1）跟这次的心跳决定（要求 0 次）对不上。"""
    (payload / HEARTBEAT_CRED_FILE).write_text(json.dumps({"stale": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="陈旧"):
        build_release(payload, "0.1.0", tmp_path / "channel", key[0], heartbeat=None)


def test_refuses_a_stale_heartbeat_credential_already_sitting_in_the_payload_with_a_fresh_one(
    tmp_path: Path, payload, key
):
    """**这条钉住 `_write_zip` 那道"事前"守卫**（另一道数次数的守卫由上一条测试钉住）。payload
    根目录已经带着一份陈旧 heartbeat.json（同上，来自 --payload 传入的上一版本 payload），这一版
    又显式给了一份新鲜的 --heartbeat：不拦的话，zip 里最终会有**两个同名的** heartbeat.json 成员。

    两份重名的成员在 set 里看起来跟"只有一份"一模一样。CPython 的 `extractall` 恰好是后写的
    赢，侥幸装上了新鲜那份；但换一个"先到先得"的 zip 读取器，装上的就是陈旧凭证：这台
    机器往后每一次心跳 PUT 都会静默 403，而维护者会以为自己有可见性，实际上什么都看不见。
    所以 `_write_zip` 在压任何一个字节**之前**就拒绝，绝不靠"哪个实现赢了"这种运气。
    """
    (payload / HEARTBEAT_CRED_FILE).write_text(json.dumps({"stale": True}), encoding="utf-8")
    out = tmp_path / "channel"

    # ⚠️ match 的是 _write_zip 那道守卫**独有**的措辞（"重名成员"），别改回两道守卫共用的"陈旧"
    # ——否则这条测试就钉不住它了。把 _write_zip 里那道守卫删掉，zip 会照样被压出来（两个同名
    # 成员，zipfile 只发一句 UserWarning，而本项目默认跑测试时它不会变成错误），然后
    # _verify_as_a_machine_would 那道数次数的守卫会在事后把同一件事拦下来——**也**抛 ValueError、
    # 消息里**也**有"陈旧"。用"陈旧"匹配的话 pytest.raises 照样绿，一道被删掉的安全守卫悄无声息。
    # 两道守卫本该各自独立、各有各的红灯。
    #
    # 也不能改回靠"out 目录里的残留"来区分：build_release 事后的 except 会 unlink 掉刚覆盖出来的
    # zip（防"新 zip + 旧清单"三件套那道 Critical 修复），所以事前拒绝、还是事后拦下再 unlink，
    # out 最后都是空的——区分只能落在消息措辞上。
    with pytest.raises(ValueError, match="重名成员"):
        _build(payload, out, key)
    assert not list(out.iterdir())  # 被拒的构建不许留下任何产出（连一个 .part 都不许）


def test_the_ram_policy_grants_nothing_but_put_on_the_heartbeat_prefix():
    """这份凭证会随包扩散到维护者控制不了的机器上（包本身就挂在公共读的 OSS 上，谁都能
    下）。**必须当它已经泄漏了来设计。** 唯一能限住损失的是 RAM 策略：

    - 只 PutObject：不能读（别人的机器 ID 和版本）、不能列举（不能枚举所有机器）、
      **不能删**（所以"某台机器没有心跳"这个信号伪造不了——它恰恰是最要紧的那半）；
    - 只在 heartbeat/ 前缀里：够不到通道里的包和清单。
    """
    policy = heartbeat_ram_policy("xiaozhushou-heartbeat")
    assert policy["Statement"] == [
        {
            "Effect": "Allow",
            "Action": ["oss:PutObject"],
            "Resource": [f"acs:oss:*:*:xiaozhushou-heartbeat/{HEARTBEAT_PREFIX}/*"],
        }
    ]
    # 结构上再钉一遍（上面那条相等断言以后被人"顺手扩一格"时，这几条会先红）：
    actions = [a for s in policy["Statement"] for a in s["Action"]]
    assert actions == ["oss:PutObject"]  # 不许有第二个动作，尤其不许有 oss:* 这种通配
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        for resource in statement["Resource"]:
            assert resource.endswith(f"/{HEARTBEAT_PREFIX}/*")  # 够不到通道里的包和清单


# =============================================================================
# 发布：上传顺序 + 绝不 cp 整个目录
# =============================================================================


def test_publish_uploads_named_files_only_never_a_directory(tmp_path: Path, payload, key):
    """**绝不能 `ossutil cp -rf <目录> oss://.../channel/`。**

    那会把产物目录里**所有**东西传上一个公共读的桶——维护者只要有一次把 channel.key
    （或任何别的东西）放在了那个目录里，私钥就上了公网。而私钥泄漏是不可回收的：公钥
    烧在每台机器的 channel.json 里，换不掉。
    """
    out = tmp_path / "channel"
    built = _build(payload, out, key)
    cmds = publish_commands(out, CHANNEL_BUCKET)
    sources = [cmd[-2] for cmd in cmds]
    assert sources == [built["zip"], built["sig"], built["manifest"]]
    for cmd in cmds:
        assert cmd[0] == "ossutil"
        assert Path(cmd[-2]).is_file()  # 每一条上传的都是**一个文件**
        assert "-r" not in cmd and "-rf" not in cmd
        assert cmd[-1] == f"oss://{CHANNEL_BUCKET}/channel/"


def test_publish_uploads_the_package_before_the_manifest(tmp_path: Path, payload, key):
    """顺序是发布的原子性：清单是**唯一的生效开关**（跟机器上的 current.txt 一样）。

    清单先上去、包还在传的那几分钟里，每台开机的机器都会拿着新清单去下一个还不存在的
    包（404）——它自愈（静默重试），但通道在那段时间里是坏的。清单最后上，通道就永远
    自洽：机器要么看见旧版本，要么看见一个**已经完整躺在桶里**的新版本。
    """
    out = tmp_path / "channel"
    _build(payload, out, key)
    names = [Path(cmd[-2]).name for cmd in publish_commands(out, CHANNEL_BUCKET)]
    assert names.index(package_name("0.1.0")) < names.index(SIG_NAME) < names.index(MANIFEST_NAME)


def test_publish_refuses_when_the_signing_key_sits_in_the_release_dir(tmp_path: Path, payload, key):
    out = tmp_path / "channel"
    _build(payload, out, key)
    (out / keys.KEY_FILE).write_text(key[0], encoding="utf-8")
    with pytest.raises(ValueError, match=keys.KEY_FILE):
        publish_commands(out, CHANNEL_BUCKET)


def test_publish_refuses_an_incomplete_build(tmp_path: Path, payload, key):
    """没有清单 = 没有一次完整的构建。绝不能发布"半个通道"。"""
    out = tmp_path / "channel"
    out.mkdir()
    with pytest.raises(ValueError, match=MANIFEST_NAME):
        publish_commands(out, CHANNEL_BUCKET)

    _build(payload, out, key)
    (out / package_name("0.1.0")).unlink()  # 清单指着的包不见了
    with pytest.raises(ValueError, match=package_name("0.1.0")):
        publish_commands(out, CHANNEL_BUCKET)


def test_publish_refuses_a_package_whose_sha256_no_longer_matches_the_manifest(
    tmp_path: Path, payload, key
):
    """发布前必须**以机器的视角**重新 hash 一遍包、跟已签名清单里的 sha256 对一遍——这是字节
    进公共读桶之前的最后一道闸门，绝不能信"磁盘上这三样天生自洽"。

    最典型的走到这里的路：一次同版本重构建覆盖了 dist-<v>.zip，却没重写清单（构建在覆盖之后、
    重写清单之前失败了）。此时清单/签名描述的是**上一份**包的 sha。照着发出去，每台机器下完
    895MB 后 sha256 不匹配 → 每次开机 SystemExit(1)、永远装不上。
    """
    out = tmp_path / "channel"
    built = _build(payload, out, key)  # 好三件套
    # 把包换成另一份**合法但内容不同**的 zip（= 磁盘上的包不再等于清单里签过的 sha）。
    with zipfile.ZipFile(built["zip"], "w") as z:
        z.writestr("不一样的字节.txt", "x")
    with pytest.raises(ValueError, match="sha256"):
        publish_commands(out, CHANNEL_BUCKET)


def test_publish_refuses_a_heartbeat_credential_aimed_at_the_channel_bucket(
    tmp_path: Path, payload, key
):
    """心跳桶必须**独立于**通道桶。

    通道桶是**公共读**的（更新器匿名 GET）。心跳落在里面 = 任何人都能匿名列举/读取所有
    机器的 ID、版本和时间戳。而且那把随包扩散出去的写凭证，从此就有了对通道桶的写权限
    ——离"覆盖清单"只差一次策略写错。心跳桶应该是一个**私有的、除了心跳什么都没有的桶**。
    """
    out = tmp_path / "channel"
    _build(payload, out, key, heartbeat={**FAKE_HEARTBEAT, "bucket": CHANNEL_BUCKET})
    with pytest.raises(ValueError, match="公共读|独立"):
        publish_commands(out, CHANNEL_BUCKET)


# =============================================================================
# 装配：payload 是怎么长出来的
# =============================================================================


def test_assemble_payload_ships_the_tools_that_can_repair_the_machine(payload: Path):
    """tools/ 必须随版本走：更新器是唯一的远程修复通道，它自己不可更新的话，它的 bug
    就永远修不掉。builder/paths.py 也必须跟着——tools/* 全都 import 它。"""
    for name in ("updater.py", "launcher.py", "recover.py", "factory_state.py", "__init__.py"):
        assert (payload / TOOLS_DIR_REL / name).is_file()
    assert (payload / BUILDER_DIR_REL / "paths.py").is_file()
    # 只带路径契约：签名代码（keys.py）和打包代码（release.py）跟机器无关，不进包。
    assert not (payload / BUILDER_DIR_REL / "keys.py").exists()
    assert not (payload / BUILDER_DIR_REL / "release.py").exists()
    assert not list(payload.rglob("__pycache__"))


def test_assemble_payload_renders_the_factory_master(payload: Path):
    assert_factory_complete(payload / FACTORY_DIR_REL)
    assert (payload / FACTORY_DIR_REL / "skills" / "creative" / "ascii-art" / "SKILL.md").is_file()


def test_assemble_payload_refuses_a_dirty_destination(tmp_path: Path, snapshot, skills_src):
    """脏目录 = 上一次装配（或者随手扔进去的任何东西）会**静默地**混进这个发行版。"""
    dest = tmp_path / "payload"
    dest.mkdir()
    (dest / "leftover.txt").write_text("上一次的残留", encoding="utf-8")
    with pytest.raises(ValueError):
        assemble_payload(snapshot, skills_src, dest, "0.1.0")


def test_assemble_payload_refuses_a_snapshot_that_is_missing_the_desktop_app(
    tmp_path: Path, snapshot, skills_src
):
    (snapshot / rel_path(ELECTRON_EXE_REL)).unlink()
    with pytest.raises(ValueError, match="缺"):
        assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.0")


def test_assemble_payload_refuses_to_ship_an_incomplete_tool_layer(
    tmp_path: Path, snapshot, skills_src, monkeypatch
):
    """装配也要单独把这一关看住（Task 8 的安装器 payload 走的就是 assemble，不一定经过
    build_release 那道 import 探针）：少一个工具 = 机器上少一条修复通道，而且完全静默
    ——双击图标没反应（launcher.py）、修复按钮没反应（recover.py）、再也收不到更新
    （updater.py）。"""
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / "tools").mkdir(parents=True)
    (fake_repo / "tools" / "updater.py").write_text("", encoding="utf-8")  # 只剩一个
    monkeypatch.setattr("builder.release.REPO_ROOT", fake_repo)
    with pytest.raises(ValueError, match="launcher.py"):
        assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.0")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def test_assemble_payload_cleans_a_dirty_hermes_agent_git_worktree(
    tmp_path: Path, snapshot, skills_src
):
    """真机 2026-07-20 复现过的故障：装配机上 `git checkout` 之后，一批文本文件在 git
    眼里"被改动"——即使内容跟 HEAD 逐字节等价（用 `git diff --ignore-all-space` 验证过：
    忽略空白后的 diff 是空的，Windows 上的换行符处理是头号嫌疑）。这份脏工作区如果被
    原样打进包，机器首次启动时 Hermes 自己的 bootstrap 会跑 `git checkout <钉死的
    commit>`——git 一看工作区有本地修改，为了不丢数据直接 Aborting，退出码 1，桌面端
    弹 "Hermes couldn't start"。跟国内网络能不能连没有任何关系：硅谷这台装配机今天就
    复现过一次，靠真人在故障机上手动 `git checkout -f` 才绕过去。

    assemble_payload 必须在装配时就把 hermes-agent 的 git 工作区强制清成跟 HEAD 一致
    ——这样机器上 Hermes 自己那次 checkout 永远拿到一个干净的仓库，不管装配机在打包过程中
    是怎么把它弄脏的。"""
    agent_dir = snapshot / rel_path(AGENT_DIR_REL)
    tracked = agent_dir / "tools" / "skills_guard.py"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("print('committed')\n", encoding="utf-8")
    _git(agent_dir, "init", "-q")
    _git(agent_dir, "config", "user.email", "test@example.com")
    _git(agent_dir, "config", "user.name", "test")
    _git(agent_dir, "add", "-A")
    _git(agent_dir, "commit", "-q", "-m", "initial")
    # 模拟装配机上真实发生的事：工作区被改动过、从未提交。
    tracked.write_text("print('modified on disk, never committed')\n", encoding="utf-8")
    assert _git(agent_dir, "status", "--porcelain").stdout.strip() != ""  # 前置条件：真的脏了

    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.0")

    status = _git(dest / rel_path(AGENT_DIR_REL), "status", "--porcelain")
    assert status.stdout.strip() == ""  # payload 里的仓库必须干净


def test_assemble_payload_tolerates_a_snapshot_with_no_git_repo(
    tmp_path: Path, snapshot, skills_src
):
    """快照里的 hermes-agent 不一定是 git 仓库（测试用的假快照、或者 Hermes 未来换成
    别的分发方式）。这道修复是"锦上添花"，不该在这种情况下把装配本身搞炸。"""
    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.0")
    assert (dest / rel_path(AGENT_DIR_REL)).is_dir()


def test_assemble_payload_refuses_a_hermes_agent_whose_git_repo_is_unusable(
    tmp_path: Path, snapshot, skills_src
):
    """这个模块几乎全是守卫，每条守卫都该有一条证明它真的会炸的测试——`_clean_git_worktree`
    也不例外：`.git` 存在但不是一个能用的仓库（比如装配机上被半途中断的一次 git 操作留下的
    残局）时，`git checkout -f` 本身会失败，必须响亮地炸掉，而不是把这个坏仓库悄悄打进包。"""
    agent_dir = snapshot / rel_path(AGENT_DIR_REL)
    (agent_dir / ".git").mkdir()  # 一个空目录，不是能用的 git 仓库
    with pytest.raises(ValueError, match="清理.*git 工作区失败"):
        assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.0")


def _pyvenv_cfg_path(snapshot: Path) -> Path:
    return (snapshot / rel_path(VENV_PYTHON_REL)).parent.parent / "pyvenv.cfg"


def test_assemble_payload_rewrites_the_venv_home_to_the_target_version(
    tmp_path: Path, snapshot, skills_src
):
    """真机 2026-07-21 复现过的故障：这次装 0.1.2 时复用了 0.1.0 快照里现成的 venv（省得
    重新走一遍 Windows Native 安装），但 venv 的 `pyvenv.cfg` 是**创建时**烧下的绝对路径
    ——`home` 那一行原样写着 `...\\versions\\0.1.0\\python-base`。全新机器上根本没有
    `versions\\0.1.0\\` 这个目录，Python 解释器启动时找不到 stdlib，直接
    `ModuleNotFoundError: No module named 'encodings'`——比 Hermes 自己的 bootstrap 还早，
    装机流程走到 `launcher --init` 那一步就以错误码 1 崩溃，装出一个只有 `.env` 的空壳。

    assemble_payload 必须在装配时把 `home` 重写成**这次装配的目标版本号**对应的路径，
    不能相信快照自带的那份是对的——快照本来就可能是从别的版本借来的。"""
    base_python_dir = BASE_PYTHON_REL.rsplit("\\", 1)[0]  # "python-base"
    cfg = _pyvenv_cfg_path(snapshot)
    cfg.write_text(
        "home = " + INSTALL_ROOT + "\\versions\\0.1.0\\" + base_python_dir + "\n"
        "implementation = CPython\n"
        "uv = 0.11.28\n"
        "version_info = 3.11\n"
        "include-system-site-packages = false\n",
        encoding="utf-8",
    )

    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.2")

    lines = _pyvenv_cfg_path(dest).read_text(encoding="utf-8").splitlines()
    home_lines = [ln for ln in lines if ln.split("=", 1)[0].strip() == "home"]
    assert home_lines == ["home = " + INSTALL_ROOT + "\\versions\\0.1.2\\" + base_python_dir]
    # 其它行原样保留——这道修复只改 home 这一行，不是重写整个文件。
    assert "uv = 0.11.28" in lines


def test_assemble_payload_tolerates_a_snapshot_with_no_pyvenv_cfg(
    tmp_path: Path, snapshot, skills_src
):
    """测试快照里的 venv 只是几个假字节（见 snapshot fixture），没有真的 `pyvenv.cfg`
    ——这不是这道修复要管的事（它只负责"文件存在但 home 指错了"这一种病），不该把装配
    本身搞炸。"""
    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.2")
    assert not _pyvenv_cfg_path(dest).exists()


def _install_ps1_path(root: Path) -> Path:
    return root / rel_path(AGENT_DIR_REL) / "scripts" / "install.ps1"


_CLONE_LINES = (
    "Invoke-NativeWithRelaxedErrorAction { git -c windows.appendAtomically=false"
    " clone --depth 1 --branch $Branch $RepoUrlSsh $InstallDir }\n"
    "Invoke-NativeWithRelaxedErrorAction { git -c windows.appendAtomically=false"
    " clone --depth 1 --branch $Branch $RepoUrlHttps $InstallDir }\n"
)


def test_assemble_payload_pins_autocrlf_on_the_hermes_clone(
    tmp_path: Path, snapshot, skills_src
):
    """真机 2026-07-21 复现过的故障：桌面端首次启动，Hermes 自己的 bootstrap 从 GitHub
    **现克隆**一份仓库到 `data\\hermes-agent`（打包进去的那份它根本不用），然后对这份新克隆
    跑 `git checkout <钉死的 commit>`——直接 abort：324 个文件"有本地修改"。

    刚克隆出来的仓库为什么是脏的：Git for Windows 的 system 层默认 `core.autocrlf=true`，
    克隆时把仓库里 LF 的文本文件全检出成 CRLF；而 install.ps1 是在**克隆之后**才
    `git config core.autocrlf false`。于是工作区是 CRLF、blob 是 LF、又不再做转换，
    git 逐字节比对 → 全员"已修改"。install.ps1 自己的注释把这个机制写得一字不差，
    只是那句 pin 落在了克隆之后——脏是在克隆那一刻就造成的。

    所以必须在 clone 命令**自己身上**加 `-c core.autocrlf=false`（跟它已经在传的
    `-c windows.appendAtomically=false` 并排）。真机验证过：删掉 data\\hermes-agent 逼出
    全新克隆路径，带这个补丁克隆完工作区干净、checkout 成功、正确钉到目标 commit。

    为什么可以改 Hermes 的源码：这跟 `_clean_git_worktree`、`_rewrite_venv_home` 是同一类
    装配期外科修复——"只消费不修改"说的是不 fork、不长期维护它的源码，不是装配期一个字节
    都不能碰。配一条守卫（见下一条测试）保证上游一改动构建立刻红。"""
    ps1 = _install_ps1_path(snapshot)
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(_CLONE_LINES, encoding="utf-8")

    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.5")

    text = _install_ps1_path(dest).read_text(encoding="utf-8")
    assert text.count("-c core.autocrlf=false") == 2  # SSH 和 HTTPS 两条路都得钉
    # 原来那个 -c 不能被顶掉：它治的是另一个病（杀软/OneDrive 导致的原子写失败）。
    assert text.count("-c windows.appendAtomically=false") == 2


def test_assemble_payload_refuses_an_install_ps1_whose_clone_it_cannot_find(
    tmp_path: Path, snapshot, skills_src
):
    """install.ps1 在、但里面找不到可以钉的 `git ... clone`——说明上游把这段改了。

    这时候**必须响亮地炸在装配机上**，而不是默默产出一个"看起来正常"的包：那个包装到
    爸妈机器上的表现是桌面端起不来、弹 "Hermes couldn't start"，而维护者在一万公里外
    完全不知道自己发了个坏版本。这正是本项目对静默失败的底线。

    注释里那句话是故意留的：install.ps1 本身就有大段解释这段克隆逻辑的注释，如果守卫把
    注释也算成"找到了 clone"，它就会在最需要它的那一刻（上游真把命令删了、只剩讲解它的
    注释）被骗过去。"""
    ps1 = _install_ps1_path(snapshot)
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(
        "# 上游重写了这段：原来这里是 git clone --depth 1，现在改走别的路了\n"
        "Write-Host 'hello'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="install.ps1"):
        assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.5")


def test_assemble_payload_does_not_pin_a_git_clone_inside_a_string_literal(
    tmp_path: Path, snapshot, skills_src
):
    """真实的 install.ps1 里有这么一行（0.1.5 装配时撞到的）：

        throw "Failed to download repository (tried git clone SSH, HTTPS, and ZIP)"

    那是**错误信息的文本**，不是要执行的命令。把 -c 插进去只会让维护者看到一条莫名其妙的
    报错；更要命的是守卫会因此产生假阴性——上游哪天真把三条 clone 命令都换掉、只剩这行
    错误信息，守卫就被这段字符串骗过去、照常产出一个坏包。

    跟跳过注释是同一个道理：只认真正处在命令位置上的 `git ... clone`。"""
    ps1 = _install_ps1_path(snapshot)
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(
        _CLONE_LINES
        + '        throw "Failed to download repository (tried git clone SSH, HTTPS, and ZIP)"\n',
        encoding="utf-8",
    )

    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.5")

    text = _install_ps1_path(dest).read_text(encoding="utf-8")
    assert text.count("-c core.autocrlf=false") == 2  # 两条真命令，不含那行字符串
    assert 'tried git clone SSH, HTTPS, and ZIP' in text  # 错误信息一个字都没被动过


def test_assemble_payload_refuses_when_the_only_git_clone_is_inside_a_string(
    tmp_path: Path, snapshot, skills_src
):
    """只剩一行提到 git clone 的**错误信息**、真正的克隆命令没了——守卫必须照炸不误。
    这是上一条测试那个假阴性真正会咬人的形态。"""
    ps1 = _install_ps1_path(snapshot)
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(
        '        throw "Failed to download repository (tried git clone SSH, HTTPS, and ZIP)"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="install.ps1"):
        assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.5")


def test_assemble_payload_tolerates_a_snapshot_with_no_install_ps1(
    tmp_path: Path, snapshot, skills_src
):
    """测试快照里没有 `scripts/install.ps1`（见 snapshot fixture）——这道修复只负责
    "文件在、但 clone 没钉 autocrlf"这一种病，不该把装配本身搞炸。"""
    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.5")
    assert not _install_ps1_path(dest).exists()


def test_assemble_payload_refuses_a_pyvenv_cfg_without_a_home_line(
    tmp_path: Path, snapshot, skills_src
):
    """`pyvenv.cfg` 存在但没有 `home` 这一行，说明它根本不是一个正常的 venv 配置文件
    ——装出来的 Python 解释器大概率起不来。响亮地炸掉，而不是悄悄放行一个改不了的坏文件。"""
    cfg = _pyvenv_cfg_path(snapshot)
    cfg.write_text("implementation = CPython\nversion_info = 3.11\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pyvenv.cfg"):
        assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.2")


def test_assemble_payload_accepts_a_pyvenv_cfg_whose_home_is_already_correct(
    tmp_path: Path, snapshot, skills_src
):
    """同版本装配（从 0.1.2 自己的快照装 0.1.2）时，`home` 本来就已经指对了目标版本，重写
    后内容一字不差。这不是"没有 home 行"（那才该炸），而是"已经对了"，必须照常放行——
    别让"重写后没变化"被误判成畸形文件。"""
    base_python_dir = BASE_PYTHON_REL.rsplit("\\", 1)[0]  # "python-base"
    already_correct = "home = " + INSTALL_ROOT + "\\versions\\0.1.2\\" + base_python_dir
    cfg = _pyvenv_cfg_path(snapshot)
    cfg.write_text(already_correct + "\nimplementation = CPython\n", encoding="utf-8")

    dest = assemble_payload(snapshot, skills_src, tmp_path / "payload", "0.1.2")

    lines = _pyvenv_cfg_path(dest).read_text(encoding="utf-8").splitlines()
    assert already_correct in lines
    assert "implementation = CPython" in lines
