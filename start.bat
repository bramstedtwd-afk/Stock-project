@echo off
rem StockSage one-command launcher (Windows).
rem   .\start.bat            -> open StockSage in its own app window
rem   .\start.bat setup      -> new machine? guided setup: brain, settings, checks
rem   .\start.bat install    -> put a StockSage shortcut on your Desktop
rem   .\start.bat phone      -> share to your phone over home Wi-Fi (QR code)
rem   .\start.bat web        -> open in a normal browser tab instead
rem   .\start.bat update     -> pull the latest code improvements from your repo
rem   .\start.bat doctor     -> check everything that can go wrong, with fixes
rem   .\start.bat security   -> who has signed in to your broker account, and how
rem                           exposed the stored login is (--signin 8:30am)
rem   .\start.bat leave      -> done with this computer: remove every scheduled
rem                             job, credential, token and log stored on it
rem   .\start.bat autopilot  -> learn automatically every weekday (off|status)
rem   .\start.bat daily      -> run the daily learn+scan cycle in the terminal
rem   .\start.bat <anything> -> passed through to the CLI (suggest, sectors, ...)

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [stocksage] Python 3 is required. Install from https://www.python.org/downloads/
    echo             and check "Add python.exe to PATH" during setup, then re-run.
    exit /b 1
)

rem BEGIN heal-venv
rem A virtualenv is a pointer to the Python it was built from. If that Python
rem is later moved or removed the pointer survives but nothing runs, and the
rem exist check below would never rebuild it.
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo [stocksage] The Python this setup was built on has been moved or removed - rebuilding it...
        rmdir /s /q .venv
    )
)
rem END heal-venv

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
) else if "%~1"=="update" (
    rem update can replace this very file. cmd reads a batch file by byte
    rem position while it runs, so carrying on after a replacement resumes
    rem in the middle of the NEW file and runs a stray fragment of it.
    rem Leave before cmd reads another line.
    %VENV_PY% -m stocksage.update || exit /b 1
    exit /b 0
) else if "%~1"=="doctor" (
    %VENV_PY% -m stocksage.doctor
) else if "%~1"=="autopilot" (
    if "%~2"=="" (
        %VENV_PY% -m stocksage.autopilot on
    ) else (
        %VENV_PY% -m stocksage.autopilot %2 %3
    )
) else (
    %VENV_PY% -m stocksage %*
)
