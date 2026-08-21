"""
geophone_fft.py
===============

GRC-UGM-PERTAMINA OBS
Real-Time FFT Spectrum — Geophone X / Y / Z

Version: 4
Shared data: shared_data_v5.py

Architecture
------------
The FFT display follows the same low-jitter architecture as
geophone_realtime_v9.py:

1. Shared RAM is read in a dedicated QThread.
2. FFT computation runs OUTSIDE the GUI thread.
3. CUDA/CuPy is preferred automatically when available.
   CuPy FFT uses NVIDIA CUDA/cuFFT, so the numerical FFT workload is executed
   on the GeForce RTX rather than Intel UHD.
4. NumPy FFT is retained as a safe CPU fallback.
5. The three spectra share ONE PyQtGraph GraphicsLayoutWidget and ONE
   QOpenGLWidget viewport.
6. The spectrum result may arrive only when fresh ADC data exists, but the GUI
   renders at 60 FPS and smoothly blends toward the newest spectrum so the
   display does not visually jump at the OBS 128-sample bulk cadence.
7. Pause freezes only the display. ADC acquisition and background FFT may
   continue.

ADC mapping
-----------
    CH0 = Geophone X
    CH1 = Geophone Y
    CH2 = Geophone Z
    CH3 = auxiliary / not displayed here

FFT frequency calibration
-------------------------
The physical frequency axis uses the authoritative EFFECTIVE ADC sample rate
published by shared_data_v5.

Example:
    raw ADC rate       = 1000 Hz
    decimation samples = 5
    effective rate     = 200 Hz
    FFT Nyquist        = 100 Hz

Network/bulk-packet arrival rate is NOT used as the FFT sample rate.

FFT responsiveness
------------------
A large FFT size also means a long analysis-history window. At effective
Fs=200 Hz:
    NFFT 4096 -> 20.48 s of signal history
    NFFT 1024 ->  5.12 s
    NFFT  256 ->  1.28 s
    NFFT  128 ->  0.64 s

Therefore v3 added 128/256-point FFT choices. Version 4 keeps the fast
NFFT=256 default but disables temporal spectrum smoothing by default so an old
tone is not visually blended with a newly selected generator frequency.

Important FFT limitation:
    shorter time window -> faster response but wider spectral main lobe
    longer time window  -> sharper spectral peak but slower response

Version 4 also adds parabolic/sub-bin peak interpolation plus a vertical peak
marker. The raw FFT curve remains physically honest; the marker gives a sharp,
fast frequency indication without pretending that a short FFT has the
resolution of a long FFT.

Amplitude
---------
Default display is single-sided amplitude in:
    dB re 1 ADC count

The FFT is normalized by the coherent gain of the selected window:
    amplitude = 2 * abs(rFFT(windowed_signal)) / sum(window)

DC and Nyquist bins are not doubled.

Dependencies
------------
Required:
    PySide6
    numpy
    pyqtgraph

NVIDIA GPU FFT:
    CuPy compatible with the installed NVIDIA CUDA environment.

If CuPy/CUDA is unavailable the program automatically falls back to NumPy CPU
FFT and remains functional.
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
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.FFT"

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

        # ABOVE_NORMAL_PRIORITY_CLASS
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            0x00008000,
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
    QScrollArea,
    QSpinBox,
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

from shared_data_v5 import (
    RAW_ADC_SAMPLE_RATE_HZ,
    OBSSharedData,
)


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Geophone FFT"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

CHANNELS = (
    ("CH0", "Geophone X", "ch0"),
    ("CH1", "Geophone Y", "ch1"),
    ("CH2", "Geophone Z", "ch2"),
)

DEFAULT_RENDER_FPS = 60

# Worker checks for a fresh ADC snapshot at this cadence. Actual FFT result
# cadence is naturally limited by how often new shared ADC samples arrive.
DEFAULT_FFT_UPDATE_HZ = 20

FFT_SIZES = (
    128,
    256,
    512,
    1024,
    2048,
    4096,
    8192,
    16384,
)

# Fast-response default. At Fs=200 Hz this is a 1.28-second analysis window.
DEFAULT_FFT_SIZE = 256

WINDOW_TYPES = (
    "Hann",
    "Hamming",
    "Blackman",
    "Rectangular",
)

DEFAULT_WINDOW = "Hann"

DEFAULT_FREQ_MIN = 0.0

# Project analysis focus is 0-100 Hz. The actual upper limit is always clamped
# to the effective Nyquist frequency at runtime.
DEFAULT_FREQ_VIEW_MAX_HZ = 100.0

DEFAULT_DB_MIN = -20.0
DEFAULT_DB_MAX = 140.0

DEFAULT_SPECTRUM_SMOOTH_MS = 0

SPECTRUM_SMOOTH_CHOICES_MS = (
    0,
    100,
    250,
    500,
    1000,
    2000,
)

FFT_UPDATE_CHOICES_HZ = (
    2,
    5,
    8,
    10,
    15,
    20,
    30,
)

RENDER_FPS_CHOICES = (
    30,
    45,
    60,
    75,
    90,
    120,
)

MAX_RENDER_BINS = 6000
STATUS_INTERVAL_MS = 500
PEAK_LABEL_INTERVAL_S = 0.10

EPSILON = 1.0e-20


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


def build_numpy_window(
    window_name: str,
    fft_size: int,
):
    name = str(
        window_name
    ).strip().lower()

    if name == "hann":
        return np.hanning(
            fft_size
        ).astype(
            np.float32,
            copy=False,
        )

    if name == "hamming":
        return np.hamming(
            fft_size
        ).astype(
            np.float32,
            copy=False,
        )

    if name == "blackman":
        return np.blackman(
            fft_size
        ).astype(
            np.float32,
            copy=False,
        )

    return np.ones(
        fft_size,
        dtype=np.float32,
    )


def single_sided_amplitude_numpy(
    spectrum,
    window_sum: float,
    fft_size: int,
):
    amplitude = (
        np.abs(
            spectrum
        ).astype(
            np.float32,
            copy=False,
        )
        / max(
            float(
                window_sum
            ),
            EPSILON,
        )
    )

    if amplitude.shape[-1] > 1:
        amplitude[..., 1:] *= 2.0

        if (
            fft_size % 2 == 0
            and amplitude.shape[-1] >= 2
        ):
            amplitude[..., -1] *= 0.5

    return amplitude



def interpolate_fft_peak(
    frequency_hz,
    spectrum_db,
    index: int,
) -> tuple[float, float]:
    """
    Three-bin parabolic interpolation around the discrete FFT maximum.

    This improves dominant-tone frequency estimation without changing or
    artificially narrowing the plotted FFT spectrum.
    """

    index = int(
        index
    )

    count = len(
        spectrum_db
    )

    if (
        count < 3
        or index <= 0
        or index >= count - 1
    ):
        return (
            float(
                frequency_hz[
                    index
                ]
            ),
            float(
                spectrum_db[
                    index
                ]
            ),
        )

    y_left = float(
        spectrum_db[
            index - 1
        ]
    )
    y_center = float(
        spectrum_db[
            index
        ]
    )
    y_right = float(
        spectrum_db[
            index + 1
        ]
    )

    denominator = (
        y_left
        - 2.0 * y_center
        + y_right
    )

    if (
        not np.isfinite(
            denominator
        )
        or abs(
            denominator
        ) < 1.0e-12
    ):
        return (
            float(
                frequency_hz[
                    index
                ]
            ),
            y_center,
        )

    delta = (
        0.5
        * (
            y_left
            - y_right
        )
        / denominator
    )

    delta = max(
        -0.5,
        min(
            0.5,
            float(
                delta
            ),
        ),
    )

    bin_hz = float(
        frequency_hz[
            index + 1
        ]
        - frequency_hz[
            index
        ]
    )

    peak_frequency_hz = (
        float(
            frequency_hz[
                index
            ]
        )
        + delta
        * bin_hz
    )

    peak_db = (
        y_center
        - 0.25
        * (
            y_left
            - y_right
        )
        * delta
    )

    return (
        float(
            peak_frequency_hz
        ),
        float(
            peak_db
        ),
    )


def detect_cupy() -> tuple[
    bool,
    str,
    int,
]:
    """
    Lightweight CUDA/CuPy detection.

    Returns:
        available, description, device_count
    """

    try:
        import cupy as cp

        count = int(
            cp.cuda.runtime.getDeviceCount()
        )

        if count <= 0:
            return (
                False,
                "CuPy installed, no CUDA device",
                0,
            )

        names = []

        for device_id in range(
            count
        ):
            props = (
                cp.cuda.runtime.getDeviceProperties(
                    device_id
                )
            )

            raw_name = props.get(
                "name",
                b"NVIDIA CUDA GPU",
            )

            if isinstance(
                raw_name,
                bytes,
            ):
                name = raw_name.decode(
                    "utf-8",
                    errors="replace",
                )
            else:
                name = str(
                    raw_name
                )

            names.append(
                name
            )

        return (
            True,
            " | ".join(
                names
            ),
            count,
        )

    except Exception as exc:
        return (
            False,
            f"CUDA/CuPy unavailable: {exc}",
            0,
        )


# =============================================================================
# FFT worker
# =============================================================================

@dataclass(frozen=True)
class FFTSettings:
    fft_size: int
    window_name: str
    update_hz: int
    remove_dc: bool
    backend: str
    cuda_device: int


@dataclass(frozen=True)
class FFTResult:
    timestamp_monotonic: float
    total_samples: int

    raw_sample_rate_hz: float
    effective_sample_rate_hz: float
    decimation_samples: int
    decimation_mode: str
    adc_session_id: int

    frequency_hz: object
    spectrum_db: object

    peak_frequency_hz: tuple[
        float,
        float,
        float,
    ]

    peak_db: tuple[
        float,
        float,
        float,
    ]

    backend_name: str
    compute_ms: float
    copy_ms: float
    gpu_name: str


class FFTWorkerThread(QThread):
    """
    Reads shared ADC RAM and computes CH0/CH1/CH2 FFT outside the GUI thread.

    CUDA mode:
        NumPy shared-RAM snapshot
              ↓
        CuPy transfer
              ↓
        window / mean removal on GPU
              ↓
        cp.fft.rfft(axis=1)
              ↓
        magnitude + dB on GPU
              ↓
        only final spectrum copied back to host

    CPU mode:
        NumPy equivalent.
    """

    result_ready = Signal(object)
    worker_status = Signal(str)
    worker_error = Signal(str)

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

        self._settings_lock = (
            threading.Lock()
        )

        self._settings = FFTSettings(
            fft_size=DEFAULT_FFT_SIZE,
            window_name=DEFAULT_WINDOW,
            update_hz=DEFAULT_FFT_UPDATE_HZ,
            remove_dc=True,
            backend="Auto (CUDA preferred)",
            cuda_device=0,
        )

    def stop(
        self,
    ) -> None:

        self._stop_event.set()

    def set_settings(
        self,
        settings: FFTSettings,
    ) -> None:

        with self._settings_lock:
            self._settings = settings

    def _get_settings(
        self,
    ) -> FFTSettings:

        with self._settings_lock:
            return self._settings

    @staticmethod
    def _resolve_backend(
        requested: str,
    ) -> str:

        requested = str(
            requested
        )

        if requested.startswith(
            "CPU"
        ):
            return "numpy"

        if requested.startswith(
            "CUDA"
        ):
            return "cupy"

        # Auto: CUDA first.
        try:
            import cupy as cp

            if int(
                cp.cuda.runtime.getDeviceCount()
            ) > 0:
                return "cupy"

        except Exception:
            pass

        return "numpy"

    @staticmethod
    def _compute_numpy(
        matrix,
        *,
        fft_size: int,
        window_name: str,
        remove_dc: bool,
    ):
        window = build_numpy_window(
            window_name,
            fft_size,
        )

        x = matrix.astype(
            np.float32,
            copy=True,
        )

        if remove_dc:
            x -= np.mean(
                x,
                axis=1,
                keepdims=True,
                dtype=np.float32,
            )

        x *= window[
            np.newaxis,
            :
        ]

        spectrum = np.fft.rfft(
            x,
            axis=1,
        )

        amplitude = (
            single_sided_amplitude_numpy(
                spectrum,
                float(
                    np.sum(
                        window,
                        dtype=np.float64,
                    )
                ),
                fft_size,
            )
        )

        spectrum_db = (
            20.0
            * np.log10(
                np.maximum(
                    amplitude,
                    EPSILON,
                )
            )
        ).astype(
            np.float32,
            copy=False,
        )

        return spectrum_db

    @staticmethod
    def _compute_cupy(
        matrix,
        *,
        fft_size: int,
        window_name: str,
        remove_dc: bool,
        device_id: int,
    ):
        import cupy as cp

        with cp.cuda.Device(
            int(
                device_id
            )
        ):
            props = (
                cp.cuda.runtime.getDeviceProperties(
                    int(
                        device_id
                    )
                )
            )

            raw_name = props.get(
                "name",
                b"NVIDIA CUDA GPU",
            )

            gpu_name = (
                raw_name.decode(
                    "utf-8",
                    errors="replace",
                )
                if isinstance(
                    raw_name,
                    bytes,
                )
                else str(
                    raw_name
                )
            )

            copy_start = (
                time.perf_counter()
            )

            x = cp.asarray(
                matrix,
                dtype=cp.float32,
            )

            cp.cuda.get_current_stream().synchronize()

            copy_ms = (
                time.perf_counter()
                - copy_start
            ) * 1000.0

            name = str(
                window_name
            ).strip().lower()

            if name == "hann":
                window = cp.hanning(
                    fft_size
                ).astype(
                    cp.float32
                )

            elif name == "hamming":
                window = cp.hamming(
                    fft_size
                ).astype(
                    cp.float32
                )

            elif name == "blackman":
                window = cp.blackman(
                    fft_size
                ).astype(
                    cp.float32
                )

            else:
                window = cp.ones(
                    fft_size,
                    dtype=cp.float32,
                )

            compute_start = (
                time.perf_counter()
            )

            if remove_dc:
                x -= cp.mean(
                    x,
                    axis=1,
                    keepdims=True,
                )

            x *= window[
                cp.newaxis,
                :
            ]

            spectrum = cp.fft.rfft(
                x,
                axis=1,
            )

            amplitude = (
                cp.abs(
                    spectrum
                )
                / cp.maximum(
                    cp.sum(
                        window,
                        dtype=cp.float64,
                    ),
                    EPSILON,
                )
            )

            if amplitude.shape[-1] > 1:
                amplitude[
                    :,
                    1:
                ] *= 2.0

                if (
                    fft_size % 2 == 0
                    and amplitude.shape[-1] >= 2
                ):
                    amplitude[
                        :,
                        -1
                    ] *= 0.5

            spectrum_db_gpu = (
                20.0
                * cp.log10(
                    cp.maximum(
                        amplitude,
                        EPSILON,
                    )
                )
            ).astype(
                cp.float32
            )

            cp.cuda.get_current_stream().synchronize()

            compute_ms = (
                time.perf_counter()
                - compute_start
            ) * 1000.0

            host_copy_start = (
                time.perf_counter()
            )

            spectrum_db = cp.asnumpy(
                spectrum_db_gpu
            )

            host_copy_ms = (
                time.perf_counter()
                - host_copy_start
            ) * 1000.0

            return (
                spectrum_db,
                compute_ms,
                copy_ms + host_copy_ms,
                gpu_name,
            )

    def run(
        self,
    ) -> None:

        shared = None
        last_processed_total = -1
        last_compute_time = 0.0
        last_session_id = -1
        last_effective_rate_hz = -1.0

        try:
            shared = OBSSharedData()

            self.worker_status.emit(
                "FFT worker attached to shared_data_v5 RAM"
            )

            while not self._stop_event.is_set():

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

                if (
                    session_id != last_session_id
                    or abs(
                        effective_rate_hz
                        - last_effective_rate_hz
                    )
                    > max(
                        1.0e-9,
                        1.0e-6
                        * effective_rate_hz,
                    )
                ):
                    last_session_id = session_id
                    last_effective_rate_hz = (
                        effective_rate_hz
                    )
                    last_processed_total = -1

                    self.worker_status.emit(
                        (
                            f"ADC stream: "
                            f"{stream_info.raw_sample_rate_hz:g} Hz / "
                            f"N={stream_info.decimation_samples} -> "
                            f"{effective_rate_hz:.3f} Hz | "
                            f"session {session_id}"
                        )
                    )

                settings = (
                    self._get_settings()
                )

                update_period_s = (
                    1.0
                    / max(
                        1,
                        int(
                            settings.update_hz
                        ),
                    )
                )

                now = time.perf_counter()

                if (
                    now
                    - last_compute_time
                    < update_period_s
                ):
                    self.msleep(
                        2
                    )
                    continue

                total = (
                    shared.adc_total_samples()
                )

                # Do not recompute identical data.
                if total == last_processed_total:
                    self.msleep(
                        2
                    )
                    continue

                fft_size = int(
                    settings.fft_size
                )

                if total < fft_size:
                    self.worker_status.emit(
                        (
                            f"Waiting for {fft_size:,} ADC samples "
                            f"({total:,} available)"
                        )
                    )

                    self.msleep(
                        20
                    )
                    continue

                snapshot = (
                    shared.read_adc_latest_numpy(
                        fft_size
                    )
                )

                if len(
                    snapshot.ch0
                ) < fft_size:
                    self.msleep(
                        5
                    )
                    continue

                # Keep exactly the newest N samples.
                matrix = np.stack(
                    (
                        snapshot.ch0[
                            -fft_size:
                        ],
                        snapshot.ch1[
                            -fft_size:
                        ],
                        snapshot.ch2[
                            -fft_size:
                        ],
                    ),
                    axis=0,
                )

                backend = (
                    self._resolve_backend(
                        settings.backend
                    )
                )

                gpu_name = ""
                copy_ms = 0.0

                try:
                    if backend == "cupy":
                        (
                            spectrum_db,
                            compute_ms,
                            copy_ms,
                            gpu_name,
                        ) = self._compute_cupy(
                            matrix,
                            fft_size=fft_size,
                            window_name=(
                                settings.window_name
                            ),
                            remove_dc=(
                                settings.remove_dc
                            ),
                            device_id=(
                                settings.cuda_device
                            ),
                        )

                        backend_name = (
                            "CUDA / CuPy / cuFFT"
                        )

                    else:
                        compute_start = (
                            time.perf_counter()
                        )

                        spectrum_db = (
                            self._compute_numpy(
                                matrix,
                                fft_size=fft_size,
                                window_name=(
                                    settings.window_name
                                ),
                                remove_dc=(
                                    settings.remove_dc
                                ),
                            )
                        )

                        compute_ms = (
                            time.perf_counter()
                            - compute_start
                        ) * 1000.0

                        backend_name = (
                            "CPU / NumPy FFT"
                        )

                except Exception as cuda_exc:
                    # If Auto selected and CUDA fails at runtime, stay alive by
                    # falling back to NumPy. Explicit CUDA selection reports the
                    # fallback in the status string too.
                    compute_start = (
                        time.perf_counter()
                    )

                    spectrum_db = (
                        self._compute_numpy(
                            matrix,
                            fft_size=fft_size,
                            window_name=(
                                settings.window_name
                            ),
                            remove_dc=(
                                settings.remove_dc
                            ),
                        )
                    )

                    compute_ms = (
                        time.perf_counter()
                        - compute_start
                    ) * 1000.0

                    backend_name = (
                        "CPU fallback / NumPy FFT"
                    )

                    gpu_name = (
                        f"CUDA error: {cuda_exc}"
                    )

                # shared_data_v5 injects the authoritative effective stream
                # rate into every ADC snapshot. This is the physical FFT rate.
                sample_rate_hz = max(
                    0.001,
                    float(
                        snapshot.sample_rate_hz
                    ),
                )

                frequency_hz = (
                    np.fft.rfftfreq(
                        fft_size,
                        d=(
                            1.0
                            / sample_rate_hz
                        ),
                    )
                    .astype(
                        np.float32,
                        copy=False,
                    )
                )

                # Ignore DC for peak search when DC removal is enabled.
                peak_start_bin = (
                    1
                    if (
                        settings.remove_dc
                        and spectrum_db.shape[1] > 1
                    )
                    else 0
                )

                peak_frequency = []
                peak_db = []

                for channel_index in range(
                    3
                ):
                    channel_spectrum = (
                        spectrum_db[
                            channel_index,
                            peak_start_bin:
                        ]
                    )

                    if len(
                        channel_spectrum
                    ):
                        local_index = int(
                            np.argmax(
                                channel_spectrum
                            )
                        )

                        index = (
                            local_index
                            + peak_start_bin
                        )

                        (
                            interpolated_frequency_hz,
                            interpolated_peak_db,
                        ) = interpolate_fft_peak(
                            frequency_hz,
                            spectrum_db[
                                channel_index
                            ],
                            index,
                        )

                        peak_frequency.append(
                            interpolated_frequency_hz
                        )

                        peak_db.append(
                            interpolated_peak_db
                        )

                    else:
                        peak_frequency.append(
                            0.0
                        )

                        peak_db.append(
                            float(
                                "-inf"
                            )
                        )

                result = FFTResult(
                    timestamp_monotonic=(
                        time.perf_counter()
                    ),
                    total_samples=int(
                        snapshot.total_samples
                    ),
                    raw_sample_rate_hz=float(
                        stream_info.raw_sample_rate_hz
                    ),
                    effective_sample_rate_hz=float(
                        sample_rate_hz
                    ),
                    decimation_samples=int(
                        stream_info.decimation_samples
                    ),
                    decimation_mode=str(
                        stream_info.decimation_mode
                    ),
                    adc_session_id=int(
                        stream_info.adc_session_id
                    ),
                    frequency_hz=frequency_hz,
                    spectrum_db=(
                        spectrum_db.astype(
                            np.float32,
                            copy=False,
                        )
                    ),
                    peak_frequency_hz=tuple(
                        peak_frequency
                    ),
                    peak_db=tuple(
                        peak_db
                    ),
                    backend_name=backend_name,
                    compute_ms=float(
                        compute_ms
                    ),
                    copy_ms=float(
                        copy_ms
                    ),
                    gpu_name=str(
                        gpu_name
                    ),
                )

                last_processed_total = (
                    int(
                        snapshot.total_samples
                    )
                )

                last_compute_time = (
                    time.perf_counter()
                )

                self.result_ready.emit(
                    result
                )

        except Exception as exc:
            if not self._stop_event.is_set():
                self.worker_error.emit(
                    str(
                        exc
                    )
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
class FFTChannelControl:
    index: int
    channel_name: str
    axis_name: str

    group: QGroupBox

    freq_min_spin: QDoubleSpinBox
    freq_max_spin: QDoubleSpinBox

    amp_min_spin: QDoubleSpinBox
    amp_max_spin: QDoubleSpinBox

    auto_y_checkbox: QCheckBox

    peak_label: QLabel

    apply_button: QPushButton
    reset_button: QPushButton


# =============================================================================
# Main window
# =============================================================================

class GeophoneFFTWindow(QMainWindow):

    def __init__(
        self,
    ):
        super().__init__()

        if np is None or pg is None:
            raise RuntimeError(
                "Geophone FFT requires NumPy and PyQtGraph."
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
            self.decimation_samples = int(
                stream_info.decimation_samples
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

        (
            self.cuda_available,
            self.cuda_description,
            self.cuda_device_count,
        ) = detect_cupy()

        self.paused = False

        self.latest_result: Optional[
            FFTResult
        ] = None

        self.target_spectrum = None
        self.display_spectrum = None

        self.last_spectrum_arrival = 0.0
        self.last_peak_label_update = 0.0

        self.render_fps = 0.0
        self.render_jitter_ms = 0.0

        self._fps_count = 0
        self._fps_window_start = (
            time.perf_counter()
        )

        self._last_render_ns: Optional[
            int
        ] = None

        self.opengl_active = False
        self.opengl_error = ""

        self.plots = []
        self.curves = []
        self.peak_lines = []

        self.channel_controls: list[
            FFTChannelControl
        ] = []

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

        # FFT worker.
        self.fft_worker = (
            FFTWorkerThread(
                self
            )
        )

        self.fft_worker.result_ready.connect(
            self.on_fft_result
        )

        self.fft_worker.worker_status.connect(
            self.on_worker_status
        )

        self.fft_worker.worker_error.connect(
            self.on_worker_error
        )

        self._push_fft_settings()

        self.fft_worker.start()

        # 60-FPS visual renderer.
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

        self._set_render_fps(
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

    def current_sample_rate_hz(
        self,
    ) -> float:

        return max(
            0.001,
            float(
                self.effective_sample_rate_hz
            ),
        )

    def current_nyquist_hz(
        self,
    ) -> float:

        return (
            self.current_sample_rate_hz()
            / 2.0
        )

    def default_frequency_max_hz(
        self,
    ) -> float:

        return min(
            DEFAULT_FREQ_VIEW_MAX_HZ,
            self.current_nyquist_hz(),
        )

    def _update_fft_size_labels(
        self,
    ) -> None:

        if not hasattr(
            self,
            "fft_size_combo",
        ):
            return

        sample_rate_hz = (
            self.current_sample_rate_hz()
        )

        for index in range(
            self.fft_size_combo.count()
        ):
            fft_size = int(
                self.fft_size_combo.itemData(
                    index
                )
            )

            resolution = (
                sample_rate_hz
                / fft_size
            )

            history_s = (
                fft_size
                / sample_rate_hz
            )

            self.fft_size_combo.setItemText(
                index,
                (
                    f"{fft_size:,} "
                    f"({history_s:.3f} s • "
                    f"{resolution:.4f} Hz/bin)"
                ),
            )

        if hasattr(
            self,
            "fft_response_hint",
        ):
            fft_size = int(
                self.fft_size_combo.currentData()
                or DEFAULT_FFT_SIZE
            )

            history_s = (
                fft_size
                / sample_rate_hz
            )

            window_name = (
                self.window_combo.currentText()
                if hasattr(
                    self,
                    "window_combo",
                )
                else DEFAULT_WINDOW
            )

            normalized_window = (
                window_name
                .strip()
                .lower()
            )

            if normalized_window == "rectangular":
                main_lobe_bins = 2.0
                window_note = (
                    "narrowest main lobe, higher leakage"
                )
            elif normalized_window == "blackman":
                main_lobe_bins = 6.0
                window_note = (
                    "widest main lobe, low leakage"
                )
            else:
                main_lobe_bins = 4.0
                window_note = (
                    "balanced leakage/main-lobe width"
                )

            approx_null_width_hz = (
                main_lobe_bins
                * resolution
            )

            self.fft_response_hint.setText(
                (
                    f"History ≈ {history_s:.3f} s • "
                    f"Δf={resolution:.4f} Hz/bin • "
                    f"main-lobe width ≈ {approx_null_width_hz:.3f} Hz "
                    f"({window_note}). "
                    f"Peak marker uses sub-bin interpolation."
                )
            )

    def _apply_stream_rate_to_ui(
        self,
        *,
        raw_sample_rate_hz: float,
        effective_sample_rate_hz: float,
        decimation_samples: int,
        decimation_mode: str,
        adc_session_id: int,
    ) -> None:

        old_effective_rate = float(
            self.effective_sample_rate_hz
        )
        old_session_id = int(
            self.adc_session_id
        )

        self.raw_sample_rate_hz = float(
            raw_sample_rate_hz
        )
        self.effective_sample_rate_hz = max(
            0.001,
            float(
                effective_sample_rate_hz
            ),
        )
        self.decimation_samples = max(
            1,
            int(
                decimation_samples
            ),
        )
        self.decimation_mode = str(
            decimation_mode
        )
        self.adc_session_id = int(
            adc_session_id
        )

        rate_changed = (
            abs(
                old_effective_rate
                - self.effective_sample_rate_hz
            )
            > max(
                1.0e-9,
                1.0e-6
                * self.effective_sample_rate_hz,
            )
        )

        session_changed = (
            old_session_id
            != self.adc_session_id
        )

        self._update_fft_size_labels()

        if hasattr(
            self,
            "channel_controls",
        ):
            nyquist_hz = (
                self.current_nyquist_hz()
            )

            default_max_hz = (
                self.default_frequency_max_hz()
            )

            for control in (
                self.channel_controls
            ):
                control.freq_min_spin.setRange(
                    0.0,
                    nyquist_hz,
                )
                control.freq_max_spin.setRange(
                    0.0,
                    nyquist_hz,
                )

                if (
                    control.freq_min_spin.value()
                    > nyquist_hz
                ):
                    control.freq_min_spin.setValue(
                        0.0
                    )

                if (
                    control.freq_max_spin.value()
                    > nyquist_hz
                    or control.freq_max_spin.value()
                    <= control.freq_min_spin.value()
                ):
                    control.freq_max_spin.setValue(
                        default_max_hz
                    )

            if (
                rate_changed
                or session_changed
            ):
                # Old spectral arrays have a different frequency calibration.
                self.latest_result = None
                self.target_spectrum = None
                self.display_spectrum = None

                for control in (
                    self.channel_controls
                ):
                    self.reset_channel_view(
                        control
                    )

    # -------------------------------------------------------------------------
    # Graphics
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

            fmt.setSamples(
                0
            )

            fmt.setSwapInterval(
                0
            )

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
        header = QHBoxLayout()

        title_box = QVBoxLayout()
        title_box.setSpacing(
            1
        )

        title = QLabel(
            "GEOPHONE FFT SPECTRUM"
        )

        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            (
                "CH0 / X  •  CH1 / Y  •  CH2 / Z  •  "
                "NVIDIA CUDA/cuFFT preferred"
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

        header.addLayout(
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

        header.addWidget(
            self.pause_button
        )

        root.addLayout(
            header
        )

        # Status.
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

        self.fft_status_label = QLabel(
            "FFT: waiting..."
        )

        self.fft_status_label.setObjectName(
            "statusLabel"
        )

        self.render_status_label = QLabel(
            "Render: --"
        )

        self.render_status_label.setObjectName(
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
            self.fft_status_label
        )

        status_layout.addSpacing(
            14
        )

        status_layout.addWidget(
            self.render_status_label
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

        splitter.addWidget(
            self._build_plot_panel()
        )

        splitter.addWidget(
            self._build_settings_panel()
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

        pens = (
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
                "Amplitude",
                units="dB(count)",
            )

            plot.setLabel(
                "bottom",
                "Frequency",
                units="Hz",
            )

            plot.setTitle(
                (
                    f"{channel_name} — "
                    f"{axis_name}"
                ),
                color="#FFFFFF",
                size="11pt",
            )

            plot.setXRange(
                DEFAULT_FREQ_MIN,
                self.default_frequency_max_hz(),
                padding=0.0,
            )

            plot.setYRange(
                DEFAULT_DB_MIN,
                DEFAULT_DB_MAX,
                padding=0.0,
            )

            curve = plot.plot(
                [],
                [],
                pen=pens[
                    index
                ],
            )

            peak_line = pg.InfiniteLine(
                pos=0.0,
                angle=90,
                movable=False,
                pen=pg.mkPen(
                    pens[
                        index
                    ].color(),
                    width=1,
                ),
            )

            peak_line.setZValue(
                20
            )
            peak_line.setVisible(
                True
            )

            plot.addItem(
                peak_line
            )

            self.plots.append(
                plot
            )

            self.curves.append(
                curve
            )

            self.peak_lines.append(
                peak_line
            )

        return panel

    def _build_settings_panel(
        self,
    ) -> QWidget:

        outer = QFrame()
        outer.setObjectName(
            "settingsPanel"
        )

        layout = QVBoxLayout(
            outer
        )

        layout.setContentsMargins(
            8,
            0,
            0,
            0,
        )

        layout.setSpacing(
            8
        )

        heading = QLabel(
            "FFT SETTINGS"
        )

        heading.setObjectName(
            "settingsTitle"
        )

        layout.addWidget(
            heading
        )

        # Compute / performance card.
        performance = QGroupBox(
            "FFT Compute / Performance"
        )

        performance.setObjectName(
            "channelGroup"
        )

        grid = QGridLayout(
            performance
        )

        grid.setContentsMargins(
            10,
            12,
            10,
            10,
        )

        grid.setHorizontalSpacing(
            7
        )

        grid.setVerticalSpacing(
            6
        )

        self.backend_combo = QComboBox()

        self.backend_combo.addItem(
            "Auto (CUDA preferred)"
        )

        self.backend_combo.addItem(
            "CUDA / CuPy / cuFFT"
        )

        self.backend_combo.addItem(
            "CPU / NumPy FFT"
        )

        self.backend_combo.currentIndexChanged.connect(
            self._push_fft_settings
        )

        self.cuda_device_combo = QComboBox()

        if self.cuda_available:
            try:
                import cupy as cp

                for device_id in range(
                    self.cuda_device_count
                ):
                    props = (
                        cp.cuda.runtime.getDeviceProperties(
                            device_id
                        )
                    )

                    raw_name = props.get(
                        "name",
                        b"NVIDIA CUDA GPU",
                    )

                    name = (
                        raw_name.decode(
                            "utf-8",
                            errors="replace",
                        )
                        if isinstance(
                            raw_name,
                            bytes,
                        )
                        else str(
                            raw_name
                        )
                    )

                    self.cuda_device_combo.addItem(
                        (
                            f"CUDA {device_id}: "
                            f"{name}"
                        ),
                        device_id,
                    )

            except Exception:
                self.cuda_device_combo.addItem(
                    "CUDA device 0",
                    0,
                )

        else:
            self.cuda_device_combo.addItem(
                "CUDA unavailable",
                0,
            )

            self.cuda_device_combo.setEnabled(
                False
            )

        self.cuda_device_combo.currentIndexChanged.connect(
            self._push_fft_settings
        )

        self.fft_size_combo = QComboBox()

        for fft_size in FFT_SIZES:
            sample_rate_hz = (
                self.current_sample_rate_hz()
            )

            resolution = (
                sample_rate_hz
                / fft_size
            )

            history_s = (
                fft_size
                / sample_rate_hz
            )

            self.fft_size_combo.addItem(
                (
                    f"{fft_size:,} "
                    f"({history_s:.3f} s • "
                    f"{resolution:.4f} Hz/bin)"
                ),
                fft_size,
            )

        default_index = (
            self.fft_size_combo.findData(
                DEFAULT_FFT_SIZE
            )
        )

        if default_index >= 0:
            self.fft_size_combo.setCurrentIndex(
                default_index
            )

        self.fft_size_combo.currentIndexChanged.connect(
            self._push_fft_settings
        )

        self.fft_response_hint = QLabel(
            ""
        )
        self.fft_response_hint.setObjectName(
            "hintText"
        )
        self.fft_response_hint.setWordWrap(
            True
        )

        self.window_combo = QComboBox()

        self.window_combo.addItems(
            list(
                WINDOW_TYPES
            )
        )

        self.window_combo.setCurrentText(
            DEFAULT_WINDOW
        )

        self.window_combo.currentIndexChanged.connect(
            self._push_fft_settings
        )

        self.fft_update_combo = QComboBox()

        for value in (
            FFT_UPDATE_CHOICES_HZ
        ):
            self.fft_update_combo.addItem(
                f"{value} Hz",
                value,
            )

        default_index = (
            self.fft_update_combo.findData(
                DEFAULT_FFT_UPDATE_HZ
            )
        )

        if default_index >= 0:
            self.fft_update_combo.setCurrentIndex(
                default_index
            )

        self.fft_update_combo.currentIndexChanged.connect(
            self._push_fft_settings
        )

        self.render_fps_combo = QComboBox()

        for fps in RENDER_FPS_CHOICES:
            self.render_fps_combo.addItem(
                f"{fps} FPS",
                fps,
            )

        default_index = (
            self.render_fps_combo.findData(
                DEFAULT_RENDER_FPS
            )
        )

        if default_index >= 0:
            self.render_fps_combo.setCurrentIndex(
                default_index
            )

        self.render_fps_combo.currentIndexChanged.connect(
            self.on_render_fps_changed
        )

        self.smoothing_combo = QComboBox()

        for value_ms in (
            SPECTRUM_SMOOTH_CHOICES_MS
        ):
            label = (
                "Off"
                if value_ms == 0
                else f"{value_ms} ms"
            )

            self.smoothing_combo.addItem(
                label,
                value_ms,
            )

        default_index = (
            self.smoothing_combo.findData(
                DEFAULT_SPECTRUM_SMOOTH_MS
            )
        )

        if default_index >= 0:
            self.smoothing_combo.setCurrentIndex(
                default_index
            )

        self.remove_dc_checkbox = (
            QCheckBox(
                "Remove DC / Mean"
            )
        )

        self.remove_dc_checkbox.setChecked(
            True
        )

        self.remove_dc_checkbox.stateChanged.connect(
            self._push_fft_settings
        )

        self.peak_marker_checkbox = (
            QCheckBox(
                "Show interpolated peak marker"
            )
        )

        self.peak_marker_checkbox.setChecked(
            True
        )

        self.peak_marker_checkbox.stateChanged.connect(
            self._update_peak_marker_visibility
        )

        grid.addWidget(
            QLabel("FFT Engine"),
            0,
            0,
        )

        grid.addWidget(
            self.backend_combo,
            0,
            1,
        )

        grid.addWidget(
            QLabel("CUDA Device"),
            1,
            0,
        )

        grid.addWidget(
            self.cuda_device_combo,
            1,
            1,
        )

        grid.addWidget(
            QLabel("FFT Size"),
            2,
            0,
        )

        grid.addWidget(
            self.fft_size_combo,
            2,
            1,
        )

        grid.addWidget(
            QLabel("Window"),
            3,
            0,
        )

        grid.addWidget(
            self.window_combo,
            3,
            1,
        )

        grid.addWidget(
            QLabel("FFT Update"),
            4,
            0,
        )

        grid.addWidget(
            self.fft_update_combo,
            4,
            1,
        )

        grid.addWidget(
            QLabel("Render"),
            5,
            0,
        )

        grid.addWidget(
            self.render_fps_combo,
            5,
            1,
        )

        grid.addWidget(
            QLabel("Spectrum Smooth"),
            6,
            0,
        )

        grid.addWidget(
            self.smoothing_combo,
            6,
            1,
        )

        grid.addWidget(
            self.remove_dc_checkbox,
            7,
            0,
            1,
            2,
        )

        grid.addWidget(
            self.peak_marker_checkbox,
            8,
            0,
            1,
            2,
        )

        self.cuda_info_label = QLabel(
            (
                self.cuda_description
                if self.cuda_available
                else (
                    "CUDA FFT unavailable; "
                    "NumPy CPU fallback will be used."
                )
            )
        )

        self.cuda_info_label.setObjectName(
            "sampleInfo"
        )

        self.cuda_info_label.setWordWrap(
            True
        )

        grid.addWidget(
            self.cuda_info_label,
            9,
            0,
            1,
            2,
        )

        layout.addWidget(
            performance
        )

        # Channel cards in scroll area.
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
            _attribute,
        ) in enumerate(
            CHANNELS
        ):
            control = (
                self._create_channel_control(
                    index,
                    channel_name,
                    axis_name,
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

        layout.addWidget(
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

        layout.addWidget(
            reset_all
        )

        return outer

    def _create_channel_control(
        self,
        index: int,
        channel_name: str,
        axis_name: str,
    ) -> FFTChannelControl:

        group = QGroupBox(
            (
                f"{channel_name}  •  "
                f"{axis_name}"
            )
        )

        group.setObjectName(
            "channelGroup"
        )

        grid = QGridLayout(
            group
        )

        grid.setContentsMargins(
            10,
            12,
            10,
            10,
        )

        grid.setHorizontalSpacing(
            7
        )

        grid.setVerticalSpacing(
            6
        )

        freq_min = QDoubleSpinBox()
        freq_min.setRange(
            0.0,
            self.current_nyquist_hz(),
        )
        freq_min.setDecimals(
            2
        )
        freq_min.setSuffix(
            " Hz"
        )
        freq_min.setValue(
            DEFAULT_FREQ_MIN
        )

        freq_max = QDoubleSpinBox()
        freq_max.setRange(
            0.0,
            self.current_nyquist_hz(),
        )
        freq_max.setDecimals(
            2
        )
        freq_max.setSuffix(
            " Hz"
        )
        freq_max.setValue(
            self.default_frequency_max_hz()
        )

        amp_min = QDoubleSpinBox()
        amp_min.setRange(
            -300.0,
            300.0,
        )
        amp_min.setDecimals(
            1
        )
        amp_min.setSuffix(
            " dB"
        )
        amp_min.setValue(
            DEFAULT_DB_MIN
        )

        amp_max = QDoubleSpinBox()
        amp_max.setRange(
            -300.0,
            300.0,
        )
        amp_max.setDecimals(
            1
        )
        amp_max.setSuffix(
            " dB"
        )
        amp_max.setValue(
            DEFAULT_DB_MAX
        )

        auto_y = QCheckBox(
            "Auto Y Range"
        )

        peak_label = QLabel(
            "Peak: -- Hz / -- dB"
        )

        peak_label.setObjectName(
            "currentValue"
        )

        apply_button = QPushButton(
            "Apply"
        )

        apply_button.setObjectName(
            "smallPrimaryButton"
        )

        reset_button = QPushButton(
            "Reset"
        )

        reset_button.setObjectName(
            "smallButton"
        )

        grid.addWidget(
            QLabel("Freq Min"),
            0,
            0,
        )
        grid.addWidget(
            freq_min,
            0,
            1,
            1,
            2,
        )

        grid.addWidget(
            QLabel("Freq Max"),
            1,
            0,
        )
        grid.addWidget(
            freq_max,
            1,
            1,
            1,
            2,
        )

        grid.addWidget(
            QLabel("Amp Min"),
            2,
            0,
        )
        grid.addWidget(
            amp_min,
            2,
            1,
            1,
            2,
        )

        grid.addWidget(
            QLabel("Amp Max"),
            3,
            0,
        )
        grid.addWidget(
            amp_max,
            3,
            1,
            1,
            2,
        )

        grid.addWidget(
            auto_y,
            4,
            0,
            1,
            3,
        )

        grid.addWidget(
            peak_label,
            5,
            0,
            1,
            3,
        )

        grid.addWidget(
            apply_button,
            6,
            0,
            1,
            2,
        )

        grid.addWidget(
            reset_button,
            6,
            2,
        )

        control = FFTChannelControl(
            index=index,
            channel_name=channel_name,
            axis_name=axis_name,
            group=group,
            freq_min_spin=freq_min,
            freq_max_spin=freq_max,
            amp_min_spin=amp_min,
            amp_max_spin=amp_max,
            auto_y_checkbox=auto_y,
            peak_label=peak_label,
            apply_button=apply_button,
            reset_button=reset_button,
        )

        apply_button.clicked.connect(
            lambda checked=False, c=control:
            self.apply_channel_view(
                c
            )
        )

        reset_button.clicked.connect(
            lambda checked=False, c=control:
            self.reset_channel_view(
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

    # -------------------------------------------------------------------------
    # Styles
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
                font-size: 12px;
                font-weight: 800;
            }

            QLabel#sampleInfo {
                color: #7894A4;
                font-size: 9px;
            }

            QDoubleSpinBox,
            QSpinBox,
            QComboBox {
                background-color: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 5px;
                min-height: 25px;
                padding: 1px 5px;
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
    # Keyboard
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
    # Worker settings
    # -------------------------------------------------------------------------

    def _current_fft_settings(
        self,
    ) -> FFTSettings:

        fft_size = int(
            self.fft_size_combo.currentData()
            or DEFAULT_FFT_SIZE
        )

        update_hz = int(
            self.fft_update_combo.currentData()
            or DEFAULT_FFT_UPDATE_HZ
        )

        device_id = int(
            self.cuda_device_combo.currentData()
            or 0
        )

        return FFTSettings(
            fft_size=fft_size,
            window_name=(
                self.window_combo.currentText()
            ),
            update_hz=update_hz,
            remove_dc=(
                self.remove_dc_checkbox.isChecked()
            ),
            backend=(
                self.backend_combo.currentText()
            ),
            cuda_device=device_id,
        )

    def _push_fft_settings(
        self,
        *_args,
    ) -> None:

        if not hasattr(
            self,
            "fft_worker",
        ):
            return

        self.fft_worker.set_settings(
            self._current_fft_settings()
        )

        self._update_fft_size_labels()

        # New FFT size/window/backend means previous smoothed spectrum should
        # not be blended with an incompatible result.
        self.latest_result = None
        self.target_spectrum = None
        self.display_spectrum = None

    def on_render_fps_changed(
        self,
        *_args,
    ) -> None:

        fps = int(
            self.render_fps_combo.currentData()
            or DEFAULT_RENDER_FPS
        )

        self._set_render_fps(
            fps
        )

    def _set_render_fps(
        self,
        fps: int,
    ) -> None:

        interval_ms = max(
            1,
            round(
                1000.0
                / max(
                    1,
                    int(
                        fps
                    ),
                )
            ),
        )

        self.render_timer.start(
            interval_ms
        )

    # -------------------------------------------------------------------------
    # Worker callbacks
    # -------------------------------------------------------------------------

    def on_fft_result(
        self,
        result: FFTResult,
    ) -> None:

        self._apply_stream_rate_to_ui(
            raw_sample_rate_hz=(
                result.raw_sample_rate_hz
            ),
            effective_sample_rate_hz=(
                result.effective_sample_rate_hz
            ),
            decimation_samples=(
                result.decimation_samples
            ),
            decimation_mode=(
                result.decimation_mode
            ),
            adc_session_id=(
                result.adc_session_id
            ),
        )

        self.latest_result = result

        new_target = (
            result.spectrum_db.astype(
                np.float32,
                copy=True,
            )
        )

        if (
            self.target_spectrum is None
            or self.target_spectrum.shape
            != new_target.shape
        ):
            self.target_spectrum = (
                new_target
            )

            self.display_spectrum = (
                new_target.copy()
            )

        else:
            self.target_spectrum = (
                new_target
            )

        self.last_spectrum_arrival = (
            time.perf_counter()
        )

    def on_worker_status(
        self,
        message: str,
    ) -> None:

        self.fft_status_label.setText(
            message
        )

    def on_worker_error(
        self,
        message: str,
    ) -> None:

        self.fft_status_label.setText(
            (
                "FFT worker error: "
                f"{message}"
            )
        )

    def _update_peak_marker_visibility(
        self,
        *_args,
    ) -> None:

        visible = bool(
            self.peak_marker_checkbox.isChecked()
        )

        for line in self.peak_lines:
            line.setVisible(
                visible
            )

    # -------------------------------------------------------------------------
    # Smooth spectrum rendering
    # -------------------------------------------------------------------------

    def _smoothing_alpha(
        self,
        dt_s: float,
    ) -> float:

        smoothing_ms = int(
            self.smoothing_combo.currentData()
            or 0
        )

        if smoothing_ms <= 0:
            return 1.0

        tau_s = (
            smoothing_ms
            / 1000.0
        )

        return float(
            1.0
            - np.exp(
                -max(
                    0.0,
                    dt_s,
                )
                / max(
                    1.0e-6,
                    tau_s,
                )
            )
        )

    @staticmethod
    def _limit_render_bins(
        frequency,
        amplitude,
    ):
        count = len(
            frequency
        )

        if count <= MAX_RENDER_BINS:
            return (
                frequency,
                amplitude,
            )

        step = int(
            np.ceil(
                count
                / MAX_RENDER_BINS
            )
        )

        return (
            frequency[
                ::step
            ],
            amplitude[
                ::step
            ],
        )

    def render_frame(
        self,
    ) -> None:

        if self.paused:
            return

        result = (
            self.latest_result
        )

        target = (
            self.target_spectrum
        )

        if result is None or target is None:
            return

        if (
            self.display_spectrum is None
            or self.display_spectrum.shape
            != target.shape
        ):
            self.display_spectrum = (
                target.copy()
            )

        now = time.perf_counter()

        if self._last_render_ns is None:
            dt_s = (
                1.0
                / DEFAULT_RENDER_FPS
            )

        else:
            dt_s = (
                (
                    time.perf_counter_ns()
                    - self._last_render_ns
                )
                / 1_000_000_000.0
            )

        alpha = self._smoothing_alpha(
            dt_s
        )

        if alpha >= 0.9999:
            self.display_spectrum[...] = (
                target
            )

        else:
            self.display_spectrum += (
                target
                - self.display_spectrum
            ) * alpha

        frequency = (
            result.frequency_hz
        )

        for control in self.channel_controls:
            spectrum = (
                self.display_spectrum[
                    control.index
                ]
            )

            freq_min = float(
                control.freq_min_spin.value()
            )

            freq_max = float(
                control.freq_max_spin.value()
            )

            left = int(
                np.searchsorted(
                    frequency,
                    freq_min,
                    side="left",
                )
            )

            right = int(
                np.searchsorted(
                    frequency,
                    freq_max,
                    side="right",
                )
            )

            right = max(
                left + 1,
                min(
                    len(
                        frequency
                    ),
                    right,
                ),
            )

            f = frequency[
                left:right
            ]

            y = spectrum[
                left:right
            ]

            (
                f_render,
                y_render,
            ) = self._limit_render_bins(
                f,
                y,
            )

            self.curves[
                control.index
            ].setData(
                f_render,
                y_render,
                connect="all",
            )

            if (
                control.auto_y_checkbox.isChecked()
                and len(
                    y_render
                )
            ):
                finite = y_render[
                    np.isfinite(
                        y_render
                    )
                ]

                if len(
                    finite
                ):
                    y_min = float(
                        np.min(
                            finite
                        )
                    )

                    y_max = float(
                        np.max(
                            finite
                        )
                    )

                    margin = max(
                        2.0,
                        (
                            y_max
                            - y_min
                        )
                        * 0.08,
                    )

                    self.plots[
                        control.index
                    ].setYRange(
                        y_min - margin,
                        y_max + margin,
                        padding=0.0,
                    )

        if (
            now
            - self.last_peak_label_update
            >= PEAK_LABEL_INTERVAL_S
        ):
            for control in (
                self.channel_controls
            ):
                peak_frequency_hz = float(
                    result.peak_frequency_hz[
                        control.index
                    ]
                )

                control.peak_label.setText(
                    (
                        f"Peak: "
                        f"{peak_frequency_hz:.3f} Hz"
                        f" / "
                        f"{result.peak_db[control.index]:.1f} dB"
                    )
                )

                if (
                    0
                    <= control.index
                    < len(
                        self.peak_lines
                    )
                ):
                    self.peak_lines[
                        control.index
                    ].setPos(
                        peak_frequency_hz
                    )

            self.last_peak_label_update = (
                now
            )

        self._update_render_metrics()

    def _update_render_metrics(
        self,
    ) -> None:

        now_ns = (
            time.perf_counter_ns()
        )

        if self._last_render_ns is not None:
            interval_ms = (
                now_ns
                - self._last_render_ns
            ) / 1_000_000.0

            target_fps = int(
                self.render_fps_combo.currentData()
                or DEFAULT_RENDER_FPS
            )

            target_ms = (
                1000.0
                / target_fps
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

        self._last_render_ns = (
            now_ns
        )

        self._fps_count += 1

        now = time.perf_counter()

        elapsed = (
            now
            - self._fps_window_start
        )

        if elapsed >= 0.75:
            self.render_fps = (
                self._fps_count
                / elapsed
            )

            self._fps_count = 0
            self._fps_window_start = now

    # -------------------------------------------------------------------------
    # View controls
    # -------------------------------------------------------------------------

    def apply_channel_view(
        self,
        control: FFTChannelControl,
    ) -> None:

        f_min = float(
            control.freq_min_spin.value()
        )

        f_max = float(
            control.freq_max_spin.value()
        )

        y_min = float(
            control.amp_min_spin.value()
        )

        y_max = float(
            control.amp_max_spin.value()
        )

        if f_min >= f_max:
            QMessageBox.warning(
                self,
                APP_TITLE,
                (
                    f"{control.channel_name}: "
                    "Freq Min must be lower than Freq Max."
                ),
            )
            return

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

        self.plots[
            control.index
        ].setXRange(
            f_min,
            f_max,
            padding=0.0,
        )

        if not (
            control.auto_y_checkbox.isChecked()
        ):
            self.plots[
                control.index
            ].setYRange(
                y_min,
                y_max,
                padding=0.0,
            )

    def on_auto_y_changed(
        self,
        control: FFTChannelControl,
    ) -> None:

        if not (
            control.auto_y_checkbox.isChecked()
        ):
            self.apply_channel_view(
                control
            )

    def reset_channel_view(
        self,
        control: FFTChannelControl,
    ) -> None:

        control.freq_min_spin.setValue(
            DEFAULT_FREQ_MIN
        )

        control.freq_max_spin.setValue(
            self.default_frequency_max_hz()
        )

        control.amp_min_spin.setValue(
            DEFAULT_DB_MIN
        )

        control.amp_max_spin.setValue(
            DEFAULT_DB_MAX
        )

        control.auto_y_checkbox.setChecked(
            False
        )

        self.plots[
            control.index
        ].setXRange(
            DEFAULT_FREQ_MIN,
            self.default_frequency_max_hz(),
            padding=0.0,
        )

        self.plots[
            control.index
        ].setYRange(
            DEFAULT_DB_MIN,
            DEFAULT_DB_MAX,
            padding=0.0,
        )

    def reset_all_views(
        self,
    ) -> None:

        for control in (
            self.channel_controls
        ):
            self.reset_channel_view(
                control
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

            # Resume immediately at latest result.
            if (
                self.target_spectrum is not None
            ):
                self.display_spectrum = (
                    self.target_spectrum.copy()
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
    # Status
    # -------------------------------------------------------------------------

    def refresh_status(
        self,
    ) -> None:

        try:
            telemetry = (
                self.shared.read_telemetry()
            )

            bulk = (
                self.shared.read_bulk_status()
            )

            stream_info = (
                self.shared.read_adc_stream_info()
            )

            self._apply_stream_rate_to_ui(
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

            total = (
                self.shared.adc_total_samples()
            )

            self.connection_label.setText(
                (
                    "Shared RAM: DATA CONNECTED"
                    if telemetry.data_connected
                    else "Shared RAM: DATA NOT CONNECTED"
                )
            )

            result = (
                self.latest_result
            )

            if result is None:
                fft_size = int(
                    self.fft_size_combo.currentData()
                    or DEFAULT_FFT_SIZE
                )

                self.fft_status_label.setText(
                    (
                        f"FFT: waiting | "
                        f"NFFT={fft_size:,} | "
                        f"Fs={self.effective_sample_rate_hz:.3f} Hz | "
                        f"ADC={total:,}"
                    )
                )

            else:
                gpu_suffix = (
                    (
                        f" | {result.gpu_name}"
                    )
                    if result.gpu_name
                    and not result.gpu_name.startswith(
                        "CUDA error:"
                    )
                    else ""
                )

                self.fft_status_label.setText(
                    (
                        f"{result.backend_name} | "
                        f"Fs {result.effective_sample_rate_hz:.3f} Hz | "
                        f"Twin {self._current_fft_settings().fft_size / max(0.001, result.effective_sample_rate_hz):.3f} s | "
                        f"Δf {(float(result.frequency_hz[1] - result.frequency_hz[0]) if len(result.frequency_hz) > 1 else 0.0):.4f} Hz | "
                        f"FFT {result.compute_ms:.2f} ms | "
                        f"copy {result.copy_ms:.2f} ms"
                        f"{gpu_suffix}"
                    )
                )

            renderer = (
                "OpenGL single-view"
                if self.opengl_active
                else "CPU/Raster"
            )

            self.render_status_label.setText(
                (
                    f"Render {self.render_fps:4.1f} FPS | "
                    f"jitter {self.render_jitter_ms:3.1f} ms | "
                    f"{renderer} | "
                    f"Fs {self.effective_sample_rate_hz:.1f} Hz "
                    f"(raw {self.raw_sample_rate_hz:.1f}/"
                    f"N{self.decimation_samples}) | "
                    f"Nyq {self.current_nyquist_hz():.1f} Hz | "
                    f"drop {bulk.dropped_frames} | "
                    f"sync {bulk.channel_id_mismatches}"
                )
            )

            tooltip = (
                f"Process executable: {sys.executable}\n"
                "FFT CUDA backend uses NVIDIA CUDA directly when CuPy is "
                "available. OpenGL rendering GPU remains selected by Windows."
            )

            if self.opengl_error:
                tooltip = (
                    self.opengl_error
                    + "\n\n"
                    + tooltip
                )

            self.render_status_label.setToolTip(
                tooltip
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
            self.render_timer.stop()
            self.status_timer.stop()
        except Exception:
            pass

        try:
            self.fft_worker.stop()
            self.fft_worker.wait(
                2500
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

            fmt.setSamples(
                0
            )

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
        window = GeophoneFFTWindow()

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            (
                "Cannot start Geophone FFT:\n\n"
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
