"""独立本地生成；不创建发布账本、不连接站点或微信。"""
from datetime import datetime
import asyncio
import json
from pathlib import Path
import time

from .content import validate_edition, validate_packet
from .store import BEIJING, RunnerLock


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(10):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 9: raise
            time.sleep(.02)


async def generate_local(directory, backend, *, packet=None, sources=None, collector=None, image_budget=300, resume=False):
    # 每次显式执行使用独立目录；未知轮次保留原记录，不覆盖或自动重跑。
    directory = Path(directory).resolve()
    previous = None
    if resume:
        previous = json.loads((directory / 'generation.json').read_text(encoding='utf-8'))
        if previous['state'] == 'validated':
            return validate_edition(json.loads((directory / 'edition.json').read_text(encoding='utf-8')))
        if previous['state'] != 'imaging':
            raise ValueError('只允许恢复配图阶段；正文轮次未知不得重放')
        packet = json.loads((directory / 'packet.json').read_text(encoding='utf-8'))
    directory.mkdir(parents=True, exist_ok=resume)
    owner = RunnerLock(directory)
    record = {'state': 'collecting', 'started_at': time.time(), 'model_calls': 0,
              'website_writes': 0, 'mcp_writes': 0}
    if previous: record = previous
    def save():
        save_json(directory / 'generation.json', record)
    save()
    try:
        if packet is None:
            from .collectors import collect
            now = datetime.now(BEIJING)
            packet = await asyncio.to_thread(collector or collect, now.date().isoformat(), now.isoformat(), sources or {})
        save_json(directory / 'packet.json', packet)
        validate_packet(packet)
        await backend.start()
        from .images import prepare_images, validate_freshness
        record['state'] = 'imaging'; save()
        packet = await prepare_images(packet, backend, directory, budget=image_budget)
        save_json(directory / 'packet.imaged.json', packet)
        validate_freshness(packet)
        thread_id = await asyncio.wait_for(backend.create_report_thread(), 60)
        record.update(state='generating', thread_id=thread_id, model_calls=1)
        save()
        def observe(notification):
            # 仅记录计时、事件类型与字符数，不记录推理、正文或凭据。
            payload = notification.payload
            value = payload.model_dump(mode='json', by_alias=True) if hasattr(payload, 'model_dump') else payload
            value = value if isinstance(value, dict) else {}
            if value.get('turnId') and not record.get('turn_id'):
                record['turn_id'] = value['turnId']
                save()
            entry = {'elapsed': round(time.time() - record['started_at'], 3),
                     'method': notification.method, 'turn_id': value.get('turnId'),
                     'item_type': (value.get('item') or {}).get('type'),
                     'delta_chars': len(value.get('delta') or '')}
            with (directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(entry) + '\n')
        value, metadata = await backend.generate_report(thread_id, packet, on_event=observe)
        # 完整原始结果先保存；校验失败也能人工核查，不需要重新调用模型。
        save_json(directory / 'generated.json', value)
        record.update(state='generated', metadata=metadata, turn_id=metadata.get('turn_id'))
        save()
        edition = validate_edition(value, packet=packet, allow_simulation=False)
        save_json(directory / 'edition.json', edition)
        record.update(state='validated', date=edition['date'], content_hash=edition['content_hash'],
                      article_counts={key: len(items) for key, items in edition['articles'].items()})
        return edition
    except BaseException as error:
        record.update(state='generation_unknown' if record['state'] == 'generating' and not getattr(error, 'completed', False) else 'failed',
                      error_type=type(error).__name__, error=str(error),
                      turn_id=getattr(error, 'turn_id', record.get('turn_id')),
                      timeout_stage=getattr(error, 'timeout_stage', None),
                      interruption_confirmed=getattr(error, 'interruption_confirmed', None))
        raise
    finally:
        record['finished_at'] = time.time()
        save()
        try:
            await backend.close()
        finally:
            owner.close()
