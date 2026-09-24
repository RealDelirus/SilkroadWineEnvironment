#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# build-appimage.sh - packages the PySide6 GUI (gui/main.py) plus the whole
# installer payload (swe.sh, lib/, patches/, prebuilt/) into a single
# self-contained AppImage. The GUI itself is a thin wrapper - swe.sh and
# sro.sh (via lib/sro-launcher.sh) do all the real work, unchanged, so the
# AppImage's job is purely "bundle a Python+Qt runtime + that payload
# together so it runs on any glibc-based x86_64 Linux without the user
# installing anything first".
#
# Usage: ./build-appimage.sh            (builds using THIS machine's glibc -
#                                         the AppImage then only runs on a
#                                         target with an equal-or-newer glibc)
#        ./build-appimage.sh --docker    (recommended for anything you intend
#                                         to actually distribute: builds
#                                         inside an Ubuntu 20.04 container
#                                         (glibc 2.31) so the result also runs
#                                         on older still-in-use bases like
#                                         Debian Bullseye or older ChromeOS
#                                         Flex Crostini containers, not just
#                                         ones as new as the build machine)
# Output: SilkroadWineEnvironment-x86_64.AppImage in the repo root.
#
# Why --docker matters: an AppImage bundles Python/Qt but NOT glibc itself -
# the frozen binaries still link against whatever glibc was on the machine
# that built them, and glibc is forwards- but not backwards-compatible (a
# binary built against glibc 2.44 refuses to run against an older 2.35, e.g.
# stock Ubuntu 22.04 - confirmed the hard way: an AppImage built on a
# bleeding-edge rolling-release host failed on plain Ubuntu with
# "GLIBC_2.44 not found"; likewise, a build against Ubuntu 22.04 (glibc 2.35)
# failed on Debian Bullseye / older ChromeOS Flex Crostini (glibc 2.31) with
# "GLIBC_2.35 not found"). Building inside an older, widely-deployed distro
# is the standard fix; there is no way around it from a newer host. Ubuntu
# 20.04 is used here (not Debian Bullseye directly) because it has the exact
# same glibc 2.31 but, unlike Bullseye, still has actively maintained
# package repositories (ESM until 2030).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/.appimage-build"
# Pinned by digest, not just the tag: the whole point of the container is a
# fixed glibc 2.31 / Python 3.8 base, and a re-pushed "20.04" tag must not be
# able to change what a build links against without anyone noticing.
BASE_IMAGE="ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214"
APPDIR="$BUILD/AppDir"
VENV="$BUILD/venv"
CACHE="$HERE/.cache-appimage-tools"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

# The version the GUI shows (sidebar footer) and compares against the latest
# release for its update hint. Taken from git on the HOST - inside the
# container git refuses the root-owned view of this user-owned checkout
# ("dubious ownership") - and handed in via SRO_VERSION. Tag BEFORE building a
# release, or it reads e.g. v1.1.0-1-gabc1234 (which is still accurate).
SRO_VERSION="${SRO_VERSION:-$(git -C "$HERE" describe --tags --always --dirty 2>/dev/null || echo dev)}"

if [ "${1:-}" = "--docker" ]; then
    command -v docker >/dev/null 2>&1 || die "docker is required for --docker."
    say "Building $SRO_VERSION inside Ubuntu 20.04 (glibc 2.31) for broad compatibility"
    docker run --rm -e SRO_VERSION="$SRO_VERSION" -v "$HERE:/work" -w /work "$BASE_IMAGE" bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            python3 python3-venv python3-pip python3-dev \
            build-essential curl ca-certificates git file \
            libglib2.0-0 libnss3 libnspr4 \
            libgl1 libegl1 libgles2 libopengl0 libglx0 \
            libxkbcommon0 libxkbcommon-x11-0 \
            libfontconfig1 libfreetype6 \
            libdbus-1-3 \
            libx11-6 libx11-xcb1 libxext6 libxrender1 libxi6 libxtst6 \
            libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libxrandr2 libxinerama1 \
            libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
            libxcb-randr0 libxcb-render-util0 libxcb-render0 libxcb-shape0 \
            libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 \
            libxcb-glx0 libxcb-util1 \
            libasound2 libpulse0 \
            libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
            libcups2 libdrm2 libgbm1 \
            libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libcairo-gobject2 \
            libgdk-pixbuf2.0-0 \
            libsqlite3-0 libssl1.1 zlib1g \
            > /tmp/apt.log 2>&1 || { tail -80 /tmp/apt.log; exit 1; }
        bash ./build-appimage.sh
    '
    # The container runs as root, so its output is root-owned on the bind
    # mount - hand it back to whoever is actually running this script.
    docker run --rm -v "$HERE:/work" "$BASE_IMAGE" \
        chown -R "$(id -u):$(id -g)" /work/.appimage-build /work/.cache-appimage-tools \
            /work/SilkroadWineEnvironment-x86_64.AppImage 2>/dev/null || true
    exit 0
