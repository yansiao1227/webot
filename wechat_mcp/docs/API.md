# MCP 工具与事件

## 传输与身份

同一实现支持 stdio 以及 `/mcp` Streamable HTTP JSON 响应。支持 `2026-07-28` 的 `server/discover`，以及 `2025-11-25` / `2025-06-18` / `2024-11-05` initialize 兼容。HTTP 无会话 ID，GET /mcp 和 DELETE /mcp 返回 405。单个 HTTP 服务最多32个请求，其中消息拉取最多16个；超额连接拒绝或工具返回错误。

本期是单所有者、单微信账号模型。MCP bearer 持有者可操作配置允许的会话；消费者 ID 用于进度隔离，不是不同用户的授权边界。不宣称支持多租户 OAuth。stdio 由宿主进程权限控制。替换 agent 无需改变微信接口。

## 工具

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `bridge_status` | `{}` | 账号、允许会话、模拟标记、采集及投递/回复状态计数 |
| `get_messages` | `consumer_id`，可选 timeout=0–25、limit=1–100、start=earliest/latest | 未确认文本批次；未 ack 重复返回原事件与 token |
| `ack_messages` | consumer_id、ack_token | 成功处理整批后推进此消费者进度，重复确认幂等 |
| `get_message` | event_id | 读取允许范围内的已采集事件 |
| `reply_text` | event_id、text | 只向原会话回复，一条事件一个持久回复意图 |
| `get_reply_status` | event_id | 查询原发送操作，不重新提交 |

工具返回 MCP `content`、`structuredContent` 和 `isError`。失败结果不伪造成功；发送副作用在 metadata 中明确标注。`consumer_id` 仅允许 1–63 个 ASCII 字母、数字、下划线或连字符。同一消费者用于一个串行消费循环，不应让不同 agent 共用它。

`reply_text` 不接受 toid。相同事件和正文复用原请求，不同正文报冲突。调用前验证当前微信账号。正文为非空、无 NUL 的 UTF-8，最大65536字节。账号、事件和固定用途生成 `agent_` 请求 ID。发送状态 prepared/submitting 用于桥接恢复，queued/running/accepted/failed/unknown 对齐上游。请求提交附近断线仅查询原 ID；查不到已提交操作则保留 unknown，不自动重发。accepted 不等于送达。

消费采用至少一次语义；处理成功后再 ack。外部业务需按 event_id 幂等。不同消费者会各自收到相同事件，但回复意图仍全局按事件唯一，以避免切换 agent 后重复回复。

## 定时任务授权与投递

这些工具供外部调度宿主使用，MCP 本身不计算计划或调用模型：

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `authorize_schedule` | `event_id, schedule_id` | 从真实入站文本核验账号、白名单、创建者及群原生 @，保存长期原会话证据 |
| `get_schedule_authorization` | `schedule_id` | 执行前核验授权和当前微信账号，返回原会话及创建者 |
| `revoke_schedule` | `schedule_id` | 永久撤销该任务后续发送；不能用相同ID重新授权 |
| `send_scheduled_text` | `schedule_id, run_id, part_index, text, final` | 向授权原会话投递一个持久幂等段 |
| `get_scheduled_text_status` | `schedule_id, run_id, part_index` | 查询原发送操作，不重新发送；撤销后仍可核查已提交操作 |

任务与执行标识为1至128位ASCII字母、数字、下划线或连字符。分段从0连续递增，每次执行最多64段，每段1至1200字符；前段必须已被本地接受且非结束段。相同任务、执行、段序号只对应一个正文和结束标记，禁止覆盖、跳段或在结束后追加。不同执行必须使用稳定且不同的 `run_id`。

授权证据独立保存，不受普通消息七天清理影响。每次发送重新检查授权、原会话白名单和当前账号，不能通过参数指定其他收件人。任务授权ID不随暂停或修改时间更换；取消后不可复活。

`state=accepted` 仅表示客户端本地接受，`delivery_confirmed` 始终为false。发送响应丢失后查询原请求；`unknown` 不产生新请求，也不能跳到下一段。查询 `prepared` 不触发发送。宿主应保留执行及发送状态，不能把状态未知当成失败重跑整个任务。

## 可选 MCP Events

