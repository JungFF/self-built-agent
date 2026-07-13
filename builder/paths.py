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
    │   ├── python-base\\                    ← pyvenv.cfg 的 home 指向这里（Task 8 真机更正）
    │   ├── ms-playwright\\                  ← PLAYWRIGHT_BROWSERS_PATH 指向这里
    │   ├── factory\\                        ← 本版本的出厂原件（只读母版）
    │   │   ├── config.yaml.tmpl
    │   │   ├── SOUL.md
    │   │   └── skills\\                     ← 出厂技能母版（深度恢复据此重铺，ADR-0005）
    │   ├── tools\\                          ← recover.py / updater.py / launcher.py
    │   │                                      （必须随版本走：更新器是唯一的远程修复通道，
    │   │                                       它自己不可更新的话，它的 bug 就永远修不掉）
    │   ├── builder\\                        ← 运行时路径契约，跟 tools\\ **平级**（不能塞进
    │   │                                      tools\\ 内部——见下面"真机实测确认"那条）。运行时
    │   │                                      只需要 __init__.py + paths.py 这两个文件；
    │   │                                      factory.py / keys.py / release.py 是装配机专用的
    │   │                                      打包/签名代码，跟机器无关，不必下发（下发了也
    │   │                                      无害，纯属多余字节——keys.py 例外：见下面
    │   │                                      BUILDER_DIR_REL 的注释，它会诱导下一个人把
    │   │                                      secrets/ 一起拷进包）。tools\\ 里每个模块都
    │   │                                      `from builder.paths import ...`：少了 paths.py，
    │   │                                      机器上的更新器每次开机 ModuleNotFoundError，
    │   │                                      而更新器正是唯一能把这台机器救回来的东西。
    │   │                                      ⚠️ 由此推出一条**调用约定**：tools\\ 里的脚本只能
    │   │                                      以 versions\\<版本号>\\ 为工作目录（或用 -m）来跑
    │   │                                      ——`python versions\\<v>\\tools\\updater.py` 会把
    │   │                                      sys.path[0] 设成 tools\\，`builder` 和 `tools` 两个
    │   │                                      包都 import 不到。稳定入口的 .cmd 必须先 cd 过去
    │   │                                      ——这个 cd 还身兼一条安全职责，见下面真机实测。
    │   └── heartbeat.json                   ← 受限 OSS 凭证（ADR-0004，可缺省）。见下面
    │                                          HEARTBEAT_CRED_FILE：随**版本**下发，不是装机时
    │                                          烧进 channel.json——那是它唯一能被轮换的路径。
    │
    ├── current.txt、previous.txt、bad_versions.txt、last_good.txt
    ├── startup_failures.txt、desktop.pid    ← 启动器的旁证账本（见下面两个常量）
    ├── channel.json、update.lock、launch.lock
    └── *.cmd                               ← 稳定入口（快捷方式与计划任务指向这里）
                                              内容只有"读 current.txt → exec 对应版本的 python+脚本"，
                                              永不需要更新。快捷方式绝不能直接指向 versions/<v>/，
                                              那个路径会在两次更新后被清理掉。

⚠️ 装配机必须把每个版本装到它自己的 versions/<版本号>/ 路径下，让绝对路径按该版本的
最终位置烧进去——这样回滚到旧版本时，旧目录里的路径依然正确。

===============================================================================
真机实测确认（2026-07-11，装配机首次跑通端到端流程后回填）
===============================================================================

⚠️ **tools\\ 与 builder\\ 平级**这条不是纸面推导，是真的在装配机上验证过的：把两者按
上面的树摆好之后，`cd versions\\0.1.0\\ && python -m tools.updater` 里 `tools.updater`
和 `builder.paths` 都解析到我们自己的文件；把 builder\\ 改放到 tools\\ 内部（而不是跟它
平级），同一条命令的 import 直接失败。

