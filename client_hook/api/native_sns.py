"""固定账号朋友圈缓存读取；密钥及明文数据库仅存在于内存。"""
import re
import xml.etree.ElementTree as ET

from .errors import ApiError


def post_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value) or int(value) >= 1 << 64:
        raise ApiError(400, 'invalid_post_id', 'post_id 必须为非零 uint64 十进制字符串')
    return int(value)


def parse_item(tid, author, xml):
    identifier = str(tid % (1 << 64))
    try:
        size = len(xml.encode('utf-8')) if isinstance(xml, str) else None
    except UnicodeError:
        size = None
    if size is None or size > 2 * 1024 * 1024:
        raise ApiError(503, 'sns_cache_item_invalid', '朋友圈缓存 XML 超过限制')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', xml, re.I):
        raise ApiError(503, 'sns_cache_item_invalid', '朋友圈缓存 XML 包含不支持的声明')
    try:
        root = ET.fromstring(xml)
        timeline = root.find('TimelineObject')
        if root.tag != 'SnsDataItem' or timeline is None:
            raise ValueError('root')
        if timeline.findtext('id') != identifier or timeline.findtext('username') != author:
            raise ValueError('identity')
        created = int(timeline.findtext('createTime', '0'))
        if created < 0:
            raise ValueError('timestamp')
    except (ET.ParseError, ValueError, TypeError):
        raise ApiError(503, 'sns_cache_item_invalid', '朋友圈缓存结构或归属校验失败') from None
    local = root.find('LocalExtraInfo')
    flag = local.findtext('like_flag') if local is not None else None
    return {'post_id': identifier, 'author_id': author, 'created_at': created,
            'text': timeline.findtext('contentDesc', ''),
            'content_type': timeline.findtext('ContentObject/type'),
            'local_like_flag': flag, 'source': 'local_cache'}


def read_timeline(account, params):
    from .native_assets import account_directory
    from .native_media import media_snapshot
    limit = params.get('limit', 20)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ApiError(400, 'invalid_parameter', 'limit 必须为 1–100 的整数')
    author = params.get('author_id')
    if author is not None and (not isinstance(author, str) or
                              not re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,120}', author)):
        raise ApiError(400, 'invalid_parameter', 'author_id 必须为 wxid')
    clauses, values = [], []
    if author:
        clauses.append('user_name=?')
        values.append(author)
    cursor = params.get('max_id', '0')
    if cursor != '0':
        unsigned = post_id(cursor)
        signed = unsigned if unsigned < 1 << 63 else unsigned - (1 << 64)
        negative = int(signed < 0)
        clauses.append('((tid<0)<? OR ((tid<0)=? AND tid<?))')
        values.extend((negative, negative, signed))
    connection = media_snapshot(account_directory(account) / 'db_storage/sns/sns.db', account)
    try:
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        # SQLite INTEGER 为有符号数，SNS ID 为 uint64，不能直接按 tid 排序。
        rows = connection.execute('SELECT tid,user_name,content FROM SnsTimeLine' + where +
                                  ' ORDER BY (tid<0) DESC,tid DESC LIMIT ?', values + [limit + 1]).fetchall()
        items = [parse_item(*row) for row in rows[:limit]]
        return {'items': items, 'has_more': len(rows) > limit,
                'next_max_id': items[-1]['post_id'] if len(rows) > limit and items else None,
                'source': 'local_cache', 'network_refreshed': False,
                'server_confirmed': False}
    finally:
        connection.close()


def validate_target(account, identifier, author):
    """执行前核对同账号精确帖子；不使用昵称或调用方正文推断作者。"""
    from .native_assets import account_directory
    from .native_media import media_snapshot
    unsigned = post_id(identifier)
    signed = unsigned if unsigned < 1 << 63 else unsigned - (1 << 64)
    connection = media_snapshot(account_directory(account) / 'db_storage/sns/sns.db', account)
    try:
        row = connection.execute('SELECT tid,user_name,content FROM SnsTimeLine WHERE tid=?', (signed,)).fetchone()
        if row is None or row[1] != author:
            raise ApiError(403, 'sns_post_scope_mismatch', '当前账号缓存未能核实此帖子与作者')
        return parse_item(*row)
    finally:
        connection.close()


def validate_friend(account, author):
    from .native_assets import account_directory
    from .native_media import media_snapshot
    connection = media_snapshot(account_directory(account) / 'db_storage/contact/contact.db', account)
    try:
        rows = connection.execute('SELECT flag FROM contact WHERE username=?', (author,)).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], int) or not rows[0][0] & 1:
            raise ApiError(403, 'sns_friend_unverified', '当前点赞适配只支持已核实的好友作者')
    finally:
        connection.close()
