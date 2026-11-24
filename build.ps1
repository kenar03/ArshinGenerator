# Requires PowerShell 7 or Windows PowerShell 5.1 and Python 3.12 x64.
[CmdletBinding()]
param(
    [string] $Python = 'python',
    [string] $IsccPath = '',
    [switch] $UseExistingEnvironment,
    [switch] $PortableOnly
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location $root
$oldPyInstallerCache = $env:PYINSTALLER_CONFIG_DIR
try {
    & $Python -c "import struct,sys; assert sys.platform=='win32' and sys.version_info[:2]==(3,12) and struct.calcsize('P')==8, 'Python 3.12 x64 required'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 required.' }
    if (-not $UseExistingEnvironment) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
        $Python = Join-Path $root '.venv\Scripts\python.exe'
        & $Python -m pip --disable-pip-version-check install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) { throw 'Could not install pinned build dependencies.' }
    }

    $dist = Join-Path $root 'build\distribution'
    $out = Join-Path $root 'release'
    $work = Join-Path $root 'build\pyinstaller'
    $appIcon = Join-Path $root 'arshin_generator\assets\app.ico'
    New-Item -ItemType Directory -Force $work,$dist,$out | Out-Null
    $version = & $Python -c 'from arshin_generator import VERSION; print(VERSION)'
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid application version.' }
    $versionResource = Join-Path $work 'version-info.txt'
    $versionTemplate = Get-Content -Raw -Encoding utf8 -LiteralPath 'packaging/windows-version.txt'
    $versionInfo = $versionTemplate.Replace('@VERSION@', $version).Replace('@VERSION_TUPLE@', ($version.Replace('.', ', ') + ', 0'))
    [IO.File]::WriteAllText($versionResource, $versionInfo)
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $work 'cache'
    & $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name SI-Export `
        --paths $root --workpath $work --distpath $dist --specpath $work `
        --icon $appIcon --version-file $versionResource `
        --add-data "${appIcon}:arshin_generator/assets" `
        --add-data "$root/arshin_generator/schemas/import.2020-06-19.xsd:arshin_generator/schemas" `
        --exclude-module numpy --exclude-module pandas --exclude-module matplotlib `
        'arshin_generator/__main__.py'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    $portable = Join-Path $dist 'SI-Export'
    New-Item -ItemType File -Force (Join-Path $portable 'portable.flag') | Out-Null
    Copy-Item docs/quick_start.md (Join-Path $portable 'Инструкция.md')
    & $Python packaging/collect_licenses.py $portable
    if ($LASTEXITCODE -ne 0) { throw 'Could not collect third-party notices.' }
    Compress-Archive -LiteralPath $portable -DestinationPath (Join-Path $out 'SI-Export-Portable-x64.zip') -Force

    if (-not $PortableOnly) {
        if (-not $IsccPath) {
            $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
            if ($command) { $IsccPath = $command.Source }
            else {
                $candidates = @(
                    (Join-Path "${env:ProgramFiles(x86)}" 'Inno Setup 6\ISCC.exe'),
                    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
                )
                $IsccPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
            }
        }
        if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) {
            throw 'Portable build is ready. Set -IsccPath to the Inno Setup 6 ISCC.exe path.'
        }
        & $IsccPath "/DPortablePath=$portable" "/DOutputPath=$out" "/DAppVersion=$version" 'packaging/arshin.iss'
        if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
    }
}
finally {
    $env:PYINSTALLER_CONFIG_DIR = $oldPyInstallerCache
    Pop-Location
}
