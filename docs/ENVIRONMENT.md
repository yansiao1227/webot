# 本机配置与内容缓存

仓库根目录的`.env`集中保存本机服务地址、运行程序和工具路径。新环境先复制`.env.example`为`.env`；`.env`已被忽略，不提交本机值。账号、白名单和消费者继续保存在原JSON配置，凭据继续使用现有DPAPI密文，没有搬入`.env`。MCP的账号/白名单位于`wechat_mcp/state/live/config.json`，Hook与MCP密文token位于同目录`credentials.json`。

Python入口通过`tool/settings.py`自动读取配置，不依赖当前工作目录或第三方dotenv包。优先级为：显式工具/CLI参数、已存在的进程环境变量、`.env`、代码默认值；Codex运行配置中的同名地址/路径由非空环境配置覆盖。`RE_WECHAT_ENV_FILE`可指定另一份文件，相对位置按仓库根目录解释。支持`KEY=value`、可选引号/`export`、`${VAR}`；反斜杠不做转义，不执行shell表达式。相对配置路径按仓库根目录解析。

## 已接入的设置

| 变量 | 用途和默认值 |
| --- | --- |
| `RE_WECHAT_HOST` | 服务监听、客户端连接、健康检查的回环主机，默认`127.0.0.1` |
| `RE_WECHAT_HOOK_PORT` / `RE_WECHAT_MCP_PORT` | 默认`30001` / `30002`，Hook/MCP URL由主机和端口统一构造 |
| `RE_WECHAT_PYTHON` | 管理器和PowerShell工具入口使用的Python，默认`wechat_codex/state/sdk-env/Scripts/python.exe` |
| `RE_WECHAT_CODEX_BIN` | 非空时注入SDK配置，留空沿用运行配置或SDK默认 |
| `RE_WECHAT_HOOK_STATE_DIR` / `RE_WECHAT_MCP_STATE_DIR` / `RE_WECHAT_CODEX_STATE_DIR` | 管理器使用的现有状态目录，默认值见`.env.example` |
| `CONTENT_CACHE_DIR` | 通用工具未显式指定输出位置时的缓存根，默认`tool/state/cache` |
| `CONTENT_MODEL_DIR` | 本机转写权重，默认`tool/state/models/faster-whisper-small` |
| `CONTENT_BROWSER_PROFILE_DIR` | 专用浏览器profile，可由CLI参数覆盖；示例值为`wechat_codex/state/live/browser-profile` |
| `CONTENT_BROWSER_CHANNEL` | 默认`msedge` |
| `RE_WECHAT_GCC` / `RE_WECHAT_OBJDUMP` / `RE_WECHAT_NM` | 构建/诊断程序路径，留空使用PATH中的同名命令 |
| `RE_WECHAT_WEIXIN_DLL` | 本机固定版本微信模块位置；构建时写入生成的profile，版本与散列检查保持 |

已覆盖管理器、Hook CLI/SDK/OpenAPI、MCP服务、Codex运行配置、网页读取入口、通用tool、工具调用说明和本机构建脚本。`RE_WECHAT_ROOT`由设置模块按源码位置自动注入，供运行时定位工具仓库。Windows工具可运行`./scripts/run_content.ps1 --list-tools`，它从`.env`选择Python。MCP stdio示例通过`./scripts/run_bridge.ps1`启动，配置它的客户端工作目录必须为仓库根目录；示例JSON不假定客户端支持环境字符串插值。

修改配置后需正常重启对应服务。改端口前先用旧配置停止原服务，再修改并启动；管理器按准确入口和状态目录识别进程。状态目录包含持久队列和身份，修改路径不会自动迁移已有数据。现有生成的build/state产物仍保留创建时的绝对路径，新构建按环境注入；此次未重建或替换原生DLL。测试/模拟器的随机回环地址、公网策略中的localhost拒绝名单、HTTP Host校验名单及历史证据中的地址不属于部署配置，不替换成可变变量。

## 文件放在哪里

| 来源 | 默认位置 | 清理方式 |
| --- | --- | --- |
| 通用tool的图片、抽帧、音频转换、PDF采样等输出 | `CONTENT_CACHE_DIR/content-reading-*/`或`content-image-*/` | 每次调用独立目录，由调用者用完清理；没有自动7天清理 |
| 显式`--output-dir`或API `output_dir` | 调用者指定目录 | 由调用者管理，覆盖缓存根配置 |
| 微信原生引用下载及预处理 | `wechat_codex/state/live/resources/<事件散列>/`，派生文件在`prepared/` | 服务每约30秒检查；已超过7天且无未完成任务保护的自有资源可清理 |
| 网页正文、字幕、评论/弹幕文字 | 工具JSON/API返回值，默认不另存文本缓存文件 | 微信侧纳入业务任务/结果；业务记录位于`wechat_codex/state/live/agent.sqlite3` |
| 浏览器登录状态 | `CONTENT_BROWSER_PROFILE_DIR` | 持久profile，不随结果缓存清理 |
| 转写模型权重 | `CONTENT_MODEL_DIR` | 持久模型，不随结果缓存清理 |

上述微信路径以默认`RE_WECHAT_CODEX_STATE_DIR`为例。工具文件默认输出策略变更前，未指定输出目录会创建系统临时目录下的`content-reading-*`或`content-image-*`，没有自动清理。2026-10-27按用户要求，将旧系统临时目录中的2个`content-image-*`目录（4个文件，共564994字节）复制到工作区`tool/state/cache/`，逐文件校验SHA256和大小后删除原目录；旧`content-reading-*`/`content-image-*`目录已清空。迁移清单为`tool/state/cache/migration-20261027.json`，缓存与清单均由`.gitignore`排除。没有删除系统临时目录本身或其他程序的文件。
