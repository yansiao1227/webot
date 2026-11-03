# 视频与内容解析工具

提供视频链接解析、字幕读取、画面采样和本地音轨转写，同时支持图片、音频、文档和网页读取。通过 Python API 或 JSON CLI 输出提取内容、派生文件路径及读取范围，供内容分析、检索和素材整理使用。

视频解析的来源支持、调用示例和输出字段见[视频解析工具](VIDEO_LINK.md)。

## 调用方式

本机设置从`.env`加载，Python环境按所需能力安装依赖。

仓库根目录执行 `python -m tool.content_reading --help`，或在任意目录执行 `python <仓库绝对路径>/tool/read_content.py --help`。`--list-tools` 输出可用工具名及用途。

| 工具 | 输入 | 主要结果 |
| --- | --- | --- |
| `read-video-link` | `--url`、可选`--subtitle-url --output-dir --model-dir` | 下载MP4直链或HTTP重定向至MP4的短链，抽帧和转写；可优先读取指定SRT字幕 |
| `read-image-content` | `--url`、可选`--output-dir` | 下载并校验图片，返回供视觉查看的本机路径 |
| `inspect-media` | `--path`、`--kind image/sticker/audio/voice/video` | 图片路径、尺寸、视频采样时间、音轨转写 |
| `transcribe-audio` | `--path`、可选 `--model-dir` | CPU转写、分段时间戳、语言 |
| `read-document` | `--path`、可选 `--name` | 文本、PDF、DOCX、XLSX内容；扫描PDF采样图片 |
| `read-web-content` | `--url`、可选 `--browser --profile-dir` | 正文/可见文本、原始/最终链接、读取方式和范围 |
| `read-video-content` | `--url --profile-dir` | B站当前视频的播放器字幕和时间范围 |
| `read-platform-content` | `--url`、可选`--output-dir` | 通过小红书/B站MCP读取详情和评论；小红书视频优先字幕，无字幕则下载抽帧与转写；[需安装并登录](PLATFORM_MCP.md) |

```powershell
python -m tool.content_reading read-video-link --url 'https://media.example/short'
python -m tool.content_reading read-video-link --url 'https://media.example/video.mp4' --output-dir './output/video'
python -m tool.content_reading read-document --path './资料/说明.txt'
```

Python 调用：

```python
from tool.content_reading.api import invoke

result = invoke('read-video-link', url='https://media.example/video.mp4', output_dir='./output/video')
result = invoke('read-document', path='./资料/说明.txt')
```

统一结果含 `tool`、`state`、`source`；成功结果含 `result` 和 `content_scope`，失败含 `reason`，可能保留已取得的部分内容。CLI输出JSON；成功退出0，读取失败退出1。将返回的图片交给视觉模型，结合提取文本和时间范围完成内容分析。

## 环境和范围

媒体/文档依赖：`python -m pip install -r tool/requirements-media.txt`；浏览器依赖：`python -m pip install -r tool/requirements-browser.txt`，默认使用已安装的Edge。浏览器profile由`CONTENT_BROWSER_PROFILE_DIR`或显式`profile_dir`指定，channel由`CONTENT_BROWSER_CHANNEL`或CLI覆盖。音频模型位置通过`CONTENT_MODEL_DIR`或显式`model_dir`指定，首次使用前运行`python tool/install_transcription_model.py`准备权重。

公众号链接在已配置profile时自动使用浏览器并复用已有登录状态。浏览器默认使用实际安装版本的桌面UA，`CONTENT_WEB_USER_AGENT`可覆盖HTTP和浏览器UA。遇到站点验证时，在同一专用profile中完成验证后继续读取。

输出目录由调用者指定并为每份输入隔离；省略时在`CONTENT_CACHE_DIR`（默认`tool/state/cache/`）下创建独立目录，缓存由调用者管理。图片/视频/扫描PDF最多8张，视频最长10分钟，音轨转写最多180秒；文本上限100000字符、网页和字幕上限24000字符。具体截断、采样范围见返回值；视频音轨失败时仍保留已取得画面。

本地文件按实际内容识别类型：支持PDF、常见图片、DOCX、XLSX、文本，以及ffmpeg可解码的音视频。M4A等音频可进入本地转写，视频可进入画面采样和音轨转写；`detected_type`说明实际识别类型。

`public_only=True` / `--public-only` 用于仅允许公网来源的场景；默认关闭，支持自定义端口和内网地址。`urls.public_url` 和 `web.resolve_public` 仅在调用方选择公网策略时使用。

视频平台的MCP安装和登录方式见[平台接入说明](PLATFORM_MCP.md)。

## 语音识别

`tool.transcription`提供本机CPU语音识别：`transcribe_audio(path, output_dir=None, model_dir=None)`将ffmpeg支持的音频文件转换为16kHz单声道WAV，再返回转写文本、语言、置信度和分段时间戳；最多处理180秒。`transcribe_pcm`用于已规范化的PCM WAV。推理使用预先安装的本地模型。

独立CLI：

```powershell
python tool/transcribe_audio.py --path "./资料/录音.wav"
python tool/install_transcription_model.py --verify-only
```

首次安装模型时显式运行`python tool/install_transcription_model.py`，安装器使用固定版本和SHA256校验。模型目录由`CONTENT_MODEL_DIR`配置，默认`tool/state/models/faster-whisper-small/`；也可用`--model-dir`覆盖。转换文件仍使用`CONTENT_CACHE_DIR`或显式`--output-dir`。

## 验证

运行`python -B -X utf8 -m unittest discover -s tool/tests -v`，检查内容读取、环境配置和缓存输出。
