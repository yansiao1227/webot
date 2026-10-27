# 4.1.9.30 出站身份：现有证据与未完成项

固定目标为 `D:/Wechat/Weixin/4.1.9.30/Weixin.dll`，SHA256：
`89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494`。
以下 RVA 仅适用于此目标，不能移用于其他版本。

## 已建立的事实

- TextInput `+0x618` 在原生 factory 中生成 UUID，发送提交前读取，真实发送已返回该值。
- `0x3552cf0` 构造普通文字 Record 时走 class2 分支，不把此 UUID 复制到 Record `+0x278`。不能声称普通文字已通过 UUID 回显关联。
- `0x16699e0` 在 `0x166a0e0` 调用 Task 构造器 `0x3552820`；Task `+0x08` 保留原 TextInput，`+0x18` 保留所构造的 Record。
- `0x166a188` 调用 `0x166d370`，把 Task 放入 manager `+0xb78` 所指状态的 `+0x90` 映射；键为 Task `+0x38` 的会话，值为 SharedRef<Task> 数组。
- Record `+0x110` 来自 `0x29329b0`。该函数以创建秒数乘 1000 起算，并依据已保存序号递增；这是排序序号候选，不能称为 local_id 或 server_id。
- 原生库中已采集图片/文件的精确 server_id 查询分别得到 local_id 219/225、sort_seq 1792841763000/1792841942000。这只能证明这些行的元数据，尚未证明发送诊断与数据库字段的完整映射。
- 实际发送结果 `+0x28` payload 为空；前一版结果 payload 诊断未取得 Record 身份。超时错误发送 UUID 在本会话原生 source/packed_info_data 精确搜索也没有匹配。

## 当前只读诊断

`native/outgoing_identity.h` 在既有 owner queue 完成 submit 后，有限遍历该映射，仅在会话匹配且 Task 保留的 TextInput 指针等于本次 submit 输入时返回 Record `+0x110`。它不安装 hook、不更改原生对象、不保留借用指针、不读取正文、不重复 submit。字段不可读、结构不符、目标不唯一或任务已经移除时返回 0。

侧车专用诊断占用既有 reserved 槽，profile 标记 `0x41930001`；Python 新结果名为 `native_identity_diagnostic_profile` 和 `native_record_110_candidate`。这些仍是诊断字段，不授予 MCP 任何跨层数据库访问权。构建后的侧车由 operator 开关 `CLIENT_HOOK_SEND_SIDECAR=1` 使用；该开关不开放给消息正文。

真实seq137已使用上述任务表诊断，返回0。这条请求正常完成且对端确认收到，但任务表诊断不能建立出站身份关联。

诊断阶段新增可选构建参数 `scripts/build_resource_sidecar.py --task-observer`，在既有 owner queue 的发送流程内安装一个固定 Task 构造入口观测。它以 TLS、线程和精确 TextInput 指针限定本次操作，原构造调用转发一次，返回后只复制 Record 标量；模块及观测存储驻留至微信进程退出。构造入口前16字节必须与固定目标匹配，普通资源构建不启用此观测。成功采到排序序号的诊断 profile 为 `0x41930002`。这与此前无新增 hook 的资源下载侧车构建不同，不能混称。该诊断已取得下述seq159真实身份映射，完整回显仍待验。

自有进程测试覆盖精确输入指针、其他输入、其他会话、重复目标、错误 vtable、链表不闭合和无效内存；构造观测还覆盖转发次数、参数及返回值、线程隔离、TLS 生命周期。44 项 client_hook 回归及两项生命周期检查通过，均不能代替微信真机验证。

新采集版本另复制 Record `+0x110` 和 `+0x134` 原始字段，以新 profile 显式标注。**真机已证伪 `+0x134` 是创建时间的推断**：出站为0，入站为19188等值，原始字段名raw_created_134是早期错误命名，API已停止解释其时间语义。API按精确原生排序键查询本账号本会话唯一原生行的create_time后，才使用真实创建秒数排除启动前旧出站记录；下限取启动时间的上整秒，就绪通知等到该边界之后。缺少可靠原生创建时间的出站事件不进入新增上下文，不用采集时间补造。新版DLL已加载，修正只需重载Python服务；历史过滤及回显仍待新一轮真机验证。

## 继续验收的条件

真实seq159已命中构造观测profile `0x41930002`，取得排序序号1792847037000；按同账号同测试群精确查得唯一行local_id=239、server_id=3432953162213164618、发送者为当前账号、类型为文字。原生提交一次，用户确认收到。证明了发送任务到原生服务器ID的映射；仍未证明新版采集事件与该任务的回显闭环。

该已验证观测现已并入标准live驻留引擎（构建pins的send_task_observer=true）；标准发送不再要求CLIENT_HOOK_SEND_SIDECAR。普通下载侧车已重新构建为不启用Task观测的版本。sender仅在构建pin与诊断profile同时匹配时输出可信native_sort_seq_source。client_hook在发送结果及新出站事件内按精确排序序号查询本账号原生消息库的元数据，要求唯一行、当前账号发送者、文字类型；不读正文、密钥不落盘；不可用或冲突时不伪造ID，也不改变原发送accepted状态。

当前三层服务已正常停止，标准live引擎已构建并通过47项原生回归及两项生命周期检查；MCP33项、业务43项通过。需重启微信加载新增字段和标准构造观测后继续真机验证。

后续身份查询只能由 client_hook 执行，并限定原发送账号、会话及精确原生键；匹配不唯一时拒绝，禁止采用正文、附件名或时间接近推断。

还须修正出站采集路径会观察到界面加载旧记录的问题，并把人工出站背景与自动回复回显准确区分。原生采到出站、提交 accepted、对端收件、服务器消息 ID 和任务回显精确关联是不同的验收结果。

静态导出证据目录：`build/reference-native-analysis/outgoing-identity-decompile`、`outgoing-result-decompile`、`outgoing-local-id-decompile`。自有测试：`native/test_outgoing_identity.c`、`tests/test_outgoing_identity.py`。
