@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title H3 Storyboard

rem ============================================================
rem   H3 Storyboard - launcher
rem   Settings live in config.json (copy config.example.json).
rem   The server runs IN THIS WINDOW. Close it / Ctrl+C to stop.
rem ============================================================

if not exist "h3-server.py" (
    echo [ERROR] h3-server.py not found
    goto :halt
)
if not exist "h3-batch-tester.html" (
    echo [ERROR] h3-batch-tester.html not found
    goto :halt
)

rem ---------- python ----------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [ERROR] Python 3 not found - https://www.python.org/downloads/
    goto :halt
)

rem ---------- config.json ----------
if not exist "config.json" (
    copy /y "config.example.json" "config.json" >nul
    echo ============================================================
    echo   [FIRST RUN] config.json was created from config.example.json
    echo   It currently points at http://127.0.0.1:8080 which is
    echo   probably NOT your llama-server.
    echo.
    echo   Edit config.json now, then run this again.
    echo ============================================================
    notepad "config.json"
    goto :halt
)

rem ---------- read port + llama url ----------
set "PORT=9998"
set "LLAMA="
for /f "usebackq delims=" %%L in (`%PY% -c "import json;c=json.load(open('config.json',encoding='utf-8'));print(c.get('port',9998))"`) do set "PORT=%%L"
for /f "usebackq delims=" %%L in (`%PY% -c "import json;c=json.load(open('config.json',encoding='utf-8'));print(c.get('llama_url',''))"`) do set "LLAMA=%%L"

echo ============================================================
echo   H3 Storyboard
echo ============================================================
echo   config     : config.json
echo   llama      : %LLAMA%
echo   port       : %PORT%
echo.

rem ---------- already running? just open the page ----------
set "INUSE="
for /f "tokens=*" %%L in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "INUSE=1"
set "URL=http://127.0.0.1:%PORT%/"
if defined INUSE (
    echo   port %PORT% is already in use - a server is running.
    echo   If you just updated the code, CLOSE that old server window
    echo   first, then run this again. Opening the page now.
    echo.
    start "" "%URL%"
    goto :halt
)

rem ---------- open the page shortly after the server is up, then run the server HERE ----------
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start "" "%URL%""
echo   starting server in this window - keep it open. Ctrl+C to stop.
echo ------------------------------------------------------------
%PY% h3-server.py
echo.
echo   server stopped.
goto :halt

:halt
pause
endlocal
