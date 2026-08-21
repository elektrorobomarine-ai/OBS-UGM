"""
GRC-UGM-PERTAMINA OBS
Main launcher/dashboard for the modular OBS monitoring application.

Requirement:
    pip install PySide6

Expected folder structure:

OBS_Monitor/
├── main.py
├── assets/
│   ├── logos/
│   │   ├── mipa_ugm.png
│   │   ├── grc.png
│   │   ├── pertamina_hulu_energi.png
│   │   └── ui.png
│   └── icons/
│       ├── app_icon.ico
│       ├── app_icon.png
│       ├── obs_setting.png
│       ├── camera.png
│       ├── position.png
│       ├── geophone.png
│       ├── other_sensors.png
│       └── miniseed.png
├── obs_setting.py
├── camera.py
├── position.py
├── geophone.py
├── other_sensors.py
└── miniseed_recording.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ============================================================
# WINDOWS CONSOLE / SHELL
# ============================================================
#
# When main.py is started with python.exe (for example by double-clicking
# the .py file), Windows may create a black console window.  Detach this
# GUI process from that console so only the PySide6 window remains.
#
# If the program is started from an already-open CMD/PowerShell/terminal,
# that terminal itself is not closed; only this Python process detaches.
#
if os.name == "nt":
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()
    except Exception:
        pass


# ============================================================
# WINDOWS TASKBAR ID
# IMPORTANT:
# This must be set BEFORE QApplication is created.
# It prevents Windows from treating this GUI only as python.exe.
# ============================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS"

if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception:
        pass


from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QFont, QIcon, QPixmap
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


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================

APP_TITLE = "GRC-UGM-PERTAMINA OBS"

BASE_DIR = Path(__file__).resolve().parent
EXTERNAL_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else BASE_DIR
)

ASSETS_DIR = BASE_DIR / "assets"
LOGO_DIR = ASSETS_DIR / "logos"
ICON_DIR = ASSETS_DIR / "icons"

# Main application icon.
# On Windows, ICO is preferred.
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"


# ============================================================
# MODULE CONFIGURATION
# ============================================================

@dataclass(frozen=True)
class ModuleSpec:
    title: str
    script: str
    icon_file: str
    fallback_icon: QStyle.StandardPixmap
    description: str


MODULES = (
    ModuleSpec(
        title="OBS Setting",
        script="obs_setting.py",
        icon_file="obs_setting.png",
        fallback_icon=QStyle.SP_FileDialogDetailedView,
        description="Configuration & device setup",
    ),
    ModuleSpec(
        title="Camera",
        script="camera.py",
        icon_file="camera.png",
        fallback_icon=QStyle.SP_ComputerIcon,
        description="Live underwater camera",
    ),
    ModuleSpec(
        title="Position",
        script="position.py",
        icon_file="position.png",
        fallback_icon=QStyle.SP_DriveNetIcon,
        description="GPS / position monitoring",
    ),
    ModuleSpec(
        title="Geophone",
        script="geophone.py",
        icon_file="geophone.png",
        fallback_icon=QStyle.SP_MediaVolume,
        description="3-axis seismic channels",
    ),
    ModuleSpec(
        title="Other Sensors",
        script="other_sensors.py",
        icon_file="other_sensors.png",
        fallback_icon=QStyle.SP_FileDialogInfoView,
        description="IMU, depth & auxiliary sensors",
    ),
    ModuleSpec(
        title="MiniSeed Recording",
        script="miniseed_recording.py",
        icon_file="miniseed.png",
        fallback_icon=QStyle.SP_DialogSaveButton,
        description="OBS data recording",
    ),
)


# ============================================================
# ICON HANDLING
# ============================================================

def get_application_icon() -> QIcon:
    """
    Get the main application icon.

    Priority:
    1. app_icon.ico on Windows
    2. app_icon.png
    3. app_icon.ico as fallback on other OS
    """

    if os.name == "nt" and APP_ICON_ICO.is_file():
        icon = QIcon(str(APP_ICON_ICO))
        if not icon.isNull():
            return icon

    if APP_ICON_PNG.is_file():
        icon = QIcon(str(APP_ICON_PNG))
        if not icon.isNull():
            return icon

    if APP_ICON_ICO.is_file():
        icon = QIcon(str(APP_ICON_ICO))
        if not icon.isNull():
            return icon

    return QIcon()


# ============================================================
# LOGO WIDGET
# ============================================================

class LogoLabel(QLabel):
    """Logo widget that preserves image aspect ratio."""

    def __init__(
        self,
        image_path: Path,
        max_width: int,
        max_height: int,
        parent=None,
    ):
        super().__init__(parent)

        self.image_path = image_path
        self.max_width = max_width
        self.max_height = max_height

        self.setObjectName("logoLabel")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Fixed,
        )
        self.setMinimumHeight(max_height)

        self._load_logo()

    def _load_logo(self) -> None:
        pixmap = QPixmap(str(self.image_path))

        if pixmap.isNull():
            self.setText(self.image_path.stem)
            return

        pixmap = pixmap.scaled(
            self.max_width,
            self.max_height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        self.setPixmap(pixmap)


# ============================================================
# MENU BUTTON
# ============================================================

class ModuleButton(QPushButton):
    """Large dashboard button for one OBS module."""

    def __init__(
        self,
        spec: ModuleSpec,
        parent=None,
    ):
        super().__init__(parent)

        self.spec = spec

        self.setObjectName("moduleButton")
        self.setCursor(Qt.PointingHandCursor)

        self.setMinimumSize(260, 145)

        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )

        self.setIconSize(QSize(46, 46))

        custom_icon = ICON_DIR / spec.icon_file

        if custom_icon.is_file():
            icon = QIcon(str(custom_icon))

            if not icon.isNull():
                self.setIcon(icon)
            else:
                self.setIcon(
                    self.style().standardIcon(
                        spec.fallback_icon
                    )
                )
        else:
            self.setIcon(
                self.style().standardIcon(
                    spec.fallback_icon
                )
            )

        self.setText(
            f"{spec.title}\n"
            f"{spec.description}"
        )


# ============================================================
# MAIN WINDOW
# ============================================================

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self._processes: dict[str, subprocess.Popen] = {}

        self.setWindowTitle(APP_TITLE)

        # Explicitly assign icon to main window as well.
        app_icon = get_application_icon()

        if not app_icon.isNull():
            self.setWindowIcon(app_icon)

        self.setMinimumSize(1050, 720)
        self.resize(1366, 768)

        self._build_ui()
        self._apply_styles()

        # Periodically clean handles of closed module processes.
        self.process_timer = QTimer(self)
        self.process_timer.timeout.connect(
            self._cleanup_processes
        )
        self.process_timer.start(1500)

    # ========================================================
    # BUILD UI
    # ========================================================

    def _build_ui(self) -> None:

        central = QWidget()
        central.setObjectName("centralWidget")

        self.setCentralWidget(central)

        root = QVBoxLayout(central)

        root.setContentsMargins(
            36,
            24,
            36,
            24,
        )

        root.setSpacing(18)

        # ----------------------------------------------------
        # MAIN TITLE
        # ----------------------------------------------------

        title = QLabel(APP_TITLE)

        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignCenter)

        root.addWidget(title)

        subtitle = QLabel(
            "Ocean Bottom Seismometer Monitoring & Acquisition System"
        )

        subtitle.setObjectName("subtitleLabel")
        subtitle.setAlignment(Qt.AlignCenter)

        root.addWidget(subtitle)

        # ----------------------------------------------------
        # PARTNER LOGOS
        # ----------------------------------------------------

        logo_panel = QFrame()

        logo_panel.setObjectName("logoPanel")

        logo_layout = QHBoxLayout(logo_panel)

        logo_layout.setContentsMargins(
            24,
            12,
            24,
            12,
        )

        logo_layout.setSpacing(28)

        logo_layout.addWidget(
            LogoLabel(
                LOGO_DIR / "mipa_ugm.png",
                285,
                105,
            ),
            3,
        )

        logo_layout.addWidget(
            LogoLabel(
                LOGO_DIR / "grc.png",
                260,
                88,
            ),
            2,
        )

        logo_layout.addWidget(
            LogoLabel(
                LOGO_DIR / "pertamina_hulu_energi.png",
                245,
                95,
            ),
            2,
        )

        logo_layout.addWidget(
            LogoLabel(
                LOGO_DIR / "ui.png",
                230,
                88,
            ),
            2,
        )

        root.addWidget(logo_panel)

        # ----------------------------------------------------
        # SYSTEM MODULE CAPTION
        # ----------------------------------------------------

        menu_caption = QLabel("SYSTEM MODULES")

        menu_caption.setObjectName("menuCaption")

        menu_caption.setAlignment(
            Qt.AlignLeft | Qt.AlignVCenter
        )

        root.addWidget(menu_caption)

        # ----------------------------------------------------
        # MODULE BUTTON GRID
        # ----------------------------------------------------

        menu_frame = QFrame()

        menu_frame.setObjectName("menuFrame")

        grid = QGridLayout(menu_frame)

        grid.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(18)

        for index, spec in enumerate(MODULES):

            button = ModuleButton(spec)

            button.clicked.connect(
                lambda checked=False, module=spec:
                self.launch_module(module)
            )

            row, col = divmod(index, 3)

            grid.addWidget(
                button,
                row,
                col,
            )

        for col in range(3):
            grid.setColumnStretch(
                col,
                1,
            )

        for row in range(2):
            grid.setRowStretch(
                row,
                1,
            )

        root.addWidget(
            menu_frame,
            1,
        )

        # ----------------------------------------------------
        # FOOTER
        # ----------------------------------------------------

        footer = QHBoxLayout()

        self.status_label = QLabel(
            "System ready"
        )

        self.status_label.setObjectName(
            "statusLabel"
        )

        footer.addWidget(
            self.status_label
        )

        footer.addItem(
            QSpacerItem(
                40,
                20,
                QSizePolicy.Expanding,
                QSizePolicy.Minimum,
            )
        )

        architecture = QLabel(
            "Modular Process Architecture"
        )

        architecture.setObjectName(
            "architectureLabel"
        )

        footer.addWidget(
            architecture
        )

        root.addLayout(
            footer
        )

    # ========================================================
    # STYLE
    # ========================================================

    def _apply_styles(self) -> None:

        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background-color: #07131D;
                color: #EAF2F7;
                font-family: "Segoe UI", "Arial";
            }

            QLabel#titleLabel {
                background: transparent;
                color: #F5F9FC;
                font-size: 28px;
                font-weight: 800;
                letter-spacing: 1px;
            }

            QLabel#subtitleLabel {
                background: transparent;
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
                background: transparent;
                color: #73B9E6;
                font-size: 13px;
                font-weight: 800;
                letter-spacing: 2px;
                padding-top: 2px;
            }

            QFrame#menuFrame {
                background: transparent;
                border: none;
            }

            QPushButton#moduleButton {
                background-color: #0D2231;
                color: #F3F7FA;

                border:
                    1px solid #1D4157;

                border-radius: 14px;

                text-align: left;

                padding:
                    22px 24px;

                font-size: 16px;
                font-weight: 700;
            }

            QPushButton#moduleButton:hover {
                background-color: #113047;

                border:
                    1px solid #3A8FBD;
            }

            QPushButton#moduleButton:pressed {
                background-color: #0A1B27;

                border:
                    1px solid #65B6DF;

                padding-top: 24px;
                padding-left: 26px;
            }

            QLabel#statusLabel {
                background: transparent;
                color: #7C99AA;
                font-size: 11px;
                padding-top: 4px;
            }

            QLabel#architectureLabel {
                background: transparent;
                color: #4E7185;
                font-size: 11px;
                padding-top: 4px;
            }
            """
        )

    # ========================================================
    # PYTHON GUI EXECUTABLE
    # ========================================================

    @staticmethod
    def _gui_python_executable() -> str:
        """
        Prefer pythonw.exe on Windows so launched GUI modules
        do not open a CMD/console window.
        """

        executable = Path(
            sys.executable
        )

        if os.name == "nt":

            if executable.name.lower() == "python.exe":

                pythonw = executable.with_name(
                    "pythonw.exe"
                )

                if pythonw.exists():
                    return str(pythonw)

        return str(executable)

    # ========================================================
    # LAUNCH MODULE
    # ========================================================

    def launch_module(
        self,
        spec: ModuleSpec,
    ) -> None:

        self._cleanup_processes()

        current = self._processes.get(
            spec.script
        )

        if (
            current is not None
            and current.poll() is None
        ):

            QMessageBox.information(
                self,
                APP_TITLE,
                f"{spec.title} is already running.",
            )

            return

        script_path = (
            BASE_DIR / spec.script
        )

        if getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "--module",
                Path(spec.script).stem,
            ]
            child_cwd = EXTERNAL_DIR
        else:
            if not script_path.exists():
                QMessageBox.information(
                    self,
                    spec.title,
                    (
                        f"Module '{spec.title}' "
                        f"is not implemented yet.\n\n"
                        f"Expected file:\n"
                        f"{script_path.name}"
                    ),
                )
                self.status_label.setText(
                    f"{spec.title}: module file not available"
                )
                return

            command = [
                self._gui_python_executable(),
                str(script_path),
            ]
            child_cwd = BASE_DIR

        popen_kwargs = {
            "cwd": str(child_cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        # Hide console window on Windows.
        if os.name == "nt":

            popen_kwargs[
                "creationflags"
            ] = subprocess.CREATE_NO_WINDOW

        try:

            process = subprocess.Popen(
                command,
                **popen_kwargs,
            )

        except Exception as exc:

            QMessageBox.critical(
                self,
                f"Cannot open {spec.title}",
                (
                    "Failed to start module:\n\n"
                    f"{exc}"
                ),
            )

            self.status_label.setText(
                f"Failed to start {spec.title}"
            )

            return

        self._processes[
            spec.script
        ] = process

        self.status_label.setText(
            f"{spec.title} launched"
        )

    # ========================================================
    # CLEAN FINISHED PROCESS HANDLES
    # ========================================================

    def _cleanup_processes(self) -> None:

        finished = [
            script_name
            for script_name, process
            in self._processes.items()
            if process.poll() is not None
        ]

        for script_name in finished:
            self._processes.pop(
                script_name,
                None,
            )

    # ========================================================
    # WINDOW CLOSE
    # ========================================================

    def closeEvent(
        self,
        event,
    ) -> None:
        """
        Child modules remain independent.
        Closing the launcher does not forcibly kill them.
        """

        event.accept()


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    # QApplication MUST be created after Windows AppUserModelID
    # has been configured above.

    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        APP_TITLE
    )

    app.setApplicationDisplayName(
        APP_TITLE
    )

    # Main GUI/taskbar icon
    app_icon = get_application_icon()

    if not app_icon.isNull():
        app.setWindowIcon(
            app_icon
        )

    # Global font
    font = QFont(
        "Segoe UI"
    )

    font.setPointSize(
        10
    )

    app.setFont(
        font
    )

    window = MainWindow()

    # Explicit second assignment for Windows.
    if not app_icon.isNull():
        window.setWindowIcon(
            app_icon
        )

    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
