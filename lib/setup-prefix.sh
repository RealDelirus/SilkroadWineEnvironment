#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# lib/setup-prefix.sh - sets up one Wine prefix per client type and writes a
# start launcher into the game folder. It is about the client's protection
# ARCHITECTURE, not a specific server (Athens, Hero Journey, etc. are only
# example servers on these architectures):
#
# Functions:
#   setup_maxiguard  <gamedir>       MaxiGuard/VMProtect clients (GE-Proton Wine)
#   setup_vsroplus   <gamedir>       vSroPlus clients            (wine-sro)
#   setup_plain      <gamedir>       generic Silkroad client     (wine-sro, no
#                                    special protection - only wine-sro/phBot fixes)
#   setup_phbot      [installer.exe]  phBot                       (wine-sro)
#
# Also: an interactive universal launcher (sro.sh) - see write_universal_launcher.
#
# Expects: common.sh sourced; for wine-sro targets wine-sro built
# ($WINE_SRO_LOADER present), for MaxiGuard a GE-Proton build installed.
set -euo pipefail

_sp_here() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }

# --- environment that uses the private build (no system Wine, no Proton) ---
_sro_env() {   # prints env assignments; use via `eval "$(_sro_env PREFIX)"`
    local prefix="$1"
    printf 'export WINEPREFIX=%q\n'  "$prefix"
    printf 'export WINELOADER=%q\n'  "$WINE_SRO_LOADER"
    printf 'export WINESERVER=%q\n'  "$WINE_SRO_SERVER"
    printf 'export WINEARCH=win64\n'
    printf 'export WINEDEBUG=%q\n'   "-all"
}

_require_wine_sro() {
    [ -x "$WINE_SRO_LOADER" ] || die "wine-sro missing ($WINE_SRO_LOADER).
   Please build Wine first (swe.sh wine)."
}

_prefix_init() {   # $1 = prefix path   [$2 = optional path to VC_redist.x86.exe]
    local prefix="$1"
    mkdir -p "$prefix"
    eval "$(_sro_env "$prefix")"
    # wine-sro built before Mono was staged (or a --skip-wine run against such
    # a tree) would still hit the install dialog here, so check once more.
    ensure_wine_mono >/dev/null 2>&1 || true
    say "Initializing prefix $prefix (first run takes a moment) ..."
    "$WINE_SRO_LOADER" wineboot -u >/dev/null 2>&1 || \
        "$WINE_SRO_LOADER" wineboot >/dev/null 2>&1 || \
        warn "wineboot reported a problem - check the prefix anyway."
    "$WINE_SRO_SERVER" -w 2>/dev/null || true
    # EVERY prefix gets the VC++ runtime, not only phBot's: phBot's own
    # installer used to put it into its prefix as a side effect, and a client
    # prefix that phBot (or Manager) is later started against needs it too.
    _install_vcredist "$prefix" "${2:-}"
}

# Install the ACPI shim INTO a prefix (system32 + syswow64), so any MaxiGuard
# client run in that prefix finds it - no per-folder WUDFPlatform.dll needed.
# (On real Windows WUDFPlatform.dll lives in system32; the DLL search reaches the
# system dir, so a 32-bit client loads it from syswow64. Verified to pass MaxiGuard.)
_install_maxiguard_shim_prefix() {   # $1 = prefix
    local pfx="$1" PKG; PKG="$(_sp_here)"
    local sw="$pfx/drive_c/windows/syswow64" s32="$pfx/drive_c/windows/system32"
    mkdir -p "$sw" "$s32"
    if command -v i686-w64-mingw32-gcc >/dev/null 2>&1 \
       && i686-w64-mingw32-gcc -O2 -shared -o "$sw/WUDFPlatform.dll" "$PKG/src/wudfshim.c" -luser32 -Wl,--kill-at 2>/dev/null; then
        cp -f "$sw/WUDFPlatform.dll" "$s32/WUDFPlatform.dll"
    else
        cp -f "$PKG/prebuilt/WUDFPlatform.dll" "$sw/WUDFPlatform.dll"
        cp -f "$PKG/prebuilt/WUDFPlatform.dll" "$s32/WUDFPlatform.dll"
    fi
    say "ACPI shim installed into the prefix (system32 + syswow64) - no per-folder DLL needed."
    # phBot.exe imports icuuc.dll; some Proton builds (e.g. proton-cachyos) do NOT
    # ship the icu DLLs, so phBot fails with "icuuc.dll not found". Provide the
    # consistent icu trio from our wine-sro build (always present, built with g++).
    _install_icu_prefix "$pfx"
    # phBot's UI draws arrow glyphs (e.g. down-triangle in combo/spin controls).
    # GE-Proton ships different Tahoma/Marlett files whose coverage makes those
    # glyphs render as tofu (empty boxes). Wine's own builtin UI fonts render them
    # correctly (phBot looks right under wine-sro), so copy that proven set in.
    _install_ui_fonts_prefix "$pfx"
}

# Copy Wine's builtin UI fonts (Tahoma/Marlett/Symbol/Webdings/Wingdings) from the
# wine-sro build into a prefix's Fonts dir, overwriting whatever Proton shipped.
# Wine auto-registers fonts found in C:\windows\Fonts at startup, so dropping the
# files is enough. Fixes phBot arrow glyphs showing as boxes under MaxiGuard.
_install_ui_fonts_prefix() {   # $1 = prefix
    # Two statements: bash expands every word of a `local` before assigning
    # any, so "$pfx" in the same line would still be the CALLER's variable.
    local pfx="$1" src="$WINE_SRO/share/wine/fonts" f
    local fdir="$pfx/drive_c/windows/Fonts"
    [ -d "$src" ] || { warn "wine-sro fonts dir not found - phBot UI glyphs may render as boxes."; return 0; }
    mkdir -p "$fdir"
    for f in tahoma.ttf tahomabd.ttf marlett.ttf symbol.ttf webdings.ttf wingding.ttf; do
        [ -f "$src/$f" ] && cp -f "$src/$f" "$fdir/$f"
    done
    say "Wine UI fonts (Tahoma/Marlett/...) installed into the prefix (fixes phBot arrow glyphs)."
}

# Copy the icu trio (icuuc/icuin/icu, 32-bit) from wine-sro into a prefix's syswow64.
_install_icu_prefix() {   # $1 = prefix
    local pfx="$1" src f
    local sw="$pfx/drive_c/windows/syswow64"   # separate local - see _install_ui_fonts_prefix
    src="$WINE_SRO_TREE/i386-windows"
    [ -f "$src/icuuc.dll" ] || src="$(wine_lib_dir 2>/dev/null)/i386-windows"
    [ -f "$src/icuuc.dll" ] || { warn "no icu DLLs found in wine-sro - phBot may miss icuuc.dll on Proton builds that lack it."; return 0; }
    mkdir -p "$sw"
    for f in icuuc.dll icuin.dll icu.dll; do [ -f "$src/$f" ] && cp -f "$src/$f" "$sw/$f"; done
    say "icu DLLs (icuuc/icuin/icu) installed into the prefix (phBot needs icuuc)."
}

# ---------------------------------------------------------- build/copy ACPI shim
_provide_maxiguard_dlls() {   # $1 = gamedir
    local gamedir="$1" PKG; PKG="$(_sp_here)"
    if command -v i686-w64-mingw32-gcc >/dev/null 2>&1; then
        say "Building WUDFPlatform.dll + winecheck32.exe from source"
        i686-w64-mingw32-gcc -O2 -shared -o "$gamedir/WUDFPlatform.dll" \
            "$PKG/src/wudfshim.c" -luser32 -Wl,--kill-at \
          || { warn "Build failed - using prebuilt/"; cp "$PKG/prebuilt/WUDFPlatform.dll" "$gamedir/"; }
        i686-w64-mingw32-gcc -O2 -o "$gamedir/winecheck32.exe" \
            "$PKG/src/winecheck.c" -ladvapi32 \
          || cp "$PKG/prebuilt/winecheck32.exe" "$gamedir/"
    else
        warn "mingw-w64 not found - using bundled prebuilt/ binaries"
        cp "$PKG/prebuilt/WUDFPlatform.dll" "$gamedir/WUDFPlatform.dll"
        cp "$PKG/prebuilt/winecheck32.exe"  "$gamedir/winecheck32.exe"
    fi
}

