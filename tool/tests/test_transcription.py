import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave
import numpy  # 临时替换 sys.modules 前先加载原生扩展。

from tool import transcription, install_transcription_model as installer
from tool.content_reading import media


def write_wav(path, *, seconds=1, rate=16000, channels=1):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b'\x00\x00' * (seconds * rate * channels))


class TranscriptionTests(unittest.TestCase):
    def test_pcm_recognition_preserves_segments_and_loads_only_local_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / 'model'
            model_dir.mkdir()
            (model_dir / 'model.bin').write_bytes(b'weights')
            path = root / 'audio.wav'
            write_wav(path)
            segment = SimpleNamespace(start=0.1, end=0.8, text=' 测试成功', avg_logprob=-0.2)
            recognizer = Mock()
            recognizer.transcribe.side_effect = lambda *a, **k: (iter([segment]),
                SimpleNamespace(language='zh', language_probability=0.99))
            constructor = Mock(return_value=recognizer)
            with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=constructor)}), \
                 patch.object(transcription, '_model', None), patch.object(transcription, '_model_dir', None):
                first = transcription.transcribe_pcm(path, model_dir)
                second = transcription.transcribe_pcm(path, model_dir)
            self.assertEqual(first, second)
            self.assertEqual(first['transcript'], '测试成功')
            self.assertEqual(first['transcription_segments'][0]['start'], 0.1)
            constructor.assert_called_once_with(str(model_dir), device='cpu', compute_type='int8',
                                                cpu_threads=4, local_files_only=True)
            self.assertIs(media.transcribe, transcription.transcribe_pcm)

    def test_real_ffmpeg_conversion_limits_audio_and_does_not_overwrite_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'audio.wav'
            write_wav(source, seconds=181, rate=8000)
            before = source.read_bytes()
            def recognize(path, model_dir):
                with wave.open(str(path)) as stream:
                    self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (1, 2, 16000))
                    self.assertEqual(stream.getnframes(), 180 * 16000)
                return {'transcript': 'mock recognition'}
            with patch.object(transcription, 'transcribe_pcm', side_effect=recognize):
                result = transcription.transcribe_audio(source, output_dir=root)
            self.assertEqual(result['audio_limit_seconds'], 180)
            self.assertNotEqual(result['audio'], str(source))
            self.assertEqual(source.read_bytes(), before)

    def test_missing_weights_and_wrong_pcm_format_do_not_start_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'audio.wav'
            write_wav(path, rate=8000)
            constructor = Mock()
            with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=constructor)}):
                with self.assertRaisesRegex(ValueError, '模型尚未安装'):
                    transcription.transcribe_pcm(path, root)
                (root / 'model.bin').write_bytes(b'weights')
                with self.assertRaisesRegex(ValueError, '16kHz'):
                    transcription.transcribe_pcm(path, root)
            constructor.assert_not_called()

    def test_installer_verify_only_checks_local_hashes_without_network_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b'local model weights'
            (root / 'model.bin').write_bytes(data)
            with patch.object(installer, 'FILES', {'model.bin': hashlib.sha256(data).hexdigest()}), \
                 patch.object(installer.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')):
                report = installer.install_model(root, verify_only=True)
                self.assertEqual(report['model_dir'], str(root))
                self.assertFalse((root / 'source.json').exists())
                (root / 'model.bin').write_bytes(b'corrupt')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    installer.install_model(root, verify_only=True)

    def test_standalone_entrypoint_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(transcription.__file__).with_name('transcribe_audio.py')
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(entry), '--path', 'missing.wav'],
                                    cwd=directory, capture_output=True, encoding='utf-8', timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)['state'], 'unavailable')
