import tempfile
import time
import unittest
from unittest.mock import patch
from api.backend import NativeBackend
from api.store import Store
from api.resources import Resources
from api.read_state import ReadState, allowed
from api.errors import ApiError

class ReadTests(unittest.TestCase):
    def test_group_gate_direction_and_simulation_do_not_fake_success(self):
        account='wxid_self'
        event={'account':account,'conversation_id':'123@chatroom','direction':'incoming','message_type':'text'}
        self.assertFalse(allowed(event,account))
        event['mentions']={'state':'known','source':'weixin41930_record_source_180','wxids':[account]}
        self.assertTrue(allowed(event,account))
        self.assertFalse(allowed({**event,'direction':'outgoing'},account))
        self.assertFalse(allowed({**event,'message_type':'image'},account))
        self.assertFalse(allowed(event,'wxid_other'))

    def test_exact_event_idempotency_and_real_count_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp+'/messages.db');backend=NativeBackend();backend.profile=lambda: {'wxid':'wxid_self'}
            event={'account':'wxid_self','conversation_id':'wxid_peer','direction':'incoming',
                'message_type':'text','payload':{'text':'hello'}}
            eid=store.append('wxid_self',event)
            reads=ReadState(store,Resources(store,backend),backend)
            before={'unread_count':2,'last_clear_unread_timestamp':1}
            after={'unread_count':0,'last_clear_unread_timestamp':2}
            with patch('api.read_state.snapshot',side_effect=[before,after]),patch('hook_runtime.read_rpc.invoke',
                    return_value={'submitted':True,'error':0,'thread_error':0,'calls':1}) as invoke:
                reads.request(eid,'wxid_self');reads.close()
                self.assertEqual(reads.status(eid,'wxid_self')['state'],'confirmed')
                reads.request(eid,'wxid_self');self.assertEqual(invoke.call_count,1)
            # Unknown submission must remain unknown after restart and duplicate requests.
            eid2=store.append('wxid_self',event)
            with patch('api.read_state.snapshot',return_value=before),patch('hook_runtime.read_rpc.invoke',side_effect=TimeoutError):
                reads.request(eid2,'wxid_self');reads.close()
            reads=ReadState(store,Resources(store,backend),backend)
            with patch('hook_runtime.read_rpc.invoke') as invoke:
                self.assertEqual(reads.request(eid2,'wxid_self')['state'],'unknown');invoke.assert_not_called()
            with self.assertRaises(ApiError):reads.request(eid,'wxid_other')
            store.close()