fi

command -v python3 >/dev/null 2>&1 || die "python3 is required."
command -v curl    >/dev/null 2>&1 || die "curl is required (to fetch appimagetool)."

rm -rf "$BUILD"
mkdir -p "$APPDIR" "$CACHE"

# ------------------------------------------------------------ 1) Python + Qt
say "Setting up a build venv (pinned PySide6 + PyInstaller, see gui/requirements-appimage.txt)"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/gui/requirements-appimage.txt"

say "Freezing the GUI with PyInstaller (this bundles Python + Qt itself)"
ICON_DATA_ARGS=()
[ -f "$HERE/gui/icon.png" ] && ICON_DATA_ARGS=(--add-data "$HERE/gui/icon.png:.")
"$VENV/bin/pyinstaller" --noconfirm --clean --windowed --name sro-gui \
    --distpath "$BUILD/dist" --workpath "$BUILD/pyi-work" \
    --specpath "$BUILD" \
    "${ICON_DATA_ARGS[@]}" \
    "$HERE/gui/main.py"

# Strip Qt's Wayland platform integration entirely - this app only ever needs
# the xcb platform plugin (works fine under Wayland too, via XWayland; already
# verified across every distro this has been tested on). Confirmed the hard
# way: the closure resolver below vendors libwayland-client.so.0 from THIS
# build container, but the bundled libQt6WaylandClient.so.6 (shipped by the
# PySide6 wheel, built against a newer Wayland ABI) needs wl_proxy_marshal_flags
# from it - a symbol Ubuntu 20.04's own libwayland-client (1.18) doesn't have,
# so it crashed with "undefined symbol: wl_proxy_marshal_flags" on launch on a
# real Ubuntu machine. There is no single libwayland-client version that is
# simultaneously old enough to exist on 20.04 and new enough for the bundled
# Qt - removing Wayland support instead of chasing that sidesteps the whole
# ABI mismatch, and this app never needed it in the first place.
say "Removing Qt Wayland platform integration (xcb covers X11 + XWayland; avoids a libwayland-client ABI mismatch)"
# Qt spells this "Wl" in some filenames (libQt6WlShellIntegration.so.6), not
# just "wayland" - a first pass here missed exactly that file. The GTK3
# platform theme plugin (libqgtk3.so) also unconditionally links
# libwayland-client (GTK3 itself supports both X11 and Wayland backends) even
# though it has nothing to do with Qt's own Wayland integration - remove it
# too so nothing left in the bundle references libwayland-client at all; Qt
# falls back to its default (Fusion) style fine without it.
find "$BUILD/dist/sro-gui" \( -iname '*wayland*' -o -iname 'libQt6Wl*' \) -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/dist/sro-gui" -iname 'libqgtk3.so' -delete 2>/dev/null || true

# Same idea for OpenGL: this is a plain Qt Widgets app with no QOpenGLWidget
# anywhere, so the xcb GL integrations (the only thing that would ever load the
# HOST's GPU driver - Mesa/LLVM, NVIDIA's libGLX - into this process, next to
# the bundle's own older libraries) have nothing to do. AppRun also sets
# QT_XCB_GL_INTEGRATION=none. libGL.so.1 itself stays: libQt6Gui links it
# directly, and it is only glvnd's vendor-neutral dispatcher.
say "Removing Qt's xcb OpenGL integrations (a Widgets-only app never renders through GL)"
find "$BUILD/dist/sro-gui" -type d -name 'xcbglintegrations' -exec rm -rf {} + 2>/dev/null || true

