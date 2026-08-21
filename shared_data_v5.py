"""
shared_data_v5.py
=================

GRC-UGM-PERTAMINA OBS shared-memory API.

Version: 5

Purpose
-------
Version 5 separates the physical/raw ADC rate from the rate of the ADC stream
published to shared RAM after decimation/averaging.

Example:

    raw ADC rate       = 1000 Hz/channel
    decimation_samples = 5 raw samples/output sample
    decimation_mode    = mean
    effective rate     = 1000 / 5 = 200 Hz/channel
    output period      = 5 ms

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
    Physical/source ADC frame rate from the OBS bulk protocol.

ADC_SAMPLE_RATE_HZ
    Compatibility alias for RAW_ADC_SAMPLE_RATE_HZ. New visualization and
    recording modules should not use it as the shared-stream rate.

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

Therefore all processes that exchange ADC data must migrate to shared_data_v5
together. v3/v4 processes cannot safely share the v5 ADC stream.
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

SHARED_DATA_API_VERSION = 5
VERSION = 5

DEFAULT_SHARED_MEMORY_NAME = "GRC_UGM_PERTAMINA_OBS_V5"
MAGIC = b"OBSRAM05"

# The OBS bulk protocol's physical/source ADC frame rate.
RAW_ADC_SAMPLE_RATE_HZ = float(
    _v3.ADC_SAMPLE_RATE_HZ
)
ADC_SOURCE_SAMPLE_RATE_HZ = (
    RAW_ADC_SAMPLE_RATE_HZ
)

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


# =============================================================================
# Public ADC stream snapshot
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

        self._protocol_clock_lock = (
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
                RAW_ADC_SAMPLE_RATE_HZ
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
                "all modules use shared_data_v5."
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
            RAW_ADC_SAMPLE_RATE_HZ
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

        return self._write_adc_stream_info(
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

            return self._write_adc_stream_info(
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
