"""
position.py
===========

GRC-UGM-PERTAMINA OBS
GNSS / USBL Position Map

Version: 2
Shared data: shared_data_v5.py

Layout
------
- Left  1/5 : settings / source / position status / GeoTIFF overlays
- Right 4/5 : interactive online map

Features
--------
- Read GNSS and USBL positions from shared RAM.
- GNSS marker = circle.
- USBL marker = triangle.
- When BOTH GNSS and USBL have no valid position, a circle and triangle are
  shown side-by-side in the CENTER OF THE MAP DISPLAY as a no-data placeholder.
- Online map source dropdown.
- Default map center: UGM / Bulaksumur, Yogyakarta.
- Standard Leaflet mouse drag / wheel zoom.
- Optional GNSS auto-center; default OFF.
- Multiple GeoTIFF overlays.
- GeoTIFFs are reprojected for display to EPSG:4326 using rasterio and rendered
  as transparent PNG image overlays.

Required
--------
    pip install PySide6

Qt WebEngine must be available:
    PySide6.QtWebEngineWidgets
    PySide6.QtWebEngineCore

GeoTIFF overlay support:
    pip install rasterio pillow numpy

Notes
-----
The map itself uses online tile servers and Leaflet. Internet access is needed
for base-map tiles. GeoTIFF overlays are local and can remain visible without
re-downloading after they have been loaded into the map.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.POSITION"


def configure_windows_runtime() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
        )

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
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
    WEBENGINE_ERROR = ""

except Exception as exc:
    QWebEngineSettings = None
    QWebEngineView = None

    WEBENGINE_AVAILABLE = False
    WEBENGINE_ERROR = str(exc)


# =============================================================================
# Optional GeoTIFF packages
# =============================================================================

try:
    import numpy as np
except Exception:
    np = None

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject, transform_bounds

    RASTERIO_AVAILABLE = True
    RASTERIO_ERROR = ""

except Exception as exc:
    rasterio = None
    Resampling = None
    from_bounds = None
    reproject = None
    transform_bounds = None

    RASTERIO_AVAILABLE = False
    RASTERIO_ERROR = str(exc)

try:
    from PIL import Image

    PIL_AVAILABLE = True
    PIL_ERROR = ""

except Exception as exc:
    Image = None
    PIL_AVAILABLE = False
    PIL_ERROR = str(exc)


# =============================================================================
# Shared data
# =============================================================================

from shared_data_v5 import OBSSharedData


# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "Position"

BASE_DIR = Path(__file__).resolve().parent
ICON_DIR = BASE_DIR / "assets" / "icons"

APP_ICON_ICO = ICON_DIR / "app_icon.ico"
APP_ICON_PNG = ICON_DIR / "app_icon.png"

# UGM / Bulaksumur, Yogyakarta.
DEFAULT_CENTER_LAT = -7.7708
DEFAULT_CENTER_LON = 110.3776
DEFAULT_ZOOM = 16

POSITION_REFRESH_MS = 500

# Limit a single raster display overlay to keep GUI memory and WebEngine image
# decoding practical. The GeoTIFF file itself is never modified.
GEOTIFF_MAX_DISPLAY_DIM = 4096

MAP_SOURCES = {
    "Esri Satellite": {
        "url": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        "attribution": (
            "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, "
            "and the GIS User Community"
        ),
        "maxZoom": 20,
    },
    "OpenStreetMap": {
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": (
            "&copy; OpenStreetMap contributors"
        ),
        "maxZoom": 19,
    },
    "OpenTopoMap": {
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": (
            "Map data &copy; OpenStreetMap contributors, "
            "SRTM | Map style &copy; OpenTopoMap"
        ),
        "maxZoom": 17,
    },
    "CARTO Light": {
        "url": (
            "https://{s}.basemaps.cartocdn.com/light_all/"
            "{z}/{x}/{y}{r}.png"
        ),
        "attribution": (
            "&copy; OpenStreetMap contributors &copy; CARTO"
        ),
        "maxZoom": 20,
    },
    "CARTO Dark": {
        "url": (
            "https://{s}.basemaps.cartocdn.com/dark_all/"
            "{z}/{x}/{y}{r}.png"
        ),
        "attribution": (
            "&copy; OpenStreetMap contributors &copy; CARTO"
        ),
        "maxZoom": 20,
    },
    "Esri Street": {
        "url": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
        ),
        "attribution": (
            "Tiles &copy; Esri"
        ),
        "maxZoom": 20,
    },
}

DEFAULT_MAP_SOURCE = "Esri Satellite"


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


def valid_coordinate(
    valid: bool,
    latitude: float,
    longitude: float,
) -> bool:
    if not valid:
        return False

    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
    ):
        return False

    return (
        -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )


# =============================================================================
# Leaflet HTML
# =============================================================================


MAP_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<link
  rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
  src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>
html, body, #map {
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 0;
    background: #07131D;
    overflow: hidden;
}

.leaflet-container {
    background: #07131D;
    font-family: "Segoe UI", Arial, sans-serif;
}

.leaflet-control-attribution {
    font-size: 9px;
}

.gnss-marker-wrap,
.usbl-marker-wrap {
    background: transparent;
    border: none;
}

.gnss-marker {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: #37E6FF;
    border: 3px solid #FFFFFF;
    box-sizing: border-box;
    box-shadow: 0 0 0 2px rgba(0,0,0,0.45);
}

.usbl-marker {
    width: 0;
    height: 0;
    border-left: 11px solid transparent;
    border-right: 11px solid transparent;
    border-bottom: 20px solid #FFCF4B;
    filter: drop-shadow(0 0 2px rgba(0,0,0,0.8));
}

.marker-label {
    color: #FFFFFF;
    background: rgba(6,18,27,0.86);
    border: 1px solid rgba(255,255,255,0.25);
    border-radius: 4px;
    padding: 2px 5px;
    font-size: 10px;
    white-space: nowrap;
}

/* No-position placeholder is screen-centered, independent of map pan/zoom. */
#noDataMarkers {
    position: absolute;
    z-index: 1000;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    gap: 20px;
    pointer-events: none;
}

.placeholder-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 7px;
}

.placeholder-gnss {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: #37E6FF;
    border: 4px solid #FFFFFF;
    box-sizing: border-box;
    opacity: 0.90;
    box-shadow: 0 2px 8px rgba(0,0,0,0.6);
}

.placeholder-usbl {
    width: 0;
    height: 0;
    border-left: 17px solid transparent;
    border-right: 17px solid transparent;
    border-bottom: 31px solid #FFCF4B;
    opacity: 0.92;
    filter: drop-shadow(0 2px 4px rgba(0,0,0,0.7));
}

.placeholder-text {
    color: #FFFFFF;
    background: rgba(5,18,27,0.84);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 4px;
    font-size: 10px;
    padding: 2px 5px;
}
</style>
</head>

<body>
<div id="map"></div>

<div id="noDataMarkers">
    <div class="placeholder-item">
        <div class="placeholder-gnss"></div>
        <div class="placeholder-text">GNSS</div>
    </div>
    <div class="placeholder-item">
        <div class="placeholder-usbl"></div>
        <div class="placeholder-text">USBL</div>
    </div>
</div>

<script>
const DEFAULT_LAT = __DEFAULT_LAT__;
const DEFAULT_LON = __DEFAULT_LON__;
const DEFAULT_ZOOM = __DEFAULT_ZOOM__;

const MAP_SOURCES = __MAP_SOURCES__;

let map = L.map(
    'map',
    {
        zoomControl: true,
        attributionControl: true,
        scrollWheelZoom: true,
        doubleClickZoom: true,
        dragging: true,
        boxZoom: true,
        keyboard: true
    }
).setView(
    [DEFAULT_LAT, DEFAULT_LON],
    DEFAULT_ZOOM
);

let currentBaseLayer = null;
let currentBaseName = null;

let gnssMarker = null;
let usblMarker = null;

const geoTiffLayers = {};

const gnssIcon = L.divIcon({
    className: 'gnss-marker-wrap',
    html: '<div class="gnss-marker"></div>',
    iconSize: [18, 18],
    iconAnchor: [9, 9]
});

const usblIcon = L.divIcon({
    className: 'usbl-marker-wrap',
    html: '<div class="usbl-marker"></div>',
    iconSize: [22, 20],
    iconAnchor: [11, 18]
});

function setBaseLayer(name) {
    const source = MAP_SOURCES[name];
    if (!source) {
        return;
    }

    if (currentBaseLayer) {
        map.removeLayer(currentBaseLayer);
    }

    currentBaseLayer = L.tileLayer(
        source.url,
        {
            attribution: source.attribution || '',
            maxZoom: source.maxZoom || 20,
            subdomains: source.subdomains || 'abc'
        }
    );

    currentBaseLayer.addTo(map);
    currentBaseName = name;
}

function resetToUGM() {
    map.setView(
        [DEFAULT_LAT, DEFAULT_LON],
        DEFAULT_ZOOM,
        {animate: false}
    );
}

function centerGNSS() {
    if (gnssMarker) {
        map.panTo(
            gnssMarker.getLatLng(),
            {animate: false}
        );
    }
}

function positionTooltip(
    name,
    latitude,
    longitude,
    altitude,
    fixQuality,
    satellites,
    hdop
) {
    return (
        '<div class="marker-label">' +
        '<b>' + name + '</b><br>' +
        latitude.toFixed(7) + ', ' + longitude.toFixed(7) + '<br>' +
        'Alt: ' + altitude.toFixed(2) + ' m' +
        ' &nbsp; Fix: ' + fixQuality +
        ' &nbsp; Sat: ' + satellites +
        ' &nbsp; HDOP: ' + hdop.toFixed(2) +
        '</div>'
    );
}

function updatePositions(
    gnss,
    usbl,
    autoCenterGNSS
) {
    const bothInvalid = !gnss.valid && !usbl.valid;

    document.getElementById(
        'noDataMarkers'
    ).style.display = bothInvalid ? 'flex' : 'none';

    if (gnss.valid) {
        const latlng = [
            gnss.latitude,
            gnss.longitude
        ];

        if (!gnssMarker) {
            gnssMarker = L.marker(
                latlng,
                {
                    icon: gnssIcon,
                    zIndexOffset: 1000
                }
            ).addTo(map);
        } else {
            gnssMarker.setLatLng(latlng);
        }

        gnssMarker.unbindTooltip();
        gnssMarker.bindTooltip(
            positionTooltip(
                'GNSS',
                gnss.latitude,
                gnss.longitude,
                gnss.altitude,
                gnss.fix_quality,
                gnss.satellites,
                gnss.hdop
            ),
            {
                direction: 'top',
                offset: [0, -10],
                opacity: 1.0
            }
        );

        if (autoCenterGNSS) {
            map.panTo(
                latlng,
                {animate: false}
            );
        }
    } else if (gnssMarker) {
        map.removeLayer(gnssMarker);
        gnssMarker = null;
    }

    if (usbl.valid) {
        const latlng = [
            usbl.latitude,
            usbl.longitude
        ];

        if (!usblMarker) {
            usblMarker = L.marker(
                latlng,
                {
                    icon: usblIcon,
                    zIndexOffset: 900
                }
            ).addTo(map);
        } else {
            usblMarker.setLatLng(latlng);
        }

        usblMarker.unbindTooltip();
        usblMarker.bindTooltip(
            positionTooltip(
                'USBL',
                usbl.latitude,
                usbl.longitude,
                usbl.altitude,
                usbl.fix_quality,
                usbl.satellites,
                usbl.hdop
            ),
            {
                direction: 'top',
                offset: [0, -14],
                opacity: 1.0
            }
        );
    } else if (usblMarker) {
        map.removeLayer(usblMarker);
        usblMarker = null;
    }
}

function addGeoTiffOverlay(
    overlayId,
    overlayName,
    relativeUrl,
    bounds,
    opacity
) {
    if (geoTiffLayers[overlayId]) {
        map.removeLayer(
            geoTiffLayers[overlayId]
        );
    }

    const layer = L.imageOverlay(
        relativeUrl,
        bounds,
        {
            opacity: opacity,
            interactive: false
        }
    );

    layer.addTo(map);
    geoTiffLayers[overlayId] = layer;
}

function removeGeoTiffOverlay(
    overlayId
) {
    const layer = geoTiffLayers[
        overlayId
    ];

    if (layer) {
        map.removeLayer(layer);
        delete geoTiffLayers[
            overlayId
        ];
    }
}

function clearGeoTiffOverlays() {
    for (
        const overlayId
        in geoTiffLayers
    ) {
        map.removeLayer(
            geoTiffLayers[
                overlayId
            ]
        );
    }

    for (
        const overlayId
        in geoTiffLayers
    ) {
        delete geoTiffLayers[
            overlayId
        ];
    }
}

setBaseLayer(
    '__DEFAULT_MAP_SOURCE__'
);
</script>

</body>
</html>
"""


