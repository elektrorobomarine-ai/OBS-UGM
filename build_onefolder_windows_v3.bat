@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  GRC-UGM-PERTAMINA OBS - Windows OneFolder Build v3
REM  Long-path-safe build using SUBST short working drive.
REM ============================================================

set "ORIGINAL_DIR=%~dp0"
if "%ORIGINAL_DIR:~-1%"=="\" set "ORIGINAL_DIR=%ORIGINAL_DIR:~0,-1%"

echo ============================================================
echo  GRC-UGM-PERTAMINA OBS - Windows OneFolder Build v3
echo ============================================================
echo.
echo Source folder:
echo   %ORIGINAL_DIR%
echo.

REM ============================================================
REM 0. Create a short drive alias to avoid Windows MAX_PATH
REM    failures in PySide6_Essentials / Qt QML package paths.
REM ============================================================

set "BUILD_DRIVE="

for %%D in (R S T U V W X Y Z) do (
    if not defined BUILD_DRIVE (
        if not exist %%D:\ (
            subst %%D: "%ORIGINAL_DIR%" >nul 2>nul
            if not errorlevel 1 (
                set "BUILD_DRIVE=%%D:"
            )
        )
    )
)

if not defined BUILD_DRIVE (
    echo ERROR: Could not create a temporary SUBST drive.
    echo.
    echo Please move/extract this package to a short folder such as:
    echo   C:\OBS_BUILD
    echo and run the BAT again.
    goto :fail_no_cleanup
)

echo [0/7] Short build path created:
echo   %BUILD_DRIVE%\  -^>  %ORIGINAL_DIR%
echo.

cd /d "%BUILD_DRIVE%\"
if errorlevel 1 goto :fail

REM Use a short venv path on the substituted drive.
set "VENV_DIR=%BUILD_DRIVE%\.venv"

REM Disable pip cache during the build. This avoids additional deeply nested
REM cache paths and keeps the installation path predictable.
set "PIP_NO_CACHE_DIR=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

set "PY311="
set "PY311_DESC="

REM ============================================================
REM 1. Reuse valid Python 3.11 venv if present.
REM ============================================================

if exist "%VENV_DIR%\Scripts\python.exe" (
    "%VENV_DIR%\Scripts\python.exe" -c "import sys,struct; raise SystemExit(0 if sys.version_info[:2]==(3,11) and struct.calcsize('P')*8==64 else 1)" >nul 2>nul
    if not errorlevel 1 (
        echo [1/7] Existing Python 3.11 64-bit virtual environment found.
        goto :venv_ready
    ) else (
        echo [1/7] Existing .venv is not Python 3.11 64-bit. Recreating...
        rmdir /S /Q "%VENV_DIR%"
    )
)

REM ============================================================
REM 2. Find Python 3.11 64-bit.
REM ============================================================

echo [2/7] Looking for Python 3.11 64-bit...

where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys; print(sys.executable)" > "%TEMP%\obs_py311_path.txt" 2>nul
    if not errorlevel 1 (
        set /p PY311=<"%TEMP%\obs_py311_path.txt"
        set "PY311_DESC=Python Launcher py -3.11"
        del /Q "%TEMP%\obs_py311_path.txt" >nul 2>nul
        goto :python_found
    )
)

if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PY311=%LocalAppData%\Programs\Python\Python311\python.exe"
    set "PY311_DESC=LocalAppData Python 3.11"
    goto :python_found
)

if exist "%ProgramFiles%\Python311\python.exe" (
    set "PY311=%ProgramFiles%\Python311\python.exe"
    set "PY311_DESC=Program Files Python 3.11"
    goto :python_found
)

if exist "%ProgramFiles(x86)%\Python311\python.exe" (
    set "PY311=%ProgramFiles(x86)%\Python311\python.exe"
    set "PY311_DESC=Program Files x86 Python 3.11"
    goto :python_found
)

