"""
miniseed_recording.py
=====================

GRC-UGM-PERTAMINA OBS
MiniSEED Recording

Version: 2
Shared data: shared_data_v5.py

Recorded MiniSEED channels
--------------------------
Geophone:
    CH0 / X -> EH1   (editable)
    CH1 / Y -> EH2   (editable)
    CH2 / Z -> EHZ   (editable)

IMU:
    Roll  -> RLL     (editable)
    Pitch -> PIT     (editable)
    Yaw   -> YAW     (editable)

Geophone data are recorded AFTER decimation. Decimation uses a continuous
anti-alias FIR low-pass filter and maintains filter state across normal TCP
chunks. Timestamp gaps reset the FIR state and create a new MiniSEED trace
segment; missing ADC data are never fabricated.

IMU data are currently low-rate telemetry (~1 Hz). The nominal IMU recording
rate is editable because firmware rate will be increased later.

USBL position
-------------
MiniSEED waveform records do not normally carry station latitude/longitude as
sample metadata. Therefore this recorder stores OBS position in:

    station.xml          StationXML, using USBL position at start/first fix
    usbl_position.csv    time history of USBL fixes during recording
    session_metadata.json

The waveform files remain proper MiniSEED time-series data.

Dependencies
------------
    pip install PySide6 numpy obspy

No SciPy dependency is required. The legacy FIR helper remains only as a
pass-through gap segmenter with factor=1; it does not decimate the v5 stream
implementation.
"""

from __future__ import annotations

import configparser
import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.MINISEED"


def configure_windows_runtime() -> None:
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
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                kernel32.FreeConsole()
        except Exception:
            pass

    except Exception:
        pass


configure_windows_runtime()


# =============================================================================
# Qt
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


# =============================================================================
# NumPy / ObsPy
# =============================================================================

try:
    import numpy as np
except ImportError:
    np = None

try:
    from obspy import Stream, Trace, UTCDateTime
    from obspy.core.inventory import (
        Channel,
        Inventory,
        Network,
        Site,
        Station,
    )

    OBSPY_AVAILABLE = True
    OBSPY_ERROR = ""

except Exception as exc:
    Stream = None
    Trace = None
    UTCDateTime = None
    Channel = None
    Inventory = None
    Network = None
    Site = None
    Station = None

    OBSPY_AVAILABLE = False
    OBSPY_ERROR = str(exc)


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

APP_TITLE = "MiniSEED Recording"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
EXTERNAL_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else BASE_DIR
)
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

OBS_SETTINGS_INI = EXTERNAL_DIR / "obs_settings.ini"

DEFAULT_NETWORK = "RM"
DEFAULT_STATION = "OBS01"
DEFAULT_LOCATION = "00"

DEFAULT_GEO_X_CHANNEL = "EH1"
DEFAULT_GEO_Y_CHANNEL = "EH2"
DEFAULT_GEO_Z_CHANNEL = "EHZ"

DEFAULT_ROLL_CHANNEL = "RLL"
DEFAULT_PITCH_CHANNEL = "PIT"
DEFAULT_YAW_CHANNEL = "YAW"

DEFAULT_IMU_RATE_HZ = 1.0

# Buffered writing keeps MiniSEED record overhead reasonable.
GEOPHONE_FLUSH_SECONDS = 10.0
IMU_FLUSH_SECONDS = 60.0

WORKER_POLL_MS = 10
GUI_STATUS_MS = 250

GEOPHONE_MSEED_RECLEN = 4096
IMU_MSEED_RECLEN = 512

MAX_NEW_ADC_REQUEST = int(
    RAW_ADC_SAMPLE_RATE_HZ
    * 120
)


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


def default_record_folder() -> Path:
    """
    Prefer [Recording]/miniseed_folder from obs_settings.ini.
    """

    fallback = (
        EXTERNAL_DIR
        / "recordings"
        / "miniseed"
    )

    try:
        parser = configparser.ConfigParser()
        parser.read(
            OBS_SETTINGS_INI,
            encoding="utf-8",
        )

        value = parser.get(
            "Recording",
            "miniseed_folder",
            fallback=str(
                fallback
            ),
        ).strip()

        path = Path(value)

        if not path.is_absolute():
            path = (
                EXTERNAL_DIR
                / path
            )

        return path.resolve()

    except Exception:
        return fallback


def default_decimation_factor() -> int:
    """Recorder-local decimation is disabled in shared_data_v5 architecture."""
    return 1


def valid_position(
    position,
) -> bool:
    try:
        return (
            bool(
                position.valid
            )
            and math.isfinite(
                float(
                    position.latitude
                )
            )
            and math.isfinite(
                float(
                    position.longitude
                )
            )
            and -90.0
            <= float(
                position.latitude
            )
            <= 90.0
            and -180.0
            <= float(
                position.longitude
            )
            <= 180.0
        )
    except Exception:
        return False


def iso_utc_from_ns(
    timestamp_ns: int,
) -> str:
    if timestamp_ns <= 0:
        return ""

    seconds = (
        int(timestamp_ns)
        / 1_000_000_000.0
    )

    if UTCDateTime is not None:
        try:
            return str(
                UTCDateTime(
                    seconds
                )
            )
        except Exception:
            pass

    return time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(
            seconds
        ),
    )


def safe_filename_component(
    value: str,
) -> str:
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(
            value
        ).strip(),
    )

    return value or "OBS"


# =============================================================================
# Recording configuration
# =============================================================================


@dataclass(frozen=True)
class RecorderConfig:
    base_folder: str

    network: str
    station: str
    location: str

    geo_x_channel: str
    geo_y_channel: str
    geo_z_channel: str

    roll_channel: str
    pitch_channel: str
    yaw_channel: str

    # Global stream calibration captured when recording starts.
    raw_adc_sample_rate_hz: float
    input_geophone_rate_hz: float
    global_average_n: int

    # Kept as an explicit invariant: recorder-local decimation is always 1.
    decimation_factor: int
    imu_rate_hz: float

    record_geophone: bool
    record_imu: bool
    log_usbl: bool

    @property
    def output_geophone_rate_hz(
        self,
    ) -> float:
        return float(
            self.input_geophone_rate_hz
        )


# =============================================================================
# Streaming FIR decimator
# =============================================================================


