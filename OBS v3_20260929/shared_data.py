"""
shared_data.py
=================

GRC-UGM-PERTAMINA OBS shared-memory API.

Version: 10

Runtime canonical module: shared_data.py

Purpose
-------
Version 10 keeps the Version 5 shared-memory binary layout and finalizes the
acquisition-rate contract for the corrected firmware. The OBS ADC design/expected
raw rate is 1000 Hz. The dynamic ADC stream record remains the authoritative source
for consumers, while a separate diagnostic record carries a long-window PC-side
measured rate, ppm error and PASS/WARN/FAULT status. The diagnostic measurement is
verification only and never changes waveform metadata or resamples ADC samples.

Version 8 extended Version 7 with centralized OBS USB-logger/STRG0 status,
command-broker status, and acquisition-manager heartbeat/freshness timestamps.

All OBS network acquisition remains owned by obs_setting. Consumer GUIs read
telemetry, pressure, leak monitor, controller health, USB logger status and broker
status only from shared RAM. Command-producing GUIs use local IPC, never OBS TCP.

Version 5 separates the physical/raw ADC rate from the rate of the ADC stream
published to shared RAM after decimation/averaging.

Example:

    raw ADC rate       = 1000.000 Hz/channel (corrected firmware target)
    decimation_samples = 5 raw samples/output sample
    decimation_mode    = mean
    effective rate     = 1000.000 / 5 = 200.000 Hz/channel
    output period      = 5.000 ms

The September 2026 land-test value (976.580 Hz) is retained only as historical
diagnostic context; it is no longer the runtime default.

The shared ADC ring contains the PROCESSED/output stream. Therefore readers
such as Real-Time, FFT, Spectrogram, PSD, Event Monitor and MiniSEED must use:

    shared.read_adc_stream_info().effective_sample_rate_hz

or:

    adc_snapshot.sample_rate_hz

They must NOT assume that the shared ADC stream is always 1000 Hz.

Design
------
- New shared-memory name and v5 header prevent a v3/v4 process from silently
  attaching and interpreting the output stream with the old fixed-rate
  assumption.
- The proven v3 ring layout is retained for ADC / telemetry / GNSS / USBL /
  bulk diagnostics.
- A new ADC stream-information record is stored in unused header space.
- ADC timestamps use the EFFECTIVE output period.
- Real acquisition gaps are represented only through
  missing_output_samples_before; decimation itself is not represented as fake
  missing samples.
- Each new acquisition/configuration session receives a monotonically
  increasing adc_session_id.

Important terminology
---------------------
RAW_ADC_SAMPLE_RATE_HZ / ADC_SOURCE_SAMPLE_RATE_HZ
    Legacy protocol/design-rate compatibility constants (1000 Hz). They are kept
    unchanged because older code and the inherited v3 identity header use them.
    They are NOT the authoritative acquisition rate for new consumers.

DEFAULT_ADC_ACQUISITION_SAMPLE_RATE_HZ
    Current measured default for acquisition metadata (976.580 Hz from the
    September 2026 land test). OBS Setting may override this per acquisition
    session, for example to 1000.000 Hz after the firmware clock is corrected.

ADC_SAMPLE_RATE_HZ
    Legacy compatibility alias only. New visualization and recording modules
    must read ADCStreamInfoSnapshot.effective_sample_rate_hz.

effective_sample_rate_hz
    Actual nominal rate of samples stored in the shared ADC ring.

decimation_samples
    Number of consecutive raw ADC frames used for one output ADC frame.

decimation_mode
    "raw"  : no reduction, N=1
    "mean" : block averaging / arithmetic mean

Compatibility
-------------
This module imports the v3 implementation for the stable ring and telemetry
structures, but uses a NEW shared-memory identity:

    GRC_UGM_PERTAMINA_OBS_V5

Version 7 intentionally keeps the Version 5 shared-memory identity and binary
layout. Existing modules that use shared_data_v5/v6 continue to attach to the
same RAM. Version 7 adds the centralized $XCHM1 record in previously unused
header space; older readers simply ignore it. v3/v4 processes still cannot
safely share the v5/v6/v7 ADC stream.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass, replace
from typing import Iterable, Optional, Sequence

import shared_data_v3 as _v3
from shared_data_v3 import *  # noqa: F401,F403
from shared_data_v3 import OBSSharedData as _OBSSharedDataV3


# =============================================================================
# Public version / identity
# =============================================================================

SHARED_DATA_API_VERSION = 10
# Binary shared-memory layout remains Version 5 for backward compatibility.
VERSION = 5

DEFAULT_SHARED_MEMORY_NAME = "GRC_UGM_PERTAMINA_OBS_V5"
MAGIC = b"OBSRAM05"

# The OBS bulk protocol's physical/source ADC frame rate.
# Legacy protocol/design target. Keep this value unchanged for binary/header
# compatibility with the inherited v3/v5 shared-memory identity.
RAW_ADC_SAMPLE_RATE_HZ = float(
    _v3.ADC_SAMPLE_RATE_HZ
)
ADC_SOURCE_SAMPLE_RATE_HZ = (
    RAW_ADC_SAMPLE_RATE_HZ
)
ADC_DESIGN_SAMPLE_RATE_HZ = float(
    RAW_ADC_SAMPLE_RATE_HZ
)

# September 2026 Bandung land-test consensus from the pre-fix firmware. Keep it
# only as historical/reference information. It is NOT the runtime default.
ADC_LAND_TEST_MEASURED_SAMPLE_RATE_HZ = 976.580

# Final corrected-firmware contract. Runtime acquisition metadata starts at the
# 1000 Hz design rate and may later be replaced by authoritative firmware-reported
# metadata if the protocol exposes it. PC-side rate measurement is diagnostic only.
DEFAULT_ADC_ACQUISITION_SAMPLE_RATE_HZ = float(
    ADC_DESIGN_SAMPLE_RATE_HZ
)

ADC_RATE_PASS_PPM = 50.0
ADC_RATE_WARN_PPM = 1000.0
ADC_RATE_STATUS_WARMING_UP = 0
ADC_RATE_STATUS_PASS = 1
ADC_RATE_STATUS_WARN = 2
ADC_RATE_STATUS_FAULT = 3
ADC_RATE_STATUS_TO_TEXT = {
    ADC_RATE_STATUS_WARMING_UP: "WARMING UP",
    ADC_RATE_STATUS_PASS: "PASS",
    ADC_RATE_STATUS_WARN: "WARN",
    ADC_RATE_STATUS_FAULT: "FAULT",
}

# Backward-compatible source-rate alias only.
#
# Do NOT use this constant as the processed/shared ADC rate in new modules.
# Read ADCStreamInfoSnapshot.effective_sample_rate_hz instead.
ADC_SAMPLE_RATE_HZ = (
    RAW_ADC_SAMPLE_RATE_HZ
)

DEFAULT_DECIMATION_SAMPLES = 1

ADC_DECIMATION_MODE_RAW = 0
ADC_DECIMATION_MODE_MEAN = 1

ADC_DECIMATION_MODE_TO_CODE = {
    "raw": ADC_DECIMATION_MODE_RAW,
    "mean": ADC_DECIMATION_MODE_MEAN,
}

ADC_DECIMATION_CODE_TO_MODE = {
    value: key
    for key, value
    in ADC_DECIMATION_MODE_TO_CODE.items()
}


# =============================================================================
# v5 header extension
# =============================================================================

# Existing v3 header data ends below this point:
#   FIRMWARE_INFO  : 1408 .. 1647
#   ADC_META       : 1664 .. 1671
#
# 1728 keeps the new record naturally separated and safely inside the 4096-byte
# header area.
ADC_STREAM_INFO_OFFSET = 1728

# seq,
# update_timestamp_ns,
# raw_sample_rate_hz,
# decimation_samples,
# 4-byte alignment,
# effective_sample_rate_hz,
# sample_period_ns,
# adc_session_id,
# decimation_mode_code,
# 4-byte padding
ADC_STREAM_INFO_STRUCT = struct.Struct(
    "<QqdI4xdqQI4x"
)

assert (
    ADC_STREAM_INFO_STRUCT.size
    == 64
)

assert (
    ADC_STREAM_INFO_OFFSET
    + ADC_STREAM_INFO_STRUCT.size
    <= _v3.HEADER_SIZE
)

# Long-window PC-side ADC-rate diagnostic. This occupies the 64-byte gap between
# ADC_STREAM_INFO (ends at 1792) and GNSS_NMEA (starts at 1856), preserving the
# Version-5 binary identity and every existing record offset.
ADC_RATE_DIAGNOSTIC_OFFSET = 1792
# seq, timestamp_ns, expected_raw_hz, measured_raw_hz, measured_output_hz,
# error_ppm, status_code, measurement_window_ms, source_sample_span
ADC_RATE_DIAGNOSTIC_STRUCT = struct.Struct("<QqddddiIQ")
assert ADC_RATE_DIAGNOSTIC_STRUCT.size == 64
assert (
    ADC_RATE_DIAGNOSTIC_OFFSET + ADC_RATE_DIAGNOSTIC_STRUCT.size
    <= 1856
)

# Latest raw NMEA sentence diagnostics.
#
# These records deliberately live in header space that is unused by the v3/v5
# position and ADC layouts. Existing v5 readers remain binary-compatible.
NMEA_TEXT_BYTES = 256

GNSS_NMEA_OFFSET = 1856
USBL_NMEA_OFFSET = 2176

NMEA_SENTENCE_STRUCT = struct.Struct(
    f"<Qq{NMEA_TEXT_BYTES}s"
)

assert (
    GNSS_NMEA_OFFSET
    + NMEA_SENTENCE_STRUCT.size
    <= USBL_NMEA_OFFSET
)

assert (
    USBL_NMEA_OFFSET
    + NMEA_SENTENCE_STRUCT.size
    <= _v3.HEADER_SIZE
)

# Centralized $XCHM1 controller-health record.
# Kept in previously unused header space; binary shared-memory identity remains v5.
CONTROLLER_TELEMETRY_OFFSET = 2560
# seq, timestamp_ns, then 11 signed integer fields exactly as received:
# VMONI0,VMONI1,VMONI2,VMONI3,TEMP,PMON,HMON,LMON,OASM,ID,COUNT
CONTROLLER_TELEMETRY_STRUCT = struct.Struct("<Qq11q")
assert (
    CONTROLLER_TELEMETRY_OFFSET
    + CONTROLLER_TELEMETRY_STRUCT.size
    <= _v3.HEADER_SIZE
)

# Centralized $STRG0 / onboard USB logger status.
USB_LOGGER_STATUS_OFFSET = 2816
USB_STATUS_FILENAME_BYTES = 128
USB_STATUS_MESSAGE_BYTES = 192
USB_STATUS_RAW_BYTES = 256
# seq, timestamp_ns, device/media/logger tri-state ints, current file, last closed,
# normalized message string, raw STRG0 sentence.
USB_LOGGER_STATUS_STRUCT = struct.Struct(
    f"<Qqiii4x{USB_STATUS_FILENAME_BYTES}s{USB_STATUS_FILENAME_BYTES}s"
    f"{USB_STATUS_MESSAGE_BYTES}s{USB_STATUS_RAW_BYTES}s"
)
assert (
    USB_LOGGER_STATUS_OFFSET
    + USB_LOGGER_STATUS_STRUCT.size
    <= _v3.HEADER_SIZE
)

# Local command broker status. Consumers can diagnose whether OBS Setting is alive
# and whether its single physical TCP 54300 link is connected.
COMMAND_BROKER_STATUS_OFFSET = 3584
BROKER_SOURCE_BYTES = 48
BROKER_SENTENCE_BYTES = 192
BROKER_RESULT_BYTES = 128
COMMAND_BROKER_STATUS_STRUCT = struct.Struct(
    f"<Qqii{BROKER_SOURCE_BYTES}s{BROKER_SENTENCE_BYTES}s{BROKER_RESULT_BYTES}s"
)
assert (
    COMMAND_BROKER_STATUS_OFFSET
    + COMMAND_BROKER_STATUS_STRUCT.size
    <= _v3.HEADER_SIZE
)

# Acquisition-manager heartbeat and source freshness timestamps.
# This record fits in the remaining unused v5 header bytes. It does not change
# the shared-memory identity or the proven ADC ring layout.
ACQUISITION_HEALTH_OFFSET = 3992
# seq, heartbeat timestamp, pid, command_connected, data_connected, broker_running,
# then last RX/update timestamps for command, data, AHRS, depth, controller, GNSS,
# USBL and published ADC output.
ACQUISITION_HEALTH_STRUCT = struct.Struct("<Qqiiii8q")
assert (
    ACQUISITION_HEALTH_OFFSET
    + ACQUISITION_HEALTH_STRUCT.size
    <= _v3.HEADER_SIZE
)

HEARTBEAT_STALE_S = 2.0
DEFAULT_SOURCE_STALE_S = 3.0

_TRI_UNKNOWN = -1
_TRI_FALSE = 0
_TRI_TRUE = 1
_UNSET = object()


# =============================================================================
# Public ADC stream / NMEA snapshots
# =============================================================================

@dataclass(frozen=True)
class ADCStreamInfoSnapshot:
    timestamp_ns: int

    raw_sample_rate_hz: float
    decimation_samples: int
    effective_sample_rate_hz: float
    sample_period_ns: int

    adc_session_id: int
    decimation_mode: str

    @property
    def adc_raw_sample_rate_hz(
        self,
    ) -> float:
        """Explicit alias used by v10+ consumers for source/acquisition rate."""
        return float(
            self.raw_sample_rate_hz
        )

    @property
    def adc_output_sample_rate_hz(
        self,
    ) -> float:
        """Explicit alias used by v10+ consumers for the shared output rate."""
        return float(
            self.effective_sample_rate_hz
        )

    @property
    def output_sample_rate_hz(
        self,
    ) -> float:
        return float(
            self.effective_sample_rate_hz
        )

    @property
    def decimation_factor(
        self,
    ) -> int:
        return int(
            self.decimation_samples
        )

    @property
    def ring_history_seconds(
        self,
    ) -> float:
        rate = float(
            self.effective_sample_rate_hz
        )

        if rate <= 0.0:
            return 0.0

        return (
            float(
                _v3.ADC_CAPACITY
            )
            / rate
        )


@dataclass(frozen=True)
class ADCRateDiagnosticSnapshot:
    timestamp_ns: int
    expected_raw_sample_rate_hz: float
    measured_raw_sample_rate_hz: float
    measured_output_sample_rate_hz: float
    error_ppm: float
    status: str
    measurement_window_s: float
    source_sample_span: int

    @property
    def ready(self) -> bool:
        return (
            self.status in ("PASS", "WARN", "FAULT")
            and self.measured_raw_sample_rate_hz > 0.0
            and self.measurement_window_s > 0.0
        )


@dataclass(frozen=True)
class NMEASentenceSnapshot:
    timestamp_ns: int
    text: str


@dataclass(frozen=True)
class ControllerTelemetrySnapshot:
    timestamp_ns: int
    vmoni0_raw: int
    vmoni1_raw: int
    vmoni2_raw: int
    vmoni3_raw: int
    temp_raw: int
    pmon_raw: int
    hmon_raw: int
    lmon: int
    oasm: int
    device_id: int
    count: int

    @property
    def vmoni0_v(self) -> float:
        return float(self.vmoni0_raw) / 10.0

    @property
    def vmoni1_v(self) -> float:
        return float(self.vmoni1_raw) / 10.0

    @property
    def reserved_nonzero(self) -> bool:
        return any(value != 0 for value in (
            self.vmoni2_raw, self.vmoni3_raw, self.temp_raw,
            self.pmon_raw, self.hmon_raw,
        ))


@dataclass(frozen=True)
class USBLoggerStatusSnapshot:
    timestamp_ns: int
    device_present: Optional[bool]
    media_present: Optional[bool]
    logger_active: Optional[bool]
    current_filename: str
    last_closed_filename: str
    last_message: str
    last_raw_sentence: str


@dataclass(frozen=True)
class CommandBrokerStatusSnapshot:
    timestamp_ns: int
    broker_running: bool
    obs_command_connected: bool
    last_source: str
    last_sentence: str
    last_result: str


@dataclass(frozen=True)
class AcquisitionHealthSnapshot:
    timestamp_ns: int
    process_id: int
    command_connected: bool
    data_connected: bool
    broker_running: bool
    last_command_rx_ns: int
    last_data_rx_ns: int
    last_ahrs_ns: int
    last_depth_ns: int
    last_controller_ns: int
    last_gnss_ns: int
    last_usbl_ns: int
    last_adc_ns: int

    def heartbeat_age_s(self, now_ns: Optional[int] = None) -> float:
        return timestamp_age_seconds(self.timestamp_ns, now_ns=now_ns)

    def source_age_s(self, source: str, now_ns: Optional[int] = None) -> float:
        source = str(source).strip().lower()
        mapping = {
            "command": self.last_command_rx_ns,
            "data": self.last_data_rx_ns,
            "ahrs": self.last_ahrs_ns,
            "imu": self.last_ahrs_ns,
            "depth": self.last_depth_ns,
            "controller": self.last_controller_ns,
            "xchm1": self.last_controller_ns,
            "gnss": self.last_gnss_ns,
            "usbl": self.last_usbl_ns,
            "adc": self.last_adc_ns,
        }
        if source not in mapping:
            raise KeyError(f"Unknown acquisition source: {source}")
        return timestamp_age_seconds(mapping[source], now_ns=now_ns)

    def is_alive(self, stale_s: float = HEARTBEAT_STALE_S) -> bool:
        return self.process_id > 0 and self.heartbeat_age_s() <= float(stale_s)


def timestamp_age_seconds(timestamp_ns: int, *, now_ns: Optional[int] = None) -> float:
    timestamp_ns = int(timestamp_ns or 0)
    if timestamp_ns <= 0:
        return float("inf")
    if now_ns is None:
        now_ns = time.time_ns()
    return max(0.0, (int(now_ns) - timestamp_ns) / 1_000_000_000.0)


def freshness_state(timestamp_ns: int, *, stale_s: float = DEFAULT_SOURCE_STALE_S, now_ns: Optional[int] = None) -> str:
    age = timestamp_age_seconds(timestamp_ns, now_ns=now_ns)
    if age == float("inf"):
        return "WAITING"
    return "LIVE" if age <= float(stale_s) else "STALE"


# =============================================================================
# Helpers
# =============================================================================

def _normalize_decimation_mode(
    mode: str,
    decimation_samples: int,
) -> str:
    value = str(
        mode
    ).strip().lower()

    if not value:
        value = (
            "raw"
            if int(
                decimation_samples
            ) == 1
            else "mean"
        )

    if value not in (
        ADC_DECIMATION_MODE_TO_CODE
    ):
        raise ValueError(
            "decimation_mode must be "
            "'raw' or 'mean'"
        )

    if (
        value == "raw"
        and int(
            decimation_samples
        ) != 1
    ):
        raise ValueError(
            "decimation_mode='raw' "
            "requires decimation_samples=1"
        )

    return value


def _stream_parameters(
    *,
    raw_sample_rate_hz: float,
    decimation_samples: int,
    decimation_mode: str,
):
    raw_rate = float(
        raw_sample_rate_hz
    )

    if not (
        raw_rate > 0.0
    ):
        raise ValueError(
            "raw_sample_rate_hz must be > 0"
        )

    decimation = max(
        1,
        int(
            decimation_samples
        ),
    )

    mode = (
        _normalize_decimation_mode(
            decimation_mode,
            decimation,
        )
    )

    effective_rate = (
        raw_rate
        / float(
            decimation
        )
    )

    sample_period_ns = int(
        round(
            1_000_000_000.0
            / effective_rate
        )
    )

    if sample_period_ns <= 0:
        raise ValueError(
            "effective sample period is invalid"
        )

    return (
        raw_rate,
        decimation,
        mode,
        effective_rate,
        sample_period_ns,
    )


# =============================================================================
# Shared RAM class
# =============================================================================

class OBSSharedData(
    _OBSSharedDataV3
):
    """
    v5 shared-data API.

    The ADC ring stores output samples at the rate defined by
    ADCStreamInfoSnapshot.effective_sample_rate_hz.
    """

    def __init__(
        self,
        name: str = DEFAULT_SHARED_MEMORY_NAME,
    ):
        # _OBSSharedDataV3.__init__ allocates the proven v3-sized memory area.
        # It dynamically calls the overridden v5 initializer/validator below.
        super().__init__(
            name=name
        )

        self._adc_stream_info_lock = (
            threading.Lock()
        )

        self._adc_rate_diagnostic_lock = (
            threading.Lock()
        )

        self._protocol_clock_lock = (
            threading.Lock()
        )

        self._gnss_nmea_lock = (
            threading.Lock()
        )

        self._usbl_nmea_lock = (
            threading.Lock()
        )

        self._controller_telemetry_lock = (
            threading.Lock()
        )

        self._usb_logger_status_lock = (
            threading.Lock()
        )

        self._command_broker_status_lock = (
            threading.Lock()
        )

        self._acquisition_health_lock = (
            threading.Lock()
        )

    # ---------------------------------------------------------------------
    # Initialization / validation
    # ---------------------------------------------------------------------

    def _initialize_new_memory(
        self,
    ) -> None:
        """
        Initialize all inherited v3 sections, then replace the identity header
        with v5 and initialize the dynamic ADC stream record.
        """

        _OBSSharedDataV3._initialize_new_memory(
            self
        )

        # Replace the v3 identity with v5.
        _v3.HEADER_STRUCT.pack_into(
            self._buf,
            _v3.HEADER_OFFSET,
            MAGIC,
            VERSION,
            _v3.ADC_CAPACITY,
            int(
                round(
                    RAW_ADC_SAMPLE_RATE_HZ
                )
            ),
            _v3.ADC_SLOT_SIZE,
            _v3.SHARED_MEMORY_SIZE,
        )

        (
            raw_rate,
            decimation,
            mode,
            effective_rate,
            period_ns,
        ) = _stream_parameters(
            raw_sample_rate_hz=(
                DEFAULT_ADC_ACQUISITION_SAMPLE_RATE_HZ
            ),
            decimation_samples=(
                DEFAULT_DECIMATION_SAMPLES
            ),
            decimation_mode="raw",
        )

        ADC_STREAM_INFO_STRUCT.pack_into(
            self._buf,
            ADC_STREAM_INFO_OFFSET,
            0,  # stable initial sequence
            time.time_ns(),
            raw_rate,
            decimation,
            effective_rate,
            period_ns,
            0,  # no acquisition session started yet
            ADC_DECIMATION_MODE_TO_CODE[
                mode
            ],
        )

        ADC_RATE_DIAGNOSTIC_STRUCT.pack_into(
            self._buf,
            ADC_RATE_DIAGNOSTIC_OFFSET,
            0,  # stable initial sequence
            time.time_ns(),
            float(raw_rate),
            0.0,
            0.0,
            0.0,
            ADC_RATE_STATUS_WARMING_UP,
            0,
            0,
        )

        empty_nmea = b"\x00" * NMEA_TEXT_BYTES

        NMEA_SENTENCE_STRUCT.pack_into(
            self._buf,
            GNSS_NMEA_OFFSET,
            0,
            0,
            empty_nmea,
        )

        NMEA_SENTENCE_STRUCT.pack_into(
            self._buf,
            USBL_NMEA_OFFSET,
            0,
            0,
            empty_nmea,
        )

        CONTROLLER_TELEMETRY_STRUCT.pack_into(
            self._buf, CONTROLLER_TELEMETRY_OFFSET,
            0, 0, *([0] * 11),
        )

        USB_LOGGER_STATUS_STRUCT.pack_into(
            self._buf, USB_LOGGER_STATUS_OFFSET,
            0, 0, _TRI_UNKNOWN, _TRI_UNKNOWN, _TRI_UNKNOWN,
            b"\x00" * USB_STATUS_FILENAME_BYTES,
            b"\x00" * USB_STATUS_FILENAME_BYTES,
            b"\x00" * USB_STATUS_MESSAGE_BYTES,
            b"\x00" * USB_STATUS_RAW_BYTES,
        )

        COMMAND_BROKER_STATUS_STRUCT.pack_into(
            self._buf, COMMAND_BROKER_STATUS_OFFSET,
            0, 0, 0, 0,
            b"\x00" * BROKER_SOURCE_BYTES,
            b"\x00" * BROKER_SENTENCE_BYTES,
            b"\x00" * BROKER_RESULT_BYTES,
        )

        ACQUISITION_HEALTH_STRUCT.pack_into(
            self._buf, ACQUISITION_HEALTH_OFFSET,
            0, 0, 0, 0, 0, 0,
            *([0] * 8),
        )

    def _validate_existing_memory(
        self,
    ) -> None:
        if self.size < (
            _v3.HEADER_STRUCT.size
        ):
            raise RuntimeError(
                f"Shared RAM '{self.name}' "
                "is too small."
            )

        (
            magic,
            version,
            capacity,
            raw_sample_rate,
            slot_size,
            required_size,
        ) = _v3.HEADER_STRUCT.unpack_from(
            self._buf,
            _v3.HEADER_OFFSET,
        )

        valid_raw_rate = (
            int(
                round(
                    RAW_ADC_SAMPLE_RATE_HZ
                )
            )
        )

        if (
            magic != MAGIC
            or version != VERSION
            or capacity
            != _v3.ADC_CAPACITY
            or raw_sample_rate
            != valid_raw_rate
            or slot_size
            != _v3.ADC_SLOT_SIZE
            or required_size
            != _v3.SHARED_MEMORY_SIZE
            or self.size
            < _v3.SHARED_MEMORY_SIZE
        ):
            raise RuntimeError(
                "Shared-memory layout mismatch. "
                "Close old OBS processes and ensure "
                "all modules use shared_data_v5+ compatible RAM."
            )

        # Validate the new stream record too.
        info = self._read_adc_stream_info_unlocked()

        if (
            info.raw_sample_rate_hz
            <= 0.0
            or info.decimation_samples
            <= 0
            or info.effective_sample_rate_hz
            <= 0.0
            or info.sample_period_ns
            <= 0
        ):
            raise RuntimeError(
                "Invalid v5 ADC stream metadata."
            )

        # When attaching to RAM originally created by v5/v6/v7, the v8+
        # extension area is still all-zero. Initialize only those new records in-place
        # without changing the shared-memory identity or disturbing older readers.
        usb_seq, usb_ts = struct.unpack_from(
            "<Qq", self._buf, USB_LOGGER_STATUS_OFFSET
        )
        if usb_seq == 0 and usb_ts == 0:
            USB_LOGGER_STATUS_STRUCT.pack_into(
                self._buf, USB_LOGGER_STATUS_OFFSET,
                0, 0, _TRI_UNKNOWN, _TRI_UNKNOWN, _TRI_UNKNOWN,
                b"\x00" * USB_STATUS_FILENAME_BYTES,
                b"\x00" * USB_STATUS_FILENAME_BYTES,
                b"\x00" * USB_STATUS_MESSAGE_BYTES,
                b"\x00" * USB_STATUS_RAW_BYTES,
            )

        broker_seq, broker_ts = struct.unpack_from(
            "<Qq", self._buf, COMMAND_BROKER_STATUS_OFFSET
        )
        if broker_seq == 0 and broker_ts == 0:
            COMMAND_BROKER_STATUS_STRUCT.pack_into(
                self._buf, COMMAND_BROKER_STATUS_OFFSET,
                0, 0, 0, 0,
                b"\x00" * BROKER_SOURCE_BYTES,
                b"\x00" * BROKER_SENTENCE_BYTES,
                b"\x00" * BROKER_RESULT_BYTES,
            )

    # ---------------------------------------------------------------------
    # Latest raw NMEA diagnostics
    # ---------------------------------------------------------------------

    @staticmethod
    def _encode_nmea_text(
        text: str,
    ) -> bytes:
        encoded = str(
            text
        ).strip().encode(
            "ascii",
            errors="replace",
        )[:NMEA_TEXT_BYTES - 1]

        return encoded + (
            b"\x00"
            * (
                NMEA_TEXT_BYTES
                - len(encoded)
            )
        )

    def _write_nmea_sentence(
        self,
        offset: int,
        lock: threading.Lock,
        text: str,
        *,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        encoded = self._encode_nmea_text(
            text
        )

        with lock:
            write_seq = self._next_odd_sequence(
                offset
            )

            NMEA_SENTENCE_STRUCT.pack_into(
                self._buf,
                offset,
                write_seq,
                int(
                    timestamp_ns
                ),
                encoded,
            )

            struct.pack_into(
                "<Q",
                self._buf,
                offset,
                write_seq + 1,
            )

    def _read_nmea_sentence(
        self,
        offset: int,
    ) -> NMEASentenceSnapshot:
        values = self._stable_unpack(
            NMEA_SENTENCE_STRUCT,
            offset,
        )

        raw = values[2].split(
            b"\x00",
            1,
        )[0]

        return NMEASentenceSnapshot(
            timestamp_ns=int(
                values[1]
            ),
            text=raw.decode(
                "ascii",
                errors="replace",
            ),
        )

    def update_gnss_nmea_sentence(
        self,
        text: str,
        *,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        self._write_nmea_sentence(
            GNSS_NMEA_OFFSET,
            self._gnss_nmea_lock,
            text,
            timestamp_ns=timestamp_ns,
        )

    def update_usbl_nmea_sentence(
        self,
        text: str,
        *,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        self._write_nmea_sentence(
            USBL_NMEA_OFFSET,
            self._usbl_nmea_lock,
            text,
            timestamp_ns=timestamp_ns,
        )

    def read_gnss_nmea_sentence(
        self,
    ) -> NMEASentenceSnapshot:
        return self._read_nmea_sentence(
            GNSS_NMEA_OFFSET
        )

    def read_usbl_nmea_sentence(
        self,
    ) -> NMEASentenceSnapshot:
        return self._read_nmea_sentence(
            USBL_NMEA_OFFSET
        )

    # ---------------------------------------------------------------------
    # Centralized controller telemetry ($XCHM1)
    # ---------------------------------------------------------------------

    def update_controller_telemetry(
        self, *, vmoni0_raw: int, vmoni1_raw: int, vmoni2_raw: int,
        vmoni3_raw: int, temp_raw: int, pmon_raw: int, hmon_raw: int,
        lmon: int, oasm: int, device_id: int, count: int,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()
        values = (vmoni0_raw, vmoni1_raw, vmoni2_raw, vmoni3_raw,
                  temp_raw, pmon_raw, hmon_raw, lmon, oasm, device_id, count)
        with self._controller_telemetry_lock:
            write_seq = self._next_odd_sequence(CONTROLLER_TELEMETRY_OFFSET)
            CONTROLLER_TELEMETRY_STRUCT.pack_into(
                self._buf, CONTROLLER_TELEMETRY_OFFSET,
                write_seq, int(timestamp_ns), *(int(v) for v in values),
            )
            struct.pack_into("<Q", self._buf, CONTROLLER_TELEMETRY_OFFSET, write_seq + 1)

    def read_controller_telemetry(self) -> ControllerTelemetrySnapshot:
        v = self._stable_unpack(CONTROLLER_TELEMETRY_STRUCT, CONTROLLER_TELEMETRY_OFFSET)
        return ControllerTelemetrySnapshot(
            timestamp_ns=int(v[1]),
            vmoni0_raw=int(v[2]), vmoni1_raw=int(v[3]),
            vmoni2_raw=int(v[4]), vmoni3_raw=int(v[5]),
            temp_raw=int(v[6]), pmon_raw=int(v[7]), hmon_raw=int(v[8]),
            lmon=int(v[9]) & 0xFF, oasm=int(v[10]) & 0xFF,
            device_id=int(v[11]) & 0xFF, count=int(v[12]) & 0xFF,
        )

    # ---------------------------------------------------------------------
    # Centralized USB logger / STRG0 status
    # ---------------------------------------------------------------------

    @staticmethod
    def _encode_fixed_text(value: str, size: int) -> bytes:
        data = str(value or "").encode("utf-8", errors="replace")[: max(0, size - 1)]
        return data + (b"\x00" * (size - len(data)))

    @staticmethod
    def _decode_fixed_text(value: bytes) -> str:
        return bytes(value).split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    @staticmethod
    def _tri_to_optional(value: int) -> Optional[bool]:
        if int(value) == _TRI_TRUE:
            return True
        if int(value) == _TRI_FALSE:
            return False
        return None

    @staticmethod
    def _optional_to_tri(value: Optional[bool]) -> int:
        if value is True:
            return _TRI_TRUE
        if value is False:
            return _TRI_FALSE
        return _TRI_UNKNOWN

    def read_usb_logger_status(self) -> USBLoggerStatusSnapshot:
        values = self._stable_unpack(
            USB_LOGGER_STATUS_STRUCT,
            USB_LOGGER_STATUS_OFFSET,
        )
        return USBLoggerStatusSnapshot(
            timestamp_ns=int(values[1]),
            device_present=self._tri_to_optional(values[2]),
            media_present=self._tri_to_optional(values[3]),
            logger_active=self._tri_to_optional(values[4]),
            current_filename=self._decode_fixed_text(values[5]),
            last_closed_filename=self._decode_fixed_text(values[6]),
            last_message=self._decode_fixed_text(values[7]),
            last_raw_sentence=self._decode_fixed_text(values[8]),
        )

    def update_usb_logger_status(
        self,
        *,
        device_present=_UNSET,
        media_present=_UNSET,
        logger_active=_UNSET,
        current_filename=_UNSET,
        last_closed_filename=_UNSET,
        last_message=_UNSET,
        last_raw_sentence=_UNSET,
        timestamp_ns: Optional[int] = None,
    ) -> USBLoggerStatusSnapshot:
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._usb_logger_status_lock:
            current = self.read_usb_logger_status()
            device = current.device_present if device_present is _UNSET else device_present
            media = current.media_present if media_present is _UNSET else media_present
            active = current.logger_active if logger_active is _UNSET else logger_active
            current_file = current.current_filename if current_filename is _UNSET else str(current_filename or "")
            closed_file = current.last_closed_filename if last_closed_filename is _UNSET else str(last_closed_filename or "")
            message = current.last_message if last_message is _UNSET else str(last_message or "")
            raw_sentence = current.last_raw_sentence if last_raw_sentence is _UNSET else str(last_raw_sentence or "")

            write_seq = self._next_odd_sequence(USB_LOGGER_STATUS_OFFSET)
            USB_LOGGER_STATUS_STRUCT.pack_into(
                self._buf,
                USB_LOGGER_STATUS_OFFSET,
                write_seq,
                int(timestamp_ns),
                self._optional_to_tri(device),
                self._optional_to_tri(media),
                self._optional_to_tri(active),
                self._encode_fixed_text(current_file, USB_STATUS_FILENAME_BYTES),
                self._encode_fixed_text(closed_file, USB_STATUS_FILENAME_BYTES),
                self._encode_fixed_text(message, USB_STATUS_MESSAGE_BYTES),
                self._encode_fixed_text(raw_sentence, USB_STATUS_RAW_BYTES),
            )
            struct.pack_into("<Q", self._buf, USB_LOGGER_STATUS_OFFSET, write_seq + 1)

        return self.read_usb_logger_status()

    # ---------------------------------------------------------------------
    # Local command broker status
    # ---------------------------------------------------------------------

    def read_command_broker_status(self) -> CommandBrokerStatusSnapshot:
        values = self._stable_unpack(
            COMMAND_BROKER_STATUS_STRUCT,
            COMMAND_BROKER_STATUS_OFFSET,
        )
        return CommandBrokerStatusSnapshot(
            timestamp_ns=int(values[1]),
            broker_running=bool(values[2]),
            obs_command_connected=bool(values[3]),
            last_source=self._decode_fixed_text(values[4]),
            last_sentence=self._decode_fixed_text(values[5]),
            last_result=self._decode_fixed_text(values[6]),
        )

    def update_command_broker_status(
        self,
        *,
        broker_running=_UNSET,
        obs_command_connected=_UNSET,
        last_source=_UNSET,
        last_sentence=_UNSET,
        last_result=_UNSET,
        timestamp_ns: Optional[int] = None,
    ) -> CommandBrokerStatusSnapshot:
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._command_broker_status_lock:
            current = self.read_command_broker_status()
            running = current.broker_running if broker_running is _UNSET else bool(broker_running)
            connected = current.obs_command_connected if obs_command_connected is _UNSET else bool(obs_command_connected)
            source = current.last_source if last_source is _UNSET else str(last_source or "")
            sentence = current.last_sentence if last_sentence is _UNSET else str(last_sentence or "")
            result = current.last_result if last_result is _UNSET else str(last_result or "")

            write_seq = self._next_odd_sequence(COMMAND_BROKER_STATUS_OFFSET)
            COMMAND_BROKER_STATUS_STRUCT.pack_into(
                self._buf,
                COMMAND_BROKER_STATUS_OFFSET,
                write_seq,
                int(timestamp_ns),
                int(running),
                int(connected),
                self._encode_fixed_text(source, BROKER_SOURCE_BYTES),
                self._encode_fixed_text(sentence, BROKER_SENTENCE_BYTES),
                self._encode_fixed_text(result, BROKER_RESULT_BYTES),
            )
            struct.pack_into("<Q", self._buf, COMMAND_BROKER_STATUS_OFFSET, write_seq + 1)

        return self.read_command_broker_status()

    # ---------------------------------------------------------------------
    # Acquisition manager heartbeat / source freshness
    # ---------------------------------------------------------------------

    def read_acquisition_health(self) -> AcquisitionHealthSnapshot:
        values = self._stable_unpack(
            ACQUISITION_HEALTH_STRUCT,
            ACQUISITION_HEALTH_OFFSET,
        )
        return AcquisitionHealthSnapshot(
            timestamp_ns=int(values[1]),
            process_id=int(values[2]),
            command_connected=bool(values[3]),
            data_connected=bool(values[4]),
            broker_running=bool(values[5]),
            last_command_rx_ns=int(values[6]),
            last_data_rx_ns=int(values[7]),
            last_ahrs_ns=int(values[8]),
            last_depth_ns=int(values[9]),
            last_controller_ns=int(values[10]),
            last_gnss_ns=int(values[11]),
            last_usbl_ns=int(values[12]),
            last_adc_ns=int(values[13]),
        )

    def update_acquisition_health(
        self,
        *,
        process_id=_UNSET,
        command_connected=_UNSET,
        data_connected=_UNSET,
        broker_running=_UNSET,
        last_command_rx_ns=_UNSET,
        last_data_rx_ns=_UNSET,
        last_ahrs_ns=_UNSET,
        last_depth_ns=_UNSET,
        last_controller_ns=_UNSET,
        last_gnss_ns=_UNSET,
        last_usbl_ns=_UNSET,
        last_adc_ns=_UNSET,
        timestamp_ns: Optional[int] = None,
    ) -> AcquisitionHealthSnapshot:
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._acquisition_health_lock:
            current = self.read_acquisition_health()

            def pick(value, old, cast=int):
                return old if value is _UNSET else cast(value)

            write_seq = self._next_odd_sequence(ACQUISITION_HEALTH_OFFSET)
            ACQUISITION_HEALTH_STRUCT.pack_into(
                self._buf,
                ACQUISITION_HEALTH_OFFSET,
                write_seq,
                int(timestamp_ns),
                pick(process_id, current.process_id),
                int(pick(command_connected, current.command_connected, bool)),
                int(pick(data_connected, current.data_connected, bool)),
                int(pick(broker_running, current.broker_running, bool)),
                pick(last_command_rx_ns, current.last_command_rx_ns),
                pick(last_data_rx_ns, current.last_data_rx_ns),
                pick(last_ahrs_ns, current.last_ahrs_ns),
                pick(last_depth_ns, current.last_depth_ns),
                pick(last_controller_ns, current.last_controller_ns),
                pick(last_gnss_ns, current.last_gnss_ns),
                pick(last_usbl_ns, current.last_usbl_ns),
                pick(last_adc_ns, current.last_adc_ns),
            )
            struct.pack_into(
                "<Q", self._buf, ACQUISITION_HEALTH_OFFSET, write_seq + 1
            )

        return self.read_acquisition_health()

    # ---------------------------------------------------------------------
    # ADC sample-rate diagnostic
    # ---------------------------------------------------------------------

    def read_adc_rate_diagnostic(
        self,
    ) -> ADCRateDiagnosticSnapshot:
        values = self._stable_unpack(
            ADC_RATE_DIAGNOSTIC_STRUCT,
            ADC_RATE_DIAGNOSTIC_OFFSET,
        )
        status_code = int(values[6])
        return ADCRateDiagnosticSnapshot(
            timestamp_ns=int(values[1]),
            expected_raw_sample_rate_hz=float(values[2]),
            measured_raw_sample_rate_hz=float(values[3]),
            measured_output_sample_rate_hz=float(values[4]),
            error_ppm=float(values[5]),
            status=ADC_RATE_STATUS_TO_TEXT.get(
                status_code,
                f"UNKNOWN:{status_code}",
            ),
            measurement_window_s=float(values[7]) / 1000.0,
            source_sample_span=int(values[8]),
        )

    def update_adc_rate_diagnostic(
        self,
        *,
        measured_raw_sample_rate_hz: float,
        expected_raw_sample_rate_hz: Optional[float] = None,
        measured_output_sample_rate_hz: Optional[float] = None,
        measurement_window_s: float = 0.0,
        source_sample_span: int = 0,
        timestamp_ns: Optional[int] = None,
    ) -> ADCRateDiagnosticSnapshot:
        if expected_raw_sample_rate_hz is None:
            expected_raw_sample_rate_hz = (
                self.read_adc_stream_info().raw_sample_rate_hz
            )

        expected = float(expected_raw_sample_rate_hz)
        measured = float(measured_raw_sample_rate_hz)
        if not (expected > 0.0 and measured > 0.0):
            raise ValueError(
                "ADC diagnostic expected/measured rates must be > 0"
            )

        stream_info = self.read_adc_stream_info()
        if measured_output_sample_rate_hz is None:
            measured_output_sample_rate_hz = (
                measured / max(1, int(stream_info.decimation_samples))
            )
        measured_output = float(measured_output_sample_rate_hz)

        error_ppm = ((measured - expected) / expected) * 1_000_000.0
        absolute_error_ppm = abs(error_ppm)
        if absolute_error_ppm <= ADC_RATE_PASS_PPM:
            status_code = ADC_RATE_STATUS_PASS
        elif absolute_error_ppm <= ADC_RATE_WARN_PPM:
            status_code = ADC_RATE_STATUS_WARN
        else:
            status_code = ADC_RATE_STATUS_FAULT

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        window_ms = max(
            0,
            min(0xFFFFFFFF, int(round(float(measurement_window_s) * 1000.0))),
        )

        with self._adc_rate_diagnostic_lock:
            write_seq = self._next_odd_sequence(
                ADC_RATE_DIAGNOSTIC_OFFSET
            )
            ADC_RATE_DIAGNOSTIC_STRUCT.pack_into(
                self._buf,
                ADC_RATE_DIAGNOSTIC_OFFSET,
                write_seq,
                int(timestamp_ns),
                expected,
                measured,
                measured_output,
                float(error_ppm),
                int(status_code),
                int(window_ms),
                max(0, int(source_sample_span)),
            )
            struct.pack_into(
                "<Q",
                self._buf,
                ADC_RATE_DIAGNOSTIC_OFFSET,
                write_seq + 1,
            )

        return self.read_adc_rate_diagnostic()

    def reset_adc_rate_diagnostic(
        self,
        *,
        expected_raw_sample_rate_hz: Optional[float] = None,
        timestamp_ns: Optional[int] = None,
    ) -> ADCRateDiagnosticSnapshot:
        if expected_raw_sample_rate_hz is None:
            expected_raw_sample_rate_hz = (
                self.read_adc_stream_info().raw_sample_rate_hz
            )
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._adc_rate_diagnostic_lock:
            write_seq = self._next_odd_sequence(
                ADC_RATE_DIAGNOSTIC_OFFSET
            )
            ADC_RATE_DIAGNOSTIC_STRUCT.pack_into(
                self._buf,
                ADC_RATE_DIAGNOSTIC_OFFSET,
                write_seq,
                int(timestamp_ns),
                float(expected_raw_sample_rate_hz),
                0.0,
                0.0,
                0.0,
                ADC_RATE_STATUS_WARMING_UP,
                0,
                0,
            )
            struct.pack_into(
                "<Q",
                self._buf,
                ADC_RATE_DIAGNOSTIC_OFFSET,
                write_seq + 1,
            )
        return self.read_adc_rate_diagnostic()

    # ---------------------------------------------------------------------
    # ADC stream metadata
    # ---------------------------------------------------------------------

    def _read_adc_stream_info_unlocked(
        self,
    ) -> ADCStreamInfoSnapshot:
        values = self._stable_unpack(
            ADC_STREAM_INFO_STRUCT,
            ADC_STREAM_INFO_OFFSET,
        )

        mode_code = int(
            values[
                7
            ]
        )

        mode = (
            ADC_DECIMATION_CODE_TO_MODE.get(
                mode_code,
                f"unknown:{mode_code}",
            )
        )

        return ADCStreamInfoSnapshot(
            timestamp_ns=int(
                values[
                    1
                ]
            ),
            raw_sample_rate_hz=float(
                values[
                    2
                ]
            ),
            decimation_samples=int(
                values[
                    3
                ]
            ),
            effective_sample_rate_hz=float(
                values[
                    4
                ]
            ),
            sample_period_ns=int(
                values[
                    5
                ]
            ),
            adc_session_id=int(
                values[
                    6
                ]
            ),
            decimation_mode=mode,
        )

    def read_adc_stream_info(
        self,
    ) -> ADCStreamInfoSnapshot:
        """
        Read the current shared ADC stream configuration.

        This is the authoritative sample-rate source for FFT, Spectrogram, PSD,
        event-window conversion, time-axis display and MiniSEED writing.
        """

        return (
            self._read_adc_stream_info_unlocked()
        )

    def _write_adc_stream_info(
        self,
        *,
        raw_sample_rate_hz: float,
        decimation_samples: int,
        decimation_mode: str,
        adc_session_id: int,
        timestamp_ns: Optional[
            int
        ] = None,
    ) -> ADCStreamInfoSnapshot:

        (
            raw_rate,
            decimation,
            mode,
            effective_rate,
            period_ns,
        ) = _stream_parameters(
            raw_sample_rate_hz=(
                raw_sample_rate_hz
            ),
            decimation_samples=(
                decimation_samples
            ),
            decimation_mode=(
                decimation_mode
            ),
        )

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._adc_stream_info_lock:
            write_seq = (
                self._next_odd_sequence(
                    ADC_STREAM_INFO_OFFSET
                )
            )

            ADC_STREAM_INFO_STRUCT.pack_into(
                self._buf,
                ADC_STREAM_INFO_OFFSET,
                write_seq,
                int(
                    timestamp_ns
                ),
                float(
                    raw_rate
                ),
                int(
                    decimation
                ),
                float(
                    effective_rate
                ),
                int(
                    period_ns
                ),
                int(
                    adc_session_id
                )
                & 0xFFFFFFFFFFFFFFFF,
                ADC_DECIMATION_MODE_TO_CODE[
                    mode
                ],
            )

            struct.pack_into(
                "<Q",
                self._buf,
                ADC_STREAM_INFO_OFFSET,
                write_seq + 1,
            )

        return (
            self.read_adc_stream_info()
        )

    def configure_adc_stream(
        self,
        *,
        raw_sample_rate_hz: float = (
            DEFAULT_ADC_ACQUISITION_SAMPLE_RATE_HZ
        ),
        decimation_samples: int = 1,
        decimation_mode: str = "raw",
        start_new_session: bool = True,
        reset_bulk_status: bool = False,
    ) -> ADCStreamInfoSnapshot:
        """
        Configure the ADC stream.

        Safe default:
            start_new_session=True

        Changing sample rate while old samples remain in the ring would make
        one ring contain two time bases, so the normal operation is to start a
        new ADC session whenever the decimation configuration changes.
        """

        if start_new_session:
            return (
                self.start_new_adc_session(
                    reset_bulk_status=(
                        reset_bulk_status
                    ),
                    raw_sample_rate_hz=(
                        raw_sample_rate_hz
                    ),
                    decimation_samples=(
                        decimation_samples
                    ),
                    decimation_mode=(
                        decimation_mode
                    ),
                )
            )

        if self.adc_total_samples() > 0:
            raise RuntimeError(
                "Cannot change ADC stream rate "
                "while the ADC ring contains data. "
                "Use start_new_session=True."
            )

        current = (
            self.read_adc_stream_info()
        )

        info = self._write_adc_stream_info(
            raw_sample_rate_hz=(
                raw_sample_rate_hz
            ),
            decimation_samples=(
                decimation_samples
            ),
            decimation_mode=(
                decimation_mode
            ),
            adc_session_id=(
                current.adc_session_id
            ),
        )
        self.reset_adc_rate_diagnostic(
            expected_raw_sample_rate_hz=info.raw_sample_rate_hz
        )
        return info

    def start_new_adc_session(
        self,
        *,
        reset_bulk_status: bool = True,
        raw_sample_rate_hz: Optional[
            float
        ] = None,
        decimation_samples: Optional[
            int
        ] = None,
        decimation_mode: Optional[
            str
        ] = None,
    ) -> ADCStreamInfoSnapshot:
        """
        Start a clean ADC acquisition session.

        If rate/decimation parameters are supplied they become authoritative
        for the new session. Otherwise the existing stream configuration is
        retained.

        adc_session_id increments once per call.
        """

        with self._protocol_clock_lock:
            current = (
                self.read_adc_stream_info()
            )

            raw_rate = (
                current.raw_sample_rate_hz
                if raw_sample_rate_hz
                is None
                else float(
                    raw_sample_rate_hz
                )
            )

            decimation = (
                current.decimation_samples
                if decimation_samples
                is None
                else int(
                    decimation_samples
                )
            )

            if decimation_mode is None:
                if (
                    decimation == 1
                ):
                    mode = "raw"
                else:
                    # Preserve known mode when sensible; otherwise mean is the
                    # v5 processing baseline for N > 1.
                    mode = (
                        current.decimation_mode
                        if current.decimation_mode
                        in ("mean",)
                        else "mean"
                    )
            else:
                mode = (
                    str(
                        decimation_mode
                    )
                )

            # Reset the ADC ring first. No new output sample can then be read
            # with metadata from the previous stream configuration.
            super().reset_adc()

            if reset_bulk_status:
                self.reset_bulk_status()

            next_session_id = (
                int(
                    current.adc_session_id
                )
                + 1
            ) & 0xFFFFFFFFFFFFFFFF

            info = self._write_adc_stream_info(
                raw_sample_rate_hz=(
                    raw_rate
                ),
                decimation_samples=(
                    decimation
                ),
                decimation_mode=(
                    mode
                ),
                adc_session_id=(
                    next_session_id
                ),
            )
            self.reset_adc_rate_diagnostic(
                expected_raw_sample_rate_hz=info.raw_sample_rate_hz
            )
            return info

    # ---------------------------------------------------------------------
    # Stream-rate convenience accessors
    # ---------------------------------------------------------------------

    def adc_raw_sample_rate_hz(
        self,
    ) -> float:
        return float(
            self.read_adc_stream_info()
            .raw_sample_rate_hz
        )

    def adc_effective_sample_rate_hz(
        self,
    ) -> float:
        return float(
            self.read_adc_stream_info()
            .effective_sample_rate_hz
        )

    def adc_decimation_samples(
        self,
    ) -> int:
        return int(
            self.read_adc_stream_info()
            .decimation_samples
        )

    def adc_sample_period_ns(
        self,
    ) -> int:
        return int(
            self.read_adc_stream_info()
            .sample_period_ns
        )

    # ---------------------------------------------------------------------
    # ADC clock / writer
    # ---------------------------------------------------------------------

    def latest_adc_timestamp_ns(
        self,
    ) -> Optional[int]:
        """
        Return the timestamp of the latest committed output ADC frame.
        """

        total = (
            self.adc_total_samples()
        )

        if total <= 0:
            return None

        absolute_index = (
            total - 1
        )

        slot_index = (
            absolute_index
            % _v3.ADC_CAPACITY
        )

        offset = (
            _v3.ADC_RING_OFFSET
            + slot_index
            * _v3.ADC_SLOT_SIZE
        )

        expected_seq = (
            absolute_index + 1
        ) * 2

        for _ in range(
            16
        ):
            seq_before = (
                struct.unpack_from(
                    "<Q",
                    self._buf,
                    offset,
                )[0]
            )

            if (
                seq_before
                != expected_seq
                or (
                    seq_before
                    & 1
                )
            ):
                continue

            values = (
                _v3.ADC_SLOT_STRUCT
                .unpack_from(
                    self._buf,
                    offset,
                )
            )

            seq_after = (
                struct.unpack_from(
                    "<Q",
                    self._buf,
                    offset,
                )[0]
            )

            if (
                seq_before
                == expected_seq
                and seq_after
                == expected_seq
                and values[
                    0
                ]
                == expected_seq
                and not (
                    seq_after
                    & 1
                )
            ):
                return int(
                    values[
                        1
                    ]
                )

        return None

    def write_adc_stream_block(
        self,
        samples: Iterable[
            Sequence[int]
        ],
        *,
        statuses: Optional[
            Iterable[
                Sequence[int]
            ]
        ] = None,
        receive_timestamp_ns: Optional[
            int
        ] = None,
        missing_output_samples_before: int = 0,
        timestamps_ns: Optional[
            Iterable[int]
        ] = None,
    ) -> int:
        """
        Write samples that are ALREADY at the current effective/shared rate.

        Parameters
        ----------
        samples
            Output ADC frames, each:
                (CH0, CH1, CH2, CH3)

        statuses
            One status tuple per output frame.

        receive_timestamp_ns
            Host time when the processed block became available. Used only when
            explicit timestamps_ns are not supplied.

        missing_output_samples_before
            Number of genuinely absent OUTPUT samples immediately before this
            block. Decimation itself must NOT be represented here.

            Example:
                effective rate = 200 Hz
                one output sample missing
                -> next sample is 10 ms after previous sample rather than 5 ms.

        timestamps_ns
            Optional explicit timestamps for every output frame. Use this when
            the acquisition/decimator has a more precise source-time mapping,
            e.g. center timestamps of averaging windows.

        Returns
        -------
        Number of output ADC frames committed.
        """

        sample_list = list(
            samples
        )

        if not sample_list:
            return 0

        status_list = (
            list(
                statuses
            )
            if statuses is not None
            else [
                (
                    0,
                    1,
                    2,
                    3,
                )
                for _ in sample_list
            ]
        )

        if (
            len(
                status_list
            )
            != len(
                sample_list
            )
        ):
            raise ValueError(
                "statuses length must "
                "match samples length"
            )

        # Explicit timestamps are authoritative.
        if timestamps_ns is not None:
            timestamp_list = list(
                timestamps_ns
            )

            if (
                len(
                    timestamp_list
                )
                != len(
                    sample_list
                )
            ):
                raise ValueError(
                    "timestamps_ns length "
                    "must match samples length"
                )

            with self._protocol_clock_lock:
                return (
                    super()
                    .write_adc_block(
                        sample_list,
                        statuses=(
                            status_list
                        ),
                        timestamps_ns=(
                            timestamp_list
                        ),
                    )
                )

        if receive_timestamp_ns is None:
            receive_timestamp_ns = (
                time.time_ns()
            )

        info = (
            self.read_adc_stream_info()
        )

        interval_ns = int(
            info.sample_period_ns
        )

        missing_output_samples_before = max(
            0,
            int(
                missing_output_samples_before
            ),
        )

        with self._protocol_clock_lock:
            latest_timestamp_ns = (
                self.latest_adc_timestamp_ns()
            )

            if latest_timestamp_ns is None:
                # First processed block: final output sample is anchored to the
                # host receive time. Callers with precise center-of-window
                # timing should pass timestamps_ns explicitly.
                first_timestamp_ns = (
                    int(
                        receive_timestamp_ns
                    )
                    - (
                        len(
                            sample_list
                        )
                        - 1
                    )
                    * interval_ns
                )

            else:
                first_timestamp_ns = (
                    int(
                        latest_timestamp_ns
                    )
                    + (
                        missing_output_samples_before
                        + 1
                    )
                    * interval_ns
                )

            timestamp_list = [
                (
                    first_timestamp_ns
                    + index
                    * interval_ns
                )
                for index in range(
                    len(
                        sample_list
                    )
                )
            ]

            return (
                super()
                .write_adc_block(
                    sample_list,
                    statuses=(
                        status_list
                    ),
                    timestamps_ns=(
                        timestamp_list
                    ),
                )
            )

    def write_adc_protocol_block(
        self,
        samples: Iterable[
            Sequence[int]
        ],
        *,
        statuses: Optional[
            Iterable[
                Sequence[int]
            ]
        ] = None,
        receive_timestamp_ns: Optional[
            int
        ] = None,
        missing_samples_before: int = 0,
    ) -> int:
        """
        Compatibility wrapper for v4-style callers.

        v5 semantic change:
            'samples' are assumed to already be at the CURRENT EFFECTIVE
            shared-stream rate, and missing_samples_before is interpreted as
            missing OUTPUT samples.

        New code should call write_adc_stream_block() directly.
        """

        return (
            self.write_adc_stream_block(
                samples,
                statuses=statuses,
                receive_timestamp_ns=(
                    receive_timestamp_ns
                ),
                missing_output_samples_before=(
                    missing_samples_before
                ),
            )
        )

    # ---------------------------------------------------------------------
    # ADC readers with dynamic/effective rate
    # ---------------------------------------------------------------------

    def read_adc_latest(
        self,
        count: int = 1000,
    ):
        snapshot = (
            super()
            .read_adc_latest(
                count
            )
        )

        effective_rate = (
            self.adc_effective_sample_rate_hz()
        )

        return replace(
            snapshot,
            sample_rate_hz=(
                effective_rate
            ),
        )

    def read_adc_latest_numpy(
        self,
        count: int = 3000,
    ):
        snapshot = (
            super()
            .read_adc_latest_numpy(
                count
            )
        )

        effective_rate = (
            self.adc_effective_sample_rate_hz()
        )

        return replace(
            snapshot,
            sample_rate_hz=(
                effective_rate
            ),
        )


# =============================================================================
# Demo / self-check helper
# =============================================================================

def demo() -> None:
    shared = OBSSharedData()

    try:
        info = (
            shared.read_adc_stream_info()
        )

        print(
            "Shared-data API:",
            SHARED_DATA_API_VERSION,
        )
        print(
            "RAM name:",
            shared.name,
        )
        print(
            "ADC stream:",
            info,
        )
        print(
            "Effective rate:",
            (
                f"{info.effective_sample_rate_hz:.3f} Hz"
            ),
        )

    finally:
        shared.close()


if __name__ == "__main__":
    demo()