# ---------------------------------------------------------- VC++ runtime (phBot)
# phBot is 32-bit and requires a recent Visual C++ 2015-2026 runtime ("a newer
# version of vcredist is missing"). We install the official VC_redist.x86.exe
# silently into the prefix. phBot checks the official runtimes registry key; we
# use it as a reliable success signal (the bundle exit code is worthless via Wine).
#
# Which redist: phBot's own installer always fetched Microsoft's CURRENT one
# (aka.ms/vc14/vc_redist.x86.exe) and phBot builds track it - the copy bundled
# in prebuilt/ is older (14.44 vs. the 14.51 phBot's installer put in), which
# is exactly the "newer vcredist required" case. Now that phBot's installer is
# no longer run, we fetch that same current redist ourselves (cached, only
# re-downloaded when Microsoft's copy changes). Offline, the next best is the
# one phBot's own package ships (VC_redist.x86.exe inside the full zip, kept
# as VC_redist.x86.phbot.exe once phBot was downloaded), then prebuilt/.
#
# The marker holds the sha256 of the redist that was applied, the same scheme
# sro.sh's mg_ensure_vcredist/wsro_ensure_vcredist use at launch time - so a
# newer redist is rolled into every prefix once, and a launch never redoes
# what setup already did.
_VCREDIST_REFRESHED=0
_refresh_vcredist() {   # download/refresh the current redist into the cache (once per run)
    [ "$_VCREDIST_REFRESHED" = 1 ] && return 0
    _VCREDIST_REFRESHED=1
    local dst="$BUILD_CACHE/vcredist/VC_redist.x86.exe"
    mkdir -p "$(dirname "$dst")"
    local z=(); [ -s "$dst" ] && z=(-z "$dst")
    rm -f "$dst.part"
    # -z: a 304 when the cached copy is still current (nothing is written);
    # -R: keep the server's timestamp so that comparison stays meaningful.
    if curl -fsSL -R --retry 2 --connect-timeout 15 --max-time 300 "${z[@]}" \
            -o "$dst.part" "$SRO_VCREDIST_URL" 2>/dev/null \
       && [ -s "$dst.part" ] && [ "$(head -c2 "$dst.part")" = MZ ]; then
        mv -f "$dst.part" "$dst"
        say "Current VC++ runtime downloaded from Microsoft."
    fi
    rm -f "$dst.part"
    return 0
}
vcredist_exe() {   # [$1 = explicit path] -> the VC_redist.x86.exe to use (or nothing)
    local PKG c; PKG="$(_sp_here)"
    for c in "${1:-}" "$BUILD_CACHE/vcredist/VC_redist.x86.exe" \
             "$BUILD_CACHE/vcredist/VC_redist.x86.phbot.exe" \
             "$PKG/prebuilt/VC_redist.x86.exe" "$HOME/Downloads/VC_redist.x86.exe"; do
        [ -n "$c" ] && [ -s "$c" ] && { printf '%s\n' "$c"; return 0; }
    done
    return 1
}

_VCRT_KEY_WOW='HKLM\SOFTWARE\Wow6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86'
_VCRT_KEY_STD='HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86'
_vcredist_installed() {   # uses WINEPREFIX/WINELOADER from the calling environment
    "$WINE_SRO_LOADER" reg query "$_VCRT_KEY_WOW" /v Installed 2>/dev/null | grep -q '0x1' && return 0
    "$WINE_SRO_LOADER" reg query "$_VCRT_KEY_STD" /v Installed 2>/dev/null | grep -q '0x1'
}
_vcredist_version() {
    "$WINE_SRO_LOADER" reg query "$_VCRT_KEY_WOW" /v Version 2>/dev/null \
        | awk '/Version/{print $NF}' | tail -1 | tr -d '\r'
}

_install_vcredist() {   # $1 = prefix   [$2 = optional path to VC_redist.x86.exe]
    # Uses WINEPREFIX/WINELOADER from the caller (see _prefix_init).
    local prefix="$1" override="${2:-}"
    local marker="$prefix/.vcredist-x86-installed"
    [ -n "$override" ] || _refresh_vcredist
    local redist; redist="$(vcredist_exe "$override")" || redist=""
    if [ -z "$redist" ]; then
        warn "No VC_redist.x86.exe available (download failed, none in prebuilt/) - VC++ runtime
   not installed. phBot may report 'newer vcredist required'. Re-run the phBot
   setup (--phbot) once online."
        return 0
    fi

    # This exact redist already applied -> skip, idempotent.
    local hash; hash="$(sha256sum "$redist" 2>/dev/null | cut -d' ' -f1)"
    if [ -n "$hash" ] && [ "$(cat "$marker" 2>/dev/null)" = "$hash" ] && _vcredist_installed; then
        say "VC++ runtime (x86) already installed ($(_vcredist_version)) - skipped."
        return 0
    fi

    say "Installing Visual C++ runtime (x86) silently - this takes a moment ..."
    # /install /quiet /norestart: unattended. The bundle chainer returns no
    # reliable exit code via Wine (0x666=already-installed is passed through as
    # e.g. 102). So do NOT evaluate the code, check the registry key phBot itself
    # queries.
    "$WINE_SRO_LOADER" "$redist" /install /quiet /norestart >/dev/null 2>&1 || true
    "$WINE_SRO_SERVER" -w 2>/dev/null || true

    # Success = VC++ runtimes key present (x86; under WoW64 in Wow6432Node).
    if _vcredist_installed; then
        printf '%s' "$hash" > "$marker"
        say "VC++ runtime set up ($(_vcredist_version))."
    else
        warn "VC++ runtime could not be confirmed (no runtimes registry key).
   If phBot still reports 'newer vcredist required': '$SRO_HOME/phbot.sh kill',
   then re-run the phBot setup: ./swe.sh --skip-deps --skip-wine --phbot"
    fi
}

# ---------------------------------------------------------- universal launcher
# Stash the shim assets (WUDFPlatform.dll + winecheck32.exe) into $SRO_HOME/assets
# so sro.sh can copy them into any MaxiGuard folder on demand.
_stash_launcher_assets() {
    local PKG; PKG="$(_sp_here)"
    local dst="$SRO_HOME/assets"; mkdir -p "$dst"
    if command -v i686-w64-mingw32-gcc >/dev/null 2>&1; then
        i686-w64-mingw32-gcc -O2 -shared -o "$dst/WUDFPlatform.dll" "$PKG/src/wudfshim.c" -luser32 -Wl,--kill-at 2>/dev/null \
            || cp -f "$PKG/prebuilt/WUDFPlatform.dll" "$dst/" 2>/dev/null || true
        i686-w64-mingw32-gcc -O2 -o "$dst/winecheck32.exe" "$PKG/src/winecheck.c" -ladvapi32 2>/dev/null \
            || cp -f "$PKG/prebuilt/winecheck32.exe" "$dst/" 2>/dev/null || true
    else
        cp -f "$PKG/prebuilt/WUDFPlatform.dll" "$dst/" 2>/dev/null || true
        cp -f "$PKG/prebuilt/winecheck32.exe"  "$dst/" 2>/dev/null || true
    fi
    # Also stash the VC++ redist installer, so sro.sh can (re)install it at
    # launch time into any MaxiGuard/Manager prefix that ended up without it -
    # see mg_ensure_vcredist()/wsro_ensure_vcredist() in sro-launcher.sh.
    # ALWAYS refresh (not just if missing): when a newer redist is available
    # (Microsoft's current one fetched by _refresh_vcredist, or a newer
    # prebuilt/VC_redist.x86.exe), the hash-marker check in those
    # ensure_vcredist functions only ever notices a change if this copy is
    # actually kept in sync with the one setup installs.
    local redist; redist="$(vcredist_exe)" && cp -f "$redist" "$dst/VC_redist.x86.exe" 2>/dev/null || true
}

