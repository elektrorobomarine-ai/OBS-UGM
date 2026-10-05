@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Build GRC-UGM-PERTAMINA OBS - FAST CPU Single OBS.exe

REM ============================================================================
REM GRC-UGM-PERTAMINA OBS Runtime
REM SINGLE PHYSICAL APPLICATION FILE
REM
REM Output:
REM   dist\OBS.exe
REM
REM OBS modules remain multi-process, but every OBS module is launched by
REM relaunching the SAME physical OBS.exe with:
REM   OBS.exe --obs-module <module.py>
REM
REM shared_data.py, shared_data_v3.py and obs_ipc.py remain internal libraries.
REM Source asli TIDAK diubah. All frozen-build patches happen in _build_src.
REM ============================================================================

cd /d "%~dp0"
set "ROOT=%CD%"
set "STAGE=%ROOT%\_build_src"
set "DIST=%ROOT%\dist"
set "WORK=%ROOT%\build\pyinstaller_work_single"
set "SPECDIR=%ROOT%\build\spec_single"

echo.
echo ============================================================
echo   GRC-UGM-PERTAMINA OBS - FAST CPU SINGLE OBS.exe
echo ============================================================
echo Root : %ROOT%
echo.

REM ---------------------------------------------------------------------------
REM 1. Select Python exactly like build_obs_runtime_v2.bat
REM Priority: myenv -> .venv -> venv -> PATH
REM ---------------------------------------------------------------------------
set "PY="

if exist "%ROOT%\myenv\Scripts\python.exe" set "PY=%ROOT%\myenv\Scripts\python.exe"
if not defined PY if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY if exist "%ROOT%\venv\Scripts\python.exe" set "PY=%ROOT%\venv\Scripts\python.exe"

if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY set "PY=%%P"
    )
)

if not defined PY (
    echo [ERROR] Python tidak ditemukan.
    echo Aktifkan virtual environment OBS Runtime terlebih dahulu.
    goto :FAIL
)

echo Python:
"%PY%" --version
if errorlevel 1 goto :FAIL
echo Interpreter: %PY%
echo.

REM ---------------------------------------------------------------------------
REM 2. Verify canonical source files
REM ---------------------------------------------------------------------------
set "MISSING=0"

for %%F in (
    main.py
    obs_setting.py
    camera.py
    position.py
    geophone.py
    geophone_realtime.py
    geophone_fft.py
    geophone_spectrogram.py
    geophone_3d.py
    geophone_imu.py
    geophone_hodogram.py
    geophone_quality.py
    geophone_event.py
    geophone_psd.py
    other_sensors.py
    miniseed_recording.py
    shared_data.py
    shared_data_v3.py
    obs_ipc.py
    obs_health_check.py
) do (
    if not exist "%ROOT%\%%F" (
        echo [MISSING] %%F
        set "MISSING=1"
    ) else (
        echo [OK] %%F
    )
)

if "!MISSING!"=="1" (
    echo.
    echo [ERROR] Ada source file yang belum tersedia.
    goto :FAIL
)

if not exist "%ROOT%\assets" (
    echo [ERROR] Folder assets tidak ditemukan:
    echo         %ROOT%\assets
    goto :FAIL
)

echo [OK] Source dan assets tersedia.
echo.

REM ---------------------------------------------------------------------------
REM 3. Dependency check from v2
REM ---------------------------------------------------------------------------
echo [CHECK] Required runtime dependencies...
"%PY%" -c "import PySide6, numpy, serial, pyqtgraph, obspy, cv2, PIL; from PySide6 import QtWebEngineWidgets, QtWebEngineCore, QtMultimedia, QtMultimediaWidgets, QtOpenGLWidgets"
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency REQUIRED OBS Runtime belum lengkap.
    echo Paket utama:
    echo   PySide6 numpy pyserial pyqtgraph obspy opencv-python pillow
    goto :FAIL
)
echo [OK] Required runtime dependencies.
echo.

REM Optional PyOpenGL.
set "OPENGL_OK=0"
"%PY%" -c "import OpenGL" >nul 2>&1
if not errorlevel 1 (
    set "OPENGL_OK=1"
    echo [OK] Optional PyOpenGL available.
) else (
    echo [WARN] PyOpenGL unavailable. 3D follows source fallback.
)
echo.

REM Optional Rasterio / GeoTIFF.
set "RASTERIO_OK=0"
"%PY%" -c "import rasterio; from rasterio.enums import Resampling; from rasterio.transform import from_bounds; from rasterio.warp import reproject, transform_bounds" >nul 2>&1
if not errorlevel 1 (
    set "RASTERIO_OK=1"
    echo [OK] Optional Rasterio/GeoTIFF available.
) else (
    echo [WARN] Rasterio tidak dapat di-import.
    echo        Build tetap lanjut; Position online/GNSS/USBL tetap tersedia.
    echo        GeoTIFF overlay unavailable pada build ini.
)
echo.

REM ---------------------------------------------------------------------------
REM 4. PyInstaller
REM ---------------------------------------------------------------------------
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    "%PY%" -m pip install --upgrade pyinstaller pyinstaller-hooks-contrib
    if errorlevel 1 goto :FAIL
)

echo [INFO] PyInstaller:
"%PY%" -m PyInstaller --version
if errorlevel 1 goto :FAIL
echo.

REM ---------------------------------------------------------------------------
REM 5. Clean
REM ---------------------------------------------------------------------------
echo [CLEAN] Preparing staging/build folders...

if exist "%STAGE%" rmdir /s /q "%STAGE%"
if exist "%DIST%" rmdir /s /q "%DIST%"
if exist "%WORK%" rmdir /s /q "%WORK%"
if exist "%SPECDIR%" rmdir /s /q "%SPECDIR%"

mkdir "%STAGE%" || goto :FAIL
mkdir "%DIST%" || goto :FAIL
mkdir "%WORK%" || goto :FAIL
mkdir "%SPECDIR%" || goto :FAIL
mkdir "%STAGE%\defaults" || goto :FAIL

REM ---------------------------------------------------------------------------
REM 6. Copy canonical source to staging
REM ---------------------------------------------------------------------------
for %%F in (
    main.py
    obs_setting.py
    camera.py
    position.py
    geophone.py
    geophone_realtime.py
    geophone_fft.py
    geophone_spectrogram.py
    geophone_3d.py
    geophone_imu.py
    geophone_hodogram.py
    geophone_quality.py
    geophone_event.py
    geophone_psd.py
    other_sensors.py
    miniseed_recording.py
    shared_data.py
    shared_data_v3.py
    obs_ipc.py
    obs_health_check.py
) do (
    copy /y "%ROOT%\%%F" "%STAGE%\%%F" >nul || goto :FAIL
)

