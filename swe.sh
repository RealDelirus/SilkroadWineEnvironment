#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# swe.sh - ONE installer that sets up Silkroad clients + phBot under
# Linux/Wine. It is about the client's protection ARCHITECTURE, not a specific
# server (Athens, Hero Journey, etc. are only examples):
#
#   * vSroPlus  : mountmgr disk geometry               -> wine-sro (self-built)
#   * MaxiGuard : export rename + dxdiagn + ACPI shim   -> GE-Proton via umu-run   
#   * plain     : generic client, no special protection -> wine-sro
#   * phBot     : user32 Win10/11 exports               -> wine-sro
#
# Flow:  system packages -> build wine-sro -> set up a prefix per client type.
# At the end an interactive launcher ($SRO_HOME/sro.sh) is created.
#
# Examples:
#   ./swe.sh --games "/home/you/Downloads/GameFolder" --phbot
#   ./swe.sh --maxiguard "/path/MaxiGuard Client"     # e.g. Athens 80 Cap
#   ./swe.sh --vsroplus  "/path/vSroPlus Client"      # e.g. Hero Journey EN
#   ./swe.sh --plain     "/path/Silkroad Client"      # no special protection
#   ./swe.sh --wine-only          # environment only (packages + wine-sro)
#   ./swe.sh --skip-deps --games ~/Games   # skip system packages
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/common.sh"
. "$HERE/lib/build-wine-sro.sh"
. "$HERE/lib/setup-prefix.sh"

# Always restore the cursor (the interactive menu hides it while navigating).
# To stderr, not stdout - a couple of exit paths (--status-json) print
# machine-readable output on stdout, and this must never land in that stream.
trap 'printf "\033[?25h" 1>&2 2>/dev/null || true; sudo_release 2>/dev/null || true' EXIT

# ------------------------------------------------------------------ arguments
SKIP_DEPS=0; SKIP_WINE=0; WINE_ONLY=0; DEPS_ONLY=0; DO_PHBOT=0; MG_ENV=0; VSRO_ENV=0
PHBOT_INSTALLER=""; VCREDIST=""; GAMES_DIR=""; MG_DIR=""; VSRO_DIR=""; PLAIN_DIR=""
PHBOT_CHANNEL=""   # "testing"|"stable"; empty = use the persisted/default choice (phbot_channel())
PHBOT_COMPONENTS=""; PHBOT_COMPONENTS_SET=0   # set by --phbot-components; else phbot_components()
PHBOT_REINSTALL=0  # --phbot-reinstall: download phBot again even if installed (update)
# Was a concrete action chosen via a flag? If not, the interactive wizard runs
# (swe.sh without arguments = guided installation).
ACTION_GIVEN=0

usage() {
    cat <<EOF
swe.sh - ONE installer for Silkroad clients + phBot under Linux/Wine.
By protection ARCHITECTURE (not by server):
  * vSroPlus  : mountmgr disk geometry                 -> wine-sro (self-built)
  * MaxiGuard : export rename + dxdiagn + ACPI shim     -> GE-Proton via umu-run   
  * plain     : generic client (no special protection)  -> wine-sro
  * phBot     : user32 Win10/11 exports                 -> wine-sro

Flow: system packages -> build wine-sro -> set up a prefix per client type.
At the end: interactive launcher \$SRO_HOME/sro.sh.

Running without options: on a fresh machine (no launcher yet) this opens a
short guided wizard - it asks for your client folder and whether to set up
phBot, shows a summary, then does everything in one go. Once a launcher
exists it instead starts that launcher directly; anything else (rebuilds,
toggles, adding another client, a MaxiGuard-only environment, ...) lives
under "Expert settings" in that same menu.
For a fully automatic run without prompts (deps + wine + any client found
under the default GameFolder locations + phBot):
  swe.sh -y

Options:
  --games DIR         folder under which client folders are auto-detected
  --maxiguard [DIR]   set up MaxiGuard; DIR optional - without it just prepares the
                      system-wide MaxiGuard environment (alias: --athens)
  --vsroplus [DIR]    set up vSroPlus support; DIR optional - without it just enables
                      the vSroPlus mode/prefix so it shows up in the launcher (alias: --herojourney)
  --plain DIR         generic Silkroad client folder (no special protection)
  --phbot [EXE]       set up phBot - downloaded directly from phBot's CDN
                      (EXE optional: a local phBot installer to run instead)
  --phbot-channel CH  phBot update channel: 'testing' (default) or 'stable'
  --phbot-components LIST
                      what to install besides phBot, comma-separated:
                      manager,plugins,navmesh,minimap - or 'all' (default) / 'none'
  --phbot-reinstall   download phBot again even if it is installed (update;
                      configs are kept)
  --accept-phbot-terms  you have read and accept phBot's Terms and Conditions
                      (https://phbot.org/en/download/) - required to install
                      phBot without a terminal to ask on, and with -y
                      (mutually exclusive; persisted for later reinstalls)
  --vcredist EXE      custom VC_redist.x86.exe for phBot (default: prebuilt/)
  --skip-deps         do not install system packages
  --skip-wine         do not (re)build wine-sro (use existing)
  --wine-only         packages + wine-sro only, no clients
  --deps-only         install build dependencies only (needs sudo), then stop
  -y | --yes          no prompts (wizard with all defaults)
  --status-json       print current install status as JSON and exit (for a GUI wrapper)
  --toggle-wined3d              flip the force-WineD3D flag (VMs without Vulkan) and exit
  --toggle-phbot-channel        flip the persisted phBot channel (testing/stable) and exit
  --recreate-maxiguard-prefix   delete + recreate the shared MaxiGuard prefix and exit
  --remove-maxiguard            undo the MaxiGuard opt-in (prefix, launcher, GE-Proton copy) and exit
  --uninstall                   remove everything under the install root and exit
  --refresh-launcher            re-install sro.sh from this copy if it differs, and exit
  -h | --help         this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --games)       GAMES_DIR="$2"; ACTION_GIVEN=1; shift 2 ;;
        --maxiguard|--athens)      MG_ENV=1; ACTION_GIVEN=1
                       # folder is OPTIONAL: '--maxiguard' alone sets up the
                       # system-wide MaxiGuard environment (shared prefix + shim).
                       if [ $# -ge 2 ] && [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then MG_DIR="$2"; shift; fi
                       shift ;;
        --vsroplus|--herojourney)  VSRO_ENV=1; ACTION_GIVEN=1
                       # folder is OPTIONAL, same as --maxiguard: '--vsroplus'
                       # alone just opts into the vSroPlus prefix/mode so it
                       # shows up in the launcher, without needing a client yet.
                       if [ $# -ge 2 ] && [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then VSRO_DIR="$2"; shift; fi
                       shift ;;
        --plain)                   PLAIN_DIR="$2"; ACTION_GIVEN=1; shift 2 ;;
        --phbot)       DO_PHBOT=1; ACTION_GIVEN=1
                       if [ $# -ge 2 ] && [ -f "$2" ]; then PHBOT_INSTALLER="$2"; shift; fi
                       shift ;;
        --phbot-channel) case "${2:-}" in
                             testing|stable) PHBOT_CHANNEL="$2" ;;
                             *) die "--phbot-channel needs 'testing' or 'stable'" ;;
                         esac; shift 2 ;;
        --phbot-components)
                       PHBOT_COMPONENTS="$(phbot_components_parse "${2:-}")" \
                           || die "--phbot-components: unknown component in '${2:-}' (known: $PHBOT_COMPONENTS_ALL, all, none)"
                       PHBOT_COMPONENTS_SET=1; shift 2 ;;
        --phbot-reinstall) PHBOT_REINSTALL=1; shift ;;
        --vcredist)    VCREDIST="$2"; shift 2 ;;
        --accept-phbot-terms) export SRO_PHBOT_TERMS_ACCEPTED=1; shift ;;
        --skip-deps)   SKIP_DEPS=1; shift ;;
        --skip-wine)   SKIP_WINE=1; shift ;;
        --wine-only)   WINE_ONLY=1; ACTION_GIVEN=1; shift ;;
        --deps-only)   DEPS_ONLY=1; ACTION_GIVEN=1; shift ;;
        -y|--yes)      export SRO_ASSUME_YES=1; shift ;;
        --status-json) STATUS_JSON_ONLY=1; shift ;;
        --toggle-wined3d)            TOGGLE_WINED3D_ONLY=1; shift ;;
        --toggle-phbot-channel)      TOGGLE_PHBOT_CHANNEL_ONLY=1; shift ;;
        --recreate-maxiguard-prefix) RECREATE_MG_PREFIX_ONLY=1; shift ;;
        --remove-maxiguard)          REMOVE_MG_ONLY=1; shift ;;
        --uninstall)                 UNINSTALL_ONLY=1; shift ;;
        --refresh-launcher)          REFRESH_LAUNCHER_ONLY=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             die "Unknown option: $1  (--help for help)" ;;
    esac
