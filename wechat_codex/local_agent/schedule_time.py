"""无第三方依赖的北京时间规则；持久化时间均为UTC时间戳。"""
from datetime import datetime, timedelta, timezone
import math
import re

BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')


def clock(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('时间必须为HH:MM')
    return tuple(map(int, value.split(':')))


def normalize(kind, value, now):
    if not isinstance(value, str):
        raise ValueError('时间规则必须为字符串')
    if kind == 'once':
        match = re.fullmatch(r'\+([1-9]\d*)([smhd])', value)
        if match:
            at = now + int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
        else:
            dt = datetime.fromisoformat(value)
            at = (dt if dt.tzinfo else dt.replace(tzinfo=BEIJING)).timestamp()
        if not math.isfinite(at) or at <= now:
            raise ValueError('一次性时间必须在未来')
        datetime.fromtimestamp(at, BEIJING)  # 拒绝超出可表示日期的时间。
        return {'type': kind, 'run_at': at}
    if kind == 'interval':
        if not re.fullmatch(r'\d+', value) or not 60 <= int(value) <= 366 * 86400:
            raise ValueError('间隔必须为60至31622400秒')
        return {'type': kind, 'seconds': int(value), 'anchor': now}
    if kind == 'daily':
        hour, minute = clock(value)
        return {'type': kind, 'hour': hour, 'minute': minute}
    if kind == 'weekly':
        match = re.fullmatch(r'([1-7]) (\d\d:\d\d)', value)
        if not match:
            raise ValueError('每周规则为1至7加HH:MM，1表示周一')
        hour, minute = clock(match[2])
        return {'type': kind, 'weekday': int(match[1]) - 1, 'hour': hour, 'minute': minute}
    raise ValueError('仅支持once、interval、daily、weekly')


def next_time(rule, after):
    kind = rule['type']
    if kind == 'once':
        return rule['run_at'] if rule['run_at'] > after else None
    if kind == 'interval':
        return rule['anchor'] + max(1, math.floor((after - rule['anchor']) / rule['seconds']) + 1) * rule['seconds']
    dt = datetime.fromtimestamp(after, BEIJING)
    candidate = dt.replace(hour=rule['hour'], minute=rule['minute'], second=0, microsecond=0)
    if kind == 'weekly':
        candidate += timedelta(days=(rule['weekday'] - dt.weekday()) % 7)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=7 if kind == 'weekly' else 1)
    return candidate.timestamp()


def display(at):
    return datetime.fromtimestamp(at, BEIJING).isoformat() if at is not None else None
