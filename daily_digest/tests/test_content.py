from copy import deepcopy
import json
import unittest
from daily_digest.content import validate_edition, canonical, build_prompt, REPORT_SCHEMA, REPORT_FIELDS, bind_report, report_schema, check_shape
from daily_digest.examples.build_preview import sample


class ContentTests(unittest.TestCase):
    def test_optional_source_image_is_bound_and_requires_public_url(self):
        value = deepcopy(self.value)
        value['simulation'] = False
        value.pop('content_hash', None)
        value['sources'][0]['image'] = {'url': 'https://images.example.org/photo.jpg',
                                        'caption': '相关配图', 'credit': '原文来源'}
        self.assertEqual(validate_edition(value)['sources'][0]['image']['caption'], '相关配图')
        value['sources'][0]['image']['url'] = 'http://127.0.0.1/private.jpg'
        with self.assertRaises(ValueError):
            validate_edition(value)
    def setUp(self):
        self.value = sample()
        self.value.pop('content_hash')

    def test_report_schema_grounds_each_column_without_constraining_prose_or_global_schema(self):
        packet = {key: deepcopy(self.value[key]) for key in ('sources',)}
        packet['news'] = {key: [{'source_id': a['source_ids'][0]} for a in items]
                          for key, items in self.value['articles'].items()}
        original = deepcopy(REPORT_SCHEMA)
        schema = report_schema(packet)
        draft = {key: deepcopy(self.value[key]) for key in REPORT_FIELDS}
        check_shape(draft, schema)
        self.assertEqual(REPORT_SCHEMA, original)
        bad = deepcopy(draft)
        bad['articles']['ai'][0]['source_ids'] = [draft['articles']['society'][0]['source_ids'][0]]
        with self.assertRaisesRegex(ValueError, '枚举'):
            check_shape(bad, schema)
        bad = deepcopy(draft)
        bad['analysis'][0]['source_ids'] = ['invented-source']
        with self.assertRaisesRegex(ValueError, '枚举'):
            check_shape(bad, schema)

    def test_preview_only_and_deterministic_hash(self):
        with self.assertRaisesRegex(ValueError, '模拟'):
            validate_edition(self.value)
        result = validate_edition(self.value, allow_simulation=True)
        self.assertEqual(validate_edition(result, allow_simulation=True), result)
        self.assertEqual(canonical({'x': 1.0, 'y': .000001}), '{"x":1,"y":0.000001}')

    def test_shape_and_invalid_text(self):
        for key, value in [('title', '\x00'), ('schema_version', True), ('unexpected', '')]:
            bad = deepcopy(self.value); bad[key] = value
            with self.assertRaises(ValueError):
                validate_edition(bad, allow_simulation=True)

    def test_repeated_news_and_unknown_sources(self):
        for mutation in ('same_source', 'same_title', 'missing'):
            bad = deepcopy(self.value)
            if mutation == 'same_source':
                bad['articles']['ai'][1]['source_ids'] = bad['articles']['ai'][0]['source_ids']
            elif mutation == 'same_title':
                bad['articles']['ai'][1]['title'] = bad['articles']['ai'][0]['title']
            else:
                bad['articles']['ai'][1]['source_ids'] = ['nonexistent']
            with self.assertRaises(ValueError):
                validate_edition(bad, allow_simulation=True)

    def test_old_news_and_future_captures_rejected(self):
        for key, value in [('published_at', '2029-12-31T10:00:00+08:00'), ('captured_at', '2030-01-03T10:00:00+08:00')]:
            bad = deepcopy(self.value); bad['sources'][0][key] = value
            with self.assertRaises(ValueError):
                validate_edition(bad, allow_simulation=True)

    def test_rank_direction_dates_and_invalid_numbers(self):
        for kind in ('positive_loser', 'old_open', 'nonfinite', 'closed_rank'):
            bad = deepcopy(self.value); m = bad['markets']['a']
            if kind == 'positive_loser': m['losers'][0]['change_pct'] = 1
            if kind == 'old_open': m['trade_date'] = '2029-12-31'
            if kind == 'nonfinite': m['indices'][0]['close'] = float('nan')
            if kind == 'closed_rank': m['status'] = 'closed'
            with self.assertRaises(ValueError):
                validate_edition(bad, allow_simulation=True)

    def test_collector_snapshot_cannot_be_changed(self):
        packet = {k: deepcopy(self.value[k]) for k in ('date', 'cutoff_at', 'markets', 'sources')}
        packet['news'] = {cat: [{'source_id': a['source_ids'][0]} for a in self.value['articles'][cat]] for cat in ('ai', 'society')}
        self.assertTrue(validate_edition(self.value, packet, True)['content_hash'])
        bad = deepcopy(self.value); bad['markets']['a']['indices'][0]['close'] += 1
        with self.assertRaisesRegex(ValueError, '快照'):
            validate_edition(bad, packet, True)
        bad = deepcopy(self.value); bad['sources'][0]['title'] = 'new fabricated source'
        with self.assertRaisesRegex(ValueError, '来源'):
            validate_edition(bad, packet, True)
        self.assertIn('不调用发布', build_prompt(packet))

    def test_minimum_sources_and_teaser(self):
        bad = deepcopy(self.value); bad['articles']['ai'].pop()
        with self.assertRaises(ValueError):
            validate_edition(bad, allow_simulation=True)

    def test_model_schema_has_no_optional_object_fields_or_mutable_quotes(self):
        def strict_schema(node):
            if node.get('type') == 'object':
                self.assertEqual(set(node['required']), set(node['properties']))
                self.assertIs(node['additionalProperties'], False)
                for child in node['properties'].values():
                    strict_schema(child)
            if node.get('type') == 'array':
                strict_schema(node['items'])
        strict_schema(REPORT_SCHEMA)
        self.assertNotIn('markets', REPORT_SCHEMA['properties'])
        draft = {key: deepcopy(self.value[key]) for key in REPORT_FIELDS}
        packet = {key: deepcopy(self.value[key]) for key in ('date', 'cutoff_at', 'markets', 'sources', 'warnings')}
        edition = bind_report(draft, packet)
        self.assertEqual(edition['markets'], packet['markets'])
        self.assertIsNot(edition['markets'], packet['markets'])
        draft['markets'] = {}
        with self.assertRaises(ValueError):
            bind_report(draft, packet)
        bad = deepcopy(self.value); bad['teaser'] = 'too short'
        with self.assertRaises(ValueError):
            validate_edition(bad, allow_simulation=True)


if __name__ == '__main__':
    unittest.main()
