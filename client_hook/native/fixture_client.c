#define FIXTURE_CLIENT_BUILD
#include "fixture_abi.h"
#include <string.h>

typedef struct FixtureQueue {
    uintptr_t reserved;
    void *fallback;
    void *executor;
} FixtureQueue;

static SRWLOCK state_lock = SRWLOCK_INIT;
static FixtureStats counters;
static FixtureQueue queue_object;
static uintptr_t root_object;
static uintptr_t result_object;
static const uintptr_t native_type = 0x4649585455524531ULL;
static HANDLE work_event;
static HANDLE stop_event;
static HANDLE worker_thread;
static DWORD worker_delay;
static DWORD accepted_posts;
static BOOL stopping;
static FixtureFn64 queued_function;
static SRWLOCK text_lock = SRWLOCK_INIT;
typedef struct FixtureToken { const uintptr_t *vtable; } FixtureToken;
typedef struct TextRegistration {
    void *object;
    FixtureCtrl *control;
    BOOL alive;
    BOOL consumed;
} TextRegistration;
static TextRegistration text_registrations[16];
typedef struct ResultRegistration {
    void *raw;
    size_t allocation_size;
    BOOL large;
    BOOL active;
} ResultRegistration;
static SRWLOCK result_lock = SRWLOCK_INIT;
static ResultRegistration result_registrations[4];
static volatile LONG result_mode;
static FixtureToken context1_object = {FixtureSendContextVtable};
static FixtureToken context2_object = {FixtureSendContextVtable};
static unsigned char context3_object[0xB80];
static unsigned char cache_state[0xA8];
static uintptr_t cache_sentinel[2];
FIXTURE_API volatile DWORD FixtureExpectedWorkerTid;
FIXTURE_API const uintptr_t FixtureSendContextVtable[1] = {0x4649584354583033ULL};
FIXTURE_API const uintptr_t FixtureTextVtable[1] = {0x4649585445585431ULL};

FIXTURE_API const FixtureFnVtable FixtureNativeVtable = {
    FixtureFnCopy, FixtureFnMove, FixtureFnInvoke, FixtureFnType, FixtureFnDelete
};
FIXTURE_API const FixtureCtrlVtable FixtureControlVtable = {
    FixtureCtrlDestroy, FixtureCtrlDelete
};
FIXTURE_API const FixtureCtrlVtable FixtureQueueControlVtable = {
    FixtureCtrlDestroy, FixtureCtrlDelete
};
FIXTURE_API const FixtureCtrlVtable FixtureResultControlVtable = {
    FixtureCtrlDestroy, FixtureCtrlDelete
};
FIXTURE_API const FixtureCtrlVtable FixtureContext1ControlVtable = {FixtureCtrlDestroy, FixtureCtrlDelete};
FIXTURE_API const FixtureCtrlVtable FixtureContext2ControlVtable = {FixtureCtrlDestroy, FixtureCtrlDelete};
FIXTURE_API const FixtureCtrlVtable FixtureContext3ControlVtable = {FixtureCtrlDestroy, FixtureCtrlDelete};
FIXTURE_API const FixtureCtrlVtable FixtureTextControlVtable = {FixtureCtrlDestroy, FixtureCtrlDelete};

static FixtureString32 *text_string(void *text, size_t offset) {
    return (FixtureString32 *)((unsigned char *)text + offset);
}

static void destroy_string(FixtureString32 *string) {
    if (string->capacity >= 16 && string->data.heap != NULL) {
        HeapFree(GetProcessHeap(), 0, string->data.heap);
        InterlockedIncrement(&counters.strings_heap_frees);
    }
    memset(string, 0, sizeof(*string));
    string->capacity = 15;
}

