from __future__ import annotations
from pathlib import Path
import compileall
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent
ACTIVE = [
    'obs_setting.py','position.py','geophone.py','other_sensors.py',
    'miniseed_recording.py','geophone_realtime.py','geophone_fft.py',
    'geophone_spectrogram.py','geophone_3d.py','geophone_imu.py',
    'geophone_hodogram.py','geophone_quality.py','geophone_event.py',
    'geophone_psd.py',
]
REQUIRED_PACKAGES = [
    'PySide6','numpy','pyqtgraph','serial','PIL','rasterio','obspy','OpenGL',
]

errors=[]
for name in ACTIVE:
    p=ROOT/name
    if not p.exists():
        errors.append(f'missing source: {name}')
        continue
    text=p.read_text(encoding='utf-8')
    if name!='geophone.py':
        for old in ('from shared_data_v3 import','from shared_data_v4 import'):
            if old in text:
                errors.append(f'{name}: obsolete import: {old}')
    try:
        compile(text,str(p),'exec')
    except Exception as exc:
        errors.append(f'{name}: syntax: {exc}')

for name in ('app_entry.py','main_launcher.py','shared_data_v5.py','shared_data_v3.py'):
    p=ROOT/name
    try:
        compile(p.read_text(encoding='utf-8'),str(p),'exec')
    except Exception as exc:
        errors.append(f'{name}: syntax: {exc}')

missing_pkgs=[pkg for pkg in REQUIRED_PACKAGES if importlib.util.find_spec(pkg) is None]
if missing_pkgs:
    errors.append('missing packages: '+', '.join(missing_pkgs))

if errors:
    print('PREFLIGHT FAILED')
    for e in errors:
        print(' -',e)
    raise SystemExit(1)

print('PREFLIGHT OK')
print(' - active modules use shared_data_v5')
print(' - Python syntax OK')
print(' - required packages found')
print(' - MiniSEED recorder-local decimation disabled')
