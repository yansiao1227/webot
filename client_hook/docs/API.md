# client_hook 文本收发使用说明

2026-10-24：已扩展文本出站及六类附件事件、引用解析、资源任务／分块HTTP接口。重启微信加载新版DLL后，真实文字服务器消息ID、原生引用及MCP精确关联已核对通过；六类附件与下载入口仍在真机验证和适配。DAT分段解密及本地SILK解码已加入，当前仅返回内容校验通过的已缓存资源；未完成的原生下载不得视为可用。详见[固定版本原生适配记录](NON_TEXT_41930.md)及[三层实施记录](../../docs/REFERENCE_CONTEXT.md)。

**2026-10-22 最新验收：** 已将原生提交入口从 RVA `0x166AAD0` 修正为 `0x166C970`，加载新构建后向颜料盒私聊与测试群各发一条 P02/G02 文本，用户确认对方客户端收到。17项测试和两项生命周期回归通过。`accepted` 仍仅表示本地接受，接口没有服务器或对端回执；本次收件证据为用户确认。详见 [发送分支修复记录](SEND_PATH.md)。

本文位于 `client_hook/docs/`；下文模块目录指上一级 `client_hook/`，命令的工作目录按各节说明执行。实现过程见 [消息监听与发送图解](implementation.html)。

本期提供私聊、群聊文本发送和入站文本采集，以及本地 HTTP、消息持久化、长轮询、确认、回调和 Python 客户端。实现适配微信 4.1.9.30 x64，使用 Python 标准库与模块内 MinHook；构建及运行不需要 WeChat-Hook 参考仓库或本项目其他源码目录。

模块现位于项目根目录 `client_hook/`。迁移保留了全部状态和历史证据，服务按用户要求保持停止。此前真机验收使用的数据库位于 `state/live-validation/`；重新打开微信后，如需沿用原有消息和消费者进度，从项目根目录执行 `python -B -X utf8 .\client_hook\client_hook.py serve --state-dir .\client_hook\state\live-validation`。

当前 17 项本期测试及生命周期回归通过；真实微信已完成身份/登录查询、私聊和群聊各一次文本发送、两条入站标记的持久消费、入口恢复和 API 重启续取。用户已确认两条出站标记均已显示，完成本期短时文本收发验收；一般请求的本地接受仍不等于对端送达回执。图片、语音、文件、群 @发送、下载、解码和历史消息属于后续阶段，相关路由返回明确的 `not_implemented`。

## 构建与启动

在 `client_hook` 目录执行，默认需要 64 位 Python 和 `.env`中的`RE_WECHAT_GCC`：

```powershell
.\scripts\build_runtime.ps1
python -B -X utf8 .\client_hook.py serve
```

编译器可通过 `-Compiler` 指定，Python 可通过 `-Python` 指定。产物写入本目录 `build/runtime-唯一标识`，每次构建生成新目录，不覆盖旧 DLL。`build/runtime-current.json` 指向新构建；协议 v3 的公开状态块为 224 字节。固定版本发送 profile 随源码保存，构建不需要读取历史 `work/` 目录或真实微信 DLL。

默认附加唯一的匹配微信进程。多个客户端时使用 `serve --pid PID`。默认采集所有会话，`serve --endpoint wxid_xxx` 或 `--endpoint 数字@chatroom` 可限定一个会话。原始消息经过本地消费者；采集及消费确认不改变微信已读。新增独立原生会话已读接口见文末和 [READ_STATE_41930.md](READ_STATE_41930.md)。

当前进程若已加载旧版 DLL，必须先正常退出并重启微信，再启动服务。旧 `scripts/receiver.py` 命令仍可用，但它与 HTTP 服务共用进程互斥，不能同时独立监听同一客户端。进程重启前不要删除或覆盖任何已经加载的构建目录。

默认服务为 `http://127.0.0.1:30001`；状态目录为模块内 `state/`，可用 `--state-dir` 修改。同一状态目录只允许一个服务实例。使用 Ctrl+C 正常停止；控制进程失联时驻留引擎会关闭接收 Hook，DLL 保持驻留，不尝试并发卸载。

```powershell
python -B -X utf8 .\client_hook.py --token my-local-token serve --port 30001
python -B -X utf8 .\client_hook.py --token my-local-token status
```

也可设置 `CLIENT_HOOK_TOKEN`。非回环监听必须设置令牌；设置令牌后所有接口需要 `Authorization: Bearer ...`。启动参数支持 `--StartPort` 与 `--CallBackURL` 别名。本期仅支持 HTTP、解析后的文本，不提供 WebSocket 或原始 PB 接口。

## agent 获取最新未消费消息

不需要写定时任务。服务持续采集并保存消息，agent 通过稳定消费者 ID 接入：

```powershell
python -B -X utf8 .\client_hook.py listen --consumer-id my-agent
```

该命令内部使用长轮询，按行输出 JSON，成功输出整批后确认。它确认的是输出成功，不能替代下游 agent 的业务处理确认；需要严格控制处理结果时，用 Python SDK 或直接 HTTP，在业务成功后再 ack。

HTTP 调用顺序：

