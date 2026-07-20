; installer/setup.iss —— 「小助手」离线安装器
; 用 ISCC 编译：
;   ISCC.exe setup.iss /DVersion=0.1.0 /DChannelUrl=https://<bucket>.oss-cn-beijing.aliyuncs.com/channel /DPubKey=<channel.pub 内容>
;
; 本脚本把真机预演（Task 8）验证过的所有约束编码进去：
; - 三方布局：data\（HERMES_HOME，用户数据）+ versions\<v>\（程序）+ 稳定 .cmd 入口
; - 固定安装根 C:\Users\Public\xiaozhushou（不含用户名——venv 绝对路径的前提）
; - pythonw.exe 隐藏窗口启动（不弹黑控制台）
; - cd /d <版本目录> && python -m tools.xxx（工作目录让 import 命中我们的包、绕开 Hermes 的 tools）
; - HERMES_HOME / PLAYWRIGHT_BROWSERS_PATH 环境变量由 .cmd 导出
; - 激活码写进 data\.env 的 DASHSCOPE_API_KEY（ADR-0002）

#ifndef Version
  #define Version "0.1.0"
#endif
#ifndef ChannelUrl
  #define ChannelUrl "https://example.oss-cn-beijing.aliyuncs.com/channel"
#endif
#ifndef PubKey
  #define PubKey "0000000000000000000000000000000000000000000000000000000000000000"
#endif

; 固定安装根（不含用户名，铁律一）。Inno 没有 C:\Users\Public 的内置常量，
; {commonappdata} 是 C:\ProgramData，不对——必须硬编码这个路径。
#define InstallRoot "C:\Users\Public\xiaozhushou"

