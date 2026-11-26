param(
    [switch] $TestInstaller,
    [switch] $CheckConnection,
    [switch] $TestPdf,
    [string] $UpgradeFrom = '',
    [string] $Python = 'python'
)
$ErrorActionPreference = 'Stop'
if ($UpgradeFrom -and -not $TestInstaller) { throw 'Set -TestInstaller with -UpgradeFrom.' }
$root = Split-Path $PSScriptRoot
$release = Join-Path $root 'release'
$qa = Join-Path $root ('build\qa-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Force -Path $qa | Out-Null
$sample = (Get-Item -LiteralPath (Join-Path $root 'Файлы для задания\П-Т1-з2-1260-26.xlsm')).FullName
function Get-WorkbookHash {
    $stream = [IO.File]::Open($sample, [IO.FileMode]::Open, [IO.FileAccess]::Read,
        ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try { (Get-FileHash -InputStream $stream).Hash }
    finally { $stream.Dispose() }
}
$baseHash = Get-WorkbookHash

function Test-Guide($folder) {
    $guide = Join-Path $folder 'Инструкция.txt'
    if (Test-Path -LiteralPath (Join-Path $folder 'Инструкция.md')) { throw 'The old Markdown guide is still present.' }
    $bytes = [IO.File]::ReadAllBytes($guide)
    $expected = (Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $root 'docs/quick_start.txt')) -replace '\r?\n', "`r`n"
    if ($bytes.Length -lt 3 -or [BitConverter]::ToString($bytes, 0, 3) -ne 'EF-BB-BF' -or
        [IO.File]::ReadAllText($guide) -cne $expected) {
        throw 'The packaged guide is not the expected UTF-8 text file.'
    }
}

function Test-Executable($exe, $report, [switch] $ImportSample, [switch] $Legacy, [switch] $ExportPdf) {
    $args = @('--smoke-check', ('"' + $report + '"'))
    if ($ExportPdf) {
        $pdfFolder = Join-Path $qa 'PDF'
        New-Item -ItemType Directory -Force -Path $pdfFolder | Out-Null
        $args += @('--smoke-pdf', ('"' + $pdfFolder + '"'))
    }
    if ($ImportSample -or $ExportPdf) { $args += ('"' + $sample + '"') }
    $oldPath = $env:PATH
    $oldPythonPath = $env:PYTHONPATH
    $oldPythonHome = $env:PYTHONHOME
    $oldQt = $env:QT_QPA_PLATFORM
    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        $env:PYTHONHOME = Join-Path $qa 'absent-python'
        $env:PYTHONPATH = Join-Path $qa 'absent-python'
        $env:QT_QPA_PLATFORM = 'windows'
        $process = Start-Process -FilePath $exe -ArgumentList $args -WindowStyle Hidden -PassThru
        $timeout = if ($ExportPdf) { 165000 } else { 30000 }
        if (-not $process.WaitForExit($timeout)) { throw "Timeout for $exe" }
        if (-not (Test-Path -LiteralPath $report)) { throw "Missing application report, exit $($process.ExitCode)" }
        $check = Get-Content -Raw -Encoding utf8 -LiteralPath $report | ConvertFrom-Json
        if ($ExportPdf) {
            $pdf = Join-Path $pdfFolder (([IO.Path]::GetFileNameWithoutExtension($sample)) + '.pdf')
            if ($process.ExitCode -ne 0 -or $check.results[0].status -ne 'created' -or
                (Get-Item -LiteralPath $pdf).Length -le 0) {
                throw "Packaged Excel PDF check failed. See $report."
            }
            return $check
        }
        if ($process.ExitCode -ne 0 -or $check.error) { throw "App failed: $($check.error)" }
        if ($check.schema -ne 'cc92b2a1f8670d230a405d42f9ad08a729a6a35cab94751d5bb1208cf493e361') {
            throw 'Unexpected XSD.'
        }
        if (($check.icon_sizes -join ',') -ne '16,24,32,48,64,128,256') {
            throw 'The packaged Qt window icon is missing or incomplete.'
        }
        if ($Legacy) {
            if ($check.version -ne '2.1.0') { throw 'The upgrade baseline must be 2.1.0.' }
        }
        else {
            $metadata = (Get-Item -LiteralPath $exe).VersionInfo
            if ($check.name -ne 'СИ-Экспорт' -or $metadata.ProductName -ne $check.name -or
                $metadata.FileVersion -ne $check.version -or $metadata.OriginalFilename -ne 'SI-Export.exe') {
                throw 'Unexpected Windows executable or Qt application metadata.'
            }
        }
        if ($ImportSample -and ($check.protocols -ne 1 -or $check.instruments -ne 25 -or
                $check.equipment -ne 7 -or $check.ready -ne 0)) {
            throw 'Workbook check failed.'
        }
        $check
    }
    finally {
        $env:PATH = $oldPath
        $env:PYTHONHOME = $oldPythonHome
        $env:PYTHONPATH = $oldPythonPath
        $env:QT_QPA_PLATFORM = $oldQt
    }
}

