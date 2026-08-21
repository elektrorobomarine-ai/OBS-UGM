"""
geophone_quality.py
===================

GRC-UGM-PERTAMINA OBS
Geophone Signal Quality Monitor

Version: 2
Shared data: shared_data_v5.py

Purpose
-------
Monitor real-time health and signal quality for:
    CH0 = Geophone X
    CH1 = Geophone Y
    CH2 = Geophone Z

The module combines two kinds of information:

1. Signal statistics calculated from the latest synchronized ADC window:
   - Mean / DC offset
   - RMS
   - Peak
   - Peak-to-peak
   - Crest factor
   - Near-rail / clipping ratio
   - Zero-crossing rate

2. Protocol/status quality from the AD7768/OBS per-channel status byte:
   - ERROR
   - Filter not settled
   - Repeated/duplicated data
   - Filter saturated
   - Channel ID consistency

It also monitors global OBS bulk-stream health:
   - Dropped frames
   - Sequence resets
   - Malformed frames
   - Channel-ID mismatches

No arbitrary "signal too small" fault is generated because a quiet seismic
channel may legitimately have a low RMS level. Quality state is primarily based
on protocol faults, saturation/clipping, repeated samples, filter-settling
state, and excessive DC offset.

Performance
-----------
- Shared RAM is read only when the source sample counter changes.
- Signal-quality processing runs in a background QThread.
- GUI updates use cached metrics and a 30 FPS timer.
- Historical RMS / peak-to-peak / DC trends are maintained locally.
- One PyQtGraph GraphicsLayoutWidget is used for the trend plots.
- OpenGL is requested through one QOpenGLWidget viewport.
- v2 reads the authoritative effective ADC sample rate from shared_data_v5.
- Analysis windows remain specified in seconds, but sample counts are derived
  from the effective shared-stream rate.
- Zero-crossing rate is calibrated using the effective sample rate instead of
  the raw 1000-Hz source rate.
- A change of adc_session_id or effective sample rate clears trend history and
  resets the worker's producer-rate estimator so statistics from two different
  acquisition configurations are never mixed.

Example:
    raw ADC = 1000 Hz
    Average N = 5
    effective shared rate = 200 Hz

A 2-second quality window therefore uses about 400 synchronized samples per
channel, not 2000 raw-rate samples.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.QUALITY"
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
    QCloseEvent,
    QFont,
    QIcon,
    QSurfaceFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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

APP_TITLE = "Geophone Signal Quality"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

CHANNELS = (
    ("CH0", "Geophone X", "ch0", "status0"),
    ("CH1", "Geophone Y", "ch1", "status1"),
    ("CH2", "Geophone Z", "ch2", "status2"),
)

ADC_FULL_SCALE = 8_388_608.0
NEAR_RAIL_FRACTION = 0.98
DC_WARN_FRACTION = 0.20
CLIP_WARN_RATIO = 0.001
CLIP_BAD_RATIO = 0.01

DEFAULT_ANALYSIS_WINDOW_S = 2.0
ANALYSIS_WINDOW_CHOICES_S = (
    0.25,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
)

DEFAULT_METRIC_RATE_HZ = 10
METRIC_RATE_CHOICES_HZ = (
    2,
    5,
    10,
    15,
    20,
)

DEFAULT_TREND_HISTORY_S = 60
TREND_HISTORY_CHOICES_S = (
    30,
    60,
    120,
    300,
)

DEFAULT_GUI_FPS = 30
GUI_FPS_CHOICES = (
    15,
    30,
    45,
    60,
)

STATUS_INTERVAL_MS = 500

# Measured producer throughput is a health/diagnostic value only.
# Physical time calibration comes from shared_data_v5 effective Fs.
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0


# =============================================================================
# Helpers / data classes
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


@dataclass(frozen=True)
class ChannelQuality:
    channel_name: str
    axis_name: str

    sample_count: int

    mean: float
    rms: float
    peak: float
    peak_to_peak: float
    crest_factor: float
    zero_crossing_rate_hz: float

    near_rail_ratio: float

    error_ratio: float
    unsettled_ratio: float
    repeated_ratio: float
    saturated_ratio: float
    channel_id_mismatch_ratio: float

    state: str
    reason: str


@dataclass(frozen=True)
class QualitySnapshot:
    timestamp_monotonic: float
    total_samples: int

    raw_sample_rate_hz: float
    effective_sample_rate_hz: float
    decimation_samples: int
    decimation_mode: str
    adc_session_id: int
    analysis_window_s: float
    analysis_sample_count: int

    channels: tuple

    frames_received: int
    dropped_frames: int
    sequence_resets: int
    malformed_frames: int
    channel_id_mismatches: int

    error_flag_words: int
    filter_not_settled_words: int
    repeated_words: int
    saturated_words: int

    producer_rate_hz: float
    compute_ms: float


# =============================================================================
# Background quality processor
# =============================================================================


class QualityWorkerThread(QThread):
    quality_ready = Signal(object)
    worker_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._stop_event = threading.Event()
        self._settings_lock = threading.Lock()

        self._analysis_window_s = DEFAULT_ANALYSIS_WINDOW_S
        self._metric_rate_hz = DEFAULT_METRIC_RATE_HZ

        self._last_total = -1
        self._last_compute_time = 0.0

        self._rate_history = deque()
        self._producer_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._effective_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._adc_session_id = -1

    def stop(self) -> None:
        self._stop_event.set()

    def set_settings(
        self,
        *,
        analysis_window_s: float,
        metric_rate_hz: int,
    ) -> None:
        with self._settings_lock:
            self._analysis_window_s = max(
                0.10,
                float(analysis_window_s),
            )
            self._metric_rate_hz = max(
                1,
                int(metric_rate_hz),
            )

    def _get_settings(self):
        with self._settings_lock:
            return (
                float(self._analysis_window_s),
                int(self._metric_rate_hz),
            )

    @staticmethod
    def _channel_quality(
        channel_index: int,
        channel_name: str,
        axis_name: str,
        signal,
        status,
        sample_rate_hz: float,
    ) -> ChannelQuality:

        signal = signal.astype(
            np.float64,
            copy=False,
        )

        n = len(signal)

        if n <= 0:
            return ChannelQuality(
                channel_name=channel_name,
                axis_name=axis_name,
                sample_count=0,
                mean=0.0,
                rms=0.0,
                peak=0.0,
                peak_to_peak=0.0,
                crest_factor=0.0,
                zero_crossing_rate_hz=0.0,
                near_rail_ratio=0.0,
                error_ratio=0.0,
                unsettled_ratio=0.0,
                repeated_ratio=0.0,
                saturated_ratio=0.0,
                channel_id_mismatch_ratio=0.0,
                state="NO DATA",
                reason="No ADC samples",
            )

        mean = float(
            np.mean(signal)
        )

        centered = signal - mean

        rms = float(
            np.sqrt(
                np.mean(
                    centered * centered
                )
            )
        )

        peak = float(
            np.max(
                np.abs(centered)
            )
        )

        peak_to_peak = float(
            np.ptp(signal)
        )

        crest_factor = (
            peak / rms
            if rms > 1.0e-20
            else 0.0
        )

        if n >= 2:
            sign_changes = np.count_nonzero(
                np.signbit(centered[:-1])
                != np.signbit(centered[1:])
            )

            duration_s = (
                (n - 1)
                / max(
                    0.001,
                    float(
                        sample_rate_hz
                    ),
                )
            )

            zero_crossing_rate_hz = (
                float(sign_changes)
                / max(
                    duration_s,
                    1.0e-12,
                )
            )
        else:
            zero_crossing_rate_hz = 0.0

        near_rail_ratio = float(
            np.mean(
                np.abs(signal)
                >= (
                    ADC_FULL_SCALE
                    * NEAR_RAIL_FRACTION
                )
            )
        )

        status = status.astype(
            np.uint8,
            copy=False,
        )

        error_ratio = float(
            np.mean(
                (status & 0x80) != 0
            )
        )

        unsettled_ratio = float(
            np.mean(
                (status & 0x40) != 0
            )
        )

        repeated_ratio = float(
            np.mean(
                (status & 0x20) != 0
            )
        )

        saturated_ratio = float(
            np.mean(
                (status & 0x08) != 0
            )
        )

        channel_ids = (
            status & 0x07
        )

        channel_id_mismatch_ratio = float(
            np.mean(
                channel_ids
                != channel_index
            )
        )

        dc_fraction = (
            abs(mean)
            / ADC_FULL_SCALE
        )

        # Quality classification deliberately avoids "low RMS" as an error.
        bad_reasons = []
        warn_reasons = []

        if error_ratio > 0.0:
            bad_reasons.append(
                "ADC ERROR flag"
            )

        if channel_id_mismatch_ratio > 0.0:
            bad_reasons.append(
                "channel ID mismatch"
            )

        if (
            saturated_ratio > 0.0
            or near_rail_ratio
            >= CLIP_BAD_RATIO
        ):
            bad_reasons.append(
                "saturation/clipping"
            )

        if repeated_ratio > 0.0:
            warn_reasons.append(
                "repeated data"
            )

        if unsettled_ratio > 0.0:
            warn_reasons.append(
                "filter not settled"
            )

        if (
            near_rail_ratio
            >= CLIP_WARN_RATIO
            and near_rail_ratio
            < CLIP_BAD_RATIO
        ):
            warn_reasons.append(
                "near ADC rail"
            )

        if dc_fraction >= DC_WARN_FRACTION:
            warn_reasons.append(
                "high DC offset"
            )

        if bad_reasons:
            state = "BAD"
            reason = "; ".join(
                bad_reasons
                + warn_reasons
            )

        elif warn_reasons:
            state = "WARN"
            reason = "; ".join(
                warn_reasons
            )

        else:
            state = "GOOD"
            reason = "No protocol/rail fault"

        return ChannelQuality(
            channel_name=channel_name,
            axis_name=axis_name,
            sample_count=n,
            mean=mean,
            rms=rms,
            peak=peak,
            peak_to_peak=peak_to_peak,
            crest_factor=crest_factor,
            zero_crossing_rate_hz=(
                zero_crossing_rate_hz
            ),
            near_rail_ratio=(
                near_rail_ratio
            ),
            error_ratio=(
                error_ratio
            ),
            unsettled_ratio=(
                unsettled_ratio
            ),
            repeated_ratio=(
                repeated_ratio
            ),
            saturated_ratio=(
                saturated_ratio
            ),
            channel_id_mismatch_ratio=(
                channel_id_mismatch_ratio
            ),
            state=state,
            reason=reason,
        )

    def run(self) -> None:
        shared = None

        try:
            shared = OBSSharedData()

            stream_info = (
                shared.read_adc_stream_info()
            )
            self._effective_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self._producer_rate_hz = float(
                self._effective_rate_hz
            )
            self._adc_session_id = int(
                stream_info.adc_session_id
            )

            while not self._stop_event.is_set():

                (
                    analysis_window_s,
                    metric_rate_hz,
                ) = self._get_settings()

                stream_info = (
                    shared.read_adc_stream_info()
                )

                effective_rate_hz = max(
                    0.001,
                    float(
                        stream_info.effective_sample_rate_hz
                    ),
                )
                session_id = int(
                    stream_info.adc_session_id
                )

                stream_changed = (
                    session_id
                    != self._adc_session_id
                    or abs(
                        effective_rate_hz
                        - self._effective_rate_hz
                    )
                    > max(
                        1.0e-9,
                        1.0e-6
                        * effective_rate_hz,
                    )
                )

                if stream_changed:
                    self._adc_session_id = (
                        session_id
                    )
                    self._effective_rate_hz = (
                        effective_rate_hz
                    )
                    self._producer_rate_hz = (
                        effective_rate_hz
                    )
                    self._rate_history.clear()
                    self._last_total = -1
                    self._last_compute_time = 0.0

                total = (
                    shared.adc_total_samples()
                )

                now = time.perf_counter()

                if total != self._last_total:
                    self._rate_history.append(
                        (
                            now,
                            int(total),
                        )
                    )

                    cutoff = now - 5.0

                    while (
                        len(
                            self._rate_history
                        )
                        > 2
                        and self._rate_history[0][0]
                        < cutoff
                    ):
                        self._rate_history.popleft()

                    if len(
                        self._rate_history
                    ) >= 2:
                        t0, n0 = (
                            self._rate_history[0]
                        )
                        t1, n1 = (
                            self._rate_history[-1]
                        )

                        dt = t1 - t0
                        dn = n1 - n0

                        if (
                            dt >= 1.0
                            and dn > 0
                        ):
                            measured = (
                                dn / dt
                            )

                            min_rate = max(
                                0.001,
                                effective_rate_hz
                                * PRODUCER_RATE_MIN_RATIO,
                            )
                            max_rate = max(
                                min_rate * 2.0,
                                effective_rate_hz
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

                    self._last_total = int(
                        total
                    )

                min_period = (
                    1.0
                    / max(
                        1,
                        metric_rate_hz,
                    )
                )

                if (
                    total <= 0
                    or (
                        now
                        - self._last_compute_time
                    )
                    < min_period
                ):
                    self.msleep(5)
                    continue

                self._last_compute_time = now

                count = max(
                    16,
                    int(
                        round(
                            analysis_window_s
                            * effective_rate_hz
                        )
                    ),
                )

                compute_start = (
                    time.perf_counter()
                )

                adc = (
                    shared.read_adc_latest_numpy(
                        count
                    )
                )

                sample_rate_hz = max(
                    0.001,
                    float(
                        adc.sample_rate_hz
                    ),
                )

                bulk = (
                    shared.read_bulk_status()
                )

                channels = []

                for index, (
                    channel_name,
                    axis_name,
                    data_attr,
                    status_attr,
                ) in enumerate(
                    CHANNELS
                ):
                    channels.append(
                        self._channel_quality(
                            index,
                            channel_name,
                            axis_name,
                            getattr(
                                adc,
                                data_attr,
                            ),
                            getattr(
                                adc,
                                status_attr,
                            ),
                            sample_rate_hz,
                        )
                    )

                compute_ms = (
                    time.perf_counter()
                    - compute_start
                ) * 1000.0

                snapshot = QualitySnapshot(
                    timestamp_monotonic=(
                        time.perf_counter()
                    ),
                    total_samples=int(
                        adc.total_samples
                    ),
                    raw_sample_rate_hz=float(
                        stream_info.raw_sample_rate_hz
                    ),
                    effective_sample_rate_hz=float(
                        sample_rate_hz
                    ),
                    decimation_samples=max(
                        1,
                        int(
                            stream_info.decimation_samples
                        ),
                    ),
                    decimation_mode=str(
                        stream_info.decimation_mode
                    ),
                    adc_session_id=int(
                        stream_info.adc_session_id
                    ),
                    analysis_window_s=float(
                        analysis_window_s
                    ),
                    analysis_sample_count=int(
                        len(
                            adc.ch0
                        )
                    ),
                    channels=tuple(
                        channels
                    ),
                    frames_received=int(
                        bulk.frames_received
                    ),
                    dropped_frames=int(
                        bulk.dropped_frames
                    ),
                    sequence_resets=int(
                        bulk.sequence_resets
                    ),
                    malformed_frames=int(
                        bulk.malformed_frames
                    ),
                    channel_id_mismatches=int(
                        bulk.channel_id_mismatches
                    ),
                    error_flag_words=int(
                        bulk.error_flag_words
                    ),
                    filter_not_settled_words=int(
                        bulk.filter_not_settled_words
                    ),
                    repeated_words=int(
                        bulk.repeated_words
                    ),
                    saturated_words=int(
                        bulk.saturated_words
                    ),
                    producer_rate_hz=float(
                        self._producer_rate_hz
                    ),
                    compute_ms=float(
                        compute_ms
                    ),
                )

                self.quality_ready.emit(
                    snapshot
                )

        except Exception as exc:
            if not self._stop_event.is_set():
                self.worker_error.emit(
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


class GeophoneQualityWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        if np is None or pg is None:
            raise RuntimeError(
                "Geophone Quality requires NumPy and PyQtGraph."
            )

        self.shared: Optional[
            OBSSharedData
        ] = None

        try:
            self.shared = OBSSharedData()

        except Exception as exc:
            raise RuntimeError(
                f"Cannot attach OBS shared RAM: {exc}"
            ) from exc

        try:
            stream_info = (
                self.shared.read_adc_stream_info()
            )

            self.raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self.effective_sample_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self.decimation_samples = max(
                1,
                int(
                    stream_info.decimation_samples
                ),
            )
            self.decimation_mode = str(
                stream_info.decimation_mode
            )
            self.adc_session_id = int(
                stream_info.adc_session_id
            )

        except Exception:
            self.raw_sample_rate_hz = float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            self.effective_sample_rate_hz = float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            self.decimation_samples = 1
            self.decimation_mode = "raw"
            self.adc_session_id = -1

        self.latest_quality: Optional[
            QualitySnapshot
        ] = None

        self.trend_times = deque()
        self.trend_rms = [
            deque(),
            deque(),
            deque(),
        ]
        self.trend_ptp = [
            deque(),
            deque(),
            deque(),
        ]
        self.trend_dc = [
            deque(),
            deque(),
            deque(),
        ]

        self.opengl_active = False
        self.opengl_error = ""

        self.render_fps = 0.0
        self.render_jitter_ms = 0.0
        self._frame_count = 0
        self._fps_start = (
            time.perf_counter()
        )
        self._last_render_ns = None

        self.channel_cards = []
        self.rms_curves = []
        self.ptp_curves = []
        self.dc_curves = []

        self.setWindowTitle(
            f"{APP_TITLE} - {SYSTEM_TITLE}"
        )

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(
                icon
            )

        self.resize(
            1500,
            900,
        )
        self.setMinimumSize(
            1100,
            700,
        )

        self._configure_pyqtgraph()
        self._build_ui()
        self._apply_style()

        self.worker = (
            QualityWorkerThread(
                self
            )
        )
        self.worker.quality_ready.connect(
            self.on_quality_snapshot
        )
        self.worker.worker_error.connect(
            self.on_worker_error
        )
        self._push_worker_settings()
        self.worker.start()

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
        self._set_gui_fps(
            DEFAULT_GUI_FPS
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

    # ------------------------------------------------------------------ graphics

    @staticmethod
    def _configure_pyqtgraph():
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

    def _install_opengl_viewport(
        self,
        graphics,
    ) -> None:
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

            viewport.setFormat(
                fmt
            )
            graphics.setViewport(
                viewport
            )

            self.opengl_active = isinstance(
                graphics.viewport(),
                QOpenGLWidget,
            )

        except Exception as exc:
            self.opengl_active = False
            self.opengl_error = str(
                exc
            )

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
            14, 12, 14, 12
        )
        root.setSpacing(8)

        header = QHBoxLayout()

        title_box = QVBoxLayout()
        title_box.setSpacing(1)

        title = QLabel(
            "GEOPHONE SIGNAL QUALITY"
        )
        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            "CH0 / X  •  CH1 / Y  •  CH2 / Z  •  ADC Status  •  Bulk Stream Health"
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

        self.overall_state_label = QLabel(
            "WAITING"
        )
        self.overall_state_label.setObjectName(
            "overallWaiting"
        )
        self.overall_state_label.setMinimumWidth(
            120
        )
        self.overall_state_label.setAlignment(
            Qt.AlignCenter
        )

        header.addWidget(
            self.overall_state_label
        )

        root.addLayout(
            header
        )

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

        self.connection_label = QLabel(
            "Shared RAM: checking..."
        )
        self.connection_label.setObjectName(
            "statusLabel"
        )

        self.worker_label = QLabel(
            "Quality worker: --"
        )
        self.worker_label.setObjectName(
            "statusLabel"
        )

        self.render_label = QLabel(
            "Render: --"
        )
        self.render_label.setObjectName(
            "statusLabel"
        )

        sl.addWidget(
            self.connection_label
        )
        sl.addStretch(1)
        sl.addWidget(
            self.worker_label
        )
        sl.addSpacing(14)
        sl.addWidget(
            self.render_label
        )

        root.addWidget(
            status
        )

        # Channel cards.
        cards = QHBoxLayout()
        cards.setSpacing(8)

        for index, (
            channel_name,
            axis_name,
            _data,
            _status,
        ) in enumerate(
            CHANNELS
        ):
            card = self._create_channel_card(
                index,
                channel_name,
                axis_name,
            )
            self.channel_cards.append(
                card
            )
            cards.addWidget(
                card["frame"],
                1,
            )

        root.addLayout(
            cards
        )

        splitter = QSplitter(
            Qt.Horizontal
        )
        splitter.setChildrenCollapsible(
            False
        )

        splitter.addWidget(
            self._build_trend_panel()
        )
        splitter.addWidget(
            self._build_control_panel()
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
            [1100, 360]
        )

        root.addWidget(
            splitter,
            1,
        )

    def _create_channel_card(
        self,
        index: int,
        channel_name: str,
        axis_name: str,
    ):
        frame = QFrame()
        frame.setObjectName(
            "channelCard"
        )

        layout = QVBoxLayout(
            frame
        )
        layout.setContentsMargins(
            12, 10, 12, 10
        )
        layout.setSpacing(5)

        header = QHBoxLayout()

        name = QLabel(
            f"{channel_name} — {axis_name}"
        )
        name.setObjectName(
            "cardTitle"
        )

        state = QLabel(
            "WAITING"
        )
        state.setObjectName(
            "qualityWaiting"
        )
        state.setAlignment(
            Qt.AlignCenter
        )
        state.setMinimumWidth(
            80
        )

        header.addWidget(
            name
        )
        header.addStretch(1)
        header.addWidget(
            state
        )

        layout.addLayout(
            header
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(
            12
        )
        grid.setVerticalSpacing(
            3
        )

        labels = {}

        fields = (
            ("RMS", "rms"),
            ("Peak", "peak"),
            ("Peak-to-Peak", "ptp"),
            ("Mean / DC", "mean"),
            ("Crest Factor", "crest"),
            ("Zero Crossing", "zcr"),
            ("Near Rail", "rail"),
            ("ADC Error", "error"),
            ("Unsettled", "unsettled"),
            ("Repeated", "repeated"),
            ("Saturated", "saturated"),
            ("ID Mismatch", "id_mismatch"),
        )

        for row, (
            text,
            key,
        ) in enumerate(
            fields
        ):
            title = QLabel(text)
            title.setObjectName(
                "metricName"
            )

            value = QLabel("--")
            value.setObjectName(
                "metricValue"
            )
            value.setAlignment(
                Qt.AlignRight
            )

            grid.addWidget(
                title,
                row,
                0,
            )
            grid.addWidget(
                value,
                row,
                1,
            )

            labels[key] = value

        layout.addLayout(
            grid
        )

        reason = QLabel(
            "Waiting for ADC data"
        )
        reason.setObjectName(
            "qualityReason"
        )
        reason.setWordWrap(
            True
        )

        layout.addWidget(
            reason
        )

        return {
            "frame": frame,
            "state": state,
            "reason": reason,
            **labels,
        }

    def _build_trend_panel(self):
        frame = QFrame()
        frame.setObjectName(
            "trendFrame"
        )

        layout = QVBoxLayout(
            frame
        )
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

        pens = (
            pg.mkPen(
                "#FF5E5E",
                width=1.4,
            ),
            pg.mkPen(
                "#5FE07B",
                width=1.4,
            ),
            pg.mkPen(
                "#5C96FF",
                width=1.4,
            ),
        )

        self.rms_plot = (
            self.graphics.addPlot(
                row=0,
                col=0,
            )
        )
        self.rms_plot.setTitle(
            "RMS Trend",
            color="#FFFFFF",
            size="11pt",
        )
        self.rms_plot.setLabel(
            "left",
            "RMS",
            units="count",
        )
        self.rms_plot.setLabel(
            "bottom",
            "History",
            units="s",
        )
        self.rms_plot.showGrid(
            x=True,
            y=True,
            alpha=0.18,
        )

        self.ptp_plot = (
            self.graphics.addPlot(
                row=1,
                col=0,
            )
        )
        self.ptp_plot.setTitle(
            "Peak-to-Peak Trend",
            color="#FFFFFF",
            size="11pt",
        )
        self.ptp_plot.setLabel(
            "left",
            "P-P",
            units="count",
        )
        self.ptp_plot.setLabel(
            "bottom",
            "History",
            units="s",
        )
        self.ptp_plot.showGrid(
            x=True,
            y=True,
            alpha=0.18,
        )

        self.dc_plot = (
            self.graphics.addPlot(
                row=2,
                col=0,
            )
        )
        self.dc_plot.setTitle(
            "DC / Mean Trend",
            color="#FFFFFF",
            size="11pt",
        )
        self.dc_plot.setLabel(
            "left",
            "Mean",
            units="count",
        )
        self.dc_plot.setLabel(
            "bottom",
            "History",
            units="s",
        )
        self.dc_plot.showGrid(
            x=True,
            y=True,
            alpha=0.18,
        )

        for index in range(3):
            self.rms_curves.append(
                self.rms_plot.plot(
                    [],
                    [],
                    pen=pens[index],
                )
            )
            self.ptp_curves.append(
                self.ptp_plot.plot(
                    [],
                    [],
                    pen=pens[index],
                )
            )
            self.dc_curves.append(
                self.dc_plot.plot(
                    [],
                    [],
                    pen=pens[index],
                )
            )

        return frame

    def _build_control_panel(self):
        panel = QFrame()
        panel.setObjectName(
            "controlPanel"
        )

        layout = QVBoxLayout(
            panel
        )
        layout.setContentsMargins(
            8, 0, 0, 0
        )
        layout.setSpacing(8)

        heading = QLabel(
            "QUALITY SETTINGS"
        )
        heading.setObjectName(
            "settingsTitle"
        )
        layout.addWidget(
            heading
        )

        settings = QGroupBox(
            "Analysis"
        )
        settings.setObjectName(
            "channelGroup"
        )

        grid = QGridLayout(
            settings
        )
        grid.setContentsMargins(
            10, 12, 10, 10
        )

        self.window_combo = QComboBox()
        for value in (
            ANALYSIS_WINDOW_CHOICES_S
        ):
            self.window_combo.addItem(
                f"{value:g} s",
                float(value),
            )
        self.window_combo.setCurrentText(
            f"{DEFAULT_ANALYSIS_WINDOW_S:g} s"
        )
        self.window_combo.currentIndexChanged.connect(
            self._push_worker_settings
        )

        self.metric_rate_combo = QComboBox()
        for rate in (
            METRIC_RATE_CHOICES_HZ
        ):
            self.metric_rate_combo.addItem(
                f"{rate} Hz",
                rate,
            )
        self.metric_rate_combo.setCurrentText(
            f"{DEFAULT_METRIC_RATE_HZ} Hz"
        )
        self.metric_rate_combo.currentIndexChanged.connect(
            self._push_worker_settings
        )

        self.history_combo = QComboBox()
        for seconds in (
            TREND_HISTORY_CHOICES_S
        ):
            self.history_combo.addItem(
                f"{seconds} s",
                seconds,
            )
        self.history_combo.setCurrentText(
            f"{DEFAULT_TREND_HISTORY_S} s"
        )

        self.gui_fps_combo = QComboBox()
        for fps in (
            GUI_FPS_CHOICES
        ):
            self.gui_fps_combo.addItem(
                f"{fps} FPS",
                fps,
            )
        self.gui_fps_combo.setCurrentText(
            f"{DEFAULT_GUI_FPS} FPS"
        )
        self.gui_fps_combo.currentIndexChanged.connect(
            self.on_gui_fps_changed
        )

        grid.addWidget(
            QLabel("Analysis Window"),
            0,
            0,
        )
        grid.addWidget(
            self.window_combo,
            0,
            1,
        )

        grid.addWidget(
            QLabel("Metric Rate"),
            1,
            0,
        )
        grid.addWidget(
            self.metric_rate_combo,
            1,
            1,
        )

        grid.addWidget(
            QLabel("Trend History"),
            2,
            0,
        )
        grid.addWidget(
            self.history_combo,
            2,
            1,
        )

        grid.addWidget(
            QLabel("GUI FPS"),
            3,
            0,
        )
        grid.addWidget(
            self.gui_fps_combo,
            3,
            1,
        )

        layout.addWidget(
            settings
        )

        bulk_group = QGroupBox(
            "OBS Bulk Stream"
        )
        bulk_group.setObjectName(
            "channelGroup"
        )

        bg = QGridLayout(
            bulk_group
        )
        bg.setContentsMargins(
            10, 12, 10, 10
        )

        self.bulk_frames = QLabel(
            "Frames received: --"
        )
        self.bulk_drops = QLabel(
            "Dropped frames: --"
        )
        self.bulk_resets = QLabel(
            "Sequence resets: --"
        )
        self.bulk_malformed = QLabel(
            "Malformed frames: --"
        )
        self.bulk_sync = QLabel(
            "CH-ID mismatches: --"
        )
        self.bulk_errors = QLabel(
            "ERROR words: --"
        )
        self.bulk_unsettled = QLabel(
            "Unsettled words: --"
        )
        self.bulk_repeated = QLabel(
            "Repeated words: --"
        )
        self.bulk_saturated = QLabel(
            "Saturated words: --"
        )

        for row, label in enumerate(
            (
                self.bulk_frames,
                self.bulk_drops,
                self.bulk_resets,
                self.bulk_malformed,
                self.bulk_sync,
                self.bulk_errors,
                self.bulk_unsettled,
                self.bulk_repeated,
                self.bulk_saturated,
            )
        ):
            label.setObjectName(
                "bulkValue"
            )
            bg.addWidget(
                label,
                row,
                0,
                1,
                2,
            )

        layout.addWidget(
            bulk_group
        )

        criteria = QGroupBox(
            "Quality Criteria"
        )
        criteria.setObjectName(
            "channelGroup"
        )

        cg = QVBoxLayout(
            criteria
        )
        cg.setContentsMargins(
            10, 12, 10, 10
        )

        note = QLabel(
            "BAD: ADC ERROR, channel-ID mismatch, filter saturation, or ≥1% near-rail samples.\n\n"
            "WARN: repeated data, filter not settled, ≥0.1% near-rail samples, or |DC| ≥20% full-scale.\n\n"
            "Low RMS alone is not treated as a fault because a quiet seismic channel can be valid."
        )
        note.setObjectName(
            "sampleInfo"
        )
        note.setWordWrap(
            True
        )

        cg.addWidget(
            note
        )

        layout.addWidget(
            criteria
        )

        reset_trends = QPushButton(
            "Reset Trend History"
        )
        reset_trends.setObjectName(
            "smallPrimaryButton"
        )
        reset_trends.clicked.connect(
            self.reset_trends
        )

        layout.addWidget(
            reset_trends
        )
        layout.addStretch(
            1
        )

        return panel

    # ------------------------------------------------------------------ style

    def _apply_style(self):
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

            QFrame#statusFrame,
            QFrame#channelCard {
                background-color: #0B1B27;
                border: 1px solid #17374A;
                border-radius: 8px;
            }

            QLabel#statusLabel {
                color: #B7CBD6;
                font-size: 10px;
            }

            QLabel#cardTitle {
                color: #FFFFFF;
                font-size: 13px;
                font-weight: 800;
            }

            QLabel#metricName {
                color: #89A4B3;
                font-size: 9px;
            }

            QLabel#metricValue {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 10px;
                font-weight: 700;
            }

            QLabel#qualityReason {
                color: #AFC3CE;
                font-size: 9px;
                padding-top: 4px;
            }

            QLabel#qualityGood,
            QLabel#overallGood {
                background-color: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 7px;
                color: #A9F1D2;
                font-weight: 800;
                padding: 4px 10px;
            }

            QLabel#qualityWarn,
            QLabel#overallWarn {
                background-color: #403510;
                border: 1px solid #A88821;
                border-radius: 7px;
                color: #FFE49A;
                font-weight: 800;
                padding: 4px 10px;
            }

            QLabel#qualityBad,
            QLabel#overallBad {
                background-color: #481D22;
                border: 1px solid #A84B58;
                border-radius: 7px;
                color: #FFB3BD;
                font-weight: 800;
                padding: 4px 10px;
            }

            QLabel#qualityWaiting,
            QLabel#overallWaiting {
                background-color: #172631;
                border: 1px solid #35546A;
                border-radius: 7px;
                color: #A9BECA;
                font-weight: 800;
                padding: 4px 10px;
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

            QLabel#bulkValue {
                color: #D6E4EB;
                font-family: "Consolas";
                font-size: 10px;
            }

            QLabel#sampleInfo {
                color: #7894A4;
                font-size: 9px;
            }

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

            QPushButton {
                min-height: 28px;
                border-radius: 6px;
                padding: 3px 7px;
                font-weight: 700;
            }

            QPushButton#smallPrimaryButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QSplitter::handle {
                background-color: #17374A;
                width: 2px;
            }
            """
        )

    # ------------------------------------------------------------------ settings / history

    def current_sample_rate_hz(
        self,
    ) -> float:
        return max(
            0.001,
            float(
                self.effective_sample_rate_hz
            ),
        )

    def current_analysis_sample_count(
        self,
    ) -> int:
        return max(
            16,
            int(
                round(
                    self.current_analysis_window_s()
                    * self.current_sample_rate_hz()
                )
            ),
        )

    def current_analysis_window_s(
        self,
    ):
        return float(
            self.window_combo.currentData()
            or DEFAULT_ANALYSIS_WINDOW_S
        )

    def current_metric_rate_hz(
        self,
    ):
        return int(
            self.metric_rate_combo.currentData()
            or DEFAULT_METRIC_RATE_HZ
        )

    def current_history_s(self):
        return int(
            self.history_combo.currentData()
            or DEFAULT_TREND_HISTORY_S
        )

    def current_gui_fps(self):
        return int(
            self.gui_fps_combo.currentData()
            or DEFAULT_GUI_FPS
        )

    def _push_worker_settings(
        self,
        *_args,
    ):
        if not hasattr(
            self,
            "worker",
        ):
            return

        self.worker.set_settings(
            analysis_window_s=(
                self.current_analysis_window_s()
            ),
            metric_rate_hz=(
                self.current_metric_rate_hz()
            ),
        )

    def _set_gui_fps(
        self,
        fps: int,
    ):
        self.render_timer.start(
            max(
                1,
                round(
                    1000.0
                    / max(
                        1,
                        int(fps),
                    )
                ),
            )
        )

    def on_gui_fps_changed(
        self,
        *_args,
    ):
        self._set_gui_fps(
            self.current_gui_fps()
        )

    def reset_trends(self):
        self.trend_times.clear()

        for group in (
            self.trend_rms,
            self.trend_ptp,
            self.trend_dc,
        ):
            for series in group:
                series.clear()

    def _trim_trends(self):
        if not self.trend_times:
            return

        now = (
            self.trend_times[-1]
        )
        cutoff = (
            now
            - self.current_history_s()
        )

        while (
            self.trend_times
            and self.trend_times[0]
            < cutoff
        ):
            self.trend_times.popleft()

            for group in (
                self.trend_rms,
                self.trend_ptp,
                self.trend_dc,
            ):
                for series in group:
                    if series:
                        series.popleft()

    # ------------------------------------------------------------------ worker callback

    def on_quality_snapshot(
        self,
        snapshot: QualitySnapshot,
    ):
        previous_session_id = int(
            self.adc_session_id
        )
        previous_effective_rate = float(
            self.effective_sample_rate_hz
        )

        self.raw_sample_rate_hz = float(
            snapshot.raw_sample_rate_hz
        )
        self.effective_sample_rate_hz = max(
            0.001,
            float(
                snapshot.effective_sample_rate_hz
            ),
        )
        self.decimation_samples = max(
            1,
            int(
                snapshot.decimation_samples
            ),
        )
        self.decimation_mode = str(
            snapshot.decimation_mode
        )
        self.adc_session_id = int(
            snapshot.adc_session_id
        )

        stream_changed = (
            self.adc_session_id
            != previous_session_id
            or abs(
                self.effective_sample_rate_hz
                - previous_effective_rate
            )
            > max(
                1.0e-9,
                1.0e-6
                * self.effective_sample_rate_hz,
            )
        )

        if stream_changed:
            self.reset_trends()

        self.latest_quality = snapshot

        t = (
            snapshot.timestamp_monotonic
        )

        self.trend_times.append(
            t
        )

        for index, channel in enumerate(
            snapshot.channels
        ):
            self.trend_rms[
                index
            ].append(
                channel.rms
            )

            self.trend_ptp[
                index
            ].append(
                channel.peak_to_peak
            )

            self.trend_dc[
                index
            ].append(
                channel.mean
            )

        self._trim_trends()

    def on_worker_error(
        self,
        message: str,
    ):
        self.worker_label.setText(
            f"Quality worker error: {message}"
        )

    # ------------------------------------------------------------------ UI update

    @staticmethod
    def _percent(
        value: float,
    ):
        return (
            f"{100.0 * value:.3f}%"
        )

    def _set_state_label(
        self,
        label: QLabel,
        state: str,
        overall: bool = False,
    ):
        state = str(state).upper()

        if overall:
            names = {
                "GOOD": "overallGood",
                "WARN": "overallWarn",
                "BAD": "overallBad",
                "WAITING": "overallWaiting",
            }
        else:
            names = {
                "GOOD": "qualityGood",
                "WARN": "qualityWarn",
                "BAD": "qualityBad",
                "NO DATA": "qualityWaiting",
                "WAITING": "qualityWaiting",
            }

        label.setText(
            state
        )

        label.setObjectName(
            names.get(
                state,
                names.get(
                    "WAITING"
                ),
            )
        )

        label.style().unpolish(
            label
        )
        label.style().polish(
            label
        )

    def _update_channel_cards(
        self,
    ):
        q = self.latest_quality
        if q is None:
            return

        states = []

        for index, channel in enumerate(
            q.channels
        ):
            card = self.channel_cards[
                index
            ]

            self._set_state_label(
                card["state"],
                channel.state,
                overall=False,
            )

            states.append(
                channel.state
            )

            card["rms"].setText(
                f"{channel.rms:,.1f}"
            )
            card["peak"].setText(
                f"{channel.peak:,.1f}"
            )
            card["ptp"].setText(
                f"{channel.peak_to_peak:,.1f}"
            )
            card["mean"].setText(
                f"{channel.mean:,.1f}"
            )
            card["crest"].setText(
                f"{channel.crest_factor:.3f}"
            )
            card["zcr"].setText(
                f"{channel.zero_crossing_rate_hz:.2f} Hz"
            )

            card["rail"].setText(
                self._percent(
                    channel.near_rail_ratio
                )
            )
            card["error"].setText(
                self._percent(
                    channel.error_ratio
                )
            )
            card["unsettled"].setText(
                self._percent(
                    channel.unsettled_ratio
                )
            )
            card["repeated"].setText(
                self._percent(
                    channel.repeated_ratio
                )
            )
            card["saturated"].setText(
                self._percent(
                    channel.saturated_ratio
                )
            )
            card["id_mismatch"].setText(
                self._percent(
                    channel.channel_id_mismatch_ratio
                )
            )

            card["reason"].setText(
                channel.reason
            )

        if any(
            state == "BAD"
            for state in states
        ):
            overall = "BAD"
        elif any(
            state == "WARN"
            for state in states
        ):
            overall = "WARN"
        elif all(
            state == "GOOD"
            for state in states
        ):
            overall = "GOOD"
        else:
            overall = "WAITING"

        # Global bulk-stream failures can escalate overall state.
        if (
            q.malformed_frames > 0
            or q.channel_id_mismatches > 0
        ):
            overall = "BAD"
        elif (
            overall == "GOOD"
            and (
                q.dropped_frames > 0
                or q.sequence_resets > 0
            )
        ):
            overall = "WARN"

        self._set_state_label(
            self.overall_state_label,
            overall,
            overall=True,
        )

    def _update_bulk_panel(
        self,
    ):
        q = self.latest_quality
        if q is None:
            return

        self.bulk_frames.setText(
            f"Frames received: {q.frames_received:,}"
        )
        self.bulk_drops.setText(
            f"Dropped frames: {q.dropped_frames:,}"
        )
        self.bulk_resets.setText(
            f"Sequence resets: {q.sequence_resets:,}"
        )
        self.bulk_malformed.setText(
            f"Malformed frames: {q.malformed_frames:,}"
        )
        self.bulk_sync.setText(
            f"CH-ID mismatches: {q.channel_id_mismatches:,}"
        )
        self.bulk_errors.setText(
            f"ERROR words: {q.error_flag_words:,}"
        )
        self.bulk_unsettled.setText(
            f"Unsettled words: {q.filter_not_settled_words:,}"
        )
        self.bulk_repeated.setText(
            f"Repeated words: {q.repeated_words:,}"
        )
        self.bulk_saturated.setText(
            f"Saturated words: {q.saturated_words:,}"
        )

    def _update_trend_plots(
        self,
    ):
        if len(
            self.trend_times
        ) < 2:
            return

        times = np.asarray(
            self.trend_times,
            dtype=np.float64,
        )

        x = (
            times
            - times[-1]
        )

        history_s = float(
            self.current_history_s()
        )

        for plot in (
            self.rms_plot,
            self.ptp_plot,
            self.dc_plot,
        ):
            plot.setXRange(
                -history_s,
                0.0,
                padding=0.0,
            )

        for index in range(3):
            rms = np.asarray(
                self.trend_rms[
                    index
                ],
                dtype=np.float64,
            )
            ptp = np.asarray(
                self.trend_ptp[
                    index
                ],
                dtype=np.float64,
            )
            dc = np.asarray(
                self.trend_dc[
                    index
                ],
                dtype=np.float64,
            )

            n = min(
                len(x),
                len(rms),
                len(ptp),
                len(dc),
            )

            if n <= 0:
                continue

            self.rms_curves[
                index
            ].setData(
                x[-n:],
                rms[-n:],
            )

            self.ptp_curves[
                index
            ].setData(
                x[-n:],
                ptp[-n:],
            )

            self.dc_curves[
                index
            ].setData(
                x[-n:],
                dc[-n:],
            )

    def render_frame(self):
        q = self.latest_quality

        if q is not None:
            self._update_channel_cards()
            self._update_bulk_panel()
            self._update_trend_plots()

            self.worker_label.setText(
                f"Metric {self.current_metric_rate_hz()} Hz | "
                f"window {q.analysis_window_s:g} s "
                f"≈{q.analysis_sample_count:,} samples | "
                f"Fs {q.effective_sample_rate_hz:.1f} Hz "
                f"(raw {q.raw_sample_rate_hz:.1f}/N{q.decimation_samples}) | "
                f"compute {q.compute_ms:.2f} ms | "
                f"producer {q.producer_rate_hz:.1f} Hz"
            )

        self._update_render_metrics()

    def _update_render_metrics(self):
        now_ns = (
            time.perf_counter_ns()
        )

        if self._last_render_ns is not None:
            dt_ms = (
                now_ns
                - self._last_render_ns
            ) / 1_000_000.0

            target_ms = (
                1000.0
                / self.current_gui_fps()
            )

            jitter = abs(
                dt_ms
                - target_ms
            )

            self.render_jitter_ms = (
                0.90
                * self.render_jitter_ms
                + 0.10
                * jitter
            )

        self._last_render_ns = (
            now_ns
        )

        self._frame_count += 1

        now = (
            time.perf_counter()
        )

        elapsed = (
            now
            - self._fps_start
        )

        if elapsed >= 0.75:
            self.render_fps = (
                self._frame_count
                / elapsed
            )
            self._frame_count = 0
            self._fps_start = now

    # ------------------------------------------------------------------ status

    def refresh_status(self):
        try:
            telemetry = (
                self.shared.read_telemetry()
            )

            stream_info = (
                self.shared.read_adc_stream_info()
            )

            self.raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self.effective_sample_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self.decimation_samples = max(
                1,
                int(
                    stream_info.decimation_samples
                ),
            )
            self.decimation_mode = str(
                stream_info.decimation_mode
            )
            self.adc_session_id = int(
                stream_info.adc_session_id
            )

            self.connection_label.setText(
                "Shared RAM: DATA CONNECTED"
                if telemetry.data_connected
                else "Shared RAM: DATA NOT CONNECTED"
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
                "Quality metrics are computed in a background QThread.\n"
                "Trend plots share one PyQtGraph/OpenGL viewport.\n"
                f"ADC stream: raw {self.raw_sample_rate_hz:.3f} Hz / "
                f"N={self.decimation_samples} -> "
                f"{self.effective_sample_rate_hz:.3f} Hz effective.\n"
                "Zero-crossing and analysis-window timing use effective Fs."
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
    ):
        try:
            self.render_timer.stop()
            self.status_timer.stop()
        except Exception:
            pass

        try:
            self.worker.stop()
            self.worker.wait(2500)
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
    font.setPointSize(9)
    app.setFont(font)

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
            "Required package(s) missing:\n\n"
            + ", ".join(missing),
        )
        return 1

    try:
        window = (
            GeophoneQualityWindow()
        )
    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Geophone Quality:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
