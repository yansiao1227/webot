# 公开文字朋友圈

MCP 仅通过 Hook HTTP 为当前绑定账号发布公开纯文字朋友圈。图片、视频、其他可见范围，以及读取、点赞和评论工具未接入 MCP。

当前提供 4.1.9.30 原生公开文字发布，实际能力以 Hook 的 `capabilities.sns.publish` 为准。完整 UI 上下文差异仍未解决，存在发帖成功后延迟安全退出的已知风险；入口开放不代表根因已修复，`confirmed` 不代表账号健康。详见[原生限制](../../client_hook/docs/SNS_41930.md)。

## 默认能力与授权

公开文字发布默认提供，不需要启用开关。需要调整授权或限频时，可将[策略示例](../sns.example.json)复制到 MCP 状态目录的 `sns-policy.json`，由本机操作者修改并正常重启 MCP；也可在 `serve` 后用 `--sns-config <文件>` 指定策略文件。管理器启动的 MCP 同样读取其状态目录的该文件。不修改 Hook 配置，也不需要重载 DLL。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `allow_direct` | `true` | 授权可信 MCP 调用方直接发布当前账号的公开文字帖 |
| `event_conversations` | `[]` | 可触发发布的入站会话，必须是 MCP 会话白名单的子集 |
| `min_interval_seconds` | `600` | 两个新写入意图之间的最短间隔 |
| `window_seconds` | `86400` | 滚动额度窗口 |
| `max_writes` | `5` | 窗口内新意图最多次数，失败与未知也占额度 |

以上数字仅为本机保护性默认值，不能保证避免风控，也不是微信认可的安全额度。数值字段必须为1至31536000的整数。未知配置字段会报错；旧版 `enabled` 字段已移除，已有策略文件应删除该字段。

HTTP 使用现有 MCP bearer，stdio 使用宿主权限；当前为单所有者模型。可信 MCP 调用方默认可直接发布当前绑定账号的公开文字帖；`allow_direct=false` 可撤销直接调用授权。若由微信入站指令触发，必须传入真实 `event_id`，该事件须为当前账号、白名单内入站文字，且所在会话列在 `event_conversations` 中；群聊还必须有可信原生 @ 当前账号。授权独立于帖文和昵称，不能从外部内容取得。`allow_direct` 是调用方授权，不能用来替代 MCP 身份认证；不希望调用方有直接权限时可设为 false。

配置中的会话发布授权不代替用户的发帖意图，调用方仍须确认任务确实要求发布，不能将读取到的任意消息解释为发布命令。

## 工具

`publish_moment(intent_id, text, event_id?)` 创建一次业务意图。`intent_id` 使用1至128位 ASCII 字母、数字、下划线或连字符，由调用方持久保存。文本非空，最大64KiB UTF-8，禁止非法XML字符。可见范围固定为 `public`，工具不接受账号、Hook 请求 ID、媒体或其他可见范围参数。

可信 MCP 调用示例：

```json
{"name":"publish_moment","arguments":{"intent_id":"publish-task-001","text":"准备发布的公开文字"}}
```

微信事件触发时加上 `"event_id":"<原入站事件ID>"`。相同业务 ID 必须沿用完全相同的文本和触发事件；冲突明确报错。重复调用只返回或查询原 Hook 操作，不重新 POST，也不新增额度。新任务才可使用新 ID，不能通过换 ID 重试未知状态。

`get_moment_publish_status(intent_id)` 仅查询已有意图；发布授权撤销后仍可核查原操作。状态为 `queued/running/accepted/confirmed/failed/unknown`。返回包含 `request_id`、`local_accepted`、`server_confirmed` 以及原 `operation`。`accepted` 只表示客户端本地接受；`confirmed` 必须有 Hook 的服务器确认和非零 uint64 帖子 ID（`operation.result.post_id`），不能据此声称长期使用或风控安全已验证。

