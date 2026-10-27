#ifndef CH_OUTGOING_IDENTITY_H
#define CH_OUTGOING_IDENTITY_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

/* Fixed 4.1.9.30 layout, read only on its owner queue after submit.
 * 166a188 inserts Task into state+90, keyed by Task+38 (conversation).
 * Task+08 retains the exact TextInput; Task+18 retains its Record.
 * Identity requires pointer equality to this submit's input, never text/time.
 * Caller must pin the core SHA and ensure no other native queue can mutate it.
 * Zero is unavailable, not evidence of successful correlation. */
typedef int (*ChIdentityRead)(const void *, void *, size_t);
static uint64_t ch_outgoing_record110(ChIdentityRead read, uintptr_t base,
                                     uintptr_t manager, uintptr_t input,
                                     const char *conversation) {
    uintptr_t state, header[2], again[2], links[2], node, task, vt, source, record;
    uintptr_t vector[3], retained[2];
    uint64_t string[4], found=0, candidate;
    size_t length=strlen(conversation);
    char key[129];
    unsigned matches=0, slots=0;
    if (!input || !length || length>128 ||
        !read((void *)(manager+0xb78),&state,8) || !state ||
        !read((void *)(state+0x98),header,16) || !header[0] || header[1]>1024 ||
        !read((void *)header[0],links,16)) return 0;
    node=links[0];
    for (uintptr_t index=0; index<header[1]; ++index) {
        if (!node || node==header[0] || !read((void *)(node+0x10),string,32) ||
            string[2]>128 || string[3]<string[2] || string[3]>1048576) return 0;
        if (string[2]==length) {
            uintptr_t data=string[3]<16?node+0x10:(uintptr_t)string[0];
            if (!read((void *)data,key,length+1)) return 0;
            if (!memcmp(key,conversation,length+1)) {
                if (!read((void *)(node+0x30),vector,24) || vector[1]<vector[0] ||
                    vector[2]<vector[1] || (vector[1]-vector[0])%16 ||
                    vector[1]-vector[0]>16*1024) return 0;
                for (uintptr_t slot=vector[0];slot<vector[1];slot+=16) {
                    if (++slots>1024) return 0;
                    if (!read((void *)slot,retained,16) || !(task=retained[0]) || !retained[1] ||
                        !read((void *)task,&vt,8) || vt!=base+0x874c1e8 ||
                        !read((void *)(task+8),&source,8)) return 0;
                    if (source!=input) continue;
                    if (++matches>1 || !read((void *)(task+0x18),&record,8) || !record ||
                        !read((void *)record,&vt,8) || vt!=base+0x8096da8 ||
                        !read((void *)(record+0x110),&candidate,8) || !candidate) return 0;
                    found=candidate;
                }
            }
        }
        if (!read((void *)node,links,16)) return 0;
        node=links[0];
    }
    if (node!=header[0] || !read((void *)(state+0x98),again,16) ||
        memcmp(header,again,16) || !read((void *)(manager+0xb78),&node,8) || node!=state) return 0;
    return matches==1?found:0;
}
#endif
