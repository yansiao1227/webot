"""先把消息作为本地业务结果持久保存，再由 SDK 确认消费。"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tool.settings import hook_url
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=hook_url())
    parser.add_argument('--token')
    parser.add_argument('--consumer-id', default='example-agent')
    parser.add_argument('--inbox', type=Path, default=Path(__file__).resolve().parents[1] / 'state/agent-inbox.sqlite3')
    args = parser.parse_args()
    args.inbox.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.inbox) as db:
        db.execute('CREATE TABLE IF NOT EXISTS inbox(event_id TEXT PRIMARY KEY, event TEXT NOT NULL)')
        def handle(event):
            with db:
                db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?)',
                           (event['event_id'], json.dumps(event, ensure_ascii=False)))
            # This local database insert is the business action. For external
            # side effects, use a transactional outbox or the service's own
            # idempotency key; console output is not a transaction boundary.
        Client(args.url, args.token).listen(args.consumer_id, handle)


if __name__ == '__main__':
    main()
