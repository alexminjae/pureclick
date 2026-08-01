$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Error "Python launcher 'py' was not found. Install Python 3.11+ from python.org first."
}

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name PureClick `
  --hidden-import pureclick_seat_core `
  --add-data "browser;browser" `
  pureclick.py

Write-Host ""
Write-Host "Built: dist\PureClick.exe"
