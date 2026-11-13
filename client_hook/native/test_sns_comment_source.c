#include "sns_comment_source.h"
#include <assert.h>

static uint64_t u64(const void *object,size_t offset) {
    uint64_t value;memcpy(&value,(const unsigned char *)object+offset,8);return value;
}
static uint32_t u32(const void *object,size_t offset) {
    uint32_t value;memcpy(&value,(const unsigned char *)object+offset,4);return value;
}

int main(void) {
    const uintptr_t base=0x180000000ull;
    ChSnsCommentSource source;
    const char sender[]="wxid_source_account", author[]="wxid_target_author";
    const char request[]="sns_stable_request_001";
    const char content[]="\xe4\xb8\xad\xe6\x96\x87 comment \xf0\x9f\x98\x80";
    assert(ch_sns_comment_source_init(&source,base,UINT64_MAX,sender,sizeof(sender)-1,
        author,sizeof(author)-1,request,sizeof(request)-1,1,0,123,NULL,0,0));
    assert(u64(source.request,0)==base+0x89c69a8);
    assert(u64(source.request,8)==0); /* BaseRequest 由原生请求层处理 */
    assert(u64(source.request,0x10)==(uintptr_t)source.group);
    assert(u64(source.request,0x18)==(uintptr_t)&source.client_id);
    assert(u32(source.request,0x24)==6);
    assert(u64(source.group,8)==UINT64_MAX);
    assert(u64(source.group,0x10)==0); /* 未设置第二个标量字段 */
    assert(u64(source.group,0x20)==0); /* 无 ReferAction */
    assert(u64(source.group,0x18)==(uintptr_t)source.action);
    assert(u32(source.group,0x38)==5);
    assert(u64(source.action,8)==(uintptr_t)&source.sender);
    assert(u64(source.action,0x10)==(uintptr_t)&source.author);
    assert(u64(source.action,0x18)==base+0xa6674f8);
    assert(u64(source.action,0x20)==base+0xa6674f8);
    assert(u64(source.action,0x30)==base+0xa6674f8);
    assert(u32(source.action,0x28)==1 && u32(source.action,0x2c)==0);
    assert(u32(source.action,0x38)==123 && u32(source.action,0xb0)==0x73);
    assert(source.client_id.data.borrowed==request);
    assert(source.client_id.size==sizeof(request)-1);
    assert(!memcmp(source.sender.data.borrowed,sender,sizeof(sender)-1));

    /* 不重分配输入；必须在原生深拷贝完成前保留这些 buffer。 */
    assert(ch_sns_comment_source_init(&source,base,42,sender,sizeof(sender)-1,
        author,sizeof(author)-1,request,sizeof(request)-1,2,0,124,content,sizeof(content)-1,1));
    assert(u32(source.action,0xb0)==0xf3);
    assert(u64(source.action,0x30)==(uintptr_t)&source.content);
    assert(source.content.data.borrowed==content);
    assert(source.content.size==sizeof(content)-1);
    assert(ch_sns_comment_source_init(&source,base,42,"wxid_a",6,"wxid_b",6,"stable",6,
        5,0,125,NULL,0,0));
    assert(source.client_id.capacity==15 && source.client_id.size==6);
    assert(!memcmp(source.client_id.data.small,"stable\0",7));
    assert(u32(source.action,0x28)==5); /* 明确保留调用方选定值，不映射 UI 枚举 */
    assert(!ch_sns_comment_source_init(&source,base,0,"wxid_a",6,"wxid_b",6,"stable",6,
        1,0,125,NULL,0,0));
    assert(!ch_sns_comment_source_init(&source,base,42,"wxid_a",6,"wxid_b",6,"bad\0id",6,
        1,0,125,NULL,0,0));
    assert(!ch_sns_comment_source_init(&source,base,42,"wxid_a",6,"wxid_b",6,"stable",6,
        2,0,125,"",0,1));
    assert(!ch_sns_comment_source_init(&source,base,42,"wxid_a",6,"wxid_b",6,"stable",6,
        1,0,125,"x",1,0));
    return 0;
}
