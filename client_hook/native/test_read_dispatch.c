#include <windows.h>
#include <string.h>
#include <assert.h>
#include "read_dispatch.h"
static const char *expected;
static int calls;
static void wrapper(void *unused,const FixtureString32 *s,unsigned scene,unsigned char notify) {
    assert(!unused && scene==3 && notify==1);
    assert(s->size==strlen(expected));
    assert(!strcmp(s->capacity>15?s->data.heap:s->data.small,expected));
    ++calls;
}
int main(void) {
    expected="wxid_short";ch_read_dispatch(wrapper,expected);
    expected="48418824345@chatroom";ch_read_dispatch(wrapper,expected);
    expected="wxid_ptf2vy5yblno12";ch_read_dispatch(wrapper,expected);
    assert(calls==3);return 0;
}
