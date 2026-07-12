# tests/test_updater.py
"""更新客户端的安全测试。

这段代码跑在“Windows 上登录时触发的计划任务”里，非交互、无人看 stderr，
安装根是一万公里外维护者管不到的爸妈的机器。测试因此不只覆盖“正常升级
成功”这一条路径，还要钉死三个判断题的结论：

1. 原子性——下载/解压中途失败，机器必须“完全没变”，不能半新半旧。
2. 磁盘膨胀——versions/ 只保留 current + previous 两份，不会无限堆积。
3. 网络不可达——是静默任务的常态，不是异常：拉不到清单要静默返回 None，
   不能让计划任务崩溃退出。

反过来，“签名校验失败”“sha256 不匹配”“黑名单版本”“清单不是合法 JSON”
这几条必须清晰地失败（抛异常/返回 None 且不落地任何文件）——“对失败宽容”
只覆盖“连不上网”，绝不能覆盖“内容不可信”。
"""
import base64
import contextlib
import hashlib
import http.server
import json
import os
import threading
import tracemalloc
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from builder.paths import FACTORY_STAMP, WORKSPACE_DIRNAME
from tools import updater
from tools.updater import apply_update, parse_version, verify_manifest


def _ws(tmp_path: Path) -> Path:
    """桌面上的「小助手」工作台目录。

    测试**必须**显式传它：apply_update 会把它渲染进 config.yaml，还会在它不存在时
    建出来。如果让 apply_update 自己去推导"真实桌面"，跑一次测试就会在开发机
    （和 CI）的 ~/Desktop 下真的长出一个「小助手」文件夹。推导只发生在 main()。
    """
    return tmp_path / "desktop" / WORKSPACE_DIRNAME


def _publish(ch_dir: Path, version: str, priv: Ed25519PrivateKey, payload: bytes = b"") -> None:
    """用同一把密钥在通道目录里发布一个版本：包 + 清单 + 签名。可以对同一个
    目录反复调用来追加发布新版本（模拟维护者连续发版，用于测试“连续更新两次，
    旧版本会不会被清理”）。payload 用来发布一个“体量真实”的大包（不压缩存储），
    给内存峰值测试用。

    包里必须带一份形状完整的 factory/（出厂母版）——真实发行版就是这个形状，
    而且更新器会在切版本之前校验它。"""
    pkg = ch_dir / f"dist-{version}.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("marker.txt", version)
        z.writestr("factory/config.yaml.tmpl", f'model:\n  default: "{version}"\nterminal:\n  cwd: "{{{{WORKSPACE_DIR}}}}"\n')
        z.writestr("factory/SOUL.md", f"我是小助手 {version}")
        z.writestr("factory/skills/creative/ascii-art/SKILL.md", f"出厂技能 {version}")
        if payload:
            z.writestr(zipfile.ZipInfo("blob.bin"), payload, compress_type=zipfile.ZIP_STORED)
    manifest = json.dumps({
        "version": version,
        "package": pkg.name,
        "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest(),
    }).encode()
    (ch_dir / "manifest.json").write_bytes(manifest)
    (ch_dir / "manifest.sig").write_bytes(base64.b64encode(priv.sign(manifest)))


