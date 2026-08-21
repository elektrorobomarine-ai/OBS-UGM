GRC-UGM-PERTAMINA OBS — OneFolder Build Package

TARGET
- Windows 10/11 64-bit
- Python 3.11 recommended for building
- PyInstaller one-folder mode
- One application EXE dispatches all GUI modules using --module
- obs_settings.ini remains EXTERNAL beside the EXE

BUILD
1. Copy/extract this folder to the Windows development PC.
2. Install 64-bit Python 3.11.
3. Double-click build_onefolder_windows.bat.

PYTHON 3.11 HANDLING
- Build v2 detects an existing 64-bit Python 3.11 automatically.
- If Python 3.11 is missing and winget is available, the BAT asks permission
  before installing Python.Python.3.11 for the current user.
- Other Python versions do NOT need to be uninstalled; Python 3.11 can coexist.
- The BAT validates that the build interpreter is exactly Python 3.11 64-bit.
- If an old .venv was made with a different Python version, it is recreated.
4. Output:
   dist\GRC_UGM_PERTAMINA_OBS\GRC_UGM_PERTAMINA_OBS.exe

RUNTIME FOLDER
Keep these writable items beside the EXE:
- obs_settings.ini
- logs\
- recordings\
The PyInstaller runtime files remain under _internal\.

GPU
CuPy is optional. Without CuPy, FFT/PSD/Spectrogram use NumPy CPU fallback.
For CUDA 12.x builds, activate .venv and install cupy-cuda12x before building.

IMPORTANT DATA-RATE ARCHITECTURE
- raw ADC source rate is configured in OBS Setting/shared_data_v5
- global Average N is applied ONCE before shared RAM
- all geophone viewers use effective_sample_rate_hz
- MiniSEED v2 records the already-effective shared stream directly; it does not decimate again

BUILD ENVIRONMENT NOTE
PyInstaller is platform-specific. A Windows EXE must be generated on Windows; this package contains the Windows-ready spec/build script and synchronized application sources.

DIRECT MODULE TESTS AFTER BUILD
- GRC_UGM_PERTAMINA_OBS.exe --module geophone_fft
- GRC_UGM_PERTAMINA_OBS.exe --module position
- GRC_UGM_PERTAMINA_OBS.exe --module miniseed_recording

The main launcher and Geophone launcher spawn these child processes through the SAME EXE when frozen.


WINDOWS LONG PATH / PySide6
--------------------------
Build v3 no longer relies on Windows Long Path support for the normal build.
The build script automatically maps the extracted project folder to a short
temporary drive letter (R: through Z:) using SUBST.

Example:
    D:\2026\2608\#UGM OBS\...\OBS_ONEFOLDER_BUILD_v3
becomes temporarily:
    R:\

The virtual environment therefore becomes:
    R:\.venv

This prevents PySide6_Essentials QML paths from exceeding the traditional
Windows MAX_PATH limit.

The SUBST drive is removed automatically after BUILD COMPLETE or BUILD FAILED.

If every R:-Z: drive letter is already occupied, move the package manually to:
    C:\OBS_BUILD
and run build_onefolder_windows.bat again.

IMPORTANT:
Do not copy the old .venv from Build v1/v2 into Build v3. Let v3 create a new
virtual environment.
