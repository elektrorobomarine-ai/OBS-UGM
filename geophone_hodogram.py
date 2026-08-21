"""
geophone_hodogram.py
====================

GRC-UGM-PERTAMINA OBS
Real-Time Hodogram / Polarization View

Version: 2
Shared data: shared_data_v5.py

Displays three synchronized planar hodograms:
    XY : CH0 / X versus CH1 / Y
    XZ : CH0 / X versus CH2 / Z
    YZ : CH1 / Y versus CH2 / Z

The same smooth sample-index presentation clock used by the real-time waveform
modules is used here. No ADC samples are interpolated or fabricated.

Polarization analysis
---------------------
A 3x3 covariance matrix is calculated from the currently displayed X/Y/Z
window. Its eigenvectors/eigenvalues are used to estimate:

    Principal vector   : dominant particle-motion direction
    Rectilinearity     : 0..1
    Planarity          : 0..1
    Horizontal angle   : atan2(Y, X), degrees
    Inclination        : angle above/below XY plane, degrees

The dominant principal axis is also projected onto the XY, XZ and YZ
hodograms.

Performance
-----------
- Shared RAM copying is performed only when new ADC samples exist.
- RAM copying runs in a dedicated QThread.
- GUI renders from a cached NumPy snapshot.
- A sample-index jitter buffer decouples the GUI from 128-sample TCP bursts.
- All three hodograms share one PyQtGraph GraphicsLayoutWidget.
- OpenGL is requested through one QOpenGLWidget viewport.
- Polarization PCA is only a 3x3 eigendecomposition, so NumPy CPU processing is
  intentionally used; GPU/CUDA would add more transfer overhead than compute.
- v2 reads the authoritative effective ADC rate from shared_data_v5.
  Hodogram/polarization span remains specified in seconds, while the number of
  X/Y/Z samples used automatically follows the effective shared-stream rate.
- The measured producer rate is used only for jitter-buffer presentation; it
  does not redefine the physical sample rate.
- ADC session or decimation-rate changes reset the local presentation playhead
  so windows from different acquisition configurations are never mixed.

Example:
    raw ADC = 1000 Hz
    Average N = 5
    effective shared rate = 200 Hz

A 2-second hodogram/polarization window therefore contains about 400 samples
before optional display point reduction.

Dependencies
------------
    pip install PySide6 numpy pyqtgraph
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

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.HODOGRAM"
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
# NumPy / PyQtGraph
# =============================================================================

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pyqtgraph as pg
except ImportError:
    pg = None


# =============================================================================
# Shared data
# =============================================================================

from shared_data_v5 import RAW_ADC_SAMPLE_RATE_HZ, OBSSharedData


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Geophone Hodogram"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

DEFAULT_RENDER_FPS = 60
FPS_CHOICES = (30, 45, 60, 75, 90)

DEFAULT_BUFFER_MS = 1536
BUFFER_CHOICES_MS = (512, 768, 1024, 1536, 2048, 3072)

DEFAULT_HODOGRAM_SPAN_S = 2.0
HODOGRAM_SPAN_CHOICES_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)

DEFAULT_MAX_POINTS = 3000
MAX_POINTS_CHOICES = (500, 1000, 2000, 3000, 5000, 10000)

DEFAULT_MANUAL_RANGE = 1_000_000.0

ADC_READER_POLL_MS = 5
STATUS_INTERVAL_MS = 500
AUTO_RANGE_INTERVAL_S = 0.10
POLARIZATION_UPDATE_INTERVAL_S = 0.10

PRODUCER_RATE_WINDOW_S = 5.0

# Measured producer throughput is a presentation diagnostic only.
# Physical time calibration comes from shared_data_v5 effective Fs.
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0

PLOT_MARGIN_FRACTION = 0.10
PRINCIPAL_AXIS_FRACTION = 0.85

COLOR_XY = "#54D4FF"
COLOR_XZ = "#FFB454"
COLOR_YZ = "#A98CFF"
COLOR_CURRENT = "#FFF176"
COLOR_AXIS = "#FFFFFF"


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


def safe_percentile_abs(values, percentile=99.0) -> float:
    if values is None or not len(values):
        return 1.0

    finite = values[np.isfinite(values)]
    if not len(finite):
        return 1.0

    return max(
        float(np.percentile(np.abs(finite), percentile)),
        1.0,
    )


# =============================================================================
# Background ADC reader
# =============================================================================


class ADCReaderThread(QThread):
    # snapshot, measured producer rate, publish gap ms, ADCStreamInfoSnapshot
    snapshot_ready = Signal(object, float, float, object)
    read_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._stop_event = threading.Event()
        self._count_lock = threading.Lock()
        # Conservative startup count. Once v5 metadata is attached, the GUI
        # updates this from the effective shared-stream rate.
        self._desired_count = int(
            (
                DEFAULT_HODOGRAM_SPAN_S
                + DEFAULT_BUFFER_MS / 1000.0
                + 2.0
            )
            * RAW_ADC_SAMPLE_RATE_HZ
        )

        self._rate_history = deque()
        self._producer_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._effective_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
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
            self._effective_rate_hz = max(
                0.001,
                float(stream_info.effective_sample_rate_hz),
            )
            self._producer_rate_hz = self._effective_rate_hz
            self._adc_session_id = int(stream_info.adc_session_id)

            while not self._stop_event.is_set():
                stream_info = shared.read_adc_stream_info()
                effective_rate_hz = max(
                    0.001,
                    float(stream_info.effective_sample_rate_hz),
                )
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

                    self._rate_history.append(
                        (now, current_total)
                    )

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
                                self._effective_rate_hz
                                * PRODUCER_RATE_MIN_RATIO,
                            )
                            max_rate = max(
                                min_rate * 2.0,
                                self._effective_rate_hz
                                * PRODUCER_RATE_MAX_RATIO,
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


class GeophoneHodogramWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        if np is None or pg is None:
            raise RuntimeError(
                "Geophone Hodogram requires NumPy and PyQtGraph."
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
            self.effective_sample_rate_hz = max(
                0.001,
                float(stream_info.effective_sample_rate_hz),
            )
            self.decimation_samples = max(1, int(stream_info.decimation_samples))
            self.decimation_mode = str(stream_info.decimation_mode)
            self.adc_session_id = int(stream_info.adc_session_id)
        except Exception:
            self.raw_sample_rate_hz = float(RAW_ADC_SAMPLE_RATE_HZ)
            self.effective_sample_rate_hz = float(RAW_ADC_SAMPLE_RATE_HZ)
            self.decimation_samples = 1
            self.decimation_mode = "raw"
            self.adc_session_id = -1

        # Cached source data.
        self.cached_adc = None
        self.cached_total = -1
        self.producer_rate_hz = float(self.effective_sample_rate_hz)
        self.publish_gap_ms = 0.0

        # Sample-index playback clock.
        self.playhead_sample: Optional[float] = None
        self.playhead_wall_ns: Optional[int] = None
        self.reserve_ms = 0.0
        self.underruns = 0
        self._in_underrun = False

        # Render metrics.
        self.paused = False
        self.render_fps = 0.0
        self.render_jitter_ms = 0.0

        self._fps_count = 0
        self._fps_start = time.perf_counter()
        self._last_render_ns: Optional[int] = None

        self._last_auto_range = 0.0
        self._last_polarization_update = 0.0

        self.opengl_active = False
        self.opengl_error = ""

        # Last polarization calculation.
        self.principal_vector = np.array(
            [1.0, 0.0, 0.0],
            dtype=np.float64,
        )

        self.eigenvalues = np.zeros(
            3,
            dtype=np.float64,
        )

        self.rectilinearity = 0.0
        self.planarity = 0.0
        self.horizontal_angle_deg = 0.0
        self.inclination_deg = 0.0

        # Plot items.
        self.plots = {}
        self.curves = {}
        self.current_points = {}
        self.principal_lines = {}

        self.setWindowTitle(
            f"{APP_TITLE} - {SYSTEM_TITLE}"
        )

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.resize(1450, 860)
        self.setMinimumSize(1050, 680)

        self._configure_pyqtgraph()
        self._build_ui()
        self._apply_style()
        self._install_shortcuts()

        self.reader = ADCReaderThread(self)
        self.reader.snapshot_ready.connect(
            self.on_adc_snapshot
        )
        self.reader.read_error.connect(
            self.on_reader_error
        )
        self.reader.start()

        self._update_reader_window()

        self.render_timer = QTimer(self)
        try:
            self.render_timer.setTimerType(
                Qt.TimerType.PreciseTimer
            )
        except Exception:
            pass

        self.render_timer.timeout.connect(
            self.render_frame
        )
        self._set_render_fps(
            DEFAULT_RENDER_FPS
        )

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(
            self.refresh_status
        )
        self.status_timer.start(
            STATUS_INTERVAL_MS
        )

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

    def _install_opengl_viewport(self, graphics) -> None:
        if QOpenGLWidget is None:
            self.opengl_error = (
                "QOpenGLWidget unavailable"
            )
            return

        try:
            viewport = QOpenGLWidget()

            fmt = QSurfaceFormat()
            fmt.setRenderableType(
                QSurfaceFormat.RenderableType.OpenGL
            )
            fmt.setSwapBehavior(
                QSurfaceFormat.SwapBehavior.DoubleBuffer
            )
            fmt.setSamples(0)
            fmt.setSwapInterval(0)

            viewport.setFormat(fmt)
            graphics.setViewport(viewport)

            self.opengl_active = isinstance(
                graphics.viewport(),
                QOpenGLWidget,
            )

        except Exception as exc:
            self.opengl_active = False
            self.opengl_error = str(exc)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(
            14, 12, 14, 12
        )
        root.setSpacing(8)

        header = QHBoxLayout()

        title_box = QVBoxLayout()
        title_box.setSpacing(1)

        title = QLabel(
            "GEOPHONE HODOGRAM / POLARIZATION"
        )
        title.setObjectName("titleLabel")

        subtitle = QLabel(
            "XY  •  XZ  •  YZ  •  Principal Motion Axis  •  Polarization Metrics"
        )
        subtitle.setObjectName(
            "subtitleLabel"
        )

        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.pause_button = QPushButton(
            "Pause"
        )
        self.pause_button.setObjectName(
            "pauseButton"
        )
        self.pause_button.setCheckable(
            True
        )
        self.pause_button.setMinimumWidth(
            125
        )
        self.pause_button.clicked.connect(
            self.toggle_pause
        )

        header.addWidget(
            self.pause_button
        )

        root.addLayout(header)

        # Status strip.
        status = QFrame()
        status.setObjectName(
            "statusFrame"
        )

        sl = QHBoxLayout(status)
        sl.setContentsMargins(
            10, 6, 10, 6
        )

        self.connection_label = QLabel(
            "Shared RAM: checking..."
        )
        self.connection_label.setObjectName(
            "statusLabel"
        )

        self.stream_label = QLabel(
            "ADC: --"
        )
        self.stream_label.setObjectName(
            "statusLabel"
        )

        self.render_label = QLabel(
            "Render: --"
        )
        self.render_label.setObjectName(
            "statusLabel"
        )

        self.mode_label = QLabel("LIVE")
        self.mode_label.setObjectName(
            "modeLive"
        )

        sl.addWidget(self.connection_label)
        sl.addStretch(1)
        sl.addWidget(self.stream_label)
        sl.addSpacing(14)
        sl.addWidget(self.render_label)
        sl.addSpacing(14)
        sl.addWidget(self.mode_label)

        root.addWidget(status)

        splitter = QSplitter(
            Qt.Horizontal
        )
        splitter.setChildrenCollapsible(
            False
        )

        splitter.addWidget(
            self._build_hodogram_panel()
        )
        splitter.addWidget(
            self._build_control_panel()
        )

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1080, 370])

        root.addWidget(splitter, 1)

    def _build_hodogram_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName(
            "viewFrame"
        )

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(
            0, 0, 0, 0
        )

        self.graphics = (
            pg.GraphicsLayoutWidget()
        )

        self._install_opengl_viewport(
            self.graphics
        )

        layout.addWidget(
            self.graphics,
            1,
        )

        definitions = (
            (
                "XY",
                0,
                0,
                "X / CH0",
                "Y / CH1",
                COLOR_XY,
            ),
            (
                "XZ",
                0,
                1,
                "X / CH0",
                "Z / CH2",
                COLOR_XZ,
            ),
            (
                "YZ",
                1,
                0,
                "Y / CH1",
                "Z / CH2",
                COLOR_YZ,
            ),
        )

        for (
            key,
            row,
            col,
            x_label,
            y_label,
            color,
        ) in definitions:

            plot = self.graphics.addPlot(
                row=row,
                col=col,
            )

            plot.setTitle(
                f"{key} Hodogram",
                color="#FFFFFF",
                size="11pt",
            )

            plot.setLabel(
                "bottom",
                x_label,
            )
            plot.setLabel(
                "left",
                y_label,
            )

            plot.showGrid(
                x=True,
                y=True,
                alpha=0.18,
            )

            plot.setMouseEnabled(
                x=True,
                y=True,
            )

            plot.setAspectLocked(
                True,
                ratio=1.0,
            )

            plot.setXRange(
                -DEFAULT_MANUAL_RANGE,
                DEFAULT_MANUAL_RANGE,
                padding=0.0,
            )
            plot.setYRange(
                -DEFAULT_MANUAL_RANGE,
                DEFAULT_MANUAL_RANGE,
                padding=0.0,
            )

            curve = plot.plot(
                [],
                [],
                pen=pg.mkPen(
                    color,
                    width=1.4,
                ),
            )

            point = pg.ScatterPlotItem(
                size=8,
                brush=pg.mkBrush(
                    COLOR_CURRENT
                ),
                pen=pg.mkPen(
                    COLOR_CURRENT
                ),
            )
            plot.addItem(point)

            principal = plot.plot(
                [],
                [],
                pen=pg.mkPen(
                    COLOR_AXIS,
                    width=1.5,
                    style=Qt.DashLine,
                ),
            )

            self.plots[key] = plot
            self.curves[key] = curve
            self.current_points[key] = point
            self.principal_lines[key] = (
                principal
            )

        # Bottom-right information panel inside graphics area.
        info = self.graphics.addLabel(
            row=1,
            col=1,
            justify="left",
        )
        self.graphics_info_label = info

        self._update_graphics_info()

        return panel

    def _build_control_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName(
            "controlPanel"
        )

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(
            8, 0, 0, 0
        )
        layout.setSpacing(8)

        heading = QLabel(
            "HODOGRAM SETTINGS"
        )
        heading.setObjectName(
            "settingsTitle"
        )

        layout.addWidget(heading)

        # Performance.
        perf = QGroupBox(
            "Display / Performance"
        )
        perf.setObjectName(
            "channelGroup"
        )

        grid = QGridLayout(perf)
        grid.setContentsMargins(
            10, 12, 10, 10
        )

        self.fps_combo = QComboBox()
        for fps in FPS_CHOICES:
            self.fps_combo.addItem(
                f"{fps} FPS",
                fps,
            )
        self.fps_combo.setCurrentText(
            f"{DEFAULT_RENDER_FPS} FPS"
        )
        self.fps_combo.currentIndexChanged.connect(
            self.on_fps_changed
        )

        self.buffer_combo = QComboBox()
        for ms in BUFFER_CHOICES_MS:
            self.buffer_combo.addItem(
                f"{ms} ms",
                ms,
            )
        self.buffer_combo.setCurrentText(
            f"{DEFAULT_BUFFER_MS} ms"
        )
        self.buffer_combo.currentIndexChanged.connect(
            self.on_buffer_changed
        )

        self.span_combo = QComboBox()
        for value in HODOGRAM_SPAN_CHOICES_S:
            self.span_combo.addItem(
                f"{value:g} s",
                float(value),
            )
        self.span_combo.setCurrentText(
            f"{DEFAULT_HODOGRAM_SPAN_S:g} s"
        )
        self.span_combo.currentIndexChanged.connect(
            self.on_span_changed
        )

        self.max_points_combo = QComboBox()
        for count in MAX_POINTS_CHOICES:
            self.max_points_combo.addItem(
                f"{count:,}",
                count,
            )
        self.max_points_combo.setCurrentText(
            f"{DEFAULT_MAX_POINTS:,}"
        )

        grid.addWidget(
            QLabel("Target FPS"),
            0,
            0,
        )
        grid.addWidget(
            self.fps_combo,
            0,
            1,
        )

        grid.addWidget(
            QLabel("Smooth Buffer"),
            1,
            0,
        )
        grid.addWidget(
            self.buffer_combo,
            1,
            1,
        )

        grid.addWidget(
            QLabel("Window"),
            2,
            0,
        )
        grid.addWidget(
            self.span_combo,
            2,
            1,
        )

        grid.addWidget(
            QLabel("Max Points"),
            3,
            0,
        )
        grid.addWidget(
            self.max_points_combo,
            3,
            1,
        )

        layout.addWidget(perf)

        # Hodogram controls.
        display = QGroupBox(
            "Hodogram"
        )
        display.setObjectName(
            "channelGroup"
        )

        dg = QGridLayout(display)
        dg.setContentsMargins(
            10, 12, 10, 10
        )

        self.remove_mean_check = QCheckBox(
            "Remove mean / center trajectory"
        )
        self.remove_mean_check.setChecked(
            True
        )

        self.equal_scale_check = QCheckBox(
            "Equal X / Y / Z scale"
        )
        self.equal_scale_check.setChecked(
            True
        )

        self.auto_range_check = QCheckBox(
            "Auto Range"
        )
        self.auto_range_check.setChecked(
            True
        )

        self.show_principal_check = QCheckBox(
            "Show principal polarization axis"
        )
        self.show_principal_check.setChecked(
            True
        )

        dg.addWidget(
            self.remove_mean_check,
            0,
            0,
            1,
            2,
        )
        dg.addWidget(
            self.equal_scale_check,
            1,
            0,
            1,
            2,
        )
        dg.addWidget(
            self.auto_range_check,
            2,
            0,
            1,
            2,
        )
        dg.addWidget(
            self.show_principal_check,
            3,
            0,
            1,
            2,
        )

        self.manual_range_spin = (
            QDoubleSpinBox()
        )
        self.manual_range_spin.setRange(
            1.0,
            100_000_000.0,
        )
        self.manual_range_spin.setDecimals(
            0
        )
        self.manual_range_spin.setValue(
            DEFAULT_MANUAL_RANGE
        )
        self.manual_range_spin.setGroupSeparatorShown(
            True
        )

        apply_range = QPushButton(
            "Apply Manual Range"
        )
        apply_range.setObjectName(
            "smallPrimaryButton"
        )
        apply_range.clicked.connect(
            self.apply_manual_range
        )

        dg.addWidget(
            QLabel("± Range"),
            4,
            0,
        )
        dg.addWidget(
            self.manual_range_spin,
            4,
            1,
        )
        dg.addWidget(
            apply_range,
            5,
            0,
            1,
            2,
        )

        layout.addWidget(display)

        # Polarization metrics.
        metrics = QGroupBox(
            "Polarization"
        )
        metrics.setObjectName(
            "channelGroup"
        )

        mg = QGridLayout(metrics)
        mg.setContentsMargins(
            10, 12, 10, 10
        )

        self.rect_value = QLabel(
            "Rectilinearity: --"
        )
        self.plan_value = QLabel(
            "Planarity: --"
        )
        self.angle_value = QLabel(
            "Horizontal angle: --"
        )
        self.incl_value = QLabel(
            "Inclination: --"
        )

        self.eig1_value = QLabel(
            "λ1: --"
        )
        self.eig2_value = QLabel(
            "λ2: --"
        )
        self.eig3_value = QLabel(
            "λ3: --"
        )

        for label in (
            self.rect_value,
            self.plan_value,
            self.angle_value,
            self.incl_value,
            self.eig1_value,
            self.eig2_value,
            self.eig3_value,
        ):
            label.setObjectName(
                "numericValue"
            )

        mg.addWidget(
            self.rect_value,
            0,
            0,
            1,
            2,
        )
        mg.addWidget(
            self.plan_value,
            1,
            0,
            1,
            2,
        )
        mg.addWidget(
            self.angle_value,
            2,
            0,
            1,
            2,
        )
        mg.addWidget(
            self.incl_value,
            3,
            0,
            1,
            2,
        )
        mg.addWidget(
            self.eig1_value,
            4,
            0,
        )
        mg.addWidget(
            self.eig2_value,
            5,
            0,
        )
        mg.addWidget(
            self.eig3_value,
            6,
            0,
        )

        note = QLabel(
            "Horizontal angle = atan2(Y, X). "
            "It is a sensor-axis angle, not a compass bearing unless the sensor axes are georeferenced."
        )
        note.setObjectName(
            "sampleInfo"
        )
        note.setWordWrap(True)

        mg.addWidget(
            note,
            7,
            0,
            1,
            2,
        )

        layout.addWidget(metrics)
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
                font-size: 12px;
                font-weight: 700;
            }

            QLabel#sampleInfo {
                color: #7894A4;
                font-size: 9px;
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
            }
            """
        )

    # ------------------------------------------------------------------ shortcuts

    def _install_shortcuts(self) -> None:
        action = QAction(self)
        action.setShortcut(
            QKeySequence(Qt.Key_Space)
        )
        action.triggered.connect(
            self.toggle_pause_shortcut
        )
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

    def current_span_s(self) -> float:
        return float(
            self.span_combo.currentData()
            or DEFAULT_HODOGRAM_SPAN_S
        )

    def current_max_points(self) -> int:
        return int(
            self.max_points_combo.currentData()
            or DEFAULT_MAX_POINTS
        )

    def _set_render_fps(self, fps: int) -> None:
        self.render_timer.start(
            max(
                1,
                round(
                    1000.0
                    / max(1, int(fps))
                ),
            )
        )

    def on_fps_changed(self, *_args) -> None:
        self._set_render_fps(
            self.current_fps()
        )

    def on_buffer_changed(self, *_args) -> None:
        self._reset_playhead()
        self._update_reader_window()

    def on_span_changed(self, *_args) -> None:
        self._update_reader_window()

    def current_sample_rate_hz(self) -> float:
        return max(0.001, float(self.effective_sample_rate_hz))

    def _update_reader_window(self) -> None:
        if not hasattr(self, "reader"):
            return

        seconds = (
            self.current_span_s()
            + self.current_buffer_ms()
            / 1000.0
            + 1.0
        )

        self.reader.set_desired_count(
            int(seconds * self.current_sample_rate_hz())
            + 64
        )

    def apply_manual_range(self) -> None:
        r = float(
            self.manual_range_spin.value()
        )

        self.auto_range_check.setChecked(
            False
        )

        for plot in self.plots.values():
            plot.setXRange(
                -r,
                r,
                padding=0.0,
            )
            plot.setYRange(
                -r,
                r,
                padding=0.0,
            )

    # ------------------------------------------------------------------ reader callback

    def on_adc_snapshot(
        self,
        snapshot,
        producer_rate_hz: float,
        publish_gap_ms: float,
        stream_info,
    ) -> None:

        previous_total = self.cached_total
        previous_session_id = int(self.adc_session_id)
        previous_effective_rate = float(self.effective_sample_rate_hz)

        self.cached_adc = snapshot
        self.cached_total = int(snapshot.total_samples)
        self.raw_sample_rate_hz = float(stream_info.raw_sample_rate_hz)
        self.effective_sample_rate_hz = max(
            0.001, float(stream_info.effective_sample_rate_hz)
        )
        self.decimation_samples = max(1, int(stream_info.decimation_samples))
        self.decimation_mode = str(stream_info.decimation_mode)
        self.adc_session_id = int(stream_info.adc_session_id)

        min_rate = max(
            0.001, self.effective_sample_rate_hz * PRODUCER_RATE_MIN_RATIO
        )
        max_rate = max(
            min_rate * 2.0,
            self.effective_sample_rate_hz * PRODUCER_RATE_MAX_RATIO,
        )
        if min_rate <= float(producer_rate_hz) <= max_rate:
            self.producer_rate_hz = float(producer_rate_hz)

        if publish_gap_ms >= 0.0:
            if self.publish_gap_ms <= 0.0:
                self.publish_gap_ms = float(publish_gap_ms)
            else:
                self.publish_gap_ms = (
                    0.85 * self.publish_gap_ms
                    + 0.15 * float(publish_gap_ms)
                )

        session_changed = self.adc_session_id != previous_session_id
        rate_changed = (
            abs(self.effective_sample_rate_hz - previous_effective_rate)
            > max(1.0e-9, 1.0e-6 * self.effective_sample_rate_hz)
        )
        counter_reset = (
            previous_total >= 0
            and self.cached_total < previous_total
        )

        if session_changed or rate_changed or counter_reset:
            self.producer_rate_hz = float(self.effective_sample_rate_hz)
            self.publish_gap_ms = 0.0
            self._reset_playhead()
            self.underruns = 0
            self._in_underrun = False
            self._update_reader_window()

    def on_reader_error(
        self,
        message: str,
    ) -> None:
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

        latest = float(total - 1)
        oldest = float(total - len(adc.ch0))

        effective_rate = self.current_sample_rate_hz()
        min_rate = max(0.001, effective_rate * PRODUCER_RATE_MIN_RATIO)
        max_rate = max(min_rate * 2.0, effective_rate * PRODUCER_RATE_MAX_RATIO)
        rate = max(min_rate, min(max_rate, float(self.producer_rate_hz)))

        configured_buffer = (
            self.current_buffer_ms()
            / 1000.0
            * rate
        )

        gap_s = max(
            0.001,
            self.publish_gap_ms
            / 1000.0,
        )

        target_reserve = max(
            configured_buffer * 0.70,
            (3.0 * gap_s + 0.100)
            * rate,
        )

        safety_reserve = max(
            configured_buffer * 0.35,
            (2.0 * gap_s + 0.050)
            * rate,
            32.0,
        )

        available = max(
            64.0,
            latest - oldest,
        )

        target_reserve = min(
            target_reserve,
            available * 0.80,
        )

        safety_reserve = min(
            safety_reserve,
            max(
                32.0,
                target_reserve * 0.75,
            ),
        )

        now_ns = time.perf_counter_ns()

        if (
            self.playhead_sample is None
            or self.playhead_wall_ns is None
        ):
            self.playhead_sample = max(
                oldest,
                latest - target_reserve,
            )
            self.playhead_wall_ns = now_ns
            self._in_underrun = False

            return float(
                self.playhead_sample
            )

        elapsed = max(
            0.0,
            (
                now_ns
                - self.playhead_wall_ns
            )
            / 1_000_000_000.0,
        )

        reserve_samples = (
            latest
            - self.playhead_sample
        )

        self.reserve_ms = (
            reserve_samples
            / max(1.0, rate)
            * 1000.0
        )

        error_fraction = (
            reserve_samples
            - target_reserve
        ) / max(
            1.0,
            target_reserve,
        )

        correction = max(
            -0.25,
            min(
                0.12,
                error_fraction * 0.40,
            ),
        )

        playback_rate = (
            rate
            * (1.0 + correction)
        )

        proposed = (
            self.playhead_sample
            + elapsed
            * playback_rate
        )

        max_playhead = (
            latest
            - safety_reserve
        )

        if proposed > max_playhead:
            proposed = max_playhead

            if not self._in_underrun:
                self.underruns += 1
                self._in_underrun = True
        else:
            recovery = max(
                16.0,
                rate * gap_s * 0.50,
            )

            if (
                reserve_samples
                > safety_reserve
                + recovery
            ):
                self._in_underrun = False

        if proposed < oldest:
            proposed = oldest

        self.playhead_sample = proposed
        self.playhead_wall_ns = now_ns

        return float(
            self.playhead_sample
        )

    # ------------------------------------------------------------------ data window

    def _visible_window(self, adc):
        count = len(adc.ch0)
        if count < 2:
            return None

        playhead = self._display_sample_index(
            adc
        )

        cache_start = (
            int(adc.total_samples)
            - count
        )

        end_index = int(
            np.floor(
                playhead
                - cache_start
            )
        ) + 1

        end_index = max(
            0,
            min(
                count,
                end_index,
            ),
        )

        if end_index < 2:
            return None

        fractional = (
            playhead
            - np.floor(playhead)
        )

        display_ns = int(
            adc.timestamp_ns[
                end_index - 1
            ]
            + fractional
            * (
                1_000_000_000
                / self.current_sample_rate_hz()
            )
        )

        start_ns = (
            display_ns
            - int(
                self.current_span_s()
                * 1_000_000_000
            )
        )

        start_index = int(
            np.searchsorted(
                adc.timestamp_ns[
                    :end_index
                ],
                start_ns,
                side="left",
            )
        )

        x = adc.ch0[
            start_index:
            end_index
        ].astype(
            np.float64,
            copy=False,
        )

        y = adc.ch1[
            start_index:
            end_index
        ].astype(
            np.float64,
            copy=False,
        )

        z = adc.ch2[
            start_index:
            end_index
        ].astype(
            np.float64,
            copy=False,
        )

        n = min(
            len(x),
            len(y),
            len(z),
        )

        if n < 2:
            return None

        x = x[-n:]
        y = y[-n:]
        z = z[-n:]

        if self.remove_mean_check.isChecked():
            x = x - np.mean(x)
            y = y - np.mean(y)
            z = z - np.mean(z)

        max_points = (
            self.current_max_points()
        )

        if n > max_points:
            step = int(
                math.ceil(
                    n / max_points
                )
            )
            x = x[::step]
            y = y[::step]
            z = z[::step]

        return x, y, z

    # ------------------------------------------------------------------ polarization

    def _calculate_polarization(
        self,
        x,
        y,
        z,
    ) -> None:

        n = min(
            len(x),
            len(y),
            len(z),
        )

        if n < 3:
            return

        matrix = np.column_stack(
            (
                x[-n:],
                y[-n:],
                z[-n:],
            )
        ).astype(
            np.float64,
            copy=False,
        )

        # PCA should always use centered signals regardless of the visual
        # centering option.
        matrix = (
            matrix
            - np.mean(
                matrix,
                axis=0,
                keepdims=True,
            )
        )

        covariance = np.cov(
            matrix,
            rowvar=False,
            bias=False,
        )

        if (
            covariance.shape
            != (3, 3)
            or not np.all(
                np.isfinite(
                    covariance
                )
            )
        ):
            return

        values, vectors = np.linalg.eigh(
            covariance
        )

        order = np.argsort(
            values
        )[::-1]

        values = np.maximum(
            values[order],
            0.0,
        )

        vectors = vectors[
            :,
            order
        ]

        principal = vectors[
            :,
            0
        ].astype(
            np.float64,
            copy=True,
        )

        # Resolve sign for a stable on-screen line: positive X preferred,
        # then positive Y if X is nearly zero.
        if (
            principal[0] < 0.0
            or (
                abs(principal[0]) < 1.0e-9
                and principal[1] < 0.0
            )
        ):
            principal *= -1.0

        l1, l2, l3 = [
            float(v)
            for v in values
        ]

        eps = 1.0e-30

        # Common 3-component polarization measures.
        rectilinearity = (
            1.0
            - (
                l2 + l3
            )
            / max(
                2.0 * l1,
                eps,
            )
        )

        planarity = (
            1.0
            - (
                2.0 * l3
            )
            / max(
                l1 + l2,
                eps,
            )
        )

        rectilinearity = max(
            0.0,
            min(
                1.0,
                rectilinearity,
            ),
        )

        planarity = max(
            0.0,
            min(
                1.0,
                planarity,
            ),
        )

        horizontal = math.degrees(
            math.atan2(
                principal[1],
                principal[0],
            )
        )

        inclination = math.degrees(
            math.atan2(
                principal[2],
                math.hypot(
                    principal[0],
                    principal[1],
                ),
            )
        )

        self.principal_vector = (
            principal
        )

        self.eigenvalues = values

        self.rectilinearity = (
            rectilinearity
        )
        self.planarity = planarity
        self.horizontal_angle_deg = (
            horizontal
        )
        self.inclination_deg = (
            inclination
        )

    # ------------------------------------------------------------------ plot helpers

    def _set_auto_ranges(
        self,
        x,
        y,
        z,
    ) -> None:

        if not self.auto_range_check.isChecked():
            return

        if self.equal_scale_check.isChecked():
            common = max(
                safe_percentile_abs(x),
                safe_percentile_abs(y),
                safe_percentile_abs(z),
                1.0,
            )

            r = common * (
                1.0
                + PLOT_MARGIN_FRACTION
            )

            for plot in self.plots.values():
                plot.setXRange(
                    -r,
                    r,
                    padding=0.0,
                )
                plot.setYRange(
                    -r,
                    r,
                    padding=0.0,
                )
        else:
            ranges = {
                "XY": (
                    safe_percentile_abs(x),
                    safe_percentile_abs(y),
                ),
                "XZ": (
                    safe_percentile_abs(x),
                    safe_percentile_abs(z),
                ),
                "YZ": (
                    safe_percentile_abs(y),
                    safe_percentile_abs(z),
                ),
            }

            for key, (
                rx,
                ry,
            ) in ranges.items():

                rx *= (
                    1.0
                    + PLOT_MARGIN_FRACTION
                )
                ry *= (
                    1.0
                    + PLOT_MARGIN_FRACTION
                )

                # Aspect lock means the ViewBox will preserve geometry.
                common = max(
                    rx,
                    ry,
                    1.0,
                )

                self.plots[
                    key
                ].setXRange(
                    -common,
                    common,
                    padding=0.0,
                )
                self.plots[
                    key
                ].setYRange(
                    -common,
                    common,
                    padding=0.0,
                )

    def _update_principal_lines(
        self,
        x,
        y,
        z,
    ) -> None:

        visible = (
            self.show_principal_check.isChecked()
        )

        for line in self.principal_lines.values():
            line.setVisible(
                visible
            )

        if not visible:
            return

        px, py, pz = [
            float(v)
            for v in self.principal_vector
        ]

        # Scale the displayed axis to the data envelope, while keeping the
        # eigenvector direction unchanged.
        common = max(
            safe_percentile_abs(x),
            safe_percentile_abs(y),
            safe_percentile_abs(z),
            1.0,
        )

        length = (
            common
            * PRINCIPAL_AXIS_FRACTION
        )

        projections = {
            "XY": (
                px,
                py,
            ),
            "XZ": (
                px,
                pz,
            ),
            "YZ": (
                py,
                pz,
            ),
        }

        for key, (
            a,
            b,
        ) in projections.items():

            norm = math.hypot(
                a,
                b,
            )

            if norm <= 1.0e-12:
                self.principal_lines[
                    key
                ].setData(
                    [],
                    [],
                )
                continue

            a /= norm
            b /= norm

            self.principal_lines[
                key
            ].setData(
                [
                    -a * length,
                    a * length,
                ],
                [
                    -b * length,
                    b * length,
                ],
            )

    def _update_metrics_labels(self) -> None:

        l1, l2, l3 = [
            float(v)
            for v in self.eigenvalues
        ]

        self.rect_value.setText(
            f"Rectilinearity: {self.rectilinearity:.3f}"
        )

        self.plan_value.setText(
            f"Planarity: {self.planarity:.3f}"
        )

        self.angle_value.setText(
            f"Horizontal angle: {self.horizontal_angle_deg:+.1f}°"
        )

        self.incl_value.setText(
            f"Inclination: {self.inclination_deg:+.1f}°"
        )

        self.eig1_value.setText(
            f"λ1: {l1:.3e}"
        )
        self.eig2_value.setText(
            f"λ2: {l2:.3e}"
        )
        self.eig3_value.setText(
            f"λ3: {l3:.3e}"
        )

        self._update_graphics_info()

    def _update_graphics_info(self) -> None:
        if not hasattr(
            self,
            "graphics_info_label",
        ):
            return

        px, py, pz = [
            float(v)
            for v in self.principal_vector
        ]

        text = (
            "<div style='color:#FFFFFF;'>"
            "<span style='font-size:12pt; font-weight:700;'>POLARIZATION</span><br><br>"
            f"<span style='color:#A9BECA;'>Principal vector</span><br>"
            f"X = {px:+.4f}<br>"
            f"Y = {py:+.4f}<br>"
            f"Z = {pz:+.4f}<br><br>"
            f"Rectilinearity = {self.rectilinearity:.3f}<br>"
            f"Planarity = {self.planarity:.3f}<br>"
            f"Horizontal = {self.horizontal_angle_deg:+.1f}°<br>"
            f"Inclination = {self.inclination_deg:+.1f}°"
            "</div>"
        )

        try:
            self.graphics_info_label.setText(
                text
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ render

    def render_frame(self) -> None:
        if self.paused:
            return

        adc = self.cached_adc
        if adc is None or len(adc.ch0) < 2:
            return

        window = self._visible_window(
            adc
        )

        if window is None:
            return

        x, y, z = window

        self.curves["XY"].setData(
            x,
            y,
            connect="all",
        )
        self.curves["XZ"].setData(
            x,
            z,
            connect="all",
        )
        self.curves["YZ"].setData(
            y,
            z,
            connect="all",
        )

        if len(x):
            self.current_points["XY"].setData(
                [x[-1]],
                [y[-1]],
            )
            self.current_points["XZ"].setData(
                [x[-1]],
                [z[-1]],
            )
            self.current_points["YZ"].setData(
                [y[-1]],
                [z[-1]],
            )

        now = time.perf_counter()

        if (
            now
            - self._last_polarization_update
            >= POLARIZATION_UPDATE_INTERVAL_S
        ):
            self._calculate_polarization(
                x,
                y,
                z,
            )

            self._update_principal_lines(
                x,
                y,
                z,
            )

            self._update_metrics_labels()

            self._last_polarization_update = (
                now
            )

        if (
            now
            - self._last_auto_range
            >= AUTO_RANGE_INTERVAL_S
        ):
            self._set_auto_ranges(
                x,
                y,
                z,
            )

            self._last_auto_range = now

        self._update_render_metrics()

    def _update_render_metrics(self) -> None:
        now_ns = time.perf_counter_ns()

        if self._last_render_ns is not None:
            interval_ms = (
                now_ns
                - self._last_render_ns
            ) / 1_000_000.0

            target_ms = (
                1000.0
                / self.current_fps()
            )

            jitter = abs(
                interval_ms
                - target_ms
            )

            self.render_jitter_ms = (
                0.90
                * self.render_jitter_ms
                + 0.10
                * jitter
            )

        self._last_render_ns = now_ns
        self._fps_count += 1

        now = time.perf_counter()
        elapsed = (
            now
            - self._fps_start
        )

        if elapsed >= 0.75:
            self.render_fps = (
                self._fps_count
                / elapsed
            )
            self._fps_count = 0
            self._fps_start = now

    # ------------------------------------------------------------------ pause / status

    def toggle_pause(
        self,
        checked: bool,
    ) -> None:
        self.paused = bool(
            checked
        )

        if self.paused:
            self.pause_button.setText(
                "Continue"
            )
            self.mode_label.setText(
                "PAUSED"
            )
            self.mode_label.setObjectName(
                "modePaused"
            )
        else:
            self.pause_button.setText(
                "Pause"
            )
            self.mode_label.setText(
                "LIVE"
            )
            self.mode_label.setObjectName(
                "modeLive"
            )
            self.playhead_wall_ns = (
                time.perf_counter_ns()
            )

        self.mode_label.style().unpolish(
            self.mode_label
        )
        self.mode_label.style().polish(
            self.mode_label
        )

    def toggle_pause_shortcut(self) -> None:
        checked = not (
            self.pause_button.isChecked()
        )

        self.pause_button.setChecked(
            checked
        )

        self.toggle_pause(
            checked
        )

    def refresh_status(self) -> None:
        try:
            telemetry = (
                self.shared.read_telemetry()
            )
            bulk = (
                self.shared.read_bulk_status()
            )
            total = self.shared.adc_total_samples()
            stream_info = self.shared.read_adc_stream_info()
            self.raw_sample_rate_hz = float(stream_info.raw_sample_rate_hz)
            self.effective_sample_rate_hz = max(
                0.001, float(stream_info.effective_sample_rate_hz)
            )
            self.decimation_samples = max(1, int(stream_info.decimation_samples))
            self.decimation_mode = str(stream_info.decimation_mode)
            self.adc_session_id = int(stream_info.adc_session_id)

            self.connection_label.setText(
                "Shared RAM: DATA CONNECTED"
                if telemetry.data_connected
                else "Shared RAM: DATA NOT CONNECTED"
            )

            expected_window_samples = int(
                round(self.current_span_s() * self.current_sample_rate_hz())
            )
            self.stream_label.setText(
                f"ADC {total:,} | "
                f"Fs {self.effective_sample_rate_hz:6.1f} Hz "
                f"(raw {self.raw_sample_rate_hz:6.1f}/N{self.decimation_samples}) | "
                f"span {self.current_span_s():.2f}s ≈{expected_window_samples} samples | "
                f"producer {self.producer_rate_hz:6.1f} Hz | "
                f"reserve {self.reserve_ms:3.0f} ms | "
                f"underrun {self.underruns} | "
                f"drop {bulk.dropped_frames} | "
                f"sync {bulk.channel_id_mismatches} | "
                f"session {self.adc_session_id}"
            )

            renderer = (
                "OpenGL single-view"
                if self.opengl_active
                else "CPU/Raster"
            )

            self.render_label.setText(
                f"Render {self.render_fps:4.1f} FPS | "
                f"jitter {self.render_jitter_ms:3.1f} ms | "
                f"{renderer}"
            )

            tooltip = (
                f"Executable: {sys.executable}\n"
                "Hodogram plotting uses one PyQtGraph/OpenGL viewport.\n"
                "Polarization PCA is a 3x3 NumPy eigendecomposition."
            )

            if self.opengl_error:
                tooltip = (
                    self.opengl_error
                    + "\n\n"
                    + tooltip
                )

            self.render_label.setToolTip(
                tooltip
            )

        except Exception as exc:
            self.connection_label.setText(
                f"Shared RAM status error: {exc}"
            )

    # ------------------------------------------------------------------ close

    def closeEvent(
        self,
        event: QCloseEvent,
    ) -> None:

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
            fmt.setRenderableType(
                QSurfaceFormat.RenderableType.OpenGL
            )
            fmt.setSwapBehavior(
                QSurfaceFormat.SwapBehavior.DoubleBuffer
            )
            fmt.setSamples(0)
            fmt.setSwapInterval(0)
            QSurfaceFormat.setDefaultFormat(
                fmt
            )
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
        window = GeophoneHodogramWindow()
    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Geophone Hodogram:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
