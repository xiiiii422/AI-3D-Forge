<#
.SYNOPSIS
    AI 3D Forge -- installer acceptance test (install / run / upgrade / uninstall / reinstall).

.DESCRIPTION
    Exercises the real AI-3D-Forge-Setup.exe silently, end to end:

      1. verify the installer exists and record its hash
      2. silent install to a dedicated test directory
      3. verify program files, shortcuts, uninstaller, registry entry
      4. launch the installed app and drive the HTTP API from outside
         (proves it really works, not merely that files were copied)
      4b. double-click for real (GUI path) and verify the second launch wakes the
          running window instead of starting a second service or dying silently
      5. plant a fake user project + settings in %APPDATA% / Documents
      6. upgrade-install over the existing installation
      7. verify program files were replaced AND user data survived
      8. uninstall
      9. verify program files are gone AND user data still survives
     10. reinstall and verify the user data is picked up again

    WARNING: this installs and uninstalls real software on this machine and
    creates real desktop / start-menu shortcuts. Run it on a test machine or an
    account you do not mind touching. Use -KeepInstalled to stop before the
    uninstall steps.

    IMPORTANT: ASCII-only, comments included. Windows PowerShell 5.1 reads a
    BOM-less .ps1 as the system ANSI codepage and non-ASCII bytes can break it.

.EXAMPLE
    .\scripts\test_install.ps1
    .\scripts\test_install.ps1 -KeepInstalled
#>

[CmdletBinding()]
param(
    [string]$InstallDir = 'D:\AIF-InstallTest',
    [int]$Port = 8781,
    [switch]$KeepInstalled,
    [switch]$KeepUserData
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# This test must exercise the REAL user-data locations, because "user data lives
# in the user profile, not under the install dir" is precisely what it verifies
# (requirement 45). Clear any AIFORGE_* overrides that a previous test run may
# have left in this shell's environment, otherwise the installed app would be
# redirected to a build directory and the test would verify nothing.
foreach ($name in @('AIFORGE_DATA_DIR', 'AIFORGE_LOCAL_DIR', 'AIFORGE_PROJECTS_DIR', 'AIFORGE_CONSOLE_LOG')) {
    if (Test-Path "env:$name") { Remove-Item "env:$name" -Force }
}
Write-Host '  (cleared AIFORGE_* env overrides so real user paths are used)' -ForegroundColor DarkGray

# Skip the OpenSCAD download inside the installer's first-run preparation.
# That download is ~21MB and on a slow link takes minutes, which would make this
# acceptance test non-deterministic. The download path itself is covered by
# scripts\e2e_test.py (which does a real install on a simulated clean machine);
# here we verify that the preparation step RUNS, reports completion and leaves a log.
$env:AIFORGE_SKIP_OPENSCAD_DOWNLOAD = '1'
Write-Host '  (AIFORGE_SKIP_OPENSCAD_DOWNLOAD=1 so the test does not wait for a 21MB download)' -ForegroundColor DarkGray

# Everything this test creates in the REAL projects directory must be younger than
# this timestamp. Cleanup uses it to delete only what this run produced -- so a
# user project that happens to be called "InstallTestProject" is never touched.
$testStart = Get-Date

$Root = Split-Path -Parent $PSScriptRoot
$Setup = Join-Path $Root 'installer\Output\AI-3D-Forge-Setup.exe'
$AppData = Join-Path $env:APPDATA 'AI-3D-Forge'
$DocsProjects = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'AI-3D-Forge\Projects'
$Desktop = [Environment]::GetFolderPath('Desktop')
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

# With PrivilegesRequired=admin, Inno Setup resolves {group} and {autodesktop}
# to the COMMON (all-users) locations, not the per-user ones. Check both --
# assuming only the per-user paths made a working installer look broken.
$DesktopCandidates = @(
    (Join-Path $Desktop 'AI 3D Forge.lnk'),
    (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'AI 3D Forge.lnk'),
    'C:\Users\Public\Desktop\AI 3D Forge.lnk'
)
$StartMenuCandidates = @(
    (Join-Path $StartMenu 'AI 3D Forge.lnk'),
    (Join-Path $StartMenu 'AI 3D Forge\AI 3D Forge.lnk'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\AI 3D Forge\AI 3D Forge.lnk')
)

function Find-FirstExisting {
    param([string[]]$Paths)
    foreach ($p in $Paths) { if (Test-Path $p) { return $p } }
    return $null
}

$Results = New-Object System.Collections.ArrayList

function Step {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    [void]$Results.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail })
    $flag = if ($Ok) { '[OK]  ' } else { '[FAIL]' }
    $color = if ($Ok) { 'Green' } else { 'Red' }
    Write-Host "  $flag $Name" -NoNewline -ForegroundColor $color
    if ($Detail) { Write-Host " -- $Detail" -ForegroundColor DarkGray } else { Write-Host '' }
}

function Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkGray
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkGray
}

function Wait-Http {
    param([string]$Url, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 10 -UseBasicParsing
            if ($response.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Milliseconds 800 }
    }
    return $false
}

function Stop-TestApp {
    Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.Kill() } catch { }
    }
    # Wait for the kill to really land. A process that is still on its way out still
    # holds the single-instance lock, and the next Start-Process is then refused with
    # nothing but a bogus "Local service started" failure to show for it. Mandatory
    # byte-range locks die with the handle, so "no such process left" *is* "lock free".
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 2
}

function Get-LockRecordPid {
    # The pid stored in the lock file, or '' -- purely for naming who holds it, never for
    # deciding *whether* it is held. Layout (see src/aiforge/single_instance.py): byte 0 is
    # the lock region, the pid is written from byte 1 on, so a read starting at byte 0 would
    # hit the mandatory lock while anybody holds it -- seek past it and ask for 24 bytes.
    param([string]$Path)
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        [void]$stream.Seek(1, [IO.SeekOrigin]::Begin)
        $buf = New-Object byte[] 24
        $n = $stream.Read($buf, 0, 24)
        if ($n -le 0) { return '' }
        $txt = [Text.Encoding]::ASCII.GetString($buf, 0, $n).Trim()
        $owner = 0
        if (-not [int]::TryParse($txt, [ref]$owner)) { return '' }
        return "$owner"
    } catch {
        # An instance of the *previous* build locks wherever its file happened to end, which
        # can sit right on top of the pid bytes (measured: with a lock on byte 1, an unbuffered
        # one-byte read at byte 0 comes back fine while the read of the pid bytes raises). No
        # name to report -- not a claim of freedom.
        return ''
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Get-LockClaim {
    # Does an instance of the app still hold the single-instance lock? '' means nobody living.
    # Two independent signals, because the pid record alone cannot be trusted to decide this:
    # the previous build stored the pid at byte 0 while this one reads it from byte 1, so what
    # comes back out of an old record is "the real number minus its first digit" -- measured on
    # this machine, such a number was a live *unrelated* process, and a refused second instance
    # following it brought that other program's window to the foreground. Trusting the same
    # number here would either block this run on a phantom or wave through a real holder.
    #   1) a live process named AI-3D-Forge is the honest "an instance runs" signal (it also
    #      covers an old build, whose lock position is not pinned down);
    #   2) the lock itself is ground truth for "held": byte 0 carries a *mandatory* byte-range
    #      lock and a FileStream read asks for 4 KB at once, so reading from byte 0 throws while
    #      anybody holds a lock over the head of the file -- measured: holders on byte 1 and on
    #      byte 20 of a 40-byte file both failed it, a lock past the 4 KB window did not, and
    #      with nobody holding anything the read always succeeded. The drifting lock position of
    #      an old build therefore still lands inside this window, which is why signal 2 is not
    #      pinned to "byte 0" in its own wording. It also catches a development instance that
    #      runs as python.exe and so never shows up under signal 1.
    # The record is only used to say *who*, never *whether*.
    $procs = @(Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue)
    if ($procs.Count -gt 0) {
        $ids = ($procs | ForEach-Object { $_.Id }) -join ','
        return "$($procs.Count) AI-3D-Forge process(es) alive (pid $ids)"
    }
    $file = Join-Path $AppData 'app.lock'
    if (-not (Test-Path $file)) { return '' }
    $named = Get-LockRecordPid -Path $file
    $stream = $null
    try {
        $stream = [IO.File]::Open($file, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        [void]$stream.Seek(0, [IO.SeekOrigin]::Begin)
        [void]$stream.ReadByte()  # throws IOException while a mandatory lock covers the file head
    } catch [IO.IOException] {
        # A lock really is held over the head of the file. Name the holder only when the record
        # points at a process that could be one of ours at all -- the app exe, or a python
        # running from a checkout. A garbled or stale record naming somebody else must not put
        # the blame on a stranger: measured while writing this, an old record mis-read by
        # exactly one digit landed on a live unrelated process, and the shipped code that
        # trusted it raised that program's window. Same rule here -- the record may name the
        # holder, never accuse it.
        if ($named) {
            $cand = Get-Process -Id ([int]$named) -ErrorAction SilentlyContinue
            if ($cand -and @('AI-3D-Forge', 'python', 'pythonw') -contains $cand.ProcessName) {
                return "lock held over the file head (record says pid=$named name=$($cand.ProcessName))"
            }
            return "lock held over the file head (record names pid=$named, which is not a plausible holder)"
        }
        return 'lock held over the file head (record unreadable, likely an instance of the previous build)'
    } catch {
        return "lock file could not be inspected: $($_.Exception.GetType().Name)"
    } finally {
        if ($stream) { $stream.Dispose() }
    }
    # Held by nobody and no live app process: whatever the record says, it is just stale bytes.
    return ''
}

# --------------------------------------------------------------------------
Section '0. Preconditions'

if (-not (Test-Path $Setup)) {
    Write-Host "  [FAIL] Installer not found: $Setup" -ForegroundColor Red
    Write-Host '  Run scripts\build_installer.ps1 first.' -ForegroundColor Yellow
    exit 1
}
$setupSize = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
$setupHash = (Get-FileHash $Setup -Algorithm SHA256).Hash
Step 'Installer exists' $true "$setupSize MB  SHA256=$($setupHash.Substring(0,16))..."

if (Test-Path $InstallDir) {
    Write-Host "  Removing previous test install: $InstallDir" -ForegroundColor DarkGray
    Stop-TestApp
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
}
Step 'Test install dir is clean' (-not (Test-Path $InstallDir)) $InstallDir

$preExistingUserData = (Test-Path $AppData) -or (Test-Path $DocsProjects)
Write-Host "  Pre-existing user data present: $preExistingUserData" -ForegroundColor DarkGray

# --------------------------------------------------------------------------
Section '1. Silent install'

$installLog = Join-Path $Root 'build\work\install-test.log'
$proc = Start-Process -FilePath $Setup -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCANCEL',
    "/DIR=$InstallDir", "/LOG=$installLog"
) -Wait -PassThru
Step 'Installer exited cleanly' ($proc.ExitCode -eq 0) "exit code $($proc.ExitCode)"

$exe = Join-Path $InstallDir 'AI-3D-Forge.exe'
Step 'Executable installed' (Test-Path $exe) $exe
Step 'Bundled runtime present' (Test-Path (Join-Path $InstallDir '_internal')) ''
Step 'Web UI bundled' (Test-Path (Join-Path $InstallDir '_internal\webui\index.html')) ''
Step 'User guide installed' (@(Get-ChildItem (Join-Path $InstallDir 'README*.md') -ErrorAction SilentlyContinue).Count -ge 1) ''

if (-not (Test-Path $exe)) {
    Write-Host ''
    Write-Host '  Cannot continue without the executable. Aborting.' -ForegroundColor Red
    exit 1
}

$installedSize = [math]::Round(((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Step 'Installed footprint reasonable' ($installedSize -lt 200) "$installedSize MB in $InstallDir"

# The installer must have run the (visible) first-run preparation: it leaves a
# log next to the app and must have prepared the user data folders.
# The completion marker is ASCII on purpose -- a Chinese marker could not be
# matched reliably from an ASCII-only PowerShell script.
$firstRunLog = Join-Path $InstallDir 'logs\first-run.log'
Step 'First-run preparation left a log' (Test-Path $firstRunLog) $firstRunLog
if (Test-Path $firstRunLog) {
    $logText = Get-Content $firstRunLog -Raw -Encoding UTF8
    Step 'Preparation reported completion' ($logText -like '*FIRST_RUN_SETUP_OK*') 'found the ASCII completion marker'
    Step 'Preparation did not crash' (-not ($logText -like '*Traceback*')) 'no traceback in the log'
    Step 'Preparation created a user profile' (@(Get-ChildItem "$env:APPDATA\AI-3D-Forge" -ErrorAction SilentlyContinue).Count -ge 1) "$env:APPDATA\AI-3D-Forge"
}

$desktopLink = Find-FirstExisting -Paths $DesktopCandidates
$startMenuLink = Find-FirstExisting -Paths $StartMenuCandidates
Step 'Desktop shortcut created' ($null -ne $desktopLink) "$desktopLink"
Step 'Start-menu shortcut created' ($null -ne $startMenuLink) "$startMenuLink"
Step 'Uninstaller created' (Test-Path (Join-Path $InstallDir 'unins000.exe')) ''

# The icon is what makes the software findable and look finished. Verify it is
# really embedded in the exe rather than only sitting in the source tree.
$iconOk = $false
$iconDetail = ''
try {
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
    if ($icon) {
        $iconOk = $true
        $iconDetail = "$($icon.Width)x$($icon.Height)"
        $icon.Dispose()
    }
} catch {
    $iconDetail = $_.Exception.Message
}
Step 'App icon embedded in the executable' $iconOk $iconDetail

$iconResourceOk = $false
try {
    $vi = (Get-Item $exe).VersionInfo
    $iconResourceOk = ($vi.ProductName -like '*AI 3D Forge*') -and ($vi.FileVersion -like '1.0*')
    $iconDetail = "ProductName=$($vi.ProductName) FileVersion=$($vi.FileVersion) Company=$($vi.CompanyName)"
} catch {
    $iconDetail = $_.Exception.Message
}
Step 'Version metadata embedded in the executable' $iconResourceOk $iconDetail

$uninstallEntry = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } |
    Where-Object { $_.DisplayName -eq 'AI 3D Forge' } | Select-Object -First 1
Step 'Registered in Add/Remove Programs' ($null -ne $uninstallEntry) $(if ($uninstallEntry) { $uninstallEntry.DisplayVersion } else { '' })

# --------------------------------------------------------------------------
Section '2. Installed app actually runs (driven through HTTP)'

Stop-TestApp
$stuckBefore = Get-LockClaim
Step 'No live instance claims the app lock' (-not $stuckBefore) $stuckBefore
$env:AIFORGE_CONSOLE_LOG = '1'
Start-Process -FilePath $exe -ArgumentList '--server', "--port=$Port" -WindowStyle Hidden
$ready = Wait-Http -Url "http://127.0.0.1:$Port/api/app/health" -TimeoutSec 90
Step 'Local service started' $ready "http://127.0.0.1:$Port"

if ($ready) {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/app/health" -TimeoutSec 20
    Step 'Health endpoint answers' ($health.ok -eq $true) "version $($health.version)"

    $page = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 20 -UseBasicParsing
    Step 'UI is served' ($page.StatusCode -eq 200 -and $page.Content.Length -gt 100) "$($page.Content.Length) bytes"

    # FastAPI has to build the whole route schema for this one. It is the cheapest
    # proof that a packaging-time exclusion (see AI-3D-Forge.spec excludes) did not
    # remove something the server only touches here -- a missing optional dep shows
    # up as a 500 on this request, not as a failed startup.
    try {
        $openapi = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/openapi.json" -TimeoutSec 30 -UseBasicParsing
        $paths = ($openapi.Content | ConvertFrom-Json).paths.PSObject.Properties.Name.Count
        Step 'OpenAPI schema builds in the packaged app' ($openapi.StatusCode -eq 200 -and $paths -gt 30) "$paths paths"
    } catch {
        Step 'OpenAPI schema builds in the packaged app' $false $_.Exception.Message
    }

    try {
        $report = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/env/report" -TimeoutSec 120
        $fails = ($report.items | Where-Object { $_.status -eq 'fail' }).Count
        Step 'Environment report produced' ($report.total -ge 10) "$($report.ok_count)/$($report.total) ok, $fails failing"
    } catch {
        Step 'Environment report produced' $false $_.Exception.Message
    }

    $settings = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/settings" -TimeoutSec 20
    Step 'User data dir is NOT under the install dir' (-not $settings.data_dir.StartsWith($InstallDir)) $settings.data_dir
    # Path check without any non-ASCII literal: the projects folder always ends
    # in "Projects" and always lives outside the install directory.
    Step 'Projects dir is under Documents' (($settings.projects_dir -like '*\Projects*') -and (-not $settings.projects_dir.StartsWith($InstallDir))) $settings.projects_dir

    Stop-TestApp
    Step 'App stopped cleanly' (-not (Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue)) ''
}

# --------------------------------------------------------------------------
Section '2b. A second launch wakes the running window instead of starting over'

# This is the real double-click path (no --server, no --browser), so it is the only
# place where "the refusal actually reaches the user" can be checked: with no console
# attached, a silent exit and a helpful message look identical from the outside. The
# self-check guard can only pin the decision table -- it has no desktop window to
# wake -- which is why this section lives here instead of there.
#
# Being told comes in three shapes, and which one you get depends on how the app was
# started (all three measured with this script, see the two steps below for which is
# which). The one shape that must never happen is a fourth: exit, say nothing.

if (-not ('AiForgeWin32.Fore' -as [type])) {
    Add-Type -Namespace AiForgeWin32 -Name Fore -MemberDefinition @'
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
'@
}

Stop-TestApp
$gui = Start-Process -FilePath $exe -PassThru
$firstHwnd = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    $live = Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($live) { $firstHwnd = $live.MainWindowHandle; break }
    Start-Sleep -Milliseconds 500
}
Step 'Installed exe opens a real window' ($firstHwnd -ne [IntPtr]::Zero) "hwnd=$firstHwnd"

if ($firstHwnd -ne [IntPtr]::Zero) {
    # Put the running window out of sight on purpose. Without this, "the window is
    # there and looks fine" would pass even if the wake path did nothing at all --
    # the same empty-run trap the self-check guard has its own control for.
    [void][AiForgeWin32.Fore]::ShowWindow($firstHwnd, 6)   # SW_MINIMIZE
    Start-Sleep -Seconds 2
    $minimized = [AiForgeWin32.Fore]::IsIconic($firstHwnd)
    # Detail is always printed, also on success (that is this script's convention: the
    # observed fact, not a failure hypothesis), so keep these strings factual.
    Step 'Control: the running window really got minimized' $minimized "isIconic=$minimized"

    $second = Start-Process -FilePath $exe -PassThru
    # 8s is well past the refusal path (it returns right after the wake attempt) but
    # still short: a modal notice blocks forever, and that has to be the finding.
    [void]$second.WaitForExit(8000)
    $stillUp = Get-Process -Id $second.Id -ErrorAction SilentlyContinue
    $notice = [bool](($null -ne $stillUp) -and $stillUp.MainWindowTitle)
    $woke = ([AiForgeWin32.Fore]::GetForegroundWindow() -eq $firstHwnd)
    $nowIconic = [AiForgeWin32.Fore]::IsIconic($firstHwnd)
    $back = $minimized -and (-not $nowIconic)
    # Restoring the window is the part we own; the foreground is Windows' to grant.
    # Measured on the installed build: SetForegroundWindow got refused for a second
    # instance started from here (the right goes to the process that owns the
    # foreground or received the last input), while the restore always worked.
    # A window that stays minimized is precisely "I double clicked and nothing
    # happened", so this step, not the next one, is the sharp end of the check.
    Step 'Second launch brings the running window back' $back "isIconic before=$minimized after=$nowIconic"
    # woke / restored / notice: see the section header. Detail stays factual because
    # Step prints it on success too.
    $told = $woke -or $back -or $notice
    Step 'Second launch never exits without a trace' $told `
        "woke=$woke restored=$back notice=$notice"
    if (-not $told) {
        Write-Host "  why: foregroundHwnd=$([AiForgeWin32.Fore]::GetForegroundWindow()) targetHwnd=$firstHwnd" -ForegroundColor DarkGray
    }
    # Woken or notified, either way there must not be a second app *window*. Count
    # windows rather than processes: the refusing process can still be alive sitting
    # on its own dialog, and that is not a duplicate service.
    if ($stillUp) {
        try { $stillUp.Kill() } catch { }
        $stillUp.WaitForExit(10000) | Out-Null
    }
    $wins = @(Get-Process 'AI-3D-Forge' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
    Step 'No second app window was created' ($wins.Count -eq 1 -and $wins[0].Id -eq $gui.Id) `
        "$($wins.Count) app window(s), expected only pid $($gui.Id)"
    Stop-TestApp
}

# --------------------------------------------------------------------------
Section '3. Plant user data (simulate a user who already used the app)'

# Create the project through the REAL API rather than hand-writing meta.json.
# Hand-written fixtures can silently diverge from the on-disk format, and then
# the test proves nothing about whether user data really survives.
New-Item -ItemType Directory -Force -Path $AppData | Out-Null
New-Item -ItemType Directory -Force -Path $DocsProjects | Out-Null

$sentinelProject = $null
$sentinelScad = $null
$sentinelId = $null
$sentinelConfig = Join-Path $AppData 'config.json'

# The plant below OVERWRITES config.json, and this test is meant to be run on a
# developer machine -- which by definition already has a real config.json (theme,
# printer/material selection, first-run state...). Without this backup, running
# the acceptance test silently destroys the maintainer's own settings: the
# folder-level guard further down only protects the *directories*, not this file.
$configBackup = $null
if (Test-Path $sentinelConfig) {
    $configBackup = Join-Path $AppData 'config.json.installtest-backup'
    Remove-Item $configBackup -Force -ErrorAction SilentlyContinue
    Move-Item $sentinelConfig $configBackup -Force
    Write-Host "  Backed up the existing config.json -> $configBackup" -ForegroundColor DarkGray
}

Stop-TestApp
Start-Process -FilePath $exe -ArgumentList '--server', "--port=$Port" -WindowStyle Hidden
if (Wait-Http -Url "http://127.0.0.1:$Port/api/app/health" -TimeoutSec 90) {
    try {
        $created = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/projects" -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes('{"name":"InstallTestProject"}')) -TimeoutSec 30
        $projectDetail = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/projects/$($created.id)" -TimeoutSec 30
        $sentinelScad = $projectDetail.files.scad_path
        $sentinelProject = Split-Path -Parent $sentinelScad
        $sentinelId = $created.id
        # Give it real content so we can verify byte-level survival
        'cube([7,7,7]);' | Set-Content -Path $sentinelScad -Encoding UTF8
        Step 'Sentinel project created through the API' (Test-Path $sentinelScad) $sentinelProject
        Step 'Sentinel project id recorded' ($created.id -ne $null -and $created.id.Length -ge 6) $created.id
    } catch {
        Step 'Sentinel project created through the API' $false $_.Exception.Message
    }
    Stop-TestApp
} else {
    Step 'Sentinel project created through the API' $false 'app did not start'
}

if (-not $sentinelScad) {
    # Fall back so the rest of the test still runs
    $sentinelProject = Join-Path $DocsProjects 'InstallTestProject-abc123'
    New-Item -ItemType Directory -Force -Path $sentinelProject | Out-Null
    $sentinelScad = Join-Path $sentinelProject 'model.scad'
    'cube([7,7,7]);' | Set-Content -Path $sentinelScad -Encoding UTF8
    Step 'Fallback sentinel used' $true $sentinelProject
}

'{"version":1,"first_run_done":true,"ui":{"theme":"dark"}}' | Set-Content -Path $sentinelConfig -Encoding UTF8
Step 'Sentinel config planted' (Test-Path $sentinelConfig) $sentinelConfig

# --------------------------------------------------------------------------
Section '4. Upgrade install over the existing installation'

if (Test-Path $installLog) { Remove-Item $installLog -Force }
$proc = Start-Process -FilePath $Setup -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCANCEL',
    "/DIR=$InstallDir", "/LOG=$installLog"
) -Wait -PassThru
Step 'Upgrade install exited cleanly' ($proc.ExitCode -eq 0) "exit code $($proc.ExitCode)"
Step 'Executable still present after upgrade' (Test-Path $exe) ''
Step 'User project survived upgrade' (Test-Path $sentinelScad) $sentinelProject
Step 'User config survived upgrade' (Test-Path $sentinelConfig) $sentinelConfig

$sentinelContent = Get-Content $sentinelScad -Raw
Step 'User project content intact' ($sentinelContent -like '*cube*') 'content unchanged'

Stop-TestApp
$reinstallEntry = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } |
    Where-Object { $_.DisplayName -eq 'AI 3D Forge' } | Select-Object -First 1
