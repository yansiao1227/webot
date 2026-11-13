"""常驻服务内的每日任务；网页与朋友圈副作用仅由业务层执行。"""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import time

from .store import BEIJING, DigestStore, RunnerLock, TERMINAL

COLLECTION_TIMEOUT_SECONDS = 300


def format_moment(edition, url):
    """固定分享格式；只使用已验证日报的三项重点，不另调模型。"""
    highlights = edition.get('highlights')
    if not isinstance(highlights, list) or len(highlights) != 3 or any(
            not isinstance(item, str) or not item.strip() for item in highlights):
        raise ValueError('朋友圈简评需要三项已验证重点')
    comments = [' '.join(item.split()) for item in highlights]
    return '每日观察：' + url + '\n简评：\n' + '\n'.join(
        str(index) + '. ' + text for index, text in enumerate(comments, 1))


class DailyDigestRunner:
    def __init__(self, store, backend, mcp, publisher, config, collector=None, validator=None, now=time.time):
        self.store, self.backend, self.mcp, self.publisher = store, backend, mcp, publisher
        self.config, self.now = config, now
        if collector is None:
            from .collectors import collect
            collector = collect
        if validator is None:
            from .content import validate_edition
            validator = validate_edition
        self.collector, self.validator = collector, validator
        self.task = None
        self.stopping = False
        self.history_ready = False
        self.history_date = None
        self.history_retry_at = 0
        self.owner = RunnerLock(store.directory)
        try:
            self.store.recover()
        except BaseException:
            self.owner.close()
            raise

    @property
    def activated(self):
        return bool(self.config.get('mobile_verified_at') and self.config.get('site_url'))

    async def tick(self):
        if self.stopping or not self.activated:
            return
        if self.task is not None:
            if not self.task.done():
                return
            await self.task  # process() 已封装业务异常，不传播到普通微信任务。
            self.task = None
        self.store.expire(self.now())
        # 旧发帖状态查询不依赖网站历史可用性，不能被网站故障阻断。
        unsettled = self.store.next_unsettled()
        if unsettled:
            self.task = asyncio.create_task(self.process(unsettled['date']))
            return
        if self.store.meta('paused'):
            return
        if not self.history_ready:
            if self.now() >= self.history_retry_at:
                self.task = asyncio.create_task(self.sync_history())
            return
        row = self.store.ensure_today(self.now())
        if row and row['state'] not in TERMINAL:
            if row['state'] == 'waiting' and self.now() < row['planned_at']:
                return
            if row['state'] == 'waiting' and row['error'] == 'mcp_paused' and self.now() - row['updated'] < 30:
                return
            if row['state'] in ('website_unknown', 'uploading_media') and self.now() - row['updated'] < 60:
                return
            self.task = asyncio.create_task(self.process(row['date']))

    async def sync_history(self, date=None):
        self.store.meta('history_sync', {'state': 'syncing', 'at': self.now()})
        try:
            editions = await self.publisher.history()
            if not isinstance(editions, list):
                raise ValueError('网站历史列表格式无效')
            from .content import validate_edition
            editions = [validate_edition(e, allow_simulation=False) for e in editions]
            if len({e['date'] for e in editions}) != len(editions):
                raise ValueError('网站历史日期重复')
            self.store.sync_history(editions, self.now())
            self.history_ready, self.history_date = True, date
            self.store.meta('history_sync', {'state': 'verified', 'at': self.now(), 'count': len(editions)})
            return True
        except Exception as error:
            self.history_ready = False
            self.history_retry_at = self.now() + 60
            self.store.meta('history_sync', {'state': 'failed', 'at': self.now(), 'error': type(error).__name__})
            return False

    def can_continue(self, row):
        if self.now() >= row['deadline']:
            self.store.update(row['date'], 'expired', error='publish_window_closed')
            return False
        return not self.stopping and not self.store.meta('paused')

    async def process(self, date):
        row = self.store.run(date)
        state = row['state']
        try:
            if state in ('submitting', 'queued', 'running', 'accepted', 'unknown'):
                await self.reconcile(row)
                return
            if not self.can_continue(row):
                return
            if state == 'pending':
                # 附加每日刷新，避免上午启动同步后漏掉下午的手工部署。
                if self.history_date != date and not await self.sync_history(date):
                    return
                if not self.can_continue(row):
                    return
                existing = self.store.historical_edition(date)
                if existing:
                    row = self.store.update(date, 'uploading', edition=existing)
                    state = 'uploading'  # 已存在网页只读核对，不覆盖或伪造已发朋友圈。
            if state == 'pending':
                self.store.update(date, 'collecting')
                sources_config = dict(self.config.get('sources') or {})
                sources_config['exclude_urls'] = sorted(set(sources_config.get('exclude_urls', [])) |
                                                       self.store.published_article_urls(date))
                packet = await asyncio.wait_for(asyncio.to_thread(self.collector, date,
                    datetime.fromtimestamp(self.now(), BEIJING).isoformat(), sources_config),
                    timeout=min(COLLECTION_TIMEOUT_SECONDS, max(.001, row['deadline'] - self.now())))
                row = self.store.update(date, 'collected', packet=packet)
                state = 'collected'
            if not self.can_continue(row):
                return
            if state == 'collected':
                if self.stopping or self.store.meta('paused'):
                    return
                if any(len(row['packet'].get('news', {}).get(key, [])) < 3 for key in ('ai', 'society')):
                    self.store.update(date, 'failed', error='insufficient_verified_news')
                    return
                from .content import validate_packet
                validate_packet(row['packet'])
                from .images import prepare_images, validate_freshness
                packet = await prepare_images(row['packet'], self.backend, self.store.directory / 'images' / date,
                    budget=self.config.get('image_search_budget_seconds', 300), deadline=row['deadline'], now=self.now)
                validate_freshness(packet, self.now)
                row = self.store.update(date, 'imaged', packet=packet)
                state = 'imaged'
            if state == 'imaged':
                if not self.can_continue(row):
                    return
                from .images import validate_freshness
                validate_freshness(row['packet'], self.now)
                # 独立thread在轮次提交前写入账本；生成执行未知不自动重跑。
                thread_id = await asyncio.wait_for(self.backend.create_report_thread(row.get('thread_id')),
                    min(60, max(.001, row['deadline'] - self.now())))
                row = self.store.update(date, 'imaged', thread_id=thread_id)
                if not self.can_continue(row):
                    return
                self.store.update(date, 'generating', thread_id=thread_id)
                value, metadata = await self.backend.generate_report(thread_id, row['packet'], deadline=row['deadline'])
                row = self.store.update(date, 'generated', edition=value,
                                        turn_id=metadata.get('turn_id'))
                state = 'generated'
            if state == 'generated':
                edition = self.validator(row['edition'], packet=row['packet'], allow_simulation=False)
                row = self.store.update(date, 'validated', edition=edition)
                state = 'validated'
            if not self.can_continue(row):
                return
            if state in ('validated', 'uploading_media'):
                self.store.update(date, 'uploading_media')
                await self.publisher.upload_media(row['edition'], self.store.directory / 'images' / date / 'media')
                row = self.store.update(date, 'media_ready')
                state = 'media_ready'
            if state == 'media_ready':
                if not self.can_continue(row):
                    return
                self.store.update(date, 'uploading')
                try:
                    await self.publisher.upload(row['edition'])
                except Exception:
                    # PUT响应丢失后只进行精确读回，不换日期或覆盖内容。
                    pass
                state = 'uploading'
            if state in ('uploading', 'website_unknown'):
                url = await self.publisher.verify(row['edition'])
                self.store.sync_history([row['edition']], self.now())
                text = format_moment(row['edition'], url)
                row = self.store.update(date, 'waiting', post_text=text)
                state = 'waiting'
            if state == 'waiting' and self.now() >= row['planned_at']:
                if self.now() >= row['deadline']:
                    self.store.update(date, 'expired', error='publish_window_closed')
                    return
                if self.stopping or self.store.meta('paused'):
                    return
                status = await self.mcp.call('bridge_status')
                if status.get('account') != self.store.account or status.get('simulation') is not False:
                    raise ValueError('日报发布账号或真实模式不匹配')
                if (status.get('sns_policy') or {}).get('paused') is True:
                    self.store.update(date, 'waiting', error='mcp_paused')
                    return
                # 截止与暂停在等待上游状态之后再次检查。
                if self.now() >= row['deadline']:
                    self.store.update(date, 'expired', error='publish_window_closed')
                    return
                if self.stopping or self.store.meta('paused'):
                    return
                self.store.update(date, 'submitting', error=None)
                try:
                    operation = await self.mcp.call('publish_moment', intent_id=row['intent_id'], text=row['post_text'])
                except Exception:
                    self.store.update(date, 'unknown', error='mcp_submission_unknown')
                    return
                self.save_operation(row, operation)
        except asyncio.CancelledError:
            if self.store.run(date)['state'] == 'generating':
                self.store.update(date, 'generation_unknown', error='generation_cancelled')
            raise
        except Exception as error:
            current = self.store.run(date)
            if current['state'] == 'uploading_media':
                state = 'uploading_media'
            elif current['state'] in ('uploading', 'website_unknown'):
                state = 'website_unknown'
            else:
                state = ('generation_unknown' if current['state'] == 'generating' and
                         not getattr(error, 'completed', False) else 'failed')
            diagnostic = type(error).__name__
            if getattr(error, 'timeout_stage', None):
                diagnostic += ':' + error.timeout_stage
            self.store.update(date, state, error=diagnostic,
                              turn_id=getattr(error, 'turn_id', current.get('turn_id')))

    def save_operation(self, row, operation):
        state = operation.get('state') if isinstance(operation, dict) else 'unknown'
        if state not in ('confirmed', 'failed', 'queued', 'running', 'accepted', 'unknown'):
            state = 'unknown'
        if not isinstance(operation, dict) or operation.get('intent_id') != row['intent_id']:
            state, operation = 'unknown', {'error': 'invalid_mcp_operation'}
        if state == 'confirmed' and operation.get('server_confirmed') is not True:
            state = 'unknown'
        self.store.update(row['date'], state, operation=operation)

    async def reconcile(self, row):
        # 有界查询间隔，避免每秒轮询朋友圈；重启不产生新的写入意图。
        if self.now() - row['updated'] < 30:
            return
        try:
            operation = await self.mcp.call('get_moment_publish_status', intent_id=row['intent_id'])
        except Exception:
            self.store.update(row['date'], 'unknown', error='mcp_status_unavailable')
            return
        self.save_operation(row, operation)

    async def close(self):
        self.stopping = True
        if self.task is not None:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.store.close()
        self.owner.close()


def attach(agent):
    """没有配置时不创建任务，不影响已有用户服务。"""
    from tool.settings import path_setting
    from .publisher import create_publisher
    directory = path_setting('DAILY_DIGEST_STATE_DIR', 'daily_digest/state/live')
    file = directory / 'config.json'
    if not file.exists():
        return None
    config = json.loads(file.read_text(encoding='utf-8-sig'))
    if config.get('account') != agent.config['account']:
        raise ValueError('日报配置账号不匹配')
    if not config.get('site_url') or not config.get('mobile_verified_at'):
        return None
    # 凭据与SSH留在发布适配器；不给模型prompt或生成线程。
    publisher = create_publisher(config, directory)
    store = DigestStore(directory, agent.config['account'])
    try:
        return DailyDigestRunner(store, agent.backend, agent.mcp, publisher, config)
    except BaseException:
        store.close()
        raise
