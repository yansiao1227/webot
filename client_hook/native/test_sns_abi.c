/* 自有进程的参数传递、布局及结果误判边界；不加载微信。 */
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "sns_abi.h"

static unsigned constructor_calls;
static uintptr_t expected_vtable = UINT64_C(0x1889c6cf8);
static ChSnsObjectOp *fixture_constructor(ChSnsObjectOp *request) {
    const unsigned char *bytes = (const unsigned char *)request;
    for (size_t i=0; i<sizeof(*request); ++i) assert(bytes[i] == 0);
    request->vtable = expected_vtable;
    ++constructor_calls;
    return request;
}

static ChSnsObjectOp *wrong_constructor(ChSnsObjectOp *request) {
    request->vtable = expected_vtable + 8;
    return request;
}

static void build_and_pass(void) {
    struct Guarded {
        uint64_t before;
        ChSnsObjectOp value;
        uint64_t after;
    } guarded;
    memset(&guarded, 0xa5, sizeof(guarded));
    guarded.before = UINT64_C(0x1234567890abcdef);
    guarded.after = UINT64_C(0xfedcba0987654321);
    assert(ch_sns_build_unlike(&guarded.value, fixture_constructor, expected_vtable, UINT64_MAX));
    assert(guarded.before == UINT64_C(0x1234567890abcdef));
    assert(guarded.after == UINT64_C(0xfedcba0987654321));
    assert(guarded.value.post_id == UINT64_MAX);
    assert(guarded.value.operation == 5 && guarded.value.presence == 3);
    assert(guarded.value.opaque_metadata == 0 && guarded.value.unknown_1c == 0);
    assert(constructor_calls == 1);
    assert(!ch_sns_build_unlike(&guarded.value, fixture_constructor, expected_vtable, 0));
    assert(constructor_calls == 1);
    assert(!ch_sns_build_unlike(&guarded.value, wrong_constructor, expected_vtable, 1));
    const unsigned char *bytes = (const unsigned char *)&guarded.value;
    for (size_t i=0; i<sizeof(guarded.value); ++i) assert(bytes[i] == 0);
}

static void completion_boundaries(void) {
    ChSnsNativeResult result = {0};
    assert(ch_sns_completed_status(&result,1,1,1,1) == CH_SNS_UNKNOWN);
    result.text.capacity = 15;
    assert(ch_sns_completed_status(&result,0,1,1,1) == CH_SNS_UNKNOWN);
    assert(ch_sns_completed_status(&result,1,0,1,1) == CH_SNS_UNKNOWN);
    assert(ch_sns_completed_status(&result,1,1,0,1) == CH_SNS_UNKNOWN);
    assert(ch_sns_completed_status(&result,1,1,1,0) == CH_SNS_UNKNOWN);
    assert(ch_sns_completed_status(&result,1,1,1,1) == CH_SNS_CONFIRMED);
    result.status_0 = -110;
    assert(ch_sns_completed_status(&result,1,1,1,1) == CH_SNS_FAILED);
    result.status_0 = 0; result.status_1 = -1;
    assert(ch_sns_completed_status(&result,1,1,1,1) == CH_SNS_FAILED);
    result.status_1 = 0;
    result.text.size = 16;
    assert(ch_sns_completed_status(&result,1,1,1,1) == CH_SNS_UNKNOWN);
    result.text.capacity = 31;
    assert(!ch_sns_result_string_shape(&result.text));
    result.text.data.pointer = UINT64_C(0x12340000);
    assert(ch_sns_result_string_shape(&result.text));
    result.text.size = 65537; result.text.capacity = 65551;
    assert(!ch_sns_result_string_shape(&result.text));
}

int main(void) {
    build_and_pass();
    completion_boundaries();
    return 0;
}
