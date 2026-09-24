#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# lib/common.sh - shared helpers for the SilkroadWineEnvironment installer.
#
# Sourced by swe.sh and the lib/*.sh files. Provides:
#   - log / error output
#   - package-manager detection (apt / pacman / dnf) and dependency install
#   - central paths (wine-sro, prefixes)
#   - Wine layout checks (new WoW64)
#
# No side effects on source other than variable definitions.

# Force a UTF-8 locale so multi-byte characters (the spinner glyphs below,
# accented paths in step output) are counted per glyph, not per byte - some
# environments (fresh/minimal distros, containers, SSH without locale
# forwarding) start bash in the C/POSIX locale, which would otherwise garble
# the spinner and any width-based truncation.
if [ "$(locale charmap 2>/dev/null)" != "UTF-8" ]; then
    for _sro_loc in en_US.UTF-8 en_US.utf8 C.UTF-8 C.utf8 de_DE.UTF-8 de_DE.utf8; do
        if locale -a 2>/dev/null | grep -qiFx "$_sro_loc"; then export LC_ALL="$_sro_loc"; break; fi
    done
    unset _sro_loc
fi

# ------------------------------------------------------------------ output
if [ -t 1 ]; then
    C_G=$'\033[1;32m'; C_Y=$'\033[1;33m'; C_R=$'\033[1;31m'; C_B=$'\033[1;34m'; C_0=$'\033[0m'
else
    C_G=; C_Y=; C_R=; C_B=; C_0=
