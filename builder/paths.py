# builder/paths.py
"""发行版的磁盘布局契约。

全部值来自 2026-07-11 的 Windows 快照 spike（装配机：阿里云硅谷 Windows Server 2022，
Hermes v0.18.2 / commit 4281151a）。改动这些常量需要重新跑 spike 验证。

本模块是唯一的真相来源。任何 Task 需要一个路径/环境变量名，都必须从这里 import——
不要在自己的模块里私自重新定义一遍（Task 5 曾私自复制了一份 update.lock 的名字，
导致契约与实现可以静默脱钩）。

===============================================================================
三条铁律
===============================================================================

**铁律一：安装根不含 Windows 用户名。**
Hermes 的 venv 把绝对路径烧进 pyvenv.cfg 和每个 Scripts/*.exe 的 shebang。
只有固定根（C:\\Users\\Public\\... 在每台 Windows 上都是同一个路径）才能让装配机
生成的路径在目标机上原样成立。

**铁律二：用户的东西与版本的东西分开。**
更新 = 解压一个新的 versions/<v>/ 再切 current.txt。所以凡是放进 versions/<v>/ 的
东西，更新时都会被整个换掉。激活码、习得技能、聊天记录若放在里面，第一次自动更新就
全没了——静默发生，维护者在一万公里外收不到信号。

**铁律三：但"出厂状态"属于版本，不属于机器。**
ADR-0003 定义的发行版 = 钉死的 Hermes + 出厂配置/persona/技能，打成一个整体包。
所以 SOUL.md、config.yaml、出厂技能必须**随版本下发**——否则新版本只能改代码，
永远改不了 persona（而 Task 10 的验收项恰恰是"改一行 SOUL.md 发个新版"）。

铁律二和铁律三的张力，靠"每次切版本时把版本的出厂状态应用到 data/"来化解：

    切版本（升级或回滚）时执行：
      versions/<新>/factory/config.yaml.tmpl  --渲染-->  data/config.yaml
      versions/<新>/factory/SOUL.md           --覆盖-->  data/SOUL.md
      versions/<新>/factory/skills/*          --覆盖-->  data/skills/*   （只覆盖出厂技能）
      data/.env、data/sessions/、data/memories/、data/skills/ 里的习得技能  --绝不触碰-->

**契约要求（不是建议）：每个会切版本的进程，都必须在自己每次运行的开头做一次校对。**
出厂状态是在 current.txt 切过去**之后**才应用的（顺序反过来会得到"旧代码 + 新配置"，
更危险）。也就是说它落在提交点之后：一旦它失败（杀软锁文件、权限、磁盘满），机器就停在
"新代码 + 旧 persona / 旧配置"上，而 current.txt 已经指向新版本了。这个中间态**不会自己
好起来**——下一次运行看到 current.txt 已经是最新版本，会直接判定"无需更新"、退出 0，
心跳还报着新版本号，一万公里外的维护者收不到任何信号。维护者为"别再瞎编价格"发的那版
SOUL.md 修复，就此永远不会落地。

校对靠 data/.factory_version（FACTORY_STAMP）这个戳：它记着 data/ 里的出厂状态已经收敛到
哪个版本，由 apply_factory_state 在**全部写完之后**才落盘。规则：

    current.txt 里的版本 != data/.factory_version  →  先重新应用一遍出厂状态，再干别的

更新器（每次登录都跑、而且持着 update.lock）必须做这件事，因为它是唯一保证会跑的进程；
Task 6 的启动器**也**应该做（纵深防御），但更新器不能依赖它——启动器还不存在，而"更新
静默失效"这个故障今天就已经能发生了。

⚠️ 待办（心跳任务，不在本次改动范围内）：自愈收敛不了时（母版不可用，或母版没问题但
这台机器套用不上），更新器今天只把这条消息 print 出来——而没有人会看这台机器的 stdout。
下一个发行版带的母版会在下一次开机自动补上这次收敛，所以不落地不是错误，但维护者应该
**主动**知道有机器停在这个中间态，而不是被动等它自愈。HEARTBEAT_PREFIX（见下面
"更新通道心跳"一节，ADR-0004）已经在契约里；心跳那一行应该多带一个字段，标记"这台机器
这次跳过了出厂状态自愈"，而不是让这个信号止步于一条没人读的 print()。

⚠️ 待维护者决策（Task 8 别自己悄悄替它选一个答案）：terminal.cwd 是**按 Windows 用户**
算的（桌面是每个用户各自的），而 data/config.yaml 是**全用户共享**的（INSTALL_ROOT 故意
不含用户名，铁律一）。同一台机器上两个 Windows 账号各自的 ONLOGON 任务都会渲染一遍
config.yaml——最后跑的那个把**它自己的**桌面路径盖进共享配置里，另一个账号的 agent 于是
把文件写到别人的桌面上。两条路：(a) 明确假设"一台机器一个账号"（当前实现的隐含假设，
最省事，也符合"一位长辈一台电脑"的实际场景），(b) 让 cwd 按用户走（config.yaml 拆成
per-user，或 Hermes 支持环境变量展开）。这是产品决策，不是实现细节。

===============================================================================
布局
===============================================================================

    C:\\Users\\Public\\xiaozhushou\\          ← INSTALL_ROOT（不含用户名）
    │
    ├── data\\                               ← HERMES_HOME：用户的东西，跨版本存活
    │   ├── .env                            ← 激活码（用户独有，永不下发、永不覆盖）
    │   ├── .factory_version                ← 出厂状态已收敛到哪个版本（见上面的契约要求）
    │   ├── config.yaml                     ← 由版本的模板渲染而来（切版本时重渲染）
    │   ├── SOUL.md                         ← 由版本下发（切版本时覆盖）
    │   ├── skills\\                         ← 出厂技能（版本下发）+ 习得技能（用户的）混在一起
    │   ├── sessions\\、memories\\、logs\\     ← 用户的，永不触碰
    │   └── （Hermes 自建的其他目录）
    │
    ├── versions\\<版本号>\\                  ← 版本的东西：更新时整个替换，回滚时整个切回
    │   ├── hermes-agent\\                   ← 代码 + venv（绝对路径烧死在这个路径上）
    │   ├── python\\                         ← pyvenv.cfg 的 home 指向这里
    │   ├── ms-playwright\\                  ← PLAYWRIGHT_BROWSERS_PATH 指向这里
    │   ├── factory\\                        ← 本版本的出厂原件（只读母版）
    │   │   ├── config.yaml.tmpl
    │   │   ├── SOUL.md
    │   │   └── skills\\                     ← 出厂技能母版（深度恢复据此重铺，ADR-0005）
    │   └── tools\\                          ← recover.py / updater.py / launcher.py
    │                                          （必须随版本走：更新器是唯一的远程修复通道，
    │                                           它自己不可更新的话，它的 bug 就永远修不掉）
    │
    ├── current.txt、previous.txt、bad_versions.txt
    ├── channel.json、update.lock
    └── *.cmd                               ← 稳定入口（快捷方式与计划任务指向这里）
                                              内容只有"读 current.txt → exec 对应版本的 python+脚本"，
                                              永不需要更新。快捷方式绝不能直接指向 versions/<v>/，
                                              那个路径会在两次更新后被清理掉。

⚠️ 装配机必须把每个版本装到它自己的 versions/<版本号>/ 路径下，让绝对路径按该版本的
最终位置烧进去——这样回滚到旧版本时，旧目录里的路径依然正确。

⚠️ 待 Task 8 在装配机上实测：
  1. -HermesHome 与 -InstallDir 分离（spike 当时是嵌套装的）。上游源码确认这是设计支持的
     用法（config.py 原文："$HERMES_HOME is a shared DATA directory"、install stamp
     "lives in the install tree, not in $HERMES_HOME, so that two installs sharing one
     data directory do not overwrite each other's marker"），但没实测过。
  2. **staging 目录的路径长度**。最终布局实测 68 字符（比 spike 成功建树的 69 字符还短
     1 个），不是风险；但更新时解压用的 versions/.s-<uuid>/ 前缀更长，而 node_modules
     嵌套很深，Win10/11 家庭版默认没开 LongPathsEnabled，MAX_PATH=260 会咬人。
     实测命令（枚举本身就需要 \\\\?\\ 前缀）：
         Get-ChildItem -LiteralPath "\\\\?\\C:\\Users\\Public\\xiaozhushou\\versions\\0.1.0" \\
             -Recurse -Force | ForEach-Object { $_.FullName.Length } | Measure-Object -Maximum
"""

