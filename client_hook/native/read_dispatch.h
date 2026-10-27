#ifndef CH_READ_DISPATCH_H
#define CH_READ_DISPATCH_H
#include "fixture_abi.h"
typedef void (*ChMarkReadFn)(void *, const FixtureString32 *, unsigned, unsigned char);
/* UI 封装将此借用字符串复制到原生持有的排队闭包中。
 * 它从不销毁或持有调用方的字符串；仅验证过 4.1.9.30。 */
static void ch_read_dispatch(ChMarkReadFn function, const char *chat) {
    FixtureString32 value={.size=0,.capacity=15};
    size_t length=strlen(chat);
    value.size=length;
    if (length<16) memcpy(value.data.small,chat,length+1);
    else {value.data.heap=(char *)chat;value.capacity=length;}
    function(NULL,&value,3,1);
}
#endif
