"""只读公网采集探测：保存统计，不调用模型、网站写接口或微信。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .collectors import collect, FEEDS, hsi_values, hsi_time
from tool.digest_sources import read_bytes, parse_feed


def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    records, lock = [], threading.Lock()
    feed_urls = {item['url']: category for category, items in FEEDS.items() for item in items}
    def reader(url):
        started = time.monotonic()
        record = {'host': urlsplit(url).hostname, 'path': urlsplit(url).path, 'state': 'unavailable'}
        series = parse_qs(urlsplit(url).query).get('seriesCode')
        if series:
            record['series'] = series[0]
        try:
            raw = read_bytes(url)
            record.update(state='read', size=len(raw))
            if url in feed_urls:
                record.update(category=feed_urls[url], rss_items=len(parse_feed(raw)))
            return raw
        except Exception as error:
            record['error'] = type(error).__name__
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 3)
            with lock:
                records.append(record)
    packet = collect(now.date().isoformat(), now.isoformat(), reader=reader)
    try:
        industry = hsi_values('industry', reader)
        hk_industry = {'rows': len(industry), 'dates': sorted({hsi_time(x).date().isoformat() for x in industry}),
                       'closing_ready': all(hsi_time(x).strftime('%H:%M') >= '16:10' for x in industry)}
    except (ValueError, KeyError, OSError):
        hk_industry = {'rows': 0, 'dates': [], 'closing_ready': False}
    report = {'date': packet['date'], 'cutoff_at': packet['cutoff_at'],
              'news_with_body': {key: len(value) for key, value in packet['news'].items()},
              'markets': {key: {'state': value['status'], 'trade_date': value['trade_date'],
                               'indices': len(value['indices']), 'gainers': len(value['gainers']),
                               'losers': len(value['losers']), 'missing': value['missing']}
                          for key, value in packet['markets'].items()},
              'warnings': packet['warnings'], 'requests': records, 'hsi_official_industry': hk_industry,
              'model_calls': 0, 'website_writes': 0, 'mcp_writes': 0}
    target = Path(__file__).parent / 'state' / 'collection-probe.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('date', 'news_with_body', 'markets', 'model_calls', 'website_writes', 'mcp_writes')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
