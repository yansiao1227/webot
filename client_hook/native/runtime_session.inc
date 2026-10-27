/* 持久引擎、每个会话不可变的订阅，以及自有快照。 */
#ifdef CH_FIXTURE
#define CH_RAW_LOG_LIMIT (256*1024)
#else
#define CH_RAW_LOG_LIMIT (8*1024*1024)
#endif
typedef struct ChSession {
    SRWLOCK lock;
    volatile LONG refs, accepting;
    volatile LONG entry_hits, observed, filtered, rejected, dropped, cancelled;
    volatile LONG identity_changed, force_missed, route_missed;
    volatile LONGLONG last_return_rva;
    PVOID volatile reject_reason;
    uint64_t generation;
    char endpoint[128], self_id[129];
    StreamSample queue[32];
    unsigned head, count;
} ChSession;

__declspec(dllexport) ChPublicStatus HookStatus = {.magic=CH_MAGIC,.version=3,.size=sizeof(ChPublicStatus),.state=CH_IDLE};
static SRWLOCK session_gate = SRWLOCK_INIT;
static ChSession *current_session, *retired_session;
static volatile LONG engine_busy;
static BOOL engine_created, engine_fault;
static Wx41930FinalizeFn original;
static uintptr_t engine_target, expected_return, expected_vtable;
static unsigned char engine_before[16];
static __thread int reentrant;

/* 这些直接调用位于 SHA 固定的映像中，已分别反汇编验证。
 * 新路径仅接纳出站记录，不扩大入站采集范围。 */
static BOOL outgoing_route(uintptr_t caller) {
#ifdef CH_FIXTURE
    (void)caller; return FALSE;
#else
    static const DWORD returns[] = {0x9c4152,0x9c44d4,0x9dcf90,0x9ddc7e,
        0xa3ba46,0xa3fb46,0x166f44a,0x1680328,0x16e699f,0x16e9874,
        0x16fbc72,0x21e0f8b,0x2a966c8,0x3347833,0x33cbfb3,0x342dae9,
        0x3556e8d,0x4591f63,0x4acfe96,0x4ae2026,0x4afacf5,0x4bd8d28,
        0x504fa01,0x5050782,0x50cb100,0x59ac53a,0x59e8ef8,0x59e91ca,
        /* 0x9e17d0 恢复 kind/subkind，并以 DL=1 尾跳转到同一
         * 终结函数；这些调用方保留各自的返回 PC。 */
        0x1427603,0x1429561,0x1447d78,0x144b828,0x144eac8,0x1450398,
        0x1452883,0x1480989,0x1487038,0x2c9015f,0x2c90e19};
    if (caller < native_base) return FALSE;
    for (unsigned i=0;i<sizeof(returns)/sizeof(*returns);++i)
        if (caller-native_base==returns[i]) return TRUE;
    return FALSE;
#endif
}

/* 固定版本的类型判断函数：0xa0a9c0（图片）、0xa0a7c0（视频）、
 * 0xa0a820（语音）、0xa0a790（表情）、0xa0b730（appmsg）、0xa0ae20（名片）。
 * 载荷语义和资源就绪状态由 API 适配层检查。 */
static BOOL capture_kind(uint32_t kind) {
    switch (kind) {
    case 1: case 3: case 13: case 39: case 34: case 43: case 44:
    case 62: case 47: case 49: case 42: case 66: case 67: return TRUE;
    default: return FALSE;
    }
}

static void publish(ChSession *s, unsigned state) {
    if (s) {
        HookStatus.entry_hits=s->entry_hits; HookStatus.observed=s->observed;
        HookStatus.filtered=s->filtered; HookStatus.rejected=s->rejected;
        HookStatus.dropped=s->dropped; HookStatus.cancelled=s->cancelled;
        HookStatus.last_return_rva=(uint64_t)s->last_return_rva;
        HookStatus.identity_changed=s->identity_changed;
    }
    InterlockedExchange((volatile LONG *)&HookStatus.state, (LONG)state);
}

