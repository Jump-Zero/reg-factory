@echo off
setlocal
cd /d "%~dp0"

echo Starting standalone Codex K12 on http://127.0.0.1:8806 ...
if exist "reg-factory.exe" (
  reg-factory.exe --k12
) else if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -m uvicorn k12.server:app --host 127.0.0.1 --port 8806
) else (
  echo [ERROR] Install reg-factory or the main Python environment first.
  pause
  exit /b 1
)
pause
