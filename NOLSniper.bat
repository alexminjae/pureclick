@echo off
rem NOL Sniper - run from source on Windows.
rem
rem The normal way to use this on Windows is NOLSniper.exe from the Releases
rem page - no Python needed. This file is for running the source directly, which
rem is what you want when the exe misbehaves and you need to see the error.
rem
rem Two rules keep this window from vanishing before anyone can read it:
rem   * every failure path ends at :end_fail, which pauses;
rem   * the file is saved with CRLF line endings (.gitattributes enforces it).
rem     cmd.exe can fail to find a `goto` label in an LF-only file, and then it
rem     prints "cannot find the batch label" and closes instantly - exactly the
rem     silent close this file used to produce on a clean machine.
rem The Korean text is UTF-8, so the code page is switched first.

chcp 65001 >nul
setlocal
cd /d "%~dp0"
title NOL 스나이퍼
set "LOG=%~dp0nolsniper_setup.log"
echo ==== %date% %time% ==== > "%LOG%"

echo.
echo [NOL 스나이퍼] 시작합니다...
echo.

rem ---- 0. Was the zip actually extracted? Double-clicking the .bat inside a
rem         zip viewer runs it from a temp folder holding nothing else.
if not exist "nolsniper_main.py" goto :not_extracted

rem ---- 1. Find a working Python. `py` (the launcher python.org installs) first,
rem         then `python`. Each is *run*, not just looked up: the Windows Store
rem         "python" alias passes `where`, but opens the Store and exits 9009.
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  python3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python3"
)
if not defined PY goto :no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
echo Python 확인:
%PY% --version
echo.

rem ---- 2. A private virtualenv next to this file, made once. The venv's own
rem         python.exe is called directly; activate.bat is one more thing that
rem         can fail, and nothing here needs it.
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo 처음 실행이라 준비가 필요합니다. 1~2분 걸립니다...
  %PY% -m venv ".venv" >>"%LOG%" 2>&1
  if errorlevel 1 goto :fail_venv
)
if not exist "%VPY%" goto :fail_venv

rem ---- 3. Dependencies. Output goes to the log and is shown if this fails -
rem         "--quiet" used to hide the one line that said what went wrong.
echo 필요한 구성 요소를 확인합니다...
"%VPY%" -m pip install --upgrade pip >>"%LOG%" 2>&1
"%VPY%" -m pip install -r "mac\requirements.txt" >>"%LOG%" 2>&1
if errorlevel 1 goto :fail_pip
echo 준비 완료.
echo.

rem ---- 4. Run. One entry point for both roles - see nolsniper_main.py.
echo 실행합니다. 프로그램을 쓰는 동안 이 창은 닫지 마세요.
"%VPY%" nolsniper_main.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail_run
endlocal
exit /b 0

rem ======================= failure paths - all pause =======================

:not_extracted
echo [문제] 압축을 풀지 않은 상태로 실행했습니다.
echo   NOLSniper_Windows.zip 을 마우스 오른쪽 버튼 - "압축 풀기" 로 완전히 푼 뒤,
echo   풀린 폴더 안의 NOLSniper.bat 을 더블클릭하세요.
goto :end_fail

:no_python
echo [문제] Python 이 설치되어 있지 않습니다.
echo   1. https://www.python.org/downloads/  에서 Python 을 내려받아 설치하세요.
echo   2. 설치 첫 화면 맨 아래의 "Add python.exe to PATH" 를 반드시 체크하세요.
echo   3. 설치가 끝나면 이 파일을 다시 더블클릭하세요.
echo.
echo   이미 설치했는데도 이 메시지가 나오면:
echo   설정 - 앱 - 고급 앱 설정 - "앱 실행 별칭" 에서 python.exe 항목을 끄세요.
goto :end_fail

:old_python
echo [문제] 설치된 Python 이 너무 오래된 버전입니다. (3.10 이상 필요)
%PY% --version
echo   https://www.python.org/downloads/  에서 최신 버전을 설치한 뒤 다시 실행하세요.
goto :end_fail

:fail_venv
echo [문제] 실행 환경(.venv 폴더) 을 만들지 못했습니다.
echo   이 폴더 안의 .venv 폴더를 삭제한 뒤 다시 실행해 보세요.
goto :show_log

:fail_pip
echo [문제] 필요한 구성 요소 설치에 실패했습니다. (pip install)
echo   인터넷 연결을 확인한 뒤 다시 실행해 보세요.
echo   계속 실패하면 이 폴더 안의 .venv 폴더를 삭제하고 다시 시도하세요.
goto :show_log

:fail_run
echo.
echo [문제] 프로그램이 오류로 종료되었습니다. (종료 코드 %RC%)
if exist "%LOCALAPPDATA%\NOLSniper\crash.log" (
  echo.
  echo ---- 오류 기록 마지막 부분: %LOCALAPPDATA%\NOLSniper\crash.log ----
  powershell -NoProfile -Command "Get-Content -Tail 40 -LiteralPath '%LOCALAPPDATA%\NOLSniper\crash.log'"
)
goto :end_fail

:show_log
echo.
echo ---- 설치 기록 (%LOG%) 마지막 부분 ----
powershell -NoProfile -Command "Get-Content -Tail 30 -LiteralPath '%LOG%'"
goto :end_fail

:end_fail
echo.
echo ============================================================
echo  이 창의 내용을 화면 캡처해서 보내주세요.
echo  아무 키나 누르면 창이 닫힙니다.
echo ============================================================
pause
endlocal
exit /b 1
