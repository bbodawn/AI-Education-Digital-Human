@echo off
REM ==========================================================
REM  AI Digital Human Education Coach - One-click startup
REM  Thin wrapper only: all logic lives in launcher.py so the
REM  batch file stays ASCII and free of encoding pitfalls.
REM ==========================================================
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

set "PROJECT_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PROJECT_PYTHON%" (
  echo.
  echo [ERROR] Project virtual environment not found.
  echo         Please create and install .venv first:
  echo         D:\Anaconda3_2024\Anaconda3\python.exe -m venv .venv
  echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

"%PROJECT_PYTHON%" "%~dp0launcher.py" start
set EXITCODE=%errorlevel%

echo.
if not "%EXITCODE%"=="0" (
  echo [FAILED] Startup did not finish. Read the error above, fix it, then run start.bat again.
) else (
  echo Press any key to close this window...
)
pause >nul
exit /b %EXITCODE%
