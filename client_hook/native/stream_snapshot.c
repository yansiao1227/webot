#ifndef __USE_MINGW_ANSI_STDIO
#define __USE_MINGW_ANSI_STDIO 1
#endif
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include "stream_snapshot.h"

#include <string.h>

#if UINTPTR_MAX != UINT64_MAX
#error stream_snapshot requires a 64-bit build
#endif

#define RECORD_SIZE ((size_t)0x298)
#define READ_BUDGET ((size_t)90000)
#define DEADLINE_MS ((ULONGLONG)100)
#define STRING_LIMIT ((size_t)65536)
#define ALLOWLIST_LIMIT ((size_t)64)
#define USER_MIN ((uintptr_t)0x10000)
#define USER_MAX ((uintptr_t)0x00007ffffffeffffULL)

typedef struct {
    HANDLE process;
    ULONGLONG started;
    StreamSample *sample;
    const char *failure;
} ReadContext;

typedef struct {
    unsigned char raw[32];
    uintptr_t address;
    uintptr_t data;
    uint64_t length;
    uint64_t capacity;
} StringHeader;

#ifdef STREAM_SNAPSHOT_TESTING
/* Test-only hooks are absent from a normal backend build. */
static ULONGLONG (*test_clock)(void);
static BOOL (*test_read)(HANDLE, LPCVOID, LPVOID, SIZE_T, SIZE_T *);
#endif

static ULONGLONG snapshot_clock(void)
{
#ifdef STREAM_SNAPSHOT_TESTING
    if (test_clock != NULL) return test_clock();
#endif
    return GetTickCount64();
}

static int fail(ReadContext *reader, const char *reason)
{
    if (reader->failure == NULL) reader->failure = reason;
    return 0;
}

static int user_range(uintptr_t address, uint64_t length)
{
    return length != 0 && address >= USER_MIN && address <= USER_MAX &&
           length - 1 <= (uint64_t)(USER_MAX - address);
}

static int deadline_ok(ReadContext *reader)
{
    reader->sample->elapsed_ms = snapshot_clock() - reader->started;
    if (reader->sample->elapsed_ms >= DEADLINE_MS)
        return fail(reader, "deadline_exceeded");
    return reader->failure == NULL;
}

static int read_exact(ReadContext *reader, uintptr_t address,
                      void *destination, size_t length)
{
    SIZE_T transferred = 0;
    BOOL success;
    StreamSample *sample = reader->sample;
    if (reader->failure != NULL) return 0;
    if (!user_range(address, length)) return fail(reader, "invalid_read_range");
    if (sample->requested > READ_BUDGET ||
        length > READ_BUDGET - sample->requested)
        return fail(reader, "read_budget_exceeded");
    if (!deadline_ok(reader)) return 0;
    sample->requested += length;
#ifdef STREAM_SNAPSHOT_TESTING
    if (test_read != NULL)
        success = test_read(reader->process, (LPCVOID)address, destination,
                            length, &transferred);
    else
#endif
        success = ReadProcessMemory(reader->process, (LPCVOID)address,
                                    destination, length, &transferred);
    if (transferred > length) return fail(reader, "invalid_read_result");
    sample->transferred += (size_t)transferred;
    if (!deadline_ok(reader)) return 0;
    if (!success || transferred != length) return fail(reader, "read_failed");
    return 1;
}

static int strict_utf8(const char *value, size_t length)
{
    size_t index = 0;
    while (index < length) {
        unsigned char first = (unsigned char)value[index++];
        uint32_t codepoint;
        uint32_t minimum;
        size_t remaining;
        if (first == 0) return 0;
        if (first < 0x80) continue;
        if (first >= 0xc2 && first <= 0xdf) {
            codepoint = first & 0x1f; minimum = 0x80; remaining = 1;
        } else if (first >= 0xe0 && first <= 0xef) {
            codepoint = first & 0x0f; minimum = 0x800; remaining = 2;
        } else if (first >= 0xf0 && first <= 0xf4) {
            codepoint = first & 0x07; minimum = 0x10000; remaining = 3;
        } else return 0;
        if (remaining > length - index) return 0;
        while (remaining-- != 0) {
            unsigned char next = (unsigned char)value[index++];
            if ((next & 0xc0) != 0x80) return 0;
            codepoint = (codepoint << 6) | (next & 0x3f);
        }
        if (codepoint < minimum || codepoint > 0x10ffff ||
            (codepoint >= 0xd800 && codepoint <= 0xdfff)) return 0;
    }
    return 1;
}

