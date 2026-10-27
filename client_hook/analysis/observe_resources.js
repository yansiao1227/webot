// 固定版本的只读参数观察，不调用原生函数。
const core = Process.getModuleByName('Weixin.dll');
const base = core.base;
const account = 'wxid_x9enzopa4clm12';
const peer = 'wxid_ptf2vy5yblno12';
const ids = new Set(['2802806602853769603', '2598994970885895011', '3006432889162004287',
                     '6586583174111131346', '5478127133164431389']);
function narrow(at, limit = 128) {
  const n = at.add(16).readU64().toNumber(), capacity = at.add(24).readU64().toNumber();
  if (n < 0 || n > limit || n > capacity) throw new Error('bounded string');
  return (capacity < 16 ? at : at.readPointer()).readUtf8String(n);
}
function record(at) {
  if (!at.readPointer().equals(base.add(0x8096da8))) return null;
  const id = at.add(0x108).readU64().toString();
  if (!ids.has(id)) return null;
  const a = narrow(at.add(0x18)), b = narrow(at.add(0x38));
  if (a !== peer || b !== account) return null;
  let control = null;
  try {
    if (at.sub(16).readPointer().equals(base.add(0x8096d18)))
      control = {strong: at.sub(8).readS32(), weak: at.sub(4).readS32()};
  } catch (_) {}
  return {message_id: id, kind: at.add(12).readU32(), record: at.toString(), control,
          raw_104: at.add(0x104).readU32(), raw_110: at.add(0x110).readU64().toString(),
          raw_124: at.add(0x124).readU32()};
}
Interceptor.attach(base.add(0x9da310), {
  onEnter(args) { this.at = args[0]; },
  onLeave() { try { const r = record(this.at); if (r) send({type:'record', tid:Process.getCurrentThreadId(), ...r}); } catch (_) {} }
});
Interceptor.attach(base.add(0x16c84d0), {
  onEnter(args) {
    try {
      const r = record(args[2].readPointer());
      if (!r) return;
      send({type:'download', tid:Process.getCurrentThreadId(), ...r,
            manager:args[0].toString(), shared:args[2].toString(),
            shared_control:args[2].add(8).readPointer().toString(),
            resource_type:args[3].toInt32(), reason:args[4].toInt32(),
            options:args[5].toString(), options_type:args[5].readS32(),
            return_rva:this.returnAddress.sub(base).toString()});
    } catch (_) {}
  }
});
send({type:'ready', module:core.name, pid:Process.id, scope:'five_authorized_sample_ids_readonly_no_native_calls'});
