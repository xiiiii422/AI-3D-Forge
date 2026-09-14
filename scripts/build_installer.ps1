<#
.SYNOPSIS
    AI 3D Forge -- build the Windows installer (AI-3D-Forge-Setup.exe).

.DESCRIPTION
    Compiles installer\AI-3D-Forge.iss with Inno Setup 6 (or 7).

    If Inno Setup is missing, the script acquires it automatically. Acquisition
    route 1 is winget (the official Windows package manager): it resolves the
    same jrsoftware package but fetches through Microsoft's delivery CDN, which
    works on networks where github.com is blocked -- measured on the build
    machine, where api.github.com is reachable but github.com times out and the
    official download page only links to github.com assets. Route 2 is a direct
    download, used when winget is unavailable.

    Inno Setup is needed on the BUILD machine only. End users never need it.

    Output: installer\Output\AI-3D-Forge-Setup.exe

    IMPORTANT: ASCII-only (comments included). Windows PowerShell 5.1 reads a
    BOM-less .ps1 as the system ANSI codepage and non-ASCII bytes break parsing.

.EXAMPLE
    .\scripts\build_installer.ps1
    .\scripts\build_installer.ps1 -ForceDownload
#>

[CmdletBinding()]
param(
    [switch]$ForceDownload
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$Work = Join-Path $Root 'build\work'
$Cache = Join-Path $Root 'build\downloads'
New-Item -ItemType Directory -Force -Path $Work, $Cache | Out-Null

function Write-Ok   { param([string]$T) Write-Host "  [OK]   $T" -ForegroundColor Green }
function Write-Warn { param([string]$T) Write-Host "  [WARN] $T" -ForegroundColor Yellow }
function Write-Err  { param([string]$T) Write-Host "  [FAIL] $T" -ForegroundColor Red }

Write-Host ''
Write-Host '  AI 3D Forge -- building installer' -ForegroundColor White

# --------------------------------------------------------------------------
# 1. Locate ISCC.exe
# --------------------------------------------------------------------------
function Find-InnoCompiler {
    $candidates = @(
        'D:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'D:\Program Files\Inno Setup 6\ISCC.exe',
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 5\ISCC.exe'),
        (Join-Path $Root 'build\tools\Inno Setup 6\ISCC.exe')
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) { return $path }
    }

    foreach ($hive in @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )) {
        $entry = Get-ItemProperty $hive -ErrorAction SilentlyContinue |
                 Where-Object { $_.DisplayName -like 'Inno Setup*' -and $_.InstallLocation } |
                 Select-Object -First 1
        # InstallLocation can be present but empty/null. Join-Path throws on a
        # null -Path ("Cannot bind argument to parameter 'Path' because it is
        # null"), so validate the value instead of trusting the filter alone.
        if ($entry -and $entry.InstallLocation) {
            $location = ([string]$entry.InstallLocation).Trim()
            if ($location) {
                $exe = Join-Path $location 'ISCC.exe'
                if (Test-Path $exe) { return $exe }
            }
        }
    }

    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Install-InnoViaWinget {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }

    Write-Host '  Trying winget (official Windows package manager)...' -ForegroundColor DarkGray
    $common = @('--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')

    $global:LASTEXITCODE = 0
    & $winget.Source install --id JRSoftware.InnoSetup @common 2>&1 |
        ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -eq 0) { return $true }

    Write-Host '  Inno Setup 6 unavailable; trying Inno Setup 7...' -ForegroundColor DarkGray
    $global:LASTEXITCODE = 0
    & $winget.Source install --id JRSoftware.InnoSetup.7 @common 2>&1 |
        ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    return ($LASTEXITCODE -eq 0)
}