# PyInstaller's dependency scan follows ELF NEEDED entries of the main
# executable and Python extension modules, but Qt loads its PLATFORM PLUGINS
# (xcb, the GL/EGL integrations, image format plugins, ...) via dlopen() at
# runtime, from platforms/, xcbglintegrations/, imageformats/ etc. - nothing
# statically references them, so PyInstaller never sees (or bundles) THEIR
# dependencies either.
#
# Checking for ldd's "not found" lines during the BUILD does NOT catch this:
# this build container has every one of these libraries installed (that's
# how the plugins got built/tested at all), so ldd resolves them just fine
# HERE and never reports them as missing - they only go missing on a bare
# deployment target that never runs ldd during this build at all (confirmed:
# a fresh, library-free Ubuntu 22.04 container failed on libGL.so.1, then
# libxcb.so.1, each fixed individually only proved MORE were still missing).
# The only correct fix is to vendor the FULL transitive closure of every
# resolved dependency of every bundled .so, not just the ones flagged
# "not found" here - i.e. build a portable bundle the way linuxdeploy's own
# dependency plugin would, since PyInstaller's own collector structurally
# cannot see into what Qt's plugins pull in.
say "Vendoring the full runtime library closure (Qt plugins dlopen() their own deps)"
INTERNAL="$BUILD/dist/sro-gui/_internal"
# NEVER vendor these: they're tightly coupled to the host's kernel/dynamic
# loader (glibc, the loader itself, NSS modules that must match the host's
# /etc/nsswitch.conf setup) - shipping a different build of any of them
# alongside a newer or older host copy is a well-known way to break a
# "portable" bundle far worse than the missing-library crash this is fixing.
#
# libstdc++/libgcc_s are left to the host as well, for the opposite reason:
# every host new enough for glibc 2.31 already has a NEWER libstdc++ than this
# container's (the bundle needs at most GLIBCXX_3.4.22; Ubuntu 20.04 / Debian
# Bullseye ship 3.4.28), so bundling one only ever adds risk - an older copy
# loaded first breaks any host library pulled into the process later that
# needs a newer GLIBCXX (e.g. Mesa's LLVM on a current distro wants 3.4.30).
LIBC_BLOCKLIST='^(libc|libm|libpthread|libdl|librt|libutil|libresolv|libnsl|ld-linux-x86-64|libnss_.*|libstdc\+\+|libgcc_s)\.so'
for pass in 1 2 3 4 5 6; do
    added=0
    # LD_LIBRARY_PATH so already-bundled deps resolve to _internal (and get
    # skipped) instead of ldd reporting some OTHER system copy for them.
    resolved="$(find "$BUILD/dist/sro-gui" -name '*.so*' \
        -exec env LD_LIBRARY_PATH="$INTERNAL" ldd {} \; 2>/dev/null \
        | grep '=>' | awk '{print $1, $3}' | sort -u)"
    while IFS=' ' read -r libname libpath; do
        [ -n "$libname" ] && [ -n "$libpath" ] || continue
        [[ "$libname" =~ $LIBC_BLOCKLIST ]] && continue
        [ -f "$INTERNAL/$libname" ] && continue          # already vendored
        case "$libpath" in "$INTERNAL"/*) continue ;; esac  # already resolves into the bundle
        [ -f "$libpath" ] || continue                     # "not found" (no third field) - nothing to copy
        cp -L "$libpath" "$INTERNAL/$libname" 2>/dev/null && added=1
    done <<< "$resolved"
    [ "$added" = 1 ] || break
done
# PyInstaller's own collector may already have dropped copies of the
# blocklisted C++ runtime in - remove those too (see LIBC_BLOCKLIST above).
find "$BUILD/dist/sro-gui" \( -name 'libstdc++.so*' -o -name 'libgcc_s.so*' \) -delete 2>/dev/null || true
say "  vendored $(find "$INTERNAL" -maxdepth 1 -name '*.so*' | wc -l) extra system libraries total"

# Everything bundled above comes with its own license, and the LGPL parts
# (Qt/PySide6, Wine-derived files) require passing those notices on. Collected
# here from the sources themselves - the Python/PySide6 dist-info files and,
# for the vendored system libraries, the build container's own Debian
# copyright files - so the list can't drift from what is actually bundled.
# Shown in the GUI under "Licenses" (sidebar).
say "Writing THIRD-PARTY-NOTICES.txt (licenses of everything bundled)"
NOTICES="$BUILD/dist/sro-gui/THIRD-PARTY-NOTICES.txt"
SRO_WINE_VERSION="$(sed -n 's/^SRO_WINE_VERSION="${SRO_WINE_VERSION:-\([^}]*\)}"$/\1/p' "$HERE/lib/common.sh")"
section() { printf '\n\n%s\n%s\n%s\n\n' "==============================================================================" "$1" "=============================================================================="; }
{
    cat <<'HDR'
SilkroadWineEnvironment - third-party notices

SilkroadWineEnvironment itself is free software under the GNU General Public
License, version 3 or later (see LICENSE). This AppImage also contains the
components below, each under its own license. phBot is NOT included: it is
downloaded from phbot.org during setup, under ProjectHax LLC's own terms.
HDR
    pyver="$("$VENV/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
    section "Python $pyver - Python Software Foundation License"
    pylic="$("$VENV/bin/python" -c 'import os, sys; print(os.path.join(sys.base_prefix, "lib", "python%d.%d" % sys.version_info[:2], "LICENSE.txt"))')"
    if [ -f "$pylic" ]; then cat "$pylic"
    else cat /usr/share/doc/python3.*/copyright 2>/dev/null | head -400 || true; fi
    for pkg in PySide6 PySide6_Essentials PySide6_Addons shiboken6 pyinstaller; do
        d="$(ls -d "$VENV"/lib/python*/site-packages/"$pkg"-*.dist-info 2>/dev/null | head -1)"
        [ -n "$d" ] || continue
        ver="$(sed -n 's/^Version: //p' "$d/METADATA" | head -1)"
        lic="$(sed -n 's/^License: //p' "$d/METADATA" | head -1)"
        section "$pkg $ver${lic:+ - $lic}"
        [ "$pkg" = pyinstaller ] && echo "(Only PyInstaller's bootloader is part of this app - GPL with the bootloader exception below.)"
        found=0
        while IFS= read -r f; do cat "$f"; echo; found=1; done < <(find "$d" -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' \) | sort)
        if [ "$found" = 0 ]; then
            case "$pkg" in
                PySide6*|shiboken6)
                    # The wheels carry no license file, only "License: LGPL".
                    echo "Qt for Python / Qt $ver by The Qt Company, used under the GNU LGPL version 3"
                    echo "(full text in the \"GNU LGPL 3.0\" section below)."
                    echo "Source: https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://download.qt.io/"
                    echo "The Qt / PySide6 / shiboken6 libraries are shipped as separate shared libraries in"
                    echo "opt/sro-gui/_internal/ (see --appimage-extract) and can be replaced there." ;;
                *) echo "License: ${lic:-see https://pypi.org/project/$pkg/}" ;;
            esac
        fi
    done
    section "GNU LGPL 3.0 (applies to Qt, PySide6 and shiboken6)"
    cat "$HERE/gui/COPYING.LGPL-3.0"
    if command -v dpkg >/dev/null 2>&1; then
        # Packages built from one source (krb5, libx11, cairo, ...) carry the
        # identical copyright file - print each text once, refer back after.
        declare -A seen_copyright=()
        for pkg in $(for f in "$INTERNAL"/*.so*; do
                         [ -L "$f" ] && continue
                         dpkg -S "*/$(basename "$f")" 2>/dev/null | head -1 | cut -d: -f1
                     done | sort -u); do
            section "$pkg (system library, $(. /etc/os-release && echo "$PRETTY_NAME") package $(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null))"
            cfile="/usr/share/doc/$pkg/copyright"
            if [ ! -f "$cfile" ]; then echo "See the Debian/Ubuntu package '$pkg'."; continue; fi
            sum="$(sha256sum "$cfile" | cut -d' ' -f1)"
            if [ -n "${seen_copyright[$sum]:-}" ]; then
                echo "Same copyright and license text as ${seen_copyright[$sum]} above."
            else
                seen_copyright[$sum]="$pkg"
                cat "$cfile"
            fi
        done
    else
        section "System libraries"
        echo "Bundled from the build machine (no dpkg here to look up their licenses):"
        find "$INTERNAL" -maxdepth 1 -name '*.so*' ! -type l -printf '  %f\n' | sort
    fi
    section "Microsoft Visual C++ 2015-2022 Redistributable (x86) - opt/sro-linux/prebuilt/VC_redist.x86.exe"
    cat <<'MSVC'
