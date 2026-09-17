<#
    AIOS Intelligence Monitor - one-click portable release builder.

    Run it by double-clicking Build_Release.bat in the repository root. There
    is no manual fix-up step, no manual deletion and no manual zipping: this
    script validates, tests, builds, smoke-tests, cleans, packages and opens
    the release folder.

    Written for Windows PowerShell 5.1, which constrains two things:

      * Add-Type has no -CompilerOptions, so the launcher is compiled with the
        .NET Framework csc.exe directly (that is the only way to set the icon
        and the winexe subsystem).
      * No ternary / null-coalescing operators.
      * This file MUST be saved as UTF-8 WITH a BOM. Windows PowerShell 5.1
        re-reads a BOM-less .ps1 as ANSI, which silently turns the Chinese
        literals below into mojibake - the end-user manual then ships under a
        garbled filename. The 使用说明.txt entry in the archive check at the
        end of this script is the tripwire for exactly that mistake.

    Everything the recipient must NOT receive - the developer's database,
    reports, logs, API credentials, tests, caches - is excluded by construction:
    the staging directory is built from an explicit allow-list, never by
    copying the repository and deleting afterwards.
#>

[CmdletBinding()]
param(
    # Skip the test suite. For iterating on packaging itself only; a real
    # release must never be built with this.
    [switch]$SkipTests,
    # Skip the packaged-application smoke test.
    [switch]$SkipSmokeTest,
    # Python version for the embeddable runtime.
    [string]$PyVersion = "3.12.7"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# --- output helpers ---------------------------------------------------------

$script:StepIndex = 0

function Write-Step {
    param([string]$Message)
    $script:StepIndex++
    Write-Host ""
    Write-Host ("  [{0,2}] {1}" -f $script:StepIndex, $Message) -ForegroundColor Cyan
}

function Write-Detail {
    param([string]$Message)
    Write-Host "       $Message" -ForegroundColor DarkGray
}

function Write-Ok {
    param([string]$Message)
    Write-Host "       $Message" -ForegroundColor Green
}

function Fail {
    param([string]$Message)
    Write-Host ""
    Write-Host "  BUILD FAILED" -ForegroundColor Red
    Write-Host "  $Message" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# --- 1. locate the project root --------------------------------------------

Write-Step "Locating project root"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
Set-Location $Root

foreach ($required in @("app.py", "aios", "requirements.txt", "AIOS.ico")) {
    if (-not (Test-Path (Join-Path $Root $required))) {
        Fail "This does not look like the AIOS repository - '$required' is missing from $Root"
    }
}
Write-Detail $Root

# --- 2. read the version from the single source of truth --------------------

Write-Step "Reading application version"

$InitFile = Join-Path $Root "aios\__init__.py"
$VersionMatch = Select-String -Path $InitFile -Pattern '^__version__\s*=\s*"([^"]+)"'
if (-not $VersionMatch) { Fail "Could not read __version__ from $InitFile" }
$Version = $VersionMatch.Matches[0].Groups[1].Value

$Stamp = Get-Date -Format "yyyyMMdd-HHmm"
$PackageName = "AIOS_Intelligence_Monitor_v${Version}_${Stamp}_Portable"
Write-Detail "version $Version -> $PackageName"

# --- developer python -------------------------------------------------------

$DevPython = $null
foreach ($candidate in @("python", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $DevPython = $cmd.Source; break }
}
if (-not $DevPython) { Fail "Python was not found on PATH; it is needed to compile and test." }

# --- 3. byte-compile --------------------------------------------------------

Write-Step "Validating sources (compileall)"
& $DevPython -m compileall -q "aios" "app.py" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Source validation failed - fix the syntax errors above." }
Write-Ok "aios/ and app.py compile cleanly"

# --- 4. test suite ----------------------------------------------------------

if ($SkipTests) {
    Write-Step "Test suite SKIPPED (-SkipTests)"
    Write-Host "       Never ship a build made this way." -ForegroundColor Yellow
} else {
    Write-Step "Running the test suite"
    Write-Detail "this takes a couple of minutes"
    $TestOutput = & $DevPython -m pytest "tests" -q 2>&1
    $TestExit = $LASTEXITCODE
    $Summary = ($TestOutput | Select-String -Pattern "passed|failed|error" | Select-Object -Last 1)
    if ($TestExit -ne 0) {
        $TestOutput | Select-Object -Last 25 | ForEach-Object { Write-Host "       $_" }
        Fail "Tests failed. A release is only built from a green suite."
    }
    if ($Summary) { Write-Ok ($Summary.ToString().Trim()) }
}

# --- 5. clean staging -------------------------------------------------------

Write-Step "Creating clean staging directory"

$ReleaseDir = Join-Path $Root "release"
$Stage = Join-Path $ReleaseDir $PackageName
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Write-Detail $Stage

# --- 6. copy only what the recipient needs ----------------------------------

Write-Step "Staging application files"

# An allow-list, deliberately. Copying the repo and deleting afterwards is how
# a stray aios.db or an API key ends up in someone else's ZIP.
$IncludeFiles = @("app.py", "requirements.txt", "alembic.ini", "AIOS.ico")
$IncludeDirs  = @("aios", "alembic", "scripts")

foreach ($file in $IncludeFiles) {
    $source = Join-Path $Root $file
    if (Test-Path $source) { Copy-Item $source -Destination $Stage -Force }
}
foreach ($dir in $IncludeDirs) {
    $source = Join-Path $Root $dir
    if (-not (Test-Path $source)) { continue }
    Copy-Item $source -Destination $Stage -Recurse -Force
}

# Nothing developer-shaped may survive, even if it slipped into a copied tree.
$Junk = @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
foreach ($name in $Junk) {
    Get-ChildItem -Path $Stage -Filter $name -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}
Get-ChildItem -Path $Stage -Include "*.pyc", "*.pyo" -Recurse -Force -File -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }

# The recipient gets an empty data tree; the app creates its own database.
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "data\logs") | Out-Null
Write-Ok "application staged (no developer data copied)"

