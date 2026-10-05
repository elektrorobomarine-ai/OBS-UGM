"""
obs_setting.py
==============

GRC-UGM-PERTAMINA OBS
OBS Setting / Connection Manager

Version: 28
Shared data: shared_data.py


Version 28 corrected-firmware ADC-rate contract
------------------------------------------------
- Removes the operator-editable ADC Source Rate field. The corrected firmware
  contract is an expected 1000.000 Hz raw ADC rate; this value is read-only in
  OBS Setting and is the authoritative runtime time-base until the protocol
  exposes an explicit firmware-reported sample-rate field.
- Adds a long-window PC-side ADC-rate estimator using bulk-frame sequence span
  versus monotonic PC time. It is diagnostic only: PASS/WARN/FAULT never
  changes waveform samples, timestamps or MiniSEED metadata dynamically.
- Publishes the measured rate, ppm error and diagnostic state through
  shared_data API v10. A new acquisition session resets the diagnostic to
  WARMING UP.
- obs_settings.ini no longer controls raw ADC rate. Legacy raw_sample_rate_hz
  entries are ignored; only the decimation setting remains operator-configurable.
- ADC sample values, TCP framing, channel mapping and existing block-average
  processing are unchanged. FIR/decimation ownership remains a separate
  firmware/runtime integration decision.

Version 27 acquisition-rate metadata / timing correction
--------------------------------------------------------
- Adds an explicit configurable ADC source/acquisition sample rate.
- Transitional version that allowed a configured source-rate override while
  firmware clock correction was pending. Retained here only as version history;
  v28 removes that operator override.

Version 26 GNSS/USBL freshness and effective-fix handling
--------------------------------------------------------
- Separates transport readiness from live navigation data: UDP is shown as
  LISTENING and serial as COM OPEN rather than treating either as a live fix.
- Tracks raw NMEA receive count and source age for GNSS and USBL.
- A GNSS/USBL source becomes STALE after 3 seconds without new NMEA data.
- Effective GGA Fix becomes 0 when NMEA/GGA is stale, while the last valid
  latitude/longitude remain visible as LAST VALID POSITION.
- Raw shared-RAM GGA snapshots are not rewritten or falsified; source freshness
  is derived from shared_data acquisition-health timestamps.
- Cerulean Tracker can therefore stop/close without leaving a misleading live
  Fix 1 indication in OBS Setting.
- TCP acquisition, ADC processing, command broker, gimbal/power control and the
  v25 proportional OBS-disconnect dialog are unchanged.

Version 25 proportional unexpected-disconnect dialog
------------------------------------------------------
- Replaces the wide native QMessageBox with a compact custom QDialog.
- Uses a balanced 520 x 400 px initial footprint instead of a long horizontal box.
- DATA and COMMAND states are shown as separate compact rows.
- Technical socket details remain expandable inside the same dialog.
- Reconnect/Dismiss behavior and all v22-v24 disconnect detection logic are unchanged.

Version 24 operator-friendly unexpected disconnect dialog
----------------------------------------------------------
- Replaces the long raw disconnect warning with a concise operator dialog.
- Shows DATA/COMMAND link state, a short human-readable cause and disconnect
  time in WIB; raw socket errors remain available under Show Details.
- Adds Dismiss and Reconnect OBS actions. Reconnect cleanly stops any remaining
  OBS worker before reusing the existing connect_obs() path.
- Retains the v22 behavior that coalesces near-simultaneous DATA/COMMAND losses
  into one alert and suppresses alerts for explicit disconnect/shutdown.

Version 23 default OBS power state
----------------------------------
- Changes the no-feedback/default OBS power-mode display from NORMAL MODE to
  LOW POWER, matching the controller power state expected at initial connection.
- Startup shared telemetry now publishes power_mode=LOW.
- When the COMMAND link is unavailable and the GUI falls back to its protocol
  default, the displayed power state is LOW POWER instead of NORMAL MODE.
- The Low Power / Normal Mode command mapping and transmitted $RMCMD frames are
  unchanged.

Version 22 unexpected OBS disconnect notification
-------------------------------------------------
- Shows a visible warning when DATA (TCP 54301) or COMMAND (TCP 54300) had
  previously reached CONNECTED state and is then lost unexpectedly.
- Suppresses the warning during explicit Disconnect OBS and final application
  shutdown, and does not warn for a connection attempt that never connected.
- Coalesces near-simultaneous DATA/COMMAND losses into one notification.
- Restores/raises the OBS Setting window if it was minimized so the operator can
  see the disconnect warning.
- Adds a DATA-stream watchdog: no valid OBS bulk frame for 3 seconds after the
  DATA link connects is treated as an unexpected DATA loss.
- Aligns Geophone display terminology with the established physical orientation:
  CH0 = N, CH1 = E, CH2 = Z. Signal processing and channel data mapping are
  unchanged.

Version 21 reliable spin-box arrow controls
-------------------------------------------
- Replaces the native QSpinBox instances with ReliableSpinBox.
- The right-side spinner hit area is explicitly split into an upper increment
  zone and a lower decrement zone, avoiding the Qt/Windows styling issue where
  the visible UP arrow could fail to receive the expected step action.
- Applies consistently to Data Port, Command Port, Geophone Decimation,
  GNSS UDP Port and USBL UDP Port.
- Keyboard entry, keyboard arrows, mouse wheel and existing valueChanged
  behavior remain inherited from QSpinBox.

Version 20 centralized IMU yaw calibration
------------------------------------------
- Loads [IMU] yaw_offset_deg from obs_settings.ini.
- Accepts local IPC request type set_yaw_offset from Other Sensors.
- Applies the configured offset to AHRS2 yaw BEFORE publishing telemetry.yaw
  into shared_data. All consumers therefore use one corrected/system yaw.
- The raw AHRS2 wire yaw is never modified on the OBS controller; correction is
  a PC-side calibration applied only in the centralized acquisition manager.

Version 19 lifecycle update
---------------------------
- Closing the OBS Setting window with the title-bar X no longer disconnects
  TCP 54300/54301. The window is minimized and acquisition continues.
- Physical OBS disconnect occurs only from the explicit Disconnect OBS action
  or from a local shutdown request issued by the main launcher.
- The local IPC broker accepts show_window and main-only shutdown lifecycle
  requests before checking the OBS command-link state.

Version 18 centralized OBS I/O + local command broker
-----------------------------------
- Preserves the native-scroll UI introduced in v15.
- The OBS Setting process is the sole acquisition owner for command-channel
  telemetry used by Other Sensors: $AHRS2, $DEPT0 and $XCHM1 are parsed here
  and published to shared_data. Consumer GUIs do not open OBS sockets.
- $AHRS2 roll/pitch/yaw are wire-format E1 values and are converted to
  physical degrees by dividing by 10 BEFORE publishing to shared RAM.
- $DEPT0 field 0 is treated as the firmware pressure source and converted
  with E1 scaling (raw / 10). It is published through telemetry.pressure.
- $DEPT0 temperature is converted with E2 scaling (raw / 100) before
  publishing to shared RAM.
- P/Q/R angular rates are left unchanged because no new scale factor was
  supplied for those fields.

Version 15 UI update
--------------------
- The settings body uses an explicit native QScrollArea with a vertical scrollbar
  shown as needed. Header and footer remain fixed.
- The scroll content uses a minimum-size layout constraint so cards retain their
  natural height instead of being compressed when the window is short.
- Mouse-wheel/trackpad scrolling uses a practical step and the scrollbar is styled
  consistently with the dark OBS interface.
Protocol baseline: OBS TCP protocol supplied 2026-08-19.

OBS TCP
-------
Command / Telemetry:
    TCP port 54300
    Client connects to OBS.
    NMEA-0183 style text:
        $ID,field1,...*CC<CR><LF>
    CC = XOR of bytes between '$' and '*'
    Maximum sentence length = 82 bytes.

Telemetry parsed:
    $GDAT2
    $TIME1
    $DEPT0
    $AHRS2
    $XFWVR
    $XCHM0
    $XCHM1

Bulk data:
    TCP port 54301

    Header 12 bytes:
        0..3   magic "OBS:"
        4..7   uint32 sequence, little-endian
        8..11  uint32 payload length, little-endian

    Current payload:
        2048 bytes
        4 channels x 128 ADC frames x 4-byte words

    ADC word:
        bits 23..0  signed 24-bit ADC
        bits 31..24 status byte

Shared RAM
----------
Live data is written through shared_data.py.

The corrected-firmware ADC source-rate contract is 1000.000 Hz/channel and is
not operator-editable. OBS Setting also computes a long-window PC-side rate
estimate as a diagnostic only; that estimate never changes waveform metadata or
sample timestamps dynamically. Before publication to shared RAM, CH0/CH1/CH2
(Geophone N/E/Z) are block-averaged using the selected Decimation Samples value.
CH3 follows the same averaging window to preserve the 4-channel frame structure.

Example with corrected-firmware timing:
    ADC Expected Rate = 1000.000 Hz
    Decimation Samples = 5
    1000.000 / 5 = 200.000 output frames/s

shared_data publishes the authoritative effective sample rate and output
sample period. Decimation is NOT represented as fake missing samples.

GNSS
----
Selectable COM Port or UDP. Every received NMEA sentence is published to the
shared-RAM diagnostic slot; valid GGA is parsed and published as GNSS position.

USBL
----
Selectable COM Port or UDP. Every received NMEA sentence is published to the
shared-RAM diagnostic slot; valid GGA is parsed and published separately.

Remote GPIO control
-------------------
The supplied firmware protocol defines gimbal LOCK and PWR_MODE through:

    $RMCMD,<CMD_CODE>*CC<CR><LF>

    1 = ENABLE_1  -> PWR_MODE GPIO HIGH
    2 = ENABLE_2  -> LOCK GPIO HIGH
    5 = DISABLE_1 -> PWR_MODE GPIO LOW
    6 = DISABLE_2 -> LOCK GPIO LOW

Exact transmitted sentences generated by the existing XOR checksum routine:

    PWR_MODE HIGH : $RMCMD,1*48\r\n
    LOCK HIGH     : $RMCMD,2*4B\r\n
    PWR_MODE LOW  : $RMCMD,5*4C\r\n
    LOCK LOW      : $RMCMD,6*4F\r\n
The GUI treats LOW POWER as PWR_MODE HIGH and NORMAL MODE as PWR_MODE LOW,
matching the established OBS Setting button mapping. The firmware protocol does
not define a separate acknowledgement/state-feedback sentence, so a displayed
change after a button press is a commanded/requested state.
"""

from __future__ import annotations

import configparser
from collections import deque
import logging
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows GUI startup
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.SETTING"

if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

        kernel32 = ctypes.windll.kernel32

        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()

    except Exception:
        pass


# =============================================================================
# Qt
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)


# =============================================================================
# Shared RAM v3
# =============================================================================

from shared_data import (
    ADC_DESIGN_SAMPLE_RATE_HZ,
    ADC_STATUS_CHANNEL_ID_MASK,
    ADC_STATUS_ERROR,
    ADC_STATUS_FILTER_NOT_SETTLED,
    ADC_STATUS_REPEATED,
    ADC_STATUS_SATURATED,
    OBSSharedData,
)

from obs_ipc import OBSIPCServer


# =============================================================================
# Application constants
# =============================================================================

APP_TITLE = "OBS Setting"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"
APP_VERSION = "28"

BASE_DIR = Path(__file__).resolve().parent

ASSETS_DIR = BASE_DIR / "assets"
ICON_DIR = ASSETS_DIR / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

INI_PATH = BASE_DIR / "obs_settings.ini"

DEFAULT_IMU_YAW_OFFSET_DEG = 0.0

def wrap_angle_deg(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0

def load_imu_yaw_offset_deg() -> float:
    parser = configparser.ConfigParser()
    try:
        if INI_PATH.is_file():
            parser.read(INI_PATH, encoding="utf-8")
        value = parser.getfloat(
            "IMU",
            "yaw_offset_deg",
            fallback=DEFAULT_IMU_YAW_OFFSET_DEG,
        )
        if value == value and abs(value) != float("inf") and -360.0 <= value <= 360.0:
            return float(value)
    except Exception:
        pass
    return DEFAULT_IMU_YAW_OFFSET_DEG

def save_imu_yaw_offset_deg(value: float) -> None:
    value = float(value)
    if not (value == value and abs(value) != float("inf")):
        raise ValueError("Yaw offset must be finite.")
    if value < -360.0 or value > 360.0:
        raise ValueError("Yaw offset must be between -360 and +360 degrees.")
    parser = configparser.ConfigParser()
    if INI_PATH.is_file():
        parser.read(INI_PATH, encoding="utf-8")
    if not parser.has_section("IMU"):
        parser.add_section("IMU")
    parser.set("IMU", "yaw_offset_deg", f"{value:.6f}")
    with INI_PATH.open("w", encoding="utf-8") as handle:
        parser.write(handle)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LOG_PATH = LOG_DIR / "obs_setting.log"

DEFAULT_IP = "192.168.1.100"

# Actual OBS protocol ports.
DEFAULT_COMMAND_PORT = 54300
DEFAULT_DATA_PORT = 54301

# DATA TCP should carry continuous bulk frames.  If no complete valid OBS bulk
# frame is seen for this long after connection, treat the DATA stream as lost.
OBS_DATA_STALL_TIMEOUT_S = 3.0

# Corrected-firmware acquisition contract. The raw ADC is expected to run at
# 1000 Hz. This is not operator-editable. If a future firmware protocol reports
# an authoritative rate, that reported value can replace this constant.
EXPECTED_ADC_SOURCE_RATE_HZ = float(
    ADC_DESIGN_SAMPLE_RATE_HZ
)

# PC-side verification only. A regression over bulk-frame sequence span versus
# monotonic PC time reduces TCP scheduling jitter. The estimate never changes
# the authoritative waveform time-base dynamically.
ADC_RATE_DIAGNOSTIC_WINDOW_S = 300.0
ADC_RATE_DIAGNOSTIC_MIN_SPAN_S = 60.0
ADC_RATE_DIAGNOSTIC_PUBLISH_S = 5.0

# Decimation parameter = number of consecutive RAW ADC frames averaged
# into one output ADC frame.
DEFAULT_DECIMATION_SAMPLES = 5

# Derived result rate for the corrected-firmware acquisition clock.
DEFAULT_DECIMATION_RATE_HZ = (
    float(EXPECTED_ADC_SOURCE_RATE_HZ)
    / float(DEFAULT_DECIMATION_SAMPLES)
)

DEFAULT_GNSS_MODE = "COM Port"
DEFAULT_GNSS_COM = "COM3"
DEFAULT_GNSS_BAUD = 115200
DEFAULT_GNSS_UDP_IP = "0.0.0.0"
DEFAULT_GNSS_UDP_PORT = 10110

DEFAULT_USBL_MODE = "COM Port"
DEFAULT_USBL_COM = "COM4"
DEFAULT_USBL_BAUD = 115200

DEFAULT_USBL_UDP_IP = "0.0.0.0"
DEFAULT_USBL_UDP_PORT = 10110

# GNSS/USBL source freshness. Cerulean/NMEA is normally 1 Hz; three missed
# seconds marks the source stale without immediately reacting to one late packet.
NMEA_SOURCE_STALE_S = 3.0

DEFAULT_RECORD_FOLDER = str(
    BASE_DIR / "recordings"
)

OBS_COMMAND_MAX_SENTENCE_BYTES = 82

# Firmware-defined REMOTE GPIO CONTROL command codes.
RMCMD_PWR_MODE_HIGH = 1
RMCMD_LOCK_HIGH = 2
RMCMD_PWR_MODE_LOW = 5
RMCMD_LOCK_LOW = 6

RMCMD_ALLOWED_CODES = frozenset(
    (
        RMCMD_PWR_MODE_HIGH,
        RMCMD_LOCK_HIGH,
        RMCMD_PWR_MODE_LOW,
        RMCMD_LOCK_LOW,
    )
)

# Commands accepted from local consumer GUIs through obs_ipc.py.
# Camera: 50..60, MiniSEED onboard USB logger: 61..62.
RMCMD_IPC_ALLOWED_CODES = frozenset(range(50, 63))
OBS_STRG0_MAX_SENTENCE_BYTES = 192
OBS_TIME1_COUNTER = 0

BULK_MAGIC = b"OBS:"
BULK_HEADER_STRUCT = struct.Struct("<4sII")
BULK_HEADER_SIZE = BULK_HEADER_STRUCT.size
assert BULK_HEADER_SIZE == 12

CURRENT_BULK_PAYLOAD_BYTES = 2048
MAX_BULK_PAYLOAD_BYTES = 1024 * 1024

ADC_CHANNEL_COUNT = 4
ADC_WORD_BYTES = 4
ADC_FRAME_BYTES = (
    ADC_CHANNEL_COUNT
    * ADC_WORD_BYTES
)

ADC24_MIN = -(1 << 23)
ADC24_MAX = (1 << 23) - 1


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(threadName)s | "
        "%(message)s"
    ),
)

LOGGER = logging.getLogger(
    "obs_setting"
)


# =============================================================================
# Small helper functions
# =============================================================================

def application_icon() -> QIcon:
    candidates = []

    if os.name == "nt":
        candidates.append(
            APP_ICON_ICO
        )

    candidates.extend(
        [
            APP_ICON_PNG,
            APP_ICON_ICO,
        ]
    )

    for path in candidates:
        if path.is_file():
            icon = QIcon(
                str(path)
            )

            if not icon.isNull():
                return icon

    return QIcon()


def available_serial_ports() -> list[str]:
    """
    Enumerate serial ports when pyserial is installed.

    If pyserial is not installed, keep editable fallback names so the settings
    window itself remains usable.
    """

    try:
        from serial.tools import list_ports

        ports = sorted(
            item.device
            for item in list_ports.comports()
            if item.device
        )

        if ports:
            return ports

    except Exception:
        pass

    if os.name == "nt":
        return [
            f"COM{i}"
            for i in range(
                1,
                21,
            )
        ]

    return [
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
        "/dev/ttyACM0",
        "/dev/ttyACM1",
    ]


def nmea_xor_checksum(
    body: str,
) -> int:
    checksum = 0

    for byte_value in body.encode(
        "ascii",
        errors="strict",
    ):
        checksum ^= byte_value

    return checksum


def build_obs_sentence(
    body: str,
) -> bytes:
    """
    Convert a configured body such as:
        SOMEID,1,2

    into:
        $SOMEID,1,2*CC\r\n
    """

    body = str(
        body
    ).strip()

    if body.startswith("$"):
        body = body[1:]

    if "*" in body:
        body = body.split(
            "*",
            1,
        )[0]

    body = body.strip()

    if not body:
        raise ValueError(
            "OBS command body is empty."
        )

    checksum = nmea_xor_checksum(
        body
    )

    sentence = (
        f"${body}*{checksum:02X}\r\n"
    ).encode(
        "ascii",
        errors="strict",
    )

    if len(sentence) > OBS_COMMAND_MAX_SENTENCE_BYTES:
        raise ValueError(
            "OBS command exceeds the 82-byte protocol limit."
        )

    return sentence


def build_remote_gpio_sentence(
    command_code: int,
) -> bytes:
    """Build one firmware-defined $RMCMD remote GPIO sentence."""

    command_code = int(
        command_code
    )

    if command_code not in RMCMD_ALLOWED_CODES:
        raise ValueError(
            (
                "Unsupported RMCMD code "
                f"{command_code}. "
                "Allowed codes are 1, 2, 5, 6."
            )
        )

    return build_obs_sentence(
        f"RMCMD,{command_code}"
    )