REM Assets are embedded inside OBS.exe.
xcopy "%ROOT%\assets" "%STAGE%\assets\" /E /I /H /Y >nul
if errorlevel 2 goto :FAIL

REM Existing INI files become first-run defaults embedded inside OBS.exe.
REM They are copied beside OBS.exe only if the external file does not exist yet.
for %%F in ("%ROOT%\*.ini") do (
    if exist "%%~fF" copy /y "%%~fF" "%STAGE%\defaults\%%~nxF" >nul
)

REM Keep defaults directory non-empty so PyInstaller always accepts it.
> "%STAGE%\defaults\README.txt" echo Embedded first-run configuration defaults.

REM ---------------------------------------------------------------------------
REM 7. Build-only patcher
REM Keep BASE_DIR at _MEIPASS for bundled assets / source checks.
REM Redirect ONLY writable files/folders to the directory containing OBS.exe.
REM ---------------------------------------------------------------------------
echo [PATCH] Preparing one-file runtime paths...

set "PATCH_B64=ZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCmltcG9ydCByZQppbXBvcnQgc3lzCgpzdGFnZSA9IFBhdGgoc3lzLmFyZ3ZbMV0pLnJlc29sdmUoKQoKZGVmIHJlYWQobmFtZSk6CiAgICByZXR1cm4gKHN0YWdlIC8gbmFtZSkucmVhZF90ZXh0KGVuY29kaW5nPSJ1dGYtOC1zaWciKQoKZGVmIHdyaXRlKG5hbWUsIHRleHQpOgogICAgKHN0YWdlIC8gbmFtZSkud3JpdGVfdGV4dCh0ZXh0LCBlbmNvZGluZz0idXRmLTgiKQoKZGVmIGVuc3VyZV9ydW50aW1lX2RpcihuYW1lKToKICAgIHRleHQgPSByZWFkKG5hbWUpCiAgICBpZiAiUlVOVElNRV9ESVIgPSIgaW4gdGV4dDoKICAgICAgICByZXR1cm4gdGV4dAogICAgbWFya2VyID0gIkJBU0VfRElSID0gUGF0aChfX2ZpbGVfXykucmVzb2x2ZSgpLnBhcmVudCIKICAgIGlmIG1hcmtlciBub3QgaW4gdGV4dDoKICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoZiJ7bmFtZX06IEJBU0VfRElSIG1hcmtlciBub3QgZm91bmQiKQogICAgcnVudGltZSA9ICgKICAgICAgICBtYXJrZXIKICAgICAgICArICJcblxuUlVOVElNRV9ESVIgPSAoXG4iCiAgICAgICAgICAiICAgIFBhdGgoc3lzLmV4ZWN1dGFibGUpLnJlc29sdmUoKS5wYXJlbnRcbiIKICAgICAgICAgICIgICAgaWYgZ2V0YXR0cihzeXMsIFwiZnJvemVuXCIsIEZhbHNlKVxuIgogICAgICAgICAgIiAgICBlbHNlIEJBU0VfRElSXG4iCiAgICAgICAgICAiKVxuIgogICAgKQogICAgcmV0dXJuIHRleHQucmVwbGFjZShtYXJrZXIsIHJ1bnRpbWUsIDEpCgojIG9ic19zZXR0aW5nLnB5CnRleHQgPSBlbnN1cmVfcnVudGltZV9kaXIoIm9ic19zZXR0aW5nLnB5IikKdGV4dCA9IHRleHQucmVwbGFjZSgKICAgICdJTklfUEFUSCA9IEJBU0VfRElSIC8gIm9ic19zZXR0aW5ncy5pbmkiJywKICAgICdJTklfUEFUSCA9IFJVTlRJTUVfRElSIC8gIm9ic19zZXR0aW5ncy5pbmkiJywKKQp0ZXh0ID0gdGV4dC5yZXBsYWNlKAogICAgJ0xPR19ESVIgPSBCQVNFX0RJUiAvICJsb2dzIicsCiAgICAnTE9HX0RJUiA9IFJVTlRJTUVfRElSIC8gImxvZ3MiJywKKQp0ZXh0ID0gdGV4dC5yZXBsYWNlKAogICAgJ0JBU0VfRElSIC8gInJlY29yZGluZ3MiJywKICAgICdSVU5USU1FX0RJUiAvICJyZWNvcmRpbmdzIicsCikKd3JpdGUoIm9ic19zZXR0aW5nLnB5IiwgdGV4dCkKCiMgb3RoZXJfc2Vuc29ycy5weQp0ZXh0ID0gZW5zdXJlX3J1bnRpbWVfZGlyKCJvdGhlcl9zZW5zb3JzLnB5IikKdGV4dCA9IHRleHQucmVwbGFjZSgKICAgICdJTVVfT0ZGU0VUX0lOSSA9IEJBU0VfRElSIC8gImltdV9vZmZzZXRzLmluaSInLAogICAgJ0lNVV9PRkZTRVRfSU5JID0gUlVOVElNRV9ESVIgLyAiaW11X29mZnNldHMuaW5pIicsCikKdGV4dCA9IHRleHQucmVwbGFjZSgKICAgICdMRUdBQ1lfT0JTX1NFVFRJTkdTX0lOSSA9IEJBU0VfRElSIC8gIm9ic19zZXR0aW5ncy5pbmkiJywKICAgICdMRUdBQ1lfT0JTX1NFVFRJTkdTX0lOSSA9IFJVTlRJTUVfRElSIC8gIm9ic19zZXR0aW5ncy5pbmkiJywKKQp3cml0ZSgib3RoZXJfc2Vuc29ycy5weSIsIHRleHQpCgojIG1pbmlzZWVkX3JlY29yZGluZy5weQp0ZXh0ID0gZW5zdXJlX3J1bnRpbWVfZGlyKCJtaW5pc2VlZF9yZWNvcmRpbmcucHkiKQp0ZXh0ID0gdGV4dC5yZXBsYWNlKAogICAgJ09CU19TRVRUSU5HU19JTkkgPSBCQVNFX0RJUiAvICJvYnNfc2V0dGluZ3MuaW5pIicsCiAgICAnT0JTX1NFVFRJTkdTX0lOSSA9IFJVTlRJTUVfRElSIC8gIm9ic19zZXR0aW5ncy5pbmkiJywKKQp0ZXh0ID0gdGV4dC5yZXBsYWNlKAogICAgJ0lNVV9PRkZTRVRfSU5JID0gQkFTRV9ESVIgLyAiaW11X29mZnNldHMuaW5pIicsCiAgICAnSU1VX09GRlNFVF9JTkkgPSBSVU5USU1FX0RJUiAvICJpbXVfb2Zmc2V0cy5pbmkiJywKKQoKIyBEZWZhdWx0L2ZhbGxiYWNrIE1pbmlTRUVEIGZvbGRlci4KdGV4dCA9IHJlLnN1YigKICAgIHInZmFsbGJhY2tccyo9XHMqXChccypCQVNFX0RJUlxzKi9ccyoicmVjb3JkaW5ncyJccyovXHMqIm1pbmlzZWVkIlxzKlwpJywKICAgICdmYWxsYmFjayA9IChSVU5USU1FX0RJUiAvICJyZWNvcmRpbmdzIiAvICJtaW5pc2VlZCIpJywKICAgIHRleHQsCiAgICBjb3VudD0xLAopCgojIFJlbGF0aXZlIGNvbmZpZ3VyZWQgcmVjb3JkIHBhdGggc2hvdWxkIHJlc29sdmUgYmVzaWRlIE9CUy5leGUsIG5vdCBfTUVJUEFTUy4KdGV4dCA9IHJlLnN1YigKICAgIHIncGF0aFxzKj1ccypcKFxzKkJBU0VfRElSXHMqL1xzKnBhdGhccypcKScsCiAgICAncGF0aCA9IChSVU5USU1FX0RJUiAvIHBhdGgpJywKICAgIHRleHQsCiAgICBjb3VudD0xLAopCndyaXRlKCJtaW5pc2VlZF9yZWNvcmRpbmcucHkiLCB0ZXh0KQoKCiMgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiMgRkFTVCBDUFUgQlVJTEQgUEFUQ0gKIyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KZGVmIGZvcmNlX2Zhc3RfY3B1X2ZmdCgpOgogICAgbmFtZSA9ICJnZW9waG9uZV9mZnQucHkiCiAgICB0ZXh0ID0gcmVhZChuYW1lKQoKICAgIG1hcmtlciA9ICIgICAgZXJyb3JzID0gW10iCiAgICBjcHVfcmV0dXJuID0gKAogICAgICAgICcgICAgcmV0dXJuIChGYWxzZSwgIkZhc3QgQ1BVIGJ1aWxkIHwgTnVtUHkgRkZUIiwgMCwgIm51bXB5IilcblxuJwogICAgICAgICcgICAgIyBVbnJlYWNoYWJsZSBjb21wYXRpYmlsaXR5IGNvZGUgcmV0YWluZWQgYmVsb3cuXG4nCiAgICAgICAgJyAgICBlcnJvcnMgPSBbXScKICAgICkKICAgIGlmIG1hcmtlciBub3QgaW4gdGV4dDoKICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoZiJ7bmFtZX06IENVREEgZGV0ZWN0b3IgbWFya2VyIG5vdCBmb3VuZCIpCiAgICB0ZXh0ID0gdGV4dC5yZXBsYWNlKG1hcmtlciwgY3B1X3JldHVybiwgMSkKCiAgICBvbGRfdWkgPSAoCiAgICAgICAgJyAgICAgICAgc2VsZi5iYWNrZW5kX2NvbWJvID0gUUNvbWJvQm94KClcblxuJwogICAgICAgICcgICAgICAgIHNlbGYuYmFja2VuZF9jb21iby5hZGRJdGVtKFxuJwogICAgICAgICcgICAgICAgICAgICAiQXV0byAoQ1VEQSBwcmVmZXJyZWQpIlxuJwogICAgICAgICcgICAgICAgIClcblxuJwogICAgICAgICcgICAgICAgIHNlbGYuYmFja2VuZF9jb21iby5hZGRJdGVtKFxuJwogICAgICAgICcgICAgICAgICAgICAiQ1VEQSAvIFB5VG9yY2ggLyB0b3JjaC5mZnQiXG4nCiAgICAgICAgJyAgICAgICAgKVxuXG4nCiAgICAgICAgJyAgICAgICAgc2VsZi5iYWNrZW5kX2NvbWJvLmFkZEl0ZW0oXG4nCiAgICAgICAgJyAgICAgICAgICAgICJDVURBIC8gQ3VQeSAvIGN1RkZUIlxuJwogICAgICAgICcgICAgICAgIClcblxuJwogICAgICAgICcgICAgICAgIHNlbGYuYmFja2VuZF9jb21iby5hZGRJdGVtKFxuJwogICAgICAgICcgICAgICAgICAgICAiQ1BVIC8gTnVtUHkgRkZUIlxuJwogICAgICAgICcgICAgICAgIClcbicKICAgICkKICAgIG5ld191aSA9ICgKICAgICAgICAnICAgICAgICBzZWxmLmJhY2tlbmRfY29tYm8gPSBRQ29tYm9Cb3goKVxuJwogICAgICAgICcgICAgICAgIHNlbGYuYmFja2VuZF9jb21iby5hZGRJdGVtKFxuJwogICAgICAgICcgICAgICAgICAgICAiQ1BVIC8gTnVtUHkgRkZUIlxuJwogICAgICAgICcgICAgICAgIClcbicKICAgICkKICAgIGlmIG9sZF91aSBub3QgaW4gdGV4dDoKICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoZiJ7bmFtZX06IEZGVCBiYWNrZW5kIFVJIGJsb2NrIG5vdCBmb3VuZCIpCiAgICB0ZXh0ID0gdGV4dC5yZXBsYWNlKG9sZF91aSwgbmV3X3VpLCAxKQoKICAgIHdyaXRlKG5hbWUsIHRleHQpCiAgICBwcmludCgiW0ZBU1QgQ1BVXSBnZW9waG9uZV9mZnQucHkgLT4gTnVtUHktb25seSBiYWNrZW5kIikKCgpkZWYgZm9yY2VfZmFzdF9jcHVfc3BlY3Ryb2dyYW0oKToKICAgIG5hbWUgPSAiZ2VvcGhvbmVfc3BlY3Ryb2dyYW0ucHkiCiAgICB0ZXh0ID0gcmVhZChuYW1lKQoKICAgIG1hcmtlciA9ICIgICAgZXJyb3JzID0gW10iCiAgICBjcHVfcmV0dXJuID0gKAogICAgICAgICcgICAgcmV0dXJuIChGYWxzZSwgIkZhc3QgQ1BVIGJ1aWxkIHwgTnVtUHkgU1RGVCIsIDAsICJudW1weSIpXG5cbicKICAgICAgICAnICAgICMgVW5yZWFjaGFibGUgY29tcGF0aWJpbGl0eSBjb2RlIHJldGFpbmVkIGJlbG93LlxuJwogICAgICAgICcgICAgZXJyb3JzID0gW10nCiAgICApCiAgICBpZiBtYXJrZXIgbm90IGluIHRleHQ6CiAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKGYie25hbWV9OiBDVURBIGRldGVjdG9yIG1hcmtlciBub3QgZm91bmQiKQogICAgdGV4dCA9IHRleHQucmVwbGFjZShtYXJrZXIsIGNwdV9yZXR1cm4sIDEpCgogICAgb2xkX3VpID0gKAogICAgICAgICcgICAgICAgIHNlbGYuYmFja2VuZF9jb21ibyA9IFFDb21ib0JveCgpXG4nCiAgICAgICAgJyAgICAgICAgc2VsZi5iYWNrZW5kX2NvbWJvLmFkZEl0ZW1zKFxuJwogICAgICAgICcgICAgICAgICAgICBbXG4nCiAgICAgICAgJyAgICAgICAgICAgICAgICAiQXV0byAoQ1VEQSBwcmVmZXJyZWQpIixcbicKICAgICAgICAnICAgICAgICAgICAgICAgICJDVURBIC8gUHlUb3JjaCAvIHRvcmNoLmZmdCIsXG4nCiAgICAgICAgJyAgICAgICAgICAgICAgICAiQ1VEQSAvIEN1UHkgLyBjdUZGVCIsXG4nCiAgICAgICAgJyAgICAgICAgICAgICAgICAiQ1BVIC8gTnVtUHkgU1RGVCIsXG4nCiAgICAgICAgJyAgICAgICAgICAgIF1cbicKICAgICAgICAnICAgICAgICApXG4nCiAgICApCiAgICBuZXdfdWkgPSAoCiAgICAgICAgJyAgICAgICAgc2VsZi5iYWNrZW5kX2NvbWJvID0gUUNvbWJvQm94KClcbicKICAgICAgICAnICAgICAgICBzZWxmLmJhY2tlbmRfY29tYm8uYWRkSXRlbXMoXG4nCiAgICAgICAgJyAgICAgICAgICAgIFtcbicKICAgICAgICAnICAgICAgICAgICAgICAgICJDUFUgLyBOdW1QeSBTVEZUIixcbicKICAgICAgICAnICAgICAgICAgICAgXVxuJwogICAgICAgICcgICAgICAgIClcbicKICAgICkKICAgIGlmIG9sZF91aSBub3QgaW4gdGV4dDoKICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoZiJ7bmFtZX06IFNURlQgYmFja2VuZCBVSSBibG9jayBub3QgZm91bmQiKQogICAgdGV4dCA9IHRleHQucmVwbGFjZShvbGRfdWksIG5ld191aSwgMSkKCiAgICB3cml0ZShuYW1lLCB0ZXh0KQogICAgcHJpbnQoIltGQVNUIENQVV0gZ2VvcGhvbmVfc3BlY3Ryb2dyYW0ucHkgLT4gTnVtUHktb25seSBiYWNrZW5kIikKCgpmb3JjZV9mYXN0X2NwdV9mZnQoKQpmb3JjZV9mYXN0X2NwdV9zcGVjdHJvZ3JhbSgpCnByaW50KCJbRkFTVCBDUFVdIFB5VG9yY2gvQ3VQeSBydW50aW1lIHBhdGhzIGRpc2FibGVkIGluIHN0YWdlZCBzb3VyY2UuIikKCnByaW50KCJTaW5nbGUtRVhFIHJ1bnRpbWUgcGF0aCArIEZBU1QgQ1BVIHBhdGNoZXMgYXBwbGllZC4iKQo="
"%PY%" -c "import base64,pathlib; pathlib.Path(r'%STAGE%\_patch_single.py').write_bytes(base64.b64decode(r'%PATCH_B64%'))"
if errorlevel 1 goto :FAIL

