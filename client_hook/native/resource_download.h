#ifndef CH_RESOURCE_DOWNLOAD_H
#define CH_RESOURCE_DOWNLOAD_H
#define CH_RESOURCE_MAGIC 0x43524431u
typedef struct {
    uint32_t magic, version, size, pid, mode, error, stage, calls;
    char account[128], request_id[64], file_id[4096], aes_key[128], output[2048];
    uint64_t expected_size;
    uint32_t file_type, done, callback_tid, cleanup_ok;
    uint32_t native_done, native_status_0, native_status_1, future_state;
    uint64_t transferred, total;
    uint32_t task_found, task_state, task_phase, task_count;
    char native_md5[33];
} ChResourceRpc;
_Static_assert(sizeof(ChResourceRpc)==6608, "resource RPC Windows x64 ABI");
_Static_assert(offsetof(ChResourceRpc,expected_size)==6496, "resource size offset");
#endif
