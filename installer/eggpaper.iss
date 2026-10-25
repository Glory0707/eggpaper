; eggpaper 安装包（Inno Setup 6）
;
; 由 tools/build_installer.py 调用，版本号通过 /DMyVersion=<VERSION> 传进来。
; 手动编译：ISCC.exe /DMyVersion=0.1.3 installer\eggpaper.iss
;
; 六个关键决定：
;   1. **AppId 固定**：升级靠它认出"这是同一个软件"。改了就变成两份并存，
;      用户的快捷方式还指着旧的那份。永远不要动这个 GUID。
;   2. **PrivilegesRequired=lowest**：装到 {localappdata}\Programs\eggpaper，
;      全程不弹 UAC。用户想换盘（D:\eggpaper）在目录页改就是——那一页是开着的。
;   3. **用户数据不在 [Files] 里，也不在 [UninstallDelete] 里**：config.yaml、
;      文库 PDF、SQLite 全在数据目录（新装默认安装目录旁 data\，不可写则回落
;      %LOCALAPPDATA%\eggpaper\data，老安装原地继续；见 backend/appinfo.py）。
;      所以覆盖安装不会丢东西，卸载也不会——卸载只在用户手动勾选时才问数据的事。
;   4. **升级前先关掉正在跑的旧版本**（CloseApplications + 兜底 taskkill）：
;      我们的 exe 占着自己的文件，不关掉就替换不了。
;   5. **两个安装器不许同时跑**（[Code] 里的原子互斥体，不是 Inno 的 SetupMutex）：
;      用户双击两下安装包会开出两个安装窗口，两个实例在同一个目录上互相删/盖对方
;      刚写进去的文件，装完就是缺运行库的半成品——真事故，见 [Code] 第一段。
;   6. **升级给 _internal"让路"（改名）而不是先删**，装完逐个校验关键文件，缺件
;      就把旧版本换回来（见 [Code] 第二段）：中断/被杀软拦住也留得下能用的旧版本。

#define MyAppName "eggpaper"
#ifndef MyVersion
  #define MyVersion "0.1.3"   ; 兜底：正式构建由 build_installer.py /D 传入
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

; 这里原来有一条 [InstallDelete] filesandordirs "{app}\_internal"（升级前清空整个 _internal）。
; 化石文件（旧版本里改了名就永远留在盘上的那些：实测攒过 154 个 / 21 MB）确实要靠整目录换掉
; 才不会累积，但"删干净、还没装上"这个窗口被留给了外界：装到一半取消、被杀软拦住、断电、
; 或者撞上第二个安装实例，用户手里就是一个缺运行库的程序目录。现在改由 [Code] 里的
; "让路"（_internal → _internal.old）完成同样的事：新目录是全新拷贝，旧文件随 .old 一起消失。

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
; 让路式升级留下的备份目录：正常升级装完就删了，只有"换回去也没成功"的那种坏结局会留到卸载
Type: filesandordirs; Name: "{app}\_internal.old"

[Code]
// ==================== 一、两个安装器不许同时跑 ====================
// 真事故（2026-10-14 用户反馈）：双击了两下安装包，开出两个安装窗口。两个实例在同一个
// 目录上各自"清空 _internal 再装新的"，互相删掉/覆盖对方刚写进去的文件，装完 _internal
// 里缺运行库，启动就报：
//     Failed to load Python DLL '…\_internal\python311.dll'.   LoadLibrary:找不到指定的模块。
// 这里用**原子的"创建并看是否已存在"**（CreateMutexW + 紧跟 GetLastError），不用 Inno 的
// SetupMutex——官方文档写的是"检查有没有，没有才创建"，两次点击只差几百毫秒时可能双双通过；
// 也不加 Global\ 前缀：PrivilegesRequired=lowest 的安装没有 SeCreateGlobalPrivilege，
// 建不出全局对象，等于没锁。
// 静默升级（软件里点"立即重启并安装"）撞上手动安装时：本次静默实例直接退出、日志留痕，
// 另一份照常把同一个包装完，用户从快捷方式重开即可。
const
  CM_ERROR_ALREADY_EXISTS = 183;