```http
POST /consumers
Content-Type: application/json

{"consumer_id":"my-agent","start":"earliest"}
```

```http
GET /messages?consumer_id=my-agent&timeout=30&limit=100
```

有未消费消息立即返回；没有时等待至消息提交或 30 秒超时。超时是最长等待时间，不是每 30 秒才检查一次。返回示意：

```json
{
  "events": [{
    "event_id": "数据库UUID:1",
    "account": "wxid_self",
    "message_type": "text",
    "payload": {"text": "你好"},
    "conversation_id": "wxid_peer",
    "fromid": "wxid_peer",
    "toid": "wxid_self",
    "direction": "incoming",
    "source": "live",
    "captured_at": 1792650000.0
  }],
  "next_cursor": "数据库UUID:my-agent:0:1",
  "ack_token": "本批次确认token",
  "has_more": false,
  "capture": {"state":"running"}
}
```

业务处理整批成功后：

```http
POST /consumers/my-agent/ack
Content-Type: application/json

{"ack_token":"本批次确认token"}
```

再发下一次 `/messages`。也可带上已确认批次的 `next_cursor` 校验位置；客户端不能用任意游标跳过消息。SDK 使用服务端保存的进度，不需要自行管理游标。

- `earliest` 从保留区最早消息开始；`latest` 仅从首次注册时之后的新消息开始。已存在的消费者保持原进度，注册参数不会偷偷重置。
- 每个消费者的进度独立；同一消费者只允许一个等待请求。未确认时重新读取返回同一批次和 token。
- agent 或服务重启后，使用相同消费者 ID 继续读取。处理失败不要 ack；处理过但未确认的事件可能再次返回，以 `event_id` 去重。
- 空批次的 `ack_token=null`。检查 `capture.state`：采集暂停不会被描述为持续运行，SDK 会对暂停明确报错。
- `captured_at` 是本地采集时间，不是微信服务端消息时间。未验证的微信消息 ID、群成员身份和时间不伪造。群消息的 `conversation_id/fromid` 是群 ID，`actor_candidate` 仅保留原字段候选，不能当作已确认群成员。

Python 接入可参考 `examples/agent_listener.py`：处理器返回后 SDK 才确认。本地数据库内的业务写入可与去重记录放在同一事务，外部副作用仍需业务自身幂等。

### 保留与缺口

默认保留 7 天、事件内容逻辑容量 1 GiB，可通过 `--retention-days`、`--max-bytes` 调整。SQLite 使用 WAL 和 FULL 同步。容量仅衡量事件内容，不代表数据库、待确认批次和发送记录的物理文件上限。

过期未消费消息返回 HTTP 410 `cursor_expired`；客户端必须查看缺口后显式执行：

```http
POST /consumers/my-agent/reset
Content-Type: application/json

{"start":"earliest"}
```

`latest` 表示明确跳过现存积压。重置会撤销旧 token，并记录跳过范围。不会自动重置。

这里的“未读”是 agent 尚未确认的已采集事件。本期不能查询服务启动前的微信历史未读，也不补取服务停止期间的消息。原生快照队列未落盘前遇到进程崩溃仍可能丢失；状态会记录已知覆盖缺口，不能推算未知丢失条数。历史查询与补取保留在后续计划。

## 发送文本

```powershell
python -B -X utf8 .\client_hook.py send --to wxid_test_peer --text '你好 😀' --request-id greeting-001
```

```http
POST /SendTextMsg
Content-Type: application/json

{"toid":"wxid_test_peer","msg":"你好 😀","request_id":"greeting-001"}
```

`toid` 支持 wxid、数字群 ID 和 `filehelper`，兼容 `wxidorgid`；`msg` 兼容 `content`。同义字段取值冲突、空正文、NUL、无效 Unicode 或 UTF-8 超过 64 KiB 均拒绝。正文不会截断。

同步最多等待 2 秒。正常返回 HTTP 200、`ret=0`、`state=accepted` 仅表示客户端本地接受，`delivery_confirmed=false`。未完成返回 HTTP 202，使用原请求 ID 查询：

```http
GET /client_hook/requests/greeting-001
```

状态为 `queued/running/accepted/failed/unknown`。不要因超时生成新 ID 重发。相同账号、ID 和内容复用原操作；内容不同返回 409。提交附近发生崩溃时保留 `unknown`，不自动重投。普通幂等记录保留 7 天，未知结果不会自动清理。DLL 还保留每次请求的原生操作记录，最多 65536 项，达到上限明确拒绝，需要正常重启客户端后继续。

原生提交在验证过的客户端执行队列运行；首次队列回调动态确认执行线程，不硬编码历史线程 ID。每次创建独立文本对象，校验身份和 profile，转移输入所有权并释放结果和上下文。原生等待超时后查询同一次操作，不终止微信线程。生命周期不明时保留可能仍在使用的内存。

## 状态与回调

