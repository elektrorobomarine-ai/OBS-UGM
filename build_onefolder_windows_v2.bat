@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  GRC-UGM-PERTAMINA OBS - Windows OneFolder Build v2
echo ============================================================
echo.

set "PY311="
set "PY311_DESC="

REM ============================================================
REM 0. Reuse a valid Python 3.11 virtual environment if present.
REM ============================================================

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 (
        echo [0/6] Existing Python 3.11 virtual environment found.
        goto :venv_ready
    ) else (
        echo [0/6] Existing .venv is not Python 3.11. Recreating it...
        rmdir /S /Q ".venv"
    )
)

REM ============================================================
REM 1. Find Python 3.11.
REM ============================================================

echo [1/6] Looking for Python 3.11...

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

REM ============================================================
REM Python 3.11 not found: offer automatic installation.
REM ============================================================

echo.
echo Python 3.11 64-bit is not installed.
echo This build is intentionally pinned to Python 3.11 because PySide6,
echo rasterio, ObsPy and PyInstaller are most predictable together on 3.11.
echo.

where winget >nul 2>nul
if errorlevel 1 goto :manual_python_install

choice /C YN /N /M "Install Python 3.11 automatically with winget? [Y/N]: "
if errorlevel 2 goto :manual_python_install

echo.
echo Installing Python 3.11...
winget install --id Python.Python.3.11 -e --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo.
    echo ERROR: winget could not install Python 3.11.
    goto :manual_python_install
)

REM Prefer the normal per-user installation path after winget.
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PY311=%LocalAppData%\Programs\Python\Python311\python.exe"
    set "PY311_DESC=winget Python 3.11"
    goto :python_found
)

REM Some installations register with the launcher immediately.
where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys; print(sys.executable)" > "%TEMP%\obs_py311_path.txt" 2>nul
    if not errorlevel 1 (
        set /p PY311=<"%TEMP%\obs_py311_path.txt"
        set "PY311_DESC=winget Python 3.11 via launcher"
        del /Q "%TEMP%\obs_py311_path.txt" >nul 2>nul
        goto :python_found
    )
)

echo.
echo Python installation completed but this command window cannot find it yet.
echo Close this window and run build_onefolder_windows_v2.bat again.
pause
exit /b 1

:manual_python_install
echo.
echo ============================================================
echo  PYTHON 3.11 REQUIRED
echo ============================================================
echo Install 64-bit Python 3.11, then run this BAT again.
echo.
echo Recommended command if winget is available:
echo   winget install --id Python.Python.3.11 -e --scope user
echo.
echo You DO NOT need to uninstall another Python version.
echo Python 3.11 may be installed side-by-side.
echo.
pause
exit /b 1

:python_found
echo Found: %PY311_DESC%
echo Path : %PY311%
echo.

REM Validate exact version and 64-bit.
"%PY311%" -c "import sys,struct; print('Python',sys.version.split()[0],'-',struct.calcsize('P')*8,'bit'); raise SystemExit(0 if sys.version_info[:2]==(3,11) and struct.calcsize('P')*8==64 else 1)"
if errorlevel 1 (
    echo ERROR: A 64-bit Python 3.11 runtime is required.
    goto :fail
)

echo [2/6] Creating Python 3.11 virtual environment...
"%PY311%" -m venv ".venv"
if errorlevel 1 goto :fail

:venv_ready
call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :fail

python -c "import sys,struct; print('Build interpreter:',sys.executable); print('Python:',sys.version.split()[0],struct.calcsize('P')*8,'bit')"
if errorlevel 1 goto :fail

if /I "%~1"=="--skip-install" goto :preflight

echo.
echo [3/6] Updating pip/build tools...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

echo.
echo [4/6] Installing runtime/build dependencies...
python -m pip install -r requirements-onefolder.txt
if errorlevel 1 goto :fail

REM Optional NVIDIA/CUDA acceleration:
REM python -m pip install cupy-cuda12x

:preflight
echo.
echo [5/6] Running source/dependency preflight...
python preflight_check.py
if errorlevel 1 goto :fail

echo.
echo [6/6] Building one-folder application...
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
echo EXE:
echo   %CD%\dist\GRC_UGM_PERTAMINA_OBS\GRC_UGM_PERTAMINA_OBS.exe
echo.
echo Keep obs_settings.ini beside the EXE.
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo  BUILD FAILED
echo ============================================================
echo Review the error above.
echo.
pause
exit /b 1
