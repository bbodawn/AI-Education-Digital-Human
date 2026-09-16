@echo off
REM ==========================================================
REM  AI Digital Human Education Coach - One-click startup
REM  Thin wrapper only: all logic lives in launcher.py so the
REM  batch file stays ASCII and free of encoding pitfalls.
REM ==========================================================
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo         Install Python 3.11+ and make sure "python" works in a terminal.
  echo.
  pause
  exit /b 1
)

python "%~dp0launcher.py" start
set EXITCODE=%errorlevel%

echo.
if not "%EXITCODE%"=="0" (
  echo [FAILED] Startup did not finish. Read the error above, fix it, then run start.bat again.
) else (
  echo Press any key to close this window...
)
pause >nul
exit /b %EXITCODE%