@contextlib.contextmanager
def _serve(ch_dir: Path, truncate_zip: bool = False):
    """把通道目录用一个真的 HTTP 服务器端出来（生产里通道是 OSS 上的 HTTP）。

    file:// 太宽容，掩盖了两类真实故障，所以这两条必须用真 socket 测：
    1. truncate_zip=True：声明 Content-Length: N，只发 N/2 字节就挂断——这就是
       brief 里点名的“下载中途断网”。file:// 永远造不出这种情况。
    2. 路径严格：`//manifest.json`（channel_url 末尾多一个斜杠拼出来的）在 OSS
       上是 404，而本地文件系统会把 `//` 当成 `/` 悄悄放过。
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 保持测试输出干净
            pass

        def do_GET(self):
            # 不能用 self.path：http.server 会把 `//x` 悄悄折叠成 `/x`（gh-87389
            # 防开放重定向），而 OSS 上 `//manifest.json` 是实打实的 404。回到原始
            # 请求行，忠实还原对象存储那种“路径就是 key，一个字符都不含糊”的行为。
            target = self.raw_requestline.decode("latin-1").split()[1]
            name = target[1:]
            if not target.startswith("/") or "/" in name or not (ch_dir / name).is_file():
                self.send_error(404)
                return
            body = (ch_dir / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if truncate_zip and name.endswith(".zip"):
                body = body[: len(body) // 2]  # 声明了 N，只发一半就挂断
                self.close_connection = True
            with contextlib.suppress(OSError):
                self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _channel(
    tmp_path: Path, version: str, payload: bytes = b""
) -> tuple[Path, str, Ed25519PrivateKey]:
    """构造一个本地目录形式的“通道”，更新器用 `file://` URL（ch.as_uri()）访问。
    返回 (channel_dir, pubkey_hex, priv)——priv 留给需要用同一把密钥连续发布多个
    版本的测试复用（模拟维护者连续发版），不必每次都换新密钥。payload 透传给
    _publish，用来发布一个“体量真实”的大包（给内存峰值测试用）。
    """
    ch = tmp_path / "channel"
    ch.mkdir(exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    _publish(ch, version, priv, payload)
    return ch, priv.public_key().public_bytes_raw().hex(), priv


def test_parse_version_orders_correctly():
    assert parse_version("0.10.0") > parse_version("0.9.9")


def test_apply_update_installs_new_version(tmp_path: Path):
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    # 一台真实的、跑在 0.1.0 上的机器：current.txt 指向 0.1.0，而 versions/0.1.0
    # 就在磁盘上。这个前提是 previous.txt 能被写成 "0.1.0" 的**前提条件**——写一个
    # 磁盘上不存在的版本号进 previous.txt 就是伪造回滚目标，见
    # test_missing_current_txt_neither_prunes_the_fallback_nor_fakes_a_rollback_target。
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    assert (root / "versions/0.1.1/marker.txt").read_text() == "0.1.1"
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"


def test_apply_update_noop_when_up_to_date(tmp_path: Path):
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.1", encoding="utf-8")
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) is None


def test_bad_signature_rejected(tmp_path: Path):
    ch, _, _ = _channel(tmp_path, "0.1.1")
    wrong_pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    with pytest.raises(InvalidSignature):
        apply_update(root, ch.as_uri(), wrong_pub, _ws(tmp_path))
    # 签名不对 = 通道可能被冒充：拒绝必须是原子的，不能已经落了一半文件。
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "previous.txt").exists()
    assert not (root / "versions").exists()


def test_sha256_mismatch_rejected(tmp_path: Path):
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    pkg = next(ch.glob("dist-*.zip"))
    pkg.write_bytes(pkg.read_bytes() + b"tampered")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        apply_update(root, ch.as_uri(), pub, _ws(tmp_path))
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "versions").exists()


def test_bad_version_is_not_reinstalled(tmp_path: Path):
    """Task 6 启动器会把启动即崩的版本写进 bad_versions.txt。更新器如果无视
    黑名单，就会陷入“装 → 崩 → 回滚 → 又装 → 又崩”的死循环，机器彻底废掉。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    (root / "bad_versions.txt").write_text("0.1.1\n", encoding="utf-8")
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) is None
    assert not (root / "versions" / "0.1.1").exists()
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"


def test_network_unreachable_returns_none_not_crash(tmp_path: Path):
    """静默计划任务每次开机都跑，机器经常没网、通道一时够不到。这必须是
    “正常返回 None”，而不是抛异常把计划任务弄崩——没有人会看那个 traceback。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    unreachable_url = (tmp_path / "does-not-exist").as_uri()
    fake_pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    assert apply_update(root, unreachable_url, fake_pub, _ws(tmp_path)) is None
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"


def test_signed_but_not_json_manifest_raises_instead_of_silently_ignored(tmp_path: Path):
    """签名验证通过，但 manifest 内容本身不是合法 JSON——这是维护者打包流程
    出了真 bug，属于“内容可信但格式错误”，不该被“网络不可达”那条宽容路径
    捎带吞掉，必须清晰地报错，而不是静默当成“无更新”放过。"""
    ch = tmp_path / "channel"
    ch.mkdir()
    priv = Ed25519PrivateKey.generate()
    garbage = b"this is not json"
    (ch / "manifest.json").write_bytes(garbage)
    (ch / "manifest.sig").write_bytes(base64.b64encode(priv.sign(garbage)))
    pub_hex = priv.public_key().public_bytes_raw().hex()
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        apply_update(root, ch.as_uri(), pub_hex, _ws(tmp_path))


def test_disk_does_not_grow_unbounded_across_multiple_updates(tmp_path: Path):
    """连续更新两次（0.1.0 → 0.1.1 → 0.1.2），每个版本 2.87GB 的现实体量下，
    versions/ 不能三个版本都留着——只留 current + previous 两份；回滚目标
    (previous) 绝不能被清理掉。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.0.9", encoding="utf-8")

    ch, pub, priv = _channel(tmp_path, "0.1.0")
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.0"
    assert (root / "versions" / "0.1.0").is_dir()

    _publish(ch, "0.1.1", priv)
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    # 刚更新到 0.1.1：previous 是 0.1.0，是回滚目标，不能删。
    assert (root / "versions" / "0.1.0").is_dir()
    assert (root / "versions" / "0.1.1").is_dir()

    _publish(ch, "0.1.2", priv)
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.2"
    # 再更新一次：previous 变成 0.1.1，再往前的 0.1.0 不再是任何人的回滚
    # 目标，必须被清理掉，否则 2.87GB/版本会无限堆积。
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (root / "versions" / "0.1.2").is_dir()
    assert (root / "versions" / "0.1.1").is_dir()
    assert not (root / "versions" / "0.1.0").exists()


