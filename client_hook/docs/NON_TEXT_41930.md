# 4.1.9.30 非文字与引用原生适配

固定 DLL SHA256：89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494。2026-10-24 静态扩展；真实收发验收另行记录。

## 服务器消息 ID 证据

既有构造器0x9da310把输入+0x50复制到消息记录+0x108。仅凭此复制不足以命名该字段。

本次在固定二进制中定位引用解析函数0xa3b780：它通过XML字段 `svrid` 读取64位值，存入引用结构 `param_1[4]`；随后分配0x2a8字节（16字节共享控制块＋0x298消息对象），在分配地址+0x10构造同一消息记录，并将 `param_1[4]` 写入 `plVar5[0x23]`。相对于消息对象，该位置为 `0x23*8-0x10 = 0x108`。

其配套序列化函数0xa3b600把同一引用结构的 `param_1[4]` 写回XML `svrid`。这建立了真实原生引用字段与记录+0x108的双向对应，不是根据字段宽度猜测。

证据为 `build/reference-native-analysis/decompile/180a3b780.c`、`180a3b600.c`，对应边界来自固定PE的.pdata，反汇编另存同名.asm。`.annotated.c`只把从固定二进制读出的字符串注释回伪代码；原始反编译仍保留。工具为项目构建目录内的Ghidra12.1.4和JDK21，未修改全局REA或Codex配置。

仅新 `weixin-4.1.9.30-record-v2` 采集配置的非零稳定值提供 message_id。旧配置及零值不提供消息ID，不将本地event_id代作服务器ID。

## 类型与载荷

0xa0a9c0对3/13/39分支，0xa0a7c0对43/44/62分支，0xa0a820对34分支，0xa0a790对47分支，0xa0b730对49分支；0xa0ae20包含42/66/67。相应独立类型对象的选择见0x9dfdb0。API还要求实际XML根/节点匹配img、videomsg、voicemsg、emoji或appmsg，匹配失败保留unknown，不触发回复。

49的appmsg.type=57解析成引用文字，附言和refermsg分离；type=6为文件，其他应用卡片作为card。kind1正文即使含同样XML也不会升级为可信引用。XML实体、DTD、歧义svrid及无效结构拒绝。

资源原始XML只保存在client_hook自己的native_resources表，不把CDN令牌、AES参数或本地路径传给模型。MCP只取得资源ID和安全元数据。

## 资源定位边界

当前账号目录从微信本机小型路径配置或注册表读取，只接受唯一匹配当前账号的目录。附件目录使用会话ID的MD5定位，并要求文件实际解码后MD5与原生XML中的内容标识匹配；不能靠同名文件或时间接近认定命中。

卡片返回原生XML，不自动请求其中的URL。已缓存且内容校验通过的资源可直接返回。未下载、无可靠标识或WXGF尚未适配时，返回明确的native_download_required/native_resource_locator_required等状态，不报告成功。

本次真机确认：图片原生md5对应hdlength所指原图，不能拿length/hevc_mid_size对应的中图代替。本机中图为WXGF，解密成功也不等于与原图MD5一致；实际返回的是完整MD5匹配的9551713字节PNG原图。视频文件名亦不等于原消息md5，使用原生长度收窄候选后要求完整内容MD5精确匹配。普通文件还需检查当前账号的msg/file统一目录。

语音通过client_hook内的只读媒体存储适配获取：media_*.db以当前账号目录为界，通过对应salt和逐页HMAC验证密钥；WAL只合并完整校验且已提交的帧，内存快照按Name2Id会话行及VoiceInfo.svr_id双条件精确查询。密钥与明文数据库不落盘，不向MCP传递数据库访问能力。样本SILK6874字节与原始事件长度一致，解码WAV320684字节。

表情包已通过原生cdnurl进行真实下载，严格限制已验证的vweixinf.tc.qq.com/110/20401/stodownload端点、不跟随重定向，长度与完整MD5校验后才提供内容。遵循原生URL的HTTP/HTTPS方式，HTTPS不禁用证书校验。图片、视频、文件未缓存时已接入原生CDN侧车，真实三类缓存缺失验收通过，详见下节。

## DAT 解密适配与验证边界

新增V1/V2分段解密，要求PKCS7严格校验及完整内容MD5匹配。XOR尾段逐个验证256个候选，不使用跨账号的默认常数。错误密钥、错误内容标识和截断数据均拒绝。分段协议参考上游源码 https://github.com/jackwener/wx-cli-again/blob/077a54cbfe679bda963cd038d8440422907fc797/src/attachment/decoder/v2.rs ；协议夹具通过，真实图片验证待完成。

