# webot

面向 Windows 微信 **4.1.9.30 x64** 的本地消息接口与 Codex 自动回复工具。它把微信消息转换为可持久消费的事件，通过 MCP 提供给 Agent，并将处理结果以文字回复到原会话。

适合在本机为指定私聊和群聊接入自动助手，也可以单独使用消息接口、MCP 桥接或内容读取工具。

## 主要功能

- 接收文字、图片、表情、语音、视频、文件和卡片事件，保存消息及消费者位置。
- 提供本地 HTTP、Python SDK/CLI 和 MCP 接口，支持长轮询、消费确认与幂等文字回复。
- 对允许的私聊自动回复；群聊要求消息包含可信的、指向当前账号的真实 @ 信息。
- 根据同会话背景处理问题，通过原生引用获取附件，调用内容工具读取媒体、文档、网页和 B 站字幕。
- 使用本机 Codex 登录和配置执行任务，支持按问题组织上下文及分段文字回复。

非文字消息本身不触发附件分析；需要符合会话授权、原生引用和明确处理要求。消息发送状态 `accepted` 表示微信客户端本地接受，不代表对端已收到。

## 快速开始

运行环境为 Windows、匹配版本的微信及 64 位 Python。使用自动回复还需要本机 Codex 登录、SDK 运行环境，以及已配置的账号、会话白名单和服务凭据。

1. 复制 [.env.example](.env.example) 为 `.env`，设置本机程序、服务地址和状态目录，详见[环境配置](docs/ENVIRONMENT.md)。
2. 按[消息接口说明](client_hook/docs/README.md)、[MCP 说明](wechat_mcp/docs/README.md)和[自动回复说明](wechat_codex/docs/README.md)准备构建产物、依赖与配置。
3. 打开并登录微信，双击根目录 `start_wechat.cmd`，等待各服务就绪。
4. 用 `status_wechat.cmd` 查看状态，用 `stop_wechat.cmd` 正常停止服务。

管理脚本的配置、日志位置及异常处理见[一键管理说明](scripts/wechat_stack/docs/README.md)。

## 目录说明

| 目录 | 用途 | 入口文档 |
| --- | --- | --- |
| `client_hook/` | 微信原生消息采集、文字发送、附件资源、本地 HTTP 与持久队列 | [消息接口](client_hook/docs/README.md) |
| `wechat_mcp/` | 将本地消息服务封装为 MCP 工具和事件，管理消费者与回复意图 | [MCP 桥接](wechat_mcp/docs/README.md) |
| `wechat_codex/` | 监听微信消息，调度 Codex 处理并回复 | [自动回复](wechat_codex/docs/README.md) |
| `tool/` | 可独立调用的媒体、文档、网页和字幕读取工具 | [内容工具](tool/README.md) |
| `scripts/wechat_stack/` | 整套服务的启动、停止与状态检查 | [服务管理](scripts/wechat_stack/docs/README.md) |
| `docs/` | 环境配置及项目维护文档 | [环境配置](docs/ENVIRONMENT.md) |

运行链路为 `client_hook → wechat_mcp → wechat_codex`。各层通过本地 HTTP 通信；`tool/` 提供通用内容处理能力。

## 构建与检查

以下命令在项目根目录执行。构建需要可用的 GCC，可通过 `.env` 中的 `RE_WECHAT_GCC` 或构建参数 `-Compiler` 指定。

```powershell
.\client_hook\scripts\build_runtime.ps1
python -B -X utf8 .\client_hook\run_tests.py --with-lifecycle
python -B -X utf8 .\wechat_mcp\run_tests.py
python -B -X utf8 .\wechat_codex\run_tests.py
python -B -X utf8 -m unittest discover -s .\scripts\wechat_stack -p test_manage.py
```

每次原生构建生成独立目录，并更新 `client_hook/build/runtime-current.json`。微信已加载的 DLL 需通过正常重启微信更换；日常启停使用现有产物即可。

开发与文档规范见 [AGENTS.md](AGENTS.md)。`state/`、`.env`、凭据、聊天数据库和浏览器配置属于本机运行数据，不应作为源码提交。
