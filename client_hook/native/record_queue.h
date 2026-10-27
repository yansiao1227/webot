#ifndef CLIENT_HOOK_RECORD_QUEUE_H
#define CLIENT_HOOK_RECORD_QUEUE_H
#include <windows.h>
#include <stdint.h>
#define HOOK_QUEUE_CAPACITY 16
typedef struct {
    uint32_t sequence;
    uint32_t kind;
    char text[128];
} HookRecord;
typedef struct {
    SRWLOCK lock;
    HookRecord records[HOOK_QUEUE_CAPACITY];
    unsigned read_at, count;
    volatile LONG dropped;
} HookQueue;
void hook_queue_init(HookQueue *queue);
/* 复制自有 POD 数据，不保留指向调用方记录的指针。 */
BOOL hook_queue_push(HookQueue *queue, const HookRecord *record);
BOOL hook_queue_pop(HookQueue *queue, HookRecord *record);
#endif
