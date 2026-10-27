# Codex 微信自动回复

`wechat_codex` 是连接微信 MCP 桥接的本地常驻助手。它监听允许会话的新消息，调用 Codex 处理问题或执行工具任务，再将结果以文字回复到原会话。

监听器负责消息队列、消费确认、资源传输和发送；Codex 负责理解与任务执行。模块通过本地 HTTP MCP 接入 `wechat_mcp`，不直接读取 Hook 数据库或调用微信 DLL。

## 能做什么

- 为白名单私聊提供自动回复；群聊仅处理真实 @ 当前账号、且具有可信元数据的消息。
- 按账号和会话隔离背景，按问题组织执行线程；同题的执行提示和最终答案可分段返回。
- 在合法原生引用和明确处理要求下读取附件，调用[通用内容工具](../tool/README.md)处理图片、表情、语音、视频、文档、网页及 B 站字幕。
- 持久保存任务、草稿、会话映射及发送结果，重启后恢复处理；执行或发送结果未知时暂停自动重试。
- 在原会话创建固定提醒或定时 Codex 任务，支持一次性、固定间隔、每天和每周；可通过微信或本地命令管理，见[定时任务](docs/SCHEDULES.md)。

非文字事件只保存背景与元数据，不自动开始内容分析。微信出站仅发送文字，每段最多 1200 字符，同题最多 64 段。`accepted` 表示本地接受，不能用于判断对端收件。

## 目录与入口

| 路径 | 职责 |
| --- | --- |
| `agent.py` | 服务命令入口，支持配置、启停、状态和会话重置 |
| `local_agent/` | 命令行实现、任务调度、MCP 接入、上下文、存储及微信内容适配 |
| `scripts/` | 手动运维和微信侧工具入口 |
| `tests/`、`run_tests.py` | 本地模拟回归及执行入口 |
| `validation/` | 需显式运行的真实 SDK、浏览器或 MCP 验证 |
| `docs/` | 上下文、定时任务、内容处理与恢复专题文档 |
| `state/` | 本机配置、运行数据、虚拟环境和验证报告，不纳入源码 |

通用内容处理及语音模型安装由仓库根目录的 [tool/](../tool/README.md) 提供；本模块的媒体、浏览器依赖清单转引对应通用依赖。

## 安装依赖

以下命令在项目根目录执行。需要已配置的 `client_hook` 和 `wechat_mcp`，以及可用的本机 Codex 登录。

```powershell
python -m venv .\wechat_codex\state\sdk-env
.\wechat_codex\state\sdk-env\Scripts\python.exe -m pip install -r .\wechat_codex\requirements.txt
.\wechat_codex\state\sdk-env\Scripts\python.exe -m pip install -r .\wechat_codex\requirements-media.txt
.\wechat_codex\state\sdk-env\Scripts\python.exe -m pip install -r .\wechat_codex\requirements-browser.txt
.\wechat_codex\state\sdk-env\Scripts\python.exe -m pip install -r .\client_hook\requirements-resources.txt
.\wechat_codex\state\sdk-env\Scripts\python.exe .\client_hook\scripts\build_codecs.py
.\wechat_codex\state\sdk-env\Scripts\python.exe .\tool\install_transcription_model.py
.\wechat_codex\state\sdk-env\Scripts\python.exe .\wechat_codex\agent.py check-dependencies
```

SDK 版本由 `requirements.txt` 固定。语音识别使用本机 CPU 和预先安装的模型，处理消息时不下载权重；编解码器构建可通过 `RE_WECHAT_GCC` 或 `--compiler` 指定编译器。浏览器读取默认使用本机 Edge，可通过 `CONTENT_BROWSER_CHANNEL` 和 `CONTENT_BROWSER_PROFILE_DIR` 配置。

## 配置

先通过 Codex 完成本机登录，再配置账号、允许会话及 MCP 凭据：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py configure `
  --account wxid_当前账号 --conversation wxid_允许私聊 `
  --conversation '数字群ID@chatroom'
