# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import importlib.util
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH)

hiddenimports = [
    'main_launcher',
    'camera', 'obs_setting', 'position', 'geophone', 'other_sensors',
    'miniseed_recording',
    'geophone_realtime', 'geophone_fft', 'geophone_spectrogram',
    'geophone_3d', 'geophone_imu', 'geophone_hodogram',
    'geophone_quality', 'geophone_event', 'geophone_psd',
    'shared_data_v5', 'shared_data_v3',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore',
    'PySide6.QtOpenGLWidgets',
]

datas = []
binaries = []

assets = ROOT / 'assets'
if assets.exists():
    datas.append((str(assets), 'assets'))

# Packages with native binaries/data that benefit from explicit collection.
for pkg in ('rasterio', 'obspy'):
    if importlib.util.find_spec(pkg) is not None:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h

# Optional GPU backend. CPU NumPy fallback remains valid when CuPy is absent.
if importlib.util.find_spec('cupy') is not None:
    d, b, h = collect_all('cupy')
    datas += d
    binaries += b
    hiddenimports += h

# OpenGL plugins/modules are dynamically discovered by pyqtgraph.
if importlib.util.find_spec('OpenGL') is not None:
    hiddenimports += collect_submodules('OpenGL')

icon_path = ROOT / 'assets' / 'icons' / 'app_icon.ico'
icon_value = str(icon_path) if icon_path.exists() else None


a = Analysis(
    ['app_entry.py'],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GRC_UGM_PERTAMINA_OBS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_value,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='GRC_UGM_PERTAMINA_OBS',
)