/* Tombstones let a second consume be counted without dereferencing freed memory. */
static BOOL claim_text_input(const FixtureSharedRef *owned) {
    size_t i;
    BOOL found = FALSE;
    BOOL consumed_before = FALSE;
    if (owned == NULL) {
        InterlockedIncrement(&counters.input_bad_refs);
        return FALSE;
    }
    AcquireSRWLockExclusive(&text_lock);
    for (i = 0; i < sizeof(text_registrations) / sizeof(text_registrations[0]); ++i) {
        if (owned->object != NULL && text_registrations[i].object == owned->object &&
            text_registrations[i].control == owned->control) {
            if (text_registrations[i].alive && !text_registrations[i].consumed) {
                text_registrations[i].consumed = TRUE;
                found = TRUE;
                break;
            }
            if (text_registrations[i].consumed) consumed_before = TRUE;
        }
    }
    ReleaseSRWLockExclusive(&text_lock);
    if (found) InterlockedIncrement(&counters.input_consumed_count);
    else if (consumed_before) InterlockedIncrement(&counters.input_double_consume_count);
    else InterlockedIncrement(&counters.input_bad_refs);
    return found;
}

static void clear_function(FixtureFn64 *fn) {
    FixtureFnImpl *impl = fn->impl;
    if (impl != NULL) {
        fn->impl = NULL;
        impl->vtable->delete_this(impl, (unsigned char)(impl != (FixtureFnImpl *)fn));
    }
}

static FixtureCtrl *make_control(void *object, DWORD kind) {
    FixtureCtrl *ctrl = (FixtureCtrl *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*ctrl));
    if (ctrl == NULL) return NULL;
    switch (kind) {
        case 1: ctrl->vtable = &FixtureQueueControlVtable; break;
        case 2: ctrl->vtable = &FixtureResultControlVtable; break;
        case 11: ctrl->vtable = &FixtureContext1ControlVtable; break;
        case 12: ctrl->vtable = &FixtureContext2ControlVtable; break;
        case 13: ctrl->vtable = &FixtureContext3ControlVtable; break;
        case 20: ctrl->vtable = &FixtureTextControlVtable; break;
        default: ctrl->vtable = &FixtureControlVtable; break;
    }
    ctrl->strong = 1;
    ctrl->weak = kind == 20 ? 2 : 1; /* text owns one enable_shared_from_this weak */
    ctrl->object = object;
    ctrl->kind = kind;
    InterlockedIncrement(&counters.ctrl_allocs);
    return ctrl;
}

FIXTURE_API void FixtureCtrlDestroy(FixtureCtrl *ctrl) {
    if (ctrl == NULL) return;
    if (ctrl->kind == 20 && ctrl->object != NULL) {
        size_t i;
        AcquireSRWLockExclusive(&text_lock);
        for (i = 0; i < sizeof(text_registrations) / sizeof(text_registrations[0]); ++i) {
            if (text_registrations[i].alive && text_registrations[i].control == ctrl &&
                text_registrations[i].object == ctrl->object) {
                text_registrations[i].alive = FALSE;
                break;
            }
        }
        ReleaseSRWLockExclusive(&text_lock);
        destroy_string(text_string(ctrl->object, 0xB0));
        destroy_string(text_string(ctrl->object, 0x618));
        destroy_string(text_string(ctrl->object, 0x678));
        HeapFree(GetProcessHeap(), 0, ctrl->object);
        InterlockedIncrement(&counters.text_destroy);
        /* Native text destruction removes selfweak; bridge removes implicit weak. */
        InterlockedDecrement(&ctrl->weak);
    }
    /* Queue/result referents are permanent tokens. A released result never cancels work. */
    ctrl->object = NULL;
    InterlockedIncrement(&counters.ctrl_destroys);
}

FIXTURE_API void FixtureCtrlDelete(FixtureCtrl *ctrl) {
    if (ctrl == NULL) return;
    InterlockedIncrement(&counters.ctrl_frees);
    HeapFree(GetProcessHeap(), 0, ctrl);
}

FIXTURE_API FixtureFnImpl *FixtureFnCopy(const FixtureFnImpl *self, void *small_buffer) {
    FixtureFnImpl *copy;
    if (self == NULL || small_buffer == NULL) return NULL;
    copy = (FixtureFnImpl *)small_buffer;
    copy->vtable = &FixtureNativeVtable;
    copy->payload = self->payload;
    InterlockedIncrement(&counters.fn_copies);
    return copy;
}

