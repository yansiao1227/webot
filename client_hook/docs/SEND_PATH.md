# 发送方可见、对方未收到：发送分支排查

日期：2026-10-22；适配对象固定为微信4.1.9.30 x64，Weixin.dll SHA256 为 `89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494`。

## 已观察到的事实

- 颜料盒私聊、测试群均收到接口调用产生的本地显示，内部结果 accepted，原生提交各1次，但对方客户端均看不到。
- 用户在微信界面手动向相同两处发送，确认两条都可收到。
- 原始接口结果一直为 delivery_confirmed=false。此前用户“显示确认”未区分发送方和接收方，真实送达通过结论已撤回。

## 离线调用链证据

以下是普通文件读取、objdump 和已安装 Ghidra 的只读项目分析结果，不是实时网络观测；函数标签为分析者命名。没有执行目标 DLL 或修改微信进程。原始结果保存在 `state/send-delivery-investigation/`，复用了同一文件哈希的既有分析。

1. 原 profile 的 P_SUBMIT 为 RVA `0x166AAD0`；包装器调用 `0x16699E0` 前明确将 R9D 清零。另一包装器 `0x166C970` 使用相同参数与引用计数清理形状，但传 R9B=1。对照见 `work/engine-output-second/send_message_wrapper_a.asm`、`send_message_wrapper_b.asm`。
2. core 将该值传入任务构造函数 `0x3552820`，存到任务偏移 `0xB8`。新分析见 `state/send-delivery-investigation/decompiled/task_ctor_flag.asm`。
3. 任务构造中的 `0x3552CF0` 用该值影响消息记录 `+0x128`，赋值为 `2-flag`。字段的原始符号名尚未确认，不能仅按偏移命名。见 `decompiled-next/task_build.c` 及对应汇编。
4. `0x16BD300` 读取该标志；为0时走本地处理后构造返回结果，跳过后续路径。`0x166DF10` 也要求该标志非0，才进入成功后的异步任务队列分支。见既有 `work/engine-output/candidate_send_stage_a.c` 和 `work/engine-output-second/send_existing_task_candidate.c`。这些静态分支事实与用户观察吻合，但不构成服务器收件证据。

## 修复与验证边界

已将模块内 `profiles/weixin41930_send.h` 的 P_SUBMIT 改为 `0x166C970`，核对新入口16字节及包装器 R9B=1 指令。没有复制其他版本的地址或对象布局，也没有引入参考仓库运行依赖。

新构建为 `build/runtime-20261022T131638Z-1621747a/`；17项 HTTP/原生自有 host 测试和两项生命周期回归通过。fixture 不模拟真实服务器，这些通过结果不能代替真实收件验证。

用户正常重启并登录后，已确认加载新构建 `runtime-20261022T131638Z-1621747a`。P02/G02 两条新请求均仅原生提交1次、返回 accepted；随后用户回复“我已收到”，确认本轮两会话对方客户端收件。证据为平级模块 `wechat_mcp/state/tunnel/send-fix-test.json`，仅用于验收，不是运行依赖。原始 operation.delivery_confirmed 仍为 false，另以 peer_delivery_confirmed_by_user 记录人工确认。旧 P01/G01 的失败证据保留，不能追溯改写为成功。

原生直接发送复测通过，MCP 已恢复真实上游；新版 Dot 对P03/G03各自动回复1次，用户明确确认私聊与群聊对方客户端均已收到，修复后自动回复端到端收件复测通过；测试订阅已取消。
