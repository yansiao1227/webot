import hashlib
import hmac
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.native_media import _checksum, valid_page, hmac_key, wal_pages, voice_bytes, PAGE
from api.errors import ApiError


class NativeMediaTests(unittest.TestCase):
    def test_authenticated_page_is_bound_to_salt_and_page_number(self):
        key, salt = b'k' * 32, b's' * 16
        page = salt + b'e' * (PAGE - 80 - 16) + b'i' * 16
        digest = hmac.new(hmac_key(key, salt), page[16:] + struct.pack('<I', 1), hashlib.sha512).digest()
        page += digest
        self.assertTrue(valid_page(page, 1, key, salt))
        self.assertFalse(valid_page(page, 2, key, salt))
        self.assertFalse(valid_page(page, 1, b'x' * 32, salt))

    def test_wal_ignores_uncommitted_and_corrupted_tail(self):
        salt = b'walsalts'
        head = struct.pack('>6I', 0x377f0682, 3007000, PAGE, 0, 17, 23)
        salt = head[16:24]
        checksum = _checksum(head, '<')
        wal = head + struct.pack('>II', *checksum)
        payload = b'a' * PAGE
        frame = struct.pack('>II', 1, 1)
        checksum = _checksum(frame + payload, '<', checksum)
        wal += frame + salt + struct.pack('>II', *checksum) + payload
        committed_end = len(wal)
        frame = struct.pack('>II', 2, 0)
        checksum = _checksum(frame + b'b' * PAGE, '<', checksum)
        wal += frame + salt + struct.pack('>II', *checksum) + b'b' * PAGE
        self.assertEqual(wal_pages(wal), ({1: payload}, 1))
        corrupted = bytearray(wal)
        corrupted[committed_end + 30] ^= 1
        self.assertEqual(wal_pages(corrupted), ({1: payload}, 1))

    def test_voice_query_is_bound_to_both_chat_and_native_message(self):
        def snapshot(*args):
            db = sqlite3.connect(':memory:')
            db.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,svr_id INTEGER,data_index TEXT,voice_data BLOB)')
            db.executemany('INSERT INTO Name2Id VALUES(?)', [('wxid_peer',), ('wxid_other',)])
            db.executemany('INSERT INTO VoiceInfo VALUES(?,?,?,?)',
                [(1, 123, '0', b'\x02#!SILK_V3correct'), (2, 123, '0', b'\x02#!SILK_V3other')])
            return db
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'db_storage/message'
            root.mkdir(parents=True)
            (root / 'media_0.db').touch()
            event = {'message_id': '123', 'conversation_id': 'wxid_peer', 'payload': {'size': 17}}
            with patch('api.native_media.media_snapshot', side_effect=snapshot):
                self.assertEqual(voice_bytes(directory, event, 'wxid_self'), b'\x02#!SILK_V3correct')
                event['message_id'] = '124'
                with self.assertRaises(ApiError):
                    voice_bytes(directory, event, 'wxid_self')
                event['message_id'] = '123'
                event['conversation_id'] = 'wxid_missing'
                with self.assertRaises(ApiError):
                    voice_bytes(directory, event, 'wxid_self')