# ---- 固定安装根（不含用户名，铁律一）----
INSTALL_ROOT = r"C:\Users\Public\xiaozhushou"

# ---- INSTALL_ROOT 下的顶层布局 ----
DATA_DIR_REL = "data"                  # HERMES_HOME
VERSIONS_DIR = "versions"

# ---- 版本切换状态（相对 INSTALL_ROOT）----
CURRENT_FILE = "current.txt"
PREVIOUS_FILE = "previous.txt"         # 可能不存在 = 诚实地表示"无回滚目标"
BAD_VERSIONS_FILE = "bad_versions.txt"
CHANNEL_FILE = "channel.json"          # {"url": ..., "pubkey": ...}
LOCK_FILE = "update.lock"              # 更新器的 OS 级排他锁

# ---- 稳定入口（相对 INSTALL_ROOT）----
# 桌面快捷方式与"登录时更新"计划任务只能指向这几个 .cmd。它们内容极简
# （读 current.txt → exec versions/<cur>/ 下的 python 与脚本），永不需要更新。
# 直接指向 versions/<v>/ 的快捷方式会在该版本被清理后变成死链接。
LAUNCH_CMD = "小助手.cmd"
REPAIR_CMD = "小助手修复.cmd"
UPDATE_CMD = "小助手更新.cmd"

