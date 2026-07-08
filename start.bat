@echo off
rem StockSage one-command launcher (Windows).
rem   start.bat            -> open the dashboard in your browser
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
    echo [stocksage] Starting the dashboard - press Ctrl+C to stop...
    %VENV_PY% -m streamlit run app.py --browser.gatherUsageStats false
) else (
    %VENV_PY% -m stocksage %*
)
