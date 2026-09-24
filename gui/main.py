#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""SilkroadWineEnvironment GUI - a thin PySide6 front end around swe.sh / sro.sh.

All actual install/launch/stop logic stays in the bash scripts (single
source of truth - see lib/sro-launcher.sh's --list-json/--stop and swe.sh's
--status-json). This file only drives those scripts and renders their output;
it deliberately does not re-implement any of the Wine/prefix/process-tree
knowledge those scripts already have.

Layout: a sidebar picks one of three pages (Setup / Launch / Manage), a
status panel below them is always visible, and the output log is a drawer
under that which stays closed unless asked for (or unless something fails).
Everything visual lives in theme.py and widgets.py; the progress arithmetic
lives in progress.py.
"""
from __future__ import annotations  # `list[str]`/`X | None` hints need this on Python < 3.10 (e.g. Ubuntu 20.04's stock 3.8)

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import PySide6
from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, QSettings, QSize, qVersion
from PySide6.QtGui import QFont, QColor, QTextCursor, QTextCharFormat, QIcon, QPixmap, QImage, QImageReader
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QPlainTextEdit,
    QFileDialog, QTreeWidget, QTreeWidgetItem, QMessageBox,
    QComboBox, QInputDialog, QScrollArea, QProgressBar,
    QListWidget, QListWidgetItem, QStackedWidget, QFrame, QMenu,
)

import peicon
import theme
import widgets
import progress as prog

# The bash side's own markers (step()'s "### ", say()'s "==> ", run_step()'s
# ">>> STEP/DONE/FAIL") are parsed in progress.py - this file reuses those
# patterns for log colouring rather than defining a second set.
ANSI_RE = re.compile(r'\x1b\[[?0-9;]*[a-zA-Z]')


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


def ci_file_exists(folder: Path, name: str) -> bool:
    """Case-insensitive file check within one directory - Linux filesystems
    are case-sensitive, but real client releases are inconsistent about exe
    casing (confirmed: a real client shipped a lowercase silkroad.exe, which
    a literal Path(folder, "Silkroad.exe").exists() silently missed, hiding
    the Launcher option for that client entirely). Mirrors _ci_file() in
    lib/common.sh, which the bash side uses for the same reason."""
    want = name.lower()
    try:
        return any(f.name.lower() == want for f in folder.iterdir() if f.is_file())
    except OSError:
        return False


def repo_root() -> Path:
    """Directory containing swe.sh, whether run from source or a packaged
    AppImage (AppRun sets SRO_GUI_REPO to where the payload was bundled,
    since PyInstaller's __file__ points inside its own temp bundle, not at
    the actual installer payload)."""
    env = os.environ.get("SRO_GUI_REPO")
    if env and (Path(env) / "swe.sh").exists():
        return Path(env)
    here = Path(__file__).resolve().parent
    if (here / "swe.sh").exists():
        return here
    return here.parent


def icon_path() -> Path | None:
    """gui/icon.png, whether run from source (next to this file) or frozen
    into the AppImage (PyInstaller's --add-data drops it at the root of
    sys._MEIPASS, the bootloader's extracted-bundle dir - not under REPO,
    which for the AppImage points at the separate installer payload, not
    the frozen GUI's own bundle)."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        p = Path(frozen) / "icon.png"
        if p.exists():
            return p
    p = Path(__file__).resolve().parent / "icon.png"
    return p if p.exists() else None


REPO = repo_root()
SWE = REPO / "swe.sh"
SRO_HOME = Path(os.environ.get("SRO_HOME", Path.home() / ".local/share/sro-linux"))
SRO_SH = SRO_HOME / "sro.sh"
CLIENTS_TSV = SRO_HOME / "clients.tsv"

MODES = ["plain", "vsroplus", "maxiguard"]
MODE_LABEL = {"maxiguard": "MaxiGuard", "vsroplus": "vSroPlus", "plain": "Plain"}

# The nine things that actually have to be set up, in the order they are shown.
# Row 10 (the WineD3D toggle) is deliberately NOT in here: it is an opt-in
# fallback, not a missing component, so it must not count against "n of m
# ready".
# Rows that exist only because an opt-in asked for them. Without the
# corresponding checkbox, the Install button will never satisfy these - so
# showing them as plain "missing" made the panel look like there was outstanding
# work that only the Advanced buttons could do, which is what prompted "do I
# have to run some of these by hand?". They are reported as "not selected"
# instead, and left out of the "n of m" count, until the opt-in is on.
MAXIGUARD_ROWS = ("umu", "runtime32", "ge_proton", "maxiguard_prefix")
VSROPLUS_ROWS = ("vsroplus",)

STATUS_ROWS = [
    ("deps", "Build dependencies"),
    ("wine", "wine-sro build"),
    ("umu", "umu-launcher"),
    ("runtime32", "32-bit runtime"),
    ("ge_proton", "GE-Proton (MaxiGuard)"),
    ("maxiguard_prefix", "MaxiGuard prefix"),
    ("vsroplus", "vSroPlus support"),
    ("phbot", "phBot"),
    ("launcher", "Launcher (swe.sh)"),
]


def clean_subprocess_env() -> QProcessEnvironment:
    """Environment for any swe.sh/sro.sh child process - NOT the frozen
    app's own environment as-is.

    When running from the AppImage, PyInstaller's bootloader points
    LD_LIBRARY_PATH at its own bundled libs (an older libssl.so.3 among
    them) so the frozen Python/Qt can find them, and saves the ORIGINAL
    value in LD_LIBRARY_PATH_ORIG for exactly this situation - a child
    process that must NOT inherit it. Without restoring it, swe.sh's calls
    to system tools (curl, apt, the whole Wine build toolchain) pick up the
    bundle's older libssl instead of the system's, e.g. curl failing with
    "OPENSSL_3.2.0 not found" against the system's own newer libcurl -
    confirmed on a real Ubuntu run of the AppImage.
    """
    env = QProcessEnvironment.systemEnvironment()
    orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if orig:
        env.insert("LD_LIBRARY_PATH", orig)
    else:
        env.remove("LD_LIBRARY_PATH")
    # Password prompts for the privileged steps (installing packages): with no
    # terminal attached, _sudo in lib/common.sh uses `sudo -A` with this
    # helper, which shows our own dialog - so it works without a PolicyKit
    # agent too (Crostini / ChromeOS Flex and plain window-manager setups run
    # none, and pkexec fails there). A user's own SUDO_ASKPASS wins.
    askpass = askpass_path()
    if askpass and not env.contains("SUDO_ASKPASS"):
        env.insert("SUDO_ASKPASS", str(askpass))
        if not getattr(sys, "frozen", False):
            env.insert("SRO_GUI_PYTHON", sys.executable)
    return env


def askpass_path() -> Path | None:
    """gui/askpass.sh - next to the frozen binary in the AppImage (copied
    there by build-appimage.sh), next to this file in a source checkout."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    p = base / "askpass.sh"
    return p if os.access(p, os.X_OK) else None


def askpass_dialog(prompt: str) -> QInputDialog:
    """The sudo password dialog - built separately from run_askpass() so
    tools/screenshots.py can render the exact same dialog for the docs."""
    dlg = QInputDialog()
    dlg.setWindowTitle("SilkroadWineEnvironment - administrator password")
    dlg.setLabelText("Installing system packages needs administrator rights (sudo).\n\n" + prompt)
    dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
    return dlg


def run_askpass(app: QApplication) -> int:
    """`--askpass [prompt]`: sudo's password helper. Prints the password on
    stdout (what sudo -A reads) and exits 0, or exits 1 on cancel."""
    prompt = sys.argv[sys.argv.index("--askpass") + 1:] or ["Password:"]
    dlg = askpass_dialog(" ".join(prompt))
    if dlg.exec() != QInputDialog.Accepted:
        return 1
    sys.stdout.write(dlg.textValue() + "\n")
    sys.stdout.flush()
    return 0


def sync_installed_launcher() -> None:
    """Make the installed sro.sh match THIS GUI's payload.

    sro.sh is a copy of lib/sro-launcher.sh made at install time, and this GUI
    drives it through flags (--list-json, --start-phbot, ...). After the user
    swaps in a newer (or older) AppImage, the copy under SRO_HOME would still
    be the old one, so GUI and launcher could disagree about those flags.
    Compared by content, not by a version number, so it can never be
    forgotten to bump; swe.sh --refresh-launcher does the actual rewrite."""
    payload = REPO / "lib" / "sro-launcher.sh"
    try:
        if not SRO_SH.exists() or SRO_SH.read_bytes() == payload.read_bytes():
            return
    except OSError:
        return
    p = QProcess()
    p.setProgram(str(SWE))
    p.setArguments(["--refresh-launcher"])
    p.setProcessEnvironment(clean_subprocess_env())
    p.start()
    p.waitForFinished(30000)


PROJECT_URL = "https://github.com/RealDelirus/SilkroadWineEnvironment"
# phBot's Terms and Conditions: the text phbot.org's own download page loads,
# and that page itself (see phbot_terms_ok() in lib/common.sh for the shell side).
PHBOT_TERMS_URL = "https://phbot.org/static/legal.txt"
PHBOT_TERMS_PAGE = "https://phbot.org/en/download/"
# Public, no token needed: the newest published (non-draft, non-prerelease)
# GitHub release. Unauthenticated calls are limited to 60/h per IP - plenty
# for one check per start.
LATEST_RELEASE_API = "https://api.github.com/repos/RealDelirus/SilkroadWineEnvironment/releases/latest"
AUTHOR = "delirus@delirus.biz"


def app_version() -> str:
    """The version this GUI + payload was built from.

    AppImage: opt/sro-linux/VERSION, stamped by build-appimage.sh from
    `git describe`. Source checkout: `git describe` directly. Otherwise
    "dev"."""
    try:
        v = (REPO / "VERSION").read_text().strip()
        if v:
            return v
    except OSError:
        pass
    p = QProcess()
    p.setProgram("git")
    p.setArguments(["-C", str(REPO), "describe", "--tags", "--always", "--dirty"])
    p.setProcessEnvironment(clean_subprocess_env())
    p.start()
    if p.waitForFinished(3000) and p.exitCode() == 0:
        v = bytes(p.readAllStandardOutput()).decode(errors="replace").strip()
        if v:
            return v
    return "dev"


def version_tuple(v: str):
    """(1, 2, 3) from "v1.2.3" / "v1.2.3-4-gabc" / "1.2.3", None otherwise."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def system_summary() -> str:
    """E.g. "Ubuntu 26.04.1 LTS · x86_64" - for the sidebar footer and bug
    reports. /etc/os-release is on every distro this targets."""
    name = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    return " · ".join(x for x in (name or "Linux", os.uname().machine) if x)


def open_url(url: str) -> None:
    """Open a link in the user's browser.

    Deliberately via xdg-open with clean_subprocess_env() instead of
    QDesktopServices: that one launches the browser with THIS process's
    environment, i.e. the AppImage's LD_LIBRARY_PATH pointing at the bundle's
    older libraries - the same class of breakage clean_subprocess_env()
    exists for (curl failing with "OPENSSL_3.2.0 not found")."""
    p = QProcess()
    p.setProgram("xdg-open")
    p.setArguments([url])
    p.setProcessEnvironment(clean_subprocess_env())
    p.startDetached()


# ------------------------------------------------------------------ shortcuts
# Desktop / application-menu shortcuts for one start action (phBot or the
# Manager for a client type, a saved client's Client or Launcher). They call
# the installed sro.sh's headless start flags directly - the same thing the
# Launch page's buttons do - so they keep working without this GUI running
# and no matter where the AppImage lives (its own mount path is temporary).
SHORTCUT_MARKER = "X-SilkroadWineEnvironment"
SHORTCUT_PREFIX = "silkroadwineenvironment-"


def _desktop_exec_arg(arg: str) -> str:
    """Quote one argument for a .desktop Exec= line, per the Desktop Entry
    spec: reserved characters force double quotes, inside which " ` $ \\
    get a backslash - and since Exec is itself a "string" value whose own
    escape character is the backslash, every such backslash is doubled once
    more. A literal % is written %% (field codes)."""
    arg = arg.replace("%", "%%")
    if arg and not re.search(r'[\s"\'\\><~|&;$*?#()`]', arg):
        return arg
    return '"%s"' % re.sub(r'(["`$\\])', r'\\\\\1', arg)


def shortcut_dirs() -> dict:
    """{"desktop": Path, "menu": Path} - the desktop folder comes from
    xdg-user-dir (localised names like ~/Schreibtisch), falling back to
    ~/Desktop."""
    desktop = ""
    p = QProcess()
    p.setProgram("xdg-user-dir")
    p.setArguments(["DESKTOP"])
    p.setProcessEnvironment(clean_subprocess_env())
    p.start()
    if p.waitForFinished(2000) and p.exitCode() == 0:
        desktop = bytes(p.readAllStandardOutput()).decode(errors="replace").strip()
    if not desktop or Path(desktop) == Path.home():
        desktop = str(Path.home() / "Desktop")
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return {"desktop": Path(desktop), "menu": Path(data) / "applications"}


def shortcut_path(where: str, key: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return shortcut_dirs()[where] / ("%s%s.desktop" % (SHORTCUT_PREFIX, slug))


def shortcut_icon_file(key: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return SRO_HOME / "icons" / (slug + ".png")


def program_icon(key: str, exe_args: list) -> str:
    """The started program's own icon as a PNG path, for its shortcut.

    exe_args go to `sro.sh --exe-path` (phbot | manager <mode> | client
    <folder> | launcher <folder>), which resolves the exe exactly the way the
    matching --start-* flag would; peicon then reads the icon out of that
    exe's resources. Saved under SRO_HOME/icons/ so it outlives the AppImage's
    temporary mount (and goes away with "Uninstall everything"). Returns ""
    if any step fails - the caller falls back to this app's own icon."""
    rc, exe, _ = run_sro_sh(["--exe-path"] + list(exe_args))
    if rc != 0 or not exe:
        return ""
    ico = peicon.exe_icon_ico(exe.splitlines()[-1])
    if not ico:
        return ""
    img = QImage.fromData(ico, "ICO")
    if img.isNull():
        return ""
    dst = shortcut_icon_file(key)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return str(dst) if img.save(str(dst), "PNG") else ""


def create_shortcut(where: str, key: str, name: str, args: list, comment: str,
                    exe_args: list | None = None) -> Path:
    """Write (or overwrite) one shortcut. Raises OSError on failure."""
    path = shortcut_path(where, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The started program's own icon (phBot's, the client's, ...); this app's
    # icon only if that can't be had. Either way a copy under SRO_HOME: the
    # AppImage's own files live on a mount that is gone once it exits.
    icon = program_icon(key, exe_args) if exe_args else ""
    src = icon_path()
    if not icon and src:
        dst = SRO_HOME / "icon.png"
        try:
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                dst.write_bytes(src.read_bytes())
            icon = str(dst)
        except OSError:
            icon = ""
    exec_line = " ".join(_desktop_exec_arg(a) for a in [str(SRO_SH)] + list(args))
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=%s" % name,
        "Comment=%s" % comment,
        "Exec=%s" % exec_line,
        "Terminal=false",
        "Categories=Game;",
        "StartupNotify=false",
        "%s=true" % SHORTCUT_MARKER,
    ]
    if icon:
        lines.insert(4, "Icon=%s" % icon)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    if where == "desktop":
        # GNOME's desktop (and Nautilus) only run a .desktop file that is
        # both executable and explicitly trusted; KDE/Xfce only need +x.
        # Best effort - no gio on the host just means one extra click there.
        g = QProcess()
        g.setProgram("gio")
        g.setArguments(["set", str(path), "metadata::trusted", "true"])
        g.setProcessEnvironment(clean_subprocess_env())
        g.start()
        g.waitForFinished(2000)
    return path


def remove_all_shortcuts() -> int:
    """Delete every shortcut this GUI created (found by its marker line, not
    by name) - used after "Uninstall everything", when they'd all point at an
    sro.sh that no longer exists."""
    n = 0
    for d in shortcut_dirs().values():
        try:
            for f in d.glob(SHORTCUT_PREFIX + "*.desktop"):
                try:
                    if "%s=true" % SHORTCUT_MARKER in f.read_text(errors="replace"):
                        f.unlink()
                        n += 1
                except OSError:
                    pass
        except OSError:
            pass
    return n


def shortcut_menu(parent: QWidget, status_panel, key: str, name: str, args: list, comment: str,
                  menu: QMenu | None = None, title: str = "", exe_args: list | None = None) -> QMenu:
    """Add "Desktop shortcut" / "Application menu" entries for one start
    action to `menu` (a new one if None). Entries for a shortcut that already
    exists say "Update" and get a matching "Remove"."""
    menu = menu if menu is not None else QMenu(parent)
    if title:
        head = menu.addAction(title)
        head.setEnabled(False)
    if not SRO_SH.exists():
        na = menu.addAction("Shortcuts need a finished setup first")
        na.setEnabled(False)
        return menu

    def do_create(where):
        try:
            path = create_shortcut(where, key, name, args, comment, exe_args)
        except OSError as e:
            QMessageBox.warning(parent, "Could not create shortcut", str(e))
            return
        status_panel.flash("done", "Shortcut created", "%s — %s" % (name, path))

    def do_remove(where):
        try:
            shortcut_path(where, key).unlink()
        except OSError:
            pass
        # Its extracted program icon is shared by the desktop and the menu
        # entry - drop it once neither is left.
        if not any(shortcut_path(w, key).exists() for w in ("desktop", "menu")):
            try:
                shortcut_icon_file(key).unlink()
            except OSError:
                pass
        status_panel.flash("done", "Shortcut removed", name)

    for where, label, icon_name in (("desktop", "desktop shortcut", "folder"),
                                    ("menu", "application menu entry", "plus")):
        exists = shortcut_path(where, key).exists()
        act = menu.addAction(theme.icon(icon_name, theme.TEXT_MUTED, 14),
                             ("Update " if exists else "Create ") + label)
        act.triggered.connect(lambda _checked=False, w=where: do_create(w))
        if exists:
            rm = menu.addAction(theme.icon("trash", theme.DANGER, 14), "Remove " + label)
            rm.triggered.connect(lambda _checked=False, w=where: do_remove(w))
    return menu


def show_licenses(parent: QWidget) -> None:
    """This app's license and the notices for everything the AppImage
    bundles (THIRD-PARTY-NOTICES.txt is written next to the frozen binary
    by build-appimage.sh; a source checkout has none)."""
    def read(path: Path, missing: str) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return missing
    notices = (Path(sys.executable).parent / "THIRD-PARTY-NOTICES.txt") if getattr(sys, "frozen", False) else None
    widgets.TextTabsDialog(
        parent, "Licenses",
        "SilkroadWineEnvironment is free software under the GNU General Public License, "
        "version 3 or later. The Wine patches it applies are LGPL-2.1-or-later, like Wine "
        "itself. phBot is not part of it - it is downloaded from ProjectHax's servers under ProjectHax's "
        "own terms.",
        [("SilkroadWineEnvironment (GPL-3.0)", read(REPO / "LICENSE", "LICENSE not found - see " + PROJECT_URL)),
         ("Third-party notices", read(notices, "THIRD-PARTY-NOTICES.txt is missing from this build.") if notices else
          "The list of bundled third-party components is part of the AppImage build "
          "(THIRD-PARTY-NOTICES.txt) - this is a source checkout, which bundles nothing.")],
    ).exec()


def fetch_phbot_terms() -> str:
    """phBot's current Terms and Conditions text, or "" if it can't be had.
    Through curl for the same reason as UpdateCheck (TLS on old hosts)."""
    p = QProcess()
    p.setProgram("curl")
    p.setArguments(["-fsSL", "--max-time", "15", PHBOT_TERMS_URL])
    p.setProcessEnvironment(clean_subprocess_env())
    p.start()
    if not p.waitForFinished(20000) or p.exitCode() != 0:
        p.kill()
        return ""
    return bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace").strip()


# Where phBot is downloaded from (lib/phbot-fetch.py reads the same variable).
PHBOT_CDN = os.environ.get("SRO_PHBOT_CDN", "https://cdn.projecthax.com").rstrip("/")


class PhbotCdnProbe:
    """Fills a PhbotOptionsDialog while it is open: both channels'
    update.json (version, changelog) and each package's download size.

    Through curl, like UpdateCheck. Every request is independent and silent
    on failure - the dialog shows whatever arrived, and the install itself
    does not depend on any of it."""

    def __init__(self, dialog: widgets.PhbotOptionsDialog):
        self.dialog = dialog
        self._procs: list[QProcess] = []
        self._sized: set = set()

    def start(self):
        for ch in ("testing", "stable"):
            self._run(["-fsSL", "--max-time", "15", "%s/%s/update.json" % (PHBOT_CDN, ch)],
                      lambda ok, out, ch=ch: self._manifest(ch, ok, out))
        self._run(["-fsSL", "--max-time", "15", "%s/manager2/update.json" % PHBOT_CDN], self._manager)

    def stop(self):
        for p in self._procs:
            p.kill()
            p.waitForFinished(1000)
        self._procs = []

    def _run(self, args, callback):
        p = QProcess()
        p.setProgram("curl")
        p.setArguments(args)
        p.setProcessEnvironment(clean_subprocess_env())
        p.finished.connect(lambda code, _status, p=p: self._finished(p, code, callback))
        self._procs.append(p)
        p.start()

    def _finished(self, p, code, callback):
        if p not in self._procs:
            return                          # stopped - the dialog is gone
        self._procs.remove(p)
        out = bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace")
        callback(code == 0, out)

    @staticmethod
    def _json(ok, out):
        if not ok:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _manifest(self, channel, ok, out):
        data = self._json(ok, out)
        if not data or not data.get("version"):
            self.dialog.set_channel_info(channel, {"error": "no answer from %s" % PHBOT_CDN})
            return
        self.dialog.set_channel_info(channel, data)
        if data.get("full"):
            self._size("full:" + channel, data["full"])
        for key, field in (("plugins", "python"), ("navmesh", "navmesh"), ("minimap", "minimap")):
            if data.get(field):
                self._size(key, data[field])

    def _manager(self, ok, out):
        data = self._json(ok, out) or {}
        for f in data.get("win") or []:
            if str(f.get("name", "")).lower() == "manager.exe" and f.get("size"):
                self.dialog.set_size("manager", int(f["size"]))

    def _size(self, key, url):
        if key in self._sized:
            return
        self._sized.add(key)
        self._run(["-sSIL", "--max-time", "15", url],
                  lambda ok, out, key=key: self._head(key, out))

    def _head(self, key, out):
        # -L prints one header block per redirect hop; the last one is the file.
        size, status = 0, ""
        for line in out.replace("\r", "").split("\n"):
            if line.upper().startswith("HTTP/"):
                parts = line.split()
                status, size = (parts[1] if len(parts) > 1 else ""), 0
            elif line.lower().startswith("content-length:"):
                try:
                    size = int(line.split(":", 1)[1].strip())
                except ValueError:
                    size = 0
        if status == "200" and size:
            self.dialog.set_size(key, size)


class UpdateCheck:
    """One asynchronous look at the latest GitHub release.

    Through curl (like everything else that touches the network here) rather
    than QtNetwork or Python's ssl: Qt 6.6 dlopen()s OpenSSL 3, which the
    oldest supported hosts don't have, and the bundled Python 3.8's OpenSSL
    only knows Debian's CA path, not Fedora's or Arch's. Silent on any
    failure (offline, API change, ...); SRO_NO_UPDATE_CHECK=1 turns it off."""

    def __init__(self, current: str, on_newer):
        self._current = version_tuple(current)
        self._on_newer = on_newer
        self._proc: QProcess | None = None

    def start(self):
        if self._current is None or os.environ.get("SRO_NO_UPDATE_CHECK") == "1":
            return
        p = QProcess()
        p.setProgram("curl")
        p.setArguments(["-fsSL", "--max-time", "8",
                        "-H", "Accept: application/vnd.github+json", LATEST_RELEASE_API])
        p.setProcessEnvironment(clean_subprocess_env())
        p.finished.connect(self._done)
        self._proc = p
        p.start()

    def stop(self):
        """Called when the window closes - a curl still in flight would
        otherwise be torn down with Qt's "Destroyed while process is still
        running" warning."""
        p, self._proc = self._proc, None
        if p is not None and p.state() != QProcess.NotRunning:
            p.kill()
            p.waitForFinished(1000)

    def _done(self, code, _status):
        p, self._proc = self._proc, None
        if p is None or code != 0:
            return
        try:
            tag = json.loads(bytes(p.readAllStandardOutput()).decode(errors="replace")).get("tag_name", "")
        except (ValueError, AttributeError):
            return
        latest = version_tuple(tag)
        if latest and latest > self._current:
            self._on_newer(tag)


def run_sro_sh(args: list[str], timeout_ms: int = 15000) -> tuple[int, str, str]:
    """Synchronous helper for the short, non-interactive sro.sh flags
    (--modes-json, --detect-client, --add-client, --start-phbot, ...). All
    of these reuse the exact same Wine/prefix/process-tree logic as the
    terminal control panel - this file never re-derives any of that itself."""
    if not SRO_SH.exists():
        return 1, "", "sro.sh not set up yet - run Setup first."
    p = QProcess()
    p.setProgram(str(SRO_SH))
    p.setArguments(args)
    p.setProcessEnvironment(clean_subprocess_env())
    p.start()
    p.waitForFinished(timeout_ms)
    out = bytes(p.readAllStandardOutput()).decode(errors="replace")
    # stderr always carries the exit trap's cursor-restore escape codes (see
    # lib/sro-launcher.sh's _sro_exit_cleanup) on top of any real message -
    # strip them so a "did not start" dialog doesn't show garbled tail bytes.
    err = strip_ansi(bytes(p.readAllStandardError()).decode(errors="replace"))
    return p.exitCode(), out.strip(), err.strip()


def run_sro_sh_async(args: list[str], on_done, parent: QWidget | None = None) -> QProcess | None:
    """Non-blocking counterpart to run_sro_sh(), for the Launch page's start
    actions specifically - those can take a real moment (a first-run Wine
    prefix bootstrap especially), and run_sro_sh()'s synchronous
    waitForFinished() freezes the whole window with zero visual feedback for
    that whole time, which is exactly what showed up as "did my click even
    register?" after pressing e.g. Start phBot. on_done(rc, out, err) fires
    when the process exits. Returns the QProcess so the caller can hold a
    reference (a QProcess with no owner still alive when this function
    returns would otherwise get garbage-collected mid-run)."""
    if not SRO_SH.exists():
        on_done(1, "", "sro.sh not set up yet - run Setup first.")
        return None
    proc = QProcess(parent)
    proc.setProgram(str(SRO_SH))
    proc.setArguments(args)
    proc.setProcessEnvironment(clean_subprocess_env())

    def _finished(code, _status):
        out = bytes(proc.readAllStandardOutput()).decode(errors="replace").strip()
        err = strip_ansi(bytes(proc.readAllStandardError()).decode(errors="replace")).strip()
        on_done(code, out, err)

    proc.finished.connect(_finished)
    proc.start()
    return proc


class LogView(QPlainTextEdit):
    """The shared output log, with the bash side's own markers highlighted.

    The scripts already structure their output (step()'s "### ", say()'s
    "==> ", and run_step()'s ">>> STEP/DONE/FAIL" boundaries); colouring
    those is what turns a wall of compiler noise into something skimmable
    now that the log is a drawer people open on purpose.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("logView")
        self.setReadOnly(True)
        self.setFont(theme.mono_font(9))
        self.setMaximumBlockCount(5000)
        self._pending = ""

    # -- formatting -------------------------------------------------------
    @staticmethod
    def _format_for(line: str):
        """(display text, QTextCharFormat) for one complete line - or None
        for a line that is only there to drive the progress bars."""
        fmt = QTextCharFormat()
        # phBot's download/unpack progress arrives once a second; the status
        # panel shows it live. The log keeps one line per finished file.
        m = prog.DL_RE.match(line)
        if m:
            if m.group("pct") != "100":
                return None
            fmt.setForeground(QColor(theme.TEXT_MUTED))
            return "  ↓ %s  %s MB  (%s)" % (m.group("name"), m.group("got"), m.group("rate")), fmt
        m = prog.EXTRACT_RE.match(line)
        if m:
            if m.group("pct") != "100":
                return None
            fmt.setForeground(QColor(theme.TEXT_MUTED))
            return "  ⇲ %s  %s files unpacked" % (m.group("name"), m.group("of")), fmt
        m = prog.STEP_RE.match(line)
        if m:
            fmt.setForeground(QColor(theme.ACCENT))
            fmt.setFontWeight(QFont.Bold)
            return "\n▸ " + m.group(1), fmt
        m = prog.DONE_RE.match(line)
        if m:
            fmt.setForeground(QColor(theme.OK))
            return "  ✓ " + m.group(1), fmt
        m = prog.FAIL_RE.match(line)
        if m:
            fmt.setForeground(QColor(theme.DANGER))
            fmt.setFontWeight(QFont.Bold)
            return "  ✗ %s  (Exit %s)" % (m.group(2), m.group(1)), fmt
        m = prog.PHASE_RE.match(line)
        if m:
            fmt.setForeground(QColor(theme.ACCENT_HOVER))
            fmt.setFontWeight(QFont.Bold)
            return "  " + m.group(1), fmt

        low = line.lower()
        if line.startswith("XX ") or "error" in low or "failed" in low:
            fmt.setForeground(QColor(theme.DANGER))
        elif line.startswith("!!") or "warning" in low or "warn:" in low:
            fmt.setForeground(QColor(theme.WARN))
        elif line.startswith("==>"):
            fmt.setForeground(QColor(theme.OK))
        elif line.startswith("$ "):
            fmt.setForeground(QColor(theme.TEXT))
            fmt.setFontWeight(QFont.Bold)
        else:
            fmt.setForeground(QColor(theme.TEXT_MUTED))
        return line, fmt

    # -- input ------------------------------------------------------------
    def append_line(self, line: str):
        formatted = self._format_for(line)
        if formatted is None:
            return
        text, fmt = formatted
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        if at_bottom:
            sb.setValue(sb.maximum())

    def append_text(self, text: str):
        """Append a chunk of output, buffering any incomplete trailing line.

        QProcess hands over arbitrary chunks that routinely end mid-line. The
        old insertPlainText() did not care, but per-line colouring does: an
        unbuffered chunk would split a "### phase" marker across two writes
        and lose its highlight.
        """
        text = strip_ansi(text)
        if not text:
            return
        buf = self._pending + text
        lines = buf.split("\n")
        self._pending = lines.pop()
        for line in lines:
            self.append_line(line.rstrip("\r"))

    def flush(self):
        if self._pending:
            self.append_line(self._pending.rstrip("\r"))
            self._pending = ""

    def appendPlainText(self, text: str):
        """Overridden so existing callers keep working AND get coloured.

        The base class would insert the text unformatted, which would make
        every message from the Launch/Manage pages the one uncoloured thing
        in the log.
        """
        for line in str(text).split("\n"):
            self.append_line(line)


class SetupTab(QWidget):
    def __init__(self, log: LogView, status_panel, on_status_changed, on_open_launch=None):
        super().__init__()
        self.log = log
        self.status_panel = status_panel
        self.on_status_changed = on_status_changed
        self.on_open_launch = on_open_launch
        self._last_args: list[str] = []
        self.proc: QProcess | None = None
        self.tracker: prog.RunTracker | None = None
        self._line_buf = ""
        self._run_started = 0.0
        self._status = {}
        self._status_proc: QProcess | None = None
        self._status_ready = False
        # Label of an Install that ran its build deps first and continues
        # once they are in (see start_install()).
        self._resume_install: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # The status panel + guided-setup form go inside a scroll area: on a
        # tiling window manager (common on the CachyOS/Arch-family default
        # this project targets), the window is not necessarily given the
        # size resize() asks for, and without a scroll area anything below
        # the status panel (the actual folder picker / Install button) can
        # end up genuinely unreachable rather than just visually cramped -
        # reported as "the whole install part is missing" from the Setup tab.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_body = QWidget()
        scroll_layout = QVBoxLayout(scroll_body)
        scroll_layout.setContentsMargins(20, 18, 20, 18)
        scroll_layout.setSpacing(12)

        # action_buttons: every button that starts a swe.sh run - all get
        # disabled together while ONE is in flight, since only one swe.sh
        # process should ever run at a time against the same install.
        self.action_buttons: list[QPushButton] = []

        # Install options: MaxiGuard and vSroPlus are separate things to opt
        # into - MaxiGuard needs its own curated GE-Proton download, vSroPlus
        # its own wine-sro prefix. Both default to ON (remembered in
        # QSettings once the user changes them) and live in the collapsed
        # Advanced section below, so the top of the page stays one clear
        # action; the hero's "Includes: ..." line still says they are part
        # of the next Install. Neither shows up anywhere in Launch until it
        # has actually been set up once (see _maxiguard_available()/
        # _vsroplus_available() in sro-launcher.sh).
        settings = QSettings()
        self.maxiguard_check = QCheckBox("MaxiGuard support (extra ~150 MB download)")
        self.maxiguard_check.setChecked(settings.value("setup/maxiguard", True, type=bool))
        self.vsroplus_check = QCheckBox("vSroPlus support")
        self.vsroplus_check.setChecked(settings.value("setup/vsroplus", True, type=bool))
        # toggled -> repaint (fires for programmatic changes too, so the grid
        # always matches the boxes); clicked -> remember the choice (fires
        # ONLY for the user's own clicks, so reflecting an installed option
        # as checked never overwrites what the user picked).
        for cb, key in ((self.maxiguard_check, "setup/maxiguard"),
                        (self.vsroplus_check, "setup/vsroplus")):
            cb.toggled.connect(lambda _checked=False: self.render_status_rows())
            cb.clicked.connect(lambda checked=False, k=key: QSettings().setValue(k, bool(checked)))
        # phBot's channel and optional packages are picked in their own
        # window (widgets.PhbotOptionsDialog) that opens whenever a run is
        # about to download phBot - see _phbot_options().

        # THE thing this tab is for, and the first thing shown: where the
        # install stands and the one action that follows from it. The button
        # label comes from the real status (see render_status_rows()):
        # "Install" (nothing usable yet), "Continue install" (something is
        # there, something still missing), "Open Launch" once everything is
        # set up - with "Re-run setup" as a quiet link for verifying/repairing.
        # Mirrors the terminal wizard's first-run wizard vs. "Continue - set
        # up everything missing".
        self.hero = widgets.SetupHero()
        self.install_btn = self.hero.primary
        self.install_btn.clicked.connect(self._primary_clicked)
        self.rerun_btn = self.hero.link
        self.rerun_btn.clicked.connect(lambda _checked=False: self.start_install("Re-run setup"))
        self.cancel_btn = self.hero.cancel
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_run)
        self.action_buttons += [self.install_btn, self.rerun_btn]
        self._hero_kind = "checking"
        scroll_layout.addWidget(self.hero)

        # Current status: every component as a small tile, grouped by what it
        # belongs to. Numbered as before (1..10, the STATUS_ROWS order) and
        # reused as-is in the Advanced buttons' labels below (e.g. "6.
        # Recreate MaxiGuard prefix"), so it's obvious which button affects
        # which component.
        status_box = widgets.Card()
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("Current status")
        title.setFont(theme.ui_font(11, bold=True))
        head.addWidget(title)
        head.addStretch(1)
        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "muted")
        self.summary_label.setFont(theme.ui_font(9))
        head.addWidget(self.summary_label)
        self.summary_bar = QProgressBar()
        self.summary_bar.setTextVisible(False)
        self.summary_bar.setFixedSize(90, 4)
        self.summary_bar.setRange(0, len(STATUS_ROWS))
        self.summary_bar.setValue(0)
        head.addWidget(self.summary_bar)
        refresh_btn = QPushButton()
        refresh_btn.setProperty("variant", "ghost")
        refresh_btn.setIcon(theme.icon("refresh", theme.TEXT_MUTED, 15))
        refresh_btn.setToolTip("Refresh status")
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_status)
        head.addWidget(refresh_btn)
        status_box.add(head)

        number = {key: i for i, (key, _) in enumerate(STATUS_ROWS, start=1)}
        full = dict(STATUS_ROWS)

        def item(key, short):
            return (key, number[key], short, full[key])

        self.status_grid = widgets.StatusGrid([
            [("core", "Core", [item("deps", "Build deps"), item("wine", "wine-sro"),
                               item("phbot", "phBot"), item("launcher", "Launcher")])],
            [("maxiguard", "MaxiGuard", [item("umu", "umu-launcher"), item("runtime32", "32-bit runtime"),
                                         item("ge_proton", "GE-Proton"),
                                         item("maxiguard_prefix", "Prefix")])],
            [("vsroplus", "vSroPlus", [item("vsroplus", "Support")]),
             # WineD3D is an opt-in TOGGLE (for VMs without a working Vulkan
             # driver), not a requirement - its own group so its default
             # "off" never reads as something missing, matching the terminal
             # panel's "optional (off)" wording for it.
             ("optional", "Optional", [("wined3d", len(STATUS_ROWS) + 1, "WineD3D fallback",
                                        "Force WineD3D (no Vulkan)")])],
        ])
        status_box.add(self.status_grid)
        scroll_layout.addWidget(status_box)

        # Individual steps - mirror the terminal control panel's "Expert
        # settings" menu (build deps only, rebuild wine-sro, MaxiGuard
        # environment on its own, phBot on its own, recreate a broken
        # MaxiGuard prefix, the WineD3D fallback toggle for GPU-less VMs).
        adv_box = self._section_body()
        adv_layout = adv_box.layout()
        opt_title = QLabel("INSTALL OPTIONS")
        opt_title.setFont(theme.ui_font(8, bold=True))
        opt_title.setProperty("role", "dim")
        adv_layout.addWidget(opt_title)
        adv_layout.addWidget(self.maxiguard_check)
        adv_layout.addWidget(self.vsroplus_check)
        adv_layout.addSpacing(6)
        adv_layout.addWidget(widgets.hline())
        steps_title = QLabel("INDIVIDUAL STEPS")
        steps_title.setFont(theme.ui_font(8, bold=True))
        steps_title.setProperty("role", "dim")
        adv_layout.addWidget(steps_title)
        adv_steps = [
            ("1. Install build dependencies", ["--deps-only"], None),
            ("2. Build / rebuild wine-sro (~20-60 min)", ["--skip-deps", "--wine-only"], None),
            # 3-6, not "5+6": setup_maxiguard also runs ensure_umu and
            # ensure_32bit_runtime, i.e. rows 3 and 4 come with it.
            ("3-6. Set up MaxiGuard environment (no client folder)",
             ["--skip-deps", "--skip-wine", "--maxiguard"], None),
            ("6. Recreate MaxiGuard prefix",
             ["--skip-deps", "--skip-wine", "--recreate-maxiguard-prefix"],
             "This deletes the existing shared MaxiGuard prefix and creates it fresh.\nContinue?"),
            ("7. Set up vSroPlus support (no client folder)",
             ["--skip-deps", "--skip-wine", "--vsroplus"], None),
            ("10. Toggle: force WineD3D (VMs without Vulkan)", ["--toggle-wined3d"], None),
        ]
        for text, args, confirm in adv_steps:
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked=False, a=args, c=confirm, t=text: self.run_step(a, t, c))
            adv_layout.addWidget(btn)
            self.action_buttons.append(btn)
        # Separate from the static adv_steps list above: it opens the phBot
        # options window first, and when phBot is already there it becomes
        # an update (downloaded again, configs kept).
        phbot_btn = QPushButton("8. Set up / update phBot (pick channel and components)")
        phbot_btn.clicked.connect(self.setup_phbot)
        adv_layout.addWidget(phbot_btn)
        self.action_buttons.append(phbot_btn)
        scroll_layout.addWidget(widgets.CollapsibleSection(
            "Advanced - install options and individual steps", adv_box))

        # Patching a single Silkroad client folder (registers/patches just
        # that one folder, no phBot involved) is the least-used action here -
        # kept last, and separate from the main Install/Continue flow above,
        # so it never has to be filled in just to get the environment set up.
        client_box = self._section_body()
        client_layout = client_box.layout()
        hint = QLabel("Client folder (or a folder with several):")
        hint.setProperty("role", "muted")
        client_layout.addWidget(hint)
        client_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("/path/to/silkroad-client")
        browse_btn = QPushButton("Browse …")
        browse_btn.setIcon(theme.icon("folder", theme.TEXT_MUTED, 15))
        browse_btn.clicked.connect(self.browse_folder)
        client_row.addWidget(self.folder_edit, 1)
        client_row.addWidget(browse_btn)
        client_layout.addLayout(client_row)
        self.setup_client_btn = QPushButton("Set up this client folder")
        self.setup_client_btn.clicked.connect(self.setup_client_folder)
        client_layout.addWidget(self.setup_client_btn)
        self.action_buttons.append(self.setup_client_btn)
        scroll_layout.addWidget(widgets.CollapsibleSection(
            "Client setup (patch a single Silkroad client folder)", client_box))

        # Undo actions - kept last and visually separated since they're
        # destructive and rarely needed, but should still be easy to find
        # rather than requiring a terminal.
        remove_box = self._section_body()
        remove_layout = remove_box.layout()
        remove_mg_btn = QPushButton("Remove MaxiGuard support")
        remove_mg_btn.setProperty("variant", "danger")
        remove_mg_btn.setIcon(theme.icon("trash", theme.DANGER, 15))
        remove_mg_btn.clicked.connect(lambda: self.run_step(
            ["--remove-maxiguard"], "Remove MaxiGuard support",
            "Removes the shared MaxiGuard prefix, the phBot-under-MaxiGuard launcher, and the\n"
            "patched GE-Proton copy this installer made. Your own GE-Proton install (if any) is\n"
            "kept. Saved MaxiGuard clients stay listed but won't start until set up again.\n\nContinue?"
        ))
        remove_layout.addWidget(remove_mg_btn)
        self.action_buttons.append(remove_mg_btn)
        uninstall_btn = QPushButton("Uninstall everything")
        uninstall_btn.setProperty("variant", "danger")
        uninstall_btn.setFont(theme.ui_font(10, bold=True))
        uninstall_btn.setIcon(theme.icon("power", theme.DANGER, 15))
        uninstall_btn.clicked.connect(self.confirm_uninstall)
        remove_layout.addWidget(uninstall_btn)
        self.action_buttons.append(uninstall_btn)
        scroll_layout.addWidget(widgets.CollapsibleSection("Remove", remove_box))
        scroll_layout.addStretch(1)

        # No stretch here (0, the default) - the scroll area then sizes
        # itself to its content's natural height instead of splitting space
        # evenly with whatever is below, so on a big-enough window (see
        # MainWindow's resize()) status + guided setup + advanced steps are
        # all visible without any scrolling at all, and only shrink into a
        # scrollbar on a window too small to fit everything.
        #
        # QScrollArea's OWN sizeHint does NOT follow its content by default
        # (it's a small fixed-ish size regardless of what's inside), so
        # without this the layout would still squeeze it down to near-
        # nothing - setting its minimum height to the content's actual
        # sizeHint is what makes "big enough window -> no scrolling" true in
        # practice, not just in theory.
        #
        # Capped, though: the redesign's card padding makes that sizeHint
        # considerably taller than the old flat layout's, and an uncapped
        # minimum would push the window's own minimum size past 480px high
        # and re-create the very bug this trick exists to fix.
        scroll.setWidget(scroll_body)
        scroll.setMinimumHeight(min(scroll_body.sizeHint().height() + 4, 420))
        layout.addWidget(scroll)

        # Progress is polled rather than purely event-driven: the wine build's
        # `make` writes to a log file, not to stdout, so there is no output
        # event to hang the update off (see progress.WineBuildProbe).
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._tick)

        # Nothing may be started before the first status lands: start_install()
        # derives its --skip-* flags from it, and the poll is asynchronous now.
        for b in self.action_buttons:
            b.setEnabled(False)
        QTimer.singleShot(200, self.refresh_status)

    @staticmethod
    def _section_body() -> QWidget:
        """Body panel for a CollapsibleSection - squared off at the top so it
        reads as one piece with the header button above it."""
        w = QFrame()
        w.setObjectName("card")
        w.setProperty("joined", "top")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        return w

    def browse_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select your Silkroad client folder")
        if d:
            self.folder_edit.setText(d)

    def refresh_status(self, restart: bool = False):
        """Poll swe.sh --status-json without blocking the UI.

        Used to be synchronous (waitForFinished), which froze the window for
        about a second on every call. That ruled out calling it DURING a run,
        so the status rows only ever caught up once everything had finished -
        the one moment the user no longer needs them. Now it is fired again at
        every step boundary, so the rows fill in as the install progresses.

        restart=True drops a poll still in flight and starts a fresh one - for
        when the answer must reflect a run that has only just finished.
        """
        if self._status_proc is not None and self._status_proc.state() != QProcess.NotRunning:
            if not restart:
                return                  # one in flight is enough
            old, self._status_proc = self._status_proc, None
            old.finished.disconnect()
            old.kill()
            old.waitForFinished(1000)
        p = QProcess(self)
        p.setProgram(str(SWE))
        p.setArguments(["--status-json"])
        p.setProcessEnvironment(clean_subprocess_env())
        p.finished.connect(self._apply_status)
        self._status_proc = p
        p.start()

    def _apply_status(self, *_):
        p, self._status_proc = self._status_proc, None
        if p is None:
            return
        try:
            out = bytes(p.readAllStandardOutput()).decode(errors="replace").strip()
        except RuntimeError:
            # The window closed while this poll was still in flight, so Qt
            # deleted the QProcess out from under its Python wrapper. Nothing
            # left to apply.
            return
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            self.log.append_text("(status check failed: %r)\n" % out)
            self._resume_install = None
            return
        self.apply_status(data)
        if self._resume_install and not self._busy():
            label, self._resume_install = self._resume_install, None
            if data.get("deps"):
                self.start_install(label)
            else:
                self.log.appendPlainText("Build dependencies are still missing - install stopped here.")

    def apply_status(self, data: dict):
        """Take one --status-json result: sync the options, repaint the grid
        and the hero. Split from _apply_status() so it can be driven with a
        prepared dict too (screenshots/tests), not only from a real poll."""
        self._status = data
        if not self._status_ready:
            self._status_ready = True
            for b in self.action_buttons:
                b.setEnabled(True)
        # A run in flight owns the form: re-checking the boxes from a mid-run
        # poll would fight whatever the user picked before pressing Install.
        if not self._busy():
            # Reflect reality: an option that is already set up shows checked
            # (unchecking it only skips RE-verifying it on the next click -
            # setup_maxiguard/setup_vsroplus are additive, so nothing gets
            # removed; "Remove MaxiGuard support" in Remove is for that). An
            # option that is NOT set up keeps the user's own choice (default
            # on) instead of being unchecked here.
            for cb, key in ((self.maxiguard_check, "maxiguard"), (self.vsroplus_check, "vsroplus")):
                if data.get(key) and not cb.isChecked():
                    cb.setChecked(True)
        self.render_status_rows()

    def _busy(self) -> bool:
        return bool(self.proc and self.proc.state() != QProcess.NotRunning)

    def render_status_rows(self):
        """Paint the status grid and the hero from the last poll plus the
        current opt-ins.

        Also called when an option is toggled, so ticking "MaxiGuard" turns
        its four tiles from "off" into "missing" straight away - i.e. the page
        shows what the next Install click will actually cover.
        """
        data = self._status
        if not data:
            return
        want_mg = self.maxiguard_check.isChecked() or bool(data.get("maxiguard"))
        want_vs = self.vsroplus_check.isChecked() or bool(data.get("vsroplus"))
        done = relevant = 0
        missing = []
        for key, text in STATUS_ROWS:
            ok = bool(data.get(key))
            wanted = not ((key in MAXIGUARD_ROWS and not want_mg)
                          or (key in VSROPLUS_ROWS and not want_vs))
            if ok:
                self.status_grid.set_state(key, "ok")
                done += 1
                relevant += 1
            elif wanted:
                self.status_grid.set_state(key, "missing")
                relevant += 1
                missing.append(text)
            else:
                self.status_grid.set_state(key, "off", "not selected")
        self.status_grid.set_state("wined3d", "on" if data.get("wined3d_forced") else "off",
                                   "on" if data.get("wined3d_forced") else "optional (off)")
        self.status_grid.set_note("maxiguard", "" if want_mg else "not selected")
        self.status_grid.set_note("vsroplus", "" if want_vs else "not selected")
        self.summary_bar.setRange(0, max(1, relevant))
        self.summary_bar.setValue(done)
        self.summary_bar.setProperty("bar", "ok" if done >= relevant else "")
        widgets.repolish(self.summary_bar)
        self.summary_label.setText("%d/%d ready" % (done, relevant))
        self._render_hero(data, done, relevant, missing, want_mg, want_vs)
        self.on_status_changed(data, done, relevant)

    def _render_hero(self, data, done, relevant, missing, want_mg, want_vs):
        if self._busy():
            self._hero_kind = "running"
            self.hero.set_state("running", "Installing …",
                                "Progress and details are shown below.", "Running …", "loader")
            return
        channel = data.get("phbot_channel") or "testing"
        # "Install" vs. "Continue install": same "has anything usable been
        # set up yet" test as before - build deps alone (often already on
        # the system) do not count as a started install.
        started = bool(data.get("wine") or data.get("phbot") or data.get("launcher"))
        if done >= relevant:
            self._hero_kind = "complete"
            parts = []
            wine = (data.get("wine_version") or "").replace("wine-", "")
            if wine:
                parts.append("wine-sro " + wine)
            parts.append(" ".join(x for x in ("phBot", channel, data.get("phbot_version")) if x))
            if data.get("maxiguard"):
                parts.append("MaxiGuard")
            if data.get("vsroplus"):
                parts.append("vSroPlus")
            self.hero.set_state("complete", "Everything is set up", " · ".join(parts),
                                "Open Launch", "arrow-right", show_link=True)
        elif started:
            self._hero_kind = "partial"
            n = relevant - done
            detail = "Missing: " + ", ".join(missing) if n <= 3 else \
                "%d of %d components are still missing." % (n, relevant)
            self.hero.set_state("partial", "Setup incomplete", detail,
                                "Continue install", "play")
        else:
            self._hero_kind = "fresh"
            parts = ["build deps", "wine-sro", "phBot (%s)" % channel]
            if want_mg:
                parts.append("MaxiGuard")
            if want_vs:
                parts.append("vSroPlus")
            self.hero.set_state("fresh", "Ready to install",
                                "Includes: " + " · ".join(parts) + ". Change in Advanced below.",
                                "Install", "play")

    def _primary_clicked(self, _checked=False):
        if self._hero_kind == "complete":
            if self.on_open_launch:
                self.on_open_launch()
            return
        self.start_install(self.install_btn.text())

    def start_install(self, label: str = "Install"):
        # Skip whatever refresh_status() already found done, so clicking
        # this again later never re-triggers a 20-60 min wine-sro rebuild or
        # a redundant package-manager run just because it already succeeded.
        status = self._status
        if not status.get("deps") and not status.get("phbot"):
            # The phBot dialogs below load the changelog and the terms through
            # curl, which a fresh system may only get with the build deps. So
            # every root step runs first (MaxiGuard's prerequisites too - one
            # password prompt), and the install continues from _apply_status()
            # once the status poll after it confirms the deps are in.
            args = ["--deps-only"]
            if self.maxiguard_check.isChecked():
                args.append("--maxiguard")
            self.run_step(args, label)
            if self._busy():
                self._resume_install = label
            return
        args = []
        if status.get("deps"):
            args.append("--skip-deps")
        if status.get("wine"):
            args.append("--skip-wine")
        args.append("--phbot")
        if not status.get("phbot"):
            # phBot is going to be downloaded in this run - ask what exactly.
            opts = self._phbot_options(reinstall=False)
            if opts is None:
                return
            args += opts
        if self.maxiguard_check.isChecked():
            args.append("--maxiguard")
        if self.vsroplus_check.isChecked():
            args.append("--vsroplus")
        self.run_step(args, label)

    def setup_phbot(self):
        installed = bool(self._status.get("phbot"))
        opts = self._phbot_options(reinstall=installed)
        if opts is None:
            return
        args = ["--skip-deps", "--skip-wine", "--phbot"] + opts
        if installed:
            args.append("--phbot-reinstall")
        self.run_step(args, "Update phBot" if installed else "Set up phBot")

    def _phbot_options(self, reinstall: bool) -> list[str] | None:
        """Open the phBot options window. Returns the swe.sh arguments for
        the choice, or None when cancelled."""
        data = self._status
        comps = data.get("phbot_components")
        if comps is None:
            comps = "manager,plugins,navmesh,minimap"
        installed = ""
        if data.get("phbot"):
            installed = " ".join(x for x in ("phBot", data.get("phbot_version"),
                                             "(%s)" % (data.get("phbot_channel") or "testing")) if x)
        dlg = widgets.PhbotOptionsDialog(
            self, data.get("phbot_channel") or "testing",
            [c for c in comps.split(",") if c], installed=installed, reinstall=reinstall)
        probe = PhbotCdnProbe(dlg)
        probe.start()
        try:
            dlg.exec()
        finally:
            probe.stop()
        if not dlg.accepted_choice:
            return None
        return ["--phbot-channel", dlg.channel(),
                "--phbot-components", ",".join(dlg.components()) or "none"]

    def setup_client_folder(self):
        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Client folder needed",
                                 "Pick a client folder first (or a folder with several clients).")
            return
        self.run_step(["--skip-deps", "--skip-wine", "--games", folder], "Client setup")

    def confirm_uninstall(self):
        # Deleting the install root out from under anything still running
        # (a client, phBot, a Manager) would pull the rug out on a live Wine
        # prefix - check first so the confirmation says so if that's the
        # case, instead of the user finding out from a crash.
        _, out, _ = run_sro_sh(["--list-json"])
        running_note = ""
        try:
            running = json.loads(out) if out else []
        except json.JSONDecodeError:
            running = []
        if running:
            running_note = (
                "\n\n%d process(es) are currently running (see the Manage page) - stop them "
                "first, or they'll be left running against files that no longer exist." % len(running)
            )
        self.run_step(
            ["--uninstall"], "Uninstall everything",
            "This removes EVERYTHING under the install root: wine-sro, all prefixes, saved\n"
            "clients, phBot, MaxiGuard support and the desktop/menu shortcuts made here. The\n"
            "Wine build cache is kept, so a later reinstall won't have to re-download Wine's\n"
            "sources.\n\n"
            "This cannot be undone." + running_note + "\n\nContinue?"
        )

    # -- running a step ---------------------------------------------------
    def _make_tracker(self, args) -> prog.RunTracker:
        probe = prog.WineBuildProbe(
            self._status.get("build_cache", ""),
            self._status.get("wine_build_version", ""),
            store=QSettings(),
        )
        return prog.RunTracker(prog.plan_from_args(args, self._status), probe)

    def run_step(self, args: list[str], label: str, confirm: str | None = None):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            QMessageBox.information(self, "Busy",
                                     "Another step is already running - wait for it to finish, or cancel it first.")
            return
        if confirm and QMessageBox.question(self, label, confirm) != QMessageBox.StandardButton.Yes:
            return
        # phBot is only downloaded when it is missing or an update was asked
        # for - exactly when its terms must be agreed to.
        if "--phbot" in args and (not self._status.get("phbot") or "--phbot-reinstall" in args):
            args = self._phbot_terms(args)
            if args is None:
                return
        self.log.appendPlainText("$ swe.sh %s" % " ".join(args))
        for b in self.action_buttons:
            b.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._last_args = list(args)

        self.tracker = self._make_tracker(args)
        self._line_buf = ""
        self._run_started = time.time()
        self.status_panel.set_state("running", "Installation running", label)
        self.status_panel.set_progress(0.0, None)
        self._poll.start()

        self.proc = QProcess(self)
        self.proc.setProgram(str(SWE))
        self.proc.setArguments(args)
        self.proc.setProcessEnvironment(clean_subprocess_env())
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.start()
        self.render_status_rows()       # hero -> "Installing …" + Cancel

    ACTION_FLAGS = ("--phbot", "--maxiguard", "--vsroplus", "--games", "--plain",
                    "--wine-only", "--deps-only")

    def _phbot_terms(self, args: list[str]) -> list[str] | None:
        """Show phBot's Terms and Conditions before a run that installs phBot.
        Returns the args to run (with --accept-phbot-terms, or without phBot
        when the user chose to skip it), or None to not run at all."""
        # The same run without phBot: drop --phbot and its options.
        rest, it = [], iter(args)
        for a in it:
            if a in ("--phbot-channel", "--phbot-components"):
                next(it, None)
            elif a not in ("--phbot", "--phbot-reinstall"):
                rest.append(a)
        missing_base = not (self._status.get("deps") and self._status.get("wine"))
        can_skip = missing_base or any(a in self.ACTION_FLAGS for a in rest)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            terms = fetch_phbot_terms()
        finally:
            QApplication.restoreOverrideCursor()
        if not terms:
            terms = ("The Terms and Conditions could not be loaded right now.\n\n"
                     "Please read them on phBot's download page before agreeing:\n" + PHBOT_TERMS_PAGE)
        dlg = widgets.TermsDialog(
            self, "phBot - Terms and Conditions",
            "phBot is third-party software by ProjectHax LLC. It is downloaded from ProjectHax's servers, and "
            "to download and use it you must agree to its Terms and Conditions.",
            terms, "I have read and agree to the Terms and Conditions", "Accept and install",
            skip_label="Continue without phBot" if can_skip else "",
            source_label="phbot.org", on_source=lambda: open_url(PHBOT_TERMS_PAGE))
        dlg.exec()
        if dlg.choice == "accept":
            return list(args) + ["--accept-phbot-terms"]
        if dlg.choice == "skip":
            if not any(a in self.ACTION_FLAGS for a in rest):
                # Only the build deps / wine-sro were left besides phBot. With no
                # action flag at all swe.sh would fall back to its full automatic
                # run (client scan included) - ask for exactly those two instead.
                rest.append("--wine-only")
            return rest
        return None

    def cancel_run(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.status_panel.set_detail("Cancelling …")
            self.proc.terminate()
            if not self.proc.waitForFinished(3000):
                self.proc.kill()

    def _on_output(self):
        if not self.proc:
            return
        data = strip_ansi(bytes(self.proc.readAllStandardOutput()).decode(errors="replace"))
        self.log.append_text(data)
        if not self.tracker:
            return
        # Same buffering reason as LogView.append_text: a chunk can end
        # mid-line, and half a ">>> STEP:" marker matches nothing.
        buf = self._line_buf + data
        lines = buf.split("\n")
        self._line_buf = lines.pop()
        changed = False
        boundary = False
        for line in lines:
            line = line.rstrip("\r")
            if self.tracker.feed(line):
                changed = True
            if prog.DONE_RE.match(line):
                boundary = True
        if changed:
            self._paint_progress()
        if boundary:
            # A step just completed - re-read the real status so "Aktueller
            # Stand" ticks over now instead of only at the end of the run.
            self.refresh_status()

    def _tick(self):
        if not self.tracker:
            return
        if self.tracker.refresh():
            self._paint_progress()
        self.status_panel.set_elapsed(
            prog.format_elapsed(time.time() - self._run_started))

    def _paint_progress(self):
        t = self.tracker
        if not t:
            return
        if not t.has_steps():
            # No step boundary seen - an early-exit flag, or a step type that
            # is not phase()-wrapped. Say "running", do not invent a number.
            if t.phase:
                self.status_panel.set_detail(t.phase)
            self.status_panel.set_progress(None, None)
            return
        n, total = t.step_position()
        name = t.step_name()
        parts = ["Step %d of %d" % (n, total)]
        if name:
            parts.append(name)
        detail = " · ".join(parts)
        if t.phase:
            detail += " — " + t.phase
        self.status_panel.set_detail(detail)
        self.status_panel.set_progress(
            t.overall(), t.step_fraction(),
            t.step_note() or prog.format_eta(t.eta_seconds()))

    def _on_finished(self, code, status):
        self._poll.stop()
        self.log.flush()
        if self.tracker and self._line_buf:
            self.tracker.feed(self._line_buf)
        self._line_buf = ""
        self.log.appendPlainText("(exit code %d)" % code)
        for b in self.action_buttons:
            b.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        elapsed = prog.format_elapsed(time.time() - self._run_started)
        if code == 0:
            if "--uninstall" in self._last_args:
                n = remove_all_shortcuts()
                if n:
                    self.log.appendPlainText("Removed %d desktop/menu shortcut(s)." % n)
            if "--remove-maxiguard" in self._last_args:
                # Deliberately removed - don't let the default-on option put
                # it straight back with the next "Continue install".
                QSettings().setValue("setup/maxiguard", False)
                self.maxiguard_check.setChecked(False)
            if self.tracker:
                self.tracker.finish_all()
            self.status_panel.set_state("done", "Done", "Finished in %s" % elapsed)
            self.status_panel.finish_bars(True)
            self.status_panel.settle_to_idle()
        else:
            self.status_panel.set_state(
                "failed", "Failed (exit code %d)" % code,
                "Open Details for the full output.")
            self.status_panel.finish_bars(False)
            self.status_panel.open_details()
            self._resume_install = None
        self.tracker = None
        self.render_status_rows()       # hero leaves "Installing …" right away
        self.refresh_status(restart=self._resume_install is not None)


def read_clients():
    if not CLIENTS_TSV.exists():
        return []
    rows = []
    for line in CLIENTS_TSV.read_text(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and "\x1b" not in parts[1]:
            rows.append(tuple(parts))
    return rows


def available_modes() -> dict:
    """{'plain': True, 'vsroplus': bool, 'maxiguard': bool} - mirrors the TUI's
    pick_mode(), which only ever offers a mode that is actually usable (e.g.
    MaxiGuard needs a patched GE-Proton build, vSroPlus needs at least one
    vSroPlus client ever set up)."""
    rc, out, _ = run_sro_sh(["--modes-json"])
    if rc == 0 and out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"plain": True, "vsroplus": False, "maxiguard": False}


class ModeCombo(QComboBox):
    """A mode picker that only lists modes --modes-json reports as usable -
    same idea as the TUI hiding a mode with nothing behind it instead of
    offering a choice that would just fail."""
    def __init__(self):
        super().__init__()
        self.reload()

    def reload(self):
        current = self.currentData()
        self.clear()
        modes = available_modes()
        for m in MODES:
            if modes.get(m):
                self.addItem(MODE_LABEL[m], m)
        if current:
            idx = self.findData(current)
            if idx >= 0:
                self.setCurrentIndex(idx)

    def mode(self) -> str | None:
        return self.currentData()


class _StartPane(QWidget):
    """Shared shape of the phBot / phBot Manager panes: pick a client type,
    press one button, watch a busy bar."""

    ARG = ""
    TITLE = ""
    DESCRIPTION = ""
    BUTTON = ""
    FAIL_TITLE = ""

    def __init__(self, log: LogView, status_panel):
        super().__init__()
        self.log = log
        self.status_panel = status_panel
        self.proc: QProcess | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        card = widgets.Card(self.TITLE, self.DESCRIPTION)

        row = QHBoxLayout()
        lbl = QLabel("Client type:")
        lbl.setProperty("role", "muted")
        row.addWidget(lbl)
        self.mode_combo = ModeCombo()
        row.addWidget(self.mode_combo, 1)
        card.add(row)

        self.start_btn = QPushButton(self.BUTTON)
        self.start_btn.setProperty("variant", "primary")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setIcon(theme.icon("play", "#0B1220", 15))
        self.start_btn.clicked.connect(self.start)
        card.add(self.start_btn)

        self.progress = widgets.busy_bar()
        card.add(self.progress)
        layout.addWidget(card)
        layout.addStretch(1)
        # Shortcuts live behind a right-click, so the pane looks exactly as
        # before; the button's tooltip is what makes it discoverable.
        self.start_btn.setToolTip("Right-click for a desktop / menu shortcut")
        card.setContextMenuPolicy(Qt.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda pos, c=card: self._shortcut_menu(c.mapToGlobal(pos)))

    def _shortcut_menu(self, global_pos):
        mode = self.mode_combo.mode()
        if not mode:
            return
        name = "%s (%s)" % (self.TITLE, MODE_LABEL[mode])
        shortcut_menu(self, self.status_panel, "%s-%s" % (self.TITLE, mode), name,
                      [self.ARG, mode], "Start %s with SilkroadWineEnvironment" % name,
                      title=name, exe_args=[self.EXE, mode]).exec(global_pos)

    def start(self):
        mode = self.mode_combo.mode()
        if not mode:
            QMessageBox.warning(self, "No client type available",
                                 "Set up a client first (Setup page).")
            return
        if self.proc and self.proc.state() != QProcess.NotRunning:
            return
        self.start_btn.setEnabled(False)
        self.progress.show()

        def done(rc, out, err):
            self.start_btn.setEnabled(True)
            self.progress.hide()
            name = "%s (%s)" % (self.TITLE, MODE_LABEL[mode])
            if rc == 0:
                self.log.appendPlainText("%s: %s" % (name, out))
                # The log is a drawer now, so a success that only ever went
                # there would be invisible - surface it in the status panel.
                self.status_panel.flash("done", "%s started" % self.TITLE,
                                        out or MODE_LABEL[mode])
            else:
                QMessageBox.warning(self, self.FAIL_TITLE, err or "unknown error")
                self.log.appendPlainText("%s failed: %s" % (name, err))
                self.status_panel.flash("failed", "%s did not start" % self.TITLE,
                                        err or "unknown error")

        self.proc = run_sro_sh_async([self.ARG, mode], done, self)


class PhbotPane(_StartPane):
    ARG = "--start-phbot"
    EXE = "phbot"           # sro.sh --exe-path kind, for the shortcut icon
    TITLE = "phBot"
    DESCRIPTION = "Starts phBot against the runtime of the selected client type."
    BUTTON = "Start phBot"
    FAIL_TITLE = "phBot did not start"


class ManagerPane(_StartPane):
    ARG = "--start-manager"
    EXE = "manager"
    TITLE = "phBot Manager"
    DESCRIPTION = "Starts phBot's own account/routine manager."
    BUTTON = "Start phBot Manager"
    FAIL_TITLE = "Manager did not start"


class ClientPane(QWidget):
    START_BTN_WIDTH = 108

    def __init__(self, log: LogView, status_panel):
        super().__init__()
        self.log = log
        self.status_panel = status_panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        row = QHBoxLayout()
        lbl = QLabel("Client type:")
        lbl.setProperty("role", "muted")
        row.addWidget(lbl)
        self.mode_combo = ModeCombo()
        self.mode_combo.currentIndexChanged.connect(self.reload_clients)
        row.addWidget(self.mode_combo, 1)
        layout.addLayout(row)

        # Scrollable, same reasoning as SetupTab: an unbounded client list
        # (or a small/tiled window) must never push the buttons below off
        # somewhere unreachable.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_body = QWidget()
        self.clients_layout = QVBoxLayout(scroll_body)
        self.clients_layout.setContentsMargins(0, 0, 0, 0)
        self.clients_layout.setSpacing(8)
        self.clients_layout.addStretch(1)
        scroll.setWidget(scroll_body)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        add_btn = QPushButton("Add new client …")
        add_btn.setIcon(theme.icon("plus", theme.TEXT, 15))
        add_btn.clicked.connect(self.add_client)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("variant", "ghost")
        refresh_btn.setIcon(theme.icon("refresh", theme.TEXT_MUTED, 15))
        refresh_btn.clicked.connect(self.reload_clients)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        self.progress = widgets.busy_bar()
        layout.addWidget(self.progress)

        self._procs: list[QProcess] = []   # keep references while starts are in flight
        self.reload_clients()

    def reload_clients(self):
        while self.clients_layout.count():
            item = self.clients_layout.takeAt(0)
            w = item.widget()
            if w:
                # setParent(None) BEFORE deleteLater(): takeAt() only removes
                # the layout item, so without it the widget stays a visible
                # child with its last geometry until the event loop gets round
                # to the deferred delete - which showed up as the previous
                # client's card still painted, stretched, underneath the new
                # list.
                w.setParent(None)
                w.deleteLater()

        mode = self.mode_combo.mode()
        rows = [r for r in read_clients() if r[0] == mode]
        if not rows:
            self.clients_layout.addWidget(widgets.EmptyState(
                "inbox", "No saved clients for this type yet",
                "Add a client folder below."))
            self.clients_layout.addStretch(1)
            return
        for m, label, folder in rows:
            card = widgets.ClientCard(label, folder)
            # One button per thing that can be started, instead of a
            # "what" combo box next to a single Start button: Client always,
            # Launcher (the client's own Silkroad.exe patcher/login launcher)
            # only when the folder actually has one.
            client_btn = QPushButton("Client")
            client_btn.setProperty("variant", "primary")
            client_btn.setIcon(theme.icon("play", "#0B1220", 14))
            client_btn.setToolTip("Start the game client directly\n(right-click for a desktop / menu shortcut)")
            client_btn.setCursor(Qt.PointingHandCursor)
            client_btn.setFixedWidth(self.START_BTN_WIDTH)
            client_btn.clicked.connect(
                lambda checked=False, mm=m, f=folder, n=label, b=client_btn:
                    self.start_client(mm, f, n, "client", b)
            )
            card.add_action(client_btn)
            has_launcher = ci_file_exists(Path(folder), "Silkroad.exe")
            if has_launcher:
                launcher_btn = QPushButton("Launcher")
                launcher_btn.setIcon(theme.icon("play", theme.TEXT, 14))
                launcher_btn.setToolTip("Start the client's own launcher (Silkroad.exe)\n"
                                        "(right-click for a desktop / menu shortcut)")
                launcher_btn.setCursor(Qt.PointingHandCursor)
                launcher_btn.setFixedWidth(self.START_BTN_WIDTH)
                launcher_btn.clicked.connect(
                    lambda checked=False, mm=m, f=folder, n=label, b=launcher_btn:
                        self.start_client(mm, f, n, "launcher", b)
                )
                card.add_action(launcher_btn)
            else:
                # Same-width gap, so every card's buttons stay lined up in
                # columns whether or not that client has a launcher.
                gap = QWidget()
                gap.setFixedWidth(self.START_BTN_WIDTH)
                card.add_action(gap)
            remove_btn = QPushButton()
            remove_btn.setProperty("variant", "danger")
            remove_btn.setToolTip("Remove this saved client")
            remove_btn.setIcon(theme.icon("trash", theme.DANGER, 15))
            if remove_btn.icon().isNull():
                remove_btn.setText("Remove")
            remove_btn.clicked.connect(lambda checked=False, mm=m, f=folder: self.remove_client(mm, f))
            card.add_action(remove_btn)
            card.setContextMenuPolicy(Qt.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda pos, c=card, mm=m, f=folder, n=label, hl=has_launcher:
                    self._shortcut_menu(c.mapToGlobal(pos), mm, f, n, hl))
            self.clients_layout.addWidget(card)
        self.clients_layout.addStretch(1)

    def _shortcut_menu(self, global_pos, mode: str, folder: str, label: str, has_launcher: bool):
        menu = QMenu(self)
        whats = [("client", "Client")] + ([("launcher", "Launcher")] if has_launcher else [])
        for i, (what, what_label) in enumerate(whats):
            if i:
                menu.addSeparator()
            name = label if what == "client" else "%s (Launcher)" % label
            shortcut_menu(self, self.status_panel,
                          "client-%s-%s-%s" % (mode, what, hashlib.sha1(folder.encode()).hexdigest()[:8]),
                          name, ["--start-client", mode, folder, what],
                          "Start %s (%s) with SilkroadWineEnvironment" % (name, MODE_LABEL[mode]),
                          menu=menu, title="%s: %s" % (label, what_label), exe_args=[what, folder])
        menu.exec(global_pos)

    def add_client(self):
        mode = self.mode_combo.mode()
        if not mode:
            QMessageBox.warning(self, "No client type available",
                                 "Set up a client first (Setup page).")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Select a %s client folder" % MODE_LABEL[mode])
        if not folder:
            return
        rc, exe, err = run_sro_sh(["--detect-client", folder])
        if rc != 0 or not exe:
            QMessageBox.warning(self, "No client found",
                                 "No Silkroad client executable found in:\n%s" % folder)
            return
        default_name = Path(folder).name
        name, ok = QInputDialog.getText(self, "Name this client", "Name:", text=default_name)
        if not ok:
            return
        name = name.strip() or default_name
        rc, out, err = run_sro_sh(["--add-client", mode, name, folder])
        if rc == 0:
            self.log.appendPlainText("Added %s client: %s (%s)" % (MODE_LABEL[mode], name, folder))
            self.status_panel.flash("done", "Client added", "%s — %s" % (name, folder))
        else:
            QMessageBox.warning(self, "Could not add client", err or "unknown error")
        self.reload_clients()

    def remove_client(self, mode: str, folder: str):
        if QMessageBox.question(self, "Remove client",
                                "Remove this saved client?\n%s" % folder) != QMessageBox.StandardButton.Yes:
            return
        run_sro_sh(["--remove-client", mode, folder])
        self.reload_clients()

    def start_client(self, mode: str, folder: str, label: str, which: str, button: QPushButton):
        # Reuses start_silkroad_headless via sro.sh - NOT the per-client
        # <mode>.sh script some folders have: that script only exists for
        # clients that went through the full setup_* installer, while a
        # client added here via "Add new client..." only ever registers an
        # entry in clients.tsv (matching what the TUI's own "add new client"
        # step does) - starting it has to work the same way the TUI starts
        # it, not depend on a file that may never have been generated.
        # Async: a first launch can take a moment (fresh Wine prefix), and
        # the blocking version left no sign anything had happened at all.
        button.setEnabled(False)
        self.progress.show()

        def done(rc, out, err):
            button.setEnabled(True)
            self._procs = [p for p in self._procs if p.state() != QProcess.NotRunning]
            if not self._procs:
                self.progress.hide()
            if rc == 0:
                self.log.appendPlainText("%s: %s" % (label, out))
                self.status_panel.flash("done", "%s started" % label, out or "")
            else:
                QMessageBox.warning(self, "Could not start client", err or "unknown error")
                self.log.appendPlainText("%s failed: %s" % (label, err))
                self.status_panel.flash("failed", "%s did not start" % label,
                                        err or "unknown error")

        proc = run_sro_sh_async(["--start-client", mode, folder, which], done, self)
        if proc:
            self._procs.append(proc)


class LaunchTab(QWidget):
    def __init__(self, log: LogView, status_panel):
        super().__init__()
        self.log = log
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)

        self.sub_tabs = QTabWidget()
        self.sub_tabs.setDocumentMode(True)
        self.phbot_pane = PhbotPane(log, status_panel)
        self.manager_pane = ManagerPane(log, status_panel)
        self.client_pane = ClientPane(log, status_panel)
        self.sub_tabs.addTab(self.phbot_pane, "phBot")
        self.sub_tabs.addTab(self.manager_pane, "phBot Manager")
        self.sub_tabs.addTab(self.client_pane, "Silkroad Client")
        layout.addWidget(self.sub_tabs)

    def reload_clients(self):
        for pane in (self.phbot_pane, self.manager_pane):
            pane.mode_combo.reload()
        self.client_pane.mode_combo.reload()
        self.client_pane.reload_clients()


class ManageTab(QWidget):
    def __init__(self, log: LogView, status_panel):
        super().__init__()
        self.log = log
        self.status_panel = status_panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        # An empty QTreeWidget is just a blank rectangle with a header - the
        # stack swaps in a proper "nothing is running" panel instead, so the
        # normal case reads as an answer rather than as a failure to load.
        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Process", "PID"])
        self.tree.setColumnWidth(0, 420)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.stack.addWidget(self.tree)
        self.stack.addWidget(widgets.EmptyState(
            "activity", "Nothing is running",
            "Started clients, bots and managers appear here."))
        layout.addWidget(self.stack, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("variant", "ghost")
        refresh_btn.setIcon(theme.icon("refresh", theme.TEXT_MUTED, 15))
        refresh_btn.clicked.connect(self.reload)
        # Short label + tooltip rather than spelling the cascade out inline:
        # the full sentence made this row alone ~620px wide, which is more
        # than the whole content area has at the 600px minimum window size.
        stop_btn = QPushButton("Stop selected")
        stop_btn.setToolTip("Stops the selected row and everything it started")
        stop_btn.clicked.connect(self.stop_selected)
        stop_all_btn = QPushButton("Stop ALL")
        stop_all_btn.setProperty("variant", "danger")
        stop_all_btn.clicked.connect(self.stop_all)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(stop_btn)
        btn_row.addWidget(stop_all_btn)
        layout.addLayout(btn_row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.reload)
        self.timer.start(5000)
        self.reload()

    def _list_json(self):
        rc, out, _ = run_sro_sh(["--list-json"])
        if rc != 0 or not out:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return []

    def reload(self):
        selected_ref = None
        if self.tree.currentItem():
            selected_ref = self.tree.currentItem().data(0, Qt.UserRole)
        self.tree.clear()
        entries = self._list_json()
        stack: list[tuple[int, QTreeWidgetItem]] = []
        for e in entries:
            item = QTreeWidgetItem([e["label"], e.get("pid", "")])
            item.setData(0, Qt.UserRole, e["ref"])
            depth = e.get("depth", 0)
            while stack and stack[-1][0] >= depth:
                stack.pop()
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            stack.append((depth, item))
            if e["ref"] == selected_ref:
                self.tree.setCurrentItem(item)
        self.tree.expandAll()
        self.stack.setCurrentIndex(0 if entries else 1)

    def stop_selected(self):
        item = self.tree.currentItem()
        if not item:
            QMessageBox.information(self, "Nothing selected", "Select a row first.")
            return
        ref = item.data(0, Qt.UserRole)
        self._stop_ref(ref)
        self.reload()

    def stop_all(self):
        # Stopping every ROOT (top-level tree item) cascades to its own
        # subtree already (see sro.sh --stop), so only roots need calling.
        n = self.tree.topLevelItemCount()
        for i in range(n):
            ref = self.tree.topLevelItem(i).data(0, Qt.UserRole)
            self._stop_ref(ref)
        if n:
            self.status_panel.flash("done", "Stopped", "%d process tree(s) stopped" % n)
        self.reload()

    def _stop_ref(self, ref: str):
        _, out, err = run_sro_sh(["--stop", ref])
        self.log.appendPlainText("stop %s: %s" % (ref, out or err))


class Sidebar(QFrame):
    """Icon + label navigation down the left edge.

    Collapses to icons only on a narrow window rather than raising the
    window's minimum width - the 600x480 floor exists because a tiling WM can
    hand the window a size it never asked for, and widening that floor would
    put the Setup page's Install button out of reach again.
    """

    ITEMS = [("gear", "Setup"), ("play", "Launch"), ("activity", "Manage")]
    WIDE, NARROW = 196, 56

    def __init__(self, on_change):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(self.WIDE)
        self._collapsed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 12)
        lay.setSpacing(10)

        self.brand = QWidget()
        brand_lay = QHBoxLayout(self.brand)
        brand_lay.setContentsMargins(14, 0, 14, 0)
        brand_lay.setSpacing(10)
        self.logo = QLabel()
        p = icon_path()
        if p:
            pm = QPixmap(str(p))
            if not pm.isNull():
                self.logo.setPixmap(pm.scaled(28, 28, Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation))
        brand_lay.addWidget(self.logo)
        self.wordmark = QWidget()
        wm = QVBoxLayout(self.wordmark)
        wm.setContentsMargins(0, 0, 0, 0)
        wm.setSpacing(0)
        t1 = QLabel("Silkroad")
        t1.setFont(theme.ui_font(11, bold=True))
        t2 = QLabel("Wine Environment")
        t2.setFont(theme.ui_font(8))
        t2.setProperty("role", "dim")
        wm.addWidget(t1)
        wm.addWidget(t2)
        brand_lay.addWidget(self.wordmark, 1)
        lay.addWidget(self.brand)
        lay.addSpacing(4)

        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for name, label in self.ITEMS:
            it = QListWidgetItem(theme.icon(name, theme.TEXT_MUTED, 18), label)
            it.setToolTip(label)
            # Explicit height: QSS padding on ::item does NOT grow the item's
            # sizeHint, so Qt kept laying the rows out ~29px apart while
            # painting a 34px selection pill - the rows visibly overlapped.
            it.setSizeHint(QSize(0, 38))
            self.nav.addItem(it)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(on_change)
        lay.addWidget(self.nav)
        lay.addStretch(1)

        self.footer = widgets.SidebarFooter(
            app_version(), system_summary(), AUTHOR,
            on_project=lambda: open_url(PROJECT_URL),
            on_update=lambda: open_url("%s/releases/tag/%s" % (PROJECT_URL, self.footer._update)),
            on_licenses=lambda: show_licenses(self))
        lay.addWidget(self.footer)

    def set_collapsed(self, collapsed: bool):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(self.NARROW if collapsed else self.WIDE)
        self.wordmark.setVisible(not collapsed)
        for i, (_, label) in enumerate(self.ITEMS):
            self.nav.item(i).setText("" if collapsed else label)
        self.footer.set_collapsed(collapsed)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SilkroadWineEnvironment")
        # Tall enough that the whole Setup page (hero + status grid + the
        # collapsed sections, ~520px) plus the progress panel while a run is
        # going (~96px) fit without scrolling - a too-short default window
        # was the actual cause of "the whole install part is missing" reports
        # (see SetupTab). Wide enough to keep the sidebar expanded (>= 820,
        # see resizeEvent) and the status grid at three columns.
        self.resize(960, 660)
        self.setMinimumSize(600, 480)

        shared_log = LogView()
        self.log_drawer = widgets.LogDrawer(shared_log, on_closed=self._drawer_closed)
        # StatusPanel only knows that it wants the details shown; which widget
        # that is belongs here, so the panel stays self-contained.
        self.status_panel = widgets.StatusPanel(on_toggle_details=self._toggle_details)

        self.setup_tab = SetupTab(shared_log, self.status_panel, self.on_status_changed,
                                  on_open_launch=lambda: self.sidebar.nav.setCurrentRow(1))
        self.launch_tab = LaunchTab(shared_log, self.status_panel)
        self.manage_tab = ManageTab(shared_log, self.status_panel)

        self.pages = QStackedWidget()
        for w in (self.setup_tab, self.launch_tab, self.manage_tab):
            self.pages.addWidget(w)

        content = QWidget()
        content.setObjectName("content")
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(0)
        content_lay.addWidget(self.pages, 1)
        content_lay.addWidget(self.status_panel)
        content_lay.addWidget(self.log_drawer)

        self.sidebar = Sidebar(self._on_page_changed)

        root = QWidget()
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)
        root_lay.addWidget(self.sidebar)
        root_lay.addWidget(content, 1)
        self.setCentralWidget(root)

        self._startup_tab_decided = False

        self._update_check = UpdateCheck(self.sidebar.footer._version, self.sidebar.footer.set_update)
        QTimer.singleShot(1500, self._update_check.start)

    # -- log drawer -------------------------------------------------------
    def _toggle_details(self, is_open: bool):
        self.log_drawer.set_open(is_open)

    def _drawer_closed(self):
        self.status_panel.set_details_open(False)

    # -- navigation -------------------------------------------------------
    def on_status_changed(self, data: dict, done: int, total: int):
        self.status_panel.set_summary(done, total)
        self.sidebar.footer.set_wine(data.get("wine_version", "") if data.get("wine") else "")
        # Only on the very first status check after opening (not every
        # later "Refresh status" click, which would otherwise yank the user
        # back to Launch mid-session): once a launcher exists, an install
        # has already completed successfully at least once, so what's left
        # to do here is start things, not install them - same reasoning the
        # terminal control panel uses to go straight to its own launcher menu.
        if self._startup_tab_decided:
            return
        self._startup_tab_decided = True
        if data.get("launcher"):
            self.sidebar.nav.setCurrentRow(1)

    def _on_page_changed(self, idx: int):
        if idx < 0:
            return
        self.pages.setCurrentIndex(idx)
        page = self.pages.widget(idx)
        if page is self.launch_tab:
            self.launch_tab.reload_clients()
        elif page is self.manage_tab:
            self.manage_tab.reload()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sidebar.set_collapsed(self.width() < 820)

    def closeEvent(self, event):
        # Only the Setup page's swe.sh process is checked here - NOT anything
        # running in Launch/Manage (phBot, Manager, a client): those are meant
        # to keep running after the GUI window closes, same as closing a
        # terminal that started them with & would. An install step killed
        # mid-way (especially the ~20-60 min wine-sro build) is the one case
        # that actually loses real work, so that's the one worth a
        # confirmation before an accidental X-close throws it away.
        self._update_check.stop()
        poll = self.setup_tab._status_proc
        if poll is not None and poll.state() != QProcess.NotRunning:
            poll.kill()
            poll.waitForFinished(1000)
        proc = self.setup_tab.proc
        if proc and proc.state() != QProcess.NotRunning:
            reply = QMessageBox.question(
                self, "Installation running",
                "An installation step is still running.\n\n"
                "Closing now will abort it - safe to resume later, but a step already in "
                "progress (e.g. the wine-sro build) will have to start over from scratch "
                "next time.\n\nAbort and close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.setup_tab.cancel_run()
        event.accept()


def run_selftest(app: QApplication) -> int:
    """`--selftest`: prove the bundle actually works on this machine, then exit.

    Used by tests/smoke-appimage.sh on each target distro. Builds the real
    main window (so every Qt module/plugin it needs gets loaded), makes one
    real swe.sh round trip through the same environment the GUI uses, and
    lets the event loop run briefly so painting happens too. Any missing
    library shows up as a crash / non-zero exit instead of on a user's
    machine."""
    win = MainWindow()
    win.show()
    p = QProcess()
    p.setProgram(str(SWE))
    p.setArguments(["--status-json"])
    p.setProcessEnvironment(clean_subprocess_env())
    p.start()
    if not p.waitForFinished(60000):
        print("selftest FAILED: swe.sh --status-json did not finish", file=sys.stderr)
        return 1
    out = bytes(p.readAllStandardOutput()).decode(errors="replace").strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        err = strip_ansi(bytes(p.readAllStandardError()).decode(errors="replace")).strip()
        print("selftest FAILED: swe.sh --status-json printed no JSON: %r / %r" % (out[-300:], err[-300:]),
              file=sys.stderr)
        return 1
    formats = [bytes(f).decode() for f in QImageReader.supportedImageFormats()]
    missing = [f for f in ("png", "svg", "ico") if f not in formats]
    if missing:
        print("selftest FAILED: Qt image formats missing from the bundle: %s" % ", ".join(missing),
              file=sys.stderr)
        return 1
    QTimer.singleShot(1500, win.close)
    QTimer.singleShot(1600, app.quit)
    app.exec()
    print("selftest OK: platform=%s qt=%s pyside=%s python=%s status-keys=%d version=%s" % (
        app.platformName(), qVersion(), PySide6.__version__, sys.version.split()[0], len(data),
        app_version()))
    return 0


def main():
    app = QApplication(sys.argv)
    # Set before anything touches QSettings or QStandardPaths - they key their
    # storage off these, and the wine build's progress calibration is stored
    # through QSettings.
    app.setOrganizationName("SilkroadWineEnvironment")
    app.setApplicationName("sro-gui")
    theme.apply_theme(app)
    icon = icon_path()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))
    if "--askpass" in sys.argv:
        sys.exit(run_askpass(app))
    if not SWE.exists():
        print("error: swe.sh not found next to this GUI (looked at %s)" % SWE, file=sys.stderr)
        sys.exit(1)
    if "--selftest" in sys.argv:
        sys.exit(run_selftest(app))
    sync_installed_launcher()
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
