#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""lib/phbot-fetch.py - download phBot straight from ProjectHax's CDN.

This replaces running phBot's own Windows installer under Wine. The installer
is only a downloader: it reads the same update.json this script reads
(https://cdn.projecthax.com/<channel>/update.json) and unpacks what it lists.
Doing that natively means no .NET installer window under Wine, no background
continuation to poll for, and real progress (bytes, rate, time left).

What ends up where mirrors the official installer, so everything that already
looks for phBot (find_phbot_exe, find_manager_exe, the generated launchers)
keeps working unchanged:

    <programs>/phBot Testing|Stable/   phBot.exe, phBot.dll, ... ("full" zip)
        Plugins/                       plugins zip (bundled Python runtime)
        navmesh/                       navmesh zip
        minimap/                       minimap zip
    <programs>/Manager/                Manager.exe (manager2 channel)

Downloads are cached (--cache) and only fetched again when the server's copy
changed. Files never replace what is installed until they are complete:
phBot.exe/phBot.dll are checked against update.json's sha256 and fetched one by
one if the full package's copies do not match.

Progress: on a terminal a live one-line bar; otherwise (the GUI, the menu's
spinner, a log) one `>>> DL:` line per second (file, then all downloads):

    >>> DL: navmesh.zip [3/5] 45.2/120.0 MB 37% 5.3 MB/s ETA 0:14 | total 118.0/300.0 MB 39% ETA 0:34

and `>>> EXTRACT: navmesh.zip 1200/6234 files 19%` while unpacking.
gui/progress.py parses exactly these two formats.

The downloads go through curl (a hard dependency of this project already), so
proxies, TLS and retries behave exactly like every other download here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

DEFAULT_CDN = "https://cdn.projecthax.com"
# The Manager the current installer offers; "manager" is the older 2.x line.
MANAGER_CHANNEL = "manager2"
COMPONENTS = ("manager", "plugins", "navmesh", "minimap")

IS_TTY = sys.stdout.isatty()


def say(msg: str) -> None:
    c, r = ("\033[1;32m", "\033[0m") if IS_TTY else ("", "")
    print("%s==>%s %s" % (c, r, msg), flush=True)


def warn(msg: str) -> None:
    c, r = ("\033[1;33m", "\033[0m") if sys.stderr.isatty() else ("", "")
    print("%s!!%s  %s" % (c, r, msg), file=sys.stderr, flush=True)


class FetchError(Exception):
    pass


# ------------------------------------------------------------------ http (curl)
def curl_text(url: str, timeout: int = 30) -> str:
    try:
        out = subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "--connect-timeout", "20",
             "--max-time", str(timeout), url],
            check=True, capture_output=True)
    except FileNotFoundError:
        raise FetchError("curl is not installed")
    except subprocess.CalledProcessError as e:
        raise FetchError("could not fetch %s (%s)" % (url, e.stderr.decode(errors="replace").strip()))
    return out.stdout.decode("utf-8", errors="replace")