# Writes the interactive universal launcher sro.sh:
#   phBot | Silkroad -> (fix mode maxiguard/vsroplus/plain) -> folder browser
#   -> Launcher (Silkroad.exe) or Client (<exe> 0 /22 0 0) -> start.
write_universal_launcher() {
    _stash_launcher_assets
    local PKG; PKG="$(_sp_here)"
    mkdir -p "$SRO_HOME"
    # Install the interactive launcher (a static script that derives all paths
    # from its own location). Copying keeps the installed sro.sh self-contained.
    # Written to a temp file and renamed over the old one: bash reads a
    # running script incrementally, so overwriting sro.sh in place (cp -f
    # truncates the same inode) would corrupt a control panel that is open in
    # a terminal right now - e.g. when the GUI refreshes it at startup.
    cp -f "$PKG/lib/sro-launcher.sh" "$SRO_HOME/sro.sh.new"
    chmod +x "$SRO_HOME/sro.sh.new"
    mv -f "$SRO_HOME/sro.sh.new" "$SRO_HOME/sro.sh"
}

# ---------------------------------------------------------- write launcher
# Writes a self-contained launcher that hard-wires wine-sro + prefix.
_write_launcher() {   # $1=outfile  $2=prefix  $3=gamedir(""=phbot)  $4=default-exe  $5=default-args  $6=extra-env
    local out="$1" prefix="$2" gamedir="$3" exe="$4" args="$5" extra="$6"
    cat > "$out" <<EOF
#!/usr/bin/env bash
# Auto-generated by the SilkroadWineEnvironment installer. Starts the client with the private
# patched Wine (wine-sro) and a dedicated prefix. No Proton.
set -euo pipefail
export WINEPREFIX=${prefix@Q}
export WINELOADER=${WINE_SRO_LOADER@Q}
export WINESERVER=${WINE_SRO_SERVER@Q}
export WINEARCH=win64
${extra}
GAMEDIR=${gamedir@Q}
WINE=${WINE_SRO_LOADER@Q}

usage() { grep '^  [a-z]' "\$0" | sed 's/^  //'; }

case "\${1:-run}" in
  run)      EXE=${exe@Q}; shift || true; set -- ${args} ;;   # start default
  debug)    export WINEDEBUG="+seh,+loaddll,err+all"; EXE=${exe@Q}; shift || true; set -- ${args} ;;
  check)    if [ -f "\$GAMEDIR/winecheck32.exe" ]; then
                cd "\$GAMEDIR"; "\$WINE" winecheck32.exe >/dev/null 2>&1 || true
                cat "\$WINEPREFIX/drive_c/winecheck.txt" 2>/dev/null || echo "(no winecheck result)"
            else echo "winecheck only available for MaxiGuard clients"; fi; exit 0 ;;
  cfg)      exec "\$WINE" winecfg ;;                          # winecfg in the prefix
  kill)     exec "${WINE_SRO_SERVER@Q}" -k ;;        # stop prefix/server
  *)        EXE="\$1"; shift ;;                               # <exe> run any EXE
esac

[ -n "\$GAMEDIR" ] && cd "\$GAMEDIR"
exec "\$WINE" "\$EXE" "\$@"
EOF
    chmod +x "$out"
}

# ============================================================ MaxiGuard
#
# SPECIAL CASE MaxiGuard: the from-source wine-sro (whether 11.15 vanilla, 11.15
# or 11.0 wine-staging - all tested) does NOT pass MaxiGuard's VM/Wine check
# (client error 41728, no green "you can login now"). Only GE-Proton's Wine build
# (11.0 Staging + GE's curated extra patch set) gets through - and it does so even
# WITHOUT umu-run/Steam runtime when its Wine binaries are run DIRECTLY. The GE
# patches cannot be practically ported onto vanilla Wine; therefore MaxiGuard
# clients (and only those) use GE-Proton's Wine directly. phBot + vSroPlus keep
# running on the from-source wine-sro.
#
# Requirement: a GE-Proton build is installed (e.g. via ProtonUp-Qt). The build
# MUST contain the staging patch HideWineExports (GE-Proton, proton-cachyos;
# NOT Proton-Experimental/vanilla Wine).
#
# Pinned to ONE exact build (SRO_GE_PROTON_VERSION, default GE-Proton11-7).
# Some hosts already have a different compat tool installed (e.g.
# proton-cachyos-nativ), which MaxiGuard does not run reliably on (crash right
# after login). Rather than picking up whatever is found first, only that
# exact version is ever searched for, downloaded and used - other Proton/
# proton-cachyos builds present in compatibilitytools.d are ignored.
SRO_GE_PROTON_VERSION="${SRO_GE_PROTON_VERSION:-GE-Proton11-7}"

# Recent GE-Proton tarballs (multi-arch: x86_64 + aarch64) extract into a
# directory named "<tag>-x86_64", not "<tag>" as older releases did - so a
# manually-installed build may be named either way depending on how/when it
# was extracted. Print both candidate on-disk base names for
# SRO_GE_PROTON_VERSION, exact name first.
_mg_ge_basenames() {
    printf '%s\n' "$SRO_GE_PROTON_VERSION" "$SRO_GE_PROTON_VERSION-x86_64"
}

_mg_compat_dirs() {
    printf '%s\n' \
        "$HOME/.steam/root/compatibilitytools.d" \
        "$HOME/.local/share/Steam/compatibilitytools.d" \
        "/usr/share/steam/compatibilitytools.d"
}

# Does the build have the HideWineExports staging patch? (base detection)
_mg_has_hidewine() {   # $1 = build root (contains files/)
    local n="$1/files/lib/wine/i386-windows/ntdll.dll"
    [ -f "$n" ] && grep -qa "HideWineExports" "$n"
}
# Is the export rename already applied? Checks the oldest AND the newest
# renamed name, so a *-SRO build patched by an older version of this repo
# (before wine_server_call/__wine_dbg_output were added to RENAMES) is
# correctly treated as stale and gets re-patched instead of reused as-is.
_mg_exports_hidden() {   # $1 = build root
    local n="$1/files/lib/wine/i386-windows/ntdll.dll"
    [ -f "$n" ] || return 1
    ! grep -qa "wine_get_version" "$n" && ! grep -qa "wine_server_call" "$n"
}

# Find an existing patched *-SRO build (export rename already applied). Only
# the pinned SRO_GE_PROTON_VERSION is accepted - any other -SRO build present
# (e.g. from an older proton-cachyos-nativ install) is ignored.
_mg_find_patched_ge() {
    local d b p
    while IFS= read -r d; do
        while IFS= read -r b; do
            p="$d/$b-SRO"
            [ -d "$p" ] && _mg_exports_hidden "$p" && { echo "$p"; return 0; }
        done < <(_mg_ge_basenames)
    done < <(_mg_compat_dirs)
    return 1
}
# Find the pinned base GE-Proton build (with HideWineExports, without -SRO)
# for the reflink copy. Only SRO_GE_PROTON_VERSION is looked for - other
# Proton/proton-cachyos builds already installed are deliberately ignored so a
# host with e.g. proton-cachyos-nativ present doesn't pick that one up.
_mg_find_base_ge() {
    local d b p
    while IFS= read -r d; do
        [ -d "$d" ] || continue
        while IFS= read -r b; do
            p="$d/$b"
            [ -d "$p" ] && _mg_has_hidewine "$p" && { echo "$p"; return 0; }
        done < <(_mg_ge_basenames)
    done < <(_mg_compat_dirs)
    return 1
}
_mg_writable_compat_dir() {
    local d
    while IFS= read -r d; do
        mkdir -p "$d" 2>/dev/null && [ -w "$d" ] && { echo "$d"; return 0; }
    done < <(_mg_compat_dirs)
    return 1
}

