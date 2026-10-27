#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <stdio.h>
#include "MinHook.h"
#include "fixture_abi.h"
static BOOL read_self(const void *p,void *out,size_t n) {
    SIZE_T got=0;return ReadProcessMemory(GetCurrentProcess(),p,out,n,&got)&&got==n;
}
#include "task_observer.inc"
static unsigned char record[0x298],task[0xe0];
static unsigned calls;
static unsigned char last_flag;
static void put(void *p,uint64_t v) {memcpy(p,&v,8);}
__attribute__((noinline,optimize("O0")))
static void *fixture_ctor(void *result,const FixtureSharedRef *source,unsigned char flag) {
    ++calls;last_flag=flag;
    put(result,0x11110000);put((BYTE *)result+8,(uintptr_t)source->object);
    put((BYTE *)result+0x18,(uintptr_t)record);
    return result;
}
static ChTaskCtor volatile invoke=fixture_ctor;
static FixtureSharedRef source_a={(void *)0x12345678,(void *)0x87654321};
static DWORD WINAPI another_thread(void *unused) {
    (void)unused;assert(invoke(task,&source_a,7)==task);return 0;
}
int main(void) {
    BYTE entry[16];ChTaskObservation watch;
    memcpy(entry,(void *)(uintptr_t)fixture_ctor,16);
    put(record,0x22220000);put(record+0x110,123456789);put(record+0x108,987654321);
    uint32_t created=12345;memcpy(record+0x134,&created,4);
    assert(ch_task_observer_install((uintptr_t)fixture_ctor,entry,0x11110000,0x22220000));
    assert(calls==0);
    assert(ch_task_observe_begin(&watch,(uintptr_t)source_a.object));
    assert(!ch_task_observe_begin(&watch,(uintptr_t)source_a.object));
    FixtureSharedRef other={(void *)0x12345679,(void *)0x87654321};
    assert(invoke(task,&other,9)==task);assert(last_flag==9 && watch.matches==0);
    HANDLE worker=CreateThread(NULL,0,another_thread,NULL,0,NULL);
    assert(worker && WaitForSingleObject(worker,5000)==WAIT_OBJECT_0);CloseHandle(worker);
    assert(watch.matches==0);
    assert(invoke(task,&source_a,42)==task);ch_task_observe_end(&watch);
    assert(calls==3 && last_flag==42 && watch.matches==1);
    assert(watch.record110==123456789 && watch.record108==987654321 && watch.created134==12345);
    assert(invoke(task,&source_a,0)==task);assert(watch.matches==1);
    assert(ch_task_observe_begin(&watch,(uintptr_t)source_a.object));
    assert(invoke(task,&source_a,0)==task);assert(invoke(task,&source_a,0)==task);
    ch_task_observe_end(&watch);assert(watch.matches==2 && !watch.record110);
    assert(MH_DisableHook((void *)(uintptr_t)fixture_ctor)==MH_OK);
    assert(MH_Uninitialize()==MH_OK);
    puts("constructor forwarding, exact input, thread isolation, duplicate rejection and lifetime: passed");
    return 0;
}
