"""解析单独采集的原生 msgsource，不检查消息正文。"""
import re
from xml.etree import ElementTree as ET

SOURCE = 'weixin41930_record_source_180'
MAX_SOURCE_BYTES = 16384


def parse_mentions(item):
    unknown = {'state': 'unknown', 'wxids': [], 'all': False, 'source': SOURCE}
    raw = item.get('source_180')
    if item.get('source_180_complete') is not True or not isinstance(raw, str):
        return unknown
    if len(raw.encode('utf-8')) > MAX_SOURCE_BYTES or '\0' in raw:
        return unknown
    if not raw:
        return {**unknown, 'state': 'known'}
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        return unknown
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        return unknown
    if root.tag != 'msgsource':
        return unknown
    nodes = list(root.iter('atuserlist'))
    if not nodes:
        return {**unknown, 'state': 'known'}
    if len(nodes) != 1 or nodes[0] not in list(root) or len(nodes[0]) or nodes[0].attrib:
        return unknown
    values = (nodes[0].text or '').split(',')
    ids, all_users = [], False
    if len(values) > 256:
        return unknown
    for value in values:
        value = value.strip()
        if not value:
            if len(values) != 1:
                return unknown
            continue
        if value == 'notify@all':
            all_users = True
        elif re.fullmatch(r'[A-Za-z0-9_-]{1,127}', value):
            if value not in ids:
                ids.append(value)
        else:
            return unknown
    return {'state': 'known', 'wxids': ids, 'all': all_users, 'source': SOURCE}
