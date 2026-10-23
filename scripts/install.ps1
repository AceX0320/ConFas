# ConFas one-click installer (run in an ordinary PowerShell - no admin needed).
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Write-Host "== ConFas setup ==" -ForegroundColor Cyan

# 1. Python check
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "Python 3.10+ is required. Install it from https://python.org and re-run." -ForegroundColor Red; exit 1 }

# 2. Virtualenv + deps
Push-Location $root
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
& ".\.venv\Scripts\python.exe" -m pip install "pywin32" "pystray"

# 3. Build the single-file EXE
& ".\.venv\Scripts\python.exe" build.py
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Host "Build failed." -ForegroundColor Red; exit 1 }

# 4. Place the EXE in %LOCALAPPDATA%\ConFas and run first-time setup
$dest = Join-Path $env:LOCALAPPDATA "ConFas"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item ".\dist\ConFas.exe" $dest -Force
Start-Process -FilePath (Join-Path $dest "ConFas.exe") -ArgumentList "install" -Wait
Start-Process -FilePath (Join-Path $dest "ConFas.exe")   # launches tray daemon now

Pop-Location
Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  * ConFas.exe installed to $dest"
Write-Host "  * Registered in Startup apps (check Task Manager > Startup apps)"
Write-Host "  * Right-click any document in Explorer -> 'Convert with ConFas'"
Write-Host "  * Drop files into ~\ConFasDrop for automatic conversion"
