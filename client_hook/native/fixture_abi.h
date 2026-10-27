#ifndef NATIVE_SEND_FIXTURE_ABI_H
#define NATIVE_SEND_FIXTURE_ABI_H

#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <stddef.h>
#include <stdint.h>

#if defined(FIXTURE_CLIENT_BUILD)
#define FIXTURE_API __declspec(dllexport)
#else
#define FIXTURE_API
#endif

typedef struct FixtureFnImpl FixtureFnImpl;
typedef struct FixtureFn64 FixtureFn64;
typedef struct FixtureCtrl FixtureCtrl;
typedef struct FixtureFnVtable {
    FixtureFnImpl *(*copy)(const FixtureFnImpl *, void *);
    FixtureFnImpl *(*move)(FixtureFnImpl *, void *);
    void (*invoke)(FixtureFnImpl *);
    const void *(*type)(const FixtureFnImpl *);
    void (*delete_this)(FixtureFnImpl *, unsigned char);
} FixtureFnVtable;

struct FixtureFnImpl {
    const FixtureFnVtable *vtable;
    FixtureFn64 *payload;
};

struct FixtureFn64 {
    uintptr_t storage[7];
    FixtureFnImpl *impl;
};

typedef struct FixtureCtrlVtable {
    void (*destroy)(FixtureCtrl *);
    void (*delete_this)(FixtureCtrl *);
} FixtureCtrlVtable;

struct FixtureCtrl {
    const FixtureCtrlVtable *vtable;
    volatile LONG strong;
    volatile LONG weak;
    void *object;
    DWORD kind;
};

typedef struct FixtureSharedRef {
    void *object;
    FixtureCtrl *control;
} FixtureSharedRef;

typedef struct FixtureLocation {
    const void *p0;
    const void *p1;
    DWORD number;
    DWORD padding;
    const void *return_address;
} FixtureLocation;

typedef struct FixtureString32 {
    union {
        char small[16];
        char *heap;
    } data;
    uint64_t size;
    uint64_t capacity;
} FixtureString32;

typedef struct FixtureSubmitResult {
    unsigned char bytes[0x38];
} FixtureSubmitResult;

typedef struct FixtureStats {
    DWORD struct_size;
    DWORD version;
    DWORD main_tid;
    DWORD worker_tid;
    DWORD invoke_tid;
    LONG started;
    LONG stopped;
    LONG posts;
    LONG posts_rejected;
    LONG jobs_queued;
    LONG jobs_completed;
    LONG fn_copies;
    LONG fn_moves;
    LONG fn_deletes;
    LONG fn_heap_allocs;
    LONG fn_heap_frees;
    LONG ctrl_allocs;
    LONG ctrl_destroys;
    LONG ctrl_frees;
    LONG queue_ref_acquires;
    uint64_t location_return;
    LONG pending;
    LONG reserved;
    LONG factory_calls;
    LONG context1_calls;
    LONG context2_calls;
    LONG context3_calls;
    LONG submit_calls;
    LONG text_destroy;
    LONG strings_heap_allocs;
    LONG strings_heap_frees;
    LONG field_valid;
    LONG duplicate_submit;
    LONG bad_thread;
    LONG submit_accepted;
    LONG context_getter_calls;
    LONG string_assign_calls;
    LONG submit_bad_fields;
    LONG text_allocs;
    LONG result_heap_allocs;
    LONG result_heap_frees;
    LONG result_large_allocs;
    LONG result_large_frees;
    LONG result_free_bad_size;
    LONG input_consumed_count;
    LONG input_double_consume_count;
    LONG input_bad_refs;
    LONG result_alloc_failures;
    LONG nonempty_cache;
} FixtureStats;

typedef void *(*FixtureRootFn)(void);
typedef FixtureSharedRef *(*FixtureGetQueueFn)(void *, FixtureSharedRef *);
typedef FixtureLocation *(*FixtureMakeLocationFn)(FixtureLocation *, const void *, const void *, DWORD);
typedef FixtureSharedRef *(*FixturePostFn)(void *, FixtureSharedRef *, const FixtureLocation *, FixtureFn64 *, DWORD);
typedef const FixtureFnVtable *(*FixtureGetFnVtableFn)(void);
typedef BOOL (*FixtureStartFn)(DWORD);
typedef void (*FixtureStopFn)(void);
typedef BOOL (*FixtureGetStatsFn)(FixtureStats *);
typedef void (*FixtureSetExpectedTidMismatchFn)(BOOL);
typedef FixtureSharedRef *(*FixtureGetContext1Fn)(FixtureSharedRef *);
typedef FixtureSharedRef *(*FixtureGetContext2Fn)(void *, FixtureSharedRef *);
typedef FixtureSharedRef *(*FixtureGetContext3Fn)(void *, FixtureSharedRef *);
typedef FixtureSharedRef *(*FixtureTextFactoryFn)(FixtureSharedRef *);
typedef FixtureString32 *(*FixtureStringAssignFn)(FixtureString32 *, size_t, uintptr_t, const char *);
typedef FixtureSubmitResult *(*FixtureSubmitFn)(void *, FixtureSubmitResult *, FixtureSharedRef *);

typedef void (*FixtureSizedFreeFn)(void *, size_t);
typedef void (*FixtureSetResultModeFn)(DWORD);
typedef void (*FixtureSetCacheNonemptyFn)(BOOL);

