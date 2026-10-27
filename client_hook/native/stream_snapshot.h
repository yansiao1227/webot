#ifndef STREAM_SNAPSHOT_H
#define STREAM_SNAPSHOT_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <windows.h>

typedef struct {
    char body[65537];
    char endpoint18[512];
    char endpoint38[512];
    char raw58[512];
    char source180[16385];
    size_t source180_len;
    int source180_complete;
    uint32_t raw_c;
    uint64_t raw108;
    uint64_t raw110;
    uint32_t created134;
    int identity_scalars_complete;
    char client_id[129];
    size_t client_id_len;
    size_t body_len;
    size_t id18_len;
    size_t id38_len;
    size_t raw58_len;
    size_t requested;
    size_t transferred;
    uint64_t elapsed_ms;
    const char *reason;
} StreamSample;

/*
 * Capture synchronously before the original callback mutates the record.
 * Returns 1: complete UTF-8 text and an endpoint in allowlist, or all-mode;
 *         0: valid endpoints outside allowlist (body is never read);
 *        -1: rejected or incomplete capture.
 * An allowlist of 1..64 native wxid_/chatroom IDs may be used; NULL/count=0
 * selects all-mode (caller still checks incoming direction). Fields in out
 * are populated only on return 1; diagnostics are populated on every return.
 * The endpoint offsets describe evidence, not sender/recipient semantics.
 * raw_c/raw58 also retain their native offsets without inferred semantics.
 * Body limit is 65536 UTF-8 bytes, identifiers/candidate are limited to 511;
 * Optional native msgsource +0x180 is bounded to 16384 bytes. An unreadable
 * source does not discard text, but source180_complete stays false.
 * Total reads have a 90000-byte budget.
 * The 100 ms bound is checked around each synchronous ReadProcessMemory call;
 * the operating system can delay a call beyond that deadline.
 */
int stream_snapshot_capture(HANDLE process, uintptr_t record,
                            uintptr_t expected_vt,
                            const char *const *allowlist, size_t count,
                            StreamSample *out);

/* Write one quoted JSON string. Input must already be valid UTF-8. */
void stream_json_escape(FILE *file, const char *value, size_t length);

#endif
