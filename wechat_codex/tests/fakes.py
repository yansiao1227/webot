import asyncio
import time

from local_agent.mcp import MCPError

ACCOUNT = 'wxid_test_self'
CONVERSATIONS = ['wxid_test_peer', '12345@chatroom', 'wxid_test_other']
CONFIG = {'account': ACCOUNT, 'conversations': CONVERSATIONS, 'consumer_id': 'test-codex',
          'mcp_url': 'http://127.0.0.1:30002/mcp', 'simulation': True, 'max_concurrency': 2}


def event(eid='1', conversation='wxid_test_peer', text='你好', mention=True):
    value = {'event_id': eid, 'account': ACCOUNT, 'conversation_id': conversation, 'direction': 'incoming',
             'message_type': 'text', 'payload': {'text': text}, 'captured_at': time.time()}
    if conversation.endswith('@chatroom'):
        value['mentions'] = {'state': 'known', 'source': 'weixin41930_record_source_180',
                             'wxids': [ACCOUNT] if mention else [], 'all': False}
    return value


class FakeMCP:
    def __init__(self):
        self.events, self.sent, self.operations, self.calls = [], [], {}, []
        self.position = 0
        self.fail_ack = self.lose_send_response = self.fail_send_unknown = False
        self.account = ACCOUNT
        self.subscriptions = 0

    async def call(self, name, **args):
        await asyncio.sleep(.001)
        self.calls.append((name, args))
        if name in ('mark_conversation_read', 'get_conversation_read_status'):
            return {'event_id': args['event_id'], 'state': 'already_read'}
        if name == 'reply_text_part':
            key=(args['event_id'],args['part_index'])
            if key not in self.operations:
                self.sent.append(args)
                self.operations[key]={'state':'accepted','request_id':'part-'+str(key)}
            return self.operations[key]
        if name == 'get_reply_part_status':
            return self.operations[(args['event_id'],args['part_index'])]
        if name == 'bridge_status':
            return {'account': self.account, 'allowed_conversations': CONVERSATIONS, 'simulation': True,
                    'subscriptions_active': self.subscriptions,
                    'group_reply_policy': {'require_explicit_self_mention': True}}
        if name == 'get_messages':
            events = self.events[self.position:self.position+1]
            return {'events': events, 'ack_token': str(self.position) if events else None}
        if name == 'ack_messages':
            if self.fail_ack:
                raise MCPError('模拟ack丢失')
            if args['ack_token'] == str(self.position):
                self.position += 1
            return {'acknowledged': True}
        if name == 'reply_text':
            eid = args['event_id']
            if self.fail_send_unknown:
                raise MCPError('模拟未知发送')
            if eid not in self.operations:
                self.sent.append(args)
                self.operations[eid] = {'state': 'accepted', 'request_id': 'test-' + eid, 'delivery_confirmed': False}
            if self.lose_send_response:
                raise MCPError('模拟发送响应丢失')
            return self.operations[eid]
        if name == 'get_reply_status':
            if args['event_id'] not in self.operations:
                raise MCPError('无操作')
            return self.operations[args['event_id']]
        raise AssertionError(name)


class FakeBackend:
    def __init__(self, shared=None):
        self.threads = shared if shared is not None else {}
        self.created, self.resumed, self.calls = [], [], []
        self.fail = False
        self.running = {}
        self.max_active = 0
        self.gate = None

    async def start(self): pass
    async def close(self): pass

    async def ensure_thread(self, cid, thread_id=None):
        if thread_id:
            if thread_id not in self.threads:
                raise ValueError('模拟恢复失败')
            self.resumed.append(thread_id)
        else:
            thread_id = 'thread-' + str(len(self.threads)+1)
            self.threads[thread_id] = []
            self.created.append(thread_id)
        return thread_id

    async def generate(self, thread_id, value):
        cid = value['conversation_id']
        self.running[cid] = self.running.get(cid, 0) + 1
        assert self.running[cid] == 1
        self.max_active = max(self.max_active, len(self.running))
        try:
            if self.gate:
                await self.gate.wait()
            if self.fail:
                raise RuntimeError('模拟模型失败')
            self.calls.append(value['event_id'])
            self.threads[thread_id].append(value['payload']['text'])
            return '测试回复：' + value['payload']['text'], {'thread_id': thread_id, 'tool_calls': 0}
        finally:
            self.running.pop(cid)
