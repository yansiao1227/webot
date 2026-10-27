#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <psapi.h>
#include <bcrypt.h>
#include <stdint.h>
#include <string.h>
#include <wchar.h>
#include "send_diagnostics.h"
#include "text_send.h"
#include "fixture_abi.h"
#ifdef CH_RESOURCE_SIDECAR
#include "outgoing_identity.h"
#endif

struct profile_entry { DWORD rva; BYTE bytes[16]; };
#include "queue-fixture-profile.h"
#include "queue-real-profile.h"

enum { P_ROOT, P_QUEUE, P_LOCATION, P_POST, P_COPY, P_MOVE, P_INVOKE,
       P_TYPE, P_DELETE, P_QUEUE_CTRL_DESTROY, P_RESULT_CTRL_DESTROY, P_CTRL_DELETE,
       P_CONTEXT1, P_CONTEXT2, P_CONTEXT3, P_FACTORY, P_GROW, P_SUBMIT, P_SIZED_FREE };
struct runtime_profile {
    const wchar_t *path;
    const BYTE *sha256;
    DWORD image_size, fn_vtable_rva, queue_ctrl_vtable_rva, result_ctrl_vtable_rva, function_count, kind;
    const struct profile_entry *functions;
    BOOL release_ready;
    DWORD context_vtable_rva, text_vtable_rva, text_control_rva, expected_tid_rva;
};
static const struct runtime_profile fixture_profile = {
    QUEUE_FIXTURE_MODULE_PATH, queue_fixture_sha256, QUEUE_FIXTURE_IMAGE_SIZE,
    QUEUE_FIXTURE_FN_VTABLE_RVA, QUEUE_FIXTURE_QUEUE_CTRL_VTABLE_RVA, QUEUE_FIXTURE_RESULT_CTRL_VTABLE_RVA,
    19, 1, queue_functions, TRUE, NATIVE_FIXTURE_CONTEXT_VTABLE_RVA,
    NATIVE_FIXTURE_TEXT_VTABLE_RVA, NATIVE_FIXTURE_TEXT_CONTROL_RVA, NATIVE_FIXTURE_EXPECTED_TID_RVA
};
static const struct runtime_profile real_profile = {
    QUEUE_REAL_MODULE_PATH, queue_real_sha256, QUEUE_REAL_IMAGE_SIZE,
    QUEUE_REAL_FN_VTABLE_RVA, QUEUE_REAL_QUEUE_CTRL_VTABLE_RVA, QUEUE_REAL_RESULT_CTRL_VTABLE_RVA,
    19, 2, queue_real_functions, QUEUE_REAL_RELEASE_READY, NATIVE_REAL_CONTEXT_VTABLE_RVA,
    NATIVE_REAL_TEXT_VTABLE_RVA, NATIVE_REAL_TEXT_CONTROL_RVA, 0
};

typedef struct Operation Operation;
typedef struct OwnImpl { const FixtureFnVtable *vtable; Operation *operation; } OwnImpl;
struct Operation {
    char request_id[64], account[128], target[128];
    char native_client_id[129];
    unsigned char fingerprint[32];
    char *body;
    struct Operation *next;
    volatile LONG gate, posted, done, count, callback_tid, consumed;
    volatile LONG queue_released, result_released, release_ok, failure;
    DWORD code;
    bridge_config result;
    FixtureFn64 inner;
    OwnImpl own_impl;
    FixtureSharedRef submit_input;
    FixtureSubmitResult submit_out;
#ifdef CH_RESOURCE_SIDECAR
    void *resource;
#endif
#ifdef CH_SEND_TASK_OBSERVER
    void *task_observation;
#endif
};
static Operation prepare_operation;
static Operation *operations;
static unsigned operation_count;
static SRWLOCK send_gate = SRWLOCK_INIT;
static const char location_file[] = "client_hook/text_send";
static const char location_function[] = "dispatch_text";
static const struct runtime_profile *active_profile;
static uintptr_t active_base;
static DWORD expected_tid;
static const BYTE *disk_bytes;
static SIZE_T disk_size;
static HANDLE disk_file, disk_mapping;
static void inner_invoke(FixtureFnImpl *self);
static void work_native(Operation *operation);
#ifdef CH_RESOURCE_SIDECAR
static void work_resource(Operation *operation);
#endif
/* Only Invoke(+0x10) may ever be reached. Never hand this inner to client ownership. */
static const FixtureFnVtable own_invoke_only_vtable = { NULL, NULL, inner_invoke, NULL, NULL };

static LONG atomic_read(volatile LONG *value) { return InterlockedCompareExchange(value, 0, 0); }
static void inner_invoke(FixtureFnImpl *self) {
    Operation *operation = ((OwnImpl *)self)->operation;
    InterlockedExchange(&operation->callback_tid, (LONG)GetCurrentThreadId());
    if (InterlockedIncrement(&operation->count)!=1) return;
    if (!expected_tid && operation->code == BRIDGE_OP_PREPARE) expected_tid=GetCurrentThreadId();
    char identity[129];
    if (!account_identity(identity) || strcmp(identity,operation->account)) {
        operation->result.callback_error=ERROR_NOT_LOGGED_ON;
    } else if (GetCurrentThreadId() != expected_tid) {
        operation->result.callback_error = ERROR_INVALID_THREAD_ID;
        operation->result.error_phase = 1;
    } else work_native(operation);
    InterlockedExchange(&operation->done, 1);
}

