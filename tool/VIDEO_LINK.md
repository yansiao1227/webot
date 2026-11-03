# 视频解析工具

读取MP4媒体地址或HTTP重定向至MP4的短链，下载视频、抽取画面并转写音轨；可指定SRT字幕地址优先读取字幕。提供 Python API 与 JSON 命令行接口，适用于视频概括、内容检索、素材整理，以及为模型准备分析材料。

## 支持的来源

| 来源 | 能力 |
| --- | --- |
| MP4直链、HTTP重定向至MP4的短链 | 下载视频、抽帧和转写；可指定SRT字幕地址优先读取字幕 |

## 快速使用

在包含`tool/`目录的源码根目录执行：

```powershell
python -m tool.content_reading read-video-link --url 'https://media.example/video.mp4' --output-dir './output/video'
python -m tool.content_reading read-video-link --url 'https://media.example/short'
```

指定字幕和本地转写模型：

```powershell
python -m tool.content_reading read-video-link --url 'https://media.example/short' --subtitle-url 'https://media.example/subtitles.srt'
python -m tool.content_reading read-video-link --url 'https://media.example/video.mp4' --model-dir './models/faster-whisper-small'
```

也可以从任意目录运行`python <源码根目录>/tool/read_content.py read-video-link --url <URL>`。

## Python API

```python
from tool.content_reading.api import invoke

response = invoke(
    'read-video-link',
    url='https://media.example/short',
    output_dir='./output/video',
)

if response['state'] == 'read':
    evidence = response['result']
    frames = evidence.get('images', [])
    transcript = evidence.get('transcript', '')
```

将`images`中的采样画面交给视觉模型，结合字幕或`transcript`及时间范围完成内容分析。

## 参数与输出

| 参数 | 用途 |
| --- | --- |
| `url` / `--url` | MP4媒体地址，或HTTP重定向至MP4的短链 |
| `subtitle_url` / `--subtitle-url` | MP4来源优先读取的公网SRT字幕地址 |
| `output_dir` / `--output-dir` | 下载文件和派生资源的输出父目录，每次处理使用单独子目录 |
| `model_dir` / `--model-dir` | 本地音轨转写模型目录 |

统一结果包含`tool`、`source`、`state`和`result`；`state`为`read`或`unavailable`，失败原因通过`reason`返回。CLI成功退出0，读取失败退出1。

| 结果字段 | 内容 |
| --- | --- |
| `method` | 实际读取方式 |
| `text` | 从指定SRT字幕文件提取的文本 |
| `images` | 本机采样帧路径 |
| `duration_seconds` | 视频时长 |
| `sampled_frame_seconds_approx` | 各采样帧的近似时间 |
| `transcript` / `transcription_segments` | 本地转写文本及分段时间戳 |
| `transcription_state` | 转写状态：`read`、`empty`或`unavailable` |
| `content_scope` | 实际读取范围和采样说明 |

## 环境准备

媒体依赖和本地转写模型：

```powershell
python -m pip install -r tool/requirements-media.txt
python tool/install_transcription_model.py
```

本机设置从`.env`读取；`CONTENT_MODEL_DIR`指定模型位置，`CONTENT_CACHE_DIR`指定默认缓存目录。显式API或CLI参数优先，缓存文件由调用方管理。

## 处理范围

视频下载上限64MiB、下载时限30秒，支持10分钟以内的视频；画面均匀采样至多8帧，音轨转写至多180秒。字幕文件上限2MiB，提取文本上限24000字。使用采样时间和转写范围判断材料覆盖情况，字幕和音轨转写分别标注来源。

媒体和字幕采用公网HTTP(S)地址，逐跳校验重定向，最多6次请求。输出中保留实际读取方式、范围与状态，供调用者判断处理结果。

## 批量资源API

调用方已取得媒体地址、字幕地址或时长时，可直接调用处理核心：

```python
from tool.content_reading.video import prepare

evidence = prepare(
    [{'url': 'https://media.example/video.mp4', 'size': 1000000}],
    subtitles=[{'url': 'https://media.example/subtitles.srt', 'language': 'zh-CN'}],
    duration=60,
    output_dir='./output/video',
)
```

`size`和`duration`为可选元数据，下载和媒体探测阶段还会检查实际大小和时长。提供已知时长时，字幕时间段也会与时长核对；省略时按10分钟的处理范围校验。
