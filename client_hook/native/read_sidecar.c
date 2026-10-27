/* No detours; invoke the same self-queuing wrapper as MarkSessionRead in UI. */
#include "runtime.c"
#include "read-profile.h"
#include "read_dispatch.h"
typedef struct {
    DWORD magic,version,size,pid,mode,error,calls,submitted;
    char account[128],request_id[64],chat[128];
} ChReadRpc;
typedef struct ReadEntry {ChReadRpc rpc;struct ReadEntry *next;} ReadEntry;
static SRWLOCK read_gate=SRWLOCK_INIT;
static ReadEntry *read_entries;
static unsigned read_count;
static BOOL read_chat_valid(const char *chat) {
    size_t n=strlen(chat),end=n;
    if (n<1 || n>127) return FALSE;
    if (n>9 && !strcmp(chat+n-9,"@chatroom")) {
        end=n-9;
        for(size_t i=0;i<end;i++) if(chat[i]<'0'||chat[i]>'9') return FALSE;
        return TRUE;
    }
    if (n<6 || strncmp(chat,"wxid_",5)) return FALSE;
    for(size_t i=5;i<end;i++)
        if(!((chat[i]>='A'&&chat[i]<='Z')||(chat[i]>='a'&&chat[i]<='z')||
             (chat[i]>='0'&&chat[i]<='9')||chat[i]=='_'||chat[i]=='-')) return FALSE;
    return TRUE;
}
__declspec(dllexport) DWORD WINAPI HookMarkRead(void *argument) {
    ChReadRpc input;
    char identity[129];
    MODULEINFO info;
    HMODULE core=GetModuleHandleW(L"Weixin.dll"),pin=NULL;
    DWORD error=ERROR_INVALID_PARAMETER;
    if(!read_self(argument,&input,sizeof(input)) || input.magic!=0x43524d31 ||
       input.version!=1 || input.size!=sizeof(input) || input.pid!=GetCurrentProcessId() || input.mode>1 ||
       !memchr(input.account,0,128)||!memchr(input.chat,0,128)||!memchr(input.request_id,0,64)||
       !input.request_id[0]||!read_chat_valid(input.chat)) return error;
    if(!TryAcquireSRWLockExclusive(&read_gate)) return ERROR_BUSY;
    native_base=(uintptr_t)core;
    if(!core||!profile_matches(core,&info,&real_profile)||!account_identity(identity)||strcmp(identity,input.account)) {
        error=ERROR_INVALID_DATA;goto done;
    }
    for(size_t i=0;i<sizeof(read_functions)/sizeof(read_functions[0]);i++)
        if(!code_matches((uintptr_t)core,real_profile.image_size,&read_functions[i])) {
            error=ERROR_INVALID_DATA;goto done;
        }
    ReadEntry *entry;
    for(entry=read_entries;entry;entry=entry->next)
        if(!strcmp(entry->rpc.request_id,input.request_id)) break;
    if(entry) {
        if(strcmp(entry->rpc.chat,input.chat)||strcmp(entry->rpc.account,input.account)) {
            error=ERROR_ALREADY_EXISTS;goto done;
        }
    } else {
        if(input.mode) {error=ERROR_NOT_FOUND;goto done;}
        if(read_count>=4096) {error=ERROR_NOT_ENOUGH_QUOTA;goto done;}
        if(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
             (LPCWSTR)(uintptr_t)HookMarkRead,&pin)) {error=ERROR_INVALID_DATA;goto done;}
        entry=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(*entry));
        if(!entry) {error=ERROR_OUTOFMEMORY;goto done;}
        entry->rpc=input;entry->rpc.calls=1;entry->rpc.submitted=0;entry->rpc.error=ERROR_IO_PENDING;
        entry->next=read_entries;read_entries=entry;++read_count;
        /* Persist intent in resident operation before native side effect. */
        ch_read_dispatch((ChMarkReadFn)((uintptr_t)core+0x15b3570),entry->rpc.chat);
        entry->rpc.submitted=1;entry->rpc.error=0;
    }
    memcpy(argument,&entry->rpc,sizeof(input));error=entry->rpc.error;
done:
    ReleaseSRWLockExclusive(&read_gate);return error;
}
