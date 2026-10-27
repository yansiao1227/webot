#ifndef __USE_MINGW_ANSI_STDIO
#define __USE_MINGW_ANSI_STDIO 1
#endif
#include <windows.h>
#include <bcrypt.h>
#include <psapi.h>
#include <stdio.h>
#include <string.h>
#include <io.h>
#include <fcntl.h>
#include "MinHook.h"
#include "weixin41930.h"
#include "stream_snapshot.h"
#include "runtime_protocol.h"
#include "runtime_pins.h"


static uintptr_t native_base;
static BOOL exact(uintptr_t at, void *out, SIZE_T count) {
    SIZE_T read = 0;
    return ReadProcessMemory(GetCurrentProcess(), (void *)at, out, count, &read) && read == count;
}

static BOOL account_identity(char result[129]) {
#ifdef CH_FIXTURE
    volatile LONG *logged=(volatile LONG *)(uintptr_t)GetProcAddress((HMODULE)native_base,"fixture_logged_in");
    if (!logged || InterlockedCompareExchange(logged,0,0)!=1) return FALSE;
    strcpy(result, "wxid_fixture_self");
    return TRUE;
#else
    uintptr_t root, root_again, root_vt, object_vt, pair[2], pair_again[2], getter, data;
    unsigned char header[32], again[32], code[5];
    const unsigned char expected[5] = {0x48,0x8d,0x41,0x48,0xc3};
    uint64_t length, capacity;
    ULONGLONG start = GetTickCount64();
    if (!exact(native_base+0xa6634b8,&root,8) || root < 0x10000 ||
        !exact(root,&root_vt,8) || root_vt != native_base+0x8040838 ||
        !exact(root+0x68,pair,16) || pair[0] < 0x10000 || !pair[1] ||
        !exact(pair[0],&object_vt,8) || object_vt != native_base+0x80e58e8 ||
        !exact(object_vt+0x18,&getter,8) || getter != native_base+0x3035b0 ||
        !exact(getter,code,5) || memcmp(code,expected,5) ||
        !exact(pair[0]+0x48,header,32)) return FALSE;
    memcpy(&length,header+16,8); memcpy(&capacity,header+24,8);
    if (length < 6 || length > 69 || length > capacity || capacity == UINT64_MAX) return FALSE;
    if (capacity < 16) {
        if (length > 15) return FALSE;
        memcpy(result,header,(size_t)length+1);
    } else {
        memcpy(&data,header,8);
        if (data < 0x10000 || data > 0x00007ffffffeffffULL-length || !exact(data,result,(size_t)length+1)) return FALSE;
    }
    if (result[length] || memcmp(result,"wxid_",5)) return FALSE;
    for (size_t i=5; i<length; ++i) {
        unsigned char c=(unsigned char)result[i];
        if (!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='_'||c=='-')) return FALSE;
    }
    return exact(pair[0]+0x48,again,32) && !memcmp(header,again,32) &&
        exact(native_base+0xa6634b8,&root_again,8) && root_again==root &&
        exact(root+0x68,pair_again,16) && !memcmp(pair,pair_again,16) &&
        exact(pair[0],&object_vt,8) && object_vt==native_base+0x80e58e8 && GetTickCount64()-start<100;
#endif
}

static BOOL hash_file(HANDLE file, const char *expected) {
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    unsigned char buffer[65536], digest[32];
    DWORD read;
    BOOL ok = FALSE;
    char hex[65];
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0 ||
        BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    for (;;) {
        if (!ReadFile(file, buffer, sizeof(buffer), &read, NULL)) goto done;
        if (!read) break;
        if (BCryptHashData(hash, buffer, read, 0) < 0) goto done;
    }
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0) goto done;
    for (unsigned i = 0; i < 32; ++i) sprintf(hex + i * 2, "%02x", digest[i]);
    ok = strcmp(hex, expected) == 0;
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    return ok;
}


#include "runtime_session.inc"
#include "text_send.inc"

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, void *reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
