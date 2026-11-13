/* 固定版本 SNS 请求只在客户端所属队列运行。无跳转钩子、不卸载已加载模块。 */
#define CH_SNS_SIDECAR 1
#include "runtime.c"
#include "sns-profile.h"
#include "sns_abi.h"
#include "sns_comment_source.h"
#include "sns_publish_source.h"
#include "sns_publish_xml.h"

typedef struct {
    DWORD magic,version,size,pid,mode,error,state,submitted,completed,cleaned,calls,tid;
    DWORD action;
    int32_t status0,status1;
    uint64_t post_id,server_post_id;
    char account[128],request_id[64];
    char author[128],body[65537];
} SnsRpc;
typedef struct SnsEntry { SnsRpc rpc; Operation operation; struct SnsEntry *next; } SnsEntry;
static SnsEntry *sns_entries;
static unsigned sns_count;
static SRWLOCK sns_gate=SRWLOCK_INIT;

static void work_sns(Operation *operation) {
    SnsEntry *entry=operation->sns;
    SnsRpc *rpc=&entry->rpc;
    rpc->tid=GetCurrentThreadId();
    if (rpc->mode==2) {rpc->completed=1;rpc->cleaned=1;return;}
    void *returned=NULL;
    if (rpc->action==1) {
        ChSnsObjectOp object_op;
        ChSnsObjectOpCtor ctor=(ChSnsObjectOpCtor)(active_base+0x4c478f0);
        if(!ch_sns_build_unlike(&object_op,ctor,active_base+0x89c6cf8,rpc->post_id)) {
            operation->result.callback_error=ERROR_INVALID_DATA;return;
        }
        ChSnsObjectOpExecute invoke=(ChSnsObjectOpExecute)(active_base+0x4cf6970);
        InterlockedExchange((volatile LONG *)&rpc->submitted,1);
        returned=invoke(rpc->post_id,(ChSnsNativeResult *)operation->submit_out.bytes,&object_op);
    } else if(rpc->action==2 || rpc->action==3) {
        ChSnsCommentSource source;
        FILETIME now;ULARGE_INTEGER ticks;GetSystemTimeAsFileTime(&now);
        ticks.LowPart=now.dwLowDateTime;ticks.HighPart=now.dwHighDateTime;
        uint32_t created=(uint32_t)(ticks.QuadPart/10000000-11644473600ULL);
        if(!ch_sns_comment_source_init(&source,active_base,rpc->post_id,
           rpc->account,strlen(rpc->account),rpc->author,strlen(rpc->author),
           rpc->request_id,strlen(rpc->request_id),rpc->action==2?1:2,0,created,
           rpc->body,strlen(rpc->body),rpc->action==3)) {
            operation->result.callback_error=ERROR_INVALID_PARAMETER;return;
        }
        FixtureString32 empty={0};empty.capacity=15;
        unsigned char options[56]={0};uint64_t defaults=UINT64_C(0x1d4c000000000);
        memcpy(options,&defaults,8);uint64_t capacity=15;memcpy(options+32,&capacity,8);
        typedef void *(*Execute)(void *,uintptr_t,void *,void *,void *);
        Execute invoke=(Execute)(active_base+0x2c17030);
        InterlockedExchange((volatile LONG *)&rpc->submitted,1);
        returned=invoke(operation->submit_out.bytes,0,source.request,&empty,options);
    } else if(rpc->action==4) {
        size_t capacity=strlen(rpc->body)*6+2048;
        char *xml=HeapAlloc(GetProcessHeap(),0,capacity);
        if(!xml){operation->result.callback_error=ERROR_OUTOFMEMORY;return;}
        FILETIME now;ULARGE_INTEGER ticks;GetSystemTimeAsFileTime(&now);
        ticks.LowPart=now.dwLowDateTime;ticks.HighPart=now.dwHighDateTime;
        uint32_t created=(uint32_t)(ticks.QuadPart/10000000-11644473600ULL);
        size_t length=ch_sns_publish_xml(xml,capacity,rpc->account,
                                      rpc->body,strlen(rpc->body),created);
        ChSnsPublishSource source;
        if(!length||!ch_sns_publish_source(&source,(ChSnsPostCtor)(active_base+0x4c47260),
           (ChSnsPostCtor)(active_base+0x5c72a0),active_base+0x89c5af8,active_base+0x8128948,
           xml,length,rpc->request_id,strlen(rpc->request_id))) {
            HeapFree(GetProcessHeap(),0,xml);operation->result.callback_error=ERROR_INVALID_PARAMETER;return;
        }
        FixtureString32 empty={0};empty.capacity=15;
        unsigned char options[56]={0};uint64_t defaults=UINT64_C(0x1d4c000000000),string_cap=15;
        memcpy(options,&defaults,8);memcpy(options+32,&string_cap,8);
        typedef void *(*Execute)(void *,uintptr_t,void *,void *,void *);
        InterlockedExchange((volatile LONG *)&rpc->submitted,1);
        returned=((Execute)(active_base+0x4d30a50))(operation->submit_out.bytes,0,&source.request,&empty,options);
        HeapFree(GetProcessHeap(),0,xml);
    } else {operation->result.callback_error=ERROR_INVALID_PARAMETER;return;}
    if (returned!=operation->submit_out.bytes) {operation->result.callback_error=ERROR_INVALID_DATA;return;}
    memcpy(&rpc->status0,operation->submit_out.bytes,4);
    memcpy(&rpc->status1,operation->submit_out.bytes+4,4);
    BOOL post_valid=TRUE;
    if(rpc->action==4&&!rpc->status0&&!rpc->status1) {
        uintptr_t response=0,object=0,vt=0;
        post_valid=read_self(operation->submit_out.bytes+0x28,&response,8)&&response&&
            read_self((void *)response,&vt,8)&&vt==active_base+0x89c5b78&&
            read_self((void *)(response+0x10),&object,8)&&object&&
            read_self((void *)object,&vt,8)&&vt==active_base+0x89c5428&&
            read_self((void *)(object+8),&rpc->server_post_id,8)&&rpc->server_post_id;
    }
    rpc->cleaned=clean_out(operation);
    if (!rpc->cleaned) {operation->result.callback_error=ERROR_INVALID_DATA;return;}
    if (!post_valid) {operation->result.callback_error=ERROR_INVALID_DATA;return;}
    InterlockedExchange((volatile LONG *)&rpc->completed,1);
}