class StreamingFIRDecimator:
    """
    Continuous three-channel FIR decimator.

    Important properties:
    - filter state survives normal shared-memory/TCP chunks;
    - timestamp gaps reset state instead of filtering across missing data;
    - output timestamp is corrected for FIR group delay;
    - no samples are synthesized to fill gaps.
    """

    def __init__(
        self,
        factor: int,
        sample_rate_hz: float,
    ):
        self.factor = max(
            1,
            int(
                factor
            ),
        )

        self.sample_rate_hz = float(
            sample_rate_hz
        )

        self.raw_interval_ns = int(
            round(
                1_000_000_000.0
                / self.sample_rate_hz
            )
        )

        if self.factor <= 1:
            self.taps = np.array(
                [1.0],
                dtype=np.float64,
            )
        else:
            # 8*D+1 gives a practical transition width for this application.
            # Cap at 511 taps to keep real-time CPU cost bounded.
            ntaps = max(
                31,
                8
                * self.factor
                + 1,
            )

            ntaps = min(
                511,
                ntaps,
            )

            if ntaps % 2 == 0:
                ntaps += 1

            n = np.arange(
                ntaps,
                dtype=np.float64,
            ) - (
                ntaps - 1
            ) / 2.0

            # New Nyquist after downsampling:
            #       Fs_out/2 = Fs/(2D)
            # Use 80% of it as passband cutoff:
            #       Fc = 0.4 * Fs/D
            cutoff_cycles_per_sample = (
                0.40
                / self.factor
            )

            taps = (
                2.0
                * cutoff_cycles_per_sample
                * np.sinc(
                    2.0
                    * cutoff_cycles_per_sample
                    * n
                )
            )

            taps *= np.hamming(
                ntaps
            )

            taps /= np.sum(
                taps
            )

            self.taps = taps

        self.state_length = (
            len(
                self.taps
            )
            - 1
        )

        self.group_delay_samples = (
            (
                len(
                    self.taps
                )
                - 1
            )
            / 2.0
        )

        self.group_delay_ns = int(
            round(
                self.group_delay_samples
                * self.raw_interval_ns
            )
        )

        self.states = None
        self.phase = 0
        self.segment_processed = 0

        self.last_input_timestamp_ns = None

    def reset(
        self,
    ):
        self.states = None
        self.phase = 0
        self.segment_processed = 0
        self.last_input_timestamp_ns = None

    def _start_segment(
        self,
        first_values,
    ):
        if self.state_length > 0:
            self.states = np.vstack(
                [
                    np.full(
                        self.state_length,
                        float(
                            first_values[
                                channel
                            ]
                        ),
                        dtype=np.float64,
                    )
                    for channel in range(
                        3
                    )
                ]
            )
        else:
            self.states = np.zeros(
                (
                    3,
                    0,
                ),
                dtype=np.float64,
            )

        self.phase = 0
        self.segment_processed = 0

    def _filter_segment(
        self,
        signals,
        timestamps_ns,
    ):
        signals = np.asarray(
            signals,
            dtype=np.float64,
        )

        timestamps_ns = np.asarray(
            timestamps_ns,
            dtype=np.int64,
        )

        count = signals.shape[
            1
        ]

        if count <= 0:
            return None

        if self.states is None:
            self._start_segment(
                signals[
                    :,
                    0
                ]
            )

        if self.factor <= 1:
            filtered = signals

        else:
            filtered = np.empty_like(
                signals,
                dtype=np.float64,
            )

            for channel in range(
                3
            ):
                extended = np.concatenate(
                    (
                        self.states[
                            channel
                        ],
                        signals[
                            channel
                        ],
                    )
                )

                convolution = np.convolve(
                    extended,
                    self.taps,
                    mode="full",
                )

                begin = (
                    self.state_length
                )

                filtered[
                    channel
                ] = convolution[
                    begin:
                    begin + count
                ]

                if self.state_length > 0:
                    self.states[
                        channel
                    ] = extended[
                        -self.state_length:
                    ]

        local_indices = np.arange(
            count,
            dtype=np.int64,
        )

        select = (
            (
                self.phase
                + local_indices
            )
            % self.factor
        ) == 0

        # Suppress startup samples until enough real input exists to cover
        # symmetric FIR group delay. This prevents artificial pre-start data.
        if self.factor > 1:
            warm = (
                self.segment_processed
                + local_indices
            ) >= int(
                math.ceil(
                    self.group_delay_samples
                )
            )

            select &= warm

        output_signals = filtered[
            :,
            select
        ]

        output_timestamps = (
            timestamps_ns[
                select
            ]
            - self.group_delay_ns
        )

        self.phase = (
            self.phase
            + count
        ) % self.factor

        self.segment_processed += (
            count
        )

        self.last_input_timestamp_ns = int(
            timestamps_ns[
                -1
            ]
        )

        if output_signals.shape[
            1
        ] <= 0:
            return None

        return (
            output_signals,
            output_timestamps,
        )

    def process(
        self,
        signals,
        timestamps_ns,
    ):
        """
        Returns a list of contiguous decimated output segments:
            [(signals_3xN, timestamps_N), ...]
        """

        signals = np.asarray(
            signals,
            dtype=np.float64,
        )

        timestamps_ns = np.asarray(
            timestamps_ns,
            dtype=np.int64,
        )

        count = min(
            signals.shape[
                1
            ],
            len(
                timestamps_ns
            ),
        )

        if count <= 0:
            return []

        signals = signals[
            :,
            :count
        ]

        timestamps_ns = (
            timestamps_ns[
                :count
            ]
        )

        outputs = []

        # Gap against previous input chunk.
        if (
            self.last_input_timestamp_ns
            is not None
        ):
            delta = (
                int(
                    timestamps_ns[
                        0
                    ]
                )
                - int(
                    self.last_input_timestamp_ns
                )
            )

            if (
                delta
                <= 0
                or delta
                > int(
                    1.5
                    * self.raw_interval_ns
                )
            ):
                self.states = None
                self.phase = 0
                self.segment_processed = 0

        diffs = np.diff(
            timestamps_ns
        )

        gap_locations = np.flatnonzero(
            (
                diffs <= 0
            )
            | (
                diffs
                > int(
                    1.5
                    * self.raw_interval_ns
                )
            )
        )

        starts = [
            0
        ] + [
            int(
                index
                + 1
            )
            for index in gap_locations
        ]

        ends = [
            int(
                index
                + 1
            )
            for index in gap_locations
        ] + [
            count
        ]

        for segment_number, (
            start,
            end,
        ) in enumerate(
            zip(
                starts,
                ends,
            )
        ):
            if (
                segment_number > 0
            ):
                self.states = None
                self.phase = 0
                self.segment_processed = 0

            result = self._filter_segment(
                signals[
                    :,
                    start:end
                ],
                timestamps_ns[
                    start:end
                ],
            )

            if result is not None:
                outputs.append(
                    result
                )

        return outputs


# =============================================================================
# MiniSEED helpers
# =============================================================================


def append_miniseed_stream(
    path: Path,
    stream,
    reclen: int,
):
    buffer = io.BytesIO()

    stream.write(
        buffer,
        format="MSEED",
        encoding="FLOAT32",
        reclen=int(
            reclen
        ),
    )

    payload = buffer.getvalue()

    with path.open(
        "ab"
    ) as handle:
        handle.write(
            payload
        )

    return len(
        payload
    )


def make_trace(
    data,
    *,
    config: RecorderConfig,
    channel: str,
    start_timestamp_ns: int,
    sample_rate_hz: float,
):
    trace = Trace(
        data=np.asarray(
            data,
            dtype=np.float32,
        )
    )

    trace.stats.network = (
        config.network
    )
    trace.stats.station = (
        config.station
    )
    trace.stats.location = (
        config.location
    )
    trace.stats.channel = (
        channel
    )

    trace.stats.starttime = UTCDateTime(
        int(
            start_timestamp_ns
        )
        / 1_000_000_000.0
    )

    trace.stats.sampling_rate = float(
        sample_rate_hz
    )

    trace.stats.mseed = {
        "dataquality": "D"
    }

    return trace


# =============================================================================
# Recording worker
# =============================================================================


