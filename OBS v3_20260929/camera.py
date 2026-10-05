"""
camera.py
=========

GRC-UGM-PERTAMINA OBS
Camera Monitor / PTZ Control UI

Version: 17


Version 17 direct shared-telemetry attitude
-------------------------------------------
- Removes Camera yaw offset handling.
- HUD Roll/Pitch/Yaw now come directly from shared_data.py telemetry.
- Keeps Operational Marine HUD and MP4 burn-in unchanged.

Version 16 Operational Marine HUD refinement
--------------------------------------------
- Refine HUD into a cleaner marine-instrument style suitable for field use.
- Reduce visual obstruction with lighter translucent panels and thinner borders.
- Use compact LIVE / PARTIAL / STALE status indicator.
- Standardize typography and spacing using Consolas.
- Use degree symbol for Roll / Pitch / Yaw.
- Make Depth the strongest lower-left readout.
- Keep timestamp compact at top-center in YYYYMMDD_HHMMSS.
- Keep Yaw prominent at top-right.
- Keep Roll/Pitch grouped at bottom-right.
- Preserve overlay checkbox behavior and MP4 burn-in.

Version 15 operational HUD overlay
----------------------------------
- Redesign telemetry overlay into a cleaner operational HUD.
- Add timestamp in YYYYMMDD_HHMMSS format.
- Add operator checkbox to enable/disable HUD overlay.
- HUD is applied to both live preview and recorded MP4 when enabled.
- When disabled, preview and recording remain clean with no telemetry/timestamp overlay.
- Layout:
    * top-left     : OBS / telemetry state
    * top-center   : timestamp
    * top-right    : YAW / heading
    * bottom-left  : DEPTH
    * bottom-right : ROLL / PITCH

Version 14 canonical shared-data import
---------------------------------------
- Import OBSSharedData from runtime canonical module shared_data.py.
- Compatible with shared_data API Version 10.
- Removes dependency on legacy filename shared_data_v8.py.

Version 12 telemetry video overlay
----------------------------------
- Overlay Depth, Roll, Pitch and Yaw directly on the QVideoSink frame.
- Overlay appears in live preview and is burned into recorded MP4 frames.
- Values are read from centralized shared_data.py API v10 RAM.
- Depth uses telemetry.pressure as meters, matching Other Sensors.
- Yaw applies optional [IMU] yaw_offset_deg from obs_settings.ini.
- Stale IMU/depth sources are marked STALE.

Version 11 dedicated QVideoSink recorder update
-----------------------------------------------
- Use a dedicated QVideoSink owned by QMediaCaptureSession for both live preview
  and OpenCV recording instead of depending on QVideoWidget.videoSink().
- This fixes USB/UVC modes (including NV12) where QVideoWidget displays correctly
  but does not expose usable frame callbacks to the recorder.
- On Windows, reliable recording now requires OpenCV + NumPy + QVideoSink; the GUI
  no longer silently falls back to QMediaRecorder when the deterministic frame path
  is unavailable, because that fallback can create zero-byte MP4 files.
- Live preview is rendered from the same QVideoSink frames that are recorded.
- Recorder diagnostics explicitly show DIRECT QVideoSink / OpenCV or the reason
  recording is unavailable.
- Show live capture FPS, writer FPS, dropped-frame count, and local staging-file
  size so the operator can immediately see whether recording keeps up in real time.
- Prefer the measured QVideoSink delivery rate for MP4 timing once a stable live
  rate is available; fall back to the advertised camera format rate at startup.

Version 10 frame-recorder update
--------------------------------
- On Windows, record the exact frames already delivered to the Qt video sink
  instead of relying on QMediaRecorder/Media Foundation encoder negotiation.
- Use OpenCV VideoWriter (MP4V in MP4) on a background writer thread.
- Record/Pause/Resume/Stop now operate on a deterministic frame queue.
- Stop releases/finalizes VideoWriter before reporting the file as saved.
- Zero-frame/zero-byte recordings are treated as explicit errors and an empty
  file is removed instead of being left as a misleading MP4.
- Keep QMediaRecorder as a compatibility fallback when OpenCV is unavailable.

Version 9 recording reliability update
--------------------------------------
- Fix Record -> Pause/Stop controls on asynchronous Qt multimedia backends.
- Track recording session state in the GUI instead of relying only on an
  immediate recorderState() read after record().
- Keep Stop available as soon as a recording request is issued.
- Confirm recorder stop/finalization before reporting a file as saved.
- Wait for recorder finalization before the active camera is detached/stopped.
- Report recorder/backend errors more explicitly and verify non-empty output.

Version 8 centralized OBS command transport
--------------------------------------
- On Windows, prefer the native Windows Media Foundation Qt backend unless
  QT_MEDIA_BACKEND is explicitly set by the user.
- Enumerate every format advertised by the selected UVC/USB camera and choose
  a smooth format explicitly instead of accepting Qt's implicit default.
- Prefer >=30 FPS, compressed JPEG/MJPEG modes, and a resolution near 1280x720.
- Show the selected capture format in the camera status text.
- Avoid opening the same camera twice during camera-list refresh.
- Preserve Version 5 layout, recording, and OBS RMCMD 50-60 behavior.

Current scope
-------------
- Enumerate available hardware camera devices using Qt Multimedia.
- Select a camera from the device list.
- Display live video in the left 3/4 of the window.
- Record live camera video to a selectable folder:
    * Record
    * Pause / Resume Record
    * Stop Record
- Send RC-panel control commands through the local OBS Setting IPC broker:
    * Manual/Auto toggle
    * LED toggle
    * RC speed command
    * Up / Down / Left / Right press
    * Direction-specific depress on button release
    * Stop-all helper sends vertical + horizontal depress

OBS command protocol
--------------------
The camera module never opens an OBS TCP socket. RC commands are sent through
obs_ipc.py to OBS Setting, which owns the single physical TCP 54300 connection.

Wire format:
    $RMCMD,<CMD_CODE>*<XOR><CR><LF>

RC panel codes:
    50 = RC_MANUAL_AUTO
    51 = RC_LED
    52 = RC_SPEED
    53 = RC_UP_PRESS
    54 = RC_UP_DEPRESS
    55 = RC_DOWN_PRESS
    56 = RC_DOWN_DEPRESS
    57 = RC_RIGHT_PRESS
    58 = RC_RIGHT_DEPRESS
    59 = RC_LEFT_PRESS
    60 = RC_LEFT_DEPRESS

Important protocol detail:
    RC_MANUAL_AUTO and RC_LED are TOGGLE commands. The protocol does not
    provide explicit "Manual", "Auto", "LED ON", or "LED OFF" commands.
    Therefore v4 uses one toggle button for each function and does not invent
    a hardware state that cannot be confirmed from the supplied protocol.

    RC_SPEED is also a fixed command frame. No numeric speed parameter is
    present in $RMCMD,52, so the old editable numeric speed field is replaced
    by an RC Speed command button.

Dependencies
------------
    pip install PySide6 opencv-python numpy

PySide6 QtMultimedia / QtMultimediaWidgets must be available.
OpenCV + NumPy are used for the preferred reliable MP4 frame recorder.
QMediaRecorder remains only as a non-Windows compatibility fallback.
"""

from __future__ import annotations

import math
import os
import queue
import shutil
import sys
import tempfile
import traceback
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.CAMERA"


def configure_windows_runtime() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

        kernel32 = ctypes.windll.kernel32

        # Keep the GUI responsive without using an aggressive process class.
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
        )

        # V13 diagnostic-safe startup:
        # keep the console attached when launched through python.exe so any
        # low-level Qt/Multimedia error remains visible during commissioning.
        # Production EXE builds can still use CREATE_NO_WINDOW/pythonw.exe.

    except Exception:
        pass


configure_windows_runtime()

# Qt 6 uses its FFmpeg multimedia backend by default on Windows. For UVC/USB
# cameras the native Windows Media Foundation path is often closer to what
# vendor camera applications use, and can avoid uneven delivery from some
# webcam drivers. Keep an explicit user/system override untouched.
if os.name == "nt":
    os.environ.setdefault("QT_MEDIA_BACKEND", "windows")


# =============================================================================
# Qt
# =============================================================================

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from obs_ipc import OBSIPCClient
from shared_data import OBSSharedData

try:
    from PySide6.QtMultimedia import (
        QCamera,
        QMediaCaptureSession,
        QMediaDevices,
        QVideoFrameFormat,
    )
    from PySide6.QtMultimediaWidgets import QVideoWidget

    QT_MULTIMEDIA_AVAILABLE = True
    QT_MULTIMEDIA_ERROR = ""

except Exception as exc:
    QCamera = None
    QMediaCaptureSession = None
    QMediaDevices = None
    QVideoFrameFormat = None
    QVideoWidget = None

    QT_MULTIMEDIA_AVAILABLE = False
    QT_MULTIMEDIA_ERROR = str(exc)

# QVideoSink is imported separately so an older PySide6 build can still display
# a useful dependency error without disabling the rest of Qt Multimedia.
try:
    from PySide6.QtMultimedia import QVideoSink
    QT_VIDEO_SINK_AVAILABLE = True
    QT_VIDEO_SINK_ERROR = ""
except Exception as exc:
    QVideoSink = None
    QT_VIDEO_SINK_AVAILABLE = False
    QT_VIDEO_SINK_ERROR = str(exc)

# Recorder support is kept separate so live camera display still works even if
# the installed PySide6 multimedia backend does not provide QMediaRecorder.
try:
    from PySide6.QtMultimedia import (
        QMediaFormat,
        QMediaRecorder,
    )

    QT_RECORDER_AVAILABLE = True
    QT_RECORDER_ERROR = ""

except Exception as exc:
    QMediaFormat = None
    QMediaRecorder = None

    QT_RECORDER_AVAILABLE = False
    QT_RECORDER_ERROR = str(exc)

# Preferred deterministic recorder.  The Qt video sink already receives the
# exact frames used by the preview, so writing those frames avoids a second
# Media Foundation encoder negotiation path that can enter RecordingState yet
# still produce a zero-byte MP4 on some Windows webcam/driver combinations.
try:
    import cv2
    import numpy as np

    CV_RECORDER_AVAILABLE = True
    CV_RECORDER_ERROR = ""
except Exception as exc:
    cv2 = None
    np = None
    CV_RECORDER_AVAILABLE = False
    CV_RECORDER_ERROR = str(exc)


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Camera"

BASE_DIR = Path(__file__).resolve().parent

# In a PyInstaller one-folder build, configuration remains external beside the
# executable while resources remain relative to the bundled module location.
RUNTIME_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else BASE_DIR
)

ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

DEFAULT_RECORD_FOLDER = RUNTIME_DIR / "recordings" / "video"

OVERLAY_REFRESH_MS = 100
OVERLAY_SOURCE_STALE_S = 2.0

# USB/UVC live-view policy. The camera can still fall back to any advertised
# format when 1280x720 or 30 FPS are unavailable. Smooth frame delivery is
# deliberately prioritized over maximum still-image resolution.
CAMERA_TARGET_WIDTH = 1280
CAMERA_TARGET_HEIGHT = 720
CAMERA_SMOOTH_FPS = 30.0
CAMERA_MAX_USEFUL_FPS = 60.0

OBS_COMMAND_MAX_SENTENCE_BYTES = 82

RC_MANUAL_AUTO = 50
RC_LED = 51
RC_SPEED = 52
RC_UP_PRESS = 53
RC_UP_DEPRESS = 54
RC_DOWN_PRESS = 55
RC_DOWN_DEPRESS = 56
RC_RIGHT_PRESS = 57
RC_RIGHT_DEPRESS = 58
RC_LEFT_PRESS = 59
RC_LEFT_DEPRESS = 60

RC_ALLOWED_CODES = frozenset(
    (
        RC_MANUAL_AUTO,
        RC_LED,
        RC_SPEED,
        RC_UP_PRESS,
        RC_UP_DEPRESS,
        RC_DOWN_PRESS,
        RC_DOWN_DEPRESS,
        RC_RIGHT_PRESS,
        RC_RIGHT_DEPRESS,
        RC_LEFT_PRESS,
        RC_LEFT_DEPRESS,
    )
)

RC_PRESS_CODES = {
    "UP": RC_UP_PRESS,
    "DOWN": RC_DOWN_PRESS,
    "RIGHT": RC_RIGHT_PRESS,
    "LEFT": RC_LEFT_PRESS,
}

RC_DEPRESS_CODES = {
    "UP": RC_UP_DEPRESS,
    "DOWN": RC_DOWN_DEPRESS,
    "RIGHT": RC_RIGHT_DEPRESS,
    "LEFT": RC_LEFT_DEPRESS,
}

# A STOP button is not explicitly defined by the supplied socket protocol.
# Underlying UART frames show one common vertical depress and one common
# horizontal depress, so Stop All transmits one depress command for each axis.
RC_STOP_ALL_CODES = (
    RC_UP_DEPRESS,
    RC_RIGHT_DEPRESS,
)

