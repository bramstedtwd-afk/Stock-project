@echo off
rem StockSage one-command launcher (Windows).
rem   start.bat            -> open StockSage in its own app window
rem   start.bat install    -> put a StockSage shortcut on your Desktop
rem   start.bat phone      -> share to your phone over home Wi-Fi (QR code)
rem   start.bat web        -> open in a normal browser tab instead
rem   start.bat autopilot  -> learn automatically every weekday (off|status)
rem   start.bat daily      -> run the daily learn+scan cycle in the terminal
rem   start.bat <anything> -> passed through to the CLI (suggest, sectors, ...)

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [stocksage] Python 3 is required. Install from https://www.python.org/downloads/
    echo             and check "Add python.exe to PATH" during setup, then re-run.
    exit /b 1
)

if not exist .venv\Scripts\python.exe (
    echo [stocksage] First run: creating virtual environment...
    python -m venv .venv || exit /b 1
)
set VENV_PY=.venv\Scripts\python.exe

fc /b requirements.txt .venv\requirements.stamp >nul 2>nul
if errorlevel 1 (
    echo [stocksage] Installing dependencies - a few minutes on first run...
    %VENV_PY% -m pip install --quiet --upgrade pip || exit /b 1
    %VENV_PY% -m pip install --quiet -r requirements.txt || exit /b 1
    copy /y requirements.txt .venv\requirements.stamp >nul
    echo [stocksage] Dependencies ready.
)

if not exist .env (
    copy .env.example .env >nul
    echo [stocksage] Created .env - link Robinhood from the dashboard's Portfolio tab.
)

if "%~1"=="" (
    %VENV_PY% -m stocksage.desktop app
) else if "%~1"=="app" (
    %VENV_PY% -m stocksage.desktop app
) else if "%~1"=="web" (
    %VENV_PY% -m stocksage.desktop web
) else if "%~1"=="phone" (
    %VENV_PY% -m stocksage.desktop phone
) else if "%~1"=="install" (
    %VENV_PY% -m stocksage.desktop install
) else if "%~1"=="autopilot" (
    if "%~2"=="" (
        %VENV_PY% -m stocksage.autopilot on
    ) else (
        %VENV_PY% -m stocksage.autopilot %2
    )
) else (
    %VENV_PY% -m stocksage %*
)
