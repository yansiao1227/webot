#include <assert.h>
#include <stdio.h>
#include "outgoing_identity.h"
static unsigned char arena[8192];
static uintptr_t a(size_t offset) {return (uintptr_t)(arena+offset);}
static int read_owned(const void *source,void *dest,size_t size) {
    uintptr_t p=(uintptr_t)source,begin=a(0),end=a(sizeof(arena));
    if(p<begin || p>end || size>end-p) return 0;
    memcpy(dest,source,size);return 1;
}
static void put(size_t at,uint64_t value) {memcpy(arena+at,&value,8);}
int main(void) {
    const uintptr_t base=0x180000000ULL, input=0x12345678;
    /* manager=0,state=0xc00,head=0xd00,node=0xe00,vector=0xf00,
       task=0x1000,record=0x1400. */
    put(0xb78,a(0xc00));put(0xc98,a(0xd00));put(0xca0,1);
    put(0xd00,a(0xe00));put(0xd08,a(0xe00));
    put(0xe00,a(0xd00));put(0xe08,a(0xd00));
    memcpy(arena+0xe10,"123@chatroom",13);put(0xe20,12);put(0xe28,15);
    put(0xe30,a(0xf00));put(0xe38,a(0xf10));put(0xe40,a(0xf20));
    put(0xf00,a(0x1000));put(0xf08,a(0x1800));
    put(0x1000,base+0x874c1e8);put(0x1008,input);put(0x1018,a(0x1400));
    put(0x1400,base+0x8096da8);put(0x1510,123456789);
    assert(ch_outgoing_record110(read_owned,base,a(0),input,"123@chatroom")==123456789);
    assert(ch_outgoing_record110(read_owned,base,a(0),input+1,"123@chatroom")==0);
    assert(ch_outgoing_record110(read_owned,base,a(0),input,"124@chatroom")==0);
    put(0xf10,a(0x1000));put(0xf18,a(0x1800));put(0xe38,a(0xf20));
    assert(ch_outgoing_record110(read_owned,base,a(0),input,"123@chatroom")==0);
    put(0xe38,a(0xf10));put(0x1000,base+1);
    assert(ch_outgoing_record110(read_owned,base,a(0),input,"123@chatroom")==0);
    put(0x1000,base+0x874c1e8);put(0xe00,a(0xe00));
    assert(ch_outgoing_record110(read_owned,base,a(0),input,"123@chatroom")==0);
    put(0xe00,a(0xd00));put(0xe30,1);
    assert(ch_outgoing_record110(read_owned,base,a(0),input,"123@chatroom")==0);
    puts("exact input identity, conversation isolation, ambiguity and invalid layout: passed");
    return 0;
}
