; ============================================================================
; AI 3D Forge -- Windows installer script (Inno Setup 6)
; ----------------------------------------------------------------------------
; Build : scripts\build_installer.ps1
; Output: installer\Output\AI-3D-Forge-Setup.exe
;
; Requirement mapping:
;   Sec.37 Installer : install app / create program dir / data dir / user config
;                      dir / desktop shortcut / start-menu shortcut / detect
;                      OpenSCAD / first-run initialization
;   Sec.42 Supports install-path selection, start menu, desktop shortcut,
;          uninstall, and upgrade-in-place
;   Sec.45 User data never goes to Program Files. The installer never touches
;          anything under the user profile, so upgrading or uninstalling loses
;          no API key, projects, .scad or .stl files.
;   "Use less of C:"  On a fresh install, if drive D: exists we default to
;          D:\Program Files\AI 3D Forge.
;
; NOTE: This file is deliberately ASCII-only.
;   CustomMessages are looked up by the active language: Chinese uses the
;   official ChineseSimplified.isl translations for all built-in text, and the
;   custom messages below have both a "chinese." and an "english." variant.
;   Inno Setup .iss encoding quirks are thus avoided entirely.
; ============================================================================

#define AppName "AI 3D Forge"
#define AppVersion "1.0.0"
#define AppPublisher "AI 3D Forge"
#define AppExeName "AI-3D-Forge.exe"
#define SourceDir "..\build\dist\AI-3D-Forge"
#define IconFile "..\build\AI-3D-Forge.ico"

; Inno Setup ships a ChineseSimplified.isl with the standard install, but the
; winget package (and some trimmed installs) omit the unofficial translations.
; Detect it and enable the Chinese language only when it is really available,
; so the build never fails just because a language file is missing.
#define ChineseIsl "compiler:Languages\ChineseSimplified.isl"
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
  #define HaveChinese
#endif

