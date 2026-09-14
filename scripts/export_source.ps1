<#
.SYNOPSIS
    AI 3D Forge -- export the developer source package (AI-3D-Forge-Source).

.DESCRIPTION
    Copy everything a developer needs to build the project, excluding:
      - build outputs, caches and virtual environments (build\*, node_modules)
      - the installer output
      - anything that could contain a DeepSeek API key (secrets\, *.local.env)
      - local screenshots and logs

    Produces:
      build\source\AI-3D-Forge-Source\           the tree
      build\source\AI-3D-Forge-Source.zip        zipped, ready to hand over

    IMPORTANT: ASCII-only. Windows PowerShell 5.1 reads a BOM-less .ps1 as the
    system ANSI codepage and non-ASCII bytes can break parsing.

.EXAMPLE
    .\scripts\export_source.ps1
#>

[CmdletBinding()]
param(
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Root 'build\source' }

function Write-Ok   { param([string]$T) Write-Host "  [OK]   $T" -ForegroundColor Green }
function Write-Warn { param([string]$T) Write-Host "  [WARN] $T" -ForegroundColor Yellow }

$Target = Join-Path $OutDir 'AI-3D-Forge-Source'
if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Target | Out-Null

# Directories that never belong in the source package.
$excludeDirs = @(
    'build', 'node_modules', '.git', '.vscode', '.idea',
    '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache'
)
# File patterns that are outputs, caches or secrets.
$excludeFiles = @(
    '*.pyc', '*.pyo', '*.log', '*.tmp', '*.bak',
    'secrets', '.env', '*.local.env', 'config.json', 'credentials.json'
)
# Path fragments that must never ship (defence in depth against leaking a key).
$excludeContains = @(
    '\secrets\', '\build\', '\node_modules\', 'installer\Output',
    'aiforge-edge', 'build-manifest.json'
)

$copied = 0
$skipped = 0
$totalBytes = 0

Get-ChildItem -Path $Root -Recurse -Force -File | ForEach-Object {
    $full = $_.FullName
    $relative = $full.Substring($Root.Length).TrimStart('\')

    $skip = $false
    foreach ($fragment in $excludeContains) {
        if ($full -like "*$fragment*") { $skip = $true; break }
    }
    if (-not $skip) {
        foreach ($dir in $excludeDirs) {
            if ($relative -like "$dir\*" -or $relative -eq $dir) { $skip = $true; break }
        }
    }
    if (-not $skip) {
        foreach ($pattern in $excludeFiles) {
            if ($_.Name -like $pattern) { $skip = $true; break }
        }
    }

    if ($skip) { $skipped++; return }

    $destination = Join-Path $Target $relative
    $destinationDir = Split-Path -Parent $destination
    if (-not (Test-Path $destinationDir)) { New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null }
    Copy-Item $full -Destination $destination -Force
    $copied++
    $script:totalBytes += $_.Length
}

Write-Ok "Copied $copied files ($([math]::Round($totalBytes/1KB,1)) KB); skipped $skipped"

# --- Security gate: refuse to ship anything that looks like a real API key --
# Two patterns:
#   * DeepSeek keys are "sk-" + exactly 32 hex characters -> always fatal.
#   * The masked form ("sk-XXXX****YYYY") is sensitive-ish but a unit test
#     legitimately asserts on it, so it is reported rather than fatal.
# Short fabricated placeholders such as 'sk-mock1234567890abcdef' do not match
# the real shape and are fine.
$realKeyPattern = 'sk-[0-9a-f]{32}'
$maskedPattern = 'sk-[0-9a-zA-Z]{4}\*{4}[0-9a-zA-Z]{4}'
$fatal = @()
$warned = @()

Get-ChildItem $Target -Recurse -Force -File | ForEach-Object {
    if ($_.Length -gt 4MB) { return }
    $relative = $_.FullName.Substring($Target.Length).TrimStart('\')
    try {
        $content = Get-Content $_.FullName -Raw -ErrorAction Stop
    } catch { return }
    if ($content -match $realKeyPattern) { $fatal += $relative }
    elseif ($content -match $maskedPattern) { $warned += $relative }
}

if ($fatal.Count -gt 0) {
    Write-Host ''
    Write-Host '  [FAIL] SECURITY: the source package contains a full API key:' -ForegroundColor Red
    $fatal | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    Write-Host '  Refusing to produce the package. Remove the secret and re-run.' -ForegroundColor Red
    Remove-Item $Target -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}
if ($warned.Count -gt 0) {
    Write-Warn "Masked-key literals present (expected in unit tests): $($warned -join ', ')"
}
Write-Ok 'Security check passed: no full API key in the package'

# --- Sanity check: the package must actually be buildable -------------------
# NOTE: this script is ASCII-only, so the Chinese-named docs are asserted with a
# wildcard instead of a literal filename. A literal Chinese filename here would
# be mangled by Windows PowerShell 5.1's ANSI decoding and the check would fail
# spuriously (this script had exactly that bug).
$required = @(
    'main.py', 'requirements.txt', 'requirements-build.txt',
    'AI-3D-Forge.spec', 'README.md',
    'src\aiforge\app.py', 'src\aiforge\jobs.py', 'src\aiforge\openscad.py',
    'webui\package.json', 'webui\src\App.tsx',
    'installer\AI-3D-Forge.iss',
    'docs\API-CONTRACT.md', 'docs\OPENSCAD-SOURCES.md', 'docs\LICENSE.txt',
    'scripts\build.ps1', 'scripts\build_installer.ps1', 'scripts\selfcheck.py',
    'scripts\e2e_test.py', 'scripts\test_install.ps1', 'scripts\export_source.ps1'
)
$missing = @()
foreach ($item in $required) {
    if (-not (Test-Path (Join-Path $Target $item))) { $missing += $item }
}

# The two Chinese-titled guides: README-<chinese>.md (user and developer)
$readmeDocs = @(Get-ChildItem (Join-Path $Target 'README-*.md') -ErrorAction SilentlyContinue)
if ($readmeDocs.Count -lt 2) {
    $missing += 'README-<chinese>.md (expected 2: user guide + developer guide)'
}

if ($missing.Count -gt 0) {
    Write-Host ''
    Write-Host "  [FAIL] Package is incomplete; missing: $($missing -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Ok "Package completeness check passed ($($required.Count) files + $($readmeDocs.Count) localized guides)"

# --- Zip it ----------------------------------------------------------------
$zipPath = Join-Path $OutDir 'AI-3D-Forge-Source.zip'
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $Target '*') -DestinationPath $zipPath -CompressionLevel Optimal
$zipMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
$hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash

Write-Host ''
Write-Ok "Source package: $zipPath ($zipMb MB)"
Write-Host "  Directory : $Target" -ForegroundColor White
Write-Host "  SHA-256   : $hash" -ForegroundColor DarkGray
exit 0
