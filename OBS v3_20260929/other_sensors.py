"""
other_sensors_v5.py
====================

GRC-UGM-PERTAMINA OBS
Other Sensors Monitor

Version: 9
Shared data: shared_data.py (API v10)
Controller health source: centralized shared RAM populated by OBS Setting

Version 9 raw/shared attitude update
------------------------------------
- Removes Roll/Pitch/Yaw offset controls and imu_offsets.ini dependency.
- Other Sensors now displays Roll/Pitch/Yaw directly from shared_data.py.
- The 3D attitude model uses the same unmodified telemetry values.
- Uses runtime canonical import: from shared_data import ...

Version 8 reliable IMU offset spin-box controls
------------------------------------------------
- Replaces the three native QDoubleSpinBox IMU offset controls with
  ReliableDoubleSpinBox.
- The right-side spinner hit area is explicitly split into an upper increment
  zone and a lower decrement zone, avoiding the Qt/Windows styling issue where
  the visible UP arrow can fail to trigger a step.
- Applies to Roll Offset, Pitch Offset and Yaw Offset.
- Keyboard entry, keyboard arrows, mouse wheel, decimals, suffix, ranges,
  valueChanged handling and automatic imu_offsets.ini persistence are unchanged.

Version 7 IMU offset update
---------------------------
- Based operationally on other_sensors_v5.py, as requested.
- Adds independent Roll, Pitch and Yaw offsets in degrees.
- Offsets are applied to the physical AHRS values already published by
  obs_setting_v19.py after the firmware E1 (/10) conversion.
- The corrected Roll/Pitch/Yaw values drive the numeric display and 3D model.
- Offsets are persisted in imu_offsets.ini, separate from obs_settings.ini so an
  OBS Setting "Save Settings" operation cannot erase the IMU calibration.
- miniseed_recording_v18.py reads the same imu_offsets.ini so its
  obs_position_attitude_*.csv stores the same offset-corrected attitude values.
- The authoritative raw/E1-scaled values in shared_data.py are not modified.

Version 5 depth-unit correction
-------------------------------
- $DEPT0 field 0 is displayed as Depth in meters (m), not pressure in mbar.
- The legacy shared-memory attribute name telemetry.pressure is retained only
  for binary/API compatibility; Other Sensors interprets that value as depth.
- No additional scaling is applied in this GUI. The value is consumed exactly
  as published by OBS Setting.

Version 4 centralized-pool update
---------------------------------
- Opens NO TCP/UDP connection to the OBS. All sensor data is read from the
  centralized shared RAM populated by obs_setting_v19.
- $AHRS2 IMU, $DEPT0 depth/temperature and $XCHM1 controller-health/leak
  therefore share the same authoritative acquisition connection.
- Fully decodes the supplied $XCHM1 LMON leak bitfield:
    bit0 = leak sensor 0
    bit1 = leak sensor 1
    bit4 = sensor 0 fault/not-connected (also forces bit0 in firmware)
    bit5 = sensor 1 fault/not-connected (also forces bit1 in firmware)
    bits2/3/6/7 = reserved and expected to remain 0.
- Fully decodes the supplied OASM startup/reset bitfield:
    POR_RESET, BOR_RESET, WDT_RESET, S_X_RESET,
    OSC_ERROR, ADR_ERROR, STK_ERROR, MTH_ERROR.
- OASM remains a startup snapshot; it is not treated as continuously changing
  controller health.
- VMONI0/1 remain valid E1 rails (raw / 10). XCHM1 VMONI2/3, TEMP, PMON and
  HMON remain reserved/not-populated and are ignored as sensor readings.
- IMU and $DEPT0 physical-unit conversion is performed upstream by
  obs_setting_v19.py before values enter shared_data.py. Other Sensors therefore
  must NOT divide those shared values a second time.
- Version 7 applies optional Roll/Pitch/Yaw installation/reference offsets
  after the wire-scale conversion. All defaults are 0 degrees.
- Depth is displayed in meters from the legacy telemetry.pressure attribute,
  supplied by $DEPT0 field 0. The attribute name is retained for shared-memory
  compatibility. Temperature is supplied by $DEPT0 with its existing scaling.

Important yaw note
------------------
The existing 3D transform visual_yaw = 90 - compass_yaw only converts compass
heading into the OpenGL coordinate frame. It is NOT a sensor calibration and
cannot by itself turn 162.0 deg into 88.2 deg. A 162.0 -> 88.2 calibration
requires an offset of -73.8 deg; one heading is not sufficient to prove that
this offset is constant, so it remains configurable.

Dependencies
------------
    pip install PySide6 numpy pyqtgraph PyOpenGL PyOpenGL_accelerate
"""

from __future__ import annotations

import configparser
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.OTHER.SENSORS"
_WINDOWS_TIMER_ACTIVE = False


def configure_windows_runtime() -> None:
    global _WINDOWS_TIMER_ACTIVE

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
            if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
                _WINDOWS_TIMER_ACTIVE = True
        except Exception:
            pass

        try:
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                kernel32.FreeConsole()
        except Exception:
            pass

    except Exception:
        pass


def release_windows_runtime() -> None:
    global _WINDOWS_TIMER_ACTIVE

    if os.name == "nt" and _WINDOWS_TIMER_ACTIVE:
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass

        _WINDOWS_TIMER_ACTIVE = False


configure_windows_runtime()


# =============================================================================
# Qt / graphics
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QFont,
    QIcon,
    QMatrix4x4,
    QSurfaceFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pyqtgraph.opengl as gl
except Exception:
    gl = None

from shared_data import OBSSharedData, ControllerTelemetrySnapshot


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Other Sensors"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

TELEMETRY_POLL_MS = 20
GUI_UPDATE_MS = 33
STATUS_UPDATE_MS = 500
TELEMETRY_STALE_S = 2.5

MODEL_Z = 1.15
BODY_AXIS_LENGTH = 1.8

IMU_SMOOTH_TAU_S = 0.10
IMU_MAX_PREDICTION_S = 1.25

XCHM1_STALE_S = 1.5



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


def wrap_angle_deg(value: float) -> float:
    return (
        float(value)
        + 180.0
    ) % 360.0 - 180.0


def shortest_angle_delta_deg(
    current: float,
    target: float,
) -> float:
    return wrap_angle_deg(
        float(target)
        - float(current)
    )


def visual_yaw_from_compass_deg(
    yaw_compass: float,
) -> float:
    """
    Ground convention:
        +Y = North
        +X = East
        +Z = Up

    Qt/OpenGL mathematical yaw 0° points along +X.
    Sensor compass yaw 0° means North (+Y).

    Therefore:
        visual_yaw = 90° - compass_yaw
    """
    return wrap_angle_deg(
        90.0
        - float(yaw_compass)
    )


def visual_pitch_deg(
    pitch_sensor: float,
) -> float:
    """
    The model nose points local +X.

    With QMatrix4x4's right-handed rotation around +Y, negative visual pitch
    makes local +X rotate upward. Therefore sensor-positive pitch is drawn as
    nose UP by negating the visual OpenGL pitch.
    """
    return -float(
        pitch_sensor
    )


