"""
obs_setting.py
==============

GRC-UGM-PERTAMINA OBS
OBS Setting / Connection Manager

Version: 10
Shared data: shared_data_v5.py
Protocol baseline: OBS TCP protocol supplied 2026-08-19.

OBS TCP
-------
Command / Telemetry:
    TCP port 54300
    Client connects to OBS.
    NMEA-0183 style text:
        $ID,field1,...*CC<CR><LF>
    CC = XOR of bytes between '$' and '*'
    Maximum sentence length = 82 bytes.

Telemetry parsed:
    $GDAT2
    $TIME1
    $DEPT0
    $AHRS2
    $XFWVR
    $XCHM0

Bulk data:
    TCP port 54301

    Header 12 bytes:
        0..3   magic "OBS:"
        4..7   uint32 sequence, little-endian
        8..11  uint32 payload length, little-endian

    Current payload:
        2048 bytes
        4 channels x 128 ADC frames x 4-byte words

    ADC word:
        bits 23..0  signed 24-bit ADC
        bits 31..24 status byte

Shared RAM
----------
Live data is written through shared_data_v5.py.

The bulk ADC source remains nominally 1000 Hz/channel. Before publication to
shared RAM, CH0/CH1/CH2 (Geophone X/Y/Z) are block-averaged using the selected
Decimation Samples value. CH3 follows the same averaging window to preserve the
4-channel frame structure.

Example:
    Decimation Samples = 5
    1000 raw frames/s / 5 = 200 output frames/s

shared_data_v5 publishes the authoritative effective sample rate and output
sample period. Decimation is NOT represented as fake missing samples.

GNSS
----
Serial COM Port only. NMEA GGA is parsed and published to shared RAM.

USBL
----
Selectable COM Port or UDP. NMEA GGA is parsed and published separately.

Gimbal / Power commands
-----------------------
The supplied protocol describes the command-channel framing and outgoing
telemetry, but it does NOT define the actual gimbal/power command sentence IDs
or their feedback mapping.

Therefore this program does not invent unsafe command IDs. Optional command
bodies can be configured in obs_settings.ini under [OBS_Commands]. Example:

    gimbal_lock = SOMEID,1

The program will automatically convert that body to:

    $SOMEID,1*CC\r\n

Until the actual command IDs are provided, the command values may remain blank.
"""

from __future__ import annotations

import configparser
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows GUI startup
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.SETTING"

if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

        kernel32 = ctypes.windll.kernel32

        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()

    except Exception:
        pass


# =============================================================================
# Qt
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


# =============================================================================
# Shared RAM v3
# =============================================================================

from shared_data_v5 import (
    RAW_ADC_SAMPLE_RATE_HZ,
    ADC_STATUS_CHANNEL_ID_MASK,
    ADC_STATUS_ERROR,
    ADC_STATUS_FILTER_NOT_SETTLED,
    ADC_STATUS_REPEATED,
    ADC_STATUS_SATURATED,
    OBSSharedData,
)


# =============================================================================
# Application constants
# =============================================================================

APP_TITLE = "OBS Setting"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"
APP_VERSION = "10"

BASE_DIR = Path(__file__).resolve().parent

# Bundled read-only resources live beside the frozen module. Writable user
# configuration/logs remain next to the executable in one-folder releases.
EXTERNAL_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else BASE_DIR
)

ASSETS_DIR = BASE_DIR / "assets"
ICON_DIR = ASSETS_DIR / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

INI_PATH = EXTERNAL_DIR / "obs_settings.ini"

LOG_DIR = EXTERNAL_DIR / "logs"
LOG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LOG_PATH = LOG_DIR / "obs_setting.log"

DEFAULT_IP = "192.168.1.100"

# Actual OBS protocol ports.
DEFAULT_COMMAND_PORT = 54300
DEFAULT_DATA_PORT = 54301

# Decimation parameter = number of consecutive RAW ADC frames averaged
# into one output ADC frame.
DEFAULT_DECIMATION_SAMPLES = 5

# Derived nominal result rate; display/compatibility only.
DEFAULT_DECIMATION_RATE_HZ = (
    float(RAW_ADC_SAMPLE_RATE_HZ)
    / float(DEFAULT_DECIMATION_SAMPLES)
)

DEFAULT_GNSS_COM = "COM3"
DEFAULT_GNSS_BAUD = 115200

DEFAULT_USBL_MODE = "COM Port"
DEFAULT_USBL_COM = "COM4"
DEFAULT_USBL_BAUD = 115200

DEFAULT_USBL_UDP_IP = "0.0.0.0"
DEFAULT_USBL_UDP_PORT = 10110

DEFAULT_RECORD_FOLDER = str(
    EXTERNAL_DIR / "recordings"
)

OBS_COMMAND_MAX_SENTENCE_BYTES = 82

BULK_MAGIC = b"OBS:"
BULK_HEADER_STRUCT = struct.Struct("<4sII")
BULK_HEADER_SIZE = BULK_HEADER_STRUCT.size
assert BULK_HEADER_SIZE == 12

CURRENT_BULK_PAYLOAD_BYTES = 2048
MAX_BULK_PAYLOAD_BYTES = 1024 * 1024

ADC_CHANNEL_COUNT = 4
ADC_WORD_BYTES = 4
ADC_FRAME_BYTES = (
    ADC_CHANNEL_COUNT
    * ADC_WORD_BYTES
)

ADC24_MIN = -(1 << 23)
ADC24_MAX = (1 << 23) - 1


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(threadName)s | "
        "%(message)s"
    ),
)

LOGGER = logging.getLogger(
    "obs_setting"
)


# =============================================================================
# Small helper functions
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


def available_serial_ports() -> list[str]:
    """
    Enumerate serial ports when pyserial is installed.

    If pyserial is not installed, keep editable fallback names so the settings
    window itself remains usable.
    """

    try:
        from serial.tools import list_ports

        ports = sorted(
            item.device
            for item in list_ports.comports()
            if item.device
        )

        if ports:
            return ports

    except Exception:
        pass

    if os.name == "nt":
        return [
            f"COM{i}"
            for i in range(
                1,
                21,
            )
        ]

    return [
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
        "/dev/ttyACM0",
        "/dev/ttyACM1",
    ]


def nmea_xor_checksum(
    body: str,
) -> int:
    checksum = 0

    for byte_value in body.encode(
        "ascii",
        errors="strict",
    ):
        checksum ^= byte_value

    return checksum


def build_obs_sentence(
    body: str,
) -> bytes:
    """
    Convert a configured body such as:
        SOMEID,1,2

    into:
        $SOMEID,1,2*CC\r\n
    """

    body = str(
        body
    ).strip()

    if body.startswith("$"):
        body = body[1:]

    if "*" in body:
        body = body.split(
            "*",
            1,
        )[0]

    body = body.strip()

    if not body:
        raise ValueError(
            "OBS command body is empty."
        )

    checksum = nmea_xor_checksum(
        body
    )

    sentence = (
        f"${body}*{checksum:02X}\r\n"
    ).encode(
        "ascii",
        errors="strict",
    )

    if len(sentence) > OBS_COMMAND_MAX_SENTENCE_BYTES:
        raise ValueError(
            "OBS command exceeds the 82-byte protocol limit."
        )

    return sentence


def parse_obs_sentence(
    sentence: str,
) -> Optional[dict]:
    """
    Strict parser for the OBS command/telemetry TCP channel.

    Returns:
        {
            "id": "AHRS2",
            "fields": [...],
            "raw": "$AHRS2,...*CC",
            "timestamp_ns": ...
        }

    Invalid checksum/format returns None.
    """

    text = sentence.strip(
        "\r\n "
    )

    if not text.startswith("$"):
        return None

    try:
        encoded = text.encode(
            "ascii",
            errors="strict",
        )
    except UnicodeEncodeError:
        return None

    # Include CR/LF in the protocol maximum even though they were stripped.
    if (
        len(encoded) + 2
        > OBS_COMMAND_MAX_SENTENCE_BYTES
    ):
        return None

    star_index = text.rfind("*")

    if star_index <= 1:
        return None

    body = text[
        1:star_index
    ]

    checksum_text = text[
        star_index + 1:
        star_index + 3
    ]

    if len(checksum_text) != 2:
        return None

    try:
        expected_checksum = int(
            checksum_text,
            16,
        )
    except ValueError:
        return None

    calculated_checksum = (
        nmea_xor_checksum(
            body
        )
    )

    if (
        calculated_checksum
        != expected_checksum
    ):
        return None

    parts = body.split(
        ","
    )

    if not parts:
        return None

    message_id = (
        parts[0]
        .strip()
        .upper()
    )

    if not message_id:
        return None

    return {
        "id": message_id,
        "fields": parts[1:],
        "raw": text,
        "timestamp_ns": time.time_ns(),
    }


def nmea_checksum_valid(
    sentence: str,
) -> bool:
    """
    NMEA validation for GNSS/USBL input.

    Some external GNSS/USBL units can be configured without a checksum.
    Those sentences are accepted. When *CC is present it must be valid.
    """

    text = sentence.strip()

    if not text.startswith("$"):
        return False

    if "*" not in text:
        return True

    body = text[
        1:
        text.rfind("*")
    ]

    checksum_text = text[
        text.rfind("*") + 1:
        text.rfind("*") + 3
    ]

    try:
        expected = int(
            checksum_text,
            16,
        )
    except ValueError:
        return False

    try:
        calculated = (
            nmea_xor_checksum(
                body
            )
        )
    except UnicodeEncodeError:
        return False

    return (
        calculated
        == expected
    )


def nmea_coordinate(
    value: str,
    hemisphere: str,
) -> float:
    raw = float(
        value
    )

    degrees = int(
        raw // 100
    )

    minutes = (
        raw
        - degrees * 100
    )

    decimal = (
        degrees
        + minutes / 60.0
    )

    hemisphere = (
        hemisphere
        .strip()
        .upper()
    )

    if hemisphere in (
        "S",
        "W",
    ):
        decimal = -decimal

    return decimal


def parse_nmea_gga(
    sentence: str,
) -> Optional[dict]:

    text = sentence.strip()

    if not nmea_checksum_valid(
        text
    ):
        return None

    payload = text.split(
        "*",
        1,
    )[0]

    fields = payload.split(
        ","
    )

    if len(fields) < 10:
        return None

    message_id = (
        fields[0]
        .lstrip("$")
        .upper()
    )

    if not message_id.endswith(
        "GGA"
    ):
        return None

    try:
        latitude = nmea_coordinate(
            fields[2],
            fields[3],
        )

        longitude = nmea_coordinate(
            fields[4],
            fields[5],
        )

        fix_quality = int(
            fields[6]
            or 0
        )

        satellites = int(
            fields[7]
            or 0
        )

        hdop = float(
            fields[8]
            or 0.0
        )

        altitude = float(
            fields[9]
            or 0.0
        )

    except (
        ValueError,
        TypeError,
        IndexError,
    ):
        return None

    return {
        "timestamp_ns": time.time_ns(),
        "utc": fields[1],
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "fix_quality": fix_quality,
        "satellites": satellites,
        "hdop": hdop,
        "valid": fix_quality > 0,
        "raw": text,
    }


def parse_int_auto(
    value: str,
) -> int:
    """
    Parse decimal, 0x-prefixed hex, or bare hex containing A-F.
    """

    text = str(
        value
    ).strip()

    if not text:
        return 0

    if text.lower().startswith(
        "0x"
    ):
        return int(
            text,
            16,
        )

    if any(
        char in "ABCDEFabcdef"
        for char in text
    ):
        return int(
            text,
            16,
        )

    return int(
        text,
        10,
    )


def decode_signed_adc24(
    word: int,
) -> int:
    raw = int(
        word
    ) & 0x00FFFFFF

    if raw & 0x00800000:
        raw -= 0x01000000

    return raw



# =============================================================================
# ADC block-average decimator
# =============================================================================

