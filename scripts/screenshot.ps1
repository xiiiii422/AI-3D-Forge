<#
.SYNOPSIS
    AI 3D Forge -- capture a screenshot of the running UI (visual acceptance).

.DESCRIPTION
    Uses the system Edge browser in headless mode to visit the running UI and
    save a PNG. The result can be handed to a vision model for review.

    IMPORTANT: this script is ASCII-only, comments included.
    Windows PowerShell 5.1 reads a BOM-less .ps1 as the system ANSI codepage
    (GBK on Chinese Windows). Non-ASCII bytes -- even inside comments -- can
    break parsing. Keep every .ps1 in this project ASCII-only.
    Also avoid $args: it is a reserved automatic variable in PowerShell.

.EXAMPLE
    .\scripts\screenshot.ps1 -Url "http://127.0.0.1:8770/welcome" -Out "build\shots\welcome.png"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$Width = 1440,
    [int]$Height = 900,
    [int]$WaitMs = 4500,
    [switch]$ReuseProfile
)

$ErrorActionPreference = 'Stop'

$edgeCandidates = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
)
$edgeExe = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edgeExe) { throw 'Microsoft Edge not found; cannot capture screenshots.' }

$outPath = [System.IO.Path]::GetFullPath($Out)
$outDir = Split-Path -Parent $outPath
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $outPath) { Remove-Item $outPath -Force }

# Probe the page first so an error page is not mistaken for a UI defect.
try {
    $probe = Invoke-WebRequest -Uri $Url -TimeoutSec 15 -UseBasicParsing
    Write-Host "  reachable (HTTP $($probe.StatusCode)): $Url" -ForegroundColor DarkGray
}
catch {
    Write-Host "  [WARN] page not reachable: $($_.Exception.Message)" -ForegroundColor Yellow
}

# Use a FRESH profile per invocation by default.
# Reusing one profile makes Edge restore the previous session instead of
# honouring the URL argument, which silently produces identical screenshots
# for different routes and would hide real page-breaking bugs.
$profileDir = Join-Path $env:TEMP ("aiforge-edge-" + [guid]::NewGuid().ToString('N').Substring(0, 10))

$browserArgs = @(
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    '--no-first-run',
    '--no-default-browser-check',
    '--no-restore-last-session',
    '--disable-session-crashed-bubble',
    '--disable-extensions',
    '--disable-features=Translate,MediaRouter',
    "--user-data-dir=$profileDir",
    "--window-size=$Width,$Height",
    "--virtual-time-budget=$WaitMs",
    "--screenshot=$outPath",
    $Url
)

try {
    $proc = Start-Process -FilePath $edgeExe -ArgumentList $browserArgs -Wait -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 500
}
finally {
    if (-not $ReuseProfile) {
        Start-Sleep -Milliseconds 300
        Remove-Item $profileDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path $outPath)) {
    throw "Screenshot failed (Edge exit code $($proc.ExitCode)): $outPath"
}

$sizeKb = [math]::Round((Get-Item $outPath).Length / 1KB, 1)
Write-Host "  captured: $outPath ($sizeKb KB, $Width x $Height)" -ForegroundColor Green
