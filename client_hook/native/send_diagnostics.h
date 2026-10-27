#ifndef NATIVE_PROTOCOL_H
#define NATIVE_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>

#define BRIDGE_MAGIC UINT32_C(0x4e535032)
#define BRIDGE_VERSION UINT32_C(2)
#define BRIDGE_OP_PREPARE UINT32_C(3)
#define BRIDGE_OP_SEND UINT32_C(4)
#define BRIDGE_FLAG_FIXTURE UINT32_C(1)
#define BRIDGE_STATUS_OK UINT32_C(0)
#define BRIDGE_STATUS_REJECTED UINT32_C(1)
#define BRIDGE_STATUS_FAILED UINT32_C(2)
#define BRIDGE_STATUS_PENDING UINT32_C(3)

typedef struct bridge_config {
    uint32_t magic, version, size, op;
    uint32_t allowed_pid, flags;
    uint64_t module_base, module_size, bridge_base;
    uint32_t actual_pid, actual_tid, status, win32_error;
    uint32_t callback_tid, invocation_count, queue_gate, posted;
    uint32_t callback_done, callback_pending, fn_consumed, queue_ref_released;
    uint32_t result_ref_released, profile_kind, wait_ms, error_phase;
    uint32_t context_vtable_rva, factory_vtable_rva, factory_control_vtable_rva, fields_verified;
    uint64_t key_length, key_capacity, body_length, body_capacity, tag_length, tag_capacity;
    uint32_t context_release_mask, text_released, native_cleanup_ok, native_prepare_ok;
    uint32_t callback_error, submit_enabled;
    uint32_t cache_checked, cache_empty;
    uint64_t cache_size, reserved;
    uint32_t native_submit_invocations, attempt_claimed, local_status_0, local_status_1;
    uint32_t input_consumed, out_cleaned, local_accepted, attempt_seen;
    uint64_t result_string_length, result_string_capacity;
    uint64_t reserved_v2[2];
} bridge_config;

_Static_assert(sizeof(void *) == 8, "Windows x64 only");
_Static_assert(sizeof(bridge_config) == 288, "native protocol size");
#define NATIVE_OFFSET_ASSERT(field, expected) \
    _Static_assert(offsetof(bridge_config, field) == expected, #field " offset")
NATIVE_OFFSET_ASSERT(magic, 0);
NATIVE_OFFSET_ASSERT(version, 4);
NATIVE_OFFSET_ASSERT(size, 8);
NATIVE_OFFSET_ASSERT(op, 12);
NATIVE_OFFSET_ASSERT(allowed_pid, 16);
NATIVE_OFFSET_ASSERT(flags, 20);
NATIVE_OFFSET_ASSERT(module_base, 24);
NATIVE_OFFSET_ASSERT(module_size, 32);
NATIVE_OFFSET_ASSERT(bridge_base, 40);
NATIVE_OFFSET_ASSERT(actual_pid, 48);
NATIVE_OFFSET_ASSERT(actual_tid, 52);
NATIVE_OFFSET_ASSERT(status, 56);
NATIVE_OFFSET_ASSERT(win32_error, 60);
NATIVE_OFFSET_ASSERT(callback_tid, 64);
NATIVE_OFFSET_ASSERT(invocation_count, 68);
NATIVE_OFFSET_ASSERT(queue_gate, 72);
NATIVE_OFFSET_ASSERT(posted, 76);
NATIVE_OFFSET_ASSERT(callback_done, 80);
NATIVE_OFFSET_ASSERT(callback_pending, 84);
NATIVE_OFFSET_ASSERT(fn_consumed, 88);
NATIVE_OFFSET_ASSERT(queue_ref_released, 92);
NATIVE_OFFSET_ASSERT(result_ref_released, 96);
NATIVE_OFFSET_ASSERT(profile_kind, 100);
NATIVE_OFFSET_ASSERT(wait_ms, 104);
NATIVE_OFFSET_ASSERT(error_phase, 108);
NATIVE_OFFSET_ASSERT(context_vtable_rva, 112);
NATIVE_OFFSET_ASSERT(factory_vtable_rva, 116);
NATIVE_OFFSET_ASSERT(factory_control_vtable_rva, 120);
NATIVE_OFFSET_ASSERT(fields_verified, 124);
NATIVE_OFFSET_ASSERT(key_length, 128);
NATIVE_OFFSET_ASSERT(key_capacity, 136);
NATIVE_OFFSET_ASSERT(body_length, 144);
NATIVE_OFFSET_ASSERT(body_capacity, 152);
NATIVE_OFFSET_ASSERT(tag_length, 160);
NATIVE_OFFSET_ASSERT(tag_capacity, 168);
NATIVE_OFFSET_ASSERT(context_release_mask, 176);
NATIVE_OFFSET_ASSERT(text_released, 180);
NATIVE_OFFSET_ASSERT(native_cleanup_ok, 184);
NATIVE_OFFSET_ASSERT(native_prepare_ok, 188);
NATIVE_OFFSET_ASSERT(callback_error, 192);
NATIVE_OFFSET_ASSERT(submit_enabled, 196);
NATIVE_OFFSET_ASSERT(cache_checked, 200);
NATIVE_OFFSET_ASSERT(cache_empty, 204);
NATIVE_OFFSET_ASSERT(cache_size, 208);
NATIVE_OFFSET_ASSERT(reserved, 216);
NATIVE_OFFSET_ASSERT(native_submit_invocations, 224);
NATIVE_OFFSET_ASSERT(attempt_claimed, 228);
NATIVE_OFFSET_ASSERT(local_status_0, 232);
NATIVE_OFFSET_ASSERT(local_status_1, 236);
NATIVE_OFFSET_ASSERT(input_consumed, 240);
NATIVE_OFFSET_ASSERT(out_cleaned, 244);
NATIVE_OFFSET_ASSERT(local_accepted, 248);
NATIVE_OFFSET_ASSERT(attempt_seen, 252);
NATIVE_OFFSET_ASSERT(result_string_length, 256);
NATIVE_OFFSET_ASSERT(result_string_capacity, 264);
NATIVE_OFFSET_ASSERT(reserved_v2, 272);
#undef NATIVE_OFFSET_ASSERT
#endif
