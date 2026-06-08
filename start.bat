@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "ROOT_DIR=%CD%"
set "RUNTIME_DIR=%ROOT_DIR%\.runtime"
set "PID_FILE=%RUNTIME_DIR%\trpg_engine.pid"
set "RUNNER=%ROOT_DIR%\scripts\windows_run_server.ps1"

echo =====================================================
echo  Z.R.I.C TRPG Engine - one-click launcher
echo =====================================================
echo.

if not exist "%RUNTIME_DIR%" (
    mkdir "%RUNTIME_DIR%"
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY_BOOT=py -3"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        set "PY_BOOT=python"
    ) else (
        echo [ERROR] Python 3 was not found. Please install Python 3.10+ and retry.
        pause
        exit /b 1
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
) else (
    echo [1/3] Local virtual environment found.
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
echo      GM console: http://127.0.0.1:8000/
echo      Press Ctrl+C in this window to stop the server.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%" -PythonPath "%ROOT_DIR%\.venv\Scripts\python.exe" -ScriptPath "%ROOT_DIR%\main.py" -WorkingDirectory "%ROOT_DIR%" -PidFile "%PID_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Server stopped with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
