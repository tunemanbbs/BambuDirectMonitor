$ErrorActionPreference = "Stop"

param(
    [Parameter(Mandatory = $true)]
    [string] $PfxPath,

    [string] $ExePath = (Join-Path $PSScriptRoot "dist\BambuDirectMonitor.exe"),

    [string] $TimestampUrl = "http://timestamp.digicert.com"
)

function Find-SignTool {
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) {
        return $onPath.Source
    }

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits",
        "${env:ProgramFiles}\Windows Kits"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    foreach ($root in $roots) {
        $match = Get-ChildItem -LiteralPath $root -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    throw "signtool.exe was not found. Install the Windows SDK Signing Tools component."
}

if (-not (Test-Path -LiteralPath $PfxPath)) {
    throw "PFX file was not found: $PfxPath"
}

if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "EXE file was not found: $ExePath"
}

$signTool = Find-SignTool
$securePassword = Read-Host "PFX password" -AsSecureString
$passwordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)

try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPtr)

    & $signTool sign `
        /f $PfxPath `
        /p $plainPassword `
        /fd SHA256 `
        /tr $TimestampUrl `
        /td SHA256 `
        $ExePath

    if ($LASTEXITCODE -ne 0) {
        throw "signtool sign failed"
    }

    & $signTool verify /pa /v $ExePath
    if ($LASTEXITCODE -ne 0) {
        throw "signtool verify failed"
    }
}
finally {
    if ($passwordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPtr)
    }
}

Write-Host "Signed and verified: $ExePath"
