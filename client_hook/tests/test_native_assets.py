import hashlib
import importlib.util
import struct
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.native_assets import NativeAssets, decode_image
from api.store import Store
from api.errors import ApiError


class NativeAssetsTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('cryptography'), 'resource crypto dependency absent')
    def test_v2_segment_decryption_requires_exact_message_hash(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = b'0123456789abcdef'
        head, raw, tail = b'wxgf' + b'a' * 28, b'raw middle', b'original tail'
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        encrypted = encryptor.update(head + b'\x10' * 16) + encryptor.finalize()
        dat = b'\x07\x08V2\x08\x07' + struct.pack('<II', len(head), len(tail)) + b'\0'
        dat += encrypted + raw + bytes(b ^ 0xa7 for b in tail)
        expected = head + raw + tail
        hashes = {hashlib.md5(expected).hexdigest()}
        self.assertEqual(decode_image(dat, hashes, lambda: key), expected)
        with self.assertRaises(ApiError):
            decode_image(dat, {'0' * 32}, lambda: key)
        with self.assertRaises(ApiError):
            decode_image(dat, hashes, lambda: b'fedcba9876543210')
        with self.assertRaises(ApiError):
            decode_image(dat[:15], hashes, lambda: key)

    def test_cached_file_requires_native_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = root / 'wxid_self_local'
            conversation = 'wxid_peer'
            base = account / 'msg/attach' / hashlib.md5(conversation.encode()).hexdigest()
            base.mkdir(parents=True)
            data = b'original attachment bytes'
            md5 = hashlib.md5(data).hexdigest()
            path = base / 'same.txt'
            path.write_bytes(b'wrong attachment with same name')
            store = Store(root / 'events.sqlite3')
            try:
                store.db.execute('CREATE TABLE native_resources(id TEXT,account TEXT,conversation TEXT,kind TEXT,xml TEXT,created REAL)')
                store.db.execute('INSERT INTO native_resources VALUES(?,?,?,?,?,?)',
                    ('r', 'wxid_self', conversation, 'file', '<msg><appmsg><md5>' + md5 + '</md5></appmsg></msg>', 0))
                store.db.commit()
                assets = NativeAssets(store, 'wxid_self', root)
                event = {'conversation_id': conversation, 'message_type': 'file', 'payload': {'resource_id': 'r', 'name': 'same.txt'}}
                with self.assertRaises(ApiError):
                    assets.fetch(event)
                path.write_bytes(data)
                self.assertEqual(assets.fetch(event), data)
                unified = account / 'msg/file/2026-10'
                unified.mkdir(parents=True)
                path.rename(unified / path.name)
                self.assertEqual(assets.fetch(event), data)
                event['conversation_id'] = 'wxid_other'
                with self.assertRaises(ApiError):
                    assets.fetch(event)
            finally:
                store.close()

    def test_legacy_image_xor_and_unknown_encryption(self):
        clear = b'\x89PNG\r\n\x1a\nfixture bytes'
        self.assertEqual(decode_image(bytes(b ^ 0x63 for b in clear)), clear)
        with self.assertRaises(ApiError):
            decode_image(b'\x07\x08V2\x08\x07encrypted')
