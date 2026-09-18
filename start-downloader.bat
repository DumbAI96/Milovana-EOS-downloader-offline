@echo off
setlocal
cd /d "%~dp0"

rem --- local settings (self-contained) ---
rem DOWNLOAD_RATE = files per second (raise for faster; 1.0 = one file per second)
set "DOWNLOAD_RATE=2.0"
set "PYTHONUTF8=1"

rem --- the downloader's own venv (created on first run, nothing system-wide) ---
if not exist "downloader\venv\Scripts\python.exe" (
  echo Creating downloader venv in "%~dp0downloader\venv" ...
  python -m venv downloader\venv
  if errorlevel 1 (
    echo.
    echo ERROR: could not create venv. Is Python installed and in PATH?
    pause
    exit /b 1
  )
)

call "downloader\venv\Scripts\activate.bat"
python downloader\downloader.py

echo.
pause
