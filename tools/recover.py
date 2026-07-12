# tools/recover.py
"""恢复工具：小助手唯一需要长辈学会的故障处理动作。

两种恢复语义（不要混淆，误用后果不同）：

一键恢复（factory reset，deep=False，默认）
    桌面「小助手修复」图标双击触发。把出厂 payload 里的文件（config.yaml、
    SOUL.md 等）原样盖回 hermes_home，修复被用户/agent 弄坏的配置。
    - 绝不触碰 .env（激活码，ADR-0002：没了 key 产品直接不能用）
    - 绝不触碰 skills/ 下任何东西——习得技能是 agent 自我改进沉淀下来的
      资产，不该因为一次修复就清零
    - 只“盖回”payload 里存在的文件，不清空、不删除 payload 之外的任何
      用户文件

深度恢复（deep reset，deep=True）
    维护者远程执行，不对最终用户暴露。行为漂移时的终极刹车：
    出厂状态 + 清空习得技能。仍然保留 .env（激活码属于本机，与技能无关）。

    “习得技能”的判定：Hermes 把出厂技能和习得技能混在同一个 skills/ 目录里
    （没有独立的 learned/ 子目录），靠 skills/.bundled_manifest 里的技能名单
    区分——名字不在清单里的技能目录即视为习得技能。manifest 缺失时无法安全
    区分，此时选择*中止整个操作并抛异常*，而不是静默跳过或者硬删：删错了
    出厂技能不可逆，宁可让维护者看到报错去手动排查，也不要在他们以为“深度
    恢复已经清干净了”的时候其实什么都没清。

    失败保证的范围（不要泛化成“失败就等于什么都没做”）：只有三道校验闸门
    ——payload 目录存在性检查、payload 混入 .env 检查、manifest 缺失检查
    ——保证在任何文件系统写入之前执行，所以*这三种*失败必然对应“hermes_home
    完全未被修改”。一旦校验通过、真正开始写入（盖回出厂文件、rmtree 清理技能
    目录），操作本身不是原子的：rmtree 要遍历删除多个目录，中途若因权限、
    并发修改等原因抛出异常，hermes_home 会停在一个改了一半的中间态。
"""

import argparse
import shutil
from pathlib import Path, PureWindowsPath

from builder.paths import BUNDLED_MANIFEST, ENV_FILE, SKILLS_DIR

SKILL_MARKER = "SKILL.md"


def _manifest_path(hermes_home: Path) -> Path:
    """把 BUNDLED_MANIFEST 解析成 hermes_home 下的真实路径。

    BUNDLED_MANIFEST（来自 builder/paths.py）是 Windows 路径格式的字符串
    （`skills\\.bundled_manifest`），但恢复工具要能在任何平台上跑（本仓库
    的测试就跑在 macOS 上）。直接 `Path(BUNDLED_MANIFEST)` 在非 Windows 平台
    上会把整条反斜杠字符串当成一个奇怪的单一文件名，而不是两级目录。这里用
    PureWindowsPath 按 Windows 规则把它拆成 parts，再用当前平台的 Path 拼
    回去，从而不需要改动 paths.py（那是上一个 Task 已经 commit 的文件）。
    """
    return hermes_home.joinpath(*PureWindowsPath(BUNDLED_MANIFEST).parts)


def _assert_payload_has_no_env(factory_payload: Path) -> None:
    """出厂 payload 里绝不能混入 .env——那意味着维护者打包时手滑把自己的
    激活码打进了出厂包，一旦分发出去就是密钥泄露。递归检查整棵 payload 树
    （不只是顶层），防止 .env 被藏在某个子目录里。

    不区分文件/目录：payload/.env/key.txt 这种“.env 是个目录”的形态同样要
    在这里被拒绝，否则会漏网放行，直到 _restore_factory_files 复制阶段撞见
    hermes_home 下同名的真实 .env 才炸出一个不明所以的 FileExistsError。
    """
    leaked = next(factory_payload.rglob(ENV_FILE), None)
    if leaked is not None:
        raise ValueError(
            f"出厂 payload 中混入了 {ENV_FILE}（{leaked}）——绝不能把激活码打进"
            "出厂包再分发出去。已拒绝执行，未做任何修改。"
        )