# --- 7. python embeddable runtime, via the build cache ----------------------

Write-Step "Preparing portable Python runtime ($PyVersion)"

$CacheDir = Join-Path $Root ".build_cache"
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
$PipCache = Join-Path $CacheDir "pip"
New-Item -ItemType Directory -Force -Path $PipCache | Out-Null

$EmbedZip = Join-Path $CacheDir "python-$PyVersion-embed-amd64.zip"
if (Test-Path $EmbedZip) {
    Write-Detail "using cached runtime archive"
} else {
    $PythonUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
    Write-Detail "downloading $PythonUrl"
    try {
        Invoke-WebRequest -Uri $PythonUrl -OutFile $EmbedZip -UseBasicParsing
    } catch {
        if (Test-Path $EmbedZip) { Remove-Item $EmbedZip -Force -ErrorAction SilentlyContinue }
        Fail "Could not download the official Python embeddable runtime.`n$($_.Exception.Message)"
    }
}

$Runtime = Join-Path $Stage "runtime"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
Expand-Archive -Path $EmbedZip -DestinationPath $Runtime -Force

# --- 8. configure the _pth --------------------------------------------------

Write-Step "Configuring the embedded interpreter path file"

$Pth = Get-ChildItem $Runtime -Filter "python*._pth" | Select-Object -First 1
if (-not $Pth) { Fail "The embeddable runtime has no python*._pth file." }

# The embeddable distribution runs in isolated mode: it ignores the working
# directory and PYTHONPATH entirely. Without these three lines `import aios`
# and `import app` fail, because the application lives one level above runtime\.
$PthLines = @(Get-Content $Pth.FullName | ForEach-Object {
    if ($_ -match '^\s*#\s*import site\s*$') { "import site" } else { $_ }
})
if (-not ($PthLines -contains "..")) { $PthLines += ".." }
if (-not ($PthLines -contains "Lib\site-packages")) { $PthLines += "Lib\site-packages" }
if (-not ($PthLines -contains "import site")) { $PthLines += "import site" }
$PthLines | Set-Content -Path $Pth.FullName -Encoding ASCII
New-Item -ItemType Directory -Force -Path (Join-Path $Runtime "Lib\site-packages") | Out-Null
Write-Ok ("_pth configured: " + (($PthLines | Where-Object { $_ -and -not $_.StartsWith("#") }) -join ", "))

