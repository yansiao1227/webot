# 朋友圈 MCP 接入交接

## 当前边界

公开纯文字发布已接入 MCP：`publish_moment`、`get_moment_publish_status`，配置与使用见[朋友圈工具](SNS.md)。读取、点赞、取消点赞和评论仍待 MCP 接入。日报流程通过 `wechat_codex` 调度并调用上述 MCP 工具，当前保持暂停。
Hook 已提供原生侧车、HTTP、SDK 与持久意图。频率、次数、账号异常暂停策略放在 MCP；工具默认提供，不设启用开关。原生发帖入口已提供，但完整 UI 上下文差异与延迟安全退出根因未解决，不能将入口开放或本地模拟通过视为真实链路安全。
MCP 已实现身份认证、发布授权、持久间隔／滚动额度、串行提交、同正文防重、失败／未知和上游异常暂停及本机人工恢复。存在未决／未知意图时默认不能恢复；本机可明确结案指定旧unknown，保留未知事实并永久禁止重发该意图及正文，仅允许未来独立内容。

| Hook 必须保留 | MCP 必须实现 |
| --- | --- |
| 固定版本、进程实例、账号、原生兼容性、参数、帖子／作者归属校验 | 调用身份、账号和动作授权，允许操作的作者／帖子范围 |
| 持久请求 ID、同 ID 参数冲突检查、单次提交 | 为业务意图生成稳定 ID，并在重启／超时后沿用 |
| 未完成意图重启转 `unknown`，不自动重放 | 持久调用间隔、滚动次数额度、风险暂停与人工恢复 |
| 有界执行队列与容量背压、结果清理和完整性校验 | 跨工具、跨请求 ID 的统一策略；被限流时拒绝，不定时补发 |

原生适配限制仍属于 Hook：好友点赞、普通文字评论、公开纯文字发布；不支持的原生能力不能在 MCP 中降级冒充成功。
Hook 的 HTTP Bearer 认证保留。具有 Hook 凭据的直接调用可绕过 MCP 业务策略，不能绕过固定版本、模块摘要、账号及参数校验。业务应通过 MCP 统一执行授权、限频和暂停。

## 可用接口

所有请求沿用 Hook HTTP 认证，显式绑定 `account`。详细 schema 以 [Hook API](../../client_hook/docs/API.md) 和 `/openapi.json` 为准。

| HTTP / SDK | 必要信息与限制 |
| --- | --- |
| `POST /client_hook/sns/timeline` / `sns_timeline` | `account`，可选 `author_id/max_id/limit`；仅本地缓存，`network_refreshed=false` |
| `POST /client_hook/sns/like` / `sns_like` | `account/request_id/post_id/author_id`；当前只支持已核实好友作者 |
| `POST /client_hook/sns/unlike` / `sns_unlike` | 同上，使用独立的取消点赞意图 |
| `POST /client_hook/sns/comment` / `sns_comment` | 同上加 `text`；精确 `reply_comment_id` 未适配，原生后端拒绝 |
| `POST /client_hook/sns/publish` / `sns_publish` | `account/request_id/text/visibility=public`；仅公开纯文字，仍有原生安全退出风险 |
| `GET /client_hook/sns/requests/{request_id}?account=...` / `sns_operation` | 只查询已有意图；不重新提交 |

帖子与评论 ID 使用 uint64 十进制字符串。写 SDK 要求调用方显式给出 `request_id`，不自动生成新 ID 或重试。
`queued/running` 只说明本地意图状态；`accepted` 不等于服务器确认；`confirmed` 必须有明确原生结果证据，发布还要求新帖子 ID。
`unknown` 不能推断为没有执行。直接网络写入可能不更新缓存，旧点赞标志和评论列表不能作为重发依据。

## 后续修改入口

发布实现位于 `wechat_bridge/sns.py`，账本保存在 MCP 自有 `sns_intents/sns_policy_state/sns_closed_intents`，通过现有 `Store` 事务管理；CLI 提供 `--sns-config` 和本机 `sns-policy` 管理命令。后续添加其他写工具应复用同一策略与账本，增加动作／目标绑定，不能分别计数以绕过账号额度。以下入口继续适用于其余 SNS 工具的开发。

