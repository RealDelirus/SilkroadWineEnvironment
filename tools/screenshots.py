#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/screenshots.py - regenerate the pictures in docs/images/ from the
real GUI, so the README and the guide never show an outdated interface.

    python3 tools/screenshots.py            # -> docs/images/*.png
    python3 tools/screenshots.py OUT_DIR

Needs PySide6 (pip install -r gui/requirements.txt); the terminal-menu shot
additionally needs pyte (pip install pyte) and is skipped without it.

Everything runs off-screen against demo data: a throw-away SRO_HOME, fake
saved clients under /home/user/Games, a fake process list - nothing on this
machine is read or started, and no real path or name ends up in the images.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["SRO_NO_UPDATE_CHECK"] = "1"
TMP = Path(tempfile.mkdtemp(prefix="sro-shots-"))
os.environ["SRO_HOME"] = str(TMP / "home")
(TMP / "home").mkdir()
(TMP / "home" / "sro.sh").write_text("#!/bin/sh\nexit 1\n")   # "setup finished"

sys.path.insert(0, str(ROOT / "gui"))
from PySide6.QtCore import QPoint, QRect, QSettings, Qt                  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QPushButton, QTabWidget  # noqa: E402

# Settings (remembered options etc.) go to the temp dir, not ~/.config.
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(TMP / "config"))

import main  # noqa: E402

# ------------------------------------------------------------------ demo data
GAMES = "/home/user/Games"
CLIENTS = [("maxiguard", "Athens 80 Cap", GAMES + "/Athens 80 Cap"),
           ("maxiguard", "Eternal Online", GAMES + "/Eternal Online"),
           ("maxiguard", "Legion SRO", GAMES + "/Legion SRO")]
HAS_LAUNCHER = {GAMES + "/Athens 80 Cap", GAMES + "/Eternal Online"}
PROCESSES = [
    {"ref": "a", "label": "phBot Manager (MaxiGuard / GE-Proton)", "kind": "job", "depth": 0, "has_children": True, "pid": "41210"},
    {"ref": "b", "label": "phBot.exe", "kind": "ext", "depth": 1, "has_children": True, "pid": "41388"},
    {"ref": "c", "label": "sro_client.exe", "kind": "ext", "depth": 2, "has_children": False, "pid": "41502"},
    {"ref": "d", "label": "phBot.exe", "kind": "ext", "depth": 1, "has_children": True, "pid": "41611"},
    {"ref": "e", "label": "sro_client.exe", "kind": "ext", "depth": 2, "has_children": False, "pid": "41730"},
    {"ref": "f", "label": "MaxiGuard: Eternal Online (launcher)", "kind": "job", "depth": 0, "has_children": False, "pid": "43011"},
]
ALL = dict(distro="apt", deps=True, wine=True, wine_version="wine-11.15", umu=True, runtime32=True,
           ge_proton=True, maxiguard_prefix=True, maxiguard=True, vsroplus=True, phbot=True,
           phbot_channel="testing", phbot_version="33.7.9", phbot_components="manager,plugins,navmesh,minimap",
           launcher=True, wined3d_forced=False, build_cache="",
           wine_build_version="11.15")
NONE = {k: (False if isinstance(v, bool) else v) for k, v in ALL.items()}
NONE["wine_version"] = ""
PARTIAL = dict(ALL, maxiguard_prefix=False, maxiguard=False, vsroplus=False)

LOG_LINES = """$ swe.sh --skip-deps --skip-wine --phbot --phbot-channel testing --phbot-components manager,plugins,navmesh,minimap --maxiguard --vsroplus
>>> STEP: Setting up phBot
### Setting up phBot
==> VC++ runtime set up (v14.51.36247.00).
==> phBot 33.7.9 (testing)  +  Manager 3.2.5
>>> DL: 33.7.9.zip [1/5] 69.2/69.2 MB 100% 11.9 MB/s ETA 0:00 | total 69.2/177.2 MB 39% ETA 0:09
==> phBot 33.7.9 (testing) installed.
>>> DONE: Setting up phBot
>>> STEP: Setting up MaxiGuard environment
### Setting up MaxiGuard environment
==> Looking for GE-Proton11-7 ...
==> Downloading GE-Proton11-7-x86_64.tar.gz
!!  Download interrupted - retrying (1/3)
XX  GE-Proton download failed: could not resolve host github.com
>>> FAIL 1: Setting up MaxiGuard environment""".splitlines()

