@echo off
setlocal
cd /d "%~dp0"

set "URL=%~1"
if not defined URL (
    echo ==========================================================
    echo   mpv-iso-stream - play a Blu-ray ISO from the cloud
    echo   Paste an Alist/115 direct ISO link below, then press Enter.
    echo ==========================================================
    set /p "URL=ISO URL: "
)
if not defined URL (
    echo No URL provided.
    pause
    exit /b 1
)

set "PY="
if exist "%~dp0python.exe" set "PY=%~dp0python.exe"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3 was not found. Install Python or use MPV_lazy's bundled python.exe.
    pause
    exit /b 1
)

echo Parsing ISO and starting the local proxy...
"%PY%" "%~dp0iso_stream_proxy.py" "%URL%" --mpv --log-file "%~dp0iso-play-last.log"
pause
