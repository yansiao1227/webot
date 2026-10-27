"""同步标准库客户端。业务处理成功才确认；不自动重发发送请求。"""
import json
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
import uuid
from tool.settings import hook_url


class ClientError(Exception):
    def __init__(self, status, body):
        super().__init__(f'HTTP {status}: {body}')
        self.status, self.body = status, body


class Client:
    def __init__(self, base_url=None, token=None):
        self.base_url, self.token = (base_url or hook_url()).rstrip('/'), token

    def request(self, method, path, body=None, timeout=40):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        request = Request(self.base_url + path, data, headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise ClientError(error.code, json.load(error)) from None

    def status(self):
        return self.request('GET', '/client_hook/status')

    def mark_conversation_read(self, event_id):
        return self.request('POST', '/client_hook/read/mark', {'event_id': event_id})

    def conversation_read_status(self, event_id):
        return self.request('POST', '/client_hook/read/status', {'event_id': event_id})

    def send_text(self, toid, msg, request_id=None):
        request_id = request_id or uuid.uuid4().hex
        return self.request('POST', '/SendTextMsg', {'toid': toid, 'msg': msg, 'request_id': request_id})

    def operation(self, request_id):
        return self.request('GET', '/client_hook/requests/' + quote(request_id, safe=''))

    def acquire_resource(self, event_id, resource_id):
        return self.request('POST', '/client_hook/resources/acquire',
                            {'event_id': event_id, 'resource_id': resource_id})

    def resource(self, resource_id, offset=None):
        path = '/client_hook/resources/' + quote(resource_id, safe='')
        return self.request('GET', path + ('?' + urlencode({'offset': offset}) if offset is not None else ''))

    def consumer(self, consumer_id, start='earliest'):
        return self.request('POST', '/consumers', {'consumer_id': consumer_id, 'start': start})

    def messages(self, consumer_id, timeout=30, limit=100):
        return self.request('GET', '/messages?' + urlencode({'consumer_id': consumer_id, 'timeout': timeout, 'limit': limit}),
                            timeout=timeout + 10)

    def ack(self, consumer_id, token):
        return self.request('POST', '/consumers/' + quote(consumer_id, safe='') + '/ack', {'ack_token': token})

    def listen(self, consumer_id, handler, start='earliest', stop=None):
        self.consumer(consumer_id, start)
        while stop is None or not stop.is_set():
            batch = self.messages(consumer_id)
            if not batch['events']:
                if batch['capture'].get('state') != 'running':
                    raise ClientError(503, {'error': {'code': 'capture_paused'}, 'capture': batch['capture']})
                continue
            for event in batch['events']:
                handler(event)  # Exception exits without ack: batch will be redelivered.
            self.ack(consumer_id, batch['ack_token'])