[Setup]
AppId={{8F3A72C1-5D64-4E0B-9A17-2C6E4B8D1F30}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={code:GetDefaultDir}
DefaultGroupName={#AppName}
AllowNoIcons=yes
LicenseFile=..\docs\LICENSE.txt
OutputDir=Output
OutputBaseFilename=AI-3D-Forge-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Brand the installer itself: the wizard window, the taskbar during setup, and
; the icon shown in Add/Remove Programs all use the app icon.
SetupIconFile={#IconFile}
UninstallDisplayIcon={#IconFile}
ShowLanguageDialog=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 or newer
MinVersion=10.0.17763
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayName={#AppName}
DisableProgramGroupPage=no
DisableDirPage=no
DisableReadyPage=no
; Show what is happening during the (potentially slow) first-run preparation
; instead of leaving the user staring at a frozen progress bar.
DisableFinishedPage=no
; Upgrade in place: overwrite program files, keep every user file untouched
UsePreviousAppDir=yes
UsePreviousGroup=yes
; UsePreviousTasks=no on purpose: with it enabled, a task the user once
; unchecked (or that a silent install never selected) stays unselected forever,
; so the desktop shortcut would silently never come back after an upgrade.
; The desktop shortcut is expected, so always offer it as checked.
UsePreviousTasks=no
; Close a running instance before upgrading so no file is locked.
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
SetupLogging=yes

[Languages]
; "Name" must be an ASCII identifier, so the language entry stays "english" and
; its custom messages below are keyed "english." -- but every string the user
; reads is Simplified Chinese. LanguageID/LanguageCodePage tell Windows this is
; Simplified Chinese (0x0804 / cp936) so text rendering and locale behaviour are
; correct, and [Messages] sets the human-readable language name.
;
; Why not a real Chinese language file?
;   Inno Setup ships Languages\ChineseSimplified.isl with the standard install,
;   but the official winget package omits the unofficial translations, and the
;   upstream translation repos live on raw.githubusercontent.com which is
;   unreachable from some networks (measured on the build machine).
;   Rather than fail the build or ship an English-only wizard, we use the
;   built-in English message file and override every string the user actually
;   sees. A few rarely shown built-in dialogs stay English. Drop a
;   ChineseSimplified.isl into the compiler's Languages folder and re-point
;   MessagesFile at it for a 100% translated wizard.
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; LanguageName is defined inside the .isl file, so it is overridden here rather
; than in [Languages]. LanguageID / LanguageCodePage also live in the .isl and
; cannot be set from here; since ShowLanguageDialog=no and only one language is
; compiled in, they have no user-visible effect.
english.LanguageName=简体中文
english.SetupAppTitle=安装 AI 3D Forge
english.SetupWindowTitle=安装 - AI 3D Forge
english.WelcomeLabel1=欢迎安装 AI 3D Forge
english.WelcomeLabel2=安装程序将把 AI 3D Forge（AI 3D 打印设计工具）安装到你的电脑上。%n%n继续之前，建议先关闭其他正在运行的程序。%n%n点击「下一步」继续。
english.SelectDirLabel3=安装程序将把 AI 3D Forge 安装到下面的文件夹中。%n%n如果要安装到其他位置，请点击「浏览」。
english.SelectDirBrowseLabel=点击「下一步」继续。如果要更换安装位置，请点击「浏览」。
english.SelectTasksLabel2=请选择安装程序要执行的附加任务，然后点击「下一步」。
english.ReadyLabel1=安装程序已准备好，可以开始安装。
english.ReadyLabel2=点击「安装」开始安装。%n%n如果你需要修改设置，请点击「上一步」。
english.ReadyLabel3=点击「安装」开始安装。
english.InstallingLabel=正在安装 AI 3D Forge，请稍候…
english.FinishedLabel=AI 3D Forge 已安装完成。%n%n点击「完成」关闭安装程序。
english.FinishedHeadingLabel=安装完成
english.ClickNext=点击「下一步」继续。
english.ButtonNext=下一步(&N) > 
english.ButtonBack=< 上一步(&B)
english.ButtonInstall=安装(&I)
english.ButtonCancel=取消
english.ButtonFinish=完成(&F)
english.ButtonBrowse=浏览(&R)…
english.ExitSetupTitle=退出安装
english.ExitSetupMessage=安装尚未完成。如果现在退出，AI 3D Forge 将不会被安装。%n%n确定要退出安装程序吗？
english.UninstallAppFullTitle=卸载 AI 3D Forge
english.ConfirmUninstall=确定要完全卸载 %1 及其所有组件吗？
english.UninstallStatusLabel=正在从你的电脑上卸载 AI 3D Forge，请稍候…
english.UninstalledAll=AI 3D Forge 已从你的电脑上卸载完成。
english.AdditionalIcons=附加任务：
english.CreateQuickLaunchIcon=创建快速启动栏快捷方式(&Q)
english.ProgramOnTheWeb=访问 AI 3D Forge 网站

[CustomMessages]
english.CreateDesktopIcon=创建桌面快捷方式(&D)
english.LaunchApp=安装完成后启动 AI 3D Forge
english.OpenScadMissingTitle=未检测到 OpenSCAD（3D 建模引擎）
english.OpenScadMissingText=本软件需要一个免费的 3D 建模引擎 OpenSCAD，用来把设计稿转换成可打印的 3D 模型文件。%n%n安装完成后，程序会自动帮你下载并安装它，你不需要做任何事。%n%n如果自动安装没有成功，也可以在程序里点击「自动安装 OpenSCAD」，或者访问 openscad.org 免费下载。
english.OpenScadFoundTitle=已检测到 OpenSCAD
english.OpenScadFoundText=很好，你的电脑上已经安装了 OpenSCAD，安装完成后可以直接开始使用。
english.InstallInfoTitle=你的文件保存在哪里
english.InstallInfoText=程序文件会安装到你选择的目录。%n%n你的 API Key、项目、SCAD 与 STL 文件都保存在 Windows 用户目录中：%n    %%APPDATA%%\AI-3D-Forge%n    文档\AI-3D-Forge\Projects%n%n升级或卸载本软件都不会删除这些文件。
english.PreparingTitle=正在准备 3D 建模引擎
english.PreparingText=本软件需要一个免费的 3D 建模引擎 OpenSCAD，才能把设计稿转换成可打印的模型文件。%n%n安装程序现在会自动帮你下载并装好它（约 21MB，视网速约 1～3 分钟）。这样做的好处是：你第一次打开软件就能直接生成模型，不用再等一次下载。这一步只做一次，以后升级软件也不会重复。%n%n如果这一步没有成功，完全不影响软件安装 —— 首次打开软件时会再引导你一次，或者按提示到 openscad.org 免费下载。%n%n点击「安装」后请不要关闭窗口。
english.PreparingMsg=正在下载并安装 3D 建模引擎 OpenSCAD（约 21MB，视网速 1～3 分钟，请勿关闭窗口）…
english.FirstRunFailedTitle=3D 建模引擎未能自动安装
english.FirstRunFailedText=这不影响软件安装，软件现在就可以使用。%n%n首次打开软件时会再次引导你安装 OpenSCAD，也可以访问 openscad.org 免费下载后，在软件的「系统诊断」里点一次「重新检测」。%n%n详细原因见安装目录下的 logs\first-run.log。
english.UninstallKeptDataTitle=你的文件已保留
english.UninstallKeptDataText=AI 3D Forge 的程序文件已经删除。%n%n你的 API Key、项目、SCAD 与 STL 文件仍然保存在：%n    %%APPDATA%%\AI-3D-Forge%n    文档\AI-3D-Forge\Projects%n%n如果以后重新安装，项目会自动回来。
english.UninstallRemoveDataAsk=是否同时删除上面这些文件？%n%n如果以后还可能再用这个软件，建议选「否」保留项目。
english.UninstallDataRemoved=已删除以下文件：
english.UninstallDataNotFound=没有找到需要删除的文件（可能此前已手动删除）。

[Tasks]
; NOTE: do NOT add the "checkedonce" flag here.
;   With checkedonce, Inno Setup never selects the task in silent/unattended
;   installs, so /VERYSILENT produced no desktop shortcut at all (confirmed in
;   the install log: only 2 icon entries were created, both start-menu).
;   Users expect a desktop icon after a normal double-click install, and
;   requirement 37 asks for it explicitly.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The application: exe + bundled Python runtime + bundled web UI + docs
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; User guide sits next to the executable so it can be opened by double-clicking
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
Source: "..\README-用户.md"; DestDir: "{app}"; DestName: "README-用户.md"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\README-开发者.md"; DestDir: "{app}"; DestName: "README-开发者.md"; Flags: ignoreversion skipifsourcedoesntexist
; License texts must ship with the product: the app downloads and runs the
; OpenSCAD official installer, and OpenSCAD is GPLv2 -- so the GPLv2 full text
; plus the source-code pointer has to be included, not just referenced.
Source: "..\docs\LICENSE.txt"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\docs\LICENSE-OpenSCAD-GPLv2.txt"; DestDir: "{app}"; DestName: "LICENSE-OpenSCAD-GPLv2.txt"; Flags: ignoreversion
Source: "..\docs\OPENSCAD-SOURCES.md"; DestDir: "{app}"; DestName: "OPENSCAD-SOURCES.md"; Flags: ignoreversion
; Troubleshooting helper: if the app ever opens in the browser instead of its own
; window, this writes a diagnosis report to the Desktop that the user can send.
Source: "..\scripts\diagnose_native_window.py"; DestDir: "{app}"; DestName: "诊断原生窗口.py"; Flags: ignoreversion

[Dirs]
Name: "{app}\logs"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\README-用户.md"; Comment: "阅读使用说明"
Name: "{group}\许可与第三方组件"; Filename: "{app}\LICENSE.txt"; Comment: "许可与第三方组件说明"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: quicklaunchicon

[Run]
; First-run preparation, run VISIBLY and WITHOUT a shell.
;
; It creates the user folders, then downloads and installs OpenSCAD (about
; 21MB) so the app works the moment the user opens it -- no second wait inside
; the welcome wizard. Because that can take a minute or two, the step must not
; be runhidden: the progress page shows StatusMsg.
;
; The app writes its own log via --log-file instead of using shell redirection.
; Do NOT "improve" this back into
;     Filename: "{cmd}"; Parameters: "/C ... >> ""..."" 2>&1"
; -- that was tried and failed: cmd reports
; "The filename, directory name, or volume label syntax is incorrect" and exits
; 1 without creating the log at all (cmd's quote handling around a quoted exe
; path plus redirection is not dependable). Calling the exe directly is exact.
Filename: "{app}\{#AppExeName}"; Parameters: "--first-run-setup --log-file ""{app}\logs\first-run.log"""; \
    Flags: waituntilterminated runminimized; \
    StatusMsg: "{cm:PreparingMsg}"
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Close a running instance first so no file is locked during uninstall
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExeName} /F >nul 2>&1"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; Remove only what the installer itself owns. User data under APPDATA and
; Documents is never touched -- the uninstaller says so explicitly on the
; finish page so nobody fears losing their models.
Type: filesandordirs; Name: "{app}\_server"
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\logs"
Type: files; Name: "{app}\*.log"

[Code]
var
  OpenScadPage: TOutputMsgWizardPage;
  InstallInfoPage: TOutputMsgWizardPage;
  PreparingPage: TOutputMsgWizardPage;

function DriveExists(Drive: String): Boolean;
begin
  Result := DirExists(Drive + '\');
end;

{ "Use less of C:": prefer drive D: when it exists, then E:, else the Windows default. }
function GetDefaultDir(Param: String): String;
begin
  if DriveExists('D:') then
    Result := 'D:\Program Files\AI 3D Forge'
  else if DriveExists('E:') then
    Result := 'E:\Program Files\AI 3D Forge'
  else
    Result := ExpandConstant('{autopf}\AI 3D Forge');
end;

{ Look for openscad.exe in the common locations. Mirrors the runtime search in
  src\aiforge\openscad.py -- we never assume C:\Program Files\OpenSCAD only. }
function FindOpenScad(): String;
var
  Candidates: TArrayOfString;
  I: Integer;
  Found: String;
begin
  SetArrayLength(Candidates, 12);
  Candidates[0]  := ExpandConstant('{pf}\OpenSCAD\openscad.exe');
  Candidates[1]  := ExpandConstant('{pf32}\OpenSCAD\openscad.exe');
  Candidates[2]  := ExpandConstant('{localappdata}\Programs\OpenSCAD\openscad.exe');
  Candidates[3]  := ExpandConstant('{localappdata}\AI-3D-Forge\runtime\openscad\openscad.exe');
  Candidates[4]  := 'D:\Program Files\OpenSCAD\openscad.exe';
  Candidates[5]  := 'D:\OpenSCAD\openscad.exe';
  Candidates[6]  := 'E:\Program Files\OpenSCAD\openscad.exe';
  Candidates[7]  := 'E:\OpenSCAD\openscad.exe';
  Candidates[8]  := ExpandConstant('{sd}\OpenSCAD\openscad.exe');
  Candidates[9]  := ExpandConstant('{localappdata}\Programs\OpenSCAD\openscad-2021.01\openscad.exe');
  Candidates[10] := ExpandConstant('{localappdata}\AI-3D-Forge\runtime\openscad\openscad-2021.01\openscad.exe');
  Candidates[11] := ExpandConstant('{pf}\OpenSCAD\openscad.com');
  Found := '';
  for I := 0 to GetArrayLength(Candidates) - 1 do
  begin
    if (Found = '') and FileExists(Candidates[I]) then
      Found := Candidates[I];
  end;
  Result := Found;
end;

procedure InitializeWizard();
var
  OpenScadPath: String;
begin
  InstallInfoPage := CreateOutputMsgPage(
    wpSelectDir,
    ExpandConstant('{cm:InstallInfoTitle}'),
    ExpandConstant('{cm:InstallInfoTitle}'),
    ExpandConstant('{cm:InstallInfoText}')
  );

  OpenScadPath := FindOpenScad();
  if OpenScadPath = '' then
  begin
    OpenScadPage := CreateOutputMsgPage(
      wpSelectTasks,
      ExpandConstant('{cm:OpenScadMissingTitle}'),
      ExpandConstant('{cm:OpenScadMissingTitle}'),
      ExpandConstant('{cm:OpenScadMissingText}') + #13#10#13#10 + ExpandConstant('{cm:PreparingText}')
    );
  end
  else
  begin
    OpenScadPage := CreateOutputMsgPage(
      wpSelectTasks,
      ExpandConstant('{cm:OpenScadFoundTitle}'),
      ExpandConstant('{cm:OpenScadFoundTitle}'),
      ExpandConstant('{cm:OpenScadFoundText}') + #13#10#13#10 + OpenScadPath
    );
  end;

  { Explain the post-install preparation before it starts, so the user is not
    surprised by a step that takes a minute or two. }
  PreparingPage := CreateOutputMsgPage(
    wpSelectTasks,
    ExpandConstant('{cm:PreparingTitle}'),
    ExpandConstant('{cm:PreparingTitle}'),
    ExpandConstant('{cm:PreparingText}')
  );
end;

{ Tell the user their data survives uninstall, and offer to remove it explicitly.
  Losing models is the single biggest fear when uninstalling, and silently keeping
  the data looks like the uninstaller failed to clean up.

  GUARD REQUIRED: never show a modal in a silent uninstall. /SUPPRESSMSGBOXES is
  documented not to suppress MsgBox calls made from [Code], so an unguarded
  MsgBox makes `unins000.exe /VERYSILENT` hang forever on an invisible dialog
  waiting for a click nobody can give. That breaks unattended uninstall and any
  automated deployment -- verified the hard way.
  In silent mode we therefore keep the user data (the safe default). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Choice: Integer;
  Removed: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  if UninstallSilent then
  begin
    Log('Silent uninstall: keeping user data in ' + ExpandConstant('{userappdata}') + '\AI-3D-Forge');
    Exit;
  end;

  Removed := '';
  Choice := MsgBox(
    ExpandConstant('{cm:UninstallKeptDataText}') + #13#10#13#10 + ExpandConstant('{cm:UninstallRemoveDataAsk}'),
    mbConfirmation, MB_YESNO);
  if Choice = IDYES then
  begin
    if DelTree(ExpandConstant('{userappdata}\AI-3D-Forge'), True, True, True) then
      Removed := Removed + ExpandConstant('{userappdata}\AI-3D-Forge') + #13#10;
    if DelTree(ExpandConstant('{userdocs}\AI-3D-Forge'), True, True, True) then
      Removed := Removed + ExpandConstant('{userdocs}\AI-3D-Forge') + #13#10;
    if Removed <> '' then
      MsgBox(ExpandConstant('{cm:UninstallDataRemoved}') + #13#10#13#10 + Removed, mbInformation, MB_OK)
    else
      MsgBox(ExpandConstant('{cm:UninstallDataNotFound}'), mbInformation, MB_OK);
  end;
end;
