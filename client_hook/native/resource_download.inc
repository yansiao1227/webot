#include "resource_download.h"
#include "resource-profile.h"

typedef void (*ResourceCtor)(void *);
typedef void *(*ResourceSubmit)(void *, void *, void *, void *, void *, unsigned char);
typedef struct ResourceOperation {
    ChResourceRpc rpc;
    Operation queue;
    Operation release_queue;
    FixtureSharedRef future;
    FixtureSharedRef contexts[3];
    unsigned char fingerprint[32];
    struct ResourceOperation *next;
} ResourceOperation;
static SRWLOCK resource_gate = SRWLOCK_INIT;
static ResourceOperation *resource_operations;
static unsigned resource_operation_count;

static BOOL resource_fingerprint(const ChResourceRpc *rpc,unsigned char digest[32]) {
    BCRYPT_ALG_HANDLE algorithm=NULL;
    BCRYPT_HASH_HANDLE hash=NULL;
    BOOL ok=FALSE;
    if (BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0)<0 ||
        BCryptCreateHash(algorithm,&hash,NULL,0,NULL,0,0)<0) goto done;
    const char *parts[]={rpc->account,rpc->request_id,rpc->file_id,rpc->aes_key,rpc->output};
    for (unsigned i=0;i<5;++i)
        if (BCryptHashData(hash,(PUCHAR)parts[i],(ULONG)strlen(parts[i])+1,0)<0) goto done;
    if (BCryptHashData(hash,(PUCHAR)&rpc->expected_size,8,0)<0 ||
        BCryptHashData(hash,(PUCHAR)&rpc->file_type,4,0)<0) goto done;
    ok=BCryptFinishHash(hash,digest,32,0)>=0;
done:
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    return ok;
}
typedef struct ResourceProgress { const void *vtable; ResourceOperation *job; } ResourceProgress;
typedef struct {
    void *(*copy)(const ResourceProgress *,void *);
    void *(*move)(ResourceProgress *,void *);
    void (*invoke)(ResourceProgress *,const uint64_t *,const uint64_t *);
    const void *(*type)(const ResourceProgress *);
    void (*destroy)(ResourceProgress *,unsigned char);
} ResourceProgressVtable;
static void *progress_copy(const ResourceProgress *self,void *storage) {
    ResourceProgress *out=storage?storage:HeapAlloc(GetProcessHeap(),0,sizeof(*out));
    if (out) *out=*self;
    return out;
}
static void *progress_move(ResourceProgress *self,void *storage) {return progress_copy(self,storage);}
static void progress_invoke(ResourceProgress *self,const uint64_t *current,const uint64_t *total) {
    uint64_t values[2];
    if (!read_self(current,&values[0],8) || !read_self(total,&values[1],8)) return;
    InterlockedExchange64((LONG64 *)&self->job->rpc.transferred,(LONG64)values[0]);
    InterlockedExchange64((LONG64 *)&self->job->rpc.total,(LONG64)values[1]);
}
static const void *progress_type(const ResourceProgress *self) {(void)self;return NULL;}
static void progress_destroy(ResourceProgress *self,unsigned char heap) {if (heap) HeapFree(GetProcessHeap(),0,self);}
static const ResourceProgressVtable resource_progress_vtable={
    progress_copy,progress_move,progress_invoke,progress_type,progress_destroy};

static BOOL resource_profile_matches(void) {
    for (size_t i=0; i<sizeof(resource_functions)/sizeof(resource_functions[0]); ++i)
        if (!code_matches(active_base, real_profile.image_size, &resource_functions[i])) return FALSE;
    return TRUE;
}

static BOOL resource_string(void *request, size_t offset, const char *value) {
    FixtureString32 *s=(FixtureString32 *)((BYTE *)request+offset);
    FixtureStringAssignFn grow;
    uintptr_t at=active_base+real_profile.functions[P_GROW].rva;
    memcpy(&grow,&at,sizeof(grow));
    if (s->size || s->capacity!=15) return FALSE;
    if (grow(s,strlen(value),0,value)!=s || s->size!=strlen(value) || s->capacity<s->size) return FALSE;
    const char *data=s->capacity>=16?s->data.heap:s->data.small;
    return !memcmp(data,value,(size_t)s->size+1);
}