static BOOL read_self(const void *source, void *dest, size_t size) {
    SIZE_T got = 0;
    return ReadProcessMemory(GetCurrentProcess(), source, dest, size, &got) && got == size;
}
#ifdef CH_SEND_TASK_OBSERVER
#include "task_observer.inc"
#endif

static BOOL input_fingerprint(const ChSendRpc *rpc,unsigned char digest[32]) {
    BCRYPT_ALG_HANDLE algorithm=NULL;
    BCRYPT_HASH_HANDLE hash=NULL;
    BOOL ok=FALSE;
    if (BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0)<0 ||
        BCryptCreateHash(algorithm,&hash,NULL,0,NULL,0,0)<0) goto done;
    const char *parts[]={rpc->account,rpc->target,rpc->body};
    for (unsigned i=0;i<3;++i)
        if (BCryptHashData(hash,(PUCHAR)parts[i],(ULONG)strlen(parts[i])+1,0)<0) goto done;
    ok=BCryptFinishHash(hash,digest,32,0)>=0;
done:
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    return ok;
}

static BOOL hash_module(HMODULE module, const struct runtime_profile *profile, BOOL retain) {
    wchar_t path[32768], canonical[32768];
    HANDLE file = INVALID_HANDLE_VALUE, mapping = NULL;
    const BYTE *bytes = NULL;
    LARGE_INTEGER length;
    BCRYPT_ALG_HANDLE algorithm = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    BYTE digest[32];
    BOOL ok = FALSE;
    DWORD size;
    if (!GetModuleFileNameW(module, path, 32768)) goto done;
    file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) goto done;
    size = GetFinalPathNameByHandleW(file, canonical, 32768, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!size || size >= 32768) goto done;
    if (wcsncmp(canonical, L"\\\\?\\", 4) == 0 && canonical[5] == L':')
        memmove(canonical, canonical + 4, (wcslen(canonical + 4) + 1) * sizeof(wchar_t));
    if (!GetFileSizeEx(file, &length) || length.QuadPart <= 0 || length.QuadPart > UINT32_MAX) goto done;
    mapping = CreateFileMappingW(file, NULL, PAGE_READONLY, 0, 0, NULL);
    if (!mapping) goto done;
    bytes = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
    if (!bytes || BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0 ||
        BCryptCreateHash(algorithm, &hash, NULL, 0, NULL, 0, 0) < 0 ||
        BCryptHashData(hash, (PUCHAR)bytes, (ULONG)length.QuadPart, 0) < 0 ||
        BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0 ||
        memcmp(digest, profile->sha256, sizeof(digest))) goto done;
    ok = TRUE;
    if (retain && disk_bytes) {
        /* Existing retained mapping is tied to the same verified module hash. */
    } else if (retain) {
        disk_file = file; disk_mapping = mapping; disk_bytes = bytes; disk_size = (SIZE_T)length.QuadPart;
        file = INVALID_HANDLE_VALUE; mapping = NULL; bytes = NULL;
    }
done:
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    if (bytes) UnmapViewOfFile(bytes);
    if (mapping) CloseHandle(mapping);
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    return ok;
}

static BOOL code_matches(uintptr_t base, DWORD image_size, const struct profile_entry *entry) {
    MEMORY_BASIC_INFORMATION info;
    BYTE observed[16];
    DWORD protection;
    uintptr_t address;
    if (entry->rva >= image_size || sizeof(observed) > image_size - entry->rva) return FALSE;
    address = base + entry->rva;
    if (!VirtualQuery((LPCVOID)address, &info, sizeof(info))) return FALSE;
    protection = info.Protect & 0xff;
    return info.State == MEM_COMMIT && info.Type == MEM_IMAGE &&
        (uintptr_t)info.AllocationBase == base && !(info.Protect & (PAGE_GUARD | PAGE_NOACCESS)) &&
        (protection == PAGE_EXECUTE || protection == PAGE_EXECUTE_READ ||
         protection == PAGE_EXECUTE_READWRITE || protection == PAGE_EXECUTE_WRITECOPY) &&
        read_self((LPCVOID)address, observed, sizeof(observed)) &&
        memcmp(observed, entry->bytes, sizeof(observed)) == 0;
}

static BOOL readonly_image_range(uintptr_t base, DWORD image_size, DWORD rva, SIZE_T length) {
    MEMORY_BASIC_INFORMATION memory;
    uintptr_t address;
    if (rva >= image_size || length > image_size - rva) return FALSE;
    address = base + rva;
    return VirtualQuery((LPCVOID)address, &memory, sizeof(memory)) && memory.State == MEM_COMMIT &&
        memory.Type == MEM_IMAGE && (uintptr_t)memory.AllocationBase == base &&
        (memory.Protect & 0xff) == PAGE_READONLY && !(memory.Protect & (PAGE_GUARD | PAGE_NOACCESS)) &&
        address >= (uintptr_t)memory.BaseAddress &&
        length <= memory.RegionSize - (address - (uintptr_t)memory.BaseAddress);
}