def test_crash_mid_extraction_leaves_current_untouched(tmp_path: Path):
    """模拟“解压到一半断电”：current.txt 必须停在旧版本，新版本目录不能
    以“最终名字”出现——切换用的是目录级原子 rename，只有解压完全成功之后
    才会把 staging 目录改名成 versions/<version>。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with patch("zipfile.ZipFile.extractall", side_effect=RuntimeError("simulated power loss")):
        with pytest.raises(RuntimeError):
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "versions" / "0.1.1").exists()


def test_recovers_cleanly_after_a_crashed_attempt(tmp_path: Path):
    """上一条测试模拟的“半解压崩溃”会在 versions/ 下留一个 staging 垃圾。
    这不能永久占着磁盘——下一次（断电/断网恢复后的）运行必须能清掉它，
    并正常完成更新，不留任何临时产物。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with patch("zipfile.ZipFile.extractall", side_effect=RuntimeError("simulated power loss")):
        with pytest.raises(RuntimeError):
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    versions_dir = root / "versions"
    assert not list(versions_dir.glob(".s-*"))
    assert not (root / "download.tmp.zip").exists()


def test_second_instance_during_extraction_cannot_brick_current(tmp_path: Path):
    """两个实例同时跑（同一台机器上两个 Windows 账号各自的 ONLOGON 计划任务
    指向同一个共享安装根 C:\\Users\\Public\\xiaozhushou；或维护者远程手动跑更新
    时计划任务正好在下载）。解压一个 2.87GB 的包要几十秒，重叠窗口很宽。

    不加锁的话：后来者的 _cleanup_stale_temp 会删掉前一个实例正在用的 staging
    目录，两个实例还抢同一个最终目录名，最后 current.txt 指向一个已经被删掉的
    版本目录 = 开机即挂、一万公里外救不回来。这里直接在解压中途重入一次
    apply_update 来复现那个交错。"""
    ch, pub, _ = _channel(tmp_path, "0.1.2")
    root = tmp_path / "root"
    (root / "versions" / "0.1.1").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.1", encoding="utf-8")

    reentered, nested_result = [], []
    real_extractall = zipfile.ZipFile.extractall

    def extract_then_let_a_second_instance_run(self, path):
        real_extractall(self, path)
        if not reentered:  # 只重入一次，避免无限递归
            reentered.append(True)
            nested_result.append(apply_update(root, ch.as_uri(), pub, _ws(tmp_path)))

    with patch.object(zipfile.ZipFile, "extractall", extract_then_let_a_second_instance_run):
        apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    assert nested_result == [None]  # 第二个实例拿不到锁：安静让路，不是错误
    current = (root / "current.txt").read_text(encoding="utf-8").strip()
    assert (root / "versions" / current).is_dir()  # current.txt 绝不能指向一个不存在的目录
    assert not list((root / "versions").glob(".s-*"))


def test_mid_download_drop_returns_none_not_crash(tmp_path: Path):
    """下载到一半断网（服务器声明 Content-Length: N，只发了 N/2 就挂断）。

    http.client.IncompleteRead 不是 OSError 的子类，只 `except OSError` 会漏掉
    它——静默计划任务会以非零码崩溃退出，而没有人会看那个 stderr。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with _serve(ch, truncate_zip=True) as url:
        assert apply_update(root, url, pub, _ws(tmp_path)) is None

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "versions" / "0.1.1").exists()
    assert not (root / "download.tmp.zip").exists()


def test_trailing_slash_in_channel_url_is_normalized(tmp_path: Path):
    """channel.json 里的 url 末尾多打一个斜杠，拼出来就是 `//manifest.json`——
    在 OSS 上是 404（本地文件系统会悄悄放过，所以这条必须用真 HTTP 测）。
    404 → 静默 None → 所有机器都永远收不到更新，而且没有任何人会发现。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with _serve(ch) as url:
        assert apply_update(root, url + "/", pub, _ws(tmp_path)) == "0.1.1"


def test_download_does_not_buffer_the_whole_package_in_memory(tmp_path: Path):
    """包体 895MB。整包读进内存的峰值内存是包体的两倍（bytes + 写盘时的拷贝），
    约 1.8GB——爸妈那台 4GB 的笔记本在开机时（杀软和启动项都驻留着）很可能直接
    MemoryError。而一旦在这里 OOM，这台机器就再也收不到任何更新（包括安全
    更新）：更新通道本身死了。所以下载必须是流式的，峰值内存与包体大小无关。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1", payload=os.urandom(16 * 1024 * 1024))
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    tracemalloc.start()
    try:
        assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 6 * 1024 * 1024, f"下载了一个 16MB 的包，峰值内存 {peak / 1e6:.0f}MB——没有流式下载"
    assert (root / "versions" / "0.1.1" / "blob.bin").stat().st_size == 16 * 1024 * 1024


def test_missing_current_txt_neither_prunes_the_fallback_nor_fakes_a_rollback_target(tmp_path: Path):
    """current.txt 丢了（断电写到一半、磁盘错误、人手删），但磁盘上还躺着一个
    已知能启动的版本。

    这时绝不能拿 "0.0.0" 这种占位版本号继续往下走：它会一路流进 previous.txt
    （伪造出一个根本不存在的回滚目标——启动器回滚过去会发现目录是空的）和
    _prune 的 keep 集合（"0.0.0" 保护不了磁盘上任何东西，磁盘上唯一那个能启动
    的旧版本反而被当成垃圾剪掉）。一次运行同时毁掉安全网和回滚目标。

    没有 previous.txt = 诚实的“没有回滚目标”；previous.txt="0.0.0" = 一个看起来
    有效的假回滚目标，比没有更危险。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "versions" / "0.1.0" / "marker.txt").write_text("0.1.0", encoding="utf-8")

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (root / "versions" / "0.1.0").is_dir(), "磁盘上唯一一个能启动的旧版本被剪掉了"
    assert not (root / "previous.txt").exists(), "编造了一个不存在的回滚目标"


