@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   reg-factory installer (Python + Codex K12)
echo ============================================================
echo.

REM ---- 1. find Python (>=3.10) ----
set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" (
  where python >nul 2>nul && set PY=python
)
if "%PY%"=="" (
  echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo         Check "Add Python to PATH" during install, then run this script again.
  if not "%REG_FACTORY_NONINTERACTIVE%"=="1" pause
  exit /b 1
)
echo [1/6] Python: %PY%
%PY% --version

REM ---- 2. create venv ----
if exist ".venv\Scripts\python.exe" (
  echo [2/6] venv exists, skip create.
) else (
  echo [2/6] creating venv .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] venv create failed
    if not "%REG_FACTORY_NONINTERACTIVE%"=="1" pause
    exit /b 1
  )
)

set VENV_PY=.venv\Scripts\python.exe

REM ---- 3. install deps ----
echo [3/6] installing deps (pip install -r requirements.txt) ...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>nul
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] deps install failed, check network/pip mirror
  if not "%REG_FACTORY_NONINTERACTIVE%"=="1" pause
  exit /b 1
)

REM ---- 4. install browser runtimes ----
echo [4/6] installing RuyiPage Firefox and Playwright Chromium ...
"%VENV_PY%" -m ruyipage install
if errorlevel 1 ( echo [WARN] RuyiPage Firefox install failed, run later: .venv\Scripts\python -m ruyipage install )
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 ( echo [WARN] playwright core install failed, run later: .venv\Scripts\playwright install chromium )

REM ---- 5. install/build Codex K12 (optional when Node is unavailable) ----
echo [5/6] preparing Codex K12 console ...
set K12_OK=0
where node >nul 2>nul
if errorlevel 1 (
  echo [WARN] Node.js not found. Main panel remains available; install Node.js 20+ to enable Codex K12.
) else (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [WARN] npm not found. Skip Codex K12 setup.
  ) else if exist "codex_k12\package.json" (
    pushd "codex_k12"
    call npm install
    if not errorlevel 1 call npm run build
    if not errorlevel 1 set K12_OK=1
    popd
    if "!K12_OK!"=="0" echo [WARN] Codex K12 setup failed. Retry later in codex_k12 with npm install and npm run build.
  )
)

REM ---- 6. prepare .env ----
if exist ".env" (
  echo [6/6] .env exists, keep your config.
) else (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo [6/6] .env created from template, fill keys later in the web panel Config page.
  ) else (
    echo [6/6] .env.example not found, skip.
  )
)

echo.
echo ============================================================
echo   Install done!
echo   - RuyiPage Firefox is the default; start Clash Verge before tasks
echo   - Start BitBrowser/AdsPower only when selecting those providers
echo   - Double-click start.bat to open the control panel
echo ============================================================
if not "%REG_FACTORY_NONINTERACTIVE%"=="1" pause
