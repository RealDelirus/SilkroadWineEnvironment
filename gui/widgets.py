#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable UI pieces for the SilkroadWineEnvironment GUI.

Nothing in here talks to swe.sh/sro.sh - these are presentation widgets the
tabs in main.py assemble. Colors and icons all come from theme.py; no widget
here carries a hardcoded color.

Styling convention: instead of per-widget setStyleSheet() calls, widgets set a
dynamic property (objectName, `variant`, `state`, `role`, `bar`) that the one
global stylesheet in theme.py selects on. Changing such a property at runtime
needs an unpolish/polish round - see repolish().
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QFontMetrics, QTransform
from PySide6.QtWidgets import (
    QFrame, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QProgressBar, QSizePolicy, QApplication, QDialog, QPlainTextEdit, QCheckBox,
    QButtonGroup, QGridLayout,
)

import theme


def repolish(w: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed.

    Qt resolves property selectors when a widget is polished, not on every
    paint, so a `setProperty("state", ...)` alone changes nothing visually.
    """
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("hline")
    f.setFixedHeight(1)
    return f


def busy_bar() -> QProgressBar:
    """Small indeterminate 'something is running' bar - same look everywhere
    it is used (the Launch panes)."""
    bar = QProgressBar()
    bar.setRange(0, 0)
    bar.setTextVisible(False)
    bar.setFixedHeight(4)
    bar.hide()
    return bar


class SectionHeader(QWidget):
    """Title plus optional muted description, for the top of a Card."""

    def __init__(self, title: str, description: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.title = QLabel(title)
        self.title.setFont(theme.ui_font(11, bold=True))
        self.title.setProperty("role", "title")
        lay.addWidget(self.title)
        if description:
            d = QLabel(description)
            d.setProperty("role", "muted")
            d.setWordWrap(True)
            lay.addWidget(d)


class Card(QFrame):
    """A rounded, bordered panel - replaces QGroupBox everywhere.

    QGroupBox's etched frame and inset legend are the single biggest reason
    the old layout read as dated, and its title cannot be styled to sit
    inside the panel the way a heading should.
    """

    def __init__(self, title: str = "", description: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)
        if title:
            self.body.addWidget(SectionHeader(title, description))

    def add(self, w):
        if isinstance(w, QWidget):
            self.body.addWidget(w)
        else:
            self.body.addLayout(w)
        return w


class StatusTile(QFrame):
    """`● N  Name` - one component in the status grid.

    The number is not decoration: the Advanced buttons refer to components by
    it ("6. Recreate MaxiGuard prefix"), so it has to stay in sync with the
    order in main.py's STATUS_ROWS. The full name and the state word live in
    the tooltip - the tile itself only has room for the short name.
    """

    STATE_TEXT = {
        "ok": "set up",
        "missing": "missing",
        "on": "on",
        "off": "off",
        "unknown": "checking …",
    }

    def __init__(self, number: int, short: str, full: str):
        super().__init__()
        self.setObjectName("tile")
        self._full = full
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self.dot = QLabel()
        self.dot.setObjectName("dot")
        self.dot.setFixedSize(8, 8)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)

        num = QLabel("%d" % number)
        num.setFont(theme.mono_font(8))
        num.setProperty("role", "dim")
        lay.addWidget(num)

        self.name = QLabel(short)
        self.name.setFont(theme.ui_font(9))
        lay.addWidget(self.name, 1)
        self.set_state("unknown")

    def set_state(self, state: str, text: str = ""):
        self.dot.setProperty("state", state)
        self.setProperty("state", state)
        self.name.setProperty("role", "dim" if state in ("off", "unknown") else "")
        self.setToolTip("%s: %s" % (self._full, text or self.STATE_TEXT.get(state, state)))
        for w in (self, self.dot, self.name):
            repolish(w)


class StatusGrid(QWidget):
    """The Setup page's component overview: one column per group
    (Core | MaxiGuard | vSroPlus + Optional), each a short stack of tiles.

    Replaces a 10-row list of full-width rows - the same information in about
    half the height, still readable at a glance because the group a missing
    component belongs to is right above it.
    """

    def __init__(self, columns):
        """columns: [[(group_key, title, [(key, number, short, full), ...]), ...], ...]
        - one inner list per visual column, each holding one or more groups."""
        super().__init__()
        self.tiles: dict[str, StatusTile] = {}
        self._notes: dict[str, QLabel] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        for groups in columns:
            col = QVBoxLayout()
            col.setSpacing(6)
            for i, (gkey, title, items) in enumerate(groups):
                if i:
                    col.addSpacing(6)
                head = QHBoxLayout()
                head.setSpacing(6)
                t = QLabel(title.upper())
                t.setFont(theme.ui_font(8, bold=True))
                t.setProperty("role", "dim")
                head.addWidget(t)
                head.addStretch(1)
                note = QLabel("")
                note.setFont(theme.ui_font(8))
                note.setProperty("role", "dim")
                head.addWidget(note)
                self._notes[gkey] = note
                col.addLayout(head)
                for key, number, short, full in items:
                    tile = StatusTile(number, short, full)
                    self.tiles[key] = tile
                    col.addWidget(tile)
            col.addStretch(1)
            lay.addLayout(col, 1)

    def set_state(self, key: str, state: str, text: str = ""):
        tile = self.tiles.get(key)
        if tile:
            tile.set_state(state, text)

    def set_note(self, group_key: str, text: str):
        note = self._notes.get(group_key)
        if note:
            note.setText(text)


class SetupHero(QFrame):
    """Top card of the Setup page: where the install stands, plus THE action.

    Big state icon + title + one-line subtitle on the left, the primary button
    (sized to its text, not stretched across the card) on the right, with a
    quiet text link under it for the secondary action ("Re-run setup") and a
    Cancel button that only exists while something is running.
    """

    TONES = {
        "checking": ("circle", theme.TEXT_MUTED, "idle"),
        "fresh": ("download", theme.ACCENT, "accent"),
        "partial": ("alert", theme.WARN, "warn"),
        "complete": ("check-circle", theme.OK, "ok"),
        "running": ("loader", theme.ACCENT, "accent"),
    }

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)

        self.icon = QLabel()
        self.icon.setObjectName("heroIcon")
        self.icon.setFixedSize(44, 44)
        self.icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.icon, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self.title = QLabel("Checking status …")
        self.title.setFont(theme.ui_font(14, bold=True))
        text_col.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setProperty("role", "muted")
        self.subtitle.setWordWrap(True)
        text_col.addWidget(self.subtitle)
        text_col.addStretch(1)
        lay.addLayout(text_col, 1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.cancel = QPushButton("Cancel")
        self.cancel.setProperty("variant", "ghost")
        self.cancel.setMinimumHeight(40)
        self.cancel.hide()
        btn_row.addWidget(self.cancel)
        self.primary = QPushButton("Install")
        self.primary.setProperty("variant", "primary")
        self.primary.setFont(theme.ui_font(11, bold=True))
        self.primary.setMinimumHeight(40)
        self.primary.setMinimumWidth(170)
        self.primary.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(self.primary)
        btn_col.addLayout(btn_row)
        self.link = QPushButton("Re-run setup (verify)")
        self.link.setProperty("variant", "link")
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.hide()
        btn_col.addWidget(self.link, 0, Qt.AlignRight)
        btn_col.addStretch(1)
        lay.addLayout(btn_col)
        self.set_state("checking", "Checking status …", "", "Install", "play")

    def set_state(self, kind: str, title: str, subtitle: str,
                  button_text: str, button_icon: str, show_link: bool = False):
        name, color, tone = self.TONES.get(kind, self.TONES["checking"])
        pm = theme.pixmap(name, color, 24, 2.0)
        if not pm.isNull():
            self.icon.setPixmap(pm)
        self.icon.setProperty("tone", tone)
        repolish(self.icon)
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.subtitle.setVisible(bool(subtitle))
        self.primary.setText(button_text)
        self.primary.setIcon(theme.icon(button_icon, "#0B1220", 16))
        self.link.setVisible(show_link)
        self.cancel.setVisible(kind == "running")


class SidebarFooter(QWidget):
    """Small print at the bottom of the sidebar: version, update hint, the
    installed wine-sro, host system, project link and author.

    Collapses to just the short version when the sidebar goes icon-only; the
    full text then lives in the tooltip."""

    def __init__(self, version: str, system: str, author: str, on_project, on_update, on_licenses=None):
        super().__init__()
        self.setObjectName("sidebarFooter")
        self._version = version
        self._system = system
        self._author = author
        self._wine = ""
        self._update = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 12, 0)
        lay.setSpacing(3)

        self.version = QLabel(version)
        self.version.setFont(theme.mono_font(8))
        self.version.setProperty("role", "muted")
        lay.addWidget(self.version)

        self.update_btn = QPushButton("")
        self.update_btn.setProperty("variant", "update")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.clicked.connect(lambda _checked=False: on_update())
        self.update_btn.hide()
        lay.addWidget(self.update_btn, 0, Qt.AlignLeft)

        self.details = QWidget()
        det = QVBoxLayout(self.details)
        det.setContentsMargins(0, 0, 0, 0)
        det.setSpacing(3)
        self.wine = self._small("")
        self.wine.hide()
        det.addWidget(self.wine)
        det.addWidget(self._small(system))
        self.project_btn = QPushButton("Project")
        self.project_btn.setProperty("variant", "link")
        self.project_btn.setFont(theme.ui_font(8))
        self.project_btn.setIcon(theme.icon("external-link", theme.TEXT_MUTED, 12))
        self.project_btn.setCursor(Qt.PointingHandCursor)
        self.project_btn.clicked.connect(lambda _checked=False: on_project())
        det.addWidget(self.project_btn, 0, Qt.AlignLeft)
        if on_licenses:
            lic_btn = QPushButton("Licenses")
            lic_btn.setProperty("variant", "link")
            lic_btn.setFont(theme.ui_font(8))
            lic_btn.setIcon(theme.icon("file-text", theme.TEXT_MUTED, 12))
            lic_btn.setCursor(Qt.PointingHandCursor)
            lic_btn.clicked.connect(lambda _checked=False: on_licenses())
            det.addWidget(lic_btn, 0, Qt.AlignLeft)
        det.addWidget(self._small("Built by " + author))
        lay.addWidget(self.details)
        self._sync_tooltip()

    @staticmethod
    def _small(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(theme.ui_font(8))
        lbl.setProperty("role", "dim")
        return lbl

    def set_wine(self, wine_version: str):
        self._wine = (wine_version or "").replace("wine-", "")
        self.wine.setText("wine-sro " + self._wine if self._wine else "")
        self.wine.setVisible(bool(self._wine))
        self._sync_tooltip()

    def set_update(self, tag: str):
        self._update = tag
        self.update_btn.setText("●  Update available: " + tag)
        self.update_btn.setVisible(bool(tag) and self.details.isVisible())
        self._sync_tooltip()

    def set_collapsed(self, collapsed: bool):
        self.details.setVisible(not collapsed)
        self.update_btn.setVisible(bool(self._update) and not collapsed)
        short = self._version.split("-")[0]
        self.version.setText(short if collapsed else self._version)
        self.version.setProperty("role", "accent" if collapsed and self._update else "muted")
        repolish(self.version)

    def _sync_tooltip(self):
        lines = ["SilkroadWineEnvironment " + self._version]
        if self._update:
            lines.append("Update available: " + self._update)
        if self._wine:
            lines.append("wine-sro " + self._wine)
        lines += [self._system, "Built by " + self._author]
        self.setToolTip("\n".join(lines))


class CollapsibleSection(QWidget):
    """A titled section that starts collapsed and expands on click.

    Originally lived in main.py; moved here with the chevron switched from a
    literal arrow character to an SVG icon. The isChecked() workaround in
    _on_toggle is load-bearing and carried over verbatim.
    """

    def __init__(self, title: str, content: QWidget, start_expanded: bool = False):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.toggle_btn = QPushButton("  " + title)
        self.toggle_btn.setObjectName("sectionToggle")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(start_expanded)
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setIconSize(QSize(16, 16))
        self.toggle_btn.clicked.connect(self._on_toggle)
        layout.addWidget(self.toggle_btn)
        self._title = title
        self.content = content
        self.content.setVisible(start_expanded)
        layout.addWidget(self.content)
        self._sync()

    def _sync(self):
        checked = self.toggle_btn.isChecked()
        name = "chevron-down" if checked else "chevron-right"
        color = theme.TEXT if checked else theme.TEXT_MUTED
        self.toggle_btn.setIcon(theme.icon(name, color, 16))
        if self.toggle_btn.icon().isNull():
            # SVG unavailable - fall back to the original text arrows rather
            # than leaving a header with no affordance at all.
            self.toggle_btn.setText("%s %s" % ("v" if checked else ">", self._title))

    def _on_toggle(self, checked: bool = False):
        # Read the actual state from the button itself instead of trusting
        # the signal's argument - some Qt/PySide combinations can emit
        # clicked() with no argument at all depending on how the click was
        # triggered, which would otherwise show/hide based on a stale default
        # instead of the button's real checked state.
        checked = self.toggle_btn.isChecked()
        self.content.setVisible(checked)
        self._sync()


class EmptyState(QWidget):
    """Centred icon + title + hint, for a list with nothing in it."""

    def __init__(self, icon_name: str, title: str, hint: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(8)

        pm = theme.pixmap(icon_name, theme.TEXT_DIM, 40, 2.0)
        if not pm.isNull():
            ic = QLabel()
            ic.setPixmap(pm)
            ic.setAlignment(Qt.AlignCenter)
            lay.addWidget(ic)

        t = QLabel(title)
        t.setFont(theme.ui_font(11))
        t.setProperty("role", "muted")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)

        if hint:
            h = QLabel(hint)
            h.setProperty("role", "dim")
            h.setAlignment(Qt.AlignCenter)
            h.setWordWrap(True)
            lay.addWidget(h)


class ElidedLabel(QLabel):
    """Single-line label that elides in the middle instead of forcing the
    window wider. Used for client folder paths, which are long and whose
    interesting parts are at both ends."""

    def __init__(self, text: str = ""):
        super().__init__()
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str):
        self._full = text
        self.setToolTip(text)
        super().setText(self._elided())

    def _elided(self) -> str:
        fm = QFontMetrics(self.font())
        return fm.elidedText(self._full, Qt.ElideMiddle, max(60, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        super().setText(self._elided())


class ClientCard(QFrame):
    """One saved client: name, folder path, what-to-start picker, actions."""

    def __init__(self, name: str, folder: str):
        super().__init__()
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title = QLabel(name)
        title.setFont(theme.ui_font(10, bold=True))
        text_col.addWidget(title)
        path = ElidedLabel(folder)
        path.setFont(theme.ui_font(9))
        path.setProperty("role", "dim")
        text_col.addWidget(path)
        lay.addLayout(text_col, 1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        lay.addLayout(self.actions, 0)

    def add_action(self, w: QWidget):
        self.actions.addWidget(w)
        return w


class StatusPanel(QFrame):
    """The always-visible status strip below the pages.

    This is what replaces the old "Output:" log taking up a third of the
    window: the log is now a drawer, and this is the thing that is supposed to
    be readable from across the room. It sits outside the page stack on
    purpose, so a 20-60 minute wine build stays visible while the user is off
    looking at the Launch or Manage page.
    """

    def __init__(self, on_toggle_details=None):
        super().__init__()
        self.setObjectName("statusPanel")
        self._on_toggle_details = on_toggle_details
        self._state = "idle"
        self._spin_angle = 0
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._restore_idle)
        self._idle_title = "Ready"
        self._idle_detail = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(34, 34)
        self.icon_label.setAlignment(Qt.AlignCenter)
        top.addWidget(self.icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.title = QLabel("Ready")
        self.title.setFont(theme.ui_font(11, bold=True))
        text_col.addWidget(self.title)
        self.detail = QLabel("")
        self.detail.setProperty("role", "muted")
        self.detail.setFont(theme.ui_font(9))
        text_col.addWidget(self.detail)
        top.addLayout(text_col, 1)

        self.elapsed = QLabel("")
        self.elapsed.setFont(theme.mono_font(10))
        self.elapsed.setProperty("role", "muted")
        top.addWidget(self.elapsed)

        self.details_btn = QPushButton("Details")
        self.details_btn.setProperty("variant", "ghost")
        self.details_btn.setCheckable(True)
        self.details_btn.setCursor(Qt.PointingHandCursor)
        self.details_btn.clicked.connect(self._details_clicked)
        top.addWidget(self.details_btn)
        outer.addLayout(top)

        self.bars = QWidget()
        bars_lay = QVBoxLayout(self.bars)
        bars_lay.setContentsMargins(0, 0, 0, 0)
        bars_lay.setSpacing(5)
        self.overall_bar, self.overall_value = self._make_bar(
            bars_lay, "Overall", 6, "overall")
        self.step_bar, self.step_value = self._make_bar(
            bars_lay, "Current step", 4, "step")
        outer.addWidget(self.bars)
        self.bars.hide()

        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._spin)
        self._paint_icon()

    def _make_bar(self, parent_layout, caption, height, kind):
        row = QHBoxLayout()
        row.setSpacing(10)
        cap = QLabel(caption)
        cap.setFont(theme.ui_font(9))
        cap.setProperty("role", "dim")
        cap.setFixedWidth(108)
        row.addWidget(cap)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(height)
        bar.setRange(0, 1000)
        bar.setValue(0)
        if kind == "step":
            bar.setProperty("bar", "step")
        row.addWidget(bar, 1)
        value = QLabel("")
        value.setFont(theme.ui_font(9))
        value.setProperty("role", "muted")
        value.setMinimumWidth(120)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(value)
        parent_layout.addLayout(row)
        return bar, value

    # -- appearance -------------------------------------------------------
    ICONS = {
        "idle": ("circle", theme.TEXT_MUTED),
        "running": ("loader", theme.ACCENT),
        "done": ("check-circle", theme.OK),
        "failed": ("x-circle", theme.DANGER),
        "warn": ("alert", theme.WARN),
    }

    def _paint_icon(self):
        name, color = self.ICONS.get(self._state, self.ICONS["idle"])
        pm = theme.pixmap(name, color, 22, 2.0)
        if pm.isNull():
            self.icon_label.setText("*")
            return
        if self._state == "running" and self._spin_angle:
            pm = pm.transformed(QTransform().rotate(self._spin_angle),
                                Qt.SmoothTransformation)
        self.icon_label.setPixmap(pm)

    def _spin(self):
        self._spin_angle = (self._spin_angle + 30) % 360
        self._paint_icon()

    def _bar_tone(self, tone):
        for bar, default in ((self.overall_bar, ""), (self.step_bar, "step")):
            bar.setProperty("bar", tone or default)
            repolish(bar)

    # -- API --------------------------------------------------------------
    def set_state(self, kind: str, title: str, detail: str = ""):
        self._state = kind
        self.title.setText(title)
        self.detail.setText(detail)
        self.title.setProperty("role", {
            "running": "accent", "done": "ok", "failed": "danger", "warn": "warn",
        }.get(kind, "title"))
        repolish(self.title)
        if kind == "running":
            self._spin_timer.start(90)
            self.bars.show()
            self._bar_tone(None)
        else:
            self._spin_timer.stop()
            self._spin_angle = 0
            if kind == "done":
                self._bar_tone("ok")
            elif kind == "failed":
                self._bar_tone("danger")
            else:
                self.bars.hide()
                self.elapsed.setText("")
        self._paint_icon()

    def set_summary(self, done: int, total: int):
        """Idle text: how much of the environment is already set up."""
        if done >= total:
            self._idle_title = "Ready"
            self._idle_detail = "All %d components set up" % total
        else:
            self._idle_title = "Setup incomplete"
            self._idle_detail = "%d of %d components set up" % (done, total)
        if self._state == "idle":
            self.set_state("idle", self._idle_title, self._idle_detail)

    def set_detail(self, text: str):
        self.detail.setText(text)

    def set_elapsed(self, text: str):
        self.elapsed.setText(text)

    def set_progress(self, overall, step, eta_text: str = ""):
        """overall/step: 0..1, or None for indeterminate."""
        if overall is None:
            self.overall_bar.setRange(0, 0)
            self.overall_value.setText("running …")
        else:
            self.overall_bar.setRange(0, 1000)
            self.overall_bar.setValue(int(round(max(0.0, min(1.0, overall)) * 1000)))
            self.overall_value.setText("%d %%" % int(round(overall * 100)))
        if step is None:
            # Nothing measurable for this step - an honest pulse beats an
            # invented number.
            self.step_bar.setRange(0, 0)
            self.step_value.setText(eta_text or "running …")
        else:
            self.step_bar.setRange(0, 1000)
            self.step_bar.setValue(int(round(max(0.0, min(1.0, step)) * 1000)))
            self.step_value.setText(eta_text or "%d %%" % int(round(step * 100)))

    def finish_bars(self, ok: bool):
        if ok:
            self.overall_bar.setRange(0, 1000)
            self.overall_bar.setValue(1000)
            self.overall_value.setText("100 %")
            self.step_bar.setRange(0, 1000)
            self.step_bar.setValue(1000)
            self.step_value.setText("")
        else:
            # Freeze wherever it got to: an aborted run did not reach 100%,
            # and showing a full bar next to a red "failed" would be a lie.
            if self.step_bar.minimum() == self.step_bar.maximum():
                self.step_bar.setRange(0, 1000)
                self.step_bar.setValue(0)
            self.step_value.setText("cancelled")

    def flash(self, kind: str, title: str, detail: str = "", msec: int = 6000):
        """Transient message - used by Launch/Manage, whose results would
        otherwise only ever land in the (now collapsed) log."""
        if self._state == "running":
            return          # never talk over a running install
        self.set_state(kind, title, detail)
        self._flash_timer.start(msec)

    def _restore_idle(self):
        if self._state != "running":
            self.set_state("idle", self._idle_title, self._idle_detail)

    def settle_to_idle(self, msec: int = 10000):
        self._flash_timer.start(msec)

    # -- details button ---------------------------------------------------
    def _details_clicked(self):
        checked = self.details_btn.isChecked()
        if self._on_toggle_details:
            self._on_toggle_details(checked)

    def set_details_open(self, is_open: bool):
        self.details_btn.setChecked(is_open)

    def open_details(self):
        """Force the details drawer open - used when a run fails, where the
        output is the whole point and asking for one more click is friction."""
        self.details_btn.setChecked(True)
        self._details_clicked()


class LogDrawer(QWidget):
    """The output log, collapsed by default.

    Plain show/hide rather than a QSplitter: collapsing a splitter pane to
    zero and restoring it at a sensible size is fiddly, and there is nothing
    here worth dragging.
    """

    def __init__(self, log_view: QWidget, on_closed=None):
        super().__init__()
        self.setObjectName("logDrawer")
        self.log_view = log_view
        self._on_closed = on_closed

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        icon = QLabel()
        pm = theme.pixmap("terminal", theme.TEXT_MUTED, 15, 2.0)
        if not pm.isNull():
            icon.setPixmap(pm)
            head.addWidget(icon)
        title = QLabel("Output")
        title.setProperty("role", "muted")
        title.setFont(theme.ui_font(10, bold=True))
        head.addWidget(title)
        head.addStretch(1)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setProperty("variant", "ghost")
        self.copy_btn.setIcon(theme.icon("copy", theme.TEXT_MUTED, 15))
        self.copy_btn.clicked.connect(self._copy)
        head.addWidget(self.copy_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setProperty("variant", "ghost")
        self.clear_btn.setIcon(theme.icon("trash", theme.TEXT_MUTED, 15))
        self.clear_btn.clicked.connect(self.log_view.clear)
        head.addWidget(self.clear_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setProperty("variant", "ghost")
        self.close_btn.setIcon(theme.icon("chevron-down", theme.TEXT_MUTED, 15))
        self.close_btn.clicked.connect(self._close)
        head.addWidget(self.close_btn)
        lay.addLayout(head)

        lay.addWidget(self.log_view, 1)
        self.setFixedHeight(300)
        self.setVisible(False)

    def _copy(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())

    def _close(self):
        self.setVisible(False)
        if self._on_closed:
            self._on_closed()

    def set_open(self, is_open: bool):
        self.setVisible(is_open)


class TermsDialog(QDialog):
    """Show a third-party license/terms text and ask for agreement - modelled
    on phbot.org's own download page: the text, an "I have read and agree"
    checkbox, and the action only enabled once it is ticked.

    After exec(), `choice` is "accept", "skip" (only offered when allow_skip)
    or "cancel"."""

    def __init__(self, parent, title: str, intro: str, text: str, agree_label: str,
                 accept_label: str, skip_label: str = "", source_label: str = "", on_source=None):
        super().__init__(parent)
        self.choice = "cancel"
        self.setWindowTitle(title)
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        head = QLabel(title)
        head.setFont(theme.ui_font(13, bold=True))
        lay.addWidget(head)
        intro_lbl = QLabel(intro)
        intro_lbl.setWordWrap(True)
        intro_lbl.setProperty("role", "muted")
        lay.addWidget(intro_lbl)

        body = QPlainTextEdit()
        body.setObjectName("logView")
        body.setReadOnly(True)
        body.setFont(theme.ui_font(9))
        body.setPlainText(text)
        lay.addWidget(body, 1)

        row = QHBoxLayout()
        self.agree = QCheckBox(agree_label)
        row.addWidget(self.agree, 1)
        if source_label and on_source:
            src = QPushButton(source_label)
            src.setProperty("variant", "link")
            src.setIcon(theme.icon("external-link", theme.TEXT_MUTED, 12))
            src.setCursor(Qt.PointingHandCursor)
            src.clicked.connect(lambda _checked=False: on_source())
            row.addWidget(src)
        lay.addLayout(row)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "ghost")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        if skip_label:
            skip = QPushButton(skip_label)
            skip.clicked.connect(lambda _checked=False: self._done("skip"))
            btns.addWidget(skip)
        self.accept_btn = QPushButton(accept_label)
        self.accept_btn.setProperty("variant", "primary")
        self.accept_btn.setEnabled(False)
        self.accept_btn.clicked.connect(lambda _checked=False: self._done("accept"))
        btns.addWidget(self.accept_btn)
        lay.addLayout(btns)
        self.agree.toggled.connect(self.accept_btn.setEnabled)

    def _done(self, choice: str):
        self.choice = choice
        self.accept()


def human_size(n) -> str:
    if not n:
        return ""
    if n >= 1024 ** 3:
        return "%.1f GB" % (n / 1024.0 ** 3)
    if n >= 1024 ** 2:
        return "%.0f MB" % (n / 1024.0 ** 2)
    return "%.0f KB" % (n / 1024.0)


class PhbotOptionsDialog(QDialog):
    """What to install of phBot - the choices phBot's own installer offers:
    the update channel and the optional packages.

    Pure presentation: the caller fetches the channel data (versions,
    changelogs, download sizes) and feeds it in through set_channel_info()
    and set_size() as it arrives, so the dialog is usable immediately and
    fills in while it is open. After exec(), `accepted` says whether to go
    ahead, and channel()/components() hold the choice.
    """

    COMPONENTS = [
        ("manager", "Manager", "phBot's account and routine manager (runs several bots)"),
        ("plugins", "Plugins", "the Python runtime phBot plugins run on"),
        ("navmesh", "Navmesh", "walking and pathing data - needed for training areas and scripts"),
        ("minimap", "Minimap", "the minimap images shown in phBot"),
    ]
    CHANNELS = [
        ("testing", "Testing", "newest fixes first - recommended"),
        ("stable", "Stable", "fewer updates"),
    ]

    def __init__(self, parent, channel: str, components, installed: str = "", reinstall: bool = False):
        super().__init__(parent)
        self.accepted_choice = False
        self._info = {}             # channel -> {"version", "changelog", "error"}
        self._sizes = {}            # "full:<channel>" | component -> bytes
        self.setWindowTitle("phBot - install options")
        self.resize(640, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        head = QLabel("phBot - install options")
        head.setFont(theme.ui_font(13, bold=True))
        lay.addWidget(head)
        intro = QLabel(
            "phBot is downloaded directly from ProjectHax's servers (cdn.projecthax.com) - the same "
            "files phBot's own installer fetches. Pick the channel and what to install with it.")
        intro.setWordWrap(True)
        intro.setProperty("role", "muted")
        lay.addWidget(intro)

        # -- channel ------------------------------------------------------
        lay.addWidget(self._caption("CHANNEL"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self._channel_group = QButtonGroup(self)
        self._channel_btns = {}
        for key, title, hint in self.CHANNELS:
            b = QPushButton()
            b.setProperty("variant", "choice")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(54)
            b.setChecked(key == channel)
            b.toggled.connect(lambda _on=False: self._refresh())
            self._channel_group.addButton(b)
            self._channel_btns[key] = (b, title, hint)
            row.addWidget(b, 1)
        if not any(b.isChecked() for b, _, _ in self._channel_btns.values()):
            self._channel_btns["testing"][0].setChecked(True)
        lay.addLayout(row)

        self.changelog = QPlainTextEdit()
        self.changelog.setObjectName("logView")
        self.changelog.setReadOnly(True)
        self.changelog.setFont(theme.ui_font(9))
        self.changelog.setFixedHeight(110)
        lay.addWidget(self.changelog)

        # -- components ---------------------------------------------------
        lay.addWidget(self._caption("COMPONENTS"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self._checks = {}
        self._size_labels = {}
        rows = [("phbot", "phBot", "phBot.exe + phBot.dll (complete package) - always installed")]
        rows += self.COMPONENTS
        for i, (key, title, hint) in enumerate(rows):
            cb = QCheckBox(title)
            cb.setFont(theme.ui_font(10, bold=True))
            if key == "phbot":
                cb.setChecked(True)
                cb.setEnabled(False)
            else:
                cb.setChecked(key in components)
                cb.toggled.connect(lambda _on=False: self._refresh())
                self._checks[key] = cb
            grid.addWidget(cb, i, 0)
            h = QLabel(hint)
            h.setProperty("role", "muted")
            h.setWordWrap(True)
            grid.addWidget(h, i, 1)
            s = QLabel("")
            s.setProperty("role", "dim")
            s.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            s.setMinimumWidth(64)
            grid.addWidget(s, i, 2)
            self._size_labels[key] = s
        lay.addLayout(grid)

        self.total_label = QLabel("")
        self.total_label.setProperty("role", "muted")
        self.total_label.setAlignment(Qt.AlignRight)
        lay.addWidget(self.total_label)

        lay.addWidget(hline())
        notes = []
        if installed and reinstall:
            notes.append("Installed now: %s. It is downloaded again and its program files are "
                         "replaced - your configs and settings are kept." % installed)
        notes.append("The Visual C++ runtime phBot needs is installed into every Wine prefix automatically.")
        for n in notes:
            lbl = QLabel(n)
            lbl.setWordWrap(True)
            lbl.setProperty("role", "dim")
            lay.addWidget(lbl)
        lay.addStretch(1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "ghost")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.ok_btn = QPushButton("Update phBot" if reinstall else "Continue")
        self.ok_btn.setProperty("variant", "primary")
        self.ok_btn.clicked.connect(self._ok)
        btns.addWidget(self.ok_btn)
        lay.addLayout(btns)
        self._refresh()

    @staticmethod
    def _caption(text):
        lbl = QLabel(text)
        lbl.setFont(theme.ui_font(8, bold=True))
        lbl.setProperty("role", "dim")
        return lbl

    # -- data in ----------------------------------------------------------
    def set_channel_info(self, channel: str, info: dict):
        self._info[channel] = info or {}
        self._refresh()

    def set_size(self, key: str, size: int):
        self._sizes[key] = size
        self._refresh()

    # -- result -----------------------------------------------------------
    def channel(self) -> str:
        for key, (b, _, _) in self._channel_btns.items():
            if b.isChecked():
                return key
        return "testing"

    def components(self) -> list:
        return [k for k, _, _ in self.COMPONENTS if self._checks[k].isChecked()]

    def _ok(self):
        self.accepted_choice = True
        self.accept()

    # -- view -------------------------------------------------------------
    def _refresh(self):
        ch = self.channel()
        for key, (b, title, hint) in self._channel_btns.items():
            info = self._info.get(key)
            if info is None:
                ver = "checking …"
            elif info.get("error"):
                ver = "not reachable"
            else:
                ver = "v" + info.get("version", "?")
            b.setText("%s   ·   %s\n%s" % (title, ver, hint))
        info = self._info.get(ch)
        if info is None:
            self.changelog.setPlainText("Loading the changelog …")
        elif info.get("error"):
            self.changelog.setPlainText("Could not reach phBot's update server:\n%s\n\n"
                                        "You can still continue - the download is tried again "
                                        "during the install." % info["error"])
        else:
            lines = [l for l in (info.get("changelog") or []) if l.strip()]
            text = "phBot %s (%s) - changes:\n%s" % (info.get("version", "?"), ch, "\n".join(lines)) \
                if lines else "phBot %s (%s)" % (info.get("version", "?"), ch)
            self.changelog.setPlainText(text)
        total = 0
        known = True
        for key, lbl in self._size_labels.items():
            size = self._sizes.get("full:" + ch if key == "phbot" else key)
            lbl.setText(human_size(size))
            if key == "phbot" or self._checks[key].isChecked():
                if size:
                    total += size
                else:
                    known = False
        if total:
            self.total_label.setText("Download: %s%s" % ("" if known else "at least ", human_size(total)))
        else:
            self.total_label.setText("")


class TextTabsDialog(QDialog):
    """Read-only texts on tabs - the "Licenses" dialog (this app's license,
    the bundled third-party notices)."""

    def __init__(self, parent, title: str, intro: str, tabs):
        super().__init__(parent)
        from PySide6.QtWidgets import QTabWidget
        self.setWindowTitle(title)
        self.resize(760, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        head = QLabel(title)
        head.setFont(theme.ui_font(13, bold=True))
        lay.addWidget(head)
        intro_lbl = QLabel(intro)
        intro_lbl.setWordWrap(True)
        intro_lbl.setProperty("role", "muted")
        lay.addWidget(intro_lbl)
        tw = QTabWidget()
        for name, text in tabs:
            body = QPlainTextEdit()
            body.setObjectName("logView")
            body.setReadOnly(True)
            body.setFont(theme.mono_font(9))
            body.setPlainText(text)
            tw.addTab(body, name)
        lay.addWidget(tw, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
