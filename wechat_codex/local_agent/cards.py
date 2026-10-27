"""提取分享元数据及候选来源，收到消息时不主动获取。"""
from xml.etree import ElementTree as ET


from tool.content_reading.urls import public_url


def extract(raw):
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise ValueError('卡片包含不允许的XML结构')
    root = ET.fromstring(raw)
    app = root if root.tag == 'appmsg' else root.find('./appmsg')
    if app is None:
        # 联系人或商务名片默认没有来源网站。
        return {'card_metadata': {k: v for k, v in root.attrib.items()
                                 if k in {'nickname', 'username', 'alias', 'certinfo'}},
                'source_urls': [], 'media_urls': []}
    values = {key: app.findtext('./' + key, default='') for key in
              ('title', 'des', 'type', 'url', 'appname')}
    values['publisher'] = app.findtext('./appinfo/appname', default='')
    source = public_url(values['url'])
    # 视频号通用 appmsg.url 常是微信升级或帮助页面，并非视频内容页。
    if values['type'] == '51':
        source = None
    media = []
    feed = app.find('./finderFeed')
    if feed is not None:
        values['finder'] = {key: feed.findtext('./' + key, default='') for key in
                            ('nickname', 'desc', 'objectId', 'objectNonceId')}
        for node in feed.findall('./mediaList/media'):
            item = {key: node.findtext('./' + key, default='') for key in
                    ('mediaType', 'duration', 'width', 'height')}
            url = public_url(node.findtext('./url'))
            if url:
                media.append({**item, 'url': url})
    return {'card_kind': 'app-message', 'card_metadata': values, 'source_urls': [source] if source else [],
            'media_urls': media}
