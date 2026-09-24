#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pull the program icon out of a Windows .exe - for desktop shortcuts that
should show phBot's / the client's own icon, not this app's.

Pure Python on purpose (no pefile, no icoutils/wrestool): nothing extra has
to be installed on the host or bundled into the AppImage, and it runs on the
bundle's Python 3.8. Returns .ico bytes, which Qt's bundled ICO image plugin
decodes; main.py turns them into a PNG.

A PE file keeps its icons in the resource section as two kinds of entries:
RT_GROUP_ICON (type 14) - a directory listing the available sizes - and
RT_ICON (type 3) - the raw image of each size (a DIB, or a PNG for 256px).
The first group icon is the one Explorer shows for the exe; this rebuilds it
as a normal .ico file containing its single largest, deepest image.
"""
from __future__ import annotations

import struct

RT_ICON = 3
RT_GROUP_ICON = 14


class _PE:
    def __init__(self, data: bytes):
        self.d = data
        if data[:2] != b"MZ":
            raise ValueError("not an MZ executable")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("not a PE executable")
        coff = pe + 4
        nsections, = struct.unpack_from("<H", data, coff + 2)
        opt_size, = struct.unpack_from("<H", data, coff + 16)
        opt = coff + 20
        magic, = struct.unpack_from("<H", data, opt)
        dd = opt + (96 if magic == 0x10B else 112)          # PE32 / PE32+
        self.rsrc_rva, _ = struct.unpack_from("<II", data, dd + 2 * 8)
        if not self.rsrc_rva:
            raise ValueError("no resource section")
        self.sections = []
        sec = opt + opt_size
        for i in range(nsections):
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, sec + i * 40 + 8)
            self.sections.append((vaddr, max(vsize, rawsize), rawptr))
        self.rsrc = self.off(self.rsrc_rva)

    def off(self, rva: int) -> int:
        for vaddr, size, rawptr in self.sections:
            if vaddr <= rva < vaddr + size:
                return rawptr + (rva - vaddr)
        raise ValueError("RVA outside every section")

    def _entries(self, dir_off: int):
        """(id_or_None, is_dir, target_offset) for one resource directory.
        Named entries get id None - icons are looked up by numeric id."""
        named, ids = struct.unpack_from("<HH", self.d, dir_off + 12)
        for i in range(named + ids):
            name, target = struct.unpack_from("<II", self.d, dir_off + 16 + i * 8)
            rid = None if name & 0x80000000 else name
            yield rid, bool(target & 0x80000000), self.rsrc + (target & 0x7FFFFFFF)

    def _leaf(self, off: int, is_dir: bool) -> bytes:
        """Descend (the language level) to the first data entry."""
        while is_dir:
            _, is_dir, off = next(iter(self._entries(off)))
        rva, size = struct.unpack_from("<II", self.d, off)
        start = self.off(rva)
        return self.d[start:start + size]

    def resources(self, rtype: int) -> list:
        """[(id, bytes)] for every resource of one type, in file order."""
        for rid, is_dir, off in self._entries(self.rsrc):
            if rid == rtype and is_dir:
                return [(nid, self._leaf(o, d)) for nid, d, o in self._entries(off)]
        return []


def _image_dims(img: bytes):
    """(width, height, bits) read from the image itself - NOT from the group
    directory, which some real exes get wrong (Macro_Client.exe lists its
    32x32 icon as 32x64, the DIB's doubled XOR+AND-mask height), and Qt's
    ICO reader rejects an entry whose size disagrees with its image."""
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack_from(">II", img, 16)
        return w, h, 32
    hsize, w, h, _planes, bits = struct.unpack_from("<IiiHH", img, 0)
    if hsize < 40 or w <= 0:
        return None
    return w, abs(h) // 2, bits


def exe_icon_ico(path: str) -> bytes | None:
    """The exe's main icon as .ico file bytes (largest image only), or None
    if it has no icon / isn't a PE file / anything is malformed."""
    try:
        with open(path, "rb") as fh:
            pe = _PE(fh.read())
        groups = pe.resources(RT_GROUP_ICON)
        if not groups:
            return None
        icons = {rid: data for rid, data in pe.resources(RT_ICON) if rid is not None}
        grp = groups[0][1]
        _, _, count = struct.unpack_from("<HHH", grp, 0)
        best = None
        for i in range(count):
            _w, _h, colors, _r, _planes, _bits, _size, nid = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
            img = icons.get(nid)
            dims = _image_dims(img) if img else None
            if not dims:
                continue
            w, h, bits = dims
            if best is None or (w * h, bits) > best[0]:
                best = ((w * h, bits), (w, h, colors, bits), img)
        if best is None:
            return None
        (w, h, colors, bits), img = best[1], best[2]
        header = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", w % 256, h % 256, colors, 0, 1, bits, len(img), 6 + 16)
        return header + entry + img
    except (OSError, ValueError, struct.error, StopIteration, IndexError):
        return None