__declspec(dllexport) DWORD WINAPI HookSns(void *argument) {
    SnsRpc input;MODULEINFO info;HMODULE pin=NULL;
    HMODULE core=GetModuleHandleW(L"Weixin.dll");
    char identity[129];DWORD error=ERROR_INVALID_PARAMETER;
    if (!read_self(argument,&input,sizeof(input))||input.magic!=0x534e5331||input.version!=3||
        input.size!=sizeof(input)||input.pid!=GetCurrentProcessId()||input.mode>2||
        !memchr(input.account,0,128)||!memchr(input.request_id,0,64)||!input.request_id[0]||
        !memchr(input.author,0,128)||!memchr(input.body,0,65537)||
        (input.mode==0&&(input.action<1||input.action>4||(input.action<4&&!input.post_id)))) return error;
    if (!TryAcquireSRWLockExclusive(&sns_gate)) return ERROR_BUSY;
    native_base=(uintptr_t)core;
    if (!core||!profile_matches(core,&info,&real_profile)||
        !account_identity(identity)||strcmp(identity,input.account)) {error=ERROR_INVALID_DATA;goto done;}
    for (unsigned i=0;i<sizeof(sns_functions)/sizeof(sns_functions[0]);++i)
        if (!code_matches((uintptr_t)core,real_profile.image_size,&sns_functions[i])) {error=ERROR_INVALID_DATA;goto done;}
    SnsEntry *entry;
    for(entry=sns_entries;entry;entry=entry->next)
        if(!strcmp(entry->rpc.request_id,input.request_id)) break;
    if(entry) {
        if(strcmp(entry->rpc.account,input.account)||entry->rpc.post_id!=input.post_id||
           (input.mode!=1&&(entry->rpc.mode!=input.mode||entry->rpc.action!=input.action||
           strcmp(entry->rpc.author,input.author)||strcmp(entry->rpc.body,input.body)))) {error=ERROR_ALREADY_EXISTS;goto done;}
    } else {
        if(input.mode==1){error=ERROR_NOT_FOUND;goto done;}
        if(input.mode==0&&!expected_tid){error=ERROR_NOT_READY;goto done;}
        if(sns_count>=4096){error=ERROR_NOT_ENOUGH_QUOTA;goto done;}
        if(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
           (LPCWSTR)(uintptr_t)HookSns,&pin)){error=ERROR_INVALID_DATA;goto done;}
        entry=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(*entry));
        if(!entry){error=ERROR_OUTOFMEMORY;goto done;}
        entry->rpc=input;entry->rpc.calls=1;
        entry->rpc.submitted=entry->rpc.completed=entry->rpc.cleaned=0;
        entry->rpc.server_post_id=0;
        entry->rpc.status0=entry->rpc.status1=0x7fffffff;
        entry->operation.sns=entry;
        entry->operation.code=input.mode==2?BRIDGE_OP_PREPARE:BRIDGE_OP_SEND;
        strcpy(entry->operation.account,input.account);
        entry->next=sns_entries;sns_entries=entry;++sns_count;
        post_once((uintptr_t)core,&real_profile,&entry->operation);
    }
    BOOL done=atomic_read(&entry->operation.done)!=0;
    input=entry->rpc;
    input.state=done?2:1;
    if(!done)input.completed=0;
    input.error=entry->operation.result.callback_error;
    if(atomic_read(&entry->operation.gate)==3) {input.error=ERROR_INVALID_DATA;input.state=4;}
    else if(input.error)input.state=input.submitted?4:3;
    memcpy(argument,&input,sizeof(input));error=0;
done:
    ReleaseSRWLockExclusive(&sns_gate);return error;
}
