# 4.1.9.30 DLL 接收运行时

本文保存v1单轮运行时的历史规范。当前驻留、多轮启停接口为v2，请使用[RESIDENT.md](RESIDENT.md)；下述2480字节配置、单次运行限制和构建后重开窗口流程不再是当前API。

当前实现只提供接收，没有发送接口、客户端业务调用或HTTP服务。读取官方客户端现有会话，不实现扫码和设备验证。

## 构建和入口

```powershell
.\client_hook\scripts\build_runtime.ps1
python -B -X utf8 .\client_hook\tests\lifecycle\test_runtime.py
python -B -X utf8 .\client_hook\scripts\receiver.py --endpoint 12345@chatroom --duration 120 --max-events 1
```

endpoint替换为已确认的测试群或对端ID，不接受自身ID。持续时间1..600秒，max-events为0..1000（0表示到时停止）。ready后从另一个账号发来文本；stdout输出UTF-8 NDJSON。stdin `stop` 请求停止，达到条数或时限也会停止。

每次构建写入新的 `work/client-hook/runtime-<UTC>-<id>/`，包括自有hook-host.exe、hook-fixture.dll、hook-live.dll及pins.json。runtime-current.json指向本轮构建；receiver只加载这些固定文件并匹配完整SHA256，不接受任意DLL路径或函数地址。相同DLL在同一进程仅允许运行一次；后续窗口需新构建，第一版没有复用/自动重启逻辑。

## 加载与启用

1. 对真实微信执行已有metadata-only status，要求唯一4.1.9.30目标、固定磁盘SHA和进程创建时间。额外核对SID、无调试器及进程实例互斥，避免与原断点观察器同时运行。
2. 独占只读打开编译产物，禁止写入/删除。离线解析HookRun导出RVA，不在控制器加载该DLL。
3. 根据LoadLibraryW实际所属系统模块计算远程地址，并比较本地/远程入口16字节，再创建远程线程加载固定DLL。通过模块枚举取64位DLL基址，不把线程32位退出值误当HMODULE。
4. DLL的DllMain只禁用线程通知。第二条远程线程调用HookRun，验证无指针配置、PID/FILETIME、路径和加载模块磁盘SHA；再次匹配入口/CALL字节。
5. 只读既有账号对象的身份串，核对vtable、getter代码和前后稳定性；拒绝订阅自身ID。这里没有调用账号getter，没有读取凭据、数据库或历史正文。
6. 安装MinHook，限定返回地址base+9DACD5和force=1；采样器校验固定record vtable及端点白名单。准备成功才写ready。

远程配置使用2480字节的固定x64结构，包含目标实例、接收限制、端点、事件文件和随机命名停止事件。remote path/config在其对应线程结束后释放；等待超时则保留仍可能被使用的缓冲区，并在controller.json记录pending，不强杀微信或远程线程。

## 采样与本地输出

Hook线程复核现场账号身份，使用既有stream_snapshot进行本进程有界读取：先校验端点，命中才读正文；只接受kind1且+38为当前self的入站记录。前后再次核对账号身份，变化则停止。

复制后的POD快照进入32项队列，生产者TryAcquireSRWLock，不等消费者。写入失败/队列满会计数并触发停止。独立HookRun工作线程序列化至本次run目录下events.ndjson，控制器增量读取同一文件并输出stdout；文件创建使用CREATE_NEW，不覆盖既有文件。

事件包括ready、message、end。message保留body、endpoint_18、endpoint_38、actor_candidate_58、self_id和固定构建的incoming标签；群正文保留原始成员前缀。尚无业务消息ID去重或历史/实时区分，不能把每条转换记录视为唯一网络到达事件。每串511字节，超长或非法UTF-8拒绝。

停止后的队列内容清零，未消费项计入queued_discarded；已消费槽位立即清零，不将借用字符串指针保留给工作线程。

## 停止与驻留策略

停止顺序：关闭accepting→MH_DisableHook恢复入口→等待当前active回调（最多2秒）→核对入口16字节→清队列→写end→关闭文件/停止事件/磁盘句柄→HookRun退出。控制器独立复读真实入口和CALL共32字节，要求与安装前一致。

**停止不卸载DLL，也不释放trampoline。** 存在被跳转进detour但尚未递增active的线程，仅凭active==0不能证明FreeLibrary或MH_RemoveHook安全。本实现用GetModuleHandleEx(PIN)保持DLL，保留MinHook记录和trampoline到微信进程退出；迟到的detour会绕过采样直接调仍然有效的原函数。inactive时无轮询线程或业务调用。

因此end的restored表示入口已恢复，不表示模块已卸载；resident_after_stop始终明确为true。代码没有远程FreeLibrary、模块隐藏、防撤回或版本检查补丁。此阶段不适合不断新建窗口积累驻留DLL；后续产品化需要复用同一驻留模块的控制会话或经过证明的安全卸载。

## 已验证范围

自有host的跨进程四项测试通过：真实LoadLibraryW/HookRun、中文消息、独立本地输出、显式stop、到时停止、达到max-events停止、原函数仍在执行时请求停止并等待退出、恢复后原函数仍可调用。详见work/client-hook/runtime-test-results.json。格式检查以-Werror=format构建。

只读诊断命令：`python -B client_hook/scripts/runtime_status.py ABSOLUTE_RUN_DIRECTORY`。它验证目标实例和我们自己DLL的指纹，只读取该DLL中固定符号的计数器，不读取微信记录或聊天正文。各计数单独读取，不声称是原子的整体快照。entry_hits是所有helper入口次数，observed是返回地址/force门槛命中，filtered/rejected进一步区分白名单、方向或结构校验；last_return_rva仅用于定位共享调用路径。

2026-10-22真实微信短时启停：PID34332、4.1.9.30，5秒随机占位端点；ready成功，end code0/restored=true/active0，无消息采集；控制器复读入口和CALL共32字节完全一致。证据：runtime-20261022T043830Z-834b1233/run-20d9b50cfb3d。该检查只证明加载/安装/停用恢复，不证明真实消息捕获。

后续用户等待窗口就绪后，真实测试群kind1入站记录`RE_STREAM_G02`捕获成功，max-events=1自动停止，入口外部复读恢复一致。证据位于runtime-20261022T060450Z-66d8370f/run-c38d85b889cf，具体范围见本目录LIVE_VALIDATION.md。未实现发送能力，不会自动刷新窗口。
