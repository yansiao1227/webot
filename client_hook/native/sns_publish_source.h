#ifndef CH_SNS_PUBLISH_SOURCE_H
#define CH_SNS_PUBLISH_SOURCE_H

/* 4.1.9.30 SnsPostRequest 的借用输入，仅供已校验的原生 CopyFrom 包装使用。
 * 4d2e6b0 构造请求：ObjectDesc(+10)、ClientId(+38)、public(+1c=0)、
 * 显式零默认字段及 presence(+194)。输入不拥有微信堆内存，不能交给原生析构。
 * source 以及 xml/client_id 的外部字节在同步包装返回前必须保持有效且不能移动。
 */
#include "sns_abi.h"

enum { CH_SNS_POST_REQUEST_SIZE = 0x1a0, CH_SNS_POST_XML_LIMIT = 2 * 1024 * 1024 };

typedef struct ChSnsPostRequest {
    uint64_t words[CH_SNS_POST_REQUEST_SIZE / 8];
} ChSnsPostRequest;

typedef struct ChSnsMmBuffer {
    uintptr_t vtable;
    uintptr_t string;
    uint32_t length;
    uint32_t padding_14;
    uint32_t presence;
    uint32_t padding_1c;
} ChSnsMmBuffer;

typedef void *(*ChSnsPostCtor)(void *);

typedef struct ChSnsPublishSource {
    ChSnsPostRequest request;
    ChSnsMmBuffer description;
    ChSnsNativeString xml;
    ChSnsNativeString client_id;
} ChSnsPublishSource;

_Static_assert(sizeof(ChSnsPostRequest) == 0x1a0, "SnsPostRequest fixed size");
_Static_assert(sizeof(ChSnsMmBuffer) == 0x20, "MmBuffer fixed size");
_Static_assert(offsetof(ChSnsMmBuffer, string) == 8, "MmBuffer string pointer");
_Static_assert(offsetof(ChSnsMmBuffer, length) == 0x10, "MmBuffer byte count");
_Static_assert(offsetof(ChSnsMmBuffer, presence) == 0x18, "MmBuffer presence bits");

static int ch_sns_borrow_string(ChSnsNativeString *out, const char *bytes, size_t length) {
    if (!out || !bytes || memchr(bytes, 0, length)) return 0;
    memset(out, 0, sizeof(*out));
    out->size = length;
    if (length <= 15) {
        memcpy(out->data.small, bytes, length);
        out->capacity = 15;
    } else {
        out->data.pointer = (uintptr_t)bytes;
        out->capacity = length;
    }
    return 1;
}

static int ch_sns_publish_source(ChSnsPublishSource *source,
    ChSnsPostCtor request_ctor, ChSnsPostCtor buffer_ctor,
    uintptr_t expected_request_vtable, uintptr_t expected_buffer_vtable,
    const char *xml, size_t xml_length, const char *client_id, size_t client_id_length) {
    uintptr_t pointer;
    uint32_t zero = 0;
    /* 4d2e6b0: (object_desc ? 2 : 0) | client_id 0x40 | defaults 0x1000009124
     * | public presence 0x10；public 值为零，无白/黑名单。
     */
    uint64_t presence = UINT64_C(0x1000009176);
    if (!source) return 0;
    memset(source, 0, sizeof(*source));
    if (!request_ctor || !buffer_ctor || !expected_request_vtable || !expected_buffer_vtable ||
        !xml_length || xml_length > CH_SNS_POST_XML_LIMIT || !client_id_length || client_id_length > 127 ||
        !ch_sns_borrow_string(&source->xml, xml, xml_length) ||
        !ch_sns_borrow_string(&source->client_id, client_id, client_id_length)) return 0;
    if (request_ctor(&source->request) != &source->request ||
        source->request.words[0] != expected_request_vtable ||
        buffer_ctor(&source->description) != &source->description ||
        source->description.vtable != expected_buffer_vtable) {
        memset(source, 0, sizeof(*source));
        return 0;
    }
    source->description.string = (uintptr_t)&source->xml;
    source->description.length = (uint32_t)xml_length;
    source->description.presence = 3;
    pointer = (uintptr_t)&source->description;
    memcpy((unsigned char *)&source->request + 0x10, &pointer, sizeof(pointer));
    pointer = (uintptr_t)&source->client_id;
    memcpy((unsigned char *)&source->request + 0x38, &pointer, sizeof(pointer));
    memcpy((unsigned char *)&source->request + 0x1c, &zero, sizeof(zero));
    memcpy((unsigned char *)&source->request + 0x40, &zero, sizeof(zero));
    memcpy((unsigned char *)&source->request + 0x60, &zero, sizeof(zero));
    memcpy((unsigned char *)&source->request + 0x88, &zero, sizeof(zero));
    memcpy((unsigned char *)&source->request + 0x18c, &zero, sizeof(zero));
    memcpy((unsigned char *)&source->request + 0x194, &presence, sizeof(presence));
    return 1;
}

#endif