class ADCAveragingDecimator:
    """
    Continuous block-average decimator for the 4-channel OBS ADC stream.

    User parameter:
        averaging_samples = number of consecutive RAW ADC frames averaged
        into one OUTPUT frame.

    For averaging_samples = 5:
        raw 1000 Hz/channel -> output 200 Hz/channel.

    CH0 / CH1 / CH2 are Geophone X / Y / Z.
    CH3 uses the same averaging window to keep each shared ADC frame aligned.

    The averaging state persists across normal 128-frame OBS payload
    boundaries. A real source-data gap or source restart resets only the
    incomplete averaging window, so samples on opposite sides of a gap are
    never averaged together.

    Output timestamps are the center timestamps of the raw averaging windows.
    """

    def __init__(
        self,
        averaging_samples: int,
    ):
        self.averaging_samples = max(
            1,
            int(
                averaging_samples
            ),
        )

        self.reset()

    @property
    def result_rate_hz(
        self,
    ) -> float:
        return (
            float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            / float(
                self.averaging_samples
            )
        )

    def reset(
        self,
    ) -> None:
        self._sum = [
            0,
            0,
            0,
            0,
        ]

        self._status_or = [
            0,
            0,
            0,
            0,
        ]

        self._count = 0
        self._first_timestamp_ns = None

    def break_stream(
        self,
    ) -> None:
        """
        Discard an incomplete averaging window at a real acquisition gap.
        """
        self.reset()

    @staticmethod
    def _clamp_adc24(
        value: float,
    ) -> int:
        result = int(
            round(
                float(
                    value
                )
            )
        )

        return max(
            ADC24_MIN,
            min(
                ADC24_MAX,
                result,
            ),
        )

    def process(
        self,
        samples,
        statuses,
        raw_timestamps_ns,
    ):
        """
        Returns:
            output_samples
            output_statuses
            output_timestamps_ns
        """

        if not (
            len(samples)
            == len(statuses)
            == len(raw_timestamps_ns)
        ):
            raise ValueError(
                "samples/statuses/timestamps length mismatch"
            )

        output_samples = []
        output_statuses = []
        output_timestamps = []

        for (
            sample,
            status,
            timestamp_ns,
        ) in zip(
            samples,
            statuses,
            raw_timestamps_ns,
        ):
            if self._count == 0:
                self._first_timestamp_ns = int(
                    timestamp_ns
                )

            for channel_index in range(
                ADC_CHANNEL_COUNT
            ):
                self._sum[
                    channel_index
                ] += int(
                    sample[
                        channel_index
                    ]
                )

                # Preserve any diagnostic flag that appeared in the averaging
                # window. Channel-ID bits are rebuilt below.
                self._status_or[
                    channel_index
                ] |= (
                    int(
                        status[
                            channel_index
                        ]
                    )
                    & (
                        ~ADC_STATUS_CHANNEL_ID_MASK
                        & 0xFF
                    )
                )

            self._count += 1

            if (
                self._count
                < self.averaging_samples
            ):
                continue

            averaged = tuple(
                self._clamp_adc24(
                    self._sum[
                        channel_index
                    ]
                    / float(
                        self.averaging_samples
                    )
                )
                for channel_index in range(
                    ADC_CHANNEL_COUNT
                )
            )

            aggregated_status = tuple(
                (
                    self._status_or[
                        channel_index
                    ]
                    | (
                        channel_index
                        & ADC_STATUS_CHANNEL_ID_MASK
                    )
                )
                & 0xFF
                for channel_index in range(
                    ADC_CHANNEL_COUNT
                )
            )

            last_timestamp_ns = int(
                timestamp_ns
            )

            center_timestamp_ns = (
                int(
                    self._first_timestamp_ns
                )
                + last_timestamp_ns
            ) // 2

            output_samples.append(
                averaged
            )
            output_statuses.append(
                aggregated_status
            )
            output_timestamps.append(
                center_timestamp_ns
            )

            self.reset()

        return (
            output_samples,
            output_statuses,
            output_timestamps,
        )


# =============================================================================
# DATA TCP receiver
# =============================================================================

class BulkFrameStreamParser:
    """
    Stateful parser for the TCP bulk byte stream.

    Important TCP rule:
        one recv() call is NOT one OBS frame.

    recv() may contain:
    - part of one OBS frame,
    - exactly one OBS frame,
    - several OBS frames.

    Parsing therefore follows this exact state machine:

        recv()
          -> append to RX buffer
          -> find b"OBS:"
          -> wait for 12-byte header
          -> read payload_length
          -> wait for 12 + payload_length bytes
          -> extract exactly one frame
          -> leave remaining bytes in RX buffer
          -> repeat

    Current firmware payload length is strictly 2048 bytes, therefore a normal
    complete frame is 2060 bytes.
    """

    def __init__(
        self,
        *,
        expected_payload_length: int = CURRENT_BULK_PAYLOAD_BYTES,
    ):
        self.expected_payload_length = int(
            expected_payload_length
        )

        self.buffer = bytearray()

        self.discarded_bytes = 0
        self.resync_events = 0
        self.invalid_headers = 0

    def clear(
        self,
    ) -> None:

        self.buffer.clear()

    def feed(
        self,
        chunk: bytes,
    ) -> list[
        tuple[int, bytes]
    ]:
        """
        Append arbitrary TCP bytes and return every complete OBS frame that can
        currently be extracted.

        Returned tuples:
            (uint32 frame_sequence, payload_bytes)
        """

        if chunk:
            self.buffer.extend(
                chunk
            )

        complete_frames: list[
            tuple[int, bytes]
        ] = []

        while True:

            # -------------------------------------------------------------
            # 1. Find the OBS: magic.
            # -------------------------------------------------------------
            magic_index = self.buffer.find(
                BULK_MAGIC
            )

            if magic_index < 0:
                # No full magic is currently present. Keep only the final
                # three bytes because they may be the prefix of "OBS:" split
                # across the next recv().
                keep = min(
                    len(self.buffer),
                    len(BULK_MAGIC) - 1,
                )

                discard_count = (
                    len(self.buffer)
                    - keep
                )

                if discard_count > 0:
                    del self.buffer[
                        :discard_count
                    ]

                    self.discarded_bytes += (
                        discard_count
                    )

                    self.resync_events += 1

                break

            if magic_index > 0:
                # Remove only bytes before the first complete OBS: magic.
                del self.buffer[
                    :magic_index
                ]

                self.discarded_bytes += (
                    magic_index
                )

                self.resync_events += 1

            # -------------------------------------------------------------
            # 2. Do we already have the complete 12-byte header?
            # -------------------------------------------------------------
            if (
                len(self.buffer)
                < BULK_HEADER_SIZE
            ):
                break

            (
                magic,
                frame_sequence,
                payload_length,
            ) = BULK_HEADER_STRUCT.unpack_from(
                self.buffer,
                0,
            )

            if magic != BULK_MAGIC:
                # Defensive fallback. This should not normally happen because
                # the buffer was aligned above.
                del self.buffer[0]

                self.discarded_bytes += 1
                self.resync_events += 1
                continue

            # -------------------------------------------------------------
            # 3. Validate payload_length before trusting the frame size.
            # -------------------------------------------------------------
            if (
                payload_length
                != self.expected_payload_length
            ):
                # Current OBS firmware specifies a fixed 2048-byte payload.
                # If this header is corrupt, do not skip payload_length bytes
                # because that could discard valid following frames.
                # Advance one byte and search for OBS: again.
                del self.buffer[0]

                self.discarded_bytes += 1
                self.invalid_headers += 1
                self.resync_events += 1
                continue

            complete_length = (
                BULK_HEADER_SIZE
                + payload_length
            )

            # -------------------------------------------------------------
            # 4. Wait until the entire 2060-byte frame is available.
            # -------------------------------------------------------------
            if (
                len(self.buffer)
                < complete_length
            ):
                break

            # -------------------------------------------------------------
            # 5. Extract exactly one frame, leaving following TCP data intact.
            # -------------------------------------------------------------
            frame = bytes(
                self.buffer[
                    :complete_length
                ]
            )

            del self.buffer[
                :complete_length
            ]

            # Parse again from the isolated frame rather than relying on a
            # changing bytearray.
            (
                isolated_magic,
                isolated_sequence,
                isolated_payload_length,
            ) = BULK_HEADER_STRUCT.unpack_from(
                frame,
                0,
            )

            if (
                isolated_magic != BULK_MAGIC
                or isolated_payload_length
                != self.expected_payload_length
            ):
                self.invalid_headers += 1
                self.resync_events += 1
                continue

            payload = frame[
                BULK_HEADER_SIZE:
            ]

            if (
                len(payload)
                != self.expected_payload_length
            ):
                self.invalid_headers += 1
                continue

            complete_frames.append(
                (
                    int(
                        isolated_sequence
                    ),
                    payload,
                )
            )

        return complete_frames


