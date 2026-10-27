"""Versioned native-message boundary. Never derive identity from payload text."""
import re
import hashlib
from xml.etree import ElementTree as ET

TYPES = {'text', 'image', 'voice', 'video', 'file', 'sticker', 'card', 'unknown'}
REFERENCE_SOURCE = 'weixin41930_native_reference_v1'


def current_outgoing(event, minimum_created_at):
    """Original creation seconds filter history loaded through the UI route.

    The receiver only reports ready after the ceiling-of-start boundary, so
    every post-ready message can pass without admitting the start second's
    historical rows. Capture time is never substituted for creation time.
    """
    if event.get('direction') != 'outgoing':
        return True
    return (event.get('native_record_metadata_source') == 'weixin41930_record_identity_v2'
            and event.get('native_message_id_source') == 'weixin41930_message_db_exact_sort_seq_v1'
            and type(event.get('native_created_at')) is int
            and event['native_created_at'] >= minimum_created_at)


def payload_xml(raw):
    if len(raw.encode('utf-8')) > 65536 or '\0' in raw or '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise ValueError('原生XML载荷无效')
    return ET.fromstring(raw)


def attachment_payload(kind, raw):
    root = payload_xml(raw)
    tags = {3: ('image', 'img'), 13: ('image', 'img'), 39: ('image', 'img'),
            34: ('voice', 'voicemsg'), 43: ('video', 'videomsg'), 44: ('video', 'videomsg'),
            62: ('video', 'videomsg'), 47: ('sticker', 'emoji')}
    if kind in tags:
        message_type, tag = tags[kind]
        node = root if root.tag == tag else root.find(tag)
        if node is None:
            raise ValueError('原生类型与载荷结构不匹配')
        size = next((node.get(k) for k in ('length', 'length1', 'totallen') if node.get(k)), None)
        return message_type, {'size': int(size)} if size and size.isdigit() else {}
    if kind == 49:
        apps = root.findall('appmsg') if root.tag == 'msg' else [root] if root.tag == 'appmsg' else []
        if len(apps) != 1:
            raise ValueError('应用消息结构缺失或歧义')
        app = apps[0]
        subtype = app.findtext('type')
        if subtype == '57':
            return 'reference', parse_reference_xml(raw)
        return ('file' if subtype == '6' else 'card'), {'name': app.findtext('title') or '', 'subtype': subtype}
    if kind in (42, 66, 67):
        return 'card', {'name': root.get('nickname', '')}
    raise ValueError('原生类型不受支持')


def parse_reference_xml(raw):
    """Parse a native appmsg payload, not arbitrary text. Caller verifies kind."""
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536 or '\0' in raw:
        raise ValueError('引用载荷无效或超限')
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise ValueError('引用载荷不允许DTD或实体')
    root = ET.fromstring(raw)
    app = root.find('appmsg') if root.tag == 'msg' else root if root.tag == 'appmsg' else None
    if app is None or app.findtext('type') != '57':
        raise ValueError('不是原生引用消息')
    refs = app.findall('refermsg')
    titles = app.findall('title')
    if len(refs) != 1 or len(titles) != 1:
        raise ValueError('引用结构缺失或歧义')
    ids = refs[0].findall('svrid')
    if len(ids) != 1 or not re.fullmatch(r'[1-9][0-9]{0,19}', ids[0].text or ''):
        raise ValueError('引用目标标识无效')
    return {'text': titles[0].text or '', 'reference': {
        'source': REFERENCE_SOURCE, 'message_id': ids[0].text}}


def native_event(item, instance):
    """Parse the fixed native profile; unknown kinds stay explicit."""
    a, b, account = item['endpoint_18'], item['endpoint_38'], item['self_id']
    incoming = b == account and a != account
    outgoing = a == account and b != account
    if not incoming and not outgoing:
        raise ValueError('原生会话方向不明确')
    peer = a if incoming else b
    body = item['body']
    kind = item['raw_kind']
    event = {'message_type': 'text' if kind == 1 else 'unknown',
             'payload': {'text': body} if kind == 1 else {'raw_kind': kind},
             'conversation_id': peer, 'fromid': a, 'toid': b,
             'direction': 'incoming' if incoming else 'outgoing', 'source': 'live',
             'client_instance': instance, 'generation': item['generation'], 'seq': item['seq']}
    candidate = item.get('raw_108')
    client_id = item.get('native_client_id_278')
    if (outgoing and item.get('native_identity_profile') in
            ('weixin41930_record_client278_v1', 'weixin41930_record_identity_v2') and
            isinstance(client_id, str) and 1 <= len(client_id) <= 128 and
            all(32 < ord(c) < 127 for c in client_id)):
        event['native_client_id'] = client_id
        event['native_client_id_source'] = 'weixin41930_record_client278_v1'
    if item.get('native_identity_profile') == 'weixin41930_record_identity_v2':
        # +134 was a misidentified diagnostic field. Never use it as time;
        # the provider resolves create_time from the exact native DB row.
        sort_seq = item.get('raw_110')
        if (isinstance(sort_seq,str) and re.fullmatch(r'[1-9][0-9]{0,19}',sort_seq)
                and int(sort_seq)<1<<64):
            event.update(native_sort_seq=sort_seq,
                         native_record_metadata_source='weixin41930_record_identity_v2')
    if isinstance(candidate, str) and re.fullmatch(r'[1-9][0-9]{0,19}', candidate):
        event['native_message_id_candidate'] = candidate
        # Fixed-build native refermsg parser 0xa3b780 reads svrid into its
        # field+0x20, then copies it to allocation+0x118 = record+0x108.
        if item.get('semantic_profile') == 'weixin-4.1.9.30-record-v2':
            event['message_id'] = candidate
    if not peer.endswith('@chatroom'):
        event['sender_id'] = a
    elif incoming:
        actor = item.get('actor_candidate_58')
        if isinstance(actor, str) and re.fullmatch(r'wxid_[A-Za-z0-9_-]+', actor) and body.startswith(actor + ':\n'):
            event['sender_id'] = actor
            if kind == 1:
                event['payload']['text'] = body[len(actor) + 2:]
            else:
                body = body[len(actor) + 2:]
    elif outgoing:
        event['sender_id'] = account
    if kind != 1:
        try:
            message_type, payload = attachment_payload(kind, body)
            if message_type == 'reference':
                event.update(message_type='text', payload={'text': payload['text']}, reference=payload['reference'])
            else:
                resource_id = hashlib.sha256((account + '\0' + peer + '\0' + str(candidate) + '\0' + body).encode()).hexdigest()
                event.update(message_type=message_type, payload={**payload, 'resource_id': resource_id})
                event['_native_resource'] = {'id': resource_id, 'xml': body, 'kind': message_type}
        except (ValueError, ET.ParseError):
            event['_native_resource'] = {'id': hashlib.sha256((instance + ':' + str(item['seq'])).encode()).hexdigest(),
                                         'xml': body, 'kind': 'unknown'}
    # Old capture profiles keep only the candidate and cannot grant identity.
    return event
