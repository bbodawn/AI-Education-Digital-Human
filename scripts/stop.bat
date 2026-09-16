@echo off
REM ==========================================================
REM  AI Digital Human Education Coach - Stop
REM  Stops only what start.bat started (tracked by PID).
REM  Ollama and the AutoDL instance are left untouched.
REM ==========================================================
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

set "PROJECT_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PROJECT_PYTHON%" (
  echo.
  echo [ERROR] Project virtual environment not found.
  echo         Please create and install .venv first.
  echo.
  pause
  exit /b 1
)

"%PROJECT_PYTHON%" "%~dp0launcher.py" stop

echo.
echo Press any key to close this window...
pause >nul
exit /b 0