# --- 9. dependencies --------------------------------------------------------

Write-Step "Installing dependencies into the portable runtime"

$RuntimePython = Join-Path $Runtime "python.exe"
$SitePackages = Join-Path $Runtime "Lib\site-packages"

$GetPip = Join-Path $CacheDir "get-pip.py"
if (-not (Test-Path $GetPip)) {
    Write-Detail "downloading get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip -UseBasicParsing
} else {
    Write-Detail "using cached get-pip.py"
}

& $RuntimePython $GetPip --no-warn-script-location --quiet
if ($LASTEXITCODE -ne 0) { Fail "Installing pip into the portable runtime failed." }

# requirements.txt also pins pytest and httpx, under a "# Tests" heading. The
# recipient never runs the suite, so installing them would only add weight to
# the download. Everything from that heading onwards is dropped.
$RuntimeReqs = Join-Path $CacheDir "requirements-runtime.txt"
$SkippingTestDeps = $false
@(Get-Content (Join-Path $Root "requirements.txt") | ForEach-Object {
    if ($_ -match '^\s*#\s*Tests\s*$') { $SkippingTestDeps = $true }
    if (-not $SkippingTestDeps) { $_ }
}) | Set-Content -Path $RuntimeReqs -Encoding ASCII

Write-Detail "pip install -r requirements.txt, runtime deps only (wheel cache: .build_cache\pip)"
& $RuntimePython -m pip install `
    --disable-pip-version-check --no-warn-script-location --quiet `
    --cache-dir $PipCache `
    -r $RuntimeReqs `
    --target $SitePackages
if ($LASTEXITCODE -ne 0) { Fail "Installing the application dependencies failed." }
Write-Ok "dependencies installed"

# --- 10. launcher executable ------------------------------------------------

Write-Step "Compiling the launcher executable"

$CscCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$Csc = $CscCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Csc) {
    Fail ".NET Framework csc.exe was not found. Windows 10/11 normally includes it."
}

$LauncherSource = Join-Path $ScriptDir "launcher\AIOSLauncher.cs"
if (-not (Test-Path $LauncherSource)) { Fail "Launcher source is missing: $LauncherSource" }

$LauncherExe = Join-Path $Stage "AIOS Intelligence Monitor.exe"
$StageIcon = Join-Path $Stage "AIOS.ico"

# Windows PowerShell 5.1's Add-Type cannot pass /win32icon or /target:winexe.
& $Csc /nologo /target:winexe /platform:anycpu "/win32icon:$StageIcon" `
    /reference:System.dll /reference:System.Windows.Forms.dll `
    "/out:$LauncherExe" "$LauncherSource"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $LauncherExe)) {
    Fail "Launcher compilation failed (csc exit $LASTEXITCODE)."
}
Write-Ok "AIOS Intelligence Monitor.exe built with icon"

# --- 11-14. helper scripts and documentation --------------------------------

Write-Step "Generating helper scripts and documentation"

$DebugCmd = @'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AIOS Intelligence Monitor - Debug
echo.
echo  Starting AIOS with a visible console.
echo  Errors stay on screen instead of only reaching data\logs\launcher_console.log.
echo.
"%~dp0runtime\python.exe" "%~dp0app.py"
echo.
echo  AIOS has stopped. Press any key to close.
pause >nul
'@
Set-Content -Path (Join-Path $Stage "AIOS_Debug_Start.cmd") -Value $DebugCmd -Encoding UTF8

$ShortcutCmd = @'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "TARGET=%~dp0AIOS Intelligence Monitor.exe"
set "ICON=%~dp0AIOS.ico"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$([Environment]::GetFolderPath('Desktop'))\AIOS 情报监测.lnk\"); $s.TargetPath=\"%TARGET%\"; $s.WorkingDirectory=\"%~dp0\"; $s.IconLocation=\"%ICON%\"; $s.Description='AIOS Intelligence Monitor'; $s.Save()"
echo.
echo  Desktop shortcut created: AIOS 情报监测
echo.
pause
'@
Set-Content -Path (Join-Path $Stage "Create_Desktop_Shortcut.cmd") -Value $ShortcutCmd -Encoding UTF8

