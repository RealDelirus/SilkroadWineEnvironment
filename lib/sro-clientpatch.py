#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""sro-clientpatch.py - neutralise a Silkroad client's "execute Silkroad.exe" check.

A Silkroad client started directly (the way this launcher, edxSilkroadLoader5
and every bot start it - `sro_client.exe 0 /NN 0 0`) is supposed to be allowed
to run.  Most clients accept that, but some - Cyron is one - keep the original
launcher HANDSHAKE and refuse to start with a modal

    ERROR:  Please Execute the "Silkroad.exe."

The client's own launcher (Silkroad.exe) normally satisfies that handshake:
before it starts the client it creates two named mutexes ("Silkroad Online
Launcher" and "Ready"), passes the client its own window handle, and exits when
the client tells it to.  edxSilkroadLoader5 works because its loader stands in
for that launcher.  We start the client on its own, so the handshake fails and
the client bails out with the message above - exactly the "one more check" that
was still missing next to the Redirect feature.

Rather than emulate the whole launcher IPC, this does what a memory patcher (the
EDX DLL's own style) does, but once and on a COPY of the exe: it finds the one
branch that guards the error box and forces it to always take the "proceed"
path.  The check disassembles to:

    cmp  dword [g_flag], 0
    jne  proceed                 <- guard A (flag already validated)
    call launcher_handshake      <- creates/checks the mutexes + waits
    test al, al
    jne  proceed                 <- guard B (handshake succeeded)
    push 0 / push "ERROR" / push "Please Execute the \"Silkroad.exe.\"" / push 0
    call MessageBoxA
    ...
  proceed:

Flipping the conditional jump that sits right before the message (guard B) into
an unconditional jump makes the client always continue, whatever the handshake
returned.  The client then runs completely normally - it reads its content
locale, gateway and port from Media.pk2 as always; only the launcher gate is
gone.  The original exe is never touched (sro.sh runs the patched copy from a
shadow folder of symlinks), so a client update simply re-triggers a re-patch.

The guard is located by signature (the message string -> the code that pushes
it -> the nearest preceding short conditional jump into the proceed label), so
it is not tied to one client build.  If no such branch is found the exe is left
alone and the client starts as before (with the message, if it has the check).

Usage:
    sro-clientpatch.py --check <client.exe>     # exit 0 if patchable, 1 if not
    sro-clientpatch.py <client.exe> <out.exe>   # write patched copy (0), else 1

Pure python3, no third-party modules (mirrors sro-clientinfo.py).
"""
import struct
import sys

# The exact modal text, as stored (with the surrounding quotes) in the client.
NEEDLE = b'Please Execute the "Silkroad.exe."'


def _sections(f):
    """(imagebase, [(name, va, vsize, raw, rawsize), ...]) for a PE32 image."""
    e_lfanew = struct.unpack_from("<I", f, 0x3C)[0]
    if f[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    coff = e_lfanew + 4
    nsec = struct.unpack_from("<H", f, coff + 2)[0]
    opt = coff + 20
    if struct.unpack_from("<H", f, opt)[0] != 0x10B:
        raise ValueError("not a 32-bit (PE32) image")   # Silkroad clients are 32-bit
    imgbase = struct.unpack_from("<I", f, opt + 28)[0]
    opt_size = struct.unpack_from("<H", f, coff + 16)[0]
    sec = opt + opt_size
    out = []
    for i in range(nsec):
        o = sec + i * 40
        name = f[o:o + 8].split(b"\0", 1)[0]
        vsize, va, rawsize, raw = struct.unpack_from("<IIII", f, o + 8)
        out.append((name, imgbase + va, vsize, raw, rawsize))
    return imgbase, out


def _off2va(secs, off):
    for _n, va, vsize, raw, rawsize in secs:
        if raw <= off < raw + rawsize:
            return va + (off - raw)
    return None


def _va2foff(secs, va):
    for _n, sva, vsize, raw, rawsize in secs:
        if sva <= va < sva + vsize and (va - sva) < rawsize:
            return raw + (va - sva)
    return None


def _text(secs):
    for s in secs:
        if s[0] == b".text":
            return s
    return secs[0]


def find_guard(f):
    """Locate the branch guarding the launcher-check message.

    Returns (file_offset, original_bytes, patched_bytes) or None. The patched
    bytes turn the guarding conditional jump into an unconditional jump to the
    same target (the "proceed" label), so the message is never shown.
    """
    imgbase, secs = _sections(f)
    _n, tva, tvsize, traw, trawsize = _text(secs)
    text = f[traw:traw + trawsize]

    start = 0
    while True:
        si = f.find(NEEDLE, start)
        if si < 0:
            return None
        start = si + 1
        sva = _off2va(secs, si)
        if sva is None:
            continue
        # every `push <string>` (68 imm32) that references this message
        push = b"\x68" + struct.pack("<I", sva)
        psearch = 0
        while True:
            pi = text.find(push, psearch)
            if pi < 0:
                break
            psearch = pi + 1
            push_va = tva + pi
            # nearest preceding short conditional jump (je/jne rel8) that jumps
            # FORWARD into the proceed label just past the small error block.
            for back in range(2, 0x48):
                j = pi - back
                if j < 0:
                    break
                op = text[j]
                if op in (0x74, 0x75):                 # je / jne rel8
                    disp = text[j + 1]
                    if disp < 0x80:                    # forward only
                        tgt = tva + j + 2 + disp
                        if push_va < tgt <= push_va + 0x40:
                            return (traw + j, bytes([op, disp]), bytes([0xEB, disp]))
                elif op == 0x0F and text[j + 1] in (0x84, 0x85):   # je/jne rel32
                    disp = struct.unpack_from("<i", text, j + 2)[0]
                    tgt = tva + j + 6 + disp
                    if push_va < tgt <= push_va + 0x60:
                        # 0F 8x d32  ->  90 (nop) E9 d32 : same length, same target.
                        return (traw + j, bytes([0x0F, text[j + 1]]), bytes([0x90, 0xE9]))
    return None


def main(argv):
    check = False
    args = argv[1:]
    if args and args[0] == "--check":
        check = True
        args = args[1:]
    if (check and len(args) != 1) or (not check and len(args) != 2):
        sys.stderr.write(__doc__.split("Usage:")[-1])
        return 2
    try:
        data = bytearray(open(args[0], "rb").read())
    except OSError as e:
        sys.stderr.write("%s\n" % e)
        return 2
    try:
        g = find_guard(data)
    except (ValueError, struct.error, IndexError) as e:
        sys.stderr.write("%s\n" % e)
        return 1
    if not g:
        return 1
    off, orig, new = g
    if check:
        sys.stderr.write("launcher-check guard at 0x%x: %s -> %s\n"
                         % (off, orig.hex(), new.hex()))
        return 0
    data[off:off + len(new)] = new
    try:
        with open(args[1], "wb") as fh:
            fh.write(data)
    except OSError as e:
        sys.stderr.write("%s\n" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
