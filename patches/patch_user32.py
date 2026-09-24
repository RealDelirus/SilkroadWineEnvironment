#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
patch_user32.py
===============
Make a Wine builtin 32-bit DLL export a handful of Windows-10/11 functions that
phBot.exe (and its bundled Python 3.14 runtime) require, so they load and run
under Wine bases that lack them.  Despite the name it patches either user32.dll
or kernel32.dll -- the export set is chosen from the target DLL's basename.

What it does
------------
1. Reads a Wine builtin 32-bit DLL (a full native PE with real code).
2. Adds the missing exports for that DLL (see USER32_ADDS / KERNEL32_ADDS):
   either an alias of an existing export (shares its function slot) or a small
   generated stub whose return value AND stdcall cleanup (`ret N`) mirror what
   Wine 11.15 returns for that function.
3. Because Wine resolves export *names* with a binary search that assumes the
   export-name table is sorted (C strcmp order), all names are re-sorted and
   the name/ordinal tables are rewritten to match.
4. Existing export forwarders and every original export are kept byte-for-byte
   (their RVAs are adjusted for the string-region shift).
5. By default it also strips the "Wine builtin DLL" marker (17 bytes at file
   offset 0x40) so the loader treats the result as a *native* DLL.  Pass
   --keep-builtin to leave the marker intact (used when patching a Proton build's
   DLL in place; the added exports still resolve as a builtin).

The stubs live in the free raw padding at the end of the .text section.  No new
section is added and no relocation is required.

Usage
-----
    python3 patch_user32.py [--keep-builtin] [SRC] [DST]

    SRC  default: /usr/lib/wine/i386-windows/user32.dll
    DST  default: $WINEPREFIX/drive_c/windows/syswow64/user32.dll
         The export set follows DST's basename (user32.dll / kernel32.dll).