static void detour(Wx41930Record *record, uint8_t force) {
    uintptr_t caller=(uintptr_t)__builtin_return_address(0);
    ChSession *s=NULL;
    InterlockedIncrement(&HookStatus.callbacks);
    /* 此锁界定会话接纳边界，不代表网络到达时间。
     * 引用获取和指针分离在此串行执行。 */
    if (!reentrant && TryAcquireSRWLockShared(&session_gate)) {
        if (current_session && current_session->accepting) {
            s=current_session;
            InterlockedIncrement(&s->refs);
            InterlockedIncrement(&HookStatus.admitted);
        }
        ReleaseSRWLockShared(&session_gate);
    }
    if (s) {
        InterlockedIncrement(&s->entry_hits);
        InterlockedExchange64(&s->last_return_rva,(LONGLONG)(caller-native_base));
        if (caller!=expected_return && !outgoing_route(caller)) InterlockedIncrement(&s->route_missed);
        else if (force!=1) InterlockedIncrement(&s->force_missed);
        else {
            const char *allowlist[]={s->endpoint};
            StreamSample sample;
            char before_id[129],after_id[129];
            reentrant=1;
            InterlockedIncrement(&s->observed);
            int rc=-1;
            memset(&sample,0,sizeof(sample));
            if (!account_identity(before_id) || strcmp(before_id,s->self_id)) {
                InterlockedExchange(&s->identity_changed,1);
                InterlockedExchange(&s->accepting,0);
            } else {
                rc=stream_snapshot_capture(GetCurrentProcess(),(uintptr_t)record,
                                            expected_vtable,s->endpoint[0]=='*'?NULL:allowlist,s->endpoint[0]=='*'?0:1,&sample);
                if (!account_identity(after_id) || strcmp(after_id,s->self_id)) {
                    InterlockedExchange(&s->identity_changed,1);
                    InterlockedExchange(&s->accepting,0); rc=-1;
                }
            }
            if (rc==1 && capture_kind(sample.raw_c) &&
                (caller==expected_return || !strcmp(sample.endpoint18,s->self_id)) &&
                ((strcmp(sample.endpoint18,s->self_id) && !strcmp(sample.endpoint38,s->self_id)) ||
                 (!strcmp(sample.endpoint18,s->self_id) && strcmp(sample.endpoint38,s->self_id)))) {
                if (TryAcquireSRWLockExclusive(&s->lock)) {
                    if (!s->accepting) InterlockedIncrement(&s->cancelled);
                    else if (s->count<32) {
                        s->queue[(s->head+s->count)%32]=sample; ++s->count;
                    } else InterlockedIncrement(&s->dropped);
                    ReleaseSRWLockExclusive(&s->lock);
                } else InterlockedIncrement(&s->dropped);
            } else if (rc<0) {
                InterlockedIncrement(&s->rejected);
                InterlockedExchangePointer(&s->reject_reason,(void *)(sample.reason ? sample.reason : "identity_changed"));
            } else InterlockedIncrement(&s->filtered);
            SecureZeroMemory(&sample,sizeof(sample)); reentrant=0;
        }
    }
    original(record,force);
    if (s) {
        InterlockedDecrement(&HookStatus.admitted);
        InterlockedDecrement(&s->refs); /* 对该会话的最后一次访问。 */
    }
    InterlockedDecrement(&HookStatus.callbacks);
}

static void write_message(FILE *file, ChSession *s, StreamSample *sample, unsigned sequence) {
    fprintf(file,"{\"schema\":\"client-hook-events-v3\",\"type\":\"message\",\"generation\":%llu,\"seq\":%u,\"raw_kind\":%u,\"body\":",
            (unsigned long long)s->generation,sequence,sample->raw_c);
    stream_json_escape(file,sample->body,sample->body_len);
    fprintf(file,",\"raw_108\":\"%llu\"",(unsigned long long)sample->raw108);
    fputs(",\"native_client_id_278\":",file); stream_json_escape(file,sample->client_id,sample->client_id_len);
    fputs(",\"native_identity_profile\":\"weixin41930_record_identity_v2\"",file);
    if (sample->identity_scalars_complete)
        fprintf(file,",\"raw_110\":\"%llu\",\"raw_created_134\":%u",
                (unsigned long long)sample->raw110,(unsigned)sample->created134);
    fputs(",\"endpoint_18\":",file); stream_json_escape(file,sample->endpoint18,sample->id18_len);
    fputs(",\"endpoint_38\":",file); stream_json_escape(file,sample->endpoint38,sample->id38_len);
    fputs(",\"actor_candidate_58\":",file); stream_json_escape(file,sample->raw58,sample->raw58_len);
    fprintf(file,",\"source_180_complete\":%s,\"source_180\":",sample->source180_complete ? "true" : "false");
    stream_json_escape(file,sample->source180,sample->source180_len);
    fputs(",\"self_id\":",file); stream_json_escape(file,s->self_id,strlen(s->self_id));
    fprintf(file,",\"direction\":\"%s\",\"semantic_profile\":\"weixin-4.1.9.30-record-v2\"}\n",
            !strcmp(sample->endpoint38,s->self_id) ? "incoming" : "outgoing");
}

