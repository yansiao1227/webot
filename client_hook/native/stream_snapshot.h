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

/* 在原始回调修改记录前同步采集。
 * 返回 1：UTF-8 文本完整，且端点在白名单中或使用全量模式；
 * 返回 0：端点有效但不在白名单中，不读取正文；
 * 返回 -1：采集被拒绝或不完整。
 * 可使用包含 1 至 64 个原生 wxid_/chatroom ID 的白名单；NULL/count=0
 * 表示全量模式，调用方仍需检查入站方向。仅返回 1 时填充 out 的数据字段，
 * 每次返回都填充诊断字段。
 * 端点偏移仅描述采集证据，不表示发送者或接收者语义。
 * raw_c/raw58 同样保留原生偏移，不推断语义。
 * 正文最多 65536 个 UTF-8 字节，标识符和候选值最多 511 字节。
 * 可选原生 msgsource +0x180 最多 16384 字节。无法读取 source 时
 * 不丢弃文本，但 source180_complete 保持为假。
 * 读取总预算为 90000 字节。
 * 每次同步 ReadProcessMemory 调用前后检查 100 毫秒时限；
 * 操作系统可能使单次调用超出该时限。 */
int stream_snapshot_capture(HANDLE process, uintptr_t record,
                            uintptr_t expected_vt,
                            const char *const *allowlist, size_t count,
                            StreamSample *out);

/* 写出一个带引号的 JSON 字符串；输入必须已经是有效 UTF-8。 */
void stream_json_escape(FILE *file, const char *value, size_t length);

#endif
