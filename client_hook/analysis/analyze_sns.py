"""4.1.9.30 朋友圈离线定位；仅生成候选证据，不执行或注入微信。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import struct
import sys

MODULE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE), str(MODULE.parent)]
from hook_runtime.pe_local import PE
from tool.settings import executable_setting

DEFAULT_SEEDS = (
    '/cgi-bin/micromsg-bin/mmsnstimeline',
    '/cgi-bin/micromsg-bin/mmsnsuserpage',
    '/cgi-bin/micromsg-bin/mmsnscomment',
    '/cgi-bin/micromsg-bin/mmsnspost',
    'micromsg.SnsTimeLineRequest', 'micromsg.SnsTimeLineResponse',
    'micromsg.SnsCommentRequest', 'micromsg.SnsCommentResponse',
    'micromsg.SnsPostRequest', 'micromsg.SnsPostResponse',
    'kernel::SNSCommentContentInfo', 'mmui::SnsPublishViewModel',
    'mmui::TimelineBaseDataSource',
    'E:\\x\\x_rel_t2\\ui\\sns\\timeline\\viewmodel\\timeline_base_data_source.cc',
    'sns_like', 'sns_cmt_send',
)


def validate_artifact(pe, profile):
    digest = hashlib.sha256(pe.data).hexdigest()
    if pe.machine != 0x8664 or digest != profile['sha256']:
        raise ValueError('目标不是已固定的微信 4.1.9.30 x64 构建')
    return digest


def instruction_addresses(assembly):
    """只接受反汇编器输出的指令起点，不把字节搜索命中当成指令。"""
    return {int(m[1], 16) for line in assembly.splitlines()
            if (m := re.match(r'^\s*([0-9a-fA-F]+):\s+(?:[0-9a-fA-F]{2}\s+)+\s*\S', line))}


def inspect_table(pe, rva, slots=6):
    """读取显式指定的候选函数表；非代码值和无 unwind 的叶函数不猜测边界。"""
    if rva % 8:
        raise ValueError('候选表地址必须按 8 字节对齐')
    result = []
    for index in range(slots):
        offset = pe.rva_to_offset(rva + index * 8)
        # 核对整个槽都在文件映射中，不能跨节误读。
        if pe.rva_to_offset(rva + index * 8 + 7) != offset + 7:
            raise ValueError('候选表槽跨越不连续文件映射')
        pointer = struct.unpack_from('<Q', pe.data, offset)[0]
        target = pointer - pe.image_base
        executable = any(s['executable'] and s['rva'] <= target <
                         s['rva'] + s['raw_size'] for s in pe.sections)
        function = pe.function(target) if executable else None
        # 表中值必须等于 unwind 入口；落入函数中间的值不能成为导出入口。
        if function and function['begin_rva'] != target:
            function = None
        result.append({'slot': index, 'slot_rva': rva + index * 8,
                       'pointer_va': hex(pointer), 'target_rva': target if executable else None,
                       'executable': executable, 'function': function,
                       'evidence': 'static_pointer_only', 'runtime_callable': False})
    return {'rva': rva, 'slots': result}


def discover(pe, seeds, radius=64, entries=()):
    strings = []
    for seed in seeds:
        # 限定完整 NUL 结尾文本；内嵌立即数中的 sns_likeH 不算字符串。
        for hit in pe.find(seed.encode('utf-8') + b'\0'):
            if hit['rva'] is not None:
                strings.append({'text': seed, **hit})
    targets = [item['rva'] for item in strings]
    refs = pe.riprefs(targets, radius=radius)
    if entries:
        # 信号槽常以函数指针连接，须同时查 LEA 和直接 CALL/JMP。
        refs.extend(pe.riprefs(list(entries)))
        refs.extend(pe.relbranches(list(entries), b'\xe8\xe9'))
    for ref in refs:
        ref['evidence'] = 'unverified_instruction_bytes'
        ref['seed_matches'] = [
            {'text': item['text'], 'string_rva': item['rva'],
             'target_delta': ref['target_rva'] - item['rva'],
             'exact': ref['target_rva'] == item['rva']}
            for item in strings if item['rva'] in ref.get('matching_targets', [])]
        ref['entry_matches'] = [entry for entry in entries if entry == ref['target_rva']]
    return strings, refs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=MODULE / 'build/sns-analysis')
    parser.add_argument('--objdump', default=executable_setting('RE_WECHAT_OBJDUMP', 'objdump'))
    parser.add_argument('--seed', action='append', help='替换默认定位字符串，可重复指定')
    parser.add_argument('--entry', action='append', type=lambda value: int(value, 0), default=[],
                        help='追踪候选函数 RVA 的直接调用和函数指针引用，可重复指定')
    parser.add_argument('--table', action='append', type=lambda value: int(value, 0), default=[],
                        help='读取候选闭包表的前六个指针槽 RVA，可重复指定；不推断槽语义')
    parser.add_argument('--radius', type=int, default=64,
                        help='字符串周边数据的检索半径；周边命中不是字符串交叉引用')
    args = parser.parse_args()
    if not 0 <= args.radius <= 256:
        parser.error('radius 必须为 0–256')
    pe = PE(str(args.artifact))
    profile = json.loads((MODULE / 'profiles/weixin-4.1.9.30.json').read_text(encoding='utf-8'))
    digest = validate_artifact(pe, profile)
    if any(not 0 <= entry < pe.size_of_image for entry in args.entry):
        parser.error('entry 必须是映像内的 RVA')
    strings, refs = discover(pe, args.seed or DEFAULT_SEEDS, args.radius, args.entry)
    tables = [inspect_table(pe, rva) for rva in args.table]
    args.output.mkdir(parents=True, exist_ok=True)
    functions = {r['function']['begin_rva']: r['function'] for r in refs if r['function']}
    table_functions = {slot['function']['begin_rva']: slot['function']
                       for table in tables for slot in table['slots'] if slot['function']}
    functions.update(table_functions)
    for start, function in functions.items():
        # 只解码已知 unwind 范围，拒绝从任意匹配字节开始解码。
        assembly = subprocess.check_output([
            args.objdump, '-d', '-M', 'intel', '--insn-width=16',
            f'--start-address={pe.image_base + start:#x}',
            f'--stop-address={pe.image_base + function["end_rva"]:#x}', str(pe.path)
        ], text=True, errors='replace')
        (args.output / f'{start:x}.asm').write_text(assembly, encoding='utf-8')
        addresses = instruction_addresses(assembly)
        for ref in refs:
            if ref['function'] == function:
                aligned = pe.image_base + ref['instruction_rva'] in addresses
                ref['instruction_aligned'] = aligned
                ref['evidence'] = 'aligned_reference' if aligned else 'rejected_unaligned_bytes'
    verified = sorted({r['function']['begin_rva'] for r in refs
                       if r.get('instruction_aligned')} | set(table_functions))
    # ExportSelected.java 的地址为绝对 VA；清单不授予执行这些函数的权限。
    ranges = '\n'.join(f'{pe.image_base+s:x} {pe.image_base+functions[s]["end_rva"]:x}'
                       for s in verified)
    (args.output / 'ranges.txt').write_text(ranges + '\n', encoding='utf-8')
    report = {
        'schema': 'client-hook-sns-static-analysis-v1', 'version': profile['version'],
        'artifact_sha256': digest, 'scope': 'offline_only_no_target_execution',
        'image_base': hex(pe.image_base), 'radius': args.radius, 'entries': args.entry,
        'seeds': strings, 'references': refs, 'candidate_tables': tables,
        'runtime_callable': False,
        'limitations': [
            '只覆盖 REX + LEA/MOV 的部分 RIP 相对编码，不覆盖全部交叉引用',
            '周边数据引用不能证明引用 CGI 或 protobuf 类型；必须结合反编译确认',
            '指令对齐不证明业务语义、参数 ABI、对象生命周期或线程归属',
            '无 unwind 范围的匹配保持未验证；没有命中不表示不存在实现',
            '读取、点赞、评论、发布均未完成运行时适配，禁止直接调用候选地址',
        ],
    }
    (args.output / 'sns-analysis.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'strings': len(strings), 'references': len(refs),
                      'aligned_functions': len(verified), 'runtime_callable': False}))


if __name__ == '__main__':
    main()
