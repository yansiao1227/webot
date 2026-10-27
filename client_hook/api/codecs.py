"""Decode attachment bytes using a pinned, locally built SILK decoder."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import wave

from .errors import ApiError

ROOT = Path(__file__).resolve().parents[1]


def silk_to_wav(data):
    if data.startswith(b'RIFF'):
        return data
    if not data.startswith((b'\x02#!SILK_V3', b'#!SILK_V3')):
        raise ApiError(503, 'voice_codec_unsupported', '未识别为SILK语音')
    executable = ROOT / 'build/codecs/silk-decoder.exe'
    pins = ROOT / 'build/codecs/pins.json'
    if not executable.is_file() or not pins.is_file():
        raise ApiError(503, 'voice_codec_missing', 'SILK解码器尚未构建')
    expected = json.loads(pins.read_text())['decoder_sha256']
    if hashlib.sha256(executable.read_bytes()).hexdigest() != expected:
        raise ApiError(503, 'voice_codec_mismatch', 'SILK解码器指纹不匹配')
    with tempfile.TemporaryDirectory(prefix='silk-', dir=ROOT / 'build/codecs') as directory:
        source, destination = Path(directory) / 'input.silk', Path(directory) / 'output.pcm'
        source.write_bytes(data)
        result = subprocess.run([str(executable), str(source), str(destination), '-Fs_API', '24000', '-quiet'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not destination.is_file() or not 0 < destination.stat().st_size <= 180 * 24000 * 2:
            raise ApiError(503, 'voice_decode_failed', 'SILK语音未能完成解码')
        pcm = destination.read_bytes()
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(pcm)
    return output.getvalue()
