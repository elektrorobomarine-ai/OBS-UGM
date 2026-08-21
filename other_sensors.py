"""
other_sensors.py
================

GRC-UGM-PERTAMINA OBS
Other Sensors Monitor

Version: 2
Shared data: shared_data_v5.py

Displays:
- IMU 3D attitude view (Roll / Pitch / Yaw + P/Q/R), based on the same visual
  convention used by geophone_imu:
      yaw 0° = North
      yaw 90° = East
      positive pitch = nose UP
      negative pitch = nose DOWN
- Temperature
- Depth + depth rate
- Leak sensor, 3 states:
      NO LEAK
      LEAK
      DISCONNECTED
- OBS battery monitor:
      2 × 6.4 V nominal LiFePO4 batteries in series
      nominal bank voltage = 12.8 V

Battery percentage
------------------
The default displayed percentage is an OPERATIONAL voltage estimate based on
the project's requested thresholds:

    13.2 V bank = 100 %
    12.0 V bank =  20 %

Between those voltages, percentage is linearly interpolated. Below 12.0 V it
falls linearly to 0 % at 11.0 V.

This is intentionally labelled "Voltage Estimate", not true electrochemical
State of Charge. LiFePO4 has a very flat discharge curve, so accurate SOC is
better obtained from a BMS or coulomb-counting current shunt.

Current shared_data_v4 telemetry already contains:
    roll, pitch, yaw
    angular_rate_p/q/r
    depth, depth_rate, temperature

Leak and battery-voltage fields are not yet part of the current shared-memory
layout. This module is forward-compatible and looks for these future fields:

Battery:
    battery_voltage
    voltage
    battery_v
    pack_voltage

Leak:
    leak_state
    leak_connected + leak_detected
    leak_detected

Until one of those fields exists, Leak shows DISCONNECTED and Battery shows
NO DATA. No raw_status bit is guessed or invented.

Dependencies
------------
    pip install PySide6 numpy pyqtgraph PyOpenGL PyOpenGL_accelerate
"""

from __future__ import annotations

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

from shared_data_v5 import OBSSharedData


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

# Battery configuration.
BATTERY_PACK_COUNT = 2
BATTERY_PACK_NOMINAL_V = 6.4
BATTERY_BANK_NOMINAL_V = (
    BATTERY_PACK_COUNT
    * BATTERY_PACK_NOMINAL_V
)

# Project-requested operational mapping.
BATTERY_OPERATIONAL_FULL_V = 13.2
BATTERY_OPERATIONAL_20_PERCENT_V = 12.0
BATTERY_OPERATIONAL_ZERO_V = 11.0


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


def operational_battery_percent(
    bank_voltage: float,
) -> float:
    """
    Project operational voltage estimate.

    13.2 V -> 100 %
    12.0 V ->  20 %
    11.0 V ->   0 %

    This is not a precise LiFePO4 electrochemical SOC estimator.
    """
    voltage = float(
        bank_voltage
    )

    if not math.isfinite(
        voltage
    ):
        return float("nan")

    if voltage >= BATTERY_OPERATIONAL_FULL_V:
        return 100.0

    if (
        voltage
        >= BATTERY_OPERATIONAL_20_PERCENT_V
    ):
        fraction = (
            (
                voltage
                - BATTERY_OPERATIONAL_20_PERCENT_V
            )
            / (
                BATTERY_OPERATIONAL_FULL_V
                - BATTERY_OPERATIONAL_20_PERCENT_V
            )
        )

        return (
            20.0
            + 80.0
            * fraction
        )

    if voltage > BATTERY_OPERATIONAL_ZERO_V:
        fraction = (
            (
                voltage
                - BATTERY_OPERATIONAL_ZERO_V
            )
            / (
                BATTERY_OPERATIONAL_20_PERCENT_V
                - BATTERY_OPERATIONAL_ZERO_V
            )
        )

        return (
            20.0
            * fraction
        )

    return 0.0


def extract_battery_voltage(
    telemetry,
) -> Optional[float]:
    for attr in (
        "battery_voltage",
        "voltage",
        "battery_v",
        "pack_voltage",
    ):
        if hasattr(
            telemetry,
            attr,
        ):
            try:
                value = float(
                    getattr(
                        telemetry,
                        attr,
                    )
                )

                if math.isfinite(
                    value
                ) and value > 0.0:
                    return value
            except Exception:
                pass

    return None


