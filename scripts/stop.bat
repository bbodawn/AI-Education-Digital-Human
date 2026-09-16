@echo off
REM ==========================================================
REM  AI Digital Human Education Coach - Stop
REM  Stops only what start.bat started (tracked by PID).
REM  Ollama and the AutoDL instance are left untouched.
REM ==========================================================
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo.
  pause
  exit /b 1
)

python "%~dp0launcher.py" stop

echo.
echo Press any key to close this window...
pause >nul
exit /b 0