def test_crash_between_the_two_writes_leaves_a_bootable_rollbackable_state(tmp_path: Path):
    """previous.txt 必须先于 current.txt 落盘——这是模块里注释最重的那条不变量，
    但在此之前它只由注释担保：把两次写入调换顺序，整个测试套件依然全绿。

    这条测试让**第二次**写入失败（不管那次写的是哪个文件），把顺序钉死：
    - 现在的顺序（previous 先）：崩在第二次 = current.txt 没换 → 机器还是旧版本，
      能启动，回滚目标也还在。
    - 调换之后（current 先）：崩在第二次 = current.txt 已经指向新版本、previous.txt
      却还是上上个版本 —— 断言 current == "0.1.1" 直接红。
    """
    ch, pub, priv = _channel(tmp_path, "0.1.0")
    root = tmp_path / "root"
    root.mkdir()

    # 先跑出一个稳态：current=0.1.1、previous=0.1.0，两个版本目录都在磁盘上。
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.0"
    _publish(ch, "0.1.1", priv)
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"

    _publish(ch, "0.1.2", priv)
    real_atomic_write, calls = updater._atomic_write, []

    def flaky(path: Path, text: str) -> None:
        calls.append(path.name)
        if len(calls) == 2:  # 第二次写入断电——不管它写的是哪个文件
            raise RuntimeError("power loss between the two writes")
        real_atomic_write(path, text)

    with patch.object(updater, "_atomic_write", side_effect=flaky):
        with pytest.raises(RuntimeError):
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    current = (root / "current.txt").read_text(encoding="utf-8").strip()
    previous = (root / "previous.txt").read_text(encoding="utf-8").strip()
    assert current == "0.1.1"  # 生效开关没有被拨过去
    assert (root / "versions" / current).is_dir()  # current 指向一个真实存在的目录
    assert (root / "versions" / previous).is_dir()  # 回滚目标还在


def test_tampered_manifest_with_the_maintainers_own_signature_rejected(tmp_path: Path):
    """真正的威胁模型不是“攻击者拿错了公钥”，而是“攻击者拿到了 OSS 桶的写权限、
    但没有私钥”：他保留维护者那份合法签名，只把清单正文换掉（抬高 version、把
    package/sha256 指向自己的包）。验签必须是对**正文字节**验的，这样任何正文
    改动都会让原签名失效——这才是挡住“谁能写 OSS 桶谁就拥有所有装机”的那道闸。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    evil_pkg = ch / "evil.zip"
    with zipfile.ZipFile(evil_pkg, "w") as z:
        z.writestr("marker.txt", "pwned")
    # 同一份（合法的）manifest.sig 原封不动，只换正文。
    (ch / "manifest.json").write_bytes(json.dumps({
        "version": "9.9.9",
        "package": evil_pkg.name,
        "sha256": hashlib.sha256(evil_pkg.read_bytes()).hexdigest(),
    }).encode())

    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with pytest.raises(InvalidSignature):
        apply_update(root, ch.as_uri(), pub, _ws(tmp_path))  # 注意：公钥是**对的**那把
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "versions").exists()


def test_not_enough_free_disk_returns_none_instead_of_crashing(tmp_path: Path):
    """磁盘峰值需求约 9.5GB（previous 2.87 + current 2.87 + staging 2.87 + zip 0.9）。
    磁盘满是持续性本地故障：不做预检查的话，每次开机都会在写盘/解压中途炸出一条
    没人看的 traceback，还会留下一个几 GB 的半成品 staging 目录。预检查发现空间
    不够就静默返回 None（下次开机再试），机器状态完全不变。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    almost_full = types.SimpleNamespace(total=10**9, used=10**9 - 512, free=512)
    with patch("shutil.disk_usage", return_value=almost_full):
        assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) is None

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "versions" / "0.1.1").exists()
    assert not (root / "download.tmp.zip").exists()