static BOOL instance_matches(HANDLE process,uint64_t expected) {
    FILETIME t[4];
    return GetProcessTimes(process,&t[0],&t[1],&t[2],&t[3]) &&
           (((uint64_t)t[0].dwHighDateTime<<32)|t[0].dwLowDateTime)==expected;
}

static FILE *open_event_file(const wchar_t *path) {
    HANDLE out=CreateFileW(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
    if (out==INVALID_HANDLE_VALUE) return NULL;
    int fd=_open_osfhandle((intptr_t)out,_O_WRONLY|_O_BINARY);
    if (fd<0) {CloseHandle(out);return NULL;}
    FILE *file=_fdopen(fd,"wb");
    if (!file) {_close(fd);return NULL;}
    setvbuf(file,NULL,_IONBF,0);
    return file;
}

__declspec(dllexport) DWORD WINAPI HookRun(void *argument) {
    ChConfig cfg;
    HMODULE self=NULL,core;
    HANDLE stop=NULL,owner=NULL,disk=INVALID_HANDLE_VALUE;
    FILE *file=NULL;
    ChSession *s=NULL;
    wchar_t path[512];
    unsigned char before[16],after[16];
    const unsigned char live_entry[16]={0x56,0x57,0x53,0x48,0x83,0xec,0x20,0x48,0x89,0xce,0x84,0xd2,0x75,0x09,0x80,0xbe};
    const unsigned char live_call[16]={0xe8,0x1b,0x0f,0,0,0x90,0x48,0x8b,0x85,0x60,1,0,0,0x48,0x81,0xc4};
    unsigned emitted=0,discarded=0,rotation=0;
    DWORD code=3;
    BOOL enabled=FALSE,restored=TRUE;
    const char *phase="config",*reason="startup_error";
    if (!exact((uintptr_t)argument,&cfg,sizeof(cfg)) || cfg.magic!=CH_MAGIC || cfg.version!=3 ||
        cfg.size!=sizeof(cfg) || cfg.pid!=GetCurrentProcessId() || cfg.duration_ms>600000 ||
        cfg.max_events>1000 || !cfg.owner_pid || cfg.reserved ||
        !memchr(cfg.endpoint,0,sizeof(cfg.endpoint)) || !cfg.endpoint[0] ||
        cfg.exe_path[259] || cfg.core_path[259] || cfg.output_path[511] || cfg.stop_name[127] ||
        !instance_matches(GetCurrentProcess(),cfg.creation_time)) return 2;
    if (!GetModuleFileNameW(NULL,path,512) || _wcsicmp(path,cfg.exe_path)) return 2;
    if (InterlockedCompareExchange(&engine_busy,1,0)) return 5;
    HookStatus.run_busy=1;
    /* 仍有已接纳回调使用会话时，不释放已退出会话的内存。 */
    if (retired_session) {
        if (InterlockedCompareExchange(&retired_session->refs,0,0)) { code=5; goto leave; }
        SecureZeroMemory(retired_session,sizeof(*retired_session));
        HeapFree(GetProcessHeap(),0,retired_session); retired_session=NULL;
        publish(NULL,CH_IDLE);
    }
    if (engine_fault) { code=6; goto leave; }
    publish(NULL,CH_STARTING);
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
                            (LPCWSTR)(uintptr_t)HookRun,&self)) goto done;
    file=open_event_file(cfg.output_path);
    if (!file) goto done;
    stop=OpenEventW(SYNCHRONIZE,FALSE,cfg.stop_name);
    owner=OpenProcess(SYNCHRONIZE|PROCESS_QUERY_LIMITED_INFORMATION,FALSE,cfg.owner_pid);
    if (!stop || !owner || !instance_matches(owner,cfg.owner_creation)) goto done;
    phase="fixed-module";