Requires: Python 3 only (no third-party packages).
"""

import os
import sys
import struct

# ---------------------------------------------------------------------------
# Minimal PE parser (PE32 + PE32+), enough for export-table surgery.
# ---------------------------------------------------------------------------

def _parse_sections(data, pe):
    machine, nsec = struct.unpack_from('<HH', data, pe + 4)
    opthdr_size = struct.unpack_from('<H', data, pe + 4 + 16)[0]
    opt = pe + 4 + 20
    magic = struct.unpack_from('<H', data, opt)[0]
    sec_off = opt + opthdr_size
    secs = []
    for i in range(nsec):
        o = sec_off + i * 40
        name = data[o:o + 8].rstrip(b'\x00').decode('latin1')
        vsize, vaddr, rawsize, rawptr = struct.unpack_from('<IIII', data, o + 8)
        secs.append(dict(name=name, vsize=vsize, vaddr=vaddr,
                         rawsize=rawsize, rawptr=rawptr, off=o))
    return magic, secs


def _make_r2o(secs):
    def r2o(rva):
        for s in secs:
            span = max(s['vsize'], s['rawsize'])
            if s['rawptr'] and s['vaddr'] <= rva < s['vaddr'] + span:
                return s['rawptr'] + (rva - s['vaddr'])
        return None
    return r2o


def parse_exports(data):
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    magic, secs = _parse_sections(data, pe)
    opt = pe + 4 + 20
    is64 = (magic == 0x20b)
    dd = opt + (112 if is64 else 96)
    exp_rva, exp_size = struct.unpack_from('<II', data, dd)
    r2o = _make_r2o(secs)
    eo = r2o(exp_rva)
    (cr, tds, majmin, name_rva, base, nfunc, nname, af, an, ao) = \
        struct.unpack_from('<10I', data, eo)
    func_rvas = [struct.unpack_from('<I', data, r2o(af + i * 4))[0]
                 for i in range(nfunc)]
    ords = [struct.unpack_from('<H', data, r2o(ao + i * 2))[0]
            for i in range(nname)]
    names = []
    for i in range(nname):
        nrva = struct.unpack_from('<I', data, r2o(an + i * 4))[0]
        no = r2o(nrva)
        j = no
        while data[j]:
            j += 1
        names.append((data[no:j].decode('latin1'), ords[i]))
    return dict(pe=pe, magic=magic, secs=secs, r2o=r2o,
                exp_off=eo, exp_rva=exp_rva, exp_size=exp_size,
                name_rva=name_rva, base=base, nfunc=nfunc, nname=nname,
                af=af, an=an, ao=ao, func_rvas=func_rvas,
                ords=ords, names=names)


# ---------------------------------------------------------------------------
# The exports to add, per DLL.
#
# Each entry is ("name", target) where target is either
#     a str            -> alias: reuse the function slot of that existing export
#     a bytes object   -> raw machine code for a generated stub function
#
# The stubs mirror what Wine 11.15 returns for the same function (return value
# AND the correct stdcall stack cleanup `ret N`), so a call behaves exactly as it
# did on 11.15 -- where phBot already ran.  phBot mostly only needs the symbols
# to *resolve* (it imports them via its bundled Python 3.14 runtime), but getting
# `ret N` right means a stub is safe even if actually called.
# ---------------------------------------------------------------------------

def _stub(retval, nargs):
    """Machine code: set EAX=retval, then stdcall-return cleaning nargs*4 bytes."""
    code = b'\x31\xc0' if retval == 0 else b'\xb8' + struct.pack('<I', retval & 0xffffffff)
    argbytes = nargs * 4
    code += b'\xc3' if argbytes == 0 else b'\xc2' + struct.pack('<H', argbytes)
    return code

E_NOTIMPL     = 0x80004005
ERROR_NOT_FOUND = 0x490          # 1168; what Wine 11.15's Pss{Capture,Query} return

# USER32: 8 LongPtr aliases + Win10 pointer / immersive stubs.
USER32_ADDS = [
    ("GetPointerDevice",         _stub(0, 2)),   # BOOL  (present in GE-Proton; used on plain 11.15)
    ("SkipPointerFrameMessages", _stub(1, 1)),   # BOOL TRUE
    ("GetWindowLongPtrA",        "GetWindowLongA"),
    ("GetWindowLongPtrW",        "GetWindowLongW"),
    ("SetWindowLongPtrA",        "SetWindowLongA"),
    ("SetWindowLongPtrW",        "SetWindowLongW"),
    ("GetClassLongPtrA",         "GetClassLongA"),
    ("GetClassLongPtrW",         "GetClassLongW"),
    ("SetClassLongPtrA",         "SetClassLongA"),
    ("SetClassLongPtrW",         "SetClassLongW"),
    # Win10 pointer-frame / immersive APIs: present in Wine 11.15 but absent in
    # older Wine bases (e.g. GE-Proton11-6).  All return BOOL FALSE.
    ("GetPointerFrameInfo",             _stub(0, 3)),
    ("GetPointerFrameInfoHistory",      _stub(0, 4)),
    ("GetPointerFramePenInfo",          _stub(0, 3)),
    ("GetPointerFramePenInfoHistory",   _stub(0, 4)),
    ("GetPointerFrameTouchInfo",        _stub(0, 3)),
    ("GetPointerFrameTouchInfoHistory", _stub(0, 4)),
    ("GetPointerPenInfoHistory",        _stub(0, 3)),
    ("IsImmersiveProcess",              _stub(0, 1)),
]

# KERNEL32: Win8+ Process-Snapshot (PSS) API + GetMachineTypeAttributes.
# GE-Proton11-6 implements none of these (not in kernel32 nor kernelbase);
# Wine 11.15 has them (Pss* as error stubs).  Mirror 11.15's returns.
KERNEL32_ADDS = [
    ("PssCaptureSnapshot",      _stub(ERROR_NOT_FOUND, 4)),  # DWORD error
    ("PssQuerySnapshot",        _stub(ERROR_NOT_FOUND, 4)),  # DWORD error
    ("PssFreeSnapshot",         _stub(0, 2)),                # DWORD ERROR_SUCCESS
    ("GetMachineTypeAttributes", _stub(E_NOTIMPL, 2)),       # HRESULT
]

# Selected by DLL basename; default (unknown) is USER32 for back-compat.
ADDS_BY_DLL = {"user32.dll": USER32_ADDS, "kernel32.dll": KERNEL32_ADDS}
ADD_EXPORTS = USER32_ADDS                       # back-compat module attribute

# Back-compat aliases (older external callers referenced these names).
STUB_TRUE = _stub(1, 0)
STUB_FAIL = b'\xb8\x05\x40\x00\x80\xc3'


def patch(src, dst, keep_builtin=False, adds=None):
    if adds is None:
        adds = ADD_EXPORTS
    data = bytearray(open(src, 'rb').read())
    e = parse_exports(data)
    secs, r2o = e['secs'], e['r2o']
    base, nfunc, nname = e['base'], e['nfunc'], e['nname']
    af, an, ao = e['af'], e['an'], e['ao']
    func_rvas = list(e['func_rvas'])
    ords = list(e['ords'])
    names = list(e['names'])
    byname = dict(names)
    exp_rva, name_rva = e['exp_rva'], e['name_rva']

    # Sanity: alias base exports must exist; note names already present.
    have = set(n for n, _ in names)
    for nm, tgt in adds:
        if nm in have:
            print("note: %s already present, skipping" % nm)
        if isinstance(tgt, str) and tgt not in have:
            sys.exit("error: base export %s not found in %s" % (tgt, src))

    text = secs[0]
    edata = next(s for s in secs if s['name'] == '.edata')
    next_va = secs[secs.index(edata) + 1]['vaddr']

    # Read originals NOW (before any writes), so later table growth cannot
    # clobber the values we still need.
    old_name_rvas = [struct.unpack_from('<I', data, r2o(an + i * 4))[0]
                     for i in range(nname)]
    ts = struct.unpack_from('<I', data, e['exp_off'] + 4)[0]

    STR_START = name_rva                          # DLL-name string starts the region
    STR_END = edata['vaddr'] + edata['vsize']

    # distinct raw-code stub blobs referenced by adds (one function slot each,
    # deduplicated: identical machine code shares a slot)
    stub_blobs = []
    for _nm, _tgt in adds:
        if isinstance(_tgt, (bytes, bytearray)) and bytes(_tgt) not in stub_blobs:
            stub_blobs.append(bytes(_tgt))
    nstub = len(stub_blobs)

    # ---- new table sizes ----
    nf2 = nfunc + nstub                           # +1 function slot per distinct stub
    nn2 = nname + len(adds)                        # +len(adds) names
    ft_rva = af
    nt_rva = ft_rva + nf2 * 4
    ot_rva = nt_rva + nn2 * 4
    ot_end = ot_rva + nn2 * 2

    # shift the string region down to clear the (grew) tables
    delta = max(0, ot_end - STR_START)
    delta = (delta + 3) & ~3
    new_str_start = STR_START + delta
    new_str_end = STR_END + delta
    ns_rva = (new_str_end + 3) & ~3               # new name strings go after

    # new name string blob
    blob = bytearray()
    new_name_rvas = []
    for nm, _ in adds:
        off = len(blob)
        blob += nm.encode('ascii') + b'\x00'
        new_name_rvas.append(ns_rva + off)
    total_end = ns_rva + len(blob)
    if total_end >= next_va:
        sys.exit("error: no room in .edata (end %#x >= next section %#x)"
                 % (total_end, next_va))

    # ---- code cave for the stubs (end of .text raw padding) ----
    cave = text['vaddr'] + text['vsize']          # first free RVA after code
    cave = (cave + 3) & ~3
    need = sum(len(b) for b in stub_blobs)
    if cave + need > text['vaddr'] + max(text['rawsize'], text['vsize']):
        sys.exit("error: no raw room in .text for stubs")
    text_new_vsize = (cave + need - text['vaddr'] + 0xff) & ~0xff
    stub_rva = {}                                  # code bytes -> code RVA
    stub_slot = {}                                 # code bytes -> function-table slot
    _off = cave
    for _i, _b in enumerate(stub_blobs):
        stub_rva[_b] = _off
        stub_slot[_b] = nfunc + _i
        _off += len(_b)

    def w(rva, b):
        o = r2o(rva)
        if o is None:
            sys.exit("error: RVA %#x not in a mapped section" % rva)
        data[o:o + len(b)] = b

    def readr(rva, n):
        return bytes(data[r2o(rva):r2o(rva) + n])

    # 1) move old string region down by delta
    w(new_str_start, readr(STR_START, STR_END - STR_START))
    # 2) blank the region the (grew) tables now occupy
    w(ot_rva, b'\x00' * (new_str_start - ot_rva))
    # 3) write the stubs into the .text cave
    for _b in stub_blobs:
        w(stub_rva[_b], _b)
    # 4) function table: original RVAs (forwarders shifted) + one slot per stub
    fwd = lambda r: r + delta if (STR_START <= r < STR_END) else r
    new_ft = [fwd(r) for r in func_rvas] + [stub_rva[_b] for _b in stub_blobs]
    w(ft_rva, struct.pack('<%dI' % nf2, *new_ft))
    # 5) new name strings
    w(ns_rva, bytes(blob))

    # 6) build (name, name_rva, slot) for every export, then SORT by name
    entries = []
    for i, (nm, slot) in enumerate(names):
        entries.append([nm, old_name_rvas[i] + delta, slot])
    added = 0
    for (nm, tgt), nrva in zip(adds, new_name_rvas):
        if nm in have:
            continue                              # already present; keep original
        if isinstance(tgt, (bytes, bytearray)):
            slot = stub_slot[bytes(tgt)]          # generated stub
        else:
            slot = byname[tgt]                    # alias: reuse base slot
        entries.append([nm, nrva, slot])
        added += 1
    entries.sort(key=lambda t: t[0].encode('ascii'))
    for i in range(len(entries) - 1):
        if not (entries[i][0].encode() < entries[i + 1][0].encode()):
            sys.exit("error: names not strictly sorted at %d: %r %r"
                     % (i, entries[i][0], entries[i + 1][0]))
    # recompute counts from the final list
    final_nname = len(entries)
    w(nt_rva, struct.pack('<%dI' % final_nname, *[t[1] for t in entries]))
    w(ot_rva, struct.pack('<%dH' % final_nname, *[t[2] for t in entries]))

    # 7) export directory
    w(exp_rva, struct.pack('<10I', 0, ts, 0, name_rva + delta, base,
                           nf2, final_nname, ft_rva, nt_rva, ot_rva))
    # 8) strip the "Wine builtin DLL" marker so the loader treats the file as a
    #    *native* DLL (needed when the patched file is loaded via a
    #    WINEDLLOVERRIDES=user32=n,b override from the prefix's syswow64).
    #    With --keep-builtin we leave the marker intact: the file stays a Wine
    #    builtin and the added exports are simply present in its PE export table
    #    (same technique patch-proton-exports.py uses on builtin ntdll).  This
    #    is what we want when patching a Proton build's user32 *in place*, so no
    #    override is needed and Proton's per-launch DLL relinking can't clobber
    #    it (the symlink target -- the build file -- is already patched).
    if not keep_builtin:
        data[0x40:0x40 + 17] = b'\x00' * 17
    # 9) grow .text vsize (stubs) and .edata vsize + export dir size (strings)
    struct.pack_into('<I', data, text['off'] + 8, text_new_vsize)
    new_eva = total_end - edata['vaddr']
    struct.pack_into('<I', data, edata['off'] + 8, new_eva)
    opt = e['pe'] + 4 + 20
    dd_export = opt + (112 if e['magic'] == 0x20b else 96)
    assert exp_rva == struct.unpack_from('<I', data, dd_export)[0]
    struct.pack_into('<I', data, dd_export + 4, new_eva)

    open(dst, 'wb').write(data)
    print("patched %s -> %s" % (src, dst))
    print("  names: %d -> %d (+%d new)   funcs: %d -> %d   strshift=%d"
          % (nname, final_nname, added, nfunc, nf2, delta))
    print("  .text vsize -> %#x   .edata vsize -> %#x" % (text_new_vsize, new_eva))
    addset = set(a for a, _ in adds)
    for t in entries:
        if t[0] in addset:
            print("  %-30s slot=%-5d rva=%#x" % (t[0], t[2], new_ft[t[2]]))


def main():
    args = [a for a in sys.argv[1:] if a != '--keep-builtin']
    keep_builtin = '--keep-builtin' in sys.argv[1:]
    src = args[0] if len(args) > 0 else '/usr/lib/wine/i386-windows/user32.dll'
    if len(args) > 1:
        dst = args[1]
    else:
        wp = os.environ.get('WINEPREFIX', os.path.expanduser('~/.wine'))
        dst = os.path.join(wp, 'drive_c', 'windows', 'syswow64', 'user32.dll')
    if not os.path.exists(src):
        sys.exit("error: source builtin DLL not found: %s" % src)
    # Pick the export set from the target DLL's name (default: USER32).
    dllname = os.path.basename(dst).lower()
    adds = ADDS_BY_DLL.get(dllname, USER32_ADDS)
    os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
    patch(src, dst, keep_builtin=keep_builtin, adds=adds)


if __name__ == '__main__':
    main()