FIXTURE_API FixtureFnImpl *FixtureFnMove(FixtureFnImpl *self, void *small_buffer) {
    FixtureFnImpl *moved;
    if (self == NULL || small_buffer == NULL) return NULL;
    moved = (FixtureFnImpl *)small_buffer;
    moved->vtable = &FixtureNativeVtable;
    moved->payload = self->payload;
    InterlockedIncrement(&counters.fn_moves);
    return moved;
}

FIXTURE_API void FixtureFnInvoke(FixtureFnImpl *self) {
    FixtureFnImpl *inner;
    if (self == NULL || self->payload == NULL) return;
    inner = self->payload->impl;
    if (inner != NULL && inner->vtable != NULL && inner->vtable->invoke != NULL)
        inner->vtable->invoke(inner);
}

FIXTURE_API const void *FixtureFnType(const FixtureFnImpl *self) {
    (void)self;
    return &native_type;
}

FIXTURE_API void FixtureFnDelete(FixtureFnImpl *self, unsigned char heap) {
    if (self == NULL) return;
    /* The outer reference never owns, retains, mutates, or destroys its payload. */
    InterlockedIncrement(&counters.fn_deletes);
    if (heap != 0) {
        InterlockedIncrement(&counters.fn_heap_frees);
        HeapFree(GetProcessHeap(), 0, self);
    }
}

static DWORD WINAPI run_queue(void *argument) {
    HANDLE events[2];
    FixtureFn64 local = {{0}, NULL};
    (void)argument;
    events[0] = work_event;
    events[1] = stop_event;
    for (;;) {
        DWORD wait_result = WaitForMultipleObjects(2, events, FALSE, INFINITE);
        BOOL has_job;
        AcquireSRWLockExclusive(&state_lock);
        has_job = queued_function.impl != NULL;
        if (has_job) {
            /* This second move models the consumer taking its persistent job. */
            if (queued_function.impl == (FixtureFnImpl *)&queued_function) {
                local.impl = queued_function.impl->vtable->move(queued_function.impl, &local);
                queued_function.impl->vtable->delete_this(queued_function.impl, 0);
            } else local.impl = queued_function.impl;
            queued_function.impl = NULL;
        }
        ReleaseSRWLockExclusive(&state_lock);
        if (has_job) {
            if (worker_delay != 0) Sleep(worker_delay);
            if (local.impl != NULL) {
                InterlockedExchange((volatile LONG *)&counters.invoke_tid, (LONG)GetCurrentThreadId());
                local.impl->vtable->invoke(local.impl);
                clear_function(&local);
                InterlockedIncrement(&counters.jobs_completed);
            }
            InterlockedExchange(&counters.pending, 0);
        }
        if (wait_result == WAIT_OBJECT_0 + 1 || WaitForSingleObject(stop_event, 0) == WAIT_OBJECT_0) break;
        if (wait_result != WAIT_OBJECT_0) break;
    }
    return 0;
}

FIXTURE_API void *FixtureRoot(void) {
    return &root_object;
}

FIXTURE_API FixtureSharedRef *FixtureGetQueue(void *root, FixtureSharedRef *out) {
    FixtureCtrl *ctrl = NULL;
    if (out == NULL) return NULL;
    out->object = NULL;
    out->control = NULL;
    AcquireSRWLockShared(&state_lock);
    if (root == &root_object && worker_thread != NULL && !stopping && counters.stopped == 0)
        ctrl = make_control(&queue_object, 1);
    if (ctrl != NULL) {
        out->object = &queue_object;
        out->control = ctrl;
        InterlockedIncrement(&counters.queue_ref_acquires);
    }
    ReleaseSRWLockShared(&state_lock);
    return out;
}

__attribute__((noinline)) FIXTURE_API FixtureLocation *FixtureMakeLocation(
    FixtureLocation *out, const void *p0, const void *p1, DWORD number) {
    if (out == NULL) return NULL;
    out->p0 = p0;
    out->p1 = p1;
    out->number = number;
    out->return_address = __builtin_return_address(0);
    return out;
}

