"""
camera.py
=========

GRC-UGM-PERTAMINA OBS
Camera Monitor / PTZ Control UI

Version: 3

Current scope
-------------
- Enumerate available hardware camera devices using Qt Multimedia.
- Select a camera from the device list.
- Display live video in the left 3/4 of the window.
- Record live camera video to a selectable folder:
    * Record
    * Pause / Resume Record
    * Stop Record
- Prepare UI controls for future OBS camera protocol:
    * Pan Up / Down / Left / Right
    * Pan Stop
    * Editable Speed
    * Manual / Auto Pan
    * Lighting On / Off

The PTZ / lighting buttons currently update only the local UI/status.
No OBS command is transmitted yet.

Dependencies
------------
    pip install PySide6

PySide6 QtMultimedia / QtMultimediaWidgets must be available.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


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

        # Hide console when launched through python.exe.
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

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import (
        QCamera,
        QMediaCaptureSession,
        QMediaDevices,
    )
    from PySide6.QtMultimediaWidgets import QVideoWidget

    QT_MULTIMEDIA_AVAILABLE = True
    QT_MULTIMEDIA_ERROR = ""

except Exception as exc:
    QCamera = None
    QMediaCaptureSession = None
    QMediaDevices = None
    QVideoWidget = None

    QT_MULTIMEDIA_AVAILABLE = False
    QT_MULTIMEDIA_ERROR = str(exc)

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


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Camera"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

DEFAULT_SPEED = "50"
DEFAULT_RECORD_FOLDER = BASE_DIR / "recordings" / "video"


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

        self.media_recorder = None
        self.current_record_path = None
        self.record_folder = Path(DEFAULT_RECORD_FOLDER)

        try:
            self.record_folder.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception:
            # Folder can still be selected manually from the UI.
            pass

        self.manual_mode = True
        self.lighting_on = False

        self._build_ui()
        self._apply_style()

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
            self.video_widget = QVideoWidget()
            self.video_widget.setObjectName("videoWidget")

            try:
                self.video_widget.setAspectRatioMode(
                    Qt.KeepAspectRatio
                )
            except Exception:
                pass

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

        speed_label = QLabel(
            "Speed"
        )
        speed_label.setObjectName(
            "fieldLabel"
        )

        self.speed_edit = QLineEdit(
            DEFAULT_SPEED
        )
        self.speed_edit.setObjectName(
            "speedEdit"
        )
        self.speed_edit.setPlaceholderText(
            "Speed"
        )

        pg.addWidget(
            speed_label,
            3,
            0,
        )
        pg.addWidget(
            self.speed_edit,
            3,
            1,
            1,
            2,
        )

        controls.addWidget(
            pan_group
        )

        # Manual / Auto pan.
        mode_group = QGroupBox(
            "Pan Mode"
        )
        mode_group.setObjectName(
            "controlGroup"
        )

        mg = QHBoxLayout(
            mode_group
        )
        mg.setContentsMargins(
            10, 14, 10, 10
        )
        mg.setSpacing(7)

        self.manual_button = QPushButton(
            "Manual"
        )
        self.auto_button = QPushButton(
            "Auto Pan"
        )

        self.manual_button.setObjectName(
            "modeButton"
        )
        self.auto_button.setObjectName(
            "modeButton"
        )

        self.manual_button.setCheckable(
            True
        )
        self.auto_button.setCheckable(
            True
        )

        self.mode_group = QButtonGroup(
            self
        )
        self.mode_group.setExclusive(
            True
        )

        self.mode_group.addButton(
            self.manual_button,
            0,
        )
        self.mode_group.addButton(
            self.auto_button,
            1,
        )

        self.manual_button.setChecked(
            True
        )

        self.manual_button.clicked.connect(
            lambda: self.set_pan_mode(
                "manual"
            )
        )
        self.auto_button.clicked.connect(
            lambda: self.set_pan_mode(
                "auto"
            )
        )

        mg.addWidget(
            self.manual_button
        )
        mg.addWidget(
            self.auto_button
        )

        controls.addWidget(
            mode_group
        )

        # Lighting.
        light_group = QGroupBox(
            "Lighting"
        )
        light_group.setObjectName(
            "controlGroup"
        )

        lg = QHBoxLayout(
            light_group
        )
        lg.setContentsMargins(
            10, 14, 10, 10
        )
        lg.setSpacing(7)

        self.light_on_button = QPushButton(
            "Lighting ON"
        )
        self.light_off_button = QPushButton(
            "Lighting OFF"
        )

        self.light_on_button.setObjectName(
            "lightButton"
        )
        self.light_off_button.setObjectName(
            "lightButton"
        )

        self.light_on_button.setCheckable(
            True
        )
        self.light_off_button.setCheckable(
            True
        )

        self.light_group = QButtonGroup(
            self
        )
        self.light_group.setExclusive(
            True
        )

        self.light_group.addButton(
            self.light_on_button,
            1,
        )
        self.light_group.addButton(
            self.light_off_button,
            0,
        )

        self.light_off_button.setChecked(
            True
        )

        self.light_on_button.clicked.connect(
            lambda: self.set_lighting(
                True
            )
        )
        self.light_off_button.clicked.connect(
            lambda: self.set_lighting(
                False
            )
        )

        lg.addWidget(
            self.light_on_button
        )
        lg.addWidget(
            self.light_off_button
        )

        controls.addWidget(
            light_group
        )

        # Future protocol status.
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

        self.control_status_label = QLabel(
            "Manual Pan • Lighting OFF\n"
            "OBS control protocol: not connected yet"
        )
        self.control_status_label.setObjectName(
            "statusText"
        )
        self.control_status_label.setWordWrap(
            True
        )

        sg.addWidget(
            self.control_status_label
        )

        controls.addWidget(
            protocol_group
        )
        controls.addStretch(
            1
        )

        splitter.addWidget(
            control_panel
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
            self.on_pan_released
        )
        self.pan_down_button.released.connect(
            self.on_pan_released
        )
        self.pan_left_button.released.connect(
            self.on_pan_released
        )
        self.pan_right_button.released.connect(
            self.on_pan_released
        )

        self.pan_stop_button.clicked.connect(
            self.on_pan_stop
        )

    # ------------------------------------------------------------------ multimedia

    def _initialize_multimedia(self) -> None:
        self.capture_session = (
            QMediaCaptureSession()
        )

        if self.video_widget is not None:
            self.capture_session.setVideoOutput(
                self.video_widget
            )

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

    def _initialize_recorder(self) -> None:
        if (
            not QT_RECORDER_AVAILABLE
            or self.capture_session is None
        ):
            self.media_recorder = None

            if hasattr(
                self,
                "record_status_label",
            ):
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
            self.capture_session.setRecorder(
                self.media_recorder
            )

            # Prefer MP4. Codec remains backend-selected so the application is
            # more portable across Windows / Linux Qt multimedia backends.
            if QMediaFormat is not None:
                try:
                    media_format = QMediaFormat()
                    media_format.setFileFormat(
                        QMediaFormat.FileFormat.MPEG4
                    )
                    self.media_recorder.setMediaFormat(
                        media_format
                    )
                except Exception:
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

            self.record_status_label.setText(
                "Recorder ready"
            )
            self._update_record_controls()

        except Exception as exc:
            self.media_recorder = None
            self.record_status_label.setText(
                f"Recorder initialization failed: {exc}"
            )
            self._update_record_controls()

    def _recorder_state_name(self) -> str:
        if self.media_recorder is None:
            return "StoppedState"

        try:
            state = self.media_recorder.recorderState()

            name = getattr(
                state,
                "name",
                None,
            )

            if name:
                return str(name)

            return str(state)

        except Exception:
            return "StoppedState"

    def _is_recording(self) -> bool:
        return "RecordingState" in self._recorder_state_name()

    def _is_record_paused(self) -> bool:
        return "PausedState" in self._recorder_state_name()

    def _is_recorder_stopped(self) -> bool:
        state = self._recorder_state_name()
        return (
            "StoppedState" in state
            or (
                "RecordingState" not in state
                and "PausedState" not in state
            )
        )

    def _update_record_controls(self) -> None:
        recorder_ok = self.media_recorder is not None

        camera_active = False
        if self.camera is not None:
            try:
                camera_active = bool(
                    self.camera.isActive()
                )
            except Exception:
                camera_active = True

        stopped = self._is_recorder_stopped()
        recording = self._is_recording()
        paused = self._is_record_paused()

        self.record_button.setEnabled(
            recorder_ok
            and camera_active
            and stopped
        )

        self.pause_record_button.setEnabled(
            recorder_ok
            and (
                recording
                or paused
            )
        )

        self.stop_record_button.setEnabled(
            recorder_ok
            and not stopped
        )

        self.pause_record_button.setText(
            "Resume"
            if paused
            else "Pause"
        )

        # Avoid changing the active camera while recording because doing so can
        # terminate or corrupt the current recording on some multimedia backends.
        self.camera_combo.setEnabled(
            not (
                recording
                or paused
            )
        )
        self.refresh_button.setEnabled(
            not (
                recording
                or paused
            )
        )

    def choose_record_folder(self) -> None:
        start_folder = str(
            self.record_folder
        )

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

        self.record_folder_edit.setText(
            str(self.record_folder)
        )

    def _next_record_path(self) -> Path:
        try:
            self.record_folder.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception:
            pass

        stamp = time.strftime(
            "%Y%m%d_%H%M%S"
        )

        base_name = (
            f"OBS_CAMERA_{stamp}"
        )

        path = self.record_folder / (
            base_name + ".mp4"
        )

        counter = 1
        while path.exists():
            path = self.record_folder / (
                f"{base_name}_{counter:02d}.mp4"
            )
            counter += 1

        return path

    def start_recording(self) -> None:
        if self.media_recorder is None:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Video recorder is not available in this Qt multimedia backend.",
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

        if not self._is_recorder_stopped():
            return

        path = self._next_record_path()

        try:
            self.media_recorder.setOutputLocation(
                QUrl.fromLocalFile(
                    str(path)
                )
            )

            self.current_record_path = path
            self.media_recorder.record()

            self.record_status_label.setText(
                f"Recording: {path.name}"
            )
            self._update_record_controls()

        except Exception as exc:
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

    def pause_or_resume_recording(self) -> None:
        if self.media_recorder is None:
            return

        try:
            if self._is_record_paused():
                self.media_recorder.record()
                self.record_status_label.setText(
                    "Recording resumed"
                )

            elif self._is_recording():
                self.media_recorder.pause()
                self.record_status_label.setText(
                    "Recording paused"
                )

            self._update_record_controls()

        except Exception as exc:
            self.record_status_label.setText(
                f"Pause/resume failed: {exc}"
            )

    def stop_recording(self) -> None:
        if self.media_recorder is None:
            return

        if self._is_recorder_stopped():
            self._update_record_controls()
            return

        try:
            self.media_recorder.stop()

            if self.current_record_path is not None:
                self.record_status_label.setText(
                    f"Saved: {self.current_record_path.name}"
                )
            else:
                self.record_status_label.setText(
                    "Recording stopped"
                )

        except Exception as exc:
            self.record_status_label.setText(
                f"Stop recording failed: {exc}"
            )

        self._update_record_controls()

    def on_recorder_state_changed(self, _state) -> None:
        self._update_record_controls()

        if self._is_recording():
            if self.current_record_path is not None:
                self.record_status_label.setText(
                    f"Recording: {self.current_record_path.name}"
                )

        elif self._is_record_paused():
            self.record_status_label.setText(
                "Recording paused"
            )

        else:
            if self.current_record_path is not None:
                self.record_status_label.setText(
                    f"Saved: {self.current_record_path.name}"
                )

    def on_record_duration_changed(self, duration_ms: int) -> None:
        if not (
            self._is_recording()
            or self._is_record_paused()
        ):
            return

        seconds = max(
            0,
            int(duration_ms) // 1000,
        )

        hours = seconds // 3600
        minutes = (
            seconds % 3600
        ) // 60
        secs = seconds % 60

        state_text = (
            "PAUSED"
            if self._is_record_paused()
            else "REC"
        )

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
                self.current_record_path = Path(
                    local_file
                )
        except Exception:
            pass

    def on_recorder_error(self, *args) -> None:
        message = "Recorder error"

        for value in reversed(args):
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break

        self.record_status_label.setText(
            message
        )
        self._update_record_controls()

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

        self.camera_combo.blockSignals(
            False
        )

        if not devices:
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

        self.camera_combo.setCurrentIndex(
            selected_index
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

            self.capture_session.setCamera(
                self.camera
            )

            self.camera.start()

            name = str(
                device.description()
            )

            self.camera_status_label.setText(
                f"Starting: {name}"
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
        if (
            self.media_recorder is not None
            and not self._is_recorder_stopped()
        ):
            self.stop_recording()

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
                f"Active: {name}"
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

    def _show_video_placeholder(
        self,
        text: str,
    ) -> None:
        if self.video_widget is not None:
            self.video_widget.hide()

        self.video_placeholder.setText(
            text
        )
        self.video_placeholder.show()

    # ------------------------------------------------------------------ future OBS camera protocol hooks

    def current_speed_text(self) -> str:
        return self.speed_edit.text().strip()

    def set_pan_mode(
        self,
        mode: str,
    ) -> None:
        mode = str(
            mode
        ).lower()

        self.manual_mode = (
            mode != "auto"
        )

        for button in (
            self.pan_up_button,
            self.pan_down_button,
            self.pan_left_button,
            self.pan_right_button,
            self.pan_stop_button,
        ):
            button.setEnabled(
                self.manual_mode
            )

        if self.manual_mode:
            self.control_status_label.setText(
                f"Manual Pan • Lighting "
                f"{'ON' if self.lighting_on else 'OFF'}\n"
                "OBS control protocol: not connected yet"
            )
        else:
            self.control_status_label.setText(
                f"Auto Pan • Lighting "
                f"{'ON' if self.lighting_on else 'OFF'}\n"
                "OBS control protocol: not connected yet"
            )

        # TODO:
        # Send Manual/Auto pan command through OBS control protocol.

    def set_lighting(
        self,
        enabled: bool,
    ) -> None:
        self.lighting_on = bool(
            enabled
        )

        mode_text = (
            "Manual Pan"
            if self.manual_mode
            else "Auto Pan"
        )

        self.control_status_label.setText(
            f"{mode_text} • Lighting "
            f"{'ON' if self.lighting_on else 'OFF'}\n"
            "OBS control protocol: not connected yet"
        )

        # TODO:
        # Send Lighting ON/OFF command through OBS control protocol.

    def on_pan_pressed(
        self,
        direction: str,
    ) -> None:
        speed = (
            self.current_speed_text()
            or "--"
        )

        self.control_status_label.setText(
            f"PAN {direction} • Speed {speed}\n"
            "OBS control protocol: not connected yet"
        )

        # TODO:
        # Send PTZ motion-start command:
        # direction + speed.

    def on_pan_released(self) -> None:
        if not self.manual_mode:
            return

        self.control_status_label.setText(
            f"Manual Pan • STOP • Lighting "
            f"{'ON' if self.lighting_on else 'OFF'}\n"
            "OBS control protocol: not connected yet"
        )

        # TODO:
        # Send PTZ STOP command.

    def on_pan_stop(self) -> None:
        self.control_status_label.setText(
            f"Manual Pan • STOP • Lighting "
            f"{'ON' if self.lighting_on else 'OFF'}\n"
            "OBS control protocol: not connected yet"
        )

        # TODO:
        # Send PTZ STOP command.

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

            QVideoWidget#videoWidget {
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

            QPushButton#modeButton:checked {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #35A0D0;
            }

            QPushButton#lightButton:checked {
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
        # _stop_camera() also stops and finalizes an active recording first.
        self._stop_camera()
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

    window = CameraWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