def extract_leak_state(
    telemetry,
) -> str:
    """
    Return:
        LEAK
        NO LEAK
        DISCONNECTED

    No raw status bit is guessed because the current protocol mapping does not
    define one for the leak sensor.
    """

    if hasattr(
        telemetry,
        "leak_state",
    ):
        value = getattr(
            telemetry,
            "leak_state",
        )

        text = str(
            value
        ).strip().upper()

        if text in (
            "LEAK",
            "WET",
            "ALARM",
            "1",
            "TRUE",
        ):
            return "LEAK"

        if text in (
            "NO LEAK",
            "NO_LEAK",
            "DRY",
            "OK",
            "0",
            "FALSE",
        ):
            return "NO LEAK"

        if text in (
            "DISCONNECTED",
            "OFFLINE",
            "UNKNOWN",
            "NONE",
        ):
            return "DISCONNECTED"

    if hasattr(
        telemetry,
        "leak_connected",
    ):
        try:
            connected = bool(
                getattr(
                    telemetry,
                    "leak_connected",
                )
            )

            if not connected:
                return "DISCONNECTED"

            if hasattr(
                telemetry,
                "leak_detected",
            ):
                return (
                    "LEAK"
                    if bool(
                        getattr(
                            telemetry,
                            "leak_detected",
                        )
                    )
                    else "NO LEAK"
                )

        except Exception:
            pass

    if hasattr(
        telemetry,
        "leak_detected",
    ):
        try:
            return (
                "LEAK"
                if bool(
                    getattr(
                        telemetry,
                        "leak_detected",
                    )
                )
                else "NO LEAK"
            )
        except Exception:
            pass

    return "DISCONNECTED"


# =============================================================================
# Telemetry worker
# =============================================================================