```

配置过程使用不回显输入保存 MCP token，Windows 下以当前用户 DPAPI 密文持久化。账号和白名单须与 MCP 配置一致。

默认 MCP 地址为 `http://127.0.0.1:30002/mcp`，可用 `--mcp-url` 指定。省略 `--model` 时沿用本机默认模型和推理强度；`RE_WECHAT_CODEX_BIN` 可指定 CLI 路径。路径与地址设置见[环境配置](../docs/ENVIRONMENT.md)。

适配器通过官方 Python SDK 使用 Codex 登录与配置，继承运行时支持的工具、技能、MCP 和插件，并启用实时搜索。模型推理可能访问云端，工具调用可能产生实际操作；桌面应用专属工具是否可用取决于 CLI/SDK 运行时。

## 启动、查看和停止

整套服务使用根目录 `start_wechat.cmd`、`status_wechat.cmd`、`stop_wechat.cmd`，详见[管理说明](../scripts/wechat_stack/README.md)。单独管理本模块：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py start
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py status
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py stop
```

`start` 隐藏后台运行，前台模式使用 `agent.py run`。运行时需保持电脑唤醒、微信登录和上游服务可用。停止后不接收新任务，已开始的模型轮次可完成并保存；尚未发送的结果等待显式重启。

状态与任务保存在 `wechat_codex/state/live/`，其中 `agent.sqlite3` 包含会话、任务及回复资料。`status` 的 `heartbeat_fresh` 表示最近 5 秒内有心跳。检测到活动事件订阅时拒绝发送；同一收发链路应只启用一个自动回复 Agent。

## 上下文与内容处理

默认启用按需上下文和处理时原生已读。`startup_timeout=60` 单独限制模型启动准备，准备完成后由 `decision_timeout=30` 限制等待下一决定或首次输出的时间。正文流式输出期间按 `generation_idle_timeout=30` 判断停滞，`tool_timeout=180` 单独限制工具执行，`turn_timeout=600` 限制单轮总时长；配置方法见[计时与分段规则](docs/ADAPTIVE_REPLIES.md)。正文持续生成达到30秒时会提示一次“内容过长，正在输出，请稍等……”，同一请求续写不重复提示。这些值不是对端收件时限。

视频使用采样画面与有限音轨转写；文档覆盖文本、PDF、DOCX、XLSX。网页或字幕读取失败时应如实说明，不能把卡片字段当作完整来源内容。

详细行为见[上下文与分段回复](docs/ADAPTIVE_REPLIES.md)、[会话与恢复](docs/SESSIONS.md)、[内容分析与浏览器配置](docs/CONTENT_ANALYSIS.md)。

## 本地检查

```powershell
python -B -X utf8 -W error::ResourceWarning .\wechat_codex\run_tests.py
```

需要单独检查 SDK 或联网工具时，可运行 `validation/sdk_validation.py` 或 `validation/capabilities_validation.py`；这些命令会调用真实 Codex 服务，不连接微信收发接口。


手动验证脚本集中在 `wechat_codex/validation/`，与 `tests/` 中的本地回归分开；验证输出保存在 `wechat_codex/state/`。以下命令在项目根目录执行：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\validation\sdk_validation.py
```

`validate_*.py` 也从该目录运行；其中 `validate_card_source.py <event_id>` 和 `validate_timeout_wechat.py` 会访问真实 MCP，仅在已授权的验证场景中使用。

## 手动运维与网页入口

以下命令在项目根目录执行：

```powershell
# 打开服务使用的专用浏览器 profile，手动登录站点
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\scripts\open_browser_profile.py
# 读取一个公网链接；动态页面可加 --browser
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\scripts\read_web.py 'https://example.com/'
# 压缩一个已授权且空闲的现有会话线程；需要真实 Codex 服务
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\scripts\compact_thread.py --conversation wxid_允许私聊
```

三个脚本均支持 `--state-dir`；浏览器和网页入口默认沿用 `RE_WECHAT_CODEX_STATE_DIR`，会话压缩默认使用本模块的 `state/live/`。压缩要求取得本地状态锁，应在停止服务后执行。语音模型安装统一使用上文的 `tool/install_transcription_model.py`。