固定二进制0x972ae0(param_2>=2)调用账号上下文0x303f80获取图片密钥；后者复制对象+0x278字符串，并与固定32字节掩码异或。账号上下文沿用既有账户身份链：全局0xa6634b8 -> root+0x68共享对象；root/object的vtable均严格验证。只读访问已验证字段，连读校验账号、共享指针、字符串及100ms时间界限，不执行未经验证的原生方法、不扫描整个内存、不输出或落盘密钥。仅在授权资源获取时调用，结果仍须原生MD5验证。

反编译原始证据位于build/reference-native-analysis/image-key-decompile及image-crypto-decompile。该静态证据不等于已经完成真机解密验收。

## 真机引用检查

2026-10-24用户重启后，新版runtime-20261024T055009Z-8a48fad4已加载到新微信进程，采集running。真实私聊原文NATIVE_REF_20261024_01的message_id为177100765339415681，随后原生引用附言解析为“请复述引用的验证码”，reference.message_id精确等于177100765339415681；两条事件发送者、账号及会话相同。MCP真实HTTP resolve_reference亦精确返回原消息事件53，引用事件为54。

校验脚本scripts/wechat_stack/validate_native_reference.py只读消息、不发送回复。成功证据已写入client_hook/state/mcp-live-validation/native-reference-validation.json。此验收仅证明真实文字的服务器ID、原生引用与MCP关联；尚不证明附件获取、内容理解及对端收件。

后续六类真实资源及模型、发送的本地验收已完成，证据见../../docs/REFERENCE_CONTEXT.md。本轮用户在微信打开了图片/语音/PDF/短视频，不能据此声称无人操作的主动下载已通过。

15:00:40(Asia/Shanghai)临时Frida观测期间发生frida-agent.dll/0xc0000409崩溃。观测仅留下ready，无有效下载ABI证据，脚本已无条件禁用。重新登录后使用已验证驻留DLL及只读资源适配，未重新附加Frida。

## 主动原生CDN下载（真实验收）

新增resource_sidecar仅通过LoadLibraryW驻留，不安装hook、不卸载回调模块。沿用既有原生owner queue调用CDN manager getter0x18f430、request构造0x18eab0、下载wrapper0x954c00；请求被move消费，future和三个上下文持有到完成，mode3仍在owner queue释放并等待stage6确认。固定4.1.9.30核心SHA、入口字节和侧车SHA，缺少构建或版本不符明确失败。

原生fileID/AES key只来自该事件原始XML；MCP和业务层不接触密钥或原生本地路径。图片有big用type1，否则mid用type2；视频type4、文件type5。输出必须位于当前账号msg/temp/client_hook_downloads/<64hex>/content，Windows反斜杠是原生路径白名单比较要求。

必须检查future的内层DownloadResult前两个状态字段；外层Result=0不等于成功。结果+0x10是错误说明字符串，不能用作MD5。缓存命中仍需完整消息MD5；同一事件精确CDN下载允许原生返回不同媒体表示，但仅在内层完成成功且源MD5匹配或完整传输进度等于实际字节数时接纳，另存SHA256，不伪称源MD5匹配。

真实新样本图片102525B、视频6201002B、PDF1284386B，均在调用前确认缓存缺失，完成真实MCP获取与Codex处理。短视频从未本地缓存；图片/PDF由微信自动缓存后经操作者工具精确隔离，完成后已恢复。没有手动打开或下载。证据state/native-download-validation/uncached-reference-chain.json。验证只涉及颜料盒私聊，不扩大为群聊附件实测。

## 出站原生标识增量（等待真机）

原生RPC v4返回发送对象+0x618的UUID，字段native_client_id、来源weixin41930_send_object_618。但普通文字走class2分支，不复制到记录+0x278，不能以此UUID声称已关联回显。后续真实seq159已通过精确输入对象的Task构造观测取得record+0x110排序序号，并在同账号同群原生数据库中精确查询到唯一出站文字、local_id及server_id；未比较正文或时间接近。47项回归和生命周期检查通过。新版采集加入排序序号、原生创建时间和启动前旧出站过滤；完整回显及历史过滤尚待重启微信后的真机验收。详见OUTGOING_IDENTITY_41930.md及state/group-reference-validation/send-identity-progress.json。