static BOOL profile_matches(HMODULE module, MODULEINFO *info, const struct runtime_profile *profile) {
    uintptr_t base = (uintptr_t)module;
    uintptr_t native[5], control[2];
    unsigned int index;
    if (!GetModuleInformation(GetCurrentProcess(), module, info, sizeof(*info)) ||
        info->SizeOfImage != profile->image_size || !hash_module(module, profile, FALSE) ||
        !readonly_image_range(base, info->SizeOfImage, profile->fn_vtable_rva, sizeof(native))) return FALSE;
    for (index = 0; index < profile->function_count; ++index)
        if (!code_matches(base, info->SizeOfImage, &profile->functions[index])) return FALSE;
    if (!read_self((LPCVOID)(base + profile->fn_vtable_rva), native, sizeof(native))) return FALSE;
    for (index = 0; index < 5; ++index)
        if (native[index] != base + profile->functions[P_COPY + index].rva) return FALSE;
    if (!readonly_image_range(base, info->SizeOfImage, profile->queue_ctrl_vtable_rva, sizeof(control)) ||
        !read_self((LPCVOID)(base + profile->queue_ctrl_vtable_rva), control, sizeof(control)) ||
        control[0] != base + profile->functions[P_QUEUE_CTRL_DESTROY].rva ||
        control[1] != base + profile->functions[P_CTRL_DELETE].rva) return FALSE;
    return readonly_image_range(base, info->SizeOfImage, profile->result_ctrl_vtable_rva, sizeof(control)) &&
        read_self((LPCVOID)(base + profile->result_ctrl_vtable_rva), control, sizeof(control)) &&
        control[0] == base + profile->functions[P_RESULT_CTRL_DESTROY].rva &&
        control[1] == base + profile->functions[P_CTRL_DELETE].rva;
}

static BOOL release_ref(FixtureSharedRef *ref, uintptr_t base, const struct runtime_profile *profile, BOOL result_ref) {
    struct { uintptr_t vtable; LONG strong, weak; } header;
    MEMORY_BASIC_INFORMATION memory;
    FixtureCtrl *control = ref->control;
    FixtureCtrlVtable vtable;
    uintptr_t slots[2];
    DWORD expected_vtable = result_ref ? profile->result_ctrl_vtable_rva : profile->queue_ctrl_vtable_rva;
    unsigned int destroy_index = result_ref ? P_RESULT_CTRL_DESTROY : P_QUEUE_CTRL_DESTROY;
    if (!control) { ref->object = NULL; return TRUE; }
    if (!profile->release_ready ||
        !read_self(control, &header, sizeof(header)) ||
        header.vtable != base + expected_vtable || header.strong <= 0 ||
        header.strong > 1000000 || header.weak <= 0 || header.weak > 1000000 ||
        !readonly_image_range(base, profile->image_size, expected_vtable, sizeof(slots)) ||
        !read_self((LPCVOID)header.vtable, slots, sizeof(slots)) ||
        slots[0] != base + profile->functions[destroy_index].rva ||
        slots[1] != base + profile->functions[P_CTRL_DELETE].rva ||
        !code_matches(base, profile->image_size, &profile->functions[destroy_index]) ||
        !code_matches(base, profile->image_size, &profile->functions[P_CTRL_DELETE]) ||
        !read_self((LPCVOID)header.vtable, &vtable, sizeof(vtable)) ||
        !VirtualQuery(control, &memory, sizeof(memory)) || memory.State != MEM_COMMIT ||
        (uintptr_t)control < (uintptr_t)memory.BaseAddress ||
        sizeof(header) > memory.RegionSize - ((uintptr_t)control - (uintptr_t)memory.BaseAddress) ||
        (memory.Protect & (PAGE_GUARD | PAGE_NOACCESS)) ||
        !((memory.Protect & 0xff) == PAGE_READWRITE || (memory.Protect & 0xff) == PAGE_WRITECOPY ||
          (memory.Protect & 0xff) == PAGE_EXECUTE_READWRITE || (memory.Protect & 0xff) == PAGE_EXECUTE_WRITECOPY)) return FALSE;
    ref->object = NULL; ref->control = NULL;
    if (InterlockedDecrement(&control->strong) == 0) {
        vtable.destroy(control);
        if (InterlockedDecrement(&control->weak) == 0) vtable.delete_this(control);
    }
    return TRUE;
}

/* Only compare bytes inside the already SHA-256 verified, retained file mapping. */
static const BYTE *disk_rva(DWORD rva, SIZE_T length) {
    const IMAGE_DOS_HEADER *dos;
    const IMAGE_NT_HEADERS64 *nt;
    const IMAGE_SECTION_HEADER *sections;
    unsigned int index;
    if (!disk_bytes || disk_size < sizeof(*dos)) return NULL;
    dos = (const IMAGE_DOS_HEADER *)disk_bytes;
    if (dos->e_lfanew < 0 || (SIZE_T)dos->e_lfanew > disk_size ||
        sizeof(*nt) > disk_size - (SIZE_T)dos->e_lfanew) return NULL;
    nt = (const IMAGE_NT_HEADERS64 *)(disk_bytes + dos->e_lfanew);
    sections = IMAGE_FIRST_SECTION(nt);
    if ((const BYTE *)sections < disk_bytes || (SIZE_T)((const BYTE *)sections - disk_bytes) > disk_size ||
        nt->FileHeader.NumberOfSections * sizeof(*sections) > disk_size - ((const BYTE *)sections - disk_bytes)) return NULL;
    for (index = 0; index < nt->FileHeader.NumberOfSections; ++index) {
        const IMAGE_SECTION_HEADER *s = sections + index;
        SIZE_T delta, offset;
        if (rva < s->VirtualAddress) continue;
        delta = rva - s->VirtualAddress;
        if (delta > s->SizeOfRawData || length > s->SizeOfRawData - delta) continue;
        offset = (SIZE_T)s->PointerToRawData + delta;
        if (offset <= disk_size && length <= disk_size - offset) return disk_bytes + offset;
    }
    return NULL;
}