fi
say()  { printf '%s==>%s %s\n' "$C_G" "$C_0" "$*"; }
step() { printf '\n%s### %s%s\n' "$C_B" "$*" "$C_0"; }
warn() { printf '%s!!%s  %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%sXX%s  %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

# Case-insensitive file lookup within one directory. Linux filesystems are
# case-sensitive, but real Silkroad client releases are inconsistent about
# the exact casing of their exe (confirmed: a real client shipped with a
# lowercase silkroad.exe) - every literal `[ -f "$dir/Silkroad.exe" ]` check
# in this codebase silently treated that as "file doesn't exist", which hid
# the launcher option for that client entirely (GUI and TUI both). Prints
# the REAL on-disk filename (whatever case it actually has) on a match, or
# nothing - callers use the printed name for any further -f check/exec
# instead of the literal name they searched for.
_ci_file() {   # $1=dir $2=name -> real on-disk filename, or nothing (exit 1)
    local d="$1" want f base
    want="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
    for f in "$d"/*; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        [ "$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')" = "$want" ] && { printf '%s\n' "$base"; return 0; }
    done
    return 1
}
ask()  { # ask "question" [default y/n] -> exit code 0=yes. Reads from stdin (terminal).
    local q="$1" d="${2:-y}" a
    if [ "${SRO_ASSUME_YES:-0}" = 1 ]; then [ "$d" = n ] && return 1 || return 0; fi
    read -r -p "$q [$( [ "$d" = y ] && echo 'Y/n' || echo 'y/N' )] " a || a=
    a="${a:-$d}"
    case "$a" in [jJyY]*) return 0;; *) return 1;; esac
}

ask_str() { # ask_str "question" "default" -> answer on stdout (default when empty/-y)
    local q="$1" d="${2:-}" a
    if [ "${SRO_ASSUME_YES:-0}" = 1 ]; then printf '%s' "$d"; return 0; fi
    read -r -p "$q${d:+ [$d]}: " a || a=
    printf '%s' "${a:-$d}"
}

# Minimal JSON string escaping (no jq dependency) - covers what actually shows
# up in paths/labels here (backslash, double quote, newline/tab). Used by
# --status-json (and by sro-launcher.sh's own copy for --list-json) so a GUI
# wrapper can consume machine-readable output instead of re-deriving status
# checks itself.
_json_esc() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

# ------------------------------------------------------ machine-readable steps
# Step boundaries for a non-TTY caller (in practice: the GUI, which drives
# swe.sh through QProcess and needs to know which of several steps is running
# to show overall progress). On a real terminal these print nothing at all, so
# no interactive output changes.
gui_step() { [ -t 1 ] || printf '>>> STEP: %s\n' "$*"; }
gui_done() { [ -t 1 ] || printf '>>> DONE: %s\n' "$*"; }
gui_fail() { [ -t 1 ] || printf '>>> FAIL %s: %s\n' "$1" "$2"; }

# Wrap one phase of the FLAG-DRIVEN run (swe.sh's non-interactive path).
#
# Deliberately not run_step(): that one captures the command's output and
# animates a spinner, which is what the interactive menus want but would
# change what `./swe.sh -y` prints in a terminal today. This only frames the
# call with markers and otherwise runs it exactly as before - under `set -e`
# a failure aborts before gui_done, which is what the GUI reads as a failed
# step anyway (it also has the process exit code).
phase() {   # phase "Label" cmd [args...]
    local label="$1"; shift
    gui_step "$label"
    # The exit code is captured explicitly instead of leaning on `set -e` to
    # abort before gui_done: inside a function called from an `||`/`if`
    # context, errexit is switched off for the whole body, so a bare "$@"
    # would fall through and report a step as DONE that actually failed.
    # Confirmed with `phase "x" false || rc=$?` -> printed DONE, rc=0.
    local rc=0
    "$@" || rc=$?
    if [ "$rc" -eq 0 ]; then
        gui_done "$label"
    else
        gui_fail "$rc" "$label"
    fi
    return "$rc"
}

# ------------------------------------------------------------------ step runner
# Wraps a long-running step (system package install, the 20-60 min wine-sro
# build, ...). On a real terminal: a spinner while it runs, then a green check
# or a red X, with the output captured into a temp log and only dumped in full
# on failure - a spinner fighting a wall of apt/compiler output for the same
# terminal lines is more confusing than helpful when the step just works.
# Piped (the GUI), there is no spinner to protect, so the output is streamed
# through as-is and framed by >>> STEP/DONE/FAIL markers - see the branch.
_SRO_SPIN=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
run_step() {   # run_step "Label" fn [args...]
    local label="$1"; shift
    # Ask for the sudo password (if any) now, BEFORE the spinner takes over the
    # terminal. Terminal only: without one, sudo would fall back to the GUI's
    # SUDO_ASKPASS dialog and pop it up for every step, privileged or not.
    if [ "$(id -u)" != 0 ] && { [ -t 0 ] || [ -t 1 ]; }; then sudo -v 2>/dev/null || true; fi

    if [ ! -t 1 ]; then
        # No terminal to animate on - a piped/logged run, which in practice
        # means the GUI driving this through QProcess.
        #
        # This branch used to capture the step's output into a temp file and
        # only dump it on failure, mirroring what the spinner branch below
        # does for a real terminal. That made every phase line the scripts
        # print from INSIDE a step (step()'s "### ..." and say()'s "==> ...",
        # e.g. all of build_wine_sro's download/configure/make/install
        # phases) invisible to the caller: during the 20-60 min wine-sro
        # build the GUI saw exactly two lines and had nothing to show a
        # progress indicator from. Stream it through instead - a piped run
        # already keeps the full output in whatever it is piped into, so the
        # old dump-on-failure would now only duplicate what already scrolled
        # past.
        #
        # The markers below are the machine-readable step boundaries the GUI
        # tracks overall progress with. They deliberately do NOT reuse the
        # "==> " prefix: say() emits that too, and once the GUI strips ANSI
        # a step announcement and an arbitrary say() line would be
        # indistinguishable. Same spirit as step()'s existing "### " marker.
        gui_step "$label"
        local rc=0
        # NOT a `| tee` pipeline: without `set -o pipefail` a pipeline reports
        # tee's exit status (always 0), which would report every failed step
        # as successful. Running "$@" directly keeps $? the step's own status,
        # and `|| rc=$?` keeps `set -e` from aborting before it is read.
        "$@" 2>&1 || rc=$?
        if [ "$rc" -eq 0 ]; then
            gui_done "$label"
        else
            gui_fail "$rc" "$label"
        fi
        return "$rc"
    fi

    local log; log="$(mktemp)"
    ( "$@" ) >"$log" 2>&1 &
    local pid=$! i=0 cols line maxw
    printf '\033[?25l'
    while kill -0 "$pid" 2>/dev/null; do
        cols=$(tput cols 2>/dev/null || echo 80)
        line="$(tail -n1 "$log" 2>/dev/null | tr -d '\r\n')" || line=""
        line="${line#>>> DL: }"; line="${line#>>> EXTRACT: }"   # phBot download progress
        maxw=$((cols - ${#label} - 10)); [ "$maxw" -gt 0 ] || maxw=0
        [ "${#line}" -gt "$maxw" ] && line="${line:0:maxw}"
        printf '\r\033[2K%s   %s %s%s   %s%s%s' "$C_B" "${_SRO_SPIN[i % 10]}" "$label" "$C_0" "$C_Y" "$line" "$C_0"
        i=$((i+1))
        sleep 0.12
    done
    local rc=0
    wait "$pid" || rc=$?
    printf '\033[?25h'
    if [ "$rc" -eq 0 ]; then
        printf '\r\033[2K%s   ✓ %s%s\n' "$C_G" "$label" "$C_0"
    else
        printf '\r\033[2K%s   ✗ %s%s\n' "$C_R" "$label" "$C_0"
        printf '%s   ----- output -----%s\n' "$C_Y" "$C_0"
        sed 's/^/   /' "$log"
        printf '%s   -------------------%s\n' "$C_Y" "$C_0"
    fi
    rm -f "$log"
    return "$rc"
}

# ------------------------------------------------------------------ paths
SRO_HOME="${SRO_HOME:-$HOME/.local/share/sro-linux}"
WINE_SRO="$SRO_HOME/wine-sro"          # patched private Wine build
PREFIX_ROOT="$SRO_HOME/prefixes"
# wine-sro is installed as a self-contained Wine 11.15 (new WoW64) from source
# into $WINE_SRO (make install --prefix=$WINE_SRO). A Wine built this way is
# self-consistent - the loader finds datadir/DLLs relative to its own --prefix,
# hence fixed default paths (no layout mirroring, no segfault).
WINE_SRO_TREE="$WINE_SRO/lib/wine"
WINE_SRO_LOADER="$WINE_SRO/bin/wine"
WINE_SRO_SERVER="$WINE_SRO/bin/wineserver"
BUILD_CACHE="${SRO_BUILD_CACHE:-$HOME/.cache/sro-linux}"
STATE_DIR="$SRO_HOME/state"

# --------------------------------------------------------- WineD3D toggle
# Some hosts (VMs without GPU passthrough, e.g. plain QEMU/bochs-drm) have no
# working Vulkan driver at all, so DXVK fails with "Failed to create Vulkan
# instance" and the client aborts right after boot. Toggle this on to force
# PROTON_USE_WINED3D=1 (OpenGL, falls back to Mesa llvmpipe software
# rendering) on every MaxiGuard launch. Off by default: on real GPUs DXVK is
# much faster than WineD3D, so this must stay opt-in.
WINED3D_FLAG="$STATE_DIR/force_wined3d"
wined3d_forced() { [ -f "$WINED3D_FLAG" ]; }

# ------------------------------------------------------- phBot channel + components
# phBot is downloaded straight from ProjectHax's CDN (lib/phbot-fetch.py) -
# the same packages phBot's own installer offers:
#   channel     testing | stable            (<cdn>/<channel>/update.json)
#   components  manager, plugins, navmesh, minimap  (phBot itself always)
# Both are persisted (like the WineD3D toggle) so a later reinstall, or the
# GUI, keeps remembering the choice instead of re-asking every time. Testing
# is the default because that's the channel phBot's own fixes land on first;
# all components are on by default, as they were with phBot's installer.
PHBOT_CHANNEL_FILE="$STATE_DIR/phbot_channel"
phbot_channel() {   # prints "testing" or "stable" (default: testing)
    local c; c="$(cat "$PHBOT_CHANNEL_FILE" 2>/dev/null || true)"
    [ "$c" = stable ] && echo stable || echo testing
}
PHBOT_COMPONENTS_ALL="manager,plugins,navmesh,minimap"
PHBOT_COMPONENTS_FILE="$STATE_DIR/phbot_components"
phbot_components() {   # prints the comma list (may be empty = phBot only)
    if [ -f "$PHBOT_COMPONENTS_FILE" ]; then
        tr -d ' \n' < "$PHBOT_COMPONENTS_FILE"; echo
    else
        echo "$PHBOT_COMPONENTS_ALL"
    fi
}
# Normalises a user-given component list ("all", "none", "navmesh,minimap",
# "Minimap Manager", ...). Prints it in canonical order; returns 1 on an
# unknown name.
phbot_components_parse() {   # $1 = list
    local in; in="$(printf '%s' "$1" | tr 'A-Z ;' 'a-z,,')"
    case "$in" in all) echo "$PHBOT_COMPONENTS_ALL"; return 0 ;; none|"") echo ""; return 0 ;; esac
    local c out=""
    for c in ${in//,/ }; do
        case ",$PHBOT_COMPONENTS_ALL," in *",$c,"*) ;; *) return 1 ;; esac
    done
    for c in ${PHBOT_COMPONENTS_ALL//,/ }; do
        case ",$in," in *",$c,"*) out="${out:+$out,}$c" ;; esac
    done
    echo "$out"
}
# What was actually installed last (written by lib/phbot-fetch.py).
PHBOT_STATE_FILE="$STATE_DIR/phbot_installed.json"
phbot_installed_version() {   # prints e.g. "33.7.9" (or nothing)
    sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$PHBOT_STATE_FILE" 2>/dev/null | head -1
}

# Which Wine version wine-sro is built from. The fixes (dxdiagn offsets,
# user32 patch, mountmgr patch) are created/tested against 11.15.
SRO_WINE_VERSION="${SRO_WINE_VERSION:-11.15}"

# ProjectHax's CDN - where phBot's own installer downloads phBot from
# (<cdn>/testing/update.json, <cdn>/stable/update.json, <cdn>/manager2/...).
# Overridable via the SRO_PHBOT_CDN environment variable (e.g. a mirror).
export SRO_PHBOT_CDN="${SRO_PHBOT_CDN:-https://cdn.projecthax.com}"

# Microsoft's current VC++ 2015-2026 x86 redistributable - the same link
# phBot's own installer uses. See _refresh_vcredist in setup-prefix.sh.
SRO_VCREDIST_URL="${SRO_VCREDIST_URL:-https://aka.ms/vc14/vc_redist.x86.exe}"

# phBot is third-party software (ProjectHax LLC). Its download page requires
# agreeing to its Terms and Conditions before downloading, so every run that
# would actually run phBot's installer asks for the same agreement first -
# showing the CURRENT text, fetched from where phbot.org's own download page
# loads it. Never accepted on the user's behalf: not by -y, not without a
# terminal. The GUI shows the terms in its own dialog and then passes
# --accept-phbot-terms, which sets SRO_PHBOT_TERMS_ACCEPTED=1.
SRO_PHBOT_TERMS_URL="${SRO_PHBOT_TERMS_URL:-https://phbot.org/static/legal.txt}"
SRO_PHBOT_TERMS_PAGE="https://phbot.org/en/download/"

phbot_terms_ok() {   # 0 = accepted for this run (asks on a terminal if not yet)
    [ "${SRO_PHBOT_TERMS_ACCEPTED:-0}" = 1 ] && return 0
    [ "${SRO_ASSUME_YES:-0}" = 1 ] && return 1
    { [ -t 0 ] && [ -t 1 ]; } || return 1
    local terms
    terms="$(curl -fsSL --max-time 15 "$SRO_PHBOT_TERMS_URL" 2>/dev/null)" || terms=""
    [ -n "$terms" ] || terms="(The terms could not be loaded right now - read them at
$SRO_PHBOT_TERMS_PAGE before accepting.)"
    step "phBot - Terms and Conditions"
    echo "phBot is third-party software by ProjectHax LLC, downloaded from phbot.org."
    echo "To download and use it you must agree to its Terms and Conditions"
    echo "($SRO_PHBOT_TERMS_PAGE)."
    echo
    if command -v less >/dev/null 2>&1; then
        say "Showing the terms - scroll with the arrow keys, press q when done."
        printf '%s\n' "$terms" | less -P "phBot Terms and Conditions - press q when done"
    else
        printf '%s\n\n' "$terms"
    fi
    local a
    read -r -p "Do you accept phBot's Terms and Conditions? [y/N] " a || a=
    case "$a" in [jJyY]*) export SRO_PHBOT_TERMS_ACCEPTED=1; return 0 ;; esac
    return 1
}

# ------------------------------------------------------------------ distro / packages
detect_pm() {   # sets PM = apt|pacman|dnf|zypper|unknown
    if   command -v apt-get >/dev/null 2>&1; then PM=apt
    elif command -v pacman  >/dev/null 2>&1; then PM=pacman
    elif command -v dnf     >/dev/null 2>&1; then PM=dnf
    elif command -v zypper  >/dev/null 2>&1; then PM=zypper
    else PM=unknown
    fi
    echo "$PM"
}

# ------------------------------------------------------------- Wine Mono
# On a prefix it has never seen before, wineboot pops up Wine's own "Wine Mono
# is not installed" dialog and waits for a click - which turns an otherwise
# unattended install (and every later phBot prefix) into something that blocks
# on a modal window. phBot is a .NET application, so declining is not an
# option either.
#
# Wine installs the addon silently if it can find the .msi locally first. It
# searches, in order: $WINEBUILDDIR/.., $WINEDATADIR/<subdir>, $INSTALL_DATADIR
# /wine/<subdir>, /usr/share/wine/<subdir>, ~/.cache/wine (see
# dlls/appwiz.cpl/addons.c). $WINEDATADIR is our own tree, so dropping the file
# in $WINE_SRO/share/wine/mono keeps it self-contained and takes effect for
# every prefix this project creates. Verified against a real wineboot: the msi
# was picked up from there and Mono installed without any dialog.
_wine_mono_version() {
    local a="$BUILD_CACHE/wine-src/wine-$SRO_WINE_VERSION/dlls/appwiz.cpl/addons.c"
    if [ -f "$a" ]; then
        sed -n 's/^#define[[:space:]]*MONO_VERSION[[:space:]]*"\([^"]*\)".*/\1/p' "$a" | head -1
        return 0
    fi
    # Source tree pruned (or --skip-wine against an older install): fall back
    # to what the build recorded.
    [ -f "$STATE_DIR/wine-mono-version" ] && cat "$STATE_DIR/wine-mono-version"
}

ensure_wine_mono() {
    local dest="$WINE_SRO/share/wine/mono"
    # Any msi already in place means wineboot will not ask.
    ls "$dest"/wine-mono-*.msi >/dev/null 2>&1 && return 0

    local ver; ver="$(_wine_mono_version)"
    [ -n "$ver" ] || { warn "Could not determine the Wine Mono version - a prefix may ask to install Mono."; return 1; }
    mkdir -p "$STATE_DIR"; printf '%s\n' "$ver" > "$STATE_DIR/wine-mono-version"

    local f="wine-mono-$ver-x86.msi"
    local cache="$BUILD_CACHE/addons/$f"
    mkdir -p "$BUILD_CACHE/addons" "$dest"

    # Reuse a copy Wine itself already downloaded on this machine rather than
    # pulling 80 MB again.
    if [ ! -s "$cache" ] && [ -s "${XDG_CACHE_HOME:-$HOME/.cache}/wine/$f" ]; then
        cp -f "${XDG_CACHE_HOME:-$HOME/.cache}/wine/$f" "$cache" 2>/dev/null || true
    fi
    if [ ! -s "$cache" ]; then
        say "Downloading Wine Mono $ver (~80 MB, once) so prefixes install it without asking ..."
        curl -fL --retry 3 --connect-timeout 20 -o "$cache.part" \
             "https://dl.winehq.org/wine/wine-mono/$ver/$f" \
            && mv -f "$cache.part" "$cache" \
            || { rm -f "$cache.part"
                 warn "Wine Mono download failed - a prefix will ask to install Mono itself."
                 return 1; }
    fi
    cp -f "$cache" "$dest/$f" || return 1
    say "Wine Mono $ver staged - prefixes install it silently."
    return 0
}

# Ask for the password ONCE per run, then keep the credential warm.
#
# Without this the user gets asked again and again: the privileged steps are
# spread across a run that can take an hour (packages at the start, then
# umu-launcher and the 32-bit runtime much later), and sudo's timestamp
# expires after ~5 minutes in between. Reported from a Fedora machine.
#
# With no terminal (the GUI) this asks through the GUI's password dialog
# instead (see _sudo_askpass_ok), but cannot keep that credential warm: sudo
# keys a tty-less credential on the PARENT pid, so a refresh from the
# background loop below would be a different record. For that case the fix is
# having the privileged steps run next to each other, see
# install_root_prereqs() and swe.sh's flag-driven section.
_SRO_SUDO_KEEPALIVE=""
sudo_prime() {
    [ "$(id -u)" = 0 ] && return 0
    command -v sudo >/dev/null 2>&1 || return 0
    sudo -n true 2>/dev/null && return 0          # passwordless already - nothing to ask
    if ! [ -t 0 ] && ! [ -t 1 ]; then             # no terminal: ask via the GUI now, at the start
        _sudo_askpass_ok || true
        return 0
    fi
    say "Asking for your password once now - the privileged steps later in this run will not ask again."
    sudo -v || return 1
    # Refresh the timestamp until this script exits. `kill -0 $$` stops the
    # loop if the parent is gone, so a crash cannot leave it behind.
    ( while :; do
          sleep 50
          kill -0 "$$" 2>/dev/null || exit 0
          sudo -n -v 2>/dev/null || exit 0
      done ) &
    _SRO_SUDO_KEEPALIVE=$!
    return 0
}
sudo_release() {
    [ -n "$_SRO_SUDO_KEEPALIVE" ] && kill "$_SRO_SUDO_KEEPALIVE" 2>/dev/null
    _SRO_SUDO_KEEPALIVE=""
    return 0
}

# Privileged prerequisites that are NOT build dependencies. Exists so swe.sh
# can run them right after install_deps instead of leaving them buried inside
# setup_maxiguard, which happens after the 20-60 min wine build - far too late
# for any cached credential to still be valid.
install_root_prereqs() {
    ensure_umu           || warn "umu-launcher could not be installed now - MaxiGuard setup will try again later."
    ensure_32bit_runtime || true
    return 0
}

# Can sudo get a password through the GUI's own dialog (SUDO_ASKPASS, set by
# gui/main.py)? Validated once with `sudo -A -v` - which also caches the
# credential for the _sudo calls that follow - and remembered, so a cancelled
# dialog or a user who isn't in sudoers falls through to pkexec instead of
# being asked again for every single step.
_SRO_ASKPASS_OK=""   # "" = not tried yet, 1 = works, 0 = cancelled/failed/unavailable
_sudo_askpass_ok() {
    if [ -z "$_SRO_ASKPASS_OK" ]; then
        _SRO_ASKPASS_OK=0
        if [ -n "${SUDO_ASKPASS:-}" ] && [ -x "$SUDO_ASKPASS" ] && command -v sudo >/dev/null 2>&1 \
           && sudo -A -v 2>/dev/null; then
            _SRO_ASKPASS_OK=1
        fi
    fi
    [ "$_SRO_ASKPASS_OK" = 1 ]
}

_sudo() {   # run $@ as root
    if [ "$(id -u)" = 0 ]; then "$@"
    elif [ -t 0 ] || [ -t 1 ]; then sudo "$@"
    # No controlling terminal to type a sudo password into - this script is
    # being driven by the GUI (e.g. the AppImage). In order of preference:
    #  1. sudo that needs no password right now - ChromeOS Flex / Crostini
    #     ship passwordless sudo, or a credential is still cached
    #  2. sudo -A with the GUI's own password dialog. Needs no PolicyKit
    #     agent, which Crostini and plain window-manager setups don't run -
    #     pkexec fails there with "No authentication agent found"
    #  3. pkexec's native PolicyKit prompt
    #  4. plain sudo, which then fails with a clear "password required"
    #     message instead of hanging
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then sudo "$@"
    elif _sudo_askpass_ok; then sudo -A "$@"
    elif command -v pkexec >/dev/null 2>&1; then pkexec env PATH="$PATH" "$@"
    else sudo "$@"; fi
}

# Build dependencies for Wine 11.15 from source (NEW WoW64).
# Benefit of new WoW64: the 32-bit part is built via mingw -> NO i386 system
# libraries needed, only 64-bit *-dev + mingw (i686 & x86_64).
# The -dev libraries cover what the clients need: X11, OpenGL (DX9 via wined3d),
# FreeType/Fontconfig, GnuTLS (login/HTTPS), sound.
install_deps() {
    local pm; pm="$(detect_pm)"
    step "Installing build dependencies (package manager: $pm)"
    case "$pm" in
      apt)
        # update+install in ONE elevated shell: two _sudo calls mean two
        # password (or two PolicyKit) prompts for what is one operation.
        _sudo sh -c 'apt-get update && apt-get install -y --no-install-recommends "$@"' _ \
            build-essential flex bison \
            gcc-mingw-w64-i686 gcc-mingw-w64-x86-64 \
            g++-mingw-w64-i686 g++-mingw-w64-x86-64 \
            python3 curl git xz-utils ca-certificates pkg-config gettext \
            libfreetype-dev libfontconfig-dev libgnutls28-dev \
            libx11-dev libxext-dev libxcursor-dev libxi-dev libxrandr-dev \
            libxrender-dev libxfixes-dev libxcomposite-dev libxinerama-dev \
            libgl-dev libglu1-mesa-dev libvulkan-dev \
            libpulse-dev libasound2-dev libudev-dev libsdl2-dev \
          || die "apt install failed."

        ;;
      pacman)
        # Arch/CachyOS: headers ship with the library packages.
        _sudo pacman -Sy --needed --noconfirm \
            base-devel flex bison \
            mingw-w64-gcc \
            python curl git xz pkgconf gettext \
            freetype2 fontconfig gnutls \
            libx11 libxext libxcursor libxi libxrandr libxrender libxfixes \
            libxcomposite libxinerama \
            mesa glu vulkan-icd-loader \
            libpulse alsa-lib sdl2 \
          || die "pacman install failed."
        ;;
      dnf)
        _sudo dnf install -y \
            gcc gcc-c++ make flex bison \
            mingw32-gcc mingw64-gcc mingw32-gcc-c++ mingw64-gcc-c++ \
            python3 curl git xz pkgconf gettext \
            freetype-devel fontconfig-devel gnutls-devel \
            libX11-devel libXext-devel libXcursor-devel libXi-devel \
            libXrandr-devel libXrender-devel libXfixes-devel \
            libXcomposite-devel libXinerama-devel \
            mesa-libGL-devel mesa-libGLU-devel vulkan-loader-devel \
            pulseaudio-libs-devel alsa-lib-devel SDL2-devel \
          || die "dnf install failed."
        ;;
      zypper)
        # openSUSE. Package names are best-effort; if one is wrong, install the
        # missing dev packages manually and re-run with --skip-deps.
        _sudo zypper --non-interactive install \
            gcc gcc-c++ make flex bison \
            mingw32-cross-gcc mingw64-cross-gcc mingw32-cross-gcc-c++ mingw64-cross-gcc-c++ \
            python3 curl git xz pkgconf gettext-tools \
            freetype2-devel fontconfig-devel libgnutls-devel \
            libX11-devel libXext-devel libXcursor-devel libXi-devel \
            libXrandr-devel libXrender-devel libXfixes-devel \
            libXcomposite-devel libXinerama-devel \
            Mesa-libGL-devel glu-devel vulkan-devel \
            libpulse-devel alsa-devel libudev-devel libSDL2-devel \
          || die "zypper install failed - install the Wine build deps manually, then --skip-deps."
        ;;
      *)
        die "Unknown package manager. Please install manually (Wine build deps):
   gcc/g++ make flex bison, mingw-w64 (i686+x86_64), python3 curl git xz,
   plus -dev packages for: freetype fontconfig gnutls, X11 (x11/xext/xcursor/
   xi/xrandr/xrender/xfixes/xcomposite/xinerama), OpenGL (GL/GLU), vulkan,
   pulseaudio alsa sdl2. Then re-run with --skip-deps."
        ;;
    esac
    say "Build dependencies ok."
}