# Download the pinned GE-Proton build (SRO_GE_PROTON_VERSION, ships the
# HideWineExports staging patch) into a writable compatibilitytools.d, so
# MaxiGuard works on any distro without the user installing Proton by hand.
# Deliberately fetches that exact tagged release (not "latest") so the result
# is always the same build regardless of what else is already installed.
# Prints the extracted build dir on success.
_mg_download_ge() {
    local cdir; cdir="$(_mg_writable_compat_dir)" || return 1
    local tag="$SRO_GE_PROTON_VERSION" b
    # already downloaded before? (either on-disk naming)
    while IFS= read -r b; do
        [ -d "$cdir/$b" ] && { echo "$cdir/$b"; return 0; }
    done < <(_mg_ge_basenames)
    local api="https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/tags/$tag"
    local url tmp tar sum
    url="$(curl -fsSL --connect-timeout 20 "$api" 2>/dev/null \
        | grep -oE 'https://[^"]*'"$tag"'-x86_64\.tar\.gz' | head -1)"
    [ -n "$url" ] || { warn "Could not determine the download URL for $tag (network, or the release tag doesn't exist)."; return 1; }
    tmp="$(mktemp -d)"; tar="$tmp/$tag.tar.gz"
    say "Downloading $tag (~450 MB, one time) ..." >&2
    curl -# -fL --retry 3 --connect-timeout 20 -o "$tar" "$url" \
        || { warn "GE-Proton download failed."; rm -rf "$tmp"; return 1; }
    sum="$(curl -fsSL "${url%.tar.gz}.sha512sum" 2>/dev/null | awk 'NR==1{print $1}')"
    if [ -n "$sum" ] && command -v sha512sum >/dev/null 2>&1; then
        echo "$sum  $tar" | sha512sum -c - >/dev/null 2>&1 \
            || { warn "GE-Proton checksum mismatch - not installing."; rm -rf "$tmp"; return 1; }
        say "Checksum OK." >&2
    fi
    say "Extracting into $cdir ..." >&2
    tar -xzf "$tar" -C "$cdir" || { warn "GE-Proton extract failed."; rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    # The tarball's top-level directory is named "<tag>-x86_64" (multi-arch
    # releases), so check that name first, then the bare tag as a fallback for
    # older/differently-packaged releases.
    while IFS= read -r b; do
        [ -d "$cdir/$b" ] && { echo "$cdir/$b"; return 0; }
    done < <(_mg_ge_basenames)
    warn "Extraction reported success but $cdir/$tag(-x86_64) was not found afterwards."
    return 1
}

# Bake the phBot Win10/11 user32/kernel32 exports into a Proton build's builtin
# DLLs (in place, builtin marker kept). Idempotent via a pristine .phbot-orig
# backup. Needed because some Proton builds (e.g. proton-cachyos) don't ship the
# pointer-frame APIs phBot's bundled Python imports (e.g. GetPointerPenInfoHistory).
_mg_bake_user32() {   # $1 = build dir (contains files/)
    local ge="$1" PKG; PKG="$(_sp_here)"
    local dir="$ge/files/lib/wine/i386-windows" d orig
    [ -d "$dir" ] || return 0
    for d in user32.dll kernel32.dll; do
        [ -f "$dir/$d" ] || continue
        orig="$dir/$d.phbot-orig"
        [ -f "$orig" ] || cp -a "$dir/$d" "$orig"
        chmod u+w "$dir/$d" 2>/dev/null || true
        python3 "$PKG/patches/patch_user32.py" --keep-builtin "$orig" "$dir/$d" \
            || warn "phBot export bake into $d failed (phBot may miss Win10/11 exports)."
    done
    say "phBot Win10/11 exports baked into the GE build (user32 + kernel32)."
}

# Ensures a patched GE build exists; sets GE_MG_DIR. Reuses an existing *-SRO
# with hidden exports if present; otherwise reflink-copies a base GE build and
# applies export rename (Fix 1) + dxdiagn (Fix 3). Always bakes the phBot exports.
_mg_ensure_ge() {
    local PKG; PKG="$(_sp_here)"
    local existing base dest
    if existing="$(_mg_find_patched_ge)"; then
        say "Found patched GE-Proton build: $(basename "$existing")"
        GE_MG_DIR="$existing"; _mg_bake_user32 "$GE_MG_DIR"; return 0
    fi
    base="$(_mg_find_base_ge)" || base="$(_mg_download_ge)" \
        || die "$SRO_GE_PROTON_VERSION not found and auto-download failed.
   MaxiGuard is pinned to exactly this build (other Proton/proton-cachyos
   builds already installed on this system - e.g. proton-cachyos-nativ - are
   deliberately ignored, since MaxiGuard crashes after login on some of them).
   Install $SRO_GE_PROTON_VERSION into ~/.steam/root/compatibilitytools.d (e.g.
   via ProtonUp-Qt) and re-run, or check your network for the auto-download."
    local cdir; cdir="$(_mg_writable_compat_dir)" \
        || die "No writable compatibilitytools.d found for the GE copy."
    dest="$cdir/$(basename "$base")-SRO"
    say "Creating patched GE build: $(basename "$dest")  (from $(basename "$base"))"
    rm -rf "$dest"
    cp -a --reflink=auto "$base" "$dest"
    say "Applying Fix 1 (export rename) + Fix 3 (dxdiagn) to the GE build ..."
    python3 "$PKG/patches/patch-exports.py" "$dest" \
        || die "Export rename on the GE build failed."
    python3 "$PKG/patches/patch-dxdiagn.py" "$dest" \
        || warn "dxdiagn patch on the GE build reported a problem (login crash possible)."
    GE_MG_DIR="$dest"; _mg_bake_user32 "$GE_MG_DIR"
}

# Write the MaxiGuard launcher: starts the client via umu-run (GE-Proton + Steam
# Linux Runtime) with the MaxiGuard prefix. This is the ONLY combo that both
# passes MaxiGuard's VM/Wine check AND survives world entry - running GE-Proton's
# Wine directly passes the check but crashes when the client resets the device on
# entering the world (the full Proton runtime is what makes it stable).
_write_mg_launcher() {   # $1=out $2=prefix $3=gamedir $4=ge-dir $5=client-exe
    local out="$1" prefix="$2" gamedir="$3" ge="$4" cexe="${5:-Macro_Client.exe}"
    local umu; umu="$(umu_run_path || echo umu-run)"
    cat > "$out" <<EOF
#!/usr/bin/env bash
# Auto-generated by the SilkroadWineEnvironment installer. Starts a MaxiGuard client via umu-run
# (GE-Proton + Steam Linux Runtime) with the MaxiGuard prefix (WUDF ACPI shim +
# HideWineExports). This passes MaxiGuard's VM/Wine check AND survives world entry.
# (Example server on this architecture: Athens 80 Cap and others.)
set -euo pipefail
export WINEPREFIX=${prefix@Q}
export GAMEID="umu-sro"
export STORE="none"
export PROTONPATH=${ge@Q}
export PROTON_DISABLE_NVAPI=1
export WINE_HIDE_WINE_EXPORTS=1
# Hosts without a working Vulkan driver (VMs without GPU passthrough) need
# WineD3D instead of DXVK - opt-in via 'Toggle: force WineD3D' in swe.sh.
[ -f ${WINED3D_FLAG@Q} ] && export PROTON_USE_WINED3D=1
GAMEDIR=${gamedir@Q}
UMU=${umu@Q}
[ -x "\$UMU" ] || command -v "\$UMU" >/dev/null || { echo "error: umu-run not found (package: umu-launcher)." >&2; exit 1; }

case "\${1:-run}" in
  run)      EXE=${cexe@Q}; shift || true; set -- 0 /22 0 0 ;;   # client
  debug)    export WINEDEBUG="+seh,err+all"; EXE=${cexe@Q}; shift || true; set -- 0 /22 0 0 ;;
  launcher) EXE="Silkroad.exe";       shift || true ;;                   # official patcher
  autologin)EXE="MacroAutoLogin.exe"; shift || true ;;
  check)    if [ -f "\$GAMEDIR/winecheck32.exe" ]; then
                cd "\$GAMEDIR"; "\$UMU" winecheck32.exe >/dev/null 2>&1 || true
                cat "\$WINEPREFIX/drive_c/winecheck.txt" 2>/dev/null || echo "(no winecheck result)"
            else echo "winecheck not available"; fi; exit 0 ;;
  cfg)      exec "\$UMU" winecfg ;;
  *)        EXE="\$1"; shift ;;