static BOOL dynamic_code(uintptr_t address) {
    struct profile_entry entry;
    const BYTE *source;
    if (address < active_base || address - active_base > UINT32_MAX) return FALSE;
    entry.rva = (DWORD)(address - active_base);
    source = disk_rva(entry.rva, sizeof(entry.bytes));
    if (!source) return FALSE;
    memcpy(entry.bytes, source, sizeof(entry.bytes));
    return code_matches(active_base, active_profile->image_size, &entry);
}

static BOOL native_vtable(uintptr_t vtable, uintptr_t slots[3]) {
    SIZE_T count_slots = active_profile->kind == 1 ? 2 : 3;
    unsigned int index;
    if (vtable < active_base || vtable - active_base > UINT32_MAX ||
        !readonly_image_range(active_base, active_profile->image_size, (DWORD)(vtable - active_base), count_slots * 8) ||
        !read_self((LPCVOID)vtable, slots, count_slots * 8)) return FALSE;
    for (index = 0; index < count_slots; ++index) if (!dynamic_code(slots[index])) return FALSE;
    return TRUE;
}

static BOOL owned_control(FixtureSharedRef *ref, DWORD expected_vtable) {
    struct { uintptr_t vtable; LONG strong, weak; } header;
    MEMORY_BASIC_INFORMATION memory;
    uintptr_t slots[3];
    if (!ref->object || !ref->control || !read_self(ref->control, &header, sizeof(header)) ||
        (expected_vtable && header.vtable != active_base + expected_vtable) ||
        header.strong <= 0 || header.strong > 1000000 || header.weak <= 0 || header.weak > 1000000 ||
        !native_vtable(header.vtable, slots) || !VirtualQuery(ref->control, &memory, sizeof(memory)) ||
        memory.State != MEM_COMMIT || (memory.Protect & (PAGE_GUARD | PAGE_NOACCESS)) ||
        (memory.Protect & 0xff) != PAGE_READWRITE || (uintptr_t)ref->control < (uintptr_t)memory.BaseAddress ||
        sizeof(header) > memory.RegionSize - ((uintptr_t)ref->control - (uintptr_t)memory.BaseAddress)) return FALSE;
    return TRUE;
}

static BOOL release_native(FixtureSharedRef *ref, DWORD expected_vtable) {
    FixtureCtrl *control;
    uintptr_t vtable, slots[3];
    void (*destroy)(FixtureCtrl *), (*delete_control)(FixtureCtrl *);
    if (!ref->control && !ref->object) return TRUE;
    if (!owned_control(ref, expected_vtable)) return FALSE;
    control = ref->control; ref->object = NULL; ref->control = NULL;
    if (InterlockedDecrement(&control->strong) != 0) return TRUE;
    if (!read_self(control, &vtable, sizeof(vtable)) || !native_vtable(vtable, slots)) return FALSE;
    memcpy(&destroy, &slots[0], sizeof(destroy)); destroy(control);
    /* Native callers reread the control table after object destruction. */
    if (!read_self(control, &vtable, sizeof(vtable)) || !native_vtable(vtable, slots)) return FALSE;
    if (InterlockedDecrement(&control->weak) != 0) return TRUE;
    if (!read_self(control, &vtable, sizeof(vtable)) || !native_vtable(vtable, slots)) return FALSE;
    memcpy(&delete_control, &slots[1], sizeof(delete_control)); delete_control(control);
    return TRUE;
}

static BOOL write_self(void *address, const void *source, SIZE_T length) {
    SIZE_T written = 0;
    return WriteProcessMemory(GetCurrentProcess(), address, source, length, &written) && written == length;
}

static BOOL cache_metadata(void *manager, Operation *operation) {
    uintptr_t state = 0, state_again = 0, links[2];
    uint64_t header[2], header_again[2];
    /* Empty case is exactly 64 requested bytes; no keys or messages are read. */
    operation->result.cache_checked = 0; operation->result.cache_empty = 0;
    operation->result.cache_size = 0;
    if (!read_self((BYTE *)manager + 0xB78, &state, 8) || !state ||
        !read_self((LPCVOID)(state + 0x98), header, 16) || !header[0]) return FALSE;
    operation->result.cache_checked = 1; operation->result.cache_size = header[1];
    if (header[1] != 0) { operation->result.cache_empty = 0; return TRUE; }
    if (!read_self((LPCVOID)(uintptr_t)header[0], links, 16) ||
        !read_self((LPCVOID)(state + 0x98), header_again, 16) ||
        !read_self((BYTE *)manager + 0xB78, &state_again, 8) || state != state_again ||
        memcmp(header, header_again, sizeof(header)) || links[0] != header[0] || links[1] != header[0]) return FALSE;
    operation->result.cache_empty = 1; return TRUE;
}

