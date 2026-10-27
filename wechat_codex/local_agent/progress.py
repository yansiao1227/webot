"""对外执行进度及决定或工具时限，不暴露推理内容。"""
import asyncio


class ProgressDeliveryError(Exception):
    """进度发送未确认时停止模型，不跳过该段索引。"""


class DecisionClock:
    STREAM_NOTICE_SECONDS = 30
    STREAM_NOTICE_TEXT = '内容过长，正在输出，请稍等……'
    STREAM_NOTICE_KEY = 'streaming_long_reply'
    TOOL_TEXT = {
        'webSearch': '我先联网搜索，核对相关资料。',
        'commandExecution': '我先运行本地工具核对所需信息。',
        'mcpToolCall': '我先调用工具获取所需信息。',
        'dynamicToolCall': '我先执行工具，核对相关信息。',
        'fileChange': '我先修改相关文件，再检查结果。',
        'imageGeneration': '我先生成所需图片。',
        'computerUse': '我先打开相关页面，查看实际内容。',
        'collabAgentToolCall': '我先执行下一项处理。',
    }

    def __init__(self, turn_id, decision_timeout, tool_timeout, callback=None, *,
                 startup_deadline=None, generation_idle_timeout=30, total_deadline=None):
        self.loop = asyncio.get_running_loop()
        self.turn_id = turn_id
        self.decision_timeout, self.tool_timeout = decision_timeout, tool_timeout
        self.idle_deadline = self.loop.time() + decision_timeout
        self.started = self.loop.time()
        self.startup_deadline = startup_deadline
        self.total_deadline = total_deadline
        self.generation_idle_timeout = generation_idle_timeout
        self.generating = False
        self.stream_started_at = None
        self.stream_notice_sent = False
        self.first_decision_seconds = None
        self.tools = {}
        self.seen = set()
        self.commentary_for_tool = False
        self.callback = callback
        self.changed = asyncio.Event()
        self.sending_deadline = None
        self.decision_count = 0
        self.tool_started_count = 0
        self.timeout_stage = 'decision'

    def deadline(self):
        deadlines = [self.total_deadline] if self.total_deadline is not None else []
        if self.sending_deadline is not None:
            deadlines.append(self.sending_deadline)
        elif self.startup_deadline is not None:
            deadlines.append(self.startup_deadline)
        else:
            deadlines.extend(self.tools.values() if self.tools else [self.idle_deadline])
        return min(deadlines)

    def ready(self):
        if self.startup_deadline is not None:
            self.startup_deadline = None
            self.idle_deadline = self.loop.time() + self.decision_timeout
            self.changed.set()

    async def publish(self, text, key, kind, *, streaming=False):
        if not self.callback:
            return
        self.sending_deadline = self.loop.time() + 15
        self.changed.set()
        try:
            await asyncio.wait_for(self.callback(text, {'key': key, 'kind': kind}), 15)
        finally:
            self.sending_deadline = None
            self.idle_deadline = self.loop.time() + (self.generation_idle_timeout if streaming else self.decision_timeout)
            self.changed.set()

    async def observe(self, notification):
        payload = notification.payload
        value = payload.model_dump(mode='json', by_alias=True) if hasattr(payload, 'model_dump') else payload
        if not isinstance(value, dict) or value.get('turnId') != self.turn_id:
            return
        method = notification.method
        item = value.get('item') or {}
        kind, item_id = item.get('type'), item.get('id')
        # 增量使用顶层 itemId/delta，而不是 item 对象。不得发布
        # 未完成的 schema JSON，也不能对连续文本片段去重。
        if method == 'item/agentMessage/delta':
            delta = value.get('delta')
            if value.get('itemId') and isinstance(delta, str) and delta:
                self.ready()
                self.generating = True
                if self.stream_started_at is None:
                    self.stream_started_at = self.loop.time()
                self.idle_deadline = self.loop.time() + self.generation_idle_timeout
                self.changed.set()
                if (not self.stream_notice_sent and not self.tools and self.callback and
                        self.loop.time() - self.stream_started_at >= self.STREAM_NOTICE_SECONDS):
                    self.stream_notice_sent = True
                    # 此标识跨模型轮次保持稳定，持久化回复段回调
                    # 会对整个入站请求抑制重复提示。
                    await self.publish(self.STREAM_NOTICE_TEXT, self.STREAM_NOTICE_KEY,
                                       'streaming_notice', streaming=True)
            return
        if not item_id:
            return
        # 外部输入完成表示运行时准备完成；旧运行时可能
        # 不发送该事件，实际模型条目也可证明就绪。
        if ((method == 'item/completed' and kind == 'functionCallOutput' and item.get('name') == 'wechat_message') or
                (method in ('item/started', 'item/completed') and
                 (kind in ('reasoning', 'agentMessage') or kind in self.TOOL_TEXT))):
            self.ready()
        identity = (method, item_id)
        if identity in self.seen:
            return
        self.seen.add(identity)
        if method == 'item/completed' and kind == 'agentMessage':
            self.stream_started_at = None
        if method == 'item/completed' and kind == 'agentMessage' and item.get('phase') in ('commentary', 'partial_answer'):
            text = item.get('text', '').strip()
            # 不转发结构化的最终回答或规划字段，也不转发原始推理。
            if text and not text.startswith(('{', '```')):
                if len(text) > 1200 or '\0' in text:
                    raise ProgressDeliveryError('进度消息格式无效')
                if self.first_decision_seconds is None:
                    self.first_decision_seconds = round(self.loop.time() - self.started, 3)
                await self.publish(text, self.turn_id + ':' + item_id, 'commentary')
                self.commentary_for_tool = True
                self.idle_deadline = self.loop.time() + self.decision_timeout
                self.generating = False
                self.decision_count += 1
                self.changed.set()
        if kind in self.TOOL_TEXT:
            if method == 'item/started':
                self.generating = False
                self.stream_started_at = None
                self.tool_started_count += 1
                if self.first_decision_seconds is None:
                    self.first_decision_seconds = round(self.loop.time() - self.started, 3)
                self.tools[item_id] = self.loop.time() + self.tool_timeout
                self.decision_count += 1
                self.changed.set()
                if not self.commentary_for_tool:
                    text = self.TOOL_TEXT[kind]
                    if kind == 'webSearch':
                        action = (item.get('action') or {}).get('type')
                        if action == 'openPage':
                            text = '我再打开来源网页，核对原文。'
                        elif action == 'findInPage':
                            text = '我在来源页面中查找相关内容。'
                    if kind == 'dynamicToolCall' and 'web__run' in str(item.get('arguments', item.get('input', ''))):
                        text = self.TOOL_TEXT['webSearch']
                    await self.publish(text, self.turn_id + ':' + item_id, 'tool_decision')
                self.commentary_for_tool = False
            elif method == 'item/completed':
                self.tools.pop(item_id, None)
                self.generating = False
                self.stream_started_at = None
                self.idle_deadline = self.loop.time() + self.decision_timeout
                self.changed.set()

    async def wait(self, task):
        while not task.done():
            self.changed.clear()
            changed = asyncio.create_task(self.changed.wait())
            try:
                await asyncio.wait({task, changed}, timeout=max(.001, self.deadline() - self.loop.time()),
                                   return_when=asyncio.FIRST_COMPLETED)
                if not task.done() and self.loop.time() >= self.deadline():
                    self.timeout_stage = ('total' if self.total_deadline is not None and self.loop.time() >= self.total_deadline
                                          else 'progress_send' if self.sending_deadline is not None
                                          else 'startup' if self.startup_deadline is not None
                                          else 'tool' if self.tools else 'generation' if self.generating else 'decision')
                    raise TimeoutError('decision_or_tool_timeout')
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
        return task.result()
