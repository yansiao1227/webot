"""Hook-layer native download validation of one captured event, without sends."""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import argparse
import hashlib
import json
import sqlite3
import time
from api.native_download import download, request_spec, query
from api.errors import ApiError

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('seq', type=int)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--query', action='store_true')
    parser.add_argument('--attempt', default='validation')
    args = parser.parse_args()
    db = sqlite3.connect((ROOT / 'state/mcp-live-validation/messages.sqlite3').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT account,body FROM events WHERE seq=?', (args.seq,)).fetchone()
        event = json.loads(row['body'])
        event['event_id'] = db.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0] + ':' + str(args.seq)
        raw = db.execute('SELECT xml FROM native_resources WHERE id=? AND account=? AND conversation=?',
                         (event['payload']['resource_id'], row['account'], event['conversation_id'])).fetchone()['xml']
        event['account'] = row['account']
    finally:
        db.close()
    spec = request_spec(event, raw)
    if args.query:
        print(json.dumps(query(event, raw, args.attempt), ensure_ascii=False))
        return
    report = {'seq': args.seq, 'kind': event['message_type'], 'size': spec['size'], 'native_type': spec['file_type'],
              'checked_at': time.time(), 'prepare_only': args.prepare, 'metadata_only': args.metadata_only}
    if not args.metadata_only:
        try:
            result = download(event, raw, prepare_only=args.prepare, attempt_id=args.attempt)
        except ApiError as error:
            print(json.dumps({'passed': False, 'code': error.code, 'details': error.details}, ensure_ascii=False))
            raise SystemExit(1)
        if args.prepare:
            report['native'] = result
        else:
            report.update(downloaded_bytes=len(result), sha256=hashlib.sha256(result).hexdigest(),
                          source_md5_matches=hashlib.md5(result).hexdigest()==spec['md5'],
                          native_completion_verified=True, manual_open_for_this_download=False)
        target = ROOT / 'state/native-download-validation'
        target.mkdir(exist_ok=True)
        (target / (('prepare-' if args.prepare else 'download-') + str(args.seq) + '.json')).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