static BOOL clean_out(Operation *operation) {
    FixtureSharedRef payload;
    FixtureString32 string;
    FixtureSizedFreeFn sized_free;
    uintptr_t address = active_base + active_profile->functions[P_SIZED_FREE].rva;
    void *allocation;
    size_t allocation_size;
    BYTE *out = operation->submit_out.bytes;
    if (!read_self(out + 0x28, &payload, 16) ||
        !read_self(out + 8, &string, 32) || string.size > string.capacity ||
        string.capacity > UINT64_C(0x1000000) ||
        (string.capacity < 16 && (string.capacity != 15 || string.size > 15))) return FALSE;
    operation->result.result_string_length = string.size;
    operation->result.result_string_capacity = string.capacity;
    allocation = string.data.heap; allocation_size = (size_t)string.capacity + 1;
    if (string.capacity >= 16) {
        MEMORY_BASIC_INFORMATION memory;
        if (!allocation || !VirtualQuery(allocation, &memory, sizeof(memory)) || memory.State != MEM_COMMIT ||
            (memory.Protect & (PAGE_GUARD | PAGE_NOACCESS))) return FALSE;
        if (allocation_size >= 0x1000) {
            uintptr_t pointer = (uintptr_t)allocation, raw = 0;
            if (pointer < 8 || !read_self((LPCVOID)(pointer - 8), &raw, 8) ||
                raw > pointer - 8 || pointer - 8 - raw >= 0x20) return FALSE;
            allocation = (void *)raw; allocation_size = (size_t)string.capacity + 0x28;
        }
    }
    if (!release_native(&payload, 0)) return FALSE;
    memset(out + 0x28, 0, 16);
    if (string.capacity >= 16) {
        if (!code_matches(active_base, active_profile->image_size, &active_profile->functions[P_SIZED_FREE])) return FALSE;
        memcpy(&sized_free, &address, sizeof(sized_free)); sized_free(allocation, allocation_size);
    }
    memset(&string, 0, sizeof(string)); string.capacity = 15;
    memcpy(out + 8, &string, 32);
    return TRUE;
}