#ifdef CH_FIXTURE
    core=GetModuleHandleW(NULL);
#else
    core=GetModuleHandleW(L"Weixin.dll");
#endif
    if (!core || !GetModuleFileNameW(core,path,512) || _wcsicmp(path,cfg.core_path)) goto done;
    if (engine_created && native_base!=(uintptr_t)core) {engine_fault=TRUE;goto done;}
    disk=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    phase="fixed-module-hash";
    if (disk==INVALID_HANDLE_VALUE || !hash_file(disk,RUNTIME_CORE_SHA256)) goto done;
    if (!engine_created) {
        native_base=(uintptr_t)core;
#ifdef CH_FIXTURE
        engine_target=(uintptr_t)GetProcAddress(core,"fixture_finalize_entry");
        expected_return=(uintptr_t)GetProcAddress(core,"fixture_allowed_return");
        expected_vtable=(uintptr_t)GetProcAddress(core,"fixture_record_vtable");
        if (!engine_target || !expected_return || !expected_vtable) goto done;
#else
        engine_target=native_base+WX41930_FINALIZE_RVA;
        expected_return=native_base+WX41930_ALLOWED_RETURN_RVA;
        expected_vtable=native_base+WX41930_RECORD_VTABLE_RVA;
#endif
    }
    phase="entry-bytes";
    if (!exact(engine_target,before,16)) goto done;
#ifdef CH_FIXTURE
    if (memcmp(before,live_entry,7)) goto done;
    (void)live_call;
#else
    unsigned char call[16];
    if (memcmp(before,live_entry,16) || !exact(native_base+0x9dacd0,call,16) || memcmp(call,live_call,16)) goto done;
#endif
    if (engine_created && memcmp(before,engine_before,16)) {engine_fault=TRUE;goto done;}
    s=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(*s)); if(!s) goto done;
    InitializeSRWLock(&s->lock);
    memcpy(s->endpoint,cfg.endpoint,sizeof(s->endpoint));
    phase="account-identity";
    if (!account_identity(s->self_id) || !strcmp(s->self_id,s->endpoint)) goto done;
    memcpy(HookStatus.self_id,s->self_id,128);
    phase="create-hook";
    if (!engine_created) {
        MH_STATUS init=MH_Initialize();
        if (init!=MH_OK && init!=MH_ERROR_ALREADY_INITIALIZED) goto done;
        if (MH_CreateHook((void *)engine_target,(void *)(uintptr_t)detour,(void **)&original)!=MH_OK) goto done;
        memcpy(engine_before,before,16);engine_created=TRUE;
    }
    s->generation=++HookStatus.generation;
    HookStatus.target_pid=GetCurrentProcessId();HookStatus.owner_pid=cfg.owner_pid;
    publish(s,CH_STARTING);
    if (WaitForSingleObject(stop,0)!=WAIT_TIMEOUT || WaitForSingleObject(owner,0)!=WAIT_TIMEOUT) {
        code=0;reason="cancelled_before_start";goto done;
    }
    AcquireSRWLockExclusive(&session_gate);
    s->accepting=1;current_session=s;
    ReleaseSRWLockExclusive(&session_gate);
    if (MH_EnableHook((void *)engine_target)!=MH_OK) goto done;
    enabled=TRUE;HookStatus.installed=1;publish(s,CH_RUNNING);
    fprintf(file,"{\"schema\":\"client-hook-events-v3\",\"type\":\"ready\",\"pid\":%lu,\"generation\":%llu,\"duration_ms\":%u,\"resident_after_stop\":true,\"self_id\":\"%s\"}\n",
            GetCurrentProcessId(),(unsigned long long)s->generation,cfg.duration_ms,s->self_id);
    ULONGLONG deadline=GetTickCount64()+cfg.duration_ms;
    code=0;reason="duration";
    for (;;) {
        if (WaitForSingleObject(stop,0)!=WAIT_TIMEOUT) {reason="stop";break;}
        if (WaitForSingleObject(owner,0)!=WAIT_TIMEOUT) {reason="controller_exit";break;}
        if (cfg.duration_ms && GetTickCount64()>=deadline) break;
        if (s->dropped) {code=4;reason="queue_overflow";break;}
        char current_id[129];
        if (!account_identity(current_id) || strcmp(current_id,s->self_id)) s->identity_changed=1;
        if (s->identity_changed) {code=4;reason="identity_changed";break;}
        if (ftell(file)>CH_RAW_LOG_LIMIT) {
            wchar_t next_path[544];
            swprintf(next_path,544,L"%ls.%u",cfg.output_path,++rotation);
            FILE *next=open_event_file(next_path);
            if (!next) {code=4;reason="output_rotation_error";break;}
            fprintf(file,"{\"type\":\"rotation\",\"index\":%u}\n",rotation);
            if (ferror(file)) {fclose(next);code=4;reason="output_error";break;}
            fclose(file);file=next;
        }
        StreamSample sample; BOOL have=FALSE;
        AcquireSRWLockExclusive(&s->lock);
        if (s->count) {
            sample=s->queue[s->head];SecureZeroMemory(&s->queue[s->head],sizeof(sample));
            s->head=(s->head+1)%32;--s->count;have=TRUE;
        }
        ReleaseSRWLockExclusive(&s->lock);
        if (have) {
            write_message(file,s,&sample,++emitted);SecureZeroMemory(&sample,sizeof(sample));
            if (ferror(file)) {code=4;reason="output_error";break;}
            if (cfg.max_events && emitted>=cfg.max_events) {reason="max_events";break;}
        } else Sleep(10);
        publish(s,CH_RUNNING);
    }