#define FIXTURE_RESULT_SSO 0U
#define FIXTURE_RESULT_HEAP 1U
#define FIXTURE_RESULT_LARGE 2U

_Static_assert(sizeof(void *) == 8, "Windows x64 required");
_Static_assert(sizeof(FixtureFn64) == 64, "Fn64 ABI");
_Static_assert(offsetof(FixtureFn64, impl) == 0x38, "Fn64 impl ABI");
_Static_assert(sizeof(FixtureFnImpl) == 16, "small native implementation ABI");
_Static_assert(offsetof(FixtureFnImpl, payload) == 8, "non-owning Fn64 reference ABI");
_Static_assert(sizeof(FixtureFnVtable) == 40, "five function slots");
_Static_assert(offsetof(FixtureFnVtable, invoke) == 0x10, "invoke slot ABI");
_Static_assert(offsetof(FixtureFnVtable, delete_this) == 0x20, "delete slot ABI");
_Static_assert(offsetof(FixtureCtrl, strong) == 8, "strong count ABI");
_Static_assert(offsetof(FixtureCtrl, weak) == 12, "weak count ABI");
_Static_assert(sizeof(FixtureSharedRef) == 16, "shared reference ABI");
_Static_assert(sizeof(FixtureLocation) == 32, "metadata ABI");
_Static_assert(offsetof(FixtureLocation, return_address) == 24, "metadata return ABI");
_Static_assert(sizeof(FixtureString32) == 32, "MSVC string ABI");
_Static_assert(offsetof(FixtureString32, size) == 0x10, "string size ABI");
_Static_assert(offsetof(FixtureString32, capacity) == 0x18, "string capacity ABI");
_Static_assert(sizeof(FixtureSubmitResult) == 0x38, "submit result ABI");
_Static_assert(offsetof(FixtureStats, factory_calls) == 96, "stats v2 extension ABI");
_Static_assert(offsetof(FixtureStats, result_heap_allocs) == 160, "stats v3 extension ABI");
_Static_assert(sizeof(FixtureStats) == 200, "stats ABI version 3");

FIXTURE_API void *FixtureRoot(void);
FIXTURE_API FixtureSharedRef *FixtureGetQueue(void *, FixtureSharedRef *);
FIXTURE_API FixtureLocation *FixtureMakeLocation(FixtureLocation *, const void *, const void *, DWORD);
FIXTURE_API FixtureSharedRef *FixturePost(void *, FixtureSharedRef *, const FixtureLocation *, FixtureFn64 *, DWORD);
FIXTURE_API const FixtureFnVtable *FixtureGetFnVtable(void);
FIXTURE_API BOOL FixtureStart(DWORD);
FIXTURE_API void FixtureStop(void);
FIXTURE_API BOOL FixtureGetStats(FixtureStats *);
FIXTURE_API FixtureFnImpl *FixtureFnCopy(const FixtureFnImpl *, void *);
FIXTURE_API FixtureFnImpl *FixtureFnMove(FixtureFnImpl *, void *);
FIXTURE_API void FixtureFnInvoke(FixtureFnImpl *);
FIXTURE_API const void *FixtureFnType(const FixtureFnImpl *);
FIXTURE_API void FixtureFnDelete(FixtureFnImpl *, unsigned char);
FIXTURE_API void FixtureCtrlDestroy(FixtureCtrl *);
FIXTURE_API void FixtureCtrlDelete(FixtureCtrl *);
FIXTURE_API extern const FixtureFnVtable FixtureNativeVtable;
FIXTURE_API extern const FixtureCtrlVtable FixtureControlVtable;
FIXTURE_API extern const FixtureCtrlVtable FixtureQueueControlVtable;
FIXTURE_API extern const FixtureCtrlVtable FixtureResultControlVtable;
FIXTURE_API FixtureSharedRef *FixtureGetContext1(FixtureSharedRef *);
FIXTURE_API FixtureSharedRef *FixtureGetContext2(void *, FixtureSharedRef *);
FIXTURE_API FixtureSharedRef *FixtureGetContext3(void *, FixtureSharedRef *);
FIXTURE_API FixtureSharedRef *FixtureTextFactory(FixtureSharedRef *);
FIXTURE_API FixtureString32 *FixtureStringAssign(FixtureString32 *, size_t, uintptr_t, const char *);
FIXTURE_API FixtureSubmitResult *FixtureSubmit(void *, FixtureSubmitResult *, FixtureSharedRef *);
FIXTURE_API void FixtureSizedFree(void *, size_t);
FIXTURE_API void FixtureSetResultMode(DWORD);
FIXTURE_API void FixtureSetCacheNonempty(BOOL);
FIXTURE_API extern const FixtureCtrlVtable FixtureContext1ControlVtable;
FIXTURE_API extern const FixtureCtrlVtable FixtureContext2ControlVtable;
FIXTURE_API extern const FixtureCtrlVtable FixtureContext3ControlVtable;
FIXTURE_API extern const FixtureCtrlVtable FixtureTextControlVtable;
FIXTURE_API extern const uintptr_t FixtureSendContextVtable[1];
FIXTURE_API extern const uintptr_t FixtureTextVtable[1];
FIXTURE_API extern volatile DWORD FixtureExpectedWorkerTid;
FIXTURE_API void FixtureSetExpectedTidMismatch(BOOL);

#endif
