import type { Edition, Market } from './types';
const market = (hk = false): Market => ({
  status: 'open',
  trade_date: '2026-01-01',
  as_of: '2026-01-01T16:30:00+08:00',
  source: '排版演示数据',
  source_url: 'https://www.hsi.com.hk/',
  classification: hk ? '恒生综合行业指数 · 演示' : '东方财富行业板块 · 演示',
  indices: (hk
    ? [
        ['HSI', '恒生指数', 24210.36, 0.82],
        ['HSCEI', '国企指数', 8834.22, 0.61],
        ['HSTECH', '恒生科技', 5214.08, 1.26],
      ]
    : [
        ['000001', '上证指数', 3520.28, 0.46],
        ['399001', '深证成指', 11340.72, 0.89],
        ['399006', '创业板指', 2380.15, 1.12],
        ['000300', '沪深300', 4124.56, 0.53],
        ['000688', '科创50', 1082.44, 1.63],
      ]
  ).map(([code, name, close, change_pct]) => ({
    code: String(code),
    name: String(name),
    close: Number(close),
    change_pct: Number(change_pct),
  })),
  gainers: [
    { code: '01', name: hk ? '资讯科技' : '半导体', change_pct: 2.61 },
    { code: '02', name: hk ? '医疗保健' : '软件服务', change_pct: 1.84 },
    { code: '03', name: hk ? '工业' : '通信设备', change_pct: 1.32 },
  ],
  losers: [
    { code: '04', name: hk ? '地产建筑' : '煤炭行业', change_pct: -1.21 },
    { code: '05', name: hk ? '公用事业' : '贵金属', change_pct: -0.83 },
  ],
  missing: [],
});
const news = (titles: string[], prefix: string) =>
  titles.map((title, i) => ({
    id: prefix + i,
    title,
    summary:
      '这是一条用于检查报纸版式的示例文字，不代表实际发生的新闻。正式日报会交代事件经过，并提供可核验的原始来源。',
    background:
      '观察一项变化，需要把当下消息放回它的政策、技术和社会背景中。此处展示背景段落的排版。',
    impact: '正式内容会区分已经发生的影响与仍待验证的推测，帮助读者理解变化可能与谁有关。',
    uncertainty: '本页仅为视觉演示，具体消息与数字不应作为事实引用。',
    source_ids: ['s' + (i + 1)],
  }));
export const demo: Edition = {
  schema_version: 1,
  date: '2026-01-01',
  cutoff_at: '2026-01-01T16:45:00+08:00',
  title: '看见变化，也看见变化的来处',
  intro:
    '从人工智能的新进展，到身边的公共议题，再到两地市场的收盘信号。每日观察把分散的信息放在同一张桌上，帮助你读懂今天。',
  teaser:
    '今天，哪些技术变化值得认真看一眼？哪些公共议题需要更多背景？两地市场又在如何定价这些变化？每日观察用一份有出处的深度日报，把新闻、背景与影响放在一起。花十分钟，把今天看清楚，也为明天留下一点判断的余地。',
  highlights: [
    '技术走向真实场景，关注能力背后的使用条件。',
    '公共议题看见细节，也看见与日常生活的连接。',
    '收盘数字之外，行业分化更值得耐心阅读。',
  ],
  articles: {
    ai: news(
      [
        '当AI走出对话框：从能力演示到真实场景',
        '模型的下一站，是更可靠的工具协作',
        '开放研究，让进展能够被检查与复现',
      ],
      'ai',
    ),
    society: news(
      [
        '一项公共政策，如何走到日常生活中',
        '城市之间的连接，正在改变什么',
        '数字公共服务：便利之外，也需要透明',
      ],
      'soc',
    ),
  },
  analysis: [
    {
      title: '在快消息之外，给判断一点时间',
      text: '每天都有新的观点，也有新的数字。信息的价值，不只在于它来得早，更在于它是否准确、是否完整，以及它在怎样的背景下发生。\n\n每日观察把事实、背景、影响与不确定性分开呈现。我们希望一份日报留下的，不是更多焦虑，而是一套能够继续追问的线索。',
      source_ids: ['s1'],
    },
  ],
  sources: [1, 2, 3].map((i) => ({
    id: 's' + i,
    title: '示例来源 ' + i + '（仅供版式预览）',
    url: 'https://example.com/source/' + i,
    published_at: '2026-01-01T12:00:00+08:00',
    captured_at: '2026-01-01T16:30:00+08:00',
  })),
  markets: { a: market(), hk: market(true) },
  warnings: ['此页面全部内容与行情均为排版演示，未发布为正式日报。'],
  simulation: true,
  content_hash: '0'.repeat(64),
};
