#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# smoke-appimage.sh - start the built AppImage on every distro it has to run
# on, in throwaway containers, before a user finds out it doesn't.
#
# Every AppImage portability bug so far ("GLIBC_2.44 not found",
# "wl_proxy_marshal_flags", missing libGL/libxcb, "OPENSSL_3.2.0 not found")
# only surfaced on a real machine. This runs `sro-gui --selftest` (builds the
# real main window, does one real swe.sh round trip, exits 0) on each target:
#
#   bare  - a stock image with nothing installed and Qt's offscreen platform:
#           proves the bundle's own library closure is complete.
#   x11   - the same image plus Xvfb and Mesa, through Qt's real xcb platform
#           plugin: proves the bundle coexists with a desktop's host libraries
#           (the direction that breaks on NEW distros).
#
# Usage: tests/smoke-appimage.sh [path/to/AppImage] [image ...]
#        default AppImage: ./SilkroadWineEnvironment-x86_64.AppImage
#        default images:   the oldest targets (glibc 2.31 - Ubuntu 20.04,
#                          Debian Bullseye = older ChromeOS Flex / Crostini),
#                          current Crostini (Bookworm), and current distros.
# Needs docker or podman. Exits non-zero if any run fails.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPIMAGE="${1:-$HERE/SilkroadWineEnvironment-x86_64.AppImage}"
[ $# -gt 0 ] && shift
IMAGES=("$@")
[ ${#IMAGES[@]} -gt 0 ] || IMAGES=(
    ubuntu:20.04 debian:bullseye debian:bookworm ubuntu:22.04
    ubuntu:24.04 fedora:latest archlinux:latest
)

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

ENGINE="$(command -v docker || command -v podman || true)"
[ -n "$ENGINE" ] || die "docker or podman is required."
[ -f "$APPIMAGE" ] || die "AppImage not found: $APPIMAGE (build it with ./build-appimage.sh --docker)"
APPIMAGE="$(readlink -f "$APPIMAGE")"
LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/sro-smoke.XXXXXX")"

# Package install line per distro family for the x11 pass: Xvfb + xauth for
# xvfb-run, Mesa so a real desktop's GL/driver libraries are present too.
x11_deps() {
    case "$1" in
        debian:bullseye)
            # Bullseye is past LTS: its security/updates pools are being
            # emptied (404s) while the main archive still serves 11.11 - plenty
            # for Xvfb in a throwaway test container.
            echo 'export DEBIAN_FRONTEND=noninteractive; rm -f /etc/apt/sources.list.d/*; echo "deb http://deb.debian.org/debian bullseye main" > /etc/apt/sources.list; apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libgl1-mesa-dri >/dev/null' ;;
        ubuntu:*|debian:*)
            echo 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libgl1-mesa-dri >/dev/null' ;;
        fedora:*)
            echo 'dnf install -y -q xorg-x11-server-Xvfb xorg-x11-xauth mesa-dri-drivers >/dev/null' ;;
        archlinux:*)
            echo 'pacman -Sy --noconfirm --needed xorg-server-xvfb xorg-xauth mesa >/dev/null' ;;
        *)  return 1 ;;
    esac
}

# --appimage-extract-and-run: containers have no FUSE; the runtime unpacks to
# /tmp instead of mounting, the payload itself runs exactly the same.
RUN='/app/sro.AppImage --appimage-extract-and-run --selftest'

declare -a RESULTS=()
fail=0
run_case() {   # run_case <image> <pass> <expected Qt platform> <shell snippet>
    local image="$1" pass="$2" platform="$3" script="$4" log rc
    log="$LOGDIR/$(printf '%s' "$image" | tr ':/' '__')-$pass.log"
    say "$image [$pass]"
    timeout 600 "$ENGINE" run --rm -v "$APPIMAGE:/app/sro.AppImage:ro" "$image" \
        bash -c "$script" >"$log" 2>&1
    rc=$?
    if [ "$rc" = 0 ] && grep -q "^selftest OK: platform=$platform " "$log"; then
        RESULTS+=("PASS  $image [$pass]  $(grep '^selftest OK' "$log" | head -1 | cut -c14-)")
    else
        RESULTS+=("FAIL  $image [$pass]  (exit $rc, log: $log)")
        fail=1
        tail -n 25 "$log" | sed 's/^/    /'
    fi
}

for image in "${IMAGES[@]}"; do
    run_case "$image" bare offscreen "QT_QPA_PLATFORM=offscreen $RUN"
    if deps="$(x11_deps "$image")"; then
        # QT_QPA_PLATFORM=wayland on purpose: a desktop that exports it
        # user-wide must still end up on xcb (AppRun forces that).
        run_case "$image" x11 xcb "$deps && xvfb-run -a env QT_QPA_PLATFORM=wayland $RUN"
    else
        RESULTS+=("SKIP  $image [x11]  (no package recipe for this image)")
    fi
done

echo
say "Results"
printf '  %s\n' "${RESULTS[@]}"
[ "$fail" = 0 ] && rm -rf "$LOGDIR"
exit "$fail"
