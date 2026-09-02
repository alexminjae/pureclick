@echo off
rem Run PureClick from source on Windows.
rem
rem The normal way to use this on Windows is PureClick.exe from the Releases
rem page — no Python needed. This file is for running the source directly, which
rem is what you want when the exe misbehaves and you need to see the error.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python이 필요합니다.
  echo   https://www.python.org/downloads/  에서 설치한 뒤
  echo   설치 화면의 "Add python.exe to PATH" 를 반드시 체크하세요.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 처음 실행이라 준비가 필요합니다. 1~2분 걸립니다...
  python -m venv .venv || goto :fail
)

call ".venv\Scripts\activate.bat" || goto :fail
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r mac\requirements.txt || goto :fail

rem One entry point, both roles — see pureclick_main.py.
python pureclick_main.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo 실행에 실패했습니다. 위의 메시지를 그대로 알려주세요.
echo WebView2 런타임이 없다는 메시지면 아래에서 설치하세요:
echo   https://developer.microsoft.com/microsoft-edge/webview2/
echo.
pause
exit /b 1
