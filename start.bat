@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "ROOT_DIR=%CD%"
set "START_PORT=%~1"

if "%START_PORT%"=="" (
    set "START_PORT=8000"
)

echo %START_PORT%| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERROR] Invalid port: %START_PORT%
    echo Usage: start.bat [port]
    pause
    exit /b 1
)
if %START_PORT% GTR 65535 (
    echo [ERROR] Invalid port: %START_PORT%
    echo Usage: start.bat [port]
    pause
    exit /b 1
)

echo =====================================================
echo  Z.R.I.C TRPG Engine - one-click launcher
echo =====================================================
echo.

set "PY_BOOT="

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
    set "PY_BOOT=py -3"
    )
)

if not defined PY_BOOT (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
        set "PY_BOOT=python"
        )
    )
)

if not defined PY_BOOT (
    echo [ERROR] Usable Python 3.10+ was not found. Please install Python 3.10+ and retry.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    echo [1/3] Checking local virtual environment...
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [1/3] Existing .venv is not usable on Windows; rebuilding it...
        rmdir /s /q ".venv"
        if exist ".venv" (
            echo [ERROR] Failed to remove the broken .venv. Close any terminals using it and retry.
            pause
            exit /b 1
        )
    ) else (
        echo [1/3] Local virtual environment found.
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating local virtual environment...
    %PY_BOOT% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv.
        pause
        exit /b 1
    )
)

echo [2/3] Installing or updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

echo.
echo [3/3] Starting Z.R.I.C TRPG Engine...
echo      GM console: http://127.0.0.1:%START_PORT%/
echo      Press Ctrl+C in this window to stop the server.
echo.

".venv\Scripts\python.exe" "%ROOT_DIR%\main.py" --port %START_PORT%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Server stopped with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