echo.
echo Python 3.11 64-bit is not installed.
echo.

where winget >nul 2>nul
if errorlevel 1 goto :manual_python_install

choice /C YN /N /M "Install Python 3.11 automatically with winget? [Y/N]: "
if errorlevel 2 goto :manual_python_install

echo.
echo Installing Python 3.11...
winget install --id Python.Python.3.11 -e --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :manual_python_install

if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PY311=%LocalAppData%\Programs\Python\Python311\python.exe"
    set "PY311_DESC=winget Python 3.11"
    goto :python_found
)

echo.
echo Python installation completed, but this command window cannot see it yet.
echo Close this window and run build_onefolder_windows.bat again.
goto :fail

:manual_python_install
echo.
echo Install 64-bit Python 3.11, then run this BAT again.
echo Recommended:
echo   winget install --id Python.Python.3.11 -e --scope user
goto :fail

:python_found
echo Found: %PY311_DESC%
echo Path : %PY311%
echo.

"%PY311%" -c "import sys,struct; print('Python',sys.version.split()[0],'-',struct.calcsize('P')*8,'bit'); raise SystemExit(0 if sys.version_info[:2]==(3,11) and struct.calcsize('P')*8==64 else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.11 64-bit is required.
    goto :fail
)

REM ============================================================
REM 3. Create virtual environment on SHORT path.
REM ============================================================

echo [3/7] Creating virtual environment at:
echo   %VENV_DIR%
"%PY311%" -m venv "%VENV_DIR%"
if errorlevel 1 goto :fail

:venv_ready
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :fail

python -c "import sys,struct; print('Build interpreter:',sys.executable); print('Python:',sys.version.split()[0],struct.calcsize('P')*8,'bit')"
if errorlevel 1 goto :fail

if /I "%~1"=="--skip-install" goto :preflight

echo.
echo [4/7] Updating pip/build tools...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

echo.
echo [5/7] Installing runtime/build dependencies...
echo NOTE: Build path is intentionally short to prevent PySide6 Long Path errors.
python -m pip install --no-cache-dir -r requirements-onefolder.txt
if errorlevel 1 goto :fail

:preflight
echo.
echo [6/7] Running source/dependency preflight...
python preflight_check.py
if errorlevel 1 goto :fail

echo.
echo [7/7] Building one-folder application...
python -m PyInstaller --noconfirm --clean GRC_UGM_PERTAMINA_OBS.spec
if errorlevel 1 goto :fail

echo.
echo Finalizing external configuration...
copy /Y "obs_settings.ini" "dist\GRC_UGM_PERTAMINA_OBS\obs_settings.ini" >nul

if not exist "dist\GRC_UGM_PERTAMINA_OBS\logs" (
    mkdir "dist\GRC_UGM_PERTAMINA_OBS\logs"
)

if not exist "dist\GRC_UGM_PERTAMINA_OBS\recordings" (
    mkdir "dist\GRC_UGM_PERTAMINA_OBS\recordings"
)

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ============================================================
echo.
echo Output folder:
echo   %ORIGINAL_DIR%\dist\GRC_UGM_PERTAMINA_OBS
echo.
echo EXE:
echo   %ORIGINAL_DIR%\dist\GRC_UGM_PERTAMINA_OBS\GRC_UGM_PERTAMINA_OBS.exe
echo.

cd /d "%TEMP%" >nul 2>nul
subst %BUILD_DRIVE% /D >nul 2>nul

pause
exit /b 0

:fail
echo.
echo ============================================================
echo  BUILD FAILED
echo ============================================================
echo Review the error above.
echo.
cd /d "%TEMP%" >nul 2>nul
if defined BUILD_DRIVE subst %BUILD_DRIVE% /D >nul 2>nul
pause
exit /b 1

:fail_no_cleanup
echo.
echo ============================================================
echo  BUILD FAILED
echo ============================================================
echo Review the error above.
echo.
pause
exit /b 1
