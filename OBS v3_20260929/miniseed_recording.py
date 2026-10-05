"""
miniseed_recording.py
=========================

GRC-UGM-PERTAMINA OBS
MiniSEED Recording

Version: 29
Shared data: shared_data.py (API v10)

Version 29 selectable OBS time source (PC / GNSS)
--------------------------------------------------
- Adds a TIME SOURCE selector: PC TIME or GNSS TIME.
- GNSS time is read from centralized shared-RAM NMEA and supports GGA/RMC/ZDA.
- RMC/ZDA provide UTC date+time directly. GGA provides UTC time only, so v29
  combines GGA time with the nearest PC UTC calendar date and labels that fallback.
- GNSS sync is permitted only while the NMEA sample is fresh; stale/unavailable GNSS
  cannot trigger manual or automatic OBS time synchronization.
- OBS-vs-reference offset and AUTO KEEP OBS TIME CLOSE TO ... follow the selected
  source. PC mode preserves v28 behaviour.
- GNSS transmission stays on the centralized OBS IPC command path; v29 detects a
  broker method capable of accepting an explicit reference datetime and reports a
  clear error if an older obs_ipc.py supports PC-only TIME_SYNC_FROM_PC.
- No MiniSEED waveform, USBL, ADC timing, USB logger recovery, archive, or CSV
  behaviour is changed.

Version 28 USB logger reconnect / rotation-state recovery
--------------------------------------------------------
- On CMD/IPC reconnect, the onboard USB logger state is explicitly marked
  RECOVERING until a factual STRG0 event establishes the current hardware state.
- A reconnect never assumes that OBS USB logging stopped just because the PC or
  GUI restarted; the OBS-side logger remains the source of truth.
- MSDV_FILE_CLOSED is treated as provisional unless it confirms an explicit STOP.
  Normal firmware file rotation emits CLOSED followed by OPENED, so a short grace
  period prevents a false STOPPED indication during rotation.
- MSDV_FILE_OPENED immediately restores RECORDING/current-file state after a PC
  restart/reconnect. START remains available while state is unknown, but requires
  an operator confirmation while recovery is still unresolved.
- MiniSEED waveform recording, sampling/timestamps, USBL/attitude CSV, time sync,
  archive naming and all existing v27 data paths are unchanged.

Version 27 corrected-firmware timing metadata
---------------------------------------------
- Uses shared_data API v10 as the single source for the corrected-firmware
  expected raw rate, shared decimation and effective MiniSEED sampling rate.
- Records the independent PC-side ADC-rate diagnostic (measured rate, ppm,
  PASS/WARN/FAULT and measurement window) in session_metadata JSON for audit.
  The diagnostic never changes MiniSEED sampling_rate dynamically.
- Keeps raw waveform samples untouched: no recorder-side decimation, resampling,
  interpolation or zero-fill is introduced.
- IMU remains operational CSV-only at 1 Hz; if IMU waveform MiniSEED is ever
  re-enabled, its source-rate default remains the real 25 Hz (40 ms spacing).

Version 26 acquisition-rate fidelity
------------------------------------
- MiniSEED waveform sampling_rate remains sourced exclusively from the dynamic
  shared ADC stream metadata published by OBS Setting; no 1000/N assumption is
  used by the recorder.
- Session metadata explicitly records source/raw rate, decimation, effective
  MiniSEED rate and shared-data API version for audit/reprocessing.
- The recorder performs no second decimation, resampling, interpolation or
  zero-fill. Raw OBS source files remain external/unchanged source-of-truth data.
- IMU MiniSEED remains disabled. Operational attitude/position CSV logging stays
  at fixed 1 Hz, while the dormant IMU waveform default is corrected to 25 Hz
  to match the 40 ms source spacing if waveform recording is re-enabled later.

Recorded MiniSEED channels
--------------------------
Geophone:
    CH0 / N -> EHN   (fixed)
    CH1 / E -> EHE   (fixed)
    CH2 / Z -> EHZ   (fixed)

IMU MiniSEED:
    Disabled. Roll / Pitch / Yaw remain logged in the per-session
    obs_position_attitude_YYYYMMDD_HH_NN.csv at a fixed 1 Hz PC-UTC snapshot rate.
    Version 19 retains the same additive Roll/Pitch/Yaw installation offsets used
    by other_sensors_v7.py. The shared source remains obs_setting_v19 physical
    $AHRS2 values after E1 scaling (/10); offsets are loaded from imu_offsets.ini
    and are not written back into shared_data_v10.

Geophone data are recorded directly from the processed ADC stream published by
shared_data_v10. OBS Setting is the authoritative owner of ADC averaging /
decimation, so this recorder does NOT decimate a second time. Timestamp gaps or
ADC-session changes create separate MiniSEED trace segments; missing ADC data
are never fabricated.

Geophone output is one MiniSEED file per physical orientation:
    N -> one EHN file
    E -> one EHE file
    Z -> one EHZ file

Geophone samples are stored as INT32 counts using STEIM2 lossless compression.
Version 17 intentionally does not create a separate <STATION>_imu.mseed file.
Roll / Pitch / Yaw are retained in the per-session position/attitude CSV.

USBL position
-------------
MiniSEED waveform records do not normally carry station latitude/longitude as
sample metadata. Therefore this recorder stores OBS position in:

    usbl_position_YYYYMMDD_HH_NN.csv
                        fixed 1 Hz effective USBL state history during recording
    obs_position_attitude_YYYYMMDD_HH_NN.csv
                        combined UTC position/depth/attitude state log
    session_metadata_YYYYMMDD_HH_NN.json

The waveform files remain proper MiniSEED time-series data. Version 19 does not
create a StationXML file in the recording archive.

OBS onboard USB logging control
-------------------------------
This GUI also provides independent firmware commands for the OBS onboard USB
binary logger through OBS Setting's centralized command broker:

    61 = USB_LOG_START -> $RMCMD,61*7E\r\n
    62 = USB_LOG_STOP  -> $RMCMD,62*7D\r\n

These controls are independent from PC MiniSEED recording. Version 11 sends
commands through local IPC to OBS Setting. $STRG0 and TIME1 telemetry are read
from centralized shared RAM; MiniSEED opens no OBS TCP socket.
MSDV_FILE_OPENED:<FILENAME> and MSDV_FILE_CLOSED:<FILENAME> are used as the
hardware status indicators for START/STOP, while USB media/device/error events
control readiness and diagnostics.

Selectable PC / GNSS / OBS time synchronization
----------------------------------------------
The GUI shows the current factual local time from the laptop/PC and the latest
OBS TIME1 device time. Version 13 fixes the OBS-PC difference calculation by
advancing the last TIME1 sample by its receive age before comparing it with the
current PC clock. This prevents a stale TIME1 sample from appearing to drift by
roughly one second per second between telemetry updates.

PC TIME keeps the existing broker-generated TIME1 behavior. GNSS TIME uses fresh
shared-RAM GGA/RMC/ZDA UTC time when the centralized broker supports explicit TIME1 values:

    $TIME1,ms,yy,month,week,date,hour,minute,second,cnt*CC\r\n

The firmware example uses a two-digit year (2026 -> 26), Sunday=1 ... Saturday=7
for week, and cnt=0. The GUI follows that supplied example. A later TIME1
telemetry sentence is used as confirmation when available; no separate ACK is
invented.

Version 13 also adds optional automatic clock discipline. When enabled, a fresh
TIME1 sample whose estimated OBS-PC offset exceeds the configured threshold
(default 0.50 s) causes a new time-sync request, with a 60 s minimum interval so
the OBS command channel is never flooded.

Version 19 uses the final UTC archive hierarchy:

    <base>/YYYY/YYYYMM/YYYYMMDD/YYYYMMDD_HH/

With the normal base folder <application>/recordings/miniseed, for example:

    recordings/miniseed/2026/202609/20260907/20260907_07/

HH is UTC. Individual START sessions inside that hour are distinguished by NN in
the filenames:

    <STATION>_N_EHN_YYYYMMDD_HH_NN.mseed
    <STATION>_E_EHE_YYYYMMDD_HH_NN.mseed
    <STATION>_Z_EHZ_YYYYMMDD_HH_NN.mseed
    obs_position_attitude_YYYYMMDD_HH_NN.csv
    usbl_position_YYYYMMDD_HH_NN.csv
    session_metadata_YYYYMMDD_HH_NN.json

The YYYY, YYYYMM, YYYYMMDD and YYYYMMDD_HH folder names are all derived from
the PC UTC clock. If the operator stops and starts again in the same UTC hour,
NN increments inside the same YYYYMMDD_HH folder. Continuous recording rotates
automatically at the UTC hour boundary into the new hourly folder; the first
unused NN in that folder is used.

Version 25 coordinated Main shutdown
------------------------------------
When the Main launcher performs a confirmed application shutdown, this recorder
now treats the request as an automatic graceful close:

    active PC MiniSEED recording -> STOP -> FINALIZE -> SAVE -> close window

The worker is never force-terminated by this path. MiniSEED buffers, the two
operational CSV files and session metadata are finalized through the same worker
stop/finalization path used by the normal STOP button. A direct user click on
the MiniSEED window X still asks for confirmation when recording is active.

OBS onboard USB logging remains intentionally independent. Coordinated PC
shutdown does NOT send RMCMD 62 and therefore does not stop an active onboard
OBS USB binary log.

Version 24 USBL freshness and fixed-1-Hz USBL CSV
-------------------------------------------------
Both operational CSV files are now written at a fixed one-row-per-PC-UTC-second
cadence while PC recording is active:

    obs_position_attitude_YYYYMMDD_HH_NN.csv
    usbl_position_YYYYMMDD_HH_NN.csv

USBL source freshness is 3 seconds. A fix is operationally valid only when both
the raw NMEA source and the latest GGA snapshot remain fresh. If Cerulean Tracker
stops or the NMEA receive counter stops, the last valid longitude/latitude are
retained, but effective fix_quality and usbl_valid are written as 0.

usbl_position CSV columns are now:

    timestamp_utc,longitude_deg,latitude_deg,altitude_m,fix_quality,
    satellites,hdop,usbl_valid

No missing seconds are synthetically backfilled after a PC/worker stall; the
worker writes at most one factual latest-state row for each observed UTC second.
MiniSEED waveform samples, timing, NEZ mapping, EHN/EHE/EHZ codes, depth/IMU
handling, UTC folder rotation and OBS onboard BIN logging are unchanged.

Version 23 position/attitude CSV and fixed channel metadata
----------------------------------------------------------
The combined position/attitude CSV is now a fixed 1 Hz PC-UTC state snapshot with
exactly these columns:

    timestamp_utc,longitude_deg,latitude_deg,depth_m,roll_deg,pitch_deg,
    yaw_deg,usbl_valid

Each row uses the latest available shared-RAM values at that UTC second. No
interpolation or synthetic sensor samples are created. Longitude/latitude retain
the last valid USBL fix; usbl_valid becomes 0 when the current fix is invalid or
older than the configured freshness window. Depth uses the physical $DEPT0 value
and Roll/Pitch/Yaw retain the configured additive imu_offsets.ini correction.

All human-readable UTC timestamps written to CSV files use:

    YYYY-MM-DD HH:MM:SS

with no ISO T separator, no trailing Z, and no milliseconds. The time basis is
still UTC. The standalone usbl_position CSV uses the same whole-second UTC timestamp
format as the combined operational CSV.

Geophone MiniSEED channel codes are fixed to EHN / EHE / EHZ in both the GUI and
RecorderConfig; operators cannot change them. Network, Station, and Location remain
editable.


Version 22 depth logging correction
-----------------------------------
The PC recorder now reads physical depth from the same authoritative shared-RAM
slot used by Other Sensors: telemetry.pressure.  The attribute name is legacy
for binary/API compatibility; in the current OBS protocol $DEPT0 field 0 is
physical depth in metres.  This correction applies consistently to:

    - live Depth display
    - obs_position_attitude_YYYYMMDD_HH_NN.csv depth_m
    - session metadata depth_m_at_start
    - session metadata depth_m_at_metadata_write

No ADC samples, MiniSEED waveform data, timestamps, IMU offsets, USBL handling,
or archive naming are changed.


Version 21 geophone NEZ metadata update
----------------------------------------
The physical geophone orientation is now defined consistently as:

    CH0 = Geophone N (North)
    CH1 = Geophone E (East)
    CH2 = Geophone Z (Vertical)

MiniSEED channel codes are EHN / EHE / EHZ. Version 23 locks these
codes so they are no longer operator-editable. The waveform filename axis token
follows the physical orientation (N / E / Z), producing OBS01_N_EHN_...,
OBS01_E_EHE_..., and OBS01_Z_EHZ_....
Shared-RAM acquisition order is unchanged: CH0 still reads ch0, CH1 reads ch1,
and CH2 reads ch2. No ADC samples are reordered or remapped.


Version 20 reliable spinner update
----------------------------------
The two editable QDoubleSpinBox controls in this GUI now use a deterministic
right-side mouse hit area so the upper arrow always increments and the lower
arrow always decrements on Windows/PySide6 styles where the native UP-button
hit area can fail after an application stylesheet is applied. Keyboard entry,
Up/Down keys, mouse wheel, numeric ranges, decimals, suffixes and value semantics
remain unchanged.


Version 19 archive hierarchy
----------------------------
The PC MiniSEED archive now follows exactly:

    recordings/miniseed/YYYY/YYYYMM/YYYYMMDD/YYYYMMDD_HH/

The hourly folder no longer contains the MSEED_<STATION> prefix. File names retain
YYYYMMDD_HH_NN and therefore remain unique across repeated START sessions in the
same UTC hour. StationXML output is removed from the PC recorder so each NN set
contains only the three waveform files, obs_position_attitude CSV, USBL CSV and
session metadata JSON shown in the archive specification.


Version 18 IMU offset logging
-----------------------------
Roll/Pitch/Yaw written to obs_position_attitude_YYYYMMDD_HH_NN.csv are now the
offset-corrected physical attitude values. Offsets are additive degrees loaded
from imu_offsets.ini:

    [IMU]
    roll_offset_deg = ...
    pitch_offset_deg = ...
    yaw_offset_deg = ...

The file is shared with other_sensors_v7.py. Changes are detected while recording
and subsequent AHRS rows use the updated offsets. Source shared-RAM attitude is
never modified. This change affects the PC position/attitude CSV only; OBS onboard
USB .bin logging remains a firmware-side raw log.

Version 16 removed the separate <STATION>_imu.mseed recording. Version 19 keeps
that behavior and uses the finalized YYYY/YYYYMM/YYYYMMDD/YYYYMMDD_HH archive
hierarchy, with one or more NN-indexed recording sets per UTC-hour folder. IMU
attitude remains available in the per-session position/attitude CSV together with
UTC timestamp, USBL longitude/latitude, depth, and USBL freshness.

OBS onboard USB logging is intentionally NOT stopped when this GUI closes. Once
RMCMD 61 has been confirmed by MSDV_FILE_OPENED, the logger is an OBS-side
firmware function; loss of the PC therefore does not cause this application to
send RMCMD 62.

Dependencies
------------
    pip install PySide6 numpy obspy

No SciPy dependency is required. Geophone samples are read from the authoritative
processed ADC stream in shared_data_v10.
"""

from __future__ import annotations

import configparser
import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.MINISEED"


def configure_windows_runtime() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
        )

        try:
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                kernel32.FreeConsole()
        except Exception:
            pass

    except Exception:
        pass


configure_windows_runtime()


# =============================================================================
# Qt
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


# =============================================================================
# NumPy / ObsPy
# =============================================================================

try:
    import numpy as np
except ImportError:
    np = None

try:
    from obspy import Stream, Trace, UTCDateTime

    OBSPY_AVAILABLE = True
    OBSPY_ERROR = ""

except Exception as exc:
    Stream = None
    Trace = None
    UTCDateTime = None

    OBSPY_AVAILABLE = False
    OBSPY_ERROR = str(exc)


# =============================================================================
# Shared data
# =============================================================================

from shared_data import (
    RAW_ADC_SAMPLE_RATE_HZ,
    SHARED_DATA_API_VERSION,
    OBSSharedData,
)

from obs_ipc import OBSIPCClient


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "MiniSEED Recording"
APP_VERSION = "29"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

# Set by Main for child processes. The file exists only after the operator has
# answered YES to the Main shutdown confirmation. It lets this window suppress
# its own second confirmation and finalize PC recording automatically.
MAIN_SHUTDOWN_FLAG_ENV = "OBS_MAIN_SHUTDOWN_FLAG"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

OBS_SETTINGS_INI = BASE_DIR / "obs_settings.ini"
IMU_OFFSET_INI = BASE_DIR / "imu_offsets.ini"

DEFAULT_ROLL_OFFSET_DEG = 0.0
DEFAULT_PITCH_OFFSET_DEG = 0.0
DEFAULT_YAW_OFFSET_DEG = 0.0

# OBS command channel used by onboard USB binary logging control.
OBS_COMMAND_MAX_SENTENCE_BYTES = 82
OBS_USB_LOG_START = 61
OBS_USB_LOG_STOP = 62
OBS_USB_LOG_ALLOWED_CODES = frozenset((OBS_USB_LOG_START, OBS_USB_LOG_STOP))
OBS_COMMAND_CONNECT_TIMEOUT_S = 2.5
OBS_COMMAND_RECONNECT_S = 1.0
OBS_COMMAND_RX_TIMEOUT_S = 0.25
OBS_USB_CONFIRM_TIMEOUT_S = 5.0
OBS_USB_ROTATION_GRACE_S = 1.5
OBS_TIME_CONFIRM_TIMEOUT_S = 5.0
OBS_TIME_CONFIRM_TOLERANCE_S = 2.0

# v13 time-discipline settings.  A fresh TIME1 sample is extrapolated to the
# current display instant before OBS-PC difference is calculated.  Automatic
# resync is deliberately rate-limited; it is a clock-maintenance aid, not a
# high-rate servo loop.
OBS_TIME_AUTO_SYNC_DEFAULT = True
OBS_TIME_AUTO_SYNC_DEFAULT_THRESHOLD_S = 0.50
OBS_TIME_AUTO_SYNC_MIN_THRESHOLD_S = 0.10
OBS_TIME_AUTO_SYNC_MAX_THRESHOLD_S = 5.00
OBS_TIME_AUTO_SYNC_MIN_INTERVAL_S = 60.0
OBS_TIME_AUTO_SYNC_MAX_SAMPLE_AGE_S = 5.0
GNSS_TIME_MAX_SAMPLE_AGE_S = 2.0
TIME_SOURCE_PC = "PC TIME"
TIME_SOURCE_GNSS = "GNSS TIME"

OBS_STRG0_MAX_SENTENCE_BYTES = 192
OBS_TIME1_MAX_SENTENCE_BYTES = 96
OBS_TIME1_COUNTER = 0

DEFAULT_NETWORK = "RM"
DEFAULT_STATION = "OBS01"
DEFAULT_LOCATION = "00"

DEFAULT_GEO_N_CHANNEL = "EHN"
DEFAULT_GEO_E_CHANNEL = "EHE"
DEFAULT_GEO_Z_CHANNEL = "EHZ"

DEFAULT_ROLL_CHANNEL = "RLL"
DEFAULT_PITCH_CHANNEL = "PIT"
DEFAULT_YAW_CHANNEL = "YAW"

DEFAULT_IMU_RATE_HZ = 25.0

# v24 operational USBL freshness. Cerulean/NMEA is normally 1 Hz; after
# three seconds without new source/GGA data, retain last-known coordinates but
# mark the effective fix invalid in both operational CSV files.
POSITION_ATTITUDE_USBL_FRESH_S = 3.0

# Buffered writing keeps MiniSEED record overhead reasonable.
GEOPHONE_FLUSH_SECONDS = 10.0
IMU_FLUSH_SECONDS = 60.0

WORKER_POLL_MS = 10
GUI_STATUS_MS = 250

GEOPHONE_MSEED_RECLEN = 4096
IMU_MSEED_RECLEN = 512

MAX_NEW_ADC_REQUEST = int(
    max(
        1.0,
        float(RAW_ADC_SAMPLE_RATE_HZ) * 120.0,
    )
)


# =============================================================================
# Helpers
# =============================================================================


def application_icon() -> QIcon:
    candidates = (
        [APP_ICON_ICO, APP_ICON_PNG]
        if os.name == "nt"
        else [APP_ICON_PNG, APP_ICON_ICO]
    )

    for path in candidates:
        if path.is_file():
            icon = QIcon(str(path))

            if not icon.isNull():
                return icon

    return QIcon()


# =============================================================================
# Reliable double spin box
# =============================================================================

class ReliableDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox with deterministic mouse hit-zones for UP/DOWN arrows.

    Some Windows/PySide6 style combinations can draw both native arrow glyphs
    correctly while the UP-button mouse hit area does not behave reliably after
    an application stylesheet is applied. Keep the normal QDoubleSpinBox
    behavior, but make the right-side button band deterministic:

        upper half -> stepUp()
        lower half -> stepDown()

    The native implementation remains active everywhere outside that band.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccelerated(True)

    def mousePressEvent(self, event) -> None:
        if (
            self.isEnabled()
            and not self.isReadOnly()
            and event.button() == Qt.LeftButton
        ):
            position = event.position()

            # Use a practical target width instead of relying on the small
            # native arrow-glyph hit area. This also helps on high-DPI displays.
            button_band_width = max(
                24,
                min(
                    32,
                    int(self.height()),
                ),
            )

            if position.x() >= self.width() - button_band_width:
                if position.y() < (self.height() / 2.0):
                    self.stepUp()
                else:
                    self.stepDown()

                event.accept()
                return

        super().mousePressEvent(event)


def obs_xor_checksum(
    body: str,
) -> int:
    checksum = 0
    for value in body.encode(
        "ascii",
        errors="strict",
    ):
        checksum ^= value
    return checksum