⚠️ 上面"稳定入口的 .cmd 必须先 cd 过去"不只是图省事、也不只是为了 sys.path[0]——真机
实测还揪出了一个包名撞车：Hermes 是 editable install（venv 的 site-packages 里躺着
`__editable__.hermes_agent-0.18.2.pth`），而 **Hermes 自己也有一个顶层 `tools` 包**
（hermes-agent\\tools\\__init__.py）。我们的 tools\\、builder\\ 用的又恰好是同样的顶层
包名，而更新器/启动器**必须**跑在 Hermes 自带的那个 venv 上（要用它装好的
cryptography，不能自己另起一个）。真机验证结论：`import tools` 之所以解析到我们自己
的 tools\\，纯粹是因为当前工作目录（sys.path[0]）在路径查找顺序里排在 editable-install
的 finder 前面——**去掉这个 cd，`import tools` 会静默地绑定到 Hermes 自己的 tools 包**，
在爸妈的机器上就是一次完全摸不着头脑的失败。所以这个 `cd` 是**安全要求**，不是习惯
写法：Task 8 的稳定入口 .cmd 里 `cd /d <版本目录> && python -m tools.updater` 这条命令，
删掉 cd 就等于让更新器悄悄换了一个模块在跑；启动器（Task 6）将来如果也要拉起
tools\\ 下的脚本，同样必须先 cd 过去，理由一样。