FIXTURE_API FixtureSharedRef *FixturePost(void *queue, FixtureSharedRef *out,
    const FixtureLocation *location, FixtureFn64 *input, DWORD flags) {
    FixtureFn64 local = {{0}, NULL};
    FixtureCtrl *ctrl = NULL;
    BOOL valid;
    if (out == NULL) return NULL;
    out->object = NULL;
    out->control = NULL;
    InterlockedIncrement(&counters.posts);
    if (input != NULL && input->impl != NULL) {
        FixtureFnImpl *original = input->impl;
        if (original->vtable == &FixtureNativeVtable) {
            if (original == (FixtureFnImpl *)input) {
                local.impl = original->vtable->move(original, &local);
                original->vtable->delete_this(original, 0);
            } else local.impl = original;
            input->impl = NULL;
        }
    }
    AcquireSRWLockExclusive(&state_lock);
    valid = queue == &queue_object && out != NULL && location != NULL &&
        location->p0 != NULL && location->p1 != NULL && location->return_address != NULL &&
        flags == 1 && local.impl != NULL && worker_thread != NULL &&
        !stopping && counters.stopped == 0 && accepted_posts < 65536 &&
        queued_function.impl == NULL;
    if (valid) ctrl = make_control(&result_object, 2);
    if (ctrl != NULL) {
        queued_function.impl = local.impl->vtable->copy(local.impl, &queued_function);
        if (queued_function.impl != NULL) {
            ++accepted_posts;
            out->object = &result_object;
            out->control = ctrl;
            counters.location_return = (uint64_t)(uintptr_t)location->return_address;
            InterlockedIncrement(&counters.jobs_queued);
            InterlockedExchange(&counters.pending, 1);
            SetEvent(work_event);
        } else {
            FixtureCtrlDestroy(ctrl);
            FixtureCtrlDelete(ctrl);
        }
    }
    if (out->control == NULL) InterlockedIncrement(&counters.posts_rejected);
    ReleaseSRWLockExclusive(&state_lock);
    clear_function(&local);
    return out;
}

FIXTURE_API const FixtureFnVtable *FixtureGetFnVtable(void) {
    return &FixtureNativeVtable;
}

static FixtureSharedRef *context_reference(void *object, DWORD kind, FixtureSharedRef *out) {
    if (out == NULL) return NULL;
    out->object = NULL;
    out->control = make_control(object, kind);
    if (out->control != NULL) out->object = object;
    return out;
}

FIXTURE_API FixtureSharedRef *FixtureGetContext1(FixtureSharedRef *out) {
    InterlockedIncrement(&counters.context1_calls);
    InterlockedIncrement(&counters.context_getter_calls);
    return context_reference(&context1_object, 11, out);
}

FIXTURE_API FixtureSharedRef *FixtureGetContext2(void *object, FixtureSharedRef *out) {
    InterlockedIncrement(&counters.context2_calls);
    InterlockedIncrement(&counters.context_getter_calls);
    if (object != &context1_object) {
        if (out != NULL) memset(out, 0, sizeof(*out));
        return out;
    }
    return context_reference(&context2_object, 12, out);
}

FIXTURE_API FixtureSharedRef *FixtureGetContext3(void *object, FixtureSharedRef *out) {
    InterlockedIncrement(&counters.context3_calls);
    InterlockedIncrement(&counters.context_getter_calls);
    if (object != &context2_object) {
        if (out != NULL) memset(out, 0, sizeof(*out));
        return out;
    }
    return context_reference(&context3_object, 13, out);
}

FIXTURE_API FixtureSharedRef *FixtureTextFactory(FixtureSharedRef *out) {
    unsigned char *text;
    FixtureCtrl *ctrl;
    size_t i;
    BOOL registered = FALSE;
    static const char auto_id[] = "fixture_auto_id";
    InterlockedIncrement(&counters.factory_calls);
    if (out == NULL) return NULL;
    memset(out, 0, sizeof(*out));
    text = (unsigned char *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, 0x6B8);
    if (text == NULL) return out;
    *(const uintptr_t **)text = FixtureTextVtable;
    {
        DWORD native_default_kind = 1;
        memcpy(text + 0x9C, &native_default_kind, sizeof(native_default_kind));
    }
    text_string(text, 0xB0)->capacity = 15;
    text_string(text, 0x618)->capacity = 15;
    text_string(text, 0x678)->capacity = 15;
    memcpy(text_string(text, 0x618)->data.small, auto_id, sizeof(auto_id));
    text_string(text, 0x618)->size = sizeof(auto_id) - 1;
    ctrl = make_control(text, 20);
    if (ctrl == NULL) { HeapFree(GetProcessHeap(), 0, text); return out; }
    AcquireSRWLockExclusive(&text_lock);
    for (i = 0; i < sizeof(text_registrations) / sizeof(text_registrations[0]); ++i) {
        if (text_registrations[i].object == NULL) {
            text_registrations[i].object = text;
            text_registrations[i].control = ctrl;
            text_registrations[i].alive = TRUE;
            text_registrations[i].consumed = FALSE;
            registered = TRUE;
            break;
        }
    }
    ReleaseSRWLockExclusive(&text_lock);
    if (!registered) {
        FixtureCtrlDestroy(ctrl);
        FixtureCtrlDelete(ctrl);
        return out;
    }
    out->object = text;
    out->control = ctrl;
    InterlockedIncrement(&counters.text_allocs);
    return out;
}

