/* 自有进程中的 SnsPostRequest 输入布局回归，不加载微信、不执行网络。 */
#include <assert.h>
#include <stdio.h>
#include "sns_publish_source.h"

static unsigned request_calls, buffer_calls;
static void *request_ctor(void *object) {
    ChSnsPostRequest *request = object;
    ++request_calls;
    memset(request, 0, sizeof(*request));
    request->words[0] = 0x89c5af8;
    request->words[0x38 / 8] = 0xa6674f8;
    request->words[0xc8 / 8] = 0xa6674f8;
    request->words[0x148 / 8] = 0xa6674f8;
    return object;
}
static void *buffer_ctor(void *object) {
    ChSnsMmBuffer *buffer = object;
    ++buffer_calls;
    memset(buffer, 0, sizeof(*buffer));
    buffer->vtable = 0x8128948;
    buffer->string = 0xa6674f8;
    return object;
}
static uint64_t u64(const void *p) { uint64_t n; memcpy(&n, p, 8); return n; }
static uint32_t u32(const void *p) { uint32_t n; memcpy(&n, p, 4); return n; }

int main(void) {
    ChSnsPublishSource source;
    const char *xml = "<TimelineObject><id>0</id></TimelineObject>";
    /* 同一 ABI 头中的对象操作/完成判定由 test_sns_abi.c 覆盖。 */
    (void)ch_sns_build_unlike;
    (void)ch_sns_completed_status;
    assert(ch_sns_publish_source(&source, request_ctor, buffer_ctor, 0x89c5af8, 0x8128948,
                                xml, strlen(xml), "request_1", 9));
    assert(request_calls == 1 && buffer_calls == 1);
    assert(source.request.words[0] == 0x89c5af8);
    assert(source.description.vtable == 0x8128948);
    assert(source.description.length == strlen(xml) && source.description.presence == 3);
    assert(source.description.string == (uintptr_t)&source.xml);
    assert(source.xml.data.pointer == (uintptr_t)xml && source.xml.size == strlen(xml));
    assert(source.client_id.capacity == 15 && source.client_id.size == 9);
    assert(!memcmp(source.client_id.data.small, "request_1", 9));
    assert(u64((char *)&source.request + 0x10) == (uintptr_t)&source.description);
    assert(u64((char *)&source.request + 0x38) == (uintptr_t)&source.client_id);
    assert(u64((char *)&source.request + 0x194) == UINT64_C(0x1000009176));
    assert(!u32((char *)&source.request + 0x1c));
    assert(source.request.words[0xc8 / 8] == 0xa6674f8);
    assert(source.request.words[0x148 / 8] == 0xa6674f8);
    {
        ChSnsNativeString small, heap;
        const char fifteen[] = "123456789012345";
        const char sixteen[] = "1234567890123456";
        assert(ch_sns_borrow_string(&small, fifteen, 15));
        assert(small.capacity == 15 && small.size == 15 && small.data.small[15] == 0);
        assert(ch_sns_borrow_string(&heap, sixteen, 16));
        assert(heap.capacity == 16 && heap.size == 16 && heap.data.pointer == (uintptr_t)sixteen);
    }
    /* 原始输入保持借用，不能把堆指针误当 inline 字符串或接受内嵌 NUL。 */
    assert(!ch_sns_publish_source(&source, request_ctor, buffer_ctor, 1, 0x8128948,
                                  xml, strlen(xml), "x", 1));
    assert(!ch_sns_publish_source(&source, request_ctor, buffer_ctor, 0x89c5af8, 0x8128948,
                                  "x\0y", 3, "x", 1));
    assert(!ch_sns_publish_source(&source, request_ctor, buffer_ctor, 0x89c5af8, 0x8128948,
                                  xml, strlen(xml), "", 0));
    assert(!ch_sns_publish_source(&source, request_ctor, buffer_ctor, 0x89c5af8, 0x8128948,
                                  xml, CH_SNS_POST_XML_LIMIT + 1u, "x", 1));
    puts("SNS publish source ABI: ok");
    return 0;
}
