#include "record_queue.h"
#include <string.h>
void hook_queue_init(HookQueue *queue) {
    memset(queue, 0, sizeof(*queue));
    InitializeSRWLock(&queue->lock);
}
BOOL hook_queue_push(HookQueue *queue, const HookRecord *record) {
    if (!TryAcquireSRWLockExclusive(&queue->lock)) {
        InterlockedIncrement(&queue->dropped);
        return FALSE;
    }
    if (queue->count == HOOK_QUEUE_CAPACITY) {
        ReleaseSRWLockExclusive(&queue->lock);
        InterlockedIncrement(&queue->dropped);
        return FALSE;
    }
    queue->records[(queue->read_at + queue->count) % HOOK_QUEUE_CAPACITY] = *record;
    ++queue->count;
    ReleaseSRWLockExclusive(&queue->lock);
    return TRUE;
}
BOOL hook_queue_pop(HookQueue *queue, HookRecord *record) {
    AcquireSRWLockExclusive(&queue->lock);
    if (!queue->count) {
        ReleaseSRWLockExclusive(&queue->lock);
        return FALSE;
    }
    *record = queue->records[queue->read_at];
    queue->read_at = (queue->read_at + 1) % HOOK_QUEUE_CAPACITY;
    --queue->count;
    ReleaseSRWLockExclusive(&queue->lock);
    return TRUE;
}
