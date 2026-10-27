/* 以下全部内存和函数均属于此测试可执行文件。 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "MinHook.h"
#include "weixin41930.h"
#include "stream_snapshot.h"

extern void fixture_finalize_entry(Wx41930Record *, uint8_t);
extern void fixture_allowed_call(Wx41930Record *, uint8_t);
extern void fixture_other_call(Wx41930Record *, uint8_t);
extern char fixture_allowed_return;
static Wx41930FinalizeFn original;
static unsigned calls, accepted, rejected, filtered;
static uint8_t observed_force;
static Wx41930Record *observed_record;
static StreamSample captured;
static uintptr_t fixture_vtable[1];
static const char *allowlist[] = {"wxid_fixture_peer"};

void fixture_finalize_body(Wx41930Record *record, uint8_t force) {
    ++calls;
    observed_force = force;
    observed_record = record;
    /* 模拟原生快速路径和强制刷新分支。 */
    if (force || !record->parsed_flag) {
        record->cached_118 = record->raw_kind;
        record->parsed_flag = 1;
    }
}

static void detour(Wx41930Record *record, uint8_t force) {
    uintptr_t caller = (uintptr_t)__builtin_return_address(0);
    uintptr_t vt = 0;
    SIZE_T bytes = 0;
    if (caller == (uintptr_t)&fixture_allowed_return && force == 1 &&
        ReadProcessMemory(GetCurrentProcess(), record, &vt, sizeof(vt), &bytes) &&
        bytes == sizeof(vt) && vt == (uintptr_t)fixture_vtable) {
        StreamSample sample;
        int rc = stream_snapshot_capture(GetCurrentProcess(), (uintptr_t)record,
                         (uintptr_t)fixture_vtable, allowlist, 1, &sample);
        if (rc == 1) { captured = sample; ++accepted; }
        else if (rc < 0) ++rejected;
        else ++filtered;
    } else ++filtered;
    original(record, force); /* 保留完整字节，包括 0x81。 */
}

static void set_string(Wx41930String *out, const char *text) {
    size_t n = strlen(text);
    memset(out, 0, sizeof(*out));
    out->length = n;
    out->capacity = n < 16 ? 15 : n;
    if (n < 16) memcpy(out->storage.inline_bytes, text, n + 1);
    else out->storage.heap = (uintptr_t)text;
}

static void make_record(Wx41930Record *out, const char *body) {
    memset(out, 0, sizeof(*out));
    out->vtable = (uintptr_t)fixture_vtable;
    out->raw_kind = 1;
    set_string(&out->endpoint_a, "wxid_fixture_peer");
    set_string(&out->endpoint_b, "wxid_fixture_self");
    set_string(&out->actor_candidate, "wxid_fixture_peer");
    set_string(&out->text, body);
}

int main(void) {
    Wx41930Record record;
    LPVOID target = (LPVOID)(uintptr_t)fixture_finalize_entry;
    unsigned char before[32];
    int result = 1;
    BOOL created = FALSE, enabled = FALSE;
    const char *heap_body = "\xe4\xb8\xad\xe6\x96\x87 heap message\n\"quoted\"";
    memcpy(before, target, sizeof(before));
    /* 独立于测试桩地址校验路由条件。 */
    uintptr_t base = 0x180000000ULL;
    if (!wx41930_route_matches(base, base + WX41930_ALLOWED_RETURN_RVA, 1,
                              base + WX41930_RECORD_VTABLE_RVA) ||
        wx41930_route_matches(base, base + WX41930_ALLOWED_RETURN_RVA + 1, 1,
                             base + WX41930_RECORD_VTABLE_RVA)) return 2;
    if (MH_Initialize() != MH_OK) return 2;
    if (MH_CreateHook(target, (LPVOID)(uintptr_t)detour, (LPVOID *)&original) != MH_OK) goto cleanup;
    created = TRUE;
    if (MH_EnableHook(target) != MH_OK) goto cleanup;
    enabled = TRUE;
    make_record(&record, "\xe7\x9f\xad");
    fixture_allowed_call(&record, 1);
    if (accepted != 1 || strcmp(captured.body, "\xe7\x9f\xad") || !record.parsed_flag ||
        observed_record != &record || observed_force != 1) goto cleanup;
    make_record(&record, heap_body);
    fixture_allowed_call(&record, 1);
    memset(&record, 0xa5, sizeof(record));
    if (accepted != 2 || strcmp(captured.body, heap_body) || captured.raw_c != 1 ||
        strcmp(captured.endpoint18, "wxid_fixture_peer")) goto cleanup;
    make_record(&record, "filtered");
    fixture_other_call(&record, 1);
    fixture_allowed_call(&record, 0);
    fixture_allowed_call(&record, 0x81);
    if (filtered != 3 || observed_force != 0x81 || accepted != 2) goto cleanup;
    make_record(&record, "\xc0\xaf"); /* 必须拒绝过长 UTF-8 编码。 */
    fixture_allowed_call(&record, 1);
    if (rejected != 1) goto cleanup;
    make_record(&record, "wrong vtable");
    record.vtable = 0;
    fixture_allowed_call(&record, 1);
    if (filtered != 4 || calls != 7) goto cleanup;
    if (MH_DisableHook(target) != MH_OK) goto cleanup;
    enabled = FALSE;
    if (MH_RemoveHook(target) != MH_OK) goto cleanup;
    created = FALSE;
    make_record(&record, "restored");
    fixture_allowed_call(&record, 1);
    if (calls != 8 || accepted != 2 || memcmp(before, target, sizeof(before))) goto cleanup;
    result = 0;
cleanup:
    if (enabled && MH_DisableHook(target) != MH_OK) result = 3;
    if (created && MH_RemoveHook(target) != MH_OK) result = 3;
    if (MH_Uninitialize() != MH_OK) result = 3;
    printf("{\"schema\":\"client-hook-41930-abi-fixture-v1\",\"passed\":%s,"
           "\"original_calls\":%u,\"accepted\":%u,\"rejected\":%u,\"filtered\":%u,"
           "\"scope\":\"own_process_layout_and_ms_x64_abi_only\"}\n",
           result ? "false" : "true", calls, accepted, rejected, filtered);
    return result;
}
