"""
geophone.py
===========

GRC-UGM-PERTAMINA OBS
Geophone visualization launcher

Version: 2

This window is intentionally a launcher/panel only. Each visualization runs in
a separate Python process so heavy plotting, FFT, spectrogram, 3D rendering,
filtering, event detection, etc. cannot freeze the launcher or other OBS
modules.

Shared data source:
    shared_data_v5.py

Version 2 adapts this launcher/status panel to the current ADC stream model:
- raw ADC source rate and effective shared-stream rate are shown separately;
- global Average/Decimation N is read from shared_data_v5;
- ADC session ID is monitored;
- TCP packet timing is not used as the physical signal sample clock.

Baseline mapping:
    CH0 = Geophone X
    CH1 = Geophone Y
    CH2 = Geophone Z
    CH3 = Auxiliary ADC channel

The individual visualization programs are:
    geophone_realtime.py
    geophone_fft.py
    geophone_spectrogram.py
    geophone_3d.py
    geophone_imu.py
    geophone_hodogram.py
    geophone_quality.py
    geophone_event.py
    geophone_psd.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ----------------------------------------------------------------------
# Windows GUI behaviour
# ----------------------------------------------------------------------

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE"

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


from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from shared_data_v5 import (
    OBSSharedData,
    SHARED_DATA_API_VERSION,
)


APP_TITLE = "Geophone"
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


@dataclass(frozen=True)
class ViewSpec:
    title: str
    subtitle: str
    script: str
    icon_kind: str


VIEWS = (
    ViewSpec(
        title="Real-Time Waveform",
        subtitle="Scrolling X / Y / Z time series",
        script="geophone_realtime.py",
        icon_kind="wave",
    ),
    ViewSpec(
        title="FFT Spectrum",
        subtitle="Frequency spectrum per axis",
        script="geophone_fft.py",
        icon_kind="fft",
    ),
    ViewSpec(
        title="Spectrogram",
        subtitle="Time-frequency waterfall view",
        script="geophone_spectrogram.py",
        icon_kind="spectrogram",
    ),
    ViewSpec(
        title="3D Particle Motion",
        subtitle="Real-time XYZ motion trajectory",
        script="geophone_3d.py",
        icon_kind="3d",
    ),
    ViewSpec(
        title="Geophone + IMU",
        subtitle="Seismic signal with roll / pitch / yaw",
        script="geophone_imu.py",
        icon_kind="imu",
    ),
    ViewSpec(
        title="Hodogram / Polarization",
        subtitle="XY, XZ, YZ particle-motion planes",
        script="geophone_hodogram.py",
        icon_kind="hodogram",
    ),
    ViewSpec(
        title="Signal Quality",
        subtitle="ADC status, sync, saturation & errors",
        script="geophone_quality.py",
        icon_kind="quality",
    ),
    ViewSpec(
        title="Event Monitor",
        subtitle="Peak / trigger / seismic event view",
        script="geophone_event.py",
        icon_kind="event",
    ),
    ViewSpec(
        title="PSD / Noise Floor",
        subtitle="Power spectral density & ambient noise",
        script="geophone_psd.py",
        icon_kind="psd",
    ),
)


def application_icon() -> QIcon:
    candidates = []

    if os.name == "nt":
        candidates.append(APP_ICON_ICO)

    candidates.extend(
        [
            APP_ICON_PNG,
            APP_ICON_ICO,
        ]
    )

    for path in candidates:
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon

    return QIcon()


class IconFactory:
    """
    Draw compact vector-like icons at runtime.

    This avoids additional icon files while keeping each visualization button
    visually distinct.
    """

    SIZE = 52

    @classmethod
    def make(cls, kind: str) -> QIcon:
        pixmap = QPixmap(
            cls.SIZE,
            cls.SIZE,
        )
        pixmap.fill(
            Qt.transparent
        )

        painter = QPainter(
            pixmap
        )
        painter.setRenderHint(
            QPainter.Antialiasing,
            True,
        )

        pen = QPen(
            QColor("#A9DCF5")
        )
        pen.setWidthF(
            2.4
        )
        pen.setCapStyle(
            Qt.RoundCap
        )
        pen.setJoinStyle(
            Qt.RoundJoin
        )

        painter.setPen(
            pen
        )

        accent_pen = QPen(
            QColor("#FFFFFF")
        )
        accent_pen.setWidthF(
            1.7
        )
        accent_pen.setCapStyle(
            Qt.RoundCap
        )

        draw_map: dict[
            str,
            Callable[[QPainter, QPen, QPen], None],
        ] = {
            "wave": cls._draw_wave,
            "fft": cls._draw_fft,
            "spectrogram": cls._draw_spectrogram,
            "3d": cls._draw_3d,
            "imu": cls._draw_imu,
            "hodogram": cls._draw_hodogram,
            "quality": cls._draw_quality,
            "event": cls._draw_event,
            "psd": cls._draw_psd,
        }

        draw = draw_map.get(
            kind,
            cls._draw_wave,
        )

        draw(
            painter,
            pen,
            accent_pen,
        )

        painter.end()

        return QIcon(
            pixmap
        )

    @staticmethod
    def _draw_axes(
        painter: QPainter,
        pen: QPen,
    ) -> None:
        painter.setPen(
            pen
        )
        painter.drawLine(
            9,
            42,
            45,
            42,
        )
        painter.drawLine(
            9,
            42,
            9,
            9,
        )

    @classmethod
    def _draw_wave(
        cls,
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        cls._draw_axes(
            painter,
            accent,
        )

        points = [
            QPointF(10, 27),
            QPointF(15, 27),
            QPointF(19, 16),
            QPointF(23, 37),
            QPointF(28, 20),
            QPointF(33, 30),
            QPointF(38, 23),
            QPointF(44, 27),
        ]

        painter.setPen(
            pen
        )
        painter.drawPolyline(
            QPolygonF(points)
        )

    @classmethod
    def _draw_fft(
        cls,
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        cls._draw_axes(
            painter,
            accent,
        )

        painter.setPen(
            pen
        )

        bars = [
            (14, 35, 14, 30),
            (19, 35, 19, 20),
            (24, 35, 24, 12),
            (29, 35, 29, 25),
            (34, 35, 34, 17),
            (39, 35, 39, 31),
        ]

        for x1, y1, x2, y2 in bars:
            painter.drawLine(
                x1,
                y1,
                x2,
                y2,
            )

    @staticmethod
    def _draw_spectrogram(
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        painter.setPen(
            accent
        )
        painter.drawRect(
            9,
            10,
            35,
            32,
        )

        painter.setPen(
            pen
        )

        for y in (
            16,
            23,
            30,
            37,
        ):
            painter.drawLine(
                12,
                y,
                41,
                y,
            )

        for x in (
            17,
            25,
            33,
        ):
            painter.drawLine(
                x,
                13,
                x,
                39,
            )

    @staticmethod
    def _draw_3d(
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        painter.setPen(
            accent
        )

        painter.drawLine(
            26,
            28,
            43,
            28,
        )
        painter.drawLine(
            26,
            28,
            16,
            42,
        )
        painter.drawLine(
            26,
            28,
            26,
            9,
        )

        painter.setPen(
            pen
        )

        points = [
            QPointF(15, 32),
            QPointF(19, 24),
            QPointF(29, 19),
            QPointF(37, 25),
            QPointF(34, 34),
            QPointF(25, 38),
            QPointF(17, 34),
        ]

        painter.drawPolyline(
            QPolygonF(points)
        )

    @staticmethod
    def _draw_imu(
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        painter.setPen(
            accent
        )
        painter.drawEllipse(
            13,
            13,
            27,
            27,
        )
        painter.drawLine(
            26,
            7,
            26,
            45,
        )
        painter.drawLine(
            7,
            26,
            45,
            26,
        )

        painter.setPen(
            pen
        )
        painter.drawArc(
            16,
            16,
            20,
            20,
            30 * 16,
            220 * 16,
        )

    @staticmethod
    def _draw_hodogram(
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        painter.setPen(
            accent
        )
        painter.drawLine(
            26,
            6,
            26,
            46,
        )
        painter.drawLine(
            6,
            26,
            46,
            26,
        )

        painter.setPen(
            pen
        )
        painter.drawEllipse(
            14,
            18,
            25,
            16,
        )
        painter.drawEllipse(
            19,
            12,
            14,
            28,
        )

    @staticmethod
    def _draw_quality(
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        painter.setPen(
            accent
        )
        painter.drawRoundedRect(
            10,
            10,
            33,
            33,
            5,
            5,
        )

        painter.setPen(
            pen
        )
        painter.drawLine(
            16,
            28,
            22,
            34,
        )
        painter.drawLine(
            22,
            34,
            37,
            18,
        )

    @classmethod
    def _draw_event(
        cls,
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        cls._draw_axes(
            painter,
            accent,
        )

        points = [
            QPointF(10, 31),
            QPointF(18, 31),
            QPointF(22, 29),
            QPointF(25, 11),
            QPointF(28, 39),
            QPointF(31, 19),
            QPointF(34, 31),
            QPointF(44, 31),
        ]

        painter.setPen(
            pen
        )
        painter.drawPolyline(
            QPolygonF(points)
        )

    @classmethod
    def _draw_psd(
        cls,
        painter: QPainter,
        pen: QPen,
        accent: QPen,
    ) -> None:
        cls._draw_axes(
            painter,
            accent,
        )

        points = [
            QPointF(10, 15),
            QPointF(15, 18),
            QPointF(20, 23),
            QPointF(25, 26),
            QPointF(30, 30),
            QPointF(35, 34),
            QPointF(40, 36),
            QPointF(44, 38),
        ]

        painter.setPen(
            pen
        )
        painter.drawPolyline(
            QPolygonF(points)
        )


class ViewButton(QPushButton):

    def __init__(
        self,
        spec: ViewSpec,
        parent=None,
    ):
        super().__init__(
            parent
        )

        self.spec = spec

        self.setObjectName(
            "viewButton"
        )

        self.setCursor(
            Qt.PointingHandCursor
        )

        self.setMinimumSize(
            255,
            120,
        )

        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )

        self.setIcon(
            IconFactory.make(
                spec.icon_kind
            )
        )

        self.setIconSize(
            QSize(
                52,
                52,
            )
        )

        self.setText(
            (
                f"{spec.title}\n"
                f"{spec.subtitle}"
            )
        )


class GeophoneLauncher(
    QMainWindow
):

    def __init__(
        self,
    ):
        super().__init__()

        self._processes: dict[
            str,
            subprocess.Popen,
        ] = {}

        self.shared: Optional[
            OBSSharedData
        ] = None

        try:
            self.shared = (
                OBSSharedData()
            )
        except Exception:
            self.shared = None

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

        self.resize(
            1020,
            760,
        )

        self.setMinimumSize(
            760,
            600,
        )

        self._build_ui()
        self._apply_style()

        self.status_timer = QTimer(
            self
        )

        self.status_timer.timeout.connect(
            self._refresh_shared_status
        )

        self.status_timer.start(
            500
        )

        self.process_timer = QTimer(
            self
        )

        self.process_timer.timeout.connect(
            self._cleanup_processes
        )

        self.process_timer.start(
            1500
        )

        self._refresh_shared_status()

    def _build_ui(
        self,
    ) -> None:

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
            24,
            20,
            24,
            18,
        )

        root.setSpacing(
            12
        )

        title = QLabel(
            "GEOPHONE VISUALIZATION"
        )

        title.setObjectName(
            "titleLabel"
        )

        subtitle = QLabel(
            (
                "Choose a display mode for "
                "Geophone X / Y / Z (ADC CH0 / CH1 / CH2)"
            )
        )

        subtitle.setObjectName(
            "subtitleLabel"
        )

        root.addWidget(
            title
        )

        root.addWidget(
            subtitle
        )

        status_frame = QFrame()
        status_frame.setObjectName(
            "statusFrame"
        )

        status_layout = QHBoxLayout(
            status_frame
        )

        status_layout.setContentsMargins(
            12,
            8,
            12,
            8,
        )

        self.shared_status_label = QLabel(
            f"Shared RAM v{SHARED_DATA_API_VERSION}: checking..."
        )

        self.shared_status_label.setObjectName(
            "sharedStatus"
        )

        self.sample_status_label = QLabel(
            "ADC stream: --"
        )

        self.sample_status_label.setObjectName(
            "sampleStatus"
        )

        status_layout.addWidget(
            self.shared_status_label
        )

        status_layout.addStretch(
            1
        )

        status_layout.addWidget(
            self.sample_status_label
        )

        root.addWidget(
            status_frame
        )

        section = QLabel(
            "DISPLAY MODES"
        )

        section.setObjectName(
            "sectionLabel"
        )

        root.addWidget(
            section
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(
            True
        )
        scroll.setFrameShape(
            QFrame.NoFrame
        )

        content = QWidget()
        content.setObjectName(
            "scrollContent"
        )

        grid = QGridLayout(
            content
        )

        grid.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        grid.setHorizontalSpacing(
            14
        )

        grid.setVerticalSpacing(
            14
        )

        # 3 columns on desktop keeps the panel compact but still readable.
        for index, spec in enumerate(
            VIEWS
        ):
            button = ViewButton(
                spec
            )

            button.clicked.connect(
                lambda checked=False, view=spec:
                self.launch_view(
                    view
                )
            )

            row, col = divmod(
                index,
                3,
            )

            grid.addWidget(
                button,
                row,
                col,
            )

        for col in range(
            3
        ):
            grid.setColumnStretch(
                col,
                1,
            )

        scroll.setWidget(
            content
        )

        root.addWidget(
            scroll,
            1,
        )

        footer = QLabel(
            (
                "Each display runs in a separate process and reads "
                "the effective ADC stream from shared_data_v5 independently."
            )
        )

        footer.setObjectName(
            "footerLabel"
        )

        footer.setWordWrap(
            True
        )

        root.addWidget(
            footer
        )

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

            QLabel#titleLabel {
                font-size: 24px;
                font-weight: 800;
                letter-spacing: 1px;
            }

            QLabel#subtitleLabel {
                color: #B7C9D4;
                font-size: 11px;
                padding-bottom: 3px;
            }

            QLabel#sectionLabel {
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 800;
                letter-spacing: 1.5px;
                padding-top: 3px;
            }

            QFrame#statusFrame {
                background-color: #0B1B27;
                border: 1px solid #17374A;
                border-radius: 9px;
            }

            QLabel#sharedStatus,
            QLabel#sampleStatus {
                color: #B9CDD8;
                font-size: 10px;
                font-weight: 600;
            }

            QPushButton#viewButton {
                background-color: #0D2231;
                color: #FFFFFF;
                border: 1px solid #1D4157;
                border-radius: 12px;
                text-align: left;
                padding: 16px 18px;
                font-size: 13px;
                font-weight: 700;
            }

            QPushButton#viewButton:hover {
                background-color: #123047;
                border: 1px solid #469BC5;
            }

            QPushButton#viewButton:pressed {
                background-color: #091A25;
                border: 1px solid #72C3EB;
                padding-top: 18px;
                padding-left: 20px;
            }

            QLabel#footerLabel {
                color: #6F8B9B;
                font-size: 10px;
            }
            """
        )

    @staticmethod
    def _gui_python_executable(
    ) -> str:

        executable = Path(
            sys.executable
        )

        if os.name == "nt":
            if (
                executable.name.lower()
                == "python.exe"
            ):
                pythonw = (
                    executable.with_name(
                        "pythonw.exe"
                    )
                )

                if pythonw.exists():
                    return str(
                        pythonw
                    )

        return str(
            executable
        )

    @staticmethod
    def _check_view_shared_data_version(
        script_path: Path,
    ) -> Optional[str]:
        """
        Warn when a child visualization clearly still imports the old RAM API.
        """
        try:
            source = script_path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        except Exception:
            return None

        obsolete = []

        if "from shared_data_v3 import" in source:
            obsolete.append("shared_data_v3")

        if "from shared_data_v4 import" in source:
            obsolete.append("shared_data_v4")

        if not obsolete:
            return None

        return (
            f"{script_path.name} still imports "
            + " / ".join(obsolete)
            + ". Update that module to shared_data_v5 before running it."
        )

    def launch_view(
        self,
        spec: ViewSpec,
    ) -> None:

        self._cleanup_processes()

        current = (
            self._processes
            .get(
                spec.script
            )
        )

        if (
            current is not None
            and current.poll() is None
        ):
            QMessageBox.information(
                self,
                APP_TITLE,
                (
                    f"{spec.title} is already running."
                ),
            )
            return

        script_path = (
            BASE_DIR
            / spec.script
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
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    (
                        f"Display module is not available:\n\n"
                        f"{script_path.name}"
                    ),
                )
                return

            compatibility_warning = (
                self._check_view_shared_data_version(
                    script_path
                )
            )
            if compatibility_warning:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    compatibility_warning,
                )
                return

            command = [
                self._gui_python_executable(),
                str(script_path),
            ]
            child_cwd = BASE_DIR

        kwargs = {
            "cwd": str(child_cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        if os.name == "nt":
            kwargs[
                "creationflags"
            ] = subprocess.CREATE_NO_WINDOW

        try:
            process = subprocess.Popen(
                command,
                **kwargs,
            )

        except Exception as exc:
            QMessageBox.critical(
                self,
                APP_TITLE,
                (
                    f"Cannot open {spec.title}:\n\n"
                    f"{exc}"
                ),
            )
            return

        self._processes[
            spec.script
        ] = process

    def _cleanup_processes(
        self,
    ) -> None:

        finished = [
            name
            for name, process
            in self._processes.items()
            if process.poll() is not None
        ]

        for name in finished:
            self._processes.pop(
                name,
                None,
            )

    def _refresh_shared_status(
        self,
    ) -> None:

        shared = self.shared

        if shared is None:
            self.shared_status_label.setText(
                f"Shared RAM v{SHARED_DATA_API_VERSION}: unavailable"
            )
            self.sample_status_label.setText(
                "ADC stream: --"
            )
            return

        try:
            telemetry = shared.read_telemetry()
            bulk = shared.read_bulk_status()
            stream_info = shared.read_adc_stream_info()
            total = shared.adc_total_samples()

            connection = (
                "DATA CONNECTED"
                if telemetry.data_connected
                else "DATA NOT CONNECTED"
            )

            raw_fs = max(
                0.0,
                float(stream_info.raw_sample_rate_hz),
            )
            effective_fs = max(
                0.0,
                float(stream_info.effective_sample_rate_hz),
            )
            average_n = max(
                1,
                int(stream_info.decimation_samples),
            )
            session_id = int(
                stream_info.adc_session_id
            )
            decimation_mode = str(
                stream_info.decimation_mode
            )

            self.shared_status_label.setText(
                (
                    f"Shared RAM v{SHARED_DATA_API_VERSION}: "
                    f"{connection} | session {session_id}"
                )
            )

            self.sample_status_label.setText(
                (
                    f"ADC total: {total:,} | "
                    f"Fs {effective_fs:.3f} Hz "
                    f"(raw {raw_fs:.3f}/N{average_n}) | "
                    f"mode {decimation_mode} | "
                    f"dropped {bulk.dropped_frames} | "
                    f"sync err {bulk.channel_id_mismatches}"
                )
            )

            self.sample_status_label.setToolTip(
                (
                    "Frequency/time calibration uses effective Fs from "
                    "shared_data_v5.\n"
                    f"Raw ADC source rate: {raw_fs:.6f} Hz\n"
                    f"Average/Decimation N: {average_n}\n"
                    f"Effective shared rate: {effective_fs:.6f} Hz\n"
                    f"ADC session ID: {session_id}\n"
                    f"Mode: {decimation_mode}\n"
                    "TCP packet arrival timing is diagnostic only."
                )
            )

        except Exception as exc:
            self.shared_status_label.setText(
                f"Shared RAM v{SHARED_DATA_API_VERSION}: read error"
            )
            self.sample_status_label.setText(
                "ADC stream: unavailable"
            )
            self.sample_status_label.setToolTip(
                str(exc)
            )

    def closeEvent(
        self,
        event,
    ) -> None:

        # View processes remain independent.
        if self.shared is not None:
            try:
                self.shared.close()
            except Exception:
                pass

        event.accept()


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

    window = GeophoneLauncher()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