static void work_resource(Operation *operation) {
    ResourceOperation *job=(ResourceOperation *)operation->resource;
    ChResourceRpc *rpc=&job->rpc;
    if (operation==&job->release_queue) {
        BOOL released=TRUE;
        if (job->future.object || job->future.control) released=release_native(&job->future,0) && released;
        for (int i=2;i>=0;--i)
            if (job->contexts[i].object || job->contexts[i].control)
                released=release_native(&job->contexts[i],0) && released;
        rpc->cleanup_ok=released; rpc->stage=6;
        SecureZeroMemory(rpc->aes_key,sizeof(rpc->aes_key));
        SecureZeroMemory(rpc->file_id,sizeof(rpc->file_id));
        operation->result.callback_error=released?0:ERROR_INVALID_DATA;
        return;
    }
    FixtureSharedRef refs[3]={{0}}, future={0};
    BYTE request[0x588];
    FixtureFn64 progress={{0},NULL};
    ResourceProgress progress_impl={&resource_progress_vtable,job};
    FixtureString32 tag={.size=0,.capacity=15};
    FixtureGetContext1Fn get1=(FixtureGetContext1Fn)(active_base+0x20e30);
    FixtureGetContext2Fn get2=(FixtureGetContext2Fn)(active_base+0x300340);
    FixtureGetContext3Fn get_cdn=(FixtureGetContext3Fn)(active_base+0x18f430);
    ResourceCtor ctor=(ResourceCtor)(active_base+0x18eab0);
    ResourceCtor dtor=(ResourceCtor)(active_base+0x191cd0);
    ResourceSubmit submit=(ResourceSubmit)(active_base+0x954c00);
    BOOL constructed=FALSE, consumed=FALSE, started=FALSE, clean=TRUE;
    rpc->error=ERROR_INVALID_DATA; rpc->callback_tid=GetCurrentThreadId(); rpc->stage=1;
    if (!resource_profile_matches()) goto done;
    /* 使用与 ImageTask/FileTask 相同的只读模板及原生构造顺序。 */
    memcpy(request,(const void *)(active_base+0x80938a8),sizeof(request));
    ctor(request); constructed=TRUE; rpc->stage=2;
    if (!resource_string(request,0x50,rpc->request_id) ||
        !resource_string(request,0x70,rpc->file_id) ||
        !resource_string(request,0x90,rpc->aes_key) ||
        !resource_string(request,0xb0,rpc->output)) goto done;
    memcpy(request+0xd0,&rpc->file_type,4);
    memcpy(request+0x198,&rpc->expected_size,8);
    /* 沿用显式 FileTask/VideoTask 请求的默认值。autostart
     * 描述自动策略，不表示可以绕过该策略。 */
    request[0x329]=0;
    uint32_t connections=2;
    memcpy(request+0x170,&connections,4);
    if (rpc->file_type==1 || rpc->file_type==2) {
        uint32_t kind=1;
        const uint32_t *range[2]={&kind,&kind+1};
        ((void *(*)(void *,const void *))(active_base+0x94d640))(request+0x330,range);
    }
    if (!rpc->mode) {rpc->error=0;goto done;}
    rpc->stage=3;
    if (get1(&refs[0])!=&refs[0] || !owned_control(&refs[0],0) ||
        get2(refs[0].object,&refs[1])!=&refs[1] || !owned_control(&refs[1],0) ||
        get_cdn(refs[1].object,&refs[2])!=&refs[2] || !owned_control(&refs[2],0)) goto done;
    memcpy(progress.storage,&progress_impl,sizeof(progress_impl));
    progress.impl=(FixtureFnImpl *)progress.storage;
    rpc->calls=1; rpc->stage=4;
    if (submit(refs[2].object,&future,&tag,request,&progress,0)!=&future) {
        clean=FALSE; consumed=TRUE; goto done;
    }
    consumed=TRUE; started=TRUE;
    rpc->error=0; rpc->stage=5;
done:
    if (constructed && !consumed) dtor(request);
    /* 保留 future，直到状态 RPC 读取其终态值。
     * 提交返回后，原生下载器可能仍在执行。 */
    if (future.control || future.object) job->future=future;
    for (int i=2;i>=0;--i) {
        if (started || consumed) job->contexts[i]=refs[i];
        else if (refs[i].control || refs[i].object) clean=release_native(&refs[i],0) && clean;
    }
    rpc->cleanup_ok=clean;
    if (!clean) rpc->error=ERROR_INVALID_DATA;
    operation->result.callback_error=rpc->error;
    InterlockedExchange((LONG *)&rpc->done,1);
}