$Readme = @"
AIOS Intelligence Monitor v$Version
便携版使用说明
====================================================

一、启动

    双击   AIOS Intelligence Monitor.exe

    浏览器会自动打开 http://127.0.0.1:8765
    首次启动需要几秒钟初始化数据库，请稍候。

    不需要安装 Python，不需要 pip，不需要命令行。

二、首次配置

    1. 打开左侧「设置」
    2. 进入「AI 模型」，填写你自己的 API Key
       （支持 DeepSeek / 通义千问 / 智谱 GLM / Kimi / 豆包 /
         MiniMax / OpenAI / Claude / Gemini 等）
    3. 回到「概览」，点击「立即监测」

    API Key 保存在 Windows 凭据管理器中，不会写入数据库，
    也不会随本软件包分发。

三、数据位置

    所有数据都在本目录下的 data\ 文件夹：

        data\aios.db        数据库
        data\reports\       导出的日报
        data\logs\          运行日志

    整个文件夹可以直接复制到别的电脑或移动硬盘。
    删除 data\ 文件夹即可恢复到全新状态。

四、退出

    关闭浏览器不会停止后台服务。
    完全退出请在「任务管理器」中结束 python.exe 进程。

五、启动失败怎么办

    1. 双击 AIOS_Debug_Start.cmd
       这会显示完整的控制台输出，错误会留在屏幕上。

    2. 查看 data\logs\launcher_console.log

    3. 如果提示端口被占用，说明 AIOS 已经在运行，
       直接访问 http://127.0.0.1:8765 即可。

六、创建桌面快捷方式

    双击 Create_Desktop_Shortcut.cmd

====================================================
本软件在本机运行，不上传任何数据。
情报判断不等同于事实，关键数字请回溯原始来源核验。
"@
Set-Content -Path (Join-Path $Stage "使用说明.txt") -Value $Readme -Encoding UTF8

$PyDisplay = (& $RuntimePython -c "import sys; print(sys.version.split()[0])") 2>&1
$BuildInfo = @"
AIOS Intelligence Monitor
Portable Windows build
====================================================

Application version : $Version
Package             : $PackageName
Built at            : $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Built on            : $env:COMPUTERNAME
Embedded Python     : $PyDisplay (windows x64 embeddable)
Builder             : tools/packaging/build_release.ps1

Contents
----------------------------------------------------
AIOS Intelligence Monitor.exe   launcher (hidden console, opens the browser)
AIOS_Debug_Start.cmd            visible-console launcher for troubleshooting
Create_Desktop_Shortcut.cmd     creates a desktop shortcut
使用说明.txt                     end-user instructions (Chinese)
runtime\                        embedded Python + dependencies
aios\, app.py, alembic\         the application
data\                           empty; created on first run

This package contains no database, no reports, no logs, no API
credentials, no tests and no build tooling.
"@
Set-Content -Path (Join-Path $Stage "BUILD_INFO.txt") -Value $BuildInfo -Encoding UTF8
Write-Ok "AIOS_Debug_Start.cmd, Create_Desktop_Shortcut.cmd, 使用说明.txt, BUILD_INFO.txt"

# --- 15. import smoke test with the EMBEDDED interpreter --------------------

Write-Step "Smoke test: imports under the embedded interpreter"

$ImportProbe = @"
import sys
sys.path.insert(0, r'$Stage')
import aios, app, fastapi, sqlalchemy, keyring, apscheduler, uvicorn, requests
import bs4, jinja2, alembic, multipart
print('IMPORTS-OK', aios.__version__)
"@
$ProbeFile = Join-Path $env:TEMP "aios_import_probe.py"
Set-Content -Path $ProbeFile -Value $ImportProbe -Encoding UTF8

