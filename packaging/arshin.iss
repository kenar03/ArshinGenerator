#ifndef PortablePath
  #error PortablePath must point to the PyInstaller distribution
#endif
#ifndef OutputPath
  #error OutputPath must point to the release directory
#endif
#ifndef AppVersion
  #define AppVersion "2.2.0"
#endif

[Setup]
AppId={{6462E024-6818-4B52-AA51-9ED6CCB87EC2}
AppName=СИ-Экспорт
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Установщик программы «СИ-Экспорт»
VersionInfoProductName=СИ-Экспорт
DefaultDirName={localappdata}\Programs\ArshinGenerator
DefaultGroupName=СИ-Экспорт
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayName=СИ-Экспорт
UninstallDisplayIcon={app}\SI-Export.exe
SetupIconFile=..\arshin_generator\assets\app.ico
OutputDir={#OutputPath}
OutputBaseFilename=SI-Export-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Files]
Source: "{#PortablePath}\*"; DestDir: "{app}"; Excludes: "portable.flag,data"; Flags: ignoreversion recursesubdirs createallsubdirs
; Keep existing custom shortcuts and taskbar pins working after an in-place upgrade.
Source: "{#PortablePath}\SI-Export.exe"; DestDir: "{app}"; DestName: "ArshinGenerator.exe"; Flags: ignoreversion; Check: KeepLegacyLauncher

[InstallDelete]
Type: files; Name: "{autoprograms}\Аршин · Генератор XML.lnk"; Check: KeepLegacyLauncher
Type: files; Name: "{autodesktop}\Аршин · Генератор XML.lnk"; Check: KeepLegacyLauncher

[Icons]
Name: "{autoprograms}\СИ-Экспорт"; Filename: "{app}\SI-Export.exe"; WorkingDir: "{app}"; IconFilename: "{app}\SI-Export.exe"; AppUserModelID: "ArshinGenerator.Desktop"
Name: "{autodesktop}\СИ-Экспорт"; Filename: "{app}\SI-Export.exe"; WorkingDir: "{app}"; IconFilename: "{app}\SI-Export.exe"; AppUserModelID: "ArshinGenerator.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\SI-Export.exe"; Description: "Запустить СИ-Экспорт"; Flags: nowait postinstall skipifsilent

[Code]
function KeepLegacyLauncher: Boolean;
var
  PreviousPath, InstallPath: String;
begin
  InstallPath := ExpandConstant('{app}');
  Result := FileExists(InstallPath + '\ArshinGenerator.exe') and
    RegQueryStringValue(HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6462E024-6818-4B52-AA51-9ED6CCB87EC2}_is1',
      'Inno Setup: App Path', PreviousPath) and
    (CompareText(RemoveBackslashUnlessRoot(PreviousPath), RemoveBackslashUnlessRoot(InstallPath)) = 0);
end;
