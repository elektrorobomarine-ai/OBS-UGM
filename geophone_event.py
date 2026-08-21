"""
geophone_event.py
=================

GRC-UGM-PERTAMINA OBS
Real-Time Geophone Event Monitor

Version: 3
Shared data: shared_data_v5.py

Purpose
-------
Detect transient seismic/acoustic events from synchronized:
    CH0 = Geophone X
    CH1 = Geophone Y
    CH2 = Geophone Z

Detection method
----------------
The detector uses a three-component vector-energy STA/LTA trigger:

    1. A slow per-axis baseline is removed from X/Y/Z.
    2. Vector energy is calculated:
           E = X^2 + Y^2 + Z^2
    3. Short-Term Average (STA) and Long-Term Average (LTA) are calculated.
    4. Trigger ratio:
           R = STA / LTA

Event start:
    R >= Trigger On
    and optional minimum vector RMS is satisfied.

Event end:
    R <= Trigger Off continuously for the configured release interval.

The detector processes new ADC samples incrementally. It does not repeatedly
re-detect the same shared-memory window.

Display
-------
- Combined real-time X / Y / Z waveform
- Real-time STA/LTA ratio and trigger thresholds
- Current detector state / current event
- Recent event table
- Event peak statistics
- OBS bulk-stream status

Performance
-----------
- Shared RAM reading and event detection run in a dedicated QThread.
- GUI renders from cached NumPy snapshots.
- A sample-index jitter buffer is used for smooth waveform presentation.
- GUI defaults to 60 FPS.
- Event detection runs sample-by-sample in the worker and is independent of
  the GUI frame rate.
- No synthetic/interpolated ADC samples are created.
- v3 reads the authoritative effective ADC sample rate from shared_data_v5.
  STA, LTA, release time, minimum event duration, dead time, baseline time
  constant, waveform window and gap detection all use that effective rate.
- ADC session or decimation changes reset the rolling detector state so one
  STA/LTA window never mixes samples from different stream configurations.
- Real timestamp gaps also break the detector windows; an active event is
  closed at the last valid sample with quality note DATA GAP.
- Measured producer throughput is used only for smooth display pacing; it does
  not redefine the physical sample rate.

Example:
    raw ADC = 1000 Hz
    Average N = 5
    effective shared rate = 200 Hz

Then:
    STA 0.25 s -> 50 samples
    LTA 5.0 s  -> 1000 samples

Important
---------
The event detector is an operator aid, not a substitute for calibrated seismic
processing. Trigger parameters should be tuned using real deployment data.

Dependencies
------------
    pip install PySide6 numpy pyqtgraph
"""

from __future__ import annotations

import csv
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

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.EVENT"
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
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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

APP_TITLE = "Geophone Event Monitor"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

DEFAULT_RENDER_FPS = 60
FPS_CHOICES = (
    30,
    45,
    60,
    75,
    90,
)

DEFAULT_BUFFER_MS = 1536
BUFFER_CHOICES_MS = (
    512,
    768,
    1024,
    1536,
    2048,
    3072,
)

DEFAULT_WAVEFORM_SPAN_S = 10.0
WAVEFORM_SPAN_CHOICES_S = (
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
)

DEFAULT_RATIO_HISTORY_S = 60
RATIO_HISTORY_CHOICES_S = (
    30,
    60,
    120,
    300,
)

DEFAULT_STA_S = 0.25
DEFAULT_LTA_S = 5.0
DEFAULT_TRIGGER_ON = 3.5
DEFAULT_TRIGGER_OFF = 1.5
DEFAULT_RELEASE_S = 0.20
DEFAULT_MIN_EVENT_S = 0.15
DEFAULT_DEADTIME_S = 1.0
DEFAULT_BASELINE_TAU_S = 2.0
DEFAULT_MIN_VECTOR_RMS = 0.0

DEFAULT_MAX_EVENTS = 200
MAX_EVENT_CHOICES = (
    50,
    100,
    200,
    500,
)

MAX_WAVE_RENDER_POINTS = 8000
MAX_RATIO_POINTS = 3000

ADC_READER_POLL_MS = 5
STATUS_INTERVAL_MS = 500

PRODUCER_RATE_WINDOW_S = 5.0

# Measured producer throughput is only a presentation/jitter diagnostic.
# Physical detector timing comes from shared_data_v5 effective Fs.
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0

COLOR_X = "#FF5E5E"
COLOR_Y = "#5FE07B"
COLOR_Z = "#5C96FF"
COLOR_RATIO = "#FFD166"
COLOR_TRIGGER_ON = "#FF6B6B"
COLOR_TRIGGER_OFF = "#66D9A0"


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


