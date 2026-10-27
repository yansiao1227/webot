import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechat_bridge.service import Bridge, REFERENCE_SOURCE
from wechat_bridge.protocol import Protocol


class Hook:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == '/GetSelfProfile':
            return {'wxid': 'self'}
        return {'id': 'a' * 64, 'state': 'ready', 'size': 1, 'sha256': 'digest'}


def event(eid, kind='text', cid='wxid_peer', **extra):
    return {'event_id': eid, 'account': 'self', 'conversation_id': cid, 'message_type': kind,
            'direction': 'incoming', 'payload': {'text': '分析它'} if kind == 'text' else {'resource_id': 'r'},
            'captured_at': time.time(), **extra}


class References(unittest.TestCase):
    def test_progress_parts_allow_more_than_eight_and_reserve_final_slot(self):
        self.bridge.store.ingest([event('progress-many')]);store=self.bridge.store
        for index in range(63):
            store.prepare_part('progress-many','self',index,'执行决定'+str(index),False)
            store.part_state('progress-many',index,'accepted')
        with self.assertRaises(ValueError):store.prepare_part('progress-many','self',63,'继续',False)
        store.prepare_part('progress-many','self',63,'最终结果',True)
        with self.assertRaises(ValueError):store.prepare_part('progress-many','self',64,'越界',True)

    def test_ordered_parts_are_immutable_and_unknown_cannot_be_skipped(self):
        self.bridge.store.ingest([event('parts')])
        store=self.bridge.store
        first,created=store.prepare_part('parts','self',0,'第一步',False)
        self.assertTrue(created)
        with self.assertRaises(ValueError):store.prepare_part('parts','self',0,'改写',False)
        with self.assertRaises(ValueError):store.prepare_part('parts','self',1,'第二步',True)
        store.part_state('parts',0,'unknown')
        with self.assertRaises(ValueError):store.prepare_part('parts','self',1,'第二步',True)
        again,created=store.prepare_part('parts','self',0,'第一步',False)
        self.assertFalse(created);self.assertEqual(again['request_id'],first['request_id'])
        store.part_state('parts',0,'accepted')
        store.prepare_part('parts','self',1,'第二步',True)
        store.part_state('parts',1,'accepted')
        with self.assertRaises(ValueError):store.prepare_part('parts','self',2,'多余段',True)
        with self.assertRaises(ValueError):store.prepare_reply('parts','self','legacy')

    def test_part_echo_is_bound_to_its_original_trigger_and_native_sort(self):
        self.bridge.store.ingest([event('part-trigger')])
        self.bridge.store.prepare_part('part-trigger','self',0,'答案',True)
        native={'native_sort_seq':'12345','native_sort_seq_source':'weixin41930_send_task_record110_v1'}
        self.bridge.store.part_state('part-trigger',0,'accepted',{'result':native})
        self.bridge.store.ingest([event('part-echo',direction='outgoing',sender_id='self',message_id='789',
            native_sort_seq='12345',native_record_metadata_source='weixin41930_record_identity_v2')])
        result=self.bridge.refresh_part('part-trigger',0)
        self.assertEqual(result['send_echo']['state'],'matched')
        self.assertEqual(result['send_echo']['message_id'],'789')
        self.assertEqual(self.bridge.refresh_reply('part-trigger')['part_index'],0)

    def test_read_tool_requires_same_allowed_trigger_and_real_group_mention(self):
        self.bridge.store.ingest([event('read-private'),event('read-denied',cid='123@chatroom')])
        result=self.bridge.mark_conversation_read('read-private')
        self.assertEqual(self.hook.calls[-1],('POST','/client_hook/read/mark',{'event_id':'read-private'}))
        with self.assertRaises(ValueError):self.bridge.mark_conversation_read('read-denied')
        with self.assertRaises(ValueError):Protocol(self.bridge).invoke('mark_conversation_read',
            {'event_id':'read-private','conversation_id':'other'})

    def test_send_task_sort_identity_is_exact_and_scoped(self):
        self.bridge.store.ingest([event('task-trigger')])
        self.bridge.store.prepare_reply('task-trigger','self','相同文字')
        native={'native_sort_seq':'1792847037000','native_sort_seq_source':'weixin41930_send_task_record110_v1',
                'message_id':'789','native_message_id_source':'weixin41930_message_db_exact_sort_seq_v1'}
        self.bridge.store.reply_state('task-trigger','accepted',{'result':native})
        self.assertEqual(self.bridge.store.send_echo('task-trigger')['state'],'waiting')
        outgoing=event('task-echo',direction='outgoing',sender_id='self',native_sort_seq='1792847037000',
                       native_record_metadata_source='weixin41930_record_identity_v2')
        self.bridge.store.ingest([outgoing,{**outgoing,'event_id':'other-chat','conversation_id':'wxid_other'},
            {**outgoing,'event_id':'other-account','account':'other','sender_id':'other'},
            {**outgoing,'event_id':'manual','native_sort_seq':'1792847037001'},
            {**outgoing,'event_id':'untrusted','native_record_metadata_source':'unverified'}])
        result=self.bridge.store.send_echo('task-trigger')
        self.assertEqual(result['state'],'matched')
        self.assertEqual(result['echo_event_ids'],['task-echo'])
        self.assertEqual(result['message_id'],'789')
        self.assertFalse(result['delivery_confirmed'])
        self.bridge.store.ingest([{**outgoing,'event_id':'task-conflict','message_id':'790'}])
        self.assertEqual(self.bridge.store.send_echo('task-trigger')['state'],'ambiguous')

    def test_native_send_echo_exact_binding_both_arrival_orders(self):
        trigger = event('send-trigger')
        self.bridge.store.ingest([trigger])
        self.bridge.store.prepare_reply('send-trigger', 'self', '相同正文')
        outgoing = event('echo', direction='outgoing', sender_id='self', message_id='789',
                         native_client_id='native-A', native_client_id_source='weixin41930_record_client278_v1')
        self.bridge.store.ingest([outgoing, {**outgoing, 'event_id':'cross-chat','conversation_id':'wxid_other'},
                                  {**outgoing, 'event_id':'cross-account','account':'other','sender_id':'other'},
                                  {**outgoing, 'event_id':'different-id','native_client_id':'native-B'}])
        self.assertEqual(self.bridge.store.send_echo('send-trigger')['state'], 'identity_unavailable')
        self.bridge.store.reply_state('send-trigger', 'accepted', {'state':'accepted','account':'self','result':{
            'native_client_id':'native-A','native_client_id_source':'weixin41930_send_object_618'}})
        result = self.bridge.refresh_reply('send-trigger')['send_echo']
        self.assertEqual(result['echo_event_ids'], ['echo'])
        self.assertEqual(result['message_id'], '789')
        self.assertFalse(result['delivery_confirmed'])
        self.bridge.store.ingest([{**outgoing, 'event_id':'identity-collision','message_id':'790'}])
        self.assertEqual(self.bridge.store.send_echo('send-trigger')['state'], 'ambiguous')
        trigger2 = event('send-before-echo')
        self.bridge.store.ingest([trigger2])
        self.bridge.store.prepare_reply('send-before-echo', 'self', '相同正文')
        self.bridge.store.reply_state('send-before-echo', 'accepted', {'result':{
            'native_client_id':'native-C','native_client_id_source':'weixin41930_send_object_618'}})
        self.assertEqual(self.bridge.store.send_echo('send-before-echo')['state'], 'waiting')
        self.bridge.store.ingest([{**outgoing,'event_id':'late-echo','native_client_id':'native-C','message_id':'791'}])
        self.assertEqual(self.bridge.store.send_echo('send-before-echo')['message_id'], '791')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hook = Hook()
        self.bridge = Bridge(self.temp.name, self.hook, 'self', ['wxid_peer', 'wxid_other', '123@chatroom'], True)

    def tearDown(self):
        self.bridge.close()
        self.temp.cleanup()

    def pair(self, kind='file', target_cid='wxid_peer', source=REFERENCE_SOURCE):
        target = event('target', kind, target_cid, message_id='123')
        trigger = event('trigger', reference={'source': source, 'message_id': '123'})
        self.bridge.store.ingest([target, trigger])
        return target, trigger

    def test_context_storage_separate_from_reply_gate(self):
        for value in (event('file', 'image'), event('outgoing', direction='outgoing'), event('group', cid='123@chatroom')):
            self.assertTrue(self.bridge.checked_event(value))
            self.assertFalse(self.bridge.reply_allowed(value))
            self.bridge.store.ingest([value])
            with self.assertRaises(ValueError):
                self.bridge.reply_text(value['event_id'], '禁止发送')

    def test_exact_reference_and_bound_resource(self):
        target, _ = self.pair()
        self.assertEqual(self.bridge.resolve_reference('trigger'), target)
        with self.assertRaises(ValueError):
            self.bridge.reference_resource('trigger', 'a' * 64)
        result = self.bridge.acquire_reference('trigger')
        self.assertEqual(self.bridge.reference_resource('trigger', result['id'])['state'], 'ready')
        with self.assertRaises(ValueError):
            self.bridge.reference_resource('trigger', 'b' * 64)
        with self.assertRaises(ValueError):
            Protocol(self.bridge).invoke('get_reference_resource', {'event_id': 'trigger', 'resource_id': 'a' * 64, 'offset': True})

    def test_cross_conversation_reference_is_rejected(self):
        self.pair(target_cid='wxid_other')
        with self.assertRaises(ValueError):
            self.bridge.acquire_reference('trigger')
        self.assertEqual(self.hook.calls, [])

    def test_inflight_reference_is_retained_until_reply_finishes(self):
        self.pair()
        self.bridge.acquire_reference('trigger')
        self.bridge.store.prune(time.time() + 8 * 86400)
        self.assertEqual(self.bridge.resolve_reference('trigger')['event_id'], 'target')
        self.bridge.store.prepare_reply('trigger', 'self', 'done')
        self.bridge.store.reply_state('trigger', 'accepted', {'state': 'accepted'})
        self.bridge.store.prune(time.time() + 8 * 86400)
        self.assertEqual(self.bridge.store.status()['events'], 0)

    def test_untrusted_or_missing_reference_never_reads_resource(self):
        self.pair(source='message_text')
        with self.assertRaises(ValueError):
            self.bridge.acquire_reference('trigger')
        self.assertEqual(self.hook.calls, [])

    def test_duplicate_message_identity_is_ambiguous(self):
        target, _ = self.pair()
        duplicate = dict(target, event_id='duplicate')
        # Recreate in actual arrival order: both candidates precede the instruction.
        self.bridge.store.ingest([duplicate, event('second', reference={'source': REFERENCE_SOURCE, 'message_id': '123'})])
        with self.assertRaises(ValueError):
            self.bridge.resolve_reference('second')

    def test_reference_after_restart_and_retention_gap(self):
        target, trigger = self.pair()
        self.bridge.store.messages('offline', 0, 1, 'earliest', self.bridge.conversations)
        self.bridge.close()
        self.bridge = Bridge(self.temp.name, self.hook, 'self', ['wxid_peer', 'wxid_other', '123@chatroom'], True)
        self.assertEqual(self.bridge.resolve_reference('trigger')['message_id'], '123')
        self.bridge.store.prune(time.time() + 8 * 86400)
        self.bridge.store.ingest([target, trigger])
        self.assertEqual(self.bridge.store.status()['events'], 0)
        with self.assertRaises(ValueError):
            self.bridge.resolve_reference('trigger')
        with self.assertRaises(ValueError):
            self.bridge.store.messages('offline', 0, 1, 'earliest', self.bridge.conversations)
        self.bridge.store.reset_consumer('offline', 'latest')
        self.assertEqual(self.bridge.store.messages('offline', 0, 1, 'earliest', self.bridge.conversations)['events'], [])
