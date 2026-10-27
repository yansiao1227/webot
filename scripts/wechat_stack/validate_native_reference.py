"""Read real hook HTTP events with an independent consumer; never send messages."""
import argparse
import http.client
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.wechat_stack import manage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marker', default='NATIVE_REF_20261024_01')
    parser.add_argument('--consumer', default='native-validation-20261024')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    config, token, _ = manage.configuration()
    def request(method, path, body=None):
        conn = http.client.HTTPConnection(manage.local_host(), manage.hook_port(), timeout=35)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            value = json.loads(response.read())
            if response.status != 200:
                raise RuntimeError('hook HTTP ' + str(response.status))
            return value
        finally:
            conn.close()
    request('POST', '/consumers', {'consumer_id': args.consumer, 'start': 'latest'})
    originals, quotes = {}, []
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        batch = request('GET', '/messages?consumer_id=' + args.consumer + '&timeout=25')
        for item in batch['events']:
            # Hook HTTP envelopes hold their event under data on some older versions.
            event = item.get('data', item)
            cid = event.get('conversation_id')
            if cid not in config['conversations'] or event.get('direction') != 'incoming':
                continue
            if event.get('payload', {}).get('text') == args.marker and event.get('message_id'):
                originals[(cid, event['message_id'])] = event
            if event.get('reference'):
                quotes.append(event)
        for quote in quotes:
            target = originals.get((quote['conversation_id'], quote['reference'].get('message_id')))
            if target and quote['reference'].get('source') == 'weixin41930_native_reference_v1':
                report = {'verified': True, 'scope': 'real_weixin_4.1.9.30_incoming_text_native_quote',
                    'checked_at': time.time(), 'account': config['account'],
                    'conversation_id': quote['conversation_id'],
                    'original_event_id': target.get('event_id'), 'original_message_id': target['message_id'],
                    'quote_event_id': quote.get('event_id'), 'quote_message_id': quote.get('message_id'),
                    'referenced_message_id': quote['reference']['message_id'],
                    'sender_equal': target.get('sender_id') == quote.get('sender_id'),
                    'attachments_verified': False, 'reply_sent': False}
                output = ROOT / 'client_hook/state/mcp-live-validation/native-reference-validation.json'
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(report, ensure_ascii=False), flush=True)
                return
        # Keep the successful batch unacknowledged for manual evidence inspection.
        if batch.get('ack_token'):
            request('POST', '/consumers/' + args.consumer + '/ack', {'ack_token': batch['ack_token']})
    print(json.dumps({'verified': False, 'reason': 'waiting_for_matching_real_samples'}), flush=True)


if __name__ == '__main__':
    main()