# =============================================================================
# Main window
# =============================================================================


class PositionWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 860)
        self.setMinimumSize(1050, 650)

        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.shared: Optional[
            OBSSharedData
        ] = None

        try:
            self.shared = OBSSharedData()
        except Exception as exc:
            raise RuntimeError(
                f"Cannot attach OBS shared RAM: {exc}"
            ) from exc

        self.map_ready = False

        self.temp_dir = (
            tempfile.TemporaryDirectory(
                prefix="obs_position_map_"
            )
        )

        self.temp_path = Path(
            self.temp_dir.name
        )

        self.html_path = (
            self.temp_path
            / "position_map.html"
        )

        self.overlay_records = {}

        self.last_gnss_timestamp_ns = -1
        self.last_usbl_timestamp_ns = -1

        self._build_ui()
        self._apply_style()

        if WEBENGINE_AVAILABLE:
            self._initialize_map()
        else:
            self.map_placeholder.setText(
                "Map display unavailable.\n\n"
                "PySide6 Qt WebEngine could not be loaded.\n\n"
                f"{WEBENGINE_ERROR}"
            )

        self.position_timer = QTimer(
            self
        )
        self.position_timer.timeout.connect(
            self.refresh_positions
        )
        self.position_timer.start(
            POSITION_REFRESH_MS
        )

        self.refresh_positions()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        central = QWidget()
        central.setObjectName(
            "centralWidget"
        )
        self.setCentralWidget(
            central
        )

        root = QHBoxLayout(
            central
        )
        root.setContentsMargins(
            8, 8, 8, 8
        )
        root.setSpacing(0)

        splitter = QSplitter(
            Qt.Horizontal
        )
        splitter.setChildrenCollapsible(
            False
        )

        # ==============================================================
        # LEFT 1/5 — SETTINGS
        # ==============================================================
        settings_panel = QFrame()
        settings_panel.setObjectName(
            "settingsPanel"
        )
        settings_panel.setMinimumWidth(
            230
        )

        settings = QVBoxLayout(
            settings_panel
        )
        settings.setContentsMargins(
            7, 5, 9, 5
        )
        settings.setSpacing(8)

        # Base map.
        map_group = QGroupBox(
            "Online Map"
        )
        map_group.setObjectName(
            "controlGroup"
        )

        mg = QVBoxLayout(
            map_group
        )
        mg.setContentsMargins(
            9, 14, 9, 9
        )
        mg.setSpacing(6)

        self.map_source_combo = (
            QComboBox()
        )

        self.map_source_combo.addItems(
            list(
                MAP_SOURCES.keys()
            )
        )

        self.map_source_combo.setCurrentText(
            DEFAULT_MAP_SOURCE
        )

        self.map_source_combo.currentTextChanged.connect(
            self.on_map_source_changed
        )

        reset_ugm = QPushButton(
            "Center UGM"
        )
        reset_ugm.setObjectName(
            "secondaryButton"
        )
        reset_ugm.clicked.connect(
            self.center_ugm
        )

        self.center_gnss_button = QPushButton(
            "Center GNSS"
        )
        self.center_gnss_button.setObjectName(
            "secondaryButton"
        )
        self.center_gnss_button.clicked.connect(
            self.center_gnss
        )

        self.auto_center_gnss = QCheckBox(
            "Auto Center GNSS"
        )
        self.auto_center_gnss.setChecked(
            False
        )

        mg.addWidget(
            self.map_source_combo
        )
        mg.addWidget(
            reset_ugm
        )
        mg.addWidget(
            self.center_gnss_button
        )
        mg.addWidget(
            self.auto_center_gnss
        )

        settings.addWidget(
            map_group
        )

        # GNSS.
        gnss_group = QGroupBox(
            "GNSS"
        )
        gnss_group.setObjectName(
            "controlGroup"
        )

        gg = QVBoxLayout(
            gnss_group
        )
        gg.setContentsMargins(
            9, 14, 9, 9
        )

        self.gnss_status = QLabel(
            "No valid position"
        )
        self.gnss_status.setObjectName(
            "positionStatus"
        )
        self.gnss_status.setWordWrap(
            True
        )

        gg.addWidget(
            self.gnss_status
        )

        settings.addWidget(
            gnss_group
        )

        # USBL.
        usbl_group = QGroupBox(
            "USBL"
        )
        usbl_group.setObjectName(
            "controlGroup"
        )

        ug = QVBoxLayout(
            usbl_group
        )
        ug.setContentsMargins(
            9, 14, 9, 9
        )

        self.usbl_status = QLabel(
            "No valid position"
        )
        self.usbl_status.setObjectName(
            "positionStatus"
        )
        self.usbl_status.setWordWrap(
            True
        )

        ug.addWidget(
            self.usbl_status
        )

        settings.addWidget(
            usbl_group
        )

        # GeoTIFF overlays.
        geotiff_group = QGroupBox(
            "GeoTIFF Overlay"
        )
        geotiff_group.setObjectName(
            "controlGroup"
        )

        tg = QVBoxLayout(
            geotiff_group
        )
        tg.setContentsMargins(
            9, 14, 9, 9
        )
        tg.setSpacing(5)

        self.load_geotiff_button = (
            QPushButton(
                "Load GeoTIFF(s)"
            )
        )
        self.load_geotiff_button.setObjectName(
            "primaryButton"
        )
        self.load_geotiff_button.clicked.connect(
            self.load_geotiffs
        )

        self.overlay_list = QListWidget()
        self.overlay_list.setSelectionMode(
            QAbstractItemView.ExtendedSelection
        )
        self.overlay_list.setMinimumHeight(
            100
        )

        remove_selected = QPushButton(
            "Remove Selected"
        )
        remove_selected.clicked.connect(
            self.remove_selected_overlays
        )

        clear_all = QPushButton(
            "Clear All"
        )
        clear_all.clicked.connect(
            self.clear_overlays
        )

        tg.addWidget(
            self.load_geotiff_button
        )
        tg.addWidget(
            self.overlay_list,
            1,
        )
        tg.addWidget(
            remove_selected
        )
        tg.addWidget(
            clear_all
        )

        self.geotiff_status = QLabel(
            "Multiple GeoTIFF files supported"
        )
        self.geotiff_status.setObjectName(
            "hintText"
        )
        self.geotiff_status.setWordWrap(
            True
        )

        tg.addWidget(
            self.geotiff_status
        )

        settings.addWidget(
            geotiff_group,
            1,
        )

        settings.addStretch(
            1
        )

        splitter.addWidget(
            settings_panel
        )

        # ==============================================================
        # RIGHT 4/5 — MAP
        # ==============================================================
        map_frame = QFrame()
        map_frame.setObjectName(
            "mapFrame"
        )

        map_layout = QVBoxLayout(
            map_frame
        )
        map_layout.setContentsMargins(
            0, 0, 0, 0
        )
        map_layout.setSpacing(0)

        self.map_view = None

        self.map_placeholder = QLabel(
            "Loading map..."
        )
        self.map_placeholder.setObjectName(
            "mapPlaceholder"
        )
        self.map_placeholder.setAlignment(
            Qt.AlignCenter
        )

        if WEBENGINE_AVAILABLE:
            self.map_view = (
                QWebEngineView()
            )
            self.map_view.setObjectName(
                "mapView"
            )
            map_layout.addWidget(
                self.map_view,
                1,
            )

            self.map_placeholder.hide()
        else:
            map_layout.addWidget(
                self.map_placeholder,
                1,
            )

        splitter.addWidget(
            map_frame
        )

        # Approx. 1/5 : 4/5.
        splitter.setStretchFactor(
            0,
            1,
        )
        splitter.setStretchFactor(
            1,
            4,
        )
        splitter.setSizes(
            [285, 1140]
        )

        root.addWidget(
            splitter,
            1,
        )

    # ------------------------------------------------------------------ map setup

    def _initialize_map(self):
        settings = (
            self.map_view.settings()
        )

        try:
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
                True,
            )
        except Exception:
            pass

        try:
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
                True,
            )
        except Exception:
            pass

        try:
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptEnabled,
                True,
            )
        except Exception:
            pass

        html = MAP_HTML

        html = html.replace(
            "__DEFAULT_LAT__",
            repr(
                DEFAULT_CENTER_LAT
            ),
        )
        html = html.replace(
            "__DEFAULT_LON__",
            repr(
                DEFAULT_CENTER_LON
            ),
        )
        html = html.replace(
            "__DEFAULT_ZOOM__",
            str(
                DEFAULT_ZOOM
            ),
        )
        html = html.replace(
            "__MAP_SOURCES__",
            json.dumps(
                MAP_SOURCES
            ),
        )
        html = html.replace(
            "__DEFAULT_MAP_SOURCE__",
            DEFAULT_MAP_SOURCE.replace(
                "'",
                "\\'",
            ),
        )

        self.html_path.write_text(
            html,
            encoding="utf-8",
        )

        self.map_view.loadFinished.connect(
            self.on_map_loaded
        )

        self.map_view.load(
            QUrl.fromLocalFile(
                str(
                    self.html_path
                )
            )
        )

    def on_map_loaded(
        self,
        ok: bool,
    ):
        self.map_ready = bool(
            ok
        )

        if not ok:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "The map page could not be loaded. "
                "Check Qt WebEngine and internet access.",
            )
            return

        self.on_map_source_changed(
            self.map_source_combo.currentText()
        )

        self.refresh_positions()

    def run_js(
        self,
        script: str,
    ):
        if (
            self.map_ready
            and self.map_view is not None
        ):
            try:
                self.map_view.page().runJavaScript(
                    script
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ online map

    def on_map_source_changed(
        self,
        source_name: str,
    ):
        if source_name not in MAP_SOURCES:
            return

        self.run_js(
            f"setBaseLayer({json.dumps(source_name)});"
        )

    def center_ugm(self):
        self.run_js(
            "resetToUGM();"
        )

    def center_gnss(self):
        gnss = self.shared.read_gnss()

        if not valid_coordinate(
            gnss.valid,
            gnss.latitude,
            gnss.longitude,
        ):
            QMessageBox.information(
                self,
                APP_TITLE,
                "GNSS position is not valid yet.",
            )
            return

        self.run_js(
            "map.panTo("
            f"[{gnss.latitude:.10f}, {gnss.longitude:.10f}], "
            "{animate:false});"
        )

    # ------------------------------------------------------------------ shared positions

    @staticmethod
    def _position_payload(
        position,
    ):
        valid = valid_coordinate(
            bool(
                position.valid
            ),
            float(
                position.latitude
            ),
            float(
                position.longitude
            ),
        )

        return {
            "valid": valid,
            "latitude": float(
                position.latitude
            )
            if valid
            else 0.0,
            "longitude": float(
                position.longitude
            )
            if valid
            else 0.0,
            "altitude": float(
                position.altitude
            ),
            "fix_quality": int(
                position.fix_quality
            ),
            "satellites": int(
                position.satellites
            ),
            "hdop": float(
                position.hdop
            ),
        }

    @staticmethod
    def _status_text(
        position,
    ):
        valid = valid_coordinate(
            position.valid,
            position.latitude,
            position.longitude,
        )

        if not valid:
            return "No valid position"

        age_ms = max(
            0.0,
            (
                time.time_ns()
                - int(
                    position.timestamp_ns
                )
            )
            / 1_000_000.0,
        )

        return (
            f"Lat : {position.latitude:.7f}\n"
            f"Lon : {position.longitude:.7f}\n"
            f"Alt : {position.altitude:.2f} m\n"
            f"Fix : {position.fix_quality}   "
            f"Sat : {position.satellites}\n"
            f"HDOP: {position.hdop:.2f}   "
            f"Age: {age_ms:.0f} ms"
        )

    def refresh_positions(self):
        if self.shared is None:
            return

        try:
            gnss = self.shared.read_gnss()
            usbl = self.shared.read_usbl()

            self.gnss_status.setText(
                self._status_text(
                    gnss
                )
            )
            self.usbl_status.setText(
                self._status_text(
                    usbl
                )
            )

            gnss_valid = valid_coordinate(
                gnss.valid,
                gnss.latitude,
                gnss.longitude,
            )

            self.center_gnss_button.setEnabled(
                gnss_valid
            )

            if not self.map_ready:
                return

            gnss_payload = (
                self._position_payload(
                    gnss
                )
            )
            usbl_payload = (
                self._position_payload(
                    usbl
                )
            )

            script = (
                "updatePositions("
                f"{json.dumps(gnss_payload)}, "
                f"{json.dumps(usbl_payload)}, "
                f"{str(self.auto_center_gnss.isChecked()).lower()}"
                ");"
            )

            self.run_js(
                script
            )

        except Exception as exc:
            self.gnss_status.setText(
                f"Shared RAM error: {exc}"
            )
            self.usbl_status.setText(
                f"Shared RAM error: {exc}"
            )

    # ------------------------------------------------------------------ GeoTIFF

    def load_geotiffs(self):
        files, _ = (
            QFileDialog.getOpenFileNames(
                self,
                "Load GeoTIFF Overlay(s)",
                "",
                (
                    "GeoTIFF (*.tif *.tiff *.geotiff);;"
                    "TIFF (*.tif *.tiff);;"
                    "All Files (*.*)"
                ),
            )
        )

        if not files:
            return

        if not (
            RASTERIO_AVAILABLE
            and PIL_AVAILABLE
            and np is not None
        ):
            missing = []

            if not RASTERIO_AVAILABLE:
                missing.append(
                    "rasterio"
                )

            if not PIL_AVAILABLE:
                missing.append(
                    "Pillow"
                )

            if np is None:
                missing.append(
                    "numpy"
                )

            QMessageBox.warning(
                self,
                APP_TITLE,
                "GeoTIFF overlay requires:\n\n"
                + ", ".join(
                    missing
                )
                + "\n\nInstall for example:\n"
                "pip install rasterio pillow numpy",
            )
            return

        loaded = 0
        errors = []

        for filename in files:
            try:
                record = (
                    self._prepare_geotiff_overlay(
                        Path(
                            filename
                        )
                    )
                )

                overlay_id = record[
                    "overlay_id"
                ]

                self.overlay_records[
                    overlay_id
                ] = record

                item = QListWidgetItem(
                    record[
                        "display_name"
                    ]
                )
                item.setData(
                    Qt.UserRole,
                    overlay_id,
                )

                self.overlay_list.addItem(
                    item
                )

                self._add_overlay_to_map(
                    record
                )

                loaded += 1

            except Exception as exc:
                errors.append(
                    f"{Path(filename).name}: {exc}"
                )

        if errors:
            self.geotiff_status.setText(
                f"Loaded {loaded}. "
                f"Failed {len(errors)}."
            )

            QMessageBox.warning(
                self,
                APP_TITLE,
                "Some GeoTIFF files could not be loaded:\n\n"
                + "\n".join(
                    errors[:10]
                ),
            )
        else:
            self.geotiff_status.setText(
                f"{loaded} GeoTIFF overlay(s) loaded"
            )

    @staticmethod
    def _scale_band_to_uint8(
        array,
        mask,
    ):
        array = np.asarray(
            array
        )

        valid = (
            (mask > 0)
            & np.isfinite(
                array
            )
        )

        result = np.zeros(
            array.shape,
            dtype=np.uint8,
        )

        if not np.any(
            valid
        ):
            return result

        values = array[
            valid
        ].astype(
            np.float64,
            copy=False,
        )

        if (
            array.dtype
            == np.uint8
        ):
            result[
                valid
            ] = array[
                valid
            ]
            return result

        low = float(
            np.percentile(
                values,
                2.0,
            )
        )
        high = float(
            np.percentile(
                values,
                98.0,
            )
        )

        if high <= low:
            low = float(
                np.min(
                    values
                )
            )
            high = float(
                np.max(
                    values
                )
            )

        if high <= low:
            result[
                valid
            ] = 128
            return result

        scaled = (
            (
                array.astype(
                    np.float64,
                    copy=False,
                )
                - low
            )
            / (
                high - low
            )
            * 255.0
        )

        result[
            valid
        ] = np.clip(
            scaled[
                valid
            ],
            0.0,
            255.0,
        ).astype(
            np.uint8
        )

        return result

    def _prepare_geotiff_overlay(
        self,
        filename: Path,
    ):
        overlay_id = (
            "gt_"
            + uuid.uuid4().hex
        )

        png_name = (
            overlay_id
            + ".png"
        )

        png_path = (
            self.temp_path
            / png_name
        )

        with rasterio.open(
            filename
        ) as src:

            if src.crs is None:
                raise ValueError(
                    "GeoTIFF has no CRS"
                )

            west, south, east, north = (
                transform_bounds(
                    src.crs,
                    "EPSG:4326",
                    *src.bounds,
                    densify_pts=21,
                )
            )

            if not all(
                math.isfinite(
                    value
                )
                for value in (
                    west,
                    south,
                    east,
                    north,
                )
            ):
                raise ValueError(
                    "Invalid geographic bounds"
                )

            if (
                east <= west
                or north <= south
            ):
                raise ValueError(
                    "Invalid GeoTIFF extent"
                )

            scale = max(
                1.0,
                max(
                    src.width,
                    src.height,
                )
                / float(
                    GEOTIFF_MAX_DISPLAY_DIM
                ),
            )

            dst_width = max(
                1,
                int(
                    round(
                        src.width
                        / scale
                    )
                ),
            )
            dst_height = max(
                1,
                int(
                    round(
                        src.height
                        / scale
                    )
                ),
            )

            dst_transform = (
                from_bounds(
                    west,
                    south,
                    east,
                    north,
                    dst_width,
                    dst_height,
                )
            )

            src_mask = (
                src.dataset_mask()
            )

            dst_mask = np.zeros(
                (
                    dst_height,
                    dst_width,
                ),
                dtype=np.uint8,
            )

            reproject(
                source=src_mask,
                destination=dst_mask,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs="EPSG:4326",
                resampling=(
                    Resampling.nearest
                ),
            )

            if src.count >= 3:
                band_indices = (
                    1,
                    2,
                    3,
                )
            else:
                band_indices = (
                    1,
                    1,
                    1,
                )

            rgb = []

            for band_index in band_indices:
                destination = np.zeros(
                    (
                        dst_height,
                        dst_width,
                    ),
                    dtype=np.float32,
                )

                reproject(
                    source=rasterio.band(
                        src,
                        band_index,
                    ),
                    destination=destination,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=(
                        Resampling.bilinear
                    ),
                )

                rgb.append(
                    self._scale_band_to_uint8(
                        destination,
                        dst_mask,
                    )
                )

            rgba = np.zeros(
                (
                    dst_height,
                    dst_width,
                    4,
                ),
                dtype=np.uint8,
            )

            rgba[
                :,
                :,
                0,
            ] = rgb[
                0
            ]
            rgba[
                :,
                :,
                1,
            ] = rgb[
                1
            ]
            rgba[
                :,
                :,
                2,
            ] = rgb[
                2
            ]

            rgba[
                :,
                :,
                3,
            ] = dst_mask

            Image.fromarray(
                rgba,
                mode="RGBA",
            ).save(
                png_path,
                format="PNG",
                optimize=False,
            )

        return {
            "overlay_id": overlay_id,
            "display_name": filename.name,
            "source_path": str(
                filename
            ),
            "png_name": png_name,
            "bounds": [
                [
                    float(
                        south
                    ),
                    float(
                        west
                    ),
                ],
                [
                    float(
                        north
                    ),
                    float(
                        east
                    ),
                ],
            ],
            "opacity": 0.78,
        }

    def _add_overlay_to_map(
        self,
        record,
    ):
        if not self.map_ready:
            return

        script = (
            "addGeoTiffOverlay("
            f"{json.dumps(record['overlay_id'])}, "
            f"{json.dumps(record['display_name'])}, "
            f"{json.dumps(record['png_name'])}, "
            f"{json.dumps(record['bounds'])}, "
            f"{float(record['opacity'])}"
            ");"
        )

        self.run_js(
            script
        )

    def remove_selected_overlays(
        self,
    ):
        selected = (
            self.overlay_list.selectedItems()
        )

        if not selected:
            return

        for item in selected:
            overlay_id = item.data(
                Qt.UserRole
            )

            self.run_js(
                "removeGeoTiffOverlay("
                f"{json.dumps(overlay_id)}"
                ");"
            )

            self.overlay_records.pop(
                overlay_id,
                None,
            )

            row = self.overlay_list.row(
                item
            )
            self.overlay_list.takeItem(
                row
            )

        self.geotiff_status.setText(
            f"{self.overlay_list.count()} overlay(s) loaded"
        )

    def clear_overlays(
        self,
    ):
        self.run_js(
            "clearGeoTiffOverlays();"
        )

        self.overlay_records.clear()
        self.overlay_list.clear()

        self.geotiff_status.setText(
            "No GeoTIFF overlays"
        )

    # ------------------------------------------------------------------ styling

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow,
            QWidget#centralWidget {
                background-color: #07131D;
                color: #FFFFFF;
                font-family: "Segoe UI", "Arial";
            }

            QFrame#settingsPanel {
                background-color: #07131D;
                border-right: 1px solid #17374A;
            }

            QFrame#mapFrame {
                background-color: #07131D;
                border: none;
            }

            QLabel#mapPlaceholder {
                background-color: #07131D;
                color: #7894A4;
                font-size: 14px;
            }

            QGroupBox#controlGroup {
                background-color: #0D1E2A;
                border: 1px solid #1A3D52;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 6px;
                color: #FFFFFF;
                font-weight: 800;
            }

            QGroupBox#controlGroup::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0px 5px;
                color: #FFFFFF;
            }

            QLabel {
                background: transparent;
                color: #FFFFFF;
            }

            QLabel#positionStatus {
                color: #DCE8EE;
                font-family: "Consolas";
                font-size: 10px;
            }

            QLabel#hintText {
                color: #7894A4;
                font-size: 9px;
            }

            QComboBox {
                background-color: #071620;
                color: #FFFFFF;
                border: 1px solid #24485D;
                border-radius: 5px;
                min-height: 27px;
                padding: 2px 6px;
            }

            QComboBox QAbstractItemView {
                background-color: #0B1B26;
                color: #F4FAFD;
                border: 1px solid #2B526A;
                selection-background-color: #245B79;
                selection-color: #FFFFFF;
                outline: none;
            }

            QCheckBox {
                color: #DDE9EF;
                spacing: 6px;
            }

            QPushButton {
                min-height: 28px;
                border-radius: 6px;
                padding: 3px 7px;
                font-weight: 700;
                background-color: #162D3A;
                color: #DDEAF2;
                border: 1px solid #2A4E62;
            }

            QPushButton:hover {
                background-color: #1C3A4A;
                border-color: #39708B;
            }

            QPushButton#primaryButton {
                background-color: #17678F;
                color: #FFFFFF;
                border: 1px solid #2D8AB6;
            }

            QPushButton#secondaryButton {
                background-color: #123147;
                border: 1px solid #285B78;
            }

            QListWidget {
                background-color: #071620;
                color: #E4EEF3;
                border: 1px solid #24485D;
                border-radius: 5px;
                outline: none;
            }

            QListWidget::item {
                padding: 4px;
            }

            QListWidget::item:selected {
                background-color: #245B79;
                color: #FFFFFF;
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
    ):
        try:
            self.position_timer.stop()
        except Exception:
            pass

        if self.shared is not None:
            try:
                self.shared.close()
            except Exception:
                pass

        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

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
    font.setPointSize(
        9
    )
    app.setFont(
        font
    )

    try:
        window = (
            PositionWindow()
        )

    except Exception as exc:
        QMessageBox.critical(
            None,
            APP_TITLE,
            f"Cannot start Position module:\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
