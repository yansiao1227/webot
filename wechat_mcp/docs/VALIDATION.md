# 验证范围

2026-10-27：旧云接入组件与配置已移除。下文相关Dot/隧道内容仅为历史验收记录，旧state目录证据已删除，不再作为当前配置或操作步骤。当前本机服务使用`wechat_mcp/state/live/`。

**2026-10-22 最新状态：** 旧 P01/G01 自动回复只有本地显示，对端未收到，旧送达通过结论撤回。原生提交分支已修复，微信重启加载新版后，直接发送 P02/G02 私聊与群聊获用户确认对方收到。MCP 已恢复真实上游，P03/G03 的 Dot 自动回复已各原生提交1次，用户明确确认对方客户端两处都已收到，修复后的端到端验收通过。以下旧阶段的“显示确认”仅指发送方客户端；accepted 没有对端回执语义。

2026-10-22：22项回归通过（含现有 client_hook 的 HTTP 合同测试及4项隧道配置测试），官方 SDK 的3种传输/协商组合通过。Dot 模拟端到端验收通过，旧真实事件自动回复验证到本地接受；旧两次发送的对端收件失败，新版复测进行中。

## 已完成

- demo 真实本机 HTTP 闭环：3条注入、2条签名 webhook、2条规则回复、未授权会话过滤、重复回复不重发。
- 标准库回归：订阅 challenge、续期/轮换/取消/过期、重试和终止、HTTP 认证与 Origin、并发回复、冲突、发送响应丢失、未知结果、账号切换拒绝、DPAPI、消费重启与独立进度、长轮询、容量事务回滚、投递中断恢复、stdio EOF。
- 可选合同测试使用现有 client_hook 的 Application/Server/FixtureBackend，实际调用 HTTP、SQLite 和发送队列；不加载 Hook DLL。
- 官方 Python MCP SDK 2.3.0：HTTP MCP 2.0 discover、旧握手 initialize、tools/list/get_messages/reply_text/ack_messages 以及 stdio 初始化与状态调用通过。同一事件跨两种 HTTP 客户端只产生1次模拟发送。
- 实际 Dot“颜料盒dot”：隧道 ready、插件发现、工具调用、订阅 challenge、1个有效订阅、模拟事件首次投递、自动调用 reply_text、带原标记的中文回复 accepted。注入后未发送聊天指令推动回复。证据见 state/tunnel/dot-event-test.json。相关旧接入说明已移除。
- 实际微信 + Dot：新建独立真实状态目录，核验账号及两条允许会话；P01/G01 两条实时入站均首次投递成功，Dot 自动回复各1次，原生 native_submit_invocations 各为1，最终均 accepted。用户明确确认“两处都已显示”；两条入站后未手动唤醒 Dot 或代发。任务按预定规则取消订阅，有效订阅0，服务保留运行。证据见 state/tunnel/live-test-result.json。
- 延迟：从本地采集到持久回复意图，私聊42.016秒、群聊128.269秒；包含云端调度、模型与工具调用，不代表网络回调耗时或实际显示时间。

报告位于模块 `state/test-results.json`、`state/sdk-validation-result.json` 和 `state/demo-*/demo-result.json`；这些运行数据不作为源码依赖。

## 重现

从项目根目录：

```powershell
python -B -X utf8 -W error::ResourceWarning .\wechat_mcp\run_tests.py
python -B -X utf8 .\wechat_mcp\bridge.py demo
# 可选 SDK 测试，依赖仅安装在此虚拟环境里
python -m venv .\wechat_mcp\state\sdk-validation
.\wechat_mcp\state\sdk-validation\Scripts\python.exe -m pip install -r .\wechat_mcp\validation\requirements-sdk.txt
.\wechat_mcp\state\sdk-validation\Scripts\python.exe -B -X utf8 .\wechat_mcp\validation\sdk_validation.py
```

独立拷贝 wechat_mcp 后，普通回归可运行；缺少平级 client_hook 时仅跳过可选合同测试。桥接运行和 demo 不需要 SDK 依赖。

## 尚未完成

长期运行、持续多消息压力、订阅自动续期及多租户认证尚未验收；当前短时结果不提供秒级回复保证。当前状态见[项目状态](../../docs/PROJECT_STATUS.md)。

## 修复后的真实自动回复验收

2026-10-22：私聊P03、群聊G03入站均首次回调投递成功；Dot各自动回复1次，原生提交各1次、accepted，随后取消本轮订阅。用户明确确认“两处都已收到”，确认位置为对方客户端。采集至回复意图分别49.829秒和154.657秒，不代表送达时间或延迟保证。证据：`state/tunnel/send-fix-dot-test.json`。原始接口不提供对端回执，人工收件确认单独记录。

## 2026-10-23 群聊@硬性门禁验收

client_hook 19项回归、生命周期回归与MCP 23项回归通过。新版加载后，测试群普通消息、手输@昵称的结构化名单均为空，被MCP过滤，无webhook和回复；真正通过微信菜单@当前账号的消息名单匹配，首次事件投递成功，Dot自动回复，原生提交1次。用户明确确认“已收到，前两条也没有回复”，端到端验收通过。原始delivery_confirmed仍为false，人工确认单独保留。持续订阅保持，不再要求测试标记。证据：`state/tunnel/group-mentions-test.json`。
