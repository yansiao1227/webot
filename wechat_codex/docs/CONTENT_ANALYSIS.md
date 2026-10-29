# 短视频与网页来源读取

2026-10-27：content-reading Skill已移除，工具调用及证据范围规则由`codex_backend.py`直接提供。音频经`tool.transcription.transcribe_audio`执行转换和CPU识别，模型安装、校验及权重目录统一在tool下；微信资源获取和SILK原生格式解码仍由原层处理。语音tool不依赖微信业务模块。

2026-10-26后续调整：链接视频没有字幕时跳过视频本体，允许分析实际读到的评论、弹幕并给出大概结论。B站浏览器读取增加独立discussion_content，包含当前已加载的评论/弹幕样本，每类最多12000字，支持评论Shadow DOM，排除推荐区；模型须区分字幕与观众讨论，不把评论说法当作视频事实或全体共识。修复字幕读取器误引用不存在的.web_content模块。93项回归与真实Edge合成页面提取验证通过；真实站点样本及新微信请求待验收。

2026-10-26链接图片与字幕策略：微信运行时明确指向content-reading Skill及有依赖的Python入口。普通图片直链可由fetch自动下载，或显式使用`read-image-content --url <URL> --public-only`，校验实际图片、20MiB上限并输出本机视觉路径；必须实际查看后才能描述。网页读取返回最多8个`image_urls`候选（包括懒加载图片），由模型选择相关正文图片下载查看。普通HTTP链接无需微信附件原生引用。链接视频优先字幕，没有字幕或读取失败时停止该视频分析，不再下载整片或尝试转写，也不使用简介、封面和评论替代。此策略不改变已授权微信原生短视频附件的抽帧/转写。12项通用工具测试、91项微信侧回归通过；新增图片测试使用本机HTTP真实响应，未发送微信测试消息，实际新微信请求仍待验收。

2026-10-26新增：B站视频卡片自动读取当前登录播放器实际请求的WBI字幕响应，核对aid/cid与当前视频，并校验字幕时间不超过视频时长。优先中文，自动字幕会明确标注；字幕文本含时间点，最多24000字，超过则标记截断。当前范围为10分钟内视频。读取失败时明确失败，不再用标题或简介代替视频分析。页面中的推荐列表、评论不作为视频内容。

已用真实原卡片来源验证：176段中文自动字幕，0.04—288.48秒，未截断，真实模型按字幕给出了新干员、特别招募、卫戍协议和系统更新等分段概括。报告`state/bilibili-validation-1792988400/result.json`，本轮未发送微信。67项回归通过；新引用的微信完整闭环待确认。用户已确认上一轮原生短视频分析正确且两条均收件。

Fake-IP公网核验现优先使用阿里DoH（dns.alidns.com），Cloudflare（1.1.1.1）备用；只缓存已核验公网答案60秒，所有私网答案继续拒绝。待访问域名会发送给相应解析器。此调整处理了本机直连固定DoH地址失败导致B站CDN资源被误拦的问题。

2026-10-25：本轮新增来源读取器，已通过59项离线回归、真实ffmpeg解码、真实Edge/Playwright与fetch公网读取，以及真实SDK合成内容验证。微信真实新引用/对端语义确认仍待验收；旧207/211失败事件不重跑，不能据此把历史验收改为通过。

原生短视频仍经MCP精确引用获取。全片至多8帧，改为960像素以内JPEG，附近似采样时间与图像字节数，降低多模态输入体积；音轨最多180秒，本地转写。音轨解析失败时保留画面并标明错误。已有180秒模型预算未增加。过去群视频超时的根因尚未证实，优化输入和合成样本通过不代表已定位原故障。

卡片首次模型规划新增`read_source`：分析正文时为true；只提取标题、链接等元数据时为false。仍须原生引用＋明确处理要求才读取附件。app-message优先Playwright，用持久profile加载当前页面，读取`#js_content`、`article`、`main`或body的可见文本。成功结果包含来源、最终地址、方式、文本、截断标记和范围；失败保留卡片字段并明确`unavailable`。正文上限24000字，fetch原始响应上限2MiB。

普通文字链接可由模型调用`scripts/read_web.py <URL>`，默认fetch；公众号链接在已配置profile时自动复用浏览器登录状态，动态页面加`--browser`，B站/b23链接自动使用浏览器以读取字幕。底层实现现位于根目录[通用工具](../../tool/README.md)，微信兼容层显式指定`public_only=True`：fetch验证每次重定向，固定已验证的公网连接地址，保留HTTPS主机证书校验。Playwright校验请求目标并禁用Service Worker。对本机代理的`198.18.0.0/15` Fake-IP解析，使用上述DoH核验真实公网A记录；不能核验时失败，不把其他私网地址放行。此公网策略只适用于微信来源读取链路，通用工具默认支持本机和内网链接。

可见页面可能只是登录提示、验证页或视频简介；不能把页面文本等同完整文章、字幕、视频画面或音轨。视频号的媒体候选仍不自动下载或绕过访问限制。

安装浏览器依赖：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -m pip install -r .\wechat_codex\requirements-browser.txt
```

默认使用已安装的Microsoft Edge，profile为`wechat_codex/state/live/browser-profile`。可在本机`config.json`设置绝对路径`browser_profile_dir`和`browser_channel`（msedge/chrome/chromium）。chromium需另执行`python -m playwright install chromium`。

首次登录或登录过期时，在无卡片分析任务期间执行：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe .\wechat_codex\scripts\open_browser_profile.py
```

在打开的浏览器中访问需要的站点并登录，完成后关闭窗口。后台读取复用该profile，任务之间串行使用它。不要同时运行两个使用同一profile的浏览器。profile包含登录状态，独立于七天附件缓存保留，不复制到文档、模型输入或测试报告。已有Playwright专用profile可直接配置；[Playwright官方文档](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)不支持直接自动化Chrome日常主User Data目录，应使用专用目录。

公众号读取使用实际安装浏览器版本的桌面UA，可通过`CONTENT_WEB_USER_AGENT`覆盖。登录profile原地复用，不清除Cookie；仍遇到微信验证页时返回`unavailable`及`verification_required`，需在上述同一profile中手动完成验证，无法保证仅修改UA即可通过站点验证。

不调用微信的真实内容验证：

```powershell
.\wechat_codex\state\sdk-env\Scripts\python.exe -B -X utf8 .\wechat_codex\validation\validate_content_analysis.py
```

该脚本自制红底数字视频、读取公开示例页，并调用真实SDK，不取得真实微信附件，不连接微信传输。报告位于`state/content-validation-*/result.json`。本轮视频模型6.832秒、页面概括5.714秒，均无工具调用，结果核对通过；端到端还包含解码、浏览器读取等开销。
