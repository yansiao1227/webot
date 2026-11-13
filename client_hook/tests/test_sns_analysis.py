"""离线定位工具的证据边界；不依赖本机微信或反编译器。"""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import struct

spec = importlib.util.spec_from_file_location(
    'sns_analysis', Path(__file__).resolve().parents[1] / 'analysis/analyze_sns.py')
sns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sns)


class SnsAnalysisTests(unittest.TestCase):
    def test_table_keeps_only_exact_executable_entries(self):
        class FakePE:
            image_base = 0x180000000
            data = struct.pack('<QQQ', image_base + 0x1000,
                               image_base + 0x1001, 0)
            sections = [{'rva': 0x1000, 'raw_size': 0x100, 'executable': True}]

            def rva_to_offset(self, rva):
                return rva - 0x2000

            def function(self, rva):
                return {'begin_rva': 0x1000, 'end_rva': 0x1010}

        table = sns.inspect_table(FakePE(), 0x2000, slots=3)
        self.assertIsNotNone(table['slots'][0]['function'])
        self.assertIsNone(table['slots'][1]['function'])
        self.assertFalse(table['slots'][2]['executable'])
        self.assertTrue(all(not slot['runtime_callable'] for slot in table['slots']))
        with self.assertRaises(ValueError):
            sns.inspect_table(FakePE(), 0x2001)

    def test_rejects_wrong_build_or_architecture(self):
        pe = SimpleNamespace(data=b'fixture', machine=0x8664)
        profile = {'sha256': hashlib.sha256(pe.data).hexdigest()}
        self.assertEqual(sns.validate_artifact(pe, profile), profile['sha256'])
        for machine, digest in ((0x14c, profile['sha256']), (0x8664, '0' * 64)):
            with self.subTest(machine=machine, digest=digest), self.assertRaises(ValueError):
                sns.validate_artifact(SimpleNamespace(data=pe.data, machine=machine), {'sha256': digest})

    def test_alignment_accepts_only_instruction_start(self):
        assembly = '''
0000000180001000 <.text>:
   180001000: 48 8d 05 01 00 00 00    lea rax,[rip+0x1]
   180001007: c3                      ret
unrelated 180001003
'''
        self.assertEqual(sns.instruction_addresses(assembly), {0x180001000, 0x180001007})

    def test_nearby_reference_does_not_become_exact_reference(self):
        class FakePE:
            def find(self, value):
                self.value = value
                return [{'file_offset': 64, 'rva': 0x1000}]

            def riprefs(self, targets, radius):
                return [{'target_rva': 0x102c, 'matching_targets': targets, 'function': None}]

        pe = FakePE()
        strings, refs = sns.discover(pe, ['sns_like'])
        self.assertEqual(pe.value, b'sns_like\0')
        self.assertEqual(strings[0]['text'], 'sns_like')
        self.assertFalse(refs[0]['seed_matches'][0]['exact'])
        self.assertEqual(refs[0]['seed_matches'][0]['target_delta'], 44)
        self.assertEqual(refs[0]['evidence'], 'unverified_instruction_bytes')

    def test_direct_branch_and_function_pointer_are_both_retained(self):
        class FakePE:
            def riprefs(self, targets, radius=0):
                return [{'target_rva': target, 'matching_targets': [target], 'function': None}
                        for target in targets]

            def relbranches(self, targets, kinds):
                return [{'target_rva': target, 'opcode': '0xe8', 'function': None}
                        for target in targets]

        _, refs = sns.discover(FakePE(), [], entries=[0x1000])
        self.assertEqual(len(refs), 2)
        self.assertEqual([ref['entry_matches'] for ref in refs], [[0x1000], [0x1000]])


if __name__ == '__main__':
    unittest.main()
