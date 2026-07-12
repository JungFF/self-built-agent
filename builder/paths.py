# builder/paths.py
"""发行版内的路径契约。

全部值来自 2026-07-11 的 Windows 快照 spike（装配机：阿里云硅谷 Windows Server 2022，
Hermes v0.18.2 / commit 4281151a）。改动这些常量需要重新跑 Task 2 的 spike 验证。

核心约束：安装根不含 Windows 用户名。Hermes 的 venv 会把绝对路径烧进 pyvenv.cfg 和每个
Scripts/*.exe 的 shebang，只有固定根才能让装配机生成的路径在目标机上原样成立。
"""

# ---- 固定安装根（不含用户名，这是整个方案成立的前提）----
INSTALL_ROOT = r"C:\Users\Public\xiaozhushou"

# ---- 固定根下的三大件（相对 INSTALL_ROOT）----
HERMES_HOME_REL = "hermes"
PYTHON_DIR_REL = "python"
PLAYWRIGHT_DIR_REL = "ms-playwright"

# ---- 可执行文件（相对 INSTALL_ROOT）----
VENV_PYTHON_REL = r"hermes\hermes-agent\venv\Scripts\python.exe"
HERMES_EXE_REL = r"hermes\hermes-agent\venv\Scripts\hermes.exe"
BASE_PYTHON_REL = r"python\cpython-3.11.15-windows-x86_64-none\python.exe"
DESKTOP_APP_REL = r"hermes\hermes-agent\apps\desktop"

# ---- Hermes home 内的文件（相对 HERMES_HOME）----
ENV_FILE = ".env"                      # 激活码：DASHSCOPE_API_KEY
CONFIG_FILE = "config.yaml"
SOUL_FILE = "SOUL.md"
SKILLS_DIR = "skills"
BUNDLED_MANIFEST = r"skills\.bundled_manifest"   # 出厂技能清单；深度恢复靠它区分习得技能

# ---- 版本管理（相对 INSTALL_ROOT，供 Task 5 更新器 / Task 6 启动器用）----
VERSIONS_DIR = "versions"
CURRENT_FILE = "current.txt"
PREVIOUS_FILE = "previous.txt"
BAD_VERSIONS_FILE = "bad_versions.txt"
CHANNEL_FILE = "channel.json"          # {"url": ..., "pubkey": ...}

# ---- 工作台 ----
WORKSPACE_DIRNAME = "小助手"            # 桌面上的工作台文件夹名

# ---- 模型接入（ADR-0002：激活码 = 维护者按人分配的 DashScope key）----
# Hermes 原生支持 DashScope，凭证项定义在 hermes_cli/config.py。
# 注意：DASHSCOPE_BASE_URL 的默认值是国际版（coding-intl / dashscope-intl）端点，最终用户
# 在大陆，必须显式覆盖成北京端点，否则会慢或不通。
#
# 路由查证结论（见 factory/config.yaml.tmpl 顶部注释的详细依据）：DASHSCOPE_API_KEY
# 存在时，provider: "auto" 会在 hermes_cli/auth.py::resolve_provider() 里被自动检测到
# provider id "alibaba"（PROVIDER_REGISTRY["alibaba"].api_key_env_vars 含
# DASHSCOPE_API_KEY），不需要任何 "dashscope/<model>" 前缀路由。
ACTIVATION_ENV_KEY = "DASHSCOPE_API_KEY"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
