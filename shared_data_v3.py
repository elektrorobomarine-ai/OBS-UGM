"""
shared_data_v3.py
=================

GRC-UGM-PERTAMINA OBS high-speed cross-process shared RAM.

Version: 3
Protocol baseline: OBS TCP protocol supplied 2026-08-19.

The shared-memory layout is designed around the actual OBS interfaces:

COMMAND TCP : port 54300
    $GDAT2
    $TIME1
    $DEPT0
    $AHRS2
    $XFWVR
    $XCHM0

BULK DATA TCP : port 54301
    "OBS:" + uint32 sequence + uint32 payload_length
    payload = interleaved CH0..CH3 uint32 words
    current payload = 4 channels x 128 ADC frames x 4 bytes = 2048 bytes

ADC word:
    bits 23..0  : signed 24-bit ADC
    bits 31..24 : channel status byte

Storage strategy
----------------
The live transport is RAM only, using multiprocessing.shared_memory.
No ADC stream is written to a disk-backed shared file.

The ADC ring stores:
    timestamp
    CH0, CH1, CH2, CH3 signed ADC values
    status byte CH0..CH3

Nominal ADC rate:
    1000 ADC frames/second/channel

Ring history:
    120 seconds

Other Python processes attach using the same shared-memory name:

    from shared_data_v3 import OBSSharedData

    shared = OBSSharedData()

    adc = shared.read_adc_latest_numpy(3000)
    telemetry = shared.read_telemetry()
    bulk = shared.read_bulk_status()
    gnss = shared.read_gnss()
    usbl = shared.read_usbl()

    shared.close()

Writer ownership
----------------
This design assumes one acquisition/control process writes the shared sections
and other programs primarily read them. This matches the OBS launcher design,
where obs_setting.py owns the physical/network connections.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Any, Iterable, Optional, Sequence


# =============================================================================
# Public constants
# =============================================================================

DEFAULT_SHARED_MEMORY_NAME = "GRC_UGM_PERTAMINA_OBS_V3"

VERSION = 3
MAGIC = b"OBSRAM03"

ADC_SAMPLE_RATE_HZ = 1000
ADC_CAPACITY = 120_000

ADC_STATUS_ERROR = 0x80
ADC_STATUS_FILTER_NOT_SETTLED = 0x40
ADC_STATUS_REPEATED = 0x20
ADC_STATUS_FILTER_TYPE = 0x10
ADC_STATUS_SATURATED = 0x08
ADC_STATUS_CHANNEL_ID_MASK = 0x07


# =============================================================================
# Shared-memory layout
# =============================================================================

HEADER_OFFSET = 0
HEADER_SIZE = 4096

TELEMETRY_OFFSET = 256
GNSS_OFFSET = 512
USBL_OFFSET = 640
DEVICE_TIME_OFFSET = 768
BULK_STATUS_OFFSET = 896
DIAGNOSTIC_OFFSET = 1152
CONTROLLER_HEALTH_OFFSET = 1280
FIRMWARE_INFO_OFFSET = 1408

ADC_RING_OFFSET = HEADER_SIZE


# magic[8], version, capacity, sample_rate, slot_size, total_size
HEADER_STRUCT = struct.Struct("<8sIIIII")

# Telemetry:
# seq, receive_timestamp_ns,
# roll,pitch,yaw,p,q,r,depth,depth_rate,temperature,pressure,
# raw_status, ahrs_device_id,
# gimbal_locked,power_low,data_connected,command_connected,
# gnss_connected,usbl_connected
TELEMETRY_STRUCT = struct.Struct("<Qq10dII6B2x")

# Position:
# seq, timestamp_ns, valid + padding,
# lat, lon, altitude, fix_quality, satellites, hdop
POSITION_STRUCT = struct.Struct("<QqB7xdddiid")

# Device time:
# seq, receive_timestamp_ns,
# ms, year, month, week, date, hours, minutes, seconds
DEVICE_TIME_STRUCT = struct.Struct("<Qq8i")

# Bulk diagnostics:
# seq_record, last_receive_timestamp_ns,
# last_frame_sequence, last_payload_length,
# frames_received, dropped_frames, sequence_resets, malformed_frames,
# channel_id_mismatches, error_flag_words, unsettled_words,
# repeated_words, saturated_words
BULK_STATUS_STRUCT = struct.Struct("<QqII9Q")

# GDAT2:
# seq_record, receive_timestamp_ns, 10 x uint32 hex values, counter
DIAGNOSTIC_STRUCT = struct.Struct("<Qq10IQ")

# XCHM0:
# seq_record, receive_timestamp_ns, unit_id, processor_status_code
CONTROLLER_HEALTH_STRUCT = struct.Struct("<QqII")

# XFWVR:
# seq_record, receive_timestamp_ns, UTF-8 summary bytes
FIRMWARE_TEXT_BYTES = 224
FIRMWARE_INFO_STRUCT = struct.Struct(
    f"<Qq{FIRMWARE_TEXT_BYTES}s"
)

# ADC slot, 40 bytes:
# sequence lock, timestamp,
# ch0,ch1,ch2,ch3,
# status0,status1,status2,status3,
# padding
ADC_SLOT_STRUCT = struct.Struct("<Qqiiii4B4x")
ADC_SLOT_SIZE = ADC_SLOT_STRUCT.size
assert ADC_SLOT_SIZE == 40

# ADC metadata:
# committed absolute ADC-frame count
ADC_META_OFFSET = 1664
ADC_META_STRUCT = struct.Struct("<Q")

SHARED_MEMORY_SIZE = (
    ADC_RING_OFFSET
    + ADC_CAPACITY * ADC_SLOT_SIZE
)


# =============================================================================
# Snapshot classes
# =============================================================================

@dataclass(frozen=True)
class TelemetrySnapshot:
    timestamp_ns: int

    roll: float
    pitch: float
    yaw: float

    angular_rate_p: float
    angular_rate_q: float
    angular_rate_r: float

    depth: float
    depth_rate: float
    temperature: float

    # Retained for future firmware that exposes raw pressure explicitly.
    # The supplied protocol currently provides depth, not pressure.
    pressure: float

    raw_status: int
    ahrs_device_id: int

    gimbal_locked: bool
    power_mode: str

    data_connected: bool
    command_connected: bool
    gnss_connected: bool
    usbl_connected: bool


@dataclass(frozen=True)
class PositionSnapshot:
    timestamp_ns: int
    valid: bool
    latitude: float
    longitude: float
    altitude: float
    fix_quality: int
    satellites: int
    hdop: float


@dataclass(frozen=True)
class DeviceTimeSnapshot:
    timestamp_ns: int
    milliseconds: int
    year: int
    month: int
    week: int
    date: int
    hours: int
    minutes: int
    seconds: int


@dataclass(frozen=True)
class BulkStatusSnapshot:
    timestamp_ns: int
    last_frame_sequence: int
    last_payload_length: int
    frames_received: int
    dropped_frames: int
    sequence_resets: int
    malformed_frames: int
    channel_id_mismatches: int
    error_flag_words: int
    filter_not_settled_words: int
    repeated_words: int
    saturated_words: int


@dataclass(frozen=True)
class DiagnosticSnapshot:
    timestamp_ns: int
    fields: tuple[int, ...]
    counter: int


@dataclass(frozen=True)
class ControllerHealthSnapshot:
    timestamp_ns: int
    unit_id: int
    processor_status_code: int


@dataclass(frozen=True)
class FirmwareInfoSnapshot:
    timestamp_ns: int
    text: str


@dataclass(frozen=True)
class ADCSnapshot:
    total_samples: int
    sample_rate_hz: int

    timestamp_ns: list[int]

    ch0: list[int]
    ch1: list[int]
    ch2: list[int]
    ch3: list[int]

    status0: list[int]
    status1: list[int]
    status2: list[int]
    status3: list[int]

    def __len__(self) -> int:
        return len(self.timestamp_ns)

    @property
    def x(self) -> list[int]:
        return self.ch0

    @property
    def y(self) -> list[int]:
        return self.ch1

    @property
    def z(self) -> list[int]:
        return self.ch2


@dataclass(frozen=True)
class ADCNumpySnapshot:
    total_samples: int
    sample_rate_hz: int

    timestamp_ns: Any

    ch0: Any
    ch1: Any
    ch2: Any
    ch3: Any

    status0: Any
    status1: Any
    status2: Any
    status3: Any

    def __len__(self) -> int:
        return int(len(self.timestamp_ns))

    @property
    def x(self):
        return self.ch0

    @property
    def y(self):
        return self.ch1

    @property
    def z(self):
        return self.ch2


# =============================================================================
# Shared RAM class
# =============================================================================

class OBSSharedData:
    """
    Named full-RAM shared-data transport.
    """

    def __init__(
        self,
        name: str = DEFAULT_SHARED_MEMORY_NAME,
    ):
        self.name = str(name)
        self._created = False

        try:
            self._shm = shared_memory.SharedMemory(
                name=self.name,
                create=True,
                size=SHARED_MEMORY_SIZE,
            )
            self._created = True

        except FileExistsError:
            self._shm = shared_memory.SharedMemory(
                name=self.name,
                create=False,
            )

        self._buf = self._shm.buf
        self.size = int(self._shm.size)

        # Writers are all expected to live in the acquisition/control process.
        # These locks therefore protect concurrent receiver threads without
        # introducing IPC serialization.
        self._adc_lock = threading.Lock()
        self._telemetry_lock = threading.Lock()
        self._gnss_lock = threading.Lock()
        self._usbl_lock = threading.Lock()
        self._device_time_lock = threading.Lock()
        self._bulk_lock = threading.Lock()
        self._diagnostic_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._firmware_lock = threading.Lock()

        if self._created:
            self._initialize_new_memory()
        else:
            self._validate_existing_memory()

    # -------------------------------------------------------------------------
    # Lifetime
    # -------------------------------------------------------------------------

    @property
    def created_by_this_process(self) -> bool:
        return self._created

    def close(self) -> None:
        try:
            self._buf.release()
        except Exception:
            pass

        try:
            self._shm.close()
        except Exception:
            pass

    def unlink(self) -> None:
        """
        Explicitly delete the named shared-memory object.

        Ordinary readers should not call this.
        """
        try:
            self._shm.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ):
        self.close()

    # -------------------------------------------------------------------------
    # Initialization
    # -------------------------------------------------------------------------

    def _initialize_new_memory(self) -> None:
        if self.size < SHARED_MEMORY_SIZE:
            raise RuntimeError(
                "Shared-memory allocation is smaller than the v3 layout."
            )

        self._buf[:SHARED_MEMORY_SIZE] = (
            b"\x00" * SHARED_MEMORY_SIZE
        )

        HEADER_STRUCT.pack_into(
            self._buf,
            HEADER_OFFSET,
            MAGIC,
            VERSION,
            ADC_CAPACITY,
            ADC_SAMPLE_RATE_HZ,
            ADC_SLOT_SIZE,
            SHARED_MEMORY_SIZE,
        )

        ADC_META_STRUCT.pack_into(
            self._buf,
            ADC_META_OFFSET,
            0,
        )

        self._write_telemetry_full(
            timestamp_ns=time.time_ns(),
            roll=0.0,
            pitch=0.0,
            yaw=0.0,
            angular_rate_p=0.0,
            angular_rate_q=0.0,
            angular_rate_r=0.0,
            depth=0.0,
            depth_rate=0.0,
            temperature=0.0,
            pressure=0.0,
            raw_status=0,
            ahrs_device_id=0,
            gimbal_locked=True,
            power_low=False,
            data_connected=False,
            command_connected=False,
            gnss_connected=False,
            usbl_connected=False,
        )

        self.update_gnss(
            valid=False,
            latitude=0.0,
            longitude=0.0,
        )

        self.update_usbl(
            valid=False,
            latitude=0.0,
            longitude=0.0,
        )

        self.update_device_time(
            milliseconds=0,
            year=0,
            month=0,
            week=0,
            date=0,
            hours=0,
            minutes=0,
            seconds=0,
        )

        self.reset_bulk_status()
        self.update_diagnostic(
            [0] * 10,
            counter=0,
        )

        self.update_controller_health(
            unit_id=0,
            processor_status_code=0,
        )

        self.update_firmware_info("")

    def _validate_existing_memory(self) -> None:
        if self.size < HEADER_STRUCT.size:
            raise RuntimeError(
                f"Shared RAM '{self.name}' is too small."
            )

        (
            magic,
            version,
            capacity,
            sample_rate,
            slot_size,
            required_size,
        ) = HEADER_STRUCT.unpack_from(
            self._buf,
            HEADER_OFFSET,
        )

        if (
            magic != MAGIC
            or version != VERSION
            or capacity != ADC_CAPACITY
            or sample_rate != ADC_SAMPLE_RATE_HZ
            or slot_size != ADC_SLOT_SIZE
            or required_size != SHARED_MEMORY_SIZE
            or self.size < SHARED_MEMORY_SIZE
        ):
            raise RuntimeError(
                "Shared-memory layout mismatch. "
                "Close old OBS processes or use the matching shared_data version."
            )

    # -------------------------------------------------------------------------
    # Generic sequence-lock record helpers
    # -------------------------------------------------------------------------

    def _next_odd_sequence(
        self,
        offset: int,
    ) -> int:
        current = struct.unpack_from(
            "<Q",
            self._buf,
            offset,
        )[0]

        value = current + 1

        if not (value & 1):
            value += 1

        return value

    def _stable_unpack(
        self,
        record_struct: struct.Struct,
        offset: int,
    ):
        values = None

        for _ in range(64):
            seq_before = struct.unpack_from(
                "<Q",
                self._buf,
                offset,
            )[0]

            if seq_before & 1:
                continue

            values = record_struct.unpack_from(
                self._buf,
                offset,
            )

            seq_after = struct.unpack_from(
                "<Q",
                self._buf,
                offset,
            )[0]

            if (
                seq_before == seq_after
                and not (seq_after & 1)
            ):
                return values

        if values is None:
            values = record_struct.unpack_from(
                self._buf,
                offset,
            )

        return values

    # -------------------------------------------------------------------------
    # ADC high-rate writer
    # -------------------------------------------------------------------------

    def adc_total_samples(self) -> int:
        (total,) = ADC_META_STRUCT.unpack_from(
            self._buf,
            ADC_META_OFFSET,
        )

        return int(total)

    def reset_adc(self) -> None:
        with self._adc_lock:
            ADC_META_STRUCT.pack_into(
                self._buf,
                ADC_META_OFFSET,
                0,
            )

    def _write_adc_slot(
        self,
        absolute_index: int,
        timestamp_ns: int,
        values: Sequence[int],
        statuses: Sequence[int],
    ) -> None:

        slot_index = (
            absolute_index
            % ADC_CAPACITY
        )

        offset = (
            ADC_RING_OFFSET
            + slot_index * ADC_SLOT_SIZE
        )

        stable_seq = (
            absolute_index + 1
        ) * 2

        writing_seq = (
            stable_seq - 1
        )

        # Mark unstable first.
        struct.pack_into(
            "<Q",
            self._buf,
            offset,
            writing_seq,
        )

        ADC_SLOT_STRUCT.pack_into(
            self._buf,
            offset,
            writing_seq,
            int(timestamp_ns),
            int(values[0]),
            int(values[1]),
            int(values[2]),
            int(values[3]),
            int(statuses[0]) & 0xFF,
            int(statuses[1]) & 0xFF,
            int(statuses[2]) & 0xFF,
            int(statuses[3]) & 0xFF,
        )

        # Commit.
        struct.pack_into(
            "<Q",
            self._buf,
            offset,
            stable_seq,
        )

    def write_adc_sample(
        self,
        ch0: int,
        ch1: int,
        ch2: int,
        ch3: int,
        *,
        status0: int = 0,
        status1: int = 1,
        status2: int = 2,
        status3: int = 3,
        timestamp_ns: Optional[int] = None,
    ) -> int:

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._adc_lock:
            total = self.adc_total_samples()
            absolute_index = total

            self._write_adc_slot(
                absolute_index,
                int(timestamp_ns),
                (ch0, ch1, ch2, ch3),
                (status0, status1, status2, status3),
            )

            ADC_META_STRUCT.pack_into(
                self._buf,
                ADC_META_OFFSET,
                absolute_index + 1,
            )

            return absolute_index

    def write_adc_block(
        self,
        samples: Iterable[Sequence[int]],
        *,
        statuses: Optional[Iterable[Sequence[int]]] = None,
        timestamps_ns: Optional[Iterable[int]] = None,
    ) -> int:
        """
        Write multiple ADC frames under one local writer lock.

        samples:
            iterable of (ch0, ch1, ch2, ch3)

        statuses:
            optional iterable of (status0, status1, status2, status3)

        timestamps_ns:
            optional iterable with one timestamp per ADC frame
        """

        sample_list = list(samples)

        if not sample_list:
            return 0

        if statuses is None:
            status_list = [
                (0, 1, 2, 3)
                for _ in sample_list
            ]
        else:
            status_list = list(statuses)

        if len(status_list) != len(sample_list):
            raise ValueError(
                "statuses length must match samples length"
            )

        if timestamps_ns is None:
            now = time.time_ns()
            interval_ns = int(
                1_000_000_000
                / ADC_SAMPLE_RATE_HZ
            )

            first = (
                now
                - (len(sample_list) - 1)
                * interval_ns
            )

            timestamp_list = [
                first + i * interval_ns
                for i in range(
                    len(sample_list)
                )
            ]
        else:
            timestamp_list = list(
                timestamps_ns
            )

        if len(timestamp_list) != len(sample_list):
            raise ValueError(
                "timestamps_ns length must match samples length"
            )

        with self._adc_lock:
            total = self.adc_total_samples()

            for index, (
                values,
                status_values,
                timestamp_ns,
            ) in enumerate(
                zip(
                    sample_list,
                    status_list,
                    timestamp_list,
                )
            ):
                self._write_adc_slot(
                    total + index,
                    int(timestamp_ns),
                    values,
                    status_values,
                )

            ADC_META_STRUCT.pack_into(
                self._buf,
                ADC_META_OFFSET,
                total + len(sample_list),
            )

        return len(sample_list)

    # -------------------------------------------------------------------------
    # ADC standard reader
    # -------------------------------------------------------------------------

    def read_adc_latest(
        self,
        count: int = 1000,
    ) -> ADCSnapshot:

        count = max(
            0,
            min(
                int(count),
                ADC_CAPACITY,
            ),
        )

        total = self.adc_total_samples()
        start = max(
            0,
            total - count,
        )

        timestamp_ns: list[int] = []

        ch0: list[int] = []
        ch1: list[int] = []
        ch2: list[int] = []
        ch3: list[int] = []

        status0: list[int] = []
        status1: list[int] = []
        status2: list[int] = []
        status3: list[int] = []

        for absolute_index in range(
            start,
            total,
        ):
            slot_index = (
                absolute_index
                % ADC_CAPACITY
            )

            offset = (
                ADC_RING_OFFSET
                + slot_index * ADC_SLOT_SIZE
            )

            expected_seq = (
                absolute_index + 1
            ) * 2

            seq_before = struct.unpack_from(
                "<Q",
                self._buf,
                offset,
            )[0]

            if (
                seq_before != expected_seq
                or (seq_before & 1)
            ):
                continue

            values = ADC_SLOT_STRUCT.unpack_from(
                self._buf,
                offset,
            )

            seq_after = struct.unpack_from(
                "<Q",
                self._buf,
                offset,
            )[0]

            if (
                values[0] != expected_seq
                or seq_after != expected_seq
                or (seq_after & 1)
            ):
                continue

            timestamp_ns.append(
                int(values[1])
            )

            ch0.append(int(values[2]))
            ch1.append(int(values[3]))
            ch2.append(int(values[4]))
            ch3.append(int(values[5]))

            status0.append(int(values[6]))
            status1.append(int(values[7]))
            status2.append(int(values[8]))
            status3.append(int(values[9]))

        return ADCSnapshot(
            total_samples=total,
            sample_rate_hz=ADC_SAMPLE_RATE_HZ,
            timestamp_ns=timestamp_ns,
            ch0=ch0,
            ch1=ch1,
            ch2=ch2,
            ch3=ch3,
            status0=status0,
            status1=status1,
            status2=status2,
            status3=status3,
        )

    # -------------------------------------------------------------------------
    # ADC NumPy reader
    # -------------------------------------------------------------------------

    def read_adc_latest_numpy(
        self,
        count: int = 3000,
    ) -> ADCNumpySnapshot:
        """
        Fast copy for plotting / FFT.

        NumPy arrays are copied out of the live ring so readers can safely
        process them while the TCP writer continues.
        """

        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "NumPy is required for read_adc_latest_numpy(). "
                "Install with: pip install numpy"
            ) from exc

        count = max(
            0,
            min(
                int(count),
                ADC_CAPACITY,
            ),
        )

        total = self.adc_total_samples()
        start = max(
            0,
            total - count,
        )

        dtype = np.dtype(
            [
                ("seq", "<u8"),
                ("timestamp_ns", "<i8"),
                ("ch0", "<i4"),
                ("ch1", "<i4"),
                ("ch2", "<i4"),
                ("ch3", "<i4"),
                ("status0", "u1"),
                ("status1", "u1"),
                ("status2", "u1"),
                ("status3", "u1"),
                ("padding", "V4"),
            ],
            align=False,
        )

        if dtype.itemsize != ADC_SLOT_SIZE:
            raise RuntimeError(
                "NumPy ADC dtype does not match v3 ring layout."
            )

        if start >= total:
            empty_i64 = np.empty(
                0,
                dtype=np.int64,
            )
            empty_i32 = np.empty(
                0,
                dtype=np.int32,
            )
            empty_u8 = np.empty(
                0,
                dtype=np.uint8,
            )

            return ADCNumpySnapshot(
                total_samples=total,
                sample_rate_hz=ADC_SAMPLE_RATE_HZ,
                timestamp_ns=empty_i64,
                ch0=empty_i32.copy(),
                ch1=empty_i32.copy(),
                ch2=empty_i32.copy(),
                ch3=empty_i32.copy(),
                status0=empty_u8.copy(),
                status1=empty_u8.copy(),
                status2=empty_u8.copy(),
                status3=empty_u8.copy(),
            )

        ring = np.ndarray(
            shape=(ADC_CAPACITY,),
            dtype=dtype,
            buffer=self._buf,
            offset=ADC_RING_OFFSET,
        )

        absolute_indices = np.arange(
            start,
            total,
            dtype=np.uint64,
        )

        ring_indices = (
            absolute_indices
            % ADC_CAPACITY
        ).astype(
            np.intp,
            copy=False,
        )

        snapshot = ring[
            ring_indices
        ].copy()

        seq_after = ring["seq"][
            ring_indices
        ].copy()

        expected_seq = (
            absolute_indices + 1
        ) * 2

        valid = (
            (snapshot["seq"] == expected_seq)
            & (seq_after == expected_seq)
            & ((seq_after & 1) == 0)
        )

        snapshot = snapshot[
            valid
        ]

        return ADCNumpySnapshot(
            total_samples=total,
            sample_rate_hz=ADC_SAMPLE_RATE_HZ,
            timestamp_ns=snapshot[
                "timestamp_ns"
            ].astype(
                np.int64,
                copy=True,
            ),
            ch0=snapshot["ch0"].astype(
                np.int32,
                copy=True,
            ),
            ch1=snapshot["ch1"].astype(
                np.int32,
                copy=True,
            ),
            ch2=snapshot["ch2"].astype(
                np.int32,
                copy=True,
            ),
            ch3=snapshot["ch3"].astype(
                np.int32,
                copy=True,
            ),
            status0=snapshot["status0"].astype(
                np.uint8,
                copy=True,
            ),
            status1=snapshot["status1"].astype(
                np.uint8,
                copy=True,
            ),
            status2=snapshot["status2"].astype(
                np.uint8,
                copy=True,
            ),
            status3=snapshot["status3"].astype(
                np.uint8,
                copy=True,
            ),
        )

    # -------------------------------------------------------------------------
    # Telemetry
    # -------------------------------------------------------------------------

    def _write_telemetry_full(
        self,
        *,
        timestamp_ns: int,
        roll: float,
        pitch: float,
        yaw: float,
        angular_rate_p: float,
        angular_rate_q: float,
        angular_rate_r: float,
        depth: float,
        depth_rate: float,
        temperature: float,
        pressure: float,
        raw_status: int,
        ahrs_device_id: int,
        gimbal_locked: bool,
        power_low: bool,
        data_connected: bool,
        command_connected: bool,
        gnss_connected: bool,
        usbl_connected: bool,
    ) -> None:

        write_seq = self._next_odd_sequence(
            TELEMETRY_OFFSET
        )

        TELEMETRY_STRUCT.pack_into(
            self._buf,
            TELEMETRY_OFFSET,
            write_seq,
            int(timestamp_ns),
            float(roll),
            float(pitch),
            float(yaw),
            float(angular_rate_p),
            float(angular_rate_q),
            float(angular_rate_r),
            float(depth),
            float(depth_rate),
            float(temperature),
            float(pressure),
            int(raw_status) & 0xFFFFFFFF,
            int(ahrs_device_id) & 0xFFFFFFFF,
            int(bool(gimbal_locked)),
            int(bool(power_low)),
            int(bool(data_connected)),
            int(bool(command_connected)),
            int(bool(gnss_connected)),
            int(bool(usbl_connected)),
        )

        struct.pack_into(
            "<Q",
            self._buf,
            TELEMETRY_OFFSET,
            write_seq + 1,
        )

    def read_telemetry(
        self,
    ) -> TelemetrySnapshot:

        values = self._stable_unpack(
            TELEMETRY_STRUCT,
            TELEMETRY_OFFSET,
        )

        return TelemetrySnapshot(
            timestamp_ns=int(values[1]),
            roll=float(values[2]),
            pitch=float(values[3]),
            yaw=float(values[4]),
            angular_rate_p=float(values[5]),
            angular_rate_q=float(values[6]),
            angular_rate_r=float(values[7]),
            depth=float(values[8]),
            depth_rate=float(values[9]),
            temperature=float(values[10]),
            pressure=float(values[11]),
            raw_status=int(values[12]),
            ahrs_device_id=int(values[13]),
            gimbal_locked=bool(values[14]),
            power_mode=(
                "LOW"
                if values[15]
                else "NORMAL"
            ),
            data_connected=bool(values[16]),
            command_connected=bool(values[17]),
            gnss_connected=bool(values[18]),
            usbl_connected=bool(values[19]),
        )

    def update_telemetry(
        self,
        *,
        timestamp_ns: Optional[int] = None,
        roll: Optional[float] = None,
        pitch: Optional[float] = None,
        yaw: Optional[float] = None,
        angular_rate_p: Optional[float] = None,
        angular_rate_q: Optional[float] = None,
        angular_rate_r: Optional[float] = None,
        depth: Optional[float] = None,
        depth_rate: Optional[float] = None,
        temperature: Optional[float] = None,
        pressure: Optional[float] = None,
        raw_status: Optional[int] = None,
        ahrs_device_id: Optional[int] = None,
        gimbal_locked: Optional[bool] = None,
        power_mode: Optional[str] = None,
        data_connected: Optional[bool] = None,
        command_connected: Optional[bool] = None,
        gnss_connected: Optional[bool] = None,
        usbl_connected: Optional[bool] = None,
    ) -> None:

        with self._telemetry_lock:
            current = self.read_telemetry()

            if timestamp_ns is None:
                timestamp_ns = time.time_ns()

            power_low = (
                current.power_mode == "LOW"
                if power_mode is None
                else str(
                    power_mode
                ).strip().upper() == "LOW"
            )

            self._write_telemetry_full(
                timestamp_ns=int(timestamp_ns),
                roll=current.roll if roll is None else roll,
                pitch=current.pitch if pitch is None else pitch,
                yaw=current.yaw if yaw is None else yaw,
                angular_rate_p=(
                    current.angular_rate_p
                    if angular_rate_p is None
                    else angular_rate_p
                ),
                angular_rate_q=(
                    current.angular_rate_q
                    if angular_rate_q is None
                    else angular_rate_q
                ),
                angular_rate_r=(
                    current.angular_rate_r
                    if angular_rate_r is None
                    else angular_rate_r
                ),
                depth=current.depth if depth is None else depth,
                depth_rate=(
                    current.depth_rate
                    if depth_rate is None
                    else depth_rate
                ),
                temperature=(
                    current.temperature
                    if temperature is None
                    else temperature
                ),
                pressure=(
                    current.pressure
                    if pressure is None
                    else pressure
                ),
                raw_status=(
                    current.raw_status
                    if raw_status is None
                    else raw_status
                ),
                ahrs_device_id=(
                    current.ahrs_device_id
                    if ahrs_device_id is None
                    else ahrs_device_id
                ),
                gimbal_locked=(
                    current.gimbal_locked
                    if gimbal_locked is None
                    else gimbal_locked
                ),
                power_low=power_low,
                data_connected=(
                    current.data_connected
                    if data_connected is None
                    else data_connected
                ),
                command_connected=(
                    current.command_connected
                    if command_connected is None
                    else command_connected
                ),
                gnss_connected=(
                    current.gnss_connected
                    if gnss_connected is None
                    else gnss_connected
                ),
                usbl_connected=(
                    current.usbl_connected
                    if usbl_connected is None
                    else usbl_connected
                ),
            )

    # -------------------------------------------------------------------------
    # GNSS / USBL
    # -------------------------------------------------------------------------

    def _write_position(
        self,
        offset: int,
        lock: threading.Lock,
        *,
        timestamp_ns: Optional[int],
        valid: bool,
        latitude: float,
        longitude: float,
        altitude: float,
        fix_quality: int,
        satellites: int,
        hdop: float,
    ) -> None:

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with lock:
            write_seq = self._next_odd_sequence(
                offset
            )

            POSITION_STRUCT.pack_into(
                self._buf,
                offset,
                write_seq,
                int(timestamp_ns),
                int(bool(valid)),
                float(latitude),
                float(longitude),
                float(altitude),
                int(fix_quality),
                int(satellites),
                float(hdop),
            )

            struct.pack_into(
                "<Q",
                self._buf,
                offset,
                write_seq + 1,
            )

    def _read_position(
        self,
        offset: int,
    ) -> PositionSnapshot:

        values = self._stable_unpack(
            POSITION_STRUCT,
            offset,
        )

        return PositionSnapshot(
            timestamp_ns=int(values[1]),
            valid=bool(values[2]),
            latitude=float(values[3]),
            longitude=float(values[4]),
            altitude=float(values[5]),
            fix_quality=int(values[6]),
            satellites=int(values[7]),
            hdop=float(values[8]),
        )

    def update_gnss(
        self,
        *,
        valid: bool,
        latitude: float,
        longitude: float,
        altitude: float = 0.0,
        fix_quality: int = 0,
        satellites: int = 0,
        hdop: float = 0.0,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        self._write_position(
            GNSS_OFFSET,
            self._gnss_lock,
            timestamp_ns=timestamp_ns,
            valid=valid,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            fix_quality=fix_quality,
            satellites=satellites,
            hdop=hdop,
        )

    def update_usbl(
        self,
        *,
        valid: bool,
        latitude: float,
        longitude: float,
        altitude: float = 0.0,
        fix_quality: int = 0,
        satellites: int = 0,
        hdop: float = 0.0,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        self._write_position(
            USBL_OFFSET,
            self._usbl_lock,
            timestamp_ns=timestamp_ns,
            valid=valid,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            fix_quality=fix_quality,
            satellites=satellites,
            hdop=hdop,
        )

    def read_gnss(self) -> PositionSnapshot:
        return self._read_position(
            GNSS_OFFSET
        )

    def read_usbl(self) -> PositionSnapshot:
        return self._read_position(
            USBL_OFFSET
        )

    # -------------------------------------------------------------------------
    # TIME1
    # -------------------------------------------------------------------------

    def update_device_time(
        self,
        *,
        milliseconds: int,
        year: int,
        month: int,
        week: int,
        date: int,
        hours: int,
        minutes: int,
        seconds: int,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._device_time_lock:
            write_seq = self._next_odd_sequence(
                DEVICE_TIME_OFFSET
            )

            DEVICE_TIME_STRUCT.pack_into(
                self._buf,
                DEVICE_TIME_OFFSET,
                write_seq,
                int(timestamp_ns),
                int(milliseconds),
                int(year),
                int(month),
                int(week),
                int(date),
                int(hours),
                int(minutes),
                int(seconds),
            )

            struct.pack_into(
                "<Q",
                self._buf,
                DEVICE_TIME_OFFSET,
                write_seq + 1,
            )

    def read_device_time(
        self,
    ) -> DeviceTimeSnapshot:

        values = self._stable_unpack(
            DEVICE_TIME_STRUCT,
            DEVICE_TIME_OFFSET,
        )

        return DeviceTimeSnapshot(
            timestamp_ns=int(values[1]),
            milliseconds=int(values[2]),
            year=int(values[3]),
            month=int(values[4]),
            week=int(values[5]),
            date=int(values[6]),
            hours=int(values[7]),
            minutes=int(values[8]),
            seconds=int(values[9]),
        )

    # -------------------------------------------------------------------------
    # Bulk-data diagnostics
    # -------------------------------------------------------------------------

    def reset_bulk_status(self) -> None:
        with self._bulk_lock:
            write_seq = self._next_odd_sequence(
                BULK_STATUS_OFFSET
            )

            BULK_STATUS_STRUCT.pack_into(
                self._buf,
                BULK_STATUS_OFFSET,
                write_seq,
                time.time_ns(),
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            )

            struct.pack_into(
                "<Q",
                self._buf,
                BULK_STATUS_OFFSET,
                write_seq + 1,
            )

    def read_bulk_status(
        self,
    ) -> BulkStatusSnapshot:

        values = self._stable_unpack(
            BULK_STATUS_STRUCT,
            BULK_STATUS_OFFSET,
        )

        return BulkStatusSnapshot(
            timestamp_ns=int(values[1]),
            last_frame_sequence=int(values[2]),
            last_payload_length=int(values[3]),
            frames_received=int(values[4]),
            dropped_frames=int(values[5]),
            sequence_resets=int(values[6]),
            malformed_frames=int(values[7]),
            channel_id_mismatches=int(values[8]),
            error_flag_words=int(values[9]),
            filter_not_settled_words=int(values[10]),
            repeated_words=int(values[11]),
            saturated_words=int(values[12]),
        )

    def update_bulk_status(
        self,
        *,
        frame_sequence: Optional[int] = None,
        payload_length: Optional[int] = None,
        frames_received_add: int = 0,
        dropped_frames_add: int = 0,
        sequence_resets_add: int = 0,
        malformed_frames_add: int = 0,
        channel_id_mismatches_add: int = 0,
        error_flag_words_add: int = 0,
        filter_not_settled_words_add: int = 0,
        repeated_words_add: int = 0,
        saturated_words_add: int = 0,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._bulk_lock:
            current = self.read_bulk_status()

            write_seq = self._next_odd_sequence(
                BULK_STATUS_OFFSET
            )

            BULK_STATUS_STRUCT.pack_into(
                self._buf,
                BULK_STATUS_OFFSET,
                write_seq,
                int(timestamp_ns),
                (
                    current.last_frame_sequence
                    if frame_sequence is None
                    else int(frame_sequence) & 0xFFFFFFFF
                ),
                (
                    current.last_payload_length
                    if payload_length is None
                    else int(payload_length)
                ),
                current.frames_received
                + int(frames_received_add),
                current.dropped_frames
                + int(dropped_frames_add),
                current.sequence_resets
                + int(sequence_resets_add),
                current.malformed_frames
                + int(malformed_frames_add),
                current.channel_id_mismatches
                + int(channel_id_mismatches_add),
                current.error_flag_words
                + int(error_flag_words_add),
                current.filter_not_settled_words
                + int(filter_not_settled_words_add),
                current.repeated_words
                + int(repeated_words_add),
                current.saturated_words
                + int(saturated_words_add),
            )

            struct.pack_into(
                "<Q",
                self._buf,
                BULK_STATUS_OFFSET,
                write_seq + 1,
            )

    # -------------------------------------------------------------------------
    # GDAT2
    # -------------------------------------------------------------------------

    def update_diagnostic(
        self,
        fields: Sequence[int],
        *,
        counter: int,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        if len(fields) != 10:
            raise ValueError(
                "GDAT2 requires exactly 10 diagnostic fields."
            )

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._diagnostic_lock:
            write_seq = self._next_odd_sequence(
                DIAGNOSTIC_OFFSET
            )

            DIAGNOSTIC_STRUCT.pack_into(
                self._buf,
                DIAGNOSTIC_OFFSET,
                write_seq,
                int(timestamp_ns),
                *[
                    int(value) & 0xFFFFFFFF
                    for value in fields
                ],
                int(counter) & 0xFFFFFFFFFFFFFFFF,
            )

            struct.pack_into(
                "<Q",
                self._buf,
                DIAGNOSTIC_OFFSET,
                write_seq + 1,
            )

    def read_diagnostic(
        self,
    ) -> DiagnosticSnapshot:

        values = self._stable_unpack(
            DIAGNOSTIC_STRUCT,
            DIAGNOSTIC_OFFSET,
        )

        return DiagnosticSnapshot(
            timestamp_ns=int(values[1]),
            fields=tuple(
                int(value)
                for value in values[2:12]
            ),
            counter=int(values[12]),
        )

    # -------------------------------------------------------------------------
    # XCHM0
    # -------------------------------------------------------------------------

    def update_controller_health(
        self,
        *,
        unit_id: int,
        processor_status_code: int,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        with self._health_lock:
            write_seq = self._next_odd_sequence(
                CONTROLLER_HEALTH_OFFSET
            )

            CONTROLLER_HEALTH_STRUCT.pack_into(
                self._buf,
                CONTROLLER_HEALTH_OFFSET,
                write_seq,
                int(timestamp_ns),
                int(unit_id) & 0xFFFFFFFF,
                int(processor_status_code) & 0xFFFFFFFF,
            )

            struct.pack_into(
                "<Q",
                self._buf,
                CONTROLLER_HEALTH_OFFSET,
                write_seq + 1,
            )

    def read_controller_health(
        self,
    ) -> ControllerHealthSnapshot:

        values = self._stable_unpack(
            CONTROLLER_HEALTH_STRUCT,
            CONTROLLER_HEALTH_OFFSET,
        )

        return ControllerHealthSnapshot(
            timestamp_ns=int(values[1]),
            unit_id=int(values[2]),
            processor_status_code=int(values[3]),
        )

    # -------------------------------------------------------------------------
    # XFWVR
    # -------------------------------------------------------------------------

    def update_firmware_info(
        self,
        text: str,
        *,
        timestamp_ns: Optional[int] = None,
    ) -> None:

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

        encoded = str(text).encode(
            "utf-8",
            errors="replace",
        )[:FIRMWARE_TEXT_BYTES - 1]

        encoded = encoded + (
            b"\x00"
            * (
                FIRMWARE_TEXT_BYTES
                - len(encoded)
            )
        )

        with self._firmware_lock:
            write_seq = self._next_odd_sequence(
                FIRMWARE_INFO_OFFSET
            )

            FIRMWARE_INFO_STRUCT.pack_into(
                self._buf,
                FIRMWARE_INFO_OFFSET,
                write_seq,
                int(timestamp_ns),
                encoded,
            )

            struct.pack_into(
                "<Q",
                self._buf,
                FIRMWARE_INFO_OFFSET,
                write_seq + 1,
            )

    def read_firmware_info(
        self,
    ) -> FirmwareInfoSnapshot:

        values = self._stable_unpack(
            FIRMWARE_INFO_STRUCT,
            FIRMWARE_INFO_OFFSET,
        )

        raw = values[2].split(
            b"\x00",
            1,
        )[0]

        return FirmwareInfoSnapshot(
            timestamp_ns=int(values[1]),
            text=raw.decode(
                "utf-8",
                errors="replace",
            ),
        )

    # -------------------------------------------------------------------------
    # Runtime reset
    # -------------------------------------------------------------------------

    def reset_runtime_state(
        self,
        *,
        clear_adc: bool = False,
    ) -> None:

        if clear_adc:
            self.reset_adc()

        self.update_telemetry(
            roll=0.0,
            pitch=0.0,
            yaw=0.0,
            angular_rate_p=0.0,
            angular_rate_q=0.0,
            angular_rate_r=0.0,
            depth=0.0,
            depth_rate=0.0,
            temperature=0.0,
            pressure=0.0,
            raw_status=0,
            ahrs_device_id=0,
            gimbal_locked=True,
            power_mode="NORMAL",
            data_connected=False,
            command_connected=False,
            gnss_connected=False,
            usbl_connected=False,
        )

        self.update_gnss(
            valid=False,
            latitude=0.0,
            longitude=0.0,
        )

        self.update_usbl(
            valid=False,
            latitude=0.0,
            longitude=0.0,
        )


def decode_adc_status(
    status: int,
) -> dict[str, int | bool]:
    """
    Decode the ADC top-byte status according to the supplied OBS protocol.
    """

    value = int(status) & 0xFF

    return {
        "error": bool(
            value
            & ADC_STATUS_ERROR
        ),
        "filter_not_settled": bool(
            value
            & ADC_STATUS_FILTER_NOT_SETTLED
        ),
        "repeated": bool(
            value
            & ADC_STATUS_REPEATED
        ),
        "filter_type": bool(
            value
            & ADC_STATUS_FILTER_TYPE
        ),
        "saturated": bool(
            value
            & ADC_STATUS_SATURATED
        ),
        "channel_id": (
            value
            & ADC_STATUS_CHANNEL_ID_MASK
        ),
    }


def demo() -> None:
    with OBSSharedData() as shared:
        print(
            "Shared RAM:",
            shared.name,
        )
        print(
            "Size:",
            f"{shared.size / (1024 * 1024):.2f} MB",
        )
        print(
            "Telemetry:",
            shared.read_telemetry(),
        )
        print(
            "Bulk:",
            shared.read_bulk_status(),
        )


if __name__ == "__main__":
    demo()