Copyright (c) Microsoft Corporation. All rights reserved.
Redistributed unmodified, as the "Distributable Code" of the Microsoft Visual
C++ Redistributable, under Microsoft's license terms for it:
https://visualstudio.microsoft.com/license-terms/
It is installed into the Wine environment that runs phBot, which needs it.
MSVC
    section "Wine - opt/sro-linux/patches/ and opt/sro-linux/prebuilt/user32.dll"
    cat <<WINE
Derived from Wine (https://www.winehq.org), which is licensed under the GNU
Lesser General Public License, version 2.1 or later - full text in
opt/sro-linux/patches/COPYING.LGPL-2.1. The corresponding source is Wine
${SRO_WINE_VERSION:-(see lib/common.sh)} (https://dl.winehq.org/wine/source/) plus the patches
in opt/sro-linux/patches/, which is also exactly how the setup builds it.
WINE
} > "$NOTICES"
say "  $(grep -c "^$(printf '=%.0s' $(seq 78))\$" "$NOTICES" | awk '{print int($1/2)}') sections, $(wc -c < "$NOTICES") bytes"

# ------------------------------------------------------------ 2) AppDir layout
say "Assembling the AppDir"
mkdir -p "$APPDIR/opt/sro-linux"
# The installer payload swe.sh actually needs at runtime - NOT the gui/,
# build artifacts, or version control metadata.
for entry in swe.sh lib patches prebuilt src README.md LICENSE; do
    [ -e "$HERE/$entry" ] && cp -a "$HERE/$entry" "$APPDIR/opt/sro-linux/"
done
printf '%s\n' "$SRO_VERSION" > "$APPDIR/opt/sro-linux/VERSION"
say "  version: $SRO_VERSION"
cp -a "$BUILD/dist/sro-gui" "$APPDIR/opt/sro-gui"
# sudo's password helper (SUDO_ASKPASS) - sudo can only run a plain
# executable path, so this wrapper turns that into `sro-gui --askpass`.
install -m 755 "$HERE/gui/askpass.sh" "$APPDIR/opt/sro-gui/askpass.sh"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export SRO_GUI_REPO="$HERE/opt/sro-linux"
# Widgets-only app: never let Qt load the host's GPU driver (see
# build-appimage.sh, "xcb OpenGL integrations").
export QT_XCB_GL_INTEGRATION="${QT_XCB_GL_INTEGRATION:-none}"
# Only Qt's xcb platform plugin is bundled (X11, or XWayland under a Wayland
# session), so force it: a user-wide QT_QPA_PLATFORM=wayland (common on
# Sway/Hyprland setups) made Qt abort with "Could not find the Qt platform
# plugin wayland", and even unset, Qt tries wayland first in a Wayland
# session. Explicit other choices (offscreen, minimal, ...) are left alone.
case "${QT_QPA_PLATFORM:-}" in
    ""|*wayland*)
        # Without any X display Qt would only abort with "could not connect
        # to display" - say what is actually missing instead.
        if [ -z "${DISPLAY:-}" ]; then
            echo "SilkroadWineEnvironment needs an X11 display (X11, or XWayland in a Wayland session), but DISPLAY is not set." >&2
            [ -n "${WAYLAND_DISPLAY:-}" ] && echo "A Wayland session was found without XWayland - install/enable XWayland and try again." >&2
            exit 1
        fi
        export QT_QPA_PLATFORM=xcb ;;
