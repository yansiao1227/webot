import hashlib
import sqlite3
import unittest
from unittest.mock import patch
from api.native_identity import outgoing_rows,enrich_outgoing


class NativeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.chat='123@chatroom';self.table='Msg_'+hashlib.md5(self.chat.encode()).hexdigest()
        self.db.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)')
        self.db.executemany('INSERT INTO Name2Id VALUES(?)',[('wxid_self',),('wxid_other',)])
        self.db.execute(f'CREATE TABLE "{self.table}"(local_id INTEGER,server_id INTEGER,sort_seq INTEGER,create_time INTEGER,real_sender_id INTEGER,local_type INTEGER)')
        self.db.execute(f'INSERT INTO "{self.table}" VALUES(1,123,1792847037000,1792847037,1,1)')

    def tearDown(self):
        self.db.close()

    def test_exact_row_and_scope_without_content_reads(self):
        result=outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000',1792847037)[0]
        self.assertEqual(result['message_id'],'123')
        self.assertEqual(result['native_local_id'],'1')
        self.assertEqual(outgoing_rows(self.db,'wxid_self','124@chatroom','1792847037000'),[])
        self.assertEqual(outgoing_rows(self.db,'wxid_self',self.chat,'1792847037001'),[])
        with self.assertRaises(ValueError):outgoing_rows(self.db,'wxid_other',self.chat,'1792847037000')
        with self.assertRaises(ValueError):outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000',1792847036)

    def test_pending_and_ambiguity_never_guess_by_creation(self):
        self.db.execute(f'UPDATE "{self.table}" SET server_id=0')
        self.assertEqual(outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000')[0]['native_message_id_state'],'pending')
        self.db.execute(f'INSERT INTO "{self.table}" VALUES(2,456,1792847037000,1792847038,1,1)')
        with self.assertRaises(ValueError):outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000',1792847037)

    def test_lookup_failure_preserves_accepted_send(self):
        with patch('api.native_identity.outgoing_metadata',side_effect=TimeoutError):
            result=enrich_outgoing({'state':'accepted','native_sort_seq':'1792847037000'},'wxid_self',self.chat)
        self.assertEqual(result['state'],'accepted')
        self.assertEqual(result['native_message_id_state'],'unavailable')
        self.assertNotIn('message_id',result)

    def test_native_app_type_and_reference_are_not_plain_send_text(self):
        self.db.execute(f'UPDATE "{self.table}" SET local_type=?',((6<<32)|49,))
        self.assertEqual(outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000',message_type='file')[0]['message_id'],'123')
        with self.assertRaises(ValueError):outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000')
        self.db.execute(f'UPDATE "{self.table}" SET local_type=?',((57<<32)|49,))
        self.assertEqual(outgoing_rows(self.db,'wxid_self',self.chat,'1792847037000',message_type='reference')[0]['message_id'],'123')