esac

[ -n "\$GAMEDIR" ] && cd "\$GAMEDIR"
# HKLM\Software\Wine is recreated by wineboot at the start of every fresh
# wineserver session, so it must be deleted IN THE SAME process tree as the
# client, right before exec'ing it - a separate prior umu-run call would not
# stick. cmd.exe's "&" chains both in one wine session.
MGCMD="\$EXE"; for a in "\$@"; do MGCMD="\$MGCMD \$a"; done
exec "\$UMU" cmd /c "reg delete HKLM\\\\Software\\\\Wine /f & \$MGCMD"
EOF
    chmod +x "$out"
}

# Launcher that starts phBot under GE-Proton's Wine (direct) with the MaxiGuard
# sroprivate prefix, so the client STARTED BY phBot also passes MaxiGuard.
_write_phbot_mg_launcher() {   # $1=out $2=sroprivate-prefix $3=ge-dir $4=phbot-prefix-root
    local out="$1" prefix="$2" ge="$3" phbotpfx="$4"
    local umu; umu="$(umu_run_path || echo umu-run)"
    cat > "$out" <<EOF
#!/usr/bin/env bash
# Auto-generated by the SilkroadWineEnvironment installer. Starts phBot via umu-run (GE-Proton +
# Steam Linux Runtime) with the MaxiGuard sroprivate prefix - so the client
# started by phBot inherits that runtime and passes MaxiGuard's VM/Wine check
# (green "you can login now") AND survives world entry. In phBot, point the client
# path at the MaxiGuard folder.
#
# Note: MaxiGuard is also an anti-cheat and can detect phBot at WORLD ENTRY via a
# module scan and terminate the client (bot-vs-anticheat, NOT a Wine/Linux
# problem). The standalone client (maxiguard.sh) runs fully; for botting phBot is
# meant to be used clientless.
set -euo pipefail
export WINEPREFIX=${prefix@Q}
export GAMEID="umu-sro"
export STORE="none"
export PROTONPATH=${ge@Q}
export PROTON_DISABLE_NVAPI=1
export WINE_HIDE_WINE_EXPORTS=1
# Force native from the prefix (=n,b: native if present, else builtin):
#  - VC++ redist (14.51) over Wine's/GE's older builtin CRT ("newer vcredist" msg)
#  - the icu trio, so phBot's icuuc import resolves even on Proton builds that
#    don't ship icu (e.g. proton-cachyos) - the DLLs are placed in the prefix.
export WINEDLLOVERRIDES="vcruntime140,msvcp140,msvcp140_1,msvcp140_2,msvcp140_atomic_wait,concrt140=n,b;icuuc,icuin,icu=n,b"
# Hosts without a working Vulkan driver (VMs without GPU passthrough) need
# WineD3D instead of DXVK - opt-in via 'Toggle: force WineD3D' in swe.sh.
[ -f ${WINED3D_FLAG@Q} ] && export PROTON_USE_WINED3D=1
UMU=${umu@Q}
[ -x "\$UMU" ] || command -v "\$UMU" >/dev/null || { echo "error: umu-run not found (package: umu-launcher)." >&2; exit 1; }

# Find phBot.exe (argument, otherwise the most recently installed one in the phBot prefix).
PHB="\${1:-}"
if [ -z "\$PHB" ]; then
    PHB="\$(find ${phbotpfx@Q}/drive_c -iname 'phBot.exe' -type f 2>/dev/null \\
        | grep -viE '/(Temp|tmp|windows/Installer|Downloads)/' \\
        | while IFS= read -r f; do printf '%s\t%s\n' "\$(stat -c %Y "\$f" 2>/dev/null || echo 0)" "\$f"; done \\
        | sort -rn | head -1 | cut -f2- || true)"
fi
[ -n "\$PHB" ] && [ -f "\$PHB" ] || { echo "phBot.exe not found - set up phBot first: ./swe.sh --skip-deps --skip-wine --phbot" >&2; exit 1; }
cd "\$(dirname "\$PHB")"
exec "\$UMU" "\$PHB"
EOF
    chmod +x "$out"
}

# Determine a MaxiGuard folder's client EXE (server-independent).
_mg_client_exe() {   # $1 = gamedir ; prints EXE name (empty if none)
    local d="$1" e m
    for e in Macro_Client.exe sro_client.exe Silkroad.exe; do
        m="$(_ci_file "$d" "$e")" && { echo "$m"; return 0; }
    done
    echo ""
}

# Resolve (or create) the shared, Proton-created MaxiGuard prefix -> sets MG_PREFIX.
# The whole MaxiGuard environment (GE build + this prefix + the ACPI shim inside
# it) is system-wide; individual servers need NO per-folder patching. IMPORTANT:
# MaxiGuard only accepts a Proton-created prefix - a self "wineboot" prefix fails
# with 41728. So: reuse an existing sroprivate (with HideWineExports), else create
# the shared one ONCE via umu-run.
# Create the shared MaxiGuard prefix FRESH via umu-run (Proton-created; MaxiGuard
# rejects a self-wineboot prefix with 41728). Sets MG_PREFIX (relies on bash
# dynamic scope - the caller declares it local).
_mg_create_shared_prefix() {   # $1 = GE build dir
    local ge="$1"
    local umu; umu="$(umu_run_path || echo umu-run)"
    [ -n "$umu" ] || die "No MaxiGuard (Proton) prefix found and umu-run is missing.
   MaxiGuard only works with a Proton-created prefix. Install umu-launcher once,
   then re-run './swe.sh --maxiguard' - the shared prefix is created automatically."
    MG_PREFIX="$PREFIX_ROOT/maxiguard-sroprivate"; mkdir -p "$MG_PREFIX"
    say "Creating the shared MaxiGuard prefix via umu-run: $MG_PREFIX"
    WINEPREFIX="$MG_PREFIX" GAMEID="umu-maxiguard" STORE="none" PROTONPATH="$ge" \
        "$umu" wineboot -u >/dev/null 2>&1 || warn "umu-run wineboot reported a problem."
    # The VC++ runtime follows in _mg_finalize_prefix (for new AND reused prefixes).
}

