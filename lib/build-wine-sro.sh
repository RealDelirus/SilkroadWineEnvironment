#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# lib/build-wine-sro.sh - builds ONE self-contained, patched Wine 11.15
# (NEW WoW64) from source into $WINE_SRO and combines all Wine-side fixes
# (no Proton, independent of the system Wine):
#
#   1. mountmgr.sys  : disk geometry from real size          (vSroPlus)
#   2. export rename : wine_get_* out of the PE export tables (MaxiGuard, Fix 1)
#   3. dxdiagn.dll   : js -> jne against login crash          (MaxiGuard, Fix 3)
#
# Why a full build instead of a symlink farm: only a Wine installed with
# --prefix=$WINE_SRO is self-consistent (loader finds datadir/DLLs relative to
# its own prefix) and runs distro-independently. It also gives us exactly
# version 11.15 as new WoW64 - required for vSroPlus and matching the dxdiagn/
# user32 offsets. The system Wine is neither needed nor modified.
#
# Usage: build_wine_sro
set -euo pipefail

_bws_here() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }

build_wine_sro() {
    local PKG; PKG="$(_bws_here)"
    local PATCHFILE="$PKG/patches/0001-mountmgr.sys-derive-disk-geometry-from-media-size.patch"
    local VER="$SRO_WINE_VERSION"

    step "Building wine-sro: self-contained Wine $VER (new WoW64) into $WINE_SRO"

    # Check toolchain
    local t
    for t in gcc make bison flex x86_64-w64-mingw32-gcc i686-w64-mingw32-gcc curl tar patch python3; do
        command -v "$t" >/dev/null 2>&1 || die "Build tool '$t' missing (build dependencies not installed? run without --skip-deps)."
    done
    # mingw g++ is mandatory: without the PE C++ cross-compiler Wine does NOT
    # build the C++ modules - among them icu.dll, which icuuc.dll forwards to.
    # If it is missing, phBot fails with "ucnv_getMaxCharSize ... could not be
    # located in icuuc.dll".
    for t in i686-w64-mingw32-g++ x86_64-w64-mingw32-g++; do
        command -v "$t" >/dev/null 2>&1 || die "Build tool '$t' missing (mingw-g++).
   Without a PE C++ compiler icu.dll is not built -> phBot won't start.
   Install the g++ mingw packages (run without --skip-deps or use --deps-only)."
    done

    # -------------------------------------------------- 1) fetch Wine sources
    local SRC="$BUILD_CACHE/wine-src"
    mkdir -p "$SRC"
    if [ ! -d "$SRC/wine-$VER" ]; then
        local TARBALL="$SRC/wine-$VER.tar.xz"
        if [ ! -f "$TARBALL" ]; then
            say "Downloading Wine sources wine-$VER ..."
            # WineHQ layout: .0 release under source/<version>/, point release
            # under source/<major>.x/. Try both patterns.
            local major="${VER%%.*}" minor="${VER#*.}"; minor="${minor%%.*}"
            local urls=()
            if [ "$minor" = 0 ]; then
                urls+=("https://dl.winehq.org/wine/source/$VER/wine-$VER.tar.xz")
                urls+=("https://dl.winehq.org/wine/source/$major.x/wine-$VER.tar.xz")
            else
                urls+=("https://dl.winehq.org/wine/source/$major.x/wine-$VER.tar.xz")
                urls+=("https://dl.winehq.org/wine/source/$VER/wine-$VER.tar.xz")
            fi
            local u ok=0
            for u in "${urls[@]}"; do
                say "  trying: $u"
                if curl -# -fLo "$TARBALL" "$u"; then ok=1; break; fi
            done
            [ "$ok" = 1 ] || die "Download of Wine sources wine-$VER failed."
        fi
        say "Extracting ..."
        tar xf "$TARBALL" -C "$SRC"
    fi
    cd "$SRC/wine-$VER"

    # -------------------------------------------------- 2) source patches
    if patch -p1 --dry-run --silent < "$PATCHFILE" >/dev/null 2>&1; then
        patch -p1 < "$PATCHFILE"; say "mountmgr patch applied."
    elif patch -p1 -R --dry-run --silent < "$PATCHFILE" >/dev/null 2>&1; then
        say "mountmgr patch already applied."
    else
        die "mountmgr patch does not apply to wine-$VER (dlls/mountmgr.sys/device.c changed?)."
    fi

    # Fix the dxdiagn login crash (MaxiGuard) at source level - compiler-
    # independent instead of fragile byte offsets.
    say "Applying dxdiagn source fix (MaxiGuard)"
    python3 "$PKG/patches/fix-dxdiagn-source.py" "$PWD"

    # -------------------------------------------------- 3) configure + build + install
    # NEW WoW64: --enable-archs=i386,x86_64 (32-bit PE via mingw, Unix side 64-bit only).
    #
    # --without-opencl: OpenCL is not needed by the SRO clients/phBot. It is
    # deliberately disabled, otherwise the build depends on the combination of the
    # OpenCL loader (libOpenCL, e.g. via ocl-icd) AND the OpenCL headers: if only
    # the loader is present (typical with NVIDIA/CUDA/ocl-icd on Arch/CachyOS) but
    # the opencl-headers package is missing, Wine still enables the module but does
    # not include <CL/cl.h> -> dlls/opencl/unix_thunks.c calls undeclared cl*
    # functions. Under GCC >=14 (CachyOS: GCC 15) "implicit declaration" is an
    # ERROR (not just a warning) and the build aborts. Disabling it is robust
    # across distros and saves a dependency.
    mkdir -p build && cd build
    # Reconfigure when (a) there is no Makefile yet OR (b) an OLD Makefile from a
    # previous run still has OpenCL enabled (then it contains 'dlls/opencl/opencl.so'
    # as a target). This lets an already misconfigured build directory heal itself
    # instead of aborting again with the OpenCL error. config.h does not change
    # due to --without-opencl, so a reconfigure discards no already-compiled objects.
    if [ ! -f Makefile ] || grep -q 'dlls/opencl/opencl\.so' Makefile; then
        say "running configure (a few minutes) ..."
        ../configure --prefix="$WINE_SRO" --enable-archs=i386,x86_64 \
                     --disable-tests --without-opencl > configure.log 2>&1 \
            || die "configure failed, see $PWD/configure.log"
    fi
    # Safety net: without the PE C++ compiler icu.dll (and thus phBot) would be
    # missing. Since g++ is already mandatory above, this is only a safety net.
    if grep -qi "PE cross-compiler supporting C++.*not found" configure.log; then
        die "configure found NO PE C++ compiler - icu.dll would be missing.
   Install mingw-g++ (--deps-only) and delete 'build' in the cache:
   rm -rf \"$SRC/wine-$VER/build\" ; then rebuild."
    fi
    say "building Wine $VER (this takes a while - 20-60 min depending on CPU) ..."
    make -j"$(nproc)" > build.log 2>&1 \
        || die "Build failed. Last lines:
$(tail -n 20 build.log)
   Full log: $PWD/build.log"

    say "installing into $WINE_SRO ..."
    rm -rf "$WINE_SRO"
    make install > install.log 2>&1 \
        || die "make install failed, see $PWD/install.log"

    [ -x "$WINE_SRO_LOADER" ] || die "After make install the loader is missing ($WINE_SRO_LOADER)."
    say "mountmgr patch is part of this build."

    # -------------------------------------------------- 4) export rename + dxdiagn
    # The MaxiGuard scripts expect the Proton layout 'files/lib/wine/...'. We
    # mirror our installed tree via a shim directory; the files underneath are
    # real (make install copies real PE DLLs).
    local SHIM; SHIM="$(mktemp -d "${TMPDIR:-/tmp}/wine-sro-shim.XXXXXX")"
    mkdir -p "$SHIM/files/lib"
    ln -sfn "$WINE_SRO_TREE" "$SHIM/files/lib/wine"

    step "Fix 1: renaming wine_get_* exports (MaxiGuard)"
    python3 "$PKG/patches/patch-exports.py" "$SHIM" \
        || { rm -rf "$SHIM"; die "Export rename failed - build not usable."; }
    rm -rf "$SHIM"
    # (Fix 3 dxdiagn is now a source patch, see above - no binary patch anymore.)

    # -------------------------------------------------- 4b) Wine Mono, offline
    # Staged here because this is the one point where the Wine source tree is
    # guaranteed to be around to read MONO_VERSION from, and it has to happen
    # before any prefix is created - otherwise wineboot opens its "install
    # Wine Mono?" dialog and waits for a click.
    ensure_wine_mono || true

    # -------------------------------------------------- 5) store state
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$VER" > "$STATE_DIR/wine-version"
    date -u +%FT%TZ       > "$STATE_DIR/wine-built-at"

    step "wine-sro done"
    say "Loader : $WINE_SRO_LOADER"
    say "Version: $("$WINE_SRO_LOADER" --version 2>/dev/null || echo "wine-$VER")"
    say "Size   : $(du -sh "$WINE_SRO" 2>/dev/null | cut -f1)"
}

# directly runnable (for debugging)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    . "$HERE/common.sh"
    build_wine_sro
fi
