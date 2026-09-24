#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# sro.sh - interactive SRO control panel (installed copy of lib/sro-launcher.sh).
#
# A full-screen TUI control panel: arrow keys to move, Enter to select, Esc back.
# Clients/bots are started DETACHED in the background (Wine output -> log file),
# so you can run several at once, see how many are running, and stop them again.
#
# Shortcut: 'sro.sh phbot' starts phBot (background) and returns.
#
# All paths are derived from this script's own location (SRO_HOME), so the
# installed copy is fully self-contained.

# Force a UTF-8 locale for correct character-width math: every box-drawing
# border and padded row below assumes 1 glyph = 1 counted character. Some
# environments (fresh/minimal Ubuntu, containers, SSH sessions without locale
# forwarding) start bash in the C/POSIX locale, where multi-byte UTF-8
# sequences (saved client names/paths with accents, the box-drawing borders
# themselves) are counted per BYTE instead of per glyph - that alone is
# enough to shift every bordered row out of alignment ("versetztes Layout").
if [ "$(locale charmap 2>/dev/null)" != "UTF-8" ]; then
    for _sro_loc in en_US.UTF-8 en_US.utf8 C.UTF-8 C.utf8 de_DE.UTF-8 de_DE.utf8; do
        if locale -a 2>/dev/null | grep -qiFx "$_sro_loc"; then export LC_ALL="$_sro_loc"; break; fi
    done
    unset _sro_loc
fi

SRO_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WSRO_LOADER="$SRO_HOME/wine-sro/bin/wine"
WSRO_SERVER="$SRO_HOME/wine-sro/bin/wineserver"
PHBOT="$SRO_HOME/phbot.sh"
PHBOT_MG="$SRO_HOME/phbot-maxiguard.sh"
ASSETS="$SRO_HOME/assets"
CLIENTS="$SRO_HOME/clients.tsv"     # lines: mode<TAB>label<TAB>folder
RUNDIR="$SRO_HOME/run"              # one <id>.pid / <id>.info / <id>.log per instance

# Some hosts (VMs without GPU passthrough, e.g. plain QEMU/bochs-drm) have no
# working Vulkan driver, so DXVK fails with "Failed to create Vulkan instance"
# and the client aborts right after boot. Toggle via swe.sh's
# 'Toggle: force WineD3D' menu entry (touches/removes this flag file).
WINED3D_FLAG="$SRO_HOME/state/force_wined3d"
wined3d_forced() { [ -f "$WINED3D_FLAG" ]; }

# umu-run may be a native package (on PATH) or the zipapp the installer dropped
# into $SRO_HOME/umu. Resolve it here so MaxiGuard works either way.
umu_bin() {
    [ -x "$SRO_HOME/umu/umu-run" ] && { echo "$SRO_HOME/umu/umu-run"; return 0; }
    command -v umu-run 2>/dev/null && return 0
    return 1
}

APP_TITLE="SilkroadWineEnvironment"
APP_FOOT="made by delirus@delirus.biz"

# MaxiGuard is pinned to exactly ONE GE-Proton build (must match
# SRO_GE_PROTON_VERSION in lib/setup-prefix.sh). Other Proton/proton-cachyos
# builds already installed on the host (e.g. proton-cachyos-nativ) are
# deliberately ignored - MaxiGuard crashes right after login on some of them.
SRO_GE_PROTON_VERSION="${SRO_GE_PROTON_VERSION:-GE-Proton11-7}"

# ---- theme (256-color) ----
C_BORDER=$'\033[38;5;39m'     # blue frame
C_TITLE=$'\033[1;38;5;51m'    # bright cyan title
C_FOOT=$'\033[38;5;245m'      # grey footer
C_HEAD=$'\033[1m'             # section title
C_HINT=$'\033[2m'             # dim hint
C_SEL=$'\033[48;5;24m\033[97m'  # selected row: blue bg, white fg
C_OK=$'\033[38;5;42m'
C_R=$'\033[0m'

# Leaving the alternate screen buffer here unconditionally would be harmless
# (terminals no-op it when not currently showing the alt buffer) but is only
# ever CORRECT to emit once the interactive session below has actually
# switched into it - ALT_SCREEN_ACTIVE tracks that so the 'sro.sh phbot'
# shortcut (which exits before ever entering the alt buffer) doesn't send it.
ALT_SCREEN_ACTIVE=0
_sro_exit_cleanup() {
    # To stderr, not stdout - --list-json/--status-json-style scriptable exits
    # print machine-readable output on stdout and this must never land there.
    printf '\033[?25h\033[?7h\033[0m' 1>&2
    [ "$ALT_SCREEN_ACTIVE" = 1 ] && printf '\033[?1049l' 1>&2
}
trap _sro_exit_cleanup EXIT INT TERM   # restore cursor + wrap + normal screen buffer

# ============================================================ drawing helpers
_rep() { local c="$1" n="$2" s='' i; for ((i=0;i<n;i++)); do s+="$c"; done; printf '%s' "$s"; }
_vislen() { local t; t=$(printf '%s' "$1" | sed $'s/\033\\[[0-9;]*m//g'); printf '%s' "${#t}"; }
clear_screen() { printf '\033[2J\033[3J\033[H'; }

_termsize() {
    # 'stty size' asks the kernel/pty directly for the real window size and
    # works everywhere, regardless of terminfo. 'tput cols/lines' instead
    # depends on TERM having a matching entry in the terminfo database - on a
    # fresh/minimal distro (e.g. Ubuntu without ncurses-term for the terminal
    # in use) that lookup can silently fail, making tput fall back to a
    # hardcoded 80x24 that does not match the real terminal and shifts the
    # whole frame off-screen. Try stty first, tput second, then env/defaults.
    local sz
    sz="$(stty size 2>/dev/null </dev/tty)"
    if [ -n "$sz" ]; then
        ROWS="${sz%% *}"; COLS="${sz##* }"
    else
        COLS=$( { tput cols; } 2>/dev/null || echo "${COLUMNS:-80}" )
        ROWS=$( { tput lines; } 2>/dev/null || echo "${LINES:-24}" )
    fi
    [ "$COLS" -ge 40 ] 2>/dev/null || COLS=80
    [ "$ROWS" -ge 12 ] 2>/dev/null || ROWS=24
}

# a border line of full width with a centered, colored label
_border() {   # $1=row $2=label $3=top|bottom|mid
    local row="$1" label="$2" which="$3" lc rc
    case "$which" in
        top)    lc='╔'; rc='╗' ;;
        bottom) lc='╚'; rc='╝' ;;
        *)      lc='╟'; rc='╢' ;;
    esac
    local inner=$((COLS-2))
    if [ -z "$label" ]; then
        printf '\033[%d;1H%s%s%s%s%s' "$row" "$C_BORDER" "$lc" "$(_rep '═' "$inner")" "$rc" "$C_R"
        return 0
    fi
    local text=" $label "
    local tl=${#text}
    [ "$tl" -gt "$inner" ] && { text="${text:0:inner}"; tl=$inner; }
    local left=$(((inner-tl)/2)) right
    right=$((inner-tl-left))
    printf '\033[%d;1H%s%s%s%s%s%s%s%s%s' "$row" "$C_BORDER" "$lc" \
        "$(_rep '═' "$left")" "$C_R$C_TITLE" "$text" "$C_R$C_BORDER" \
        "$(_rep '═' "$right")" "$rc" "$C_R"
}

# a bordered content line at absolute row; text may contain ANSI; optional select
_row() {   # $1=row $2=text $3=1(selected)
    local row="$1" text="$2" sel="${3:-}"
    local innerw=$((COLS-4))
    local vl; vl=$(_vislen "$text"); local pad=$((innerw-vl)); [ "$pad" -lt 0 ] && pad=0
    if [ "$sel" = 1 ]; then
        printf '\033[%d;1H%s║ %s%s%*s%s %s║%s' "$row" "$C_BORDER" \
            "$C_SEL" "$text" "$pad" "" "$C_R" "$C_BORDER" "$C_R"
    else
        printf '\033[%d;1H%s║%s %s%*s %s║%s' "$row" "$C_BORDER" "$C_R" \
            "$text" "$pad" "" "$C_BORDER" "$C_R"
    fi
}

# draw the whole frame: title bar, header (running count), separator, hint, footer
chrome() {   # $1 = optional extra header note
    _termsize
    printf '\033[?7l'   # no autowrap
    clear_screen
    local r
    _border 1 "$APP_TITLE" top
    for ((r=2; r<ROWS; r++)); do _row "$r" ""; done
    _border "$ROWS" "$APP_FOOT" bottom
    _border 3 "" mid
    # header (row 2): live running count
    local n; n="$(running_count)"
    local dot="$C_HINT●$C_R"; [ "$n" -gt 0 ] 2>/dev/null && dot="$C_OK●$C_R"
    _row 2 "$dot  Running background clients: ${C_HEAD}${n}${C_R}${1:+     $1}"
    # hint (row ROWS-1)
    _row $((ROWS-1)) "${C_HINT}↑/↓ move   ·   Enter select   ·   Esc back${C_R}"
}

TITLE_ROW=5
OPT_ROW=7
_opt_visible() { echo $(( (ROWS-2) - OPT_ROW + 1 )); }   # rows available for options

# frame_msg "section title" "line1" "line2" ...   (then waits for Enter)
frame_msg() {
    local title="$1"; shift
    chrome
    _row "$TITLE_ROW" "${C_HEAD}${title}${C_R}"
    local i=0 line
    for line in "$@"; do _row $((TITLE_ROW+2+i)) "$line"; i=$((i+1)); done
    printf '\033[?25h'
    _row $((ROWS-1)) "${C_HINT}Press Enter to continue...${C_R}"
    printf '\033[%d;3H' $((ROWS-1))
    local x; IFS= read -r x || true
}

