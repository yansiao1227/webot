"""微信附件封套与缓存生命周期管理，解析实现位于 tool/。"""
import json
import os
from pathlib import Path
from tool.content_reading import media as content_media
from .cards import extract
from . import web_content

ASR_MODEL = content_media.ASR_MODEL
external_content = content_media.external_content


def dependencies():
    return content_media.dependencies(model_dir=ASR_MODEL)


def prepare(attachment, source_options=None):
    try:
        path = Path(attachment['path']).resolve(strict=True)
        if attachment['message_type'] == 'card':
            card = extract(path.read_text(encoding='utf-8-sig'))
            content = {**card, 'text': json.dumps(card['card_metadata'], ensure_ascii=False)}
            if source_options and source_options.get('read_source'):
                content['source_contents'] = web_content.retrieve(card, source_options)
        else:
            content = content_media.prepare_content(path, attachment['message_type'],
                name=attachment.get('name'), model_dir=ASR_MODEL, output_dir=path.parent / 'prepared')
        captured = path.stat().st_mtime
        output = path.parent / 'prepared'
        if output.exists():
            for file in output.iterdir():
                if file.is_file() and not file.is_symlink():
                    os.utime(file, (captured, captured))
        return {**attachment, 'prepared': content}
    except ValueError:
        raise
    except Exception as error:
        raise ValueError('附件解析失败：' + type(error).__name__) from None


def __getattr__(name):
    # 兼容已有本地诊断入口，实现仍放在 tool/。
    return getattr(content_media, name)
