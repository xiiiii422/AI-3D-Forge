<#
.SYNOPSIS
    AI 3D Forge -- one-click full build script.

.DESCRIPTION
    Full build pipeline:
      1. check build environment (Python / Node)
      2. install/verify Python dependencies (into build\venv on drive D)
      3. build the frontend (webui/dist)
      4. generate the Windows version resource
      5. backend self-check (optional, aborts on failure)
      6. PyInstaller packaging -> build\dist\AI-3D-Forge\
      7. smoke test the packaged build (--version / --check)
      8. build the installer -> installer\Output\AI-3D-Forge-Setup.exe

    All caches and artifacts live under the project directory (usually drive D)
    so the nearly-full C: drive barely grows.

    ====================================================================
    THIS FILE MUST STAY 100% ASCII -- COMMENTS INCLUDED.
    ====================================================================
    Windows PowerShell 5.1 (which is what this machine actually runs) decodes a
    BOM-less .ps1 using the system ANSI code page, GBK here. A Chinese character
    right before an end-of-line can leave a stray byte that GBK turns into a
    backtick, which PowerShell reads as a line-continuation -- silently merging
    the next line into this one. Statements then vanish and the script simply
    stops partway with no syntax error. That is extremely hard to diagnose, so:
    keep every .ps1 ASCII, and let the Python programs do all Chinese output.
    Run `python scripts\check_ps_encoding.py` to verify.

.EXAMPLE
    .\scripts\build.ps1
    .\scripts\build.ps1 -SkipFrontend -SkipSelfcheck
    .\scripts\build.ps1 -SkipInstaller
#>

[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipSelfcheck,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root 'build\venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$Work = Join-Path $Root 'build\work'
$Dist = Join-Path $Root 'build\dist'
$DownloadCache = Join-Path $Root 'build\downloads'

# Keep every cache inside the project directory.
$env:PIP_CACHE_DIR = Join-Path $Root 'build\cache\pip'
$env:npm_config_cache = Join-Path $Root 'build\cache\npm'
$env:TMP = Join-Path $Root 'build\cache\tmp'
$env:TEMP = $env:TMP
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force -Path $env:TMP, $Work, $Dist, $DownloadCache | Out-Null

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkGray
}
function Write-Ok   { param([string]$Text) Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [WARN] $Text" -ForegroundColor Yellow }
function Write-Err  { param([string]$Text) Write-Host "  [FAIL] $Text" -ForegroundColor Red }

$BuildId = (Get-Date -Format 'yyyyMMdd-HHmmss')
$env:AIFORGE_BUILD_ID = $BuildId

$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
Write-Host ''
Write-Host '  AI 3D Forge -- full build' -ForegroundColor White
Write-Host "  Build ID : $BuildId" -ForegroundColor DarkGray
Write-Host "  Root     : $Root" -ForegroundColor DarkGray

# --------------------------------------------------------------------------
Write-Step '1/8  Checking build environment'

if (-not (Test-Path $Python)) {
    Write-Warn "Virtual environment not found, creating: $Venv"
    $sysPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $sysPython) { Write-Err 'Python not found. Cannot build.'; exit 1 }
    & $sysPython -m venv $Venv --without-pip | Out-Null
    & $Python -m ensurepip --upgrade | Out-Null
}

$pyVersion = (& $Python -V 2>&1) -join ''
Write-Ok "Python: $pyVersion"

$nodeOk = $false
if (-not $SkipFrontend) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $node -or -not $npm) {
        Write-Err 'Node.js / npm not found. Use -SkipFrontend to reuse an existing webui\dist.'
        exit 1
    }
    Write-Ok "Node: $((& node -v))   npm: $((& npm -v))"
    $nodeOk = $true
} else {
    Write-Warn 'Frontend build skipped (reusing webui\dist)'
}

# --------------------------------------------------------------------------
Write-Step '2/8  Installing Python dependencies'

& $Python -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& $Python -m pip install -r (Join-Path $Root 'requirements-build.txt') --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Err 'Dependency installation failed.'; exit 1 }
$pkgCount = (& $Python -m pip list --format=freeze | Measure-Object).Count
Write-Ok "Dependencies ready ($pkgCount packages)"

# --------------------------------------------------------------------------
if ($nodeOk) {
    Write-Step '3/8  Building frontend'
    Push-Location (Join-Path $Root 'webui')
    try {
        if (-not (Test-Path 'node_modules')) {
            Write-Host '  Installing frontend dependencies...' -ForegroundColor DarkGray
            & npm install --no-audit --no-fund 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
        }
        & npm run build 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
    } catch {
        Write-Err $_.Exception.Message
        Pop-Location
        exit 1
    }
    Pop-Location
} else {
    Write-Step '3/8  Building frontend (skipped)'
}