class TelemetryReaderThread(QThread):
    telemetry_ready = Signal(object)
    read_error = Signal(str)

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self._stop_event = (
            threading.Event()
        )

    def stop(self):
        self._stop_event.set()

    def run(self):
        shared = None
        last_timestamp = -1

        try:
            shared = OBSSharedData()

            while not self._stop_event.is_set():
                telemetry = (
                    shared.read_telemetry()
                )

                timestamp = int(
                    telemetry.timestamp_ns
                )

                if timestamp != last_timestamp:
                    last_timestamp = (
                        timestamp
                    )

                    self.telemetry_ready.emit(
                        telemetry
                    )

                self.msleep(
                    TELEMETRY_POLL_MS
                )

        except Exception as exc:
            if not self._stop_event.is_set():
                self.read_error.emit(
                    str(exc)
                )

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
            "IMU  •  Temperature  •  Depth  •  Leak Sensor  •  OBS Battery"
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
            16
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
            "Temperature from DEPTH telemetry"
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
            "Depth"
        )
        depth.setObjectName(
            "sensorGroup"
        )

        dl = QVBoxLayout(
            depth
        )
        dl.setContentsMargins(
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

        self.depth_rate_value = QLabel(
            "Rate: -- m/s"
        )
        self.depth_rate_value.setObjectName(
            "sensorSubValue"
        )
        self.depth_rate_value.setAlignment(
            Qt.AlignCenter
        )

        self.depth_bar = QProgressBar()
        self.depth_bar.setObjectName(
            "depthBar"
        )
        self.depth_bar.setRange(
            0,
            1000,
        )
        self.depth_bar.setValue(
            0
        )
        self.depth_bar.setTextVisible(
            False
        )

        depth_note = QLabel(
            "Bar auto-scales visually to 0–100 m"
        )
        depth_note.setObjectName(
            "hintText"
        )
        depth_note.setAlignment(
            Qt.AlignCenter
        )

        dl.addStretch(
            1
        )
        dl.addWidget(
            self.depth_value
        )
        dl.addWidget(
            self.depth_rate_value
        )
        dl.addWidget(
            self.depth_bar
        )
        dl.addStretch(
            1
        )
        dl.addWidget(
            depth_note
        )

        layout.addWidget(
            depth,
            0,
            1,
        )

        # Leak ----------------------------------------------------------
        leak = QGroupBox(
            "Leak Sensor"
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
            "DISCONNECTED"
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
            "States: NO LEAK / LEAK / DISCONNECTED"
        )
        leak_note.setObjectName(
            "hintText"
        )
        leak_note.setAlignment(
            Qt.AlignCenter
        )

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

        # Battery -------------------------------------------------------
        battery = QGroupBox(
            "OBS Battery"
        )
        battery.setObjectName(
            "sensorGroup"
        )

        bl = QVBoxLayout(
            battery
        )
        bl.setContentsMargins(
            12, 16, 12, 12
        )

        battery_heading = QLabel(
            "2 × 6.4 V LiFePO₄ Series"
        )
        battery_heading.setObjectName(
            "batteryHeading"
        )
        battery_heading.setAlignment(
            Qt.AlignCenter
        )

        self.battery_voltage_value = QLabel(
            "--.-- V"
        )
        self.battery_voltage_value.setObjectName(
            "sensorBigValue"
        )
        self.battery_voltage_value.setAlignment(
            Qt.AlignCenter
        )

        self.battery_percent_value = QLabel(
            "-- %"
        )
        self.battery_percent_value.setObjectName(
            "batteryPercent"
        )
        self.battery_percent_value.setAlignment(
            Qt.AlignCenter
        )

        self.battery_bar = QProgressBar()
        self.battery_bar.setObjectName(
            "batteryBarNoData"
        )
        self.battery_bar.setRange(
            0,
            100
        )
        self.battery_bar.setValue(
            0
        )
        self.battery_bar.setFormat(
            "%p%"
        )

        self.battery_state_value = QLabel(
            "NO DATA"
        )
        self.battery_state_value.setObjectName(
            "sensorSubValue"
        )
        self.battery_state_value.setAlignment(
            Qt.AlignCenter
        )

        battery_note = QLabel(
            "Voltage Estimate: 13.2 V = 100%, 12.0 V = 20%.\n"
            "Nominal bank = 12.8 V."
        )
        battery_note.setObjectName(
            "hintText"
        )
        battery_note.setAlignment(
            Qt.AlignCenter
        )
        battery_note.setWordWrap(
            True
        )

        bl.addWidget(
            battery_heading
        )
        bl.addStretch(
            1
        )
        bl.addWidget(
            self.battery_voltage_value
        )
        bl.addWidget(
            self.battery_percent_value
        )
        bl.addWidget(
            self.battery_bar
        )
        bl.addWidget(
            self.battery_state_value
        )
        bl.addStretch(
            1
        )
        bl.addWidget(
            battery_note
        )

        layout.addWidget(
            battery,
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

        self.target_roll = float(
            telemetry.roll
        )
        self.target_pitch = float(
            telemetry.pitch
        )
        self.target_yaw = float(
            telemetry.yaw
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
            f"Device ID "
            f"{self.latest_telemetry.ahrs_device_id}"
        )

    def _update_temperature_depth(self):
        telemetry = (
            self.latest_telemetry
        )

        if telemetry is None:
            self.temperature_value.setText(
                "--.- °C"
            )
            self.temperature_status.setText(
                "NO DATA"
            )

            self.depth_value.setText(
                "--.-- m"
            )
            self.depth_rate_value.setText(
                "Rate: -- m/s"
            )
            self.depth_bar.setValue(
                0
            )
            return

        live = (
            self._telemetry_is_live()
        )

        temperature = float(
            telemetry.temperature
        )

        depth = float(
            telemetry.depth
        )

        depth_rate = float(
            telemetry.depth_rate
        )

        self.temperature_value.setText(
            f"{temperature:.2f} °C"
        )
        self.temperature_status.setText(
            "LIVE"
            if live
            else "STALE"
        )

        self.depth_value.setText(
            f"{depth:.2f} m"
        )
        self.depth_rate_value.setText(
            f"Rate: {depth_rate:+.3f} m/s"
        )

        # 0..100 m visual bar. Numeric readout is not clamped.
        self.depth_bar.setValue(
            int(
                max(
                    0.0,
                    min(
                        100.0,
                        depth,
                    ),
                )
                * 10.0
            )
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
        telemetry = (
            self.latest_telemetry
        )

        if telemetry is None:
            leak_state = (
                "DISCONNECTED"
            )
        else:
            leak_state = (
                extract_leak_state(
                    telemetry
                )
            )

        if leak_state == "LEAK":
            self.leak_icon.setText(
                "⚠"
            )
            self.leak_value.setText(
                "LEAK"
            )
            self.leak_warning.setText(
                "⚠  LEAK DETECTED  ⚠\n"
                "CHECK OBS IMMEDIATELY"
            )

            self._set_dynamic_object_name(
                self.leak_icon,
                "leakAlarmIcon",
            )
            self._set_dynamic_object_name(
                self.leak_value,
                "leakAlarm",
            )

        elif leak_state == "NO LEAK":
            self.leak_icon.setText(
                "●"
            )
            self.leak_value.setText(
                "NO LEAK"
            )
            self.leak_warning.setText(
                "Dry / normal"
            )

            self._set_dynamic_object_name(
                self.leak_icon,
                "leakGoodIcon",
            )
            self._set_dynamic_object_name(
                self.leak_value,
                "leakGood",
            )

        else:
            self.leak_icon.setText(
                "●"
            )
            self.leak_value.setText(
                "DISCONNECTED"
            )
            self.leak_warning.setText(
                "Leak-sensor telemetry unavailable"
            )

            self._set_dynamic_object_name(
                self.leak_icon,
                "leakDisconnectedIcon",
            )
            self._set_dynamic_object_name(
                self.leak_value,
                "leakDisconnected",
            )

    def _update_battery(self):
        telemetry = (
            self.latest_telemetry
        )

        voltage = (
            extract_battery_voltage(
                telemetry
            )
            if telemetry is not None
            else None
        )

        if voltage is None:
            self.battery_voltage_value.setText(
                "--.-- V"
            )
            self.battery_percent_value.setText(
                "-- %"
            )
            self.battery_bar.setValue(
                0
            )
            self.battery_state_value.setText(
                "NO DATA"
            )

            self._set_dynamic_object_name(
                self.battery_bar,
                "batteryBarNoData",
            )
            return

        percent = (
            operational_battery_percent(
                voltage
            )
        )

        percent_clamped = int(
            round(
                max(
                    0.0,
                    min(
                        100.0,
                        percent,
                    ),
                )
            )
        )

        self.battery_voltage_value.setText(
            f"{voltage:.2f} V"
        )
        self.battery_percent_value.setText(
            f"{percent_clamped:d} %"
        )
        self.battery_bar.setValue(
            percent_clamped
        )

        if percent_clamped > 50:
            state = "NORMAL"
            bar_style = (
                "batteryBarGood"
            )
        elif percent_clamped > 20:
            state = "LOW"
            bar_style = (
                "batteryBarWarn"
            )
        else:
            state = "CRITICAL"
            bar_style = (
                "batteryBarCritical"
            )

        self.battery_state_value.setText(
            f"{state} • Voltage Estimate"
        )

        self._set_dynamic_object_name(
            self.battery_bar,
            bar_style,
        )

    # ------------------------------------------------------------------ render/status

    def render_frame(self):
        self._update_orientation()
        self._update_imu_labels()
        self._update_temperature_depth()
        self._update_leak()
        self._update_battery()

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
            telemetry = (
                self.shared.read_telemetry()
            )

            connected = bool(
                telemetry.command_connected
                or telemetry.data_connected
            )

            self.connection_label.setText(
                "Shared RAM: CONNECTED"
                if connected
                else "Shared RAM: NOT CONNECTED"
            )

            age_ms = max(
                0.0,
                (
                    time.time_ns()
                    - int(
                        telemetry.timestamp_ns
                    )
                )
                / 1_000_000.0,
            ) if int(
                telemetry.timestamp_ns
            ) > 0 else float("inf")

            if math.isfinite(
                age_ms
            ):
                self.telemetry_label.setText(
                    f"Telemetry age: {age_ms:.0f} ms"
                )
            else:
                self.telemetry_label.setText(
                    "Telemetry age: --"
                )

            self.power_mode_label.setText(
                f"Power Mode: {telemetry.power_mode}"
            )

            leak_state = (
                extract_leak_state(
                    telemetry
                )
            )

            if leak_state == "LEAK":
                self._set_system_state(
                    "⚠ LEAK",
                    "systemAlarm",
                )
            elif not connected:
                self._set_system_state(
                    "OFFLINE",
                    "systemWaiting",
                )
            elif (
                age_ms
                > TELEMETRY_STALE_S
                * 1000.0
            ):
                self._set_system_state(
                    "STALE",
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
