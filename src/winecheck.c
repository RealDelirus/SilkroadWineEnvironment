/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <windows.h>
#include <stdio.h>

static const char *names[] = { "wine_get_version", "wine_get_build_id", "wine_get_host_version", "wine_server_call", "__wine_dbg_output" };

/* walk the export table of the mapped image directly, like VMProtect does */
static int manual_export_lookup(HMODULE mod, const char *want)
{
    BYTE *base = (BYTE *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    DWORD rva = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT].VirtualAddress;
    if (!rva) return 0;
    IMAGE_EXPORT_DIRECTORY *exp = (IMAGE_EXPORT_DIRECTORY *)(base + rva);
    DWORD *nameRvas = (DWORD *)(base + exp->AddressOfNames);
    for (DWORD i = 0; i < exp->NumberOfNames; i++)
        if (!strcmp((char *)(base + nameRvas[i]), want)) return 1;
    return 0;
}

int main(void)
{
    freopen("C:\\winecheck.txt", "w", stdout);
    HMODULE ntdll = GetModuleHandleA("ntdll.dll");
    printf("=== ntdll export checks (GetProcAddress / manual PE walk) ===\n");
    for (int i = 0; i < sizeof(names)/sizeof(names[0]); i++) {
        void *p = GetProcAddress(ntdll, names[i]);
        int m = manual_export_lookup(ntdll, names[i]);
        printf("  %-24s GetProcAddress: %-8s  export table: %s\n",
               names[i], p ? "FOUND" : "hidden", m ? "FOUND" : "hidden");
    }

    printf("\n=== other giveaways ===\n");
    printf("  kernel32!wine_get_unix_file_name : %s\n",
           GetProcAddress(GetModuleHandleA("kernel32.dll"), "wine_get_unix_file_name") ? "FOUND" : "hidden");
    printf("  GetModuleHandle(\"wined3d.dll\")    : %s\n", GetModuleHandleA("wined3d.dll") ? "LOADED" : "not loaded");

    HKEY k;
    char buf[256]; DWORD sz = sizeof(buf);
    if (!RegOpenKeyExA(HKEY_LOCAL_MACHINE, "Software\\Wine", 0, KEY_READ, &k)) {
        printf("  HKLM\\Software\\Wine                : PRESENT\n"); RegCloseKey(k);
    } else printf("  HKLM\\Software\\Wine                : absent\n");

    sz = sizeof(buf);
    if (!RegGetValueA(HKEY_LOCAL_MACHINE, "Software\\Microsoft\\Windows NT\\CurrentVersion", "CurrentBuild", RRF_RT_REG_SZ, NULL, buf, &sz))
        printf("  Windows build reported            : %s\n", buf);

    printf("\n  IsWow64 / arch: %u-bit test binary\n", (unsigned)(sizeof(void*) * 8));
    return 0;
}
