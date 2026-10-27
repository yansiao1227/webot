"""Shared local ffmpeg execution helpers."""
import subprocess


def run_media(args, timeout=90):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('媒体解码失败，原资源可能尚未解密或格式不受支持')
    return result


def ffmpeg_executable():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise ValueError('缺少imageio-ffmpeg媒体解码依赖') from None


