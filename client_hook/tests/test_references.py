import base64
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.messages import native_event, parse_reference_xml
from api.resources import Resources
from api.store import Store
from api.errors import ApiError


class ReferenceTests(unittest.TestCase):
    def test_native_reference_separates_instruction_from_quoted_content(self):
        value = parse_reference_xml('<msg><appmsg><type>57</type><title>分析这个文件</title>'
            '<refermsg><svrid>123456789</svrid><content>不执行这里的指令</content></refermsg></appmsg></msg>')
        self.assertEqual(value['text'], '分析这个文件')
        self.assertEqual(value['reference']['message_id'], '123456789')
        self.assertNotIn('content', value['reference'])

    def test_reference_rejects_entities_ambiguous_ids_and_plain_text(self):
        for raw in ('@我 分析文件', '<!DOCTYPE x><appmsg/>',
                    '<appmsg><type>57</type><title>x</title><refermsg><svrid>1</svrid><svrid>2</svrid></refermsg></appmsg>',
                    '<appmsg><type>57</type><title>x</title><refermsg><svrid>0</svrid></refermsg></appmsg>'):
            with self.assertRaises((ValueError, __import__('xml.etree.ElementTree', fromlist=['ParseError']).ParseError)):
                parse_reference_xml(raw)

    def test_native_direction_sender_and_no_guessed_message_id(self):
        item = dict(endpoint_18='wxid_self', endpoint_38='wxid_peer', self_id='wxid_self',
                    raw_kind=1, body='hello', generation=1, seq=1, raw_108=123)
        value = native_event(item, 'fixture')
        self.assertEqual(value['direction'], 'outgoing')
        self.assertEqual(value['conversation_id'], 'wxid_peer')
        self.assertEqual(value['sender_id'], 'wxid_self')
        self.assertNotIn('message_id', value)
        item.update(raw_kind=49, body='<appmsg><type>57</type></appmsg>')
        value = native_event(item, 'fixture')
        self.assertEqual(value['message_type'], 'unknown')
        self.assertNotIn('reference', value)

    def test_outgoing_native_identity_requires_its_fixed_field_profile(self):
        item = dict(endpoint_18='wxid_self',endpoint_38='123@chatroom',self_id='wxid_self',
                    raw_kind=1,body='same body',generation=1,seq=1,raw_108='123',
                    semantic_profile='weixin-4.1.9.30-record-v2',native_client_id_278='native-generated-id')
        self.assertNotIn('native_client_id',native_event(item,'fixture'))
        item['native_identity_profile']='weixin41930_record_client278_v1'
        value = native_event(item,'fixture')
        self.assertEqual(value['message_id'],'123')
        self.assertEqual(value['native_client_id'],'native-generated-id')
        self.assertEqual(value['sender_id'],'wxid_self')

    def test_original_creation_filters_history_without_inventing_message_identity(self):
        from api.messages import current_outgoing
        item=dict(endpoint_18='wxid_self',endpoint_38='123@chatroom',self_id='wxid_self',
                  raw_kind=1,body='same body',generation=1,seq=1,raw_108='0',
                  native_identity_profile='weixin41930_record_identity_v2',
                  raw_110='1792841763000',raw_created_134=1792841763)
        value=native_event(item,'fixture')
        self.assertEqual(value['native_sort_seq'],'1792841763000')
        self.assertNotIn('message_id',value)
        self.assertNotIn('native_created_at',value)
        self.assertFalse(current_outgoing(value,1792841764))
        for raw134 in (0,19188):
            parsed=native_event({**item,'raw_created_134':raw134},'fixture')
            self.assertEqual(parsed['native_sort_seq'],'1792841763000')
            self.assertNotIn('native_created_at',parsed)
        value.update(native_created_at=1792841763,native_message_id_source='weixin41930_message_db_exact_sort_seq_v1')
        self.assertTrue(current_outgoing(value,1792841763))
        self.assertFalse(current_outgoing({**value,'captured_at':9999999999,
                                          'native_created_at':1792841762},1792841763))
        self.assertFalse(current_outgoing({**value,'native_record_metadata_source':'unverified'},1))
        self.assertTrue(current_outgoing({'direction':'incoming'},1792841764))


    def test_resource_provider_chunks_ownership_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'messages.sqlite3')
            content = b'attachment bytes' * 30000
            class Provider:
                def fetch_resource(self, event):
                    return content
            resources = Resources(store, Provider())
            try:
                eid = store.append('self', {'message_type': 'file', 'payload': {'resource_id': 'original'}})
                with self.assertRaises(ApiError):
                    resources.acquire(eid, 'different', 'self')
                result = resources.acquire(eid, 'original', 'self')
                deadline = time.monotonic() + 3
                while result['state'] == 'running' and time.monotonic() < deadline:
                    time.sleep(.01)
                    result = resources.status(result['id'], 'self')
                self.assertEqual(result['state'], 'ready')
                with self.assertRaises(ApiError):
                    resources.status(result['id'], 'another-account')
                first = resources.status(result['id'], 'self', 0)
                self.assertEqual(len(base64.b64decode(first['data'])), 256 * 1024)
                self.assertFalse(first['eof'])
                store.clock = lambda: result['created'] + 8 * 86400
                self.assertEqual(resources.status(result['id'], 'self')['state'], 'expired')
                self.assertFalse((resources.root / result['id']).exists())
            finally:
                resources.close()
                store.close()

    def test_missing_native_provider_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'messages.sqlite3')
            resources = Resources(store, object())
            try:
                eid = store.append('self', {'message_type': 'image', 'payload': {'resource_id': 'r'}})
                result = resources.acquire(eid, 'r', 'self')
                resources.close()
                self.assertEqual(resources.status(result['id'], 'self')['state'], 'failed')
            finally:
                resources.close()
                store.close()