⚠️ 待验证（GBK）：zh-CN Windows 上 Python 的默认 stdout 编码是 GBK（cp936）。AST 扫描过
tools/*.py 里全部 print() 与 argparse 的 help=/description= 字符串字面量，确认都是纯
中文、不含 emoji，GBK 可编码——这几条运行时路径是安全的（测试见
tests/test_factory.py 的 GBK 扫描测试）。但**模块 docstring 和注释里散落着 ⚠️
（U+26A0 + U+FE0F），GBK 编不了它**：今天没有任何代码把带 ⚠️ 的 docstring 接到 argparse
的 description/help 上，风险低（终端用户不会主动跑 --help），但记在这里是为了 Task 8
别图省事把某个模块 docstring 直接接给 argparse 当 description——那在真机上会是一次
UnicodeEncodeError。真要接的话，先把 ⚠️ 从那份 docstring 里删掉，或者换成 ASCII 的
"WARN:" 之类。

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
# 拉黑名单：更新器读它，永远不再自动装里面的版本。
#
# ⚠️ 待办（心跳任务，ADR-0004，不在 Task 6 范围内）：**这个文件只增不减，而且没有任何东西
# 会主动告诉一万公里外的维护者**。今天它只在本机留下痕迹（ToDesk 进去能看见）。两件事欠着：
#   1. 心跳那一行必须**带上 bad_versions.txt 的内容**——否则"这台机器拉黑过一个版本"这个
#      信号止步于一个没人读的文件，维护者永远不知道哪台机器掉出了更新通道。
#   2. 需要一条**维护者侧的解除拉黑通道**（比如通道里下发一个 unblacklist 列表，更新器据此
#      把对应行从 bad_versions.txt 里删掉）。没有它，任何一次误判都是**永久**的：更新器只装
#      比 current 更新的版本，被误拉黑的那个版本再也回不来。启动器已经尽力不误判（旁证 + PID
#      观察 + 回滚失败时撤销拉黑，见 tools/launcher.py），但"不可逆的动作没有解除路径"这件事
#      本身就是设计缺口，不能靠"我们保证不误判"来补。
BAD_VERSIONS_FILE = "bad_versions.txt"
CHANNEL_FILE = "channel.json"          # {"url": ..., "pubkey": ...}
LOCK_FILE = "update.lock"              # 更新器的 OS 级排他锁

# 上一次**活过健康窗口**的版本（启动器写）。它是回滚的军械开关：只有从没被证明健康过的
# 版本才允许被回滚 + 拉黑。没有它，一次普通的"用户把窗口关掉"或"双击第二下把已经在跑的
# 应用又启动一次"（Electron 单实例锁会让第二个实例瞬间以 0 退出——和"启动即崩"在退出码和
# 存活时间上完全一样）就会**静默地**把一个跑得好好的版本回滚掉、并永久拉黑它。
LAST_GOOD_FILE = "last_good.txt"
# 启动器的 OS 级排他锁（同一时刻只允许一个启动器）。健康窗口里启动器要阻塞几十秒，长辈
# 没看见窗口就再双击一下是常态；没有锁的话第二个启动器会在第一个还没来得及记下 last_good
# 的当口判定"启动即崩"，把好版本回滚掉。OS 级锁在进程消失时由内核释放，不会像标记文件
# 那样在断电后留下永久残骸。
LAUNCH_LOCK_FILE = "launch.lock"

# 某个版本已经被**独立观察到**几次"启动即崩"（内容：<版本号> <次数>）。回滚 + 拉黑是不可逆的
# （见 BAD_VERSIONS_FILE），而"启动即崩"这个观察本身会误报——单实例回弹、长辈手滑关窗口、
# 一次瞬时的杀软锁，长得和真崩溃一模一样。真坏的版本每次双击都崩，假崩溃不会重演：所以开火
# 前要求旁证。读不出/写不下这个文件时，计数永远凑不满 → 永远不回滚（安全方向）。
STARTUP_FAILURES_FILE = "startup_failures.txt"
# 上一次**活过健康窗口**的那个桌面端进程（内容：<版本号> <PID>）。用来把"小助手已经在跑了"
# 从**推断**变成**观察**：长辈重复双击时，第二个 Electron 实例被单实例锁瞬间弹回（退出码 0），
# 而那个真正在跑的进程还活着——PID 还在，就绝不是"启动即崩"。
DESKTOP_PID_FILE = "desktop.pid"

# ---- 稳定入口（相对 INSTALL_ROOT）----
# 桌面快捷方式与"登录时更新"计划任务只能指向这几个 .cmd。它们内容极简
# （读 current.txt → exec versions/<cur>/ 下的 python 与脚本），永不需要更新。
# 直接指向 versions/<v>/ 的快捷方式会在该版本被清理后变成死链接。
#
# ⚠️ 待 Task 8 决策（别自己悄悄替它选一个答案）：**.cmd 一定会弹出一个控制台窗口**，而启动器
# 明确要求"无黑框"（tools/launcher.py 的 __main__：健康窗口那 30 秒里，一个黑色控制台会挂在
# 爸妈屏幕上，他们看到的是"小助手旁边多了个奇怪的黑框框"，然后打电话）。这两条契约今天是
# **互相矛盾**的。可选的隐藏窗口机制（.vbs shim / .lnk 里指定 pythonw.exe / start /min /
# 计划任务的"不显示窗口"），各有代价（杀软对 .vbs 敏感、.lnk 不是纯文本没法随版本下发……），
# Task 8 必须在装配机上实测后**显式**选一个并改这里，不能在实现里静默发明一个。
LAUNCH_CMD = "小助手.cmd"
REPAIR_CMD = "小助手修复.cmd"
UPDATE_CMD = "小助手更新.cmd"

# ---- 一个版本目录内的布局（相对 versions/<版本号>/，**不是**相对 INSTALL_ROOT）----
AGENT_DIR_REL = "hermes-agent"
VENV_PYTHON_REL = r"hermes-agent\venv\Scripts\python.exe"
HERMES_EXE_REL = r"hermes-agent\venv\Scripts\hermes.exe"
DESKTOP_APP_REL = r"hermes-agent\apps\desktop"
ELECTRON_EXE_REL = r"hermes-agent\node_modules\electron\dist\electron.exe"
# Task 8 真机实测更正：venv 是 uv 建的，base python 原本指向装配机
# administrator 用户目录下的 uv 缓存——**装到目标机器上会直接断**。真机上
# 已经把 base python 拷进版本目录、固定为 python-base\，pyvenv.cfg 的
# home 也已经写死指向这个路径——契约回填成真机验证过的实际值，不是反过来
# 让已经装出去、验证过的产物迁就一个从没在真机上跑过的初始假设。
BASE_PYTHON_REL = r"python-base\python.exe"
PLAYWRIGHT_DIR_REL = "ms-playwright"
TOOLS_DIR_REL = "tools"                # 随版本走，更新器才能更新它自己
FACTORY_DIR_REL = "factory"            # 本版本的出厂原件（只读母版）
# 包里那份路径契约（只有 paths.py，没有 builder 的其余部分）。tools/ 里每个模块都
# `from builder.paths import ...`——少了它，机器上的更新器/启动器/修复器**一个都 import
# 不起来**。签名代码（builder/keys.py）和打包代码（builder/release.py）绝不进包：它们跟
# 机器无关，而 keys.py 出现在包里只会诱导下一个人把 secrets/ 一起拷进去。
BUILDER_DIR_REL = "builder"

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
LOGS_DIR = "logs"                      # 用户的（Hermes 自己也往这里写），切版本绝不触碰
LAUNCHER_LOG = "launcher.log"          # 相对 data/logs/：启动器留给维护者的痕迹
                                       # （回滚/起不来都只在这里留痕：没人会看这个进程的
                                       #  stdout——它跑在 pythonw 下，连控制台都没有）

# 切版本时绝不触碰的用户资产。任何"重铺/清理"逻辑都必须先排除这些。
USER_OWNED = (ENV_FILE, "sessions", "memories", LOGS_DIR)

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

# ---- 更新通道（OSS 静态托管，ADR-0003）----
# 通道里只有三样东西：dist-<版本号>.zip、manifest.sig、manifest.json。桶是**公共读**的
# （更新器匿名 GET），所以这个前缀底下**永远不许出现第四样东西**——尤其是 channel.key。
CHANNEL_PREFIX = "channel"

# ---- 更新通道心跳（ADR-0004）----
# 更新器每次运行往 OSS 的 heartbeat/ 前缀写一行 <机器ID>:<版本>:<时间戳>:<结果>，
# 让维护者能看出哪台机器掉队了。单向、只 PUT、无服务端逻辑。
HEARTBEAT_PREFIX = "heartbeat"

# 那份受限凭证在版本目录里的落点（versions/<版本号>/heartbeat.json，可缺省 = 本版本不带心跳）。
#
# 为什么随**版本**下发，而不是装机时烧进 channel.json：channel.json 是安装器写的，装完之后
# **没有任何东西会再改它**——凭证一旦要轮换（被人从包里抠出来灌垃圾了、或者 AK 到期），
# 已经装出去的机器就永远拿不到新的那份。随版本走，则轮换 = 发一个新版本，机器下次开机自己
# 收到；而且它落在签名覆盖的范围里（清单签的 sha256 覆盖整个 zip）。
#
# ⚠️ **必须当这份凭证已经泄漏了来设计。** 包就挂在公共读的 OSS 上，谁都能下载、解压、把它
# 抠出来——它是一个静态的共享密钥，给的是"扩散到维护者控制不了的机器上"的代码用的，它提供
# 不了任何**身份认证**，只提供"不被顺手撞见"。唯一真正限住损失的是 RAM 策略（见
# builder/release.heartbeat_ram_policy）：只 PutObject、只在 heartbeat/ 前缀、不可读、不可
# 列举、**不可删**。由此推出读心跳时唯一站得住的结论：
#   - "某台机器**没有**心跳" —— 可信（没有 DeleteObject，谁也抹不掉一条已经写上去的记录）；
#   - "某台机器说它一切正常" —— **不可信**（谁都能 PUT 一条覆盖上去）。
# 心跳是运维提示，绝不是安全控制，更不能拿来给任何东西授权。
HEARTBEAT_CRED_FILE = "heartbeat.json"
# 凭证的字段（写的人和读的人共用这一份定义：形状一漂移，心跳就静默地 403 到死）。
HEARTBEAT_CRED_FIELDS = ("endpoint", "bucket", "prefix", "access_key_id", "access_key_secret")
