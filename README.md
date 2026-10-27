# webot

面向 Windows 微信 **4.1.9.30 x64** 的本地消息接口与 Codex 助手。项目实现了从微信原生消息采集、持久消费、授权资源读取，到模型处理、工具执行和原会话文字回复的完整链路，也提供可单独使用的 HTTP、Python SDK、CLI 和 MCP 接口。

适用于在本机为指定私聊、群聊接入自动助手，或将微信消息接入自己的 Agent 和业务程序。

## 项目实现了什么

| 模块 | 实现内容 | 使用入口 |
| --- | --- | --- |
| 微信原生适配 | 校验固定版本与目标进程，使用驻留 DLL 采集消息，保留原生引用、群聊 @ 和消息标识；提供文字发送、资源获取与会话已读操作 | [client_hook](client_hook/README.md) |
| 持久消息接口 | 将已采集事件写入 SQLite，通过本地 HTTP、Python SDK 和 CLI 提供长轮询、独立消费者、批次确认及断点续取；发送请求使用稳定 ID 查询状态 | [消息接口](client_hook/docs/API.md) |
| MCP 桥接与授权 | 提供 stdio、HTTP MCP、事件订阅和签名 webhook；按账号、会话白名单及真实群聊 @ 控制处理范围，保存回复意图并防止重复提交 | [wechat_mcp](wechat_mcp/README.md) |
| Codex 自动回复 | 监听允许会话的新请求，选择同会话背景，为每个问题组织独立执行线程，调用本机 Codex 的模型与工具，将执行进度和最终答案分段回复 | [wechat_codex](wechat_codex/README.md) |
| 提醒与定时任务 | 在原会话创建固定提醒或定时 Codex 任务，支持一次性、固定间隔、每天和每周，提供查看、修改、暂停、恢复和取消操作 | [定时任务](wechat_codex/docs/SCHEDULES.md) |
| 内容读取工具 | 读取图片、音视频、文本、PDF、DOCX、XLSX、网页和 B 站播放器字幕；提供画面采样、本地 CPU 语音转写及读取范围说明 | [tool](tool/README.md) |
| 本地服务管理 | 按依赖顺序后台启动、检查和正常停止 Hook、MCP 与 Codex，校验配置、凭据、进程身份及服务健康状态 | [服务管理](scripts/wechat_stack/README.md) |

## 在微信里如何使用

- **文字问答与工具任务**：向白名单私聊发送问题，或在允许的群聊中真实 @ 当前账号；助手结合背景回答，需要时调用搜索、文件、终端或其他可用工具。
- **处理引用内容**：使用微信原生引用选中图片、语音、视频、文件或卡片，并说明处理要求；服务精确获取该资源，再交给内容工具和模型读取、分析。
- **读取链接**：请求概括网页或视频内容，按来源使用普通网页读取、专用浏览器或可获取的播放器字幕。
- **定时执行**：在原会话设置固定文字提醒，或让 Codex 按计划执行任务；任务与发送状态持久保存，后续可查看和管理。

非文字消息本身仅保存为事件和背景，不自动开始内容分析。资源获取、内容提取和模型理解是不同步骤，具体支持范围见各模块说明。

## 运行链路

```text
微信客户端 ↔ client_hook ↔ wechat_mcp ↔ wechat_codex ↔ Codex 模型与工具
wechat_codex → tool/（内容读取与预处理）
```

Hook 负责固定版本原生适配和本地 HTTP 接口；MCP 通过 HTTP 连接 Hook，负责事件与资源授权；Codex 业务通过 MCP 读取微信消息、获取资源并回复。`tool/` 提供独立的通用内容处理能力，也可由其他程序直接调用。

消息事件、消费位置、任务状态和回复意图持久保存。发生执行或发送结果未知时，服务保留原操作供核查，不以新请求自动重跑。

## 环境要求

- Windows、已登录的微信 **4.1.9.30 x64**，以及 64 位 Python。
- 构建原生模块需要可用的 GCC；每次构建使用独立输出目录。
- 自动回复需要可用的本机 Codex 登录、CLI/SDK 环境，以及一致的账号、会话白名单和凭据配置。
- 媒体、文档、浏览器和语音识别依赖按需安装；语音转写使用预先安装的本地模型，浏览器默认使用本机 Edge。

微信采集和服务运行在本机；Codex 模型推理可能访问云端。后台处理需要保持电脑唤醒、微信登录和服务可用。

## 开始使用

以下命令均在项目根目录执行。

1. 复制 [.env.example](.env.example) 为 `.env`，按[环境配置](docs/ENVIRONMENT.md)设置本机程序、服务地址及状态目录。
2. 按 [Codex 助手说明](wechat_codex/README.md#安装依赖)准备 Python 环境与所需依赖，按 [Hook 说明](client_hook/README.md#构建与启动)构建原生模块。
3. 按 [MCP 桥接说明](wechat_mcp/README.md)和 [Codex 配置说明](wechat_codex/README.md#配置)设置账号、允许会话与凭据。使用整套服务管理器时，需准备对应状态目录的配置及密文凭据，详见[服务管理](scripts/wechat_stack/README.md#使用前准备)。
4. 打开并登录匹配版本微信，启动服务：

```powershell
.\start_wechat.cmd --no-pause
```

查看状态和正常停止：

```powershell
.\status_wechat.cmd --no-pause
.\stop_wechat.cmd --no-pause
```

也可直接双击这三个入口。构建产物位于 `client_hook/build/runtime-*/`，`client_hook/build/runtime-current.json` 指向所选构建；路径与部署配置以 `.env` 及对应模块配置为准。微信已加载的 DLL 需通过正常退出并重启微信更换，不能覆盖或删除仍在使用的 DLL。

只想体验消息接口和 MCP 消费流程时，可以运行模拟示例，无需登录微信或调用模型：

```powershell
python -B -X utf8 .\wechat_mcp\bridge.py demo
```

## 使用范围与限制

- 原生适配限定微信 **4.1.9.30 x64**，其他版本不能直接套用地址和结构。
- 支持文字、图片、表情、语音、视频、文件和卡片事件采集；微信出站仅支持文字，Codex 回复每段最多 1200 字符、同题最多 64 段。
- 群聊必须携带可信的、指向当前账号的原生 @ 元数据；普通 @ 文本或仅 @ 所有人不能替代。
- 引用与附件授权限定同账号、同会话的精确目标；资源获取结果不代表已经读取或理解内容。
- 网页、媒体和字幕读取受来源可访问性、资源状态、采样及转写上限影响。未取得的正文或字幕不能作为已读内容。
- 消费确认与微信已读独立；发送状态 `accepted` 只表示微信客户端本地接受，不提供服务器确认或对端送达回执。
- 服务停止期间及启动前的历史消息不自动补取；队列保留与容量规则见各模块说明。

## 开发与检查

模块入口文档放在各模块根目录，`docs/` 保存环境、接口和实现专题说明。开发约定见 [AGENTS.md](AGENTS.md)。常用本地回归命令：

```powershell
python -B -X utf8 .\client_hook\run_tests.py --with-lifecycle
python -B -X utf8 .\wechat_mcp\run_tests.py
python -B -X utf8 .\wechat_codex\run_tests.py
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 -m unittest discover -s .\tool\tests -v
python -B -X utf8 -m unittest discover -s .\scripts\wechat_stack -p test_manage.py
```

本地模拟测试、真实模型调用和真实微信收发分别验证；普通回归不代表对端收件已确认。`.env`、凭据、`state/`、`build/`、聊天数据库、缓存和浏览器配置属于本机数据，不作为源码提交。