$indexHtml = Join-Path $Root 'webui\dist\index.html'
if (-not (Test-Path $indexHtml)) {
    Write-Err 'Missing webui\dist\index.html. Cannot continue.'
    exit 1
}
$distSize = [math]::Round(((Get-ChildItem (Join-Path $Root 'webui\dist') -Recurse -File | Measure-Object Length -Sum).Sum / 1KB), 1)
Write-Ok "Frontend ready ($distSize KB)"

# --------------------------------------------------------------------------
Write-Step '4/8  Generating version resource and icon'
$verOut = & $Python (Join-Path $Root 'scripts\make_version_info.py') 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { Write-Err 'Failed to generate version info.'; exit 1 }
$verOut | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

# The app icon is used by the exe, the shortcuts, the installer wizard and the
# Add/Remove Programs entry. Without it the software looks unfinished and is
# hard to find among other shortcuts.
$iconOut = & $Python (Join-Path $Root 'scripts\make_icon.py') 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { Write-Err 'Failed to generate the app icon.'; exit 1 }
$iconOut | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

# Guard against the nastiest trap in this repo: a non-ASCII byte inside a .ps1
# comment can be mis-decoded by Windows PowerShell 5.1 into a backtick, which it
# treats as a line-continuation -- the next line gets swallowed and the script
# stops partway with no error at all. Refuse to build if that is present.
$encCheck = & $Python (Join-Path $Root 'scripts\check_ps_encoding.py') 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) {
    Write-Err 'A .ps1 file contains a line that Windows PowerShell 5.1 would mis-parse.'
    $encCheck | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    Write-Host '  Fix: python scripts\check_ps_encoding.py --fix' -ForegroundColor Yellow
    exit 1
}
Write-Ok 'PowerShell scripts are encoding-safe'

# --------------------------------------------------------------------------
if (-not $SkipSelfcheck) {
    Write-Step '5/8  Backend self-check'
    # Capture first, then filter: Windows PowerShell 5.1 turns a native command's
    # stderr into a terminating error while $ErrorActionPreference is 'Stop', and
    # the Python logger writes to stderr.
    $selfcheckOut = & $Python (Join-Path $Root 'scripts\selfcheck.py') 2>&1 | ForEach-Object { "$_" }
    $selfcheckCode = $LASTEXITCODE
    $selfcheckOut | Where-Object { $_ -match '^\s*\[(OK|FAIL)\]|^  ' } |
        ForEach-Object { Write-Host "    $_" }
    if ($selfcheckCode -ne 0) {
        Write-Err "Self-check failed (exit code $selfcheckCode). Build aborted (use -SkipSelfcheck to override)."
        exit 1
    }
    Write-Ok 'Self-check passed'
} else {
    Write-Step '5/8  Backend self-check (skipped)'
}

# --------------------------------------------------------------------------
Write-Step '6/8  Packaging with PyInstaller'
$packageDir = Join-Path $Dist 'AI-3D-Forge'

# A previously launched build keeps native .pyd files locked, which makes
# Remove-Item fail with "Access to the path ... is denied".
$running = Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue
if ($running) {
    Write-Warn "Closing $($running.Count) running AI-3D-Forge process(es) so files can be replaced"
    $running | ForEach-Object { try { $_.Kill() } catch { } }
    Start-Sleep -Seconds 2
}

if (Test-Path $packageDir) {
    $attempt = 0
    while ($attempt -lt 4) {
        try {
            Remove-Item $packageDir -Recurse -Force -ErrorAction Stop
            break
        } catch {
            $attempt++
            if ($attempt -ge 4) {
                Write-Err "Cannot remove $packageDir (file still locked): $($_.Exception.Message)"
                Write-Host '  Close any running AI-3D-Forge window and retry.' -ForegroundColor Yellow
                exit 1
            }
            Write-Warn "Removal attempt $attempt failed; retrying..."
            Start-Sleep -Seconds 2
        }
    }
}