# ---- 一个版本目录内的布局（相对 versions/<版本号>/，**不是**相对 INSTALL_ROOT）----
AGENT_DIR_REL = "hermes-agent"
VENV_PYTHON_REL = r"hermes-agent\venv\Scripts\python.exe"
HERMES_EXE_REL = r"hermes-agent\venv\Scripts\hermes.exe"
DESKTOP_APP_REL = r"hermes-agent\apps\desktop"
ELECTRON_EXE_REL = r"hermes-agent\node_modules\electron\dist\electron.exe"
BASE_PYTHON_REL = r"python\cpython-3.11.15-windows-x86_64-none\python.exe"
PLAYWRIGHT_DIR_REL = "ms-playwright"
TOOLS_DIR_REL = "tools"                # 随版本走，更新器才能更新它自己
FACTORY_DIR_REL = "factory"            # 本版本的出厂原件（只读母版）

# ---- 出厂原件内的文件（相对 versions/<v>/factory/）----
FACTORY_CONFIG_TMPL = "config.yaml.tmpl"
FACTORY_SOUL = "SOUL.md"
FACTORY_SKILLS = "skills"

# ---- HERMES_HOME（= data/）内的文件（相对 DATA_DIR_REL）----
ENV_FILE = ".env"                      # 激活码：DASHSCOPE_API_KEY。用户独有，永不下发
CONFIG_FILE = "config.yaml"            # 由 factory/config.yaml.tmpl 渲染而来
SOUL_FILE = "SOUL.md"                  # 由 factory/SOUL.md 覆盖
SKILLS_DIR = "skills"                  # 出厂技能与习得技能混在一起
FACTORY_STAMP = ".factory_version"     # data/ 里的出厂状态已经收敛到哪个版本（见上面的契约）

# 切版本时绝不触碰的用户资产。任何"重铺/清理"逻辑都必须先排除这些。
USER_OWNED = (ENV_FILE, "sessions", "memories", "logs")

# ---- 环境变量（**必须**由每个启动 Hermes 的进程导出）----
# 不设 HERMES_HOME 的后果：Hermes 会**静默地**在 %LOCALAPPDATA%\hermes 建一个全新的空 home
# ——没有激活码、没有技能、没有历史——而且不报任何错。整个 data/ 分离方案无声蒸发。
# PLAYWRIGHT_BROWSERS_PATH 同理：spike 实测它是浏览器落点的唯一控制项。
HERMES_HOME_ENV = "HERMES_HOME"                    # → <root>\data
PLAYWRIGHT_ENV = "PLAYWRIGHT_BROWSERS_PATH"        # → <root>\versions\<cur>\ms-playwright

# ---- 工作台 ----
WORKSPACE_DIRNAME = "小助手"            # 桌面上的工作台文件夹名

# ---- 模型接入（ADR-0002：激活码 = 维护者按人分配的 DashScope key）----
# 路由查证结论（依据见 factory/config.yaml.tmpl 顶部注释）：DASHSCOPE_API_KEY 存在时，
# provider: "auto" 会在 hermes_cli/auth.py::resolve_provider() 里被自动检测到 provider id
# "alibaba"（PROVIDER_REGISTRY["alibaba"].api_key_env_vars 含 DASHSCOPE_API_KEY），
# 不需要任何 "dashscope/<model>" 前缀路由。
#
# ⚠️ .env 里绝不能出现 OPENAI_API_KEY / OPENROUTER_API_KEY——resolve_provider() 会短路，
# 抢在 DashScope 之前命中。
#
# ⚠️ DASHSCOPE_BASE_URL 的默认值是国际版（dashscope-intl）端点。最终用户在大陆，必须
# 显式覆盖成北京端点，否则会慢或不通。
ACTIVATION_ENV_KEY = "DASHSCOPE_API_KEY"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# ---- 更新通道心跳（ADR-0004）----
# 更新器每次运行往 OSS 的 heartbeat/ 前缀写一行 <机器ID>:<版本>:<时间戳>:<结果>，
# 让维护者能看出哪台机器掉队了。单向、只 PUT、无服务端逻辑。
HEARTBEAT_PREFIX = "heartbeat"
