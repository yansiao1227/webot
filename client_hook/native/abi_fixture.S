.text
.globl fixture_finalize_entry
.def fixture_finalize_entry; .scl 2; .type 32; .endef
.seh_proc fixture_finalize_entry
fixture_finalize_entry:
    /* Same first seven instruction bytes as Weixin.dll+9DBBF0. */
    pushq %rsi
    .seh_pushreg %rsi
    pushq %rdi
    .seh_pushreg %rdi
    pushq %rbx
    .seh_pushreg %rbx
    subq $32, %rsp
    .seh_stackalloc 32
    .seh_endprologue
    call fixture_finalize_body
    addq $32, %rsp
    popq %rbx
    popq %rdi
    popq %rsi
    ret
.seh_endproc

.globl fixture_allowed_call
.globl fixture_allowed_return
.def fixture_allowed_call; .scl 2; .type 32; .endef
.seh_proc fixture_allowed_call
fixture_allowed_call:
    subq $40, %rsp
    .seh_stackalloc 40
    .seh_endprologue
    call fixture_finalize_entry
fixture_allowed_return:
    addq $40, %rsp
    ret
.seh_endproc

.globl fixture_other_call
.def fixture_other_call; .scl 2; .type 32; .endef
.seh_proc fixture_other_call
fixture_other_call:
    subq $40, %rsp
    .seh_stackalloc 40
    .seh_endprologue
    call fixture_finalize_entry
    addq $40, %rsp
    ret
.seh_endproc
