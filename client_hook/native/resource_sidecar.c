/* Downloads use the verified native owner queue. The optional operator build
 * CH_SEND_TASK_OBSERVER additionally observes the fixed send Task constructor;
 * it forwards exactly once and remains resident until process exit. */
#define CH_RESOURCE_SIDECAR
#include "runtime.c"
#include "resource_download.inc"