FIXTURE_API FixtureString32 *FixtureStringAssign(FixtureString32 *string, size_t length,
    uintptr_t unused, const char *source) {
    char small[16] = {0};
    char *heap = NULL;
    (void)unused;
    InterlockedIncrement(&counters.string_assign_calls);
    if (string == NULL || length > 65536 || (source == NULL && length != 0)) return NULL;
    if (length <= 15) {
        if (length != 0) memcpy(small, source, length);
    } else {
        heap = (char *)HeapAlloc(GetProcessHeap(), 0, length + 1);
        if (heap == NULL) return NULL;
        memcpy(heap, source, length);
        heap[length] = '\0';
        InterlockedIncrement(&counters.strings_heap_allocs);
    }
    destroy_string(string);
    string->size = length;
    if (heap != NULL) { string->data.heap = heap; string->capacity = length; }
    else memcpy(string->data.small, small, sizeof(small));
    return string;
}

static BOOL string_equals(const FixtureString32 *string, const char *literal, size_t size) {
    const char *data;
    if (string->size != size || string->capacity < size || string->capacity > 65536) return FALSE;
    data = string->capacity >= 16 ? string->data.heap : string->data.small;
    return data != NULL && memcmp(data, literal, size) == 0 && data[size] == '\0';
}

FIXTURE_API void FixtureSetResultMode(DWORD mode) {
    if (mode <= FIXTURE_RESULT_LARGE) InterlockedExchange(&result_mode, (LONG)mode);
}

FIXTURE_API void FixtureSetCacheNonempty(BOOL nonempty) {
    uint64_t count = nonempty ? 1 : 0;
    AcquireSRWLockExclusive(&state_lock);
    memcpy(cache_state + 0xA0, &count, sizeof(count));
    InterlockedExchange(&counters.nonempty_cache, nonempty ? 1 : 0);
    ReleaseSRWLockExclusive(&state_lock);
}

/* Result strings use a separate registry. A sized delete must name the exact
   raw allocation and byte count; arbitrary pointers can never reach HeapFree. */
FIXTURE_API void FixtureSizedFree(void *pointer, size_t size) {
    size_t i;
    BOOL found = FALSE;
    AcquireSRWLockExclusive(&result_lock);
    for (i = 0; i < sizeof(result_registrations) / sizeof(result_registrations[0]); ++i) {
        ResultRegistration *entry = &result_registrations[i];
        if (entry->active && entry->raw == pointer) {
            found = TRUE;
            if (entry->allocation_size != size) {
                InterlockedIncrement(&counters.result_free_bad_size);
            } else if (HeapFree(GetProcessHeap(), 0, entry->raw)) {
                entry->active = FALSE;
                if (entry->large) InterlockedIncrement(&counters.result_large_frees);
                else InterlockedIncrement(&counters.result_heap_frees);
            }
            break;
        }
    }
    if (!found) InterlockedIncrement(&counters.result_free_bad_size);
    ReleaseSRWLockExclusive(&result_lock);
}

