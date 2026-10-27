# 驻留接收接口 v2

适配Windows x64微信4.1.9.30。DLL首次加载后留在同一微信进程中，后续会话复用同一DLL和trampoline。当前目标进程已部署v2，验证后处于idle，入口已恢复，采样已停止。

## 使用

从仓库根目录执行，持续订阅此前的测试群：

```powershell
python -B -X utf8 .\client_hook\scripts\receiver.py events --endpoint 48418824345@chatroom
```

events（别名start）为前台进程接口，stdout逐行输出UTF-8 NDJSON。默认duration=0、max-events=0，持续到主动停止或控制进程退出；单次进程只保存计数和最后一帧，不积攒消息列表。每轮的原始事件另保存在work/client-hook/runtime-*/run-*/events.ndjson，文件随流量增长。

在另一终端查询、停止：

```powershell
python -B -X utf8 .\client_hook\scripts\receiver.py status
python -B -X utf8 .\client_hook\scripts\receiver.py stop
```

也可向events进程stdin写`stop\n`，或按Ctrl+C。stop对idle重复调用返回already_stopped。再次执行events会使用同一驻留DLL；loading帧的dll_reused、dll_base和ready帧的generation可核对复用情况。

可选参数：

| 参数 | 含义 |
|---|---|
| --endpoint | 必填，一个wxid或数字群ID；拒绝订阅当前自身ID |
| --duration 0 | 默认无限时；非零1..600秒到时停止 |
| --max-events 0 | 默认不限条数；非零1..1000条后停止 |
| --fixture-pid PID | 仅用于固定自有hook-host.exe的跨进程测试 |

旧的`scripts/receiver.py --endpoint ...`形式仍按events解析。对于程序调用，启动该子进程、持续读取stdout JSON、通过stdin或独立stop命令结束即可；同一微信进程只允许一个接收会话。

## 输出及状态

事件schema为client-hook-events-v2，主要帧为loading、ready、message、end和verified_end；错误为controller_error。ready/message/end带同一generation，seq从每轮1重新开始。message保留kind、原始正文、端点、成员候选、现场self_id及固定版本的入站标签；群成员前缀保留在原文。

status只读取已部署DLL的固定公开状态块，不启动接收、不加载DLL、不调用微信业务函数。状态字段是单字段读取的近似快照，不能当整体原子事务。

| state | 含义 |
|---|---|
| not_loaded | 当前进程未登记/加载驻留接收模块 |
| idle | 可以开始下一轮，入口已恢复 |
| starting/running/stopping | 正在建立、监听或停止会话 |
| draining | 入口已停用，但旧会话仍有已入门回调未结束；拒绝启动新会话 |
| fault | 入口恢复等引擎不变量失败；不继续启用 |

draining中的回调退出后，下一次events会自动回收旧会话并启动新会话。回调始终引用自己的会话配置，不能切换到下一轮的订阅/队列。

## 生命周期与隔离

引擎只执行一次MH_CreateHook。每轮MH_EnableHook/MH_DisableHook使用同一个原函数trampoline，停止时恢复原入口。DLL和trampoline到微信进程退出才释放，避免迟到跳转访问已卸载代码。

每轮单独分配不可变订阅和32项快照队列。回调在共享采样门内取得会话引用；stop独占关门、清除当前会话指针，再停用Hook并等待已入门引用退出。成功停止才释放该轮配置；超时则保留配置进入draining。队列未消费项清零并计入queued_discarded，不静默移交下一轮。

会话归属以进入采样门的时刻为界，不是网络到达时间。线程在进入采样门前停顿后恢复，可能被归入之后的会话；接口不提供消息唯一ID、历史/实时区分或业务级exactly-once保证。

duration=0依然绑定控制进程的PID及创建时间。控制进程退出/崩溃时，DLL检测其进程句柄并关闭该轮监听、恢复入口；不会因为控制端丢失而无限保留采样。文件I/O在工作线程，微信回调只执行有界快照和非阻塞入队。队列满或账号变化会显式停止并记录原因。

## 部署、升级和证据

首次构建或微信进程重启后首次部署：

```powershell
.\client_hook\scripts\build_runtime.ps1
```

随后直接使用events/status/stop，**不要为每次启停重新构建**。resident-PID-FILETIME.json登记固定构建路径和hash；即使runtime-current指向新构建，同一进程仍复用已登记的v2 DLL。需要升级原生引擎时，先停止接口并正常重启微信，再部署新构建，不尝试原地卸载。

v2配置结构为2496字节，公开状态块96字节；版本、PID/创建时间、SID、固定DLL完整hash、加载路径和入口代码均校验。与原断点接收器共用进程实例互斥，拒绝并发控制。

测试：

```powershell
python -B -X utf8 .\client_hook\tests\lifecycle\test_runtime.py
python -B -X utf8 .\client_hook\tests\lifecycle\test_resident.py
```

四项基础跨进程测试通过；同一自有host的7轮会话、9项检查通过，覆盖连续多条、配置/计数隔离、重复停止、并发启动拒绝、控制端崩溃、draining拒绝重启及安全回收。真实4.1.9.30另验证三轮启停，dll_reused=[false,true,true]，同一基址，会话号[1,2,3]，每轮入口恢复且最终idle。

本轮v2真机复用检查没有生成测试输入、捕获数为0；原入口的真实kind1群文本采集已在v1验证。v2尚无长期真机流量或全类型消息覆盖结论。详见LIVE_VALIDATION.md和work/client-hook/resident-*-results.json。

接口不提供发送功能。
