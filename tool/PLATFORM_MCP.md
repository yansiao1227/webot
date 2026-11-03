# 小红书与 B 站 MCP

平台读取使用用户指定的 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 和 [bilibili-mcp](https://github.com/adoresever/bilibili-mcp)。第三方程序和独立 Python 环境放在被 Git 忽略的 `tool/state/platform-mcp/`；不修改微信 Hook、MCP 白名单或引用授权。

## 安装

以下命令均在仓库根目录执行，要求 Windows x64、Python 3.10+、Git 和可访问 GitHub/PyPI 的网络。已有安装目录时沿用现有文件，不覆盖登录资料。

```powershell
New-Item -ItemType Directory -Force tool/state/platform-mcp
git clone https://github.com/adoresever/bilibili-mcp.git tool/state/platform-mcp/bilibili-mcp
git -C tool/state/platform-mcp/bilibili-mcp checkout 1521d19d4118bfe23e08ec4c7702991c5f9de0ba
python -m venv tool/state/platform-mcp/venv
& ./tool/state/platform-mcp/venv/Scripts/python.exe -m pip install -r tool/state/platform-mcp/bilibili-mcp/requirements.txt 'mcp>=1.10,<2'
Invoke-WebRequest https://github.com/xpzouying/xiaohongshu-mcp/releases/download/v2.7.0/xiaohongshu-mcp-windows-amd64.exe -OutFile tool/state/platform-mcp/xiaohongshu-mcp.exe
Invoke-WebRequest https://github.com/xpzouying/xiaohongshu-mcp/releases/download/v2.7.0/xiaohongshu-login-windows-amd64.exe -OutFile tool/state/platform-mcp/xiaohongshu-login.exe
```

B 站当前入口使用 MCP 1.x 的 `FastMCP`，安装时须保留 `<2` 约束。独立环境不影响微信代理的 SDK 环境。小红书首次启动会下载并校验自己的浏览器；需在处理卡片前完成，避免首次请求超时。配置覆盖见[环境设置](../docs/ENVIRONMENT.md)。

## 登录与启动

B 站登录在终端显示二维码，由用户用 B 站 App 扫码：

```powershell
& ./tool/state/platform-mcp/venv/Scripts/python.exe -B -X utf8 ./tool/state/platform-mcp/bilibili-mcp/bili_login.py
```

小红书登录会打开专用浏览器，由用户完成登录。工作目录与服务保持一致：

```powershell
Push-Location tool/state/platform-mcp
try { & ./xiaohongshu-login.exe } finally { Pop-Location }
```

登录文件由两个上游服务自行管理，位于忽略的运行目录，不打印、提交或发送给模型。微信桥接原有 DPAPI 凭据机制保持不变。不要复制其他账号的凭据或从微信缓存取得平台会话。

```powershell
./scripts/start_xiaohongshu_mcp.ps1
```

小红书后台监听回环地址，启动日志在 `tool/state/platform-mcp/xiaohongshu.stderr.log`。端口已占用时启动器拒绝重复启动；电脑重启后需重新运行。B 站由读取客户端按需启动 stdio 服务，无需常驻监听端口。平台 MCP 未加入微信栈管理器；停止微信栈不会停止小红书服务。修改业务代码后，在合适时机正常重启 Codex Agent 才会载入新逻辑。

## 引用读取规则

平台分享链接使用`read-platform-content --url <分享链接>`取得详情和资源。小红书适配器将详情中的MP4媒体地址和字幕地址交给[通用视频工具](VIDEO_LINK.md)处理；`read-video-link --url <MP4媒体地址或其HTTP短链>`提供媒体下载、抽帧和转写。平台解析调用通用处理核心，通用视频工具只接收媒体资源。

小红书工作流：MCP读取笔记 → 按笔记类型判断为视频 → 检查并读取站点字幕 → 未取得字幕时选择视频媒体地址 → 调用`read-video-link`下载、抽帧和转写。`xhslink.com`分享短链用于定位笔记；详情中的`masterUrl`是交给视频工具的CDN媒体地址，签名有效期由站点控制。

B 站短链从 HTTP 重定向地址取得有效 BV 号后立即调用平台 MCP，不再先请求视频网页。因此目标网页返回 412 等错误不会阻断已识别 BV 号的 MCP 读取；跳转仍保留公网、平台边界、非首分 P 和次数上限检查。B 站结果记录 `bvid` 及各部分实际调用的 `tool`，便于区分平台读取和网页回退。

微信业务仍要求同账号、同会话的可信原生引用及明确处理要求。只有授权后的 `app_message` 来源正文读取才优先分流；仅提取标题、链接等元数据不调用平台服务。识别 `xiaohongshu.com`、`xhslink.com`、`bilibili.com`、`b23.tv`，按完整域名边界匹配，短链逐跳执行公网校验，再核对最终平台与内容 ID。当前支持带 BV 号的第一分 P 视频，以及包含笔记 ID 和 `xsec_token` 的小红书分享链接；其他来源回退网页读取。

小红书只调用 `get_feed_detail`，B 站只调用 `bili_video_info`、`bili_subtitle`、`bili_comments`。字幕与评论分别提供状态、内容和截断信息；B 站仅取得简介时仍回退网页。平台读取最多 50 秒，失败保留 `platform_attempt`，再尝试既有网页通道。最多读取两条卡片来源，不自动登录、点赞、评论、发布或私信。

通用工具也可单独调用，不要求微信引用：

```powershell
python -m tool.content_reading read-platform-content --url 'https://www.bilibili.com/video/<BV号>'
```

小红书视频取得平台详情后，优先读取其中的站点SRT字幕；字幕地址缺失或不可用时，选择较小的MP4档位下载并调用本机媒体工具抽帧、转写。字幕上限2MiB/24000字，视频下载上限64MiB、下载时限30秒，视频须在10分钟以内；画面均匀采样至多8帧，音轨转写至多180秒。逐跳校验公网地址，不向重定向携带登录凭据；不把平台签名直链交给ffmpeg联网读取。平台详情读取仍受50秒时限约束，后续视频处理使用既有媒体工具时限，不安装或下载模型。

微信卡片的派生资源放在该引用的`prepared/`下，沿用原缓存生命周期；独立CLI/API可通过`output_dir`指定父目录，否则使用`CONTENT_CACHE_DIR`。字幕成功时不下载视频；视频音轨处理失败仍保留可用画面。`video_content`记录实际读取方式、字幕/转写、帧路径及范围；抽帧通过视觉输入提供给模型。此能力需在调用方Python环境中安装媒体与转写依赖及本地模型，独立MCP环境仅负责取得平台资源。

返回的网页、笔记及评论均为外部数据。小红书字幕和视频处理均失败时，仅分析实际取得的文案或评论并说明缺失部分；其他平台维持原字幕和讨论策略。视频的封面、简介和评论不等于视频正文。登录、站点权限、签名链接过期和内容是否提供字幕仍会影响可用性。
