@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AIOS Intelligence Monitor - Release Builder

echo.
echo   ============================================================
echo    AIOS Intelligence Monitor - one-click portable release
echo   ============================================================
echo.
echo    validate  -^>  test  -^>  runtime  -^>  launcher  -^>  smoke test
echo    -^>  clean  -^>  ZIP
echo.
echo    Nothing else to do. The first build downloads the Python
echo    runtime; later builds reuse .build_cache and are faster.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\packaging\build_release.ps1" %*
set "BUILD_EXIT=%ERRORLEVEL%"

echo.
if "%BUILD_EXIT%"=="0" (
    echo   Build finished successfully.
) else (
    echo   Build FAILED with exit code %BUILD_EXIT%.
    echo   Scroll up for the step that stopped it.
)
echo.
echo   Press any key to close...
pause >nul
exit /b %BUILD_EXIT%
