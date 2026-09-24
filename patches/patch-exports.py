#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Renames the Wine-giveaway exports in a Proton/Wine build.

Why: the staging patch HideWineExports hides wine_get_version & co. only from
GetProcAddress/LdrGetProcedureAddress. VMProtect (here: MaxiGuard.dll) however
walks the export table of the mapped ntdll image itself and finds the names
anyway -> "Virtual Machine detected!".

Solution: rename the names directly in the PE files. All replacement names are
exactly the same length AND keep their position in the alphabetically sorted
export name table (mandatory, because GetProcAddress/import resolution binary-
searches over it). Importing builtins (shell32, ws2_32, umu.exe, ...) are patched
too, so everything keeps working.

Usage:  ./patch-exports.py <proton-dir> [--check]
"""
import os, sys, struct

RENAMES = {
    b"wine_get_version":        b"widl_svc_version",
    b"wine_get_build_id":       b"widl_svc_build_id",
    b"wine_get_host_version":   b"widl_svc_host_version",
    b"wine_get_unix_file_name": b"widl_svc_unix_file_name",
    b"wine_get_dos_file_name":  b"widl_svc_dos_file_name",
    # These two are ntdll-internal but still exported, and VMProtect (MaxiGuard.dll)
    # walks the raw export table and flags them just like the wine_get_* names.
    # Replacement names keep the exact alphabetical slot between their real
    # neighbours (wine_nt_to_unix_file_name / wine_server_fd_to_handle, and
    # __wine_dbg_header / __wine_dbg_strdup) - verified with --check below.
    b"wine_server_call":        b"wine_rpcsvc_call",
    b"__wine_dbg_output":       b"__wine_dbg_notify",
}
for a, b in RENAMES.items():
    assert len(a) == len(b), (a, b)

PE_DIRS = ("files/lib/wine/i386-windows", "files/lib/wine/x86_64-windows")


# --- minimal PE parser, only for verifying the sort order -------------------
def _load(fn):
    d = open(fn, "rb").read()
    if d[:2] != b"MZ":
        raise ValueError("not a PE")
    e = struct.unpack_from("<I", d, 0x3C)[0]
    if d[e:e + 4] != b"PE\0\0":
        raise ValueError("not a PE")
    magic = struct.unpack_from("<H", d, e + 24)[0]
    nsec = struct.unpack_from("<H", d, e + 6)[0]
    optsz = struct.unpack_from("<H", d, e + 20)[0]
    so = e + 24 + optsz
    secs = [(struct.unpack_from("<I", d, so + 40 * i + 12)[0],
             struct.unpack_from("<I", d, so + 40 * i + 16)[0],
             struct.unpack_from("<I", d, so + 40 * i + 20)[0]) for i in range(nsec)]
    return d, e + 24 + (112 if magic == 0x20B else 96), secs


def export_names(fn):
    d, ddoff, secs = _load(fn)
    rva = struct.unpack_from("<I", d, ddoff)[0]
    if not rva:
        return []

    def r2o(r):
        for va, rs, pr in secs:
            if va <= r < va + max(rs, 1):
                return pr + (r - va)
        raise ValueError(hex(r))

    o = r2o(rva)
    n = struct.unpack_from("<I", d, o + 24)[0]
    no = r2o(struct.unpack_from("<I", d, o + 32)[0])
    out = []
    for i in range(n):
        so = r2o(struct.unpack_from("<I", d, no + 4 * i)[0])
        out.append(d[so:d.index(b"\0", so)].decode("latin1"))
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    check_only = "--check" in sys.argv
    total = 0

    for sub in PE_DIRS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            print(f"  ! {sub} missing"); continue
        for fn in sorted(os.listdir(base)):
            p = os.path.join(base, fn)
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            data = open(p, "rb").read()
            new = data
            hits = {}
            for old, rep in RENAMES.items():
                c = new.count(old + b"\0")
                if c:
                    hits[old.decode()] = c
                    new = new.replace(old + b"\0", rep + b"\0")
            if not hits:
                continue
            total += sum(hits.values())
            print(f"  {sub.split('/')[-1]:16s} {fn:28s} {hits}")
            if not check_only:
                mode = os.stat(p).st_mode
                os.chmod(p, mode | 0o200)
                with open(p, "wb") as fh:
                    fh.write(new)
                os.chmod(p, mode)

    print(f"\n{'found' if check_only else 'replaced'}: {total} occurrences")

    # verify the sort order of the export name tables
    print("\nVerification (export name table must stay sorted):")
    ok = True
    for sub in PE_DIRS:
        for dll in ("ntdll.dll", "kernel32.dll"):
            p = os.path.join(root, sub, dll)
            if not os.path.exists(p):
                continue
            names = export_names(p)
            srt = names == sorted(names)
            leak = [n for n in names if n.startswith("wine_get")]
            ok &= srt
            print(f"  {sub.split('/')[-1]:16s} {dll:14s} sorted={srt}  "
                  f"wine_get* exports left: {leak if leak else 'none'}")
    print("\nOK" if ok else "\nERROR: sort order broken - do not use this build!")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