def format_timestamp_ns(timestamp_ns: int) -> str:
    if timestamp_ns <= 0:
        return "--"

    seconds = timestamp_ns / 1_000_000_000.0
    local = time.localtime(seconds)

    milliseconds = int(
        (timestamp_ns // 1_000_000)
        % 1000
    )

    return (
        time.strftime(
            "%H:%M:%S",
            local,
        )
        + f".{milliseconds:03d}"
    )


@dataclass(frozen=True)
class DetectorSettings:
    sta_s: float
    lta_s: float
    trigger_on: float
    trigger_off: float
    release_s: float
    min_event_s: float
    deadtime_s: float
    baseline_tau_s: float
    min_vector_rms: float
    enabled: bool


@dataclass(frozen=True)
class DetectedEvent:
    event_id: int

    start_timestamp_ns: int
    end_timestamp_ns: int

    start_absolute_sample: int
    end_absolute_sample: int

    duration_s: float

    peak_vector: float
    peak_x: float
    peak_y: float
    peak_z: float

    max_ratio: float

    quality_note: str


@dataclass(frozen=True)
class DetectorState:
    timestamp_monotonic: float

    detector_state: str
    enabled: bool

    ratio: float
    sta_energy: float
    lta_energy: float

    vector_rms: float

    current_event_duration_s: float
    current_peak_vector: float
    current_max_ratio: float

    event_count: int

    producer_rate_hz: float
    publish_gap_ms: float

    frames_received: int
    dropped_frames: int
    sequence_resets: int
    malformed_frames: int
    channel_id_mismatches: int

    error_flag_words: int
    filter_not_settled_words: int
    repeated_words: int
    saturated_words: int


# =============================================================================
# Event detector worker
# =============================================================================


class EventWorkerThread(QThread):
    # ADC snapshot, measured producer rate, publish gap ms, ADCStreamInfoSnapshot
    display_ready = Signal(
        object,
        float,
        float,
        object,
    )

    state_ready = Signal(
        object
    )

    event_detected = Signal(
        object
    )

    worker_error = Signal(
        str
    )

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

        self._display_lock = (
            threading.Lock()
        )

        self._display_count = int(
            (
                DEFAULT_WAVEFORM_SPAN_S
                + DEFAULT_BUFFER_MS
                / 1000.0
                + 2.0
            )
            * RAW_ADC_SAMPLE_RATE_HZ
        )

        self._settings = DetectorSettings(
            sta_s=DEFAULT_STA_S,
            lta_s=DEFAULT_LTA_S,
            trigger_on=DEFAULT_TRIGGER_ON,
            trigger_off=DEFAULT_TRIGGER_OFF,
            release_s=DEFAULT_RELEASE_S,
            min_event_s=DEFAULT_MIN_EVENT_S,
            deadtime_s=DEFAULT_DEADTIME_S,
            baseline_tau_s=DEFAULT_BASELINE_TAU_S,
            min_vector_rms=DEFAULT_MIN_VECTOR_RMS,
            enabled=True,
        )

        self._last_total = -1
        self._rate_history = deque()
        self._producer_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._effective_sample_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._raw_sample_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._decimation_samples = 1
        self._adc_session_id = -1
        self._last_publish_time = None

        self._event_id = 0

        self._reset_detector_runtime()

    # ------------------------------------------------------------------ settings

    def stop(self) -> None:
        self._stop_event.set()

    def set_display_count(
        self,
        count: int,
    ) -> None:
        with self._display_lock:
            self._display_count = max(
                128,
                int(count),
            )

    def _get_display_count(
        self,
    ) -> int:
        with self._display_lock:
            return int(
                self._display_count
            )

    def set_detector_settings(
        self,
        settings: DetectorSettings,
    ) -> None:
        with self._settings_lock:
            self._settings = settings

        # Window-length changes are applied cleanly on next processing pass.
        self._reset_detector_windows_only()

    def _get_settings(self) -> DetectorSettings:
        with self._settings_lock:
            return self._settings

    # ------------------------------------------------------------------ runtime reset

    def _reset_detector_windows_only(self):
        self._energy_sta = deque()
        self._energy_lta = deque()
        self._sta_sum = 0.0
        self._lta_sum = 0.0

    def _reset_detector_runtime(self):
        self._reset_detector_windows_only()

        self._baseline_initialized = False
        self._baseline_x = 0.0
        self._baseline_y = 0.0
        self._baseline_z = 0.0

        self._triggered = False
        self._release_count = 0

        self._current_start_timestamp_ns = 0
        self._current_start_absolute_sample = 0
        self._current_peak_vector = 0.0
        self._current_peak_x = 0.0
        self._current_peak_y = 0.0
        self._current_peak_z = 0.0
        self._current_max_ratio = 0.0

        self._dead_until_absolute_sample = -1

        self._last_ratio = 0.0
        self._last_sta = 0.0
        self._last_lta = 0.0
        self._last_vector_rms = 0.0

        self._last_processed_absolute_sample = -1
        self._last_processed_timestamp_ns = None

    # ------------------------------------------------------------------ processing helpers

    def _current_sample_rate_hz(self) -> float:
        return max(
            0.001,
            float(
                self._effective_sample_rate_hz
            ),
        )

    def _update_producer_rate(
        self,
        now: float,
        total: int,
    ) -> None:
        self._rate_history.append(
            (
                now,
                int(total),
            )
        )

        cutoff = (
            now
            - PRODUCER_RATE_WINDOW_S
        )

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
                    self._current_sample_rate_hz()
                    * PRODUCER_RATE_MIN_RATIO,
                )
                max_rate = max(
                    min_rate * 2.0,
                    self._current_sample_rate_hz()
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

    def _push_energy(
        self,
        energy: float,
        sta_count: int,
        lta_count: int,
    ):
        self._energy_sta.append(
            energy
        )
        self._sta_sum += energy

        if len(
            self._energy_sta
        ) > sta_count:
            self._sta_sum -= (
                self._energy_sta.popleft()
            )

        self._energy_lta.append(
            energy
        )
        self._lta_sum += energy

        if len(
            self._energy_lta
        ) > lta_count:
            self._lta_sum -= (
                self._energy_lta.popleft()
            )

        sta = (
            self._sta_sum
            / max(
                1,
                len(
                    self._energy_sta
                ),
            )
        )

        lta = (
            self._lta_sum
            / max(
                1,
                len(
                    self._energy_lta
                ),
            )
        )

        if (
            len(
                self._energy_lta
            )
            < lta_count
            or lta <= 1.0e-18
        ):
            ratio = 0.0
        else:
            ratio = (
                sta / lta
            )

        return (
            sta,
            lta,
            ratio,
        )

    @staticmethod
    def _status_quality_note(
        status_x: int,
        status_y: int,
        status_z: int,
    ) -> str:
        combined = (
            int(status_x)
            | int(status_y)
            | int(status_z)
        )

        notes = []

        if combined & 0x80:
            notes.append(
                "ADC ERROR"
            )
        if combined & 0x40:
            notes.append(
                "FILTER UNSETTLED"
            )
        if combined & 0x20:
            notes.append(
                "REPEATED"
            )
        if combined & 0x08:
            notes.append(
                "SATURATED"
            )

        if not notes:
            return "OK"

        return ", ".join(
            notes
        )

    def _finish_event(
        self,
        *,
        end_timestamp_ns: int,
        end_absolute_sample: int,
        settings: DetectorSettings,
        quality_note: str,
    ) -> None:
        duration_samples = max(
            0,
            int(
                end_absolute_sample
                - self._current_start_absolute_sample
                + 1
            ),
        )

        duration_s = (
            duration_samples
            / self._current_sample_rate_hz()
        )

        if (
            duration_s
            >= settings.min_event_s
        ):
            self._event_id += 1

            event = DetectedEvent(
                event_id=self._event_id,
                start_timestamp_ns=int(
                    self._current_start_timestamp_ns
                ),
                end_timestamp_ns=int(
                    end_timestamp_ns
                ),
                start_absolute_sample=int(
                    self._current_start_absolute_sample
                ),
                end_absolute_sample=int(
                    end_absolute_sample
                ),
                duration_s=float(
                    duration_s
                ),
                peak_vector=float(
                    self._current_peak_vector
                ),
                peak_x=float(
                    self._current_peak_x
                ),
                peak_y=float(
                    self._current_peak_y
                ),
                peak_z=float(
                    self._current_peak_z
                ),
                max_ratio=float(
                    self._current_max_ratio
                ),
                quality_note=str(
                    quality_note
                ),
            )

            self.event_detected.emit(
                event
            )

        dead_samples = int(
            round(
                settings.deadtime_s
                * self._current_sample_rate_hz()
            )
        )

        self._dead_until_absolute_sample = (
            int(
                end_absolute_sample
            )
            + max(
                0,
                dead_samples,
            )
        )

        self._triggered = False
        self._release_count = 0

    def _process_new_samples(
        self,
        adc,
        settings: DetectorSettings,
    ) -> None:
        count = len(
            adc.ch0
        )

        if count <= 0:
            return

        total = int(
            adc.total_samples
        )

        cache_start_absolute = (
            total
            - count
        )

        if (
            self._last_processed_absolute_sample
            < cache_start_absolute - 1
        ):
            # Reader fell behind the retained snapshot. Reset rolling detector
            # windows rather than pretending the missing interval was present.
            self._reset_detector_windows_only()
            self._baseline_initialized = False
            self._triggered = False
            self._release_count = 0
            self._last_processed_absolute_sample = (
                cache_start_absolute - 1
            )
            self._last_processed_timestamp_ns = None

        local_start = max(
            0,
            self._last_processed_absolute_sample
            - cache_start_absolute
            + 1,
        )

        if local_start >= count:
            return

        sample_rate_hz = max(
            0.001,
            float(
                adc.sample_rate_hz
            ),
        )

        # Snapshot Fs is authoritative for the samples being processed.
        self._effective_sample_rate_hz = sample_rate_hz

        sta_count = max(
            1,
            int(
                round(
                    settings.sta_s
                    * sample_rate_hz
                )
            ),
        )

        lta_count = max(
            sta_count + 1,
            int(
                round(
                    settings.lta_s
                    * sample_rate_hz
                )
            ),
        )

        release_samples = max(
            1,
            int(
                round(
                    settings.release_s
                    * sample_rate_hz
                )
            ),
        )

        baseline_tau = max(
            0.02,
            float(
                settings.baseline_tau_s
            ),
        )

        baseline_alpha = math.exp(
            -1.0
            / (
                sample_rate_hz
                * baseline_tau
            )
        )

        expected_interval_ns = int(
            round(
                1_000_000_000.0
                / sample_rate_hz
            )
        )

        for local_index in range(
            local_start,
            count,
        ):
            absolute_sample = (
                cache_start_absolute
                + local_index
            )

            timestamp_ns = int(
                adc.timestamp_ns[
                    local_index
                ]
            )

            # A real timestamp discontinuity means samples are missing. Never
            # allow an STA/LTA or baseline window to bridge that gap.
            if self._last_processed_timestamp_ns is not None:
                timestamp_delta_ns = (
                    timestamp_ns
                    - int(
                        self._last_processed_timestamp_ns
                    )
                )

                if (
                    timestamp_delta_ns <= 0
                    or timestamp_delta_ns
                    > int(
                        1.75
                        * expected_interval_ns
                    )
                ):
                    if (
                        self._triggered
                        and absolute_sample > 0
                    ):
                        self._finish_event(
                            end_timestamp_ns=int(
                                self._last_processed_timestamp_ns
                            ),
                            end_absolute_sample=(
                                absolute_sample - 1
                            ),
                            settings=settings,
                            quality_note="DATA GAP",
                        )

                    self._reset_detector_windows_only()
                    self._baseline_initialized = False
                    self._triggered = False
                    self._release_count = 0
                    self._dead_until_absolute_sample = -1
                    self._last_ratio = 0.0
                    self._last_sta = 0.0
                    self._last_lta = 0.0
                    self._last_vector_rms = 0.0

            self._last_processed_timestamp_ns = (
                timestamp_ns
            )

            raw_x = float(
                adc.ch0[
                    local_index
                ]
            )
            raw_y = float(
                adc.ch1[
                    local_index
                ]
            )
            raw_z = float(
                adc.ch2[
                    local_index
                ]
            )

            if not self._baseline_initialized:
                self._baseline_x = raw_x
                self._baseline_y = raw_y
                self._baseline_z = raw_z
                self._baseline_initialized = True

            self._baseline_x = (
                baseline_alpha
                * self._baseline_x
                + (
                    1.0
                    - baseline_alpha
                )
                * raw_x
            )
            self._baseline_y = (
                baseline_alpha
                * self._baseline_y
                + (
                    1.0
                    - baseline_alpha
                )
                * raw_y
            )
            self._baseline_z = (
                baseline_alpha
                * self._baseline_z
                + (
                    1.0
                    - baseline_alpha
                )
                * raw_z
            )

            x = (
                raw_x
                - self._baseline_x
            )
            y = (
                raw_y
                - self._baseline_y
            )
            z = (
                raw_z
                - self._baseline_z
            )

            energy = (
                x * x
                + y * y
                + z * z
            )

            (
                sta,
                lta,
                ratio,
            ) = self._push_energy(
                energy,
                sta_count,
                lta_count,
            )

            vector = math.sqrt(
                max(
                    0.0,
                    energy,
                )
            )

            vector_rms = math.sqrt(
                max(
                    0.0,
                    sta,
                )
            )

            self._last_ratio = (
                ratio
            )
            self._last_sta = sta
            self._last_lta = lta
            self._last_vector_rms = (
                vector_rms
            )

            quality_note = (
                self._status_quality_note(
                    int(
                        adc.status0[
                            local_index
                        ]
                    ),
                    int(
                        adc.status1[
                            local_index
                        ]
                    ),
                    int(
                        adc.status2[
                            local_index
                        ]
                    ),
                )
            )

            if not settings.enabled:
                if self._triggered:
                    self._finish_event(
                        end_timestamp_ns=(
                            timestamp_ns
                        ),
                        end_absolute_sample=(
                            absolute_sample
                        ),
                        settings=settings,
                        quality_note=(
                            "Detection disabled"
                        ),
                    )

                self._last_processed_absolute_sample = (
                    absolute_sample
                )
                continue

            lta_ready = (
                len(
                    self._energy_lta
                )
                >= lta_count
            )

            min_rms_ok = (
                settings.min_vector_rms
                <= 0.0
                or vector_rms
                >= settings.min_vector_rms
            )

            if not self._triggered:
                if (
                    absolute_sample
                    > self._dead_until_absolute_sample
                    and lta_ready
                    and ratio
                    >= settings.trigger_on
                    and min_rms_ok
                ):
                    self._triggered = True
                    self._release_count = 0

                    self._current_start_timestamp_ns = (
                        timestamp_ns
                    )
                    self._current_start_absolute_sample = (
                        absolute_sample
                    )

                    self._current_peak_vector = (
                        vector
                    )
                    self._current_peak_x = (
                        abs(x)
                    )
                    self._current_peak_y = (
                        abs(y)
                    )
                    self._current_peak_z = (
                        abs(z)
                    )
                    self._current_max_ratio = (
                        ratio
                    )

            else:
                self._current_peak_vector = max(
                    self._current_peak_vector,
                    vector,
                )
                self._current_peak_x = max(
                    self._current_peak_x,
                    abs(x),
                )
                self._current_peak_y = max(
                    self._current_peak_y,
                    abs(y),
                )
                self._current_peak_z = max(
                    self._current_peak_z,
                    abs(z),
                )
                self._current_max_ratio = max(
                    self._current_max_ratio,
                    ratio,
                )

                if ratio <= settings.trigger_off:
                    self._release_count += 1
                else:
                    self._release_count = 0

                if (
                    self._release_count
                    >= release_samples
                ):
                    self._finish_event(
                        end_timestamp_ns=(
                            timestamp_ns
                        ),
                        end_absolute_sample=(
                            absolute_sample
                        ),
                        settings=settings,
                        quality_note=(
                            quality_note
                        ),
                    )

            self._last_processed_absolute_sample = (
                absolute_sample
            )

    # ------------------------------------------------------------------ worker state

    def _make_state(
        self,
        shared,
        settings: DetectorSettings,
    ) -> DetectorState:
        bulk = shared.read_bulk_status()

        current_duration = 0.0

        if (
            self._triggered
            and self._last_processed_absolute_sample
            >= self._current_start_absolute_sample
        ):
            current_duration = (
                (
                    self._last_processed_absolute_sample
                    - self._current_start_absolute_sample
                    + 1
                )
                / self._current_sample_rate_hz()
            )

        if not settings.enabled:
            state = "DISABLED"
        elif self._triggered:
            state = "TRIGGERED"
        elif (
            self._last_processed_absolute_sample
            <= self._dead_until_absolute_sample
        ):
            state = "DEADTIME"
        else:
            state = "ARMED"

        return DetectorState(
            timestamp_monotonic=(
                time.perf_counter()
            ),
            detector_state=state,
            enabled=bool(
                settings.enabled
            ),
            ratio=float(
                self._last_ratio
            ),
            sta_energy=float(
                self._last_sta
            ),
            lta_energy=float(
                self._last_lta
            ),
            vector_rms=float(
                self._last_vector_rms
            ),
            current_event_duration_s=float(
                current_duration
            ),
            current_peak_vector=float(
                self._current_peak_vector
                if self._triggered
                else 0.0
            ),
            current_max_ratio=float(
                self._current_max_ratio
                if self._triggered
                else 0.0
            ),
            event_count=int(
                self._event_id
            ),
            producer_rate_hz=float(
                self._producer_rate_hz
            ),
            publish_gap_ms=float(
                self._last_publish_gap_ms
                if hasattr(
                    self,
                    "_last_publish_gap_ms",
                )
                else 0.0
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
        )

    # ------------------------------------------------------------------ main thread loop

    def run(self) -> None:
        shared = None
        last_state_emit = 0.0

        try:
            shared = OBSSharedData()

            stream_info = (
                shared.read_adc_stream_info()
            )
            self._raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self._effective_sample_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self._decimation_samples = max(
                1,
                int(
                    stream_info.decimation_samples
                ),
            )
            self._adc_session_id = int(
                stream_info.adc_session_id
            )
            self._producer_rate_hz = float(
                self._effective_sample_rate_hz
            )

            while not self._stop_event.is_set():
                stream_info = (
                    shared.read_adc_stream_info()
                )

                incoming_effective_rate_hz = max(
                    0.001,
                    float(
                        stream_info.effective_sample_rate_hz
                    ),
                )
                incoming_session_id = int(
                    stream_info.adc_session_id
                )

                stream_changed = (
                    incoming_session_id
                    != self._adc_session_id
                    or abs(
                        incoming_effective_rate_hz
                        - self._effective_sample_rate_hz
                    )
                    > max(
                        1.0e-9,
                        1.0e-6
                        * incoming_effective_rate_hz,
                    )
                )

                if stream_changed:
                    self._raw_sample_rate_hz = float(
                        stream_info.raw_sample_rate_hz
                    )
                    self._effective_sample_rate_hz = (
                        incoming_effective_rate_hz
                    )
                    self._decimation_samples = max(
                        1,
                        int(
                            stream_info.decimation_samples
                        ),
                    )
                    self._adc_session_id = (
                        incoming_session_id
                    )
                    self._producer_rate_hz = (
                        incoming_effective_rate_hz
                    )
                    self._rate_history.clear()
                    self._last_publish_time = None
                    self._last_total = -1
                    self._reset_detector_runtime()

                total = (
                    shared.adc_total_samples()
                )

                now = time.perf_counter()

                if total != self._last_total:
                    self._update_producer_rate(
                        now,
                        int(total),
                    )

                    if self._last_publish_time is None:
                        publish_gap_ms = 0.0
                    else:
                        publish_gap_ms = (
                            now
                            - self._last_publish_time
                        ) * 1000.0

                    self._last_publish_time = now
                    self._last_publish_gap_ms = (
                        publish_gap_ms
                    )

                    display_count = (
                        self._get_display_count()
                    )

                    adc = (
                        shared.read_adc_latest_numpy(
                            display_count
                        )
                    )

                    settings = (
                        self._get_settings()
                    )

                    self._process_new_samples(
                        adc,
                        settings,
                    )

                    self.display_ready.emit(
                        adc,
                        float(
                            self._producer_rate_hz
                        ),
                        float(
                            publish_gap_ms
                        ),
                        stream_info,
                    )

                    self._last_total = int(
                        adc.total_samples
                    )

                if (
                    now
                    - last_state_emit
                    >= 0.10
                ):
                    settings = (
                        self._get_settings()
                    )

                    self.state_ready.emit(
                        self._make_state(
                            shared,
                            settings,
                        )
                    )

                    last_state_emit = now

                self.msleep(
                    ADC_READER_POLL_MS
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


class GeophoneEventWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        if np is None or pg is None:
            raise RuntimeError(
                "Geophone Event Monitor requires NumPy and PyQtGraph."
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

        self.cached_adc = None
        self.cached_total = -1

        self.producer_rate_hz = float(
            self.effective_sample_rate_hz
        )
        self.publish_gap_ms = 0.0

        self.playhead_sample = None
        self.playhead_wall_ns = None
        self.reserve_ms = 0.0
        self.underruns = 0
        self._in_underrun = False

        self.latest_state: Optional[
            DetectorState
        ] = None

        self.events = deque()

        self.ratio_times = deque()
        self.ratio_values = deque()

        self.paused = False

        self.render_fps = 0.0
        self.render_jitter_ms = 0.0
        self._frame_count = 0
        self._fps_start = time.perf_counter()
        self._last_render_ns = None

        self.opengl_active = False
        self.opengl_error = ""

        self.setWindowTitle(
            f"{APP_TITLE} - {SYSTEM_TITLE}"
        )

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(
                icon
            )

        self.resize(
            1550,
            920,
        )
        self.setMinimumSize(
            1120,
            720,
        )

        self._configure_pyqtgraph()
        self._build_ui()
        self._apply_style()
        self._install_shortcuts()

        self.worker = EventWorkerThread(
            self
        )
        self.worker.display_ready.connect(
            self.on_display_snapshot
        )
        self.worker.state_ready.connect(
            self.on_detector_state
        )
        self.worker.event_detected.connect(
            self.on_event_detected
        )
        self.worker.worker_error.connect(
            self.on_worker_error
        )

        self._push_detector_settings()
        self._update_worker_display_count()

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

            viewport.setFormat(fmt)
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

        # Header.
        header = QHBoxLayout()

        title_box = QVBoxLayout()
        title_box.setSpacing(1)

        title = QLabel(
            "GEOPHONE EVENT MONITOR"
        )
        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            "3-Component STA/LTA Trigger  •  X/Y/Z Waveform  •  Event Log"
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

        self.detector_state_label = QLabel(
            "WAITING"
        )
        self.detector_state_label.setObjectName(
            "detectorWaiting"
        )
        self.detector_state_label.setAlignment(
            Qt.AlignCenter
        )
        self.detector_state_label.setMinimumWidth(
            140
        )

        self.pause_button = QPushButton(
            "Pause Display"
        )
        self.pause_button.setObjectName(
            "pauseButton"
        )
        self.pause_button.setCheckable(
            True
        )
        self.pause_button.clicked.connect(
            self.toggle_pause
        )

        header.addWidget(
            self.detector_state_label
        )
        header.addSpacing(8)
        header.addWidget(
            self.pause_button
        )

        root.addLayout(
            header
        )

        # Status strip.
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

        self.detector_info_label = QLabel(
            "Detector: --"
        )
        self.detector_info_label.setObjectName(
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
            self.detector_info_label
        )
        sl.addSpacing(14)
        sl.addWidget(
            self.render_label
        )

        root.addWidget(
            status
        )

        # Current-event metrics row.
        metrics = QHBoxLayout()
        metrics.setSpacing(8)

        self.ratio_card = self._metric_card(
            "STA / LTA",
            "--",
        )
        self.rms_card = self._metric_card(
            "Vector RMS",
            "--",
        )
        self.duration_card = self._metric_card(
            "Current Duration",
            "--",
        )
        self.peak_card = self._metric_card(
            "Current Peak",
            "--",
        )
        self.count_card = self._metric_card(
            "Detected Events",
            "0",
        )

        for card in (
            self.ratio_card,
            self.rms_card,
            self.duration_card,
            self.peak_card,
            self.count_card,
        ):
            metrics.addWidget(
                card["frame"],
                1,
            )

        root.addLayout(
            metrics
        )

        main_splitter = QSplitter(
            Qt.Horizontal
        )
        main_splitter.setChildrenCollapsible(
            False
        )

        main_splitter.addWidget(
            self._build_plot_panel()
        )
        main_splitter.addWidget(
            self._build_control_panel()
        )

        main_splitter.setStretchFactor(
            0,
            3,
        )
        main_splitter.setStretchFactor(
            1,
            1,
        )
        main_splitter.setSizes(
            [1120, 390]
        )

        root.addWidget(
            main_splitter,
            3,
        )

        root.addWidget(
            self._build_event_table(),
            2,
        )

    def _metric_card(
        self,
        title: str,
        value: str,
    ):
        frame = QFrame()
        frame.setObjectName(
            "metricCard"
        )

        layout = QVBoxLayout(
            frame
        )
        layout.setContentsMargins(
            10, 7, 10, 7
        )
        layout.setSpacing(2)

        title_label = QLabel(
            title
        )
        title_label.setObjectName(
            "metricCardTitle"
        )

        value_label = QLabel(
            value
        )
        value_label.setObjectName(
            "metricCardValue"
        )

        layout.addWidget(
            title_label
        )
        layout.addWidget(
            value_label
        )

        return {
            "frame": frame,
            "value": value_label,
        }

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

        # Combined waveform.
        self.wave_plot = (
            self.graphics.addPlot(
                row=0,
                col=0,
            )
        )

        self.wave_plot.setTitle(
            "Combined Real-Time Waveform — X / Y / Z",
            color="#FFFFFF",
            size="11pt",
        )

        self.wave_plot.setLabel(
            "left",
            "Amplitude",
            units="count",
        )
        self.wave_plot.setLabel(
            "bottom",
            "Time",
            units="s",
        )

        self.wave_plot.showGrid(
            x=True,
            y=True,
            alpha=0.18,
        )

        self.wave_plot.setClipToView(
            True
        )

        self.wave_plot.setXRange(
            -DEFAULT_WAVEFORM_SPAN_S,
            0.0,
            padding=0.0,
        )

        self.curve_x = (
            self.wave_plot.plot(
                [],
                [],
                pen=pg.mkPen(
                    COLOR_X,
                    width=1.0,
                ),
            )
        )
        self.curve_y = (
            self.wave_plot.plot(
                [],
                [],
                pen=pg.mkPen(
                    COLOR_Y,
                    width=1.0,
                ),
            )
        )
        self.curve_z = (
            self.wave_plot.plot(
                [],
                [],
                pen=pg.mkPen(
                    COLOR_Z,
                    width=1.0,
                ),
            )
        )

        # Trigger ratio plot.
        self.ratio_plot = (
            self.graphics.addPlot(
                row=1,
                col=0,
            )
        )

        self.ratio_plot.setTitle(
            "STA / LTA Trigger Ratio",
            color="#FFFFFF",
            size="11pt",
        )
        self.ratio_plot.setLabel(
            "left",
            "Ratio",
        )
        self.ratio_plot.setLabel(
            "bottom",
            "History",
            units="s",
        )
        self.ratio_plot.showGrid(
            x=True,
            y=True,
            alpha=0.18,
        )

        self.ratio_curve = (
            self.ratio_plot.plot(
                [],
                [],
                pen=pg.mkPen(
                    COLOR_RATIO,
                    width=1.4,
                ),
            )
        )

        self.trigger_on_line = (
            pg.InfiniteLine(
                angle=0,
                movable=False,
                pen=pg.mkPen(
                    COLOR_TRIGGER_ON,
                    width=1.2,
                    style=Qt.DashLine,
                ),
            )
        )

        self.trigger_off_line = (
            pg.InfiniteLine(
                angle=0,
                movable=False,
                pen=pg.mkPen(
                    COLOR_TRIGGER_OFF,
                    width=1.2,
                    style=Qt.DashLine,
                ),
            )
        )

        self.ratio_plot.addItem(
            self.trigger_on_line
        )
        self.ratio_plot.addItem(
            self.trigger_off_line
        )

        self._update_threshold_lines()

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
            "EVENT DETECTOR"
        )
        heading.setObjectName(
            "settingsTitle"
        )

        layout.addWidget(
            heading
        )

        detector = QGroupBox(
            "STA / LTA"
        )
        detector.setObjectName(
            "channelGroup"
        )

        grid = QGridLayout(
            detector
        )
        grid.setContentsMargins(
            10, 12, 10, 10
        )

        self.detection_enabled = (
            QCheckBox(
                "Enable Event Detection"
            )
        )
        self.detection_enabled.setChecked(
            True
        )

        self.sta_spin = QDoubleSpinBox()
        self.sta_spin.setRange(
            0.02,
            5.0,
        )
        self.sta_spin.setDecimals(2)
        self.sta_spin.setSingleStep(
            0.05
        )
        self.sta_spin.setValue(
            DEFAULT_STA_S
        )
        self.sta_spin.setSuffix(
            " s"
        )

        self.lta_spin = QDoubleSpinBox()
        self.lta_spin.setRange(
            0.20,
            60.0,
        )
        self.lta_spin.setDecimals(2)
        self.lta_spin.setSingleStep(
            0.5
        )
        self.lta_spin.setValue(
            DEFAULT_LTA_S
        )
        self.lta_spin.setSuffix(
            " s"
        )

        self.trigger_on_spin = (
            QDoubleSpinBox()
        )
        self.trigger_on_spin.setRange(
            1.01,
            100.0,
        )
        self.trigger_on_spin.setDecimals(
            2
        )
        self.trigger_on_spin.setSingleStep(
            0.1
        )
        self.trigger_on_spin.setValue(
            DEFAULT_TRIGGER_ON
        )

        self.trigger_off_spin = (
            QDoubleSpinBox()
        )
        self.trigger_off_spin.setRange(
            0.01,
            100.0,
        )
        self.trigger_off_spin.setDecimals(
            2
        )
        self.trigger_off_spin.setSingleStep(
            0.1
        )
        self.trigger_off_spin.setValue(
            DEFAULT_TRIGGER_OFF
        )

        self.release_spin = QDoubleSpinBox()
        self.release_spin.setRange(
            0.01,
            10.0,
        )
        self.release_spin.setDecimals(
            2
        )
        self.release_spin.setValue(
            DEFAULT_RELEASE_S
        )
        self.release_spin.setSuffix(
            " s"
        )

        self.min_event_spin = (
            QDoubleSpinBox()
        )
        self.min_event_spin.setRange(
            0.0,
            60.0,
        )
        self.min_event_spin.setDecimals(
            2
        )
        self.min_event_spin.setValue(
            DEFAULT_MIN_EVENT_S
        )
        self.min_event_spin.setSuffix(
            " s"
        )

        self.deadtime_spin = (
            QDoubleSpinBox()
        )
        self.deadtime_spin.setRange(
            0.0,
            60.0,
        )
        self.deadtime_spin.setDecimals(
            2
        )
        self.deadtime_spin.setValue(
            DEFAULT_DEADTIME_S
        )
        self.deadtime_spin.setSuffix(
            " s"
        )

        self.baseline_spin = (
            QDoubleSpinBox()
        )
        self.baseline_spin.setRange(
            0.05,
            60.0,
        )
        self.baseline_spin.setDecimals(
            2
        )
        self.baseline_spin.setValue(
            DEFAULT_BASELINE_TAU_S
        )
        self.baseline_spin.setSuffix(
            " s"
        )

        self.min_rms_spin = (
            QDoubleSpinBox()
        )
        self.min_rms_spin.setRange(
            0.0,
            100_000_000.0,
        )
        self.min_rms_spin.setDecimals(
            0
        )
        self.min_rms_spin.setValue(
            DEFAULT_MIN_VECTOR_RMS
        )
        self.min_rms_spin.setGroupSeparatorShown(
            True
        )

        row = 0

        grid.addWidget(
            self.detection_enabled,
            row,
            0,
            1,
            2,
        )
        row += 1

        controls = (
            ("STA Window", self.sta_spin),
            ("LTA Window", self.lta_spin),
            ("Trigger ON", self.trigger_on_spin),
            ("Trigger OFF", self.trigger_off_spin),
            ("Release Time", self.release_spin),
            ("Min Event", self.min_event_spin),
            ("Dead Time", self.deadtime_spin),
            ("Baseline τ", self.baseline_spin),
            ("Min Vector RMS", self.min_rms_spin),
        )

        for name, widget in controls:
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
            row += 1

        apply_button = QPushButton(
            "Apply Detector Settings"
        )
        apply_button.setObjectName(
            "smallPrimaryButton"
        )
        apply_button.clicked.connect(
            self.apply_detector_settings
        )

        grid.addWidget(
            apply_button,
            row,
            0,
            1,
            2,
        )

        layout.addWidget(
            detector
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
            self.on_display_setting_changed
        )

        self.wave_span_combo = QComboBox()
        for value in (
            WAVEFORM_SPAN_CHOICES_S
        ):
            self.wave_span_combo.addItem(
                f"{value:g} s",
                float(value),
            )
        self.wave_span_combo.setCurrentText(
            f"{DEFAULT_WAVEFORM_SPAN_S:g} s"
        )
        self.wave_span_combo.currentIndexChanged.connect(
            self.on_display_setting_changed
        )

        self.ratio_history_combo = QComboBox()
        for seconds in (
            RATIO_HISTORY_CHOICES_S
        ):
            self.ratio_history_combo.addItem(
                f"{seconds} s",
                seconds,
            )
        self.ratio_history_combo.setCurrentText(
            f"{DEFAULT_RATIO_HISTORY_S} s"
        )

        self.max_events_combo = QComboBox()
        for count in (
            MAX_EVENT_CHOICES
        ):
            self.max_events_combo.addItem(
                str(count),
                count,
            )
        self.max_events_combo.setCurrentText(
            str(
                DEFAULT_MAX_EVENTS
            )
        )

        display_controls = (
            ("Target FPS", self.fps_combo),
            ("Smooth Buffer", self.buffer_combo),
            ("Waveform Span", self.wave_span_combo),
            ("Ratio History", self.ratio_history_combo),
            ("Max Event Log", self.max_events_combo),
        )

        for row, (
            name,
            widget,
        ) in enumerate(
            display_controls
        ):
            dg.addWidget(
                QLabel(name),
                row,
                0,
            )
            dg.addWidget(
                widget,
                row,
                1,
            )

        layout.addWidget(
            display
        )

        bulk = QGroupBox(
            "OBS Bulk Health"
        )
        bulk.setObjectName(
            "channelGroup"
        )

        bg = QVBoxLayout(
            bulk
        )
        bg.setContentsMargins(
            10, 12, 10, 10
        )

        self.bulk_label = QLabel(
            "Waiting for status..."
        )
        self.bulk_label.setObjectName(
            "bulkValue"
        )
        self.bulk_label.setWordWrap(
            True
        )

        bg.addWidget(
            self.bulk_label
        )

        layout.addWidget(
            bulk
        )

        note = QLabel(
            "Recommended starting point: STA 0.25 s, LTA 5 s, ON 3.5, OFF 1.5. "
            "Tune the trigger using recorded field data."
        )
        note.setObjectName(
            "sampleInfo"
        )
        note.setWordWrap(
            True
        )

        layout.addWidget(
            note
        )
        layout.addStretch(
            1
        )

        return panel

    def _build_event_table(self):
        frame = QFrame()
        frame.setObjectName(
            "eventTableFrame"
        )

        layout = QVBoxLayout(
            frame
        )
        layout.setContentsMargins(
            8, 7, 8, 7
        )
        layout.setSpacing(5)

        header = QHBoxLayout()

        title = QLabel(
            "RECENT EVENTS"
        )
        title.setObjectName(
            "settingsTitle"
        )

        clear_button = QPushButton(
            "Clear Log"
        )
        clear_button.clicked.connect(
            self.clear_event_log
        )

        export_button = QPushButton(
            "Export CSV"
        )
        export_button.setObjectName(
            "smallPrimaryButton"
        )
        export_button.clicked.connect(
            self.export_events_csv
        )

        header.addWidget(
            title
        )
        header.addStretch(1)
        header.addWidget(
            clear_button
        )
        header.addWidget(
            export_button
        )

        layout.addLayout(
            header
        )

        self.event_table = QTableWidget(
            0,
            10,
        )

        self.event_table.setHorizontalHeaderLabels(
            [
                "ID",
                "Start",
                "Duration",
                "Peak Vector",
                "Peak X",
                "Peak Y",
                "Peak Z",
                "Max STA/LTA",
                "Quality",
                "Sample",
            ]
        )

        self.event_table.setEditTriggers(
            QTableWidget.NoEditTriggers
        )

        self.event_table.setSelectionBehavior(
            QTableWidget.SelectRows
        )

        self.event_table.verticalHeader().setVisible(
            False
        )

        header_view = (
            self.event_table.horizontalHeader()
        )

        header_view.setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        header_view.setStretchLastSection(
            True
        )

        layout.addWidget(
            self.event_table,
            1,
        )

        return frame

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
            QFrame#metricCard,
            QFrame#eventTableFrame {
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

            QLabel#metricCardTitle {
                color: #86A2B1;
                font-size: 9px;
            }

            QLabel#metricCardValue {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 16px;
                font-weight: 800;
            }

            QLabel#detectorArmed {
                background-color: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 7px;
                color: #A9F1D2;
                font-weight: 800;
                padding: 5px 12px;
            }

            QLabel#detectorTriggered {
                background-color: #541C24;
                border: 1px solid #C94D5E;
                border-radius: 7px;
                color: #FFC0C8;
                font-weight: 900;
                padding: 5px 12px;
            }

            QLabel#detectorDeadtime {
                background-color: #403510;
                border: 1px solid #A88821;
                border-radius: 7px;
                color: #FFE49A;
                font-weight: 800;
                padding: 5px 12px;
            }

            QLabel#detectorDisabled,
            QLabel#detectorWaiting {
                background-color: #172631;
                border: 1px solid #35546A;
                border-radius: 7px;
                color: #A9BECA;
                font-weight: 800;
                padding: 5px 12px;
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

            QComboBox QAbstractItemView {
                background-color: #0B1B26;
                color: #F4FAFD;
                border: 1px solid #2B526A;
                selection-background-color: #245B79;
                selection-color: #FFFFFF;
                outline: none;
                padding: 3px;
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
                background-color: #162D3A;
                color: #DDEAF2;
                border: 1px solid #2A4E62;
            }

            QPushButton#smallPrimaryButton,
            QPushButton#pauseButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QPushButton#pauseButton:checked {
                background-color: #705C16;
                border: 1px solid #B49326;
            }

            QTableWidget {
                background-color: #081722;
                alternate-background-color: #0B1C28;
                color: #E9F2F6;
                gridline-color: #19384A;
                border: 1px solid #1A3D52;
                selection-background-color: #205774;
                selection-color: #FFFFFF;
            }

            QHeaderView::section {
                background-color: #102737;
                color: #DDEAF2;
                border: 1px solid #1C4053;
                padding: 4px;
                font-weight: 700;
            }

            QSplitter::handle {
                background-color: #17374A;
                width: 2px;
            }
            """
        )

    # ------------------------------------------------------------------ shortcuts/settings

    def _install_shortcuts(self):
        action = QAction(self)
        action.setShortcut(
            QKeySequence(Qt.Key_Space)
        )
        action.triggered.connect(
            self.toggle_pause_shortcut
        )
        self.addAction(action)

    def current_fps(self):
        return int(
            self.fps_combo.currentData()
            or DEFAULT_RENDER_FPS
        )

    def current_buffer_ms(self):
        return int(
            self.buffer_combo.currentData()
            or DEFAULT_BUFFER_MS
        )

    def current_wave_span_s(self):
        return float(
            self.wave_span_combo.currentData()
            or DEFAULT_WAVEFORM_SPAN_S
        )

    def current_ratio_history_s(self):
        return int(
            self.ratio_history_combo.currentData()
            or DEFAULT_RATIO_HISTORY_S
        )

    def current_max_events(self):
        return int(
            self.max_events_combo.currentData()
            or DEFAULT_MAX_EVENTS
        )

    def _set_render_fps(
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

    def on_fps_changed(
        self,
        *_args,
    ):
        self._set_render_fps(
            self.current_fps()
        )

    def on_display_setting_changed(
        self,
        *_args,
    ):
        self._reset_playhead()
        self._update_worker_display_count()

        self.wave_plot.setXRange(
            -self.current_wave_span_s(),
            0.0,
            padding=0.0,
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

    def _update_worker_display_count(
        self,
    ):
        if not hasattr(
            self,
            "worker",
        ):
            return

        seconds = (
            self.current_wave_span_s()
            + self.current_buffer_ms()
            / 1000.0
            + max(
                DEFAULT_LTA_S,
                float(
                    self.lta_spin.value()
                )
                if hasattr(
                    self,
                    "lta_spin",
                )
                else DEFAULT_LTA_S,
            )
            + 1.0
        )

        self.worker.set_display_count(
            int(
                seconds
                * self.current_sample_rate_hz()
            )
            + 128
        )

    def _detector_settings(
        self,
    ) -> DetectorSettings:
        return DetectorSettings(
            sta_s=float(
                self.sta_spin.value()
            ),
            lta_s=float(
                self.lta_spin.value()
            ),
            trigger_on=float(
                self.trigger_on_spin.value()
            ),
            trigger_off=float(
                self.trigger_off_spin.value()
            ),
            release_s=float(
                self.release_spin.value()
            ),
            min_event_s=float(
                self.min_event_spin.value()
            ),
            deadtime_s=float(
                self.deadtime_spin.value()
            ),
            baseline_tau_s=float(
                self.baseline_spin.value()
            ),
            min_vector_rms=float(
                self.min_rms_spin.value()
            ),
            enabled=bool(
                self.detection_enabled.isChecked()
            ),
        )

    def _push_detector_settings(
        self,
    ):
        if not hasattr(
            self,
            "worker",
        ):
            return

        self.worker.set_detector_settings(
            self._detector_settings()
        )

        self._update_threshold_lines()
        self._update_worker_display_count()

    def apply_detector_settings(
        self,
    ):
        if (
            self.sta_spin.value()
            >= self.lta_spin.value()
        ):
            QMessageBox.warning(
                self,
                APP_TITLE,
                "STA Window must be shorter than LTA Window.",
            )
            return

        if (
            self.trigger_off_spin.value()
            >= self.trigger_on_spin.value()
        ):
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Trigger OFF should be lower than Trigger ON.",
            )
            return

        self._push_detector_settings()

    def _update_threshold_lines(
        self,
    ):
        # During UI construction _build_plot_panel() is called before
        # _build_control_panel(). At that point the InfiniteLine objects already
        # exist, but trigger_on_spin / trigger_off_spin do not yet exist.
        #
        # Use the configured widgets when available; otherwise use the detector
        # defaults. _push_detector_settings() is called again after the full UI
        # has been built, so the lines are then synchronized with the widgets.
        if not hasattr(
            self,
            "trigger_on_line",
        ) or not hasattr(
            self,
            "trigger_off_line",
        ):
            return

        trigger_on = (
            float(
                self.trigger_on_spin.value()
            )
            if hasattr(
                self,
                "trigger_on_spin",
            )
            else float(
                DEFAULT_TRIGGER_ON
            )
        )

        trigger_off = (
            float(
                self.trigger_off_spin.value()
            )
            if hasattr(
                self,
                "trigger_off_spin",
            )
            else float(
                DEFAULT_TRIGGER_OFF
            )
        )

        self.trigger_on_line.setValue(
            trigger_on
        )
        self.trigger_off_line.setValue(
            trigger_off
        )

    # ------------------------------------------------------------------ callbacks

    def on_display_snapshot(
        self,
        adc,
        producer_rate_hz: float,
        publish_gap_ms: float,
        stream_info,
    ):
        previous_total = self.cached_total
        previous_session_id = int(
            self.adc_session_id
        )
        previous_effective_rate = float(
            self.effective_sample_rate_hz
        )

        self.cached_adc = adc
        self.cached_total = int(
            adc.total_samples
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

        min_rate = max(
            0.001,
            self.current_sample_rate_hz()
            * PRODUCER_RATE_MIN_RATIO,
        )
        max_rate = max(
            min_rate * 2.0,
            self.current_sample_rate_hz()
            * PRODUCER_RATE_MAX_RATIO,
        )

        if (
            min_rate
            <= float(
                producer_rate_hz
            )
            <= max_rate
        ):
            self.producer_rate_hz = float(
                producer_rate_hz
            )

        if publish_gap_ms >= 0.0:
            if self.publish_gap_ms <= 0.0:
                self.publish_gap_ms = float(
                    publish_gap_ms
                )
            else:
                self.publish_gap_ms = (
                    0.85
                    * self.publish_gap_ms
                    + 0.15
                    * float(
                        publish_gap_ms
                    )
                )

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
            and self.cached_total
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
            self.publish_gap_ms = 0.0
            self._reset_playhead()
            self.underruns = 0
            self._in_underrun = False
            self._update_worker_display_count()

    def on_detector_state(
        self,
        state: DetectorState,
    ):
        self.latest_state = state

        now = (
            state.timestamp_monotonic
        )

        self.ratio_times.append(
            now
        )
        self.ratio_values.append(
            state.ratio
        )

        cutoff = (
            now
            - max(
                RATIO_HISTORY_CHOICES_S
            )
            - 5.0
        )

        while (
            self.ratio_times
            and self.ratio_times[0]
            < cutoff
        ):
            self.ratio_times.popleft()
            self.ratio_values.popleft()

    def on_event_detected(
        self,
        event: DetectedEvent,
    ):
        self.events.appendleft(
            event
        )

        while (
            len(
                self.events
            )
            > self.current_max_events()
        ):
            self.events.pop()

        self._rebuild_event_table()

    def on_worker_error(
        self,
        message: str,
    ):
        self.detector_info_label.setText(
            f"Detector error: {message}"
        )

    # ------------------------------------------------------------------ event table

    def _rebuild_event_table(self):
        self.event_table.setRowCount(
            len(
                self.events
            )
        )

        for row, event in enumerate(
            self.events
        ):
            values = (
                str(
                    event.event_id
                ),
                format_timestamp_ns(
                    event.start_timestamp_ns
                ),
                f"{event.duration_s:.3f} s",
                f"{event.peak_vector:,.0f}",
                f"{event.peak_x:,.0f}",
                f"{event.peak_y:,.0f}",
                f"{event.peak_z:,.0f}",
                f"{event.max_ratio:.2f}",
                event.quality_note,
                str(
                    event.start_absolute_sample
                ),
            )

            for col, value in enumerate(
                values
            ):
                item = QTableWidgetItem(
                    value
                )

                if col in (
                    0,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    9,
                ):
                    item.setTextAlignment(
                        Qt.AlignRight
                        | Qt.AlignVCenter
                    )

                self.event_table.setItem(
                    row,
                    col,
                    item,
                )

    def clear_event_log(self):
        self.events.clear()
        self.event_table.setRowCount(
            0
        )

    def export_events_csv(self):
        if not self.events:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Event log is empty.",
            )
            return

        filename, _ = (
            QFileDialog.getSaveFileName(
                self,
                "Export Event Log",
                "geophone_events.csv",
                "CSV Files (*.csv)",
            )
        )

        if not filename:
            return

        try:
            with open(
                filename,
                "w",
                newline="",
                encoding="utf-8",
            ) as handle:
                writer = csv.writer(
                    handle
                )

                writer.writerow(
                    [
                        "event_id",
                        "start_timestamp_ns",
                        "start_local_time",
                        "end_timestamp_ns",
                        "duration_s",
                        "peak_vector",
                        "peak_x",
                        "peak_y",
                        "peak_z",
                        "max_sta_lta",
                        "quality_note",
                        "start_absolute_sample",
                        "end_absolute_sample",
                    ]
                )

                for event in reversed(
                    self.events
                ):
                    writer.writerow(
                        [
                            event.event_id,
                            event.start_timestamp_ns,
                            format_timestamp_ns(
                                event.start_timestamp_ns
                            ),
                            event.end_timestamp_ns,
                            event.duration_s,
                            event.peak_vector,
                            event.peak_x,
                            event.peak_y,
                            event.peak_z,
                            event.max_ratio,
                            event.quality_note,
                            event.start_absolute_sample,
                            event.end_absolute_sample,
                        ]
                    )

        except Exception as exc:
            QMessageBox.critical(
                self,
                APP_TITLE,
                f"Failed to export CSV:\n\n{exc}",
            )

    # ------------------------------------------------------------------ smooth playhead

    def _reset_playhead(self):
        self.playhead_sample = None
        self.playhead_wall_ns = None

    def _display_sample_index(
        self,
        adc,
    ):
        total = int(
            adc.total_samples
        )

        if total <= 1:
            return 0.0

        latest = float(
            total - 1
        )
        oldest = float(
            total
            - len(
                adc.ch0
            )
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
            configured_buffer
            * 0.70,
            (
                3.0
                * gap_s
                + 0.100
            )
            * rate,
        )

        safety_reserve = max(
            configured_buffer
            * 0.35,
            (
                2.0
                * gap_s
                + 0.050
            )
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
                target_reserve
                * 0.75,
            ),
        )

        now_ns = (
            time.perf_counter_ns()
        )

        if (
            self.playhead_sample is None
            or self.playhead_wall_ns is None
        ):
            self.playhead_sample = max(
                oldest,
                latest
                - target_reserve,
            )

            self.playhead_wall_ns = (
                now_ns
            )

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
            / max(
                1.0,
                rate,
            )
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
                error_fraction
                * 0.40,
            ),
        )

        playback_rate = (
            rate
            * (
                1.0
                + correction
            )
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
            proposed = (
                max_playhead
            )

            if not self._in_underrun:
                self.underruns += 1
                self._in_underrun = True

        else:
            recovery = max(
                16.0,
                rate
                * gap_s
                * 0.50,
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

    # ------------------------------------------------------------------ waveform rendering

    @staticmethod
    def _downsample(
        x,
        y,
    ):
        count = len(
            y
        )

        if count <= MAX_WAVE_RENDER_POINTS:
            return (
                x,
                y,
                1,
            )

        step = int(
            math.ceil(
                count
                / MAX_WAVE_RENDER_POINTS
            )
        )

        return (
            x[::step],
            y[::step],
            step,
        )

    def _gap_breaks(
        self,
        x,
        y,
        step: int,
    ):
        if len(y) < 2:
            return (
                y,
                "all",
            )

        expected = (
            max(
                1,
                int(step),
            )
            / self.current_sample_rate_hz()
        )

        gaps = np.flatnonzero(
            np.diff(x)
            > expected
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

    def _render_waveform(self):
        adc = self.cached_adc

        if (
            adc is None
            or len(
                adc.ch0
            )
            < 2
        ):
            return

        playhead = (
            self._display_sample_index(
                adc
            )
        )

        count = len(
            adc.ch0
        )

        cache_start = (
            int(
                adc.total_samples
            )
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
            return

        fractional = (
            playhead
            - np.floor(
                playhead
            )
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
                self.current_wave_span_s()
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

        ts = (
            adc.timestamp_ns[
                start_index:
                end_index
            ]
        )

        if len(ts) < 2:
            return

        x_time = (
            ts.astype(
                np.float64,
                copy=False,
            )
            - float(
                display_ns
            )
        ) / 1_000_000_000.0

        finite_all = []

        for signal, curve in (
            (
                adc.ch0[
                    start_index:
                    end_index
                ],
                self.curve_x,
            ),
            (
                adc.ch1[
                    start_index:
                    end_index
                ],
                self.curve_y,
            ),
            (
                adc.ch2[
                    start_index:
                    end_index
                ],
                self.curve_z,
            ),
        ):
            (
                x_render,
                y_render,
                step,
            ) = self._downsample(
                x_time,
                signal,
            )

            (
                y_render,
                connect_mode,
            ) = self._gap_breaks(
                x_render,
                y_render,
                step,
            )

            curve.setData(
                x_render,
                y_render,
                connect=connect_mode,
            )

            finite = y_render[
                np.isfinite(
                    y_render
                )
            ]

            if len(finite):
                finite_all.append(
                    finite
                )

        if finite_all:
            values = np.concatenate(
                finite_all
            )

            low = float(
                np.percentile(
                    values,
                    1.0,
                )
            )
            high = float(
                np.percentile(
                    values,
                    99.0,
                )
            )

            if high <= low:
                margin = max(
                    1.0,
                    abs(low)
                    * 0.05,
                )
            else:
                margin = (
                    high - low
                ) * 0.10

            self.wave_plot.setYRange(
                low - margin,
                high + margin,
                padding=0.0,
            )

    def _render_ratio(self):
        if len(
            self.ratio_times
        ) < 2:
            return

        times = np.asarray(
            self.ratio_times,
            dtype=np.float64,
        )

        values = np.asarray(
            self.ratio_values,
            dtype=np.float64,
        )

        now = times[-1]

        history = float(
            self.current_ratio_history_s()
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
        values = values[
            start:
        ]

        if len(
            times
        ) > MAX_RATIO_POINTS:
            step = int(
                math.ceil(
                    len(times)
                    / MAX_RATIO_POINTS
                )
            )
            times = times[::step]
            values = values[::step]

        x = (
            times
            - now
        )

        self.ratio_curve.setData(
            x,
            values,
        )

        self.ratio_plot.setXRange(
            -history,
            0.0,
            padding=0.0,
        )

        upper = max(
            float(
                self.trigger_on_spin.value()
            )
            * 1.5,
            float(
                np.percentile(
                    values,
                    99.0,
                )
            )
            * 1.15
            if len(values)
            else 5.0,
            2.0,
        )

        self.ratio_plot.setYRange(
            0.0,
            upper,
            padding=0.0,
        )

    # ------------------------------------------------------------------ render / state

    def _set_detector_state_label(
        self,
        state: str,
    ):
        state = str(
            state
        ).upper()

        names = {
            "ARMED": "detectorArmed",
            "TRIGGERED": "detectorTriggered",
            "DEADTIME": "detectorDeadtime",
            "DISABLED": "detectorDisabled",
            "WAITING": "detectorWaiting",
        }

        self.detector_state_label.setText(
            state
        )

        self.detector_state_label.setObjectName(
            names.get(
                state,
                "detectorWaiting",
            )
        )

        self.detector_state_label.style().unpolish(
            self.detector_state_label
        )
        self.detector_state_label.style().polish(
            self.detector_state_label
        )

    def _update_state_widgets(self):
        state = self.latest_state

        if state is None:
            return

        self._set_detector_state_label(
            state.detector_state
        )

        self.ratio_card["value"].setText(
            f"{state.ratio:.2f}"
        )
        self.rms_card["value"].setText(
            f"{state.vector_rms:,.0f}"
        )
        self.duration_card["value"].setText(
            f"{state.current_event_duration_s:.2f} s"
        )
        self.peak_card["value"].setText(
            f"{state.current_peak_vector:,.0f}"
        )
        self.count_card["value"].setText(
            str(
                state.event_count
            )
        )

        self.detector_info_label.setText(
            f"{state.detector_state} | "
            f"STA/LTA {state.ratio:.2f} | "
            f"Fs {self.effective_sample_rate_hz:.1f} Hz "
            f"(raw {self.raw_sample_rate_hz:.1f}/N{self.decimation_samples}) | "
            f"producer {state.producer_rate_hz:.1f} Hz | "
            f"reserve {self.reserve_ms:.0f} ms | "
            f"underrun {self.underruns}"
        )

        self.bulk_label.setText(
            f"Frames: {state.frames_received:,}\n"
            f"Dropped: {state.dropped_frames:,}   "
            f"Resets: {state.sequence_resets:,}\n"
            f"Malformed: {state.malformed_frames:,}   "
            f"CH-ID mismatch: {state.channel_id_mismatches:,}\n"
            f"ERROR words: {state.error_flag_words:,}\n"
            f"Unsettled: {state.filter_not_settled_words:,}   "
            f"Repeated: {state.repeated_words:,}   "
            f"Saturated: {state.saturated_words:,}"
        )

    def render_frame(self):
        if not self.paused:
            self._render_waveform()
            self._render_ratio()

        self._update_state_widgets()
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
                / self.current_fps()
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

    # ------------------------------------------------------------------ pause/status

    def toggle_pause(
        self,
        checked: bool,
    ):
        self.paused = bool(
            checked
        )

        if self.paused:
            self.pause_button.setText(
                "Continue Display"
            )
        else:
            self.pause_button.setText(
                "Pause Display"
            )
            self.playhead_wall_ns = (
                time.perf_counter_ns()
            )

    def toggle_pause_shortcut(self):
        checked = not (
            self.pause_button.isChecked()
        )

        self.pause_button.setChecked(
            checked
        )

        self.toggle_pause(
            checked
        )

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

            settings = self._detector_settings()
            fs = self.current_sample_rate_hz()
            sta_samples = max(1, int(round(settings.sta_s * fs)))
            lta_samples = max(sta_samples + 1, int(round(settings.lta_s * fs)))

            tooltip = (
                f"Executable: {sys.executable}\n"
                "Detection continues even when the display is paused.\n"
                "The GUI uses one PyQtGraph/OpenGL viewport.\n"
                f"ADC: raw {self.raw_sample_rate_hz:.3f} Hz / "
                f"N={self.decimation_samples} -> "
                f"effective {fs:.3f} Hz.\n"
                f"STA {settings.sta_s:.3f}s = {sta_samples} samples; "
                f"LTA {settings.lta_s:.3f}s = {lta_samples} samples."
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
            + ", ".join(missing),
        )
        return 1

    try:
        window = (
            GeophoneEventWindow()
        )

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Event Monitor:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
