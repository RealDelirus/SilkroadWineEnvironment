#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Patches Wine's dxdiagn.dll against a crash after login.

Problem: Macro_Client/MaxiGuard makes dxdiagn build the DxDiag SystemInfo tree.
While doing so dxdiagn calls IEnumWbemClassObject::Next() for various WMI classes
(Win32_OperatingSystem/ComputerSystem/Processor/BIOS ...). MaxiGuard obfuscates
the VM/hardware info and makes Wine's wbemprox return 0 objects for one of these
classes (Next -> S_FALSE).

dxdiagn then only checks "if (FAILED(hr))", not "hr != S_OK", and on S_FALSE
dereferences an uninitialized object pointer (stale stack data, the bytes "fixme"
in the trace) -> EXCEPTION_ACCESS_VIOLATION in _build_systeminfo_tree; the client
writes a minidump and exits.

Fix: at the 5 HRESULT checks in _build_systeminfo_tree change the jump
"js cleanup" (jumps only on error, HRESULT < 0) to "jne cleanup" (also jumps on
S_FALSE = 1). Opcode 0F 88 -> 0F 85, same length, same target. In the healthy
case Next returns S_OK (0), no jump, behavior unchanged (standalone test:
SystemInfo with 39 properties, no crash). Only the 0-objects case produced by
MaxiGuard now bails out cleanly and returns S_FALSE instead of crashing.

Usage:  ./patch-dxdiagn.py <proton-dir> [--check]
"""
import os, sys

# The 5 js sites in _build_systeminfo_tree, each as a unique 6-byte sequence
# 0F 88 <rel32>. After the patch: 0F 85 <rel32> (jne).
SITES = [
    bytes.fromhex("0f88d1070000"),
    bytes.fromhex("0f8833070000"),
    bytes.fromhex("0f88bb060000"),
    bytes.fromhex("0f8861060000"),
    bytes.fromhex("0f8877050000"),
]

PE_DIRS = ("files/lib/wine/i386-windows", "files/lib/wine/x86_64-windows")


def patched(seq):
    return b"\x0f\x85" + seq[2:]


def process(path, check_only):
    data = open(path, "rb").read()
    done = 0
    todo = 0
    new = bytearray(data)
    for seq in SITES:
        n_orig = data.count(seq)
        n_patched = data.count(patched(seq))
        if n_orig == 1:
            if not check_only:
                i = new.find(seq)
                new[i:i + 6] = patched(seq)
            todo += 1
        elif n_patched == 1 and n_orig == 0:
            done += 1
        elif n_orig == 0 and n_patched == 0:
            # sequence not present (different dxdiagn version) -> skip
            pass
        else:
            print(f"  ! {os.path.basename(path)}: {seq.hex()} ambiguous "
                  f"(orig={n_orig}, patched={n_patched}) - skipped")
    if todo and not check_only:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | 0o200)
        with open(path, "wb") as fh:
            fh.write(new)
        os.chmod(path, mode)
    return todo, done


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    check = "--check" in sys.argv
    any_file = False
    for sub in PE_DIRS:
        p = os.path.join(root, sub, "dxdiagn.dll")
        if not os.path.isfile(p):
            continue
        any_file = True
        todo, done = process(p, check)
        if check:
            print(f"  {sub.split('/')[-1]:16s} dxdiagn.dll: "
                  f"{todo} to patch, {done} already ok")
        else:
            print(f"  {sub.split('/')[-1]:16s} dxdiagn.dll: {todo} jumps patched"
                  + (f", {done} were already ok" if done else ""))
    if not any_file:
        print("  ! no dxdiagn.dll found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
