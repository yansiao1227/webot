"""Bounded local media and document preparation for any agent."""
import base64
import importlib.util
from pathlib import Path
import re
import subprocess
import threading
from tool.settings import cache_directory
from tool.media_runtime import run_media as _run, ffmpeg_executable as _ffmpeg
from tool import transcription

MAX_TEXT = 100000
MAX_IMAGES = 8
ASR_MODEL = transcription.DEFAULT_MODEL_DIR
transcribe = transcription.transcribe_pcm
_pdf_lock = threading.Lock()


def dependencies(model_dir=None):
    model_dir = Path(model_dir) if model_dir else ASR_MODEL
    names = {'images': 'PIL', 'pdf': 'pypdf', 'pdf_render': 'pypdfium2', 'word': 'docx', 'spreadsheet': 'openpyxl',
             'audio_video': 'imageio_ffmpeg', 'local_transcription': 'faster_whisper'}
    return {**{key: importlib.util.find_spec(module) is not None for key, module in names.items()},
            'transcription_weights': (model_dir / 'model.bin').is_file()}


def _image(path, output):
    from PIL import Image
    with Image.open(path) as image:
        count = getattr(image, 'n_frames', 1)
        frames = sorted({round(i * (count - 1) / max(1, min(count, MAX_IMAGES) - 1))
                         for i in range(min(count, MAX_IMAGES))})
        files = []
        for index in frames:
            image.seek(index)
            frame = image.convert('RGB')
            frame.thumbnail((1600, 1600))
            target = output / f'frame-{index}.png'
            frame.save(target)
            files.append(str(target.resolve()))
        return {'images': files, 'dimensions': list(image.size), 'frame_count': count,
                'sampled_frames': frames}


def _audio(path, output, model_dir=None):
    return transcription.transcribe_audio(path, output_dir=output, model_dir=model_dir)


