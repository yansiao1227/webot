"""日报公开来源采集；来源不可用时保留缺项，不推断或补造行情。"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date as Date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode

from tool.digest_sources import read_bytes, read_with_retry, parse_feed, article_text, article_image, article_image_evidence, canonical_url, timestamp
from .content import parse_time

AI_TITLE_KEYWORDS = ['AI', 'LLM', 'GPT', 'ChatGPT', 'Claude', 'Gemini', 'DeepSeek', 'Copilot',
                     'Anthropic', 'OpenAI', '人工智能', '大模型', '智能体', '机器学习', '深度学习',
                     '具身智能', 'Vibe Coding', 'agent', 'agentic']

FEEDS = {
    'ai': [
        {'name': 'OpenAI News', 'url': 'https://openai.com/news/rss.xml'},
        {'name': 'Google AI', 'url': 'https://blog.google/innovation-and-ai/technology/ai/rss/'},
        {'name': 'MIT Artificial Intelligence', 'url': 'https://news.mit.edu/rss/topic/artificial-intelligence2'},
        {'name': 'Hugging Face Blog', 'url': 'https://huggingface.co/blog/feed.xml'},
        {'name': 'NVIDIA Generative AI', 'url': 'https://blogs.nvidia.com/blog/category/generative-ai/feed/'},
        {'name': 'Microsoft Cloud AI（标题筛选）', 'url': 'https://www.microsoft.com/en-us/ai/blog/feed/',
         'title_keywords': AI_TITLE_KEYWORDS},
        {'name': '量子位AI（标题筛选）', 'url': 'https://www.qbitai.com/feed', 'title_keywords': AI_TITLE_KEYWORDS},
    ],
    'society': [
        {'name': '中国新闻网时政', 'url': 'https://www.chinanews.com.cn/rss/china.xml'},
        {'name': '中国新闻网社会', 'url': 'https://www.chinanews.com.cn/rss/society.xml'},
        {'name': '香港政府新闻', 'url': 'https://www.news.gov.hk/tc/common/html/topstories.rss.xml'},
        {'name': '香港电台本地', 'url': 'https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml'},
        {'name': '香港电台大中华', 'url': 'https://rthk.hk/rthk/news/rss/c_expressnews_greaterchina.xml'},
    ],
}
INDEX_CODES = {'a': [('1.000001', '上证指数'), ('0.399001', '深证成指'), ('0.399006', '创业板指'),
                     ('1.000300', '沪深300'), ('1.000688', '科创50')],
               'hk': [('100.HSI', '恒生指数'), ('100.HSCEI', '恒生国企指数'), ('100.HSTECH', '恒生科技指数')]}
EASTMONEY_KLINE = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
EASTMONEY_SECTORS = 'https://push2.eastmoney.com/api/qt/clist/get'
HSI_METADATA = 'https://www.hsi.com.hk/api/wsit-hsi-ddoc-ea-public-website-proxy/v1/product-data/pub/series/metadata/v2'
HSI_INDUSTRIES = {'00011.01': '能源业', '00011.02': '原材料业', '00011.03': '工业',
                  '00011.06': '电讯业', '00011.07': '公用事业', '00011.08': '金融业',
                  '00011.09': '地产建筑业', '00011.10': '资讯科技业', '00011.11': '综合企业',
                  '00011.12': '非必需性消费', '00011.13': '必需性消费', '00011.14': '医疗保健业'}
COLLECTION_BUDGET_SECONDS = 240
MAX_ARTICLE_CANDIDATES = 30
MAX_VERIFIED_ARTICLES = 10


def feed_matches_topic(item, feed):
    keywords = feed.get('title_keywords')
    if keywords is None:
        return True
    if (not isinstance(keywords, list) or not keywords or len(keywords) > 30 or
            any(not isinstance(x, str) or not x.strip() or len(x) > 40 for x in keywords)):
        raise ValueError('title_keywords需要1至30个有效词')
    for word in keywords:
        if re.search(r'[A-Za-z]', word):
            if re.search(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', item['title'], flags=re.I):
                return True
        elif word in item['title']:
            return True
    return False


def article_matches_ai(title, text):
    # 学术AI频道也可能混入其他研究；正文可证实AI关联才计入门槛。
    keywords = [word for word in AI_TITLE_KEYWORDS if word != 'agent'] + ['artificial intelligence', 'machine learning', 'deep learning', 'large language model']
    return feed_matches_topic({'title': title + '\n' + text}, {'title_keywords': keywords})


def source_failure(error):
    message = str(error)
    if re.fullmatch(r'source_http_\d{3}|source_timeout|source_too_large|source_xml_invalid|source_xml_entities_not_allowed|article_body_unavailable|article_ai_topic_unverified|collection_budget_exhausted', message):
        return message
    return type(error).__name__


def number(value):
    if isinstance(value, bool):
        raise ValueError('invalid_market_number')
    result = round(float(value), 6)
    if not math.isfinite(result):
        raise ValueError('invalid_market_number')
    return int(result) if result.is_integer() else result


def missing_market(key, cutoff, reason):
    return {'status': 'unavailable', 'trade_date': None, 'as_of': cutoff,
            'source': '东方财富公开行情' if key == 'a' else '东方财富公开指数／恒生综合行业指数',
            'source_url': 'https://quote.eastmoney.com/center/hszs.html' if key == 'a' else 'https://www.hsi.com.hk/en-hk/indexes/all-indexes/index-details/?seriesCode=hsci',
            'classification': '东方财富行业板块' if key == 'a' else '恒生综合行业指数',
            'indices': [], 'gainers': [], 'losers': [], 'missing': [reason]}


def index_quote(secid, name, report_date, reader):
    query = urlencode({'secid': secid, 'klt': 101, 'fqt': 0, 'lmt': 2,
                       'beg': (Date.fromisoformat(report_date) - timedelta(days=30)).strftime('%Y%m%d'),
                       'end': report_date.replace('-', ''),
                       'fields1': 'f1,f2,f3,f4,f5,f6',
                       'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
    payload = json.loads(read_with_retry(EASTMONEY_KLINE + '?' + query, reader))
    data = payload.get('data')
    if not isinstance(data, dict) or not data.get('klines'):
        raise ValueError('index_data_missing')
    rows = [row.split(',') for row in data['klines'] if isinstance(row, str)]
    rows = [row for row in rows if len(row) >= 9 and row[0] <= report_date]
    if not rows:
        raise ValueError('index_date_missing')
    current = max(rows, key=lambda x: x[0])
    traded = Date.fromisoformat(current[0]).isoformat()
    if traded > report_date or (Date.fromisoformat(report_date) - Date.fromisoformat(traded)).days > 10:
        raise ValueError('index_data_stale')
    result = {'code': secid, 'name': name, 'close': number(current[2]), 'change_pct': number(current[8]),
              'turnover': number(current[6])}
    if result['close'] <= 0 or result['turnover'] < 0 or abs(result['change_pct']) > 100:
        raise ValueError('invalid_index_data')
    return traded, result


def sina_index_quotes(report_date, cutoff, reader=read_bytes):
    """新浪官方指数页面使用的完整报价含日期与时间；不使用缺日期的s_简版。"""
    symbols = [('sh' if secid.startswith('1.') else 'sz') + secid.split('.')[1] for secid, _ in INDEX_CODES['a']]
    raw = read_with_retry('https://hq.sinajs.cn/list=' + ','.join(symbols), reader).decode('gb18030')
    records = dict(re.findall(r'var\s+hq_str_([a-z0-9]+)="([^"]*)";', raw))
    result = []
    for (secid, name), symbol in zip(INDEX_CODES['a'], symbols):
        fields = records.get(symbol, '').split(',')
        if len(fields) < 32:
            raise ValueError('sina_index_snapshot_incomplete')
        at = datetime.fromisoformat(fields[30] + 'T' + fields[31] + '+08:00')
        if at.date().isoformat() > report_date or at > parse_time(cutoff):
            raise ValueError('sina_index_future')
        if (Date.fromisoformat(report_date) - at.date()).days > 10:
            raise ValueError('sina_index_stale')
        close, previous = number(fields[3]), number(fields[2])
        if close <= 0 or previous <= 0:
            raise ValueError('sina_index_number_invalid')
        result.append((at.date().isoformat(), {'code': secid, 'name': name, 'close': close,
            'change_pct': number((close / previous - 1) * 100), 'turnover': number(fields[9])}, at))
    return result


def sector_ranking(report_date, reader):
    rows, total = [], None
    # 公开服务实测将每页上限限制为100；必须完整翻页，不能把首100名当全市场。
    for page in range(1, 11):
        query = urlencode({'pn': page, 'pz': 100, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
                           'fid': 'f3', 'fs': 'm:90+t:2', 'fields': 'f12,f14,f3,f124'})
        payload = json.loads(read_with_retry(EASTMONEY_SECTORS + '?' + query, reader))
        data = payload.get('data') or {}
        current = data.get('diff')
        if not isinstance(current, list) or not current or not isinstance(data.get('total'), int):
            raise ValueError('industry_snapshot_incomplete')
        if total is None:
            total = data['total']
        if data['total'] != total or not 1 <= total <= 1000:
            raise ValueError('industry_snapshot_total_changed')
        rows.extend(current)
        if len(rows) >= total:
            break
    if len(rows) != total:
        raise ValueError('industry_snapshot_incomplete')
    sectors = []
    for row in rows:
        local_date = datetime.fromtimestamp(float(row['f124']), timezone(timedelta(hours=8))).date().isoformat()
        if local_date != report_date:
            raise ValueError('industry_snapshot_stale')
        sectors.append({'code': str(row['f12']), 'name': str(row['f14']), 'change_pct': number(row['f3'])})
    if len({row['code'] for row in sectors}) != len(sectors):
        raise ValueError('industry_snapshot_duplicate')
    return ([x for x in sorted(sectors, key=lambda x: (-x['change_pct'], x['code'])) if x['change_pct'] > 0][:5],
            [x for x in sorted(sectors, key=lambda x: (x['change_pct'], x['code'])) if x['change_pct'] < 0][:5])


def hsi_values(series, reader=read_bytes):
    """HSI官网公开脚本与hsci元数据核实的API；industry为完整12行业集合。"""
    query = urlencode({'data': 'values', 'seriesCode': series, 'language': 'en-hk'})
    payload = json.loads(read_with_retry(HSI_METADATA + '?' + query, reader))
    if payload.get('code') != '0' or not isinstance(payload.get('data'), list):
        raise ValueError('hsi_values_unavailable')
    rows = [x for x in payload['data'] if x.get('indexType') == 'PI']
    if not rows:
        raise ValueError('hsi_price_indices_missing')
    return rows


def hsi_time(row):
    # 官网indexTime明示香港市场本地时间，响应本身不含时区。
    return datetime.strptime(row['indexTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=8)))


def hsi_industry_ranking(report_date, cutoff, reader=read_bytes, *, close_time='16:00'):
    rows = hsi_values('industry', reader)
    if len(rows) != 12 or {x.get('indexCode') for x in rows} != set(HSI_INDUSTRIES):
        raise ValueError('hsi_industry_snapshot_incomplete')
    sectors = []
    dates = []
    for row in rows:
        at = hsi_time(row)
        if at.date().isoformat() != report_date or at > parse_time(cutoff) or at.strftime('%H:%M') < close_time:
            raise ValueError('hsi_industry_snapshot_not_closed')
        if not row.get('indexName', '').startswith('Hang Seng Composite Industry Index - '):
            raise ValueError('hsi_industry_classification_mismatch')
        pct = number(row['changePercentage'])
        if abs(pct) > 100 or number(row['indexValue']) <= 0:
            raise ValueError('hsi_industry_number_invalid')
        sectors.append({'code': row['indexCode'], 'name': HSI_INDUSTRIES[row['indexCode']], 'change_pct': pct})
        dates.append(at)
    return ([x for x in sorted(sectors, key=lambda x: (-x['change_pct'], x['code'])) if x['change_pct'] > 0][:5],
            [x for x in sorted(sectors, key=lambda x: (x['change_pct'], x['code'])) if x['change_pct'] < 0][:5],
            min(dates).isoformat())


def collect_hk_market(report_date, cutoff, config, reader):
    market = missing_market('hk', cutoff, '恒生指数官方收市快照不可用')
    market['source'] = '恒生指数公司公开指数快照（可能延迟）'
    expected_closed = Date.fromisoformat(report_date).weekday() >= 5 or report_date in config.get('market_holidays', {}).get('hk', [])
    # 接受当日16点及以后的官方快照，保留原始时间，不要求时间戳达到16:10。
    close_time = config.get('early_closes', {}).get('hk', {}).get(report_date, '16:00')
    dates, quotes, errors, as_ofs = set(), [], [], []
    for series, name, official_name in [('hsi', '恒生指数', 'Hang Seng Index'),
                                        ('hscei', '恒生国企指数', 'Hang Seng China Enterprises Index'),
                                        ('hstech', '恒生科技指数', 'Hang Seng TECH Index')]:
        try:
            rows = hsi_values(series, reader)
            row = next(x for x in rows if x.get('indexName') == official_name)
            at = hsi_time(row)
            if at.date().isoformat() > report_date or at > parse_time(cutoff):
                raise ValueError('hsi_index_future')
            if not expected_closed and (at.date().isoformat() != report_date or at.strftime('%H:%M') < close_time):
                raise ValueError('hsi_index_not_closed')
            quote = {'code': row['indexCode'], 'name': name, 'close': number(row['indexValue']), 'change_pct': number(row['changePercentage'])}
            if quote['close'] <= 0 or abs(quote['change_pct']) > 100:
                raise ValueError('hsi_index_number_invalid')
            dates.add(at.date().isoformat()); as_ofs.append(at); quotes.append(quote)
        except (ValueError, TypeError, KeyError, StopIteration, OSError):
            errors.append(name + '官方已收市快照不可用')
    if len(dates) == 1:
        market.update(trade_date=dates.pop(), indices=quotes, as_of=min(as_ofs).isoformat(),
                      status='closed' if expected_closed else 'open', missing=errors)
    else:
        market['missing'] = ['恒生指数官方快照没有一致的交易日期'] + errors
        if expected_closed:
            market['status'] = 'closed'
    if market['status'] == 'open':
        try:
            gains, losses, at = hsi_industry_ranking(report_date, cutoff, reader, close_time=close_time)
            market.update(gainers=gains, losers=losses, as_of=min(parse_time(market['as_of']), parse_time(at)).isoformat())
        except (ValueError, TypeError, KeyError, OSError):
            market['missing'].append('恒生综合12行业完整收市快照不可用，未生成行业榜')
    market['missing'].append('全市场成交与涨跌家数未核实，不以所选指数或成分股代替')
    return market


def collect_market(key, report_date, cutoff, config, reader=read_bytes):
    if key == 'hk':
        return collect_hk_market(report_date, cutoff, config, reader)
    market = missing_market(key, cutoff, '主要指数数据不可用')
    expected_closed = Date.fromisoformat(report_date).weekday() >= 5 or report_date in config.get('market_holidays', {}).get(key, [])
    if not expected_closed and parse_time(cutoff).astimezone(timezone(timedelta(hours=8))).strftime('%H:%M') < '15:00':
        market['missing'] = ['A股尚未收市，盘中指数和行业报价不冒充收盘数据']
        return market
    dates, errors = set(), []
    for secid, name in INDEX_CODES[key]:
        try:
            traded, quote = index_quote(secid, name, report_date, reader)
            market['indices'].append(quote)
            dates.add(traded)
        except (ValueError, TypeError, KeyError, OSError):
            errors.append(name + '不可用')
    if errors:
        try:
            fallback = sina_index_quotes(report_date, cutoff, reader)
            if any(at.strftime('%H:%M') < '15:00' and day == report_date for day, quote, at in fallback):
                raise ValueError('sina_index_not_closed')
            if len({day for day, quote, at in fallback}) != 1:
                raise ValueError('sina_index_dates_inconsistent')
            dates = {day for day, quote, at in fallback}
            market['indices'] = [quote for day, quote, at in fallback]
            market['as_of'] = min(at for day, quote, at in fallback).isoformat()
            market['source'] = '新浪财经公开指数；东方财富行业板块'
            market['source_url'] = 'https://finance.sina.com.cn/realstock/company/sh000001/nc.shtml'
            errors = ['东方财富指数日K不可用，指数使用新浪同日完整报价核对']
        except (ValueError, TypeError, KeyError, OSError):
            pass
    if len(dates) != 1:
        market['indices'] = []  # 不将不同日期的行情拼成同一收盘快照。
        market['missing'] = ['指数快照缺少一致的交易日期'] + errors
        if expected_closed:
            market['status'] = 'closed'
        return market
    traded = dates.pop()
    market['trade_date'] = traded
    market['status'] = 'closed' if expected_closed else ('open' if traded == report_date else 'unavailable')
    market['missing'] = errors
    if market['status'] == 'unavailable':
        market['missing'].append('未核实当天休市安排，指数仅为最近交易日数据')
    if market['status'] == 'open' and key == 'a':
        try:
            market['gainers'], market['losers'] = sector_ranking(report_date, reader)
        except (ValueError, TypeError, KeyError, OSError):
            market['missing'].append('东方财富当日完整行业快照不可用')
    market['missing'].append('全市场成交与涨跌家数未核实，不以所选指数或成分股代替')
    return market


def collect(report_date, now_iso, config=None, *, reader=read_bytes):
    deadline = time.monotonic() + COLLECTION_BUDGET_SECONDS
    raw_reader = reader

    def bounded_reader(url):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('collection_budget_exhausted')
        if raw_reader is read_bytes:
            return raw_reader(url, timeout=min(8, remaining))
        return raw_reader(url)

    reader = bounded_reader
    """同步有界采集；reader可注入以进行无外部网络的契约回归。"""
    Date.fromisoformat(report_date)
    cutoff = parse_time(now_iso)
    if cutoff.astimezone(timezone(timedelta(hours=8))).date().isoformat() != report_date:
        raise ValueError('日报日期与北京时间不一致')
    config = config or {}
    if not isinstance(config, dict):
        raise ValueError('sources必须为配置对象')
    exclusions = config.get('exclude_urls', [])
    if not isinstance(exclusions, list) or any(not isinstance(url, str) for url in exclusions):
        raise ValueError('exclude_urls必须为网址列表')
    exclusions = {canonical_url(url) for url in exclusions}
    feeds = config.get('feeds', FEEDS)
    if not isinstance(feeds, dict) or set(feeds) != {'ai', 'society'}:
        raise ValueError('feeds需要ai及society两个栏目')
    warnings, news, sources = [], {'ai': [], 'society': []}, []
    candidates = {'ai': [], 'society': []}
    for category in ('ai', 'society'):
        if not isinstance(feeds[category], list) or len(feeds[category]) > 8:
            raise ValueError('每栏最多8个RSS来源')
        for feed in feeds[category]:
            try:
                items = parse_feed(read_with_retry(feed['url'], reader))
                candidates[category].extend(x for x in items if cutoff - timedelta(hours=24) <= timestamp(x['published_at']) <= cutoff and feed_matches_topic(x, feed))
            except (ValueError, KeyError, TypeError, OSError) as error:
                warnings.append(category + ' RSS不可用（' + source_failure(error) + '）：' + str(feed.get('name', '未命名来源'))[:80])
    used_urls, used_titles = set(), set()
    selected = {'ai': [], 'society': []}
    for category in ('ai', 'society'):
        for item in sorted(candidates[category], key=lambda x: x['published_at'], reverse=True):
            title_key = re.sub(r'\W+', '', item['title']).casefold()
            if item['url'] in exclusions or item['url'] in used_urls or title_key in used_titles:
                continue
            used_urls.add(item['url']); used_titles.add(title_key)
            selected[category].append(item)
            if len(selected[category]) >= MAX_ARTICLE_CANDIDATES:
                break

    def article_body(category, item):
        raw = read_with_retry(item['url'], reader)
        body = article_text(raw)
        if category == 'ai' and not article_matches_ai(item['title'], body):
            raise ValueError('article_ai_topic_unverified')
        image = article_image(raw, item['url'], item['title'])
        return body[:1600], datetime.now(timezone.utc).isoformat(), image, article_image_evidence(raw, item['url'], image)

    with ThreadPoolExecutor(max_workers=4) as pool:
        pending, next_category = {}, 0
        categories = ('ai', 'society')
        remaining = {key: iter(items) for key, items in selected.items()}
        in_flight = dict.fromkeys(categories, 0)

        def fill_slots():
            nonlocal next_category
            # 两栏轮流补位；并发、每URL重试及总预算保持原限制。
            while len(pending) < 4 and time.monotonic() < deadline:
                for offset in range(len(categories)):
                    index = (next_category + offset) % len(categories)
                    category = categories[index]
                    if len(news[category]) + in_flight[category] >= MAX_VERIFIED_ARTICLES:
                        continue
                    item = next(remaining[category], None)
                    if item is None:
                        continue
                    pending[pool.submit(article_body, category, item)] = (category, item)
                    in_flight[category] += 1
                    next_category = (index + 1) % len(categories)
                    break
                else:
                    break

        fill_slots()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                category, item = pending.pop(future)
                in_flight[category] -= 1
                try:
                    body, captured, image, evidence = future.result()
                    sid = 'source-' + hashlib.sha256(item['url'].encode()).hexdigest()[:16]
                    source = {'id': sid, **item, 'captured_at': captured}
                    if image:
                        source['image'] = image
                    sources.append(source)
                    news[category].append({'source_id': sid, 'title': item['title'], 'text': body,
                        **({'image_provenance': evidence} if evidence else {})})
                except (ValueError, TypeError, OSError) as error:
                    warnings.append(category + '正文读取失败（' + source_failure(error) + '）：' + item['title'][:80])
            fill_slots()
    markets = {key: collect_market(key, report_date, now_iso, config, reader) for key in ('a', 'hk')}
    ended = max(cutoff, datetime.now(timezone.utc))
    final_cutoff = ended.isoformat()
    # 读取期间跨过24小时边界的条目不能继续满足新闻门槛。
    sources = [source for source in sources if ended - timedelta(hours=24) <= parse_time(source['published_at']) <= ended]
    retained = {source['id'] for source in sources}
    for category in ('ai', 'society'):
        news[category] = [item for item in news[category] if item['source_id'] in retained]
    # 采集时间必须处于本次采集快照截止之前。
    for source in sources:
        source['captured_at'] = min(parse_time(source['captured_at']), ended).isoformat()
    for category in ('ai', 'society'):
        news[category].sort(key=lambda x: x['source_id'])
        if len(news[category]) < 3:
            warnings.append(category + '可核实新闻不足3条，当期不得发布')
    return {'date': report_date, 'cutoff_at': final_cutoff, 'sources': sorted(sources, key=lambda x: x['id']),
            'markets': markets, 'news': news, 'warnings': warnings}
