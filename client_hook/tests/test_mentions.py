import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.mentions import parse_mentions


class MentionTests(unittest.TestCase):
    def parse(self, source, complete=True):
        return parse_mentions({'source_180': source, 'source_180_complete': complete})

    def test_native_source_only_and_exact_ids(self):
        self.assertEqual(self.parse('<msgsource><atuserlist><![CDATA[wxid_self, wxid_other]]></atuserlist></msgsource>')['wxids'], ['wxid_self', 'wxid_other'])
        self.assertEqual(self.parse('<msgsource><atuserlist>notify@all</atuserlist></msgsource>'),
                         {'state': 'known', 'wxids': [], 'all': True, 'source': 'weixin41930_record_source_180'})
        self.assertEqual(self.parse('<msgsource/>')['wxids'], [])
        self.assertEqual(self.parse('')['state'], 'known')
        self.assertEqual(parse_mentions({'body': '<msgsource><atuserlist>wxid_self</atuserlist></msgsource>'})['state'], 'unknown')

    def test_incomplete_malformed_and_entity_sources_fail_closed(self):
        for raw in ('<msgsource>', '@wxid_self', '<root><atuserlist>wxid_self</atuserlist></root>',
                    '<msgsource><atuserlist>wxid_self</atuserlist><atuserlist>wxid_other</atuserlist></msgsource>',
                    '<msgsource><x><atuserlist>wxid_self</atuserlist></x></msgsource>',
                    '<msgsource><atuserlist><x>wxid_self</x></atuserlist></msgsource>',
                    '<msgsource><atuserlist>wxid_self,,wxid_other</atuserlist></msgsource>',
                    '<!DOCTYPE msgsource [<!ENTITY x "wxid_self">]><msgsource><atuserlist>&x;</atuserlist></msgsource>',
                    '<msgsource>\0</msgsource>', 'a' * 16385):
            with self.subTest(raw=raw[:80]):
                self.assertEqual(self.parse(raw)['state'], 'unknown')
        self.assertEqual(self.parse('<msgsource/>', complete=False)['state'], 'unknown')
