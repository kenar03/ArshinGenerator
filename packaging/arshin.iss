#ifndef PortablePath
  #error PortablePath must point to the PyInstaller distribution
#endif
#ifndef OutputPath
  #error OutputPath must point to the release directory
#endif
#ifndef AppVersion
  #define AppVersion "1.3.1"
#endif

[Setup]
AppId={{6462E024-6818-4B52-AA51-9ED6CCB87EC2}
AppName=Аршин · Генератор XML
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Установка приложения «Аршин · Генератор XML»
DefaultDirName={localappdata}\Programs\ArshinGenerator
DefaultGroupName=Аршин · Генератор XML
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayIcon={app}\ArshinGenerator.exe
SetupIconFile=..\arshin_generator\assets\app.ico
OutputDir={#OutputPath}
OutputBaseFilename=ArshinGenerator-Setup-x64
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

[Icons]
Name: "{autoprograms}\Аршин · Генератор XML"; Filename: "{app}\ArshinGenerator.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Аршин · Генератор XML"; Filename: "{app}\ArshinGenerator.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ArshinGenerator.exe"; Description: "Запустить приложение"; Flags: nowait postinstall skipifsilent