# VC++ runtime for a MaxiGuard (Proton-created) prefix - same hash marker as
# _install_vcredist, installed through umu-run like everything in this prefix.
_mg_install_vcredist() {   # $1 = prefix ; $2 = GE build dir
    local pfx="$1" ge="$2" umu; umu="$(umu_run_path || echo umu-run)"
    _refresh_vcredist
    local redist; redist="$(vcredist_exe)" || { warn "No VC_redist.x86.exe available - VC++ runtime not installed into the MaxiGuard prefix."; return 0; }
    local hash marker="$pfx/.vcredist-x86-installed"
    hash="$(sha256sum "$redist" 2>/dev/null | cut -d' ' -f1)"
    [ -n "$hash" ] && [ "$(cat "$marker" 2>/dev/null)" = "$hash" ] && { say "VC++ runtime already in the MaxiGuard prefix - skipped."; return 0; }
    say "Installing VC++ runtime into the MaxiGuard prefix ..."
    WINEPREFIX="$pfx" GAMEID="umu-maxiguard" STORE="none" PROTONPATH="$ge" \
        "$umu" "$redist" /install /quiet /norestart >/dev/null 2>&1 || true
    if WINEPREFIX="$pfx" "$ge/files/bin/wine" reg query "$_VCRT_KEY_WOW" /v Installed 2>/dev/null | grep -q '0x1' \
       || WINEPREFIX="$pfx" "$ge/files/bin/wine" reg query "$_VCRT_KEY_STD" /v Installed 2>/dev/null | grep -q '0x1'; then
        printf '%s' "$hash" > "$marker"
        say "VC++ runtime set up in the MaxiGuard prefix."
    else
        warn "VC++ runtime in the MaxiGuard prefix could not be confirmed - sro.sh retries it at the next phBot start."
    fi
}

# Finalize any MaxiGuard prefix: HideWineExports=Y + ACPI shim + icu + UI fonts
# + VC++ runtime.
_mg_finalize_prefix() {   # $1 = prefix ; $2 = GE build dir
    local pfx="$1" ge="$2" umu; umu="$(umu_run_path || echo umu-run)"
    if ! grep -aq '"HideWineExports"="Y"' "$pfx/user.reg" 2>/dev/null; then
        WINEPREFIX="$pfx" GAMEID="umu-maxiguard" STORE="none" PROTONPATH="$ge" \
            "$umu" reg add 'HKCU\Software\Wine' /v HideWineExports /t REG_SZ /d Y /f >/dev/null 2>&1 || true
    fi
    _install_maxiguard_shim_prefix "$pfx"   # ACPI shim + icu + fonts, system-wide
    _mg_install_vcredist "$pfx" "$ge"
}

# Pure query (no side effects, never creates anything): finds any ALREADY
# valid MaxiGuard prefix - shared, per-folder, or anywhere else the search
# below would accept - or prints nothing. Shared between _mg_ensure_prefix
# (setup, below) and swe.sh's st_mg_prefix (status) so "is MaxiGuard set
# up" means the same thing on both sides - it did NOT before: setup happily
# reuses a prefix it finds under a game folder (by design, to avoid a
# needless duplicate), but the status check only ever looked at the shared
# location, so a real install using a reused per-folder prefix showed
# "missing" in the status panel/GUI checkbox despite being fully set up.
_mg_find_any_prefix() {   # [$1 = optional gamedir] -> prefix path, or nothing
    local gamedir="${1:-}" shared="$PREFIX_ROOT/maxiguard-sroprivate" c
    for c in "$shared" ${gamedir:+"$gamedir/sroprivate"} "$HOME/Games/athens-linux-fix/sroprivate"; do
        [ -f "$c/system.reg" ] && grep -aq '"HideWineExports"="Y"' "$c/user.reg" 2>/dev/null && { echo "$c"; return 0; }
    done
    find "$HOME/Games" "$HOME/Downloads" -maxdepth 4 -type d -name sroprivate 2>/dev/null \
        | grep -viE '/([Bb]ackup|\.bak)/' \
        | while IFS= read -r p; do [ -f "$p/system.reg" ] && grep -aq '"HideWineExports"="Y"' "$p/user.reg" 2>/dev/null && { echo "$p"; break; }; done
}

_mg_ensure_prefix() {   # $1 = GE build dir ; [$2 = optional gamedir]
    local ge="$1" gamedir="${2:-}"
    # "no prefix found anywhere" is a valid, expected outcome on a fresh
    # install, not an error - but _mg_find_any_prefix's internal `grep -v`
    # exits 1 when it selects zero lines (no sroprivate dirs at all), and
    # under pipefail that makes the whole pipeline (and thus the function)
    # return 1. Without the `|| true`, this bare assignment would trip
    # `set -e` and silently kill the whole installer right here on any
    # machine with no pre-existing MaxiGuard prefix - confirmed via a real
    # fresh-install run that died right after "exports baked" with no error.
    MG_PREFIX="$(_mg_find_any_prefix "$gamedir")" || true

    if [ -n "$MG_PREFIX" ]; then
        say "Using existing MaxiGuard prefix: $MG_PREFIX"
    else
        _mg_create_shared_prefix "$ge"
    fi
    _mg_finalize_prefix "$MG_PREFIX" "$ge"
}

# Public: delete the shared MaxiGuard prefix and recreate it FRESH. Fixes a stale
# prefix that passes the VM check but crashes on world entry (as happened with an
# old Backup/experiment prefix). Ensures umu + a patched GE build first.
recreate_maxiguard_prefix() {
    ensure_umu || die "MaxiGuard needs umu-launcher and it could not be installed."
    local GE_MG_DIR; _mg_ensure_ge
    local shared="$PREFIX_ROOT/maxiguard-sroprivate" MG_PREFIX
    if [ -d "$shared" ]; then
        say "Removing the old shared MaxiGuard prefix: $shared"
        rm -rf "$shared"
    fi
    _mg_create_shared_prefix "$GE_MG_DIR"
    _mg_finalize_prefix "$MG_PREFIX" "$GE_MG_DIR"
    say "Shared MaxiGuard prefix recreated fresh: $MG_PREFIX"
    _write_phbot_mg_launcher "$SRO_HOME/phbot-maxiguard.sh" "$MG_PREFIX" "$GE_MG_DIR" "$PREFIX_ROOT/phbot"
}

# Public: undo the MaxiGuard opt-in - removes only what THIS installer itself
# created for it (the shared prefix, the phBot-under-MaxiGuard launcher, and
# the patched "*-SRO" GE-Proton copy it made). Deliberately leaves the user's
# own, non-SRO GE-Proton build in compatibilitytools.d untouched - that is
# their asset, not ours. Once this runs, find_ge/_maxiguard_available() go
# back to "not available", so MaxiGuard mode disappears from the launcher
# again exactly like it never had been set up.
remove_maxiguard() {
    local removed=0
    local shared="$PREFIX_ROOT/maxiguard-sroprivate"
    if [ -d "$shared" ]; then
        say "Removing the shared MaxiGuard prefix: $shared"
        rm -rf "$shared"; removed=1
    fi
    if [ -f "$SRO_HOME/phbot-maxiguard.sh" ]; then
        rm -f "$SRO_HOME/phbot-maxiguard.sh"; removed=1
    fi
    local ge; ge="$(_mg_find_patched_ge 2>/dev/null)"
    if [ -n "$ge" ] && [ -d "$ge" ]; then
        say "Removing the patched GE-Proton copy: $ge"
        rm -rf "$ge"; removed=1
    fi
    if [ "$removed" = 1 ]; then
        say "MaxiGuard support removed. Your saved MaxiGuard clients stay listed, but won't start until MaxiGuard support is set up again."
    else
        say "Nothing to remove - MaxiGuard support was not set up."
    fi
}