# ------------------------------------------------------ 32-bit host runtime
# wine-sro itself needs NO i386 system libs (new WoW64, 32-bit side is built
# via mingw - see install_deps above). But MaxiGuard clients run via umu-run,
# which launches GE-Proton inside the Steam Linux Runtime (pressure-vessel).
# That container needs a REAL 32-bit glibc/libstdc++/libgcc on the HOST to run
# the 32-bit game exe and its own 32-bit helper tools (e.g.
# i386-linux-gnu-capsule-capture-libs) - without it, pressure-vessel silently
# fails to set up 32-bit library capture and the client aborts right after
# boot with no clear error. Debian/Ubuntu need this added explicitly (i386 is
# a "foreign architecture" there); Fedora ships i686 multilib packages by
# default; Arch/CachyOS need the (often disabled by default) [multilib] repo;
# openSUSE ships "-32bit" multilib packages by default on most setups.
ensure_32bit_runtime() {
    case "$(detect_pm)" in
      apt)
        # Enabling i386 and installing the libs go into ONE elevated shell.
        # As two _sudo calls this asked for the password twice for what the
        # user experiences as a single step.
        local need_arch=0
        [ "$(dpkg --print-foreign-architectures 2>/dev/null | grep -c '^i386$')" = 0 ] && need_arch=1
        if [ "$need_arch" = 0 ] \
           && dpkg -s libc6:i386 >/dev/null 2>&1 && dpkg -s libstdc++6:i386 >/dev/null 2>&1 \
           && dpkg -s libgcc-s1:i386 >/dev/null 2>&1; then
            say "32-bit runtime libs ok."
            return 0
        fi
        step "Installing 32-bit runtime libs (GE-Proton/Steam Runtime need these to run 32-bit clients)"
        _sudo sh -c '
            set -e
            if [ "$1" = 1 ]; then dpkg --add-architecture i386; apt-get update; fi
            apt-get install -y --no-install-recommends libc6:i386 libstdc++6:i386 libgcc-s1:i386
        ' _ "$need_arch" \
          || { warn "32-bit runtime install failed - MaxiGuard clients (32-bit) may abort right after boot."; return 1; }
        ;;
      pacman)
        pacman -Qi lib32-glibc >/dev/null 2>&1 && pacman -Qi lib32-gcc-libs >/dev/null 2>&1 && return 0
        step "Installing 32-bit runtime libs (GE-Proton/Steam Runtime need these to run 32-bit clients)"
        _sudo pacman -Sy --needed --noconfirm lib32-glibc lib32-gcc-libs 2>/dev/null && return 0
        warn "lib32-glibc/lib32-gcc-libs not installable - the [multilib] repo is probably disabled.
   Enable it: uncomment [multilib] in /etc/pacman.conf, then run:
     sudo pacman -Sy lib32-glibc lib32-gcc-libs
   Without it, MaxiGuard clients (32-bit) may abort right after boot."
        return 1
        ;;
      dnf)
        rpm -q glibc.i686 >/dev/null 2>&1 && rpm -q libstdc++.i686 >/dev/null 2>&1 && return 0
        step "Installing 32-bit runtime libs (GE-Proton/Steam Runtime need these to run 32-bit clients)"
        _sudo dnf install -y glibc.i686 libstdc++.i686 libgcc.i686 \
          || { warn "32-bit runtime install failed - MaxiGuard clients (32-bit) may abort right after boot."; return 1; }
        ;;
      zypper)
        rpm -q glibc-32bit >/dev/null 2>&1 && return 0
        step "Installing 32-bit runtime libs (GE-Proton/Steam Runtime need these to run 32-bit clients)"
        _sudo zypper --non-interactive install glibc-32bit libstdc++6-32bit libgcc_s1-32bit 2>/dev/null \
          || warn "32-bit runtime package names may differ on your openSUSE version - install the 32-bit glibc/libstdc++/libgcc packages manually if MaxiGuard clients abort right after boot."
        ;;
      *)
        warn "Unknown package manager - could not check/install 32-bit runtime libs.
   If a MaxiGuard client aborts right after boot with no window, install your
   distro's 32-bit glibc/libstdc++/libgcc packages manually (needed by
   GE-Proton's Steam Linux Runtime to run 32-bit clients)."
        return 1
        ;;
    esac
    say "32-bit runtime libs ok."
}

