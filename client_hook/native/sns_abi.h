#ifndef CH_SNS_ABI_H
#define CH_SNS_ABI_H

/* 微信 4.1.9.30 x64 的 SNS 对象操作与结果布局。
 * 本文件不选择进程、加载模块或调用网络；地址仍须由调用侧固定 profile 校验。
 * UI 的 unlike=1 与协议对象操作 unlike=5 不属于同一枚举。
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

enum { CH_SNS_OBJECT_OP_UNLIKE = 5 };

typedef struct ChSnsObjectOp {
    uintptr_t vtable;
    uint64_t post_id;
    uint64_t opaque_metadata;
    uint32_t operation;
    uint32_t unknown_1c;
    uint32_t presence;
    uint32_t padding_24;
} ChSnsObjectOp;

typedef struct ChSnsNativeString {
    union { unsigned char small[16]; uintptr_t pointer; } data;
    uint64_t size;
    uint64_t capacity;
} ChSnsNativeString;

typedef struct ChSnsSharedRef {
    uintptr_t object;
    uintptr_t control;
} ChSnsSharedRef;

typedef struct ChSnsNativeResult {
    int32_t status_0;
    int32_t status_1;
    ChSnsNativeString text;
    ChSnsSharedRef response;
} ChSnsNativeResult;

typedef ChSnsObjectOp *(*ChSnsObjectOpCtor)(ChSnsObjectOp *);
typedef ChSnsNativeResult *(*ChSnsObjectOpExecute)(
    uint64_t, ChSnsNativeResult *, const ChSnsObjectOp *);

_Static_assert(sizeof(uintptr_t) == 8, "SNS ABI requires x64");
_Static_assert(sizeof(ChSnsObjectOp) == 0x28, "SNS object operation size");
_Static_assert(offsetof(ChSnsObjectOp, post_id) == 0x08, "SNS post ID offset");
_Static_assert(offsetof(ChSnsObjectOp, operation) == 0x18, "SNS operation offset");
_Static_assert(offsetof(ChSnsObjectOp, presence) == 0x20, "SNS presence offset");
_Static_assert(sizeof(ChSnsNativeString) == 0x20, "SNS string layout");
_Static_assert(sizeof(ChSnsNativeResult) == 0x38, "SNS result layout");
_Static_assert(offsetof(ChSnsNativeResult, status_1) == 0x04, "SNS second status offset");
_Static_assert(offsetof(ChSnsNativeResult, text) == 0x08, "SNS error text offset");
_Static_assert(offsetof(ChSnsNativeResult, response) == 0x28, "SNS response reference offset");

/* 调用方应先校验 constructor 的固定入口字节及 expected_vtable 的只读映像归属。
 * 构造前、构造失败后均清零，不能把借用指针带入 protobuf 元数据。
 */
static int ch_sns_build_unlike(ChSnsObjectOp *request, ChSnsObjectOpCtor constructor,
                               uintptr_t expected_vtable, uint64_t post_id) {
    if (!request) return 0;
    memset(request, 0, sizeof(*request));
    if (!constructor || !expected_vtable || !post_id) return 0;
    if (constructor(request) != request || request->vtable != expected_vtable ||
        request->opaque_metadata || request->unknown_1c || request->presence) {
        memset(request, 0, sizeof(*request));
        return 0;
    }
    request->post_id = post_id;
    request->operation = CH_SNS_OBJECT_OP_UNLIKE;
    request->presence |= 3;
    return 1;
}

/* 零初始化结果故意不是可判定成功的 string 状态。
 * 这里只校验元数据形状，绝不解引用来自微信的字符串指针。
 */
static int ch_sns_result_string_shape(const ChSnsNativeString *text) {
    if (!text || text->size > 65536 || text->size > text->capacity) return 0;
    if (text->capacity < 16) return text->capacity == 15 && text->size <= 15;
    return text->data.pointer != 0;
}

enum ChSnsDecision {
    CH_SNS_UNKNOWN = 0,
    CH_SNS_FAILED = 1,
    CH_SNS_CONFIRMED = 2
};

/* 两个状态字段为零并不单独证明成功。returned、completion_observed 和
 * response_type_verified 必须由实际执行／回调路径产生，不能由调用方猜测。
 * 所有判定都不授予重发权限；特别是 UNKNOWN 必须保留原 request_id。
 */
static enum ChSnsDecision ch_sns_completed_status(const ChSnsNativeResult *result,
    int returned, int completion_observed, int response_type_verified, int account_matches) {
    if (!result || !returned || !completion_observed || !response_type_verified ||
        !account_matches || !ch_sns_result_string_shape(&result->text)) return CH_SNS_UNKNOWN;
    return result->status_0 || result->status_1 ? CH_SNS_FAILED : CH_SNS_CONFIRMED;
}

#endif