function Get-InnoDownloadUrl {
    # The official download page links only to github.com release assets, which
    # is unreachable on some networks. Scrape it anyway so the fallback keeps
    # working when the version changes.
    try {
        $page = Invoke-WebRequest -Uri 'https://jrsoftware.org/isdl.php' -UseBasicParsing -TimeoutSec 40
        $m = [regex]::Match($page.Content, 'href="(https://github\.com/[^"]*innosetup-[67][^"]*\.exe)"', 'IgnoreCase')
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe'
}

$iscc = Find-InnoCompiler

# --------------------------------------------------------------------------
# 2. Acquire Inno Setup when missing
# --------------------------------------------------------------------------
if (-not $iscc -or $ForceDownload) {
    Write-Warn 'Inno Setup not found. Acquiring it automatically (build machine only, never needed by end users)...'

    $target = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6'
    if (Test-Path 'D:\') { $target = 'D:\Program Files (x86)\Inno Setup 6' }

    if (Install-InnoViaWinget) {
        $iscc = Find-InnoCompiler
        if ($iscc) { Write-Ok "Inno Setup installed via winget: $iscc" }
    }

    if (-not $iscc) {
        $installer = Join-Path $Cache 'innosetup-setup.exe'
        $url = Get-InnoDownloadUrl
        Write-Host "  Direct download: $url" -ForegroundColor DarkGray
        if (Test-Path $installer) { Remove-Item $installer -Force -ErrorAction SilentlyContinue }
        try {
            Invoke-WebRequest -Uri $url -OutFile $installer -TimeoutSec 300 -UseBasicParsing
        } catch {
            Write-Warn "Direct download failed: $($_.Exception.Message)"
        }

        if ((Test-Path $installer) -and ((Get-Item $installer).Length -gt 1MB)) {
            $sizeMb = [math]::Round((Get-Item $installer).Length / 1MB, 2)
            Write-Ok "Installer downloaded ($sizeMb MB)"
            Write-Host "  Silent install to: $target" -ForegroundColor DarkGray
            $installerArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCANCEL', "/DIR=`"$target`"")
            $proc = Start-Process -FilePath $installer -ArgumentList $installerArgs -Wait -PassThru
            Write-Host "  Setup exit code: $($proc.ExitCode)" -ForegroundColor DarkGray
        }
        $iscc = Find-InnoCompiler
    }

    if (-not $iscc) {
        Write-Err 'Could not obtain Inno Setup automatically.'
        Write-Host '  Options:' -ForegroundColor Yellow
        Write-Host '    winget install --id JRSoftware.InnoSetup --exact' -ForegroundColor Yellow
        Write-Host '    or download from https://jrsoftware.org/isdl.php' -ForegroundColor Yellow
        Write-Host '    or ship the portable app only: scripts\build.ps1 -SkipInstaller' -ForegroundColor Yellow
        exit 1
    }
}

Write-Ok "Inno Setup compiler: $iscc"

# --------------------------------------------------------------------------
# 3. Verify the packaged app exists
# --------------------------------------------------------------------------
$packageDir = Join-Path $Root 'build\dist\AI-3D-Forge'
$packedExe = Join-Path $packageDir 'AI-3D-Forge.exe'
if (-not (Test-Path $packedExe)) {
    Write-Err "Missing packaged app: $packedExe"
    Write-Host '  Run scripts\build.ps1 first.' -ForegroundColor Yellow
    exit 1
}
$pkgSize = [math]::Round(((Get-ChildItem $packageDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Ok "Packaged app ready ($pkgSize MB)"

# --------------------------------------------------------------------------
# 4. Compile
# --------------------------------------------------------------------------
Write-Host ''
Write-Host '  Compiling installer...' -ForegroundColor Cyan
$compileLog = Join-Path $Work 'iscc.log'
Push-Location (Join-Path $Root 'installer')
try {
    $isccProc = Start-Process -FilePath $iscc -ArgumentList @('/Qp', 'AI-3D-Forge.iss') `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $compileLog -RedirectStandardError (Join-Path $Work 'iscc.err.log')
    $isccCode = $isccProc.ExitCode
} finally {
    Pop-Location
}

if (Test-Path $compileLog) {
    Get-Content $compileLog | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}
# ISCC writes warnings/errors to stderr. On success that file is EMPTY, and
# Get-Content on an empty file returns $null -- piping $null into
# Where-Object { $_.Trim() } then throws
# "Cannot bind argument to parameter 'Path' because it is null"
# even with $ErrorActionPreference. Collect into an array first.
$errLog = Join-Path $Work 'iscc.err.log'
if (Test-Path $errLog) {
    $errors = @(Get-Content $errLog -ErrorAction SilentlyContinue | Where-Object { $_ -and "$_".Trim() })
    foreach ($line in $errors) { Write-Host "    $line" -ForegroundColor DarkYellow }
}

if ($isccCode -ne 0) {
    Write-Err "Installer compilation failed (ISCC exit code $isccCode)."
    exit 1
}

$setup = Join-Path $Root 'installer\Output\AI-3D-Forge-Setup.exe'
if (-not (Test-Path $setup)) {
    Write-Err "Expected installer not produced: $setup"
    exit 1
}

$setupSize = [math]::Round((Get-Item $setup).Length / 1MB, 1)
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash
Write-Host ''
Write-Ok "Installer built ($setupSize MB)"
Write-Host "  Path   : $setup" -ForegroundColor White
Write-Host "  SHA-256: $hash" -ForegroundColor DarkGray

$manifest = [ordered]@{
    product  = 'AI 3D Forge'
    version  = '1.0.0'
    setup    = 'AI-3D-Forge-Setup.exe'
    size_mb  = $setupSize
    sha256   = $hash
    built_at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    build_id = $env:AIFORGE_BUILD_ID
    compiler = $iscc
}
$manifest | ConvertTo-Json -Depth 4 | Out-File (Join-Path $Root 'installer\Output\build-manifest.json') -Encoding UTF8
Write-Ok 'Wrote installer\Output\build-manifest.json'
exit 0
