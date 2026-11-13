from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch
from collections import Counter
from daily_digest.collectors import collect_market, sector_ranking, index_quote, collect, hsi_industry_ranking, HSI_INDUSTRIES, sina_index_quotes, INDEX_CODES, feed_matches_topic, AI_TITLE_KEYWORDS, FEEDS, article_matches_ai, source_failure
from tool.digest_sources import parse_feed, read_with_retry, canonical_url, plain_text, article_text, article_image, article_image_evidence


class CollectorTests(unittest.TestCase):
    def test_unreadable_latest_ten_do_not_hide_older_verified_candidates(self):
        items = ''.join('<item><title>AI research ' + str(i) + '</title><link>https://' +
            ('blocked' if i < 10 else 'healthy') + '.example.org/' + str(i) +
            '</link><pubDate>Wed, 02 Jan 2030 ' + ('07' if i < 10 else '06') +
            ':00:00 GMT</pubDate></item>' for i in range(13))
        feed = ('<rss><channel>' + items + '</channel></rss>').encode()
        calls = []
        def reader(url):
            calls.append(url)
            if url.endswith('/feed'):
                return feed
            if 'blocked.example' in url:
                raise ValueError('source_http_403')
            return ('<article>' + '已核实的人工智能研究事实和背景。' * 30 + '</article>').encode()
        with patch('daily_digest.collectors.collect_market', return_value={}):
            packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00',
                {'feeds': {'ai': [{'url': 'https://feed.example.org/feed'}], 'society': []}}, reader=reader)
        self.assertEqual(len(packet['news']['ai']), 3)
        self.assertEqual(len({s['url'] for s in packet['sources']}), 3)
        self.assertEqual(len([url for url in calls if 'blocked.example' in url]), 20)
        self.assertTrue(all(count == 1 for url, count in Counter(calls).items() if 'healthy.example' in url))
        self.assertFalse(any('ai可核实新闻不足3条' in warning for warning in packet['warnings']))

    def test_success_target_stops_without_fetching_all_bounded_candidates(self):
        calls = []
        def reader(url):
            calls.append(url)
            if url.endswith('/feed'):
                category = url.split('/')[-2]
                items = ''.join('<item><title>' + category + ' AI story ' + str(i) + '</title><link>https://news.example.org/' +
                    category + '/' + str(i) + '</link><pubDate>Wed, 02 Jan 2030 07:00:00 GMT</pubDate></item>' for i in range(40))
                return ('<rss><channel>' + items + '</channel></rss>').encode()
            return ('<article>' + '已核实的人工智能研究事实和背景。' * 30 + '</article>').encode()
        with patch('daily_digest.collectors.collect_market', return_value={}):
            packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00',
                {'feeds': {category: [{'url': 'https://feeds.example.org/' + category + '/feed'}]
                           for category in ('ai', 'society')}}, reader=reader)
        self.assertEqual({category: len(items) for category, items in packet['news'].items()}, {'ai': 10, 'society': 10})
        self.assertEqual(len(calls), 22)  # 两个RSS加每栏恰好10次成功正文读取。

    def test_candidate_exhaustion_remains_bounded_and_does_not_lower_news_gate(self):
        calls = []
        def reader(url):
            calls.append(url)
            if url.endswith('/feed'):
                items = ''.join('<item><title>AI candidate ' + str(i) + '</title><link>https://news.example.org/' +
                    str(i) + '</link><pubDate>Wed, 02 Jan 2030 07:00:00 GMT</pubDate></item>' for i in range(40))
                return ('<rss><channel>' + items + '</channel></rss>').encode()
            raise ValueError('source_http_403')
        with patch('daily_digest.collectors.collect_market', return_value={}):
            packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00',
                {'feeds': {'ai': [{'url': 'https://feeds.example.org/feed'}], 'society': []}}, reader=reader)
        self.assertEqual(len({url for url in calls if not url.endswith('/feed')}), 30)
        self.assertEqual(len(calls), 61)
        self.assertFalse(packet['news']['ai'])
        self.assertTrue(any('ai可核实新闻不足3条' in warning for warning in packet['warnings']))

    def test_article_image_uses_original_metadata_or_body_not_site_logo(self):
        page = 'https://news.example.org/news/story.html'
        raw = '<meta property="og:image" content="/photos/story.jpg"><article><img src="body.jpg"></article>'
        image = article_image(raw, page, '新闻标题')
        self.assertEqual(image['url'], 'https://news.example.org/photos/story.jpg')
        self.assertEqual(image['caption'], '新闻标题')
        raw = '<meta property="og:image" content="/logo.png"><aside><img src="related.jpg"></aside><article><img width="20" src="tiny.jpg"><img src="actual.jpg" alt="现场"></article>'
        self.assertEqual(article_image(raw, page)['url'], 'https://news.example.org/news/actual.jpg')
        self.assertIsNone(article_image('<meta property="og:image" content="http://127.0.0.1/image.jpg">', page))
        self.assertIsNone(article_image('<nav><img src="menu.jpg"></nav>', page))
    def test_original_image_evidence_preserves_relative_url_and_html(self):
        page = 'https://news.example.org/story'
        raw = b'<div class="left_zw"><img src="//images.example.org/story.jpg" alt="scene"></div><aside><img src="other.jpg"></aside>'
        image = article_image(raw, page)
        evidence = article_image_evidence(raw, page, image)
        self.assertEqual(evidence['image_url'], 'https://images.example.org/story.jpg')
        self.assertEqual(evidence['page_url'], page)
        self.assertIn('alt="scene"', evidence['html_tag'])
        self.assertEqual(len(evidence['page_sha256']), 64)
        self.assertIsNone(article_image_evidence(raw, page, {'url': 'https://news.example.org/other.jpg'}))

    def test_source_expiring_during_collection_cannot_meet_final_news_gate(self):
        feed = b'<rss><channel><item><title>Boundary</title><link>https://news.example.org/boundary</link><pubDate>Tue, 01 Jan 2030 08:30:15 GMT</pubDate></item></channel></rss>'
        def reader(url):
            return feed if url.endswith('/feed') else ('<article>' + 'verified news body ' * 30 + '</article>').encode()
        with patch('daily_digest.collectors.datetime', wraps=datetime) as clock, patch('daily_digest.collectors.collect_market', return_value={}):
            clock.now.return_value = datetime.fromisoformat('2030-01-02T08:30:30+00:00')
            packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00',
                             {'feeds': {'ai': [{'url': 'https://news.example.org/feed'}], 'society': []}}, reader=reader)
        self.assertEqual(packet['sources'], [])
        self.assertEqual(packet['news']['ai'], [])
        self.assertTrue(any('不足3条' in warning for warning in packet['warnings']))

    def test_excludes_previous_url_but_keeps_new_progress_and_unused_candidate(self):
        entries = ''.join('<item><title>' + title + '</title><link>' + url +
            '</link><pubDate>Wed, 02 Jan 2030 02:00:00 GMT</pubDate></item>' for title, url in [
                ('昨日已刊新闻', 'https://news.example.org/old?utm_source=rss'),
                ('昨日未采用候选', 'https://news.example.org/unused'),
                ('同一事件的新进展', 'https://news.example.org/new-progress')])
        feed = ('<rss><channel>' + entries + '</channel></rss>').encode()
        requested = []
        def reader(url):
            requested.append(url)
            if url == 'https://news.example.org/feed':
                return feed
            return ('<article><p>' + '已读取的人工智能新闻事实内容' * 40 + '</p></article>').encode()
        with patch('daily_digest.collectors.collect_market', return_value={}):
            packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00', {
                'feeds': {'ai': [{'url': 'https://news.example.org/feed'}], 'society': []},
                'exclude_urls': ['https://news.example.org/old']}, reader=reader)
        self.assertEqual({s['url'] for s in packet['sources']},
                         {'https://news.example.org/unused', 'https://news.example.org/new-progress'})
        self.assertNotIn('https://news.example.org/old', requested)
    def test_feed_window_dates_and_dedup_urls(self):
        raw = b'<rss><channel><item><title>One</title><link>https://news.example.com/a?utm_source=x</link><pubDate>Wed, 02 Jan 2030 02:00:00 GMT</pubDate></item><item><title>Missing date</title><link>https://news.example.com/b</link></item></channel></rss>'
        items = parse_feed(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['url'], 'https://news.example.com/a')
        self.assertEqual(items[0]['published_at'], '2030-01-02T02:00:00Z')
        with self.assertRaises(ValueError): parse_feed(b'<!DOCTYPE x><rss/>')
        with self.assertRaises(ValueError): parse_feed(b'<html>')

    def test_http_retries_only_once(self):
        calls = []
        def missing(url):
            calls.append(url); raise TimeoutError()
        with self.assertRaises(TimeoutError): read_with_retry('https://example.com', missing)
        self.assertEqual(len(calls), 2)

    def test_parser_removes_script_text(self):
        self.assertEqual(plain_text(b'<p>hello</p><script>evil()</script>'), 'hello')

    def test_menu_heavy_news_selects_real_body_container(self):
        menu = '<nav>' + '北京 上海 香港 菜单 ' * 100 + '</nav>'
        body = '这是已经读取的新闻正文，包含可供核实的具体事实和背景。' * 8
        for container in ['<div class="left_zw">{}</div>', '<div class="field--name-body">{}</div>',
                          '<div class="blog-content prose">{}</div>', '<div data-component="uni-article-body">{}</div>']:
            html = '<header>网站主菜单</header>' + menu + '<main><article><div role="navigation">假新闻导航</div>' + container.format(body) + '<aside>相关推荐</aside></article></main>'
            self.assertEqual(article_text(html), body)

    def test_verified_wordpress_and_nvidia_body_templates(self):
        body = 'This paragraph explains an artificial intelligence application with verifiable details. ' * 6
        for container in ['<div class="entry-content wp-block-post-content">{}</div>',
                          '<div class="content-area full-width-layout light">{}</div>',
                          '<div class="article">{}</div>']:
            html = '<nav>site navigation menu</nav><main><article>Short related card only.</article>' + container.format('<p>' + body + '</p>') + '</main>'
            self.assertEqual(article_text(html), body.strip())

    def test_mixed_ai_feeds_require_explicit_title_keywords(self):
        feed = {'title_keywords': AI_TITLE_KEYWORDS}
        for title in ['New Gemini intelligence features', 'AI transforms recycling', '大模型走入生产线', 'Copilot and agentic workflows']:
            self.assertTrue(feed_matches_topic({'title': title}, feed))
        for title in ['Daily hardware roundup', '从芯片到千比特整机，超导量子计算还有硬仗', 'Azure backup performance update']:
            self.assertFalse(feed_matches_topic({'title': title}, feed))
        self.assertLessEqual(len(FEEDS['ai']), 8)

    def test_ai_research_must_have_actual_ai_topic_evidence(self):
        self.assertTrue(article_matches_ai('Next-generation weed killers', 'The team develops generative artificial intelligence models to discover molecules.'))
        self.assertFalse(article_matches_ai('Daily hardware roundup', 'A new screen and battery are available for laptops.'))
        self.assertEqual(source_failure(ValueError('source_http_403')), 'source_http_403')
        self.assertEqual(source_failure(ValueError('unexpected provider text must not appear')), 'ValueError')

    def test_menu_or_summary_alone_is_not_a_verified_article(self):
        html = '<main><header>网站介绍</header><div role="navigation">' + '菜单项目 ' * 100 + '</div></main>'
        with self.assertRaises(ValueError): article_text(html)
        with self.assertRaises(ValueError): article_text('<div>' + '只有整页文本' * 100 + '</div>')
        with self.assertRaises(ValueError): article_text('<article><p>摘要很短。</p></article>')

    def test_article_main_fallback_ignores_footer_and_related_cards(self):
        body = '真实文章中的事实段落，应当完整留在正文中。' * 8
        html = '<main><article>' + body + '</article><section class="related-news">' + '假推荐内容' * 100 + '</section></main><footer>底部导航</footer>'
        self.assertEqual(article_text(html), body)

    def test_index_dated_snapshot_and_holidays_are_separate(self):
        def reader(url):
            return json.dumps({'data': {'klines': ['2030-01-01,100,101,102,99,1000,1234,3,1,1,0']}}).encode()
        # 显式核实的A股休市不能被套用到香港；HK旧行情保守unknown。
        config = {'market_holidays': {'a': ['2030-01-02'], 'hk': []}}
        a = collect_market('a', '2030-01-02', '2030-01-02T16:30:00+08:00', config, reader)
        hk = collect_market('hk', '2030-01-02', '2030-01-02T16:30:00+08:00', config, reader)
        self.assertEqual(a['status'], 'closed')
        self.assertEqual(hk['status'], 'unavailable')
        self.assertFalse(a['gainers'])

    def test_sector_ranks_only_positive_and_negative_and_current(self):
        stamp = datetime.fromisoformat('2030-01-02T16:00:00+08:00').timestamp()
        rows = [{'f12': str(i), 'f14': str(i), 'f3': n, 'f124': stamp} for i, n in enumerate([2, 1, 0, -3, -1])]
        def reader(url): return json.dumps({'data': {'total': 5, 'diff': rows}}).encode()
        gains, losses = sector_ranking('2030-01-02', reader)
        self.assertEqual([x['change_pct'] for x in gains], [2, 1])
        self.assertEqual([x['change_pct'] for x in losses], [-3, -1])
        with self.assertRaises(ValueError): sector_ranking('2030-01-03', reader)

    def test_hsi_complete_classification_and_close_time_are_required(self):
        rows = [{'indexCode': code, 'indexName': 'Hang Seng Composite Industry Index - Demo',
                 'indexType': 'PI', 'indexValue': '1000', 'changePercentage': str(i - 6),
                 'indexTime': '2030-01-02 16:15:00'} for i, code in enumerate(HSI_INDUSTRIES)]
        def reader(url): return json.dumps({'code': '0', 'data': rows}).encode()
        gains, losses, at = hsi_industry_ranking('2030-01-02', '2030-01-02T16:30:00+08:00', reader)
        self.assertEqual(len(gains), 5)
        self.assertEqual(len(losses), 5)
        self.assertEqual(losses[0]['change_pct'], -6)
        rows.pop()
        with self.assertRaises(ValueError): hsi_industry_ranking('2030-01-02', '2030-01-02T16:30:00+08:00', reader)

    def test_industry_server_page_limit_requires_complete_pagination(self):
        from urllib.parse import parse_qs, urlsplit
        stamp = datetime.fromisoformat('2030-01-02T16:00:00+08:00').timestamp()
        calls = []
        def reader(url):
            page = int(parse_qs(urlsplit(url).query)['pn'][0]); calls.append(page)
            rows = [{'f12': str(i), 'f14': str(i), 'f3': 60 - i, 'f124': stamp}
                    for i in range((page - 1) * 100, min(page * 100, 150))]
            return json.dumps({'data': {'total': 150, 'diff': rows}}).encode()
        gains, losses = sector_ranking('2030-01-02', reader)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(losses[0]['change_pct'], -89)

    def test_hsi_intraday_cannot_masquerade_as_closing_industry_table(self):
        rows = [{'indexCode': code, 'indexName': 'Hang Seng Composite Industry Index - Demo',
                 'indexType': 'PI', 'indexValue': '1000', 'changePercentage': '1',
                 'indexTime': '2030-01-02 12:05:00'} for code in HSI_INDUSTRIES]
        def reader(url): return json.dumps({'code': '0', 'data': rows}).encode()
        with self.assertRaisesRegex(ValueError, 'not_closed'):
            hsi_industry_ranking('2030-01-02', '2030-01-02T16:30:00+08:00', reader)

    def test_hk_accepts_same_day_snapshots_from_four_pm(self):
        from urllib.parse import parse_qs, urlsplit
        stamp = '2030-01-02 16:09:10'
        def reader(url):
            series = parse_qs(urlsplit(url).query)['seriesCode'][0]
            names = {'hsi': 'Hang Seng Index', 'hscei': 'Hang Seng China Enterprises Index',
                     'hstech': 'Hang Seng TECH Index'}
            rows = ([{'indexCode': code, 'indexName': 'Hang Seng Composite Industry Index - Demo',
                      'changePercentage': str(i - 6)} for i, code in enumerate(HSI_INDUSTRIES)]
                    if series == 'industry' else
                    [{'indexCode': series, 'indexName': names[series], 'changePercentage': '-1'}])
            return json.dumps({'code': '0', 'data': [dict(row, indexType='PI', indexValue='1000',
                                                          indexTime=stamp) for row in rows]}).encode()
        for value in ('2030-01-02 16:00:00', '2030-01-02 16:09:10'):
            stamp = value
            market = collect_market('hk', '2030-01-02', '2030-01-02T17:00:00+08:00', {}, reader)
            self.assertEqual(market['status'], 'open')
            self.assertEqual(len(market['indices']), 3)
            self.assertEqual(len(market['gainers']), 5)
            self.assertEqual(len(market['losers']), 5)
        for value in ('2030-01-02 15:59:59', '2030-01-01 16:09:10', '2030-01-02 17:00:01'):
            stamp = value
            market = collect_market('hk', '2030-01-02', '2030-01-02T17:00:00+08:00', {}, reader)
            self.assertEqual(market['status'], 'unavailable')
            self.assertFalse(market['indices'])
            self.assertFalse(market['gainers'])

    def test_sina_full_quotes_require_dates_for_all_five_indices(self):
        payload = []
        for secid, name in INDEX_CODES['a']:
            symbol = ('sh' if secid.startswith('1.') else 'sz') + secid.split('.')[1]
            fields = [name, '100', '100', '101'] + ['0'] * 26 + ['2030-01-02', '15:00:01']
            payload.append('var hq_str_' + symbol + '="' + ','.join(fields) + '";')
        raw = ''.join(payload).encode('gb18030')
        quotes = sina_index_quotes('2030-01-02', '2030-01-02T16:30:00+08:00', lambda url: raw)
        self.assertEqual(len(quotes), 5)
        self.assertEqual(quotes[0][1]['change_pct'], 1)
        with self.assertRaises(ValueError):
            sina_index_quotes('2030-01-01', '2030-01-01T16:30:00+08:00', lambda url: raw)

    def test_failed_sources_produce_missing_packet_without_invented_news(self):
        def missing(url): raise TimeoutError()
        packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00', {'feeds': {'ai': [], 'society': []}}, reader=missing)
        self.assertFalse(packet['sources'])
        self.assertFalse(packet['news']['ai'])
        self.assertEqual(packet['markets']['a']['status'], 'unavailable')
        self.assertTrue(packet['warnings'])

    def test_collection_budget_stops_starting_additional_reads(self):
        reader = Mock()
        clock = iter([0] + [241] * 100)
        with patch('daily_digest.collectors.time.monotonic', side_effect=lambda: next(clock)):
            packet = collect('2030-01-02', '2030-01-02T16:30:00+08:00',
                             {'feeds': {'ai': [], 'society': []}}, reader=reader)
        reader.assert_not_called()
        self.assertEqual(packet['markets']['a']['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
