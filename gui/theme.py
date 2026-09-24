#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dark theme for the SilkroadWineEnvironment GUI - colors, fonts, QSS, icons.

Everything visual lives here; no other module should contain a color literal.

Two hard constraints shape this file, both verified against what the AppImage
actually ships (see build-appimage.sh):

1. The bundle contains NO Qt style plugin and deliberately deletes the GTK3
   platform theme plugin, so Fusion is the only style available and the host
   desktop's theme is never picked up. That is why apply_theme() sets Fusion
   explicitly and paints everything itself - the look is then identical on
   every distro instead of depending on what happens to be installed.

2. The bundled PySide6 Python modules are only QtCore/QtDBus/QtGui/QtNetwork/
   QtWidgets - QtSvg's *Python binding* is NOT among them. The Qt SVG
   *library* and its image-format/icon-engine plugins ARE bundled though, so
   SVG still works through QIcon/QImageReader/QSS url() - just never via
   `import PySide6.QtSvg`.

Target is Qt 6.6 / Python 3.8 (the AppImage builds on Ubuntu 20.04), so
nothing newer may be used - notably QStyleHints.setColorScheme(), which only
exists from Qt 6.8 on.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, QStandardPaths
from PySide6.QtGui import QColor, QFont, QIcon, QImageReader, QPalette, QPixmap

# --------------------------------------------------------------- color tokens
BG_APP        = "#0F1419"
BG_SIDEBAR    = "#12181F"
BG_CONTENT    = "#161C24"
BG_CARD       = "#1C232D"
BG_CARD_HOVER = "#222A35"
BG_INPUT      = "#10151B"

BORDER        = "#2A3441"
BORDER_SUBTLE = "#222B36"

TEXT          = "#E6EDF3"
TEXT_MUTED    = "#8B98A8"
TEXT_DIM      = "#64707E"

ACCENT         = "#4C8DFF"
ACCENT_HOVER   = "#6BA0FF"
ACCENT_PRESSED = "#3A78E0"

STEP_BAR = "#56C7E0"           # second progress bar - same family, clearly distinct

OK     = "#3FB950"
WARN   = "#D29922"
DANGER = "#F85149"