Push-Location $Stage
$ImportResult = & $RuntimePython $ProbeFile 2>&1
$ImportExit = $LASTEXITCODE
Pop-Location
Remove-Item $ProbeFile -Force -ErrorAction SilentlyContinue

if ($ImportExit -ne 0 -or ($ImportResult -join "`n") -notmatch "IMPORTS-OK") {
    $ImportResult | ForEach-Object { Write-Host "       $_" -ForegroundColor Red }
    Fail "The packaged runtime cannot import the application. The _pth is probably wrong."
}
Write-Ok (($ImportResult | Select-String "IMPORTS-OK").ToString().Trim())

# --- 16-19. run the packaged application for real --------------------------

function Invoke-SmokeAttempt {
    param([int]$SmokePort, [string]$SmokeLog)

    # Each attempt starts from a fresh data\ tree, so a half-created database
    # from a failed attempt cannot be what breaks the next one.
    $dataDir = Join-Path $Stage "data"
    if (Test-Path $dataDir) { Remove-Item $dataDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path (Join-Path $Stage "data\logs") | Out-Null
    Remove-Item $SmokeLog, "$SmokeLog.err" -Force -ErrorAction SilentlyContinue

    $SmokeProcess = Start-Process -FilePath $RuntimePython `
        -ArgumentList @("app.py", "--no-browser", "--port", "$SmokePort") `
        -WorkingDirectory $Stage -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $SmokeLog -RedirectStandardError "$SmokeLog.err"

    $SmokeOk = $false
    $SmokeStatus = "no response"
    try {
        $deadline = (Get-Date).AddSeconds(120)
        while ((Get-Date) -lt $deadline) {
            if ($SmokeProcess.HasExited) {
                $SmokeStatus = "the process exited with code $($SmokeProcess.ExitCode)"
                break
            }
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$SmokePort/healthz" `
                    -UseBasicParsing -TimeoutSec 3
                if ($response.StatusCode -eq 200) {
                    $SmokeOk = $true
                    $SmokeStatus = "HTTP 200 from /healthz"
                    break
                }
            } catch {
                Start-Sleep -Milliseconds 400
            }
        }

        if ($SmokeOk) {
            # The UI itself, not just the health probe.
            try {
                # NOT $home - that is a ReadOnly automatic variable in PowerShell
                # and assigning to it throws, failing a smoke test that passed.
                $Dashboard = Invoke-WebRequest -Uri "http://127.0.0.1:$SmokePort/" `
                    -UseBasicParsing -TimeoutSec 10
                if ($Dashboard.StatusCode -ne 200) {
                    $SmokeOk = $false
                    $SmokeStatus = "the dashboard returned HTTP $($Dashboard.StatusCode)"
                }
            } catch {
                $SmokeOk = $false
                $SmokeStatus = "the dashboard did not render: $($_.Exception.Message)"
            }
        }
    } finally {
        # 19. terminate cleanly, and take the hidden child with it.
        if ($SmokeProcess -and -not $SmokeProcess.HasExited) {
            Stop-Process -Id $SmokeProcess.Id -Force -ErrorAction SilentlyContinue
            $SmokeProcess.WaitForExit(10000) | Out-Null
        }
    }

    return [pscustomobject]@{ Ok = $SmokeOk; Status = $SmokeStatus }
}

if ($SkipSmokeTest) {
    Write-Step "Application smoke test SKIPPED (-SkipSmokeTest)"
} else {
    Write-Step "Smoke test: starting the packaged application"

    # Two attempts, because importing the dependency tree for the first time on
    # Windows is not perfectly reliable through no fault of this package:
    # pydantic can raise "Definitions error: definition ... was never filled"
    # while fastapi's models are being built, and the same staged directory then
    # starts cleanly on the very next try. A genuinely broken package fails both
    # attempts, so this cannot hide a real defect - and when the first attempt
    # does fail, the reason is printed rather than swallowed.
    $SmokeLog = Join-Path $env:TEMP "aios_smoke_$Stamp.log"
    $MaxAttempts = 2
    $Smoke = $null

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        # A different port each attempt: a killed server can leave the previous
        # one in TIME_WAIT, and app.py treats a busy port as "already running".
        $Smoke = Invoke-SmokeAttempt -SmokePort (8790 + $attempt) -SmokeLog $SmokeLog
        if ($Smoke.Ok) {
            if ($attempt -gt 1) { Write-Detail "attempt $attempt succeeded" }
            break
        }

        Write-Host "       attempt $attempt of $MaxAttempts failed: $($Smoke.Status)" -ForegroundColor Yellow
        foreach ($logFile in @($SmokeLog, "$SmokeLog.err")) {
            if (Test-Path $logFile) {
                Get-Content $logFile -Tail 12 |
                    ForEach-Object { Write-Host "       $_" -ForegroundColor DarkGray }
            }
        }
        if ($attempt -lt $MaxAttempts) { Write-Detail "retrying once" }
    }

    if (-not $Smoke.Ok) {
        Fail ("The packaged application did not serve HTTP after $MaxAttempts attempts: " +
              "$($Smoke.Status)`n       The output of both attempts is above.")
    }
    Remove-Item $SmokeLog, "$SmokeLog.err" -Force -ErrorAction SilentlyContinue
    Write-Ok $Smoke.Status
}

# --- 20. remove everything the smoke test created ---------------------------

Write-Step "Removing smoke-test artifacts"

$DataDir = Join-Path $Stage "data"
if (Test-Path $DataDir) { Remove-Item $DataDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "data\logs") | Out-Null

# The smoke test byte-compiled the application inside the package.
Get-ChildItem -Path $Stage -Filter "__pycache__" -Recurse -Force -Directory -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

$Leaks = @()
$Leaks += Get-ChildItem -Path $Stage -Filter "*.db" -Recurse -Force -File -ErrorAction SilentlyContinue
$Leaks += Get-ChildItem -Path $Stage -Filter "*.db-wal" -Recurse -Force -File -ErrorAction SilentlyContinue
$Leaks += Get-ChildItem -Path $Stage -Filter "*.db-shm" -Recurse -Force -File -ErrorAction SilentlyContinue
if ($Leaks.Count -gt 0) {
    $Leaks | ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
    Write-Detail "removed $($Leaks.Count) database file(s) created by the smoke test"
}
Write-Ok "package contains an empty data\ tree"

# --- 21. zip ----------------------------------------------------------------

Write-Step "Creating the distributable ZIP"

$ZipPath = Join-Path $ReleaseDir "$PackageName.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

Add-Type -AssemblyName System.IO.Compression.FileSystem

# Written entry by entry rather than with ZipFile::CreateFromDirectory, for two
# reasons specific to .NET Framework:
#
#   * CreateFromDirectory names entries with the platform separator, i.e.
#     BACKSLASHES. The ZIP spec requires forward slashes; unzip on macOS and
#     Linux takes "aiospp.py" as a single file whose name contains a
#     backslash, so the package cannot be opened anywhere but Windows.
#   * The entry-name encoding is not selectable there, and one shipped file has
#     a Chinese name. Opening the archive with Encoding.UTF8 sets the
#     language-encoding flag so that name survives.
$StagePrefix = (Split-Path -Leaf $Stage)
$Archive = [System.IO.Compression.ZipFile]::Open(
    $ZipPath, [System.IO.Compression.ZipArchiveMode]::Create, [System.Text.Encoding]::UTF8)
try {
    $StageRootLength = $Stage.Length + 1
    foreach ($item in Get-ChildItem -Path $Stage -Recurse -Force) {
        # [char]92 / [char]47 rather than '\' and '/' literals: this file is
        # edited by tooling that has eaten lone backslashes before, and an
        # empty first argument makes String.Replace throw at run time.
        $relative = $item.FullName.Substring($StageRootLength).Replace([char]92, [char]47)
        $entryName = "$StagePrefix/$relative"
        if ($item.PSIsContainer) {
            # Only empty directories need an entry of their own; data\logs is
            # empty by design and the application expects it to exist.
            if (-not (Get-ChildItem -Path $item.FullName -Force | Select-Object -First 1)) {
                $Archive.CreateEntry("$entryName/") | Out-Null
            }
        } else {
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $Archive, $item.FullName, $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
} finally {
    $Archive.Dispose()
}
if (-not (Test-Path $ZipPath)) { Fail "The ZIP was not created." }

$ZipInfo = Get-Item $ZipPath
$ZipMB = [math]::Round($ZipInfo.Length / 1MB, 1)
Write-Ok "$($ZipInfo.Name)  ($ZipMB MB)"

# --- 22. validate the archive ----------------------------------------------

Write-Step "Validating the archive contents"

$Verify = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
try {
    # Normalised, so this check can never again pass or fail on a separator.
    $Entries = $Verify.Entries | ForEach-Object { $_.FullName.Replace([char]92, [char]47) }
    $RawEntries = $Verify.Entries | ForEach-Object { $_.FullName }
} finally {
    $Verify.Dispose()
}

$BackslashEntries = @($RawEntries | Where-Object { $_.IndexOf([char]92) -ge 0 })
if ($BackslashEntries.Count -gt 0) {
    Fail "The archive uses backslash entry names; it will not unzip correctly off Windows."
}

$MustContain = @(
    "AIOS Intelligence Monitor.exe",
    "AIOS_Debug_Start.cmd",
    "Create_Desktop_Shortcut.cmd",
    "使用说明.txt",   # if this fails, this .ps1 lost its UTF-8 BOM and PowerShell
                      # 5.1 re-read the literal as ANSI
    "BUILD_INFO.txt",
    "app.py",
    "aios/__init__.py",
    "runtime/python.exe"
)
$Missing = @()
foreach ($needle in $MustContain) {
    $found = $Entries | Where-Object { $_ -like "*$needle" }
    if (-not $found) { $Missing += $needle }
}
if ($Missing.Count -gt 0) {
    Fail "The ZIP is missing: $($Missing -join ', ')"
}

# Nothing private, nothing developer-only.
#
# Scoped to OUR files: third-party packages under runtime\Lib\site-packages are
# not ours to police, and several legitimately ship what these patterns
# describe (beautifulsoup4 ships bs4/tests/, pip byte-compiles on install).
# Leaving the scan unscoped fails a build that is in fact correct.
$OwnEntries = $Entries | Where-Object { $_ -notmatch '(^|/)runtime/' }

$Forbidden = @{
    "a database"        = "*.db"
    "tests"             = "*/tests/*"
    "pytest cache"      = "*.pytest_cache*"
    "compiled bytecode" = "*.pyc"
    "git metadata"      = "*/.git/*"
    "the build cache"   = "*.build_cache*"
    "builder sources"   = "*/tools/packaging/*"
    "a report export"   = "*/data/reports/*"
    "an env file"       = "*.env"
}
$Violations = @()
foreach ($label in $Forbidden.Keys) {
    $pattern = $Forbidden[$label]
    $hit = $OwnEntries | Where-Object { $_ -like $pattern } | Select-Object -First 1
    if ($hit) { $Violations += "$label ($hit)" }
}

# Credentials and the developer's own data must be absent from the WHOLE
# archive, third-party directories included.
foreach ($secret in @("*aios.db*", "*.env", "*credentials.json", "*secrets.*")) {
    $hit = $Entries | Where-Object { $_ -like $secret } | Select-Object -First 1
    if ($hit) { $Violations += "a credential or developer data file ($hit)" }
}
if ($Violations.Count -gt 0) {
    Fail "The ZIP contains files it must not ship:`n       $($Violations -join "`n       ")"
}

Write-Ok "$($Entries.Count) entries; required files present, nothing private included"

# --- 23. done ---------------------------------------------------------------

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   BUILD SUCCEEDED" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "   Package : $PackageName"
Write-Host "   ZIP     : $ZipPath"
Write-Host "   Size    : $ZipMB MB"
Write-Host ""
Write-Host "   Send the ZIP. The recipient unzips it and double-clicks"
Write-Host "   'AIOS Intelligence Monitor.exe'. Nothing else is required."
Write-Host ""

Start-Process explorer.exe "/select,`"$ZipPath`""
exit 0