"%PY%" "%STAGE%\_patch_single.py" "%STAGE%"
if errorlevel 1 (
    echo [ERROR] Build-only runtime patch gagal.
    goto :FAIL
)

del /q "%STAGE%\_patch_single.py" >nul 2>&1

REM ---------------------------------------------------------------------------
REM 8. Create one-EXE dispatcher
REM ---------------------------------------------------------------------------
echo [CREATE] OBS.exe internal dispatcher...

set "DISPATCH_B64=ZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0IG11bHRpcHJvY2Vzc2luZwppbXBvcnQgc2h1dGlsCmltcG9ydCBzdWJwcm9jZXNzCmltcG9ydCBzeXMKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCmltcG9ydCBydW5weQoKX09SSUdJTkFMX1BPUEVOID0gc3VicHJvY2Vzcy5Qb3BlbgoKQUxMT1dFRF9NT0RVTEVTID0gewogICAgIm9ic19zZXR0aW5nLnB5IiwKICAgICJjYW1lcmEucHkiLAogICAgInBvc2l0aW9uLnB5IiwKICAgICJnZW9waG9uZS5weSIsCiAgICAib3RoZXJfc2Vuc29ycy5weSIsCiAgICAibWluaXNlZWRfcmVjb3JkaW5nLnB5IiwKICAgICJnZW9waG9uZV9yZWFsdGltZS5weSIsCiAgICAiZ2VvcGhvbmVfZmZ0LnB5IiwKICAgICJnZW9waG9uZV9zcGVjdHJvZ3JhbS5weSIsCiAgICAiZ2VvcGhvbmVfM2QucHkiLAogICAgImdlb3Bob25lX2ltdS5weSIsCiAgICAiZ2VvcGhvbmVfaG9kb2dyYW0ucHkiLAogICAgImdlb3Bob25lX3F1YWxpdHkucHkiLAogICAgImdlb3Bob25lX2V2ZW50LnB5IiwKICAgICJnZW9waG9uZV9wc2QucHkiLAogICAgIm9ic19oZWFsdGhfY2hlY2sucHkiLAp9CgpkZWYgYnVuZGxlX2RpcigpIC0+IFBhdGg6CiAgICByZXR1cm4gUGF0aChnZXRhdHRyKHN5cywgIl9NRUlQQVNTIiwgUGF0aChfX2ZpbGVfXykucmVzb2x2ZSgpLnBhcmVudCkpCgpkZWYgcnVudGltZV9kaXIoKSAtPiBQYXRoOgogICAgaWYgZ2V0YXR0cihzeXMsICJmcm96ZW4iLCBGYWxzZSk6CiAgICAgICAgcmV0dXJuIFBhdGgoc3lzLmV4ZWN1dGFibGUpLnJlc29sdmUoKS5wYXJlbnQKICAgIHJldHVybiBQYXRoKF9fZmlsZV9fKS5yZXNvbHZlKCkucGFyZW50CgpkZWYgaW5zdGFsbF9kZWZhdWx0X2luaV9maWxlcygpIC0+IE5vbmU6CiAgICBzcmNfZGlyID0gYnVuZGxlX2RpcigpIC8gImRlZmF1bHRzIgogICAgaWYgbm90IHNyY19kaXIuaXNfZGlyKCk6CiAgICAgICAgcmV0dXJuCiAgICBkc3RfZGlyID0gcnVudGltZV9kaXIoKQogICAgZm9yIHNyYyBpbiBzcmNfZGlyLmdsb2IoIiouaW5pIik6CiAgICAgICAgZHN0ID0gZHN0X2RpciAvIHNyYy5uYW1lCiAgICAgICAgaWYgbm90IGRzdC5leGlzdHMoKToKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgc2h1dGlsLmNvcHkyKHNyYywgZHN0KQogICAgICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICAgICAgcGFzcwoKZGVmIGluc3RhbGxfc2luZ2xlX2V4ZV9wb3Blbl9icmlkZ2UoKSAtPiBOb25lOgogICAgaWYgbm90IGdldGF0dHIoc3lzLCAiZnJvemVuIiwgRmFsc2UpOgogICAgICAgIHJldHVybgoKICAgIGN1cnJlbnRfZXhlID0gUGF0aChzeXMuZXhlY3V0YWJsZSkucmVzb2x2ZSgpCgogICAgZGVmIG9ic19wb3BlbihhcmdzLCAqcGFyZ3MsICoqa3dhcmdzKToKICAgICAgICBjb21tYW5kID0gYXJncwogICAgICAgIHRyeToKICAgICAgICAgICAgaWYgaXNpbnN0YW5jZShhcmdzLCAobGlzdCwgdHVwbGUpKSBhbmQgbGVuKGFyZ3MpID49IDI6CiAgICAgICAgICAgICAgICBmaXJzdCA9IFBhdGgoc3RyKGFyZ3NbMF0pKS5yZXNvbHZlKCkKICAgICAgICAgICAgICAgIGNoaWxkX2FyZyA9IHN0cihhcmdzWzFdKQogICAgICAgICAgICAgICAgY2hpbGRfbmFtZSA9IFBhdGgoY2hpbGRfYXJnKS5uYW1lCgogICAgICAgICAgICAgICAgIyBFeGlzdGluZyBPQlMgbGF1bmNoZXJzIHVzZToKICAgICAgICAgICAgICAgICMgICBbc3lzLmV4ZWN1dGFibGUsICIuLi4vY2FtZXJhLnB5Il0KICAgICAgICAgICAgICAgICMgSW4gYSBmcm96ZW4gYnVpbGQsIHJlZGlyZWN0IHRoYXQgdG8gdGhlIHNhbWUgT0JTLmV4ZS4KICAgICAgICAgICAgICAgIGlmICgKICAgICAgICAgICAgICAgICAgICBmaXJzdCA9PSBjdXJyZW50X2V4ZQogICAgICAgICAgICAgICAgICAgIGFuZCBjaGlsZF9uYW1lIGluIEFMTE9XRURfTU9EVUxFUwogICAgICAgICAgICAgICAgICAgIGFuZCBjaGlsZF9uYW1lLmxvd2VyKCkuZW5kc3dpdGgoIi5weSIpCiAgICAgICAgICAgICAgICApOgogICAgICAgICAgICAgICAgICAgIGNvbW1hbmQgPSBbCiAgICAgICAgICAgICAgICAgICAgICAgIHN0cihjdXJyZW50X2V4ZSksCiAgICAgICAgICAgICAgICAgICAgICAgICItLW9icy1tb2R1bGUiLAogICAgICAgICAgICAgICAgICAgICAgICBjaGlsZF9uYW1lLAogICAgICAgICAgICAgICAgICAgICAgICAqbGlzdChhcmdzWzI6XSksCiAgICAgICAgICAgICAgICAgICAgXQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIGNvbW1hbmQgPSBhcmdzCgogICAgICAgIHJldHVybiBfT1JJR0lOQUxfUE9QRU4oY29tbWFuZCwgKnBhcmdzLCAqKmt3YXJncykKCiAgICBzdWJwcm9jZXNzLlBvcGVuID0gb2JzX3BvcGVuCgpkZWYgcnVuX2J1bmRsZWRfbW9kdWxlKG1vZHVsZV9uYW1lOiBzdHIsIG1vZHVsZV9hcmd2OiBsaXN0W3N0cl0pIC0+IGludDoKICAgIG5hbWUgPSBQYXRoKG1vZHVsZV9uYW1lKS5uYW1lCiAgICBpZiBuYW1lIG5vdCBpbiBBTExPV0VEX01PRFVMRVM6CiAgICAgICAgcmFpc2UgU3lzdGVtRXhpdChmIlVua25vd24gT0JTIG1vZHVsZToge25hbWV9IikKCiAgICB0YXJnZXQgPSBidW5kbGVfZGlyKCkgLyBuYW1lCiAgICBpZiBub3QgdGFyZ2V0LmlzX2ZpbGUoKToKICAgICAgICByYWlzZSBTeXN0ZW1FeGl0KGYiQnVuZGxlZCBPQlMgbW9kdWxlIG5vdCBmb3VuZDoge3RhcmdldH0iKQoKICAgIHN5cy5hcmd2ID0gW3N0cih0YXJnZXQpLCAqbW9kdWxlX2FyZ3ZdCiAgICBydW5weS5ydW5fcGF0aChzdHIodGFyZ2V0KSwgcnVuX25hbWU9Il9fbWFpbl9fIikKICAgIHJldHVybiAwCgpkZWYgbWFpbigpIC0+IGludDoKICAgICMgUmVxdWlyZWQgYnkgUHlJbnN0YWxsZXIgd2hlbiBtdWx0aXByb2Nlc3Npbmcvc3Bhd24gaXMgdXNlZC4KICAgIG11bHRpcHJvY2Vzc2luZy5mcmVlemVfc3VwcG9ydCgpCgogICAgaW5zdGFsbF9kZWZhdWx0X2luaV9maWxlcygpCiAgICBpbnN0YWxsX3NpbmdsZV9leGVfcG9wZW5fYnJpZGdlKCkKCiAgICBpZiBsZW4oc3lzLmFyZ3YpID49IDMgYW5kIHN5cy5hcmd2WzFdID09ICItLW9icy1tb2R1bGUiOgogICAgICAgIHJldHVybiBydW5fYnVuZGxlZF9tb2R1bGUoc3lzLmFyZ3ZbMl0sIHN5cy5hcmd2WzM6XSkKCiAgICBpbXBvcnQgbWFpbiBhcyBvYnNfbWFpbgogICAgcmVzdWx0ID0gb2JzX21haW4ubWFpbigpCiAgICByZXR1cm4gMCBpZiByZXN1bHQgaXMgTm9uZSBlbHNlIGludChyZXN1bHQpCgppZiBfX25hbWVfXyA9PSAiX19tYWluX18iOgogICAgcmFpc2UgU3lzdGVtRXhpdChtYWluKCkpCg=="
"%PY%" -c "import base64,pathlib; pathlib.Path(r'%STAGE%\obs_entry.py').write_bytes(base64.b64decode(r'%DISPATCH_B64%'))"
if errorlevel 1 goto :FAIL

