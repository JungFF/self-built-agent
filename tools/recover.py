# tools/recover.py
"""恢复工具：小助手唯一需要长辈学会的故障处理动作。

两种恢复语义（不要混淆，误用后果不同）：

一键恢复（factory reset，deep=False，默认）
    桌面「小助手修复」图标双击触发。语义 = **一次例行更新对 data/ 做的事**
    （tools/factory_state.py 的 apply_factory_state），只是母版取自当前版本而不是新版本：
    - config.yaml.tmpl **渲染**成 config.yaml（不是原样拷过去！config.yaml 才是
      Hermes 真正读的文件，也是最可能被弄坏的那个。原样拷模板 = 修复了一个谁也
      不读的文件、印一句「小助手修复完成！」，而坏配置一个字节没动——一句对长辈
      的自信的谎言，一万公里外的维护者也收不到任何信号。）
    - SOUL.md 覆盖回出厂 persona
    - **出厂技能覆盖回母版的版本**，习得技能原样保留（覆盖，不是重铺）
    - 绝不触碰 .env（激活码，ADR-0002：没了 key 产品直接不能用）
    - 只“盖回”出厂母版里存在的文件，不清空、不删除 data/ 里任何别的用户文件

    为什么一键恢复也必须覆盖出厂技能（这里曾经是整个跳过 skills/ 的）：ADR-0005 说
    **agent 就地改写某个出厂技能**是最可能的行为漂移方式。跳过 skills/ 的话，长辈唯一
    会按的那个按钮恰好修不了最可能坏的那个东西——而一次静默的例行更新反倒能修好它。
    "习得技能是资产、不能清零"这个理由拦不住**覆盖**：覆盖只盖母版里有的那些文件，
    习得技能原样留着。要清空是 --deep 的语义。

深度恢复（deep reset，deep=True）
    维护者远程执行，不对最终用户暴露。行为漂移时的终极刹车：出厂状态 +
    **整棵 skills/ 从出厂母版重铺**（ADR-0005）。仍然保留 .env（激活码属于本机，
    与技能无关）。

    为什么是"重铺"而不是"按 .bundled_manifest 做差集只删习得技能"：差集只比
    **技能名**，所以 agent **就地改写某个出厂技能的 SKILL.md** 造成的漂移，差集
    法根本刹不住（名字还在清单里，被判为出厂技能而保留）。而"改写出厂技能"恰恰
    是自我改进循环最可能的漂移方式——终极刹车对最该刹住的那种漂移无效，等于没有
    终极刹车。重铺回归"把机器打回出厂状态"的定义，代价是习得技能全清（那本就是
    深度恢复的定义；一键恢复才是"保留习得技能"的那个）。

    重铺的母版必须来自 **versions/<v>/factory/skills/**（版本自带、只读、agent
    够不到），不能拿运行时的 data/skills/ 当源——那棵树可能已经被污染了。

失败保证的范围（不要泛化成“失败就等于什么都没做”）：所有校验闸门——HERMES_HOME 存在性
（路径敲错时**拒绝**，绝不凭空建一棵 data/ 再报成功）、出厂母版目录存在性、母版混入
.env / 用户资产 / 渲染好的 config.yaml、母版缺文件或模板的占位符不是双引号标量、
data/skills 是符号链接——都保证在任何文件系统写入之前执行，所以*这些*失败必然对应
“data/ 完全未被修改”。
一旦校验通过、真正开始写入，操作本身不是原子的：单个文件是 tmp + os.replace 换名
写的（不会出现截断的半个 YAML），但深度恢复的“rmtree skills/ 再整棵拷回来”要遍历
删除/复制多个目录，中途若因权限、杀软锁文件等原因抛异常，skills/ 会停在一个铺了
一半的中间态。这个中间态是**可收敛**的：母版在版本目录里、永远还在，重跑一次
`--deep` 就能铺完（深度恢复本来就是维护者盯着跑的，不是无人值守的静默任务）。
"""

import argparse
import shutil
from pathlib import Path

from builder.paths import DATA_DIR_REL, FACTORY_SKILLS, SKILLS_DIR
from tools.factory_state import (
    assert_factory_complete,
    assert_skills_not_symlink,
    default_workspace_dir,
    overwrite_bundled_skills,
    restore_factory_files,
)