# --------------------------------------------------------------- umu-launcher
# MaxiGuard clients run via umu-run (GE-Proton + Steam Linux Runtime). Prefer a
# native package; fall back to the distro-agnostic zipapp (needs only python3),
# installed under $SRO_HOME/umu so it works on any distro without PATH changes.
UMU_DIR="$SRO_HOME/umu"
umu_run_path() {   # prints a usable umu-run path, or nothing (rc 1)
    [ -x "$UMU_DIR/umu-run" ] && { echo "$UMU_DIR/umu-run"; return 0; }
    command -v umu-run 2>/dev/null && return 0
    return 1
}
_umu_install_zipapp() {
    command -v python3 >/dev/null 2>&1 || { warn "python3 is required for the umu-launcher zipapp."; return 1; }
    local api="https://api.github.com/repos/Open-Wine-Components/umu-launcher/releases/latest"
    local url tmp
    url="$(curl -fsSL --connect-timeout 20 "$api" 2>/dev/null \
        | grep -oE 'https://[^"]*umu-launcher-[0-9.]+-zipapp\.tar' | head -1)"
    [ -n "$url" ] || { warn "Could not determine the umu-launcher zipapp URL (network?)."; return 1; }
    tmp="$(mktemp -d)"
    say "Downloading umu-launcher (zipapp): $(basename "$url")"
    curl -fL --retry 3 --connect-timeout 20 -o "$tmp/umu.tar" "$url" \
        || { warn "umu-launcher download failed."; rm -rf "$tmp"; return 1; }
    tar -xf "$tmp/umu.tar" -C "$tmp" || { warn "umu-launcher extract failed."; rm -rf "$tmp"; return 1; }
    local src; src="$(find "$tmp" -type f -name 'umu-run' | head -1)"
    [ -n "$src" ] || { warn "umu-run not found inside the zipapp."; rm -rf "$tmp"; return 1; }
    mkdir -p "$UMU_DIR"; cp -f "$src" "$UMU_DIR/umu-run"; chmod +x "$UMU_DIR/umu-run"
    rm -rf "$tmp"
    [ -x "$UMU_DIR/umu-run" ]
}
ensure_umu() {
    umu_run_path >/dev/null && { say "umu-run present: $(umu_run_path)"; return 0; }
    step "Installing umu-launcher (required for MaxiGuard)"
    case "$(detect_pm)" in
        pacman) _sudo pacman -Sy --needed --noconfirm umu-launcher 2>/dev/null || true ;;
        dnf)    _sudo dnf install -y umu-launcher 2>/dev/null || true ;;
        zypper) _sudo zypper --non-interactive install umu-launcher 2>/dev/null || true ;;
        # apt has no umu-launcher in the default repos -> go straight to the zipapp.
    esac
    umu_run_path >/dev/null && { say "umu-run installed: $(umu_run_path)"; return 0; }
    _umu_install_zipapp && { say "umu-run installed (zipapp): $UMU_DIR/umu-run"; return 0; }
    warn "Could not install umu-launcher automatically. MaxiGuard needs it.
   Install the 'umu-launcher' package for your distro, then re-run --maxiguard."
    return 1
}

