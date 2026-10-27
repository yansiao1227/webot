import unittest
from unittest.mock import Mock
from local_agent.bilibili import parse_captions, player_response, video_url
from tool.content_reading.bilibili import read_discussion


class BilibiliTests(unittest.TestCase):
    def test_discussion_stays_separate_from_captions_and_preserves_limits(self):
        page = Mock(url='https://www.bilibili.com/video/BVtest/')
        page.evaluate.side_effect = [None, {'comments': {'text': '观众认为更新不错', 'truncated': False},
                                          'danmaku': {'text': '字' * 13000}}]
        result = read_discussion(page)
        self.assertEqual(result['state'], 'read')
        self.assertEqual(result['comments']['text'], '观众认为更新不错')
        self.assertEqual(len(result['danmaku']['text']), 12000)
        self.assertTrue(result['danmaku']['truncated'])
        self.assertNotIn('video_content', result)
        self.assertIn('不是字幕', result['content_scope'])

    def test_missing_discussion_is_unavailable_without_invented_text(self):
        page = Mock(url='https://www.bilibili.com/video/BVtest/')
        page.evaluate.side_effect = [None, {}]
        result = read_discussion(page)
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['comments']['text'], '')
        self.assertEqual(result['danmaku']['state'], 'unavailable')

    def test_match_current_player_cid_and_aid(self):
        identity = {'cid': 123, 'aid': 456, 'bvid': 'BVtest'}
        self.assertTrue(player_response('https://api.bilibili.com/x/player/wbi/v2?cid=123&aid=456', identity))
        for url in ('https://api.bilibili.com/x/player/v2?cid=123&aid=456',
                    'https://api.bilibili.com/x/player/wbi/v2?cid=999&aid=456',
                    'https://api.bilibili.com/x/player/wbi/v2?cid=123&aid=789',
                    'https://other.example/x/player/wbi/v2?cid=123&aid=456'):
            self.assertFalse(player_response(url, identity))

    def test_reject_captions_from_longer_unrelated_video(self):
        with self.assertRaises(ValueError):
            parse_captions([{'from': 1000, 'to': 1062.8, 'content': '另一条视频'}], 289)

    def test_actual_subtitles_keep_times_and_scope(self):
        result = parse_captions([{'from': .04, 'to': 1.44, 'content': '台词一'},
                                 {'from': 280, 'to': 288.48, 'content': '结束'}], 289)
        self.assertEqual(result['time_range_seconds'], [.04, 288.48])
        self.assertEqual(result['segment_count'], 2)
        self.assertIn('[280.00-288.48秒] 结束', result['text'])
        self.assertFalse(result['truncated'])

    def test_invalid_captions_fail_closed(self):
        for row in ({'from': True, 'to': 1, 'content': 'bad'},
                    {'from': 0, 'to': float('nan'), 'content': 'bad'},
                    {'from': 5, 'to': 1, 'content': 'bad'}):
            with self.assertRaises(ValueError):
                parse_captions([row], 10)

    def test_caption_limit_is_explicit(self):
        result = parse_captions([{'from': i, 'to': i + 1, 'content': '字' * 2000} for i in range(20)], 30)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['text']), 24000)
        self.assertLess(result['segment_count'], 20)

    def test_only_canonical_bilibili_video_pages(self):
        self.assertTrue(video_url('https://www.bilibili.com/video/BV1fWHM65EiK/?p=1'))
        self.assertFalse(video_url('https://www.bilibili.com.evil.example/video/BVtest/'))
        self.assertFalse(video_url('https://www.bilibili.com/'))
