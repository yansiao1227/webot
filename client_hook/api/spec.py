"""与第一阶段路由一致的 OpenAPI 3.1 描述。"""

from tool.settings import hook_url

STRING = {'type': 'string'}
CONSUMER_ID = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,63}$'}
START = {'type': 'string', 'enum': ['earliest', 'latest'], 'default': 'earliest'}
GENERIC = {'type': 'object', 'additionalProperties': True}
SUCCESS = (200, 202)
ERRORS = (400, 401, 404, 409, 410, 413, 429, 502, 503)


def ref(name):
    return {'$ref': '#/components/schemas/' + name}


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required)}


def path_param(name):
    return {'name': name, 'in': 'path', 'required': True, 'schema': CONSUMER_ID}


def query_param(name, schema, required=False):
    value = {'name': name, 'in': 'query'}
    if required:
        value['required'] = True
    value['schema'] = schema
    return value


def response_for(code, schema):
    if code == 200:
        description = '成功'
    elif code == 202:
        description = '异步处理中'
    else:
        description = '明确错误'
    body = schema if code in SUCCESS and schema is not None else ref('Error')
    return {'description': description,
            'content': {'application/json': {'schema': body}}}


def operation(summary, *, request=None, response=None, parameters=None):
    value = {'summary': summary,
             'responses': {str(code): response_for(code, response)
                           for code in SUCCESS + ERRORS}}
    if request is not None:
        value['requestBody'] = {'required': True,
                                 'content': {'application/json': {'schema': request}}}
    if parameters:
        value['parameters'] = parameters
    return value


def _schemas():
    event = obj({
        'event_id': STRING,
        'account': STRING,
        'message_type': {'enum': ['text', 'image', 'voice', 'video', 'file', 'sticker', 'card', 'unknown']},
        'payload': obj({'text': STRING, 'resource_id': STRING, 'name': STRING, 'mime': STRING}),
        'message_id': STRING,
        'native_client_id': {'type':'string','minLength':1,'maxLength':128},
        'native_client_id_source': {'const':'weixin41930_record_client278_v1'},
        'native_sort_seq': {'type':'string','pattern':'^[1-9][0-9]{0,19}$'},
        'native_created_at': {'type':'integer','minimum':1},
        'native_record_metadata_source': {'const':'weixin41930_record_identity_v2'},
        'native_local_id': STRING,
        'native_message_id_source': {'const':'weixin41930_message_db_exact_sort_seq_v1'},
        'native_message_id_state': {'enum':['ready','pending','unavailable']},
        'sender_id': STRING,
        'reference': obj({'message_id': STRING, 'source': {'const': 'weixin41930_native_reference_v1'}},
                         ['message_id', 'source']),
        'conversation_id': STRING,
        'fromid': STRING,
        'toid': STRING,
        'captured_at': {'type': 'number'},
        'direction': {'enum': ['incoming', 'outgoing']},
        'source': {'const': 'live'},
        'mentions': obj({'state': {'enum': ['known', 'unknown']},
                         'wxids': {'type': 'array', 'items': STRING},
                         'all': {'type': 'boolean'},
                         'source': {'const': 'weixin41930_record_source_180'}},
                        ['state', 'wxids', 'all', 'source']),
    }, ['event_id', 'account', 'message_type', 'payload', 'conversation_id'])
    return {
        'Error': obj({
            'ret': {'type': 'integer'},
            'error': obj({'code': STRING, 'message': STRING, 'retryable': {'type': 'boolean'}}),
        }),
        'Event': event,
        'Operation': obj({
            'request_id': CONSUMER_ID,
            'account': STRING,
            'state': {'enum': ['queued', 'running', 'accepted', 'failed', 'unknown']},
            'delivery_confirmed': {'const': False},
            'result': {'type': ['object', 'null']},
        }),
        'Batch': obj({
            'events': {'type': 'array', 'items': ref('Event')},
            'next_cursor': STRING,
            'ack_token': {'type': ['string', 'null']},
            'has_more': {'type': 'boolean'},
            'capture': GENERIC,
        }),
    }