# ------------------------------------------------------------------ Wine checks
wine_bin() { command -v wine 2>/dev/null || echo /usr/bin/wine; }

# Depending on the distro wineserver is NOT in PATH but in the Wine libdir
# (e.g. /usr/lib/x86_64-linux-gnu/wine/wineserver). Search robustly.
wine_server_bin() {
    local c
    c="$(command -v wineserver 2>/dev/null || true)"
    [ -n "$c" ] && { echo "$c"; return 0; }
    local d
    for d in /usr/lib/x86_64-linux-gnu/wine /usr/lib/wine /usr/lib64/wine \
             /usr/libexec/wine /usr/lib/wine/x86_64-unix; do
        [ -x "$d/wineserver" ] && { echo "$d/wineserver"; return 0; }
    done
    return 1
}

wine_version() {   # prints e.g. 11.15 (without 'wine-' / suffix)
    "$(wine_bin)" --version 2>/dev/null | sed 's/^wine-//; s/ .*//'
}

# Checks whether the installed Wine is a NEW WoW64 (required for vSroPlus:
# classic 32-bit Wine is detected by VMProtect via direct syscalls).
# New WoW64 has NO /usr/lib/wine/i386-unix.
wine_is_new_wow64() {
    local d
    for d in /usr/lib/wine /usr/lib64/wine /usr/lib/x86_64-linux-gnu/wine; do
        [ -d "$d/i386-unix" ] && return 1
    done
    return 0
}

