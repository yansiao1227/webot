/* 仅挂钩本可执行文件自有的函数，不调用微信 API 或操作微信进程。 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "MinHook.h"
#include "record_queue.h"

typedef int (*ReceiveFn)(HookRecord *);
static ReceiveFn original_receive;
static HookQueue queue;
static volatile LONG original_calls;
static volatile LONG hook_calls;

__attribute__((noinline)) static int fixture_receive(HookRecord *record) {
    InterlockedIncrement(&original_calls);
    /* 模拟由调用方持有、在清理期间存储失效的记录。 */
    int value = (int)record->sequence + 17;
    memset(record->text, 0, sizeof(record->text));
    return value;
}

static int intercepted_receive(HookRecord *record) {
    InterlockedIncrement(&hook_calls);
    hook_queue_push(&queue, record);
    return original_receive(record);
}

int main(void) {
    HookRecord input = {0}, captured;
    unsigned char before[32];
    ReceiveFn volatile dispatch = fixture_receive;
    LPVOID target = (LPVOID)(ULONG_PTR)fixture_receive;
    BOOL created = FALSE, enabled = FALSE;
    int result = 1;
    hook_queue_init(&queue);
    memcpy(before, target, sizeof(before));
    if (MH_Initialize() != MH_OK) return 2;
    if (MH_CreateHook(target, (LPVOID)(ULONG_PTR)intercepted_receive,
                      (LPVOID *)&original_receive) != MH_OK) goto cleanup;
    created = TRUE;
    if (MH_EnableHook(target) != MH_OK) goto cleanup;
    enabled = TRUE;
    for (unsigned i = 0; i < HOOK_QUEUE_CAPACITY + 3; ++i) {
        input.sequence = i;
        input.kind = 1;
        strcpy(input.text, "\xe4\xb8\xad\xe6\x96\x87\n\"fixture\"");
        if (dispatch(&input) != (int)i + 17 || input.text[0] != 0) goto cleanup;
    }
    if (hook_calls != 19 || original_calls != 19 || queue.dropped != 3) goto cleanup;
    for (unsigned i = 0; i < HOOK_QUEUE_CAPACITY; ++i) {
        if (!hook_queue_pop(&queue, &captured) || captured.sequence != i ||
            strcmp(captured.text, "\xe4\xb8\xad\xe6\x96\x87\n\"fixture\"")) goto cleanup;
    }
    if (hook_queue_pop(&queue, &captured)) goto cleanup;
    if (MH_DisableHook(target) != MH_OK) goto cleanup;
    enabled = FALSE;
    if (MH_RemoveHook(target) != MH_OK) goto cleanup;
    created = FALSE;
    input.sequence = 99;
    if (dispatch(&input) != 116 || hook_calls != 19 || original_calls != 20 ||
        memcmp(before, target, sizeof(before))) goto cleanup;
    result = 0;
cleanup:
    if (enabled && MH_DisableHook(target) != MH_OK) result = 3;
    if (created && MH_RemoveHook(target) != MH_OK) result = 3;
    if (MH_Uninitialize() != MH_OK) result = 3;
    printf("{\"schema\":\"client-hook-fixture-v1\",\"passed\":%s,"
           "\"hook_calls\":%ld,\"original_calls\":%ld,\"dropped\":%ld,"
           "\"scope\":\"own_process_only\"}\n",
           result == 0 ? "true" : "false", hook_calls, original_calls, queue.dropped);
    return result;
}