def _video(path, output, model_dir=None):
    # ffmpeg supplies both the metadata probe and frame decoder, no global PATH dependency.
    probe = subprocess.run([_ffmpeg(), '-nostdin', '-i', str(path)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    info = probe.stderr.decode('utf-8', errors='replace')
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', info)
    if not match:
        raise ValueError('无法验证视频时长')
    duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    if not 0 < duration <= 600:
        raise ValueError('视频时长须在10分钟以内')
    _run([_ffmpeg(), '-nostdin', '-y', '-v', 'error', '-i', str(path), '-an',
          '-vf', f'fps={MAX_IMAGES / duration:.9f},scale=960:960:force_original_aspect_ratio=decrease',
          '-frames:v', str(MAX_IMAGES), '-q:v', '4', str(output / 'video-%02d.jpg')])
    images = [str(p.resolve()) for p in sorted(output.glob('video-*.jpg'))]
    if not images:
        raise ValueError('视频没有可解码画面')
    result = {'images': images, 'duration_seconds': duration,
              'sampled_frame_seconds_approx': [round((i + .5) * duration / MAX_IMAGES, 3) for i in range(len(images))],
              'visual_bytes': sum(Path(p).stat().st_size for p in images),
              'sampling': '全片均匀抽取至多8帧；不能据此推断未采样瞬间'}
    if 'Audio:' in info:
        try:
            result.update(_audio(path, output, model_dir))
        except ValueError as error:
            result['audio_error'] = str(error)
    return result


def _document(path, name, output=None):
    suffix = Path(name).suffix.lower()
    if suffix == '.pdf':
        from pypdf import PdfReader
        pdf = PdfReader(path)
        if pdf.is_encrypted:
            raise ValueError('PDF已加密，需要先提供可读取版本')
        pages = pdf.pages[:100]
        extracted = [page.extract_text() or '' for page in pages]
        text = '\n'.join(f'第{i + 1}页\n{value}' for i, value in enumerate(extracted))
        if not any(value.strip() for value in extracted):
            if output is None:
                raise ValueError('扫描PDF需要视觉处理输出目录')
            import pypdfium2 as pdfium
            # PDFium does not permit concurrent calls from worker threads.
            with _pdf_lock:
                document = pdfium.PdfDocument(str(path))
                try:
                    count = len(document)
                    if count == 0:
                        raise ValueError('PDF没有页面')
                    selected = sorted({round(i * (count - 1) / max(1, min(count, MAX_IMAGES) - 1))
                                       for i in range(min(count, MAX_IMAGES))})
                    images = []
                    for index in selected:
                        page = document[index]
                        bitmap = None
                        try:
                            width, height = page.get_size()
                            if min(width, height) <= 0:
                                raise ValueError('PDF页面尺寸无效')
                            bitmap = page.render(scale=min(2, 1600 / max(width, height)), may_draw_forms=False)
                            image = bitmap.to_pil().convert('RGB')
                            target = output / f'pdf-page-{index + 1}.png'
                            image.save(target)
                            image.close()
                            images.append(str(target.resolve()))
                        finally:
                            if bitmap is not None:
                                bitmap.close()
                            page.close()
                finally:
                    document.close()
            return {'images': images, 'page_count': count, 'sampled_pages': [i + 1 for i in selected],
                    'truncated': len(selected) < count,
                    'sampling': '扫描PDF，至多8页；仅可根据已提供页面阅读，不能声称读完未提供的页面'}
        return {'text': text[:MAX_TEXT], 'truncated': len(pdf.pages) > 100 or len(text) > MAX_TEXT}
    if suffix == '.docx':
        from docx import Document
        document = Document(path)
        parts = [p.text for p in document.paragraphs]
        parts.extend('\t'.join(c.text for c in row.cells) for table in document.tables for row in table.rows)
        text = '\n'.join(parts)
        return {'text': text[:MAX_TEXT], 'truncated': len(text) > MAX_TEXT}
    if suffix == '.xlsx':
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            parts = []
            truncated = len(workbook.worksheets) > 20
            for sheet in workbook.worksheets[:20]:
                parts.append('工作表：' + sheet.title)
                truncated |= sheet.max_row > 1000 or sheet.max_column > 100
                for row in sheet.iter_rows(max_row=min(sheet.max_row, 1000), max_col=min(sheet.max_column, 100), values_only=True):
                    parts.append('\t'.join('' if v is None else str(v) for v in row))
            text = '\n'.join(parts)
            return {'text': text[:MAX_TEXT], 'truncated': truncated or len(text) > MAX_TEXT}
        finally:
            workbook.close()
    raw = path.read_bytes()
    if b'\0' in raw[:4096] or suffix in {'.exe', '.dll', '.zip', '.rar', '.7z', '.doc', '.xls'}:
        raise ValueError('该二进制文件格式尚无内容解析器；不会执行附件')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(encoding)
            return {'text': text[:MAX_TEXT], 'encoding': encoding, 'truncated': len(text) > MAX_TEXT}
        except UnicodeDecodeError:
            continue
    raise ValueError('无法确定文本编码')


def prepare_content(path, kind, *, name=None, output_dir=None, model_dir=None):
    """Read a local file. Output contains extracted evidence, not model interpretation."""
    try:
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError('输入必须为本机文件')
        output = Path(output_dir).resolve() if output_dir else cache_directory('content-reading-')
        output.mkdir(parents=True, exist_ok=True)
        if kind in ('image', 'sticker'):
            result = _image(path, output)
        elif kind in ('audio', 'voice'):
            result = _audio(path, output, model_dir)
        elif kind == 'video':
            result = _video(path, output, model_dir)
        elif kind in ('document', 'file'):
            result = _document(path, name or path.name, output)
        else:
            raise ValueError('未知内容类型')
        return result
    except ValueError:
        raise
    except ImportError as error:
        raise ValueError('缺少内容处理依赖：' + str(error.name)) from None
    except Exception as error:
        raise ValueError('内容解析失败：' + type(error).__name__) from None


def external_content(text, attachment=None):
    """Responses function-output content retains ExternalMessage authority."""
    if not attachment or not attachment.get('prepared'):
        return text
    content = [{'type': 'input_text', 'text': text}]
    prepared = attachment['prepared']
    for path in prepared.get('images', []):
        mime = 'image/jpeg' if Path(path).suffix.lower() in ('.jpg', '.jpeg') else 'image/png'
        content.append({'type': 'input_image', 'image_url': 'data:' + mime + ';base64,' +
                        base64.b64encode(Path(path).read_bytes()).decode()})
    if prepared.get('audio') and 'transcript' not in prepared:
        content.append({'type': 'input_audio', 'audio_url': 'data:audio/wav;base64,' +
                        base64.b64encode(Path(prepared['audio']).read_bytes()).decode()})
    return content