def _restore_factory_files(hermes_home: Path, factory_payload: Path) -> list[str]:
    """把 payload 里的每个文件按相对路径原样盖回 hermes_home。

    这是纯粹的“复制/覆盖”，从不删除 hermes_home 里任何 payload 之外的文件，
    所以“保留 .env”“保留习得技能”“不清空用户目录”都是这个函数天然满足的
    性质，不需要额外的白名单/黑名单逻辑去特殊照顾。
    """
    log = []
    for src in sorted(factory_payload.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(factory_payload)
        dest = hermes_home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        log.append(f"已还原出厂文件：{rel.as_posix()}")
    return log


def _bundled_skill_names(manifest_path: Path) -> set[str]:
    """解析 `.bundled_manifest`（每行 `技能名:哈希`），取出出厂技能名集合。"""
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    names = (line.split(":", 1)[0].strip() for line in lines)
    return {name for name in names if name}


def _skill_dirs(skills_dir: Path) -> set[Path]:
    """找出 skills/ 下所有“技能目录”。

    判据：目录下直接放着 SKILL.md，就是一个技能目录候选，目录名即技能名。
    这个判据只看“有没有 SKILL.md 这个标记文件”，不假设固定嵌套深度——
    apple/apple-notes/SKILL.md（两层）和 computer-use/SKILL.md（一层，
    computer-use 本身就是技能目录）都能正确识别，不需要区分“分类目录”和
    “技能目录”这两层结构。

    候选集里必须排除两类噪音，否则会误删：
    - skills_dir 自身：如果 SKILL.md 直接躺在 skills/ 根目录（污染/手滑），
      会让整个 skills/ 被当成一个“技能目录”纳入删除候选，进而 rmtree 掉
      整棵技能树（含 .bundled_manifest 和所有出厂技能）。
    - 嵌套在其他候选目录内部的 SKILL.md：技能内部常见 examples/SKILL.md、
      templates/SKILL.md 这类模板/示例文件，不是独立技能。只保留“最外层”
      候选——若某候选的任一祖先目录也在候选集里，它就是被外层技能目录
      吞并的内部文件，而不是独立技能。这同时避免了父子两层都进入删除
      列表、导致 rmtree 对同一路径删两次而崩溃。

      已知限制：“嵌套的都是模板/示例”只是一个假设，不是保证过的事实。如果
      一个真正习得的技能恰好被放在了某个出厂技能目录内部（而不是自己的
      顶层目录），--deep 也会把它当成模板/示例保留下来、不会清理。这是
      刻意接受的权衡，不是疏漏：另一种做法（连嵌套目录一起删）会有把出厂
      技能内部合法模板/示例一起误删的风险，而误删不可逆、漏删可以事后
      人工处理，所以宁可少删也不可错删。
    """
    candidates = {p.parent for p in skills_dir.rglob(SKILL_MARKER)} - {skills_dir}
    return {d for d in candidates if not any(anc in candidates for anc in d.parents)}


def _plan_deep_wipe(hermes_home: Path) -> list[Path]:
    """算出深度恢复要删掉哪些习得技能目录，但不做任何实际修改。

    调用方必须在改动文件系统之前先调用这个函数：manifest 缺失时它会抛
    FileNotFoundError，让整个 recover() 在“动手之前”就整体中止，不会出现
    “出厂文件已经盖回去了、技能清理却因为分不清出厂/习得而失败了一半”的
    中间态。
    """
    skills_dir = hermes_home / SKILLS_DIR
    if skills_dir.is_symlink():
        # 正常出厂形态里 skills/ 一定是真实目录；如果是符号链接，rmtree 会
        # 跟着链接删到 hermes_home 之外。这里显式拒绝，而不是靠“碰巧没人
        # 这么部署”的隐性安全。注意判断顺序：is_symlink() 必须先于
        # is_dir()——指向目录的符号链接会让 is_dir() 也返回 True。
        raise ValueError(
            f"深度恢复中止：{skills_dir} 是符号链接，可能会把删除操作导向"
            "hermes_home 之外的位置。请检查安装是否被篡改。未做任何修改。"
        )
    if not skills_dir.is_dir():
        # 没有 skills/ 目录，也就没有任何技能需要区分——这不是异常情况，
        # 是“无事可做”，不应该报错。
        return []

    manifest_path = _manifest_path(hermes_home)
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"深度恢复中止：找不到 {manifest_path}，无法区分出厂技能和习得"
            "技能。为避免误删出厂技能（不可逆），本次调用未做任何修改，"
            "请检查 hermes_home 是否完整。"
        )

    bundled = _bundled_skill_names(manifest_path)
    return sorted(d for d in _skill_dirs(skills_dir) if d.name not in bundled)