class OBSDataTCPThread(QThread):
    """
    Robust OBS bulk binary receiver for TCP port 54301.

    The receiver never assumes recv() boundaries correspond to OBS frames.
    """

    connection_changed = Signal(
        bool,
        str,
    )

    socket_error = Signal(
        str
    )

    # measured raw ADC frame rate,
    # measured output/shared ADC frame rate,
    # dropped bulk frames,
    # channel-ID mismatches,
    # ADC ERROR words,
    # malformed/resync count
    stream_status = Signal(
        float,
        float,
        int,
        int,
        int,
        int,
    )

    def __init__(
        self,
        host: str,
        port: int,
        shared: OBSSharedData,
        decimation_samples: int,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.host = str(
            host
        )

        self.port = int(
            port
        )

        self.shared = shared

        self.decimation_samples = max(
            1,
            int(
                decimation_samples
            ),
        )

        self.decimation_mode = (
            "raw"
            if self.decimation_samples == 1
            else "mean"
        )

        self._decimator = (
            ADCAveragingDecimator(
                self.decimation_samples
            )
        )

        self._raw_interval_ns = int(
            round(
                1_000_000_000.0
                / float(
                    RAW_ADC_SAMPLE_RATE_HZ
                )
            )
        )

        self._last_raw_timestamp_ns = None

        self._stop_event = (
            threading.Event()
        )

        self._socket: Optional[
            socket.socket
        ] = None

        self._last_frame_sequence: Optional[
            int
        ] = None

        self._rate_window_start = (
            time.perf_counter()
        )

        self._rate_window_adc_frames = 0
        self._rate_window_output_frames = 0

        self._parser = (
            BulkFrameStreamParser(
                expected_payload_length=(
                    CURRENT_BULK_PAYLOAD_BYTES
                )
            )
        )

        self._reported_parser_errors = 0

    def stop(
        self,
    ) -> None:

        self._stop_event.set()

        sock = self._socket

        if sock is not None:
            try:
                sock.shutdown(
                    socket.SHUT_RDWR
                )
            except Exception:
                pass

            try:
                sock.close()
            except Exception:
                pass

    def _frame_sequence_delta(
        self,
        current: int,
    ) -> tuple[
        int,
        int,
    ]:
        """
        Returns:
            (dropped_bulk_frames, sequence_reset_count)
        """

        current = (
            int(current)
            & 0xFFFFFFFF
        )

        previous = (
            self._last_frame_sequence
        )

        self._last_frame_sequence = (
            current
        )

        if previous is None:
            return 0, 0

        expected = (
            previous + 1
        ) & 0xFFFFFFFF

        if current == expected:
            return 0, 0

        delta = (
            current - expected
        ) & 0xFFFFFFFF

        # A small forward jump is a real missing-frame count.
        # A backward/very large jump is treated as OBS restart/reset.
        if (
            0 < delta < 1_000_000
        ):
            return int(delta), 0

        return 0, 1

    @staticmethod
    def _decode_payload(
        payload: bytes,
    ) -> tuple[
        list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ],
        list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ],
        int,
        int,
        int,
        int,
        int,
    ]:
        """
        Parse the fixed 2048-byte payload.

        Returns:
            samples
            statuses
            channel_mismatches
            error_words
            unsettled_words
            repeated_words
            saturated_words
        """

        if (
            len(payload)
            != CURRENT_BULK_PAYLOAD_BYTES
        ):
            raise ValueError(
                (
                    "Bulk payload must be exactly "
                    f"{CURRENT_BULK_PAYLOAD_BYTES} bytes, "
                    f"got {len(payload)}."
                )
            )

        word_count = (
            len(payload)
            // ADC_WORD_BYTES
        )

        if word_count != 512:
            raise ValueError(
                (
                    "Expected 512 uint32 ADC words, "
                    f"got {word_count}."
                )
            )

        words = struct.unpack(
            "<512I",
            payload,
        )

        adc_frame_count = (
            word_count
            // ADC_CHANNEL_COUNT
        )

        if adc_frame_count != 128:
            raise ValueError(
                (
                    "Expected 128 ADC frames, "
                    f"got {adc_frame_count}."
                )
            )

        samples: list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ] = []

        statuses: list[
            tuple[
                int,
                int,
                int,
                int,
            ]
        ] = []

        channel_mismatches = 0
        error_words = 0
        unsettled_words = 0
        repeated_words = 0
        saturated_words = 0

        # The payload order is authoritative:
        # CH0, CH1, CH2, CH3, CH0, CH1, ...
        for adc_frame_index in range(
            128
        ):
            base_word = (
                adc_frame_index
                * ADC_CHANNEL_COUNT
            )

            frame_values = [
                0,
                0,
                0,
                0,
            ]

            frame_status = [
                0,
                0,
                0,
                0,
            ]

            for channel_index in range(
                ADC_CHANNEL_COUNT
            ):
                word = words[
                    base_word
                    + channel_index
                ]

                status = (
                    word >> 24
                ) & 0xFF

                raw24 = (
                    word
                    & 0x00FFFFFF
                )

                if (
                    raw24
                    & 0x00800000
                ):
                    raw24 -= (
                        0x01000000
                    )

                frame_values[
                    channel_index
                ] = int(
                    raw24
                )

                frame_status[
                    channel_index
                ] = int(
                    status
                )

                reported_channel = (
                    status
                    & ADC_STATUS_CHANNEL_ID_MASK
                )

                if (
                    reported_channel
                    != channel_index
                ):
                    channel_mismatches += 1

                if (
                    status
                    & ADC_STATUS_ERROR
                ):
                    error_words += 1

                if (
                    status
                    & ADC_STATUS_FILTER_NOT_SETTLED
                ):
                    unsettled_words += 1

                if (
                    status
                    & ADC_STATUS_REPEATED
                ):
                    repeated_words += 1

                if (
                    status
                    & ADC_STATUS_SATURATED
                ):
                    saturated_words += 1

            samples.append(
                (
                    frame_values[0],
                    frame_values[1],
                    frame_values[2],
                    frame_values[3],
                )
            )

            statuses.append(
                (
                    frame_status[0],
                    frame_status[1],
                    frame_status[2],
                    frame_status[3],
                )
            )

        return (
            samples,
            statuses,
            channel_mismatches,
            error_words,
            unsettled_words,
            repeated_words,
            saturated_words,
        )

    def _make_raw_timestamps(
        self,
        *,
        frame_count: int,
        receive_timestamp_ns: int,
        missing_raw_frames_before: int = 0,
    ) -> list[int]:
        """
        Create source-time timestamps for one decoded raw ADC block.

        First block:
            anchor final raw frame to receive_timestamp_ns.

        Following blocks:
            continue from the previous RAW ADC timestamp using the physical
            1000-Hz source period. Real dropped source frames create a real
            timestamp gap.
        """

        frame_count = max(
            0,
            int(
                frame_count
            ),
        )

        if frame_count <= 0:
            return []

        missing_raw_frames_before = max(
            0,
            int(
                missing_raw_frames_before
            ),
        )

        if self._last_raw_timestamp_ns is None:
            first_timestamp_ns = (
                int(
                    receive_timestamp_ns
                )
                - (
                    frame_count - 1
                )
                * self._raw_interval_ns
            )
        else:
            first_timestamp_ns = (
                int(
                    self._last_raw_timestamp_ns
                )
                + (
                    missing_raw_frames_before + 1
                )
                * self._raw_interval_ns
            )

        timestamps = [
            (
                first_timestamp_ns
                + index
                * self._raw_interval_ns
            )
            for index in range(
                frame_count
            )
        ]

        self._last_raw_timestamp_ns = (
            timestamps[
                -1
            ]
        )

        return timestamps

    def _process_bulk_frame(
        self,
        frame_sequence: int,
        payload: bytes,
        receive_timestamp_ns: int,
    ) -> None:

        try:
            (
                samples,
                statuses,
                channel_mismatches,
                error_words,
                unsettled_words,
                repeated_words,
                saturated_words,
            ) = self._decode_payload(
                payload
            )

        except (
            ValueError,
            struct.error,
        ):
            self.shared.update_bulk_status(
                frame_sequence=frame_sequence,
                payload_length=len(
                    payload
                ),
                malformed_frames_add=1,
                timestamp_ns=receive_timestamp_ns,
            )
            return

        dropped_frames, sequence_resets = (
            self._frame_sequence_delta(
                frame_sequence
            )
        )

        if sequence_resets:
            # New/restarted source sequence: do not mix the old acquisition
            # session, raw clock or incomplete averaging window with the new
            # source stream.
            self.shared.start_new_adc_session(
                reset_bulk_status=False,
                raw_sample_rate_hz=(
                    RAW_ADC_SAMPLE_RATE_HZ
                ),
                decimation_samples=(
                    self.decimation_samples
                ),
                decimation_mode=(
                    self.decimation_mode
                ),
            )

            self._last_raw_timestamp_ns = None
            self._decimator.reset()

        missing_adc_frames = (
            dropped_frames
            * 128
        )

        if (
            missing_adc_frames > 0
            and not sequence_resets
        ):
            # Never average samples across a genuine raw-stream gap.
            self._decimator.break_stream()

        raw_timestamps_ns = (
            self._make_raw_timestamps(
                frame_count=len(
                    samples
                ),
                receive_timestamp_ns=(
                    receive_timestamp_ns
                ),
                missing_raw_frames_before=(
                    missing_adc_frames
                ),
            )
        )

        (
            output_samples,
            output_statuses,
            output_timestamps_ns,
        ) = self._decimator.process(
            samples,
            statuses,
            raw_timestamps_ns,
        )

        if output_samples:
            # shared_data_v5 stores the processed/effective stream directly.
            # No D-1 fake missing samples are inserted. Explicit timestamps are
            # the centers of the raw averaging windows.
            self.shared.write_adc_stream_block(
                output_samples,
                statuses=(
                    output_statuses
                ),
                timestamps_ns=(
                    output_timestamps_ns
                ),
            )

        self.shared.update_bulk_status(
            frame_sequence=frame_sequence,
            payload_length=len(
                payload
            ),
            frames_received_add=1,
            dropped_frames_add=dropped_frames,
            sequence_resets_add=sequence_resets,
            channel_id_mismatches_add=channel_mismatches,
            error_flag_words_add=error_words,
            filter_not_settled_words_add=unsettled_words,
            repeated_words_add=repeated_words,
            saturated_words_add=saturated_words,
            timestamp_ns=receive_timestamp_ns,
        )

        self._rate_window_adc_frames += (
            len(samples)
        )

        self._rate_window_output_frames += (
            len(
                output_samples
            )
        )

        now = (
            time.perf_counter()
        )

        elapsed = (
            now
            - self._rate_window_start
        )

        if elapsed >= 0.5:
            raw_rate_hz = (
                self._rate_window_adc_frames
                / elapsed
            )

            output_rate_hz = (
                self._rate_window_output_frames
                / elapsed
            )

            bulk = (
                self.shared.read_bulk_status()
            )

            parser_errors = (
                self._parser.resync_events
                + self._parser.invalid_headers
            )

            self.stream_status.emit(
                float(
                    raw_rate_hz
                ),
                float(
                    output_rate_hz
                ),
                int(
                    bulk.dropped_frames
                ),
                int(
                    bulk.channel_id_mismatches
                ),
                int(
                    bulk.error_flag_words
                ),
                int(
                    parser_errors
                ),
            )

            self._rate_window_start = now
            self._rate_window_adc_frames = 0
            self._rate_window_output_frames = 0

    def run(
        self,
    ) -> None:

        sock: Optional[
            socket.socket
        ] = None

        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            )

            self._socket = sock

            sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )

            # A larger receive buffer reduces avoidable pressure while still
            # relying entirely on the explicit application RX bytearray.
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_RCVBUF,
                    256 * 1024,
                )
            except OSError:
                pass

            sock.settimeout(
                3.0
            )

            sock.connect(
                (
                    self.host,
                    self.port,
                )
            )

            sock.settimeout(
                0.5
            )

            self._parser.clear()
            self._last_frame_sequence = None
            self._rate_window_start = (
                time.perf_counter()
            )
            self._rate_window_adc_frames = 0
            self._rate_window_output_frames = 0
            self._last_raw_timestamp_ns = None
            self._decimator.reset()

            self.shared.update_telemetry(
                data_connected=True
            )

            self.connection_changed.emit(
                True,
                f"{self.host}:{self.port}",
            )

            LOGGER.info(
                (
                    "Bulk DATA connected to %s:%d; "
                    "framing=OBS:+uint32 seq+uint32 len+2048 payload; "
                    "ADC average N=%d; effective nominal rate=%.3f Hz"
                ),
                self.host,
                self.port,
                self.decimation_samples,
                self._decimator.result_rate_hz,
            )

            while not self._stop_event.is_set():

                try:
                    chunk = sock.recv(
                        64 * 1024
                    )

                except socket.timeout:
                    continue

                if not chunk:
                    raise ConnectionResetError(
                        "OBS closed bulk DATA connection."
                    )

                # ---------------------------------------------------------
                # The parser owns all framing state.
                #
                # recv()
                #   -> append RX buffer
                #   -> find OBS:
                #   -> wait header
                #   -> read payload_length
                #   -> wait 12 + payload_length
                #   -> extract frame
                #   -> leave remaining bytes
                # ---------------------------------------------------------
                resync_before = (
                    self._parser.resync_events
                )

                frames = self._parser.feed(
                    chunk
                )

                resync_after = (
                    self._parser.resync_events
                )

                new_resync_events = (
                    resync_after
                    - resync_before
                )

                if new_resync_events > 0:
                    self.shared.update_bulk_status(
                        malformed_frames_add=(
                            new_resync_events
                        )
                    )

                # One recv() may yield zero, one, or many full OBS frames.
                for (
                    frame_sequence,
                    payload,
                ) in frames:

                    self._process_bulk_frame(
                        frame_sequence=frame_sequence,
                        payload=payload,
                        receive_timestamp_ns=(
                            time.time_ns()
                        ),
                    )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "Bulk DATA error: %s",
                message,
            )

            if not self._stop_event.is_set():
                self.socket_error.emit(
                    message
                )

        finally:
            self.shared.update_telemetry(
                data_connected=False
            )

            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

            self._socket = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )

            LOGGER.info(
                (
                    "Bulk DATA disconnected; "
                    "parser discarded=%d bytes, "
                    "resync=%d, invalid_header=%d"
                ),
                self._parser.discarded_bytes,
                self._parser.resync_events,
                self._parser.invalid_headers,
            )


# =============================================================================
# Command / telemetry TCP receiver
# =============================================================================