function CreateMutexW(lpMutexAttributes: LongWord; bInitialOwner: LongWord;
  lpName: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function GetLastError: LongWord;
  external 'GetLastError@kernel32.dll stdcall';

var
  // 故意不 CloseHandle：进程活着就一直持有这把锁，进程退出时系统自动释放
  SetupLock: THandle;
  // _internal 已经让路成 _internal.old、而这次安装还没被确认成功。只在这段时间里为 True；
  // 退出路径上（取消、失败）要据此把旧版本换回来
  NeedRestore: Boolean;

function InitializeSetup(): Boolean;
var err: LongWord;
begin
  Result := True;
  SetupLock := CreateMutexW(0, 0, 'eggpaper-setup-7C4A1E02-9B3D-4F61-8AE5-2D7B6C1F0A93');
  err := GetLastError;      // 紧跟创建的下一句取走，中间不能插别的调用
  if (SetupLock <> 0) and (err = CM_ERROR_ALREADY_EXISTS) then
  begin
    Log('检测到另一个安装实例正在运行，本次退出（避免两个实例同时改同一个目录）');
    if not WizardSilent then
      MsgBox('已经有一个 eggpaper 安装窗口在运行了。' + #13#10 + #13#10 +
             '请等它装完（或关掉它）再重新运行安装包——双击一次就够，不用点第二下。' + #13#10 + #13#10 +
             '两个安装窗口同时装会把程序文件装坏，之后启动时会报"找不到 python311.dll"。',
             mbInformation, MB_OK);
    Result := False;
  end;
end;

// ==================== 二、升级"让路"而不是"先删"，装完校验、缺件就换回来 ====================
// 老写法是 [InstallDelete] 把 {app}\_internal 整个删掉再装新的。整目录换掉确实解决了化石
// 文件问题（旧版本里"改了名就永远留在盘上"的那些），但把"删干净、还没装上"这个窗口留给了
// 外界：装到一半取消、被杀软拦住、断电、或者撞上第二个安装实例，用户手里就是一个缺运行库
// 的程序目录，而报错里没有任何能自己修好的线索。
// 现在：先把 _internal 改名为 _internal.old（同盘改名，秒级，不动内容），新文件装进全新的
// _internal，装完逐个校验关键文件；齐了就删掉 .old，缺件就把它换回来并告诉用户去查安全软件。
// 任何一步断掉，这次退出或下一次安装时都能把旧版本换回来——老版本至少还能用。
// 代价说清楚：.old 与新副本同时占盘，峰值多一份 _internal（约 280MB）。盘刚好只够一份的
// 机器会在写入中途报空间不足——这次的失败是可恢复的（退出时换回 .old），腾出空间重装即可；
// 想省这份空间就得先删 .old 再装，那等于把回滚能力丢掉，不值。

function InternalPath(const Suffix: String): String;
begin
  Result := ExpandConstant('{app}\_internal') + Suffix;
end;

function WildcardCount(const Pattern: String): Integer;
var Rec: TFindRec;
begin
  Result := 0;
  if FindFirst(Pattern, Rec) then
  begin
    repeat
      if (Rec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then Result := Result + 1;
    until not FindNext(Rec);
    FindClose(Rec);
  end;
end;

// 关键运行件齐不齐：空串 = 齐了；非空 = 缺的那样东西（人话，直接报给用户看）。
// 挑这几个是因为"缺了必然报 Failed to load Python DLL"：python311.dll 自己（连 python3.dll
// 一起数够两个，将来换 Python 版本这句检查才不失效）、它的依赖 vcruntime140.dll（缺依赖时
// Windows 也报"找不到指定的模块"）、PyInstaller 的运行时 base_library.zip，外加界面本体。
function MissingRuntime(const Dir: String): String;
begin
  Result := '';
  if not DirExists(Dir) then
    Result := '程序目录 _internal 本身'
  else if not FileExists(Dir + '\base_library.zip') then
    Result := 'Python 运行时 base_library.zip'
  else if WildcardCount(Dir + '\python3*.dll') < 2 then
    Result := 'Python 本体 python311.dll'
  else if not FileExists(Dir + '\vcruntime140.dll') then
    Result := 'VC++ 运行库 vcruntime140.dll'
  else if not FileExists(Dir + '\frontend\dist\index.html') then
    Result := '界面文件 frontend\dist\index.html';
end;

// 按映像名清一次正在跑的旧版本。CloseApplications 走 Restart Manager，但它认的是"谁占着
// 要替换的文件"——软件是被批处理/任务计划/双击 PDF 拉起的时候它认不出来。
procedure KillApp();
var ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#MyExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  KillApp();      // 早点清：RM 的"文件被占用"提示就不会弹到用户脸上
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Cur, Old, Miss, Tip: String;
begin
  if CurStep = ssInstall then
  begin
    KillApp();    // 紧接改名之前再清一次，确保没人攥着 _internal 里的文件
    Cur := InternalPath('');
    Old := InternalPath('.old');
    // 上一次安装断在半路留下的 .old：现在这份是好的，它就是垃圾；现在这份缺件而它齐全，
    // 说明上次换过去的新版本没装成，先换回来（用户手里马上恢复成能用的旧版本），
    // 再照常走这次的让路流程。
    if DirExists(Old) then
    begin
      if MissingRuntime(Cur) = '' then
        DelTree(Old, True, True, True)
      else if MissingRuntime(Old) = '' then
      begin
        DelTree(Cur, True, True, True);
        if RenameFile(Old, Cur) then
          Log('上次中断留下的 _internal.old 已换回 _internal');
      end
      else
        DelTree(Old, True, True, True);   // 两份都不齐：.old 没有价值，清掉好让位
    end;
    if DirExists(Cur) then
    begin
      if not RenameFile(Cur, Old) then
      begin
        // 改名失败 = 有进程还攥着里面的文件（或没写权限）。宁可什么都不做，
        // 也不要留下一半新一半旧的目录
        // （SuppressibleMsgBox 而不是 MsgBox：静默升级里也要能自动略过，不然会卡在弹窗上）
        SuppressibleMsgBox('无法替换程序目录：' + Cur + ' 里的文件正被占用，或者没有写权限。' + #13#10 + #13#10 +
               '请确认 eggpaper 已经完全退出（任务管理器里结束 eggpaper.exe；托盘里还有的话一起退掉），' +
               '然后重新运行安装包。', mbError, MB_OK, IDOK);
        Abort;    // ssInstall 里 Abort 是致命的：Setup 到此终止并回滚（此时还没动过任何文件）
      end;
      NeedRestore := True;
      Log('_internal 已让路为 _internal.old，本次装进全新的 _internal');
    end;
  end
  else if CurStep = ssPostInstall then
  begin
    Cur := InternalPath('');
    Old := InternalPath('.old');
    Miss := MissingRuntime(Cur);
    if Miss = '' then
    begin
      if DirExists(Old) then DelTree(Old, True, True, True);
      NeedRestore := False;
      Log('程序文件校验通过：_internal 完整');
    end
    else
    begin
      // 装完了却缺件：现实里几乎只有一种可能——安全软件把刚写进去的文件删了/隔离了
      // （未签名的安装包往非系统盘写 DLL，是它们爱干的事）
      Log('校验失败：缺少 ' + Miss + '；清掉半成品，把 _internal.old 换回来');
      DelTree(Cur, True, True, True);
      Tip := '这次装进去的程序文件不完整（缺少 ' + Miss + '），多半是被安全软件（360、电脑管家、' +
             '火绒、Defender 等）当成可疑文件删掉了。';
      if DirExists(Old) and RenameFile(Old, Cur) then
      begin
        NeedRestore := False;
        Tip := Tip + #13#10 + #13#10 +
               '已经自动把上一个能用的版本恢复回来了，eggpaper 可以照常用。想升级：先把 ' +
               ExpandConstant('{app}') + ' 加入安全软件的信任名单（或临时关掉它），再运行一次安装包。';
      end
      else
        Tip := Tip + #13#10 + #13#10 +
               '请把 ' + ExpandConstant('{app}') + ' 加入安全软件的信任名单（或临时关掉它），再运行一次安装包。';
      SuppressibleMsgBox(Tip, mbError, MB_OK, IDOK);
    end;
  end;
end;

// Setup 的每一条退出路径都会走到这里（装完、取消、出错）。只要 .old 还留着而这次安装没被
// 确认成功，就尽量把旧版本换回来：用户手里留一个能用的旧版本，比一个缺文件的目录强。
procedure DeinitializeSetup();
var Cur, Old: String;
begin
  if not NeedRestore then exit;
  Cur := InternalPath('');
  Old := InternalPath('.old');
  if not DirExists(Old) then exit;
  if MissingRuntime(Cur) <> '' then
    DelTree(Cur, True, True, True);
  if not DirExists(Cur) then
  begin
    if RenameFile(Old, Cur) then Log('安装中断：已把 _internal.old 换回 _internal')
    else Log('安装中断：_internal.old 换不回来，程序目录可能不完整（重装一次即可）');
  end;
end;

// 这里**不要**去动 WizardForm.DirEdit：Inno 会先把「上次装在哪」填进目录页，
// 静默升级（/SILENT）也靠这个记忆回到原目录。手写一行 SetText 就等于：
//   ① 吃掉 /DIR= 参数（本次实测：装到了默认目录，而不是指定目录）
//   ② 升级时把老目录忘掉，装出第二份，正在跑的那份永远升不上去