def recover(
    hermes_home: Path, factory_dir: Path, workspace_dir: Path, deep: bool = False
) -> list[str]:
    """执行恢复，返回人类可读的动作日志（维护者远程排障时要能看懂做了什么）。

    factory_dir 是**当前版本的出厂母版**（versions/<v>/factory/），不再是 Task 3 那个
    临时渲染出来的 payload：出厂状态属于版本（铁律三），母版跟着版本走，回滚回旧版本
    就该恢复成旧版本的出厂状态。显式传目录（而不是让 recover 自己去读 current.txt
    推导）也让维护者能"把这台机器恢复成某个指定版本的出厂状态"——远程排障时是个真用得上
    的动作。

    workspace_dir（桌面的「小助手」文件夹）也显式传：它要被渲染进 config.yaml 的
    terminal.cwd。推导只发生在 main()（见 factory_state.default_workspace_dir）。
    """
    # 闸门全部先于任何写入。少了任何一道，失败的表现都是“印着「修复完成」，其实
    # 什么都没修 / 反而弄坏了更多东西”。
    if not hermes_home.is_dir():
        # 绝不 mkdir 出一个新的 data/：这个工具是维护者远程 ToDesk 进去手敲路径跑的，
        # 敲错一个字符就会凭空长出一棵 data/ 树、把出厂状态铺进去、印「小助手修复完成！」、
        # 退出 0——而真正坏掉的那台机器上的 data/ 一个字节都没动。修复工具报的成功必须是
        # 真的成功。（第一次装机时建 data/ 是安装器的活，不是修复工具的。）
        raise ValueError(
            f"HERMES_HOME 不存在：{hermes_home}。它应该是安装根下的 {DATA_DIR_REL}/"
            "（路径敲错了？）。未做任何修改。"
        )
    assert_factory_complete(factory_dir)
    # 一键恢复也要写 skills/（覆盖出厂技能），所以两种语义都得先挡掉符号链接：深度恢复的
    # rmtree 会跟着链接删到 hermes_home 之外，一键恢复的覆盖会把文件写到外面去。
    assert_skills_not_symlink(hermes_home / SKILLS_DIR)

    log = restore_factory_files(hermes_home, factory_dir, workspace_dir)
    if deep:
        log.extend(_relay_skills(hermes_home, factory_dir))
    else:
        # 一键恢复必须至少做到一次例行更新做到的事（apply_factory_state 的语义：覆盖出厂
        # 技能、保留习得技能）。跳过 skills/ 的话，长辈唯一会按的那个按钮恰好修不了
        # **最可能坏的那个东西**——ADR-0005 说 agent 就地改写出厂技能是最可能的行为漂移，
        # 而一次静默的例行更新反倒能修好它。"习得技能是资产"这个理由拦不住覆盖：覆盖只盖
        # 母版里有的那些文件，习得技能原样留着（要清空是 --deep 的语义）。
        log.extend(overwrite_bundled_skills(hermes_home, factory_dir))
    # 故意不写 data/.factory_version：本函数接的是任意一个 factory_dir，不像更新器那样
    # 知道它是不是 current.txt 指的那个版本——落一个自己没资格担保的戳等于伪造收敛凭据，
    # 会让更新器的 _reconcile_factory_state 误判"已收敛"而跳过下次自愈。这不是遗漏。
    return log


def _relay_skills(hermes_home: Path, factory_dir: Path) -> list[str]:
    """删掉整棵 skills/，从出厂母版重新铺一份（ADR-0005）。

    调用方必须已经跑过 assert_skills_not_symlink（符号链接会让 rmtree 删到 hermes_home
    之外）和 assert_factory_complete（母版里没有 skills/ 的话，这里会先把机器上的技能
    删光、再从一个不存在的源拷 0 个文件回来——机器上一个技能都不剩，而 CLI 印的是
    「小助手深度恢复完成！」）。
    """
    skills = hermes_home / SKILLS_DIR
    log = []
    if skills.exists():
        shutil.rmtree(skills)
        log.append(f"已清空 {SKILLS_DIR}/（含全部习得技能）")
    shutil.copytree(factory_dir / FACTORY_SKILLS, skills)
    log.append(f"已从出厂母版重铺 {SKILLS_DIR}/：{factory_dir / FACTORY_SKILLS}")
    return log


def main() -> None:
    parser = argparse.ArgumentParser(description="小助手恢复工具")
    parser.add_argument("hermes_home", type=Path, help="HERMES_HOME（安装根下的 data/）")
    parser.add_argument("factory_dir", type=Path, help="出厂母版目录（versions/<版本>/factory/）")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="桌面的「小助手」工作台目录（默认按当前登录用户的桌面推导）",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="深度恢复：整棵 skills/ 从出厂母版重铺，习得技能全清（维护者专用，不对最终用户暴露）",
    )
    args = parser.parse_args()

    # 推导只在这一层发生。⚠️ 桌面是**按 Windows 用户**算的，所以「小助手修复」快捷方式
    # 必须以最终用户身份运行（见 factory_state.default_workspace_dir）。
    workspace = args.workspace or default_workspace_dir()
    try:
        actions = recover(args.hermes_home, args.factory_dir, workspace, deep=args.deep)
    except Exception as exc:  # 面向最终用户的 CLI：不抛裸 traceback，给中文提示
        print(f"小助手修复失败：{exc}")
        raise SystemExit(1) from exc

    print("小助手深度恢复完成！" if args.deep else "小助手修复完成！")
    for action in actions:
        print(f"  - {action}")


if __name__ == "__main__":
    main()