main.read_clients = lambda: list(CLIENTS)
main.available_modes = lambda: {"plain": True, "vsroplus": True, "maxiguard": True}
main.ci_file_exists = lambda folder, name: str(folder) in HAS_LAUNCHER
main.run_sro_sh = lambda args, timeout_ms=15000: (1, "", "")
main.system_summary = lambda: "Ubuntu 24.04 LTS · x86_64"
main.shortcut_dirs = lambda: {"desktop": TMP / "Desktop", "menu": TMP / "applications"}
main.SetupTab.refresh_status = lambda self: None
main.ManageTab._list_json = lambda self: list(PROCESSES)
try:
    VERSION = subprocess.run(["git", "-C", str(ROOT), "describe", "--tags", "--abbrev=0"],
                             capture_output=True, text=True).stdout.strip() or "v1.0.0"
except OSError:
    VERSION = "v1.0.0"
main.app_version = lambda: VERSION

app = QApplication(["sro-gui"])
app.setOrganizationName("SilkroadWineEnvironment")
app.setApplicationName("sro-gui")
main.theme.apply_theme(app)

SIZE = (960, 660)


def settle(n=6):
    for _ in range(n):
        app.processEvents()


def window(status, size=SIZE, page=0):
    QSettings().clear()
    w = main.MainWindow()
    w.resize(*size)
    w.show()
    w.setup_tab.apply_status(status)
    w.sidebar.nav.setCurrentRow(page)
    settle()
    return w


def save(img, name):
    path = OUT / (name + ".png")
    img.save(str(path))
    print("  %s  (%dx%d)" % (path.relative_to(ROOT) if ROOT in path.parents else path,
                             img.width(), img.height()))


def shot(w, name):
    settle()
    save(w.grab().toImage(), name)
    w.close()
    settle()


def section(w, prefix):
    for b in w.findChildren(QPushButton, "sectionToggle"):
        if b.text().strip().startswith(prefix):
            b.click()
    settle()


def launch_tab(w, title):
    tabs = w.launch_tab.findChild(QTabWidget)
    for i in range(tabs.count()):
        if tabs.tabText(i) == title:
            tabs.setCurrentIndex(i)
    settle()


# ---------------------------------------------------------------- GUI shots
print("GUI:")
shot(window(ALL), "setup-complete")
shot(window(NONE), "setup-fresh")
shot(window(PARTIAL), "setup-partial")

w = window(NONE)
w.setup_tab._busy = lambda: True
w.status_panel.set_state("running", "Installation running", "Install")
w.status_panel.set_detail("Step 3 of 6 · Building wine-sro — compiling (12,431 of 29,870 objects)")
w.status_panel.set_progress(0.46, 0.42, "18 min left")
w.status_panel.set_elapsed("21:07")
w.setup_tab.render_status_rows()
shot(w, "setup-running")

w = window(ALL)
section(w, "Advanced")
shot(w, "setup-advanced")

# phBot downloading: the status panel fed with the same lines a real run prints.
w = window(dict(NONE, deps=True, wine=True, launcher=True))
w.setup_tab._busy = lambda: True
w.setup_tab.tracker = w.setup_tab._make_tracker(["--skip-deps", "--skip-wine", "--phbot"])
w.status_panel.set_state("running", "Installation running", "Install")
for line in """>>> STEP: Setting up phBot
==> phBot 33.7.9 (testing)  +  Manager 3.2.5
==> Downloading 5 file(s), 177.2 MB ...
>>> DL: 33.7.9.zip [1/5] 69.2/69.2 MB 100% 11.9 MB/s ETA 0:00 | total 69.2/177.2 MB 39% ETA 0:09
>>> DL: Manager.exe [2/5] 11.0/11.0 MB 100% 11.0 MB/s ETA 0:00 | total 80.2/177.2 MB 45% ETA 0:08
>>> DL: plugins314.zip [3/5] 17.1/17.1 MB 100% 11.5 MB/s ETA 0:00 | total 97.3/177.2 MB 54% ETA 0:07
>>> DL: navmesh.zip [4/5] 12.0/22.4 MB 53% 11.8 MB/s ETA 0:01 | total 109.3/177.2 MB 61% ETA 0:06""".splitlines():
    w.setup_tab.tracker.feed(line)