事件名 `wechat.message.received`，`arguments={"conversation_ids":["wxid_xxx"]}`。订阅会话必须属于本机配置允许列表。支持 events/list、events/subscribe、events/unsubscribe；与工具共用认证端点。默认订阅24小时、可请求1秒至7天；ttlMs=null 也仅授予24小时，以返回的 refreshBefore 为准。

只订阅之后进入桥接 inbox 的事件。取消或过期后重建不补放历史；cursor 固定 null，不宣称事件协议重放。持续有效订阅续期保留未投递积压。普通工具仍可读取已持久化消息。

订阅时验证签名 challenge 后才激活。事件载荷：

```json
{
  "eventId":"上游event_id",
  "name":"wechat.message.received",
  "timestamp":"ISO8601本地采集时间",
  "data":{
    "event_id":"上游event_id",
    "account":"wxid_self",
    "conversation_id":"wxid_peer",
    "text":"正文",
    "message_type":"text",
    "source":"live"
  },
  "cursor":null
}
```

timestamp 是本地采集时间，不是微信服务端时间。群会话正文保留上游原始成员前缀，不伪造已验证的成员身份。

投递采用 Standard Webhooks HMAC，重试保持 eventId，签名时间更新。HTTP 2xx 只标记 delivered，agent 可能异步处理；临时故障指数退避至多8次，400/401/403/404/410/413 或永久地址错误不重试。密钥轮换保留旧签名至多5分钟，普通续期不延长旧密钥窗口。取消订阅后停止新投递；已经在网络中的单次请求仍可能完成。

真实回调必须公网 HTTPS，每次连接重新验证全部 DNS 结果并固定连接已校验 IP，TLS 校验原域名，拒绝重定向、凭据 URL 和私网地址。离线 --simulate 仅允许回环回调。

这些约束依据 [OpenAI MCP Events 文档](https://developers.openai.com/plugins/build/mcp-events) 实现。事件协议属于可选扩展；不支持它的 agent 用 get_messages / ack_messages。

## 群聊硬性限制（2026-10-23）

群消息必须由可信结构化 `mentions.wxids` 明确包含当前账号，才会进入新事件投递并允许 `reply_text`。缺失/unknown、其他账号或仅@所有人拒绝；正文不作为依据。`bridge_status.group_reply_policy` 返回门禁配置，事件data增加可为空的mentions。见 [GROUP_MENTIONS.md](GROUP_MENTIONS.md)。

get_reply_status新增send_echo：以发送对象标识、同账号、原触发会话精确连接出站事件；返回identity_unavailable/waiting/matched/ambiguous及message_id、echo_event_ids。回显可先于发送结果到达，独立索引支持两种顺序，保留不可变事件。不同账号/会话或不同原生标识不会关联；多个服务器ID明确ambiguous。回显与accepted均不表示对端收件。旧事件缺少可信标识时不猜测。

普通文字新增 `weixin41930_exact_send_task_record110` 关联路径：上游发送结果必须含 `native_sort_seq_source=weixin41930_send_task_record110_v1`，出站事件必须含 `native_record_metadata_source=weixin41930_record_identity_v2`。仅在同账号、同会话内精确匹配原生排序序号，不比较正文或时间接近。client_hook提供的原生数据库服务器ID映射与回显ID冲突时明确ambiguous；MCP不自行访问微信数据库。新增索引增量创建、回填已有保留事件，并随事件清理。

`mark_conversation_read(event_id)` 请求微信原生清除该指令会话的未读数；`get_conversation_read_status(event_id)` 只查询原操作。两者重新校验当前账号、白名单、合法入站文字及群真实 @，会话只能从原事件派生，不允许额外会话参数。只有 `confirmed` 才表示观测到本机原生未读由正数清零，`already_read` 表示调用前已为零，`requested/unknown` 不冒充完成。MCP不读微信库。`ack_messages`仍只确认消费。

分段回复提供`reply_text_part(event_id,part_index,text,final)`和`get_reply_part_status(event_id,part_index)`。2026-10-26为执行进度＋最终答案将上限扩展为64段，索引0至63连续递增，每段1至1200字；上一段accepted且未结束才能创建下一段，第64段必须结束。`event_id+part_index`唯一意图，正文/结束标记不可改写，未知段不能越过。同一事件不可混用旧reply_text。每段发送前核验原事件门禁和当前账号，查询及回显限定原事件会话。get_reply_status兼容查询分段事件的最新段，但不会提交下一段。reply_parts表增量迁移，不覆盖旧replies或消费进度。
