"""
geophone_psd.py
===============

GRC-UGM-PERTAMINA OBS
Real-Time Power Spectral Density / Noise Floor Monitor

Version: 2
Shared data: shared_data_v5.py

Purpose
-------
Display calibrated-in-frequency PSD estimates for:
    CH0 = Geophone X
    CH1 = Geophone Y
    CH2 = Geophone Z

Processing
----------
Welch PSD is calculated independently for X/Y/Z:

    - optional DC removal
    - Hann window
    - configurable segment length
    - configurable overlap
    - averaging over the latest analysis window
    - one-sided PSD in count^2/Hz
    - display in dB re 1 count^2/Hz

Per-channel metrics:
    - broadband RMS from PSD integration
    - selected-band RMS
    - median noise floor in selected band
    - peak PSD frequency in selected band
    - peak PSD level

The physical frequency axis and Welch normalization use the authoritative
effective ADC sample rate published by shared_data_v5.

Example:
    raw ADC rate       = 1000 Hz
    Average N          = 5
    effective PSD rate = 200 Hz
    Nyquist            = 100 Hz

Network arrival rate is NOT used as the signal sampling frequency. Measured
producer throughput is shown only as a stream-health diagnostic.

Performance
-----------
- Shared RAM is copied only when new ADC data exists.
- PSD calculation runs in a dedicated QThread.
- GUI renders cached PSD arrays.
- PyQtGraph uses one GraphicsLayoutWidget and one OpenGL viewport.
- NumPy FFT is always available.
- CuPy/cuFFT is used when available and selected.
- v2 recalculates analysis-window sample count, Nyquist, Welch frequency bins,
  and count^2/Hz normalization from the effective shared-stream rate.
- ADC session / decimation changes clear old PSD and noise-floor history so
  spectra calibrated with different sample rates are never mixed.

Dependencies
------------
    pip install PySide6 numpy pyqtgraph

Optional CUDA:
    pip install cupy-cuda12x
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.PSD"
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
# NumPy / PyQtGraph / optional CuPy
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
    import cupy as cp
except Exception:
    cp = None


# =============================================================================
# Shared data
# =============================================================================

from shared_data_v5 import RAW_ADC_SAMPLE_RATE_HZ, OBSSharedData


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Geophone PSD / Noise Floor"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

CHANNELS = (
    ("CH0", "Geophone X", "ch0", "#FF5E5E"),
    ("CH1", "Geophone Y", "ch1", "#5FE07B"),
    ("CH2", "Geophone Z", "ch2", "#5C96FF"),
)

SEGMENT_CHOICES = (
    256,
    512,
    1024,
    2048,
    4096,
)

DEFAULT_SEGMENT = 1024
DEFAULT_OVERLAP_PERCENT = 50

ANALYSIS_WINDOW_CHOICES_S = (
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
)

DEFAULT_ANALYSIS_WINDOW_S = 10.0

PSD_UPDATE_RATE_CHOICES_HZ = (
    1,
    2,
    5,
    10,
)

DEFAULT_PSD_UPDATE_RATE_HZ = 5

GUI_FPS_CHOICES = (
    15,
    30,
    45,
    60,
)

DEFAULT_GUI_FPS = 30

NOISE_HISTORY_CHOICES_S = (
    30,
    60,
    120,
    300,
)

DEFAULT_NOISE_HISTORY_S = 60

DEFAULT_FREQ_MIN_HZ = 0.5
DEFAULT_FREQ_VIEW_MAX_HZ = 100.0

DEFAULT_BAND_MIN_HZ = 1.0
DEFAULT_BAND_VIEW_MAX_HZ = 100.0

# Measured producer rate is diagnostic only; calibration uses effective Fs.
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0

EPS_PSD = 1.0e-30


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
class PSDSettings:
    segment_length: int
    overlap_percent: int
    analysis_window_s: float
    update_rate_hz: int
    remove_dc: bool
    backend: str


@dataclass(frozen=True)
class PSDMetrics:
    broadband_rms: float
    band_rms: float
    noise_floor_db: float
    peak_frequency_hz: float
    peak_level_db: float


@dataclass(frozen=True)
class PSDSnapshot:
    timestamp_monotonic: float
    total_samples: int

    raw_sample_rate_hz: float
    effective_sample_rate_hz: float
    decimation_samples: int
    decimation_mode: str
    adc_session_id: int

    analysis_window_s: float
    analysis_sample_count: int
    segment_length: int
    frequency_resolution_hz: float

    frequency_hz: object
    psd_db: tuple
    psd_linear: tuple
    compute_ms: float
    backend: str
    producer_rate_hz: float


# =============================================================================
# Welch PSD worker
# =============================================================================


class PSDWorkerThread(QThread):
    psd_ready = Signal(object)
    worker_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._stop_event = threading.Event()
        self._settings_lock = threading.Lock()

        self._settings = PSDSettings(
            segment_length=DEFAULT_SEGMENT,
            overlap_percent=DEFAULT_OVERLAP_PERCENT,
            analysis_window_s=DEFAULT_ANALYSIS_WINDOW_S,
            update_rate_hz=DEFAULT_PSD_UPDATE_RATE_HZ,
            remove_dc=True,
            backend="Auto",
        )

        self._last_total = -1
        self._last_compute_time = 0.0

        self._rate_history = []
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
        settings: PSDSettings,
    ) -> None:
        with self._settings_lock:
            self._settings = settings

    def _get_settings(self) -> PSDSettings:
        with self._settings_lock:
            return self._settings

    def _resolve_backend(
        self,
        requested: str,
    ) -> str:
        requested = str(requested)

        if requested == "CPU / NumPy":
            return "CPU / NumPy"

        if requested == "CUDA / CuPy":
            if cp is not None:
                return "CUDA / CuPy"
            return "CPU / NumPy"

        if cp is not None:
            try:
                if cp.cuda.runtime.getDeviceCount() > 0:
                    return "CUDA / CuPy"
            except Exception:
                pass

        return "CPU / NumPy"

    @staticmethod
    def _producer_rate(
        history,
        current_rate: float,
        expected_rate_hz: float,
    ):
        if len(history) < 2:
            return current_rate

        t0, n0 = history[0]
        t1, n1 = history[-1]

        dt = t1 - t0
        dn = n1 - n0

        if dt < 1.0 or dn <= 0:
            return current_rate

        measured = dn / dt

        expected_rate_hz = max(
            0.001,
            float(expected_rate_hz),
        )
        min_rate = max(
            0.001,
            expected_rate_hz
            * PRODUCER_RATE_MIN_RATIO,
        )
        max_rate = max(
            min_rate * 2.0,
            expected_rate_hz
            * PRODUCER_RATE_MAX_RATIO,
        )

        if min_rate <= measured <= max_rate:
            return (
                0.80 * current_rate
                + 0.20 * measured
            )

        return current_rate

    @staticmethod
    def _welch_numpy(
        signals,
        fs: float,
        nperseg: int,
        overlap_percent: int,
        remove_dc: bool,
    ):
        data = np.asarray(
            signals,
            dtype=np.float64,
        )

        channels, count = data.shape

        nperseg = int(
            min(
                max(16, nperseg),
                count,
            )
        )

        overlap = int(
            round(
                nperseg
                * overlap_percent
                / 100.0
            )
        )
        overlap = min(
            overlap,
            nperseg - 1,
        )

        step = max(
            1,
            nperseg - overlap,
        )

        starts = np.arange(
            0,
            count - nperseg + 1,
            step,
            dtype=np.int64,
        )

        if len(starts) == 0:
            starts = np.array(
                [count - nperseg],
                dtype=np.int64,
            )

        window = np.hanning(
            nperseg
        ).astype(
            np.float64,
            copy=False,
        )

        window_power = float(
            np.sum(
                window * window
            )
        )

        scale = (
            fs * window_power
        )

        psd_sum = np.zeros(
            (
                channels,
                nperseg // 2 + 1,
            ),
            dtype=np.float64,
        )

        for start in starts:
            segment = data[
                :,
                start:
                start + nperseg,
            ]

            if remove_dc:
                segment = (
                    segment
                    - np.mean(
                        segment,
                        axis=1,
                        keepdims=True,
                    )
                )

            segment = (
                segment
                * window[None, :]
            )

            spectrum = np.fft.rfft(
                segment,
                axis=1,
            )

            power = (
                np.abs(
                    spectrum
                ) ** 2
            ) / scale

            if nperseg % 2 == 0:
                if power.shape[1] > 2:
                    power[
                        :,
                        1:-1,
                    ] *= 2.0
            else:
                if power.shape[1] > 1:
                    power[
                        :,
                        1:,
                    ] *= 2.0

            psd_sum += power

        psd = (
            psd_sum
            / max(
                1,
                len(starts),
            )
        )

        freq = np.fft.rfftfreq(
            nperseg,
            d=1.0 / fs,
        )

        return (
            freq,
            psd,
        )

    @staticmethod
    def _welch_cupy(
        signals,
        fs: float,
        nperseg: int,
        overlap_percent: int,
        remove_dc: bool,
    ):
        data_np = np.asarray(
            signals,
            dtype=np.float32,
        )

        channels, count = (
            data_np.shape
        )

        nperseg = int(
            min(
                max(16, nperseg),
                count,
            )
        )

        overlap = int(
            round(
                nperseg
                * overlap_percent
                / 100.0
            )
        )
        overlap = min(
            overlap,
            nperseg - 1,
        )

        step = max(
            1,
            nperseg - overlap,
        )

        starts = list(
            range(
                0,
                count - nperseg + 1,
                step,
            )
        )

        if not starts:
            starts = [
                count - nperseg
            ]

        data = cp.asarray(
            data_np
        )

        # Build the window in NumPy for compatibility across CuPy versions.
        window = cp.asarray(
            np.hanning(
                nperseg
            ).astype(
                np.float32
            )
        )

        window_power = cp.sum(
            window * window
        )

        psd_sum = cp.zeros(
            (
                channels,
                nperseg // 2 + 1,
            ),
            dtype=cp.float32,
        )

        for start in starts:
            segment = data[
                :,
                start:
                start + nperseg,
            ]

            if remove_dc:
                segment = (
                    segment
                    - cp.mean(
                        segment,
                        axis=1,
                        keepdims=True,
                    )
                )

            segment = (
                segment
                * window[None, :]
            )

            spectrum = cp.fft.rfft(
                segment,
                axis=1,
            )

            power = (
                cp.abs(
                    spectrum
                ) ** 2
            ) / (
                fs * window_power
            )

            if nperseg % 2 == 0:
                if power.shape[1] > 2:
                    power[
                        :,
                        1:-1,
                    ] *= 2.0
            else:
                if power.shape[1] > 1:
                    power[
                        :,
                        1:,
                    ] *= 2.0

            psd_sum += power

        psd = (
            psd_sum
            / max(
                1,
                len(starts),
            )
        )

        freq = np.fft.rfftfreq(
            nperseg,
            d=1.0 / fs,
        )

        return (
            freq,
            cp.asnumpy(
                psd
            ),
        )

    def run(self) -> None:
        shared = None

        try:
            shared = OBSSharedData()

            stream_info = shared.read_adc_stream_info()
            self._effective_rate_hz = max(
                0.001,
                float(stream_info.effective_sample_rate_hz),
            )
            self._producer_rate_hz = self._effective_rate_hz
            self._adc_session_id = int(
                stream_info.adc_session_id
            )

            while not self._stop_event.is_set():
                settings = self._get_settings()

                stream_info = shared.read_adc_stream_info()
                effective_rate_hz = max(
                    0.001,
                    float(stream_info.effective_sample_rate_hz),
                )
                session_id = int(
                    stream_info.adc_session_id
                )

                stream_changed = (
                    session_id != self._adc_session_id
                    or abs(
                        effective_rate_hz
                        - self._effective_rate_hz
                    )
                    > max(
                        1.0e-9,
                        1.0e-6 * effective_rate_hz,
                    )
                )

                if stream_changed:
                    self._adc_session_id = session_id
                    self._effective_rate_hz = effective_rate_hz
                    self._producer_rate_hz = effective_rate_hz
                    self._rate_history.clear()
                    self._last_total = -1
                    self._last_compute_time = 0.0

                now = time.perf_counter()
                total = shared.adc_total_samples()

                if total != self._last_total:
                    self._rate_history.append(
                        (
                            now,
                            int(total),
                        )
                    )

                    cutoff = (
                        now
                        - 5.0
                    )

                    self._rate_history = [
                        item
                        for item
                        in self._rate_history
                        if item[0]
                        >= cutoff
                    ]

                    self._producer_rate_hz = (
                        self._producer_rate(
                            self._rate_history,
                            self._producer_rate_hz,
                            effective_rate_hz,
                        )
                    )

                    self._last_total = int(
                        total
                    )

                min_period = (
                    1.0
                    / max(
                        1,
                        settings.update_rate_hz,
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

                required = max(
                    settings.segment_length,
                    int(
                        round(
                            settings.analysis_window_s
                            * effective_rate_hz
                        )
                    ),
                )

                adc = shared.read_adc_latest_numpy(
                    required
                )

                # shared_data_v5 puts the authoritative effective Fs here.
                sample_rate_hz = max(
                    0.001,
                    float(adc.sample_rate_hz),
                )

                # Avoid relying on ADC snapshot __len__ implementation.
                count = len(
                    adc.ch0
                )

                if count < 16:
                    self.msleep(10)
                    continue

                signals = np.vstack(
                    (
                        adc.ch0,
                        adc.ch1,
                        adc.ch2,
                    )
                ).astype(
                    np.float64,
                    copy=False,
                )

                backend = self._resolve_backend(
                    settings.backend
                )

                compute_start = (
                    time.perf_counter()
                )

                if (
                    backend
                    == "CUDA / CuPy"
                ):
                    try:
                        (
                            frequency_hz,
                            psd,
                        ) = self._welch_cupy(
                            signals,
                            float(sample_rate_hz),
                            settings.segment_length,
                            settings.overlap_percent,
                            settings.remove_dc,
                        )
                    except Exception:
                        backend = (
                            "CPU / NumPy"
                        )
                        (
                            frequency_hz,
                            psd,
                        ) = self._welch_numpy(
                            signals,
                            float(sample_rate_hz),
                            settings.segment_length,
                            settings.overlap_percent,
                            settings.remove_dc,
                        )
                else:
                    (
                        frequency_hz,
                        psd,
                    ) = self._welch_numpy(
                        signals,
                        float(sample_rate_hz),
                        settings.segment_length,
                        settings.overlap_percent,
                        settings.remove_dc,
                    )

                compute_ms = (
                    time.perf_counter()
                    - compute_start
                ) * 1000.0

                psd = np.maximum(
                    psd,
                    EPS_PSD,
                )

                psd_db = (
                    10.0
                    * np.log10(
                        psd
                    )
                )

                frequency_resolution_hz = (
                    float(
                        frequency_hz[1]
                        - frequency_hz[0]
                    )
                    if len(frequency_hz) > 1
                    else 0.0
                )

                snapshot = PSDSnapshot(
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
                        int(stream_info.decimation_samples),
                    ),
                    decimation_mode=str(
                        stream_info.decimation_mode
                    ),
                    adc_session_id=int(
                        stream_info.adc_session_id
                    ),
                    analysis_window_s=float(
                        settings.analysis_window_s
                    ),
                    analysis_sample_count=int(
                        count
                    ),
                    segment_length=int(
                        min(
                            max(16, settings.segment_length),
                            count,
                        )
                    ),
                    frequency_resolution_hz=float(
                        frequency_resolution_hz
                    ),
                    frequency_hz=(
                        frequency_hz.astype(
                            np.float64,
                            copy=False,
                        )
                    ),
                    psd_db=(
                        psd_db[0],
                        psd_db[1],
                        psd_db[2],
                    ),
                    psd_linear=(
                        psd[0],
                        psd[1],
                        psd[2],
                    ),
                    compute_ms=float(
                        compute_ms
                    ),
                    backend=backend,
                    producer_rate_hz=float(
                        self._producer_rate_hz
                    ),
                )

                self.psd_ready.emit(
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


class GeophonePSDWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        if np is None or pg is None:
            raise RuntimeError(
                "Geophone PSD requires NumPy and PyQtGraph."
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
            stream_info = self.shared.read_adc_stream_info()
            self.raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self.effective_sample_rate_hz = max(
                0.001,
                float(stream_info.effective_sample_rate_hz),
            )
            self.decimation_samples = max(
                1,
                int(stream_info.decimation_samples),
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

        self.latest_psd: Optional[
            PSDSnapshot
        ] = None

        self.channel_metrics = [
            PSDMetrics(
                0.0,
                0.0,
                float("nan"),
                float("nan"),
                float("nan"),
            )
            for _ in range(3)
        ]

        self.noise_history_t = []
        self.noise_history = [
            [],
            [],
            [],
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

        self.psd_curves = []
        self.noise_curves = []
        self.metric_labels = []

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
            920,
        )
        self.setMinimumSize(
            1100,
            720,
        )

        self._configure_pyqtgraph()
        self._build_ui()
        self._apply_style()

        self.worker = PSDWorkerThread(
            self
        )
        self.worker.psd_ready.connect(
            self.on_psd_snapshot
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
            500
        )

        self.refresh_status()

    # ------------------------------------------------------------------ graph config

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
    ):
        if QOpenGLWidget is None:
            self.opengl_error = (
                "QOpenGLWidget unavailable"
            )
            return

        try:
            viewport = (
                QOpenGLWidget()
            )

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
            "GEOPHONE PSD / NOISE FLOOR"
        )
        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            "Welch PSD  •  X/Y/Z Noise Floor  •  Band RMS  •  Peak Frequency"
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

        self.backend_state_label = QLabel(
            "PSD"
        )
        self.backend_state_label.setObjectName(
            "backendLabel"
        )
        self.backend_state_label.setAlignment(
            Qt.AlignCenter
        )
        self.backend_state_label.setMinimumWidth(
            150
        )

        header.addWidget(
            self.backend_state_label
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
            "PSD worker: --"
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

        splitter = QSplitter(
            Qt.Horizontal
        )
        splitter.setChildrenCollapsible(
            False
        )

        splitter.addWidget(
            self._build_plot_panel()
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
            [1120, 380]
        )

        root.addWidget(
            splitter,
            1,
        )

    def current_sample_rate_hz(self) -> float:
        return max(
            0.001,
            float(self.effective_sample_rate_hz),
        )

    def current_nyquist_hz(self) -> float:
        return (
            self.current_sample_rate_hz()
            / 2.0
        )

    def default_frequency_max_hz(self) -> float:
        return min(
            DEFAULT_FREQ_VIEW_MAX_HZ,
            self.current_nyquist_hz(),
        )

    def default_band_max_hz(self) -> float:
        return min(
            DEFAULT_BAND_VIEW_MAX_HZ,
            self.current_nyquist_hz(),
        )

    def clear_psd_history(self) -> None:
        self.latest_psd = None
        self.noise_history_t.clear()

        for series in self.noise_history:
            series.clear()

    def update_segment_labels(self) -> None:
        if not hasattr(self, "segment_combo"):
            return

        fs = self.current_sample_rate_hz()

        for index in range(
            self.segment_combo.count()
        ):
            n = int(
                self.segment_combo.itemData(index)
            )
            self.segment_combo.setItemText(
                index,
                (
                    f"{n:,} "
                    f"({n/fs:.3f} s • "
                    f"{fs/n:.4f} Hz/bin)"
                ),
            )

        if hasattr(self, "sample_info_note"):
            self.sample_info_note.setText(
                (
                    f"PSD uses effective Fs={fs:.3f} Hz "
                    f"(raw {self.raw_sample_rate_hz:.3f} Hz / "
                    f"N={self.decimation_samples}). "
                    f"Nyquist={fs/2.0:.3f} Hz. "
                    "Welch normalization is count²/Hz using this effective Fs."
                )
            )

    def apply_stream_info(
        self,
        *,
        raw_sample_rate_hz: float,
        effective_sample_rate_hz: float,
        decimation_samples: int,
        decimation_mode: str,
        adc_session_id: int,
    ) -> None:
        old_fs = float(
            self.effective_sample_rate_hz
        )
        old_session = int(
            self.adc_session_id
        )

        self.raw_sample_rate_hz = float(
            raw_sample_rate_hz
        )
        self.effective_sample_rate_hz = max(
            0.001,
            float(effective_sample_rate_hz),
        )
        self.decimation_samples = max(
            1,
            int(decimation_samples),
        )
        self.decimation_mode = str(
            decimation_mode
        )
        self.adc_session_id = int(
            adc_session_id
        )

        stream_changed = (
            old_session != self.adc_session_id
            or abs(
                old_fs
                - self.effective_sample_rate_hz
            )
            > max(
                1.0e-9,
                1.0e-6
                * self.effective_sample_rate_hz,
            )
        )

        self.update_segment_labels()

        if hasattr(self, "freq_min_spin"):
            nyquist = self.current_nyquist_hz()

            for spin in (
                self.freq_min_spin,
                self.freq_max_spin,
                self.band_min_spin,
                self.band_max_spin,
            ):
                spin.setRange(
                    0.0,
                    nyquist,
                )

            if self.freq_min_spin.value() >= nyquist:
                self.freq_min_spin.setValue(0.0)

            if (
                self.freq_max_spin.value() > nyquist
                or self.freq_max_spin.value()
                <= self.freq_min_spin.value()
            ):
                self.freq_max_spin.setValue(
                    self.default_frequency_max_hz()
                )

            if self.band_min_spin.value() >= nyquist:
                self.band_min_spin.setValue(0.0)

            if (
                self.band_max_spin.value() > nyquist
                or self.band_max_spin.value()
                <= self.band_min_spin.value()
            ):
                self.band_max_spin.setValue(
                    self.default_band_max_hz()
                )

        if stream_changed:
            self.clear_psd_history()

            if hasattr(self, "psd_plots"):
                for plot in self.psd_plots:
                    plot.setXRange(
                        0.0,
                        self.default_frequency_max_hz(),
                        padding=0.0,
                    )

    def _build_plot_panel(self):
        frame = QFrame()
        frame.setObjectName(
            "viewFrame"
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

        self.psd_plots = []

        for row, (
            channel_name,
            axis_name,
            _attr,
            color,
        ) in enumerate(
            CHANNELS
        ):
            plot = (
                self.graphics.addPlot(
                    row=row,
                    col=0,
                )
            )

            plot.setTitle(
                f"{channel_name} — {axis_name}",
                color="#FFFFFF",
                size="10pt",
            )

            plot.setLabel(
                "left",
                "PSD",
                units="dB count²/Hz",
            )

            plot.setLabel(
                "bottom",
                "Frequency",
                units="Hz",
            )

            plot.showGrid(
                x=True,
                y=True,
                alpha=0.18,
            )

            plot.setXRange(
                DEFAULT_FREQ_MIN_HZ,
                self.default_frequency_max_hz(),
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

            self.psd_plots.append(
                plot
            )
            self.psd_curves.append(
                curve
            )

        self.noise_plot = (
            self.graphics.addPlot(
                row=3,
                col=0,
            )
        )

        self.noise_plot.setTitle(
            "Selected-Band Noise Floor History",
            color="#FFFFFF",
            size="10pt",
        )

        self.noise_plot.setLabel(
            "left",
            "Median PSD",
            units="dB",
        )

        self.noise_plot.setLabel(
            "bottom",
            "History",
            units="s",
        )

        self.noise_plot.showGrid(
            x=True,
            y=True,
            alpha=0.18,
        )

        for (
            _channel_name,
            _axis_name,
            _attr,
            color,
        ) in CHANNELS:
            self.noise_curves.append(
                self.noise_plot.plot(
                    [],
                    [],
                    pen=pg.mkPen(
                        color,
                        width=1.3,
                    ),
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
            "PSD SETTINGS"
        )
        heading.setObjectName(
            "settingsTitle"
        )
        layout.addWidget(
            heading
        )

        processing = QGroupBox(
            "Welch Processing"
        )
        processing.setObjectName(
            "channelGroup"
        )

        grid = QGridLayout(
            processing
        )
        grid.setContentsMargins(
            10, 12, 10, 10
        )

        self.backend_combo = QComboBox()
        self.backend_combo.addItems(
            [
                "Auto",
                "CPU / NumPy",
                "CUDA / CuPy",
            ]
        )
        self.backend_combo.setCurrentText(
            "Auto"
        )

        self.segment_combo = QComboBox()
        for value in SEGMENT_CHOICES:
            fs = self.current_sample_rate_hz()
            self.segment_combo.addItem(
                (
                    f"{value:,} "
                    f"({value/fs:.3f} s • "
                    f"{fs/value:.4f} Hz/bin)"
                ),
                value,
            )

        default_index = self.segment_combo.findData(
            DEFAULT_SEGMENT
        )
        if default_index >= 0:
            self.segment_combo.setCurrentIndex(
                default_index
            )

        self.overlap_combo = QComboBox()
        for value in (
            0,
            25,
            50,
            75,
        ):
            self.overlap_combo.addItem(
                f"{value}%",
                value,
            )
        self.overlap_combo.setCurrentText(
            f"{DEFAULT_OVERLAP_PERCENT}%"
        )

        self.analysis_combo = QComboBox()
        for value in (
            ANALYSIS_WINDOW_CHOICES_S
        ):
            self.analysis_combo.addItem(
                f"{value:g} s",
                float(value),
            )
        self.analysis_combo.setCurrentText(
            f"{DEFAULT_ANALYSIS_WINDOW_S:g} s"
        )

        self.update_combo = QComboBox()
        for value in (
            PSD_UPDATE_RATE_CHOICES_HZ
        ):
            self.update_combo.addItem(
                f"{value} Hz",
                value,
            )
        self.update_combo.setCurrentText(
            f"{DEFAULT_PSD_UPDATE_RATE_HZ} Hz"
        )

        self.remove_dc_check = (
            QCheckBox(
                "Remove DC before Welch"
            )
        )
        self.remove_dc_check.setChecked(
            True
        )

        controls = (
            ("Backend", self.backend_combo),
            ("Segment Length", self.segment_combo),
            ("Overlap", self.overlap_combo),
            ("Analysis Window", self.analysis_combo),
            ("PSD Update", self.update_combo),
        )

        for row, (
            name,
            widget,
        ) in enumerate(
            controls
        ):
            grid.addWidget(
                QLabel(name),
                row,
                0,
            )
            grid.addWidget(
                widget,
                row,
                1,
            )

        grid.addWidget(
            self.remove_dc_check,
            len(controls),
            0,
            1,
            2,
        )

        apply_button = QPushButton(
            "Apply PSD Settings"
        )
        apply_button.setObjectName(
            "smallPrimaryButton"
        )
        apply_button.clicked.connect(
            self.apply_psd_settings
        )

        grid.addWidget(
            apply_button,
            len(controls) + 1,
            0,
            1,
            2,
        )

        layout.addWidget(
            processing
        )

        frequency = QGroupBox(
            "Frequency / Band"
        )
        frequency.setObjectName(
            "channelGroup"
        )

        fg = QGridLayout(
            frequency
        )
        fg.setContentsMargins(
            10, 12, 10, 10
        )

        nyquist = (
            self.current_nyquist_hz()
        )

        self.freq_min_spin = (
            QDoubleSpinBox()
        )
        self.freq_min_spin.setRange(
            0.0,
            nyquist,
        )
        self.freq_min_spin.setDecimals(
            2
        )
        self.freq_min_spin.setValue(
            DEFAULT_FREQ_MIN_HZ
        )
        self.freq_min_spin.setSuffix(
            " Hz"
        )

        self.freq_max_spin = (
            QDoubleSpinBox()
        )
        self.freq_max_spin.setRange(
            0.0,
            nyquist,
        )
        self.freq_max_spin.setDecimals(
            2
        )
        self.freq_max_spin.setValue(
            self.default_frequency_max_hz()
        )
        self.freq_max_spin.setSuffix(
            " Hz"
        )

        self.band_min_spin = (
            QDoubleSpinBox()
        )
        self.band_min_spin.setRange(
            0.0,
            nyquist,
        )
        self.band_min_spin.setDecimals(
            2
        )
        self.band_min_spin.setValue(
            DEFAULT_BAND_MIN_HZ
        )
        self.band_min_spin.setSuffix(
            " Hz"
        )

        self.band_max_spin = (
            QDoubleSpinBox()
        )
        self.band_max_spin.setRange(
            0.0,
            nyquist,
        )
        self.band_max_spin.setDecimals(
            2
        )
        self.band_max_spin.setValue(
            self.default_band_max_hz()
        )
        self.band_max_spin.setSuffix(
            " Hz"
        )

        fg.addWidget(
            QLabel("Display Min"),
            0,
            0,
        )
        fg.addWidget(
            self.freq_min_spin,
            0,
            1,
        )

        fg.addWidget(
            QLabel("Display Max"),
            1,
            0,
        )
        fg.addWidget(
            self.freq_max_spin,
            1,
            1,
        )

        fg.addWidget(
            QLabel("Band Min"),
            2,
            0,
        )
        fg.addWidget(
            self.band_min_spin,
            2,
            1,
        )

        fg.addWidget(
            QLabel("Band Max"),
            3,
            0,
        )
        fg.addWidget(
            self.band_max_spin,
            3,
            1,
        )

        apply_frequency = QPushButton(
            "Apply Frequency Range"
        )
        apply_frequency.setObjectName(
            "smallPrimaryButton"
        )
        apply_frequency.clicked.connect(
            self.apply_frequency_range
        )

        fg.addWidget(
            apply_frequency,
            4,
            0,
            1,
            2,
        )

        layout.addWidget(
            frequency
        )

        display = QGroupBox(
            "Display"
        )
        display.setObjectName(
            "channelGroup"
        )

        dg = QGridLayout(
            display
        )
        dg.setContentsMargins(
            10, 12, 10, 10
        )

        self.history_combo = QComboBox()
        for value in (
            NOISE_HISTORY_CHOICES_S
        ):
            self.history_combo.addItem(
                f"{value} s",
                value,
            )
        self.history_combo.setCurrentText(
            f"{DEFAULT_NOISE_HISTORY_S} s"
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

        dg.addWidget(
            QLabel("Noise History"),
            0,
            0,
        )
        dg.addWidget(
            self.history_combo,
            0,
            1,
        )

        dg.addWidget(
            QLabel("GUI FPS"),
            1,
            0,
        )
        dg.addWidget(
            self.gui_fps_combo,
            1,
            1,
        )

        layout.addWidget(
            display
        )

        metrics_group = QGroupBox(
            "Channel Metrics"
        )
        metrics_group.setObjectName(
            "channelGroup"
        )

        mg = QVBoxLayout(
            metrics_group
        )
        mg.setContentsMargins(
            10, 12, 10, 10
        )

        for (
            channel_name,
            axis_name,
            _attr,
            _color,
        ) in CHANNELS:
            label = QLabel(
                f"{channel_name} / {axis_name}\n"
                "Broadband RMS: --\n"
                "Band RMS: --\n"
                "Noise floor: --\n"
                "Peak: --"
            )
            label.setObjectName(
                "metricBlock"
            )
            label.setWordWrap(
                True
            )
            mg.addWidget(
                label
            )
            self.metric_labels.append(
                label
            )

        layout.addWidget(
            metrics_group
        )

        self.sample_info_note = QLabel("")
        self.sample_info_note.setObjectName(
            "sampleInfo"
        )
        self.sample_info_note.setWordWrap(
            True
        )

        self.update_segment_labels()

        layout.addWidget(
            self.sample_info_note
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

            QLabel#backendLabel {
                background-color: #102C3C;
                border: 1px solid #28607E;
                border-radius: 7px;
                color: #BCEAFF;
                font-weight: 800;
                padding: 5px 12px;
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

            QLabel#metricBlock {
                color: #DDEAF2;
                font-family: "Consolas";
                font-size: 10px;
                border-bottom: 1px solid #17374A;
                padding-bottom: 7px;
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

            QCheckBox {
                color: #DDE9EF;
                spacing: 6px;
            }

            QPushButton {
                min-height: 28px;
                border-radius: 6px;
                padding: 3px 8px;
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

    # ------------------------------------------------------------------ settings

    def current_settings(self):
        return PSDSettings(
            segment_length=int(
                self.segment_combo.currentData()
                or DEFAULT_SEGMENT
            ),
            overlap_percent=int(
                self.overlap_combo.currentData()
                or DEFAULT_OVERLAP_PERCENT
            ),
            analysis_window_s=float(
                self.analysis_combo.currentData()
                or DEFAULT_ANALYSIS_WINDOW_S
            ),
            update_rate_hz=int(
                self.update_combo.currentData()
                or DEFAULT_PSD_UPDATE_RATE_HZ
            ),
            remove_dc=bool(
                self.remove_dc_check.isChecked()
            ),
            backend=str(
                self.backend_combo.currentText()
            ),
        )

    def _push_worker_settings(self):
        if not hasattr(
            self,
            "worker",
        ):
            return

        self.worker.set_settings(
            self.current_settings()
        )

    def apply_psd_settings(self):
        settings = self.current_settings()

        required = int(
            round(
                settings.analysis_window_s
                * self.current_sample_rate_hz()
            )
        )

        if (
            settings.segment_length
            > required
        ):
            QMessageBox.warning(
                self,
                APP_TITLE,
                (
                    "Segment Length is longer than the selected Analysis Window "
                    f"at Fs={self.current_sample_rate_hz():.3f} Hz "
                    f"({required} samples available)."
                ),
            )
            return

        self._push_worker_settings()

    def apply_frequency_range(self):
        fmin = float(
            self.freq_min_spin.value()
        )
        fmax = float(
            self.freq_max_spin.value()
        )

        bmin = float(
            self.band_min_spin.value()
        )
        bmax = float(
            self.band_max_spin.value()
        )

        if fmin >= fmax:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Display Min must be lower than Display Max.",
            )
            return

        if bmin >= bmax:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Band Min must be lower than Band Max.",
            )
            return

        for plot in self.psd_plots:
            plot.setXRange(
                fmin,
                fmax,
                padding=0.0,
            )

        # Recalculate metrics immediately from current PSD.
        if self.latest_psd is not None:
            self._calculate_metrics(
                self.latest_psd
            )

    def current_history_s(self):
        return int(
            self.history_combo.currentData()
            or DEFAULT_NOISE_HISTORY_S
        )

    def current_gui_fps(self):
        return int(
            self.gui_fps_combo.currentData()
            or DEFAULT_GUI_FPS
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

    # ------------------------------------------------------------------ PSD metrics

    def _calculate_metrics(
        self,
        snapshot: PSDSnapshot,
    ):
        frequency = (
            snapshot.frequency_hz
        )

        if len(
            frequency
        ) < 2:
            return

        df = float(
            frequency[1]
            - frequency[0]
        )

        bmin = float(
            self.band_min_spin.value()
        )
        bmax = float(
            self.band_max_spin.value()
        )

        band_mask = (
            (frequency >= bmin)
            & (frequency <= bmax)
        )

        metrics = []

        for index in range(3):
            linear = np.asarray(
                snapshot.psd_linear[
                    index
                ],
                dtype=np.float64,
            )

            db = np.asarray(
                snapshot.psd_db[
                    index
                ],
                dtype=np.float64,
            )

            broadband_power = float(
                np.sum(
                    linear
                )
                * df
            )

            broadband_rms = math.sqrt(
                max(
                    0.0,
                    broadband_power,
                )
            )

            if np.any(
                band_mask
            ):
                band_linear = linear[
                    band_mask
                ]
                band_db = db[
                    band_mask
                ]
                band_freq = frequency[
                    band_mask
                ]

                band_power = float(
                    np.sum(
                        band_linear
                    )
                    * df
                )

                band_rms = math.sqrt(
                    max(
                        0.0,
                        band_power,
                    )
                )

                noise_floor_db = float(
                    np.median(
                        band_db
                    )
                )

                peak_index = int(
                    np.argmax(
                        band_db
                    )
                )

                peak_frequency_hz = float(
                    band_freq[
                        peak_index
                    ]
                )
                peak_level_db = float(
                    band_db[
                        peak_index
                    ]
                )
            else:
                band_rms = 0.0
                noise_floor_db = float(
                    "nan"
                )
                peak_frequency_hz = float(
                    "nan"
                )
                peak_level_db = float(
                    "nan"
                )

            metrics.append(
                PSDMetrics(
                    broadband_rms=(
                        broadband_rms
                    ),
                    band_rms=(
                        band_rms
                    ),
                    noise_floor_db=(
                        noise_floor_db
                    ),
                    peak_frequency_hz=(
                        peak_frequency_hz
                    ),
                    peak_level_db=(
                        peak_level_db
                    ),
                )
            )

        self.channel_metrics = (
            metrics
        )

    # ------------------------------------------------------------------ callbacks

    def on_psd_snapshot(
        self,
        snapshot: PSDSnapshot,
    ):
        self.apply_stream_info(
            raw_sample_rate_hz=(
                snapshot.raw_sample_rate_hz
            ),
            effective_sample_rate_hz=(
                snapshot.effective_sample_rate_hz
            ),
            decimation_samples=(
                snapshot.decimation_samples
            ),
            decimation_mode=(
                snapshot.decimation_mode
            ),
            adc_session_id=(
                snapshot.adc_session_id
            ),
        )

        self.latest_psd = snapshot

        self._calculate_metrics(
            snapshot
        )

        now = (
            snapshot.timestamp_monotonic
        )

        self.noise_history_t.append(
            now
        )

        for index in range(3):
            self.noise_history[
                index
            ].append(
                self.channel_metrics[
                    index
                ].noise_floor_db
            )

        cutoff = (
            now
            - max(
                NOISE_HISTORY_CHOICES_S
            )
            - 5.0
        )

        while (
            self.noise_history_t
            and self.noise_history_t[0]
            < cutoff
        ):
            self.noise_history_t.pop(
                0
            )

            for series in (
                self.noise_history
            ):
                if series:
                    series.pop(
                        0
                    )

    def on_worker_error(
        self,
        message: str,
    ):
        self.worker_label.setText(
            f"PSD worker error: {message}"
        )

    # ------------------------------------------------------------------ render

    def _render_psd(self):
        snapshot = self.latest_psd

        if snapshot is None:
            return

        frequency = (
            snapshot.frequency_hz
        )

        if len(
            frequency
        ) == 0:
            return

        fmin = float(
            self.freq_min_spin.value()
        )
        fmax = float(
            self.freq_max_spin.value()
        )

        mask = (
            (frequency >= fmin)
            & (frequency <= fmax)
        )

        if not np.any(
            mask
        ):
            return

        for index in range(3):
            self.psd_curves[
                index
            ].setData(
                frequency[
                    mask
                ],
                np.asarray(
                    snapshot.psd_db[
                        index
                    ]
                )[
                    mask
                ],
            )

    def _render_noise_history(self):
        if len(
            self.noise_history_t
        ) < 2:
            return

        times = np.asarray(
            self.noise_history_t,
            dtype=np.float64,
        )

        now = times[-1]

        history = float(
            self.current_history_s()
        )

        cutoff = (
            now - history
        )

        start = int(
            np.searchsorted(
                times,
                cutoff,
                side="left",
            )
        )

        times = times[
            start:
        ]

        x = (
            times - now
        )

        self.noise_plot.setXRange(
            -history,
            0.0,
            padding=0.0,
        )

        all_values = []

        for index in range(3):
            values = np.asarray(
                self.noise_history[
                    index
                ],
                dtype=np.float64,
            )[
                start:
            ]

            self.noise_curves[
                index
            ].setData(
                x,
                values,
            )

            finite = values[
                np.isfinite(
                    values
                )
            ]

            if len(
                finite
            ):
                all_values.append(
                    finite
                )

        if all_values:
            values = np.concatenate(
                all_values
            )

            low = float(
                np.percentile(
                    values,
                    2.0,
                )
            )
            high = float(
                np.percentile(
                    values,
                    98.0,
                )
            )

            margin = max(
                1.0,
                (
                    high - low
                )
                * 0.15,
            )

            self.noise_plot.setYRange(
                low - margin,
                high + margin,
                padding=0.0,
            )

    def _update_metric_labels(self):
        bmin = float(
            self.band_min_spin.value()
        )
        bmax = float(
            self.band_max_spin.value()
        )

        for index, metric in enumerate(
            self.channel_metrics
        ):
            (
                channel_name,
                axis_name,
                _attr,
                _color,
            ) = CHANNELS[
                index
            ]

            self.metric_labels[
                index
            ].setText(
                f"{channel_name} / {axis_name}\n"
                f"Broadband RMS: {metric.broadband_rms:,.2f} count\n"
                f"Band RMS {bmin:g}-{bmax:g} Hz: {metric.band_rms:,.2f} count\n"
                f"Noise floor: {metric.noise_floor_db:.2f} dB count²/Hz\n"
                f"Peak: {metric.peak_frequency_hz:.2f} Hz @ {metric.peak_level_db:.2f} dB"
            )

    def render_frame(self):
        self._render_psd()
        self._render_noise_history()
        self._update_metric_labels()

        if self.latest_psd is not None:
            snapshot = self.latest_psd

            self.backend_state_label.setText(
                snapshot.backend
            )

            self.worker_label.setText(
                f"{snapshot.backend} | "
                f"Fs {snapshot.effective_sample_rate_hz:.1f} Hz "
                f"(raw {snapshot.raw_sample_rate_hz:.1f}/"
                f"N{snapshot.decimation_samples}) | "
                f"Nseg {snapshot.segment_length:,} | "
                f"Δf {snapshot.frequency_resolution_hz:.4f} Hz | "
                f"window {snapshot.analysis_window_s:g}s "
                f"≈{snapshot.analysis_sample_count:,} samp | "
                f"compute {snapshot.compute_ms:.2f} ms | "
                f"producer {snapshot.producer_rate_hz:.1f} Hz"
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
                dt_ms - target_ms
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

            self.apply_stream_info(
                raw_sample_rate_hz=(
                    stream_info.raw_sample_rate_hz
                ),
                effective_sample_rate_hz=(
                    stream_info.effective_sample_rate_hz
                ),
                decimation_samples=(
                    stream_info.decimation_samples
                ),
                decimation_mode=(
                    stream_info.decimation_mode
                ),
                adc_session_id=(
                    stream_info.adc_session_id
                ),
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
                f"PSD effective Fs = {self.effective_sample_rate_hz:.3f} Hz "
                f"(raw {self.raw_sample_rate_hz:.3f} Hz / "
                f"N={self.decimation_samples}).\n"
                f"Nyquist = {self.current_nyquist_hz():.3f} Hz.\n"
                "Welch frequency axis and count²/Hz normalization use effective Fs.\n"
                "Network packet arrival rate is not used as the signal sample clock."
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
            self.worker.wait(
                3000
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
            + ", ".join(
                missing
            ),
        )
        return 1

    try:
        window = (
            GeophonePSDWindow()
        )

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Geophone PSD:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