# ============================================================ arrow-key menu
# menu "title" "opt1" ...  -> sets MENU_IDX (0-based); returns 1 on Esc.
MENU_IDX=-1
menu() {
    local title="$1"; shift
    local -a opts=("$@")
    local n=${#opts[@]} cur=0 top=0 k rest i vis
    [ "$n" -gt 0 ] || { MENU_IDX=-1; return 1; }

    chrome
    _row "$TITLE_ROW" "${C_HEAD}${title}${C_R}"
    printf '\033[?25l'

    _render() {
        vis=$(_opt_visible); [ "$vis" -lt 1 ] && vis=1
        [ "$cur" -lt "$top" ] && top=$cur
        [ "$cur" -ge $((top+vis)) ] && top=$((cur-vis+1))
        local r idx
        for ((r=0; r<vis; r++)); do
            idx=$((top+r))
            if [ "$idx" -lt "$n" ]; then
                if [ "$idx" -eq "$cur" ]; then _row $((OPT_ROW+r)) "» ${opts[idx]}" 1
                else                          _row $((OPT_ROW+r)) "  ${opts[idx]}"; fi
            else
                _row $((OPT_ROW+r)) ""
            fi
        done
    }
    _render
    while IFS= read -rsn1 k; do
        case "$k" in
            $'\x1b')
                read -rsn2 -t 0.05 rest || rest=""
                case "$rest" in
                    '[A'|'OA') cur=$(((cur-1+n)%n)) ;;
                    '[B'|'OB') cur=$(((cur+1)%n)) ;;
                    '[5') read -rsn1 -t 0.05 _x; cur=$((cur-vis)); [ "$cur" -lt 0 ] && cur=0 ;;
                    '[6') read -rsn1 -t 0.05 _x; cur=$((cur+vis)); [ "$cur" -ge "$n" ] && cur=$((n-1)) ;;
                    '') MENU_IDX=-1; printf '\033[?25h'; return 1 ;;
                    *) : ;;
                esac ;;
            ''|$'\r')
                # Enter: some terminals send a bare CR (\r) instead of a line
                # ('' here, from \n) - accept both, and drain a paired second
                # byte (\n after \r, or vice versa) so one physical Enter
                # press never needs a second keystroke to register (this was
                # the "have to press Enter twice" issue in the file browser).
                read -rsn1 -t 0.02 rest 2>/dev/null || true
                MENU_IDX=$cur; printf '\033[?25h'; return 0 ;;
            k) cur=$(((cur-1+n)%n)) ;;
            j) cur=$(((cur+1)%n)) ;;
            q) MENU_IDX=-1; printf '\033[?25h'; return 1 ;;
            *) : ;;
        esac
        _render
    done
    MENU_IDX=-1; printf '\033[?25h'; return 1
}

# Prompt inside the frame -> answer left in FRAME_PROMPT_REPLY (a global, NOT
# stdout - this draws the frame via printf/chrome/_row like every other TUI
# helper here, so it must be called as a plain statement, never wrapped in
# $(...): command substitution captures ALL of a subshell's stdout, so every
# escape sequence this draws would be silently swallowed into the captured
# string instead of ever reaching the screen - the terminal would just keep
# showing whatever the previous real menu drew (e.g. the client-picker list
# behind it), which is exactly the "layout looks broken" symptom this fixes.
FRAME_PROMPT_REPLY=""
frame_prompt() {   # $1=section $2=prompt $3=default
    chrome
    _row "$TITLE_ROW" "${C_HEAD}${1}${C_R}"
    _row $((TITLE_ROW+2)) "$2 ${C_HINT}[${3}]${C_R}"
    printf '\033[?25h'
    printf '\033[%d;3H> ' $((TITLE_ROW+4))
    local a; IFS= read -r a || a=""
    [ -n "$a" ] || a="$3"
    printf '\033[?25l'
    FRAME_PROMPT_REPLY="$a"
}

# ============================================================ saved clients
_clients_for() { [ -f "$CLIENTS" ] || return 0; awk -F '\t' -v m="$1" '$1==m {print $2 "\t" $3}' "$CLIENTS" 2>/dev/null || true; }
_client_add() {
    mkdir -p "$SRO_HOME"
    if [ -f "$CLIENTS" ]; then local tmp; tmp="$(mktemp)"; awk -F '\t' -v m="$1" -v f="$3" '!($1==m && $3==f)' "$CLIENTS" > "$tmp" 2>/dev/null || true; mv -f "$tmp" "$CLIENTS"; fi
    printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$CLIENTS"
}
_client_remove() { [ -f "$CLIENTS" ] || return 0; local tmp; tmp="$(mktemp)"; awk -F '\t' -v m="$1" -v f="$2" '!($1==m && $3==f)' "$CLIENTS" > "$tmp" 2>/dev/null || true; mv -f "$tmp" "$CLIENTS"; }

# ============================================================ file browser
# Deliberately duplicated from lib/common.sh instead of sourced: this file is
# installed by copying it verbatim to $SRO_HOME/sro.sh (see
# write_universal_launcher), so it has to stand on its own - nothing from
# common.sh is available at runtime.
#
# It was missing, and the three callers below silently degraded: every one of
# them runs _ci_file in a command substitution, so "command not found" just
# yielded an empty string. detect_client() therefore reported "no client" for
# EVERY folder and client type, which is what broke adding a server from the
# GUI, and the two Silkroad.exe launcher checks stopped ever finding one.
# Keep this in sync with _ci_file() in lib/common.sh.
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

detect_client() { local d="$1" e m; for e in sro_client.exe Macro_Client.exe Silkroad.exe Client.exe; do m="$(_ci_file "$d" "$e")" && { echo "$m"; return 0; }; done; echo ""; }

SELECTED=""
browse() {
    local cur="${1:-$HOME}"
    cur="$(cd "$cur" 2>/dev/null && pwd || echo "$HOME")"
    while true; do
        local -a labels=() targets=() kinds=()
        local ce; ce="$(detect_client "$cur")"
        local usetag="[ USE THIS FOLDER ]"; [ -n "$ce" ] && usetag="[ USE THIS FOLDER - client: $ce ]"
        labels+=("${C_OK}${usetag}${C_R}");         targets+=("$cur");              kinds+=(use)
        labels+=($'\033[1;33m''[ .. up ]'"$C_R");   targets+=("$(dirname "$cur")"); kinds+=(up)
        local d f
        while IFS= read -r d; do labels+=($'\033[1;36m''DIR  '"$(basename "$d")"'/'"$C_R"); targets+=("$d"); kinds+=(dir); done \
            < <(find "$cur" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | LC_ALL=C sort)
        while IFS= read -r f; do labels+=("${C_HINT}   $(basename "$f")${C_R}"); targets+=("$f"); kinds+=(file); done \
            < <(find "$cur" -maxdepth 1 -mindepth 1 -type f 2>/dev/null | LC_ALL=C sort)

        menu "Browse a server folder:   $cur" "${labels[@]}" || { SELECTED=""; return 1; }
        local i="$MENU_IDX"
        case "${kinds[i]}" in
            use)  SELECTED="$cur"; return 0 ;;
            up|dir) cur="${targets[i]}" ;;
            file) : ;;
        esac
    done
}

# ============================================================ background jobs
_alive() { if command -v pgrep >/dev/null 2>&1; then pgrep -s "$1" >/dev/null 2>&1; else kill -0 "$1" 2>/dev/null; fi; }