esac
exec "$HERE/opt/sro-gui/sro-gui" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/sro-gui.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=SilkroadWineEnvironment
Comment=Run Silkroad Online clients (and phBot) on Linux via Wine
Exec=sro-gui
Icon=sro-gui
Categories=Game;Utility;
Terminal=false
EOF

# Minimal placeholder icon (a flat-colored PNG, generated with no extra
# dependency beyond the stdlib zlib module) - appimagetool requires SOME
# icon matching the desktop file's Icon= to exist. Replace
# AppDir/sro-gui.png with real artwork any time; this build script only
# generates one if none is already checked into the repo.
if [ -f "$HERE/gui/icon.png" ]; then
    cp "$HERE/gui/icon.png" "$APPDIR/sro-gui.png"
else
    python3 - "$APPDIR/sro-gui.png" <<'PY'
import struct, zlib, sys
path = sys.argv[1]
size = 256
# Solid blue-grey square with a lighter inset square - just enough to not be
# a blank/broken icon; swap in real artwork via gui/icon.png whenever ready.
bg = (38, 60, 92, 255)
fg = (90, 150, 220, 255)
rows = []
for y in range(size):
    row = bytearray([0])  # filter byte
    inset = size // 4
    for x in range(size):
        c = fg if (inset <= x < size - inset and inset <= y < size - inset) else bg
        row.extend(c)
    rows.append(bytes(row))
