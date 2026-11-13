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

状态查询会单独显示回复任务异常数量。发送待确认、发送失败或已中止的任务不单独使 Codex 判为未就绪；模型或引导执行仍待核查、账号及授权不匹配等异常继续保留就绪门禁。

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

## 跟随微信自动运行

配置好上述环境后，在项目根目录执行以下命令，可立即在后台启动监控器，并注册当前 Windows 用户下次登录时自动运行：

```powershell
python -B -X utf8 .\scripts\wechat_stack\autostart.py start
python -B -X utf8 .\scripts\wechat_stack\autostart.py install
python -B -X utf8 .\scripts\wechat_stack\manage.py status
```

后台入口使用已配置 Python 环境内的 `pythonw.exe`，不弹出终端。注册使用当前用户的 Windows Run 项，无需管理员权限，不修改微信安装目录。`install` 仅注册，`start` 仅启动；重复启动由独立实例锁拦截。仓库搬迁前先执行 `autostart.py uninstall`，搬迁后重新注册。

监控器每 5 秒检查微信，沿用 Hook 的固定版本指纹、进程创建时间、同用户及只读登录身份校验。连续两次确认原配置账号后，按 Hook → MCP → Codex 启动，并在启动阶段再次校验身份。关闭微信窗口但进程仍在运行不影响服务；退出登录、换号、进程退出或身份校验失败时，正常停止收发服务。重新打开并登录原账号后自动接入新实例，不自动登录或拉起微信。

微信异常退出与正常退出均通过进程消失检测。监控器按 Codex → MCP → Hook 收尾，旧服务停止超时则保留上游并阻止新实例启动；错误重试从 10 秒退避至最多 300 秒。监控器自身重新启动时，也先正常收回既有服务，再启动已核验的实例。服务进程缺失时清理整栈后恢复；服务仍在但健康检查未通过时显示 `degraded`，不靠重启绕过任务或授权门禁。

手动入口仍可使用：`stop_wechat.cmd` 会持久暂停自动跟随并停止服务，避免立即被重新拉起；`start_wechat.cmd` 清除暂停并立即尝试启动，仍要求微信已登录。如果希望恢复跟随但等待稍后登录，执行：

```powershell
python -B -X utf8 .\scripts\wechat_stack\supervisor.py resume
```

退出监控器、取消登录自启动及查看注册状态：

```powershell
python -B -X utf8 .\scripts\wechat_stack\supervisor.py stop
python -B -X utf8 .\scripts\wechat_stack\autostart.py uninstall
python -B -X utf8 .\scripts\wechat_stack\autostart.py status
```

退出监控器和取消自启动均不停止现有收发服务。要全部停止，先执行 `stop_wechat.cmd`，再退出监控器；正常启停正在进行时，退出请求会等待本轮操作完成。可用 `supervisor.py run` 前台运行监控器，Ctrl+C 退出。

`manage.py status` 显示自动跟随是否暂停、监控状态及心跳是否过期。主要状态为 `waiting_wechat`（等待微信）、`waiting_login`（等待登录）、`waiting_account`（等待原账号）、`stabilizing`（确认稳定身份）、`running`、`paused`、`degraded` 和 `retry_wait`（等待重试）。运行记录保存在本目录忽略的 `state/` 内，`supervisor.log` 记录服务启停输出；不记录账号或凭据。

自动恢复只恢复服务，不重置消费者、任务或请求 ID，不自动重做结果未知的工具操作或发送。原有业务恢复规则继续生效，未发送草稿不保证自动补发，`accepted` 仍仅表示本地接受。停采集期间的历史消息不会自动补取。

## 重复启动与异常处理

已由管理器启动且健康的服务会被复用。操作使用本机文件锁；端口冲突、配置不一致、活动事件订阅或群聊门禁异常时，启动会报告失败。

仅使用手动模式时，同一微信进程重新登录同一账号后，Hook 会在确认原钩子已安全退出、身份连续稳定后自动恢复采集；MCP 沿用原消费者自动续连，不需要重启整套服务。恢复等待与阻塞原因见 Hook `/client_hook/status` 的 `capture_recovery`。手动模式下换号、微信进程实例变化或原生校验失败需人工处理；自动模式按上节规则停止并重新核验。

接管其他方式启动的旧服务时，可能需要先在原终端用 Ctrl+C 停止。管理器按入口文件和状态目录识别进程，不仅凭 PID 或端口结束进程。

停止时先等待 Codex 最多 220 秒，以便模型轮次保存结果，再分别等待 MCP 和 Hook 最多 65 秒退出。未发送草稿等待显式重启；已经提交的微信发送无法通过停止撤销。微信保持运行，已加载的 DLL 可能继续驻留，更换 DLL 仍需正常重启微信。

退出超时会保留上游并提示检查日志。启动失败仅回收本次新启动的服务。操作窗口中途关闭后，后台服务可能仍在运行，可再次查询状态或执行停止。

## 本地检查

```powershell
python -B -X utf8 -W error::ResourceWarning .\scripts\wechat_stack\test_manage.py
python -B -X utf8 -m unittest discover -s .\scripts\wechat_stack -p "test_*.py"
```

测试使用模拟 HTTP 子进程及隔离状态机检查进程识别、端口冲突、启停、崩溃恢复、手动暂停及失败回收，不连接微信或模型，不注册本机自启动。
