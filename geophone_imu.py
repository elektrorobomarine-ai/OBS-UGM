"""
geophone_imu.py

GRC-UGM-PERTAMINA OBS
Geophone + IMU real-time monitor

Version: 6
Shared data: shared_data_v5.py

Layout:
- Left: CH0/X, CH1/Y, CH2/Z real-time waveforms.
- Right: 3D OBS model, world ground plane, world/body XYZ axes,
  roll/pitch/yaw numeric indicators, P/Q/R rates, and display controls.

Performance:
- Shared RAM is copied only by a dedicated QThread when new ADC data exists.
- GUI renders cached ADC data at 60 FPS using a sample-domain jitter buffer.
- Three waveforms share one PyQtGraph/OpenGL viewport.
- 3D model uses PyQtGraph GLViewWidget/OpenGL.
- IMU orientation is interpolated at render rate; optional P/Q/R prediction
  smooths the ~1 Hz AHRS2 telemetry cadence.
- v6 reads the authoritative effective GEOPHONE ADC sample rate from
  shared_data_v5. Geophone time-window sizing, timestamp interpolation,
  gap detection, and jitter-buffer pacing all follow that effective rate.
- IMU telemetry remains independent of geophone decimation and keeps its own
  telemetry cadence.
- The v5 orientation convention is preserved:
      yaw 0° = North, yaw 90° = East
      positive pitch = nose UP

Example:
    raw ADC = 1000 Hz
    Average N = 5
    shared geophone stream = 200 Hz

A 5-second waveform display therefore uses about 1000 output samples, not
5000 raw-rate samples.

Dependencies:
    pip install PySide6 numpy pyqtgraph PyOpenGL PyOpenGL_accelerate
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

# -----------------------------------------------------------------------------
# Windows runtime
# -----------------------------------------------------------------------------
APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.GEOPHONE.IMU"
_WINDOWS_TIMER_ACTIVE = False


def configure_windows_runtime() -> None:
    global _WINDOWS_TIMER_ACTIVE
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00008000)  # ABOVE_NORMAL
        try:
            if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
                _WINDOWS_TIMER_ACTIVE = True
        except Exception:
            pass
        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()
    except Exception:
        pass


def release_windows_runtime() -> None:
    global _WINDOWS_TIMER_ACTIVE
    if os.name == "nt" and _WINDOWS_TIMER_ACTIVE:
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        _WINDOWS_TIMER_ACTIVE = False


configure_windows_runtime()

# -----------------------------------------------------------------------------
# Qt / graphics
# -----------------------------------------------------------------------------
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont, QIcon, QKeySequence, QMatrix4x4, QSurfaceFormat
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception:
    QOpenGLWidget = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pyqtgraph as pg
except ImportError:
    pg = None

try:
    import pyqtgraph.opengl as gl
except Exception:
    gl = None

from shared_data_v5 import RAW_ADC_SAMPLE_RATE_HZ, OBSSharedData

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
APP_TITLE = "Geophone + IMU"
SYSTEM_TITLE = "GRC-UGM-PERTAMINA OBS"
BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"
APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

CHANNELS = (
    ("CH0", "Geophone X", "ch0"),
    ("CH1", "Geophone Y", "ch1"),
    ("CH2", "Geophone Z", "ch2"),
)

FPS_CHOICES = (30, 45, 60, 75, 90)
BUFFER_CHOICES_MS = (512, 768, 1024, 1536, 2048, 3072)
TIME_SPAN_CHOICES_S = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
SCALE_OPTIONS = (("0.1×", 0.1), ("0.2×", 0.2), ("0.5×", 0.5), ("1×", 1.0),
                 ("2×", 2.0), ("5×", 5.0), ("10×", 10.0), ("20×", 20.0))

DEFAULT_FPS = 60
DEFAULT_BUFFER_MS = 1536
DEFAULT_TIME_SPAN_S = 5.0
DEFAULT_Y_MIN = -8_388_608.0
DEFAULT_Y_MAX = 8_388_607.0
MAX_RENDER_POINTS = 6000
READER_POLL_MS = 5
STATUS_MS = 500
PRODUCER_RATE_WINDOW_S = 5.0

# Measured producer rate is used only for jitter-buffer pacing.
# Physical geophone timing comes from shared_data_v5 effective Fs.
PRODUCER_RATE_MIN_RATIO = 0.10
PRODUCER_RATE_MAX_RATIO = 10.0
IMU_SMOOTH_TAU_S = 0.10
IMU_MAX_PREDICTION_S = 1.25
IMU_STALE_S = 2.5
MODEL_Z = 1.15
BODY_AXIS_LENGTH = 1.8

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def application_icon() -> QIcon:
    for path in ([APP_ICON_ICO, APP_ICON_PNG] if os.name == "nt" else [APP_ICON_PNG, APP_ICON_ICO]):
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


def wrap_angle_deg(a: float) -> float:
    return (float(a) + 180.0) % 360.0 - 180.0


def shortest_angle_delta_deg(current: float, target: float) -> float:
    return wrap_angle_deg(float(target) - float(current))


def visual_yaw_from_compass_deg(yaw_compass: float) -> float:
    """Map compass heading to OpenGL/math yaw for the 3D view.

    Compass convention used by the IMU display:
        yaw =   0° -> North (+Y)
        yaw =  90° -> East  (+X)
        yaw = 180° -> South (-Y)
        yaw = -90° / 270° -> West (-X)

    The model nose and red body X-axis point along local +X, so to align them
    with North when yaw = 0 we use:
        visual_yaw = 90° - yaw_compass
    """
    return wrap_angle_deg(90.0 - float(yaw_compass))


def visual_pitch_deg(pitch_sensor: float) -> float:
    """
    Visual pitch convention:
        positive pitch  -> nose UP
        negative pitch  -> nose DOWN

    The 3D model points forward along local +X. In the right-handed Qt/OpenGL
    coordinate system, that requires using -pitch for the visual Y rotation.
    """
    return -float(pitch_sensor)


def rotation_matrix_rpy_deg(roll: float, pitch: float, yaw: float):
    y = visual_yaw_from_compass_deg(yaw)
    p = visual_pitch_deg(pitch)
    r, p, y = map(math.radians, (roll, p, y))
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return np.array([
        [cy*cp, cy*sp*sr - sy*cr, cy*sp*cr + sy*sr],
        [sy*cp, sy*sp*sr + cy*cr, sy*sp*cr - cy*sr],
        [-sp,   cp*sr,            cp*cr],
    ], dtype=np.float64)


def make_box_mesh(length=1.9, width=1.15, height=0.50):
    hx, hy, hz = length/2, width/2, height/2
    v = np.array([
        [-hx,-hy,-hz], [hx,-hy,-hz], [hx,hy,-hz], [-hx,hy,-hz],
        [-hx,-hy,hz],  [hx,-hy,hz],  [hx,hy,hz],  [-hx,hy,hz],
    ], dtype=np.float32)
    f = np.array([
        [0,1,2],[0,2,3],[4,6,5],[4,7,6],
        [0,4,5],[0,5,1],[1,5,6],[1,6,2],
        [2,6,7],[2,7,3],[3,7,4],[3,4,0],
    ], dtype=np.int32)
    return v, f


def make_nose_mesh():
    xb, xt, yy, zz = 0.95, 1.55, 0.52, 0.25
    v = np.array([
        [xb,-yy,-zz], [xb,yy,-zz], [xb,yy,zz], [xb,-yy,zz], [xt,0,0]
    ], dtype=np.float32)
    f = np.array([[0,1,2],[0,2,3],[0,4,1],[1,4,2],[2,4,3],[3,4,0]], dtype=np.int32)
    return v, f

# -----------------------------------------------------------------------------
# Shared-data reader
# -----------------------------------------------------------------------------
class ReaderThread(QThread):
    # snapshot, producer rate, publish gap ms, ADCStreamInfoSnapshot
    adc_ready = Signal(object, float, float, object)
    imu_ready = Signal(object)
    read_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._desired_count = int(
            (
                DEFAULT_TIME_SPAN_S
                + 4.0
            )
            * RAW_ADC_SAMPLE_RATE_HZ
        )
        self._rate_history = deque()
        self._producer_rate = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._effective_rate_hz = float(
            RAW_ADC_SAMPLE_RATE_HZ
        )
        self._adc_session_id = -1
        self._last_adc_publish: Optional[float] = None

    def stop(self):
        self._stop.set()

    def set_desired_count(self, count: int):
        with self._lock:
            self._desired_count = max(32, int(count))

    def desired_count(self):
        with self._lock:
            return self._desired_count

    def run(self):
        shared = None
        last_total = -1
        last_telemetry_ts = -1
        last_telemetry_check = 0.0
        try:
            shared = OBSSharedData()

            stream_info = shared.read_adc_stream_info()
            self._effective_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self._producer_rate = self._effective_rate_hz
            self._adc_session_id = int(
                stream_info.adc_session_id
            )

            while not self._stop.is_set():
                now = time.perf_counter()

                stream_info = shared.read_adc_stream_info()
                effective_rate_hz = max(
                    0.001,
                    float(
                        stream_info.effective_sample_rate_hz
                    ),
                )
                session_id = int(
                    stream_info.adc_session_id
                )

                if (
                    session_id != self._adc_session_id
                    or abs(
                        effective_rate_hz
                        - self._effective_rate_hz
                    )
                    > max(
                        1.0e-9,
                        1.0e-6
                        * effective_rate_hz,
                    )
                ):
                    self._adc_session_id = session_id
                    self._effective_rate_hz = effective_rate_hz
                    self._producer_rate = effective_rate_hz
                    self._rate_history.clear()
                    self._last_adc_publish = None
                    last_total = -1

                total = shared.adc_total_samples()
                if total != last_total:
                    snap = shared.read_adc_latest_numpy(self.desired_count())
                    current_total = int(snap.total_samples)
                    gap_ms = 0.0 if self._last_adc_publish is None else (now - self._last_adc_publish) * 1000.0
                    self._last_adc_publish = now
                    self._rate_history.append((now, current_total))
                    cutoff = now - PRODUCER_RATE_WINDOW_S
                    while len(self._rate_history) > 2 and self._rate_history[0][0] < cutoff:
                        self._rate_history.popleft()
                    if len(self._rate_history) >= 2:
                        t0, n0 = self._rate_history[0]
                        t1, n1 = self._rate_history[-1]
                        dt, dn = t1-t0, n1-n0
                        if dt >= 1.0 and dn > 0:
                            measured = dn/dt
                            min_rate = max(
                                0.001,
                                self._effective_rate_hz
                                * PRODUCER_RATE_MIN_RATIO,
                            )
                            max_rate = max(
                                min_rate * 2.0,
                                self._effective_rate_hz
                                * PRODUCER_RATE_MAX_RATIO,
                            )
                            if (
                                min_rate
                                <= measured
                                <= max_rate
                            ):
                                self._producer_rate = (
                                    0.80
                                    * self._producer_rate
                                    + 0.20
                                    * measured
                                )
                    last_total = current_total
                    self.adc_ready.emit(
                        snap,
                        float(
                            self._producer_rate
                        ),
                        float(
                            gap_ms
                        ),
                        stream_info,
                    )

                if now - last_telemetry_check >= 0.02:
                    last_telemetry_check = now
                    tel = shared.read_telemetry()
                    if int(tel.timestamp_ns) != last_telemetry_ts:
                        last_telemetry_ts = int(tel.timestamp_ns)
                        self.imu_ready.emit(tel)

                self.msleep(READER_POLL_MS)
        except Exception as exc:
            if not self._stop.is_set():
                self.read_error.emit(str(exc))
        finally:
            if shared is not None:
                try:
                    shared.close()
                except Exception:
                    pass

# -----------------------------------------------------------------------------
# Main window
# -----------------------------------------------------------------------------
class GeophoneIMUWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        if np is None or pg is None:
            raise RuntimeError("NumPy and PyQtGraph are required.")

        self.shared = OBSSharedData()

        try:
            stream_info = self.shared.read_adc_stream_info()
            self.raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self.effective_sample_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self.decimation_samples = max(
                1,
                int(
                    stream_info.decimation_samples
                ),
            )
            self.decimation_mode = str(
                stream_info.decimation_mode
            )
            self.adc_session_id = int(
                stream_info.adc_session_id
            )
        except Exception:
            self.raw_sample_rate_hz = float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            self.effective_sample_rate_hz = float(
                RAW_ADC_SAMPLE_RATE_HZ
            )
            self.decimation_samples = 1
            self.decimation_mode = "raw"
            self.adc_session_id = -1

        self.cached_adc = None
        self.cached_total = -1
        self.producer_rate_hz = float(
            self.effective_sample_rate_hz
        )
        self.publish_gap_ms = 0.0
        self.playhead_sample: Optional[float] = None
        self.playhead_wall_ns: Optional[int] = None
        self.reserve_ms = 0.0
        self.underruns = 0
        self.in_underrun = False

        self.latest_telemetry = None
        self.target_roll = self.target_pitch = self.target_yaw = 0.0
        self.rate_p = self.rate_q = self.rate_r = 0.0
        self.imu_received_monotonic = time.perf_counter()
        self.display_roll = self.display_pitch = self.display_yaw = 0.0
        self.last_orientation_t = time.perf_counter()

        self.paused = False
        self.render_fps = 0.0
        self.render_jitter_ms = 0.0
        self._fps_count = 0
        self._fps_start = time.perf_counter()
        self._last_render_ns = None
        self._last_auto_y = 0.0
        self._last_label = 0.0

        self.wave_gl = False
        self.wave_gl_error = ""
        self.plots = []
        self.curves = []
        self.scale_combos = []

        self.gl_view = None
        self.model_body = None
        self.model_nose = None
        self.body_axis_x = self.body_axis_y = self.body_axis_z = None

        self.setWindowTitle(f"{APP_TITLE} - {SYSTEM_TITLE}")
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1500, 880)
        self.setMinimumSize(1120, 680)

        self._configure_pg()
        self._build_ui()
        self._apply_style()
        self._install_shortcuts()

        self.reader = ReaderThread(self)
        self.reader.adc_ready.connect(self.on_adc)
        self.reader.imu_ready.connect(self.on_imu)
        self.reader.read_error.connect(lambda m: self.connection_label.setText(f"Reader error: {m}"))
        self.reader.start()
        self._update_reader_window()

        self.render_timer = QTimer(self)
        try:
            self.render_timer.setTimerType(Qt.TimerType.PreciseTimer)
        except Exception:
            pass
        self.render_timer.timeout.connect(self.render_frame)
        self._set_fps(DEFAULT_FPS)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(STATUS_MS)
        self.refresh_status()

    @staticmethod
    def _configure_pg():
        try:
            pg.setConfigOptions(useOpenGL=True, antialias=False,
                                background="#07131D", foreground="#DDEAF2")
        except Exception:
            pg.setConfigOptions(useOpenGL=False, antialias=False,
                                background="#07131D", foreground="#DDEAF2")

    def _set_wave_gl_viewport(self, graphics):
        if QOpenGLWidget is None:
            self.wave_gl_error = "QOpenGLWidget unavailable"
            return
        try:
            vp = QOpenGLWidget()
            fmt = QSurfaceFormat()
            fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
            fmt.setSamples(0)
            fmt.setSwapInterval(0)
            vp.setFormat(fmt)
            graphics.setViewport(vp)
            self.wave_gl = isinstance(graphics.viewport(), QOpenGLWidget)
        except Exception as exc:
            self.wave_gl_error = str(exc)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget(); central.setObjectName("centralWidget"); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(14,12,14,12); root.setSpacing(8)

        head = QHBoxLayout()
        title_box = QVBoxLayout(); title_box.setSpacing(1)
        t = QLabel("GEOPHONE + IMU"); t.setObjectName("titleLabel")
        s = QLabel("CH0 / X  •  CH1 / Y  •  CH2 / Z  •  3D Roll / Pitch / Yaw Orientation")
        s.setObjectName("subtitleLabel")
        title_box.addWidget(t); title_box.addWidget(s); head.addLayout(title_box, 1)
        self.pause_button = QPushButton("Pause"); self.pause_button.setObjectName("pauseButton")
        self.pause_button.setCheckable(True); self.pause_button.setMinimumWidth(125)
        self.pause_button.clicked.connect(self.toggle_pause); head.addWidget(self.pause_button)
        root.addLayout(head)

        sf = QFrame(); sf.setObjectName("statusFrame"); sl = QHBoxLayout(sf); sl.setContentsMargins(10,6,10,6)
        self.connection_label = QLabel("Shared RAM: checking..."); self.connection_label.setObjectName("statusLabel")
        self.stream_label = QLabel("ADC: --"); self.stream_label.setObjectName("statusLabel")
        self.imu_status_label = QLabel("IMU: --"); self.imu_status_label.setObjectName("statusLabel")
        self.render_label = QLabel("Render: --"); self.render_label.setObjectName("statusLabel")
        self.mode_label = QLabel("LIVE"); self.mode_label.setObjectName("modeLive")
        sl.addWidget(self.connection_label); sl.addStretch(1); sl.addWidget(self.stream_label); sl.addSpacing(12)
        sl.addWidget(self.imu_status_label); sl.addSpacing(12); sl.addWidget(self.render_label); sl.addSpacing(12); sl.addWidget(self.mode_label)
        root.addWidget(sf)

        splitter = QSplitter(Qt.Horizontal); splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_wave_panel()); splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0,2); splitter.setStretchFactor(1,1); splitter.setSizes([1000,500])
        root.addWidget(splitter, 1)

    def _build_wave_panel(self):
        panel = QFrame(); panel.setObjectName("plotPanel")
        lay = QVBoxLayout(panel); lay.setContentsMargins(0,0,0,0)
        self.graphics = pg.GraphicsLayoutWidget(); self._set_wave_gl_viewport(self.graphics); lay.addWidget(self.graphics,1)
        pens = (pg.mkPen("#5CC8FF", width=1), pg.mkPen("#8EE28E", width=1), pg.mkPen("#FFD166", width=1))
        for i,(ch,axis,_attr) in enumerate(CHANNELS):
            plot = self.graphics.addPlot(row=i,col=0)
            plot.showGrid(x=True,y=True,alpha=0.18); plot.setMouseEnabled(x=True,y=True); plot.setClipToView(True)
            plot.setDownsampling(ds=1,auto=False,mode="subsample")
            plot.setLabel("left", f"{ch} / {axis}", units="count"); plot.setLabel("bottom","Time",units="s")
            plot.setTitle(f"{ch} — {axis}", color="#FFFFFF", size="11pt")
            plot.setXRange(-DEFAULT_TIME_SPAN_S,0,padding=0); plot.setYRange(DEFAULT_Y_MIN,DEFAULT_Y_MAX,padding=0)
            self.plots.append(plot); self.curves.append(plot.plot([],[],pen=pens[i]))
        return panel

    def _build_right_panel(self):
        panel = QFrame(); panel.setObjectName("orientationPanel")
        lay = QVBoxLayout(panel); lay.setContentsMargins(8,0,0,0); lay.setSpacing(8)
        h = QLabel("IMU ORIENTATION"); h.setObjectName("settingsTitle"); lay.addWidget(h)

        model_frame = QFrame(); model_frame.setObjectName("modelFrame"); ml = QVBoxLayout(model_frame); ml.setContentsMargins(0,0,0,0)
        if gl is not None:
            self.gl_view = gl.GLViewWidget(); self.gl_view.setCameraPosition(distance=9.0,elevation=25.0,azimuth=-45.0)
            ml.addWidget(self.gl_view,1); self._build_3d_scene()
        else:
            miss = QLabel("3D OpenGL unavailable.\n\nInstall:\npip install PyOpenGL PyOpenGL_accelerate")
            miss.setAlignment(Qt.AlignCenter); miss.setWordWrap(True); miss.setObjectName("missing3DLabel"); ml.addWidget(miss,1)
        lay.addWidget(model_frame, 5)

        ag = QGroupBox("Attitude"); ag.setObjectName("channelGroup"); g = QGridLayout(ag); g.setContentsMargins(10,12,10,10)
        self.roll_value = QLabel("0.00°"); self.pitch_value = QLabel("0.00°"); self.yaw_value = QLabel("0.00°")
        for lab in (self.roll_value,self.pitch_value,self.yaw_value): lab.setObjectName("angleValue"); lab.setAlignment(Qt.AlignCenter)
        for col,name in enumerate(("ROLL","PITCH","YAW")): g.addWidget(QLabel(name),0,col)
        g.addWidget(self.roll_value,1,0); g.addWidget(self.pitch_value,1,1); g.addWidget(self.yaw_value,1,2)
        self.p_value=QLabel("P: 0.00 °/s"); self.q_value=QLabel("Q: 0.00 °/s"); self.r_value=QLabel("R: 0.00 °/s")
        for col,lab in enumerate((self.p_value,self.q_value,self.r_value)):
            lab.setObjectName("rateValue"); lab.setAlignment(Qt.AlignCenter); g.addWidget(lab,2,col)
        self.imu_age_label = QLabel("IMU age: --"); self.imu_age_label.setObjectName("sampleInfo"); g.addWidget(self.imu_age_label,3,0,1,3)
        lay.addWidget(ag)

        cg = QGroupBox("Display Controls"); cg.setObjectName("channelGroup"); c = QGridLayout(cg); c.setContentsMargins(10,12,10,10)
        self.fps_combo = QComboBox(); [self.fps_combo.addItem(f"{v} FPS",v) for v in FPS_CHOICES]
        self.fps_combo.setCurrentText(f"{DEFAULT_FPS} FPS"); self.fps_combo.currentIndexChanged.connect(lambda *_: self._set_fps(self.current_fps()))
        self.buffer_combo = QComboBox(); [self.buffer_combo.addItem(f"{v} ms",v) for v in BUFFER_CHOICES_MS]
        self.buffer_combo.setCurrentText(f"{DEFAULT_BUFFER_MS} ms"); self.buffer_combo.currentIndexChanged.connect(self.on_buffer_changed)
        self.time_combo = QComboBox(); [self.time_combo.addItem(f"{v:g} s",float(v)) for v in TIME_SPAN_CHOICES_S]
        self.time_combo.setCurrentText(f"{DEFAULT_TIME_SPAN_S:g} s"); self.time_combo.currentIndexChanged.connect(self.on_time_changed)
        c.addWidget(QLabel("Target FPS"),0,0); c.addWidget(self.fps_combo,0,1)
        c.addWidget(QLabel("Smooth Buffer"),1,0); c.addWidget(self.buffer_combo,1,1)
        c.addWidget(QLabel("Time Span"),2,0); c.addWidget(self.time_combo,2,1)

        for row,(ch,axis,_attr) in enumerate(CHANNELS,start=3):
            combo=QComboBox(); [combo.addItem(lbl,val) for lbl,val in SCALE_OPTIONS]; combo.setCurrentText("1×")
            self.scale_combos.append(combo); c.addWidget(QLabel(f"{ch} {axis} Scale"),row,0); c.addWidget(combo,row,1)

        self.y_min = QDoubleSpinBox(); self.y_min.setRange(-100_000_000,100_000_000); self.y_min.setDecimals(0); self.y_min.setValue(DEFAULT_Y_MIN); self.y_min.setGroupSeparatorShown(True)
        self.y_max = QDoubleSpinBox(); self.y_max.setRange(-100_000_000,100_000_000); self.y_max.setDecimals(0); self.y_max.setValue(DEFAULT_Y_MAX); self.y_max.setGroupSeparatorShown(True)
        c.addWidget(QLabel("Amp Min"),6,0); c.addWidget(self.y_min,6,1); c.addWidget(QLabel("Amp Max"),7,0); c.addWidget(self.y_max,7,1)
        apply_y=QPushButton("Apply Amplitude Range"); apply_y.setObjectName("smallPrimaryButton"); apply_y.clicked.connect(self.apply_y_range); c.addWidget(apply_y,8,0,1,2)
        self.auto_y = QCheckBox("Auto Y Range — all channels"); self.auto_y.stateChanged.connect(lambda *_: None if self.auto_y.isChecked() else self.apply_y_range()); c.addWidget(self.auto_y,9,0,1,2)
        self.predict_rates = QCheckBox("Predict orientation using P / Q / R"); self.predict_rates.setChecked(True); c.addWidget(self.predict_rates,10,0,1,2)
        note=QLabel("3D reference: ground grid = world plane; North arrow and compass labels N/E/S/W are shown. Compass yaw convention: 0° = North, 90° = East. Pitch convention: positive = nose UP, negative = nose DOWN. World/body XYZ axes shown. Visual Euler order: Yaw(Z) → Pitch(Y) → Roll(X).")
        note.setObjectName("sampleInfo"); note.setWordWrap(True); c.addWidget(note,11,0,1,2)
        lay.addWidget(cg)
        return panel

    def _build_3d_scene(self):
        ground = gl.GLGridItem(); ground.setSize(x=10,y=10,z=1); ground.setSpacing(x=1,y=1,z=1); self.gl_view.addItem(ground)
        world = gl.GLAxisItem(); world.setSize(x=3,y=3,z=3); self.gl_view.addItem(world)

        # Compass reference on the ground plane.
        # World convention:
        #   +Y = North, +X = East, +Z = Up.
        north_x = -3.3
        north_y0 = -2.7
        north_y1 = 2.9
        north_z = 0.035
        head_half = 0.26
        head_back = 0.38
        north_points = np.array([
            [north_x, north_y0, north_z],
            [north_x, north_y1, north_z],
            [north_x, north_y1, north_z],
            [north_x - head_half, north_y1 - head_back, north_z],
            [north_x, north_y1, north_z],
            [north_x + head_half, north_y1 - head_back, north_z],
        ], dtype=np.float32)
        self.north_arrow = gl.GLLinePlotItem(
            pos=north_points,
            color=(1.0, 0.95, 0.35, 1.0),
            width=3.0,
            antialias=False,
            mode="lines",
        )
        self.gl_view.addItem(self.north_arrow)

        self.compass_labels = []
        if hasattr(gl, "GLTextItem"):
            try:
                compass_specs = [
                    ("N", (0.0, 4.7, 0.05)),
                    ("E", (4.7, 0.0, 0.05)),
                    ("S", (0.0, -4.7, 0.05)),
                    ("W", (-4.7, 0.0, 0.05)),
                ]
                for text, pos in compass_specs:
                    item = gl.GLTextItem(pos=pos, text=text, color=(1.0, 1.0, 1.0, 1.0))
                    self.gl_view.addItem(item)
                    self.compass_labels.append(item)
            except Exception:
                self.compass_labels = []

        v,f = make_box_mesh(); md=gl.MeshData(vertexes=v,faces=f)
        self.model_body=gl.GLMeshItem(meshdata=md,smooth=False,drawFaces=True,drawEdges=True,
                                      edgeColor=(0.85,0.90,0.95,0.85),color=(0.12,0.38,0.58,0.95)); self.gl_view.addItem(self.model_body)
        v,f = make_nose_mesh(); md=gl.MeshData(vertexes=v,faces=f)
        self.model_nose=gl.GLMeshItem(meshdata=md,smooth=False,drawFaces=True,drawEdges=True,
                                      edgeColor=(1,1,1,0.9),color=(0.75,0.30,0.12,0.95)); self.gl_view.addItem(self.model_nose)
        center=np.array([0,0,MODEL_Z],dtype=np.float32)
        self.body_axis_x=gl.GLLinePlotItem(pos=np.array([center,center+[BODY_AXIS_LENGTH,0,0]],dtype=np.float32),color=(1,.25,.25,1),width=3,antialias=False,mode="lines")
        self.body_axis_y=gl.GLLinePlotItem(pos=np.array([center,center+[0,BODY_AXIS_LENGTH,0]],dtype=np.float32),color=(.25,1,.35,1),width=3,antialias=False,mode="lines")
        self.body_axis_z=gl.GLLinePlotItem(pos=np.array([center,center+[0,0,BODY_AXIS_LENGTH]],dtype=np.float32),color=(.30,.55,1,1),width=3,antialias=False,mode="lines")
        for item in (self.body_axis_x,self.body_axis_y,self.body_axis_z): self.gl_view.addItem(item)
        self._update_3d_model(0,0,0)

    # -------------------------------------------------------------- settings
    def current_fps(self): return int(self.fps_combo.currentData() or DEFAULT_FPS)
    def current_buffer_ms(self): return int(self.buffer_combo.currentData() or DEFAULT_BUFFER_MS)
    def current_time_span(self): return float(self.time_combo.currentData() or DEFAULT_TIME_SPAN_S)

    def _set_fps(self, fps):
        self.render_timer.start(max(1,round(1000/max(1,int(fps)))))

    def on_buffer_changed(self,*_):
        self.playhead_sample=None; self.playhead_wall_ns=None; self._update_reader_window()

    def on_time_changed(self,*_):
        span=self.current_time_span()
        for p in self.plots: p.setXRange(-span,0,padding=0)
        self._update_reader_window()

    def current_sample_rate_hz(self):
        return max(
            0.001,
            float(
                self.effective_sample_rate_hz
            ),
        )

    def _update_reader_window(self):
        if hasattr(self,"reader"):
            seconds=self.current_time_span()+self.current_buffer_ms()/1000.0+1.0
            self.reader.set_desired_count(
                int(
                    seconds
                    * self.current_sample_rate_hz()
                )
                + 32
            )

    def apply_y_range(self):
        ymin,ymax=float(self.y_min.value()),float(self.y_max.value())
        if ymin>=ymax:
            QMessageBox.warning(self,APP_TITLE,"Amp Min must be lower than Amp Max."); return
        self.auto_y.setChecked(False)
        for p in self.plots: p.setYRange(ymin,ymax,padding=0)

    # -------------------------------------------------------------- callbacks
    def on_adc(
        self,
        snap,
        producer_rate,
        publish_gap,
        stream_info,
    ):
        previous = self.cached_total
        previous_session_id = int(
            self.adc_session_id
        )
        previous_effective_rate = float(
            self.effective_sample_rate_hz
        )

        self.cached_adc = snap
        self.cached_total = int(
            snap.total_samples
        )

        self.raw_sample_rate_hz = float(
            stream_info.raw_sample_rate_hz
        )
        self.effective_sample_rate_hz = max(
            0.001,
            float(
                stream_info.effective_sample_rate_hz
            ),
        )
        self.decimation_samples = max(
            1,
            int(
                stream_info.decimation_samples
            ),
        )
        self.decimation_mode = str(
            stream_info.decimation_mode
        )
        self.adc_session_id = int(
            stream_info.adc_session_id
        )

        min_rate = max(
            0.001,
            self.effective_sample_rate_hz
            * PRODUCER_RATE_MIN_RATIO,
        )
        max_rate = max(
            min_rate * 2.0,
            self.effective_sample_rate_hz
            * PRODUCER_RATE_MAX_RATIO,
        )

        if (
            min_rate
            <= float(
                producer_rate
            )
            <= max_rate
        ):
            self.producer_rate_hz = float(
                producer_rate
            )

        incoming_gap = float(
            publish_gap
        )
        self.publish_gap_ms = (
            incoming_gap
            if self.publish_gap_ms <= 0.0
            else (
                0.85
                * self.publish_gap_ms
                + 0.15
                * incoming_gap
            )
        )

        session_changed = (
            self.adc_session_id
            != previous_session_id
        )
        rate_changed = (
            abs(
                self.effective_sample_rate_hz
                - previous_effective_rate
            )
            > max(
                1.0e-9,
                1.0e-6
                * self.effective_sample_rate_hz,
            )
        )
        counter_reset = (
            previous >= 0
            and self.cached_total
            < previous
        )

        if (
            session_changed
            or rate_changed
            or counter_reset
        ):
            self.producer_rate_hz = float(
                self.effective_sample_rate_hz
            )
            self.publish_gap_ms = 0.0
            self.playhead_sample = None
            self.playhead_wall_ns = None
            self.underruns = 0
            self.in_underrun = False
            self._update_reader_window()

    def on_imu(self,tel):
        self.latest_telemetry=tel
        self.target_roll=float(tel.roll); self.target_pitch=float(tel.pitch); self.target_yaw=float(tel.yaw)
        self.rate_p=float(tel.angular_rate_p); self.rate_q=float(tel.angular_rate_q); self.rate_r=float(tel.angular_rate_r)
        self.imu_received_monotonic=time.perf_counter()

    # -------------------------------------------------------------- playhead
    def _display_sample_index(self, adc):
        total=int(adc.total_samples)
        if total<=1: return 0.0
        latest = float(
            total - 1
        )
        oldest = float(
            total
            - len(
                adc.ch0
            )
        )

        effective_rate = (
            self.current_sample_rate_hz()
        )
        min_rate = max(
            0.001,
            effective_rate
            * PRODUCER_RATE_MIN_RATIO,
        )
        max_rate = max(
            min_rate * 2.0,
            effective_rate
            * PRODUCER_RATE_MAX_RATIO,
        )
        rate = max(
            min_rate,
            min(
                max_rate,
                float(
                    self.producer_rate_hz
                ),
            ),
        )
        buffer_samples=self.current_buffer_ms()/1000.0*rate
        gap_s=max(0.001,self.publish_gap_ms/1000.0)
        target=max(buffer_samples*0.70,(3.0*gap_s+0.100)*rate)
        safety=max(buffer_samples*0.35,(2.0*gap_s+0.050)*rate,32.0)
        history=max(64.0,latest-oldest); target=min(target,history*0.80); safety=min(safety,max(32.0,target*0.75))
        now=time.perf_counter_ns()
        if self.playhead_sample is None or self.playhead_wall_ns is None:
            self.playhead_sample=max(oldest,latest-target); self.playhead_wall_ns=now; self.in_underrun=False; return self.playhead_sample
        elapsed=max(0.0,(now-self.playhead_wall_ns)/1e9)
        reserve=latest-self.playhead_sample; self.reserve_ms=reserve/max(1.0,rate)*1000.0
        err=(reserve-target)/max(1.0,target); corr=max(-0.25,min(0.12,err*0.40)); proposed=self.playhead_sample+elapsed*rate*(1+corr)
        max_play=latest-safety
        if proposed>max_play:
            proposed=max_play
            if not self.in_underrun: self.underruns+=1; self.in_underrun=True
        elif reserve>safety+max(16.0,rate*gap_s*0.5): self.in_underrun=False
        self.playhead_sample=max(oldest,proposed); self.playhead_wall_ns=now; return self.playhead_sample

    @staticmethod
    def _downsample(x,y):
        if len(y)<=MAX_RENDER_POINTS: return x,y,1
        step=int(np.ceil(len(y)/MAX_RENDER_POINTS)); return x[::step],y[::step],step

    def _gap_breaks(self,x,y,step):
        if len(y)<2: return y,"all"
        expected = (
            max(
                1,
                int(
                    step
                ),
            )
            / self.current_sample_rate_hz()
        )
        gaps=np.flatnonzero(np.diff(x)>expected*1.75)
        if not len(gaps): return y,"all"
        out=y.astype(np.float64,copy=True); out[gaps+1]=np.nan; return out,"finite"

    def _render_waveforms(self, auto_y_update=False):
        adc = self.cached_adc

        if (
            adc is None
            or len(
                adc.ch0
            ) < 2
        ):
            return

        ts = adc.timestamp_ns
        n = len(
            adc.ch0
        )
        play = self._display_sample_index(
            adc
        )
        cache_start = int(
            adc.total_samples
        ) - n
        end=int(np.floor(play-cache_start))+1; end=max(0,min(n,end))
        if end<2: return
        frac = play - np.floor(
            play
        )
        display_ns = int(
            ts[
                end - 1
            ]
            + frac
            * (
                1.0e9
                / self.current_sample_rate_hz()
            )
        )
        start_ns=display_ns-int(self.current_time_span()*1e9); start=int(np.searchsorted(ts[:end],start_ns,side="left"))
        vis_ts=ts[start:end]
        if len(vis_ts)<2: return
        x=(vis_ts.astype(np.float64,copy=False)-float(display_ns))/1e9
        for i,(_ch,_axis,attr) in enumerate(CHANNELS):
            raw=getattr(adc,attr)[start:end]
            scale=float(self.scale_combos[i].currentData() or 1.0)
            y=raw if scale==1.0 else raw.astype(np.float64,copy=False)*scale
            xr,yr,step=self._downsample(x,y); yr,connect=self._gap_breaks(xr,yr,step)
            self.curves[i].setData(xr,yr,connect=connect)
            if self.auto_y.isChecked() and auto_y_update:
                finite=yr[np.isfinite(yr)]
                if len(finite):
                    ymin,ymax=float(np.min(finite)),float(np.max(finite)); margin=max(1.0,(ymax-ymin)*0.08)
                    self.plots[i].setYRange(ymin-margin,ymax+margin,padding=0)

    # -------------------------------------------------------------- IMU
    def _predicted_target(self):
        r,p,y=self.target_roll,self.target_pitch,self.target_yaw
        if self.latest_telemetry is not None and self.predict_rates.isChecked():
            dt=min(max(0.0,time.perf_counter()-self.imu_received_monotonic),IMU_MAX_PREDICTION_S)
            r+=self.rate_p*dt; p+=self.rate_q*dt; y+=self.rate_r*dt
        return wrap_angle_deg(r),wrap_angle_deg(p),wrap_angle_deg(y)

    def _update_orientation(self):
        now=time.perf_counter(); dt=max(0.0,now-self.last_orientation_t); self.last_orientation_t=now
        tr,tp,ty=self._predicted_target(); alpha=1-math.exp(-dt/max(1e-4,IMU_SMOOTH_TAU_S))
        self.display_roll=wrap_angle_deg(self.display_roll+shortest_angle_delta_deg(self.display_roll,tr)*alpha)
        self.display_pitch=wrap_angle_deg(self.display_pitch+shortest_angle_delta_deg(self.display_pitch,tp)*alpha)
        self.display_yaw=wrap_angle_deg(self.display_yaw+shortest_angle_delta_deg(self.display_yaw,ty)*alpha)
        self._update_3d_model(self.display_roll,self.display_pitch,self.display_yaw)

    def _update_3d_model(self,roll,pitch,yaw):
        if gl is None or self.gl_view is None or self.model_body is None: return
        visual_yaw = visual_yaw_from_compass_deg(yaw)
        visual_pitch = visual_pitch_deg(pitch)
        m=QMatrix4x4(); m.translate(0,0,MODEL_Z); m.rotate(float(visual_yaw),0,0,1); m.rotate(float(visual_pitch),0,1,0); m.rotate(float(roll),1,0,0)
        self.model_body.setTransform(m); self.model_nose.setTransform(m)
        R=rotation_matrix_rpy_deg(roll,pitch,yaw); center=np.array([0,0,MODEL_Z],dtype=np.float64)
        axes=(np.array([BODY_AXIS_LENGTH,0,0]),np.array([0,BODY_AXIS_LENGTH,0]),np.array([0,0,BODY_AXIS_LENGTH]))
        for vec,item in zip(axes,(self.body_axis_x,self.body_axis_y,self.body_axis_z)):
            item.setData(pos=np.array([center,center+R@vec],dtype=np.float32))

    def _update_numeric(self):
        self.roll_value.setText(f"{self.display_roll:+.2f}°"); self.pitch_value.setText(f"{self.display_pitch:+.2f}°"); self.yaw_value.setText(f"{self.display_yaw:+.2f}°")
        self.p_value.setText(f"P: {self.rate_p:+.2f} °/s"); self.q_value.setText(f"Q: {self.rate_q:+.2f} °/s"); self.r_value.setText(f"R: {self.rate_r:+.2f} °/s")
        if self.latest_telemetry is None: self.imu_age_label.setText("IMU age: --"); return
        age=max(0.0,(time.time_ns()-int(self.latest_telemetry.timestamp_ns))/1e6); state="STALE" if age>IMU_STALE_S*1000 else "LIVE"
        self.imu_age_label.setText(f"IMU age: {age:.0f} ms • {state} • Device ID {self.latest_telemetry.ahrs_device_id}")

    # -------------------------------------------------------------- render
    def render_frame(self):
        if self.paused: return
        now=time.perf_counter(); auto_update=(now-self._last_auto_y)>=0.10; label_update=(now-self._last_label)>=0.10
        self._render_waveforms(auto_update); self._update_orientation()
        if auto_update: self._last_auto_y=now
        if label_update: self._update_numeric(); self._last_label=now
        self._update_metrics()

    def _update_metrics(self):
        now_ns=time.perf_counter_ns()
        if self._last_render_ns is not None:
            dt_ms=(now_ns-self._last_render_ns)/1e6; target_ms=1000.0/self.current_fps(); jitter=abs(dt_ms-target_ms)
            self.render_jitter_ms=0.90*self.render_jitter_ms+0.10*jitter
        self._last_render_ns=now_ns; self._fps_count+=1; now=time.perf_counter(); elapsed=now-self._fps_start
        if elapsed>=0.75:
            self.render_fps=self._fps_count/elapsed; self._fps_count=0; self._fps_start=now

    # -------------------------------------------------------------- pause/status
    def toggle_pause(self,checked):
        self.paused=bool(checked)
        if self.paused:
            self.pause_button.setText("Continue"); self.mode_label.setText("PAUSED"); self.mode_label.setObjectName("modePaused")
        else:
            self.pause_button.setText("Pause"); self.mode_label.setText("LIVE"); self.mode_label.setObjectName("modeLive")
            self.playhead_wall_ns=time.perf_counter_ns(); self.last_orientation_t=time.perf_counter()
        self.mode_label.style().unpolish(self.mode_label); self.mode_label.style().polish(self.mode_label)

    def toggle_pause_shortcut(self):
        checked=not self.pause_button.isChecked(); self.pause_button.setChecked(checked); self.toggle_pause(checked)

    def refresh_status(self):
        try:
            tel = self.shared.read_telemetry()
            bulk = self.shared.read_bulk_status()
            total = self.shared.adc_total_samples()
            stream_info = self.shared.read_adc_stream_info()

            self.raw_sample_rate_hz = float(
                stream_info.raw_sample_rate_hz
            )
            self.effective_sample_rate_hz = max(
                0.001,
                float(
                    stream_info.effective_sample_rate_hz
                ),
            )
            self.decimation_samples = max(
                1,
                int(
                    stream_info.decimation_samples
                ),
            )
            self.decimation_mode = str(
                stream_info.decimation_mode
            )
            self.adc_session_id = int(
                stream_info.adc_session_id
            )

            self.connection_label.setText(
                "Shared RAM: DATA CONNECTED"
                if tel.data_connected
                else "Shared RAM: DATA NOT CONNECTED"
            )

            self.stream_label.setText(
                (
                    f"ADC {total:,} | "
                    f"Fs {self.effective_sample_rate_hz:6.1f} Hz "
                    f"(raw {self.raw_sample_rate_hz:6.1f}/"
                    f"N{self.decimation_samples}) | "
                    f"producer {self.producer_rate_hz:6.1f} Hz | "
                    f"reserve {self.reserve_ms:3.0f} ms | "
                    f"underrun {self.underruns} | "
                    f"drop {bulk.dropped_frames} | "
                    f"session {self.adc_session_id}"
                )
            )

            age = max(
                0.0,
                (
                    time.time_ns()
                    - int(
                        tel.timestamp_ns
                    )
                )
                / 1e6,
            )

            imu_state = (
                (
                    "IMU LIVE"
                    if age <= IMU_STALE_S * 1000
                    else "IMU STALE"
                )
                if tel.command_connected
                else "COMMAND NOT CONNECTED"
            )

            self.imu_status_label.setText(
                (
                    f"{imu_state} | "
                    f"R {tel.roll:+.1f}° "
                    f"P {tel.pitch:+.1f}° "
                    f"Y {tel.yaw:+.1f}°"
                )
            )

            wave = (
                "Wave GL"
                if self.wave_gl
                else "Wave Raster"
            )
            model = (
                "3D GL"
                if gl is not None
                else "3D unavailable"
            )

            self.render_label.setText(
                (
                    f"Render {self.render_fps:4.1f} FPS | "
                    f"jitter {self.render_jitter_ms:3.1f} ms | "
                    f"{wave} + {model}"
                )
            )

            tip = (
                f"Executable: {sys.executable}\n"
                "Assign this executable to NVIDIA High performance GPU in "
                "Windows Graphics Settings if desired.\n"
                "Waveforms use one OpenGL viewport; 3D uses "
                "GLViewWidget/OpenGL.\n"
                "IMU convention: yaw 0°=North, yaw 90°=East; "
                "positive pitch=nose UP."
            )

            if self.wave_gl_error:
                tip = (
                    self.wave_gl_error
                    + "\n\n"
                    + tip
                )

            self.render_label.setToolTip(
                tip
            )

        except Exception as exc:
            self.connection_label.setText(
                f"Shared RAM status error: {exc}"
            )

    def _install_shortcuts(self):
        act=QAction(self); act.setShortcut(QKeySequence(Qt.Key_Space)); act.triggered.connect(self.toggle_pause_shortcut); self.addAction(act)

    # -------------------------------------------------------------- style
    def _apply_style(self):
        self.setStyleSheet("""
        QMainWindow, QWidget#centralWidget { background:#07131D; color:#FFFFFF; font-family:'Segoe UI','Arial'; }
        QLabel { background:transparent; color:#FFFFFF; }
        QLabel#titleLabel { font-size:20px; font-weight:800; letter-spacing:.8px; }
        QLabel#subtitleLabel { color:#A9BECA; font-size:10px; }
        QFrame#statusFrame { background:#0B1B27; border:1px solid #17374A; border-radius:8px; }
        QLabel#statusLabel { color:#B7CBD6; font-size:10px; }
        QLabel#modeLive { background:#123A2D; border:1px solid #2D8E66; border-radius:7px; color:#A9F1D2; font-weight:800; padding:3px 10px; }
        QLabel#modePaused { background:#403510; border:1px solid #A88821; border-radius:7px; color:#FFE49A; font-weight:800; padding:3px 10px; }
        QLabel#settingsTitle { font-size:12px; font-weight:800; letter-spacing:1px; }
        QFrame#modelFrame { background:#07131D; border:1px solid #1A3D52; border-radius:9px; }
        QLabel#missing3DLabel { color:#FFDCA8; font-size:11px; padding:18px; }
        QGroupBox#channelGroup { background:#0D1E2A; border:1px solid #1A3D52; border-radius:9px; margin-top:11px; padding-top:6px; font-weight:800; color:#FFFFFF; }
        QGroupBox#channelGroup::title { subcontrol-origin:margin; left:9px; padding:0 5px; color:#FFFFFF; }
        QLabel#angleValue { background:#091821; border:1px solid #24485D; border-radius:7px; color:#FFFFFF; font-family:'Consolas'; font-size:20px; font-weight:800; padding:6px; }
        QLabel#rateValue { color:#B8CBD6; font-family:'Consolas'; font-size:10px; }
        QLabel#sampleInfo { color:#7894A4; font-size:9px; }
        QDoubleSpinBox, QComboBox { background:#071620; color:#FFFFFF; border:1px solid #24485D; border-radius:5px; min-height:25px; padding:1px 5px; }
        QComboBox::drop-down { width:24px; border-left:1px solid #24485D; background:#0E2533; }
        QComboBox QAbstractItemView { background:#0B1B26; color:#F4FAFD; border:1px solid #2B526A; selection-background-color:#245B79; selection-color:#FFFFFF; outline:none; }
        QComboBox QAbstractItemView::item { color:#F4FAFD; background:#0B1B26; min-height:26px; padding:4px 8px; }
        QComboBox QAbstractItemView::item:selected { color:#FFFFFF; background:#245B79; }
        QCheckBox { color:#DDE9EF; spacing:6px; }
        QPushButton { min-height:28px; border-radius:6px; padding:3px 7px; font-weight:700; }
        QPushButton#pauseButton, QPushButton#smallPrimaryButton { background:#17678F; color:#FFFFFF; border:1px solid #2D8AB6; }
        QPushButton#pauseButton:checked { background:#705C16; border:1px solid #B49326; }
        QSplitter::handle { background:#17374A; width:2px; }
        """)

    def closeEvent(self,event:QCloseEvent):
        try: self.render_timer.stop(); self.status_timer.stop()
        except Exception: pass
        try: self.reader.stop(); self.reader.wait(2000)
        except Exception: pass
        try: self.shared.close()
        except Exception: pass
        release_windows_runtime(); event.accept()

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    if QOpenGLWidget is not None:
        try:
            fmt=QSurfaceFormat(); fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer); fmt.setSamples(0); fmt.setSwapInterval(0)
            QSurfaceFormat.setDefaultFormat(fmt)
        except Exception:
            pass

    app=QApplication(sys.argv); app.setApplicationName(APP_TITLE); app.setApplicationDisplayName(f"{APP_TITLE} - {SYSTEM_TITLE}")
    icon=application_icon()
    if not icon.isNull(): app.setWindowIcon(icon)
    font=QFont("Segoe UI"); font.setPointSize(9); app.setFont(font)

    if np is None or pg is None:
        missing=[]
        if np is None: missing.append("numpy")
        if pg is None: missing.append("pyqtgraph")
        QMessageBox.critical(None,APP_TITLE,"Missing package(s): "+", ".join(missing)+"\n\nInstall: pip install numpy pyqtgraph")
        return 1

    try:
        w=GeophoneIMUWindow()
    except Exception as exc:
        QMessageBox.critical(None,APP_TITLE,f"Cannot start Geophone + IMU:\n\n{exc}")
        return 1
    w.show(); return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
