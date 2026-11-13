#ifndef CLIENT_HOOK_SNS_COMMENT_SOURCE_H
#define CLIENT_HOOK_SNS_COMMENT_SOURCE_H

/* 仅供固定 4.1.9.30 的 SnsCommentRequest 拷贝构造使用。
 * 所有对象和 string32 header 由调用方持有；长字符串正文借用输入缓冲区。
 * 源对象绝不能交给原生析构，也不能在原生深拷贝完成前失效。
 * 此布局自身不提供队列、账号授权、网络提交或完成确认。
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

typedef struct ChSnsBorrowedString32 {
    union { unsigned char small[16]; const char *borrowed; } data;
    uint64_t size, capacity;
} ChSnsBorrowedString32;

typedef struct ChSnsCommentSource {
    uint64_t request[5];
    uint64_t group[8];
    uint64_t action[23];
    ChSnsBorrowedString32 sender, author, client_id, content;
} ChSnsCommentSource;

_Static_assert(sizeof(uintptr_t)==8, "SNS source requires x64");
_Static_assert(sizeof(ChSnsBorrowedString32)==0x20, "string32 ABI");
_Static_assert(offsetof(ChSnsBorrowedString32,size)==0x10, "string32 length ABI");
_Static_assert(offsetof(ChSnsBorrowedString32,capacity)==0x18, "string32 capacity ABI");
_Static_assert(sizeof(((ChSnsCommentSource *)0)->request)==0x28, "SnsCommentRequest ABI");
_Static_assert(sizeof(((ChSnsCommentSource *)0)->group)==0x40, "SnsActionGroup ABI");
_Static_assert(sizeof(((ChSnsCommentSource *)0)->action)==0xb8, "SnsAction ABI");

static inline void ch_sns_source_u32(void *object,size_t offset,uint32_t value) {
    memcpy((unsigned char *)object+offset,&value,sizeof(value));
}
static inline void ch_sns_source_u64(void *object,size_t offset,uint64_t value) {
    memcpy((unsigned char *)object+offset,&value,sizeof(value));
}
static inline void ch_sns_source_pointer(void *object,size_t offset,const void *value) {
    uintptr_t pointer=(uintptr_t)value;
    memcpy((unsigned char *)object+offset,&pointer,sizeof(pointer));
}

static inline int ch_sns_source_string(ChSnsBorrowedString32 *out,const char *value,size_t length) {
    if (!out || !value || length>65536 || memchr(value,0,length)) return 0;
    memset(out,0,sizeof(*out));out->size=length;
    if (length<16) {memcpy(out->data.small,value,length);out->capacity=15;}
    else {out->data.borrowed=value;out->capacity=length;}
    return 1;
}

/* action_type 必须由经过验证的业务分支给出；此函数不将 UI 枚举转换成协议枚举。
 * 普通评论值 2 的证据链：2c24640 第五参常量 2 -> 4d29560 ->
 * 4d1fd20 保存 model+0x88 -> 4d20780 写入 Action+0x28。
 * 普通好友点赞为 1，联系对象 flag bit0 未置位时为 5（2c14560）。
 * has_content=0 构造点赞源；has_content=1 构造带 Content 字段的评论源。
 * 回复评论的 ReferAction 尚未在此构造，不能用此源冒充精确回复。
 */
static inline int ch_sns_comment_source_init(
    ChSnsCommentSource *out,uintptr_t base,uint64_t post_id,
    const char *sender,size_t sender_length,const char *author,size_t author_length,
    const char *client_id,size_t client_id_length,
    uint32_t action_type,uint32_t source_value,uint32_t created_at,
    const char *content,size_t content_length,int has_content) {
    if (!out) return 0;
    memset(out,0,sizeof(*out));
    if (!base || !post_id || !created_at || !action_type ||
        !sender_length || sender_length>127 || !author_length || author_length>127 ||
        !client_id_length || client_id_length>127 ||
        (has_content!=0 && has_content!=1) ||
        (has_content && (!content_length || content_length>65536)) ||
        (!has_content && content_length)) return 0;
    if (!ch_sns_source_string(&out->sender,sender,sender_length) ||
        !ch_sns_source_string(&out->author,author,author_length) ||
        !ch_sns_source_string(&out->client_id,client_id,client_id_length) ||
        (has_content && !ch_sns_source_string(&out->content,content,content_length))) return 0;

    /* 4c47810 / 4c477d0 / 4c47740 的构造默认值。
     * 未设置的 protobuf 字符串也必须指向同构建的空单例，不能填 NULL。
     */
    uintptr_t empty=base+0xa6674f8;
    out->request[0]=base+0x89c69a8;
    out->request[3]=empty;
    out->group[0]=base+0x89c6928;
    out->group[5]=empty;
    out->action[0]=base+0x89c68a8;
    out->action[1]=out->action[2]=out->action[3]=out->action[4]=out->action[6]=empty;

    ch_sns_source_pointer(out->request,0x10,out->group);
    ch_sns_source_pointer(out->request,0x18,&out->client_id);
    ch_sns_source_u32(out->request,0x24,6); /* ActionGroup、ClientId */
    ch_sns_source_u64(out->group,0x08,post_id);
    ch_sns_source_pointer(out->group,0x18,out->action);
    ch_sns_source_u32(out->group,0x38,5); /* Id、CurrentAction */
    ch_sns_source_pointer(out->action,0x08,&out->sender);
    ch_sns_source_pointer(out->action,0x10,&out->author);
    ch_sns_source_u32(out->action,0x28,action_type);
    ch_sns_source_u32(out->action,0x2c,source_value);
    ch_sns_source_u32(out->action,0x38,created_at);
    ch_sns_source_u32(out->action,0xb0,0x73u|(has_content?0x80u:0));
    if (has_content) ch_sns_source_pointer(out->action,0x30,&out->content);
    return 1;
}

#endif
