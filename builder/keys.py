# builder/keys.py
"""通道签名密钥 —— 整个更新通道的信任根（ADR-0003）。

任何人都可能往 OSS 桶里塞一个包（凭证泄漏、账号被撞库、维护者自己手滑）。挡住"塞进去
的包被机器装上"的，只有这一把 Ed25519 私钥：机器上的更新器验的是**清单正文字节**的签名，
公钥在装机时就烧进了 channel.json。

由此推出两条不可协商的规则：

1. **channel.key 绝不离开维护者的机器。** `secrets/` 已经 gitignore；私钥泄漏是这个项目
   里唯一**无法回收**的灾难——公钥装出去之后没有任何东西能换掉它，拿到私钥的人可以签一个
   包让每台机器静默执行任意代码，永远。builder/release.py 会在打包前扫一遍 payload，撞见
   私钥就当场炸掉。

2. **channel.key 绝不能被覆盖。** 覆盖 = 所有已装机的机器永久失联：它们只认烧进
   channel.json 的那把旧公钥，新私钥签出来的包一个都验不过——**包括那个本来能修好这件事的
   更新**。generate_keypair 因此用 O_EXCL 创建，撞见已有的密钥就抛 FileExistsError。

生成真密钥是**一次性人工步骤**（不在任何自动化流程里）：

    mkdir -p secrets
    uv run python -c "from pathlib import Path; from builder.keys import generate_keypair; \\
        print(generate_keypair(Path('secrets')))"

    # secrets/ 已被 .gitignore（tests/test_guardrails.py 钉住了这一条）
    # channel.key 立刻额外备份到密码管理器——它丢了，通道就永远发不出下一个版本了
    # channel.pub 的内容烧进安装器：ISCC /DPubKey=<channel.pub 的内容>
"""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_FILE = "channel.key"  # 私钥（hex）：只在维护者机器上
PUB_FILE = "channel.pub"  # 公钥（hex）：烧进每台机器的 channel.json


def generate_keypair(dest: Path) -> tuple[Path, Path]:
    """生成一对 Ed25519 密钥，返回 (私钥路径, 公钥路径)。

    **已经存在就抛 FileExistsError，绝不覆盖**（见模块 docstring 第 2 条：覆盖 = 所有
    已装机的机器永久失联）。O_EXCL 让"检查 + 创建"是一步原子操作，而不是一次 TOCTOU
    赛跑；0o600 让私钥不是全局可读的。
    """
    dest.mkdir(parents=True, exist_ok=True)
    key_path, pub_path = dest / KEY_FILE, dest / PUB_FILE
    if pub_path.exists():
        # 公钥先挡一道：私钥没了而公钥还在，说明维护者丢了私钥——此时更不能"再生成一对"
        # 把仅存的那半个真相也盖掉（那把公钥可能正烧在某台机器的 channel.json 里）。
        raise FileExistsError(f"{pub_path} 已存在——绝不覆盖通道密钥。未做任何修改。")

    priv = Ed25519PrivateKey.generate()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # 已存在 → FileExistsError
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(priv.private_bytes_raw().hex())
    pub_path.write_text(priv.public_key().public_bytes_raw().hex(), encoding="utf-8")
    return key_path, pub_path


def load_private_key(priv_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex.strip()))


def sign(data: bytes, priv_hex: str) -> bytes:
    """签名（base64）。签的是**正文字节**，不是解析后的对象：拿到 OSS 写权限但没有私钥的
    人，最自然的动作是留着维护者那份合法签名、只改正文（抬高 version、把 package/sha256
    指向自己的包）——正文一改，签名就对不上（tools/updater.py::verify_manifest）。"""
    return base64.b64encode(load_private_key(priv_hex).sign(data))


def public_key_hex(priv_hex: str) -> str:
    """私钥对应的公钥（hex）。打包器用它以**消费方的视角**验一遍自己刚签的清单
    ——不用去读 channel.pub（那份可能是别的密钥留下的、或者根本不在手边）。"""
    return load_private_key(priv_hex).public_key().public_bytes_raw().hex()
