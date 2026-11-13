export type Route = {
  kind: 'front' | 'article' | 'analysis' | 'markets' | 'notes' | 'archive' | 'missing';
  date?: string;
  sample?: boolean;
  id?: string;
};

export function parseRoute(path: string): Route {
  if (path === '/') return { kind: 'front' };
  if (path === '/archive') return { kind: 'archive' };
  const match = /^(\/preview|\/daily\/(\d{4}-\d{2}-\d{2}))(.*)$/.exec(path);
  if (!match) return { kind: 'missing' };
  const context = { date: match[2], sample: match[1] === '/preview' };
  if (!match[3] || match[3] === '/') return { ...context, kind: 'front' };
  if (match[3] === '/markets') return { ...context, kind: 'markets' };
  if (match[3] === '/notes') return { ...context, kind: 'notes' };
  const detail = /^\/(article|analysis)\/([^/]+)$/.exec(match[3]);
  if (!detail) return { ...context, kind: 'missing' };
  try {
    return {
      ...context,
      kind: detail[1] as 'article' | 'analysis',
      id: decodeURIComponent(detail[2]),
    };
  } catch {
    return { ...context, kind: 'missing' };
  }
}

export const editionBase = (date: string, sample = false) =>
  sample ? '/preview' : '/daily/' + date;
export const articleHref = (base: string, id: string) =>
  base + '/article/' + encodeURIComponent(id);

export function brief(text: string, limit = 64) {
  const value = text.replace(/\s+/g, ' ').trim();
  if (value.length <= limit) return value;
  const cut = value.slice(0, limit);
  const sentence = Math.max(cut.lastIndexOf('。'), cut.lastIndexOf('；'));
  return sentence >= limit * 0.6
    ? cut.slice(0, sentence + 1)
    : cut.replace(/[，、；：\s]+$/, '') + '…';
}
