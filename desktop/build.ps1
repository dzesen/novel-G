param([string]$StageName = ("stage-" + (Get-Date -Format "yyyyMMdd-HHmmss")))
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repo
$toolchain = Join-Path $repo 'reports/desktop-toolchain'
$build = Join-Path $repo 'reports/desktop-build'
$python = Join-Path $repo '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the documented Python 3.12 .venv first.' }
function Run-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build command failed: $Program" }
}
Run-Checked $python @('desktop/fetch_runtime.py')
$buildPython = Join-Path $toolchain 'python-build/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) { Run-Checked $python @('-m', 'venv', (Join-Path $toolchain 'python-build')) }
Run-Checked $buildPython @('-m','pip','install','-r','requirements.txt','pyinstaller==6.22.2')
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:NEXT_TELEMETRY_DISABLED = '1'
$env:PYTHONPATH = $repo
$dotnet = Join-Path $toolchain 'dotnet/dotnet.exe'
Run-Checked $dotnet @('publish','desktop/NovelG.Desktop/NovelG.Desktop.csproj','-c','Release','-r','win-x64','--self-contained','true','-o',(Join-Path $build 'host'),'-p:RestoreLockedMode=true')
Run-Checked $buildPython @('-m','PyInstaller','desktop/backend.spec','--distpath',(Join-Path $build 'backend'),'--workpath',(Join-Path $build 'pyinstaller'),'--noconfirm')
$nodeDirectory = Join-Path $toolchain 'node/node-v24.21.0-win-x64'
$env:PATH = "$nodeDirectory;$env:PATH"
$frontendBuild = Join-Path $build ($StageName + '-frontend')
Run-Checked $python @('desktop/prepare_frontend.py',$frontendBuild)
Push-Location -LiteralPath $frontendBuild
try {
    Run-Checked (Join-Path $nodeDirectory 'npm.cmd') @('ci')
    $env:NOVEL_G_DESKTOP_BUILD = '1'
    Run-Checked (Join-Path $nodeDirectory 'npm.cmd') @('run','build')
} finally {
    Remove-Item Env:NOVEL_G_DESKTOP_BUILD -ErrorAction SilentlyContinue
    Pop-Location
}
$stage = Join-Path $build $StageName
Run-Checked $python @('desktop/stage_runtime.py','--output',$stage,'--frontend',$frontendBuild)
Run-Checked $buildPython @('desktop/collect_notices.py',(Join-Path $stage 'licenses'),$frontendBuild)
$inno = Join-Path $toolchain 'inno/ISCC.exe'
if (-not (Test-Path -LiteralPath $inno)) {
    $innoProcess = Start-Process -FilePath (Join-Path $toolchain 'innosetup-7.1.0-x64.exe') -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER',('/DIR="' + (Join-Path $toolchain 'inno') + '"') -WindowStyle Hidden -Wait -PassThru
    if ($innoProcess.ExitCode -ne 0) { throw 'Unable to install the build-only Inno Setup compiler.' }
}
Run-Checked $inno @(("/DStageDir=$stage"),("/DOutputPath=" + (Join-Path $repo 'reports/desktop-installer')),'desktop/installer.iss')
Write-Output "Staged desktop: $stage"
