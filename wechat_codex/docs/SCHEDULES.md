# 定时提醒与 Codex 任务

定时任务由 `wechat_codex` 常驻服务调度，计划、执行结果和管理请求保存在已有 `agent.sqlite3`。固定提醒到点直接投递文字；AI 任务到点使用独立 Codex 线程处理，再向创建任务的原会话投递成果。无需安装全局 skill、额外调度库或配置系统计划任务。

## 在微信中使用

在已授权私聊中直接提出请求，例如：

- “十分钟后提醒我休息。”
- “每天早上九点提醒我整理日程。”
- “每周一早上九点整理公开行业新闻，附上来源链接。”
- “查看我的定时任务”“暂停任务 `<任务ID>`”“把任务 `<任务ID>` 改到每天十点”“取消任务 `<任务ID>`”。

群聊需使用微信真实 @ 当前账号；昵称文字、普通 @ 字符和仅 @ 所有人不能授权。任务只发回创建时的原群，成员只能查看、管理本人在当前群创建的任务。第一版不支持指定其他群或联系人。

创建确认包含任务 ID、实际状态和下次执行时间。计划保存成功不表示提醒已经发送。自动执行的最终输出只包含要求的成果，发送由业务层统一负责。

AI 任务说明应包含未来执行所需的背景。需要微信引用资源时，先按现有引用流程处理，再把必要文字写进任务；定时任务不会保留未来读取微信附件的授权，也不自动读取完整聊天历史。

## 时间规则

默认按北京时间解释，内部保存 UTC 时间戳；查询显示带 `+08:00` 的时间。

| 类型 | `schedule-type` | `schedule-value` 示例 |
| --- | --- | --- |
| 十分钟后，仅一次 | `once` | `+10m` |
| 指定时间，仅一次 | `once` | `2030-01-01T09:00:00+08:00` |
| 每小时，从创建时开始 | `interval` | `3600` |
| 每天九点 | `daily` | `09:00` |
| 每周一九点 | `weekly` | `1 09:00` |

相对时间支持 `s/m/h/d`；固定间隔为60至31622400秒；每周用1至7表示周一至周日。不支持完整 cron/RRULE。服务约每秒检查一次，但并发、会话中的其他任务、工具和网络耗时都可能延迟实际执行。

服务停机或电脑休眠期间错过的一次性任务，恢复后补执行一次并注明原计划时间和延迟；周期任务跳到未来下一次，不补发积压。已认领、因会话忙而等待的执行记录仍会保留。同一任务不重叠执行，周期按计划计算，不随处理耗时持续漂移。

## 本地命令

以下命令在项目根目录执行，使用服务已经安装依赖的 Python。可在 `agent.py` 后、`schedules` 前添加 `--state-dir <状态目录>`。

```powershell
# 查看全部任务、单个任务及最近30次执行
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules list
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules show --task-id '<任务ID>'
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules runs --task-id '<任务ID>'

# 创建固定提醒；event-id必须是有效的原会话入站授权事件
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules create --event-id '<授权事件ID>' --name '休息提醒' --message '休息一下' --schedule-type once --schedule-value '+10m'

# 创建AI任务；message与ai-task二选一
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules create --event-id '<授权事件ID>' --name '行业周报' --ai-task '整理公开行业新闻并附来源链接' --schedule-type weekly --schedule-value '1 09:00'

# 修改、暂停、恢复、取消
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules update --task-id '<任务ID>' --schedule-type daily --schedule-value '10:00'
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules pause --task-id '<任务ID>'
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules resume --task-id '<任务ID>'
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules cancel --task-id '<任务ID>'
```

CLI 是机主管理入口，可查看和管理全部任务；微信入口仍按创建者隔离。只读查询可在服务停止时执行。修改操作必须由已就绪的服务处理；CLI 最多等待10秒，返回 `pending` 时使用下列命令查询原请求，不要重复创建：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py schedules request --request-id '<原请求ID>'
```

修改时间需同时提供类型和值；修改内容时提供 `--message` 或 `--ai-task`。存在未结束执行时不能修改任务。暂停会阻止后续执行和未提交的投递，但不保证中止正在进行的工具操作；取消还会撤销 MCP 长期授权。已经提交的发送不能撤回。

## 状态与恢复限制

任务状态包括授权中、活动、暂停、完成、取消和待核查；CLI 对应值为 `authorizing/active/paused/completed/cancelled/blocked`。一次性任务完成后保留记录，不自动删除。

模型执行中断时记录 `generation_unknown`，不重新调用模型。发送状态未知时记录 `unknown`，只允许查询原操作；任务进入 `blocked` 后不能用恢复命令绕过核查。`schedules runs` 保留模型线程、轮次和原发送标识，供人工定位；第一版不提供强制清除未知状态或自动重跑命令。不要通过新建任务或更换请求 ID 掩盖未知结果。

服务需要电脑保持唤醒、微信登录、MCP与Hook可用；AI任务还需要Codex服务可用。授权在创建时绑定账号、原会话及创建者，不依赖原消息七天保留期；执行和发送前仍重新检查账号、白名单及授权。`accepted` 只表示微信客户端本地接受，不代表服务器确认或对端收到。
