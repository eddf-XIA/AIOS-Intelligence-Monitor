<#
    Clean-install acceptance test for the portable AIOS ZIP.

    Simulates the recipient exactly: a machine with no Python, no repository,
    no configuration. Unzip into an empty folder, double-click the launcher,
    expect a working dashboard.
#>
[CmdletBinding()]
param(
    # Defaults to the newest ZIP in release/, i.e. the one just built.
    [string]$ZipPath,
    [string]$TestRoot = (Join-Path $env:TEMP "aios_clean_install_test")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $ZipPath) {
    $ReleaseDir = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "release"
    $newest = Get-ChildItem -Path $ReleaseDir -Filter "*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "No ZIP found in $ReleaseDir - run Build_Release.bat first." }
    $ZipPath = $newest.FullName
}

$script:Pass = 0
$script:Fail = 0

function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = "")
    if ($Condition) {
        $script:Pass++
        Write-Host "  PASS  $Name" -ForegroundColor Green
    } else {
        $script:Fail++
        Write-Host "  FAIL  $Name" -ForegroundColor Red
        if ($Detail) { Write-Host "        $Detail" -ForegroundColor Red }
    }
}

function Info { param([string]$m) Write-Host "        $m" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "CLEAN-INSTALL ACCEPTANCE TEST" -ForegroundColor Cyan
Write-Host "=============================" -ForegroundColor Cyan

# --- 1. a genuinely empty target directory ---------------------------------
Write-Host ""
Write-Host "[1] Extracting into a clean directory" -ForegroundColor Cyan
if (Test-Path $TestRoot) { Remove-Item $TestRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $TestRoot | Out-Null

Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.Drawing
[System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $TestRoot)

$TopLevel = @(Get-ChildItem $TestRoot)
$Installed = Get-ChildItem $TestRoot -Directory | Select-Object -First 1
Check "ZIP unzips into a single named folder (no tarbomb)" `
    (($TopLevel.Count -eq 1) -and ($null -ne $Installed)) `
    "$($TopLevel.Count) top-level entries"
$App = $Installed.FullName
Info $App

# --- 2. the recipient gets no developer data -------------------------------
Write-Host ""
Write-Host "[2] Verifying the package is clean" -ForegroundColor Cyan

$dbs = @(Get-ChildItem $App -Recurse -Force -File -Filter "*.db" -ErrorAction SilentlyContinue)
Check "no database shipped" ($dbs.Count -eq 0) (($dbs | ForEach-Object FullName) -join "; ")

$envs = @(Get-ChildItem $App -Recurse -Force -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*.env" })
Check "no .env / credential file shipped" ($envs.Count -eq 0) (($envs | ForEach-Object FullName) -join "; ")

Check "no tests/ directory shipped in the application" (-not (Test-Path (Join-Path $App "tests")))
Check "no build tooling shipped" (-not (Test-Path (Join-Path $App "tools")))
Check "no reports shipped" (-not (Test-Path (Join-Path $App "data\reports")))

$pycache = @(Get-ChildItem $App -Recurse -Force -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue)
Check "no __pycache__ shipped" ($pycache.Count -eq 0) "$($pycache.Count) found"

# --- 3. everything the recipient needs is present --------------------------
Write-Host ""
Write-Host "[3] Verifying required files" -ForegroundColor Cyan
$Required = @(
    "AIOS Intelligence Monitor.exe",
    "AIOS_Debug_Start.cmd",
    "Create_Desktop_Shortcut.cmd",
    "BUILD_INFO.txt",
    "app.py",
    "AIOS.ico",
    "aios\__init__.py",
    "alembic.ini",
    "runtime\python.exe"
)
foreach ($needed in $Required) {
    Check "present: $needed" (Test-Path (Join-Path $App $needed))
}
# Non-ASCII name, checked by enumeration so console encoding cannot skew it.
$Manual = @(Get-ChildItem $App -File | Where-Object { $_.Extension -eq ".txt" -and $_.Name -ne "BUILD_INFO.txt" })
Check "present: the Chinese end-user instructions (.txt)" ($Manual.Count -ge 1) `
    "found: $(($Manual | ForEach-Object Name) -join ', ')"

# --- the version bump must have reached every place that states it ----------
$RepoVersion = (Select-String -Path (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "aios\__init__.py") `
    -Pattern '^__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$PkgVersion = (Select-String -Path (Join-Path $App "aios\__init__.py") `
    -Pattern '^__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Check "packaged source carries the repository version ($RepoVersion)" `
    ($PkgVersion -eq $RepoVersion) "package says $PkgVersion"
Check "BUILD_INFO.txt states version $RepoVersion" `
    ((Get-Content (Join-Path $App "BUILD_INFO.txt") -Raw) -match [regex]::Escape("Application version : $RepoVersion"))
Check "package folder name carries version $RepoVersion" `
    ($Installed.Name -like "*_v${RepoVersion}_*") "folder: $($Installed.Name)"

$exe = Join-Path $App "AIOS Intelligence Monitor.exe"
$icon = $null
try { $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($exe) } catch { }
Check "launcher carries an embedded icon" ($null -ne $icon)

# --- 4. no interpreter on PATH is required ---------------------------------
Write-Host ""
Write-Host "[4] The package is self-contained" -ForegroundColor Cyan
$embedded = Join-Path $App "runtime\python.exe"
$ver = & $embedded -c "import sys; print(sys.version.split()[0])"
Check "embedded interpreter runs" ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE"
Info "embedded Python $ver"

# Run with the working directory somewhere else entirely, to prove the ._pth -
# not a lucky CWD - is what makes 'import aios' resolve.
Push-Location $env:WINDIR
try {
    $probe = & $embedded (Join-Path $App "app.py") "--help" 2>&1
    $probeExit = $LASTEXITCODE
} finally { Pop-Location }
Check "application resolves its own imports from any working directory" `
    ($probeExit -eq 0) "exit $probeExit : $($probe | Select-Object -Last 3)"

# --- 5. the actual double-click path ---------------------------------------
Write-Host ""
Write-Host "[5] Cold first run via the launcher executable" -ForegroundColor Cyan

$inUse = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Check "port 8765 was free before the test" $false "something is already listening"
    Write-Host ""
    Write-Host "ACCEPTANCE ABORTED - port 8765 busy" -ForegroundColor Red
    exit 1
}
Check "port 8765 was free before the test" $true

$before = Get-Date
$proc = Start-Process -FilePath $exe -WorkingDirectory $App -PassThru
$proc.WaitForExit(180000) | Out-Null
$elapsed = [math]::Round(((Get-Date) - $before).TotalSeconds, 1)

Check "launcher exited 0 (reported success, no error dialog)" `
    ($proc.HasExited -and $proc.ExitCode -eq 0) "exited=$($proc.HasExited) code=$($proc.ExitCode)"
Info "launcher returned in ${elapsed}s (cold start: imports + database creation)"

try {
    # --- 6. the dashboard actually serves ----------------------------------
    Write-Host ""
    Write-Host "[6] Verifying the running application" -ForegroundColor Cyan

    $conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    $serverPid = $null
    if ($conn) { $serverPid = ($conn.OwningProcess | Select-Object -First 1) }
    Check "a server is listening on 127.0.0.1:8765" ($null -ne $serverPid)
    if ($serverPid) {
        $sp = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
        Info "served by PID $serverPid ($($sp.ProcessName))"
        Check "served by the PACKAGED interpreter, not a system Python" `
            ($sp.Path -and $sp.Path.StartsWith($App, [StringComparison]::OrdinalIgnoreCase)) `
            "$($sp.Path)"
    }

    $health = Invoke-WebRequest -Uri "http://127.0.0.1:8765/healthz" -UseBasicParsing -TimeoutSec 15
    Check "/healthz returns HTTP 200" ($health.StatusCode -eq 200) "got $($health.StatusCode)"

    # The running application, not just the files on disk, reports the new version.
    $startupLog = Get-Content (Join-Path $App "data\logs\launcher_console.log") -Raw -ErrorAction SilentlyContinue
    Check "the running application reports v$RepoVersion" `
        ($startupLog -match [regex]::Escape("v$RepoVersion")) `
        "startup log did not mention v$RepoVersion"

    $dash = Invoke-WebRequest -Uri "http://127.0.0.1:8765/" -UseBasicParsing -TimeoutSec 30
    Check "dashboard returns HTTP 200" ($dash.StatusCode -eq 200) "got $($dash.StatusCode)"
    Check "dashboard renders the application HTML" `
        ($dash.Content.Length -gt 1000 -and $dash.Content -match "(?i)<html") `
        "$($dash.Content.Length) bytes"
    Info "dashboard: $($dash.Content.Length) bytes"

    # --- 7. first run created its own data, inside the package -------------
    Write-Host ""
    Write-Host "[7] First-run data lives inside the package" -ForegroundColor Cyan
    $db = Join-Path $App "data\aios.db"
    Check "the application created its own database on first run" (Test-Path $db)
    if (Test-Path $db) { Info "$([math]::Round((Get-Item $db).Length / 1KB, 1)) KB" }
    Check "the launcher wrote a startup log" `
        (Test-Path (Join-Path $App "data\logs\launcher_console.log"))

    # Seeded content is what makes the dashboard usable out of the box: a first
    # run that produced an empty database would still answer 200 on /.
    foreach ($route in @("/monitoring", "/dashboard/status", "/reports", "/settings")) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8765$route" -UseBasicParsing -TimeoutSec 20
            Check "$route returns HTTP 200" ($r.StatusCode -eq 200) "got $($r.StatusCode)"
        } catch {
            Check "$route returns HTTP 200" $false $_.Exception.Message
        }
    }

    # The seeded rows must be real, read back out of the database the packaged
    # application just created for itself. A first run that produced an empty
    # database would still answer 200 on every route above.
    $probeSql = @"
import sqlite3
c = sqlite3.connect(r'$db')
for t in ('monitor_modules', 'topics', 'search_queries', 'app_settings', 'alembic_version'):
    try:
        print(t, c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0])
    except Exception:
        print(t, -1)
"@
    $rows = @(& $embedded -c $probeSql)
    Check "the schema migrated and seeded without error" ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE"
    $counts = @{}
    foreach ($line in $rows) {
        $parts = $line.Trim() -split '\s+'
        if ($parts.Count -eq 2) { $counts[$parts[0]] = [int]$parts[1] }
    }
    # 8 is what the startup banner promises the user.
    Check "first run seeded exactly 8 monitor modules" ($counts['monitor_modules'] -eq 8) `
        "monitor_modules = $($counts['monitor_modules'])"
    Check "first run seeded topics and search queries" `
        ($counts['topics'] -ge 1 -and $counts['search_queries'] -ge 1) `
        "topics=$($counts['topics']) search_queries=$($counts['search_queries'])"
    Check "application settings were written" ($counts['app_settings'] -ge 1) `
        "app_settings = $($counts['app_settings'])"
    Check "alembic stamped the schema version" ($counts['alembic_version'] -ge 1) `
        "alembic_version = $($counts['alembic_version'])"
    Info ("seeded: " + (($counts.GetEnumerator() | Sort-Object Name |
        ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ", "))

    # --- 8. a second double-click must not start a second server ----------
    Write-Host ""
    Write-Host "[8] Double-clicking again reuses the running server" -ForegroundColor Cyan
    $second = Start-Process -FilePath $exe -WorkingDirectory $App -PassThru
    $second.WaitForExit(60000) | Out-Null
    Check "second launch exited 0" ($second.HasExited -and $second.ExitCode -eq 0) `
        "code=$($second.ExitCode)"
    $conn2 = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    $pids2 = @($conn2 | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique)
    Check "still exactly one server process on 8765" ($pids2.Count -eq 1) `
        "PIDs: $($pids2 -join ',')"
}
finally {
    Write-Host ""
    Write-Host "[9] Cleaning up" -ForegroundColor Cyan
    $held = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    foreach ($p in @($held | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique)) {
        if ($p) {
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            Info "stopped server PID $p"
        }
    }
    Start-Sleep -Milliseconds 1000
    $left = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($left) { Write-Host "        WARNING: port 8765 still held" -ForegroundColor Yellow }
    else { Info "port 8765 released" }
}

Write-Host ""
Write-Host "=============================" -ForegroundColor Cyan
if ($script:Fail -eq 0) {
    Write-Host "ACCEPTANCE PASSED - $($script:Pass) checks" -ForegroundColor Green
    exit 0
} else {
    Write-Host "ACCEPTANCE FAILED - $($script:Fail) failed, $($script:Pass) passed" -ForegroundColor Red
    exit 1
}
