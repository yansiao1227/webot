# 微信本地消息接口

`client_hook` 为 Windows 微信 **4.1.9.30 x64** 提供本地 HTTP 服务、Python SDK 和 CLI。它负责进程内消息采集、文字发送、附件资源获取及消息持久化，供 MCP 桥接或其他本机程序使用。

## 提供什么

- 采集文字及图片、表情、语音、视频、文件、卡片事件，保留原生引用和群聊 @ 元数据。
- 通过 SQLite 保存已采集消息，支持长轮询、独立消费者、批次确认和断点续取。
- 发送私聊、群聊文字，使用请求 ID 查询发送状态并避免重复提交。
- 提供资源任务、分块读取和原生会话已读接口。

消息消费确认与微信已读是独立操作。`accepted` 仅表示客户端本地接受发送请求；服务不提供对端送达回执。服务停止期间的消息及启动前的历史消息不自动补取，非文字发送不在接口范围内。

## 目录与入口

| 路径 | 职责 |
| --- | --- |
| `client_hook.py` | HTTP 服务与客户端命令的薄入口，实现在 `api/cli.py` |
| `api/`、`client/` | HTTP 服务、持久队列、资源接口及 Python SDK |
| `hook_runtime/` | 固定版本目标校验、DLL 控制与原生 RPC |
| `scripts/` | 原生构建、独立接收和运行诊断命令 |
| `native/`、`profiles/`、`vendor/` | 原生源码、版本适配资料和固定依赖 |
| `analysis/` | 离线入口分析与显式运行的观测工具 |
| `tests/` | 本地回归；`lifecycle/` 为自有 host 生命周期测试 |
| `validation/` | 真实客户端验证脚本及验证辅助代码，需显式运行 |
| `docs/` | 接口、版本适配和原生实现专题文档 |
| `build/`、`state/` | 本机构建产物和运行数据，不纳入源码 |

## 构建与启动

以下命令在项目根目录执行。需要已登录的匹配版本微信、64 位 Python 和可用的 GCC；编译器可在根目录 `.env` 中设置 `RE_WECHAT_GCC`，也可用 `-Compiler` 指定。

```powershell
.\client_hook\scripts\build_runtime.ps1
python -B -X utf8 .\client_hook\client_hook.py serve
```

服务默认地址为 `http://127.0.0.1:30001`，独立启动默认状态目录为 `client_hook/state/`。使用 `serve --state-dir <目录>` 指定持久数据位置，多个微信进程时使用 `serve --pid <PID>` 选择目标。地址配置见[环境配置](../docs/ENVIRONMENT.md)。

设置 `CLIENT_HOOK_TOKEN` 后，请求需携带 `Authorization: Bearer <token>`。用 Ctrl+C 正常停止前台服务；整套服务的日常启停见[管理说明](../scripts/wechat_stack/README.md)。

构建产物写入 `client_hook/build/runtime-*/`，`client_hook/build/runtime-current.json` 指向所选构建。微信加载过 DLL 后，更换构建需先正常退出并重启微信；进程退出前不要覆盖或删除已加载目录。

## 消费消息

在另一终端执行：

```powershell
python -B -X utf8 .\client_hook\client_hook.py listen --consumer-id my-agent
```

`listen` 使用长轮询，有消息时立即输出 JSON，并在整批输出成功后确认。需要在业务处理成功后才确认的程序，应使用 Python SDK 或直接调用 HTTP 接口，按 `event_id` 做业务去重。

每个消费者独立保存位置；重启后沿用相同 `consumer_id` 即可续取。首次注册可选择 `earliest` 或 `latest`。消息默认保留 7 天，事件内容逻辑容量为 1 GiB；位置过期返回 `410 cursor_expired`，需要显式处理缺口并重置消费者。

## 文档与源码

| 入口 | 内容 |
| --- | --- |
| [API.md](docs/API.md) | HTTP 路由、SDK/CLI、消息消费、发送状态与资源接口 |
| [READ_STATE_41930.md](docs/READ_STATE_41930.md) | 原生会话已读接口 |
| [MENTIONS.md](docs/MENTIONS.md) | 群聊 @ 信息来源与字段 |
| [implementation.html](docs/implementation.html) | 消息监听与发送实现图解 |
| `client_hook/native/` | 原生 Hook、快照队列与运行时 |
| `client_hook/client/`、`client_hook/api/` | Python 客户端和 HTTP 服务 |
| `client_hook/profiles/` | 固定版本适配资料 |

模块构建不需要外部参考仓库或真实微信 DLL。MinHook 的依赖出处与许可证保留在 `client_hook/vendor/`。

## 本地检查

```powershell
python -B -X utf8 .\client_hook\run_tests.py --with-lifecycle
```

该命令用于本地回归；真实微信的加载、收发及资源操作需要单独验证。
