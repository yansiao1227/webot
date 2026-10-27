#ifndef CLIENT_HOOK_WEIXIN41930_H
#define CLIENT_HOOK_WEIXIN41930_H
#include <stddef.h>
#include <stdint.h>
#if UINTPTR_MAX != UINT64_MAX
#error This profile requires Windows x64
#endif

/* Binary layout only. Never construct/destroy the client's C++ objects. */
typedef struct {
    union { char inline_bytes[16]; uintptr_t heap; } storage;
    uint64_t length;
    uint64_t capacity;
} Wx41930String;

typedef struct {
    uintptr_t vtable;                          /* 000 */
    uint32_t unknown_008, raw_kind;            /* 008,00c */
    uint8_t unknown_010[8];
    Wx41930String endpoint_a;                  /* 018 */
    Wx41930String endpoint_b;                  /* 038 */
    Wx41930String actor_candidate;             /* 058 */
    uint8_t unknown_078[0x90];
    uint64_t raw_108, raw_110, cached_118;       /* no msg-id/time claims */
    uint8_t unknown_120[0x40];
    Wx41930String text;                        /* 160 */
    Wx41930String source;                      /* 180, optional native msgsource */
    uint8_t unknown_1a0[0x98];
    uintptr_t parsed_object, parsed_control;    /* 238,240 */
    uintptr_t source_object, source_control;    /* 248,250 */
    uint8_t unknown_258[0x18];
    uint8_t parsed_flag;                       /* 270 */
    uint8_t unknown_271[0x27];
} Wx41930Record;

/* MS x64 ABI: RCX=borrowed record, DL=force (only low byte consumed), void.
 * Do not substitute int/bool return or the constructor's Record* return.
 * GCC's Windows x64 default and MSVC's default both use this platform ABI. */
typedef void (*Wx41930FinalizeFn)(Wx41930Record *record, uint8_t force);
typedef Wx41930Record *(*Wx41930ConstructFn)(Wx41930Record *out, const void *input);

#define WX41930_FINALIZE_RVA ((uintptr_t)0x9dbbf0)
#define WX41930_ALLOWED_RETURN_RVA ((uintptr_t)0x9dacd5)
#define WX41930_RECORD_VTABLE_RVA ((uintptr_t)0x8096da8)

static inline int wx41930_route_matches(uintptr_t base, uintptr_t return_address,
                                      uint8_t force, uintptr_t vtable) {
    return force == 1 && return_address >= base && vtable >= base &&
           return_address - base == WX41930_ALLOWED_RETURN_RVA &&
           vtable - base == WX41930_RECORD_VTABLE_RVA;
}

_Static_assert(sizeof(Wx41930String) == 0x20, "native narrow string size");
_Static_assert(offsetof(Wx41930String, length) == 0x10, "native string length");
_Static_assert(offsetof(Wx41930String, capacity) == 0x18, "native string capacity");
_Static_assert(sizeof(Wx41930Record) == 0x298, "record size");
#define WX_ASSERT_FIELD(field, offset) _Static_assert(offsetof(Wx41930Record, field) == offset, #field)
WX_ASSERT_FIELD(raw_kind, 0xc);
WX_ASSERT_FIELD(endpoint_a, 0x18);
WX_ASSERT_FIELD(endpoint_b, 0x38);
WX_ASSERT_FIELD(actor_candidate, 0x58);
WX_ASSERT_FIELD(raw_108, 0x108);
WX_ASSERT_FIELD(raw_110, 0x110);
WX_ASSERT_FIELD(cached_118, 0x118);
WX_ASSERT_FIELD(text, 0x160);
WX_ASSERT_FIELD(source, 0x180);
WX_ASSERT_FIELD(parsed_object, 0x238);
WX_ASSERT_FIELD(source_object, 0x248);
WX_ASSERT_FIELD(parsed_flag, 0x270);
#undef WX_ASSERT_FIELD
#endif
