# 群聊 @元数据

2026-10-23：新版已在微信重启后加载。测试群真机对照确认：普通消息、手输@昵称均获得known空名单；从微信菜单真正@当前账号的消息，名单准确包含当前账号。原生@识别对照通过；用户明确确认第三条自动回复对端收到、前两条无回复，端到端验收通过。

## 数据来源

固定微信4.1.9.30 x64及既有SHA256。消息记录 `+0x180` 为独立的 native narrow string，不从正文提取。离线只读分析确认：`0x9DBBF0` 将该字段传给 `0x2210670`，后者解析 `msgsource` 和其直接子节点 `atuserlist`。对应字符串RVA为 `0x81AE53F` 和 `0x8510A7B`。分析保存在 `state/group-mentions-investigation/`；为普通 Ghidra/PE 工具证据，并非 REA MCP Evidence。

在原回调执行前同步复制，检查字符串头、长度、UTF-8、终止符和快照一致性。来源最多16384字节；总读取预算90000字节、100ms deadline。未知或读取失败时保留文本、将 @信息标为 unknown，不猜测。原始来源只保留于本机 native 事件日志，不作为消息正文，也不整体投递到 agent。

## HTTP 事件字段

`/messages` 的每条事件新增：

```json
{"mentions":{"state":"known","wxids":["wxid_self"],"all":false,"source":"weixin41930_record_source_180"}}
```

`state` 为 known 或 unknown。旧 DLL 没有来源采集字段时返回 unknown。列表按完整账号 ID 匹配，`notify@all` 单独映射为 all=true，不自动加入当前账号。仅出现正文“@昵称”不会增加名单。XML非法、DTD/ENTITY、重复或嵌套 atuserlist、超长或采集不完整均返回 unknown。没有名单时列表为空。

此模块仅报告元数据；MCP 的自动回复硬性限制由平级 wechat_mcp 独立实施。群 @发送、图片/语音/文件收发不属于本次增量。
