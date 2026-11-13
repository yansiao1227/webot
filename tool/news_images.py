"""公开新闻图片校验与内容寻址缓存，不执行图片来源内容。"""
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from .digest_sources import read_bytes


def cache_image(url, directory, *, timeout=8, reader=None):
    from PIL import Image
    raw = reader(url) if reader else read_bytes(url, timeout=timeout)
    with Image.open(BytesIO(raw)) as picture:
        if picture.width < 300 or picture.height < 120 or picture.width * picture.height > 40_000_000:
            raise ValueError('image_dimensions')
        picture.verify()
    with Image.open(BytesIO(raw)) as picture:
        picture.seek(0)
        picture = picture.convert('RGB')
        picture.thumbnail((1600, 1600))
        for quality in (82, 65, 45):
            output = BytesIO()
            picture.save(output, format='WEBP', quality=quality)
            data = output.getvalue()
            if len(data) <= 512 * 1024:
                break
        else:
            raise ValueError('image_too_large')
        dimensions = picture.size
    digest = sha256(data).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + '.webp')
    if target.exists() and sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError('cached_image_corrupt')
    if not target.exists():
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(target)
    return {'asset_id': digest, 'width': dimensions[0], 'height': dimensions[1]}, target
