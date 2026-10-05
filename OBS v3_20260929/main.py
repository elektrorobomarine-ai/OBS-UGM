"""
GRC-UGM-PERTAMINA OBS
Main launcher/dashboard for the modular OBS monitoring application.

Version: 12

Requirements:
    pip install PySide6

Project layout expected by this launcher:

obs_monitor_framework/
├── main.py
├── assets/
│   ├── logos/
│   │   ├── Prameya_rev.png          # main application header logo
│   │   ├── mipa_ugm.png
│   │   ├── grc.png
│   │   ├── pertamina_hulu_energi.png
│   │   └── ui.png
│   └── icons/
│       ├── app_icon.ico          # main Windows application/taskbar icon
│       ├── app_icon.png          # fallback application icon
│       ├── obs_setting.png       # optional
│       ├── camera.png            # optional
│       ├── position.png          # optional
│       ├── geophone.png          # optional
│       ├── other_sensors.png     # optional
│       └── miniseed.png          # optional
├── obs_setting.py                # to be developed
├── camera.py                     # to be developed
├── position.py                   # to be developed
├── geophone.py                   # to be developed
├── other_sensors.py              # to be developed
└── miniseed_recording.py         # to be developed

Header branding:
The visible application title is rendered from assets/logos/Prameya_rev.png in a
full-width application-logo card that is taller than the partner-logo panel.

Architecture note:
Each functional module is launched as a SEPARATE Python process. OBS Setting is
the exclusive owner of the physical OBS TCP connections (54300 and 54301).
Camera and MiniSEED send OBS commands through local IPC; other consumers read
centralized shared RAM. This keeps controller connection count fixed at two.

Version 10 lifecycle update:
- Closing the main launcher sends a local IPC shutdown request to OBS Setting.
- OBS Setting can therefore close TCP 54300/54301 and publish OFFLINE health
  cleanly instead of becoming an orphan acquisition process.
- Other functional GUI processes remain independent; they are not force-killed
  by the launcher, avoiding corruption of an active recording.

Version 11 PyCharm / Qt6 type-safety cleanup:
- Uses scoped Qt6 enums for QStyle, Qt and QSizePolicy.
- Removes the unused Optional import and explicitly types optional shared RAM.
- Uses canonical runtime shared_data.py rather than a versioned consumer import.
- Resolves the Windows AppUserModelID API dynamically to keep IDE inspection clean.
- Narrows known exception handlers while preserving v10 launcher/lifecycle behavior.

Version 12 coordinated application shutdown:
- Closing Main asks for confirmation before any process is stopped.
- YES starts a coordinated shutdown of modules launched by this Main instance.
- PC MiniSEED is closed first. If it is recording, its own v25 close path
  finalizes buffered MiniSEED/CSV/JSON output before the process exits.
- Remaining GUI modules receive a normal window-close request so their Qt
  closeEvent cleanup runs instead of being killed immediately.
- OBS Setting still receives the existing local-IPC final shutdown request so
  TCP 54300/54301 are closed cleanly.
- OBS onboard USB logging is intentionally not stopped by this workflow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


# Windows taskbar identity. Set before QApplication is created.
APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS"


def _configure_windows_app_identity() -> None:
    """Set the Windows taskbar identity without a statically resolved DLL member."""
    if os.name != "nt":
        return

    import ctypes

    try:
        setter = getattr(
            ctypes.windll.shell32,
            "SetCurrentProcessExplicitAppUserModelID",
            None,
        )
        if setter is not None:
            setter(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        # Cosmetic Windows integration must never block launcher startup.
        pass


_configure_windows_app_identity()


from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtGui import QCloseEvent, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from shared_data import OBSSharedData
from obs_ipc import IPC_SERVER_NAME


APP_TITLE = "GRC-UGM-PERTAMINA OBS"

# Main creates this per-process marker only while a confirmed coordinated
# shutdown is in progress. Child modules receive the path through their
# environment. MiniSEED uses it to distinguish Main-initiated shutdown from a
# user clicking X directly on the recorder window.
MAIN_SHUTDOWN_FLAG_ENV = "OBS_MAIN_SHUTDOWN_FLAG"

# Non-recording modules are expected to close quickly after WM_CLOSE. MiniSEED
# is never force-terminated: Main waits for its finalization to complete.
MODULE_GRACEFUL_CLOSE_S = 8.0
MODULE_FORCE_KILL_S = 2.0

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
LOGO_DIR = ASSETS_DIR / "logos"
ICON_DIR = ASSETS_DIR / "icons"
APP_HEADER_LOGO = LOGO_DIR / "Prameya_rev.png"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"


def get_application_icon() -> QIcon:
    """Load the main application icon. Prefer ICO on Windows, otherwise PNG."""
    candidates = ((APP_ICON_ICO, APP_ICON_PNG) if os.name == "nt" else (APP_ICON_PNG, APP_ICON_ICO))
    for path in candidates:
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


def _request_process_window_close(process: subprocess.Popen) -> bool:
    """Request a normal GUI close for a child process.

    On Windows this posts WM_CLOSE to every top-level window owned by the
    process so Qt receives closeEvent(). This is deliberately different from
    Popen.terminate(), which can bypass application cleanup.

    Non-Windows builds use terminate() as a compatibility fallback because the
    deployed OBS Runtime target is Windows.
    """
    if process.poll() is not None:
        return True

    if os.name != "nt":
        try:
            process.terminate()
            return True
        except (OSError, ProcessLookupError):
            return False

    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.windll.user32
        enum_windows = getattr(user32, "EnumWindows")
        get_window_pid = getattr(user32, "GetWindowThreadProcessId")
        post_message = getattr(user32, "PostMessageW")

        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        target_pid = int(process.pid)
        found = {"value": False}

        @callback_type
        def _enum_callback(hwnd, _lparam):
            owner_pid = wintypes.DWORD()
            get_window_pid(hwnd, ctypes.byref(owner_pid))
            if int(owner_pid.value) == target_pid:
                found["value"] = True
                post_message(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True

        enum_windows(_enum_callback, 0)
        return bool(found["value"])
    except (AttributeError, OSError, ValueError):
        return False


@dataclass(frozen=True)
class ModuleSpec:
    title: str
    script: str
    icon_file: str
    fallback_icon: QStyle.StandardPixmap
    description: str


MODULES = (
    ModuleSpec(
        "OBS Setting",
        "obs_setting.py",
        "obs_setting.png",
        QStyle.StandardPixmap.SP_FileDialogDetailedView,
        "Configuration & device setup",
    ),
    ModuleSpec(
        "Camera",
        "camera.py",
        "camera.png",
        QStyle.StandardPixmap.SP_ComputerIcon,
        "Live underwater camera",
    ),
    ModuleSpec(
        "Position",
        "position.py",
        "position.png",
        QStyle.StandardPixmap.SP_DriveNetIcon,
        "GPS / position monitoring",
    ),
    ModuleSpec(
        "Geophone",
        "geophone.py",
        "geophone.png",
        QStyle.StandardPixmap.SP_MediaVolume,
        "3-axis seismic channels",
    ),
    ModuleSpec(
        "Other Sensors",
        "other_sensors.py",
        "other_sensors.png",
        QStyle.StandardPixmap.SP_FileDialogInfoView,
        "IMU, depth & auxiliary sensors",
    ),
    ModuleSpec(
        "MiniSeed Recording",
        "miniseed_recording.py",
        "miniseed.png",
        QStyle.StandardPixmap.SP_DialogSaveButton,
        "OBS data recording",
    ),
)


class LogoLabel(QLabel):
    """Logo label that keeps the original image aspect ratio."""

    def __init__(self, image_path: Path, max_width: int, max_height: int):
        super().__init__()
        self.image_path = image_path
        self.max_width = max_width
        self.max_height = max_height
        self.setObjectName("logoLabel")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(max_height)

        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            pixmap = pixmap.scaled(
                max_width,
                max_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.setPixmap(pixmap)
        else:
            self.setText(image_path.stem)
            self.setStyleSheet("color: #A9B8C6; font-weight: 600;")


class ModuleButton(QPushButton):
    """Large dashboard card used to open a functional module."""

    def __init__(self, spec: ModuleSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(260, 145)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setIconSize(QSize(46, 46))

        # Use a custom future icon when available; otherwise use a Qt icon.
        custom_icon = ICON_DIR / spec.icon_file
        if custom_icon.exists():
            self.setIcon(QIcon(str(custom_icon)))
        else:
            self.setIcon(self.style().standardIcon(spec.fallback_icon))

        self.setText(f"{spec.title}\n{spec.description}")
        self.setObjectName("moduleButton")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._processes: dict[str, subprocess.Popen] = {}

        self._shutdown_in_progress = False
        self._shutdown_complete = False
        self._shutdown_stage = "idle"
        self._shutdown_stage_started = 0.0
        self._shutdown_force_sent = False
        self._shutdown_last_close_request = 0.0
        self._shutdown_flag_path = Path(tempfile.gettempdir()) / (
            f"grc_obs_runtime_shutdown_{os.getpid()}.flag"
        )
        try:
            self._shutdown_flag_path.unlink(missing_ok=True)
        except OSError:
            pass

        app_icon = get_application_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1050, 720)
        self.resize(1366, 900)

        self.shared: OBSSharedData | None
        try:
            self.shared = OBSSharedData()
        except (BufferError, OSError, RuntimeError, ValueError):
            self.shared = None

        self._build_ui()
        self._apply_styles()

        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self._refresh_health_status)
        self.health_timer.start(500)
        self._refresh_health_status()

        # Periodically remove finished child-process handles.
        self.process_timer = QTimer(self)
        self.process_timer.timeout.connect(self._cleanup_processes)
        self.process_timer.start(1500)

        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.setInterval(200)
        self.shutdown_timer.timeout.connect(self._poll_coordinated_shutdown)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(36, 24, 36, 24)
        root.setSpacing(18)

        # ===== Application header logo =====
        # The Prameya application logo gets its own full-width card, matching
        # the partner-logo panel width below but with a taller visual hierarchy.
        app_logo_panel = QFrame()
        app_logo_panel.setObjectName("appLogoPanel")
        app_logo_panel.setMinimumHeight(190)

        app_logo_layout = QHBoxLayout(app_logo_panel)
        app_logo_layout.setContentsMargins(28, 18, 28, 18)
        app_logo_layout.setSpacing(0)

        app_logo = LogoLabel(APP_HEADER_LOGO, 820, 155)
        app_logo.setObjectName("appLogoLabel")
        app_logo.setToolTip("Prameya")
        app_logo_layout.addWidget(app_logo, 1)

        root.addWidget(app_logo_panel)

        subtitle = QLabel("Ocean Bottom Seismometer Monitoring & Acquisition System")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(subtitle)

        # ===== Partner logos =====
        logo_panel = QFrame()
        logo_panel.setObjectName("logoPanel")
        logo_layout = QHBoxLayout(logo_panel)
        logo_layout.setContentsMargins(24, 12, 24, 12)
        logo_layout.setSpacing(28)

        logo_layout.addWidget(LogoLabel(LOGO_DIR / "mipa_ugm.png", 285, 105), 3)
        logo_layout.addWidget(LogoLabel(LOGO_DIR / "grc.png", 260, 88), 2)
        logo_layout.addWidget(
            LogoLabel(LOGO_DIR / "pertamina_hulu_energi.png", 245, 95), 2
        )
        logo_layout.addWidget(LogoLabel(LOGO_DIR / "ui.png", 230, 88), 2)

        root.addWidget(logo_panel)

        # ===== Menu title =====
        menu_caption = QLabel("SYSTEM MODULES")
        menu_caption.setObjectName("menuCaption")
        menu_caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(menu_caption)

        # ===== 3 x 2 module menu =====
        menu_frame = QFrame()
        menu_frame.setObjectName("menuFrame")
        grid = QGridLayout(menu_frame)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(18)

        for index, spec in enumerate(MODULES):
            button = ModuleButton(spec)
            button.clicked.connect(
                lambda checked=False, module=spec: self.launch_module(module)
            )
            row, col = divmod(index, 3)
            grid.addWidget(button, row, col)

        for col in range(3):
            grid.setColumnStretch(col, 1)
        for row in range(2):
            grid.setRowStretch(row, 1)

        root.addWidget(menu_frame, 1)

        # ===== Footer/status =====
        footer = QHBoxLayout()
        self.status_label = QLabel("System ready")
        self.status_label.setObjectName("statusLabel")
        footer.addWidget(self.status_label)

        footer.addItem(
            QSpacerItem(
                40,
                20,
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Minimum,
            )
        )

        self.health_label = QLabel("OBS Core: checking...")
        self.health_label.setObjectName("architectureLabel")
        footer.addWidget(self.health_label)
        footer.addSpacing(16)

        architecture = QLabel("Modular Process Architecture")
        architecture.setObjectName("architectureLabel")
        footer.addWidget(architecture)

        root.addLayout(footer)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #07131D;
                color: #EAF2F7;
                font-family: "Segoe UI", "Arial";
            }

            QFrame#appLogoPanel {
                background-color: #FFFFFF;
                border: 1px solid #DCE5EB;
                border-radius: 12px;
            }

            QLabel#appLogoLabel {
                background-color: #FFFFFF;
                border: none;
                padding: 0px;
            }

            QLabel#subtitleLabel {
                color: #8FA9BA;
                font-size: 13px;
                font-weight: 500;
                padding-bottom: 2px;
            }

            QFrame#logoPanel {
                background-color: #FFFFFF;
                border: 1px solid #DCE5EB;
                border-radius: 12px;
            }

            QLabel#logoLabel {
                background-color: #FFFFFF;
                border: none;
                color: #0B3148;
            }

            QLabel#menuCaption {
                color: #73B9E6;
                font-size: 13px;
                font-weight: 800;
                letter-spacing: 2px;
                padding-top: 2px;
            }

            QFrame#menuFrame {
                background: transparent;
            }

            QPushButton#moduleButton {
                background-color: #0D2231;
                color: #F3F7FA;
                border: 1px solid #1D4157;
                border-radius: 14px;
                text-align: left;
                padding: 22px 24px;
                font-size: 16px;
                font-weight: 700;
            }

            QPushButton#moduleButton:hover {
                background-color: #113047;
                border: 1px solid #3A8FBD;
            }

            QPushButton#moduleButton:pressed {
                background-color: #0A1B27;
                border: 1px solid #65B6DF;
                padding-top: 24px;
                padding-left: 26px;
            }

            QLabel#statusLabel {
                color: #7C99AA;
                font-size: 11px;
                padding-top: 4px;
            }

            QLabel#architectureLabel {
                color: #4E7185;
                font-size: 11px;
                padding-top: 4px;
            }
            """
        )

    @staticmethod
    def _gui_python_executable() -> str:
        """
        Prefer pythonw.exe on Windows so GUI child modules do not open a console.
        Fall back to the current interpreter everywhere else.
        """
        executable = Path(sys.executable)

        if os.name == "nt":
            if executable.name.lower() == "python.exe":
                pythonw = executable.with_name("pythonw.exe")
                if pythonw.exists():
                    return str(pythonw)

        return str(executable)

    def launch_module(self, spec: ModuleSpec) -> None:
        """
        Start the selected module in a separate process.

        Separating the modules by process is deliberate:
        - a heavy camera/plot loop will not freeze the main dashboard;
        - each module may create its own QThreads;
        - GPU contexts can be managed independently;
        - a failure in one module does not automatically kill the dashboard.
        """
        self._cleanup_processes()

        current = self._processes.get(spec.script)
        if current is not None and current.poll() is None:
            QMessageBox.information(
                self,
                APP_TITLE,
                f"{spec.title} is already running.",
            )
            return

        script_path = BASE_DIR / spec.script

        if not script_path.exists():
            QMessageBox.information(
                self,
                spec.title,
                (
                    f"Module '{spec.title}' is not implemented yet.\n\n"
                    f"Expected file:\n{script_path.name}\n\n"
                    "The main dashboard is ready; this module can be added "
                    "as a separate Python program in the next development step."
                ),
            )
            self.status_label.setText(
                f"{spec.title}: module file not available yet"
            )
            return

        command = [self._gui_python_executable(), str(script_path)]

        child_env = os.environ.copy()
        child_env[MAIN_SHUTDOWN_FLAG_ENV] = str(self._shutdown_flag_path)

        popen_kwargs = {
            "cwd": str(BASE_DIR),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": child_env,
        }

        # Prevent an extra black console window on Windows.
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            QMessageBox.critical(
                self,
                f"Cannot open {spec.title}",
                f"Failed to start module:\n\n{exc}",
            )
            self.status_label.setText(f"Failed to start {spec.title}")
            return

        self._processes[spec.script] = process
        self.status_label.setText(f"{spec.title} launched")

    def _refresh_health_status(self) -> None:
        shared = self.shared
        if shared is None:
            self.health_label.setText("OBS Core: shared RAM unavailable")
            return

        try:
            health = shared.read_acquisition_health()
            age = health.heartbeat_age_s()
            state = "LIVE" if health.is_alive() else "STALE"
            self.health_label.setText(
                f"OBS Core {state} • HB {age:.1f}s • "
                f"54300 {'ON' if health.command_connected else 'OFF'} • "
                f"54301 {'ON' if health.data_connected else 'OFF'}"
            )
        except (BufferError, KeyError, OSError, RuntimeError, ValueError) as exc:
            self.health_label.setText(f"OBS Core: {exc}")

    def _cleanup_processes(self) -> None:
        finished = [
            name
            for name, process in self._processes.items()
            if process.poll() is not None
        ]
        for name in finished:
            self._processes.pop(name, None)

    def _request_obs_manager_shutdown(self, timeout_ms: int = 900) -> bool:
        """Ask the local OBS acquisition manager to shut down cleanly.

        This uses only the existing QLocalServer command bus; it does not open
        any extra TCP connection to the OBS controller.
        """
        sock = QLocalSocket(self)
        try:
            sock.connectToServer(IPC_SERVER_NAME)
            if not sock.waitForConnected(max(50, int(timeout_ms // 3))):
                return False

            request = {
                "type": "shutdown",
                "source": "main",
                "request_id": uuid.uuid4().hex,
            }
            raw = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            if sock.write(raw) < 0:
                return False
            sock.flush()
            sock.waitForBytesWritten(max(50, int(timeout_ms // 3)))
            # Give OBS Setting a short opportunity to accept the request before
            # this launcher destroys its own Qt objects.
            sock.waitForReadyRead(max(50, int(timeout_ms // 3)))
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        finally:
            sock.disconnectFromServer()

    def _set_module_buttons_enabled(self, enabled: bool) -> None:
        for button in self.findChildren(ModuleButton):
            button.setEnabled(enabled)

    def _write_shutdown_flag(self) -> None:
        try:
            self._shutdown_flag_path.write_text(
                "confirmed coordinated shutdown\n",
                encoding="utf-8",
            )
        except OSError:
            # MiniSEED still has a normal close safeguard if the marker cannot
            # be written, so failure here must not crash the launcher.
            pass

    def _clear_shutdown_flag(self) -> None:
        try:
            self._shutdown_flag_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _request_close_for_script(self, script: str) -> bool:
        process = self._processes.get(script)
        if process is None or process.poll() is not None:
            return True
        return _request_process_window_close(process)

    def _begin_coordinated_shutdown(self) -> None:
        if self._shutdown_in_progress:
            return

        self._shutdown_in_progress = True
        self._shutdown_stage = "wait_miniseed"
        self._shutdown_stage_started = time.perf_counter()
        self._shutdown_force_sent = False
        self._shutdown_last_close_request = 0.0
        self._write_shutdown_flag()
        self._set_module_buttons_enabled(False)
        self.process_timer.stop()
        self._cleanup_processes()

        mini = self._processes.get("miniseed_recording.py")
        if mini is not None and mini.poll() is None:
            self.status_label.setText(
                "Shutting down • finalizing PC MiniSEED recording if active..."
            )
            self._request_close_for_script("miniseed_recording.py")
            self._shutdown_last_close_request = time.perf_counter()
        else:
            self._start_remaining_module_shutdown()

        self.shutdown_timer.start()

    def _start_remaining_module_shutdown(self) -> None:
        self._shutdown_stage = "wait_modules"
        self._shutdown_stage_started = time.perf_counter()
        self._shutdown_force_sent = False
        self.status_label.setText("Shutting down OBS Runtime modules...")

        # Request ordinary Qt closes first so each module can stop timers,
        # worker threads, camera/plot resources and shared-RAM attachments.
        for script in (
            "camera.py",
            "position.py",
            "geophone.py",
            "other_sensors.py",
        ):
            self._request_close_for_script(script)

        # OBS Setting owns the real controller sockets. Its existing local IPC
        # path performs the authoritative final shutdown of TCP 54300/54301.
        self._request_obs_manager_shutdown()

    def _poll_coordinated_shutdown(self) -> None:
        if not self._shutdown_in_progress:
            self.shutdown_timer.stop()
            return

        self._cleanup_processes()

        if self._shutdown_stage == "wait_miniseed":
            mini = self._processes.get("miniseed_recording.py")
            if mini is None or mini.poll() is not None:
                self.status_label.setText(
                    "PC MiniSEED finalized • closing remaining modules..."
                )
                self._start_remaining_module_shutdown()
                return

            now = time.perf_counter()
            elapsed = now - self._shutdown_stage_started
            self.status_label.setText(
                f"Finalizing PC MiniSEED recording... {elapsed:.1f} s"
            )
            # Retry the normal close request if the recorder was still starting
            # when the first WM_CLOSE was posted. Repeated requests remain safe:
            # MiniSEED v25 ignores close while finalization is in progress.
            if now - self._shutdown_last_close_request >= 1.0:
                self._request_close_for_script("miniseed_recording.py")
                self._shutdown_last_close_request = now

            # Never force-terminate MiniSEED. A long finalization is safer than
            # corrupting the last MiniSEED/CSV/JSON segment.
            return

        if self._shutdown_stage != "wait_modules":
            return

        running = {
            name: process
            for name, process in self._processes.items()
            if process.poll() is None
        }

        if not running:
            self._finish_coordinated_shutdown()
            return

        elapsed = time.perf_counter() - self._shutdown_stage_started
        names = ", ".join(
            Path(name).stem.replace("_", " ")
            for name in sorted(running)
        )
        self.status_label.setText(
            f"Closing modules: {names} • {elapsed:.1f} s"
        )

        if elapsed >= MODULE_GRACEFUL_CLOSE_S and not self._shutdown_force_sent:
            # MiniSEED is already gone before this stage. Remaining processes
            # are non-PC-recording GUI modules. Force termination is only a
            # last-resort fallback when a module ignored its normal close path.
            for process in running.values():
                try:
                    process.terminate()
                except (OSError, ProcessLookupError):
                    pass
            self._shutdown_force_sent = True
            self._shutdown_stage_started = time.perf_counter()
            return

        if self._shutdown_force_sent and elapsed >= MODULE_FORCE_KILL_S:
            for process in running.values():
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass

    def _finish_coordinated_shutdown(self) -> None:
        self.shutdown_timer.stop()
        self.health_timer.stop()
        self.process_timer.stop()

        shared = self.shared
        if shared is not None:
            try:
                shared.close()
            except (BufferError, OSError, RuntimeError, ValueError):
                pass
            finally:
                self.shared = None

        self._clear_shutdown_flag()
        self._shutdown_complete = True
        self._shutdown_in_progress = False
        self.status_label.setText("OBS Runtime closed safely")
        QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_complete:
            event.accept()
            return

        if self._shutdown_in_progress:
            event.ignore()
            return

        answer = QMessageBox.question(
            self,
            APP_TITLE,
            (
                "Are you sure you want to close this application?\n\n"
                "All OBS Runtime modules launched from this Main window will be closed.\n"
                "If PC MiniSEED recording is active, it will be stopped and saved first.\n\n"
                "OBS onboard USB logging will remain active."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        event.ignore()
        self._begin_coordinated_shutdown()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)

    app_icon = get_application_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    # Global UI font.
    font = QFont("Segoe UI")
    font.setPointSize(10)
    app.setFont(font)

    window = MainWindow()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