def build_obs_usb_log_sentence(
    command_code: int,
) -> bytes:
    """Build one firmware-defined USB logging RMCMD sentence."""
    command_code = int(command_code)
    if command_code not in OBS_USB_LOG_ALLOWED_CODES:
        raise ValueError(
            f"Unsupported OBS USB logging command {command_code}. "
            "Allowed codes are 61 (START) and 62 (STOP)."
        )

    body = f"RMCMD,{command_code}"
    checksum = obs_xor_checksum(body)
    sentence = f"${body}*{checksum:02X}\r\n".encode("ascii")

    if len(sentence) > OBS_COMMAND_MAX_SENTENCE_BYTES:
        raise ValueError(
            f"OBS command exceeds {OBS_COMMAND_MAX_SENTENCE_BYTES} bytes."
        )
    return sentence


def _utc_offset_text(
    dt: datetime,
) -> str:
    """Return a stable UTC+HH:MM / UTC-HH:MM string from an aware datetime."""
    offset = dt.utcoffset()
    if offset is None:
        return "UTC?"
    total_minutes = int(round(offset.total_seconds() / 60.0))
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def current_pc_local_time() -> datetime:
    """Return the PC's current timezone-aware local wall-clock time."""
    return datetime.now().astimezone()


def _nmea_checksum_valid(sentence: str) -> bool:
    text = str(sentence or "").strip()
    if not text.startswith("$") or "*" not in text:
        return False
    body, checksum_text = text[1:].rsplit("*", 1)
    checksum_text = checksum_text[:2]
    if len(checksum_text) != 2:
        return False
    try:
        expected = int(checksum_text, 16)
    except ValueError:
        return False
    checksum = 0
    try:
        for value in body.encode("ascii", errors="strict"):
            checksum ^= value
    except UnicodeEncodeError:
        return False
    return checksum == expected


def _nmea_sentence_and_timestamp(snapshot) -> tuple[str, int]:
    """Return (sentence, timestamp_ns) from shared_data NMEA snapshot variants."""
    if snapshot is None:
        return "", 0
    if isinstance(snapshot, str):
        return snapshot.strip(), 0
    if isinstance(snapshot, dict):
        sentence = str(snapshot.get("sentence") or snapshot.get("raw") or "").strip()
        timestamp_ns = int(snapshot.get("timestamp_ns") or 0)
        return sentence, timestamp_ns
    if isinstance(snapshot, (tuple, list)):
        sentence = str(snapshot[0] if snapshot else "").strip()
        timestamp_ns = int(snapshot[1] if len(snapshot) > 1 else 0)
        return sentence, timestamp_ns
    sentence = str(
        getattr(snapshot, "sentence", "")
        or getattr(snapshot, "raw", "")
        or getattr(snapshot, "text", "")
    ).strip()
    timestamp_ns = int(getattr(snapshot, "timestamp_ns", 0) or 0)
    return sentence, timestamp_ns


def _parse_hhmmss_utc(value: str) -> Optional[tuple[int, int, int, int]]:
    text = str(value or "").strip()
    if len(text) < 6:
        return None
    try:
        hour = int(text[0:2])
        minute = int(text[2:4])
        sec_float = float(text[4:])
        second = int(sec_float)
        microsecond = int(round((sec_float - second) * 1_000_000.0))
        if microsecond >= 1_000_000:
            second += 1
            microsecond -= 1_000_000
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        return hour, minute, second, microsecond
    except Exception:
        return None


def parse_gnss_utc_datetime(sentence: str, *, pc_utc_now: Optional[datetime] = None) -> Optional[dict]:
    """Parse GGA/RMC/ZDA GNSS UTC time.

    RMC/ZDA supply the calendar date. GGA supplies time only, so the nearest PC UTC
    date is used to avoid the normal midnight rollover ambiguity.
    """
    text = str(sentence or "").strip()
    if not _nmea_checksum_valid(text):
        return None
    body = text[1:text.rfind("*")]
    fields = body.split(",")
    if not fields:
        return None
    msg = fields[0].upper()
    suffix = msg[-3:]
    if suffix not in ("GGA", "RMC", "ZDA"):
        return None
    if len(fields) < 2:
        return None
    parsed_time = _parse_hhmmss_utc(fields[1])
    if parsed_time is None:
        return None
    hour, minute, second, microsecond = parsed_time
    now_utc = pc_utc_now or datetime.now(timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    date_source = "GNSS"

    try:
        if suffix == "RMC":
            if len(fields) < 10 or str(fields[2]).upper() != "A":
                return None
            d = str(fields[9]).strip()
            if len(d) != 6 or not d.isdigit():
                return None
            day, month, yy = int(d[:2]), int(d[2:4]), int(d[4:6])
            year = 2000 + yy if yy < 80 else 1900 + yy
            dt = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc)
        elif suffix == "ZDA":
            if len(fields) < 5:
                return None
            day, month, year = int(fields[2]), int(fields[3]), int(fields[4])
            dt = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc)
        else:  # GGA: UTC time only
            date_source = "PC UTC DATE + GNSS GGA TIME"
            base = datetime(now_utc.year, now_utc.month, now_utc.day, hour, minute, second, microsecond, tzinfo=timezone.utc)
            candidates = (base - timedelta(days=1), base, base + timedelta(days=1))
            dt = min(candidates, key=lambda item: abs((item - now_utc).total_seconds()))
    except Exception:
        return None

    return {
        "datetime": dt,
        "sentence_type": suffix,
        "date_source": date_source,
        "raw": text,
    }


def time1_week_from_datetime(
    dt: datetime,
) -> int:
    """
    Map Python weekday to the firmware example convention:
        Sunday=1, Monday=2, ... Saturday=7.

    2026-08-27 is Thursday and therefore maps to 5, matching the supplied
    example $TIME1,...,8,5,27,...
    """
    return ((int(dt.weekday()) + 1) % 7) + 1


