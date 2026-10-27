#ifndef CH_READ_DISPATCH_H
#define CH_READ_DISPATCH_H
#include "fixture_abi.h"
typedef void (*ChMarkReadFn)(void *, const FixtureString32 *, unsigned, unsigned char);
/* The UI wrapper copies this borrowed string into a native-owned queued closure.
 * It never destroys or retains the caller's string. Verified 4.1.9.30 only. */
static void ch_read_dispatch(ChMarkReadFn function, const char *chat) {
    FixtureString32 value={.size=0,.capacity=15};
    size_t length=strlen(chat);
    value.size=length;
    if (length<16) memcpy(value.data.small,chat,length+1);
    else {value.data.heap=(char *)chat;value.capacity=length;}
    function(NULL,&value,3,1);
}
#endif