running_ids() {
    local p pid
    [ -d "$RUNDIR" ] || return 0
    for p in "$RUNDIR"/*.pid; do
        [ -f "$p" ] || continue
        pid="$(cat "$p" 2>/dev/null)"
        if [ -n "$pid" ] && _alive "$pid"; then echo "${p%.pid}"; else rm -f "$p" "${p%.pid}.info"; fi
    done
}
running_count() { { running_ids; foreign_jobs; } 2>/dev/null | grep -c . ; }

# ---------------------------------------------- processes started OUTSIDE sro.sh
# phBot/Manager/a client can end up running without going through launch_bg -
# most commonly Manager itself starting a phBot instance as its own child, but
# also anything started by hand outside this menu. Detect those by scanning
# the process table for known exe names and excluding whatever session ids we
# already track (launch_bg's setsid group), so nothing is double-listed.
_tracked_sids() {   # session ids of everything already tracked via $RUNDIR
    local p pid
    [ -d "$RUNDIR" ] || return 0
    for p in "$RUNDIR"/*.pid; do
        [ -f "$p" ] || continue
        pid="$(cat "$p" 2>/dev/null)"
        [ -n "$pid" ] && ps -o sid= -p "$pid" 2>/dev/null | tr -d ' '
    done
}
# prints  sid<TAB>label<TAB>pid   - one row per distinct session id/job
# Matches on the exact process name (comm, e.g. "phBot.exe" - what wine sets
# the Linux process name to for the guest exe), NOT the full command line:
# args can be an arbitrary quoted blob (shell wrappers, this very menu's own
# text) that may coincidentally CONTAIN one of these names without actually
# BEING that process - comm doesn't have that false-positive problem.
# ":32" widens the column so a 16-char name like "Macro_Client.exe" isn't
# silently truncated to 15 and missed.
foreign_jobs() {
    local -a tracked=(); local t
    while IFS= read -r t; do [ -n "$t" ] && tracked+=("$t"); done < <(_tracked_sids)
    ps -eo pid,sid,comm:32 --no-headers 2>/dev/null | while read -r pid sid comm; do
        local lbl=""
        case "$comm" in
            [Pp][Hh][Bb]ot.exe)       lbl="phBot.exe" ;;
            [Mm]anager.exe)           lbl="Manager.exe" ;;
            [Ss]ro_client.exe)        lbl="sro_client.exe" ;;
            [Mm]acro_[Cc]lient.exe)   lbl="Macro_Client.exe" ;;
            [Ss]ilkroad.exe)          lbl="Silkroad.exe" ;;
            *) continue ;;
        esac
        local skip=0 s
        for s in "${tracked[@]}"; do [ "$s" = "$sid" ] && { skip=1; break; }; done
        [ "$skip" = 1 ] && continue
        printf '%s\t%s\t%s\n' "$sid" "$lbl" "$pid"
    done | awk -F'\t' '!seen[$1]++'   # one row per distinct session (job tree)
}
stop_foreign() {   # $1 = session id
    local sid="$1"
    command -v pkill >/dev/null 2>&1 || return 1
    pkill -TERM -s "$sid" 2>/dev/null || true
    local i; for i in 1 2 3 4 5 6; do pgrep -s "$sid" >/dev/null 2>&1 || break; sleep 0.3; done
    pgrep -s "$sid" >/dev/null 2>&1 && pkill -KILL -s "$sid" 2>/dev/null || true
}
# Terminates a single pid (TERM, then KILL if it doesn't go within ~1.8s).
_kill_single_pid() {   # $1 = pid
    local pid="$1" i
    _alive "$pid" || return 0
    kill -TERM "$pid" 2>/dev/null || true
    for i in 1 2 3 4 5 6; do _alive "$pid" || break; sleep 0.3; done
    _alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
}
# Stops a candidate (see _build_candidates below) and everything nested under
# it in OUR tree, i.e. via CAND_PARENT_IDX - NOT via OS ppid. An earlier
# version walked `ps ppid` here, which never actually found anything: Wine
# hands CreateProcess() off to wineserver, which starts the new process
# reparented to systemd/init, not as a real child of the calling process (see
# the process-tree comment below) - so that version only ever killed the one
# pid it was given, silently failing to stop anything nested below it. Only
# valid right after _build_candidates() has populated CAND_* for this screen.
stop_cand_subtree() {   # $1 = index into CAND_*
    local idx="$1" j
    for j in "${!CAND_PID[@]}"; do
        [ "${CAND_PARENT_IDX[j]}" = "$idx" ] && stop_cand_subtree "$j"
    done
    _kill_single_pid "${CAND_PID[idx]}"
}

# ---------------------------------------------- process tree (who started whom)
# Manager can spawn phBot, and phBot/Manager spawn the actual game client - but
# NOT as a direct OS child process: Wine hands the actual CreateProcess() off to
# wineserver, which starts the new process independently of the calling one, so
# it ends up reparented to the user's systemd/init (ppid 1, or the user
# session's systemd --user), with its own fresh session id. Walking pid -> ppid
# (what an earlier version of this did) therefore finds NOTHING to nest -
# confirmed by inspecting a live Manager/phBot/client trio, all three showing
# the SAME ppid (systemd --user), each its own session leader.
#
# What DOES reliably link them: every process Wine spawns for a given prefix
# keeps that prefix's WINEPREFIX in its own environment (visible via
# /proc/<pid>/environ), and Manager/phBot/the client it drives all run in
# whichever ONE prefix that job uses. So group by WINEPREFIX instead of ppid,
# and order same-prefix processes by start time (Manager necessarily starts
# before the phBot it spawns, which necessarily starts before the client it
# spawns) - verified against a real running trio to come out in the right order.
_cand_wineprefix() {   # $1=pid -> its WINEPREFIX env value, or empty
    [ -r "/proc/$1/environ" ] || return 0
    tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | sed -n 's/^WINEPREFIX=//p' | head -1
}
_cand_starttime() {   # $1=pid -> a sortable "when did this start" number (clock
                      # ticks since boot, field 22 of /proc/pid/stat - plain
                      # mtime is only 1-second resolution, too coarse when a
                      # spawner launches its child within the same second)
    local raw rest
    raw="$(cat "/proc/$1/stat" 2>/dev/null)" || { echo 0; return 0; }
    rest="${raw##*) }"   # drop "pid (comm) " - comm itself may contain spaces
    set -- $rest
    printf '%s' "${20:-0}"
}
_job_label_for_comm() {   # exact process name (comm) -> our friendly label, or ""
    case "$1" in
        [Pp][Hh][Bb]ot.exe)       echo "phBot.exe" ;;
        [Mm]anager.exe)           echo "Manager.exe" ;;
        [Ss]ro_client.exe)        echo "sro_client.exe" ;;
        [Mm]acro_[Cc]lient.exe)   echo "Macro_Client.exe" ;;
        [Ss]ilkroad.exe)          echo "Silkroad.exe" ;;
        *)                        echo "" ;;
    esac
}
# Our own tracked jobs are always rank 0 (top): each one was started as its
# own deliberate action from this menu, so two tracked jobs must never nest
# under each other even if they happen to share a prefix (e.g. two Manager
# instances of the SAME mode - that prefix path is fixed per mode, not per
# instance). Manager/phBot/client get the known logical order below it.
_job_rank_for_comm() {
    case "$1" in
        [Mm]anager.exe)         echo 1 ;;
        [Pp][Hh][Bb]ot.exe)     echo 2 ;;
        *)                      echo 3 ;;   # sro_client/Macro_Client/Silkroad.exe
    esac
}

# Builds the flat candidate list (every tracked job's top pid + every other
# live process matching a known name), and for each one the index of its
# nesting parent (or -1 if none - a root).
declare -a CAND_PID CAND_LABEL CAND_KIND CAND_REF CAND_SID CAND_PFX CAND_START CAND_RANK CAND_PARENT_IDX
_build_candidates() {
    CAND_PID=(); CAND_LABEL=(); CAND_KIND=(); CAND_REF=(); CAND_SID=()
    CAND_PFX=(); CAND_START=(); CAND_RANK=(); CAND_PARENT_IDX=()

    local b pid lbl since log
    while IFS= read -r b; do
        [ -n "$b" ] || continue
        pid="$(cat "$b.pid" 2>/dev/null)"
        IFS=$'\t' read -r lbl since log < "$b.info" 2>/dev/null || { lbl="?"; since="?"; }
        CAND_PID+=("$pid"); CAND_LABEL+=("${C_HEAD}${lbl}${C_R}   ${C_HINT}since ${since}${C_R}")
        CAND_KIND+=("tracked"); CAND_REF+=("$b"); CAND_SID+=(""); CAND_RANK+=(0)
        CAND_PFX+=("$(_cand_wineprefix "$pid")"); CAND_START+=("$(_cand_starttime "$pid")")
    done < <(running_ids)

    local pid2 sid2 comm2 lbl2 skip j
    while read -r pid2 sid2 comm2; do
        [ -n "$pid2" ] || continue
        lbl2="$(_job_label_for_comm "$comm2")"; [ -n "$lbl2" ] || continue
        skip=0
        for j in "${!CAND_PID[@]}"; do [ "${CAND_PID[j]}" = "$pid2" ] && { skip=1; break; }; done
        [ "$skip" = 1 ] && continue
        CAND_PID+=("$pid2"); CAND_LABEL+=("${C_HEAD}${lbl2}${C_R}   ${C_HINT}pid ${pid2}${C_R}")
        CAND_KIND+=("foreign"); CAND_REF+=("pid:$pid2"); CAND_SID+=("$sid2")
        CAND_RANK+=("$(_job_rank_for_comm "$comm2")")
        CAND_PFX+=("$(_cand_wineprefix "$pid2")"); CAND_START+=("$(_cand_starttime "$pid2")")
    done < <(ps -eo pid=,sid=,comm:32= 2>/dev/null)

    local i; for i in "${!CAND_PID[@]}"; do CAND_PARENT_IDX+=(-1); done

    local -A groups   # WINEPREFIX -> space-separated candidate indices, insertion order
    for i in "${!CAND_PID[@]}"; do
        local pfx="${CAND_PFX[i]}"
        [ -n "$pfx" ] || continue
        groups["$pfx"]="${groups[$pfx]:-} $i"
    done
    # Within a shared prefix, each candidate's parent is the NEAREST earlier
    # (by start time) candidate with a STRICTLY lower rank - not just "whoever
    # came right before it". That's what makes several phBots under one
    # Manager come out as siblings under that Manager instead of a false
    # phBot1 -> phBot2 -> phBot3 chain, while still letting Manager -> phBot ->
    # client nest correctly, and it never lets two rank-0 tracked jobs (e.g.
    # two Managers of the same mode, which share that mode's fixed prefix
    # path) attach to one another.
    local pfx idx sorted oi oj
    for pfx in "${!groups[@]}"; do
        sorted="$(for idx in ${groups[$pfx]}; do printf '%s\t%s\n' "${CAND_START[idx]}" "$idx"; done \
                  | sort -n | cut -f2)"
        local -a order=(); for idx in $sorted; do order+=("$idx"); done
        for ((oi=0; oi<${#order[@]}; oi++)); do
            # NOTE: kept as two statements deliberately - `local cur=X
            # cur_rank=${CAND_RANK[cur]}` on one line would expand the second
            # assignment's $cur BEFORE the first assignment takes effect (word
            # expansion happens before any assignment in the command runs),
            # silently using the PREVIOUS loop iteration's cur instead of this
            # one's.
            local cur="${order[oi]}"
            local cur_rank="${CAND_RANK[cur]}"
            for ((oj=oi-1; oj>=0; oj--)); do
                local cand="${order[oj]}"
                if [ "${CAND_RANK[cand]}" -lt "$cur_rank" ]; then
                    CAND_PARENT_IDX[cur]="$cand"
                    break
                fi
            done
        done
    done
}
_cand_has_children() {   # $1 = index -> 0 if it has children in CAND_*
    local idx="$1" j
    for j in "${!CAND_PID[@]}"; do [ "${CAND_PARENT_IDX[j]}" = "$idx" ] && return 0; done
    return 1
}
# Depth-first render order: roots first (tracked jobs before newly-seen
# foreign ones, since those are added first above), each immediately followed
# by its own nested children, indented so the parent/child relationship reads
# top-to-bottom without needing box-drawing connectors that could wrap oddly.
declare -a RENDER_IDX
declare -A RENDER_PREFIX RENDER_DEPTH
_emit_subtree() {   # $1=idx $2=depth
    local idx="$1" depth="$2" prefix="" k j
    for ((k=0; k<depth; k++)); do prefix+="   "; done
    [ "$depth" -gt 0 ] && prefix+="${C_HINT}\`-${C_R} "
    RENDER_IDX+=("$idx"); RENDER_PREFIX["$idx"]="$prefix"; RENDER_DEPTH["$idx"]="$depth"
    for j in "${!CAND_PID[@]}"; do
        [ "${CAND_PARENT_IDX[j]}" = "$idx" ] && _emit_subtree "$j" $((depth+1))
    done
}
_render_tree_order() {
    RENDER_IDX=(); RENDER_PREFIX=(); RENDER_DEPTH=()
    local i
    for i in "${!CAND_PID[@]}"; do
        [ "${CAND_PARENT_IDX[i]}" = "-1" ] && _emit_subtree "$i" 0
    done
}

# Minimal JSON string escaping (no jq dependency) - same approach as
# lib/common.sh's _json_esc, duplicated here because this file is installed
# standalone as sro.sh and does not source common.sh.
_json_esc() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

launch_bg() {   # $1=label $2=folder(or empty) ; rest = cmd args...
    local label="$1" folder="$2"; shift 2
    mkdir -p "$RUNDIR"
    local id; id="$(date +%s%N)"; local log="$RUNDIR/$id.log"
    setsid bash -c 'd="$1"; shift; [ -n "$d" ] && cd "$d" 2>/dev/null; exec "$@"' \
        _ "$folder" "$@" >"$log" 2>&1 </dev/null &
    local pid=$!
    disown 2>/dev/null || true
    echo "$pid" > "$RUNDIR/$id.pid"
    printf '%s\t%s\t%s\n' "$label" "$(date '+%F %T')" "$log" > "$RUNDIR/$id.info"
    LAST_LOG="$log"; LAST_PID="$pid"
}

SPIN_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

# Call right after launch_bg. Shows a short spinner animation instead of a
# static "Started" message. If the process is still alive after the grace
# period, flashes a green checkmark and RETURNS AUTOMATICALLY (no keypress)
# so the caller lands back on the main menu right away. If it died within the
# grace period (immediate crash), shows the error + a log tail and waits for
# Enter instead, since that is something the user actually needs to read.
# $1=label  $2..=extra hint lines shown together with the checkmark on success
# Headless counterpart to wait_started() below, for --start-phbot/
# --start-manager/--start-client: launch_bg's own job returns the instant
# the background wrapper is forked, well before the actual Wine process
# inside it has done anything - a GUI wrapper calling one of those flags
# saw its own subprocess finish in tens of milliseconds regardless of how
# long the real startup takes, so a "starting..." indicator it shows had
# nothing to actually show for. This waits out the same ~4s grace period
# the TUI's own spinner does and treats an immediate crash as failure,
# giving both a real duration to animate against and genuine crash
# detection instead of always reporting "started" whether or not it was.
wait_started_headless() {   # $1=label ; uses $LAST_PID/$LAST_LOG from launch_bg
    local label="$1" pid="$LAST_PID" log="$LAST_LOG"
    local ticks=0 max_ticks=27   # ~0.15s * 27 =~ 4s, same grace period as wait_started()
    while [ "$ticks" -lt "$max_ticks" ]; do
        if ! _alive "$pid"; then
            HEADLESS_ERR="${label} exited right after start.
$(tail -n 6 "$log" 2>/dev/null)"
            return 1
        fi
        sleep 0.15
        ticks=$((ticks+1))
    done
    return 0
}

wait_started() {
    local label="$1"; shift
    local pid="$LAST_PID" log="$LAST_LOG"
    local ticks=0 max_ticks=27 fi=0   # ~0.15s * 27 =~ 4s grace period
    printf '\033[?25l'
    # Draw the frame/log line ONCE. Re-clearing+redrawing the whole screen on
    # every tick (the old chrome-in-the-loop approach) is what caused the
    # visible full-screen "flash" - only the spinner glyph itself needs to
    # change per tick, so from here on we only touch that one row in place.
    chrome
    _row $((TITLE_ROW+2)) "${C_HINT}log: ${log}${C_R}"
    while [ "$ticks" -lt "$max_ticks" ]; do
        if ! _alive "$pid"; then
            printf '\033[?25h'
            frame_msg "Failed to start" "${C_R}✗ ${label} exited right after start.${C_R}" "" \
                "log: ${C_HINT}${log}${C_R}" "" "$(tail -n 6 "$log" 2>/dev/null)"
            return 1
        fi
        _row "$TITLE_ROW" "${C_OK}${SPIN_FRAMES[fi % ${#SPIN_FRAMES[@]}]}${C_R}  Starting ${C_HEAD}${label}${C_R} ..."
        fi=$((fi+1)); ticks=$((ticks+1))
        sleep 0.15
    done
    _row "$TITLE_ROW" "${C_OK}✓ ${label}${C_R} is running."
    local i=0 line
    for line in "$@"; do _row $((TITLE_ROW+2+i)) "$line"; i=$((i+1)); done
    sleep 1.1
    printf '\033[?25h'
    return 0
}

# Runs a (potentially slow) command in the background while showing a spinner
# in place, instead of the screen just sitting there with no feedback. Used
# for one-off, blocking setup work that has to finish BEFORE launch_bg can
# start the actual client (e.g. creating a fresh wine-sro prefix on first
# use) - launch_bg + wait_started already animate the client start itself,
# but that prefix bootstrap step used to run silently first.
run_with_spinner() {   # $1=label ; rest = command...
    local label="$1"; shift
    # No terminal to animate on (a GUI-invoked subprocess via --start-client
    # etc. has none) - run it plainly instead, same fallback run_step() in
    # common.sh already uses. Drawing chrome()/_row() here regardless would
    # write raw escape codes to stdout, corrupting whatever the caller reads
    # back from it (this fed the same "escape codes leak into machine-
    # readable output" class of bug already fixed for the exit trap).
    if [ ! -t 1 ]; then
        "$@" >/dev/null 2>&1
        return $?
    fi
    chrome
    _row "$TITLE_ROW" "${C_HINT}Starting ...${C_R}"
    printf '\033[?25l'
    ( "$@" ) >/dev/null 2>&1 &
    local pid=$! fi=0
    while kill -0 "$pid" 2>/dev/null; do
        _row "$TITLE_ROW" "${C_OK}${SPIN_FRAMES[fi % ${#SPIN_FRAMES[@]}]}${C_R}  ${label} ..."
        fi=$((fi+1))
        sleep 0.15
    done
    wait "$pid" 2>/dev/null
    local rc=$?
    printf '\033[?25h'
    return "$rc"
}

stop_id() {
    local base="$1" pid i
    pid="$(cat "$base.pid" 2>/dev/null)"
    if [ -n "$pid" ]; then
        if command -v pkill >/dev/null 2>&1; then pkill -TERM -s "$pid" 2>/dev/null || true
        else kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true; fi
        for i in 1 2 3 4 5 6; do _alive "$pid" || break; sleep 0.3; done
        if _alive "$pid"; then
            if command -v pkill >/dev/null 2>&1; then pkill -KILL -s "$pid" 2>/dev/null || true
            else kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true; fi
        fi
    fi
    rm -f "$base.pid" "$base.info"
}

# ============================================================ launch helpers
find_ge() {
    local d b p
    for d in "$HOME/.steam/root/compatibilitytools.d" "$HOME/.local/share/Steam/compatibilitytools.d" "/usr/share/steam/compatibilitytools.d"; do
        # multi-arch GE-Proton tarballs extract as "<tag>-x86_64", so the -SRO
        # copy made from one is named "<tag>-x86_64-SRO"; older/manual installs
        # may still be plain "<tag>-SRO" - accept either.
        for b in "$SRO_GE_PROTON_VERSION" "$SRO_GE_PROTON_VERSION-x86_64"; do
            p="$d/$b-SRO"
            [ -x "$p/files/bin/wine" ] && { echo "$p"; return 0; }
        done
    done
    return 1
}

# One shared, Proton-created MaxiGuard prefix is enough for ALL MaxiGuard servers
# (it is server-agnostic). The ACPI shim lives INSIDE it (system32/syswow64), so
# no per-folder WUDFPlatform.dll is needed - new servers work without patching.
mg_prefix() {   # $1 = selected client folder (optional). prints a MaxiGuard-ready prefix.
    local folder="${1:-}" c p
    # Priority: the shared MaxiGuard prefix (the system-wide design goal; freshly
    # recreated by 'swe.sh -> Recreate MaxiGuard prefix') > the client's OWN
    # prefix > any scanned prefix. Backup/.bak copies are stale (they pass the VM
    # check but crash on world entry), so they are excluded.
    local -a cands=("$SRO_HOME/prefixes/maxiguard-sroprivate")
    [ -n "$folder" ] && cands+=("$folder/sroprivate")
    while IFS= read -r p; do cands+=("$p"); done \
        < <(find "$HOME/Games" "$HOME/Downloads" -maxdepth 4 -type d -name sroprivate 2>/dev/null \
              | grep -viE '/([Bb]ackup|\.bak)/' | LC_ALL=C sort)
    # 1) fully ready: HideWineExports=Y AND ACPI shim already present
    for c in "${cands[@]}"; do
        [ -f "$c/system.reg" ] || continue
        grep -aq '"HideWineExports"="Y"' "$c/user.reg" 2>/dev/null || continue
        [ -f "$c/drive_c/windows/syswow64/WUDFPlatform.dll" ] && { echo "$c"; return 0; }
    done
    # 2) HideWineExports=Y (shim gets added by mg_ensure_shim before launch)
    for c in "${cands[@]}"; do
        [ -f "$c/system.reg" ] || continue
        grep -aq '"HideWineExports"="Y"' "$c/user.reg" 2>/dev/null && { echo "$c"; return 0; }
    done
    # 3) last resort: any valid (non-backup) Proton prefix
    for c in "${cands[@]}"; do [ -f "$c/system.reg" ] && { echo "$c"; return 0; }; done
}
mg_ensure_shim() {   # $1 = prefix : put WUDFPlatform.dll + icu trio into the prefix (once)
    local p="$1" sw="$1/drive_c/windows/syswow64" s32="$1/drive_c/windows/system32" f
    mkdir -p "$sw" "$s32"
    [ -f "$sw/WUDFPlatform.dll" ]  || cp -f "$ASSETS/WUDFPlatform.dll" "$sw/WUDFPlatform.dll" 2>/dev/null || true
    [ -f "$s32/WUDFPlatform.dll" ] || cp -f "$ASSETS/WUDFPlatform.dll" "$s32/WUDFPlatform.dll" 2>/dev/null || true
    # icu trio from wine-sro (phBot imports icuuc.dll; some Proton builds lack it)
    local icusrc="$SRO_HOME/wine-sro/lib/wine/i386-windows"
    if [ -f "$icusrc/icuuc.dll" ]; then
        for f in icuuc.dll icuin.dll icu.dll; do
            [ -f "$sw/$f" ] || cp -f "$icusrc/$f" "$sw/$f" 2>/dev/null || true
        done
    fi
    # Wine's builtin UI fonts (phBot arrow glyphs render as boxes on some Proton
    # builds whose Tahoma/Marlett lack the glyphs). Replace the prefix's font
    # entries with the set that renders correctly. --remove-destination unlinks
    # the symlink Proton put there first, so we drop a real file INTO the prefix
    # instead of writing THROUGH the link into the shared Proton build.
    local fsrc="$SRO_HOME/wine-sro/share/wine/fonts" fdir="$1/drive_c/windows/Fonts"
    if [ -d "$fsrc" ]; then
        mkdir -p "$fdir"
        for f in tahoma.ttf tahomabd.ttf marlett.ttf symbol.ttf webdings.ttf wingding.ttf; do
            [ -f "$fsrc/$f" ] && cp -f --remove-destination "$fsrc/$f" "$fdir/$f" 2>/dev/null || true
        done
    fi
}

# The VC++ runtime is only ever installed into the shared MaxiGuard prefix
# ONCE, when it is first created (swe.sh's MaxiGuard setup / "Recreate
# MaxiGuard prefix"). A prefix picked up from a folder that never went through
# that step (e.g. an older, hand-rolled MaxiGuard install, or one whose OWN
# client installer already dropped an older Microsoft vcredist in) can have
# an OLDER VC++ runtime already registered as "Installed" - checking only for
# presence (not version) then wrongly skips installing our newer bundled one,
# and phBot/Manager keeps showing "a newer version of vcredist is required".
# So instead: track exactly WHICH vcredist build (by hash of the installer we
# ship) is currently applied via a marker file, and (re)install whenever the
# bundled one is different - the installer itself is safe to (re)run; a real
# Microsoft bundle just upgrades/no-ops instead of erroring.
# The vcredist bundle's own exit code is not reliable under Wine (its chainer
# can report a nonzero code even on a genuine success, e.g. 0x666/"already
# installed" passed through as something like 102 - same issue documented for
# _install_vcredist() in setup-prefix.sh). So verify the actual VC++ runtimes
# registry key instead of trusting the exit code, and only cache success once
# that's confirmed. An earlier version of both functions below wrote the
# marker unconditionally - one failed/interrupted install on a freshly
# created prefix (a real risk right after wineboot, before wineserver has
# fully settled) got permanently marked "done", so Manager/phBot kept
# popping the "an older version ... needs to be updated" dialog on every
# single start after that, with no way for a later run to ever retry.
_VCRT_KEY_WOW='HKLM\SOFTWARE\Wow6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86'
_VCRT_KEY_STD='HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86'
_vcredist_installed_in() {   # $1=wine loader binary  $2=prefix
    WINEPREFIX="$2" "$1" reg query "$_VCRT_KEY_WOW" /v Installed 2>/dev/null | grep -q '0x1' && return 0
    WINEPREFIX="$2" "$1" reg query "$_VCRT_KEY_STD" /v Installed 2>/dev/null | grep -q '0x1'
}

mg_ensure_vcredist() {   # $1 = prefix  $2 = GE-Proton build dir
    local pfx="$1" ge="$2" umu; umu="$(umu_bin)" || return 1
    local redist="$ASSETS/VC_redist.x86.exe"
    [ -f "$redist" ] || return 1
    local hash; hash="$(sha256sum "$redist" 2>/dev/null | cut -d' ' -f1)"
    local marker="$pfx/.vcredist-x86-installed"
    [ -n "$hash" ] && [ "$(cat "$marker" 2>/dev/null)" = "$hash" ] && return 0
    WINEPREFIX="$pfx" GAMEID="umu-sro" STORE=none PROTONPATH="$ge" \
        "$umu" "$redist" /install /quiet /norestart >/dev/null 2>&1 || true
    _vcredist_installed_in "$ge/files/bin/wine" "$pfx" || return 1
    [ -n "$hash" ] && printf '%s' "$hash" > "$marker" 2>/dev/null
    return 0
}

# Same idea, for a plain wine-sro prefix (native loader, no umu-run/Proton).
wsro_ensure_vcredist() {   # $1 = prefix
    local pfx="$1" redist="$ASSETS/VC_redist.x86.exe"
    [ -f "$redist" ] || return 1
    local hash; hash="$(sha256sum "$redist" 2>/dev/null | cut -d' ' -f1)"
    local marker="$pfx/.vcredist-x86-installed"
    [ -n "$hash" ] && [ "$(cat "$marker" 2>/dev/null)" = "$hash" ] && return 0
    WINEPREFIX="$pfx" WINEDEBUG=-all "$WSRO_LOADER" "$redist" /install /quiet /norestart >/dev/null 2>&1 || true
    WINEPREFIX="$pfx" "$WSRO_SERVER" -w 2>/dev/null || true
    _vcredist_installed_in "$WSRO_LOADER" "$pfx" || return 1
    [ -n "$hash" ] && printf '%s' "$hash" > "$marker" 2>/dev/null
    return 0
}

# The phBot Win10/11 user32 patch (adds exports like SkipPointerFrameMessages
# that Manager also calls) is applied PER wine-sro PREFIX by setup_phbot - it
# is NOT baked into wine-sro itself (unlike the MaxiGuard/GE-Proton build,
# where it IS baked system-wide - see _mg_bake_user32). Any wine-sro prefix
# other than the one phBot's setup already patched (like a fresh dedicated
# Manager prefix) is missing it, and Manager fails silently on startup
# ("SkipPointerFrameMessages ordinal 0 not found"). Rather than re-running
# the Python patcher here, just copy the already-patched DLL straight from
# the phBot prefix, which is required to exist anyway (Manager is installed
# by phBot itself, right next to it).
wsro_ensure_user32_patch() {   # $1 = prefix
    local pfx="$1" dst="$1/drive_c/windows/syswow64/user32.dll"
    local src="$SRO_HOME/prefixes/phbot/drive_c/windows/syswow64/user32.dll"
    [ -f "$src" ] || return 1
    [ -f "$dst" ] && cmp -s "$src" "$dst" && return 0
    mkdir -p "$(dirname "$dst")"
    cp -f "$src" "$dst" 2>/dev/null || return 1
    # user32 is memory-mapped by wineserver; a new mapping only takes effect
    # after that server instance restarts (same as setup_phbot's own step).
    WINEPREFIX="$pfx" "$WSRO_SERVER" -k 2>/dev/null || true
}

# wsro_ensure_vcredist()'s silent MSI install can leave the "Installed"
# registry key set (its own success signal) while the actual DLL FILES it
# places are still an older/different build than what phBot/Manager check
# via GetFileVersionInfo on the loaded DLL - confirmed on a real system: the
# registry key read 0x1 in a manager-plain prefix that still showed the "an
# older version ... needs to be updated" dialog, and every one of these six
# files differed byte-for-byte from the same files in the phbot prefix (which
# does NOT show that dialog). Rather than trust the installer a second time,
# just copy the already-verified-working set straight from the phbot prefix -
# same idea as wsro_ensure_user32_patch() above, for the exact same reason.
_VCRT_DLLS=(vcruntime140.dll msvcp140.dll msvcp140_1.dll msvcp140_2.dll msvcp140_atomic_wait.dll concrt140.dll)
wsro_ensure_vcredist_dlls() {   # $1 = prefix
    local pfx="$1" src="$SRO_HOME/prefixes/phbot/drive_c/windows/syswow64" \
        dst="$pfx/drive_c/windows/syswow64" f changed=0
    [ -d "$src" ] || return 1
    mkdir -p "$dst"
    for f in "${_VCRT_DLLS[@]}"; do
        [ -f "$src/$f" ] || continue
        if [ ! -f "$dst/$f" ] || ! cmp -s "$src/$f" "$dst/$f"; then
            cp -f "$src/$f" "$dst/$f" 2>/dev/null && changed=1
        fi
    done
    [ "$changed" = 1 ] && WINEPREFIX="$pfx" "$WSRO_SERVER" -k 2>/dev/null
    return 0
}

find_phbot_exe() {   # prints the installed phBot.exe (or empty)
    find "$SRO_HOME/prefixes/phbot/drive_c" -iname 'phBot.exe' -type f 2>/dev/null \
        | grep -viE '/(Temp|tmp|windows/Installer|Downloads)/' \
        | while IFS= read -r f; do printf '%s\t%s\n' "$(stat -c %Y "$f" 2>/dev/null || echo 0)" "$f"; done \
        | sort -rn | head -1 | cut -f2- || true
}

# The phBot Manager (account login / routine orchestration) is an optional
# component of the phBot setup (swe.sh --phbot-components manager), installed
# right next to phBot in the same prefix (.../AppData/Local/Programs/Manager/
# Manager.exe - phBot's own installer uses the same place). So we just look
# for it there, the same way find_phbot_exe looks for phBot.exe.
find_manager_exe() {   # prints the installed Manager.exe (or empty)
    find "$SRO_HOME/prefixes/phbot/drive_c" -iname 'Manager.exe' -type f 2>/dev/null \
        | grep -viE '/(Temp|tmp|windows/Installer|Downloads)/' \
        | while IFS= read -r f; do printf '%s\t%s\n' "$(stat -c %Y "$f" 2>/dev/null || echo 0)" "$f"; done \
        | sort -rn | head -1 | cut -f2- || true
}

# start_silkroad <mode> <folder> <label> <exe> [args...]
# Headless counterpart - see start_phbot_headless/start_manager_headless above
# for why this exists (frame_msg/wait_started draw a full-screen TUI and can
# block waiting on Enter, which is unusable from a GUI-invoked subprocess
# with no terminal attached). On success launch_bg has set LAST_PID/LAST_LOG;
# on failure HEADLESS_ERR holds a plain-text reason and the function returns 1.
start_silkroad_headless() {   # $1=mode $2=folder $3=label ; rest = exe [args...]
    HEADLESS_ERR=""
    local mode="$1" folder="$2" label="$3"; shift 3
    if [ "$mode" = maxiguard ]; then
        local ge; ge="$(find_ge)" || { HEADLESS_ERR="No patched GE-Proton *-SRO build found. Run once: ./swe.sh --skip-deps --skip-wine --maxiguard \"$folder\""; return 1; }
        local umu; umu="$(umu_bin)" || { HEADLESS_ERR="umu-run not found (package: umu-launcher). Run once: ./swe.sh --skip-deps --skip-wine --maxiguard (auto-installs it), or install it manually."; return 1; }
        local pfx; pfx="$(mg_prefix "$folder")"
        [ -n "$pfx" ] && [ -f "$pfx/system.reg" ] || { HEADLESS_ERR="No Proton-created MaxiGuard prefix found. Create it once (with ANY MaxiGuard folder): ./swe.sh --skip-deps --skip-wine --maxiguard \"$folder\""; return 1; }
        mg_ensure_shim "$pfx"     # shim lives in the prefix - no per-folder DLL needed
        # HKLM\Software\Wine is recreated by wineboot at the start of every fresh
        # wineserver session, so it must be deleted IN THE SAME process tree as the
        # client, right before exec'ing it (a separate prior umu-run call would not
        # stick). cmd.exe's "&" chains both in one wine session.
        local mgcmd="$1"; shift
        local a; for a in "$@"; do mgcmd="$mgcmd $a"; done
        # Hosts without a working Vulkan driver (VMs without GPU passthrough) need
        # WineD3D instead of DXVK - opt-in via 'Toggle: force WineD3D' in swe.sh.
        local wined3d_env=(); wined3d_forced && wined3d_env=(PROTON_USE_WINED3D=1)
        # Launch via umu-run (GE-Proton + Steam Linux Runtime): the ONLY combo that
        # both passes MaxiGuard's VM check AND survives world entry. Running GE Wine
        # directly passes the VM check but crashes when the client resets the device
        # entering the world - the full Proton runtime is what makes it stable.
        launch_bg "$label" "$folder" \
            env WINEPREFIX="$pfx" GAMEID="umu-sro" STORE=none PROTONPATH="$ge" \
                PROTON_DISABLE_NVAPI=1 WINE_HIDE_WINE_EXPORTS=1 "${wined3d_env[@]}" \
                "$umu" cmd /c "reg delete HKLM\\Software\\Wine /f & $mgcmd"
    else
        [ -x "$WSRO_LOADER" ] || { HEADLESS_ERR="wine-sro missing ($WSRO_LOADER)."; return 1; }
        local pfx="$SRO_HOME/prefixes/$mode"
        if [ ! -f "$pfx/system.reg" ]; then
            mkdir -p "$pfx"
            run_with_spinner "Preparing Wine prefix (first run)" bash -c '
                WINEPREFIX="$1" WINEDEBUG=-all "$2" wineboot -u >/dev/null 2>&1
                WINEPREFIX="$1" "$3" -w 2>/dev/null
            ' _ "$pfx" "$WSRO_LOADER" "$WSRO_SERVER"
        fi
        launch_bg "$label" "$folder" \
            env WINEPREFIX="$pfx" WINELOADER="$WSRO_LOADER" WINESERVER="$WSRO_SERVER" \
                WINEARCH=win64 WINEDEBUG=-all "$WSRO_LOADER" "$@"
    fi
    return 0
}

start_silkroad() {
    local mode="$1" folder="$2" label="$3"; shift 3
    if start_silkroad_headless "$mode" "$folder" "$label" "$@"; then
        wait_started "$label"
    else
        frame_msg "Error" "$HEADLESS_ERR"
        return 1
    fi
}

mode_label() { case "$1" in maxiguard) echo MaxiGuard;; vsroplus) echo vSroPlus;; plain) echo plain;; esac; }

# MaxiGuard and vSroPlus are OPTIONAL add-ons on top of a plain phBot/wine-sro
# install - a phBot-only setup has neither a GE-Proton build nor any vSroPlus
# client ever added, so there is nothing meaningful to pick between. Only
# offer a mode that is actually usable; if that leaves just "plain", skip the
# picker entirely instead of asking a question with one answer.
_maxiguard_available() { find_ge >/dev/null 2>&1; }
_vsroplus_available() {
    [ -f "$SRO_HOME/state/vsroplus_enabled" ] && return 0
    [ -n "$(_clients_for vsroplus)" ] && return 0
    # setup_vsroplus() always creates this prefix - covers a vSroPlus client
    # set up before the state marker above existed (or via --vsroplus/--games
    # without ever being added as a saved client in this menu).
    [ -f "$SRO_HOME/prefixes/vsroplus/system.reg" ] && return 0
    return 1
}

pick_mode() {
    local -a opts=() modes=()
    opts+=("plain       (wine-sro, no special protection / default)"); modes+=(plain)
    _maxiguard_available && { opts+=("MaxiGuard   (GE-Proton Wine, ACPI shim, HideWineExports, DXVK)"); modes+=(maxiguard); }
    _vsroplus_available  && { opts+=("vSroPlus    (wine-sro; mountmgr disk geometry built in)"); modes+=(vsroplus); }

    if [ "${#modes[@]}" -eq 1 ]; then MODE="${modes[0]}"; return 0; fi
    menu "For which client type?" "${opts[@]}" || return 1
    MODE="${modes[$MENU_IDX]}"
}

pick_client() {
    local mode="$1"
    while true; do
        local -a labels=() folders=() names=()
        while IFS=$'\t' read -r lbl fld; do
            [ -n "$fld" ] || continue
            local mk=""; [ -d "$fld" ] || mk="  ${C_HINT}(missing)${C_R}"
            labels+=("${C_HEAD}${lbl}${C_R}   ${C_HINT}${fld}${C_R}${mk}"); folders+=("$fld"); names+=("$lbl")
        done < <(_clients_for "$mode")
        local base=${#labels[@]}
        labels+=("${C_OK}[ + add a new client ]${C_R}")
        [ "$base" -gt 0 ] && labels+=($'\033[38;5;203m''[ - remove a client ]'"$C_R")

        menu "$(mode_label "$mode") clients - pick one:" "${labels[@]}" || return 1
        local i="$MENU_IDX"
        if [ "$i" -lt "$base" ]; then
            FOLDER="${folders[i]}"; CLABEL="${names[i]}"
            [ -d "$FOLDER" ] || { frame_msg "Missing" "Folder no longer exists:" "$FOLDER"; continue; }
            return 0
        elif [ "$i" -eq "$base" ]; then
            browse "$HOME/Games" || { [ -d "$HOME/Games" ] || browse "$HOME"; }
            [ -n "$SELECTED" ] || continue
            local ce; ce="$(detect_client "$SELECTED")"
            [ -n "$ce" ] || { frame_msg "Not saved" "No Silkroad client in that folder."; continue; }
            frame_prompt "Name this client" "Name:" "$(basename "$SELECTED")"
            local nm="$FRAME_PROMPT_REPLY"
            _client_add "$mode" "$nm" "$SELECTED"
            FOLDER="$SELECTED"; CLABEL="$nm"; return 0
        else
            local -a rlabels=() rfolders=()
            while IFS=$'\t' read -r lbl fld; do [ -n "$fld" ] || continue; rlabels+=("${C_HEAD}${lbl}${C_R}   ${C_HINT}${fld}${C_R}"); rfolders+=("$fld"); done < <(_clients_for "$mode")
            rlabels+=("${C_HINT}[ cancel ]${C_R}")
            menu "Remove which client?" "${rlabels[@]}" || continue
            local j="$MENU_IDX"
            [ "$j" -lt "${#rfolders[@]}" ] && _client_remove "$mode" "${rfolders[j]}"
        fi
    done
}

# ============================================================ flows
# ---- headless phBot/Manager starters -------------------------------------
# Shared by the interactive menu below AND the --start-phbot/--start-manager
# CLI flags (used by a GUI wrapper with no terminal attached). On success
# launch_bg has set LAST_PID/LAST_LOG as usual; on failure HEADLESS_ERR holds
# a plain-text reason and the function returns 1. Reporting via a global
# variable rather than stdout/stderr capture is deliberate: capturing this
# function's own output via $(...) would run it in a SUBSHELL, and any
# launch_bg globals (LAST_PID/LAST_LOG) it sets would then be lost the moment
# that subshell exits - the exact class of bug fixed earlier in frame_prompt.
HEADLESS_ERR=""
start_phbot_headless() {   # $1 = mode (plain|vsroplus|maxiguard)
    HEADLESS_ERR=""
    local mode="$1"
    if [ "$mode" = maxiguard ]; then
        # Prefer the generated phbot-maxiguard.sh: it has the working, fully
        # set-up prefix baked in (native VC++ redist etc.). Only if it is not
        # there yet, derive everything inline.
        if [ -x "$PHBOT_MG" ]; then
            # Best-effort: that prefix may predate the vcredist install step
            # (or it silently failed back then) - verify/fix it now too.
            local ge0 pfx0
            ge0="$(find_ge 2>/dev/null)"; pfx0="$(mg_prefix 2>/dev/null)"
            [ -n "$ge0" ] && [ -n "$pfx0" ] && [ -f "$pfx0/system.reg" ] && mg_ensure_vcredist "$pfx0" "$ge0" 2>/dev/null
            launch_bg "phBot (MaxiGuard / GE-Proton)" "" env WINEDEBUG=-all "$PHBOT_MG"
        else
            local ge; ge="$(find_ge)" || { HEADLESS_ERR="No patched GE-Proton *-SRO build found. Run ./swe.sh --skip-deps --skip-wine --maxiguard once."; return 1; }
            local umu; umu="$(umu_bin)" || { HEADLESS_ERR="umu-run not found (package: umu-launcher). Run ./swe.sh --skip-deps --skip-wine --maxiguard (auto-installs it)."; return 1; }
            local pfx; pfx="$(mg_prefix)"
            [ -n "$pfx" ] && [ -f "$pfx/system.reg" ] || { HEADLESS_ERR="No Proton-created MaxiGuard prefix found. Run ./swe.sh --skip-deps --skip-wine --maxiguard once."; return 1; }
            local pexe; pexe="$(find_phbot_exe)"
            [ -n "$pexe" ] || { HEADLESS_ERR="phBot.exe not found. Run ./swe.sh --phbot first."; return 1; }
            mg_ensure_shim "$pfx"
            mg_ensure_vcredist "$pfx" "$ge"
            local wined3d_env=(); wined3d_forced && wined3d_env=(PROTON_USE_WINED3D=1)
            # phBot runs IN the MaxiGuard prefix via umu-run, so the client it
            # spawns inherits the runtime that survives world entry.
            launch_bg "phBot (MaxiGuard / GE-Proton)" "$(dirname "$pexe")" \
                env WINEPREFIX="$pfx" GAMEID="umu-sro" STORE=none PROTONPATH="$ge" \
                    PROTON_DISABLE_NVAPI=1 WINE_HIDE_WINE_EXPORTS=1 "${wined3d_env[@]}" \
                    WINEDLLOVERRIDES="vcruntime140,msvcp140,msvcp140_1,msvcp140_2,msvcp140_atomic_wait,concrt140=n,b;icuuc,icuin,icu=n,b" \
                    "$umu" "$pexe"
        fi
    else
        [ -x "$PHBOT" ] || { HEADLESS_ERR="phBot not set up - run ./swe.sh --phbot."; return 1; }
        launch_bg "phBot ($mode / wine-sro)" "" env WINEDEBUG=-all "$PHBOT"
    fi
    return 0
}
start_manager_headless() {   # $1 = mode (plain|vsroplus|maxiguard)
    HEADLESS_ERR=""
    case "${1:-}" in plain|vsroplus|maxiguard) ;; *) HEADLESS_ERR="Unknown client type '${1:-}'."; return 1 ;; esac
    # NOT `local mode="$1" mdir="$MANAGER_ROOT_DIR/$mode"`: bash expands every
    # word of a `local` before assigning any of them, so $mode was still empty
    # there and every mode got the SAME folder ($MANAGER_ROOT_DIR itself) -
    # plain, vSroPlus and MaxiGuard Managers then shared one set of accounts
    # and settings. Two statements keep them apart.
    local mode="$1"
    local mdir="$MANAGER_ROOT_DIR/$mode"
    manager_split_legacy
    if [ ! -f "$mdir/Manager.exe" ]; then
        # Seed: the data the modes used to share (see manager_split_legacy),
        # so nobody loses their accounts - otherwise the installed Manager.
        local seed="$MANAGER_ROOT_DIR/$MANAGER_LEGACY_NAME"
        if [ ! -f "$seed/Manager.exe" ]; then
            local src; src="$(find_manager_exe)"
            [ -n "$src" ] || {
                HEADLESS_ERR="Manager.exe not found. Install it with the phBot setup: tick 'Manager' in the phBot options (GUI: Setup > Advanced > Set up / update phBot), or run ./swe.sh --skip-deps --skip-wine --phbot --phbot-reinstall --phbot-components all"
                return 1
            }
            seed="$(dirname "$src")"
        fi
        mkdir -p "$mdir"
        cp -a "$seed/." "$mdir/" 2>/dev/null || {
            HEADLESS_ERR="Could not create a dedicated $mode copy of the Manager ($mdir)."
            return 1
        }
        rm -f "$mdir/phBot"   # a copied shortcut points wherever the seed's did - recreated below
    fi
    local mexe="$mdir/Manager.exe"
    ensure_phbot_shortcut "$mdir"

    if [ "$mode" = maxiguard ]; then
        local ge; ge="$(find_ge)" || { HEADLESS_ERR="No patched GE-Proton *-SRO build found. Run ./swe.sh --skip-deps --skip-wine --maxiguard once."; return 1; }
        local umu; umu="$(umu_bin)" || { HEADLESS_ERR="umu-run not found (package: umu-launcher). Run ./swe.sh --skip-deps --skip-wine --maxiguard (auto-installs it)."; return 1; }
        local pfx; pfx="$(mg_prefix)"
        [ -n "$pfx" ] && [ -f "$pfx/system.reg" ] || { HEADLESS_ERR="No Proton-created MaxiGuard prefix found. Run ./swe.sh --skip-deps --skip-wine --maxiguard once."; return 1; }
        mg_ensure_shim "$pfx"
        mg_ensure_vcredist "$pfx" "$ge"
        local wined3d_env=(); wined3d_forced && wined3d_env=(PROTON_USE_WINED3D=1)
        # Runs in the SAME MaxiGuard prefix as the clients it will manage
        # (that prefix is already separate from the plain/vSroPlus ones below).
        launch_bg "phBot Manager (MaxiGuard / GE-Proton)" "$mdir" \
            env WINEPREFIX="$pfx" GAMEID="umu-sro" STORE=none PROTONPATH="$ge" \
                PROTON_DISABLE_NVAPI=1 WINE_HIDE_WINE_EXPORTS=1 "${wined3d_env[@]}" \
                WINEDLLOVERRIDES="vcruntime140,msvcp140,msvcp140_1,msvcp140_2,msvcp140_atomic_wait,concrt140=n,b;icuuc,icuin,icu=n,b" \
                "$umu" "$mexe"
    else
        [ -x "$WSRO_LOADER" ] || { HEADLESS_ERR="wine-sro missing ($WSRO_LOADER)."; return 1; }
        # Dedicated prefix per mode (manager-plain / manager-vsroplus) - NOT
        # the shared "phbot" prefix, so plain and vSroPlus never see each
        # other's Manager data either.
        local pfx="$SRO_HOME/prefixes/manager-$mode"
        if [ ! -f "$pfx/system.reg" ]; then
            mkdir -p "$pfx"
            WINEPREFIX="$pfx" WINEDEBUG=-all "$WSRO_LOADER" wineboot -u >/dev/null 2>&1 || true
            WINEPREFIX="$pfx" "$WSRO_SERVER" -w 2>/dev/null || true
        fi
        wsro_ensure_vcredist "$pfx"
        wsro_ensure_vcredist_dlls "$pfx"
        wsro_ensure_user32_patch "$pfx"
        # Same WINEDLLOVERRIDES as phbot.sh: force native vcruntime/msvcp (the
        # VC++ runtime just installed above into THIS prefix) over Wine's own
        # older builtins, and native user32 (the patch just copied in above,
        # required for the Win10/11 exports Manager calls) over Wine's builtin -
        # without this the Manager fails right on startup.
        launch_bg "phBot Manager ($mode / wine-sro)" "$mdir" \
            env WINEPREFIX="$pfx" WINELOADER="$WSRO_LOADER" WINESERVER="$WSRO_SERVER" \
                WINEARCH=win64 WINEDEBUG=-all \
                WINEDLLOVERRIDES="user32.dll=n,b;vcruntime140,msvcp140,msvcp140_1,msvcp140_2,msvcp140_atomic_wait,concrt140=n,b" \
                "$WSRO_LOADER" "$mexe"
    fi
    return 0
}

start_flow() {
    menu "Start what?" "phBot" "Silkroad client" || return 0
    local top=$MENU_IDX
    pick_mode || return 0

    if [ "$top" = 0 ]; then
        if start_phbot_headless "$MODE"; then
            if [ "$MODE" = maxiguard ]; then
                wait_started "phBot (MaxiGuard / GE-Proton)" "In phBot, set the client path to your MaxiGuard folder."
            else
                wait_started "phBot ($MODE / wine-sro)"
            fi
        else
            frame_msg "phBot / $MODE" "$HEADLESS_ERR"
        fi
        return 0
    fi

    pick_client "$MODE" || return 0
    local cexe; cexe="$(detect_client "$FOLDER")"
    [ -n "$cexe" ] || { frame_msg "Error" "No Silkroad client in '$FOLDER'."; return 0; }

    menu "What to start?" "Launcher (Silkroad.exe)" "Client   ($cexe  0 /22 0 0)" || return 0
    local lbl="$(mode_label "$MODE"): $CLABEL"
    local launcher_exe; launcher_exe="$(_ci_file "$FOLDER" "Silkroad.exe")"
    if [ "$MENU_IDX" = 0 ] && [ -n "$launcher_exe" ]; then
        start_silkroad "$MODE" "$FOLDER" "$lbl (launcher)" "$launcher_exe"
    else
        start_silkroad "$MODE" "$FOLDER" "$lbl" "$cexe" 0 /22 0 0
    fi
}

# Unlike phBot (one shared install/prefix for every mode), each of the three
# categories gets its OWN Manager copy in its OWN prefix here - Manager keeps
# local data (accounts/routines/settings) that must not mix between a plain,
# a vSroPlus and a MaxiGuard setup. The very first phBot-installed Manager.exe
# is only ever used as the SEED that gets cloned into each mode's own folder;
# after that, each mode's copy lives and is updated independently.
MANAGER_ROOT_DIR="$SRO_HOME/manager"

# Until this was fixed, every mode ran the Manager straight out of
# $MANAGER_ROOT_DIR itself (see start_manager_headless), so its accounts and
# settings were shared. Such a Manager is moved aside ONCE into
# $MANAGER_LEGACY_NAME, and each mode's own folder is seeded from it the first
# time that mode's Manager starts - every mode keeps what it had, and from
# then on they are independent. Nothing is deleted.
MANAGER_LEGACY_NAME="shared-before-split"
manager_split_legacy() {
    local root="$MANAGER_ROOT_DIR" e
    local legacy="$root/$MANAGER_LEGACY_NAME"
    [ -f "$root/Manager.exe" ] || return 0
    mkdir -p "$legacy" || return 1
    for e in "$root"/* "$root"/.[!.]*; do
        [ -e "$e" ] || [ -L "$e" ] || continue
        case "$(basename "$e")" in plain|vsroplus|maxiguard|"$MANAGER_LEGACY_NAME") continue ;; esac
        mv -f "$e" "$legacy/" 2>/dev/null || true
    done
}

# Manager's own "pick which phBot to control" file dialog starts next to
# Manager.exe - from there the real phBot install is several folders away
# (prefixes/phbot/drive_c/.../AppData/Local/Programs/phBot ...). Drop a
# symlink named "phBot" right beside each mode's Manager.exe so it shows up
# immediately in that dialog instead of requiring a manual dig through the
# wine prefix tree.
ensure_phbot_shortcut() {   # $1 = manager mode dir
    local mdir="$1" pexe target link="$1/phBot"
    pexe="$(find_phbot_exe)" && [ -n "$pexe" ] || return 1
    target="$(dirname "$pexe")"
    if [ -L "$link" ]; then
        [ "$(readlink -f "$link" 2>/dev/null)" = "$(readlink -f "$target" 2>/dev/null)" ] && return 0
        rm -f "$link"
    elif [ -e "$link" ]; then
        return 0   # something real already there (unlikely) - don't clobber it
    fi
    ln -s "$target" "$link" 2>/dev/null
}

# The Manager needs the SAME runtime the sro_client.exe it drives will end up
# running under (native wine-sro for plain/vSroPlus, GE-Proton/umu-run for
# MaxiGuard) - otherwise it cannot see/control that client's window. So it
# gets the exact same 3-way mode pick as phBot itself.
start_manager() {
    pick_mode || return 0
    if start_manager_headless "$MODE"; then
        if [ "$MODE" = maxiguard ]; then
            wait_started "phBot Manager (MaxiGuard / GE-Proton)"
        else
            wait_started "phBot Manager ($MODE / wine-sro)"
        fi
    else
        frame_msg "Manager / $MODE" "$HEADLESS_ERR"
    fi
}

view_log() {   # $1=log path (plain full-screen)
    printf '\033[?7h'; clear_screen; printf '\033[?25h'
    printf '%s=== %s ===%s\n\n' "$C_TITLE" "$1" "$C_R"
    tail -n $((${ROWS:-24}-6)) "$1" 2>/dev/null || echo "(no log)"
    printf '\n%sPress Enter to return...%s' "$C_HINT" "$C_R"; local x; IFS= read -r x || true
}

manage_flow() {
    while true; do
        _build_candidates
        _render_tree_order

        local -a labels=() refs=()
        local ri idx
        for ri in "${!RENDER_IDX[@]}"; do
            idx="${RENDER_IDX[ri]}"
            labels+=("${RENDER_PREFIX[$idx]}${CAND_LABEL[idx]}")
            refs+=("$idx")
        done

        local n=${#labels[@]}
        if [ "$n" -eq 0 ]; then menu "Nothing is running" "[ back ]" || return 0; return 0; fi
        local -a opts=("${labels[@]}" $'\033[38;5;203m''[ STOP ALL ]'"$C_R" "${C_HINT}[ back ]${C_R}")
        menu "Running clients - pick one to manage   ${C_HINT}(indented = started by the row above it)${C_R}" "${opts[@]}" || return 0
        local sel="$MENU_IDX"
        if [ "$sel" -lt "$n" ]; then
            local cidx="${refs[sel]}" haschild=1
            _cand_has_children "$cidx" || haschild=0
            if [ "${CAND_KIND[cidx]}" = tracked ]; then
                local rb="${CAND_REF[cidx]}" lbl since log
                IFS=$'\t' read -r lbl since log < "$rb.info" 2>/dev/null || true
                local stoplbl="Stop this client"; [ "$haschild" = 1 ] && stoplbl="Stop this + everything it started"
                menu "$lbl" "$stoplbl" "View log" "[ back ]" || continue
                case "$MENU_IDX" in
                    # stop_id takes down this job's own session; stop_cand_subtree
                    # additionally takes down whatever it spawned further down the
                    # tree, which (Wine reparenting away from its caller) usually
                    # lands in a DIFFERENT session that stop_id alone would miss.
                    0) stop_id "$rb"; stop_cand_subtree "$cidx"; frame_msg "Stopped" "${lbl}" ;;
                    1) view_log "$log" ;;
                    *) : ;;
                esac
            else
                local pid="${CAND_PID[cidx]}" parent_idx="${CAND_PARENT_IDX[cidx]}"
                local stoplbl="Stop this process"; [ "$haschild" = 1 ] && stoplbl="Stop this + everything it started"
                menu "${CAND_LABEL[cidx]}" "$stoplbl" "[ back ]" || continue
                case "$MENU_IDX" in
                    0) [ "$parent_idx" = -1 ] && stop_foreign "${CAND_SID[cidx]}"
                       stop_cand_subtree "$cidx"
                       frame_msg "Stopped" "Process (pid $pid) stopped." ;;
                    *) : ;;
                esac
            fi
        elif [ "$sel" -eq "$n" ]; then
            local x
            for x in "${!CAND_PID[@]}"; do
                if [ "${CAND_KIND[x]}" = tracked ]; then stop_id "${CAND_REF[x]}"
                elif [ "${CAND_PARENT_IDX[x]}" = -1 ]; then stop_foreign "${CAND_SID[x]}"; fi
                _kill_single_pid "${CAND_PID[x]}"
            done
            frame_msg "Stopped all" "All background clients (tracked + external) stopped."
        else
            return 0
        fi
    done
}

# ============================================================ main
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    mkdir -p "$RUNDIR"

    # Scriptable, non-interactive modes for a GUI wrapper (e.g. the AppImage)
    # to drive - these need no controlling terminal, so they're handled BEFORE
    # the "needs an interactive terminal" check below. They reuse the exact
    # same tree-building/stop logic as the "Manage" TUI screen, so a GUI never
    # has to re-derive the WINEPREFIX/rank grouping or the Wine-aware stop
    # semantics itself.
    if [ "${1:-}" = "--list-json" ]; then
        _build_candidates
        _render_tree_order
        printf '['
        _json_first=1
        for _json_ri in "${!RENDER_IDX[@]}"; do
            _json_idx="${RENDER_IDX[_json_ri]}"
            _json_plain="$(printf '%s' "${CAND_LABEL[_json_idx]}" | sed 's/\x1b\[[0-9;]*m//g')"
            _json_hc=false; _cand_has_children "$_json_idx" && _json_hc=true
            [ "$_json_first" = 1 ] || printf ','
            _json_first=0
            printf '{"ref":"%s","label":"%s","kind":"%s","depth":%s,"has_children":%s,"pid":"%s"}' \
                "$(_json_esc "${CAND_REF[_json_idx]}")" "$(_json_esc "$_json_plain")" "${CAND_KIND[_json_idx]}" \
                "${RENDER_DEPTH[$_json_idx]}" "$_json_hc" "${CAND_PID[_json_idx]}"
        done
        printf ']\n'
        exit 0
    fi
    if [ "${1:-}" = "--stop" ]; then
        [ -n "${2:-}" ] || { echo "usage: sro.sh --stop <ref>  (ref from --list-json)" >&2; exit 1; }
        _build_candidates
        _json_ref="$2"; _json_found=-1
        for _json_i in "${!CAND_REF[@]}"; do [ "${CAND_REF[_json_i]}" = "$_json_ref" ] && { _json_found="$_json_i"; break; }; done
        [ "$_json_found" != -1 ] || { echo "not found: $_json_ref" >&2; exit 1; }
        if [ "${CAND_KIND[_json_found]}" = tracked ]; then stop_id "${CAND_REF[_json_found]}"
        elif [ "${CAND_PARENT_IDX[_json_found]}" = -1 ]; then stop_foreign "${CAND_SID[_json_found]}"; fi
        stop_cand_subtree "$_json_found"
        echo "stopped"
        exit 0
    fi
    if [ "${1:-}" = "--modes-json" ]; then
        # Which of the 3 client types are actually usable right now - mirrors
        # pick_mode()'s own availability checks, so a GUI can grey out /
        # hide a mode exactly the same way the TUI's mode picker does instead
        # of offering a choice that would immediately fail.
        _mg=false; _maxiguard_available && _mg=true
        _vp=false; _vsroplus_available && _vp=true
        printf '{"plain":true,"maxiguard":%s,"vsroplus":%s}\n' "$_mg" "$_vp"
        exit 0
    fi
    if [ "${1:-}" = "--detect-client" ]; then
        [ -n "${2:-}" ] || { echo "usage: sro.sh --detect-client <folder>" >&2; exit 1; }
        detect_client "$2"
        exit 0
    fi
    if [ "${1:-}" = "--add-client" ]; then
        [ -n "${2:-}" ] && [ -n "${3:-}" ] && [ -n "${4:-}" ] \
            || { echo "usage: sro.sh --add-client <plain|vsroplus|maxiguard> <label> <folder>" >&2; exit 1; }
        _client_add "$2" "$3" "$4"
        echo "added"
        exit 0
    fi
    if [ "${1:-}" = "--remove-client" ]; then
        [ -n "${2:-}" ] && [ -n "${3:-}" ] \
            || { echo "usage: sro.sh --remove-client <plain|vsroplus|maxiguard> <folder>" >&2; exit 1; }
        _client_remove "$2" "$3"
        echo "removed"
        exit 0
    fi
    if [ "${1:-}" = "--start-phbot" ]; then
        [ -n "${2:-}" ] || { echo "usage: sro.sh --start-phbot <plain|vsroplus|maxiguard>" >&2; exit 1; }
        if start_phbot_headless "$2" && wait_started_headless "phBot"; then
            echo "started (log: $LAST_LOG)"; exit 0
        else echo "$HEADLESS_ERR" >&2; exit 1; fi
    fi
    if [ "${1:-}" = "--start-manager" ]; then
        [ -n "${2:-}" ] || { echo "usage: sro.sh --start-manager <plain|vsroplus|maxiguard>" >&2; exit 1; }
        if start_manager_headless "$2" && wait_started_headless "Manager"; then
            echo "started (log: $LAST_LOG)"; exit 0
        else echo "$HEADLESS_ERR" >&2; exit 1; fi
    fi
    # Which Windows exe a start would actually run - used by the GUI to take a
    # desktop shortcut's icon from the program itself. Same lookups the
    # --start-* flags above use, so the icon always matches what starts.
    if [ "${1:-}" = "--exe-path" ]; then
        _ep=""
        case "${2:-}" in
            phbot)    _ep="$(find_phbot_exe)" ;;
            manager)  if [ -n "${3:-}" ] && [ -f "$MANAGER_ROOT_DIR/$3/Manager.exe" ]; then
                          _ep="$MANAGER_ROOT_DIR/$3/Manager.exe"
                      else _ep="$(find_manager_exe)"; fi ;;
            # detect_client/_ci_file print just the on-disk file NAME.
            client)   [ -n "${3:-}" ] && _ep="$(detect_client "$3")" && [ -n "$_ep" ] && _ep="$3/$_ep" ;;
            launcher) [ -n "${3:-}" ] && _ep="$(_ci_file "$3" "Silkroad.exe")" && _ep="$3/$_ep" ;;
            *) echo "usage: sro.sh --exe-path <phbot|manager [mode]|client <folder>|launcher <folder>>" >&2; exit 1 ;;
        esac
        [ -n "$_ep" ] && [ -f "$_ep" ] || exit 1
        printf '%s\n' "$_ep"
        exit 0
    fi
    if [ "${1:-}" = "--start-client" ]; then
        [ -n "${2:-}" ] && [ -n "${3:-}" ] \
            || { echo "usage: sro.sh --start-client <plain|vsroplus|maxiguard> <folder> [launcher|client]" >&2; exit 1; }
        _sc_mode="$2"; _sc_folder="$3"; _sc_which="${4:-client}"
        _sc_exe="$(detect_client "$_sc_folder")"
        [ -n "$_sc_exe" ] || { echo "No Silkroad client executable found in: $_sc_folder" >&2; exit 1; }
        _sc_label="$(mode_label "$_sc_mode"): $(basename "$_sc_folder")"
        _sc_launcher_exe="$(_ci_file "$_sc_folder" "Silkroad.exe")"
        if [ "$_sc_which" = launcher ] && [ -n "$_sc_launcher_exe" ]; then
            start_silkroad_headless "$_sc_mode" "$_sc_folder" "$_sc_label (launcher)" "$_sc_launcher_exe"
        else
            start_silkroad_headless "$_sc_mode" "$_sc_folder" "$_sc_label" "$_sc_exe" 0 /22 0 0
        fi
        if [ $? -eq 0 ] && wait_started_headless "$_sc_label"; then
            echo "started (log: $LAST_LOG)"; exit 0
        else echo "$HEADLESS_ERR" >&2; exit 1; fi
    fi

    [ -t 0 ] || { echo "sro.sh needs an interactive terminal." >&2; exit 1; }

    if [ "${1:-}" = "phbot" ] || [ "${1:-}" = "phBot" ]; then
        [ -x "$PHBOT" ] || { echo "phBot not set up ($PHBOT missing). ./swe.sh --phbot" >&2; exit 1; }
        launch_bg "phBot (plain / wine-sro)" "" env WINEDEBUG=-all "$PHBOT"
        echo "phBot started in the background (log: $LAST_LOG)."
        exit 0
    fi

    # Switch to the terminal's ALTERNATE screen buffer for the whole session,
    # the same thing vim/less/htop do. Without this, every redraw here relies
    # on absolute cursor positioning over whatever the terminal already had -
    # if a PRIOR frame was ever taller than the current one (a window resize
    # between screens, a split/tab switch, ...), rows below the new frame's
    # last line are never touched and old content (e.g. a previous menu's
    # "[+ add a new client]" row) can keep showing through underneath the new
    # screen. The alternate buffer is a guaranteed-blank canvas every time, so
    # that class of leftover-content glitch cannot happen regardless of what
    # the terminal emulator did before this session started.
    printf '\033[?1049h'
    ALT_SCREEN_ACTIVE=1
    while true; do
        menu "Control panel" \
            "Start a client / bot" \
            "phBot Manager start" \
            "Manage / stop running clients" \
            "Refresh" \
            "Quit  (leaves running clients running)" || break
        case "$MENU_IDX" in
            0) start_flow ;;
            1) start_manager ;;
            2) manage_flow ;;
            3) : ;;
            4) break ;;
            *) break ;;
        esac
    done
    clear_screen; printf '\033[?25h\033[?7h\033[?1049l'
fi