static int id_character(unsigned char value)
{
    return (value >= 'a' && value <= 'z') ||
           (value >= 'A' && value <= 'Z') ||
           (value >= '0' && value <= '9') || value == '_' || value == '-';
}

static int allowed_id(const char *value, size_t length)
{
    size_t index, end;
    if (length > 5 && memcmp(value, "wxid_", 5) == 0) {
        for (index = 5; index < length; ++index)
            if (!id_character((unsigned char)value[index])) break;
        if (index == length) return 1;
    }
    if (length <= 9 || memcmp(value + length - 9, "@chatroom", 9) != 0)
        return 0;
    end = length - 9;
    for (index = 0; index < end; ++index)
        if (!id_character((unsigned char)value[index])) return 0;
    return 1;
}

static int allowlist_valid(const char *const *allowlist, size_t count,
                           size_t *lengths)
{
    size_t index, length;
    if (allowlist == NULL || count == 0 || count > ALLOWLIST_LIMIT) return 0;
    for (index = 0; index < count; ++index) {
        if (allowlist[index] == NULL) return 0;
        for (length = 0; length <= STRING_LIMIT; ++length)
            if (allowlist[index][length] == '\0') break;
        if (length > STRING_LIMIT ||
            !strict_utf8(allowlist[index], length) ||
            !allowed_id(allowlist[index], length)) return 0;
        lengths[index] = length;
    }
    return 1;
}

static int read_header(ReadContext *reader, uintptr_t address,
                       StringHeader *header)
{
    header->address = address;
    if (!read_exact(reader, address, header->raw, sizeof(header->raw))) return 0;
    memcpy(&header->length, header->raw + 0x10, sizeof(header->length));
    memcpy(&header->capacity, header->raw + 0x18, sizeof(header->capacity));
    if (header->length > header->capacity)
        return fail(reader, "string_length_exceeds_capacity");
    if (header->length > STRING_LIMIT)
        return fail(reader, "string_limit_exceeded");
    if (header->capacity < 16) {
        if (header->length > 15) return fail(reader, "invalid_inline_length");
        header->data = address;
    } else {
        memcpy(&header->data, header->raw, sizeof(header->data));
        if (header->capacity == UINT64_MAX ||
            !user_range(header->data, header->capacity + 1))
            return fail(reader, "invalid_heap_range");
    }
    if (!user_range(header->data, header->length + 1))
        return fail(reader, "invalid_string_range");
    return 1;
}

static int read_string(ReadContext *reader, const StringHeader *header,
                       char *destination)
{
    size_t length = (size_t)header->length;
    if (header->capacity < 16)
        memcpy(destination, header->raw, length + 1);
    else if (!read_exact(reader, header->data, destination, length + 1)) return 0;
    if (destination[length] != '\0')
        return fail(reader, "string_terminator_missing");
    if (!strict_utf8(destination, length)) return fail(reader, "invalid_utf8");
    return 1;
}

