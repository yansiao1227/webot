# 通用内容工具

用于读取图片、音频、视频、文档和网页内容的 Python 工具，提供 API 和 JSON CLI。支持本机文件及本机、内网、公网 HTTP(S) 来源，输出提取内容、派生文件路径和读取范围，供调用者进一步分析。

## 调用方式

本机设置自动从仓库`.env`加载。Windows推荐`./scripts/run_content.ps1 ...`，启动器按配置选择Python运行环境。

仓库根目录执行 `python -m tool.content_reading --help`，或在任意目录执行 `python <仓库绝对路径>/tool/read_content.py --help`。`--list-tools` 输出可用工具名及用途。

| 工具 | 输入 | 主要结果 |
| --- | --- | --- |
| `read-image-content` | `--url`、可选`--output-dir` | 下载并校验图片，返回供视觉查看的本机路径 |
| `inspect-media` | `--path`、`--kind image/sticker/audio/voice/video` | 图片路径、尺寸、视频采样时间、音轨转写 |
| `transcribe-audio` | `--path`、可选 `--model-dir` | CPU转写、分段时间戳、语言 |
| `read-document` | `--path`、可选 `--name` | 文本、PDF、DOCX、XLSX内容；扫描PDF采样图片 |
| `read-web-content` | `--url`、可选 `--browser --profile-dir` | 正文/可见文本、原始/最终链接、读取方式和范围 |
| `read-video-content` | `--url --profile-dir` | B站当前视频的播放器字幕和时间范围；没有字幕时明确失败 |

```powershell
python -m tool.content_reading read-document --path "D:/资料/说明.txt" --output-dir "D:/输出/说明"
python -m tool.content_reading read-web-content --url "http://127.0.0.1:8080/"
```

Python 调用：

```python
from tool.content_reading.api import invoke

result = invoke('read-document', path='D:/资料/说明.txt', output_dir='D:/输出/说明')
result = invoke('read-web-content', url='http://localhost:8080/')
```

统一结果含 `tool`、`state`、`source`；成功结果含 `result` 和 `content_scope`，失败含 `reason`，可能保留已取得的部分内容。CLI输出JSON；成功退出0，读取失败退出1。工具本身不做图片语义理解，Agent需查看返回图片并分析提取内容。

## 环境和范围

媒体/文档依赖：`python -m pip install -r tool/requirements-media.txt`；浏览器依赖：`python -m pip install -r tool/requirements-browser.txt`，默认使用已安装的Edge。浏览器profile由`CONTENT_BROWSER_PROFILE_DIR`或显式`profile_dir`指定，channel由`CONTENT_BROWSER_CHANNEL`或CLI覆盖。音频模型位置通过`CONTENT_MODEL_DIR`或显式`model_dir`指定，处理请求时不下载权重。

公众号链接在已配置profile时自动使用浏览器，保留完整原始链接并复用已有登录状态，避免先以无Cookie的HTTP请求读取。浏览器默认从实际安装版本取得桌面UA，移除`HeadlessChrome`标识；`CONTENT_WEB_USER_AGENT`可覆盖HTTP和浏览器UA。HTTP默认使用Windows已安装Edge/Chrome版本的桌面UA，无可用版本时回退`Mozilla/5.0`。这不能保证站点免验证；识别到微信公众号验证页时返回`state=unavailable`、`error=verification_required`，不将其当作文章正文。请在同一专用profile中人工完成验证后再读取，工具不自动尝试验证码。

输出目录由调用者指定并为每份输入隔离；省略时在`CONTENT_CACHE_DIR`（默认`tool/state/cache/`）下创建独立目录，调用者负责用完清理。工具不自动按保留期清理。图片/视频/扫描PDF最多8张，视频最长10分钟，音轨转写最多180秒；文本上限100000字符、网页和字幕上限24000字符。具体截断、采样范围见返回值；视频音轨失败时仍保留已取得画面。

文件附件（`prepare_content` 的 `file`/`document`，以及 `read-document`）按实际内容分流：文件头识别PDF和常见图片，ZIP内部结构识别DOCX/XLSX，媒体文件通过ffmpeg的实际音视频流选择转写或视频采样。M4A等音频作为文件发送也可转写；带封面图片的音频仍按音频处理。文件改名不影响已识别格式，返回`detected_type`说明实际处理类型。未识别内容尝试读取文本；APK、普通压缩包和其他不支持的二进制文件明确报错，不执行附件。

`public_only=True` / `--public-only` 用于仅允许公网来源的场景；默认关闭，支持自定义端口和内网地址。`urls.public_url` 和 `web.resolve_public` 仅在调用方选择公网策略时使用。

工具通过 Python API 与 CLI 调用；需要 MCP 接入时，由调用方封装服务。

## 语音识别

`tool.transcription`提供本机CPU语音识别：`transcribe_audio(path, output_dir=None, model_dir=None)`将ffmpeg支持的音频文件转换为16kHz单声道WAV，再返回转写文本、语言、置信度和分段时间戳；最多处理180秒。`transcribe_pcm`用于已规范化的PCM WAV。推理只读取本机模型，不自动下载权重。

独立CLI：

```powershell
python tool/transcribe_audio.py --path "./资料/录音.wav"
python tool/install_transcription_model.py --verify-only
```

首次安装模型时显式运行`python tool/install_transcription_model.py`，安装器使用固定版本和SHA256校验。模型目录由`CONTENT_MODEL_DIR`配置，默认`tool/state/models/faster-whisper-small/`；也可用`--model-dir`覆盖。转换文件仍使用`CONTENT_CACHE_DIR`或显式`--output-dir`。

## 验证

运行`python -B -X utf8 -m unittest discover -s tool/tests -v`，检查内容读取、环境配置和缓存输出。
