# 微信收发栈管理

这一组脚本用于统一启动、停止和检查 `client_hook`、`wechat_mcp`、`wechat_codex`，让本地微信自动回复服务在后台运行。

## 日常使用

先打开并登录微信，再双击项目根目录的管理入口：

| 文件 | 用途 |
| --- | --- |
| `start_wechat.cmd` | 按 client_hook → MCP → Codex 的顺序启动并检查就绪 |
| `status_wechat.cmd` | 查询进程、认证健康检查和就绪状态 |
| `stop_wechat.cmd` | 按 Codex → MCP → client_hook 的顺序正常停止 |

启动窗口显示全部就绪后即可按提示关闭。命令行可加 `--no-pause`，例如 `.\start_wechat.cmd --no-pause`；退出码非 0 表示操作未完成。

也可在项目根目录直接运行：

```powershell
python -B -X utf8 .\scripts\wechat_stack\manage.py start
python -B -X utf8 .\scripts\wechat_stack\manage.py status
python -B -X utf8 .\scripts\wechat_stack\manage.py stop
```

## 使用前准备

需要 PATH 中可用的 Python、已安装的 Codex SDK 环境、Hook 构建产物和各模块配置。微信与管理脚本应由配置凭据的同一 Windows 用户运行。

根目录 `.env` 设置监听地址、端口、Python、Codex 和状态目录。模板见 [.env.example](../../.env.example)，变量说明见[环境配置](../../docs/ENVIRONMENT.md)。修改端口或状态目录前，先使用原配置停止服务。

| 项目 | 默认位置或地址 |
| --- | --- |
| Hook 状态 | `client_hook/state/mcp-live-validation/` |
| Hook HTTP | `http://127.0.0.1:30001` |
| MCP 状态 | `wechat_mcp/state/live/` |
| MCP HTTP | `http://127.0.0.1:30002/mcp` |
| Codex 状态 | `wechat_codex/state/live/` |
| Python | `wechat_codex/state/sdk-env/Scripts/python.exe` |
| 管理日志 | `scripts/wechat_stack/state/client_hook.log`、`mcp.log`、`codex.log` |

MCP 的 `config.json` 保存账号和白名单，`credentials.json` 保存 Hook/MCP 的 DPAPI 密文 token。Codex 配置位于自身状态目录；管理器检查两侧账号、白名单和凭据一致性。启动沿用已有消费者和会话数据，不创建新 token 或清空数据库。

脚本管理收发服务，微信本身需手动打开；后台运行也需要电脑保持唤醒。群聊自动回复要求可信元数据中的真实 @ 当前账号。

## 重复启动与异常处理

已由管理器启动且健康的服务会被复用。操作使用本机文件锁；端口冲突、配置不一致、活动事件订阅或群聊门禁异常时，启动会报告失败。

接管其他方式启动的旧服务时，可能需要先在原终端用 Ctrl+C 停止。管理器按入口文件和状态目录识别进程，不仅凭 PID 或端口结束进程。

停止时先等待 Codex 最多 220 秒，以便模型轮次保存结果，再分别等待 MCP 和 Hook 最多 65 秒退出。未发送草稿等待显式重启；已经提交的微信发送无法通过停止撤销。微信保持运行，已加载的 DLL 可能继续驻留，更换 DLL 仍需正常重启微信。

退出超时会保留上游并提示检查日志。启动失败仅回收本次新启动的服务。操作窗口中途关闭后，后台服务可能仍在运行，可再次查询状态或执行停止。

## 本地检查

```powershell
python -B -X utf8 -W error::ResourceWarning .\scripts\wechat_stack\test_manage.py
```

测试使用模拟 HTTP 子进程检查进程识别、端口冲突、启停及失败回收，不连接微信或模型。