Push-Location $Root
$pyiLog = Join-Path $Work 'pyinstaller.log'
$pyiErrLog = Join-Path $Work 'pyinstaller.err.log'
try {
    # Use Start-Process with native redirection rather than PowerShell pipes:
    # 'native.exe *> file' is not valid redirection syntax and silently never
    # runs the command, leaving a stale $LASTEXITCODE behind.
    $pyiArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', $Dist,
        '--workpath', (Join-Path $Work 'pyinstaller'),
        'AI-3D-Forge.spec'
    )
    $pyiProc = Start-Process -FilePath $Python -ArgumentList $pyiArgs `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $pyiLog -RedirectStandardError $pyiErrLog
    if (Test-Path $pyiLog) {
        Get-Content $pyiLog -Tail 14 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
    if ($pyiProc.ExitCode -ne 0) {
        $tail = @()
        if (Test-Path $pyiLog) { $tail += Get-Content $pyiLog -Tail 20 }
        if (Test-Path $pyiErrLog) { $tail += Get-Content $pyiErrLog -Tail 20 }
        throw "PyInstaller failed with exit code $($pyiProc.ExitCode)`n" + ($tail -join "`n")
    }
} catch {
    Write-Err $_.Exception.Message
    Pop-Location
    exit 1
}
Pop-Location

$exe = Join-Path $packageDir 'AI-3D-Forge.exe'
if (-not (Test-Path $exe)) { Write-Err "Missing executable: $exe"; exit 1 }
$pkgSize = [math]::Round(((Get-ChildItem $packageDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Ok "Packaged: $exe ($pkgSize MB)"

# --------------------------------------------------------------------------
Write-Step '7/8  Smoke test on packaged build'
$env:AIFORGE_DATA_DIR = Join-Path $Work 'packaged-data'
$env:AIFORGE_LOCAL_DIR = Join-Path $Work 'packaged-local'
$env:AIFORGE_PROJECTS_DIR = Join-Path $Work 'packaged-projects'

# Redirect through cmd.exe: keeps the child's stderr out of PowerShell's error
# stream, and the app writes UTF-8 so the file is read back as UTF-8.
$cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
$versionLog = Join-Path $Work 'smoke-version.txt'
& $cmdExe /c "`"$exe`" --version > `"$versionLog`" 2>&1" | Out-Null
$versionOut = Get-Content $versionLog -Encoding UTF8 -ErrorAction SilentlyContinue
if (-not $versionOut) {
    Write-Err '--version produced no output; the packaged build looks broken.'
    exit 1
}
Write-Ok "Executable runs: $(($versionOut | Select-Object -First 1).Trim())"

$checkLog = Join-Path $Work 'smoke-check.txt'
& $cmdExe /c "`"$exe`" --check > `"$checkLog`" 2>&1" | Out-Null
$checkOut = (Get-Content $checkLog -Encoding UTF8 -ErrorAction SilentlyContinue) -join "`n"
$passedCount = ([regex]::Matches($checkOut, [char]0x2713)).Count
$failedCount = ([regex]::Matches($checkOut, [char]0x2717)).Count
$checkOut -split "`n" | Where-Object { $_ -match '^(\u2713|\u26A0|\u2717)' } | ForEach-Object { Write-Host "    $($_.Trim())" -ForegroundColor DarkGray }
Write-Host "    Passed: $passedCount    Failed: $failedCount" -ForegroundColor DarkGray
if ($failedCount -gt 0) {
    Write-Warn 'Some checks failed (expected when no API key is configured or OpenSCAD is absent)'
}
Write-Ok 'Packaged build smoke test done'

# Did the web UI really make it into the package? If not the app shows nothing.
$packedWebui = Join-Path $packageDir '_internal\webui\index.html'
if (-not (Test-Path $packedWebui)) {
    Write-Err 'UI assets were NOT bundled - the app would show no interface.'
    exit 1
}
Write-Ok 'UI assets bundled (works offline)'

# --------------------------------------------------------------------------
if ($SkipInstaller) {
    Write-Step '8/8  Building installer (skipped)'
} else {
    Write-Step '8/8  Building installer'
    $installerScript = Join-Path $Root 'scripts\build_installer.ps1'
    $installerLog = Join-Path $Work 'build_installer.log'
    # Capture to a file, then replay. Piping a child command's stderr straight
    # into the pipeline would trip $ErrorActionPreference='Stop' and abort the
    # build even when the installer compiled fine.
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installerScript *> $installerLog
    $installerCode = $LASTEXITCODE
    if (Test-Path $installerLog) {
        Get-Content $installerLog | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
    if ($installerCode -ne 0) {
        Write-Warn "Installer step exited with code $installerCode. The app itself is ready to run directly."
        Write-Host "    Full log: $installerLog" -ForegroundColor Yellow
    }
}

# --------------------------------------------------------------------------
$Stopwatch.Stop()
Write-Host ''
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host ("  Build finished in {0:N1} minutes" -f $Stopwatch.Elapsed.TotalMinutes) -ForegroundColor Green
Write-Host "  App dir   : $packageDir" -ForegroundColor White
Write-Host "  Executable: $exe" -ForegroundColor White
$setup = Join-Path $Root 'installer\Output\AI-3D-Forge-Setup.exe'
if (Test-Path $setup) {
    $setupSize = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "  Installer : $setup ($setupSize MB)" -ForegroundColor White
}
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host ''
Write-Host 'BUILD_COMPLETE'
exit 0
