/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * WUDFPlatform.dll - anti-VM shim for the MaxiGuard/Silkroad client under Wine.
 *
 * MaxiGuard.dll (VMProtect) calls, right before its check,
 * EnumSystemFirmwareTables('ACPI') / GetSystemFirmwareTable('ACPI', ...).
 * Wine does not implement these APIs (fixme:ntdll:enum_firmware_info) and
 * returns 0 tables -> "Virtual Machine detected!".
 *
 * The client loads WUDFPlatform.dll from the game folder right before this
 * check (on real Windows it lives in system32; under Wine it does not exist
 * there, so the copy in the game folder is used). This DLL uses that as an
 * entry point and replaces the two firmware APIs with an implementation that
 * reports the REAL ACPI table list of this machine.
 *
 * Portable: the table list is read at runtime from /sys/firmware/acpi/tables
 * (via Wine's Z: drive = Linux root). No machine-specific hardcoded state. If
 * reading fails, a plausible default list of a real desktop board is used.
 */
#include <windows.h>
#include <string.h>
#include <stdio.h>

#define PROV_ACPI  0x41435049u   /* 'ACPI' */
#define SYSTEM_FIRMWARE_TABLE_INFORMATION_CLASS 76

typedef LONG NTSTATUS;
typedef NTSTATUS (WINAPI *PFN_NTQSI)(ULONG, PVOID, ULONG, PULONG);
static PFN_NTQSI pNtQuerySystemInformation;

/* Plausible ACPI tables of a real AMI/desktop BIOS (fallback) */
static const char FALLBACK_TABLES[][5] = {
    "APIC","BGRT","DSDT","FACP","FACS","FIDT","FPDT","HPET","MCFG",
    "SSDT","SSDT","SSDT","TPM2","WSMT",
};

#define MAX_TABLES 128
static char  g_tables[MAX_TABLES][5];
static int   g_ntables = 0;

static void log_line(const char *s)
{
    HANDLE h = CreateFileA("wudfshim.log", FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD w; WriteFile(h, s, (DWORD)strlen(s), &w, NULL);
        WriteFile(h, "\r\n", 2, &w, NULL); CloseHandle(h);
    }
}

/* Read /sys/firmware/acpi/tables via Z:\. File names = 4-character signatures
 * (DSDT, SSDT1, ...). Truncate to 4 characters, duplicates allowed. */
static void load_acpi_tables(void)
{
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA("Z:\\sys\\firmware\\acpi\\tables\\*", &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
            char sig[5] = {0};
            int n = 0;
            for (int i = 0; i < 4 && fd.cFileName[i]; i++) {
                char c = fd.cFileName[i];
                if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) sig[n++] = c;
                else break;
            }
            if (n == 4 && g_ntables < MAX_TABLES) {
                memcpy(g_tables[g_ntables++], sig, 5);
            }
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    if (g_ntables == 0) {
        int n = (int)(sizeof(FALLBACK_TABLES) / sizeof(FALLBACK_TABLES[0]));
        for (int i = 0; i < n; i++) memcpy(g_tables[g_ntables++], FALLBACK_TABLES[i], 5);
        log_line("ACPI tables: fallback list used (Z:\\sys not readable)");
    } else {
        char msg[64]; wsprintfA(msg, "ACPI tables read: %d", g_ntables); log_line(msg);
    }
}

static UINT WINAPI my_EnumSystemFirmwareTables(DWORD prov, PVOID buf, DWORD size)
{
    if (prov == PROV_ACPI) {
        DWORD need = (DWORD)g_ntables * 4;
        if (!buf || size < need) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return need; }
        for (int i = 0; i < g_ntables; i++) memcpy((char *)buf + 4 * i, g_tables[i], 4);
        SetLastError(0);
        return need;
    }
    {   /* pass RSMB/FIRM through to ntdll unchanged */
        struct { ULONG sig, act, id, len; UCHAR buf[1]; } *info;
        ULONG total = 16 + size, ret = 0; UINT out = 0;
        info = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, total);
        if (!info) return 0;
        info->sig = prov; info->act = 0; info->len = size;
        if (pNtQuerySystemInformation &&
            pNtQuerySystemInformation(SYSTEM_FIRMWARE_TABLE_INFORMATION_CLASS, info, total, &ret) >= 0) {
            out = info->len; if (buf && size >= out) memcpy(buf, info->buf, out);
        }
        HeapFree(GetProcessHeap(), 0, info);
        return out;
    }
}

