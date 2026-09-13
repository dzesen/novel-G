; Complete per-user Windows package. Persistent data is never an uninstall target.
#ifndef StageDir
  #error StageDir must point to a verified desktop staging directory
#endif
#ifndef OutputPath
  #define OutputPath "..\reports\desktop-installer"
#endif
[Setup]
AppId={{3985130B-3D70-4184-AF92-3B633FCA6D39}
AppName=Novel-G Desktop Preview
AppVersion=0.1.0-preview.2
AppPublisher=Novel-G
DefaultDirName={localappdata}\Programs\Novel-G
DefaultGroupName=Novel-G
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
OutputDir={#OutputPath}
OutputBaseFilename=Novel-G-Desktop-0.1.0-preview.2-win-x64-setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\NovelG.exe
CloseApplications=yes
RestartApplications=no
LicenseFile=licenses\installer-license.txt
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimplified.DesktopShortcut=创建桌面快捷方式
english.DesktopShortcut=Create a desktop shortcut
chinesesimplified.OpenNovelG=打开 Novel-G
english.OpenNovelG=Open Novel-G
chinesesimplified.VCRequired=需要 Microsoft Visual C++ x64 运行库 14.51.36247 或更新版本。请从微软官方下载并安装，然后重新运行 Novel-G 安装包。是否打开微软下载说明？
english.VCRequired=Microsoft Visual C++ x64 Runtime 14.51.36247 or newer is required. Install it from Microsoft, then run this installer again. Open the Microsoft download page?

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; Flags: unchecked

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Novel-G"; Filename: "{app}\NovelG.exe"
Name: "{autodesktop}\Novel-G"; Filename: "{app}\NovelG.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\NovelG.exe"; Description: "{cm:OpenNovelG}"; Flags: nowait postinstall skipifsilent

[Code]
function HasVisualCppRuntime: Boolean;
var Installed, Major, Minor, Build: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed)
    and (Installed = 1)
    and RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major)
    and RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor)
    and RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Bld', Build);
  if Result then
    Result := (Major > 14) or ((Major = 14) and ((Minor > 51) or ((Minor = 51) and (Build >= 36247))));
end;

function InitializeSetup: Boolean;
var ErrorCode: Integer;
begin
  Result := HasVisualCppRuntime;
  if not Result then begin
    Log('Microsoft Visual C++ x64 Runtime 14.51.36247 or newer is required.');
    if not WizardSilent then
      if MsgBox(CustomMessage('VCRequired'), mbInformation, MB_YESNO) = IDYES then
        ShellExec('open', 'https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    if not Exec(ExpandConstant('{sys}\icacls.exe'), '"' + ExpandConstant('{app}\webview2') + '" /grant *S-1-15-2-1:(OI)(CI)(RX) *S-1-15-2-2:(OI)(CI)(RX) /T /Q', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('Unable to configure the desktop rendering runtime. Please repair the installation.');
    if ResultCode <> 0 then
      RaiseException('Unable to configure the desktop rendering runtime. Please repair the installation.');
  end;
end;
