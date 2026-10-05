"""
geophone_spectrogram.py
=======================
GRC-UGM-PERTAMINA OBS — Real-Time Geophone Spectrogram
Version: 10
Shared data: shared_data.py


Version 10 channel-legend standardization:
- Hard-lock CH0/N = Cyan #5CC8FF, CH1/E = Green #8EE28E, CH2/Z = Yellow #FFD166.
- Add a persistent bottom channel/color legend for operator clarity.
Performance design:
- STFT runs in a dedicated QThread, never in the GUI thread.
- v4 prefers NVIDIA CUDA through the application's validated PyTorch CUDA
  runtime (torch.fft.rfft). CuPy/cuFFT is the secondary CUDA backend and
  NumPy is the CPU fallback.
- All three spectrograms share one PyQtGraph GraphicsLayoutWidget and one
  QOpenGLWidget viewport.
- New STFT columns are queued and released by a smooth presentation clock so
  128-sample OBS TCP bulk bursts do not make the waterfall jump.
- Pause freezes only the display. Acquisition/STFT continue in background.
- v4 retains the authoritative effective ADC rate from shared_data. The
  spectrogram frequency axis always defaults to the full physical Nyquist range
  of the processed shared stream (Fs/2). Examples: Fs=200 Hz -> 0..100 Hz,
  Fs=500 Hz -> 0..250 Hz, Fs=1000 Hz -> 0..500 Hz. A runtime ADC session/rate
  change clears old waterfall history and resets all three views to the new
  0..Nyquist range.
- Spectrogram time calibration uses the actual STFT hop:
      column_rate = effective_sample_rate / hop_samples
  rather than blindly assuming the requested update rate is exact.

Dependencies: PySide6, numpy, pyqtgraph.
NVIDIA GPU STFT:
    Preferred: CUDA-enabled PyTorch (torch.cuda + torch.fft)
    Optional : CuPy + NVIDIA CUDA/cuFFT

Backend order in v4:
    PyTorch CUDA -> CuPy CUDA -> NumPy CPU

UI v4:
- The complete right-side settings panel is vertically scrollable, including
  STFT engine/performance controls and all Z/N/E display controls.
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

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.SPECTROGRAM"
_WINDOWS_TIMER_ACTIVE = False


def configure_windows_runtime():
    global _WINDOWS_TIMER_ACTIVE
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00008000)  # ABOVE_NORMAL
        try:
            if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
                _WINDOWS_TIMER_ACTIVE = True
        except Exception:
            pass
        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()
    except Exception:
        pass


def release_windows_runtime():
    global _WINDOWS_TIMER_ACTIVE
    if os.name == "nt" and _WINDOWS_TIMER_ACTIVE:
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        _WINDOWS_TIMER_ACTIVE = False


configure_windows_runtime()

from PySide6.QtCore import QRectF, Qt, QThread, Signal, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont, QIcon, QKeySequence, QSurfaceFormat
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSplitter, QVBoxLayout, QWidget,
    QToolButton,
)

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception:
    QOpenGLWidget = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pyqtgraph as pg
except ImportError:
    pg = None

from shared_data import RAW_ADC_SAMPLE_RATE_HZ, OBSSharedData

APP_TITLE = "Geophone Spectrogram v10"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"
BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"


# Canonical geophone channel palette used across all OBS geophone modules.
CHANNEL_COLOR_N = "#5CC8FF"  # CH0 / North / Cyan
CHANNEL_COLOR_E = "#8EE28E"  # CH1 / East  / Green
CHANNEL_COLOR_Z = "#FFD166"  # CH2 / Vertical / Yellow
CHANNEL_MAPPING_TEXT = "CH0 = N = Cyan   •   CH1 = E = Green   •   CH2 = Z = Yellow"


CHANNELS = (
    ("CH0", "Geophone N", "ch0"),
    ("CH1", "Geophone E", "ch1"),
    ("CH2", "Geophone Z", "ch2"),
)
DISPLAY_ORDER = (2, 0, 1)
CHANNEL_COLORS = (CHANNEL_COLOR_N, CHANNEL_COLOR_E, CHANNEL_COLOR_Z)
FFT_SIZES = (128, 256, 512, 1024, 2048, 4096, 8192)
WINDOW_TYPES = ("Hann", "Hamming", "Blackman", "Rectangular")
UPDATE_RATES = (2, 5, 8, 10, 15, 20, 25, 30)
HISTORY_OPTIONS = (10, 20, 30, 60, 120)
RENDER_FPS_OPTIONS = (30, 45, 60, 75, 90)
BUFFER_OPTIONS_MS = (0, 250, 500, 1000, 1500, 2000, 3000)
COLORMAPS = ("viridis", "plasma", "inferno", "magma", "CET-L9", "CET-D1")

# A responsive default for the common v5 configuration Fs=200 Hz:
# 512 samples = 2.56 s analysis window, 0.390625 Hz/bin.
DEFAULT_FFT_SIZE = 512
DEFAULT_WINDOW = "Hann"
DEFAULT_UPDATE_HZ = 10
DEFAULT_HISTORY_S = 30
DEFAULT_RENDER_FPS = 60
DEFAULT_BUFFER_MS = 1000
DEFAULT_COLORMAP = "viridis"
DEFAULT_FREQ_MIN = 0.0
# Frequency display defaults to the complete physical one-sided bandwidth.
# The actual maximum is calculated dynamically as effective Fs / 2.
DEFAULT_COLOR_MIN = -20.0
DEFAULT_COLOR_MAX = 120.0
STATUS_INTERVAL_MS = 500
WORKER_POLL_MS = 4
MAX_BATCH_COLUMNS = 64
RATE_WINDOW_S = 5.0
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0
EPSILON = 1e-20



def make_channel_mapping_footer() -> QLabel:
    """Create the canonical OBS geophone channel/color legend."""
    label = QLabel(
        "<span style='color:#5CC8FF; font-weight:700;'>CH0 = N = Cyan</span>"
        "<span style='color:#78909C;'> &nbsp;•&nbsp; </span>"
        "<span style='color:#8EE28E; font-weight:700;'>CH1 = E = Green</span>"
        "<span style='color:#78909C;'> &nbsp;•&nbsp; </span>"
        "<span style='color:#FFD166; font-weight:700;'>CH2 = Z = Yellow</span>"
    )
    label.setAlignment(Qt.AlignCenter)
    label.setTextFormat(Qt.RichText)
    label.setMinimumHeight(26)
    label.setStyleSheet(
        "QLabel {"
        " background-color: #081923;"
        " border: 1px solid #17384A;"
        " border-radius: 5px;"
        " padding: 4px 10px;"
        " font-size: 10px;"
        "}"
    )
    label.setToolTip(
        "Canonical geophone mapping: "
        "CH0/N = Cyan, CH1/E = Green, CH2/Z = Yellow."
    )
    return label


def application_icon() -> QIcon:
    for p in ((APP_ICON_ICO, APP_ICON_PNG) if os.name == "nt" else (APP_ICON_PNG, APP_ICON_ICO)):
        if p.is_file():
            icon = QIcon(str(p))
            if not icon.isNull():
                return icon
    return QIcon()


def detect_cuda_compute():
    """
    Detect a CUDA backend by executing a real tiny CUDA workload.

    Preference:
        1. PyTorch CUDA
        2. CuPy CUDA
        3. NumPy CPU

    Returns:
        available, description, device_count, preferred_backend
    """

    errors = []

    # ------------------------------------------------------------------
    # PyTorch CUDA
    # ------------------------------------------------------------------
    try:
        import torch

        if not torch.cuda.is_available():
            errors.append(
                "PyTorch installed but torch.cuda.is_available() is False"
            )
        else:
            count = int(
                torch.cuda.device_count()
            )

            if count <= 0:
                errors.append(
                    "PyTorch CUDA reports zero devices"
                )
            else:
                # Do a real CUDA calculation; this catches missing CUDA DLLs
                # or runtime problems that a simple import may not expose.
                with torch.cuda.device(0):
                    probe = torch.tensor(
                        [1.0, 2.0, 3.0],
                        dtype=torch.float32,
                        device="cuda:0",
                    )
                    probe = (
                        probe
                        * probe
                    ).sum()

                    torch.cuda.synchronize(0)

                    value = float(
                        probe.detach().cpu().item()
                    )

                if abs(
                    value
                    - 14.0
                ) > 1.0e-5:
                    raise RuntimeError(
                        "unexpected PyTorch CUDA probe result"
                    )

                names = [
                    torch.cuda.get_device_name(
                        device_id
                    )
                    for device_id in range(
                        count
                    )
                ]

                torch_cuda = getattr(
                    torch.version,
                    "cuda",
                    None,
                )

                description = (
                    "PyTorch CUDA available | "
                    + " | ".join(
                        names
                    )
                    + f" | torch {torch.__version__}"
                    + (
                        f" | CUDA {torch_cuda}"
                        if torch_cuda
                        else ""
                    )
                )

                return (
                    True,
                    description,
                    count,
                    "torch",
                )

    except Exception as exc:
        errors.append(
            f"PyTorch CUDA: {exc}"
        )

    # ------------------------------------------------------------------
    # CuPy CUDA
    # ------------------------------------------------------------------
    try:
        import cupy as cp

        count = int(
            cp.cuda.runtime.getDeviceCount()
        )

        if count <= 0:
            errors.append(
                "CuPy installed but reports zero CUDA devices"
            )
        else:
            with cp.cuda.Device(0):
                probe = cp.asarray(
                    [1.0, 2.0, 3.0],
                    dtype=cp.float32,
                )

                value = float(
                    cp.asnumpy(
                        cp.sum(
                            probe * probe
                        )
                    )
                )

            if abs(
                value
                - 14.0
            ) > 1.0e-5:
                raise RuntimeError(
                    "unexpected CuPy CUDA probe result"
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

                raw = props.get(
                    "name",
                    b"NVIDIA CUDA GPU",
                )

                name = (
                    raw.decode(
                        "utf-8",
                        errors="replace",
                    )
                    if isinstance(
                        raw,
                        bytes,
                    )
                    else str(
                        raw
                    )
                )

                names.append(
                    name
                )

            return (
                True,
                (
                    "CuPy CUDA available | "
                    + " | ".join(
                        names
                    )
                ),
                count,
                "cupy",
            )

    except Exception as exc:
        errors.append(
            f"CuPy CUDA: {exc}"
        )

    return (
        False,
        (
            "CUDA compute unavailable | "
            + " ; ".join(
                errors
            )
        ),
        0,
        "numpy",
    )


def cuda_device_names(
    preferred_backend: str,
):
    """
    Return CUDA device display names for the backend that passed the probe.
    """

    if preferred_backend == "torch":
        try:
            import torch

            return [
                torch.cuda.get_device_name(
                    device_id
                )
                for device_id in range(
                    int(
                        torch.cuda.device_count()
                    )
                )
            ]

        except Exception:
            return []

    if preferred_backend == "cupy":
        try:
            import cupy as cp

            names = []

            for device_id in range(
                int(
                    cp.cuda.runtime.getDeviceCount()
                )
            ):
                props = (
                    cp.cuda.runtime.getDeviceProperties(
                        device_id
                    )
                )

                raw = props.get(
                    "name",
                    b"NVIDIA CUDA GPU",
                )

                names.append(
                    raw.decode(
                        "utf-8",
                        errors="replace",
                    )
                    if isinstance(
                        raw,
                        bytes,
                    )
                    else str(
                        raw
                    )
                )

            return names

        except Exception:
            return []

    return []


def np_window(name: str, n: int):
    key = name.strip().lower()
    if key == "hann":
        return np.hanning(n).astype(np.float32, copy=False)
    if key == "hamming":
        return np.hamming(n).astype(np.float32, copy=False)
    if key == "blackman":
        return np.blackman(n).astype(np.float32, copy=False)
    return np.ones(n, dtype=np.float32)




# =============================================================================
# Reliable double spin box
# =============================================================================

class ReliableDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox with explicit UP/DOWN child buttons.

    The native QAbstractSpinBox arrow subcontrols are intentionally disabled.
    On the OBS Windows/PySide6 UI the platform style can lay out those native
    subcontrols differently from the logical hit area (the UP glyph can be
    drawn to the left while the DOWN control owns the rightmost band).  That
    makes mousePressEvent hit-zone workarounds unreliable.

    This implementation uses two real QToolButton children instead:

        upper button -> stepUp()
        lower button -> stepDown()

    Keyboard entry, Up/Down keys, mouse wheel, decimals, range, suffix,
    valueChanged signals and all ordinary QDoubleSpinBox semantics remain
    provided by QDoubleSpinBox itself.
    """

    _BUTTON_WIDTH = 26

    def __init__(self, parent=None):
        super().__init__(parent)

        # Remove platform/native arrows completely.  The explicit child
        # buttons below are the only mouse buttons rendered for stepping.
        self.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        self.setAccelerated(True)

        self._spin_up_button = QToolButton(self)
        self._spin_down_button = QToolButton(self)

        self._spin_up_button.setText("▲")
        self._spin_down_button.setText("▼")

        for button in (
            self._spin_up_button,
            self._spin_down_button,
        ):
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(350)
            button.setAutoRepeatInterval(75)
            button.setStyleSheet(
                """
                QToolButton {
                    background-color: #0E2533;
                    color: #DDEAF2;
                    border: 0px;
                    border-left: 1px solid #24485D;
                    border-radius: 0px;
                    padding: 0px;
                    margin: 0px;
                    font-size: 9px;
                }
                QToolButton:hover {
                    background-color: #17384B;
                    color: #FFFFFF;
                }
                QToolButton:pressed {
                    background-color: #245B79;
                    color: #FFFFFF;
                }
                QToolButton:disabled {
                    color: #526B78;
                    background-color: #0A1A24;
                }
                """
            )

        # A divider makes the two explicit buttons visually unambiguous.
        self._spin_down_button.setStyleSheet(
            self._spin_down_button.styleSheet()
            + "QToolButton { border-top: 1px solid #24485D; }"
        )

        self._spin_up_button.clicked.connect(
            self._explicit_step_up
        )
        self._spin_down_button.clicked.connect(
            self._explicit_step_down
        )

        self._layout_spin_buttons()

    def _explicit_step_up(self) -> None:
        if self.isEnabled() and not self.isReadOnly():
            self.stepUp()

    def _explicit_step_down(self) -> None:
        if self.isEnabled() and not self.isReadOnly():
            self.stepDown()

    def _layout_spin_buttons(self) -> None:
        width = min(
            self._BUTTON_WIDTH,
            max(20, self.height() - 2),
        )
        height = max(2, self.height() - 2)
        upper_height = max(1, height // 2)
        lower_height = max(1, height - upper_height)
        x = max(0, self.width() - width - 1)

        self._spin_up_button.setGeometry(
            x,
            1,
            width,
            upper_height,
        )
        self._spin_down_button.setGeometry(
            x,
            1 + upper_height,
            width,
            lower_height,
        )

        edit = self.lineEdit()
        if edit is not None:
            edit.setTextMargins(
                0,
                0,
                width + 3,
                0,
            )

        # Keep the explicit controls above the line editor and style painting.
        self._spin_up_button.raise_()
        self._spin_down_button.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_spin_buttons()

    def setReadOnly(self, read_only: bool) -> None:
        super().setReadOnly(read_only)
        enabled = not bool(read_only)
        up_button = getattr(self, "_spin_up_button", None)
        down_button = getattr(self, "_spin_down_button", None)
        if up_button is not None:
            up_button.setEnabled(enabled)
        if down_button is not None:
            down_button.setEnabled(enabled)


@dataclass(frozen=True)
class STFTSettings:
    fft_size: int
    window: str
    update_hz: int
    remove_dc: bool
    backend: str
    cuda_device: int


@dataclass(frozen=True)
class STFTBatch:
    total_samples: int

    raw_sample_rate_hz: float
    effective_sample_rate_hz: float
    decimation_samples: int
    decimation_mode: str
    adc_session_id: int

    hop_samples: int
    column_rate_hz: float

    frequency_hz: object
    columns_db: object  # [n_columns, 3, n_freq]

    backend_name: str
    gpu_name: str
    compute_ms: float
    copy_ms: float
    producer_rate_hz: float
    publish_gap_ms: float
    skipped_columns: int


class STFTWorker(QThread):
    batch_ready = Signal(object)
    status = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._settings = STFTSettings(
            DEFAULT_FFT_SIZE, DEFAULT_WINDOW, DEFAULT_UPDATE_HZ, True,
            "Auto (CUDA preferred)", 0,
        )
        self._generation = 0

    def stop(self):
        self._stop.set()

    def set_settings(self, value: STFTSettings):
        with self._lock:
            self._settings = value
            self._generation += 1

    def get_settings(self):
        with self._lock:
            return self._settings, self._generation

    @staticmethod
    def resolve_backend(requested: str):
        requested = str(
            requested
        )

        if requested.startswith(
            "CPU"
        ):
            return "numpy"

        if requested.startswith(
            "CUDA / PyTorch"
        ):
            return "torch"

        if requested.startswith(
            "CUDA / CuPy"
        ):
            return "cupy"

        # Compatibility with an older generic CUDA label.
        if (
            requested.startswith(
                "CUDA"
            )
            and "PyTorch" not in requested
            and "CuPy" not in requested
        ):
            try:
                import torch

                if (
                    torch.cuda.is_available()
                    and int(
                        torch.cuda.device_count()
                    ) > 0
                ):
                    return "torch"

            except Exception:
                pass

            return "cupy"

        # Auto: prefer the CUDA runtime that is already validated elsewhere
        # in the OBS application: PyTorch CUDA.
        try:
            import torch

            if (
                torch.cuda.is_available()
                and int(
                    torch.cuda.device_count()
                ) > 0
            ):
                return "torch"

        except Exception:
            pass

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
    def compute_numpy(frames, settings: STFTSettings):
        n = settings.fft_size
        win = np_window(settings.window, n)
        x = frames.astype(np.float32, copy=True)
        if settings.remove_dc:
            x -= np.mean(x, axis=2, keepdims=True, dtype=np.float32)
        x *= win[np.newaxis, np.newaxis, :]
        spectrum = np.fft.rfft(x, axis=2)
        amp = np.abs(spectrum).astype(np.float32, copy=False) / max(float(np.sum(win, dtype=np.float64)), EPSILON)
        if amp.shape[-1] > 1:
            amp[:, :, 1:] *= 2.0
            if n % 2 == 0:
                amp[:, :, -1] *= 0.5
        return (20.0 * np.log10(np.maximum(amp, EPSILON))).astype(np.float32, copy=False)

    @staticmethod
    def compute_torch(
        frames,
        settings: STFTSettings,
    ):
        """
        Batch STFT through PyTorch CUDA.

        Input shape:
            [n_columns, 3, fft_size]

        Only the final dB matrix is copied back to host memory.
        """

        import torch

        n = int(
            settings.fft_size
        )

        device_id = int(
            settings.cuda_device
        )

        device = torch.device(
            f"cuda:{device_id}"
        )

        gpu_name = (
            torch.cuda.get_device_name(
                device_id
            )
        )

        # Host -> GPU transfer.
        t0 = time.perf_counter()

        x = torch.as_tensor(
            frames,
            dtype=torch.float32,
            device=device,
        )

        torch.cuda.synchronize(
            device_id
        )

        copy_in_ms = (
            time.perf_counter()
            - t0
        ) * 1000.0

        key = (
            settings.window
            .strip()
            .lower()
        )

        # periodic=False matches NumPy's np.hanning / np.hamming / np.blackman
        # convention used by the CPU path.
        if key == "hann":
            win = torch.hann_window(
                n,
                periodic=False,
                dtype=torch.float32,
                device=device,
            )

        elif key == "hamming":
            win = torch.hamming_window(
                n,
                periodic=False,
                dtype=torch.float32,
                device=device,
            )

        elif key == "blackman":
            win = torch.blackman_window(
                n,
                periodic=False,
                dtype=torch.float32,
                device=device,
            )

        else:
            win = torch.ones(
                n,
                dtype=torch.float32,
                device=device,
            )

        # GPU compute.
        t1 = time.perf_counter()

        if settings.remove_dc:
            x = (
                x
                - torch.mean(
                    x,
                    dim=2,
                    keepdim=True,
                )
            )

        x = (
            x
            * win.view(
                1,
                1,
                n,
            )
        )

        spectrum = torch.fft.rfft(
            x,
            dim=2,
        )

        window_sum = torch.sum(
            win,
            dtype=torch.float64,
        )

        amp = (
            torch.abs(
                spectrum
            )
            / torch.clamp(
                window_sum,
                min=EPSILON,
            )
        )

        if amp.shape[-1] > 1:
            amp[
                :,
                :,
                1:
            ] *= 2.0

            if (
                n % 2 == 0
                and amp.shape[-1] >= 2
            ):
                amp[
                    :,
                    :,
                    -1
                ] *= 0.5

        db_gpu = (
            20.0
            * torch.log10(
                torch.clamp(
                    amp,
                    min=EPSILON,
                )
            )
        ).to(
            dtype=torch.float32
        )

        torch.cuda.synchronize(
            device_id
        )

        compute_ms = (
            time.perf_counter()
            - t1
        ) * 1000.0

        # GPU -> host transfer.
        t2 = time.perf_counter()

        db = (
            db_gpu
            .detach()
            .cpu()
            .numpy()
        )

        copy_out_ms = (
            time.perf_counter()
            - t2
        ) * 1000.0

        return (
            db,
            compute_ms,
            copy_in_ms
            + copy_out_ms,
            gpu_name,
        )


    @staticmethod
    def compute_cupy(frames, settings: STFTSettings):
        import cupy as cp
        n = settings.fft_size
        with cp.cuda.Device(settings.cuda_device):
            props = cp.cuda.runtime.getDeviceProperties(settings.cuda_device)
            raw = props.get("name", b"NVIDIA CUDA GPU")
            gpu_name = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

            t0 = time.perf_counter()
            x = cp.asarray(frames, dtype=cp.float32)
            cp.cuda.get_current_stream().synchronize()
            copy_in_ms = (time.perf_counter() - t0) * 1000.0

            key = settings.window.strip().lower()
            if key == "hann":
                win = cp.hanning(n).astype(cp.float32)
            elif key == "hamming":
                win = cp.hamming(n).astype(cp.float32)
            elif key == "blackman":
                win = cp.blackman(n).astype(cp.float32)
            else:
                win = cp.ones(n, dtype=cp.float32)

            t1 = time.perf_counter()
            if settings.remove_dc:
                x -= cp.mean(x, axis=2, keepdims=True)
            x *= win[cp.newaxis, cp.newaxis, :]
            spectrum = cp.fft.rfft(x, axis=2)
            amp = cp.abs(spectrum) / cp.maximum(cp.sum(win, dtype=cp.float64), EPSILON)
            if amp.shape[-1] > 1:
                amp[:, :, 1:] *= 2.0
                if n % 2 == 0:
                    amp[:, :, -1] *= 0.5
            db_gpu = (20.0 * cp.log10(cp.maximum(amp, EPSILON))).astype(cp.float32)
            cp.cuda.get_current_stream().synchronize()
            compute_ms = (time.perf_counter() - t1) * 1000.0

            t2 = time.perf_counter()
            db = cp.asnumpy(db_gpu)
            copy_out_ms = (time.perf_counter() - t2) * 1000.0
            return db, compute_ms, copy_in_ms + copy_out_ms, gpu_name

    def run(self):
        shared = None
        next_end = None
        active_generation = -1
        rate_hist = deque()
        producer_rate = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        last_publish = None
        skipped_total = 0

        last_session_id = -1
        last_effective_rate_hz = -1.0

        try:
            shared = OBSSharedData()
            self.status.emit(
                "Spectrogram worker attached to shared_data RAM"
            )

            while not self._stop.is_set():
                settings, generation = self.get_settings()

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
                    != last_session_id
                    or abs(
                        effective_rate_hz
                        - last_effective_rate_hz
                    )
                    > max(
                        1.0e-9,
                        1.0e-6
                        * effective_rate_hz,
                    )
                )

                if stream_changed:
                    last_session_id = session_id
                    last_effective_rate_hz = (
                        effective_rate_hz
                    )

                    next_end = None
                    rate_hist.clear()
                    last_publish = None
                    skipped_total = 0
                    producer_rate = (
                        effective_rate_hz
                    )

                    self.status.emit(
                        (
                            f"ADC stream: "
                            f"{stream_info.raw_sample_rate_hz:g} Hz / "
                            f"N={stream_info.decimation_samples} -> "
                            f"{effective_rate_hz:.3f} Hz | "
                            f"session {session_id}"
                        )
                    )

                settings, generation = self.get_settings()
                n = int(
                    settings.fft_size
                )

                hop = max(
                    1,
                    int(
                        round(
                            effective_rate_hz
                            / max(
                                1,
                                settings.update_hz,
                            )
                        )
                    ),
                )

                column_rate_hz = (
                    effective_rate_hz
                    / float(
                        hop
                    )
                )

                total = (
                    shared.adc_total_samples()
                )

                if (
                    generation
                    != active_generation
                ):
                    active_generation = generation
                    next_end = None
                    skipped_total = 0

                if total < n:
                    self.status.emit(f"Waiting for {n:,} ADC samples ({total:,} available)")
                    self.msleep(20)
                    continue

                if next_end is None:
                    next_end = int(total - 1)

                if next_end >= total:
                    self.msleep(WORKER_POLL_MS)
                    continue

                available = ((int(total - 1) - next_end) // hop) + 1
                if available <= 0:
                    self.msleep(WORKER_POLL_MS)
                    continue

                if available > MAX_BATCH_COLUMNS:
                    skip = available - MAX_BATCH_COLUMNS
                    next_end += skip * hop
                    skipped_total += skip
                    available = MAX_BATCH_COLUMNS

                ends = next_end + np.arange(available, dtype=np.int64) * hop
                earliest = int(ends[0] - n + 1)
                latest = int(ends[-1])
                need = max(n, latest - earliest + 1 + 8)
                snap = shared.read_adc_latest_numpy(need)
                count = len(snap.ch0)
                if count < n:
                    self.msleep(WORKER_POLL_MS)
                    continue

                snap_start = int(snap.total_samples) - count
                if earliest < snap_start:
                    next_end = int(snap.total_samples - 1)
                    skipped_total += available
                    continue

                frames = np.empty((available, 3, n), dtype=np.float32)
                src = (snap.ch0, snap.ch1, snap.ch2)
                valid = True
                for ci, end_sample in enumerate(ends):
                    local_start = int(end_sample - n + 1 - snap_start)
                    local_end = local_start + n
                    if local_start < 0 or local_end > count:
                        valid = False
                        break
                    frames[ci, 0] = src[0][local_start:local_end]
                    frames[ci, 1] = src[1][local_start:local_end]
                    frames[ci, 2] = src[2][local_start:local_end]
                if not valid:
                    self.msleep(WORKER_POLL_MS)
                    continue

                backend = self.resolve_backend(settings.backend)
                gpu_name = ""
                copy_ms = 0.0
                try:
                    if backend == "torch":
                        (
                            db,
                            compute_ms,
                            copy_ms,
                            gpu_name,
                        ) = self.compute_torch(
                            frames,
                            settings,
                        )
                        backend_name = "CUDA / PyTorch / torch.fft"

                    elif backend == "cupy":
                        (
                            db,
                            compute_ms,
                            copy_ms,
                            gpu_name,
                        ) = self.compute_cupy(
                            frames,
                            settings,
                        )
                        backend_name = "CUDA / CuPy / cuFFT"

                    else:
                        t0 = time.perf_counter()
                        db = self.compute_numpy(frames, settings)
                        compute_ms = (time.perf_counter() - t0) * 1000.0
                        backend_name = "CPU / NumPy STFT"
                except Exception as cuda_exc:
                    t0 = time.perf_counter()
                    db = self.compute_numpy(frames, settings)
                    compute_ms = (time.perf_counter() - t0) * 1000.0
                    backend_name = "CPU fallback / NumPy STFT"
                    gpu_name = f"CUDA error: {cuda_exc}"

                sample_rate_hz = max(
                    0.001,
                    float(
                        snap.sample_rate_hz
                    ),
                )

                freq = (
                    np.fft.rfftfreq(
                        n,
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
                now = time.perf_counter()
                gap_ms = 0.0 if last_publish is None else (now - last_publish) * 1000.0
                last_publish = now

                rate_hist.append((now, int(snap.total_samples)))
                cutoff = now - RATE_WINDOW_S
                while len(rate_hist) > 2 and rate_hist[0][0] < cutoff:
                    rate_hist.popleft()
                if len(rate_hist) >= 2:
                    t0, n0 = rate_hist[0]
                    t1, n1 = rate_hist[-1]
                    dt = t1 - t0
                    dn = n1 - n0
                    if dt >= 1.0 and dn > 0:
                        measured = dn / dt
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
                            producer_rate = (
                                0.8
                                * producer_rate
                                + 0.2
                                * measured
                            )

                self.batch_ready.emit(STFTBatch(
                    total_samples=int(
                        snap.total_samples
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
                    hop_samples=int(
                        hop
                    ),
                    column_rate_hz=float(
                        column_rate_hz
                    ),
                    frequency_hz=freq,
                    columns_db=db.astype(np.float32, copy=False),
                    backend_name=backend_name,
                    gpu_name=str(gpu_name),
                    compute_ms=float(compute_ms),
                    copy_ms=float(copy_ms),
                    producer_rate_hz=float(producer_rate),
                    publish_gap_ms=float(gap_ms),
                    skipped_columns=int(skipped_total),
                ))
                next_end = int(ends[-1] + hop)

        except Exception as exc:
            if not self._stop.is_set():
                self.error.emit(str(exc))
        finally:
            if shared is not None:
                try:
                    shared.close()
                except Exception:
                    pass


@dataclass
class ChannelControl:
    index: int
    group: QGroupBox
    freq_min: QDoubleSpinBox
    freq_max: QDoubleSpinBox
    color_min: QDoubleSpinBox
    color_max: QDoubleSpinBox
    auto_color: QCheckBox


class GeophoneSpectrogramWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        if np is None or pg is None:
            raise RuntimeError("NumPy and PyQtGraph are required")

        self.shared = OBSSharedData()

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

        self.nominal_column_rate_hz = float(
            DEFAULT_UPDATE_HZ
        )

        (
            self.cuda_available,
            self.cuda_description,
            self.cuda_count,
            self.cuda_preferred_backend,
        ) = detect_cuda_compute()

        self.paused = False
        self.pending = deque()              # each entry shape [3, n_freq]
        self.history = deque()              # rolling displayed columns
        self.frequency_hz = None
        self.latest_batch = None
        self.producer_rate_hz = float(
            self.effective_sample_rate_hz
        )
        self.publish_gap_ms = 0.0
        self.display_rate_hz = float(DEFAULT_UPDATE_HZ)
        self._column_accum = 0.0
        self._last_release = time.perf_counter()
        self._presentation_started = False
        self.underruns = 0
        self._in_underrun = False

        self.render_fps = 0.0
        self.render_jitter_ms = 0.0
        self._fps_count = 0
        self._fps_start = time.perf_counter()
        self._last_render_ns = None
        self.opengl_active = False
        self.opengl_error = ""

        self.plots = [None, None, None]
        self.images = [None, None, None]
        self.controls = []

        self.setWindowTitle(f"{APP_TITLE} - {SYSTEM_TITLE}")
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1450, 860)
        self.setMinimumSize(1050, 650)

        self.configure_pg()
        self.build_ui()
        self.apply_style()
        self.install_shortcuts()

        self.worker = STFTWorker(self)
        self.worker.batch_ready.connect(self.on_batch)
        self.worker.status.connect(self.compute_label.setText)
        self.worker.error.connect(lambda m: self.compute_label.setText(f"STFT error: {m}"))
        self.push_worker_settings()
        self.worker.start()

        self.render_timer = QTimer(self)
        try:
            self.render_timer.setTimerType(Qt.TimerType.PreciseTimer)
        except Exception:
            pass
        self.render_timer.timeout.connect(self.render_frame)
        self.set_render_fps(DEFAULT_RENDER_FPS)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(STATUS_INTERVAL_MS)
        self.refresh_status()

    @staticmethod
    def configure_pg():
        try:
            pg.setConfigOptions(useOpenGL=True, antialias=False,
                                background="#07131D", foreground="#DDEAF2",
                                imageAxisOrder="row-major")
        except Exception:
            pg.setConfigOptions(useOpenGL=False, antialias=False,
                                background="#07131D", foreground="#DDEAF2",
                                imageAxisOrder="row-major")

    def install_gl(self, graphics):
        if QOpenGLWidget is None:
            self.opengl_error = "QOpenGLWidget unavailable"
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
            self.opengl_active = isinstance(graphics.viewport(), QOpenGLWidget)
        except Exception as exc:
            self.opengl_error = str(exc)

    def build_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("GEOPHONE SPECTROGRAM")
        title.setObjectName("titleLabel")
        subtitle = QLabel("Time-Frequency Waterfall • CH2/Z • CH0/N • CH1/E • CUDA/cuFFT preferred")
        subtitle.setObjectName("subtitleLabel")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("pauseButton")
        self.pause_button.setCheckable(True)
        self.pause_button.setMinimumWidth(125)
        self.pause_button.clicked.connect(self.toggle_pause)
        header.addWidget(self.pause_button)
        root.addLayout(header)

        sf = QFrame()
        sf.setObjectName("statusFrame")
        sl = QHBoxLayout(sf)
        sl.setContentsMargins(10, 6, 10, 6)
        self.connection_label = QLabel("Shared RAM: checking...")
        self.compute_label = QLabel("STFT: waiting...")
        self.render_label = QLabel("Render: --")
        self.mode_label = QLabel("LIVE")
        for w in (self.connection_label, self.compute_label, self.render_label):
            w.setObjectName("statusLabel")
        self.mode_label.setObjectName("modeLive")
        sl.addWidget(self.connection_label)
        sl.addStretch(1)
        sl.addWidget(self.compute_label)
        sl.addSpacing(12)
        sl.addWidget(self.render_label)
        sl.addSpacing(12)
        sl.addWidget(self.mode_label)
        root.addWidget(sf)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.build_plot_panel())
        splitter.addWidget(self.build_settings_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1080, 360])
        root.addWidget(splitter, 1)
        root.addWidget(
            make_channel_mapping_footer()
        )


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
        # Full one-sided bandwidth of the currently published ADC stream.
        # Never keep a fixed 100-Hz ceiling when the effective rate is higher.
        return self.current_nyquist_hz()

    def current_nominal_column_rate_hz(
        self,
    ) -> float:
        if (
            self.latest_batch
            is not None
            and float(
                self.latest_batch.column_rate_hz
            ) > 0.0
        ):
            return float(
                self.latest_batch.column_rate_hz
            )

        requested = max(
            1,
            self.current_update_hz()
            if hasattr(
                self,
                "update_combo",
            )
            else DEFAULT_UPDATE_HZ,
        )

        hop = max(
            1,
            int(
                round(
                    self.current_sample_rate_hz()
                    / requested
                )
            ),
        )

        return (
            self.current_sample_rate_hz()
            / float(
                hop
            )
        )

    def update_fft_labels(
        self,
    ) -> None:

        if not hasattr(
            self,
            "fft_combo",
        ):
            return

        fs = (
            self.current_sample_rate_hz()
        )

        for index in range(
            self.fft_combo.count()
        ):
            n = int(
                self.fft_combo.itemData(
                    index
                )
            )

            history_s = (
                n
                / fs
            )

            resolution = (
                fs
                / n
            )

            self.fft_combo.setItemText(
                index,
                (
                    f"{n:,} "
                    f"({history_s:.3f} s • "
                    f"{resolution:.4f} Hz/bin)"
                ),
            )

        if hasattr(
            self,
            "stft_info",
        ):
            n = (
                self.current_fft_size()
            )

            hop = max(
                1,
                int(
                    round(
                        fs
                        / max(
                            1,
                            self.current_update_hz()
                        )
                    )
                ),
            )

            column_rate = (
                fs
                / hop
            )

            overlap = max(
                0.0,
                100.0
                * (
                    1.0
                    - hop
                    / float(
                        n
                    )
                ),
            )

            self.stft_info.setText(
                (
                    f"Fs={fs:.3f} Hz • "
                    f"Nyquist={fs/2.0:.3f} Hz • "
                    f"NFFT={n:,} ({n/fs:.3f} s) • "
                    f"hop={hop} samples • "
                    f"column={column_rate:.3f} Hz • "
                    f"overlap={overlap:.1f}%"
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

        changed = (
            old_session
            != self.adc_session_id
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

        self.update_fft_labels()

        if hasattr(
            self,
            "controls",
        ):
            nyquist = (
                self.current_nyquist_hz()
            )
            default_max = (
                self.default_frequency_max_hz()
            )

            for ctrl in self.controls:
                ctrl.freq_min.setRange(
                    0.0,
                    nyquist,
                )
                ctrl.freq_max.setRange(
                    0.0,
                    nyquist,
                )

                if changed:
                    # A different decimation/sample-rate means a different
                    # physical bandwidth. Reset the view so, for example,
                    # 500-Hz output immediately becomes a 0..250-Hz plot
                    # instead of retaining an old 0..100-Hz view.
                    ctrl.freq_min.setValue(
                        DEFAULT_FREQ_MIN
                    )
                    ctrl.freq_max.setValue(
                        default_max
                    )
                else:
                    # Preserve a user's manual zoom while the stream
                    # configuration is unchanged, but keep it physically valid.
                    if (
                        ctrl.freq_min.value()
                        >= nyquist
                    ):
                        ctrl.freq_min.setValue(
                            DEFAULT_FREQ_MIN
                        )

                    if (
                        ctrl.freq_max.value()
                        > nyquist
                        or ctrl.freq_max.value()
                        <= ctrl.freq_min.value()
                    ):
                        ctrl.freq_max.setValue(
                            default_max
                        )

        if changed:
            self.clear_history()

            if hasattr(
                self,
                "plots",
            ):
                for plot in self.plots:
                    plot.setYRange(
                        DEFAULT_FREQ_MIN,
                        self.default_frequency_max_hz(),
                        padding=0.0,
                    )

    def build_plot_panel(self):
        panel = QFrame()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.graphics = pg.GraphicsLayoutWidget()
        self.install_gl(self.graphics)
        layout.addWidget(self.graphics, 1)

        for row, channel_index in enumerate(DISPLAY_ORDER):
            ch, axis, _ = CHANNELS[channel_index]
            plot = self.graphics.addPlot(row=row, col=0)
            plot.showGrid(x=True, y=True, alpha=0.12)
            plot.setMouseEnabled(x=True, y=True)
            plot.setLabel("left", "Frequency", units="Hz")
            plot.setLabel("bottom", "Time", units="s")
            plot.setTitle(f"{ch} — {axis}", color=CHANNEL_COLORS[channel_index], size="11pt")
            plot.setXRange(-DEFAULT_HISTORY_S, 0.0, padding=0.0)
            plot.setYRange(DEFAULT_FREQ_MIN, self.default_frequency_max_hz(), padding=0.0)
            image = pg.ImageItem(axisOrder="row-major")
            plot.addItem(image)
            self.plots[channel_index] = plot
            self.images[channel_index] = image

        self.apply_colormap(DEFAULT_COLORMAP)
        return panel

    def build_settings_panel(self):
        outer = QFrame()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(8, 0, 0, 0)
        outer_layout.setSpacing(6)

        h = QLabel("SPECTROGRAM SETTINGS")
        h.setObjectName("settingsTitle")
        outer_layout.addWidget(h)

        # v4: scroll the complete settings body, including compute controls.
        scroll = QScrollArea()
        scroll.setObjectName("settingsPanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        content = QWidget()
        content.setObjectName("settingsScroll")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(8)

        group = QGroupBox("STFT Compute / Performance")
        group.setObjectName("channelGroup")
        g = QGridLayout(group)

        self.backend_combo = QComboBox()
        self.backend_combo.addItems(
            [
                "Auto (CUDA preferred)",
                "CUDA / PyTorch / torch.fft",
                "CUDA / CuPy / cuFFT",
                "CPU / NumPy STFT",
            ]
        )
        self.backend_combo.currentIndexChanged.connect(self.on_analysis_changed)

        self.cuda_combo = QComboBox()
        if self.cuda_available:
            names = cuda_device_names(self.cuda_preferred_backend)
            if names:
                for device_id, name in enumerate(names):
                    self.cuda_combo.addItem(
                        f"CUDA {device_id}: {name}",
                        device_id,
                    )
            else:
                for device_id in range(self.cuda_count):
                    self.cuda_combo.addItem(
                        f"CUDA device {device_id}",
                        device_id,
                    )
        else:
            self.cuda_combo.addItem("CUDA unavailable", 0)
            self.cuda_combo.setEnabled(False)
        self.cuda_combo.currentIndexChanged.connect(self.on_analysis_changed)

        self.fft_combo = QComboBox()
        for n in FFT_SIZES:
            fs = self.current_sample_rate_hz()
            self.fft_combo.addItem(
                (
                    f"{n:,} "
                    f"({n/fs:.3f} s • "
                    f"{fs/n:.4f} Hz/bin)"
                ),
                n,
            )
        self.fft_combo.setCurrentIndex(
            max(0, self.fft_combo.findData(DEFAULT_FFT_SIZE))
        )
        self.fft_combo.currentIndexChanged.connect(self.on_analysis_changed)

        self.window_combo = QComboBox()
        self.window_combo.addItems(list(WINDOW_TYPES))
        self.window_combo.setCurrentText(DEFAULT_WINDOW)
        self.window_combo.currentIndexChanged.connect(self.on_analysis_changed)

        self.update_combo = QComboBox()
        for hz in UPDATE_RATES:
            self.update_combo.addItem(f"{hz} Hz", hz)
        self.update_combo.setCurrentIndex(
            max(0, self.update_combo.findData(DEFAULT_UPDATE_HZ))
        )
        self.update_combo.currentIndexChanged.connect(self.on_analysis_changed)

        self.history_combo = QComboBox()
        for s in HISTORY_OPTIONS:
            self.history_combo.addItem(f"{s} s", s)
        self.history_combo.setCurrentIndex(
            max(0, self.history_combo.findData(DEFAULT_HISTORY_S))
        )
        self.history_combo.currentIndexChanged.connect(self.on_history_changed)

        self.render_combo = QComboBox()
        for fps in RENDER_FPS_OPTIONS:
            self.render_combo.addItem(f"{fps} FPS", fps)
        self.render_combo.setCurrentIndex(
            max(0, self.render_combo.findData(DEFAULT_RENDER_FPS))
        )
        self.render_combo.currentIndexChanged.connect(
            lambda *_: self.set_render_fps(
                int(self.render_combo.currentData() or DEFAULT_RENDER_FPS)
            )
        )

        self.buffer_combo = QComboBox()
        for ms in BUFFER_OPTIONS_MS:
            self.buffer_combo.addItem(
                "Off" if ms == 0 else f"{ms} ms",
                ms,
            )
        self.buffer_combo.setCurrentIndex(
            max(0, self.buffer_combo.findData(DEFAULT_BUFFER_MS))
        )
        self.buffer_combo.currentIndexChanged.connect(
            self.reset_presentation_clock
        )

        self.colormap_combo = QComboBox()
        self.colormap_combo.addItems(list(COLORMAPS))
        self.colormap_combo.setCurrentText(DEFAULT_COLORMAP)
        self.colormap_combo.currentTextChanged.connect(self.apply_colormap)

        self.remove_dc = QCheckBox("Remove DC / Mean")
        self.remove_dc.setChecked(True)
        self.remove_dc.stateChanged.connect(self.on_analysis_changed)

        self.stft_info = QLabel("")
        self.stft_info.setObjectName("sampleInfo")
        self.stft_info.setWordWrap(True)

        rows = [
            ("STFT Engine", self.backend_combo),
            ("CUDA Device", self.cuda_combo),
            ("FFT Size", self.fft_combo),
            ("Window", self.window_combo),
            ("STFT Update", self.update_combo),
            ("History", self.history_combo),
            ("Render", self.render_combo),
            ("Smooth Buffer", self.buffer_combo),
            ("Color Map", self.colormap_combo),
        ]
        for row, (label, widget) in enumerate(rows):
            g.addWidget(QLabel(label), row, 0)
            g.addWidget(widget, row, 1)

        g.addWidget(self.remove_dc, len(rows), 0, 1, 2)
        g.addWidget(self.stft_info, len(rows) + 1, 0, 1, 2)

        self.cuda_info = QLabel(self.cuda_description)
        self.cuda_info.setObjectName("sampleInfo")
        self.cuda_info.setWordWrap(True)
        g.addWidget(self.cuda_info, len(rows) + 2, 0, 1, 2)
        self.update_fft_labels()

        layout.addWidget(group)

        for channel_index in DISPLAY_ORDER:
            ch, axis, _ = CHANNELS[channel_index]
            ctrl = self.create_channel_control(channel_index, ch, axis)
            self.controls.append(ctrl)
            layout.addWidget(ctrl.group)

        reset = QPushButton("Reset All Views")
        reset.setObjectName("secondaryButton")
        reset.clicked.connect(self.reset_all_views)
        layout.addWidget(reset)
        layout.addStretch(1)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll, 1)
        return outer

    def create_channel_control(self, index, ch, axis):
        group = QGroupBox(f"{ch} • {axis}")
        group.setObjectName("channelGroup")
        g = QGridLayout(group)

        fmin = ReliableDoubleSpinBox(); fmin.setRange(0.0, self.current_nyquist_hz()); fmin.setDecimals(2); fmin.setSuffix(" Hz"); fmin.setValue(DEFAULT_FREQ_MIN)
        fmax = ReliableDoubleSpinBox(); fmax.setRange(0.0, self.current_nyquist_hz()); fmax.setDecimals(2); fmax.setSuffix(" Hz"); fmax.setValue(self.default_frequency_max_hz())
        cmin = ReliableDoubleSpinBox(); cmin.setRange(-300.0, 300.0); cmin.setDecimals(1); cmin.setSuffix(" dB"); cmin.setValue(DEFAULT_COLOR_MIN)
        cmax = ReliableDoubleSpinBox(); cmax.setRange(-300.0, 300.0); cmax.setDecimals(1); cmax.setSuffix(" dB"); cmax.setValue(DEFAULT_COLOR_MAX)
        auto = QCheckBox("Auto Color Range")
        apply_b = QPushButton("Apply"); apply_b.setObjectName("smallPrimaryButton")
        reset_b = QPushButton("Reset"); reset_b.setObjectName("smallButton")

        for r, (label, widget) in enumerate((("Freq Min", fmin), ("Freq Max", fmax), ("Color Min", cmin), ("Color Max", cmax))):
            g.addWidget(QLabel(label), r, 0); g.addWidget(widget, r, 1, 1, 2)
        g.addWidget(auto, 4, 0, 1, 3)
        g.addWidget(apply_b, 5, 0, 1, 2); g.addWidget(reset_b, 5, 2)

        ctrl = ChannelControl(index, group, fmin, fmax, cmin, cmax, auto)
        apply_b.clicked.connect(lambda _=False, c=ctrl: self.apply_channel_view(c))
        reset_b.clicked.connect(lambda _=False, c=ctrl: self.reset_channel_view(c))
        auto.stateChanged.connect(lambda _=0, c=ctrl: self.apply_channel_view(c))
        return ctrl

    def apply_style(self):
        self.setStyleSheet(r'''
        QMainWindow, QWidget#centralWidget, QWidget#settingsScroll { background:#07131D; color:#FFFFFF; font-family:"Segoe UI","Arial"; }
        QLabel { background:transparent; color:#FFFFFF; }
        QLabel#titleLabel { font-size:20px; font-weight:800; letter-spacing:.8px; }
        QLabel#subtitleLabel { color:#A9BECA; font-size:10px; }
        QFrame#statusFrame { background:#0B1B27; border:1px solid #17374A; border-radius:8px; }
        QLabel#statusLabel { color:#B7CBD6; font-size:10px; }
        QLabel#modeLive { background:#123A2D; border:1px solid #2D8E66; border-radius:7px; color:#A9F1D2; font-weight:800; padding:3px 10px; }
        QLabel#modePaused { background:#403510; border:1px solid #A88821; border-radius:7px; color:#FFE49A; font-weight:800; padding:3px 10px; }
        QLabel#settingsTitle { color:#FFFFFF; font-size:12px; font-weight:800; letter-spacing:1px; }
        QGroupBox#channelGroup { background:#0D1E2A; border:1px solid #1A3D52; border-radius:9px; margin-top:11px; padding-top:6px; font-weight:800; color:#FFFFFF; }
        QGroupBox#channelGroup::title { subcontrol-origin:margin; left:9px; padding:0 5px; color:#FFFFFF; }
        QLabel#sampleInfo { color:#7894A4; font-size:9px; }
        QDoubleSpinBox, QComboBox { background:#071620; color:#FFFFFF; border:1px solid #24485D; border-radius:5px; min-height:25px; padding:1px 5px; }
        QComboBox QLineEdit { background:#071620; color:#FFFFFF; border:none; selection-background-color:#2B739A; selection-color:#FFFFFF; }
        QComboBox::drop-down { subcontrol-origin:padding; subcontrol-position:top right; width:24px; border-left:1px solid #24485D; background:#0E2533; }
        QComboBox QAbstractItemView { background:#0B1B26; color:#F4FAFD; border:1px solid #2B526A; selection-background-color:#245B79; selection-color:#FFFFFF; outline:none; padding:3px; }
        QComboBox QAbstractItemView::item { color:#F4FAFD; background:#0B1B26; min-height:26px; padding:4px 8px; }
        QComboBox QAbstractItemView::item:selected, QComboBox QAbstractItemView::item:hover { color:#FFFFFF; background:#245B79; }
        QCheckBox { color:#DDE9EF; spacing:6px; }
        QPushButton { min-height:28px; border-radius:6px; padding:3px 7px; font-weight:700; }
        QPushButton#pauseButton { background:#17678F; color:#FFFFFF; border:1px solid #2D8AB6; min-height:34px; }
        QPushButton#pauseButton:checked { background:#705C16; border:1px solid #B49326; }
        QPushButton#smallPrimaryButton { background:#17678F; color:#FFFFFF; border:1px solid #2D8AB6; }
        QPushButton#smallButton, QPushButton#secondaryButton { background:#132A39; color:#FFFFFF; border:1px solid #27526A; }
        QScrollArea { background:transparent; border:none; }
        QScrollArea#settingsPanelScroll > QWidget > QWidget { background:#07131D; }
        QScrollBar:vertical {
            background:#081923;
            width:12px;
            margin:0;
            border:none;
            border-radius:6px;
        }
        QScrollBar::handle:vertical {
            background:#315F78;
            min-height:34px;
            border-radius:6px;
        }
        QScrollBar::handle:vertical:hover { background:#3D7592; }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height:0;
            background:none;
            border:none;
        }
        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical { background:transparent; }
        QSplitter::handle { background:#17374A; width:2px; }
        ''')

    def install_shortcuts(self):
        a = QAction(self)
        a.setShortcut(QKeySequence(Qt.Key_Space))
        a.triggered.connect(self.toggle_pause_shortcut)
        self.addAction(a)

    def current_fft_size(self): return int(self.fft_combo.currentData() or DEFAULT_FFT_SIZE)
    def current_update_hz(self): return int(self.update_combo.currentData() or DEFAULT_UPDATE_HZ)
    def current_history_s(self): return int(self.history_combo.currentData() or DEFAULT_HISTORY_S)
    def current_buffer_ms(self): return int(self.buffer_combo.currentData() or 0)

    def worker_settings(self):
        return STFTSettings(
            fft_size=self.current_fft_size(), window=self.window_combo.currentText(),
            update_hz=self.current_update_hz(), remove_dc=self.remove_dc.isChecked(),
            backend=self.backend_combo.currentText(), cuda_device=int(self.cuda_combo.currentData() or 0),
        )

    def push_worker_settings(self):
        if hasattr(self, "worker"):
            self.worker.set_settings(self.worker_settings())

    def on_analysis_changed(self, *_):
        self.clear_history()
        self.update_fft_labels()
        self.push_worker_settings()

    def on_history_changed(self, *_):
        self.trim_history()
        h = self.current_history_s()
        for p in self.plots:
            p.setXRange(-h, 0.0, padding=0.0)
        if self.history:
            self.update_images()

    def set_render_fps(self, fps):
        # During UI construction the combo box can emit before render_timer is
        # created. The requested value is applied again immediately after the
        # timer is initialized in __init__.
        if not hasattr(self, "render_timer"):
            return
        self.render_timer.start(max(1, round(1000.0 / max(1, int(fps)))))

    def reset_presentation_clock(self, *_):
        self._presentation_started = False
        self._column_accum = 0.0
        self._last_release = time.perf_counter()

    def max_history_columns(self):
        return max(
            2,
            int(
                math.ceil(
                    self.current_history_s()
                    * self.current_nominal_column_rate_hz()
                )
            ),
        )

    def trim_history(self):
        m = self.max_history_columns()
        while len(self.history) > m:
            self.history.popleft()

    def clear_history(self):
        self.pending.clear()
        self.history.clear()
        self.frequency_hz = None
        self._presentation_started = False
        self._column_accum = 0.0
        self.underruns = 0
        self._in_underrun = False
        for img in self.images:
            img.clear()

    def apply_colormap(self, name):
        try:
            cmap = pg.colormap.get(str(name))
        except Exception:
            try:
                cmap = pg.colormap.get(DEFAULT_COLORMAP)
            except Exception:
                return
        for img in self.images:
            try:
                img.setColorMap(cmap)
            except Exception:
                pass

    def on_batch(self, batch: STFTBatch):
        self.apply_stream_info(
            raw_sample_rate_hz=(
                batch.raw_sample_rate_hz
            ),
            effective_sample_rate_hz=(
                batch.effective_sample_rate_hz
            ),
            decimation_samples=(
                batch.decimation_samples
            ),
            decimation_mode=(
                batch.decimation_mode
            ),
            adc_session_id=(
                batch.adc_session_id
            ),
        )

        self.latest_batch = batch
        self.nominal_column_rate_hz = float(
            batch.column_rate_hz
        )
        self.frequency_hz = batch.frequency_hz
        self.producer_rate_hz = float(batch.producer_rate_hz)
        if batch.publish_gap_ms >= 0:
            self.publish_gap_ms = float(batch.publish_gap_ms) if self.publish_gap_ms <= 0 else 0.85*self.publish_gap_ms + 0.15*float(batch.publish_gap_ms)
        for i in range(batch.columns_db.shape[0]):
            self.pending.append(batch.columns_db[i].copy())

        # Bound visual backlog to about 10 seconds.
        max_pending = max(
            64,
            int(
                math.ceil(
                    self.current_nominal_column_rate_hz()
                    * 10.0
                )
            ),
        )
        while len(self.pending) > max_pending:
            self.pending.popleft()

    def effective_display_rate(self):
        nominal_column_rate = max(
            0.001,
            self.current_nominal_column_rate_hz()
        )

        source_ratio = max(
            0.10,
            min(
                2.0,
                self.producer_rate_hz
                / self.current_sample_rate_hz(),
            ),
        )

        base = (
            nominal_column_rate
            * source_ratio
        )
        target = self.current_buffer_ms() / 1000.0 * max(1.0, base)
        if target <= 0:
            return max(0.5, base)
        error = (len(self.pending) - target) / max(1.0, target)
        correction = max(-0.20, min(0.15, error * 0.25))
        return max(0.5, base * (1.0 + correction))

    def release_columns(self):
        now = time.perf_counter()
        elapsed = max(0.0, now - self._last_release)
        self._last_release = now
        rate = self.effective_display_rate()
        self.display_rate_hz = rate
        buffer_cols = int(math.ceil(self.current_buffer_ms()/1000.0 * max(1.0, rate)))

        if not self._presentation_started:
            if self.current_buffer_ms() <= 0 or len(self.pending) >= max(1, buffer_cols):
                self._presentation_started = True
            else:
                return False

        self._column_accum += elapsed * rate
        n_release = int(self._column_accum)
        if n_release <= 0:
            return False
        self._column_accum -= n_release

        changed = False
        for _ in range(n_release):
            if not self.pending:
                if not self._in_underrun:
                    self.underruns += 1
                    self._in_underrun = True
                self._column_accum = min(self._column_accum, 1.0)
                break
            self.history.append(self.pending.popleft())
            changed = True
            if self._in_underrun and len(self.pending) >= max(2, buffer_cols // 2):
                self._in_underrun = False
        self.trim_history()
        return changed

    def channel_image(self, channel):
        if not self.history:
            return None
        # history entries [3, freq], output row-major [freq, time]
        return np.stack([col[channel] for col in self.history], axis=0).T.astype(np.float32, copy=False)

    def update_images(self):
        if self.frequency_hz is None or not self.history:
            return
        column_rate_hz = max(
            0.001,
            self.current_nominal_column_rate_hz()
        )

        duration = min(
            float(
                self.current_history_s()
            ),
            len(
                self.history
            )
            / column_rate_hz,
        )
        nyquist = float(self.frequency_hz[-1])

        for ctrl in self.controls:
            data = self.channel_image(ctrl.index)
            if data is None:
                continue
            if ctrl.auto_color.isChecked():
                finite = data[np.isfinite(data)]
                if len(finite):
                    lo = float(np.percentile(finite, 5.0)); hi = float(np.percentile(finite, 99.5))
                    if hi <= lo: hi = lo + 1.0
                    levels = (lo, hi)
                else:
                    levels = (DEFAULT_COLOR_MIN, DEFAULT_COLOR_MAX)
            else:
                levels = (float(ctrl.color_min.value()), float(ctrl.color_max.value()))
            img = self.images[ctrl.index]
            img.setImage(data, autoLevels=False, levels=levels)
            img.setRect(QRectF(-duration, 0.0, duration, nyquist))

    def render_frame(self):
        if self.paused:
            return
        if self.release_columns():
            self.update_images()
        self.update_render_metrics()

    def update_render_metrics(self):
        now_ns = time.perf_counter_ns()
        if self._last_render_ns is not None:
            dt_ms = (now_ns - self._last_render_ns)/1e6
            target_ms = 1000.0 / max(1, int(self.render_combo.currentData() or DEFAULT_RENDER_FPS))
            jitter = abs(dt_ms - target_ms)
            self.render_jitter_ms = 0.9*self.render_jitter_ms + 0.1*jitter
        self._last_render_ns = now_ns
        self._fps_count += 1
        now = time.perf_counter()
        elapsed = now - self._fps_start
        if elapsed >= 0.75:
            self.render_fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_start = now

    def apply_channel_view(self, ctrl):
        fmin, fmax = float(ctrl.freq_min.value()), float(ctrl.freq_max.value())
        cmin, cmax = float(ctrl.color_min.value()), float(ctrl.color_max.value())
        if fmin >= fmax:
            QMessageBox.warning(self, APP_TITLE, "Freq Min must be lower than Freq Max.")
            return
        if not ctrl.auto_color.isChecked() and cmin >= cmax:
            QMessageBox.warning(self, APP_TITLE, "Color Min must be lower than Color Max.")
            return
        self.plots[ctrl.index].setXRange(-self.current_history_s(), 0.0, padding=0.0)
        self.plots[ctrl.index].setYRange(fmin, fmax, padding=0.0)
        if self.history:
            self.update_images()

    def reset_channel_view(self, ctrl):
        ctrl.freq_min.setValue(DEFAULT_FREQ_MIN); ctrl.freq_max.setValue(self.default_frequency_max_hz())
        ctrl.color_min.setValue(DEFAULT_COLOR_MIN); ctrl.color_max.setValue(DEFAULT_COLOR_MAX)
        ctrl.auto_color.setChecked(False)
        self.apply_channel_view(ctrl)

    def reset_all_views(self):
        for c in self.controls:
            self.reset_channel_view(c)

    def toggle_pause(self, checked):
        self.paused = bool(checked)
        if self.paused:
            self.pause_button.setText("Continue"); self.mode_label.setText("PAUSED"); self.mode_label.setObjectName("modePaused")
        else:
            self.pause_button.setText("Pause"); self.mode_label.setText("LIVE"); self.mode_label.setObjectName("modeLive")
            self._last_release = time.perf_counter()
        self.mode_label.style().unpolish(self.mode_label); self.mode_label.style().polish(self.mode_label)

    def toggle_pause_shortcut(self):
        checked = not self.pause_button.isChecked()
        self.pause_button.setChecked(checked)
        self.toggle_pause(checked)

    def refresh_status(self):
        try:
            tel = self.shared.read_telemetry()
            bulk = self.shared.read_bulk_status()
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

            self.connection_label.setText("Shared RAM: DATA CONNECTED" if tel.data_connected else "Shared RAM: DATA NOT CONNECTED")
            b = self.latest_batch
            if b is None:
                self.compute_label.setText(f"STFT: waiting | ADC {self.shared.adc_total_samples():,}")
            else:
                gpu = f" | {b.gpu_name}" if b.gpu_name and not b.gpu_name.startswith("CUDA error:") else ""
                self.compute_label.setText(
                    (
                        f"{b.backend_name} | "
                        f"Fs {b.effective_sample_rate_hz:.3f} Hz | "
                        f"NFFT {self.current_fft_size():,} | "
                        f"hop {b.hop_samples} | "
                        f"col {b.column_rate_hz:.3f} Hz | "
                        f"STFT {b.compute_ms:.2f} ms | "
                        f"copy {b.copy_ms:.2f} ms"
                        f"{gpu}"
                    )
                )
            renderer = "OpenGL single-view" if self.opengl_active else "CPU/Raster"
            self.render_label.setText(
                f"Render {self.render_fps:4.1f} FPS | jitter {self.render_jitter_ms:3.1f} ms | {renderer} | "
                f"Fs {self.effective_sample_rate_hz:6.1f} Hz "
                f"(raw {self.raw_sample_rate_hz:6.1f}/N{self.decimation_samples}) | "
                f"Nyq {self.current_nyquist_hz():5.1f} Hz | "
                f"producer {self.producer_rate_hz:6.1f} Hz | queue {len(self.pending)} | "
                f"display {self.display_rate_hz:4.1f} col/s | underrun {self.underruns} | drop {bulk.dropped_frames}"
            )
            tip = (
                f"Executable: {sys.executable}\n"
                "STFT CUDA backend order: PyTorch CUDA/torch.fft -> "
                "CuPy/cuFFT -> NumPy CPU fallback.\n"
                f"CUDA detection: {self.cuda_description}"
            )
            if self.opengl_error:
                tip = self.opengl_error + "\n\n" + tip
            self.render_label.setToolTip(tip)
        except Exception as exc:
            self.connection_label.setText(f"Shared RAM status error: {exc}")

    def closeEvent(self, event: QCloseEvent):
        try:
            self.render_timer.stop(); self.status_timer.stop()
        except Exception:
            pass
        try:
            self.worker.stop(); self.worker.wait(2500)
        except Exception:
            pass
        try:
            self.shared.close()
        except Exception:
            pass
        release_windows_runtime()
        event.accept()


def main():
    if QOpenGLWidget is not None:
        try:
            fmt = QSurfaceFormat()
            fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
            fmt.setSamples(0); fmt.setSwapInterval(0)
            QSurfaceFormat.setDefaultFormat(fmt)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(f"{APP_TITLE} - {SYSTEM_TITLE}")
    icon = application_icon()
    if not icon.isNull(): app.setWindowIcon(icon)
    font = QFont("Segoe UI"); font.setPointSize(9); app.setFont(font)

    if np is None or pg is None:
        missing = []
        if np is None: missing.append("numpy")
        if pg is None: missing.append("pyqtgraph")
        QMessageBox.critical(None, APP_TITLE, "Missing packages: " + ", ".join(missing) + "\n\nInstall: pip install numpy pyqtgraph")
        return 1

    try:
        window = GeophoneSpectrogramWindow()
    except Exception as exc:
        QMessageBox.critical(None, APP_TITLE, f"Cannot start Geophone Spectrogram:\n\n{exc}")
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
