; eggpaper 安装包（Inno Setup 6）
;
; 由 tools/build_installer.py 调用，版本号通过 /DMyVersion=0.2.0 传进来。
; 手动编译：ISCC.exe /DMyVersion=0.1.0 installer\eggpaper.iss
;
; 四个关键决定：
;   1. **AppId 固定**：升级靠它认出"这是同一个软件"。改了就变成两份并存，
;      用户的快捷方式还指着旧的那份。永远不要动这个 GUID。
;   2. **PrivilegesRequired=lowest**：装到 {localappdata}\Programs\eggpaper，
;      全程不弹 UAC。用户想换盘（D:\eggpaper）在目录页改就是——那一页是开着的。
;   3. **用户数据不在 [Files] 里，也不在 [UninstallDelete] 里**：config.yaml、
;      文库 PDF、SQLite 全在 %LOCALAPPDATA%\eggpaper\data。所以覆盖安装不会丢东西，
;      卸载也不会——卸载只在用户手动勾选时才问数据的事。
;   4. **升级前先关掉正在跑的旧版本**（CloseApplications + 兜底 taskkill）：
;      我们的 exe 占着自己的文件，不关掉就替换不了。

#define MyAppName "eggpaper"
#ifndef MyVersion
  #define MyVersion "0.1.0"
#endif
#define MyPublisher "eggpaper"
#define MyExe "eggpaper.exe"

