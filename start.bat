@echo off
cd /d "%~dp0"
echo.
echo   ============================================
echo    AI Investment Committee
echo   ============================================
echo.

where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto done
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
  goto done
)

echo   ERROR: Python 3 was not found on PATH.
echo.
echo   1. Download it from https://www.python.org/downloads/
echo   2. During setup, tick "Add python.exe to PATH"
echo   3. Re-run this file
echo.
echo   No third-party packages are needed - the standard library is enough.

:done
echo.
echo   Server stopped. Press any key to close.
pause >nul