def test_missing_install_root_returns_none_instead_of_crashing(tmp_path: Path):
    """本模块是开机时静默跑的计划任务，没人会看它的 stderr。任何逃出去的异常都会
    变成 SystemExit(1)、每次开机崩一遍——而这台机器可能只是盘没挂载、或者调用方
    传错了路径。没有安装根 = 没有东西可更新，安静返回 None 即可，绝不能抛。

    （这条契约曾被破坏过：把 _try_lock 里的 mkdir 拿掉后，open() 落在了 try 外面，
    于是 FileNotFoundError 直接逃了出去。）"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    missing_root = tmp_path / "never-installed"

    assert apply_update(missing_root, ch.as_uri(), pub, _ws(tmp_path)) is None
    assert not missing_root.exists(), "不该凭空长出一棵谁都没打算要的目录树"


def test_last_resort_guard_never_deletes_the_dir_current_txt_points_at(tmp_path: Path):
    """锁是第一道防线。本测试把锁摘掉——模拟某些文件系统上字节范围锁静默失效的情况——
    单独验证最后一道保险：绝不删除 current.txt 当前指向的目录。"""
    ch, pub, _ = _channel(tmp_path, "0.1.2")
    root = tmp_path / "root"
    (root / "versions" / "0.1.1").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.1", encoding="utf-8")

    # 一把“谁来都给”的假锁：_try_lock 对每个调用方都返回它，两个实例于是同时往下跑。
    # apply_update 只判断它是不是 None，从不把它当文件句柄用（_unlock 也一并摘掉），
    # 所以一个空对象就够了——反过来给它安一个 fileno()，真被 _unlock 用上就是在动
    # fd 0（stdin），把“摘锁摘漏了”这种错误变成静默的。
    granted_to_everyone = object()

    reentered = []
    real_extractall = zipfile.ZipFile.extractall

    def extract_then_let_a_second_instance_win(self, path):
        real_extractall(self, path)
        if not reentered:                     # 第二个实例跑完整个更新，把 current.txt 切过去
            reentered.append(True)
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    with patch.object(updater, "_try_lock", return_value=granted_to_everyone), \
         patch.object(updater, "_unlock", lambda fh: None), \
         patch.object(zipfile.ZipFile, "extractall", extract_then_let_a_second_instance_win):
        # 输的那个实例发现自己的 staging 被对方的 _cleanup_stale_temp 删了。它具体炸在
        # 哪一步不重要（现在是"校验出厂母版"这道闸门先撞见目录没了 → ValueError；以前是
        # os.replace → FileNotFoundError）——这条测试要钉死的是**炸完之后**的那条断言：
        # current.txt 绝不能指向一个已经被删掉的目录。
        with contextlib.suppress(FileNotFoundError, ValueError):
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    current = (root / "current.txt").read_text(encoding="utf-8").strip()
    assert (root / "versions" / current).is_dir(), "current.txt 指向了一个已被删除的目录"


def test_last_resort_guard_never_rmtrees_the_version_current_txt_points_at(tmp_path: Path):
    """同一道"最后的保险"，这次真的把它执行到：另一个实例已经把这个版本装好、current.txt
    也切过去了。本实例接着走到"最终目录已存在"那个分支——它绝不能把那个目录当成"上次半途
    而废的残留"rmtree 掉：那一瞬间 current.txt 就指向了虚空，机器开机即挂。

    上一条测试（..._never_deletes_the_dir_current_txt_points_at）走不到这个分支：输的那个
    实例的 staging 先被对方的清理删掉，它在更早的"校验出厂母版"闸门上就炸了。于是这道保险
    其实**没有被任何测试钉住**——把它换成 `if False:`，全套测试依然全绿。这里让第二个实例
    在闸门**之后**才插进来，把执行真正送进那个分支。"""
    ch, pub, _ = _channel(tmp_path, "0.1.2")
    root = tmp_path / "root"
    (root / "versions" / "0.1.1").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.1", encoding="utf-8")

    granted_to_everyone = object()  # 假锁：两个实例同时往下跑（模拟锁在某个文件系统上失灵）
    reentered = []
    real_gate = updater.assert_factory_complete

    def gate_then_let_a_second_instance_finish(factory):
        real_gate(factory)
        if not reentered:  # 第二个实例跑完整个更新：装好 0.1.2 并把 current.txt 切过去
            reentered.append(True)
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    with patch.object(updater, "_try_lock", return_value=granted_to_everyone), \
         patch.object(updater, "_unlock", lambda fh: None), \
         patch.object(updater, "assert_factory_complete", gate_then_let_a_second_instance_finish):
        with contextlib.suppress(FileNotFoundError, ValueError):
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    assert (root / "current.txt").read_text(encoding="utf-8").strip() == "0.1.2"
    assert (root / "versions" / "0.1.2" / "marker.txt").exists(), "current.txt 指向的版本目录被删了"


def test_signed_but_malformed_manifest_fails_with_a_readable_error(tmp_path: Path):
    """签名有效但正文缺字段/版本号不是数字（"0.1.1-rc1"）——只可能是维护者自己的
    发版流程出了 bug。它必须以一条读得懂的错误出现，而不是一条裸的 KeyError
    traceback（main() 会把异常打成中文提示，前提是它别是个光秃秃的 KeyError）。"""
    ch = tmp_path / "channel"
    ch.mkdir()
    priv = Ed25519PrivateKey.generate()
    manifest = json.dumps({"package": "dist.zip", "sha256": "00"}).encode()  # 没有 version
    (ch / "manifest.json").write_bytes(manifest)
    (ch / "manifest.sig").write_bytes(base64.b64encode(priv.sign(manifest)))
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with pytest.raises(ValueError, match="清单"):
        apply_update(root, ch.as_uri(), priv.public_key().public_bytes_raw().hex(), _ws(tmp_path))


def test_staging_path_is_not_much_longer_than_the_final_one(tmp_path: Path):
    """Win10/11 家庭版默认没开 LongPathsEnabled，MAX_PATH=260。最终布局实测 68 字符，
    而 node_modules 嵌套很深——staging 目录名每多一个字符，整棵树里最长的那条路径就
    多一个字符。旧名字 `.staging-<version>-<pid>-<uuid8>` 比最终名字长 24 个字符。

    咬到的时候会怎样：extractall 抛的 OSError **不在** `except _UNREACHABLE` 的覆盖
    范围里（那个 except 只包着 _download），于是异常一路逃到 main()，SystemExit(1)。
    每次开机重下 895MB、失败、退出 1，永远装不上——把爸妈的带宽烧光，维护者收不到
    任何信号。

    锁已经提供了互斥（见 _try_lock），staging 名字里再放版本号和 pid 是冗余的。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    seen = []
    real_extractall = zipfile.ZipFile.extractall

    def record(self, path):
        seen.append(Path(path).name)
        real_extractall(self, path)

    with patch.object(zipfile.ZipFile, "extractall", record):
        assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"

    assert len(seen) == 1
    staging_name, final_name = seen[0], "0.1.1"
    assert len(staging_name) - len(final_name) <= 6, f"staging 目录名太长：{staging_name}"
    assert staging_name.startswith(".")  # 点开头：_prune_old_versions 不会把它当成版本目录