[Setup]
AppId={{7C4A1E02-9B3D-4F61-8AE5-2D7B6C1F0A93}
AppName={#MyAppName}
AppVersion={#MyVersion}
AppVerName={#MyAppName} {#MyVersion}
AppPublisher={#MyPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableDirPage=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
; 代码签名证书：有的话填进来（SignTool / signtool 命令），没有就留空——
; 没签名的安装包，Windows 首次运行会提示"未知发布者"，这是正常的
; SignTool=signtool
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename={#MyAppName}-{#MyVersion}-setup
SetupIconFile=eggpaper.ico
UninstallDisplayIcon={app}\{#MyExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
; 关掉"欢迎"页之外的多余向导页，装一次只要点三下
DisableWelcomePage=no
LicenseFile=..\LICENSE

[Languages]
; 只留一个语言。原来写了两行（chinese / english）但都把 MessagesFile 指向
; compiler:Default.isl——于是下拉里两个选项都叫 English，而多语言又会额外弹一个
; "Select Setup Language" 页（用户第一眼看到的就是它，还以为是安装包坏了）。
; Inno 不带中文语言文件（官方 29 个里没有），所以中文靠下面的 [Messages] 直接覆盖——
; 安装向导只露三页，覆盖的就是这三页上那几句话，比塞一份第三方 .isl 干净。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; —— 标题与欢迎页 ——
SetupAppTitle=安装程序
SetupWindowTitle=安装 eggpaper %1
WelcomeLabel1=欢迎安装 eggpaper
WelcomeLabel2=这将把 [name/ver] 装到你的电脑上。%n%n数据（配置、文库、批注）默认存放在安装目录旁的 data 文件夹，升级与卸载都不会动它。%n%n继续前建议先关掉其它正在运行的程序。
; —— 选目录页（安装路径就在这一页改）——
WizardSelectDir=选择安装位置
SelectDirDesc=eggpaper 装到哪？
SelectDirLabel3=安装程序会把 [name] 装到下面这个文件夹。
SelectDirBrowseLabel=点「下一步」继续；想换目录就点「浏览」。
DiskSpaceMBLabel=至少需要 [mb] MB 可用磁盘空间。
; —— 附加任务（桌面快捷方式）——
WizardSelectTasks=附加任务
SelectTasksDesc=还要做哪些事？
SelectTasksLabel2=选好附加任务后点「下一步」。
; —— 准备安装 ——
WizardReady=准备安装
ReadyLabel1=安装程序已准备好，可以开始安装了。
ReadyLabel2a=点「安装」开始；想再看一遍前面的设置就点「上一步」。
PreparingDesc=正在准备安装
; —— 安装中 ——
WizardInstalling=正在安装
InstallingLabel=正在安装 [name]，请稍候……
; —— 完成 ——
FinishedHeadingLabel=eggpaper 安装完成
FinishedLabel=eggpaper 已经装好了，可以从开始菜单或桌面快捷方式启动。
ClickFinish=点「完成」关闭安装程序。
; —— 卸载 ——
UninstallAppFullTitle=卸载 eggpaper
ConfirmUninstall=确定要卸载 %1 吗？%n%n用户数据（配置、文库、批注）不会被删除。
UninstallStatusLabel=正在卸载 %1，请稍候……
UninstalledAll=%1 已从电脑上移除。你的数据还在数据目录里（默认是安装目录旁的 data）。
UninstalledMost=卸载完成。%n%n有些文件没能删除，可以手动删掉。
; —— 按钮 ——
ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安装(&I)
ButtonCancel=取消
ButtonBrowse=浏览(&R)...
ButtonFinish=完成(&F)
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonOK=确定

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
; 整个 onedir 产物（exe + _internal）。注意：用户数据不在这里，也不会被这里覆盖
Source: "..\build\pyi\eggpaper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 升级前先清空整个 _internal 再装新的。**必须整目录清**：升级只覆盖同名文件，
; 旧版本里"改了名就永远留在盘上"的文件会无限累积——实测一台从 0.1.8 一路升上来的
; 机器，_internal 里攒了 154 个化石文件共 21 MB（25 代前端带哈希的旧 chunk、
; pywebview 实验时代被撤掉的 pythonnet/webview……），没有任何机制会再去删它们。
; 先删后装，安装目录永远等于这次构建的准确内容。
Type: filesandordirs; Name: "{app}\_internal"
; 网页图标搬进 dist\icons\ 之后，这几张旧位置的还在——留着就会"明明换了图标，
; 服务端还能吐出旧的那张"，排查时能白耗半天。整目录清后这条本可去掉，留着防的是
; 更古老的版本（还没有 _internal 结构时）留下的散落文件。
Type: files; Name: "{app}\_internal\frontend\dist\icon-192.png"
Type: files; Name: "{app}\_internal\frontend\dist\icon-512.png"
Type: files; Name: "{app}\_internal\frontend\dist\favicon-256.png"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyExe}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyExe}"; Tasks: desktopicon

[Registry]
; 文件关联：**只做"可选项"，不抢 .pdf 的默认程序**。
; 一个读论文的工具把 PDF 默认程序顶掉，会把人家双击 PDF 的习惯整个改掉（Edge/Acrobat
; 在前），这不是加分项而是冒犯。所以走三条不改默认的路：
;   ① OpenWithProgids →「打开方式」列表里有它；
;   ② SystemFileAssociations 的 verb → 右键菜单多一条「用 eggpaper 打开」；
;   ③ Applications →「选择其他应用」里能找到它（且只对 .pdf 生效，见 SupportedTypes）。
Root: HKCU; Subkey: "Software\Classes\eggpaper.pdf"; ValueType: string; ValueName: ""; \
    ValueData: "PDF 论文（eggpaper）"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\eggpaper.pdf\DefaultIcon"; ValueType: string; ValueName: ""; \
    ValueData: "{app}\{#MyExe},0"
Root: HKCU; Subkey: "Software\Classes\eggpaper.pdf\shell\open\command"; ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#MyExe}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; \
    ValueName: "eggpaper.pdf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\eggpaper"; \
    ValueType: string; ValueName: ""; ValueData: "用 eggpaper 打开"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\eggpaper"; \
    ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyExe},0"
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\eggpaper\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#MyExe}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyExe}\SupportedTypes"; \
    ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyExe}\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#MyExe}"" ""%1"""; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyExe}"; ValueType: string; \
    ValueName: "FriendlyAppName"; ValueData: "eggpaper"

[Run]
; 交互安装：装完问一句要不要现在打开
Filename: "{app}\{#MyExe}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
; 静默升级（用户点的是「立即重启并安装」）：装完直接把它拉起来，别让软件凭空消失
Filename: "{app}\{#MyExe}"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
; 只清程序目录自己生成的东西。**不要**在这里加任何指向用户数据的路径
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// 兜底：CloseApplications 走的是 Restart Manager，万一它没认出我们的进程
// （比如软件是被别的方式拉起来的），这里再按映像名清一次，免得替换文件失败
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    Exec('taskkill.exe', '/F /IM {#MyExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

// 这里**不要**去动 WizardForm.DirEdit：Inno 会先把「上次装在哪」填进目录页，
// 静默升级（/SILENT）也靠这个记忆回到原目录。手写一行 SetText 就等于：
//   ① 吃掉 /DIR= 参数（本次实测：装到了默认目录，而不是指定目录）
//   ② 升级时把老目录忘掉，装出第二份，正在跑的那份永远升不上去