新意图先经只读 Hook 能力预检，能力缺失、不可用或预检失败时不创建意图、不提交发布请求。MCP 在事务中保存业务意图、Hook 请求 ID、参数和额度，再提交 HTTP；提交前再次检查停止、暂停和上游异常。已预占但确定未提交的操作记为 `failed`，`operation.result.submitted=false`。

相同账号串行发布；存在 `submitting/queued/running/accepted` 或未人工结案的 `unknown` 意图时拒绝新意图。同一滚动额度窗口内，相同正文不能通过更换意图 ID 或触发事件再次提交；人工结案正文永久禁止换 ID 重发。错误中提供原意图 ID，不会悄悄复用其他意图。网络错误或中断后只允许查询原请求，包含 Hook 返回404的情况。重启期间中断的提交转为未知并暂停；发现已有未决意图也会保留暂停。没有补发队列、定时重试或基于本地朋友圈缓存的重发。

## 暂停与人工恢复

`failed/unknown` 会持久暂停新发布。成功查询到原操作结果、重新登录及服务重启均不会自动恢复；`bridge_status.sns_policy` 可查看当前配置和暂停原因。暂停不阻止状态查询。

存在发布历史时，现有消息采集发现上游不可用、采集停止或账号核验失败，也会持久暂停朋友圈；已经 `confirmed` 的原帖保持确认，不篡改为失败。该异常只说明上游或账号不可确认，不能直接认定微信风控。保护写盘失败会保留进程内 `worker_errors.sns_protection` 门禁。普通消息消费继续使用原消费者恢复，不因朋友圈暂停停止服务；不新增微信进程探测或高频轮询。

以下命令在项目根目录执行，**先正常停止使用同一状态目录的 MCP**，命令利用同一目录锁阻止并发管理：

```powershell
python -B -X utf8 .\wechat_mcp\bridge.py sns-policy --state-dir <MCP状态目录> --account wxid_当前账号 status
python -B -X utf8 .\wechat_mcp\bridge.py sns-policy --state-dir <MCP状态目录> --account wxid_当前账号 pause
python -B -X utf8 .\wechat_mcp\bridge.py sns-policy --state-dir <MCP状态目录> --account wxid_当前账号 resume
```

模拟目录还须传 `--simulate`。普通 `resume` 在存在未决／未知意图时拒绝，需先查询原意图。若操作者明确结束某个旧未知操作、决定不再重发，但需要继续独立的新任务，可指定确切 ID：

```powershell
python -B -X utf8 .\wechat_mcp\bridge.py sns-policy --state-dir <MCP状态目录> --account wxid_当前账号 resume --close-unknown <旧意图ID>
```

结案单独绑定原意图、请求 ID、参数摘要及时间，不修改旧 `unknown` 或伪造确认；状态查询的 `closed_no_retry=true` 只表示人工结案并禁止重发，不是发送结果。旧意图仍只查询，同正文禁止用新 ID 重发。其他未结案未知或进行中意图仍阻止恢复，整个结案／恢复事务会回滚。该参数仅限本机 CLI 的显式 `resume`，不支持全部自动结案，不提供 MCP 工具。`bridge_status.sns_policy.closed_unknown_count` 显示已结案的未知意图数。

结案后合法旧查询仍未知不会重新触发暂停；返回进行中、身份不符、畸形响应或账号不可确认时仍暂停，新意图的失败／未知也照常暂停。恢复不重置发布授权、额度、消费者或请求 ID，不恢复日报调度，也不提交帖子。不要删除状态数据库来规避限制。

Hook 的幂等与校验仍保留。持有 Hook 凭据的直接调用可绕过 MCP 策略，参见[接入交接](SNS_HANDOFF.md)和[原生实现限制](../../client_hook/docs/SNS_41930.md)。本功能的模拟和本地回归不会发布真实朋友圈；真实写入测试需要另行明确授权。
