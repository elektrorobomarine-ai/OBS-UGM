from __future__ import annotations

import argparse
import importlib
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

APP_NAME = "GRC_UGM_PERTAMINA_OBS"

MODULES = {
    "obs_setting": "obs_setting",
    "camera": "camera",
    "position": "position",
    "geophone": "geophone",
    "other_sensors": "other_sensors",
    "miniseed_recording": "miniseed_recording",
    "geophone_realtime": "geophone_realtime",
    "geophone_fft": "geophone_fft",
    "geophone_spectrogram": "geophone_spectrogram",
    "geophone_3d": "geophone_3d",
    "geophone_imu": "geophone_imu",
    "geophone_hodogram": "geophone_hodogram",
    "geophone_quality": "geophone_quality",
    "geophone_event": "geophone_event",
    "geophone_psd": "geophone_psd",
}


def external_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def run_module(module_key: str) -> int:
    module_name = MODULES[module_key]
    module = importlib.import_module(module_name)
    main_func = getattr(module, "main", None)
    if not callable(main_func):
        raise RuntimeError(f"{module_name} does not expose main()")
    result = main_func()
    return int(result or 0)


def main() -> int:
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--module", choices=sorted(MODULES))
    args, _ = parser.parse_known_args()

    # Writable files created by modules should resolve from the release folder.
    os.chdir(external_dir())

    if args.module:
        return run_module(args.module)

    import main_launcher
    return int(main_launcher.main() or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # Frozen windowed executables have no console. Keep a crash log next
        # to the EXE so packaging/runtime failures are diagnosable.
        try:
            crash = external_dir() / "startup_crash.log"
            crash.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