def curl_head(url: str) -> dict:
    """Size and validators of a URL, following redirects. {} when unknown."""
    try:
        out = subprocess.run(
            ["curl", "-sSIL", "--retry", "2", "--connect-timeout", "20", "--max-time", "30", url],
            check=True, capture_output=True).stdout.decode("latin-1", errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    info = {}
    # With -L every hop prints its own header block - only the last one is
    # the file itself.
    for line in out.replace("\r", "").split("\n"):
        if line.upper().startswith("HTTP/"):
            info = {"status": line.split()[1] if len(line.split()) > 1 else ""}
        elif ":" in line:
            k, v = line.split(":", 1)
            info[k.strip().lower()] = v.strip()
    if info.get("status") != "200":
        return {}
    res = {}
    try:
        res["size"] = int(info.get("content-length", ""))
    except ValueError:
        pass
    for k in ("etag", "last-modified"):
        if info.get(k):
            res[k] = info[k]
    return res


# ------------------------------------------------------------------ progress
def human_mb(n: float) -> str:
    return "%.1f" % (n / 1048576.0)


def human_rate(bps: float) -> str:
    if bps >= 1048576:
        return "%.1f MB/s" % (bps / 1048576.0)
    return "%.0f KB/s" % (bps / 1024.0)


def human_eta(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


class Progress:
    """Tracks bytes across all downloads of one run and prints them."""

    def __init__(self, total_bytes: int, count: int):
        self.total = max(0, total_bytes)
        self.count = count
        self.done_before = 0          # bytes of finished downloads
        self.samples = []             # (time, bytes) of the running download
        self._last_print = 0.0

    def rate(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def update(self, name: str, idx: int, got: int, size: int, final: bool = False):
        now = time.time()
        self.samples.append((now, got))
        # A ~4 s window: steady enough to read, still reacts to a slowdown.
        while len(self.samples) > 2 and now - self.samples[0][0] > 4.0:
            self.samples.pop(0)
        if not final and now - self._last_print < (0.2 if IS_TTY else 1.0):
            return
        self._last_print = now
        rate = self.rate()
        eta = (size - got) / rate if (rate > 0 and size) else None
        pct = min(100, int(got * 100 / size)) if size else 0
        all_got = self.done_before + got
        all_pct = min(100, int(all_got * 100 / self.total)) if self.total else pct
        all_eta = (self.total - all_got) / rate if (rate > 0 and self.total) else None
        if final:
            pct, eta = 100, 0
        text = "%s [%d/%d] %s/%s MB %d%% %s ETA %s | total %s/%s MB %d%% ETA %s" % (
            name, idx, self.count, human_mb(got), human_mb(size) if size else "?",
            pct, human_rate(rate), human_eta(eta),
            human_mb(all_got), human_mb(self.total) if self.total else "?", all_pct,
            human_eta(all_eta))
        if IS_TTY:
            width = shutil.get_terminal_size((100, 20)).columns
            barw = 24
            fill = int(barw * pct / 100)
            line = "   %s [%d/%d] [%s%s] %3d%%  %s/%s MB  %s  %s left" % (
                name, idx, self.count, "#" * fill, "." * (barw - fill), pct,
                human_mb(got), human_mb(size) if size else "?", human_rate(rate), human_eta(eta))
            sys.stdout.write("\r\033[2K" + line[:width - 1])
            if final:
                sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            print(">>> DL: " + text, flush=True)

    def finish_file(self, got: int):
        self.done_before += got
        self.samples = []


# ------------------------------------------------------------------ download
class Item:
    def __init__(self, key, name, url, size=0, sha256="", title=""):
        self.key = key            # full | manager | plugins | navmesh | minimap | file
        self.name = name          # file name shown in progress / used in the cache
        self.url = url
        self.size = size
        self.sha256 = sha256.lower() if sha256 else ""
        self.title = title or name
        self.meta = {}
        self.path = ""


def _meta_path(path):
    return path + ".meta"


def _cache_valid(item: Item, path: str) -> bool:
    """Is the cached copy the same file the server has right now?"""
    if not os.path.isfile(path):
        return False
    if item.sha256:
        return sha256_file(path) == item.sha256
    try:
        with open(_meta_path(path)) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return False
    if meta.get("url") != item.url:
        return False
    size = os.path.getsize(path)
    if item.meta.get("size") is not None and item.meta["size"] != size:
        return False
    for k in ("etag", "last-modified"):
        if item.meta.get(k) and meta.get(k) and item.meta[k] != meta[k]:
            return False
    # Nothing from the server to compare against (HEAD failed): reuse only a
    # file whose recorded size still matches - better than a pointless re-fetch.
    return meta.get("size") == size


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(item: Item, dest: str, prog: Progress, idx: int) -> None:
    part = dest + ".part"
    size = item.meta.get("size") or item.size or 0
    # -C - resumes a .part left behind by an interrupted run.
    cmd = ["curl", "-fsSL", "--retry", "3", "--retry-delay", "2", "--connect-timeout", "20",
           "-C", "-", "-o", part, item.url]
    for attempt in (1, 2):
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while proc.poll() is None:
            got = os.path.getsize(part) if os.path.exists(part) else 0
            prog.update(item.name, idx, got, size)
            time.sleep(0.2)
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.returncode == 0:
            break
        # 33 = the server refuses the resume range; start over once.
        if attempt == 1 and proc.returncode in (33, 36) and os.path.exists(part):
            os.unlink(part)
            continue
        raise FetchError("download failed: %s (%s)" % (item.url, err or "curl exit %d" % proc.returncode))
    got = os.path.getsize(part)
    prog.update(item.name, idx, got, size or got, final=True)
    prog.finish_file(got)
    if size and got != size:
        os.unlink(part)
        raise FetchError("%s: got %d bytes, expected %d" % (item.name, got, size))
    if item.sha256:
        digest = sha256_file(part)
        if digest != item.sha256:
            os.unlink(part)
            raise FetchError("%s: sha256 mismatch (got %s, expected %s)" % (item.name, digest, item.sha256))
    os.replace(part, dest)
    meta = {"url": item.url, "size": got}
    for k in ("etag", "last-modified"):
        if item.meta.get(k):
            meta[k] = item.meta[k]
    with open(_meta_path(dest), "w") as fh:
        json.dump(meta, fh)


# ------------------------------------------------------------------ extract
def _safe_members(zf: zipfile.ZipFile):
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
            if parts:
                raise FetchError("unsafe path in %s: %r" % (zf.filename, info.filename))
            continue
        yield info, parts


def extract(zip_path: str, dest: str, strip_dir=None, marker=None, wrap=None) -> int:
    """Unpack zip_path into dest, overwriting files but never deleting any.

    strip_dir: if every entry sits below ONE top-level folder whose name is
               in this set (case-insensitive), that folder level is dropped -
               e.g. navmesh.zip holding navmesh/<files> lands in dest itself.
    marker:    a file that belongs directly in dest (e.g. phBot.exe): if it is
               found one folder down instead, that folder level is dropped.
    wrap:      folder to put the contents in when the zip has no top-level
               folder of its own (the plugins runtime lives in Plugins/<wrap>).
    Returns the number of files written.
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise FetchError("%s is not a valid zip file" % os.path.basename(zip_path))
    with zf:
        members = list(_safe_members(zf))
        tops = {parts[0] for _, parts in members}
        files = [(i, p) for i, p in members if not i.is_dir()]
        one_folder = len(tops) == 1 and all(len(p) > 1 for _, p in files)
        drop = 0
        if one_folder:
            top = next(iter(tops)).lower()
            if strip_dir and top in {s.lower() for s in strip_dir}:
                drop = 1
            elif marker and any(len(p) == 2 and p[1].lower() == marker.lower() for _, p in files):
                drop = 1
        prefix = [wrap] if (wrap and not one_folder) else []
        total = len(files)
        written = 0
        last = 0.0
        for info, parts in files:
            rel = prefix + parts[drop:]
            if not rel:
                continue
            target = os.path.join(dest, *rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Written next to the target and renamed over it: a running phBot
            # keeps its old (mapped) file, and a crash never leaves half a DLL.
            tmp = target + ".swe-new"
            with zf.open(info) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.replace(tmp, target)
            if (info.external_attr >> 16) & 0o111:
                os.chmod(target, 0o755)
            written += 1
            now = time.time()
            if now - last > (0.2 if IS_TTY else 1.0) or written == total:
                last = now
                pct = int(written * 100 / total) if total else 100
                if IS_TTY:
                    sys.stdout.write("\r\033[2K   extracting %s  %d/%d files  %d%%" % (
                        os.path.basename(zip_path), written, total, pct))
                    if written == total:
                        sys.stdout.write("\n")
                    sys.stdout.flush()
                else:
                    print(">>> EXTRACT: %s %d/%d files %d%%" % (
                        os.path.basename(zip_path), written, total, pct), flush=True)
        return written


# ------------------------------------------------------------------ main flow
def load_manifest(cdn: str, channel: str) -> dict:
    url = "%s/%s/update.json" % (cdn.rstrip("/"), channel)
    try:
        data = json.loads(curl_text(url))
    except ValueError:
        raise FetchError("%s is not valid JSON" % url)
    if not isinstance(data, dict) or not data.get("version"):
        raise FetchError("%s has no version" % url)
    return data


def plan(cdn: str, channel: str, components) -> tuple:
    """(bot manifest, manager manifest or None, [Item...]) for this run."""
    bot = load_manifest(cdn, channel)
    items = []
    if bot.get("full"):
        items.append(Item("full", os.path.basename(bot["full"]), bot["full"],
                          title="phBot %s (%s)" % (bot["version"], channel)))
    else:
        for f in bot.get("win") or []:
            items.append(Item("file", f["name"], f["url"], f.get("size", 0), f.get("sha256", "")))
    mgr = None
    if "manager" in components:
        try:
            mgr = load_manifest(cdn, MANAGER_CHANNEL)
        except FetchError as e:
            warn("Manager: %s - skipped." % e)
        if mgr:
            win = [f for f in (mgr.get("win") or []) if f.get("name", "").lower() == "manager.exe"]
            if win:
                f = win[0]
                items.append(Item("manager", f["name"], f["url"], f.get("size", 0), f.get("sha256", ""),
                                  title="Manager %s" % mgr["version"]))
            elif mgr.get("full"):
                items.append(Item("manager", "manager-%s.zip" % mgr["version"], mgr["full"],
                                  title="Manager %s" % mgr["version"]))
    for key, field in (("plugins", "python"), ("navmesh", "navmesh"), ("minimap", "minimap")):
        if key in components:
            if bot.get(field):
                items.append(Item(key, os.path.basename(bot[field]), bot[field]))
            else:
                warn("update.json has no %s package - %s skipped." % (field, key))
    return bot, mgr, items


def cache_name(item: Item, channel: str, bot_version: str) -> str:
    if item.key in ("full", "file"):
        return os.path.join(channel, bot_version, item.name)
    if item.key == "manager":
        return os.path.join("manager", item.url.rstrip("/").split("/")[-2], item.name)
    return os.path.join("data", item.name)


def install(args) -> int:
    raw = (args.components or "").replace(" ", "").lower()
    raw = {"all": ",".join(COMPONENTS), "none": ""}.get(raw, raw)
    components = [c for c in raw.split(",") if c]
    for c in components:
        if c not in COMPONENTS:
            raise FetchError("unknown component %r (known: %s)" % (c, ", ".join(COMPONENTS)))
    channel = args.channel
    title = channel.capitalize()
    bot_dir = os.path.join(args.programs, "phBot " + title)
    mgr_dir = os.path.join(args.programs, "Manager")

    say("Reading phBot's update data (%s channel) ..." % channel)
    bot, mgr, items = plan(args.cdn, channel, components)
    say("phBot %s (%s)%s" % (bot["version"], channel,
                              "  +  Manager %s" % mgr["version"] if mgr else ""))
    picked = ["phBot"] + [c.capitalize() for c in components]
    say("Components: " + ", ".join(picked))

    # Sizes up front: they give the overall bar its denominator and let a
    # still-current cached copy be recognised without downloading it again.
    for it in items:
        it.meta = curl_head(it.url)
        if not it.meta.get("size") and it.size:
            it.meta["size"] = it.size
        it.path = os.path.join(args.cache, cache_name(it, channel, bot["version"]))
        os.makedirs(os.path.dirname(it.path), exist_ok=True)

    todo = [it for it in items if not _cache_valid(it, it.path)]
    for it in items:
        if it not in todo:
            say("%s: up to date in the cache - not downloaded again." % it.name)
    total = sum(it.meta.get("size") or 0 for it in todo)
    if todo:
        say("Downloading %d file(s)%s ..." % (
            len(todo), ", %s MB" % human_mb(total) if total else ""))
    prog = Progress(total, len(todo))
    for n, it in enumerate(todo, 1):
        download(it, it.path, prog, n)

    # ---- unpack -------------------------------------------------------
    os.makedirs(bot_dir, exist_ok=True)
    for it in items:
        if it.key == "full":
            say("Installing phBot %s into %s ..." % (bot["version"], bot_dir))
            extract(it.path, bot_dir, marker="phBot.exe")
        elif it.key == "file":
            shutil.copyfile(it.path, os.path.join(bot_dir, it.name))
        elif it.key == "manager":
            say("Installing the Manager into %s ..." % mgr_dir)
            os.makedirs(mgr_dir, exist_ok=True)
            if it.name.lower().endswith(".zip"):
                extract(it.path, mgr_dir, marker="Manager.exe")
            else:
                shutil.copyfile(it.path, os.path.join(mgr_dir, "Manager.exe"))
        elif it.key == "plugins":
            say("Installing the plugin runtime into %s ..." % os.path.join(bot_dir, "Plugins"))
            # plugins314.zip -> Plugins/python314 (the folder name phBot looks for)
            wrap = "python" + "".join(ch for ch in it.name if ch.isdigit())
            extract(it.path, os.path.join(bot_dir, "Plugins"), strip_dir={"plugins"}, wrap=wrap)
        elif it.key in ("navmesh", "minimap"):
            say("Installing %s data into %s ..." % (it.key, os.path.join(bot_dir, it.key)))
            extract(it.path, os.path.join(bot_dir, it.key), strip_dir={it.key})

    # ---- verify -------------------------------------------------------
    # update.json lists phBot.exe/phBot.dll with checksums. phBot does not
    # start without phBot.dll, so a full package that lacks it (or carries a
    # different build) is corrected file by file.
    for f in bot.get("win") or []:
        target = os.path.join(bot_dir, f["path"].replace("\\", "/"))
        want = (f.get("sha256") or "").lower()
        if os.path.isfile(target) and (not want or sha256_file(target) == want):
            continue
        say("%s missing or not the published build - downloading it directly ..." % f["name"])
        it = Item("file", f["name"], f["url"], f.get("size", 0), want)
        it.meta = {"size": f.get("size")} if f.get("size") else {}
        it.path = os.path.join(args.cache, channel, bot["version"], "files", f["name"])
        os.makedirs(os.path.dirname(it.path), exist_ok=True)
        if not _cache_valid(it, it.path):
            download(it, it.path, Progress(it.size, 1), 1)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(it.path, target)
    for need in ("phBot.exe", "phBot.dll"):
        if not os.path.isfile(os.path.join(bot_dir, need)):
            raise FetchError("%s is missing after the install" % need)
    # Newest mtime = the one find_phbot_exe (sro.sh, phbot-maxiguard.sh)
    # picks when both channels are installed side by side.
    os.utime(os.path.join(bot_dir, "phBot.exe"))

    if args.state_file:
        os.makedirs(os.path.dirname(args.state_file), exist_ok=True)
        with open(args.state_file, "w") as fh:
            json.dump({"channel": channel, "version": bot["version"],
                       "manager": mgr["version"] if mgr else "",
                       "components": components, "dir": bot_dir,
                       "installed_at": int(time.time())}, fh)
    say("phBot %s (%s) installed: %s" % (bot["version"], channel, os.path.join(bot_dir, "phBot.exe")))
    return 0


def info(args) -> int:
    """Machine-readable channel info (version, changelog, sizes) as JSON."""
    out = {}
    for ch in ("testing", "stable"):
        try:
            m = load_manifest(args.cdn, ch)
            out[ch] = {"version": m.get("version", ""), "changelog": m.get("changelog") or [],
                       "urls": {k: m.get(k, "") for k in ("full", "python", "navmesh", "minimap")}}
        except FetchError as e:
            out[ch] = {"error": str(e)}
    print(json.dumps(out))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--channel", choices=("testing", "stable"), default="testing")
    ap.add_argument("--components", default=",".join(COMPONENTS),
                    help="comma-separated: " + ",".join(COMPONENTS) + " (phBot itself is always installed)")
    ap.add_argument("--programs", help="the prefix's ...\\AppData\\Local\\Programs folder")
    ap.add_argument("--cache", help="download cache directory")
    ap.add_argument("--state-file", default="", help="where to record the installed version")
    ap.add_argument("--cdn", default=os.environ.get("SRO_PHBOT_CDN", DEFAULT_CDN))
    ap.add_argument("--info", action="store_true", help="print channel info as JSON and exit")
    args = ap.parse_args()
    try:
        if args.info:
            return info(args)
        if not args.programs or not args.cache:
            ap.error("--programs and --cache are required")
        return install(args)
    except FetchError as e:
        if IS_TTY:
            sys.stdout.write("\n")
        warn("phBot download failed: %s" % e)
        return 1
    except KeyboardInterrupt:
        warn("phBot download cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
