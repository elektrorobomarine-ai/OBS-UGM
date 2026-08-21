"""
geophone_realtime.py
====================

GRC-UGM-PERTAMINA OBS
Real-Time Geophone Waveform

Version: 10
Shared data: shared_data_v5.py

Performance architecture
------------------------
This version is optimized for frame pacing rather than merely increasing raw
GPU utilization.

1. One PyQtGraph GraphicsLayoutWidget is used for all CH0/CH1/CH2 plots.
   Therefore the three plots share ONE QGraphicsView / ONE OpenGL viewport
   instead of three separate QOpenGLWidget contexts.

2. Shared RAM is read by a dedicated QThread only when the OBS ADC total-sample
   counter changes. The GUI thread does NOT copy shared-memory NumPy arrays on
   every 60-Hz paint tick.

3. The GUI uses a cached ADC snapshot and a jitter-buffer presentation
   playhead. Producer-rate measurement is performed inside the background
   reader thread (not the GUI event loop), and the reserve floor adapts to the
   measured shared-data publication gap.

4. X ranges are updated only when settings change, not on every repaint.

5. Auto-Y calculation is throttled. Numeric labels are also updated at a lower
   rate than the waveform.

6. Windows uses a 1-ms multimedia timer request and ABOVE_NORMAL process
   priority to reduce GUI timer scheduling jitter. Real-time priority is NOT
   used.

7. OpenGL viewport is explicit. VSync is requested OFF to reduce frame-pacing
   stalls; Windows DWM may still synchronize final presentation.

GPU selection
-------------
On hybrid Intel/NVIDIA systems, Python/Qt cannot reliably choose the physical
adapter at runtime. Windows Graphics Settings should assign the exact
pythonw.exe/python.exe used by this process to the NVIDIA "High performance"
GPU. This version reduces rendering overhead first, so moving to RTX becomes
additional headroom rather than a workaround for inefficient redraws.

Dependencies
------------
    pip install PySide6 numpy pyqtgraph
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows process identity / timing
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.REALTIME"

_WINDOWS_TIMER_ACTIVE = False


def configure_windows_runtime() -> None:
    """
    Improve GUI frame scheduling without using dangerous real-time priority.
    """

    global _WINDOWS_TIMER_ACTIVE

    if os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

        kernel32 = ctypes.windll.kernel32

        # ABOVE_NORMAL_PRIORITY_CLASS
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            ABOVE_NORMAL_PRIORITY_CLASS,
        )

        # Request 1-ms timer granularity for this application's active period.
        try:
            winmm = ctypes.windll.winmm

            if winmm.timeBeginPeriod(1) == 0:
                _WINDOWS_TIMER_ACTIVE = True

        except Exception:
            pass

        # If launched with python.exe, detach console after startup.
        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()

    except Exception:
        pass


def release_windows_runtime() -> None:
    global _WINDOWS_TIMER_ACTIVE

    if (
        os.name == "nt"
        and _WINDOWS_TIMER_ACTIVE
    ):
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
    QScrollArea,
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
# Shared RAM
# =============================================================================

from shared_data_v5 import (
    RAW_ADC_SAMPLE_RATE_HZ,
    OBSSharedData,
)


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Geophone Real-Time"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

DEFAULT_RENDER_FPS = 60
STATUS_INTERVAL_MS = 500
LABEL_UPDATE_INTERVAL_S = 0.10
AUTO_Y_UPDATE_INTERVAL_S = 0.10

# 128 samples / 1000 Hz = 128 ms per OBS bulk payload.
DEFAULT_SMOOTH_BUFFER_MS = 1536

SMOOTH_BUFFER_CHOICES_MS = (
    256,
    512,
    768,
    1024,
    1536,
    2048,
    3072,
)

FPS_CHOICES = (
    30,
    45,
    60,
    75,
    90,
    120,
)

DEFAULT_TIME_SPAN_S = 5.0
MIN_TIME_SPAN_S = 0.10
MAX_TIME_SPAN_S = 120.0

DEFAULT_Y_MIN = -8_388_608.0
DEFAULT_Y_MAX = 8_388_607.0

# For long windows, line resolution beyond a few thousand points does not add
# useful visual detail on a normal desktop monitor. Lower vertex count improves
# frame pacing significantly.
MAX_RENDER_POINTS = 6000

# Background shared-RAM poll. It only copies when total_samples changed.
ADC_READER_POLL_MS = 5

# Measured producer throughput is used for presentation pacing only.
# Physical time/frequency calibration comes from shared_data_v5 effective Fs.
PRODUCER_RATE_WINDOW_S = 5.0
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0

CHANNELS = (
    ("CH0", "Geophone X", "ch0"),
    ("CH1", "Geophone Y", "ch1"),
    ("CH2", "Geophone Z", "ch2"),
)

SCALE_OPTIONS = (
    ("0.1×", 0.1),
    ("0.2×", 0.2),
    ("0.5×", 0.5),
    ("1×", 1.0),
    ("2×", 2.0),
    ("5×", 5.0),
    ("10×", 10.0),
    ("20×", 20.0),
)


# =============================================================================
# Helpers
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


def format_adc_value(
    value: float,
) -> str:

    try:
        return f"{int(value):,}"

    except Exception:
        return "--"


# =============================================================================
# Dedicated shared-RAM reader
# =============================================================================

class ADCSharedReaderThread(QThread):
    """
    Reads/copies ADC shared RAM only when the acquisition counter advances.

    This removes shared-memory NumPy copying from the 60-FPS GUI render loop.
    """

    # snapshot, measured producer rate [ADC frames/s],
    # latest publish gap [ms], ADCStreamInfoSnapshot
    snapshot_ready = Signal(object, float, float, object)
    read_error = Signal(str)

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self._stop_event = threading.Event()

        self._count_lock = threading.Lock()
        self._desired_count = int(
            (
                DEFAULT_TIME_SPAN_S
                + 2.0
            )
            * RAW_ADC_SAMPLE_RATE_HZ
        )

        # Rate estimation is intentionally performed in THIS worker thread.
        self._rate_history: list[
            tuple[float, int]
        ] = []

        self._producer_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._effective_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._adc_session_id = -1

        self._last_publish_time: Optional[
            float
        ] = None

    def set_desired_count(
        self,
        count: int,
    ) -> None:

        with self._count_lock:
            self._desired_count = max(
                16,
                int(count),
            )

    def _get_desired_count(
        self,
    ) -> int:

        with self._count_lock:
            return int(
                self._desired_count
            )

    def stop(
        self,
    ) -> None:

        self._stop_event.set()

    def run(
        self,
    ) -> None:

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

                total = (
                    shared.adc_total_samples()
                )

                if total != last_total:
                    now = time.perf_counter()

                    count = (
                        self._get_desired_count()
                    )

                    snapshot = (
                        shared.read_adc_latest_numpy(
                            count
                        )
                    )

                    current_total = int(
                        snapshot.total_samples
                    )

                    # Latest interval between shared-RAM publications. One
                    # publication may contain one or several 128-sample OBS
                    # bulk blocks; the rolling rate below uses sample COUNTS,
                    # so TCP coalescing does not bias the long-term result.
                    if self._last_publish_time is None:
                        publish_gap_ms = 0.0
                    else:
                        publish_gap_ms = (
                            now
                            - self._last_publish_time
                        ) * 1000.0

                    self._last_publish_time = now

                    self._rate_history.append(
                        (
                            now,
                            current_total,
                        )
                    )

                    cutoff = (
                        now
                        - PRODUCER_RATE_WINDOW_S
                    )

                    while (
                        len(self._rate_history) > 2
                        and self._rate_history[0][0] < cutoff
                    ):
                        self._rate_history.pop(0)

                    if len(
                        self._rate_history
                    ) >= 2:
                        t0, n0 = (
                            self._rate_history[0]
                        )
                        t1, n1 = (
                            self._rate_history[-1]
                        )

                        dt = (
                            t1 - t0
                        )

                        dn = (
                            n1 - n0
                        )

                        if (
                            dt >= 1.0
                            and dn > 0
                        ):
                            measured = (
                                dn / dt
                            )

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

                            if (
                                min_rate
                                <= measured
                                <= max_rate
                            ):
                                self._producer_rate_hz = (
                                    0.80
                                    * self._producer_rate_hz
                                    + 0.20
                                    * measured
                                )

                    last_total = current_total

                    self.snapshot_ready.emit(
                        snapshot,
                        float(
                            self._producer_rate_hz
                        ),
                        float(
                            publish_gap_ms
                        ),
                        stream_info,
                    )

                self.msleep(
                    ADC_READER_POLL_MS
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
# Per-channel controls
# =============================================================================

@dataclass
class ChannelControl:
    index: int
    channel_name: str
    axis_name: str
    data_attribute: str

    group: QGroupBox

    y_min_spin: QDoubleSpinBox
    y_max_spin: QDoubleSpinBox

    time_span_spin: QDoubleSpinBox
    scale_combo: QComboBox

    auto_y_checkbox: QCheckBox

    current_value_label: QLabel
    samples_label: QLabel

    apply_button: QPushButton
    auto_button: QPushButton
    reset_button: QPushButton

    scale_value: float = 1.0


# =============================================================================
# Main window
# =============================================================================

class GeophoneRealtimeWindow(QMainWindow):

    def __init__(
        self,
    ):
        super().__init__()

        if (
            np is None
            or pg is None
        ):
            raise RuntimeError(
                "Geophone Real-Time requires NumPy and PyQtGraph. "
                "Install with: pip install numpy pyqtgraph"
            )

        self.shared: Optional[
            OBSSharedData
        ] = None

        try:
            self.shared = OBSSharedData()

        except Exception as exc:
            raise RuntimeError(
                f"Cannot attach shared_data_v5 RAM: {exc}"
            ) from exc

        self.paused = False

        self.cached_adc = None
        self.cached_total_samples = -1

        # Presentation playhead is maintained in ABSOLUTE ADC SAMPLE INDEX,
        # not host receive timestamp. This makes the scrolling speed follow the
        # measured producer throughput and prevents gradual jitter-buffer drain.
        self.playhead_sample: Optional[float] = None

        self.playhead_wall_ns: Optional[
            int
        ] = None

        try:
            stream_info = self.shared.read_adc_stream_info()
            self.effective_sample_rate_hz = max(
                0.001,
                float(stream_info.effective_sample_rate_hz),
            )
            self.raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self.decimation_samples = int(
                stream_info.decimation_samples
            )
            self.adc_session_id = int(
                stream_info.adc_session_id
            )
        except Exception:
            self.effective_sample_rate_hz = float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            self.raw_sample_rate_hz = float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            self.decimation_samples = 1
            self.adc_session_id = -1

        # Measured producer rate is only for jitter-buffer pacing.
        self.producer_rate_hz = float(
            self.effective_sample_rate_hz
        )

        self.source_publish_gap_ms = 0.0

        # Count underrun EPISODES, not every 60-FPS render tick.
        self.buffer_underruns = 0
        self._in_underrun = False

        self.render_fps = 0.0
        self.render_jitter_ms = 0.0

        self._fps_frame_count = 0
        self._fps_window_start = (
            time.perf_counter()
        )

        self._last_render_tick_ns: Optional[
            int
        ] = None

        self._last_label_update = 0.0
        self._last_auto_y_update = 0.0

        self._reserve_ms = 0.0

        self.channel_controls: list[
            ChannelControl
        ] = []

        self.plots: list = []
        self.curves: list = []

        self.opengl_active = False
        self.opengl_error = ""

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

        self.resize(
            1450,
            860,
        )

        self.setMinimumSize(
            1050,
            650,
        )

        self._configure_pyqtgraph()
        self._build_ui()
        self._apply_style()
        self._install_shortcuts()

        # Background shared-memory reader.
        self.reader_thread = (
            ADCSharedReaderThread(
                self
            )
        )

        self.reader_thread.snapshot_ready.connect(
            self.on_adc_snapshot
        )

        self.reader_thread.read_error.connect(
            self.on_adc_reader_error
        )

        self.reader_thread.start()

        self._update_reader_window()

        # Render timer is independent of acquisition update cadence.
        self.plot_timer = QTimer(
            self
        )

        try:
            self.plot_timer.setTimerType(
                Qt.TimerType.PreciseTimer
            )
        except Exception:
            pass

        self.plot_timer.timeout.connect(
            self.refresh_plots
        )

        self.set_target_fps(
            DEFAULT_RENDER_FPS
        )

        self.status_timer = QTimer(
            self
        )

        self.status_timer.timeout.connect(
            self.refresh_status
        )

        self.status_timer.start(
            STATUS_INTERVAL_MS
        )

        self.refresh_status()

    # -------------------------------------------------------------------------
    # PyQtGraph / OpenGL
    # -------------------------------------------------------------------------

    @staticmethod
    def _configure_pyqtgraph(
    ) -> None:

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

    def _install_single_opengl_viewport(
        self,
        graphics_widget,
    ) -> None:
        """
        One GPU viewport for all three plots.
        """

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

            # Disable multisampling and request no vsync.
            fmt.setSamples(
                0
            )

            fmt.setSwapInterval(
                0
            )

            viewport.setFormat(
                fmt
            )

            graphics_widget.setViewport(
                viewport
            )

            self.opengl_active = isinstance(
                graphics_widget.viewport(),
                QOpenGLWidget,
            )

        except Exception as exc:
            self.opengl_active = False
            self.opengl_error = str(
                exc
            )

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _build_ui(
        self,
    ) -> None:

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
            14,
            12,
            14,
            12,
        )

        root.setSpacing(
            8
        )

        # Header.
        header_row = QHBoxLayout()

        title_box = QVBoxLayout()
        title_box.setSpacing(
            1
        )

        title = QLabel(
            "GEOPHONE REAL-TIME"
        )

        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            (
                "ADC CH0 / CH1 / CH2  •  "
                "Geophone X / Y / Z  •  "
                "Shared RAM • dynamic effective ADC rate"
            )
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

        header_row.addLayout(
            title_box,
            1,
        )

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

        header_row.addWidget(
            self.pause_button
        )

        root.addLayout(
            header_row
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
            10,
            6,
            10,
            6,
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

        self.mode_label = QLabel(
            "LIVE"
        )

        self.mode_label.setObjectName(
            "modeLive"
        )

        status_layout.addWidget(
            self.connection_label
        )

        status_layout.addStretch(
            1
        )

        status_layout.addWidget(
            self.stream_label
        )

        status_layout.addSpacing(
            14
        )

        status_layout.addWidget(
            self.render_label
        )

        status_layout.addSpacing(
            14
        )

        status_layout.addWidget(
            self.mode_label
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

        plot_panel = (
            self._build_plot_panel()
        )

        settings_panel = (
            self._build_settings_panel()
        )

        splitter.addWidget(
            plot_panel
        )

        splitter.addWidget(
            settings_panel
        )

        splitter.setStretchFactor(
            0,
            3,
        )

        splitter.setStretchFactor(
            1,
            1,
        )

        splitter.setSizes(
            [
                1080,
                360,
            ]
        )

        root.addWidget(
            splitter,
            1,
        )

    def _build_plot_panel(
        self,
    ) -> QWidget:

        panel = QFrame()
        panel.setObjectName(
            "plotPanel"
        )

        layout = QVBoxLayout(
            panel
        )

        layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        # IMPORTANT: one GraphicsView for all three plots.
        self.graphics = (
            pg.GraphicsLayoutWidget()
        )

        self._install_single_opengl_viewport(
            self.graphics
        )

        layout.addWidget(
            self.graphics,
            1,
        )

        channel_pens = (
            pg.mkPen(
                "#5CC8FF",
                width=1,
            ),
            pg.mkPen(
                "#8EE28E",
                width=1,
            ),
            pg.mkPen(
                "#FFD166",
                width=1,
            ),
        )

        for index, (
            channel_name,
            axis_name,
            _attribute,
        ) in enumerate(
            CHANNELS
        ):
            plot = self.graphics.addPlot(
                row=index,
                col=0,
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

            plot.setClipToView(
                True
            )

            plot.setDownsampling(
                ds=1,
                auto=False,
                mode="subsample",
            )

            plot.setLabel(
                "left",
                (
                    f"{channel_name} / "
                    f"{axis_name}"
                ),
                units="count",
            )

            plot.setLabel(
                "bottom",
                "Time",
                units="s",
            )

            plot.setTitle(
                (
                    f"{channel_name} — "
                    f"{axis_name}"
                ),
                color="#FFFFFF",
                size="11pt",
            )

            plot.setYRange(
                DEFAULT_Y_MIN,
                DEFAULT_Y_MAX,
                padding=0.0,
            )

            plot.setXRange(
                -DEFAULT_TIME_SPAN_S,
                0.0,
                padding=0.0,
            )

            curve = plot.plot(
                [],
                [],
                pen=channel_pens[
                    index
                ],
            )

            self.plots.append(
                plot
            )

            self.curves.append(
                curve
            )

        return panel

    def _build_settings_panel(
        self,
    ) -> QWidget:

        outer = QFrame()
        outer.setObjectName(
            "settingsPanel"
        )

        outer_layout = QVBoxLayout(
            outer
        )

        outer_layout.setContentsMargins(
            8,
            0,
            0,
            0,
        )

        outer_layout.setSpacing(
            8
        )

        heading = QLabel(
            "CHANNEL SETTINGS"
        )

        heading.setObjectName(
            "settingsTitle"
        )

        outer_layout.addWidget(
            heading
        )

        # Display performance controls.
        perf_group = QGroupBox(
            "Display Performance"
        )

        perf_group.setObjectName(
            "channelGroup"
        )

        perf_layout = QGridLayout(
            perf_group
        )

        perf_layout.setContentsMargins(
            10,
            12,
            10,
            10,
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
            self.on_fps_setting_changed
        )

        self.buffer_combo = QComboBox()

        for value_ms in (
            SMOOTH_BUFFER_CHOICES_MS
        ):
            self.buffer_combo.addItem(
                f"{value_ms} ms",
                value_ms,
            )

        self.buffer_combo.setCurrentText(
            f"{DEFAULT_SMOOTH_BUFFER_MS} ms"
        )

        self.buffer_combo.currentIndexChanged.connect(
            self.on_buffer_setting_changed
        )

        perf_layout.addWidget(
            QLabel("Target FPS"),
            0,
            0,
        )

        perf_layout.addWidget(
            self.fps_combo,
            0,
            1,
        )

        perf_layout.addWidget(
            QLabel("Smooth Buffer"),
            1,
            0,
        )

        perf_layout.addWidget(
            self.buffer_combo,
            1,
            1,
        )

        perf_hint = QLabel(
            (
                "Higher buffer = smoother TCP bulk playback, "
                "with additional display latency."
            )
        )

        perf_hint.setObjectName(
            "sampleInfo"
        )

        perf_hint.setWordWrap(
            True
        )

        perf_layout.addWidget(
            perf_hint,
            2,
            0,
            1,
            2,
        )

        outer_layout.addWidget(
            perf_group
        )

        scroll = QScrollArea()

        scroll.setWidgetResizable(
            True
        )

        scroll.setFrameShape(
            QFrame.NoFrame
        )

        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )

        content = QWidget()
        content.setObjectName(
            "settingsScroll"
        )

        content_layout = QVBoxLayout(
            content
        )

        content_layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        content_layout.setSpacing(
            8
        )

        for index, (
            channel_name,
            axis_name,
            data_attribute,
        ) in enumerate(
            CHANNELS
        ):
            control = (
                self._create_channel_control(
                    index=index,
                    channel_name=channel_name,
                    axis_name=axis_name,
                    data_attribute=data_attribute,
                )
            )

            self.channel_controls.append(
                control
            )

            content_layout.addWidget(
                control.group
            )

        content_layout.addStretch(
            1
        )

        scroll.setWidget(
            content
        )

        outer_layout.addWidget(
            scroll,
            1,
        )

        reset_all = QPushButton(
            "Reset All Views"
        )

        reset_all.setObjectName(
            "secondaryButton"
        )

        reset_all.clicked.connect(
            self.reset_all_views
        )

        outer_layout.addWidget(
            reset_all
        )

        return outer

    def _create_channel_control(
        self,
        *,
        index: int,
        channel_name: str,
        axis_name: str,
        data_attribute: str,
    ) -> ChannelControl:

        group = QGroupBox(
            (
                f"{channel_name}  •  "
                f"{axis_name}"
            )
        )

        group.setObjectName(
            "channelGroup"
        )

        layout = QGridLayout(
            group
        )

        layout.setContentsMargins(
            10,
            12,
            10,
            10,
        )

        layout.setHorizontalSpacing(
            7
        )

        layout.setVerticalSpacing(
            6
        )

        current_value = QLabel(
            "--"
        )

        current_value.setObjectName(
            "currentValue"
        )

        current_value.setAlignment(
            Qt.AlignRight
            | Qt.AlignVCenter
        )

        layout.addWidget(
            QLabel("Current"),
            0,
            0,
        )

        layout.addWidget(
            current_value,
            0,
            1,
            1,
            2,
        )

        y_min_spin = (
            self._new_amplitude_spin(
                DEFAULT_Y_MIN
            )
        )

        y_max_spin = (
            self._new_amplitude_spin(
                DEFAULT_Y_MAX
            )
        )

        layout.addWidget(
            QLabel("Amp Min"),
            1,
            0,
        )

        layout.addWidget(
            y_min_spin,
            1,
            1,
            1,
            2,
        )

        layout.addWidget(
            QLabel("Amp Max"),
            2,
            0,
        )

        layout.addWidget(
            y_max_spin,
            2,
            1,
            1,
            2,
        )

        time_span_spin = (
            QDoubleSpinBox()
        )

        time_span_spin.setRange(
            MIN_TIME_SPAN_S,
            MAX_TIME_SPAN_S,
        )

        time_span_spin.setDecimals(
            2
        )

        time_span_spin.setSingleStep(
            0.5
        )

        time_span_spin.setSuffix(
            " s"
        )

        time_span_spin.setValue(
            DEFAULT_TIME_SPAN_S
        )

        layout.addWidget(
            QLabel("Time Span"),
            3,
            0,
        )

        layout.addWidget(
            time_span_spin,
            3,
            1,
            1,
            2,
        )

        scale_combo = QComboBox()

        for label, value in SCALE_OPTIONS:
            scale_combo.addItem(
                label,
                value,
            )

        scale_combo.setCurrentText(
            "1×"
        )

        layout.addWidget(
            QLabel("Scale"),
            4,
            0,
        )

        layout.addWidget(
            scale_combo,
            4,
            1,
            1,
            2,
        )

        auto_y = QCheckBox(
            "Auto Y Range"
        )

        layout.addWidget(
            auto_y,
            5,
            0,
            1,
            3,
        )

        apply_button = QPushButton(
            "Apply"
        )

        auto_button = QPushButton(
            "Auto Y"
        )

        reset_button = QPushButton(
            "Reset"
        )

        apply_button.setObjectName(
            "smallPrimaryButton"
        )

        auto_button.setObjectName(
            "smallButton"
        )

        reset_button.setObjectName(
            "smallButton"
        )

        layout.addWidget(
            apply_button,
            6,
            0,
        )

        layout.addWidget(
            auto_button,
            6,
            1,
        )

        layout.addWidget(
            reset_button,
            6,
            2,
        )

        samples_label = QLabel(
            "Samples: --"
        )

        samples_label.setObjectName(
            "sampleInfo"
        )

        layout.addWidget(
            samples_label,
            7,
            0,
            1,
            3,
        )

        control = ChannelControl(
            index=index,
            channel_name=channel_name,
            axis_name=axis_name,
            data_attribute=data_attribute,
            group=group,
            y_min_spin=y_min_spin,
            y_max_spin=y_max_spin,
            time_span_spin=time_span_spin,
            scale_combo=scale_combo,
            auto_y_checkbox=auto_y,
            current_value_label=current_value,
            samples_label=samples_label,
            apply_button=apply_button,
            auto_button=auto_button,
            reset_button=reset_button,
            scale_value=1.0,
        )

        apply_button.clicked.connect(
            lambda checked=False, c=control:
            self.apply_channel_settings(
                c
            )
        )

        auto_button.clicked.connect(
            lambda checked=False, c=control:
            self.auto_range_channel(
                c
            )
        )

        reset_button.clicked.connect(
            lambda checked=False, c=control:
            self.reset_channel_view(
                c
            )
        )

        scale_combo.currentIndexChanged.connect(
            lambda _index, c=control:
            self.on_scale_changed(
                c
            )
        )

        time_span_spin.valueChanged.connect(
            lambda _value, c=control:
            self.on_time_span_changed(
                c
            )
        )

        auto_y.stateChanged.connect(
            lambda _state, c=control:
            self.on_auto_y_changed(
                c
            )
        )

        return control

    @staticmethod
    def _new_amplitude_spin(
        value: float,
    ) -> QDoubleSpinBox:

        spin = QDoubleSpinBox()

        spin.setRange(
            -100_000_000.0,
            100_000_000.0,
        )

        spin.setDecimals(
            0
        )

        spin.setSingleStep(
            1000.0
        )

        spin.setValue(
            value
        )

        spin.setGroupSeparatorShown(
            True
        )

        return spin

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
            QWidget#settingsScroll {
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

            QLabel#currentValue {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 13px;
                font-weight: 800;
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

            QComboBox:editable {
                color: #FFFFFF;
            }

            QComboBox QLineEdit {
                background-color: #071620;
                color: #FFFFFF;
                border: none;
                selection-background-color: #2B739A;
                selection-color: #FFFFFF;
            }

            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid #24485D;
                background-color: #0E2533;
                border-top-right-radius: 5px;
                border-bottom-right-radius: 5px;
            }

            QComboBox::down-arrow {
                width: 0px;
                height: 0px;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 7px solid #DDEAF2;
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

            QComboBox QAbstractItemView::item:hover {
                color: #FFFFFF;
                background-color: #18384C;
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

            QPushButton#pauseButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
                min-height: 34px;
            }

            QPushButton#pauseButton:checked {
                background-color: #705C16;
                border: 1px solid #B49326;
            }

            QPushButton#smallPrimaryButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QPushButton#smallButton,
            QPushButton#secondaryButton {
                background-color: #132A39;
                color: #FFFFFF;
                border: 1px solid #27526A;
            }

            QPushButton#smallButton:hover,
            QPushButton#secondaryButton:hover {
                background-color: #18384C;
                border: 1px solid #3C7898;
            }

            QScrollArea {
                background: transparent;
                border: none;
            }

            QSplitter::handle {
                background-color: #17374A;
                width: 2px;
            }
            """
        )

    # -------------------------------------------------------------------------
    # Shortcuts
    # -------------------------------------------------------------------------

    def _install_shortcuts(
        self,
    ) -> None:

        pause_action = QAction(
            self
        )

        pause_action.setShortcut(
            QKeySequence(
                Qt.Key_Space
            )
        )

        pause_action.triggered.connect(
            self.toggle_pause_shortcut
        )

        self.addAction(
            pause_action
        )

    # -------------------------------------------------------------------------
    # Performance settings
    # -------------------------------------------------------------------------

    def set_target_fps(
        self,
        fps: int,
    ) -> None:

        fps = max(
            10,
            int(fps),
        )

        interval_ms = max(
            1,
            round(
                1000.0
                / fps
            ),
        )

        self.plot_timer.start(
            interval_ms
        )

    def on_fps_setting_changed(
        self,
    ) -> None:

        value = (
            self.fps_combo.currentData()
        )

        if value is None:
            return

        self.set_target_fps(
            int(value)
        )

    def current_buffer_ms(
        self,
    ) -> int:

        value = (
            self.buffer_combo.currentData()
        )

        if value is None:
            return (
                DEFAULT_SMOOTH_BUFFER_MS
            )

        return int(
            value
        )

    def on_buffer_setting_changed(
        self,
    ) -> None:

        self._reset_playhead()
        self._update_reader_window()

    def current_sample_rate_hz(
        self,
    ) -> float:

        return max(
            0.001,
            float(
                self.effective_sample_rate_hz
            ),
        )

    def _update_reader_window(
        self,
    ) -> None:

        if not hasattr(
            self,
            "reader_thread",
        ):
            return

        max_span_s = max(
            (
                float(
                    control.time_span_spin.value()
                )
                for control
                in self.channel_controls
            ),
            default=DEFAULT_TIME_SPAN_S,
        )

        buffer_s = (
            self.current_buffer_ms()
            / 1000.0
        )

        # Additional 0.8 s handles OBS/TCP batching beyond the nominal buffer.
        desired_seconds = (
            max_span_s
            + buffer_s
            + 0.8
        )

        desired_count = int(
            desired_seconds
            * self.current_sample_rate_hz()
        ) + 16

        self.reader_thread.set_desired_count(
            desired_count
        )

    # -------------------------------------------------------------------------
    # Snapshot receive
    # -------------------------------------------------------------------------

    def on_adc_snapshot(
        self,
        snapshot,
        producer_rate_hz: float,
        publish_gap_ms: float,
        stream_info,
    ) -> None:

        previous_total = (
            self.cached_total_samples
        )
        previous_session_id = int(
            self.adc_session_id
        )
        previous_effective_rate = float(
            self.effective_sample_rate_hz
        )

        self.cached_adc = snapshot
        self.cached_total_samples = int(
            snapshot.total_samples
        )

        self.effective_sample_rate_hz = max(
            0.001,
            float(stream_info.effective_sample_rate_hz),
        )
        self.raw_sample_rate_hz = float(
            stream_info.raw_sample_rate_hz
        )
        self.decimation_samples = int(
            stream_info.decimation_samples
        )
        self.adc_session_id = int(
            stream_info.adc_session_id
        )

        min_rate = max(
            0.001,
            self.effective_sample_rate_hz
            * PRODUCER_RATE_MIN_RATIO,
        )
        max_rate = max(
            min_rate * 2.0,
            self.effective_sample_rate_hz
            * PRODUCER_RATE_MAX_RATIO,
        )

        if (
            min_rate
            <= float(producer_rate_hz)
            <= max_rate
        ):
            self.producer_rate_hz = float(
                producer_rate_hz
            )

        if publish_gap_ms >= 0.0:
            # Smooth the displayed gap metric enough to remain readable while
            # still exposing prolonged TCP/producer bursts.
            if self.source_publish_gap_ms <= 0.0:
                self.source_publish_gap_ms = float(
                    publish_gap_ms
                )
            else:
                self.source_publish_gap_ms = (
                    0.85
                    * self.source_publish_gap_ms
                    + 0.15
                    * float(
                        publish_gap_ms
                    )
                )

        # Acquisition session reset/reconnect OR effective-rate change.
        session_changed = (
            self.adc_session_id
            != previous_session_id
        )
        rate_changed = (
            abs(
                self.effective_sample_rate_hz
                - previous_effective_rate
            )
            > max(
                1.0e-9,
                1.0e-6
                * self.effective_sample_rate_hz,
            )
        )
        counter_reset = (
            previous_total >= 0
            and self.cached_total_samples
            < previous_total
        )

        if (
            session_changed
            or rate_changed
            or counter_reset
        ):
            self.producer_rate_hz = float(
                self.effective_sample_rate_hz
            )
            self.source_publish_gap_ms = 0.0
            self.buffer_underruns = 0
            self._in_underrun = False
            self._reset_playhead()
            self._update_reader_window()

    def on_adc_reader_error(
        self,
        message: str,
    ) -> None:

        self.connection_label.setText(
            (
                "ADC reader error: "
                f"{message}"
            )
        )

    # -------------------------------------------------------------------------
    # Pause
    # -------------------------------------------------------------------------

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

            # Prevent pause duration from being added to playback progression.
            self.playhead_wall_ns = (
                time.perf_counter_ns()
            )

        self.mode_label.style().unpolish(
            self.mode_label
        )

        self.mode_label.style().polish(
            self.mode_label
        )

    def toggle_pause_shortcut(
        self,
    ) -> None:

        checked = not (
            self.pause_button.isChecked()
        )

        self.pause_button.setChecked(
            checked
        )

        self.toggle_pause(
            checked
        )

    # -------------------------------------------------------------------------
    # Channel settings
    # -------------------------------------------------------------------------

    def on_scale_changed(
        self,
        control: ChannelControl,
    ) -> None:

        try:
            control.scale_value = float(
                control.scale_combo.currentData()
            )

        except Exception:
            control.scale_value = 1.0

    def on_time_span_changed(
        self,
        control: ChannelControl,
    ) -> None:

        self.apply_channel_x_range(
            control
        )

        self._update_reader_window()

    def apply_channel_x_range(
        self,
        control: ChannelControl,
    ) -> None:

        span = float(
            control.time_span_spin.value()
        )

        self.plots[
            control.index
        ].setXRange(
            -span,
            0.0,
            padding=0.0,
        )

    def apply_channel_settings(
        self,
        control: ChannelControl,
    ) -> None:

        y_min = float(
            control.y_min_spin.value()
        )

        y_max = float(
            control.y_max_spin.value()
        )

        if y_min >= y_max:
            QMessageBox.warning(
                self,
                APP_TITLE,
                (
                    f"{control.channel_name}: "
                    "Amp Min must be lower than Amp Max."
                ),
            )
            return

        control.auto_y_checkbox.setChecked(
            False
        )

        self.plots[
            control.index
        ].disableAutoRange(
            axis=pg.ViewBox.YAxis
        )

        self.plots[
            control.index
        ].setYRange(
            y_min,
            y_max,
            padding=0.0,
        )

        self.apply_channel_x_range(
            control
        )

        self.on_scale_changed(
            control
        )

    def auto_range_channel(
        self,
        control: ChannelControl,
    ) -> None:

        control.auto_y_checkbox.setChecked(
            True
        )

    def on_auto_y_changed(
        self,
        control: ChannelControl,
    ) -> None:

        if not (
            control.auto_y_checkbox.isChecked()
        ):
            self.apply_channel_settings(
                control
            )

    def reset_channel_view(
        self,
        control: ChannelControl,
    ) -> None:

        control.y_min_spin.setValue(
            DEFAULT_Y_MIN
        )

        control.y_max_spin.setValue(
            DEFAULT_Y_MAX
        )

        control.time_span_spin.setValue(
            DEFAULT_TIME_SPAN_S
        )

        control.scale_combo.setCurrentText(
            "1×"
        )

        control.scale_value = 1.0

        control.auto_y_checkbox.setChecked(
            False
        )

        plot = self.plots[
            control.index
        ]

        plot.disableAutoRange(
            axis=pg.ViewBox.YAxis
        )

        plot.setYRange(
            DEFAULT_Y_MIN,
            DEFAULT_Y_MAX,
            padding=0.0,
        )

        plot.setXRange(
            -DEFAULT_TIME_SPAN_S,
            0.0,
            padding=0.0,
        )

        self._update_reader_window()

    def reset_all_views(
        self,
    ) -> None:

        for control in self.channel_controls:
            self.reset_channel_view(
                control
            )

    # -------------------------------------------------------------------------
    # Buffered playhead
    # -------------------------------------------------------------------------

    def _reset_playhead(
        self,
    ) -> None:

        self.playhead_sample = None
        self.playhead_wall_ns = None

    def _display_sample_index(
        self,
        adc,
    ) -> float:
        """
        Adaptive jitter-buffer playhead in absolute ADC sample-number domain.

        Two independent quantities matter:
        - producer_rate_hz: long-term samples made available per second;
        - source_publish_gap_ms: how bursty that availability is.

        The reserve floor is therefore sized from BOTH the user buffer setting
        and the measured publish gap. This avoids reaching the edge every time
        Windows/TCP delivers several OBS blocks in a burst.
        """

        total = int(
            adc.total_samples
        )

        if total <= 1:
            return 0.0

        latest_sample = float(
            total - 1
        )

        oldest_sample = float(
            total - len(adc)
        )

        effective_rate = (
            self.current_sample_rate_hz()
        )

        min_rate = max(
            0.001,
            effective_rate
            * PRODUCER_RATE_MIN_RATIO,
        )
        max_rate = max(
            min_rate * 2.0,
            effective_rate
            * PRODUCER_RATE_MAX_RATIO,
        )

        rate = max(
            min_rate,
            min(
                max_rate,
                float(
                    self.producer_rate_hz
                ),
            ),
        )

        configured_buffer_samples = (
            self.current_buffer_ms()
            / 1000.0
            * rate
        )

        # Dynamic reserve based on observed delivery burst spacing. Keep at
        # least ~3 source-publication intervals plus margin.
        gap_s = max(
            0.001,
            self.source_publish_gap_ms
            / 1000.0,
        )

        burst_reserve_samples = (
            (
                3.0
                * gap_s
                + 0.100
            )
            * rate
        )

        target_reserve = max(
            configured_buffer_samples
            * 0.70,
            burst_reserve_samples,
        )

        safety_reserve = max(
            configured_buffer_samples
            * 0.35,
            (
                2.0
                * gap_s
                + 0.050
            )
            * rate,
            32.0,
        )

        # Never request a target reserve larger than what this cached snapshot
        # can hold. Leave at least a small visible history.
        available_history = max(
            64.0,
            latest_sample
            - oldest_sample,
        )

        target_reserve = min(
            target_reserve,
            available_history
            * 0.80,
        )

        safety_reserve = min(
            safety_reserve,
            max(
                32.0,
                target_reserve
                * 0.75,
            ),
        )

        now_wall_ns = (
            time.perf_counter_ns()
        )

        if (
            self.playhead_sample is None
            or self.playhead_wall_ns is None
        ):
            self.playhead_sample = max(
                oldest_sample,
                latest_sample
                - target_reserve,
            )

            self.playhead_wall_ns = (
                now_wall_ns
            )

            self._in_underrun = False

            return float(
                self.playhead_sample
            )

        elapsed_s = max(
            0.0,
            (
                now_wall_ns
                - self.playhead_wall_ns
            ) / 1_000_000_000.0,
        )

        reserve_samples = (
            latest_sample
            - self.playhead_sample
        )

        self._reserve_ms = (
            reserve_samples
            / max(
                1.0,
                rate,
            )
            * 1000.0
        )

        # Stronger PLL correction than v7. If reserve is low, slow down well
        # before the hard boundary. If reserve is high, catch up gently.
        reserve_error_fraction = (
            (
                reserve_samples
                - target_reserve
            )
            / max(
                1.0,
                target_reserve,
            )
        )

        correction_fraction = max(
            -0.25,
            min(
                0.12,
                reserve_error_fraction
                * 0.40,
            ),
        )

        playback_rate_samples_s = (
            rate
            * (
                1.0
                + correction_fraction
            )
        )

        proposed = (
            self.playhead_sample
            + elapsed_s
            * playback_rate_samples_s
        )

        max_playhead = (
            latest_sample
            - safety_reserve
        )

        hit_boundary = (
            proposed > max_playhead
        )

        if hit_boundary:
            proposed = max_playhead

            if not self._in_underrun:
                self.buffer_underruns += 1
                self._in_underrun = True

        else:
            # Require some recovery above the safety line before declaring the
            # underrun episode finished.
            recovery_margin = max(
                16.0,
                rate
                * gap_s
                * 0.50,
            )

            if (
                reserve_samples
                > safety_reserve
                + recovery_margin
            ):
                self._in_underrun = False

        if proposed < oldest_sample:
            proposed = oldest_sample

        self.playhead_sample = (
            proposed
        )

        self.playhead_wall_ns = (
            now_wall_ns
        )

        return float(
            self.playhead_sample
        )

    # -------------------------------------------------------------------------
    # Render helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _downsample(
        x,
        y,
    ):
        count = len(
            y
        )

        if count <= MAX_RENDER_POINTS:
            return (
                x,
                y,
                1,
            )

        step = int(
            np.ceil(
                count
                / MAX_RENDER_POINTS
            )
        )

        return (
            x[::step],
            y[::step],
            step,
        )

    def _apply_gap_breaks(
        self,
        x,
        y,
        render_step: int,
    ):
        """
        Use NaN only when there is a true source timestamp gap.
        Otherwise use the fast all-connected rendering path.
        """

        if len(y) < 2:
            return (
                y,
                "all",
            )

        expected_dt = (
            max(
                1,
                int(render_step),
            )
            / self.current_sample_rate_hz()
        )

        gaps = np.flatnonzero(
            np.diff(
                x
            )
            > expected_dt
            * 1.75
        )

        if not len(
            gaps
        ):
            return (
                y,
                "all",
            )

        result = y.astype(
            np.float64,
            copy=True,
        )

        result[
            gaps + 1
        ] = np.nan

        return (
            result,
            "finite",
        )

    def _update_frame_metrics(
        self,
    ) -> None:

        now_ns = (
            time.perf_counter_ns()
        )

        if self._last_render_tick_ns is not None:
            delta_ms = (
                now_ns
                - self._last_render_tick_ns
            ) / 1_000_000.0

            target_fps = int(
                self.fps_combo.currentData()
                or DEFAULT_RENDER_FPS
            )

            target_ms = (
                1000.0
                / target_fps
            )

            # Exponential moving estimate of excess frame interval.
            jitter = abs(
                delta_ms
                - target_ms
            )

            self.render_jitter_ms = (
                0.90
                * self.render_jitter_ms
                + 0.10
                * jitter
            )

        self._last_render_tick_ns = (
            now_ns
        )

        self._fps_frame_count += 1

        now = (
            time.perf_counter()
        )

        elapsed = (
            now
            - self._fps_window_start
        )

        if elapsed >= 0.75:
            self.render_fps = (
                self._fps_frame_count
                / elapsed
            )

            self._fps_frame_count = 0
            self._fps_window_start = now

    # -------------------------------------------------------------------------
    # Render
    # -------------------------------------------------------------------------

    def refresh_plots(
        self,
    ) -> None:

        if self.paused:
            return

        adc = self.cached_adc

        if adc is None:
            return

        sample_count = len(
            adc
        )

        if sample_count < 2:
            return

        timestamps_ns = (
            adc.timestamp_ns
        )

        playhead_sample = (
            self._display_sample_index(
                adc
            )
        )

        cache_start_sample = (
            int(
                adc.total_samples
            )
            - sample_count
        )

        # Cached array index corresponding to the absolute sample playhead.
        end_index = int(
            np.floor(
                playhead_sample
                - cache_start_sample
            )
        ) + 1

        end_index = max(
            0,
            min(
                sample_count,
                end_index,
            ),
        )

        if end_index < 2:
            return

        # Fractional display time in the ADC's nominal sample-time domain.
        fractional_sample = (
            playhead_sample
            - np.floor(
                playhead_sample
            )
        )

        display_ns = int(
            timestamps_ns[
                end_index - 1
            ]
            + fractional_sample
            * (
                1_000_000_000
                / self.current_sample_rate_hz()
            )
        )

        now = (
            time.perf_counter()
        )

        update_labels = (
            now
            - self._last_label_update
            >= LABEL_UPDATE_INTERVAL_S
        )

        update_auto_y = (
            now
            - self._last_auto_y_update
            >= AUTO_Y_UPDATE_INTERVAL_S
        )

        for control in self.channel_controls:

            span = float(
                control.time_span_spin.value()
            )

            start_ns = (
                display_ns
                - int(
                    span
                    * 1_000_000_000
                )
            )

            start_index = int(
                np.searchsorted(
                    timestamps_ns[
                        :end_index
                    ],
                    start_ns,
                    side="left",
                )
            )

            source = getattr(
                adc,
                control.data_attribute,
            )

            visible_ts = (
                timestamps_ns[
                    start_index:
                    end_index
                ]
            )

            raw = source[
                start_index:
                end_index
            ]

            if len(raw) < 2:
                continue

            x = (
                visible_ts.astype(
                    np.float64,
                    copy=False,
                )
                - float(
                    display_ns
                )
            ) / 1_000_000_000.0

            scale = float(
                control.scale_value
            )

            if scale == 1.0:
                y = raw
            else:
                y = (
                    raw.astype(
                        np.float64,
                        copy=False,
                    )
                    * scale
                )

            (
                x_render,
                y_render,
                render_step,
            ) = self._downsample(
                x,
                y,
            )

            (
                y_render,
                connect_mode,
            ) = self._apply_gap_breaks(
                x_render,
                y_render,
                render_step,
            )

            self.curves[
                control.index
            ].setData(
                x_render,
                y_render,
                connect=connect_mode,
            )

            if (
                control.auto_y_checkbox.isChecked()
                and update_auto_y
            ):
                finite_y = y_render[
                    np.isfinite(
                        y_render
                    )
                ]

                if len(
                    finite_y
                ):
                    y_min = float(
                        np.min(
                            finite_y
                        )
                    )

                    y_max = float(
                        np.max(
                            finite_y
                        )
                    )

                    if y_min == y_max:
                        margin = max(
                            1.0,
                            abs(
                                y_min
                            )
                            * 0.05,
                        )
                    else:
                        margin = (
                            y_max
                            - y_min
                        ) * 0.08

                    self.plots[
                        control.index
                    ].setYRange(
                        y_min - margin,
                        y_max + margin,
                        padding=0.0,
                    )

            if update_labels:
                control.current_value_label.setText(
                    (
                        f"{format_adc_value(raw[-1])}"
                        f"  ({scale:g}×)"
                    )
                )

                control.samples_label.setText(
                    (
                        f"Samples: {len(raw):,} "
                        f"• rendered: {len(y_render):,}"
                    )
                )

        if update_labels:
            self._last_label_update = now

        if update_auto_y:
            self._last_auto_y_update = now

        self._update_frame_metrics()

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def refresh_status(
        self,
    ) -> None:

        shared = self.shared

        if shared is None:
            return

        try:
            telemetry = (
                shared.read_telemetry()
            )

            bulk = (
                shared.read_bulk_status()
            )

            total = (
                shared.adc_total_samples()
            )

            self.connection_label.setText(
                (
                    "Shared RAM: DATA CONNECTED"
                    if telemetry.data_connected
                    else "Shared RAM: DATA NOT CONNECTED"
                )
            )

            self.stream_label.setText(
                (
                    f"ADC frames: {total:,} | "
                    f"drop: {bulk.dropped_frames} | "
                    f"sync: {bulk.channel_id_mismatches}"
                )
            )

            renderer = (
                "OpenGL single-view"
                if self.opengl_active
                else "CPU/Raster"
            )

            buffer_ms = (
                self.current_buffer_ms()
            )

            self.render_label.setText(
                (
                    f"Render: {self.render_fps:4.1f} FPS | "
                    f"jitter {self.render_jitter_ms:3.1f} ms | "
                    f"{renderer} | "
                    f"Fs {self.effective_sample_rate_hz:6.1f} Hz "
                    f"(raw {self.raw_sample_rate_hz:6.1f}/N{self.decimation_samples}) | "
                    f"producer {self.producer_rate_hz:6.1f} Hz | "
                    f"publish gap {self.source_publish_gap_ms:4.0f} ms | "
                    f"buffer {buffer_ms} ms | "
                    f"reserve {self._reserve_ms:3.0f} ms | "
                    f"underrun {self.buffer_underruns}"
                )
            )

            executable_hint = (
                f"Executable: {sys.executable}\n"
                "For NVIDIA RTX on hybrid Windows systems, assign this exact "
                "python.exe/pythonw.exe to High performance in Windows "
                "Settings > System > Display > Graphics."
            )

            if self.opengl_error:
                executable_hint = (
                    self.opengl_error
                    + "\n\n"
                    + executable_hint
                )

            self.render_label.setToolTip(
                executable_hint
            )

        except Exception as exc:
            self.connection_label.setText(
                (
                    "Shared RAM status error: "
                    f"{exc}"
                )
            )

    # -------------------------------------------------------------------------
    # Close
    # -------------------------------------------------------------------------

    def closeEvent(
        self,
        event: QCloseEvent,
    ) -> None:

        try:
            self.plot_timer.stop()
            self.status_timer.stop()

        except Exception:
            pass

        try:
            self.reader_thread.stop()
            self.reader_thread.wait(
                1500
            )

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

    # Default surface for the single QOpenGLWidget.
    if QOpenGLWidget is not None:
        try:
            fmt = QSurfaceFormat()

            fmt.setRenderableType(
                QSurfaceFormat.RenderableType.OpenGL
            )

            fmt.setSwapBehavior(
                QSurfaceFormat.SwapBehavior.DoubleBuffer
            )

            fmt.setSamples(
                0
            )

            # Prefer low-latency frame pacing. DWM can still synchronize the
            # final window composition.
            fmt.setSwapInterval(
                0
            )

            QSurfaceFormat.setDefaultFormat(
                fmt
            )

        except Exception:
            pass

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

    if np is None or pg is None:
        missing = []

        if np is None:
            missing.append(
                "numpy"
            )

        if pg is None:
            missing.append(
                "pyqtgraph"
            )

        QMessageBox.critical(
            None,
            APP_TITLE,
            (
                "Required package(s) are missing:\n\n"
                + ", ".join(
                    missing
                )
                + "\n\nInstall with:\n"
                "pip install numpy pyqtgraph"
            ),
        )

        return 1

    try:
        window = (
            GeophoneRealtimeWindow()
        )

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            (
                "Cannot start Geophone Real-Time:\n\n"
                f"{exc}"
            ),
        )

        return 1

    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