def build_obs_time1_sentence(
    dt: Optional[datetime] = None,
    *,
    counter: int = OBS_TIME1_COUNTER,
) -> tuple[bytes, dict]:
    """Build TIME1 from the factual PC local time at the actual TCP-send instant."""
    if dt is None:
        dt = datetime.now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.astimezone()

    week = ((int(dt.weekday()) + 1) % 7) + 1  # Sunday=1 ... Saturday=7
    fields = {
        "milliseconds": int(dt.microsecond // 1000),
        "year": int(dt.year % 100),
        "month": int(dt.month),
        "week": int(week),
        "date": int(dt.day),
        "hours": int(dt.hour),
        "minutes": int(dt.minute),
        "seconds": int(dt.second),
        "counter": int(counter) & 0xFF,
        "pc_iso": dt.isoformat(timespec="milliseconds"),
    }
    body = (
        f"TIME1,{fields['milliseconds']},{fields['year']},{fields['month']},"
        f"{fields['week']},{fields['date']},{fields['hours']},"
        f"{fields['minutes']},{fields['seconds']},{fields['counter']}"
    )
    return build_obs_sentence(body), fields


def classify_strg0_message(message: str) -> tuple[str, str]:
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


def parse_obs_sentence(
    sentence: str,
) -> Optional[dict]:
    """
    Strict parser for the OBS command/telemetry TCP channel.

    Returns:
        {
            "id": "AHRS2",
            "fields": [...],
            "raw": "$AHRS2,...*CC",
            "timestamp_ns": ...
        }

    Invalid checksum/format returns None.
    """

    text = sentence.strip(
        "\r\n "
    )

    if not text.startswith("$"):
        return None

    try:
        encoded = text.encode(
            "ascii",
            errors="strict",
        )
    except UnicodeEncodeError:
        return None

    # STRG0 status strings can be longer than ordinary NMEA-style command frames.
    max_sentence_bytes = (
        OBS_STRG0_MAX_SENTENCE_BYTES
        if text.startswith("$STRG0,")
        else OBS_COMMAND_MAX_SENTENCE_BYTES
    )
    if len(encoded) + 2 > max_sentence_bytes:
        return None

    star_index = text.rfind("*")

    if star_index <= 1:
        return None

    body = text[
        1:star_index
    ]

    checksum_text = text[
        star_index + 1:
        star_index + 3
    ]

    if len(checksum_text) != 2:
        return None

    try:
        expected_checksum = int(
            checksum_text,
            16,
        )
    except ValueError:
        return None

    calculated_checksum = (
        nmea_xor_checksum(
            body
        )
    )

    if (
        calculated_checksum
        != expected_checksum
    ):
        return None

    parts = body.split(
        ","
    )

    if not parts:
        return None

    message_id = (
        parts[0]
        .strip()
        .upper()
    )

    if not message_id:
        return None

    return {
        "id": message_id,
        "fields": parts[1:],
        "raw": text,
        "timestamp_ns": time.time_ns(),
    }


def nmea_checksum_valid(
    sentence: str,
) -> bool:
    """
    NMEA validation for GNSS/USBL input.

    Some external GNSS/USBL units can be configured without a checksum.
    Those sentences are accepted. When *CC is present it must be valid.
    """

    text = sentence.strip()

    if not text.startswith("$"):
        return False

    if "*" not in text:
        return True

    body = text[
        1:
        text.rfind("*")
    ]

    checksum_text = text[
        text.rfind("*") + 1:
        text.rfind("*") + 3
    ]

    try:
        expected = int(
            checksum_text,
            16,
        )
    except ValueError:
        return False

    try:
        calculated = (
            nmea_xor_checksum(
                body
            )
        )
    except UnicodeEncodeError:
        return False

    return (
        calculated
        == expected
    )


def nmea_coordinate(
    value: str,
    hemisphere: str,
) -> float:
    raw = float(
        value
    )

    degrees = int(
        raw // 100
    )

    minutes = (
        raw
        - degrees * 100
    )

    decimal = (
        degrees
        + minutes / 60.0
    )

    hemisphere = (
        hemisphere
        .strip()
        .upper()
    )

    if hemisphere in (
        "S",
        "W",
    ):
        decimal = -decimal

    return decimal


def parse_nmea_gga(
    sentence: str,
) -> Optional[dict]:

    text = sentence.strip()

    if not nmea_checksum_valid(
        text
    ):
        return None

    payload = text.split(
        "*",
        1,
    )[0]

    fields = payload.split(
        ","
    )

    if len(fields) < 10:
        return None

    message_id = (
        fields[0]
        .lstrip("$")
        .upper()
    )

    if not message_id.endswith(
        "GGA"
    ):
        return None

    try:
        latitude = nmea_coordinate(
            fields[2],
            fields[3],
        )

        longitude = nmea_coordinate(
            fields[4],
            fields[5],
        )

        fix_quality = int(
            fields[6]
            or 0
        )

        satellites = int(
            fields[7]
            or 0
        )

        hdop = float(
            fields[8]
            or 0.0
        )

        altitude = float(
            fields[9]
            or 0.0
        )

    except (
        ValueError,
        TypeError,
        IndexError,
    ):
        return None

    return {
        "timestamp_ns": time.time_ns(),
        "utc": fields[1],
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "fix_quality": fix_quality,
        "satellites": satellites,
        "hdop": hdop,
        "valid": fix_quality > 0,
        "raw": text,
    }


def parse_int_auto(
    value: str,
) -> int:
    """
    Parse decimal, 0x-prefixed hex, or bare hex containing A-F.
    """

    text = str(
        value
    ).strip()

    if not text:
        return 0

    if text.lower().startswith(
        "0x"
    ):
        return int(
            text,
            16,
        )

    if any(
        char in "ABCDEFabcdef"
        for char in text
    ):
        return int(
            text,
            16,
        )

    return int(
        text,
        10,
    )


def decode_signed_adc24(
    word: int,
) -> int:
    raw = int(
        word
    ) & 0x00FFFFFF

    if raw & 0x00800000:
        raw -= 0x01000000

    return raw



# =============================================================================
# ADC block-average decimator
# =============================================================================

class ADCAveragingDecimator:
    """
    Continuous block-average decimator for the 4-channel OBS ADC stream.

    User parameter:
        averaging_samples = number of consecutive RAW ADC frames averaged
        into one OUTPUT frame.

    For averaging_samples = 5 with corrected firmware:
        raw 1000.000 Hz/channel -> output 200.000 Hz/channel.

    CH0 / CH1 / CH2 are Geophone N / E / Z.
    CH3 uses the same averaging window to keep each shared ADC frame aligned.

    The averaging state persists across normal 128-frame OBS payload
    boundaries. A real source-data gap or source restart resets only the
    incomplete averaging window, so samples on opposite sides of a gap are
    never averaged together.

    Output timestamps are the center timestamps of the raw averaging windows.
    """

    def __init__(
        self,
        averaging_samples: int,
        raw_sample_rate_hz: float,
    ):
        self.averaging_samples = max(
            1,
            int(
                averaging_samples
            ),
        )

        self.raw_sample_rate_hz = float(
            raw_sample_rate_hz
        )
        if not (
            math.isfinite(self.raw_sample_rate_hz)
            and self.raw_sample_rate_hz > 0.0
        ):
            raise ValueError(
                "raw_sample_rate_hz must be finite and > 0"
            )

        self.reset()

    @property
    def result_rate_hz(
        self,
    ) -> float:
        return (
            float(
                self.raw_sample_rate_hz
            )
            / float(
                self.averaging_samples
            )
        )

    def reset(
        self,
    ) -> None:
        self._sum = [
            0,
            0,
            0,
            0,
        ]

        self._status_or = [
            0,
            0,
            0,
            0,
        ]

        self._count = 0
        self._first_timestamp_ns = None

    def break_stream(
        self,
    ) -> None:
        """
        Discard an incomplete averaging window at a real acquisition gap.
        """
        self.reset()

    @staticmethod
    def _clamp_adc24(
        value: float,
    ) -> int:
        result = int(
            round(
                float(
                    value
                )
            )
        )

        return max(
            ADC24_MIN,
            min(
                ADC24_MAX,
                result,
            ),
        )

    def process(
        self,
        samples,
        statuses,
        raw_timestamps_ns,
    ):
        """
        Returns:
            output_samples
            output_statuses
            output_timestamps_ns
        """

        if not (
            len(samples)
            == len(statuses)
            == len(raw_timestamps_ns)
        ):
            raise ValueError(
                "samples/statuses/timestamps length mismatch"
            )

        output_samples = []
        output_statuses = []
        output_timestamps = []

        for (
            sample,
            status,
            timestamp_ns,
        ) in zip(
            samples,
            statuses,
            raw_timestamps_ns,
        ):
            if self._count == 0:
                self._first_timestamp_ns = int(
                    timestamp_ns
                )

            for channel_index in range(
                ADC_CHANNEL_COUNT
            ):
                self._sum[
                    channel_index
                ] += int(
                    sample[
                        channel_index
                    ]
                )

                # Preserve any diagnostic flag that appeared in the averaging
                # window. Channel-ID bits are rebuilt below.
                self._status_or[
                    channel_index
                ] |= (
                    int(
                        status[
                            channel_index
                        ]
                    )
                    & (
                        ~ADC_STATUS_CHANNEL_ID_MASK
                        & 0xFF
                    )
                )

            self._count += 1

            if (
                self._count
                < self.averaging_samples
            ):
                continue

            averaged = tuple(
                self._clamp_adc24(
                    self._sum[
                        channel_index
                    ]
                    / float(
                        self.averaging_samples
                    )
                )
                for channel_index in range(
                    ADC_CHANNEL_COUNT
                )
            )

            aggregated_status = tuple(
                (
                    self._status_or[
                        channel_index
                    ]
                    | (
                        channel_index
                        & ADC_STATUS_CHANNEL_ID_MASK
                    )
                )
                & 0xFF
                for channel_index in range(
                    ADC_CHANNEL_COUNT
                )
            )

            last_timestamp_ns = int(
                timestamp_ns
            )

            center_timestamp_ns = (
                int(
                    self._first_timestamp_ns
                )
                + last_timestamp_ns
            ) // 2

            output_samples.append(
                averaged
            )
            output_statuses.append(
                aggregated_status
            )
            output_timestamps.append(
                center_timestamp_ns
            )

            self.reset()

        return (
            output_samples,
            output_statuses,
            output_timestamps,
        )


# =============================================================================
# DATA TCP receiver
# =============================================================================

class BulkFrameStreamParser:
    """
    Stateful parser for the TCP bulk byte stream.

    Important TCP rule:
        one recv() call is NOT one OBS frame.

    recv() may contain:
    - part of one OBS frame,
    - exactly one OBS frame,
    - several OBS frames.

    Parsing therefore follows this exact state machine:

        recv()
          -> append to RX buffer
          -> find b"OBS:"
          -> wait for 12-byte header
          -> read payload_length
          -> wait for 12 + payload_length bytes
          -> extract exactly one frame
          -> leave remaining bytes in RX buffer
          -> repeat

    Current firmware payload length is strictly 2048 bytes, therefore a normal
    complete frame is 2060 bytes.
    """

    def __init__(
        self,
        *,
        expected_payload_length: int = CURRENT_BULK_PAYLOAD_BYTES,
    ):
        self.expected_payload_length = int(
            expected_payload_length
        )

        self.buffer = bytearray()

        self.discarded_bytes = 0
        self.resync_events = 0
        self.invalid_headers = 0

    def clear(
        self,
    ) -> None:

        self.buffer.clear()

    def feed(
        self,
        chunk: bytes,
    ) -> list[
        tuple[int, bytes]
    ]:
        """
        Append arbitrary TCP bytes and return every complete OBS frame that can
        currently be extracted.

        Returned tuples:
            (uint32 frame_sequence, payload_bytes)
        """

        if chunk:
            self.buffer.extend(
                chunk
            )

        complete_frames: list[
            tuple[int, bytes]
        ] = []

        while True:

            # -------------------------------------------------------------
            # 1. Find the OBS: magic.
            # -------------------------------------------------------------
            magic_index = self.buffer.find(
                BULK_MAGIC
            )

            if magic_index < 0:
                # No full magic is currently present. Keep only the final
                # three bytes because they may be the prefix of "OBS:" split
                # across the next recv().
                keep = min(
                    len(self.buffer),
                    len(BULK_MAGIC) - 1,
                )

                discard_count = (
                    len(self.buffer)
                    - keep
                )

                if discard_count > 0:
                    del self.buffer[
                        :discard_count
                    ]

                    self.discarded_bytes += (
                        discard_count
                    )

                    self.resync_events += 1

                break

            if magic_index > 0:
                # Remove only bytes before the first complete OBS: magic.
                del self.buffer[
                    :magic_index
                ]

                self.discarded_bytes += (
                    magic_index
                )

                self.resync_events += 1

            # -------------------------------------------------------------
            # 2. Do we already have the complete 12-byte header?
            # -------------------------------------------------------------
            if (
                len(self.buffer)
                < BULK_HEADER_SIZE
            ):
                break

            (
                magic,
                frame_sequence,
                payload_length,
            ) = BULK_HEADER_STRUCT.unpack_from(
                self.buffer,
                0,
            )

            if magic != BULK_MAGIC:
                # Defensive fallback. This should not normally happen because
                # the buffer was aligned above.
                del self.buffer[0]

                self.discarded_bytes += 1
                self.resync_events += 1
                continue

            # -------------------------------------------------------------
            # 3. Validate payload_length before trusting the frame size.
            # -------------------------------------------------------------
            if (
                payload_length
                != self.expected_payload_length
            ):
                # Current OBS firmware specifies a fixed 2048-byte payload.
                # If this header is corrupt, do not skip payload_length bytes
                # because that could discard valid following frames.
                # Advance one byte and search for OBS: again.
                del self.buffer[0]

                self.discarded_bytes += 1
                self.invalid_headers += 1
                self.resync_events += 1
                continue

            complete_length = (
                BULK_HEADER_SIZE
                + payload_length
            )

            # -------------------------------------------------------------
            # 4. Wait until the entire 2060-byte frame is available.
            # -------------------------------------------------------------
            if (
                len(self.buffer)
                < complete_length
            ):
                break

            # -------------------------------------------------------------
            # 5. Extract exactly one frame, leaving following TCP data intact.
            # -------------------------------------------------------------
            frame = bytes(
                self.buffer[
                    :complete_length
                ]
            )

            del self.buffer[
                :complete_length
            ]

            # Parse again from the isolated frame rather than relying on a
            # changing bytearray.
            (
                isolated_magic,
                isolated_sequence,
                isolated_payload_length,
            ) = BULK_HEADER_STRUCT.unpack_from(
                frame,
                0,
            )

            if (
                isolated_magic != BULK_MAGIC
                or isolated_payload_length
                != self.expected_payload_length
            ):
                self.invalid_headers += 1
                self.resync_events += 1
                continue

            payload = frame[
                BULK_HEADER_SIZE:
            ]

            if (
                len(payload)
                != self.expected_payload_length
            ):
                self.invalid_headers += 1
                continue

            complete_frames.append(
                (
                    int(
                        isolated_sequence
                    ),
                    payload,
                )
            )

        return complete_frames


class OBSDataTCPThread(QThread):
    """
    Robust OBS bulk binary receiver for TCP port 54301.

    The receiver never assumes recv() boundaries correspond to OBS frames.
    """

    connection_changed = Signal(
        bool,
        str,
    )

    socket_error = Signal(
        str
    )

    # measured raw ADC frame rate,
    # measured output/shared ADC frame rate,
    # dropped bulk frames,
    # channel-ID mismatches,
    # ADC ERROR words,
    # malformed/resync count
    stream_status = Signal(
        float,
        float,
        int,
        int,
        int,
        int,
    )

    def __init__(
        self,
        host: str,
        port: int,
        shared: OBSSharedData,
        decimation_samples: int,
        raw_sample_rate_hz: float,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.host = str(
            host
        )

        self.port = int(
            port
        )

        self.shared = shared

        self.decimation_samples = max(
            1,
            int(
                decimation_samples
            ),
        )

        self.raw_sample_rate_hz = float(
            raw_sample_rate_hz
        )
        if not (
            math.isfinite(self.raw_sample_rate_hz)
            and self.raw_sample_rate_hz > 0.0
        ):
            raise ValueError(
                "raw_sample_rate_hz must be finite and > 0"
            )

        self.decimation_mode = (
            "raw"
            if self.decimation_samples == 1
            else "mean"
        )

        self._decimator = (
            ADCAveragingDecimator(
                self.decimation_samples,
                self.raw_sample_rate_hz,
            )
        )

        self._raw_interval_ns = int(
            round(
                1_000_000_000.0
                / float(
                    self.raw_sample_rate_hz
                )
            )
        )

        self._last_raw_timestamp_ns = None

        self._stop_event = (
            threading.Event()
        )

        self._socket: Optional[
            socket.socket
        ] = None

        self._last_frame_sequence: Optional[
            int
        ] = None

        self._rate_window_start = (
            time.perf_counter()
        )

        self._rate_window_adc_frames = 0
        self._rate_window_output_frames = 0

        # Long-window clock-rate verification. Points are (PC monotonic time,
        # source raw-sample index). Sequence gaps advance the source index, so
        # a dropped TCP bulk frame does not masquerade as a slower ADC clock.
        self._adc_rate_points = deque(maxlen=4096)
        self._adc_rate_source_index = 0
        self._adc_rate_last_publish_monotonic = 0.0

        self._parser = (
            BulkFrameStreamParser(
                expected_payload_length=(
                    CURRENT_BULK_PAYLOAD_BYTES
                )
            )
        )

        self._reported_parser_errors = 0

    def stop(
        self,
    ) -> None:

        self._stop_event.set()

        sock = self._socket

        if sock is not None:
            try:
                sock.shutdown(
                    socket.SHUT_RDWR
                )
            except Exception:
                pass

            try:
                sock.close()
            except Exception:
                pass

    def _frame_sequence_delta(
        self,
        current: int,
    ) -> tuple[
        int,
        int,
    ]:
        """
        Returns:
            (dropped_bulk_frames, sequence_reset_count)
        """

        current = (
            int(current)
            & 0xFFFFFFFF
        )

        previous = (
            self._last_frame_sequence
        )

        self._last_frame_sequence = (
            current
        )

        if previous is None:
            return 0, 0

        expected = (
            previous + 1
        ) & 0xFFFFFFFF

        if current == expected:
            return 0, 0

        delta = (
            current - expected
        ) & 0xFFFFFFFF

        # A small forward jump is a real missing-frame count.
        # A backward/very large jump is treated as OBS restart/reset.
        if (
            0 < delta < 1_000_000
        ):
            return int(delta), 0

        return 0, 1

    def _update_adc_rate_diagnostic(
        self,
        *,
        dropped_frames: int,
        sequence_reset: bool,
        frame_sample_count: int,
    ) -> None:
        now = time.perf_counter()
        frame_sample_count = max(1, int(frame_sample_count))

        if sequence_reset or not self._adc_rate_points:
            self._adc_rate_points.clear()
            self._adc_rate_source_index = 0
            self._adc_rate_last_publish_monotonic = 0.0
            self._adc_rate_points.append((now, 0))
            return

        # Current block is one source block after the previous block plus any
        # sequence-declared missing blocks. Use source span, not RX count.
        source_blocks_advanced = max(1, int(dropped_frames) + 1)
        self._adc_rate_source_index += (
            source_blocks_advanced * frame_sample_count
        )
        self._adc_rate_points.append(
            (now, int(self._adc_rate_source_index))
        )

        cutoff = now - float(ADC_RATE_DIAGNOSTIC_WINDOW_S)
        while (
            len(self._adc_rate_points) > 2
            and self._adc_rate_points[0][0] < cutoff
        ):
            self._adc_rate_points.popleft()

        if len(self._adc_rate_points) < 20:
            return

        first_t = float(self._adc_rate_points[0][0])
        last_t = float(self._adc_rate_points[-1][0])
        span_s = last_t - first_t
        if span_s < float(ADC_RATE_DIAGNOSTIC_MIN_SPAN_S):
            return

        if (
            self._adc_rate_last_publish_monotonic > 0.0
            and now - self._adc_rate_last_publish_monotonic
            < float(ADC_RATE_DIAGNOSTIC_PUBLISH_S)
        ):
            return

        # Least-squares slope across all block arrivals. This is materially more
        # stable than one-packet timing and remains a diagnostic, not a clock
        # source for waveform metadata.
        x_values = [
            float(t) - first_t
            for t, _index in self._adc_rate_points
        ]
        base_index = int(self._adc_rate_points[0][1])
        y_values = [
            float(index - base_index)
            for _t, index in self._adc_rate_points
        ]
        count = float(len(x_values))
        mean_x = sum(x_values) / count
        mean_y = sum(y_values) / count
        variance_x = sum(
            (x - mean_x) ** 2 for x in x_values
        )
        if variance_x <= 0.0:
            return
        covariance_xy = sum(
            (x - mean_x) * (y - mean_y)
            for x, y in zip(x_values, y_values)
        )
        measured_raw_hz = covariance_xy / variance_x
        if not (math.isfinite(measured_raw_hz) and measured_raw_hz > 0.0):
            return

        try:
            self.shared.update_adc_rate_diagnostic(
                measured_raw_sample_rate_hz=float(measured_raw_hz),
                expected_raw_sample_rate_hz=float(self.raw_sample_rate_hz),
                measured_output_sample_rate_hz=(
                    float(measured_raw_hz)
                    / float(max(1, self.decimation_samples))
                ),
                measurement_window_s=float(span_s),
                source_sample_span=max(
                    0,
                    int(self._adc_rate_points[-1][1]) - base_index,
                ),
            )
            self._adc_rate_last_publish_monotonic = now
        except Exception:
            # Rate verification must never interrupt ADC acquisition.
            pass

    @staticmethod
    def _decode_payload(
        payload: bytes,
    ) -> tuple[
        list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ],
        list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ],
        int,
        int,
        int,
        int,
        int,
    ]:
        """
        Parse the fixed 2048-byte payload.

        Returns:
            samples
            statuses
            channel_mismatches
            error_words
            unsettled_words
            repeated_words
            saturated_words
        """

        if (
            len(payload)
            != CURRENT_BULK_PAYLOAD_BYTES
        ):
            raise ValueError(
                (
                    "Bulk payload must be exactly "
                    f"{CURRENT_BULK_PAYLOAD_BYTES} bytes, "
                    f"got {len(payload)}."
                )
            )

        word_count = (
            len(payload)
            // ADC_WORD_BYTES
        )

        if word_count != 512:
            raise ValueError(
                (
                    "Expected 512 uint32 ADC words, "
                    f"got {word_count}."
                )
            )

        words = struct.unpack(
            "<512I",
            payload,
        )

        adc_frame_count = (
            word_count
            // ADC_CHANNEL_COUNT
        )

        if adc_frame_count != 128:
            raise ValueError(
                (
                    "Expected 128 ADC frames, "
                    f"got {adc_frame_count}."
                )
            )

        samples: list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ] = []

        statuses: list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ] = []

        channel_mismatches = 0
        error_words = 0
        unsettled_words = 0
        repeated_words = 0
        saturated_words = 0

        # The payload order is authoritative:
        # CH0, CH1, CH2, CH3, CH0, CH1, ...
        for adc_frame_index in range(
            128
        ):
            base_word = (
                adc_frame_index
                * ADC_CHANNEL_COUNT
            )

            frame_values = [
                0,
                0,
                0,
                0,
            ]

            frame_status = [
                0,
                0,
                0,
                0,
            ]

            for channel_index in range(
                ADC_CHANNEL_COUNT
            ):
                word = words[
                    base_word
                    + channel_index
                ]

                status = (
                    word >> 24
                ) & 0xFF

                raw24 = (
                    word
                    & 0x00FFFFFF
                )

                if (
                    raw24
                    & 0x00800000
                ):
                    raw24 -= (
                        0x01000000
                    )

                frame_values[
                    channel_index
                ] = int(
                    raw24
                )

                frame_status[
                    channel_index
                ] = int(
                    status
                )

                reported_channel = (
                    status
                    & ADC_STATUS_CHANNEL_ID_MASK
                )

                if (
                    reported_channel
                    != channel_index
                ):
                    channel_mismatches += 1

                if (
                    status
                    & ADC_STATUS_ERROR
                ):
                    error_words += 1

                if (
                    status
                    & ADC_STATUS_FILTER_NOT_SETTLED
                ):
                    unsettled_words += 1

                if (
                    status
                    & ADC_STATUS_REPEATED
                ):
                    repeated_words += 1

                if (
                    status
                    & ADC_STATUS_SATURATED
                ):
                    saturated_words += 1

            samples.append(
                (
                    frame_values[0],
                    frame_values[1],
                    frame_values[2],
                    frame_values[3],
                )
            )

            statuses.append(
                (
                    frame_status[0],
                    frame_status[1],
                    frame_status[2],
                    frame_status[3],
                )
            )

        return (
            samples,
            statuses,
            channel_mismatches,
            error_words,
            unsettled_words,
            repeated_words,
            saturated_words,
        )

    def _make_raw_timestamps(
        self,
        *,
        frame_count: int,
        receive_timestamp_ns: int,
        missing_raw_frames_before: int = 0,
    ) -> list[int]:
        """
        Create source-time timestamps for one decoded raw ADC block.

        First block:
            anchor final raw frame to receive_timestamp_ns.

        Following blocks:
            continue from the previous RAW ADC timestamp using the configured
            acquisition/source sample period. Real dropped source frames create
            a real timestamp gap.
        """

        frame_count = max(
            0,
            int(
                frame_count
            ),
        )

        if frame_count <= 0:
            return []

        missing_raw_frames_before = max(
            0,
            int(
                missing_raw_frames_before
            ),
        )

        if self._last_raw_timestamp_ns is None:
            first_timestamp_ns = (
                int(
                    receive_timestamp_ns
                )
                - (
                    frame_count - 1
                )
                * self._raw_interval_ns
            )
        else:
            first_timestamp_ns = (
                int(
                    self._last_raw_timestamp_ns
                )
                + (
                    missing_raw_frames_before + 1
                )
                * self._raw_interval_ns
            )

        timestamps = [
            (
                first_timestamp_ns
                + index
                * self._raw_interval_ns
            )
            for index in range(
                frame_count
            )
        ]

        self._last_raw_timestamp_ns = (
            timestamps[
                -1
            ]
        )

        return timestamps

    def _process_bulk_frame(
        self,
        frame_sequence: int,
        payload: bytes,
        receive_timestamp_ns: int,
    ) -> None:

        try:
            (
                samples,
                statuses,
                channel_mismatches,
                error_words,
                unsettled_words,
                repeated_words,
                saturated_words,
            ) = self._decode_payload(
                payload
            )

        except (
            ValueError,
            struct.error,
        ):
            self.shared.update_bulk_status(
                frame_sequence=frame_sequence,
                payload_length=len(
                    payload
                ),
                malformed_frames_add=1,
                timestamp_ns=receive_timestamp_ns,
            )
            return

        dropped_frames, sequence_resets = (
            self._frame_sequence_delta(
                frame_sequence
            )
        )

        self._update_adc_rate_diagnostic(
            dropped_frames=int(dropped_frames),
            sequence_reset=bool(sequence_resets),
            frame_sample_count=len(samples),
        )

        if sequence_resets:
            # New/restarted source sequence: do not mix the old acquisition
            # session, raw clock or incomplete averaging window with the new
            # source stream.
            self.shared.start_new_adc_session(
                reset_bulk_status=False,
                raw_sample_rate_hz=(
                    self.raw_sample_rate_hz
                ),
                decimation_samples=(
                    self.decimation_samples
                ),
                decimation_mode=(
                    self.decimation_mode
                ),
            )

            self._last_raw_timestamp_ns = None
            self._decimator.reset()

        missing_adc_frames = (
            dropped_frames
            * 128
        )

        if (
            missing_adc_frames > 0
            and not sequence_resets
        ):
            # Never average samples across a genuine raw-stream gap.
            self._decimator.break_stream()

        raw_timestamps_ns = (
            self._make_raw_timestamps(
                frame_count=len(
                    samples
                ),
                receive_timestamp_ns=(
                    receive_timestamp_ns
                ),
                missing_raw_frames_before=(
                    missing_adc_frames
                ),
            )
        )

        (
            output_samples,
            output_statuses,
            output_timestamps_ns,
        ) = self._decimator.process(
            samples,
            statuses,
            raw_timestamps_ns,
        )

        if output_samples:
            # shared_data stores the processed/effective stream directly.
            # No D-1 fake missing samples are inserted. Explicit timestamps are
            # the centers of the raw averaging windows.
            self.shared.write_adc_stream_block(
                output_samples,
                statuses=(
                    output_statuses
                ),
                timestamps_ns=(
                    output_timestamps_ns
                ),
            )

        health_updates = {
            "last_data_rx_ns": int(receive_timestamp_ns),
        }
        if output_samples and output_timestamps_ns:
            health_updates["last_adc_ns"] = int(output_timestamps_ns[-1])
        try:
            self.shared.update_acquisition_health(**health_updates)
        except Exception:
            pass

        self.shared.update_bulk_status(
            frame_sequence=frame_sequence,
            payload_length=len(
                payload
            ),
            frames_received_add=1,
            dropped_frames_add=dropped_frames,
            sequence_resets_add=sequence_resets,
            channel_id_mismatches_add=channel_mismatches,
            error_flag_words_add=error_words,
            filter_not_settled_words_add=unsettled_words,
            repeated_words_add=repeated_words,
            saturated_words_add=saturated_words,
            timestamp_ns=receive_timestamp_ns,
        )

        self._rate_window_adc_frames += (
            len(samples)
        )

        self._rate_window_output_frames += (
            len(
                output_samples
            )
        )

        now = (
            time.perf_counter()
        )

        elapsed = (
            now
            - self._rate_window_start
        )

        if elapsed >= 0.5:
            raw_rate_hz = (
                self._rate_window_adc_frames
                / elapsed
            )

            output_rate_hz = (
                self._rate_window_output_frames
                / elapsed
            )

            bulk = (
                self.shared.read_bulk_status()
            )

            parser_errors = (
                self._parser.resync_events
                + self._parser.invalid_headers
            )

            self.stream_status.emit(
                float(
                    raw_rate_hz
                ),
                float(
                    output_rate_hz
                ),
                int(
                    bulk.dropped_frames
                ),
                int(
                    bulk.channel_id_mismatches
                ),
                int(
                    bulk.error_flag_words
                ),
                int(
                    parser_errors
                ),
            )

            self._rate_window_start = now
            self._rate_window_adc_frames = 0
            self._rate_window_output_frames = 0

    def run(
        self,
    ) -> None:

        sock: Optional[
            socket.socket
        ] = None

        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            )

            self._socket = sock

            sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )

            # A larger receive buffer reduces avoidable pressure while still
            # relying entirely on the explicit application RX bytearray.
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_RCVBUF,
                    256 * 1024,
                )
            except OSError:
                pass

            sock.settimeout(
                3.0
            )

            sock.connect(
                (
                    self.host,
                    self.port,
                )
            )

            sock.settimeout(
                0.5
            )

            self._parser.clear()
            self._last_frame_sequence = None
            self._rate_window_start = (
                time.perf_counter()
            )
            self._rate_window_adc_frames = 0
            self._rate_window_output_frames = 0
            self._adc_rate_points.clear()
            self._adc_rate_source_index = 0
            self._adc_rate_last_publish_monotonic = 0.0
            self._last_raw_timestamp_ns = None
            self._decimator.reset()

            self.shared.update_telemetry(
                data_connected=True
            )

            self.connection_changed.emit(
                True,
                f"{self.host}:{self.port}",
            )

            LOGGER.info(
                (
                    "Bulk DATA connected to %s:%d; "
                    "framing=OBS:+uint32 seq+uint32 len+2048 payload; "
                    "ADC average N=%d; effective nominal rate=%.3f Hz"
                ),
                self.host,
                self.port,
                self.decimation_samples,
                self._decimator.result_rate_hz,
            )

            # v22 watchdog uses complete valid OBS frames, not merely TCP bytes.
            # A connected socket carrying no valid bulk frames is not healthy DATA.
            last_valid_frame_monotonic = time.monotonic()

            while not self._stop_event.is_set():

                try:
                    chunk = sock.recv(
                        64 * 1024
                    )

                except socket.timeout:
                    if (
                        time.monotonic() - last_valid_frame_monotonic
                        >= OBS_DATA_STALL_TIMEOUT_S
                    ):
                        raise TimeoutError(
                            "No valid OBS bulk DATA frame received for "
                            f"{OBS_DATA_STALL_TIMEOUT_S:.1f} s."
                        )
                    continue

                if not chunk:
                    raise ConnectionResetError(
                        "OBS closed bulk DATA connection."
                    )

                # ---------------------------------------------------------
                # The parser owns all framing state.
                #
                # recv()
                #   -> append RX buffer
                #   -> find OBS:
                #   -> wait header
                #   -> read payload_length
                #   -> wait 12 + payload_length
                #   -> extract frame
                #   -> leave remaining bytes
                # ---------------------------------------------------------
                resync_before = (
                    self._parser.resync_events
                )

                frames = self._parser.feed(
                    chunk
                )

                if frames:
                    last_valid_frame_monotonic = time.monotonic()
                elif (
                    time.monotonic() - last_valid_frame_monotonic
                    >= OBS_DATA_STALL_TIMEOUT_S
                ):
                    raise TimeoutError(
                        "No valid OBS bulk DATA frame received for "
                        f"{OBS_DATA_STALL_TIMEOUT_S:.1f} s."
                    )

                resync_after = (
                    self._parser.resync_events
                )

                new_resync_events = (
                    resync_after
                    - resync_before
                )

                if new_resync_events > 0:
                    self.shared.update_bulk_status(
                        malformed_frames_add=(
                            new_resync_events
                        )
                    )

                # One recv() may yield zero, one, or many full OBS frames.
                for (
                    frame_sequence,
                    payload,
                ) in frames:

                    self._process_bulk_frame(
                        frame_sequence=frame_sequence,
                        payload=payload,
                        receive_timestamp_ns=(
                            time.time_ns()
                        ),
                    )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "Bulk DATA error: %s",
                message,
            )

            if not self._stop_event.is_set():
                self.socket_error.emit(
                    message
                )

        finally:
            self.shared.update_telemetry(
                data_connected=False
            )

            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

            self._socket = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )

            LOGGER.info(
                (
                    "Bulk DATA disconnected; "
                    "parser discarded=%d bytes, "
                    "resync=%d, invalid_header=%d"
                ),
                self._parser.discarded_bytes,
                self._parser.resync_events,
                self._parser.invalid_headers,
            )