| 接口 | 行为 |
| --- | --- |
| `GET /client_hook/status` | 采集覆盖、队列统计、事件存储、回调错误及能力验证范围 |
| `POST /IsLoginStatus` 加 `{}` | 可确认当前身份时返回 `onlinestatus="3"`；无法确认时返回 503，不根据进程存在猜测登录状态 |
| `POST /GetSelfProfile` 加 `{}` | 真实自身 wxid；其他尚未核实的资料返回 null |
| `GET /consumers/{id}` | 消费进度、待确认批次与过期状态 |
| `GET /openapi.json` | 本期可用接口的 OpenAPI 3.1 描述，后续能力单独列出 |

回调可选，不影响 agent 的消费进度：

```powershell
python -B -X utf8 .\client_hook.py serve --callback-url http://127.0.0.1:8003/messages
```

每条文本使用文档的 `ServerPort/selfwxid/sendorrecv/msgnumber/msglist` 外层格式并附加本地 `event_id`。HTTP 2xx 视为接收成功，5 秒超时或其他错误会暂停回调并保留批次。服务不自动重试；检查错误后 `POST /client_hook/callback/resume` 加 `{}` 显式恢复。对方处理成功但响应丢失时可能重投，应按 `event_id` 去重。内部 `callback` 消费者 ID 不对外开放重置和确认。

如果回调消费者已过期或账号发生变化，先查看状态，再对已暂停的回调执行 `POST /client_hook/callback/reset` 加 `{"start":"earliest"}` 或 `{"start":"latest"}`，最后 resume；重置会记录跳过范围，不能恢复已清理事件。

## 验证与独立使用

```powershell
.\scripts\build.ps1 -RunFixture
.\scripts\build_runtime.ps1
python -B -X utf8 .\tests\lifecycle\test_runtime.py
python -B -X utf8 .\tests\lifecycle\test_resident.py
python -B -X utf8 -m unittest discover -s .\tests -v
```

测试只启动自有 host；`tests/test_native_api.py` 实际跨进程调用驻留 DLL，验证连续文本发送、原始字节、释放计数和全链路接收，并非用 HTTP 模拟成功替代原生验证。`serve --simulate` 是显式的 HTTP 模拟模式，状态会标注 simulation，不加载微信或 DLL。

将本目录复制到独立位置后重新构建即可。可通过 `CLIENT_HOOK_BUILD_ROOT` 为控制器和构建指定同一个产物根目录；`scripts/build_runtime.ps1 -OutputDirectory` 也支持独立输出，但运行控制器时必须指向同一根目录。原始接收中转文件按 8 MiB 轮换，控制器读完后回收旧文件；异常退出遗留的单次运行目录可在确认相关进程退出后清理。正式消息与消费进度保存在 `state/messages.sqlite3`。

## 群聊 @接收元数据（2026-10-23）

事件新增 `mentions`，包括 state、wxids、all、source。来源为原生独立msgsource字段，不解析正文。字段和未知状态说明见 [MENTIONS.md](MENTIONS.md)。原生fixture、解析回归及真机三条对照已通过；用户确认回复对端收到、负向对照无回复，验收通过。

## 图片、视频、文件主动原生下载

固定微信4.1.9.30、当前账号已登录且原采集运行时已构建后，构建独立下载侧车：

```powershell
python -B -X utf8 .\scripts\build_resource_sidecar.py
```

构建生成build/resource-current.json及固定SHA、入口清单；不在构建时注入。获取附件时按操作绑定侧车版本，核验当前核心、模块摘要和账号后加载；缺少产物、错误版本、原生失败或完成状态未知均返回明确错误，不回退假成功。生产使用原resources/acquire和分块接口，无新增任意路径、账号或DLL参数。

缓存miss的图片、视频、文件由client_hook在原生owner queue请求精确事件的CDN资源，完成并验证后才ready。成功即删除原生临时副本，7天清理仅遍历自有散列目录，活动/未知操作延后。仅操作员使用validate_native_download.py和cache_validation.py验收；业务层不得直接读hook数据库或操作微信缓存。

出站事件增量字段：native_sort_seq、native_created_at、native_record_metadata_source=weixin41930_record_identity_v2；原生数据库精确映射可另给出native_local_id、message_id、native_message_id_source及ready/pending/unavailable状态。缺少映射不冒充服务器ID。标准live发送引擎以精确TextInput对象绑定Task的record+0x110，发送结果标注native_sort_seq_source=weixin41930_send_task_record110_v1。OpenAPI由api/spec.py生成，Python SDK原样传递这些字段，无任意数据库查询入口。

native_created_at仅由同账号、同会话的原生数据库精确行create_time提供，并要求native_message_id_source为weixin41930_message_db_exact_sort_seq_v1；raw_created_134的早期命名错误，不能作为创建时间。history_filter统计区分filtered_old、accepted_current及unresolved，必须同时证明旧记录被过滤与新记录被保留，不能将“所有出站都被过滤”算作通过。

会话原生已读新增 `POST /client_hook/read/mark`、`POST /client_hook/read/status`，仅传原入站 `event_id`。Python SDK 对应 `mark_conversation_read`、`conversation_read_status`。独立构建 `python scripts/build_read_sidecar.py`；实际未读清零与原生提交分开报告，unknown 不自动重试，消费者 ack 不影响微信已读。实现和验收范围见 [READ_STATE_41930.md](READ_STATE_41930.md)。
