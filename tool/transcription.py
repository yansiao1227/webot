"""Local CPU speech recognition for audio files; never downloads model weights."""
from pathlib import Path
import threading
import wave

from .settings import path_setting, cache_directory
from .media_runtime import ffmpeg_executable, run_media

DEFAULT_MODEL_DIR = path_setting('CONTENT_MODEL_DIR', 'tool/state/models/faster-whisper-small')
MAX_SECONDS = 180
_model_lock = threading.Lock()
_model = None
_model_dir = None


def transcribe_pcm(path, model_dir=None):
    """Recognize up to 180 seconds of 16-kHz mono signed 16-bit WAV."""
    global _model, _model_dir
    model_dir = Path(model_dir).resolve() if model_dir else DEFAULT_MODEL_DIR
    if not (model_dir / 'model.bin').is_file():
        raise ValueError('本地语音转写模型尚未安装')
    from faster_whisper import WhisperModel
    import numpy as np
    with wave.open(str(path), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16000):
            raise ValueError('语音转写输入必须是16kHz单声道16位PCM')
        frames = stream.readframes(MAX_SECONDS * 16000)
    if not frames:
        raise ValueError('没有可读取的音轨')
    audio = np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768.0
    with _model_lock:
        if _model is None or _model_dir != model_dir:
            model = WhisperModel(str(model_dir), device='cpu', compute_type='int8', cpu_threads=4,
                                 local_files_only=True)
            _model, _model_dir = model, model_dir
        segments, info = _model.transcribe(audio, beam_size=5, vad_filter=True)
        segments = list(segments)
    return {'transcript': ''.join(segment.text for segment in segments).strip(),
            'transcription_model': 'Systran/faster-whisper-small', 'transcription_language': info.language,
            'language_probability': info.language_probability,
            'transcription_segments': [{'start': segment.start, 'end': segment.end, 'text': segment.text,
                                        'avg_logprob': segment.avg_logprob} for segment in segments]}


def transcribe_audio(path, *, output_dir=None, model_dir=None):
    """Normalize a local audio/video file and return timestamped transcription."""
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('输入必须为本机文件')
    output = Path(output_dir).resolve() if output_dir else cache_directory('content-reading-')
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'audio.wav'
    if target.resolve() == path:
        target = output / 'normalized-audio.wav'
    run_media([ffmpeg_executable(), '-nostdin', '-y', '-v', 'error', '-i', str(path), '-vn',
               '-ac', '1', '-ar', '16000', '-t', str(MAX_SECONDS), str(target)])
    if not target.exists() or target.stat().st_size <= 44:
        raise ValueError('没有可读取的音轨')
    return {'audio': str(target.resolve()), 'audio_limit_seconds': MAX_SECONDS,
            **transcribe_pcm(target, model_dir)}