# =============================================================================
# Command / telemetry TCP receiver
# =============================================================================

class OBSCommandTCPThread(QThread):

    connection_changed = Signal(bool, str)
    sentence_received = Signal(object)
    sentence_sent = Signal(object)
    socket_error = Signal(str)
    protocol_error = Signal(str)

    def __init__(self, host: str, port: int, shared: OBSSharedData, parent=None):
        super().__init__(parent)
        self.host = str(host)
        self.port = int(port)
        self.shared = shared
        self._stop_event = threading.Event()
        self._send_queue: queue.Queue[object] = queue.Queue()
        self._socket: Optional[socket.socket] = None

    def send_sentence(self, payload: bytes, metadata: Optional[dict] = None) -> None:
        self._send_queue.put({
            "kind": "bytes",
            "payload": bytes(payload),
            "metadata": dict(metadata or {}),
        })

    def send_time1(self, metadata: Optional[dict] = None) -> None:
        # TIME1 is deliberately generated in run() immediately before sendall().
        self._send_queue.put({
            "kind": "time1",
            "metadata": dict(metadata or {}),
        })

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _next_tx(self):
        item = self._send_queue.get_nowait()
        if isinstance(item, (bytes, bytearray)):
            return bytes(item), {}, None
        if not isinstance(item, dict):
            raise ValueError("Invalid command queue item")
        kind = str(item.get("kind", "bytes"))
        metadata = dict(item.get("metadata") or {})
        if kind == "time1":
            payload, fields = build_obs_time1_sentence()
            return payload, metadata, fields
        payload = bytes(item.get("payload") or b"")
        if not payload:
            raise ValueError("Empty OBS command payload")
        return payload, metadata, None

    def run(self) -> None:
        sock: Optional[socket.socket] = None
        buffer = bytearray()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket = sock
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(3.0)
            sock.connect((self.host, self.port))
            sock.settimeout(0.20)

            self.shared.update_telemetry(command_connected=True)
            self.connection_changed.emit(True, f"{self.host}:{self.port}")
            LOGGER.info("COMMAND connected to %s:%d", self.host, self.port)

            while not self._stop_event.is_set():
                while not self._send_queue.empty():
                    try:
                        payload, metadata, time_fields = self._next_tx()
                    except queue.Empty:
                        break

                    sock.sendall(payload)
                    text = payload.decode("ascii", errors="replace").strip()
                    LOGGER.info("COMMAND TX: %r", payload)
                    self.sentence_sent.emit({
                        "sentence": text,
                        "metadata": metadata,
                        "time_fields": time_fields,
                        "endpoint": f"{self.host}:{self.port}",
                        "timestamp_ns": time.time_ns(),
                    })

                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue

                if not chunk:
                    raise ConnectionResetError("OBS closed COMMAND connection.")

                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw_line, _, remaining = buffer.partition(b"\n")
                    buffer = bytearray(remaining)
                    raw_line = raw_line.rstrip(b"\r")
                    if not raw_line:
                        continue
                    try:
                        line = raw_line.decode("ascii", errors="strict")
                    except UnicodeDecodeError:
                        self.protocol_error.emit("Non-ASCII command-channel sentence.")
                        continue

                    parsed = parse_obs_sentence(line)
                    if parsed is None:
                        self.protocol_error.emit(
                            "Invalid command-channel sentence/checksum."
                        )
                        continue
                    self.sentence_received.emit(parsed)

                # STRG0 is allowed to be longer than ordinary command frames.
                if len(buffer) > 1024:
                    buffer.clear()
                    self.protocol_error.emit(
                        "Command-channel line exceeded framing limit."
                    )

        except Exception as exc:
            message = str(exc)
            LOGGER.warning("COMMAND error: %s", message)
            if not self._stop_event.is_set():
                self.socket_error.emit(message)
        finally:
            self.shared.update_telemetry(command_connected=False)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            self._socket = None
            self.connection_changed.emit(False, "Not connected")
            LOGGER.info("COMMAND disconnected")


# =============================================================================
# GNSS / USBL receiver threads
# =============================================================================

class SerialNMEAThread(QThread):

    connection_changed = Signal(
        bool,
        str,
    )

    gga_received = Signal(
        object
    )

    sentence_received = Signal(
        object
    )

    io_error = Signal(
        str
    )

    def __init__(
        self,
        device_name: str,
        port: str,
        baudrate: int,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.device_name = str(
            device_name
        )

        self.port = str(
            port
        )

        self.baudrate = int(
            baudrate
        )

        self._stop_event = (
            threading.Event()
        )

        self._serial = None

    def stop(self) -> None:
        self._stop_event.set()

        serial_obj = (
            self._serial
        )

        if serial_obj is not None:
            try:
                serial_obj.close()
            except Exception:
                pass

    def run(self) -> None:
        serial_obj = None

        try:
            try:
                import serial

            except ImportError as exc:
                raise RuntimeError(
                    "pyserial is not installed. "
                    "Run: pip install pyserial"
                ) from exc

            serial_obj = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.5,
            )

            self._serial = (
                serial_obj
            )

            self.connection_changed.emit(
                True,
                (
                    f"{self.port} "
                    f"@ {self.baudrate}"
                ),
            )

            LOGGER.info(
                "%s serial connected: %s @ %d",
                self.device_name,
                self.port,
                self.baudrate,
            )

            while not self._stop_event.is_set():

                raw = serial_obj.readline()

                if not raw:
                    continue

                line = raw.decode(
                    "ascii",
                    errors="ignore",
                ).strip()

                if not line:
                    continue

                self.sentence_received.emit(
                    {
                        "timestamp_ns": time.time_ns(),
                        "raw": line,
                    }
                )

                gga = parse_nmea_gga(
                    line
                )

                if gga is not None:
                    self.gga_received.emit(
                        gga
                    )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "%s serial error: %s",
                self.device_name,
                message,
            )

            if not self._stop_event.is_set():
                self.io_error.emit(
                    message
                )

        finally:
            if serial_obj is not None:
                try:
                    serial_obj.close()
                except Exception:
                    pass

            self._serial = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )


class UDPNMEAThread(QThread):

    connection_changed = Signal(
        bool,
        str,
    )

    gga_received = Signal(
        object
    )

    sentence_received = Signal(
        object
    )

    io_error = Signal(
        str
    )

    def __init__(
        self,
        device_name: str,
        listen_ip: str,
        port: int,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.device_name = str(
            device_name
        )

        self.listen_ip = str(
            listen_ip
        )

        self.port = int(
            port
        )

        self._stop_event = (
            threading.Event()
        )

        self._socket: Optional[
            socket.socket
        ] = None

    def stop(self) -> None:
        self._stop_event.set()

        sock = self._socket

        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def run(self) -> None:
        sock: Optional[
            socket.socket
        ] = None

        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
            )

            self._socket = sock

            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )

            sock.bind(
                (
                    self.listen_ip,
                    self.port,
                )
            )

            sock.settimeout(
                0.5
            )

            self.connection_changed.emit(
                True,
                (
                    f"{self.listen_ip}:"
                    f"{self.port}"
                ),
            )

            LOGGER.info(
                "%s UDP listening on %s:%d",
                self.device_name,
                self.listen_ip,
                self.port,
            )

            while not self._stop_event.is_set():

                try:
                    payload, _sender = (
                        sock.recvfrom(
                            8192
                        )
                    )

                except socket.timeout:
                    continue

                text = payload.decode(
                    "ascii",
                    errors="ignore",
                )

                for line in (
                    text
                    .replace(
                        "\r",
                        "\n",
                    )
                    .split(
                        "\n"
                    )
                ):
                    line = line.strip()

                    if not line:
                        continue

                    self.sentence_received.emit(
                        {
                            "timestamp_ns": time.time_ns(),
                            "raw": line,
                        }
                    )

                    gga = parse_nmea_gga(
                        line
                    )

                    if gga is not None:
                        self.gga_received.emit(
                            gga
                        )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "%s UDP error: %s",
                self.device_name,
                message,
            )

            if not self._stop_event.is_set():
                self.io_error.emit(
                    message
                )

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

            self._socket = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )


