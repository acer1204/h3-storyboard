@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title H3 Storyboard

rem ============================================================
rem   H3 Storyboard - launcher
rem   Settings live in config.json (copy config.example.json).
rem   CLI flags on h3-server.py override config.json.
rem ============================================================

if not exist "h3-server.py" (
    echo [ERROR] h3-server.py not found
    goto :halt
)
if not exist "h3-batch-tester.html" (
    echo [ERROR] h3-batch-tester.html not found
    goto :halt
)
if not exist "config.json" (
    echo [INFO] config.json not found - creating from config.example.json
    copy /y "config.example.json" "config.json" >nul
    echo        edit config.json to point at your llama-server / ComfyUI, then re-run.
)

rem ---------- python ----------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [ERROR] Python 3 not found - https://www.python.org/downloads/
    goto :halt
)

rem ---------- read port + llama url from config.json ----------
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
    echo   server already running - opening %URL%
    start "" "%URL%"
    goto :end
)

start "" /min %PY% h3-server.py
timeout /t 2 /nobreak >nul
start "" "%URL%"
echo   opened %URL%
echo   (server window is minimized; close it to stop)
goto :end

:halt
pause
:end
endlocal