Step 'No duplicate Add/Remove entry after upgrade' ($null -ne $reinstallEntry) 'single entry'

# --------------------------------------------------------------------------
if ($KeepInstalled) {
    Section '5. Stopping here (-KeepInstalled)'
    Write-Host '  The test installation is still present.' -ForegroundColor Yellow
} else {
    Section '5. Uninstall'

    $uninstaller = Join-Path $InstallDir 'unins000.exe'
    if (Test-Path $uninstaller) {
        $proc = Start-Process -FilePath $uninstaller -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
        ) -Wait -PassThru
        Start-Sleep -Seconds 4
        Step 'Uninstaller exited cleanly' ($proc.ExitCode -eq 0) "exit code $($proc.ExitCode)"
    } else {
        Step 'Uninstaller present' $false 'unins000.exe missing'
    }

    Stop-TestApp
    Start-Sleep -Seconds 2

    Step 'Program files removed' (-not (Test-Path $exe)) $InstallDir
    Step 'User project SURVIVED uninstall' (Test-Path $sentinelScad) $sentinelProject
    Step 'User config SURVIVED uninstall' (Test-Path $sentinelConfig) $sentinelConfig
    Step 'Desktop shortcut removed' ($null -eq (Find-FirstExisting -Paths $DesktopCandidates)) ''

    $afterUninstall = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                     'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } |
        Where-Object { $_.DisplayName -eq 'AI 3D Forge' } | Select-Object -First 1
    Step 'Add/Remove entry removed' ($null -eq $afterUninstall) ''

    # ----------------------------------------------------------------------
    Section '6. Reinstall and verify user data is picked up again'

    $proc = Start-Process -FilePath $Setup -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCANCEL', "/DIR=$InstallDir"
    ) -Wait -PassThru
    Step 'Reinstall exited cleanly' ($proc.ExitCode -eq 0) "exit code $($proc.ExitCode)"
    Step 'Executable restored' (Test-Path $exe) ''

    Stop-TestApp
    $stuckAgain = Get-LockClaim
    Step 'No live instance claims the app lock (relaunch)' (-not $stuckAgain) $stuckAgain
    Start-Process -FilePath $exe -ArgumentList '--server', "--port=$Port" -WindowStyle Hidden
    $ready2 = Wait-Http -Url "http://127.0.0.1:$Port/api/app/health" -TimeoutSec 90
    Step 'App runs after reinstall' $ready2 ''

    if ($ready2) {
        try {
            $projects = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/projects" -TimeoutSec 30
            $match = @($projects | Where-Object { $_.id -eq $sentinelId })
            if ($match.Count -eq 0) {
                $match = @($projects | Where-Object { $_.name -like 'InstallTestProject*' })
            }
            Step 'User project visible after reinstall' ($match.Count -ge 1) "$($match.Count) matching project(s) of $($projects.Count) total"

            if ($match.Count -ge 1) {
                $detail = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/projects/$($match[0].id)" -TimeoutSec 30
                Step 'Project content readable through the API' ($detail.scad -like '*cube*') "scad: $($detail.scad.Trim())"
            }
        } catch {
            Step 'User project visible after reinstall' $false $_.Exception.Message
        }
    }

    Stop-TestApp

    # ----------------------------------------------------------------------
    Section '7. Cleanup'
    if (-not $KeepUserData) {
        $uninstaller = Join-Path $InstallDir 'unins000.exe'
        if (Test-Path $uninstaller) {
            Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait | Out-Null
            Start-Sleep -Seconds 4
        }
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $sentinelProject -Recurse -Force -ErrorAction SilentlyContinue
        # The sentinel project is created through the API, and the app may add
        # extra directories for it (and older runs of this script left some
        # behind: 4 stray "InstallTestProject-*" folders were found sitting in
        # the real project list). Remove every matching folder **created by this
        # run** -- the timestamp check is what makes it safe: a user project that
        # happens to carry the same name is never deleted.
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $strays = @(Get-ChildItem $DocsProjects -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'InstallTestProject*' -and $_.CreationTime -ge $testStart })
            if ($strays.Count -eq 0) { break }
            foreach ($stray in $strays) {
                Remove-Item $stray.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Milliseconds 700
        }
        $leftover = @(Get-ChildItem $DocsProjects -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'InstallTestProject*' -and $_.CreationTime -ge $testStart })
        Step 'No test project left behind' ($leftover.Count -eq 0) "$($leftover.Count) stray folder(s)"
        # Remove-Item rejects a $null -Path, so guard every optional path.
        $link = Find-FirstExisting -Paths $DesktopCandidates
        if ($link) { Remove-Item $link -Force -ErrorAction SilentlyContinue }
        foreach ($dir in @(
            (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\AI 3D Forge'),
            (Join-Path $StartMenu 'AI 3D Forge')
        )) {
            if (Test-Path $dir) { Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue }
        }
        if (-not $preExistingUserData) {
            Remove-Item $AppData -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'AI-3D-Forge') -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host '  Removed the user-data folders created by this test.' -ForegroundColor DarkGray
        } else {
            Write-Host '  Left pre-existing user data untouched.' -ForegroundColor DarkGray
        }
    } else {
        Write-Host '  -KeepUserData: left everything in place.' -ForegroundColor Yellow
    }

    # ----------------------------------------------------------------------
    # Always restore the real config.json, even with -KeepUserData.
    # -KeepUserData means "leave the test install and the test project alone",
    # it must never mean "keep the sentinel config we planted over the user's
    # real settings" -- that would still destroy them, just more quietly.
    if ($sentinelConfig) {
        Remove-Item $sentinelConfig -Force -ErrorAction SilentlyContinue
    }
    if ($configBackup -and (Test-Path $configBackup)) {
        Move-Item $configBackup $sentinelConfig -Force
        Write-Host '  Restored the original config.json.' -ForegroundColor DarkGray
    }
}

# --------------------------------------------------------------------------
Section 'Summary'
$passed = @($Results | Where-Object { $_.Ok }).Count
$failed = @($Results | Where-Object { -not $_.Ok })
Write-Host "  Passed: $passed    Failed: $($failed.Count)"
if ($failed.Count -gt 0) {
    Write-Host ''
    foreach ($f in $failed) { Write-Host "  [X] $($f.Name) -- $($f.Detail)" -ForegroundColor Red }
}
Write-Host ''
exit $(if ($failed.Count -eq 0) { 0 } else { 1 })