# =============================================================================
# Reliable spin box
# =============================================================================

class ReliableSpinBox(QSpinBox):
    """QSpinBox with deterministic mouse hit-zones for UP/DOWN arrows.

    On some Windows/PySide6 style combinations the native QSpinBox can draw
    both arrow glyphs correctly while the UP-button mouse hit area does not
    behave reliably after applying an application stylesheet.  Keep all normal
    QSpinBox behavior, but make the right-side button band deterministic:

        upper half -> stepUp()
        lower half -> stepDown()

    The native implementation is still used everywhere outside that band.
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

            # Make the mouse target intentionally wider than the tiny native
            # arrow glyph.  This improves operation on high-DPI displays too.
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


# =============================================================================
# Status indicator
# =============================================================================

class StatusPill(QLabel):

    def __init__(
        self,
        text: str = "",
        parent=None,
    ):
        super().__init__(
            text,
            parent,
        )

        self.setObjectName(
            "statusPill"
        )

        self.setAlignment(
            Qt.AlignCenter
        )

        self.setMinimumHeight(
            28
        )

    def set_state(
        self,
        text: str,
        state: str,
    ) -> None:

        state = (
            state
            .strip()
            .lower()
        )

        if state == "good":
            bg = "#123A2D"
            border = "#2D8E66"
            fg = "#A9F1D2"

        elif state == "warning":
            bg = "#403510"
            border = "#A88821"
            fg = "#FFE49A"

        elif state == "active":
            bg = "#102E42"
            border = "#2C86B8"
            fg = "#A8DDF7"

        else:
            bg = "#3A1D20"
            border = "#91424A"
            fg = "#FFBEC4"

        self.setText(
            text
        )

        self.setStyleSheet(
            f"""
            QLabel#statusPill {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
                color: {fg};
                font-weight: 700;
                padding: 3px 8px;
            }}
            """
        )


# =============================================================================
# Proportional unexpected-disconnect dialog
# =============================================================================

class OBSDisconnectDialog(QDialog):
    """Compact operator dialog for an unexpected OBS connection loss."""

    def __init__(self, *, parent, data_port, command_port, data_state,
                 command_state, cause, disconnected_at, details_text):
        super().__init__(parent)
        self.reconnect_requested = False
        self._details_visible = False
        self.setWindowTitle("OBS Connection Lost")
        self.setModal(True)
        self.setMinimumSize(500, 380)
        self.resize(520, 400)

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(14)
        icon_label = QLabel()
        warning_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        icon_label.setPixmap(warning_icon.pixmap(52, 52))
        icon_label.setFixedSize(58, 58)
        icon_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        header.addWidget(icon_label, 0, Qt.AlignTop)

        header_text = QVBoxLayout()
        header_text.setSpacing(4)
        title = QLabel("OBS CONNECTION LOST")
        title.setObjectName("disconnectDialogTitle")
        header_text.addWidget(title)
        subtitle = QLabel("Connection to OBS was interrupted.")
        subtitle.setObjectName("disconnectDialogSubtitle")
        subtitle.setWordWrap(True)
        header_text.addWidget(subtitle)
        header.addLayout(header_text, 1)
        root.addLayout(header)

        status_frame = QFrame()
        status_frame.setObjectName("disconnectStatusCard")
        status_grid = QGridLayout(status_frame)
        status_grid.setContentsMargins(14, 12, 14, 12)
        status_grid.setHorizontalSpacing(12)
        status_grid.setVerticalSpacing(8)

        rows = (
            (f"DATA  (TCP {int(data_port)})", str(data_state)),
            (f"COMMAND  (TCP {int(command_port)})", str(command_state)),
        )
        for row, (name, state) in enumerate(rows):
            name_label = QLabel(name)
            name_label.setObjectName("disconnectStatusName")
            value_label = QLabel(state)
            value_label.setObjectName(
                "disconnectStatusBad" if state.upper() == "DISCONNECTED"
                else "disconnectStatusGood"
            )
            status_grid.addWidget(name_label, row, 0)
            status_grid.addWidget(value_label, row, 1, Qt.AlignRight)
        status_grid.setColumnStretch(0, 1)
        root.addWidget(status_frame)

        cause_label = QLabel(str(cause))
        cause_label.setObjectName("disconnectCause")
        cause_label.setWordWrap(True)
        root.addWidget(cause_label)

        time_label = QLabel(f"Disconnected at  {disconnected_at}")
        time_label.setObjectName("disconnectTime")
        root.addWidget(time_label)

        self.details_box = QPlainTextEdit()
        self.details_box.setObjectName("disconnectDetails")
        self.details_box.setReadOnly(True)
        self.details_box.setPlainText(str(details_text))
        self.details_box.setMinimumHeight(125)
        self.details_box.setMaximumHeight(165)
        self.details_box.hide()
        root.addWidget(self.details_box)
        root.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        self.details_button = QPushButton("Show Details")
        self.details_button.setObjectName("disconnectSecondaryButton")
        self.details_button.clicked.connect(self._toggle_details)
        dismiss_button = QPushButton("Dismiss")
        dismiss_button.setObjectName("disconnectSecondaryButton")
        dismiss_button.clicked.connect(self.reject)
        reconnect_button = QPushButton("Reconnect OBS")
        reconnect_button.setObjectName("disconnectPrimaryButton")
        reconnect_button.clicked.connect(self._accept_reconnect)
        reconnect_button.setDefault(True)
        buttons.addWidget(self.details_button)
        buttons.addStretch(1)
        buttons.addWidget(dismiss_button)
        buttons.addWidget(reconnect_button)
        root.addLayout(buttons)

        self.setStyleSheet("""
            QDialog { background-color:#0A1C28; color:#FFFFFF; font-family:'Segoe UI','Arial'; }
            QLabel { background:transparent; }
            QLabel#disconnectDialogTitle { color:#FFFFFF; font-size:18px; font-weight:800; }
            QLabel#disconnectDialogSubtitle { color:#B9CBD5; font-size:11px; }
            QFrame#disconnectStatusCard { background-color:#0D2635; border:1px solid #23495F; border-radius:9px; }
            QLabel#disconnectStatusName { color:#D5E4EC; font-size:11px; font-weight:600; }
            QLabel#disconnectStatusBad { color:#FFB8BE; background-color:#3A2027; border:1px solid #7A4049; border-radius:7px; padding:4px 8px; font-weight:800; }
            QLabel#disconnectStatusGood { color:#A9F1D2; background-color:#123A2D; border:1px solid #2D8E66; border-radius:7px; padding:4px 8px; font-weight:800; }
            QLabel#disconnectCause { color:#F2F6F8; font-size:11px; font-weight:600; padding:2px 1px; }
            QLabel#disconnectTime { color:#8FAAB9; font-size:10px; }
            QPlainTextEdit#disconnectDetails { background-color:#06141D; color:#C7D8E1; border:1px solid #24485D; border-radius:7px; font-family:'Cascadia Mono','Consolas'; font-size:9px; padding:7px; }
            QPushButton { min-height:32px; border-radius:7px; padding:4px 12px; font-weight:700; }
            QPushButton#disconnectPrimaryButton { background-color:#17678F; color:#FFFFFF; border:1px solid #2D8AB6; min-width:120px; }
            QPushButton#disconnectPrimaryButton:hover { background-color:#1D78A4; }
            QPushButton#disconnectSecondaryButton { background-color:#132A39; color:#EAF4F8; border:1px solid #2B586F; min-width:96px; }
            QPushButton#disconnectSecondaryButton:hover { background-color:#193B4E; border-color:#3C7898; }
        """)

    def _toggle_details(self):
        self._details_visible = not self._details_visible
        self.details_box.setVisible(self._details_visible)
        self.details_button.setText("Hide Details" if self._details_visible else "Show Details")
        self.resize(520, 545 if self._details_visible else 400)

    def _accept_reconnect(self):
        self.reconnect_requested = True
        self.accept()


# =============================================================================
# Main window
# =============================================================================

class OBSSettingWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        # Window close and acquisition shutdown are intentionally different.
        # A normal title-bar X only minimizes this acquisition-manager window.
        # _final_shutdown_requested is set only by the main launcher shutdown
        # path (or another explicit application-level shutdown path).
        self._final_shutdown_requested = False
        self._shutdown_reason = ""

        self.shared = OBSSharedData()
        self.imu_yaw_offset_deg = load_imu_yaw_offset_deg()

        self.data_thread: Optional[
            OBSDataTCPThread
        ] = None

        self.command_thread: Optional[
            OBSCommandTCPThread
        ] = None

        self.gnss_thread: Optional[
            QThread
        ] = None

        self.usbl_thread: Optional[
            QThread
        ] = None

        self.data_connected = False
        self.command_connected = False
        self.gnss_connected = False
        self.usbl_connected = False

        # v26 navigation freshness state.  These are UI/operational state only;
        # raw GGA snapshots in shared RAM remain exactly as received.
        self.gnss_nmea_count = 0
        self.usbl_nmea_count = 0
        self._gnss_last_valid_gga: Optional[dict] = None
        self._usbl_last_valid_gga: Optional[dict] = None

        # v22 unexpected-disconnect tracking.  A warning is generated only when
        # a link was previously CONNECTED in the current session and then drops
        # without an operator-requested disconnect/shutdown.
        self._obs_disconnect_requested = False
        self._obs_disconnect_alert_shown = False
        self._obs_disconnect_alert_pending = False
        self._obs_disconnect_lost_links: set[str] = set()
        self._obs_disconnect_reasons: dict[str, str] = {}
        self._last_data_socket_error = ""
        self._last_command_socket_error = ""

        self.gimbal_locked = True
        self.power_mode = "LOW"

        self.command_templates = {
            "gimbal_lock": "",
            "gimbal_unlock": "",
            "power_low": "",
            "power_normal": "",
        }

        self.setWindowTitle(
            (
                f"{APP_TITLE} - "
                f"{SYSTEM_TITLE}"
            )
        )

        icon = application_icon()

        if not icon.isNull():
            self.setWindowIcon(
                icon
            )

        # Slim vertical window for side-by-side use with monitoring windows.
        self.resize(
            505,
            950,
        )

        self.setMinimumSize(
            445,
            700,
        )

        self.setMaximumWidth(
            700
        )

        self._build_ui()
        self._apply_style()

        # Local command broker: consumer GUIs connect here instead of opening
        # their own TCP 54300 connection to the OBS controller.
        self.command_broker = OBSIPCServer(self)
        self.command_broker_ready = False
        self.command_broker.request_received.connect(
            self.on_ipc_command_request
        )
        self.command_broker.client_count_changed.connect(
            self.on_ipc_client_count_changed
        )
        try:
            self.command_broker.start()
            self.command_broker_ready = True
            self.shared.update_command_broker_status(
                broker_running=True,
                obs_command_connected=False,
                last_result="READY",
            )
        except Exception as exc:
            # Do not overwrite the authoritative shared-RAM broker/heartbeat
            # status when this is a second OBS Setting instance and the primary
            # instance already owns the local IPC server.
            QMessageBox.critical(
                self,
                "OBS Local Command Broker",
                f"Cannot start local OBS command broker:\n\n{exc}",
            )

        self.status_timer = QTimer(
            self
        )

        self.status_timer.setSingleShot(
            True
        )

        self.status_timer.timeout.connect(
            lambda:
            self.footer_status.setText(
                "Ready"
            )
        )

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(500)
        self.heartbeat_timer.timeout.connect(
            self._publish_acquisition_heartbeat
        )
        self.heartbeat_timer.start()
        self._publish_acquisition_heartbeat()

        # v26: refresh GNSS/USBL source freshness independently from transport
        # connection state.  This catches a closed Cerulean Tracker even though
        # the local UDP socket remains bound/listening.
        self.nmea_freshness_timer = QTimer(self)
        self.nmea_freshness_timer.setInterval(500)
        self.nmea_freshness_timer.timeout.connect(
            self._refresh_nmea_freshness_status
        )
        self.nmea_freshness_timer.start()

        self.refresh_com_ports()
        self.load_settings(
            show_message=False
        )

        # Safe startup states.
        self.shared.update_telemetry(
            data_connected=False,
            command_connected=False,
            gnss_connected=False,
            usbl_connected=False,
            gimbal_locked=True,
            power_mode="LOW",
        )

        self._set_data_connection_status(
            False
        )

        self._set_command_connection_status(
            False
        )

        self._set_gnss_connection_status(
            False
        )

        self._set_usbl_connection_status(
            False
        )

        self._set_gimbal_status(
            True,
            source="default",
        )

        self._set_power_status(
            "LOW",
            source="default",
        )

        self.update_gnss_connection_fields()
        self.update_usbl_connection_fields()

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:

        central = QWidget()
        central.setObjectName(
            "centralWidget"
        )

        self.setCentralWidget(
            central
        )

        outer = QVBoxLayout(
            central
        )

        outer.setContentsMargins(
            14,
            14,
            14,
            12,
        )

        outer.setSpacing(
            10
        )

        title = QLabel(
            "OBS SETTING"
        )

        title.setObjectName(
            "headerTitle"
        )

        subtitle = QLabel(
            (
                "OBS Protocol / "
                "GNSS / USBL / Shared RAM"
            )
        )

        subtitle.setObjectName(
            "headerSubtitle"
        )

        outer.addWidget(
            title
        )

        outer.addWidget(
            subtitle
        )

        # Native vertical settings scroller.  Keep the header/footer outside this
        # area so only the settings cards move when the window is short.
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setObjectName(
            "settingsScrollArea"
        )
        self.settings_scroll.setWidgetResizable(
            True
        )
        self.settings_scroll.setFrameShape(
            QFrame.NoFrame
        )
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self.settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded
        )
        self.settings_scroll.setFocusPolicy(
            Qt.StrongFocus
        )
        self.settings_scroll.viewport().setObjectName(
            "settingsScrollViewport"
        )

        # A moderate wheel step feels natural for stacked cards and also applies
        # to touchpad-generated wheel events handled by QScrollArea.
        self.settings_scroll.verticalScrollBar().setSingleStep(
            34
        )

        content = QWidget()
        content.setObjectName(
            "scrollContent"
        )
        content.setSizePolicy(
            QSizePolicy.Preferred,
            QSizePolicy.Maximum,
        )

        body = QVBoxLayout(
            content
        )
        try:
            body.setSizeConstraint(
                QLayout.SizeConstraint.SetMinimumSize
            )
        except AttributeError:
            # Compatibility with older PySide6 enum exposure.
            body.setSizeConstraint(
                QLayout.SetMinimumSize
            )

        body.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        body.setSpacing(
            10
        )

        body.addWidget(
            self._build_network_card()
        )

        body.addWidget(
            self._build_geophone_card()
        )

        body.addWidget(
            self._build_gnss_card()
        )

        body.addWidget(
            self._build_usbl_card()
        )

        body.addWidget(
            self._build_recording_card()
        )

        body.addWidget(
            self._build_gimbal_card()
        )

        body.addWidget(
            self._build_power_card()
        )

        body.addWidget(
            self._build_config_card()
        )

        # Do not use a vertical stretch here: the layout's minimum-size
        # constraint defines the natural scrollable content height.
        body.addSpacing(
            2
        )

        self.settings_scroll.setWidget(
            content
        )

        outer.addWidget(
            self.settings_scroll,
            1,
        )

        self.footer_status = QLabel(
            "Ready"
        )

        self.footer_status.setObjectName(
            "footerStatus"
        )

        outer.addWidget(
            self.footer_status
        )

    def _card(
        self,
        title: str,
    ) -> tuple[
        QFrame,
        QVBoxLayout,
    ]:

        frame = QFrame()
        frame.setObjectName(
            "card"
        )

        layout = QVBoxLayout(
            frame
        )

        layout.setContentsMargins(
            12,
            10,
            12,
            12,
        )

        layout.setSpacing(
            8
        )

        title_label = QLabel(
            title
        )

        title_label.setObjectName(
            "cardTitle"
        )

        layout.addWidget(
            title_label
        )

        return frame, layout

    def _build_network_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "OBS TCP Connection"
        )

        form = QFormLayout()
        form.setSpacing(
            7
        )

        self.ip_edit = QLineEdit()

        self.ip_edit.setPlaceholderText(
            DEFAULT_IP
        )

        self.data_port_spin = ReliableSpinBox()

        self.data_port_spin.setRange(
            1,
            65535,
        )

        self.data_port_spin.setValue(
            DEFAULT_DATA_PORT
        )

        data_port_box = QWidget()

        data_port_row = QHBoxLayout(
            data_port_box
        )

        data_port_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        data_port_row.setSpacing(
            7
        )

        self.data_connection_indicator = (
            StatusPill()
        )

        self.data_connection_indicator.setMinimumWidth(
            128
        )

        data_port_row.addWidget(
            self.data_port_spin,
            1,
        )

        data_port_row.addWidget(
            self.data_connection_indicator
        )

        self.command_port_spin = (
            ReliableSpinBox()
        )

        self.command_port_spin.setRange(
            1,
            65535,
        )

        self.command_port_spin.setValue(
            DEFAULT_COMMAND_PORT
        )

        command_port_box = QWidget()

        command_port_row = QHBoxLayout(
            command_port_box
        )

        command_port_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        command_port_row.setSpacing(
            7
        )

        self.command_connection_indicator = (
            StatusPill()
        )

        self.command_connection_indicator.setMinimumWidth(
            128
        )

        command_port_row.addWidget(
            self.command_port_spin,
            1,
        )

        command_port_row.addWidget(
            self.command_connection_indicator
        )

        form.addRow(
            "OBS IP",
            self.ip_edit,
        )

        form.addRow(
            "Data Port",
            data_port_box,
        )

        form.addRow(
            "Command Port",
            command_port_box,
        )

        layout.addLayout(
            form
        )

        self.obs_connect_button = (
            QPushButton(
                "Connect OBS"
            )
        )

        self.obs_connect_button.setObjectName(
            "primaryButton"
        )

        self.obs_connect_button.clicked.connect(
            self.toggle_obs_connection
        )

        layout.addWidget(
            self.obs_connect_button
        )

        self.data_rate_label = QLabel(
            (
                "ADC raw: -- Hz | output: -- Hz | "
                "avg N=-- | drop: -- | sync: -- | error: --"
            )
        )

        self.data_rate_label.setObjectName(
            "detailLabel"
        )

        self.data_rate_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.data_rate_label
        )

        self.command_rx_label = QLabel(
            "Telemetry: waiting for command channel"
        )

        self.command_rx_label.setObjectName(
            "detailLabel"
        )

        self.command_rx_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.command_rx_label
        )

        return card

    def _build_geophone_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "Geophone"
        )

        rate_row = QHBoxLayout()

        rate_label = QLabel(
            "ADC Expected Rate"
        )

        self.adc_source_rate_value_label = QLabel(
            f"{EXPECTED_ADC_SOURCE_RATE_HZ:.6f} Hz"
        )
        self.adc_source_rate_value_label.setObjectName(
            "valueLabel"
        )
        self.adc_source_rate_value_label.setToolTip(
            "Read-only corrected-firmware contract. PC measured rate is "
            "shown as a diagnostic after a long observation window."
        )

        rate_row.addWidget(rate_label)
        rate_row.addStretch(1)
        rate_row.addWidget(self.adc_source_rate_value_label)
        layout.addLayout(rate_row)

        row = QHBoxLayout()

        label = QLabel(
            "Decimation / Average"
        )

        self.decimation_spin = ReliableSpinBox()

        self.decimation_spin.setRange(
            1,
            max(1, int(math.ceil(EXPECTED_ADC_SOURCE_RATE_HZ))),
        )

        self.decimation_spin.setSuffix(
            " data"
        )

        self.decimation_spin.setValue(
            DEFAULT_DECIMATION_SAMPLES
        )

        self.decimation_spin.setMinimumWidth(
            120
        )

        self.decimation_spin.valueChanged.connect(
            self.update_decimation_result
        )

        row.addWidget(
            label
        )

        row.addStretch(
            1
        )

        row.addWidget(
            self.decimation_spin
        )

        layout.addLayout(
            row
        )

        self.decimation_result_label = QLabel(
            ""
        )

        self.decimation_result_label.setObjectName(
            "detailLabel"
        )

        self.decimation_result_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.decimation_result_label
        )

        detail = QLabel(
            (
                "ADC Expected Rate 1000 Hz adalah kontrak firmware yang sudah "
                "dikoreksi dan tidak dapat diedit operator. PC mengukur rate "
                "secara long-window hanya sebagai health-check; hasil ukur tidak "
                "mengubah waveform atau metadata secara dinamis. N = jumlah data "
                "RAW yang dirata-ratakan menjadi 1 data output. "
                "CH0 / CH1 / CH2 = Geophone N / E / Z. "
                "CH3 mengikuti window N yang sama untuk menjaga sinkronisasi "
                "frame 4-channel. Averaging tetap kontinu melewati boundary "
                "payload OBS 128 frame."
            )
        )

        detail.setObjectName(
            "detailLabel"
        )

        detail.setWordWrap(
            True
        )

        layout.addWidget(
            detail
        )

        self.update_decimation_result()

        return card

    def _expected_adc_source_rate_hz(
        self,
    ) -> float:
        return float(EXPECTED_ADC_SOURCE_RATE_HZ)

    def update_decimation_result(
        self,
        *_args,
    ) -> None:

        averaging_samples = max(
            1,
            int(
                self.decimation_spin.value()
            ),
        )

        source_rate_hz = self._expected_adc_source_rate_hz()

        max_decimation = max(
            1,
            int(math.ceil(source_rate_hz)),
        )
        if self.decimation_spin.maximum() != max_decimation:
            self.decimation_spin.setMaximum(max_decimation)
            averaging_samples = max(
                1,
                int(self.decimation_spin.value()),
            )

        result_rate_hz = (
            float(source_rate_hz)
            / float(averaging_samples)
        )

        self.decimation_result_label.setText(
            (
                f"Expected source: {source_rate_hz:.6f} Hz/channel  |  "
                f"Average N={averaging_samples} data  |  "
                f"Result: {result_rate_hz:.6f} Hz/channel"
            )
        )

    def _build_gnss_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "GNSS Connection"
        )

        form = QFormLayout()

        self.gnss_mode_combo = QComboBox()
        self.gnss_mode_combo.addItems(
            [
                "COM Port",
                "UDP",
            ]
        )
        self.gnss_mode_combo.currentTextChanged.connect(
            self.update_gnss_connection_fields
        )
        form.addRow(
            "Connection",
            self.gnss_mode_combo,
        )

        self.gnss_com_widget = QWidget()

        com_row = QHBoxLayout(
            self.gnss_com_widget
        )

        com_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        com_row.setSpacing(
            6
        )

        self.gnss_com_combo = QComboBox()
        self.gnss_com_combo.setEditable(
            True
        )

        self.gnss_refresh_button = (
            QPushButton(
                "Refresh"
            )
        )

        self.gnss_refresh_button.setObjectName(
            "smallButton"
        )

        self.gnss_refresh_button.clicked.connect(
            self.refresh_com_ports
        )

        com_row.addWidget(
            self.gnss_com_combo,
            1,
        )

        com_row.addWidget(
            self.gnss_refresh_button
        )

        self.gnss_baud_combo = (
            self._new_baud_combo(
                DEFAULT_GNSS_BAUD
            )
        )

        self.gnss_udp_ip_edit = QLineEdit()
        self.gnss_udp_ip_edit.setPlaceholderText(
            DEFAULT_GNSS_UDP_IP
        )

        self.gnss_udp_port_spin = ReliableSpinBox()
        self.gnss_udp_port_spin.setRange(
            1,
            65535,
        )
        self.gnss_udp_port_spin.setValue(
            DEFAULT_GNSS_UDP_PORT
        )

        form.addRow(
            "COM Port",
            self.gnss_com_widget,
        )

        form.addRow(
            "Baudrate",
            self.gnss_baud_combo,
        )

        form.addRow(
            "UDP Listen IP",
            self.gnss_udp_ip_edit,
        )

        form.addRow(
            "UDP Port",
            self.gnss_udp_port_spin,
        )

        layout.addLayout(
            form
        )

        row = QHBoxLayout()

        self.gnss_connect_button = (
            QPushButton(
                "Connect GNSS"
            )
        )

        self.gnss_connect_button.setObjectName(
            "primaryButton"
        )

        self.gnss_connect_button.clicked.connect(
            self.toggle_gnss_connection
        )

        self.gnss_connection_indicator = (
            StatusPill()
        )

        self.gnss_connection_indicator.setMinimumWidth(
            128
        )

        row.addWidget(
            self.gnss_connect_button,
            1,
        )

        row.addWidget(
            self.gnss_connection_indicator
        )

        layout.addLayout(
            row
        )

        self.gnss_gga_label = QLabel(
            "NMEA: NOT LISTENING • age -- • count 0\nGGA: --, -- | Fix 0 • NO VALID POSITION"
        )

        self.gnss_gga_label.setObjectName(
            "detailLabel"
        )

        self.gnss_gga_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.gnss_gga_label
        )

        return card

    def _build_usbl_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "USBL Connection"
        )

        form = QFormLayout()

        self.usbl_mode_combo = QComboBox()

        self.usbl_mode_combo.addItems(
            [
                "COM Port",
                "UDP",
            ]
        )

        self.usbl_mode_combo.currentTextChanged.connect(
            self.update_usbl_connection_fields
        )

        form.addRow(
            "Connection",
            self.usbl_mode_combo,
        )

        self.usbl_com_widget = QWidget()

        com_row = QHBoxLayout(
            self.usbl_com_widget
        )

        com_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        com_row.setSpacing(
            6
        )

        self.usbl_com_combo = QComboBox()
        self.usbl_com_combo.setEditable(
            True
        )

        self.usbl_refresh_button = (
            QPushButton(
                "Refresh"
            )
        )

        self.usbl_refresh_button.setObjectName(
            "smallButton"
        )

        self.usbl_refresh_button.clicked.connect(
            self.refresh_com_ports
        )

        com_row.addWidget(
            self.usbl_com_combo,
            1,
        )

        com_row.addWidget(
            self.usbl_refresh_button
        )

        self.usbl_baud_combo = (
            self._new_baud_combo(
                DEFAULT_USBL_BAUD
            )
        )

        self.usbl_udp_ip_edit = QLineEdit()

        self.usbl_udp_ip_edit.setPlaceholderText(
            DEFAULT_USBL_UDP_IP
        )

        self.usbl_udp_port_spin = ReliableSpinBox()

        self.usbl_udp_port_spin.setRange(
            1,
            65535,
        )

        self.usbl_udp_port_spin.setValue(
            DEFAULT_USBL_UDP_PORT
        )

        form.addRow(
            "COM Port",
            self.usbl_com_widget,
        )

        form.addRow(
            "Baudrate",
            self.usbl_baud_combo,
        )

        form.addRow(
            "UDP Listen IP",
            self.usbl_udp_ip_edit,
        )

        form.addRow(
            "UDP Port",
            self.usbl_udp_port_spin,
        )

        layout.addLayout(
            form
        )

        row = QHBoxLayout()

        self.usbl_connect_button = (
            QPushButton(
                "Connect USBL"
            )
        )

        self.usbl_connect_button.setObjectName(
            "primaryButton"
        )

        self.usbl_connect_button.clicked.connect(
            self.toggle_usbl_connection
        )

        self.usbl_connection_indicator = (
            StatusPill()
        )

        self.usbl_connection_indicator.setMinimumWidth(
            128
        )

        row.addWidget(
            self.usbl_connect_button,
            1,
        )

        row.addWidget(
            self.usbl_connection_indicator
        )

        layout.addLayout(
            row
        )

        self.usbl_gga_label = QLabel(
            "NMEA: NOT LISTENING • age -- • count 0\nGGA: --, -- | Fix 0 • NO VALID POSITION"
        )

        self.usbl_gga_label.setObjectName(
            "detailLabel"
        )

        self.usbl_gga_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.usbl_gga_label
        )

        return card

    def _build_recording_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "MiniSEED Recording"
        )

        row = QHBoxLayout()

        self.record_folder_edit = QLineEdit()

        self.browse_button = QPushButton(
            "Browse"
        )

        self.browse_button.setObjectName(
            "smallButton"
        )

        self.browse_button.clicked.connect(
            self.browse_record_folder
        )

        row.addWidget(
            self.record_folder_edit,
            1,
        )

        row.addWidget(
            self.browse_button
        )

        layout.addLayout(
            row
        )

        return card

    def _build_gimbal_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "Gimbal Control"
        )

        self.gimbal_indicator = StatusPill()

        layout.addWidget(
            self.gimbal_indicator
        )

        row = QHBoxLayout()

        self.lock_gimbal_button = QPushButton(
            "Lock Gimbal"
        )

        self.unlock_gimbal_button = QPushButton(
            "Unlock Gimbal"
        )

        for button in (
            self.lock_gimbal_button,
            self.unlock_gimbal_button,
        ):
            button.setObjectName(
                "controlButton"
            )

        self.lock_gimbal_button.clicked.connect(
            lambda:
            self._send_remote_gpio_command(
                RMCMD_LOCK_HIGH,
                "Gimbal Lock",
                gimbal_locked=True,
            )
        )

        self.unlock_gimbal_button.clicked.connect(
            lambda:
            self._send_remote_gpio_command(
                RMCMD_LOCK_LOW,
                "Gimbal Unlock",
                gimbal_locked=False,
            )
        )

        row.addWidget(
            self.lock_gimbal_button
        )

        row.addWidget(
            self.unlock_gimbal_button
        )

        layout.addLayout(
            row
        )

        self.gimbal_detail = QLabel()

        self.gimbal_detail.setObjectName(
            "detailLabel"
        )

        self.gimbal_detail.setWordWrap(
            True
        )

        layout.addWidget(
            self.gimbal_detail
        )

        return card

    def _build_power_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "OBS Power Mode"
        )

        self.power_indicator = StatusPill()

        layout.addWidget(
            self.power_indicator
        )

        row = QHBoxLayout()

        self.low_power_button = QPushButton(
            "Low Power"
        )

        self.normal_power_button = QPushButton(
            "Normal Mode"
        )

        for button in (
            self.low_power_button,
            self.normal_power_button,
        ):
            button.setObjectName(
                "controlButton"
            )

        self.low_power_button.clicked.connect(
            lambda:
            self._send_remote_gpio_command(
                RMCMD_PWR_MODE_HIGH,
                "Low Power / PWR_MODE HIGH",
                power_mode="LOW",
            )
        )

        self.normal_power_button.clicked.connect(
            lambda:
            self._send_remote_gpio_command(
                RMCMD_PWR_MODE_LOW,
                "Normal Mode / PWR_MODE LOW",
                power_mode="NORMAL",
            )
        )

        row.addWidget(
            self.low_power_button
        )

        row.addWidget(
            self.normal_power_button
        )

        layout.addLayout(
            row
        )

        self.power_detail = QLabel()

        self.power_detail.setObjectName(
            "detailLabel"
        )

        self.power_detail.setWordWrap(
            True
        )

        layout.addWidget(
            self.power_detail
        )

        return card

    def _build_config_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "Configuration File"
        )

        ini_label = QLabel(
            str(INI_PATH)
        )

        ini_label.setObjectName(
            "pathLabel"
        )

        ini_label.setWordWrap(
            True
        )

        layout.addWidget(
            ini_label
        )

        shared_label = QLabel(
            (
                f"Shared RAM: "
                f"{self.shared.name} | "
                f"{self.shared.size / (1024 * 1024):.2f} MB"
            )
        )

        shared_label.setObjectName(
            "pathLabel"
        )

        shared_label.setWordWrap(
            True
        )

        layout.addWidget(
            shared_label
        )

        row = QHBoxLayout()

        self.load_button = QPushButton(
            "Load INI"
        )

        self.load_button.setObjectName(
            "controlButton"
        )

        self.load_button.clicked.connect(
            lambda:
            self.load_settings(
                show_message=True
            )
        )

        self.save_button = QPushButton(
            "Save Settings"
        )

        self.save_button.setObjectName(
            "primaryButton"
        )

        self.save_button.clicked.connect(
            self.save_settings
        )

        row.addWidget(
            self.load_button
        )

        row.addWidget(
            self.save_button
        )

        layout.addLayout(
            row
        )

        return card

    @staticmethod
    def _new_baud_combo(
        default: int,
    ) -> QComboBox:

        combo = QComboBox()

        combo.setEditable(
            True
        )

        combo.addItems(
            [
                "4800",
                "9600",
                "19200",
                "38400",
                "57600",
                "115200",
                "230400",
                "460800",
                "921600",
            ]
        )

        combo.setCurrentText(
            str(default)
        )

        return combo

    # -------------------------------------------------------------------------
    # Style
    # -------------------------------------------------------------------------

    def _apply_style(
        self,
    ) -> None:

        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget,
            QWidget#scrollContent {
                background-color: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QScrollArea#settingsScrollArea {
                background: transparent;
                border: none;
            }

            QWidget#settingsScrollViewport {
                background-color: #07131D;
                border: none;
            }

            /* Visible, conventional vertical scrollbar for the settings panel. */
            QScrollBar:vertical {
                background: #091923;
                width: 12px;
                margin: 2px 0px 2px 2px;
                border: none;
                border-radius: 6px;
            }

            QScrollBar::handle:vertical {
                background: #2C566C;
                min-height: 42px;
                border-radius: 5px;
                margin: 1px 2px;
            }

            QScrollBar::handle:vertical:hover {
                background: #3C7898;
            }

            QScrollBar::handle:vertical:pressed {
                background: #4FA5D0;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
                border: none;
                background: transparent;
            }

            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }

            QLabel {
                background: transparent;
                color: #FFFFFF;
            }

            QLabel#headerTitle {
                color: #FFFFFF;
                font-size: 21px;
                font-weight: 800;
                letter-spacing: 1px;
            }

            QLabel#headerSubtitle {
                color: #D3E0E8;
                font-size: 11px;
                padding-bottom: 2px;
            }

            QFrame#card {
                background-color: #0D1E2A;
                border: 1px solid #17374A;
                border-radius: 10px;
            }

            QLabel#cardTitle {
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 800;
                letter-spacing: 0.7px;
            }

            QLabel#detailLabel {
                color: #B6C7D1;
                font-size: 10px;
            }

            QLabel#pathLabel {
                background-color: #091620;
                border: 1px solid #17374A;
                border-radius: 6px;
                color: #B6C7D1;
                font-size: 9px;
                padding: 6px;
            }

            QLabel#footerStatus {
                color: #B6C7D1;
                font-size: 10px;
                padding: 2px;
            }

            QLineEdit,
            QSpinBox,
            QComboBox {
                background-color: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 6px;
                min-height: 28px;
                padding: 2px 7px;
                selection-background-color: #2B739A;
            }

            /* v21: reserve a comfortable right-side band for the reliable
               spinner UP/DOWN mouse targets. */
            QSpinBox {
                padding-right: 26px;
            }

            QLineEdit:focus,
            QSpinBox:focus,
            QComboBox:focus {
                border: 1px solid #4FA5D0;
            }

            QLineEdit:disabled,
            QSpinBox:disabled,
            QComboBox:disabled {
                background-color: #0B151B;
                color: #607784;
                border: 1px solid #1B2B34;
            }

            QComboBox QAbstractItemView {
                background-color: #0B1B26;
                color: #FFFFFF;
                selection-background-color: #245B79;
            }

            QPushButton {
                min-height: 30px;
                border-radius: 7px;
                padding: 3px 10px;
                font-weight: 700;
            }

            QPushButton#primaryButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QPushButton#primaryButton:hover {
                background-color: #1D78A4;
            }

            QPushButton#controlButton,
            QPushButton#smallButton {
                background-color: #132A39;
                color: #FFFFFF;
                border: 1px solid #27526A;
            }

            QPushButton#controlButton:hover,
            QPushButton#smallButton:hover {
                background-color: #18384C;
                border: 1px solid #3C7898;
            }

            QPushButton:disabled {
                background-color: #101B22;
                color: #536873;
                border: 1px solid #1B2B34;
            }

            /* Ensure message text is visible on Windows native dialogs. */
            QMessageBox {
                background-color: #0D1E2A;
            }

            QMessageBox QLabel {
                background: transparent;
                color: #FFFFFF;
                min-width: 340px;
            }

            QMessageBox QPushButton {
                min-width: 84px;
                min-height: 28px;
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
                border-radius: 6px;
                padding: 3px 10px;
            }

            QMessageBox QPushButton:hover {
                background-color: #1D78A4;
            }
            """
        )

    # -------------------------------------------------------------------------
    # Footer
    # -------------------------------------------------------------------------

    def _set_footer(
        self,
        text: str,
        timeout_ms: int = 4000,
    ) -> None:

        self.footer_status.setText(
            text
        )

        if timeout_ms > 0:
            self.status_timer.start(
                timeout_ms
            )

    # -------------------------------------------------------------------------
    # OBS connection
    # -------------------------------------------------------------------------

    def toggle_obs_connection(
        self,
    ) -> None:

        if (
            self._obs_workers_running()
        ):
            self.disconnect_obs()
        else:
            self.connect_obs()

    def _obs_workers_running(
        self,
    ) -> bool:

        return (
            (
                self.data_thread is not None
                and self.data_thread.isRunning()
            )
            or
            (
                self.command_thread is not None
                and self.command_thread.isRunning()
            )
        )

    def connect_obs(
        self,
    ) -> None:

        if not getattr(self, "command_broker_ready", False):
            QMessageBox.critical(
                self,
                "OBS Centralized I/O",
                (
                    "Local command broker is not available.\n\n"
                    "To guarantee that only one OBS Setting instance owns TCP "
                    "54300/54301, this process is not allowed to connect to OBS."
                ),
            )
            return

        host = (
            self.ip_edit
            .text()
            .strip()
        )

        if not host:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "OBS IP address cannot be empty.",
            )
            return

        data_port = int(
            self.data_port_spin.value()
        )

        command_port = int(
            self.command_port_spin.value()
        )

        # New OBS connection attempt = new notification session.
        self._obs_disconnect_requested = False
        self._obs_disconnect_alert_shown = False
        self._obs_disconnect_alert_pending = False
        self._obs_disconnect_lost_links.clear()
        self._obs_disconnect_reasons.clear()
        self._last_data_socket_error = ""
        self._last_command_socket_error = ""

        self.data_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.command_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.obs_connect_button.setText(
            "Disconnect OBS"
        )

        self._set_obs_fields_enabled(
            False
        )

        decimation_samples = max(
            1,
            int(
                self.decimation_spin.value()
            ),
        )

        decimation_mode = (
            "raw"
            if decimation_samples == 1
            else "mean"
        )

        raw_sample_rate_hz = (
            self._expected_adc_source_rate_hz()
        )

        # New physical connection = new acquisition session. The raw rate is the
        # corrected-firmware 1000 Hz contract; PC measurement is diagnostic only.
        # The stream metadata becomes the single source of truth for every
        # downstream module.
        stream_info = (
            self.shared.start_new_adc_session(
                reset_bulk_status=True,
                raw_sample_rate_hz=(
                    raw_sample_rate_hz
                ),
                decimation_samples=(
                    decimation_samples
                ),
                decimation_mode=(
                    decimation_mode
                ),
            )
        )

        self._set_footer(
            (
                f"ADC stream expected: "
                f"{stream_info.raw_sample_rate_hz:g} Hz / "
                f"N={stream_info.decimation_samples} -> "
                f"{stream_info.effective_sample_rate_hz:.3f} Hz"
            ),
            5000,
        )

        data_worker = OBSDataTCPThread(
            host=host,
            port=data_port,
            shared=self.shared,
            decimation_samples=(
                decimation_samples
            ),
            raw_sample_rate_hz=(
                raw_sample_rate_hz
            ),
            parent=self,
        )

        command_worker = OBSCommandTCPThread(
            host=host,
            port=command_port,
            shared=self.shared,
            parent=self,
        )

        data_worker.connection_changed.connect(
            self.on_data_connection_changed
        )

        data_worker.socket_error.connect(
            self.on_data_socket_error
        )

        data_worker.stream_status.connect(
            self.on_stream_status
        )

        data_worker.finished.connect(
            self.on_data_thread_finished
        )

        command_worker.connection_changed.connect(
            self.on_command_connection_changed
        )

        command_worker.sentence_received.connect(
            self.on_obs_sentence
        )

        command_worker.sentence_sent.connect(
            self.on_command_sentence_sent
        )

        command_worker.socket_error.connect(
            self.on_command_socket_error
        )

        command_worker.protocol_error.connect(
            lambda message:
            self._set_footer(
                message,
                5000,
            )
        )

        command_worker.finished.connect(
            self.on_command_thread_finished
        )

        self.data_thread = data_worker
        self.command_thread = command_worker

        data_worker.start()
        command_worker.start()

    def disconnect_obs(
        self,
    ) -> None:

        # Operator-requested disconnect is intentional; do not show the v22
        # unexpected-disconnect warning while the worker threads are stopping.
        self._obs_disconnect_requested = True
        self._obs_disconnect_alert_pending = False
        self._obs_disconnect_lost_links.clear()
        self._obs_disconnect_reasons.clear()

        if self.data_thread is not None:
            self.data_thread.stop()

        if self.command_thread is not None:
            self.command_thread.stop()

        self._set_footer(
            "Disconnecting OBS..."
        )

    def _set_obs_fields_enabled(
        self,
        enabled: bool,
    ) -> None:

        self.ip_edit.setEnabled(
            enabled
        )

        self.data_port_spin.setEnabled(
            enabled
        )

        self.command_port_spin.setEnabled(
            enabled
        )

        # N defines the processed shared-stream rate and must not change inside
        # one acquisition session. The corrected-firmware 1000 Hz source rate is
        # read-only.
        self.decimation_spin.setEnabled(
            enabled
        )

    def _set_data_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.data_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            data_connected=self.data_connected
        )

        if connected:
            self.data_connection_indicator.set_state(
                "CONNECTED",
                "good",
            )
        else:
            self.data_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

    def _set_command_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.command_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            command_connected=self.command_connected
        )

        try:
            self.shared.update_command_broker_status(
                broker_running=True,
                obs_command_connected=self.command_connected,
                last_result=(
                    "OBS TCP 54300 CONNECTED"
                    if self.command_connected
                    else "OBS TCP 54300 DISCONNECTED"
                ),
            )
            self.command_broker.set_obs_command_connected(
                self.command_connected,
                (
                    "OBS command TCP 54300 connected via OBS Setting"
                    if self.command_connected
                    else "OBS Setting is running; TCP 54300 is not connected"
                ),
            )
        except Exception:
            pass

        if connected:
            self.command_connection_indicator.set_state(
                "CONNECTED",
                "good",
            )
        else:
            self.command_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

            # Requested no-feedback defaults.
            self._set_gimbal_status(
                True,
                source="default",
            )

            self._set_power_status(
                "LOW",
                source="default",
            )

        self._update_command_buttons()

    def _publish_acquisition_heartbeat(self) -> None:
        if not self.command_broker_ready:
            return
        try:
            self.shared.update_acquisition_health(
                process_id=os.getpid(),
                command_connected=self.command_connected,
                data_connected=self.data_connected,
                broker_running=bool(self.command_broker_ready),
                timestamp_ns=time.time_ns(),
            )
        except Exception:
            pass

    def on_ipc_client_count_changed(self, count: int) -> None:
        self._set_footer(f"Local command clients: {int(count)}", 1500)

    def on_ipc_command_request(self, request: dict, client_socket: object) -> None:
        request_id = str(request.get("request_id") or "")
        source = str(request.get("source") or "consumer")[:48]
        request_type = str(request.get("type") or "").strip().lower()
        client_id = int(request.get("_client_id") or id(client_socket))

        # Lifecycle requests are local process-control messages, not OBS wire
        # commands. Handle them BEFORE requiring TCP 54300 to be connected.
        if request_type == "show_window":
            try:
                self.showNormal()
                self.raise_()
                self.activateWindow()
            except Exception:
                pass
            self.command_broker.send_response(client_socket, {
                "type": "accepted",
                "request_id": request_id,
                "request_type": "show_window",
            })
            return

        if request_type == "shutdown":
            # Only the main launcher is permitted to request final manager
            # shutdown. The IPC endpoint is local to this PC.
            if source != "main":
                self.command_broker.send_response(client_socket, {
                    "type": "error",
                    "request_id": request_id,
                    "message": "Shutdown request is reserved for the main launcher.",
                })
                return

            self.command_broker.send_response(client_socket, {
                "type": "accepted",
                "request_id": request_id,
                "request_type": "shutdown",
            })
            QTimer.singleShot(0, lambda: self.request_final_shutdown("main launcher closed"))
            return

        if request_type == "set_yaw_offset":
            # This is a local acquisition-calibration request, not an OBS wire
            # command, so it remains valid even when TCP 54300 is disconnected.
            if source != "other_sensors":
                self.command_broker.send_response(client_socket, {
                    "type": "error",
                    "request_id": request_id,
                    "message": "Yaw offset updates are reserved for Other Sensors.",
                })
                return
            try:
                offset_deg = float(request.get("offset_deg"))
                save_imu_yaw_offset_deg(offset_deg)
                self.imu_yaw_offset_deg = offset_deg
                self._set_footer(
                    f"System IMU yaw offset set to {offset_deg:+.2f}°",
                    2500,
                )
                self.command_broker.send_response(client_socket, {
                    "type": "accepted",
                    "request_id": request_id,
                    "request_type": "set_yaw_offset",
                    "offset_deg": offset_deg,
                    "detail": "Applied centrally; next AHRS2 sample publishes corrected yaw.",
                })
            except Exception as exc:
                self.command_broker.send_response(client_socket, {
                    "type": "error",
                    "request_id": request_id,
                    "request_type": "set_yaw_offset",
                    "message": str(exc),
                })
            return

        worker = self.command_thread
        if (
            not self.command_connected
            or worker is None
            or not worker.isRunning()
        ):
            self.command_broker.send_response(client_socket, {
                "type": "error",
                "request_id": request_id,
                "message": "OBS TCP 54300 is not connected in OBS Setting.",
            })
            return

        metadata = {
            "ipc": True,
            "request_id": request_id,
            "source": source,
            "client_id": client_id,
            "request_type": request_type,
        }

        try:
            if request_type == "rmcmd":
                code = int(request.get("code"))
                if code not in RMCMD_IPC_ALLOWED_CODES:
                    raise ValueError(
                        f"IPC RMCMD code {code} is not allowed; expected 50..62."
                    )
                sentence = build_obs_sentence(f"RMCMD,{code}")
                metadata["code"] = code
                worker.send_sentence(sentence, metadata=metadata)
                self.command_broker.send_response(client_socket, {
                    "type": "accepted",
                    "request_id": request_id,
                    "request_type": "rmcmd",
                    "code": code,
                })

            elif request_type == "time_sync":
                worker.send_time1(metadata=metadata)
                self.command_broker.send_response(client_socket, {
                    "type": "accepted",
                    "request_id": request_id,
                    "request_type": "time_sync",
                })

            else:
                raise ValueError(f"Unsupported IPC request type: {request_type!r}")

        except Exception as exc:
            self.shared.update_command_broker_status(
                last_source=source,
                last_result=f"REJECTED: {exc}",
            )
            self.command_broker.send_response(client_socket, {
                "type": "error",
                "request_id": request_id,
                "message": str(exc),
            })

    def on_command_sentence_sent(self, result: object) -> None:
        info = dict(result or {})
        sentence = str(info.get("sentence") or "")
        metadata = dict(info.get("metadata") or {})
        source = str(metadata.get("source") or "obs_setting")

        self.shared.update_command_broker_status(
            broker_running=True,
            obs_command_connected=True,
            last_source=source,
            last_sentence=sentence,
            last_result="SENT",
            timestamp_ns=int(info.get("timestamp_ns") or time.time_ns()),
        )

        if not metadata.get("ipc"):
            return

        payload = {
            "type": "sent",
            "request_id": str(metadata.get("request_id") or ""),
            "request_type": str(metadata.get("request_type") or ""),
            "sentence": sentence,
            "endpoint": f"OBS Setting -> OBS {info.get('endpoint') or 'TCP 54300'}",
        }
        if "code" in metadata:
            payload["code"] = int(metadata["code"])
        if info.get("time_fields") is not None:
            payload["fields"] = dict(info.get("time_fields") or {})

        self.command_broker.send_to_client_id(
            int(metadata.get("client_id") or 0),
            payload,
        )

    def on_data_socket_error(self, message: str) -> None:
        self._last_data_socket_error = str(message or "").strip()
        self._set_footer(
            f"DATA error: {self._last_data_socket_error or 'Unknown error'}",
            7000,
        )

    def on_command_socket_error(self, message: str) -> None:
        self._last_command_socket_error = str(message or "").strip()
        self._set_footer(
            f"COMMAND error: {self._last_command_socket_error or 'Unknown error'}",
            7000,
        )

    def _queue_unexpected_obs_disconnect(
        self,
        link_name: str,
        detail: str = "",
    ) -> None:
        """Coalesce unexpected OBS link losses into one visible warning."""
        if (
            self._final_shutdown_requested
            or self._obs_disconnect_requested
            or self._obs_disconnect_alert_shown
        ):
            return

        link_name = str(link_name or "OBS").strip().upper()
        detail = str(detail or "").strip()

        self._obs_disconnect_lost_links.add(link_name)
        if detail and detail.lower() != "not connected":
            self._obs_disconnect_reasons[link_name] = detail

        if self._obs_disconnect_alert_pending:
            return

        self._obs_disconnect_alert_pending = True
        # DATA and COMMAND sockets commonly fail almost simultaneously when a
        # cable/power link is lost. Give Qt a short interval to collect both
        # signals so the operator gets one dialog instead of two.
        QTimer.singleShot(250, self._show_unexpected_obs_disconnect)

    def _summarize_obs_disconnect_reason(self) -> str:
        """Return a short operator-facing cause; keep raw errors in Details."""
        reasons = [
            str(self._obs_disconnect_reasons.get(name, "") or "").strip()
            for name in ("DATA", "COMMAND")
        ]
        reasons = [reason for reason in reasons if reason]

        if not reasons:
            return "The OBS network connection was interrupted."

        lowered = " ".join(reasons).lower()
        if (
            "winerror 10054" in lowered
            or "forcibly closed by the remote host" in lowered
            or "connection reset by peer" in lowered
        ):
            return "OBS/controller closed the TCP connection."
        if "timed out" in lowered or "timeout" in lowered:
            return "The OBS network connection timed out."
        if "refused" in lowered:
            return "The OBS connection was refused."

        return "The OBS network connection was interrupted."

    def _begin_obs_reconnect_after_loss(self) -> None:
        """Stop any remaining workers, then reconnect through connect_obs()."""
        if self._final_shutdown_requested:
            return

        self._set_footer(
            "Reconnecting OBS...",
            0,
        )

        # Mark the worker shutdown as intentional so its final signals cannot
        # create another unexpected-disconnect alert during this reconnect cycle.
        self._obs_disconnect_requested = True

        for worker in (self.data_thread, self.command_thread):
            if worker is not None:
                try:
                    worker.stop()
                except Exception:
                    pass

        self._wait_for_obs_workers_then_reconnect(0)

    def _wait_for_obs_workers_then_reconnect(self, attempt: int) -> None:
        if self._final_shutdown_requested:
            return

        if not self._obs_workers_running():
            self._refresh_obs_button_state()
            self._obs_disconnect_requested = False
            self.connect_obs()
            return

        if int(attempt) >= 50:
            self._set_footer(
                "Reconnect could not start because the previous OBS connection is still stopping.",
                7000,
            )
            self._refresh_obs_button_state()
            return

        QTimer.singleShot(
            100,
            lambda: self._wait_for_obs_workers_then_reconnect(int(attempt) + 1),
        )

    def _show_unexpected_obs_disconnect(self) -> None:
        self._obs_disconnect_alert_pending = False

        if (
            self._final_shutdown_requested
            or self._obs_disconnect_requested
            or self._obs_disconnect_alert_shown
            or not self._obs_disconnect_lost_links
        ):
            return

        self._obs_disconnect_alert_shown = True

        data_port = int(self.data_port_spin.value())
        command_port = int(self.command_port_spin.value())

        data_state = "CONNECTED" if self.data_connected else "DISCONNECTED"
        command_state = "CONNECTED" if self.command_connected else "DISCONNECTED"
        cause = self._summarize_obs_disconnect_reason()
        disconnected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S WIB")

        lost_links = []
        for name in ("DATA", "COMMAND"):
            if name in self._obs_disconnect_lost_links:
                port = data_port if name == "DATA" else command_port
                lost_links.append(f"{name} (TCP {port})")

        if not lost_links:
            lost_links = sorted(self._obs_disconnect_lost_links)

        detail_lines = []
        for name in ("DATA", "COMMAND"):
            if name not in self._obs_disconnect_lost_links:
                continue
            port = data_port if name == "DATA" else command_port
            reason = self._obs_disconnect_reasons.get(name, "").strip()
            detail_lines.extend(
                [
                    f"{name} (TCP {port})",
                    reason or "No socket error detail was reported.",
                    "",
                ]
            )

        if not detail_lines:
            detail_lines.append("No additional socket details were reported.")

        LOGGER.warning(
            "Unexpected OBS disconnect: links=%s reasons=%s",
            ",".join(sorted(self._obs_disconnect_lost_links)),
            self._obs_disconnect_reasons,
        )

        try:
            self.status_timer.stop()
        except Exception:
            pass

        self._set_footer(
            "OBS DISCONNECTED unexpectedly: " + ", ".join(lost_links),
            0,
        )

        # OBS Setting may be minimized while it continues centralized acquisition.
        # Bring it forward so the warning cannot be missed.
        try:
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
            QApplication.alert(self, 0)
        except Exception:
            pass

        dialog = OBSDisconnectDialog(
            parent=self,
            data_port=data_port,
            command_port=command_port,
            data_state=data_state,
            command_state=command_state,
            cause=cause,
            disconnected_at=disconnected_at,
            details_text="\n".join(detail_lines).rstrip(),
        )
        dialog.exec()

        if dialog.reconnect_requested:
            self._begin_obs_reconnect_after_loss()

    def on_data_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        was_connected = bool(self.data_connected)

        self._set_data_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"DATA connected: {detail}"
            )
        elif was_connected:
            reason = self._last_data_socket_error or str(detail or "")
            self._queue_unexpected_obs_disconnect(
                "DATA",
                reason,
            )

    def on_command_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        was_connected = bool(self.command_connected)

        self._set_command_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"COMMAND connected: {detail}"
            )
        elif was_connected:
            reason = self._last_command_socket_error or str(detail or "")
            self._queue_unexpected_obs_disconnect(
                "COMMAND",
                reason,
            )

    def on_data_thread_finished(
        self,
    ) -> None:

        self.data_thread = None

        self._set_data_connection_status(
            False
        )

        self._refresh_obs_button_state()

    def on_command_thread_finished(
        self,
    ) -> None:

        self.command_thread = None

        self._set_command_connection_status(
            False
        )

        self._refresh_obs_button_state()

    def _refresh_obs_button_state(
        self,
    ) -> None:

        if self._obs_workers_running():
            self.obs_connect_button.setText(
                "Disconnect OBS"
            )

            self._set_obs_fields_enabled(
                False
            )

        else:
            self.obs_connect_button.setText(
                "Connect OBS"
            )

            self._set_obs_fields_enabled(
                True
            )

    def on_stream_status(
        self,
        raw_rate_hz: float,
        output_rate_hz: float,
        dropped_frames: int,
        channel_mismatches: int,
        error_words: int,
        parser_resyncs: int,
    ) -> None:

        try:
            stream_info = (
                self.shared.read_adc_stream_info()
            )

            configured_rate = (
                stream_info.effective_sample_rate_hz
            )

            configured_n = (
                stream_info.decimation_samples
            )

        except Exception:
            configured_rate = (
                self._expected_adc_source_rate_hz()
                / max(
                    1,
                    int(
                        self.decimation_spin.value()
                    ),
                )
            )

            configured_n = (
                self.decimation_spin.value()
            )

        try:
            rate_diag = self.shared.read_adc_rate_diagnostic()
            if rate_diag.ready:
                diagnostic_text = (
                    f"clock est {rate_diag.measured_raw_sample_rate_hz:,.3f} Hz "
                    f"({rate_diag.error_ppm:+.0f} ppm, {rate_diag.status}, "
                    f"{rate_diag.measurement_window_s:.0f}s)"
                )
            else:
                diagnostic_text = "clock est WARMING UP"
        except Exception:
            diagnostic_text = "clock est unavailable"

        self.data_rate_label.setText(
            (
                f"RX short: {raw_rate_hz:,.1f} raw/s | "
                f"output: {output_rate_hz:,.1f}/s "
                f"(expected {configured_rate:,.1f}) | "
                f"{diagnostic_text} | "
                f"avg N={configured_n} | "
                f"drop: {dropped_frames} | "
                f"sync: {channel_mismatches} | "
                f"error: {error_words} | "
                f"parser: {parser_resyncs}"
            )
        )

    # -------------------------------------------------------------------------
    # Command-channel telemetry
    # -------------------------------------------------------------------------

    def on_obs_sentence(
        self,
        parsed: dict,
    ) -> None:

        message_id = str(
            parsed.get(
                "id",
                "",
            )
        ).upper()

        fields = list(
            parsed.get(
                "fields",
                [],
            )
        )

        timestamp_ns = int(
            parsed.get(
                "timestamp_ns",
                time.time_ns(),
            )
        )

        try:
            self.shared.update_acquisition_health(
                last_command_rx_ns=timestamp_ns
            )
        except Exception:
            pass

        try:

            if message_id == "STRG0":
                message = ",".join(fields).strip()
                raw_sentence = str(parsed.get("raw") or "")
                event_type, detail = classify_strg0_message(message)
                updates = {
                    "last_message": message,
                    "last_raw_sentence": raw_sentence,
                    "timestamp_ns": timestamp_ns,
                }

                if event_type == "MEDIA_INSERTION":
                    updates["media_present"] = True
                elif event_type == "MEDIA_REMOVAL":
                    updates.update(
                        media_present=False,
                        logger_active=False,
                        current_filename="",
                    )
                elif event_type in ("DEVICE_CONNECTION", "DEVICE_INSERTION"):
                    updates["device_present"] = True
                elif event_type in ("DEVICE_DISCONNECTION", "NO_DEVICE"):
                    updates.update(
                        device_present=False,
                        media_present=False,
                        logger_active=False,
                        current_filename="",
                    )
                elif event_type == "ENUMERATION_FAILURE":
                    updates.update(
                        device_present=True,
                        media_present=False,
                        logger_active=False,
                    )
                elif event_type == "MSC_ID":
                    updates["device_present"] = True
                elif event_type == "FILE_OPENED":
                    updates.update(
                        device_present=True,
                        media_present=True,
                        logger_active=True,
                        current_filename=detail or "(unnamed)",
                    )
                elif event_type == "FILE_CLOSED":
                    updates.update(
                        logger_active=False,
                        current_filename="",
                        last_closed_filename=detail,
                    )

                self.shared.update_usb_logger_status(**updates)
                self.command_rx_label.setText(f"STRG0: {message[:70]}")

            elif message_id == "AHRS2":
                if len(fields) < 7:
                    raise ValueError(
                        "AHRS2 requires 7 fields."
                    )

                # Firmware transmits attitude angles as E1 values:
                # wire value = physical degrees * 10.
                roll_raw = float(fields[0])
                pitch_raw = float(fields[1])
                yaw_raw = float(fields[2])

                roll_deg = roll_raw / 10.0
                pitch_deg = pitch_raw / 10.0
                yaw_raw_deg = yaw_raw / 10.0
                # v20: one authoritative system yaw for every consumer. The
                # installation/reference offset is applied here, before the
                # value enters shared_data.
                yaw_deg = wrap_angle_deg(
                    yaw_raw_deg + float(self.imu_yaw_offset_deg)
                )

                # No revised scale factor was supplied for P/Q/R. Keep the
                # established interpretation instead of guessing.
                rate_p = float(fields[3])
                rate_q = float(fields[4])
                rate_r = float(fields[5])

                self.shared.update_acquisition_health(last_ahrs_ns=timestamp_ns)
                self.shared.update_telemetry(
                    timestamp_ns=timestamp_ns,
                    roll=roll_deg,
                    pitch=pitch_deg,
                    yaw=yaw_deg,
                    angular_rate_p=rate_p,
                    angular_rate_q=rate_q,
                    angular_rate_r=rate_r,
                    ahrs_device_id=parse_int_auto(
                        fields[6]
                    ),
                )

                self.command_rx_label.setText(
                    (
                        "AHRS2: "
                        f"R {roll_deg:.1f}° | "
                        f"P {pitch_deg:.1f}° | "
                        f"Y {yaw_deg:.1f}° "
                        f"(raw {yaw_raw_deg:.1f}° + offset {self.imu_yaw_offset_deg:+.1f}°)"
                    )
                )

            elif message_id == "DEPT0":
                if len(fields) < 3:
                    raise ValueError(
                        "DEPT0 requires 3 fields."
                    )

                # Updated firmware scaling supplied for this build:
                # pressure = field0 / 10, temperature = field2 / 100.
                # The exact physical meaning/scale of field1 was not revised,
                # so retain its previous depth-rate slot for compatibility.
                pressure_raw = float(fields[0])
                rate_raw = float(fields[1])
                temperature_raw = float(fields[2])

                pressure_mbar = pressure_raw / 10.0
                temperature_c = temperature_raw / 100.0

                self.shared.update_acquisition_health(last_depth_ns=timestamp_ns)
                self.shared.update_telemetry(
                    timestamp_ns=timestamp_ns,
                    pressure=pressure_mbar,
                    depth_rate=rate_raw,
                    temperature=temperature_c,
                )

                self.command_rx_label.setText(
                    (
                        "DEPT0: "
                        f"pressure {pressure_mbar:.1f} mbar | "
                        f"field1 {rate_raw:.3f} | "
                        f"temp {temperature_c:.2f} °C"
                    )
                )

            elif message_id == "XCHM1":
                if len(fields) != 11:
                    raise ValueError("XCHM1 requires exactly 11 fields.")

                values = [parse_int_auto(field) for field in fields]
                (vmoni0, vmoni1, vmoni2, vmoni3, temp_raw, pmon_raw,
                 hmon_raw, lmon, oasm, device_id, count) = values

                if not (0 <= lmon <= 0xFF and 0 <= oasm <= 0xFF):
                    raise ValueError("XCHM1 LMON/OASM must be 8-bit values.")
                if not (0 <= device_id <= 0xFF and 0 <= count <= 0xFF):
                    raise ValueError("XCHM1 ID/COUNT must be 8-bit values.")

                # Publish the complete controller-health packet into the
                # centralized shared-RAM pool. No consumer opens another OBS
                # command socket.
                self.shared.update_acquisition_health(last_controller_ns=timestamp_ns)
                self.shared.update_controller_telemetry(
                    timestamp_ns=timestamp_ns,
                    vmoni0_raw=vmoni0, vmoni1_raw=vmoni1,
                    vmoni2_raw=vmoni2, vmoni3_raw=vmoni3,
                    temp_raw=temp_raw, pmon_raw=pmon_raw, hmon_raw=hmon_raw,
                    lmon=lmon, oasm=oasm, device_id=device_id, count=count,
                )

                # Keep the legacy compact controller-health slot synchronized
                # for older readers that only know XCHM0-style ID/OASM.
                self.shared.update_controller_health(
                    timestamp_ns=timestamp_ns,
                    unit_id=device_id,
                    processor_status_code=oasm,
                )

                self.command_rx_label.setText(
                    f"XCHM1: V0 {vmoni0/10.0:.1f} V | "
                    f"V1 {vmoni1/10.0:.1f} V | "
                    f"LMON 0x{lmon:02X} | OASM 0x{oasm:02X}"
                )

            elif message_id == "TIME1":
                if len(fields) < 8:
                    raise ValueError(
                        "TIME1 requires 8 fields."
                    )

                values = [
                    int(
                        item,
                        10,
                    )
                    for item in fields[
                        :8
                    ]
                ]

                self.shared.update_device_time(
                    timestamp_ns=timestamp_ns,
                    milliseconds=values[0],
                    year=values[1],
                    month=values[2],
                    week=values[3],
                    date=values[4],
                    hours=values[5],
                    minutes=values[6],
                    seconds=values[7],
                )

                self.command_rx_label.setText(
                    (
                        "TIME1: "
                        f"{values[1]:04d}-"
                        f"{values[2]:02d}-"
                        f"{values[4]:02d} "
                        f"{values[5]:02d}:"
                        f"{values[6]:02d}:"
                        f"{values[7]:02d}"
                    )
                )

            elif message_id == "GDAT2":
                if len(fields) < 11:
                    raise ValueError(
                        (
                            "GDAT2 requires "
                            "10 hex fields + counter."
                        )
                    )

                diagnostic_fields = [
                    int(
                        field.strip(),
                        16,
                    )
                    for field in fields[
                        :10
                    ]
                ]

                counter = parse_int_auto(
                    fields[10]
                )

                self.shared.update_diagnostic(
                    diagnostic_fields,
                    counter=counter,
                    timestamp_ns=timestamp_ns,
                )

                self.command_rx_label.setText(
                    (
                        "GDAT2: "
                        f"counter {counter}"
                    )
                )

            elif message_id == "XCHM0":
                if len(fields) < 2:
                    raise ValueError(
                        "XCHM0 requires unit ID + processor status."
                    )

                unit_id = parse_int_auto(
                    fields[0]
                )

                status_code = parse_int_auto(
                    fields[1]
                )

                self.shared.update_controller_health(
                    unit_id=unit_id,
                    processor_status_code=status_code,
                    timestamp_ns=timestamp_ns,
                )

                self.command_rx_label.setText(
                    (
                        "XCHM0: "
                        f"unit {unit_id} | "
                        f"status 0x{status_code:X}"
                    )
                )

            elif message_id == "XFWVR":
                summary = ",".join(
                    fields
                )

                self.shared.update_firmware_info(
                    summary,
                    timestamp_ns=timestamp_ns,
                )

                self.command_rx_label.setText(
                    (
                        "XFWVR: "
                        f"{summary[:60]}"
                    )
                )

            else:
                self.command_rx_label.setText(
                    (
                        f"{message_id}: "
                        f"{','.join(fields)[:65]}"
                    )
                )

            self._set_footer(
                f"COMMAND RX: ${message_id}"
            )

        except (
            ValueError,
            TypeError,
            IndexError,
        ) as exc:

            LOGGER.warning(
                "Failed parsing %s: %s",
                message_id,
                exc,
            )

            self._set_footer(
                (
                    f"Invalid ${message_id} "
                    f"telemetry: {exc}"
                ),
                6000,
            )

    # -------------------------------------------------------------------------
    # Gimbal / power
    # -------------------------------------------------------------------------

    def _update_command_buttons(
        self,
    ) -> None:

        # Buttons remain available only when the command TCP link exists.
        enabled = (
            self.command_connected
        )

        self.lock_gimbal_button.setEnabled(
            enabled
        )

        self.unlock_gimbal_button.setEnabled(
            enabled
        )

        self.low_power_button.setEnabled(
            enabled
        )

        self.normal_power_button.setEnabled(
            enabled
        )

    def _set_gimbal_status(
        self,
        locked: bool,
        *,
        source: str,
    ) -> None:

        self.gimbal_locked = bool(
            locked
        )

        self.shared.update_telemetry(
            gimbal_locked=self.gimbal_locked
        )

        if locked:
            self.gimbal_indicator.set_state(
                "GIMBAL LOCKED",
                "good",
            )
        else:
            self.gimbal_indicator.set_state(
                "GIMBAL UNLOCKED",
                "warning",
            )

        if source == "feedback":
            detail = (
                "State confirmed by OBS feedback."
            )
        elif source == "command":
            detail = (
                "Command queued to OBS. "
                "LOCK HIGH = $RMCMD,2*4B; "
                "LOCK LOW = $RMCMD,6*4F. "
                "No separate hardware acknowledgement is defined."
            )
        else:
            detail = (
                "Default display state. Protocol mapping: "
                "LOCK HIGH = RMCMD 2, LOCK LOW = RMCMD 6."
            )

        self.gimbal_detail.setText(
            detail
        )

    def _set_power_status(
        self,
        mode: str,
        *,
        source: str,
    ) -> None:

        mode = (
            str(mode)
            .strip()
            .upper()
        )

        if mode not in (
            "LOW",
            "NORMAL",
        ):
            return

        self.power_mode = mode

        self.shared.update_telemetry(
            power_mode=mode
        )

        if mode == "LOW":
            self.power_indicator.set_state(
                "LOW POWER",
                "warning",
            )
        else:
            self.power_indicator.set_state(
                "NORMAL MODE",
                "good",
            )

        if source == "feedback":
            detail = (
                "State confirmed by OBS feedback."
            )
        elif source == "command":
            detail = (
                "Command queued to OBS. LOW POWER uses "
                "PWR_MODE HIGH = $RMCMD,1*48; NORMAL MODE uses "
                "PWR_MODE LOW = $RMCMD,5*4C. "
                "No separate hardware acknowledgement is defined."
            )
        else:
            detail = (
                "Default LOW POWER display state. Button mapping: "
                "LOW POWER -> RMCMD 1; NORMAL MODE -> RMCMD 5."
            )

        self.power_detail.setText(
            detail
        )

    def _send_remote_gpio_command(
        self,
        command_code: int,
        description: str,
        *,
        gimbal_locked: Optional[bool] = None,
        power_mode: Optional[str] = None,
    ) -> None:
        """Queue one firmware-defined $RMCMD command on TCP port 54300."""

        worker = self.command_thread

        if (
            not self.command_connected
            or worker is None
            or not worker.isRunning()
        ):
            message = (
                "OBS command port is not connected.\n\n"
                "Connect the COMMAND channel (TCP 54300) before sending "
                "Gimbal or Power commands."
            )
            self._set_footer(
                "OBS command port is not connected."
            )
            QMessageBox.warning(
                self,
                "OBS Command Not Connected",
                message,
            )
            return

        try:
            sentence = build_remote_gpio_sentence(
                command_code
            )
        except Exception as exc:
            message = (
                str(exc).strip()
                or "Unknown error while building the OBS command."
            )
            LOGGER.exception(
                "Failed to build remote GPIO command."
            )
            QMessageBox.warning(
                self,
                description,
                message,
            )
            return

        try:
            worker.send_sentence(
                sentence
            )
        except Exception as exc:
            message = (
                str(exc).strip()
                or "Unknown error while queuing the OBS command."
            )
            LOGGER.exception(
                "Failed to queue remote GPIO command."
            )
            QMessageBox.warning(
                self,
                description,
                message,
            )
            return

        LOGGER.info(
            "REMOTE GPIO TX queued: code=%d description=%s sentence=%r",
            int(command_code),
            description,
            sentence,
        )

        # Commanded/requested state only. The supplied firmware protocol has no
        # separate acknowledgement/state-feedback sentence for these GPIOs.
        if gimbal_locked is not None:
            self._set_gimbal_status(
                bool(gimbal_locked),
                source="command",
            )

        if power_mode is not None:
            self._set_power_status(
                power_mode,
                source="command",
            )

        self._set_footer(
            (
                f"Sent {description}: "
                f"{sentence.decode('ascii').strip()}"
            ),
            5000,
        )

    # -------------------------------------------------------------------------
    # GNSS / USBL freshness
    # -------------------------------------------------------------------------

    @staticmethod
    def _position_snapshot_age_s(snapshot, now_ns: int) -> float:
        timestamp_ns = int(getattr(snapshot, "timestamp_ns", 0) or 0)
        if timestamp_ns <= 0:
            return float("inf")
        return max(
            0.0,
            (int(now_ns) - timestamp_ns) / 1_000_000_000.0,
        )

    @staticmethod
    def _cache_gga_snapshot(snapshot) -> dict:
        return {
            "timestamp_ns": int(getattr(snapshot, "timestamp_ns", 0) or 0),
            "latitude": float(getattr(snapshot, "latitude", 0.0)),
            "longitude": float(getattr(snapshot, "longitude", 0.0)),
            "altitude": float(getattr(snapshot, "altitude", 0.0)),
            "fix_quality": int(getattr(snapshot, "fix_quality", 0) or 0),
            "satellites": int(getattr(snapshot, "satellites", 0) or 0),
            "hdop": float(getattr(snapshot, "hdop", 0.0)),
        }

    def _navigation_transport_text(self, mode_combo: QComboBox) -> str:
        mode = mode_combo.currentText().strip()
        return "UDP LISTENING" if mode == "UDP" else "COM OPEN"

    def _refresh_navigation_source(
        self,
        *,
        source: str,
        connected: bool,
        mode_combo: QComboBox,
        indicator: StatusPill,
        label: QLabel,
        nmea_count: int,
        snapshot,
        source_age_s: float,
        last_valid: Optional[dict],
    ) -> Optional[dict]:
        """Render transport/freshness/fix state without modifying raw shared GGA."""
        now_ns = time.time_ns()
        gga_age_s = self._position_snapshot_age_s(snapshot, now_ns)

        raw_valid = bool(
            getattr(snapshot, "valid", False)
            and int(getattr(snapshot, "fix_quality", 0) or 0) > 0
        )

        if raw_valid:
            # A stale valid snapshot is still the factual last-valid fix.
            last_valid = self._cache_gga_snapshot(snapshot)

        source_fresh = (
            math.isfinite(source_age_s)
            and source_age_s <= NMEA_SOURCE_STALE_S
        )
        gga_fresh = (
            math.isfinite(gga_age_s)
            and gga_age_s <= NMEA_SOURCE_STALE_S
        )
        effective_valid = bool(
            connected
            and source_fresh
            and gga_fresh
            and raw_valid
        )

        if connected:
            indicator.set_state(
                self._navigation_transport_text(mode_combo),
                "active",
            )
        else:
            indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

        if not connected:
            source_state = "NOT LISTENING"
            age_text = "--"
        elif not math.isfinite(source_age_s):
            source_state = "WAITING"
            age_text = "--"
        elif source_fresh:
            source_state = "LIVE"
            age_text = f"{source_age_s:.1f} s"
        else:
            source_state = "STALE"
            age_text = f"{source_age_s:.1f} s"

        if effective_valid:
            display = self._cache_gga_snapshot(snapshot)
            fix_quality = int(display["fix_quality"])
            sat_text = str(int(display["satellites"]))
            suffix = ""
        elif last_valid is not None:
            display = last_valid
            fix_quality = 0
            sat_text = str(int(display["satellites"]))
            suffix = " • LAST VALID POSITION"
        else:
            display = None
            fix_quality = 0
            sat_text = "--"
            suffix = " • NO VALID POSITION"

        if display is None:
            position_text = "GGA: --, --"
        else:
            position_text = (
                f"GGA: {float(display['latitude']):.7f}, "
                f"{float(display['longitude']):.7f}"
            )

        gga_state = (
            "LIVE"
            if effective_valid
            else (
                "STALE"
                if connected and (not source_fresh or not gga_fresh)
                else "NO FIX"
            )
        )

        label.setText(
            (
                f"NMEA: {source_state} • age {age_text} • count {int(nmea_count)}\n"
                f"{position_text} | Fix {fix_quality} | Sat {sat_text} "
                f"• GGA {gga_state}{suffix}"
            )
        )
        return last_valid

    def _refresh_nmea_freshness_status(self) -> None:
        try:
            health = self.shared.read_acquisition_health()
            gnss = self.shared.read_gnss()
            usbl = self.shared.read_usbl()

            self._gnss_last_valid_gga = self._refresh_navigation_source(
                source="GNSS",
                connected=bool(self.gnss_connected),
                mode_combo=self.gnss_mode_combo,
                indicator=self.gnss_connection_indicator,
                label=self.gnss_gga_label,
                nmea_count=self.gnss_nmea_count,
                snapshot=gnss,
                source_age_s=health.source_age_s("gnss"),
                last_valid=self._gnss_last_valid_gga,
            )

            self._usbl_last_valid_gga = self._refresh_navigation_source(
                source="USBL",
                connected=bool(self.usbl_connected),
                mode_combo=self.usbl_mode_combo,
                indicator=self.usbl_connection_indicator,
                label=self.usbl_gga_label,
                nmea_count=self.usbl_nmea_count,
                snapshot=usbl,
                source_age_s=health.source_age_s("usbl"),
                last_valid=self._usbl_last_valid_gga,
            )
        except Exception as exc:
            LOGGER.debug(
                "GNSS/USBL freshness refresh failed: %s",
                exc,
            )

    # -------------------------------------------------------------------------
    # GNSS
    # -------------------------------------------------------------------------

    def refresh_com_ports(
        self,
    ) -> None:

        gnss_current = ""

        if hasattr(
            self,
            "gnss_com_combo",
        ):
            gnss_current = (
                self.gnss_com_combo
                .currentText()
                .strip()
            )

        usbl_current = ""

        if hasattr(
            self,
            "usbl_com_combo",
        ):
            usbl_current = (
                self.usbl_com_combo
                .currentText()
                .strip()
            )

        ports = available_serial_ports()

        self.gnss_com_combo.blockSignals(
            True
        )

        self.usbl_com_combo.blockSignals(
            True
        )

        self.gnss_com_combo.clear()
        self.usbl_com_combo.clear()

        self.gnss_com_combo.addItems(
            ports
        )

        self.usbl_com_combo.addItems(
            ports
        )

        self.gnss_com_combo.setCurrentText(
            gnss_current
            or DEFAULT_GNSS_COM
        )

        self.usbl_com_combo.setCurrentText(
            usbl_current
            or DEFAULT_USBL_COM
        )

        self.gnss_com_combo.blockSignals(
            False
        )

        self.usbl_com_combo.blockSignals(
            False
        )

        if hasattr(
            self,
            "footer_status",
        ):
            self._set_footer(
                (
                    f"COM ports refreshed "
                    f"({len(ports)})"
                )
            )

    def toggle_gnss_connection(
        self,
    ) -> None:

        if (
            self.gnss_thread is not None
            and self.gnss_thread.isRunning()
        ):
            self.disconnect_gnss()
        else:
            self.connect_gnss()

    def connect_gnss(
        self,
    ) -> None:

        mode = (
            self.gnss_mode_combo
            .currentText()
            .strip()
        )

        if mode == "COM Port":
            port = (
                self.gnss_com_combo
                .currentText()
                .strip()
            )

            if not port:
                QMessageBox.warning(
                    self,
                    "GNSS",
                    "GNSS COM Port cannot be empty.",
                )
                return

            try:
                baudrate = int(
                    self.gnss_baud_combo
                    .currentText()
                    .strip()
                )

            except ValueError:
                QMessageBox.warning(
                    self,
                    "GNSS",
                    "GNSS baudrate must be an integer.",
                )
                return

            worker: QThread = SerialNMEAThread(
                "GNSS",
                port,
                baudrate,
                self,
            )

        else:
            listen_ip = (
                self.gnss_udp_ip_edit
                .text()
                .strip()
                or DEFAULT_GNSS_UDP_IP
            )

            udp_port = int(
                self.gnss_udp_port_spin.value()
            )

            worker = UDPNMEAThread(
                "GNSS",
                listen_ip,
                udp_port,
                self,
            )

        self.gnss_nmea_count = 0
        self.gnss_gga_label.setText(
            "NMEA: WAITING • age -- • count 0\nGGA: --, -- | Fix 0 • NO VALID POSITION"
        )

        self.gnss_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.gnss_connect_button.setText(
            "Disconnect GNSS"
        )

        self.gnss_thread = worker

        worker.connection_changed.connect(
            self.on_gnss_connection_changed
        )

        worker.gga_received.connect(
            self.on_gnss_gga
        )

        worker.sentence_received.connect(
            self.on_gnss_sentence
        )

        worker.io_error.connect(
            lambda message:
            self._set_footer(
                f"GNSS error: {message}",
                7000,
            )
        )

        worker.finished.connect(
            self.on_gnss_thread_finished
        )

        worker.start()
        self.update_gnss_connection_fields()

    def disconnect_gnss(
        self,
    ) -> None:

        worker = self.gnss_thread

        if worker is not None:
            worker.stop()

        self._set_footer(
            "Disconnecting GNSS..."
        )

    def update_gnss_connection_fields(
        self,
    ) -> None:

        mode = (
            self.gnss_mode_combo
            .currentText()
            .strip()
        )

        use_com = (
            mode == "COM Port"
        )

        can_edit = not (
            self.gnss_thread is not None
            and self.gnss_thread.isRunning()
        )

        self.gnss_mode_combo.setEnabled(
            can_edit
        )

        self.gnss_com_widget.setEnabled(
            can_edit
            and use_com
        )

        self.gnss_baud_combo.setEnabled(
            can_edit
            and use_com
        )

        self.gnss_udp_ip_edit.setEnabled(
            can_edit
            and not use_com
        )

        self.gnss_udp_port_spin.setEnabled(
            can_edit
            and not use_com
        )

    def _set_gnss_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.gnss_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            gnss_connected=self.gnss_connected
        )

        if connected:
            self.gnss_connection_indicator.set_state(
                self._navigation_transport_text(self.gnss_mode_combo),
                "active",
            )
        else:
            self.gnss_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

    def on_gnss_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        self._set_gnss_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"GNSS transport open: {detail}"
            )

    def on_gnss_sentence(
        self,
        packet: dict,
    ) -> None:
        self.gnss_nmea_count += 1
        try:
            timestamp_ns = int(packet.get("timestamp_ns", time.time_ns()))
            self.shared.update_acquisition_health(last_gnss_ns=timestamp_ns)
            self.shared.update_gnss_nmea_sentence(
                str(
                    packet.get(
                        "raw",
                        "",
                    )
                ),
                timestamp_ns=timestamp_ns,
            )
        except Exception as exc:
            LOGGER.warning(
                "GNSS raw NMEA shared-RAM update failed: %s",
                exc,
            )

    def on_gnss_gga(
        self,
        gga: dict,
    ) -> None:

        self.shared.update_gnss(
            timestamp_ns=gga["timestamp_ns"],
            valid=gga["valid"],
            latitude=gga["latitude"],
            longitude=gga["longitude"],
            altitude=gga["altitude"],
            fix_quality=gga["fix_quality"],
            satellites=gga["satellites"],
            hdop=gga["hdop"],
        )

        if bool(gga.get("valid", False)):
            self._gnss_last_valid_gga = dict(gga)

        self._refresh_nmea_freshness_status()

    def on_gnss_thread_finished(
        self,
    ) -> None:

        self.gnss_thread = None

        self._set_gnss_connection_status(
            False
        )

        self.gnss_connect_button.setText(
            "Connect GNSS"
        )

        self.update_gnss_connection_fields()

    # -------------------------------------------------------------------------
    # USBL
    # -------------------------------------------------------------------------

    def update_usbl_connection_fields(
        self,
    ) -> None:

        mode = (
            self.usbl_mode_combo
            .currentText()
            .strip()
        )

        use_com = (
            mode == "COM Port"
        )

        can_edit = not (
            self.usbl_thread is not None
            and self.usbl_thread.isRunning()
        )

        self.usbl_mode_combo.setEnabled(
            can_edit
        )

        self.usbl_com_widget.setEnabled(
            can_edit
            and use_com
        )

        self.usbl_baud_combo.setEnabled(
            can_edit
            and use_com
        )

        self.usbl_udp_ip_edit.setEnabled(
            can_edit
            and not use_com
        )

        self.usbl_udp_port_spin.setEnabled(
            can_edit
            and not use_com
        )

    def toggle_usbl_connection(
        self,
    ) -> None:

        if (
            self.usbl_thread is not None
            and self.usbl_thread.isRunning()
        ):
            self.disconnect_usbl()
        else:
            self.connect_usbl()

    def connect_usbl(
        self,
    ) -> None:

        mode = (
            self.usbl_mode_combo
            .currentText()
            .strip()
        )

        if mode == "COM Port":

            port = (
                self.usbl_com_combo
                .currentText()
                .strip()
            )

            if not port:
                QMessageBox.warning(
                    self,
                    "USBL",
                    "USBL COM Port cannot be empty.",
                )
                return

            try:
                baudrate = int(
                    self.usbl_baud_combo
                    .currentText()
                    .strip()
                )

            except ValueError:
                QMessageBox.warning(
                    self,
                    "USBL",
                    "USBL baudrate must be an integer.",
                )
                return

            worker: QThread = (
                SerialNMEAThread(
                    "USBL",
                    port,
                    baudrate,
                    self,
                )
            )

        else:

            listen_ip = (
                self.usbl_udp_ip_edit
                .text()
                .strip()
                or DEFAULT_USBL_UDP_IP
            )

            udp_port = int(
                self.usbl_udp_port_spin.value()
            )

            worker = UDPNMEAThread(
                "USBL",
                listen_ip,
                udp_port,
                self,
            )

        self.usbl_nmea_count = 0
        self.usbl_gga_label.setText(
            "NMEA: WAITING • age -- • count 0\nGGA: --, -- | Fix 0 • NO VALID POSITION"
        )

        self.usbl_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.usbl_connect_button.setText(
            "Disconnect USBL"
        )

        self.usbl_thread = worker

        worker.connection_changed.connect(
            self.on_usbl_connection_changed
        )

        worker.gga_received.connect(
            self.on_usbl_gga
        )

        worker.sentence_received.connect(
            self.on_usbl_sentence
        )

        worker.io_error.connect(
            lambda message:
            self._set_footer(
                f"USBL error: {message}",
                7000,
            )
        )

        worker.finished.connect(
            self.on_usbl_thread_finished
        )

        self.update_usbl_connection_fields()

        worker.start()

    def disconnect_usbl(
        self,
    ) -> None:

        worker = (
            self.usbl_thread
        )

        if worker is not None:
            worker.stop()

        self._set_footer(
            "Disconnecting USBL..."
        )

    def _set_usbl_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.usbl_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            usbl_connected=self.usbl_connected
        )

        if connected:
            self.usbl_connection_indicator.set_state(
                self._navigation_transport_text(self.usbl_mode_combo),
                "active",
            )
        else:
            self.usbl_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

    def on_usbl_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        self._set_usbl_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"USBL transport open: {detail}"
            )

    def on_usbl_sentence(
        self,
        packet: dict,
    ) -> None:
        self.usbl_nmea_count += 1
        try:
            timestamp_ns = int(packet.get("timestamp_ns", time.time_ns()))
            self.shared.update_acquisition_health(last_usbl_ns=timestamp_ns)
            self.shared.update_usbl_nmea_sentence(
                str(
                    packet.get(
                        "raw",
                        "",
                    )
                ),
                timestamp_ns=timestamp_ns,
            )
        except Exception as exc:
            LOGGER.warning(
                "USBL raw NMEA shared-RAM update failed: %s",
                exc,
            )

    def on_usbl_gga(
        self,
        gga: dict,
    ) -> None:

        self.shared.update_usbl(
            timestamp_ns=gga["timestamp_ns"],
            valid=gga["valid"],
            latitude=gga["latitude"],
            longitude=gga["longitude"],
            altitude=gga["altitude"],
            fix_quality=gga["fix_quality"],
            satellites=gga["satellites"],
            hdop=gga["hdop"],
        )

        if bool(gga.get("valid", False)):
            self._usbl_last_valid_gga = dict(gga)

        self._refresh_nmea_freshness_status()

    def on_usbl_thread_finished(
        self,
    ) -> None:

        self.usbl_thread = None

        self._set_usbl_connection_status(
            False
        )

        self.usbl_connect_button.setText(
            "Connect USBL"
        )

        self.update_usbl_connection_fields()

    # -------------------------------------------------------------------------
    # Folder
    # -------------------------------------------------------------------------

    def browse_record_folder(
        self,
    ) -> None:

        current = (
            self.record_folder_edit
            .text()
            .strip()
            or DEFAULT_RECORD_FOLDER
        )

        selected = (
            QFileDialog.getExistingDirectory(
                self,
                "Select MiniSEED Record Folder",
                current,
            )
        )

        if selected:
            self.record_folder_edit.setText(
                selected
            )

    # -------------------------------------------------------------------------
    # INI
    # -------------------------------------------------------------------------

    def save_settings(
        self,
    ) -> None:

        record_folder = (
            self.record_folder_edit
            .text()
            .strip()
            or DEFAULT_RECORD_FOLDER
        )

        try:
            Path(
                record_folder
            ).expanduser().mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as exc:
            QMessageBox.warning(
                self,
                "Recording Folder",
                (
                    "Cannot create/access "
                    f"recording folder:\n\n{exc}"
                ),
            )
            return

        config = (
            configparser.ConfigParser()
        )

        config["Network"] = {
            "ip": (
                self.ip_edit
                .text()
                .strip()
            ),
            "command_port": str(
                self.command_port_spin.value()
            ),
            "data_port": str(
                self.data_port_spin.value()
            ),
        }

        decimation_samples = max(
            1,
            int(
                self.decimation_spin.value()
            ),
        )

        raw_sample_rate_hz = (
            self._expected_adc_source_rate_hz()
        )

        result_rate_hz = (
            float(raw_sample_rate_hz)
            / float(decimation_samples)
        )

        config["Geophone"] = {
            # Read-only firmware contract recorded for audit; load_settings()
            # never treats this as operator configuration.
            "expected_raw_sample_rate_hz": f"{raw_sample_rate_hz:.6f}",
            # Primary decimation setting.
            "decimation_samples": str(
                decimation_samples
            ),
            # Derived compatibility/display value.
            "decimation_rate_hz": (
                f"{result_rate_hz:.6f}"
            ),
        }

        config["GNSS"] = {
            "connection": (
                self.gnss_mode_combo
                .currentText()
                .strip()
            ),
            "com_port": (
                self.gnss_com_combo
                .currentText()
                .strip()
            ),
            "baudrate": (
                self.gnss_baud_combo
                .currentText()
                .strip()
            ),
            "udp_ip": (
                self.gnss_udp_ip_edit
                .text()
                .strip()
            ),
            "udp_port": str(
                self.gnss_udp_port_spin.value()
            ),
        }

        config["USBL"] = {
            "connection": (
                self.usbl_mode_combo
                .currentText()
                .strip()
            ),
            "com_port": (
                self.usbl_com_combo
                .currentText()
                .strip()
            ),
            "baudrate": (
                self.usbl_baud_combo
                .currentText()
                .strip()
            ),
            "udp_ip": (
                self.usbl_udp_ip_edit
                .text()
                .strip()
            ),
            "udp_port": str(
                self.usbl_udp_port_spin.value()
            ),
        }

        config["Recording"] = {
            "miniseed_folder": (
                record_folder
            ),
        }

        config["OBS_Commands"] = {
            key: value
            for key, value
            in self.command_templates.items()
        }

        try:
            with INI_PATH.open(
                "w",
                encoding="utf-8",
            ) as handle:
                config.write(
                    handle
                )

        except Exception as exc:
            LOGGER.exception(
                "Failed to save INI."
            )

            QMessageBox.critical(
                self,
                "Save Settings",
                (
                    "Failed to save settings:\n\n"
                    f"{exc}"
                ),
            )
            return

        self._set_footer(
            "Settings saved"
        )

        QMessageBox.information(
            self,
            "Save Settings",
            "OBS settings saved successfully.",
        )

    def load_settings(
        self,
        *,
        show_message: bool = False,
    ) -> None:

        config = (
            configparser.ConfigParser()
        )

        if not INI_PATH.exists():
            self._apply_default_settings()

            if show_message:
                QMessageBox.information(
                    self,
                    "Load Settings",
                    (
                        "obs_settings.ini not found. "
                        "Defaults loaded."
                    ),
                )

            return

        try:
            config.read(
                INI_PATH,
                encoding="utf-8",
            )

            self.ip_edit.setText(
                config.get(
                    "Network",
                    "ip",
                    fallback=DEFAULT_IP,
                )
            )

            self.command_port_spin.setValue(
                config.getint(
                    "Network",
                    "command_port",
                    fallback=DEFAULT_COMMAND_PORT,
                )
            )

            self.data_port_spin.setValue(
                config.getint(
                    "Network",
                    "data_port",
                    fallback=DEFAULT_DATA_PORT,
                )
            )

            # v28: raw ADC source rate is a corrected-firmware contract, not an
            # operator setting. Any legacy raw_sample_rate_hz entry is ignored.
            raw_sample_rate_hz = float(EXPECTED_ADC_SOURCE_RATE_HZ)

            if config.has_option(
                "Geophone",
                "decimation_samples",
            ):
                decimation_samples = (
                    config.getint(
                        "Geophone",
                        "decimation_samples",
                        fallback=(
                            DEFAULT_DECIMATION_SAMPLES
                        ),
                    )
                )
            else:
                # Legacy configuration stored desired output rate in Hz.
                legacy_rate_hz = (
                    config.getfloat(
                        "Geophone",
                        "decimation_rate_hz",
                        fallback=(
                            DEFAULT_DECIMATION_RATE_HZ
                        ),
                    )
                )

                if legacy_rate_hz > 0.0:
                    decimation_samples = int(
                        round(
                            float(
                                raw_sample_rate_hz
                            )
                            / legacy_rate_hz
                        )
                    )
                else:
                    decimation_samples = (
                        DEFAULT_DECIMATION_SAMPLES
                    )

            self.decimation_spin.setValue(
                max(
                    1,
                    min(
                        max(1, int(math.ceil(raw_sample_rate_hz))),
                        int(
                            decimation_samples
                        ),
                    ),
                )
            )

            self.update_decimation_result()

            self.gnss_mode_combo.setCurrentText(
                config.get(
                    "GNSS",
                    "connection",
                    fallback=DEFAULT_GNSS_MODE,
                )
            )

            self.gnss_com_combo.setCurrentText(
                config.get(
                    "GNSS",
                    "com_port",
                    fallback=DEFAULT_GNSS_COM,
                )
            )

            self.gnss_baud_combo.setCurrentText(
                config.get(
                    "GNSS",
                    "baudrate",
                    fallback=str(
                        DEFAULT_GNSS_BAUD
                    ),
                )
            )

            self.gnss_udp_ip_edit.setText(
                config.get(
                    "GNSS",
                    "udp_ip",
                    fallback=DEFAULT_GNSS_UDP_IP,
                )
            )

            self.gnss_udp_port_spin.setValue(
                config.getint(
                    "GNSS",
                    "udp_port",
                    fallback=DEFAULT_GNSS_UDP_PORT,
                )
            )

            self.usbl_mode_combo.setCurrentText(
                config.get(
                    "USBL",
                    "connection",
                    fallback=DEFAULT_USBL_MODE,
                )
            )

            self.usbl_com_combo.setCurrentText(
                config.get(
                    "USBL",
                    "com_port",
                    fallback=DEFAULT_USBL_COM,
                )
            )

            self.usbl_baud_combo.setCurrentText(
                config.get(
                    "USBL",
                    "baudrate",
                    fallback=str(
                        DEFAULT_USBL_BAUD
                    ),
                )
            )

            self.usbl_udp_ip_edit.setText(
                config.get(
                    "USBL",
                    "udp_ip",
                    fallback=DEFAULT_USBL_UDP_IP,
                )
            )

            self.usbl_udp_port_spin.setValue(
                config.getint(
                    "USBL",
                    "udp_port",
                    fallback=DEFAULT_USBL_UDP_PORT,
                )
            )

            self.record_folder_edit.setText(
                config.get(
                    "Recording",
                    "miniseed_folder",
                    fallback=DEFAULT_RECORD_FOLDER,
                )
            )

            for key in (
                "gimbal_lock",
                "gimbal_unlock",
                "power_low",
                "power_normal",
            ):
                self.command_templates[key] = (
                    config.get(
                        "OBS_Commands",
                        key,
                        fallback="",
                    )
                    .strip()
                )

        except Exception as exc:
            LOGGER.exception(
                "Failed to load INI."
            )

            QMessageBox.warning(
                self,
                "Load Settings",
                (
                    "Failed to read obs_settings.ini.\n"
                    "Defaults will be used.\n\n"
                    f"{exc}"
                ),
            )

            self._apply_default_settings()
            return

        self.update_gnss_connection_fields()
        self.update_usbl_connection_fields()

        if hasattr(
            self,
            "footer_status",
        ):
            self._set_footer(
                "Settings loaded"
            )

        if show_message:
            QMessageBox.information(
                self,
                "Load Settings",
                "OBS settings loaded successfully.",
            )

    def _apply_default_settings(
        self,
    ) -> None:

        self.ip_edit.setText(
            DEFAULT_IP
        )

        self.command_port_spin.setValue(
            DEFAULT_COMMAND_PORT
        )

        self.data_port_spin.setValue(
            DEFAULT_DATA_PORT
        )

        self.decimation_spin.setValue(
            DEFAULT_DECIMATION_SAMPLES
        )

        self.update_decimation_result()

        self.gnss_mode_combo.setCurrentText(
            DEFAULT_GNSS_MODE
        )

        self.gnss_com_combo.setCurrentText(
            DEFAULT_GNSS_COM
        )

        self.gnss_baud_combo.setCurrentText(
            str(
                DEFAULT_GNSS_BAUD
            )
        )

        self.gnss_udp_ip_edit.setText(
            DEFAULT_GNSS_UDP_IP
        )

        self.gnss_udp_port_spin.setValue(
            DEFAULT_GNSS_UDP_PORT
        )

        self.usbl_mode_combo.setCurrentText(
            DEFAULT_USBL_MODE
        )

        self.usbl_com_combo.setCurrentText(
            DEFAULT_USBL_COM
        )

        self.usbl_baud_combo.setCurrentText(
            str(
                DEFAULT_USBL_BAUD
            )
        )

        self.usbl_udp_ip_edit.setText(
            DEFAULT_USBL_UDP_IP
        )

        self.usbl_udp_port_spin.setValue(
            DEFAULT_USBL_UDP_PORT
        )

        self.record_folder_edit.setText(
            DEFAULT_RECORD_FOLDER
        )

        self.command_templates = {
            "gimbal_lock": "",
            "gimbal_unlock": "",
            "power_low": "",
            "power_normal": "",
        }

        self.update_usbl_connection_fields()

    # -------------------------------------------------------------------------
    # Close / acquisition-manager lifecycle
    # -------------------------------------------------------------------------

    def request_final_shutdown(self, reason: str = "application shutdown") -> None:
        """Request a real process shutdown.

        A title-bar close is deliberately NOT a final shutdown because this
        window owns the centralized OBS acquisition links. The main launcher
        uses the local IPC shutdown request to enter this path.
        """
        if self._final_shutdown_requested:
            return
        self._final_shutdown_requested = True
        self._shutdown_reason = str(reason or "application shutdown")
        self._set_footer(
            f"Shutting down OBS acquisition: {self._shutdown_reason}",
            0,
        )
        self.close()

    def closeEvent(
        self,
        event: QCloseEvent,
    ) -> None:

        # IMPORTANT: OBS Setting is the centralized acquisition manager.
        # Clicking X only minimizes the GUI; it must not tear down 54300/54301.
        if not self._final_shutdown_requested:
            event.ignore()
            self._set_footer(
                "OBS Setting minimized; acquisition remains active. "
                "Use Disconnect OBS or close the main launcher to disconnect.",
                0,
            )
            self.showMinimized()
            return

        workers = [
            self.data_thread,
            self.command_thread,
            self.gnss_thread,
            self.usbl_thread,
        ]

        for worker in workers:
            if worker is not None:
                try:
                    worker.stop()
                except Exception:
                    pass

        for worker in workers:
            if worker is not None:
                try:
                    worker.wait(
                        1500
                    )
                except Exception:
                    pass

        try:
            self.heartbeat_timer.stop()
        except Exception:
            pass

        try:
            self.nmea_freshness_timer.stop()
        except Exception:
            pass

        self.shared.update_telemetry(
            data_connected=False,
            command_connected=False,
            gnss_connected=False,
            usbl_connected=False,
        )

        if self.command_broker_ready:
            try:
                self.shared.update_acquisition_health(
                    process_id=0,
                    command_connected=False,
                    data_connected=False,
                    broker_running=False,
                    timestamp_ns=time.time_ns(),
                )
            except Exception:
                pass

        try:
            if not self.command_broker_ready:
                raise RuntimeError("Not the active OBS command-broker owner")
            self.shared.update_command_broker_status(
                broker_running=False,
                obs_command_connected=False,
                last_result="STOPPED",
            )
            self.command_broker.stop()
        except Exception:
            pass

        self.shared.close()

        event.accept()


# =============================================================================
# Main
# =============================================================================

def main() -> int:

    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        APP_TITLE
    )

    app.setApplicationDisplayName(
        (
            f"{APP_TITLE} - "
            f"{SYSTEM_TITLE}"
        )
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

    window = OBSSettingWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(
            main()
        )

    except SystemExit:
        raise

    except Exception:
        LOGGER.exception(
            "Unhandled fatal error in obs_setting.py"
        )
        raise
