@echo off
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 pureclick.py
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python pureclick.py
    goto done
)

echo Python 3.11 or newer is required.
echo Download it from https://www.python.org/downloads/windows/
pause

:done
