$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "src\bambu_direct_monitor.py"
$dist = Join-Path $root "dist"
$work = Join-Path $root "build"

Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dist | Out-Null

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "BambuDirectMonitor" `
    --distpath $dist `
    --workpath $work `
    --specpath $root `
    --hidden-import paho.mqtt.client `
    --hidden-import curl_cffi `
    --collect-submodules curl_cffi `
    $src

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed"
}

$exe = Join-Path $dist "BambuDirectMonitor.exe"
if (-not (Test-Path $exe)) {
    throw "Build completed but exe was not found: $exe"
}

Write-Host "Built: $exe"