done

# ------------------------------------------------------------------ detect game folders
# Classifies a folder by unambiguous markers.
classify_dir() {   # $1=dir -> prints "maxiguard" | "vsroplus" | "plain" | ""
    local d="$1"
    # Case-insensitive (_ci_file): real client releases are inconsistent
    # about exe/DLL casing (confirmed: a real client shipped a lowercase
    # silkroad.exe, which a literal -f check silently missed).
    # MaxiGuard markers take precedence (MaxiGuard.dll, otherwise Macro_Client.exe).
    if   [ -n "$(_ci_file "$d" MaxiGuard.dll)" ] || [ -n "$(_ci_file "$d" Macro_Client.exe)" ]; then echo maxiguard
    elif [ -n "$(_ci_file "$d" vsroplus_lib.dll)" ];                                            then echo vsroplus
    elif [ -n "$(_ci_file "$d" sro_client.exe)" ] || [ -n "$(_ci_file "$d" Silkroad.exe)" ];     then echo plain
    else echo ""; fi
}

scan_games() {   # scans GAMES_DIR (max 2 levels) for client folders
    local root="$1" d kind
    [ -d "$root" ] || return 0
    # root itself?
    kind="$(classify_dir "$root")"
    if [ -n "$kind" ]; then echo "$kind:$root"; fi
    # direct subfolders
    for d in "$root"/*/; do
        [ -d "$d" ] || continue
        kind="$(classify_dir "${d%/}")"
        [ -n "$kind" ] && echo "$kind:${d%/}"
    done
}

# Looks for a GameFolder containing clients at common locations (for the wizard
# default). Prints the first match or empty.
detect_games_default() {
    local c
    for c in "$HOME/Downloads/GameFolder" "$HOME/GameFolder" "$HOME/Games" \
             "$HOME/Downloads" "$PWD"; do
        [ -d "$c" ] || continue
        [ -n "$(scan_games "$c")" ] && { echo "$c"; return 0; }
    done
    echo ""
}

# ============================================================ status detection
# Each st_* returns 0 (present/done) or 1 (missing). Used by the status panel and
# the "continue - install what's missing" action.
st_deps() {   # key build tools present?
    local t
    for t in gcc make flex bison python3 curl git \
             i686-w64-mingw32-gcc x86_64-w64-mingw32-gcc; do
        command -v "$t" >/dev/null 2>&1 || return 1
    done
    return 0
}
st_wine()     { [ -x "$WINE_SRO_LOADER" ]; }
st_umu()      { umu_run_path >/dev/null 2>&1; }
st_32bit()    { case "$(detect_pm)" in
                  apt)    dpkg -s libc6:i386 >/dev/null 2>&1 ;;
                  pacman) pacman -Qi lib32-glibc >/dev/null 2>&1 ;;
                  dnf)    rpm -q glibc.i686 >/dev/null 2>&1 ;;
                  zypper) rpm -q glibc-32bit >/dev/null 2>&1 ;;
                  *)      return 1 ;;
                esac; }
st_ge()       { _mg_find_patched_ge >/dev/null 2>&1 || _mg_find_base_ge >/dev/null 2>&1; }
# Same broad search _mg_ensure_prefix() itself uses (lib/setup-prefix.sh) -
# setup happily reuses any already-valid prefix it finds (shared or under a
# game folder) rather than creating a needless duplicate, so the status
# check has to accept the same possibilities or it can say "missing" for a
# genuinely finished install.
st_mg_prefix(){ local p; p="$(_mg_find_any_prefix 2>/dev/null)" || true
                [ -n "$p" ] && [ -f "$p/drive_c/windows/syswow64/WUDFPlatform.dll" ]; }
st_maxiguard(){ st_umu && st_ge && st_mg_prefix; }   # the whole MaxiGuard env
# Same marker setup_vsroplus() always touches (whether called with a client
# folder or env-only) - mirrors _vsroplus_available() in sro-launcher.sh.
st_vsroplus() { [ -f "$STATE_DIR/vsroplus_enabled" ] || [ -f "$PREFIX_ROOT/vsroplus/system.reg" ]; }
# Both the .exe AND the generated phbot.sh launcher, not just the .exe -
# _write_launcher() only runs as the LAST step of setup_phbot(), so a run
# interrupted partway through (crashed, killed, a stale wineserver from an
# earlier attempt causing it to hang) can leave phBot.exe already installed
# with phbot.sh never written. Checking the .exe alone showed "done" in a
# real case like that, while actually starting phBot failed with "not set
# up - run ./swe.sh --phbot", which was true but contradicted the status
# panel - the status now reflects "can this actually be started", not just
# "did installation get partway there".
st_phbot()    { find "$PREFIX_ROOT/phbot/drive_c" -iname 'phBot.exe' -type f 2>/dev/null \
                  | grep -qviE '/(Temp|tmp|windows/Installer|Downloads)/' \
                && [ -f "$SRO_HOME/phbot.sh" ]; }
st_launcher() { [ -f "$SRO_HOME/sro.sh" ]; }
st_wined3d()  { wined3d_forced; }

_mark() {   # $1 = st_* function name -> coloured ✓/✗
    if "$1" 2>/dev/null; then printf '%s✓ done%s'    "$C_G" "$C_0"
    else                      printf '%s✗ missing%s' "$C_Y" "$C_0"; fi
}
_panel_line() {   # $1=label  $2=st fn  [$3=extra note]
    printf '   %-24s %b' "$1" "$(_mark "$2")"
    [ -n "${3:-}" ] && printf '   %s%s%s' "$C_B" "$3" "$C_0"
    printf '\n'
}
# Some components (like the WineD3D toggle) are an OPT-IN switch, not a
# required part of the setup - it being off is normal on most (GPU-passthrough)
# hosts, so showing it as "✗ missing" next to real prerequisites is misleading.
_panel_line_toggle() {   # $1=label  $2=st fn -> "✓ on" (green) or "optional (off)" (neutral)
    printf '   %-24s ' "$1"
    if "$2" 2>/dev/null; then printf '%s✓ on%s\n' "$C_G" "$C_0"
    else printf '%soptional (off)%s\n' "$C_B" "$C_0"; fi
}
print_status() {
    local wv=""; st_wine && wv="$("$WINE_SRO_LOADER" --version 2>/dev/null | head -1)"
    local ge=""; ge="$(_mg_find_patched_ge 2>/dev/null || _mg_find_base_ge 2>/dev/null || true)"
    printf '%s   Current status%s\n' "$C_B" "$C_0"
    _panel_line "Build dependencies" st_deps
    _panel_line "wine-sro build"     st_wine     "$wv"
    _panel_line "umu-launcher"       st_umu
    _panel_line "32-bit runtime (Steam Runtime)" st_32bit
    _panel_line "GE-Proton (MaxiGuard)" st_ge    "$( [ -n "$ge" ] && basename "$ge")"
    _panel_line "MaxiGuard prefix"   st_mg_prefix
    local pv; pv="$(phbot_installed_version)"
    _panel_line "phBot"              st_phbot   "channel: $(phbot_channel)${pv:+, v$pv}"
    _panel_line "Launcher (sro.sh)"  st_launcher
    _panel_line_toggle "Force WineD3D (no Vulkan)" st_wined3d
    printf '\n'
}
# Machine-readable version of print_status(), for a GUI wrapper (e.g. the
# AppImage) to poll instead of re-implementing every st_* check itself.
print_status_json() {
    local wv=""; st_wine && wv="$("$WINE_SRO_LOADER" --version 2>/dev/null | head -1)"
    local ge=""; ge="$(_mg_find_patched_ge 2>/dev/null || _mg_find_base_ge 2>/dev/null || true)"
    local gebase=""; [ -n "$ge" ] && gebase="$(basename "$ge")"
    printf '{'
    printf '"distro":"%s",'          "$(_json_esc "$(detect_pm)")"
    printf '"sro_home":"%s",'        "$(_json_esc "$SRO_HOME")"
    # Where build_wine_sro() works (lib/common.sh's BUILD_CACHE + the version
    # it builds). The GUI derives the wine build's own log paths from these
    # ($build_cache/wine-src/wine-$wine_build_version/build/build.log) to show
    # progress during the 20-60 min build, instead of hardcoding a layout that
    # only lib/build-wine-sro.sh actually decides.
    printf '"build_cache":"%s",'     "$(_json_esc "$BUILD_CACHE")"
    printf '"wine_build_version":"%s",' "$(_json_esc "$SRO_WINE_VERSION")"
    printf '"deps":%s,'              "$(st_deps      && echo true || echo false)"
    printf '"wine":%s,'              "$(st_wine      && echo true || echo false)"
    printf '"wine_version":"%s",'    "$(_json_esc "$wv")"
    printf '"umu":%s,'               "$(st_umu       && echo true || echo false)"
    printf '"runtime32":%s,'         "$(st_32bit     && echo true || echo false)"
    printf '"ge_proton":%s,'         "$(st_ge        && echo true || echo false)"
    printf '"ge_proton_build":"%s",' "$(_json_esc "$gebase")"
    printf '"maxiguard_prefix":%s,'  "$(st_mg_prefix && echo true || echo false)"
    printf '"maxiguard":%s,'         "$(st_maxiguard && echo true || echo false)"
    printf '"vsroplus":%s,'          "$(st_vsroplus  && echo true || echo false)"
    printf '"phbot":%s,'             "$(st_phbot     && echo true || echo false)"
    printf '"phbot_channel":"%s",'   "$(_json_esc "$(phbot_channel)")"
    printf '"phbot_components":"%s",' "$(_json_esc "$(phbot_components)")"
    printf '"phbot_version":"%s",'   "$(_json_esc "$(phbot_installed_version)")"
    printf '"launcher":%s,'          "$(st_launcher  && echo true || echo false)"
    printf '"wined3d_forced":%s'     "$(wined3d_forced && echo true || echo false)"
    printf '}\n'
}

# ============================================================ action wrappers
act_deps()      { [ "$SKIP_DEPS" = 1 ] && { say "Skipping deps (--skip-deps)."; return 0; }; install_deps; }
act_wine()      { build_wine_sro; }
act_maxiguard() { setup_maxiguard ""; }        # system-wide env (umu + GE + prefix + shim)
act_phbot()     { setup_phbot "$PHBOT_INSTALLER" "$VCREDIST" "${1:-0}" "${PHBOT_CHANNEL:-$(phbot_channel)}" "$(phbot_selected_components)"; }   # $1=force reinstall
phbot_selected_components() {   # --phbot-components if given, else the persisted choice
    if [ "$PHBOT_COMPONENTS_SET" = 1 ]; then echo "$PHBOT_COMPONENTS"; else phbot_components; fi
}
# Terminal: ask for the phBot channel and each optional component (the same
# choices phBot's own installer offers). Sets PHBOT_CHANNEL/PHBOT_COMPONENTS.
ask_phbot_options() {
    local ch cur c out="" label
    ch="${PHBOT_CHANNEL:-$(phbot_channel)}"
    if [ "$ch" = testing ]; then
        ask "  -> phBot channel: use stable instead of testing (default)?" n && ch=stable
    else
        ask "  -> phBot channel: keep stable (no = testing)?" y || ch=testing
    fi
    PHBOT_CHANNEL="$ch"
    cur="$(phbot_selected_components)"
    for c in ${PHBOT_COMPONENTS_ALL//,/ }; do
        case "$c" in
            manager) label="phBot Manager (accounts/routines)" ;;
            plugins) label="plugins (Python runtime for plugins)" ;;
            navmesh) label="navmesh (walking/pathing data)" ;;
            minimap) label="minimap (map images)" ;;
        esac
        local def=n; case ",$cur," in *",$c,"*) def=y ;; esac
        ask "  -> also install the $label?" "$def" && out="${out:+$out,}$c"
    done
    PHBOT_COMPONENTS="$out"; PHBOT_COMPONENTS_SET=1
}

# phBot's Terms and Conditions, asked for right BEFORE a run that would
# download phBot (phBot missing, or a reinstall) - and before run_step,
# whose spinner hides a step's output: a prompt inside a step would be
# invisible. Returns 1 when phBot has to be left out of this run.
phbot_gate() {   # $1 = 1 for a forced reinstall
    [ "${1:-0}" = 1 ] || ! st_phbot || return 0        # nothing to install - no terms needed
    phbot_terms_ok && return 0
    warn "phBot left out: its Terms and Conditions were not accepted ($SRO_PHBOT_TERMS_PAGE).
   Read them, then run again - in a terminal it asks, or pass --accept-phbot-terms."
    return 1
}
act_games() {   # scan a GameFolder and set up every client found
    local dir="${1:-}"
    [ -n "$dir" ] || dir="$(ask_str "Path to the GameFolder (empty = cancel)" "$(detect_games_default)")"
    [ -n "$dir" ] || return 0
    [ -d "$dir" ] || { warn "Folder '$dir' does not exist."; return 0; }
    step "Scanning for game folders under '$dir'"
    local found=0 line kind d
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        found=1; kind="${line%%:*}"; d="${line#*:}"
        case "$kind" in
            maxiguard) setup_maxiguard "$d" ;;
            vsroplus)  setup_vsroplus  "$d" ;;
            plain)     setup_plain     "$d" ;;
        esac
    done < <(scan_games "$dir")
    [ "$found" = 1 ] || warn "No known client folders found under '$dir'."
}
finish_launcher() { write_universal_launcher; say "Launcher ready: $SRO_HOME/sro.sh"; }
act_toggle_wined3d() {   # some hosts (VMs without GPU passthrough) have no working
                         # Vulkan driver at all, so DXVK fails and the client aborts
                         # right after boot. This forces WineD3D (OpenGL/llvmpipe)
                         # instead on every MaxiGuard launch (client + phBot).
    mkdir -p "$STATE_DIR"
    if wined3d_forced; then
        rm -f "$WINED3D_FLAG"
        say "WineD3D forcing disabled - back to DXVK (Vulkan)."
    else
        touch "$WINED3D_FLAG"
        say "WineD3D forcing enabled - MaxiGuard launches now use PROTON_USE_WINED3D=1."
        echo "   Use this only if DXVK fails with 'Failed to create Vulkan instance'"
        echo "   (typical for VMs without GPU passthrough, e.g. plain QEMU/bochs-drm)."
    fi
}
act_toggle_phbot_channel() {   # flips the persisted phBot channel; only takes
                                # effect on the NEXT (re)install of phBot.
    mkdir -p "$STATE_DIR"
    if [ "$(phbot_channel)" = testing ]; then
        printf 'stable\n' > "$PHBOT_CHANNEL_FILE"
        say "phBot channel set to: stable"
    else
        printf 'testing\n' > "$PHBOT_CHANNEL_FILE"
        say "phBot channel set to: testing (default)"
    fi
    echo "   Takes effect on the next phBot (re)install."
}

# Set up whatever is still missing (the "continue where you left off" action).
# MaxiGuard is a heavy, separate, OPT-IN environment (GE-Proton + umu-run +
# its own prefix) - phBot/plain/vSroPlus never need it, so it is only ever
# offered here, never forced.
run_missing() {
    step "Continuing - setting up everything that is missing"
    local do_phbot=0
    # phBot's options are asked up front, like everything else here - not
    # once the run reaches phBot after the long wine build.
    st_phbot || { ask_phbot_options; phbot_gate && do_phbot=1; }
    st_deps  || run_step "Installing build dependencies" act_deps
    st_wine  || run_step "Building wine-sro (~20-60 min)" act_wine
    [ "$do_phbot" = 1 ] && run_step "Setting up phBot" act_phbot
    if ! st_maxiguard; then
        if ask "Also set up the MaxiGuard environment (GE-Proton + umu-run)? Only needed for MaxiGuard-protected servers." n; then
            run_step "Setting up MaxiGuard environment" act_maxiguard
        fi
    fi
    finish_launcher
    say "All core components are set up."
}
# Non-interactive full run (-y, or no TTY): deps + wine (if missing) + any
# client auto-detected under the default GameFolder locations + phBot.
# Does NOT force a 20-60 min rebuild when wine-sro already exists. A MaxiGuard
# ENVIRONMENT (GE-Proton + umu-run on its own, without a client folder) stays
# opt-in (pass --maxiguard explicitly) - but if a MaxiGuard-protected client is
# actually found by the scan below, it is set up automatically, same as
# vSroPlus/plain: pointing this installer at your client folder(s) is enough,
# it should not require a second, separate flag to notice what is right there.
run_auto() {
    local do_phbot=1
    phbot_gate || do_phbot=0
    [ "$SKIP_DEPS" = 1 ] || run_step "Installing build dependencies" install_deps
    st_wine || run_step "Building wine-sro (~20-60 min)" act_wine

    local games; games="$(detect_games_default)"
    if [ -n "$games" ]; then
        step "Auto-detected game folder: $games"
        local found=0 line kind d
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            found=1; kind="${line%%:*}"; d="${line#*:}"
            case "$kind" in
                maxiguard) run_step "Setting up MaxiGuard ($d)" setup_maxiguard "$d" ;;
                vsroplus)  run_step "Setting up vSroPlus ($d)"  setup_vsroplus  "$d" ;;
                plain)     run_step "Setting up client ($d)"    setup_plain     "$d" ;;
            esac
        done < <(scan_games "$games")
        [ "$found" = 1 ] || warn "No known client folders found under '$games'."
    fi

    [ "$do_phbot" = 1 ] && run_step "Setting up phBot" act_phbot
    finish_launcher
}

# ============================================================ interactive menu
# Minimal arrow-key selector (Up/Down + Enter; q quits). Sets MENU_IDX.
_read_key() {
    local k rest
    IFS= read -rsn1 k 2>/dev/null || return 1
    case "$k" in
        $'\x1b') read -rsn2 -t 0.05 rest 2>/dev/null || rest=""
                 case "$rest" in '[A') echo up;; '[B') echo down;; *) echo esc;; esac ;;
        ''|$'\r') # Enter: some terminals send a bare CR (\r) instead of a line
                 # ('' here, from \n) - accept both, and drain a paired second
                 # byte (\n after \r, or vice versa) so one physical Enter
                 # press never needs a second keystroke to register.
                 read -rsn1 -t 0.02 rest 2>/dev/null || true
                 echo enter ;;
        q|Q)     echo quit ;;
        *)       echo other ;;
    esac
}
MENU_IDX=0
menu_select() {   # $1=title ; rest=options ; sets MENU_IDX, returns 0 (enter) / 1 (quit)
    local title="$1"; shift
    local -a opts=("$@"); local n=${#opts[@]} i key
    [ "$MENU_IDX" -ge "$n" ] && MENU_IDX=0
    printf '%s   %s%s   %s(↑/↓, Enter, q=quit)%s\n\n' "$C_B" "$title" "$C_0" "$C_Y" "$C_0"
    printf '\033[?25l'
    local first=1
    while true; do
        [ "$first" = 1 ] || printf '\033[%dA' "$n"     # move cursor back up over the list
        first=0
        for i in $(seq 0 $((n-1))); do
            if [ "$i" = "$MENU_IDX" ]; then
                printf '\033[2K   %s❯ %s%s\n' "$C_G" "${opts[$i]}" "$C_0"
            else
                printf '\033[2K     %s\n' "${opts[$i]}"
            fi
        done
        key="$(_read_key)" || key=quit
        case "$key" in
            up)    MENU_IDX=$(( (MENU_IDX - 1 + n) % n )) ;;
            down)  MENU_IDX=$(( (MENU_IDX + 1) % n )) ;;
            enter) printf '\033[?25h'; return 0 ;;
            quit)  printf '\033[?25h'; return 1 ;;
        esac
    done
}
_pause() { printf '\n%sPress Enter to return to the menu ...%s' "$C_Y" "$C_0"; local x; IFS= read -r x || true; }

installer_menu() {
    while true; do
        clear 2>/dev/null || printf '\033[2J\033[H'
        printf '%s╔══════════════════════════════════════════════════════════╗%s\n' "$C_B" "$C_0"
        printf '%s║   SilkroadWineEnvironment installer   -   Silkroad + phBot on Wine      ║%s\n' "$C_B" "$C_0"
        printf '%s╚══════════════════════════════════════════════════════════╝%s\n' "$C_B" "$C_0"
        printf '   distro: %s    root: %s\n\n' "$(detect_pm)" "$SRO_HOME"
        print_status
        menu_select "What do you want to do?" \
            "Continue  -  set up everything missing" \
            "Reinstall EVERYTHING (rebuilds wine-sro, ~20-60 min)" \
            "Build dependencies (system packages)" \
            "Build / rebuild wine-sro" \
            "MaxiGuard environment (umu + GE-Proton + prefix)" \
            "Recreate MaxiGuard prefix (fix world-entry crash)" \
            "phBot (download + set up / update)" \
            "Game clients - scan a GameFolder" \
            "Toggle: force WineD3D (VMs without Vulkan/GPU)" \
            "Toggle: phBot channel (currently: $(phbot_channel))" \
            "Open the launcher (sro.sh)" \
            "Quit" \
            || return 0
        echo
        case "$MENU_IDX" in
            0) run_missing;                                   _pause ;;
            1) if ask "Reinstall EVERYTHING - rebuild wine-sro too?" n; then
                   local reinstall_phbot=0
                   ask_phbot_options
                   phbot_gate 1 && reinstall_phbot=1
                   run_step "Installing build dependencies" act_deps
                   run_step "Building wine-sro (~20-60 min)" act_wine
                   run_step "Setting up MaxiGuard environment" act_maxiguard
                   [ "$reinstall_phbot" = 1 ] && run_step "Setting up phBot" act_phbot 1
                   finish_launcher
                   say "Full reinstall done."
               fi;                                            _pause ;;
            2) run_step "Installing build dependencies" act_deps;  _pause ;;
            3) if st_wine; then
                   if ask "wine-sro exists - rebuild? (~20-60 min)" n; then run_step "Building wine-sro (~20-60 min)" act_wine
                   else say "Kept existing wine-sro."; fi
               else run_step "Building wine-sro (~20-60 min)" act_wine; fi;     _pause ;;
            4) if st_wine; then run_step "Setting up MaxiGuard environment" act_maxiguard; finish_launcher
               else warn "Build wine-sro first."; fi;         _pause ;;
            5) if st_wine; then
                   if ask "Delete the shared MaxiGuard prefix and recreate it fresh?" y; then
                       run_step "Recreating MaxiGuard prefix" recreate_maxiguard_prefix
                       finish_launcher; say "Done - try the client again via the launcher."
                   else say "Cancelled."; fi
               else warn "Build wine-sro first."; fi;         _pause ;;
            6) if st_wine; then
                   local force_phbot=0
                   if st_phbot; then
                       if ask "phBot is already installed - download it again (update/reinstall, configs are kept)?" n; then force_phbot=1; fi
                   fi
                   { ! st_phbot || [ "$force_phbot" = 1 ]; } && ask_phbot_options
                   phbot_gate "$force_phbot" && run_step "Setting up phBot" act_phbot "$force_phbot"
                   finish_launcher
               else warn "Build wine-sro first."; fi;         _pause ;;
            7) if st_wine; then
                   local dir; dir="$(ask_str "Path to the GameFolder (empty = cancel)" "$(detect_games_default)")"
                   if [ -n "$dir" ]; then run_step "Scanning game folders" act_games "$dir"; finish_launcher; fi
               else warn "Build wine-sro first."; fi;         _pause ;;
            8) act_toggle_wined3d;                             _pause ;;
            9) act_toggle_phbot_channel;                       _pause ;;
            10) if st_launcher; then "$SRO_HOME/sro.sh" || true
               else warn "Launcher not created yet - set up a client first."; _pause; fi ;;
            11) return 0 ;;
        esac
    done
}

# ============================================================ first-run wizard
# Fresh install (no launcher yet): ask EVERYTHING up front (one path, one
# phBot yes/no), show a summary, THEN do all the work in one go - instead of
# the old flow which only ever asked about the MaxiGuard environment and left
# actually setting up a client (vSroPlus/plain/MaxiGuard) to a separate menu
# entry nobody is told about on a fresh machine. Every less-common, one-off
# action (rebuild wine-sro, MaxiGuard-only environment, WineD3D toggle,
# recreate the MaxiGuard prefix, ...) is deliberately NOT asked about here -
# it lives in "Expert settings" instead, so this stays a handful of questions.
run_fresh_wizard() {
    clear 2>/dev/null || printf '\033[2J\033[H'
    printf '%s╔══════════════════════════════════════════════════════════╗%s\n' "$C_B" "$C_0"
    printf '%s║   SilkroadWineEnvironment installer   -   guided setup                   ║%s\n' "$C_B" "$C_0"
    printf '%s╚══════════════════════════════════════════════════════════╝%s\n\n' "$C_B" "$C_0"
    say "A couple of questions up front, then everything is set up in one go."
    say "Need something more specific (rebuild wine-sro, MaxiGuard-only"
    say "environment, WineD3D toggle, ...)? That's all under Expert settings."
    echo

    local dir; dir="$(ask_str "Path to your Silkroad client (or a GameFolder with several)" "$(detect_games_default)")"
    local -a w_kinds=() w_dirs=()
    if [ -n "$dir" ] && [ -d "$dir" ]; then
        local line
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            w_kinds+=("${line%%:*}"); w_dirs+=("${line#*:}")
        done < <(scan_games "$dir")
    elif [ -n "$dir" ]; then
        warn "Folder '$dir' does not exist."
    fi
    if [ "${#w_dirs[@]}" -eq 0 ] && [ -n "$dir" ] && [ -d "$dir" ]; then
        warn "No known client was recognised under '$dir'."
        if ask "Set it up anyway as a generic (plain) client?" y; then
            w_kinds+=("plain"); w_dirs+=("$dir")
        fi
    fi

    local want_phbot=0
    ask "Also set up phBot?" y && want_phbot=1
    [ "$want_phbot" = 1 ] && ! phbot_gate && want_phbot=0
    [ "$want_phbot" = 1 ] && ask_phbot_options

    # ---- summary first, THEN execute ---------------------------------
    echo; step "Summary - this will be set up:"
    st_deps || echo "   - build dependencies (system packages)"
    st_wine || echo "   - wine-sro (self-built Wine, ~20-60 min)"
    local i
    for i in "${!w_dirs[@]}"; do echo "   - ${w_kinds[i]} client: ${w_dirs[i]}"; done
    [ "$want_phbot" = 1 ] && echo "   - phBot (channel: $PHBOT_CHANNEL; with: $(c="$(phbot_selected_components)"; echo "${c:-nothing else}"))"
    echo

    if [ "${#w_dirs[@]}" -eq 0 ] && [ "$want_phbot" = 0 ]; then
        warn "Nothing selected - opening Expert settings instead."
        installer_menu; return 0
    fi
    ask "Proceed?" y || { say "Cancelled."; installer_menu; return 0; }

    st_deps || run_step "Installing build dependencies" act_deps
    st_wine || run_step "Building wine-sro (~20-60 min)" act_wine
    for i in "${!w_dirs[@]}"; do
        case "${w_kinds[i]}" in
            maxiguard) run_step "Setting up MaxiGuard (${w_dirs[i]})" setup_maxiguard "${w_dirs[i]}" ;;
            vsroplus)  run_step "Setting up vSroPlus (${w_dirs[i]})"  setup_vsroplus  "${w_dirs[i]}" ;;
            plain)     run_step "Setting up client (${w_dirs[i]})"    setup_plain     "${w_dirs[i]}" ;;
        esac
    done
    [ "$want_phbot" = 1 ] && run_step "Setting up phBot" act_phbot
    finish_launcher
    say "All set. Next time, starting this script opens the launcher directly."
    _pause
}

# swe.sh is the default starter: once a launcher exists, starting it is THE
# main action - Expert settings only needs to be visited again for
# maintenance (rebuilds, toggles, new clients). Before a launcher exists there
# is nothing to launch yet, so a fresh machine gets the guided wizard above.
top_menu() {
    st_launcher || { run_fresh_wizard; return 0; }
    while true; do
        clear 2>/dev/null || printf '\033[2J\033[H'
        printf '%s╔══════════════════════════════════════════════════════════╗%s\n' "$C_B" "$C_0"
        printf '%s║              SilkroadWineEnvironment                       ║%s\n' "$C_B" "$C_0"
        printf '%s╚══════════════════════════════════════════════════════════╝%s\n\n' "$C_B" "$C_0"
        printf '%s   ┌──────────────────────────────────────────────────────┐%s\n' "$C_G" "$C_0"
        printf '%s   │                                                      │%s\n' "$C_G" "$C_0"
        printf '%s   │        ▶▶   START THE SRO LAUNCHER   ◀◀              │%s\n' "$C_G" "$C_0"
        printf '%s   │                                                      │%s\n' "$C_G" "$C_0"
        printf '%s   └──────────────────────────────────────────────────────┘%s\n\n' "$C_G" "$C_0"
        MENU_IDX=0
        menu_select "What do you want to do?" \
            "Start the SRO Launcher" \
            "Expert settings (rebuilds, toggles, add another client, ...)" \
            "Quit" \
            || return 0
        echo
        case "$MENU_IDX" in
            0) "$SRO_HOME/sro.sh" || true ;;
            1) installer_menu ;;
            2) return 0 ;;
        esac
    done
}

# ------------------------------------------------------------------ go
[ "${STATUS_JSON_ONLY:-0}" = 1 ] && { print_status_json; exit 0; }
[ "${TOGGLE_WINED3D_ONLY:-0}" = 1 ] && { act_toggle_wined3d; exit 0; }
[ "${TOGGLE_PHBOT_CHANNEL_ONLY:-0}" = 1 ] && { act_toggle_phbot_channel; exit 0; }
if [ "${RECREATE_MG_PREFIX_ONLY:-0}" = 1 ]; then
    st_wine || die "Build wine-sro first (--wine-only)."
    recreate_maxiguard_prefix
    finish_launcher
    exit 0
fi
if [ "${REMOVE_MG_ONLY:-0}" = 1 ]; then
    remove_maxiguard
    exit 0
fi
# The installed sro.sh is a copy of lib/sro-launcher.sh taken at install time.
# A GUI from a newer/older AppImage calls this at startup when the two differ,
# so the launcher it drives always speaks the same flags as the GUI itself.
# Only ever REFRESHES an existing install - never creates one.
if [ "${REFRESH_LAUNCHER_ONLY:-0}" = 1 ]; then
    if [ ! -f "$SRO_HOME/sro.sh" ]; then
        say "No launcher installed yet - nothing to refresh."
    elif cmp -s "$HERE/lib/sro-launcher.sh" "$SRO_HOME/sro.sh"; then
        say "Launcher is up to date."
    else
        write_universal_launcher
        say "Launcher refreshed from this installer's copy."
    fi
    exit 0
fi
# Removes only what THIS installer created under $SRO_HOME - never the
# system's own Wine (never touched to begin with) and never the build cache
# (~/.cache/sro-linux), so a later reinstall can still reuse cached Wine
# sources instead of re-downloading everything. A GUI wrapper is expected to
# have already confirmed this with the user before passing the flag - by
# the time this runs there is no prompt left to show.
if [ "${UNINSTALL_ONLY:-0}" = 1 ]; then
    if [ -d "$SRO_HOME" ]; then
        say "Removing $SRO_HOME"
        rm -rf "$SRO_HOME"
        say "Uninstalled. Your Wine build cache under ~/.cache/sro-linux was kept (safe to delete too if you don't plan to reinstall)."
    else
        say "Nothing to remove - $SRO_HOME does not exist."
    fi
    exit 0
fi

# No flag given -> interactive status menu (on a TTY) or a full auto run.
if [ "$ACTION_GIVEN" = 0 ]; then
    if [ "${SRO_ASSUME_YES:-0}" = 1 ] || [ ! -t 0 ] || [ ! -t 1 ]; then
        step "SilkroadWineEnvironment installer (automatic)"
        run_auto
        step "Done."
    else
        top_menu
        step "Bye."
    fi
    exit 0
fi

# ------------------------------------------------------------------ flag-driven run
# The phase() wrappers below exist for non-TTY callers (the GUI): unlike the
# interactive menus further up, this path calls install_deps/build_wine_sro/
# setup_* directly rather than through run_step(), so without them nothing
# here announces which step is running and a GUI has no way to show overall
# progress. On a terminal phase() prints nothing extra and runs the command
# exactly as before.
step "SilkroadWineEnvironment installer"
say  "Distro/package manager : $(detect_pm)"
say  "Build Wine             : $SRO_WINE_VERSION (new WoW64, from source)"
say  "Install root           : $SRO_HOME"

# 1) system packages
#
# Every step that needs root lives in this block, deliberately. They used to be
# spread out - install_deps here, but ensure_umu/ensure_32bit_runtime inside
# setup_maxiguard, i.e. after the 20-60 min wine build - so the password had to
# be typed a second time once the cached credential had expired.
# phBot's terms are asked for NOW, not after the 20-60 min wine build.
if [ "$DO_PHBOT" = 1 ] && ! phbot_gate "$PHBOT_REINSTALL"; then DO_PHBOT=0; fi

# Only when something privileged actually follows - without a terminal this
# shows the GUI's password dialog, which must not pop up for e.g. a
# --skip-deps client setup that never needs root. (--games can turn up a
# MaxiGuard client, whose setup installs umu-launcher / 32-bit libs.) Nor for
# MaxiGuard when its prerequisites are already in - e.g. the GUI's install
# continuing after a --deps-only run that set them up; should the check miss
# something, _sudo still asks at the point it is needed.
mg_needs_root=0
if { [ "$MG_ENV" = 1 ] || [ -n "$MG_DIR" ]; } && ! { st_umu && st_32bit; }; then mg_needs_root=1; fi
if [ "$SKIP_DEPS" = 0 ] || [ "$mg_needs_root" = 1 ] || [ -n "$GAMES_DIR" ]; then
    sudo_prime || true
fi
if [ "$SKIP_DEPS" = 0 ]; then
    phase "Installing build dependencies" install_deps
else
    say "System packages skipped (--skip-deps)."
fi
if [ "$MG_ENV" = 1 ] || [ -n "$MG_DIR" ]; then
    phase "Installing MaxiGuard prerequisites" install_root_prereqs
fi

[ "$DEPS_ONLY" = 1 ] && { step "Done (build dependencies only)."; exit 0; }

# 2) wine-sro (self-contained Wine 11.15, new WoW64 - system Wine irrelevant)
if [ "$SKIP_WINE" = 1 ] && [ -x "$WINE_SRO_LOADER" ]; then
    say "wine-sro present - build skipped (--skip-wine)."
elif [ "$SKIP_WINE" = 1 ]; then
    die "--skip-wine set, but $WINE_SRO_LOADER is missing."
else
    phase "Building wine-sro (~20-60 min)" build_wine_sro
fi

[ "$WINE_ONLY" = 1 ] && { step "Done (environment only)."; exit 0; }

# 3) set up clients
SETUP_COUNT=0

# a) explicit paths
[ "$MG_ENV" = 1   ] && { phase "Setting up MaxiGuard environment" setup_maxiguard "$MG_DIR";    SETUP_COUNT=$((SETUP_COUNT+1)); }
[ "$VSRO_ENV" = 1 ] && { phase "Setting up vSroPlus environment"  setup_vsroplus  "$VSRO_DIR";  SETUP_COUNT=$((SETUP_COUNT+1)); }
[ -n "$PLAIN_DIR" ] && { phase "Setting up client ($PLAIN_DIR)"   setup_plain     "$PLAIN_DIR"; SETUP_COUNT=$((SETUP_COUNT+1)); }

# b) auto-detection under --games
if [ -n "$GAMES_DIR" ]; then
    step "Scanning for game folders under '$GAMES_DIR'"
    found=0
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        found=1
        kind="${line%%:*}"; dir="${line#*:}"
        case "$kind" in
            maxiguard) [ -n "$MG_DIR"    ] || { phase "Setting up MaxiGuard ($dir)" setup_maxiguard "$dir"; SETUP_COUNT=$((SETUP_COUNT+1)); } ;;
            vsroplus)  [ -n "$VSRO_DIR"  ] || { phase "Setting up vSroPlus ($dir)"  setup_vsroplus  "$dir"; SETUP_COUNT=$((SETUP_COUNT+1)); } ;;
            plain)     [ -n "$PLAIN_DIR" ] || { phase "Setting up client ($dir)"    setup_plain     "$dir"; SETUP_COUNT=$((SETUP_COUNT+1)); } ;;
        esac
    done < <(scan_games "$GAMES_DIR")
    [ "$found" = 1 ] || warn "No known client folders found under '$GAMES_DIR'."
fi

# c) phBot
if [ "$DO_PHBOT" = 1 ]; then
    phase "Setting up phBot" setup_phbot "$PHBOT_INSTALLER" "$VCREDIST" "$PHBOT_REINSTALL" \
        "${PHBOT_CHANNEL:-$(phbot_channel)}" "$(phbot_selected_components)"
    SETUP_COUNT=$((SETUP_COUNT+1))
fi

# ------------------------------------------------------------------ finish
step "Done"
if [ "$SETUP_COUNT" = 0 ]; then
    warn "No client was set up."
    echo "  Example: $0 --games \"/path/to/GameFolder\" --phbot"
    echo "  or:      $0 --maxiguard \"/path/MaxiGuard Client\""
else
    # write/update the interactive universal launcher
    write_universal_launcher
    say "$SETUP_COUNT target(s) set up."
    echo "  Universal launcher (interactive): $SRO_HOME/sro.sh"
    echo "     -> step 1: phBot or Silkroad client"
    echo "     -> step 2: fix mode (MaxiGuard / vSroPlus / plain)"
    echo "     -> step 3: pick the server folder in the console file browser"
    echo "     -> step 4: Launcher (Silkroad.exe) or Client (<exe> 0 /22 0 0)"
    echo "     Shortcut: $SRO_HOME/sro.sh phbot   (starts phBot directly)"
    echo "  Per-client launchers also live in the game folders (maxiguard.sh / vsroplus.sh /"
    echo "  plain.sh) and under $SRO_HOME/ (phbot.sh, phbot-maxiguard.sh)."
fi
