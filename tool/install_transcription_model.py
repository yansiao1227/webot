"""显式安装固定版本本地语音识别权重的工具。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tool.transcription import DEFAULT_MODEL_DIR


REPOSITORY = 'Systran/faster-whisper-small'
REVISION = '536b0662742c02347bc0e980a01041f333bce120'
FILES = {
    'config.json': 'b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828',
    'model.bin': '3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671',
    'tokenizer.json': 'fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab',
    'vocabulary.txt': '34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913',
}


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()



def install_model(model_dir=None, *, verify_only=False):
    root = Path(model_dir).resolve() if model_dir else DEFAULT_MODEL_DIR
    if not verify_only:
        root.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = root / name
        if target.is_file() and checksum(target) == expected:
            continue
        if verify_only:
            raise ValueError(name + ': model file missing or checksum mismatch')
        temporary = root / (name + '.partial')
        request = urllib.request.Request('https://huggingface.co/' + REPOSITORY + '/resolve/' + REVISION + '/' + name,
                                         headers={'User-Agent': 'LocalTranscriptionModelInstaller'})
        with urllib.request.urlopen(request, timeout=60) as source, temporary.open('wb') as stream:
            while chunk := source.read(1024 * 1024):
                stream.write(chunk)
        if checksum(temporary) != expected:
            temporary.unlink()
            raise ValueError(name + ': model checksum mismatch')
        temporary.replace(target)
    report = {'model_dir': str(root), 'repo': REPOSITORY, 'revision': REVISION, 'sha256': FILES}
    if not verify_only:
        (root / 'source.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, help='目标模型目录，默认读取CONTENT_MODEL_DIR')
    parser.add_argument('--verify-only', action='store_true', help='只检查本机文件，不联网、不写入')
    args = parser.parse_args(argv)
    report = install_model(args.model_dir, verify_only=args.verify_only)
    print(json.dumps({'state': 'verified', **report}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