done:
    publish(s,CH_STOPPING);
    AcquireSRWLockExclusive(&session_gate);
    if (s) s->accepting=0;
    current_session=NULL;
    ReleaseSRWLockExclusive(&session_gate);
    if (enabled) {
        phase="disable-hook";
        if (MH_DisableHook((void *)engine_target)!=MH_OK) restored=FALSE;
        else {HookStatus.installed=0;restored=exact(engine_target,after,16)&&!memcmp(engine_before,after,16);}
        if (!restored) {engine_fault=TRUE;code=4;}
    }
    if (s) {
        ULONGLONG until=GetTickCount64()+2000;
        while (InterlockedCompareExchange(&s->refs,0,0) && GetTickCount64()<until) Sleep(1);
        if (s->refs) {code=4;reason="drain_timeout";}
        AcquireSRWLockExclusive(&s->lock);
        discarded=s->count;SecureZeroMemory(s->queue,sizeof(s->queue));s->count=0;
        ReleaseSRWLockExclusive(&s->lock);
    }
    unsigned state=engine_fault?CH_FAULT:(s&&s->refs)?CH_DRAINING:CH_IDLE;
    publish(s,state);
    if (file) {
        const char *why=s?InterlockedCompareExchangePointer(&s->reject_reason,NULL,NULL):NULL;
        fprintf(file,"{\"schema\":\"client-hook-events-v3\",\"type\":\"end\",\"generation\":%llu,\"code\":%lu,\"phase\":\"%s\",\"reason\":\"%s\",\"restored\":%s,\"active\":%ld,\"entry_hits\":%ld,\"observed\":%ld,\"filtered\":%ld,\"rejected\":%ld,\"dropped\":%ld,\"cancelled\":%ld,\"route_missed\":%ld,\"force_missed\":%ld,\"last_return_rva\":\"0x%llx\",\"last_reject_reason\":\"%s\",\"queued_discarded\":%u,\"identity_changed\":%s,\"emitted\":%u,\"resident_after_stop\":true}\n",
            (unsigned long long)(s?s->generation:0),code,phase,reason,restored?"true":"false",s?s->refs:0,
            s?s->entry_hits:0,s?s->observed:0,s?s->filtered:0,s?s->rejected:0,s?s->dropped:0,s?s->cancelled:0,
            s?s->route_missed:0,s?s->force_missed:0,(unsigned long long)(s?s->last_return_rva:0),why?why:"none",
            discarded,s&&s->identity_changed?"true":"false",emitted);
        fclose(file);
    }
    if (s) {
        if (s->refs) retired_session=s;
        else {SecureZeroMemory(s,sizeof(*s));HeapFree(GetProcessHeap(),0,s);}
        ++HookStatus.completed;
    }
    if (stop) CloseHandle(stop);
    if (owner) CloseHandle(owner);
    if (disk!=INVALID_HANDLE_VALUE) CloseHandle(disk);
leave:
    HookStatus.run_busy=0;InterlockedExchange(&engine_busy,0);
    return code;
}
