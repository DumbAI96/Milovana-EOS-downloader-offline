@echo off
setlocal
cd /d "%~dp0"

rem --- local settings (self-contained) ---
set "PORT=8123"
set "OPEN_BROWSER=1"

rem --- local venv (created on first run, nothing installed system-wide) ---
if not exist "server\venv\Scripts\python.exe" (
  echo Creating local Python venv in "%~dp0server\venv" ...
  python -m venv server\venv
  if errorlevel 1 (
    echo.
    echo ERROR: could not create venv. Is Python installed and in PATH?
    pause
    exit /b 1
  )
)

call "server\venv\Scripts\activate.bat"

echo ==================================================
echo  Offline teases - local server
echo.
echo  URL:   http://localhost:%PORT%/    (landing page lists all teases)
echo  Stop:  close this window (or press Ctrl+C)
echo ==================================================
echo.

python server\server.py %PORT%

echo.
echo Server stopped.
pause
