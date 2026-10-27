# 4.1.9.30 接收运行时验证记录

2026-10-22，用户授权实现DLL加载、Hook生命周期及本地输出，在当前官方微信上验证收消息；明确禁止发送。用户指定“测试群”，仅从既有receive-stream-live-02.ndjson提取唯一受控群ID用于接收白名单，没有读取旧正文。

## 已完成

- 固定官方进程PID34332，创建FILETIME134371105571622195，版本4.1.9.30，core完整SHA与固定artifact匹配；真实线程及代码操作前均核对身份。
- 自有host：跨进程DLL加载→启用→三条中文事件→显式停止→恢复后原调用，另测TTL到时、max-events自动停止、原函数执行中请求停止，四项均通过。
- 真实微信5秒占位端点窗口：ready成功；end code0、restored=true、active=0、无消息。独立复读入口/CALL共32字节完全一致。
- 真实测试群120秒窗口：正常ready、正常恢复，observed=0、emitted=0；不能据此宣称已收到真实消息。

## 后续：驻留多会话v2

当前v2使用同一DLL、同一trampoline重复启停，配置和队列按会话分配，duration=0持续到主动停止或控制进程退出。

自有host同DLL运行7轮会话、9项检查通过，覆盖持续多条、重复/外部停止、并发启动拒绝、配置与计数隔离、控制端崩溃恢复、旧回调仍执行时拒绝重启、回调退出后回收旧会话再正常接收。四项原基础跨进程测试也通过。

真机4.1.9.30三轮检查通过：DLL基址`0x63b00000`，dll_reused依次false/true/true，generation依次1/2/3；每轮工作线程退出0、入口/CALL外部复读一致，最终state=idle、installed=0、admitted=callbacks=0。本轮没有生成发送输入，三轮捕获数均0，因此不把它写作v2真实消息捕获或长期运行验证。

驻留构建：`work/client-hook/runtime-20261022T063733Z-9b8f3846`。真机逐轮日志和状态在`work/client-hook/resident-live-results.json`，自有进程结果在resident-test-results.json。接口见[RESIDENT.md](RESIDENT.md)。

## 最新：真实测试群记录已捕获

用户随后明确说明上一轮尚未发送，等待长窗口就绪。重新构建并在同一官方进程上开启600秒、max-events=1窗口，通知就绪后捕获测试群kind1入站记录，正文载荷为成员前缀加`RE_STREAM_G02`。原始正文保留；没有代发任何消息。

执行目录：`work/client-hook/runtime-20261022T060450Z-66d8370f/run-c38d85b889cf`。入口总命中、来源门槛命中及输出均为1；返回RVA为`0x9DACD5`；route_missed/force_missed/filtered/rejected/dropped/queued_discarded均0，identity_changed=false。记录+18为指定群、+38等于现场self、+58与正文成员前缀一致。

达到1条后自动停用：worker退出0、pending=none、restored=true、active=0；外部复读入口与CALL共32字节完全一致。随后只读复核自己的DLL计数器为accepting=0、active=0。DLL/trampoline保持惰性驻留，未执行卸载。

由此验证固定4.1.9.30上的官方客户端→DLL Hook→有界快照队列→本地NDJSON→恢复入口链路。profile已标记该范围的live ABI、入口及文本采集验证通过；这不证明覆盖全部消息类型、长期运行或新消息去重，观察点本身仍不能单独排除历史/同步重放。

## 较长窗口

在用户已授权的同一测试群上延长至600秒、max-events=1，增加全入口及来源门槛计数。真实执行目录：

`work/client-hook/runtime-20261022T044508Z-23be9280/run-80fe1b222861`

2026-10-22 12:50左右的只读复核：accepting=1、active=0、entry_hits=0、observed=0，其他拒绝/丢弃计数均0。独立读取入口16字节为`e9e0535fffec204889ce84d2750980be`，确认存在JMP补丁；结果保存于during-hook.json。

该历史窗口于北京时间约12:55正常结束：code=0、restored=true、active=0；entry_hits/observed/filtered/rejected/dropped/emitted均为0，identity_changed=false。控制器独立复读入口和CALL共32字节与安装前完全一致。当时没有真实捕获证据，故当时只标记生命周期验证。后续用户明确此前尚未发送，随后新窗口的成功结果见上节，不将该空窗口当作入口失效证据。

## 证据目录

| 检查 | 目录 |
|---|---|
| 5秒真实启停 | work/client-hook/runtime-20261022T043830Z-834b1233/run-20d9b50cfb3d |
| 120秒测试群 | work/client-hook/runtime-20261022T044123Z-e1a6f91c/run-0eb15a296068 |
| 600秒测试群 | work/client-hook/runtime-20261022T044508Z-23be9280/run-80fe1b222861 |
| 最新源码构建与四项自有host测试 | work/client-hook/runtime-20261022T045422Z-7c674f7b、work/client-hook/runtime-test-results.json |
| 真实群文本捕获成功 | work/client-hook/runtime-20261022T060450Z-66d8370f/run-c38d85b889cf |

只有自己的fixture主动构造合成消息；没有调用真实微信发送函数或通过UI代发。真实微信收到的端点/正文不替代用户对测试来源的确认。账号ID不存进版本profile。

## 生命周期限制

停止恢复入口，但DLL/trampoline保持驻留；没有远程FreeLibrary，避免迟到回调引用释放代码。上文v1使用单轮构建；当前v2改为同DLL多会话、旧回调引用保护及控制端存活检查。仍未验证生产级高并发、异常展开、长期真机稳定性或全类型消息覆盖。

只读窗口核查曾遇到Windows.Graphics.Capture超时，可访问性仅暴露“微信”根窗口，未能由UI确认聊天界面。仅执行过一次置前，没有点击、输入、登录或发送操作；不把窗口存在当作已登录证据。