static UINT WINAPI my_GetSystemFirmwareTable(DWORD prov, DWORD id, PVOID buf, DWORD size)
{
    if (prov == PROV_ACPI) {
        /* Minimal, plausible 36-byte ACPI header. The actual table content
         * lives under /sys/firmware/acpi/tables and is root-only readable on
         * Linux; MaxiGuard primarily checks existence and OEM id - not a VM
         * signature like "VBOX__"/"VMWARE". */
        unsigned char hdr[36]; memset(hdr, 0, sizeof(hdr));
        memcpy(hdr + 0, &id, 4);            /* Signature   */
        *(DWORD *)(hdr + 4) = sizeof(hdr);  /* Length      */
        hdr[8] = 2;                         /* Revision    */
        memcpy(hdr + 10, "ALASKA", 6);      /* OEMID       */
        memcpy(hdr + 16, "A M I ", 6);      /* OEMTableID  */
        *(DWORD *)(hdr + 24) = 0x01072009;  /* OEMRevision */
        memcpy(hdr + 28, "AMI ", 4);        /* CreatorID   */
        *(DWORD *)(hdr + 32) = 0x00010013;
        { unsigned char s = 0; for (unsigned i = 0; i < sizeof(hdr); i++) s = (unsigned char)(s + hdr[i]);
          hdr[9] = (unsigned char)(0u - s); }   /* checksum -> 0 */
        if (!buf || size < sizeof(hdr)) return sizeof(hdr);
        memcpy(buf, hdr, sizeof(hdr));
        return sizeof(hdr);
    }
    {
        struct { ULONG sig, act, id, len; UCHAR buf[1]; } *info;
        ULONG total = 16 + size, ret = 0; UINT out = 0;
        info = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, total);
        if (!info) return 0;
        info->sig = prov; info->act = 1; info->id = id; info->len = size;
        if (pNtQuerySystemInformation &&
            pNtQuerySystemInformation(SYSTEM_FIRMWARE_TABLE_INFORMATION_CLASS, info, total, &ret) >= 0) {
            out = info->len; if (buf && size >= out) memcpy(buf, info->buf, out);
        }
        HeapFree(GetProcessHeap(), 0, info);
        return out;
    }
}

/* place a 5-byte jmp at the start of the original function (inline hook) */
static void hook(const char *dll, const char *fn, void *repl)
{
    HMODULE m = GetModuleHandleA(dll);
    BYTE *t = m ? (BYTE *)GetProcAddress(m, fn) : NULL;
    DWORD old; char msg[256];
    if (!t || t[0] == 0xE9) return;
    if (!VirtualProtect(t, 5, PAGE_EXECUTE_READWRITE, &old)) return;
    t[0] = 0xE9; *(DWORD *)(t + 1) = (DWORD)((BYTE *)repl - (t + 5));
    VirtualProtect(t, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), t, 5);
    wsprintfA(msg, "hook: %s!%s @ %p", dll, fn, t); log_line(msg);
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved)
{
    (void)h; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        pNtQuerySystemInformation = (PFN_NTQSI)GetProcAddress(GetModuleHandleA("ntdll.dll"),
                                                              "NtQuerySystemInformation");
        log_line("--- WUDFPlatform shim loaded ---");
        load_acpi_tables();
        hook("kernelbase.dll", "EnumSystemFirmwareTables", (void *)my_EnumSystemFirmwareTables);
        hook("kernelbase.dll", "GetSystemFirmwareTable",   (void *)my_GetSystemFirmwareTable);
        hook("kernel32.dll",   "EnumSystemFirmwareTables", (void *)my_EnumSystemFirmwareTables);
        hook("kernel32.dll",   "GetSystemFirmwareTable",   (void *)my_GetSystemFirmwareTable);
    }
    return TRUE;
}
