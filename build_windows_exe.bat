@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Building PureClick.exe (single file)
echo ============================================
echo.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
    echo Python 3.11 or newer is required to build.
    echo Download it from https://www.python.org/downloads/windows/
    echo Make sure to check "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo Using Python: %PYCMD%
echo.

echo [1/3] Creating build environment...
%PYCMD% -m venv .venv
if errorlevel 1 goto fail

echo [2/3] Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto fail

echo [3/3] Building single-file executable...
".venv\Scripts\python.exe" -m PyInstaller --onefile --windowed --name PureClick --hidden-import pureclick_core --hidden-import pureclick_watch_core pureclick.py
if errorlevel 1 goto fail

echo.
echo ============================================
echo   Done. Your app is here:
echo       dist\PureClick.exe
echo ============================================
echo.
echo You can now send dist\PureClick.exe to anyone.
echo They just double-click it. No Python needed.
echo.
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
echo.
pause
exit /b 1