RC_CODE_NAMES = {
    RC_MANUAL_AUTO: "RC_MANUAL_AUTO",
    RC_LED: "RC_LED",
    RC_SPEED: "RC_SPEED",
    RC_UP_PRESS: "RC_UP_PRESS",
    RC_UP_DEPRESS: "RC_UP_DEPRESS",
    RC_DOWN_PRESS: "RC_DOWN_PRESS",
    RC_DOWN_DEPRESS: "RC_DOWN_DEPRESS",
    RC_RIGHT_PRESS: "RC_RIGHT_PRESS",
    RC_RIGHT_DEPRESS: "RC_RIGHT_DEPRESS",
    RC_LEFT_PRESS: "RC_LEFT_PRESS",
    RC_LEFT_DEPRESS: "RC_LEFT_DEPRESS",
}


# =============================================================================
# Helpers
# =============================================================================


def obs_xor_checksum(
    body: str,
) -> int:
    checksum = 0

    for value in body.encode(
        "ascii"
    ):
        checksum ^= value

    return checksum


def build_obs_sentence(
    body: str,
) -> bytes:
    body = str(
        body
    ).strip()

    if not body:
        raise ValueError(
            "OBS command body is empty."
        )

    if body.startswith(
        "$"
    ):
        body = body[1:]

    if "*" in body:
        body = body.split(
            "*",
            1,
        )[0]

    checksum = obs_xor_checksum(
        body
    )

    sentence = (
        f"${body}*{checksum:02X}\r\n"
    ).encode(
        "ascii"
    )

    if len(
        sentence
    ) > OBS_COMMAND_MAX_SENTENCE_BYTES:
        raise ValueError(
            (
                "OBS command exceeds "
                f"{OBS_COMMAND_MAX_SENTENCE_BYTES} bytes."
            )
        )

    return sentence


def build_rc_sentence(
    command_code: int,
) -> bytes:
    command_code = int(
        command_code
    )

    if command_code not in RC_ALLOWED_CODES:
        raise ValueError(
            (
                f"Unsupported RC command code {command_code}. "
                "Allowed codes are 50 through 60."
            )
        )

    return build_obs_sentence(
        f"RMCMD,{command_code}"
    )


class OBSCommandClientThread(OBSIPCClient):
    """Compatibility wrapper around the local OBS Setting IPC command bus.

    Despite the legacy class name, this object is NOT a thread and opens NO TCP
    socket to the OBS controller. It keeps the existing CameraWindow lifecycle API
    so the rest of the camera UI does not need invasive changes.
    """

    sentence_sent = Signal(str)

    def __init__(self, _host: str = "", _port: int = 0, parent=None):
        super().__init__(source="camera", parent=parent)
        self.response_received.connect(self._handle_response)

    def queue_sentence(self, sentence: bytes) -> None:
        text = bytes(sentence).decode("ascii", errors="strict").strip()
        if not text.startswith("$RMCMD,"):
            raise ValueError("Camera IPC accepts only $RMCMD commands.")
        body = text[1:].split("*", 1)[0]
        parts = body.split(",")
        if len(parts) != 2:
            raise ValueError("Invalid Camera RMCMD sentence.")
        code = int(parts[1], 10)
        if code not in RC_ALLOWED_CODES:
            raise ValueError(f"Unsupported Camera RMCMD code {code}.")
        self.send_rmcmd(code)

    def _handle_response(self, response: object) -> None:
        message = dict(response or {})
        response_type = str(message.get("type") or "")
        if response_type == "sent" and message.get("request_type") == "rmcmd":
            self.sentence_sent.emit(str(message.get("sentence") or ""))
        elif response_type == "error":
            self.error_occurred.emit(str(message.get("message") or "IPC command error"))



def wrap_angle_deg(value: float) -> float:
    value = float(value) % 360.0
    if value < 0.0:
        value += 360.0
    return value



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


# =============================================================================
# Background frame recorder
# =============================================================================


class CVVideoWriterWorker:
    """Small bounded-queue MP4 writer for frames from the dedicated QVideoSink.

    The queue is deliberately short.  If storage/encoding momentarily falls
    behind, old queued frames are dropped rather than allowing unbounded memory
    growth or making the live preview progressively delayed.
    """

    def __init__(self, path: Path, fps: float, queue_size: int = 4):
        self.path = Path(path)
        self.fps = max(1.0, float(fps))
        self.queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self.stop_event = threading.Event()
        self.thread = None
        self.writer = None
        self.frames_written = 0
        self.frames_dropped = 0
        self.error_text = ""
        self.frame_size = None
        self.codec_name = "mp4v"

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name="OBS-Camera-MP4-Writer",
            daemon=True,
        )
        self.thread.start()

    def enqueue(self, image: QImage) -> bool:
        if self.stop_event.is_set() or self.error_text:
            return False
        if image is None or image.isNull():
            return False

        # Detach from QVideoFrame-owned storage before crossing threads.
        safe_image = image.copy()
        try:
            self.queue.put_nowait(safe_image)
            return True
        except queue.Full:
            # Low-latency policy: discard the oldest queued frame, then keep the
            # newest frame.  Recording remains close to live time.
            try:
                self.queue.get_nowait()
                self.frames_dropped += 1
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(safe_image)
                return True
            except queue.Full:
                self.frames_dropped += 1
                return False

    def stop(self, timeout_s: float = 5.0) -> bool:
        self.stop_event.set()

        # Put a sentinel behind any frames already queued.
        placed = False
        while not placed:
            try:
                self.queue.put_nowait(None)
                placed = True
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.frames_dropped += 1
                except queue.Empty:
                    placed = True

        if self.thread is not None:
            self.thread.join(timeout=max(0.1, float(timeout_s)))
            return not self.thread.is_alive()
        return True

    @staticmethod
    def _qimage_to_bgr(image: QImage):
        if np is None:
            raise RuntimeError("NumPy is unavailable")

        fmt_bgr = getattr(QImage.Format, "Format_BGR888", None)
        if fmt_bgr is not None:
            converted = image.convertToFormat(fmt_bgr)
            is_bgr = True
        else:
            converted = image.convertToFormat(QImage.Format.Format_RGB888)
            is_bgr = False

        width = int(converted.width())
        height = int(converted.height())
        bytes_per_line = int(converted.bytesPerLine())
        size_bytes = int(converted.sizeInBytes())

        if width <= 0 or height <= 0 or size_bytes <= 0:
            raise RuntimeError("Invalid video frame dimensions")

        bits = converted.bits()
        flat = np.frombuffer(bits, dtype=np.uint8, count=size_bytes)
        rows = flat.reshape((height, bytes_per_line))
        rgb = rows[:, : width * 3].reshape((height, width, 3)).copy()

        if is_bgr:
            return rgb
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    def _open_writer(self, width: int, height: int):
        fourcc = cv2.VideoWriter_fourcc(*self.codec_name)
        writer = cv2.VideoWriter(
            str(self.path),
            fourcc,
            self.fps,
            (int(width), int(height)),
        )
        if not writer.isOpened():
            try:
                writer.release()
            except Exception:
                pass
            raise RuntimeError(
                "OpenCV cannot open MP4V encoder/output. "
                "Check opencv-python/FFmpeg support and recording-folder permissions."
            )
        return writer

    def _run(self) -> None:
        try:
            while True:
                try:
                    image = self.queue.get(timeout=0.1)
                except queue.Empty:
                    if self.stop_event.is_set():
                        break
                    continue

                if image is None:
                    break

                frame = self._qimage_to_bgr(image)
                height, width = frame.shape[:2]

                if self.writer is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.writer = self._open_writer(width, height)
                    self.frame_size = (width, height)

                if self.frame_size != (width, height):
                    frame = cv2.resize(
                        frame,
                        self.frame_size,
                        interpolation=cv2.INTER_AREA,
                    )

                self.writer.write(frame)
                self.frames_written += 1

        except Exception as exc:
            self.error_text = str(exc)

        finally:
            if self.writer is not None:
                try:
                    self.writer.release()
                except Exception:
                    pass
                self.writer = None


# =============================================================================
# Main camera window
# =============================================================================


class CameraWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(APP_TITLE)
        self.resize(1440, 820)
        self.setMinimumSize(960, 600)

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.media_devices = None
        self.capture_session = None
        self.camera = None
        self.camera_devices = []
        self.current_camera_format_text = ""

        # v11: one dedicated QVideoSink feeds BOTH preview and recorder.  This
        # avoids relying on QVideoWidget.videoSink(), which can be unavailable on
        # the Windows backend even while the widget itself displays video.
        self.preview_uses_direct_sink = False
        self.latest_preview_image = None
        # Live sink-rate diagnostics.  QVideoSink callbacks are authoritative for
        # the actual frame-delivery rate; the camera format only states a nominal
        # / maximum rate and can differ from what the UVC driver really delivers.
        self.sink_frame_count = 0
        self.sink_rate_window_start = time.monotonic()
        self.sink_rate_window_count = 0
        self.sink_fps = 0.0
        self.sink_last_frame_monotonic = 0.0
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(33)  # ~30 FPS GUI rendering
        self.preview_timer.timeout.connect(self._render_latest_preview_frame)

        self.media_recorder = None
        self.current_record_path = None

        # Preferred frame-recorder state.
        self.video_sink = None
        self.cv_record_capable = False
        self.cv_writer_worker = None
        self.cv_staging_path = None
        self.cv_record_start_monotonic = 0.0
        self.cv_pause_started_monotonic = None
        self.cv_paused_total_s = 0.0
        self.cv_record_timer = QTimer(self)
        self.cv_record_timer.setInterval(250)
        self.cv_record_timer.timeout.connect(self._refresh_cv_record_status)

        # QMediaRecorder state transitions are asynchronous on some Windows
        # backends.  Keep a UI/session state that changes immediately when the
        # operator presses Record/Pause/Stop, then reconcile it with the actual
        # backend state from recorderStateChanged.  This prevents Pause/Stop
        # from remaining disabled while the backend is already recording.
        self.record_ui_state = "stopped"
        self.record_session_in_progress = False
        self.record_last_error = ""
        self.record_stop_verify_generation = 0
        self.record_folder = Path(DEFAULT_RECORD_FOLDER)

        try:
            self.record_folder.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception:
            # Folder can still be selected manually from the UI.
            pass

        self.command_host = "OBS Setting IPC"
        self.command_port = 0

        self.command_thread: Optional[
            OBSCommandClientThread
        ] = None

        self.command_connected = False
        self.active_pan_direction: Optional[
            str
        ] = None
        self.last_rc_sentence = ""

        try:
            self.shared = OBSSharedData()
        except Exception:
            self.shared = None

        self.overlay_depth_m = 0.0
        self.overlay_roll_deg = 0.0
        self.overlay_pitch_deg = 0.0
        self.overlay_yaw_deg = 0.0
        self.overlay_imu_live = False
        self.overlay_depth_live = False
        self.overlay_has_telemetry = False
        self.overlay_enabled = True

        self._build_ui()
        self._apply_style()
        self._start_command_client()

        self.health_timer = QTimer(self)
        self.health_timer.setInterval(500)
        self.health_timer.timeout.connect(self.refresh_acquisition_health)
        self.health_timer.start()
        self.refresh_acquisition_health()

        self.overlay_timer = QTimer(self)
        self.overlay_timer.setInterval(OVERLAY_REFRESH_MS)
        self.overlay_timer.timeout.connect(self.refresh_video_overlay_telemetry)
        self.overlay_timer.start()
        self.refresh_video_overlay_telemetry()

        if QT_MULTIMEDIA_AVAILABLE:
            self._initialize_multimedia()
        else:
            self.camera_status_label.setText(
                "Qt Multimedia unavailable"
            )
            self.video_placeholder.setText(
                "Camera video unavailable.\n\n"
                "Qt Multimedia could not be loaded.\n\n"
                f"{QT_MULTIMEDIA_ERROR}"
            )
            self.camera_combo.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.record_button.setEnabled(False)
            self.pause_record_button.setEnabled(False)
            self.stop_record_button.setEnabled(False)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ==============================================================
        # LEFT 3/4 — VIDEO ONLY, NO DISPLAY TITLE
        # ==============================================================
        self.video_frame = QFrame()
        self.video_frame.setObjectName("videoFrame")

        video_layout = QVBoxLayout(self.video_frame)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        self.video_stack = QFrame()
        self.video_stack.setObjectName("videoStack")

        stack_layout = QVBoxLayout(self.video_stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(0)

        self.video_widget = None

        if QT_MULTIMEDIA_AVAILABLE:
            if QT_VIDEO_SINK_AVAILABLE:
                # QLabel preview fed by the dedicated QVideoSink.  The same frame
                # objects are also passed to the OpenCV recorder.
                self.video_widget = QLabel()
                self.video_widget.setObjectName("videoWidget")
                self.video_widget.setAlignment(Qt.AlignCenter)
                self.video_widget.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Expanding,
                )
                self.preview_uses_direct_sink = True
            else:
                # Display-only compatibility fallback for older Qt builds.
                self.video_widget = QVideoWidget()
                self.video_widget.setObjectName("videoWidget")
                try:
                    self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
                except Exception:
                    pass
                self.preview_uses_direct_sink = False

            stack_layout.addWidget(
                self.video_widget,
                1,
            )

        self.video_placeholder = QLabel(
            "No Camera Selected"
        )
        self.video_placeholder.setObjectName(
            "videoPlaceholder"
        )
        self.video_placeholder.setAlignment(
            Qt.AlignCenter
        )

        # Placeholder is overlaid simply by placing it after QVideoWidget and
        # toggling visibility depending on camera state.
        stack_layout.addWidget(
            self.video_placeholder,
            1,
        )

        if self.video_widget is not None:
            self.video_widget.hide()

        video_layout.addWidget(
            self.video_stack,
            1,
        )

        splitter.addWidget(
            self.video_frame
        )

        # ==============================================================
        # RIGHT 1/4 — CAMERA + PAN/LIGHT CONTROLS
        # ==============================================================
        control_panel = QFrame()
        control_panel.setObjectName(
            "controlPanel"
        )
        control_panel.setMinimumWidth(190)
        # Keep the panel content at its natural vertical size. The surrounding
        # QScrollArea provides scrolling instead of allowing the control groups
        # to be crushed together when the main window becomes short.
        control_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )

        controls = QVBoxLayout(
            control_panel
        )
        controls.setContentsMargins(
            8, 6, 6, 6
        )
        controls.setSpacing(7)

        # Camera selection.
        camera_group = QGroupBox(
            "Camera Device"
        )
        camera_group.setObjectName(
            "controlGroup"
        )

        cg = QVBoxLayout(
            camera_group
        )
        cg.setContentsMargins(
            10, 14, 10, 10
        )
        cg.setSpacing(7)

        self.camera_combo = QComboBox()
        self.camera_combo.setObjectName(
            "cameraCombo"
        )
        self.camera_combo.currentIndexChanged.connect(
            self.on_camera_selected
        )

        self.refresh_button = QPushButton(
            "Refresh Camera List"
        )
        self.refresh_button.setObjectName(
            "secondaryButton"
        )
        self.refresh_button.clicked.connect(
            self.refresh_camera_list
        )

        self.camera_status_label = QLabel(
            "Searching camera..."
        )
        self.camera_status_label.setObjectName(
            "statusText"
        )
        self.camera_status_label.setWordWrap(
            True
        )

        cg.addWidget(
            self.camera_combo
        )
        cg.addWidget(
            self.refresh_button
        )
        cg.addWidget(
            self.camera_status_label
        )

        controls.addWidget(
            camera_group
        )

        # Video recording.
        record_group = QGroupBox(
            "Video Recording"
        )
        record_group.setObjectName(
            "controlGroup"
        )

        rg = QVBoxLayout(
            record_group
        )
        rg.setContentsMargins(
            8, 14, 8, 8
        )
        rg.setSpacing(5)

        folder_label = QLabel(
            "Save Folder"
        )
        folder_label.setObjectName(
            "fieldLabel"
        )

        self.record_folder_edit = QLineEdit(
            str(self.record_folder)
        )
        self.record_folder_edit.setObjectName(
            "recordFolderEdit"
        )
        self.record_folder_edit.setReadOnly(
            True
        )

        self.record_folder_button = QPushButton(
            "Choose Folder"
        )
        self.record_folder_button.setObjectName(
            "secondaryButton"
        )
        self.record_folder_button.clicked.connect(
            self.choose_record_folder
        )

        record_buttons = QHBoxLayout()
        record_buttons.setSpacing(4)

        self.record_button = QPushButton(
            "Record"
        )
        self.record_button.setObjectName(
            "recordButton"
        )

        self.pause_record_button = QPushButton(
            "Pause"
        )
        self.pause_record_button.setObjectName(
            "pauseRecordButton"
        )

        self.stop_record_button = QPushButton(
            "Stop"
        )
        self.stop_record_button.setObjectName(
            "stopRecordButton"
        )

        self.record_button.clicked.connect(
            self.start_recording
        )
        self.pause_record_button.clicked.connect(
            self.pause_or_resume_recording
        )
        self.stop_record_button.clicked.connect(
            self.stop_recording
        )

        self.record_button.setEnabled(
            False
        )
        self.pause_record_button.setEnabled(
            False
        )
        self.stop_record_button.setEnabled(
            False
        )

        record_buttons.addWidget(
            self.record_button
        )
        record_buttons.addWidget(
            self.pause_record_button
        )
        record_buttons.addWidget(
            self.stop_record_button
        )

        self.record_status_label = QLabel(
            "Recorder: ready"
            if QT_RECORDER_AVAILABLE
            else "Recorder unavailable"
        )
        self.record_status_label.setObjectName(
            "statusText"
        )
        self.record_status_label.setWordWrap(
            True
        )

        rg.addWidget(
            folder_label
        )
        rg.addWidget(
            self.record_folder_edit
        )
        rg.addWidget(
            self.record_folder_button
        )

        self.overlay_checkbox = QCheckBox(
            "Operational HUD Overlay"
        )
        self.overlay_checkbox.setObjectName(
            "overlayCheckBox"
        )
        self.overlay_checkbox.setChecked(
            True
        )
        self.overlay_checkbox.setToolTip(
            "Enable operational Depth / Roll / Pitch / Yaw / Timestamp HUD "
            "for both live preview and recorded MP4."
        )
        self.overlay_checkbox.toggled.connect(
            self.on_overlay_toggled
        )

        rg.addWidget(
            self.overlay_checkbox
        )

        rg.addLayout(
            record_buttons
        )
        rg.addWidget(
            self.record_status_label
        )

        controls.addWidget(
            record_group
        )

        # Pan controls.
        pan_group = QGroupBox(
            "Pan Control"
        )
        pan_group.setObjectName(
            "controlGroup"
        )

        pg = QGridLayout(
            pan_group
        )
        pg.setContentsMargins(
            10, 14, 10, 10
        )
        pg.setHorizontalSpacing(
            7
        )
        pg.setVerticalSpacing(
            7
        )

        self.pan_up_button = QPushButton(
            "▲"
        )
        self.pan_down_button = QPushButton(
            "▼"
        )
        self.pan_left_button = QPushButton(
            "◀"
        )
        self.pan_right_button = QPushButton(
            "▶"
        )
        self.pan_stop_button = QPushButton(
            "STOP"
        )

        for button in (
            self.pan_up_button,
            self.pan_down_button,
            self.pan_left_button,
            self.pan_right_button,
        ):
            button.setObjectName(
                "directionButton"
            )
            button.setMinimumSize(
                62,
                48,
            )

        self.pan_stop_button.setObjectName(
            "stopButton"
        )
        self.pan_stop_button.setMinimumSize(
            62,
            48,
        )

        pg.addWidget(
            self.pan_up_button,
            0,
            1,
        )
        pg.addWidget(
            self.pan_left_button,
            1,
            0,
        )
        pg.addWidget(
            self.pan_stop_button,
            1,
            1,
        )
        pg.addWidget(
            self.pan_right_button,
            1,
            2,
        )
        pg.addWidget(
            self.pan_down_button,
            2,
            1,
        )

        self.speed_button = QPushButton(
            "RC Speed"
        )
        self.speed_button.setObjectName(
            "secondaryButton"
        )
        self.speed_button.setToolTip(
            (
                "Send $RMCMD,52. The supplied protocol defines RC_SPEED "
                "as a fixed 5-byte UART frame and does not carry a numeric "
                "speed value."
            )
        )
        self.speed_button.clicked.connect(
            self.send_rc_speed
        )

        pg.addWidget(
            self.speed_button,
            3,
            0,
            1,
            3,
        )

        controls.addWidget(
            pan_group
        )

        # Manual / Auto pan.
        #
        # Firmware protocol exposes only one TOGGLE frame (RMCMD 50), not
        # separate Manual and Auto selection commands.
        mode_group = QGroupBox(
            "Pan Mode"
        )
        mode_group.setObjectName(
            "controlGroup"
        )

        mg = QVBoxLayout(
            mode_group
        )
        mg.setContentsMargins(
            10, 14, 10, 10
        )
        mg.setSpacing(
            7
        )

        self.mode_toggle_button = QPushButton(
            "Manual / Auto Toggle"
        )
        self.mode_toggle_button.setObjectName(
            "modeButton"
        )
        self.mode_toggle_button.clicked.connect(
            self.toggle_pan_mode
        )

        mode_note = QLabel(
            "RC_MANUAL_AUTO • code 50"
        )
        mode_note.setObjectName(
            "fieldLabel"
        )
        mode_note.setWordWrap(
            True
        )

        mg.addWidget(
            self.mode_toggle_button
        )
        mg.addWidget(
            mode_note
        )

        controls.addWidget(
            mode_group
        )

        # Lighting.
        #
        # Firmware exposes RC_LED as a toggle; no explicit ON/OFF command is
        # defined in the supplied protocol.
        light_group = QGroupBox(
            "Lighting"
        )
        light_group.setObjectName(
            "controlGroup"
        )

        lg = QVBoxLayout(
            light_group
        )
        lg.setContentsMargins(
            10, 14, 10, 10
        )
        lg.setSpacing(
            7
        )

        self.light_toggle_button = QPushButton(
            "Lighting Toggle"
        )
        self.light_toggle_button.setObjectName(
            "lightButton"
        )
        self.light_toggle_button.clicked.connect(
            self.toggle_lighting
        )

        light_note = QLabel(
            "RC_LED • code 51"
        )
        light_note.setObjectName(
            "fieldLabel"
        )
        light_note.setWordWrap(
            True
        )

        lg.addWidget(
            self.light_toggle_button
        )
        lg.addWidget(
            light_note
        )

        controls.addWidget(
            light_group
        )

        # Command-port / RC protocol status.
        protocol_group = QGroupBox(
            "Control Status"
        )
        protocol_group.setObjectName(
            "controlGroup"
        )

        sg = QVBoxLayout(
            protocol_group
        )
        sg.setContentsMargins(
            10, 14, 10, 10
        )

        self.command_connection_label = QLabel(
            (
                "Command: connecting to "
                f"{self.command_host}:{self.command_port}"
            )
        )
        self.command_connection_label.setObjectName(
            "statusText"
        )
        self.command_connection_label.setWordWrap(
            True
        )

        self.core_health_label = QLabel(
            "OBS Core: checking heartbeat..."
        )
        self.core_health_label.setObjectName("statusText")
        self.core_health_label.setWordWrap(True)

        self.control_status_label = QLabel(
            (
                "RC panel ready\n"
                "Manual/Auto and LED are protocol toggles; "
                "hardware state feedback is not defined."
            )
        )
        self.control_status_label.setObjectName(
            "statusText"
        )
        self.control_status_label.setWordWrap(
            True
        )

        self.reconnect_command_button = QPushButton(
            "Reconnect Command Port"
        )
        self.reconnect_command_button.setObjectName(
            "secondaryButton"
        )
        self.reconnect_command_button.clicked.connect(
            self.reconnect_command_client
        )

        sg.addWidget(
            self.command_connection_label
        )
        sg.addWidget(
            self.core_health_label
        )
        sg.addWidget(
            self.control_status_label
        )
        sg.addWidget(
            self.reconnect_command_button
        )

        controls.addWidget(
            protocol_group
        )
        controls.addStretch(
            1
        )

        # Put the complete settings/control column inside its own vertical
        # scroll area. Horizontal scrolling is deliberately disabled so the
        # panel always follows the right splitter width.
        self.control_scroll = QScrollArea()
        self.control_scroll.setObjectName(
            "controlScroll"
        )
        self.control_scroll.setWidgetResizable(
            True
        )
        self.control_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.control_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.control_scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )
        self.control_scroll.setWidget(
            control_panel
        )

        splitter.addWidget(
            self.control_scroll
        )

        splitter.setStretchFactor(
            0,
            5,
        )
        splitter.setStretchFactor(
            1,
            1,
        )
        splitter.setSizes(
            [1200, 240]
        )

        root.addWidget(
            splitter,
            1,
        )

        # --------------------------------------------------------------
        # Future OBS protocol hooks.
        # Use pressed/released so later PTZ motion can start while a button
        # is held and stop when released.
        # --------------------------------------------------------------
        self.pan_up_button.pressed.connect(
            lambda: self.on_pan_pressed(
                "UP"
            )
        )
        self.pan_down_button.pressed.connect(
            lambda: self.on_pan_pressed(
                "DOWN"
            )
        )
        self.pan_left_button.pressed.connect(
            lambda: self.on_pan_pressed(
                "LEFT"
            )
        )
        self.pan_right_button.pressed.connect(
            lambda: self.on_pan_pressed(
                "RIGHT"
            )
        )

        self.pan_up_button.released.connect(
            lambda: self.on_pan_released(
                "UP"
            )
        )
        self.pan_down_button.released.connect(
            lambda: self.on_pan_released(
                "DOWN"
            )
        )
        self.pan_left_button.released.connect(
            lambda: self.on_pan_released(
                "LEFT"
            )
        )
        self.pan_right_button.released.connect(
            lambda: self.on_pan_released(
                "RIGHT"
            )
        )

        self.pan_stop_button.clicked.connect(
            self.on_pan_stop
        )

    def on_overlay_toggled(
        self,
        checked: bool,
    ) -> None:
        self.overlay_enabled = bool(
            checked
        )

        if self.overlay_enabled:
            self.record_status_label.setToolTip(
                "HUD overlay enabled for preview and recorded MP4."
            )
        else:
            self.record_status_label.setToolTip(
                "HUD overlay disabled. Preview and recorded MP4 are clean."
            )

    # ------------------------------------------------------------------ multimedia

    def _initialize_multimedia(self) -> None:
        self.capture_session = QMediaCaptureSession()

        # v11: prefer a dedicated QVideoSink.  It is the authoritative frame
        # endpoint for BOTH preview and recording.
        self._initialize_frame_recording_sink()

        if self.video_sink is not None:
            try:
                self.capture_session.setVideoSink(self.video_sink)
            except Exception as exc:
                self.video_sink = None
                self.cv_record_capable = False
                self.record_last_error = f"Cannot attach QVideoSink: {exc}"

        if self.video_sink is None and self.video_widget is not None:
            # Old Qt/display-only path.  On Windows recording will deliberately
            # remain unavailable rather than falling back to zero-byte MP4s.
            try:
                self.capture_session.setVideoOutput(self.video_widget)
            except Exception:
                pass

        self._initialize_recorder()

        # Keep an instance alive so hot-plug signals remain available.
        self.media_devices = (
            QMediaDevices()
        )

        try:
            self.media_devices.videoInputsChanged.connect(
                self.refresh_camera_list
            )
        except Exception:
            pass

        QTimer.singleShot(
            0,
            self.refresh_camera_list,
        )

    def _initialize_frame_recording_sink(self) -> None:
        self.video_sink = None
        self.cv_record_capable = False

        if not QT_VIDEO_SINK_AVAILABLE or QVideoSink is None:
            self.record_last_error = (
                "QVideoSink is unavailable"
                + (f": {QT_VIDEO_SINK_ERROR}" if QT_VIDEO_SINK_ERROR else "")
            )
            return

        try:
            self.video_sink = QVideoSink(self)
            self.video_sink.videoFrameChanged.connect(self._on_video_frame_from_sink)
            self.cv_record_capable = bool(CV_RECORDER_AVAILABLE)
            if not CV_RECORDER_AVAILABLE:
                self.record_last_error = (
                    "OpenCV/NumPy recorder unavailable"
                    + (f": {CV_RECORDER_ERROR}" if CV_RECORDER_ERROR else "")
                )
        except Exception as exc:
            self.video_sink = None
            self.cv_record_capable = False
            self.record_last_error = str(exc)

    def _on_video_frame_from_sink(self, frame) -> None:
        """Receive the authoritative camera frame for preview and recording."""
        try:
            if not frame.isValid():
                return
            image = frame.toImage()
            if image is None or image.isNull():
                return

            # Detach from the QVideoFrame lifetime.  A single safe copy is used for
            # the next preview paint and, when active, copied again by the writer
            # queue before crossing to its background thread.
            safe_image = image.copy()

            if self.overlay_enabled:
                self._draw_video_telemetry_overlay(
                    safe_image
                )

            self.latest_preview_image = safe_image

            # Measure the real frame-delivery rate from QVideoSink.  Update over a
            # short rolling window so the number is useful to the operator without
            # reacting to every individual scheduling jitter.
            now = time.monotonic()
            self.sink_frame_count += 1
            self.sink_rate_window_count += 1
            self.sink_last_frame_monotonic = now
            elapsed = now - self.sink_rate_window_start
            if elapsed >= 0.75:
                self.sink_fps = self.sink_rate_window_count / max(elapsed, 1e-6)
                self.sink_rate_window_start = now
                self.sink_rate_window_count = 0

            worker = self.cv_writer_worker
            if (
                worker is not None
                and self.record_session_in_progress
                and self.record_ui_state in ("starting", "recording", "resuming")
            ):
                worker.enqueue(safe_image)

        except Exception as exc:
            if not self.record_last_error:
                self.record_last_error = str(exc)

    def refresh_video_overlay_telemetry(self) -> None:
        shared = self.shared
        if shared is None:
            self.overlay_has_telemetry = False
            self.overlay_imu_live = False
            self.overlay_depth_live = False
            return
        try:
            telemetry = shared.read_telemetry()
            self.overlay_depth_m = float(telemetry.pressure)
            self.overlay_roll_deg = float(telemetry.roll)
            self.overlay_pitch_deg = float(telemetry.pitch)
            self.overlay_yaw_deg = wrap_angle_deg(
                float(
                    telemetry.yaw
                )
            )
            self.overlay_has_telemetry = True
            try:
                health = shared.read_acquisition_health()
                imu_age = float(health.source_age_s("ahrs"))
                depth_age = float(health.source_age_s("depth"))
                self.overlay_imu_live = bool(
                    math.isfinite(imu_age) and imu_age <= OVERLAY_SOURCE_STALE_S
                )
                self.overlay_depth_live = bool(
                    math.isfinite(depth_age) and depth_age <= OVERLAY_SOURCE_STALE_S
                )
            except Exception:
                self.overlay_imu_live = False
                self.overlay_depth_live = False
        except Exception:
            self.overlay_has_telemetry = False
            self.overlay_imu_live = False
            self.overlay_depth_live = False

    def _draw_video_telemetry_overlay(
        self,
        image: QImage,
    ) -> None:
        """Draw the Version 16 operational marine HUD."""
        if image is None or image.isNull():
            return

        painter = None

        try:
            width = int(image.width())
            height = int(image.height())

            if width <= 0 or height <= 0:
                return

            scale = max(
                0.78,
                min(
                    1.55,
                    min(
                        width / 1280.0,
                        height / 720.0,
                    ),
                ),
            )

            margin = max(10, int(round(16 * scale)))
            pad_x = max(8, int(round(10 * scale)))
            pad_y = max(5, int(round(7 * scale)))
            gap = max(3, int(round(4 * scale)))
            radius = max(4, int(round(6 * scale)))

            px_label = max(10, int(round(12 * scale)))
            px_value = max(15, int(round(19 * scale)))
            px_major = max(22, int(round(28 * scale)))
            px_depth = max(24, int(round(31 * scale)))
            px_time = max(12, int(round(15 * scale)))

            # ----------------------------------------------------------
            # Values and freshness
            # ----------------------------------------------------------
            timestamp_text = time.strftime(
                "%Y%m%d_%H%M%S"
            )

            if self.overlay_has_telemetry:
                depth_value = f"{self.overlay_depth_m:.2f} m"
                roll_value = f"{self.overlay_roll_deg:+.1f}°"
                pitch_value = f"{self.overlay_pitch_deg:+.1f}°"
                yaw_value = f"{self.overlay_yaw_deg:05.1f}°"

                if self.overlay_imu_live and self.overlay_depth_live:
                    status_text = "LIVE"
                    status_mode = "live"
                elif self.overlay_imu_live or self.overlay_depth_live:
                    status_text = "PARTIAL"
                    status_mode = "partial"
                else:
                    status_text = "STALE"
                    status_mode = "stale"

            else:
                depth_value = "--.-- m"
                roll_value = "--.-°"
                pitch_value = "--.-°"
                yaw_value = "---.-°"
                status_text = "NO DATA"
                status_mode = "stale"

            # ----------------------------------------------------------
            # Painter / font setup
            # ----------------------------------------------------------
            painter = QPainter(image)
            painter.setRenderHint(
                QPainter.RenderHint.TextAntialiasing,
                True,
            )

            font_label = QFont("Consolas")
            font_label.setPixelSize(px_label)
            font_label.setBold(True)

            font_value = QFont("Consolas")
            font_value.setPixelSize(px_value)
            font_value.setBold(True)

            font_major = QFont("Consolas")
            font_major.setPixelSize(px_major)
            font_major.setBold(True)

            font_depth = QFont("Consolas")
            font_depth.setPixelSize(px_depth)
            font_depth.setBold(True)

            font_time = QFont("Consolas")
            font_time.setPixelSize(px_time)
            font_time.setBold(True)

            # Modern marine palette.
            panel_bg = QColor(3, 12, 18, 132)
            panel_bg_strong = QColor(3, 12, 18, 158)
            border = QColor(112, 190, 220, 118)
            main_text = QColor(240, 248, 252)
            secondary_text = QColor(157, 188, 201)
            accent_text = QColor(112, 219, 255)
            live_text = QColor(120, 235, 180)
            warn_text = QColor(255, 202, 108)

            if status_mode == "live":
                status_color = live_text
            elif status_mode == "partial":
                status_color = warn_text
            else:
                status_color = warn_text

            def draw_panel(x, y, w, h, strong=False):
                painter.setPen(border)
                painter.setBrush(
                    panel_bg_strong
                    if strong
                    else panel_bg
                )
                painter.drawRoundedRect(
                    int(x),
                    int(y),
                    int(w),
                    int(h),
                    radius,
                    radius,
                )

            # ----------------------------------------------------------
            # TOP LEFT — compact source/status
            # ----------------------------------------------------------
            painter.setFont(font_label)
            fm_label = painter.fontMetrics()

            status_label = f"● {status_text}"
            status_w = (
                fm_label.horizontalAdvance(status_label)
                + 2 * pad_x
            )
            status_h = fm_label.height() + 2 * pad_y

            draw_panel(
                margin,
                margin,
                status_w,
                status_h,
                False,
            )

            painter.setPen(status_color)
            painter.drawText(
                margin + pad_x,
                margin + pad_y + fm_label.ascent(),
                status_label,
            )

            # ----------------------------------------------------------
            # TOP CENTER — timestamp
            # ----------------------------------------------------------
            painter.setFont(font_time)
            fm_time = painter.fontMetrics()

            time_w = (
                fm_time.horizontalAdvance(timestamp_text)
                + 2 * pad_x
            )
            time_h = fm_time.height() + 2 * pad_y
            time_x = int((width - time_w) / 2)

            draw_panel(
                time_x,
                margin,
                time_w,
                time_h,
                False,
            )

            painter.setPen(main_text)
            painter.drawText(
                time_x + pad_x,
                margin + pad_y + fm_time.ascent(),
                timestamp_text,
            )

            # ----------------------------------------------------------
            # TOP RIGHT — heading / yaw
            # ----------------------------------------------------------
            painter.setFont(font_major)
            fm_major = painter.fontMetrics()
            painter.setFont(font_label)

            yaw_label = "YAW"
            yaw_w = max(
                fm_label.horizontalAdvance(yaw_label),
                fm_major.horizontalAdvance(yaw_value),
            ) + 2 * pad_x

            yaw_h = (
                fm_label.height()
                + fm_major.height()
                + 2 * pad_y
                + gap
            )

            yaw_x = width - margin - yaw_w

            draw_panel(
                yaw_x,
                margin,
                yaw_w,
                yaw_h,
                True,
            )

            painter.setFont(font_label)
            painter.setPen(secondary_text)
            painter.drawText(
                yaw_x + pad_x,
                margin + pad_y + fm_label.ascent(),
                yaw_label,
            )

            painter.setFont(font_major)
            painter.setPen(
                main_text
                if self.overlay_imu_live
                else warn_text
            )
            painter.drawText(
                yaw_x + pad_x,
                margin
                + pad_y
                + fm_label.height()
                + gap
                + fm_major.ascent(),
                yaw_value,
            )

            # ----------------------------------------------------------
            # BOTTOM LEFT — dominant depth
            # ----------------------------------------------------------
            painter.setFont(font_depth)
            fm_depth = painter.fontMetrics()
            painter.setFont(font_label)

            depth_label = "DEPTH"
            depth_w = max(
                fm_label.horizontalAdvance(depth_label),
                fm_depth.horizontalAdvance(depth_value),
            ) + 2 * pad_x

            depth_h = (
                fm_label.height()
                + fm_depth.height()
                + 2 * pad_y
                + gap
            )

            depth_y = height - margin - depth_h

            draw_panel(
                margin,
                depth_y,
                depth_w,
                depth_h,
                True,
            )

            painter.setFont(font_label)
            painter.setPen(secondary_text)
            painter.drawText(
                margin + pad_x,
                depth_y + pad_y + fm_label.ascent(),
                depth_label,
            )

            painter.setFont(font_depth)
            painter.setPen(
                accent_text
                if self.overlay_depth_live
                else warn_text
            )
            painter.drawText(
                margin + pad_x,
                depth_y
                + pad_y
                + fm_label.height()
                + gap
                + fm_depth.ascent(),
                depth_value,
            )

            # ----------------------------------------------------------
            # BOTTOM RIGHT — roll/pitch attitude
            # ----------------------------------------------------------
            painter.setFont(font_value)
            fm_value = painter.fontMetrics()

            rp_lines = (
                ("ROLL", roll_value),
                ("PITCH", pitch_value),
            )

            label_col_w = max(
                fm_label.horizontalAdvance("ROLL"),
                fm_label.horizontalAdvance("PITCH"),
            )

            value_col_w = max(
                fm_value.horizontalAdvance(roll_value),
                fm_value.horizontalAdvance(pitch_value),
            )

            rp_w = (
                pad_x
                + label_col_w
                + max(10, int(round(12 * scale)))
                + value_col_w
                + pad_x
            )

            line_h = max(
                fm_label.height(),
                fm_value.height(),
            )

            rp_h = (
                2 * line_h
                + gap
                + 2 * pad_y
            )

            rp_x = width - margin - rp_w
            rp_y = height - margin - rp_h

            draw_panel(
                rp_x,
                rp_y,
                rp_w,
                rp_h,
                False,
            )

            label_x = rp_x + pad_x
            value_x = (
                label_x
                + label_col_w
                + max(10, int(round(12 * scale)))
            )

            row_baseline = rp_y + pad_y

            for index, (label, value) in enumerate(rp_lines):
                y_top = (
                    row_baseline
                    + index * (line_h + gap)
                )

                painter.setFont(font_label)
                painter.setPen(secondary_text)
                painter.drawText(
                    label_x,
                    y_top + fm_label.ascent(),
                    label,
                )

                painter.setFont(font_value)
                painter.setPen(
                    main_text
                    if self.overlay_imu_live
                    else warn_text
                )
                painter.drawText(
                    value_x,
                    y_top + fm_value.ascent(),
                    value,
                )

            # ----------------------------------------------------------
            # Minimal corner reticle marks for a marine-camera feel.
            # They are subtle and do not obstruct the center image.
            # ----------------------------------------------------------
            painter.setPen(
                QColor(112, 190, 220, 90)
            )

            mark = max(12, int(round(18 * scale)))
            inset = max(8, int(round(12 * scale)))

            # top-left
            painter.drawLine(
                inset, inset,
                inset + mark, inset,
            )
            painter.drawLine(
                inset, inset,
                inset, inset + mark,
            )

            # top-right
            painter.drawLine(
                width - inset - mark, inset,
                width - inset, inset,
            )
            painter.drawLine(
                width - inset, inset,
                width - inset, inset + mark,
            )

            # bottom-left
            painter.drawLine(
                inset, height - inset,
                inset + mark, height - inset,
            )
            painter.drawLine(
                inset, height - inset - mark,
                inset, height - inset,
            )

            # bottom-right
            painter.drawLine(
                width - inset - mark, height - inset,
                width - inset, height - inset,
            )
            painter.drawLine(
                width - inset, height - inset - mark,
                width - inset, height - inset,
            )

        except Exception:
            pass

        finally:
            try:
                if (
                    painter is not None
                    and painter.isActive()
                ):
                    painter.end()
            except Exception:
                pass

    def _render_latest_preview_frame(self) -> None:
        if not self.preview_uses_direct_sink:
            return
        if self.video_widget is None:
            return
        image = self.latest_preview_image
        if image is None or image.isNull():
            return
        try:
            target = self.video_widget.size()
            if target.width() <= 1 or target.height() <= 1:
                return
            pixmap = QPixmap.fromImage(image)
            pixmap = pixmap.scaled(
                target,
                Qt.KeepAspectRatio,
                Qt.FastTransformation,
            )
            self.video_widget.setPixmap(pixmap)
        except Exception:
            pass

    def _camera_record_fps(self) -> float:
        """Return the best constant MP4 frame rate available at record start.

        Prefer the measured QVideoSink delivery rate after preview has warmed up.
        This prevents a nominal 60-FPS camera mode that is actually delivering
        ~30 FPS from being written as a 60-FPS file (which would play too fast).
        """
        advertised_fps = CAMERA_SMOOTH_FPS
        if self.camera is not None:
            try:
                camera_format = self.camera.cameraFormat()
                value = float(camera_format.maxFrameRate())
                if value > 0.0:
                    advertised_fps = value
            except Exception:
                pass

        advertised_fps = max(
            1.0,
            min(float(advertised_fps), CAMERA_MAX_USEFUL_FPS),
        )

        measured_fps = float(self.sink_fps)
        if measured_fps >= 5.0:
            # Never claim a recording rate above the advertised camera mode.
            return max(
                1.0,
                min(measured_fps, advertised_fps, CAMERA_MAX_USEFUL_FPS),
            )
        return advertised_fps

    def _initialize_recorder(self) -> None:
        # Preferred/reliable path: OpenCV writes the exact frames received by the
        # dedicated QVideoSink.
        if self.cv_record_capable and self.video_sink is not None:
            self.media_recorder = None
            self.record_ui_state = "stopped"
            self.record_session_in_progress = False
            self.record_last_error = ""
            if hasattr(self, "record_status_label"):
                self.record_status_label.setText(
                    "Recorder ready (DIRECT QVideoSink -> OpenCV MP4V)"
                )
            self._update_record_controls()
            return

        # On Windows, do NOT silently fall back to QMediaRecorder.  The exact
        # failure this application is protecting against is a recorder that
        # reports state transitions yet leaves an empty MP4 for some USB/UVC
        # devices.  Make the missing deterministic path explicit instead.
        if os.name == "nt":
            self.media_recorder = None
            self.record_ui_state = "error"
            self.record_session_in_progress = False
            detail = self.record_last_error or (
                "Reliable recorder requires QVideoSink + opencv-python + numpy"
            )
            if hasattr(self, "record_status_label"):
                self.record_status_label.setText(
                    "Recording unavailable: " + detail
                )
            self._update_record_controls()
            return

        if (
            not QT_RECORDER_AVAILABLE
            or self.capture_session is None
        ):
            self.media_recorder = None
            self.record_ui_state = "stopped"
            self.record_session_in_progress = False

            if hasattr(self, "record_status_label"):
                self.record_status_label.setText(
                    "Recorder unavailable"
                    + (
                        f": {QT_RECORDER_ERROR}"
                        if QT_RECORDER_ERROR
                        else ""
                    )
                )
            return

        try:
            self.media_recorder = QMediaRecorder()
            self.capture_session.setRecorder(self.media_recorder)

            # Prefer an MP4 container.  Do not force a video codec: Qt chooses
            # an encoder supported by the active multimedia backend.
            if QMediaFormat is not None:
                try:
                    media_format = QMediaFormat()
                    media_format.setFileFormat(
                        QMediaFormat.FileFormat.MPEG4
                    )
                    self.media_recorder.setMediaFormat(media_format)
                except Exception:
                    # Backend default is safer than failing recorder startup.
                    pass

            try:
                self.media_recorder.recorderStateChanged.connect(
                    self.on_recorder_state_changed
                )
            except Exception:
                pass

            try:
                self.media_recorder.durationChanged.connect(
                    self.on_record_duration_changed
                )
            except Exception:
                pass

            try:
                self.media_recorder.actualLocationChanged.connect(
                    self.on_record_actual_location_changed
                )
            except Exception:
                pass

            try:
                self.media_recorder.errorOccurred.connect(
                    self.on_recorder_error
                )
            except Exception:
                pass

            self.record_ui_state = "stopped"
            self.record_session_in_progress = False
            self.record_last_error = ""
            self.record_status_label.setText("Recorder ready")
            self._update_record_controls()

        except Exception as exc:
            self.media_recorder = None
            self.record_ui_state = "error"
            self.record_session_in_progress = False
            self.record_last_error = str(exc)
            self.record_status_label.setText(
                f"Recorder initialization failed: {exc}"
            )
            self._update_record_controls()

    def _actual_recorder_state(self):
        if self.media_recorder is None:
            return None
        try:
            return self.media_recorder.recorderState()
        except Exception:
            return None

    @staticmethod
    def _state_name(state) -> str:
        if state is None:
            return "UnknownState"
        name = getattr(state, "name", None)
        if name:
            return str(name)
        return str(state)

    def _actual_state_is(self, member_name: str) -> bool:
        state = self._actual_recorder_state()
        if state is None:
            return False

        # Prefer enum comparison because string representation differs across
        # PySide6/Qt releases (e.g. RecordingState vs RecorderState.RecordingState).
        try:
            enum_type = getattr(QMediaRecorder, "RecorderState", None)
            member = getattr(enum_type, member_name, None)
            if member is not None and state == member:
                return True
        except Exception:
            pass

        return member_name in self._state_name(state)

    def _recorder_state_name(self) -> str:
        return self._state_name(self._actual_recorder_state())

    def _actual_is_recording(self) -> bool:
        return self._actual_state_is("RecordingState")

    def _actual_is_paused(self) -> bool:
        return self._actual_state_is("PausedState")

    def _actual_is_stopped(self) -> bool:
        state = self._actual_recorder_state()
        if state is None:
            return True
        if self._actual_state_is("StoppedState"):
            return True
        name = self._state_name(state)
        return (
            "RecordingState" not in name
            and "PausedState" not in name
        )

    def _is_recording(self) -> bool:
        if self.record_ui_state in ("starting", "recording", "resuming"):
            return True
        return self._actual_is_recording()

    def _is_record_paused(self) -> bool:
        if self.record_ui_state in ("pausing", "paused"):
            return True
        return self._actual_is_paused()

    def _is_recorder_stopped(self) -> bool:
        if self.record_session_in_progress:
            return False
        if self.record_ui_state in (
            "starting", "recording", "resuming",
            "pausing", "paused", "stopping",
        ):
            return False
        return self._actual_is_stopped()

    def _record_session_active(self) -> bool:
        return bool(
            self.record_session_in_progress
            or self.record_ui_state in (
                "starting", "recording", "resuming",
                "pausing", "paused", "stopping",
            )
            or self._actual_is_recording()
            or self._actual_is_paused()
        )

    def _recorder_error_text(self) -> str:
        if self.media_recorder is None:
            return "Recorder unavailable"
        try:
            value = str(self.media_recorder.errorString() or "").strip()
            if value:
                return value
        except Exception:
            pass
        return str(self.record_last_error or "").strip()

    def _update_record_controls(self) -> None:
        recorder_ok = bool(
            self.cv_record_capable
            or self.media_recorder is not None
        )

        camera_active = False
        if self.camera is not None:
            try:
                camera_active = bool(self.camera.isActive())
            except Exception:
                camera_active = True

        active = self._record_session_active()
        paused = self._is_record_paused()
        stopping = self.record_ui_state == "stopping"

        self.record_button.setEnabled(
            recorder_ok
            and camera_active
            and not active
        )

        # Pause/Resume becomes available immediately after record() is issued.
        # This intentionally does not wait for an asynchronous stateChanged signal.
        self.pause_record_button.setEnabled(
            recorder_ok
            and active
            and not stopping
        )

        # Most importantly, Stop is enabled from the moment a record session is
        # requested, even if recorderState() still reports StoppedState briefly.
        self.stop_record_button.setEnabled(
            recorder_ok
            and active
            and not stopping
        )

        self.pause_record_button.setText(
            "Resume" if paused else "Pause"
        )

        self.camera_combo.setEnabled(not active)
        self.refresh_button.setEnabled(not active)

    def choose_record_folder(self) -> None:
        start_folder = str(self.record_folder)

        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose Video Recording Folder",
            start_folder,
        )

        if not folder:
            return

        self.record_folder = Path(folder)

        try:
            self.record_folder.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                APP_TITLE,
                f"Cannot use recording folder:\n\n{exc}",
            )
            return

        self.record_folder_edit.setText(str(self.record_folder))

    def _next_record_path(self) -> Path:
        try:
            self.record_folder.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception:
            pass

        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_name = f"OBS_CAMERA_{stamp}"
        path = self.record_folder / (base_name + ".mp4")

        counter = 1
        while path.exists():
            path = self.record_folder / (
                f"{base_name}_{counter:02d}.mp4"
            )
            counter += 1

        return path

    def _start_cv_recording(self, path: Path) -> None:
        fps = self._camera_record_fps()

        # Always encode to a local temporary file first.  The operator may
        # choose a mapped/network drive (for example S:\...).  Media encoders
        # and real-time frame writers are much more reliable on a local path.
        # Only after VideoWriter.release() finalizes the MP4 do we copy it to
        # the requested destination.
        staging_dir = (
            Path(tempfile.gettempdir())
            / "GRC_UGM_PERTAMINA_OBS_CAMERA"
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / (
            f"{path.stem}_{uuid.uuid4().hex[:8]}.mp4"
        )

        self.current_record_path = path
        self.cv_staging_path = staging_path
        self.record_last_error = ""
        self.record_session_in_progress = True
        self.record_ui_state = "starting"
        self.cv_paused_total_s = 0.0
        self.cv_pause_started_monotonic = None
        self.cv_record_start_monotonic = time.monotonic()

        worker = CVVideoWriterWorker(
            staging_path,
            fps=fps,
            queue_size=4,
        )
        self.cv_writer_worker = worker
        worker.start()

        self.record_ui_state = "recording"
        self.record_status_label.setText(
            f"REC 00:00:00  •  {fps:.1f} FPS\n{path.name}"
        )
        self.cv_record_timer.start()
        self._update_record_controls()

        QTimer.singleShot(
            1500,
            lambda w=worker: self._verify_cv_record_started(w),
        )

    def _verify_cv_record_started(self, worker) -> None:
        if worker is not self.cv_writer_worker:
            return
        if not self.record_session_in_progress:
            return
        if worker.error_text:
            self.record_last_error = worker.error_text
            self._stop_cv_recording(
                final_status_prefix="Recording failed"
            )
            return
        if worker.frames_written <= 0:
            self.record_last_error = (
                "No video frames reached the recorder. Preview may be active, "
                "but QVideoSink frame delivery is unavailable."
            )
            self._stop_cv_recording(
                final_status_prefix="Recording failed"
            )

    def _refresh_cv_record_status(self) -> None:
        worker = self.cv_writer_worker
        if worker is None or not self.record_session_in_progress:
            self.cv_record_timer.stop()
            return

        if worker.error_text:
            self.record_last_error = worker.error_text
            self._stop_cv_recording(
                final_status_prefix="Recording failed"
            )
            return

        now = time.monotonic()
        elapsed = max(0.0, now - self.cv_record_start_monotonic)
        elapsed -= self.cv_paused_total_s
        if self.cv_pause_started_monotonic is not None:
            elapsed -= max(0.0, now - self.cv_pause_started_monotonic)
        elapsed = max(0, int(elapsed))

        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        state_text = "PAUSED" if self._is_record_paused() else "REC"
        filename = (
            self.current_record_path.name
            if self.current_record_path is not None
            else "video"
        )
        active_elapsed = max(0.001, float(elapsed))
        writer_fps = float(worker.frames_written) / active_elapsed
        staging_mb = 0.0
        staging_path = self.cv_staging_path
        if staging_path is not None:
            try:
                if staging_path.exists():
                    staging_mb = staging_path.stat().st_size / (1024.0 * 1024.0)
            except Exception:
                staging_mb = 0.0

        capture_fps = max(0.0, float(self.sink_fps))
        self.record_status_label.setText(
            f"{state_text} {hours:02d}:{minutes:02d}:{seconds:02d}  •  "
            f"Capture {capture_fps:.1f} FPS  •  Write {writer_fps:.1f} FPS\n"
            f"Frames {worker.frames_written:,}  •  Drop {worker.frames_dropped:,}  •  "
            f"{staging_mb:.1f} MB\n"
            f"{filename}"
        )

    def _stop_cv_recording(
        self,
        final_status_prefix: str = "Saved",
    ) -> None:
        worker = self.cv_writer_worker
        path = self.current_record_path

        self.record_ui_state = "stopping"
        self.cv_record_timer.stop()
        self.record_status_label.setText("Finalizing recording...")
        self._update_record_controls()
        QApplication.processEvents()

        if self.cv_pause_started_monotonic is not None:
            self.cv_paused_total_s += max(
                0.0,
                time.monotonic() - self.cv_pause_started_monotonic,
            )
            self.cv_pause_started_monotonic = None

        stopped = True
        if worker is not None:
            stopped = worker.stop(timeout_s=5.0)

        error_text = self.record_last_error
        frames_written = 0
        frames_dropped = 0
        if worker is not None:
            frames_written = int(worker.frames_written)
            frames_dropped = int(worker.frames_dropped)
            if worker.error_text:
                error_text = worker.error_text

        self.cv_writer_worker = None
        self.record_session_in_progress = False

        if not stopped:
            self.record_ui_state = "error"
            self.record_status_label.setText(
                "Recorder finalization timeout"
            )
            self._update_record_controls()
            return

        staging_path = self.cv_staging_path
        self.cv_staging_path = None

        staging_size = 0
        if staging_path is not None:
            try:
                if staging_path.exists():
                    staging_size = int(staging_path.stat().st_size)
            except Exception:
                staging_size = 0

        if error_text or frames_written <= 0 or staging_size <= 0:
            self.record_ui_state = "error"
            # A zero-byte MP4 is not useful and can be mistaken for a valid
            # recording. Remove it after the writer has been released.
            if staging_path is not None:
                try:
                    staging_path.unlink(missing_ok=True)
                except Exception:
                    pass

            detail = error_text or (
                "No video frames were written"
                if frames_written <= 0
                else "Local staging MP4 is empty"
            )
            self.record_status_label.setText(
                f"{final_status_prefix}: {detail}"
            )
            self._update_record_controls()
            return

        # The MP4 is now finalized locally. Copy the complete file to the
        # operator-selected destination. This prevents mapped/network drives
        # from seeing a half-open/zero-byte container during recording.
        try:
            if path is None:
                raise RuntimeError("Destination path is unavailable")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(staging_path), str(path))
            final_size = int(path.stat().st_size)
            if final_size <= 0:
                raise RuntimeError("Copied destination file is empty")
            staging_path.unlink(missing_ok=True)
        except Exception as exc:
            self.record_ui_state = "error"
            self.record_status_label.setText(
                "Recording finalized locally, but copy to Save Folder failed:\n"
                f"{exc}\nLocal file: {staging_path}"
            )
            self._update_record_controls()
            return

        self.record_ui_state = "stopped"
        mb = final_size / (1024.0 * 1024.0)
        self.record_status_label.setText(
            f"Saved: {path.name} ({mb:.1f} MB, "
            f"{frames_written:,} frames, drop {frames_dropped:,})"
        )
        self._update_record_controls()

    def start_recording(self) -> None:
        if not self.cv_record_capable and self.media_recorder is None:
            detail = CV_RECORDER_ERROR or QT_RECORDER_ERROR
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Video recorder is unavailable.\n\n"
                "Install/repair: pip install opencv-python numpy PySide6"
                + (f"\n\n{detail}" if detail else ""),
            )
            return

        if self.camera is None:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Select and start a camera before recording.",
            )
            return

        try:
            if not self.camera.isActive():
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "The selected camera is not active.",
                )
                return
        except Exception:
            pass

        if self._record_session_active():
            return

        path = self._next_record_path()

        if self.cv_record_capable:
            try:
                self._start_cv_recording(path)
            except Exception as exc:
                self.record_session_in_progress = False
                self.record_ui_state = "error"
                self.record_last_error = str(exc)
                self.cv_writer_worker = None
                self.record_status_label.setText(
                    f"Record start failed: {exc}"
                )
                self._update_record_controls()
            return

        try:
            self.media_recorder.setOutputLocation(
                QUrl.fromLocalFile(str(path))
            )

            self.current_record_path = path
            self.record_last_error = ""
            self.record_session_in_progress = True
            self.record_ui_state = "starting"
            self.record_stop_verify_generation += 1
            self._update_record_controls()

            self.media_recorder.record()

            # record() is an asynchronous request.  Treat the session as active
            # immediately so Pause/Stop cannot become trapped in disabled state.
            self.record_ui_state = "recording"
            self.record_status_label.setText(
                f"Recording: {path.name}"
            )
            self._update_record_controls()

            generation = self.record_stop_verify_generation
            QTimer.singleShot(
                1000,
                lambda g=generation: self._verify_record_started(g),
            )

        except Exception as exc:
            self.record_session_in_progress = False
            self.record_ui_state = "error"
            self.record_last_error = str(exc)
            self.current_record_path = None
            QMessageBox.critical(
                self,
                APP_TITLE,
                f"Cannot start video recording:\n\n{exc}",
            )
            self.record_status_label.setText(
                f"Record start failed: {exc}"
            )
            self._update_record_controls()

    def _verify_record_started(self, generation: int) -> None:
        if generation != self.record_stop_verify_generation:
            return
        if not self.record_session_in_progress:
            return
        if self.record_ui_state in ("stopping", "stopped", "error"):
            return

        if self._actual_is_recording() or self._actual_is_paused():
            return

        error_text = self._recorder_error_text()
        if error_text:
            self.record_session_in_progress = False
            self.record_ui_state = "error"
            self.record_status_label.setText(
                f"Recorder did not start: {error_text}"
            )
            self._update_record_controls()
            return

        # If the backend still says StoppedState after a full second, the record
        # request was not accepted.  Do not leave a fake active session in the UI.
        if self._actual_is_stopped():
            self.record_session_in_progress = False
            self.record_ui_state = "error"
            self.record_status_label.setText(
                "Recorder did not enter RecordingState. "
                "Check the Qt multimedia backend/encoder."
            )
            self._update_record_controls()

    def pause_or_resume_recording(self) -> None:
        if self.cv_record_capable and self.cv_writer_worker is not None:
            if not self._record_session_active():
                return
            if self.record_ui_state == "stopping":
                return

            now = time.monotonic()
            if self._is_record_paused():
                if self.cv_pause_started_monotonic is not None:
                    self.cv_paused_total_s += max(
                        0.0, now - self.cv_pause_started_monotonic
                    )
                self.cv_pause_started_monotonic = None
                self.record_ui_state = "recording"
                self.record_status_label.setText("Recording resumed")
            else:
                self.cv_pause_started_monotonic = now
                self.record_ui_state = "paused"
                self.record_status_label.setText("Recording paused")
            self._update_record_controls()
            return

        if self.media_recorder is None:
            return
        if not self._record_session_active():
            return
        if self.record_ui_state == "stopping":
            return

        try:
            if self._is_record_paused():
                self.record_ui_state = "resuming"
                self.media_recorder.record()
                self.record_ui_state = "recording"
                self.record_status_label.setText("Recording resumed")
            else:
                self.record_ui_state = "pausing"
                self.media_recorder.pause()
                self.record_ui_state = "paused"
                self.record_status_label.setText("Recording paused")

            self._update_record_controls()

        except Exception as exc:
            self.record_last_error = str(exc)
            self.record_status_label.setText(
                f"Pause/resume failed: {exc}"
            )
            self._update_record_controls()

    def stop_recording(self) -> None:
        if self.cv_record_capable and self.cv_writer_worker is not None:
            if not self._record_session_active():
                self._update_record_controls()
                return
            self._stop_cv_recording()
            return

        if self.media_recorder is None:
            return

        # Do NOT gate Stop on the instantaneous recorderState().  Some Windows
        # backends remain in StoppedState briefly after record() while already
        # creating the target file.
        if not self._record_session_active():
            self._update_record_controls()
            return

        try:
            self.record_ui_state = "stopping"
            self.record_stop_verify_generation += 1
            generation = self.record_stop_verify_generation

            self.record_status_label.setText("Finalizing recording...")
            self._update_record_controls()
            self.media_recorder.stop()

            # State change/finalization is asynchronous.  Report Saved only once
            # the recorder is stopped and the output file is non-empty.
            QTimer.singleShot(
                100,
                lambda g=generation: self._verify_record_stopped(g, 0),
            )

        except Exception as exc:
            self.record_last_error = str(exc)
            self.record_ui_state = "error"
            self.record_session_in_progress = False
            self.record_status_label.setText(
                f"Stop recording failed: {exc}"
            )
            self._update_record_controls()

    def _verify_record_stopped(self, generation: int, attempt: int) -> None:
        if generation != self.record_stop_verify_generation:
            return

        if self.media_recorder is None:
            return

        if self._actual_is_stopped():
            self.record_ui_state = "stopped"
            self.record_session_in_progress = False
            QTimer.singleShot(250, self._finalize_recording_status)
            self._update_record_controls()
            return

        error_text = self._recorder_error_text()
        if error_text:
            self.record_last_error = error_text

        if attempt < 30:
            QTimer.singleShot(
                100,
                lambda g=generation, a=attempt + 1: self._verify_record_stopped(g, a),
            )
            return

        # Last retry after ~3 s.  Ask the backend to stop once more and make the
        # failure explicit rather than claiming that the MP4 was saved.
        try:
            self.media_recorder.stop()
        except Exception:
            pass

        self.record_ui_state = "error"
        self.record_session_in_progress = False
        self.record_status_label.setText(
            "Recorder stop/finalization timeout"
            + (f": {error_text}" if error_text else "")
        )
        self._update_record_controls()

    def _finalize_recording_status(self) -> None:
        path = self.current_record_path
        if path is None:
            self.record_status_label.setText("Recording stopped")
            return

        try:
            if path.exists():
                size = int(path.stat().st_size)
                if size > 0:
                    mb = size / (1024.0 * 1024.0)
                    self.record_status_label.setText(
                        f"Saved: {path.name} ({mb:.1f} MB)"
                    )
                else:
                    self.record_status_label.setText(
                        f"Recording stopped, but file is empty: {path.name}"
                    )
            else:
                self.record_status_label.setText(
                    f"Recording stopped, output file not found: {path.name}"
                )
        except Exception as exc:
            self.record_status_label.setText(
                f"Recording stopped; cannot verify file: {exc}"
            )

    def _stop_recording_blocking(self, timeout_ms: int = 3000) -> None:
        """Stop/finalize before detaching the camera source."""
        if self.cv_record_capable and self.cv_writer_worker is not None:
            self._stop_cv_recording()
            return

        if self.media_recorder is None:
            return
        if not self._record_session_active():
            return

        self.record_ui_state = "stopping"
        self.record_stop_verify_generation += 1
        try:
            self.media_recorder.stop()
        except Exception as exc:
            self.record_last_error = str(exc)
            return

        deadline = time.monotonic() + max(0.25, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            if self._actual_is_stopped():
                self.record_ui_state = "stopped"
                self.record_session_in_progress = False
                self._finalize_recording_status()
                self._update_record_controls()
                return
            try:
                QApplication.processEvents()
            except Exception:
                pass
            time.sleep(0.02)

        self.record_ui_state = "error"
        self.record_session_in_progress = False
        self.record_status_label.setText(
            "Recorder finalization timeout while stopping camera"
        )
        self._update_record_controls()

    def on_recorder_state_changed(self, state) -> None:
        name = self._state_name(state)

        if "RecordingState" in name:
            self.record_ui_state = "recording"
            self.record_session_in_progress = True
            if self.current_record_path is not None:
                self.record_status_label.setText(
                    f"Recording: {self.current_record_path.name}"
                )

        elif "PausedState" in name:
            self.record_ui_state = "paused"
            self.record_session_in_progress = True
            self.record_status_label.setText("Recording paused")

        elif "StoppedState" in name:
            was_active = self.record_session_in_progress
            self.record_ui_state = "stopped"
            self.record_session_in_progress = False
            if was_active:
                QTimer.singleShot(250, self._finalize_recording_status)

        self._update_record_controls()

    def on_record_duration_changed(self, duration_ms: int) -> None:
        if not self._record_session_active():
            return
        if self.record_ui_state == "stopping":
            return

        seconds = max(0, int(duration_ms) // 1000)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        state_text = "PAUSED" if self._is_record_paused() else "REC"
        filename = (
            self.current_record_path.name
            if self.current_record_path is not None
            else "video"
        )
        self.record_status_label.setText(
            f"{state_text} {hours:02d}:{minutes:02d}:{secs:02d}\n"
            f"{filename}"
        )

    def on_record_actual_location_changed(self, location) -> None:
        try:
            local_file = location.toLocalFile()
            if local_file:
                self.current_record_path = Path(local_file)
        except Exception:
            pass

    def on_recorder_error(self, *args) -> None:
        message = "Recorder error"
        for value in reversed(args):
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break

        self.record_last_error = message
        self.record_ui_state = "error"
        self.record_session_in_progress = False
        self.record_status_label.setText(message)
        self._update_record_controls()

    def _camera_format_info(self, camera_format):
        """Return normalized details for one QCameraFormat."""

        size = camera_format.resolution()
        width = max(0, int(size.width()))
        height = max(0, int(size.height()))

        try:
            min_fps = float(camera_format.minFrameRate())
        except Exception:
            min_fps = 0.0

        try:
            max_fps = float(camera_format.maxFrameRate())
        except Exception:
            max_fps = 0.0

        pixel_format = camera_format.pixelFormat()

        pixel_text = "Unknown"
        if QVideoFrameFormat is not None:
            try:
                pixel_text = str(
                    QVideoFrameFormat.pixelFormatToString(pixel_format)
                )
            except Exception:
                pass

        if not pixel_text or pixel_text == "Unknown":
            try:
                pixel_text = str(pixel_format.name)
            except Exception:
                pixel_text = str(pixel_format)

        return {
            "format": camera_format,
            "width": width,
            "height": height,
            "min_fps": min_fps,
            "max_fps": max_fps,
            "pixel": pixel_text,
        }

    def _camera_format_score(self, info) -> float:
        """Score a camera mode for smooth USB/UVC live monitoring."""

        width = info["width"]
        height = info["height"]
        max_fps = info["max_fps"]
        pixel = info["pixel"].upper()

        target_pixels = CAMERA_TARGET_WIDTH * CAMERA_TARGET_HEIGHT
        pixels = max(1, width * height)

        score = 0.0

        # A stable >=30 FPS mode is the first priority.
        if max_fps >= CAMERA_SMOOTH_FPS - 0.5:
            score += 100000.0
        else:
            score += max(0.0, max_fps) * 1000.0

        # For a USB camera, MJPEG/JPEG greatly reduces bus bandwidth compared
        # with uncompressed YUY2/YUV at the same resolution and frame rate.
        if "JPEG" in pixel or "MJPEG" in pixel or "MJPG" in pixel:
            score += 50000.0

        # Prefer useful frame rates, but do not let extreme advertised FPS
        # dominate format selection.
        score += min(max(0.0, max_fps), CAMERA_MAX_USEFUL_FPS) * 1000.0

        # Prefer a resolution near 1280x720. This is large enough for the UI
        # while avoiding unnecessary USB/CPU load from 2K/4K capture modes.
        ratio = min(pixels, target_pixels) / max(pixels, target_pixels)
        score += ratio * 20000.0

        # A 16:9 mode usually matches the main monitor region and avoids extra
        # scaling work. This is only a tie-breaker, not a hard requirement.
        if height > 0:
            aspect = width / height
            score += max(0.0, 1.0 - abs(aspect - (16.0 / 9.0))) * 1000.0

        # Strongly discourage very large modes for live monitoring unless they
        # are the only usable formats advertised by the camera.
        if pixels > 1920 * 1080:
            score -= 30000.0

        return score

    def _select_smooth_camera_format(self, device):
        """Choose and return the best advertised format for smooth live view."""

        try:
            formats = list(device.videoFormats())
        except Exception:
            formats = []

        if not formats:
            self.current_camera_format_text = "Driver default format"
            return None

        infos = [self._camera_format_info(fmt) for fmt in formats]
        best = max(infos, key=self._camera_format_score)

        fps = best["max_fps"]
        fps_text = f"{fps:.2f}".rstrip("0").rstrip(".")
        self.current_camera_format_text = (
            f'{best["width"]}x{best["height"]} @ {fps_text} FPS '
            f'• {best["pixel"]}'
        )

        return best["format"]

    def refresh_camera_list(self) -> None:
        if not QT_MULTIMEDIA_AVAILABLE:
            return

        current_id = None

        current_index = (
            self.camera_combo.currentIndex()
        )

        if (
            0 <= current_index
            < len(
                self.camera_devices
            )
        ):
            try:
                current_id = bytes(
                    self.camera_devices[
                        current_index
                    ].id()
                )
            except Exception:
                current_id = None

        try:
            devices = list(
                self.media_devices.videoInputs()
                if self.media_devices is not None
                else QMediaDevices.videoInputs()
            )
        except Exception as exc:
            self.camera_status_label.setText(
                f"Cannot enumerate cameras: {exc}"
            )
            return

        self.camera_combo.blockSignals(
            True
        )

        self.camera_combo.clear()
        self.camera_devices = devices

        selected_index = -1

        for index, device in enumerate(
            devices
        ):
            try:
                name = str(
                    device.description()
                )
            except Exception:
                name = (
                    f"Camera {index}"
                )

            if not name.strip():
                name = (
                    f"Camera {index}"
                )

            self.camera_combo.addItem(
                name
            )

            if current_id is not None:
                try:
                    if bytes(
                        device.id()
                    ) == current_id:
                        selected_index = index
                except Exception:
                    pass

        if not devices:
            self.camera_combo.blockSignals(
                False
            )
            self.camera_status_label.setText(
                "No camera device detected"
            )
            self._stop_camera()
            self._show_video_placeholder(
                "No Camera Device Detected"
            )
            return

        if selected_index < 0:
            selected_index = 0

        # Keep signals blocked while restoring the selection. Otherwise
        # currentIndexChanged() opens the camera here and start_camera() below
        # immediately closes/reopens it a second time.
        self.camera_combo.setCurrentIndex(
            selected_index
        )
        self.camera_combo.blockSignals(
            False
        )

        self.camera_status_label.setText(
            f"{len(devices)} camera device(s) available"
        )

        self.start_camera(
            selected_index
        )

    def on_camera_selected(
        self,
        index: int,
    ) -> None:
        if index < 0:
            return

        self.start_camera(
            index
        )

    def start_camera(
        self,
        index: int,
    ) -> None:
        if (
            not QT_MULTIMEDIA_AVAILABLE
            or self.capture_session is None
        ):
            return

        if (
            index < 0
            or index >= len(
                self.camera_devices
            )
        ):
            return

        self._stop_camera()

        device = self.camera_devices[
            index
        ]

        try:
            self.camera = QCamera(
                device
            )

            try:
                self.camera.errorOccurred.connect(
                    self.on_camera_error
                )
            except Exception:
                pass

            try:
                self.camera.activeChanged.connect(
                    self.on_camera_active_changed
                )
            except Exception:
                pass

            # Do not accept Qt/driver's implicit default capture format. Some
            # USB cameras default to a high-resolution uncompressed mode or a
            # low frame-rate mode, which looks choppy even though the same
            # camera is smooth in its vendor application.
            selected_format = self._select_smooth_camera_format(
                device
            )
            if selected_format is not None:
                try:
                    self.camera.setCameraFormat(
                        selected_format
                    )
                except Exception:
                    self.current_camera_format_text = (
                        "Driver default format"
                    )

            self.capture_session.setCamera(
                self.camera
            )

            self.camera.start()

            name = str(
                device.description()
            )

            backend = os.environ.get(
                "QT_MEDIA_BACKEND",
                "default",
            )

            if self.cv_record_capable and self.video_sink is not None:
                recorder_backend = "DIRECT QVideoSink -> OpenCV MP4V"
            elif os.name == "nt":
                recorder_backend = "UNAVAILABLE (no QMediaRecorder fallback)"
            else:
                recorder_backend = "Qt QMediaRecorder fallback"
            self.camera_status_label.setText(
                f"Starting: {name}\n"
                f"Video: {self.current_camera_format_text}\n"
                f"Backend: {backend}  •  Record: {recorder_backend}"
            )

            self._show_video_widget()

        except Exception as exc:
            self.camera_status_label.setText(
                f"Cannot start camera: {exc}"
            )
            self._show_video_placeholder(
                "Camera Start Failed"
            )

    def _stop_camera(self) -> None:
        if self._record_session_active():
            # Finalize the active OpenCV/Qt recording before detaching QCamera.
            self._stop_recording_blocking()

        if self.camera is not None:
            try:
                self.camera.stop()
            except Exception:
                pass

            try:
                if self.capture_session is not None:
                    self.capture_session.setCamera(
                        None
                    )
            except Exception:
                pass

            try:
                self.camera.deleteLater()
            except Exception:
                pass

            self.camera = None

        # Reset delivery diagnostics so a newly selected camera starts with a
        # clean measured-rate window instead of inheriting the previous device.
        self.sink_frame_count = 0
        self.sink_rate_window_count = 0
        self.sink_rate_window_start = time.monotonic()
        self.sink_fps = 0.0
        self.sink_last_frame_monotonic = 0.0

    def on_camera_error(
        self,
        _error,
        error_string: str,
    ) -> None:
        text = (
            str(error_string).strip()
            or "Camera error"
        )

        self.camera_status_label.setText(
            text
        )

        self._show_video_placeholder(
            "Camera Error"
        )

    def on_camera_active_changed(
        self,
        active: bool,
    ) -> None:
        if active:
            index = (
                self.camera_combo.currentIndex()
            )

            name = (
                self.camera_combo.currentText()
                if index >= 0
                else "Camera"
            )

            self.camera_status_label.setText(
                f"Active: {name}\n"
                f"Video: {self.current_camera_format_text}"
            )

            self._show_video_widget()
            self._update_record_controls()

        else:
            if self.camera is not None:
                self.camera_status_label.setText(
                    "Camera inactive"
                )

            self._update_record_controls()

    def _show_video_widget(self) -> None:
        if self.video_widget is not None:
            self.video_placeholder.hide()
            self.video_widget.show()
            if self.preview_uses_direct_sink and not self.preview_timer.isActive():
                self.preview_timer.start()

    def _show_video_placeholder(
        self,
        text: str,
    ) -> None:
        if self.video_widget is not None:
            self.video_widget.hide()
        if self.preview_timer.isActive():
            self.preview_timer.stop()
        self.latest_preview_image = None

        self.video_placeholder.setText(
            text
        )
        self.video_placeholder.show()

    # ------------------------------------------------------------------ OBS RC camera protocol

    def _start_command_client(
        self,
    ) -> None:
        self._stop_command_client()

        self.command_host = "OBS Setting IPC"
        self.command_port = 0
        self.command_connected = False

        if hasattr(
            self,
            "command_connection_label",
        ):
            self.command_connection_label.setText(
                (
                    "Command: connecting to OBS Setting local IPC"
                )
            )

        worker = OBSCommandClientThread(
            self.command_host,
            self.command_port,
            self,
        )

        worker.connection_changed.connect(
            self.on_command_connection_changed
        )

        worker.sentence_sent.connect(
            self.on_command_sentence_sent
        )

        worker.error_occurred.connect(
            self.on_command_error
        )

        self.command_thread = worker

        worker.start()

    def _stop_command_client(
        self,
    ) -> None:
        worker = self.command_thread

        if worker is None:
            return

        self.command_thread = None
        self.command_connected = False

        try:
            worker.stop()
            worker.wait(
                3000
            )
        except Exception:
            pass

        try:
            worker.deleteLater()
        except Exception:
            pass

    def refresh_acquisition_health(self) -> None:
        if self.shared is None:
            self.core_health_label.setText("OBS Core: shared RAM unavailable")
            return
        try:
            health = self.shared.read_acquisition_health()
            age = health.heartbeat_age_s()
            state = "LIVE" if health.is_alive() else "STALE"
            self.core_health_label.setText(
                f"OBS Core: {state} • HB {age:.1f}s • "
                f"CMD {'ON' if health.command_connected else 'OFF'} • "
                f"DATA {'ON' if health.data_connected else 'OFF'}"
            )
        except Exception as exc:
            self.core_health_label.setText(f"OBS Core health error: {exc}")

    def reconnect_command_client(
        self,
    ) -> None:
        self.control_status_label.setText(
            "Reconnecting to OBS Setting local command broker..."
        )

        self._start_command_client()

    def on_command_connection_changed(
        self,
        connected: bool,
        detail: str,
    ) -> None:
        self.command_connected = bool(
            connected
        )

        self.command_connection_label.setText(
            (
                "Command CONNECTED • "
                if self.command_connected
                else "Command DISCONNECTED • "
            )
            + str(
                detail
            )
        )

    def on_command_sentence_sent(
        self,
        sentence: str,
    ) -> None:
        self.last_rc_sentence = str(
            sentence
        )

        self.control_status_label.setText(
            (
                "TX: "
                f"{self.last_rc_sentence}\n"
                "Command sent by OBS Setting through the single TCP 54300 link."
            )
        )

    def on_command_error(
        self,
        message: str,
    ) -> None:
        if self.command_connected:
            return

        self.control_status_label.setText(
            (
                "OBS Setting command-broker error:\n"
                f"{message}"
            )
        )

    def send_rc_command(
        self,
        command_code: int,
        description: str,
    ) -> bool:
        """
        Send only while OBS Setting reports its single OBS TCP 54300 link connected.

        Commands are never retained across an IPC/OBS disconnect because a delayed
        stale camera PRESS command after reconnect would be unsafe.
        """

        worker = self.command_thread

        if (
            not self.command_connected
            or worker is None
            or not worker.isRunning()
        ):
            self.control_status_label.setText(
                (
                    f"{description} NOT SENT\n"
                    "OBS Setting / OBS TCP 54300 is not connected."
                )
            )
            return False

        try:
            sentence = build_rc_sentence(
                command_code
            )

        except ValueError as exc:
            self.control_status_label.setText(
                str(
                    exc
                )
            )
            return False

        worker.queue_sentence(
            sentence
        )

        self.control_status_label.setText(
            (
                f"Queued {description}\n"
                f"{sentence.decode('ascii').strip()}"
            )
        )

        return True

    def toggle_pan_mode(
        self,
    ) -> None:
        self.send_rc_command(
            RC_MANUAL_AUTO,
            "Manual/Auto Toggle",
        )

    def toggle_lighting(
        self,
    ) -> None:
        self.send_rc_command(
            RC_LED,
            "Lighting Toggle",
        )

    def send_rc_speed(
        self,
    ) -> None:
        self.send_rc_command(
            RC_SPEED,
            "RC Speed",
        )

    def on_pan_pressed(
        self,
        direction: str,
    ) -> None:
        direction = (
            str(
                direction
            )
            .upper()
            .strip()
        )

        command_code = (
            RC_PRESS_CODES.get(
                direction
            )
        )

        if command_code is None:
            self.control_status_label.setText(
                (
                    "Unknown pan direction: "
                    f"{direction}"
                )
            )
            return

        if self.send_rc_command(
            command_code,
            f"{direction} PRESS",
        ):
            self.active_pan_direction = (
                direction
            )

    def on_pan_released(
        self,
        direction: str,
    ) -> None:
        direction = (
            str(
                direction
            )
            .upper()
            .strip()
        )

        command_code = (
            RC_DEPRESS_CODES.get(
                direction
            )
        )

        if command_code is None:
            return

        self.send_rc_command(
            command_code,
            f"{direction} DEPRESS",
        )

        if (
            self.active_pan_direction
            == direction
        ):
            self.active_pan_direction = None

    def on_pan_stop(
        self,
    ) -> None:
        """
        Stop both motion axes.

        No dedicated RMCMD STOP code was supplied. The firmware table shows:
            UP/DOWN depress    -> same vertical UART stop frame
            LEFT/RIGHT depress -> same horizontal UART stop frame

        Therefore STOP sends one vertical depress and one horizontal depress.
        """

        if not self.command_connected:
            self.control_status_label.setText(
                (
                    "STOP NOT SENT\n"
                    "OBS command port is not connected."
                )
            )
            return

        sent_vertical = self.send_rc_command(
            RC_STOP_ALL_CODES[0],
            "VERTICAL DEPRESS",
        )

        sent_horizontal = self.send_rc_command(
            RC_STOP_ALL_CODES[1],
            "HORIZONTAL DEPRESS",
        )

        if (
            sent_vertical
            and sent_horizontal
        ):
            self.active_pan_direction = None

            self.control_status_label.setText(
                (
                    "STOP ALL queued\n"
                    f"{build_rc_sentence(RC_STOP_ALL_CODES[0]).decode('ascii').strip()}  +  "
                    f"{build_rc_sentence(RC_STOP_ALL_CODES[1]).decode('ascii').strip()}"
                )
            )

    # ------------------------------------------------------------------ style

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background-color: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QFrame#videoFrame,
            QFrame#videoStack {
                background-color: #000000;
                border: none;
            }

            QVideoWidget#videoWidget,
            QLabel#videoWidget {
                background-color: #000000;
                border: none;
            }

            QLabel#videoPlaceholder {
                background-color: #000000;
                color: #607987;
                font-size: 15px;
                border: none;
            }

            QFrame#controlPanel {
                background-color: #07131D;
                border-left: 1px solid #18384B;
            }

            QScrollArea#controlScroll {
                background-color: #07131D;
                border: none;
            }

            QScrollArea#controlScroll > QWidget > QWidget {
                background-color: #07131D;
            }

            QScrollArea#controlScroll QScrollBar:vertical {
                background-color: #07131D;
                width: 10px;
                margin: 0px;
                border: none;
            }

            QScrollArea#controlScroll QScrollBar::handle:vertical {
                background-color: #24485D;
                min-height: 28px;
                border-radius: 5px;
            }

            QScrollArea#controlScroll QScrollBar::handle:vertical:hover {
                background-color: #315F78;
            }

            QScrollArea#controlScroll QScrollBar::add-line:vertical,
            QScrollArea#controlScroll QScrollBar::sub-line:vertical,
            QScrollArea#controlScroll QScrollBar::add-page:vertical,
            QScrollArea#controlScroll QScrollBar::sub-page:vertical {
                background: transparent;
                height: 0px;
            }

            QGroupBox#controlGroup {
                background-color: #0D1E2A;
                border: 1px solid #1A3D52;
                border-radius: 9px;
                margin-top: 11px;
                padding-top: 7px;
                color: #FFFFFF;
                font-weight: 800;
            }

            QGroupBox#controlGroup::title {
                subcontrol-origin: margin;
                left: 9px;
                padding: 0px 5px;
                color: #FFFFFF;
            }

            QLabel {
                color: #FFFFFF;
                background: transparent;
            }

            QLabel#fieldLabel {
                color: #AFC5D0;
                font-size: 10px;
            }

            QLabel#statusText {
                color: #93AAB6;
                font-size: 10px;
            }

            QCheckBox#overlayCheckBox {
                color: #D7E7EF;
                spacing: 7px;
                font-weight: 700;
                padding: 3px 2px;
            }

            QCheckBox#overlayCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #3F7089;
                border-radius: 3px;
                background-color: #071620;
            }

            QCheckBox#overlayCheckBox::indicator:checked {
                background-color: #17678F;
                border: 1px solid #52B7E5;
            }

            QComboBox,
            QLineEdit {
                background-color: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 6px;
                min-height: 29px;
                padding: 2px 7px;
            }

            QComboBox QAbstractItemView {
                background-color: #0B1B26;
                color: #F4FAFD;
                border: 1px solid #2B526A;
                selection-background-color: #245B79;
                selection-color: #FFFFFF;
                outline: none;
            }

            QPushButton {
                min-height: 31px;
                border-radius: 7px;
                padding: 4px 8px;
                font-weight: 700;
                background-color: #162D3A;
                color: #DDEAF2;
                border: 1px solid #2A4E62;
            }

            QPushButton:hover {
                background-color: #1C3A4A;
                border: 1px solid #39708B;
            }

            QPushButton:pressed {
                background-color: #205A77;
            }

            QPushButton#secondaryButton {
                background-color: #123147;
                border: 1px solid #285B78;
            }

            QPushButton#directionButton {
                font-size: 20px;
                background-color: #123147;
                border: 1px solid #285B78;
            }

            QPushButton#stopButton {
                background-color: #4A2529;
                border: 1px solid #814049;
                color: #FFD2D8;
                font-size: 10px;
            }

            QPushButton#recordButton {
                background-color: #6A1F29;
                border: 1px solid #B84454;
                color: #FFD7DC;
                padding-left: 4px;
                padding-right: 4px;
            }

            QPushButton#pauseRecordButton {
                background-color: #5A4B18;
                border: 1px solid #9A8128;
                color: #FFF0A8;
                padding-left: 4px;
                padding-right: 4px;
            }

            QPushButton#stopRecordButton {
                background-color: #26313A;
                border: 1px solid #526673;
                color: #E4EEF3;
                padding-left: 4px;
                padding-right: 4px;
            }

            QPushButton#modeButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #35A0D0;
            }

            QPushButton#lightButton {
                background-color: #5A501D;
                color: #FFF3A6;
                border: 1px solid #B19B31;
            }

            QPushButton:disabled {
                background-color: #101D25;
                color: #50636E;
                border: 1px solid #21323C;
            }

            QSplitter::handle {
                background-color: #17374A;
                width: 2px;
            }
            """
        )

    # ------------------------------------------------------------------ close

    def closeEvent(
        self,
        event: QCloseEvent,
    ) -> None:
        # Stop RC command client first so no camera motion command can be
        # transmitted while the GUI is closing.
        self._stop_command_client()

        try:
            self.health_timer.stop()
        except Exception:
            pass
        try:
            self.overlay_timer.stop()
        except Exception:
            pass
        try:
            self.preview_timer.stop()
        except Exception:
            pass
        if self.shared is not None:
            try:
                self.shared.close()
            except Exception:
                pass

        # _stop_camera() also stops and finalizes an active recording first.
        self._stop_camera()
        event.accept()


# =============================================================================
# Main
# =============================================================================

def _startup_log_path() -> Path:
    try:
        base = RUNTIME_DIR
    except Exception:
        base = Path.cwd()
    return base / "camera_startup_error.log"


def _write_startup_error(exc: BaseException) -> Path:
    path = _startup_log_path()
    try:
        path.write_text(
            "OBS Camera startup/runtime error\n"
            "================================\n\n"
            + "".join(
                traceback.format_exception(
                    type(exc),
                    exc,
                    exc.__traceback__,
                )
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return path



def main() -> int:
    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        APP_TITLE
    )
    app.setApplicationDisplayName(
        APP_TITLE
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

    try:
        window = CameraWindow()
        window.show()

    except BaseException as exc:
        log_path = _write_startup_error(exc)

        try:
            QMessageBox.critical(
                None,
                APP_TITLE,
                "Camera module failed to start.\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                f"Diagnostic log:\n{log_path}",
            )
        except Exception:
            pass

        return 1

    return app.exec()


def _camera_excepthook(exc_type, exc_value, exc_tb):
    if exc_value is None:
        exc_value = RuntimeError(str(exc_type))

    try:
        exc_value.__traceback__ = exc_tb
    except Exception:
        pass

    log_path = _write_startup_error(exc_value)

    try:
        QMessageBox.critical(
            None,
            APP_TITLE,
            "Unhandled Camera runtime error.\n\n"
            f"{exc_type.__name__}: {exc_value}\n\n"
            f"Diagnostic log:\n{log_path}",
        )
    except Exception:
        pass


sys.excepthook = _camera_excepthook


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
