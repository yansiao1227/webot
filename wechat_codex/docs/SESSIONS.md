# Session 管理与恢复

## 映射和上下文

2026-10-24：新增独立 messages、context_progress 和 seen_events。完整会话记录与回复 jobs 分离；静默附件、未 @ 群文字和出站文字只记背景。生成成功时原子提交草稿及上下文进度。原生引用驱动附件获取的协议已通过本地合同测试，真实原生资源适配尚未完成，详见[实施与限制](../../docs/REFERENCE_CONTEXT.md)。

一个状态目录绑定微信账号、MCP地址和consumer ID，不允许混用。`conversations` 保存会话ID、Codex thread ID、上下文代次epoch和暂停状态。私聊一个thread，群聊一个thread；同群被允许的消息共享上下文，不跨群或跨私聊。

同一会话只处理最早未完成job；不同会话最多2个并行，配置范围1–4。Codex thread首次使用时创建，后续复用；重启后按持久thread ID恢复。恢复失败时暂停，不静默创建新thread丢失上下文。这些thread独立于当前开发聊天；是否显示在Desktop历史中取决于实际Codex环境，适配器不操作Desktop UI。

微信消息通过SDK ExternalMessage传入，权限级别低于机主指令，不被当作新的用户授权。凭据不进入提示词。MCP发送由本地固定代码执行，模型只产生符合JSON schema的reply；不能选择接收者或更改event_id。

## 事务顺序

1. 独立consumer长轮询MCP（新consumer从latest注册），只对本地新消息唤醒，不用模型轮询。
2. 原始事件按event_id落盘去重后，才ack上游。ack丢失时重投同批，不重复job。
3. 持久保存thread映射，标记generating，再调用模型。
4. 模型最终结果完整保存为generated，发送前再次核对桥接账号、白名单和MCP群@门禁。
5. 先保存sending，再调用MCP reply_text。回复被绑定到原事件会话；MCP创建和复用唯一发送意图。
6. accepted为本地接受终态，原始delivery_confirmed=false保持，不冒充对端收件。

## 中断边界

| 重启时状态 | 行为 |
| --- | --- |
| pending | 处理原事件，复用已保存thread |
| generating | 标记generation_unknown并暂停会话；模型可能已把轮次写入历史，不自动再生成 |
| generated | 复用已保存回复，重新核对权限后发送，无需再调用模型 |
| sending | 只查询原event_id的MCP回复状态，不再POST |
| accepted | 不处理、不重发 |
| unknown / failed | 暂停该会话，保留证据；其他会话不受影响 |

未知操作不自动重试。模型失败、不可恢复的thread、权限不匹配等错误也不自动换thread、换账号或绕过MCP发送。

## 显式重置

会话无pending/generating/generated/sending工作时可执行：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\agent.py reset --conversation wxid_会话
```

这会清空当前thread映射、增加epoch并解除暂停；下一条新消息建立新thread，旧记录保留但不重放。若存在排队或执行中的任务，拒绝重置，需先核查处理；未知发送在原接口中查清前不要人工换ID重发。重置是本机管理命令，不通过微信聊天正文触发。

## 处理超时的上游提示（2026-10-24）

模型处理超时不再把会话留在generation_unknown并阻塞后续请求。后端使用SDK turn handle绑定确切轮次，超时后请求interrupt并等待终止状态；当前指令生成一条明确超时错误，经正常MCP回复门禁和原事件唯一发送意图发送。随后队列继续，不自动重新执行超时请求。每次新轮次先确认原thread为空闲；旧轮次仍活动时返回明确暂未执行提示，不把新微信事件加入旧任务。

进程崩溃/中断的未知模型结果与发送状态未知仍单独保留核查规则，发送未知不重发。已完成但客户端等待超时的旧轮次，必须核对同thread、实际完成轮次、原请求和附件绑定后才恢复已生成回复，不能把模型重跑当恢复。

41项业务回归通过；state/timeout-validation/result.json是真实Codex SDK超时、中止确认与同thread下一轮恢复验收，不发送微信。

真实微信超时事件134已收到一次错误提示，后续137正常回复，用户确认“均正常回复”；正常时限已恢复。当前43项业务回归通过，包含按可信发送任务原生排序序号排除自动回复回显、同正文人工发言保留、初始/最终快照去重及后续上下文进度。

用户选择普通文字模型时限30秒：`text_model_timeout=30`。没有可靠附件引用目标的文字（包括文字引用、连续追问）采用此时限；精确关联的附件引用处理继续用 `model_timeout=180`。本机测试用的 `model_timeouts` 会话覆盖仍优先，聊天正文不能改变预算。该值是准备完成后的模型决定等待预算；启动、正文停滞及整轮上限分别计时，详见[计时与分段规则](ADAPTIVE_REPLIES.md)。中止确认和发送另需时间，不承诺30秒整对端收到。

`mark_read_on_process` 默认为 true：合法任务开始时，通过 MCP `mark_conversation_read(event_id)` 请求原生已读，模型处理后查询一次状态，结果存在任务 `conversation_read`；失败/超时不阻断生成与回复，未知操作不重新提交。静默附件、未 @ 群消息不独立清未读。业务不访问微信数据库、不直接调用原生DLL，也不把业务消费确认当微信已读。

2026-10-25 用户要求改为按需选择背景，默认 adaptive_context=true。完整消息继续保存，模型按问题使用独立执行线程和有界背景；旧会话thread映射保留，同一问题的分段共享任务线程，重启可按task_thread_id恢复。分段发送独立去重、逐段检查群@，文字每段模型预算30秒。见 [ADAPTIVE_REPLIES.md](ADAPTIVE_REPLIES.md)。此前“每次补入全部未注入背景”的描述仅适用于旧执行模式。