function Test-RegistryConnection($exe, $report) {
    $args = @('--check-connection', ('"' + $report + '"'))
    $process = Start-Process -FilePath $exe -ArgumentList $args -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(90000)) { throw "Registry connection check timed out for $exe." }
    if (-not (Test-Path -LiteralPath $report)) { throw "Missing registry report, exit $($process.ExitCode)." }
    $reportData = Get-Content -Raw -Encoding utf8 -LiteralPath $report | ConvertFrom-Json
    foreach ($name in 'MIT','MIETA','VRI') {
        if ($reportData.registries.$name -ne 'Доступен') { throw "Registry check failed for ${name}. See $report." }
    }
    if ($process.ExitCode -ne 0) { throw "Registry connection check returned exit $($process.ExitCode)." }
    $reportData
}

$result = [ordered]@{ test_path = $qa; excel_unchanged = $false }
if (-not $TestInstaller) {
    Expand-Archive -LiteralPath (Join-Path $release 'SI-Export-Portable-x64.zip') -DestinationPath $qa
    $portable = Join-Path $qa 'SI-Export'
    $relocated = Join-Path $qa 'Перенесённая папка СИ-Экспорт'
    Test-Guide $portable
    $before = Test-Executable (Join-Path $portable 'SI-Export.exe') (Join-Path $qa 'portable-1.json') -ImportSample
    if ($before.data -ne (Join-Path $portable 'data')) { throw 'Incorrect portable data directory.' }
    Move-Item -LiteralPath $portable -Destination $relocated
    $after = Test-Executable (Join-Path $relocated 'SI-Export.exe') (Join-Path $qa 'portable-2.json')
    if ($after.previous -ne '1' -or $after.data -ne (Join-Path $relocated 'data') -or
            $after.input_folder -ne $before.input_folder) { throw 'Portable data did not survive the move.' }
    $result.portable = @{ started = $true; relocated = $true; restored_last_folder = $true; sample_results = 25 }
    if ($TestPdf) {
        $result.pdf = Test-Executable (Join-Path $relocated 'SI-Export.exe') (Join-Path $qa 'portable-pdf.json') -ExportPdf
    }
    if ($CheckConnection) {
        $result.connection = Test-RegistryConnection (Join-Path $relocated 'SI-Export.exe') (Join-Path $qa 'portable-connection.json')
    }
}
else {
    $installTarget = Join-Path $qa 'Тест установки'
    $installer = Join-Path $release 'SI-Export-Setup-x64.exe'
    $appDataRoot = Join-Path $qa 'UserProfileData'
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6462E024-6818-4B52-AA51-9ED6CCB87EC2}_is1'
    $programs = [Environment]::GetFolderPath('Programs')
    $desktopFolder = [Environment]::GetFolderPath('DesktopDirectory')
    $menu = Join-Path $programs 'СИ-Экспорт.lnk'
    $desktop = Join-Path $desktopFolder 'СИ-Экспорт.lnk'
    $legacyMenu = Join-Path $programs 'Аршин · Генератор XML.lnk'
    $legacyDesktop = Join-Path $desktopFolder 'Аршин · Генератор XML.lnk'
    foreach ($item in $key, $menu, $desktop) {
        if (Test-Path -LiteralPath $item) {
            throw 'An existing installation or shortcut is present. The test leaves it untouched.'
        }
    }
    $existingLegacyLinks = @{}
    foreach ($link in $legacyMenu, $legacyDesktop) {
        if (Test-Path -LiteralPath $link) {
            if ($UpgradeFrom) { throw 'A previous shortcut already exists. The upgrade test leaves it untouched.' }
            $existingLegacyLinks[$link] = (Get-FileHash -LiteralPath $link).Hash
        }
    }
    $uninstaller = Join-Path $installTarget 'unins000.exe'
    $exe = Join-Path $installTarget 'SI-Export.exe'
    $legacyExe = Join-Path $installTarget 'ArshinGenerator.exe'
    $database = Join-Path $appDataRoot 'ArshinGenerator\arshin.sqlite3'
    $oldLocal = $env:LOCALAPPDATA

    function Install-TestPackage($package, $log, [switch] $SelectDesktop) {
        $options = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                     ('/DIR="' + $installTarget + '"'), ('/LOG="' + (Join-Path $qa $log) + '"'))
        if ($SelectDesktop) { $options += '/TASKS=desktopicon' }
        $setup = Start-Process -FilePath $package -WindowStyle Hidden -PassThru -Wait -ArgumentList $options
        if ($setup.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $uninstaller)) {
            throw "Installation failed ($($setup.ExitCode))."
        }
    }

    try {
        $env:LOCALAPPDATA = $appDataRoot
        if ($UpgradeFrom) {
            Install-TestPackage (Get-Item -LiteralPath $UpgradeFrom).FullName 'install-2.1.0.log' -SelectDesktop
            if (-not (Test-Path -LiteralPath $legacyMenu) -or -not (Test-Path -LiteralPath $legacyDesktop)) {
                throw 'The previous installer did not make both original shortcuts.'
            }
            $initial = Test-Executable $legacyExe (Join-Path $qa 'installed-2.1.0.json') -ImportSample -Legacy
            if ($initial.data -ne (Split-Path $database)) { throw 'Unexpected legacy data path.' }
            & $Python -X utf8 (Join-Path $PSScriptRoot 'seed-upgrade.py') $database $qa
            if ($LASTEXITCODE -ne 0) { throw 'Could not seed the isolated legacy database.' }
            $legacyDbHash = (Get-FileHash -LiteralPath $database).Hash
            $legacyXml = Join-Path $qa 'Контрольный XML.xml'
            $legacyXmlHash = (Get-FileHash -LiteralPath $legacyXml).Hash
            Install-TestPackage $installer 'upgrade.log'
            if ((Get-FileHash -LiteralPath $database).Hash -ne $legacyDbHash) {
                throw 'The update changed the existing database.'
            }
            if ((Get-FileHash -LiteralPath $legacyExe).Hash -ne (Get-FileHash -LiteralPath $exe).Hash) {
                throw 'The legacy executable path did not receive the new application.'
            }
            $before = Test-Executable $legacyExe (Join-Path $qa 'upgraded-legacy-path.json')
            if ($before.previous -ne '1' -or $before.output_folder -ne $qa -or $before.xml_automatic) {
                throw 'The upgraded app lost legacy settings or changed the default save mode.'
            }
            $expectedPrevious = '2'
        }
        else {
            New-Item -ItemType Directory -Force $installTarget | Out-Null
            Set-Content -LiteralPath (Join-Path $installTarget 'Инструкция.md') -Value 'Previous guide'
            Install-TestPackage $installer 'install.log' -SelectDesktop
            if (Test-Path -LiteralPath $legacyExe) { throw 'A clean installation contains a legacy executable path.' }
            $before = Test-Executable $exe (Join-Path $qa 'installed-1.json') -ImportSample
            $expectedPrevious = '1'
        }
        Test-Guide $installTarget
        if ((Get-ItemPropertyValue -LiteralPath $key -Name DisplayName) -ne 'СИ-Экспорт') {
            throw 'Incorrect application name in Installed apps.'
        }
        if (-not (Test-Path -LiteralPath $menu) -or -not (Test-Path -LiteralPath $desktop)) {
            throw 'Expected Start or opted-in Desktop shortcut not created.'
        }
        if ($UpgradeFrom -and ((Test-Path -LiteralPath $legacyMenu) -or (Test-Path -LiteralPath $legacyDesktop))) {
            throw 'Old shortcuts are still present.'
        }
        $shortcuts = New-Object -ComObject WScript.Shell
        $shell = New-Object -ComObject Shell.Application
        foreach ($link in $menu, $desktop) {
            $shortcut = $shortcuts.CreateShortcut($link)
            $shellItem = $shell.NameSpace((Split-Path $link)).ParseName((Split-Path $link -Leaf))
            if ($shortcut.TargetPath -ne $exe -or $shortcut.IconLocation -ne "$exe,0" -or
                $shellItem.ExtendedProperty('System.AppUserModel.ID') -ne 'ArshinGenerator.Desktop') {
                throw "Incorrect shortcut target, icon or AppUserModelID: $link"
            }
        }
        if ((Get-ItemPropertyValue -LiteralPath $key -Name DisplayIcon) -ne $exe) {
            throw 'Unexpected icon for Installed apps.'
        }
        if (Test-Path -LiteralPath (Join-Path $installTarget 'portable.flag')) { throw 'Installed build has portable mode flag.' }
        $after = Test-Executable $exe (Join-Path $qa 'installed-2.json')
        if ($TestPdf) {
            $result.pdf = Test-Executable $exe (Join-Path $qa 'installed-pdf.json') -ExportPdf
        }
        if ($CheckConnection) {
            $result.connection = Test-RegistryConnection $exe (Join-Path $qa 'installed-connection.json')
        }
        if ($after.previous -ne $expectedPrevious -or $after.input_folder -ne $before.input_folder -or
                $after.data -ne (Split-Path $database)) {
            throw 'Installed app settings or per-user data path verification failed.'
        }
    }
    finally {
        $env:LOCALAPPDATA = $oldLocal
        if (Test-Path -LiteralPath $uninstaller) {
            $rootCheck = [IO.Path]::GetFullPath($uninstaller)
            if (-not $rootCheck.StartsWith($qa + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Uninstaller is outside the isolated QA directory.'
            }
            $uninstall = Start-Process -FilePath $rootCheck -WindowStyle Hidden -PassThru -Wait -ArgumentList @(
                '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/LOG="' + (Join-Path $qa 'uninstall.log') + '"')
            )
            if ($uninstall.ExitCode -ne 0) { throw "Uninstall failed ($($uninstall.ExitCode))." }
        }
    }
    foreach ($item in $key, $menu, $desktop, $exe, $legacyExe) {
        if (Test-Path -LiteralPath $item) { throw "Uninstall left an installed item behind: $item" }
    }
    foreach ($link in $legacyMenu, $legacyDesktop) {
        if ($existingLegacyLinks.ContainsKey($link)) {
            if ((Get-FileHash -LiteralPath $link).Hash -ne $existingLegacyLinks[$link]) {
                throw 'An existing shortcut was changed during the clean installation test.'
            }
        }
        elseif (Test-Path -LiteralPath $link) { throw "Unexpected legacy shortcut: $link" }
    }
    if (-not (Test-Path -LiteralPath $database)) {
        throw 'Uninstall removed application data.'
    }
    if ($UpgradeFrom -and (Get-FileHash -LiteralPath $legacyXml).Hash -ne $legacyXmlHash) {
        throw 'The update or uninstaller changed an existing XML file.'
    }
    $result.installer = @{ started = $true; per_user = $true; start_menu = $true; desktop_opt_in = $true; icons = $true;
                           settings_restored = $true; uninstall = $true; retained_data = $true; sample_results = 25;
                           upgraded_from_2_1_0 = [bool]$UpgradeFrom }
}

$result.excel_unchanged = $baseHash -eq (Get-WorkbookHash)
if (-not $result.excel_unchanged) { throw 'Source workbook changed.' }
$result | ConvertTo-Json -Depth 4
