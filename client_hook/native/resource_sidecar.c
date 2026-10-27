/* 下载使用已验证的原生所属队列。可选的操作员构建
 * CH_SEND_TASK_OBSERVER 还会观察固定的发送 Task 构造器；
 * 每次调用仅转发一次，并保持驻留直到进程退出。 */
#define CH_RESOURCE_SIDECAR
#include "runtime.c"
#include "resource_download.inc"