# setup_maxiguard [gamedir]
#   with a folder: also set up that specific client (per-folder launcher).
#   WITHOUT a folder: just prepare the MaxiGuard environment system-wide, so any
#   server can be launched later via sro.sh with no per-folder patching.
setup_maxiguard() {
    local gamedir="" cexe=""
    if [ -n "${1:-}" ]; then
        gamedir="$(cd "$1" && pwd)"
        cexe="$(_mg_client_exe "$gamedir")"
        [ -f "$gamedir/MaxiGuard.dll" ] || [ -n "$cexe" ] \
            || die "No MaxiGuard.dll / client EXE in '$gamedir' (wrong folder?)."
        [ -n "$cexe" ] || cexe="Macro_Client.exe"
        step "Setting up MaxiGuard client: $gamedir  (EXE: $cexe)"
    else
        step "Setting up the MaxiGuard environment (system-wide - no client folder needed)"
    fi

    # 0) umu-launcher (MaxiGuard runs via umu-run; auto-installed if missing)
    ensure_umu || die "MaxiGuard needs umu-launcher and it could not be installed automatically."
    # 0b) 32-bit host runtime for the Steam Linux Runtime container (see
    # ensure_32bit_runtime in common.sh) - best-effort, does not abort setup.
    ensure_32bit_runtime || true

    # 1) patched GE-Proton build (Fix 1 + Fix 3 live inside it)
    local GE_MG_DIR; _mg_ensure_ge
    [ -x "$GE_MG_DIR/files/bin/wine" ] || die "GE Wine loader not executable: $GE_MG_DIR/files/bin/wine"

    # 2) shared MaxiGuard prefix + HideWineExports + shim (all system-wide)
    local MG_PREFIX; _mg_ensure_prefix "$GE_MG_DIR" "$gamedir"
    local prefix="$MG_PREFIX"

    # 3) phBot -> MaxiGuard launcher (always available; uses the shared prefix)
    _write_phbot_mg_launcher "$SRO_HOME/phbot-maxiguard.sh" "$prefix" "$GE_MG_DIR" "$PREFIX_ROOT/phbot"

    # 4) if a specific folder was given, also write its per-folder launcher
    if [ -n "$gamedir" ]; then
        _provide_maxiguard_dlls "$gamedir"
        _write_mg_launcher "$gamedir/maxiguard.sh" "$prefix" "$gamedir" "$GE_MG_DIR" "$cexe"
        say "Launcher: $gamedir/maxiguard.sh   (GE-Proton via umu-run)"
        echo "   Start:  \"$gamedir/maxiguard.sh\"           (client $cexe, expects green 'you can login now')"
        echo "   Check:  \"$gamedir/maxiguard.sh\" check     (camouflage: all wine_get_* = hidden)"
    fi

    say "MaxiGuard environment ready (prefix: $prefix)."
    echo "   Add/start any MaxiGuard server via the launcher - no per-folder DLL needed:"
    echo "     $SRO_HOME/sro.sh"
    echo "   phBot -> MaxiGuard client:  $SRO_HOME/phbot-maxiguard.sh"
}

# ============================================================ vSroPlus + plain (wine-sro)
# Determine a Silkroad folder's client EXE (sro_client preferred).
_sro_client_exe() {   # $1 = gamedir
    local d="$1" e m
    for e in sro_client.exe Silkroad.exe Client.exe; do
        m="$(_ci_file "$d" "$e")" && { echo "$m"; return 0; }
    done
    echo ""
}

# Shared core for wine-sro clients (vSroPlus + plain). All fixes already live in
# the wine-sro build (mountmgr for vSroPlus, user32, etc.) - only a prefix +
# launcher are needed here. $1=type(vsroplus|plain) $2=gamedir $3=label prefix
_setup_wine_sro_client() {
    _require_wine_sro
    local typ="$1" gamedir; gamedir="$(cd "$2" && pwd)"
    local labelpre="$3"
    local exe; exe="$(_sro_client_exe "$gamedir")"
    [ -n "$exe" ] || die "No Silkroad client (sro_client.exe/Silkroad.exe) in '$gamedir' (wrong folder?)."
    local label; label="$(basename "$gamedir")"
    step "Setting up $labelpre: $gamedir  (EXE: $exe)"

    local prefix="$PREFIX_ROOT/$typ"
    _prefix_init "$prefix"

    _write_launcher "$gamedir/$typ.sh" "$prefix" "$gamedir" "$exe" "0 /22 0 0" ""
    say "Launcher: $gamedir/$typ.sh"
    echo "   Start:  \"$gamedir/$typ.sh\"              (client: $exe)"
    echo "   Patcher:\"$gamedir/$typ.sh\" Silkroad.exe (official launcher, if present)"
}

# vSroPlus client (disk geometry fix lives in wine-sro/mountmgr).
# Example server on this architecture: Hero Journey EN and others.
# The marker lets sro.sh's launcher know vSroPlus was actually opted into -
# it hides the vSroPlus mode choice entirely until this exists (see
# _vsroplus_available() in sro-launcher.sh), so a phBot-only install (no
# vSroPlus/MaxiGuard client ever set up) doesn't force a 3-way mode picker.
#
# setup_vsroplus [gamedir]
#   with a folder: sets up that specific client (per-folder launcher), same
#   as before.
#   WITHOUT a folder: just opts into vSroPlus support (creates its wine-sro
#   prefix, sets the "enabled" marker) so the mode appears in the launcher
#   ahead of adding any client - mirrors setup_maxiguard's own env-only mode,
#   since vSroPlus is also its own thing to opt into, even though (unlike
#   MaxiGuard) it needs no extra download: the fix is already built into
#   wine-sro itself, only the prefix is vSroPlus-specific.
setup_vsroplus() {
    if [ -z "${1:-}" ]; then
        _require_wine_sro
        step "Setting up the vSroPlus environment (system-wide - no client folder needed)"
        _prefix_init "$PREFIX_ROOT/vsroplus"
        mkdir -p "$STATE_DIR"; touch "$STATE_DIR/vsroplus_enabled"
        say "vSroPlus support enabled - add a client any time via Client setup."
        return 0
    fi
    _setup_wine_sro_client vsroplus "$1" "vSroPlus client"
    mkdir -p "$STATE_DIR"; touch "$STATE_DIR/vsroplus_enabled"
}

# Generic Silkroad client without special protection - runs directly on wine-sro
# (only the built-in wine-sro/phBot fixes, nothing MaxiGuard/vSroPlus-specific).
setup_plain() { _setup_wine_sro_client plain "$1" "Silkroad client (plain)"; }

# ---------------------------------------------------------- obtain phBot
# phBot comes straight from ProjectHax's CDN (lib/phbot-fetch.py): the same
# update.json + packages phBot's own installer downloads, fetched natively with
# real progress instead of running that .NET installer under Wine. Nothing of
# phBot is shipped with this project - phBot's terms forbid redistributing it.
#
# Where it goes: the official installer's own layout, inside the user's
# AppData\Local\Programs - "phBot Testing" / "phBot Stable" and "Manager" -
# so installs made by the old installer and by this are interchangeable.
_phbot_programs_dir() {   # $1 = prefix -> ...\AppData\Local\Programs of the prefix's user
    local users="$1/drive_c/users" u
    u="$users/$(id -un 2>/dev/null || echo "${USER:-user}")"
    if [ ! -d "$u" ]; then
        u="$(find "$users" -mindepth 1 -maxdepth 1 -type d ! -iname public 2>/dev/null | head -1)"
        [ -n "$u" ] || u="$users/$(id -un 2>/dev/null || echo "${USER:-user}")"
    fi
    printf '%s\n' "$u/AppData/Local/Programs"
}

_phbot_download() {   # $1=prefix $2=channel $3=components -> 0 when phBot.exe is in place
    local prefix="$1" channel="$2" components="$3" PKG; PKG="$(_sp_here)"
    local programs; programs="$(_phbot_programs_dir "$prefix")"
    mkdir -p "$programs"
    python3 "$PKG/lib/phbot-fetch.py" --channel "$channel" --components "$components" \
        --programs "$programs" --cache "$BUILD_CACHE/phbot" --state-file "$PHBOT_STATE_FILE"
}

# Legacy/offline path: an explicitly given phBot installer (--phbot EXE) is
# still run headlessly exactly as before - for anyone who prefers phBot's own
# installer. Without one, _phbot_download is used.
_phbot_run_installer() {   # $1=installer $2=channel $3=components $4=prefix
    local inst="$1" channel="$2" components="$3" prefix="$4" c
    local flags=(--install "--$channel")
    for c in manager plugins navmesh minimap; do
        case ",$components," in *",$c,"*) flags+=("--$c") ;; esac
    done
    say "Running the phBot installer headlessly: $inst ${flags[*]}"
    "$WINE_SRO_LOADER" "$inst" "${flags[@]}" || warn "Installer returned an error."
    "$WINE_SRO_SERVER" -w 2>/dev/null || true
    # The installer's foreground process can return before its background
    # download (Manager/navmesh/minimap) has written phBot.exe - poll.
    local waited=0
    while [ -z "$(_find_phbot_exe "$prefix")" ] && [ "$waited" -lt 300 ]; do
        sleep 5; waited=$((waited+5))
    done
}