static int final_check(ReadContext *reader, uintptr_t record,
                       uintptr_t expected_vt, const StringHeader *headers,
                       size_t count, const uint32_t *expected_raw_c)
{
    uintptr_t vtable;
    uint32_t raw_c;
    unsigned char raw[32];
    size_t index;
    if (!read_exact(reader, record, &vtable, sizeof(vtable))) return 0;
    if (vtable != expected_vt) return fail(reader, "record_changed");
    for (index = 0; index < count; ++index) {
        if (!read_exact(reader, headers[index].address, raw, sizeof(raw))) return 0;
        if (memcmp(raw, headers[index].raw, sizeof(raw)) != 0)
            return fail(reader, "record_changed");
    }
    if (expected_raw_c != NULL) {
        if (!read_exact(reader, record + 0xc, &raw_c, sizeof(raw_c))) return 0;
        if (raw_c != *expected_raw_c) return fail(reader, "record_changed");
    }
    return deadline_ok(reader);
}

int stream_snapshot_capture(HANDLE process, uintptr_t record,
                            uintptr_t expected_vt,
                            const char *const *allowlist, size_t count,
                            StreamSample *out)
{
    ReadContext reader;
    StringHeader headers[4];
    size_t lengths[ALLOWLIST_LIMIT], index;
    uintptr_t vtable;
    int result = -1, matched = count == 0;
    if (out == NULL) return -1;
    memset(out, 0, sizeof(*out));
    memset(headers, 0, sizeof(headers));
    memset(&reader, 0, sizeof(reader));
    reader.process = process;
    reader.sample = out;
    reader.started = snapshot_clock();
    if (count && !allowlist_valid(allowlist, count, lengths)) {
        fail(&reader, "invalid_allowlist"); goto done;
    }
    if (process == NULL) {
        fail(&reader, "invalid_process_handle"); goto done;
    }
    if (!user_range(record, RECORD_SIZE) || (record & 7) != 0) {
        fail(&reader, "invalid_record_address"); goto done;
    }
    if (!user_range(expected_vt, sizeof(uintptr_t)) || (expected_vt & 7) != 0) {
        fail(&reader, "invalid_expected_vtable"); goto done;
    }
    if (!read_exact(&reader, record, &vtable, sizeof(vtable))) goto done;
    if (vtable != expected_vt) {
        fail(&reader, "record_vtable_mismatch"); goto done;
    }
    if (!read_header(&reader, record + 0x18, &headers[0]) ||
        (headers[0].length > 511 || !read_string(&reader, &headers[0], out->endpoint18)) ||
        !read_header(&reader, record + 0x38, &headers[1]) ||
        (headers[1].length > 511 || !read_string(&reader, &headers[1], out->endpoint38))) goto done;
    out->id18_len = (size_t)headers[0].length;
    out->id38_len = (size_t)headers[1].length;
    if (!allowed_id(out->endpoint18, out->id18_len) ||
        !allowed_id(out->endpoint38, out->id38_len)) {
        fail(&reader, "invalid_endpoint_id"); goto done;
    }
    for (index = 0; index < count; ++index) {
        if ((lengths[index] == out->id18_len &&
             memcmp(allowlist[index], out->endpoint18, out->id18_len) == 0) ||
            (lengths[index] == out->id38_len &&
             memcmp(allowlist[index], out->endpoint38, out->id38_len) == 0)) {
            matched = 1; break;
        }
    }
    if (!matched) {
        if (!final_check(&reader, record, expected_vt, headers, 2, NULL)) goto done;
        result = 0; goto done;
    }
    if (!read_exact(&reader, record + 0xc, &out->raw_c, sizeof(out->raw_c)) ||
        !read_header(&reader, record + 0x58, &headers[2]) ||
        (headers[2].length > 511 || !read_string(&reader, &headers[2], out->raw58)) ||
        !read_header(&reader, record + 0x160, &headers[3]) ||
        !read_string(&reader, &headers[3], out->body) ||
        !final_check(&reader, record, expected_vt, headers, 4, &out->raw_c))
        goto done;
    out->raw58_len = (size_t)headers[2].length;
    out->body_len = (size_t)headers[3].length;
    /* Copy the constructor's input+0x50 scalar. Its server-id semantics are
       verified separately against native reference svrid, never inferred here. */
    {
        uint64_t after;
        if (!read_exact(&reader, record + 0x108, &out->raw108, sizeof(out->raw108)) ||
            !read_exact(&reader, record + 0x108, &after, sizeof(after)) || after != out->raw108) {
            fail(&reader, "record_scalar_changed"); goto done;
        }
    }
    /* Separate native field, never derived from text. Capture before the
       original finalizer mutates/destroys the borrowed record. Optional read
       errors leave mention status unknown without losing the text event. */
    {
        ReadContext optional = reader;
        StringHeader source;
        optional.failure = NULL;
        if (read_header(&optional, record + 0x180, &source) &&
            source.length <= 16384 && read_string(&optional, &source, out->source180) &&
            final_check(&optional, record, expected_vt, &source, 1, &out->raw_c)) {
            out->source180_len = (size_t)source.length;
            out->source180_complete = 1;
        } else {
            memset(out->source180, 0, sizeof(out->source180));
        }
    }
    /* Some non-class2 Task branches copy TextInput+618 here. Ordinary text
       does not; an empty field must never be synthesized from body text. */
    {
        ReadContext optional = reader;
        StringHeader client;
        optional.failure = NULL;
        if (read_header(&optional, record + 0x278, &client) && client.length <= 128 &&
            read_string(&optional, &client, out->client_id) &&
            final_check(&optional, record, expected_vt, &client, 1, &out->raw_c))
            out->client_id_len = (size_t)client.length;
        else memset(out->client_id, 0, sizeof(out->client_id));
    }
    {
        ReadContext optional=reader;
        uint64_t first110=0,again110=0;
        uint32_t first134=0,again134=0;
        optional.failure=NULL;
        if (read_exact(&optional,record+0x110,&first110,8) &&
            read_exact(&optional,record+0x134,&first134,4) &&
            read_exact(&optional,record+0x110,&again110,8) &&
            read_exact(&optional,record+0x134,&again134,4) &&
            first110==again110 && first134==again134) {
            out->raw110=first110;out->created134=first134;
            out->identity_scalars_complete=1;
        }
    }
    result = 1;

done:
    if (reader.failure != NULL) result = -1;
    out->elapsed_ms = snapshot_clock() - reader.started;
    if (out->elapsed_ms >= DEADLINE_MS && reader.failure == NULL) {
        fail(&reader, "deadline_exceeded");
        result = -1;
    }
    out->reason = reader.failure != NULL ? reader.failure :
                  result == 1 ? "allowlisted_message" : "endpoint_not_allowlisted";
    if (result != 1) {
        memset(out->body, 0, sizeof(out->body));
        memset(out->endpoint18, 0, sizeof(out->endpoint18));
        memset(out->endpoint38, 0, sizeof(out->endpoint38));
        memset(out->raw58, 0, sizeof(out->raw58));
        memset(out->source180, 0, sizeof(out->source180));
        out->source180_len = 0;
        out->source180_complete = 0;
        out->raw_c = 0;
        out->raw108 = 0;
        out->raw110 = 0;out->created134=0;out->identity_scalars_complete=0;
        memset(out->client_id, 0, sizeof(out->client_id));
        out->client_id_len = 0;
        out->body_len = out->id18_len = out->id38_len = out->raw58_len = 0;
    }
    return result;
}

void stream_json_escape(FILE *file, const char *value, size_t length)
{
    size_t index;
    fputc('"', file);
    for (index = 0; index < length; ++index) {
        unsigned char current = (unsigned char)value[index];
        switch (current) {
        case '"': fputs("\\\"", file); break;
        case '\\': fputs("\\\\", file); break;
        case '\b': fputs("\\b", file); break;
        case '\f': fputs("\\f", file); break;
        case '\n': fputs("\\n", file); break;
        case '\r': fputs("\\r", file); break;
        case '\t': fputs("\\t", file); break;
        default:
            if (current < 0x20) fprintf(file, "\\u%04x", (unsigned int)current);
            else fputc(current, file);
            break;
        }
    }
    fputc('"', file);
}
