"""
geophone_3d.py
==============

GRC-UGM-PERTAMINA OBS
3D Particle Motion + Combined XYZ Waveform

Version: 4
Shared data: shared_data_v5.py

Purpose
-------
Visualize three-component geophone motion in two synchronized views:

1. 3D PARTICLE-MOTION TRAJECTORY
   CH0 = X, CH1 = Y, CH2 = Z are plotted as one XYZ trajectory in a
   hardware-accelerated OpenGL 3D view. The trajectory is centered by removing
   the per-window mean so the plot emphasizes particle motion rather than DC.

2. COMBINED REAL-TIME WAVEFORM
   X, Y, and Z are drawn together on ONE time-domain plot, using the same
   presentation playhead as the 3D trajectory.

Performance architecture
------------------------
This module follows the same smooth-display architecture used by the validated
geophone_realtime implementation:

- shared RAM is copied only when new ADC data exists;
- RAM reads occur in a dedicated QThread;
- GUI renders from a local cached NumPy snapshot;
- a sample-index jitter buffer decouples display timing from the OBS 128-sample
  bulk packet cadence;
- producer throughput is estimated from the shared ADC sample counter;
- 2D waveform uses one QOpenGLWidget-backed PyQtGraph view;
- 3D trajectory uses GLViewWidget / OpenGL;
- no interpolation or fabricated ADC samples are introduced;
- v4 reads the authoritative effective ADC stream rate from shared_data_v5;
- all time-window sample counts, gap thresholds, timestamp interpolation and
  jitter-buffer pacing are recalculated from the effective stream rate;
- ADC session/rate changes reset the local playhead so samples from different
  acquisition configurations are not visually mixed.

Example:
    raw ADC = 1000 Hz
    Average N = 5
    shared ADC = 200 Hz

A 2-second particle-motion window therefore uses about 400 output samples,
not 2000 raw-rate samples.

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
from collections import deque
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.3D"
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

        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()

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
# Qt
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFont,
    QIcon,
    QKeySequence,
    QSurfaceFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception:
    QOpenGLWidget = None


# =============================================================================
# NumPy / PyQtGraph / OpenGL
# =============================================================================

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pyqtgraph as pg
except ImportError:
    pg = None

try:
    import pyqtgraph.opengl as gl
except Exception:
    gl = None


# =============================================================================
# Shared data
# =============================================================================

from shared_data_v5 import RAW_ADC_SAMPLE_RATE_HZ, OBSSharedData


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Geophone 3D Particle Motion"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

DEFAULT_RENDER_FPS = 60
FPS_CHOICES = (30, 45, 60, 75, 90)

DEFAULT_BUFFER_MS = 1536
BUFFER_CHOICES_MS = (512, 768, 1024, 1536, 2048, 3072)

DEFAULT_WAVEFORM_SPAN_S = 5.0
WAVEFORM_SPAN_CHOICES_S = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)

DEFAULT_TRAJECTORY_SPAN_S = 2.0
TRAJECTORY_SPAN_CHOICES_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)

DEFAULT_MAX_TRAJECTORY_POINTS = 2500
TRAJECTORY_POINT_CHOICES = (500, 1000, 2500, 5000, 10000)

DEFAULT_Y_MIN = -8_388_608.0
DEFAULT_Y_MAX = 8_388_607.0

MAX_WAVE_RENDER_POINTS = 7000
ADC_READER_POLL_MS = 5
STATUS_INTERVAL_MS = 500
AUTO_RANGE_INTERVAL_S = 0.10

PRODUCER_RATE_WINDOW_S = 5.0

# Measured producer rate is used only to pace the jitter-buffer playhead.
# Physical time calibration comes from shared_data_v5 effective Fs.
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0

# Visual colors: X red, Y green, Z blue.
COLOR_X = "#FF5E5E"
COLOR_Y = "#5FE07B"
COLOR_Z = "#5C96FF"

# Bottom 3D scope geometry.
AXIS_SCOPE_HALF_LENGTH = 3.2
AXIS_SCOPE_OFFSET = 2.25
AXIS_SCOPE_Z_LENGTH = 5.6
AXIS_SCOPE_AMPLITUDE = 1.15


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
# Background shared-RAM reader
# =============================================================================


class ADCReaderThread(QThread):
    """Copy ADC shared RAM only when new source samples exist."""

    # snapshot, measured producer rate, publish gap ms, ADCStreamInfoSnapshot
    snapshot_ready = Signal(object, float, float, object)
    read_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._stop_event = threading.Event()
        self._count_lock = threading.Lock()
        # Conservative startup count; once v5 metadata is attached the GUI
        # updates this using the effective ADC rate.
        self._desired_count = int(
            (DEFAULT_WAVEFORM_SPAN_S + DEFAULT_BUFFER_MS / 1000.0 + 2.0)
            * RAW_ADC_SAMPLE_RATE_HZ
        )

        self._rate_history = deque()
        self._producer_rate_hz = float(RAW_ADC_SAMPLE_RATE_HZ)
        self._effective_rate_hz = float(RAW_ADC_SAMPLE_RATE_HZ)
        self._adc_session_id = -1
        self._last_publish_time: Optional[float] = None

    def stop(self) -> None:
        self._stop_event.set()

    def set_desired_count(self, count: int) -> None:
        with self._count_lock:
            self._desired_count = max(64, int(count))

    def _get_desired_count(self) -> int:
        with self._count_lock:
            return int(self._desired_count)

    def run(self) -> None:
        shared = None
        last_total = -1

        try:
            shared = OBSSharedData()
            stream_info = shared.read_adc_stream_info()
            self._effective_rate_hz = max(0.001, float(stream_info.effective_sample_rate_hz))
            self._producer_rate_hz = self._effective_rate_hz
            self._adc_session_id = int(stream_info.adc_session_id)

            while not self._stop_event.is_set():
                stream_info = shared.read_adc_stream_info()
                effective_rate_hz = max(0.001, float(stream_info.effective_sample_rate_hz))
                session_id = int(stream_info.adc_session_id)

                if (
                    session_id != self._adc_session_id
                    or abs(effective_rate_hz - self._effective_rate_hz)
                    > max(1.0e-9, 1.0e-6 * effective_rate_hz)
                ):
                    self._adc_session_id = session_id
                    self._effective_rate_hz = effective_rate_hz
                    self._producer_rate_hz = effective_rate_hz
                    self._rate_history.clear()
                    self._last_publish_time = None
                    last_total = -1

                total = shared.adc_total_samples()

                if total != last_total:
                    now = time.perf_counter()
                    snapshot = shared.read_adc_latest_numpy(
                        self._get_desired_count()
                    )
                    current_total = int(snapshot.total_samples)

                    if self._last_publish_time is None:
                        publish_gap_ms = 0.0
                    else:
                        publish_gap_ms = (
                            now - self._last_publish_time
                        ) * 1000.0

                    self._last_publish_time = now
                    self._rate_history.append((now, current_total))

                    cutoff = now - PRODUCER_RATE_WINDOW_S
                    while (
                        len(self._rate_history) > 2
                        and self._rate_history[0][0] < cutoff
                    ):
                        self._rate_history.popleft()

                    if len(self._rate_history) >= 2:
                        t0, n0 = self._rate_history[0]
                        t1, n1 = self._rate_history[-1]
                        dt = t1 - t0
                        dn = n1 - n0

                        if dt >= 1.0 and dn > 0:
                            measured = dn / dt
                            min_rate = max(
                                0.001,
                                self._effective_rate_hz * PRODUCER_RATE_MIN_RATIO,
                            )
                            max_rate = max(
                                min_rate * 2.0,
                                self._effective_rate_hz * PRODUCER_RATE_MAX_RATIO,
                            )

                            if min_rate <= measured <= max_rate:
                                self._producer_rate_hz = (
                                    0.80 * self._producer_rate_hz
                                    + 0.20 * measured
                                )

                    last_total = current_total

                    self.snapshot_ready.emit(
                        snapshot,
                        float(self._producer_rate_hz),
                        float(publish_gap_ms),
                        stream_info,
                    )

                self.msleep(ADC_READER_POLL_MS)

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


class Geophone3DWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        if np is None or pg is None:
            raise RuntimeError(
                "Geophone 3D requires NumPy and PyQtGraph."
            )

        self.shared: Optional[OBSSharedData] = None
        try:
            self.shared = OBSSharedData()
        except Exception as exc:
            raise RuntimeError(
                f"Cannot attach OBS shared RAM: {exc}"
            ) from exc

        # Authoritative ADC stream configuration.
        try:
            stream_info = self.shared.read_adc_stream_info()
            self.raw_sample_rate_hz = float(stream_info.raw_sample_rate_hz)
            self.effective_sample_rate_hz = max(0.001, float(stream_info.effective_sample_rate_hz))
            self.decimation_samples = int(stream_info.decimation_samples)
            self.decimation_mode = str(stream_info.decimation_mode)
            self.adc_session_id = int(stream_info.adc_session_id)
        except Exception:
            self.raw_sample_rate_hz = float(RAW_ADC_SAMPLE_RATE_HZ)
            self.effective_sample_rate_hz = float(RAW_ADC_SAMPLE_RATE_HZ)
            self.decimation_samples = 1
            self.decimation_mode = "raw"
            self.adc_session_id = -1

        # Cached ADC state.
        self.cached_adc = None
        self.cached_total_samples = -1
        self.producer_rate_hz = float(self.effective_sample_rate_hz)
        self.source_publish_gap_ms = 0.0

        # Smooth presentation playhead.
        self.playhead_sample: Optional[float] = None
        self.playhead_wall_ns: Optional[int] = None
        self.reserve_ms = 0.0
        self.buffer_underruns = 0
        self._in_underrun = False

        # Render state.
        self.paused = False
        self.render_fps = 0.0
        self.render_jitter_ms = 0.0
        self._fps_count = 0
        self._fps_start = time.perf_counter()
        self._last_render_ns: Optional[int] = None
        self._last_auto_range = 0.0

        self.wave_gl_active = False
        self.wave_gl_error = ""

        # Visual objects.
        # Top: original true XYZ particle-motion view.
        self.gl_view = None
        self.trajectory_line = None
        self.current_point = None

        # Bottom: separate real-time X/Y/Z curves in one 3D axis scope.
        self.axis_gl_view = None
        self.axis_trace_x = None
        self.axis_trace_y = None
        self.axis_trace_z = None
        self.axis_point_x = None
        self.axis_point_y = None
        self.axis_point_z = None
        self.axis_text_items = []

        self.setWindowTitle(
            f"{APP_TITLE} - {SYSTEM_TITLE}"
        )

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.resize(1500, 900)
        self.setMinimumSize(1100, 700)

        self._configure_pyqtgraph()
        self._build_ui()
        self._apply_style()
        self._install_shortcuts()

        # Reader worker.
        self.reader = ADCReaderThread(self)
        self.reader.snapshot_ready.connect(self.on_adc_snapshot)
        self.reader.read_error.connect(self.on_reader_error)
        self.reader.start()
        self._update_reader_window()

        # Render timer.
        self.render_timer = QTimer(self)
        try:
            self.render_timer.setTimerType(Qt.TimerType.PreciseTimer)
        except Exception:
            pass
        self.render_timer.timeout.connect(self.render_frame)
        self._set_render_fps(DEFAULT_RENDER_FPS)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(STATUS_INTERVAL_MS)
        self.refresh_status()

    # ------------------------------------------------------------------ graph config

    @staticmethod
    def _configure_pyqtgraph() -> None:
        try:
            pg.setConfigOptions(
                useOpenGL=True,
                antialias=False,
                background="#07131D",
                foreground="#DDEAF2",
            )
        except Exception:
            pg.setConfigOptions(
                useOpenGL=False,
                antialias=False,
                background="#07131D",
                foreground="#DDEAF2",
            )

    def _install_wave_opengl(self, graphics) -> None:
        if QOpenGLWidget is None:
            self.wave_gl_error = "QOpenGLWidget unavailable"
            return

        try:
            viewport = QOpenGLWidget()
            fmt = QSurfaceFormat()
            fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
            fmt.setSamples(0)
            fmt.setSwapInterval(0)
            viewport.setFormat(fmt)
            graphics.setViewport(viewport)
            self.wave_gl_active = isinstance(
                graphics.viewport(),
                QOpenGLWidget,
            )
        except Exception as exc:
            self.wave_gl_error = str(exc)
            self.wave_gl_active = False

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(1)

        title = QLabel("3D GEOPHONE PARTICLE MOTION")
        title.setObjectName("titleLabel")
        subtitle = QLabel(
            "Top: True XYZ Particle Motion  •  Bottom: Separated X/Y/Z 3D Real-Time Curves"
        )
        subtitle.setObjectName("subtitleLabel")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("pauseButton")
        self.pause_button.setCheckable(True)
        self.pause_button.setMinimumWidth(125)
        self.pause_button.clicked.connect(self.toggle_pause)
        header.addWidget(self.pause_button)

        root.addLayout(header)

        # Status bar.
        status = QFrame()
        status.setObjectName("statusFrame")
        sl = QHBoxLayout(status)
        sl.setContentsMargins(10, 6, 10, 6)

        self.connection_label = QLabel("Shared RAM: checking...")
        self.connection_label.setObjectName("statusLabel")
        self.stream_label = QLabel("ADC: --")
        self.stream_label.setObjectName("statusLabel")
        self.render_label = QLabel("Render: --")
        self.render_label.setObjectName("statusLabel")
        self.mode_label = QLabel("LIVE")
        self.mode_label.setObjectName("modeLive")

        sl.addWidget(self.connection_label)
        sl.addStretch(1)
        sl.addWidget(self.stream_label)
        sl.addSpacing(14)
        sl.addWidget(self.render_label)
        sl.addSpacing(14)
        sl.addWidget(self.mode_label)
        root.addWidget(status)

        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setChildrenCollapsible(False)

        main_splitter.addWidget(self._build_visualization_panel())
        main_splitter.addWidget(self._build_control_panel())
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([1120, 380])

        root.addWidget(main_splitter, 1)

    def _build_visualization_panel(self) -> QWidget:
        panel = QFrame()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        vertical = QSplitter(Qt.Vertical)
        vertical.setChildrenCollapsible(False)

        # TOP: original true 3D particle-motion trajectory.
        trajectory_frame = QFrame()
        trajectory_frame.setObjectName("viewFrame")
        tlay = QVBoxLayout(trajectory_frame)
        tlay.setContentsMargins(0, 0, 0, 0)

        if gl is not None:
            self.gl_view = gl.GLViewWidget()
            self.gl_view.setCameraPosition(
                distance=9.0,
                elevation=22.0,
                azimuth=-45.0,
            )
            tlay.addWidget(self.gl_view, 1)
            self._build_3d_scene()
        else:
            missing = QLabel(
                "3D OpenGL unavailable.\n\n"
                "Install:\n"
                "pip install PyOpenGL PyOpenGL_accelerate"
            )
            missing.setAlignment(Qt.AlignCenter)
            missing.setObjectName("missing3DLabel")
            tlay.addWidget(missing, 1)

        vertical.addWidget(trajectory_frame)

        # BOTTOM: real-time curves separated along X/Y/Z axes.
        axis_frame = QFrame()
        axis_frame.setObjectName("viewFrame")
        alay = QVBoxLayout(axis_frame)
        alay.setContentsMargins(0, 0, 0, 0)

        if gl is not None:
            self.axis_gl_view = gl.GLViewWidget()
            self.axis_gl_view.setCameraPosition(
                distance=10.5,
                elevation=23.0,
                azimuth=-42.0,
            )
            alay.addWidget(self.axis_gl_view, 1)
            self._build_axis_scope_scene()
        else:
            missing_axis = QLabel(
                "3D axis-scope unavailable.\n\n"
                "Install PyOpenGL / PyOpenGL_accelerate."
            )
            missing_axis.setAlignment(Qt.AlignCenter)
            missing_axis.setObjectName("missing3DLabel")
            alay.addWidget(missing_axis, 1)

        vertical.addWidget(axis_frame)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        vertical.setSizes([520, 330])

        layout.addWidget(vertical, 1)
        return panel

    def _build_3d_scene(self) -> None:
        if gl is None or self.gl_view is None:
            return

        # Reference floor in XY plane.
        grid = gl.GLGridItem()
        grid.setSize(x=8.0, y=8.0, z=1.0)
        grid.setSpacing(x=1.0, y=1.0, z=1.0)
        self.gl_view.addItem(grid)

        axes = gl.GLAxisItem()
        axes.setSize(x=3.5, y=3.5, z=3.5)
        self.gl_view.addItem(axes)

        # Main particle-motion trail.
        self.trajectory_line = gl.GLLinePlotItem(
            pos=np.zeros((2, 3), dtype=np.float32),
            color=(0.20, 0.85, 1.00, 1.0),
            width=2.0,
            antialias=False,
            mode="line_strip",
        )
        self.gl_view.addItem(self.trajectory_line)

        # Current particle position.
        try:
            self.current_point = gl.GLScatterPlotItem(
                pos=np.zeros((1, 3), dtype=np.float32),
                color=(1.0, 0.85, 0.20, 1.0),
                size=10.0,
                pxMode=True,
            )
            self.gl_view.addItem(self.current_point)
        except Exception:
            self.current_point = None

    def _build_axis_scope_scene(self) -> None:
        """Bottom 3D scope: X, Y and Z curves follow their own axes."""
        if gl is None or self.axis_gl_view is None:
            return

        grid = gl.GLGridItem()
        grid.setSize(x=9.0, y=9.0, z=1.0)
        grid.setSpacing(x=1.0, y=1.0, z=1.0)
        self.axis_gl_view.addItem(grid)

        axes = gl.GLAxisItem()
        axes.setSize(x=3.7, y=3.7, z=3.7)
        self.axis_gl_view.addItem(axes)

        # Baselines make it obvious which trace belongs to which axis.
        z0 = 0.15
        baselines = (
            gl.GLLinePlotItem(
                pos=np.array([[-AXIS_SCOPE_HALF_LENGTH,-AXIS_SCOPE_OFFSET,z0],
                              [ AXIS_SCOPE_HALF_LENGTH,-AXIS_SCOPE_OFFSET,z0]],dtype=np.float32),
                color=(1.0,0.25,0.25,0.35), width=1.0, antialias=False, mode="lines"),
            gl.GLLinePlotItem(
                pos=np.array([[AXIS_SCOPE_OFFSET,-AXIS_SCOPE_HALF_LENGTH,z0],
                              [AXIS_SCOPE_OFFSET, AXIS_SCOPE_HALF_LENGTH,z0]],dtype=np.float32),
                color=(0.25,1.0,0.35,0.35), width=1.0, antialias=False, mode="lines"),
            gl.GLLinePlotItem(
                pos=np.array([[-AXIS_SCOPE_OFFSET,AXIS_SCOPE_OFFSET,z0],
                              [-AXIS_SCOPE_OFFSET,AXIS_SCOPE_OFFSET,z0+AXIS_SCOPE_Z_LENGTH]],dtype=np.float32),
                color=(0.30,0.55,1.0,0.40), width=1.0, antialias=False, mode="lines"),
        )
        for item in baselines:
            self.axis_gl_view.addItem(item)

        self.axis_trace_x = gl.GLLinePlotItem(
            pos=np.zeros((2,3),dtype=np.float32), color=(1.0,0.28,0.28,1.0),
            width=2.2, antialias=False, mode="line_strip")
        self.axis_trace_y = gl.GLLinePlotItem(
            pos=np.zeros((2,3),dtype=np.float32), color=(0.28,1.0,0.38,1.0),
            width=2.2, antialias=False, mode="line_strip")
        self.axis_trace_z = gl.GLLinePlotItem(
            pos=np.zeros((2,3),dtype=np.float32), color=(0.30,0.58,1.0,1.0),
            width=2.2, antialias=False, mode="line_strip")
        for item in (self.axis_trace_x,self.axis_trace_y,self.axis_trace_z):
            self.axis_gl_view.addItem(item)

        try:
            self.axis_point_x=gl.GLScatterPlotItem(pos=np.zeros((1,3),dtype=np.float32),color=(1,.3,.3,1),size=8,pxMode=True)
            self.axis_point_y=gl.GLScatterPlotItem(pos=np.zeros((1,3),dtype=np.float32),color=(.3,1,.4,1),size=8,pxMode=True)
            self.axis_point_z=gl.GLScatterPlotItem(pos=np.zeros((1,3),dtype=np.float32),color=(.32,.62,1,1),size=8,pxMode=True)
            for item in (self.axis_point_x,self.axis_point_y,self.axis_point_z):
                self.axis_gl_view.addItem(item)
        except Exception:
            self.axis_point_x=self.axis_point_y=self.axis_point_z=None

        if hasattr(gl,"GLTextItem"):
            specs=(
                ("X / CH0",(AXIS_SCOPE_HALF_LENGTH+.35,-AXIS_SCOPE_OFFSET,z0+.05),(1,.45,.45,1)),
                ("Y / CH1",(AXIS_SCOPE_OFFSET,AXIS_SCOPE_HALF_LENGTH+.35,z0+.05),(.45,1,.55,1)),
                ("Z / CH2",(-AXIS_SCOPE_OFFSET,AXIS_SCOPE_OFFSET,z0+AXIS_SCOPE_Z_LENGTH+.3),(.5,.7,1,1)),
            )
            for text,pos,color in specs:
                try:
                    item=gl.GLTextItem(pos=pos,text=text,color=color)
                    self.axis_gl_view.addItem(item)
                    self.axis_text_items.append(item)
                except Exception:
                    pass

    def _build_control_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("controlPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(8)

        heading = QLabel("DISPLAY CONTROLS")
        heading.setObjectName("settingsTitle")
        layout.addWidget(heading)

        perf = QGroupBox("Performance")
        perf.setObjectName("channelGroup")
        grid = QGridLayout(perf)
        grid.setContentsMargins(10, 12, 10, 10)
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(6)

        self.fps_combo = QComboBox()
        for fps in FPS_CHOICES:
            self.fps_combo.addItem(f"{fps} FPS", fps)
        self.fps_combo.setCurrentText(f"{DEFAULT_RENDER_FPS} FPS")
        self.fps_combo.currentIndexChanged.connect(self.on_fps_changed)

        self.buffer_combo = QComboBox()
        for ms in BUFFER_CHOICES_MS:
            self.buffer_combo.addItem(f"{ms} ms", ms)
        self.buffer_combo.setCurrentText(f"{DEFAULT_BUFFER_MS} ms")
        self.buffer_combo.currentIndexChanged.connect(self.on_buffer_changed)

        self.wave_span_combo = QComboBox()
        for value in WAVEFORM_SPAN_CHOICES_S:
            self.wave_span_combo.addItem(f"{value:g} s", float(value))
        self.wave_span_combo.setCurrentText(f"{DEFAULT_WAVEFORM_SPAN_S:g} s")
        self.wave_span_combo.currentIndexChanged.connect(self.on_wave_span_changed)

        self.trajectory_span_combo = QComboBox()
        for value in TRAJECTORY_SPAN_CHOICES_S:
            self.trajectory_span_combo.addItem(f"{value:g} s", float(value))
        self.trajectory_span_combo.setCurrentText(
            f"{DEFAULT_TRAJECTORY_SPAN_S:g} s"
        )
        self.trajectory_span_combo.currentIndexChanged.connect(
            self.on_trajectory_setting_changed
        )

        self.trajectory_points_combo = QComboBox()
        for count in TRAJECTORY_POINT_CHOICES:
            self.trajectory_points_combo.addItem(f"{count:,}", count)
        self.trajectory_points_combo.setCurrentText(
            f"{DEFAULT_MAX_TRAJECTORY_POINTS:,}"
        )

        grid.addWidget(QLabel("Target FPS"), 0, 0)
        grid.addWidget(self.fps_combo, 0, 1)
        grid.addWidget(QLabel("Smooth Buffer"), 1, 0)
        grid.addWidget(self.buffer_combo, 1, 1)
        grid.addWidget(QLabel("Axis Scope Span"), 2, 0)
        grid.addWidget(self.wave_span_combo, 2, 1)
        grid.addWidget(QLabel("3D Trail Span"), 3, 0)
        grid.addWidget(self.trajectory_span_combo, 3, 1)
        grid.addWidget(QLabel("Max 3D Points"), 4, 0)
        grid.addWidget(self.trajectory_points_combo, 4, 1)

        layout.addWidget(perf)

        trajectory = QGroupBox("3D Trajectory")
        trajectory.setObjectName("channelGroup")
        tg = QGridLayout(trajectory)
        tg.setContentsMargins(10, 12, 10, 10)

        self.center_trajectory_check = QCheckBox(
            "Remove mean / center trajectory"
        )
        self.center_trajectory_check.setChecked(True)

        self.normalize_trajectory_check = QCheckBox(
            "Normalize X / Y / Z equally"
        )
        self.normalize_trajectory_check.setChecked(False)

        self.auto_3d_scale_check = QCheckBox(
            "Auto 3D camera scale"
        )
        self.auto_3d_scale_check.setChecked(True)

        tg.addWidget(self.center_trajectory_check, 0, 0, 1, 2)
        tg.addWidget(self.normalize_trajectory_check, 1, 0, 1, 2)
        tg.addWidget(self.auto_3d_scale_check, 2, 0, 1, 2)

        axis_note = QLabel(
            "3D axes: X = red reference, Y = green reference, Z = blue reference. "
            "The cyan trail is the synchronized CH0/CH1/CH2 particle-motion path."
        )
        axis_note.setWordWrap(True)
        axis_note.setObjectName("sampleInfo")
        tg.addWidget(axis_note, 3, 0, 1, 2)

        layout.addWidget(trajectory)

        waveform = QGroupBox("Bottom 3D Axis Scope")
        waveform.setObjectName("channelGroup")
        wg = QGridLayout(waveform)
        wg.setContentsMargins(10, 12, 10, 10)

        scope_note = QLabel(
            "CH0/X runs along the X direction, CH1/Y along Y, and CH2/Z along Z. "
            "Their scope positions are offset so the curves stay separated."
        )
        scope_note.setWordWrap(True)
        scope_note.setObjectName("sampleInfo")
        wg.addWidget(scope_note, 0, 0, 1, 2)

        layout.addWidget(waveform)

        info = QGroupBox("Current Vector")
        info.setObjectName("channelGroup")
        ig = QGridLayout(info)
        ig.setContentsMargins(10, 12, 10, 10)

        self.x_value_label = QLabel("X: --")
        self.y_value_label = QLabel("Y: --")
        self.z_value_label = QLabel("Z: --")
        self.magnitude_label = QLabel("|V|: --")

        for label in (
            self.x_value_label,
            self.y_value_label,
            self.z_value_label,
            self.magnitude_label,
        ):
            label.setObjectName("numericValue")

        ig.addWidget(self.x_value_label, 0, 0)
        ig.addWidget(self.y_value_label, 1, 0)
        ig.addWidget(self.z_value_label, 2, 0)
        ig.addWidget(self.magnitude_label, 3, 0)

        layout.addWidget(info)
        layout.addStretch(1)

        return panel

    # ------------------------------------------------------------------ style

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background-color: #07131D;
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
                background-color: #0B1B27;
                border: 1px solid #17374A;
                border-radius: 8px;
            }

            QFrame#viewFrame {
                background-color: #07131D;
                border: 1px solid #17374A;
                border-radius: 7px;
            }

            QLabel#statusLabel {
                color: #B7CBD6;
                font-size: 10px;
            }

            QLabel#modeLive {
                background-color: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 7px;
                color: #A9F1D2;
                font-weight: 800;
                padding: 3px 10px;
            }

            QLabel#modePaused {
                background-color: #403510;
                border: 1px solid #A88821;
                border-radius: 7px;
                color: #FFE49A;
                font-weight: 800;
                padding: 3px 10px;
            }

            QLabel#settingsTitle {
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 800;
                letter-spacing: 1px;
            }

            QGroupBox#channelGroup {
                background-color: #0D1E2A;
                border: 1px solid #1A3D52;
                border-radius: 9px;
                margin-top: 11px;
                padding-top: 6px;
                font-weight: 800;
                color: #FFFFFF;
            }

            QGroupBox#channelGroup::title {
                subcontrol-origin: margin;
                left: 9px;
                padding: 0px 5px;
                color: #FFFFFF;
            }

            QLabel#numericValue {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 14px;
                font-weight: 800;
            }

            QLabel#sampleInfo {
                color: #7894A4;
                font-size: 9px;
            }

            QLabel#missing3DLabel {
                color: #FFDCA8;
                font-size: 11px;
                padding: 20px;
            }

            QDoubleSpinBox,
            QComboBox {
                background-color: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 5px;
                min-height: 25px;
                padding: 1px 5px;
            }

            QComboBox QAbstractItemView {
                background-color: #0B1B26;
                color: #F4FAFD;
                border: 1px solid #2B526A;
                selection-background-color: #245B79;
                selection-color: #FFFFFF;
                outline: none;
                padding: 3px;
            }

            QComboBox QAbstractItemView::item {
                color: #F4FAFD;
                background-color: #0B1B26;
                min-height: 26px;
                padding: 4px 8px;
            }

            QComboBox QAbstractItemView::item:selected {
                color: #FFFFFF;
                background-color: #245B79;
            }

            QCheckBox {
                color: #DDE9EF;
                spacing: 6px;
            }

            QPushButton {
                min-height: 28px;
                border-radius: 6px;
                padding: 3px 7px;
                font-weight: 700;
            }

            QPushButton#pauseButton,
            QPushButton#smallPrimaryButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QPushButton#pauseButton:checked {
                background-color: #705C16;
                border: 1px solid #B49326;
            }

            QSplitter::handle {
                background-color: #17374A;
                width: 2px;
                height: 2px;
            }
            """
        )

    # ------------------------------------------------------------------ shortcuts

    def _install_shortcuts(self) -> None:
        action = QAction(self)
        action.setShortcut(QKeySequence(Qt.Key_Space))
        action.triggered.connect(self.toggle_pause_shortcut)
        self.addAction(action)

    # ------------------------------------------------------------------ settings

    def current_fps(self) -> int:
        return int(
            self.fps_combo.currentData()
            or DEFAULT_RENDER_FPS
        )

    def current_buffer_ms(self) -> int:
        return int(
            self.buffer_combo.currentData()
            or DEFAULT_BUFFER_MS
        )

    def current_wave_span_s(self) -> float:
        return float(
            self.wave_span_combo.currentData()
            or DEFAULT_WAVEFORM_SPAN_S
        )

    def current_trajectory_span_s(self) -> float:
        return float(
            self.trajectory_span_combo.currentData()
            or DEFAULT_TRAJECTORY_SPAN_S
        )

    def current_max_trajectory_points(self) -> int:
        return int(
            self.trajectory_points_combo.currentData()
            or DEFAULT_MAX_TRAJECTORY_POINTS
        )

    def _set_render_fps(self, fps: int) -> None:
        interval_ms = max(
            1,
            round(1000.0 / max(1, int(fps))),
        )
        self.render_timer.start(interval_ms)

    def on_fps_changed(self, *_args) -> None:
        self._set_render_fps(self.current_fps())

    def on_buffer_changed(self, *_args) -> None:
        self._reset_playhead()
        self._update_reader_window()

    def on_wave_span_changed(self, *_args) -> None:
        span = self.current_wave_span_s()
        self.wave_plot.setXRange(-span, 0.0, padding=0.0)
        self._update_reader_window()

    def on_trajectory_setting_changed(self, *_args) -> None:
        self._update_reader_window()

    def current_sample_rate_hz(self) -> float:
        return max(0.001, float(self.effective_sample_rate_hz))

    def _update_reader_window(self) -> None:
        if not hasattr(self, "reader"):
            return

        seconds = max(
            self.current_wave_span_s(),
            self.current_trajectory_span_s(),
        )
        seconds += self.current_buffer_ms() / 1000.0 + 1.0

        self.reader.set_desired_count(
            int(seconds * self.current_sample_rate_hz()) + 64
        )

    def apply_y_range(self) -> None:
        # Retained for compatibility with older UI revisions.
        return

    # ------------------------------------------------------------------ reader callback

    def on_adc_snapshot(
        self,
        snapshot,
        producer_rate_hz: float,
        publish_gap_ms: float,
        stream_info,
    ) -> None:
        previous_total = self.cached_total_samples
        previous_session_id = int(self.adc_session_id)
        previous_effective_rate = float(self.effective_sample_rate_hz)

        self.cached_adc = snapshot
        self.cached_total_samples = int(snapshot.total_samples)
        self.raw_sample_rate_hz = float(stream_info.raw_sample_rate_hz)
        self.effective_sample_rate_hz = max(0.001, float(stream_info.effective_sample_rate_hz))
        self.decimation_samples = max(1, int(stream_info.decimation_samples))
        self.decimation_mode = str(stream_info.decimation_mode)
        self.adc_session_id = int(stream_info.adc_session_id)

        min_rate = max(0.001, self.effective_sample_rate_hz * PRODUCER_RATE_MIN_RATIO)
        max_rate = max(min_rate * 2.0, self.effective_sample_rate_hz * PRODUCER_RATE_MAX_RATIO)
        if min_rate <= float(producer_rate_hz) <= max_rate:
            self.producer_rate_hz = float(producer_rate_hz)

        if publish_gap_ms >= 0.0:
            if self.source_publish_gap_ms <= 0.0:
                self.source_publish_gap_ms = float(publish_gap_ms)
            else:
                self.source_publish_gap_ms = (
                    0.85 * self.source_publish_gap_ms
                    + 0.15 * float(publish_gap_ms)
                )

        session_changed = self.adc_session_id != previous_session_id
        rate_changed = (
            abs(self.effective_sample_rate_hz - previous_effective_rate)
            > max(1.0e-9, 1.0e-6 * self.effective_sample_rate_hz)
        )
        counter_reset = (
            previous_total >= 0
            and self.cached_total_samples < previous_total
        )

        if session_changed or rate_changed or counter_reset:
            self.producer_rate_hz = float(self.effective_sample_rate_hz)
            self.source_publish_gap_ms = 0.0
            self._reset_playhead()
            self.buffer_underruns = 0
            self._in_underrun = False
            self._update_reader_window()

    def on_reader_error(self, message: str) -> None:
        self.connection_label.setText(
            f"Shared-data reader error: {message}"
        )

    # ------------------------------------------------------------------ smooth playhead

    def _reset_playhead(self) -> None:
        self.playhead_sample = None
        self.playhead_wall_ns = None

    def _display_sample_index(self, adc) -> float:
        total = int(adc.total_samples)
        if total <= 1:
            return 0.0

        latest_sample = float(total - 1)
        oldest_sample = float(total - len(adc.ch0))

        effective_rate = self.current_sample_rate_hz()
        min_rate = max(0.001, effective_rate * PRODUCER_RATE_MIN_RATIO)
        max_rate = max(min_rate * 2.0, effective_rate * PRODUCER_RATE_MAX_RATIO)
        rate = max(min_rate, min(max_rate, float(self.producer_rate_hz)))

        configured_buffer_samples = (
            self.current_buffer_ms() / 1000.0 * rate
        )

        gap_s = max(
            0.001,
            self.source_publish_gap_ms / 1000.0,
        )

        burst_reserve = (
            3.0 * gap_s + 0.100
        ) * rate

        target_reserve = max(
            configured_buffer_samples * 0.70,
            burst_reserve,
        )

        safety_reserve = max(
            configured_buffer_samples * 0.35,
            (2.0 * gap_s + 0.050) * rate,
            32.0,
        )

        available_history = max(
            64.0,
            latest_sample - oldest_sample,
        )

        target_reserve = min(
            target_reserve,
            available_history * 0.80,
        )

        safety_reserve = min(
            safety_reserve,
            max(32.0, target_reserve * 0.75),
        )

        now_ns = time.perf_counter_ns()

        if (
            self.playhead_sample is None
            or self.playhead_wall_ns is None
        ):
            self.playhead_sample = max(
                oldest_sample,
                latest_sample - target_reserve,
            )
            self.playhead_wall_ns = now_ns
            self._in_underrun = False
            return float(self.playhead_sample)

        elapsed_s = max(
            0.0,
            (now_ns - self.playhead_wall_ns) / 1_000_000_000.0,
        )

        reserve_samples = latest_sample - self.playhead_sample
        self.reserve_ms = (
            reserve_samples / max(1.0, rate) * 1000.0
        )

        error_fraction = (
            reserve_samples - target_reserve
        ) / max(1.0, target_reserve)

        correction = max(
            -0.25,
            min(0.12, error_fraction * 0.40),
        )

        playback_rate = rate * (1.0 + correction)
        proposed = (
            self.playhead_sample
            + elapsed_s * playback_rate
        )

        max_playhead = latest_sample - safety_reserve

        if proposed > max_playhead:
            proposed = max_playhead
            if not self._in_underrun:
                self.buffer_underruns += 1
                self._in_underrun = True
        else:
            recovery = max(
                16.0,
                rate * gap_s * 0.50,
            )
            if reserve_samples > safety_reserve + recovery:
                self._in_underrun = False

        if proposed < oldest_sample:
            proposed = oldest_sample

        self.playhead_sample = proposed
        self.playhead_wall_ns = now_ns
        return float(self.playhead_sample)

    # ------------------------------------------------------------------ render helpers

    @staticmethod
    def _downsample_xy(x, y, max_points: int):
        count = len(y)
        if count <= max_points:
            return x, y, 1

        step = int(math.ceil(count / max_points))
        return x[::step], y[::step], step

    def _break_true_gaps(self, x, y, render_step: int):
        if len(y) < 2:
            return y, "all"

        expected_dt = (
            max(1, int(render_step))
            / self.current_sample_rate_hz()
        )

        gaps = np.flatnonzero(
            np.diff(x) > expected_dt * 1.75
        )

        if not len(gaps):
            return y, "all"

        result = y.astype(np.float64, copy=True)
        result[gaps + 1] = np.nan
        return result, "finite"

    def _visible_indices(self, adc):
        n = len(adc.ch0)
        if n < 2:
            return None

        playhead = self._display_sample_index(adc)
        cache_start = int(adc.total_samples) - n

        end_index = int(
            np.floor(playhead - cache_start)
        ) + 1
        end_index = max(0, min(n, end_index))

        if end_index < 2:
            return None

        fractional = playhead - np.floor(playhead)
        display_ns = int(
            adc.timestamp_ns[end_index - 1]
            + fractional
            * (1_000_000_000 / self.current_sample_rate_hz())
        )

        return end_index, display_ns

    # ------------------------------------------------------------------ combined waveform

    def _render_combined_waveform(
        self,
        adc,
        end_index: int,
        display_ns: int,
        auto_range: bool,
    ) -> None:
        span_s = self.current_wave_span_s()
        start_ns = display_ns - int(span_s * 1_000_000_000)

        start_index = int(
            np.searchsorted(
                adc.timestamp_ns[:end_index],
                start_ns,
                side="left",
            )
        )

        ts = adc.timestamp_ns[start_index:end_index]
        if len(ts) < 2:
            return

        x_time = (
            ts.astype(np.float64, copy=False)
            - float(display_ns)
        ) / 1_000_000_000.0

        rendered_values = []

        for source, curve in (
            (adc.ch0[start_index:end_index], self.curve_x),
            (adc.ch1[start_index:end_index], self.curve_y),
            (adc.ch2[start_index:end_index], self.curve_z),
        ):
            x_render, y_render, step = self._downsample_xy(
                x_time,
                source,
                MAX_WAVE_RENDER_POINTS,
            )

            y_render, connect_mode = self._break_true_gaps(
                x_render,
                y_render,
                step,
            )

            curve.setData(
                x_render,
                y_render,
                connect=connect_mode,
            )

            rendered_values.append(y_render)

        if self.auto_y_check.isChecked() and auto_range:
            finite_arrays = [
                a[np.isfinite(a)]
                for a in rendered_values
                if len(a)
            ]

            finite_arrays = [a for a in finite_arrays if len(a)]

            if finite_arrays:
                all_values = np.concatenate(finite_arrays)
                y_min = float(np.min(all_values))
                y_max = float(np.max(all_values))

                if y_max <= y_min:
                    margin = max(1.0, abs(y_min) * 0.05)
                else:
                    margin = (y_max - y_min) * 0.08

                self.wave_plot.setYRange(
                    y_min - margin,
                    y_max + margin,
                    padding=0.0,
                )

        # Current numeric values use the actual final visible sample.
        x_now = float(adc.ch0[end_index - 1])
        y_now = float(adc.ch1[end_index - 1])
        z_now = float(adc.ch2[end_index - 1])
        magnitude = math.sqrt(
            x_now * x_now
            + y_now * y_now
            + z_now * z_now
        )

        self.x_value_label.setText(f"X: {x_now:,.0f}")
        self.y_value_label.setText(f"Y: {y_now:,.0f}")
        self.z_value_label.setText(f"Z: {z_now:,.0f}")
        self.magnitude_label.setText(f"|V|: {magnitude:,.0f}")

    @staticmethod
    def _robust_abs_scale(signal) -> float:
        if not len(signal):
            return 1.0
        finite=signal[np.isfinite(signal)]
        if not len(finite):
            return 1.0
        return max(float(np.percentile(np.abs(finite),99.0)),1.0)

    def _render_axis_scope(self, x, y, z) -> None:
        if gl is None or self.axis_trace_x is None:
            return
        count=min(len(x),len(y),len(z))
        if count<2:
            return
        x=x[-count:]; y=y[-count:]; z=z[-count:]

        sx=self._robust_abs_scale(x); sy=self._robust_abs_scale(y); sz=self._robust_abs_scale(z)
        # Use a common amplitude scale so relative X/Y/Z magnitudes remain meaningful.
        common=max(sx,sy,sz,1.0); sx=sy=sz=common
        xa=x/sx*AXIS_SCOPE_AMPLITUDE
        ya=y/sy*AXIS_SCOPE_AMPLITUDE
        za=z/sz*AXIS_SCOPE_AMPLITUDE
        u=np.linspace(-1.0,1.0,count,dtype=np.float64)
        z0=0.15

        px=np.column_stack((u*AXIS_SCOPE_HALF_LENGTH,
                            np.full(count,-AXIS_SCOPE_OFFSET),
                            z0+xa)).astype(np.float32,copy=False)
        py=np.column_stack((np.full(count,AXIS_SCOPE_OFFSET),
                            u*AXIS_SCOPE_HALF_LENGTH,
                            z0+ya)).astype(np.float32,copy=False)
        ztime=(u+1.0)*0.5*AXIS_SCOPE_Z_LENGTH+z0
        pz=np.column_stack((-AXIS_SCOPE_OFFSET+za,
                            np.full(count,AXIS_SCOPE_OFFSET),
                            ztime)).astype(np.float32,copy=False)

        self.axis_trace_x.setData(pos=px); self.axis_trace_y.setData(pos=py); self.axis_trace_z.setData(pos=pz)
        if self.axis_point_x is not None: self.axis_point_x.setData(pos=px[-1:].copy())
        if self.axis_point_y is not None: self.axis_point_y.setData(pos=py[-1:].copy())
        if self.axis_point_z is not None: self.axis_point_z.setData(pos=pz[-1:].copy())
        if self.auto_3d_scale_check.isChecked() and self.axis_gl_view is not None:
            try:
                self.axis_gl_view.opts["distance"]=10.5
                self.axis_gl_view.update()
            except Exception:
                pass

    # ------------------------------------------------------------------ 3D particle trajectory

    def _render_trajectory(
        self,
        adc,
        end_index: int,
        display_ns: int,
    ) -> None:
        if (
            gl is None
            or self.trajectory_line is None
        ):
            return

        span_s = self.current_trajectory_span_s()
        start_ns = display_ns - int(span_s * 1_000_000_000)

        start_index = int(
            np.searchsorted(
                adc.timestamp_ns[:end_index],
                start_ns,
                side="left",
            )
        )

        x = adc.ch0[start_index:end_index].astype(
            np.float64,
            copy=False,
        )
        y = adc.ch1[start_index:end_index].astype(
            np.float64,
            copy=False,
        )
        z = adc.ch2[start_index:end_index].astype(
            np.float64,
            copy=False,
        )

        count = min(len(x), len(y), len(z))
        if count < 2:
            return

        x = x[-count:]
        y = y[-count:]
        z = z[-count:]

        if self.center_trajectory_check.isChecked():
            x = x - np.mean(x)
            y = y - np.mean(y)
            z = z - np.mean(z)

        # Bottom scope uses the same synchronized window, but keeps its own separated geometry.
        self._render_axis_scope(x, y, z)

        if self.normalize_trajectory_check.isChecked():
            # One common scale is deliberately used for all three channels so
            # relative axis amplitudes are preserved.
            common = max(
                float(np.max(np.abs(x))),
                float(np.max(np.abs(y))),
                float(np.max(np.abs(z))),
                1.0,
            )
            x = x / common
            y = y / common
            z = z / common

        max_points = self.current_max_trajectory_points()
        if count > max_points:
            step = int(math.ceil(count / max_points))
            x = x[::step]
            y = y[::step]
            z = z[::step]

        points = np.column_stack((x, y, z)).astype(
            np.float32,
            copy=False,
        )

        self.trajectory_line.setData(pos=points)

        if self.current_point is not None:
            self.current_point.setData(
                pos=points[-1:].copy()
            )

        if self.auto_3d_scale_check.isChecked():
            # GLViewWidget camera distance is scalar. Robust percentile avoids
            # one transient outlier zooming the trajectory too far away.
            radial = np.linalg.norm(points, axis=1)
            if len(radial):
                radius = float(
                    np.percentile(radial, 99.0)
                )
                radius = max(radius, 1.0e-6)

                # For raw ADC counts this may be millions; GL renders them fine
                # but a very large camera distance loses numerical precision.
                # Scale the displayed coordinates only if needed, preserving
                # trajectory shape.
                if radius > 1.0e4:
                    visual_scale = 3.0 / radius
                    points_scaled = points * visual_scale
                    self.trajectory_line.setData(pos=points_scaled)
                    if self.current_point is not None:
                        self.current_point.setData(
                            pos=points_scaled[-1:].copy()
                        )
                    camera_radius = 3.0
                else:
                    camera_radius = radius

                try:
                    self.gl_view.opts["distance"] = max(
                        5.0,
                        camera_radius * 3.0,
                    )
                    self.gl_view.update()
                except Exception:
                    pass

    # ------------------------------------------------------------------ render

    def render_frame(self) -> None:
        if self.paused:
            return

        adc = self.cached_adc
        if adc is None or len(adc.ch0) < 2:
            return

        visible = self._visible_indices(adc)
        if visible is None:
            return

        end_index, display_ns = visible

        now = time.perf_counter()
        auto_range = (
            now - self._last_auto_range
            >= AUTO_RANGE_INTERVAL_S
        )

        # Bottom 3D scope is updated from _render_trajectory() using the same synchronized data window.
        x_now=float(adc.ch0[end_index-1]); y_now=float(adc.ch1[end_index-1]); z_now=float(adc.ch2[end_index-1])
        magnitude=math.sqrt(x_now*x_now+y_now*y_now+z_now*z_now)
        self.x_value_label.setText(f"X: {x_now:,.0f}")
        self.y_value_label.setText(f"Y: {y_now:,.0f}")
        self.z_value_label.setText(f"Z: {z_now:,.0f}")
        self.magnitude_label.setText(f"|V|: {magnitude:,.0f}")

        self._render_trajectory(
            adc,
            end_index,
            display_ns,
        )

        if auto_range:
            self._last_auto_range = now

        self._update_render_metrics()

    def _update_render_metrics(self) -> None:
        now_ns = time.perf_counter_ns()

        if self._last_render_ns is not None:
            interval_ms = (
                now_ns - self._last_render_ns
            ) / 1_000_000.0

            target_ms = 1000.0 / self.current_fps()
            jitter = abs(interval_ms - target_ms)
            self.render_jitter_ms = (
                0.90 * self.render_jitter_ms
                + 0.10 * jitter
            )

        self._last_render_ns = now_ns
        self._fps_count += 1

        now = time.perf_counter()
        elapsed = now - self._fps_start

        if elapsed >= 0.75:
            self.render_fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_start = now

    # ------------------------------------------------------------------ pause / status

    def toggle_pause(self, checked: bool) -> None:
        self.paused = bool(checked)

        if self.paused:
            self.pause_button.setText("Continue")
            self.mode_label.setText("PAUSED")
            self.mode_label.setObjectName("modePaused")
        else:
            self.pause_button.setText("Pause")
            self.mode_label.setText("LIVE")
            self.mode_label.setObjectName("modeLive")
            self.playhead_wall_ns = time.perf_counter_ns()

        self.mode_label.style().unpolish(self.mode_label)
        self.mode_label.style().polish(self.mode_label)

    def toggle_pause_shortcut(self) -> None:
        checked = not self.pause_button.isChecked()
        self.pause_button.setChecked(checked)
        self.toggle_pause(checked)

    def refresh_status(self) -> None:
        try:
            telemetry = self.shared.read_telemetry()
            bulk = self.shared.read_bulk_status()
            total = self.shared.adc_total_samples()
            stream_info = self.shared.read_adc_stream_info()
            self.raw_sample_rate_hz = float(stream_info.raw_sample_rate_hz)
            self.effective_sample_rate_hz = max(0.001, float(stream_info.effective_sample_rate_hz))
            self.decimation_samples = max(1, int(stream_info.decimation_samples))
            self.decimation_mode = str(stream_info.decimation_mode)
            self.adc_session_id = int(stream_info.adc_session_id)

            self.connection_label.setText(
                "Shared RAM: DATA CONNECTED"
                if telemetry.data_connected
                else "Shared RAM: DATA NOT CONNECTED"
            )

            self.stream_label.setText(
                f"ADC {total:,} | "
                f"Fs {self.effective_sample_rate_hz:6.1f} Hz "
                f"(raw {self.raw_sample_rate_hz:6.1f}/N{self.decimation_samples}) | "
                f"producer {self.producer_rate_hz:6.1f} Hz | "
                f"reserve {self.reserve_ms:3.0f} ms | "
                f"underrun {self.buffer_underruns} | "
                f"drop {bulk.dropped_frames} | "
                f"sync {bulk.channel_id_mismatches} | "
                f"session {self.adc_session_id}"
            )

            wave_mode = "Axis 3D GL" if gl is not None else "Axis 3D unavailable"
            trajectory_mode = "Particle 3D GL" if gl is not None else "Particle 3D unavailable"

            self.render_label.setText(
                f"Render {self.render_fps:4.1f} FPS | "
                f"jitter {self.render_jitter_ms:3.1f} ms | "
                f"{wave_mode} + {trajectory_mode}"
            )

            tooltip = (
                f"Executable: {sys.executable}\n"
                "Top particle motion and bottom axis scope both use PyQtGraph GLViewWidget/OpenGL.\n"
                "Assign this executable to the NVIDIA High performance GPU "
                "in Windows Graphics Settings if desired."
            )

            if self.wave_gl_error:
                tooltip = self.wave_gl_error + "\n\n" + tooltip

            self.render_label.setToolTip(tooltip)

        except Exception as exc:
            self.connection_label.setText(
                f"Shared RAM status error: {exc}"
            )

    # ------------------------------------------------------------------ close

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.render_timer.stop()
            self.status_timer.stop()
        except Exception:
            pass

        try:
            self.reader.stop()
            self.reader.wait(2000)
        except Exception:
            pass

        if self.shared is not None:
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
    if QOpenGLWidget is not None:
        try:
            fmt = QSurfaceFormat()
            fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
            fmt.setSamples(0)
            fmt.setSwapInterval(0)
            QSurfaceFormat.setDefaultFormat(fmt)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(
        f"{APP_TITLE} - {SYSTEM_TITLE}"
    )

    icon = application_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    font = QFont("Segoe UI")
    font.setPointSize(9)
    app.setFont(font)

    if np is None or pg is None:
        missing = []
        if np is None:
            missing.append("numpy")
        if pg is None:
            missing.append("pyqtgraph")

        QMessageBox.critical(
            None,
            APP_TITLE,
            "Required package(s) missing:\n\n"
            + ", ".join(missing),
        )
        return 1

    try:
        window = Geophone3DWindow()
    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Geophone 3D:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