def rotation_matrix_visual_rpy_deg(
    roll: float,
    pitch: float,
    yaw: float,
):
    y = visual_yaw_from_compass_deg(
        yaw
    )
    p = visual_pitch_deg(
        pitch
    )

    r, p, y = map(
        math.radians,
        (
            roll,
            p,
            y,
        ),
    )

    cr, sr = (
        math.cos(r),
        math.sin(r),
    )
    cp, sp = (
        math.cos(p),
        math.sin(p),
    )
    cy, sy = (
        math.cos(y),
        math.sin(y),
    )

    return np.array(
        [
            [
                cy * cp,
                cy * sp * sr
                - sy * cr,
                cy * sp * cr
                + sy * sr,
            ],
            [
                sy * cp,
                sy * sp * sr
                + cy * cr,
                sy * sp * cr
                - cy * sr,
            ],
            [
                -sp,
                cp * sr,
                cp * cr,
            ],
        ],
        dtype=np.float64,
    )


def make_box_mesh(
    length=1.9,
    width=1.15,
    height=0.50,
):
    hx = length / 2.0
    hy = width / 2.0
    hz = height / 2.0

    vertices = np.array(
        [
            [-hx, -hy, -hz],
            [hx, -hy, -hz],
            [hx, hy, -hz],
            [-hx, hy, -hz],
            [-hx, -hy, hz],
            [hx, -hy, hz],
            [hx, hy, hz],
            [-hx, hy, hz],
        ],
        dtype=np.float32,
    )

    faces = np.array(
        [
            [0, 1, 2],
            [0, 2, 3],
            [4, 6, 5],
            [4, 7, 6],
            [0, 4, 5],
            [0, 5, 1],
            [1, 5, 6],
            [1, 6, 2],
            [2, 6, 7],
            [2, 7, 3],
            [3, 7, 4],
            [3, 4, 0],
        ],
        dtype=np.int32,
    )

    return (
        vertices,
        faces,
    )


def make_nose_mesh():
    xb = 0.95
    xt = 1.55
    yy = 0.52
    zz = 0.25

    vertices = np.array(
        [
            [xb, -yy, -zz],
            [xb, yy, -zz],
            [xb, yy, zz],
            [xb, -yy, zz],
            [xt, 0, 0],
        ],
        dtype=np.float32,
    )

    faces = np.array(
        [
            [0, 1, 2],
            [0, 2, 3],
            [0, 4, 1],
            [1, 4, 2],
            [2, 4, 3],
            [3, 4, 0],
        ],
        dtype=np.int32,
    )

    return (
        vertices,
        faces,
    )


def nmea_xor_checksum(body: str) -> int:
    checksum = 0
    for byte in body.encode("ascii"):
        checksum ^= byte
    return checksum & 0xFF


def parse_int_auto(text: str) -> int:
    value = str(text).strip()
    if value.lower().startswith(("0x", "+0x", "-0x")):
        return int(value, 16)
    return int(value, 10)


LMON_LEAK0 = 1 << 0
LMON_LEAK1 = 1 << 1
LMON_FAULT0 = 1 << 4
LMON_FAULT1 = 1 << 5
LMON_RESERVED_MASK = (1 << 2) | (1 << 3) | (1 << 6) | (1 << 7)

OASM_FLAGS = (
    (0, "POR_RESET", "Power-On Reset occurred"),
    (1, "BOR_RESET", "Brown-Out Reset occurred"),
    (2, "WDT_RESET", "Watchdog Timer Reset occurred"),
    (3, "S_X_RESET", "Software Reset occurred"),
    (4, "OSC_ERROR", "Oscillator error detected"),
    (5, "ADR_ERROR", "Address error detected"),
    (6, "STK_ERROR", "Stack error detected"),
    (7, "MTH_ERROR", "Math error detected"),
)


def decode_lmon(value: int) -> dict:
    """Decode the documented 8-bit LMON field without inventing meanings."""
    raw = int(value) & 0xFF
    leak0_bit = bool(raw & LMON_LEAK0)
    leak1_bit = bool(raw & LMON_LEAK1)
    fault0 = bool(raw & LMON_FAULT0)
    fault1 = bool(raw & LMON_FAULT1)

    # Firmware documentation states that a sensor-fault bit ALSO forces the
    # corresponding leak bit. Do not report that forced bit as a confirmed
    # physical leak condition.
    leak0 = leak0_bit and not fault0
    leak1 = leak1_bit and not fault1

    return {
        "raw": raw,
        "leak0_bit": leak0_bit,
        "leak1_bit": leak1_bit,
        "leak0": leak0,
        "leak1": leak1,
        "fault0": fault0,
        "fault1": fault1,
        "reserved": raw & LMON_RESERVED_MASK,
        "alarm": leak0 or leak1 or fault0 or fault1,
    }


def decode_oasm(value: int) -> dict:
    """Decode the documented 8-bit OASM startup/reset snapshot."""
    raw = int(value) & 0xFF
    active = [
        (name, description)
        for bit, name, description in OASM_FLAGS
        if raw & (1 << bit)
    ]
    return {
        "raw": raw,
        "active": active,
        "reset_mask": raw & 0x0F,
        "error_mask": raw & 0xF0,
    }



# =============================================================================
# Telemetry worker
# =============================================================================


class TelemetryReaderThread(QThread):
    telemetry_ready = Signal(object)
    controller_ready = Signal(object)
    read_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        shared = None
        last_telemetry_timestamp = -1
        last_controller_timestamp = -1
        try:
            shared = OBSSharedData()
            while not self._stop_event.is_set():
                telemetry = shared.read_telemetry()
                tstamp = int(telemetry.timestamp_ns)
                if tstamp != last_telemetry_timestamp:
                    last_telemetry_timestamp = tstamp
                    self.telemetry_ready.emit(telemetry)

                controller = shared.read_controller_telemetry()
                cstamp = int(controller.timestamp_ns)
                if cstamp > 0 and cstamp != last_controller_timestamp:
                    last_controller_timestamp = cstamp
                    self.controller_ready.emit(controller)

                self.msleep(TELEMETRY_POLL_MS)
        except Exception as exc:
            if not self._stop_event.is_set():
                self.read_error.emit(str(exc))
        finally:
            if shared is not None:
                try:
                    shared.close()
                except Exception:
                    pass

# =============================================================================
# Main window
# =============================================================================


class OtherSensorsWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        if np is None:
            raise RuntimeError(
                "NumPy is required."
            )

        self.shared = OBSSharedData()

        self.latest_telemetry = None
        self.telemetry_received_monotonic = (
            time.perf_counter()
        )

        self.latest_xchm1: Optional[ControllerTelemetrySnapshot] = None
        self.xchm1_received_monotonic = 0.0


        self.target_roll = 0.0
        self.target_pitch = 0.0
        self.target_yaw = 0.0

        self.rate_p = 0.0
        self.rate_q = 0.0
        self.rate_r = 0.0

        self.display_roll = 0.0
        self.display_pitch = 0.0
        self.display_yaw = 0.0

        self.last_orientation_t = (
            time.perf_counter()
        )

        self.gl_view = None
        self.model_body = None
        self.model_nose = None

        self.body_axis_x = None
        self.body_axis_y = None
        self.body_axis_z = None

        self.setWindowTitle(
            f"{APP_TITLE} - {SYSTEM_TITLE}"
        )

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(
                icon
            )

        self.resize(
            1460,
            850,
        )
        self.setMinimumSize(
            1060,
            680,
        )

        self._build_ui()
        self._apply_style()

        self.reader = (
            TelemetryReaderThread(
                self
            )
        )

        self.reader.telemetry_ready.connect(
            self.on_telemetry
        )
        self.reader.read_error.connect(
            self.on_reader_error
        )
        self.reader.controller_ready.connect(
            self.on_xchm1
        )
        self.reader.start()

        self.render_timer = QTimer(
            self
        )

        try:
            self.render_timer.setTimerType(
                Qt.TimerType.PreciseTimer
            )
        except Exception:
            pass

        self.render_timer.timeout.connect(
            self.render_frame
        )
        self.render_timer.start(
            GUI_UPDATE_MS
        )

        self.status_timer = QTimer(
            self
        )
        self.status_timer.timeout.connect(
            self.refresh_status
        )
        self.status_timer.start(
            STATUS_UPDATE_MS
        )

        self.refresh_status()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
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
            "OTHER SENSORS"
        )
        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            "IMU  •  $DEPT0  •  $XCHM1 Controller Health / Leak"
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

        self.system_state = QLabel(
            "WAITING"
        )
        self.system_state.setObjectName(
            "systemWaiting"
        )
        self.system_state.setAlignment(
            Qt.AlignCenter
        )
        self.system_state.setMinimumWidth(
            120
        )

        header.addWidget(
            self.system_state
        )

        root.addLayout(
            header
        )

        # Status strip.
        status_frame = QFrame()
        status_frame.setObjectName(
            "statusFrame"
        )

        status_layout = QHBoxLayout(
            status_frame
        )
        status_layout.setContentsMargins(
            10, 6, 10, 6
        )

        self.connection_label = QLabel(
            "Shared RAM: checking..."
        )
        self.connection_label.setObjectName(
            "statusLabel"
        )

        self.telemetry_label = QLabel(
            "Telemetry: --"
        )
        self.telemetry_label.setObjectName(
            "statusLabel"
        )

        self.xchm1_status_label = QLabel(
            "XCHM1 pool: waiting for OBS Setting..."
        )
        self.xchm1_status_label.setObjectName(
            "statusLabel"
        )

        self.power_mode_label = QLabel(
            "Power Mode: --"
        )
        self.power_mode_label.setObjectName(
            "statusLabel"
        )

        status_layout.addWidget(
            self.connection_label
        )
        status_layout.addStretch(
            1
        )
        status_layout.addWidget(
            self.telemetry_label
        )
        status_layout.addSpacing(
            12
        )
        status_layout.addWidget(
            self.xchm1_status_label
        )
        status_layout.addSpacing(
            12
        )
        status_layout.addWidget(
            self.power_mode_label
        )

        root.addWidget(
            status_frame
        )

        splitter = QSplitter(
            Qt.Horizontal
        )
        splitter.setChildrenCollapsible(
            False
        )

        splitter.addWidget(
            self._build_imu_panel()
        )
        splitter.addWidget(
            self._build_sensor_panel()
        )

        splitter.setStretchFactor(
            0,
            3,
        )
        splitter.setStretchFactor(
            1,
            2,
        )
        splitter.setSizes(
            [880, 580]
        )

        root.addWidget(
            splitter,
            1,
        )

    # ------------------------------------------------------------------ IMU panel

    def _build_imu_panel(self):
        panel = QFrame()
        panel.setObjectName(
            "imuPanel"
        )

        layout = QVBoxLayout(
            panel
        )
        layout.setContentsMargins(
            0, 0, 0, 0
        )
        layout.setSpacing(
            7
        )

        model_frame = QFrame()
        model_frame.setObjectName(
            "modelFrame"
        )

        model_layout = QVBoxLayout(
            model_frame
        )
        model_layout.setContentsMargins(
            0, 0, 0, 0
        )

        if gl is not None:
            self.gl_view = (
                gl.GLViewWidget()
            )
            self.gl_view.setCameraPosition(
                distance=8.5,
                elevation=22,
                azimuth=-45,
            )

            model_layout.addWidget(
                self.gl_view,
                1,
            )

            self._build_3d_scene()

        else:
            missing = QLabel(
                "3D IMU display unavailable.\n\n"
                "Install:\n"
                "pip install pyqtgraph PyOpenGL PyOpenGL_accelerate"
            )
            missing.setObjectName(
                "missing3DLabel"
            )
            missing.setAlignment(
                Qt.AlignCenter
            )

            model_layout.addWidget(
                missing,
                1,
            )

        layout.addWidget(
            model_frame,
            1,
        )

        # R/P/Y and rates.
        angle_group = QGroupBox(
            "IMU Attitude"
        )
        angle_group.setObjectName(
            "sensorGroup"
        )

        grid = QGridLayout(
            angle_group
        )
        grid.setContentsMargins(
            10, 14, 10, 10
        )
        grid.setHorizontalSpacing(
            8
        )
        grid.setVerticalSpacing(
            5
        )

        self.roll_value = QLabel(
            "+0.00°"
        )
        self.pitch_value = QLabel(
            "+0.00°"
        )
        self.yaw_value = QLabel(
            "+0.00°"
        )

        self.p_value = QLabel(
            "P: +0.00 °/s"
        )
        self.q_value = QLabel(
            "Q: +0.00 °/s"
        )
        self.r_value = QLabel(
            "R: +0.00 °/s"
        )

        for value in (
            self.roll_value,
            self.pitch_value,
            self.yaw_value,
        ):
            value.setObjectName(
                "angleValue"
            )
            value.setAlignment(
                Qt.AlignCenter
            )

        for value in (
            self.p_value,
            self.q_value,
            self.r_value,
        ):
            value.setObjectName(
                "rateValue"
            )
            value.setAlignment(
                Qt.AlignCenter
            )

        for col, name in enumerate(
            (
                "ROLL",
                "PITCH",
                "YAW",
            )
        ):
            label = QLabel(
                name
            )
            label.setObjectName(
                "angleName"
            )
            label.setAlignment(
                Qt.AlignCenter
            )

            grid.addWidget(
                label,
                0,
                col,
            )

        grid.addWidget(
            self.roll_value,
            1,
            0,
        )
        grid.addWidget(
            self.pitch_value,
            1,
            1,
        )
        grid.addWidget(
            self.yaw_value,
            1,
            2,
        )

        grid.addWidget(
            self.p_value,
            2,
            0,
        )
        grid.addWidget(
            self.q_value,
            2,
            1,
        )
        grid.addWidget(
            self.r_value,
            2,
            2,
        )

        self.imu_age_label = QLabel(
            "IMU age: --"
        )
        self.imu_age_label.setObjectName(
            "hintText"
        )
        self.imu_age_label.setAlignment(
            Qt.AlignCenter
        )

        grid.addWidget(
            self.imu_age_label,
            3,
            0,
            1,
            3,
        )

        layout.addWidget(
            angle_group
        )


        return panel

    def _build_3d_scene(self):
        if (
            gl is None
            or self.gl_view is None
        ):
            return

        grid = gl.GLGridItem()
        grid.setSize(
            x=9.0,
            y=9.0,
            z=1.0,
        )
        grid.setSpacing(
            x=1.0,
            y=1.0,
            z=1.0,
        )
        self.gl_view.addItem(
            grid
        )

        world_axis = gl.GLAxisItem()
        world_axis.setSize(
            x=3.5,
            y=3.5,
            z=3.5,
        )
        self.gl_view.addItem(
            world_axis
        )

        # North arrow. Ground convention: +Y = North, +X = East.
        north_arrow = gl.GLLinePlotItem(
            pos=np.array(
                [
                    [-3.2, -2.8, 0.05],
                    [-3.2, 2.8, 0.05],
                    [-3.45, 2.45, 0.05],
                    [-3.2, 2.8, 0.05],
                    [-2.95, 2.45, 0.05],
                ],
                dtype=np.float32,
            ),
            color=(
                1.0,
                0.86,
                0.20,
                1.0,
            ),
            width=2.0,
            antialias=False,
            mode="line_strip",
        )
        self.gl_view.addItem(
            north_arrow
        )

        if hasattr(
            gl,
            "GLTextItem",
        ):
            labels = (
                (
                    "N",
                    (
                        0.0,
                        4.3,
                        0.05,
                    ),
                ),
                (
                    "E",
                    (
                        4.3,
                        0.0,
                        0.05,
                    ),
                ),
                (
                    "S",
                    (
                        0.0,
                        -4.3,
                        0.05,
                    ),
                ),
                (
                    "W",
                    (
                        -4.3,
                        0.0,
                        0.05,
                    ),
                ),
            )

            for text, pos in labels:
                try:
                    item = gl.GLTextItem(
                        pos=pos,
                        text=text,
                        color=(
                            1.0,
                            1.0,
                            1.0,
                            1.0,
                        ),
                    )
                    self.gl_view.addItem(
                        item
                    )
                except Exception:
                    pass

        vertices, faces = (
            make_box_mesh()
        )

        mesh = gl.MeshData(
            vertexes=vertices,
            faces=faces,
        )

        self.model_body = (
            gl.GLMeshItem(
                meshdata=mesh,
                smooth=False,
                color=(
                    0.18,
                    0.42,
                    0.60,
                    0.86,
                ),
                shader="shaded",
                drawEdges=True,
                edgeColor=(
                    0.70,
                    0.85,
                    0.95,
                    0.70,
                ),
            )
        )

        nose_vertices, nose_faces = (
            make_nose_mesh()
        )

        nose_mesh = gl.MeshData(
            vertexes=nose_vertices,
            faces=nose_faces,
        )

        self.model_nose = (
            gl.GLMeshItem(
                meshdata=nose_mesh,
                smooth=False,
                color=(
                    0.95,
                    0.18,
                    0.18,
                    0.95,
                ),
                shader="shaded",
                drawEdges=True,
                edgeColor=(
                    1.0,
                    0.45,
                    0.45,
                    1.0,
                ),
            )
        )

        self.gl_view.addItem(
            self.model_body
        )
        self.gl_view.addItem(
            self.model_nose
        )

        self.body_axis_x = (
            gl.GLLinePlotItem(
                pos=np.zeros(
                    (
                        2,
                        3,
                    ),
                    dtype=np.float32,
                ),
                color=(
                    1.0,
                    0.25,
                    0.25,
                    1.0,
                ),
                width=3.0,
                antialias=False,
            )
        )

        self.body_axis_y = (
            gl.GLLinePlotItem(
                pos=np.zeros(
                    (
                        2,
                        3,
                    ),
                    dtype=np.float32,
                ),
                color=(
                    0.25,
                    1.0,
                    0.35,
                    1.0,
                ),
                width=3.0,
                antialias=False,
            )
        )

        self.body_axis_z = (
            gl.GLLinePlotItem(
                pos=np.zeros(
                    (
                        2,
                        3,
                    ),
                    dtype=np.float32,
                ),
                color=(
                    0.30,
                    0.55,
                    1.0,
                    1.0,
                ),
                width=3.0,
                antialias=False,
            )
        )

        for item in (
            self.body_axis_x,
            self.body_axis_y,
            self.body_axis_z,
        ):
            self.gl_view.addItem(
                item
            )

        self._update_3d_model(
            0.0,
            0.0,
            0.0,
        )

    # ------------------------------------------------------------------ sensor panel

    def _build_sensor_panel(self):
        panel = QFrame()
        panel.setObjectName(
            "sensorPanel"
        )

        layout = QGridLayout(
            panel
        )
        layout.setContentsMargins(
            8, 0, 0, 0
        )
        layout.setHorizontalSpacing(
            8
        )
        layout.setVerticalSpacing(
            8
        )

        # Temperature ---------------------------------------------------
        temp = QGroupBox(
            "Temperature"
        )
        temp.setObjectName(
            "sensorGroup"
        )

        tl = QVBoxLayout(
            temp
        )
        tl.setContentsMargins(
            12, 16, 12, 12
        )

        self.temperature_value = QLabel(
            "--.- °C"
        )
        self.temperature_value.setObjectName(
            "sensorBigValue"
        )
        self.temperature_value.setAlignment(
            Qt.AlignCenter
        )

        self.temperature_status = QLabel(
            "NO DATA"
        )
        self.temperature_status.setObjectName(
            "sensorSubValue"
        )
        self.temperature_status.setAlignment(
            Qt.AlignCenter
        )

        temp_note = QLabel(
            "$DEPT0 temperature uses E2 scaling: raw / 100; $XCHM1 TEMP is reserved/ignored"
        )
        temp_note.setObjectName(
            "hintText"
        )
        temp_note.setAlignment(
            Qt.AlignCenter
        )

        tl.addStretch(
            1
        )
        tl.addWidget(
            self.temperature_value
        )
        tl.addWidget(
            self.temperature_status
        )
        tl.addStretch(
            1
        )
        tl.addWidget(
            temp_note
        )

        layout.addWidget(
            temp,
            0,
            0,
        )

        # Depth ---------------------------------------------------------
        depth = QGroupBox(
            "Depth ($DEPT0)"
        )
        depth.setObjectName(
            "sensorGroup"
        )

        pl = QVBoxLayout(
            depth
        )
        pl.setContentsMargins(
            12, 16, 12, 12
        )

        self.depth_value = QLabel(
            "--.-- m"
        )
        self.depth_value.setObjectName(
            "sensorBigValue"
        )
        self.depth_value.setAlignment(
            Qt.AlignCenter
        )

        self.depth_status = QLabel(
            "NO DATA"
        )
        self.depth_status.setObjectName(
            "sensorSubValue"
        )
        self.depth_status.setAlignment(
            Qt.AlignCenter
        )

        depth_note = QLabel(
            "$DEPT0 field 0 is Depth and is displayed in meter (m). "
            "Legacy shared-RAM field name telemetry.pressure is retained only for compatibility."
        )
        depth_note.setObjectName(
            "hintText"
        )
        depth_note.setAlignment(
            Qt.AlignCenter
        )
        depth_note.setWordWrap(True)

        pl.addStretch(1)
        pl.addWidget(self.depth_value)
        pl.addWidget(self.depth_status)
        pl.addStretch(1)
        pl.addWidget(depth_note)

        layout.addWidget(
            depth,
            0,
            1,
        )

        # Leak ----------------------------------------------------------
        leak = QGroupBox(
            "Leak Monitor ($XCHM1 LMON)"
        )
        leak.setObjectName(
            "sensorGroup"
        )

        ll = QVBoxLayout(
            leak
        )
        ll.setContentsMargins(
            12, 16, 12, 12
        )

        self.leak_icon = QLabel(
            "●"
        )
        self.leak_icon.setObjectName(
            "leakDisconnectedIcon"
        )
        self.leak_icon.setAlignment(
            Qt.AlignCenter
        )

        self.leak_value = QLabel(
            "NO XCHM1"
        )
        self.leak_value.setObjectName(
            "leakDisconnected"
        )
        self.leak_value.setAlignment(
            Qt.AlignCenter
        )

        self.leak_warning = QLabel(
            ""
        )
        self.leak_warning.setObjectName(
            "leakWarning"
        )
        self.leak_warning.setAlignment(
            Qt.AlignCenter
        )
        self.leak_warning.setWordWrap(
            True
        )

        leak_note = QLabel(
            "LMON: bit0/1 = leak S0/S1; bit4/5 = S0/S1 fault/not-connected; bits2/3/6/7 reserved."
        )
        leak_note.setObjectName(
            "hintText"
        )
        leak_note.setAlignment(
            Qt.AlignCenter
        )
        leak_note.setWordWrap(True)

        ll.addStretch(
            1
        )
        ll.addWidget(
            self.leak_icon
        )
        ll.addWidget(
            self.leak_value
        )
        ll.addWidget(
            self.leak_warning
        )
        ll.addStretch(
            1
        )
        ll.addWidget(
            leak_note
        )

        layout.addWidget(
            leak,
            1,
            0,
        )

        # Controller health / XCHM1 --------------------------------------
        health = QGroupBox(
            "Controller Health ($XCHM1)"
        )
        health.setObjectName(
            "sensorGroup"
        )

        hl = QVBoxLayout(
            health
        )
        hl.setContentsMargins(
            12, 16, 12, 12
        )
        hl.setSpacing(6)

        rail_grid = QGridLayout()
        rail_grid.setHorizontalSpacing(10)
        rail_grid.setVerticalSpacing(3)

        rail0_name = QLabel("VMONI0")
        rail1_name = QLabel("VMONI1")
        for label in (rail0_name, rail1_name):
            label.setObjectName("angleName")
            label.setAlignment(Qt.AlignCenter)

        self.vmoni0_value = QLabel("--.-- V")
        self.vmoni1_value = QLabel("--.-- V")
        for label in (self.vmoni0_value, self.vmoni1_value):
            label.setObjectName("angleValue")
            label.setAlignment(Qt.AlignCenter)

        rail_grid.addWidget(rail0_name, 0, 0)
        rail_grid.addWidget(rail1_name, 0, 1)
        rail_grid.addWidget(self.vmoni0_value, 1, 0)
        rail_grid.addWidget(self.vmoni1_value, 1, 1)
        hl.addLayout(rail_grid)

        self.health_meta_value = QLabel(
            "LMON: --   OASM: --\nID: --   COUNT: --"
        )
        self.health_meta_value.setObjectName("sensorSubValue")
        self.health_meta_value.setAlignment(Qt.AlignCenter)
        self.health_meta_value.setWordWrap(True)
        hl.addWidget(self.health_meta_value)

        self.health_age_value = QLabel("XCHM1 age: --")
        self.health_age_value.setObjectName("hintText")
        self.health_age_value.setAlignment(Qt.AlignCenter)
        hl.addWidget(self.health_age_value)

        self.health_reserved_value = QLabel(
            "VMONI2/3, TEMP, PMON, HMON: reserved / ignored"
        )
        self.health_reserved_value.setObjectName("hintText")
        self.health_reserved_value.setAlignment(Qt.AlignCenter)
        self.health_reserved_value.setWordWrap(True)
        hl.addWidget(self.health_reserved_value)

        health_note = QLabel(
            "VMONI0/1: raw / 10 V. OASM is captured once at startup; reset flags are historical until the next device reset."
        )
        health_note.setObjectName("hintText")
        health_note.setAlignment(Qt.AlignCenter)
        health_note.setWordWrap(True)
        hl.addWidget(health_note)

        layout.addWidget(
            health,
            1,
            1,
        )

        layout.setRowStretch(
            0,
            1
        )
        layout.setRowStretch(
            1,
            1
        )
        layout.setColumnStretch(
            0,
            1
        )
        layout.setColumnStretch(
            1,
            1
        )

        return panel

    # ------------------------------------------------------------------ telemetry

    def on_telemetry(
        self,
        telemetry,
    ):
        self.latest_telemetry = (
            telemetry
        )

        self.telemetry_received_monotonic = (
            time.perf_counter()
        )

        # Physical AHRS values are already published by OBS Setting.
        # Version 9 uses them directly; no per-GUI IMU offset is applied.
        self.target_roll = float(
            telemetry.roll
        )
        self.target_pitch = float(
            telemetry.pitch
        )
        self.target_yaw = wrap_angle_deg(
            float(
                telemetry.yaw
            )
        )

        self.rate_p = float(
            telemetry.angular_rate_p
        )
        self.rate_q = float(
            telemetry.angular_rate_q
        )
        self.rate_r = float(
            telemetry.angular_rate_r
        )

    def on_reader_error(
        self,
        message: str,
    ):
        self.connection_label.setText(
            f"Telemetry reader error: {message}"
        )

    def on_xchm1(self, snapshot: ControllerTelemetrySnapshot):
        self.latest_xchm1 = snapshot
        self.xchm1_received_monotonic = time.perf_counter()

    def _xchm1_age_ms(self) -> float:
        if self.latest_xchm1 is None or int(self.latest_xchm1.timestamp_ns) <= 0:
            return float("inf")
        return max(
            0.0,
            (time.time_ns() - int(self.latest_xchm1.timestamp_ns)) / 1_000_000.0,
        )

    def _xchm1_is_live(self) -> bool:
        return (
            self.latest_xchm1 is not None
            and self._xchm1_age_ms() <= XCHM1_STALE_S * 1000.0
        )

    # ------------------------------------------------------------------ IMU rendering

    def _predicted_target(self):
        if self.latest_telemetry is None:
            return (
                self.target_roll,
                self.target_pitch,
                self.target_yaw,
            )

        age_s = max(
            0.0,
            time.perf_counter()
            - self.telemetry_received_monotonic,
        )

        prediction_s = min(
            age_s,
            IMU_MAX_PREDICTION_S,
        )

        return (
            wrap_angle_deg(
                self.target_roll
                + self.rate_p
                * prediction_s
            ),
            wrap_angle_deg(
                self.target_pitch
                + self.rate_q
                * prediction_s
            ),
            wrap_angle_deg(
                self.target_yaw
                + self.rate_r
                * prediction_s
            ),
        )

    def _update_orientation(self):
        now = (
            time.perf_counter()
        )

        dt = max(
            0.0,
            min(
                0.20,
                now
                - self.last_orientation_t,
            ),
        )

        self.last_orientation_t = (
            now
        )

        (
            target_roll,
            target_pitch,
            target_yaw,
        ) = self._predicted_target()

        alpha = (
            1.0
            - math.exp(
                -dt
                / max(
                    1.0e-4,
                    IMU_SMOOTH_TAU_S,
                )
            )
        )

        self.display_roll = wrap_angle_deg(
            self.display_roll
            + shortest_angle_delta_deg(
                self.display_roll,
                target_roll,
            )
            * alpha
        )

        self.display_pitch = wrap_angle_deg(
            self.display_pitch
            + shortest_angle_delta_deg(
                self.display_pitch,
                target_pitch,
            )
            * alpha
        )

        self.display_yaw = wrap_angle_deg(
            self.display_yaw
            + shortest_angle_delta_deg(
                self.display_yaw,
                target_yaw,
            )
            * alpha
        )

        self._update_3d_model(
            self.display_roll,
            self.display_pitch,
            self.display_yaw,
        )

    def _update_3d_model(
        self,
        roll: float,
        pitch: float,
        yaw: float,
    ):
        if (
            gl is None
            or self.gl_view is None
            or self.model_body is None
        ):
            return

        visual_yaw = (
            visual_yaw_from_compass_deg(
                yaw
            )
        )

        visual_pitch = (
            visual_pitch_deg(
                pitch
            )
        )

        transform = QMatrix4x4()
        transform.translate(
            0,
            0,
            MODEL_Z,
        )
        transform.rotate(
            float(
                visual_yaw
            ),
            0,
            0,
            1,
        )
        transform.rotate(
            float(
                visual_pitch
            ),
            0,
            1,
            0,
        )
        transform.rotate(
            float(
                roll
            ),
            1,
            0,
            0,
        )

        self.model_body.setTransform(
            transform
        )
        self.model_nose.setTransform(
            transform
        )

        rotation = (
            rotation_matrix_visual_rpy_deg(
                roll,
                pitch,
                yaw,
            )
        )

        center = np.array(
            [
                0.0,
                0.0,
                MODEL_Z,
            ],
            dtype=np.float64,
        )

        vectors = (
            np.array(
                [
                    BODY_AXIS_LENGTH,
                    0.0,
                    0.0,
                ]
            ),
            np.array(
                [
                    0.0,
                    BODY_AXIS_LENGTH,
                    0.0,
                ]
            ),
            np.array(
                [
                    0.0,
                    0.0,
                    BODY_AXIS_LENGTH,
                ]
            ),
        )

        for vector, item in zip(
            vectors,
            (
                self.body_axis_x,
                self.body_axis_y,
                self.body_axis_z,
            ),
        ):
            item.setData(
                pos=np.array(
                    [
                        center,
                        center
                        + rotation
                        @ vector,
                    ],
                    dtype=np.float32,
                )
            )

    # ------------------------------------------------------------------ sensor display

    def _telemetry_age_ms(self):
        if self.latest_telemetry is None:
            return float("inf")

        timestamp_ns = int(
            self.latest_telemetry.timestamp_ns
        )

        if timestamp_ns <= 0:
            return float("inf")

        return max(
            0.0,
            (
                time.time_ns()
                - timestamp_ns
            )
            / 1_000_000.0,
        )

    def _telemetry_is_live(self):
        return (
            self._telemetry_age_ms()
            <= TELEMETRY_STALE_S
            * 1000.0
        )

    def _update_imu_labels(self):
        self.roll_value.setText(
            f"{self.display_roll:+.2f}°"
        )
        self.pitch_value.setText(
            f"{self.display_pitch:+.2f}°"
        )
        self.yaw_value.setText(
            f"{self.display_yaw:+.2f}°"
        )

        self.p_value.setText(
            f"P: {self.rate_p:+.2f} °/s"
        )
        self.q_value.setText(
            f"Q: {self.rate_q:+.2f} °/s"
        )
        self.r_value.setText(
            f"R: {self.rate_r:+.2f} °/s"
        )

        if self.latest_telemetry is None:
            self.imu_age_label.setText(
                "IMU age: --"
            )
            return

        age_ms = (
            self._telemetry_age_ms()
        )

        state = (
            "LIVE"
            if age_ms
            <= TELEMETRY_STALE_S
            * 1000.0
            else "STALE"
        )

        self.imu_age_label.setText(
            f"IMU age: {age_ms:.0f} ms • "
            f"{state} • "
            f"Device ID {self.latest_telemetry.ahrs_device_id}"
        )

    def _update_temperature_depth(self):
        telemetry = self.latest_telemetry

        if telemetry is None:
            self.temperature_value.setText("--.- °C")
            self.temperature_status.setText("NO DATA")
            self.depth_value.setText("--.-- m")
            self.depth_status.setText("NO DATA")
            return

        live = self._telemetry_is_live()

        # Physical units are published by OBS Setting.
        # Temperature is already supplied in physical units.
        # The shared-memory attribute name `pressure` is legacy; field 0 of
        # $DEPT0 is treated here as depth in meters. No second scaling is done.
        temperature = float(telemetry.temperature)
        depth_m = float(telemetry.pressure)

        self.temperature_value.setText(
            f"{temperature:.2f} °C"
        )
        self.temperature_status.setText(
            "LIVE" if live else "STALE"
        )

        self.depth_value.setText(
            f"{depth_m:.2f} m"
        )
        self.depth_status.setText(
            "LIVE" if live else "STALE"
        )

    def _set_dynamic_object_name(
        self,
        widget,
        object_name: str,
    ):
        if widget.objectName() == object_name:
            return

        widget.setObjectName(
            object_name
        )
        widget.style().unpolish(
            widget
        )
        widget.style().polish(
            widget
        )

    def _update_leak(self):
        snapshot = self.latest_xchm1
        live = self._xchm1_is_live()

        if snapshot is None or not live:
            self.leak_icon.setText("●")
            self.leak_value.setText("NO XCHM1 IN POOL" if snapshot is None else "STALE")
            self.leak_warning.setText("Leak-monitor data unavailable in centralized pool")
            self._set_dynamic_object_name(self.leak_icon, "leakDisconnectedIcon")
            self._set_dynamic_object_name(self.leak_value, "leakDisconnected")
            return

        info = decode_lmon(snapshot.lmon)
        raw = info["raw"]
        lmon_hex = f"0x{raw:02X}"
        messages = []

        if info["fault0"]:
            messages.append("Sensor 0 FAULT / NOT CONNECTED (bit4; bit0 forced)")
        elif info["leak0"]:
            messages.append("LEAK detected on sensor 0 (bit0)")

        if info["fault1"]:
            messages.append("Sensor 1 FAULT / NOT CONNECTED (bit5; bit1 forced)")
        elif info["leak1"]:
            messages.append("LEAK detected on sensor 1 (bit1)")

        if info["reserved"]:
            messages.append(
                f"Reserved LMON bits active: 0x{info['reserved']:02X} "
                "(expected 0)"
            )

        if info["alarm"]:
            self.leak_icon.setText("⚠")
            if info["fault0"] or info["fault1"]:
                self.leak_value.setText("LEAK SENSOR FAULT")
            else:
                self.leak_value.setText("LEAK DETECTED")
            self.leak_warning.setText(
                f"LMON {lmon_hex} • {raw:08b}\n" + "\n".join(messages)
            )
            self._set_dynamic_object_name(self.leak_icon, "leakAlarmIcon")
            self._set_dynamic_object_name(self.leak_value, "leakAlarm")
        elif info["reserved"]:
            self.leak_icon.setText("●")
            self.leak_value.setText("PROTOCOL WARNING")
            self.leak_warning.setText(
                f"LMON {lmon_hex} • {raw:08b}\n" + "\n".join(messages)
            )
            self._set_dynamic_object_name(self.leak_icon, "leakDisconnectedIcon")
            self._set_dynamic_object_name(self.leak_value, "leakDisconnected")
        else:
            self.leak_icon.setText("●")
            self.leak_value.setText("NO LEAK")
            self.leak_warning.setText(
                f"LMON {lmon_hex} • both leak sensors normal"
            )
            self._set_dynamic_object_name(self.leak_icon, "leakGoodIcon")
            self._set_dynamic_object_name(self.leak_value, "leakGood")

    def _update_controller_health(self):
        snapshot = self.latest_xchm1
        if snapshot is None:
            self.vmoni0_value.setText("--.-- V")
            self.vmoni1_value.setText("--.-- V")
            self.health_meta_value.setText("LMON: --   OASM: --\nID: --   COUNT: --")
            self.health_age_value.setText("XCHM1 age: -- • NO DATA")
            self.health_reserved_value.setText(
                "VMONI2/3, TEMP, PMON, HMON: reserved / ignored"
            )
            return

        age_ms = self._xchm1_age_ms()
        live = self._xchm1_is_live()
        self.vmoni0_value.setText(f"{snapshot.vmoni0_v:.1f} V")
        self.vmoni1_value.setText(f"{snapshot.vmoni1_v:.1f} V")

        lmon_info = decode_lmon(snapshot.lmon)
        oasm_info = decode_oasm(snapshot.oasm)

        if oasm_info["active"]:
            oasm_names = ", ".join(name for name, _ in oasm_info["active"])
        else:
            oasm_names = "no flags"

        self.health_meta_value.setText(
            f"LMON: 0x{lmon_info['raw']:02X}   "
            f"OASM: 0x{oasm_info['raw']:02X}\n"
            f"OASM: {oasm_names}\n"
            f"ID: {snapshot.device_id:d}   COUNT: {snapshot.count:d}"
        )
        self.health_meta_value.setToolTip(
            "\n".join(
                f"{name}: {description}"
                for name, description in oasm_info["active"]
            ) if oasm_info["active"] else "OASM=0: no captured reset/error flags"
        )

        self.health_age_value.setText(
            f"XCHM1 age: {age_ms:.0f} ms • {'LIVE' if live else 'STALE'}"
        )

        protocol_warnings = []
        if snapshot.reserved_nonzero:
            protocol_warnings.append(
                "reserved XCHM1 sensor fields nonzero: "
                f"VMONI2={snapshot.vmoni2_raw}, VMONI3={snapshot.vmoni3_raw}, "
                f"TEMP={snapshot.temp_raw}, PMON={snapshot.pmon_raw}, "
                f"HMON={snapshot.hmon_raw}"
            )
        if lmon_info["reserved"]:
            protocol_warnings.append(
                f"reserved LMON bits nonzero: 0x{lmon_info['reserved']:02X}"
            )

        if protocol_warnings:
            self.health_reserved_value.setText(
                "PROTOCOL WARNING: " + " | ".join(protocol_warnings)
            )
        else:
            self.health_reserved_value.setText(
                "Reserved XCHM1 fields and LMON bits: all 0 / ignored as specified"
            )

    # ------------------------------------------------------------------ render/status

    def render_frame(self):
        self._update_orientation()
        self._update_imu_labels()
        self._update_temperature_depth()
        self._update_leak()
        self._update_controller_health()

    def _set_system_state(
        self,
        text: str,
        object_name: str,
    ):
        self.system_state.setText(
            text
        )

        self._set_dynamic_object_name(
            self.system_state,
            object_name,
        )

    def refresh_status(self):
        try:
            telemetry = self.shared.read_telemetry()
            health = self.shared.read_acquisition_health()
            core_age = health.heartbeat_age_s()
            core_live = health.is_alive()
            connected = bool(
                telemetry.command_connected
                or telemetry.data_connected
            )

            self.connection_label.setText(
                (
                    f"OBS Core: {'LIVE' if core_live else 'STALE'} "
                    f"• HB {core_age:.1f}s • "
                    f"CMD {'ON' if health.command_connected else 'OFF'} • "
                    f"DATA {'ON' if health.data_connected else 'OFF'}"
                )
            )

            age_ms = max(
                0.0,
                (time.time_ns() - int(telemetry.timestamp_ns)) / 1_000_000.0,
            ) if int(telemetry.timestamp_ns) > 0 else float("inf")

            imu_age = health.source_age_s("ahrs")
            depth_age = health.source_age_s("depth")
            if math.isfinite(age_ms):
                self.telemetry_label.setText(
                    f"Telemetry: IMU {imu_age:.1f}s • Depth {depth_age:.1f}s "
                    f"• pool {age_ms:.0f} ms"
                )
            else:
                self.telemetry_label.setText("Telemetry age: --")

            self.power_mode_label.setText(
                f"Power Mode: {telemetry.power_mode}"
            )

            x_age = self._xchm1_age_ms()
            if math.isfinite(x_age):
                x_state = "LIVE" if self._xchm1_is_live() else "STALE"
                self.xchm1_status_label.setText(
                    f"XCHM1 pool: {x_state} • {x_age:.0f} ms"
                )
            else:
                self.xchm1_status_label.setText("XCHM1 pool: waiting for OBS Setting")

            xchm1_live = self._xchm1_is_live()
            lmon_info = (
                decode_lmon(self.latest_xchm1.lmon)
                if xchm1_live and self.latest_xchm1 is not None
                else None
            )
            oasm_info = (
                decode_oasm(self.latest_xchm1.oasm)
                if xchm1_live and self.latest_xchm1 is not None
                else None
            )

            if lmon_info is not None and lmon_info["alarm"]:
                self._set_system_state(
                    "⚠ LEAK/FAULT",
                    "systemAlarm",
                )
            elif oasm_info is not None and oasm_info["error_mask"]:
                self._set_system_state(
                    "⚠ OASM ERROR",
                    "systemAlarm",
                )
            elif lmon_info is not None and lmon_info["reserved"]:
                self._set_system_state(
                    "LMON WARNING",
                    "systemWarn",
                )
            elif not connected:
                self._set_system_state(
                    "OFFLINE",
                    "systemWaiting",
                )
            elif age_ms > TELEMETRY_STALE_S * 1000.0:
                self._set_system_state(
                    "STALE",
                    "systemWarn",
                )
            elif not xchm1_live:
                self._set_system_state(
                    "HEALTH STALE",
                    "systemWarn",
                )
            else:
                self._set_system_state(
                    "LIVE",
                    "systemGood",
                )

        except Exception as exc:
            self.connection_label.setText(
                f"Shared RAM error: {exc}"
            )
            self._set_system_state(
                "ERROR",
                "systemAlarm",
            )

    # ------------------------------------------------------------------ style

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QLabel {
                background: transparent;
                color: #FFFFFF;
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

            QLabel#systemGood {
                background: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 7px;
                color: #A9F1D2;
                font-weight: 800;
                padding: 5px 12px;
            }

            QLabel#systemWarn {
                background: #403510;
                border: 1px solid #A88821;
                border-radius: 7px;
                color: #FFE49A;
                font-weight: 800;
                padding: 5px 12px;
            }

            QLabel#systemAlarm {
                background: #571C24;
                border: 1px solid #D14C5E;
                border-radius: 7px;
                color: #FFD2D8;
                font-weight: 900;
                padding: 5px 12px;
            }

            QLabel#systemWaiting {
                background: #172631;
                border: 1px solid #35546A;
                border-radius: 7px;
                color: #A9BECA;
                font-weight: 800;
                padding: 5px 12px;
            }

            QFrame#modelFrame {
                background: #07131D;
                border: 1px solid #1A3D52;
                border-radius: 9px;
            }

            QLabel#missing3DLabel {
                color: #FFDCA8;
                font-size: 11px;
                padding: 18px;
            }

            QGroupBox#sensorGroup {
                background: #0D1E2A;
                border: 1px solid #1A3D52;
                border-radius: 9px;
                margin-top: 11px;
                padding-top: 6px;
                font-weight: 800;
                color: #FFFFFF;
            }

            QGroupBox#sensorGroup::title {
                subcontrol-origin: margin;
                left: 9px;
                padding: 0 5px;
                color: #FFFFFF;
            }

            QLabel#angleName {
                color: #8EA7B5;
                font-size: 9px;
                font-weight: 800;
            }

            QLabel#angleValue {
                background: #091821;
                border: 1px solid #24485D;
                border-radius: 7px;
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 20px;
                font-weight: 800;
                padding: 6px;
            }

            QLabel#rateValue {
                color: #B8CBD6;
                font-family: "Consolas";
                font-size: 10px;
            }

            QLabel#sensorBigValue {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 30px;
                font-weight: 900;
            }

            QLabel#sensorSubValue {
                color: #AFC4CF;
                font-family: "Consolas";
                font-size: 11px;
                font-weight: 700;
            }

            QLabel#batteryHeading {
                color: #A9BECA;
                font-size: 10px;
                font-weight: 700;
            }

            QLabel#batteryPercent {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 22px;
                font-weight: 900;
            }

            QLabel#hintText {
                color: #7894A4;
                font-size: 9px;
            }


            QPushButton {
                background: #17384A;
                color: #EAF4F8;
                border: 1px solid #2A5870;
                border-radius: 5px;
                padding: 6px 12px;
                font-weight: 700;
            }

            QPushButton:hover {
                background: #214C62;
            }

            QLabel#leakGoodIcon {
                color: #4DE69C;
                font-size: 46px;
                font-weight: 900;
            }

            QLabel#leakGood {
                color: #A9F1D2;
                font-size: 20px;
                font-weight: 900;
            }

            QLabel#leakDisconnectedIcon {
                color: #778E9A;
                font-size: 46px;
                font-weight: 900;
            }

            QLabel#leakDisconnected {
                color: #93AAB6;
                font-size: 18px;
                font-weight: 900;
            }

            QLabel#leakAlarmIcon {
                color: #FF5168;
                font-size: 52px;
                font-weight: 900;
            }

            QLabel#leakAlarm {
                color: #FFD2D8;
                background: #571C24;
                border: 2px solid #D14C5E;
                border-radius: 8px;
                font-size: 22px;
                font-weight: 900;
                padding: 6px;
            }

            QLabel#leakWarning {
                color: #FFB8C1;
                font-size: 10px;
                font-weight: 800;
            }

            QProgressBar {
                min-height: 18px;
                border: 1px solid #2A4E62;
                border-radius: 6px;
                background: #071620;
                color: #FFFFFF;
                text-align: center;
                font-weight: 800;
            }

            QProgressBar#depthBar::chunk {
                border-radius: 5px;
                background: #2D8AB6;
            }

            QProgressBar#batteryBarGood::chunk {
                border-radius: 5px;
                background: #2D9D72;
            }

            QProgressBar#batteryBarWarn::chunk {
                border-radius: 5px;
                background: #B89A2F;
            }

            QProgressBar#batteryBarCritical::chunk {
                border-radius: 5px;
                background: #C44959;
            }

            QProgressBar#batteryBarNoData::chunk {
                border-radius: 5px;
                background: #405663;
            }

            QSplitter::handle {
                background: #17374A;
                width: 2px;
            }
            """
        )

    # ------------------------------------------------------------------ close

    def closeEvent(
        self,
        event: QCloseEvent,
    ):
        try:
            self.render_timer.stop()
            self.status_timer.stop()
        except Exception:
            pass

        try:
            self.reader.stop()
            self.reader.wait(
                2000
            )
        except Exception:
            pass


        try:
            self.shared.close()
        except Exception:
            pass

        release_windows_runtime()
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

    if np is None:
        QMessageBox.critical(
            None,
            APP_TITLE,
            "NumPy is required.\n\n"
            "Install: pip install numpy",
        )
        return 1

    try:
        window = (
            OtherSensorsWindow()
        )
    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Other Sensors:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
