#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""sro-clientinfo.py - read a Silkroad client's connection info from Media.pk2.

Prints what the client itself would connect to, read from the two files the
Silkroad launcher uses for that (both live in the root of Media.pk2):

  DIVISIONINFO.TXT   content locale + the divisions and their gateway hosts
  GATEPORT.TXT       the gateway TCP port

Used by sro.sh (installed next to it as assets/sro-clientinfo.py) to know
WHICH connections of a client to redirect, and by the GUI to pre-fill the
locale in the redirect form. Output (stdout, one key per line):

  locale=22
  gateport=15779
  division=Cyron
  gateway=gw1.example.com
  gateway=gw2.example.com

Exit code 1 (message on stderr) when the pk2 is missing or unreadable - e.g.
a server that re-keyed its archives with a custom Blowfish key.

Standalone on purpose: python3 only, no third-party modules (the Blowfish
tables are the hex digits of pi and are computed instead of shipped).
"""
import struct
import sys
from pathlib import Path

# Joymax's pk2 key: the ASCII base key XORed with a fixed salt.
PK2_BASE_KEY = b"169841"
PK2_SALT = bytes([0x03, 0xF8, 0xE4, 0x44, 0x88, 0x99, 0x3F, 0x64, 0xFE, 0x35])
PK2_HEADER = 256
ENTRY_SIZE = 128
ENTRIES_PER_BLOCK = 20


def _pi_words(count: int) -> list:
    """The first `count` 32-bit words of pi's fractional part - exactly
    Blowfish's initial P-array followed by its four S-boxes."""
    bits = count * 32 + 64
    one = 1 << bits

    def arctan_inv(x: int) -> int:
        total = term = one // x
        x2, n, sign = x * x, 1, -1
        while term:
            term //= x2
            n += 2
            total += sign * (term // n)
            sign = -sign
        return total

    pi = 16 * arctan_inv(5) - 4 * arctan_inv(239)      # Machin, fixed point
    frac = (pi - (3 << bits)) >> 64                    # drop the 3 and the guard bits
    return [(frac >> (32 * (count - 1 - i))) & 0xFFFFFFFF for i in range(count)]


class Blowfish:
    """Blowfish as Joymax uses it: standard algorithm, but each 8-byte block
    is read as two LITTLE-endian words (x86 pointer casts in their code)."""

    def __init__(self, key: bytes):
        words = _pi_words(18 + 4 * 256)
        self.p = words[:18]
        self.s = [words[18 + 256 * i:18 + 256 * (i + 1)] for i in range(4)]
        k = 0
        for i in range(18):
            w = 0
            for _ in range(4):
                w = (w << 8) | key[k % len(key)]
                k += 1
            self.p[i] ^= w
        left = right = 0
        for i in range(0, 18, 2):
            left, right = self._encipher(left, right)
            self.p[i], self.p[i + 1] = left, right
        for box in self.s:
            for i in range(0, 256, 2):
                left, right = self._encipher(left, right)
                box[i], box[i + 1] = left, right

    def _f(self, x: int) -> int:
        s = self.s
        h = (s[0][x >> 24] + s[1][(x >> 16) & 0xFF]) & 0xFFFFFFFF
        return ((h ^ s[2][(x >> 8) & 0xFF]) + s[3][x & 0xFF]) & 0xFFFFFFFF

    def _encipher(self, left: int, right: int):
        p = self.p
        for i in range(16):
            left ^= p[i]
            right ^= self._f(left)
            left, right = right, left
        left, right = right, left
        return left ^ p[17], right ^ p[16]

    def _decipher(self, left: int, right: int):
        p = self.p
        for i in range(17, 1, -1):
            left ^= p[i]
            right ^= self._f(left)
            left, right = right, left
        left, right = right, left
        return left ^ p[0], right ^ p[1]

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray()
        for off in range(0, len(data) - len(data) % 8, 8):
            left, right = struct.unpack_from("<II", data, off)
            out += struct.pack("<II", *self._encipher(left, right))
        return bytes(out)

    def decrypt(self, data: bytes) -> bytes:
        out = bytearray()
        for off in range(0, len(data) - len(data) % 8, 8):
            left, right = struct.unpack_from("<II", data, off)
            out += struct.pack("<II", *self._decipher(left, right))
        return bytes(out)


def _pk2_key() -> bytes:
    return bytes(b ^ PK2_SALT[i] for i, b in enumerate(PK2_BASE_KEY))


def read_root_files(pk2: Path, wanted: set) -> dict:
    """{UPPERCASE name: bytes} for the wanted files in the pk2's ROOT folder."""
    with open(pk2, "rb") as fh:
        header = fh.read(PK2_HEADER)
        if len(header) < PK2_HEADER or not header.startswith(b"JoyMax File Manager!"):
            raise ValueError("not a pk2 archive")
        encrypted = header[34] != 0
        bf = None
        if encrypted:
            bf = Blowfish(_pk2_key())
            # Header checksum: the first 3 bytes of "Joymax Pak File" encrypted
            # with the archive key - a mismatch means a custom key.
            check = bf.encrypt(b"Joymax Pak File\x00")[:3]
            if header[35:38] != check:
                raise ValueError("pk2 uses a custom encryption key")
        found = {}
        block_off, seen = PK2_HEADER, set()
        while block_off and block_off not in seen and len(found) < len(wanted):
            seen.add(block_off)
            fh.seek(block_off)
            block = fh.read(ENTRY_SIZE * ENTRIES_PER_BLOCK)
            if len(block) < ENTRY_SIZE * ENTRIES_PER_BLOCK:
                break
            if bf:
                block = bf.decrypt(block)
            next_block = 0
            for i in range(ENTRIES_PER_BLOCK):
                e = block[i * ENTRY_SIZE:(i + 1) * ENTRY_SIZE]
                etype = e[0]
                name = e[1:82].split(b"\0", 1)[0].decode("latin-1").upper()
                position, size, nxt = struct.unpack_from("<QIQ", e, 106)
                if etype == 2 and name in wanted and name not in found:
                    fh.seek(position)
                    found[name] = fh.read(size)
                if i == ENTRIES_PER_BLOCK - 1:
                    next_block = nxt
            block_off = next_block
        return found


def parse_divisioninfo(data: bytes):
    """(locale, [(division name, [gateway host, ...]), ...])"""
    def string(off):
        (n,) = struct.unpack_from("<I", data, off)
        off += 4
        s = data[off:off + n].decode("latin-1")
        return s, off + n + 1   # + the trailing NUL

    locale, count = data[0], data[1]
    off, divisions = 2, []
    for _ in range(count):
        name, off = string(off)
        gw_count = data[off]
        off += 1
        hosts = []
        for _ in range(gw_count):
            host, off = string(off)
            hosts.append(host)
        divisions.append((name, hosts))
    return locale, divisions


def client_info(folder: Path) -> dict:
    media = next((f for f in folder.iterdir() if f.is_file() and f.name.lower() == "media.pk2"), None)
    if media is None:
        raise FileNotFoundError("no Media.pk2 in %s" % folder)
    files = read_root_files(media, {"DIVISIONINFO.TXT", "GATEPORT.TXT"})
    if "DIVISIONINFO.TXT" not in files:
        raise ValueError("DIVISIONINFO.TXT not found in Media.pk2")
    locale, divisions = parse_divisioninfo(files["DIVISIONINFO.TXT"])
    port = files.get("GATEPORT.TXT", b"").decode("latin-1").strip("\0 \r\n\t")
    return {"locale": locale, "gateport": int(port) if port.isdigit() else None,
            "divisions": divisions}


def main(argv) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: sro-clientinfo.py <client folder>\n")
        return 2
    try:
        info = client_info(Path(argv[1]))
    except (OSError, ValueError, struct.error, IndexError) as e:
        sys.stderr.write("%s\n" % e)
        return 1
    print("locale=%d" % info["locale"])
    if info["gateport"]:
        print("gateport=%d" % info["gateport"])
    for name, hosts in info["divisions"]:
        print("division=%s" % name)
        for h in hosts:
            print("gateway=%s" % h)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