[Setup]
AppName=小助手
AppVersion={#Version}
AppPublisher=家庭自用
DefaultDirName={#InstallRoot}
; 不让用户改安装目录：venv 里烧死的绝对路径依赖固定根，改了就全断
DisableDirPage=yes
DisableProgramGroupPage=yes
; 全用户共享根，装到 Public 需要管理员权限（创建目录树 + 改 ACL）
PrivilegesRequired=admin
OutputBaseFilename=小助手安装器-{#Version}
Compression=lzma2/max
SolidCompression=yes
; 目标机没有代码签名证书，SmartScreen 会拦——这是已知的，维护者装机时手动放行
; （Task 8 待办：给安装器做代码签名，避免每台机器都要放行）
WizardStyle=modern

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
; payload\ = 组装好的一个版本目录内容（hermes-agent + python + ms-playwright + factory + tools + builder）
; 装配脚本（Task 8 的组装阶段）先把这些放进 payload\，编译时打进 exe。
; DestDir 是 versions\<版本号>\——绝对路径就烧死在这条路径上（与装配机组装时一致，否则回滚会断）。
Source: "payload\*"; DestDir: "{#InstallRoot}\versions\{#Version}"; Flags: recursesubdirs createallsubdirs

; 稳定入口 .cmd：装到安装根，快捷方式与计划任务指向它们（永不随版本变化）
Source: "cmd\小助手.cmd";     DestDir: "{#InstallRoot}"; Flags: ignoreversion
Source: "cmd\小助手修复.cmd"; DestDir: "{#InstallRoot}"; Flags: ignoreversion
Source: "cmd\小助手更新.cmd"; DestDir: "{#InstallRoot}"; Flags: ignoreversion

[Dirs]
; 桌面工作台文件夹
Name: "{userdesktop}\小助手"
; data\（HERMES_HOME）由安装器建空目录；出厂状态在下面 [Code] 的 CurStepChanged(ssPostInstall) 里由 launcher --init 首次应用
Name: "{#InstallRoot}\data"

[Icons]
; 桌面快捷方式指向稳定 .cmd（不指向 versions\<v>\——那个路径两次更新后会被清理）
Name: "{userdesktop}\小助手";   Filename: "{#InstallRoot}\小助手.cmd"
Name: "{userdesktop}\小助手修复"; Filename: "{#InstallRoot}\小助手修复.cmd"

[Run]
; ⚠️ 出厂状态的初始化**不能**放在这里（真机实测）：[Run] 段执行在 CurStepChanged(ssPostInstall)
; 之前，而 current.txt 正是在 ssPostInstall 里写的。launcher --init 会读 current.txt 决定
; 初始化哪个版本，读不到就（正确地）拒绝猜、返回非零——而 runhidden 把这个失败藏得严严实实，
; 结果是每台机器都装出一个 data/ 里只有 .env 的空壳：没有 SOUL.md、没有 config.yaml、
; 没有 72 个出厂技能。已挪进 CurStepChanged（见下面的 [Code]），排在写 current.txt 之后。

; 登录时静默检查更新的计划任务，指向稳定 .cmd
Filename: "schtasks"; \
  Parameters: "/Create /F /SC ONLOGON /TN ""小助手更新"" /TR ""\""{#InstallRoot}\小助手更新.cmd\"""""; \
  Flags: runhidden; \
  StatusMsg: "正在设置自动更新……"

[Code]
var
  KeyPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  KeyPage := CreateInputQueryPage(wpSelectDir,
    '激活码', '请输入激活码',
    '找发放安装包的人要一串激活码，输入后点下一步。装完以后日常使用不需要它。');
  KeyPage.Add('激活码：', False);
end;

// 激活码的唯一来源。交互安装（爸妈那条路）走向导页；静默安装走 /KEY= 命令行参数
// ——静默模式下向导页是跳过的，Values[0] 恒为空，没有这个分支的话 NextButtonClick 会
// 弹「激活码不能为空」、Result:=False，而静默模式没人能点下一步：安装器永远挂着，
// ssPostInstall 里的 .env / current.txt / channel.json 一个都写不出来（真机实测）。
// 静默安装是**验收用的**（装机验证、更新演练），产品交付路径不用它。
function ActivationKey(): String;
begin
  if WizardSilent then
    Result := Trim(ExpandConstant('{param:KEY|}'))
  else
    Result := Trim(KeyPage.Values[0]);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = KeyPage.ID) and (not WizardSilent) and (Trim(KeyPage.Values[0]) = '') then
  begin
    MsgBox('激活码不能为空。', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  envPath: String;
  envContent: String;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // 激活码写进 data\.env 的 DASHSCOPE_API_KEY（ADR-0002：不打包，装机时注入）
    // 同时写 DASHSCOPE_BASE_URL 覆盖成北京端点（默认是国际版，大陆会慢/不通）
    envPath := ExpandConstant('{#InstallRoot}\data\.env');
    envContent := 'DASHSCOPE_API_KEY=' + ActivationKey() + #13#10 +
                  'DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1' + #13#10;
    SaveStringToFile(envPath, envContent, False);

    // current.txt / channel.json 写进安装根
    SaveStringToFile(ExpandConstant('{#InstallRoot}\current.txt'), '{#Version}', False);
    SaveStringToFile(ExpandConstant('{#InstallRoot}\channel.json'),
      '{"url": "{#ChannelUrl}", "pubkey": "{#PubKey}"}', False);

    // 出厂状态初始化：渲染 config.yaml、铺 SOUL.md、铺 72 个出厂技能与插件、落 .factory_version 戳。
    // 必须在 current.txt 落盘**之后**（launcher --init 读它决定初始化哪个版本；读不到会拒绝猜）。
    // WorkingDir 设成版本目录：cd 让 import tools 命中我们的包、绕开 Hermes 自带的 tools（真机验证过）。
    // 失败必须**响亮**：静默失败的代价是每台机器装出一个没有人格、没有技能的空壳，
    // 而装机的人以为一切正常（真机上就这么发生过一次）。
    if (not Exec(ExpandConstant('{#InstallRoot}\versions\{#Version}\hermes-agent\venv\Scripts\python.exe'),
                 '-m tools.launcher "' + ExpandConstant('{#InstallRoot}') + '" --init',
                 ExpandConstant('{#InstallRoot}\versions\{#Version}'),
                 SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      MsgBox('小助手初始化失败（错误码 ' + IntToStr(ResultCode) + '）。'#13#10 +
             '装出来的小助手会缺少技能和设置，请把这个错误告诉发放安装包的人。',
             mbError, MB_OK);
  end;
end;