static void work_native(Operation *operation) {
#ifdef CH_RESOURCE_SIDECAR
    if (operation->resource) { work_resource(operation); return; }
#endif
    FixtureGetContext1Fn get1;
    FixtureGetContext2Fn get2;
    FixtureGetContext3Fn get3;
    FixtureTextFactoryFn factory;
    FixtureStringAssignFn grow;
    FixtureSharedRef refs[3] = {{0}, {0}, {0}}, text = {0};
    FixtureString32 key, body, tag, tag_after;
    uintptr_t address, vtable = 0, control_vtable = 0;
    DWORD message_type = 1;
    uint64_t body_size = strlen(operation->body);
    DWORD actual_type = 0, default_kind = 0;
    uint64_t actual_size = 0;
    BOOL fields = FALSE, clean = TRUE;
    DWORD work_error = ERROR_INVALID_DATA;
    unsigned int index;
    operation->result.error_phase = 2;
    address = active_base + active_profile->functions[P_CONTEXT1].rva; memcpy(&get1, &address, sizeof(get1));
    address = active_base + active_profile->functions[P_CONTEXT2].rva; memcpy(&get2, &address, sizeof(get2));
    address = active_base + active_profile->functions[P_CONTEXT3].rva; memcpy(&get3, &address, sizeof(get3));
    address = active_base + active_profile->functions[P_FACTORY].rva; memcpy(&factory, &address, sizeof(factory));
    address = active_base + active_profile->functions[P_GROW].rva; memcpy(&grow, &address, sizeof(grow));
    if (get1(&refs[0]) != &refs[0] || !owned_control(&refs[0], 0)) goto cleanup;
    if (get2(refs[0].object, &refs[1]) != &refs[1] || !owned_control(&refs[1], 0)) goto cleanup;
    if (get3(refs[1].object, &refs[2]) != &refs[2] || !owned_control(&refs[2], 0)) goto cleanup;
    if (!read_self(refs[2].object, &vtable, 8) || vtable != active_base + active_profile->context_vtable_rva ||
        !readonly_image_range(active_base, active_profile->image_size, active_profile->context_vtable_rva, 8)) goto cleanup;
    operation->result.context_vtable_rva = active_profile->context_vtable_rva;
    if (!cache_metadata(refs[2].object, operation)) goto cleanup;
    operation->result.error_phase = 3;
    if (factory(&text) != &text || !owned_control(&text, active_profile->text_control_rva) ||
        !read_self(text.object, &vtable, 8) || vtable != active_base + active_profile->text_vtable_rva ||
        !read_self(text.control, &control_vtable, 8) ||
        !readonly_image_range(active_base, active_profile->image_size, active_profile->text_vtable_rva, 8)) goto cleanup;
    operation->result.factory_vtable_rva = active_profile->text_vtable_rva;
    operation->result.factory_control_vtable_rva = (DWORD)(control_vtable - active_base);
    if (active_profile->kind == 2) {
        uintptr_t dtor;
        if (!read_self((LPCVOID)vtable, &dtor, 8) || !dynamic_code(dtor)) goto cleanup;
    }
    operation->result.error_phase = 4;
    if (!read_self((BYTE *)text.object + 0xB0, &key, 32) || key.size != 0 || key.capacity != 15 ||
        !read_self((BYTE *)text.object + 0x678, &body, 32) || body.size != 0 || body.capacity != 15 ||
        !read_self((BYTE *)text.object + 0x9C, &default_kind, 4) || default_kind != 1 ||
        !read_self((BYTE *)text.object + 0x618, &tag, 32) || tag.size == 0 || tag.size > 128 ||
        tag.capacity < tag.size || tag.capacity > 1024) goto cleanup;
    if (grow((FixtureString32 *)((BYTE *)text.object + 0xB0), strlen(operation->target), 0, operation->target) !=
        (FixtureString32 *)((BYTE *)text.object + 0xB0)) goto cleanup;
    if (grow((FixtureString32 *)((BYTE *)text.object + 0x678), body_size, 0, operation->body) !=
        (FixtureString32 *)((BYTE *)text.object + 0x678)) goto cleanup;
    if (!write_self((BYTE *)text.object + 0xD8, &message_type, 4) ||
        !write_self((BYTE *)text.object + 0x188, &body_size, 8) ||
        !read_self((BYTE *)text.object + 0xB0, &key, 32) ||
        !read_self((BYTE *)text.object + 0x678, &body, 32) ||
        !read_self((BYTE *)text.object + 0xD8, &actual_type, 4) || actual_type != 1 ||
        !read_self((BYTE *)text.object + 0x188, &actual_size, 8) || actual_size != body_size ||
        !read_self((BYTE *)text.object + 0x618, &tag_after, 32) || memcmp(&tag, &tag_after, 32)) goto cleanup;
    {
        char *check=HeapAlloc(GetProcessHeap(),0,65537);
        if (!check) goto cleanup;
        BOOL same=key.size==strlen(operation->target) && key.capacity>=key.size &&
            read_self(key.capacity>=16?(void *)key.data.heap:(void *)&key.data,check,(size_t)key.size+1) &&
            !memcmp(check,operation->target,(size_t)key.size+1) && body.size==body_size && body.capacity>=body.size &&
            read_self(body.capacity>=16?(void *)body.data.heap:(void *)&body.data,check,(size_t)body.size+1) &&
            !memcmp(check,operation->body,(size_t)body.size+1);
        HeapFree(GetProcessHeap(),0,check);
        if (!same) goto cleanup;
    }
    operation->result.key_length = key.size; operation->result.key_capacity = key.capacity;
    operation->result.body_length = body.size; operation->result.body_capacity = body.capacity;
    operation->result.tag_length = tag.size; operation->result.tag_capacity = tag.capacity;
    if (!read_self(tag.capacity>=16?(void *)tag.data.heap:(void *)&tag.data,
                   operation->native_client_id,(size_t)tag.size+1) || operation->native_client_id[tag.size]) goto cleanup;
    operation->result.fields_verified = 1; fields = TRUE;
    work_error = ERROR_SUCCESS;
    if (operation->code == BRIDGE_OP_SEND) {
        FixtureSubmitFn submit;
#if defined(CH_RESOURCE_SIDECAR) || defined(CH_SEND_TASK_OBSERVER)
        uintptr_t submitted_input=(uintptr_t)text.object;
#endif
        operation->result.submit_enabled = 1;
        operation->result.error_phase = 5;
        char identity[129];
        if (!cache_metadata(refs[2].object, operation) || !account_identity(identity) ||
            strcmp(identity,operation->account)) {work_error=ERROR_NOT_LOGGED_ON;goto cleanup;}
        operation->submit_input = text;
        memset(&text, 0, sizeof(text)); /* exact owned unit transfers to wrapper */
        address = active_base + active_profile->functions[P_SUBMIT].rva;
        memcpy(&submit, &address, sizeof(submit));
#ifdef CH_SEND_TASK_OBSERVER
        ChTaskObservation *watch=NULL;
        static const BYTE ctor_entry[16]={0x55,0x41,0x56,0x56,0x57,0x53,0x48,0x81,
                                         0xec,0xb0,0,0,0,0x48,0x8d,0xac};
        if (active_profile->kind==2 && ch_task_observer_install(active_base+0x3552820,
                ctor_entry,active_base+0x874c1e8,active_base+0x8096da8)) {
            /* Resident storage also survives an uncertain native unwind. */
            operation->task_observation=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(ChTaskObservation));
            if (operation->task_observation && ch_task_observe_begin(operation->task_observation,submitted_input))
                watch=operation->task_observation;
        }
#endif
        operation->result.native_submit_invocations = 1;
        void *submitted=submit(refs[2].object, &operation->submit_out, &operation->submit_input);
#ifdef CH_SEND_TASK_OBSERVER
        if (watch) ch_task_observe_end(watch);
#endif
        if (submitted != &operation->submit_out) {
            /* Unknown output construction/ownership: never guess or repeat submit. */
            work_error = ERROR_INVALID_DATA; clean = FALSE; goto cleanup;
        }
        operation->result.input_consumed = 1;
        memset(&operation->submit_input, 0, sizeof(operation->submit_input));
        memcpy(&operation->result.local_status_0, operation->submit_out.bytes, 4);
        memcpy(&operation->result.local_status_1, operation->submit_out.bytes + 4, 4);
        operation->result.local_accepted = !(operation->result.local_status_0 | operation->result.local_status_1);
#ifdef CH_RESOURCE_SIDECAR
        /* Read only on this owner queue. Bind the exact retained TextInput;
           no body/time inference, no extra calls or retained native pointers. */
        if (active_profile->kind==2) {
            operation->result.reserved_v2[1]=ch_outgoing_record110(read_self,active_base,
                (uintptr_t)refs[2].object,submitted_input,operation->target);
            if (operation->result.reserved_v2[1]) operation->result.reserved_v2[0]=0x41930001;
        }
#endif
#ifdef CH_SEND_TASK_OBSERVER
        if (watch && watch->record110) {
            operation->result.reserved_v2[0]=0x41930002;
            operation->result.reserved_v2[1]=watch->record110;
        }
#endif
        operation->result.error_phase = 6;
        operation->result.out_cleaned = clean_out(operation);
        if (!operation->result.out_cleaned) { clean = FALSE; work_error = ERROR_INVALID_DATA; }
        else if (!operation->result.local_accepted) work_error = ERROR_GEN_FAILURE;
    }
