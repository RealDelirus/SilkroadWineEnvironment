#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Progress tracking for a swe.sh run - overall, per-step, and the wine build.

No widgets here on purpose: this is pure bookkeeping over the lines swe.sh
prints, so it can be reasoned about (and tested) without a running GUI.

Where the numbers come from
---------------------------
swe.sh has no percentage to report - it is a shell script running a package
manager and a 20-60 minute compile. So progress is reconstructed from two
signals the bash side already produces:

* `>>> STEP:` / `>>> DONE:` / `>>> FAIL n:` - the step boundaries run_step()
  emits when its output is piped (lib/common.sh). These drive the overall bar.
* step()'s `### ...` and say()'s `==> ...` phase lines from inside a step.
  These drive the per-step bar for the wine build, whose phases are known.

For the one step where neither is enough - `make` inside the wine build, which
alone is ~78% of a first install and prints nothing to stdout at all because
lib/build-wine-sro.sh redirects it to build.log - WineBuildProbe tails that
log file instead. That redirect is deliberate on the bash side (it keeps the
terminal spinner readable), so it is read rather than removed.

Nothing here is authoritative: the stream always wins over the prediction. If
a step shows up that was not planned, it is folded in and the total is
renormalised, so a wrong guess degrades the estimate instead of breaking it.
"""
from __future__ import annotations

import os
import re
import time

STEP_RE = re.compile(r'^>>> STEP: (.*)$')
DONE_RE = re.compile(r'^>>> DONE: (.*)$')
FAIL_RE = re.compile(r'^>>> FAIL (\d+): (.*)$')
PHASE_RE = re.compile(r'^###\s*(.+?)\s*$')
SAY_RE = re.compile(r'^==>\s*(.+?)\s*$')
# lib/phbot-fetch.py's progress lines (see its docstring for the format).
DL_RE = re.compile(
    r'^>>> DL: (?P<name>\S+) \[(?P<n>\d+)/(?P<of>\d+)\] (?P<got>[\d.]+)/(?P<size>[\d.?]+) MB '
    r'(?P<pct>\d+)% (?P<rate>.+?) ETA (?P<eta>\S+) \| total (?P<all_got>[\d.]+)/(?P<all_size>[\d.?]+) MB '
    r'(?P<all_pct>\d+)% ETA (?P<all_eta>\S+)$')
EXTRACT_RE = re.compile(r'^>>> EXTRACT: (?P<name>\S+) (?P<n>\d+)/(?P<of>\d+) files (?P<pct>\d+)%$')

# Rough wall-clock weights, only ever used RELATIVE to each other. The wine
# build dominates so heavily that an unweighted "step 2 of 4" bar would sit
# still for 40 minutes, which is exactly the feedback gap this is closing.
WEIGHTS = {
    "deps":      4,
    "prereq":    5,
    "wine":     75,
    "maxiguard": 8,
    "mg_prefix": 4,
    "vsroplus":  3,
    "phbot":    10,     # now includes the download (~250 MB with every component)
    "client":    4,
    "other":     3,
}

# Human labels for the step kinds, for the "Step 2 of 4 - <name>" line.
KIND_LABEL = {
    "deps":      "Build dependencies",
    "prereq":    "MaxiGuard prerequisites",
    "wine":      "Build wine-sro",
    "maxiguard": "MaxiGuard environment",
    "mg_prefix": "MaxiGuard prefix",
    "vsroplus":  "vSroPlus",
    "phbot":     "phBot",
    "client":    "Client setup",
    "other":     "Step",
}


def classify(label: str) -> str:
    """Map a run_step label to a step kind.

    Matched on substrings rather than exact strings because several labels
    embed a path (`Setting up MaxiGuard (/games/sro)`) or a duration hint
    (`Building wine-sro (~20-60 min)`).
    """
    l = label.lower()
    if "dependencies" in l:
        return "deps"
    # Before the plain "maxiguard" test below: this one is the privileged
    # prerequisites step (umu-launcher + 32-bit runtime), not the MaxiGuard
    # environment itself, and giving them the same kind would make one step
    # eat the other's weight.
    if "prerequisites" in l:
        return "prereq"
    if "wine-sro" in l:
        return "wine"
    if "maxiguard prefix" in l or "recreating maxiguard" in l:
        return "mg_prefix"
    if "maxiguard" in l:
        return "maxiguard"
    if "vsroplus" in l:
        return "vsroplus"
    if "phbot" in l:
        return "phbot"
    if "client" in l or "game folder" in l:
        return "client"
    return "other"


def plan_from_args(args, status=None):
    """Predict the step kinds a `swe.sh <args>` run will execute.

    Only an estimate for the *forward-looking* part of the bar - RunTracker
    corrects it against the actual >>> STEP: lines as they arrive. It
    deliberately does not try to re-derive swe.sh's full flag logic; that
    lives in swe.sh and stays there.
    """
    status = status or {}
    a = list(args)
    plan = []

    def has(*flags):
        return any(f in a for f in flags)

    if has("--uninstall", "--remove-maxiguard", "--toggle-wined3d"):
        return ["other"]

    if not has("--skip-deps", "--wine-only"):
        plan.append("deps")
    # swe.sh runs the privileged MaxiGuard prerequisites right after the
    # dependencies, not next to the MaxiGuard setup itself - see its
    # flag-driven section.
    if has("--maxiguard"):
        plan.append("prereq")
    if not has("--skip-wine", "--deps-only") and not status.get("wine"):
        plan.append("wine")
    if has("--deps-only"):
        return plan
    if has("--recreate-maxiguard-prefix"):
        plan.append("mg_prefix")
    if has("--maxiguard"):
        plan.append("maxiguard")
    if has("--vsroplus"):
        plan.append("vsroplus")
    if has("--games"):
        plan.append("client")
    if has("--phbot"):
        plan.append("phbot")
    return plan or ["other"]


class RunTracker:
    """Folds the output of one swe.sh run into progress numbers.

    feed(line) returns True when anything visible changed, so the caller can
    avoid repainting on every one of the thousands of compiler lines.
    """

    def __init__(self, planned_kinds, wine_probe=None):
        self.planned = list(planned_kinds) or ["other"]
        self.wine_probe = wine_probe
        self.done_kinds = []        # kinds already completed, in order
        self.current = None         # kind of the running step
        self.current_label = ""
        self._last_name = ""        # kept across the gap between two steps
        self.phase = ""
        self.failed = False
        self._overall = 0.0         # 0..1, monotonic
        self._step = None           # 0..1 or None (= indeterminate)
        self._eta = None            # seconds or None
        self._note = ""             # replaces the ETA text (download rate + time left)
        self._started = time.time()

    # -- weights ----------------------------------------------------------
    def _remaining_plan(self):
        """Planned kinds not yet done and not currently running."""
        rest = list(self.planned)
        for k in self.done_kinds:
            if k in rest:
                rest.remove(k)
        if self.current and self.current in rest:
            rest.remove(self.current)
        return rest

    def _total_weight(self):
        w = sum(WEIGHTS.get(k, 3) for k in self.done_kinds)
        if self.current:
            w += WEIGHTS.get(self.current, 3)
        w += sum(WEIGHTS.get(k, 3) for k in self._remaining_plan())
        return max(1, w)

    # -- input ------------------------------------------------------------
    def feed(self, line: str) -> bool:
        m = STEP_RE.match(line)
        if m:
            self._begin_step(m.group(1))
            return True
        m = DONE_RE.match(line)
        if m:
            self._end_step(ok=True)
            return True
        m = FAIL_RE.match(line)
        if m:
            self._end_step(ok=False)
            return True

        m = PHASE_RE.match(line)
        if m:
            self.phase = m.group(1)
            self._feed_phase(line)
            return True
        m = SAY_RE.match(line)
        if m:
            # say() lines are the finer-grained phases inside a step; only the
            # wine build and phBot (prefix, VC++ runtime, download, unpack)
            # have enough of them to be worth showing as progress.
            if self.current == "wine":
                self.phase = m.group(1)
                self._feed_phase(line)
                return True
            if self.current == "phbot":
                self.phase = m.group(1)
                self._note = ""
                return True
        if self.current == "phbot":
            m = DL_RE.match(line)
            if m:
                self._feed_download(m)
                return True
            m = EXTRACT_RE.match(line)
            if m:
                self.phase = "Unpacking %s · %s/%s files" % (m.group("name"), m.group("n"), m.group("of"))
                self._note = "%s %%" % m.group("pct")
                # Unpacking follows the downloads: the last stretch of the step.
                self._step = max(self._step or 0.0, DL_SPAN[1])
                return True
        return False

    def _feed_download(self, m):
        """One phBot download progress line -> step fraction, phase, note."""
        all_pct = int(m.group("all_pct"))
        lo, hi = DL_SPAN
        self._step = max(self._step or 0.0, lo + (hi - lo) * all_pct / 100.0)
        size = m.group("size")
        self.phase = "Downloading %s (%s of %s) · %s / %s MB" % (
            m.group("name"), m.group("n"), m.group("of"), m.group("got"), size)
        eta = parse_clock(m.group("all_eta"))
        self._eta = eta
        # Exact (m:ss) rather than format_eta()'s rounded minutes: a download
        # is short enough that "almost done" would be all it ever says.
        left = "%s left" % m.group("all_eta") if eta is not None else ""
        self._note = " · ".join(x for x in (m.group("rate"), left) if x)

    def _feed_phase(self, line):
        if self.current == "wine" and self.wine_probe is not None:
            self.wine_probe.feed(line)
            # Pull the new numbers straight away rather than leaving it to the
            # caller's next refresh(): a phase line IS the moment the value
            # changes, and requiring a separate call afterwards is an ordering
            # trap every call site would have to remember.
            self.refresh()

    def _begin_step(self, label):
        if self.current:                       # a step without its DONE line
            self._end_step(ok=True)
        kind = classify(label)
        self.current = kind
        self.current_label = label
        self.phase = ""
        self._step = None
        self._eta = None
        self._note = ""
        # Steps the plan expected before this one never ran (a --skip-*, or an
        # st_* check that found it already done): drop them so the bar does
        # not keep reserving room for work that will not happen.
        if kind in self.planned:
            idx = self.planned.index(kind)
            for skipped in self.planned[:idx]:
                if skipped not in self.done_kinds:
                    self.planned.remove(skipped)
        else:
            self.planned.insert(0, kind)       # unplanned step - fold it in
        if kind == "wine" and self.wine_probe is not None:
            self.wine_probe.start()

    def _end_step(self, ok: bool):
        if not self.current:
            return
        if self.current == "wine" and self.wine_probe is not None:
            self.wine_probe.finish(ok)
        if ok:
            self.done_kinds.append(self.current)
            if self.current in self.planned:
                self.planned.remove(self.current)
        else:
            self.failed = True
        self.current = None
        self.phase = ""
        self._step = 1.0 if ok else self._step
        self._eta = None
        self._note = ""

    # -- output -----------------------------------------------------------
    def refresh(self) -> bool:
        """Re-poll the time-based sources (the wine build log). Returns True
        if the numbers moved."""
        if self.current == "wine" and self.wine_probe is not None:
            frac, eta = self.wine_probe.poll()
            changed = (frac != self._step) or (eta != self._eta)
            self._step, self._eta = frac, eta
            return changed
        return False

    def overall(self) -> float:
        total = self._total_weight()
        w = sum(WEIGHTS.get(k, 3) for k in self.done_kinds)
        if self.current:
            frac = self._step if self._step is not None else 0.0
            w += WEIGHTS.get(self.current, 3) * max(0.0, min(1.0, frac))
        value = w / float(total)
        # Monotonic: renormalising the total when an unplanned step appears
        # could otherwise make the bar jump backwards, which reads as a bug.
        self._overall = max(self._overall, min(1.0, value))
        return self._overall

    def step_fraction(self):
        """0..1, or None when this step has nothing measurable to report."""
        return self._step

    def eta_seconds(self):
        return self._eta

    def step_note(self) -> str:
        """Text for the step bar's value label, when the step has something
        better to say than a percentage (the download rate, for phBot)."""
        return self._note if self.current else ""

    def has_steps(self) -> bool:
        """Whether this run ever announced a step boundary.

        Some swe.sh flags (--uninstall, --toggle-wined3d, ...) are single
        early-exit operations that run outside the phase()-wrapped section,
        so they emit no markers at all. Reporting 0% for their whole duration
        would be a claim, not a measurement - the caller shows an
        indeterminate bar instead.
        """
        return bool(self.done_kinds or self.current)

    def step_position(self):
        """(n, total) for 'Step n of total'."""
        total = len(self.done_kinds) + (1 if self.current else 0) + len(self._remaining_plan())
        n = len(self.done_kinds) + (1 if self.current else 0)
        return max(1, n), max(1, total)

    def step_name(self) -> str:
        if self.current:
            self._last_name = KIND_LABEL.get(self.current, self.current_label)
        return self._last_name

    def finish_all(self):
        self._overall = 1.0
        self._step = 1.0
        self._eta = None


# --------------------------------------------------------------- wine build
# Cumulative start offset of each phase within the wine step, in percent.
# Derived from the order of lib/build-wine-sro.sh; `make` is the bulk of it.
# Measured against a real build (32 cores): configure 12 s, make 199 s,
# make install 87 s. The split is machine-dependent in a way no single set of
# weights can capture - `make` parallelises across cores while `make install`
# is I/O-bound and barely does - so on the 4-8 core machines this targets
# (the ones where the build really does take the advertised 20-60 min) make
# dominates far more than it did in that measurement. These weights lean
# towards that slower case, and the per-machine calibration in
# WineBuildProbe corrects the remaining error after the first build.
WINE_PHASES = [
    ("prep",      0.0,  "Preparing"),
    ("download",  1.0,  "Downloading Wine sources"),
    ("extract",   5.0,  "Extracting"),
    ("patch",     7.0,  "Applying patches"),
    ("configure", 8.0,  "configure"),
    ("make",     13.0,  "Compiling"),
    ("install",  87.0,  "Installing"),
    ("fix",      97.0,  "Export fix"),
]
_WINE_OFFSET = dict((k, v) for k, v, _ in WINE_PHASES)
_WINE_TITLE = dict((k, t) for k, _, t in WINE_PHASES)
MAKE_SPAN = 87.0 - 13.0     # percent of the wine step that `make` occupies
TAIL_SPAN = 100.0 - 87.0    # everything after make (install + export fix)

# What `make` actually does, split into the part that can be counted and the
# part that cannot. Compiling is ~9800 measurable invocations; after that comes
# linking (winebuild/winegcc producing the import libs and modules), which has
# no comparable up-front target count. The compile fraction therefore drives
# the bar up to COMPILE_SHARE of the make span, and the rest is covered by the
# calibrated link duration once one is known.
MAKE_COMPILE_SHARE = 0.88

# One compile invocation in lib/build-wine-sro.sh's `make ... > build.log`.
# NOT a line: Wine's compile commands are backslash-continued across ~7 lines
# each, so counting lines overstates the work by that factor and pins the bar
# at its clamp within the first minute. Verified against a real build: 9641
# occurrences of this token, 9641 matching lines, none in a continuation line.
COMPILE_TOKEN = b" -c -o "

_WINE_MARKERS = [
    ("prep",      re.compile(r'^###\s*Building wine-sro')),
    ("download",  re.compile(r'^==>\s*Downloading Wine sources')),
    ("extract",   re.compile(r'^==>\s*Extracting')),
    ("patch",     re.compile(r'^==>\s*(Applying dxdiagn|mountmgr patch)')),
    ("configure", re.compile(r'^==>\s*running configure')),
    ("make",      re.compile(r'^==>\s*building Wine')),
    ("install",   re.compile(r'^==>\s*installing into')),
    ("fix",       re.compile(r'^###\s*Fix 1:')),
]


class WineBuildProbe:
    """Progress + remaining time for the wine-sro build.

    Phase granularity comes from the build script's own output; within `make`
    (which prints nothing to stdout) it counts lines appended to build.log.

    The expected line count is self-calibrating: after a successful build the
    real figure is stored per wine version, so every later rebuild on this
    machine is accurate. The first build has no reference, so it falls back to
    a source-file count, and if even that is unavailable the bar simply stays
    indeterminate for the make phase - no path depends on the guess.
    """

    def __init__(self, build_cache: str, wine_version: str, store=None):
        self.build_cache = build_cache or ""
        self.wine_version = wine_version or ""
        self.store = store
        self.src_dir = ""
        self.build_log = ""
        if self.build_cache and self.wine_version:
            self.src_dir = os.path.join(self.build_cache, "wine-src",
                                        "wine-" + self.wine_version)
            self.build_log = os.path.join(self.src_dir, "build", "build.log")
        self.reset()

    def reset(self):
        self.phase = None
        self.started = 0.0
        self.make_started = 0.0
        self._offset = 0
        self._units = 0
        self._carry = b""
        self._expected = 0
        self._compiles_done_at = 0.0
        self._eta = None
        self._last_pct = 0.0

    # -- lifecycle --------------------------------------------------------
    def start(self):
        self.reset()
        self.started = time.time()
        self._expected = self._load_expected()

    def feed(self, line: str):
        for name, rx in _WINE_MARKERS:
            if rx.match(line):
                if name != self.phase:
                    self.phase = name
                    if name == "make":
                        self.make_started = time.time()
                        self._offset = 0
                        self._units = 0
                        self._carry = b""
                        if not self._expected:
                            self._expected = self._count_makefile_targets()
                return

    def finish(self, ok: bool):
        if ok and self.started:
            self._save_calibration(self._units, time.time() - self.started)
        self.phase = None

    # -- measurement ------------------------------------------------------
    def _count_new_units(self):
        """Compile invocations appended since the last poll.

        Reads only the newly appended bytes: build.log grows past 100 MB, and
        re-reading it every two seconds would be pure waste.
        """
        if not self.build_log:
            return
        try:
            size = os.path.getsize(self.build_log)
        except OSError:
            return
        if size < self._offset:      # rebuilt from scratch - log was truncated
            self._offset = 0
            self._units = 0
            self._carry = b""
        if size <= self._offset:
            return
        try:
            with open(self.build_log, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError:
            return
        # A token can straddle a read boundary, so the tail of the previous
        # chunk is prepended. Keeping exactly len(token)-1 bytes means that
        # carry can never contain a whole token on its own, so nothing is
        # counted twice.
        buf = self._carry + chunk
        self._units += buf.count(COMPILE_TOKEN)
        self._carry = buf[-(len(COMPILE_TOKEN) - 1):]
        self._offset = size

    def poll(self):
        """(fraction 0..1 or None, eta seconds or None) for the wine step."""
        if self.phase is None:
            return None, None
        base = _WINE_OFFSET.get(self.phase, 0.0)
        pct = base
        make_frac = None

        if self.phase == "make":
            self._count_new_units()
            if self._expected > 0:
                compiled = max(0.0, min(1.0, self._units / float(self._expected)))
                make_frac = compiled * MAKE_COMPILE_SHARE
                if compiled >= 0.995:
                    # Compiling is done; linking is what is left. It cannot be
                    # counted the same way, so it is carried by the measured
                    # link duration from a previous build - and if there is
                    # none yet, the bar simply waits here rather than
                    # inventing movement.
                    if not self._compiles_done_at:
                        self._compiles_done_at = time.time()
                    link_total = self._load_calibration("linkSeconds")
                    if link_total:
                        elapsed = time.time() - self._compiles_done_at
                        link_frac = max(0.0, min(1.0, elapsed / link_total))
                        make_frac += (1.0 - MAKE_COMPILE_SHARE) * link_frac
                # Clamped below 1.0 so the bar never reads "finished" while
                # make is still running - the DONE line is what completes it.
                make_frac = max(0.0, min(0.99, make_frac))
                pct = base + MAKE_SPAN * make_frac

        pct = max(self._last_pct, pct)      # never go backwards
        self._last_pct = pct
        self._update_eta(make_frac)
        return pct / 100.0, self._eta

    def _update_eta(self, make_frac):
        raw = None
        calib = self._load_calibration_seconds()
        if calib and self.started:
            raw = max(0.0, calib - (time.time() - self.started))
        if self.phase == "make" and make_frac and make_frac > 0.02:
            elapsed = time.time() - self.make_started
            if elapsed >= 60:
                # Project make's total from how far it has come, then add the
                # post-make tail at the same observed rate.
                projected_make = elapsed / make_frac
                measured = projected_make * (1.0 - make_frac) \
                    + projected_make * (TAIL_SPAN / MAKE_SPAN)
                raw = measured if raw is None else (0.35 * raw + 0.65 * measured)
        if raw is None:
            self._eta = None
            return
        # Exponential smoothing so the number does not jitter every two
        # seconds; it is an estimate and should read like one.
        self._eta = raw if self._eta is None else (0.8 * self._eta + 0.2 * raw)

    def phase_title(self) -> str:
        return _WINE_TITLE.get(self.phase, "")

    # -- calibration ------------------------------------------------------
    def _key(self, what):
        return "wineBuild/%s/%s" % (self.wine_version or "unknown", what)

    def _load_expected(self) -> int:
        # "compileUnits", NOT the older "makeLines": that key held a raw line
        # count, which is ~7x larger for the same amount of work. Reusing the
        # name would have had a machine that ran the previous version read its
        # stored line count as an invocation count and sit near zero for the
        # whole build.
        return int(self._load_calibration("compileUnits") or 0)

    def _load_calibration(self, what):
        if self.store is None:
            return None
        try:
            v = float(self.store.value(self._key(what), 0))
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    def _load_calibration_seconds(self):
        return self._load_calibration("totalSeconds")

    def _save_calibration(self, units, seconds):
        if self.store is None or units <= 0:
            return
        try:
            self.store.setValue(self._key("compileUnits"), int(units))
            self.store.setValue(self._key("totalSeconds"), float(seconds))
            if self._compiles_done_at:
                self.store.setValue(self._key("linkSeconds"),
                                    float(max(1.0, time.time() - self._compiles_done_at)))
            # Flush now rather than whenever the store happens to be
            # destroyed: the whole point is that the NEXT build reads this,
            # and the app usually keeps running for a while afterwards.
            self.store.sync()
        except Exception:
            pass

    def _count_makefile_targets(self) -> int:
        """How many objects this build will compile, for the first build.

        Read off the Makefile configure just generated, which lists every
        object target explicitly - an exact figure rather than a guess, and it
        accounts for the two PE architectures (--enable-archs=i386,x86_64)
        without having to model them. Measured against a real build: 9686 from
        this pattern vs 9792 objects actually compiled, i.e. ~1% low.

        Only line-start targets are matched, which costs ~0.1s on the 25 MB
        Makefile instead of ~0.4s for every .o token anywhere in it - the
        extra precision is not worth four times the one-off hitch.
        """
        if not self.src_dir:
            return 0
        path = os.path.join(self.src_dir, "build", "Makefile")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return 0
        try:
            return len(set(re.findall(rb'(?m)^([A-Za-z0-9_/.-]+\.o):', data)))
        except Exception:
            return 0


# Share of the phBot step the downloads cover: before them come the prefix and
# the VC++ runtime (no measurable progress), after them the unpacking and the
# user32 patch.
DL_SPAN = (0.10, 0.85)


def parse_clock(text: str):
    """'1:02:03' / '0:14' -> seconds; None for '--:--'."""
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def format_eta(seconds) -> str:
    if seconds is None:
        return ""
    if seconds < 45:
        return "almost done"
    minutes = int(round(seconds / 60.0))
    if minutes < 1:
        return "almost done"
    if minutes == 1:
        return "~1 min left"
    if minutes < 60:
        return "~%d min left" % minutes
    h, m = divmod(minutes, 60)
    return "~%d:%02d h left" % (h, m)


def format_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)
