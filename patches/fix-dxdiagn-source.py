#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
fix-dxdiagn-source.py <wine-source-root>

Source fix for the MaxiGuard login crash. REPLACES the earlier binary js->jne
patch (patch-dxdiagn.py), whose fixed byte offsets only matched one particular
dxdiagn build.

Bug (in dlls/dxdiagn/provider.c, around build_systeminfo_tree, as of Wine 11.15):

    hr = IEnumWbemClassObject_Next(wbem_enum, 1000, 1, &wbem_class, &no);
    IEnumWbemClassObject_Release(wbem_enum);
    if(FAILED(hr))
        return hr;
    hr = IWbemClassObject_Get(wbem_class, ...);   <-- wbem_class used here

MaxiGuard obfuscates the WMI hardware data so that Next() returns 0 objects
(hr = S_FALSE, no = 0). S_FALSE is NOT FAILED, so the code keeps running and
dereferences the never-set wbem_class -> access violation; the client writes a
minidump and exits.

Fix: change the check right after Next()/Release from `if(FAILED(hr))` to
`if(hr != S_OK)` - then the 0-objects case bails out cleanly (like the old
js->jne patch, but at source level and thus compiler-independent).

Idempotent. Call with the extracted Wine source directory.
"""
import sys, os

ANCHOR   = "    IEnumWbemClassObject_Release(wbem_enum);\n"
OLD      = ANCHOR + "    if(FAILED(hr))\n"
NEW      = ANCHOR + "    if(hr != S_OK) /* SRO fix: cleanly handle S_FALSE (0 objects) */\n"
APPLIED  = ANCHOR + "    if(hr != S_OK)"   # marker regardless of the trailing comment


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    p = os.path.join(root, "dlls", "dxdiagn", "provider.c")
    if not os.path.isfile(p):
        print(f"  ! {p} not found - dxdiagn fix skipped")
        return 0
    data = open(p, encoding="utf-8").read()

    if APPLIED in data:
        print("  dxdiagn source fix already applied.")
        return 0
    if OLD not in data:
        # anchor not found: different Wine version. Do not patch incorrectly.
        print("  ! dxdiagn pattern not found (different Wine version?) - "
              "fix NOT applied, MaxiGuard clients might crash after login.")
        return 0
    if data.count(OLD) != 1:
        print(f"  ! dxdiagn pattern found {data.count(OLD)}x (ambiguous) - fix NOT applied.")
        return 0

    open(p, "w", encoding="utf-8").write(data.replace(OLD, NEW, 1))
    print("  dxdiagn source fix applied (provider.c: FAILED(hr) -> hr != S_OK).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