def _paths():
    text = obj({
        'toid': STRING,
        'msg': {'type': 'string', 'description': '非空 UTF-8，最多 64 KiB，无 NUL'},
        'wxidorgid': STRING,
        'content': STRING,
        'request_id': CONSUMER_ID,
    })
    return {
        '/SendTextMsg': {'post': operation(
            '发送文本；成功仅表示本地接受；支持 wxidorgid/content 别名',
            request=text, response=ref('Operation'))},
        '/IsLoginStatus': {'post': operation(
            '读取已验证登录状态；无法确认返回 503',
            request=GENERIC, response=GENERIC)},
        '/GetSelfProfile': {'post': operation(
            '自身资料；未核实的可选资料为 null',
            request=GENERIC, response=GENERIC)},
        '/client_hook/status': {'get': operation(
            '查询采集状态、覆盖边界与能力',
            response=GENERIC)},
        '/client_hook/requests/{request_id}': {'get': operation(
            '查询同一次发送；unknown 不能自动重发',
            response=ref('Operation'), parameters=[path_param('request_id')])},
        '/consumers': {'post': operation(
            '创建或取得持久消费者',
            request=obj({'consumer_id': CONSUMER_ID, 'start': START}, ['consumer_id']),
            response=GENERIC)},
        '/consumers/{consumer_id}': {'get': operation(
            '查询独立消费进度',
            response=GENERIC, parameters=[path_param('consumer_id')])},
        '/consumers/{consumer_id}/ack': {'post': operation(
            '成功处理整批后确认；重复 token 幂等',
            request=obj({'ack_token': CONSUMER_ID}, ['ack_token']),
            response=GENERIC, parameters=[path_param('consumer_id')])},
        '/consumers/{consumer_id}/reset': {'post': operation(
            '显式重置并记录跳过范围',
            request=obj({'start': START}, ['start']),
            response=GENERIC, parameters=[path_param('consumer_id')])},
        '/messages': {'get': operation(
            '有消息立即返回，无消息最长等待 30 秒；未确认批次重投',
            response=ref('Batch'),
            parameters=[
                query_param('consumer_id', CONSUMER_ID, required=True),
                query_param('timeout', {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 30}),
                query_param('limit', {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 100}),
                query_param('cursor', STRING),
            ])},
        '/client_hook/callback/resume': {'post': operation(
            '显式恢复失败的回调批次，可能重投',
            request=GENERIC, response=GENERIC)},
        '/client_hook/callback/reset': {'post': operation(
            '回调过期或换号时显式重置，必须先暂停；之后再 resume',
            request=obj({'start': START}, ['start']), response=GENERIC)},
        '/openapi.json': {'get': operation('OpenAPI 描述', response=GENERIC)},
    }


def specification():
    paths = _paths()
    for route in ('mark', 'status'):
        paths['/client_hook/read/' + route] = {'post': operation(
            '原生会话已读' if route == 'mark' else '查询原已读操作，不重新提交',
            request=obj({'event_id': STRING}, ['event_id']), response=GENERIC)}
    paths['/client_hook/resources/acquire'] = {'post': operation('获取原事件的资源；原生提供者未适配时明确失败',
        request=obj({'event_id': STRING, 'resource_id': STRING}, ['event_id', 'resource_id']), response=GENERIC)}
    paths['/client_hook/resources/{resource_id}'] = {'get': operation('资源状态或256KiB内容分块', response=GENERIC,
        parameters=[{'name': 'resource_id', 'in': 'path', 'required': True, 'schema': STRING},
                    query_param('offset', {'type': 'integer', 'minimum': 0})])}
    return {
        'openapi': '3.1.0',
        'info': {'title': 'client_hook 文本收发', 'version': '0.1.0'},
        'paths': paths,
        'servers': [{'url': hook_url()}],
        'security': [{'bearerAuth': []}, {}],
        'components': {
            'securitySchemes': {'bearerAuth': {'type': 'http', 'scheme': 'bearer'}},
            'schemas': _schemas(),
        },
        'x-later-phase': [
            'SendTextMsg_NoSrc', 'SendAtMsg_NoSrc', 'SendFileMsg', 'SendImgMsg',
            'SendImgMsg_NoSrc', 'SendFileMsg_NoSrc', 'ForwardXMLMsg', 'SendVoiceMsg',
            'ForwardVoiceMsg', 'SendPatMsg', 'SendFinderMsg_NoSrc', 'SendCardMsg_NoSrc',
            'upload', 'nontext_receive', 'download', 'decode', 'history',
        ],
    }