REM ---------------------------------------------------------------------------
REM 9. Compile-check ALL staging source before PyInstaller
REM ---------------------------------------------------------------------------
echo [CHECK] Staging source syntax...

for %%F in (
    obs_entry.py
    main.py
    obs_setting.py
    camera.py
    position.py
    geophone.py
    geophone_realtime.py
    geophone_fft.py
    geophone_spectrogram.py
    geophone_3d.py
    geophone_imu.py
    geophone_hodogram.py
    geophone_quality.py
    geophone_event.py
    geophone_psd.py
    other_sensors.py
    miniseed_recording.py
    shared_data.py
    shared_data_v3.py
    obs_ipc.py
    obs_health_check.py
) do (
    "%PY%" -m py_compile "%STAGE%\%%F"
    if errorlevel 1 (
        echo [ERROR] py_compile gagal: %%F
        goto :FAIL
    )
)

echo [OK] Staging syntax.
echo.

REM Remove generated pycache so it is not embedded as unnecessary data.
for /d /r "%STAGE%" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D"
)

REM ---------------------------------------------------------------------------
REM 10. PyInstaller build driver
REM
REM IMPORTANT:
REM Do NOT compose the long PyInstaller command in CMD.
REM The project path may contain spaces / #, for example:
REM   C:\Users\...\#UGM OBS _v3
REM A Python subprocess argv list is used so every path remains one argument.
REM ---------------------------------------------------------------------------
echo [CREATE] argv-safe PyInstaller build driver...

