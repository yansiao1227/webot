#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "weixin41930.h"
#include "fixture_abi.h"
extern void fixture_allowed_call(Wx41930Record *, uint8_t);
uintptr_t fixture_record_vtable[1];
volatile LONG fixture_logged_in=1;
static unsigned calls;
static BOOL slow_call;
static BOOL hold_call;
static HANDLE release_event;
void fixture_finalize_body(Wx41930Record *record, uint8_t force) {
    ++calls;
    if (force || !record->parsed_flag) record->parsed_flag = 1;
    if (slow_call) Sleep(1000);
    if (hold_call) WaitForSingleObject(release_event,INFINITE);
}
static void set_string(Wx41930String *s, const char *v) {
    size_t n = strlen(v);
    s->length = n; s->capacity = n < 16 ? 15 : n;
    if (n < 16) memcpy(s->storage.inline_bytes, v, n + 1);
    else s->storage.heap = (uintptr_t)v;
}
int main(void) {
    HMODULE sender=LoadLibraryW(L"Weixin.dll");
    FixtureStartFn start=sender?(FixtureStartFn)(uintptr_t)GetProcAddress(sender,"FixtureStart"):NULL;
    FixtureStopFn stop=sender?(FixtureStopFn)(uintptr_t)GetProcAddress(sender,"FixtureStop"):NULL;
    if (!start || !stop || !start(0)) return 3;
    char command[64];
    wchar_t name[128];
    swprintf(name,128,L"Local\\HookFixtureRelease-%lu",GetCurrentProcessId());
    release_event=CreateEventW(NULL,TRUE,TRUE,name);
    if (!release_event) return 2;
    printf("{\"type\":\"host_ready\",\"pid\":%lu}\n", GetCurrentProcessId()); fflush(stdout);
    while (fgets(command, sizeof(command), stdin)) {
        if (!strcmp(command, "quit\n")) break;
        if (!strcmp(command,"logout\n")) {InterlockedExchange(&fixture_logged_in,0);puts("{\"type\":\"configured\"}");fflush(stdout);continue;}
        if (!strcmp(command,"cache-nonempty\n")) {((FixtureSetCacheNonemptyFn)(uintptr_t)GetProcAddress(sender,"FixtureSetCacheNonempty"))(TRUE);puts("{\"type\":\"configured\"}");fflush(stdout);continue;}
        if (!strcmp(command,"result-heap\n") || !strcmp(command,"result-large\n")) {((FixtureSetResultModeFn)(uintptr_t)GetProcAddress(sender,"FixtureSetResultMode"))(!strcmp(command,"result-heap\n")?1:2);puts("{\"type\":\"configured\"}");fflush(stdout);continue;}
        if (!strcmp(command,"bad-thread\n") || !strcmp(command,"good-thread\n")) {((FixtureSetExpectedTidMismatchFn)(uintptr_t)GetProcAddress(sender,"FixtureSetExpectedTidMismatch"))(!strcmp(command,"bad-thread\n"));puts("{\"type\":\"configured\"}");fflush(stdout);continue;}
        if (!strcmp(command,"delay\n")) {((void(*)(DWORD))(uintptr_t)GetProcAddress(sender,"FixtureSetDelay"))(3000);puts("{\"type\":\"configured\"}");fflush(stdout);continue;}
        if (!strcmp(command,"stats\n")) {
            FixtureStats st;((FixtureGetStatsFn)(uintptr_t)GetProcAddress(sender,"FixtureGetStats"))(&st);
            printf("{\"type\":\"stats\",\"submitted\":%ld,\"accepted\":%ld,\"text_allocs\":%ld,\"text_destroy\":%ld,\"ctrl_allocs\":%ld,\"ctrl_frees\":%ld,\"result_bad_free\":%ld,\"input_bad_refs\":%ld,\"heap_allocs\":%ld,\"heap_frees\":%ld,\"large_allocs\":%ld,\"large_frees\":%ld}\n",st.submit_calls,st.submit_accepted,st.text_allocs,st.text_destroy,st.ctrl_allocs,st.ctrl_frees,st.result_free_bad_size,st.input_bad_refs,st.result_heap_allocs,st.result_heap_frees,st.result_large_allocs,st.result_large_frees);fflush(stdout);continue;
        }
        if (!strcmp(command, "emit\n") || !strcmp(command, "emit-slow\n") || !strcmp(command,"emit-held\n") || !strncmp(command,"emit-group",10) || !strcmp(command,"emit-outbound\n") || !strcmp(command,"emit-long\n") || !strcmp(command,"emit-image\n") || !strcmp(command,"emit-reference\n")) {
            slow_call = !strcmp(command, "emit-slow\n");
            hold_call = !strcmp(command,"emit-held\n");
            if (hold_call) ResetEvent(release_event);
            Wx41930Record record = {0};
            record.vtable = (uintptr_t)fixture_record_vtable; record.raw_kind = 1;
            set_string(&record.endpoint_a, "wxid_fixture_peer");
            set_string(&record.endpoint_b, "wxid_fixture_self");
            set_string(&record.actor_candidate, "wxid_fixture_peer");
            set_string(&record.text, "\xe4\xb8\xad\xe6\x96\x87 local event\n\"fixture\"");
            set_string(&record.source, "<msgsource/>");
            if (!strcmp(command,"emit-image\n")) {
                record.raw_kind=3;record.raw_108=123456789;
                set_string(&record.text,"<msg><img md5=\"0123456789abcdef0123456789abcdef\" length=\"100\"/></msg>");
            }
            if (!strcmp(command,"emit-reference\n")) {
                record.raw_kind=49;record.raw_108=987654321;
                set_string(&record.text,"<msg><appmsg><type>57</type><title>analyze image</title><refermsg><svrid>123456789</svrid></refermsg></appmsg></msg>");
            }
            if (!strncmp(command,"emit-group",10)) set_string(&record.endpoint_a,"123456@chatroom");
            if (!strcmp(command,"emit-group-at\n")) set_string(&record.source,"<msgsource><atuserlist><![CDATA[wxid_fixture_self,wxid_fixture_peer]]></atuserlist></msgsource>");
            if (!strcmp(command,"emit-group-other\n")) set_string(&record.source,"<msgsource><atuserlist>wxid_fixture_peer</atuserlist></msgsource>");
            if (!strcmp(command,"emit-group-all\n")) set_string(&record.source,"<msgsource><atuserlist>notify@all</atuserlist></msgsource>");
            if (!strcmp(command,"emit-group-invalid\n")) {record.source.capacity=100;record.source.length=10;record.source.storage.heap=1;}
            if (!strcmp(command,"emit-outbound\n")) {set_string(&record.endpoint_a,"wxid_fixture_self");set_string(&record.endpoint_b,"wxid_fixture_peer");}
            static char long_body[60001];
            if (!strcmp(command,"emit-long\n")) {memset(long_body,'a',60000);long_body[60000]=0;set_string(&record.text,long_body);}
            fixture_allowed_call(&record, 1);
            memset(&record, 0xa5, sizeof(record));
            printf("{\"type\":\"emitted\",\"calls\":%u}\n", calls); fflush(stdout);
        }
    }
    stop();
    CloseHandle(release_event);
    return 0;
}