__declspec(dllexport) DWORD WINAPI HookResource(void *argument) {
    ChResourceRpc input;
    ResourceOperation *job=NULL;
    char identity[129];
    HMODULE module=GetModuleHandleW(L"Weixin.dll"), pin=NULL;
    MODULEINFO info;
    DWORD error=ERROR_INVALID_PARAMETER;
    unsigned char fingerprint[32];
    if (!read_self(argument,&input,sizeof(input)) || input.magic!=CH_RESOURCE_MAGIC ||
        input.version!=5 || input.size!=sizeof(input) || input.pid!=GetCurrentProcessId() ||
        input.mode>3 || !memchr(input.account,0,sizeof(input.account)) ||
        !memchr(input.request_id,0,sizeof(input.request_id)) || !input.request_id[0] ||
        !memchr(input.file_id,0,sizeof(input.file_id)) || !input.file_id[0] ||
        !memchr(input.aes_key,0,sizeof(input.aes_key)) || strlen(input.aes_key)!=32 ||
        !memchr(input.output,0,sizeof(input.output)) || !input.output[0] ||
        !input.expected_size || input.expected_size>64u*1024u*1024u ||
        (input.file_type!=1 && input.file_type!=2 && input.file_type!=4 && input.file_type!=5)) return error;
    if (!TryAcquireSRWLockExclusive(&resource_gate)) return ERROR_BUSY;
    native_base=(uintptr_t)module;
    if (!module || !account_identity(identity) || strcmp(identity,input.account) ||
        !profile_matches(module,&info,&real_profile)) goto leave;
    if (!resource_fingerprint(&input,fingerprint)) {error=ERROR_INVALID_DATA;goto leave;}
    for (job=resource_operations;job;job=job->next)
        if (!strcmp(job->rpc.request_id,input.request_id)) break;
    if (job) {
        if (memcmp(job->fingerprint,fingerprint,32)) {error=ERROR_ALREADY_EXISTS;goto leave;}
    } else {
        if (input.mode>=2) {error=ERROR_NOT_FOUND;goto leave;}
        if (resource_operation_count>=4096) {error=ERROR_NOT_ENOUGH_QUOTA;goto leave;}
        for (ResourceOperation *it=resource_operations;it;it=it->next)
            if (!atomic_read(&it->queue.done)) {error=ERROR_BUSY;goto leave;}
        if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
            (LPCWSTR)(uintptr_t)HookResource,&pin)) goto leave;
        job=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(*job));
        if (!job) {error=ERROR_OUTOFMEMORY;goto leave;}
        job->rpc=input;job->rpc.done=0;job->rpc.calls=0;job->rpc.stage=0;
        memcpy(job->fingerprint,fingerprint,32);++resource_operation_count;
        job->queue.resource=job;job->queue.code=BRIDGE_OP_PREPARE;
        strcpy(job->queue.account,input.account);
        job->next=resource_operations;resource_operations=job;
        post_once((uintptr_t)module,&real_profile,&job->queue);
    }
    /* 返回 unknown/pending 绝不表示可以再次提交任务。 */
    error=atomic_read(&job->queue.done)?job->rpc.error:ERROR_IO_PENDING;
    if (atomic_read(&job->queue.failure)) error=(DWORD)atomic_read(&job->queue.failure);
    if (atomic_read(&job->queue.done) && job->future.object && !job->rpc.native_done) {
        DWORD state=0;
        uintptr_t value=0;
        if (!owned_control(&job->future,0) ||
            !read_self((BYTE *)job->future.object+0xc4,&state,4)) {error=ERROR_INVALID_DATA;goto leave;}
        job->rpc.future_state=state;
        if (state==1) {
            DWORD status[2]={0};
            FixtureSharedRef payload={0};
            if (!read_self((BYTE *)job->future.object+0xb8,&value,8) || !value ||
                !read_self((void *)(value+8),status,8)) {error=ERROR_INVALID_DATA;goto leave;}
            job->rpc.native_status_0=status[0]; job->rpc.native_status_1=status[1];
            /* Result<DownloadResult> 的第一个状态属于外层 Result。
             * ImageTask 从 result.payload（+0x28）读取实际 CDN 状态，
             * 不能将外层状态当作下载成功。 */
            if (!status[0] && !status[1]) {
                if (!read_self((void *)(value+0x30),&payload,16) || !payload.object || !payload.control ||
                    !read_self(payload.object,status,8)) {error=ERROR_INVALID_DATA;goto leave;}
                job->rpc.native_status_0=status[0]; job->rpc.native_status_1=status[1];

            }
            job->rpc.native_done=1;
        } else if (state==2) {job->rpc.native_done=1;job->rpc.native_status_0=ERROR_EXCEPTION_IN_SERVICE;}
    }
    if (input.mode==3 && atomic_read(&job->queue.done) && !job->release_queue.posted) {
        job->release_queue.resource=job; job->release_queue.code=BRIDGE_OP_PREPARE;
        strcpy(job->release_queue.account,input.account);
        post_once((uintptr_t)module,&real_profile,&job->release_queue);
    }
    input=job->rpc; input.error=error;
    write_self(argument,&input,sizeof(input));
leave:
    ReleaseSRWLockExclusive(&resource_gate);
    return error;
}