def test_update_applies_the_new_versions_factory_state_to_data(tmp_path: Path):
    """发行版 = 钉死的 Hermes + 出厂配置/persona/技能（ADR-0003）。更新只换代码、
    不把新版本的出厂状态应用到 data/ 的话，"改一行 SOUL.md 发个新版"永远不生效
    ——而那正是 Task 10 的验收项。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    workspace = _ws(tmp_path)

    assert apply_update(root, ch.as_uri(), pub, workspace) == "0.1.1"

    data = root / "data"
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.1"
    assert (data / "skills" / "creative" / "ascii-art" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "出厂技能 0.1.1"
    loaded = yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["model"]["default"] == "0.1.1"
    assert loaded["terminal"]["cwd"] == str(workspace)  # 占位符被渲染成了真实工作台
    assert not (data / "config.yaml.tmpl").exists()


def test_update_never_strands_the_users_own_data(tmp_path: Path):
    """铁律二。这条测试是整个布局返工的理由：旧布局把 .env / 聊天记录 / 习得技能
    都放在 versions/<v>/ 里，第一次自动更新就会把它们连同旧版本目录一起剪掉——
    激活码没了 = 产品直接变砖，静默发生，一万公里外收不到信号。"""
    ch, pub, priv = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    data = root / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "sessions" / "chat.jsonl").write_text("聊天记录", encoding="utf-8")
    (data / "memories").mkdir()
    (data / "memories" / "notes.md").write_text("记住的事", encoding="utf-8")
    (data / ".env").write_text("DASHSCOPE_API_KEY=sk-keep-me", encoding="utf-8")
    learned = data / "skills" / "business" / "quote-sheet" / "SKILL.md"
    learned.parent.mkdir(parents=True)
    learned.write_text("习得技能", encoding="utf-8")

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    _publish(ch, "0.1.2", priv)
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.2"  # 再更新一次，触发剪枝

    assert (data / ".env").read_text(encoding="utf-8") == "DASHSCOPE_API_KEY=sk-keep-me"
    assert (data / "sessions" / "chat.jsonl").read_text(encoding="utf-8") == "聊天记录"
    assert (data / "memories" / "notes.md").read_text(encoding="utf-8") == "记住的事"
    assert learned.read_text(encoding="utf-8") == "习得技能"


def test_a_failed_factory_apply_converges_on_the_next_run(tmp_path: Path):
    """应用出厂状态是在**提交点之后**（current.txt 已经切了）跑的，没有回头路。

    它失败一次（杀软锁了 skills/、权限、磁盘满）之后，旧代码里机器就**永久**停在
    "新代码 + 旧 persona + 旧配置"上：下一次开机，parse_version(0.1.1) <= parse_version
    (0.1.1) → return None → 印「已是最新版本，无需更新」→ 退出 0。心跳还报着新版本号，
    维护者收不到任何信号。维护者为"别再瞎编价格"发一版 SOUL.md 修复，它永远不会落地。

    更新器必须在每次运行的开头（它每次登录都跑、而且持着锁）对着 current.txt 校对出厂
    状态戳，缺了/旧了就先重新应用一遍——自愈，不依赖任何还不存在的启动器。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    (data / "SOUL.md").write_text("我是小助手 0.1.0", encoding="utf-8")

    boom = RuntimeError("杀软锁住了 data/skills/")
    with patch.object(updater, "apply_factory_state", side_effect=boom):
        with pytest.raises(RuntimeError):
            apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    # 提交点已经过了：机器在跑新代码，但 persona 还是旧的。这是可收敛的中间态。
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.0"

    # 下一次开机：通道里还是 0.1.1（"已是最新"），机器必须自己收敛。
    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) is None
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.1"
    assert (data / "skills" / "creative" / "ascii-art" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "出厂技能 0.1.1"


def test_a_successful_update_stamps_the_applied_factory_version(tmp_path: Path):
    """戳是"出厂状态已经收敛到这个版本"的唯一凭据。不落戳的话，自愈逻辑要么永远不跑
    （假设切了版本就等于应用过），要么每次开机把 2.87GB 的出厂状态重铺一遍。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    stamp = (root / "data" / FACTORY_STAMP).read_text(encoding="utf-8").strip()
    assert stamp == "0.1.1"


def test_a_corrupt_installed_factory_master_does_not_sever_the_update_channel(tmp_path: Path):
    """当前版本的出厂母版在装好**之后**被弄坏了（杀软隔离了 SOUL.md、磁盘错误），戳也没对上。

    自愈这一步收敛不过去（母版不合格，没有可收敛的目标）——但它绝不能因此抛异常把整次更新
    带崩：更新器是这台机器唯一的远程修复通道（builder/paths.py），而下一个发行版带的是一份
    全新的、切版本前就校验过的母版，那正是这台机器的出路。在自愈里硬抛 = 每次开机
    SystemExit(1) = 亲手把唯一的救援通道拆了。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    broken = root / "versions" / "0.1.0" / "factory"
    broken.mkdir(parents=True)
    (broken / "config.yaml.tmpl").write_text(
        'terminal:\n  cwd: "{{WORKSPACE_DIR}}"\n', encoding="utf-8"
    )  # SOUL.md 和 skills/ 没了 → 母版不合格
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"

    data = root / "data"
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.1"
    assert (data / FACTORY_STAMP).read_text(encoding="utf-8").strip() == "0.1.1"


def test_a_symlinked_data_skills_is_refused_before_the_switch(tmp_path: Path):
    """data/skills 是符号链接 = **机器状态**问题，不是"重跑就好"的 I/O 抖动：它每次运行
    都以完全相同的方式失败。在切 current.txt 之后才撞见它，机器就永久停在"新代码 + 旧
    出厂状态"上，而更新器每次开机都报「已是最新版本」。所以这道闸门必须在提交点之前。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    (data / "skills").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        apply_update(root, ch.as_uri(), pub, _ws(tmp_path))

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"  # 生效开关没动
    assert not (root / "versions" / "0.1.1").exists()
    assert not list(outside.iterdir())  # 一个字节都没写到 hermes_home 之外
    assert not list((root / "versions").glob(".s-*")), "拒绝之后不能留下几个 GB 的 staging 垃圾"


def test_orphan_staging_dir_from_an_older_updater_is_reclaimed(tmp_path: Path):
    """上一版更新器留下的 staging 目录叫 `.staging-<version>-<pid>-<uuid8>`（本版改成了
    `.s-<uuid8>`）。它是磁盘上一个 2.87GB 的孤儿：清理只 glob `.s-*` 就够不着它，
    _prune_old_versions 又跳过点开头的名字——于是它永远收不回来，还会把 _require_free_space
    永久压在"空间不够"那一侧：这台机器再也收不到任何更新（包括安全更新）。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    orphan = root / "versions" / ".staging-0.1.0-4242-deadbeef"
    (orphan / "hermes-agent" / "node_modules").mkdir(parents=True)
    (orphan / "hermes-agent" / "node_modules" / "blob.bin").write_bytes(b"x" * 4096)

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    assert not orphan.exists(), "上一版更新器的 staging 孤儿永远收不回来"


def test_package_without_a_factory_master_is_refused_before_the_switch(tmp_path: Path):
    """出厂母版缺失/不完整 = 维护者的打包流程出了 bug（内容不合法，不是"网络不好"）。
    必须在切 current.txt **之前**就拒绝：切过去之后才发现的话，机器已经跑在新代码上，
    却只有一半的出厂状态，谁也说不清它在什么状态。宁可停在旧版本、响亮地失败。"""
    ch = tmp_path / "channel"
    ch.mkdir()
    priv = Ed25519PrivateKey.generate()
    pkg = ch / "dist-0.1.1.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("marker.txt", "0.1.1")  # 没有 factory/
    manifest = json.dumps({
        "version": "0.1.1",
        "package": pkg.name,
        "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest(),
    }).encode()
    (ch / "manifest.json").write_bytes(manifest)
    (ch / "manifest.sig").write_bytes(base64.b64encode(priv.sign(manifest)))

    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with pytest.raises(ValueError):
        apply_update(root, ch.as_uri(), priv.public_key().public_bytes_raw().hex(), _ws(tmp_path))

    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.0"
    assert not (root / "versions" / "0.1.1").exists()
    assert not list((root / "versions").glob(".s-*")), "拒绝之后不能留下几个 GB 的 staging 垃圾"


def _valid_factory_master(factory: Path, version: str) -> None:
    """在 factory 目录下铺一份形状完整、能通过 assert_factory_complete 的出厂母版。"""
    factory.mkdir(parents=True)
    (factory / "config.yaml.tmpl").write_text(
        f'model:\n  default: "{version}"\nterminal:\n  cwd: "{{{{WORKSPACE_DIR}}}}"\n',
        encoding="utf-8",
    )
    (factory / "SOUL.md").write_text(f"我是小助手 {version}", encoding="utf-8")
    skill = factory / "skills" / "creative" / "ascii-art" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"出厂技能 {version}", encoding="utf-8")