set "BUILD_DRIVER_B64=ZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0IHN1YnByb2Nlc3MKaW1wb3J0IHN5cwpmcm9tIHBhdGhsaWIgaW1wb3J0IFBhdGgKCmlmIGxlbihzeXMuYXJndikgIT0gODoKICAgIHJhaXNlIFN5c3RlbUV4aXQoCiAgICAgICAgIlVzYWdlOiBfYnVpbGRfb2JzX3NpbmdsZS5weSBST09UIFNUQUdFIERJU1QgV09SSyBTUEVDRElSIE9QRU5HTF9PSyBSQVNURVJJT19PSyIKICAgICkKCnJvb3QgPSBQYXRoKHN5cy5hcmd2WzFdKS5yZXNvbHZlKCkKc3RhZ2UgPSBQYXRoKHN5cy5hcmd2WzJdKS5yZXNvbHZlKCkKZGlzdCA9IFBhdGgoc3lzLmFyZ3ZbM10pLnJlc29sdmUoKQp3b3JrID0gUGF0aChzeXMuYXJndls0XSkucmVzb2x2ZSgpCnNwZWNkaXIgPSBQYXRoKHN5cy5hcmd2WzVdKS5yZXNvbHZlKCkKb3BlbmdsX29rID0gc3lzLmFyZ3ZbNl0gPT0gIjEiCnJhc3RlcmlvX29rID0gc3lzLmFyZ3ZbN10gPT0gIjEiCgphcmdzOiBsaXN0W3N0cl0gPSBbCiAgICAiLS1ub2NvbmZpcm0iLAogICAgIi0tY2xlYW4iLAogICAgIi0tb25lZmlsZSIsCiAgICAiLS13aW5kb3dlZCIsCiAgICAiLS1ub3VweCIsCiAgICAiLS1uYW1lIiwgIk9CUyIsCiAgICAiLS1kaXN0cGF0aCIsIHN0cihkaXN0KSwKICAgICItLXdvcmtwYXRoIiwgc3RyKHdvcmspLAogICAgIi0tc3BlY3BhdGgiLCBzdHIoc3BlY2RpciksCiAgICAiLS1wYXRocyIsIHN0cihzdGFnZSksCl0KCiMgSWNvbiBpcyBvcHRpb25hbC4gUGFzcyBvcHRpb24gYW5kIHBhdGggYXMgVFdPIGFyZ3YgaXRlbXMsIHNvIHNwYWNlcy8jIGFyZSBzYWZlLgppY28gPSByb290IC8gImFzc2V0cyIgLyAiaWNvbnMiIC8gImFwcF9pY29uLmljbyIKcG5nID0gcm9vdCAvICJhc3NldHMiIC8gImljb25zIiAvICJhcHBfaWNvbi5wbmciCmlmIGljby5pc19maWxlKCk6CiAgICBhcmdzICs9IFsiLS1pY29uIiwgc3RyKGljbyldCmVsaWYgcG5nLmlzX2ZpbGUoKToKICAgIGFyZ3MgKz0gWyItLWljb24iLCBzdHIocG5nKV0KCiMgT3B0aW9uYWwgcnVudGltZSBwYWNrYWdlcy4KaWYgcmFzdGVyaW9fb2s6CiAgICBhcmdzICs9IFsiLS1jb2xsZWN0LWFsbCIsICJyYXN0ZXJpbyJdCmVsc2U6CiAgICBhcmdzICs9IFsiLS1leGNsdWRlLW1vZHVsZSIsICJyYXN0ZXJpbyJdCgppZiBvcGVuZ2xfb2s6CiAgICBhcmdzICs9IFsiLS1jb2xsZWN0LXN1Ym1vZHVsZXMiLCAiT3BlbkdMIl0KZWxzZToKICAgIGFyZ3MgKz0gWyItLWV4Y2x1ZGUtbW9kdWxlIiwgIk9wZW5HTCJdCgojIEZBU1QgQ1BVOiBleHBsaWNpdGx5IHByZXZlbnQgR1BVIGNvbXB1dGUgc3RhY2tzIGZyb20gZW50ZXJpbmcgT0JTLmV4ZS4KIyBQeUluc3RhbGxlciBzY2FucyBpbXBvcnRzIGV2ZW4gd2hlbiB0aGV5IGFyZSBpbnNpZGUgZnVuY3Rpb25zLCBzbyB0aGVzZQojIGV4Y2x1c2lvbnMgYXJlIHJlcXVpcmVkIGV2ZW4gdGhvdWdoIHN0YWdlZCBGRlQvU1RGVCBydW50aW1lIGNvZGUgaXMgQ1BVLW9ubHkuCmdwdV9leGNsdWRlcyA9IFsKICAgICJ0b3JjaCIsCiAgICAidG9yY2h2aXNpb24iLAogICAgInRvcmNoYXVkaW8iLAogICAgImN1cHkiLAogICAgImN1cHl4IiwKXQpmb3IgbW9kIGluIGdwdV9leGNsdWRlczoKICAgIGFyZ3MgKz0gWyItLWV4Y2x1ZGUtbW9kdWxlIiwgbW9kXQoKIyBSZXF1aXJlZC9rbm93biBkeW5hbWljIG1vZHVsZXMuCmhpZGRlbl9pbXBvcnRzID0gWwogICAgIlB5U2lkZTYuUXROZXR3b3JrIiwKICAgICJQeVNpZGU2LlF0TXVsdGltZWRpYSIsCiAgICAiUHlTaWRlNi5RdE11bHRpbWVkaWFXaWRnZXRzIiwKICAgICJQeVNpZGU2LlF0V2ViRW5naW5lQ29yZSIsCiAgICAiUHlTaWRlNi5RdFdlYkVuZ2luZVdpZGdldHMiLAogICAgIlB5U2lkZTYuUXRPcGVuR0wiLAogICAgIlB5U2lkZTYuUXRPcGVuR0xXaWRnZXRzIiwKICAgICJzZXJpYWwiLAogICAgInB5cXRncmFwaCIsCiAgICAicHlxdGdyYXBoLm9wZW5nbCIsCiAgICAibWFpbiIsCiAgICAib2JzX3NldHRpbmciLAogICAgImNhbWVyYSIsCiAgICAicG9zaXRpb24iLAogICAgImdlb3Bob25lIiwKICAgICJnZW9waG9uZV9yZWFsdGltZSIsCiAgICAiZ2VvcGhvbmVfZmZ0IiwKICAgICJnZW9waG9uZV9zcGVjdHJvZ3JhbSIsCiAgICAiZ2VvcGhvbmVfM2QiLAogICAgImdlb3Bob25lX2ltdSIsCiAgICAiZ2VvcGhvbmVfaG9kb2dyYW0iLAogICAgImdlb3Bob25lX3F1YWxpdHkiLAogICAgImdlb3Bob25lX2V2ZW50IiwKICAgICJnZW9waG9uZV9wc2QiLAogICAgIm90aGVyX3NlbnNvcnMiLAogICAgIm1pbmlzZWVkX3JlY29yZGluZyIsCiAgICAic2hhcmVkX2RhdGEiLAogICAgInNoYXJlZF9kYXRhX3YzIiwKICAgICJvYnNfaXBjIiwKICAgICJvYnNfaGVhbHRoX2NoZWNrIiwKXQpmb3IgbW9kIGluIGhpZGRlbl9pbXBvcnRzOgogICAgYXJncyArPSBbIi0taGlkZGVuLWltcG9ydCIsIG1vZF0KCmFyZ3MgKz0gWyItLWNvbGxlY3QtYWxsIiwgIlBJTCJdCmFyZ3MgKz0gWyItLWNvbGxlY3QtYWxsIiwgIm9ic3B5Il0KYXJncyArPSBbIi0tY29weS1tZXRhZGF0YSIsICJvYnNweSJdCgojIFB5SW5zdGFsbGVyIHZlcnNpb24gc2hvd24gYnkgdGhlIHVzZXIncyBtYWNoaW5lIHJlcXVpcmVzIFNPVVJDRTpERVNULgpkZWYgYWRkX2RhdGEoc3JjOiBQYXRoLCBkZXN0OiBzdHIpIC0+IE5vbmU6CiAgICBpZiBub3Qgc3JjLmV4aXN0cygpOgogICAgICAgIHJhaXNlIEZpbGVOb3RGb3VuZEVycm9yKGYiUmVxdWlyZWQgYnVuZGxlIGRhdGEgbm90IGZvdW5kOiB7c3JjfSIpCiAgICBhcmdzLmV4dGVuZChbIi0tYWRkLWRhdGEiLCBmIntzcmN9OntkZXN0fSJdKQoKYWRkX2RhdGEoc3RhZ2UgLyAiYXNzZXRzIiwgImFzc2V0cyIpCmFkZF9kYXRhKHN0YWdlIC8gImRlZmF1bHRzIiwgImRlZmF1bHRzIikKCiMgRXhpc3RpbmcgbWFpbi9nZW9waG9uZSBsYXVuY2hlcnMgdmVyaWZ5IHRoYXQgdGhlaXIgY2hpbGQgc2NyaXB0IGV4aXN0cyBhbmQKIyBHZW9waG9uZSBhbHNvIGluc3BlY3RzIHNvdXJjZSBjb21wYXRpYmlsaXR5LiBUaGVyZWZvcmUgdGhlc2UgLnB5IGZpbGVzIGFyZQojIGluY2x1ZGVkIGFzIERBVEEgaW4gYWRkaXRpb24gdG8gYmVpbmcgYW5hbHl6ZWQgYXMgaGlkZGVuIGltcG9ydHMuCmNoaWxkX3NjcmlwdHMgPSBbCiAgICAib2JzX3NldHRpbmcucHkiLAogICAgImNhbWVyYS5weSIsCiAgICAicG9zaXRpb24ucHkiLAogICAgImdlb3Bob25lLnB5IiwKICAgICJvdGhlcl9zZW5zb3JzLnB5IiwKICAgICJtaW5pc2VlZF9yZWNvcmRpbmcucHkiLAogICAgImdlb3Bob25lX3JlYWx0aW1lLnB5IiwKICAgICJnZW9waG9uZV9mZnQucHkiLAogICAgImdlb3Bob25lX3NwZWN0cm9ncmFtLnB5IiwKICAgICJnZW9waG9uZV8zZC5weSIsCiAgICAiZ2VvcGhvbmVfaW11LnB5IiwKICAgICJnZW9waG9uZV9ob2RvZ3JhbS5weSIsCiAgICAiZ2VvcGhvbmVfcXVhbGl0eS5weSIsCiAgICAiZ2VvcGhvbmVfZXZlbnQucHkiLAogICAgImdlb3Bob25lX3BzZC5weSIsCiAgICAib2JzX2hlYWx0aF9jaGVjay5weSIsCl0KZm9yIG5hbWUgaW4gY2hpbGRfc2NyaXB0czoKICAgIGFkZF9kYXRhKHN0YWdlIC8gbmFtZSwgIi4iKQoKZW50cnkgPSBzdGFnZSAvICJvYnNfZW50cnkucHkiCmlmIG5vdCBlbnRyeS5pc19maWxlKCk6CiAgICByYWlzZSBGaWxlTm90Rm91bmRFcnJvcihlbnRyeSkKCmFyZ3MuYXBwZW5kKHN0cihlbnRyeSkpCgpjbWQgPSBbc3lzLmV4ZWN1dGFibGUsICItbSIsICJQeUluc3RhbGxlciIsICphcmdzXQoKcHJpbnQoKQpwcmludCgiPSIgKiA3MikKcHJpbnQoIlBZSU5TVEFMTEVSIERSSVZFUiIpCnByaW50KCI9IiAqIDcyKQpwcmludCgiUHl0aG9uIDoiLCBzeXMuZXhlY3V0YWJsZSkKcHJpbnQoIkVudHJ5ICA6IiwgZW50cnkpCnByaW50KCJPdXRwdXQgOiIsIGRpc3QgLyAiT0JTLmV4ZSIpCnByaW50KCJJY29uICAgOiIsIHN0cihpY28gaWYgaWNvLmlzX2ZpbGUoKSBlbHNlIHBuZyBpZiBwbmcuaXNfZmlsZSgpIGVsc2UgImRlZmF1bHQiKSkKcHJpbnQoIk9wZW5HTCA6IiwgImluY2x1ZGUiIGlmIG9wZW5nbF9vayBlbHNlICJleGNsdWRlL2ZhbGxiYWNrIikKcHJpbnQoIlJhc3RlciA6IiwgImluY2x1ZGUiIGlmIHJhc3RlcmlvX29rIGVsc2UgImV4Y2x1ZGUvZmFsbGJhY2siKQpwcmludCgiQ29tcHV0ZTogRkFTVCBDUFUgLyBOdW1QeSAodG9yY2ggKyBjdXB5IGV4Y2x1ZGVkKSIpCnByaW50KCkKcHJpbnQoIkxhdW5jaGluZyBQeUluc3RhbGxlciB3aXRoIGFyZ3Ytc2FmZSBzdWJwcm9jZXNzIGNhbGwuLi4iKQpwcmludCgpCgpyZXN1bHQgPSBzdWJwcm9jZXNzLnJ1bihjbWQpCnJhaXNlIFN5c3RlbUV4aXQocmVzdWx0LnJldHVybmNvZGUpCg=="
"%PY%" -c "import base64,pathlib; pathlib.Path(r'%STAGE%\_build_obs_single.py').write_bytes(base64.b64decode(r'%BUILD_DRIVER_B64%'))"
if errorlevel 1 (
    echo [ERROR] Gagal membuat PyInstaller build driver.
    goto :FAIL
)

