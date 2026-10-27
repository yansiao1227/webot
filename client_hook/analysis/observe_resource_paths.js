// 追加到共享的只读观察辅助代码。
Interceptor.attach(base.add(0x9bea70), {
  onEnter(args) {
    try {
      this.info = record(args[1]);
      if (!this.info) return;
      this.out = args[0];
      this.resourceType = args[2].readS32();
    } catch (_) { this.info = null; }
  },
  onLeave() {
    if (!this.info) return;
    try {
      const path = narrow(this.out, 1024);
      send({type:'resource_path', ...this.info, resource_type:this.resourceType,
            path, tid:Process.getCurrentThreadId()});
    } catch (_) {}
  }
});
send({type:'ready', pid:Process.id, scope:'five_authorized_sample_ids_readonly_native_path_observation'});