def test_reconcile_survives_an_unreadable_current_factory_master(tmp_path: Path):
    """Path B：当前版本（0.1.0）的出厂母版**存在但读不出来**——杀软"隔离"惯常的做法是
    锁住文件而不是删除它，磁盘读错误同理——两者都抛 OSError（更准确地说是它的子类
    PermissionError），不是 ValueError。

    `assert_factory_complete` 走到最后一步 `read_text()` 校验模板占位符时撞见它，
    异常从 `except ValueError` 手缝里逃出去，一路逃到 main()：SystemExit(1) 挂在每次
    开机上。而这次通道里恰好摆着 0.1.1——本该救这台机器的那个新版本——旧代码里它永远
    装不上：堵死更新通道的后果比"跳过一次自愈"糟得多得多。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    factory = root / "versions" / "0.1.0" / "factory"
    _valid_factory_master(factory, "0.1.0")
    os.chmod(factory / "config.yaml.tmpl", 0)  # 杀软"锁住"而非删除：读的时候抛 PermissionError
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"

    data = root / "data"
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.1"
    assert (data / FACTORY_STAMP).read_text(encoding="utf-8").strip() == "0.1.1"


def test_reconcile_survives_its_own_apply_hitting_an_unwritable_data_dir(tmp_path: Path):
    """Path C（第一种复现）：当前版本的母版完全合格，但 data/ 本身被杀软的目录保护
    锁成只读（中国杀软套装的常见行为）。`_reconcile_factory_state` 里 `apply_factory_state`
    这次调用**在 try 之外**，写文件时抛出的 PermissionError 会绕开"母版不可用就跳过"
    那份宽容、原样逃到 main()——在任何网络调用**之前**就 SystemExit(1)，每次开机都这样，
    机器永远联系不上通道。

    通道里摆的仍是当前这个版本（"已是最新"），这样断言只盯住 reconcile 自己这一步的
    行为，不牵扯它之后的主更新路径。"""
    ch, pub, _ = _channel(tmp_path, "0.1.0")
    root = tmp_path / "root"
    factory = root / "versions" / "0.1.0" / "factory"
    _valid_factory_master(factory, "0.1.0")
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    os.chmod(data, 0o500)  # 读+执行，无写：目录保护型杀软锁住 data/ 的典型表现
    try:
        assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) is None  # 已是最新，且没有崩
    finally:
        os.chmod(data, 0o700)  # 还原权限，让 tmp_path 的清理逻辑能删掉这棵树

    # 戳没落：没有在假装已经收敛，下一次开机会照样重试自愈。
    assert not (data / FACTORY_STAMP).exists()


def test_reconcile_survives_its_own_apply_hitting_a_symlinked_skills_dir(tmp_path: Path):
    """Path C（第二种复现）：当前版本的母版完全合格，但 data/skills 是符号链接。
    `apply_factory_state` 内部的 `assert_skills_not_symlink` 抛 ValueError——异常类型
    虽然和"母版不可用"那支 except 匹配，但这次调用本身**在 try 之外**，同样会原样
    逃到 main()：SystemExit(1) 挂在每次开机上，在任何网络调用之前。"""
    ch, pub, _ = _channel(tmp_path, "0.1.0")
    root = tmp_path / "root"
    factory = root / "versions" / "0.1.0" / "factory"
    _valid_factory_master(factory, "0.1.0")
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    (data / "skills").symlink_to(outside, target_is_directory=True)

    assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) is None  # 已是最新，且没有崩

    assert not list(outside.iterdir())  # 一个字节都没写到 hermes_home 之外
    assert not (data / FACTORY_STAMP).exists()  # 戳没落：下一次开机照样重试自愈


def test_reconcile_survives_an_unreadable_factory_stamp(tmp_path: Path):
    """Path A：母版完全合格、data/ 可写，但**戳自己**读不出来。

    戳（data/.factory_version）住在 data/ 里——那正是最容易被杀软和长辈碰到的那棵树。
    杀软"隔离"锁住它（而不是删除它）之后，`is_file()` 照样为真，撞的是 `read_text()`
    那一下：PermissionError。而 `factory_state_is_current` 这次调用曾经**在 try 之外**，
    于是这个异常绕开"收敛不了就跳过"那份宽容、原样逃到 main()——在任何网络调用之前就
    SystemExit(1)，每次开机都这样。

    通道里恰好摆着 0.1.1（本该救这台机器的那个新版本）：自愈的一次失败绝不能连带堵死
    更新通道本身——那是这个函数存在的全部理由。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    _valid_factory_master(root / "versions" / "0.1.0" / "factory", "0.1.0")
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    data = root / "data"
    data.mkdir(parents=True)
    stamp = data / FACTORY_STAMP
    stamp.write_text("0.1.0", encoding="utf-8")
    os.chmod(stamp, 0)  # 杀软"锁住"而非删除：读的时候抛 PermissionError

    try:
        assert apply_update(root, ch.as_uri(), pub, _ws(tmp_path)) == "0.1.1"
    finally:
        os.chmod(stamp, 0o600)  # 还原权限，让 tmp_path 的清理逻辑能删掉这棵树

    # 更新通道没被堵死：0.1.1 装上了，而且它的出厂状态落到了 data/（戳被重新写过）。
    assert (data / "SOUL.md").read_text(encoding="utf-8") == "我是小助手 0.1.1"
    assert stamp.read_text(encoding="utf-8").strip() == "0.1.1"
