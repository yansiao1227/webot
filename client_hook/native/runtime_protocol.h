#ifndef CLIENT_HOOK_RUNTIME_PROTOCOL_H
#define CLIENT_HOOK_RUNTIME_PROTOCOL_H
#include <windows.h>
#include <stdint.h>
#define CH_MAGIC 0x43485231u
typedef struct {
    uint32_t magic, version, size, pid;
    uint64_t creation_time;
    uint32_t duration_ms, max_events;
    uint32_t owner_pid, reserved;
    uint64_t owner_creation;
    wchar_t exe_path[260], core_path[260], output_path[512], stop_name[128];
    char endpoint[128];
} ChConfig;
_Static_assert(sizeof(ChConfig) == 2496, "runtime config ABI v3");
enum { CH_IDLE=1, CH_STARTING=2, CH_RUNNING=3, CH_STOPPING=4, CH_DRAINING=5, CH_FAULT=6 };
typedef struct {
    uint32_t magic, version, size, state;
    uint64_t generation, completed;
    uint32_t target_pid, owner_pid;
    volatile LONG callbacks, admitted;
    LONG entry_hits, observed, filtered, rejected, dropped, cancelled;
    LONG run_busy, installed;
    uint64_t last_return_rva;
    LONG identity_changed, reserved;
    char self_id[128];
} ChPublicStatus;
_Static_assert(sizeof(ChPublicStatus) == 224, "public status ABI v3");
#endif
