"""Public execution updates and decision/tool deadlines; never expose reasoning."""
import asyncio


class ProgressDeliveryError(Exception):
    """Stop the model if a progress send is not confirmed; never skip its index."""


class DecisionClock:
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

    def __init__(self, turn_id, decision_timeout, tool_timeout, callback=None):
        self.loop = asyncio.get_running_loop()
        self.turn_id = turn_id
        self.decision_timeout, self.tool_timeout = decision_timeout, tool_timeout
        self.idle_deadline = self.loop.time() + decision_timeout
        self.started = self.loop.time()
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
        if self.sending_deadline is not None:
            return self.sending_deadline
        return min(self.tools.values()) if self.tools else self.idle_deadline

    async def publish(self, text, key, kind):
        if not self.callback:
            return
        self.sending_deadline = self.loop.time() + 15
        self.changed.set()
        try:
            await asyncio.wait_for(self.callback(text, {'key': key, 'kind': kind}), 15)
        finally:
            self.sending_deadline = None
            self.idle_deadline = self.loop.time() + self.decision_timeout
            self.changed.set()

    async def observe(self, notification):
        payload = notification.payload
        value = payload.model_dump(mode='json', by_alias=True) if hasattr(payload, 'model_dump') else payload
        if not isinstance(value, dict) or value.get('turnId') != self.turn_id:
            return
        method = notification.method
        item = value.get('item') or {}
        kind, item_id = item.get('type'), item.get('id')
        if not item_id:
            return
        identity = (method, item_id)
        if identity in self.seen:
            return
        self.seen.add(identity)
        if method == 'item/completed' and kind == 'agentMessage' and item.get('phase') in ('commentary', 'partial_answer'):
            text = item.get('text', '').strip()
            # Structured final/planning fields and raw reasoning are never forwarded.
            if text and not text.startswith(('{', '```')):
                if len(text) > 1200 or '\0' in text:
                    raise ProgressDeliveryError('进度消息格式无效')
                if self.first_decision_seconds is None:
                    self.first_decision_seconds = round(self.loop.time() - self.started, 3)
                await self.publish(text, self.turn_id + ':' + item_id, 'commentary')
                self.commentary_for_tool = True
                self.idle_deadline = self.loop.time() + self.decision_timeout
                self.decision_count += 1
                self.changed.set()
        if kind in self.TOOL_TEXT:
            if method == 'item/started':
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
                self.idle_deadline = self.loop.time() + self.decision_timeout
                self.changed.set()

    async def wait(self, task):
        while not task.done():
            self.changed.clear()
            changed = asyncio.create_task(self.changed.wait())
            try:
                done, _ = await asyncio.wait({task, changed}, timeout=max(.001, self.deadline() - self.loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done and self.loop.time() >= self.deadline():
                    self.timeout_stage = 'progress_send' if self.sending_deadline is not None else 'tool' if self.tools else 'decision'
                    raise TimeoutError('decision_or_tool_timeout')
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
        return task.result()