"%PY%" -m py_compile "%STAGE%\_build_obs_single.py"
if errorlevel 1 (
    echo [ERROR] Syntax build driver tidak valid.
    goto :FAIL
)
echo [OK] PyInstaller build driver syntax.
echo.

REM ---------------------------------------------------------------------------
REM 11. ONE PyInstaller build -> OBS.exe
REM ---------------------------------------------------------------------------
echo ============================================================
echo BUILDING SINGLE OBS.exe
echo ============================================================
echo.

"%PY%" "%STAGE%\_build_obs_single.py" ^
    "%ROOT%" ^
    "%STAGE%" ^
    "%DIST%" ^
    "%WORK%" ^
    "%SPECDIR%" ^
    "!OPENGL_OK!" ^
    "!RASTERIO_OK!"

if errorlevel 1 (
    echo.
    echo [ERROR] Build OBS.exe gagal.
    goto :FAIL
)

if not exist "%DIST%\OBS.exe" (
    echo [ERROR] Output tidak ditemukan:
    echo         %DIST%\OBS.exe
    goto :FAIL
)

REM ---------------------------------------------------------------------------
REM 13. Verify distribution contains ONE physical file only
REM ---------------------------------------------------------------------------
set /a DIST_FILES=0
set "DIST_FILE="

