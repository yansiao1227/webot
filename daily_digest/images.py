"""有界、可恢复的候选新闻配图；模型只负责搜索和视觉判定。"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from pathlib import Path
import time

from tool.news_images import cache_image
from .content import canonical, validate_image, validate_packet, parse_time, check_shape


def fallback(category):
    return {'kind': 'illustration', 'url': '', 'caption': '分类主题插画，非新闻现场',
            'credit': '本地分类插画', 'fallback_key': 'technology' if category == 'ai' else 'society-city'}


def schema(fields):
    item = {'type': 'object', 'properties': {k: {'type': 'string'} for k in fields},
            'required': list(fields), 'additionalProperties': False}
    return {'type': 'object', 'properties': {'items': {'type': 'array', 'items': item}},
            'required': ['items'], 'additionalProperties': False}


async def prepare_images(packet, backend, directory, *, budget=300, deadline=None, now=time.time, downloader=cache_image, search=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / 'images.json'
    fingerprint = sha256(canonical(packet).encode()).hexdigest()
    state = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {
        'fingerprint': fingerprint, 'results': {}, 'calls': [], 'errors': []}
    if state['fingerprint'] != fingerprint:
        raise ValueError('image_packet_mismatch')
    def save():
        temp = journal.with_suffix('.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(10):
            try:
                temp.replace(journal)
                break
            except PermissionError:
                if attempt == 9: raise
                time.sleep(.02)
    state.setdefault('deadline', min(now() + max(0, budget), deadline if deadline is not None else float('inf')))
    end = min(state['deadline'], deadline if deadline is not None else float('inf'))
    def remaining():
        return max(.001, end - now())
    async def call(phase, prompt, fields, image_paths=()):
        previous = next((c for c in state['calls'] if c['phase'] == phase), None)
        if previous:
            return previous.get('result', {}).get('items', []) if previous['state'] == 'completed' else []
        if any(c['state'] in ('pending', 'unknown') for c in state['calls']) or now() >= end:
            return []
        # 写入pending后才创建线程/轮次；恢复不会重放未知调用。
        entry = {'phase': phase, 'state': 'pending'}
        state['calls'].append(entry); save()
        def observe(notification):
            value = notification.payload
            value = value.model_dump(mode='json', by_alias=True) if hasattr(value, 'model_dump') else value
            if isinstance(value, dict) and value.get('turnId') and not entry.get('turn_id'):
                entry['turn_id'] = value['turnId']; save()
        try:
            thread = await asyncio.wait_for(backend.create_image_thread(), remaining())
            if not isinstance(thread, str): raise ValueError('invalid_image_thread')
            entry['thread_id'] = thread; save()
            result, metadata = await asyncio.wait_for(backend.image_task(thread, prompt, schema(fields), remaining(), observe, image_paths), remaining())
            check_shape(result, schema(fields), 'image_result')
            entry.update(state='completed', metadata=metadata, result=result); save()
            return result['items']
        except Exception as error:
            entry.update(state='unknown' if not getattr(error, 'completed', False) else 'failed',
                         error=type(error).__name__, error_message=str(error)[:400], timeout_stage=getattr(error, 'timeout_stage', None),
                         interruption_confirmed=getattr(error, 'interruption_confirmed', None), turn_id=getattr(error, 'turn_id', entry.get('turn_id')))
            save()
            return []
    result = deepcopy(packet)
    categories = {n['source_id']: c for c, items in packet['news'].items() for n in items}
    pending = [s for s in result['sources'] if s['id'] in categories and (s['id'] not in state['results'] or state['results'][s['id']]['kind'] == 'theme')]
    candidates = state.setdefault('candidates', [])
    async def download(source, image):
        if any(c['source_id'] == source['id'] and c['image']['url'] == image['url'] for c in candidates): return
        try:
            validate_image(image)
            info, path = await asyncio.wait_for(asyncio.to_thread(downloader, image['url'], directory / 'media', timeout=min(8, remaining())), remaining())
            candidates.append({'source_id': source['id'], 'title': source['title'], 'source_url': source['url'], 'published_at': source['published_at'],
                               'context': next((n for values in packet['news'].values() for n in values if n['source_id'] == source['id']), {}),
                               'image': {**image, **info}, 'path': str(path.resolve())}); save()
        except Exception as error:
            state['errors'].append({'source_id': source['id'], 'reason': type(error).__name__, 'detail': str(error)[:240]}); save()
    async def review(phase, selected):
        if not selected or not hasattr(backend, 'image_task'):
            return
        # 小批次直接传入图片，避免仅提供路径时模型只打开部分候选。
        for offset in range(0, len(selected), 4):
            batch = selected[offset:offset + 4]
            reviews = await call(phase + ':' + str(offset // 4),
                '按附图顺序逐张视觉核对下列新闻候选。图片由程序从所列原址下载并解码，original来自采集时原文提取。'
                '为每个候选返回且仅返回一项：source_id,asset_id,status(accept/reject/unverified),kind(original/event/theme),reason。'
                '必须覆盖全部候选，不得只返回通过项。reject用于站标、广告、无关人物或错误事件；'
                '不能确认时unverified并说明缺什么证据。图表、产品演示和正文照片均可作为原图，不要求必须是现场摄影。'
                '按所附画面与新闻标题、正文及原图出处核对；同题材非同一事件标theme。'
                'context.image_provenance是采集程序从原页HTML核验的出处证据，包含页面摘要与原始图片标签。'
                '存在该证据且图片URL一致时，出处已核实，视觉审核不必再次联网；搜索工具打不开原页不推翻已取得的HTML证据。'
                '只核对画面与原文关系，不要求另外证明原作者的摄影时间或人物身份；有具体矛盾才拒绝。'
                '图片与新闻数据：' + json.dumps(batch, ensure_ascii=False),
                ('source_id', 'asset_id', 'status', 'kind', 'reason'), [c['path'] for c in batch])
            for candidate in batch:
                matches = [v for v in reviews if v.get('source_id') == candidate['source_id'] and
                           v.get('asset_id') == candidate['image']['asset_id']]
                if len(matches) != 1:
                    state['errors'].append({'source_id': candidate['source_id'],
                        'asset_id': candidate['image']['asset_id'], 'reason': 'review_incomplete'})
                    save()
                    continue
                verdict = matches[0]
                if verdict.get('status') != 'accept' or verdict.get('kind') not in ('original', 'event', 'theme') or not verdict.get('reason'):
                    state['errors'].append({'source_id': candidate['source_id'],
                        'asset_id': candidate['image']['asset_id'], 'reason': 'review_' + verdict.get('status', 'invalid'),
                        'detail': verdict.get('reason', '')[:400]})
                    save()
                    continue
                sid = candidate['source_id']
                kind = verdict['kind']
                if candidate['image']['kind'] == 'theme': kind = 'theme'
                if kind == 'original' and candidate['image']['kind'] != 'original': kind = 'event'
                prior = state['results'].get(sid)
                if prior is None or (prior['kind'] == 'theme' and kind in ('original', 'event')):
                    state['results'][sid] = {**candidate['image'], 'kind': kind}
                    save()
    if pending and state.get('state') != 'completed':
        for source in pending:
            if source.get('image') and now() < end:
                await download(source, {**source['image'], 'kind': 'original', 'page_url': source['url']})
        await review('review_original', [c for c in candidates if c['image']['kind'] == 'original'])
        missing = [s for s in pending if (s['id'] not in state['results'] or state['results'][s['id']]['kind'] == 'theme')]
        if search and missing and hasattr(backend, 'image_task'):
            items = await call('search', '用联网搜索为以下新闻逐条查找图片，结合标题、人物/机构、地点、发布日期。每条最多3个候选，优先同事件，其次主题。'
                '必须打开出处页核实图片地址，不能猜测URL，不改变事实来源。返回source_id,url,page_url,credit,kind(event/theme),reason。无结果不返回。新闻数据：'
                + json.dumps(missing, ensure_ascii=False), ('source_id', 'url', 'page_url', 'credit', 'kind', 'reason'))
            for source in missing:
                choices = [i for i in items if i.get('source_id') == source['id']][:3]
                for item in sorted(choices, key=lambda i: i.get('kind') != 'event'):
                    if now() >= end: break
                    if item.get('kind') not in ('event', 'theme'): continue
                    await download(source, {k: item[k] for k in ('url', 'page_url', 'credit', 'kind')} | {'caption': item['reason'][:400]})
        await review('review_search', [c for c in candidates if c['image']['kind'] != 'original'])
    for source in result['sources']:
        sid = source['id']
        if sid in categories:
            if sid not in state['results']:
                state['results'][sid] = fallback(categories[sid])
                state['errors'].append({'source_id': sid, 'reason': 'budget_exhausted' if now() >= end else 'no_verified_image'})
            source['image'] = state['results'][sid]
    state['state'] = 'completed'; save()
    validate_packet(result)
    return result


def validate_freshness(packet, now=time.time):
    # 不移动cutoff_at/captured_at；配图耗时不能延长新闻有效期。
    earliest = datetime.fromtimestamp(now(), timezone.utc) - timedelta(hours=24)
    if any(parse_time(s['published_at']) < earliest for s in packet['sources']):
        raise ValueError('news_expired_after_images')
