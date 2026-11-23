param([switch] $TestInstaller, [switch] $CheckConnection)
$ErrorActionPreference = 'Stop'
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

function Test-Executable($exe, $report, [switch] $ImportSample) {
    $args = @('--smoke-check', ('"' + $report + '"'))
    if ($ImportSample) { $args += ('"' + $sample + '"') }
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
        if (-not $process.WaitForExit(30000)) { throw "Timeout for $exe" }
        if (-not (Test-Path -LiteralPath $report)) { throw "Missing application report, exit $($process.ExitCode)" }
        $check = Get-Content -Raw -Encoding utf8 -LiteralPath $report | ConvertFrom-Json
        if ($process.ExitCode -ne 0 -or $check.error) { throw "App failed: $($check.error)" }
        if ($check.schema -ne 'cc92b2a1f8670d230a405d42f9ad08a729a6a35cab94751d5bb1208cf493e361') {
            throw 'Unexpected XSD.'
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
    Expand-Archive -LiteralPath (Join-Path $release 'ArshinGenerator-Portable-x64.zip') -DestinationPath $qa
    $portable = Join-Path $qa 'ArshinGenerator'
    $relocated = Join-Path $qa 'Перенесённая папка Аршин'
    $before = Test-Executable (Join-Path $portable 'ArshinGenerator.exe') (Join-Path $qa 'portable-1.json') -ImportSample
    if ($before.data -ne (Join-Path $portable 'data')) { throw 'Incorrect portable data directory.' }
    Move-Item -LiteralPath $portable -Destination $relocated
    $after = Test-Executable (Join-Path $relocated 'ArshinGenerator.exe') (Join-Path $qa 'portable-2.json')
    if ($after.previous -ne '1' -or $after.data -ne (Join-Path $relocated 'data') -or
            $after.input_folder -ne $before.input_folder) { throw 'Portable data did not survive the move.' }
    $result.portable = @{ started = $true; relocated = $true; restored_last_folder = $true; sample_results = 25 }
    if ($CheckConnection) {
        $result.connection = Test-RegistryConnection (Join-Path $relocated 'ArshinGenerator.exe') (Join-Path $qa 'portable-connection.json')
    }
}
else {
    $installTarget = Join-Path $qa 'Тест установки'
    $installer = Join-Path $release 'ArshinGenerator-Setup-x64.exe'
    $appDataRoot = Join-Path $qa 'UserProfileData'
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6462E024-6818-4B52-AA51-9ED6CCB87EC2}_is1'
    $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Аршин · Генератор XML.lnk'
    $desktop = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Аршин · Генератор XML.lnk'
    if ((Test-Path -LiteralPath $key) -or (Test-Path -LiteralPath $menu) -or (Test-Path -LiteralPath $desktop)) {
        throw 'An existing installation or shortcut is present. The test leaves it untouched.'
    }
    $installed = $false
    $uninstaller = Join-Path $installTarget 'unins000.exe'
    try {
        $setup = Start-Process -FilePath $installer -WindowStyle Hidden -PassThru -Wait -ArgumentList @(
            '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/TASKS=desktopicon',
            ('/DIR="' + $installTarget + '"'), ('/LOG="' + (Join-Path $qa 'install.log') + '"')
        )
        $installed = Test-Path -LiteralPath $uninstaller
        if ($setup.ExitCode -ne 0 -or -not $installed) { throw "Installation failed ($($setup.ExitCode))." }
        if (-not (Test-Path -LiteralPath $key)) { throw 'No per-user uninstall registration.' }
        if (-not (Test-Path -LiteralPath $menu) -or -not (Test-Path -LiteralPath $desktop)) {
            throw 'Expected Start or opted-in Desktop shortcut not created.'
        }
        if (Test-Path -LiteralPath (Join-Path $installTarget 'portable.flag')) { throw 'Installed build has portable mode flag.' }
        $oldLocal = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $appDataRoot
            $exe = Join-Path $installTarget 'ArshinGenerator.exe'
            $before = Test-Executable $exe (Join-Path $qa 'installed-1.json') -ImportSample
            $after = Test-Executable $exe (Join-Path $qa 'installed-2.json')
            if ($CheckConnection) {
                $result.connection = Test-RegistryConnection $exe (Join-Path $qa 'installed-connection.json')
            }
        }
        finally {
            $env:LOCALAPPDATA = $oldLocal
        }
        if ($after.previous -ne '1' -or $after.input_folder -ne $before.input_folder -or
                $after.data -ne (Join-Path $appDataRoot 'ArshinGenerator')) {
            throw 'Installed app settings or per-user data path verification failed.'
        }
    }
    finally {
        if ($installed -and (Test-Path -LiteralPath $uninstaller)) {
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
    if ((Test-Path -LiteralPath $key) -or (Test-Path -LiteralPath $menu) -or
            (Test-Path -LiteralPath $desktop) -or (Test-Path -LiteralPath (Join-Path $installTarget 'ArshinGenerator.exe'))) {
        throw 'Uninstall left the application or its shortcuts behind.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $appDataRoot 'ArshinGenerator\arshin.sqlite3'))) {
        throw 'Uninstall removed application data.'
    }
    $result.installer = @{ started = $true; per_user = $true; start_menu = $true; desktop_opt_in = $true;
                           settings_restored = $true; uninstall = $true; retained_data = $true; sample_results = 25 }
}

$result.excel_unchanged = $baseHash -eq (Get-WorkbookHash)
if (-not $result.excel_unchanged) { throw 'Source workbook changed.' }
$result | ConvertTo-Json -Depth 4