def build_obs_time1_sentence(
    dt: Optional[datetime] = None,
    *,
    counter: int = OBS_TIME1_COUNTER,
) -> tuple[bytes, dict]:
    """
    Build one firmware TIME1 time-update sentence from the factual PC local time.

    The supplied firmware example uses year=26 for 2026, therefore the outbound
    command intentionally uses a two-digit year while keeping all other fields
    in their documented integer ranges.
    """
    if dt is None:
        dt = current_pc_local_time()
    elif dt.tzinfo is None:
        # Treat a naive value as local PC wall time, not UTC.
        dt = dt.astimezone()
    # If an aware datetime is supplied, preserve its own local wall-clock
    # fields. The production path already supplies current_pc_local_time().

    milliseconds = int(dt.microsecond // 1000)
    year = int(dt.year % 100)
    month = int(dt.month)
    week = int(time1_week_from_datetime(dt))
    date = int(dt.day)
    hours = int(dt.hour)
    minutes = int(dt.minute)
    seconds = int(dt.second)
    counter = int(counter) & 0xFF

    body = (
        f"TIME1,{milliseconds},{year},{month},{week},{date},"
        f"{hours},{minutes},{seconds},{counter}"
    )
    checksum = obs_xor_checksum(body)
    sentence = f"${body}*{checksum:02X}\r\n".encode("ascii")

    if len(sentence) > OBS_COMMAND_MAX_SENTENCE_BYTES:
        raise ValueError(
            f"TIME1 command exceeds {OBS_COMMAND_MAX_SENTENCE_BYTES} bytes."
        )

    fields = {
        "milliseconds": milliseconds,
        "year": year,
        "month": month,
        "week": week,
        "date": date,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "counter": counter,
        "pc_iso": dt.isoformat(timespec="milliseconds"),
        "utc_offset": _utc_offset_text(dt),
    }
    return sentence, fields


def parse_time1_sentence(
    sentence: str,
) -> Optional[dict]:
    """Parse and checksum-validate one firmware $TIME1 sentence."""
    text = str(sentence).strip("\r\n ")
    if not text.startswith("$TIME1,"):
        return None

    star_index = text.rfind("*")
    if star_index <= 1:
        return {
            "valid": False,
            "raw": text,
            "error": "missing checksum",
        }

    body = text[1:star_index]
    checksum_text = text[star_index + 1:star_index + 3]
    if len(checksum_text) != 2:
        return {
            "valid": False,
            "raw": text,
            "error": "invalid checksum field",
        }

    try:
        expected = int(checksum_text, 16)
    except ValueError:
        return {
            "valid": False,
            "raw": text,
            "error": "non-hex checksum",
        }

    try:
        calculated = obs_xor_checksum(body)
    except UnicodeEncodeError:
        return {
            "valid": False,
            "raw": text,
            "error": "non-ASCII sentence",
        }

    if calculated != expected:
        return {
            "valid": False,
            "raw": text,
            "error": (
                f"checksum mismatch: received {expected:02X}, "
                f"calculated {calculated:02X}"
            ),
        }

    parts = body.split(",")
    if len(parts) != 10 or parts[0] != "TIME1":
        return {
            "valid": False,
            "raw": text,
            "error": "TIME1 requires exactly 9 numeric fields",
        }

    try:
        values = [int(value) for value in parts[1:]]
    except ValueError:
        return {
            "valid": False,
            "raw": text,
            "error": "TIME1 contains a non-integer field",
        }

    (
        milliseconds,
        year,
        month,
        week,
        date,
        hours,
        minutes,
        seconds,
        counter,
    ) = values

    checks = (
        (0 <= milliseconds <= 999, "milliseconds 0..999"),
        (0 <= year <= 65535, "year 0..65535"),
        (1 <= month <= 12, "month 1..12"),
        (1 <= week <= 7, "week 1..7"),
        (1 <= date <= 31, "date 1..31"),
        (0 <= hours <= 23, "hours 0..23"),
        (0 <= minutes <= 59, "minutes 0..59"),
        (0 <= seconds <= 59, "seconds 0..59"),
        (0 <= counter <= 255, "counter 0..255"),
    )
    for valid, description in checks:
        if not valid:
            return {
                "valid": False,
                "raw": text,
                "error": f"TIME1 field out of range: {description}",
            }

    return {
        "valid": True,
        "raw": text,
        "error": "",
        "milliseconds": milliseconds,
        "year": year,
        "month": month,
        "week": week,
        "date": date,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "counter": counter,
    }


def _full_year_from_time1(
    year: int,
) -> int:
    """Interpret firmware two-digit years as 2000..2099 for display/comparison."""
    year = int(year)
    return 2000 + year if 0 <= year <= 99 else year


def _time1_fields_to_naive_datetime(
    fields: dict,
) -> Optional[datetime]:
    try:
        return datetime(
            _full_year_from_time1(fields["year"]),
            int(fields["month"]),
            int(fields["date"]),
            int(fields["hours"]),
            int(fields["minutes"]),
            int(fields["seconds"]),
            int(fields["milliseconds"]) * 1000,
        )
    except Exception:
        return None


def parse_strg0_sentence(
    sentence: str,
) -> Optional[dict]:
    """Parse one checksum-validated firmware $STRG0 status sentence."""
    text = str(sentence).strip("\r\n ")
    if not text.startswith("$STRG0,"):
        return None

    star_index = text.rfind("*")
    if star_index <= 1:
        return {
            "valid": False,
            "raw": text,
            "message": "",
            "error": "missing checksum",
        }

    body = text[1:star_index]
    checksum_text = text[star_index + 1:star_index + 3]
    if len(checksum_text) != 2:
        return {
            "valid": False,
            "raw": text,
            "message": "",
            "error": "invalid checksum field",
        }

    try:
        expected = int(checksum_text, 16)
    except ValueError:
        return {
            "valid": False,
            "raw": text,
            "message": "",
            "error": "non-hex checksum",
        }

    try:
        calculated = obs_xor_checksum(body)
    except UnicodeEncodeError:
        return {
            "valid": False,
            "raw": text,
            "message": "",
            "error": "non-ASCII sentence",
        }

    if calculated != expected:
        return {
            "valid": False,
            "raw": text,
            "message": "",
            "error": (
                f"checksum mismatch: received {expected:02X}, "
                f"calculated {calculated:02X}"
            ),
        }

    prefix = "STRG0,"
    if not body.startswith(prefix):
        return None

    message = body[len(prefix):]
    return {
        "valid": True,
        "raw": text,
        "message": message,
        "error": "",
    }


def classify_strg0_message(
    message: str,
) -> tuple[str, str]:
    """Return (event_type, detail) for one STRG0 MESSAGE_STRING."""
    text = str(message).strip()

    exact = {
        "USB_STORAGE_MEDIA_INSERTION": "MEDIA_INSERTION",
        "USB_STORAGE_MEDIA_REMOVAL": "MEDIA_REMOVAL",
        "USB_DEVICE_CONNECTION": "DEVICE_CONNECTION",
        "USB_DEVICE_DISCONNECTION": "DEVICE_DISCONNECTION",
        "USB_DEVICE_ENUMERATION_FAILURE": "ENUMERATION_FAILURE",
        "USB_NO_DEVICE_CONNECTED": "NO_DEVICE",
        "USB_EVENT_UNKNOWN": "EVENT_UNKNOWN",
        "USB_DEVICE_INSERTION": "DEVICE_INSERTION",
        "USB_DEVICE_REMOVAL": "DEVICE_DISCONNECTION",
    }
    if text in exact:
        return exact[text], text

    if text.startswith("USB_ERROR_UNKNOWN:"):
        return "USB_ERROR", text

    if text.startswith("USB_MSC_VID:"):
        return "MSC_ID", text

    if text.startswith("MSDV_FILE_OPENED:"):
        return "FILE_OPENED", text.split(":", 1)[1].strip()

    if text.startswith("MSDV_FILE_CLOSED:"):
        return "FILE_CLOSED", text.split(":", 1)[1].strip()

    return "OTHER", text


def wrap_angle_deg(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _finite_offset(value, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return result if math.isfinite(result) else float(fallback)


def load_imu_offsets_deg() -> tuple[float, float, float]:
    """Load additive Roll/Pitch/Yaw offsets shared with Other Sensors v7."""
    roll = DEFAULT_ROLL_OFFSET_DEG
    pitch = DEFAULT_PITCH_OFFSET_DEG
    yaw = DEFAULT_YAW_OFFSET_DEG

    if not IMU_OFFSET_INI.is_file():
        return roll, pitch, yaw

    parser = configparser.ConfigParser()
    try:
        parser.read(IMU_OFFSET_INI, encoding="utf-8")
        roll = _finite_offset(
            parser.get("IMU", "roll_offset_deg", fallback=str(roll)),
            roll,
        )
        pitch = _finite_offset(
            parser.get("IMU", "pitch_offset_deg", fallback=str(pitch)),
            pitch,
        )
        yaw = _finite_offset(
            parser.get("IMU", "yaw_offset_deg", fallback=str(yaw)),
            yaw,
        )
    except Exception:
        pass

    return float(roll), float(pitch), float(yaw)


def apply_imu_offsets_deg(
    roll_deg: float,
    pitch_deg: float,
    yaw_deg: float,
    offsets: tuple[float, float, float],
) -> tuple[float, float, float]:
    roll_offset, pitch_offset, yaw_offset = offsets
    return (
        wrap_angle_deg(float(roll_deg) + float(roll_offset)),
        wrap_angle_deg(float(pitch_deg) + float(pitch_offset)),
        wrap_angle_deg(float(yaw_deg) + float(yaw_offset)),
    )


def default_record_folder() -> Path:
    """
    Prefer [Recording]/miniseed_folder from obs_settings.ini.
    """

    fallback = (
        BASE_DIR
        / "recordings"
        / "miniseed"
    )

    try:
        parser = configparser.ConfigParser()
        parser.read(
            OBS_SETTINGS_INI,
            encoding="utf-8",
        )

        value = parser.get(
            "Recording",
            "miniseed_folder",
            fallback=str(
                fallback
            ),
        ).strip()

        path = Path(value)

        if not path.is_absolute():
            path = (
                BASE_DIR
                / path
            )

        return path.resolve()

    except Exception:
        return fallback


def valid_position(
    position,
) -> bool:
    try:
        return (
            bool(
                position.valid
            )
            and math.isfinite(
                float(
                    position.latitude
                )
            )
            and math.isfinite(
                float(
                    position.longitude
                )
            )
            and -90.0
            <= float(
                position.latitude
            )
            <= 90.0
            and -180.0
            <= float(
                position.longitude
            )
            <= 180.0
        )
    except Exception:
        return False


def iso_utc_from_ns(
    timestamp_ns: int,
) -> str:
    if timestamp_ns <= 0:
        return ""

    seconds = (
        int(timestamp_ns)
        / 1_000_000_000.0
    )

    if UTCDateTime is not None:
        try:
            return str(
                UTCDateTime(
                    seconds
                )
            )
        except Exception:
            pass

    return time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(
            seconds
        ),
    )


def csv_utc_from_ns(timestamp_ns: int) -> str:
    """Return CSV UTC time as YYYY-MM-DD HH:MM:SS (no T/Z/milliseconds)."""
    timestamp_ns = int(timestamp_ns or 0)
    if timestamp_ns <= 0:
        return ""
    value = datetime.fromtimestamp(
        timestamp_ns / 1_000_000_000.0,
        tz=timezone.utc,
    )
    return value.strftime("%Y-%m-%d %H:%M:%S")


def safe_filename_component(
    value: str,
) -> str:
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(
            value
        ).strip(),
    )

    return value or "OBS"


def shared_depth_m(telemetry) -> float:
    """Return physical OBS depth in metres from the authoritative shared slot.

    The current OBS shared-memory layout retains the legacy attribute name
    ``pressure`` for binary/API compatibility, but $DEPT0 field 0 is the physical
    depth value in metres.  Other Sensors uses the same interpretation.
    ``depth`` is only a compatibility fallback for an older/future snapshot that
    does not expose the legacy slot.
    """
    try:
        return float(telemetry.pressure)
    except (AttributeError, TypeError, ValueError):
        return float(getattr(telemetry, "depth", 0.0))


# =============================================================================
# Recording configuration
# =============================================================================


@dataclass(frozen=True)
class RecorderConfig:
    base_folder: str

    network: str
    station: str
    location: str

    geo_n_channel: str
    geo_e_channel: str
    geo_z_channel: str

    roll_channel: str
    pitch_channel: str
    yaw_channel: str

    raw_adc_rate_hz: float
    shared_decimation_samples: int
    shared_decimation_mode: str
    geophone_rate_hz: float
    adc_session_id: int

    imu_rate_hz: float

    record_geophone: bool
    record_imu: bool
    log_usbl: bool

    @property
    def output_geophone_rate_hz(
        self,
    ) -> float:
        return float(
            self.geophone_rate_hz
        )


# =============================================================================
# MiniSEED helpers
# =============================================================================


def append_miniseed_stream(
    path: Path,
    stream,
    reclen: int,
    *,
    encoding: str,
):
    buffer = io.BytesIO()

    stream.write(
        buffer,
        format="MSEED",
        encoding=str(encoding),
        reclen=int(
            reclen
        ),
    )

    payload = buffer.getvalue()

    with path.open(
        "ab"
    ) as handle:
        handle.write(
            payload
        )

    return len(
        payload
    )


def make_trace(
    data,
    *,
    config: RecorderConfig,
    channel: str,
    start_timestamp_ns: int,
    sample_rate_hz: float,
    dtype=np.float32,
):
    trace = Trace(
        data=np.asarray(
            data,
            dtype=dtype,
        )
    )

    trace.stats.network = (
        config.network
    )
    trace.stats.station = (
        config.station
    )
    trace.stats.location = (
        config.location
    )
    trace.stats.channel = (
        channel
    )

    trace.stats.starttime = UTCDateTime(
        int(
            start_timestamp_ns
        )
        / 1_000_000_000.0
    )

    trace.stats.sampling_rate = float(
        sample_rate_hz
    )

    trace.stats.mseed = {
        "dataquality": "D"
    }

    return trace


# =============================================================================
# Recording worker
# =============================================================================


class MiniSeedRecordingWorker(QThread):
    status_changed = Signal(str)
    stats_changed = Signal(object)
    recording_error = Signal(str)
    recording_finished = Signal(str)

    def __init__(
        self,
        config: RecorderConfig,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.config = config

        self._stop_event = (
            threading.Event()
        )

        self.session_folder = None

        self.raw_adc_samples = 0
        self.decimated_samples = 0
        self.imu_samples = 0
        self.bytes_written = 0
        self.recorder_lag_samples = 0

        self.metadata_json_file = None

        self.geo_buffer = [
            [],
            [],
            [],
        ]
        self.geo_time_buffer = []

        self.imu_buffer = [
            [],
            [],
            [],
        ]
        self.imu_time_buffer = []

        self.last_adc_total = None
        self.last_telemetry_timestamp_ns = -1
        self.last_usbl_timestamp_ns = -1

        self.geophone_files = [None, None, None]
        self.imu_file = None
        self.usbl_csv_file = None
        self.usbl_csv_writer = None

        # v23 combined position / attitude CSV state. This log is independent of
        # the optional standalone usbl_position.csv history and is sampled at 1 Hz
        # from the latest shared-RAM state using the PC UTC clock.
        self.position_attitude_csv_file = None
        self.position_attitude_csv_writer = None
        self._position_attitude_handle = None
        self.last_position_attitude_utc_second = -1
        self.last_usbl_csv_utc_second = -1
        self.last_valid_usbl_timestamp_ns = 0
        self.last_valid_usbl_longitude = None
        self.last_valid_usbl_latitude = None
        self.last_valid_usbl_altitude = None
        self.last_valid_usbl_satellites = None
        self.last_valid_usbl_hdop = None

        # Version 19 retains the IMU installation offsets introduced in v18.
        # The file signature is checked on
        # each new AHRS row so changes made in Other Sensors take effect without
        # restarting an active PC recording session.
        self._imu_offsets = load_imu_offsets_deg()
        self._imu_offset_signature = None

        # Overall PC recording-session start. Elapsed GUI time continues across
        # automatic UTC-hour folder rotations.
        self.session_start_ns = (
            time.time_ns()
        )

        # v19 UTC archive hierarchy / per-session-file state. Naming/rotation uses PC UTC;
        # waveform timestamps remain the authoritative shared_data_v10 timestamps.
        self.segment_start_ns = self.session_start_ns
        self.segment_hour_key = ""
        self.segment_hour_utc = None
        self.segment_index_in_hour = 0
        self.segment_rotation_reason = "manual_start"
        self.segment_raw_adc_start = 0
        self.segment_geo_samples_start = 0
        self.segment_imu_samples_start = 0
        self.segment_bytes_start = 0
        self.segment_lag_start = 0
        self.segment_usbl_start = {}
        self.segment_depth_start_m = 0.0
        self.last_completed_folder = None
        self._usbl_handle = None

    def stop_recording(
        self,
    ):
        self._stop_event.set()

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _pc_utc_now() -> datetime:
        """Return timezone-aware PC UTC time used only for folder rotation/naming."""
        return datetime.now(timezone.utc)

    @staticmethod
    def _utc_hour_key(value: datetime) -> str:
        value = value.astimezone(timezone.utc)
        return value.strftime("%Y%m%d_%H")

    def _create_session_folder(
        self,
        *,
        now_utc: Optional[datetime] = None,
        rotation_reason: str = "manual_start",
    ):
        """Activate one NN-indexed recording set inside a UTC-hour folder.

        Layout:
            <base>/YYYY/YYYYMM/YYYYMMDD/YYYYMMDD_HH/

        Files:
            <STATION>_N_<CH>_YYYYMMDD_HH_NN.mseed
            <STATION>_E_<CH>_YYYYMMDD_HH_NN.mseed
            <STATION>_Z_<CH>_YYYYMMDD_HH_NN.mseed
            obs_position_attitude_YYYYMMDD_HH_NN.csv
            usbl_position_YYYYMMDD_HH_NN.csv
            session_metadata_YYYYMMDD_HH_NN.json

        HH is taken from the PC UTC system clock. NN is selected by scanning the
        existing files in the hourly folder, so STOP/START or application restart
        in the same UTC hour never overwrites an older recording set.
        """
        if now_utc is None:
            now_utc = self._pc_utc_now()
        else:
            now_utc = now_utc.astimezone(timezone.utc)

        hour_utc = now_utc.replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        base = Path(self.config.base_folder)
        year_folder = base / f"{hour_utc.year:04d}"
        month_folder = year_folder / f"{hour_utc.year:04d}{hour_utc.month:02d}"
        day_stamp = hour_utc.strftime("%Y%m%d")
        day_folder = month_folder / day_stamp
        day_folder.mkdir(parents=True, exist_ok=True)

        station = safe_filename_component(self.config.station)
        hour_stamp = hour_utc.strftime("%Y%m%d_%H")
        folder = day_folder / hour_stamp
        folder.mkdir(parents=False, exist_ok=True)

        # Find the highest NN already present in this UTC-hour folder. Scan all
        # supported per-session output extensions so recovery remains safe even if
        # a previous run stopped before every file in its set was created.
        max_index = 0
        pattern = re.compile(
            rf"_{re.escape(hour_stamp)}_(\d+)\.(?:mseed|csv|json)$",
            re.IGNORECASE,
        )
        try:
            children = list(folder.iterdir())
        except FileNotFoundError:
            children = []

        for child in children:
            if not child.is_file():
                continue
            match = pattern.search(child.name)
            if match is None:
                continue
            try:
                max_index = max(max_index, int(match.group(1)))
            except Exception:
                pass

        counter = max_index + 1
        file_tag = f"{hour_stamp}_{counter:02d}"

        # Defensive collision check. Normally the scan above is sufficient, but a
        # concurrent/manual file creation must never cause an existing log to be
        # appended or overwritten unintentionally.
        while True:
            candidate_names = (
                f"{station}_N_{safe_filename_component(self.config.geo_n_channel)}_{file_tag}.mseed",
                f"{station}_E_{safe_filename_component(self.config.geo_e_channel)}_{file_tag}.mseed",
                f"{station}_Z_{safe_filename_component(self.config.geo_z_channel)}_{file_tag}.mseed",
                f"obs_position_attitude_{file_tag}.csv",
                f"usbl_position_{file_tag}.csv",
                f"session_metadata_{file_tag}.json",
            )
            if not any((folder / name).exists() for name in candidate_names):
                break
            counter += 1
            file_tag = f"{hour_stamp}_{counter:02d}"

        self.session_folder = folder
        self.segment_start_ns = time.time_ns()
        self.segment_hour_utc = hour_utc
        self.segment_hour_key = self._utc_hour_key(hour_utc)
        self.segment_index_in_hour = int(counter)
        self.segment_file_tag = file_tag
        self.segment_rotation_reason = str(rotation_reason)

        # Make each NN recording set self-contained: allow the latest known USBL
        # sample to be written again as the first row of the new set.
        self.last_usbl_timestamp_ns = -1

        # Per-segment counters are snapshots of the continuous recording counters.
        self.segment_raw_adc_start = int(self.raw_adc_samples)
        self.segment_geo_samples_start = int(self.decimated_samples)
        self.segment_imu_samples_start = int(self.imu_samples)
        self.segment_bytes_start = int(self.bytes_written)
        self.segment_lag_start = int(self.recorder_lag_samples)

        self.geophone_files = [
            folder / (
                f"{station}_N_{safe_filename_component(self.config.geo_n_channel)}_"
                f"{file_tag}.mseed"
            ),
            folder / (
                f"{station}_E_{safe_filename_component(self.config.geo_e_channel)}_"
                f"{file_tag}.mseed"
            ),
            folder / (
                f"{station}_Z_{safe_filename_component(self.config.geo_z_channel)}_"
                f"{file_tag}.mseed"
            ),
        ]

        # No separate IMU MiniSEED file. Attitude is retained in the CSV below.
        self.imu_file = None
        self.usbl_csv_file = folder / f"usbl_position_{file_tag}.csv"
        self.position_attitude_csv_file = (
            folder / f"obs_position_attitude_{file_tag}.csv"
        )
        self.metadata_json_file = folder / f"session_metadata_{file_tag}.json"

    def _write_metadata_json(
        self,
        shared,
        *,
        final: bool = False,
        end_reason: str = "",
    ):
        if self.session_folder is None:
            return

        usbl = shared.read_usbl()
        telemetry = shared.read_telemetry()
        try:
            rate_diag = shared.read_adc_rate_diagnostic()
            adc_rate_diagnostic = {
                "expected_raw_sample_rate_hz": float(
                    rate_diag.expected_raw_sample_rate_hz
                ),
                "measured_raw_sample_rate_hz": float(
                    rate_diag.measured_raw_sample_rate_hz
                ),
                "measured_output_sample_rate_hz": float(
                    rate_diag.measured_output_sample_rate_hz
                ),
                "error_ppm": float(rate_diag.error_ppm),
                "status": str(rate_diag.status),
                "measurement_window_s": float(
                    rate_diag.measurement_window_s
                ),
                "source_sample_span": int(rate_diag.source_sample_span),
                "diagnostic_only": True,
            }
        except Exception as exc:
            adc_rate_diagnostic = {
                "status": "UNAVAILABLE",
                "diagnostic_only": True,
                "error": str(exc),
            }

        segment_end_ns = time.time_ns() if final else 0
        hour_utc = self.segment_hour_utc
        hour_text = (
            hour_utc.strftime("%Y-%m-%dT%H:00:00Z")
            if hour_utc is not None
            else ""
        )

        metadata = {
            "application": "GRC-UGM-PERTAMINA OBS",
            "module": "MiniSEED Recording",
            "version": int(APP_VERSION),
            "shared_data_api_version": int(SHARED_DATA_API_VERSION),
            "time_basis": "UTC",
            "waveform_provenance": {
                "source": "shared ADC stream published by OBS Setting",
                "sample_rate_source": "ADCStreamInfo.effective_sample_rate_hz",
                "pc_rate_diagnostic_is_metadata_source": False,
                "recorder_decimation": False,
                "recorder_resampling": False,
                "recorder_interpolation": False,
                "recorder_zero_fill": False,
                "raw_obs_files_modified": False,
            },
            "folder_timestamp_source": "PC UTC system clock",
            "rotation_policy": "UTC hourly boundary",
            # Preserve the original overall recording start across all rotated
            # folders, and record the start of this individual hourly segment.
            "session_start_utc": iso_utc_from_ns(self.session_start_ns),
            "segment_start_utc": iso_utc_from_ns(self.segment_start_ns),
            "segment_end_utc": (
                iso_utc_from_ns(segment_end_ns)
                if segment_end_ns > 0
                else None
            ),
            "segment_hour_utc": hour_text,
            "segment_index_in_hour": int(self.segment_index_in_hour),
            "segment_start_reason": str(self.segment_rotation_reason),
            "segment_end_reason": str(end_reason) if final else None,
            "segment_folder": self.session_folder.name,
            "segment_file_tag": str(self.segment_file_tag),
            "config": asdict(self.config),
            "raw_adc_sample_rate_hz": self.config.raw_adc_rate_hz,
            "adc_source_sample_rate_hz": self.config.raw_adc_rate_hz,
            "shared_decimation_samples": self.config.shared_decimation_samples,
            "shared_decimation_factor": self.config.shared_decimation_samples,
            "shared_decimation_mode": self.config.shared_decimation_mode,
            "adc_session_id": self.config.adc_session_id,
            "recorded_geophone_sample_rate_hz": self.config.output_geophone_rate_hz,
            "adc_output_sample_rate_hz": self.config.output_geophone_rate_hz,
            "adc_rate_diagnostic": adc_rate_diagnostic,
            "geophone_miniseed_encoding": "STEIM2 / INT32",
            "imu_miniseed_recorded": False,
            "imu_attitude_logging": {
                "source": "shared_data_v10 physical AHRS degrees",
                "offset_file": str(IMU_OFFSET_INI),
                "offset_application": "additive then wrap to [-180, 180)",
                "offsets_deg_at_metadata_write": {
                    "roll": self._current_imu_offsets()[0],
                    "pitch": self._current_imu_offsets()[1],
                    "yaw": self._current_imu_offsets()[2],
                },
                "pc_csv_values_are_offset_corrected": True,
                "onboard_usb_bin_affected": False,
            },
            "geophone_files": {
                "N": self.geophone_files[0].name,
                "E": self.geophone_files[1].name,
                "Z": self.geophone_files[2].name,
            },
            "position_attitude_file": self.position_attitude_csv_file.name,
            "usbl_position_file": self.usbl_csv_file.name,
            "position_attitude_columns": [
                "timestamp_utc",
                "longitude_deg",
                "latitude_deg",
                "depth_m",
                "roll_deg",
                "pitch_deg",
                "yaw_deg",
                "usbl_valid",
            ],
            "position_attitude_usbl_fresh_s": POSITION_ATTITUDE_USBL_FRESH_S,
            "usbl_position_columns": [
                "timestamp_utc",
                "longitude_deg",
                "latitude_deg",
                "altitude_m",
                "fix_quality",
                "satellites",
                "hdop",
                "usbl_valid",
            ],
            "usbl_position_rate_hz": 1.0,
            "usbl_source_fresh_s": POSITION_ATTITUDE_USBL_FRESH_S,
            "geophone_record_length_bytes": GEOPHONE_MSEED_RECLEN,
            # Keep the v13 start fields for compatibility, but now they are
            # explicitly the start snapshot of this hourly segment.
            "usbl_at_start": dict(self.segment_usbl_start),
            "depth_m_at_start": float(self.segment_depth_start_m),
            "usbl_at_metadata_write": {
                "valid": bool(usbl.valid),
                "timestamp_ns": int(usbl.timestamp_ns),
                "latitude": float(usbl.latitude),
                "longitude": float(usbl.longitude),
                "altitude": float(usbl.altitude),
                "fix_quality": int(usbl.fix_quality),
                "satellites": int(usbl.satellites),
                "hdop": float(usbl.hdop),
            },
            "depth_m_at_metadata_write": shared_depth_m(telemetry),
            "segment_counters": {
                "raw_adc_samples": max(
                    0,
                    int(self.raw_adc_samples) - int(self.segment_raw_adc_start),
                ),
                "geophone_output_samples_per_component": max(
                    0,
                    int(self.decimated_samples) - int(self.segment_geo_samples_start),
                ),
                "bytes_written": max(
                    0,
                    int(self.bytes_written) - int(self.segment_bytes_start),
                ),
                "recorder_lag_samples": max(
                    0,
                    int(self.recorder_lag_samples) - int(self.segment_lag_start),
                ),
            },
            "notes": [
                (
                    "Folder naming and automatic rotation use the PC UTC system "
                    "clock. MiniSEED waveform timestamps continue to come from "
                    "the authoritative shared_data_v10 sample timestamps."
                ),
                (
                    "Geophone N/E/Z are stored in separate MiniSEED files. "
                    "Samples are written directly from the processed shared_data_v10 "
                    "ADC stream; this recorder does not apply a second decimation."
                ),
                (
                    "Version 19 does not create a separate IMU MiniSEED file. "
                    "Roll/pitch/yaw remain in the NN-indexed position/attitude CSV "
                    "and are offset-corrected using imu_offsets.ini."
                ),
                (
                    "USBL coordinates are stored in the NN-indexed USBL CSV and "
                    "position/attitude CSV rather than waveform MiniSEED records. Both "
                    "operational CSV files are fixed 1 Hz PC-UTC latest-state snapshots."
                ),
                (
                    "The NN-indexed position/attitude CSV is written at a fixed 1 Hz "
                    "PC-UTC snapshot rate. It uses last-valid USBL longitude/latitude when "
                    "USBL becomes stale or invalid, latest depth telemetry, and latest "
                    "shared roll/pitch/yaw after applying imu_offsets.ini."
                ),
            ],
        }

        path = self.metadata_json_file
        if path is None:
            return
        path.write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )

    def _open_usbl_csv(self):
        self._close_usbl_csv()

        handle = self.usbl_csv_file.open(
            "w",
            newline="",
            encoding="utf-8",
        )
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp_utc",
                "longitude_deg",
                "latitude_deg",
                "altitude_m",
                "fix_quality",
                "satellites",
                "hdop",
                "usbl_valid",
            ]
        )
        handle.flush()
        self._usbl_handle = handle
        self.usbl_csv_writer = writer

    def _close_usbl_csv(self):
        handle = self._usbl_handle
        self._usbl_handle = None
        self.usbl_csv_writer = None
        if handle is None:
            return
        try:
            handle.flush()
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass

    def _open_position_attitude_csv(self) -> None:
        self._close_position_attitude_csv()

        if self.position_attitude_csv_file is None:
            return

        handle = self.position_attitude_csv_file.open(
            "w",
            newline="",
            encoding="utf-8",
        )
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp_utc",
                "longitude_deg",
                "latitude_deg",
                "depth_m",
                "roll_deg",
                "pitch_deg",
                "yaw_deg",
                "usbl_valid",
            ]
        )
        handle.flush()
        self._position_attitude_handle = handle
        self.position_attitude_csv_writer = writer

    def _close_position_attitude_csv(self) -> None:
        handle = self._position_attitude_handle
        self._position_attitude_handle = None
        self.position_attitude_csv_writer = None
        if handle is None:
            return
        try:
            handle.flush()
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass

    def _update_last_valid_usbl(self, usbl) -> None:
        """Cache only genuinely valid coordinates; never replace them with no-fix data."""
        if not valid_position(usbl):
            return

        timestamp_ns = int(getattr(usbl, "timestamp_ns", 0) or 0)
        if timestamp_ns <= 0:
            return
        if timestamp_ns < int(self.last_valid_usbl_timestamp_ns):
            return

        self.last_valid_usbl_timestamp_ns = timestamp_ns
        self.last_valid_usbl_longitude = float(usbl.longitude)
        self.last_valid_usbl_latitude = float(usbl.latitude)
        self.last_valid_usbl_altitude = float(usbl.altitude)
        self.last_valid_usbl_satellites = int(usbl.satellites)
        self.last_valid_usbl_hdop = float(usbl.hdop)

    @staticmethod
    def _position_age_s(position, now_ns: int) -> float:
        timestamp_ns = int(getattr(position, "timestamp_ns", 0) or 0)
        if timestamp_ns <= 0:
            return float("inf")
        return max(
            0.0,
            (int(now_ns) - timestamp_ns) / 1_000_000_000.0,
        )

    def _effective_usbl_valid(self, shared, usbl, now_ns: int) -> bool:
        """Operational validity = raw valid GGA + fresh NMEA + fresh GGA."""
        try:
            health = shared.read_acquisition_health()
            source_age_s = health.source_age_s("usbl", now_ns=now_ns)
        except Exception:
            source_age_s = float("inf")

        gga_age_s = self._position_age_s(usbl, now_ns)
        return bool(
            valid_position(usbl)
            and source_age_s <= POSITION_ATTITUDE_USBL_FRESH_S
            and gga_age_s <= POSITION_ATTITUDE_USBL_FRESH_S
        )

    def _current_imu_offsets(self) -> tuple[float, float, float]:
        """Return current imu_offsets.ini values, reloading only when changed."""
        try:
            stat = IMU_OFFSET_INI.stat()
            signature = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            signature = None

        if signature != self._imu_offset_signature:
            self._imu_offsets = load_imu_offsets_deg()
            self._imu_offset_signature = signature

        return tuple(float(v) for v in self._imu_offsets)

    def _record_position_attitude(self, shared) -> None:
        """Append at most one latest-state snapshot for each PC UTC second.

        The CSV is deliberately 1 Hz regardless of AHRS, depth, or USBL source
        update rates. Each row snapshots the latest shared-RAM values; no sensor
        interpolation and no backfill of missed seconds are performed.
        """
        writer = self.position_attitude_csv_writer
        if writer is None:
            return

        now_ns = time.time_ns()
        utc_second = int(now_ns // 1_000_000_000)
        if utc_second == int(self.last_position_attitude_utc_second):
            return

        self.last_position_attitude_utc_second = utc_second
        row_timestamp_ns = utc_second * 1_000_000_000

        telemetry = shared.read_telemetry()
        usbl = shared.read_usbl()
        self._update_last_valid_usbl(usbl)

        usbl_valid = int(
            self._effective_usbl_valid(
                shared,
                usbl,
                now_ns,
            )
        )

        longitude_value = (
            ""
            if self.last_valid_usbl_longitude is None
            else f"{float(self.last_valid_usbl_longitude):.9f}"
        )
        latitude_value = (
            ""
            if self.last_valid_usbl_latitude is None
            else f"{float(self.last_valid_usbl_latitude):.9f}"
        )

        corrected_roll, corrected_pitch, corrected_yaw = apply_imu_offsets_deg(
            telemetry.roll,
            telemetry.pitch,
            telemetry.yaw,
            self._current_imu_offsets(),
        )

        writer.writerow(
            [
                csv_utc_from_ns(row_timestamp_ns),
                longitude_value,
                latitude_value,
                f"{shared_depth_m(telemetry):.6f}",
                f"{corrected_roll:.6f}",
                f"{corrected_pitch:.6f}",
                f"{corrected_yaw:.6f}",
                usbl_valid,
            ]
        )

        try:
            self._position_attitude_handle.flush()
        except Exception:
            pass

    def _activate_segment(
        self,
        shared,
        *,
        now_utc: Optional[datetime] = None,
        rotation_reason: str,
    ) -> None:
        self._create_session_folder(
            now_utc=now_utc,
            rotation_reason=rotation_reason,
        )

        usbl = shared.read_usbl()
        telemetry = shared.read_telemetry()
        self.segment_usbl_start = {
            "valid": bool(usbl.valid),
            "timestamp_ns": int(usbl.timestamp_ns),
            "latitude": float(usbl.latitude),
            "longitude": float(usbl.longitude),
            "altitude": float(usbl.altitude),
            "fix_quality": int(usbl.fix_quality),
            "satellites": int(usbl.satellites),
            "hdop": float(usbl.hdop),
        }
        self.segment_depth_start_m = shared_depth_m(telemetry)

        self._update_last_valid_usbl(usbl)
        self._write_metadata_json(shared)
        self._open_usbl_csv()
        self._open_position_attitude_csv()
        self.last_usbl_csv_utc_second = -1
        self.last_position_attitude_utc_second = -1

    def _finalize_current_segment(
        self,
        shared,
        *,
        end_reason: str,
    ) -> None:
        if self.session_folder is None:
            return

        if self.config.record_geophone:
            self._flush_geophone()
        # No separate IMU MiniSEED is written/flushed.
        self._close_usbl_csv()
        self._close_position_attitude_csv()
        self._write_metadata_json(
            shared,
            final=True,
            end_reason=end_reason,
        )
        self.last_completed_folder = self.session_folder

    def _rotate_if_utc_hour_changed(self, shared) -> bool:
        if not self.segment_hour_key:
            return False

        now_utc = self._pc_utc_now()
        current_key = self._utc_hour_key(now_utc)
        if current_key == self.segment_hour_key:
            return False

        previous_folder = self.session_folder
        self.status_changed.emit(
            f"UTC hour changed; finalizing {previous_folder}"
        )
        self._finalize_current_segment(
            shared,
            end_reason="utc_hour_boundary",
        )
        self._activate_segment(
            shared,
            now_utc=now_utc,
            rotation_reason="hour_boundary",
        )
        self.status_changed.emit(
            f"Recording continued in {self.session_folder}"
        )
        return True

    # ------------------------------------------------------------------ USBL

    def _record_usbl(
        self,
        shared,
    ) -> None:
        """Write one latest-state USBL row for each observed PC UTC second."""
        if not self.config.log_usbl:
            return

        writer = self.usbl_csv_writer
        if writer is None:
            return

        now_ns = time.time_ns()
        utc_second = int(now_ns // 1_000_000_000)
        if utc_second == int(self.last_usbl_csv_utc_second):
            return

        self.last_usbl_csv_utc_second = utc_second
        row_timestamp_ns = utc_second * 1_000_000_000

        usbl = shared.read_usbl()
        self._update_last_valid_usbl(usbl)

        effective_valid = self._effective_usbl_valid(
            shared,
            usbl,
            now_ns,
        )
        usbl_valid = int(effective_valid)

        longitude_value = (
            ""
            if self.last_valid_usbl_longitude is None
            else f"{float(self.last_valid_usbl_longitude):.9f}"
        )
        latitude_value = (
            ""
            if self.last_valid_usbl_latitude is None
            else f"{float(self.last_valid_usbl_latitude):.9f}"
        )
        altitude_value = (
            ""
            if self.last_valid_usbl_altitude is None
            else f"{float(self.last_valid_usbl_altitude):.3f}"
        )
        satellites_value = (
            ""
            if self.last_valid_usbl_satellites is None
            else str(int(self.last_valid_usbl_satellites))
        )
        hdop_value = (
            ""
            if self.last_valid_usbl_hdop is None
            else f"{float(self.last_valid_usbl_hdop):.3f}"
        )

        # A stale/no-fix row keeps the last valid coordinates but explicitly
        # advertises effective fix_quality=0 and usbl_valid=0.
        fix_quality = (
            int(usbl.fix_quality)
            if effective_valid
            else 0
        )

        writer.writerow(
            [
                csv_utc_from_ns(row_timestamp_ns),
                longitude_value,
                latitude_value,
                altitude_value,
                fix_quality,
                satellites_value,
                hdop_value,
                usbl_valid,
            ]
        )

        try:
            self._usbl_handle.flush()
        except Exception:
            pass


    # ------------------------------------------------------------------ geophone

    def _append_geo_segment(
        self,
        signals,
        timestamps_ns,
    ):
        if signals.shape[
            1
        ] <= 0:
            return

        for channel in range(
            3
        ):
            self.geo_buffer[
                channel
            ].append(
                np.asarray(
                    signals[
                        channel
                    ],
                    dtype=np.int32,
                )
            )

        self.geo_time_buffer.append(
            np.asarray(
                timestamps_ns,
                dtype=np.int64,
            )
        )

        self.decimated_samples += int(
            signals.shape[
                1
            ]
        )

    def _geo_buffer_count(
        self,
    ):
        if not self.geo_time_buffer:
            return 0

        return int(
            sum(
                len(
                    item
                )
                for item
                in self.geo_time_buffer
            )
        )

    def _flush_geophone(
        self,
    ):
        count = (
            self._geo_buffer_count()
        )

        if count <= 0:
            return

        timestamps = np.concatenate(
            self.geo_time_buffer
        )

        n = np.concatenate(
            self.geo_buffer[
                0
            ]
        )
        e = np.concatenate(
            self.geo_buffer[
                1
            ]
        )
        z = np.concatenate(
            self.geo_buffer[
                2
            ]
        )

        # Buffers only contain one contiguous FIR segment. If a gap is detected
        # by the worker it flushes BEFORE appending the next segment.
        start_ns = int(
            timestamps[
                0
            ]
        )

        component_data = (
            n,
            e,
            z,
        )

        component_channels = (
            self.config.geo_n_channel,
            self.config.geo_e_channel,
            self.config.geo_z_channel,
        )

        for data, channel, path in zip(
            component_data,
            component_channels,
            self.geophone_files,
        ):
            stream = Stream(
                traces=[
                    make_trace(
                        data,
                        config=self.config,
                        channel=channel,
                        start_timestamp_ns=start_ns,
                        sample_rate_hz=(
                            self.config.output_geophone_rate_hz
                        ),
                        dtype=np.int32,
                    )
                ]
            )

            self.bytes_written += (
                append_miniseed_stream(
                    path,
                    stream,
                    GEOPHONE_MSEED_RECLEN,
                    encoding="STEIM2",
                )
            )

        self.geo_buffer = [
            [],
            [],
            [],
        ]
        self.geo_time_buffer = []

    def _record_new_adc(
        self,
        shared,
    ):
        total = (
            shared.adc_total_samples()
        )

        if self.last_adc_total is None:
            self.last_adc_total = int(
                total
            )
            return

        if total < self.last_adc_total:
            # Source session reset.
            self._flush_geophone()
            self.last_adc_total = int(
                total
            )
            return

        new_count = (
            int(
                total
            )
            - int(
                self.last_adc_total
            )
        )

        if new_count <= 0:
            return

        request_count = min(
            new_count,
            MAX_NEW_ADC_REQUEST,
        )

        if request_count < new_count:
            self.recorder_lag_samples += (
                new_count
                - request_count
            )

        adc = (
            shared.read_adc_latest_numpy(
                request_count
            )
        )

        actual = len(
            adc.ch0
        )

        if actual <= 0:
            self.last_adc_total = int(
                total
            )
            return

        if actual < request_count:
            self.recorder_lag_samples += (
                request_count
                - actual
            )

        self.raw_adc_samples += int(
            actual
        )

        stream_info = (
            shared.read_adc_stream_info()
        )

        current_rate = float(
            stream_info.effective_sample_rate_hz
        )

        if (
            int(stream_info.adc_session_id)
            != int(self.config.adc_session_id)
            or not math.isclose(
                current_rate,
                self.config.output_geophone_rate_hz,
                rel_tol=1.0e-9,
                abs_tol=1.0e-9,
            )
        ):
            self._flush_geophone()
            raise RuntimeError(
                "ADC stream configuration changed during recording. "
                "Stop and start a new MiniSEED session so one file never mixes "
                "different sample rates or ADC sessions."
            )

        signals = np.vstack(
            (
                np.asarray(adc.ch0, dtype=np.int32),
                np.asarray(adc.ch1, dtype=np.int32),
                np.asarray(adc.ch2, dtype=np.int32),
            )
        )

        timestamps_ns = np.asarray(
            adc.timestamp_ns,
            dtype=np.int64,
        )

        expected_interval_ns = int(
            round(
                1_000_000_000.0
                / self.config.output_geophone_rate_hz
            )
        )

        if len(timestamps_ns) > 1:
            delta_ns = np.diff(timestamps_ns)
            break_indexes = (
                np.nonzero(
                    (delta_ns <= 0)
                    | (
                        np.abs(
                            delta_ns - expected_interval_ns
                        )
                        > int(0.35 * expected_interval_ns)
                    )
                )[0]
                + 1
            ).tolist()
        else:
            break_indexes = []

        bounds = [0] + break_indexes + [len(timestamps_ns)]

        for segment_index in range(len(bounds) - 1):
            start = bounds[segment_index]
            end = bounds[segment_index + 1]

            if end <= start:
                continue

            segment_timestamps = timestamps_ns[start:end]
            segment_signals = signals[:, start:end]

            if segment_index > 0:
                self._flush_geophone()

            if self.geo_time_buffer:
                previous_timestamp = int(
                    self.geo_time_buffer[-1][-1]
                )
                delta_from_buffer = (
                    int(segment_timestamps[0])
                    - previous_timestamp
                )

                if (
                    delta_from_buffer <= 0
                    or abs(
                        delta_from_buffer
                        - expected_interval_ns
                    )
                    > int(0.35 * expected_interval_ns)
                ):
                    self._flush_geophone()

            self._append_geo_segment(
                segment_signals,
                segment_timestamps,
            )

        self.last_adc_total = int(
            total
        )

        flush_samples = max(
            1,
            int(
                round(
                    GEOPHONE_FLUSH_SECONDS
                    * self.config.output_geophone_rate_hz
                )
            ),
        )

        if (
            self._geo_buffer_count()
            >= flush_samples
        ):
            self._flush_geophone()

    # ------------------------------------------------------------------ IMU

    def _flush_imu(
        self,
    ):
        count = len(
            self.imu_time_buffer
        )

        if count <= 0:
            return

        start_ns = int(
            self.imu_time_buffer[
                0
            ]
        )

        roll = np.asarray(
            self.imu_buffer[
                0
            ],
            dtype=np.float32,
        )
        pitch = np.asarray(
            self.imu_buffer[
                1
            ],
            dtype=np.float32,
        )
        yaw = np.asarray(
            self.imu_buffer[
                2
            ],
            dtype=np.float32,
        )

        stream = Stream(
            traces=[
                make_trace(
                    roll,
                    config=self.config,
                    channel=(
                        self.config.roll_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.imu_rate_hz
                    ),
                ),
                make_trace(
                    pitch,
                    config=self.config,
                    channel=(
                        self.config.pitch_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.imu_rate_hz
                    ),
                ),
                make_trace(
                    yaw,
                    config=self.config,
                    channel=(
                        self.config.yaw_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.imu_rate_hz
                    ),
                ),
            ]
        )

        self.bytes_written += (
            append_miniseed_stream(
                self.imu_file,
                stream,
                IMU_MSEED_RECLEN,
                encoding="FLOAT32",
            )
        )

        self.imu_samples += int(
            count
        )

        self.imu_buffer = [
            [],
            [],
            [],
        ]
        self.imu_time_buffer = []

    def _record_imu(
        self,
        shared,
    ):
        telemetry = (
            shared.read_telemetry()
        )

        timestamp_ns = int(
            telemetry.timestamp_ns
        )

        if (
            timestamp_ns <= 0
            or timestamp_ns
            == self.last_telemetry_timestamp_ns
        ):
            return

        expected_interval_ns = int(
            round(
                1_000_000_000.0
                / self.config.imu_rate_hz
            )
        )

        if (
            self.imu_time_buffer
        ):
            delta_ns = (
                timestamp_ns
                - int(
                    self.imu_time_buffer[
                        -1
                    ]
                )
            )

            # Preserve gaps / rate changes as separate MiniSEED traces rather
            # than pretending they are uniformly sampled.
            if (
                delta_ns <= 0
                or abs(
                    delta_ns
                    - expected_interval_ns
                )
                > int(
                    0.35
                    * expected_interval_ns
                )
            ):
                self._flush_imu()

        self.last_telemetry_timestamp_ns = (
            timestamp_ns
        )

        self.imu_time_buffer.append(
            timestamp_ns
        )

        self.imu_buffer[
            0
        ].append(
            float(
                telemetry.roll
            )
        )
        self.imu_buffer[
            1
        ].append(
            float(
                telemetry.pitch
            )
        )
        self.imu_buffer[
            2
        ].append(
            float(
                telemetry.yaw
            )
        )

        flush_samples = max(
            1,
            int(
                round(
                    IMU_FLUSH_SECONDS
                    * self.config.imu_rate_hz
                )
            ),
        )

        if (
            len(
                self.imu_time_buffer
            )
            >= flush_samples
        ):
            self._flush_imu()

    # ------------------------------------------------------------------ status

    def _emit_stats(
        self,
    ):
        elapsed_s = max(
            0.0,
            (
                time.time_ns()
                - self.session_start_ns
            )
            / 1_000_000_000.0,
        )

        self.stats_changed.emit(
            {
                "elapsed_s": (
                    elapsed_s
                ),
                "raw_adc_samples": (
                    self.raw_adc_samples
                ),
                "decimated_samples": (
                    self.decimated_samples
                ),
                "imu_samples": (
                    self.imu_samples
                    + len(
                        self.imu_time_buffer
                    )
                ),
                "bytes_written": (
                    self.bytes_written
                ),
                "recorder_lag_samples": (
                    self.recorder_lag_samples
                ),
                "session_folder": str(
                    self.session_folder
                    or ""
                ),
            }
        )

    # ------------------------------------------------------------------ thread

    def run(
        self,
    ):
        shared = None

        try:
            shared = OBSSharedData()

            # First segment is a manual START in the current PC UTC hour.
            self._activate_segment(
                shared,
                now_utc=self._pc_utc_now(),
                rotation_reason="manual_start",
            )

            usbl = shared.read_usbl()
            telemetry = shared.read_telemetry()

            # Do not backfill old samples that existed before the operator pressed
            # START. Rotation later keeps these cursors continuous.
            if self.config.record_geophone:
                self.last_adc_total = int(
                    shared.adc_total_samples()
                )

            self.status_changed.emit(
                f"Recording to {self.session_folder} • UTC hourly rotation"
            )

            last_stats_emit = 0.0

            while not self._stop_event.is_set():
                # Rotate on the actual PC UTC hour boundary, not after an arbitrary
                # elapsed 3600 seconds. If the PC sleeps across hours, only the
                # current hour receives a new segment; empty missed hours are not
                # fabricated.
                self._rotate_if_utc_hour_changed(
                    shared
                )

                if self.config.record_geophone:
                    self._record_new_adc(
                        shared,
                    )

                if self.config.log_usbl:
                    # v24: standalone USBL operational state is also fixed 1 Hz.
                    self._record_usbl(
                        shared
                    )

                # v24: combined operational state log remains fixed 1 Hz PC UTC.
                self._record_position_attitude(
                    shared
                )

                now = time.perf_counter()

                if now - last_stats_emit >= 0.25:
                    self._emit_stats()
                    last_stats_emit = now

                self.msleep(
                    WORKER_POLL_MS
                )

            self.status_changed.emit(
                "Finalizing current UTC-hour MiniSEED segment..."
            )

            self._finalize_current_segment(
                shared,
                end_reason="manual_stop",
            )
            self._emit_stats()

            finished_folder = (
                self.last_completed_folder
                or self.session_folder
            )
            self.recording_finished.emit(
                str(finished_folder or "")
            )

        except Exception as exc:
            # Best-effort flush of the current segment so an unrelated runtime
            # error does not unnecessarily discard already buffered samples.
            if shared is not None and self.session_folder is not None:
                try:
                    self._finalize_current_segment(
                        shared,
                        end_reason="recording_error",
                    )
                except Exception:
                    pass

            self.recording_error.emit(
                str(exc)
            )

        finally:
            self._close_usbl_csv()

            if shared is not None:
                try:
                    shared.close()
                except Exception:
                    pass


# =============================================================================
# OBS onboard USB logging command worker
# =============================================================================


class OBSUSBLoggingMonitorThread(OBSIPCClient):
    """Compatibility wrapper for centralized OBS command/status access.

    This object opens NO TCP socket to the OBS. Commands go through obs_ipc.py to
    OBS Setting, while STRG0/TIME1 status is polled from shared_data.
    """

    strg0_received = Signal(str, str)
    strg0_invalid = Signal(str, str)
    command_sent = Signal(int, str, str)
    command_error = Signal(int, str)
    time1_received = Signal(object, str)
    time1_invalid = Signal(str, str)
    time1_command_sent = Signal(str, str, object)
    time1_command_error = Signal(str)
    monitor_error = Signal(str)

    def __init__(self, _host: str = "", _port: int = 0, parent=None):
        super().__init__(source="miniseed", parent=parent)
        self.shared = OBSSharedData()
        self._pending: dict[str, tuple[str, object]] = {}
        self._last_usb_timestamp_ns = -1
        self._last_time_timestamp_ns = -1
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(100)
        self.poll_timer.timeout.connect(self._poll_shared_status)
        self.response_received.connect(self._handle_response)
        self.error_occurred.connect(self.monitor_error.emit)

    def start(self) -> None:
        super().start()
        self.poll_timer.start()
        self._poll_shared_status()

    def stop(self) -> None:
        self.poll_timer.stop()
        super().stop()
        try:
            self.shared.close()
        except Exception:
            pass

    def send_command(self, command_code: int) -> None:
        command_code = int(command_code)
        if command_code not in OBS_USB_LOG_ALLOWED_CODES:
            raise ValueError(
                f"Unsupported OBS USB logging command {command_code}."
            )
        request_id = self.send_rmcmd(command_code)
        self._pending[request_id] = ("usb", command_code)

    def send_time_update(
        self,
        reference_dt: Optional[datetime] = None,
        source: str = TIME_SOURCE_PC,
    ) -> None:
        source = str(source or TIME_SOURCE_PC)
        if source == TIME_SOURCE_PC or reference_dt is None:
            request_id = self.send_time_sync()
            self._pending[request_id] = ("time", {"source": TIME_SOURCE_PC})
            return

        # Newer brokers may expose one of these explicit-time APIs. Keep the
        # existing PC-only broker fully compatible while refusing to silently
        # substitute PC time when the operator selected GNSS.
        for method_name in (
            "send_time_sync_at",
            "send_time_sync_datetime",
            "send_time1_datetime",
        ):
            method = getattr(self, method_name, None)
            if callable(method):
                request_id = method(reference_dt)
                self._pending[request_id] = ("time", {"source": source})
                return

        try:
            request_id = self.send_time_sync(reference_dt=reference_dt, source=source)
        except TypeError as exc:
            raise RuntimeError(
                "GNSS TIME selected, but this obs_ipc.py supports only "
                "TIME_SYNC_FROM_PC. Update the centralized command broker to "
                "accept an explicit TIME1 reference datetime; PC TIME remains available."
            ) from exc

        self._pending[request_id] = ("time", {"source": source})

    def _handle_response(self, response: object) -> None:
        message = dict(response or {})
        response_type = str(message.get("type") or "")
        request_id = str(message.get("request_id") or "")

        if response_type == "sent":
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            kind, payload = pending
            sentence = str(message.get("sentence") or "")
            endpoint = str(message.get("endpoint") or "OBS Setting -> OBS TCP 54300")
            if kind == "usb":
                self.command_sent.emit(int(payload), sentence, endpoint)
            elif kind == "time":
                self.time1_command_sent.emit(
                    sentence,
                    endpoint,
                    dict(message.get("fields") or {}),
                )

        elif response_type == "error":
            pending = self._pending.pop(request_id, None)
            error = str(message.get("message") or "OBS Setting IPC command error")
            if pending is None:
                self.monitor_error.emit(error)
                return
            kind, payload = pending
            if kind == "usb":
                self.command_error.emit(int(payload), error)
            elif kind == "time":
                self.time1_command_error.emit(error)

    def _poll_shared_status(self) -> None:
        try:
            usb = self.shared.read_usb_logger_status()
            if int(usb.timestamp_ns) > 0 and int(usb.timestamp_ns) != self._last_usb_timestamp_ns:
                self._last_usb_timestamp_ns = int(usb.timestamp_ns)
                if usb.last_message or usb.last_raw_sentence:
                    self.strg0_received.emit(
                        str(usb.last_message),
                        str(usb.last_raw_sentence),
                    )

            device_time = self.shared.read_device_time()
            ts = int(device_time.timestamp_ns)
            if ts > 0 and ts != self._last_time_timestamp_ns:
                self._last_time_timestamp_ns = ts
                fields = {
                    "valid": True,
                    "milliseconds": int(device_time.milliseconds),
                    "year": int(device_time.year),
                    "month": int(device_time.month),
                    "week": int(device_time.week),
                    "date": int(device_time.date),
                    "hours": int(device_time.hours),
                    "minutes": int(device_time.minutes),
                    "seconds": int(device_time.seconds),
                    "counter": 0,
                }
                self.time1_received.emit(fields, "TIME1 via shared_data_v10")

        except Exception as exc:
            self.monitor_error.emit(str(exc).strip() or exc.__class__.__name__)


# =============================================================================
# Main window
# =============================================================================


class MiniSeedRecordingWindow(QMainWindow):

    def __init__(
        self,
    ):
        super().__init__()

        self.shared = (
            OBSSharedData()
        )

        self.worker = None
        self.recording = False
        self._close_after_recording = False
        self._close_cleanup_done = False
        self._closing_worker = None

        # Persistent command-TCP monitor for OBS onboard USB logging.
        self.usb_log_monitor_thread = None
        self.obs_usb_monitor_connected = False
        self.obs_usb_media_present = None
        self.obs_usb_device_present = None
        self.obs_usb_logger_active = None
        self.obs_usb_current_file = ""
        self.obs_usb_last_closed_file = ""
        self.obs_usb_last_message = ""
        self.obs_usb_last_sentence = ""
        self.obs_usb_last_status_time = 0.0
        self.obs_usb_pending_command = None
        self.obs_usb_pending_since = 0.0

        # v28 reconnect / rotation recovery state.  Hardware STRG0 is the
        # authority; a new GUI process never infers STOPPED from local state.
        self.obs_usb_recovering = True
        self.obs_usb_close_grace_filename = ""

        # PC/OBS time synchronization state.
        self.obs_time_last_fields = None
        self.obs_time_last_sentence = ""
        self.obs_time_last_rx_monotonic = 0.0
        self.obs_time_pending = False
        self.obs_time_pending_since = 0.0
        self.obs_time_last_sent_fields = None
        self.obs_time_pending_source = TIME_SOURCE_PC
        self.gnss_time_last_info = None
        self.gnss_time_last_age_s = float("inf")

        # v13 display / automatic clock-discipline state.
        self.obs_time_estimated_delta_s: Optional[float] = None
        self.obs_time_effective_sample_age_s = float("inf")
        self.obs_time_auto_sync_last_request_monotonic = 0.0

        self.setWindowTitle(
            f"{APP_TITLE} v{APP_VERSION} - {SYSTEM_TITLE}"
        )

        icon = application_icon()

        if not icon.isNull():
            self.setWindowIcon(
                icon
            )

        # Keep only a conservative minimum here. The actual startup geometry is
        # calculated from the monitor's available work area after the UI has been
        # constructed, so high-DPI / taskbar layouts do not clip controls.
        self.setMinimumSize(
            760,
            520,
        )
        self._startup_maximized = False

        self._build_ui()
        self._apply_style()
        self._apply_initial_window_geometry()

        # v28: firmware rotation closes the old BIN then opens the next one.
        # Delay a non-commanded CLOSED verdict briefly so the normal OPENED
        # rotation event can arrive without flashing a false STOPPED state.
        self.obs_usb_close_grace_timer = QTimer(self)
        self.obs_usb_close_grace_timer.setSingleShot(True)
        self.obs_usb_close_grace_timer.setInterval(
            int(round(OBS_USB_ROTATION_GRACE_S * 1000.0))
        )
        self.obs_usb_close_grace_timer.timeout.connect(
            self._finalize_obs_usb_close_grace
        )

        self.position_timer = QTimer(
            self
        )
        self.position_timer.timeout.connect(
            self.refresh_live_metadata
        )
        self.position_timer.start(
            500
        )

        self.clock_timer = QTimer(
            self
        )
        self.clock_timer.timeout.connect(
            self.refresh_pc_clock
        )
        self.clock_timer.start(
            100
        )

        self.refresh_pc_clock()
        self.refresh_live_metadata()
        self.update_decimation_info()
        self._start_obs_usb_monitor()

        if not OBSPY_AVAILABLE:
            self.start_button.setEnabled(
                False
            )

            self.record_state.setText(
                "OBSPY MISSING"
            )

            self.status_label.setText(
                "Install ObsPy: pip install obspy\n"
                f"{OBSPY_ERROR}"
            )

    # ------------------------------------------------------------------ window geometry

    def _apply_initial_window_geometry(
        self,
    ) -> None:
        """Choose a useful initial size from the active monitor work area.

        The recorder has two information-dense columns. A fixed 1280-pixel
        startup width leaves both columns unnecessarily cramped on a normal
        1920x1080 workstation even though much more desktop area is available.

        Version 8 keeps the V5 card-style sections and sizes the window from the
        monitor available work area. Independent left/right scroll areas preserve
        each card's natural minimum height so labels and controls are scrolled
        instead of compressed into each other. Qt reports logical pixels, so
        Windows DPI scaling is already accounted for.
        """
        app = QApplication.instance()
        screen = self.screen()
        if screen is None and app is not None:
            screen = app.primaryScreen()

        if screen is None:
            self.resize(1500, 930)
            return

        available = screen.availableGeometry()

        # Target almost the full usable desktop without blindly maximizing.
        # This gives the cards enough width while leaving a small visual margin.
        hint = self.sizeHint()
        desired_width = max(
            1380,
            int(hint.width()) + 24,
        )
        desired_height = max(
            820,
            int(hint.height()) + 24,
        )

        max_width = max(
            760,
            int(available.width() * 0.96),
        )
        max_height = max(
            520,
            int(available.height() * 0.94),
        )

        target_width = min(desired_width, max_width)
        target_height = min(desired_height, max_height)

        # Only truly small work areas start maximized. At ordinary laptop /
        # desktop sizes the application opens centered and uses scrolling when
        # the full card stack does not fit vertically.
        self._startup_maximized = bool(
            available.width() < 1280
            or available.height() < 700
        )

        self.setMinimumSize(
            min(760, target_width),
            min(520, target_height),
        )

        self.resize(target_width, target_height)

        x = (
            available.x()
            + max(0, (available.width() - target_width) // 2)
        )
        y = (
            available.y()
            + max(0, (available.height() - target_height) // 2)
        )
        self.move(x, y)

    # ------------------------------------------------------------------ UI

    def _build_ui(
        self,
    ):
        central = QWidget()
        central.setObjectName(
            "centralWidget"
        )

        self.setCentralWidget(
            central
        )

        root = QVBoxLayout(
            central
        )
        root.setContentsMargins(
            12, 10, 12, 10
        )
        root.setSpacing(
            8
        )

        # Header.
        header = QHBoxLayout()

        title_box = QVBoxLayout()
        title_box.setSpacing(
            1
        )

        title = QLabel(
            "MINISEED RECORDING"
        )
        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            "PC MiniSEED • Geophone N/E/Z • IMU • USBL • OBS Onboard USB Control"
        )
        subtitle.setObjectName(
            "subtitleLabel"
        )

        title_box.addWidget(
            title
        )
        title_box.addWidget(
            subtitle
        )

        header.addLayout(
            title_box,
            1,
        )

        self.record_state = QLabel(
            "STOPPED"
        )
        self.record_state.setObjectName(
            "stateStopped"
        )
        self.record_state.setAlignment(
            Qt.AlignCenter
        )
        self.record_state.setMinimumWidth(
            140
        )

        header.addWidget(
            self.record_state
        )

        root.addLayout(
            header
        )

        # Status.
        status = QFrame()
        status.setObjectName(
            "statusFrame"
        )

        sl = QHBoxLayout(
            status
        )
        sl.setContentsMargins(
            10, 6, 10, 6
        )

        self.status_label = QLabel(
            "Ready"
        )
        self.status_label.setObjectName(
            "statusLabel"
        )
        self.status_label.setWordWrap(
            True
        )

        self.duration_label = QLabel(
            "00:00:00"
        )
        self.duration_label.setObjectName(
            "durationLabel"
        )

        sl.addWidget(
            self.status_label,
            1,
        )
        sl.addWidget(
            self.duration_label
        )

        root.addWidget(
            status
        )

        splitter = QSplitter(
            Qt.Horizontal
        )

        splitter.setChildrenCollapsible(
            False
        )

        # Each side is scrollable as a safety net for smaller displays. On a
        # normal/maximized desktop the scrollbars remain hidden, while on a short
        # screen the child layouts keep their natural height instead of being
        # compressed until labels/buttons overlap.
        settings_scroll = QScrollArea()
        settings_scroll.setObjectName(
            "panelScroll"
        )
        settings_scroll.viewport().setObjectName(
            "panelViewport"
        )
        settings_scroll.setWidgetResizable(
            True
        )
        settings_scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )
        settings_scroll.setMinimumWidth(
            620
        )
        settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        settings_panel = self._build_settings_panel()
        settings_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        settings_panel.setMinimumHeight(
            settings_panel.sizeHint().height()
        )
        settings_scroll.setWidget(
            settings_panel
        )

        live_scroll = QScrollArea()
        live_scroll.setObjectName(
            "panelScroll"
        )
        live_scroll.viewport().setObjectName(
            "panelViewport"
        )
        live_scroll.setWidgetResizable(
            True
        )
        live_scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )
        live_scroll.setMinimumWidth(
            470
        )
        live_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        live_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        live_panel = self._build_live_panel()
        live_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        live_panel.setMinimumHeight(
            live_panel.sizeHint().height()
        )
        live_scroll.setWidget(
            live_panel
        )

        splitter.addWidget(
            settings_scroll
        )

        splitter.addWidget(
            live_scroll
        )

        splitter.setStretchFactor(
            0,
            3
        )
        splitter.setStretchFactor(
            1,
            2
        )

        splitter.setSizes(
            [
                820,
                600,
            ]
        )

        root.addWidget(
            splitter,
            1,
        )

        # Start / stop.
        buttons = QHBoxLayout()

        self.start_button = QPushButton(
            "START PC MINISEED"
        )
        self.start_button.setObjectName(
            "startButton"
        )
        self.start_button.setMinimumHeight(
            46
        )
        self.start_button.clicked.connect(
            self.start_recording
        )

        self.stop_button = QPushButton(
            "STOP PC MINISEED"
        )
        self.stop_button.setObjectName(
            "stopButton"
        )
        self.stop_button.setMinimumHeight(
            46
        )
        self.stop_button.setEnabled(
            False
        )
        self.stop_button.clicked.connect(
            self.stop_recording
        )

        buttons.addWidget(
            self.start_button,
            1,
        )
        buttons.addWidget(
            self.stop_button,
            1,
        )

        root.addLayout(
            buttons
        )

    # ------------------------------------------------------------------ settings panel

    def _build_settings_panel(
        self,
    ):
        panel = QFrame()
        panel.setObjectName(
            "settingsPanel"
        )
        layout = QVBoxLayout(
            panel
        )
        layout.setContentsMargins(
            0, 0, 8, 0
        )
        layout.setSpacing(
            8
        )

        # Folder --------------------------------------------------------
        folder_group = QGroupBox(
            "Recording Folder"
        )
        folder_group.setObjectName(
            "controlGroup"
        )

        fg = QVBoxLayout(
            folder_group
        )
        fg.setContentsMargins(
            10, 14, 10, 10
        )
        fg.setSpacing(
            6
        )

        self.folder_edit = QLineEdit(
            str(
                default_record_folder()
            )
        )
        self.folder_edit.setReadOnly(
            True
        )

        choose_folder = QPushButton(
            "Choose Folder"
        )
        choose_folder.setObjectName(
            "secondaryButton"
        )
        choose_folder.clicked.connect(
            self.choose_folder
        )

        folder_note = QLabel(
            "UTC layout: YYYY/YYYYMM/YYYYMMDD/YYYYMMDD_HH/. "
            "Files use suffix YYYYMMDD_HH_NN; a manual restart in the same UTC hour "
            "increments NN inside the same folder. A UTC hour change switches to "
            "the corresponding new hourly folder."
        )
        folder_note.setWordWrap(
            True
        )
        folder_note.setObjectName(
            "hintText"
        )

        fg.addWidget(
            self.folder_edit
        )
        fg.addWidget(
            choose_folder
        )
        fg.addWidget(
            folder_note
        )

        layout.addWidget(
            folder_group
        )

        # Station metadata ---------------------------------------------
        station_group = QGroupBox(
            "MiniSEED Station / Channel Codes"
        )
        station_group.setObjectName(
            "controlGroup"
        )

        sg = QGridLayout(
            station_group
        )
        sg.setContentsMargins(
            10, 14, 10, 10
        )
        sg.setHorizontalSpacing(
            8
        )
        sg.setVerticalSpacing(
            5
        )

        self.network_edit = QLineEdit(
            DEFAULT_NETWORK
        )
        self.station_edit = QLineEdit(
            DEFAULT_STATION
        )
        self.location_edit = QLineEdit(
            DEFAULT_LOCATION
        )

        self.geo_n_edit = QLineEdit(
            DEFAULT_GEO_N_CHANNEL
        )
        self.geo_e_edit = QLineEdit(
            DEFAULT_GEO_E_CHANNEL
        )
        self.geo_z_edit = QLineEdit(
            DEFAULT_GEO_Z_CHANNEL
        )
        for widget in (self.geo_n_edit, self.geo_e_edit, self.geo_z_edit):
            widget.setReadOnly(True)
            widget.setToolTip(
                "Fixed physical MiniSEED channel code; CH0=N/EHN, CH1=E/EHE, CH2=Z/EHZ."
            )

        self.roll_edit = QLineEdit(
            DEFAULT_ROLL_CHANNEL
        )
        self.pitch_edit = QLineEdit(
            DEFAULT_PITCH_CHANNEL
        )
        self.yaw_edit = QLineEdit(
            DEFAULT_YAW_CHANNEL
        )

        fields = (
            (
                "Network",
                self.network_edit,
            ),
            (
                "Station",
                self.station_edit,
            ),
            (
                "Location",
                self.location_edit,
            ),
            (
                "Geophone N",
                self.geo_n_edit,
            ),
            (
                "Geophone E",
                self.geo_e_edit,
            ),
            (
                "Geophone Z",
                self.geo_z_edit,
            ),
        )

        for row, (
            name,
            widget,
        ) in enumerate(
            fields
        ):
            sg.addWidget(
                QLabel(
                    name
                ),
                row,
                0,
            )
            sg.addWidget(
                widget,
                row,
                1,
            )

        code_note = QLabel(
            "Network / Station / Location remain editable. Geophone channel codes are "
            "fixed: N=EHN, E=EHE, Z=EHZ."
        )
        code_note.setObjectName(
            "hintText"
        )
        code_note.setWordWrap(
            True
        )

        sg.addWidget(
            code_note,
            len(
                fields
            ),
            0,
            1,
            2,
        )

        layout.addWidget(
            station_group
        )

        # Rates / shared-stream configuration ---------------------------
        rate_group = QGroupBox(
            "Sampling / Shared ADC Stream"
        )
        rate_group.setObjectName(
            "controlGroup"
        )

        rg = QGridLayout(
            rate_group
        )
        rg.setContentsMargins(
            10, 14, 10, 10
        )
        rg.setHorizontalSpacing(
            8
        )
        rg.setVerticalSpacing(
            6
        )

        self.raw_rate_label = QLabel(
            "-- Hz"
        )
        self.raw_rate_label.setObjectName(
            "fixedValue"
        )

        self.shared_decimation_label = QLabel(
            "--"
        )
        self.shared_decimation_label.setObjectName(
            "fixedValue"
        )

        self.output_rate_label = QLabel(
            "-- Hz"
        )
        self.output_rate_label.setObjectName(
            "fixedValue"
        )

        self.rate_health_label = QLabel(
            "WARMING UP"
        )
        self.rate_health_label.setObjectName(
            "fixedValue"
        )

        self.anti_alias_label = QLabel(
            "Recorder uses shared_data_v10 output directly. MiniSEED Fs = shared effective Fs. "
            "PC measured ADC rate is diagnostic only and never changes file Fs dynamically. "
            "No second decimation, resampling or anti-alias filter is applied here."
        )
        self.anti_alias_label.setObjectName(
            "hintText"
        )
        self.anti_alias_label.setWordWrap(
            True
        )

        self.imu_rate_spin = (
            ReliableDoubleSpinBox()
        )
        self.imu_rate_spin.setRange(
            0.1,
            1000.0
        )
        self.imu_rate_spin.setDecimals(
            3
        )
        self.imu_rate_spin.setValue(
            DEFAULT_IMU_RATE_HZ
        )
        self.imu_rate_spin.setSuffix(
            " Hz"
        )

        rg.addWidget(QLabel("Raw ADC Expected Rate"), 0, 0)
        rg.addWidget(self.raw_rate_label, 0, 1)
        rg.addWidget(QLabel("Shared Decimation"), 1, 0)
        rg.addWidget(self.shared_decimation_label, 1, 1)
        rg.addWidget(QLabel("Recorded Geo Rate"), 2, 0)
        rg.addWidget(self.output_rate_label, 2, 1)
        rg.addWidget(QLabel("ADC Rate Check"), 3, 0)
        rg.addWidget(self.rate_health_label, 3, 1)
        attitude_note = QLabel(
            "Attitude: CSV only, fixed 1 Hz PC-UTC snapshot (no IMU MiniSEED)"
        )
        attitude_note.setWordWrap(True)
        rg.addWidget(attitude_note, 4, 0, 1, 2)
        rg.addWidget(self.anti_alias_label, 5, 0, 1, 2)

        layout.addWidget(
            rate_group
        )

        # Include channels ---------------------------------------------
        include_group = QGroupBox(
            "Recorded Data"
        )
        include_group.setObjectName(
            "controlGroup"
        )

        ig = QVBoxLayout(
            include_group
        )
        ig.setContentsMargins(
            10, 14, 10, 10
        )

        self.record_geophone_check = QCheckBox(
            "Geophone N / E / Z"
        )
        self.record_geophone_check.setChecked(
            True
        )

        # Compatibility-only state. Version 19 does not record IMU MiniSEED.
        self.record_imu_check = QCheckBox(
            "IMU MiniSEED disabled"
        )
        self.record_imu_check.setChecked(False)
        self.record_imu_check.setEnabled(False)

        self.log_usbl_check = QCheckBox(
            "USBL coordinate metadata / history"
        )
        self.log_usbl_check.setChecked(
            True
        )

        ig.addWidget(
            self.record_geophone_check
        )
        ig.addWidget(
            self.log_usbl_check
        )

        layout.addWidget(
            include_group
        )

        layout.addStretch(
            1
        )

        self.settings_panel = (
            panel
        )

        return panel

    # ------------------------------------------------------------------ live panel

    def _build_live_panel(
        self,
    ):
        panel = QFrame()
        panel.setObjectName(
            "livePanel"
        )
        layout = QVBoxLayout(
            panel
        )
        layout.setContentsMargins(
            8, 0, 0, 0
        )
        layout.setSpacing(
            8
        )

        # OBS onboard USB binary logger -------------------------------
        onboard_group = QGroupBox(
            "OBS Onboard USB Binary Logging"
        )
        onboard_group.setObjectName(
            "controlGroup"
        )

        og = QVBoxLayout(
            onboard_group
        )
        og.setContentsMargins(
            10, 14, 10, 10
        )
        og.setSpacing(
            6
        )

        self.obs_usb_endpoint_label = QLabel(
            "Centralized command: OBS Setting IPC -> single TCP 54300"
        )
        self.obs_usb_endpoint_label.setObjectName(
            "monoValue"
        )

        self.obs_usb_connection_label = QLabel(
            "MONITOR: CONNECTING..."
        )
        self.obs_usb_connection_label.setObjectName(
            "onboardConnection"
        )

        onboard_buttons = QHBoxLayout()

        self.obs_usb_start_button = QPushButton(
            "START OBS USB LOG"
        )
        self.obs_usb_start_button.setObjectName(
            "onboardStartButton"
        )
        self.obs_usb_start_button.clicked.connect(
            lambda: self.send_obs_usb_logging_command(
                OBS_USB_LOG_START
            )
        )

        self.obs_usb_stop_button = QPushButton(
            "STOP OBS USB LOG"
        )
        self.obs_usb_stop_button.setObjectName(
            "onboardStopButton"
        )
        self.obs_usb_stop_button.clicked.connect(
            lambda: self.send_obs_usb_logging_command(
                OBS_USB_LOG_STOP
            )
        )

        onboard_buttons.addWidget(
            self.obs_usb_start_button,
            1,
        )
        onboard_buttons.addWidget(
            self.obs_usb_stop_button,
            1,
        )

        self.obs_usb_status_label = QLabel(
            "ONBOARD USB LOGGER: UNKNOWN / waiting for STRG0"
        )
        self.obs_usb_status_label.setObjectName(
            "onboardUnknown"
        )
        self.obs_usb_status_label.setWordWrap(
            True
        )

        self.obs_usb_device_label = QLabel(
            "Device: UNKNOWN | Media: UNKNOWN | Logger: UNKNOWN | File: --"
        )
        self.obs_usb_device_label.setObjectName(
            "monoValue"
        )
        self.obs_usb_device_label.setWordWrap(
            True
        )

        self.obs_usb_strg0_label = QLabel(
            "STRG0: waiting for status sentence"
        )
        self.obs_usb_strg0_label.setObjectName(
            "onboardSentence"
        )
        self.obs_usb_strg0_label.setWordWrap(
            True
        )

        time_frame = QFrame()
        time_frame.setObjectName(
            "timeSyncFrame"
        )
        tg = QVBoxLayout(
            time_frame
        )
        tg.setContentsMargins(
            8, 8, 8, 8
        )
        tg.setSpacing(
            5
        )

        self.pc_time_label = QLabel(
            "PC LOCAL TIME: --"
        )
        self.pc_time_label.setObjectName(
            "timeValue"
        )

        self.obs_time_label = QLabel(
            "OBS TIME1: waiting for device time"
        )
        self.obs_time_label.setObjectName(
            "timeValue"
        )
        self.obs_time_label.setWordWrap(
            True
        )

        self.obs_time_source_combo = QComboBox()
        self.obs_time_source_combo.addItems([TIME_SOURCE_PC, TIME_SOURCE_GNSS])
        self.obs_time_source_combo.setCurrentText(TIME_SOURCE_PC)
        self.obs_time_source_combo.currentTextChanged.connect(
            self.on_obs_time_source_changed
        )

        time_source_row = QHBoxLayout()
        time_source_row.setSpacing(8)
        time_source_row.addWidget(QLabel("TIME SOURCE"))
        time_source_row.addWidget(self.obs_time_source_combo, 1)

        self.gnss_time_label = QLabel(
            "GNSS UTC TIME: waiting for GNSS NMEA"
        )
        self.gnss_time_label.setObjectName("timeValue")
        self.gnss_time_label.setWordWrap(True)

        self.obs_time_delta_label = QLabel(
            "OBS-reference difference: --"
        )
        self.obs_time_delta_label.setObjectName(
            "timeDelta"
        )

        self.obs_time_update_button = QPushButton(
            "UPDATE OBS TIME"
        )
        self.obs_time_update_button.setObjectName(
            "timeSyncButton"
        )
        self.obs_time_update_button.clicked.connect(
            self.send_obs_time_update
        )

        auto_time_row = QHBoxLayout()
        auto_time_row.setSpacing(8)

        self.obs_time_auto_sync_checkbox = QCheckBox(
            "AUTO KEEP OBS TIME CLOSE TO SELECTED SOURCE"
        )
        self.obs_time_auto_sync_checkbox.setChecked(
            OBS_TIME_AUTO_SYNC_DEFAULT
        )
        self.obs_time_auto_sync_checkbox.setToolTip(
            "When a fresh TIME1 sample is outside the selected threshold, "
            "request a new OBS time update. Requests are rate-limited to one "
            f"every {OBS_TIME_AUTO_SYNC_MIN_INTERVAL_S:.0f} seconds."
        )

        auto_time_row.addWidget(
            self.obs_time_auto_sync_checkbox
        )
        auto_time_row.addStretch(1)

        auto_time_row.addWidget(
            QLabel("Resync when |OBS-reference| >")
        )

        self.obs_time_auto_sync_threshold_spin = ReliableDoubleSpinBox()
        self.obs_time_auto_sync_threshold_spin.setRange(
            OBS_TIME_AUTO_SYNC_MIN_THRESHOLD_S,
            OBS_TIME_AUTO_SYNC_MAX_THRESHOLD_S,
        )
        self.obs_time_auto_sync_threshold_spin.setDecimals(2)
        self.obs_time_auto_sync_threshold_spin.setSingleStep(0.10)
        self.obs_time_auto_sync_threshold_spin.setValue(
            OBS_TIME_AUTO_SYNC_DEFAULT_THRESHOLD_S
        )
        self.obs_time_auto_sync_threshold_spin.setSuffix(" s")
        self.obs_time_auto_sync_threshold_spin.setMaximumWidth(105)
        auto_time_row.addWidget(
            self.obs_time_auto_sync_threshold_spin
        )

        self.obs_time_sync_status_label = QLabel(
            "TIME SYNC: READY when command TCP is connected"
        )
        self.obs_time_sync_status_label.setObjectName(
            "timeSyncIdle"
        )
        self.obs_time_sync_status_label.setWordWrap(
            True
        )

        tg.addLayout(time_source_row)
        tg.addWidget(
            self.pc_time_label
        )
        tg.addWidget(
            self.gnss_time_label
        )
        tg.addWidget(
            self.obs_time_label
        )
        tg.addWidget(
            self.obs_time_delta_label
        )
        tg.addWidget(
            self.obs_time_update_button
        )
        tg.addLayout(
            auto_time_row
        )
        tg.addWidget(
            self.obs_time_sync_status_label
        )

        onboard_note = QLabel(
            "Hardware status comes from checksum-validated $STRG0 messages. "
            "MSDV_FILE_OPENED confirms logging active; MSDV_FILE_CLOSED confirms "
            "the file closed. START sends RMCMD 61 and requires USB media; STOP "
            "sends RMCMD 62. Automatic 60 s file rotation may briefly emit CLOSED "
            "followed immediately by OPENED for the next OBS_YYYYMMDD_HHMMSS.bin. "
            "Closing this GUI does NOT send STOP. If the PC loses power while the "
            "OBS remains powered, this software sends no stop command; the onboard "
            "logger is intended to keep running independently until firmware/USB/"
            "power conditions stop it. After reconnect, v28 shows RECOVERING until "
            "a factual STRG0 FILE_OPENED/CLOSED event restores logger state."
        )
        onboard_note.setObjectName(
            "hintText"
        )
        onboard_note.setWordWrap(
            True
        )

        og.addWidget(
            self.obs_usb_endpoint_label
        )
        og.addWidget(
            self.obs_usb_connection_label
        )
        og.addLayout(
            onboard_buttons
        )
        og.addWidget(
            self.obs_usb_status_label
        )
        og.addWidget(
            self.obs_usb_device_label
        )
        og.addWidget(
            self.obs_usb_strg0_label
        )
        og.addWidget(
            time_frame
        )
        og.addWidget(
            onboard_note
        )

        layout.addWidget(
            onboard_group
        )

        # USBL ---------------------------------------------------------
        usbl_group = QGroupBox(
            "OBS Position — USBL"
        )
        usbl_group.setObjectName(
            "controlGroup"
        )

        ug = QVBoxLayout(
            usbl_group
        )
        ug.setContentsMargins(
            10, 14, 10, 10
        )

        self.usbl_fix_label = QLabel(
            "NO POSITION"
        )
        self.usbl_fix_label.setObjectName(
            "positionState"
        )
        self.usbl_fix_label.setAlignment(
            Qt.AlignCenter
        )

        self.usbl_position_label = QLabel(
            "Latitude  : --\n"
            "Longitude : --\n"
            "Altitude  : --\n"
            "Fix / Sat : --\n"
            "HDOP      : --"
        )
        self.usbl_position_label.setObjectName(
            "monoValue"
        )

        position_note = QLabel(
            "Coordinates are recorded at fixed 1 Hz to the NN-indexed USBL CSV and combined "
            "position/attitude CSV. MiniSEED itself remains waveform data."
        )
        position_note.setObjectName(
            "hintText"
        )
        position_note.setWordWrap(
            True
        )

        ug.addWidget(
            self.usbl_fix_label
        )
        ug.addWidget(
            self.usbl_position_label
        )
        ug.addWidget(
            position_note
        )

        layout.addWidget(
            usbl_group
        )

        # Live source --------------------------------------------------
        source_group = QGroupBox(
            "Live Source"
        )
        source_group.setObjectName(
            "controlGroup"
        )

        src = QGridLayout(
            source_group
        )
        src.setContentsMargins(
            10, 14, 10, 10
        )

        self.adc_total_label = QLabel(
            "--"
        )
        self.adc_total_label.setObjectName(
            "monoValue"
        )

        self.imu_live_label = QLabel(
            "Roll --\nPitch --\nYaw --"
        )
        self.imu_live_label.setObjectName(
            "monoValue"
        )

        self.depth_live_label = QLabel(
            "Depth: -- m"
        )
        self.depth_live_label.setObjectName(
            "monoValue"
        )

        src.addWidget(
            QLabel(
                "ADC Total"
            ),
            0,
            0,
        )
        src.addWidget(
            self.adc_total_label,
            0,
            1,
        )
        src.addWidget(
            QLabel(
                "IMU"
            ),
            1,
            0,
        )
        src.addWidget(
            self.imu_live_label,
            1,
            1,
        )
        src.addWidget(
            QLabel(
                "Depth"
            ),
            2,
            0,
        )
        src.addWidget(
            self.depth_live_label,
            2,
            1,
        )

        layout.addWidget(
            source_group
        )

        # Recording stats ----------------------------------------------
        stats_group = QGroupBox(
            "Recording Statistics"
        )
        stats_group.setObjectName(
            "controlGroup"
        )

        st = QGridLayout(
            stats_group
        )
        st.setContentsMargins(
            10, 14, 10, 10
        )

        self.raw_samples_label = QLabel(
            "0"
        )
        self.decimated_samples_label = QLabel(
            "0"
        )
        self.imu_samples_label = QLabel(
            "0"
        )
        self.file_size_label = QLabel(
            "0 B"
        )
        self.lag_samples_label = QLabel(
            "0"
        )

        for widget in (
            self.raw_samples_label,
            self.decimated_samples_label,
            self.file_size_label,
            self.lag_samples_label,
        ):
            widget.setObjectName(
                "monoValue"
            )

        stats = (
            (
                "Shared ADC Read",
                self.raw_samples_label,
            ),
            (
                "Geo Recorded",
                self.decimated_samples_label,
            ),
            (
                "MiniSEED Size",
                self.file_size_label,
            ),
            (
                "Recorder Lag",
                self.lag_samples_label,
            ),
        )

        for row, (
            name,
            widget,
        ) in enumerate(
            stats
        ):
            st.addWidget(
                QLabel(
                    name
                ),
                row,
                0,
            )
            st.addWidget(
                widget,
                row,
                1,
            )

        layout.addWidget(
            stats_group
        )

        # Format notes -------------------------------------------------
        format_group = QGroupBox(
            "MiniSEED Format"
        )
        format_group.setObjectName(
            "controlGroup"
        )

        fl = QVBoxLayout(
            format_group
        )
        fl.setContentsMargins(
            10, 14, 10, 10
        )

        format_label = QLabel(
            "Geo     : INT32 + STEIM2, one file per N/E/Z per NN session\n"
            "IMU     : no separate MiniSEED file\n"
            "Attitude: obs_position_attitude_YYYYMMDD_HH_NN.csv • fixed 1 Hz UTC\n"
            "Quality : D (data)\n"
            "Geo rec : 4096-byte MiniSEED records\n"
            "Geo flush: 10 s\n"
            "Folder rotation: UTC hourly boundary"
        )
        format_label.setObjectName(
            "monoValue"
        )

        fl.addWidget(
            format_label
        )

        layout.addWidget(
            format_group
        )
        layout.addStretch(
            1
        )

        return panel

    # ------------------------------------------------------------------ validation/config

    @staticmethod
    def _valid_code(
        value: str,
        max_length: int,
        exact_length: Optional[
            int
        ] = None,
    ):
        value = str(
            value
        ).strip()

        if exact_length is not None:
            if len(
                value
            ) != exact_length:
                return False
        elif len(
            value
        ) > max_length:
            return False

        return bool(
            re.fullmatch(
                r"[A-Za-z0-9]*",
                value,
            )
        )

    def build_config(
        self,
    ) -> RecorderConfig:
        network = (
            self.network_edit.text()
            .strip()
            .upper()
        )

        station = (
            self.station_edit.text()
            .strip()
            .upper()
        )

        location = (
            self.location_edit.text()
            .strip()
            .upper()
        )

        # v23: geophone channel codes are authoritative fixed metadata, not UI input.
        channels = [
            DEFAULT_GEO_N_CHANNEL,
            DEFAULT_GEO_E_CHANNEL,
            DEFAULT_GEO_Z_CHANNEL,
            self.roll_edit.text().strip().upper(),
            self.pitch_edit.text().strip().upper(),
            self.yaw_edit.text().strip().upper(),
        ]

        if not self._valid_code(
            network,
            2,
        ):
            raise ValueError(
                "Network code must be "
                "0–2 alphanumeric characters."
            )

        if not self._valid_code(
            station,
            5,
        ) or not station:
            raise ValueError(
                "Station code must be "
                "1–5 alphanumeric characters."
            )

        if not self._valid_code(
            location,
            2,
        ):
            raise ValueError(
                "Location code must be "
                "0–2 alphanumeric characters."
            )

        for channel in channels:
            if not self._valid_code(
                channel,
                3,
                exact_length=3,
            ):
                raise ValueError(
                    "Every MiniSEED channel code "
                    "must be exactly 3 "
                    "alphanumeric characters."
                )

        if len(
            set(
                channels
            )
        ) != len(
            channels
        ):
            raise ValueError(
                "Channel codes must be unique."
            )

        folder = Path(
            self.folder_edit.text()
        )

        folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        stream_info = (
            self.shared.read_adc_stream_info()
        )

        raw_rate_hz = float(
            stream_info.raw_sample_rate_hz
        )
        output_rate_hz = float(
            stream_info.effective_sample_rate_hz
        )
        decimation_samples = int(
            stream_info.decimation_samples
        )

        if not (
            math.isfinite(raw_rate_hz)
            and raw_rate_hz > 0.0
            and math.isfinite(output_rate_hz)
            and output_rate_hz > 0.0
            and decimation_samples > 0
        ):
            raise RuntimeError(
                "Invalid ADC stream timing metadata. Reconnect OBS Setting "
                "before starting MiniSEED recording."
            )

        expected_output_rate_hz = (
            raw_rate_hz / float(decimation_samples)
        )
        if not math.isclose(
            output_rate_hz,
            expected_output_rate_hz,
            rel_tol=1.0e-9,
            abs_tol=1.0e-9,
        ):
            raise RuntimeError(
                "ADC stream rate metadata is inconsistent: "
                f"raw={raw_rate_hz:.9f} Hz, N={decimation_samples}, "
                f"output={output_rate_hz:.9f} Hz."
            )

        return RecorderConfig(
            base_folder=str(
                folder
            ),
            network=network,
            station=station,
            location=location,
            geo_n_channel=DEFAULT_GEO_N_CHANNEL,
            geo_e_channel=DEFAULT_GEO_E_CHANNEL,
            geo_z_channel=DEFAULT_GEO_Z_CHANNEL,
            roll_channel=channels[
                3
            ],
            pitch_channel=channels[
                4
            ],
            yaw_channel=channels[
                5
            ],
            raw_adc_rate_hz=raw_rate_hz,
            shared_decimation_samples=decimation_samples,
            shared_decimation_mode=str(
                stream_info.decimation_mode
            ),
            geophone_rate_hz=output_rate_hz,
            adc_session_id=int(
                stream_info.adc_session_id
            ),
            imu_rate_hz=float(
                self.imu_rate_spin.value()
            ),
            record_geophone=bool(
                self.record_geophone_check.isChecked()
            ),
            record_imu=False,
            log_usbl=bool(
                self.log_usbl_check.isChecked()
            ),
        )

    # ------------------------------------------------------------------ folder/rate

    def choose_folder(
        self,
    ):
        folder = (
            QFileDialog.getExistingDirectory(
                self,
                "Choose MiniSEED Recording Folder",
                self.folder_edit.text(),
            )
        )

        if folder:
            self.folder_edit.setText(
                folder
            )

    def update_decimation_info(
        self,
        *_args,
    ):
        try:
            info = (
                self.shared.read_adc_stream_info()
            )

            self.raw_rate_label.setText(
                f"{info.raw_sample_rate_hz:.3f} Hz"
            )
            self.shared_decimation_label.setText(
                f"N={info.decimation_samples} • {info.decimation_mode} "
                f"• session {info.adc_session_id}"
            )
            self.output_rate_label.setText(
                f"{info.effective_sample_rate_hz:.3f} Hz"
            )
            try:
                rate_diag = self.shared.read_adc_rate_diagnostic()
                if rate_diag.ready:
                    self.rate_health_label.setText(
                        f"{rate_diag.measured_raw_sample_rate_hz:.3f} Hz • "
                        f"{rate_diag.error_ppm:+.0f} ppm • {rate_diag.status}"
                    )
                else:
                    self.rate_health_label.setText("WARMING UP")
            except Exception:
                self.rate_health_label.setText("UNAVAILABLE")
        except Exception as exc:
            self.raw_rate_label.setText("-- Hz")
            self.shared_decimation_label.setText("--")
            self.output_rate_label.setText("-- Hz")
            self.rate_health_label.setText("UNAVAILABLE")
            self.anti_alias_label.setText(
                f"shared_data_v10 stream metadata unavailable: {exc}"
            )

    # ------------------------------------------------------------------ live metadata

    def refresh_live_metadata(
        self,
    ):
        self._check_obs_usb_pending_timeout()
        self._refresh_obs_usb_controls()

        try:
            self.update_decimation_info()

            usbl = (
                self.shared.read_usbl()
            )

            telemetry = (
                self.shared.read_telemetry()
            )

            health = self.shared.read_acquisition_health()
            core_age = health.heartbeat_age_s()
            imu_age = health.source_age_s("ahrs")
            depth_age = health.source_age_s("depth")
            usbl_age = health.source_age_s("usbl")

            total = (
                self.shared.adc_total_samples()
            )

            self.adc_total_label.setText(
                f"{total:,}"
            )

            live_offsets = load_imu_offsets_deg()
            live_roll, live_pitch, live_yaw = apply_imu_offsets_deg(
                telemetry.roll,
                telemetry.pitch,
                telemetry.yaw,
                live_offsets,
            )

            self.imu_live_label.setText(
                f"Roll  {live_roll:+.2f}°\n"
                f"Pitch {live_pitch:+.2f}°\n"
                f"Yaw   {live_yaw:+.2f}°\n"
                f"Offset R {live_offsets[0]:+.1f}° "
                f"P {live_offsets[1]:+.1f}° Y {live_offsets[2]:+.1f}°\n"
                f"IMU age {imu_age:.1f}s • Core {core_age:.1f}s"
            )

            self.depth_live_label.setText(
                f"Depth: {shared_depth_m(telemetry):.2f} m • age {depth_age:.1f}s"
            )

            gga_age = MiniSeedRecordingWorker._position_age_s(
                usbl,
                time.time_ns(),
            )
            effective_usbl_valid = bool(
                valid_position(usbl)
                and usbl_age <= POSITION_ATTITUDE_USBL_FRESH_S
                and gga_age <= POSITION_ATTITUDE_USBL_FRESH_S
            )

            if effective_usbl_valid:
                self.usbl_fix_label.setText(
                    f"VALID USBL FIX • age {max(usbl_age, gga_age):.1f}s"
                )
                self.usbl_fix_label.setObjectName(
                    "positionValid"
                )

                self.usbl_position_label.setText(
                    f"Latitude  : {usbl.latitude:.8f}\n"
                    f"Longitude : {usbl.longitude:.8f}\n"
                    f"Altitude  : {usbl.altitude:.2f} m\n"
                    f"Fix / Sat : {usbl.fix_quality} / "
                    f"{usbl.satellites}\n"
                    f"HDOP      : {usbl.hdop:.2f}"
                )

            elif valid_position(usbl):
                self.usbl_fix_label.setText(
                    f"STALE USBL • effective Fix 0 • age {max(usbl_age, gga_age):.1f}s"
                )
                self.usbl_fix_label.setObjectName(
                    "positionInvalid"
                )

                self.usbl_position_label.setText(
                    f"Last Latitude  : {usbl.latitude:.8f}\n"
                    f"Last Longitude : {usbl.longitude:.8f}\n"
                    f"Last Altitude  : {usbl.altitude:.2f} m\n"
                    f"Fix / Sat      : 0 / {usbl.satellites}\n"
                    f"HDOP           : {usbl.hdop:.2f}"
                )

            else:
                self.usbl_fix_label.setText(
                    "NO VALID USBL POSITION • effective Fix 0"
                )
                self.usbl_fix_label.setObjectName(
                    "positionInvalid"
                )

                self.usbl_position_label.setText(
                    "Latitude  : --\n"
                    "Longitude : --\n"
                    "Altitude  : --\n"
                    "Fix / Sat : 0 / --\n"
                    "HDOP      : --"
                )

            self.usbl_fix_label.style().unpolish(
                self.usbl_fix_label
            )
            self.usbl_fix_label.style().polish(
                self.usbl_fix_label
            )

        except Exception as exc:
            if not self.recording:
                self.status_label.setText(
                    f"Shared RAM error: {exc}"
                )

    # ------------------------------------------------------------------ PC / OBS time

    def _set_obs_time_sync_status(
        self,
        text: str,
        object_name: str,
    ) -> None:
        if not hasattr(self, "obs_time_sync_status_label"):
            return
        self.obs_time_sync_status_label.setText(str(text))
        self.obs_time_sync_status_label.setObjectName(object_name)
        self.obs_time_sync_status_label.style().unpolish(
            self.obs_time_sync_status_label
        )
        self.obs_time_sync_status_label.style().polish(
            self.obs_time_sync_status_label
        )

    def refresh_pc_clock(
        self,
    ) -> None:
        if not hasattr(self, "pc_time_label"):
            return

        now = current_pc_local_time()
        self.pc_time_label.setText(
            "PC LOCAL TIME: "
            f"{now:%Y-%m-%d %H:%M:%S}."
            f"{now.microsecond // 1000:03d} "
            f"{_utc_offset_text(now)}"
        )

        self._refresh_gnss_time_display()
        self._refresh_obs_time_display(now)
        self._check_obs_time_pending_timeout()
        self._maybe_auto_sync_obs_time()
        self._refresh_obs_time_control()

    def _selected_time_source(self) -> str:
        combo = getattr(self, "obs_time_source_combo", None)
        if combo is None:
            return TIME_SOURCE_PC
        return str(combo.currentText() or TIME_SOURCE_PC)

    def _read_gnss_time_reference(self) -> tuple[Optional[datetime], float, str]:
        try:
            if not hasattr(self.shared, "read_gnss_nmea_sentence"):
                return None, float("inf"), "GNSS NMEA API unavailable"
            snapshot = self.shared.read_gnss_nmea_sentence()
            sentence, timestamp_ns = _nmea_sentence_and_timestamp(snapshot)
            if not sentence:
                return None, float("inf"), "waiting for GNSS NMEA"
            age_s = (
                max(0.0, (time.time_ns() - int(timestamp_ns)) / 1_000_000_000.0)
                if int(timestamp_ns) > 0
                else float("inf")
            )
            info = parse_gnss_utc_datetime(sentence)
            if info is None:
                return None, age_s, "latest NMEA has no valid GGA/RMC/ZDA UTC time"
            dt = info["datetime"] + timedelta(seconds=(age_s if math.isfinite(age_s) else 0.0))
            self.gnss_time_last_info = info
            self.gnss_time_last_age_s = age_s
            detail = f"{info['sentence_type']} • {info['date_source']}"
            return dt, age_s, detail
        except Exception as exc:
            return None, float("inf"), str(exc).strip() or exc.__class__.__name__

    def _refresh_gnss_time_display(self) -> None:
        label = getattr(self, "gnss_time_label", None)
        if label is None:
            return
        dt, age_s, detail = self._read_gnss_time_reference()
        if dt is None:
            label.setText(f"GNSS UTC TIME: unavailable • {detail}")
            return
        freshness = "LIVE" if age_s <= GNSS_TIME_MAX_SAMPLE_AGE_S else "STALE"
        label.setText(
            "GNSS UTC TIME: "
            f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d} UTC "
            f"• {freshness} age={age_s:.2f}s • {detail}"
        )

    def _selected_reference_now(self, pc_now: Optional[datetime] = None) -> tuple[Optional[datetime], float, str]:
        source = self._selected_time_source()
        if source == TIME_SOURCE_GNSS:
            dt, age_s, detail = self._read_gnss_time_reference()
            if dt is None or age_s > GNSS_TIME_MAX_SAMPLE_AGE_S:
                return None, age_s, f"GNSS TIME • {detail}"
            return dt.replace(tzinfo=None), age_s, f"GNSS TIME • {detail}"
        if pc_now is None:
            pc_now = current_pc_local_time()
        return pc_now.replace(tzinfo=None), 0.0, "PC TIME"

    def on_obs_time_source_changed(self, _text: str) -> None:
        self.obs_time_auto_sync_last_request_monotonic = 0.0
        self._refresh_gnss_time_display()
        self._refresh_obs_time_display()
        self._refresh_obs_time_control()

    def _shared_device_time_fields(
        self,
    ) -> tuple[Optional[dict], float]:
        """
        Fallback to shared_data_v10 TIME1 data when this monitor has not yet seen a
        TIME1 sentence directly on its own command TCP connection.
        """
        try:
            snapshot = self.shared.read_device_time()
            timestamp_ns = int(snapshot.timestamp_ns)
            if timestamp_ns <= 0:
                return None, 0.0

            fields = {
                "milliseconds": int(snapshot.milliseconds),
                "year": int(snapshot.year),
                "month": int(snapshot.month),
                "week": int(snapshot.week),
                "date": int(snapshot.date),
                "hours": int(snapshot.hours),
                "minutes": int(snapshot.minutes),
                "seconds": int(snapshot.seconds),
                "counter": None,
            }
            age_s = max(
                0.0,
                (time.time_ns() - timestamp_ns) / 1_000_000_000.0,
            )
            return fields, age_s
        except Exception:
            return None, 0.0

    def _refresh_obs_time_display(
        self,
        pc_now: Optional[datetime] = None,
    ) -> None:
        if not hasattr(self, "obs_time_label"):
            return

        if pc_now is None:
            pc_now = current_pc_local_time()

        direct_fields = self.obs_time_last_fields
        direct_age_s = (
            time.monotonic() - self.obs_time_last_rx_monotonic
            if direct_fields is not None
            and self.obs_time_last_rx_monotonic > 0.0
            else float("inf")
        )
        shared_fields, shared_age_s = self._shared_device_time_fields()

        # Use the freshest factual TIME1 source available. This keeps the
        # display current when OBS Setting is receiving TIME1 into shared RAM
        # while this monitor's own TCP connection has an older sample.
        if (
            shared_fields is not None
            and (
                direct_fields is None
                or shared_age_s < direct_age_s
            )
        ):
            fields = shared_fields
            age_s = shared_age_s
            source = "shared_data_v10"
        else:
            fields = direct_fields
            age_s = (
                direct_age_s
                if math.isfinite(direct_age_s)
                else 0.0
            )
            source = "TCP TIME1"

        if fields is None:
            self.obs_time_estimated_delta_s = None
            self.obs_time_effective_sample_age_s = float("inf")
            self.obs_time_label.setText(
                "OBS TIME1: no device time received"
            )
            self.obs_time_delta_label.setText(
                "OBS-reference difference: --"
            )
            self.obs_time_delta_label.setObjectName(
                "timeDelta"
            )
            return

        device_dt = _time1_fields_to_naive_datetime(fields)
        if device_dt is None:
            self.obs_time_estimated_delta_s = None
            self.obs_time_effective_sample_age_s = float("inf")
            self.obs_time_label.setText(
                "OBS TIME1: invalid calendar value"
            )
            self.obs_time_delta_label.setText(
                "OBS-reference difference: unavailable"
            )
            self.obs_time_delta_label.setObjectName(
                "timeDeltaBad"
            )
            return

        counter = fields.get("counter")
        counter_text = "--" if counter is None else str(int(counter))
        self.obs_time_label.setText(
            "OBS TIME1: "
            f"{device_dt:%Y-%m-%d %H:%M:%S}."
            f"{device_dt.microsecond // 1000:03d} "
            f"| week={int(fields['week'])} cnt={counter_text} "
            f"| age={age_s:.2f}s | {source}"
        )

        # TIME1 is a timestamp sample captured earlier.  Comparing that raw
        # sample directly with the *current* PC clock makes the apparent offset
        # grow by approximately one second every second until the next TIME1
        # telemetry update.  v13 advances the device sample by its receive age
        # before calculating the current estimated offset.
        reference_now, reference_age_s, reference_label = self._selected_reference_now(pc_now)
        estimated_device_now = (
            device_dt
            + timedelta(seconds=max(0.0, float(age_s)))
        )
        if reference_now is None:
            self.obs_time_estimated_delta_s = None
            self.obs_time_effective_sample_age_s = float("inf")
            self.obs_time_delta_label.setText(
                f"OBS-reference difference: -- • {reference_label} unavailable/stale"
            )
            self.obs_time_delta_label.setObjectName("timeDeltaWarn")
            return
        delta_s = (estimated_device_now - reference_now).total_seconds()
        delta_ms = delta_s * 1000.0

        self.obs_time_estimated_delta_s = float(delta_s)
        self.obs_time_effective_sample_age_s = float(age_s)

        if abs(delta_s) <= OBS_TIME_AUTO_SYNC_DEFAULT_THRESHOLD_S:
            object_name = "timeDeltaGood"
        elif abs(delta_s) <= 2.0:
            object_name = "timeDeltaWarn"
        else:
            object_name = "timeDeltaBad"

        freshness = (
            "fresh"
            if age_s <= OBS_TIME_AUTO_SYNC_MAX_SAMPLE_AGE_S
            else "STALE"
        )
        self.obs_time_delta_label.setText(
            f"Estimated OBS vs {reference_label}: "
            f"{delta_ms:+.0f} ms • TIME1 sample {freshness}, age {age_s:.2f}s"
        )
        self.obs_time_delta_label.setObjectName(
            object_name
        )
        self.obs_time_delta_label.style().unpolish(
            self.obs_time_delta_label
        )
        self.obs_time_delta_label.style().polish(
            self.obs_time_delta_label
        )

    def _auto_sync_threshold_s(self) -> float:
        widget = getattr(
            self,
            "obs_time_auto_sync_threshold_spin",
            None,
        )
        if widget is None:
            return OBS_TIME_AUTO_SYNC_DEFAULT_THRESHOLD_S
        try:
            return max(
                OBS_TIME_AUTO_SYNC_MIN_THRESHOLD_S,
                min(
                    OBS_TIME_AUTO_SYNC_MAX_THRESHOLD_S,
                    float(widget.value()),
                ),
            )
        except Exception:
            return OBS_TIME_AUTO_SYNC_DEFAULT_THRESHOLD_S

    def _maybe_auto_sync_obs_time(self) -> None:
        checkbox = getattr(
            self,
            "obs_time_auto_sync_checkbox",
            None,
        )
        if checkbox is None or not checkbox.isChecked():
            return

        if (
            not self.obs_usb_monitor_connected
            or self.obs_time_pending
        ):
            return

        delta_s = self.obs_time_estimated_delta_s
        age_s = self.obs_time_effective_sample_age_s
        if delta_s is None or not math.isfinite(float(delta_s)):
            return
        if (
            not math.isfinite(float(age_s))
            or float(age_s) > OBS_TIME_AUTO_SYNC_MAX_SAMPLE_AGE_S
        ):
            return

        threshold_s = self._auto_sync_threshold_s()
        if abs(float(delta_s)) <= threshold_s:
            return

        now_mono = time.monotonic()
        if (
            self.obs_time_auto_sync_last_request_monotonic > 0.0
            and now_mono - self.obs_time_auto_sync_last_request_monotonic
            < OBS_TIME_AUTO_SYNC_MIN_INTERVAL_S
        ):
            return

        # Reserve the cooldown before sending so a failing IPC path cannot create
        # a 10-Hz retry loop from the GUI clock timer.
        self.obs_time_auto_sync_last_request_monotonic = now_mono
        self._set_obs_time_sync_status(
            f"AUTO TIME SYNC • {self._selected_time_source()} reference offset "
            f"{float(delta_s) * 1000.0:+.0f} ms exceeds "
            f"±{threshold_s * 1000.0:.0f} ms",
            "timeSyncSending",
        )
        self.send_obs_time_update()

    def _refresh_obs_time_control(
        self,
    ) -> None:
        if not hasattr(self, "obs_time_update_button"):
            return

        source_ready = True
        if self._selected_time_source() == TIME_SOURCE_GNSS:
            gnss_dt, gnss_age_s, _detail = self._read_gnss_time_reference()
            source_ready = bool(
                gnss_dt is not None
                and math.isfinite(float(gnss_age_s))
                and float(gnss_age_s) <= GNSS_TIME_MAX_SAMPLE_AGE_S
            )
        self.obs_time_update_button.setEnabled(
            bool(self.obs_usb_monitor_connected)
            and not bool(self.obs_time_pending)
            and source_ready
        )

    def _check_obs_time_pending_timeout(
        self,
    ) -> None:
        if not self.obs_time_pending:
            return

        if (
            time.monotonic() - self.obs_time_pending_since
            < OBS_TIME_CONFIRM_TIMEOUT_S
        ):
            return

        self.obs_time_pending = False
        self.obs_time_pending_since = 0.0
        self._set_obs_time_sync_status(
            "TIME UPDATE SENT, but no matching TIME1 telemetry confirmation "
            f"within {OBS_TIME_CONFIRM_TIMEOUT_S:g} s. "
            "The protocol supplied does not define a separate ACK.",
            "timeSyncWarn",
        )
        self._refresh_obs_time_control()

    def send_obs_time_update(
        self,
    ) -> None:
        if not self.obs_usb_monitor_connected:
            QMessageBox.warning(
                self,
                "OBS Time Synchronization",
                (
                    "OBS Setting centralized command broker is not connected.\n\n"
                    "Open/connect OBS Setting first, then wait for MONITOR: CONNECTED."
                ),
            )
            return

        if self.obs_time_pending:
            return

        worker = self.usb_log_monitor_thread
        if worker is None or not worker.isRunning():
            QMessageBox.warning(
                self,
                "OBS Time Synchronization",
                "OBS Setting IPC/status monitor is not running.",
            )
            return

        source = self._selected_time_source()
        reference_dt = None
        if source == TIME_SOURCE_GNSS:
            reference_dt, reference_age_s, reference_detail = self._read_gnss_time_reference()
            if (
                reference_dt is None
                or not math.isfinite(float(reference_age_s))
                or float(reference_age_s) > GNSS_TIME_MAX_SAMPLE_AGE_S
            ):
                QMessageBox.warning(
                    self,
                    "OBS Time Synchronization",
                    "GNSS TIME is unavailable or stale. Wait for a fresh GGA/RMC/ZDA "
                    "sentence, or select PC TIME.",
                )
                self._refresh_obs_time_control()
                return

        self.obs_time_pending = True
        self.obs_time_pending_source = source
        self.obs_time_pending_since = time.monotonic()
        self.obs_time_auto_sync_last_request_monotonic = self.obs_time_pending_since
        self.obs_time_last_sent_fields = None
        self._set_obs_time_sync_status(
            f"TIME UPDATE REQUESTED • source={source}",
            "timeSyncSending",
        )
        self._refresh_obs_time_control()

        try:
            worker.send_time_update(reference_dt=reference_dt, source=source)
        except Exception as exc:
            self.obs_time_pending = False
            self.obs_time_pending_since = 0.0
            self.on_obs_time_command_error(
                str(exc).strip() or exc.__class__.__name__
            )

    def on_obs_time_command_sent(
        self,
        sentence: str,
        endpoint: str,
        fields: object,
    ) -> None:
        self.obs_time_last_sent_fields = dict(fields or {})
        self.obs_time_pending_since = time.monotonic()
        self._set_obs_time_sync_status(
            f"TIME1 SENT • source={self.obs_time_pending_source} • {sentence} • {endpoint} • "
            "waiting for TIME1 telemetry confirmation",
            "timeSyncSending",
        )

    def on_obs_time_command_error(
        self,
        message: str,
    ) -> None:
        self.obs_time_pending = False
        self.obs_time_pending_since = 0.0
        self._set_obs_time_sync_status(
            f"TIME UPDATE FAILED • {message}",
            "timeSyncError",
        )
        self._refresh_obs_time_control()

    def on_obs_time1_invalid(
        self,
        raw_sentence: str,
        error: str,
    ) -> None:
        self._set_obs_time_sync_status(
            f"TIME1 RX INVALID • {error} • {raw_sentence}",
            "timeSyncError",
        )

    def on_obs_time1_received(
        self,
        fields: object,
        raw_sentence: str,
    ) -> None:
        parsed = dict(fields or {})
        self.obs_time_last_fields = parsed
        self.obs_time_last_sentence = str(raw_sentence)
        self.obs_time_last_rx_monotonic = time.monotonic()

        if self.obs_time_pending:
            device_dt = _time1_fields_to_naive_datetime(parsed)
            reference_now, _reference_age_s, reference_label = self._selected_reference_now()

            if device_dt is not None and reference_now is not None:
                delta_s = (device_dt - reference_now).total_seconds()
                if abs(delta_s) <= OBS_TIME_CONFIRM_TOLERANCE_S:
                    self.obs_time_pending = False
                    self.obs_time_pending_since = 0.0
                    self._set_obs_time_sync_status(
                        "TIME SYNC CONFIRMED BY FRESH TIME1 TELEMETRY • "
                        f"OBS vs {reference_label} {delta_s * 1000.0:+.0f} ms",
                        "timeSyncGood",
                    )
                else:
                    self._set_obs_time_sync_status(
                        "TIME1 RECEIVED AFTER UPDATE, but device time is still "
                        f"{delta_s:+.3f} s from PC • waiting for a matching TIME1",
                        "timeSyncWarn",
                    )

        self._refresh_obs_time_display()
        self._refresh_obs_time_control()

    # ------------------------------------------------------------------ OBS onboard USB logging

    @staticmethod
    def _tri_state_text(
        value: Optional[bool],
        true_text: str,
        false_text: str,
    ) -> str:
        if value is True:
            return true_text
        if value is False:
            return false_text
        return "UNKNOWN"

    def _start_obs_usb_monitor(
        self,
    ) -> None:
        if (
            self.usb_log_monitor_thread is not None
            and self.usb_log_monitor_thread.isRunning()
        ):
            return

        self.obs_usb_endpoint_label.setText(
            "Centralized command: OBS Setting IPC -> single TCP 54300"
        )

        worker = OBSUSBLoggingMonitorThread(
            parent=self,
        )
        worker.connection_changed.connect(
            self.on_obs_usb_monitor_connection_changed
        )
        worker.strg0_received.connect(
            self.on_obs_usb_strg0_received
        )
        worker.strg0_invalid.connect(
            self.on_obs_usb_strg0_invalid
        )
        worker.command_sent.connect(
            self.on_obs_usb_command_sent
        )
        worker.command_error.connect(
            self.on_obs_usb_command_error
        )
        worker.time1_received.connect(
            self.on_obs_time1_received
        )
        worker.time1_invalid.connect(
            self.on_obs_time1_invalid
        )
        worker.time1_command_sent.connect(
            self.on_obs_time_command_sent
        )
        worker.time1_command_error.connect(
            self.on_obs_time_command_error
        )
        worker.monitor_error.connect(
            self.on_obs_usb_monitor_error
        )
        self.usb_log_monitor_thread = worker
        worker.start()

    def _set_obs_usb_status(
        self,
        text: str,
        object_name: str,
    ) -> None:
        self.obs_usb_status_label.setText(text)
        self.obs_usb_status_label.setObjectName(object_name)
        self.obs_usb_status_label.style().unpolish(
            self.obs_usb_status_label
        )
        self.obs_usb_status_label.style().polish(
            self.obs_usb_status_label
        )

    def _refresh_obs_usb_device_label(
        self,
    ) -> None:
        device_text = self._tri_state_text(
            self.obs_usb_device_present,
            "CONNECTED",
            "NOT CONNECTED",
        )
        media_text = self._tri_state_text(
            self.obs_usb_media_present,
            "READY",
            "NOT PRESENT",
        )
        logger_text = self._tri_state_text(
            self.obs_usb_logger_active,
            "RECORDING",
            "STOPPED",
        )
        filename = self.obs_usb_current_file or "--"
        self.obs_usb_device_label.setText(
            f"Device: {device_text} | Media: {media_text} | "
            f"Logger: {logger_text} | File: {filename}"
        )

    def _refresh_obs_usb_controls(
        self,
    ) -> None:
        if not hasattr(self, "obs_usb_start_button"):
            return

        connected = bool(self.obs_usb_monitor_connected)
        pending = self.obs_usb_pending_command is not None

        # If MEDIA is explicitly absent, START is disabled.  UNKNOWN/recovering
        # remains usable because a freshly started PC may not receive insertion
        # history.  v28 requires an explicit operator confirmation before START
        # while the factual logger state is still unresolved.
        start_enabled = (
            connected
            and not pending
            and self.obs_usb_media_present is not False
            and self.obs_usb_logger_active is not True
        )

        # STOP is safe when status is UNKNOWN; firmware closes only if open.
        stop_enabled = (
            connected
            and not pending
            and self.obs_usb_logger_active is not False
        )

        self.obs_usb_start_button.setEnabled(start_enabled)
        self.obs_usb_stop_button.setEnabled(stop_enabled)

    def _check_obs_usb_pending_timeout(
        self,
    ) -> None:
        if self.obs_usb_pending_command is None:
            return

        if (
            time.monotonic() - self.obs_usb_pending_since
            < OBS_USB_CONFIRM_TIMEOUT_S
        ):
            return

        command_code = int(self.obs_usb_pending_command)
        self.obs_usb_pending_command = None
        self.obs_usb_pending_since = 0.0
        expected = (
            "MSDV_FILE_OPENED"
            if command_code == OBS_USB_LOG_START
            else "MSDV_FILE_CLOSED"
        )
        self._set_obs_usb_status(
            f"NO {expected} CONFIRMATION WITHIN "
            f"{OBS_USB_CONFIRM_TIMEOUT_S:g} s • check STRG0 / USB state",
            "onboardUnknown",
        )

    def _cancel_obs_usb_close_grace(
        self,
    ) -> None:
        timer = getattr(self, "obs_usb_close_grace_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
        self.obs_usb_close_grace_filename = ""

    def _finalize_obs_usb_close_grace(
        self,
    ) -> None:
        """Conclude STOPPED only if no rotation OPENED arrived during grace."""
        filename = self.obs_usb_close_grace_filename
        self.obs_usb_close_grace_filename = ""

        # A later OPENED or another definitive event already resolved state.
        if self.obs_usb_logger_active is True:
            return

        self.obs_usb_recovering = False
        self.obs_usb_logger_active = False
        self.obs_usb_current_file = ""
        self._set_obs_usb_status(
            f"USB LOGGING STOPPED • file closed: {filename or '--'}",
            "onboardStopped",
        )
        self._refresh_obs_usb_device_label()
        self._refresh_obs_usb_controls()

    def on_obs_usb_monitor_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:
        self.obs_usb_monitor_connected = bool(connected)

        if connected:
            self.obs_usb_connection_label.setText(
                f"MONITOR: CONNECTED • {detail}"
            )
            self.obs_usb_connection_label.setObjectName(
                "onboardConnectionGood"
            )

            # v28: a reconnect is not evidence that the firmware logger stopped.
            # Clear local certainty and wait for the next factual STRG0 event.
            self._cancel_obs_usb_close_grace()
            self.obs_usb_recovering = True
            self.obs_usb_logger_active = None
            self.obs_usb_current_file = ""
            self._set_obs_usb_status(
                "USB LOGGER STATE: RECOVERING • waiting for STRG0 file/open/close "
                "status from OBS; do not assume logging stopped after PC restart",
                "onboardUnknown",
            )
        else:
            self.obs_usb_connection_label.setText(
                f"MONITOR: DISCONNECTED • {detail}"
            )
            self.obs_usb_connection_label.setObjectName(
                "onboardConnectionBad"
            )
            self._cancel_obs_usb_close_grace()
            self.obs_usb_recovering = True
            self.obs_usb_logger_active = None
            self.obs_usb_current_file = ""
            self.obs_usb_pending_command = None
            self.obs_usb_pending_since = 0.0
            if self.obs_time_pending:
                self.obs_time_pending = False
                self.obs_time_pending_since = 0.0
                self._set_obs_time_sync_status(
                    "TIME SYNC INTERRUPTED • command TCP disconnected",
                    "timeSyncWarn",
                )

        self.obs_usb_connection_label.style().unpolish(
            self.obs_usb_connection_label
        )
        self.obs_usb_connection_label.style().polish(
            self.obs_usb_connection_label
        )
        self._refresh_obs_usb_controls()
        self._refresh_obs_time_control()

    def on_obs_usb_monitor_error(
        self,
        message: str,
    ) -> None:
        # Auto-reconnect runs in the worker. Keep this non-modal.
        if not self.obs_usb_monitor_connected:
            self._set_obs_usb_status(
                f"USB STATUS MONITOR OFFLINE • {message}",
                "onboardError",
            )

    def on_obs_usb_strg0_invalid(
        self,
        raw_sentence: str,
        error: str,
    ) -> None:
        self.obs_usb_last_sentence = raw_sentence
        self.obs_usb_last_status_time = time.time()
        self.obs_usb_strg0_label.setText(
            f"STRG0 INVALID • {error} • {raw_sentence}"
        )
        self._set_obs_usb_status(
            "USB STATUS RECEIVED WITH INVALID CHECKSUM",
            "onboardError",
        )

    def on_obs_usb_strg0_received(
        self,
        message: str,
        raw_sentence: str,
    ) -> None:
        self.obs_usb_last_message = str(message)
        self.obs_usb_last_sentence = str(raw_sentence)
        self.obs_usb_last_status_time = time.time()

        event_type, detail = classify_strg0_message(message)
        self.obs_usb_strg0_label.setText(
            f"STRG0 RX • {raw_sentence}"
        )

        if event_type == "MEDIA_INSERTION":
            self.obs_usb_media_present = True
            self._set_obs_usb_status(
                "USB MEDIA READY • storage media inserted",
                "onboardReady",
            )

        elif event_type == "MEDIA_REMOVAL":
            self._cancel_obs_usb_close_grace()
            self.obs_usb_recovering = False
            self.obs_usb_media_present = False
            self.obs_usb_logger_active = False
            self.obs_usb_current_file = ""
            self.obs_usb_pending_command = None
            self._set_obs_usb_status(
                "USB MEDIA REMOVED • onboard logging stopped",
                "onboardError",
            )

        elif event_type in ("DEVICE_CONNECTION", "DEVICE_INSERTION"):
            self.obs_usb_device_present = True
            self._set_obs_usb_status(
                "USB DEVICE DETECTED • waiting for storage-media status",
                "onboardReady",
            )

        elif event_type in ("DEVICE_DISCONNECTION", "NO_DEVICE"):
            self._cancel_obs_usb_close_grace()
            self.obs_usb_recovering = False
            self.obs_usb_device_present = False
            self.obs_usb_media_present = False
            self.obs_usb_logger_active = False
            self.obs_usb_current_file = ""
            self.obs_usb_pending_command = None
            self._set_obs_usb_status(
                "NO USB DEVICE • START logging unavailable",
                "onboardError",
            )

        elif event_type == "ENUMERATION_FAILURE":
            self._cancel_obs_usb_close_grace()
            self.obs_usb_recovering = False
            self.obs_usb_device_present = True
            self.obs_usb_media_present = False
            self.obs_usb_logger_active = False
            self.obs_usb_pending_command = None
            self._set_obs_usb_status(
                "USB ENUMERATION FAILURE • check USB device/media",
                "onboardError",
            )

        elif event_type == "USB_ERROR":
            self._set_obs_usb_status(
                f"USB SYSTEM ERROR • {detail}",
                "onboardError",
            )

        elif event_type == "MSC_ID":
            self.obs_usb_device_present = True
            self._set_obs_usb_status(
                f"USB MSC IDENTIFIED • {detail}",
                "onboardReady",
            )

        elif event_type == "FILE_OPENED":
            self._cancel_obs_usb_close_grace()
            self.obs_usb_recovering = False
            self.obs_usb_device_present = True
            self.obs_usb_media_present = True
            self.obs_usb_logger_active = True
            self.obs_usb_current_file = detail or "(unnamed)"
            self.obs_usb_pending_command = None
            self.obs_usb_pending_since = 0.0

            rotated = bool(
                self.obs_usb_last_closed_file
                and self.obs_usb_last_closed_file != self.obs_usb_current_file
            )
            prefix = "USB LOGGING ACTIVE"
            if rotated:
                prefix = "USB LOGGING ACTIVE • ROTATED TO NEW FILE"

            self._set_obs_usb_status(
                f"{prefix} • {self.obs_usb_current_file}",
                "onboardStarted",
            )

        elif event_type == "FILE_CLOSED":
            self.obs_usb_last_closed_file = detail
            self.obs_usb_current_file = ""

            if self.obs_usb_pending_command == OBS_USB_LOG_STOP:
                # Explicit operator STOP: this CLOSED is definitive.
                self._cancel_obs_usb_close_grace()
                self.obs_usb_recovering = False
                self.obs_usb_logger_active = False
                self.obs_usb_pending_command = None
                self.obs_usb_pending_since = 0.0
                self._set_obs_usb_status(
                    f"USB LOGGING STOPPED • file closed: {detail or '--'}",
                    "onboardStopped",
                )
            else:
                # v28: normal firmware rotation emits CLOSED then immediately
                # OPENED.  Do not publish a false STOPPED state during that gap.
                self.obs_usb_recovering = True
                self.obs_usb_logger_active = None
                self.obs_usb_close_grace_filename = detail
                self.obs_usb_close_grace_timer.start()
                self._set_obs_usb_status(
                    f"USB FILE CLOSED • {detail or '--'} • RECOVERING / waiting "
                    f"{OBS_USB_ROTATION_GRACE_S:g}s for rotation OPENED event",
                    "onboardUnknown",
                )

        elif event_type == "EVENT_UNKNOWN":
            self._set_obs_usb_status(
                "USB EVENT UNKNOWN • see raw STRG0 sentence",
                "onboardUnknown",
            )

        else:
            self._set_obs_usb_status(
                f"USB STATUS • {detail}",
                "onboardUnknown",
            )

        self._refresh_obs_usb_device_label()
        self._refresh_obs_usb_controls()

    def send_obs_usb_logging_command(
        self,
        command_code: int,
    ) -> None:
        command_code = int(command_code)

        if not self.obs_usb_monitor_connected:
            QMessageBox.warning(
                self,
                "OBS USB Logging",
                (
                    "OBS Setting centralized command broker is not connected.\n\n"
                    "Open/connect OBS Setting first, then wait for MONITOR: CONNECTED."
                ),
            )
            return

        if self.obs_usb_pending_command is not None:
            return

        if (
            command_code == OBS_USB_LOG_START
            and self.obs_usb_media_present is False
        ):
            QMessageBox.warning(
                self,
                "OBS USB Logging",
                (
                    "START blocked: STRG0 reports no USB storage media.\n\n"
                    "Insert/attach USB storage and wait for "
                    "USB_STORAGE_MEDIA_INSERTION before starting."
                ),
            )
            return

        if (
            command_code == OBS_USB_LOG_START
            and self.obs_usb_logger_active is True
        ):
            self._set_obs_usb_status(
                f"USB LOGGING ALREADY ACTIVE • {self.obs_usb_current_file or '--'}",
                "onboardStarted",
            )
            return

        if (
            command_code == OBS_USB_LOG_START
            and (self.obs_usb_recovering or self.obs_usb_logger_active is None)
        ):
            answer = QMessageBox.question(
                self,
                "OBS USB Logging - State Not Yet Confirmed",
                (
                    "The current OBS USB logger state has not yet been confirmed "
                    "after startup/reconnect.\n\n"
                    "If OBS was already logging before the PC restarted, the next "
                    "MSDV_FILE_OPENED event from firmware rotation will restore the "
                    "RECORDING state automatically.\n\n"
                    "Send START anyway?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_obs_usb_status(
                    "USB LOGGER STATE: RECOVERING • START cancelled; waiting for "
                    "factual STRG0 status",
                    "onboardUnknown",
                )
                return

        if (
            command_code == OBS_USB_LOG_STOP
            and self.obs_usb_logger_active is False
        ):
            self._set_obs_usb_status(
                "USB LOGGING ALREADY STOPPED • confirmed by STRG0",
                "onboardStopped",
            )
            return

        worker = self.usb_log_monitor_thread
        if worker is None or not worker.isRunning():
            QMessageBox.warning(
                self,
                "OBS USB Logging",
                "USB status monitor thread is not running.",
            )
            return

        action = (
            "START"
            if command_code == OBS_USB_LOG_START
            else "STOP"
        )
        expected = (
            "MSDV_FILE_OPENED"
            if command_code == OBS_USB_LOG_START
            else "MSDV_FILE_CLOSED"
        )

        self.obs_usb_pending_command = command_code
        self.obs_usb_pending_since = time.monotonic()
        self._set_obs_usb_status(
            f"{action} REQUESTED • waiting for {expected} STRG0 confirmation",
            "onboardSending",
        )
        self._refresh_obs_usb_controls()

        try:
            worker.send_command(command_code)
        except Exception as exc:
            self.obs_usb_pending_command = None
            self.obs_usb_pending_since = 0.0
            self.on_obs_usb_command_error(
                command_code,
                str(exc) or exc.__class__.__name__,
            )

    def on_obs_usb_command_sent(
        self,
        command_code: int,
        sentence: str,
        endpoint: str,
    ) -> None:
        expected = (
            "MSDV_FILE_OPENED"
            if int(command_code) == OBS_USB_LOG_START
            else "MSDV_FILE_CLOSED"
        )
        action = (
            "START"
            if int(command_code) == OBS_USB_LOG_START
            else "STOP"
        )
        self._set_obs_usb_status(
            f"{action} SENT • {sentence} • {endpoint} • "
            f"waiting for {expected}",
            "onboardSending",
        )

    def on_obs_usb_command_error(
        self,
        command_code: int,
        message: str,
    ) -> None:
        action = (
            "START"
            if int(command_code) == OBS_USB_LOG_START
            else "STOP"
        )
        self.obs_usb_pending_command = None
        self.obs_usb_pending_since = 0.0
        self._set_obs_usb_status(
            f"{action} COMMAND FAILED • {message}",
            "onboardError",
        )
        self._refresh_obs_usb_controls()

    # ------------------------------------------------------------------ recording

    def set_settings_enabled(
        self,
        enabled: bool,
    ):
        self.settings_panel.setEnabled(
            enabled
        )

    def start_recording(
        self,
    ):
        if self.recording:
            return

        if np is None:
            QMessageBox.critical(
                self,
                APP_TITLE,
                "NumPy is required.",
            )
            return

        if not OBSPY_AVAILABLE:
            QMessageBox.critical(
                self,
                APP_TITLE,
                "ObsPy is required.\n\n"
                "Install:\n"
                "pip install obspy",
            )
            return

        try:
            config = (
                self.build_config()
            )

        except Exception as exc:
            QMessageBox.warning(
                self,
                APP_TITLE,
                str(
                    exc
                ),
            )
            return

        if not config.record_geophone:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Enable Geophone N / E / Z for PC MiniSEED recording.",
            )
            return

        self.worker = (
            MiniSeedRecordingWorker(
                config,
                self,
            )
        )

        self.worker.status_changed.connect(
            self.on_worker_status
        )
        self.worker.stats_changed.connect(
            self.on_worker_stats
        )
        self.worker.recording_error.connect(
            self.on_worker_error
        )
        self.worker.recording_finished.connect(
            self.on_worker_finished
        )

        self.recording = True

        self.set_settings_enabled(
            False
        )

        self.start_button.setEnabled(
            False
        )
        self.stop_button.setEnabled(
            True
        )

        self.record_state.setText(
            "● RECORDING"
        )
        self.record_state.setObjectName(
            "stateRecording"
        )

        self.record_state.style().unpolish(
            self.record_state
        )
        self.record_state.style().polish(
            self.record_state
        )

        usbl = (
            self.shared.read_usbl()
        )

        if not valid_position(
            usbl
        ):
            self.status_label.setText(
                "Recording starting — USBL has no valid position yet; "
                "position CSV fields will remain blank until a valid fix arrives."
            )
        else:
            self.status_label.setText(
                "Starting MiniSEED recording..."
            )

        self.worker.start()

    def stop_recording(
        self,
    ):
        if (
            not self.recording
            or self.worker is None
        ):
            return

        self.stop_button.setEnabled(
            False
        )

        self.record_state.setText(
            "FINALIZING"
        )
        self.record_state.setObjectName(
            "stateFinalizing"
        )

        self.record_state.style().unpolish(
            self.record_state
        )
        self.record_state.style().polish(
            self.record_state
        )

        self.status_label.setText(
            "Finalizing MiniSEED records..."
        )

        self.worker.stop_recording()

    def on_worker_status(
        self,
        text: str,
    ):
        self.status_label.setText(
            text
        )

    @staticmethod
    def human_bytes(
        size: int,
    ):
        value = float(
            size
        )

        for unit in (
            "B",
            "KB",
            "MB",
            "GB",
            "TB",
        ):
            if value < 1024.0:
                return (
                    f"{value:.2f} {unit}"
                )

            value /= 1024.0

        return (
            f"{value:.2f} PB"
        )

    def on_worker_stats(
        self,
        stats,
    ):
        elapsed = int(
            stats.get(
                "elapsed_s",
                0.0,
            )
        )

        hours = (
            elapsed
            // 3600
        )
        minutes = (
            elapsed
            % 3600
        ) // 60
        seconds = (
            elapsed
            % 60
        )

        self.duration_label.setText(
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

        self.raw_samples_label.setText(
            f"{int(stats.get('raw_adc_samples', 0)):,}"
        )

        self.decimated_samples_label.setText(
            f"{int(stats.get('decimated_samples', 0)):,}"
        )

        self.imu_samples_label.setText(
            f"{int(stats.get('imu_samples', 0)):,}"
        )

        self.file_size_label.setText(
            self.human_bytes(
                int(
                    stats.get(
                        "bytes_written",
                        0,
                    )
                )
            )
        )

        lag = int(
            stats.get(
                "recorder_lag_samples",
                0,
            )
        )

        self.lag_samples_label.setText(
            f"{lag:,}"
        )


    def _schedule_close_after_worker_exit(self, worker) -> None:
        self._closing_worker = worker
        QTimer.singleShot(50, self._close_after_worker_exit)

    def _close_after_worker_exit(self) -> None:
        worker = self._closing_worker
        if worker is not None and worker.isRunning():
            QTimer.singleShot(50, self._close_after_worker_exit)
            return

        self._closing_worker = None
        self.close()

    def on_worker_error(
        self,
        message: str,
    ):
        # During a confirmed application shutdown, do not leave a modal error
        # box blocking the Main shutdown sequence. The worker already performs
        # its best-effort segment finalization before emitting recording_error.
        worker = self.worker

        if not self._close_after_recording:
            QMessageBox.critical(
                self,
                APP_TITLE,
                f"Recording error:\n\n{message}",
            )

        self._return_to_stopped(
            (
                "Recording stopped due to error: "
                + message
            )
        )

        if self._close_after_recording:
            self._schedule_close_after_worker_exit(worker)

    def on_worker_finished(
        self,
        folder: str,
    ):
        worker = self.worker

        self._return_to_stopped(
            f"Recording stopped; last segment saved: {folder}"
        )

        if self._close_after_recording:
            self._schedule_close_after_worker_exit(worker)

    def _return_to_stopped(
        self,
        status_text: str,
    ):
        self.recording = False

        self.set_settings_enabled(
            True
        )

        self.start_button.setEnabled(
            OBSPY_AVAILABLE
        )
        self.stop_button.setEnabled(
            False
        )

        self.record_state.setText(
            "STOPPED"
        )
        self.record_state.setObjectName(
            "stateStopped"
        )

        self.record_state.style().unpolish(
            self.record_state
        )
        self.record_state.style().polish(
            self.record_state
        )

        self.status_label.setText(
            status_text
        )

        self.worker = None

    # ------------------------------------------------------------------ style

    def _apply_style(
        self,
    ):
        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QLabel {
                color: #FFFFFF;
                background: transparent;
            }

            QLabel#titleLabel {
                font-size: 20px;
                font-weight: 800;
                letter-spacing: 0.8px;
            }

            QLabel#subtitleLabel {
                color: #A9BECA;
                font-size: 10px;
            }

            QFrame#statusFrame {
                background: #0B1B27;
                border: 1px solid #17374A;
                border-radius: 8px;
            }

            QLabel#statusLabel {
                color: #B7CBD6;
                font-size: 10px;
            }

            QLabel#durationLabel {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 18px;
                font-weight: 900;
                padding-left: 10px;
            }

            QLabel#stateStopped {
                background: #172631;
                border: 1px solid #35546A;
                border-radius: 7px;
                color: #A9BECA;
                font-weight: 900;
                padding: 5px 12px;
            }

            QLabel#stateRecording {
                background: #571C24;
                border: 1px solid #D14C5E;
                border-radius: 7px;
                color: #FFD2D8;
                font-weight: 900;
                padding: 5px 12px;
            }

            QLabel#stateFinalizing {
                background: #403510;
                border: 1px solid #A88821;
                border-radius: 7px;
                color: #FFE49A;
                font-weight: 900;
                padding: 5px 12px;
            }

            /*
             * V10: keep the scroll/work area dark, but visually separate it
             * from each group card.  Only the viewport/host panels receive
             * this background so controls inside the cards keep their own
             * styling.
             */
            QScrollArea#panelScroll {
                background: #07151F;
                border: none;
            }

            QWidget#panelViewport,
            QFrame#settingsPanel,
            QFrame#livePanel {
                background: #07151F;
            }

            QSplitter::handle {
                background: #102C3A;
                width: 2px;
            }


            QScrollBar:vertical {
                background: #07151F;
                width: 11px;
                margin: 0px;
                border: none;
            }

            QScrollBar::handle:vertical {
                background: #24485D;
                min-height: 28px;
                border-radius: 5px;
            }

            QScrollBar::handle:vertical:hover {
                background: #315F78;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
                border: none;
                height: 0px;
            }

            QScrollBar:horizontal {
                background: #07151F;
                height: 11px;
                margin: 0px;
                border: none;
            }

            QScrollBar::handle:horizontal {
                background: #24485D;
                min-width: 28px;
                border-radius: 5px;
            }

            QScrollBar::handle:horizontal:hover {
                background: #315F78;
            }

            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal,
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }

            QGroupBox#controlGroup {
                background: #0D2230;
                border: 1px solid #1E455B;
                border-radius: 9px;
                margin-top: 12px;
                padding-top: 7px;
                color: #FFFFFF;
                font-weight: 800;
            }

            QGroupBox#controlGroup::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 9px;
                padding: 2px 7px;
                color: #FFFFFF;
                background: #0D2230;
                border: 1px solid #1E455B;
                border-radius: 4px;
            }

            QLabel#hintText {
                color: #7894A4;
                font-size: 9px;
            }

            QLabel#fixedValue {
                color: #DDEAF2;
                font-family: "Consolas";
                font-size: 11px;
                font-weight: 800;
            }

            QLabel#monoValue {
                color: #DDEAF2;
                font-family: "Consolas";
                font-size: 10px;
            }

            QLabel#positionValid {
                background: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 6px;
                color: #A9F1D2;
                font-weight: 900;
                padding: 5px;
            }

            QLabel#positionInvalid,
            QLabel#positionState {
                background: #403510;
                border: 1px solid #A88821;
                border-radius: 6px;
                color: #FFE49A;
                font-weight: 900;
                padding: 5px;
            }

            QLineEdit,
            QSpinBox,
            QDoubleSpinBox {
                background: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 5px;
                min-height: 27px;
                padding: 2px 6px;
            }

            QLineEdit:read-only {
                color: #B8CBD6;
                background: #091821;
            }

            QCheckBox {
                color: #DDE9EF;
                spacing: 6px;
            }

            QPushButton {
                min-height: 29px;
                border-radius: 6px;
                padding: 4px 8px;
                font-weight: 800;
                background: #162D3A;
                color: #DDEAF2;
                border: 1px solid #2A4E62;
            }

            QPushButton#secondaryButton {
                background: #123147;
                border: 1px solid #285B78;
            }

            QPushButton#startButton {
                background: #176B4C;
                border: 1px solid #35A775;
                color: #E4FFF3;
                font-size: 13px;
            }

            QPushButton#stopButton {
                background: #6A1F29;
                border: 1px solid #B84454;
                color: #FFD7DC;
                font-size: 13px;
            }

            QPushButton#onboardStartButton {
                background: #115B43;
                border: 1px solid #2F9E73;
                color: #E4FFF3;
            }

            QPushButton#onboardStopButton {
                background: #67212B;
                border: 1px solid #AE4655;
                color: #FFD7DC;
            }

            QLabel#onboardConnection {
                color: #FFE49A;
                font-family: "Consolas";
            }

            QLabel#onboardConnectionGood {
                color: #A9F1D2;
                font-family: "Consolas";
                font-weight: 700;
            }

            QLabel#onboardConnectionBad {
                color: #FFB8C1;
                font-family: "Consolas";
                font-weight: 700;
            }

            QLabel#onboardSentence {
                background: #0A1822;
                border: 1px solid #294554;
                border-radius: 4px;
                color: #B9D6E5;
                font-family: "Consolas";
                padding: 4px;
            }

            QLabel#onboardReady {
                background: #113149;
                border: 1px solid #2B719C;
                border-radius: 5px;
                color: #C6E9FF;
                font-family: "Consolas";
                padding: 5px;
            }

            QLabel#onboardUnknown,
            QLabel#onboardSending {
                background: #403510;
                border: 1px solid #A88821;
                border-radius: 5px;
                color: #FFE49A;
                font-family: "Consolas";
                padding: 5px;
            }

            QLabel#onboardStarted {
                background: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 5px;
                color: #A9F1D2;
                font-family: "Consolas";
                padding: 5px;
            }

            QLabel#onboardStopped {
                background: #172631;
                border: 1px solid #35546A;
                border-radius: 5px;
                color: #C8DBE5;
                font-family: "Consolas";
                padding: 5px;
            }

            QLabel#onboardError {
                background: #571C24;
                border: 1px solid #D14C5E;
                border-radius: 5px;
                color: #FFD2D8;
                font-family: "Consolas";
                padding: 5px;
            }

            QFrame#timeSyncFrame {
                background: #091821;
                border: 1px solid #25495D;
                border-radius: 6px;
            }

            QLabel#timeValue {
                color: #DDEAF2;
                font-family: "Consolas";
                font-size: 10px;
            }

            QLabel#timeDelta {
                color: #A9BECA;
                font-family: "Consolas";
            }

            QLabel#timeDeltaGood,
            QLabel#timeSyncGood {
                color: #A9F1D2;
                font-family: "Consolas";
                font-weight: 800;
            }

            QLabel#timeDeltaWarn,
            QLabel#timeSyncWarn,
            QLabel#timeSyncSending,
            QLabel#timeSyncIdle {
                color: #FFE49A;
                font-family: "Consolas";
            }

            QLabel#timeDeltaBad,
            QLabel#timeSyncError {
                color: #FFB8C1;
                font-family: "Consolas";
                font-weight: 800;
            }

            QPushButton#timeSyncButton {
                background: #164B68;
                border: 1px solid #3784AB;
                color: #E2F6FF;
            }

            QMessageBox {
                background: #0D1E2A;
            }

            QMessageBox QLabel {
                color: #FFFFFF;
                background: transparent;
                min-width: 360px;
            }

            QMessageBox QPushButton {
                min-width: 84px;
                color: #FFFFFF;
                background: #163449;
                border: 1px solid #326784;
            }

            QPushButton:disabled {
                background: #101D25;
                color: #50636E;
                border: 1px solid #21323C;
            }

            QSplitter::handle {
                background: #17374A;
                width: 2px;
            }
            """
        )

    # ------------------------------------------------------------------ close

    @staticmethod
    def _main_coordinated_shutdown_requested() -> bool:
        marker = str(os.environ.get(MAIN_SHUTDOWN_FLAG_ENV, "")).strip()
        if not marker:
            return False
        try:
            return Path(marker).is_file()
        except OSError:
            return False

    def _perform_close_cleanup(self) -> None:
        if self._close_cleanup_done:
            return
        self._close_cleanup_done = True

        try:
            self.position_timer.stop()
        except Exception:
            pass

        try:
            self.clock_timer.stop()
        except Exception:
            pass

        if (
            self.usb_log_monitor_thread is not None
            and self.usb_log_monitor_thread.isRunning()
        ):
            self.usb_log_monitor_thread.stop()
            self.usb_log_monitor_thread.wait(
                int(
                    (
                        OBS_COMMAND_CONNECT_TIMEOUT_S
                        + OBS_COMMAND_RECONNECT_S
                        + 0.5
                    )
                    * 1000
                )
            )

        # IMPORTANT: stopping the PC monitor does not issue RMCMD 62. OBS
        # onboard USB logging remains active by design.
        try:
            self.shared.close()
        except Exception:
            pass

    def closeEvent(
        self,
        event: QCloseEvent,
    ):
        if self.recording and self.worker is not None:
            coordinated = self._main_coordinated_shutdown_requested()

            if not coordinated:
                answer = QMessageBox.question(
                    self,
                    APP_TITLE,
                    (
                        "PC MiniSEED recording is active.\n\n"
                        "Stop, save, and close this window?"
                    ),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )

                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return

            # Do not block the GUI thread with worker.wait(). Ask the recorder to
            # stop through its normal path, ignore this first close event, then
            # close automatically after recording_finished/recording_error.
            self._close_after_recording = True
            self.stop_recording()
            self.status_label.setText(
                "Finalizing PC MiniSEED records; application will close automatically..."
            )
            event.ignore()
            return

        self._perform_close_cleanup()
        event.accept()


def main() -> int:
    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        APP_TITLE
    )

    app.setApplicationDisplayName(
        f"{APP_TITLE} - {SYSTEM_TITLE}"
    )

    icon = application_icon()

    if not icon.isNull():
        app.setWindowIcon(
            icon
        )

    font = QFont(
        "Segoe UI"
    )
    font.setPointSize(
        9
    )
    app.setFont(
        font
    )

    try:
        window = (
            MiniSeedRecordingWindow()
        )

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start MiniSEED Recording:\n\n{exc}",
        )
        return 1

    if getattr(
        window,
        "_startup_maximized",
        False,
    ):
        window.showMaximized()
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
