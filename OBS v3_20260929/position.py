"""
position.py
===========

GRC-UGM-PERTAMINA OBS
GNSS / USBL Position Map

Version: 11
Shared data: shared_data.py

Layout
------
- Left  1/5 : settings / source / position status / GeoTIFF overlays
- Right 4/5 : interactive online map

Features
--------
- Read GNSS and USBL positions from shared_data RAM.
- Show the latest raw NMEA sentence received by GNSS and USBL, including
  receive age/counter, so transport reception can be checked independently of GGA parsing.
- Apply a 3-second GNSS/USBL freshness timeout. When reception/GGA becomes
  stale, retain the last valid coordinates but force the effective fix to 0.
- Keep stale last-valid markers visible at reduced opacity and label them
  STALE / LAST VALID; live reception restores the marker automatically.
- GNSS position is written by OBS Setting from the configured GNSS COM/UDP input.
- USBL/OBS position is written by OBS Setting from the configured USBL COM/UDP
  input.
- Poll the latest shared position at 10 Hz and move each Leaflet marker whenever
  a new source timestamp/coordinate arrives.
- GNSS marker = circle.
- USBL / OBS marker = triangle.
- When BOTH GNSS and USBL have no valid position, a circle and triangle are
  shown side-by-side in the CENTER OF THE MAP DISPLAY as a no-data placeholder.
- Online map source dropdown.
- Default map center: UGM / Bulaksumur, Yogyakarta.
- Standard Leaflet mouse drag / wheel zoom.
- Optional GNSS auto-center; default OFF.
- Position panels are vertically scroll-safe and reserve enough height for all
  GNSS/USBL status lines and complete raw NMEA diagnostics.
- Raw NMEA sentences are wrapped at comma boundaries for display only; the exact
  original sentence remains available as the label tooltip.
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
import requests
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# =============================================================================
# Windows runtime
# =============================================================================

APP_USER_MODEL_ID = "GRC.UGM.PERTAMINA.OBS.POSITION"


def configure_windows_runtime() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        setter = getattr(
            ctypes.windll.shell32,
            "SetCurrentProcessExplicitAppUserModelID",
            None,
        )
        if setter is not None:
            setter(APP_USER_MODEL_ID)

        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
        )

        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            kernel32.FreeConsole()

    except (AttributeError, OSError):
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
    QScrollArea,
    QSizePolicy,
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

from shared_data import OBSSharedData


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

# Poll faster than typical GNSS/USBL update rates so every fresh source update
# is reflected promptly on the map. The actual source timestamps remain
# authoritative; this timer does not synthesize positions.
POSITION_REFRESH_MS = 100

# Operational freshness timeout shared with OBS Setting v26 and MiniSEED v24.
# A source may remain UDP-LISTENING/open while the producer has stopped sending;
# freshness, not socket state, therefore determines the effective navigation fix.
POSITION_SOURCE_STALE_S = 3.0

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

LEAFLET_JS_URLS = (
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
)

LEAFLET_CSS_URLS = (
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
)


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
  href="leaflet.css"
/>

<script
  src="leaflet.js">
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
    hdop,
    state
) {
    return (
        '<div class="marker-label">' +
        '<b>' + name + '</b> &nbsp; ' + state + '<br>' +
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
    const bothUnavailable = !gnss.available && !usbl.available;

    document.getElementById(
        'noDataMarkers'
    ).style.display = bothUnavailable ? 'flex' : 'none';

    if (gnss.available) {
        const latlng = L.latLng(
            Number(gnss.latitude),
            Number(gnss.longitude)
        );

        if (!gnssMarker) {
            gnssMarker = L.marker(
                latlng,
                {
                    icon: gnssIcon,
                    zIndexOffset: 1000,
                    keyboard: false
                }
            ).addTo(map);
        } else {
            gnssMarker.setLatLng(latlng);
        }

        gnssMarker.setOpacity(gnss.live ? 1.0 : 0.45);
        gnssMarker.options.obsTimestampNs = String(gnss.timestamp_ns || 0);
        gnssMarker.unbindTooltip();
        gnssMarker.bindTooltip(
            positionTooltip(
                'GNSS',
                Number(gnss.latitude),
                Number(gnss.longitude),
                Number(gnss.altitude),
                Number(gnss.fix_quality),
                Number(gnss.satellites),
                Number(gnss.hdop),
                String(gnss.state || '')
            ),
            {
                direction: 'top',
                offset: [0, -10],
                opacity: 1.0
            }
        );

        if (autoCenterGNSS && gnss.live) {
            map.panTo(latlng, {animate:false});
        }
    } else if (gnssMarker) {
        map.removeLayer(gnssMarker);
        gnssMarker = null;
    }

    if (usbl.available) {
        const latlng = L.latLng(
            Number(usbl.latitude),
            Number(usbl.longitude)
        );

        if (!usblMarker) {
            usblMarker = L.marker(
                latlng,
                {
                    icon: usblIcon,
                    zIndexOffset: 900,
                    keyboard: false
                }
            ).addTo(map);
        } else {
            usblMarker.setLatLng(latlng);
        }

        usblMarker.setOpacity(usbl.live ? 1.0 : 0.45);
        usblMarker.options.obsTimestampNs = String(usbl.timestamp_ns || 0);
        usblMarker.unbindTooltip();
        usblMarker.bindTooltip(
            positionTooltip(
                'USBL / OBS',
                Number(usbl.latitude),
                Number(usbl.longitude),
                Number(usbl.altitude),
                Number(usbl.fix_quality),
                Number(usbl.satellites),
                Number(usbl.hdop),
                String(usbl.state || '')
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

    window.requestAnimationFrame(
        function() {
            map.invalidateSize(
                {
                    pan: false,
                    animate: false
                }
            );
        }
    );
}

const geoTiffLoadState = {};

function addGeoTiffOverlay(
    overlayId,
    overlayName,
    imageUrl,
    bounds,
    opacity
) {
    try {
        if (typeof L === 'undefined') {
            return JSON.stringify({
                ok: false,
                stage: 'leaflet',
                error: 'Leaflet object L is not available'
            });
        }

        if (typeof map === 'undefined' || !map) {
            return JSON.stringify({
                ok: false,
                stage: 'map',
                error: 'Leaflet map object is not available'
            });
        }

        if (geoTiffLayers[overlayId]) {
            map.removeLayer(geoTiffLayers[overlayId]);
        }

        geoTiffLoadState[overlayId] = {
            state: 'creating',
            url: imageUrl
        };

        const layer = L.imageOverlay(
            imageUrl,
            bounds,
            {
                opacity: opacity,
                interactive: false,
                pane: 'overlayPane'
            }
        );

        layer.on('load', function() {
            geoTiffLoadState[overlayId] = {
                state: 'loaded',
                url: imageUrl
            };

            if (layer.bringToFront) {
                layer.bringToFront();
            }
        });

        layer.on('error', function() {
            geoTiffLoadState[overlayId] = {
                state: 'error',
                url: imageUrl
            };
        });

        layer.addTo(map);

        if (layer.setZIndex) {
            layer.setZIndex(1000);
        }

        if (layer.bringToFront) {
            layer.bringToFront();
        }

        geoTiffLayers[overlayId] = layer;

        map.fitBounds(
            bounds,
            {
                padding: [24, 24],
                animate: false
            }
        );

        map.invalidateSize({
            pan: false,
            animate: false
        });

        return JSON.stringify({
            ok: true,
            stage: 'created',
            overlayId: overlayId,
            layerCount: Object.keys(geoTiffLayers).length,
            zoom: map.getZoom()
        });

    } catch (err) {
        return JSON.stringify({
            ok: false,
            stage: 'exception',
            error: String(
                err && err.stack
                    ? err.stack
                    : err
            )
        });
    }
}

function getGeoTiffLoadState(overlayId) {
    try {
        const state = geoTiffLoadState[overlayId] || {
            state: 'unknown'
        };

        return JSON.stringify({
            ok: true,
            state: state.state || 'unknown',
            url: state.url || ''
        });
    } catch (err) {
        return JSON.stringify({
            ok: false,
            error: String(err)
        });
    }
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



class _QuietLocalMapHandler(SimpleHTTPRequestHandler):
    """Serve the temporary Leaflet page/overlays without console request logging."""

    def log_message(self, format, *args):
        pass



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

        self.shared: OBSSharedData | None = None

        try:
            self.shared = OBSSharedData()
        except (BufferError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot attach shared_data RAM: {exc}"
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

        # V8: serve the generated HTML and GeoTIFF-derived PNG overlays over a
        # private localhost HTTP server.  Qt WebEngine/Chromium can silently
        # refuse file:/// image resources in some runtime/build combinations,
        # even when LocalContentCanAccessFileUrls is enabled.  HTTP on
        # 127.0.0.1 avoids that file-scheme restriction entirely.
        self.local_http_server = None
        self.local_http_thread = None
        self.local_http_base = ""

        # V11: Qt WebEngine on some Windows installations fails to load
        # Leaflet directly from a public CDN.  Download Leaflet with Python
        # requests first, then serve it locally beside position_map.html.
        self._prepare_leaflet_assets()
        self._start_local_map_server()

        self.overlay_records = {}

        self.last_gnss_timestamp_ns = -1
        self.last_usbl_timestamp_ns = -1

        self.last_gnss_nmea_timestamp_ns = -1
        self.last_usbl_nmea_timestamp_ns = -1

        # Last values actually sent to the Leaflet page. These are reset when
        # the map page reloads so the newest positions are always replayed.
        self.last_map_gnss_key = None
        self.last_map_usbl_key = None

        self.gnss_update_count = 0
        self.usbl_update_count = 0
        self.gnss_nmea_count = 0
        self.usbl_nmea_count = 0

        # Preserve the most recent coordinate that was valid at least once.
        # Stale/invalid sources use this only as LAST VALID operator context.
        self.last_valid_gnss = None
        self.last_valid_usbl = None

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
            Qt.Orientation.Horizontal
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
            270
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
        gnss_group.setMinimumHeight(
            220
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
        self.gnss_status.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )
        self.gnss_status.setMinimumHeight(
            118
        )

        gg.addWidget(
            self.gnss_status
        )

        self.gnss_raw_nmea = QLabel(
            "RX NMEA: waiting for sentence"
        )
        self.gnss_raw_nmea.setObjectName(
            "rawNmeaLabel"
        )
        self.gnss_raw_nmea.setWordWrap(
            True
        )
        self.gnss_raw_nmea.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.gnss_raw_nmea.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )
        self.gnss_raw_nmea.setMinimumHeight(
            62
        )

        gg.addWidget(
            self.gnss_raw_nmea
        )

        settings.addWidget(
            gnss_group
        )

        # USBL.
        usbl_group = QGroupBox(
            "USBL (OBS)"
        )
        usbl_group.setObjectName(
            "controlGroup"
        )
        usbl_group.setMinimumHeight(
            220
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
        self.usbl_status.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )
        self.usbl_status.setMinimumHeight(
            118
        )

        ug.addWidget(
            self.usbl_status
        )

        self.usbl_raw_nmea = QLabel(
            "RX NMEA: waiting for sentence"
        )
        self.usbl_raw_nmea.setObjectName(
            "rawNmeaLabel"
        )
        self.usbl_raw_nmea.setWordWrap(
            True
        )
        self.usbl_raw_nmea.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.usbl_raw_nmea.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )
        self.usbl_raw_nmea.setMinimumHeight(
            62
        )

        ug.addWidget(
            self.usbl_raw_nmea
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
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.overlay_list.setMinimumHeight(
            80
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

        settings_scroll = QScrollArea()
        settings_scroll.setObjectName(
            "settingsScroll"
        )
        settings_scroll.setWidgetResizable(
            True
        )
        settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        settings_scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )
        settings_scroll.setMinimumWidth(
            285
        )
        settings_scroll.setWidget(
            settings_panel
        )

        splitter.addWidget(
            settings_scroll
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
            Qt.AlignmentFlag.AlignCenter
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
            [320, 1105]
        )

        root.addWidget(
            splitter,
            1,
        )

    # ------------------------------------------------------------------ Leaflet runtime

    @staticmethod
    def _download_first_available(
        urls,
        destination: Path,
        min_size: int,
    ):
        last_error = None

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120 Safari/537.36"
            )
        })

        for url in urls:
            try:
                response = session.get(
                    url,
                    timeout=12,
                )
                response.raise_for_status()

                data = response.content
                if len(data) < min_size:
                    raise RuntimeError(
                        f"downloaded file too small ({len(data)} bytes)"
                    )

                destination.write_bytes(data)
                return url

            except Exception as exc:
                last_error = exc

        raise RuntimeError(
            f"all download sources failed: {last_error}"
        )

    def _prepare_leaflet_assets(self):
        js_path = self.temp_path / "leaflet.js"
        css_path = self.temp_path / "leaflet.css"

        try:
            js_source = self._download_first_available(
                LEAFLET_JS_URLS,
                js_path,
                100_000,
            )

            css_source = self._download_first_available(
                LEAFLET_CSS_URLS,
                css_path,
                5_000,
            )

            self.leaflet_runtime_status = (
                "Leaflet local runtime ready"
            )
            self.leaflet_js_source = js_source
            self.leaflet_css_source = css_source

        except Exception as exc:
            raise RuntimeError(
                "Cannot prepare local Leaflet runtime.\n\n"
                "Python could not download leaflet.js / leaflet.css "
                "from the configured CDN sources.\n\n"
                f"Detail: {exc}"
            ) from exc

    # ------------------------------------------------------------------ local map server

    def _start_local_map_server(self):
        handler = partial(
            _QuietLocalMapHandler,
            directory=str(self.temp_path),
        )

        self.local_http_server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            handler,
        )
        self.local_http_server.daemon_threads = True

        host, port = self.local_http_server.server_address
        self.local_http_base = f"http://127.0.0.1:{int(port)}"

        self.local_http_thread = threading.Thread(
            target=self.local_http_server.serve_forever,
            name="OBS-Position-LocalMapHTTP",
            daemon=True,
        )
        self.local_http_thread.start()

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

        # V8: load from localhost HTTP so the page and generated raster overlays
        # share the same ordinary HTTP origin.
        self.map_view.load(
            QUrl(
                f"{self.local_http_base}/{self.html_path.name}"
            )
        )

    def on_map_loaded(
        self,
        ok: bool,
    ):
        self.map_ready = bool(
            ok
        )

        # A newly loaded/reloaded page has no Leaflet markers yet even if the
        # Python-side coordinates have not changed. Force one replay.
        self.last_map_gnss_key = None
        self.last_map_usbl_key = None

        if not ok:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "The map page could not be loaded. "
                "Leaflet is served locally in V11; check Qt WebEngine "
                "and the local map runtime.",
            )
            return

        def _leaflet_ready_result(raw):
            if raw is not True:
                self.geotiff_status.setText(
                    "Leaflet local runtime failed to initialize"
                )
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "Leaflet local runtime did not initialize.\n\n"
                    "The library files were downloaded, but the browser "
                    "did not create the Leaflet object L."
                )
            else:
                self.geotiff_status.setText(
                    "Leaflet local runtime ready"
                )

        self.map_view.page().runJavaScript(
            "(typeof L !== 'undefined' && "
            "typeof map !== 'undefined' && !!map)",
            _leaflet_ready_result,
        )

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
        payload = self.last_valid_gnss
        if payload is None:
            QMessageBox.information(
                self,
                APP_TITLE,
                "GNSS has no valid position yet.",
            )
            return

        self.run_js(
            "map.panTo("
            f"[{payload['latitude']:.10f}, {payload['longitude']:.10f}], "
            "{animate:false});"
        )

    # ------------------------------------------------------------------ shared positions

    @staticmethod
    def _age_s(timestamp_ns: int, now_ns: int) -> float:
        timestamp_ns = int(timestamp_ns or 0)
        if timestamp_ns <= 0:
            return float("inf")
        return max(0.0, (int(now_ns) - timestamp_ns) / 1_000_000_000.0)

    @staticmethod
    def _raw_position_payload(position) -> dict:
        return {
            "latitude": float(position.latitude),
            "longitude": float(position.longitude),
            "altitude": float(position.altitude),
            "fix_quality": int(position.fix_quality),
            "satellites": int(position.satellites),
            "hdop": float(position.hdop),
            "timestamp_ns": int(position.timestamp_ns),
        }

    def _display_position_payload(
        self,
        source_name: str,
        position,
        source_rx_age_s: float,
        now_ns: int,
    ) -> dict:
        source_name = source_name.lower()
        gga_age_s = self._age_s(int(position.timestamp_ns), now_ns)
        source_fresh = (
            math.isfinite(source_rx_age_s)
            and source_rx_age_s <= POSITION_SOURCE_STALE_S
        )
        gga_fresh = gga_age_s <= POSITION_SOURCE_STALE_S
        raw_valid = valid_coordinate(
            bool(position.valid),
            float(position.latitude),
            float(position.longitude),
        )

        if raw_valid:
            last_valid = self._raw_position_payload(position)
            if source_name == "gnss":
                self.last_valid_gnss = last_valid
            else:
                self.last_valid_usbl = last_valid
        else:
            last_valid = (
                self.last_valid_gnss
                if source_name == "gnss"
                else self.last_valid_usbl
            )

        live = bool(raw_valid and source_fresh and gga_fresh)
        available = last_valid is not None

        if live:
            display = self._raw_position_payload(position)
            state = "LIVE"
            effective_fix = int(position.fix_quality)
        elif available:
            display = dict(last_valid)
            state = (
                "STALE / LAST VALID"
                if not (source_fresh and gga_fresh)
                else "NO FIX / LAST VALID"
            )
            effective_fix = 0
        else:
            display = {
                "latitude": 0.0,
                "longitude": 0.0,
                "altitude": 0.0,
                "fix_quality": 0,
                "satellites": 0,
                "hdop": 0.0,
                "timestamp_ns": int(position.timestamp_ns),
            }
            state = "NO DATA" if not math.isfinite(gga_age_s) else "NO FIX"
            effective_fix = 0

        return {
            "available": bool(available),
            "live": bool(live),
            "state": state,
            "latitude": float(display["latitude"]),
            "longitude": float(display["longitude"]),
            "altitude": float(display["altitude"]),
            "fix_quality": int(effective_fix),
            "raw_fix_quality": int(position.fix_quality),
            "satellites": int(display["satellites"]),
            "hdop": float(display["hdop"]),
            "timestamp_ns": int(position.timestamp_ns),
            "gga_age_s": float(gga_age_s),
            "rx_age_s": float(source_rx_age_s),
        }

    @staticmethod
    def _position_key(payload: dict):
        return (
            bool(payload["available"]),
            bool(payload["live"]),
            str(payload["state"]),
            round(float(payload["latitude"]), 10),
            round(float(payload["longitude"]), 10),
            round(float(payload["altitude"]), 4),
            int(payload["fix_quality"]),
            int(payload["timestamp_ns"]),
        )

    @staticmethod
    def _status_text(payload: dict) -> str:
        rx_age = payload["rx_age_s"]
        gga_age = payload["gga_age_s"]
        rx_text = "--" if not math.isfinite(rx_age) else f"{rx_age:.1f}s"
        gga_text = "--" if not math.isfinite(gga_age) else f"{gga_age:.1f}s"

        if not payload["available"]:
            return (
                f"{payload['state']}\n"
                f"Effective Fix: 0 | RX age: {rx_text} | GGA age: {gga_text}"
            )

        return (
            f"{payload['state']}\n"
            f"Lat : {payload['latitude']:.7f}\n"
            f"Lon : {payload['longitude']:.7f}\n"
            f"Alt : {payload['altitude']:.2f} m\n"
            f"Effective Fix: {payload['fix_quality']}   "
            f"Raw Fix: {payload['raw_fix_quality']}\n"
            f"Sat : {payload['satellites']}   "
            f"HDOP: {payload['hdop']:.2f}\n"
            f"RX age: {rx_text} | GGA age: {gga_text}"
        )

    @staticmethod
    def _wrap_nmea_sentence(sentence: str, max_chars: int = 42) -> str:
        """Wrap a diagnostic NMEA sentence at comma boundaries for GUI display."""
        value = str(sentence).strip()
        if not value or len(value) <= max_chars:
            return value

        fields = value.split(",")
        lines: list[str] = []
        current = ""

        for index, field in enumerate(fields):
            token = field if index == len(fields) - 1 else field + ","
            if current and len(current) + len(token) > max_chars:
                lines.append(current)
                current = token
            else:
                current += token

        if current:
            lines.append(current)

        return "\n".join(lines)

    @classmethod
    def _nmea_text(cls, snapshot, count: int) -> str:
        if snapshot is None:
            return "RX NMEA: unavailable"

        sentence = str(getattr(snapshot, "text", "")).strip()
        timestamp_ns = int(getattr(snapshot, "timestamp_ns", 0) or 0)

        if not sentence or timestamp_ns <= 0:
            return f"RX NMEA: waiting for sentence • Count {count:,}"

        age_s = max(
            0.0,
            (time.time_ns() - timestamp_ns) / 1_000_000_000.0,
        )
        state = "LIVE" if age_s <= POSITION_SOURCE_STALE_S else "STALE"
        wrapped_sentence = cls._wrap_nmea_sentence(sentence)
        return (
            f"RX NMEA • {state} • Age {age_s:.1f}s • Count {count:,}\n"
            f"{wrapped_sentence}"
        )

    def refresh_positions(self):
        shared = self.shared
        if shared is None:
            return

        try:
            now_ns = time.time_ns()
            gnss = shared.read_gnss()
            usbl = shared.read_usbl()

            gnss_nmea = (
                shared.read_gnss_nmea_sentence()
                if hasattr(shared, "read_gnss_nmea_sentence")
                else None
            )
            usbl_nmea = (
                shared.read_usbl_nmea_sentence()
                if hasattr(shared, "read_usbl_nmea_sentence")
                else None
            )

            gnss_nmea_ts = int(getattr(gnss_nmea, "timestamp_ns", 0) or 0)
            usbl_nmea_ts = int(getattr(usbl_nmea, "timestamp_ns", 0) or 0)
            if gnss_nmea_ts > 0 and gnss_nmea_ts != self.last_gnss_nmea_timestamp_ns:
                self.last_gnss_nmea_timestamp_ns = gnss_nmea_ts
                self.gnss_nmea_count += 1
            if usbl_nmea_ts > 0 and usbl_nmea_ts != self.last_usbl_nmea_timestamp_ns:
                self.last_usbl_nmea_timestamp_ns = usbl_nmea_ts
                self.usbl_nmea_count += 1

            if int(gnss.timestamp_ns) != self.last_gnss_timestamp_ns:
                self.last_gnss_timestamp_ns = int(gnss.timestamp_ns)
                self.gnss_update_count += 1
            if int(usbl.timestamp_ns) != self.last_usbl_timestamp_ns:
                self.last_usbl_timestamp_ns = int(usbl.timestamp_ns)
                self.usbl_update_count += 1

            core_state = "UNKNOWN"
            core_age = float("inf")
            gnss_age = self._age_s(int(gnss.timestamp_ns), now_ns)
            usbl_age = self._age_s(int(usbl.timestamp_ns), now_ns)

            if hasattr(shared, "read_acquisition_health"):
                health = shared.read_acquisition_health()
                core_age = float(health.heartbeat_age_s())
                core_state = "LIVE" if bool(health.is_alive()) else "STALE"
                if hasattr(health, "source_age_s"):
                    gnss_age = float(health.source_age_s("gnss"))
                    usbl_age = float(health.source_age_s("usbl"))

            gnss_payload = self._display_position_payload(
                "gnss", gnss, gnss_age, now_ns
            )
            usbl_payload = self._display_position_payload(
                "usbl", usbl, usbl_age, now_ns
            )

            self.gnss_raw_nmea.setText(
                self._nmea_text(gnss_nmea, self.gnss_nmea_count)
            )
            self.usbl_raw_nmea.setText(
                self._nmea_text(usbl_nmea, self.usbl_nmea_count)
            )
            self.gnss_raw_nmea.setToolTip(
                str(getattr(gnss_nmea, "text", "") or "").strip()
            )
            self.usbl_raw_nmea.setToolTip(
                str(getattr(usbl_nmea, "text", "") or "").strip()
            )

            core_age_text = "--" if not math.isfinite(core_age) else f"{core_age:.1f}s"
            self.gnss_status.setText(
                self._status_text(gnss_payload)
                + (
                    f"\nGGA Updates: {self.gnss_update_count:,}"
                    f" | NMEA: {self.gnss_nmea_count:,}"
                    f" | OBS Core: {core_state} {core_age_text}"
                )
            )
            self.usbl_status.setText(
                self._status_text(usbl_payload)
                + (
                    f"\nGGA Updates: {self.usbl_update_count:,}"
                    f" | NMEA: {self.usbl_nmea_count:,}"
                    f" | OBS Core: {core_state} {core_age_text}"
                )
            )

            self.center_gnss_button.setEnabled(bool(gnss_payload["available"]))

            if not self.map_ready:
                return

            auto_center = bool(self.auto_center_gnss.isChecked())
            gnss_key = self._position_key(gnss_payload)
            usbl_key = self._position_key(usbl_payload)

            map_update_needed = (
                gnss_key != self.last_map_gnss_key
                or usbl_key != self.last_map_usbl_key
                or (auto_center and bool(gnss_payload["live"]))
            )
            if not map_update_needed:
                return

            script = (
                "updatePositions("
                f"{json.dumps(gnss_payload)}, "
                f"{json.dumps(usbl_payload)}, "
                f"{str(auto_center).lower()}"
                ");"
            )
            self.run_js(script)

            self.last_map_gnss_key = gnss_key
            self.last_map_usbl_key = usbl_key

        except (
            AttributeError, BufferError, KeyError, OSError, RuntimeError,
            TypeError, ValueError,
        ) as exc:
            self.gnss_status.setText(f"shared_data error: {exc}")
            self.usbl_status.setText(f"shared_data error: {exc}")

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
                    Qt.ItemDataRole.UserRole,
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
            # _add_overlay_to_map() updates this label again from the JavaScript
            # callback when Leaflet confirms the overlay command.
            if loaded:
                self.geotiff_status.setText(
                    f"{loaded} GeoTIFF prepared • sending raster to map..."
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
            "png_path": str(png_path),
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
            self.geotiff_status.setText(
                "GeoTIFF prepared, but map page is not ready"
            )
            return

        # V10: use localhost HTTP again, but with a serializable JSON-string
        # callback and an asynchronous Leaflet image-load diagnostic.
        image_url = (
            f"{self.local_http_base}/"
            f"{record['png_name']}"
        )

        script = (
            "addGeoTiffOverlay("
            f"{json.dumps(record['overlay_id'])}, "
            f"{json.dumps(record['display_name'])}, "
            f"{json.dumps(image_url)}, "
            f"{json.dumps(record['bounds'])}, "
            f"{float(record['opacity'])}"
            ");"
        )

        def _poll_image_state():
            state_script = (
                "getGeoTiffLoadState("
                f"{json.dumps(record['overlay_id'])}"
                ");"
            )

            def _state_result(raw):
                try:
                    if raw is None:
                        self.geotiff_status.setText(
                            "GeoTIFF JS diagnostic returned no result"
                        )
                        return

                    result = (
                        json.loads(raw)
                        if isinstance(raw, str)
                        else raw
                    )

                    state = str(
                        result.get("state", "unknown")
                    )

                    if state == "loaded":
                        self.geotiff_status.setText(
                            "GeoTIFF loaded and rendered"
                        )
                    elif state == "error":
                        self.geotiff_status.setText(
                            "GeoTIFF image request failed"
                        )
                        QMessageBox.warning(
                            self,
                            APP_TITLE,
                            "Leaflet created the GeoTIFF layer, but the "
                            "generated PNG could not be loaded.\n\n"
                            f"Image URL:\n{image_url}"
                        )
                    else:
                        self.geotiff_status.setText(
                            f"GeoTIFF layer state: {state}"
                        )

                except Exception as exc:
                    self.geotiff_status.setText(
                        f"GeoTIFF diagnostic parse error: {exc}"
                    )

            try:
                self.map_view.page().runJavaScript(
                    state_script,
                    _state_result,
                )
            except Exception as exc:
                self.geotiff_status.setText(
                    f"GeoTIFF diagnostic JS error: {exc}"
                )

        def _overlay_result(raw):
            try:
                if raw is None:
                    self.geotiff_status.setText(
                        "GeoTIFF JS returned no result"
                    )
                    QMessageBox.warning(
                        self,
                        APP_TITLE,
                        "GeoTIFF JavaScript returned no result.\n\n"
                        "This usually means the map script did not execute "
                        "or the JavaScript context is not ready."
                    )
                    return

                result = (
                    json.loads(raw)
                    if isinstance(raw, str)
                    else raw
                )

                if bool(result.get("ok")):
                    self.geotiff_status.setText(
                        "GeoTIFF layer created • waiting for image..."
                    )
                    QTimer.singleShot(
                        700,
                        _poll_image_state,
                    )
                else:
                    stage = result.get("stage", "unknown")
                    error = result.get(
                        "error",
                        "unknown JavaScript error"
                    )

                    self.geotiff_status.setText(
                        f"GeoTIFF JS failed at {stage}: {error}"
                    )

                    QMessageBox.warning(
                        self,
                        APP_TITLE,
                        "GeoTIFF JavaScript overlay failed.\n\n"
                        f"Stage: {stage}\n"
                        f"Error: {error}"
                    )

            except Exception as exc:
                self.geotiff_status.setText(
                    f"GeoTIFF callback parse error: {exc}"
                )

        try:
            self.map_view.page().runJavaScript(
                script,
                _overlay_result,
            )
        except Exception as exc:
            self.geotiff_status.setText(
                f"GeoTIFF runJavaScript error: {exc}"
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
                Qt.ItemDataRole.UserRole
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

            QScrollArea#settingsScroll {
                background-color: #07131D;
                border: none;
            }

            QScrollArea#settingsScroll > QWidget > QWidget {
                background-color: #07131D;
            }

            QScrollBar:vertical {
                background: #07131D;
                width: 9px;
                margin: 0px;
            }

            QScrollBar::handle:vertical {
                background: #24485D;
                min-height: 28px;
                border-radius: 4px;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
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

            QLabel#rawNmeaLabel {
                color: #9FD8F2;
                background-color: #071620;
                border: 1px solid #24485D;
                border-radius: 5px;
                padding: 5px;
                font-family: "Consolas";
                font-size: 9px;
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
            if self.local_http_server is not None:
                self.local_http_server.shutdown()
                self.local_http_server.server_close()
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