def alpha(hex_color: str, a: float) -> str:
    """A translucent version of `hex_color`, as an rgba() string for QSS.

    NOT written as an 8-digit hex literal: Qt parses "#12345678" as
    #AARRGGBB (alpha first), while CSS and every design tool write
    #RRGGBBAA. Getting that backwards does not fail loudly - it silently
    yields a different colour. Confirmed the hard way: "#D299221F", meant as
    amber at 12%, rendered as opaque dark red, turning every "missing"
    status badge red as if something were broken.
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "rgba(%d, %d, %d, %.3f)" % (r, g, b, a)


ACCENT_SOFT   = alpha(ACCENT, 0.15)    # tinted fills (selected nav, badges)
ACCENT_LINE   = alpha(ACCENT, 0.27)    # tinted borders
OK_SOFT       = alpha(OK, 0.12)
OK_LINE       = alpha(OK, 0.27)
WARN_SOFT     = alpha(WARN, 0.12)
WARN_LINE     = alpha(WARN, 0.27)
DANGER_SOFT   = alpha(DANGER, 0.12)
DANGER_LINE   = alpha(DANGER, 0.33)
DANGER_BORDER = alpha(DANGER, 0.27)

RADIUS      = 6
RADIUS_CARD = 10

# ---------------------------------------------------------------------- fonts
# No font is bundled (that would need --add-data plus addApplicationFont), so
# this is a preference list resolved against whatever fontconfig finds on the
# host - the last entries are near-universal on Linux.
UI_FAMILIES = ["Inter", "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans", "Sans Serif"]
MONO_FAMILIES = ["JetBrains Mono", "Fira Code", "Cascadia Mono", "Source Code Pro",
                 "Noto Sans Mono", "DejaVu Sans Mono", "Monospace"]


def ui_font(point_size: int = 10, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(UI_FAMILIES)          # Qt 6.0+
    f.setPointSize(point_size)
    f.setBold(bold)
    return f


def mono_font(point_size: int = 9) -> QFont:
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(point_size)
    return f


# ---------------------------------------------------------------------- icons
# Feather-style outline icons, inlined as source strings rather than shipped as
# files: a string needs no --add-data entry, so adding icons costs no change to
# build-appimage.sh at all. `currentColor` is substituted per use.
_SVG = {
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "check-circle": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    "alert": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "x-circle": '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
    "circle": '<circle cx="12" cy="12" r="9"/>',
    "loader": '<line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/>',
    "chevron-right": '<polyline points="9 18 15 12 9 6"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "power": '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "arrow-right": '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
    "file-text": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "external-link": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
}

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)


def svg_source(name: str, color: str, width: float = 2.0) -> str:
    return _SVG_TEMPLATE.format(color=color, width=width, body=_SVG.get(name, ""))


def pixmap(name: str, color: str, size: int = 20, dpr: float = 1.0) -> QPixmap:
    """Render one inline SVG to a QPixmap.

    Goes through QImageReader with an explicit scaled size rather than
    QPixmap.loadFromData() so the SVG is rasterised AT the target resolution
    (vector -> crisp on HiDPI) instead of being decoded at its 24x24 viewBox
    and then scaled up into a blurry mess.
    """
    data = QByteArray(svg_source(name, color).encode("utf-8"))
    buf = QBuffer(data)
    buf.open(QIODevice.ReadOnly)
    reader = QImageReader(buf, QByteArray(b"svg"))
    px = max(1, int(round(size * dpr)))
    reader.setScaledSize(QSize(px, px))
    img = reader.read()
    if img.isNull():
        return QPixmap()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str = TEXT, size: int = 20, dpr: float = 2.0) -> QIcon:
    """QIcon for one inline SVG, or an empty QIcon if SVG support is missing.

    Never raises: every caller pairs its icon with a text label, so a bundle
    without the SVG image-format plugin degrades to a text-only button rather
    than taking the whole GUI down.
    """
    try:
        pm = pixmap(name, color, size, dpr)
        return QIcon(pm) if not pm.isNull() else QIcon()
    except Exception:
        return QIcon()


def svg_available() -> bool:
    try:
        return b"svg" in [bytes(f) for f in QImageReader.supportedImageFormats()]
    except Exception:
        return False


# ------------------------------------------------------ icons as files for QSS
# QSS `image:`/`border-image:` only accept a URL, never inline data - so the few
# icons the stylesheet needs (checkbox tick, combo arrow, tree branches) are
# written out once at startup and referenced by path. Purely a cache: if this
# fails, build_qss() simply omits those rules and Fusion draws its own
# indicators.
_QSS_ICONS = {
    "check": (TEXT, 2.6),
    "chevron-down": (TEXT_MUTED, 2.2),
    "chevron-right": (TEXT_MUTED, 2.2),
    "chevron-down-accent": (ACCENT, 2.2),
    "chevron-right-accent": (ACCENT, 2.2),
}


def _materialise_qss_icons() -> dict:
    """{name: absolute path} for the icons QSS references. Empty on failure."""
    try:
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        if not base:
            return {}
        d = os.path.join(base, "sro-gui-icons")
        os.makedirs(d, exist_ok=True)
        out = {}
        for key, (color, width) in _QSS_ICONS.items():
            name = key.replace("-accent", "")
            path = os.path.join(d, key + ".svg")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(svg_source(name, color, width))
            # QSS url() has no escaping for backslashes/quotes; these paths are
            # always POSIX here, but keep forward slashes explicit anyway.
            out[key] = path.replace("\\", "/")
        return out
    except Exception:
        return {}


# ------------------------------------------------------------------ stylesheet
def build_qss(icons: dict | None = None) -> str:
    icons = icons or {}

    def img(key, prop="image"):
        p = icons.get(key)
        # Quoted: an unquoted url() breaks the WHOLE stylesheet on a path
        # with characters like < > ( ) - seen with a test harness whose app
        # name (and so its cache dir) was "<stdin>".
        return '%s: url("%s");' % (prop, p) if p else ""

    return """