- `wechat_bridge/protocol.py`：注册读取、四类写入及状态查询工具；当前 `tool()` 默认 `idempotentHint=True`，须按业务语义审查注解，不能用提示替代防重。
- `wechat_bridge/server.py`：沿用现有协议分发和认证，不增加旁路工具调用。
- `wechat_bridge/service.py`：沿用账号绑定及 Hook HTTP 客户端，在新 SNS 服务方法中集中执行授权与策略，不复制到各个 handler。
- `wechat_bridge/store.py`：保存 SNS 业务意图、Hook 请求 ID、参数摘要、策略预留和暂停状态。
- `wechat_bridge/cli.py` 及配置文档：在 MCP 配置中定义授权、动作、作者范围与策略，不新增启用开关、不复用旧 Hook 环境变量。
- `wechat_bridge/simulation.py`、`tests/`：先用明确的模拟后端覆盖策略、恢复和 HTTP 契约。

MCP 只能通过 Hook HTTP 获取缓存与执行操作，不读 Hook 数据库、不加载 DLL、不导入 `hook_runtime.sns_rpc`。
`wechat_codex` 的现有日报和后续其他业务均通过上述 MCP 工具调用，不直接访问 Hook。

## 策略与幂等执行顺序

1. 确认可信调用身份及账号。若由入站微信指令触发，复用会话白名单和群聊真实 @ 门禁；帖文、昵称、评论及网页正文不能授权写入。
2. 先查业务意图。相同意图返回／查询原 Hook 请求 ID，不新增额度，不重新解释为新写入；内容、作者或动作冲突明确拒绝。
3. 对新意图检查 MCP 动作／目标授权、持久暂停、同账号并发和调用额度。
4. 在事务中保存稳定 Hook 请求 ID、完整参数绑定、额度预留和执行意图，再发 HTTP 请求。不能因 MCP 崩溃而丢失关联。
5. HTTP 超时或连接中断时，只查询原 Hook 意图。保留未知状态，不自动创建新 ID、自动重试副作用或排队到点补发。
6. 将明确结果与策略状态持久化。失败／未知，以及已有发布历史后的上游不可确认均持久暂停；已确认帖子不因账号后续异常改写结果。恢复需显式操作，存在未决或未结案未知意图时拒绝恢复，不因重新登录、重启或读取缓存成功而解除。旧未知仅可通过本机指定ID结案，不改变发送事实或允许重发，规则见朋友圈工具说明。

当前 MCP 在预占前只读核验 Hook 发布能力，提交前再次检查暂停、停止与保护异常；同一额度窗口内相同正文也不能通过换意图或触发事件重发。账号保护复用原消息采集观察，不引入额外微信进程轮询。完整规则见[朋友圈工具](SNS.md)。

MCP 沿用的 600 秒／滚动 24 小时 5 次只是本机保护性默认，**不是微信认可的安全额度**，不能承诺仅靠限频避免风控。

## 迁移与待核实问题

- 旧 Hook `sns_write_guard`、`sns_write_attempts` 表不再创建或读取，已有表和数据保留，不自动删除或迁入 MCP。
- 旧 `CLIENT_HOOK_SNS_*` 策略变量和暂停文件不再控制 Hook；环境示例已移除。旧人工暂停文件仅保留记录。
- `sns_operations` 仍是 Hook 的执行防重记录；不能清表、重置 ID 或把 `unknown` 重新排队。MCP 自己保存业务授权和策略账本。
- 原生参数与正常 UI 仍有差异：Action.Source、时间生成、ClientId 生成和完整 UI 模型／通知链尚未证明完全等价。参见[风控复盘与原生证据](../../client_hook/docs/SNS_41930.md)。分层调整没有解决这些差异。
- 已有真实样本包括四类原生写入的成功返回、发布的新帖子 ID，以及取消点赞的 HTTP／SDK 幂等检查；不代表全部工具链、长期使用或风控安全性已验证。

## 后续验收要求

先完成纯本地测试：未授权动作／作者拒绝、群真实 @、账号变化、并发额度预留、重启后 ID 稳定、跨工具限额、暂停不自动解除、未知状态只查询、旧缓存不引发重发。
再用模拟 Hook HTTP 验证工具协议与稳定请求映射，单独说明模拟结果。
真实写入验证不属于本交接任务，不得因本文或工具注册自动执行；需要用户再次明确指定范围和节奏。