w.setup_tab._paint_progress()
w.status_panel.set_elapsed("00:47")
w.setup_tab.render_status_rows()
shot(w, "setup-phbot-download")

w = window(PARTIAL, size=(960, 860))
w.status_panel.set_state("running", "Installation running", "Continue install")
w.status_panel.set_progress(0.71, 0.3)
for line in LOG_LINES:
    w.log_drawer.log_view.append_line(line)
w.status_panel.set_state("failed", "Failed (exit code 1)", "Open Details for the full output.")
w.status_panel.finish_bars(False)
w.status_panel.open_details()
shot(w, "setup-failed-log")

w = window(ALL, page=1)
launch_tab(w, "phBot")
pane = w.launch_tab.findChild(main.PhbotPane)
pane.mode_combo.setCurrentIndex(pane.mode_combo.findData("maxiguard"))
shot(w, "launch-phbot")

w = window(ALL, page=1)
launch_tab(w, "Silkroad Client")
cpane = w.launch_tab.findChild(main.ClientPane)
cpane.mode_combo.setCurrentIndex(cpane.mode_combo.findData("maxiguard"))
cpane.reload_clients()
settle()
save(w.grab().toImage(), "launch-clients")

# Right-click menu, composited onto the window where it would pop up.
captured = []


class _CaptureMenu(QMenu):
    def exec(self, *_a):
        captured.append(self)


main.QMenu = _CaptureMenu
card = cpane.findChildren(main.widgets.ClientCard)[0]
cpane._shortcut_menu(QPoint(0, 0), "maxiguard", CLIENTS[0][2], CLIENTS[0][1], True)
menu = captured[-1]
menu.adjustSize()
menu.show()
settle()
base = w.grab().toImage()
at = card.mapTo(w, QPoint(card.width() - 330, card.height() - 6))
p = QPainter(base)
p.drawImage(at, menu.grab().toImage())
p.end()
save(base, "launch-context-menu")
menu.close()
w.close()
settle()

w = window(ALL, page=2)
w.manage_tab.reload()
w.manage_tab.tree.setCurrentItem(w.manage_tab.tree.topLevelItem(0))
shot(w, "manage")

w = window(ALL)
w.sidebar.footer.set_update("v9.9.9")
settle()
side = w.sidebar.grab().toImage()
save(side.copy(0, side.height() - 190, side.width(), 190), "sidebar-footer")
w.close()
settle()

# phBot's terms dialog, with the real text if phbot.org is reachable.
terms = main.fetch_phbot_terms() or "Terms of Service\n\n(the current text is loaded from phbot.org)"
tdlg = main.widgets.TermsDialog(
    None, "phBot - Terms and Conditions",
    "phBot is third-party software by ProjectHax LLC. It is downloaded from ProjectHax's servers, and "
    "to download and use it you must agree to its Terms and Conditions.",
    terms, "I have read and agree to the Terms and Conditions", "Accept and install",
    skip_label="Continue without phBot", source_label="phbot.org", on_source=lambda: None)
tdlg.resize(720, 520)
tdlg.show()
settle()
save(tdlg.grab().toImage(), "phbot-terms")
tdlg.close()

# phBot options, filled with demo data the way PhbotCdnProbe fills it.
odlg = main.widgets.PhbotOptionsDialog(None, "testing", ["manager", "plugins", "navmesh", "minimap"])
odlg.set_channel_info("testing", {"version": "33.7.9", "changelog": [
    "- Fixed pet summon and revive being repeated before the server responded",
    "- Fixed auto-quest re-accepting a quest right after turning it in",
    "- Added Pet tab to Pick Filter for pet potions and consumables",
    "- Added Reset button to DPS meter"]})