raw = b"".join(rows)

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(raw, 9))
png += chunk(b"IEND", b"")
with open(path, "wb") as f:
    f.write(png)
PY
fi

# ------------------------------------------------------------ 3) appimagetool
# Both tools are pinned to a fixed release + SHA256 instead of "continuous":
# the runtime stub ends up INSIDE every AppImage we ship (it is what mounts it
# on the user's machine), so a silently moved "continuous" build could change
# what the result needs from the host without any change in this repo.
# runtime 20251108 is functionally identical to the continuous build (75849dc)
# the tested AppImages shipped with - the commits between them only touch
# type2-runtime's own CI workflow. To bump: change tag + hash together, then
# re-run tests/smoke-appimage.sh.
APPIMAGETOOL_TAG="1.9.1"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_TAG="20251108"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

fetch_pinned() {   # fetch_pinned <dest> <url> <sha256> - downloads once, always verifies
    local dest="$1" url="$2" sum="$3"
    if [ ! -s "$dest" ]; then
        say "Downloading $(basename "$dest")"
        curl -fL --retry 3 -o "$dest.part" "$url" || { rm -f "$dest.part"; die "Download failed: $url"; }
        mv -f "$dest.part" "$dest"
    fi
    if ! printf '%s  %s\n' "$sum" "$dest" | sha256sum -c --status; then
        rm -f "$dest"
        die "Checksum mismatch for $(basename "$dest") ($url) - refusing to build with it."
    fi
}

APPIMAGETOOL="$CACHE/appimagetool-$APPIMAGETOOL_TAG-x86_64.AppImage"
fetch_pinned "$APPIMAGETOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_TAG/appimagetool-x86_64.AppImage" \
    "$APPIMAGETOOL_SHA256"
chmod +x "$APPIMAGETOOL"
# appimagetool bundles the final AppImage around a "runtime" stub and tries to
# download it itself on every run - that download has been flaky in some
# environments (sandboxes/CI without the exact TLS setup its bundled curl
# expects), while a plain `curl` from this script works fine. Fetch it once
# ourselves and hand it over explicitly to sidestep that.
RUNTIME="$CACHE/runtime-$RUNTIME_TAG-x86_64"
fetch_pinned "$RUNTIME" \
    "https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_TAG/runtime-x86_64" \
    "$RUNTIME_SHA256"

say "Building the AppImage"
OUT="$HERE/SilkroadWineEnvironment-x86_64.AppImage"
rm -f "$OUT"
# appimagetool itself is an AppImage and needs FUSE to run directly; extract
# and run it instead so this also works in FUSE-less environments (containers,
# some CI runners).
TOOLDIR="$CACHE/appimagetool-$APPIMAGETOOL_TAG"
if [ ! -x "$TOOLDIR/squashfs-root/AppRun" ]; then
    rm -rf "$TOOLDIR"; mkdir -p "$TOOLDIR"
    ( cd "$TOOLDIR" && "$APPIMAGETOOL" --appimage-extract >/dev/null 2>&1 || true )
fi
if [ -x "$TOOLDIR/squashfs-root/AppRun" ]; then
    ARCH=x86_64 "$TOOLDIR/squashfs-root/AppRun" --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
else
    ARCH=x86_64 "$APPIMAGETOOL" --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
fi

say "Done: $OUT"
