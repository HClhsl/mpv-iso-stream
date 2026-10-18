@echo off
setlocal
cd /d "%~dp0"

set "TARGET=%~1"
if not defined TARGET (
    echo ==========================================================
    echo   mpv-iso-stream installer
    echo   Enter the mpv / MPV_lazy folder that contains mpv.exe.
    echo   Example: D:\mpv-lazy-full
    echo ==========================================================
    set /p "TARGET=mpv folder: "
)
if not defined TARGET (
    echo No target folder provided.
    pause
    exit /b 1
)
if not exist "%TARGET%" (
    echo Folder not found: %TARGET%
    pause
    exit /b 1
)

copy /y "%~dp0iso_stream_proxy.py" "%TARGET%\" >nul
if errorlevel 1 goto :err
copy /y "%~dp0play-iso.bat" "%TARGET%\" >nul
if errorlevel 1 goto :err

if not exist "%TARGET%\portable_config\scripts" mkdir "%TARGET%\portable_config\scripts"
copy /y "%~dp0portable_config\scripts\iso_bd_play.lua" "%TARGET%\portable_config\scripts\" >nul
if errorlevel 1 goto :err
copy /y "%~dp0portable_config\scripts\iso_bd_chapters.lua" "%TARGET%\portable_config\scripts\" >nul
if errorlevel 1 goto :err

echo.
echo Installed successfully.
echo   - Copy an ISO link in your browser
echo   - Press Ctrl+b in mpv
echo   - Press Ctrl+m for the chapter menu while playing
echo.
pause
exit /b 0

:err
echo.
echo Installation failed. Check write permissions for the target folder.
pause
exit /b 1
