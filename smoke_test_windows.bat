@echo off
setlocal
set APP=%~dp0dist\GRC_UGM_PERTAMINA_OBS\GRC_UGM_PERTAMINA_OBS.exe
if not exist "%APP%" (
    echo EXE not found. Run build_onefolder_windows.bat first.
    pause
    exit /b 1
)

echo Starting main application...
start "" "%APP%"
timeout /t 2 >nul

echo You can also test a module directly, for example:
echo   "%APP%" --module geophone_fft
echo   "%APP%" --module position
echo   "%APP%" --module miniseed_recording
pause