class MiniSeedRecordingWorker(QThread):
    status_changed = Signal(str)
    stats_changed = Signal(object)
    recording_error = Signal(str)
    recording_finished = Signal(str)

    def __init__(
        self,
        config: RecorderConfig,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.config = config

        self._stop_event = (
            threading.Event()
        )

        self.session_folder = None

        self.raw_adc_samples = 0
        self.decimated_samples = 0
        self.imu_samples = 0
        self.bytes_written = 0
        self.recorder_lag_samples = 0

        self.stationxml_written = False

        self.geo_buffer = [
            [],
            [],
            [],
        ]
        self.geo_time_buffer = []

        self.imu_buffer = [
            [],
            [],
            [],
        ]
        self.imu_time_buffer = []

        self.last_adc_total = None
        self.last_telemetry_timestamp_ns = -1
        self.last_usbl_timestamp_ns = -1

        self.geophone_file = None
        self.imu_file = None
        self.usbl_csv_file = None
        self.usbl_csv_writer = None

        self.session_start_ns = (
            time.time_ns()
        )

    def stop_recording(
        self,
    ):
        self._stop_event.set()

    # ------------------------------------------------------------------ setup

    def _create_session_folder(
        self,
    ):
        base = Path(
            self.config.base_folder
        )

        base.mkdir(
            parents=True,
            exist_ok=True,
        )

        station = (
            safe_filename_component(
                self.config.station
            )
        )

        stamp = time.strftime(
            "%Y%m%d_%H%M%S"
        )

        folder = (
            base
            / f"MSEED_{station}_{stamp}"
        )

        counter = 1

        while folder.exists():
            folder = (
                base
                / (
                    f"MSEED_{station}_"
                    f"{stamp}_{counter:02d}"
                )
            )
            counter += 1

        folder.mkdir(
            parents=True,
            exist_ok=False,
        )

        self.session_folder = (
            folder
        )

        self.geophone_file = (
            folder
            / f"{station}_geophone.mseed"
        )

        self.imu_file = (
            folder
            / f"{station}_imu.mseed"
        )

        self.usbl_csv_file = (
            folder
            / "usbl_position.csv"
        )

    def _write_metadata_json(
        self,
        shared,
    ):
        usbl = shared.read_usbl()
        telemetry = (
            shared.read_telemetry()
        )

        metadata = {
            "application": (
                "GRC-UGM-PERTAMINA OBS"
            ),
            "module": (
                "MiniSEED Recording"
            ),
            "session_start_utc": (
                iso_utc_from_ns(
                    self.session_start_ns
                )
            ),
            "config": asdict(
                self.config
            ),
            "raw_adc_sample_rate_hz": (
                self.config.raw_adc_sample_rate_hz
            ),
            "global_average_n": (
                self.config.global_average_n
            ),
            "recorded_geophone_sample_rate_hz": (
                self.config.output_geophone_rate_hz
            ),
            "miniseed_encoding": (
                "FLOAT32"
            ),
            "geophone_record_length_bytes": (
                GEOPHONE_MSEED_RECLEN
            ),
            "imu_record_length_bytes": (
                IMU_MSEED_RECLEN
            ),
            "usbl_at_start": {
                "valid": bool(
                    usbl.valid
                ),
                "timestamp_ns": int(
                    usbl.timestamp_ns
                ),
                "latitude": float(
                    usbl.latitude
                ),
                "longitude": float(
                    usbl.longitude
                ),
                "altitude": float(
                    usbl.altitude
                ),
                "fix_quality": int(
                    usbl.fix_quality
                ),
                "satellites": int(
                    usbl.satellites
                ),
                "hdop": float(
                    usbl.hdop
                ),
            },
            "depth_m_at_start": float(
                telemetry.depth
            ),
            "notes": [
                (
                    "Geophone channels are "
                    "anti-alias filtered and "
                    "recorded after decimation."
                ),
                (
                    "IMU channels use the "
                    "configured nominal IMU rate."
                ),
                (
                    "USBL coordinates are stored "
                    "in StationXML and "
                    "usbl_position.csv rather "
                    "than waveform MiniSEED "
                    "records."
                ),
            ],
        }

        path = (
            self.session_folder
            / "session_metadata.json"
        )

        path.write_text(
            json.dumps(
                metadata,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _open_usbl_csv(
        self,
    ):
        handle = (
            self.usbl_csv_file.open(
                "w",
                newline="",
                encoding="utf-8",
            )
        )

        writer = csv.writer(
            handle
        )

        writer.writerow(
            [
                "timestamp_ns",
                "utc_time",
                "valid",
                "latitude_deg",
                "longitude_deg",
                "altitude_m",
                "fix_quality",
                "satellites",
                "hdop",
            ]
        )

        self._usbl_handle = (
            handle
        )
        self.usbl_csv_writer = (
            writer
        )

    def _write_stationxml(
        self,
        usbl,
        telemetry,
    ):
        if (
            self.stationxml_written
            or not valid_position(
                usbl
            )
        ):
            return

        latitude = float(
            usbl.latitude
        )
        longitude = float(
            usbl.longitude
        )
        elevation = float(
            usbl.altitude
        )

        depth = max(
            0.0,
            float(
                telemetry.depth
            ),
        )

        station = Station(
            code=self.config.station,
            latitude=latitude,
            longitude=longitude,
            elevation=elevation,
            creation_date=UTCDateTime(),
            site=Site(
                name=(
                    "OBS position from USBL"
                )
            ),
        )

        channel_specs = []

        if self.config.record_geophone:
            for channel_code in (
                self.config.geo_x_channel,
                self.config.geo_y_channel,
                self.config.geo_z_channel,
            ):
                channel_specs.append(
                    (
                        channel_code,
                        self.config.output_geophone_rate_hz,
                    )
                )

        if self.config.record_imu:
            for channel_code in (
                self.config.roll_channel,
                self.config.pitch_channel,
                self.config.yaw_channel,
            ):
                channel_specs.append(
                    (
                        channel_code,
                        self.config.imu_rate_hz,
                    )
                )

        for channel_code, rate in (
            channel_specs
        ):
            station.channels.append(
                Channel(
                    code=channel_code,
                    location_code=(
                        self.config.location
                    ),
                    latitude=latitude,
                    longitude=longitude,
                    elevation=elevation,
                    depth=depth,
                    azimuth=0.0,
                    dip=0.0,
                    sample_rate=float(
                        rate
                    ),
                )
            )

        network = Network(
            code=self.config.network,
            stations=[
                station
            ],
        )

        inventory = Inventory(
            networks=[
                network
            ],
            source=(
                "GRC-UGM-PERTAMINA OBS"
            ),
        )

        inventory.write(
            str(
                self.session_folder
                / "station.xml"
            ),
            format="STATIONXML",
        )

        self.stationxml_written = True

    # ------------------------------------------------------------------ USBL

    def _record_usbl(
        self,
        shared,
    ):
        if not self.config.log_usbl:
            return

        usbl = (
            shared.read_usbl()
        )

        timestamp_ns = int(
            usbl.timestamp_ns
        )

        if (
            timestamp_ns <= 0
            or timestamp_ns
            == self.last_usbl_timestamp_ns
        ):
            return

        self.last_usbl_timestamp_ns = (
            timestamp_ns
        )

        self.usbl_csv_writer.writerow(
            [
                timestamp_ns,
                iso_utc_from_ns(
                    timestamp_ns
                ),
                int(
                    bool(
                        usbl.valid
                    )
                ),
                float(
                    usbl.latitude
                ),
                float(
                    usbl.longitude
                ),
                float(
                    usbl.altitude
                ),
                int(
                    usbl.fix_quality
                ),
                int(
                    usbl.satellites
                ),
                float(
                    usbl.hdop
                ),
            ]
        )

        try:
            self._usbl_handle.flush()
        except Exception:
            pass

        if not self.stationxml_written:
            telemetry = (
                shared.read_telemetry()
            )

            self._write_stationxml(
                usbl,
                telemetry,
            )

    # ------------------------------------------------------------------ geophone

    def _append_geo_segment(
        self,
        signals,
        timestamps_ns,
    ):
        if signals.shape[
            1
        ] <= 0:
            return

        for channel in range(
            3
        ):
            self.geo_buffer[
                channel
            ].append(
                np.asarray(
                    signals[
                        channel
                    ],
                    dtype=np.float32,
                )
            )

        self.geo_time_buffer.append(
            np.asarray(
                timestamps_ns,
                dtype=np.int64,
            )
        )

        self.decimated_samples += int(
            signals.shape[
                1
            ]
        )

    def _geo_buffer_count(
        self,
    ):
        if not self.geo_time_buffer:
            return 0

        return int(
            sum(
                len(
                    item
                )
                for item
                in self.geo_time_buffer
            )
        )

    def _flush_geophone(
        self,
    ):
        count = (
            self._geo_buffer_count()
        )

        if count <= 0:
            return

        timestamps = np.concatenate(
            self.geo_time_buffer
        )

        x = np.concatenate(
            self.geo_buffer[
                0
            ]
        )
        y = np.concatenate(
            self.geo_buffer[
                1
            ]
        )
        z = np.concatenate(
            self.geo_buffer[
                2
            ]
        )

        # Buffers only contain one contiguous FIR segment. If a gap is detected
        # by the worker it flushes BEFORE appending the next segment.
        start_ns = int(
            timestamps[
                0
            ]
        )

        stream = Stream(
            traces=[
                make_trace(
                    x,
                    config=self.config,
                    channel=(
                        self.config.geo_x_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.output_geophone_rate_hz
                    ),
                ),
                make_trace(
                    y,
                    config=self.config,
                    channel=(
                        self.config.geo_y_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.output_geophone_rate_hz
                    ),
                ),
                make_trace(
                    z,
                    config=self.config,
                    channel=(
                        self.config.geo_z_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.output_geophone_rate_hz
                    ),
                ),
            ]
        )

        self.bytes_written += (
            append_miniseed_stream(
                self.geophone_file,
                stream,
                GEOPHONE_MSEED_RECLEN,
            )
        )

        self.geo_buffer = [
            [],
            [],
            [],
        ]
        self.geo_time_buffer = []

    def _record_new_adc(
        self,
        shared,
        decimator,
    ):
        total = (
            shared.adc_total_samples()
        )

        if self.last_adc_total is None:
            self.last_adc_total = int(
                total
            )
            return

        if total < self.last_adc_total:
            # Source session reset.
            self._flush_geophone()
            decimator.reset()
            self.last_adc_total = int(
                total
            )
            return

        new_count = (
            int(
                total
            )
            - int(
                self.last_adc_total
            )
        )

        if new_count <= 0:
            return

        request_count = min(
            new_count,
            MAX_NEW_ADC_REQUEST,
        )

        if request_count < new_count:
            self.recorder_lag_samples += (
                new_count
                - request_count
            )

        adc = (
            shared.read_adc_latest_numpy(
                request_count
            )
        )

        actual = len(
            adc.ch0
        )

        actual_rate_hz = float(
            adc.sample_rate_hz
        )
        expected_rate_hz = float(
            self.config.output_geophone_rate_hz
        )
        if abs(actual_rate_hz - expected_rate_hz) > max(
            1.0e-6,
            1.0e-6 * expected_rate_hz,
        ):
            raise RuntimeError(
                "ADC effective sample rate changed during recording "
                f"({expected_rate_hz:.6f} -> {actual_rate_hz:.6f} Hz). "
                "Stop and restart recording after changing OBS decimation."
            )

        if actual <= 0:
            self.last_adc_total = int(
                total
            )
            return

        if actual < request_count:
            self.recorder_lag_samples += (
                request_count
                - actual
            )

        self.raw_adc_samples += int(
            actual
        )

        signals = np.vstack(
            (
                adc.ch0,
                adc.ch1,
                adc.ch2,
            )
        )

        timestamps_ns = np.asarray(
            adc.timestamp_ns,
            dtype=np.int64,
        )

        segments = (
            decimator.process(
                signals,
                timestamps_ns,
            )
        )

        for index, (
            decimated,
            output_timestamps,
        ) in enumerate(
            segments
        ):
            # If decimator returned multiple segments, a timestamp gap occurred.
            # Flush the old MiniSEED trace before starting the new segment.
            if (
                index > 0
                or (
                    self.geo_time_buffer
                    and int(
                        output_timestamps[
                            0
                        ]
                    )
                    - int(
                        self.geo_time_buffer[
                            -1
                        ][
                            -1
                        ]
                    )
                    > int(
                        1.5
                        * (
                            1_000_000_000.0
                            / self.config.output_geophone_rate_hz
                        )
                    )
                )
            ):
                self._flush_geophone()

            self._append_geo_segment(
                decimated,
                output_timestamps,
            )

        self.last_adc_total = int(
            total
        )

        flush_samples = max(
            1,
            int(
                round(
                    GEOPHONE_FLUSH_SECONDS
                    * self.config.output_geophone_rate_hz
                )
            ),
        )

        if (
            self._geo_buffer_count()
            >= flush_samples
        ):
            self._flush_geophone()

    # ------------------------------------------------------------------ IMU

    def _flush_imu(
        self,
    ):
        count = len(
            self.imu_time_buffer
        )

        if count <= 0:
            return

        start_ns = int(
            self.imu_time_buffer[
                0
            ]
        )

        roll = np.asarray(
            self.imu_buffer[
                0
            ],
            dtype=np.float32,
        )
        pitch = np.asarray(
            self.imu_buffer[
                1
            ],
            dtype=np.float32,
        )
        yaw = np.asarray(
            self.imu_buffer[
                2
            ],
            dtype=np.float32,
        )

        stream = Stream(
            traces=[
                make_trace(
                    roll,
                    config=self.config,
                    channel=(
                        self.config.roll_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.imu_rate_hz
                    ),
                ),
                make_trace(
                    pitch,
                    config=self.config,
                    channel=(
                        self.config.pitch_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.imu_rate_hz
                    ),
                ),
                make_trace(
                    yaw,
                    config=self.config,
                    channel=(
                        self.config.yaw_channel
                    ),
                    start_timestamp_ns=(
                        start_ns
                    ),
                    sample_rate_hz=(
                        self.config.imu_rate_hz
                    ),
                ),
            ]
        )

        self.bytes_written += (
            append_miniseed_stream(
                self.imu_file,
                stream,
                IMU_MSEED_RECLEN,
            )
        )

        self.imu_samples += int(
            count
        )

        self.imu_buffer = [
            [],
            [],
            [],
        ]
        self.imu_time_buffer = []

    def _record_imu(
        self,
        shared,
    ):
        telemetry = (
            shared.read_telemetry()
        )

        timestamp_ns = int(
            telemetry.timestamp_ns
        )

        if (
            timestamp_ns <= 0
            or timestamp_ns
            == self.last_telemetry_timestamp_ns
        ):
            return

        expected_interval_ns = int(
            round(
                1_000_000_000.0
                / self.config.imu_rate_hz
            )
        )

        if (
            self.imu_time_buffer
        ):
            delta_ns = (
                timestamp_ns
                - int(
                    self.imu_time_buffer[
                        -1
                    ]
                )
            )

            # Preserve gaps / rate changes as separate MiniSEED traces rather
            # than pretending they are uniformly sampled.
            if (
                delta_ns <= 0
                or abs(
                    delta_ns
                    - expected_interval_ns
                )
                > int(
                    0.35
                    * expected_interval_ns
                )
            ):
                self._flush_imu()

        self.last_telemetry_timestamp_ns = (
            timestamp_ns
        )

        self.imu_time_buffer.append(
            timestamp_ns
        )

        self.imu_buffer[
            0
        ].append(
            float(
                telemetry.roll
            )
        )
        self.imu_buffer[
            1
        ].append(
            float(
                telemetry.pitch
            )
        )
        self.imu_buffer[
            2
        ].append(
            float(
                telemetry.yaw
            )
        )

        flush_samples = max(
            1,
            int(
                round(
                    IMU_FLUSH_SECONDS
                    * self.config.imu_rate_hz
                )
            ),
        )

        if (
            len(
                self.imu_time_buffer
            )
            >= flush_samples
        ):
            self._flush_imu()

    # ------------------------------------------------------------------ status

    def _emit_stats(
        self,
    ):
        elapsed_s = max(
            0.0,
            (
                time.time_ns()
                - self.session_start_ns
            )
            / 1_000_000_000.0,
        )

        self.stats_changed.emit(
            {
                "elapsed_s": (
                    elapsed_s
                ),
                "raw_adc_samples": (
                    self.raw_adc_samples
                ),
                "decimated_samples": (
                    self.decimated_samples
                ),
                "imu_samples": (
                    self.imu_samples
                    + len(
                        self.imu_time_buffer
                    )
                ),
                "bytes_written": (
                    self.bytes_written
                ),
                "recorder_lag_samples": (
                    self.recorder_lag_samples
                ),
                "session_folder": str(
                    self.session_folder
                    or ""
                ),
                "stationxml_written": (
                    self.stationxml_written
                ),
            }
        )

    # ------------------------------------------------------------------ thread

    def run(
        self,
    ):
        shared = None

        try:
            self._create_session_folder()

            shared = OBSSharedData()

            self._write_metadata_json(
                shared
            )

            self._open_usbl_csv()

            usbl = (
                shared.read_usbl()
            )
            telemetry = (
                shared.read_telemetry()
            )

            self._write_stationxml(
                usbl,
                telemetry,
            )

            if self.config.record_geophone:
                self.last_adc_total = int(
                    shared.adc_total_samples()
                )

            if self.config.record_imu:
                self.last_telemetry_timestamp_ns = int(
                    telemetry.timestamp_ns
                )

            # shared_data_v5 is already globally averaged/decimated.
            # Factor 1 keeps the existing timestamp-gap segmentation without
            # introducing a second decimation/filter stage.
            decimator = (
                StreamingFIRDecimator(
                    factor=1,
                    sample_rate_hz=(
                        self.config.output_geophone_rate_hz
                    ),
                )
            )

            self.status_changed.emit(
                f"Recording to {self.session_folder}"
            )

            last_stats_emit = 0.0

            while not self._stop_event.is_set():
                if self.config.record_geophone:
                    self._record_new_adc(
                        shared,
                        decimator,
                    )

                if self.config.record_imu:
                    self._record_imu(
                        shared
                    )

                if self.config.log_usbl:
                    self._record_usbl(
                        shared
                    )

                now = (
                    time.perf_counter()
                )

                if (
                    now
                    - last_stats_emit
                    >= 0.25
                ):
                    self._emit_stats()
                    last_stats_emit = (
                        now
                    )

                self.msleep(
                    WORKER_POLL_MS
                )

            self.status_changed.emit(
                "Finalizing MiniSEED..."
            )

            if self.config.record_geophone:
                self._flush_geophone()

            if self.config.record_imu:
                self._flush_imu()

            self._emit_stats()

            self.recording_finished.emit(
                str(
                    self.session_folder
                )
            )

        except Exception as exc:
            self.recording_error.emit(
                str(
                    exc
                )
            )

        finally:
            if hasattr(
                self,
                "_usbl_handle",
            ):
                try:
                    self._usbl_handle.flush()
                    self._usbl_handle.close()
                except Exception:
                    pass

            if shared is not None:
                try:
                    shared.close()
                except Exception:
                    pass


# =============================================================================
# Main window
# =============================================================================


class MiniSeedRecordingWindow(QMainWindow):

    def __init__(
        self,
    ):
        super().__init__()

        self.shared = (
            OBSSharedData()
        )

        self.worker = None
        self.recording = False

        self.setWindowTitle(
            f"{APP_TITLE} - {SYSTEM_TITLE}"
        )

        icon = application_icon()

        if not icon.isNull():
            self.setWindowIcon(
                icon
            )

        self.resize(
            1280,
            820,
        )
        self.setMinimumSize(
            980,
            680,
        )

        self._build_ui()
        self._apply_style()

        self.position_timer = QTimer(
            self
        )
        self.position_timer.timeout.connect(
            self.refresh_live_metadata
        )
        self.position_timer.start(
            500
        )

        self.refresh_live_metadata()
        self.update_decimation_info()

        if not OBSPY_AVAILABLE:
            self.start_button.setEnabled(
                False
            )

            self.record_state.setText(
                "OBSPY MISSING"
            )

            self.status_label.setText(
                "Install ObsPy: pip install obspy\n"
                f"{OBSPY_ERROR}"
            )

    # ------------------------------------------------------------------ UI

    def _build_ui(
        self,
    ):
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
            "MINISEED RECORDING"
        )
        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            "Geophone X/Y/Z • IMU Roll/Pitch/Yaw • USBL Position Metadata"
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

        self.record_state = QLabel(
            "STOPPED"
        )
        self.record_state.setObjectName(
            "stateStopped"
        )
        self.record_state.setAlignment(
            Qt.AlignCenter
        )
        self.record_state.setMinimumWidth(
            140
        )

        header.addWidget(
            self.record_state
        )

        root.addLayout(
            header
        )

        # Status.
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

        self.status_label = QLabel(
            "Ready"
        )
        self.status_label.setObjectName(
            "statusLabel"
        )
        self.status_label.setWordWrap(
            True
        )

        self.duration_label = QLabel(
            "00:00:00"
        )
        self.duration_label.setObjectName(
            "durationLabel"
        )

        sl.addWidget(
            self.status_label,
            1,
        )
        sl.addWidget(
            self.duration_label
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
            self._build_settings_panel()
        )

        splitter.addWidget(
            self._build_live_panel()
        )

        splitter.setStretchFactor(
            0,
            3
        )
        splitter.setStretchFactor(
            1,
            2
        )

        splitter.setSizes(
            [
                760,
                500,
            ]
        )

        root.addWidget(
            splitter,
            1,
        )

        # Start / stop.
        buttons = QHBoxLayout()

        self.start_button = QPushButton(
            "START RECORDING"
        )
        self.start_button.setObjectName(
            "startButton"
        )
        self.start_button.setMinimumHeight(
            46
        )
        self.start_button.clicked.connect(
            self.start_recording
        )

        self.stop_button = QPushButton(
            "STOP RECORDING"
        )
        self.stop_button.setObjectName(
            "stopButton"
        )
        self.stop_button.setMinimumHeight(
            46
        )
        self.stop_button.setEnabled(
            False
        )
        self.stop_button.clicked.connect(
            self.stop_recording
        )

        buttons.addWidget(
            self.start_button,
            1,
        )
        buttons.addWidget(
            self.stop_button,
            1,
        )

        root.addLayout(
            buttons
        )

    # ------------------------------------------------------------------ settings panel

    def _build_settings_panel(
        self,
    ):
        panel = QFrame()
        panel.setObjectName(
            "settingsPanel"
        )

        layout = QVBoxLayout(
            panel
        )
        layout.setContentsMargins(
            0, 0, 8, 0
        )
        layout.setSpacing(
            8
        )

        # Folder --------------------------------------------------------
        folder_group = QGroupBox(
            "Recording Folder"
        )
        folder_group.setObjectName(
            "controlGroup"
        )

        fg = QVBoxLayout(
            folder_group
        )
        fg.setContentsMargins(
            10, 14, 10, 10
        )
        fg.setSpacing(
            6
        )

        self.folder_edit = QLineEdit(
            str(
                default_record_folder()
            )
        )
        self.folder_edit.setReadOnly(
            True
        )

        choose_folder = QPushButton(
            "Choose Folder"
        )
        choose_folder.setObjectName(
            "secondaryButton"
        )
        choose_folder.clicked.connect(
            self.choose_folder
        )

        folder_note = QLabel(
            "Each Start creates a new session subfolder."
        )
        folder_note.setObjectName(
            "hintText"
        )

        fg.addWidget(
            self.folder_edit
        )
        fg.addWidget(
            choose_folder
        )
        fg.addWidget(
            folder_note
        )

        layout.addWidget(
            folder_group
        )

        # Station metadata ---------------------------------------------
        station_group = QGroupBox(
            "MiniSEED Station / Channel Codes"
        )
        station_group.setObjectName(
            "controlGroup"
        )

        sg = QGridLayout(
            station_group
        )
        sg.setContentsMargins(
            10, 14, 10, 10
        )
        sg.setHorizontalSpacing(
            8
        )
        sg.setVerticalSpacing(
            5
        )

        self.network_edit = QLineEdit(
            DEFAULT_NETWORK
        )
        self.station_edit = QLineEdit(
            DEFAULT_STATION
        )
        self.location_edit = QLineEdit(
            DEFAULT_LOCATION
        )

        self.geo_x_edit = QLineEdit(
            DEFAULT_GEO_X_CHANNEL
        )
        self.geo_y_edit = QLineEdit(
            DEFAULT_GEO_Y_CHANNEL
        )
        self.geo_z_edit = QLineEdit(
            DEFAULT_GEO_Z_CHANNEL
        )

        self.roll_edit = QLineEdit(
            DEFAULT_ROLL_CHANNEL
        )
        self.pitch_edit = QLineEdit(
            DEFAULT_PITCH_CHANNEL
        )
        self.yaw_edit = QLineEdit(
            DEFAULT_YAW_CHANNEL
        )

        fields = (
            (
                "Network",
                self.network_edit,
            ),
            (
                "Station",
                self.station_edit,
            ),
            (
                "Location",
                self.location_edit,
            ),
            (
                "Geophone X",
                self.geo_x_edit,
            ),
            (
                "Geophone Y",
                self.geo_y_edit,
            ),
            (
                "Geophone Z",
                self.geo_z_edit,
            ),
            (
                "IMU Roll",
                self.roll_edit,
            ),
            (
                "IMU Pitch",
                self.pitch_edit,
            ),
            (
                "IMU Yaw",
                self.yaw_edit,
            ),
        )

        for row, (
            name,
            widget,
        ) in enumerate(
            fields
        ):
            sg.addWidget(
                QLabel(
                    name
                ),
                row,
                0,
            )
            sg.addWidget(
                widget,
                row,
                1,
            )

        code_note = QLabel(
            "MiniSEED2 limits: Network ≤2, Station ≤5, "
            "Location ≤2, Channel = 3 characters."
        )
        code_note.setObjectName(
            "hintText"
        )
        code_note.setWordWrap(
            True
        )

        sg.addWidget(
            code_note,
            len(
                fields
            ),
            0,
            1,
            2,
        )

        layout.addWidget(
            station_group
        )

        # Rates / decimation -------------------------------------------
        rate_group = QGroupBox(
            "Sampling / Global Average"
        )
        rate_group.setObjectName(
            "controlGroup"
        )

        rg = QGridLayout(
            rate_group
        )
        rg.setContentsMargins(
            10, 14, 10, 10
        )
        rg.setHorizontalSpacing(
            8
        )
        rg.setVerticalSpacing(
            6
        )

        self.raw_rate_label = QLabel(
            "-- Hz"
        )
        self.raw_rate_label.setObjectName(
            "fixedValue"
        )

        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(
            1,
            1000
        )
        self.decimation_spin.setValue(1)
        self.decimation_spin.setEnabled(False)

        self.output_rate_label = QLabel(
            "-- Hz"
        )
        self.output_rate_label.setObjectName(
            "fixedValue"
        )

        self.anti_alias_label = QLabel(
            ""
        )
        self.anti_alias_label.setObjectName(
            "hintText"
        )
        self.anti_alias_label.setWordWrap(
            True
        )

        self.imu_rate_spin = (
            QDoubleSpinBox()
        )
        self.imu_rate_spin.setRange(
            0.1,
            1000.0
        )
        self.imu_rate_spin.setDecimals(
            3
        )
        self.imu_rate_spin.setValue(
            DEFAULT_IMU_RATE_HZ
        )
        self.imu_rate_spin.setSuffix(
            " Hz"
        )

        rg.addWidget(
            QLabel(
                "Raw ADC Rate"
            ),
            0,
            0,
        )
        rg.addWidget(
            self.raw_rate_label,
            0,
            1,
        )

        rg.addWidget(
            QLabel(
                "Global Average N"
            ),
            1,
            0,
        )
        rg.addWidget(
            self.decimation_spin,
            1,
            1,
        )

        rg.addWidget(
            QLabel(
                "Recorded Geo Rate (effective)"
            ),
            2,
            0,
        )
        rg.addWidget(
            self.output_rate_label,
            2,
            1,
        )

        rg.addWidget(
            QLabel(
                "IMU Nominal Rate"
            ),
            3,
            0,
        )
        rg.addWidget(
            self.imu_rate_spin,
            3,
            1,
        )

        rg.addWidget(
            self.anti_alias_label,
            4,
            0,
            1,
            2,
        )

        layout.addWidget(
            rate_group
        )

        # Include channels ---------------------------------------------
        include_group = QGroupBox(
            "Recorded Data"
        )
        include_group.setObjectName(
            "controlGroup"
        )

        ig = QVBoxLayout(
            include_group
        )
        ig.setContentsMargins(
            10, 14, 10, 10
        )

        self.record_geophone_check = QCheckBox(
            "Geophone X / Y / Z"
        )
        self.record_geophone_check.setChecked(
            True
        )

        self.record_imu_check = QCheckBox(
            "IMU Roll / Pitch / Yaw"
        )
        self.record_imu_check.setChecked(
            True
        )

        self.log_usbl_check = QCheckBox(
            "USBL coordinate metadata / history"
        )
        self.log_usbl_check.setChecked(
            True
        )

        ig.addWidget(
            self.record_geophone_check
        )
        ig.addWidget(
            self.record_imu_check
        )
        ig.addWidget(
            self.log_usbl_check
        )

        layout.addWidget(
            include_group
        )

        layout.addStretch(
            1
        )

        self.settings_panel = (
            panel
        )

        return panel

    # ------------------------------------------------------------------ live panel

    def _build_live_panel(
        self,
    ):
        panel = QFrame()
        panel.setObjectName(
            "livePanel"
        )

        layout = QVBoxLayout(
            panel
        )
        layout.setContentsMargins(
            8, 0, 0, 0
        )
        layout.setSpacing(
            8
        )

        # USBL ---------------------------------------------------------
        usbl_group = QGroupBox(
            "OBS Position — USBL"
        )
        usbl_group.setObjectName(
            "controlGroup"
        )

        ug = QVBoxLayout(
            usbl_group
        )
        ug.setContentsMargins(
            10, 14, 10, 10
        )

        self.usbl_fix_label = QLabel(
            "NO POSITION"
        )
        self.usbl_fix_label.setObjectName(
            "positionState"
        )
        self.usbl_fix_label.setAlignment(
            Qt.AlignCenter
        )

        self.usbl_position_label = QLabel(
            "Latitude  : --\n"
            "Longitude : --\n"
            "Altitude  : --\n"
            "Fix / Sat : --\n"
            "HDOP      : --"
        )
        self.usbl_position_label.setObjectName(
            "monoValue"
        )

        position_note = QLabel(
            "Coordinates are recorded to station.xml + "
            "usbl_position.csv. MiniSEED itself remains waveform data."
        )
        position_note.setObjectName(
            "hintText"
        )
        position_note.setWordWrap(
            True
        )

        ug.addWidget(
            self.usbl_fix_label
        )
        ug.addWidget(
            self.usbl_position_label
        )
        ug.addWidget(
            position_note
        )

        layout.addWidget(
            usbl_group
        )

        # Live source --------------------------------------------------
        source_group = QGroupBox(
            "Live Source"
        )
        source_group.setObjectName(
            "controlGroup"
        )

        src = QGridLayout(
            source_group
        )
        src.setContentsMargins(
            10, 14, 10, 10
        )

        self.adc_total_label = QLabel(
            "--"
        )
        self.adc_total_label.setObjectName(
            "monoValue"
        )

        self.imu_live_label = QLabel(
            "Roll --\nPitch --\nYaw --"
        )
        self.imu_live_label.setObjectName(
            "monoValue"
        )

        self.depth_live_label = QLabel(
            "Depth: -- m"
        )
        self.depth_live_label.setObjectName(
            "monoValue"
        )

        src.addWidget(
            QLabel(
                "ADC Total"
            ),
            0,
            0,
        )
        src.addWidget(
            self.adc_total_label,
            0,
            1,
        )
        src.addWidget(
            QLabel(
                "IMU"
            ),
            1,
            0,
        )
        src.addWidget(
            self.imu_live_label,
            1,
            1,
        )
        src.addWidget(
            QLabel(
                "Depth"
            ),
            2,
            0,
        )
        src.addWidget(
            self.depth_live_label,
            2,
            1,
        )

        layout.addWidget(
            source_group
        )

        # Recording stats ----------------------------------------------
        stats_group = QGroupBox(
            "Recording Statistics"
        )
        stats_group.setObjectName(
            "controlGroup"
        )

        st = QGridLayout(
            stats_group
        )
        st.setContentsMargins(
            10, 14, 10, 10
        )

        self.raw_samples_label = QLabel(
            "0"
        )
        self.decimated_samples_label = QLabel(
            "0"
        )
        self.imu_samples_label = QLabel(
            "0"
        )
        self.file_size_label = QLabel(
            "0 B"
        )
        self.lag_samples_label = QLabel(
            "0"
        )
        self.stationxml_label = QLabel(
            "Waiting for USBL fix"
        )

        for widget in (
            self.raw_samples_label,
            self.decimated_samples_label,
            self.imu_samples_label,
            self.file_size_label,
            self.lag_samples_label,
            self.stationxml_label,
        ):
            widget.setObjectName(
                "monoValue"
            )

        stats = (
            (
                "Shared Geo Read",
                self.raw_samples_label,
            ),
            (
                "Geo Recorded",
                self.decimated_samples_label,
            ),
            (
                "IMU Recorded",
                self.imu_samples_label,
            ),
            (
                "MiniSEED Size",
                self.file_size_label,
            ),
            (
                "Recorder Lag",
                self.lag_samples_label,
            ),
            (
                "StationXML",
                self.stationxml_label,
            ),
        )

        for row, (
            name,
            widget,
        ) in enumerate(
            stats
        ):
            st.addWidget(
                QLabel(
                    name
                ),
                row,
                0,
            )
            st.addWidget(
                widget,
                row,
                1,
            )

        layout.addWidget(
            stats_group
        )

        # Format notes -------------------------------------------------
        format_group = QGroupBox(
            "MiniSEED Format"
        )
        format_group.setObjectName(
            "controlGroup"
        )

        fl = QVBoxLayout(
            format_group
        )
        fl.setContentsMargins(
            10, 14, 10, 10
        )

        format_label = QLabel(
            "Encoding : FLOAT32\n"
            "Quality  : D (data)\n"
            "Geo file : 4096-byte MiniSEED records\n"
            "IMU file : 512-byte MiniSEED records\n"
            "Geo flush: 10 s\n"
            "IMU flush: 60 s"
        )
        format_label.setObjectName(
            "monoValue"
        )

        fl.addWidget(
            format_label
        )

        layout.addWidget(
            format_group
        )
        layout.addStretch(
            1
        )

        return panel

    # ------------------------------------------------------------------ validation/config

    @staticmethod
    def _valid_code(
        value: str,
        max_length: int,
        exact_length: Optional[
            int
        ] = None,
    ):
        value = str(
            value
        ).strip()

        if exact_length is not None:
            if len(
                value
            ) != exact_length:
                return False
        elif len(
            value
        ) > max_length:
            return False

        return bool(
            re.fullmatch(
                r"[A-Za-z0-9]*",
                value,
            )
        )

    def build_config(
        self,
    ) -> RecorderConfig:
        network = (
            self.network_edit.text()
            .strip()
            .upper()
        )

        station = (
            self.station_edit.text()
            .strip()
            .upper()
        )

        location = (
            self.location_edit.text()
            .strip()
            .upper()
        )

        channel_widgets = (
            self.geo_x_edit,
            self.geo_y_edit,
            self.geo_z_edit,
            self.roll_edit,
            self.pitch_edit,
            self.yaw_edit,
        )

        channels = [
            widget.text()
            .strip()
            .upper()
            for widget in (
                channel_widgets
            )
        ]

        if not self._valid_code(
            network,
            2,
        ):
            raise ValueError(
                "Network code must be "
                "0–2 alphanumeric characters."
            )

        if not self._valid_code(
            station,
            5,
        ) or not station:
            raise ValueError(
                "Station code must be "
                "1–5 alphanumeric characters."
            )

        if not self._valid_code(
            location,
            2,
        ):
            raise ValueError(
                "Location code must be "
                "0–2 alphanumeric characters."
            )

        for channel in channels:
            if not self._valid_code(
                channel,
                3,
                exact_length=3,
            ):
                raise ValueError(
                    "Every MiniSEED channel code "
                    "must be exactly 3 "
                    "alphanumeric characters."
                )

        if len(
            set(
                channels
            )
        ) != len(
            channels
        ):
            raise ValueError(
                "Channel codes must be unique."
            )

        folder = Path(
            self.folder_edit.text()
        )

        folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        stream_info = self.shared.read_adc_stream_info()

        return RecorderConfig(
            base_folder=str(
                folder
            ),
            network=network,
            station=station,
            location=location,
            geo_x_channel=channels[
                0
            ],
            geo_y_channel=channels[
                1
            ],
            geo_z_channel=channels[
                2
            ],
            roll_channel=channels[
                3
            ],
            pitch_channel=channels[
                4
            ],
            yaw_channel=channels[
                5
            ],
            raw_adc_sample_rate_hz=float(
                stream_info.raw_sample_rate_hz
            ),
            input_geophone_rate_hz=float(
                stream_info.effective_sample_rate_hz
            ),
            global_average_n=max(
                1,
                int(stream_info.decimation_samples),
            ),
            decimation_factor=1,
            imu_rate_hz=float(
                self.imu_rate_spin.value()
            ),
            record_geophone=bool(
                self.record_geophone_check.isChecked()
            ),
            record_imu=bool(
                self.record_imu_check.isChecked()
            ),
            log_usbl=bool(
                self.log_usbl_check.isChecked()
            ),
        )

    # ------------------------------------------------------------------ folder/rate

    def choose_folder(
        self,
    ):
        folder = (
            QFileDialog.getExistingDirectory(
                self,
                "Choose MiniSEED Recording Folder",
                self.folder_edit.text(),
            )
        )

        if folder:
            self.folder_edit.setText(
                folder
            )

    def update_decimation_info(
        self,
        *_args,
    ):
        try:
            info = self.shared.read_adc_stream_info()
            raw_rate = float(info.raw_sample_rate_hz)
            effective_rate = float(info.effective_sample_rate_hz)
            average_n = max(1, int(info.decimation_samples))

            self.raw_rate_label.setText(
                f"{raw_rate:.3f} Hz"
            )
            self.decimation_spin.setValue(
                average_n
            )
            self.output_rate_label.setText(
                f"{effective_rate:.3f} Hz"
            )
            self.anti_alias_label.setText(
                "Global averaging/decimation is already applied in OBS Setting "
                "before shared RAM. MiniSEED records this effective stream "
                "directly; no second decimation is performed."
            )
        except Exception as exc:
            self.raw_rate_label.setText("-- Hz")
            self.output_rate_label.setText("-- Hz")
            self.anti_alias_label.setText(
                f"ADC stream information unavailable: {exc}"
            )

    # ------------------------------------------------------------------ live metadata

    def refresh_live_metadata(
        self,
    ):
        self.update_decimation_info()
        try:
            usbl = (
                self.shared.read_usbl()
            )

            telemetry = (
                self.shared.read_telemetry()
            )

            total = (
                self.shared.adc_total_samples()
            )

            self.adc_total_label.setText(
                f"{total:,}"
            )

            self.imu_live_label.setText(
                f"Roll  {telemetry.roll:+.2f}°\n"
                f"Pitch {telemetry.pitch:+.2f}°\n"
                f"Yaw   {telemetry.yaw:+.2f}°"
            )

            self.depth_live_label.setText(
                f"Depth: {telemetry.depth:.2f} m"
            )

            if valid_position(
                usbl
            ):
                self.usbl_fix_label.setText(
                    "VALID USBL FIX"
                )
                self.usbl_fix_label.setObjectName(
                    "positionValid"
                )

                self.usbl_position_label.setText(
                    f"Latitude  : {usbl.latitude:.8f}\n"
                    f"Longitude : {usbl.longitude:.8f}\n"
                    f"Altitude  : {usbl.altitude:.2f} m\n"
                    f"Fix / Sat : {usbl.fix_quality} / "
                    f"{usbl.satellites}\n"
                    f"HDOP      : {usbl.hdop:.2f}"
                )

            else:
                self.usbl_fix_label.setText(
                    "NO VALID USBL POSITION"
                )
                self.usbl_fix_label.setObjectName(
                    "positionInvalid"
                )

                self.usbl_position_label.setText(
                    "Latitude  : --\n"
                    "Longitude : --\n"
                    "Altitude  : --\n"
                    "Fix / Sat : --\n"
                    "HDOP      : --"
                )

            self.usbl_fix_label.style().unpolish(
                self.usbl_fix_label
            )
            self.usbl_fix_label.style().polish(
                self.usbl_fix_label
            )

        except Exception as exc:
            if not self.recording:
                self.status_label.setText(
                    f"Shared RAM error: {exc}"
                )

    # ------------------------------------------------------------------ recording

    def set_settings_enabled(
        self,
        enabled: bool,
    ):
        self.settings_panel.setEnabled(
            enabled
        )

    def start_recording(
        self,
    ):
        if self.recording:
            return

        if np is None:
            QMessageBox.critical(
                self,
                APP_TITLE,
                "NumPy is required.",
            )
            return

        if not OBSPY_AVAILABLE:
            QMessageBox.critical(
                self,
                APP_TITLE,
                "ObsPy is required.\n\n"
                "Install:\n"
                "pip install obspy",
            )
            return

        try:
            config = (
                self.build_config()
            )

        except Exception as exc:
            QMessageBox.warning(
                self,
                APP_TITLE,
                str(
                    exc
                ),
            )
            return

        if not (
            config.record_geophone
            or config.record_imu
        ):
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Select at least one recorded data source.",
            )
            return

        self.worker = (
            MiniSeedRecordingWorker(
                config,
                self,
            )
        )

        self.worker.status_changed.connect(
            self.on_worker_status
        )
        self.worker.stats_changed.connect(
            self.on_worker_stats
        )
        self.worker.recording_error.connect(
            self.on_worker_error
        )
        self.worker.recording_finished.connect(
            self.on_worker_finished
        )

        self.recording = True

        self.set_settings_enabled(
            False
        )

        self.start_button.setEnabled(
            False
        )
        self.stop_button.setEnabled(
            True
        )

        self.record_state.setText(
            "● RECORDING"
        )
        self.record_state.setObjectName(
            "stateRecording"
        )

        self.record_state.style().unpolish(
            self.record_state
        )
        self.record_state.style().polish(
            self.record_state
        )

        usbl = (
            self.shared.read_usbl()
        )

        if not valid_position(
            usbl
        ):
            self.status_label.setText(
                "Recording starting — USBL has no valid "
                "position yet. StationXML will be created "
                "when the first valid USBL fix arrives."
            )
        else:
            self.status_label.setText(
                "Starting MiniSEED recording..."
            )

        self.worker.start()

    def stop_recording(
        self,
    ):
        if (
            not self.recording
            or self.worker is None
        ):
            return

        self.stop_button.setEnabled(
            False
        )

        self.record_state.setText(
            "FINALIZING"
        )
        self.record_state.setObjectName(
            "stateFinalizing"
        )

        self.record_state.style().unpolish(
            self.record_state
        )
        self.record_state.style().polish(
            self.record_state
        )

        self.status_label.setText(
            "Finalizing MiniSEED records..."
        )

        self.worker.stop_recording()

    def on_worker_status(
        self,
        text: str,
    ):
        self.status_label.setText(
            text
        )

    @staticmethod
    def human_bytes(
        size: int,
    ):
        value = float(
            size
        )

        for unit in (
            "B",
            "KB",
            "MB",
            "GB",
            "TB",
        ):
            if value < 1024.0:
                return (
                    f"{value:.2f} {unit}"
                )

            value /= 1024.0

        return (
            f"{value:.2f} PB"
        )

    def on_worker_stats(
        self,
        stats,
    ):
        elapsed = int(
            stats.get(
                "elapsed_s",
                0.0,
            )
        )

        hours = (
            elapsed
            // 3600
        )
        minutes = (
            elapsed
            % 3600
        ) // 60
        seconds = (
            elapsed
            % 60
        )

        self.duration_label.setText(
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

        self.raw_samples_label.setText(
            f"{int(stats.get('raw_adc_samples', 0)):,}"
        )

        self.decimated_samples_label.setText(
            f"{int(stats.get('decimated_samples', 0)):,}"
        )

        self.imu_samples_label.setText(
            f"{int(stats.get('imu_samples', 0)):,}"
        )

        self.file_size_label.setText(
            self.human_bytes(
                int(
                    stats.get(
                        "bytes_written",
                        0,
                    )
                )
            )
        )

        lag = int(
            stats.get(
                "recorder_lag_samples",
                0,
            )
        )

        self.lag_samples_label.setText(
            f"{lag:,}"
        )

        self.stationxml_label.setText(
            "Written"
            if stats.get(
                "stationxml_written",
                False,
            )
            else "Waiting for USBL fix"
        )

    def on_worker_error(
        self,
        message: str,
    ):
        QMessageBox.critical(
            self,
            APP_TITLE,
            f"Recording error:\n\n{message}",
        )

        self._return_to_stopped(
            (
                "Recording stopped due to error: "
                + message
            )
        )

    def on_worker_finished(
        self,
        folder: str,
    ):
        self._return_to_stopped(
            f"Recording saved: {folder}"
        )

    def _return_to_stopped(
        self,
        status_text: str,
    ):
        self.recording = False

        self.set_settings_enabled(
            True
        )

        self.start_button.setEnabled(
            OBSPY_AVAILABLE
        )
        self.stop_button.setEnabled(
            False
        )

        self.record_state.setText(
            "STOPPED"
        )
        self.record_state.setObjectName(
            "stateStopped"
        )

        self.record_state.style().unpolish(
            self.record_state
        )
        self.record_state.style().polish(
            self.record_state
        )

        self.status_label.setText(
            status_text
        )

        self.worker = None

    # ------------------------------------------------------------------ style

    def _apply_style(
        self,
    ):
        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QLabel {
                color: #FFFFFF;
                background: transparent;
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

            QLabel#durationLabel {
                color: #FFFFFF;
                font-family: "Consolas";
                font-size: 18px;
                font-weight: 900;
                padding-left: 10px;
            }

            QLabel#stateStopped {
                background: #172631;
                border: 1px solid #35546A;
                border-radius: 7px;
                color: #A9BECA;
                font-weight: 900;
                padding: 5px 12px;
            }

            QLabel#stateRecording {
                background: #571C24;
                border: 1px solid #D14C5E;
                border-radius: 7px;
                color: #FFD2D8;
                font-weight: 900;
                padding: 5px 12px;
            }

            QLabel#stateFinalizing {
                background: #403510;
                border: 1px solid #A88821;
                border-radius: 7px;
                color: #FFE49A;
                font-weight: 900;
                padding: 5px 12px;
            }

            QGroupBox#controlGroup {
                background: #0D1E2A;
                border: 1px solid #1A3D52;
                border-radius: 9px;
                margin-top: 11px;
                padding-top: 6px;
                color: #FFFFFF;
                font-weight: 800;
            }

            QGroupBox#controlGroup::title {
                subcontrol-origin: margin;
                left: 9px;
                padding: 0 5px;
                color: #FFFFFF;
            }

            QLabel#hintText {
                color: #7894A4;
                font-size: 9px;
            }

            QLabel#fixedValue {
                color: #DDEAF2;
                font-family: "Consolas";
                font-size: 11px;
                font-weight: 800;
            }

            QLabel#monoValue {
                color: #DDEAF2;
                font-family: "Consolas";
                font-size: 10px;
            }

            QLabel#positionValid {
                background: #123A2D;
                border: 1px solid #2D8E66;
                border-radius: 6px;
                color: #A9F1D2;
                font-weight: 900;
                padding: 5px;
            }

            QLabel#positionInvalid,
            QLabel#positionState {
                background: #403510;
                border: 1px solid #A88821;
                border-radius: 6px;
                color: #FFE49A;
                font-weight: 900;
                padding: 5px;
            }

            QLineEdit,
            QSpinBox,
            QDoubleSpinBox {
                background: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 5px;
                min-height: 27px;
                padding: 2px 6px;
            }

            QLineEdit:read-only {
                color: #B8CBD6;
                background: #091821;
            }

            QCheckBox {
                color: #DDE9EF;
                spacing: 6px;
            }

            QPushButton {
                min-height: 29px;
                border-radius: 6px;
                padding: 4px 8px;
                font-weight: 800;
                background: #162D3A;
                color: #DDEAF2;
                border: 1px solid #2A4E62;
            }

            QPushButton#secondaryButton {
                background: #123147;
                border: 1px solid #285B78;
            }

            QPushButton#startButton {
                background: #176B4C;
                border: 1px solid #35A775;
                color: #E4FFF3;
                font-size: 13px;
            }

            QPushButton#stopButton {
                background: #6A1F29;
                border: 1px solid #B84454;
                color: #FFD7DC;
                font-size: 13px;
            }

            QPushButton:disabled {
                background: #101D25;
                color: #50636E;
                border: 1px solid #21323C;
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
        if (
            self.recording
            and self.worker is not None
        ):
            answer = (
                QMessageBox.question(
                    self,
                    APP_TITLE,
                    "Recording is active. Stop and close?",
                    QMessageBox.Yes
                    | QMessageBox.No,
                    QMessageBox.No,
                )
            )

            if answer != QMessageBox.Yes:
                event.ignore()
                return

            self.worker.stop_recording()
            self.worker.wait(
                5000
            )

        try:
            self.position_timer.stop()
        except Exception:
            pass

        try:
            self.shared.close()
        except Exception:
            pass

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

    try:
        window = (
            MiniSeedRecordingWindow()
        )

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start MiniSEED Recording:\n\n{exc}",
        )
        return 1

    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
