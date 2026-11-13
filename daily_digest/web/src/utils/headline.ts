// 首页突出核心事件，详情页继续直接展示完整原标题。
export function frontTitle(title: string) {
  const value = title.trim();
  // 保留否认、传闻和核实状态，避免精简后改变事实含义。
  if (/辟谣|不实|未经证实|尚未|并非|否认|网传|传闻|疑似|据称/.test(value)) return value;
  const core = value.split(/[：，；]|:\s+/)[0].trim();
  const compact = core.length >= 8 ? core : value;
  const characters = Array.from(compact);
  return characters.length > 24
    ? characters
        .slice(0, 23)
        .join('')
        .replace(/[、\s]+$/, '') + '…'
    : compact;
}