static void make_submit_result(FixtureSubmitResult *out) {
    static const char diagnostic[] = "fixture_result_value";
    _Static_assert(sizeof(diagnostic) - 1 == 20, "fixed diagnostic length");
    FixtureString32 *string;
    DWORD mode = (DWORD)InterlockedCompareExchange(&result_mode, 0, 0);
    void *raw = NULL;
    size_t allocation_size;
    size_t i;
    BOOL registered = FALSE;
    if (out == NULL) return;
    memset(out, 0, sizeof(*out));
    {
        FixtureSharedRef payload;
        payload.object = &result_object;
        payload.control = make_control(payload.object, 2);
        if (payload.control != NULL) memcpy(out->bytes + 0x28, &payload, sizeof(payload));
        else InterlockedIncrement(&counters.result_alloc_failures);
    }
    string = (FixtureString32 *)(out->bytes + 8);
    string->capacity = 15;
    if (mode == FIXTURE_RESULT_SSO) {
        memcpy(string->data.small, "ok", 3);
        string->size = 2;
        return;
    }
    allocation_size = mode == FIXTURE_RESULT_LARGE ? 0xFFF + 0x28 : 32;
    raw = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, allocation_size);
    if (raw == NULL) goto fail;
    AcquireSRWLockExclusive(&result_lock);
    for (i = 0; i < sizeof(result_registrations) / sizeof(result_registrations[0]); ++i) {
        ResultRegistration *entry = &result_registrations[i];
        if (!entry->active) {
            entry->raw = raw;
            entry->allocation_size = allocation_size;
            entry->large = mode == FIXTURE_RESULT_LARGE;
            entry->active = TRUE;
            registered = TRUE;
            break;
        }
    }
    ReleaseSRWLockExclusive(&result_lock);
    if (!registered) {
        HeapFree(GetProcessHeap(), 0, raw);
        goto fail;
    }
    string->data.heap = (char *)raw + (mode == FIXTURE_RESULT_LARGE ? 0x20 : 0);
    string->size = sizeof(diagnostic) - 1;
    string->capacity = mode == FIXTURE_RESULT_LARGE ? 0xFFF : 31;
    if (mode == FIXTURE_RESULT_LARGE) {
        memcpy(string->data.heap - 8, &raw, sizeof(raw));
        InterlockedIncrement(&counters.result_large_allocs);
    } else InterlockedIncrement(&counters.result_heap_allocs);
    memcpy(string->data.heap, diagnostic, sizeof(diagnostic));
    return;
fail:
    InterlockedIncrement(&counters.result_alloc_failures);
}

FIXTURE_API char FixtureLastKey[128];
FIXTURE_API char FixtureLastBody[65537];
FIXTURE_API FixtureSubmitResult *FixtureSubmit(void *context, FixtureSubmitResult *out,
    FixtureSharedRef *owned) {
    BOOL live_text = claim_text_input(owned);
    BOOL valid = FALSE;
    BOOL thread_valid = GetCurrentThreadId() == counters.worker_tid;
    LONG number = InterlockedIncrement(&counters.submit_calls);
    make_submit_result(out);
    if (!thread_valid) InterlockedIncrement(&counters.bad_thread);
    (void)number;
    if (live_text) {
        const unsigned char *text = (const unsigned char *)owned->object;
        DWORD type;
        DWORD tag;
        uint64_t body_length;
        memcpy(&type, text + 0xD8, sizeof(type));
        memcpy(&tag, text + 0x9C, sizeof(tag));
        memcpy(&body_length, text + 0x188, sizeof(body_length));
        valid = context == &context3_object &&
            *(const uintptr_t *const *)text == FixtureTextVtable &&
            owned->control->vtable == &FixtureTextControlVtable &&
            text_string(owned->object, 0xB0)->size>0 && text_string(owned->object, 0xB0)->size<128 &&
            text_string(owned->object, 0x678)->size==body_length && body_length>0 && body_length<=65536 &&
            string_equals(text_string(owned->object, 0x618), "fixture_auto_id", 15) &&
            type == 1 && tag == 1;
        if (valid) {
            const FixtureString32 *key=text_string(owned->object,0xB0),*body=text_string(owned->object,0x678);
            memcpy(FixtureLastKey,key->capacity>=16?key->data.heap:key->data.small,(size_t)key->size+1);
            memcpy(FixtureLastBody,body->capacity>=16?body->data.heap:body->data.small,(size_t)body->size+1);
        }
    }
    if (valid) InterlockedIncrement(&counters.field_valid);
    else InterlockedIncrement(&counters.submit_bad_fields);
    if (valid && thread_valid) InterlockedIncrement(&counters.submit_accepted);
    /* Consume exactly one strong reference, deliberately preserving both pair words. */
    if (live_text && InterlockedDecrement(&owned->control->strong) == 0) {
        FixtureCtrl *ctrl = owned->control;
        ctrl->vtable->destroy(ctrl);
        if (InterlockedDecrement(&ctrl->weak) == 0) ctrl->vtable->delete_this(ctrl);
    }
    return out;
}