class OBSCommandTCPThread(QThread):

    connection_changed = Signal(
        bool,
        str,
    )

    sentence_received = Signal(
        object
    )

    socket_error = Signal(
        str
    )

    protocol_error = Signal(
        str
    )

    def __init__(
        self,
        host: str,
        port: int,
        shared: OBSSharedData,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.host = str(
            host
        )

        self.port = int(
            port
        )

        self.shared = shared

        self._stop_event = (
            threading.Event()
        )

        self._send_queue: queue.Queue[
            bytes
        ] = queue.Queue()

        self._socket: Optional[
            socket.socket
        ] = None

    def send_sentence(
        self,
        payload: bytes,
    ) -> None:

        self._send_queue.put(
            bytes(payload)
        )

    def stop(self) -> None:
        self._stop_event.set()

        sock = self._socket

        if sock is not None:
            try:
                sock.shutdown(
                    socket.SHUT_RDWR
                )
            except Exception:
                pass

            try:
                sock.close()
            except Exception:
                pass

    def run(self) -> None:
        sock: Optional[
            socket.socket
        ] = None

        buffer = bytearray()

        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            )

            self._socket = sock

            sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )

            sock.settimeout(
                3.0
            )

            sock.connect(
                (
                    self.host,
                    self.port,
                )
            )

            sock.settimeout(
                0.20
            )

            self.shared.update_telemetry(
                command_connected=True
            )

            self.connection_changed.emit(
                True,
                f"{self.host}:{self.port}",
            )

            LOGGER.info(
                "COMMAND connected to %s:%d",
                self.host,
                self.port,
            )

            while not self._stop_event.is_set():

                while not self._send_queue.empty():
                    try:
                        payload = (
                            self._send_queue
                            .get_nowait()
                        )
                    except queue.Empty:
                        break

                    sock.sendall(
                        payload
                    )

                    LOGGER.info(
                        "COMMAND TX: %r",
                        payload,
                    )

                try:
                    chunk = sock.recv(
                        4096
                    )

                except socket.timeout:
                    continue

                if not chunk:
                    raise ConnectionResetError(
                        "OBS closed COMMAND connection."
                    )

                buffer.extend(
                    chunk
                )

                while b"\n" in buffer:
                    raw_line, _, remaining = (
                        buffer.partition(
                            b"\n"
                        )
                    )

                    buffer = bytearray(
                        remaining
                    )

                    raw_line = raw_line.rstrip(
                        b"\r"
                    )

                    if not raw_line:
                        continue

                    try:
                        line = raw_line.decode(
                            "ascii",
                            errors="strict",
                        )

                    except UnicodeDecodeError:
                        self.protocol_error.emit(
                            "Non-ASCII command-channel sentence."
                        )
                        continue

                    parsed = parse_obs_sentence(
                        line
                    )

                    if parsed is None:
                        self.protocol_error.emit(
                            (
                                "Invalid command-channel "
                                "sentence/checksum."
                            )
                        )
                        continue

                    self.sentence_received.emit(
                        parsed
                    )

                # NMEA sentences are max 82 bytes including CR/LF.
                # A large buffer without newline indicates corrupt framing.
                if len(buffer) > 256:
                    buffer.clear()

                    self.protocol_error.emit(
                        "Command-channel line exceeded framing limit."
                    )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "COMMAND error: %s",
                message,
            )

            if not self._stop_event.is_set():
                self.socket_error.emit(
                    message
                )

        finally:
            self.shared.update_telemetry(
                command_connected=False
            )

            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

            self._socket = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )

            LOGGER.info(
                "COMMAND disconnected"
            )


# =============================================================================
# GNSS / USBL receiver threads
# =============================================================================

class SerialNMEAThread(QThread):

    connection_changed = Signal(
        bool,
        str,
    )

    gga_received = Signal(
        object
    )

    io_error = Signal(
        str
    )

    def __init__(
        self,
        device_name: str,
        port: str,
        baudrate: int,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.device_name = str(
            device_name
        )

        self.port = str(
            port
        )

        self.baudrate = int(
            baudrate
        )

        self._stop_event = (
            threading.Event()
        )

        self._serial = None

    def stop(self) -> None:
        self._stop_event.set()

        serial_obj = (
            self._serial
        )

        if serial_obj is not None:
            try:
                serial_obj.close()
            except Exception:
                pass

    def run(self) -> None:
        serial_obj = None

        try:
            try:
                import serial

            except ImportError as exc:
                raise RuntimeError(
                    "pyserial is not installed. "
                    "Run: pip install pyserial"
                ) from exc

            serial_obj = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.5,
            )

            self._serial = (
                serial_obj
            )

            self.connection_changed.emit(
                True,
                (
                    f"{self.port} "
                    f"@ {self.baudrate}"
                ),
            )

            LOGGER.info(
                "%s serial connected: %s @ %d",
                self.device_name,
                self.port,
                self.baudrate,
            )

            while not self._stop_event.is_set():

                raw = serial_obj.readline()

                if not raw:
                    continue

                line = raw.decode(
                    "ascii",
                    errors="ignore",
                ).strip()

                if not line:
                    continue

                gga = parse_nmea_gga(
                    line
                )

                if gga is not None:
                    self.gga_received.emit(
                        gga
                    )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "%s serial error: %s",
                self.device_name,
                message,
            )

            if not self._stop_event.is_set():
                self.io_error.emit(
                    message
                )

        finally:
            if serial_obj is not None:
                try:
                    serial_obj.close()
                except Exception:
                    pass

            self._serial = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )


class UDPNMEAThread(QThread):

    connection_changed = Signal(
        bool,
        str,
    )

    gga_received = Signal(
        object
    )

    io_error = Signal(
        str
    )

    def __init__(
        self,
        device_name: str,
        listen_ip: str,
        port: int,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.device_name = str(
            device_name
        )

        self.listen_ip = str(
            listen_ip
        )

        self.port = int(
            port
        )

        self._stop_event = (
            threading.Event()
        )

        self._socket: Optional[
            socket.socket
        ] = None

    def stop(self) -> None:
        self._stop_event.set()

        sock = self._socket

        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def run(self) -> None:
        sock: Optional[
            socket.socket
        ] = None

        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
            )

            self._socket = sock

            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )

            sock.bind(
                (
                    self.listen_ip,
                    self.port,
                )
            )

            sock.settimeout(
                0.5
            )

            self.connection_changed.emit(
                True,
                (
                    f"{self.listen_ip}:"
                    f"{self.port}"
                ),
            )

            LOGGER.info(
                "%s UDP listening on %s:%d",
                self.device_name,
                self.listen_ip,
                self.port,
            )

            while not self._stop_event.is_set():

                try:
                    payload, _sender = (
                        sock.recvfrom(
                            8192
                        )
                    )

                except socket.timeout:
                    continue

                text = payload.decode(
                    "ascii",
                    errors="ignore",
                )

                for line in (
                    text
                    .replace(
                        "\r",
                        "\n",
                    )
                    .split(
                        "\n"
                    )
                ):
                    line = line.strip()

                    if not line:
                        continue

                    gga = parse_nmea_gga(
                        line
                    )

                    if gga is not None:
                        self.gga_received.emit(
                            gga
                        )

        except Exception as exc:
            message = str(
                exc
            )

            LOGGER.warning(
                "%s UDP error: %s",
                self.device_name,
                message,
            )

            if not self._stop_event.is_set():
                self.io_error.emit(
                    message
                )

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

            self._socket = None

            self.connection_changed.emit(
                False,
                "Not connected",
            )


# =============================================================================
# Status indicator
# =============================================================================

class StatusPill(QLabel):

    def __init__(
        self,
        text: str = "",
        parent=None,
    ):
        super().__init__(
            text,
            parent,
        )

        self.setObjectName(
            "statusPill"
        )

        self.setAlignment(
            Qt.AlignCenter
        )

        self.setMinimumHeight(
            28
        )

    def set_state(
        self,
        text: str,
        state: str,
    ) -> None:

        state = (
            state
            .strip()
            .lower()
        )

        if state == "good":
            bg = "#123A2D"
            border = "#2D8E66"
            fg = "#A9F1D2"

        elif state == "warning":
            bg = "#403510"
            border = "#A88821"
            fg = "#FFE49A"

        elif state == "active":
            bg = "#102E42"
            border = "#2C86B8"
            fg = "#A8DDF7"

        else:
            bg = "#3A1D20"
            border = "#91424A"
            fg = "#FFBEC4"

        self.setText(
            text
        )

        self.setStyleSheet(
            f"""
            QLabel#statusPill {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
                color: {fg};
                font-weight: 700;
                padding: 3px 8px;
            }}
            """
        )


# =============================================================================
# Main window
# =============================================================================

class OBSSettingWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.shared = OBSSharedData()

        self.data_thread: Optional[
            OBSDataTCPThread
        ] = None

        self.command_thread: Optional[
            OBSCommandTCPThread
        ] = None

        self.gnss_thread: Optional[
            SerialNMEAThread
        ] = None

        self.usbl_thread: Optional[
            QThread
        ] = None

        self.data_connected = False
        self.command_connected = False
        self.gnss_connected = False
        self.usbl_connected = False

        self.gimbal_locked = True
        self.power_mode = "NORMAL"

        self.command_templates = {
            "gimbal_lock": "",
            "gimbal_unlock": "",
            "power_low": "",
            "power_normal": "",
        }

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

        # Slim vertical window for side-by-side use with monitoring windows.
        self.resize(
            505,
            950,
        )

        self.setMinimumSize(
            445,
            700,
        )

        self.setMaximumWidth(
            700
        )

        self._build_ui()
        self._apply_style()

        self.status_timer = QTimer(
            self
        )

        self.status_timer.setSingleShot(
            True
        )

        self.status_timer.timeout.connect(
            lambda:
            self.footer_status.setText(
                "Ready"
            )
        )

        self.refresh_com_ports()
        self.load_settings(
            show_message=False
        )

        # Safe startup states.
        self.shared.update_telemetry(
            data_connected=False,
            command_connected=False,
            gnss_connected=False,
            usbl_connected=False,
            gimbal_locked=True,
            power_mode="NORMAL",
        )

        self._set_data_connection_status(
            False
        )

        self._set_command_connection_status(
            False
        )

        self._set_gnss_connection_status(
            False
        )

        self._set_usbl_connection_status(
            False
        )

        self._set_gimbal_status(
            True,
            source="default",
        )

        self._set_power_status(
            "NORMAL",
            source="default",
        )

        self.update_usbl_connection_fields()

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:

        central = QWidget()
        central.setObjectName(
            "centralWidget"
        )

        self.setCentralWidget(
            central
        )

        outer = QVBoxLayout(
            central
        )

        outer.setContentsMargins(
            14,
            14,
            14,
            12,
        )

        outer.setSpacing(
            10
        )

        title = QLabel(
            "OBS SETTING"
        )

        title.setObjectName(
            "headerTitle"
        )

        subtitle = QLabel(
            (
                "OBS Protocol / "
                "GNSS / USBL / Shared RAM"
            )
        )

        subtitle.setObjectName(
            "headerSubtitle"
        )

        outer.addWidget(
            title
        )

        outer.addWidget(
            subtitle
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
            "scrollContent"
        )

        body = QVBoxLayout(
            content
        )

        body.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        body.setSpacing(
            10
        )

        body.addWidget(
            self._build_network_card()
        )

        body.addWidget(
            self._build_geophone_card()
        )

        body.addWidget(
            self._build_gnss_card()
        )

        body.addWidget(
            self._build_usbl_card()
        )

        body.addWidget(
            self._build_recording_card()
        )

        body.addWidget(
            self._build_gimbal_card()
        )

        body.addWidget(
            self._build_power_card()
        )

        body.addWidget(
            self._build_config_card()
        )

        body.addStretch(
            1
        )

        scroll.setWidget(
            content
        )

        outer.addWidget(
            scroll,
            1,
        )

        self.footer_status = QLabel(
            "Ready"
        )

        self.footer_status.setObjectName(
            "footerStatus"
        )

        outer.addWidget(
            self.footer_status
        )

    def _card(
        self,
        title: str,
    ) -> tuple[
        QFrame,
        QVBoxLayout,
    ]:

        frame = QFrame()
        frame.setObjectName(
            "card"
        )

        layout = QVBoxLayout(
            frame
        )

        layout.setContentsMargins(
            12,
            10,
            12,
            12,
        )

        layout.setSpacing(
            8
        )

        title_label = QLabel(
            title
        )

        title_label.setObjectName(
            "cardTitle"
        )

        layout.addWidget(
            title_label
        )

        return frame, layout

    def _build_network_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "OBS TCP Connection"
        )

        form = QFormLayout()
        form.setSpacing(
            7
        )

        self.ip_edit = QLineEdit()

        self.ip_edit.setPlaceholderText(
            DEFAULT_IP
        )

        self.data_port_spin = QSpinBox()

        self.data_port_spin.setRange(
            1,
            65535,
        )

        self.data_port_spin.setValue(
            DEFAULT_DATA_PORT
        )

        data_port_box = QWidget()

        data_port_row = QHBoxLayout(
            data_port_box
        )

        data_port_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        data_port_row.setSpacing(
            7
        )

        self.data_connection_indicator = (
            StatusPill()
        )

        self.data_connection_indicator.setMinimumWidth(
            128
        )

        data_port_row.addWidget(
            self.data_port_spin,
            1,
        )

        data_port_row.addWidget(
            self.data_connection_indicator
        )

        self.command_port_spin = (
            QSpinBox()
        )

        self.command_port_spin.setRange(
            1,
            65535,
        )

        self.command_port_spin.setValue(
            DEFAULT_COMMAND_PORT
        )

        command_port_box = QWidget()

        command_port_row = QHBoxLayout(
            command_port_box
        )

        command_port_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        command_port_row.setSpacing(
            7
        )

        self.command_connection_indicator = (
            StatusPill()
        )

        self.command_connection_indicator.setMinimumWidth(
            128
        )

        command_port_row.addWidget(
            self.command_port_spin,
            1,
        )

        command_port_row.addWidget(
            self.command_connection_indicator
        )

        form.addRow(
            "OBS IP",
            self.ip_edit,
        )

        form.addRow(
            "Data Port",
            data_port_box,
        )

        form.addRow(
            "Command Port",
            command_port_box,
        )

        layout.addLayout(
            form
        )

        self.obs_connect_button = (
            QPushButton(
                "Connect OBS"
            )
        )

        self.obs_connect_button.setObjectName(
            "primaryButton"
        )

        self.obs_connect_button.clicked.connect(
            self.toggle_obs_connection
        )

        layout.addWidget(
            self.obs_connect_button
        )

        self.data_rate_label = QLabel(
            (
                "ADC raw: -- Hz | output: -- Hz | "
                "avg N=-- | drop: -- | sync: -- | error: --"
            )
        )

        self.data_rate_label.setObjectName(
            "detailLabel"
        )

        self.data_rate_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.data_rate_label
        )

        self.command_rx_label = QLabel(
            "Telemetry: waiting for command channel"
        )

        self.command_rx_label.setObjectName(
            "detailLabel"
        )

        self.command_rx_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.command_rx_label
        )

        return card

    def _build_geophone_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "Geophone"
        )

        row = QHBoxLayout()

        label = QLabel(
            "Decimation / Average"
        )

        self.decimation_spin = QSpinBox()

        self.decimation_spin.setRange(
            1,
            int(
                RAW_ADC_SAMPLE_RATE_HZ
            ),
        )

        self.decimation_spin.setSuffix(
            " data"
        )

        self.decimation_spin.setValue(
            DEFAULT_DECIMATION_SAMPLES
        )

        self.decimation_spin.setMinimumWidth(
            120
        )

        self.decimation_spin.valueChanged.connect(
            self.update_decimation_result
        )

        row.addWidget(
            label
        )

        row.addStretch(
            1
        )

        row.addWidget(
            self.decimation_spin
        )

        layout.addLayout(
            row
        )

        self.decimation_result_label = QLabel(
            ""
        )

        self.decimation_result_label.setObjectName(
            "detailLabel"
        )

        self.decimation_result_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.decimation_result_label
        )

        detail = QLabel(
            (
                "N = jumlah data RAW yang dirata-ratakan menjadi 1 data output. "
                "CH0 / CH1 / CH2 = Geophone X / Y / Z. "
                "CH3 mengikuti window N yang sama untuk menjaga sinkronisasi "
                "frame 4-channel. Averaging tetap kontinu melewati boundary "
                "payload OBS 128 frame."
            )
        )

        detail.setObjectName(
            "detailLabel"
        )

        detail.setWordWrap(
            True
        )

        layout.addWidget(
            detail
        )

        self.update_decimation_result()

        return card

    def update_decimation_result(
        self,
        *_args,
    ) -> None:

        averaging_samples = max(
            1,
            int(
                self.decimation_spin.value()
            ),
        )

        result_rate_hz = (
            float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            / float(
                averaging_samples
            )
        )

        self.decimation_result_label.setText(
            (
                f"Raw: {RAW_ADC_SAMPLE_RATE_HZ:g} Hz/channel  |  "
                f"Average N={averaging_samples} data  |  "
                f"Result: {result_rate_hz:,.3f} Hz/channel"
            )
        )

    def _build_gnss_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "GNSS Connection"
        )

        form = QFormLayout()

        self.gnss_com_widget = QWidget()

        com_row = QHBoxLayout(
            self.gnss_com_widget
        )

        com_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        com_row.setSpacing(
            6
        )

        self.gnss_com_combo = QComboBox()
        self.gnss_com_combo.setEditable(
            True
        )

        self.gnss_refresh_button = (
            QPushButton(
                "Refresh"
            )
        )

        self.gnss_refresh_button.setObjectName(
            "smallButton"
        )

        self.gnss_refresh_button.clicked.connect(
            self.refresh_com_ports
        )

        com_row.addWidget(
            self.gnss_com_combo,
            1,
        )

        com_row.addWidget(
            self.gnss_refresh_button
        )

        self.gnss_baud_combo = (
            self._new_baud_combo(
                DEFAULT_GNSS_BAUD
            )
        )

        form.addRow(
            "COM Port",
            self.gnss_com_widget,
        )

        form.addRow(
            "Baudrate",
            self.gnss_baud_combo,
        )

        layout.addLayout(
            form
        )

        row = QHBoxLayout()

        self.gnss_connect_button = (
            QPushButton(
                "Connect GNSS"
            )
        )

        self.gnss_connect_button.setObjectName(
            "primaryButton"
        )

        self.gnss_connect_button.clicked.connect(
            self.toggle_gnss_connection
        )

        self.gnss_connection_indicator = (
            StatusPill()
        )

        self.gnss_connection_indicator.setMinimumWidth(
            128
        )

        row.addWidget(
            self.gnss_connect_button,
            1,
        )

        row.addWidget(
            self.gnss_connection_indicator
        )

        layout.addLayout(
            row
        )

        self.gnss_gga_label = QLabel(
            "GGA: waiting for connection"
        )

        self.gnss_gga_label.setObjectName(
            "detailLabel"
        )

        self.gnss_gga_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.gnss_gga_label
        )

        return card

    def _build_usbl_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "USBL Connection"
        )

        form = QFormLayout()

        self.usbl_mode_combo = QComboBox()

        self.usbl_mode_combo.addItems(
            [
                "COM Port",
                "UDP",
            ]
        )

        self.usbl_mode_combo.currentTextChanged.connect(
            self.update_usbl_connection_fields
        )

        form.addRow(
            "Connection",
            self.usbl_mode_combo,
        )

        self.usbl_com_widget = QWidget()

        com_row = QHBoxLayout(
            self.usbl_com_widget
        )

        com_row.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        com_row.setSpacing(
            6
        )

        self.usbl_com_combo = QComboBox()
        self.usbl_com_combo.setEditable(
            True
        )

        self.usbl_refresh_button = (
            QPushButton(
                "Refresh"
            )
        )

        self.usbl_refresh_button.setObjectName(
            "smallButton"
        )

        self.usbl_refresh_button.clicked.connect(
            self.refresh_com_ports
        )

        com_row.addWidget(
            self.usbl_com_combo,
            1,
        )

        com_row.addWidget(
            self.usbl_refresh_button
        )

        self.usbl_baud_combo = (
            self._new_baud_combo(
                DEFAULT_USBL_BAUD
            )
        )

        self.usbl_udp_ip_edit = QLineEdit()

        self.usbl_udp_ip_edit.setPlaceholderText(
            DEFAULT_USBL_UDP_IP
        )

        self.usbl_udp_port_spin = QSpinBox()

        self.usbl_udp_port_spin.setRange(
            1,
            65535,
        )

        self.usbl_udp_port_spin.setValue(
            DEFAULT_USBL_UDP_PORT
        )

        form.addRow(
            "COM Port",
            self.usbl_com_widget,
        )

        form.addRow(
            "Baudrate",
            self.usbl_baud_combo,
        )

        form.addRow(
            "UDP Listen IP",
            self.usbl_udp_ip_edit,
        )

        form.addRow(
            "UDP Port",
            self.usbl_udp_port_spin,
        )

        layout.addLayout(
            form
        )

        row = QHBoxLayout()

        self.usbl_connect_button = (
            QPushButton(
                "Connect USBL"
            )
        )

        self.usbl_connect_button.setObjectName(
            "primaryButton"
        )

        self.usbl_connect_button.clicked.connect(
            self.toggle_usbl_connection
        )

        self.usbl_connection_indicator = (
            StatusPill()
        )

        self.usbl_connection_indicator.setMinimumWidth(
            128
        )

        row.addWidget(
            self.usbl_connect_button,
            1,
        )

        row.addWidget(
            self.usbl_connection_indicator
        )

        layout.addLayout(
            row
        )

        self.usbl_gga_label = QLabel(
            "GGA: waiting for connection"
        )

        self.usbl_gga_label.setObjectName(
            "detailLabel"
        )

        self.usbl_gga_label.setWordWrap(
            True
        )

        layout.addWidget(
            self.usbl_gga_label
        )

        return card

    def _build_recording_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "MiniSEED Recording"
        )

        row = QHBoxLayout()

        self.record_folder_edit = QLineEdit()

        self.browse_button = QPushButton(
            "Browse"
        )

        self.browse_button.setObjectName(
            "smallButton"
        )

        self.browse_button.clicked.connect(
            self.browse_record_folder
        )

        row.addWidget(
            self.record_folder_edit,
            1,
        )

        row.addWidget(
            self.browse_button
        )

        layout.addLayout(
            row
        )

        return card

    def _build_gimbal_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "Gimbal Control"
        )

        self.gimbal_indicator = StatusPill()

        layout.addWidget(
            self.gimbal_indicator
        )

        row = QHBoxLayout()

        self.lock_gimbal_button = QPushButton(
            "Lock Gimbal"
        )

        self.unlock_gimbal_button = QPushButton(
            "Unlock Gimbal"
        )

        for button in (
            self.lock_gimbal_button,
            self.unlock_gimbal_button,
        ):
            button.setObjectName(
                "controlButton"
            )

        self.lock_gimbal_button.clicked.connect(
            lambda:
            self._send_configured_command(
                "gimbal_lock",
                "Gimbal Lock",
            )
        )

        self.unlock_gimbal_button.clicked.connect(
            lambda:
            self._send_configured_command(
                "gimbal_unlock",
                "Gimbal Unlock",
            )
        )

        row.addWidget(
            self.lock_gimbal_button
        )

        row.addWidget(
            self.unlock_gimbal_button
        )

        layout.addLayout(
            row
        )

        self.gimbal_detail = QLabel()

        self.gimbal_detail.setObjectName(
            "detailLabel"
        )

        self.gimbal_detail.setWordWrap(
            True
        )

        layout.addWidget(
            self.gimbal_detail
        )

        return card

    def _build_power_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "OBS Power Mode"
        )

        self.power_indicator = StatusPill()

        layout.addWidget(
            self.power_indicator
        )

        row = QHBoxLayout()

        self.low_power_button = QPushButton(
            "Low Power"
        )

        self.normal_power_button = QPushButton(
            "Normal Mode"
        )

        for button in (
            self.low_power_button,
            self.normal_power_button,
        ):
            button.setObjectName(
                "controlButton"
            )

        self.low_power_button.clicked.connect(
            lambda:
            self._send_configured_command(
                "power_low",
                "Low Power",
            )
        )

        self.normal_power_button.clicked.connect(
            lambda:
            self._send_configured_command(
                "power_normal",
                "Normal Mode",
            )
        )

        row.addWidget(
            self.low_power_button
        )

        row.addWidget(
            self.normal_power_button
        )

        layout.addLayout(
            row
        )

        self.power_detail = QLabel()

        self.power_detail.setObjectName(
            "detailLabel"
        )

        self.power_detail.setWordWrap(
            True
        )

        layout.addWidget(
            self.power_detail
        )

        return card

    def _build_config_card(
        self,
    ) -> QFrame:

        card, layout = self._card(
            "Configuration File"
        )

        ini_label = QLabel(
            str(INI_PATH)
        )

        ini_label.setObjectName(
            "pathLabel"
        )

        ini_label.setWordWrap(
            True
        )

        layout.addWidget(
            ini_label
        )

        shared_label = QLabel(
            (
                f"Shared RAM: "
                f"{self.shared.name} | "
                f"{self.shared.size / (1024 * 1024):.2f} MB"
            )
        )

        shared_label.setObjectName(
            "pathLabel"
        )

        shared_label.setWordWrap(
            True
        )

        layout.addWidget(
            shared_label
        )

        row = QHBoxLayout()

        self.load_button = QPushButton(
            "Load INI"
        )

        self.load_button.setObjectName(
            "controlButton"
        )

        self.load_button.clicked.connect(
            lambda:
            self.load_settings(
                show_message=True
            )
        )

        self.save_button = QPushButton(
            "Save Settings"
        )

        self.save_button.setObjectName(
            "primaryButton"
        )

        self.save_button.clicked.connect(
            self.save_settings
        )

        row.addWidget(
            self.load_button
        )

        row.addWidget(
            self.save_button
        )

        layout.addLayout(
            row
        )

        return card

    @staticmethod
    def _new_baud_combo(
        default: int,
    ) -> QComboBox:

        combo = QComboBox()

        combo.setEditable(
            True
        )

        combo.addItems(
            [
                "4800",
                "9600",
                "19200",
                "38400",
                "57600",
                "115200",
                "230400",
                "460800",
                "921600",
            ]
        )

        combo.setCurrentText(
            str(default)
        )

        return combo

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
            QWidget#scrollContent {
                background-color: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QScrollArea {
                background: transparent;
                border: none;
            }

            QLabel {
                background: transparent;
                color: #FFFFFF;
            }

            QLabel#headerTitle {
                color: #FFFFFF;
                font-size: 21px;
                font-weight: 800;
                letter-spacing: 1px;
            }

            QLabel#headerSubtitle {
                color: #D3E0E8;
                font-size: 11px;
                padding-bottom: 2px;
            }

            QFrame#card {
                background-color: #0D1E2A;
                border: 1px solid #17374A;
                border-radius: 10px;
            }

            QLabel#cardTitle {
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 800;
                letter-spacing: 0.7px;
            }

            QLabel#detailLabel {
                color: #B6C7D1;
                font-size: 10px;
            }

            QLabel#pathLabel {
                background-color: #091620;
                border: 1px solid #17374A;
                border-radius: 6px;
                color: #B6C7D1;
                font-size: 9px;
                padding: 6px;
            }

            QLabel#footerStatus {
                color: #B6C7D1;
                font-size: 10px;
                padding: 2px;
            }

            QLineEdit,
            QSpinBox,
            QComboBox {
                background-color: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 6px;
                min-height: 28px;
                padding: 2px 7px;
                selection-background-color: #2B739A;
            }

            QLineEdit:focus,
            QSpinBox:focus,
            QComboBox:focus {
                border: 1px solid #4FA5D0;
            }

            QLineEdit:disabled,
            QSpinBox:disabled,
            QComboBox:disabled {
                background-color: #0B151B;
                color: #607784;
                border: 1px solid #1B2B34;
            }

            QComboBox QAbstractItemView {
                background-color: #0B1B26;
                color: #FFFFFF;
                selection-background-color: #245B79;
            }

            QPushButton {
                min-height: 30px;
                border-radius: 7px;
                padding: 3px 10px;
                font-weight: 700;
            }

            QPushButton#primaryButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QPushButton#primaryButton:hover {
                background-color: #1D78A4;
            }

            QPushButton#controlButton,
            QPushButton#smallButton {
                background-color: #132A39;
                color: #FFFFFF;
                border: 1px solid #27526A;
            }

            QPushButton#controlButton:hover,
            QPushButton#smallButton:hover {
                background-color: #18384C;
                border: 1px solid #3C7898;
            }

            QPushButton:disabled {
                background-color: #101B22;
                color: #536873;
                border: 1px solid #1B2B34;
            }
            """
        )

    # -------------------------------------------------------------------------
    # Footer
    # -------------------------------------------------------------------------

    def _set_footer(
        self,
        text: str,
        timeout_ms: int = 4000,
    ) -> None:

        self.footer_status.setText(
            text
        )

        if timeout_ms > 0:
            self.status_timer.start(
                timeout_ms
            )

    # -------------------------------------------------------------------------
    # OBS connection
    # -------------------------------------------------------------------------

    def toggle_obs_connection(
        self,
    ) -> None:

        if (
            self._obs_workers_running()
        ):
            self.disconnect_obs()
        else:
            self.connect_obs()

    def _obs_workers_running(
        self,
    ) -> bool:

        return (
            (
                self.data_thread is not None
                and self.data_thread.isRunning()
            )
            or
            (
                self.command_thread is not None
                and self.command_thread.isRunning()
            )
        )

    def connect_obs(
        self,
    ) -> None:

        host = (
            self.ip_edit
            .text()
            .strip()
        )

        if not host:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "OBS IP address cannot be empty.",
            )
            return

        data_port = int(
            self.data_port_spin.value()
        )

        command_port = int(
            self.command_port_spin.value()
        )

        self.data_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.command_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.obs_connect_button.setText(
            "Disconnect OBS"
        )

        self._set_obs_fields_enabled(
            False
        )

        decimation_samples = max(
            1,
            int(
                self.decimation_spin.value()
            ),
        )

        decimation_mode = (
            "raw"
            if decimation_samples == 1
            else "mean"
        )

        # New physical connection = new v5 acquisition session.
        # The stream metadata becomes the single source of truth for every
        # downstream module.
        stream_info = (
            self.shared.start_new_adc_session(
                reset_bulk_status=True,
                raw_sample_rate_hz=(
                    RAW_ADC_SAMPLE_RATE_HZ
                ),
                decimation_samples=(
                    decimation_samples
                ),
                decimation_mode=(
                    decimation_mode
                ),
            )
        )

        self._set_footer(
            (
                f"ADC stream configured: "
                f"{stream_info.raw_sample_rate_hz:g} Hz / "
                f"N={stream_info.decimation_samples} -> "
                f"{stream_info.effective_sample_rate_hz:.3f} Hz"
            ),
            5000,
        )

        data_worker = OBSDataTCPThread(
            host=host,
            port=data_port,
            shared=self.shared,
            decimation_samples=(
                decimation_samples
            ),
            parent=self,
        )

        command_worker = OBSCommandTCPThread(
            host=host,
            port=command_port,
            shared=self.shared,
            parent=self,
        )

        data_worker.connection_changed.connect(
            self.on_data_connection_changed
        )

        data_worker.socket_error.connect(
            lambda message:
            self._set_footer(
                f"DATA error: {message}",
                7000,
            )
        )

        data_worker.stream_status.connect(
            self.on_stream_status
        )

        data_worker.finished.connect(
            self.on_data_thread_finished
        )

        command_worker.connection_changed.connect(
            self.on_command_connection_changed
        )

        command_worker.sentence_received.connect(
            self.on_obs_sentence
        )

        command_worker.socket_error.connect(
            lambda message:
            self._set_footer(
                f"COMMAND error: {message}",
                7000,
            )
        )

        command_worker.protocol_error.connect(
            lambda message:
            self._set_footer(
                message,
                5000,
            )
        )

        command_worker.finished.connect(
            self.on_command_thread_finished
        )

        self.data_thread = data_worker
        self.command_thread = command_worker

        data_worker.start()
        command_worker.start()

    def disconnect_obs(
        self,
    ) -> None:

        if self.data_thread is not None:
            self.data_thread.stop()

        if self.command_thread is not None:
            self.command_thread.stop()

        self._set_footer(
            "Disconnecting OBS..."
        )

    def _set_obs_fields_enabled(
        self,
        enabled: bool,
    ) -> None:

        self.ip_edit.setEnabled(
            enabled
        )

        self.data_port_spin.setEnabled(
            enabled
        )

        self.command_port_spin.setEnabled(
            enabled
        )

        # N defines the effective shared-stream rate. It cannot change inside
        # one acquisition session.
        self.decimation_spin.setEnabled(
            enabled
        )

    def _set_data_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.data_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            data_connected=self.data_connected
        )

        if connected:
            self.data_connection_indicator.set_state(
                "CONNECTED",
                "good",
            )
        else:
            self.data_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

    def _set_command_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.command_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            command_connected=self.command_connected
        )

        if connected:
            self.command_connection_indicator.set_state(
                "CONNECTED",
                "good",
            )
        else:
            self.command_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

            # Requested no-feedback defaults.
            self._set_gimbal_status(
                True,
                source="default",
            )

            self._set_power_status(
                "NORMAL",
                source="default",
            )

        self._update_command_buttons()

    def on_data_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        self._set_data_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"DATA connected: {detail}"
            )

    def on_command_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        self._set_command_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"COMMAND connected: {detail}"
            )

    def on_data_thread_finished(
        self,
    ) -> None:

        self.data_thread = None

        self._set_data_connection_status(
            False
        )

        self._refresh_obs_button_state()

    def on_command_thread_finished(
        self,
    ) -> None:

        self.command_thread = None

        self._set_command_connection_status(
            False
        )

        self._refresh_obs_button_state()

    def _refresh_obs_button_state(
        self,
    ) -> None:

        if self._obs_workers_running():
            self.obs_connect_button.setText(
                "Disconnect OBS"
            )

            self._set_obs_fields_enabled(
                False
            )

        else:
            self.obs_connect_button.setText(
                "Connect OBS"
            )

            self._set_obs_fields_enabled(
                True
            )

    def on_stream_status(
        self,
        raw_rate_hz: float,
        output_rate_hz: float,
        dropped_frames: int,
        channel_mismatches: int,
        error_words: int,
        parser_resyncs: int,
    ) -> None:

        try:
            stream_info = (
                self.shared.read_adc_stream_info()
            )

            configured_rate = (
                stream_info.effective_sample_rate_hz
            )

            configured_n = (
                stream_info.decimation_samples
            )

        except Exception:
            configured_rate = (
                float(
                    RAW_ADC_SAMPLE_RATE_HZ
                )
                / max(
                    1,
                    int(
                        self.decimation_spin.value()
                    ),
                )
            )

            configured_n = (
                self.decimation_spin.value()
            )

        self.data_rate_label.setText(
            (
                f"ADC raw: {raw_rate_hz:,.1f} Hz | "
                f"output: {output_rate_hz:,.1f} Hz "
                f"(cfg {configured_rate:,.1f}) | "
                f"avg N={configured_n} | "
                f"drop: {dropped_frames} | "
                f"sync: {channel_mismatches} | "
                f"error: {error_words} | "
                f"parser: {parser_resyncs}"
            )
        )

    # -------------------------------------------------------------------------
    # Command-channel telemetry
    # -------------------------------------------------------------------------

    def on_obs_sentence(
        self,
        parsed: dict,
    ) -> None:

        message_id = str(
            parsed.get(
                "id",
                "",
            )
        ).upper()

        fields = list(
            parsed.get(
                "fields",
                [],
            )
        )

        timestamp_ns = int(
            parsed.get(
                "timestamp_ns",
                time.time_ns(),
            )
        )

        try:

            if message_id == "AHRS2":
                if len(fields) < 7:
                    raise ValueError(
                        "AHRS2 requires 7 fields."
                    )

                self.shared.update_telemetry(
                    timestamp_ns=timestamp_ns,
                    roll=float(fields[0]),
                    pitch=float(fields[1]),
                    yaw=float(fields[2]),
                    angular_rate_p=float(fields[3]),
                    angular_rate_q=float(fields[4]),
                    angular_rate_r=float(fields[5]),
                    ahrs_device_id=parse_int_auto(
                        fields[6]
                    ),
                )

                self.command_rx_label.setText(
                    (
                        "AHRS2: "
                        f"R {float(fields[0]):.2f}° | "
                        f"P {float(fields[1]):.2f}° | "
                        f"Y {float(fields[2]):.2f}°"
                    )
                )

            elif message_id == "DEPT0":
                if len(fields) < 3:
                    raise ValueError(
                        "DEPT0 requires 3 fields."
                    )

                self.shared.update_telemetry(
                    timestamp_ns=timestamp_ns,
                    depth=float(fields[0]),
                    depth_rate=float(fields[1]),
                    temperature=float(fields[2]),
                )

                self.command_rx_label.setText(
                    (
                        "DEPT0: "
                        f"depth {float(fields[0]):.3f} | "
                        f"rate {float(fields[1]):.3f} | "
                        f"temp {float(fields[2]):.2f}"
                    )
                )

            elif message_id == "TIME1":
                if len(fields) < 8:
                    raise ValueError(
                        "TIME1 requires 8 fields."
                    )

                values = [
                    int(
                        item,
                        10,
                    )
                    for item in fields[
                        :8
                    ]
                ]

                self.shared.update_device_time(
                    timestamp_ns=timestamp_ns,
                    milliseconds=values[0],
                    year=values[1],
                    month=values[2],
                    week=values[3],
                    date=values[4],
                    hours=values[5],
                    minutes=values[6],
                    seconds=values[7],
                )

                self.command_rx_label.setText(
                    (
                        "TIME1: "
                        f"{values[1]:04d}-"
                        f"{values[2]:02d}-"
                        f"{values[4]:02d} "
                        f"{values[5]:02d}:"
                        f"{values[6]:02d}:"
                        f"{values[7]:02d}"
                    )
                )

            elif message_id == "GDAT2":
                if len(fields) < 11:
                    raise ValueError(
                        (
                            "GDAT2 requires "
                            "10 hex fields + counter."
                        )
                    )

                diagnostic_fields = [
                    int(
                        field.strip(),
                        16,
                    )
                    for field in fields[
                        :10
                    ]
                ]

                counter = parse_int_auto(
                    fields[10]
                )

                self.shared.update_diagnostic(
                    diagnostic_fields,
                    counter=counter,
                    timestamp_ns=timestamp_ns,
                )

                self.command_rx_label.setText(
                    (
                        "GDAT2: "
                        f"counter {counter}"
                    )
                )

            elif message_id == "XCHM0":
                if len(fields) < 2:
                    raise ValueError(
                        "XCHM0 requires unit ID + processor status."
                    )

                unit_id = parse_int_auto(
                    fields[0]
                )

                status_code = parse_int_auto(
                    fields[1]
                )

                self.shared.update_controller_health(
                    unit_id=unit_id,
                    processor_status_code=status_code,
                    timestamp_ns=timestamp_ns,
                )

                self.command_rx_label.setText(
                    (
                        "XCHM0: "
                        f"unit {unit_id} | "
                        f"status 0x{status_code:X}"
                    )
                )

            elif message_id == "XFWVR":
                summary = ",".join(
                    fields
                )

                self.shared.update_firmware_info(
                    summary,
                    timestamp_ns=timestamp_ns,
                )

                self.command_rx_label.setText(
                    (
                        "XFWVR: "
                        f"{summary[:60]}"
                    )
                )

            else:
                self.command_rx_label.setText(
                    (
                        f"{message_id}: "
                        f"{','.join(fields)[:65]}"
                    )
                )

            self._set_footer(
                f"COMMAND RX: ${message_id}"
            )

        except (
            ValueError,
            TypeError,
            IndexError,
        ) as exc:

            LOGGER.warning(
                "Failed parsing %s: %s",
                message_id,
                exc,
            )

            self._set_footer(
                (
                    f"Invalid ${message_id} "
                    f"telemetry: {exc}"
                ),
                6000,
            )

    # -------------------------------------------------------------------------
    # Gimbal / power
    # -------------------------------------------------------------------------

    def _update_command_buttons(
        self,
    ) -> None:

        # Buttons remain available only when the command TCP link exists.
        enabled = (
            self.command_connected
        )

        self.lock_gimbal_button.setEnabled(
            enabled
        )

        self.unlock_gimbal_button.setEnabled(
            enabled
        )

        self.low_power_button.setEnabled(
            enabled
        )

        self.normal_power_button.setEnabled(
            enabled
        )

    def _set_gimbal_status(
        self,
        locked: bool,
        *,
        source: str,
    ) -> None:

        self.gimbal_locked = bool(
            locked
        )

        self.shared.update_telemetry(
            gimbal_locked=self.gimbal_locked
        )

        if locked:
            self.gimbal_indicator.set_state(
                "GIMBAL LOCKED",
                "good",
            )
        else:
            self.gimbal_indicator.set_state(
                "GIMBAL UNLOCKED",
                "warning",
            )

        if source == "feedback":
            detail = (
                "State confirmed by OBS feedback."
            )
        else:
            detail = (
                "Default LOCKED. Supplied protocol does not yet define "
                "the gimbal command/feedback sentence mapping."
            )

        self.gimbal_detail.setText(
            detail
        )

    def _set_power_status(
        self,
        mode: str,
        *,
        source: str,
    ) -> None:

        mode = (
            str(mode)
            .strip()
            .upper()
        )

        if mode not in (
            "LOW",
            "NORMAL",
        ):
            return

        self.power_mode = mode

        self.shared.update_telemetry(
            power_mode=mode
        )

        if mode == "LOW":
            self.power_indicator.set_state(
                "LOW POWER",
                "warning",
            )
        else:
            self.power_indicator.set_state(
                "NORMAL MODE",
                "good",
            )

        if source == "feedback":
            detail = (
                "State confirmed by OBS feedback."
            )
        else:
            detail = (
                "Default NORMAL. Supplied protocol does not yet define "
                "the power command/feedback sentence mapping."
            )

        self.power_detail.setText(
            detail
        )

    def _send_configured_command(
        self,
        key: str,
        description: str,
    ) -> None:

        worker = (
            self.command_thread
        )

        if (
            not self.command_connected
            or worker is None
            or not worker.isRunning()
        ):
            self._set_footer(
                "OBS command port is not connected."
            )
            return

        body = (
            self.command_templates
            .get(
                key,
                "",
            )
            .strip()
        )

        if not body:
            QMessageBox.information(
                self,
                description,
                (
                    "The supplied OBS protocol does not define this "
                    "command sentence yet.\n\n"
                    "Configure the command body in obs_settings.ini "
                    "under [OBS_Commands] after the firmware command ID "
                    "and fields are confirmed."
                ),
            )
            return

        try:
            sentence = (
                build_obs_sentence(
                    body
                )
            )

        except ValueError as exc:
            QMessageBox.warning(
                self,
                description,
                str(exc),
            )
            return

        worker.send_sentence(
            sentence
        )

        self._set_footer(
            (
                f"Sent {description}: "
                f"{sentence.decode('ascii').strip()}"
            )
        )

    # -------------------------------------------------------------------------
    # GNSS
    # -------------------------------------------------------------------------

    def refresh_com_ports(
        self,
    ) -> None:

        gnss_current = ""

        if hasattr(
            self,
            "gnss_com_combo",
        ):
            gnss_current = (
                self.gnss_com_combo
                .currentText()
                .strip()
            )

        usbl_current = ""

        if hasattr(
            self,
            "usbl_com_combo",
        ):
            usbl_current = (
                self.usbl_com_combo
                .currentText()
                .strip()
            )

        ports = available_serial_ports()

        self.gnss_com_combo.blockSignals(
            True
        )

        self.usbl_com_combo.blockSignals(
            True
        )

        self.gnss_com_combo.clear()
        self.usbl_com_combo.clear()

        self.gnss_com_combo.addItems(
            ports
        )

        self.usbl_com_combo.addItems(
            ports
        )

        self.gnss_com_combo.setCurrentText(
            gnss_current
            or DEFAULT_GNSS_COM
        )

        self.usbl_com_combo.setCurrentText(
            usbl_current
            or DEFAULT_USBL_COM
        )

        self.gnss_com_combo.blockSignals(
            False
        )

        self.usbl_com_combo.blockSignals(
            False
        )

        if hasattr(
            self,
            "footer_status",
        ):
            self._set_footer(
                (
                    f"COM ports refreshed "
                    f"({len(ports)})"
                )
            )

    def toggle_gnss_connection(
        self,
    ) -> None:

        if (
            self.gnss_thread is not None
            and self.gnss_thread.isRunning()
        ):
            self.disconnect_gnss()
        else:
            self.connect_gnss()

    def connect_gnss(
        self,
    ) -> None:

        port = (
            self.gnss_com_combo
            .currentText()
            .strip()
        )

        if not port:
            QMessageBox.warning(
                self,
                "GNSS",
                "GNSS COM Port cannot be empty.",
            )
            return

        try:
            baudrate = int(
                self.gnss_baud_combo
                .currentText()
                .strip()
            )

        except ValueError:
            QMessageBox.warning(
                self,
                "GNSS",
                "GNSS baudrate must be an integer.",
            )
            return

        self.gnss_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.gnss_connect_button.setText(
            "Disconnect GNSS"
        )

        self._set_gnss_fields_enabled(
            False
        )

        worker = SerialNMEAThread(
            "GNSS",
            port,
            baudrate,
            self,
        )

        worker.connection_changed.connect(
            self.on_gnss_connection_changed
        )

        worker.gga_received.connect(
            self.on_gnss_gga
        )

        worker.io_error.connect(
            lambda message:
            self._set_footer(
                f"GNSS error: {message}",
                7000,
            )
        )

        worker.finished.connect(
            self.on_gnss_thread_finished
        )

        self.gnss_thread = worker

        worker.start()

    def disconnect_gnss(
        self,
    ) -> None:

        if self.gnss_thread is not None:
            self.gnss_thread.stop()

        self._set_footer(
            "Disconnecting GNSS..."
        )

    def _set_gnss_fields_enabled(
        self,
        enabled: bool,
    ) -> None:

        self.gnss_com_widget.setEnabled(
            enabled
        )

        self.gnss_baud_combo.setEnabled(
            enabled
        )

    def _set_gnss_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.gnss_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            gnss_connected=self.gnss_connected
        )

        if connected:
            self.gnss_connection_indicator.set_state(
                "CONNECTED",
                "good",
            )
        else:
            self.gnss_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

    def on_gnss_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        self._set_gnss_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"GNSS connected: {detail}"
            )

    def on_gnss_gga(
        self,
        gga: dict,
    ) -> None:

        self.shared.update_gnss(
            timestamp_ns=gga["timestamp_ns"],
            valid=gga["valid"],
            latitude=gga["latitude"],
            longitude=gga["longitude"],
            altitude=gga["altitude"],
            fix_quality=gga["fix_quality"],
            satellites=gga["satellites"],
            hdop=gga["hdop"],
        )

        self.gnss_gga_label.setText(
            (
                "GGA: "
                f"{gga['latitude']:.7f}, "
                f"{gga['longitude']:.7f} | "
                f"Fix {gga['fix_quality']} | "
                f"Sat {gga['satellites']}"
            )
        )

    def on_gnss_thread_finished(
        self,
    ) -> None:

        self.gnss_thread = None

        self._set_gnss_connection_status(
            False
        )

        self.gnss_connect_button.setText(
            "Connect GNSS"
        )

        self._set_gnss_fields_enabled(
            True
        )

    # -------------------------------------------------------------------------
    # USBL
    # -------------------------------------------------------------------------

    def update_usbl_connection_fields(
        self,
    ) -> None:

        mode = (
            self.usbl_mode_combo
            .currentText()
            .strip()
        )

        use_com = (
            mode == "COM Port"
        )

        can_edit = not (
            self.usbl_thread is not None
            and self.usbl_thread.isRunning()
        )

        self.usbl_mode_combo.setEnabled(
            can_edit
        )

        self.usbl_com_widget.setEnabled(
            can_edit
            and use_com
        )

        self.usbl_baud_combo.setEnabled(
            can_edit
            and use_com
        )

        self.usbl_udp_ip_edit.setEnabled(
            can_edit
            and not use_com
        )

        self.usbl_udp_port_spin.setEnabled(
            can_edit
            and not use_com
        )

    def toggle_usbl_connection(
        self,
    ) -> None:

        if (
            self.usbl_thread is not None
            and self.usbl_thread.isRunning()
        ):
            self.disconnect_usbl()
        else:
            self.connect_usbl()

    def connect_usbl(
        self,
    ) -> None:

        mode = (
            self.usbl_mode_combo
            .currentText()
            .strip()
        )

        if mode == "COM Port":

            port = (
                self.usbl_com_combo
                .currentText()
                .strip()
            )

            if not port:
                QMessageBox.warning(
                    self,
                    "USBL",
                    "USBL COM Port cannot be empty.",
                )
                return

            try:
                baudrate = int(
                    self.usbl_baud_combo
                    .currentText()
                    .strip()
                )

            except ValueError:
                QMessageBox.warning(
                    self,
                    "USBL",
                    "USBL baudrate must be an integer.",
                )
                return

            worker: QThread = (
                SerialNMEAThread(
                    "USBL",
                    port,
                    baudrate,
                    self,
                )
            )

        else:

            listen_ip = (
                self.usbl_udp_ip_edit
                .text()
                .strip()
                or DEFAULT_USBL_UDP_IP
            )

            udp_port = int(
                self.usbl_udp_port_spin.value()
            )

            worker = UDPNMEAThread(
                "USBL",
                listen_ip,
                udp_port,
                self,
            )

        self.usbl_connection_indicator.set_state(
            "CONNECTING...",
            "active",
        )

        self.usbl_connect_button.setText(
            "Disconnect USBL"
        )

        self.usbl_thread = worker

        worker.connection_changed.connect(
            self.on_usbl_connection_changed
        )

        worker.gga_received.connect(
            self.on_usbl_gga
        )

        worker.io_error.connect(
            lambda message:
            self._set_footer(
                f"USBL error: {message}",
                7000,
            )
        )

        worker.finished.connect(
            self.on_usbl_thread_finished
        )

        self.update_usbl_connection_fields()

        worker.start()

    def disconnect_usbl(
        self,
    ) -> None:

        worker = (
            self.usbl_thread
        )

        if worker is not None:
            worker.stop()

        self._set_footer(
            "Disconnecting USBL..."
        )

    def _set_usbl_connection_status(
        self,
        connected: bool,
    ) -> None:

        self.usbl_connected = bool(
            connected
        )

        self.shared.update_telemetry(
            usbl_connected=self.usbl_connected
        )

        if connected:
            self.usbl_connection_indicator.set_state(
                "CONNECTED",
                "good",
            )
        else:
            self.usbl_connection_indicator.set_state(
                "NOT CONNECTED",
                "bad",
            )

    def on_usbl_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:

        self._set_usbl_connection_status(
            connected
        )

        if connected:
            self._set_footer(
                f"USBL connected: {detail}"
            )

    def on_usbl_gga(
        self,
        gga: dict,
    ) -> None:

        self.shared.update_usbl(
            timestamp_ns=gga["timestamp_ns"],
            valid=gga["valid"],
            latitude=gga["latitude"],
            longitude=gga["longitude"],
            altitude=gga["altitude"],
            fix_quality=gga["fix_quality"],
            satellites=gga["satellites"],
            hdop=gga["hdop"],
        )

        self.usbl_gga_label.setText(
            (
                "GGA: "
                f"{gga['latitude']:.7f}, "
                f"{gga['longitude']:.7f} | "
                f"Fix {gga['fix_quality']} | "
                f"Sat {gga['satellites']}"
            )
        )

    def on_usbl_thread_finished(
        self,
    ) -> None:

        self.usbl_thread = None

        self._set_usbl_connection_status(
            False
        )

        self.usbl_connect_button.setText(
            "Connect USBL"
        )

        self.update_usbl_connection_fields()

    # -------------------------------------------------------------------------
    # Folder
    # -------------------------------------------------------------------------

    def browse_record_folder(
        self,
    ) -> None:

        current = (
            self.record_folder_edit
            .text()
            .strip()
            or DEFAULT_RECORD_FOLDER
        )

        selected = (
            QFileDialog.getExistingDirectory(
                self,
                "Select MiniSEED Record Folder",
                current,
            )
        )

        if selected:
            self.record_folder_edit.setText(
                selected
            )

    # -------------------------------------------------------------------------
    # INI
    # -------------------------------------------------------------------------

    def save_settings(
        self,
    ) -> None:

        record_folder = (
            self.record_folder_edit
            .text()
            .strip()
            or DEFAULT_RECORD_FOLDER
        )

        try:
            Path(
                record_folder
            ).expanduser().mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as exc:
            QMessageBox.warning(
                self,
                "Recording Folder",
                (
                    "Cannot create/access "
                    f"recording folder:\n\n{exc}"
                ),
            )
            return

        config = (
            configparser.ConfigParser()
        )

        config["Network"] = {
            "ip": (
                self.ip_edit
                .text()
                .strip()
            ),
            "command_port": str(
                self.command_port_spin.value()
            ),
            "data_port": str(
                self.data_port_spin.value()
            ),
        }

        decimation_samples = max(
            1,
            int(
                self.decimation_spin.value()
            ),
        )

        result_rate_hz = (
            float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            / float(
                decimation_samples
            )
        )

        config["Geophone"] = {
            # Primary setting.
            "decimation_samples": str(
                decimation_samples
            ),
            # Derived compatibility/display value.
            "decimation_rate_hz": (
                f"{result_rate_hz:.6f}"
            ),
        }

        config["GNSS"] = {
            "com_port": (
                self.gnss_com_combo
                .currentText()
                .strip()
            ),
            "baudrate": (
                self.gnss_baud_combo
                .currentText()
                .strip()
            ),
        }

        config["USBL"] = {
            "connection": (
                self.usbl_mode_combo
                .currentText()
                .strip()
            ),
            "com_port": (
                self.usbl_com_combo
                .currentText()
                .strip()
            ),
            "baudrate": (
                self.usbl_baud_combo
                .currentText()
                .strip()
            ),
            "udp_ip": (
                self.usbl_udp_ip_edit
                .text()
                .strip()
            ),
            "udp_port": str(
                self.usbl_udp_port_spin.value()
            ),
        }

        config["Recording"] = {
            "miniseed_folder": (
                record_folder
            ),
        }

        config["OBS_Commands"] = {
            key: value
            for key, value
            in self.command_templates.items()
        }

        try:
            with INI_PATH.open(
                "w",
                encoding="utf-8",
            ) as handle:
                config.write(
                    handle
                )

        except Exception as exc:
            LOGGER.exception(
                "Failed to save INI."
            )

            QMessageBox.critical(
                self,
                "Save Settings",
                (
                    "Failed to save settings:\n\n"
                    f"{exc}"
                ),
            )
            return

        self._set_footer(
            "Settings saved"
        )

        QMessageBox.information(
            self,
            "Save Settings",
            "OBS settings saved successfully.",
        )

    def load_settings(
        self,
        *,
        show_message: bool = False,
    ) -> None:

        config = (
            configparser.ConfigParser()
        )

        if not INI_PATH.exists():
            self._apply_default_settings()

            if show_message:
                QMessageBox.information(
                    self,
                    "Load Settings",
                    (
                        "obs_settings.ini not found. "
                        "Defaults loaded."
                    ),
                )

            return

        try:
            config.read(
                INI_PATH,
                encoding="utf-8",
            )

            self.ip_edit.setText(
                config.get(
                    "Network",
                    "ip",
                    fallback=DEFAULT_IP,
                )
            )

            self.command_port_spin.setValue(
                config.getint(
                    "Network",
                    "command_port",
                    fallback=DEFAULT_COMMAND_PORT,
                )
            )

            self.data_port_spin.setValue(
                config.getint(
                    "Network",
                    "data_port",
                    fallback=DEFAULT_DATA_PORT,
                )
            )

            if config.has_option(
                "Geophone",
                "decimation_samples",
            ):
                decimation_samples = (
                    config.getint(
                        "Geophone",
                        "decimation_samples",
                        fallback=(
                            DEFAULT_DECIMATION_SAMPLES
                        ),
                    )
                )
            else:
                # Legacy configuration stored desired output rate in Hz.
                legacy_rate_hz = (
                    config.getfloat(
                        "Geophone",
                        "decimation_rate_hz",
                        fallback=(
                            DEFAULT_DECIMATION_RATE_HZ
                        ),
                    )
                )

                if legacy_rate_hz > 0.0:
                    decimation_samples = int(
                        round(
                            float(
                                RAW_ADC_SAMPLE_RATE_HZ
                            )
                            / legacy_rate_hz
                        )
                    )
                else:
                    decimation_samples = (
                        DEFAULT_DECIMATION_SAMPLES
                    )

            self.decimation_spin.setValue(
                max(
                    1,
                    min(
                        int(
                            RAW_ADC_SAMPLE_RATE_HZ
                        ),
                        int(
                            decimation_samples
                        ),
                    ),
                )
            )

            self.update_decimation_result()

            self.gnss_com_combo.setCurrentText(
                config.get(
                    "GNSS",
                    "com_port",
                    fallback=DEFAULT_GNSS_COM,
                )
            )

            self.gnss_baud_combo.setCurrentText(
                config.get(
                    "GNSS",
                    "baudrate",
                    fallback=str(
                        DEFAULT_GNSS_BAUD
                    ),
                )
            )

            self.usbl_mode_combo.setCurrentText(
                config.get(
                    "USBL",
                    "connection",
                    fallback=DEFAULT_USBL_MODE,
                )
            )

            self.usbl_com_combo.setCurrentText(
                config.get(
                    "USBL",
                    "com_port",
                    fallback=DEFAULT_USBL_COM,
                )
            )

            self.usbl_baud_combo.setCurrentText(
                config.get(
                    "USBL",
                    "baudrate",
                    fallback=str(
                        DEFAULT_USBL_BAUD
                    ),
                )
            )

            self.usbl_udp_ip_edit.setText(
                config.get(
                    "USBL",
                    "udp_ip",
                    fallback=DEFAULT_USBL_UDP_IP,
                )
            )

            self.usbl_udp_port_spin.setValue(
                config.getint(
                    "USBL",
                    "udp_port",
                    fallback=DEFAULT_USBL_UDP_PORT,
                )
            )

            self.record_folder_edit.setText(
                config.get(
                    "Recording",
                    "miniseed_folder",
                    fallback=DEFAULT_RECORD_FOLDER,
                )
            )

            for key in (
                "gimbal_lock",
                "gimbal_unlock",
                "power_low",
                "power_normal",
            ):
                self.command_templates[key] = (
                    config.get(
                        "OBS_Commands",
                        key,
                        fallback="",
                    )
                    .strip()
                )

        except Exception as exc:
            LOGGER.exception(
                "Failed to load INI."
            )

            QMessageBox.warning(
                self,
                "Load Settings",
                (
                    "Failed to read obs_settings.ini.\n"
                    "Defaults will be used.\n\n"
                    f"{exc}"
                ),
            )

            self._apply_default_settings()
            return

        self.update_usbl_connection_fields()

        if hasattr(
            self,
            "footer_status",
        ):
            self._set_footer(
                "Settings loaded"
            )

        if show_message:
            QMessageBox.information(
                self,
                "Load Settings",
                "OBS settings loaded successfully.",
            )

    def _apply_default_settings(
        self,
    ) -> None:

        self.ip_edit.setText(
            DEFAULT_IP
        )

        self.command_port_spin.setValue(
            DEFAULT_COMMAND_PORT
        )

        self.data_port_spin.setValue(
            DEFAULT_DATA_PORT
        )

        self.decimation_spin.setValue(
            DEFAULT_DECIMATION_SAMPLES
        )

        self.update_decimation_result()

        self.gnss_com_combo.setCurrentText(
            DEFAULT_GNSS_COM
        )

        self.gnss_baud_combo.setCurrentText(
            str(
                DEFAULT_GNSS_BAUD
            )
        )

        self.usbl_mode_combo.setCurrentText(
            DEFAULT_USBL_MODE
        )

        self.usbl_com_combo.setCurrentText(
            DEFAULT_USBL_COM
        )

        self.usbl_baud_combo.setCurrentText(
            str(
                DEFAULT_USBL_BAUD
            )
        )

        self.usbl_udp_ip_edit.setText(
            DEFAULT_USBL_UDP_IP
        )

        self.usbl_udp_port_spin.setValue(
            DEFAULT_USBL_UDP_PORT
        )

        self.record_folder_edit.setText(
            DEFAULT_RECORD_FOLDER
        )

        self.command_templates = {
            "gimbal_lock": "",
            "gimbal_unlock": "",
            "power_low": "",
            "power_normal": "",
        }

        self.update_usbl_connection_fields()

    # -------------------------------------------------------------------------
    # Close
    # -------------------------------------------------------------------------

    def closeEvent(
        self,
        event: QCloseEvent,
    ) -> None:

        workers = [
            self.data_thread,
            self.command_thread,
            self.gnss_thread,
            self.usbl_thread,
        ]

        for worker in workers:
            if worker is not None:
                try:
                    worker.stop()
                except Exception:
                    pass

        for worker in workers:
            if worker is not None:
                try:
                    worker.wait(
                        1500
                    )
                except Exception:
                    pass

        self.shared.update_telemetry(
            data_connected=False,
            command_connected=False,
            gnss_connected=False,
            usbl_connected=False,
        )

        self.shared.close()

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

    window = OBSSettingWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(
            main()
        )

    except SystemExit:
        raise

    except Exception:
        LOGGER.exception(
            "Unhandled fatal error in obs_setting.py"
        )
        raise