/* ---- base ------------------------------------------------------------- */
QWidget {
    background: transparent;
    color: %(TEXT)s;
}
QMainWindow, QDialog, QMessageBox, QFileDialog {
    background: %(BG_APP)s;
}
QLabel { background: transparent; }
QLabel[role="muted"]    { color: %(TEXT_MUTED)s; }
QLabel[role="dim"]      { color: %(TEXT_DIM)s; }
QLabel[role="title"]    { color: %(TEXT)s; }
QLabel[role="ok"]       { color: %(OK)s; }
QLabel[role="warn"]     { color: %(WARN)s; }
QLabel[role="danger"]   { color: %(DANGER)s; }
QLabel[role="accent"]   { color: %(ACCENT)s; }

/* ---- structure -------------------------------------------------------- */
#sidebar {
    background: %(BG_SIDEBAR)s;
    border-right: 1px solid %(BORDER_SUBTLE)s;
}
#content { background: %(BG_CONTENT)s; }

#card {
    background: %(BG_CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS_CARD)spx;
}
#card[tone="danger"] { border-color: %(DANGER_BORDER)s; }
/* body of a CollapsibleSection: squared off at the top so it reads as one
   piece with its header button rather than as a second, floating card */
#card[joined="top"] {
    border-top-left-radius: 0;
    border-top-right-radius: 0;
    border-top: none;
}

#statusPanel {
    background: %(BG_CARD)s;
    border-top: 1px solid %(BORDER)s;
}
#logDrawer {
    background: %(BG_APP)s;
    border-top: 1px solid %(BORDER)s;
}

/* ---- sidebar nav ------------------------------------------------------ */
#navList {
    background: transparent;
    border: none;
    outline: none;
}
#navList::item {
    color: %(TEXT_MUTED)s;
    border-radius: %(RADIUS)spx;
    padding: 9px 10px;
    margin: 2px 8px;
    border-left: 3px solid transparent;
}
#navList::item:hover {
    background: %(BG_CARD_HOVER)s;
    color: %(TEXT)s;
}
#navList::item:selected {
    background: %(ACCENT_SOFT)s;
    color: %(ACCENT)s;
    border-left: 3px solid %(ACCENT)s;
}

