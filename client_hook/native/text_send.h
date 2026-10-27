#ifndef CH_TEXT_SEND_H
#define CH_TEXT_SEND_H
#include "send_diagnostics.h"
#define CH_SEND_MAGIC 0x43535433u
typedef struct {
    uint32_t magic,version,size,pid,query,error;
    char request_id[64],account[128],target[128],body[65537];
    bridge_config result;
    char native_client_id[129];
} ChSendRpc;
_Static_assert(sizeof(ChSendRpc)==66312,"text RPC v4 Windows x64 ABI");
#endif