for /f "delims=" %%F in ('dir /b /a-d "%DIST%" 2^>nul') do (
    set /a DIST_FILES+=1
    set "DIST_FILE=%%F"
)

if not "!DIST_FILES!"=="1" (
    echo [ERROR] Folder dist mengandung !DIST_FILES! file.
    echo Seharusnya hanya OBS.exe.
    dir /b "%DIST%"
    goto :FAIL
)

if /i not "!DIST_FILE!"=="OBS.exe" (
    echo [ERROR] File output tunggal bukan OBS.exe:
    echo         !DIST_FILE!
    goto :FAIL
)

echo.
echo ============================================================
echo BUILD SUCCESS
echo ============================================================
echo.
echo Output distribution:
echo   %DIST%\OBS.exe
echo.
echo HANYA SATU APPLICATION EXE FISIK:
echo   OBS.exe
echo.
echo Internal OBS process model:
echo   OBS.exe                       ^(Main Launcher^)
echo   OBS.exe --obs-module camera.py
echo   OBS.exe --obs-module position.py
echo   OBS.exe --obs-module geophone.py
echo   OBS.exe --obs-module ...
echo.
echo shared_data.py    = canonical/current shared API
echo shared_data_v3.py = retained internal implementation dependency
echo obs_ipc.py         = internal IPC library
echo.
echo Assets dan default INI dibundle ke dalam OBS.exe.
echo Writable INI/log/recording akan dibuat di folder OBS.exe saat runtime.
echo.
for %%A in ("%DIST%\OBS.exe") do echo Size: %%~zA bytes
echo.
echo Compute backend:
echo   FFT         = CPU / NumPy FFT
echo   Spectrogram = CPU / NumPy STFT
echo   PyTorch     = NOT bundled
echo   CuPy        = NOT bundled
echo.
echo Semua fitur OBS Runtime lainnya tetap dibundle.
echo.


explorer "%DIST%"
pause
exit /b 0

:FAIL
echo.
echo ============================================================
echo BUILD FAILED
echo ============================================================
echo Periksa pesan error di atas.
echo Source asli tidak diubah.
echo Staging dipertahankan untuk diagnosis:
echo   %STAGE%
echo.
pause
exit /b 1