FIXTURE_API void FixtureSetExpectedTidMismatch(BOOL mismatch) {
    InterlockedExchange((volatile LONG *)&FixtureExpectedWorkerTid,
        (LONG)(counters.worker_tid + (mismatch ? 1U : 0U)));
}

FIXTURE_API void FixtureSetDelay(DWORD value) {worker_delay=value;}

FIXTURE_API BOOL FixtureStart(DWORD delay_ms) {
    DWORD thread_id = 0;
    BOOL success = FALSE;
    AcquireSRWLockExclusive(&state_lock);
    if (worker_thread != NULL || counters.started != 0 || delay_ms > 5000) goto done;
    memset(&counters, 0, sizeof(counters));
    counters.struct_size = sizeof(counters);
    counters.version = 3;
    counters.main_tid = GetCurrentThreadId();
    {
        uintptr_t value = (uintptr_t)FixtureSendContextVtable;
        memcpy(context3_object, &value, sizeof(value));
        value = (uintptr_t)cache_state;
        memcpy(context3_object + 0xB78, &value, sizeof(value));
        cache_sentinel[0] = cache_sentinel[1] = (uintptr_t)cache_sentinel;
        value = (uintptr_t)cache_sentinel;
        memcpy(cache_state + 0x98, &value, sizeof(value));
        memset(cache_state + 0xA0, 0, sizeof(uint64_t));
    }
    worker_delay = delay_ms;
    stopping = FALSE;
    work_event = CreateEventW(NULL, FALSE, FALSE, NULL);
    stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (work_event == NULL || stop_event == NULL) goto fail;
    queue_object.executor = &queue_object;
    worker_thread = CreateThread(NULL, 0, run_queue, NULL, 0, &thread_id);
    if (worker_thread == NULL) goto fail;
    counters.worker_tid = thread_id;
    FixtureExpectedWorkerTid = thread_id;
    InterlockedExchange(&counters.started, 1);
    success = TRUE;
    goto done;
fail:
    if (work_event != NULL) CloseHandle(work_event);
    if (stop_event != NULL) CloseHandle(stop_event);
    work_event = NULL;
    stop_event = NULL;
done:
    ReleaseSRWLockExclusive(&state_lock);
    return success;
}

FIXTURE_API void FixtureStop(void) {
    HANDLE thread;
    AcquireSRWLockExclusive(&state_lock);
    thread = worker_thread;
    if (thread != NULL) {
        stopping = TRUE;
        SetEvent(stop_event);
    }
    ReleaseSRWLockExclusive(&state_lock);
    if (thread == NULL) return;
    /* Retain handles and module state if a callback does not finish promptly. */
    if (WaitForSingleObject(thread, 6000) != WAIT_OBJECT_0) return;
    AcquireSRWLockExclusive(&state_lock);
    if (worker_thread == thread) {
        /* Publish safe-to-unload only after the actual thread has terminated. */
        InterlockedExchange(&counters.stopped, 1);
        CloseHandle(worker_thread);
        CloseHandle(work_event);
        CloseHandle(stop_event);
        worker_thread = NULL;
        work_event = NULL;
        stop_event = NULL;
        queue_object.executor = NULL;
    }
    ReleaseSRWLockExclusive(&state_lock);
}

FIXTURE_API BOOL FixtureGetStats(FixtureStats *out) {
    if (out == NULL) return FALSE;
    AcquireSRWLockShared(&state_lock);
    *out = counters;
    ReleaseSRWLockShared(&state_lock);
    return TRUE;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)module;
    (void)reason;
    (void)reserved;
    return TRUE;
}