# Finds the directory with the PE DLLs (i386-windows).
wine_lib_dir() {
    local d
    for d in /usr/lib/wine /usr/lib64/wine /usr/lib/x86_64-linux-gnu/wine; do
        [ -d "$d/i386-windows" ] && { echo "$d"; return 0; }
    done
    return 1
}

# Find the directory with the Wine data files (nls/).
wine_data_dir() {
    local wl d
    wl="$(wine_lib_dir 2>/dev/null || true)"
    for d in /usr/share/wine /usr/local/share/wine "${wl%/wine}/../../share/wine"; do
        [ -d "$d/nls" ] && { (cd "$d" && pwd); return 0; }
    done
    return 1
}

# Longest common directory prefix of two absolute paths.
_common_prefix() {
    local a b out="" i n; local -a A B
    IFS=/ read -ra A <<<"$1"; IFS=/ read -ra B <<<"$2"
    n=${#A[@]}; [ ${#B[@]} -lt "$n" ] && n=${#B[@]}
    for ((i=0; i<n; i++)); do
        [ "${A[i]}" = "${B[i]}" ] || break
        [ -n "${A[i]}" ] && out="$out/${A[i]}"
    done
    echo "${out:-/}"
}

# Computes how the private build must be structured so that the loader's
# internal relative path resolution (dlldir -> ../../share/wine) works out:
# we mirror the system's libdir AND datadir suffix relative to their common
# prefix under $WINE_SRO. Sets global variables.
#   Ubuntu:  lib/x86_64-linux-gnu/wine  +  share/wine
#   Arch:    lib/wine                   +  share/wine
compute_sro_layout() {
    local wl dd p
    wl="$(wine_lib_dir)" || return 1
    dd="$(wine_data_dir)" || dd="/usr/share/wine"
    p="$(_common_prefix "$wl" "$dd")"
    SRO_SYS_PREFIX="$p"
    SRO_LIB_SUFFIX="${wl#"$p"/}"
    SRO_DATA_SUFFIX="${dd#"$p"/}"
    SRO_SYS_DATADIR="$dd"
    WINE_SRO_TREE="$WINE_SRO/$SRO_LIB_SUFFIX"
    WINE_SRO_LOADER="$WINE_SRO_TREE/wine"
    WINE_SRO_SERVER="$WINE_SRO_TREE/wineserver"
    return 0
}

require_new_wow64() {
    wine_is_new_wow64 || die "The installed Wine is an OLD WoW64 build
   (/usr/lib/.../wine/i386-unix exists). vSroPlus clients do NOT run with it -
   VMProtect terminates the client via direct syscalls.
   Please install a new-WoW64 Wine (current WineHQ or distro Wine)."
}