# Finds the installed phBot.exe robustly - no matter which target directory was
# chosen in the installer. Searches the whole prefix, ignores temp/recycle-bin/
# Windows-Installer paths and takes the most recently modified (= freshly installed).
_find_phbot_exe() {   # $1 = prefix ; prints path on stdout (or empty)
    local prefix="$1"
    # IMPORTANT: '|| true' at the end. Without an installed phBot.exe 'grep'
    # returns no match and exits with code 1; under 'set -o pipefail' this colors
    # the whole pipeline to 1. In "$(_find_phbot_exe ...)" 'set -e' would then
    # abort the script at the assignment - exactly BEFORE the installer is
    # started (first --phbot run on an empty prefix). '|| true' catches this
    # (and likewise a possible SIGPIPE from 'head' with many matches).
    find "$prefix/drive_c" -iname 'phBot.exe' -type f 2>/dev/null \
        | grep -viE '/(Temp|tmp|\$Recycle\.Bin|windows/Installer|Downloads)/' \
        | while IFS= read -r f; do printf '%s\t%s\n' "$(stat -c %Y "$f" 2>/dev/null || echo 0)" "$f"; done \
        | sort -rn | head -1 | cut -f2- || true
}

# ============================================================ phBot
# $1=installer EXE (optional, legacy) $2=vcredist (optional) $3=force reinstall/update ("1"=yes)
# $4=channel ("testing"|"stable", optional) $5=components (optional, comma list of
# manager,plugins,navmesh,minimap; default: the persisted choice)
setup_phbot() {
    _require_wine_sro
    local PKG; PKG="$(_sp_here)"
    step "Setting up phBot"

    local prefix="$PREFIX_ROOT/phbot" force="${3:-0}"
    local channel="${4:-}" components
    case "$channel" in testing|stable) ;; *) channel="$(phbot_channel)" ;; esac
    if [ $# -ge 5 ]; then components="$5"; else components="$(phbot_components)"; fi
    # Persist the selected channel/components as the current preference on
    # every run (not only when a real (re)install actually happens below) -
    # mirrors the WineD3D toggle - so the GUI/status always reflects the last
    # choice, ready for whenever the next (re)install does run.
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$channel" > "$PHBOT_CHANNEL_FILE"
    printf '%s\n' "$components" > "$PHBOT_COMPONENTS_FILE"
    # Prefix + VC++ runtime (x86) - phBot requires a recent vcredist. $2
    # allows a custom path to VC_redist.x86.exe (optional).
    _prefix_init "$prefix" "${2:-}"
    eval "$(_sro_env "$prefix")"

    # A wineserver instance from an earlier attempt at this same prefix
    # (crashed mid-install, killed by the user while a phBot window was
    # waiting for input, ...) can be left running in the background and hold
    # onto whatever state it had at that point - kill it first so every step
    # below starts against a genuinely fresh session.
    "$WINE_SRO_SERVER" -k 2>/dev/null || true

    # 1) Install phBot if not present yet, OR always when a reinstall/update
    #    was explicitly requested ($3=1). Downloads land next to an existing
    #    install and replace its program files; configs are kept.
    local phbot_exe
    phbot_exe="$(_find_phbot_exe "$prefix")"
    if { [ -z "$phbot_exe" ] || [ "$force" = 1 ]; } && [ "${SRO_PHBOT_TERMS_ACCEPTED:-0}" != 1 ]; then
        # Safety net - swe.sh's phbot_gate asks before any run that gets here.
        warn "phBot not downloaded: its Terms and Conditions were not accepted ($SRO_PHBOT_TERMS_PAGE)."
    elif [ -z "$phbot_exe" ] || [ "$force" = 1 ]; then
        [ -n "$phbot_exe" ] && say "Updating/reinstalling phBot (configs are kept): $phbot_exe"
        if [ -n "${1:-}" ] && [ -f "$1" ]; then
            _phbot_run_installer "$1" "$channel" "$components" "$prefix"
            phbot_exe="$(_find_phbot_exe "$prefix")"
        elif _phbot_download "$prefix" "$channel" "$components"; then
            phbot_exe="$(_phbot_programs_dir "$prefix")/phBot ${channel^}/phBot.exe"
            # phBot's package carries its own VC_redist.x86.exe. Keep it as a
            # fallback source, and if Microsoft's current one could not be
            # fetched, this re-run puts that newer-than-prebuilt runtime in.
            local bundled; bundled="$(dirname "$phbot_exe")/VC_redist.x86.exe"
            if [ -s "$bundled" ]; then
                mkdir -p "$BUILD_CACHE/vcredist"
                cp -f "$bundled" "$BUILD_CACHE/vcredist/VC_redist.x86.phbot.exe"
                _install_vcredist "$prefix" "${2:-}"
            fi
        elif [ -n "$phbot_exe" ]; then
            warn "phBot update failed - keeping the existing install: $phbot_exe"
        else
            die "phBot could not be downloaded (network? $SRO_PHBOT_CDN).
   Check your connection and run the phBot setup again (--phbot)."
        fi
        [ -n "$phbot_exe" ] && [ -f "$phbot_exe" ] || die "phBot.exe not found after the installation."
    else
        say "phBot already installed: $phbot_exe"
    fi

    # 2) Apply the user32 fix into the prefix (patched from the wine-sro builtin)
    local builtin="$WINE_SRO_TREE/i386-windows/user32.dll"
    [ -f "$builtin" ] || builtin="$(wine_lib_dir)/i386-windows/user32.dll"
    local dst="$prefix/drive_c/windows/syswow64/user32.dll"
    mkdir -p "$prefix/drive_c/windows/syswow64"
    say "Patching user32.dll (missing Win10/11 exports) ..."
    if python3 "$PKG/patches/patch_user32.py" "$builtin" "$dst"; then
        :
    else
        warn "Patcher failed - using bundled prebuilt/user32.dll"
        cp "$PKG/prebuilt/user32.dll" "$dst"
    fi
    # stop wineserver so the new user32 is mapped (see phbot README)
    "$WINE_SRO_SERVER" -k 2>/dev/null || true
    sleep 1

    # 3) Launcher: user32 native (Win10/11 exports) AND the VC++ CRT native.
    #    Important: Wine ships its own (older) msvcp140/vcruntime140 builtins
    #    (e.g. 14.50) and loads them PREFERABLY by default - then phBot sees, via
    #    GetFileVersionInfo, an older version than the one installed by VC_redist
    #    (14.51) and reports "a newer version of vcredist is required".
    #    With a native override Wine loads the real redist DLLs from syswow64.
    local gamedir=""
    [ -n "$phbot_exe" ] && gamedir="$(dirname "$phbot_exe")"
    _write_launcher "$SRO_HOME/phbot.sh" "$prefix" "$gamedir" \
        "${phbot_exe:+$(basename "$phbot_exe")}" "" \
        'export WINEDLLOVERRIDES="user32.dll=n,b;vcruntime140,msvcp140,msvcp140_1,msvcp140_2,msvcp140_atomic_wait,concrt140=n,b"'
    say "Launcher: $SRO_HOME/phbot.sh"
    [ -n "$phbot_exe" ] && echo "   Start:  \"$SRO_HOME/phbot.sh\"" \
                        || echo "   Put phBot.exe there after installation; then: \"$SRO_HOME/phbot.sh\" /path/phBot.exe"
}