def _execute_deep_wipe(hermes_home: Path, to_remove: list[Path]) -> list[str]:
    # 这个 is_dir() 判断不是一个真正的安全闸门：skills/ 不存在时
    # _plan_deep_wipe 早就返回了空的 to_remove（见上面 return []），下面的
    # `if not to_remove` 分支已经能兜住这种情况。之所以单独判断，是为了在
    # “skills/ 根本不存在”和“skills/ 存在但没有习得技能”这两种都是空
    # to_remove 的情况下，给出不同的人类可读日志——前者是“跳过技能清理”，
    # 后者是“清理过但无事可做”。不要因为看着像重复判断就把它“简化”掉。
    if not (hermes_home / SKILLS_DIR).is_dir():
        return [f"{SKILLS_DIR}/ 不存在，跳过技能清理"]
    if not to_remove:
        return ["未发现习得技能，无需清理"]

    log = []
    for skill_dir in to_remove:
        rel = skill_dir.relative_to(hermes_home)
        shutil.rmtree(skill_dir)
        log.append(f"已删除习得技能：{rel.as_posix()}")
    return log


def recover(hermes_home: Path, factory_payload: Path, deep: bool = False) -> list[str]:
    """执行恢复，返回人类可读的动作日志（维护者远程排障时要能看懂做了什么）。

    deep=False（默认，一键恢复）：只盖回出厂文件，不碰 .env、不碰 skills/。
    deep=True（深度恢复）：额外清空 skills/ 里不在 .bundled_manifest 中的
    习得技能目录；.bundled_manifest 缺失时整体中止（见模块 docstring）。
    """
    if not factory_payload.is_dir():
        # payload 目录缺失或路径写错时，rglob() 对不存在的目录静默返回空
        # 迭代器、不报错——如果没有这道闸门，_restore_factory_files 会“成功”
        # 返回空日志，CLI 印出“修复完成”、退出码 0，而 hermes_home 里被
        # 弄坏的配置其实原封不动。长辈看到的是一句自信的谎言，10000 公里外
        # 的维护者也不会收到任何信号。宁可在这里就报错中止。
        raise ValueError(f"出厂 payload 目录不存在：{factory_payload}。未做任何修改。")
    _assert_payload_has_no_env(factory_payload)

    if not deep:
        return _restore_factory_files(hermes_home, factory_payload)

    # 先算出要删什么、再动手：manifest 缺失时 _plan_deep_wipe 在这里就抛异常，
    # 早于任何文件改动，保证“中止 = 完全没动手”，不会留下改了一半的中间态。
    to_remove = _plan_deep_wipe(hermes_home)
    log = _restore_factory_files(hermes_home, factory_payload)
    log.extend(_execute_deep_wipe(hermes_home, to_remove))
    return log


def main() -> None:
    parser = argparse.ArgumentParser(description="小助手恢复工具")
    parser.add_argument("hermes_home", type=Path, help="Hermes 安装目录")
    parser.add_argument("factory_payload", type=Path, help="出厂 payload 目录")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="深度恢复：额外清空习得技能（维护者专用，不对最终用户暴露）",
    )
    args = parser.parse_args()

    try:
        actions = recover(args.hermes_home, args.factory_payload, deep=args.deep)
    except Exception as exc:  # 面向最终用户的 CLI：不抛裸 traceback，给中文提示
        print(f"小助手修复失败：{exc}")
        raise SystemExit(1) from exc

    print("小助手深度恢复完成！" if args.deep else "小助手修复完成！")
    for action in actions:
        print(f"  - {action}")


if __name__ == "__main__":
    main()