cleanup:
    if (text.control || text.object) {
        operation->result.text_released = release_native(&text, active_profile->text_control_rva);
        if (!operation->result.text_released) clean = FALSE;
    }
    for (index = 3; index > 0; --index) {
        if (refs[index - 1].control || refs[index - 1].object) {
            if (release_native(&refs[index - 1], 0)) operation->result.context_release_mask |= 1u << (index - 1);
            else clean = FALSE;
        }
    }
    operation->result.native_cleanup_ok = clean;
    operation->result.native_prepare_ok = fields && clean && operation->result.context_release_mask == 7 &&
        (operation->result.text_released || operation->result.input_consumed);
    operation->result.callback_error = operation->result.native_prepare_ok ? work_error : ERROR_INVALID_DATA;
    if (!operation->result.callback_error) operation->result.error_phase = 0;
}

static void post_once(uintptr_t base, const struct runtime_profile *profile, Operation *operation) {
    FixtureRootFn root_fn;
    FixtureGetQueueFn get_queue;
    FixtureMakeLocationFn make_location;
    FixturePostFn post;
    FixtureSharedRef queue = {0}, result = {0};
    FixtureLocation location = {0};
    FixtureFn64 function = {{0}, NULL};
    FixtureFnImpl native;
    uintptr_t address;
    void *root;
    BOOL q_ok = TRUE, r_ok = TRUE, valid = FALSE;
    active_base = base; active_profile = profile;

    if (!hash_module((HMODULE)base, profile, TRUE)) goto cleanup;
    if (profile->kind == 1 && (!profile->expected_tid_rva ||
        profile->expected_tid_rva >= profile->image_size ||
        4 > profile->image_size - profile->expected_tid_rva ||
        !read_self((LPCVOID)(base + profile->expected_tid_rva), &expected_tid, 4) || !expected_tid)) goto cleanup;
    /* Copy validated addresses into ABI-correct x64 function pointers. */
    address = base + profile->functions[P_ROOT].rva; memcpy(&root_fn, &address, sizeof(root_fn));
    address = base + profile->functions[P_QUEUE].rva; memcpy(&get_queue, &address, sizeof(get_queue));
    address = base + profile->functions[P_LOCATION].rva; memcpy(&make_location, &address, sizeof(make_location));
    address = base + profile->functions[P_POST].rva; memcpy(&post, &address, sizeof(post));
    root = root_fn();
    if (!root || get_queue(root, &queue) != &queue || !queue.object || !queue.control) goto cleanup;
    if (make_location(&location, location_file, location_function, (DWORD)__LINE__) != &location ||
        !location.return_address) goto cleanup;
    operation->own_impl.vtable = &own_invoke_only_vtable;
    operation->own_impl.operation = operation;
    operation->inner.impl = (FixtureFnImpl *)&operation->own_impl;
    native.vtable = (const FixtureFnVtable *)(base + profile->fn_vtable_rva);
    native.payload = &operation->inner;
    memcpy(function.storage, &native, sizeof(native));
    function.impl = (FixtureFnImpl *)function.storage;
    if (post(queue.object, &result, &location, &function, 1) != &result) goto cleanup;
    InterlockedExchange(&operation->consumed, function.impl == NULL);
    if (!function.impl && result.object && result.control) {
        InterlockedExchange(&operation->posted, 1); valid = TRUE;
    }
cleanup:
    /* Leftover input is local inline storage and uses the validated native Delete. */
    if (function.impl == (FixtureFnImpl *)function.storage) {
        native.vtable->delete_this(function.impl, 0); function.impl = NULL;
    } else if (function.impl) valid = FALSE;
    r_ok = release_ref(&result, base, profile, TRUE); q_ok = release_ref(&queue, base, profile, FALSE);
    InterlockedExchange(&operation->result_released, r_ok); InterlockedExchange(&operation->queue_released, q_ok);
    InterlockedExchange(&operation->release_ok, r_ok && q_ok);
    if (!valid || !r_ok || !q_ok) {
        InterlockedExchange(&operation->failure, ERROR_INVALID_DATA);
        InterlockedExchange(&operation->gate, 3);
    } else InterlockedExchange(&operation->gate, 2);
}


/* RPC copies all inputs before posting. Operation/closure storage remains resident;
 * no config pointer or external Python buffer survives this call. */
