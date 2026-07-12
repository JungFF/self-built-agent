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
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import updater
from tools.updater import apply_update, parse_version, verify_manifest


def _publish(ch_dir: Path, version: str, priv: Ed25519PrivateKey, payload: bytes = b"") -> None:
    """用同一把密钥在通道目录里发布一个版本：包 + 清单 + 签名。可以对同一个
    目录反复调用来追加发布新版本（模拟维护者连续发版，用于测试“连续更新两次，
    旧版本会不会被清理”）。payload 用来发布一个“体量真实”的大包（不压缩存储），
    给内存峰值测试用。"""
    pkg = ch_dir / f"dist-{version}.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("marker.txt", version)
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
    assert apply_update(root, ch.as_uri(), pub) == "0.1.1"
    assert (root / "versions/0.1.1/marker.txt").read_text() == "0.1.1"
    assert (root / "current.txt").read_text(encoding="utf-8") == "0.1.1"
    assert (root / "previous.txt").read_text(encoding="utf-8") == "0.1.0"


def test_apply_update_noop_when_up_to_date(tmp_path: Path):
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.1", encoding="utf-8")
    assert apply_update(root, ch.as_uri(), pub) is None


def test_bad_signature_rejected(tmp_path: Path):
    ch, _, _ = _channel(tmp_path, "0.1.1")
    wrong_pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")
    with pytest.raises(InvalidSignature):
        apply_update(root, ch.as_uri(), wrong_pub)
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
        apply_update(root, ch.as_uri(), pub)
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
    assert apply_update(root, ch.as_uri(), pub) is None
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
    assert apply_update(root, unreachable_url, fake_pub) is None
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
        apply_update(root, ch.as_uri(), pub_hex)


def test_disk_does_not_grow_unbounded_across_multiple_updates(tmp_path: Path):
    """连续更新两次（0.1.0 → 0.1.1 → 0.1.2），每个版本 2.87GB 的现实体量下，
    versions/ 不能三个版本都留着——只留 current + previous 两份；回滚目标
    (previous) 绝不能被清理掉。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.0.9", encoding="utf-8")

    ch, pub, priv = _channel(tmp_path, "0.1.0")
    assert apply_update(root, ch.as_uri(), pub) == "0.1.0"
    assert (root / "versions" / "0.1.0").is_dir()

    _publish(ch, "0.1.1", priv)
    assert apply_update(root, ch.as_uri(), pub) == "0.1.1"
    # 刚更新到 0.1.1：previous 是 0.1.0，是回滚目标，不能删。
    assert (root / "versions" / "0.1.0").is_dir()
    assert (root / "versions" / "0.1.1").is_dir()

    _publish(ch, "0.1.2", priv)
    assert apply_update(root, ch.as_uri(), pub) == "0.1.2"
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
            apply_update(root, ch.as_uri(), pub)

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
            apply_update(root, ch.as_uri(), pub)

    assert apply_update(root, ch.as_uri(), pub) == "0.1.1"
    versions_dir = root / "versions"
    assert not list(versions_dir.glob(".staging-*"))
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
            nested_result.append(apply_update(root, ch.as_uri(), pub))

    with patch.object(zipfile.ZipFile, "extractall", extract_then_let_a_second_instance_run):
        apply_update(root, ch.as_uri(), pub)

    assert nested_result == [None]  # 第二个实例拿不到锁：安静让路，不是错误
    current = (root / "current.txt").read_text(encoding="utf-8").strip()
    assert (root / "versions" / current).is_dir()  # current.txt 绝不能指向一个不存在的目录
    assert not list((root / "versions").glob(".staging-*"))


def test_mid_download_drop_returns_none_not_crash(tmp_path: Path):
    """下载到一半断网（服务器声明 Content-Length: N，只发了 N/2 就挂断）。

    http.client.IncompleteRead 不是 OSError 的子类，只 `except OSError` 会漏掉
    它——静默计划任务会以非零码崩溃退出，而没有人会看那个 stderr。"""
    ch, pub, _ = _channel(tmp_path, "0.1.1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "current.txt").write_text("0.1.0", encoding="utf-8")

    with _serve(ch, truncate_zip=True) as url:
        assert apply_update(root, url, pub) is None

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
        assert apply_update(root, url + "/", pub) == "0.1.1"


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
        assert apply_update(root, ch.as_uri(), pub) == "0.1.1"
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

    assert apply_update(root, ch.as_uri(), pub) == "0.1.1"

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
    assert apply_update(root, ch.as_uri(), pub) == "0.1.0"
    _publish(ch, "0.1.1", priv)
    assert apply_update(root, ch.as_uri(), pub) == "0.1.1"
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
            apply_update(root, ch.as_uri(), pub)

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
        apply_update(root, ch.as_uri(), pub)  # 注意：公钥是**对的**那把
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
        assert apply_update(root, ch.as_uri(), pub) is None

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

    assert apply_update(missing_root, ch.as_uri(), pub) is None
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
            apply_update(root, ch.as_uri(), pub)

    with patch.object(updater, "_try_lock", return_value=granted_to_everyone), \
         patch.object(updater, "_unlock", lambda fh: None), \
         patch.object(zipfile.ZipFile, "extractall", extract_then_let_a_second_instance_win):
        with contextlib.suppress(FileNotFoundError):   # 输的那个实例发现自己的 staging 没了
            apply_update(root, ch.as_uri(), pub)

    current = (root / "current.txt").read_text(encoding="utf-8").strip()
    assert (root / "versions" / current).is_dir(), "current.txt 指向了一个已被删除的目录"


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
        apply_update(root, ch.as_uri(), priv.public_key().public_bytes_raw().hex())
