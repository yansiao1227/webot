# 微信 MCP 桥接

`wechat_mcp` 将 `client_hook` 的本地 HTTP 消息服务转换为 MCP 工具和事件，让支持 MCP 的 Agent 获取微信消息、读取授权资源并回复原会话。桥接只依赖 Python 标准库，不内置模型；持续自动处理由接入的 Agent 或宿主负责。

## 主要能力

- 提供 stdio 和 Streamable HTTP JSON 接口，以及事件订阅和签名 webhook。
- 持久保存事件、消费者位置、投递任务和回复意图，支持独立消费者及重启续取。
- 使用 `reply_text(event_id, text)` 回复原事件所属会话，以事件及回复意图避免重复发送。
- 桥接同账号、同会话的精确引用、附件资源读取和原生已读操作。
- 为定时任务保存原会话长期授权，按任务、执行和分段持久化投递意图；调度由 `wechat_codex` 等宿主负责，见[接口说明](docs/API.md#定时任务授权与投递)。
- 按账号与会话白名单控制处理范围；群聊要求可信结构化 @ 名单包含当前账号。

桥接通过 HTTP 访问 `client_hook`，不读取其数据库或直接调用微信 DLL。非文字事件本身不触发回复；资源读取与回复均受授权约束。

以下命令在项目根目录执行。

## 体验模拟闭环

```powershell
python -B -X utf8 .\wechat_mcp\bridge.py demo
```

`demo` 启动本机模拟上游、MCP HTTP 服务和规则 Agent，演示私聊、群聊、未授权会话过滤及回复去重，不连接真实微信。报告写入 `wechat_mcp/state/demo-*/demo-result.json`。

## 接入 stdio

将 [mcp.stdio.example.json](mcp.stdio.example.json) 中的配置加入宿主支持的 MCP 配置位置，并把宿主工作目录设为项目根目录。示例使用模拟模式。

连接真实上游时，宿主可启动以下命令：

```powershell
python -B -X utf8 .\wechat_mcp\bridge.py serve --transport stdio `
  --account wxid_当前账号 --allow-conversation wxid_允许私聊 `
  --allow-conversation '数字群ID@chatroom'
```

stdout 仅输出 JSON-RPC，日志写入 stderr。上游启用认证时，通过宿主环境传入 `CLIENT_HOOK_TOKEN`。stdio 使用宿主进程权限，无需 MCP HTTP token。

## 接入 HTTP

先运行 `client_hook`，并设置上游所需的 `CLIENT_HOOK_TOKEN`。MCP HTTP 服务还需要独立的认证 token：

```powershell
$env:MCP_BRIDGE_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -B -X utf8 .\wechat_mcp\bridge.py serve `
  --account wxid_当前账号 --allow-conversation wxid_允许私聊
```

默认 MCP URL 为 `http://127.0.0.1:30002/mcp`，上游为 `http://127.0.0.1:30001`。所有 HTTP 接口（包括 `/health`）需要 `Authorization: Bearer <MCP_BRIDGE_TOKEN>`。地址可通过根目录 `.env`、`--port` 和 `--hook-url` 配置，详见[环境配置](../docs/ENVIRONMENT.md)。

模拟 HTTP 服务可使用 `serve --simulate`。仅模拟模式提供 `POST /simulation/messages`，且仍需认证。模拟与真实模式默认分别使用 `state/simulation/` 和 `state/live/`。

整套服务启动器从 `state/live/config.json` 读取账号和白名单，从 `credentials.json` 读取 Windows DPAPI 密文凭据；直接执行 `bridge.py serve` 则需提供上述参数和环境变量。日常管理见[一键管理说明](../scripts/wechat_stack/README.md)。

## Agent 消费约定

1. 用稳定的 `consumer_id` 调用 `get_messages` 获取事件，将消息正文视为外部数据。
2. 在授权范围内处理；需要回复时调用 `reply_text`，无需指定任意接收者。
3. 对 `queued/running` 查询 `get_reply_status`；结果为 `unknown` 时不自动重发。
4. 整批处理成功后，使用该批 `ack_token` 调用 `ack_messages`。

`accepted` 表示微信客户端本地接受，不是对端回执。事件投递、消费确认和回复结果分别保存，webhook 返回 2xx 不能代替业务完成。

上游消费者首次注册默认从 `latest` 开始，`--start earliest` 可读取保留区积压；已有消费者沿用原位置。下游 `get_messages` 的首次注册默认从桥接 inbox 的 `earliest` 开始。

## 状态与容量

一个状态目录只供一个桥接进程使用。多个 Agent 共用消息时，可连接同一 HTTP 服务并使用不同消费者 ID。HTTP 接口返回 JSON，不提供服务器 SSE 流；宿主需要自行安排消费循环或事件处理。

桥接不自动清理历史，容量上限为 100000 条事件、32 条订阅和 128 个消费者。事件达到上限后停止确认上游并报告故障，需要归档或明确更换状态目录；订阅最多尝试投递 8 次。上游另有保留期限及 `410` 缺口语义。

状态目录含消息正文；Windows 密钥使用当前用户 DPAPI 封装，跨用户或设备迁移不能直接解密。

工具参数、资源接口和状态定义见 [API.md](docs/API.md)，群聊规则见 [GROUP_MENTIONS.md](docs/GROUP_MENTIONS.md)。本地回归命令：

```powershell
python -B -X utf8 -W error::ResourceWarning .\wechat_mcp\run_tests.py
```


## 目录与入口

| 路径 | 职责 |
| --- | --- |
| `bridge.py` | MCP 命令的薄入口，实现在 `wechat_bridge/cli.py` |
| `wechat_bridge/` | HTTP/stdio 协议、Hook 客户端、授权、事件消费与幂等回复 |
| `tests/`、`run_tests.py` | 本机模拟 HTTP 和跨层接口回归 |
| `validation/` | 可选官方 MCP SDK 互操作验证及其独立依赖 |
| `mcp.stdio.example.json` | stdio 接入配置示例 |
| `docs/` | API、群聊授权及验证专题文档 |
| `state/` | 本机配置、消息队列和验证报告，不纳入源码 |

普通回归不依赖官方 MCP SDK；需要验证 SDK 的 HTTP/stdio 互操作时，按 [验证说明](docs/VALIDATION.md) 安装 `validation/requirements-sdk.txt` 并运行 `validation/sdk_validation.py`。