__declspec(dllexport) DWORD WINAPI HookSend(void *argument) {
    ChSendRpc *rpc=HeapAlloc(GetProcessHeap(),0,sizeof(*rpc));
    Operation *operation=NULL;
    HMODULE module=GetModuleHandleW(L"Weixin.dll"),pin=NULL;
#ifdef CH_RESOURCE_SIDECAR
    if (module) native_base=(uintptr_t)module;
#endif
    MODULEINFO info;
    DWORD result=ERROR_INVALID_PARAMETER;
    if (!rpc) return ERROR_OUTOFMEMORY;
    if (!read_self(argument,rpc,sizeof(*rpc)) || rpc->magic!=CH_SEND_MAGIC || rpc->version!=4 ||
        rpc->size!=sizeof(*rpc) || rpc->pid!=GetCurrentProcessId() ||
        !memchr(rpc->request_id,0,64) || !rpc->request_id[0] ||
        !memchr(rpc->account,0,128) || !memchr(rpc->target,0,128) ||
        !memchr(rpc->body,0,65537) || rpc->query>1) goto leave;
    if (!TryAcquireSRWLockExclusive(&send_gate)) {result=ERROR_BUSY;goto leave;}
    const struct runtime_profile *profile=
#ifdef CH_FIXTURE
        &fixture_profile;
#else
        &real_profile;
#endif
    memset(&rpc->result,0,sizeof(rpc->result));
    for (operation=operations;operation;operation=operation->next)
        if (!strcmp(operation->request_id,rpc->request_id)) break;
    unsigned char fingerprint[32]={0};
    if (!rpc->query) {
        if (!input_fingerprint(rpc,fingerprint)) {result=ERROR_INVALID_DATA;goto unlock;}
        if (operation && memcmp(operation->fingerprint,fingerprint,32)) {result=ERROR_ALREADY_EXISTS;goto unlock;}
    }
    if (!operation && rpc->query) {result=ERROR_NOT_FOUND;goto unlock;}
    if (!operation) {
        char identity[129];
        if (!module || !account_identity(identity) || strcmp(identity,rpc->account) ||
            !profile_matches(module,&info,profile)) {result=ERROR_INVALID_DATA;goto unlock;}
        if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
            (LPCWSTR)(uintptr_t)HookSend,&pin)) goto unlock;
        for (Operation *it=operations;it;it=it->next)
            if (!atomic_read(&it->done) || atomic_read(&it->gate)!=2) {result=ERROR_BUSY;goto unlock;}
        if (!prepare_operation.code) {
            prepare_operation.code=BRIDGE_OP_PREPARE;
            strcpy(prepare_operation.account,rpc->account);strcpy(prepare_operation.target,rpc->target);
            prepare_operation.body="prepare";
            post_once((uintptr_t)module,profile,&prepare_operation);
        }
        ULONGLONG until=GetTickCount64()+2000;
        while (!atomic_read(&prepare_operation.done) && atomic_read(&prepare_operation.gate)!=3 && GetTickCount64()<until) Sleep(5);
        if (!atomic_read(&prepare_operation.done)) {result=ERROR_NOT_READY;goto unlock;}
        if (prepare_operation.result.callback_error || atomic_read(&prepare_operation.gate)!=2) {result=ERROR_INVALID_DATA;goto unlock;}
        /* Retained tiny operation records prevent late closures or duplicate IDs
         * from referencing reclaimed state. Refuse growth at an explicit bound. */
        if (operation_count>=65536) {result=ERROR_NOT_ENOUGH_MEMORY;goto unlock;}
        operation=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(*operation));
        if (!operation) {result=ERROR_OUTOFMEMORY;goto unlock;}
        operation->body=HeapAlloc(GetProcessHeap(),0,strlen(rpc->body)+1);
        if (!operation->body) {HeapFree(GetProcessHeap(),0,operation);operation=NULL;result=ERROR_OUTOFMEMORY;goto unlock;}
        strcpy(operation->request_id,rpc->request_id);strcpy(operation->account,rpc->account);
        strcpy(operation->target,rpc->target);strcpy(operation->body,rpc->body);
        operation->code=BRIDGE_OP_SEND;operation->next=operations;operations=operation;++operation_count;
        memcpy(operation->fingerprint,fingerprint,32);
        post_once((uintptr_t)module,profile,operation);
    }
    if (strcmp(operation->account,rpc->account)) {result=ERROR_ACCESS_DENIED;goto unlock;}
    ULONGLONG until=GetTickCount64()+1500;
    while ((!atomic_read(&operation->done) || atomic_read(&operation->gate)==1) && atomic_read(&operation->gate)!=3 && GetTickCount64()<until) Sleep(5);
    rpc->result=operation->result;
    memcpy(rpc->native_client_id,operation->native_client_id,sizeof(rpc->native_client_id));
    rpc->result.callback_done=atomic_read(&operation->done);
    rpc->result.callback_tid=atomic_read(&operation->callback_tid);
    rpc->result.invocation_count=atomic_read(&operation->count);
    rpc->result.queue_gate=atomic_read(&operation->gate);
    rpc->result.posted=atomic_read(&operation->posted);
    rpc->result.fn_consumed=atomic_read(&operation->consumed);
    rpc->result.queue_ref_released=atomic_read(&operation->queue_released);
    rpc->result.result_ref_released=atomic_read(&operation->result_released);
    result=atomic_read(&operation->gate)==3?ERROR_INVALID_DATA:
        !atomic_read(&operation->done)?ERROR_IO_PENDING:operation->result.callback_error;
    if (atomic_read(&operation->done) && operation->body) {
        SecureZeroMemory(operation->body,strlen(operation->body));HeapFree(GetProcessHeap(),0,operation->body);operation->body=NULL;
    }
unlock:
    ReleaseSRWLockExclusive(&send_gate);
leave:
    rpc->error=result;
    SIZE_T transferred=0;
    WriteProcessMemory(GetCurrentProcess(),argument,rpc,sizeof(*rpc),&transferred);
    HeapFree(GetProcessHeap(),0,rpc);
    return result;
}
