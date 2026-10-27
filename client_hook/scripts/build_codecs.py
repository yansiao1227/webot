"""Build the fixed SILK SDK locally; does not load or call WeChat."""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import argparse
from tool.settings import executable_setting, path_setting

import hashlib
import json
import subprocess
import tarfile
import urllib.request

REVISION = '507be6bca8ce1fb977a061481f1d79e8c610e309'
ARCHIVE_SHA256 = 'f796aea4c8fb9a5887adfaf923709cf30b9cfa99f55f3b3bcfdaa6e1d001e0c5'
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', default=executable_setting('RE_WECHAT_GCC', 'gcc'))
    args = parser.parse_args()
    source_root = ROOT / 'build/silk-source'
    source_root.mkdir(parents=True, exist_ok=True)
    archive = source_root / 'source.tar.gz'
    if not archive.exists():
        request = urllib.request.Request('https://api.github.com/repos/kn007/silk-v3-decoder/tarball/' + REVISION,
                                         headers={'User-Agent': 'webot-codec-build'})
        with urllib.request.urlopen(request, timeout=45) as source, archive.open('wb') as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('SILK source archive fingerprint mismatch')
    with tarfile.open(archive) as package:
        if any(not (source_root / member.name).resolve().is_relative_to(source_root.resolve())
               for member in package.getmembers()):
            raise ValueError('SILK source archive path traversal')
        package.extractall(source_root, filter='data')
    source = source_root / ('kn007-silk-v3-decoder-' + REVISION[:7]) / 'silk'
    output = ROOT / 'build/codecs'
    output.mkdir(parents=True, exist_ok=True)
    files = [str(p.relative_to(source)) for p in (source / 'src').glob('*.c')]
    for name in ('Decoder', 'Encoder'):
        subprocess.run([args.compiler, '-O2', '-std=c99', '-Iinterface', '-Isrc', '-Itest',
                        *files, 'test/' + name + '.c', '-o', str(output / ('silk-' + name.lower() + '.exe')), '-lm'],
                       cwd=source, check=True)
    (output / 'pins.json').write_text(json.dumps({
        'decoder_sha256': hashlib.sha256((output / 'silk-decoder.exe').read_bytes()).hexdigest(),
        'source': {'repository': 'kn007/silk-v3-decoder', 'revision': REVISION, 'sha256': ARCHIVE_SHA256}
    }, indent=2), encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