odlg.set_channel_info("stable", {"version": "20.1.1", "changelog": ["- Based on 33.7.8"]})
for key, size in (("full:testing", 72589652), ("full:stable", 70866011), ("manager", 11531048),
                  ("plugins", 17937201), ("navmesh", 23493581), ("minimap", 60162412)):
    odlg.set_size(key, size)
odlg.show()
settle()
save(odlg.grab().toImage(), "phbot-options")
odlg.close()

dlg = main.askpass_dialog("[sudo] password for user:")
dlg.resize(470, dlg.sizeHint().height())
dlg.show()
settle()
save(dlg.grab().toImage(), "password-dialog")
dlg.close()


# ------------------------------------------------------- terminal menu shot
def terminal_shot():
    """sro.sh's control panel, run in a pseudo-terminal and rendered from
    pyte's screen buffer - the same thing a real terminal would show."""
    try:
        import pyte
    except ImportError:
        print("  (skipped terminal-menu: pip install pyte)")
        return
    import fcntl
    import pty
    import select
    import struct
    import termios

    home = TMP / "tui"
    home.mkdir()
    shutil.copy(ROOT / "lib" / "sro-launcher.sh", home / "sro.sh")
    (home / "clients.tsv").write_text("".join("%s\t%s\t%s\n" % c for c in CLIENTS))
    cols, rows = 92, 20
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8",
               COLUMNS=str(cols), LINES=str(rows), HOME=str(TMP))
    proc = subprocess.Popen(["bash", str(home / "sro.sh")], stdin=slave, stdout=slave, stderr=slave,
                            env=env, cwd=str(home), start_new_session=True,
                            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    end = time.time() + 4
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                stream.feed(os.read(master, 65536))
            except OSError:
                break
    proc.kill()
    proc.wait()
    os.close(master)

    palette = {"black": "#0F1419", "red": "#F85149", "green": "#3FB950", "brown": "#D29922",
               "yellow": "#D29922", "blue": "#4C8DFF", "magenta": "#C678DD", "cyan": "#56C7E0",
               "white": "#E6EDF3", "default": None}

    def colour(c, fallback):
        if c in ("default", None):
            return QColor(fallback)
        if c in palette and palette[c]:
            return QColor(palette[c])
        if isinstance(c, str) and len(c) == 6:
            return QColor("#" + c)
        return QColor(fallback)

    font = QFont("DejaVu Sans Mono")
    font.setStyleHint(QFont.Monospace)
    font.setPixelSize(15)
    cw, ch, pad, bar = 9, 19, 18, 30
    img = QImage(cols * cw + 2 * pad, rows * ch + 2 * pad + bar, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    frame = QPainterPath()
    frame.addRoundedRect(0, 0, img.width(), img.height(), 10, 10)
    p.fillPath(frame, QColor("#0B0F14"))
    p.fillRect(QRect(0, 0, img.width(), bar), QColor("#1C232D"))
    for i, c in enumerate(("#F85149", "#D29922", "#3FB950")):
        p.setBrush(QColor(c))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(16 + i * 18, bar // 2), 5, 5)
    p.setPen(QColor("#8B98A8"))
    p.drawText(QRect(0, 0, img.width(), bar), Qt.AlignCenter, "~/.local/share/sro-linux/sro.sh")
    for y in range(rows):
        line = screen.buffer[y]
        for x in range(cols):
            cell = line[x]
            fg, bg = cell.fg, cell.bg
            if cell.reverse:
                fg, bg = bg, fg
            rx, ry = pad + x * cw, bar + pad + y * ch
            if bg not in ("default", None):
                p.fillRect(QRect(rx, ry, cw, ch), colour(bg, "#0B0F14"))
            if cell.data.strip():
                f = QFont(font)
                f.setBold(cell.bold)
                p.setFont(f)
                p.setPen(colour(fg, "#E6EDF3"))
                p.drawText(QRect(rx, ry, cw + 2, ch), Qt.AlignLeft | Qt.AlignVCenter, cell.data)
    p.end()
    save(img, "terminal-menu")


print("Terminal:")
terminal_shot()
shutil.rmtree(TMP, ignore_errors=True)
print("done.")