/* ---- buttons ---------------------------------------------------------- */
QPushButton {
    background: %(BG_CARD_HOVER)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    padding: 7px 14px;
}
QPushButton:hover   { background: #2A3340; border-color: #36414F; }
QPushButton:pressed { background: #1A212B; }
QPushButton:disabled { color: %(TEXT_DIM)s; background: #1A212B; border-color: %(BORDER_SUBTLE)s; }
QPushButton:focus   { border-color: %(ACCENT)s; }

QPushButton[variant="primary"] {
    background: %(ACCENT)s;
    color: #0B1220;
    border: 1px solid %(ACCENT)s;
    font-weight: bold;
}
QPushButton[variant="primary"]:hover   { background: %(ACCENT_HOVER)s; border-color: %(ACCENT_HOVER)s; }
QPushButton[variant="primary"]:pressed { background: %(ACCENT_PRESSED)s; border-color: %(ACCENT_PRESSED)s; }
QPushButton[variant="primary"]:disabled { background: #24303F; color: %(TEXT_DIM)s; border-color: %(BORDER)s; }

QPushButton[variant="ghost"] {
    background: transparent;
    border: 1px solid transparent;
    color: %(TEXT_MUTED)s;
}
QPushButton[variant="ghost"]:hover    { background: %(BG_CARD_HOVER)s; color: %(TEXT)s; }
QPushButton[variant="ghost"]:disabled { color: %(TEXT_DIM)s; background: transparent; }

QPushButton[variant="danger"] {
    background: transparent;
    border: 1px solid %(DANGER_LINE)s;
    color: %(DANGER)s;
}
QPushButton[variant="danger"]:hover    { background: %(DANGER_SOFT)s; border-color: %(DANGER)s; }
QPushButton[variant="danger"]:disabled { color: %(TEXT_DIM)s; border-color: %(BORDER_SUBTLE)s; }

/* section toggle - a button that has to read as a card header, not a button */
QPushButton#sectionToggle {
    background: %(BG_CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS_CARD)spx;
    text-align: left;
    padding: 10px 12px;
    color: %(TEXT_MUTED)s;
}
QPushButton#sectionToggle:hover   { background: %(BG_CARD_HOVER)s; color: %(TEXT)s; }
QPushButton#sectionToggle:checked { color: %(TEXT)s; border-bottom-left-radius: 0; border-bottom-right-radius: 0; }

/* quiet text-only action ("Re-run setup", "Project") */
QPushButton[variant="link"] {
    background: transparent;
    border: none;
    padding: 2px 0;
    color: %(TEXT_MUTED)s;
    text-align: left;
}
QPushButton[variant="link"]:hover    { color: %(ACCENT)s; text-decoration: underline; }
QPushButton[variant="link"]:disabled { color: %(TEXT_DIM)s; }
/* the sidebar's "Update available" pill */
QPushButton[variant="update"] {
    background: %(ACCENT_SOFT)s;
    border: 1px solid %(ACCENT_LINE)s;
    border-radius: 9px;
    padding: 2px 8px;
    color: %(ACCENT)s;
    font-size: 8pt;
}
QPushButton[variant="update"]:hover { border-color: %(ACCENT)s; }

/* one option of a small either/or choice (phBot channel) - a card that
   lights up when picked, so the two options can carry a second line each */
QPushButton[variant="choice"] {
    background: %(BG_INPUT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS_CARD)spx;
    padding: 10px 14px;
    text-align: left;
    color: %(TEXT_MUTED)s;
}
QPushButton[variant="choice"]:hover   { border-color: #36414F; color: %(TEXT)s; }
QPushButton[variant="choice"]:checked { background: %(ACCENT_SOFT)s; border-color: %(ACCENT)s; color: %(TEXT)s; }

/* ---- setup hero ------------------------------------------------------- */
#heroIcon { border-radius: 22px; background: %(BG_CARD_HOVER)s; }
#heroIcon[tone="accent"] { background: %(ACCENT_SOFT)s; }
#heroIcon[tone="warn"]   { background: %(WARN_SOFT)s; }
#heroIcon[tone="ok"]     { background: %(OK_SOFT)s; }

/* ---- status tiles ----------------------------------------------------- */
#tile {
    background: %(BG_INPUT)s;
    border: 1px solid %(BORDER_SUBTLE)s;
    border-radius: %(RADIUS)spx;
}
#tile[state="missing"] { border-color: %(WARN_LINE)s; }
#dot { border-radius: 4px; background: %(TEXT_DIM)s; }
#dot[state="ok"]      { background: %(OK)s; }
#dot[state="missing"] { background: %(WARN)s; }
#dot[state="on"]      { background: %(ACCENT)s; }
#dot[state="off"]     { background: transparent; border: 1px solid %(TEXT_DIM)s; }
#dot[state="unknown"] { background: transparent; border: 1px dashed %(TEXT_DIM)s; }

/* ---- sidebar footer --------------------------------------------------- */
#sidebarFooter { border-top: 1px solid %(BORDER_SUBTLE)s; }

/* ---- inputs ----------------------------------------------------------- */
QLineEdit {
    background: %(BG_INPUT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    padding: 7px 10px;
    selection-background-color: %(ACCENT)s;
    selection-color: #0B1220;
}
QLineEdit:focus { border-color: %(ACCENT)s; }
QLineEdit:disabled { color: %(TEXT_DIM)s; }

QComboBox {
    background: %(BG_INPUT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    padding: 6px 10px;
    min-height: 18px;
}
QComboBox:hover { border-color: #36414F; }
QComboBox:focus { border-color: %(ACCENT)s; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { width: 12px; height: 12px; %(ICON_CHEVRON_DOWN)s }
QComboBox QAbstractItemView {
    background: %(BG_CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    selection-background-color: %(ACCENT_SOFT)s;
    selection-color: %(ACCENT)s;
    outline: none;
    padding: 4px;
}

QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid %(BORDER)s;
    border-radius: 4px;
    background: %(BG_INPUT)s;
}
QCheckBox::indicator:hover { border-color: %(ACCENT)s; }
QCheckBox::indicator:checked {
    background: %(ACCENT)s;
    border-color: %(ACCENT)s;
    %(ICON_CHECK)s
}
QCheckBox:disabled { color: %(TEXT_DIM)s; }

/* ---- progress --------------------------------------------------------- */
QProgressBar {
    background: %(BG_INPUT)s;
    border: none;
    border-radius: 3px;
    text-align: center;
    color: %(TEXT_MUTED)s;
}
QProgressBar::chunk { background: %(ACCENT)s; border-radius: 3px; }
QProgressBar[bar="step"]::chunk   { background: %(STEP_BAR)s; }
QProgressBar[bar="ok"]::chunk     { background: %(OK)s; }
QProgressBar[bar="danger"]::chunk { background: %(DANGER)s; }

/* ---- tabs (Launch sub-tabs, as a segmented control) ------------------- */
QTabWidget::pane { border: none; background: transparent; }
QTabBar { qproperty-drawBase: 0; background: transparent; }
QTabBar::tab {
    background: transparent;
    color: %(TEXT_MUTED)s;
    border: 1px solid transparent;
    border-radius: %(RADIUS)spx;
    padding: 7px 16px;
    margin-right: 4px;
}
QTabBar::tab:hover    { color: %(TEXT)s; background: %(BG_CARD_HOVER)s; }
QTabBar::tab:selected { color: %(ACCENT)s; background: %(ACCENT_SOFT)s; border-color: %(ACCENT_LINE)s; }

/* ---- tree (Manage) ---------------------------------------------------- */
QTreeWidget {
    background: %(BG_INPUT)s;
    alternate-background-color: #141A22;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    outline: none;
}
QTreeWidget::item { padding: 5px 4px; border: none; }
QTreeWidget::item:hover    { background: %(BG_CARD_HOVER)s; }
QTreeWidget::item:selected { background: %(ACCENT_SOFT)s; color: %(ACCENT)s; }
QHeaderView::section {
    background: %(BG_CARD)s;
    color: %(TEXT_MUTED)s;
    border: none;
    border-bottom: 1px solid %(BORDER)s;
    padding: 6px 8px;
}

/* ---- log -------------------------------------------------------------- */
QPlainTextEdit#logView {
    background: %(BG_APP)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    padding: 8px;
    selection-background-color: %(ACCENT)s;
    selection-color: #0B1220;
}

/* ---- scroll areas / bars ---------------------------------------------- */
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical {
    background: transparent; width: 11px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #2E3947; border-radius: 5px; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #3C4A5C; }
QScrollBar:horizontal {
    background: transparent; height: 11px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #2E3947; border-radius: 5px; min-width: 28px;
}
QScrollBar::handle:horizontal:hover { background: #3C4A5C; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ---- context menus ---------------------------------------------------- */
QMenu {
    background: %(BG_CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    padding: 4px;
}
QMenu::item { padding: 6px 16px 6px 8px; border-radius: 4px; color: %(TEXT)s; }
QMenu::item:selected { background: %(ACCENT_SOFT)s; color: %(ACCENT)s; }
QMenu::item:disabled { color: %(TEXT_DIM)s; background: transparent; font-weight: bold; }
QMenu::icon { padding-left: 6px; }
QMenu::separator { height: 1px; background: %(BORDER_SUBTLE)s; margin: 4px 6px; }

/* ---- misc ------------------------------------------------------------- */
QToolTip {
    background: %(BG_CARD)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER)s;
    border-radius: %(RADIUS)spx;
    padding: 5px 8px;
}
#hline { background: %(BORDER_SUBTLE)s; }
""" % {
        "TEXT": TEXT, "TEXT_MUTED": TEXT_MUTED, "TEXT_DIM": TEXT_DIM,
        "BG_APP": BG_APP, "BG_SIDEBAR": BG_SIDEBAR, "BG_CONTENT": BG_CONTENT,
        "BG_CARD": BG_CARD, "BG_CARD_HOVER": BG_CARD_HOVER, "BG_INPUT": BG_INPUT,
        "BORDER": BORDER, "BORDER_SUBTLE": BORDER_SUBTLE,
        "ACCENT": ACCENT, "ACCENT_HOVER": ACCENT_HOVER,
        "ACCENT_PRESSED": ACCENT_PRESSED, "ACCENT_SOFT": ACCENT_SOFT,
        "STEP_BAR": STEP_BAR,
        "OK": OK, "WARN": WARN, "DANGER": DANGER,
        "OK_SOFT": OK_SOFT, "WARN_SOFT": WARN_SOFT, "DANGER_SOFT": DANGER_SOFT,
        "OK_LINE": OK_LINE, "WARN_LINE": WARN_LINE,
        "DANGER_LINE": DANGER_LINE, "DANGER_BORDER": DANGER_BORDER,
        "ACCENT_LINE": ACCENT_LINE,
        "RADIUS": RADIUS, "RADIUS_CARD": RADIUS_CARD,
        "ICON_CHECK": img("check"),
        "ICON_CHEVRON_DOWN": img("chevron-down"),
    }


# --------------------------------------------------------------------- palette
def build_palette() -> QPalette:
    """Palette for everything QSS cannot reach.

    QMessageBox, QFileDialog, QInputDialog and tooltips are painted by the
    style from the palette, not from the app stylesheet - without this they
    would stay bright white in an otherwise dark app. The Disabled group
    matters just as much: Fusion derives greyed-out text from it, and leaving
    it at the default produces unreadable dark-on-dark labels.
    """
    p = QPalette()
    c = QColor
    p.setColor(QPalette.Window,          c(BG_APP))
    p.setColor(QPalette.WindowText,      c(TEXT))
    p.setColor(QPalette.Base,            c(BG_INPUT))
    p.setColor(QPalette.AlternateBase,   c(BG_CARD))
    p.setColor(QPalette.Text,            c(TEXT))
    p.setColor(QPalette.Button,          c(BG_CARD_HOVER))
    p.setColor(QPalette.ButtonText,      c(TEXT))
    p.setColor(QPalette.BrightText,      c(DANGER))
    p.setColor(QPalette.Highlight,       c(ACCENT))
    p.setColor(QPalette.HighlightedText, c("#0B1220"))
    p.setColor(QPalette.ToolTipBase,     c(BG_CARD))
    p.setColor(QPalette.ToolTipText,     c(TEXT))
    p.setColor(QPalette.PlaceholderText, c(TEXT_DIM))
    p.setColor(QPalette.Link,            c(ACCENT))
    p.setColor(QPalette.LinkVisited,     c(ACCENT_PRESSED))
    p.setColor(QPalette.Mid,             c(BORDER))
    p.setColor(QPalette.Dark,            c(BG_APP))
    p.setColor(QPalette.Shadow,          c("#05080B"))

    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                 QPalette.HighlightedText):
        p.setColor(QPalette.Disabled, role, c(TEXT_DIM))
    p.setColor(QPalette.Disabled, QPalette.Base, c(BG_APP))
    p.setColor(QPalette.Disabled, QPalette.Button, c("#1A212B"))
    p.setColor(QPalette.Disabled, QPalette.Highlight, c("#24303F"))
    return p


def apply_theme(app) -> None:
    """Fusion + dark palette + the global stylesheet. Call once from main()."""
    # Fusion is not a plugin (it is compiled into QtWidgets), so this is the
    # one style guaranteed to exist in the AppImage - and setting it
    # explicitly means a dev run on a machine WITH style plugins installed
    # renders identically to the shipped bundle instead of picking up Breeze
    # or Adwaita and looking different from what users get.
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setPalette(build_palette())
    app.setFont(ui_font(10))
    app.setStyleSheet(build_qss(_materialise_qss_icons()))
