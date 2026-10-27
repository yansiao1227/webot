/* The constructor ABI and record layouts are fixed-artifact evidence. This
 * observer forwards every call exactly once; only our owner-thread TLS entry
 * and exact input pointer may receive copied metadata. No native ownership is
 * acquired, and the observer module stays pinned for process lifetime. */
typedef struct ChTaskObservation {
    uintptr_t input;
    DWORD tid;
    unsigned matches;
    uint64_t record110;
    uint64_t record108;
    uint32_t created134;
} ChTaskObservation;
typedef void *(*ChTaskCtor)(void *, const FixtureSharedRef *, unsigned char);
static ChTaskCtor ch_task_original;
static DWORD ch_task_tls=TLS_OUT_OF_INDEXES;
static uintptr_t ch_task_vt,ch_record_vt;
static LONG ch_observer_state;

static void *ch_task_detour(void *task,const FixtureSharedRef *input,unsigned char flag) {
    ChTaskObservation *watch=ch_task_tls==TLS_OUT_OF_INDEXES?NULL:TlsGetValue(ch_task_tls);
    FixtureSharedRef source={0};
    BOOL matched=watch && watch->tid==GetCurrentThreadId() && watch->input &&
        read_self(input,&source,sizeof(source)) && (uintptr_t)source.object==watch->input;
    void *result=ch_task_original(task,input,flag);
    if (matched) {
        uintptr_t vt=0,retained=0,record=0;
        if (++watch->matches!=1 || result!=task ||
            !read_self(task,&vt,8) || vt!=ch_task_vt ||
            !read_self((BYTE *)task+8,&retained,8) || retained!=watch->input ||
            !read_self((BYTE *)task+0x18,&record,8) || !record ||
            !read_self((void *)record,&vt,8) || vt!=ch_record_vt ||
            !read_self((void *)(record+0x110),&watch->record110,8) ||
            !read_self((void *)(record+0x108),&watch->record108,8) ||
            !read_self((void *)(record+0x134),&watch->created134,4)) {
            watch->record110=0;watch->record108=0;watch->created134=0;
        }
    }
    return result;
}

static BOOL ch_task_observer_install(uintptr_t target,const BYTE expected[16],
                                     uintptr_t task_vt,uintptr_t record_vt) {
    if (ch_observer_state) return ch_observer_state==1;
    ch_observer_state=-1;
    BYTE observed[16];HMODULE pin=NULL;
    if (!read_self((void *)target,observed,16) || memcmp(observed,expected,16) ||
        !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
            (LPCWSTR)(uintptr_t)ch_task_detour,&pin)) return FALSE;
    ch_task_tls=TlsAlloc();
    if (ch_task_tls==TLS_OUT_OF_INDEXES) return FALSE;
    ch_task_vt=task_vt;ch_record_vt=record_vt;
    MH_STATUS init=MH_Initialize();
    if (init!=MH_OK && init!=MH_ERROR_ALREADY_INITIALIZED) return FALSE;
    if (MH_CreateHook((void *)target,(void *)(uintptr_t)ch_task_detour,
                      (void **)&ch_task_original)!=MH_OK) return FALSE;
    if (MH_EnableHook((void *)target)!=MH_OK) return FALSE;
    ch_observer_state=1;
    return TRUE;
}

static BOOL ch_task_observe_begin(ChTaskObservation *watch,uintptr_t input) {
    if (ch_observer_state!=1 || TlsGetValue(ch_task_tls)) return FALSE;
    memset(watch,0,sizeof(*watch));watch->input=input;watch->tid=GetCurrentThreadId();
    return TlsSetValue(ch_task_tls,watch);
}
static void ch_task_observe_end(ChTaskObservation *watch) {
    if (ch_task_tls!=TLS_OUT_OF_INDEXES && TlsGetValue(ch_task_tls)==watch)
        TlsSetValue(ch_task_tls,NULL);
    watch->input=0;
    if (watch->matches!=1) {watch->record110=0;watch->record108=0;watch->created134=0;}
}
